# Claude Code — Project Instructions

This is a personal equity research system automating daily news, earnings, and coverage updates for a multi-ticker portfolio, delivered via Discord. Cowork is the source of truth for portfolio context; OpenClaw is the news monitoring + event logging layer.

## Portfolio context (Cowork integration)

At the start of each conversation, read `PORTFOLIO_CONTEXT.md` (symlink to OneDrive). It contains the PM's live portfolio snapshot: macro regime, rates/FX, coverage list with thesis per name, sector drivers, today's key items, and upcoming catalysts. Updated daily at ~08:30 HKT by a separate system.

Use it as background when interpreting news or answering questions — frame answers through the portfolio lens. For the three overlapping names (STAN.L, 8316/SMFG, 1299/AIA), anchor on the PM's thesis in CONTEXT.md, then supplement with OpenClaw research. Do not override the PM's views with your own opinions.

## Architecture (v2)

```
Cowork (source of truth)
  → CONTEXT.md (daily, OneDrive) → OpenClaw reads as PORTFOLIO_CONTEXT.md
                                      ↓
OpenClaw monitors news → flags material events → Discord + update_log.md
                                      ↓
weekly_cowork_sync.py → weekly-sync-YYYY-MM-DD.md → OneDrive
                                      ↓
Cowork reads weekly sync → updates thesis/KPIs/catalysts → updates CONTEXT.md
```

Context flows FROM Cowork. Events flow BACK to Cowork. No local thesis/KPI/catalyst files maintained.

## Environment

- Python venv at `.venv/`; always use `.venv/bin/python scripts/<script>.py`
- Credentials in `.env` (gitignored); `.env.example` has placeholder keys
- Working directory: `/home/natbanyat/.openclaw/workspace-investing`

## Key files

| File | Purpose |
|------|---------|
| `PORTFOLIO_CONTEXT.md` | PM's portfolio snapshot (symlink → OneDrive, updated daily 08:30 HKT) |
| `scripts/config.py` | Single source of truth: COVERAGE, SEARCH_QUERIES, SPECIAL_CHANNELS, TICKER_META, constants |
| `scripts/channel_map.json` | Discord channel IDs (populated by discord_setup.py) |
| `coverage/<key>/update_log.md` | Per-name material event log (appended daily, read by weekly sync) |
| `coverage/earnings_calendar.json` | Structured earnings calendar per ticker |
| `scripts/article_cache.db` | SQLite cache (seen_articles, daily_runs, earnings_runs) |

## Coverage universe

**Tickers (US close):** JPM, TMX, STAN, GRAB, SE, FUTU, GOOG, MMYT
**Tickers (Asia):** 8316/SMFG (asia_japan), 1299/AIA (asia_hk)
**Sectors:** exchanges, gold-miners, uranium-miners (US), japan-banks (asia_japan)
**Markets:** japan (asia_japan), korea (asia_korea)

## Scripts

```
daily_news.py          — main orchestrator (4 close windows) → Discord + update_log.md
macro_close.py         — macro close summary → #macro-close
catalyst_monitor.py    — catalyst alerts → #catalyst-alerts
weekly_digest.py       — Sunday cross-coverage digest → #weekly-digest
weekly_cowork_sync.py  — Sunday Cowork sync → OneDrive weekly-sync-*.md
self_eval.py           — daily self-critique with human feedback → #self-eval
earnings_calendar.py   — refresh/show/set earnings calendar
earnings_processor.py  — full earnings pipeline (print + call + synthesis + Discord)
transcript_fetcher.py  — 5-level transcript cascade
discord_setup.py       — one-time channel setup (re-run when adding coverage)
```

## Coding conventions

- All scripts: `load_dotenv()` at top, `setup_logging()` from utils.py, `--dry-run` flag
- All Anthropic API calls use streaming (WSL2 NAT drops idle TCP connections)
- Claude calls: Haiku for extraction/filtering, Sonnet for synthesis
- Model IDs: `claude-haiku-4-5-20251001`, `claude-sonnet-4-6`
- No emojis in code or output
- Retry decorator via `utils.retry` on all Anthropic API calls
- Discord embeds: total char limit 6000; use `build_embed()` in post_discord.py
- `COVERAGE_ROOT` = `coverage/`; coverage keys like `tickers/JPM`, `sectors/gold-miners`

## Coverage directory (simplified)

```
coverage/<key>/
  update_log.md  — timestamped log of material events (appended by daily_news.py)
```

Legacy files (thesis.md, kpi_tree.md, debates.md, catalysts.md) have been exported to OneDrive for Cowork ingestion. They are no longer read by the pipeline.

## Event log paths

```
events/ticker_news/YYYY-MM-DD.md
events/macro/YYYY-MM-DD-close.md
events/weekly/YYYY-MM-DD-digest.md
events/eval/YYYY-MM-DD-eval.md
events/earnings/tickers_JPM_Q1_FY2026_2026-04-15.md
```

## Cron schedule (HKT, weekdays unless noted)

```
15:00  daily_news asia_japan       (1hr post Japan close)
15:30  daily_news asia_korea       (1hr post Korea close)
17:00  daily_news asia_hk          (1hr post HK close)
05:00* daily_news us + macro + catalysts  (1hr post US close)
05:30* earnings_processor
07:00  coverage_updater
08:03  self_eval (Mon-Sat)

Sunday:
13:00  earnings_calendar refresh
06:00* weekly_digest
06:30* weekly_cowork_sync → OneDrive

* Summer EDT. Winter EST = +1hr.
```

## Common commands

```bash
# Daily pipeline
.venv/bin/python scripts/daily_news.py us
.venv/bin/python scripts/daily_news.py asia_japan

# Earnings
.venv/bin/python scripts/earnings_calendar.py --refresh
.venv/bin/python scripts/earnings_processor.py --pending --dry-run

# Weekly Cowork sync
.venv/bin/python scripts/weekly_cowork_sync.py --dry-run

# Self-eval
.venv/bin/python scripts/self_eval.py --dry-run

# Setup
.venv/bin/python scripts/discord_setup.py
```

## Adding a new ticker

1. Add to `COVERAGE` in config.py (key, channel, category, close)
2. Add to `SEARCH_QUERIES` in config.py
3. Add to `TICKER_META` in config.py (sec_cik, ir_page, av_symbol)
4. Add to `earnings_calendar.json` (symbol, exchange, fiscal_year_end, entries: [])
5. Create `coverage/tickers/<TICKER>/update_log.md`
6. Run `discord_setup.py` to create the Discord channel
7. Ask Cowork to add the name to CONTEXT.md
