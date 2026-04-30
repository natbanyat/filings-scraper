#!/usr/bin/env bash
# WSL-side launcher for the filings-scraper service. NSSM (Phase 5) wraps
# this via wsl.exe so the service comes up on Windows boot.
#
# Loads .env, activates venv, execs the server binding to 0.0.0.0:8876
# with --require-auth (Phase 1 guard).

set -euo pipefail

# Path resolution — script lives at <repo>/scripts/start.sh.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Load .env (FILINGS_SCRAPER_TOKEN, FILINGS_SCRAPER_URL, etc.).
# Exported vars become the server's process env.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# Sanity: the service refuses to start without a token under --require-auth,
# but we surface a clearer error here before invoking python.
if [ -z "${FILINGS_SCRAPER_TOKEN:-}" ]; then
    echo "ERROR: FILINGS_SCRAPER_TOKEN is not set in .env or env." >&2
    echo "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'" >&2
    exit 2
fi

# Honor an override from .env / Windows side, otherwise default to 8876.
PORT="${FILINGS_SCRAPER_PORT:-8876}"

VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "ERROR: venv python not found at $VENV_PYTHON" >&2
    echo "Set up with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 2
fi

exec "$VENV_PYTHON" council_ui/official_docs_server.py \
    --host 0.0.0.0 \
    --port "$PORT" \
    --require-auth
