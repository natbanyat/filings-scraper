"""
Breaking news alert — intraday material event detection.

Checks Finnhub news for each ticker in a close window, filters through
the seen_articles cache, and uses Haiku to triage whether the article
represents a genuinely breaking, view-changing event.

Breaking = earnings surprise, M&A, regulatory action, management change,
material guidance revision.  NOT breaking = general market color, opinion,
old news, routine announcements.

Posts alerts to #breaking (special/breaking channel).

Usage:
  python scripts/breaking_news.py us
  python scripts/breaking_news.py asia_japan
  python scripts/breaking_news.py asia_hk --dry-run
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from utils import setup_logging
log = setup_logging("breaking_news")

from config import COVERAGE, TICKER_META
from fetch_news import fetch_finnhub_news
from cache import filter_unseen, mark_seen
from post_discord import send_text
from utils import retry
from bounded_router import BoundedRoutingError, run_json_task

CHANNEL_MAP_PATH = Path(__file__).parent / "channel_map.json"
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_CONTEXT_PATH = WORKSPACE_ROOT / "PORTFOLIO_CONTEXT.md"

# Only articles published within this window are considered "breaking"
BREAKING_WINDOW_HOURS = 2


def _load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        raise FileNotFoundError(
            "channel_map.json not found. Run 'python scripts/discord_setup.py' first."
        )
    return json.loads(CHANNEL_MAP_PATH.read_text())


def _load_portfolio_context() -> str:
    """Load PORTFOLIO_CONTEXT.md content."""
    if not PORTFOLIO_CONTEXT_PATH.exists():
        log.warning("PORTFOLIO_CONTEXT.md not found at %s", PORTFOLIO_CONTEXT_PATH)
        return ""
    return PORTFOLIO_CONTEXT_PATH.read_text(encoding="utf-8").strip()


def _extract_thesis_line(portfolio_text: str, coverage_key: str) -> str:
    """Extract the thesis one-liner for a ticker from the coverage table."""
    ticker_symbol = coverage_key.split("/")[-1] if "/" in coverage_key else coverage_key
    for line in portfolio_text.split("\n"):
        if "|" not in line:
            continue
        if ticker_symbol in line or ticker_symbol.upper() in line.upper():
            # Return the full table row as context
            return line.strip()
    return f"Coverage: {ticker_symbol}. No thesis line found."


def _filter_recent(articles: list[dict], hours: int = BREAKING_WINDOW_HOURS) -> list[dict]:
    """Keep only articles published within the last N hours.

    Finnhub returns a Unix timestamp in the 'age' field.  Articles without
    a valid timestamp are kept (benefit of the doubt).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_ts = cutoff.timestamp()
    result = []
    for a in articles:
        ts = a.get("age", "")
        if isinstance(ts, (int, float)) and ts > 0:
            if ts < cutoff_ts:
                continue
        result.append(a)
    if len(result) < len(articles):
        log.debug("Recency filter: %d -> %d articles (last %dh)",
                  len(articles), len(result), hours)
    return result


@retry(max_attempts=3, backoff=3.0, exceptions=(BoundedRoutingError,))
def _triage_breaking(
    articles: list[dict],
    ticker: str,
    thesis_line: str,
) -> list[dict]:
    """Haiku triage: is each article genuinely breaking and view-changing?

    Returns a list of dicts with keys: index, reason (one sentence why it is breaking).
    """
    if not articles:
        return []

    article_block = "\n".join(
        f"[{i}] {a['title']} | {a['source']} | {a.get('description', '')[:300]}"
        for i, a in enumerate(articles)
    )

    prompt = f"""You are a buy-side breaking news filter for {ticker}. Output ONLY valid JSON -- no prose.

THESIS CONTEXT:
{thesis_line}

ARTICLES ([index] title | source | snippet):
{article_block}

Return a JSON array of ONLY genuinely breaking, view-changing articles:
[{{"index": 0, "reason": "one sentence explaining why this is breaking"}}]

STRICT RULES -- "breaking" means ONLY:
- Earnings surprise (beat/miss vs. consensus, guidance revision)
- M&A announcement (acquisition, merger, divestiture, takeover bid)
- Regulatory action (fine, investigation, approval, license, antitrust ruling)
- Management change (CEO/CFO departure, appointment, board shakeup)
- Material guidance revision (profit warning, upgrade, strategy pivot)

NOT breaking (exclude these):
- General market color, sector commentary, macro opinion
- Analyst upgrades/downgrades, price target changes
- Routine announcements (AGM date, dividend ex-date, index rebalance)
- Old news resurfacing, opinion pieces, editorials
- Stock price movement commentary
- 13F ownership/stake changes

If NONE qualify, return an empty array: []"""

    try:
        hits, route = run_json_task(
            task_class="breaking_triage",
            prompt=prompt,
            expected="array",
            max_tokens=512,
        )
    except Exception as e:
        log.warning("Breaking triage failed for %s: %s", ticker, e)
        return []

    log.info("Breaking triage route (%s): %s via %s", ticker, route.tier, route.model)

    # Validate indices
    valid = []
    for h in hits:
        idx = h.get("index")
        if idx is not None and isinstance(idx, int) and 0 <= idx < len(articles):
            valid.append(h)

    return valid


def _format_alert(ticker: str, reason: str, article: dict) -> str:
    """Format a breaking news alert for Discord."""
    title = article.get("title", "Unknown")
    source = article.get("source", "Unknown")
    url = article.get("url", "")
    return f"**BREAKING | {ticker}** -- {reason}\n{title} -- {source}\n{url}"


def run(close_window: str, dry_run: bool = False) -> None:
    """Run breaking news check for all tickers in a close window."""
    # Collect tickers that have a finnhub_symbol
    targets: list[tuple[str, str, str]] = []  # (coverage_key, ticker_label, finnhub_symbol)
    for key, info in COVERAGE.items():
        if info.get("close") != close_window:
            continue
        if not key.startswith("tickers/"):
            continue
        meta = TICKER_META.get(key, {})
        fh_symbol = meta.get("finnhub_symbol")
        if not fh_symbol:
            continue
        ticker_label = key.split("/")[-1]
        targets.append((key, ticker_label, fh_symbol))

    if not targets:
        log.info("No Finnhub-eligible tickers for close window '%s'", close_window)
        return

    portfolio_text = _load_portfolio_context()

    # Load channel map for posting
    channel_id = None
    if not dry_run:
        channel_map = _load_channel_map()
        channel_id = channel_map.get("special/breaking")
        if not channel_id:
            log.warning("No channel ID for special/breaking -- run discord_setup.py")
            return

    total_alerts = 0
    for coverage_key, ticker_label, fh_symbol in targets:
        log.info("Checking breaking news: %s (%s)", ticker_label, fh_symbol)

        # Fetch from Finnhub (days_back=1 to keep API payload small)
        articles = fetch_finnhub_news(fh_symbol, days_back=1)
        if not articles:
            log.debug("%s: no Finnhub articles", ticker_label)
            continue

        # Filter to last N hours only
        articles = _filter_recent(articles, hours=BREAKING_WINDOW_HOURS)
        if not articles:
            log.debug("%s: no articles within %dh window", ticker_label, BREAKING_WINDOW_HOURS)
            continue

        # Filter through seen_articles cache
        if not dry_run:
            articles = filter_unseen(articles, coverage_key)
        if not articles:
            log.debug("%s: all articles already seen", ticker_label)
            continue

        # Haiku triage
        thesis_line = _extract_thesis_line(portfolio_text, coverage_key)
        hits = _triage_breaking(articles, ticker_label, thesis_line)

        if not hits:
            log.info("%s: %d articles checked, none breaking", ticker_label, len(articles))
        else:
            log.info("%s: %d breaking alert(s) from %d articles",
                     ticker_label, len(hits), len(articles))

        for h in hits:
            idx = h["index"]
            reason = h.get("reason", "Material development detected")
            article = articles[idx]
            alert_text = _format_alert(ticker_label, reason, article)

            if dry_run:
                print(f"\n[DRY RUN] {alert_text}")
            else:
                send_text(channel_id, alert_text)
                log.info("Posted breaking alert for %s: %s", ticker_label, article["title"][:80])

            total_alerts += 1

        # Mark all fetched articles as seen (not just hits) to avoid re-checking
        if not dry_run:
            mark_seen(articles, coverage_key)

    log.info("Breaking news check complete for '%s': %d alert(s) posted", close_window, total_alerts)


def main():
    parser = argparse.ArgumentParser(description="Breaking news alert monitor")
    parser.add_argument(
        "window",
        choices=["us", "asia_japan", "asia_korea", "asia_hk"],
        help="Close window to check",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout, do not post or write cache")
    args = parser.parse_args()

    run(close_window=args.window, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
