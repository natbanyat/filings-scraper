# Deployment Plan

This document tracks how the filings scraper is hosted across machines and what
needs to change as the execution surface evolves.

## Phase 0 — local CLI (current)

- Cloned to a non-OneDrive location on each developer machine.
  - Windows reference: `C:\Users\natba\repos\filings-scraper`
  - **Never clone inside `OneDrive/`** — sync corrupts `.git/` (file locking,
    partial pack-file syncs, ghost index conflicts).
- Per-machine Python 3.12 venv (`.venv/`) with `requirements.txt`.
- Entrypoints (`scripts/official_doc_corpus.py`, `scripts/official_docs_fetcher.py`,
  `scripts/official_docs_probe.py`) and the local browser UI
  (`council_ui/official_docs_server.py`, port 8876) all run directly from the
  venv.
- Smoke-tested on Windows: server responds `HTTP 200` on `http://127.0.0.1:8876/`.

## Phase 1 — server + Tailscale (planned, blocked on always-on host)

When a dedicated always-on machine is available (Windows or Linux box hosting
the command center / openclaw / scheduled tasks), promote the existing aiohttp
server to the canonical execution surface:

1. **Host one instance** of `council_ui/official_docs_server.py` on the always-on
   box. Bind to the Tailscale interface (e.g. `--host 100.x.x.x`) or `0.0.0.0`
   plus a Tailscale ACL — never the public internet.
2. **Auto-start + auto-restart.** The repo already ships
   `systemd/openclaw-docs-ui.service` for Linux. For a Windows always-on host,
   add an equivalent NSSM / Task Scheduler unit (TODO when the host is chosen).
3. **Bearer-token auth.** Even on a tailnet, require a header token. One leaked
   client should not equal an open scraper. Token lives only on the server in
   `.env`; clients store it in their own `.env`.
4. **Versioned API.** Prefix endpoints with `/v1/...` and have the client log
   the server version on each call. With >1 client, silent server↔client drift
   is the most likely outage cause.
5. **Client wrapper in `investing/` workspace.** A thin `scripts/filings_client.py`
   inside the OneDrive-backed `investing/` repo that skills call instead of
   shelling out to the scraper directly. Reads server URL + token from
   `context/` or `.env`. Writes returned docs into
   `companies/[TICKER]/sources/filings/` so OneDrive remains the system of record.
6. **Secrets stay on the server.** EDGAR User-Agent, IR cookies, broker creds
   live only on the always-on box. Clients hold only the tailnet bearer token.

## Cross-platform paths (open issue)

`scripts/config.py` currently hardcodes WSL-style paths:

```python
INBOX_DIR = Path("/mnt/c/Users/natba/OneDrive/@ Cowork/investing/inbox")
OFFICIAL_DOC_CORPUS_DIR = Path("/mnt/c/Users/natba/OneDrive/@ Cowork/openclaw-investing-context")
OFFICIAL_DOC_CORPUS_WINDOWS_DIR = r"C:\Users\natba\OneDrive\@ Cowork\openclaw-investing-context"
```

This works inside WSL on the openclaw box. On native Windows Python the WSL
paths construct as `Path` objects but won't resolve to the real filesystem.
`--help` calls work fine; any subcommand that touches the corpus directory will
not until paths are made platform-aware.

**Resolution (deferred to Phase 1 cutover):** introduce `OFFICIAL_DOC_CORPUS_DIR`
as a single env-var-driven `Path` that resolves correctly on the host actually
running the server (Linux or Windows). Clients shouldn't need to know this path
at all once the server fronts the corpus.

## Reachability matrix

| Surface              | How it reaches the scraper                          |
| -------------------- | --------------------------------------------------- |
| Always-on host       | Local — runs the server itself.                     |
| Windows desktop      | Tailscale → server on always-on host.               |
| MacBook Pro          | Tailscale → server on always-on host.               |
| Cowork / openclaw    | HTTP client → server on always-on host (same tailnet). |

## Open infrastructure decisions

- Which machine becomes the always-on host? (Windows desktop kept on, or a
  small Linux box / mini-PC?)
- Authoritative cache location: server-local SQLite (current) is fine; do not
  put `.db` files on OneDrive.
- Backup strategy for the corpus directory once the server is the sole writer.

## Commit cadence

While the repo is under active build:
- Feature branch per logical milestone (`phase0-setup`, `backfill-recall-fixes`, …).
- Push immediately after each commit so GitHub stays in sync.
- Merge to `main` from github.com once each milestone is reviewed.
