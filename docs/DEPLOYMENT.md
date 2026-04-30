# Filings-Scraper Deployment Runbook

End-to-end deployment of the filings-scraper service on the user's always-on
Windows desktop, reachable from three client surfaces:

| Client | Reachability | Auth |
|---|---|---|
| Cowork on the same Windows desktop | WSL2 transparent localhost forwarding (`http://127.0.0.1:8876`) | Bearer token |
| MBP / second device on the user's tailnet | Tailscale tailnet hostname (`http://openclaw-wsl.tail-xxxx.ts.net:8876`) | Bearer token |
| Anthropic-hosted cloud Cowork | Cloudflare Tunnel public hostname (`https://filings.<user-domain>.com`) | Bearer token + optional Cloudflare Access |

The rollout is split into six phases. The order is non-negotiable — each
phase makes the next safe. Skip a phase and you briefly run an
unauthenticated scraper on the network.

---

## Phase status

| Phase | Status | Branch |
|---|---|---|
| 1. Auth layer + `/version` + bind safety guard | ✅ Landed | `phase1-auth` |
| 2. Clients send `Authorization: Bearer` (skill + service.md) | ✅ Landed | `phase2-clients` |
| 3. Default bind 0.0.0.0 + Tailscale install | 🟡 In progress | `phase3-bind-tailscale` |
| 4. Cloudflare Tunnel for cloud Cowork | ⬜ Pending | `phase4-cloudflared` |
| 5. NSSM auto-start + power settings | ⬜ Pending | `phase5-nssm` |
| 6. Healthcheck + DEPLOYMENT.md rewrite + Notion | ⬜ Pending | `phase6-cleanup` |

---

## Phase 1 — Auth layer

**Code (already landed):**

- `bearer_auth_middleware` enforces `Authorization: Bearer <token>` on every endpoint except `/health` and `/version`.
- Bind safety guard in `main()` refuses to start with a non-localhost bind unless a token is set.
- `--require-auth` flag controls whether to enforce auth (defaults to True when `FILINGS_SCRAPER_TOKEN` is set in env).
- `/version` returns `{commit_sha, started_at, python_version, auth_required, host}`.
- `.env.example` documents the env vars.

**Operator steps:**

1. Generate a token:
   ```bash
   python -c 'import secrets; print(secrets.token_urlsafe(48))'
   ```
2. Drop into `.env` at the repo root:
   ```
   FILINGS_SCRAPER_TOKEN=<generated-token>
   FILINGS_SCRAPER_URL=http://127.0.0.1:8876
   ```
3. Restart the service. From the running prod process (PID held by `openclaw-docs-ui` or NSSM later) — for now manually inside WSL.
4. Verify:
   ```bash
   curl -s http://127.0.0.1:8876/health                              # 200
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8876/api/companies  # 401
   curl -s -H "Authorization: Bearer ${FILINGS_SCRAPER_TOKEN}" http://127.0.0.1:8876/api/companies | head -c 200  # 200
   ```

**Token rotation:** restart the service with a new env var. Old token is
immediately invalid; clients re-read the env on next call.

---

## Phase 2 — Clients send the token

**Code (already landed in the OneDrive investing workspace):**

- `.skills/user/ir-document-scraper/SKILL.md` reads `FILINGS_SCRAPER_URL` and `FILINGS_SCRAPER_TOKEN` from env, threads `Authorization: Bearer ...` into all curl examples, and stops cleanly with a new stop_condition when the token is missing for a non-localhost URL.
- `.skills/user/ir-document-scraper/references/service.md` documents per-client URL + auth.

**Operator steps:**

Per client environment, set:

```
FILINGS_SCRAPER_URL=<environment-specific>
FILINGS_SCRAPER_TOKEN=<same-token-as-server>
```

Where `FILINGS_SCRAPER_URL` is:
- Local Windows / openclaw: `http://127.0.0.1:8876`
- MBP via Tailscale: `http://openclaw-wsl.tail-xxxx.ts.net:8876` (after Phase 3)
- Cloud Cowork via Tunnel: `https://filings.<user-domain>.com` (after Phase 4)

WSL CLI (`scripts/official_doc_corpus.py`) speaks straight to the SQLite
corpus and does not need either env var.

---

## Phase 3 — Default bind 0.0.0.0 + Tailscale install

### Server change (already landed)

`DEFAULT_HOST` flipped from `127.0.0.1` → `0.0.0.0`. Bind safety guard from
Phase 1 makes this safe — the service refuses to start without a token unless
`--host` is forced back to localhost AND `--require-auth` is False.

⚠️ **WARNING:** do not run Phase 3 code without Phase 1 deployed and a token
set. The safety guard makes this structurally impossible (the service exits 2),
but documenting the requirement here for clarity.

### Tailscale install on Windows host

1. Install Tailscale for Windows from `https://tailscale.com/download/windows`.
2. Sign in to your Tailscale account.
3. Approve the device in the Tailscale admin console.
4. Note the Windows machine's tailnet hostname (e.g. `nat-desktop.tail-xxxx.ts.net`).

### Tailscale install inside WSL2

Required because the WSL2 Linux VM has separate networking from the Windows host.
Without this, Tailscale clients see only the Windows host, not the WSL service.

```bash
# Inside WSL2 Ubuntu:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Follow the auth URL in a browser, approve the WSL VM as a separate device.
tailscale status  # confirm WSL VM has its own tailnet IP
```

Note the WSL VM's hostname (e.g. `openclaw-wsl.tail-xxxx.ts.net`).

### Power settings (Windows desktop)

Goal: the desktop never sleeps while plugged in. Display can sleep; system
must not.

1. Settings → System → Power & battery → Screen and sleep:
   - "When plugged in, put my device to sleep after" → **Never**
   - "When plugged in, turn off my screen after" → 15 min (your preference)
2. Disable hibernate (frees disk space, prevents accidental hibernation):
   ```powershell
   powercfg /hibernate off
   ```
3. Disable USB selective suspend so the network adapter doesn't go offline:
   ```powershell
   powercfg /change disk-timeout-ac 0
   ```
4. (Optional) enable Wake-on-LAN if the desktop will sometimes be remote.

### Verification

From the Windows host (still works):
```
curl -s http://127.0.0.1:8876/health    # 200
```

From a second device on the same tailnet (e.g. MBP or another laptop):
```
curl -s http://openclaw-wsl.tail-xxxx.ts.net:8876/health    # 200 (allowlisted)
curl -s -H "Authorization: Bearer $TOKEN" http://openclaw-wsl.tail-xxxx.ts.net:8876/api/companies | head -c 200    # 200
curl -s http://openclaw-wsl.tail-xxxx.ts.net:8876/api/companies    # 401 — proves auth enforces
```

If the second-device test fails:
- `tailscale status` on both ends — both must show the other as connected.
- `tailscale ping openclaw-wsl` — must succeed.
- WSL firewall: `sudo ufw status` — should not block port 8876 from tailnet IPs.

---

## Phase 4 — Cloudflare Tunnel for cloud Cowork (pending)

Pending implementation. Will document once the tunnel is configured.

Required setup:
- Cloudflare account
- Domain managed in Cloudflare DNS
- `cloudflared` binary installed as a Windows service

Topology will be: cloud Cowork → `https://filings.<user-domain>.com` →
Cloudflare edge → tunnel → cloudflared on Windows host →
`http://localhost:8876` (which forwards into WSL via WSL2's transparent
localhost handling).

Optional: Cloudflare Access in front of the public hostname adds a zero-trust
email-auth layer on top of the bearer token (defense in depth, single-user
allowlist).

---

## Phase 5 — NSSM auto-start + power settings (pending)

Wraps the WSL service launch in a Windows service that auto-starts on boot
and auto-restarts on crash with backoff. Pending implementation.

---

## Phase 6 — Cleanup + observability (pending)

- `scripts/healthcheck.sh` — service / tunnel / tailnet status one-liner.
- Daily Task Scheduler health check email.
- DEPLOYMENT.md rewrite to consolidate.
- Notion architecture page update.

---

## Operator quick reference

### Restart the service (after Phase 5 NSSM lands)

```powershell
sc stop FilingsScraper
sc start FilingsScraper
```

### Check service health

```bash
curl -fsS http://127.0.0.1:8876/health || tail -50 %LOCALAPPDATA%\filings-scraper\logs\stderr.log
```

### Generate a new token

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Update `.env` with the new value, restart the service, distribute to clients.

### Verify auth enforcement

```bash
# Should be 401:
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8876/api/companies
# Should be 200:
curl -s -H "Authorization: Bearer $TOKEN" -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8876/api/companies
# Should be 200 (allowlisted):
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8876/health
```
