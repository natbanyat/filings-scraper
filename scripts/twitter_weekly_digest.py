"""
Twitter signal weekly leaderboard — posts top-10 accounts by signal density
to #twitter-signal, then resets weekly counters.

Scheduled: Sunday 21:00 UTC (05:00 HKT Monday).

Manual:
  python scripts/twitter_weekly_digest.py
  python scripts/twitter_weekly_digest.py --dry-run
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(_SCRIPTS_DIR.parent / ".env")
except ImportError:
    pass

from utils import setup_logging
log = setup_logging("twitter_weekly_digest")

from cache import get_twitter_leaderboard, reset_twitter_weekly_stats
from post_discord import send_text

CHANNEL_MAP_PATH = Path(__file__).parent / "channel_map.json"
CHANNEL_KEY      = "special/twitter-signal"


def _load_channel_id() -> str | None:
    if not CHANNEL_MAP_PATH.exists():
        return None
    data = json.loads(CHANNEL_MAP_PATH.read_text())
    return data.get(CHANNEL_KEY)


def run(dry_run: bool = False) -> None:
    rows = get_twitter_leaderboard(limit=10)
    if not rows:
        log.info("No twitter stats to report yet.")
        return

    week_end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"**Weekly Signal Leaderboard — top accounts by signal density (week ending {week_end})**", ""]

    for rank, r in enumerate(rows, start=1):
        pct = f"{r['pass_rate'] * 100:.0f}%"
        lines.append(
            f"{rank:2d}. @{r['handle']} ({r['category']}) — "
            f"{r['passed_count']}/{r['fetched_count']} tweets passed ({pct})"
        )

    message = "\n".join(lines)

    if dry_run:
        print(message)
    else:
        channel_id = _load_channel_id()
        if not channel_id:
            log.error(
                "twitter-signal channel not found in channel_map.json. "
                "Run twitter_signal.py first to create the channel."
            )
            return
        try:
            send_text(channel_id, message)
            log.info("Posted weekly leaderboard to #twitter-signal")
        except Exception as e:
            log.error("Failed to post weekly leaderboard: %s", e)
            return

        reset_twitter_weekly_stats()
        log.info("Weekly stats reset.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Twitter signal weekly leaderboard")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print leaderboard without posting or resetting")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
