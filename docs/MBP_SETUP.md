# MBP — Cowork client setup for filings-scraper

You're setting up a new MacBook Pro to use the filings-scraper service that lives on the always-on Windows desktop. The MBP is a **client only** — the scraper service itself does not run here. Documents and the corpus stay on the Windows desktop; the MBP just sees them through the API.

There are two reachability paths from the MBP, depending on where you are:

| Where you are | Path | URL |
|---|---|---|
| At home, on the same network as the desktop, or anywhere with Tailscale up | Tailscale tailnet | `http://openclaw-wsl.tail-xxxx.ts.net:8876` |
| Anywhere else (coffee shop, plane, etc.) | Cloudflare Tunnel public hostname | `https://filings.<your-domain>.com` |

The Tailscale path is faster and doesn't traverse the public internet; use it when both endpoints are on the tailnet. Set `FILINGS_SCRAPER_URL` to whichever path the MBP can reach right now.

---

## Step 1 — Install Tailscale

```bash
brew install --cask tailscale
open -a Tailscale.app
# Sign in to the same Tailscale account as the Windows desktop.
# Approve the new Mac in the Tailscale admin console.
```

Verify the desktop's WSL VM is visible:

```bash
tailscale status | grep openclaw
# Should show: 100.x.x.x   openclaw-wsl   <user>@   linux  ...
```

Test connectivity (no token needed for `/health`):

```bash
curl -s http://openclaw-wsl.tail-xxxx.ts.net:8876/health
# {"ok": true}
```

(Replace the tailnet hostname with the actual one printed by `tailscale status` on the desktop.)

---

## Step 2 — Install Claude Cowork (Claude Desktop)

```bash
# Download Claude Desktop from claude.ai/download.
# Sign in with the same account you use on Windows.
```

The MBP's Cowork session will share the same OneDrive-backed `investing/` workspace as the Windows side, because OneDrive syncs the entire workspace across both machines. The skill (`ir-document-scraper`) is the same in both places.

What's NOT shared:
- The filings-scraper corpus (`openclaw-investing-context/`) — lives on the Windows desktop only. Cowork on the MBP reads/writes through the HTTPS API, not the filesystem.
- Local env vars — set per machine (next step).

---

## Step 3 — Set client env vars

The skill reads these from the shell env. Add them to your shell rc so every Cowork session inherits them:

```bash
# In ~/.zshrc (or ~/.bashrc):
export FILINGS_SCRAPER_URL="http://openclaw-wsl.tail-xxxx.ts.net:8876"
export FILINGS_SCRAPER_TOKEN="<paste the same token from the Windows .env>"
```

Reload:

```bash
source ~/.zshrc
echo "$FILINGS_SCRAPER_URL"
# http://openclaw-wsl.tail-xxxx.ts.net:8876
```

If you'll travel with the MBP off-tailnet often, consider scripting a switch:

```bash
# In ~/.zshrc:
filings-tunnel() {
    export FILINGS_SCRAPER_URL="https://filings.<your-domain>.com"
    echo "filings-scraper -> tunnel"
}
filings-tailnet() {
    export FILINGS_SCRAPER_URL="http://openclaw-wsl.tail-xxxx.ts.net:8876"
    echo "filings-scraper -> tailnet"
}
```

Use `filings-tunnel` when off-tailnet, `filings-tailnet` when back home.

---

## Step 4 — End-to-end smoke test

Verify the full chain works from the MBP shell:

```bash
# 1. Health (allowlisted, no token needed):
curl -s "$FILINGS_SCRAPER_URL/health"
# {"ok": true}

# 2. Auth enforced (no token, expect 401):
curl -s -o /dev/null -w "%{http_code}\n" "$FILINGS_SCRAPER_URL/api/companies"
# 401

# 3. Auth round-trip (with token, expect 200):
curl -s -H "Authorization: Bearer $FILINGS_SCRAPER_TOKEN" \
  "$FILINGS_SCRAPER_URL/api/companies" | head -c 200
# JSON list of companies

# 4. Version (sanity-check the deployed build):
curl -s "$FILINGS_SCRAPER_URL/version"
# {"commit_sha":"...","started_at":"...","python_version":"3.12.x","auth_required":true,"host":"NatDesktop"}
```

If 1 fails: check Tailscale is up on both ends, or try the Cloudflare Tunnel URL.
If 2 returns 200: the desktop server is in dev passthrough — fix the desktop `.env` and restart `FilingsScraper`.
If 3 returns 401: token mismatch. Token is per-server and case-sensitive; copy it again from the desktop `.env`.

---

## Step 5 — Try the skill

In Cowork on the MBP:

> Backfill last 12 months of filings for HSBC.

The skill should walk the interactive intake (already-known coverage_key, no missing fields), confirm `tickers/HSBC` resolved metadata, then trigger a run via the service. You'll see counts come back and the corpus path returned. Files land on the Windows desktop's `openclaw-investing-context/HSBC_hsbc-holdings-plc/` folder, which OneDrive then syncs back to the MBP.

---

## What NOT to do on the MBP

- **Don't run the filings-scraper service.** The corpus state lives in SQLite on the Windows desktop. Running a second service on the MBP would create a split-brain.
- **Don't try to invoke the WSL CLI.** The MBP doesn't have WSL. The HTTPS API is the only client surface here.
- **Don't store the token in the OneDrive `.env`.** That file syncs across machines and would put the token in OneDrive — keep secrets in shell env / `~/.zshrc` only, on each machine.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl: (7) Failed to connect` to tailnet URL | Tailscale down on either end, or the desktop went to sleep | `tailscale status` both ends; check Windows power settings |
| `502 Bad Gateway` from tunnel URL | `cloudflared` not running on Windows OR the WSL service is down | On Windows: `sc query cloudflared` + `sc query FilingsScraper` |
| `401` even with token | Token mismatch between server and client `.env` / shell env | Re-copy the token from the desktop's `.env` |
| Skill says `FILINGS_SCRAPER_URL` not set | Shell rc not reloaded since edit | `source ~/.zshrc` or open a new terminal |
| Files appear in API but not on local disk | OneDrive sync delay | Wait 30 sec; check OneDrive sync status |

---

## Future: Tailscale on the run

If you find yourself repeatedly switching between tailnet and tunnel URLs, consider Tailscale's "Funnel" feature, which exposes a tailnet host to the public internet through Tailscale's edge. Less moving parts than running both Tailscale and Cloudflare Tunnel; tradeoff is slightly higher latency and some Tailscale plans charge for it. Skip unless the manual switch becomes annoying.
