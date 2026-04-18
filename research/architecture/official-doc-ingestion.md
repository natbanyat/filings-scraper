# Official document ingestion, first pass

## Goal
Build a higher-cred document layer for coverage names so the pipeline can rely less on secondary news and more on:
- official company IR materials
- SEC filings
- eventually exchange announcement systems

## What exists now
`scripts/official_docs_fetcher.py` provides a normalized fetch layer for recent official documents with this schema:

```json
{
  "title": "Q1 2026 earnings presentation",
  "url": "https://...",
  "source": "IR page (JPM)",
  "published_at": "2026-04-15",
  "doc_type": "presentation",
  "text_snippet": "...",
  "origin": "ir",
  "form_type": null
}
```

Current sources:
1. SEC EDGAR recent filings for covered SEC filers
2. Company IR pages, same-host links only

## Adapter layer and live probe harness
- `scripts/official_source_adapters.py`
  - adds a metadata-driven adapter interface for SEC, LSE / RNS, HKEXnews, and TSE / TDnet
  - SEC currently has a live document fetch path
  - LSE / HKEX / TSE currently expose endpoint probes plus URL/storage templates for the next parser pass

- `scripts/official_docs_probe.py`
  - probes configured company websites plus exchange endpoints
  - fetches reachable official docs for first-look setup
  - writes a persistent site-learning report capturing:
    - naming conventions
    - storage paths
    - access constraints
    - periodic document families observed in the wild

- `research/architecture/official-source-site-learnings.md`
  - first live report for STAN, HSBC, JPM, AIA, and GOOG

- `scripts/official_doc_corpus.py` and `council_ui/official_docs_server.py`
  - add a local corpus manager plus browser UI
  - store downloaded files in the OneDrive-backed context folder with per-company / per-doc-type organization
  - track runs and documents in SQLite for incremental follow-up

## Why this helps
The current news pipeline is still vulnerable to weak publishers and same-story rewrites.
An official-doc layer gives us a cleaner base for:
- earnings processing
- catalyst tracking
- coverage updates
- management-guidance extraction
- IR deck / annual-report change detection

## Design principles
- Prefer official sources over media coverage
- Same-host rules for IR scraping by default
- Keep the fetch layer normalized and reusable
- Keep exchange-specific logic modular, not hardcoded into one giant scraper

## Recommended next steps

### Step 1, wire into earnings and updates
Use `official_docs_fetcher.fetch_official_documents()` before general web/news fetches for:
- earnings periods
- investor day windows
- annual report season

### Step 2, add exchange announcement adapters
Add optional metadata-driven adapters for official exchange systems:
- HKEXnews for AIA / HSBC HK disclosures
- TDnet / JPX for Japan-listed names
- RNS / LSE for Standard Chartered
- SEDAR+ for TMX and Canadian issuers

These should live as separate small fetch functions behind per-ticker metadata, not one crawler with branching everywhere.

### Step 3, persistent storage and diffing
Store normalized documents in SQLite with:
- coverage_key
- url
- published_at
- doc_type
- content hash
- fetched_at

Then add diff logic for:
- changed guidance language
- new slide decks
- annual report wording changes
- capital return / regulatory updates

### Step 4, promote official docs into materiality filter
Pass official-doc snippets into Pass 1 and Pass 2 as a high-priority source bucket.
If an official document exists for a topic, secondary media should be demoted unless it adds genuinely incremental context.

## Pinned architecture direction, updated after external review
The latest architecture review does not change the core direction. It sharpens it.

### Keep
- Deterministic ingestion core, agent-assisted control plane
- HTTP-first discovery and download path
- Browser automation only as a fallback for JS, auth, or click-download flows
- Official docs as a higher-priority source bucket than general web/news fetches
- Crawler-first, browser-second operating model

### Add next
1. **Source registry separate from `TICKER_META`**
   - maintain explicit source records with seed URLs, allowed domains, crawl mode, download mode, parse profile, poll frequency, and owner
   - this should prevent crawler logic from spreading into one-off ticker metadata hacks
2. **Manifest DB as the real system of record**
   - extend corpus metadata to include source URL, final URL, response headers, content length, ETag, Last-Modified, download status, parse status, and parse quality flags
   - preserve raw originals in immutable storage and keep raw response artifacts where failure investigation matters
   - the raw file store remains immutable storage, but the manifest becomes the truth for tracking state
3. **Parser / normalization workers**
   - PDFs: native extraction first, table extraction where possible, OCR only as fallback
   - XLSX: workbook metadata, per-sheet extraction, normalized tables, and flags for hidden/protected sheets
4. **True delta and version tracking**
   - track `new`, `unchanged`, `updated`, `duplicate`, `moved`, `failed_download`, and `failed_parse`
   - do not treat a new URL as a new document by default, use hash plus metadata plus parsed-text comparison where needed
5. **AI-ready derived artifacts**
   - clean chunks, extracted tables, metadata JSON, short summaries, and delta summaries vs prior versions
   - for investing use cases, the change log is often more valuable than the raw file itself
6. **Browser fallback lane, not browser-first architecture**
   - use Playwright only when direct HTTP fails or the site truly requires JS/session state
   - keep the easy 80% on the simple deterministic path
7. **Scheduler / queue and operational guardrails**
   - add an explicit job scheduler/queue instead of relying only on ad hoc subprocess launches
   - enforce per-domain concurrency limits, retries with backoff, and clear transient vs permanent failure classes
   - keep provenance, parser confidence, and policy notes per source, and never overwrite raw originals

### Framework choice
- If the source mix stays mostly public, static, and Python-friendly, `Scrapy + HTTPX + Playwright fallback` is the clean low-complexity choice.
- If the source mix grows toward more JS-heavy or auth-heavy IR/document centers, `Crawlee + HTTPX + Playwright fallback` is the better long-run default.
- Either way, the parser/storage/delta layers matter more than the crawler brand.

## v2 implementation status (2026-04-18)

All seven architecture items above are now implemented in code:

1. **Source registry** (`scripts/official_source_registry.py`)
   - `SourceEntry` dataclass with all required fields
   - YAML backing at `scripts/official_source_registry.yaml`, auto-bootstrapped from TICKER_META
   - 25 entries covering all tickers (SEC, LSE, HKEX, TSE, IR sources)
   - `get_registry()` singleton, `for_coverage_key()`, `all_enabled()`

2. **Manifest DB v2** (`scripts/official_doc_corpus.py`)
   - `documents` table: added `content_length`, `etag`, `last_modified`, `first_seen_at`,
     `download_status`, `parse_status`, `parse_quality_flags`, `delta_state`,
     `failure_type`, `retry_count`, `retry_after`
   - New `document_parses` table for parser outputs (parse_id, parser_name, text_chars,
     page_count, table_count, quality_flags, artifact_path)
   - New `jobs` table for background job queue (job_id, job_type, status,
     company_key, doc_id, failure_type, retry_count)
   - `_migrate_schema()` for backward-compatible upgrade of existing DBs
   - `download_with_fallbacks()` now captures ETag, Last-Modified, Content-Length

3. **Parser workers** (`scripts/official_doc_parsers.py`)
   - `PDFParser`: pypdf native text + optional pdfplumber table extraction;
     OCR-needed flag via text-density heuristic (`< 150 chars/page`)
   - `XLSXParser`: openpyxl workbook metadata, per-sheet extraction, hidden/protected detection
   - `HTMLParser`: BeautifulSoup + Trafilatura normalization for SEC exhibit pages and text docs,
     with heading metadata and HTML table preview extraction
   - `parse_document(path, content_type)` dispatcher
   - `save_parse_artifact()` / `load_parse_artifact()` for JSON artifact persistence
   - Quality flags: `ocr_needed`, `low_text_density`, `tables_found`, `has_hidden_sheets`,
     `has_protected_sheet`, `partial_text`, `empty_output`, `html_normalized`

4. **Delta tracking** (`run_scrape()`, `compute_delta_state()`, `find_by_sha256()`)
   - States: `new`, `unchanged`, `updated`, `duplicate`, `moved`, `failed_download`, `failed_parse`
   - Hash-based: unchanged if SHA256 matches existing record; duplicate if SHA256 matches different URL
   - `unchanged` docs skip re-download; `updated` docs re-save with new hash

5. **Browser fallback lane**
   - `download_with_fallbacks(url, target_dir, download_mode=)` now routes `download_mode=browser`
     through a Playwright-backed downloader when available
   - Direct browser-triggered downloads are handled, including pages that start a file download instead of rendering inline
   - `download_mode=mixed` preserves HTTP-first behavior while allowing browser fallback on failure
   - `browser_required` download_status is still recorded when the browser dependency is unavailable

6. **AI-ready derived artifacts**
   - parse jobs now write `_meta/derived/<doc_id>.json` alongside parse artifacts
   - derived artifacts include normalized excerpts, chunked text, table previews, and a lightweight delta summary vs the prior parse artifact

7. **UI** (`council_ui/official_docs_server.py`)
   - `/api/registry` — full source registry as JSON
   - `/api/jobs` — job queue with status/type filters
   - `/api/parse-records` — document parse history
   - `/api/summary` — now includes `parse_stats`, `recent_jobs`
   - Dashboard: pipeline stats (parse_status, delta_state, job queue counts),
     delta-state and parse-status columns in documents table, jobs card with tab filter,
     source registry card, download_mode=browser highlighted in warn color

8. **New CLI commands** (`official_doc_corpus.py`)
   - `parse [--limit N] [--doc-id ID]` — run pending parse jobs
   - `list-jobs [--status STATUS]` — view job queue
   - `stats` — parse/delta/job aggregate counts
   - `run --parse` — parse inline after scrape

## Remaining limits
- Non-SEC exchange filings (LSE, HKEX, TSE) still not fetched — probe/scaffold only
- IR date extraction is heuristic and misses some page layouts
- Browser automation now works for browser-routed downloads, but listing-page discovery for exchanges like LSE is still limited by the upstream fetcher/adapters
- Corpus incrementality now uses stored `ETag` / `Last-Modified` validators when available, but legacy rows without validators still fall back to manifest/file-presence shortcuts
- Derived artifacts are lightweight and local-first today, not yet full research summaries or embedding-ready vector indexes

## Practical target state
A daily pipeline should ideally work in this order:
1. official docs
2. official exchange disclosures
3. top-tier wires
4. broader search only as a fallback

Discovery should ideally work in this order:
1. sitemap or robots-discovered sitemap
2. known archive / library pages
3. predictable listing pages
4. internal JSON/XHR/API endpoints
5. browser-rendered inspection
6. site search only if necessary

And the document pipeline should ideally work in this order:
1. source registry
2. scheduler / queue
3. HTTP-first discovery and download
4. Playwright fallback only when needed
5. manifest DB + immutable raw storage
6. parser / normalization workers
7. delta summaries and research-facing outputs

That should materially improve source quality, reduce false positives, and turn the corpus from a file cabinet into a usable research substrate.
