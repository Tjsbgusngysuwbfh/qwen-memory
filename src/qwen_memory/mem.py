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
  py mem.py trigger "用户消息"
  py mem.py budgeted-search "关键词" [--budget 500]
  py mem.py cleanup [--keep-days 90]
  py mem.py weekly-report
  py mem.py init-demo  — 插入示例数据演示
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta

# 确保能找到 store 模块
if __package__:
    from . import store
    from .db import days_ago
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import store
    from db import days_ago


# ============ 辅助统计函数 ============

def _get_cache_stats(conn, since=None):
    """获取缓存命中统计（从 memory_budget_log 表查询）"""
    try:
        if since:
            total = conn.execute(
                "SELECT COUNT(*) FROM memory_budget_log WHERE created_at > ?", (since,)
            ).fetchone()[0]
            hits = conn.execute(
                "SELECT COUNT(*) FROM memory_budget_log WHERE cache_hit=1 AND created_at > ?",
                (since,)
            ).fetchone()[0]
        else:
            total = conn.execute("SELECT COUNT(*) FROM memory_budget_log").fetchone()[0]
            hits = conn.execute("SELECT COUNT(*) FROM memory_budget_log WHERE cache_hit=1").fetchone()[0]
        return {"total_queries": total, "cache_hits": hits,
                "hit_rate": hits / total if total > 0 else 0.0}
    except Exception:
        # memory_budget_log 表尚不存在，返回零值
        return {"total_queries": 0, "cache_hits": 0, "hit_rate": 0.0}


def _get_injection_stats(conn, since=None):
    """获取注入 token 统计（从 sessions.token_estimate 聚合）"""
    try:
        if since:
            row = conn.execute(
                "SELECT COALESCE(SUM(token_estimate),0), "
                "COALESCE(AVG(token_estimate),0), "
                "COUNT(*) FROM sessions WHERE token_estimate > 0 AND created_at > ?",
                (since,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(token_estimate),0), "
                "COALESCE(AVG(token_estimate),0), "
                "COUNT(*) FROM sessions WHERE token_estimate > 0"
            ).fetchone()
        return {"total_injected": row[0], "avg_injected": round(row[1], 1),
                "sessions_with_tokens": row[2]}
    except Exception:
        return {"total_injected": 0, "avg_injected": 0, "sessions_with_tokens": 0}


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
        ended = "[done]" if s["ended_at"] else "[active]"
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

    # 新增：注入 token 统计
    conn = store.get_db()
    try:
        inj = _get_injection_stats(conn)
        cache = _get_cache_stats(conn)

        print(f"\n--- 注入 Token 统计 ---")
        print(f"总注入 token: {inj['total_injected']}")
        print(f"平均注入 token: {inj['avg_injected']}")
        print(f"有 token 记录的会话: {inj['sessions_with_tokens']}")

        print(f"\n--- 缓存统计 ---")
        print(f"总查询: {cache['total_queries']}")
        print(f"缓存命中: {cache['cache_hits']}")
        print(f"命中率: {cache['hit_rate']:.1%}")
    finally:
        conn.close()


def cmd_init_demo(args):
    """插入演示数据"""
    import time

    s1 = "demo-desktop-gui"
    store.upsert_session(
        session_id=s1,
        summary="完成了 Windows 桌面 GUI 自动化技能的全流程搭建：调研 4 个方向，安装 pywinauto + pyautogui + Pillow + Tesseract-OCR + AutoHotkey v2，编写 6 个辅助脚本，全部验证通过。",
        summary_short="桌面 GUI 自动化技能搭建完成",
        project_path="~/projects/demo-desktop-gui",
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
        project_path="~/projects/demo-phone-control",
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
        project_path="~/projects/demo-stress-test",
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
    from semantic import semantic_search
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
    from semantic import build_index_from_db
    idx = build_index_from_db()
    stats = idx.stats()
    print(f"语义索引已重建")
    print(f"  文档数: {stats['total_documents']}")
    print(f"  词汇量: {stats['vocabulary_size']}")
    print(f"  矩阵: {stats['matrix_shape']}")


# ============ 新增命令 ============


def cmd_trigger(args):
    """测试触发路由器，返回 action、token_budget 及详细解释"""
    try:
        from trigger_router import evaluate_trigger
        result = evaluate_trigger(args.message)
        action = result.action
        token_budget = result.token_budget
        rule = result.matched_rule or "default"
        desc = result.rule_description or ""
        rule_label = f"{rule} ({desc})" if desc else rule

        print(f"action={action}")
        print(f"token_budget={token_budget}")
        print(f"rule={rule_label}")
        if result.matched_keywords:
            print(f"matched_keywords: {result.matched_keywords}")
        if result.matched_type:
            print(f"match_type={result.matched_type}")
        print(f"reason: {result.reason}")

        # 记录触发路由命中日志
        try:
            store.log_trigger(args.message, result)
        except Exception:
            pass  # 日志失败不阻塞主流程
    except ImportError:
        print("ERROR: trigger_router 模块未安装", file=sys.stderr)
        print("请先实现 trigger_router.py（提供 evaluate_trigger() 函数）", file=sys.stderr)
        sys.exit(1)


def cmd_budgeted_search(args):
    """带 token 预算的搜索，返回裁剪后的结果 + token 消耗统计"""
    budget = args.budget
    # 粗估：1 token ≈ 3 字符（中英混合取中间值）
    char_budget = budget * 3

    results = store.fused_search(args.query, limit=args.limit)

    total_tokens = 0
    included_sessions = 0
    included_obs = 0

    def estimate_tokens(text):
        """粗估 token 数：中文 ~1.5 字/token，英文 ~4 字符/token，取 ~3"""
        if not text:
            return 0
        return max(1, len(text) // 3)

    print(f"=== 预算搜索 (budget={budget} tokens) ===")
    print(f"查询: {args.query}")

    if results["sessions"]:
        print(f"\n会话 ({len(results['sessions'])} 条):")
        for s in results["sessions"]:
            text = s.get("summary_short") or (s.get("summary") or "")[:200]
            t = estimate_tokens(text)
            if total_tokens + t > char_budget:
                # 裁剪到剩余预算
                remaining = max(0, char_budget - total_tokens)
                text = text[:remaining * 3]
                t = estimate_tokens(text)
                if text:
                    print(f"  [{s['session_id']}] (裁剪) {text}")
                    total_tokens += t
                    included_sessions += 1
                break
            tags = json.loads(s["tags"]) if s["tags"] else []
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            print(f"  [{s['session_id']}] {s['created_at'][:10]} "
                  f"importance={s['importance']}{tag_str}")
            print(f"    {text}")
            total_tokens += t
            included_sessions += 1

    if results["observations"]:
        print(f"\n观察 ({len(results['observations'])} 条):")
        for o in results["observations"]:
            text = o.get("content", "")[:200]
            t = estimate_tokens(text)
            if total_tokens + t > char_budget:
                remaining = max(0, char_budget - total_tokens)
                text = text[:remaining * 3]
                t = estimate_tokens(text)
                if text:
                    print(f"  #{o['id']} [{o['obs_type']}] (裁剪) {text}")
                    total_tokens += t
                    included_obs += 1
                break
            print(f"  #{o['id']} [{o['obs_type']}] importance={o['importance']}")
            print(f"    {text}")
            total_tokens += t
            included_obs += 1

    if not results["sessions"] and not results["observations"]:
        print("没有找到匹配结果")

    print(f"\n--- 消耗统计 ---")
    print(f"预算: {budget} tokens | 已用: ~{total_tokens} tokens | "
          f"剩余: ~{max(0, budget - total_tokens)} tokens")
    print(f"包含: {included_sessions} 会话 + {included_obs} 观察")


def cmd_cleanup(args):
    """清理过期缓存、旧版本、压缩旧摘要"""
    conn = store.get_db()
    stats = {"versions_removed": 0, "cache_files_removed": 0, "snapshots_removed": 0}

    try:
        # 1. 清理旧版本记录（保留最近 N 天）
        cutoff = days_ago(args.keep_days)
        row = conn.execute(
            "DELETE FROM memory_versions WHERE created_at < ?", (cutoff,)
        )
        stats["versions_removed"] = row.rowcount

        # 2. 清理过期的 semantic index 缓存文件
        from pathlib import Path
        data_dir = Path(__file__).parent / "data"
        cutoff_dt = datetime.fromisoformat(cutoff)
        for fname in ["semantic_index.json", "semantic_meta.json"]:
            fpath = data_dir / fname
            if fpath.exists():
                f_mtime = datetime.fromtimestamp(fpath.stat().st_mtime)
                if f_mtime < cutoff_dt:
                    fpath.unlink()
                    stats["cache_files_removed"] += 1

        # 3. 清理旧快照（压缩超过 keep_days 的非关键快照）
        row = conn.execute(
            "DELETE FROM snapshots WHERE created_at < ?", (cutoff,)
        )
        stats["snapshots_removed"] = row.rowcount

        conn.commit()
    finally:
        conn.close()

    print(f"=== 清理完成 (保留 {args.keep_days} 天内数据) ===")
    print(f"  版本记录删除: {stats['versions_removed']} 条")
    print(f"  缓存文件删除: {stats['cache_files_removed']} 个")
    print(f"  旧快照删除:   {stats['snapshots_removed']} 条")
    total = sum(stats.values())
    print(f"  总计清理:     {total} 项")


def cmd_weekly_report(args):
    """输出周报统计"""
    conn = store.get_db()
    try:
        cutoff_7d = (datetime.now() - timedelta(days=7)).isoformat()
        cutoff_14d = (datetime.now() - timedelta(days=14)).isoformat()

        # 本周会话数
        week_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE created_at > ?",
            (cutoff_7d,)
        ).fetchone()[0]

        # 上周会话数（用于对比）
        prev_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE created_at BETWEEN ? AND ?",
            (cutoff_14d, cutoff_7d)
        ).fetchone()[0]

        # 本周观察数
        week_obs = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE created_at > ?",
            (cutoff_7d,)
        ).fetchone()[0]

        # 本周观察类型分布
        obs_dist = conn.execute("""
            SELECT obs_type, COUNT(*) as cnt FROM observations
            WHERE created_at > ? GROUP BY obs_type ORDER BY cnt DESC
        """, (cutoff_7d,)).fetchall()

        # 平均重要度
        avg_imp = conn.execute(
            "SELECT AVG(importance) FROM sessions WHERE created_at > ?",
            (cutoff_7d,)
        ).fetchone()[0] or 0

        # 总注入 token 统计
        total_injected = conn.execute(
            "SELECT COALESCE(SUM(token_estimate), 0) FROM sessions WHERE created_at > ?",
            (cutoff_7d,)
        ).fetchone()[0]

        avg_injected = conn.execute(
            "SELECT COALESCE(AVG(token_estimate), 0) FROM sessions WHERE created_at > ? AND token_estimate > 0",
            (cutoff_7d,)
        ).fetchone()[0] or 0

        # 缓存命中率
        cache_stats = _get_cache_stats(conn, cutoff_7d)

        # 对比上周 token
        prev_tokens = conn.execute(
            "SELECT COALESCE(SUM(token_estimate), 0) FROM sessions WHERE created_at BETWEEN ? AND ?",
            (cutoff_14d, cutoff_7d)
        ).fetchone()[0] or 0

    finally:
        conn.close()

    # 输出
    print(f"=== 周报 ({(datetime.now() - timedelta(days=7)).strftime('%m/%d')} ~ {datetime.now().strftime('%m/%d')}) ===")
    print()
    print(f"会话: {week_sessions} 个 (上周 {prev_sessions})", end="")
    if prev_sessions > 0:
        delta = week_sessions - prev_sessions
        pct = delta / prev_sessions * 100
        print(f"  {'+' if delta >= 0 else ''}{delta} ({'+' if pct >= 0 else ''}{pct:.0f}%)")
    else:
        print()
    print(f"观察: {week_obs} 条")
    print(f"平均重要度: {avg_imp:.2f}")

    if obs_dist:
        print(f"\n触发分布:")
        for r in obs_dist:
            bar = '#' * min(r["cnt"], 30)
            print(f"  {r['obs_type']:12s} {r['cnt']:3d}  {bar}")

    print(f"\nToken 统计:")
    print(f"  总注入 token: {total_injected}")
    print(f"  平均注入 token: {avg_injected:.0f}")
    print(f"  对比上周: {prev_tokens} -> {total_injected}", end="")
    if prev_tokens > 0:
        delta = total_injected - prev_tokens
        pct = delta / prev_tokens * 100
        print(f"  {'+' if delta >= 0 else ''}{pct:.0f}%")
    else:
        print()

    print(f"\n缓存状态:")
    print(f"  总查询: {cache_stats['total_queries']}")
    print(f"  缓存命中: {cache_stats['cache_hits']}")
    print(f"  命中率: {cache_stats['hit_rate']:.1%}")


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
    """恢复记忆内容层"""
    if args.type == "session":
        ok, msg = store.restore_session_content(args.entity, to_version_id=args.version, steps=args.steps)
    elif args.type == "observation":
        ok, msg = store.restore_observation_content(int(args.entity), to_version_id=args.version, steps=args.steps)
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

    # trigger
    p_trigger = sub.add_parser("trigger", help="测试触发路由器")
    p_trigger.add_argument("message", help="用户消息文本")

    # budgeted-search
    p_bsearch = sub.add_parser("budgeted-search", help="带 token 预算的搜索")
    p_bsearch.add_argument("query")
    p_bsearch.add_argument("--budget", "-b", type=int, default=500, help="token 预算")
    p_bsearch.add_argument("--limit", "-l", type=int, default=10)

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="清理过期数据")
    p_cleanup.add_argument("--keep-days", type=int, default=90, help="保留最近 N 天数据")

    # weekly-report
    sub.add_parser("weekly-report", help="输出周报统计")

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
        "trigger": cmd_trigger, "budgeted-search": cmd_budgeted_search,
        "cleanup": cmd_cleanup, "weekly-report": cmd_weekly_report,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
