"""
Qwen Memory — 数据库连接和基础工具函数
从 store.py 拆分而来，提供数据库连接、时间工具、行转字典等基础能力。
"""
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_DIR = Path(__file__).parent / "data"
DB_PATH = DB_DIR / "memories.db"

UTC = timezone.utc


def set_db_path(path):
    """覆盖数据库路径（测试用）"""
    global DB_PATH
    DB_PATH = Path(path)


def _now():
    """统一时间格式：UTC ISO"""
    return datetime.now(UTC).isoformat()


def days_ago(n):
    """返回 n 天前的 UTC ISO 时间字符串，用于 SQL 参数化查询"""
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


def _row_to_dict(row):
    return dict(row) if row else None


def get_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 延迟导入避免循环依赖：migrations → repository → db
    from .migrations import init_all
    init_all(conn)
    return conn
