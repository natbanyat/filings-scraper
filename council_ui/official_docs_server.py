"""
Official documents corpus browser.

Local-first aiohttp server for:
- browsing downloaded official documents
- monitoring scrape runs
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
from pathlib import Path

from aiohttp import web

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import OFFICIAL_DOC_CORPUS_DIR, OFFICIAL_DOC_CORPUS_WINDOWS_DIR, TICKER_META  # noqa: E402
from official_doc_corpus import list_companies, list_documents, list_runs  # noqa: E402

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


def summary_payload() -> dict:
    root = corpus_root()
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
        "fallback_strategy": [
            "Use exchange/filer adapters first when available, then company IR archives.",
            "Per-file downloads retry with alternate request headers and a queryless URL variant when needed.",
            "SEC is live for filing retrieval today; LSE, HKEX, and TSE currently function as probe/discovery layers plus IR fallback.",
        ],
    }


async def index(_: web.Request) -> web.Response:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Official Document Corpus</title>
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
    --border: #28345c;
  }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: Inter, ui-sans-serif, system-ui, sans-serif;
  }
  .wrap { max-width: 1400px; margin: 0 auto; padding: 24px; }
  h1, h2, h3 { margin: 0 0 12px; }
  p { color: var(--muted); }
  .grid { display: grid; gap: 16px; }
  .grid-2 { grid-template-columns: 1.2fr 1fr; }
  .grid-3 { grid-template-columns: repeat(3, 1fr); }
  .card {
    background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.015));
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 16px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.2);
  }
  .muted { color: var(--muted); }
  .pill {
    display: inline-block;
    border-radius: 999px;
    padding: 4px 10px;
    font-size: 12px;
    border: 1px solid var(--border);
    background: var(--panel-2);
    color: var(--text);
  }
  .status-running { color: var(--warn); }
  .status-completed { color: var(--good); }
  .status-completed_with_errors, .status-failed { color: var(--bad); }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid rgba(255,255,255,0.07); vertical-align: top; }
  th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  input, select, button {
    width: 100%; box-sizing: border-box; border-radius: 10px; border: 1px solid var(--border);
    background: #0d1429; color: var(--text); padding: 10px 12px; font: inherit;
  }
  button {
    background: var(--accent); color: #081022; font-weight: 700; cursor: pointer;
  }
  button.secondary { background: transparent; color: var(--text); }
  .form-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }
  .company-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  .company-card { padding: 12px; background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 12px; }
  .small { font-size: 12px; }
  .mono { font-family: ui-monospace, SFMono-Regular, monospace; }
  .spaced { margin-top: 16px; }
  @media (max-width: 1080px) {
    .grid-2, .grid-3, .form-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="wrap grid">
  <div class="card">
    <h1>Official Document Corpus</h1>
    <p>Browse downloaded filings, presentations, transcripts, and data packs. Trigger new incremental or backfill runs without touching the CLI.</p>
    <div id="root-path" class="small mono muted"></div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <h2>Trigger a scrape</h2>
      <p class="small">Use a known coverage key or enter an ad hoc company. Incremental is faster. Backfill is better for first-look deep dives.</p>
      <form id="run-form" class="grid">
        <div class="form-grid">
          <label><span class="small muted">Known company</span><select id="coverage_key" name="coverage_key"></select></label>
          <label><span class="small muted">Mode</span><select name="mode"><option value="incremental">incremental</option><option value="backfill">backfill</option></select></label>
          <label><span class="small muted">Company name</span><input name="company_name" placeholder="Optional for ad hoc runs"></label>
          <label><span class="small muted">Ticker</span><input name="ticker" placeholder="Optional"></label>
          <label><span class="small muted">SEC CIK</span><input name="sec_cik" placeholder="0000019617"></label>
          <label><span class="small muted">IR page</span><input name="ir_page" placeholder="https://..."></label>
          <label><span class="small muted">Exchange adapter</span><select name="exchange_adapter"><option value="">none</option><option value="sec">sec</option><option value="lse">lse</option><option value="hkex">hkex</option><option value="tse">tse</option></select></label>
          <label><span class="small muted">Exchange symbol/code</span><input name="exchange_symbol" placeholder="STAN / 1299 / 8316"></label>
          <label><span class="small muted">Days back override</span><input name="days_back" placeholder="Optional integer"></label>
          <label><span class="small muted">Max docs override</span><input name="max_docs" placeholder="Optional integer"></label>
        </div>
        <div style="display:flex; gap:12px;">
          <button type="submit">Start scrape</button>
          <button type="button" id="refresh-btn" class="secondary">Refresh</button>
        </div>
      </form>
      <div id="run-result" class="small spaced"></div>
    </div>

    <div class="card">
      <h2>Fallback policy</h2>
      <ul id="fallback-list" class="small"></ul>
      <h3 class="spaced">Known coverage shortcuts</h3>
      <div id="known-companies" class="company-list"></div>
    </div>
  </div>

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

  <div class="card">
    <h2>Downloaded documents</h2>
    <table>
      <thead><tr><th>Company</th><th>Type</th><th>Date</th><th>Title</th><th>File</th><th>Source</th></tr></thead>
      <tbody id="documents-body"></tbody>
    </table>
  </div>
</div>
<script>
async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function el(tag, attrs = {}, text = null) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  if (text !== null) node.textContent = text;
  return node;
}

function formatDate(value) {
  if (!value) return '—';
  return value.replace('T', ' ').replace('+00:00', ' UTC');
}

function renderSummary(data) {
  document.getElementById('root-path').textContent = `${data.corpus_root}  |  Windows: ${data.corpus_root_windows}`;

  const coverage = document.getElementById('coverage_key');
  coverage.innerHTML = '';
  coverage.append(el('option', { value: '' }, 'Ad hoc / custom company'));
  data.known_companies.forEach(item => {
    const label = `${item.coverage_key} — ${item.company_name}`;
    coverage.append(el('option', { value: item.coverage_key }, label));
  });

  const fallbackList = document.getElementById('fallback-list');
  fallbackList.innerHTML = '';
  data.fallback_strategy.forEach(item => {
    const li = el('li');
    li.textContent = item;
    fallbackList.append(li);
  });

  const known = document.getElementById('known-companies');
  known.innerHTML = '';
  data.known_companies.slice(0, 12).forEach(item => {
    const div = el('div', { class: 'company-card' });
    div.innerHTML = `<strong>${item.company_name}</strong><br><span class="small muted">${item.coverage_key}</span><br><span class="small muted">adapter: ${item.exchange_adapter || 'none'}</span>`;
    known.append(div);
  });

  const runsBody = document.getElementById('runs-body');
  runsBody.innerHTML = '';
  data.recent_runs.forEach(run => {
    const tr = el('tr');
    tr.innerHTML = `
      <td><strong>${run.company_name}</strong><br><span class="small muted">${run.mode}</span></td>
      <td><span class="pill status-${run.status}">${run.status}</span></td>
      <td>${run.progress_message || '—'}</td>
      <td>${formatDate(run.started_at || run.created_at)}</td>
      <td class="small">found ${run.discovered_count || 0}<br>saved ${run.downloaded_count || 0}<br>skipped ${run.skipped_count || 0}<br>errors ${run.error_count || 0}</td>
    `;
    runsBody.append(tr);
  });

  const companies = document.getElementById('companies');
  companies.innerHTML = '';
  data.companies.forEach(company => {
    const div = el('div', { class: 'company-card' });
    div.innerHTML = `
      <strong>${company.company_name}</strong><br>
      <span class="small muted">${company.company_key}</span><br>
      <span class="small">${company.document_count} docs</span><br>
      <span class="small muted">last seen ${formatDate(company.last_seen_at || company.downloaded_at)}</span>
    `;
    companies.append(div);
  });

  const docsBody = document.getElementById('documents-body');
  docsBody.innerHTML = '';
  data.recent_documents.forEach(doc => {
    const tr = el('tr');
    const href = doc.relative_path ? `/files/${encodeURI(doc.relative_path)}` : (doc.final_url || doc.source_url || '#');
    const filename = doc.filename || 'missing';
    tr.innerHTML = `
      <td><strong>${doc.company_name}</strong><br><span class="small muted">${doc.company_key}</span></td>
      <td>${doc.doc_family || doc.doc_type || 'other'}</td>
      <td>${doc.published_at || 'undated'}</td>
      <td>${doc.title || 'Untitled'}</td>
      <td><a href="${href}" target="_blank">${filename}</a></td>
      <td class="small">${doc.source || '—'}</td>
    `;
    docsBody.append(tr);
  });
}

async function refresh() {
  const data = await fetchJSON('/api/summary');
  renderSummary(data);
}

document.getElementById('refresh-btn').addEventListener('click', refresh);

document.getElementById('run-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = {};
  for (const [key, value] of form.entries()) {
    if (value !== '') payload[key] = value;
  }
  ['days_back', 'max_docs'].forEach(key => {
    if (payload[key]) payload[key] = Number(payload[key]);
  });
  const resultBox = document.getElementById('run-result');
  resultBox.textContent = 'Starting run...';
  try {
    const data = await fetchJSON('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    resultBox.textContent = `Started run ${data.run_id} for ${data.company_name}`;
    refresh();
  } catch (err) {
    resultBox.textContent = `Error: ${err.message}`;
  }
});

refresh();
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
        "--root",
        str(corpus_root()),
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

    return json_response(
        {
            "ok": True,
            "pid": process.pid,
            "company_name": company_name or TICKER_META.get(coverage_key, {}).get("company_name") or coverage_key,
            "mode": mode,
            "message": "Run dispatched. Refresh to monitor progress.",
            "run_id": run_id,
        }
    )


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
    app.add_routes(
        [
            web.get("/", index),
            web.get("/health", health),
            web.get("/api/summary", api_summary),
            web.get("/api/runs", api_runs),
            web.get("/api/documents", api_documents),
            web.get("/api/companies", api_companies),
            web.post("/api/run", api_run),
            web.get(r"/files/{tail:.*}", file_handler),
        ]
    )
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
