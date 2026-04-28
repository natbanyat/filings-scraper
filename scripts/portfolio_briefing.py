"""
Portfolio briefing — unified daily analyst report.

Reads today's ticker and macro event logs (written by daily_news.py and
macro_close.py), fetches closing prices via yfinance, then uses Claude Sonnet
to synthesize a 5-section portfolio briefing:

  I.   Macro Overview
  II.  Ticker Analysis
  III. Portfolio Heat Map  (programmatic — no LLM)
  IV.  Themes & Connections
  V.   Key Watchpoints

Output:
  - Discord posts to #daily-briefing channel (plain-text, chunked)
  - Event log written to events/portfolio/YYYY-MM-DD-briefing.md

Run after the US close window (daily_news.py us + macro_close.py must have run):
  python scripts/portfolio_briefing.py
  python scripts/portfolio_briefing.py --dry-run
  python scripts/portfolio_briefing.py --date 2026-03-15   # backfill
"""

import json
import logging
import os
import re
import sys
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from utils import setup_logging
log = setup_logging("portfolio_briefing")
import yfinance as yf

from config import YAHOO_SYMBOLS, BRIEFING_BASELINE_DATE
from openclaw_gateway_model import DEEP_MODEL, GatewayModelError, run_text
from post_discord import send_briefing
from utils import retry

CHANNEL_MAP_PATH = Path(__file__).parent / "channel_map.json"
EVENTS_DIR = Path(__file__).resolve().parent.parent / "events" / "portfolio"
TICKER_NEWS_DIR = Path(__file__).resolve().parent.parent / "events" / "ticker_news"
MACRO_DIR = Path(__file__).resolve().parent.parent / "events" / "macro"
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_CONTEXT_PATH = WORKSPACE_ROOT / "PORTFOLIO_CONTEXT.md"

# Currency labels for heat map display
_CURRENCY: dict[str, str] = {
    "tickers/JPM":  "USD",
    "tickers/TMX":  "CAD",
    "tickers/STAN": "GBX",
    "tickers/GRAB": "USD",
    "tickers/SE":   "USD",
    "tickers/FUTU": "USD",
    "tickers/GOOG": "USD",
    "tickers/MMYT": "USD",
    "tickers/8316": "JPY",
    "tickers/1299": "HKD",
}

def _load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        raise FileNotFoundError(
            "channel_map.json not found. Run 'python scripts/discord_setup.py' first."
        )
    return json.loads(CHANNEL_MAP_PATH.read_text())


# ── Event log readers ─────────────────────────────────────────────────────────

def _read_ticker_log(today: str, lookback_days: int = 1) -> str:
    path = TICKER_NEWS_DIR / f"{today}.md"
    content = ""
    if path.exists():
        content = path.read_text(encoding="utf-8").strip()
    if content:
        return content
    # Fall back to yesterday if today's log is missing or empty
    if lookback_days >= 1:
        yesterday = (_date.fromisoformat(today) - timedelta(days=1)).isoformat()
        ypath = TICKER_NEWS_DIR / f"{yesterday}.md"
        if ypath.exists():
            ycontent = ypath.read_text(encoding="utf-8").strip()
            if ycontent:
                log.info("Using yesterday's ticker log (%s) — today's is empty", yesterday)
                return f"[Carried from {yesterday}]\n\n{ycontent}"
    log.warning("Ticker log not found for %s or prior day", today)
    return ""


def _read_macro_log(today: str, lookback_days: int = 1) -> str:
    path = MACRO_DIR / f"{today}-close.md"
    content = ""
    if path.exists():
        content = path.read_text(encoding="utf-8").strip()
    if content:
        return content
    # Fall back to yesterday if today's log is missing or empty
    if lookback_days >= 1:
        yesterday = (_date.fromisoformat(today) - timedelta(days=1)).isoformat()
        ypath = MACRO_DIR / f"{yesterday}-close.md"
        if ypath.exists():
            ycontent = ypath.read_text(encoding="utf-8").strip()
            if ycontent:
                log.info("Using yesterday's macro log (%s) — today's is empty", yesterday)
                return f"[Carried from {yesterday}]\n\n{ycontent}"
    log.warning("Macro log not found for %s or prior day", today)
    return ""


# ── Portfolio context loader ──────────────────────────────────────────────────

def _load_portfolio_context() -> str:
    """Load PORTFOLIO_CONTEXT.md content for synthesis prompt."""
    if not PORTFOLIO_CONTEXT_PATH.exists():
        log.warning("PORTFOLIO_CONTEXT.md not found at %s", PORTFOLIO_CONTEXT_PATH)
        return ""
    return PORTFOLIO_CONTEXT_PATH.read_text(encoding="utf-8").strip()


def _extract_portfolio_sections(portfolio_text: str) -> str:
    """Extract coverage table + thesis lines and sector drivers for the prompt.

    Keeps the block compact (<3000 chars) to leave room for logs/prices.
    """
    if not portfolio_text:
        return ""

    sections_to_extract = [
        "## Active Coverage",
        "## What Matters by Sector",
    ]
    parts: list[str] = []
    for section_header in sections_to_extract:
        in_section = False
        section_lines: list[str] = []
        for line in portfolio_text.split("\n"):
            if line.startswith(section_header):
                in_section = True
                section_lines.append(line)
                continue
            if in_section and line.startswith("## "):
                break
            if in_section:
                section_lines.append(line)
        if section_lines:
            parts.append("\n".join(section_lines).strip())

    result = "\n\n".join(parts)
    return result[:3000]


# ── JSON extraction ──────────────────────────────────────────────────────────

def _extract_json_object(text: str) -> str | None:
    """Find the first top-level JSON object using balanced-brace matching."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# ── Price fetching ────────────────────────────────────────────────────────────

def _fetch_prices(today: str) -> dict[str, dict | None]:
    """
    Fetch daily close prices and compute 1-day and cumulative % changes.

    Returns a dict keyed by coverage key. Value is either:
      {"close": float, "volume": int, "pct_1d": float, "pct_cum": float, "currency": str}
    or None if data is unavailable.
    """
    results: dict[str, dict | None] = {}

    today_dt = _date.fromisoformat(today)
    # Fetch a 10-day window ending on (today+1) to get data for the target date
    window_start = (today_dt - timedelta(days=10)).isoformat()
    window_end   = (today_dt + timedelta(days=1)).isoformat()

    for key, symbol in YAHOO_SYMBOLS.items():
        try:
            ticker = yf.Ticker(symbol)

            # Fetch a rolling window to get close for today and prev trading day
            hist_short = ticker.history(start=window_start, end=window_end)
            if hist_short.empty or len(hist_short) < 1:
                log.warning("No recent price data for %s (%s)", key, symbol)
                results[key] = None
                continue

            close = float(hist_short["Close"].iloc[-1])
            volume = int(hist_short["Volume"].iloc[-1]) if "Volume" in hist_short.columns else 0

            if len(hist_short) >= 2:
                prev_close = float(hist_short["Close"].iloc[-2])
                pct_1d = (close - prev_close) / prev_close * 100
            else:
                prev_close = close
                pct_1d = 0.0

            # Cumulative % from baseline — skip if today is before the baseline date
            baseline_dt = _date.fromisoformat(BRIEFING_BASELINE_DATE)
            if today_dt < baseline_dt:
                pct_cum = 0.0
            else:
                hist_long = ticker.history(start=BRIEFING_BASELINE_DATE, end=window_end)
                if hist_long.empty:
                    pct_cum = 0.0
                    log.warning("No baseline history for %s — cumulative set to 0", symbol)
                else:
                    baseline_close = float(hist_long["Close"].iloc[0])
                    pct_cum = (close - baseline_close) / baseline_close * 100

            results[key] = {
                "close":   close,
                "volume":  volume,
                "pct_1d":  pct_1d,
                "pct_cum": pct_cum,
                "currency": _CURRENCY.get(key, ""),
            }
            log.debug("%s (%s): %.2f %s | 1D: %+.1f%% | Cum: %+.1f%%",
                      key, symbol, close, _CURRENCY.get(key, ""), pct_1d, pct_cum)

        except Exception as e:
            log.warning("Price fetch failed for %s (%s): %s", key, symbol, e)
            results[key] = None

    return results


# ── Heat map ─────────────────────────────────────────────────────────────────

def _build_heat_map(prices: dict[str, dict | None]) -> str:
    """
    Build a fixed-width heat map table sorted by abs(1-day %) descending.
    Formatted as a Discord code block for alignment.
    """
    # Ticker display names (short)
    _label = {
        "tickers/JPM":  "JPM",
        "tickers/TMX":  "TMX",
        "tickers/STAN": "STAN",
        "tickers/GRAB": "GRAB",
        "tickers/SE":   "SE",
        "tickers/FUTU": "FUTU",
        "tickers/GOOG": "GOOG",
        "tickers/MMYT": "MMYT",
        "tickers/8316": "8316",
        "tickers/1299": "1299",
    }

    rows = []
    for key, data in prices.items():
        label = _label.get(key, key.split("/")[-1].upper())
        if data is None:
            rows.append((0.0, label, "N/A", "N/A", "N/A", ""))
        else:
            rows.append((
                abs(data["pct_1d"]),
                label,
                f"{data['close']:,.2f}",
                f"{data['pct_1d']:+.1f}%",
                f"{data['pct_cum']:+.1f}%",
                data["currency"],
            ))

    rows.sort(key=lambda r: r[0], reverse=True)

    header = f"{'Ticker':<7}  {'Close':>10}  {'1D%':>7}  {'Cum%':>7}  {'Ccy':<4}"
    sep    = f"{'-'*7}  {'-'*10}  {'-'*7}  {'-'*7}  {'-'*4}"
    lines  = [header, sep]
    for _, ticker, close, pct_1d, pct_cum, ccy in rows:
        lines.append(f"{ticker:<7}  {close:>10}  {pct_1d:>7}  {pct_cum:>7}  {ccy:<4}")

    return "```\n" + "\n".join(lines) + "\n```"


# ── Claude synthesis ──────────────────────────────────────────────────────────

def _build_price_block(prices: dict[str, dict | None]) -> str:
    """Build compact price summary string for the synthesis prompt."""
    parts = []
    labels = {
        "tickers/JPM": "JPM", "tickers/TMX": "TMX", "tickers/STAN": "STAN",
        "tickers/GRAB": "GRAB", "tickers/SE": "SE", "tickers/FUTU": "FUTU",
        "tickers/GOOG": "GOOG", "tickers/MMYT": "MMYT",
        "tickers/8316": "8316/SMFG", "tickers/1299": "1299/AIA",
    }
    for key, data in prices.items():
        label = labels.get(key, key.split("/")[-1].upper())
        if data is None:
            parts.append(f"{label}: N/A")
        else:
            ccy = data["currency"]
            parts.append(
                f"{label}: {ccy}{data['close']:,.2f} "
                f"({data['pct_1d']:+.1f}% today, {data['pct_cum']:+.1f}% vs baseline)"
            )
    return "  |  ".join(parts)


@retry(max_attempts=2, backoff=5.0, exceptions=(GatewayModelError,))
def _synthesize_briefing(
    ticker_log: str,
    macro_log: str,
    prices: dict[str, dict | None],
    today: str,
    portfolio_context: str = "",
    mode: str = "morning",
) -> dict:
    """
    Use Claude Sonnet to generate the narrative sections of the briefing.
    The heat map (section III) is built programmatically, not by the LLM.
    """
    price_block = _build_price_block(prices)
    portfolio_block = _extract_portfolio_sections(portfolio_context)

    portfolio_section = ""
    if portfolio_block:
        portfolio_section = f"""
PM PORTFOLIO CONTEXT (positions, thesis, sector drivers):
{portfolio_block}

"""

    if mode == "asia":
        session_label = "Asian session close (Japan, HK, Korea)"
        session_instruction = (
            "This is the end-of-Asia briefing. Focus on what happened in Asia hours: "
            "Japan/HK/Korea equity performance, regional FX (JPY, HKD, CNH), "
            "BOJ/PBOC/regional policy signals, and any Asia-specific name developments. "
            "For US-listed names (JPM, GOOG, etc.), note only if there was overnight news "
            "that is relevant to the Asian session context. "
            "Frame the watchpoints around what matters heading into the US open."
        )
        relevant_tickers = "8316/SMFG (Japan), 1299/AIA (HK), GRAB (SE Asia), SE (SE Asia), FUTU (HK/China), STAN (Asia/EM)"
    else:
        session_label = "US session close — morning briefing for the Asian open"
        session_instruction = (
            "This is the morning briefing for the Asian open. Recap what happened in US hours: "
            "US equity close (S&P, Nasdaq, sector rotation), rates/Fed moves, USD/JPY and key FX, "
            "commodities (gold, oil), and any US-listed name developments overnight. "
            "Frame the analysis for a PM sitting in Asia reviewing their book at the open. "
            "Watchpoints should focus on what to monitor during today's Asian session."
        )
        relevant_tickers = "JPM, TMX, STAN, GRAB, SE, FUTU, GOOG, MMYT, gold-miners, uranium-miners, exchanges"

    prompt = f"""You are a senior portfolio analyst writing the {session_label} briefing for {today}.
Write in concise, professional prose. No emojis. No hedge-fund jargon filler.
Every sentence must add information — cut padding ruthlessly.

{session_instruction}
{portfolio_section}
MACRO LOG (today):
{macro_log[:2500] or "No macro log available today."}

TICKER NEWS LOG (today):
{ticker_log[:8000] or "No ticker news logged today."}

PRICE DATA (today's close vs prior close and vs baseline {BRIEFING_BASELINE_DATE}):
{price_block}

Portfolio coverage:
  Primary for this session: {relevant_tickers}
  Full universe: JPM, TMX, STAN, GRAB, SE, FUTU, GOOG, MMYT, 8316/SMFG, 1299/AIA
  Sectors: exchanges, gold-miners, uranium-miners, japan-banks

Return ONLY valid JSON (no prose before or after):
{{
  "top_3": [
    "1-line description of the single most important portfolio development today",
    "second most important development",
    "third most important development"
  ],
  "decision_point": "The single most important thing the PM needs to decide or act on today. If nothing requires action, say 'No action required — continue monitoring.'",
  "macro_overview": "Narrative prose. 4-6 paragraphs. Connect rates, FX, and commodity moves to specific portfolio exposures. Anchor with concrete data points from the logs where available.",
  "ticker_analyses": {{
    "JPM": "2-3 paragraph narrative. Include close price and % change inline. Connect news to KPI nodes and thesis implications. Only write substantive entries.",
    "STAN": "...",
    "... (include only tickers with material news OR notable price moves today) ...": ""
  }},
  "themes": [
    "1. Theme Title: ~100-word narrative connecting 2+ holdings",
    "2. ..."
  ],
  "watchpoints": [
    "1. Specific event or threshold — date if known — why it matters for the portfolio",
    "2. ...",
    "..."
  ]
}}

Rules:
- top_3: exactly 3 items; each a single sentence; ranked by portfolio impact; reference the PM's thesis/positions from the portfolio context when assessing impact
- decision_point: one sentence; must be concrete and actionable (e.g. "Consider trimming X ahead of Y" or "Review Z exposure given..."); use 'No action required — continue monitoring.' only if genuinely nothing needs attention
- macro_overview: ~400-600 words; this is the analytical backbone of the briefing; assess net portfolio direction informed by PM positions
- ticker_analyses: omit tickers with no news and no notable price move; each entry 150-250 words; frame through the PM's thesis for each name
- themes: 2-4 themes that cut across multiple holdings; ranked by portfolio relevance
- watchpoints: 5-7 items; ranked by urgency; include concrete thresholds where possible (e.g. "if Brent sustains above $100...")
- If logs are empty, base the analysis on price moves alone and note the absence of news flow"""

    raw = run_text(prompt, model=DEEP_MODEL, timeout=300).strip()

    # Balanced-brace JSON extraction (handles nested objects reliably)
    json_str = _extract_json_object(raw)
    if not json_str:
        log.warning("Briefing synthesis: no JSON returned. Raw: %.300s", raw)
        return _fallback_synthesis()

    # Strip trailing commas before } or ] (common LLM JSON mistake)
    json_str = re.sub(r",\s*([}\]])", r"\1", json_str)

    try:
        result = json.loads(json_str)
        # Ensure new fields have defaults if the model omitted them
        result.setdefault("top_3", [])
        result.setdefault("decision_point", "No action required — continue monitoring.")
        return result
    except json.JSONDecodeError as e:
        log.warning("Briefing synthesis: JSON parse error: %s", e)
        return _fallback_synthesis()


def _fallback_synthesis() -> dict:
    """Return a safe default when synthesis fails."""
    return {
        "top_3": [],
        "decision_point": "Synthesis unavailable.",
        "macro_overview": "Synthesis unavailable.",
        "ticker_analyses": {},
        "themes": [],
        "watchpoints": [],
    }


# ── Discord posting ───────────────────────────────────────────────────────────

def _post_to_discord(
    briefing: dict,
    heat_map: str,
    channel_id: str,
    today: str,
) -> None:
    # Top 3 movers — prominent at the very top
    top_3_items = briefing.get("top_3", [])
    if top_3_items:
        top_3_block = "\n".join(f"{i+1}. {item}" for i, item in enumerate(top_3_items[:3]))
    else:
        top_3_block = "No major developments identified."

    # Decision point
    decision_point = briefing.get("decision_point", "No action required — continue monitoring.")

    ticker_block = "\n\n".join(
        f"**{ticker}**\n{body}"
        for ticker, body in briefing.get("ticker_analyses", {}).items()
        if body.strip()
    ) or "No material ticker developments today."

    themes_block = "\n\n".join(briefing.get("themes", [])) or "No cross-portfolio themes identified today."
    watchpoints_block = "\n".join(briefing.get("watchpoints", [])) or "No specific watchpoints."

    sections = [
        ("TOP 3",                    top_3_block),
        ("DECISION POINT",           decision_point),
        ("I. MACRO OVERVIEW",        briefing.get("macro_overview", "Unavailable.")),
        ("II. TICKER ANALYSIS",      ticker_block),
        ("III. PORTFOLIO HEAT MAP",  heat_map),
        ("IV. THEMES & CONNECTIONS", themes_block),
        ("V. KEY WATCHPOINTS",       watchpoints_block),
    ]

    send_briefing(channel_id, f"DAILY BRIEFING — {today}", sections)
    log.info("Portfolio briefing posted to #daily-briefing")


# ── Event log ─────────────────────────────────────────────────────────────────

def _write_event_log(briefing: dict, heat_map: str, today: str) -> None:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = EVENTS_DIR / f"{today}-briefing.md"
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Top 3
    top_3_items = briefing.get("top_3", [])
    top_3_text = "\n".join(f"{i+1}. {item}" for i, item in enumerate(top_3_items[:3])) if top_3_items else "None."

    lines = [
        f"# Daily Briefing — {today}",
        f"Generated: {now_str}\n",
        "## Top 3\n",
        top_3_text,
        "\n## Decision Point\n",
        briefing.get("decision_point", "No action required — continue monitoring."),
        "\n## I. Macro Overview\n",
        briefing.get("macro_overview", "Unavailable."),
        "\n## II. Ticker Analysis\n",
    ]

    for ticker, body in briefing.get("ticker_analyses", {}).items():
        if body.strip():
            lines.append(f"### {ticker}\n{body}\n")

    lines += [
        "\n## III. Portfolio Heat Map\n",
        heat_map,
        "\n## IV. Themes & Connections\n",
        "\n\n".join(briefing.get("themes", [])),
        "\n## V. Key Watchpoints\n",
        "\n".join(briefing.get("watchpoints", [])),
    ]

    log_file.write_text("\n".join(lines), encoding="utf-8")
    log.info("Portfolio briefing event log written: %s", log_file.name)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, date_str: str | None = None, mode: str = "morning") -> None:
    today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    posted_key = f"special/briefing-{mode}"
    log.info("=" * 60)
    log.info("Portfolio briefing | mode=%s | date=%s | dry_run=%s", mode, today, dry_run)
    log.info("=" * 60)

    # Step 1: Read event logs
    ticker_log = _read_ticker_log(today)
    macro_log  = _read_macro_log(today)
    log.info(
        "Event logs: ticker=%d chars, macro=%d chars",
        len(ticker_log), len(macro_log),
    )

    # Step 2: Load portfolio context
    portfolio_context = _load_portfolio_context()
    log.info("Portfolio context: %d chars loaded", len(portfolio_context))

    # Step 3: Fetch prices
    log.info("Fetching prices for %d tickers...", len(YAHOO_SYMBOLS))
    prices = _fetch_prices(today)
    available = sum(1 for v in prices.values() if v is not None)
    log.info("Price data: %d/%d tickers available", available, len(YAHOO_SYMBOLS))

    # Step 4: Build heat map (programmatic — no LLM)
    heat_map = _build_heat_map(prices)

    # Step 5: Synthesize with Claude Sonnet
    log.info("Synthesizing briefing with Claude Sonnet...")
    briefing = _synthesize_briefing(ticker_log, macro_log, prices, today, portfolio_context, mode=mode)
    log.info(
        "Synthesis complete: top_3=%d, macro=%d chars, %d tickers, %d themes, %d watchpoints",
        len(briefing.get("top_3", [])),
        len(briefing.get("macro_overview", "")),
        len(briefing.get("ticker_analyses", {})),
        len(briefing.get("themes", [])),
        len(briefing.get("watchpoints", [])),
    )

    if dry_run:
        print(f"\n{'='*60}")
        print(f"DAILY BRIEFING — {today}")
        print("="*60)
        print("\nTOP 3")
        for i, item in enumerate(briefing.get("top_3", [])[:3], 1):
            print(f"  {i}. {item}")
        print("\nDECISION POINT")
        print(f"  {briefing.get('decision_point', 'No action required — continue monitoring.')}")
        print("\nI. MACRO OVERVIEW")
        print(briefing.get("macro_overview", ""))
        print("\nII. TICKER ANALYSIS")
        for ticker, body in briefing.get("ticker_analyses", {}).items():
            print(f"\n{ticker}:\n{body}")
        print("\nIII. PORTFOLIO HEAT MAP")
        print(heat_map)
        print("\nIV. THEMES & CONNECTIONS")
        for theme in briefing.get("themes", []):
            print(f"\n{theme}")
        print("\nV. KEY WATCHPOINTS")
        for wp in briefing.get("watchpoints", []):
            print(f"  {wp}")
        # Write event log even in dry-run (useful for review)
        _write_event_log(briefing, heat_map, today)
        return

    # Step 6: Post to Discord
    channel_map = _load_channel_map()
    channel_id  = channel_map.get("special/daily-briefing")
    if not channel_id:
        log.warning("No channel ID for special/daily-briefing — run discord_setup.py")
    else:
        _post_to_discord(briefing, heat_map, channel_id, today)

    # Step 7: Write event log
    _write_event_log(briefing, heat_map, today)

    log.info("Portfolio briefing run complete.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate and post the daily portfolio briefing.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print output to stdout; skip Discord post.")
    parser.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD",
                        help="Override date for backfill runs.")
    parser.add_argument("--mode", choices=["morning", "asia"], default="morning",
                        help="morning = US overnight recap at 09:00 HKT; asia = Asian session recap at 17:30 HKT")
    args = parser.parse_args()
    run(dry_run=args.dry_run, date_str=args.date, mode=args.mode)
