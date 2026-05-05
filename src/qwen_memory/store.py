"""
Qwen Memory Store v2 — 统一写入服务 + 版本控制 + FTS 同步 + 融合检索
重写要点：
  1. 单一写入路径：所有写操作走统 service 函数
  2. save 和 end 分离：save 只 upsert 数据，end 只写 ended_at
  3. FTS 用稳定主键（rowid）关联，不再靠 content 回连
  4. 回滚是事务内完整操作：主表 + FTS + 版本记录一起成功或失败
  5. 时间格式统一 UTC ISO
  6. 语义索引有失效检测
  7. 检索层做最小融合：关键词优先 + 语义补充 + 去重
"""
import sqlite3
import json
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path

DB_DIR = Path(__file__).parent / "data"
DB_PATH = DB_DIR / "memories.db"

UTC = timezone.utc


def _now():
    """统一时间格式：UTC ISO"""
    return datetime.now(UTC).isoformat()


def get_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_tables(conn)
    return conn


def _init_tables(conn):
    """初始化表结构 + 自动迁移旧库"""
    # 主表创建（IF NOT EXISTS，旧表跳过）
    for ddl in [
        """CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT,
            summary TEXT, summary_short TEXT, project_path TEXT, model TEXT,
            tools_used TEXT, key_decisions TEXT, file_changes TEXT, tags TEXT,
            token_estimate INTEGER DEFAULT 0, importance REAL DEFAULT 0.5, checksum TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            obs_type TEXT NOT NULL,
            content TEXT NOT NULL,
            context TEXT, impact TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            importance REAL DEFAULT 0.5, tags TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )""",
        """CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            snapshot_type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT, file_list TEXT, metrics TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )""",
        """CREATE TABLE IF NOT EXISTS memory_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            action TEXT NOT NULL,
            before_data TEXT, after_data TEXT,
            created_at TEXT NOT NULL
        )""",
    ]:
        conn.execute(ddl)
    conn.commit()

    # 迁移旧库：补列、补 FTS（在建表之后）
    _migrate(conn)

    # 索引
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_sessions_sid ON sessions(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_obs_sid ON observations(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(obs_type)",
        "CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_versions_entity ON memory_versions(entity_type, entity_id)",
    ]:
        conn.execute(idx)
    conn.commit()


def _migrate(conn):
    """自动迁移旧库 schema（只执行一次）"""
    now = _now()
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver >= 2:
            return
    except Exception:
        ver = 0

    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}

    if "started_at" in cols and "created_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE sessions SET created_at=started_at, updated_at=started_at WHERE created_at=''")
        conn.commit()

    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "created_at" in cols and "updated_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE sessions SET updated_at=created_at WHERE updated_at=''")
        conn.commit()

    if "started_at" not in cols and "created_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE sessions SET created_at=?, updated_at=?", (now, now))
        conn.commit()

    obs_cols = {r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()}
    if "updated_at" not in obs_cols:
        conn.execute("ALTER TABLE observations ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        try:
            conn.execute("UPDATE observations SET updated_at=created_at WHERE updated_at=''")
        except Exception:
            pass
        conn.commit()

    try:
        conn.execute("SELECT count(*) FROM sessions_fts LIMIT 1")
    except Exception:
        try:
            conn.execute("DROP TABLE IF EXISTS sessions_fts")
            conn.execute("DROP TABLE IF EXISTS observations_fts")
        except Exception:
            pass
        conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(session_rowid UNINDEXED, session_id UNINDEXED, summary, summary_short, tags)')
        conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(observation_rowid UNINDEXED, session_id UNINDEXED, obs_type UNINDEXED, content, context, impact)')
        conn.commit()
        for s in conn.execute("SELECT * FROM sessions").fetchall():
            _sync_session_fts(conn, s)
        for o in conn.execute("SELECT * FROM observations").fetchall():
            _sync_observation_fts(conn, o)
        conn.commit()

    conn.execute("PRAGMA user_version=2")
    conn.commit()



# ============ 统一写入服务 ============

def _record_version(conn, entity_type, entity_id, action, before=None, after=None):
    conn.execute("""
        INSERT INTO memory_versions (entity_type, entity_id, action, before_data, after_data, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (entity_type, str(entity_id), action,
          json.dumps(before, ensure_ascii=False) if before else None,
          json.dumps(after, ensure_ascii=False) if after else None,
          _now()))


def _sync_session_fts(conn, row):
    """同步 session 的 FTS 索引"""
    rowid = row["id"]
    # 删除旧的
    conn.execute("DELETE FROM sessions_fts WHERE session_rowid=?", (rowid,))
    # 插入新的
    conn.execute("""
        INSERT INTO sessions_fts(session_rowid, session_id, summary, summary_short, tags)
        VALUES (?, ?, ?, ?, ?)
    """, (rowid, row["session_id"], row["summary"] or "",
          row["summary_short"] or "", row["tags"] or ""))


def _sync_observation_fts(conn, row):
    """同步 observation 的 FTS 索引"""
    rowid = row["id"]
    conn.execute("DELETE FROM observations_fts WHERE observation_rowid=?", (rowid,))
    conn.execute("""
        INSERT INTO observations_fts(observation_rowid, session_id, obs_type, content, context, impact)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (rowid, row["session_id"], row["obs_type"],
          row["content"] or "", row["context"] or "", row["impact"] or ""))


def upsert_session(session_id, summary, summary_short="", project_path="",
                   model="", tools_used=None, key_decisions=None,
                   file_changes=None, tags=None, importance=0.5,
                   token_estimate=0):
    """统一保存：创建或更新，不涉及 ended_at"""
    conn = get_db()
    try:
        now = _now()
        tools_used_j = json.dumps(tools_used or [], ensure_ascii=False)
        key_decisions_j = json.dumps(key_decisions or [], ensure_ascii=False)
        file_changes_j = json.dumps(file_changes or [], ensure_ascii=False)
        tags_j = json.dumps(tags or [], ensure_ascii=False)
        checksum = hashlib.md5((summary or "").encode()).hexdigest()[:12]

        # 记录修改前
        before = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                              (session_id,)).fetchone()
        before_dict = dict(before) if before else None

        is_update = before is not None

        if is_update:
            conn.execute("""
                UPDATE sessions SET
                    updated_at=?, summary=?, summary_short=?,
                    project_path=?, model=?, tools_used=?, key_decisions=?,
                    file_changes=?, tags=?, token_estimate=?, importance=?, checksum=?
                WHERE session_id=?
            """, (now, summary, summary_short, project_path, model,
                  tools_used_j, key_decisions_j, file_changes_j, tags_j,
                  token_estimate, importance, checksum, session_id))
        else:
            conn.execute("""
                INSERT INTO sessions (session_id, created_at, updated_at, summary, summary_short,
                    project_path, model, tools_used, key_decisions, file_changes,
                    tags, token_estimate, importance, checksum)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (session_id, now, now, summary, summary_short, project_path, model,
                  tools_used_j, key_decisions_j, file_changes_j, tags_j,
                  token_estimate, importance, checksum))

        # 同步 FTS
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                           (session_id,)).fetchone()
        if row:
            _sync_session_fts(conn, row)

        # 记录版本
        after = dict(conn.execute("SELECT * FROM sessions WHERE session_id=?",
                                  (session_id,)).fetchone()) if row else None
        action = "update" if is_update else "create"
        _record_version(conn, "session", session_id, action,
                       before=before_dict, after=after)

        conn.commit()
        return True
    finally:
        conn.close()


def end_session(session_id, summary=None, summary_short=None):
    """结束会话：只写 ended_at 和可选的摘要更新"""
    conn = get_db()
    try:
        now = _now()
        before = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                              (session_id,)).fetchone()
        if not before:
            return False

        if summary:
            conn.execute("""
                UPDATE sessions SET ended_at=?, summary=?, summary_short=?, updated_at=?
                WHERE session_id=?
            """, (now, summary, summary_short or "", now, session_id))
        else:
            conn.execute("UPDATE sessions SET ended_at=?, updated_at=? WHERE session_id=?",
                         (now, now, session_id))

        row = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                           (session_id,)).fetchone()
        if row:
            _sync_session_fts(conn, row)

        after = dict(row) if row else None
        _record_version(conn, "session", session_id, "end",
                       before=dict(before) if before else None, after=after)

        conn.commit()
        return True
    finally:
        conn.close()


def add_observation(session_id, obs_type, content, context="",
                    impact="", importance=0.5, tags=None):
    """添加观察：写主表 + FTS + 版本记录"""
    conn = get_db()
    try:
        now = _now()
        tags_j = json.dumps(tags or [], ensure_ascii=False)

        cursor = conn.execute("""
            INSERT INTO observations (session_id, obs_type, content, context,
                impact, created_at, updated_at, importance, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, obs_type, content, context, impact, now, now, importance, tags_j))

        obs_id = cursor.lastrowid

        row = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
        if row:
            _sync_observation_fts(conn, row)

        _record_version(conn, "observation", obs_id, "create",
                       after=dict(row) if row else None)

        conn.commit()
        return obs_id
    finally:
        conn.close()


def update_observation(obs_id, content=None, context=None, impact=None,
                       importance=None, tags=None):
    """更新观察：写主表 + FTS + 版本记录"""
    conn = get_db()
    try:
        now = _now()
        before = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
        if not before:
            return False

        updates = ["updated_at=?"]
        params = [now]

        if content is not None:
            updates.append("content=?")
            params.append(content)
        if context is not None:
            updates.append("context=?")
            params.append(context)
        if impact is not None:
            updates.append("impact=?")
            params.append(impact)
        if importance is not None:
            updates.append("importance=?")
            params.append(importance)
        if tags is not None:
            updates.append("tags=?")
            params.append(json.dumps(tags, ensure_ascii=False))

        params.append(obs_id)
        conn.execute(f"UPDATE observations SET {', '.join(updates)} WHERE id=?", params)

        row = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
        if row:
            _sync_observation_fts(conn, row)

        _record_version(conn, "observation", obs_id, "update",
                       before=dict(before) if before else None,
                       after=dict(row) if row else None)

        conn.commit()
        return True
    finally:
        conn.close()


def save_snapshot(session_id, snapshot_type, title, description="",
                  file_list=None, metrics=None):
    now = _now()
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO snapshots (session_id, snapshot_type, title, description,
                file_list, metrics, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (session_id, snapshot_type, title, description,
              json.dumps(file_list or [], ensure_ascii=False),
              json.dumps(metrics or {}, ensure_ascii=False), now))
        conn.commit()
    finally:
        conn.close()


# ============ 读取操作 ============

def _row_to_dict(row):
    return dict(row) if row else None


def get_session_detail(session_id):
    conn = get_db()
    try:
        session = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                               (session_id,)).fetchone()
        if not session:
            return None
        obs = conn.execute("SELECT * FROM observations WHERE session_id=? ORDER BY created_at",
                           (session_id,)).fetchall()
        snaps = conn.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY created_at",
                             (session_id,)).fetchall()
        return {
            "session": _row_to_dict(session),
            "observations": [_row_to_dict(o) for o in obs],
            "snapshots": [_row_to_dict(s) for s in snaps],
        }
    finally:
        conn.close()


def get_recent_sessions(limit=10):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT session_id, summary_short, created_at, ended_at, importance, tags
            FROM sessions ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_stats():
    conn = get_db()
    try:
        stats = {}
        stats["total_sessions"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        stats["total_observations"] = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        stats["total_snapshots"] = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        stats["total_versions"] = conn.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0]
        # 用 created_at（UTC ISO）比较
        recent = conn.execute("""
            SELECT COUNT(*) FROM sessions
            WHERE created_at > datetime('now', '-7 days')
        """).fetchone()[0]
        stats["sessions_last_7d"] = recent
        by_type = conn.execute("""
            SELECT obs_type, COUNT(*) as cnt FROM observations GROUP BY obs_type ORDER BY cnt DESC
        """).fetchall()
        stats["observations_by_type"] = {r["obs_type"]: r["cnt"] for r in by_type}
        avg_imp = conn.execute("SELECT AVG(importance) FROM sessions").fetchone()[0]
        stats["avg_importance"] = round(avg_imp or 0, 2)
        return stats
    finally:
        conn.close()


# ============ 检索（融合版） ============

def _fts_search_sessions(conn, query, limit=20):
    """FTS 搜索会话，返回 session_id 列表"""
    if not query.strip():
        return []
    try:
        rows = conn.execute("""
            SELECT f.session_id, rank
            FROM sessions_fts f
            WHERE sessions_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        return [(r["session_id"], abs(r["rank"])) for r in rows]
    except Exception:
        return []


def _fts_search_observations(conn, query, limit=20):
    """FTS 搜索观察，返回 observation id 列表"""
    if not query.strip():
        return []
    try:
        rows = conn.execute("""
            SELECT f.observation_rowid as id, rank
            FROM observations_fts f
            WHERE observations_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        return [(r["id"], abs(r["rank"])) for r in rows]
    except Exception:
        return []


def _like_search_sessions(conn, query, limit=20):
    """LIKE 兜底搜索"""
    like_q = f"%{query}%"
    rows = conn.execute("""
        SELECT session_id FROM sessions
        WHERE summary LIKE ? OR summary_short LIKE ? OR tags LIKE ?
        ORDER BY importance DESC LIMIT ?
    """, (like_q, like_q, like_q, limit)).fetchall()
    return [r["session_id"] for r in rows]


def _like_search_observations(conn, query, limit=20):
    like_q = f"%{query}%"
    rows = conn.execute("""
        SELECT id FROM observations
        WHERE content LIKE ? OR context LIKE ? OR impact LIKE ?
        ORDER BY importance DESC LIMIT ?
    """, (like_q, like_q, like_q, limit)).fetchall()
    return [r["id"] for r in rows]


def _get_sessions_by_ids(conn, session_ids):
    if not session_ids:
        return []
    placeholders = ",".join("?" * len(session_ids))
    # 用 CASE 保序：按传入顺序排列
    order_cases = " ".join(
        f"WHEN session_id=? THEN {i}" for i, sid in enumerate(session_ids)
    )
    rows = conn.execute(
        f"SELECT * FROM sessions WHERE session_id IN ({placeholders}) "
        f"ORDER BY CASE session_id {order_cases} END",
        session_ids + session_ids  # IN 参数 + ORDER BY 参数
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _get_obs_by_ids(conn, obs_ids):
    if not obs_ids:
        return []
    placeholders = ",".join("?" * len(obs_ids))
    order_cases = " ".join(
        f"WHEN id=? THEN {i}" for i, oid in enumerate(obs_ids)
    )
    rows = conn.execute(
        f"SELECT * FROM observations WHERE id IN ({placeholders}) "
        f"ORDER BY CASE id {order_cases} END",
        obs_ids + obs_ids
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_sessions(query, limit=10):
    """搜索会话：FTS → LIKE 兜底"""
    conn = get_db()
    try:
        ids = _fts_search_sessions(conn, query, limit)
        if ids:
            return _get_sessions_by_ids(conn, [i[0] for i in ids])
        like_ids = _like_search_sessions(conn, query, limit)
        return _get_sessions_by_ids(conn, like_ids)
    finally:
        conn.close()


def search_observations(query, limit=20, obs_type=None):
    """搜索观察：FTS → LIKE 兜底"""
    conn = get_db()
    try:
        ids = _fts_search_observations(conn, query, limit)
        if ids:
            obs = _get_obs_by_ids(conn, [i[0] for i in ids])
        else:
            like_ids = _like_search_observations(conn, query, limit)
            obs = _get_obs_by_ids(conn, like_ids)

        if obs_type:
            obs = [o for o in obs if o["obs_type"] == obs_type]
        return obs
    finally:
        conn.close()


def fused_search(query, limit=10):
    """融合检索：关键词优先 + 语义补充 + 有序去重"""
    conn = get_db()
    try:
        seen_sessions = set()
        seen_obs = set()
        ordered_sessions = []
        ordered_obs = []

        # 1. 关键词搜索（优先，保留 FTS rank 排序）
        for sid, _ in _fts_search_sessions(conn, query, limit):
            if sid not in seen_sessions:
                seen_sessions.add(sid)
                ordered_sessions.append(sid)
        if not ordered_sessions:
            for sid in _like_search_sessions(conn, query, limit):
                if sid not in seen_sessions:
                    seen_sessions.add(sid)
                    ordered_sessions.append(sid)

        for oid, _ in _fts_search_observations(conn, query, limit):
            if oid not in seen_obs:
                seen_obs.add(oid)
                ordered_obs.append(oid)
        if not ordered_obs:
            for oid in _like_search_observations(conn, query, limit):
                if oid not in seen_obs:
                    seen_obs.add(oid)
                    ordered_obs.append(oid)

        # 2. 语义搜索补充（去重，追加到有序列表尾部）
        try:
            try:
                from .semantic import semantic_search
            except ImportError:
                from semantic import semantic_search
            sem = semantic_search(query, top_k=limit)
            for s in sem.get("sessions", []):
                sid = s.get("session_id", "")
                if sid and sid not in seen_sessions:
                    seen_sessions.add(sid)
                    ordered_sessions.append(sid)
            for o in sem.get("observations", []):
                oid = o.get("observation_id")
                if oid and oid not in seen_obs:
                    seen_obs.add(oid)
                    ordered_obs.append(oid)
        except Exception:
            pass

        # 3. 按有序列表取详情
        sessions = _get_sessions_by_ids(conn, ordered_sessions[:limit])
        observations = _get_obs_by_ids(conn, ordered_obs[:limit])

        return {"sessions": sessions, "observations": observations}
    finally:
        conn.close()


# ============ 版本控制 / 回滚 ============

def get_version_history(entity_type, entity_id, limit=20):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT * FROM memory_versions
            WHERE entity_type=? AND entity_id=?
            ORDER BY created_at DESC LIMIT ?
        """, (entity_type, str(entity_id), limit)).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_all_versions(limit=50):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT * FROM memory_versions ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def rollback_observation(obs_id, to_version_id=None, steps=1):
    """回滚观察：事务内完成 主表 + FTS + 版本记录"""
    conn = get_db()
    try:
        current = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
        if not current:
            return False, f"Observation #{obs_id} not found"

        current_dict = dict(current)

        versions = conn.execute("""
            SELECT * FROM memory_versions
            WHERE entity_type='observation' AND entity_id=?
            ORDER BY created_at DESC
        """, (str(obs_id),)).fetchall()

        if not versions:
            return False, "No version history"

        # 找目标版本
        target = None
        if to_version_id:
            for v in versions:
                if v["id"] == to_version_id and v["after_data"]:
                    target = v
                    break
        else:
            count = 0
            for v in versions:
                if v["after_data"]:
                    count += 1
                    if count > steps:
                        target = v
                        break

        if not target:
            return False, "Not enough versions to rollback"

        target_data = json.loads(target["after_data"])

        # 事务内完成所有操作
        try:
            conn.execute("BEGIN")

            # pre-rollback 版本
            _record_version_raw(conn, "observation", obs_id, "pre-rollback",
                               before=current_dict, after=current_dict)

            # 回滚主表
            conn.execute("""
                UPDATE observations SET
                    session_id=?, obs_type=?, content=?, context=?,
                    impact=?, importance=?, tags=?, updated_at=?
                WHERE id=?
            """, (target_data.get("session_id", current_dict["session_id"]),
                  target_data.get("obs_type", current_dict["obs_type"]),
                  target_data.get("content", current_dict["content"]),
                  target_data.get("context", current_dict.get("context", "")),
                  target_data.get("impact", current_dict.get("impact", "")),
                  target_data.get("importance", current_dict["importance"]),
                  target_data.get("tags", current_dict.get("tags", "[]")),
                  _now(), obs_id))

            # 同步 FTS
            row = conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
            if row:
                _sync_observation_fts(conn, row)

            # rollback 版本
            _record_version_raw(conn, "observation", obs_id, "rollback",
                               before=current_dict, after=dict(row) if row else None)

            conn.execute("COMMIT")
            return True, f"Rolled back to version #{target['id']}"
        except Exception as e:
            conn.execute("ROLLBACK")
            return False, f"Rollback failed: {e}"
    finally:
        conn.close()


def rollback_session(session_id, to_version_id=None, steps=1):
    """回滚会话：事务内完成 主表 + FTS + 版本记录"""
    conn = get_db()
    try:
        current = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                               (session_id,)).fetchone()
        if not current:
            return False, f"Session {session_id} not found"

        current_dict = dict(current)

        versions = conn.execute("""
            SELECT * FROM memory_versions
            WHERE entity_type='session' AND entity_id=?
            ORDER BY created_at DESC
        """, (session_id,)).fetchall()

        if not versions:
            return False, "No version history"

        target = None
        if to_version_id:
            for v in versions:
                if v["id"] == to_version_id and v["after_data"]:
                    target = v
                    break
        else:
            count = 0
            for v in versions:
                if v["after_data"]:
                    count += 1
                    if count > steps:
                        target = v
                        break

        if not target:
            return False, "Not enough versions to rollback"

        target_data = json.loads(target["after_data"])

        try:
            conn.execute("BEGIN")

            _record_version_raw(conn, "session", session_id, "pre-rollback",
                               before=current_dict, after=current_dict)

            conn.execute("""
                UPDATE sessions SET
                    summary=?, summary_short=?, importance=?, tags=?,
                    tools_used=?, key_decisions=?, file_changes=?,
                    project_path=?, model=?, updated_at=?
                WHERE session_id=?
            """, (target_data.get("summary", current_dict["summary"]),
                  target_data.get("summary_short", current_dict.get("summary_short", "")),
                  target_data.get("importance", current_dict["importance"]),
                  target_data.get("tags", current_dict.get("tags", "[]")),
                  target_data.get("tools_used", current_dict.get("tools_used", "[]")),
                  target_data.get("key_decisions", current_dict.get("key_decisions", "[]")),
                  target_data.get("file_changes", current_dict.get("file_changes", "[]")),
                  target_data.get("project_path", current_dict.get("project_path", "")),
                  target_data.get("model", current_dict.get("model", "")),
                  _now(), session_id))

            row = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                               (session_id,)).fetchone()
            if row:
                _sync_session_fts(conn, row)

            _record_version_raw(conn, "session", session_id, "rollback",
                               before=current_dict, after=dict(row) if row else None)

            conn.execute("COMMIT")
            return True, f"Rolled back to version #{target['id']}"
        except Exception as e:
            conn.execute("ROLLBACK")
            return False, f"Rollback failed: {e}"
    finally:
        conn.close()


def _record_version_raw(conn, entity_type, entity_id, action, before=None, after=None):
    """内部版本记录（不独立开连接）"""
    conn.execute("""
        INSERT INTO memory_versions (entity_type, entity_id, action, before_data, after_data, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (entity_type, str(entity_id), action,
          json.dumps(before, ensure_ascii=False) if before else None,
          json.dumps(after, ensure_ascii=False) if after else None,
          _now()))


# ============ 语义索引失效检测 ============

def _get_db_content_signature():
    """计算数据库内容签名：行数 + 字段长度 + MAX(updated_at)，比纯计数更稳"""
    conn = get_db()
    try:
        s_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        o_count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        # 聚合内容长度（捕获内容变更）
        s_len = conn.execute("SELECT COALESCE(SUM(LENGTH(summary)+LENGTH(summary_short)+LENGTH(tags)),0) FROM sessions").fetchone()[0]
        o_len = conn.execute("SELECT COALESCE(SUM(LENGTH(content)+LENGTH(context)+LENGTH(impact)),0) FROM observations").fetchone()[0]
        # 最后修改时间
        s_max = conn.execute("SELECT COALESCE(MAX(updated_at),'') FROM sessions").fetchone()[0]
        o_max = conn.execute("SELECT COALESCE(MAX(updated_at),'') FROM observations").fetchone()[0]
        return f"{s_count}-{o_count}-{s_len}-{o_len}-{s_max}-{o_max}"
    finally:
        conn.close()


def check_semantic_index_fresh():
    """检查语义索引是否过期"""
    from pathlib import Path
    index_path = Path(__file__).parent / "data" / "semantic_index.json"
    meta_path = Path(__file__).parent / "data" / "semantic_meta.json"

    current_checksum = _get_db_content_signature()

    if not meta_path.exists():
        return False, "no index"

    try:
        with open(meta_path, 'r') as f:
            meta = json.load(f)
        if meta.get("checksum") == current_checksum:
            return True, "fresh"
        else:
            return False, f"stale (db={current_checksum}, index={meta.get('checksum')})"
    except Exception:
        return False, "meta read error"


def save_semantic_meta():
    """保存语义索引元数据"""
    meta_path = Path(__file__).parent / "data" / "semantic_meta.json"
    meta = {"checksum": _get_db_content_signature(), "updated_at": _now()}
    with open(meta_path, 'w') as f:
        json.dump(meta, f)
