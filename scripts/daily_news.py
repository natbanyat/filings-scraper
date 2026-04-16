"""
Daily news runner — market close edition.

Orchestrates: Brave Search → dedup → cache filter → Pass 1 → Pass 2 → Discord post → event log

Scheduled via cron:
  06:00 UTC  → asia_japan close  (8316, japan-banks, japan market)
  06:30 UTC  → asia_korea close  (korea market)
  08:00 UTC  → asia_hk close     (1299 / AIA)
  21:00 UTC  → us close          (JPM, TMX, STAN, GRAB, SE, FUTU, GOOG, MMYT, exchanges,
                                   gold-miners, uranium-miners)
  [US close hour is DST-aware — computed dynamically]

Manual override:
  python scripts/daily_news.py us
  python scripts/daily_news.py asia_japan
  python scripts/daily_news.py asia_korea
  python scripts/daily_news.py asia_hk

Dry-run (no Discord post, no cache write, prints analysis to stdout):
  python scripts/daily_news.py us --dry-run
"""

import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Bootstrap ─────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from utils import setup_logging
log = setup_logging("daily_news")

# ── Imports ───────────────────────────────────────────────────────────────────
from config import (
    COVERAGE, COVERAGE_ROOT, CLOSE_WINDOWS, SEARCH_QUERIES, X_ACCOUNTS,
    TICKER_META, YAHOO_SYMBOLS, RSS_FEEDS, RSS_MATCH_TERMS,
)
from fetch_news import (
    fetch_news, fetch_finnhub_news, fetch_x_mentions,
    fetch_edgar_filings, fetch_rss_news, enrich_article_bodies,
    filter_stale, fetch_price_context, fetch_technical_context, fetch_macro_context,
    deduplicate, deduplicate_similar,
)
from filter_material import pass_one, pass_two
from post_discord import build_embed, send_embed, send_text, post_run_summary
from cache import filter_unseen, mark_seen, posted_today, mark_posted, store_watchpoints
from event_memory import index_event

CHANNEL_MAP_PATH  = Path(__file__).parent / "channel_map.json"
EVENTS_DIR        = Path(__file__).resolve().parent.parent / "events" / "ticker_news"
UPDATE_FLAGS_DIR  = Path(__file__).resolve().parent.parent / "events" / "pending_updates"


# ── Coverage update flag writer ───────────────────────────────────────────────

def flag_for_update(coverage_key: str, reason: str) -> None:
    """
    Write a flag file signalling that coverage files need updating.
    coverage_updater.py reads these flags and processes pending tickers.
    """
    UPDATE_FLAGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    flag_file = UPDATE_FLAGS_DIR / f"{today}_{coverage_key.replace('/', '_')}.json"
    payload = {
        "coverage_key": coverage_key,
        "date": today,
        "reason": reason,
        "trigger": "earnings" if "earnings" in reason.lower() else "view_changing",
    }
    flag_file.write_text(__import__("json").dumps(payload, indent=2))
    log.info("%s: flagged for coverage update (%s)", coverage_key, reason)


# ── DST-aware market close resolution ────────────────────────────────────────

def _us_close_utc_hour() -> int:
    """Return the UTC hour of 4:00pm ET, correct for DST."""
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return close_et.astimezone(timezone.utc).hour


def resolve_close_window(dry_run: bool = False) -> str:
    """
    Determine which close window is active.
    CLI arg takes priority; otherwise infer from current UTC time.
    """
    # CLI override (first non-flag arg)
    for arg in sys.argv[1:]:
        if arg in CLOSE_WINDOWS:
            return arg

    now = datetime.now(timezone.utc)

    # Asia windows — fixed UTC offsets, no DST
    for window in ("asia_japan", "asia_korea", "asia_hk"):
        t = CLOSE_WINDOWS[window]
        if now.hour == t["hour"] and abs(now.minute - t["minute"]) <= 30:
            return window

    # US window — DST-aware
    us_hour = _us_close_utc_hour()
    if now.hour == us_hour and now.minute <= 30:
        return "us"

    return "us"  # default fallback


# ── Event log writer ──────────────────────────────────────────────────────────

def _write_null_entry(name: str, articles_checked: int) -> None:
    """Append a null-result entry to the event log so self-eval can distinguish
    'genuinely quiet' from 'pipeline dropped this ticker silently'."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = EVENTS_DIR / f"{today}.md"
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    entry = f"\n## {name} — {ts}\n\n(No material news today. {articles_checked} articles checked.)\n"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)


def write_event_log(coverage_key: str, name: str, brief: dict, analyzed: list[dict]) -> None:
    """Append the synthesized daily brief and supporting developments to the event log."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = EVENTS_DIR / f"{today}.md"

    lines = [f"\n## {name} — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"]
    if brief.get("headline"):
        lines.append(f"**TL;DR:** {brief.get('headline', '')}\n")
    if brief.get("thesis_line"):
        lines.append(f"**Thesis status:** {brief.get('thesis_line', '')}\n")
    if brief.get("what_changed"):
        lines.append(f"**What matters now:** {brief.get('what_changed', '')}\n")
    if brief.get("key_debate"):
        lines.append(f"**Key debate:** {brief.get('key_debate', '')}\n")

    watchpoints = [w for w in brief.get("watchpoints", []) if isinstance(w, str) and w.strip()]
    if watchpoints:
        lines.append("**Watchpoints:**")
        lines.extend(f"- {item}" for item in watchpoints)
        lines.append("")

    lines.append("### Key Developments\n")
    for a in analyzed:
        direction = str(a.get("direction", "neutral")).upper()
        impact = str(a.get("impact_type", "incremental")).upper()
        lines.append(f"#### [{direction} | {impact}] {a['title']}")
        if a.get("event"):
            lines.append(f"**Event:** {a.get('event', '')}")
        lines.append(f"**KPI node:** {a.get('kpi_node', '')}")
        if a.get("why_it_matters"):
            lines.append(f"**Why it matters:** {a.get('why_it_matters', '')}")
        if a.get("thesis_link"):
            lines.append(f"**Thesis / debate link:** {a.get('thesis_link', '')}")
        if a.get("watch_next"):
            lines.append(f"**Watch next:** {a.get('watch_next', '')}")
        lines.append(f"\nSource: {a['source']}  ")
        lines.append(f"URL: {a['url']}\n")
        lines.append("---\n")

    with log_file.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info("Event log updated: %s", log_file.name)


# ── Channel map ───────────────────────────────────────────────────────────────

def load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        raise FileNotFoundError(
            "channel_map.json not found. Run 'python scripts/discord_setup.py' first."
        )
    return json.loads(CHANNEL_MAP_PATH.read_text())


# ── Main run ──────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    close_window = resolve_close_window(dry_run)
    channel_map  = load_channel_map() if not dry_run else {}

    targets = [key for key, meta in COVERAGE.items() if meta["close"] == close_window]

    log.info("=" * 60)
    log.info("Daily news run | window=%s | UTC=%s | dry_run=%s",
             close_window, datetime.now(timezone.utc).strftime("%H:%M"), dry_run)
    log.info("Targets: %s", targets)
    log.info("=" * 60)

    # ── Pre-loop: load shared context once for all tickers ──────────────
    macro_context = ""
    try:
        macro_context = fetch_macro_context()
        if macro_context:
            log.info("Macro: %s", macro_context)
    except Exception as exc:
        log.warning("Macro context fetch failed: %s", exc)

    # Load PORTFOLIO_CONTEXT.md once (avoid redundant reads in Pass 1/2)
    from filter_material import _load_portfolio_context
    portfolio_text = _load_portfolio_context()

    # ── Phase 1: fetch + Pass 1 for ALL tickers ─────────────────────────
    phase1_results = []
    for coverage_key in targets:
        name          = coverage_key.split("/")[-1].upper()
        coverage_path = COVERAGE_ROOT / coverage_key
        channel_id    = channel_map.get(coverage_key)
        query         = SEARCH_QUERIES.get(coverage_key, name)

        log.info("── %s [Pass 1] ──────────────────────────────────", name)

        try:
            phase1 = _fetch_and_pass1(
                coverage_key=coverage_key,
                name=name,
                coverage_path=coverage_path,
                channel_id=channel_id,
                query=query,
                dry_run=dry_run,
                macro_context=macro_context,
                portfolio_text=portfolio_text,
            )
            if phase1 is not None:
                phase1_results.append(phase1)
        except Exception as exc:
            log.error("%s: Pass 1 error — %s", name, exc, exc_info=True)

    # ── Cross-ticker macro dedup ─────────────────────────────────────────
    # Remove articles appearing in 3+ different ticker briefs — these are macro
    # stories that belong in macro_close, not individual ticker briefs.
    macro_deduped_count = _macro_dedup_material(phase1_results)

    # Collect run stats before pass2 (material counts reflect post-dedup state)
    total_fetched = sum(r.get("articles_checked", 0) for r in phase1_results)
    total_passed  = sum(len(r.get("material", [])) for r in phase1_results)
    processed_names: list[str] = [r["name"] for r in phase1_results]
    material_names: list[tuple[str, int]] = []
    no_dev_names:   list[str] = []

    # ── Phase 2: Pass 2 + output for each ticker ─────────────────────────
    window_results = []
    for phase1 in phase1_results:
        name = phase1["name"]
        log.info("── %s [Pass 2] ──────────────────────────────────", name)

        try:
            result = _pass2_and_output(phase1, dry_run, channel_map, close_window=close_window)
            if result:
                window_results.append(result)
                material_names.append((name, len(phase1["material"])))
            else:
                no_dev_names.append(name)
        except Exception as exc:
            log.error("%s: Pass 2 error — %s", name, exc, exc_info=True)
            no_dev_names.append(name)

    # ── Run summary post ─────────────────────────────────────────────────
    if not dry_run and processed_names:
        summary_channel_id = channel_map.get("special/macro-close")
        if summary_channel_id:
            try:
                post_run_summary(
                    channel_id=summary_channel_id,
                    close_window=close_window,
                    coverage_processed=processed_names,
                    material_names=material_names,
                    no_development_names=no_dev_names,
                    total_fetched=total_fetched,
                    total_passed=total_passed,
                    macro_deduped=macro_deduped_count,
                )
            except Exception as exc:
                log.warning("Run summary post failed: %s", exc)
        else:
            log.debug("No special/macro-close channel in channel_map — skipping run summary")

    # ── Post-loop: cross-coverage event correlation ──────────────────────
    if len(window_results) >= 3:
        _detect_cross_coverage(window_results, close_window, channel_map, dry_run)

    log.info("Run complete.")


def _append_update_log(coverage_key: str, name: str, brief: dict, analyzed: list[dict]) -> None:
    """Append material events to coverage/update_log.md for the weekly Cowork sync."""
    coverage_path = COVERAGE_ROOT / coverage_key
    coverage_path.mkdir(parents=True, exist_ok=True)
    log_file = coverage_path / "update_log.md"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"\n## {today}\n"]

    if brief.get("headline"):
        lines.append(f"**{brief['headline']}**\n")
    if brief.get("what_changed"):
        lines.append(f"{brief['what_changed']}\n")

    for a in analyzed:
        direction = str(a.get("direction", "")).upper()
        impact = str(a.get("impact_type", "")).upper()
        lines.append(f"- [{direction} | {impact}] {a.get('event', a.get('title', ''))}")
        if a.get("why_it_matters"):
            lines.append(f"  {a['why_it_matters']}")

    lines.append("")

    with log_file.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _fetch_and_pass1(
    coverage_key: str,
    name: str,
    coverage_path: Path,
    channel_id: str | None,
    query: str,
    dry_run: bool,
    macro_context: str = "",
    portfolio_text: str = "",
) -> dict | None:
    """
    Phase 1: fetch articles and run Pass 1 materiality filter.
    Returns a phase1 dict (material may be empty list) or None to skip this ticker entirely.
    None means: already posted today, no articles found, or all articles already seen.
    A dict with material=[] means pass_one found nothing — phase2 will post a clean message.
    """

    # Step 0: Skip if already posted today (skip in dry-run to always test the full pipeline)
    if not dry_run and posted_today(coverage_key):
        log.info("%s: already posted today — skipping", name)
        return None

    # Step 1: Fetch from all sources
    # Priority: Finnhub > EDGAR 8-K > RSS > Brave > X
    # Dedup keeps the first (highest-priority) version on URL/title overlap.

    finnhub_symbol = TICKER_META.get(coverage_key, {}).get("finnhub_symbol")
    sec_cik = TICKER_META.get(coverage_key, {}).get("sec_cik")
    rss_feeds = RSS_FEEDS.get(coverage_key, [])

    finnhub_articles = []
    edgar_articles = []
    rss_articles = []

    if finnhub_symbol:
        try:
            finnhub_articles = fetch_finnhub_news(finnhub_symbol)
        except Exception as exc:
            log.warning("%s: Finnhub fetch failed: %s", name, exc)

    if sec_cik:
        try:
            edgar_articles = fetch_edgar_filings(sec_cik, name)
        except Exception as exc:
            log.warning("%s: EDGAR fetch failed: %s", name, exc)

    if rss_feeds:
        try:
            rss_articles = fetch_rss_news(
                rss_feeds,
                match_terms=RSS_MATCH_TERMS.get(coverage_key),
            )
        except Exception as exc:
            log.warning("%s: RSS fetch failed: %s", name, exc)

    # Skip Brave if higher-quality sources already returned enough articles.
    # Brave is noisy and slow (API call + Trafilatura on junk sources).
    # Still essential for TMX, STAN, 1299 where Finnhub isn't available.
    primary_count = len(finnhub_articles) + len(edgar_articles) + len(rss_articles)
    if primary_count >= 10:
        brave_articles = []
        log.debug("%s: skipping Brave — %d articles from primary sources", name, primary_count)
    else:
        brave_articles = fetch_news(query)

    x_accounts     = X_ACCOUNTS.get(coverage_key, [])
    x_articles     = fetch_x_mentions(x_accounts, name)

    # Merge in priority order: Finnhub > EDGAR > RSS > Brave > X
    all_articles = finnhub_articles + edgar_articles + rss_articles + brave_articles + x_articles
    articles = deduplicate(all_articles)
    articles = deduplicate_similar(articles)

    # Step 1b: Drop stale recaps / old earnings rewrites
    articles = filter_stale(articles)

    # Step 1c: Enrich with article body text (Trafilatura)
    articles = enrich_article_bodies(articles)

    log.info("%s: fetched %d articles (finnhub=%d, edgar=%d, rss=%d, brave=%d, x=%d)",
             name, len(articles), len(finnhub_articles), len(edgar_articles),
             len(rss_articles), len(brave_articles), len(x_articles))

    if not articles:
        log.info("%s: no articles found — skipping", name)
        return None

    # Step 1d: Fetch price + technical context for this ticker
    price_context = None
    yahoo_sym = YAHOO_SYMBOLS.get(coverage_key)
    if yahoo_sym:
        try:
            price_context = fetch_price_context(yahoo_sym)
            if price_context:
                log.info("%s: %s", name, price_context)
        except Exception:
            pass
        try:
            tech_context = fetch_technical_context(yahoo_sym)
            if tech_context:
                price_context = f"{price_context} | {tech_context}" if price_context else tech_context
                log.info("%s: technicals: %s", name, tech_context)
        except Exception:
            pass

    # Step 2: Cache filter (skip in dry-run to always test the full pipeline)
    if not dry_run:
        articles = filter_unseen(articles, coverage_key)
        if not articles:
            log.info("%s: all articles already seen today — skipping", name)
            return None

    # Step 3: Pass 1 — fast materiality filter (Haiku)
    material = pass_one(
        articles, coverage_path, coverage_key=coverage_key, name=name,
        price_context=price_context, macro_context=macro_context,
        portfolio_text=portfolio_text,
    )
    log.info("%s: Pass 1 → %d material", name, len(material))

    return {
        "coverage_key": coverage_key,
        "name": name,
        "coverage_path": coverage_path,
        "channel_id": channel_id,
        "articles_checked": len(articles),
        "material": material,
        "price_context": price_context,
        "macro_context": macro_context,
        "portfolio_text": portfolio_text,
    }


def _macro_dedup_material(phase1_results: list[dict]) -> int:
    """
    Cross-ticker macro dedup: remove articles appearing (by URL or normalized title)
    in 3+ different tickers. Modifies each result's 'material' list in place.
    These are macro stories that belong in macro_close, not individual ticker briefs.
    Returns total count of articles removed across all tickers.
    """
    import re as _re

    def _norm_title(title: str) -> str:
        return _re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()

    # Count how many distinct tickers each URL / normalized title appears in
    url_tickers: dict[str, list[str]] = {}
    title_tickers: dict[str, list[str]] = {}

    for r in phase1_results:
        name = r["name"]
        for art in r["material"]:
            url = art.get("url", "")
            if url:
                url_tickers.setdefault(url, [])
                if name not in url_tickers[url]:
                    url_tickers[url].append(name)
            norm = _norm_title(art.get("title", ""))
            if norm:
                title_tickers.setdefault(norm, [])
                if name not in title_tickers[norm]:
                    title_tickers[norm].append(name)

    macro_urls   = {u for u, names in url_tickers.items()   if len(names) >= 3}
    macro_titles = {t for t, names in title_tickers.items() if len(names) >= 3}

    if not macro_urls and not macro_titles:
        return 0

    total_removed = 0
    for r in phase1_results:
        before = len(r["material"])
        kept = []
        removed_titles = []
        for art in r["material"]:
            url  = art.get("url", "")
            norm = _norm_title(art.get("title", ""))
            if url in macro_urls or norm in macro_titles:
                removed_titles.append(art.get("title", url)[:80])
            else:
                kept.append(art)
        r["material"] = kept
        if removed_titles:
            total_removed += len(removed_titles)
            log.info(
                "%s: cross-ticker macro dedup removed %d/%d: %s",
                r["name"], len(removed_titles), before,
                "; ".join(removed_titles[:3]) + ("..." if len(removed_titles) > 3 else ""),
            )
    return total_removed


def _is_unavailable_brief(brief: dict) -> bool:
    return (
        str(brief.get("thesis_line", "")).strip()
        == "Material news was flagged, but the synthesized brief was unavailable."
        and str(brief.get("what_changed", "")).strip()
        == "Review the linked developments below for the KPI and thesis impact."
    )


def _pass2_and_output(
    phase1: dict,
    dry_run: bool,
    channel_map: dict,
    close_window: str = "",
) -> dict | None:
    """
    Phase 2: Pass 2 synthesis + Discord post + event log.
    Returns result metadata for cross-coverage detection, or None.
    """
    coverage_key    = phase1["coverage_key"]
    name            = phase1["name"]
    coverage_path   = phase1["coverage_path"]
    channel_id      = phase1["channel_id"]
    material        = phase1["material"]
    price_context   = phase1["price_context"]
    macro_context   = phase1["macro_context"]
    portfolio_text  = phase1["portfolio_text"]
    articles_checked = phase1["articles_checked"]

    # Handle zero-material case cleanly — do not call Pass 2
    if not material:
        log.info("%s: no material news today — skipping Discord post", name)
        if not dry_run:
            _write_null_entry(name, articles_checked)
            # No Discord notification when nothing is material — silence is the signal
        return None

    # Step 4: Pass 2 — batched structured analysis (Sonnet, 1 call total)
    analyzed_bundle = pass_two(
        material, coverage_path, name, coverage_key=coverage_key,
        price_context=price_context, macro_context=macro_context,
        portfolio_text=portfolio_text,
    )
    brief    = analyzed_bundle.get("brief", {})
    analyzed = analyzed_bundle.get("articles", [])
    log.info("%s: Pass 2 → %d analyzed", name, len(analyzed))

    if _is_unavailable_brief(brief):
        log.warning("%s: synthesized brief unavailable after retries — suppressing post", name)
        return None

    # Step 4b: Append material events to update_log.md for Cowork sync
    if not dry_run:
        _append_update_log(coverage_key, name, brief, analyzed)

    # Step 4c: Store watchpoints from the brief
    watchpoints = [w for w in brief.get("watchpoints", []) if isinstance(w, str) and w.strip()]
    if watchpoints and not dry_run:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        store_watchpoints(coverage_key, watchpoints, today, brief.get("headline", ""))

    # Step 4d: Index event in long-term BM25 memory (always, including dry-run)
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        kpi_nodes = ", ".join(a.get("kpi_node", "") for a in analyzed if a.get("kpi_node"))
        index_event(
            coverage_key=coverage_key,
            date=today,
            headline=brief.get("headline", ""),
            what_changed=brief.get("what_changed", ""),
            kpi_nodes=kpi_nodes,
            direction=brief.get("overall_direction", ""),
        )
    except Exception:
        pass  # never block the pipeline for memory indexing

    # Step 5: Output
    if dry_run:
        log.info("%s [DRY RUN] — analysis preview:", name)
        if brief:
            log.info("  TL;DR: %s", brief.get("headline", ""))
            log.info("  Thesis status: %s", brief.get("thesis_line", ""))
            log.info("  What matters now: %s", brief.get("what_changed", ""))
            for watch in brief.get("watchpoints", [])[:3]:
                log.info("  Watch: %s", watch)
        for a in analyzed:
            log.info("  %s\n  %s\n", a["title"], a.get("analysis", ""))
        return {
            "coverage_key": coverage_key,
            "name": name,
            "direction": brief.get("overall_direction", "neutral"),
            "kpi_nodes": [a.get("kpi_node", "") for a in analyzed],
            "directions": [a.get("direction", "") for a in analyzed],
            "headline": brief.get("headline", ""),
            "watchpoints": brief.get("watchpoints", []),
        }

    # Step 5a: Post to Discord
    if not channel_id:
        log.warning("%s: no channel ID in channel_map — run discord_setup.py", name)
    else:
        embed = build_embed(name, brief, analyzed, close_window=close_window)
        send_embed(channel_id, embed)
        log.info("%s: posted to #%s", name, COVERAGE[coverage_key]["channel"])

    # Step 5b: Write event log
    write_event_log(coverage_key, name, brief, analyzed)

    # Step 5c: Mark articles as seen and record that today's run completed
    mark_seen(analyzed, coverage_key)
    mark_posted(coverage_key)

    return {
        "coverage_key": coverage_key,
        "name": name,
        "direction": brief.get("overall_direction", "neutral"),
        "kpi_nodes": [a.get("kpi_node", "") for a in analyzed],
        "directions": [a.get("direction", "") for a in analyzed],
        "headline": brief.get("headline", ""),
        "watchpoints": brief.get("watchpoints", []),
    }


# ── Cross-coverage event correlation ─────────────────────────────────────────

def _detect_cross_coverage(
    results: list[dict],
    close_window: str,
    channel_map: dict,
    dry_run: bool,
) -> None:
    """
    Post-loop pass: detect when 3+ coverage items flagged overlapping drivers.
    Uses a single Haiku call to cluster KPI nodes into macro themes.
    """
    import anthropic

    # Build the driver list
    driver_lines = []
    for r in results:
        for kpi, direction in zip(r["kpi_nodes"], r["directions"]):
            if kpi:
                driver_lines.append(f"- {r['name']}: {kpi} ({direction})")

    if len(driver_lines) < 3:
        return

    prompt = f"""Given these KPI drivers flagged today across different coverage items:
{chr(10).join(driver_lines)}

Group them into macro themes where the SAME underlying event or force is driving multiple names.
Return ONLY valid JSON — no prose:
[{{"theme": "short theme name", "names": ["NAME1", "NAME2", ...], "direction": "bull|bear|mixed", "implication": "1 sentence on portfolio-level impact"}}]

Rules:
- Only return themes that span 3+ DIFFERENT coverage names
- If no theme spans 3+ names, return an empty array []
- Be strict: "NII" for JPM and "BOJ rate" for 8316 only cluster if they share the same underlying force"""

    try:
        client = anthropic.Anthropic(api_key=__import__("os").environ.get("ANTHROPIC_API_KEY", ""))
        raw_parts = []
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                raw_parts.append(text)

        raw = "".join(raw_parts).strip()
        import re
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return

        themes = json.loads(match.group())
        if not themes:
            return

        # Format and post
        lines = [f"**Cross-Coverage Alert | {close_window}**\n"]
        for t in themes:
            names = ", ".join(t.get("names", []))
            lines.append(f"**{t['theme']}** ({t.get('direction', 'mixed').upper()}) — {names}")
            lines.append(f"{t.get('implication', '')}\n")

        alert_text = "\n".join(lines)

        if dry_run:
            log.info("Cross-coverage [DRY RUN]:\n%s", alert_text)
        else:
            channel_id = channel_map.get("special/macro-open")
            if channel_id:
                from post_discord import send_text
                send_text(channel_id, alert_text)
                log.info("Cross-coverage alert posted to #macro-open")

    except Exception as exc:
        log.warning("Cross-coverage detection failed: %s", exc)


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)
