"""
Qwen Memory 恢复内容层校验测试
验证 restore_session_content 的行为：
  1. 内容层字段（summary, summary_compact, tags 等）恢复到目标版本
  2. 生命周期字段（ended_at, token_estimate）不被恢复
  3. checksum 重算
  4. FTS 索引同步更新
"""
import sys
import os
import time
import hashlib

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
    p = store.DB_DIR / f"rollback_{name}.db"
    if p.exists():
        p.unlink()
    store.DB_PATH = p
    return p


print("=" * 60)
print("回滚校验测试")
print("=" * 60)

# === 准备测试数据库 ===
use_db("test")
sid = "rollback-test-session"

# 1. 创建会话 v1（importance >= 0.7 才生成版本记录）
store.upsert_session(
    session_id=sid,
    summary="原始摘要 v1",
    summary_short="短摘要 v1",
    summary_compact="紧凑摘要 v1",
    importance=0.8,
    tags=["tag1"],
    token_estimate=100,
)
time.sleep(0.05)

# 2. 更新会话 v2
store.upsert_session(
    session_id=sid,
    summary="修改后摘要 v2",
    summary_short="短摘要 v2",
    summary_compact="紧凑摘要 v2",
    importance=0.8,
    tags=["tag1", "tag2"],
    token_estimate=200,
)

# 记录当前 ended_at（应该为 None/空）
before_detail = store.get_session_detail(sid)
before_ended_at = before_detail["session"]["ended_at"]
before_checksum = before_detail["session"]["checksum"]

print("\n--- 测试 1: 内容层字段回滚 ---")
ok, msg = store.restore_session_content(sid, steps=1)
check("恢复返回 True", ok, msg)

detail = store.get_session_detail(sid)
s = detail["session"]
check("summary 回滚到 v1", s["summary"] == "原始摘要 v1",
      f"got: {s['summary']}")
check("summary_compact 回滚到 v1", s["summary_compact"] == "紧凑摘要 v1",
      f"got: {s['summary_compact']}")
check("tags 回滚到 v1", '"tag1"' in s["tags"] and "tag2" not in s["tags"],
      f"got: {s['tags']}")
# importance 两个版本都是 0.8，验证回滚后仍是 0.8
check("importance 回滚正确", s["importance"] == 0.8,
      f"got: {s['importance']}")

print("\n--- 测试 2: 生命周期字段不被回滚 ---")
check("ended_at 未变", s["ended_at"] == before_ended_at,
      f"before={before_ended_at}, after={s['ended_at']}")
check("token_estimate 未变", s["token_estimate"] == 200,
      f"got: {s['token_estimate']} (expected 200, not 100)")

print("\n--- 测试 3: checksum 重算 ---")
expected_checksum = hashlib.md5("原始摘要 v1".encode()).hexdigest()[:12]
check("checksum 已重算", s["checksum"] == expected_checksum,
      f"got: {s['checksum']}, expected: {expected_checksum}")

print("\n--- 测试 4: FTS 索引同步 ---")
# 搜索原始摘要内容（回滚后的内容）
fts_results = store.search_sessions("原始摘要")
fts_ids = [r["session_id"] for r in fts_results]
check("FTS 能搜到回滚后的内容", sid in fts_ids,
      f"搜索结果: {fts_ids}")
# 确认搜不到 v2 的内容
fts_v2 = store.search_sessions("修改后摘要")
fts_v2_ids = [r["session_id"] for r in fts_v2]
check("FTS 不再索引 v2 内容", sid not in fts_v2_ids,
      f"搜索 v2 结果: {fts_v2_ids}")

print("\n" + "=" * 60)
print(f"结果: {PASS} PASS / {FAIL} FAIL")
print("=" * 60)

# 清理
for f in os.listdir(str(store.DB_DIR)):
    if f.startswith("rollback_") and f.endswith(".db"):
        os.unlink(str(store.DB_DIR / f))

sys.exit(0 if FAIL == 0 else 1)
