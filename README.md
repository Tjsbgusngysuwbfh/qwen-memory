# Qwen Memory

一个面向 AI 编码代理的轻量持久化记忆系统，围绕个人工作流设计，主打单机可控、依赖克制、中文友好。

English summary: Lightweight persistent memory for AI coding agents with SQLite, FTS5, TF-IDF semantic search, MCP tools, and Chinese-first defaults.

## 特性

- 持久化记忆：保存会话摘要、观察记录和项目快照
- 三层检索：FTS5 全文检索、LIKE 兜底、TF-IDF 语义补充
- 版本恢复：写入自动留痕，支持内容层恢复与回滚
- 触发路由：按规则决定是否注入历史上下文，并记录命中日志
- 预算日志：记录 token 预算、注入结果和 `cache_hit`
- MCP 集成：通过 stdio 暴露记忆工具，方便接入代理系统
- Web 查看器：本地浏览最近会话、详情和搜索结果

## 架构

当前版本已经从早期单体 `store.py` 演进为按职责拆分的结构：

```text
src/qwen_memory/
  db.py             # 数据库连接、时间工具、路径配置
  migrations.py     # Schema 初始化与自动迁移
  fts.py            # FTS 索引同步与重建
  repository.py     # 会话、观察、快照 CRUD
  services.py       # 搜索、版本恢复、预算日志、触发日志
  trigger_router.py # 触发规则路由
  budget.py         # 预算裁剪与压缩逻辑
  semantic.py       # TF-IDF 语义索引
  store.py          # 兼容门面
  mem.py            # CLI
  mcp_server.py     # MCP stdio server
  web_viewer.py     # 本地查看器
```

## 安装

```bash
pip install -e .
```

开发依赖：

```bash
pip install -e .[dev]
```

## CLI

安装后可直接使用：

```bash
qwen-mem stats
qwen-mem search "关键词"
qwen-mem semantic "语义查询"
qwen-mem recent --limit 10
qwen-mem budgeted-search "查询" --budget 500
```

也可以用模块方式运行：

```bash
python -m qwen_memory.mem stats
python -m qwen_memory.mem init-demo
```

常用命令：

- `add --summary "..." --importance 0.9`
- `obs --session "id" --type bugfix --content "..."`
- `end --session "id" --summary "..."`
- `search "query"`
- `search-obs "query" --type bugfix`
- `semantic "query"`
- `recent --limit 10`
- `detail "session_id"`
- `timeline "session_id"`
- `versions -t session -e "id"`
- `rollback -t session -e "id" -s 1`
- `trigger "用户消息"`
- `budgeted-search "query" --budget 500`
- `weekly-report`

## MCP 集成

本地启动：

```bash
python -m qwen_memory.mcp_server
```

示例配置：

```json
{
  "mcpServers": {
    "qwen-memory": {
      "command": "python",
      "args": ["-m", "qwen_memory.mcp_server"]
    }
  }
}
```

当前提供的核心工具包括：

- `mem_context`
- `mem_search`
- `mem_add_session`
- `mem_add_obs`
- `mem_recent`
- `mem_detail`
- `mem_stats`
- `mem_rollback`
- `mem_versions`
- `mem_budget_log`
- `mem_weekly_report`

## Web 查看器

```bash
python -m qwen_memory.web_viewer --port 37777
```

打开 [http://localhost:37777](http://localhost:37777)

## 数据库与迁移

- 默认数据库位置：`src/qwen_memory/data/memories.db`
- 测试中可通过 `store.set_db_path(...)` 覆盖数据库路径
- 打开数据库时会自动执行 schema 初始化与迁移
- 当前迁移已覆盖 `summary_compact`、`context_rules`、`memory_budget_log.cache_hit`、`trigger_log`

## 测试

脚本式回归：

```bash
python tests/test_regression.py
python tests/test_stress.py
python tests/test_budget.py
python tests/test_trigger.py
python tests/test_rollback.py
python tests/test_experience.py
```

## 发布注意事项

- 本项目优先服务个人工作流，不承诺重型分布式能力
- 不应提交真实数据库、缓存索引、临时日志或个人敏感记忆
- 发布前请确认 README 命令、安装入口、测试脚本与 GitHub 首页描述一致

## License

MIT
