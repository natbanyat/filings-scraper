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
from urllib.parse import unquote, urljoin, urlparse

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

COMPANY_DOC_RULES = {
    "PRU": {
        "annual_report": re.compile(r"\b(?:ar\s?20\d{2})\b", re.I),
        "semiannual_report": re.compile(r"\b(?:hy\s?20\d{2}\b.*\breport\b|hy\b.*\bfinancial report\b)\b", re.I),
        "non_english_file": re.compile(r"(?:^|[-_.])(tc|sc|cn|chi|traditional|simplified|chinese)(?:[-_.]|$)", re.I)
    }
}

NON_ENGLISH_TEXT_RE = re.compile(
    r"\b(chinese|traditional|simplified|zh[-_ ]?(?:hk|tw|cn)|trad(?:itional)?(?: chinese)?|simp(?:lified)?(?: chinese)?)\b",
    re.I,
)

ANNUAL_REPORT_RE = re.compile(
    r"\b(?:annual report|latest annual report|integrated report|10-k|20-f|40-f)\b",
    re.I,
)
SEMIANNUAL_REPORT_RE = re.compile(
    r"\b(?:quarterly report|interim results|interim report|interim financial report|half year report|half year financial report|q[1-4]\b.*\breport)\b",
    re.I,
)

def _is_english_text(text: str) -> bool:
    if not text:
        return True
    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    if len(text) < 100:
        return cjk_count < 10
    return (cjk_count / len(text)) < 0.05


def _clean_snippet(text: str, max_chars: int = 500) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:max_chars]


def _normalize_doc_text(text: str) -> str:
    normalized = unquote(text or "").lower()
    normalized = normalized.replace("_", " ").replace("-", " ")
    normalized = re.sub(r"\b(hy|fy|ar|q[1-4])(20\d{2})\b", r"\1 \2", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _looks_like_annual_report(text: str, ticker_name: str | None = None) -> bool:
    normalized = _normalize_doc_text(text)
    if ticker_name and ticker_name in COMPANY_DOC_RULES:
        if COMPANY_DOC_RULES[ticker_name]["annual_report"].search(normalized):
            return True
    return bool(ANNUAL_REPORT_RE.search(normalized))


def _looks_like_semiannual_or_quarterly_report(text: str, ticker_name: str | None = None) -> bool:
    normalized = _normalize_doc_text(text)
    if ticker_name and ticker_name in COMPANY_DOC_RULES:
        if COMPANY_DOC_RULES[ticker_name]["semiannual_report"].search(normalized):
            return True
    return bool(SEMIANNUAL_REPORT_RE.search(normalized))


def _is_non_english_variant(link_text: str, href: str, context: str, ticker_name: str | None = None) -> bool:
    basename = unquote(Path(urlparse(href or "").path).name).lower()
    text = _normalize_doc_text(f"{link_text} {context}")
    
    if ticker_name and ticker_name in COMPANY_DOC_RULES:
        if COMPANY_DOC_RULES[ticker_name]["non_english_file"].search(basename):
            return True
            
    return bool(NON_ENGLISH_TEXT_RE.search(text))


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


def _classify_doc_type(text: str, form_type: str | None = None, ticker_name: str | None = None) -> str:
    hay = _normalize_doc_text(text or "")
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

    if _looks_like_annual_report(hay, ticker_name):
        return "annual_report"
    if _looks_like_semiannual_or_quarterly_report(hay, ticker_name):
        return "quarterly_report"

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


def _is_ir_doc_candidate(link_text: str, href: str, context: str, ticker_name: str | None = None) -> bool:
    link_text_clean = re.sub(r"\s+", " ", (link_text or "")).strip().lower()
    href_lower = (href or "").lower()
    context_lower = (context or "").lower()
    combined = f"{link_text_clean} {href_lower} {context_lower}"
    combined_norm = _normalize_doc_text(combined)

    if _is_non_english_variant(link_text_clean, href_lower, context_lower, ticker_name):
        return False

    keywords = [keyword for keywords in IR_DOC_KEYWORDS.values() for keyword in keywords]
    has_keyword = any(keyword in combined_norm for keyword in keywords)
    has_report_signal = _looks_like_annual_report(combined_norm, ticker_name) or _looks_like_semiannual_or_quarterly_report(combined_norm, ticker_name)
    filing_forms = ["10-k", "10-q", "8-k", "6-k", "20-f", "40-f", "s-1", "s-3", "f-1", "f-3"]
    has_filing_signal = any(token in combined_norm for token in filing_forms) or "form " in combined_norm
    has_doc_signal = has_keyword or has_report_signal or has_filing_signal

    if link_text_clean in GENERIC_IR_LINK_TEXT and not has_doc_signal:
        return False

    is_pdf = href_lower.endswith(".pdf")
    has_spreadsheet_ext = href_lower.endswith((".xlsx", ".xls"))
    has_html_doc_ext = href_lower.endswith((".htm", ".html", ".txt"))
    has_doc_like_href = is_pdf or has_spreadsheet_ext or has_html_doc_ext or any(token in href_lower for token in ["results", "earnings", "presentation", "transcript", "report", "webcast", "datapack", "data-pack", "agm"])
    has_sec_or_archive_path = any(token in href_lower for token in ["sec-filings", "/content/", "edgar/data", "sec.gov/archives"])
    has_period_marker = bool(QUARTER_OR_YEAR_RE.search(combined_norm)) or bool(re.search(r"\b(?:ar|hy|fy)\s?20\d{2}\b", combined_norm))

    if is_pdf or has_spreadsheet_ext:
        return has_doc_signal

    if has_html_doc_ext and has_filing_signal:
        return True

    if has_sec_or_archive_path and has_filing_signal:
        return True

    return has_doc_signal and has_doc_like_href and has_period_marker and len(link_text_clean) >= 10


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


def _iter_filings_block(block: dict, cutoff_date: str):
    """Yield (form_type, filing_date, accession_number, primary_document) tuples
    from a single submissions block ('recent' or an archive file).
    Filings older than cutoff_date are skipped; missing dates pass through."""
    forms = block.get("form", []) or []
    dates = block.get("filingDate", []) or []
    acc_nos = block.get("accessionNumber", []) or []
    primary_docs = block.get("primaryDocument", []) or []
    for form_type, filed_at, acc_no, primary_document in zip(forms, dates, acc_nos, primary_docs):
        if filed_at and cutoff_date and filed_at < cutoff_date:
            continue
        yield form_type, filed_at, acc_no, primary_document


def _iter_sec_filings_meta(cik_padded: str, *, days_back: int):
    """Yield filing metadata tuples spanning all available history within
    days_back, paginating through both the 'recent' block and any older
    'files[]' submission archives.

    EDGAR caps the 'recent' block at ~1000 filings (1MB JSON). Active filers
    (banks, foreign filers with monthly 6-K cadence, large accelerated filers)
    routinely exceed that within 1-2 years. Older history lives in
    submissions/CIK{cik}-submissions-{n}.json archives surfaced via the
    'files' array on the main JSON.
    """
    try:
        resp = requests.get(
            f"{EDGAR_BASE}/submissions/CIK{cik_padded}.json",
            headers=HEADERS_EDGAR,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("EDGAR submissions index fetch failed for CIK %s: %s", cik_padded, exc)
        return

    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d") if days_back else ""

    # 1. Recent block (most recent ~1000 filings)
    recent = data.get("filings", {}).get("recent", {})
    yield from _iter_filings_block(recent, cutoff_date)

    # 2. Older archive files. Each archive entry has 'name', 'filingFrom',
    #    'filingTo'. Skip archives whose entire window predates our cutoff.
    archives = data.get("filings", {}).get("files", []) or []
    for archive in archives:
        archive_to = archive.get("filingTo") or ""
        if cutoff_date and archive_to and archive_to < cutoff_date:
            continue
        archive_name = archive.get("name") or ""
        if not archive_name:
            continue
        try:
            arch_resp = requests.get(
                f"{EDGAR_BASE}/submissions/{archive_name}",
                headers=HEADERS_EDGAR,
                timeout=20,
            )
            arch_resp.raise_for_status()
            arch_data = arch_resp.json()
        except Exception as exc:
            log.debug("EDGAR archive fetch failed for %s: %s", archive_name, exc)
            continue
        yield from _iter_filings_block(arch_data, cutoff_date)


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
    docs: list[dict] = []

    for form_type, filed_at, acc_no, primary_document in _iter_sec_filings_meta(
        cik_padded, days_back=days_back
    ):
        if form_type not in form_types or not _within_days(filed_at, days_back):
            continue

        acc_clean = (acc_no or "").replace("-", "")
        if not acc_clean:
            continue
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


def _new_ir_observations(ir_page: str) -> dict:
    """Initialise an IR-scrape observations record. Mutated in place by
    fetch_ir_recent_documents. Persisted to <company>/_meta/site_learning.json
    so the next run can see what worked and what didn't on this IR page."""
    from collections import Counter
    return {
        "page_url": ir_page,
        "fetched_at": None,
        "page_status": None,
        "links_seen": 0,
        "candidates_kept": 0,
        "accepted_count": 0,
        "rejected_counts": Counter(),
        "accepted_doc_types": Counter(),
        "date_source_counts": Counter(),
        "patterns_observed": [],
        "notes": [],
    }


def fetch_ir_recent_documents(
    ir_page: str,
    ticker_name: str,
    *,
    days_back: int = 180,
    limit: int = 6,
    observations: dict | None = None,
) -> list[dict]:
    """Discover IR documents on a single landing/archive page.

    When `observations` is supplied (a dict produced by _new_ir_observations),
    rejection reasons, accepted document types, and date-extraction sources
    are recorded into it. The corpus orchestrator persists this back to the
    company's _meta/site_learning.json so future runs can diagnose recall
    gaps without rerunning the scrape.
    """
    if not ir_page:
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("beautifulsoup4 not installed, skipping IR scrape")
        if observations is not None:
            observations["notes"].append("skipped: beautifulsoup4 not installed")
        return []

    if observations is not None:
        observations["fetched_at"] = datetime.now(timezone.utc).isoformat()

    try:
        resp = requests.get(ir_page, headers=HEADERS_WEB, timeout=20)
        if observations is not None:
            observations["page_status"] = resp.status_code
        resp.raise_for_status()
    except Exception as exc:
        log.warning("IR page fetch failed for %s: %s", ir_page, exc)
        if observations is not None:
            observations["rejected_counts"]["ir_page_fetch_failed"] += 1
            observations["notes"].append(f"ir_page_fetch_failed: {exc}")
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

    all_anchors = soup.find_all("a", href=True)
    if observations is not None:
        observations["links_seen"] = len(all_anchors)

    for anchor in all_anchors:
        href = anchor["href"].strip()
        link_text = " ".join(anchor.stripped_strings)
        context = " ".join(anchor.parent.stripped_strings) if anchor.parent else link_text
        if not _is_ir_doc_candidate(link_text, href, context):
            if observations is not None:
                observations["rejected_counts"]["not_a_candidate"] += 1
            continue

        url = href if href.startswith("http") else urljoin(ir_page, href)

        # Track which signal source produced the date — useful for diagnosing
        # IR pages where date inference is unreliable.
        date_from_context = _parse_date(context)
        date_from_link = _parse_date(link_text) if not date_from_context else None
        date_from_href = _parse_date(href) if not (date_from_context or date_from_link) else None
        published_at = date_from_context or date_from_link or date_from_href
        fallback_year = _find_year_context(anchor, href, link_text, context)

        if observations is not None:
            if date_from_context:
                observations["date_source_counts"]["context"] += 1
            elif date_from_link:
                observations["date_source_counts"]["link_text"] += 1
            elif date_from_href:
                observations["date_source_counts"]["href"] += 1
            elif fallback_year:
                observations["date_source_counts"]["fallback_year"] += 1
            else:
                observations["date_source_counts"]["none"] += 1

        sort_date = "0000-00-00"
        if published_at:
            sort_date = published_at
            if not _within_days(published_at, days_back):
                if observations is not None:
                    observations["rejected_counts"]["outside_window_dated"] += 1
                continue
        elif fallback_year:
            sort_date = f"{fallback_year}-01-01"
            cutoff_year = (datetime.now(timezone.utc) - timedelta(days=days_back)).year
            if int(fallback_year) < cutoff_year:
                if observations is not None:
                    observations["rejected_counts"]["outside_window_year_only"] += 1
                continue
        else:
            # no date or year found, we penalize it severely in the sort but we can still try it
            # if we don't have enough documents. Or we can reject it.
            # Usually better to keep it if we are desperate, but sort it last.
            sort_date = "0000-00-00"
        
        combined_text = _normalize_doc_text(f"{link_text} {context} {href}")
        doc_type = _classify_doc_type(combined_text)

        priority = 0
        if doc_type == "annual_report": priority = 10
        elif doc_type in {"quarterly_report", "quarterly_filing"}: priority = 9
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

    if observations is not None:
        observations["candidates_kept"] = len(candidates)

    candidates.sort(key=lambda x: (x["sort_date"], x["priority"]), reverse=True)

    seen = set()
    docs = []

    for cand in candidates:
        url = cand["url"]
        if url in seen:
            if observations is not None:
                observations["rejected_counts"]["duplicate_url_in_page"] += 1
            continue
        seen.add(url)

        final_url = url
        lower_url = url.lower()
        lightweight_link = (
            lower_url.endswith((".pdf", ".htm", ".html", ".txt", ".xls", ".xlsx"))
            or any(token in lower_url for token in ["/content/", "sec.gov/archives", "edgar/data", "/download/"])
        )

        if lightweight_link:
            snippet = _clean_snippet(f"{cand['link_text']} {cand['context']}")
        else:
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
                if observations is not None:
                    observations["rejected_counts"]["download_failed"] += 1
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
                if observations is not None:
                    observations["rejected_counts"]["low_quality_snippet"] += 1
                continue

        published_at = cand["published_at"]
        if not published_at and cand["sort_date"] != "0000-00-00":
            # If we only got a year context, format it as YYYY-01-01 so it doesn't show as 'undated'
            published_at = cand["sort_date"]

        title = cand["link_text"]
        if not title or title.strip().lower() in GENERIC_IR_LINK_TEXT:
            title = unquote(Path(urlparse(final_url).path).name) or Path(final_url).name
        title = title or f"IR document ({ticker_name})"
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
        if observations is not None:
            observations["accepted_doc_types"][cand["doc_type"] or "unknown"] += 1
        if len(docs) >= limit:
            break

    if observations is not None:
        observations["accepted_count"] = len(docs)
        # Surface a few high-signal observations the next run can act on.
        rejected = observations["rejected_counts"]
        notes = observations["notes"]
        if observations["candidates_kept"] == 0 and observations["links_seen"] > 50:
            notes.append(
                f"WARN: {observations['links_seen']} links on page but 0 passed _is_ir_doc_candidate — "
                "page is likely JS-rendered or document links sit behind a non-anchor element. Consider download_mode=browser."
            )
        if rejected.get("download_failed", 0) > max(3, observations["candidates_kept"] * 0.3):
            notes.append(
                f"WARN: {rejected['download_failed']} download failures of {observations['candidates_kept']} candidates "
                "— host may rate-limit or require a browser-quality session."
            )
        if observations["date_source_counts"].get("none", 0) > observations["candidates_kept"] * 0.5 \
                and observations["candidates_kept"] > 4:
            notes.append(
                "WARN: >50% of candidates had no date/year signal. Date inference is shallow on this page; "
                "extend _parse_date patterns or add a page-specific extractor."
            )
        if observations["accepted_count"] == 0 and observations["candidates_kept"] > 0:
            notes.append(
                f"WARN: {observations['candidates_kept']} candidates but 0 accepted — "
                "all rejected at download/quality stage. Check rejected_counts for the dominant reason."
            )

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

    ir_pages = []
    ir_page = meta.get("ir_page")
    if ir_page:
        ir_pages.append(ir_page)
    for page in meta.get("website_probe_urls") or []:
        if page and page not in ir_pages:
            ir_pages.append(page)

    for page in ir_pages:
        docs.extend(fetch_ir_recent_documents(page, name, days_back=days_back, limit=limit))

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
