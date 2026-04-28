# filings-scraper

Local-first official document corpus and browser UI for equity research.

## What it includes
- official document fetch + corpus manager
- local UI for runs, documents, and parse jobs
- systemd units for the docs UI and parse-drain timer
- source metadata overrides for custom names

## Key entrypoints
- `scripts/official_doc_corpus.py` — corpus CLI
- `scripts/official_docs_fetcher.py` — fetch/discovery layer
- `council_ui/official_docs_server.py` — local docs UI
- `scripts/custom_tickers.json` — custom source mappings

## Quick start
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

.venv/bin/python scripts/official_doc_corpus.py --help
.venv/bin/python council_ui/official_docs_server.py
```

Then open: `http://127.0.0.1:8876`

## Notes
- Corpus storage path is configured in `scripts/config.py`.
- SEC is the strongest live adapter today; many non-SEC names still rely on IR archives.
- Some IR sites may require future curl/browser fallbacks if Python `requests` times out.
