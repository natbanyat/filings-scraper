"""
Weekly Cowork sync — compile material events and save to OneDrive.

Reads update_log.md for each coverage item, synthesizes a structured weekly
summary using Sonnet, and writes it to the shared OneDrive folder so Cowork
can ingest it to update thesis/KPIs/catalysts.

Output: ONEDRIVE_DIR/weekly-sync-YYYY-MM-DD.md

Scheduled: Sunday ~22:30 UTC (after weekly_digest.py).

Manual:
  python scripts/weekly_cowork_sync.py
  python scripts/weekly_cowork_sync.py --dry-run
"""

import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from utils import setup_logging, retry

log = setup_logging("weekly_cowork_sync")
from openclaw_gateway_model import DEEP_MODEL, GatewayModelError, run_text

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_ROOT = WORKSPACE_ROOT / "coverage"
PORTFOLIO_CONTEXT_PATH = WORKSPACE_ROOT / "PORTFOLIO_CONTEXT.md"
ONEDRIVE_DIR = Path("/mnt/c/Users/natba/OneDrive/@ Cowork/openclaw-investing-context")

def _collect_update_logs(days: int = 7) -> dict[str, str]:
    """Collect recent update_log.md entries per coverage item."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    logs: dict[str, str] = {}

    for log_file in sorted(COVERAGE_ROOT.rglob("update_log.md")):
        # Derive coverage key from path: coverage/tickers/JPM/update_log.md -> tickers/JPM
        rel = log_file.parent.relative_to(COVERAGE_ROOT)
        coverage_key = str(rel)

        text = log_file.read_text(encoding="utf-8").strip()
        if not text:
            continue

        # Extract entries from the past N days
        lines = text.split("\n")
        recent = []
        include = False
        for line in lines:
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", line)
            if date_match:
                include = date_match.group(1) >= cutoff
            if include:
                recent.append(line)

        if recent:
            logs[coverage_key] = "\n".join(recent)

    return logs


def _load_portfolio_context() -> str:
    if PORTFOLIO_CONTEXT_PATH.exists():
        return PORTFOLIO_CONTEXT_PATH.read_text(encoding="utf-8").strip()
    return ""


@retry(max_attempts=3, backoff=5.0, exceptions=(GatewayModelError,))
def _synthesize(update_logs: dict[str, str], portfolio_context: str) -> str:
    """Use Sonnet to create a structured weekly synthesis for Cowork."""
    logs_block = "\n\n".join(
        f"=== {key} ===\n{text}" for key, text in update_logs.items()
    )

    prompt = f"""You are synthesizing a week of material events from an automated financial news
monitoring system (OpenClaw) into a structured update for a portfolio management system (Cowork).

CURRENT PORTFOLIO CONTEXT:
{portfolio_context[:3000]}

MATERIAL EVENTS THIS WEEK (per coverage item):
{logs_block[:12000]}

Write a structured weekly synthesis that Cowork can use to update thesis, KPIs, catalysts,
and key debates for each name. Use this format:

# OpenClaw Weekly Sync -- {datetime.now(timezone.utc).strftime("%Y-%m-%d")}

## Summary
[3-5 bullet points: most important developments across the portfolio this week]

## Per-Name Updates

### [coverage_key] -- [Name]
**Events:** [bullet list of material events]
**Thesis impact:** [1-2 sentences: does this confirm, challenge, or modify the thesis?]
**KPI updates:** [any quantitative data points that should update KPI tracking]
**Catalyst changes:** [catalysts triggered, deferred, or newly emerged]
**Key debate shift:** [which debate was informed and in which direction]

[Repeat for each name with material events]

## Cross-Portfolio Themes
[2-3 themes that cut across multiple names]

## Watchlist for Next Week
[5-7 specific items to watch]

Rules:
- Only include names that had material events (skip quiet names)
- Be specific and quantitative where possible
- Frame everything relative to the existing thesis and portfolio context
- Flag any event that is view-changing or challenges the current thesis
- Tone: institutional, direct, no filler"""

    return run_text(prompt, model=DEEP_MODEL, timeout=300).strip()


def run(dry_run: bool = False) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("=" * 60)
    log.info("Weekly Cowork sync | date=%s | dry_run=%s", today, dry_run)
    log.info("=" * 60)

    update_logs = _collect_update_logs(days=7)
    if not update_logs:
        log.warning("No update_log entries found for the past 7 days -- nothing to sync")
        return

    log.info("Found update logs for %d coverage items: %s",
             len(update_logs), list(update_logs.keys()))

    portfolio_context = _load_portfolio_context()
    synthesis = _synthesize(update_logs, portfolio_context)

    if dry_run:
        print("\n" + synthesis)
        log.info("[DRY RUN] Synthesis generated but not written to OneDrive")
        return

    # Write to OneDrive shared folder
    if not ONEDRIVE_DIR.exists():
        log.error("OneDrive directory not found: %s", ONEDRIVE_DIR)
        log.info("Writing to local events/weekly/ instead")
        out_dir = WORKSPACE_ROOT / "events" / "weekly"
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = ONEDRIVE_DIR

    out_file = out_dir / f"weekly-sync-{today}.md"
    out_file.write_text(synthesis, encoding="utf-8")
    log.info("Weekly sync written to %s", out_file)

    print("\n" + synthesis)
    log.info("Weekly Cowork sync complete.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)
