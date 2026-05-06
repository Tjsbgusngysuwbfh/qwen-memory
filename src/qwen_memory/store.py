"""
Qwen Memory Store v3 — 薄代理层
所有实际实现已拆分到：
  db.py         — 数据库连接、常量、工具函数
  migrations.py — Schema 演进、建表、种子数据
  fts.py        — FTS 全文检索同步（索引同步、重建）
  repository.py — CRUD 操作（会话、观察、快照）
  services.py   — 版本控制、检索、语义索引、预算日志、规则

本文件保留所有原有公开函数签名，内部委托给新模块，
确保 mem.py、mcp_server.py、budget.py、test_*.py 等无需改动。
"""

# ============ 重新导出 db 层 ============
from .db import DB_DIR, DB_PATH, UTC, _now, _row_to_dict, get_db  # noqa: F401


def set_db_path(path):
    """覆盖数据库路径（测试用）— 同时更新 store.DB_PATH 和 db.DB_PATH"""
    from . import db as _db
    global DB_PATH
    _db.set_db_path(path)
    DB_PATH = _db.DB_PATH  # 同步引用


# ============ 重新导出 fts 层 ============
from .fts import (  # noqa: F401
    sync_session_fts as _sync_session_fts,
    sync_observation_fts as _sync_observation_fts,
    rebuild_fts,
)

# ============ 重新导出 repository 层 ============
from .repository import (  # noqa: F401
    upsert_session,
    end_session,
    add_observation,
    update_observation,
    save_snapshot,
    get_session_detail,
    get_recent_sessions,
    get_stats,
)

# ============ 重新导出 services 层 ============
from .services import (  # noqa: F401
    get_version_history,
    get_all_versions,
    restore_observation_content,
    restore_session_content,
    rollback_observation,  # 兼容别名
    rollback_session,      # 兼容别名
    search_sessions,
    search_observations,
    fused_search,
    check_semantic_index_fresh,
    save_semantic_meta,
    log_budget,
    get_budget_log,
    get_enabled_rules,
    log_trigger,
)
