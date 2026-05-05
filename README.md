# Qwen Memory

一个面向 AI 编码代理的轻量持久化记忆系统。  
主打 `SQLite + FTS5 + TF-IDF`，中文开箱即用，依赖尽量克制。

English summary: A lightweight persistent memory system for AI coding agents, built with SQLite, FTS5, and TF-IDF semantic search.

## 功能特性

- **持久化记忆**：会话摘要、观察记录、项目快照可跨会话保存
- **三层检索**：`FTS5` 全文检索 -> `LIKE` 兜底 -> `TF-IDF` 语义搜索
- **版本控制**：每次写入都会生成版本快照，支持回滚
- **融合搜索**：关键词优先、语义补充、有序去重
- **Web 查看器**：可在 `http://localhost:37777` 浏览和搜索历史记忆
- **MCP Server**：提供 8 个 MCP 工具，方便其他应用接入
- **Schema 迁移**：旧数据库首次打开时可自动升级
- **中文原生**：基于字符 `n-gram` 分词，对中文更友好

## 快速开始

```bash
pip install -e .

# 插入演示数据
python -m qwen_memory.mem init-demo

# 搜索
python -m qwen_memory.mem search "关键词"
python -m qwen_memory.mem semantic "语义查询"

# 启动 Web 查看器
python -m qwen_memory.web_viewer --port 37777
```

## CLI 命令

| 命令 | 说明 |
|---------|-------------|
| `add --summary "..." --importance 0.9` | 保存会话 |
| `obs --session "id" --type bugfix --content "..."` | 添加观察 |
| `end --session "id" --summary "..."` | 结束会话 |
| `search "query"` | 关键词搜索 |
| `semantic "query"` | 语义搜索 |
| `recent` | 查看最近会话 |
| `detail "session_id"` | 查看会话详情 |
| `versions -t session -e "id"` | 查看版本历史 |
| `rollback -t session -e "id" -s 1` | 回滚到更早版本 |
| `stats` | 查看统计信息 |
| `rebuild-index` | 重建语义索引 |

## MCP 集成

在 `.claude.json` 或 `settings.json` 中添加：

```json
{
  "mcpServers": {
    "qwen-memory": {
      "command": "python",
      "args": ["-X", "utf8", "/path/to/mcp_server.py"]
    }
  }
}
```

### MCP 工具

`mem_search`, `mem_add_session`, `mem_add_obs`, `mem_recent`, `mem_detail`, `mem_stats`, `mem_rollback`, `mem_versions`

## 项目结构

```text
store.py       — 核心存储层（SQLite + FTS5 + 版本控制 + 迁移）
mem.py         — CLI 工具
semantic.py    — TF-IDF 语义搜索
web_viewer.py  — Web 查看器（内置 HTTP 服务器）
mcp_server.py  — MCP Server（stdio JSON-RPC）
```

### 设计原则

1. **单一写入路径**：所有写操作统一走 `upsert_session()` / `add_observation()`
2. **save / end 分离**：`save` 只负责 upsert 数据，`end` 只负责写 `ended_at`
3. **稳定 FTS 键**：FTS 使用 `rowid` 关联，而不是依赖内容回连
4. **事务化回滚**：主表、FTS、版本记录要么一起成功，要么一起回滚
5. **融合搜索**：优先保留关键词检索排序，再补语义结果
6. **语义过期检测**：基于内容签名（数量 + 长度 + 最大更新时间）自动判断索引是否过期

## 测试

```bash
# 回归测试（23 项）
python tests/regression_test.py

# 压力测试（17 项）
python tests/stress_test.py
```

## License

MIT
