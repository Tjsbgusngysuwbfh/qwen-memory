"""
Qwen Memory — 版本控制、检索、语义索引、预算日志、规则
从 store.py 拆分而来的高级服务函数。
"""
import hashlib
import json
import math
from pathlib import Path

from .db import _now, _row_to_dict, get_db
from .fts import sync_observation_fts, sync_session_fts
from .repository import (
    _record_version, _record_version_raw,
)


# ============ 搜索评分参数 ============
SEARCH_WEIGHTS = {
    "fts_base": 0.5,             # FTS命中基础分
    "fts_rank_cap": 0.5,         # FTS rank加成上限
    "like_base": 0.3,            # LIKE命中基础分
    "tfidf_base": 0.3,           # 语义命中基础分
    "tfidf_bonus": 0.3,          # 语义命中额外加成
    "importance_weight": 0.2,    # importance加权系数
    "recency_half_life_days": 30,  # 时间衰减半衰期
}


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


def restore_observation_content(obs_id, to_version_id=None, steps=1):
    """恢复观察内容层：事务内完成 主表 + FTS + 版本记录

    注意：此操作仅回滚内容层字段（content, context, impact, importance, tags 等），
    不回滚生命周期字段。名称用 restore 而非 rollback 以准确反映语义。
    """
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
                sync_observation_fts(conn, row)

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


def restore_session_content(session_id, to_version_id=None, steps=1):
    """恢复会话内容层：事务内完成 主表 + FTS + 版本记录

    注意：此操作仅回滚内容层字段（summary、tags、key_decisions 等），
    不回滚生命周期字段（ended_at、token_estimate）。
    名称用 restore 而非 rollback 以准确反映语义。
    """
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

            # 注意：只回滚内容层字段（summary、tags、key_decisions 等）
            # 不回滚生命周期字段：ended_at、token_estimate（它们保留当前值）
            # summary_compact 作为内容层也一并回滚
            # checksum 回滚后重算（因为内容变了）
            conn.execute("""
                UPDATE sessions SET
                    summary=?, summary_short=?, importance=?, tags=?,
                    tools_used=?, key_decisions=?, file_changes=?,
                    project_path=?, model=?, summary_compact=?,
                    checksum=?, updated_at=?
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
                  target_data.get("summary_compact", current_dict.get("summary_compact", "")),
                  hashlib.md5((target_data.get("summary", current_dict["summary"]) or "").encode()).hexdigest()[:12],
                  _now(), session_id))

            row = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                               (session_id,)).fetchone()
            if row:
                sync_session_fts(conn, row)

            _record_version_raw(conn, "session", session_id, "rollback",
                               before=current_dict, after=dict(row) if row else None)

            conn.execute("COMMIT")
            return True, f"Rolled back to version #{target['id']}"
        except Exception as e:
            conn.execute("ROLLBACK")
            return False, f"Rollback failed: {e}"
    finally:
        conn.close()


# ============ 检索 ============

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


def _fts_search_observations(conn, query, limit=20, obs_type=None):
    """FTS 搜索观察，返回 observation id 列表"""
    if not query.strip():
        return []
    try:
        if obs_type:
            rows = conn.execute("""
                SELECT f.observation_rowid as id, rank
                FROM observations_fts f
                JOIN observations o ON f.observation_rowid = o.id
                WHERE observations_fts MATCH ? AND o.obs_type = ?
                ORDER BY rank
                LIMIT ?
            """, (query, obs_type, limit)).fetchall()
        else:
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


def _like_search_observations(conn, query, limit=20, obs_type=None):
    like_q = f"%{query}%"
    if obs_type:
        rows = conn.execute("""
            SELECT id FROM observations
            WHERE (content LIKE ? OR context LIKE ? OR impact LIKE ?)
            AND obs_type = ?
            ORDER BY importance DESC LIMIT ?
        """, (like_q, like_q, like_q, obs_type, limit)).fetchall()
    else:
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
    """搜索观察：FTS → LIKE 兜底，obs_type 在 SQL 阶段过滤"""
    conn = get_db()
    try:
        ids = _fts_search_observations(conn, query, limit, obs_type=obs_type)
        if ids:
            obs = _get_obs_by_ids(conn, [i[0] for i in ids])
        else:
            like_ids = _like_search_observations(conn, query, limit, obs_type=obs_type)
            obs = _get_obs_by_ids(conn, like_ids)

        # 防御性断言：SQL 已过滤，此处仅做兜底校验
        if obs_type:
            assert all(o["obs_type"] == obs_type for o in obs), \
                f"obs_type mismatch: SQL filter should have excluded non-{obs_type} rows"
        return obs
    finally:
        conn.close()


def _recency_weight(created_at, half_life_days=None):
    """时间衰减权重：exp(-days/half_life)，返回 0.0 ~ 1.0"""
    if half_life_days is None:
        half_life_days = SEARCH_WEIGHTS["recency_half_life_days"]
    if not created_at:
        return 0.5
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta_days = max(0, (now - dt).total_seconds() / 86400)
        return math.exp(-delta_days / half_life_days)
    except Exception:
        return 0.5


def fused_search(query, limit=10):
    """融合检索：统一评分排序 + 有序去重

    评分规则（参数见 SEARCH_WEIGHTS）：
      - FTS 命中：base = fts_base + min(rank/10, fts_rank_cap)
      - LIKE 命中（排除 FTS 已有）：base = like_base
      - 语义命中（排除已有）：base = tfidf_base + tfidf * tfidf_bonus
      - final = base × (1 + importance_weight × importance) × recency
    """
    conn = get_db()
    try:
        # 收集所有候选：(entity_type, entity_id, base_score, rank_info)
        # entity_type: "session" / "observation"
        candidates = {}  # key -> {"type", "id", "base", "importance", "created_at"}

        # --- 1. FTS 搜索（最高优先级）---
        fts_sessions = _fts_search_sessions(conn, query, limit)
        fts_obs = _fts_search_observations(conn, query, limit)

        for sid, rank in fts_sessions:
            candidates[("session", sid)] = {
                "type": "session", "id": sid,
                "base": SEARCH_WEIGHTS["fts_base"] + min(rank / 10.0, SEARCH_WEIGHTS["fts_rank_cap"]),
                "source": "fts",
            }
        for oid, rank in fts_obs:
            candidates[("observation", oid)] = {
                "type": "observation", "id": oid,
                "base": SEARCH_WEIGHTS["fts_base"] + min(rank / 10.0, SEARCH_WEIGHTS["fts_rank_cap"]),
                "source": "fts",
            }

        # --- 2. LIKE 兜底（排除 FTS 已有的）---
        like_sessions = _like_search_sessions(conn, query, limit)
        like_obs = _like_search_observations(conn, query, limit)

        for sid in like_sessions:
            key = ("session", sid)
            if key not in candidates:
                candidates[key] = {
                    "type": "session", "id": sid,
                    "base": SEARCH_WEIGHTS["like_base"],
                    "source": "like",
                }
        for oid in like_obs:
            key = ("observation", oid)
            if key not in candidates:
                candidates[key] = {
                    "type": "observation", "id": oid,
                    "base": SEARCH_WEIGHTS["like_base"],
                    "source": "like",
                }

        # --- 3. 语义补充（排除已有的）---
        try:
            from semantic import semantic_search
            sem = semantic_search(query, top_k=limit)
            for s in sem.get("sessions", []):
                sid = s.get("session_id", "")
                key = ("session", sid)
                if sid and key not in candidates:
                    tfidf = float(s.get("score", 0))
                    candidates[key] = {
                        "type": "session", "id": sid,
                        "base": SEARCH_WEIGHTS["tfidf_base"] + tfidf * SEARCH_WEIGHTS["tfidf_bonus"],
                        "source": "semantic",
                    }
            for o in sem.get("observations", []):
                oid = o.get("observation_id")
                key = ("observation", oid)
                if oid and key not in candidates:
                    tfidf = float(o.get("score", 0))
                    candidates[key] = {
                        "type": "observation", "id": oid,
                        "base": SEARCH_WEIGHTS["tfidf_base"] + tfidf * SEARCH_WEIGHTS["tfidf_bonus"],
                        "source": "semantic",
                    }
        except Exception:
            pass

        if not candidates:
            return {"sessions": [], "observations": []}

        # --- 4. 批量获取详情以补全 importance 和 created_at ---
        session_ids = [c["id"] for c in candidates.values() if c["type"] == "session"]
        obs_ids = [c["id"] for c in candidates.values() if c["type"] == "observation"]

        sessions_detail = {s["session_id"]: s for s in _get_sessions_by_ids(conn, session_ids)}
        obs_detail = {o["id"]: o for o in _get_obs_by_ids(conn, obs_ids)}

        # --- 5. 统一评分 ---
        for c in candidates.values():
            if c["type"] == "session":
                detail = sessions_detail.get(c["id"], {})
            else:
                detail = obs_detail.get(c["id"], {})

            importance = float(detail.get("importance", 0.5))
            created_at = detail.get("created_at", "")

            importance_weight = 1.0 + SEARCH_WEIGHTS["importance_weight"] * importance
            recency = _recency_weight(created_at)
            c["final_score"] = c["base"] * importance_weight * recency

        # --- 6. 按 final_score 降序排序 + 每类最少保留名额 ---
        ranked = sorted(candidates.values(), key=lambda c: c["final_score"], reverse=True)

        # 分离 sessions 和 observations
        sessions = [r for r in ranked if r["type"] == "session"]
        observations = [r for r in ranked if r["type"] == "observation"]

        # 每类至少保留 min(3, limit//2) 条
        min_per_type = min(3, limit // 2)
        selected_sessions = sessions[:min_per_type]
        selected_obs = observations[:min_per_type]

        # 剩余名额按总分竞争
        selected_ids = set(
            (r["type"], r["id"]) for r in selected_sessions + selected_obs
        )
        remaining = [r for r in ranked if (r["type"], r["id"]) not in selected_ids]
        remaining_budget = limit - len(selected_sessions) - len(selected_obs)
        selected_remaining = remaining[:remaining_budget]

        result = selected_sessions + selected_obs + selected_remaining

        result_session_ids = [c["id"] for c in result if c["type"] == "session"]
        result_obs_ids = [c["id"] for c in result if c["type"] == "observation"]

        return {
            "sessions": _get_sessions_by_ids(conn, result_session_ids),
            "observations": _get_obs_by_ids(conn, result_obs_ids),
        }
    finally:
        conn.close()


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


# ============ 预算日志 ============

def log_budget(query, max_tokens, used_tokens, sessions_injected=0,
               obs_injected=0, compact_text="", cache_hit=0):
    """记录每次记忆注入的预算使用情况"""
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO memory_budget_log
                (query, max_tokens, used_tokens, sessions_injected,
                 obs_injected, compact_text, cache_hit, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (query, max_tokens, used_tokens, sessions_injected,
              obs_injected, compact_text, cache_hit, _now()))
        conn.commit()
    finally:
        conn.close()


def get_budget_log(limit=20):
    """查询最近的预算日志"""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT * FROM memory_budget_log
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# ============ 规则 ============

def get_enabled_rules():
    """获取所有启用的上下文规则（按优先级降序）"""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT * FROM context_rules WHERE enabled=1
            ORDER BY priority DESC
        """).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# ============ 触发路由命中日志 ============

def log_trigger(message, result):
    """记录触发路由命中日志"""
    message_hash = hashlib.md5((message or "").encode()).hexdigest()[:16]
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO trigger_log (message_hash, message_preview, matched_rule, "
            "action, token_budget, match_type, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message_hash, (message or "")[:50], result.matched_rule,
             result.action, result.token_budget, result.matched_type,
             result.reason, _now())
        )
        conn.commit()
    finally:
        conn.close()


# ============ 兼容别名 ============
# rollback_xxx → restore_xxx_content
# 保留旧名称避免外部调用方崩溃（mcp_server.py、mem.py 等仍可通过旧名调用）

rollback_session = restore_session_content
rollback_observation = restore_observation_content
