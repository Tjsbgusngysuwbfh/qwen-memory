# Qwen Memory

A lightweight persistent memory system for AI coding agents. SQLite + FTS5 + TF-IDF semantic search, zero heavy dependencies.

## Features

- **Persistent Memory** — Session summaries, observations, and snapshots survive across sessions
- **Three-layer Search** — FTS5 full-text → LIKE fallback → TF-IDF semantic search
- **Version Control** — Every write creates a version snapshot, rollback supported
- **Fusion Search** — Keyword priority + semantic supplement + ordered dedup
- **Web Viewer** — Browse and search memories at `http://localhost:37777`
- **MCP Server** — 8 tools for other applications to access your memory
- **Schema Migration** — Old databases auto-upgrade on first open
- **Chinese Native** — Character n-gram tokenization, works out of the box

## Quick Start

```bash
pip install -e .

# Insert demo data
python -m qwen_memory.mem init-demo

# Search
python -m qwen_memory.mem search "keywords"
python -m qwen_memory.mem semantic "semantic query"

# Start web viewer
python -m qwen_memory.web_viewer --port 37777
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `add --summary "..." --importance 0.9` | Save session |
| `obs --session "id" --type bugfix --content "..."` | Add observation |
| `end --session "id" --summary "..."` | End session |
| `search "query"` | Keyword search |
| `semantic "query"` | Semantic search |
| `recent` | Recent sessions |
| `detail "session_id"` | Session detail |
| `versions -t session -e "id"` | Version history |
| `rollback -t session -e "id" -s 1` | Rollback |
| `stats` | Statistics |
| `rebuild-index` | Rebuild semantic index |

## MCP Integration

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

### MCP Tools

`mem_search`, `mem_add_session`, `mem_add_obs`, `mem_recent`, `mem_detail`, `mem_stats`, `mem_rollback`, `mem_versions`

## Architecture

```
store.py       — Core storage (SQLite + FTS5 + versioning + migration)
mem.py         — CLI tool
semantic.py    — TF-IDF semantic search
web_viewer.py  — Web viewer (built-in HTTP server)
mcp_server.py  — MCP Server (stdio JSON-RPC)
```

### Design Principles

1. **Single write path** — All writes go through `upsert_session()` / `add_observation()`
2. **Save/end separation** — Save only upserts data, end only writes `ended_at`
3. **Stable FTS keys** — FTS uses rowid, not content, for joins
4. **Transactional rollback** — Main table + FTS + version record commit/rollback together
5. **Fusion search** — Keyword FTS rank priority + semantic supplement + ordered dedup
6. **Semantic staleness detection** — Content signature (count + length + max timestamp)

## Testing

```bash
# Regression tests (23 items)
python tests/regression_test.py

# Stress tests (17 items)
python tests/stress_test.py
```

## License

MIT
