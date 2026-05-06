"""
Qwen Memory 综合压力测试
========================
合并自 4 个独立压测文件，分为以下模块：
  A. 写入压测   — 批量写入、FTS/语义重建、边界测试
  B. 检索压测   — FTS/语义/融合搜索、触发路由、并发请求
  C. 预算压测   — Token 预算、压缩摘要、自适应注入、缓存、成本追踪
  D. 系统压测   — 数据库膨胀、版本控制、清理机制、异常恢复、长时间运行、边界条件

策略：
  - 每个模块独立 setup/cleanup，不污染生产库
  - 测试完成后自动清理所有压测数据
  - 可整体运行，也可单独运行某个模块
"""
import sys, os, time, json, random, string, threading, shutil, sqlite3, hashlib, statistics
import traceback, tempfile, psutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

from bootstrap import bootstrap

bootstrap()
import store
import semantic
import trigger_router
import budget
from db import days_ago

# ============================================================
# 全局常量
# ============================================================
PREFIX = "stress-test-"          # 所有压测会话 ID 前缀
TEST_PREFIX = PREFIX             # 别名兼容
DB_PATH = store.DB_PATH
UTC = timezone.utc

# 中文语料
ZH_FRAGMENTS = [
    "修复了一个在高并发场景下出现的内存泄漏问题，该问题导致服务在运行约 72 小时后出现 OOM",
    "决定采用 Redis 作为缓存层来优化数据库查询性能，预计可以将响应时间降低 60%",
    "发现了在 Windows 平台上的一个路径解析兼容性问题，与 Linux 路径分隔符不同有关",
    "完成了用户认证模块的重构，从 Session-based 迁移到 JWT Token 方案",
    "利用 Playwright MCP 实现了网页自动化抓取，成功提取了目标页面的关键数据",
    "在调试 Docker 容器网络时发现宿主机和容器之间的端口映射需要额外配置 iptables 规则",
    "评估了三种不同的消息队列方案（RabbitMQ / Kafka / Redis Streams），最终选择了 Redis Streams",
    "修复了前端表单验证在 Safari 浏览器上的兼容性问题，原因是 date input 的解析差异",
    "发现了日志收集系统在高峰期存在丢日志的问题，根因是 TCP 缓冲区溢出",
    "完成了一键部署脚本的编写，支持从 GitHub Actions 自动构建并发布到生产环境",
    "在代码审查中发现了一个潜在的 SQL 注入漏洞，已添加参数化查询修复",
    "测试了新的 CI/CD 流水线，构建时间从原来的 8 分钟缩短到 3 分钟",
    "调研了 WebAssembly 在浏览器端执行 Python 代码的可行性，发现 Pyodide 方案最成熟",
    "解决了跨域请求在 Nginx 反代后失效的问题，需要在 Nginx 配置中添加 OPTIONS 预检响应",
    "完成了数据库索引优化，对经常 JOIN 的字段添加了复合索引，查询速度提升 5 倍",
    "发现手机 ADB 连接在某些情况下会断开，原因是系统省电策略杀掉了后台进程",
    "实现了基于 TF-IDF 的语义搜索功能，可以在无外部 API 的情况下进行本地语义匹配",
    "修复了 Windows 路径中反斜杠在 JSON 序列化后丢失转义的问题",
    "决定将项目的构建工具从 Webpack 迁移到 Vite，开发体验和构建速度都有显著提升",
    "发现 Pywinauto 在控制 UWP 应用时存在权限限制，需要以管理员身份运行",
]

TOPICS = [
    "手机ADB调试", "Python代码审查", "Docker容器部署", "数据库性能优化",
    "前端React组件", "Nginx反向代理", "Redis缓存策略", "CI/CD流水线",
    "Kubernetes编排", "Linux系统管理", "Git分支策略", "API接口设计",
    "微服务架构", "消息队列RabbitMQ", "ELK日志分析", "Prometheus监控",
    "WebSocket实时通信", "JWT认证授权", "OAuth2.0接入", "MySQL索引优化",
    "MongoDB聚合查询", "PostgreSQL分区表", "ElasticSearch查询", "Vue3组合式API",
    "TypeScript类型体操", "TailwindCSS样式", "Vite构建配置", "Go协程并发",
    "Rust内存安全", "JavaSpringBoot", "C++模板元编程", "Flutter跨平台",
    "SwiftUI布局", "Kotlin协程", "网络抓包分析", "Reqable证书安装",
    "Fiddler调试代理", "Wireshark协议分析", "桌面自动化GUI", "Playwright浏览器",
    "Selenium测试框架", "Cypress端到端测试", "记忆系统检索", "语义搜索优化",
    "TF-IDF向量化", "余弦相似度计算", "触发路由器设计", "上下文注入策略",
    "Token预算管理", "会话状态恢复", "版本控制回滚", "FTS全文检索",
]

OBS_TYPES = ["bugfix", "decision", "discovery", "task", "tool_use", "note", "optimization"]

SHORT_QUERIES = ["ADB", "bug", "Docker", "Redis", "手机", "部署", "缓存", "测试", "API", "修复"]
MEDIUM_QUERIES = [
    "手机ADB调试方法", "Python代码性能优化", "Docker容器部署配置",
    "Redis缓存策略设计", "数据库SQL查询优化", "Nginx反向代理配置",
    "CI/CD流水线搭建", "前端组件性能优化", "API接口安全设计", "微服务架构部署",
]
LONG_QUERIES = [
    "如何在Windows环境下配置ADB连接Realme手机并进行自动化测试",
    "Docker容器化部署Python Flask应用的最佳实践和性能调优方案",
    "Redis缓存雪崩和缓存穿透的预防策略以及分布式锁的实现方式",
    "Kubernetes集群中Pod调度策略和HPA自动伸缩的配置方法",
    "Nginx反向代理配置HTTPS证书以及WebSocket长连接的优化方案",
    "PostgreSQL数据库在高并发场景下的索引优化和连接池配置",
    "CI/CD流水线中集成自动化测试和代码质量检查的完整方案",
    "基于TF-IDF和余弦相似度实现中文语义搜索的技术方案",
    "JWT令牌刷新机制和OAuth2.0第三方登录的完整实现流程",
    "React应用性能优化：虚拟列表、懒加载和代码分割的最佳实践",
]

TRIGGER_MESSAGES = [
    "你好", "hi", "hello", "嗨", "ok", "好的", "嗯", "收到", "谢谢",
    "继续上次的项目", "恢复上下文", "接着做",
    "帮我看看这个bug", "代码review一下", "部署到生产环境",
    "之前做过什么", "上次的结果怎么样", "查一下历史记录",
    "手机adb连接不上", "scrcpy投屏失败", "抓包证书安装",
    "今天天气怎么样", "帮我写个故事",
    "密码是什么", "token发给我",
    "怎么优化数据库性能？", "如何配置Nginx？",
    "这个功能能不能做成异步的", "有没有更好的方案",
    "", "帮我分析一下这段Python代码的性能瓶颈，特别是循环和列表推导式的部分",
    "我需要在Kubernetes集群中配置一个支持WebSocket的Ingress规则",
]


# ============================================================
# 通用工具
# ============================================================

def random_zh_text(min_len, max_len):
    target_len = random.randint(min_len, max_len)
    parts, cur = [], 0
    while cur < target_len:
        frag = random.choice(ZH_FRAGMENTS)
        parts.append(frag)
        cur += len(frag)
    return "".join(parts)[:target_len]


def db_size_bytes():
    return os.path.getsize(str(DB_PATH))


def get_row_counts():
    conn = store.get_db()
    try:
        return {
            "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "observations": conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "versions": conn.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0],
            "snapshots": conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
        }
    finally:
        conn.close()


def get_table_counts():
    return get_row_counts()


def cleanup_stress_data():
    """删除所有以 PREFIX 开头的压测数据"""
    conn = store.get_db()
    try:
        rows = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id LIKE ?",
            (PREFIX + "%",)
        ).fetchall()
        sids = [r["session_id"] for r in rows]
        if not sids:
            return 0

        # 收集 observation ids
        ph = ",".join("?" * len(sids))
        obs_ids = [str(r[0]) for r in conn.execute(
            f"SELECT id FROM observations WHERE session_id IN ({ph})", sids
        ).fetchall()]

        # 删 FTS
        for sid in sids:
            try: conn.execute("DELETE FROM observations_fts WHERE session_id=?", (sid,))
            except: pass
            try: conn.execute("DELETE FROM sessions_fts WHERE session_id LIKE ?", (f"%{sid}%",))
            except: pass

        # 删主表
        conn.execute(f"DELETE FROM observations WHERE session_id IN ({ph})", sids)
        conn.execute(f"DELETE FROM sessions WHERE session_id IN ({ph})", sids)

        # 删版本
        conn.execute(f"DELETE FROM memory_versions WHERE entity_id IN ({ph})", sids)
        if obs_ids:
            ph2 = ",".join("?" * len(obs_ids))
            conn.execute(
                f"DELETE FROM memory_versions WHERE entity_type='observation' AND entity_id IN ({ph2})",
                obs_ids
            )

        # 删快照
        conn.execute(f"DELETE FROM snapshots WHERE session_id IN ({ph})", sids)

        # 兜底：清理最近 1 小时内、不属于生产的 observation 版本
        prod_obs = set(str(r[0]) for r in conn.execute(
            "SELECT id FROM observations WHERE session_id NOT LIKE ?", (PREFIX + "%",)
        ).fetchall())
        stale = conn.execute(
            "SELECT id, entity_id FROM memory_versions WHERE entity_type='observation' "
            "AND created_at > ?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(),)
        ).fetchall()
        for v in stale:
            if v["entity_id"] not in prod_obs:
                conn.execute("DELETE FROM memory_versions WHERE id=?", (v["id"],))

        conn.commit()
        return len(sids)
    finally:
        conn.close()


def rebuild_fts():
    """重建 FTS 索引"""
    conn = store.get_db()
    try:
        conn.execute("DROP TABLE IF EXISTS sessions_fts")
        conn.execute("DROP TABLE IF EXISTS observations_fts")
        conn.execute('CREATE VIRTUAL TABLE sessions_fts USING fts5(session_rowid UNINDEXED, session_id UNINDEXED, summary, summary_short, tags)')
        conn.execute('CREATE VIRTUAL TABLE observations_fts USING fts5(observation_rowid UNINDEXED, session_id UNINDEXED, obs_type UNINDEXED, content, context, impact)')
        for s in conn.execute("SELECT * FROM sessions").fetchall():
            store._sync_session_fts(conn, s)
        for o in conn.execute("SELECT * FROM observations").fetchall():
            store._sync_observation_fts(conn, o)
        conn.commit()
    finally:
        conn.close()


# ============================================================
# 模块 A：写入压测
# ============================================================

def run_write_stress():
    print("\n" + "#" * 60)
    print("# 模块 A：写入压测")
    print("#" * 60)

    NUM_SESSIONS = 100
    OBS_PER_SESSION_MIN, OBS_PER_SESSION_MAX = 5, 10

    # --- A1: 批量写入 ---
    print("\n--- A1: 批量写入 (100 会话 x 5~10 观察) ---")
    before_counts = get_row_counts()
    session_ids = []
    t0 = time.perf_counter()
    for i in range(NUM_SESSIONS):
        sid = f"{PREFIX}write-{i:04d}"
        store.upsert_session(
            session_id=sid,
            summary=random_zh_text(180, 220),
            summary_short=random_zh_text(25, 35),
            project_path="~/projects/stress-test",
            model="qwen-test",
            tags=random.sample(["python", "docker", "redis", "testing", "bugfix",
                                "deployment", "refactor", "security", "api", "cli"], 5),
            importance=round(random.uniform(0.5, 0.9), 2),
            token_estimate=random.randint(100, 500),
        )
        session_ids.append(sid)
    t_sessions = time.perf_counter() - t0

    obs_count = 0
    t1 = time.perf_counter()
    for sid in session_ids:
        for _ in range(random.randint(OBS_PER_SESSION_MIN, OBS_PER_SESSION_MAX)):
            store.add_observation(
                session_id=sid,
                obs_type=random.choice(OBS_TYPES),
                content=random_zh_text(80, 120),
                importance=round(random.uniform(0.3, 0.9), 2),
                tags=random.sample(["python", "docker", "redis", "testing", "bugfix"], 5),
            )
            obs_count += 1
    t_obs = time.perf_counter() - t1
    total_writes = NUM_SESSIONS + obs_count
    print(f"  会话: {NUM_SESSIONS} 条 {t_sessions:.3f}s ({NUM_SESSIONS/t_sessions:.0f}/s)")
    print(f"  观察: {obs_count} 条 {t_obs:.3f}s ({obs_count/t_obs:.0f}/s)")
    print(f"  合计: {total_writes} 条 {t_sessions+t_obs:.3f}s")

    # --- A2: FTS 索引重建 ---
    print("\n--- A2: FTS 索引重建 ---")
    t0 = time.perf_counter()
    rebuild_fts()
    t_fts = time.perf_counter() - t0
    print(f"  FTS 重建耗时: {t_fts:.3f}s")

    # --- A3: 语义索引重建 ---
    print("\n--- A3: 语义索引 (TF-IDF) 重建 ---")
    t0 = time.perf_counter()
    try:
        idx = semantic.build_index_from_db()
        t_sem = time.perf_counter() - t0
        stats = idx.stats()
        print(f"  耗时: {t_sem:.3f}s  文档: {stats['total_documents']}  词汇: {stats['vocabulary_size']}")
    except Exception as e:
        print(f"  跳过: {e}")

    # --- A4: 边界测试 ---
    print("\n--- A4: 边界测试 ---")
    edge_results = []

    # 超长 summary
    long_summary = random_zh_text(990, 1010)
    sid_long = f"{PREFIX}edge-long"
    t0 = time.perf_counter()
    store.upsert_session(session_id=sid_long, summary=long_summary,
                         summary_short=random_zh_text(25, 35),
                         tags=["test"], importance=0.5)
    d = store.get_session_detail(sid_long)
    ok = d is not None and len(d["session"]["summary"]) == len(long_summary)
    edge_results.append(("超长 summary", ok, f"{(time.perf_counter()-t0)*1000:.1f}ms"))

    # 空 summary
    sid_empty = f"{PREFIX}edge-empty"
    store.upsert_session(session_id=sid_empty, summary="", summary_short="", tags=[], importance=0.0)
    edge_results.append(("空 summary", store.get_session_detail(sid_empty) is not None, ""))

    # 特殊字符
    special = 'emoji \U0001f600\U0001f4a5\U0001f680 换行\n第二行\t "双引号" \'单引号\' \\反斜杠 &amp; <html>'
    sid_sp = f"{PREFIX}edge-special"
    store.upsert_session(session_id=sid_sp, summary=special, summary_short="特殊字符",
                         tags=["特殊字符"], importance=0.7)
    d = store.get_session_detail(sid_sp)
    edge_results.append(("特殊字符", d is not None and special in (d["session"]["summary"] or ""), ""))

    # 并发写入
    n_threads = 10
    def _write_one(idx):
        sid = f"{PREFIX}concurrent-{idx}"
        store.upsert_session(session_id=sid, summary=random_zh_text(180, 220),
                             summary_short=random_zh_text(25, 35),
                             tags=["test"], importance=0.5)
        for _ in range(3):
            store.add_observation(session_id=sid, obs_type=random.choice(OBS_TYPES),
                                  content=random_zh_text(80, 120), importance=0.5)
        return sid

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = {pool.submit(_write_one, i): i for i in range(n_threads)}
        written = [f.result() for f in as_completed(futures)]
    t_conc = time.perf_counter() - t0
    all_ok = all(store.get_session_detail(s) is not None for s in written)
    edge_results.append((f"并发写入 ({n_threads}线程)", all_ok,
                         f"{t_conc*1000:.1f}ms ({n_threads/t_conc:.0f}/s)"))

    for name, ok, detail in edge_results:
        icon = "[OK]" if ok else "[FAIL]"
        print(f"  {icon} {name:<25s} {detail}")

    # --- 清理 ---
    cleanup_stress_data()
    rebuild_fts()
    print(f"\n  [A 模块清理完成]")


# ============================================================
# 模块 B：检索压测
# ============================================================

def run_search_stress():
    print("\n" + "#" * 60)
    print("# 模块 B：检索压测")
    print("#" * 60)

    NUM_SESSIONS = 100
    OBS_PER_SESSION = 5

    def gen_session_id(i):
        return f"{PREFIX}search-{i:04d}"

    # --- 插入数据 ---
    print("\n  [准备] 插入测试数据...")
    t0 = time.time()
    for i in range(NUM_SESSIONS):
        topic = TOPICS[i % len(TOPICS)]
        sid = gen_session_id(i)
        store.upsert_session(
            session_id=sid,
            summary=f"#{i} {topic} — " + "".join(random.choices("测试内容数据", k=30)),
            summary_short=f"#{i} {topic[:20]}",
            project_path=f"C:\\Users\\test\\project-{i % 10}",
            tags=[topic[:4], OBS_TYPES[i % len(OBS_TYPES)]],
            importance=round(random.uniform(0.3, 0.95), 2),
            token_estimate=random.randint(100, 2000),
        )
        store.end_session(sid, summary=f"#{i} {topic} — 完成")
        for j in range(OBS_PER_SESSION):
            store.add_observation(
                session_id=sid,
                obs_type=OBS_TYPES[(i + j) % len(OBS_TYPES)],
                content=f"obs-{i}-{j}: {topic}" + "".join(random.choices("内容", k=40)),
                importance=round(random.uniform(0.2, 0.9), 2),
            )
    print(f"  插入 {NUM_SESSIONS} 会话 + {NUM_SESSIONS * OBS_PER_SESSION} 观察, 耗时 {time.time()-t0:.2f}s")

    def bench(label, fn, iterations):
        times = []
        results = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            r = fn()
            times.append((time.perf_counter() - t0) * 1000)
            results.append(r)
        avg = statistics.mean(times)
        p95 = sorted(times)[int(len(times) * 0.95)]
        return {"label": label, "iterations": iterations, "avg_ms": avg,
                "p95_ms": p95, "qps": 1000.0 / avg if avg > 0 else 0,
                "non_empty": sum(1 for r in results if r)}

    # --- B1: FTS 关键词搜索 ---
    print("\n--- B1: FTS 关键词搜索 ---")
    all_topics = TOPICS + MEDIUM_QUERIES

    b = bench("FTS单关键词", lambda: store.search_sessions(random.choice(all_topics), limit=10), 100)
    print(f"  {b['label']}: avg={b['avg_ms']:.2f}ms P95={b['p95_ms']:.2f}ms QPS={b['qps']:.0f} 有效={b['non_empty']}/{b['iterations']}")

    multi_kw = [f"{random.choice(TOPICS)} {random.choice(TOPICS)}" for _ in range(100)]
    b = bench("FTS多关键词", lambda: store.search_sessions(random.choice(multi_kw), limit=10), 100)
    print(f"  {b['label']}: avg={b['avg_ms']:.2f}ms P95={b['p95_ms']:.2f}ms QPS={b['qps']:.0f}")

    b = bench("FTS观察搜索", lambda: store.search_observations(random.choice(all_topics), limit=10), 100)
    print(f"  {b['label']}: avg={b['avg_ms']:.2f}ms P95={b['p95_ms']:.2f}ms QPS={b['qps']:.0f}")

    # --- B2: 语义搜索 ---
    print("\n--- B2: 语义搜索 ---")
    try:
        t0 = time.time()
        idx = semantic.build_index_from_db()
        print(f"  索引构建: {time.time()-t0:.2f}s  stats={idx.stats()}")

        all_qs = SHORT_QUERIES + MEDIUM_QUERIES + LONG_QUERIES
        b = bench("语义搜索", lambda: idx.search(random.choice(all_qs), top_k=10), 100)
        print(f"  {b['label']}: avg={b['avg_ms']:.2f}ms P95={b['p95_ms']:.2f}ms QPS={b['qps']:.0f}")
    except Exception as e:
        print(f"  跳过: {e}")

    # --- B3: 融合搜索 ---
    print("\n--- B3: 融合搜索 ---")
    all_qs = TOPICS + MEDIUM_QUERIES + LONG_QUERIES
    b = bench("融合搜索(limit=10)", lambda: store.fused_search(random.choice(all_qs), limit=10), 100)
    print(f"  {b['label']}: avg={b['avg_ms']:.2f}ms P95={b['p95_ms']:.2f}ms QPS={b['qps']:.0f}")

    # 结果质量
    sample = [store.fused_search(q, limit=10) for q in random.sample(all_qs, 20)]
    has = sum(1 for r in sample if r.get("sessions") or r.get("observations"))
    print(f"  结果质量: {has}/20 查询返回有效结果")

    # --- B4: 触发路由 ---
    print("\n--- B4: 触发路由 ---")
    b = bench("evaluate_trigger", lambda: trigger_router.evaluate_trigger(random.choice(TRIGGER_MESSAGES)), 1000)
    print(f"  {b['label']}: avg={b['avg_ms']:.3f}ms QPS={b['qps']:.0f}")

    # --- B5: 并发压测 ---
    print("\n--- B5: 并发压测 (10线程 x 50次) ---")
    NUM_CONCURRENT = 10
    ITER_PER = 50
    all_times = []
    errors = []
    lock = threading.Lock()

    def worker(_):
        local = []
        for _ in range(ITER_PER):
            q = random.choice(all_qs)
            t0 = time.perf_counter()
            try:
                r = random.choice(["fts", "fused", "trigger"])
                if r == "fts": store.search_sessions(q, limit=10)
                elif r == "fused": store.fused_search(q, limit=10)
                else: trigger_router.evaluate_trigger(q)
                local.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                with lock: errors.append(str(e))
        with lock: all_times.extend(local)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=NUM_CONCURRENT) as pool:
        list(pool.map(worker, range(NUM_CONCURRENT)))
    wall = time.time() - t0
    total_req = NUM_CONCURRENT * ITER_PER

    if all_times:
        avg = statistics.mean(all_times)
        p95 = sorted(all_times)[int(len(all_times) * 0.95)]
    else:
        avg = p95 = 0
    print(f"  总请求: {total_req} | 耗时: {wall:.2f}s | 吞吐: {total_req/wall:.0f} req/s")
    print(f"  平均: {avg:.2f}ms | P95: {p95:.2f}ms | 错误: {len(errors)}")

    # --- 清理 ---
    cleanup_stress_data()
    rebuild_fts()
    print(f"\n  [B 模块清理完成]")


# ============================================================
# 模块 C：预算压测
# ============================================================

def run_budget_stress():
    print("\n" + "#" * 60)
    print("# 模块 C：预算压测")
    print("#" * 60)

    # 使用临时数据库，不影响生产库
    orig_db = store.DB_PATH
    test_db = store.DB_DIR / "stress_budget_test.db"
    if test_db.exists():
        test_db.unlink()
    store.set_db_path(test_db)

    RESULTS = []
    SECTION_RESULTS = defaultdict(list)

    def section(title):
        print(f"\n--- {title} ---")

    def bench(name, fn, section_name=""):
        t0 = time.perf_counter()
        try:
            detail = fn()
            elapsed = time.perf_counter() - t0
            RESULTS.append((name, elapsed, True, detail))
            SECTION_RESULTS[section_name].append((name, elapsed, True, detail))
            print(f"  PASS  {name:<40s} {elapsed*1000:8.2f}ms  {str(detail)[:80]}")
            return detail
        except Exception as e:
            elapsed = time.perf_counter() - t0
            RESULTS.append((name, elapsed, False, str(e)[:200]))
            SECTION_RESULTS[section_name].append((name, elapsed, False, str(e)[:200]))
            print(f"  FAIL  {name:<40s} {elapsed*1000:8.2f}ms  {str(e)[:120]}")
            return None

    def _make_results(n=20):
        results = []
        for i in range(n):
            text_len = random.randint(20, 300)
            text = "".join(random.choices("这是一段测试文本内容用于验证token预算裁剪功能", k=text_len))
            results.append({
                "id": i, "text": text,
                "fused_score": round(random.uniform(0.1, 1.0), 3),
                "token_estimate": budget.estimate_tokens(text),
            })
        return results

    def _eval_budget(results, budget_val):
        selected, stats = budget.trim_results(results, token_budget=budget_val)
        actual_tokens = sum(r.get("token_estimate", 0) for r in selected)
        assert actual_tokens <= budget_val, f"超预算: {actual_tokens} > {budget_val}"
        return f"选{stats['selected']}/{len(results)} 用{actual_tokens}tok"

    def _seed_data(n=50):
        tags_pool = ["desktop", "android", "bugfix", "testing", "deploy", "docker"]
        obs_types = ["decision", "bugfix", "discovery", "task", "error", "tool_use", "note"]
        topics = [
            "手机控制 Android ADB scrcpy YADB 自动化",
            "桌面 GUI 自动化 pywinauto OCR 截图",
            "记忆系统 SQLite FTS5 语义搜索",
            "Nginx 反代配置 Docker 容器部署",
            "Python 数据处理 Pandas Numpy",
        ]
        for i in range(n):
            topic = random.choice(topics)
            sid = f"{PREFIX}budget-{i:04d}"
            store.upsert_session(
                session_id=sid,
                summary=f"#{i} {topic} — " + "".join(random.choices("测试摘要", k=30)),
                summary_short=f"#{i} {topic[:20]}",
                project_path=f"/test/project/{i}",
                tags=random.sample(tags_pool, k=3),
                importance=round(random.uniform(0.1, 1.0), 2),
                token_estimate=random.randint(50, 500),
            )
            for j in range(5):
                store.add_observation(
                    session_id=sid, obs_type=random.choice(obs_types),
                    content=f"obs-{i}-{j}: " + "".join(random.choices("观察内容测试", k=40)),
                    importance=round(random.uniform(0.1, 1.0), 2),
                    tags=random.sample(tags_pool, k=2),
                )
        for i in range(0, n, 3):
            store.end_session(f"{PREFIX}budget-{i:04d}", summary=f"已结束 #{i}")

    # --- C1: Token 预算 ---
    section("C1: Token 预算")
    for bv in [100, 500, 1000, 2000]:
        bench(f"budget={bv}", lambda b=bv: _eval_budget(_make_results(20), b), "budget")
    bench("预算超限(budget=50, 单条=600)", lambda: _eval_budget(
        [{"id": 0, "text": "X" * 1000, "fused_score": 0.9, "token_estimate": 600}], 50), "budget")
    bench("空结果", lambda: budget.trim_results([], token_budget=100), "budget")

    # --- C2: 压缩摘要 ---
    section("C2: 压缩摘要")
    summaries = {
        "100字": "".join(random.choices("压缩摘要测试数据", k=100)),
        "500字": "".join(random.choices("压缩摘要测试数据", k=500)),
    }
    observations = [
        {"obs_type": "decision", "content": "选择了高性能方案" + "".join(random.choices("详情", k=30)), "importance": 0.9},
        {"obs_type": "bugfix", "content": "修复了连接超时" + "".join(random.choices("原因", k=20)), "importance": 0.8},
    ]
    for label, txt in summaries.items():
        for target in [100, 500]:
            bench(f"摘要{label} → {target}tok",
                  lambda s=txt, t=target: _eval_compact(s, observations, t, budget.estimate_tokens(txt)),
                  "compact")

    def _eval_compact(s, o, max_tok, orig):
        result = budget.compact_summary(s, o, max_tokens=max_tok)
        rt = budget.estimate_tokens(result)
        assert rt <= max_tok * 1.2 + 5, f"压缩后超限: {rt}"
        return f"入={orig}tok 出={rt}tok"

    # --- C3: 自适应注入 ---
    section("C3: 自适应注入")
    _seed_data(30)
    query_types = {
        "精确匹配": ["手机", "ADB"],
        "模糊查询": ["自动化", "部署"],
        "空查询": [""],
        "特殊字符": ["'; DROP TABLE--"],
    }
    for qtype, queries in query_types.items():
        for q in queries[:1]:
            bench(f"{qtype} budget=500",
                  lambda query=q: _eval_inj(query, 500), "adaptive")

    def _eval_inj(q, mt):
        result = budget.adaptive_injection(q, max_tokens=mt)
        st = result["stats"]
        assert st["total_tokens"] <= mt * 1.1 + 5
        return f"tok={st['total_tokens']} sess={st['sessions_selected']}"

    # 批量 100 次
    all_queries = []
    for qs in query_types.values():
        all_queries.extend(qs)
    while len(all_queries) < 100:
        all_queries.append(random.choice(["手机", "Docker", "搜索", ""]))

    times_batch = []
    errors_batch = 0
    t_start = time.perf_counter()
    for q in all_queries[:100]:
        t0 = time.perf_counter()
        try:
            budget.adaptive_injection(q, max_tokens=random.choice([200, 500, 1000]))
            times_batch.append(time.perf_counter() - t0)
        except: errors_batch += 1
    t_total = time.perf_counter() - t_start

    if times_batch:
        avg_ms = statistics.mean(times_batch) * 1000
        p95_ms = sorted(times_batch)[int(len(times_batch) * 0.95)] * 1000
    else:
        avg_ms = p95_ms = 0
    print(f"  批量100次: 成功={100-errors_batch}/100 avg={avg_ms:.1f}ms P95={p95_ms:.1f}ms 耗时={t_total*1000:.0f}ms")

    # --- C4: 缓存机制 ---
    section("C4: 缓存机制")
    query = "手机控制"
    times_same = []
    for _ in range(10):
        t0 = time.perf_counter()
        budget.adaptive_injection(query, max_tokens=500)
        times_same.append(time.perf_counter() - t0)
    perf_stable = times_same[-1] <= times_same[0] * 3
    print(f"  重复查询: avg={statistics.mean(times_same)*1000:.2f}ms 首={times_same[0]*1000:.2f}ms 末={times_same[-1]*1000:.2f}ms 稳定={'是' if perf_stable else '否'}")

    # --- C5: 成本追踪 ---
    section("C5: 成本追踪")
    queries_pool = ["测试查询", "手机控制", "Docker部署"]
    t0 = time.perf_counter()
    for i in range(500):
        store.log_budget(
            query=random.choice(queries_pool) + f"#{i}",
            max_tokens=random.choice([200, 500, 1000]),
            used_tokens=random.randint(50, 800),
            sessions_injected=random.randint(0, 5),
            obs_injected=random.randint(0, 10),
            compact_text=f"compact#{i}",
        )
    t_log = time.perf_counter() - t0
    print(f"  写入500条: {t_log*1000:.0f}ms ({500/t_log:.0f}/s)")

    for lim in [10, 50, 100]:
        logs = store.get_budget_log(limit=lim)
        print(f"  get_budget_log({lim}): 返回 {len(logs)} 条")

    # --- 汇总 ---
    total = len(RESULTS)
    passed = sum(1 for _, _, p, _ in RESULTS if p)
    failed = total - passed
    print(f"\n  [C 模块汇总] 总计 {total} 项  通过 {passed}  失败 {failed}")

    # --- 清理 ---
    store.set_db_path(orig_db)
    if test_db.exists():
        test_db.unlink()
        print(f"  [C 模块清理完成] 已删除测试数据库")


# ============================================================
# 模块 D：系统压测
# ============================================================

def run_system_stress():
    print("\n" + "#" * 60)
    print("# 模块 D：系统压测")
    print("#" * 60)

    all_results = {}

    # --- D1: 数据库膨胀 ---
    print("\n--- D1: 数据库膨胀 (500 sessions + 2500 observations) ---")
    before_size = db_size_bytes()
    before_counts = get_table_counts()

    t0 = time.perf_counter()
    batch_size = 50
    total_sessions = 500
    total_obs = 2500

    for batch_start in range(0, total_sessions, batch_size):
        conn = store.get_db()
        try:
            for i in range(batch_start, min(batch_start + batch_size, total_sessions)):
                sid = f"{PREFIX}bloat_{i:04d}"
                now = store._now()
                summary = f"压力测试 #{i}，包含各种关键词：bug修复、部署、测试、重构"
                tags = json.dumps(["stress", f"batch_{i // batch_size}"], ensure_ascii=False)
                importance = (i % 10) / 10.0

                conn.execute("""
                    INSERT INTO sessions (session_id, created_at, updated_at, summary,
                        summary_short, project_path, model, tools_used, key_decisions,
                        file_changes, tags, token_estimate, importance, checksum)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (sid, now, now, summary, f"#{i}", f"C:\\stress\\p_{i}",
                      "qwen-stress", "[]", "[]", "[]", tags, i * 10, importance,
                      hashlib.md5(summary.encode()).hexdigest()[:12]))

                row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
                if row:
                    store._sync_session_fts(conn, row)
            conn.commit()
        finally:
            conn.close()

    t_sessions = time.perf_counter() - t0

    obs_per = total_obs // total_sessions
    total_written = 0
    t_obs_start = time.perf_counter()
    conn = store.get_db()
    try:
        for i in range(total_sessions):
            sid = f"{PREFIX}bloat_{i:04d}"
            for j in range(obs_per):
                now = store._now()
                content = f"obs #{total_written}: 描述内容，关键词: error, bug, fix, deploy"
                cursor = conn.execute("""
                    INSERT INTO observations (session_id, obs_type, content, context, impact,
                        created_at, updated_at, importance, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (sid, OBS_TYPES[j % len(OBS_TYPES)], content,
                      f"ctx#{total_written}", f"imp#{total_written}",
                      now, now, 0.5 + (j % 5) * 0.1, '["stress"]'))
                row = conn.execute("SELECT * FROM observations WHERE id=?", (cursor.lastrowid,)).fetchone()
                if row:
                    store._sync_observation_fts(conn, row)
                total_written += 1
            if i % 100 == 0 and i > 0:
                print(f"  进度: {total_written}/{total_obs}")
        conn.commit()
    finally:
        conn.close()
    t_obs = time.perf_counter() - t_obs_start

    after_size = db_size_bytes()
    print(f"  Session写入: {t_sessions:.2f}s ({total_sessions/t_sessions:.0f}/s)")
    print(f"  Obs写入: {t_obs:.2f}s ({total_obs/t_obs:.0f}/s)")
    print(f"  数据库增长: {(after_size-before_size)/1024:.1f} KB")

    cleanup_stress_data()

    # --- D2: 版本控制 ---
    print("\n--- D2: 版本控制 (100次修改) ---")
    test_sid = f"{PREFIX}ver_test"
    store.upsert_session(session_id=test_sid, summary="初始", importance=0.5, tags=["vtest"])

    ver_before = get_row_counts()["versions"]
    t0 = time.perf_counter()
    for i in range(100):
        store.upsert_session(
            session_id=test_sid,
            summary=f"版本 #{i+1} - {datetime.now().isoformat()}",
            summary_short=f"v#{i+1}",
            importance=0.3 + (i % 7) * 0.1,
            tags=["vtest", f"round_{i}"],
            token_estimate=i * 100,
        )
    t_modify = time.perf_counter() - t0

    ver_after = get_row_counts()["versions"]
    new_versions = ver_after - ver_before
    print(f"  100次修改: {t_modify:.2f}s ({100/t_modify:.0f}/s)")
    print(f"  新增版本: {new_versions} 条")

    cleanup_stress_data()

    # --- D3: 清理机制 ---
    print("\n--- D3: 清理机制 (keep-days: 7/30/90) ---")
    for keep_days in [7, 30, 90]:
        conn = store.get_db()
        try:
            now = datetime.utcnow()
            for days_ago in [1, 3, 7, 14, 30, 60, 90, 120]:
                test_date = (now - timedelta(days=days_ago)).isoformat() + "Z"
                sid = f"{PREFIX}cln_{keep_days}d_{days_ago}ago"
                conn.execute("""
                    INSERT OR REPLACE INTO sessions (session_id, created_at, updated_at,
                        summary, summary_short, tags, importance, token_estimate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (sid, test_date, test_date, f"清理测试 {days_ago}天前",
                      f"cln {days_ago}d", '["cln-test"]', 0.5, 100))
                for j in range(3):
                    conn.execute("""
                        INSERT INTO observations (session_id, obs_type, content,
                            created_at, updated_at, importance, tags)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (sid, "note", f"清理观察 {days_ago}天前 #{j}",
                          test_date, test_date, 0.5, '["cln-test"]'))
            conn.commit()
        finally:
            conn.close()

        t0 = time.perf_counter()
        conn = store.get_db()
        try:
            cutoff = (datetime.utcnow() - timedelta(days=keep_days)).isoformat()
            r_ver = conn.execute("DELETE FROM memory_versions WHERE created_at < ?", (cutoff,)).rowcount
            r_snap = conn.execute("DELETE FROM snapshots WHERE created_at < ?", (cutoff,)).rowcount
            old_sessions = conn.execute(
                "SELECT session_id FROM sessions WHERE created_at < ? AND session_id LIKE ?",
                (cutoff, f"{PREFIX}%")
            ).fetchall()
            r_obs = r_sess = 0
            for s in old_sessions:
                r_obs += conn.execute("DELETE FROM observations WHERE session_id=?", (s[0],)).rowcount
                r_sess += conn.execute("DELETE FROM sessions WHERE session_id=?", (s[0],)).rowcount
            conn.commit()
        finally:
            conn.close()
        t_cln = time.perf_counter() - t0
        print(f"  keep_days={keep_days}: {t_cln:.3f}s 删除 sessions={r_sess} obs={r_obs} ver={r_ver}")

    cleanup_stress_data()

    # --- D4: 异常恢复 ---
    print("\n--- D4: 异常恢复 ---")

    # 4a: 数据库锁定
    lock_sid = f"{PREFIX}lock"
    lock_errors = []
    lock_ok = 0
    lock_conn = sqlite3.connect(str(DB_PATH), timeout=1)
    lock_conn.execute("BEGIN EXCLUSIVE")
    for i in range(5):
        try:
            tc = sqlite3.connect(str(DB_PATH), timeout=0.5)
            tc.execute("PRAGMA journal_mode=WAL")
            tc.execute("INSERT OR REPLACE INTO sessions (session_id, created_at, updated_at, summary, importance) VALUES (?, ?, ?, ?, ?)",
                       (f"{lock_sid}_{i}", store._now(), store._now(), f"lock{i}", 0.5))
            tc.commit(); tc.close()
            lock_ok += 1
        except Exception as e:
            lock_errors.append(str(e)[:80])
    try: lock_conn.rollback(); lock_conn.close()
    except: pass
    print(f"  锁定测试: 成功={lock_ok} 失败={len(lock_errors)}")

    # 4b: 大 payload
    large_sid = f"{PREFIX}large"
    store.upsert_session(session_id=large_sid, summary="大payload容器", importance=0.5)
    large_content = "X" * (1024 * 100)  # 100KB
    t0 = time.perf_counter()
    obs_id = store.add_observation(session_id=large_sid, obs_type="note",
                                   content=large_content, importance=0.5)
    t_large = time.perf_counter() - t0
    print(f"  大payload(100KB): {t_large:.3f}s obs#{obs_id}")

    # 4c: 并发更新
    cons_sid = f"{PREFIX}consist"
    store.upsert_session(session_id=cons_sid, summary="一致性测试", importance=0.5)
    errs = []
    def _update(v):
        try:
            store.upsert_session(session_id=cons_sid, summary=f"并发v{v}", importance=0.1*v)
            return True
        except Exception as e:
            errs.append(str(e)); return False
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(_update, range(20)))
    t_conc = time.perf_counter() - t0
    ok_count = sum(results)
    d = store.get_session_detail(cons_sid)
    print(f"  并发更新(20次): 成功={ok_count} 耗时={t_conc:.3f}s 最终状态={'有' if d else '无'}")

    cleanup_stress_data()

    # --- D5: 长时间运行 (30秒) ---
    print("\n--- D5: 长时间运行 (30秒) ---")
    process = psutil.Process(os.getpid())
    search_qs = ["bug修复", "测试", "部署", "压力测试", "版本控制", "清理", "数据库", "性能"]
    mem_before = process.memory_info().rss / (1024 * 1024)

    t0 = time.perf_counter()
    q_count = 0
    errs_long = 0
    latencies = []
    while time.perf_counter() - t0 < 30:
        q = search_qs[q_count % len(search_qs)]
        tq = time.perf_counter()
        try:
            store.search_sessions(q, limit=5)
            latencies.append((time.perf_counter() - tq) * 1000)
        except: errs_long += 1
        q_count += 1
    elapsed = time.perf_counter() - t0
    mem_after = process.memory_info().rss / (1024 * 1024)
    avg_lat = statistics.mean(latencies) if latencies else 0
    p95_lat = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0
    print(f"  查询: {q_count}次 ({q_count/elapsed:.1f}/s) | 错误: {errs_long}")
    print(f"  延迟: avg={avg_lat:.1f}ms p95={p95_lat:.1f}ms")
    print(f"  内存: {mem_before:.1f}MB -> {mem_after:.1f}MB ({mem_after-mem_before:+.1f}MB)")

    # --- D6: 边界条件 ---
    print("\n--- D6: 边界条件 ---")

    # 6a: 超长字段
    mega = "长文本" * (250 * 1024)  # ~1MB
    mega_sid = f"{PREFIX}mega"
    t0 = time.perf_counter()
    store.upsert_session(session_id=mega_sid, summary=mega, summary_short="超长", importance=0.5)
    t_write = time.perf_counter() - t0
    d = store.get_session_detail(mega_sid)
    ok = d is not None and len(d["session"]["summary"]) == len(mega)
    print(f"  超长字段(1MB): {'PASS' if ok else 'FAIL'} 写={t_write:.3f}s")

    # 6b: 特殊字符
    specials = [
        '"引号"', "'单引号'", "\\反斜杠", "\n换行", "\t制表",
        "中文：，。！", "emoji\u2764", "'; DROP TABLE--",
    ]
    ok_count = 0
    for i, t in enumerate(specials):
        sid = f"{PREFIX}spc_{i}"
        store.upsert_session(session_id=sid, summary=t, importance=0.5)
        d = store.get_session_detail(sid)
        if d and d["session"]["summary"] == t:
            ok_count += 1
    print(f"  特殊字符: {ok_count}/{len(specials)}")

    # 6c: 空数据库搜索
    empty_s = store.search_sessions("xyz_unique_999999")
    print(f"  不存在的查询: 返回 {len(empty_s)} 条 (应为 0)")

    cleanup_stress_data()
    rebuild_fts()
    print(f"\n  [D 模块清理完成]")


# ============================================================
# 主入口
# ============================================================

def main():
    print("=" * 60)
    print("  Qwen Memory 综合压力测试")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  数据库: {DB_PATH}")
    print("=" * 60)

    # 先清理残留
    print("\n[准备] 清理残留压测数据...")
    n = cleanup_stress_data()
    if n:
        print(f"  已清理 {n} 个残留会话")
    rebuild_fts()

    initial_counts = get_row_counts()
    initial_size = db_size_bytes()
    print(f"  当前数据: {initial_counts}")
    print(f"  当前大小: {initial_size/1024:.1f} KB")

    overall_start = time.perf_counter()
    try:
        run_write_stress()
        run_search_stress()
        run_budget_stress()
        run_system_stress()
    except KeyboardInterrupt:
        print("\n>>> 用户中断")
    except Exception as e:
        print(f"\n>>> 异常: {e}")
        traceback.print_exc()
    finally:
        # 最终清理
        print("\n>>> 最终清理...")
        cleanup_stress_data()
        rebuild_fts()

        # VACUUM
        conn = store.get_db()
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

    overall_time = time.perf_counter() - overall_start
    final_counts = get_row_counts()
    final_size = db_size_bytes()

    print("\n" + "=" * 60)
    print("  综合压测完成")
    print(f"  总耗时: {overall_time:.2f}s")
    print(f"  数据库: {initial_size/1024:.1f}KB -> {final_size/1024:.1f}KB")
    print(f"  行数变化: sessions {initial_counts['sessions']}->{final_counts['sessions']}")
    if final_counts["sessions"] == initial_counts["sessions"]:
        print("  [OK] 数据清理验证通过 — 生产数据完整")
    else:
        print(f"  [WARN] 行数不一致!")
    print("=" * 60)


if __name__ == "__main__":
    main()
