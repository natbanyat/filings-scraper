"""
Earnings calendar manager — maintains coverage/earnings_calendar.json.

Data sources (by tier, per ticker):
  US-listed (JPM, GOOG, GRAB, SE, FUTU, MMYT): Alpha Vantage EARNINGS_CALENDAR
  1299 (HKEx): yfinance 1299.HK → fallback to Brave Search + Claude
  8316 (TSE):  Brave Search + Claude → fallback to yfinance 8316.T
  TMX (TSX), STAN (LSE): Brave Search + Claude (not on Alpha Vantage)

Commands:
  python scripts/earnings_calendar.py --refresh
  python scripts/earnings_calendar.py --show
  python scripts/earnings_calendar.py --show --days 60
  python scripts/earnings_calendar.py --set tickers/8316 2026-05-15 "Q4 FY2026" after-close
  python scripts/earnings_calendar.py --mark-processed tickers/JPM "Q1 FY2026"
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
log = setup_logging("earnings_calendar")

import requests

from config import TICKER_META
from openclaw_gateway_model import CHEAP_MODEL, run_json
from utils import retry

CALENDAR_PATH = Path(__file__).resolve().parent.parent / "coverage" / "earnings_calendar.json"
AV_BASE = "https://www.alphavantage.co/query"

# ── Calendar I/O ──────────────────────────────────────────────────────────────

def load_calendar() -> dict:
    return json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))


def save_calendar(cal: dict) -> None:
    CALENDAR_PATH.write_text(json.dumps(cal, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Alpha Vantage ─────────────────────────────────────────────────────────────

@retry(max_attempts=2, backoff=3.0, exceptions=(requests.RequestException,))
def _fetch_av_earnings(symbol: str) -> list[dict]:
    """
    Fetch upcoming earnings dates from Alpha Vantage EARNINGS_CALENDAR.
    Returns list of {period, expected_date, expected_time} for the next 12 months.
    """
    api_key = os.environ.get("ALPHA_VANTAGE_KEY")
    if not api_key:
        log.warning("ALPHA_VANTAGE_KEY not set — skipping Alpha Vantage for %s", symbol)
        return []

    resp = requests.get(
        AV_BASE,
        params={"function": "EARNINGS_CALENDAR", "symbol": symbol, "horizon": "12month", "apikey": api_key},
        timeout=15,
    )
    resp.raise_for_status()

    # AV returns CSV for this endpoint
    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        return []

    # Header: symbol,name,reportDate,fiscalDateEnding,estimate,currency
    entries = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 3 or parts[0].upper() != symbol.upper():
            continue
        report_date = parts[2].strip()
        fiscal_end  = parts[3].strip() if len(parts) > 3 else ""

        if not report_date or report_date == "0000-00-00":
            continue

        # Derive period label from fiscal quarter end date
        try:
            fe = datetime.strptime(fiscal_end, "%Y-%m-%d")
            fy = fe.year if fe.month >= 4 else fe.year - 1
            q_map = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}
            q = q_map.get(fe.month, f"Q{(fe.month // 3)}")
            period = f"{q} FY{fy}"
        except ValueError:
            period = f"period ending {fiscal_end}"

        entries.append({
            "period":        period,
            "expected_date": report_date,
            "expected_time": "unknown",
            "source":        "alpha_vantage",
            "confirmed":     False,
            "processed":     False,
            "processed_date": None,
        })

    log.debug("Alpha Vantage: %d upcoming entries for %s", len(entries), symbol)
    return entries


# ── yfinance ──────────────────────────────────────────────────────────────────

def _fetch_yf_earnings(yf_symbol: str) -> list[dict]:
    """Fetch upcoming earnings dates via yfinance."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(yf_symbol)
        df = ticker.get_earnings_dates(limit=8)
        if df is None or df.empty:
            return []

        today = datetime.now(timezone.utc).date()
        entries = []
        for idx, row in df.iterrows():
            try:
                dt = idx.date() if hasattr(idx, "date") else idx
                if dt < today:
                    continue  # skip historical
                entries.append({
                    "period":        f"period ending ~{dt.strftime('%Y-%m')}",
                    "expected_date": dt.isoformat(),
                    "expected_time": "unknown",
                    "source":        "yfinance",
                    "confirmed":     False,
                    "processed":     False,
                    "processed_date": None,
                })
            except Exception:
                continue

        log.debug("yfinance: %d upcoming entries for %s", len(entries), yf_symbol)
        return entries
    except Exception as e:
        log.warning("yfinance fetch failed for %s: %s", yf_symbol, e)
        return []


# ── Brave Search + Claude ─────────────────────────────────────────────────────

def _brave_search(query: str, count: int = 5) -> list[dict]:
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/news/search",
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            params={"q": query, "count": count, "freshness": "pm"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        log.warning("Brave search failed for query '%s': %s", query[:60], e)
        return []


def _extract_dates_with_claude(name: str, search_results: list[dict], today: str) -> list[dict]:
    """Ask the cheap OpenAI gateway model to extract earnings dates from search snippets."""
    if not search_results:
        return []

    snippets = "\n".join(
        f"- {r.get('title', '')} | {r.get('description', '')[:200]}"
        for r in search_results[:8]
    )

    prompt = f"""Today is {today}. Extract upcoming earnings/results announcement dates for {name} from these news snippets.

SNIPPETS:
{snippets}

Return ONLY valid JSON array ([] if nothing found):
[
  {{"period": "Q3 FY2026", "expected_date": "2026-02-15", "expected_time": "unknown", "confidence": "high|medium|low"}}
]

Rules:
- Only include future dates (after {today})
- expected_date must be ISO format YYYY-MM-DD or null if only month/quarter known
- If only a month is known, use the 1st of that month as the date and set confidence to "low"
- period: use format "Q1 FY2026", "H1 FY2026", "Full Year FY2025", etc."""

    try:
        hits = run_json(prompt, expected="array", model=CHEAP_MODEL, timeout=180)
        return [
            {
                "period":         h.get("period", "unknown"),
                "expected_date":  h.get("expected_date"),
                "expected_time":  h.get("expected_time", "unknown"),
                "source":         "brave_search",
                "confirmed":      False,
                "processed":      False,
                "processed_date": None,
            }
            for h in hits
            if h.get("expected_date")
        ]
    except Exception as e:
        log.warning("Gateway date extraction failed for %s: %s", name, e)
        return []


# ── Per-ticker refresh logic ──────────────────────────────────────────────────

def _refresh_ticker(coverage_key: str, ticker_data: dict) -> list[dict]:
    """
    Refresh earnings entries for one ticker. Returns new entries list.
    Preserves any existing entries that are marked processed=True.
    """
    meta = TICKER_META.get(coverage_key, {})
    av_symbol = meta.get("av_symbol")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = coverage_key.split("/")[-1].upper()

    # Keep already-processed entries as historical record
    existing = ticker_data.get("entries", [])
    processed = [e for e in existing if e.get("processed")]

    new_entries: list[dict] = []

    # --- Alpha Vantage (US-listed tickers) ---
    if av_symbol:
        new_entries = _fetch_av_earnings(av_symbol)

    # --- yfinance (HK tickers, fallback for others) ---
    if not new_entries:
        if coverage_key == "tickers/1299":
            new_entries = _fetch_yf_earnings("1299.HK")
        elif coverage_key == "tickers/8316":
            new_entries = _fetch_yf_earnings("8316.T")

    # --- Brave Search + Claude (TMX, STAN, Asian, and any gaps) ---
    if not new_entries:
        current_year = datetime.now(timezone.utc).year
        year_terms = f"{current_year} {current_year + 1}"
        queries = {
            "tickers/TMX":  f"TMX Group Toronto Stock Exchange quarterly earnings results date {year_terms}",
            "tickers/STAN": f"Standard Chartered STAN quarterly earnings results date {year_terms}",
            "tickers/8316": f"Sumitomo Mitsui SMFG 8316 決算発表 earnings results date {year_terms}",
            "tickers/1299": f"AIA Group 1299 HKEx earnings results interim full year date {year_terms}",
        }
        query = queries.get(coverage_key, f"{name} quarterly earnings results date {year_terms}")
        results = _brave_search(query)
        new_entries = _extract_dates_with_claude(name, results, today)

    # Merge: new entries take priority; add back processed entries not already in new list
    periods_in_new = {e["period"] for e in new_entries}
    merged = new_entries + [p for p in processed if p["period"] not in periods_in_new]

    if new_entries:
        log.info("%s: refreshed %d upcoming earning(s)", name, len(new_entries))
    else:
        log.warning("%s: no upcoming earnings dates found", name)

    return merged


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_refresh(args) -> None:
    cal = load_calendar()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    targets = [args.coverage_key] if args.coverage_key else [
        k for k in cal if not k.startswith("_")
    ]

    for key in targets:
        if key not in cal:
            log.error("Unknown coverage key: %s", key)
            continue
        try:
            new_entries = _refresh_ticker(key, cal[key])
            cal[key]["entries"] = new_entries
            cal[key]["last_refreshed"] = today
        except Exception as e:
            log.error("%s: refresh failed — %s", key, e, exc_info=True)

    cal["_meta"]["last_refreshed"] = today
    save_calendar(cal)
    log.info("Earnings calendar saved to %s", CALENDAR_PATH)


def cmd_show(args) -> None:
    cal = load_calendar()
    today = datetime.now(timezone.utc).date()
    horizon = timedelta(days=args.days)

    rows = []
    for key, data in cal.items():
        if key.startswith("_"):
            continue
        for entry in data.get("entries", []):
            if not entry.get("expected_date"):
                continue
            try:
                dt = datetime.strptime(entry["expected_date"], "%Y-%m-%d").date()
                if today <= dt <= today + horizon:
                    rows.append((dt, key.split("/")[-1].upper(), entry["period"],
                                 entry.get("expected_time", "?"),
                                 "DONE" if entry.get("processed") else "pending",
                                 entry.get("source", "?")))
            except ValueError:
                continue

    if not rows:
        print(f"No earnings in the next {args.days} days.")
        return

    rows.sort()
    print(f"\nUpcoming earnings (next {args.days} days):\n")
    print(f"{'Date':<12} {'Ticker':<8} {'Period':<18} {'Time':<14} {'Status':<10} {'Source'}")
    print("-" * 75)
    for dt, ticker, period, time_, status, source in rows:
        print(f"{dt.isoformat():<12} {ticker:<8} {period:<18} {time_:<14} {status:<10} {source}")
    print()


def cmd_set(args) -> None:
    cal = load_calendar()
    key = args.coverage_key
    if key not in cal:
        log.error("Unknown coverage key: %s", key)
        sys.exit(1)

    # Remove any existing entry for this period
    cal[key]["entries"] = [
        e for e in cal[key].get("entries", [])
        if e["period"] != args.period
    ]
    cal[key]["entries"].append({
        "period":         args.period,
        "expected_date":  args.date,
        "expected_time":  args.time or "unknown",
        "source":         "manual",
        "confirmed":      False,
        "processed":      False,
        "processed_date": None,
    })
    save_calendar(cal)
    print(f"Set {key} / {args.period} → {args.date} ({args.time or 'unknown time'})")


def cmd_mark_processed(args) -> None:
    cal = load_calendar()
    key = args.coverage_key
    if key not in cal:
        log.error("Unknown coverage key: %s", key)
        sys.exit(1)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    found = False
    for entry in cal[key].get("entries", []):
        if entry["period"] == args.period:
            entry["processed"] = True
            entry["processed_date"] = today
            found = True
            break

    if not found:
        log.warning("Period '%s' not found for %s — adding as processed entry", args.period, key)
        cal[key].setdefault("entries", []).append({
            "period": args.period, "expected_date": None,
            "expected_time": "unknown", "source": "manual",
            "confirmed": True, "processed": True, "processed_date": today,
        })

    save_calendar(cal)
    print(f"Marked {key} / {args.period} as processed.")


# ── Helper (used by catalyst_monitor + earnings_processor) ───────────────────

def get_upcoming(days: int = 14) -> list[dict]:
    """
    Return list of upcoming earnings entries within `days` days.
    Each item: {coverage_key, period, expected_date, expected_time, processed}
    """
    try:
        cal = load_calendar()
    except Exception:
        return []

    today = datetime.now(timezone.utc).date()
    horizon = timedelta(days=days)
    results = []

    for key, data in cal.items():
        if key.startswith("_"):
            continue
        for entry in data.get("entries", []):
            if entry.get("processed"):
                continue
            if not entry.get("expected_date"):
                continue
            try:
                dt = datetime.strptime(entry["expected_date"], "%Y-%m-%d").date()
                if today <= dt <= today + horizon:
                    results.append({
                        "coverage_key":  key,
                        "period":        entry["period"],
                        "expected_date": entry["expected_date"],
                        "expected_time": entry.get("expected_time", "unknown"),
                        "processed":     entry.get("processed", False),
                    })
            except ValueError:
                continue

    results.sort(key=lambda x: x["expected_date"])
    return results


def get_today_earnings() -> list[dict]:
    """Return earnings entries scheduled for today (not yet processed)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [e for e in get_upcoming(days=0) if e["expected_date"] == today]


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Earnings calendar manager")
    sub = parser.add_subparsers(dest="command")

    p_refresh = sub.add_parser("refresh", help="Refresh calendar from data sources")
    p_refresh.add_argument("--coverage-key", help="Refresh a single ticker only")

    p_show = sub.add_parser("show", help="Show upcoming earnings")
    p_show.add_argument("--days", type=int, default=30, help="Horizon in days (default: 30)")

    p_set = sub.add_parser("set", help="Manually set an earnings date")
    p_set.add_argument("coverage_key")
    p_set.add_argument("date", help="Expected date YYYY-MM-DD")
    p_set.add_argument("period", help='Period label e.g. "Q1 FY2026"')
    p_set.add_argument("time", nargs="?", default="unknown",
                       help="pre-market | after-close | unknown")

    p_mp = sub.add_parser("mark-processed", help="Mark a quarter as processed")
    p_mp.add_argument("coverage_key")
    p_mp.add_argument("period")

    # Also support --refresh / --show / --set / --mark-processed as flags for README compat
    parser.add_argument("--refresh",        action="store_true")
    parser.add_argument("--show",           action="store_true")
    parser.add_argument("--days",           type=int, default=30)
    parser.add_argument("--set",            nargs="+", metavar="ARG")
    parser.add_argument("--mark-processed", nargs=2, metavar=("KEY", "PERIOD"))
    parser.add_argument("--coverage-key")

    args = parser.parse_args()

    # Subcommand routing
    if args.command == "refresh" or args.refresh:
        cmd_refresh(args)
    elif args.command == "show" or args.show:
        cmd_show(args)
    elif args.command == "set":
        cmd_set(args)
    elif args.command == "mark-processed":
        cmd_mark_processed(args)
    elif args.set:
        # --set tickers/8316 2026-05-15 "Q4 FY2026" pre-market
        parts = args.set
        args.coverage_key = parts[0]
        args.date         = parts[1] if len(parts) > 1 else None
        args.period       = parts[2] if len(parts) > 2 else "unknown"
        args.time         = parts[3] if len(parts) > 3 else "unknown"
        cmd_set(args)
    elif args.mark_processed:
        args.coverage_key = args.mark_processed[0]
        args.period       = args.mark_processed[1]
        cmd_mark_processed(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
