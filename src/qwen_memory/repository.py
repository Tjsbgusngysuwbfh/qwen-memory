"""
Qwen Memory — CRUD 操作
会话、观察、快照的增删改查，FTS 同步。
"""
import hashlib
import json

from .db import _now, _row_to_dict, days_ago, get_db
from .fts import sync_observation_fts, sync_session_fts

# 兼容别名，供旧代码通过 repository._sync_session_fts 调用
_sync_session_fts = sync_session_fts
_sync_observation_fts = sync_observation_fts


# ============ 版本记录（供 CRUD 内部调用） ============

_VERSION_IMPORTANCE_THRESHOLD = 0.7  # importance < 此值不生成版本
_VERSION_MAX_PER_ENTITY = 10          # 每个实体最多保留版本数


def _record_version(conn, entity_type, entity_id, action, before=None, after=None, importance=None):
    """记录版本。仅 importance >= 0.7 时记录（回滚操作不受此限制）。"""
    # 回滚操作（action 含 rollback）始终记录
    is_rollback = "rollback" in action
    if not is_rollback and importance is not None and importance < _VERSION_IMPORTANCE_THRESHOLD:
        return

    conn.execute("""
        INSERT INTO memory_versions (entity_type, entity_id, action, before_data, after_data, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (entity_type, str(entity_id), action,
          json.dumps(before, ensure_ascii=False) if before else None,
          json.dumps(after, ensure_ascii=False) if after else None,
          _now()))

    # 写入后清理：每个实体只保留最近 N 个版本
    count = conn.execute("""
        SELECT COUNT(*) FROM memory_versions
        WHERE entity_type=? AND entity_id=?
    """, (entity_type, str(entity_id))).fetchone()[0]
    if count > _VERSION_MAX_PER_ENTITY:
        excess = count - _VERSION_MAX_PER_ENTITY
        conn.execute("""
            DELETE FROM memory_versions WHERE id IN (
                SELECT id FROM memory_versions
                WHERE entity_type=? AND entity_id=?
                ORDER BY created_at ASC LIMIT ?
            )
        """, (entity_type, str(entity_id), excess))


def _record_version_raw(conn, entity_type, entity_id, action, before=None, after=None):
    """内部版本记录（不独立开连接）"""
    conn.execute("""
        INSERT INTO memory_versions (entity_type, entity_id, action, before_data, after_data, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (entity_type, str(entity_id), action,
          json.dumps(before, ensure_ascii=False) if before else None,
          json.dumps(after, ensure_ascii=False) if after else None,
          _now()))


# ============ 写入操作 ============

def upsert_session(session_id, summary, summary_short="", project_path="",
                   model="", tools_used=None, key_decisions=None,
                   file_changes=None, tags=None, importance=0.5,
                   token_estimate=0, summary_compact=""):
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
                    file_changes=?, tags=?, token_estimate=?, importance=?, checksum=?,
                    summary_compact=?
                WHERE session_id=?
            """, (now, summary, summary_short, project_path, model,
                  tools_used_j, key_decisions_j, file_changes_j, tags_j,
                  token_estimate, importance, checksum, summary_compact, session_id))
        else:
            conn.execute("""
                INSERT INTO sessions (session_id, created_at, updated_at, summary, summary_short,
                    project_path, model, tools_used, key_decisions, file_changes,
                    tags, token_estimate, importance, checksum, summary_compact)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (session_id, now, now, summary, summary_short, project_path, model,
                  tools_used_j, key_decisions_j, file_changes_j, tags_j,
                  token_estimate, importance, checksum, summary_compact))

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
                       before=before_dict, after=after, importance=importance)

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
                       before=dict(before) if before else None, after=after,
                       importance=before["importance"] if before else None)

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
                       after=dict(row) if row else None, importance=importance)

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
                       after=dict(row) if row else None,
                       importance=importance if importance is not None else (before["importance"] if before else None))

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
        # 用 Python 生成时间阈值，避免 SQL datetime 函数的字符串比较问题
        recent = conn.execute("""
            SELECT COUNT(*) FROM sessions
            WHERE created_at > ?
        """, (days_ago(7),)).fetchone()[0]
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
