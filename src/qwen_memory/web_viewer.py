"""
Qwen Memory Web Viewer — 浏览器查看/搜索历史记忆
启动: py web_viewer.py [--port 37777]
访问: http://localhost:37777
"""
import http.server
import json
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store

PORT = 37777


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Qwen Memory</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { color: #58a6ff; margin-bottom: 20px; font-size: 24px; }
h2 { color: #8b949e; margin: 20px 0 10px; font-size: 16px; }
.search-box { width: 100%; padding: 12px 16px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; color: #c9d1d9; font-size: 16px; margin-bottom: 20px; }
.search-box:focus { border-color: #58a6ff; outline: none; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 12px; cursor: pointer; transition: border-color 0.2s; }
.card:hover { border-color: #58a6ff; }
.card-title { font-weight: 600; color: #f0f6fc; margin-bottom: 4px; }
.card-meta { font-size: 13px; color: #8b949e; margin-bottom: 8px; }
.card-summary { font-size: 14px; color: #c9d1d9; line-height: 1.5; }
.tag { display: inline-block; background: #1f6feb33; color: #58a6ff; padding: 2px 8px; border-radius: 12px; font-size: 12px; margin-right: 4px; }
.tag-type { background: #238636; color: #3fb950; }
.tag-important { background: #9e6a03; color: #d29922; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
.stat-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; text-align: center; }
.stat-num { font-size: 28px; font-weight: 700; color: #58a6ff; }
.stat-label { font-size: 13px; color: #8b949e; margin-top: 4px; }
.obs-list { margin-top: 10px; }
.obs-item { padding: 8px 12px; border-left: 3px solid #30363d; margin-bottom: 8px; background: #0d1117; border-radius: 0 6px 6px 0; }
.obs-item.decision { border-color: #58a6ff; }
.obs-item.bugfix { border-color: #f85149; }
.obs-item.discovery { border-color: #d29922; }
.obs-item.task { border-color: #3fb950; }
.obs-item.error { border-color: #f85149; }
.obs-item.tool_use { border-color: #8b949e; }
.detail-view { display: none; }
.back-btn { color: #58a6ff; cursor: pointer; margin-bottom: 16px; font-size: 14px; }
</style>
</head>
<body>
<h1>🧠 Qwen Memory</h1>

<div id="stats-view"></div>
<input class="search-box" id="search" placeholder="搜索记忆..." oninput="doSearch(this.value)">
<div id="list-view"></div>
<div id="detail-view" class="detail-view"></div>

<script>
const API = '';

async function api(path) {
  const r = await fetch(API + path);
  return r.json();
}

async function loadStats() {
  const s = await api('/api/stats');
  document.getElementById('stats-view').innerHTML = `
    <div class="stats">
      <div class="stat-card"><div class="stat-num">${s.total_sessions}</div><div class="stat-label">会话</div></div>
      <div class="stat-card"><div class="stat-num">${s.total_observations}</div><div class="stat-label">观察</div></div>
      <div class="stat-card"><div class="stat-num">${s.total_snapshots}</div><div class="stat-label">快照</div></div>
      <div class="stat-card"><div class="stat-num">${s.sessions_last_7d}</div><div class="stat-label">近 7 天</div></div>
    </div>`;
}

async function loadRecent() {
  const sessions = await api('/api/sessions');
  renderSessionList(sessions);
}

function renderSessionList(sessions) {
  const el = document.getElementById('list-view');
  el.innerHTML = sessions.map(s => {
    const tags = (s.tags || '[]').replace(/["\\[\\]]/g,'').split(',').filter(Boolean);
    return `<div class="card" onclick="showDetail('${s.session_id}')">
      <div class="card-title">${s.session_id}</div>
      <div class="card-meta">${s.started_at?.slice(0,16) || '?'} · importance: ${s.importance}
        ${tags.map(t => `<span class="tag">${t}</span>`).join('')}
        ${s.importance >= 0.8 ? '<span class="tag tag-important">高</span>' : ''}
      </div>
      <div class="card-summary">${s.summary_short || ''}</div>
    </div>`;
  }).join('');
}

let searchTimer;
function doSearch(q) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    if (!q.trim()) { loadRecent(); return; }
    const r = await api('/api/search?q=' + encodeURIComponent(q));
    document.getElementById('detail-view').style.display = 'none';
    document.getElementById('list-view').style.display = 'block';
    renderSessionList(r.sessions || []);
    // 也显示观察结果
    const obsEl = document.getElementById('list-view');
    const obs = r.observations || [];
    if (obs.length) {
      obsEl.innerHTML += '<h2>相关观察</h2>' + obs.map(o =>
        `<div class="obs-item ${o.obs_type}">
          <div class="card-meta">#${o.id} · ${o.obs_type} · ${o.created_at?.slice(0,10)}</div>
          <div class="card-summary">${o.content}</div>
          ${o.impact ? '<div class="card-meta">影响: ' + o.impact + '</div>' : ''}
        </div>`
      ).join('');
    }
  }, 300);
}

async function showDetail(id) {
  const d = await api('/api/session/' + id);
  if (!d.session) return;
  const s = d.session;
  document.getElementById('list-view').style.display = 'none';
  const el = document.getElementById('detail-view');
  el.style.display = 'block';
  el.innerHTML = `
    <div class="back-btn" onclick="backToList()">← 返回列表</div>
    <h2>${s.session_id}</h2>
    <div class="card-meta">${s.started_at} → ${s.ended_at || '进行中'} · importance: ${s.importance}</div>
    <div class="card" style="cursor:default">
      <div class="card-summary">${s.summary || '无摘要'}</div>
    </div>
    <h2>观察 (${d.observations.length})</h2>
    <div class="obs-list">${d.observations.map(o =>
      `<div class="obs-item ${o.obs_type}">
        <div class="card-meta">#${o.id} · ${o.obs_type} · importance: ${o.importance}</div>
        <div class="card-summary">${o.content}</div>
        ${o.context ? '<div class="card-meta">上下文: ' + o.context + '</div>' : ''}
        ${o.impact ? '<div class="card-meta">影响: ' + o.impact + '</div>' : ''}
      </div>`
    ).join('')}</div>
    <h2>快照 (${d.snapshots.length})</h2>
    ${d.snapshots.map(s => `<div class="card" style="cursor:default">
      <div class="card-meta">[${s.snapshot_type}] ${s.title} · ${s.created_at}</div>
      <div class="card-summary">${s.description || ''}</div>
    </div>`).join('')}
  `;
}

function backToList() {
  document.getElementById('detail-view').style.display = 'none';
  document.getElementById('list-view').style.display = 'block';
}

loadStats();
loadRecent();
</script>
</body>
</html>"""


class MemoryHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "":
            self._html(HTML_TEMPLATE)
        elif path == "/api/stats":
            self._json(store.get_stats())
        elif path == "/api/sessions":
            limit = int(params.get("limit", [10])[0])
            self._json(store.get_recent_sessions(limit))
        elif path == "/api/search":
            q = params.get("q", [""])[0]
            if not q:
                self._json({"sessions": [], "observations": []})
                return
            sessions = store.search_sessions(q, limit=10)
            observations = store.search_observations(q, limit=10)
            self._json({"sessions": sessions, "observations": observations})
        elif path.startswith("/api/session/"):
            sid = path.split("/api/session/")[1]
            detail = store.get_session_detail(sid)
            self._json(detail or {"error": "not found"})
        elif path.startswith("/api/timeline/"):
            sid = path.split("/api/timeline/")[1]
            self._json(store.get_timeline(sid))
        elif path.startswith("/api/observation/"):
            oid = int(path.split("/api/observation/")[1])
            self._json(store.get_observations_full([oid]))
        else:
            self.send_error(404)

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    def _html(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # 静默日志


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", "-p", type=int, default=PORT)
    args = parser.parse_args()

    server = http.server.HTTPServer(("127.0.0.1", args.port), MemoryHandler)
    print(f"Qwen Memory Web Viewer")
    print(f"   http://localhost:{args.port}")
    print(f"   Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
