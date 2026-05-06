"""
Qwen Memory 体验回归测试
锁定关键体验不退化：
  1. restore_content_semantics — rollback 只回滚内容层，不回滚生命周期字段
  2. cache_hit_log_chain — log_budget 写入 cache_hit 字段，可正确查询
  3. obs_type_sql_pushdown — search_observations(obs_type=...) 在 SQL 层过滤
  4. mixed_ranking_balanced — fused_search 混合排序不丢关键结果
  5. summary_compact_priority — adaptive_injection 优先使用 summary_compact
"""
import sys
import os
import time
import hashlib
import json

from bootstrap import bootstrap

bootstrap()

import store

PASS = FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} — {detail}")


def use_db(name):
    p = store.DB_DIR / f"exp_{name}.db"
    if p.exists():
        p.unlink()
    store.set_db_path(p)
    return p


# ============ 测试 1: restore_content_semantics ============

def test_restore_content_semantics():
    """验证 rollback_session 只回滚内容层，不回滚生命周期字段"""
    print("\n--- test_restore_content_semantics ---")
    use_db("restore_sem")
    sid = "exp-restore-test"

    # 创建会话 v1
    store.upsert_session(
        session_id=sid,
        summary="原始摘要 v1",
        summary_short="短摘要 v1",
        summary_compact="紧凑摘要 v1",
        importance=0.8,
        tags=["alpha"],
        token_estimate=100,
    )
    time.sleep(0.05)

    # 创建会话 v2（修改内容层 + 生命周期字段）
    store.upsert_session(
        session_id=sid,
        summary="修改摘要 v2",
        summary_short="短摘要 v2",
        summary_compact="紧凑摘要 v2",
        importance=0.9,
        tags=["alpha", "beta"],
        token_estimate=200,
    )
    time.sleep(0.05)

    # 创建会话 v3（再次修改内容层）
    store.upsert_session(
        session_id=sid,
        summary="再次修改 v3",
        summary_short="短摘要 v3",
        summary_compact="紧凑摘要 v3",
        importance=0.7,
        tags=["alpha", "beta", "gamma"],
        token_estimate=300,
    )

    # 结束会话（设置生命周期字段 ended_at）—— 这会创建一个版本
    store.end_session(sid, summary="结束摘要", summary_short="结束短摘要")

    # 记录当前 ended_at 和 token_estimate（不应被回滚）
    detail_before = store.get_session_detail(sid)
    ended_at_before = detail_before["session"]["ended_at"]
    token_before = detail_before["session"]["token_estimate"]

    # 回滚 1 步（从 end 状态回滚到 v3 的内容）
    ok, msg = store.rollback_session(sid, steps=1)
    check("回滚返回 True", ok, msg)

    detail = store.get_session_detail(sid)
    s = detail["session"]

    # 内容层应回滚到 v3（end 之前最近的有 after_data 的版本）
    check("summary 回滚到 v3", s["summary"] == "再次修改 v3",
          f"got: {s['summary']}")
    check("summary_compact 回滚到 v3", s["summary_compact"] == "紧凑摘要 v3",
          f"got: {s['summary_compact']}")
    check("tags 回滚到 v3", "gamma" in s["tags"],
          f"got: {s['tags']}")

    # 生命周期字段不应被回滚
    check("ended_at 保留（不回滚生命周期）", s["ended_at"] == ended_at_before,
          f"ended_at={s['ended_at']}")
    check("token_estimate 保留当前值", s["token_estimate"] == token_before,
          f"got: {s['token_estimate']} (expected {token_before})")


# ============ 测试 2: cache_hit_log_chain ============

def test_cache_hit_log_chain():
    """验证 log_budget 写入 cache_hit 字段，可正确查询"""
    print("\n--- test_cache_hit_log_chain ---")
    use_db("cache_hit")

    # 写入一条 cache_hit=1 的预算日志
    store.log_budget(
        query="test query with cache",
        max_tokens=500,
        used_tokens=200,
        sessions_injected=2,
        obs_injected=3,
        compact_text="compact result",
        cache_hit=1,
    )

    # 写入一条 cache_hit=0 的预算日志
    store.log_budget(
        query="test query without cache",
        max_tokens=500,
        used_tokens=300,
        sessions_injected=3,
        obs_injected=1,
        compact_text="",
        cache_hit=0,
    )

    # 查询验证
    logs = store.get_budget_log(limit=10)
    check("get_budget_log 返回列表", isinstance(logs, list))
    check("日志数量 >= 2", len(logs) >= 2, f"got {len(logs)}")

    # 找到 cache_hit=1 的记录
    hit_logs = [l for l in logs if l.get("cache_hit") == 1]
    check("存在 cache_hit=1 的记录", len(hit_logs) >= 1)

    if hit_logs:
        hl = hit_logs[0]
        check("cache_hit 日志 query 正确", hl["query"] == "test query with cache",
              f"got: {hl['query']}")
        check("cache_hit 日志 used_tokens 正确", hl["used_tokens"] == 200,
              f"got: {hl['used_tokens']}")

    # 找到 cache_hit=0 的记录
    miss_logs = [l for l in logs if l.get("cache_hit") == 0]
    check("存在 cache_hit=0 的记录", len(miss_logs) >= 1)

    # 通过 mem.py 的 _get_cache_stats 查询验证
    from mem import _get_cache_stats
    conn = store.get_db()
    try:
        stats = _get_cache_stats(conn)
        check("cache_stats.total_queries >= 2", stats["total_queries"] >= 2,
              f"got: {stats['total_queries']}")
        check("cache_stats.cache_hits >= 1", stats["cache_hits"] >= 1,
              f"got: {stats['cache_hits']}")
        check("cache_stats.hit_rate > 0", stats["hit_rate"] > 0,
              f"got: {stats['hit_rate']}")
    finally:
        conn.close()


# ============ 测试 3: obs_type_sql_pushdown ============

def test_obs_type_sql_pushdown():
    """验证 search_observations(obs_type='bugfix') 只返回 bugfix 类型"""
    print("\n--- test_obs_type_sql_pushdown ---")
    use_db("obs_type")

    sid = "exp-obs-type-test"
    store.upsert_session(session_id=sid, summary="测试会话", importance=0.8)

    # 添加多种类型的观察（使用共同关键词以便跨类型搜索）
    store.add_observation(sid, "bugfix", "修复了登录 bug 系统问题", importance=0.8)
    store.add_observation(sid, "bugfix", "修复了超时 bug 系统问题", importance=0.7)
    store.add_observation(sid, "decision", "选择了方案 A 系统方案", importance=0.9)
    store.add_observation(sid, "discovery", "发现了性能瓶颈 系统发现", importance=0.6)
    store.add_observation(sid, "task", "完成了部署任务 系统任务", importance=0.5)
    store.add_observation(sid, "note", "记录了一条笔记 系统笔记", importance=0.4)

    # 搜索 bugfix 类型
    results = store.search_observations("系统", obs_type="bugfix")
    check("search_observations 返回列表", isinstance(results, list))
    check("bugfix 搜索有结果", len(results) >= 1, f"got {len(results)}")

    # 验证所有返回结果都是 bugfix 类型
    if results:
        all_bugfix = all(o["obs_type"] == "bugfix" for o in results)
        check("所有结果都是 bugfix 类型", all_bugfix,
              f"types: {[o['obs_type'] for o in results]}")

    # 搜索 decision 类型
    results_dec = store.search_observations("系统", obs_type="decision")
    if results_dec:
        all_decision = all(o["obs_type"] == "decision" for o in results_dec)
        check("decision 搜索只返回 decision 类型", all_decision,
              f"types: {[o['obs_type'] for o in results_dec]}")

    # 不指定类型时应返回所有类型
    results_all = store.search_observations("系统")
    types_found = set(o["obs_type"] for o in results_all)
    check("不指定类型时包含多种类型", len(types_found) > 1,
          f"types: {types_found}")


# ============ 测试 4: mixed_ranking_balanced ============

def test_mixed_ranking_balanced():
    """验证 fused_search 在 sessions 和 observations 都有时，每类至少保留几条"""
    print("\n--- test_mixed_ranking_balanced ---")
    use_db("mixed_rank")

    # 添加多个会话
    for i in range(5):
        store.upsert_session(
            session_id=f"exp-mix-s{i}",
            summary=f"Alpha 关键词会话 {i} 包含测试内容",
            importance=0.5 + i * 0.1,
        )

    # 添加多个观察
    sid = "exp-mix-s0"
    for i in range(5):
        store.add_observation(
            sid, "bugfix",
            f"Alpha 关键词修复 {i} 解决了重要问题",
            importance=0.5 + i * 0.1,
        )

    # 融合搜索
    result = store.fused_search("Alpha", limit=10)
    sessions = result.get("sessions", [])
    observations = result.get("observations", [])

    check("fused_search 返回 sessions", len(sessions) > 0,
          f"got {len(sessions)} sessions")
    check("fused_search 返回 observations", len(observations) > 0,
          f"got {len(observations)} observations")

    # 每类至少保留 1 条（只要原始数据中有匹配）
    check("sessions 至少保留 1 条", len(sessions) >= 1,
          f"got {len(sessions)}")
    check("observations 至少保留 1 条", len(observations) >= 1,
          f"got {len(observations)}")

    # 结果无重复
    session_ids = [s["session_id"] for s in sessions]
    check("sessions 无重复", len(session_ids) == len(set(session_ids)))

    obs_ids = [o["id"] for o in observations]
    check("observations 无重复", len(obs_ids) == len(set(obs_ids)))


# ============ 测试 5: summary_compact_priority ============

def test_summary_compact_priority():
    """验证 adaptive_injection 优先使用 summary_compact"""
    print("\n--- test_summary_compact_priority ---")
    use_db("compact_pri")

    sid = "exp-compact-test"
    store.upsert_session(
        session_id=sid,
        summary="这是一段很长的完整摘要，包含了会话的所有详细信息，用于测试 summary_compact 优先级",
        summary_short="这是短摘要",
        summary_compact="这是紧凑摘要，比短摘要更精炼",
        importance=0.8,
        tags=["test"],
    )
    store.add_observation(sid, "bugfix", "修复了一个重要的 bug", importance=0.7)

    # 调用 adaptive_injection
    from budget import adaptive_injection
    result = adaptive_injection("test", max_tokens=1000)

    check("adaptive_injection 返回结果", result is not None)
    check("返回 compact_text", "compact_text" in result)

    compact_text = result.get("compact_text", "")
    check("compact_text 非空", len(compact_text) > 0, f"got: '{compact_text}'")

    # 验证 compact_text 中包含紧凑摘要而非完整摘要
    check("compact_text 包含紧凑摘要",
          "紧凑摘要" in compact_text,
          f"compact_text: {compact_text[:200]}")

    # 验证不包含完整摘要的长文本
    check("compact_text 不包含完整摘要长文本",
          "包含了会话的所有详细信息" not in compact_text,
          f"compact_text 中意外包含了完整摘要")

    # 验证返回的 sessions 中 summary_compact 被用于计算 token
    if result.get("sessions"):
        s = result["sessions"][0]
        check("返回的 session 有 summary_compact",
              bool(s.get("summary_compact")),
              f"summary_compact: {s.get('summary_compact')}")


# ============ 主函数 ============

def main():
    print("=" * 60)
    print("Qwen Memory 体验回归测试")
    print("=" * 60)

    test_restore_content_semantics()
    test_cache_hit_log_chain()
    test_obs_type_sql_pushdown()
    test_mixed_ranking_balanced()
    test_summary_compact_priority()

    print("\n" + "=" * 60)
    print(f"结果: {PASS} PASS / {FAIL} FAIL")
    print("=" * 60)

    # 清理
    for f in os.listdir(str(store.DB_DIR)):
        if f.startswith("exp_") and f.endswith(".db"):
            os.unlink(str(store.DB_DIR / f))

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
