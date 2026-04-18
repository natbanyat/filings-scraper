"""
Official documents corpus browser — v2.

Local-first aiohttp server for:
- browsing downloaded official documents with parse status and delta state
- monitoring scrape runs and parse job queue
- viewing the source registry
- triggering new incremental / backfill runs
- serving saved files from the local corpus directory

Usage:
  python council_ui/official_docs_server.py
  python council_ui/official_docs_server.py --host 0.0.0.0 --port 8876
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import OFFICIAL_DOC_CORPUS_DIR, OFFICIAL_DOC_CORPUS_WINDOWS_DIR, TICKER_META  # noqa: E402
from official_doc_corpus import (  # noqa: E402
    get_parse_stats,
    list_companies,
    list_documents,
    list_jobs,
    list_parse_records,
    list_runs,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8876


def json_response(payload: dict | list, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(payload, ensure_ascii=False),
        status=status,
        content_type="application/json",
    )


def corpus_root() -> Path:
    OFFICIAL_DOC_CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    return OFFICIAL_DOC_CORPUS_DIR


def _registry_as_list() -> list[dict]:
    try:
        from official_source_registry import get_registry
        return get_registry().as_list()
    except Exception:
        return []


def summary_payload() -> dict:
    root = corpus_root()
    try:
        parse_stats = get_parse_stats(root)
    except Exception:
        parse_stats = {}
    return {
        "corpus_root": str(root),
        "corpus_root_windows": OFFICIAL_DOC_CORPUS_WINDOWS_DIR,
        "known_companies": [
            {
                "coverage_key": key,
                "company_name": meta.get("company_name") or key.split("/")[-1],
                "ticker": key.split("/")[-1] if key.startswith("tickers/") else None,
                "sec_cik": meta.get("sec_cik"),
                "ir_page": meta.get("ir_page"),
                "exchange_adapter": meta.get("exchange_adapter"),
            }
            for key, meta in sorted(TICKER_META.items())
        ],
        "companies": list_companies(root),
        "recent_runs": list_runs(root, limit=25),
        "recent_documents": list_documents(root, limit=120),
        "recent_jobs": list_jobs(root, limit=30),
        "parse_stats": parse_stats,
        "fallback_strategy": [
            "Use exchange/filer adapters first when available, then company IR archives.",
            "Per-file downloads retry with alternate request headers and a queryless URL variant when needed.",
            "SEC is live for filing retrieval; LSE, HKEX, and TSE currently function as probe/discovery layers plus IR fallback.",
            "Sources with download_mode=browser are scaffolded; HTTP-first download is the default for all active sources.",
        ],
    }


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def index(_: web.Request) -> web.Response:
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Official Document Corpus v2</title>
<style>
  :root {
    --bg: #0b1020;
    --panel: #111832;
    --panel-2: #182241;
    --text: #e6ebff;
    --muted: #98a2c6;
    --accent: #7aa2ff;
    --good: #39d98a;
    --warn: #ffcb6b;
    --bad: #ff7b8b;
    --info: #81d4fa;
    --border: #28345c;
  }
  body { margin:0; background:var(--bg); color:var(--text); font-family:Inter,ui-sans-serif,system-ui,sans-serif; }
  .wrap { max-width:1500px; margin:0 auto; padding:24px; }
  h1,h2,h3 { margin:0 0 12px; }
  p { color:var(--muted); margin:0 0 8px; }
  .grid { display:grid; gap:16px; }
  .grid-2 { grid-template-columns:1.2fr 1fr; }
  .grid-3 { grid-template-columns:repeat(3,1fr); }
  .card {
    background:linear-gradient(180deg,rgba(255,255,255,.03),rgba(255,255,255,.015));
    border:1px solid var(--border); border-radius:16px; padding:16px;
    box-shadow:0 8px 24px rgba(0,0,0,.2);
  }
  .muted { color:var(--muted); }
  .pill {
    display:inline-block; border-radius:999px; padding:4px 10px;
    font-size:12px; border:1px solid var(--border);
    background:var(--panel-2); color:var(--text);
  }
  .status-running { color:var(--warn); }
  .status-completed,.status-parsed,.status-downloaded,.delta-new { color:var(--good); }
  .status-completed_with_errors,.status-failed,.status-parse_failed,.delta-failed_download,.delta-failed_parse { color:var(--bad); }
  .status-pending,.delta-unchanged,.status-unparsed,.status-not_applicable { color:var(--muted); }
  .delta-updated { color:var(--warn); }
  .delta-duplicate { color:var(--info); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:9px 8px; border-bottom:1px solid rgba(255,255,255,.06); vertical-align:top; }
  th { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  input,select,button {
    width:100%; box-sizing:border-box; border-radius:10px; border:1px solid var(--border);
    background:#0d1429; color:var(--text); padding:10px 12px; font:inherit;
  }
  button { background:var(--accent); color:#081022; font-weight:700; cursor:pointer; }
  button.secondary { background:transparent; color:var(--text); }
  .form-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }
  .company-list { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:10px; }
  .company-card { padding:12px; background:rgba(255,255,255,.02); border:1px solid var(--border); border-radius:12px; }
  .small { font-size:12px; }
  .mono { font-family:ui-monospace,SFMono-Regular,monospace; }
  .spaced { margin-top:16px; }
  .stat-grid { display:flex; gap:16px; flex-wrap:wrap; margin-top:8px; }
  .stat { background:var(--panel-2); border:1px solid var(--border); border-radius:10px; padding:10px 16px; text-align:center; min-width:80px; }
  .stat .num { font-size:22px; font-weight:700; }
  .stat .lbl { font-size:11px; color:var(--muted); }
  .tabs { display:flex; gap:8px; margin-bottom:12px; }
  .tab { padding:6px 14px; border-radius:8px; border:1px solid var(--border); cursor:pointer; font-size:13px; background:transparent; color:var(--muted); width:auto; }
  .tab.active { background:var(--accent); color:#081022; border-color:var(--accent); }
  @media(max-width:1080px) { .grid-2,.grid-3,.form-grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="wrap grid">
  <!-- Header -->
  <div class="card">
    <h1>Official Document Corpus</h1>
    <p>Browse filings, presentations, transcripts, and data packs. Monitor parse jobs, delta states, and source registry.</p>
    <div id="root-path" class="small mono muted"></div>
  </div>

  <!-- Parse / Delta stats -->
  <div class="card">
    <h2>Pipeline stats</h2>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;">
      <div>
        <h3 class="small muted" style="margin-bottom:6px;">Parse status</h3>
        <div id="stat-parse" class="stat-grid"></div>
      </div>
      <div>
        <h3 class="small muted" style="margin-bottom:6px;">Delta state</h3>
        <div id="stat-delta" class="stat-grid"></div>
      </div>
      <div>
        <h3 class="small muted" style="margin-bottom:6px;">Job queue</h3>
        <div id="stat-jobs" class="stat-grid"></div>
      </div>
    </div>
  </div>

  <div class="grid grid-2">
    <!-- Scrape trigger -->
    <div class="card">
      <h2>Trigger a scrape</h2>
      <p class="small">Use a known coverage key or enter an ad hoc company. Incremental is faster; backfill for first-look deep dives.</p>
      <form id="run-form" class="grid">
        <div class="form-grid">
          <label><span class="small muted">Known company</span><select id="coverage_key" name="coverage_key"></select></label>
          <label><span class="small muted">Mode</span><select name="mode"><option value="incremental">incremental</option><option value="backfill">backfill</option></select></label>
          <label><span class="small muted">Company name</span><input name="company_name" placeholder="Optional for ad hoc runs"></label>
          <label><span class="small muted">Ticker</span><input name="ticker" placeholder="Optional"></label>
          <label><span class="small muted">SEC CIK</span><input name="sec_cik" placeholder="0000019617"></label>
          <label><span class="small muted">IR page</span><input name="ir_page" placeholder="https://..."></label>
          <label><span class="small muted">Exchange adapter</span>
            <select name="exchange_adapter">
              <option value="">none</option><option value="sec">sec</option>
              <option value="lse">lse</option><option value="hkex">hkex</option><option value="tse">tse</option>
            </select>
          </label>
          <label><span class="small muted">Exchange symbol/code</span><input name="exchange_symbol" placeholder="STAN / 1299 / 8316"></label>
          <label><span class="small muted">Days back</span><input name="days_back" placeholder="Optional integer"></label>
          <label><span class="small muted">Max docs</span><input name="max_docs" placeholder="Optional integer"></label>
        </div>
        <div style="display:flex;gap:12px;">
          <button type="submit">Start scrape</button>
          <button type="button" id="refresh-btn" class="secondary">Refresh</button>
        </div>
      </form>
      <div id="run-result" class="small spaced"></div>
    </div>

    <!-- Fallback policy + known companies -->
    <div class="card">
      <h2>Fallback policy</h2>
      <ul id="fallback-list" class="small"></ul>
      <h3 class="spaced">Known coverage shortcuts</h3>
      <div id="known-companies" class="company-list"></div>
    </div>
  </div>

  <!-- Runs + Companies -->
  <div class="grid grid-2">
    <div class="card">
      <h2>Recent runs</h2>
      <table>
        <thead><tr><th>Company</th><th>Status</th><th>Progress</th><th>Started</th><th>Counts</th></tr></thead>
        <tbody id="runs-body"></tbody>
      </table>
    </div>
    <div class="card">
      <h2>Companies in corpus</h2>
      <div id="companies" class="company-list"></div>
    </div>
  </div>

  <!-- Jobs -->
  <div class="card">
    <h2>Parse job queue</h2>
    <div class="tabs">
      <button class="tab active" data-filter="">All</button>
      <button class="tab" data-filter="pending">Pending</button>
      <button class="tab" data-filter="running">Running</button>
      <button class="tab" data-filter="completed">Completed</button>
      <button class="tab" data-filter="failed">Failed</button>
    </div>
    <table>
      <thead><tr><th>Job</th><th>Type</th><th>Status</th><th>Company</th><th>Created</th><th>Result / Error</th></tr></thead>
      <tbody id="jobs-body"></tbody>
    </table>
  </div>

  <!-- Documents -->
  <div class="card">
    <h2>Downloaded documents</h2>
    <table>
      <thead>
        <tr>
          <th>Company</th><th>Family</th><th>Date</th><th>Title</th>
          <th>File</th><th>Delta</th><th>Parse</th><th>Source</th>
        </tr>
      </thead>
      <tbody id="documents-body"></tbody>
    </table>
  </div>

  <!-- Source registry -->
  <div class="card">
    <h2>Source registry</h2>
    <p class="small">Auto-bootstrapped from TICKER_META. Edit <code>scripts/official_source_registry.yaml</code> to override crawl parameters.</p>
    <table>
      <thead><tr><th>Source ID</th><th>Company</th><th>Domain</th><th>Crawl</th><th>Download</th><th>Parse profile</th><th>Priority</th><th>Enabled</th></tr></thead>
      <tbody id="registry-body"></tbody>
    </table>
  </div>
</div>

<script>
let _allJobs = [];
let _jobFilter = '';

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function el(tag, attrs = {}, text = null) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  if (text !== null) node.textContent = text;
  return node;
}

function formatDate(v) {
  if (!v) return '—';
  return v.replace('T', ' ').replace('+00:00', ' UTC');
}

function badge(text, cls) {
  const span = el('span', {class: `pill ${cls || ''}`});
  span.textContent = text || '—';
  return span;
}

function renderStatGrid(id, data, classMap) {
  const container = document.getElementById(id);
  container.innerHTML = '';
  Object.entries(data || {}).forEach(([k, v]) => {
    const cls = classMap ? (classMap[k] || '') : '';
    const div = el('div', {class: `stat ${cls}`});
    div.innerHTML = `<div class="num">${v}</div><div class="lbl">${k || 'null'}</div>`;
    container.append(div);
  });
}

function renderSummary(data) {
  document.getElementById('root-path').textContent =
    `${data.corpus_root}  |  Windows: ${data.corpus_root_windows}`;

  // Stats
  const ps = data.parse_stats || {};
  renderStatGrid('stat-parse', ps.parse_status, {parsed:'status-parsed',parse_failed:'status-parse_failed',unparsed:'status-unparsed',not_applicable:'status-not_applicable'});
  renderStatGrid('stat-delta', ps.delta_state, {new:'delta-new',updated:'delta-updated',unchanged:'delta-unchanged',duplicate:'delta-duplicate',failed_download:'delta-failed_download',failed_parse:'delta-failed_parse'});
  renderStatGrid('stat-jobs', ps.jobs, {pending:'status-pending',running:'status-running',completed:'status-completed',failed:'status-failed'});

  // Coverage dropdown
  const coverage = document.getElementById('coverage_key');
  coverage.innerHTML = '';
  coverage.append(el('option', {value:''}, 'Ad hoc / custom company'));
  data.known_companies.forEach(item => {
    coverage.append(el('option', {value:item.coverage_key}, `${item.coverage_key} — ${item.company_name}`));
  });

  // Fallback list
  const fb = document.getElementById('fallback-list');
  fb.innerHTML = '';
  (data.fallback_strategy || []).forEach(item => { const li=el('li'); li.textContent=item; fb.append(li); });

  // Known companies shortcuts
  const known = document.getElementById('known-companies');
  known.innerHTML = '';
  data.known_companies.slice(0,12).forEach(item => {
    const div = el('div', {class:'company-card'});
    div.innerHTML = `<strong>${item.company_name}</strong><br><span class="small muted">${item.coverage_key}</span><br><span class="small muted">adapter: ${item.exchange_adapter||'none'}</span>`;
    known.append(div);
  });

  // Runs
  const runsBody = document.getElementById('runs-body');
  runsBody.innerHTML = '';
  (data.recent_runs || []).forEach(run => {
    const tr = el('tr');
    tr.innerHTML = `
      <td><strong>${run.company_name}</strong><br><span class="small muted">${run.mode}</span></td>
      <td><span class="pill status-${run.status}">${run.status}</span></td>
      <td>${run.progress_message||'—'}</td>
      <td>${formatDate(run.started_at||run.created_at)}</td>
      <td class="small">found ${run.discovered_count||0}<br>saved ${run.downloaded_count||0}<br>skipped ${run.skipped_count||0}<br>err ${run.error_count||0}</td>
    `;
    runsBody.append(tr);
  });

  // Companies
  const companies = document.getElementById('companies');
  companies.innerHTML = '';
  (data.companies || []).forEach(c => {
    const div = el('div', {class:'company-card'});
    div.innerHTML = `<strong>${c.company_name}</strong><br><span class="small muted">${c.company_key}</span><br><span class="small">${c.document_count} docs</span><br><span class="small muted">last ${formatDate(c.last_seen_at||c.downloaded_at)}</span>`;
    companies.append(div);
  });

  // Jobs
  _allJobs = data.recent_jobs || [];
  renderJobs();

  // Documents
  const docsBody = document.getElementById('documents-body');
  docsBody.innerHTML = '';
  (data.recent_documents || []).forEach(doc => {
    const tr = el('tr');
    const href = doc.relative_path ? `/files/${encodeURI(doc.relative_path)}` : (doc.final_url||doc.source_url||'#');
    const fname = doc.filename || 'missing';
    const delta = doc.delta_state || '';
    const parse = doc.parse_status || '';
    tr.innerHTML = `
      <td><strong>${doc.company_name}</strong><br><span class="small muted">${doc.company_key}</span></td>
      <td>${doc.doc_family||doc.doc_type||'other'}</td>
      <td>${doc.published_at||'undated'}</td>
      <td>${doc.title||'Untitled'}</td>
      <td><a href="${href}" target="_blank">${fname}</a></td>
      <td><span class="pill delta-${delta}">${delta||'—'}</span></td>
      <td><span class="pill status-${parse}">${parse||'—'}</span></td>
      <td class="small">${doc.source||'—'}</td>
    `;
    docsBody.append(tr);
  });
}

function renderJobs() {
  const jobs = _jobFilter ? _allJobs.filter(j => j.status === _jobFilter) : _allJobs;
  const body = document.getElementById('jobs-body');
  body.innerHTML = '';
  if (!jobs.length) {
    const tr = el('tr');
    tr.innerHTML = `<td colspan="6" class="muted">No jobs.</td>`;
    body.append(tr);
    return;
  }
  jobs.forEach(job => {
    const tr = el('tr');
    const result = job.result_json ? (JSON.parse(job.result_json).quality_flags||[]).join(', ') : (job.error||'');
    tr.innerHTML = `
      <td class="small mono">${job.job_id.slice(0,8)}</td>
      <td>${job.job_type}</td>
      <td><span class="pill status-${job.status}">${job.status}</span></td>
      <td>${job.company_key||'—'}</td>
      <td>${formatDate(job.created_at)}</td>
      <td class="small muted">${result||'—'}</td>
    `;
    body.append(tr);
  });
}

async function loadRegistry() {
  try {
    const items = await fetchJSON('/api/registry');
    const body = document.getElementById('registry-body');
    body.innerHTML = '';
    items.forEach(e => {
      const tr = el('tr');
      tr.innerHTML = `
        <td class="small mono">${e.source_id}</td>
        <td>${e.company}</td>
        <td class="small">${e.domain}</td>
        <td><span class="pill">${e.crawl_mode}</span></td>
        <td><span class="pill ${e.download_mode==='browser'?'status-running':''}">${e.download_mode}</span></td>
        <td class="small">${e.parse_profile}</td>
        <td>${e.priority}</td>
        <td>${e.enabled?'<span class="good">yes</span>':'<span class="muted">no</span>'}</td>
      `;
      body.append(tr);
    });
  } catch (err) {
    document.getElementById('registry-body').innerHTML =
      `<tr><td colspan="8" class="muted small">Registry not available: ${err.message}</td></tr>`;
  }
}

// Tab filtering for jobs
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    _jobFilter = tab.dataset.filter;
    renderJobs();
  });
});

async function refresh() {
  const data = await fetchJSON('/api/summary');
  renderSummary(data);
}

document.getElementById('refresh-btn').addEventListener('click', () => {
  refresh();
  loadRegistry();
});

document.getElementById('run-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = {};
  for (const [k, v] of form.entries()) { if (v !== '') payload[k] = v; }
  ['days_back','max_docs'].forEach(k => { if (payload[k]) payload[k] = Number(payload[k]); });
  const resultBox = document.getElementById('run-result');
  resultBox.textContent = 'Starting run...';
  try {
    const data = await fetchJSON('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    resultBox.textContent = `Started run ${data.run_id} for ${data.company_name}`;
    refresh();
  } catch (err) {
    resultBox.textContent = `Error: ${err.message}`;
  }
});

refresh();
loadRegistry();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def api_summary(_: web.Request) -> web.Response:
    return json_response(summary_payload())


async def api_runs(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "50"))
    return json_response(list_runs(corpus_root(), limit=limit))


async def api_documents(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "200"))
    company_key = request.query.get("company_key")
    return json_response(list_documents(corpus_root(), company_key=company_key, limit=limit))


async def api_companies(_: web.Request) -> web.Response:
    return json_response(list_companies(corpus_root()))


async def api_jobs(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "100"))
    status = request.query.get("status") or None
    job_type = request.query.get("job_type") or None
    return json_response(list_jobs(corpus_root(), status=status, job_type=job_type, limit=limit))


async def api_parse_records(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "100"))
    doc_id = request.query.get("doc_id") or None
    return json_response(list_parse_records(corpus_root(), doc_id=doc_id, limit=limit))


async def api_registry(_: web.Request) -> web.Response:
    return json_response(_registry_as_list())


async def api_run(request: web.Request) -> web.Response:
    payload = await request.json()
    coverage_key = payload.get("coverage_key") or None
    company_name = payload.get("company_name") or None
    if not coverage_key and not company_name:
        return json_response({"error": "coverage_key or company_name is required"}, status=400)

    command = [
        sys.executable,
        str(SCRIPTS_DIR / "official_doc_corpus.py"),
        "run",
        "--root", str(corpus_root()),
        "--json",
    ]
    run_id = str(uuid.uuid4())
    command.extend(["--run-id", run_id])

    for key, flag in [
        ("coverage_key", "--coverage-key"),
        ("company_name", "--company-name"),
        ("ticker", "--ticker"),
        ("sec_cik", "--sec-cik"),
        ("ir_page", "--ir-page"),
        ("exchange_adapter", "--exchange-adapter"),
        ("exchange_symbol", "--exchange-symbol"),
    ]:
        value = payload.get(key)
        if value:
            command.extend([flag, str(value)])

    mode = payload.get("mode") or "incremental"
    command.extend(["--mode", mode])
    if payload.get("days_back"):
        command.extend(["--days-back", str(payload["days_back"])])
    if payload.get("max_docs"):
        command.extend(["--max-docs", str(payload["max_docs"])])

    process = subprocess.Popen(
        command,
        cwd=str(WORKSPACE_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    return json_response({
        "ok": True,
        "pid": process.pid,
        "company_name": company_name or TICKER_META.get(coverage_key, {}).get("company_name") or coverage_key,
        "mode": mode,
        "message": "Run dispatched. Refresh to monitor progress.",
        "run_id": run_id,
    })


async def file_handler(request: web.Request) -> web.StreamResponse:
    tail = request.match_info.get("tail", "")
    root = corpus_root().resolve()
    path = (root / tail).resolve()
    if not path.is_file() or not str(path).startswith(str(root)):
        raise web.HTTPNotFound(text="File not found")
    return web.FileResponse(path)


async def health(_: web.Request) -> web.Response:
    return json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.add_routes([
        web.get("/", index),
        web.get("/health", health),
        web.get("/api/summary", api_summary),
        web.get("/api/runs", api_runs),
        web.get("/api/documents", api_documents),
        web.get("/api/companies", api_companies),
        web.get("/api/jobs", api_jobs),
        web.get("/api/parse-records", api_parse_records),
        web.get("/api/registry", api_registry),
        web.post("/api/run", api_run),
        web.get(r"/files/{tail:.*}", file_handler),
    ])
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the official documents corpus UI")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    web.run_app(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
