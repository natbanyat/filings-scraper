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

## Limits of the first pass
- IR date extraction is heuristic and will miss some page layouts
- non-SEC exchange filings are not yet implemented
- some IR sites will still require per-site tuning
- PDFs are text-extracted only, with no table normalization yet
- corpus incrementality is still mostly URL/path based, not full remote change detection

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
