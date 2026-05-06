"""Budget-related regression checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bootstrap import bootstrap

bootstrap()

import budget
import store


def use_db(name: str):
    path = store.DB_DIR / f"budget_{name}.db"
    if path.exists():
        path.unlink()
    store.set_db_path(path)
    return path


def assert_true(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


def test_estimate_tokens() -> bool:
    cases = [
        ("", 0),
        ("hello", 1),
        ("你好世界", 1),
        ("Hello 你好世界", 1),
        ("这是一段较长的中文文本，用于验证 token 估算函数可以稳定返回正数。", 10),
    ]
    ok = True
    for text, minimum in cases:
        value = budget.estimate_tokens(text)
        ok &= assert_true("estimate_tokens", value >= minimum, f"{text[:12]} -> {value}")
    return ok


def test_recency_score() -> bool:
    now = datetime.now(timezone.utc)
    recent = budget._recency_score((now - timedelta(hours=1)).isoformat())
    older = budget._recency_score((now - timedelta(days=30)).isoformat())
    oldest = budget._recency_score((now - timedelta(days=90)).isoformat())
    empty = budget._recency_score("")
    return assert_true("recency ordering", recent > older > oldest and empty == 0.5)


def test_trim_and_compact() -> bool:
    rows = [
        {"summary": "alpha", "summary_compact": "alpha compact", "fused_score": 0.9, "token_estimate": 120},
        {"summary": "beta", "summary_compact": "beta compact", "fused_score": 0.5, "token_estimate": 120},
        {"summary": "gamma", "summary_compact": "gamma compact", "fused_score": 0.1, "token_estimate": 120},
    ]
    selected, stats = budget.trim_results(rows, token_budget=40)
    compact = budget.compact_summary(
        "这是一个较长摘要。这里有第二句。这里有第三句。",
        observations=[{"obs_type": "bugfix", "content": "修复了触发日志写入问题", "importance": 0.9}],
        max_tokens=40,
    )
    return (
        assert_true("trim_results selected", len(selected) >= 1, str(stats))
        and assert_true("trim_results budget", stats["total_tokens"] <= 40, str(stats))
        and assert_true("compact_summary", len(compact) > 0, compact)
    )


def test_log_budget_and_migration() -> bool:
    use_db("log")
    store.log_budget(
        query="预算测试",
        max_tokens=500,
        used_tokens=120,
        sessions_injected=2,
        obs_injected=1,
        compact_text="压缩摘要",
        cache_hit=True,
    )
    logs = store.get_budget_log(limit=5)
    found = any((item.get("query") or "") == "预算测试" and item.get("cache_hit") == 1 for item in logs)
    conn = store.get_db()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_budget_log)").fetchall()}
        has_cache_hit = "cache_hit" in cols
    finally:
        conn.close()
    return assert_true("budget_log cache_hit", found and has_cache_hit)


def main() -> int:
    checks = [
        test_estimate_tokens,
        test_recency_score,
        test_trim_and_compact,
        test_log_budget_and_migration,
    ]
    results = [check() for check in checks]
    print(f"budget tests: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
