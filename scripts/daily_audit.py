"""
Daily pipeline audit — checks what the news/macro/portfolio pipeline produced
for a given date and writes a structured review.

Quiet by default: no stdout output.  All results go to:
  reviews/daily/YYYY-MM-DD.md
  reviews/daily/YYYY-MM-DD.json

Usage:
  python scripts/daily_audit.py                  # audit today HKT
  python scripts/daily_audit.py --date 2026-04-02
  python scripts/daily_audit.py --dry-run        # show what would be written, no file write
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import setup_logging
from config import COVERAGE, CLOSE_WINDOWS

log = setup_logging("daily_audit")

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
EVENTS_TICKER  = WORKSPACE_ROOT / "events" / "ticker_news"
EVENTS_MACRO   = WORKSPACE_ROOT / "events" / "macro"
EVENTS_PORTFOLIO = WORKSPACE_ROOT / "events" / "portfolio"
REVIEWS_DIR    = WORKSPACE_ROOT / "reviews" / "daily"

# Keys expected to have run by end-of-HKT-day (Asia windows only — US runs after midnight HKT)
_ASIA_WINDOWS = {"asia_japan", "asia_korea", "asia_hk"}


# ── Parsers ────────────────────────────────────────────────────────────────────

def _parse_ticker_news(date_str: str) -> dict:
    """
    Parse events/ticker_news/YYYY-MM-DD.md.

    Returns:
      {
        "file": str | None,
        "entries": [{"heading": str, "utc_time": str, "text_preview": str}],
        "coverage_keys_found": [str],  # matched against COVERAGE keys
      }
    """
    path = EVENTS_TICKER / f"{date_str}.md"
    result = {"file": None, "entries": [], "coverage_keys_found": []}

    if not path.exists():
        return result

    result["file"] = str(path.relative_to(WORKSPACE_ROOT))
    text = path.read_text(encoding="utf-8")

    # Each entry header: ## <NAME> — HH:MM UTC  (or similar)
    for m in re.finditer(r"^##\s+(.+?)\s+[—–-]+\s+(\d{1,2}:\d{2}\s*UTC)", text, re.MULTILINE):
        heading  = m.group(1).strip()
        utc_time = m.group(2).strip()
        # Grab first non-blank line after heading as preview
        after = text[m.end():]
        preview_lines = [ln.strip() for ln in after.splitlines() if ln.strip()]
        preview = preview_lines[0][:120] if preview_lines else ""
        result["entries"].append({"heading": heading, "utc_time": utc_time, "text_preview": preview})

    # Map headings to COVERAGE keys (channel names or short ticker names)
    channel_to_key = {v["channel"]: k for k, v in COVERAGE.items()}
    for entry in result["entries"]:
        h = entry["heading"].lower()
        for channel, key in channel_to_key.items():
            if channel.lower() in h or h in channel.lower():
                if key not in result["coverage_keys_found"]:
                    result["coverage_keys_found"].append(key)

    return result


def _find_macro_files(date_str: str) -> list[str]:
    """Return list of macro event files matching date_str (relative paths)."""
    found = []
    for p in EVENTS_MACRO.glob(f"{date_str}*.md"):
        found.append(str(p.relative_to(WORKSPACE_ROOT)))
    return sorted(found)


def _find_portfolio_file(date_str: str) -> str | None:
    """Return relative path to portfolio briefing for date, or None."""
    for p in EVENTS_PORTFOLIO.glob(f"{date_str}*.md"):
        return str(p.relative_to(WORKSPACE_ROOT))
    return None


# ── Coverage completeness check ────────────────────────────────────────────────

def _expected_asia_keys() -> list[str]:
    """Coverage keys with Asia close windows (expected to run by 23:00 HKT)."""
    return [k for k, v in COVERAGE.items() if v["close"] in _ASIA_WINDOWS]


def _audit(date_str: str) -> dict:
    """Run the full audit for date_str and return structured result dict."""
    ticker_news = _parse_ticker_news(date_str)
    macro_files  = _find_macro_files(date_str)
    portfolio_file = _find_portfolio_file(date_str)

    expected_asia = _expected_asia_keys()
    found_keys    = ticker_news["coverage_keys_found"]
    found_asia    = [k for k in found_keys if k in expected_asia]
    missing_keys  = [k for k in expected_asia if k not in found_keys]
    extra_keys    = [k for k in found_keys if k not in expected_asia]

    # Per-window breakdown
    window_coverage: dict[str, list[str]] = {w: [] for w in _ASIA_WINDOWS}
    for key in found_keys:
        win = COVERAGE.get(key, {}).get("close")
        if win and win in window_coverage:
            window_coverage[win].append(key)

    flags: list[str] = []
    if not ticker_news["file"]:
        flags.append("NO_TICKER_NEWS_FILE")
    if not macro_files:
        flags.append("NO_MACRO_FILE")
    if missing_keys:
        flags.append(f"MISSING_COVERAGE: {', '.join(missing_keys)}")

    return {
        "audit_date":     date_str,
        "generated_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ticker_news": {
            "file":   ticker_news["file"],
            "entries": len(ticker_news["entries"]),
            "coverage_keys_found": found_keys,
        },
        "macro": {
            "files": macro_files,
            "count": len(macro_files),
        },
        "portfolio": {
            "file":  portfolio_file,
            "found": portfolio_file is not None,
        },
        "coverage": {
            "expected_asia_count": len(expected_asia),
            "found_asia_count":    len(found_asia),
            "found_total_count":   len(found_keys),
            "missing":             missing_keys,
            "extra_us":            extra_keys,
            "by_window":           window_coverage,
        },
        "flags": flags,
        "entries": ticker_news["entries"],
    }


# ── Formatters ─────────────────────────────────────────────────────────────────

def _format_md(result: dict) -> str:
    date_str = result["audit_date"]
    gen_at   = result["generated_at"]
    lines = [
        f"# Daily Pipeline Audit — {date_str}",
        f"Generated: {gen_at} UTC",
        "",
    ]

    # Flags
    if result["flags"]:
        lines.append("## Flags")
        for f in result["flags"]:
            lines.append(f"- {f}")
        lines.append("")

    # Coverage
    cov = result["coverage"]
    lines += [
        "## Coverage (Asia Session)",
        f"Expected: {cov['expected_asia_count']} | "
        f"Found (Asia): {cov['found_asia_count']} | "
        f"Found (total incl US): {cov['found_total_count']} | "
        f"Missing: {len(cov['missing'])}",
        "",
    ]
    for window, keys in sorted(cov["by_window"].items()):
        status = "[ok]" if keys else "[none]"
        label  = window.replace("_", " ")
        items  = ", ".join(k.split("/")[-1] for k in keys) if keys else "—"
        lines.append(f"- {label}: {status} {items}")
    lines.append("")

    if cov["missing"]:
        lines += ["### Missing"]
        for k in cov["missing"]:
            lines.append(f"- {k}")
        lines.append("")

    # Macro
    macro = result["macro"]
    lines += [
        "## Macro",
        f"Files found: {macro['count']}",
    ]
    for f in macro["files"]:
        lines.append(f"- {f}")
    lines.append("")

    # Portfolio
    port = result["portfolio"]
    lines += [
        "## Portfolio Briefing",
        port["file"] if port["found"] else "Not found",
        "",
    ]

    # Entry table
    if result["entries"]:
        lines += [
            "## News Entries",
            f"Total: {result['ticker_news']['entries']}",
            "",
            "| Coverage | UTC |",
            "|----------|-----|",
        ]
        for e in result["entries"]:
            lines.append(f"| {e['heading']} | {e['utc_time']} |")
        lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily pipeline audit — writes reviews/daily/YYYY-MM-DD.{md,json}"
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Date to audit (default: today HKT)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results to stdout without writing files",
    )
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    else:
        # Default: today in HKT (UTC+8)
        hkt_now  = datetime.now(timezone.utc) + timedelta(hours=8)
        date_str = hkt_now.strftime("%Y-%m-%d")

    log.info("Running daily audit for %s", date_str)

    result   = _audit(date_str)
    md_text  = _format_md(result)
    json_text = json.dumps(result, indent=2, default=str)

    if args.dry_run:
        print(md_text)
        print("\n--- JSON ---\n")
        print(json_text)
        log.info("Dry-run complete — no files written")
        return

    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    (REVIEWS_DIR / f"{date_str}.md").write_text(md_text,   encoding="utf-8")
    (REVIEWS_DIR / f"{date_str}.json").write_text(json_text, encoding="utf-8")

    flag_summary = f" [{', '.join(result['flags'])}]" if result["flags"] else ""
    log.info(
        "Audit complete for %s — %d entries, %d missing%s",
        date_str,
        result["ticker_news"]["entries"],
        len(result["coverage"]["missing"]),
        flag_summary,
    )


if __name__ == "__main__":
    main()
