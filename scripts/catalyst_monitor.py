"""
Catalyst monitor — proactive calendar-driven event alerts.

Reads catalysts.md for every covered ticker/sector/market and uses Claude
to identify events that are coming up in the next N days (default: 7).

On a match, posts an alert embed to #catalyst-alerts Discord channel.

Scheduled: run daily alongside the US close (cron at ~21:00 UTC).

Manual:
  python scripts/catalyst_monitor.py
  python scripts/catalyst_monitor.py --dry-run
  python scripts/catalyst_monitor.py --horizon 14   # look 14 days ahead
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
log = setup_logging("catalyst_monitor")

from config import COVERAGE, COVERAGE_ROOT, resolve_coverage_file
from post_discord import send_embed
from utils import retry
from openclaw_gateway_model import CHEAP_MODEL, GatewayModelError, run_json

CHANNEL_MAP_PATH  = Path(__file__).parent / "channel_map.json"
WORKSPACE_ROOT    = Path(__file__).resolve().parent.parent
EARNINGS_CAL_PATH = WORKSPACE_ROOT / "coverage" / "earnings_calendar.json"

def _load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        raise FileNotFoundError(
            "channel_map.json not found. Run 'python scripts/discord_setup.py' first."
        )
    return json.loads(CHANNEL_MAP_PATH.read_text())


def _load_earnings_calendar_alerts(today_str: str, horizon: int) -> list[dict]:
    """
    Read earnings_calendar.json and return precise date-based alerts.
    Only returns unprocessed entries within the horizon window.
    """
    if not EARNINGS_CAL_PATH.exists():
        return []

    try:
        cal = json.loads(EARNINGS_CAL_PATH.read_text())
    except Exception as e:
        log.debug("Could not load earnings_calendar.json: %s", e)
        return []

    today      = datetime.strptime(today_str, "%Y-%m-%d").date()
    cutoff     = today + timedelta(days=horizon)
    alerts     = []

    for coverage_key, entry in cal.items():
        if coverage_key.startswith("_"):
            continue
        name = coverage_key.split("/")[-1].upper()
        for e in entry.get("entries", []):
            if e.get("processed"):
                continue
            expected = e.get("expected_date")
            if not expected:
                continue
            try:
                exp_date = datetime.strptime(expected, "%Y-%m-%d").date()
            except ValueError:
                continue

            if exp_date > cutoff:
                continue

            delta = (exp_date - today).days
            if delta < 0:
                # Past but unprocessed — still surface as "today" urgency
                if delta < -3:
                    continue  # more than 3 days past, skip
                urgency = "today"
                timing  = "results expected (may already be out)"
            elif delta == 0:
                urgency = "today"
                timing  = e.get("expected_time", "unknown time")
            elif delta <= 5:
                urgency = "this_week"
                timing  = f"{expected} ({e.get('expected_time', 'unknown time')})"
            else:
                urgency = "upcoming"
                timing  = f"{expected} ({e.get('expected_time', 'unknown time')})"

            period = e.get("period", "")
            source = e.get("source", "calendar")
            confirmed = e.get("confirmed", False)
            note = f"{period} earnings {'(confirmed)' if confirmed else '(expected)'}. Source: {source}."

            alerts.append({
                "coverage_key": coverage_key,
                "name":         name,
                "event":        f"{period} Earnings",
                "timing":       timing,
                "direction":    "watch",
                "urgency":      urgency,
                "note":         note,
                "_from_calendar": True,
            })

    log.info("Earnings calendar: %d upcoming earnings event(s) in %d-day horizon", len(alerts), horizon)
    return alerts


def _read_catalysts(coverage_key: str) -> str:
    path = resolve_coverage_file(COVERAGE_ROOT / coverage_key, "catalysts.md")
    if not path.exists():
        return ""
    return path.read_text().strip()


@retry(max_attempts=2, backoff=5.0, exceptions=(GatewayModelError,))
def _extract_upcoming(coverage_key: str, catalysts_text: str, today: str, horizon: int) -> list[dict]:
    """
    Ask Claude to extract upcoming events from catalysts.md.
    Returns list of dicts: {name, coverage_key, event, timing, direction, urgency}
    urgency: "today" | "this_week" | "upcoming"
    """
    name = coverage_key.split("/")[-1].upper()

    prompt = f"""Today is {today}. You are analyzing upcoming catalysts for {name}.

CATALYSTS FILE:
{catalysts_text[:2000]}

Identify any events in the file that are likely to occur within the next {horizon} days from today.
Consider:
- Earnings calendar entries (e.g., "Q1 results: ~mid-April" → is mid-April within {horizon} days of {today}?)
- Specific dated events (BOJ meetings, Fed meetings, regulatory decisions)
- Recurring monthly data releases if they fall in this window

Return ONLY valid JSON array (empty array [] if nothing is upcoming):
[
  {{
    "event": "short event description",
    "timing": "approximate timing (e.g., 'this week', 'next week', '~mid-April')",
    "direction": "bull|bear|watch",
    "urgency": "today|this_week|upcoming",
    "note": "why this matters in 1 sentence"
  }},
  ...
]

Only include events genuinely within {horizon} days. If uncertain, include with urgency "upcoming".
Return [] if nothing is likely within the window."""

    try:
        hits = run_json(prompt, expected="array", model=os.environ.get("OPENCLAW_CATALYST_MODEL") or CHEAP_MODEL)
        for h in hits:
            h["coverage_key"] = coverage_key
            h["name"] = name
        return hits
    except GatewayModelError:
        log.warning("Catalyst parse error for %s", coverage_key)
        return []


def _build_alert_embed(alerts: list[dict], today: str, horizon: int) -> dict:
    """Build a Discord embed listing all upcoming catalyst alerts."""
    today_alerts    = [a for a in alerts if a.get("urgency") == "today"]
    week_alerts     = [a for a in alerts if a.get("urgency") == "this_week"]
    upcoming_alerts = [a for a in alerts if a.get("urgency") == "upcoming"]

    fields = []

    if today_alerts:
        val = "\n".join(
            f"**{a['name']}** — {a['event']} ({a.get('timing', '')})\n_{a.get('note', '')}_"
            for a in today_alerts
        )
        fields.append({"name": "TODAY", "value": val[:1020], "inline": False})

    if week_alerts:
        val = "\n".join(
            f"**{a['name']}** — {a['event']} ({a.get('timing', '')})\n_{a.get('note', '')}_"
            for a in week_alerts
        )
        fields.append({"name": "THIS WEEK", "value": val[:1020], "inline": False})

    if upcoming_alerts:
        val = "\n".join(
            f"**{a['name']}** — {a['event']} ({a.get('timing', '')})"
            for a in upcoming_alerts
        )
        fields.append({"name": f"NEXT {horizon} DAYS", "value": val[:1020], "inline": False})

    color = 0xE74C3C if today_alerts else (0xF1C40F if week_alerts else 0x3498DB)

    return {
        "title":       f"CATALYST MONITOR — {today}",
        "description": f"{len(alerts)} upcoming event(s) across {len(set(a['coverage_key'] for a in alerts))} coverage items",
        "color":       color,
        "fields":      fields[:25],
        "footer":      {"text": f"investing-agent | {horizon}-day horizon"},
    }


def run(dry_run: bool = False, horizon: int = 7) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("=" * 60)
    log.info("Catalyst monitor | today=%s | horizon=%d days | dry_run=%s",
             today, horizon, dry_run)
    log.info("=" * 60)

    all_alerts: list[dict] = []

    # Inject precise earnings dates from calendar first
    calendar_alerts = _load_earnings_calendar_alerts(today, horizon)
    all_alerts.extend(calendar_alerts)
    calendar_keys_with_alerts = {a["coverage_key"] for a in calendar_alerts}

    for coverage_key in COVERAGE:
        name = coverage_key.split("/")[-1].upper()
        catalysts_text = _read_catalysts(coverage_key)
        if not catalysts_text:
            log.debug("%s: no catalysts.md — skipping", name)
            continue

        # Skip Claude parsing for earnings events on tickers that have precise calendar dates
        # (to avoid double-counting); Claude still parses other catalysts in the file
        try:
            alerts = _extract_upcoming(coverage_key, catalysts_text, today, horizon)
            if alerts:
                # Filter out fuzzy earnings events for tickers where calendar already provided one
                if coverage_key in calendar_keys_with_alerts:
                    alerts = [
                        a for a in alerts
                        if not any(kw in a.get("event", "").lower()
                                   for kw in ("earnings", "results", "quarterly", "fiscal"))
                    ]
                if alerts:
                    log.info("%s: %d upcoming catalyst(s) (from catalysts.md)", name, len(alerts))
                    all_alerts.extend(alerts)
            else:
                log.debug("%s: nothing in %d-day horizon", name, horizon)
        except Exception as e:
            log.error("%s: catalyst check failed — %s", name, e)

    log.info("Total upcoming catalysts: %d across %d coverage items",
             len(all_alerts), len(set(a["coverage_key"] for a in all_alerts)))

    if not all_alerts:
        log.info("No upcoming catalysts in %d-day horizon — nothing to post", horizon)
        return

    if dry_run:
        log.info("[DRY RUN] Upcoming catalysts:")
        for a in all_alerts:
            log.info("  [%s] %s — %s (%s) [%s]",
                     a.get("urgency", "").upper(), a["name"], a["event"],
                     a.get("timing", ""), a.get("direction", ""))
        return

    # Post to Discord
    channel_map = _load_channel_map()
    channel_id = channel_map.get("special/catalyst-alerts")
    if not channel_id:
        log.warning("No channel ID for special/catalyst-alerts — run discord_setup.py")
        return

    embed = _build_alert_embed(all_alerts, today, horizon)
    send_embed(channel_id, embed)
    log.info("Catalyst alerts posted to #catalyst-alerts")
    log.info("Catalyst monitor complete.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv

    horizon = 7
    for arg in sys.argv[1:]:
        if arg.startswith("--horizon="):
            try:
                horizon = int(arg.split("=")[1])
            except ValueError:
                pass
        elif arg == "--horizon" and sys.argv.index(arg) + 1 < len(sys.argv):
            try:
                horizon = int(sys.argv[sys.argv.index(arg) + 1])
            except ValueError:
                pass

    run(dry_run=dry_run, horizon=horizon)
