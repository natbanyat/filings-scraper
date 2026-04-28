"""
Weekly portfolio digest — cross-coverage summary.

Aggregates the past 7 days of event logs (ticker news + macro) and upcoming
catalysts, then uses Claude Sonnet to produce a weekly PM briefing.

Output:
  - Discord post to #weekly-digest channel
  - Written to events/weekly/YYYY-MM-DD-digest.md

Scheduled: Sunday ~22:00 UTC (after US close).

Manual:
  python scripts/weekly_digest.py
  python scripts/weekly_digest.py --dry-run
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from utils import setup_logging
log = setup_logging("weekly_digest")

from config import COVERAGE, COVERAGE_ROOT, resolve_coverage_file
from openclaw_gateway_model import DEEP_MODEL, GatewayModelError, run_text
from post_discord import send_embed
from utils import retry, extract_json_object

CHANNEL_MAP_PATH = Path(__file__).parent / "channel_map.json"
WORKSPACE_ROOT   = Path(__file__).resolve().parent.parent
EVENTS_TICKER    = WORKSPACE_ROOT / "events" / "ticker_news"
EVENTS_MACRO     = WORKSPACE_ROOT / "events" / "macro"
EVENTS_WEEKLY    = WORKSPACE_ROOT / "events" / "weekly"

def _load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        raise FileNotFoundError(
            "channel_map.json not found. Run 'python scripts/discord_setup.py' first."
        )
    return json.loads(CHANNEL_MAP_PATH.read_text())


def _collect_event_logs(events_dir: Path, days: int = 7) -> str:
    """Collect and concatenate event log files from the past N days."""
    if not events_dir.exists():
        return ""

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    parts = []

    for f in sorted(events_dir.glob("*.md")):
        try:
            # Parse date from filename: YYYY-MM-DD*.md
            date_str = f.stem[:10]
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if file_date >= cutoff:
                content = f.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"=== {f.stem} ===\n{content}")
        except (ValueError, OSError):
            continue

    return "\n\n".join(parts)


def _collect_catalysts_ahead(horizon: int = 14) -> str:
    """Collect catalysts.md excerpts for the upcoming horizon window."""
    parts = []
    for coverage_key in COVERAGE:
        path = resolve_coverage_file(COVERAGE_ROOT / coverage_key, "catalysts.md")
        if path.exists():
            content = path.read_text().strip()
            if content:
                name = coverage_key.split("/")[-1].upper()
                # Include near-term section only
                lines = content.split("\n")
                near_term_lines, capture = [], False
                for line in lines:
                    if "near-term" in line.lower():
                        capture = True
                    elif line.startswith("## ") and "near-term" not in line.lower() and capture:
                        break
                    if capture:
                        near_term_lines.append(line)
                excerpt = "\n".join(near_term_lines).strip()[:600]
                if excerpt:
                    parts.append(f"### {name}\n{excerpt}")

    return "\n\n".join(parts)


@retry(max_attempts=2, backoff=5.0, exceptions=(GatewayModelError,))
def _synthesize_digest(ticker_log: str, macro_log: str, catalysts_ahead: str, week_end: str) -> dict:
    """Use Claude Sonnet to generate a structured weekly digest."""

    # Truncate inputs to fit in context
    ticker_log_trunc  = ticker_log[:6000]   if ticker_log   else "No ticker news events logged this week."
    macro_log_trunc   = macro_log[:3000]    if macro_log    else "No macro close logs this week."
    catalysts_trunc   = catalysts_ahead[:2000] if catalysts_ahead else "No upcoming catalysts identified."

    prompt = f"""You are a portfolio manager's weekly briefing assistant. Today is {week_end}.
Summarize the week's developments across the covered portfolio and flag what matters next week.

TICKER NEWS EVENTS (past 7 days):
{ticker_log_trunc}

MACRO CLOSE LOGS (past 7 days):
{macro_log_trunc}

UPCOMING CATALYSTS (next 14 days):
{catalysts_trunc}

Write a structured weekly portfolio digest. Return ONLY valid JSON:
{{
  "headline": "One sentence capturing the single most important development of the week",
  "macro_summary": "3-4 sentences: key macro themes this week and what they mean for the portfolio",
  "top_movers": [
    {{"name": "TICKER", "development": "what happened", "direction": "bull|bear|neutral", "implication": "why it matters"}},
    ...
  ],
  "key_debates_updated": "1-2 sentences: did anything this week shift any of the key debates in the coverage universe?",
  "watch_next_week": [
    "Item 1 — what to watch and why",
    "Item 2",
    "Item 3"
  ],
  "upcoming_catalysts": [
    "Catalyst 1 — name, event, expected timing",
    "Catalyst 2",
    "Catalyst 3"
  ]
}}

top_movers: include up to 5 most significant developments (omit tickers with no material news).
watch_next_week: 3-5 specific, actionable watch items for next week.
upcoming_catalysts: 3-5 most important events in the next 14 days."""

    raw = run_text(prompt, model=DEEP_MODEL, timeout=240).strip()
    json_str = extract_json_object(raw)
    if not json_str:
        log.warning("Weekly digest: no JSON returned. Raw: %.300s", raw)
        return {"headline": "Weekly digest unavailable"}

    json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        log.warning("Weekly digest: JSON parse error: %s", e)
        return {"headline": "Weekly digest unavailable"}


def _build_digest_embed(digest: dict, week_end: str) -> dict:
    """Build a Discord embed from the digest dict."""
    fields = []

    # Top movers
    movers = digest.get("top_movers", [])
    if movers:
        direction_icon = {"bull": "▲", "bear": "▼", "neutral": "—"}
        val = "\n".join(
            f"{direction_icon.get(m.get('direction', 'neutral'), '—')} **{m['name']}** — {m['development']}\n_{m.get('implication', '')}_"
            for m in movers[:5]
        )
        fields.append({"name": "Top Movers", "value": val[:1020], "inline": False})

    # Macro summary
    macro = digest.get("macro_summary", "")
    if macro:
        fields.append({"name": "Macro", "value": macro[:1020], "inline": False})

    # Key debates
    debates = digest.get("key_debates_updated", "")
    if debates:
        fields.append({"name": "Key Debates", "value": debates[:1020], "inline": False})

    # Watch next week
    watch = digest.get("watch_next_week", [])
    if watch:
        val = "\n".join(f"• {item}" for item in watch[:5])
        fields.append({"name": "Watch Next Week", "value": val[:1020], "inline": False})

    # Upcoming catalysts
    upcoming = digest.get("upcoming_catalysts", [])
    if upcoming:
        val = "\n".join(f"• {item}" for item in upcoming[:5])
        fields.append({"name": "Upcoming Catalysts", "value": val[:1020], "inline": False})

    return {
        "title":       f"WEEKLY DIGEST — {week_end}",
        "description": digest.get("headline", ""),
        "color":       0x9B59B6,  # purple — weekly cadence
        "fields":      fields[:25],
        "footer":      {"text": "investing-agent | weekly portfolio briefing"},
    }


def _write_event_log(digest: dict, week_end: str) -> None:
    """Write the weekly digest to events/weekly/."""
    EVENTS_WEEKLY.mkdir(parents=True, exist_ok=True)
    log_file = EVENTS_WEEKLY / f"{week_end}-digest.md"

    lines = [
        f"# Weekly Digest — {week_end}\n",
        f"**Headline:** {digest.get('headline', '')}\n",
        f"## Macro\n{digest.get('macro_summary', '')}\n",
    ]

    movers = digest.get("top_movers", [])
    if movers:
        lines.append("## Top Movers\n")
        for m in movers:
            lines.append(f"### {m.get('name', '')} ({m.get('direction', '').upper()})")
            lines.append(f"{m.get('development', '')}")
            lines.append(f"_{m.get('implication', '')}_\n")

    debates = digest.get("key_debates_updated", "")
    if debates:
        lines.append(f"## Key Debates Updated\n{debates}\n")

    watch = digest.get("watch_next_week", [])
    if watch:
        lines.append("## Watch Next Week\n")
        for item in watch:
            lines.append(f"- {item}")
        lines.append("")

    upcoming = digest.get("upcoming_catalysts", [])
    if upcoming:
        lines.append("## Upcoming Catalysts\n")
        for item in upcoming:
            lines.append(f"- {item}")

    log_file.write_text("\n".join(lines), encoding="utf-8")
    log.info("Weekly digest written to %s", log_file.name)


def run(dry_run: bool = False) -> None:
    week_end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("=" * 60)
    log.info("Weekly digest | week_end=%s | dry_run=%s", week_end, dry_run)
    log.info("=" * 60)

    # Step 1: Collect inputs
    ticker_log      = _collect_event_logs(EVENTS_TICKER, days=7)
    macro_log       = _collect_event_logs(EVENTS_MACRO, days=7)
    catalysts_ahead = _collect_catalysts_ahead(horizon=14)

    log.info("Collected: ticker_log=%d chars, macro_log=%d chars, catalysts=%d chars",
             len(ticker_log), len(macro_log), len(catalysts_ahead))

    # Step 2: Synthesize
    digest = _synthesize_digest(ticker_log, macro_log, catalysts_ahead, week_end)
    log.info("Digest synthesized: %s", digest.get("headline", "")[:80])

    if dry_run:
        log.info("[DRY RUN] Weekly digest:")
        for k, v in digest.items():
            log.info("  %s: %s", k, str(v)[:200])
        return

    # Step 3: Post to Discord
    channel_map = _load_channel_map()
    channel_id = channel_map.get("special/weekly-digest")
    if not channel_id:
        log.warning("No channel ID for special/weekly-digest — run discord_setup.py")
    else:
        embed = _build_digest_embed(digest, week_end)
        send_embed(channel_id, embed)
        log.info("Weekly digest posted to #weekly-digest")

    # Step 4: Write event log
    _write_event_log(digest, week_end)

    log.info("Weekly digest run complete.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)
