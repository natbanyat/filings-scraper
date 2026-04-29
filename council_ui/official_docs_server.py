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


def corpus_python() -> str:
    venv_python = WORKSPACE_ROOT / ".venv" / "bin" / "python"
    return str(venv_python if venv_python.exists() else Path(sys.executable))


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
            "Sources with download_mode=browser can use the Playwright lane when the dependency is installed; direct asset URLs like PDFs still prefer HTTP when they are already addressable.",
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
  /* Artifact viewer modal */
  .modal-overlay{display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.82);overflow:auto;}
  .modal-overlay.open{display:flex;align-items:flex-start;justify-content:center;padding:32px 16px;}
  .modal-box{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:24px;max-width:960px;width:100%;position:relative;box-shadow:0 24px 64px rgba(0,0,0,.5);}
  .modal-close{position:absolute;top:12px;right:16px;cursor:pointer;background:transparent;color:var(--muted);border:none;font-size:22px;width:auto;padding:0 8px;line-height:1;}
  .modal-close:hover{color:var(--text);}
  .artifact-text{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;line-height:1.6;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:16px;max-height:480px;overflow:auto;margin-top:8px;}
  .artifact-raw{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,monospace;font-size:11px;line-height:1.5;color:var(--muted);background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:16px;max-height:480px;overflow:auto;margin-top:8px;}
  .chunk-block{border:1px solid var(--border);border-radius:8px;padding:12px;margin-top:8px;}
  .chunk-block summary{cursor:pointer;color:var(--muted);font-size:12px;user-select:none;}
  .chunk-block pre{white-space:pre-wrap;font-size:12px;font-family:ui-monospace,SFMono-Regular,monospace;margin:8px 0 0;max-height:300px;overflow:auto;}
  .view-btn{background:transparent;color:var(--accent);border:1px solid var(--border);border-radius:6px;padding:2px 8px;font-size:11px;cursor:pointer;width:auto;display:inline-block;vertical-align:middle;}
  .view-btn:hover{background:var(--panel-2);}
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
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;">
      <div>
        <h3 class="small muted" style="margin-bottom:6px;">Parse status</h3>
        <div id="stat-parse" class="stat-grid"></div>
      </div>
      <div>
        <h3 class="small muted" style="margin-bottom:6px;">Derived artifacts</h3>
        <div id="stat-derived" class="stat-grid"></div>
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
      <p class="small">Use a known coverage key or enter an ad hoc company. Incremental is faster; backfill for first-look deep dives. New runs parse downloaded documents inline by default.</p>
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
          <label><span class="small muted">Time horizon</span>
            <select name="days_back">
              <option value="">Default (Incremental)</option>
              <option value="90">1 Quarter (90 days)</option>
              <option value="365">1 Year (365 days)</option>
              <option value="1095">3 Years</option>
              <option value="1825">5 Years</option>
              <option value="3650">10 Years</option>
              <option value="99999">All Time</option>
            </select>
          </label>
          <label><span class="small muted">Max docs</span><input name="max_docs" placeholder="Optional integer"></label>
        </div>
        <div id="autofill-note" class="small muted" style="min-height:18px;"></div>
        <div style="display:flex;gap:12px;">
          <button type="submit">Start scrape</button>
          <button type="button" id="refresh-btn" class="secondary">Refresh</button>
        </div>
      </form>
      <div id="run-result" class="small spaced"></div>

      <h3 class="spaced">Backfill existing parses</h3>
      <p class="small">Queue parse jobs for already-downloaded corpus files, including older docs missing derived artifacts.</p>
      <form id="parse-form" class="grid">
        <div class="form-grid">
          <label><span class="small muted">Known company</span><select id="parse_company_key" name="company_key"></select></label>
          <label><span class="small muted">Limit</span><input name="limit" value="25"></label>
        </div>
        <label class="small muted" style="display:flex;gap:8px;align-items:center;">
          <input type="checkbox" id="missing-derived" name="missing_derived" checked style="width:auto;">
          Include already-parsed docs whose derived artifact is missing
        </label>
        <div style="display:flex;gap:12px;">
          <button type="submit">Run parse backfill</button>
        </div>
      </form>
      <div id="parse-result" class="small spaced"></div>
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
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;">
      <h2 style="margin:0;">Downloaded documents</h2>
      <input id="doc-filter" placeholder="Filter by company, title, family, or delta&#8230;" style="max-width:320px;padding:6px 10px;">
    </div>
    <table>
      <thead>
        <tr>
          <th>Company</th><th>Family</th><th>Date</th><th>Title</th>
          <th>Artifacts</th><th>Delta</th><th>Parse</th><th>Source</th>
        </tr>
      </thead>
      <tbody id="documents-body"></tbody>
    </table>
  </div>

  <!-- Source registry -->
  <div class="card">
    <h2 style="display:flex;align-items:center;gap:12px;">
      Source registry
    </h2>
    <p class="small">Auto-bootstrapped from TICKER_META. Edit <code>scripts/official_source_registry.yaml</code> to override crawl parameters.</p>
    <table>
      <thead><tr><th>Source ID</th><th>Company</th><th>Domain</th><th>Crawl</th><th>Download</th><th>Parse profile</th><th>Priority</th><th>Enabled</th></tr></thead>
      <tbody id="registry-body"></tbody>
    </table>
  </div>
</div>

<!-- Artifact viewer modal -->
<div class="modal-overlay" id="artifact-modal">
  <div class="modal-box">
    <button class="modal-close" id="modal-close-btn" title="Close">&times;</button>
    <div id="modal-content"></div>
  </div>
</div>

<script>
let _allJobs = [];
let _allDocs = [];
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
  renderStatGrid('stat-derived', ps.derived_artifacts, {available:'status-completed',missing:'status-unparsed'});
  renderStatGrid('stat-delta', ps.delta_state, {new:'delta-new',updated:'delta-updated',unchanged:'delta-unchanged',duplicate:'delta-duplicate',failed_download:'delta-failed_download',failed_parse:'delta-failed_parse'});
  renderStatGrid('stat-jobs', ps.jobs, {pending:'status-pending',running:'status-running',completed:'status-completed',failed:'status-failed'});

  // Coverage dropdowns
  const coverageOptionsMap = new Map();
  (data.known_companies || []).forEach(c => coverageOptionsMap.set(c.coverage_key, { key: c.coverage_key, name: c.company_name }));
  (data.companies || []).forEach(c => {
    const key = c.coverage_key || c.company_key;
    if (key && !coverageOptionsMap.has(key)) {
      coverageOptionsMap.set(key, { key: key, name: c.company_name });
    }
  });
  const coverageOptions = Array.from(coverageOptionsMap.values()).sort((a, b) => a.name.localeCompare(b.name));

  const coverage = document.getElementById('coverage_key');
  const parseCoverage = document.getElementById('parse_company_key');
  
  const selectedCoverage = coverage.value;
  const selectedParse = parseCoverage.value;

  coverage.innerHTML = '';
  parseCoverage.innerHTML = '';
  coverage.append(el('option', {value:''}, 'Ad hoc / custom company'));
  parseCoverage.append(el('option', {value:''}, 'All companies in corpus'));
  
  coverageOptions.forEach(item => {
    const label = `${item.key} — ${item.name}`;
    coverage.append(el('option', {value:item.key}, label));
    parseCoverage.append(el('option', {value:item.key}, label));
  });

  if (selectedCoverage) coverage.value = selectedCoverage;
  if (selectedParse) parseCoverage.value = selectedParse;

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
  _allDocs = data.recent_documents || [];
  renderDocs();
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

// Auto-fill metadata fields when a known company is selected. Fields stay
// editable so the user can override (e.g. switch primary exchange before
// running). Selecting "Ad hoc / custom company" leaves fields untouched.
const _autofillFields = ['company_name','ticker','sec_cik','ir_page','exchange_adapter','exchange_symbol'];
const _autofillNote = document.getElementById('autofill-note');

function _setRunFormField(name, value) {
  const el = document.querySelector('#run-form [name="' + name + '"]');
  if (!el) return;
  // Mark fields the user has manually edited so we don't clobber them on
  // a re-select. Manual edits are tracked via a 'data-user-edited' flag.
  if (el.dataset.userEdited === 'true') return;
  el.value = value || '';
  // Briefly highlight the field so the user sees what changed.
  el.style.transition = 'background-color 1.2s ease';
  el.style.backgroundColor = 'rgba(122,162,255,0.12)';
  setTimeout(() => { el.style.backgroundColor = ''; }, 1200);
}

function _clearAutofillUserEditMarks() {
  _autofillFields.forEach(name => {
    const el = document.querySelector('#run-form [name="' + name + '"]');
    if (el) delete el.dataset.userEdited;
  });
}

// Mark a field as user-edited if they type into it after auto-fill.
_autofillFields.forEach(name => {
  const el = document.querySelector('#run-form [name="' + name + '"]');
  if (!el) return;
  el.addEventListener('input', () => { el.dataset.userEdited = 'true'; });
  el.addEventListener('change', () => { el.dataset.userEdited = 'true'; });
});

document.getElementById('coverage_key').addEventListener('change', async (event) => {
  const key = event.target.value;
  if (!key) {
    _autofillNote.textContent = '';
    _clearAutofillUserEditMarks();
    return;
  }
  // Re-selecting a company clears manual-edit marks so the auto-fill applies fresh.
  _clearAutofillUserEditMarks();
  _autofillNote.textContent = 'Loading metadata for ' + key + '...';
  try {
    const data = await fetchJSON('/api/company-metadata?coverage_key=' + encodeURIComponent(key));
    _setRunFormField('company_name', data.company_name);
    _setRunFormField('ticker', data.ticker);
    _setRunFormField('sec_cik', data.sec_cik);
    _setRunFormField('ir_page', data.ir_page);
    _setRunFormField('exchange_adapter', data.exchange_adapter);
    _setRunFormField('exchange_symbol', data.exchange_symbol);
    const probeCount = (data.website_probe_urls || []).length;
    const parts = [];
    if (data.company_name) parts.push(data.company_name);
    if (data.exchange_adapter) parts.push('exchange: ' + data.exchange_adapter);
    if (data.sec_cik) parts.push('CIK: ' + data.sec_cik);
    if (probeCount > 1) parts.push(probeCount + ' probe URLs');
    _autofillNote.innerHTML = data.found
      ? 'Auto-filled from registry: <strong>' + parts.join(' &middot; ') + '</strong>. Fields are editable; e.g. change exchange adapter to override primary source.'
      : 'No registry metadata for ' + key + '. Fill in fields manually or add the entry to scripts/official_source_registry.yaml.';
  } catch (err) {
    _autofillNote.textContent = 'Auto-fill failed: ' + err.message;
  }
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

document.getElementById('parse-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = {
    process: true,
    limit: Number(form.get('limit') || 25),
    missing_derived: document.getElementById('missing-derived').checked,
  };
  if (form.get('company_key')) payload.company_key = form.get('company_key');
  const resultBox = document.getElementById('parse-result');
  resultBox.textContent = 'Dispatching parse backfill...';
  try {
    const data = await fetchJSON('/api/parse-backfill', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    resultBox.textContent = `Queued parse backfill ${data.dispatch_id} (${data.limit} doc cap${data.company_key ? `, ${data.company_key}` : ''}).`;
    refresh();
  } catch (err) {
    resultBox.textContent = `Error: ${err.message}`;
  }
});

// Document filter
function renderDocs() {
  const q = (document.getElementById('doc-filter').value || '').toLowerCase();
  const docs = q ? _allDocs.filter(d => {
    return (d.company_name||'').toLowerCase().includes(q)
        || (d.company_key||'').toLowerCase().includes(q)
        || (d.title||'').toLowerCase().includes(q)
        || (d.doc_family||d.doc_type||'').toLowerCase().includes(q)
        || (d.delta_state||'').toLowerCase().includes(q);
  }) : _allDocs;
  const docsBody = document.getElementById('documents-body');
  docsBody.innerHTML = '';
  if (!docs.length) {
    const tr = el('tr'); tr.innerHTML = '<td colspan="8" class="muted">No documents.</td>'; docsBody.append(tr); return;
  }
  docs.forEach(doc => {
    const tr = el('tr');
    const href = doc.relative_path ? `/files/${encodeURI(doc.relative_path)}` : (doc.final_url||doc.source_url||'#');
    const parseHref = doc.parse_artifact_relative_path ? `/files/${encodeURI(doc.parse_artifact_relative_path)}` : '';
    const derivedHref = doc.derived_artifact_relative_path ? `/files/${encodeURI(doc.derived_artifact_relative_path)}` : '';
    const fname = doc.filename || 'missing';
    const delta = doc.delta_state || '';
    const parse = doc.parse_status || '';
    tr.innerHTML = `
      <td><strong>${doc.company_name}</strong><br><span class="small muted">${doc.company_key}</span></td>
      <td>${doc.doc_family||doc.doc_type||'other'}</td>
      <td>${doc.published_at||'undated'}</td>
      <td>${doc.title||'Untitled'}</td>
      <td class="small">
        <a href="${href}" target="_blank">raw: ${fname}</a><br>
        ${doc.parse_artifact_available
          ? `<button class="view-btn" data-artifact-url="${parseHref}" data-artifact-type="parse">parse.json</button> <a href="${parseHref}" target="_blank" class="muted" title="Open raw">&#8599;</a>`
          : '<span class="muted">parse &mdash;</span>'}<br>
        ${doc.derived_artifact_available
          ? `<button class="view-btn" data-artifact-url="${derivedHref}" data-artifact-type="derived">derived.json</button> <a href="${derivedHref}" target="_blank" class="muted" title="Open raw">&#8599;</a>`
          : '<span class="muted">derived &mdash;</span>'}
      </td>
      <td><span class="pill delta-${delta}">${delta||'&mdash;'}</span></td>
      <td><span class="pill status-${parse}">${parse||'&mdash;'}</span><br><span class="small muted">${doc.latest_parser_name||'&mdash;'} ${doc.latest_parsed_at ? '&middot; '+formatDate(doc.latest_parsed_at) : ''}</span></td>
      <td class="small">${doc.source||'&mdash;'}</td>
    `;
    docsBody.append(tr);
  });
}

document.getElementById('doc-filter').addEventListener('input', renderDocs);

// Artifact viewer modal
const _modal = document.getElementById('artifact-modal');
const _modalContent = document.getElementById('modal-content');

document.getElementById('modal-close-btn').addEventListener('click', () => _modal.classList.remove('open'));
_modal.addEventListener('click', (e) => { if (e.target === _modal) _modal.classList.remove('open'); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _modal.classList.remove('open'); });

document.body.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-artifact-url]');
  if (!btn) return;
  openArtifact(btn.dataset.artifactUrl, btn.dataset.artifactType);
});

async function openArtifact(url, type) {
  _modalContent.innerHTML = '<p class="muted" style="padding:24px 0;">Loading\u2026</p>';
  _modal.classList.add('open');
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    _modalContent.innerHTML = '';
    _modalContent.append(buildArtifactView(data, type, url));
  } catch (err) {
    _modalContent.innerHTML = '<p style="color:var(--bad);">Failed to load: ' + err.message + '</p>';
  }
}

function buildArtifactView(data, type, rawUrl) {
  const wrap = el('div');
  const typeLabel = type === 'parse' ? 'Parse artifact' : 'Derived artifact';
  const hdr = el('div', {style:'display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap;'});
  hdr.innerHTML = '<h2 style="margin:0;">' + typeLabel + '</h2>';
  const rawLink = el('a', {href: rawUrl, target:'_blank', class:'pill', style:'font-size:12px;'}, 'Raw JSON \u2197');
  hdr.append(rawLink);
  wrap.append(hdr);

  let showRaw = false;
  const tabBar = el('div', {class:'tabs', style:'margin-bottom:12px;'});
  const fmtBtn = el('button', {class:'tab active'}, 'Formatted');
  const rawBtn = el('button', {class:'tab'}, 'Raw JSON');
  tabBar.append(fmtBtn, rawBtn);
  wrap.append(tabBar);

  const area = el('div');
  wrap.append(area);

  function repaint() {
    area.innerHTML = '';
    if (showRaw) {
      const pre = el('pre', {class:'artifact-raw'});
      pre.textContent = JSON.stringify(data, null, 2);
      area.append(pre);
    } else {
      if (type === 'parse') renderParseView(area, data);
      else renderDerivedView(area, data);
    }
  }

  fmtBtn.addEventListener('click', () => { showRaw=false; fmtBtn.classList.add('active'); rawBtn.classList.remove('active'); repaint(); });
  rawBtn.addEventListener('click', () => { showRaw=true; rawBtn.classList.add('active'); fmtBtn.classList.remove('active'); repaint(); });
  repaint();
  return wrap;
}

function renderParseView(container, d) {
  const metaRow = el('div', {style:'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;'});
  metaRow.append(badge(d.parser_name || 'unknown', ''));
  (d.quality_flags || []).forEach(f => metaRow.append(badge(f, 'status-parsed')));
  if (d.ok === false) metaRow.append(badge('failed', 'status-failed'));
  container.append(metaRow);

  if (d.error) {
    const ep = el('p', {style:'color:var(--bad);margin-bottom:8px;'});
    ep.textContent = 'Error: ' + d.error;
    container.append(ep);
  }

  const md = d.metadata || {};
  const statsWrap = el('div', {style:'display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;'});
  [
    ['Pages', d.page_count != null ? d.page_count : 'N/A'],
    ['Tables', d.table_count != null ? d.table_count : 0],
    ['Chars', d.text ? d.text.length.toLocaleString() : 0],
    ['Headings', (md.headings||[]).length || md.heading_count || 0],
  ].forEach(([lbl, val]) => {
    const s = el('div', {class:'stat'});
    s.innerHTML = '<div class="num" style="font-size:15px;">' + val + '</div><div class="lbl">' + lbl + '</div>';
    statsWrap.append(s);
  });
  container.append(statsWrap);

  if (md.title || d.title) {
    const tp = el('p', {style:'font-weight:600;margin-bottom:6px;'});
    tp.textContent = md.title || d.title;
    container.append(tp);
  }
  if (md.description) {
    const dp = el('p', {class:'small muted', style:'margin-bottom:8px;'});
    dp.textContent = md.description;
    container.append(dp);
  }

  if (d.text) {
    const hl = el('h3', {class:'small muted', style:'margin:0 0 4px;'});
    hl.textContent = 'Extracted text (' + d.text.length.toLocaleString() + ' chars)';
    container.append(hl);
    const pre = el('pre', {class:'artifact-text'});
    pre.textContent = d.text;
    container.append(pre);
  }

  if (d.tables && d.tables.length) {
    const hl = el('h3', {class:'small muted', style:'margin:12px 0 4px;'});
    hl.textContent = 'Tables (' + d.tables.length + ')';
    container.append(hl);
    d.tables.slice(0, 5).forEach((t, i) => {
      const det = el('details', {class:'chunk-block'});
      det.innerHTML = '<summary>Table ' + (i+1) + (t.caption ? ' \u2014 ' + t.caption : '') + '</summary>';
      const pre = el('pre');
      pre.textContent = JSON.stringify(t, null, 2);
      det.append(pre);
      container.append(det);
    });
  }
}

function renderDerivedView(container, d) {
  const delta = d.delta || {};
  const ds = delta.status || '\u2014';
  const deltaRow = el('div', {style:'display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px;'});
  deltaRow.innerHTML = '<span class="small muted">Delta:</span><span class="pill delta-' + (delta.status||'') + '">' + ds + '</span>';
  if (delta.has_prior) {
    const pb = el('span', {class:'pill status-running', title:'A prior version exists in the corpus'}, 'has prior version');
    deltaRow.append(pb);
  }
  container.append(deltaRow);

  const metaRow = el('div', {style:'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;'});
  metaRow.append(badge(d.parser_name || 'unknown', ''));
  (d.quality_flags || []).forEach(f => metaRow.append(badge(f, 'status-parsed')));
  container.append(metaRow);

  const summ = d.summary || {};
  const statsWrap = el('div', {style:'display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;'});
  [
    ['Chars', (d.text_chars || d.normalized_text_chars || 0).toLocaleString()],
    ['Chunks', d.chunk_count || 0],
    ['Tables', summ.table_count != null ? summ.table_count : 0],
    ['Pages', summ.page_count != null ? summ.page_count : 'N/A'],
  ].forEach(([lbl, val]) => {
    const s = el('div', {class:'stat'});
    s.innerHTML = '<div class="num" style="font-size:15px;">' + val + '</div><div class="lbl">' + lbl + '</div>';
    statsWrap.append(s);
  });
  container.append(statsWrap);

  if (summ.title) {
    const tp = el('p', {style:'font-weight:600;margin-bottom:4px;'});
    tp.textContent = summ.title;
    container.append(tp);
  }

  if (summ.excerpt) {
    const hl = el('h3', {class:'small muted', style:'margin:0 0 4px;'});
    hl.textContent = 'Excerpt';
    container.append(hl);
    const pre = el('pre', {class:'artifact-text', style:'max-height:180px;'});
    pre.textContent = summ.excerpt;
    container.append(pre);
  }

  if (d.chunks && d.chunks.length) {
    const hl = el('h3', {class:'small muted', style:'margin:12px 0 4px;'});
    hl.textContent = 'Chunks (' + d.chunks.length + ')';
    container.append(hl);
    d.chunks.forEach(chunk => {
      const det = el('details', {class:'chunk-block'});
      const range = 'chars ' + (chunk.char_start||0).toLocaleString() + '\u2013' + (chunk.char_end||0).toLocaleString() + ' (' + ((chunk.char_end||0) - (chunk.char_start||0)).toLocaleString() + ' chars)';
      det.innerHTML = '<summary>Chunk ' + chunk.chunk_index + ' \u2014 ' + range + '</summary>';
      const pre = el('pre');
      pre.textContent = chunk.text || '';
      det.append(pre);
      container.append(det);
    });
  }

  if (d.tables_preview && d.tables_preview.length) {
    const hl = el('h3', {class:'small muted', style:'margin:12px 0 4px;'});
    hl.textContent = 'Tables preview (' + d.tables_preview.length + ')';
    container.append(hl);
    d.tables_preview.slice(0, 5).forEach((t, i) => {
      const det = el('details', {class:'chunk-block'});
      det.innerHTML = '<summary>Table ' + (i+1) + '</summary>';
      const pre = el('pre');
      pre.textContent = JSON.stringify(t, null, 2);
      det.append(pre);
      container.append(det);
    });
  }
}

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


async def api_company_metadata(request: web.Request) -> web.Response:
    """Return the canonical metadata for a single coverage key, merged from
    TICKER_META and the source registry (whose seed_urls augment the
    website_probe_urls list).

    Used by the dashboard to auto-fill the scrape-trigger form when the
    user selects a known company. All fields remain editable client-side
    so the user can override (e.g. switch primary exchange before running)."""
    coverage_key = (request.query.get("coverage_key") or "").strip()
    if not coverage_key:
        return json_response({"error": "coverage_key required"}, status=400)

    meta = TICKER_META.get(coverage_key) or {}

    probe_urls: list[str] = []
    for u in meta.get("website_probe_urls") or []:
        if u and u not in probe_urls:
            probe_urls.append(u)
    ir_page = meta.get("ir_page") or ""
    if ir_page and ir_page not in probe_urls:
        probe_urls.insert(0, ir_page)

    # Augment with any seed_urls registered for this coverage key. The
    # registry is the durable home for crawl-specific URLs (archive pages,
    # sitemaps) that don't belong in TICKER_META.
    try:
        from official_source_registry import get_registry
        for entry in get_registry().for_coverage_key(coverage_key):
            for u in entry.seed_urls or []:
                if u and u not in probe_urls:
                    probe_urls.append(u)
    except Exception:
        pass

    ticker_from_key = (
        coverage_key.split("/", 1)[1]
        if coverage_key.startswith("tickers/") else None
    )

    return json_response({
        "coverage_key": coverage_key,
        "company_name": meta.get("company_name") or "",
        "ticker": meta.get("ticker") or ticker_from_key or "",
        "sec_cik": meta.get("sec_cik") or "",
        "ir_page": ir_page,
        "exchange_adapter": meta.get("exchange_adapter") or "",
        "exchange_symbol": meta.get("exchange_symbol") or meta.get("exchange_code") or "",
        "exchange_code": meta.get("exchange_code") or "",
        "exchange_slug": meta.get("exchange_slug") or "",
        "website_probe_urls": probe_urls,
        "found": bool(meta or probe_urls),
    })


async def api_run(request: web.Request) -> web.Response:
    payload = await request.json()
    coverage_key = payload.get("coverage_key") or None
    company_name = payload.get("company_name") or None
    if not coverage_key and not company_name:
        return json_response({"error": "coverage_key or company_name is required"}, status=400)

    command = [
        corpus_python(),
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
    if payload.get("parse", True):
        command.append("--parse")

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
        "message": "Run dispatched with inline parsing. Refresh to monitor progress.",
        "run_id": run_id,
    })


async def api_parse_backfill(request: web.Request) -> web.Response:
    payload = await request.json()
    command = [
        corpus_python(),
        str(SCRIPTS_DIR / "official_doc_corpus.py"),
        "parse",
        "--root", str(corpus_root()),
        "--backfill-existing",
        "--limit", str(int(payload.get("limit") or 25)),
    ]

    company_key = payload.get("company_key") or None
    if company_key:
        command.extend(["--company-key", str(company_key)])
    if payload.get("missing_derived", True):
        command.append("--missing-derived")
    if payload.get("process", True):
        command.append("--process")

    dispatch_id = str(uuid.uuid4())
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
        "dispatch_id": dispatch_id,
        "company_key": company_key,
        "limit": int(payload.get("limit") or 25),
        "message": "Parse backfill dispatched. Refresh to monitor parse jobs and artifact links.",
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
        web.get("/api/company-metadata", api_company_metadata),
        web.post("/api/run", api_run),
        web.post("/api/parse-backfill", api_parse_backfill),
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
