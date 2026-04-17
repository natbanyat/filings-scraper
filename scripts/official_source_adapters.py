"""
Official-source adapter interface for exchange and filing systems.

This module separates two concerns:
1. Exchange/filer endpoint knowledge (SEC / LSE / HKEX / TSE)
2. Company-site document discovery (handled elsewhere by official_docs_fetcher.py)

For now:
- SEC has a live document fetch path via EDGAR.
- LSE / HKEX / TSE expose endpoint probes and metadata templates so we can
  validate access, record learnings, and later add dedicated parsers without
  tangling the IR scraper.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import requests

from config import TICKER_META
from official_docs_fetcher import fetch_sec_recent_documents
from transcript_fetcher import HEADERS_EDGAR, HEADERS_WEB

SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
LSE_BASE = "https://www.londonstockexchange.com"
LSE_RNS_PDF_BASE = "https://www.rns-pdf.londonstockexchange.com"
HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en"
HKEX_DOC_HOST = "https://www1.hkexnews.hk/listedco/listconews/sehk/"
TSE_DISCLOSURE_URL = "https://www.jpx.co.jp/english/listing/disclosure/index.html"
TSE_TDNET_OVERVIEW_URL = "https://www.jpx.co.jp/english/equities/listing/disclosure/tdnet/index.html"
TSE_TDNET_API_INFO_URL = "https://www.jpx.co.jp/english/markets/paid-info-listing/tdnet/02.html"


@dataclass
class AdapterDefinition:
    name: str
    probe: Callable[[dict], dict]
    fetch_documents: Callable[[dict, int, int], list[dict]]


def _http_probe(url: str, *, headers: dict | None = None, timeout: int = 20) -> dict:
    try:
        resp = requests.get(url, headers=headers or HEADERS_WEB, timeout=timeout)
        status = resp.status_code
        content_type = resp.headers.get("Content-Type", "")
        return {
            "url": url,
            "status": status,
            "ok": status < 400,
            "final_url": resp.url,
            "content_type": content_type,
        }
    except Exception as exc:
        return {
            "url": url,
            "status": None,
            "ok": False,
            "final_url": None,
            "content_type": None,
            "error": str(exc),
        }


def _empty_fetch(_: dict, __: int, ___: int) -> list[dict]:
    return []


def _probe_sec(meta: dict) -> dict:
    cik = (meta.get("sec_cik") or "").zfill(10)
    cik_nolead = cik.lstrip("0")
    endpoints = []
    if cik:
        endpoints.append(_http_probe(f"{SEC_SUBMISSIONS_BASE}/CIK{cik}.json", headers=HEADERS_EDGAR))
        endpoints.append(_http_probe(f"{SEC_ARCHIVES_BASE}/{cik_nolead}/", headers=HEADERS_WEB))
    return {
        "adapter": "sec",
        "label": "SEC EDGAR",
        "endpoints": endpoints,
        "notes": [
            "Primary machine-readable endpoint is submissions/CIK##########.json.",
            "Document bodies live under sec.gov/Archives/edgar/data/<cik_without_leading_zeroes>/<accession_without_dashes>/.",
            "Best sources for deep dives are 10-K/10-Q and 8-K/6-K exhibits (press releases, earnings supplements, presentations).",
        ],
    }


def _fetch_sec(meta: dict, days_back: int, limit: int) -> list[dict]:
    return fetch_sec_recent_documents(meta.get("sec_cik"), days_back=days_back, limit=limit)


def _probe_lse(meta: dict) -> dict:
    symbol = meta.get("exchange_symbol") or ""
    slug = meta.get("exchange_slug") or ""
    company_page = f"{LSE_BASE}/stock/{symbol}/{slug}/company-page" if symbol and slug else None
    endpoints = []
    if company_page:
        endpoints.append(_http_probe(company_page, headers=HEADERS_WEB))
    endpoints.append(_http_probe(LSE_RNS_PDF_BASE, headers=HEADERS_WEB))
    return {
        "adapter": "lse",
        "label": "London Stock Exchange / RNS",
        "endpoints": endpoints,
        "notes": [
            "Company page is a stable identity endpoint but much of the announcement data is JS-rendered.",
            "RNS PDFs are served from rns-pdf.londonstockexchange.com/rns/<id>.pdf.",
            "For Standard Chartered, the company IR archive is currently the more practical source for results, transcripts, presentations, and data packs.",
        ],
    }


def _probe_hkex(meta: dict) -> dict:
    code = meta.get("exchange_code") or ""
    endpoints = [
        _http_probe(HKEX_SEARCH_URL, headers=HEADERS_WEB),
        _http_probe(HKEX_DOC_HOST, headers=HEADERS_WEB),
    ]
    return {
        "adapter": "hkex",
        "label": "HKEXnews",
        "endpoints": endpoints,
        "notes": [
            f"Official company code for this name is {code}." if code else "HKEX adapter expects exchange_code metadata.",
            "Search UI is public and accessible at search/titlesearch.xhtml.",
            "Announcement PDFs are commonly stored under www1.hkexnews.hk/listedco/listconews/sehk/YYYY/MMDD/<docid>.pdf.",
            "For deep dives, company IR sites often surface the same annual/interim materials in a more navigable archive than raw HKEX search results.",
        ],
    }


def _probe_tse(meta: dict) -> dict:
    code = meta.get("exchange_code") or ""
    endpoints = [
        _http_probe(TSE_DISCLOSURE_URL, headers=HEADERS_WEB),
        _http_probe(TSE_TDNET_OVERVIEW_URL, headers=HEADERS_WEB),
        _http_probe(TSE_TDNET_API_INFO_URL, headers=HEADERS_WEB),
    ]
    return {
        "adapter": "tse",
        "label": "JPX / TDnet",
        "endpoints": endpoints,
        "notes": [
            f"Official TSE code for this name is {code}." if code else "TSE adapter expects exchange_code metadata.",
            "JPX exposes a free English company announcements service and overview pages.",
            "TDnet API exists but is a paid service; production-grade full ingestion may need either paid access or company-site IR archives.",
            "For Japan names, company IR pages may remain the most practical source for presentations, transcripts, and data books unless TDnet access is added.",
        ],
    }


ADAPTERS: dict[str, AdapterDefinition] = {
    "sec": AdapterDefinition("sec", _probe_sec, _fetch_sec),
    "lse": AdapterDefinition("lse", _probe_lse, _empty_fetch),
    "hkex": AdapterDefinition("hkex", _probe_hkex, _empty_fetch),
    "tse": AdapterDefinition("tse", _probe_tse, _empty_fetch),
}


def probe_exchange_adapter(coverage_key: str) -> dict:
    meta = TICKER_META.get(coverage_key, {})
    adapter_name = meta.get("exchange_adapter")
    if not adapter_name:
        return {
            "adapter": None,
            "label": "none",
            "endpoints": [],
            "notes": ["No exchange adapter configured."],
        }
    adapter = ADAPTERS.get(adapter_name)
    if not adapter:
        return {
            "adapter": adapter_name,
            "label": adapter_name,
            "endpoints": [],
            "notes": [f"Unknown adapter: {adapter_name}"],
        }
    return adapter.probe(meta)


def fetch_exchange_documents(coverage_key: str, *, days_back: int = 400, limit: int = 20) -> list[dict]:
    meta = TICKER_META.get(coverage_key, {})
    adapter_name = meta.get("exchange_adapter")
    adapter = ADAPTERS.get(adapter_name) if adapter_name else None
    if not adapter:
        return []
    return adapter.fetch_documents(meta, days_back, limit)
