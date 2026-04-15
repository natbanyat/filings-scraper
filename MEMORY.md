# Investing Research Workspace — Session Memory

## Coverage Universe

### Tickers
| Ticker | Name | Close window | Channel |
|--------|------|-------------|---------|
| JPM | JPMorgan Chase | US | #jpm |
| TMX | TMX Group | US | #tmx |
| STAN | Standard Chartered | US | #stan |
| GRAB | Grab Holdings | US | #grab |
| SE | Sea Limited | US | #se |
| FUTU | Futu Holdings | US | #futu |
| GOOG | Alphabet | US | #goog |
| MMYT | MakeMyTrip | US | #mmyt |
| 8316 | SMFG (Sumitomo Mitsui) | Asia Japan (06:00 UTC) | #8316 |
| 1299 | AIA Group | Asia HK (08:00 UTC) | #1299 |

### Sectors
| Key | Name | Close window | Channel |
|-----|------|-------------|---------|
| sectors/exchanges | Stock exchange operators | US | #exchanges |
| sectors/gold-miners | Gold miners (GDX) | US | #gold-miners |
| sectors/uranium-miners | Uranium miners (URA) | US | #uranium-miners |
| sectors/japan-banks | Japan megabanks | Asia Japan | #japan-banks |

### Markets (country bets)
| Key | Name | Close window | Channel |
|-----|------|-------------|---------|
| markets/japan | Japan | Asia Japan | #japan |
| markets/korea | South Korea | Asia Korea (06:30 UTC) | #korea |

### Special channels (not coverage-item-bound)
| Key | Channel | When |
|-----|---------|------|
| special/macro-close | #macro-close | US close daily |
| special/catalyst-alerts | #catalyst-alerts | US close daily |
| special/weekly-digest | #weekly-digest | Sunday ~22:00 UTC |
| special/self-eval     | #self-eval     | Monday ~06:00 UTC |

---

## Pipeline Architecture

### Scripts
- `daily_news.py` — main orchestrator; auto-flags tickers needing coverage updates (earnings/view-changing)
- `coverage_updater.py` — updates thesis/KPI/catalysts/debates; propose-then-apply workflow
- `macro_close.py` — macro close summary (Brave → Sonnet → Discord + event log)
- `catalyst_monitor.py` — catalyst calendar alerts (Haiku, 7-day horizon)
- `weekly_digest.py` — Sunday cross-coverage digest (Sonnet)
- `discord_setup.py` — one-time channel creation; run after adding new coverage
- `filter_material.py` — two-pass filter (Haiku Pass 1, Sonnet Pass 2 batched)
- `fetch_news.py` — Brave Search + X API
- `post_discord.py` — Discord REST API wrapper
- `cache.py` — SQLite article cache (dedup across runs)
- `config.py` — single source of truth for coverage, channels, queries, constants
- `utils.py` — retry decorator + logging setup

### Data flow
Brave Search → dedup → cache filter → Pass 1 (Haiku) → Pass 2 (Sonnet, batched) → Discord embed → event log → mark seen

### Key files
- `.env` — real credentials (gitignored)
- `scripts/channel_map.json` — Discord channel IDs (populated by discord_setup.py)
- `events/ticker_news/YYYY-MM-DD.md` — daily ticker event logs
- `events/macro/YYYY-MM-DD-close.md` — daily macro close logs
- `events/weekly/YYYY-MM-DD-digest.md` — weekly digests

---

## Cron Schedule (to configure)
```
# Asia Japan close
0 6 * * * /path/to/.venv/bin/python /path/to/scripts/daily_news.py asia_japan

# Asia Korea close
30 6 * * * /path/to/.venv/bin/python /path/to/scripts/daily_news.py asia_korea

# Asia HK close
0 8 * * * /path/to/.venv/bin/python /path/to/scripts/daily_news.py asia_hk

# US close (DST-aware — use 21:00 UTC as trigger; script resolves correct ET time)
0 21 * * 1-5 /path/to/.venv/bin/python /path/to/scripts/daily_news.py us
0 21 * * 1-5 /path/to/.venv/bin/python /path/to/scripts/macro_close.py
0 21 * * 1-5 /path/to/.venv/bin/python /path/to/scripts/catalyst_monitor.py

# Weekly digest — Sunday
0 22 * * 0 /path/to/.venv/bin/python /path/to/scripts/weekly_digest.py

# Self-evaluation — Monday (reviews prior week's outputs)
0 6 * * 1 /path/to/.venv/bin/python /path/to/scripts/self_eval.py

# Coverage updater — process any earnings flags from the prior day (daily, after US close)
30 21 * * 1-5 /path/to/.venv/bin/python /path/to/scripts/coverage_updater.py --pending

# Monthly catalysts refresh — 1st of month
0 7 1 * * /path/to/.venv/bin/python /path/to/scripts/coverage_updater.py --all --trigger monthly

# Quarterly full update — Feb/May/Aug/Nov 1st (add --apply after first review cycle)
0 7 1 2,5,8,11 * /path/to/.venv/bin/python /path/to/scripts/coverage_updater.py --all --trigger quarterly
```

---

## Active Debates (as of setup)

### Japan banks (8316, japan-banks sector)
- Core: BOJ normalization NII step-change — how far does BOJ go? (Bull: 1.0–1.5% by end-2026)
- Debate: Re-rating already priced? (Still trading 0.8–1.0x P/TBVPS vs. 1.5–2.0x global peers)
- Watch: Shunto wage outcome, USD/JPY level, US leveraged loan credit quality

### Google (GOOG)
- Debate: AI disruption vs. search monopoly durability
- Key catalyst: DOJ antitrust remedy; GCP margin trajectory toward 20%+

### Sea Limited (SE) / GRAB
- Shared: SE Asia macro (USD strength, EM consumer, China regulatory risk)
- SE: Shopee unit economics and SeaMoney credit quality
- GRAB: GrabFin monetization and path to positive FCF

### Gold miners
- Key debate: Gold price sustainability above $2,000 and AISC inflation impact

### Uranium miners
- Structural: AI power demand → nuclear restarts → uranium supply deficit

---

## Standing Watch Items
- BOJ policy rate and next meeting date
- USD/JPY level (key for 8316 overseas earnings translation)
- US 10yr yield (credit cycle signal for JPM, STAN)
- DOJ antitrust proceedings (GOOG)
- Japan Shunto wage negotiations (annual, March)
- US card delinquency data (monthly — JPM credit watch)
- DFAST stress test results (June — JPM capital return)

---

## User Preferences
- Python venv required (Debian/Ubuntu): `python3 -m venv .venv`
- Run scripts as: `.venv/bin/python scripts/daily_news.py`
- Credentials in `.env` (gitignored); `.env.example` has placeholder values only
- Dry-run flag: `--dry-run` skips Discord posting and cache writes
- No emojis in code or output unless explicitly requested
- For material covered-ticker, sector, or macro updates, write one markdown inbox file per ticker/story to `C:\Users\natba\OneDrive\@ Cowork\investing\inbox` (`/mnt/c/Users/natba/OneDrive/@ Cowork/investing/inbox` in WSL) using filename format `YYYY-MM-DD_TICKER_type_brief-description.md`, exact YAML front matter, and `status: inbox`; write nothing if the update is not material
