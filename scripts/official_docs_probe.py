"""
Probe official document sources for one or more coverage names and record site learnings.

This is intended for:
- deep-dive / first-look setup
- validating whether official sources are fetchable
- recording periodic-document naming/storage patterns

Usage:
  .venv/bin/python scripts/official_docs_probe.py --coverage-key tickers/STAN
  .venv/bin/python scripts/official_docs_probe.py --coverage-keys tickers/STAN,tickers/HSBC,tickers/JPM,tickers/1299,tickers/GOOG --write-report research/architecture/official-source-site-learnings.md
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from config import TICKER_META
from official_docs_fetcher import fetch_ir_recent_documents
from official_source_adapters import fetch_exchange_documents, probe_exchange_adapter, _http_probe
from utils import setup_logging

log = setup_logging("official_docs_probe")

DEFAULT_KEYS = [
    "tickers/STAN",
    "tickers/HSBC",
    "tickers/JPM",
    "tickers/1299",
    "tickers/GOOG",
]


def _doc_family(doc: dict) -> str:
    text = f"{doc.get('doc_type', '')} {doc.get('title', '')} {doc.get('url', '')}".lower()
    if any(token in text for token in ["data pack", "datapack", ".xlsx", ".xls"]):
        return "data_pack"
    if "transcript" in text:
        return "transcript"
    if "presentation" in text or "slide" in text or "investor day" in text or "agm" in text:
        return "presentation"
    if "annual report" in text or "integrated report" in text or "10-k" in text or "20-f" in text or "40-f" in text:
        return "annual_report"
    if "10-q" in text or "quarterly" in text or "interim" in text or "q1" in text or "q2" in text or "q3" in text or "q4" in text:
        return "quarterly_report"
    if "8-k" in text or "6-k" in text or "announcement" in text or "press release" in text or "rns" in text:
        return "major_announcement"
    return doc.get("doc_type") or "other"


def _infer_storage_patterns(docs: list[dict], probes: list[dict]) -> list[str]:
    urls = [d.get("url", "") for d in docs if d.get("url")]
    notes: list[str] = []
    joined = "\n".join(urls)

    if "/Archives/edgar/data/" in joined:
        notes.append("SEC files live under `/Archives/edgar/data/<cik_without_leading_zeroes>/<accession_without_dashes>/...`.")
    if "/-/files/hsbc/investors/hsbc-results/" in joined:
        notes.append("HSBC stores result documents under `/-/files/hsbc/investors/hsbc-results/<year>/<period>/pdfs/hsbc-holdings-plc/...`.")
    if "/uploads/sites/66/content/docs/standard-chartered-plc-" in joined or "/uploads/sites/66/others/standard-chartered-plc-" in joined:
        notes.append("Standard Chartered stores archive files under `/uploads/sites/66/content/docs/` and `/uploads/sites/66/others/`, usually with filenames like `standard-chartered-plc-q3-2025-presentation.pdf` or `...-data-pack.xlsx`.")
    if "/content/dam/group-wise/en/docs/investor-relations/" in joined:
        notes.append("AIA stores investor documents under `/content/dam/group-wise/en/docs/investor-relations/<year>/...`.")
    if any((p.get("url") or "").startswith("https://abc.xyz/investor/Earnings/default.aspx") and p.get("status") == 403 for p in probes):
        notes.append("Alphabet's top-level IR homepage is accessible, but deeper results/event pages appear Cloudflare-protected (403) from this environment.")

    if not notes and urls:
        hosts = Counter(urlparse(url).netloc for url in urls)
        top_hosts = ", ".join(host for host, _ in hosts.most_common(3))
        notes.append(f"Observed document hosts: {top_hosts}.")
    return notes


def _summarize_access(probes: list[dict]) -> list[str]:
    lines = []
    for probe in probes:
        status = probe.get("status")
        url = probe.get("url")
        final_url = probe.get("final_url")
        suffix = f" → {final_url}" if final_url and final_url != url else ""
        lines.append(f"- `{status}` {url}{suffix}")
    return lines


def _render_doc_examples(docs: list[dict], limit: int = 8) -> list[str]:
    lines = []
    for doc in docs[:limit]:
        family = _doc_family(doc)
        published = doc.get("published_at") or "undated"
        lines.append(f"- [{family}] {published} | {doc.get('title','(untitled)')}\n  - {doc.get('url','')}" )
    return lines


def build_probe_result(coverage_key: str, *, days_back: int, limit: int) -> dict:
    meta = TICKER_META[coverage_key]
    company_name = meta.get("company_name", coverage_key.split("/")[-1])

    exchange_probe = probe_exchange_adapter(coverage_key)
    site_probes = [_http_probe(url) for url in meta.get("website_probe_urls", [meta.get("ir_page")]) if url]

    exchange_docs = fetch_exchange_documents(coverage_key, days_back=days_back, limit=limit)
    ir_docs = fetch_ir_recent_documents(meta.get("ir_page"), company_name, days_back=days_back, limit=limit)

    seen_urls: set[str] = set()
    all_docs: list[dict] = []
    for doc in exchange_docs + ir_docs:
        url = doc.get("url") or ""
        if url and url not in seen_urls:
            seen_urls.add(url)
            all_docs.append(doc)
    all_docs.sort(key=lambda d: (d.get("published_at") or "", d.get("origin") == "sec"), reverse=True)

    family_counts = Counter(_doc_family(doc) for doc in all_docs)
    pattern_notes = _infer_storage_patterns(all_docs, site_probes)

    return {
        "coverage_key": coverage_key,
        "company_name": company_name,
        "exchange_adapter": meta.get("exchange_adapter"),
        "ir_page": meta.get("ir_page"),
        "exchange_probe": exchange_probe,
        "website_probes": site_probes,
        "family_counts": dict(family_counts),
        "exchange_docs": exchange_docs,
        "ir_docs": ir_docs,
        "all_docs": all_docs,
        "pattern_notes": pattern_notes,
    }


def render_markdown(results: list[dict], *, days_back: int, limit: int) -> str:
    out = [
        "# Official source site learnings",
        "",
        f"Generated from live probes. Window: last {days_back} days. Per-source limit: {limit}.",
        "",
        "This file records how official document sources behave for covered names, including endpoint access, naming/storage patterns, and what document families are actually reachable.",
        "",
    ]

    for result in results:
        out.extend([
            f"## {result['coverage_key']} — {result['company_name']}",
            "",
            f"- Exchange adapter: `{result['exchange_adapter']}`",
            f"- IR page: `{result['ir_page']}`",
            "",
            "### Endpoint checks",
            "",
            f"**Exchange / filing endpoints ({result['exchange_probe']['label']})**",
            "",
            *[f"- `{probe.get('status')}` {probe.get('url')}" for probe in result['exchange_probe'].get('endpoints', [])],
            "",
            "**Company website endpoints**",
            "",
            *_summarize_access(result["website_probes"]),
            "",
            "### Recorded learnings",
            "",
            *[f"- {note}" for note in result["pattern_notes"]],
            *[f"- {note}" for note in result["exchange_probe"].get("notes", [])],
            "",
            "### Observed document families",
            "",
        ])
        if result["family_counts"]:
            for family, count in sorted(result["family_counts"].items()):
                out.append(f"- `{family}`: {count}")
        else:
            out.append("- None detected in this probe window.")

        out.extend([
            "",
            "### Sample documents",
            "",
        ])
        examples = _render_doc_examples(result["all_docs"] or result["ir_docs"] or result["exchange_docs"])
        if examples:
            out.extend(examples)
        else:
            out.append("- No documents captured in this run.")

        out.extend(["", "### Periodic tracking implication", ""])
        adapter = result["exchange_adapter"]
        if adapter == "sec":
            out.append("- Track SEC cadence via submissions JSON and prioritize 10-K/10-Q plus 8-K exhibits for earnings and major events.")
        elif adapter == "lse":
            out.append("- Use the company IR archive as the primary periodic tracker; treat LSE/RNS as a supplementary announcement layer.")
        elif adapter == "hkex":
            out.append("- Use company IR archives for packaged results materials and HKEXnews as the official announcement backstop for filing-time validation.")
        elif adapter == "tse":
            out.append("- Use company IR archives for packaged documents unless TDnet access is added; JPX endpoints are better as discovery/probe surfaces than complete free archives.")
        out.extend(["", "---", ""])

    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe official source endpoints and record site learnings")
    parser.add_argument("--coverage-key", help="Single coverage key")
    parser.add_argument("--coverage-keys", help="Comma-separated coverage keys")
    parser.add_argument("--days-back", type=int, default=500)
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-report", help="Write markdown report to this path")
    args = parser.parse_args()

    if args.coverage_key:
        keys = [args.coverage_key]
    elif args.coverage_keys:
        keys = [k.strip() for k in args.coverage_keys.split(",") if k.strip()]
    else:
        keys = DEFAULT_KEYS

    results = [build_probe_result(key, days_back=args.days_back, limit=args.limit) for key in keys]

    if args.json:
        print(json.dumps(results[0] if len(results) == 1 else results, indent=2, ensure_ascii=False))
        return

    markdown = render_markdown(results, days_back=args.days_back, limit=args.limit)
    if args.write_report:
        path = Path(args.write_report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        log.info("Wrote report to %s", path)
    print(markdown)


if __name__ == "__main__":
    main()
