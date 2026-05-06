"""
Qwen Memory — Schema 演进
建表、种子数据、版本迁移。
"""
import json

from .db import _now

# ============ Schema 版本历史 ============
# v1: 初始建表 (sessions, observations, snapshots, fts)
# v2: 添加 memory_versions 表
# v3: 添加 sessions.summary_compact 列
# v4: 添加 context_rules 表 + 默认规则种子
# v5: 添加 memory_budget_log.cache_hit 列
# v6: 添加 trigger_log 表（规则命中日志）


def init_all(conn):
    """初始化表结构 + 自动迁移旧库 + 建索引 + 种子数据"""
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
        """CREATE TABLE IF NOT EXISTS context_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            trigger_type TEXT NOT NULL,
            action TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 0,
            keywords TEXT DEFAULT '[]',
            pattern TEXT DEFAULT '',
            description TEXT DEFAULT '',
            token_budget INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS memory_budget_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            max_tokens INTEGER NOT NULL,
            used_tokens INTEGER NOT NULL,
            sessions_injected INTEGER DEFAULT 0,
            obs_injected INTEGER DEFAULT 0,
            compact_text TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS trigger_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_hash TEXT NOT NULL,
            message_preview TEXT,
            matched_rule TEXT NOT NULL,
            action TEXT NOT NULL,
            token_budget INTEGER DEFAULT 0,
            match_type TEXT,
            reason TEXT,
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
        "CREATE INDEX IF NOT EXISTS idx_context_rules_name ON context_rules(name)",
        "CREATE INDEX IF NOT EXISTS idx_context_rules_enabled ON context_rules(enabled, priority DESC)",
        "CREATE INDEX IF NOT EXISTS idx_budget_log_created ON memory_budget_log(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_trigger_log_rule ON trigger_log(matched_rule)",
        "CREATE INDEX IF NOT EXISTS idx_trigger_log_created ON trigger_log(created_at)",
    ]:
        conn.execute(idx)
    conn.commit()

    # 种子数据：首次初始化时写入默认上下文规则
    _seed_default_rules(conn)


def _seed_default_rules(conn):
    """首次初始化时写入默认上下文规则（已存在则跳过）"""
    now = _now()
    default_rules = [
        ("greeting_skip", "keyword", "SKIP", 1, 100,
         json.dumps(["你好", "hi", "hello", "嗨", "早上好", "晚上好", "下午好",
                      "在吗", "在不在", "hey"], ensure_ascii=False),
         "", "问候语 → 跳过注入", None),
        ("never_inject_sensitive", "never", "SKIP", 1, 999,
         "[]", "", "绝不注入敏感场景（密码、token 等）", None),
        ("session_start_full", "keyword", "FULL", 1, 80,
         json.dumps(["继续上次", "恢复上下文", "聊天记录丢了", "接着做",
                      "继续之前", "上次做到哪了", "回到项目"], ensure_ascii=False),
         "", "会话恢复请求 → 全量注入", None),
        ("project_context_full", "keyword", "FULL", 1, 70,
         json.dumps(["项目", "代码", "bug", "报错", "修复", "部署", "上线",
                      "重构", "review", "测试", "编译", "构建", "pipeline",
                      "ci", "cd", "docker", "k8s", "nginx"], ensure_ascii=False),
         "", "项目/技术关键词 → 全量注入", None),
        ("memory_query_light", "keyword", "LIGHT", 1, 60,
         json.dumps(["之前", "上次", "以前", "记得", "历史", "记录",
                      "搜索", "查一下", "找一下"], ensure_ascii=False),
         "", "记忆查询关键词 → 轻量注入", None),
        ("phone_control_full", "keyword", "FULL", 1, 75,
         json.dumps(["手机", "adb", "android", "scrcpy", "抓包", "reqable",
                      "控制手机", "自动点按"], ensure_ascii=False),
         "", "手机控制关键词 → 全量注入", None),
        ("routine_chat_skip", "pattern", "SKIP", 1, 50,
         "[]", r"^(.{0,5})$|^(ok|好的|嗯|行|收到|了解|thx|thanks|谢谢|辛苦了|辛苦)$",
         "极短回复/确认 → 跳过注入", None),
        ("question_light", "pattern", "LIGHT", 1, 40,
         "[]", r"\?|？|怎么|如何|为什么|什么|哪里|哪个|能不能|可不可以|有没有",
         "疑问句 → 轻量注入", None),
        ("default_light", "always", "LIGHT", 1, 10,
         "[]", "", "默认兜底 → 轻量注入", None),
    ]

    # 检查是否已有数据
    count = conn.execute("SELECT COUNT(*) FROM context_rules").fetchone()[0]
    if count > 0:
        return

    for name, ttype, action, enabled, priority, kw, pat, desc, budget in default_rules:
        conn.execute("""
            INSERT INTO context_rules (name, trigger_type, action, enabled, priority,
                keywords, pattern, description, token_budget, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, ttype, action, enabled, priority, kw, pat, desc, budget, now, now))
    conn.commit()


def _migrate(conn):
    """自动迁移旧库 schema（只执行一次）"""
    now = _now()
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver >= 6:
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
        # 延迟导入避免循环依赖：fts 无外部依赖，可安全导入
        from .fts import sync_observation_fts, sync_session_fts
        try:
            conn.execute("DROP TABLE IF EXISTS sessions_fts")
            conn.execute("DROP TABLE IF EXISTS observations_fts")
        except Exception:
            pass
        conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(session_rowid UNINDEXED, session_id UNINDEXED, summary, summary_short, tags)')
        conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(observation_rowid UNINDEXED, session_id UNINDEXED, obs_type UNINDEXED, content, context, impact)')
        conn.commit()
        for s in conn.execute("SELECT * FROM sessions").fetchall():
            sync_session_fts(conn, s)
        for o in conn.execute("SELECT * FROM observations").fetchall():
            sync_observation_fts(conn, o)
        conn.commit()

    # v3 迁移：添加 summary_compact 列
    sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "summary_compact" not in sess_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN summary_compact TEXT DEFAULT ''")
        conn.commit()

    # v4 迁移：添加 context_rules 表 + 默认规则
    try:
        conn.execute("SELECT COUNT(*) FROM context_rules LIMIT 1")
    except Exception:
        conn.execute("""CREATE TABLE IF NOT EXISTS context_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            trigger_type TEXT NOT NULL,
            action TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 0,
            keywords TEXT DEFAULT '[]',
            pattern TEXT DEFAULT '',
            description TEXT DEFAULT '',
            token_budget INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.commit()
        _seed_default_rules(conn)

    # v5 迁移：给 memory_budget_log 添加 cache_hit 列
    budget_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_budget_log)").fetchall()}
    if "cache_hit" not in budget_cols:
        conn.execute("ALTER TABLE memory_budget_log ADD COLUMN cache_hit INTEGER DEFAULT 0")
        conn.commit()

    # v6 迁移：添加 trigger_log 表（规则命中日志）
    try:
        conn.execute("SELECT COUNT(*) FROM trigger_log LIMIT 1")
    except Exception:
        conn.execute("""CREATE TABLE IF NOT EXISTS trigger_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_hash TEXT NOT NULL,
            message_preview TEXT,
            matched_rule TEXT NOT NULL,
            action TEXT NOT NULL,
            token_budget INTEGER DEFAULT 0,
            match_type TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trigger_log_rule ON trigger_log(matched_rule)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trigger_log_created ON trigger_log(created_at)")
        conn.commit()

    conn.execute("PRAGMA user_version=6")
    conn.commit()
