"""
Official company documents fetcher.

Goal: provide a normalized fetch layer for recent official documents that can feed
future daily news, earnings, or coverage-update workflows.

Current sources:
  1. SEC EDGAR recent filings (8-K, 6-K, 10-Q, 10-K, 20-F, 40-F)
  2. Company investor-relations pages (same-host links only)

Returns normalized document dicts:
  {
    "title": str,
    "url": str,
    "source": str,
    "published_at": "YYYY-MM-DD" | None,
    "doc_type": str,
    "text_snippet": str,
    "origin": "sec" | "ir",
    "form_type": str | None,
  }

Usage:
  .venv/bin/python scripts/official_docs_fetcher.py --coverage-key tickers/JPM
  .venv/bin/python scripts/official_docs_fetcher.py --coverage-key tickers/JPM --json
  .venv/bin/python scripts/official_docs_fetcher.py --all --limit 5
"""

import argparse
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

from config import COVERAGE, TICKER_META
from transcript_fetcher import (
    EDGAR_BASE,
    HEADERS_EDGAR,
    HEADERS_WEB,
    _clean_html_text,
    _download_limited,
    _extract_pdf_text,
    _split_url,
)
from utils import setup_logging

log = setup_logging("official_docs_fetcher")

OFFICIAL_FORM_TYPES = ("8-K", "6-K", "10-Q", "10-K", "20-F", "40-F")
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
IR_DOC_KEYWORDS = {
    "transcript": ["transcript", "conference call", "earnings call", "prepared remarks"],
    "presentation": ["presentation", "slide deck", "investor presentation"],
    "results": [
        "quarterly results",
        "earnings",
        "results release",
        "results pack",
        "financial results",
        "interim results",
        "quarterly report",
        "half-year report",
        "half year report",
        "interim report",
        "interim financial report",
        "financial report",
        "supplement",
    ],
    "annual_report": ["annual report", "20-f", "10-k", "integrated report"],
    "data_pack": ["data pack", "datapack", "excel", "xlsx", "xls"],
    "webcast": ["webcast"],
    "investor_day": ["investor day", "capital markets day", "agm", "seminar", "investor education"],
}

OFFICIAL_IR_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
DATE_PATTERNS = [
    re.compile(r"(20\d{2}-\d{2}-\d{2})"),
    re.compile(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2})", re.I),
    re.compile(r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+20\d{2})", re.I),
    re.compile(r"((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])"), # YYYYMMDD
    re.compile(r"\b(20\d{2}[01]\d[0-3]\d)\b"), # Alternate YYYYMMDD
]
QUARTER_OR_YEAR_RE = re.compile(
    r"\b(q[1-4]|fy\s?20\d{2}|20\d{2}|first quarter|second quarter|third quarter|fourth quarter|half year|interim)\b",
    re.I,
)
GENERIC_IR_LINK_TEXT = {
    "learn more",
    "read more",
    "view more",
    "see more",
    "click here",
    "download",
    "view",
    "here",
    "skip to main content",
}


def _clean_snippet(text: str, max_chars: int = 500) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:max_chars]


def _parse_date(text: str) -> str | None:
    if not text:
        return None
    for pattern in DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(1)
        try:
            if re.match(r"20\d{2}-\d{2}-\d{2}$", raw):
                return raw
            if re.match(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$", raw):
                return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
                
            cleaned = raw.replace(",", "")
            
            # Try formats
            for fmt in ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"]:
                try:
                    dt = datetime.strptime(cleaned, fmt)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass
        except ValueError:
            pass
    return None


def _classify_doc_type(text: str, form_type: str | None = None) -> str:
    hay = (text or "").lower()
    if form_type:
        form = form_type.upper()
        if form in {"10-K", "20-F", "40-F"}:
            return "annual_report"
        if form == "10-Q":
            return "quarterly_filing"
        if form in {"8-K", "6-K"}:
            if "data pack" in hay or ".xlsx" in hay or ".xls" in hay:
                return "data_pack"
            if "presentation" in hay:
                return "presentation"
            if "transcript" in hay or "conference call" in hay:
                return "transcript"
            return "filing"

    for doc_type, keywords in IR_DOC_KEYWORDS.items():
        if any(keyword in hay for keyword in keywords):
            return doc_type
    return "other"


def _within_days(date_str: str | None, days_back: int) -> bool:
    if not date_str:
        return True
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    return dt >= cutoff


def _is_ir_doc_candidate(link_text: str, href: str, context: str) -> bool:
    link_text_clean = re.sub(r"\s+", " ", (link_text or "")).strip().lower()
    href_lower = (href or "").lower()
    context_lower = (context or "").lower()
    combined = f"{link_text_clean} {href_lower} {context_lower}"
    combined_norm = combined.replace("-", " ").replace("_", " ")

    # Filter out Chinese language documents
    if any(token in combined for token in ["chinese", "zh-hk", "zh-tw", "zh-cn", "_tc.pdf", "_sc.pdf", "-chi.pdf", "_chi.pdf", "-cn.pdf", "trad_chi", "simp_chi", "繁體", "简体", "中文"]):
        return False

    keywords = [keyword for keywords in IR_DOC_KEYWORDS.values() for keyword in keywords]

    if link_text_clean in GENERIC_IR_LINK_TEXT and not any(keyword in href_lower.replace("-", " ") for keyword in keywords):
        return False

    has_keyword = any(keyword in combined_norm for keyword in keywords)
    is_pdf = href_lower.endswith(".pdf")
    has_doc_like_href = is_pdf or href_lower.endswith((".xlsx", ".xls")) or any(token in href_lower for token in ["results", "earnings", "presentation", "transcript", "report", "webcast", "datapack", "data-pack", "agm"])
    has_period_marker = bool(QUARTER_OR_YEAR_RE.search(combined))

    if is_pdf:
        return has_keyword
    return has_keyword and has_doc_like_href and has_period_marker and len(link_text_clean) >= 10


def _pick_edgar_document(index_json: dict, filing_url: str, form_type: str) -> tuple[str | None, str | None]:
    if form_type in {"10-Q", "10-K", "20-F", "40-F"}:
        return None, None

    docs = index_json.get("directory", {}).get("item", []) if "directory" in index_json else []
    best_doc = None
    best_label = None
    preferred_names = ["ex99", "99", "earnings", "results", "release", "presentation", "transcript"]

    for doc in docs:
        name = doc.get("name", "")
        lower = name.lower()
        if not lower or lower.endswith("/"):
            continue
        if any(token in lower for token in ["index", "headers", "hdr", ".xml", ".xsd", "schema", "def.xml", "lab.xml", "pre.xml"]):
            continue
        if re.match(r"r\d+\.htm$", lower):
            continue
        if not (lower.endswith(".htm") or lower.endswith(".html") or lower.endswith(".txt") or lower.endswith(".pdf")):
            continue
        score = 0
        if lower.endswith(".txt"):
            score -= 20
        if any(token in lower for token in preferred_names):
            score += 10
        if any(token in lower for token in ["exhibit", "supplement", "narrative"]):
            score += 5
        if form_type.lower().replace("-", "") in lower.replace("-", ""):
            score += 3
        if best_doc is None or score > best_doc[0]:
            best_doc = (score, filing_url + name)
            best_label = name

    if best_doc:
        return best_doc[1], best_label
    return None, None


def fetch_sec_recent_documents(
    sec_cik: str,
    *,
    days_back: int = 120,
    limit: int = 6,
    form_types: tuple[str, ...] = OFFICIAL_FORM_TYPES,
) -> list[dict]:
    if not sec_cik:
        return []

    cik_padded = sec_cik.lstrip("0").zfill(10)
    try:
        resp = requests.get(
            f"{EDGAR_BASE}/submissions/CIK{cik_padded}.json",
            headers=HEADERS_EDGAR,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("EDGAR submissions fetch failed for CIK %s: %s", sec_cik, exc)
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    acc_nos = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    docs: list[dict] = []

    for form_type, filed_at, acc_no, primary_document in zip(forms, dates, acc_nos, primary_docs):
        if form_type not in form_types or not _within_days(filed_at, days_back):
            continue

        acc_clean = acc_no.replace("-", "")
        filing_url = f"{SEC_ARCHIVES_BASE}/{cik_padded.lstrip('0')}/{acc_clean}/"
        index_url = f"{SEC_ARCHIVES_BASE}/{cik_padded.lstrip('0')}/{acc_clean}/index.json"

        chosen_url = filing_url + primary_document if primary_document else None
        chosen_name = primary_document or form_type

        try:
            idx_resp = requests.get(index_url, headers=HEADERS_EDGAR, timeout=20)
            idx_resp.raise_for_status()
            idx_json = idx_resp.json()
            alt_url, alt_name = _pick_edgar_document(idx_json, filing_url, form_type)
            if alt_url:
                chosen_url, chosen_name = alt_url, alt_name or chosen_name
        except Exception as exc:
            log.debug("EDGAR index fetch failed for %s: %s", acc_no, exc)

        if not chosen_url:
            continue

        try:
            data, content_type, final_url, encoding = _download_limited(
                chosen_url,
                headers=HEADERS_EDGAR,
            )
            lower_url = final_url.lower()
            if "pdf" in content_type or lower_url.endswith(".pdf"):
                text = _extract_pdf_text(data)
            elif lower_url.endswith((".xlsx", ".xls")) or any(token in (content_type or "").lower() for token in ["spreadsheet", "excel", "officedocument.spreadsheetml"]):
                text = f"{form_type} {chosen_name} {final_url}"
            else:
                text = _clean_html_text(data.decode(encoding, errors="replace"))
        except Exception as exc:
            log.debug("EDGAR document fetch failed for %s: %s", chosen_url[:120], exc)
            continue

        snippet = _clean_snippet(text)
        low = snippet.lower()
        if low.startswith("sec edgar submission") or low.startswith("xml ") or "xbrl document" in low:
            continue
        title = f"{form_type} {chosen_name}".strip()
        docs.append({
            "title": title,
            "url": final_url,
            "source": f"SEC EDGAR {form_type}",
            "published_at": filed_at,
            "doc_type": _classify_doc_type(f"{chosen_name} {snippet}", form_type=form_type),
            "text_snippet": snippet,
            "origin": "sec",
            "form_type": form_type,
        })
        if len(docs) >= limit:
            break

    return docs


def fetch_ir_recent_documents(
    ir_page: str,
    ticker_name: str,
    *,
    days_back: int = 180,
    limit: int = 6,
) -> list[dict]:
    if not ir_page:
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("beautifulsoup4 not installed, skipping IR scrape")
        return []

    try:
        resp = requests.get(ir_page, headers=HEADERS_WEB, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("IR page fetch failed for %s: %s", ir_page, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    # IR docs are often hosted on CDNs (q4cdn.com, av.sc.com), so we don't strictly bind the download host
    allowed_hosts = None
    
    def _find_year_context(anchor, href, link_text, context):
        import re
        # First, try to extract directly from the filename or link text (strongest signal)
        m = re.search(r'\b(19\d\d|20\d\d)\b', f"{href} {link_text}")
        if m:
            return m.group(1)
            
        for parent in anchor.parents:
            if parent.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'div', 'section']:
                text = parent.get_text()
                m = re.search(r'\b(19\d\d|20\d\d)\b', text)
                if m:
                    return m.group(1)
            prev = parent.find_previous_sibling(['h1', 'h2', 'h3', 'h4', 'h5'])
            if prev:
                m = re.search(r'\b(19\d\d|20\d\d)\b', prev.get_text())
                if m:
                    return m.group(1)
        return None

    candidates = []
    
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        link_text = " ".join(anchor.stripped_strings)
        context = " ".join(anchor.parent.stripped_strings) if anchor.parent else link_text
        if not _is_ir_doc_candidate(link_text, href, context):
            continue

        url = href if href.startswith("http") else urljoin(ir_page, href)
        
        published_at = _parse_date(context) or _parse_date(link_text) or _parse_date(href)
        fallback_year = _find_year_context(anchor, href, link_text, context)
        
        sort_date = "0000-00-00"
        if published_at:
            sort_date = published_at
            if not _within_days(published_at, days_back):
                continue
        elif fallback_year:
            sort_date = f"{fallback_year}-01-01"
            from datetime import datetime, timezone, timedelta
            cutoff_year = (datetime.now(timezone.utc) - timedelta(days=days_back)).year
            if int(fallback_year) < cutoff_year:
                continue
        else:
            # no date or year found, we penalize it severely in the sort but we can still try it
            # if we don't have enough documents. Or we can reject it.
            # Usually better to keep it if we are desperate, but sort it last.
            sort_date = "0000-00-00"
        
        combined_text = f"{link_text} {context} {href}".lower().replace("-", " ").replace("_", " ")
        doc_type = _classify_doc_type(combined_text)
        
        priority = 0
        if doc_type == "annual_report": priority = 10
        elif doc_type == "quarterly_filing": priority = 9
        elif doc_type == "results": priority = 8
        elif doc_type == "presentation": priority = 7
        elif doc_type == "data_pack": priority = 6
        elif doc_type == "transcript": priority = 5
        
        import re
        m_q = re.search(r'\b(q[1-4]|first|second|third|fourth|half year)\b', combined_text)
        if m_q:
            priority += 0.5
            
        candidates.append({
            "url": url,
            "link_text": link_text,
            "context": context,
            "published_at": published_at,
            "sort_date": sort_date,
            "priority": priority,
            "doc_type": doc_type
        })
        
    candidates.sort(key=lambda x: (x["sort_date"], x["priority"]), reverse=True)

    seen = set()
    docs = []
    
    for cand in candidates:
        url = cand["url"]
        if url in seen:
            continue
        seen.add(url)
        
        try:
            data, content_type, final_url, encoding = _download_limited(
                url,
                headers=HEADERS_WEB,
                max_bytes=OFFICIAL_IR_MAX_DOWNLOAD_BYTES,
                allowed_hosts=allowed_hosts,
            )
            if "pdf" in content_type or final_url.lower().endswith(".pdf"):
                text = _extract_pdf_text(data)
            else:
                text = _clean_html_text(data.decode(encoding, errors="replace"))
        except Exception as exc:
            log.debug("IR linked doc rejected/fetch failed for %s: %s", url[:120], exc)
            continue

        snippet = _clean_snippet(text)
        low = snippet.lower()
        if (
            len(snippet) < 80
            or low.startswith(("function ", "var ", "window."))
            or "optanonwrapper" in low
            or "createelement('script')" in low
            or "tealium" in low
        ):
            continue

        published_at = cand["published_at"]
        if not published_at and cand["sort_date"] != "0000-00-00":
            # If we only got a year context, format it as YYYY-01-01 so it doesn't show as 'undated'
            published_at = cand["sort_date"]

        title = cand["link_text"] or Path(final_url).name or f"IR document ({ticker_name})"
        docs.append({
            "title": title,
            "url": final_url,
            "source": f"IR page ({ticker_name})",
            "published_at": published_at,
            "doc_type": cand["doc_type"],
            "text_snippet": snippet,
            "origin": "ir",
            "form_type": None,
        })
        if len(docs) >= limit:
            break

    return docs


def _deduplicate_documents(documents: list[dict]) -> list[dict]:
    seen: set[str] = set()
    deduped = []
    for doc in documents:
        url = doc.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(doc)
    return deduped


def fetch_official_documents(
    coverage_key: str,
    *,
    days_back: int = 120,
    limit: int = 8,
) -> list[dict]:
    meta = TICKER_META.get(coverage_key, {})
    if not meta:
        return []

    name = coverage_key.split("/")[-1]
    docs = []
    docs.extend(fetch_sec_recent_documents(meta.get("sec_cik"), days_back=days_back, limit=limit))
    docs.extend(fetch_ir_recent_documents(meta.get("ir_page"), name, days_back=days_back, limit=limit))
    docs = _deduplicate_documents(docs)
    docs.sort(key=lambda d: (d.get("published_at") or "", d.get("origin") == "sec"), reverse=True)
    return docs[:limit]


def _render_text(documents: list[dict]) -> str:
    if not documents:
        return "No recent official documents found."
    parts = []
    for doc in documents:
        parts.append(
            f"- [{doc['doc_type']}] {doc['published_at'] or 'undated'} | {doc['source']}\n"
            f"  {doc['title']}\n"
            f"  {doc['url']}\n"
            f"  {doc['text_snippet'][:220]}"
        )
    return "\n\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch recent official company documents")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--coverage-key", help="Coverage key like tickers/JPM")
    group.add_argument("--all", action="store_true", help="Fetch for all ticker coverage keys")
    parser.add_argument("--days-back", type=int, default=120)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    keys = [args.coverage_key] if args.coverage_key else sorted(TICKER_META.keys())
    payload = {key: fetch_official_documents(key, days_back=args.days_back, limit=args.limit) for key in keys}

    if args.json:
        print(json.dumps(payload if args.all else payload[keys[0]], indent=2, ensure_ascii=False))
        return

    for key in keys:
        print(f"\n## {key}\n")
        print(_render_text(payload[key]))


if __name__ == "__main__":
    main()
