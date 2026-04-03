"""
Council UI — local web server for LLM Council run history.

Serves a dropdown of prior council sessions with full detail view.
Designed to be exposed over Tailscale later (bind to 0.0.0.0, port 8765).

Usage:
  python council_ui/server.py           # default: 127.0.0.1:8765
  python council_ui/server.py --host 0.0.0.0 --port 8765   # Tailscale-ready
  python council_ui/server.py --host 0.0.0.0               # keep default port

Tailscale exposure (when ready):
  1. Run with --host 0.0.0.0 --port 8765
  2. On the Tailscale network, access via http://<tailscale-ip>:8765
  3. Or use tailscale serve to put it behind HTTPS:
       tailscale serve --bg --https=443 --set-path / http://localhost:8765
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from aiohttp import web
except ImportError:
    sys.exit("aiohttp is required: pip install aiohttp")

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR   = WORKSPACE_ROOT / "research" / "council-sessions"
INDEX_PATH     = SESSIONS_DIR / "index.json"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


# ── Markdown → HTML (minimal, no dependencies) ────────────────────────────────

def _md_to_html(text: str) -> str:
    """Convert a subset of Markdown to HTML for display."""
    import html as htmlmod
    lines = text.split("\n")
    out: list[str] = []
    in_table = False

    for line in lines:
        escaped = htmlmod.escape(line)

        # Headers
        if line.startswith("### "):
            if in_table: out.append("</table>"); in_table = False
            out.append(f"<h3>{htmlmod.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            if in_table: out.append("</table>"); in_table = False
            out.append(f"<h2>{htmlmod.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            if in_table: out.append("</table>"); in_table = False
            out.append(f"<h1>{htmlmod.escape(line[2:])}</h1>")
        # Horizontal rule
        elif re.match(r"^-{3,}$", line):
            if in_table: out.append("</table>"); in_table = False
            out.append("<hr>")
        # Table row
        elif line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not in_table:
                out.append('<table class="md-table">')
                in_table = True
            # Separator row (all dashes)
            if all(re.match(r"^[-:]+$", c) for c in cells if c):
                continue
            tag = "th" if not any(r.startswith("<tr>") for r in out[-3:]) else "td"
            row = "".join(f"<{tag}>{htmlmod.escape(c)}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
        # Bullet
        elif line.startswith("- "):
            if in_table: out.append("</table>"); in_table = False
            content = _inline_md(htmlmod.escape(line[2:]))
            out.append(f"<li>{content}</li>")
        # Blank line
        elif line.strip() == "":
            if in_table: out.append("</table>"); in_table = False
            out.append("<br>")
        # Normal paragraph
        else:
            if in_table: out.append("</table>"); in_table = False
            out.append(f"<p>{_inline_md(escaped)}</p>")

    if in_table:
        out.append("</table>")
    return "\n".join(out)


def _inline_md(text: str) -> str:
    """Apply inline markdown: **bold**, *italic*, `code`."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*",     r"<em>\1</em>",         text)
    text = re.sub(r"`([^`]+)`",     r"<code>\1</code>",      text)
    return text


# ── Index helpers ──────────────────────────────────────────────────────────────

def _load_index() -> list[dict]:
    if not INDEX_PATH.exists():
        return []
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _load_session_file(session_id: str, filename: str) -> str | None:
    path = SESSIONS_DIR / session_id / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


# ── HTML template ──────────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Council Run History</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; }}
header {{ background: #1a1d2e; border-bottom: 1px solid #2d3148; padding: 12px 24px; display: flex; align-items: center; gap: 16px; }}
header h1 {{ font-size: 1.1rem; font-weight: 600; color: #a5b4fc; letter-spacing: 0.02em; }}
header .subtitle {{ font-size: 0.8rem; color: #64748b; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
.controls {{ display: flex; gap: 12px; align-items: center; margin-bottom: 24px; flex-wrap: wrap; }}
select {{ background: #1e2130; border: 1px solid #374151; color: #e2e8f0; padding: 8px 12px; border-radius: 6px; font-size: 0.9rem; min-width: 420px; cursor: pointer; }}
select:focus {{ outline: none; border-color: #6366f1; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }}
.badge-hold {{ background: #1e3a5f; color: #60a5fa; }}
.badge-buy {{ background: #14532d; color: #4ade80; }}
.badge-trim {{ background: #450a0a; color: #f87171; }}
.badge-add {{ background: #14532d; color: #4ade80; }}
.badge-unknown {{ background: #292524; color: #a8a29e; }}
.meta-row {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
.meta-pill {{ background: #1e2130; border: 1px solid #2d3148; border-radius: 6px; padding: 6px 14px; font-size: 0.8rem; color: #94a3b8; }}
.meta-pill span {{ color: #c7d2fe; font-weight: 600; }}
.panel {{ background: #1a1d2e; border: 1px solid #2d3148; border-radius: 8px; padding: 24px; margin-bottom: 20px; }}
.panel h2 {{ font-size: 1rem; font-weight: 600; color: #a5b4fc; margin-bottom: 16px; border-bottom: 1px solid #2d3148; padding-bottom: 8px; }}
.summary-body h1 {{ font-size: 1.15rem; color: #c7d2fe; margin: 16px 0 8px; }}
.summary-body h2 {{ font-size: 1rem; color: #a5b4fc; margin: 14px 0 6px; border-top: 1px solid #1e2a3a; padding-top: 10px; }}
.summary-body h3 {{ font-size: 0.9rem; color: #818cf8; margin: 10px 0 4px; }}
.summary-body p, .summary-body li {{ font-size: 0.88rem; line-height: 1.6; color: #cbd5e1; margin: 4px 0; }}
.summary-body li {{ margin-left: 18px; list-style: disc; }}
.summary-body strong {{ color: #e2e8f0; }}
.summary-body code {{ background: #0f172a; color: #86efac; padding: 1px 5px; border-radius: 3px; font-family: monospace; font-size: 0.82rem; }}
.summary-body hr {{ border: none; border-top: 1px solid #2d3148; margin: 12px 0; }}
.summary-body table.md-table {{ border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 0.82rem; }}
.summary-body table.md-table th, .summary-body table.md-table td {{ border: 1px solid #2d3148; padding: 4px 10px; text-align: left; }}
.summary-body table.md-table th {{ background: #1e2130; color: #a5b4fc; }}
.passover-body {{ font-size: 0.82rem; font-family: monospace; color: #94a3b8; white-space: pre-wrap; overflow-x: auto; }}
.empty {{ color: #475569; font-size: 0.9rem; text-align: center; padding: 40px; }}
.tab-row {{ display: flex; gap: 4px; margin-bottom: 16px; }}
.tab {{ padding: 6px 14px; border-radius: 5px; font-size: 0.82rem; cursor: pointer; background: #1e2130; border: 1px solid #2d3148; color: #64748b; }}
.tab.active {{ background: #2d3148; color: #a5b4fc; border-color: #4f46e5; }}
#passover-section {{ display: none; }}
#dashboard-link {{ display: none; }}
a.dash-link {{ color: #6366f1; font-size: 0.82rem; text-decoration: none; }}
a.dash-link:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<header>
  <h1>Council Run History</h1>
  <span class="subtitle">LLM Council — adversarial investment research</span>
</header>
<div class="container">
  <div class="controls">
    <select id="session-select">
      <option value="">— select a council session —</option>
    </select>
    <span id="verdict-badge"></span>
    <a id="dashboard-link" class="dash-link" href="#" target="_blank">open dashboard</a>
  </div>
  <div id="meta-row" class="meta-row"></div>
  <div class="tab-row" id="tab-row" style="display:none">
    <div class="tab active" onclick="showTab('summary')">Summary</div>
    <div class="tab" onclick="showTab('passover')">Passover / Context</div>
  </div>
  <div class="panel" id="summary-section">
    <div class="summary-body" id="summary-body">
      <div class="empty">Select a session above to view its summary.</div>
    </div>
  </div>
  <div class="panel" id="passover-section">
    <h2>Passover Context (for future reruns)</h2>
    <div class="passover-body" id="passover-body"></div>
  </div>
</div>
<script>
const sessions = SESSION_DATA_PLACEHOLDER;
const select   = document.getElementById('session-select');
const badge    = document.getElementById('verdict-badge');
const metaRow  = document.getElementById('meta-row');
const summBody = document.getElementById('summary-body');
const pasvBody = document.getElementById('passover-body');
const tabRow   = document.getElementById('tab-row');
const dashLink = document.getElementById('dashboard-link');

// Populate dropdown
sessions.forEach(s => {{
  const opt = document.createElement('option');
  opt.value = s.session_id;
  opt.textContent = `${{s.date}}  ${{s.question.length > 80 ? s.question.slice(0,80)+'…' : s.question}}`;
  select.appendChild(opt);
}});

function verdictClass(v) {{
  const m = {{ BUY:'buy', HOLD:'hold', TRIM:'trim', ADD:'add' }};
  return 'badge badge-' + (m[v] || 'unknown');
}}

function showTab(name) {{
  document.getElementById('summary-section').style.display = name === 'summary' ? '' : 'none';
  document.getElementById('passover-section').style.display = name === 'passover' ? '' : 'none';
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
}}

select.addEventListener('change', async () => {{
  const id = select.value;
  if (!id) {{
    summBody.innerHTML = '<div class="empty">Select a session above to view its summary.</div>';
    metaRow.innerHTML  = '';
    badge.innerHTML    = '';
    tabRow.style.display = 'none';
    dashLink.style.display = 'none';
    return;
  }}
  const s = sessions.find(x => x.session_id === id);

  badge.className   = verdictClass(s.verdict);
  badge.textContent = s.verdict;

  metaRow.innerHTML = `
    <div class="meta-pill">Date <span>${{s.date}}</span></div>
    <div class="meta-pill">Depth <span>${{s.depth}}</span></div>
    <div class="meta-pill">Agents <span>${{s.n_agents}}</span></div>
    <div class="meta-pill">Rounds <span>${{s.n_rounds}}</span></div>
    <div class="meta-pill">Confidence <span>${{s.confidence}}</span></div>
  `;

  tabRow.style.display = '';
  // Show summary tab by default
  document.getElementById('summary-section').style.display = '';
  document.getElementById('passover-section').style.display = 'none';
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', i===0));

  // Fetch summary
  summBody.innerHTML = '<div class="empty">Loading…</div>';
  try {{
    const r = await fetch(`/api/session/${{id}}/summary`);
    const j = await r.json();
    summBody.innerHTML = j.html || '<div class="empty">No summary available.</div>';
  }} catch(e) {{
    summBody.innerHTML = '<div class="empty">Failed to load summary.</div>';
  }}

  // Fetch passover
  pasvBody.textContent = 'Loading…';
  try {{
    const r2 = await fetch(`/api/session/${{id}}/passover`);
    const j2 = await r2.json();
    pasvBody.textContent = j2.raw || 'No passover file found.';
  }} catch(e) {{
    pasvBody.textContent = 'Failed to load passover data.';
  }}

  // Dashboard link
  if (s.has_dashboard) {{
    dashLink.href = `/api/session/${{id}}/dashboard`;
    dashLink.style.display = '';
  }} else {{
    dashLink.style.display = 'none';
  }}
}});
</script>
</body>
</html>
"""


def _render_index_html(sessions: list[dict]) -> str:
    safe_json = json.dumps(sessions, indent=None)
    return _HTML.replace("SESSION_DATA_PLACEHOLDER", safe_json)


# ── Route handlers ─────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    sessions = _load_index()
    html = _render_index_html(sessions)
    return web.Response(text=html, content_type="text/html")


async def handle_api_sessions(request: web.Request) -> web.Response:
    return web.json_response(_load_index())


async def handle_api_summary(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    raw = _load_session_file(session_id, "SUMMARY.md")
    if raw is None:
        raise web.HTTPNotFound(text="SUMMARY.md not found")
    return web.json_response({"raw": raw, "html": _md_to_html(raw)})


async def handle_api_passover(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    raw = _load_session_file(session_id, "passover.json")
    if raw is None:
        return web.json_response({"raw": None})
    return web.json_response({"raw": raw})


async def handle_api_dashboard(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    raw = _load_session_file(session_id, "dashboard.html")
    if raw is None:
        raise web.HTTPNotFound(text="dashboard.html not found")
    return web.Response(text=raw, content_type="text/html")


# ── App factory + main ─────────────────────────────────────────────────────────

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/",                              handle_index)
    app.router.add_get("/api/sessions",                  handle_api_sessions)
    app.router.add_get("/api/session/{session_id}/summary",   handle_api_summary)
    app.router.add_get("/api/session/{session_id}/passover",  handle_api_passover)
    app.router.add_get("/api/session/{session_id}/dashboard", handle_api_dashboard)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Council UI — local web server for council run history"
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Bind host (default: {DEFAULT_HOST}; use 0.0.0.0 for Tailscale)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    app = make_app()

    sessions = _load_index()
    print(f"Council UI — {len(sessions)} session(s) indexed")
    print(f"Listening on http://{args.host}:{args.port}")
    if args.host == "0.0.0.0":
        print(f"Tailscale: http://<tailscale-ip>:{args.port}")
        print(f"  Or: tailscale serve --bg --https=443 --set-path / http://localhost:{args.port}")

    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
