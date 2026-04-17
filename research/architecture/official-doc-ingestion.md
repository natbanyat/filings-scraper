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

## Limits of the first pass
- IR date extraction is heuristic and will miss some page layouts
- non-SEC exchange filings are not yet implemented
- some IR sites will still require per-site tuning
- PDFs are text-extracted only, with no table normalization yet

## Practical target state
A daily pipeline should ideally work in this order:
1. official docs
2. official exchange disclosures
3. top-tier wires
4. broader search only as a fallback

That should materially improve source quality and reduce false positives.
