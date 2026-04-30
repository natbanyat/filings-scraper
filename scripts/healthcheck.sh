#!/usr/bin/env bash
# Filings-scraper deploy healthcheck.
# Runs three probes and prints a one-page status. Designed for daily
# Task-Scheduler invocation (Phase 6) — exit 0 = all green; non-zero
# = at least one probe failed (use this as the email-on-failure signal).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$REPO_ROOT/.env" ] && set -a && . "$REPO_ROOT/.env" && set +a

URL="${FILINGS_SCRAPER_URL:-http://127.0.0.1:8876}"
TOKEN="${FILINGS_SCRAPER_TOKEN:-}"

echo "filings-scraper healthcheck @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "URL: $URL"
echo

EXIT=0

# 1. Service liveness via /health (allowlisted, no token).
HEALTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$URL/health" || echo "000")
if [ "$HEALTH_CODE" = "200" ]; then
    echo "[OK]   service /health           -> 200"
else
    echo "[FAIL] service /health           -> $HEALTH_CODE"
    EXIT=1
fi

# 2. Auth enforcement via /api/companies WITHOUT token (must be 401).
AUTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$URL/api/companies" || echo "000")
if [ "$AUTH_CODE" = "401" ]; then
    echo "[OK]   auth enforced (no token)  -> 401"
elif [ "$AUTH_CODE" = "200" ]; then
    # 200 here means the server is in dev passthrough — only OK on localhost.
    case "$URL" in
        http://127.0.0.1*|http://localhost*)
            echo "[WARN] auth not enforced        -> 200 (dev mode, localhost only)"
            ;;
        *)
            echo "[FAIL] auth not enforced on remote URL -> 200 (security risk)"
            EXIT=1
            ;;
    esac
else
    echo "[FAIL] /api/companies (no token) -> $AUTH_CODE (expected 401)"
    EXIT=1
fi

# 3. Service round-trip with token.
if [ -n "$TOKEN" ]; then
    OK_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
        -H "Authorization: Bearer $TOKEN" "$URL/api/companies" || echo "000")
    if [ "$OK_CODE" = "200" ]; then
        echo "[OK]   /api/companies (token)   -> 200"
    else
        echo "[FAIL] /api/companies (token)   -> $OK_CODE"
        EXIT=1
    fi
else
    echo "[SKIP] /api/companies (token)   -> FILINGS_SCRAPER_TOKEN not set in env"
fi

# 4. Tailscale (best-effort — installation + login are operator concerns,
# pre-Phase 3 the binary may not even be present. Don't hard-fail.)
if command -v tailscale >/dev/null 2>&1; then
    TS_STATUS=$(tailscale status --self=false 2>/dev/null | head -1 || echo "")
    if [ -n "$TS_STATUS" ]; then
        echo "[OK]   tailnet up               -> $(tailscale status --self 2>/dev/null | awk 'NR==1{print $2}')"
    else
        echo "[WARN] tailscale installed but not logged in (run: sudo tailscale up)"
    fi
else
    echo "[SKIP] tailscale not installed in this environment"
fi

# 5. Latest run age (corpus DB — sanity check the scrape pipeline still works).
if [ -n "$TOKEN" ]; then
    LATEST=$(curl -s --max-time 10 -H "Authorization: Bearer $TOKEN" "$URL/api/runs?limit=1" 2>/dev/null \
        | python3 -c "import json,sys,datetime; d=json.load(sys.stdin); r=d[0] if d else None; \
ts=r.get('finished_at') or r.get('created_at') if r else ''; print(ts)" 2>/dev/null || echo "")
    if [ -n "$LATEST" ]; then
        echo "[OK]   last run finished_at     -> $LATEST"
    else
        echo "[INFO] no runs in corpus yet"
    fi
fi

echo
if [ "$EXIT" = "0" ]; then
    echo "OVERALL: PASS"
else
    echo "OVERALL: FAIL ($EXIT failure(s))"
fi

exit $EXIT
