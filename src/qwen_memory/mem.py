"""
Qwen Memory CLI — 命令行接口
用法：
  py mem.py add --session "id" --summary "摘要" [--short "短摘要"] [--importance 0.8]
  py mem.py obs --session "id" --type bugfix --content "修复了XXX"
  py mem.py search "关键词"
  py mem.py search-obs "关键词" [--type bugfix]
  py mem.py recent [--limit 10]
  py mem.py detail "session_id"
  py mem.py timeline "session_id"
  py mem.py stats
  py mem.py init-demo  — 插入示例数据演示
"""
import argparse
import sys
import json
import os
import uuid, os
from datetime import datetime

# 确保能找到 store 模块
try:
    from . import store
    from .semantic import semantic_search, build_index_from_db
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import store
    from semantic import semantic_search, build_index_from_db


def cmd_add(args):
    session_id = args.session or f"sess-{uuid.uuid4().hex[:8]}"
    store.upsert_session(
        session_id=session_id,
        summary=args.summary,
        summary_short=args.short or "",
        project_path=args.project or "",
        model=args.model or "",
        tags=args.tags.split(",") if args.tags else [],
        importance=args.importance,
        token_estimate=args.tokens or 0,
    )
    print(f"OK: 会话已保存 → {session_id}")


def cmd_end(args):
    store.end_session(args.session, summary=args.summary, summary_short=args.short)
    print(f"OK: 会话 {args.session} 已结束")


def cmd_obs(args):
    obs_id = store.add_observation(
        session_id=args.session,
        obs_type=args.type,
        content=args.content,
        importance=args.importance,
        tags=args.tags.split(",") if args.tags else [],
    )
    print(f"OK: 观察已保存 → #{obs_id}")


def cmd_search(args):
    results = store.search_sessions(args.query, limit=args.limit)
    if not results:
        print("没有找到匹配的会话")
        return
    print(f"找到 {len(results)} 个匹配会话：")
    for s in results:
        tags = json.loads(s["tags"]) if s["tags"] else []
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        print(f"  [{s['session_id']}] {s['created_at'][:10]} "
              f"importance={s['importance']}{tag_str}")
        print(f"    {s['summary_short'] or (s['summary'] or '')[:100]}")


def cmd_search_obs(args):
    results = store.search_observations(args.query, limit=args.limit, obs_type=args.type)
    if not results:
        print("没有找到匹配的观察")
        return
    print(f"找到 {len(results)} 条观察：")
    for o in results:
        print(f"  #{o['id']} [{o['obs_type']}] {o['created_at'][:10]} "
              f"importance={o['importance']}")
        print(f"    {o['content'][:150]}")


def cmd_recent(args):
    sessions = store.get_recent_sessions(limit=args.limit)
    if not sessions:
        print("暂无会话记录")
        return
    print(f"最近 {len(sessions)} 个会话：")
    for s in sessions:
        tags = json.loads(s["tags"]) if s["tags"] else []
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        ended = "DONE" if s["ended_at"] else "OPEN"
        print(f"  {ended} [{s['session_id']}] {s['created_at'][:16]} "
              f"importance={s['importance']}{tag_str}")
        if s["summary_short"]:
            print(f"    {s['summary_short'][:100]}")


def cmd_detail(args):
    detail = store.get_session_detail(args.session)
    if not detail:
        print(f"会话 {args.session} 不存在")
        return
    s = detail["session"]
    print(f"=== 会话 {s['session_id']} ===")
    print(f"开始: {s['created_at']}")
    print(f"结束: {s['ended_at'] or '进行中'}")
    print(f"重要度: {s['importance']}")
    print(f"项目: {s['project_path']}")
    print(f"模型: {s['model']}")
    print(f"\n摘要:\n{s['summary']}")
    print(f"\n观察 ({len(detail['observations'])} 条):")
    for o in detail["observations"]:
        print(f"  #{o['id']} [{o['obs_type']}] {o['content'][:120]}")
    print(f"\n快照 ({len(detail['snapshots'])} 条):")
    for s in detail["snapshots"]:
        print(f"  [{s['snapshot_type']}] {s['title']}")


def cmd_timeline(args):
    """时间线（基于详情重建）"""
    detail = store.get_session_detail(args.session)
    if not detail:
        print(f"会话 {args.session} 不存在")
        return
    print(f"=== 时间线: {detail['session']['session_id']} ===")
    print(f"摘要: {detail['session']['summary'][:200]}")
    print(f"\n事件 ({len(detail['observations'])} 条):")
    for o in detail["observations"]:
        print(f"  {o['created_at'][:16]} [{o['obs_type']}] {o['content'][:100]}")


def cmd_stats(args):
    stats = store.get_stats()
    print(f"=== 记忆系统统计 ===")
    print(f"总会话数: {stats['total_sessions']}")
    print(f"总观察数: {stats['total_observations']}")
    print(f"总快照数: {stats['total_snapshots']}")
    print(f"近 7 天会话: {stats['sessions_last_7d']}")
    print(f"平均重要度: {stats['avg_importance']}")
    if stats["observations_by_type"]:
        print(f"观察类型分布:")
        for t, c in stats["observations_by_type"].items():
            print(f"  {t}: {c}")


def cmd_init_demo(args):
    """插入演示数据"""
    import time

    s1 = "demo-desktop-gui"
    store.upsert_session(
        session_id=s1,
        summary="完成了 Windows 桌面 GUI 自动化技能的全流程搭建：调研 4 个方向，安装 pywinauto + pyautogui + Pillow + Tesseract-OCR + AutoHotkey v2，编写 6 个辅助脚本，全部验证通过。",
        summary_short="桌面 GUI 自动化技能搭建完成",
        project_path=os.path.expanduser("~"),
        tags=["desktop", "automation", "gui"],
        importance=0.9,
    )
    store.add_observation(s1, "decision", "选择 A+B 融合方案", importance=0.9)
    store.add_observation(s1, "discovery", "pyautogui 已 21 个月未更新但仍可用", importance=0.6)
    store.add_observation(s1, "bugfix", "AutoHotkey v2 语法与 v1 不同", importance=0.7)
    store.add_observation(s1, "tool_use", "安装 pywinauto/pyautogui/Pillow/pytesseract/Tesseract/AHK", importance=0.5)
    store.end_session(s1, summary="桌面 GUI 自动化技能搭建完成，所有工具验证通过")

    time.sleep(0.1)

    # 演示会话 2
    s2 = "demo-phone-control"
    store.upsert_session(
        session_id=s2,
        summary="完成 Realme RMX3357 手机控制全量补全：自检 15 项已有能力，发现 12 项欠缺，派 3 个 agent 并行找解决方案。安装 scrcpy + YADB + ADBKeyboard。验证：屏幕镜像、中文输入、剪贴板读写全部正常。",
        summary_short="手机控制能力全量补全",
        project_path=os.path.expanduser("~"),
        tags=["android", "adb", "phone", "scrcpy"],
        importance=0.9,
    )
    store.add_observation(s2, "task", "Realme RMX3357 Android 13 Magisk Root", importance=0.8)
    store.add_observation(s2, "bugfix", "ADB 双版本冲突统一为 v37.0.0", importance=0.7)
    store.add_observation(s2, "bugfix", "scrcpy 不在 PATH，复制到 platform-tools", importance=0.7)
    store.add_observation(s2, "tool_use", "scrcpy 3.3.4 YADB 1.1.1 ADBKeyboard 2.5-dev", importance=0.5)
    store.end_session(s2, summary="手机控制全量补全完成")

    time.sleep(0.1)

    # 演示会话 3
    s3 = "demo-stress-test"
    store.upsert_session(
        session_id=s3,
        summary="全站压力测试：测试文件系统权限、Python 库兼容性、ADB 连接、故障注入。发现 4 个软缺陷并修复。极限场景分析：对话式代理的硬边界是无状态/无常驻/无物理执行。",
        summary_short="全站压力测试 + 4 个缺陷修复",
        project_path=os.path.expanduser("~"),
        tags=["testing", "bugfix", "architecture"],
        importance=0.8,
    )
    store.add_observation(s3, "discovery", "对话式代理硬边界：无状态/无常驻/无物理执行", importance=0.9)
    store.add_observation(s3, "bugfix", "YADB 被删后 exit 134 无友好提示", importance=0.6)
    store.add_observation(s3, "task", "全站 10 技能 6 脚本 3 工具 7 MCP 验证通过", importance=0.7)
    store.end_session(s3, summary="压力测试完成")

    print(f"已插入 3 个演示会话和 12 条观察")


def cmd_progressive(args):
    """融合检索"""
    result = store.fused_search(args.query, limit=args.limit if hasattr(args, 'limit') else 10)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_semantic(args):
    """语义搜索（TF-IDF + 余弦相似度）"""
    result = semantic_search(args.query, top_k=args.limit)

    # 从数据库获取详情
    if result["sessions"]:
        print(f"语义匹配会话 ({len(result['sessions'])} 条)：")
        for s in result["sessions"]:
            detail = store.get_session_detail(s["session_id"])
            if detail:
                sess = detail["session"]
                print(f"  [{s['session_id']}] score={s['score']} {sess['created_at'][:10]}")
                print(f"    {sess['summary_short'] or (sess['summary'] or '')[:100]}")

    if result["observations"]:
        print(f"\n语义匹配观察 ({len(result['observations'])} 条)：")
        for o in result["observations"]:
            conn = store.get_db()
            row = conn.execute("SELECT * FROM observations WHERE id=?", (o["observation_id"],)).fetchone()
            conn.close()
            if row:
                print(f"  #{row['id']} [{row['obs_type']}] score={o['score']}")
                print(f"    {row['content'][:120]}")

    if not result["sessions"] and not result["observations"]:
        print("没有找到语义匹配的结果")


def cmd_rebuild_index(args):
    """重建语义索引"""
    idx = build_index_from_db()
    stats = idx.stats()
    print(f"语义索引已重建")
    print(f"  文档数: {stats['total_documents']}")
    print(f"  词汇量: {stats['vocabulary_size']}")
    print(f"  矩阵: {stats['matrix_shape']}")


def cmd_versions(args):
    """查看版本历史"""
    versions = store.get_version_history(args.type, args.entity, limit=args.limit)
    if not versions:
        print("没有版本历史")
        return
    print(f"{args.type}:{args.entity} 的版本历史 ({len(versions)} 条)：")
    for v in versions:
        before_preview = ""
        if v["before_data"]:
            d = json.loads(v["before_data"])
            before_preview = d.get("content", d.get("summary", ""))[:60]
        print(f"  #{v['id']} [{v['action']}] {v['created_at'][:16]}")
        if before_preview:
            print(f"    内容: {before_preview}...")


def cmd_rollback(args):
    """回滚记忆"""
    if args.type == "session":
        ok, msg = store.rollback_session(args.entity, to_version_id=args.version, steps=args.steps)
    elif args.type == "observation":
        ok, msg = store.rollback_observation(int(args.entity), to_version_id=args.version, steps=args.steps)
    else:
        print(f"不支持的类型: {args.type}")
        return
    print(f"{'OK' if ok else 'FAIL'}: {msg}")


def cmd_all_versions(args):
    """查看所有版本变更"""
    versions = store.get_all_versions(limit=args.limit)
    if not versions:
        print("暂无版本记录")
        return
    print(f"最近 {len(versions)} 条版本变更：")
    for v in versions:
        print(f"  #{v['id']} {v['entity_type']}:{v['entity_id']} [{v['action']}] {v['created_at'][:16]}")


def main():
    parser = argparse.ArgumentParser(description="Qwen Memory CLI")
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="保存会话")
    p_add.add_argument("--session", "-s")
    p_add.add_argument("--summary", required=True)
    p_add.add_argument("--short", help="短摘要")
    p_add.add_argument("--project", "-p")
    p_add.add_argument("--model", "-m")
    p_add.add_argument("--tags", "-t")
    p_add.add_argument("--importance", "-i", type=float, default=0.5)
    p_add.add_argument("--tokens", type=int)

    # end
    p_end = sub.add_parser("end", help="结束会话")
    p_end.add_argument("--session", "-s", required=True)
    p_end.add_argument("--summary")
    p_end.add_argument("--short")

    # obs
    p_obs = sub.add_parser("obs", help="添加观察")
    p_obs.add_argument("--session", "-s", required=True)
    p_obs.add_argument("--type", required=True,
                       choices=["decision", "bugfix", "discovery", "task", "error", "tool_use", "note"])
    p_obs.add_argument("--content", "-c", required=True)
    p_obs.add_argument("--importance", "-i", type=float, default=0.5)
    p_obs.add_argument("--tags", "-t")

    # search
    p_search = sub.add_parser("search", help="搜索会话")
    p_search.add_argument("query")
    p_search.add_argument("--limit", "-l", type=int, default=10)

    # search-obs
    p_sobs = sub.add_parser("search-obs", help="搜索观察")
    p_sobs.add_argument("query")
    p_sobs.add_argument("--type", choices=["decision", "bugfix", "discovery", "task", "error", "tool_use", "note"])
    p_sobs.add_argument("--limit", "-l", type=int, default=20)

    # recent
    p_recent = sub.add_parser("recent", help="最近会话")
    p_recent.add_argument("--limit", "-l", type=int, default=10)

    # detail
    p_detail = sub.add_parser("detail", help="会话详情")
    p_detail.add_argument("session")

    # timeline
    p_tl = sub.add_parser("timeline", help="时间线")
    p_tl.add_argument("session")

    # stats
    sub.add_parser("stats", help="统计信息")

    # progressive
    p_prog = sub.add_parser("progressive", help="渐进式检索")
    p_prog.add_argument("query")
    p_prog.add_argument("--tokens", type=int, default=500)

    # semantic
    p_sem = sub.add_parser("semantic", help="语义搜索（TF-IDF）")
    p_sem.add_argument("query")
    p_sem.add_argument("--limit", "-l", type=int, default=10)

    # rebuild-index
    sub.add_parser("rebuild-index", help="重建语义索引")

    # versions
    p_ver = sub.add_parser("versions", help="查看版本历史")
    p_ver.add_argument("--type", "-t", required=True, choices=["session", "observation"])
    p_ver.add_argument("--entity", "-e", required=True, help="实体 ID")
    p_ver.add_argument("--limit", "-l", type=int, default=10)

    # rollback
    p_rb = sub.add_parser("rollback", help="回滚记忆")
    p_rb.add_argument("--type", "-t", required=True, choices=["session", "observation"])
    p_rb.add_argument("--entity", "-e", required=True, help="实体 ID")
    p_rb.add_argument("--steps", "-s", type=int, default=1, help="回滚步数")
    p_rb.add_argument("--version", "-v", type=int, help="指定版本 ID")

    # all-versions
    p_av = sub.add_parser("all-versions", help="查看所有版本变更")
    p_av.add_argument("--limit", "-l", type=int, default=20)

    # init-demo
    sub.add_parser("init-demo", help="插入演示数据")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    cmds = {
        "add": cmd_add, "end": cmd_end, "obs": cmd_obs,
        "search": cmd_search, "search-obs": cmd_search_obs,
        "recent": cmd_recent, "detail": cmd_detail, "timeline": cmd_timeline,
        "stats": cmd_stats, "progressive": cmd_progressive,
        "semantic": cmd_semantic, "rebuild-index": cmd_rebuild_index,
        "versions": cmd_versions, "rollback": cmd_rollback, "all-versions": cmd_all_versions,
        "init-demo": cmd_init_demo,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
