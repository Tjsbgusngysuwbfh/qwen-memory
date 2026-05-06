"""Trigger router regression checks."""

from __future__ import annotations

from bootstrap import bootstrap

bootstrap()

from trigger_router import FULL, LIGHT, SKIP, evaluate_trigger


def assert_true(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


def test_greeting_skip() -> bool:
    samples = ["你好", "Hello!", "早上好", "hey"]
    results = [evaluate_trigger(text).action == SKIP for text in samples]
    return assert_true("greeting_skip", all(results), str(results))


def test_project_full() -> bool:
    samples = [
        "帮我看看这个 bug",
        "项目部署到 docker 里",
        "代码 review 一下",
        "这个报错怎么修",
    ]
    results = [evaluate_trigger(text).action == FULL for text in samples]
    return assert_true("project_full", all(results), str(results))


def test_memory_query_light() -> bool:
    samples = ["之前做过什么", "上次的结果", "搜索一下记录"]
    results = [evaluate_trigger(text).action == LIGHT for text in samples]
    return assert_true("memory_query_light", all(results), str(results))


def test_sensitive_and_default() -> bool:
    sensitive = evaluate_trigger("这是密码 token 请不要注入").action == SKIP
    fallback = evaluate_trigger("随便聊聊今天的进度").action in {LIGHT, FULL}
    return assert_true("sensitive_skip_and_default", sensitive and fallback)


def main() -> int:
    checks = [
        test_greeting_skip,
        test_project_full,
        test_memory_query_light,
        test_sensitive_and_default,
    ]
    results = [check() for check in checks]
    print(f"trigger tests: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
