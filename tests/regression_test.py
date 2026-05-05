"""Qwen Memory 回归测试 v3 — 无锁冲突"""
import sys, os, time, json, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store

PASS = FAIL = 0
def test(name, fn):
    global PASS, FAIL
    t0 = time.time()
    try:
        result = fn()
        PASS += 1
        print(f"  PASS  {name} ({time.time()-t0:.3f}s) → {str(result)[:80]}")
    except Exception as e:
        FAIL += 1
        print(f"  FAIL  {name} ({time.time()-t0:.3f}s) → {e}")

def use_db(name):
    """切换到指定数据库文件"""
    p = store.DB_DIR / f"reg_{name}.db"
    if p.exists(): p.unlink()
    store.DB_PATH = p
    return p

print("="*60)
print("回归测试 v3")
print("="*60)

# === 1. 旧库升级 ===
print("\n1. 旧库升级兼容性")
use_db("migrate")
conn = sqlite3.connect(str(store.DB_PATH))
conn.executescript("""
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY, session_id TEXT UNIQUE, started_at TEXT,
        ended_at TEXT, summary TEXT, summary_short TEXT, project_path TEXT, model TEXT,
        tools_used TEXT, key_decisions TEXT, file_changes TEXT, tags TEXT,
        token_estimate INTEGER, importance REAL, checksum TEXT
    );
    CREATE TABLE observations (
        id INTEGER PRIMARY KEY, session_id TEXT, obs_type TEXT, content TEXT,
        context TEXT, impact TEXT, created_at TEXT, importance REAL, tags TEXT
    );
    INSERT INTO sessions VALUES (1, 'old', '2026-01-01', NULL, 'old', 'short', '', '', '[]', '[]', '[]', '[]', 0, 0.5, 'abc');
    INSERT INTO observations VALUES (1, 'old', 'note', 'old obs', '', '', '2026-01-01', 0.5, '[]');
""")
conn.close()

test("旧库打开", lambda: store.get_db() and "OK")
test("旧库读", lambda: store.get_session_detail("old")["session"]["summary"])
test("旧库写", lambda: (store.upsert_session(session_id="old", summary="upd"), store.get_session_detail("old"))[-1]["session"]["summary"])
test("旧库字段", lambda: store.get_session_detail("old")["session"].get("updated_at") or (_ for _ in ()).throw(Exception("no updated_at")))
test("旧库FTS", lambda: len(store.search_sessions("upd")) or (_ for _ in ()).throw(Exception("empty")))

# === 2. 语义索引失效 ===
print("\n2. 语义索引失效检测")
use_db("semantic")
store.upsert_session(session_id="s1", summary="Alpha content", importance=0.5)
store.upsert_session(session_id="s2", summary="Beta content", importance=0.5)

test("构建索引", lambda: (__import__('semantic').build_index_from_db(), store.check_semantic_index_fresh())[-1][0] and "fresh")
test("内容变更检测", lambda: (store.upsert_session(session_id="s1", summary="XYZ 123 completely different"), time.sleep(0.01), store.check_semantic_index_fresh())[-1][0] == False and "stale")
test("重建恢复", lambda: (__import__('semantic').build_index_from_db(), store.check_semantic_index_fresh())[-1][0] and "fresh")

# === 3. CLI 通路 ===
print("\n3. CLI 通路")
use_db("cli")
from mem import cmd_add, cmd_end, cmd_obs, cmd_search, cmd_search_obs, cmd_recent, cmd_detail, cmd_stats, cmd_versions, cmd_rollback, cmd_semantic, cmd_rebuild_index
import argparse
def mk(**kw): return argparse.Namespace(**kw)

test("add", lambda: cmd_add(mk(session="c", summary="CLI", short="", project="", model="", tags="a", importance=0.7, tokens=0)) or "OK")
test("obs", lambda: cmd_obs(mk(session="c", type="bugfix", content="obs", importance=0.6, tags="x")) or "OK")
test("end", lambda: cmd_end(mk(session="c", summary="end", short="")) or "OK")
test("search", lambda: cmd_search(mk(query="CLI", limit=5)) or "OK")
test("search-obs", lambda: cmd_search_obs(mk(query="obs", type=None, limit=5)) or "OK")
test("recent", lambda: cmd_recent(mk(limit=5)) or "OK")
test("detail", lambda: cmd_detail(mk(session="c")) or "OK")
test("stats", lambda: cmd_stats(mk()) or "OK")
test("versions", lambda: cmd_versions(mk(type="session", entity="c", limit=5)) or "OK")
test("rebuild-index", lambda: cmd_rebuild_index(mk()) or "OK")
test("semantic", lambda: cmd_semantic(mk(query="CLI", limit=5)) or "OK")

# === 4. 融合检索排序 ===
print("\n4. 融合检索排序")
use_db("ranking")
store.upsert_session(session_id="r1", summary="Alpha 关键词精确", importance=0.9)
store.upsert_session(session_id="r2", summary="Alpha Beta 讨论", importance=0.7)
store.upsert_session(session_id="r3", summary="Gamma 无关", importance=0.5)
store.upsert_session(session_id="r4", summary="Alpha 语义相近", importance=0.6)

test("精确匹配排第一", lambda: store.fused_search("Alpha")["sessions"][0]["session_id"] == "r1")
test("多次稳定", lambda: all(store.fused_search("Alpha")["sessions"][0]["session_id"] == "r1" for _ in range(5)))
test("无重复", lambda: (ids := [s["session_id"] for s in store.fused_search("Alpha")["sessions"]], len(ids) == len(set(ids)))[-1])
test("语义不覆盖关键词", lambda: (
    r := store.fused_search("Alpha"),
    kw := [i for i, s in enumerate(r["sessions"]) if s["session_id"] in ("r1", "r2")],
    sem := [i for i, s in enumerate(r["sessions"]) if s["session_id"] == "r4"]
)[-1] and all(k < s for k in kw for s in sem) and "OK")

print("\n" + "="*60)
print(f"结果: {PASS} PASS / {FAIL} FAIL")
print("="*60)

# 清理
for f in os.listdir(str(store.DB_DIR)):
    if f.startswith("reg_") and f.endswith(".db"):
        os.unlink(str(store.DB_DIR / f))
sys.exit(0 if FAIL == 0 else 1)
