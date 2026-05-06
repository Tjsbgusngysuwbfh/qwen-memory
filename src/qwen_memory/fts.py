"""
Qwen Memory — FTS 全文检索同步
从 repository.py 中提取，独立管理 FTS 索引的同步和重建。

职责：
  - sync_session_fts     — 同步单个会话的 FTS 索引
  - sync_observation_fts — 同步单个观察的 FTS 索引
  - rebuild_fts          — 重建所有 FTS 索引
"""


def sync_session_fts(conn, row):
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


def sync_observation_fts(conn, row):
    """同步 observation 的 FTS 索引"""
    rowid = row["id"]
    conn.execute("DELETE FROM observations_fts WHERE observation_rowid=?", (rowid,))
    conn.execute("""
        INSERT INTO observations_fts(observation_rowid, session_id, obs_type, content, context, impact)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (rowid, row["session_id"], row["obs_type"],
          row["content"] or "", row["context"] or "", row["impact"] or ""))


def rebuild_fts(conn):
    """重建所有 FTS 索引（drop + recreate + 全量同步）"""
    conn.execute("DROP TABLE IF EXISTS sessions_fts")
    conn.execute("DROP TABLE IF EXISTS observations_fts")
    conn.execute(
        'CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts '
        'USING fts5(session_rowid UNINDEXED, session_id UNINDEXED, summary, summary_short, tags)'
    )
    conn.execute(
        'CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts '
        'USING fts5(observation_rowid UNINDEXED, session_id UNINDEXED, obs_type UNINDEXED, content, context, impact)'
    )
    for s in conn.execute("SELECT * FROM sessions").fetchall():
        sync_session_fts(conn, s)
    for o in conn.execute("SELECT * FROM observations").fetchall():
        sync_observation_fts(conn, o)
    conn.commit()


# ============ 兼容别名（供旧代码 import） ============

_sync_session_fts = sync_session_fts
_sync_observation_fts = sync_observation_fts
