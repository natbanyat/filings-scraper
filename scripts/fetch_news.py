"""
News fetcher — multi-source financial news pipeline.

Sources (in priority order):
  1. Finnhub  — curated financial wire services (US-listed tickers)
  2. EDGAR    — 8-K filings as primary-source material events (SEC filers)
  3. RSS      — direct feeds from preferred outlets (Reuters, CNBC, etc.)
  4. Brave    — general web search (fallback / sectors / markets)
  5. X API v2 — optional social monitoring

Post-fetch enrichment:
  - Trafilatura body extraction (clean article text from URLs)
  - Staleness filter (drop stale recaps / old earnings rewrites)
"""

import os
import re
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from config import PREFERRED_SOURCES, BLOCKED_SOURCES
from utils import retry

log = logging.getLogger(__name__)

# Suppress noisy Trafilatura download errors (401/403 from paywalled sites are expected)
logging.getLogger("trafilatura.downloads").setLevel(logging.CRITICAL)

BRAVE_API_URL = "https://api.search.brave.com/res/v1/news/search"

LOW_SIGNAL_TITLE_PATTERNS = [
    re.compile(
        r"\b(trims? stake|reduced stake|cuts? holdings|boosts? holdings|raises position|"
        r"lowers position|purchases shares of|buys shares of|sells [\d,]+ shares of|"
        r"acquires shares of|new position in|has holdings in)\b",
        re.IGNORECASE,
    ),
]


def _is_low_signal_title(title: str) -> bool:
    return any(pattern.search(title) for pattern in LOW_SIGNAL_TITLE_PATTERNS)


@retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
def fetch_news(query: str, count: int = 20, freshness: str = "pd") -> list[dict]:
    """
    Search Brave News. Returns articles sorted: preferred sources first.

    freshness: "pd" = past day, "pw" = past week, "pm" = past month
    """
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        raise EnvironmentError("BRAVE_API_KEY not set in environment.")

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {
        "q": query,
        "count": count,
        "search_lang": "en",
        "freshness": freshness,
        "text_decorations": "false",
        "spellcheck": "false",
    }

    resp = requests.get(BRAVE_API_URL, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json().get("results", [])

    articles = [
        {
            "title":       r.get("title", "").strip(),
            "url":         r.get("url", ""),
            "description": r.get("description", "").strip(),
            "source":      r.get("meta_url", {}).get("hostname", ""),
            "age":         r.get("age", ""),
        }
        for r in raw
        if r.get("title") and r.get("url")
    ]

    # Drop blocked sources, obvious ownership-churn headlines, and articles with no description.
    blocked = {a["url"] for a in articles if any(s in a["source"] for s in BLOCKED_SOURCES)}
    if blocked:
        log.debug("Blocked %d article(s) from low-quality sources", len(blocked))
    low_signal = {a["url"] for a in articles if _is_low_signal_title(a["title"])}
    if low_signal:
        log.debug("Dropped %d low-signal ownership/position article(s)", len(low_signal))
    articles = [
        a for a in articles
        if a["url"] not in blocked
        and a["url"] not in low_signal
        and a["description"]
    ]

    # Rank by source quality: preferred sources ordered by their position in PREFERRED_SOURCES
    # (earlier = higher priority). Unrecognised sources go last.
    def _source_rank(article: dict) -> int:
        src = article["source"]
        for i, preferred in enumerate(PREFERRED_SOURCES):
            if preferred in src:
                return i
        return len(PREFERRED_SOURCES)  # unranked goes after all preferred

    result = sorted(articles, key=_source_rank)
    log.debug("Brave returned %d articles (after block/sort) for query: %s",
              len(result), query[:60])
    return result


@retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
def fetch_finnhub_news(symbol: str, days_back: int = 2) -> list[dict]:
    """
    Fetch company news from Finnhub financial wire services.

    Returns articles in the same dict format as fetch_news() so they merge
    seamlessly into the existing dedup / filter / LLM pipeline.

    Finnhub sources are curated financial outlets (Reuters, Bloomberg,
    MarketWatch, CNBC, etc.) — higher signal-to-noise than general web search.
    """
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        log.debug("FINNHUB_API_KEY not set — skipping Finnhub fetch")
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")

    resp = requests.get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": symbol, "from": from_date, "to": today, "token": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json()

    articles = []
    for r in raw:
        if not r.get("headline") or not r.get("url"):
            continue
        # Extract hostname from URL to match Brave's source format,
        # so PREFERRED_SOURCES / BLOCKED_SOURCES matching works unchanged.
        hostname = urlparse(r["url"]).hostname or ""
        articles.append({
            "title": r.get("headline", "").strip(),
            "url": r["url"],
            "description": r.get("summary", "").strip(),
            "source": hostname,
            "age": r.get("datetime", ""),
        })

    # Apply the same blocked-source and low-signal filters as Brave results
    blocked = {a["url"] for a in articles if any(s in a["source"] for s in BLOCKED_SOURCES)}
    low_signal = {a["url"] for a in articles if _is_low_signal_title(a["title"])}
    articles = [
        a for a in articles
        if a["url"] not in blocked
        and a["url"] not in low_signal
        and a["description"]
    ]

    # Rank by source quality (same logic as Brave results)
    def _source_rank(article: dict) -> int:
        src = article["source"]
        for i, preferred in enumerate(PREFERRED_SOURCES):
            if preferred in src:
                return i
        return len(PREFERRED_SOURCES)

    result = sorted(articles, key=_source_rank)
    log.debug("Finnhub returned %d articles for %s", len(result), symbol)
    return result


def fetch_x_mentions(accounts: list[str], query: str, max_results: int = 10) -> list[dict]:
    """
    Fetch recent posts from monitored X accounts.
    Requires X_BEARER_TOKEN in environment (X API v2 Basic tier or above).
    Returns empty list if token is not set — graceful degradation.
    """
    bearer = os.environ.get("X_BEARER_TOKEN", "")
    if not bearer or not accounts:
        return []

    headers = {"Authorization": f"Bearer {bearer}"}
    account_filter = " OR ".join(f"from:{a}" for a in accounts)
    full_query = f"({account_filter}) ({query}) -is:retweet lang:en"

    params = {
        "query": full_query,
        "max_results": max_results,
        "tweet.fields": "created_at,author_id,text",
        "expansions": "author_id",
        "user.fields": "username",
    }

    try:
        resp = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers=headers,
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        tweets = data.get("data", [])
        users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}

        results = [
            {
                "title":       f"@{users.get(t.get('author_id', ''), 'unknown')}: {t['text'][:120]}",
                "url":         f"https://x.com/i/web/status/{t['id']}",
                "description": t["text"],
                "source":      "x.com",
                "age":         t.get("created_at", ""),
            }
            for t in tweets
        ]
        log.debug("X returned %d mentions for accounts %s", len(results), accounts)
        return results

    except requests.RequestException as e:
        log.warning("[X] API request failed: %s", e)
        return []
    except Exception as e:
        log.warning("[X] Unexpected error: %s", e)
        return []


def deduplicate(articles: list[dict]) -> list[dict]:
    """
    Remove duplicate articles by URL.
    Preserves order; first occurrence wins.
    """
    seen_urls: set[str] = set()
    result = []
    for a in articles:
        url = a.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            result.append(a)
    return result


def _title_words(title: str) -> frozenset[str]:
    """Normalise a title to a set of meaningful words (length > 2, lowercase)."""
    return frozenset(
        w for w in re.sub(r"[^\w\s]", "", title.lower()).split()
        if len(w) > 2
    )


def _title_overlap(a: frozenset, b: frozenset) -> float:
    """Fraction of the *smaller* title's words present in the other title."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _source_rank(article: dict) -> int:
    src = article.get("source", "")
    for i, preferred in enumerate(PREFERRED_SOURCES):
        if preferred in src:
            return i
    return len(PREFERRED_SOURCES)


def _source_domain(article: dict) -> str:
    """Extract the root domain from an article's source field."""
    return article.get("source", "").lower().strip()


def deduplicate_similar(articles: list[dict], threshold: float = 0.5) -> list[dict]:
    """
    Remove near-duplicate articles that cover the same underlying story.

    Two articles are considered the same story when:
      - Title word overlap >= threshold (default 0.5), OR
      - Same source domain AND title word overlap >= 0.35 (same outlet rewrites)

    Within each duplicate cluster the article from the highest-ranked source
    (lowest index in PREFERRED_SOURCES) is kept; the rest are dropped.

    This is O(n²) but n is small (≤40 articles) so it is fast.
    """
    word_sets = [_title_words(a["title"]) for a in articles]
    domains = [_source_domain(a) for a in articles]
    dropped: set[int] = set()

    for i in range(len(articles)):
        if i in dropped:
            continue
        for j in range(i + 1, len(articles)):
            if j in dropped:
                continue
            overlap = _title_overlap(word_sets[i], word_sets[j])
            same_domain = domains[i] and domains[i] == domains[j]
            # Same-domain articles need a lower bar (same outlet re-covering a story)
            effective_threshold = 0.35 if same_domain else threshold
            if overlap >= effective_threshold:
                if _source_rank(articles[i]) <= _source_rank(articles[j]):
                    dropped.add(j)
                else:
                    dropped.add(i)
                    break  # i is now dropped; no need to compare it further

    kept = [a for idx, a in enumerate(articles) if idx not in dropped]
    if dropped:
        log.debug("deduplicate_similar: dropped %d near-duplicate article(s)", len(dropped))
    return kept


# ── Trafilatura: article body extraction ─────────────────────────────────────

def enrich_article_bodies(articles: list[dict], max_chars: int = 1200, max_articles: int = 20) -> list[dict]:
    """
    Fetch and extract clean article body text for each article via Trafilatura.

    Adds a 'body' key to each article dict.  Falls back to empty string on
    failure (paywall, timeout, Cloudflare, etc.) — never blocks the pipeline.

    Only enriches the first max_articles (already sorted by source quality)
    to keep total fetch time reasonable (~1-2s per article).
    """
    try:
        from trafilatura import fetch_url, extract
    except ImportError:
        log.debug("trafilatura not installed — skipping body extraction")
        return articles

    for i, a in enumerate(articles):
        if i >= max_articles:
            a.setdefault("body", "")
            continue
        if a.get("body"):  # already has body (e.g. EDGAR articles)
            continue
        try:
            downloaded = fetch_url(a["url"])
            if downloaded:
                body = extract(downloaded, include_comments=False, include_tables=False)
                text = (body or "")[:max_chars]
                # Paywall detection: if extracted body is <100 chars, mark as low-content
                if text and len(text.split()) < 30:
                    log.debug("Low-content extraction (%d words) for %s — likely paywall",
                              len(text.split()), a["source"])
                    a["body"] = ""  # drop unreliable body, fall back to description in LLM calls
                    a["low_content"] = True
                else:
                    a["body"] = text
            else:
                a["body"] = ""
        except Exception:
            a["body"] = ""

    enriched = sum(1 for a in articles if a.get("body"))
    low_content = sum(1 for a in articles if a.get("low_content"))
    if low_content:
        log.info("Trafilatura: %d article(s) flagged as low-content (paywall/blocked)", low_content)
    log.debug("Trafilatura: enriched %d/%d articles with body text (cap=%d)",
              enriched, len(articles), max_articles)
    return articles


# ── Staleness filter ─────────────────────────────────────────────────────────

# Matches quarter references like "Q4 2025", "Q1 FY2025", "fourth quarter 2025"
_QUARTER_RE = re.compile(
    r"(?:Q[1-4]\s*(?:FY)?\s*20\d{2})|"
    r"(?:(?:first|second|third|fourth)\s+quarter\s+20\d{2})|"
    r"(?:FY\s*20\d{2}\s+Q[1-4])",
    re.IGNORECASE,
)

# Matches explicit dates like "March 5, 2026" or "2026-01-15"
_DATE_RE = re.compile(
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2},?\s+20\d{2})|"
    r"(?:20\d{2}-\d{2}-\d{2})",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_QUARTER_END_MONTH = {"1": 3, "2": 6, "3": 9, "4": 12}


def _parse_quarter_date(text: str) -> datetime | None:
    """Extract the most recent quarter-end date mentioned in text."""
    m = _QUARTER_RE.search(text)
    if not m:
        return None
    s = m.group()
    # Extract quarter number and year
    q_match = re.search(r"[Qq]([1-4])", s) or re.search(r"(first|second|third|fourth)", s, re.I)
    y_match = re.search(r"(20\d{2})", s)
    if not q_match or not y_match:
        return None
    q_map = {"first": "1", "second": "2", "third": "3", "fourth": "4"}
    q = q_map.get(q_match.group(1).lower(), q_match.group(1))
    month = _QUARTER_END_MONTH.get(q, 12)
    year = int(y_match.group(1))
    return datetime(year, month, 28, tzinfo=timezone.utc)


def _parse_explicit_date(text: str) -> datetime | None:
    """Extract an explicit date from text."""
    m = _DATE_RE.search(text)
    if not m:
        return None
    s = m.group()
    try:
        if "-" in s:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # "March 5, 2026" or "Mar 5 2026"
        cleaned = s.replace(",", "")
        parts = cleaned.split()
        if len(parts) >= 3:
            month = _MONTH_MAP.get(parts[0][:3].lower())
            day = int(re.sub(r"\D", "", parts[1]))
            year = int(parts[2])
            if month:
                return datetime(year, month, day, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        pass
    return None


def filter_stale(articles: list[dict], max_age_days: int = 30) -> list[dict]:
    """
    Drop articles that reference dates or quarters more than max_age_days old.

    Catches stale earnings recaps, old press releases, and recycled content
    that Brave sometimes surfaces despite freshness=pd.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    kept = []
    for a in articles:
        text = f"{a.get('title', '')} {a.get('description', '')}"
        ref_date = _parse_quarter_date(text) or _parse_explicit_date(text)
        if ref_date and ref_date < cutoff:
            log.debug("Stale article dropped (ref %s): %s",
                       ref_date.strftime("%Y-%m-%d"), a["title"][:80])
            continue
        kept.append(a)
    if len(kept) < len(articles):
        log.info("Staleness filter: dropped %d stale article(s)", len(articles) - len(kept))
    return kept


# ── EdgarTools: 8-K filing fetch ─────────────────────────────────────────────

def fetch_edgar_filings(cik: str, name: str, days_back: int = 2) -> list[dict]:
    """
    Fetch recent 8-K filings from SEC EDGAR for a given CIK.

    Returns articles in the standard pipeline dict format.
    8-Ks are primary-source material events: earnings, M&A, leadership
    changes, material agreements, etc.
    """
    try:
        from edgar import Company, set_identity
    except ImportError:
        log.debug("edgartools not installed — skipping EDGAR fetch")
        return []

    try:
        set_identity("OpenClaw Research research@openclaw.dev")
        company = Company(cik)
        filings = company.get_filings(form="8-K")
        if not filings:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        articles = []
        for filing in filings[:10]:  # check last 10 filings
            filed = filing.filing_date
            if hasattr(filed, "year"):
                filed_dt = datetime(filed.year, filed.month, filed.day, tzinfo=timezone.utc)
            else:
                continue
            if filed_dt < cutoff:
                break  # filings are reverse-chronological

            # Extract 8-K item descriptions if available
            description = f"SEC 8-K filing by {name} on {filing.filing_date}"
            try:
                doc = filing.obj()
                if hasattr(doc, "items") and doc.items:
                    items_text = "; ".join(str(item) for item in doc.items[:3])
                    description = f"8-K [{items_text}] — {name} filed {filing.filing_date}"
            except Exception:
                pass

            articles.append({
                "title": f"[SEC 8-K] {name}: {description[:120]}",
                "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=8-K",
                "description": description,
                "source": "sec.gov",
                "age": str(filing.filing_date),
                "body": description,
            })

        log.debug("EDGAR returned %d recent 8-K(s) for %s", len(articles), name)
        return articles

    except Exception as exc:
        log.warning("EDGAR fetch failed for %s: %s", name, exc)
        return []


# ── RSS feed fetch ───────────────────────────────────────────────────────────

def fetch_rss_news(
    feeds: list[str],
    max_age_hours: int = 36,
    match_terms: list[str] | None = None,
) -> list[dict]:
    """
    Fetch articles from RSS/Atom feeds.

    Returns articles in the standard pipeline dict format.
    Only includes entries published within max_age_hours.
    If match_terms is provided, require at least one term match in the RSS
    title/summary before including the article.
    """
    try:
        import feedparser
    except ImportError:
        log.debug("feedparser not installed — skipping RSS fetch")
        return []

    import time as _time
    cutoff = _time.time() - (max_age_hours * 3600)
    articles = []

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:15]:
                # Check age — feedparser normalises to time.struct_time
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    entry_ts = _time.mktime(published)
                    if entry_ts < cutoff:
                        continue

                url = entry.get("link", "")
                hostname = urlparse(url).hostname or ""
                # Skip blocked sources even from RSS
                if any(s in hostname for s in BLOCKED_SOURCES):
                    continue

                title = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                # Strip HTML tags from summary
                summary = re.sub(r"<[^>]+>", "", summary).strip()

                if match_terms:
                    haystack = f"{title}\n{summary}".lower()
                    if not any(term.lower() in haystack for term in match_terms):
                        continue

                if title and url:
                    articles.append({
                        "title": title,
                        "url": url,
                        "description": summary[:300],
                        "source": hostname,
                        "age": entry.get("published", ""),
                    })
        except Exception as exc:
            log.warning("RSS fetch failed for %s: %s", feed_url, exc)
            continue

    log.debug("RSS returned %d articles from %d feed(s)", len(articles), len(feeds))
    return articles


# ── Price / volume context (yfinance) ────────────────────────────────────────

def fetch_price_context(symbol: str) -> str | None:
    """
    Fetch today's price move and volume context for a ticker.

    Returns a short string like "GOOG -3.2% on 1.8x avg volume"
    or None if data is unavailable.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d")
        if hist.empty or len(hist) < 2:
            return None

        prev_close = hist["Close"].iloc[-2]
        last_close = hist["Close"].iloc[-1]
        last_volume = hist["Volume"].iloc[-1]
        avg_volume = hist["Volume"].iloc[:-1].mean()

        pct = ((last_close - prev_close) / prev_close) * 100
        vol_ratio = last_volume / avg_volume if avg_volume > 0 else 0

        sign = "+" if pct >= 0 else ""
        context = f"{symbol} {sign}{pct:.1f}%"
        if vol_ratio > 0:
            context += f" on {vol_ratio:.1f}x avg volume"
        return context

    except Exception as exc:
        log.debug("Price context fetch failed for %s: %s", symbol, exc)
        return None


def fetch_technical_context(symbol: str) -> str | None:
    """
    Compute a 1-line technical snapshot for a ticker using yfinance history.

    Returns a string like:
      "RSI(14)=28 oversold | below EMA-21 | ADX=35 strong trend | vol regime=HIGH | momentum=-12.3% (1mo)"
    or None if insufficient data.

    Indicators (inspired by virattt/ai-hedge-fund technical ensemble):
      - RSI-14: overbought/oversold
      - EMA-8 vs EMA-21 vs price: trend direction
      - ADX: trend strength
      - Volatility regime: current vs 63-day median
      - 1-month momentum
    """
    try:
        import yfinance as yf
        import numpy as np

        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="6mo")
        if hist.empty or len(hist) < 63:
            return None

        close = hist["Close"].values
        high = hist["High"].values
        low = hist["Low"].values

        # ── RSI-14 ──────────────────────────────────────────────────
        delta = np.diff(close)
        gains = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        avg_gain = np.mean(gains[-14:])
        avg_loss = np.mean(losses[-14:])
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        rsi_label = "oversold" if rsi < 30 else "overbought" if rsi > 70 else ""
        rsi_str = f"RSI(14)={rsi:.0f}"
        if rsi_label:
            rsi_str += f" {rsi_label}"

        # ── EMA-8 / EMA-21 / price trend ────────────────────────────
        def _ema(data, span):
            alpha = 2.0 / (span + 1)
            out = np.empty_like(data)
            out[0] = data[0]
            for i in range(1, len(data)):
                out[i] = alpha * data[i] + (1 - alpha) * out[i - 1]
            return out

        ema8 = _ema(close, 8)
        ema21 = _ema(close, 21)
        price = close[-1]

        if price > ema8[-1] > ema21[-1]:
            trend_str = "above EMA-8/21 (uptrend)"
        elif price < ema8[-1] < ema21[-1]:
            trend_str = "below EMA-8/21 (downtrend)"
        elif ema8[-1] > ema21[-1]:
            trend_str = "pullback in uptrend"
        else:
            trend_str = "bounce in downtrend"

        # ── ADX (14-period) ─────────────────────────────────────────
        n = 14
        tr = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - close[:-1]),
                                   np.abs(low[1:] - close[:-1])))
        plus_dm = np.where((high[1:] - high[:-1]) > (low[:-1] - low[1:]),
                           np.maximum(high[1:] - high[:-1], 0), 0.0)
        minus_dm = np.where((low[:-1] - low[1:]) > (high[1:] - high[:-1]),
                            np.maximum(low[:-1] - low[1:], 0), 0.0)

        atr = np.convolve(tr, np.ones(n) / n, mode="valid")
        plus_di = 100 * np.convolve(plus_dm, np.ones(n) / n, mode="valid") / np.where(atr > 0, atr, 1)
        minus_di = 100 * np.convolve(minus_dm, np.ones(n) / n, mode="valid") / np.where(atr > 0, atr, 1)

        di_sum = plus_di + minus_di
        dx = 100 * np.abs(plus_di - minus_di) / np.where(di_sum > 0, di_sum, 1)
        adx = np.mean(dx[-n:]) if len(dx) >= n else np.mean(dx)

        adx_label = "strong trend" if adx > 25 else "weak/ranging"
        adx_str = f"ADX={adx:.0f} {adx_label}"

        # ── Volatility regime ───────────────────────────────────────
        daily_returns = np.diff(close) / close[:-1]
        vol_21 = np.std(daily_returns[-21:]) * np.sqrt(252) * 100
        vol_63 = np.std(daily_returns[-63:]) * np.sqrt(252) * 100
        if vol_21 > vol_63 * 1.3:
            vol_regime = "HIGH"
        elif vol_21 < vol_63 * 0.7:
            vol_regime = "LOW"
        else:
            vol_regime = "NORMAL"
        vol_str = f"vol={vol_regime} ({vol_21:.0f}% ann)"

        # ── 1-month momentum ────────────────────────────────────────
        if len(close) >= 21:
            mom_1m = ((close[-1] / close[-21]) - 1) * 100
            sign = "+" if mom_1m >= 0 else ""
            mom_str = f"1mo {sign}{mom_1m:.1f}%"
        else:
            mom_str = ""

        parts = [rsi_str, trend_str, adx_str, vol_str]
        if mom_str:
            parts.append(mom_str)
        return " | ".join(parts)

    except Exception as exc:
        log.debug("Technical context failed for %s: %s", symbol, exc)
        return None


def fetch_macro_context(symbols: dict[str, str] | None = None) -> str:
    """
    Fetch price moves for key macro instruments.

    Returns a compact string like:
    "Macro: Brent +2.1%, DXY -0.3%, VIX +8.2%, US10Y 4.35%"
    """
    if symbols is None:
        symbols = {
            "BZ=F": "Brent",
            "DX-Y.NYB": "DXY",
            "^VIX": "VIX",
            "^TNX": "US10Y",
            "GC=F": "Gold",
        }

    try:
        import yfinance as yf
        parts = []
        tickers = yf.Tickers(" ".join(symbols.keys()))
        for sym, label in symbols.items():
            try:
                hist = tickers.tickers[sym].history(period="5d")
                if hist.empty or len(hist) < 2:
                    continue
                prev = hist["Close"].iloc[-2]
                last = hist["Close"].iloc[-1]
                pct = ((last - prev) / prev) * 100
                sign = "+" if pct >= 0 else ""
                if label == "US10Y":
                    parts.append(f"{label} {last:.2f}% ({sign}{pct:.1f}%)")
                else:
                    parts.append(f"{label} {sign}{pct:.1f}%")
            except Exception:
                continue

        if parts:
            result = "Macro snapshot: " + ", ".join(parts)
            log.debug(result)
            return result
    except Exception as exc:
        log.debug("Macro context fetch failed: %s", exc)

    return ""
