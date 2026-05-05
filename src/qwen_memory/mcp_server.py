"""
Qwen Memory MCP Server — 让其他工具通过 MCP 协议访问记忆系统
遵循 MCP (Model Context Protocol) 规范，通过 stdio 通信

启动方式：
  py -X utf8 mcp_server.py
  
MCP 客户端配置（settings.json 或 settings.json）：
  {
    "mcpServers": {
      "qwen-memory": {
        "command": "py",
        "args": ["-X", "utf8", "/path/to/mcp_server.py"]
      }
    }
  }

提供的 MCP Tools：
  - mem_search: 搜索记忆（关键词 + 语义）
  - mem_add_session: 保存会话摘要
  - mem_add_obs: 添加观察记录
  - mem_recent: 获取最近会话
  - mem_detail: 获取会话详情
  - mem_stats: 获取统计信息
  - mem_rollback: 回滚记忆到指定版本
  - mem_versions: 查看版本历史
"""
import sys
import os
import json

try:
    from . import store
    from .semantic import semantic_search
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import store
    from semantic import semantic_search

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
    }
]


def handle_tool_call(tool_name, arguments):
    """处理 MCP 工具调用"""
    try:
        if tool_name == "mem_search":
            query = arguments["query"]
            mode = arguments.get("mode", "both")
            limit = arguments.get("limit", 10)

            if mode == "both":
                results = store.fused_search(query, limit=limit)
            elif mode == "keyword":
                results = {
                    "sessions": store.search_sessions(query, limit=limit),
                    "observations": store.search_observations(query, limit=limit),
                }
            elif mode == "semantic":
                try:
                    results = semantic_search(query, top_k=limit)
                except Exception as e:
                    results = {"error": str(e)}
            else:
                results = {"sessions": [], "observations": []}

            return json.dumps(results, ensure_ascii=False, default=str)

        elif tool_name == "mem_add_session":
            sid = arguments["session_id"]
            store.upsert_session(
                session_id=sid,
                summary=arguments["summary"],
                summary_short=arguments.get("summary_short", ""),
                importance=arguments.get("importance", 0.5),
                tags=arguments.get("tags", "").split(",") if arguments.get("tags") else [],
            )
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
                        "version": "1.0.0"
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
