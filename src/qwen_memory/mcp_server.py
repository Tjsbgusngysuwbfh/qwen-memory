"""
Qwen Memory MCP Server — 让其他工具通过 MCP 协议访问记忆系统
遵循 MCP (Model Context Protocol) 规范，通过 stdio 通信

启动方式：
  py -X utf8 -m qwen_memory.mcp_server

MCP 客户端配置（.claude.json 或 settings.json）：
  {
    "mcpServers": {
      "qwen-memory": {
        "command": "py",
        "args": ["-X", "utf8", "-m", "qwen_memory.mcp_server"]
      }
    }
  }

提供的 MCP Tools：
  - mem_context: 一站式上下文获取（触发判断 → 检索 → 格式化 → 返回）
  - mem_search: 搜索记忆（关键词 + 语义，集成触发路由器 + 缓存）
  - mem_add_session: 保存会话摘要（自动压缩生成短摘要）
  - mem_add_obs: 添加观察记录
  - mem_recent: 获取最近会话
  - mem_detail: 获取会话详情
  - mem_stats: 获取统计信息
  - mem_rollback: 回滚记忆到指定版本
  - mem_versions: 查看版本历史
  - mem_budget_log: 查看成本日志（token 消耗、缓存命中）
  - mem_weekly_report: 获取周报统计（触发分布、平均 token、缓存命中率）

可选依赖（由其他模块并行提供，不存在时自动降级）：
  - trigger_router: 触发路由器（classify, compress_summary）
  - budget: 预算日志（log_injection, get_logs, weekly_report）
"""
import sys
import os
import json
import time
import hashlib
import textwrap

if __package__:
    from . import __version__, budget, store, trigger_router
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from __init__ import __version__
    import store

# --- 可选依赖：由其他模块并行提供，不存在时优雅降级 ---
    try:
        import trigger_router
    except ImportError:
        trigger_router = None

    try:
        import budget
    except ImportError:
        budget = None


# ============ 内存缓存（带 TTL） ============

class _Cache:
    """简易内存缓存，按 key 存储，超时自动失效"""

    def __init__(self, default_ttl=300):
        self._store = {}
        self._default_ttl = default_ttl

    def get(self, key):
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.time() > entry["expire_at"]:
            del self._store[key]
            return None
        return entry["value"]

    def set(self, key, value, ttl=None):
        self._store[key] = {
            "value": value,
            "expire_at": time.time() + (ttl or self._default_ttl),
        }

    def clear(self):
        self._store.clear()


_cache = _Cache()


def _cache_key(*parts):
    """根据参数生成缓存 key"""
    raw = "|".join(str(p) for p in parts)
    return hashlib.md5(raw.encode()).hexdigest()


# ============ 辅助：token 估算 ============

def _estimate_tokens(text):
    """粗略估算 token 数：英文 ~4 字符/token，中文 ~1.5 字符/token"""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    cjk_chars = len(text) - ascii_chars
    return int(ascii_chars / 4 + cjk_chars / 1.5)

# MCP 协议版本
MCP_PROTOCOL_VERSION = "2024-11-05"

# 工具定义
TOOLS = [
    {
        "name": "mem_search",
        "description": "搜索 Qwen Memory 记忆库。支持关键词搜索和语义搜索。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "mode": {"type": "string", "enum": ["keyword", "semantic", "both"],
                         "default": "both", "description": "搜索模式"},
                "limit": {"type": "integer", "default": 10, "description": "返回结果数量"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "mem_add_session",
        "description": "保存一条会话摘要到记忆库",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话 ID"},
                "summary": {"type": "string", "description": "会话摘要"},
                "summary_short": {"type": "string", "description": "短摘要"},
                "importance": {"type": "number", "default": 0.5, "description": "重要度 0-1"},
                "tags": {"type": "string", "description": "逗号分隔的标签"}
            },
            "required": ["session_id", "summary"]
        }
    },
    {
        "name": "mem_add_obs",
        "description": "添加一条观察记录（决策/修复/发现/任务/错误/工具使用/笔记）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "所属会话 ID"},
                "obs_type": {"type": "string",
                             "enum": ["decision", "bugfix", "discovery", "task", "error", "tool_use", "note"],
                             "description": "观察类型"},
                "content": {"type": "string", "description": "观察内容"},
                "importance": {"type": "number", "default": 0.5, "description": "重要度 0-1"}
            },
            "required": ["session_id", "obs_type", "content"]
        }
    },
    {
        "name": "mem_recent",
        "description": "获取最近的会话列表",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "description": "返回数量"}
            }
        }
    },
    {
        "name": "mem_detail",
        "description": "获取某个会话的完整详情（含所有观察和快照）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话 ID"}
            },
            "required": ["session_id"]
        }
    },
    {
        "name": "mem_stats",
        "description": "获取记忆库统计信息",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "mem_rollback",
        "description": "回滚记忆到之前的版本",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "enum": ["session", "observation"],
                                "description": "实体类型"},
                "entity_id": {"type": "string", "description": "实体 ID（session_id 或 observation_id）"},
                "steps": {"type": "integer", "default": 1, "description": "回滚步数"},
                "version_id": {"type": "integer", "description": "指定回滚到的版本 ID（可选）"}
            },
            "required": ["entity_type", "entity_id"]
        }
    },
    {
        "name": "mem_versions",
        "description": "查看某个记忆的版本历史",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string", "enum": ["session", "observation"]},
                "entity_id": {"type": "string", "description": "实体 ID"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["entity_type", "entity_id"]
        }
    },
    # ---- 新增工具 ----
    {
        "name": "mem_context",
        "description": "一站式上下文获取：判断触发原因 → 检索记忆 → 格式化返回注入文本。新会话开始时调用，自动决定是否需要注入历史上下文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "first_message": {"type": "string", "description": "用户首条消息（用于判断触发原因和检索关键词）"},
                "session_id": {"type": "string", "description": "当前会话 ID（可选，用于记录到预算日志）"}
            },
            "required": ["first_message"]
        }
    },
    {
        "name": "mem_budget_log",
        "description": "查看记忆系统的成本日志：注入历史、token 消耗、缓存命中率等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "筛选指定会话的日志（可选）"},
                "limit": {"type": "integer", "default": 20, "description": "返回记录数"}
            }
        }
    },
    {
        "name": "mem_weekly_report",
        "description": "获取记忆系统的周报统计：触发分布、平均 token、缓存命中率等。",
        "inputSchema": {"type": "object", "properties": {}}
    }
]


def _format_context_results(trigger_reason, results, query=""):
    """将检索结果格式化为注入文本"""
    sessions = results.get("sessions", [])
    observations = results.get("observations", [])
    if not sessions and not observations:
        return None

    lines = ["--- Qwen Memory 上下文 ---"]
    lines.append(f"触发原因: {trigger_reason}")
    if query:
        lines.append(f"检索关键词: {query}")
    lines.append("")

    if sessions:
        lines.append("## 相关会话")
        for s in sessions[:5]:
            # 优先使用 summary_compact（LIGHT 模式），回退到 summary（FULL 模式）
            compact = (s.get("summary_compact") or "").strip()
            short = s.get("summary_short") or ""
            summary = s.get("summary") or ""
            if compact:
                display = compact
            elif short:
                display = short
            else:
                display = summary[:120] + "..." if len(summary) > 120 else summary
            tags = s.get("tags", "")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            lines.append(f"- [{s.get('session_id', '?')}] {display}{tag_str}")
        lines.append("")

    if observations:
        lines.append("## 相关观察")
        type_labels = {
            "decision": "决策", "bugfix": "修复", "discovery": "发现",
            "task": "任务", "error": "错误", "tool_use": "工具", "note": "笔记",
        }
        for o in observations[:5]:
            label = type_labels.get(o.get("obs_type", ""), o.get("obs_type", ""))
            content = o.get("content", "")
            display = content[:150] + "..." if len(content) > 150 else content
            lines.append(f"- [{label}] {display}")
        lines.append("")

    lines.append("--- 上下文结束 ---")
    return "\n".join(lines)


def _log_budget(session_id, trigger_reason, tokens_injected, results_count, cache_hit):
    """记录一次注入的成本日志"""
    if budget is not None:
        try:
            budget.log_injection(
                session_id=session_id or "",
                trigger_reason=trigger_reason,
                tokens_injected=tokens_injected,
                results_count=results_count,
                cache_hit=cache_hit,
            )
            return
        except Exception:
            pass
    # budget 模块不可用时，写入本地 JSONL 文件兜底
    _log_budget_local(session_id, trigger_reason, tokens_injected, results_count, cache_hit)


def _log_budget_local(session_id, trigger_reason, tokens_injected, results_count, cache_hit):
    """本地 JSONL 兜底记录"""
    from pathlib import Path
    log_path = Path(__file__).parent / "data" / "budget_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "session_id": session_id or "",
        "trigger_reason": trigger_reason,
        "tokens_injected": tokens_injected,
        "results_count": results_count,
        "cache_hit": cache_hit,
        "created_at": store._now(),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def handle_tool_call(tool_name, arguments):
    """处理 MCP 工具调用"""
    try:
        if tool_name == "mem_search":
            query = arguments["query"]
            mode = arguments.get("mode", "both")
            limit = arguments.get("limit", 10)

            # 触发路由器：优化查询词（如存在）
            if trigger_router is not None:
                try:
                    route_info = trigger_router.classify(query)
                    if isinstance(route_info, dict):
                        refined_query = route_info.get("refined_query", query)
                        if refined_query:
                            query = refined_query
                except Exception:
                    pass  # 触发路由器失败不影响正常搜索

            # 缓存查找
            ck = _cache_key("search", query, mode, limit)
            cached = _cache.get(ck)
            if cached is not None:
                return json.dumps(cached, ensure_ascii=False, default=str)

            if mode == "both":
                results = store.fused_search(query, limit=limit)
            elif mode == "keyword":
                results = {
                    "sessions": store.search_sessions(query, limit=limit),
                    "observations": store.search_observations(query, limit=limit),
                }
            elif mode == "semantic":
                try:
                    from semantic import semantic_search
                    results = semantic_search(query, top_k=limit)
                except Exception as e:
                    results = {"error": str(e)}
            else:
                results = {"sessions": [], "observations": []}

            # 写入缓存
            _cache.set(ck, results)
            return json.dumps(results, ensure_ascii=False, default=str)

        elif tool_name == "mem_add_session":
            sid = arguments["session_id"]
            summary = arguments["summary"]
            summary_short = arguments.get("summary_short", "")

            # 自动压缩摘要：如果短摘要为空，自动生成
            if not summary_short and summary:
                if trigger_router is not None:
                    try:
                        summary_short = trigger_router.compress_summary(summary)
                    except Exception:
                        summary_short = None
                # 兜底：简单截断
                if not summary_short:
                    summary_short = summary[:80] + ("..." if len(summary) > 80 else "")

            store.upsert_session(
                session_id=sid,
                summary=summary,
                summary_short=summary_short,
                importance=arguments.get("importance", 0.5),
                tags=arguments.get("tags", "").split(",") if arguments.get("tags") else [],
            )
            # 清除相关缓存（新会话写入后，搜索结果可能变化）
            _cache.clear()
            return json.dumps({"ok": True, "session_id": sid})

        elif tool_name == "mem_add_obs":
            obs_id = store.add_observation(
                session_id=arguments["session_id"],
                obs_type=arguments["obs_type"],
                content=arguments["content"],
                importance=arguments.get("importance", 0.5),
            )
            return json.dumps({"ok": True, "observation_id": obs_id})

        elif tool_name == "mem_recent":
            limit = arguments.get("limit", 10)
            sessions = store.get_recent_sessions(limit)
            return json.dumps(sessions, ensure_ascii=False, default=str)

        elif tool_name == "mem_detail":
            detail = store.get_session_detail(arguments["session_id"])
            return json.dumps(detail or {"error": "not found"}, ensure_ascii=False, default=str)

        elif tool_name == "mem_stats":
            return json.dumps(store.get_stats(), ensure_ascii=False, default=str)

        elif tool_name == "mem_rollback":
            entity_type = arguments["entity_type"]
            entity_id = arguments["entity_id"]
            steps = arguments.get("steps", 1)
            version_id = arguments.get("version_id")

            if entity_type == "session":
                ok, msg = store.rollback_session(entity_id, to_version_id=version_id, steps=steps)
            elif entity_type == "observation":
                ok, msg = store.rollback_observation(int(entity_id), to_version_id=version_id, steps=steps)
            else:
                return json.dumps({"error": f"Unknown entity_type: {entity_type}"})

            return json.dumps({"ok": ok, "message": msg})

        elif tool_name == "mem_versions":
            entity_type = arguments["entity_type"]
            entity_id = arguments["entity_id"]
            limit = arguments.get("limit", 10)
            versions = store.get_version_history(entity_type, entity_id, limit)
            return json.dumps(versions, ensure_ascii=False, default=str)

        elif tool_name == "mem_context":
            first_message = arguments["first_message"]
            session_id = arguments.get("session_id", "")

            # 1. 判断触发原因
            trigger_reason = "session_start"
            query = first_message
            if trigger_router is not None:
                try:
                    route_info = trigger_router.classify(first_message)
                    if isinstance(route_info, dict):
                        trigger_reason = route_info.get("reason", "session_start")
                        query = route_info.get("refined_query", first_message)
                except Exception:
                    pass

            # 2. 缓存查找
            ck = _cache_key("context", query)
            cached = _cache.get(ck)
            cache_hit = cached is not None

            if cache_hit:
                # 缓存命中：直接使用格式化结果
                formatted_text = cached["formatted_text"]
                token_count = cached["token_count"]
                results_count = cached["results_count"]
            else:
                # 3. 检索记忆
                results = store.fused_search(query, limit=10)
                sessions = results.get("sessions", [])
                observations = results.get("observations", [])
                results_count = len(sessions) + len(observations)

                # 4. 格式化
                formatted_text = _format_context_results(trigger_reason, results, query)
                if formatted_text is None:
                    formatted_text = "(暂无相关记忆)"

                token_count = _estimate_tokens(formatted_text)

                # 写入缓存
                _cache.set(ck, {
                    "formatted_text": formatted_text,
                    "token_count": token_count,
                    "results_count": results_count,
                })

            # 5. 记录预算
            _log_budget(session_id, trigger_reason, token_count, results_count, cache_hit)

            # 6. 记录触发路由命中日志
            if trigger_router is not None:
                try:
                    trigger_result = trigger_router.evaluate_trigger(first_message)
                    store.log_trigger(first_message, trigger_result)
                except Exception:
                    pass  # 日志失败不阻塞主流程

            return json.dumps({
                "formatted_text": formatted_text,
                "trigger_reason": trigger_reason,
                "token_count": token_count,
                "results_count": results_count,
                "cache_hit": cache_hit,
            }, ensure_ascii=False)

        elif tool_name == "mem_budget_log":
            session_id = arguments.get("session_id")
            limit = arguments.get("limit", 20)

            # 优先用 budget 模块
            if budget is not None:
                try:
                    logs = budget.get_logs(session_id=session_id, limit=limit)
                    return json.dumps(logs, ensure_ascii=False, default=str)
                except Exception:
                    pass

            # 兜底：读本地 JSONL
            from pathlib import Path
            log_path = Path(__file__).parent / "data" / "budget_log.jsonl"
            if not log_path.exists():
                return json.dumps({"logs": [], "source": "local_jsonl"})

            entries = []
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if session_id and entry.get("session_id") != session_id:
                            continue
                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue

            # 按时间倒序，取 limit 条
            entries.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return json.dumps({"logs": entries[:limit], "source": "local_jsonl"},
                              ensure_ascii=False, default=str)

        elif tool_name == "mem_weekly_report":
            # 优先用 budget 模块
            if budget is not None:
                try:
                    report = budget.weekly_report()
                    return json.dumps(report, ensure_ascii=False, default=str)
                except Exception:
                    pass

            # 兜底：从本地 JSONL 生成基础统计
            from pathlib import Path
            from collections import Counter
            from datetime import datetime, timedelta, timezone

            log_path = Path(__file__).parent / "data" / "budget_log.jsonl"
            if not log_path.exists():
                return json.dumps({
                    "trigger_distribution": {},
                    "avg_tokens": 0,
                    "cache_hit_rate": 0,
                    "total_injections": 0,
                    "period": "last_7_days",
                    "source": "local_jsonl",
                })

            # 读取最近 7 天的日志
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            entries = []
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        created = entry.get("created_at", "")
                        if created:
                            try:
                                dt = datetime.fromisoformat(created)
                                if dt < cutoff:
                                    continue
                            except Exception:
                                pass
                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue

            if not entries:
                return json.dumps({
                    "trigger_distribution": {},
                    "avg_tokens": 0,
                    "cache_hit_rate": 0,
                    "total_injections": 0,
                    "period": "last_7_days",
                    "source": "local_jsonl",
                })

            trigger_counts = Counter(e.get("trigger_reason", "unknown") for e in entries)
            total_tokens = sum(e.get("tokens_injected", 0) for e in entries)
            cache_hits = sum(1 for e in entries if e.get("cache_hit"))

            return json.dumps({
                "trigger_distribution": dict(trigger_counts),
                "avg_tokens": round(total_tokens / len(entries), 1),
                "cache_hit_rate": round(cache_hits / len(entries) * 100, 1),
                "total_injections": len(entries),
                "period": "last_7_days",
                "source": "local_jsonl",
            }, ensure_ascii=False)

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ============ MCP stdio 协议处理 ============

def read_message():
    """从 stdin 读取一条 MCP 消息"""
    header_line = sys.stdin.readline()
    if not header_line:
        return None

    # 解析 Content-Length
    content_length = 0
    while True:
        line = header_line.strip()
        if line == "":
            break
        if line.startswith("Content-Length:"):
            content_length = int(line.split(":")[1].strip())
        header_line = sys.stdin.readline()
        if not header_line:
            return None

    if content_length == 0:
        return None

    body = sys.stdin.read(content_length)
    return json.loads(body)


def write_message(msg):
    """向 stdout 写入一条 MCP 消息"""
    body = json.dumps(msg, ensure_ascii=False)
    content = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n{body}"
    sys.stdout.write(content)
    sys.stdout.flush()


def send_response(req_id, result):
    write_message({"jsonrpc": "2.0", "id": req_id, "result": result})


def send_error(req_id, code, message):
    write_message({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": code, "message": message}})


def main():
    while True:
        try:
            msg = read_message()
            if msg is None:
                break

            method = msg.get("method", "")
            req_id = msg.get("id")
            params = msg.get("params", {})

            if method == "initialize":
                send_response(req_id, {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "qwen-memory",
                        "version": __version__
                    }
                })

            elif method == "notifications/initialized":
                pass  # 客户端确认，无需回复

            elif method == "tools/list":
                send_response(req_id, {"tools": TOOLS})

            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                result_text = handle_tool_call(tool_name, arguments)
                send_response(req_id, {
                    "content": [{"type": "text", "text": result_text}]
                })

            elif method == "ping":
                send_response(req_id, {})

            elif method.startswith("notifications/"):
                pass  # 通知不需要回复

            else:
                if req_id is not None:
                    send_error(req_id, -32601, f"Method not found: {method}")

        except json.JSONDecodeError:
            continue
        except Exception as e:
            if req_id is not None:
                send_error(req_id, -32603, str(e))
            else:
                break


if __name__ == "__main__":
    main()
