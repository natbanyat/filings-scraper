# Official document corpus and local UI

## Goal
Turn the official-doc fetch layer into a usable local research corpus for first looks and deep dives.

## What this adds

### Storage layer
- `scripts/official_doc_corpus.py`
  - manages a local corpus rooted at:
    - WSL: `/mnt/c/Users/natba/OneDrive/@ Cowork/openclaw-investing-context`
    - Windows: `C:\Users\natba\OneDrive\@ Cowork\openclaw-investing-context`
  - stores files by company and document family
  - maintains a SQLite index at `_meta/corpus.db`
  - tracks scrape runs, progress, and downloaded documents
  - supports:
    - `incremental` runs for periodic follow-up
    - `backfill` runs for first-look corpus creation

### Web interface
- `council_ui/official_docs_server.py`
  - local aiohttp UI for:
    - browsing downloaded files
    - monitoring runs
    - triggering new scrapes
    - serving local files directly from the corpus folder

## Storage layout

```text
openclaw-investing-context/
  _meta/
    corpus.db
  JPM_jpmorgan-chase-co/
    annual_reports/
    quarterly_reports/
    presentations/
    transcripts/
    major_announcements/
    data_packs/
    other/
    _meta/
      site_learning.md
      site_learning.json
```

## Retrieval policy
1. Exchange/filer adapter first when available
   - SEC is live today
   - LSE / HKEX / TSE are still probe/discovery layers
2. Company IR archive second
3. Per-file download fallbacks
   - direct URL
   - alternate request headers
   - queryless URL variant when the source URL has query parameters

## Current strengths
- Good for JPM / GOOG via SEC
- Good for STAN / HSBC / AIA via company IR archives
- Captures PDFs, HTML docs, text filings, and Excel datapacks
- Records site-learning notes per company under each company `_meta/`

## Current limits
- LSE / HKEX / TSE are not yet full historical filing collectors
- IR archives remain the practical backbone for non-SEC names
- Some JS-heavy or Cloudflare-protected sites may still require browser automation or an external fallback later
- Incremental dedupe is URL/path based, not full remote change detection

## Suggested next step
Add site-specific backfill collectors for:
- STAN
- HSBC
- AIA
- SMFG / MUFG / Mizuho

That would turn the current discovery-first design into a true multi-year official archive system.

## Runbook

### Run a local scrape
```bash
.venv/bin/python scripts/official_doc_corpus.py run --coverage-key tickers/JPM --mode backfill
```

### List recent runs
```bash
.venv/bin/python scripts/official_doc_corpus.py list-runs
```

### Start the local UI
```bash
.venv/bin/python council_ui/official_docs_server.py
```

Then open:
- `http://127.0.0.1:8876`
