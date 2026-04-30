# Cloudflare Tunnel — `filings-scraper`

Public-internet reachable hostname for the filings-scraper service, behind
bearer-token auth at the application layer plus optional Cloudflare Access
at the edge. Used by the Anthropic-hosted cloud Cowork sandbox (which is
not on the user's tailnet and therefore can't use the Tailscale path).

## Topology

```
Cloud Cowork session
        |
        | HTTPS + Authorization: Bearer <FILINGS_SCRAPER_TOKEN>
        v
filings.<user-domain>.com
        |
        | Cloudflare edge -> tunnel
        v
cloudflared on Windows host (Windows service)
        |
        | http://localhost:8876 (WSL2 transparent localhost forwarding)
        v
council_ui/official_docs_server.py inside WSL2 Ubuntu
```

## One-time setup

Prerequisites:
- A domain managed in Cloudflare DNS.
- Phase 1 (auth) deployed and `FILINGS_SCRAPER_TOKEN` set in `.env`.
- Phase 3 server bind 0.0.0.0 (or 127.0.0.1 — both work since cloudflared
  hits localhost anyway).

Steps:

```powershell
# 1. Install cloudflared on Windows.
#    Download from https://github.com/cloudflare/cloudflared/releases
#    or: winget install --id Cloudflare.cloudflared

# 2. Authenticate (opens a browser to your Cloudflare account).
cloudflared tunnel login

# 3. Create a named tunnel.
cloudflared tunnel create filings-scraper
#    Note the UUID printed; you'll need it for config.yml.

# 4. Route DNS: this creates the CNAME in your Cloudflare zone.
cloudflared tunnel route dns filings-scraper filings.<your-domain>.com

# 5. Drop config.example.yml at C:\Users\<USER>\.cloudflared\config.yml
#    and replace <TUNNEL-UUID> + <USER> + <your-domain>.

# 6. Smoke test interactively first.
cloudflared tunnel run filings-scraper
#    From any browser:  https://filings.<your-domain>.com/health  -> 200

# 7. Install as a Windows service so it survives reboot.
cloudflared service install
#    The service auto-starts at boot and reads config.yml from the same
#    location.

# 8. Verify the service is registered.
sc query cloudflared
```

## Verification

From any external client (cloud Cowork, MBP off-tailnet, your phone):

```bash
# Allowlisted - no token needed
curl -s https://filings.<your-domain>.com/health
# {"ok": true}

# Auth-enforced - 401 without token
curl -s -o /dev/null -w "%{http_code}\n" https://filings.<your-domain>.com/api/companies
# 401

# Auth-enforced - 200 with token
curl -s -H "Authorization: Bearer ${FILINGS_SCRAPER_TOKEN}" \
  https://filings.<your-domain>.com/api/companies | head -c 200
# JSON list of companies
```

## Optional: Cloudflare Access in front of the public hostname

Adds a zero-trust auth layer on top of the bearer token. Browser users get
a Cloudflare login page (Google / email OTP) before the request even reaches
the tunnel. Useful if you want the dashboard UI exposed publicly without
worrying about the bearer token leaking from a client.

Tradeoff: programmatic access (curl, scripts) needs a Cloudflare Access
service token instead of a session cookie, so cloud Cowork would need to
present BOTH the Cloudflare service token AND the bearer token. Defense in
depth, but more moving parts.

For a single-user personal scraper: bearer-only is fine. Add Access only if
you want the dashboard UI exposed without bearer auth.

To enable:
1. Cloudflare Dashboard → Zero Trust → Access → Applications → Add an application.
2. Type: Self-hosted. Hostname: `filings.<your-domain>.com`.
3. Policy: allow only your email.
4. Service tokens: generate one for cloud Cowork, store as
   `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` env vars there.
5. The skill must add headers `CF-Access-Client-Id` and
   `CF-Access-Client-Secret` to every request.

Skip if not desired.

## Common operational issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl https://filings...` hangs | Tunnel down or cloudflared not running | `sc query cloudflared`; restart with `sc start cloudflared`. |
| 502 from Cloudflare edge | Service not on `localhost:8876` (WSL stopped) | `wsl --status`; start WSL distro; service should auto-start (Phase 5 NSSM). |
| 401 even with token | Server token mismatch — check `.env`; service may have been restarted with a different token. | Reload server with the right token. |
| 503 with "FILINGS_SCRAPER_TOKEN is not set" | Server started in --require-auth without env var loaded | Check the service's env (Phase 5 NSSM passes env through). |
| Public URL works on phone but not cloud Cowork | Cowork sandbox blocks outbound to that domain — unlikely, but possible if Cowork has a strict egress allowlist. | Confirm with Anthropic Cowork docs; fall back to manual scrape Tier 4. |

## Token rotation under tunnel

Same as before: edit `.env`, restart the service. The tunnel itself doesn't
care about the bearer token — it just transparently proxies requests.

## Cost

- Tunnel: free (Cloudflare's free plan covers personal use).
- Domain: whatever you pay for the domain (~$10/year for .com).
- Cloudflare Access: free for up to 50 users on the free plan.
- Egress bandwidth: free up to whatever Cloudflare considers "fair use" —
  for a personal scraper this is irrelevant.
