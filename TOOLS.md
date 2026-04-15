# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.
# TOOLS.md

## Purpose
This file defines local workspace conventions for the investing agent.

## Main workflows
1. Ticker news updates linked to thesis and KPI trees
2. Macro close updates
3. Deep research on a single name or sub-sector

## Workspace layout
- `coverage/tickers/<ticker>/` = durable company files
- `coverage/sectors/<sector>/` = durable sector files
- `coverage/markets/<market>/` = durable market files
- `news/tickers/` = dated ticker news updates
- `news/macro/` = dated macro close notes
- `research/companies/` = deep dives on single names
- `research/sub-sectors/` = sector and sub-sector deep research
- `templates/` = reusable output templates
- `memory/YYYY-MM-DD.md` = daily working notes
- `MEMORY.md` = durable preferences and recurring frameworks

## Ticker coverage conventions
For each ticker, maintain:
- `thesis.md`
- `kpis.md`
- `debates.md`
- `questions.md`
- `updates.md`

Only update `updates.md` when something meaningfully changes the view, KPI path, or key 
debate.

## Coverage system

All analysis should connect events to the KPI tree.

Coverage files store durable views.
Event files store daily developments.

Events → KPI → thesis → implication.
## Sector / market coverage conventions
For each sector or market, maintain:
- `thesis.md`
- `kpis.md`
- `debates.md`
- `watchlist.md`

## News update conventions
Ticker news updates should answer:
- what happened
- why it matters
- which thesis node it affects
- which KPI(s) it affects
- whether the event is incremental, confirming, or view-changing

Do not save raw headlines without analysis.

## External inbox handoff
When a covered ticker, sector, or macro item has a material update, write one markdown file per ticker per story to:
- Windows: `C:\Users\natba\OneDrive\@ Cowork\investing\inbox`
- WSL: `/mnt/c/Users/natba/OneDrive/@ Cowork/investing/inbox`

Filename format:
- `YYYY-MM-DD_TICKER_type_brief-description.md`
- Examples: `2026-04-15_STAN_news_africa-divestiture-update.md`, `2026-04-15_SECTOR_commentary_japan-rate-hike-implications.md`

Required front matter:
```yaml
---
type: [news | research | transcript | filing | commentary | data]
ticker: [PRIMARY_TICKER or "SECTOR" or "MACRO"]
tickers_mentioned: [TICKER1, TICKER2, ...]
source: [publication name]
source_url: [URL or "N/A"]
date: [YYYY-MM-DD of source material]
processed_date: [YYYY-MM-DD today]
priority: [high | medium | low]
tags: [comma-separated keywords]
status: inbox
---
```

Priority rules:
- high: earnings surprise, guidance change, M&A, regulatory shift, or anything that directly challenges or confirms a thesis
- medium: peer results, sector trends, management changes, relevant macro
- low: background context, tangential news

Only write a file if the update is material. If nothing is material on a run, write nothing.

## Macro close conventions
Macro close updates should focus on:
- what moved
- what likely drove it
- what changed in market narrative
- implications for covered sectors, markets, and names

Avoid generic headline summaries.

## Deep research conventions
Deep research should be saved separately from daily news.
Use:
- `research/companies/` for single-name work
- `research/sub-sectors/` for peer sets, screens, and thematic/sub-sector work

## Source conventions
- Prefer primary sources first
- Use live web research for time-sensitive facts
- Label inferred consensus clearly
- Record source date where relevant
- Do not save unsupported claims as facts

## Writing conventions
- Default to markdown
- Be concise and structured
- Use headings generously
- Use tables only when comparison is clearer than bullets
- Keep durable files compact and updateable

## Update discipline
After meaningful work:
- update the relevant durable coverage file if the view changed
- update `memory/YYYY-MM-DD.md` for session notes
- update `MEMORY.md` only for durable lessons or standing preferences

## Guardrails
- Do not scatter the same idea across many files unnecessarily
- Do not turn daily notes into long-term memory without distillation
- Do not store secrets or credentials here
- Keep this file practical and local
