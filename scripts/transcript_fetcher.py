"""
Transcript / earnings document fetcher — five-level cascade.

Levels (tried in order, first success wins):
  1. SEC EDGAR    — 8-K (US) or 6-K (foreign filers); earnings press release text
  2. IR page      — scrape company investor-relations page for transcript PDF/HTML
  3. Seeking Alpha — scrape earnings call transcript article
  4. Brave Search  — transcript summaries from Bloomberg/Reuters/FT
  5. Discord upload — post request to #earnings-uploads; poll for 24h

Returns: {"text": str, "source": str, "method": "edgar|ir|seekingalpha|brave|discord"}
Raises:  TranscriptUnavailable if all levels fail.

Future hooks (add key to .env to activate):
  FINNHUB_API_KEY  → Finnhub transcript API
  FMP_API_KEY      → FinancialModelingPrep transcript API
  (LinqAlpha has no public API — paste text into #earnings-uploads)
"""

import io
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

EDGAR_BASE    = "https://data.sec.gov"
DISCORD_API   = "https://discord.com/api/v10"
HEADERS_EDGAR = {"User-Agent": "investing-research-bot contact@example.com"}
HEADERS_WEB   = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 50_000
MAX_BRAVE_SUMMARY_CHARS = 12_000
MAX_PDF_PAGES = 30
DISCORD_CDN_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
    "attachments.discordapp.net",
}


class TranscriptUnavailable(Exception):
    pass


def _truncate_text(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    return text[:max_chars]


def _clean_html_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s{3,}", "\n\n", text).strip()


def _split_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower()


def _host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


def _trusted_discord_uploaders() -> set[str]:
    raw = os.environ.get("EARNINGS_UPLOAD_ALLOWED_USER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _download_limited(
    url: str,
    *,
    headers: dict | None = None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    allowed_hosts: set[str] | None = None,
) -> tuple[bytes, str, str, str]:
    """
    Download a resource with HTTPS, host, redirect, and size checks.
    Returns: (content_bytes, content_type, final_url, encoding)
    """
    scheme, host = _split_url(url)
    if scheme != "https" or not host:
        raise ValueError(f"Refusing non-HTTPS or malformed URL: {url}")
    if allowed_hosts and not _host_allowed(host, allowed_hosts):
        raise ValueError(f"Host not allowed: {host}")

    resp = requests.get(
        url,
        headers=headers,
        timeout=20,
        stream=True,
        allow_redirects=True,
    )
    try:
        resp.raise_for_status()
        final_scheme, final_host = _split_url(resp.url)
        if final_scheme != "https" or not final_host:
            raise ValueError(f"Redirected to invalid URL: {resp.url}")
        if allowed_hosts and not _host_allowed(final_host, allowed_hosts):
            raise ValueError(f"Redirected to disallowed host: {final_host}")

        content_length = resp.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise ValueError(f"Content too large: {content_length} bytes")
            except ValueError:
                if content_length.isdigit():
                    raise

        data = bytearray()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError(f"Downloaded content exceeded {max_bytes} bytes")

        return bytes(data), resp.headers.get("Content-Type", ""), resp.url, (resp.encoding or "utf-8")
    finally:
        resp.close()


# ── Level 1: SEC EDGAR ────────────────────────────────────────────────────────

def _edgar_fetch(sec_cik: str, form_types: list[str] = ("8-K", "6-K")) -> dict | None:
    """
    Fetch the most recent earnings press release from SEC EDGAR.
    Returns {"text": ..., "source": "SEC EDGAR", "method": "edgar", "url": ...} or None.
    """
    if not sec_cik:
        return None

    cik_padded = sec_cik.lstrip("0").zfill(10)

    try:
        resp = requests.get(
            f"{EDGAR_BASE}/submissions/CIK{cik_padded}.json",
            headers=HEADERS_EDGAR, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("EDGAR submissions fetch failed for CIK %s: %s", sec_cik, e)
        return None

    filings = data.get("filings", {}).get("recent", {})
    forms   = filings.get("form", [])
    dates   = filings.get("filingDate", [])
    acc_nos = filings.get("accessionNumber", [])

    # Find the most recent matching form
    for form, date_, acc_no in zip(forms, dates, acc_nos):
        if form not in form_types:
            continue

        acc_clean = acc_no.replace("-", "")
        filing_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik_padded.lstrip('0')}/{acc_clean}/"

        try:
            # Get the filing index to find the actual document
            idx_resp = requests.get(
                f"{EDGAR_BASE}/Archives/edgar/data/{cik_padded.lstrip('0')}/{acc_no}-index.json",
                headers=HEADERS_EDGAR, timeout=15,
            )
            idx_resp.raise_for_status()
            idx = idx_resp.json()

            # Find the primary document (press release)
            doc_url = None
            for doc in idx.get("documents", []):
                if doc.get("type") in ("8-K", "6-K", "EX-99.1") and doc.get("document"):
                    doc_url = filing_url + doc["document"]
                    break

            if not doc_url:
                continue

            doc_resp = requests.get(doc_url, headers=HEADERS_EDGAR, timeout=20)
            doc_resp.raise_for_status()

            # Strip HTML tags for plain text
            text = _clean_html_text(doc_resp.text)

            if len(text) < 200:
                continue  # probably empty or nav-only page

            log.info("EDGAR: fetched %s filing (%s chars) for CIK %s", form, len(text), sec_cik)
            return {"text": _truncate_text(text), "source": f"SEC EDGAR {form}", "method": "edgar", "url": doc_url}

        except Exception as e:
            log.debug("EDGAR document fetch failed (%s): %s", acc_no, e)
            continue

    return None


# ── Level 2: IR page scraping ─────────────────────────────────────────────────

def _ir_page_fetch(ir_page: str, ticker_name: str) -> dict | None:
    """
    Scrape company IR page for a transcript or earnings document link.
    Returns fetched document or None.
    """
    if not ir_page:
        return None

    try:
        from bs4 import BeautifulSoup

        resp = requests.get(ir_page, headers=HEADERS_WEB, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Only follow same-host / subdomain links from the configured IR page.
        _, ir_host = _split_url(ir_page)
        allowed_hosts = {ir_host} if ir_host else set()

        # Prioritize actual call/transcript artifacts, not generic results PRs.
        keywords = ["transcript", "earnings call", "conference call", "webcast"]
        for a in soup.find_all("a", href=True):
            text = (a.get_text() + " " + a["href"]).lower()
            if any(kw in text for kw in keywords):
                href = a["href"]
                if not href.startswith("http"):
                    href = urljoin(ir_page, href)

                # Fetch the linked document
                try:
                    data, content_type, final_url, encoding = _download_limited(
                        href,
                        headers=HEADERS_WEB,
                        allowed_hosts=allowed_hosts,
                    )

                    if "pdf" in content_type or final_url.lower().endswith(".pdf"):
                        text = _extract_pdf_text(data)
                    else:
                        text = _clean_html_text(data.decode(encoding, errors="replace"))

                    if len(text) > 300:
                        log.info("IR page: fetched document from %s (%d chars)", final_url[:80], len(text))
                        return {"text": _truncate_text(text), "source": f"IR page ({ticker_name})", "method": "ir", "url": final_url}
                except Exception as e:
                    log.debug("IR linked document rejected/fetch failed for %s: %s", href[:80], e)
                    continue

    except Exception as e:
        log.debug("IR page scrape failed for %s: %s", ir_page, e)

    return None


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF byte string."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            pages.append(page.extract_text() or "")
        return "\n\n".join(pages).strip()
    except Exception as e:
        log.debug("PDF extraction failed: %s", e)
        return ""


# ── Level 3: Seeking Alpha ────────────────────────────────────────────────────

def _seeking_alpha_fetch(ticker_name: str, period_label: str) -> dict | None:
    """
    Search Seeking Alpha for an earnings call transcript.
    Returns transcript text or None.
    """
    # Build search URL
    query = f"{ticker_name} earnings call transcript {period_label}"
    search_url = f"https://seekingalpha.com/search?q={requests.utils.quote(query)}&type=transcripts"

    try:
        resp = requests.get(search_url, headers=HEADERS_WEB, timeout=15)
        resp.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        # Find transcript article links
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/article/" in href and "transcript" in href.lower():
                if not href.startswith("http"):
                    href = "https://seekingalpha.com" + href

                time.sleep(1)  # be polite
                art_resp = requests.get(href, headers=HEADERS_WEB, timeout=15)
                art_resp.raise_for_status()

                # Extract article body text
                art_soup = BeautifulSoup(art_resp.text, "html.parser")
                article_div = (
                    art_soup.find("div", {"data-test-id": "article-body"})
                    or art_soup.find("section", class_=re.compile(r"article"))
                    or art_soup.find("article")
                )
                if article_div:
                    text = article_div.get_text(separator="\n").strip()
                    if len(text) > 500:
                        log.info("Seeking Alpha: fetched transcript (%d chars) from %s", len(text), href[:80])
                        return {"text": _truncate_text(text), "source": "Seeking Alpha", "method": "seekingalpha", "url": href}
    except Exception as e:
        log.debug("Seeking Alpha fetch failed for %s: %s", ticker_name, e)

    return None


# ── Level 4: Brave Search summaries ──────────────────────────────────────────

def _brave_summaries(ticker_name: str, period_label: str) -> dict | None:
    """
    Fetch transcript summaries / call coverage from Brave Search.
    Returns aggregated text from top results.
    """
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        return None

    queries = [
        f"{ticker_name} {period_label} earnings call transcript summary",
        f"{ticker_name} {period_label} earnings call management commentary guidance",
    ]

    all_text = []
    for query in queries:
        try:
            resp = requests.get(
                "https://api.search.brave.com/res/v1/news/search",
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                params={"q": query, "count": 8, "freshness": "pm"},
                timeout=10,
            )
            resp.raise_for_status()
            for r in resp.json().get("results", []):
                title = r.get("title", "")
                desc  = r.get("description", "")
                url   = r.get("url", "")
                all_text.append(f"### {title}\n{desc}\nSource: {url}")
        except Exception as e:
            log.debug("Brave summaries search failed: %s", e)

    if all_text:
        text = "\n\n".join(all_text)
        log.info("Brave summaries: %d chars for %s %s", len(text), ticker_name, period_label)
        return {"text": _truncate_text(text, MAX_BRAVE_SUMMARY_CHARS), "source": "Brave Search summaries", "method": "brave", "url": None}

    return None


# ── Level 5: Discord manual upload ───────────────────────────────────────────

def _request_discord_upload(
    coverage_key: str,
    ticker_name: str,
    period_label: str,
    channel_id: str,
    poll_hours: int = 24,
) -> dict | None:
    """
    Post a request to #earnings-uploads and poll for a file attachment or text message.
    Returns transcript text if upload received within poll_hours, else None.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    allowed_user_ids = _trusted_discord_uploaders()
    if not token or not channel_id:
        return None
    if not allowed_user_ids:
        log.warning("Discord upload fallback disabled: EARNINGS_UPLOAD_ALLOWED_USER_IDS not configured")
        return None

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    nonce = f"UPLOAD-{secrets.token_hex(4).upper()}"

    # Post the request message
    msg = (
        f"**Transcript needed: {ticker_name} {period_label}**\n"
        f"Automated fetch failed for all sources. Please upload the earnings call transcript here.\n"
        f"Reply directly to this message with the transcript text or attachment.\n"
        f"If reply is unavailable, include token `{nonce}` in your message.\n"
        f"Coverage key: `{coverage_key}`"
    )
    try:
        post_resp = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=headers,
            json={"content": msg},
            timeout=15,
        )
        post_resp.raise_for_status()
        request_msg_id = post_resp.json()["id"]
        log.info("Posted transcript request to #earnings-uploads (msg_id=%s)", request_msg_id)
    except Exception as e:
        log.warning("Failed to post Discord transcript request: %s", e)
        return None

    # Poll channel for new messages / attachments
    deadline = time.time() + poll_hours * 3600
    last_id  = request_msg_id
    poll_interval = 300  # check every 5 minutes

    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            msgs_resp = requests.get(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                params={"after": last_id, "limit": 10},
                timeout=15,
            )
            msgs_resp.raise_for_status()
            messages = msgs_resp.json()

            for msg_obj in messages:
                last_id = msg_obj["id"]
                author = msg_obj.get("author", {})
                author_id = str(author.get("id", ""))
                if author.get("bot", False) or author_id not in allowed_user_ids:
                    continue

                message_ref = msg_obj.get("message_reference", {}) or {}
                reply_to_id = str(message_ref.get("message_id", ""))
                content = msg_obj.get("content", "")
                bound_to_request = reply_to_id == request_msg_id or nonce.lower() in content.lower()
                if not bound_to_request:
                    continue

                # Check for file attachment
                for att in msg_obj.get("attachments", []):
                    if att.get("url"):
                        try:
                            data, content_type, final_url, encoding = _download_limited(
                                att["url"],
                                max_bytes=MAX_DOWNLOAD_BYTES,
                                allowed_hosts=DISCORD_CDN_HOSTS,
                            )
                            if "pdf" in content_type or att.get("filename", "").endswith(".pdf"):
                                text = _extract_pdf_text(data)
                            else:
                                text = data.decode(encoding, errors="replace")
                            if len(text) > 200:
                                log.info("Discord upload: received file '%s' (%d chars)", att.get("filename"), len(text))
                                return {
                                    "text": _truncate_text(text),
                                    "source": "Discord upload",
                                    "method": "discord",
                                    "url": final_url,
                                    "uploader_id": author_id,
                                    "uploader_name": author.get("username", ""),
                                    "message_id": msg_obj.get("id"),
                                    "request_msg_id": request_msg_id,
                                }
                        except Exception as e:
                            log.debug("Discord attachment fetch failed: %s", e)

                # Check for pasted text (long message with transcript content)
                if len(content) > 500 and coverage_key.split("/")[-1].upper() not in content:
                    # Looks like pasted transcript text (not a bot message)
                    log.info("Discord upload: received pasted text (%d chars)", len(content))
                    return {
                        "text": _truncate_text(content),
                        "source": "Discord upload (pasted)",
                        "method": "discord",
                        "url": None,
                        "uploader_id": author_id,
                        "uploader_name": author.get("username", ""),
                        "message_id": msg_obj.get("id"),
                        "request_msg_id": request_msg_id,
                    }

        except Exception as e:
            log.debug("Discord poll error: %s", e)

    log.warning("Discord upload: no transcript received within %dh for %s %s", poll_hours, ticker_name, period_label)
    return None


# ── Future API hooks ──────────────────────────────────────────────────────────

def _finnhub_fetch(symbol: str) -> dict | None:
    """Placeholder for Finnhub transcript API. Activated when FINNHUB_API_KEY is set."""
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return None
    log.info("Finnhub API key found but Finnhub transcript integration not yet implemented")
    return None


def _fmp_fetch(symbol: str) -> dict | None:
    """Placeholder for FinancialModelingPrep transcript API. Activated when FMP_API_KEY is set."""
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        return None
    log.info("FMP API key found but FMP transcript integration not yet implemented")
    return None


# ── Public interface ──────────────────────────────────────────────────────────

def fetch_press_release(
    coverage_key: str,
    period_label: str,
    sec_cik: str | None = None,
) -> dict:
    """
    Fetch earnings press release text.
    Currently EDGAR-only by design to keep "the print" separate from call artifacts.
    """
    name = coverage_key.split("/")[-1].upper()
    log.info("%s %s: starting press-release fetch", name, period_label)

    result = _edgar_fetch(sec_cik)
    if result:
        return result

    raise TranscriptUnavailable(
        f"No press release found for {name} {period_label}"
    )


def fetch_call_transcript(
    coverage_key: str,
    period_label: str,
    ir_page: str | None = None,
    earnings_uploads_channel_id: str | None = None,
    skip_discord: bool = False,
) -> dict:
    """
    Fetch call transcript / call-summary material via cascade, excluding EDGAR.
    Returns {"text": str, "source": str, "method": str, "url": str|None}.
    Raises TranscriptUnavailable if all levels fail.
    """
    name = coverage_key.split("/")[-1].upper()
    log.info("%s %s: starting call-transcript fetch cascade", name, period_label)

    # Future hooks — check first (if keys present)
    result = _finnhub_fetch(name) or _fmp_fetch(name)
    if result:
        return result

    # Level 1: IR page
    result = _ir_page_fetch(ir_page, name)
    if result:
        return result

    log.debug("%s: IR page scrape failed — trying Seeking Alpha", name)

    # Level 2: Seeking Alpha
    result = _seeking_alpha_fetch(name, period_label)
    if result:
        return result

    log.debug("%s: Seeking Alpha failed — falling back to Brave summaries", name)

    # Level 3: Brave Search summaries
    result = _brave_summaries(name, period_label)
    if result:
        return result

    log.debug("%s: Brave summaries empty — requesting Discord upload", name)

    # Level 4: Discord manual upload (unless skip_discord)
    if not skip_discord and earnings_uploads_channel_id:
        result = _request_discord_upload(
            coverage_key, name, period_label, earnings_uploads_channel_id
        )
        if result:
            return result

    raise TranscriptUnavailable(
        f"All transcript fetch levels exhausted for {name} {period_label}"
    )


def fetch_transcript(
    coverage_key: str,
    period_label: str,
    sec_cik: str | None = None,
    ir_page: str | None = None,
    earnings_uploads_channel_id: str | None = None,
    skip_discord: bool = False,
) -> dict:
    """
    Backward-compatible wrapper for older callers.
    """
    return fetch_call_transcript(
        coverage_key,
        period_label,
        ir_page=ir_page,
        earnings_uploads_channel_id=earnings_uploads_channel_id,
        skip_discord=skip_discord,
    )
