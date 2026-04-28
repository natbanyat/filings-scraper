"""
Daily macro briefing — two modes:

  morning (default): 09:00 HKT / 01:00 UTC
    Recaps US session close: what moved overnight, what it means for the portfolio
    at the Asian open. Runs after the US daily_news.py window completes.

  asia: 17:30 HKT / 09:30 UTC
    Recaps Asian session: Japan/HK/Korea close, FX moves, any regional developments.
    Runs after asia_hk daily_news.py completes.

Output:
  - Discord post to #macro-open channel
  - Event log written to events/macro/YYYY-MM-DD-{mode}-close.md

Scheduled via cron — uses posted_today guard keyed by mode to prevent duplicates.

Manual:
  python scripts/macro_close.py                   # morning mode
  python scripts/macro_close.py --mode asia
  python scripts/macro_close.py --dry-run
  python scripts/macro_close.py --mode asia --dry-run
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from utils import setup_logging
log = setup_logging("macro_close")

import requests

from fetch_news import fetch_news, deduplicate
from post_discord import build_embed, send_embed, send_text
from cache import posted_today, mark_posted
from utils import retry
from inbox_writer import write_macro_inbox_item
from openclaw_gateway_model import DEEP_MODEL, GatewayModelError, run_json

CHANNEL_MAP_PATH = Path(__file__).parent / "channel_map.json"
EVENTS_DIR = Path(__file__).resolve().parent.parent / "events" / "macro"

MACRO_QUERIES = [
    "Federal Reserve interest rates policy FOMC decision",
    "US Treasury yields 10-year bond market",
    "US dollar DXY EUR/USD USD/JPY FX currency",
    "S&P 500 Nasdaq equities stock market close",
    "gold price oil crude commodities market",
    "credit spreads investment grade high yield bonds",
]

def _load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        raise FileNotFoundError(
            "channel_map.json not found. Run 'python scripts/discord_setup.py' first."
        )
    return json.loads(CHANNEL_MAP_PATH.read_text())


MORNING_QUERIES = [
    "Federal Reserve interest rates policy FOMC",
    "US Treasury yields 10-year bond market close",
    "US dollar DXY EUR/USD USD/JPY FX overnight",
    "S&P 500 Nasdaq stock market close performance",
    "gold price oil crude commodities",
    "credit spreads investment grade high yield",
]

ASIA_QUERIES = [
    "Japan Nikkei stock market BOJ yen",
    "Hong Kong Hang Seng China equities",
    "Korea KOSPI equity market",
    "Asia FX USD/JPY USD/CNY USD/KRW",
    "Asia macro inflation PMI economic data",
    "China policy stimulus economic news",
]

MACRO_QUERIES = MORNING_QUERIES  # backward compat default


@retry(max_attempts=2, backoff=5.0, exceptions=(GatewayModelError,))
def _synthesize(articles: list[dict], today: str, mode: str = "morning") -> dict:
    """
    Use Claude Sonnet to generate a structured macro summary.
    mode: 'morning' = recap US overnight session for Asian open
          'asia'    = recap Asian session for end-of-Asia-day
    Returns a dict with keys: headline, rates, fx, equities, commodities, themes, etc.
    """
    articles_block = "\n".join(
        f"[{i}] {a['title']} | {a['source']} | {a['description'][:200]}"
        for i, a in enumerate(articles[:40])
    )

    if mode == "asia":
        session_desc = "Asian session close (Japan, HK, Korea)"
        focus = (
            "Focus on: Japan (Nikkei, BOJ, yen), Hong Kong (Hang Seng, China macro), "
            "Korea (KOSPI), regional FX, and any Asian economic data or policy developments. "
            "Frame what these moves mean at the portfolio level heading into the US open."
        )
        portfolio_context = (
            "8316/SMFG (Japan megabank, BOJ/yen sensitive), 1299/AIA (Asia insurer, HK/China sensitive), "
            "GRAB (SE Asia superapp), SE (SE Asia e-commerce/gaming), FUTU (HK/China brokerage), "
            "STAN (Asia/EM bank, FX sensitive), japan-banks sector, exchanges sector"
        )
    else:
        session_desc = "US market session close — recap for the Asian morning open"
        focus = (
            "Focus on: US equities (S&P 500, Nasdaq, key sectors), rates/Fed, USD/JPY and major FX, "
            "commodities (gold, oil), and credit. "
            "Frame what the US close means for Asia-listed names and the portfolio at today's open."
        )
        portfolio_context = (
            "JPM (US bank, NII/credit sensitive), TMX (Canadian exchange), "
            "STAN (Asia/EM bank, FX/credit sensitive), GRAB (SE Asia superapp), "
            "SE (SE Asia e-commerce/gaming), FUTU (HK/China brokerage, risk appetite sensitive), "
            "GOOG (search/cloud/AI, regulatory sensitive), MMYT (India travel, fuel/INR sensitive), "
            "8316/SMFG (Japan megabank, BOJ/yen sensitive), 1299/AIA (Asia insurer), "
            "gold-miners (gold price, real rates), uranium-miners (nuclear/energy policy), "
            "exchanges (volumes, volatility), japan-banks (BOJ, yen, NIM)"
        )

    prompt = f"""You are a macro analyst writing a briefing for the {session_desc} ({today} UTC).
{focus}

PORTFOLIO NAMES (only flag these where directly relevant):
  {portfolio_context}

HEADLINES:
{articles_block}

Return ONLY valid JSON (no prose before or after):
{{
  "headline": "One sentence capturing the single most important macro development",
  "rates": "2-3 sentences on rates/yields/central bank (what moved, direction, why)",
  "fx": "2-3 sentences on FX (key pairs — moves and drivers)",
  "equities": "2-3 sentences on equity market performance and key themes",
  "commodities": "1-2 sentences on gold, oil, or other notable commodity moves",
  "credit": "1-2 sentences on credit markets / risk appetite if data available",
  "themes": ["theme 1", "theme 2", "theme 3"],
  "portfolio_watch": "2-3 sentences on what this session means specifically for the affected portfolio names. Name them explicitly and be concrete.",
  "affected_names": ["8316", "STAN"],
  "risk_type": "risk-premium|rates|fx|commodity|geopolitical|none"
}}

Rules:
- affected_names: ONLY include names where today's session directly moves a driver (not generic beta)
- risk_type: classify the dominant macro risk channel
- If a category has no data in the headlines, write "No notable developments." for that field."""

    try:
        return run_json(prompt, expected="object", model=os.environ.get("OPENCLAW_MACRO_MODEL") or DEEP_MODEL)
    except GatewayModelError as e:
        log.warning("Macro synthesis failed: %s", e)
        return {"headline": "Macro summary unavailable", "themes": []}


def _build_macro_embed(summary: dict, article_count: int) -> dict:
    """Build a Discord embed from the macro summary dict."""
    themes_str = " | ".join(summary.get("themes", []))
    fields = []
    for section, label in [
        ("rates",       "Rates & Yields"),
        ("fx",          "FX"),
        ("equities",    "Equities"),
        ("commodities", "Commodities"),
        ("credit",      "Credit / Risk"),
        ("portfolio_watch", "Portfolio Watch"),
    ]:
        text = summary.get(section, "")
        if text and text != "No notable developments today.":
            fields.append({"name": label, "value": text[:1020], "inline": False})

    return {
        "title":       "MACRO OPEN — Daily Update",
        "description": summary.get("headline", "") + (f"\n\n**Themes:** {themes_str}" if themes_str else ""),
        "color":       0x5865F2,  # Discord blurple — neutral macro color
        "fields":      fields[:25],
        "footer":      {"text": f"investing-agent | {article_count} headlines analyzed"},
    }


def _write_event_log(summary: dict, articles: list[dict]) -> None:
    """Append macro summary to the daily event log."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = EVENTS_DIR / f"{today}-close.md"

    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [
        f"\n## Macro Close — {now_str}\n",
        f"**Headline:** {summary.get('headline', '')}\n",
    ]
    for section, label in [
        ("rates",       "Rates & Yields"),
        ("fx",          "FX"),
        ("equities",    "Equities"),
        ("commodities", "Commodities"),
        ("credit",      "Credit / Risk"),
        ("portfolio_watch", "Portfolio Watch"),
    ]:
        text = summary.get(section, "")
        if text:
            lines.append(f"**{label}:** {text}\n")

    themes = summary.get("themes", [])
    if themes:
        lines.append(f"**Themes:** {' | '.join(themes)}\n")

    lines.append("\n### Source headlines\n")
    for a in articles[:20]:
        lines.append(f"- [{a['title']}]({a['url']}) — {a['source']}")

    with log_file.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info("Macro event log updated: %s", log_file.name)


def run(dry_run: bool = False, mode: str = "morning") -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    posted_key = f"special/macro-{mode}"
    queries = ASIA_QUERIES if mode == "asia" else MORNING_QUERIES

    log.info("=" * 60)
    log.info("Macro %s | UTC=%s | dry_run=%s", mode, datetime.now(timezone.utc).strftime("%H:%M"), dry_run)
    log.info("=" * 60)

    # Guard against double-posting
    if not dry_run and posted_today(posted_key):
        log.info("Macro %s already posted today — skipping", mode)
        return

    # Step 1: Fetch macro headlines
    all_articles: list[dict] = []
    for query in queries:
        try:
            articles = fetch_news(query, count=10)
            all_articles.extend(articles)
            log.debug("Query '%s': %d articles", query[:50], len(articles))
        except Exception as e:
            log.warning("Fetch failed for query '%s': %s", query[:50], e)

    all_articles = deduplicate(all_articles)
    log.info("Fetched %d unique macro headlines", len(all_articles))

    if not all_articles:
        log.warning("No macro headlines found — aborting")
        return

    # Step 2: Synthesize with Claude
    summary = _synthesize(all_articles, today, mode=mode)
    log.info("Macro synthesis complete: %s", summary.get("headline", "")[:80])

    if dry_run:
        log.info("[DRY RUN] Macro %s summary:", mode)
        for k, v in summary.items():
            log.info("  %s: %s", k, v)
        return

    # Step 2b: Write external inbox handoff before Discord posting so the
    # downstream research processor still receives the macro item if posting fails.
    try:
        write_macro_inbox_item(summary, all_articles, mode)
    except Exception as exc:
        log.error("Macro %s inbox write failed: %s", mode, exc, exc_info=True)

    # Step 3: Post full macro summary to #macro-open
    channel_map = _load_channel_map()
    channel_id = channel_map.get("special/macro-open")
    if not channel_id:
        log.warning("No channel ID for special/macro-open — run discord_setup.py")
    else:
        embed = _build_macro_embed(summary, len(all_articles))
        # Prefix the embed title with the session label
        session_label = "🌙 US Overnight" if mode == "morning" else "🌏 Asia Close"
        embed["title"] = f"{session_label} | {embed.get('title', 'Macro Update')}"
        send_embed(channel_id, embed)
        log.info("Macro %s posted to #macro-open", mode)

    # Step 3b: If macro is portfolio-relevant, post to #daily-briefing with affected names
    affected = summary.get("affected_names", [])
    portfolio_watch = summary.get("portfolio_watch", "")
    if affected and portfolio_watch:
        briefing_id = channel_map.get("special/daily-briefing")
        if briefing_id:
            risk_type = summary.get("risk_type", "macro").upper()
            names_str = ", ".join(affected)
            session_label = "US OVERNIGHT" if mode == "morning" else "ASIA CLOSE"
            alert = (
                f"**MACRO | {session_label} | {risk_type} | Names: {names_str}**\n"
                f"{summary.get('headline', '')}\n\n"
                f"{portfolio_watch}"
            )
            send_text(briefing_id, alert)
            log.info("Portfolio macro alert posted to #daily-briefing (%d names)", len(affected))

    # Step 4: Write event log
    _write_event_log(summary, all_articles)

    # Step 5: Mark posted
    mark_posted(posted_key)

    log.info("Macro %s run complete.", mode)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Macro briefing — morning (US overnight recap) or asia (Asia session recap)")
    parser.add_argument("--mode", choices=["morning", "asia"], default="morning",
                        help="morning = US overnight recap (09:00 HKT), asia = Asia session recap (17:30 HKT)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run, mode=args.mode)
