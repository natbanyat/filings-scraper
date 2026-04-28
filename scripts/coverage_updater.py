"""
Coverage file updater — keeps thesis, KPI tree, catalysts, and debates current.

Recommended cadence:
  catalysts.md  — monthly    (near-term table rotates quickly)
  debates.md    — quarterly  (after earnings: Feb/May/Aug/Nov)
  kpi_tree.md   — quarterly  (update actuals and watchpoints after earnings)
  thesis.md     — quarterly  (only on view-changing events or major thesis shifts)

Triggers:
  earnings       — auto-flagged by daily_news.py; run this script to process
  monthly        — cron 1st of each month; updates catalysts only
  quarterly      — cron Feb/May/Aug/Nov; updates all four files
  manual         — run directly with --coverage-key

Workflow:
  1. Reads current coverage files for the ticker
  2. Reads recent event logs and any new news (via Brave)
  3. Uses Claude Sonnet to propose specific, diff-style updates
  4. Writes proposed changes to events/coverage_updates/<key>/<date>/
  5. Posts summary to #coverage-updates Discord
  6. Run with --apply to overwrite the actual coverage files

Usage:
  python scripts/coverage_updater.py --coverage-key tickers/MMYT --trigger earnings
  python scripts/coverage_updater.py --trigger monthly          # catalysts for all coverage
  python scripts/coverage_updater.py --trigger quarterly        # all files for all coverage
  python scripts/coverage_updater.py --coverage-key tickers/JPM --trigger earnings --apply
  python scripts/coverage_updater.py --pending                  # process all flagged tickers
"""

import argparse
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
log = setup_logging("coverage_updater")

from config import COVERAGE, COVERAGE_ROOT, SEARCH_QUERIES, resolve_coverage_file
from fetch_news import fetch_news, deduplicate, deduplicate_similar
from openclaw_gateway_model import DEEP_MODEL, GatewayModelError, run_text
from post_discord import send_text
from utils import retry, extract_json_object

CHANNEL_MAP_PATH  = Path(__file__).parent / "channel_map.json"
WORKSPACE_ROOT    = Path(__file__).resolve().parent.parent
EVENTS_TICKER     = WORKSPACE_ROOT / "events" / "ticker_news"
UPDATE_FLAGS_DIR  = WORKSPACE_ROOT / "events" / "pending_updates"
UPDATES_OUT_DIR   = WORKSPACE_ROOT / "events" / "coverage_updates"

# Which files to update per trigger type
TRIGGER_FILES = {
    "earnings":  ["catalysts.md", "kpi_tree.md", "debates.md", "thesis.md"],
    "quarterly": ["catalysts.md", "kpi_tree.md", "debates.md", "thesis.md"],
    "monthly":   ["catalysts.md"],
}

def _load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        raise FileNotFoundError(
            "channel_map.json not found. Run 'python scripts/discord_setup.py' first."
        )
    return json.loads(CHANNEL_MAP_PATH.read_text())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _collect_recent_events(coverage_key: str, days: int = 60) -> str:
    """Collect event log entries for this specific ticker from the past N days."""
    if not EVENTS_TICKER.exists():
        return ""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    ticker = coverage_key.split("/")[-1].upper()
    parts = []
    for f in sorted(EVENTS_TICKER.glob("*.md")):
        try:
            date_str = f.stem[:10]
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if file_date < cutoff:
                continue
            content = f.read_text(encoding="utf-8")
            # Extract only sections for this ticker
            sections = re.findall(
                rf"## {re.escape(ticker)}.*?(?=\n## |\Z)", content, re.DOTALL
            )
            if sections:
                parts.append(f"=== {f.stem} ===\n" + "\n".join(sections))
        except (ValueError, OSError):
            continue
    return "\n\n".join(parts)


@retry(max_attempts=2, backoff=5.0, exceptions=(GatewayModelError,))
def _generate_update(
    coverage_key: str,
    trigger: str,
    files_to_update: list[str],
    current_files: dict[str, str],
    recent_events: str,
    fresh_news: list[dict],
    today: str,
) -> dict[str, str]:
    """
    Use Claude Sonnet to generate updated content for each coverage file.
    Returns dict of {filename: updated_content}.
    """
    name = coverage_key.split("/")[-1].upper()

    fresh_news_block = "\n".join(
        f"- {a['title']} | {a['source']} | {a['description'][:200]}"
        for a in fresh_news[:8]
    ) or "No fresh news available."

    recent_events_block = recent_events[:2500] or "No recent event logs."

    # Build current files block
    current_block = "\n\n".join(
        f"=== CURRENT {fname} ===\n{content}"
        for fname, content in current_files.items()
        if content
    )

    files_list = ", ".join(files_to_update)

    prompt = f"""You are updating investment research coverage files for {name}.
Today is {today}. Trigger: {trigger.upper()}.

Files to update: {files_list}

━━━ CURRENT COVERAGE FILES ━━━
{current_block[:3500]}

━━━ RECENT EVENT LOGS (past 60 days) ━━━
{recent_events_block}

━━━ FRESH NEWS (today) ━━━
{fresh_news_block}

━━━ TASK ━━━

For each file listed in "Files to update", produce a complete updated version.
Rules:
- Preserve the existing structure and formatting exactly
- Update only what the new information justifies — do not rewrite everything
- For catalyst/watchlist files (catalysts.md or watchlist.md): refresh the near-term table or dated watch items; remove expired entries and add new ones with correct timing
- For KPI files (kpi_tree.md or kpis.md): update any actuals, watchpoints, or thresholds that new data informs
- For debates.md: update "Current position" or add a new debate if events warrant it; note what data shifted each debate
- For thesis.md: only update if the event is genuinely view-changing; add to Key risks or Variant view if needed; preserve the core thesis unless fundamentally challenged
- At the top of each updated file, add a one-line comment: <!-- Updated {today}: <reason> -->

Return ONLY valid JSON with one key per filename:
{{
  "catalysts.md": "full updated file content here",
  "kpi_tree.md": "full updated file content here",
  ...
}}

Only include keys for the files in "{files_list}". Do not include files not listed."""

    raw = run_text(prompt, model=DEEP_MODEL, timeout=240).strip()
    json_str = extract_json_object(raw)
    if not json_str:
        log.warning("%s: updater returned no JSON. Raw: %.200s", name, raw)
        return {}

    json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
    try:
        updates = json.loads(json_str)
        # Keep only the files we actually requested
        return {k: v for k, v in updates.items() if k in files_to_update}
    except json.JSONDecodeError as e:
        log.warning("%s: updater JSON parse error: %s", name, e)
        return {}


def _write_proposals(coverage_key: str, updates: dict[str, str], today: str) -> Path:
    """Save proposed updates to the review folder."""
    safe_key = coverage_key.replace("/", "_")
    out_dir = UPDATES_OUT_DIR / safe_key / today
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, content in updates.items():
        (out_dir / fname).write_text(content, encoding="utf-8")
    log.info("%s: proposals written to %s", coverage_key, out_dir)
    return out_dir


def _apply_updates(coverage_key: str, updates: dict[str, str]) -> None:
    """Overwrite the actual coverage files with the proposed updates."""
    coverage_path = COVERAGE_ROOT / coverage_key
    for fname, content in updates.items():
        target = coverage_path / fname
        # Keep a backup
        backup = target.with_suffix(f".bak.{datetime.now(timezone.utc).strftime('%Y%m%d')}")
        if target.exists():
            backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        target.write_text(content, encoding="utf-8")
        log.info("%s: applied %s (backup at %s)", coverage_key, fname, backup.name)


def _build_discord_message(
    coverage_key: str, trigger: str, updates: dict, out_dir: Path, applied: bool
) -> str:
    name = coverage_key.split("/")[-1].upper()
    files_updated = ", ".join(f"`{f}`" for f in updates)
    status = "**APPLIED**" if applied else "**PROPOSED** (review at events/coverage_updates/)"
    return (
        f"**COVERAGE UPDATE — {name}** ({trigger})\n"
        f"Files: {files_updated}\n"
        f"Status: {status}\n"
        f"Path: `{out_dir.relative_to(WORKSPACE_ROOT)}`\n\n"
        f"Run with `--apply` to write changes to coverage files."
        if not applied else
        f"**COVERAGE UPDATE — {name}** ({trigger})\n"
        f"Files updated: {files_updated}\n"
        f"Backups saved alongside original files."
    )


def process_one(
    coverage_key: str,
    trigger: str,
    apply: bool = False,
    dry_run: bool = False,
) -> None:
    name = coverage_key.split("/")[-1].upper()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    coverage_path = COVERAGE_ROOT / coverage_key
    logical_files = TRIGGER_FILES.get(trigger, TRIGGER_FILES["quarterly"])
    files_to_update = list(dict.fromkeys(
        resolve_coverage_file(coverage_path, fname).name for fname in logical_files
    ))

    log.info("── %s | trigger=%s | files=%s ──", name, trigger, files_to_update)

    # Read current coverage files
    current_files = {
        fname: _read(coverage_path / fname)
        for fname in files_to_update
    }

    missing = [f for f, c in current_files.items() if not c]
    if missing:
        log.warning("%s: missing coverage files: %s", name, missing)

    # Collect context
    recent_events = _collect_recent_events(coverage_key, days=60)
    query = SEARCH_QUERIES.get(coverage_key, name)
    try:
        fresh_news = deduplicate_similar(deduplicate(fetch_news(query, count=15, freshness="pw")))
    except Exception as e:
        log.warning("%s: fresh news fetch failed: %s", name, e)
        fresh_news = []

    log.info("%s: %d recent event log chars, %d fresh articles", name, len(recent_events), len(fresh_news))

    if dry_run:
        log.info("%s [DRY RUN] — would update: %s", name, files_to_update)
        return

    # Generate updates
    updates = _generate_update(
        coverage_key, trigger, files_to_update,
        current_files, recent_events, fresh_news, today,
    )

    if not updates:
        log.warning("%s: no updates generated", name)
        return

    # Write proposals
    out_dir = _write_proposals(coverage_key, updates, today)

    # Optionally apply
    if apply:
        _apply_updates(coverage_key, updates)

    # Post to Discord
    try:
        channel_map = _load_channel_map()
        channel_id = channel_map.get("special/coverage-updates")
        if channel_id:
            msg = _build_discord_message(coverage_key, trigger, updates, out_dir, apply)
            send_text(channel_id, msg)
    except Exception as e:
        log.warning("%s: Discord post failed: %s", name, e)

    log.info("%s: update complete (%s)", name, "applied" if apply else "proposed only")


def process_pending(apply: bool = False, dry_run: bool = False) -> None:
    """Process all tickers flagged by daily_news.py as needing updates."""
    if not UPDATE_FLAGS_DIR.exists():
        log.info("No pending update flags found.")
        return

    flags = sorted(UPDATE_FLAGS_DIR.glob("*.json"))
    if not flags:
        log.info("No pending update flags found.")
        return

    log.info("Found %d pending update flag(s)", len(flags))
    processed = set()

    for flag_file in flags:
        try:
            payload = json.loads(flag_file.read_text())
            key     = payload["coverage_key"]
            trigger = payload.get("trigger", "earnings")

            if key in processed:
                if not dry_run:
                    flag_file.unlink()
                continue  # already handled this ticker today

            process_one(key, trigger, apply=apply, dry_run=dry_run)
            processed.add(key)
            if not dry_run:
                flag_file.unlink()  # remove flag after processing

        except Exception as e:
            log.error("Failed to process flag %s: %s", flag_file.name, e)


def process_all(trigger: str, apply: bool = False, dry_run: bool = False) -> None:
    """Run updates for every coverage item (used for monthly/quarterly cron)."""
    targets = list(COVERAGE.keys())
    log.info("Running %s update for all %d coverage items", trigger, len(targets))
    for key in targets:
        try:
            process_one(key, trigger, apply=apply, dry_run=dry_run)
        except Exception as e:
            log.error("%s: unhandled error — %s", key, e, exc_info=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Coverage file updater")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--coverage-key", help="Single coverage key, e.g. tickers/MMYT")
    group.add_argument("--pending",  action="store_true", help="Process all pending flags from daily_news.py")
    group.add_argument("--all",      action="store_true", help="Update all coverage items")

    parser.add_argument("--trigger",  default="earnings",
                        choices=["earnings", "monthly", "quarterly"],
                        help="What triggered this update (default: earnings)")
    parser.add_argument("--apply",    action="store_true",
                        help="Apply changes to coverage files (default: propose only)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="No writes, no Discord posts")

    args = parser.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("=" * 60)
    log.info("Coverage updater | trigger=%s | apply=%s | dry_run=%s | date=%s",
             args.trigger, args.apply, args.dry_run, today)
    log.info("=" * 60)

    if args.pending:
        process_pending(apply=args.apply, dry_run=args.dry_run)
    elif args.all:
        process_all(args.trigger, apply=args.apply, dry_run=args.dry_run)
    else:
        if args.coverage_key not in COVERAGE:
            log.error("Unknown coverage key: %s. Valid keys: %s", args.coverage_key, list(COVERAGE.keys()))
            sys.exit(1)
        process_one(args.coverage_key, args.trigger, apply=args.apply, dry_run=args.dry_run)

    log.info("Coverage updater complete.")


if __name__ == "__main__":
    main()
