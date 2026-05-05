"""Qwen Memory 压力测试 v2"""
import sys, os, time, json, threading, random, string
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from qwen_memory import store
from qwen_memory.semantic import SemanticIndex, build_index_from_db

PASS = FAIL = 0
RESULTS = []

def test(name, fn):
    global PASS, FAIL
    t0 = time.time()
    try:
        result = fn()
        PASS += 1
        print(f"  PASS  {name} ({time.time()-t0:.3f}s) → {str(result)[:80]}")
    except Exception as e:
        FAIL += 1
        RESULTS.append((name, str(e)[:200]))
        print(f"  FAIL  {name} ({time.time()-t0:.3f}s) → {str(e)[:200]}")

def fresh_db():
    p = store.DB_DIR / "stress_test.db"
    if p.exists(): p.unlink()
    store.DB_PATH = p

print("="*60)
print("Qwen Memory 压力测试 v2")
print("="*60)

print("\n1. 数据完整性")
test("批量写入1000条", lambda: (
    fresh_db(),
    [store.upsert_session(session_id=f"b{i:04d}", summary=f"bulk#{i}: "+"".join(random.choices("abcdefghij",k=50)), importance=random.random()) for i in range(1000)],
    f"total={store.get_stats()['total_sessions']}"
)[-1])
test("批量观察500条", lambda: (
    store.upsert_session(session_id="obs-t", summary="target"),
    [store.add_observation(session_id="obs-t", obs_type=random.choice(["decision","bugfix","discovery","task"]), content=f"obs#{i}: "+"".join(random.choices(string.ascii_letters,k=80)), importance=random.random()) for i in range(500)],
    "500 ok"
)[-1])

errors = []
def writer(tid):
    try:
        for i in range(50): store.upsert_session(session_id=f"t{tid}-{i}", summary=f"thread{tid}#{i}")
    except Exception as e: errors.append(str(e))
ts = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
t0 = time.time()
for t in ts: t.start()
for t in ts: t.join()
test("10线程并发写入", lambda: (None if not errors else (_ for _ in ()).throw(Exception(f"{len(errors)} errors")), f"{time.time()-t0:.2f}s")[-1])

print("\n2. 搜索准确性")
fresh_db()
store.upsert_session(session_id="zh-1", summary="手机控制能力全量补全 Android ADB", importance=0.9)
store.upsert_session(session_id="zh-2", summary="桌面GUI自动化 pywinauto 截图 OCR", importance=0.8)
store.upsert_session(session_id="zh-3", summary="记忆系统 SQLite FTS5 语义搜索", importance=0.7)
test("中文搜索", lambda: f"手机={len(store.search_sessions('手机'))} 桌面={len(store.search_sessions('桌面'))} 记忆={len(store.search_sessions('记忆'))} 无={len(store.search_sessions('xyz'))}")
test("英文搜索", lambda: f"ADB={len(store.search_sessions('ADB'))} SQLite={len(store.search_sessions('SQLite'))}")
test("空查询", lambda: f"s={len(store.search_sessions(''))} o={len(store.search_observations(''))} ws={len(store.search_sessions('   '))}")
test("超长查询", lambda: f"len={len(store.search_sessions('测试'*500))}")

print("\n3. 版本控制压力")
fresh_db()
store.upsert_session(session_id="vs", summary="v0")
for i in range(50): store.upsert_session(session_id="vs", summary=f"v{i+1}", importance=0.1+i*0.01)
test("50次修改+深度回滚", lambda: (
    store.get_version_history("session", "vs", 100),
    store.rollback_session("vs", steps=50),
    store.get_session_detail("vs")
)[-1] and f"versions={len(store.get_version_history('session','vs',100))}")
test("回滚不存在实体", lambda: (not store.rollback_session("none")[0] and not store.rollback_observation(999999)[0]) and "OK")
fresh_db()
store.upsert_session(session_id="lr", summary="base")
for i in range(10): store.upsert_session(session_id="lr", summary=f"v{i+1}")
test("循环回滚", lambda: (
    store.rollback_session("lr", steps=5)[1],
    store.upsert_session(session_id="lr", summary="new"),
    store.rollback_session("lr", steps=1)[1]
)[-1])

print("\n4. 边界条件")
fresh_db()
specials = ["\"引号\"","'单引号'","\\反斜杠","\n换行","\t制表","中文：，。！","emoji🎉","'; DROP TABLE--","甲"*1000]
for i,t in enumerate(specials): store.upsert_session(session_id=f"sp-{i}", summary=t)
test("特殊字符", lambda: f"{sum(1 for i in range(len(specials)) if store.get_session_detail(f'sp-{i}') and store.get_session_detail(f'sp-{i}')['session']['summary']==specials[i])}/{len(specials)}")
fresh_db()
test("空数据库", lambda: f"s={len(store.search_sessions('x'))} r={len(store.get_recent_sessions())} v={store.get_stats()['total_sessions']}")
store.upsert_session(session_id="huge", summary="A"*100000)
d = store.get_session_detail("huge")
test("超大文本100KB", lambda: f"{len(d['session']['summary'])}/100000 ({len(d['session']['summary'])*100//100000}%)")
fresh_db()
for i in range(100): store.upsert_session(session_id=f"fts-{i}", summary=f"FTS#{i} {random.choice(['Alpha','Beta','Gamma'])}")
conn = store.get_db(); conn.execute("DELETE FROM sessions WHERE session_id='fts-50'"); conn.commit(); conn.close()
test("FTS一致性", lambda: f"deleted fts-50 searchable={len(store.search_sessions('FTS#50'))} (expect 0)")
fresh_db()
for i in range(3): store.upsert_session(session_id="vh", summary=f"v{i}")
vh = store.get_version_history("session", "vh", 10)
test("版本历史完整性", lambda: f"v={len(vh)} with_data={sum(1 for v in vh if v['before_data'] and v['after_data'])}")

print("\n5. 语义搜索")
store.upsert_session(session_id="demo1", summary="手机控制 Android ADB scrcpy YADB", importance=0.9)
store.upsert_session(session_id="demo2", summary="桌面自动化 pywinauto OCR 截图", importance=0.8)
test("TF-IDF准确性", lambda: f"queries={sum(1 for q in ['怎么控制安卓设备','桌面元素定位','ADB版本管理'] if build_index_from_db().search(q, top_k=3))}/3")

print("\n6. MCP Server")
from qwen_memory.mcp_server import handle_tool_call
test("MCP工具调用", lambda: (
    json.loads(handle_tool_call("mem_search", {"query": "手机"})),
    json.loads(handle_tool_call("mem_add_session", {"session_id": "mcp", "summary": "test"})),
    json.loads(handle_tool_call("mem_stats", {})),
    json.loads(handle_tool_call("nonexistent", {}))
)[-1].get("error") and "OK")

print("\n"+"="*60)
print(f"结果: {PASS} PASS / {FAIL} FAIL")
if FAIL:
    print("\n失败项:")
    for n,e in RESULTS: print(f"  {n}: {e}")
print("="*60)

if os.path.exists(str(store.DB_DIR/"stress_test.db")): os.unlink(str(store.DB_DIR/"stress_test.db"))
sys.exit(0 if FAIL==0 else 1)
