"""
Discord remote-control bot.

Polls #bot-commands every 5 seconds (REST GET /messages — no WebSocket, no
discord.py dependency).  Only processes messages from DISCORD_BOT_OWNER_ID.

Start:
  .venv/bin/python scripts/discord_bot.py

In a persistent tmux session:
  tmux new -s bot
  .venv/bin/python scripts/discord_bot.py
  # Ctrl+B, D to detach

Commands (send in #bot-commands):
  !help                          — list all commands
  !status                        — last 30 lines of logs/cron.log
  !calendar                      — upcoming earnings
  !refresh-calendar              — refresh earnings dates from APIs
  !run daily <window>            — daily_news.py (us/asia_japan/asia_korea/asia_hk)
  !run earnings <TICKER>         — earnings_processor.py for one ticker
  !run macro                     — macro_close.py
  !run catalyst                  — catalyst_monitor.py
  !pending                       — earnings_processor.py --pending
  !coverage-pending              — coverage_updater.py --pending
  !dry <command>                 — any above command with --dry-run
  !add-ticker <T> <EX> <close>   — full ticker onboarding via add_ticker.py
  !populate-coverage <TICKER>    — (re)generate coverage files via Claude
  !claude <instruction>          — claude -p in workspace directory
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import WORKSPACE_ROOT
from openclaw_gateway_model import CHEAP_MODEL, DEEP_MODEL, run_text
from post_discord import send_text
from utils import setup_logging

load_dotenv(WORKSPACE_ROOT / ".env")

DISCORD_API      = "https://discord.com/api/v10"
PYTHON           = str(WORKSPACE_ROOT / ".venv/bin/python")
SCRIPTS          = WORKSPACE_ROOT / "scripts"
POLL_INTERVAL    = 5    # seconds between polls
CHANNEL_POLL_MOD = 6    # check coverage channels every N bot-commands cycles (~30s)
CMD_TIMEOUT      = 120  # seconds for pipeline script commands
CLAUDE_TIMEOUT   = 300  # seconds for !claude
POPULATE_TIMEOUT = 600  # seconds for !populate-coverage (research + write)

log = setup_logging("discord_bot")


# ── Discord helpers ───────────────────────────────────────────────────────────

def _headers() -> dict:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise EnvironmentError("DISCORD_BOT_TOKEN not set in .env")
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


def _get_channel_id() -> str:
    channel_map_path = SCRIPTS / "channel_map.json"
    if not channel_map_path.exists():
        raise FileNotFoundError(
            "channel_map.json not found — run: .venv/bin/python scripts/discord_setup.py"
        )
    with open(channel_map_path, encoding="utf-8") as f:
        channel_map = json.load(f)
    key = "special/bot-commands"
    if key not in channel_map:
        raise KeyError(
            f"{key!r} not in channel_map.json — run discord_setup.py to create #bot-commands"
        )
    return channel_map[key]


def _get_messages(channel_id: str, after: str | None) -> list[dict]:
    """Fetch up to 10 new messages after *after*. Returns oldest-first."""
    params: dict = {"limit": 10}
    if after:
        params["after"] = after
    resp = requests.get(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=_headers(),
        params=params,
        timeout=10,
    )
    if resp.status_code == 429:
        retry_after = float(resp.json().get("retry_after", 1.0))
        log.warning("Rate limited — sleeping %.1fs", retry_after)
        time.sleep(retry_after + 0.1)
        return []
    resp.raise_for_status()
    return sorted(resp.json(), key=lambda m: m["id"])


# ── Script runner ─────────────────────────────────────────────────────────────

def _run_script(
    script: str,
    args: list[str] | None = None,
    dry_run: bool = False,
    timeout: int = CMD_TIMEOUT,
) -> str:
    """
    Run a pipeline script and return combined stdout+stderr (last 3000 chars).
    *script* is a filename relative to scripts/, e.g. "daily_news.py".
    """
    cmd = [PYTHON, str(SCRIPTS / script)] + (args or [])
    if dry_run:
        cmd.append("--dry-run")
    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(WORKSPACE_ROOT),
    )
    output = result.stdout + result.stderr
    return output[-3000:] if len(output) > 3000 else output


def _find_claude() -> str:
    candidates = [
        "/home/natbanyat/.npm-global/bin/claude",
        shutil.which("claude") or "",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise FileNotFoundError(
        "claude CLI not found — install with: npm install -g @anthropic-ai/claude-code"
    )


# ── Command handlers ──────────────────────────────────────────────────────────

def _cmd_help() -> str:
    return (
        "**Bot Commands**\n"
        "`!help` — this message\n"
        "`!status` — last 30 lines of logs/cron.log\n"
        "`!calendar` — upcoming earnings\n"
        "`!refresh-calendar` — refresh earnings dates from APIs\n"
        "`!run daily <window>` — daily_news.py (us / asia_japan / asia_korea / asia_hk)\n"
        "`!run earnings <TICKER>` — earnings_processor.py for a single ticker\n"
        "`!run macro` — macro_close.py\n"
        "`!run catalyst` — catalyst_monitor.py\n"
        "`!pending` — earnings_processor.py --pending\n"
        "`!coverage-pending` — coverage_updater.py --pending\n"
        "`!dry <command>` — any above command with --dry-run appended\n"
        "`!add-ticker <TICKER> <exchange> <close>` — full ticker onboarding\n"
        "`!populate-coverage <TICKER>` — (re)generate coverage files via Claude\n"
        "`!claude <instruction>` — run claude -p in the workspace directory\n"
        "**Query commands:**\n"
        "`!latest <ticker>` — summarize recent events for a ticker\n"
        "`!watchpoints [ticker]` — show open watchpoints\n"
        "`!week [ticker]` — this week's summary\n"
        "`!macro` — current macro snapshot"
    )


def _cmd_status() -> str:
    log_path = WORKSPACE_ROOT / "logs" / "cron.log"
    if not log_path.exists():
        return "logs/cron.log not found — no cron jobs have run yet."
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = "\n".join(lines[-30:])
    return f"**cron.log (last 30 lines):**\n```\n{tail}\n```"


def _cmd_calendar() -> str:
    return _run_script("earnings_calendar.py", ["--show"])


def _cmd_refresh_calendar() -> str:
    return _run_script("earnings_calendar.py", ["--refresh"])


def _cmd_run(args: list[str], dry_run: bool = False) -> str:
    if not args:
        return "Usage: `!run daily <window>` | `!run earnings <TICKER>` | `!run macro` | `!run catalyst`"
    sub = args[0].lower()
    if sub == "daily":
        if len(args) < 2:
            return "Usage: `!run daily <window>`  (us / asia_japan / asia_korea / asia_hk)"
        return _run_script("daily_news.py", [args[1]], dry_run=dry_run)
    elif sub == "earnings":
        if len(args) < 2:
            return "Usage: `!run earnings <TICKER>`"
        ticker = args[1].upper()
        return _run_script(
            "earnings_processor.py",
            ["--coverage-key", f"tickers/{ticker}"],
            dry_run=dry_run,
        )
    elif sub == "macro":
        return _run_script("macro_close.py", dry_run=dry_run)
    elif sub == "catalyst":
        return _run_script("catalyst_monitor.py", dry_run=dry_run)
    else:
        return f"Unknown subcommand: `{sub}`. Use daily / earnings / macro / catalyst."


def _cmd_pending(dry_run: bool = False) -> str:
    return _run_script("earnings_processor.py", ["--pending"], dry_run=dry_run)


def _cmd_coverage_pending(dry_run: bool = False) -> str:
    return _run_script("coverage_updater.py", ["--pending"], dry_run=dry_run)


def _cmd_dry(args: list[str]) -> str:
    if not args:
        return "Usage: `!dry <command>`  e.g. `!dry run daily us`"
    sub = args[0].lower()
    rest = args[1:]
    if sub == "run":
        return _cmd_run(rest, dry_run=True)
    elif sub == "pending":
        return _cmd_pending(dry_run=True)
    elif sub == "coverage-pending":
        return _cmd_coverage_pending(dry_run=True)
    elif sub == "calendar":
        return _cmd_calendar()
    elif sub == "refresh-calendar":
        return _cmd_refresh_calendar()
    else:
        return f"`!dry` does not support: `{sub}`"


def _cmd_add_ticker(args: list[str]) -> str:
    if len(args) < 3:
        return "Usage: `!add-ticker <TICKER> <exchange> <close>`  e.g. `!add-ticker NVDA NASDAQ us`"
    ticker, exchange, close = args[0].upper(), args[1], args[2]
    return _run_script(
        "add_ticker.py",
        ["--ticker", ticker, "--exchange", exchange, "--close", close],
        timeout=120,
    )


def _cmd_populate_coverage(args: list[str]) -> str:
    if not args:
        return "Usage: `!populate-coverage <TICKER>`"
    ticker = args[0].upper()
    return _run_script(
        "add_ticker.py",
        ["--ticker", ticker, "--populate-only"],
        timeout=POPULATE_TIMEOUT,
    )


def _cmd_claude(args: list[str]) -> str:
    if not args:
        return "Usage: `!claude <instruction>`"
    instruction = " ".join(args)
    claude_bin = _find_claude()
    result = subprocess.run(
        [claude_bin, "-p", instruction, "--output-format", "text"],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        cwd=str(WORKSPACE_ROOT),
    )
    output = (result.stdout + result.stderr).strip()
    if len(output) > 3800:
        output = output[:3800] + "\n...(truncated)"
    return output or "(no output)"


# ── Query commands (!latest, !watchpoints, !week, !macro) ────────────────────

TICKER_NEWS_DIR = WORKSPACE_ROOT / "events" / "ticker_news"
MACRO_DIR = WORKSPACE_ROOT / "events" / "macro"
WEEKLY_DIR = WORKSPACE_ROOT / "events" / "weekly"


def _resolve_ticker(arg: str) -> str | None:
    """Resolve a user argument like 'JPM' or 'grab' to a coverage_key."""
    from config import COVERAGE
    arg_upper = arg.upper()
    for key in COVERAGE:
        if key.split("/")[-1].upper() == arg_upper:
            return key
    return None


def _read_ticker_section(ticker_name: str, days_back: int = 1) -> str:
    """Read event log sections for a ticker from the last N days."""
    from datetime import timedelta
    sections = []
    for d in range(days_back + 1):
        date_str = (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%d")
        log_file = TICKER_NEWS_DIR / f"{date_str}.md"
        if not log_file.exists():
            continue
        text = log_file.read_text(encoding="utf-8")
        in_section = False
        for line in text.split("\n"):
            if line.startswith(f"## {ticker_name}"):
                in_section = True
                sections.append(line)
            elif in_section and line.startswith("## "):
                in_section = False
            elif in_section:
                sections.append(line)
    return "\n".join(sections[-100:]) if sections else ""


def _cmd_latest(args: list[str]) -> str:
    if not args:
        return "Usage: `!latest <ticker>` -- e.g. `!latest JPM`"
    coverage_key = _resolve_ticker(args[0])
    if not coverage_key:
        return f"Unknown ticker: {args[0]}. Check COVERAGE in config.py."
    name = coverage_key.split("/")[-1].upper()
    section = _read_ticker_section(name, days_back=1)
    if not section:
        return f"No recent events for {name} in the last 2 days."
    try:
        summary = run_text(
            f"Summarize the key developments for {name} in 3 sentences. Focus on what changed and what to watch.\n\n{section[:3000]}",
            model=CHEAP_MODEL,
            timeout=120,
        )
        return f"**Latest -- {name}**\n" + summary.strip()
    except Exception as exc:
        return f"**Latest -- {name}** (raw)\n{section[:1500]}"


def _cmd_watchpoints(args: list[str]) -> str:
    try:
        from cache import get_open_watchpoints
    except ImportError:
        return "Watchpoint tracking not available."
    coverage_key = _resolve_ticker(args[0]) if args else None
    watchpoints = get_open_watchpoints(coverage_key)
    if not watchpoints:
        label = args[0].upper() if args else "all tickers"
        return f"No open watchpoints for {label}."
    lines = [f"**Open Watchpoints** ({len(watchpoints)})\n"]
    for wp in watchpoints[:15]:
        name = wp["coverage_key"].split("/")[-1].upper()
        lines.append(f"[{wp['source_date']}] **{name}**: {wp['watchpoint']}")
    return "\n".join(lines)


def _cmd_week(args: list[str]) -> str:
    if args:
        coverage_key = _resolve_ticker(args[0])
        if not coverage_key:
            return f"Unknown ticker: {args[0]}."
        name = coverage_key.split("/")[-1].upper()
        section = _read_ticker_section(name, days_back=7)
        if not section:
            return f"No events for {name} in the last 7 days."
        try:
            summary = run_text(
                f"Write a 5-sentence weekly summary for {name}. What were the key themes, what moved, what to watch next week.\n\n{section[:4000]}",
                model=CHEAP_MODEL,
                timeout=120,
            )
            return f"**Week -- {name}**\n" + summary.strip()
        except Exception as exc:
            return f"Error: {exc}"
    else:
        if WEEKLY_DIR.exists():
            files = sorted(WEEKLY_DIR.glob("*.md"), reverse=True)
            if files:
                text = files[0].read_text(encoding="utf-8")
                return f"**Weekly Digest** ({files[0].stem})\n{text[:1800]}"
        return "No weekly digest found."


def _cmd_macro_query() -> str:
    if MACRO_DIR.exists():
        files = sorted(MACRO_DIR.glob("*-close.md"), reverse=True)
        if files:
            text = files[0].read_text(encoding="utf-8")
            return f"**Macro Close** ({files[0].stem})\n{text[:1800]}"
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from fetch_news import fetch_macro_context
        ctx = fetch_macro_context()
        return ctx or "No macro data available."
    except Exception as exc:
        return f"Error fetching macro: {exc}"


# ── Follow-up Q&A in coverage channels ────────────────────────────────────

PORTFOLIO_CONTEXT_PATH = WORKSPACE_ROOT / "PORTFOLIO_CONTEXT.md"

def _load_followup_context(coverage_key: str) -> str:
    """Load relevant context for a follow-up question about a coverage item."""
    name = coverage_key.split("/")[-1].upper()
    parts = []

    # 1. Portfolio context section for this name
    if PORTFOLIO_CONTEXT_PATH.exists():
        pc = PORTFOLIO_CONTEXT_PATH.read_text(encoding="utf-8")
        for line in pc.split("\n"):
            if "|" in line and (name in line.upper() or coverage_key.split("/")[-1] in line):
                parts.append(f"PORTFOLIO THESIS: {line.strip()}")
                break

    # 2. Recent event logs (last 3 days)
    section = _read_ticker_section(name, days_back=3)
    if section:
        parts.append(f"RECENT EVENTS (last 3 days):\n{section[:3000]}")

    # 3. Open watchpoints
    try:
        from cache import get_open_watchpoints
        wps = get_open_watchpoints(coverage_key)
        if wps:
            wp_lines = "\n".join(f"- {wp['watchpoint']}" for wp in wps[:5])
            parts.append(f"OPEN WATCHPOINTS:\n{wp_lines}")
    except Exception:
        pass

    return "\n\n".join(parts) if parts else f"No recent context available for {name}."


def _handle_followup(channel_id: str, coverage_key: str, question: str) -> None:
    """Answer a follow-up question using coverage context + Sonnet."""
    name = coverage_key.split("/")[-1].upper()
    context = _load_followup_context(coverage_key)

    prompt = (
        f"You are a buy-side analyst covering {name}. A portfolio manager asks a follow-up question.\n\n"
        f"CONTEXT:\n{context}\n\n"
        f"QUESTION: {question}\n\n"
        f"Answer concisely (max 200 words). Lead with the conclusion, then supporting facts. "
        f"Reference specific data points from the context when available. "
        f"If the context doesn't cover the question, say so honestly."
    )

    try:
        response = run_text(prompt, model=DEEP_MODEL, timeout=180).strip()
    except Exception as exc:
        log.error("Follow-up gateway call failed for %s: %s", name, exc)
        response = f"Error generating response: {exc}"

    send_text(channel_id, response)
    log.info("Follow-up answered in #%s (%s): %.60s", coverage_key, name, question)


def _build_channel_coverage_map() -> dict[str, str]:
    """Build reverse map: channel_id → coverage_key from channel_map.json."""
    channel_map_path = SCRIPTS / "channel_map.json"
    if not channel_map_path.exists():
        return {}
    with open(channel_map_path, encoding="utf-8") as f:
        cmap = json.load(f)
    # Invert: channel_id → coverage_key (only coverage + special channels, not bot-commands)
    reverse = {}
    for key, cid in cmap.items():
        if key.startswith("special/"):
            continue  # don't monitor special channels for follow-ups
        reverse[cid] = key
    return reverse


# ── Dispatcher ────────────────────────────────────────────────────────────────

def dispatch(content: str, channel_id: str) -> None:
    """Parse *content* as a bot command and post the response."""
    parts = content.strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    args = parts[1:]

    try:
        if cmd == "!help":
            response = _cmd_help()
        elif cmd == "!status":
            response = _cmd_status()
        elif cmd == "!calendar":
            response = _cmd_calendar()
        elif cmd == "!refresh-calendar":
            response = _cmd_refresh_calendar()
        elif cmd == "!run":
            response = _cmd_run(args)
        elif cmd == "!pending":
            response = _cmd_pending()
        elif cmd == "!coverage-pending":
            response = _cmd_coverage_pending()
        elif cmd == "!dry":
            response = _cmd_dry(args)
        elif cmd == "!add-ticker":
            response = _cmd_add_ticker(args)
        elif cmd == "!populate-coverage":
            response = _cmd_populate_coverage(args)
        elif cmd == "!claude":
            response = _cmd_claude(args)
        elif cmd == "!latest":
            response = _cmd_latest(args)
        elif cmd == "!watchpoints":
            response = _cmd_watchpoints(args)
        elif cmd == "!week":
            response = _cmd_week(args)
        elif cmd == "!macro":
            response = _cmd_macro_query()
        else:
            response = f"Unknown command: `{cmd}`. Type `!help` for the command list."
    except subprocess.TimeoutExpired:
        response = f"Command timed out."
    except Exception as exc:  # noqa: BLE001
        log.exception("Command %r failed", cmd)
        response = f"Error: {exc}"

    send_text(channel_id, response)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    owner_id = os.environ.get("DISCORD_BOT_OWNER_ID", "").strip()
    if not owner_id:
        log.warning(
            "DISCORD_BOT_OWNER_ID not set — bot will process messages from any user. "
            "Add DISCORD_BOT_OWNER_ID=<your-id> to .env to restrict access."
        )

    channel_id = _get_channel_id()
    log.info("Bot started, polling #bot-commands (channel %s)", channel_id)
    send_text(channel_id, "Bot online. Type `!help` for available commands.")

    # Seed last_message_id so we don't replay history on restart
    last_message_id: str | None = None
    try:
        msgs = _get_messages(channel_id, after=None)
        if msgs:
            last_message_id = msgs[-1]["id"]
    except Exception as exc:
        log.warning("Could not seed last_message_id: %s", exc)

    # Build coverage channel map for follow-up Q&A
    coverage_channel_map = _build_channel_coverage_map()
    # Seed last seen message IDs for all coverage channels
    channel_last_ids: dict[str, str | None] = {}
    for cid in coverage_channel_map:
        try:
            msgs = _get_messages(cid, after=None)
            channel_last_ids[cid] = msgs[-1]["id"] if msgs else None
        except Exception:
            channel_last_ids[cid] = None

    log.info("Monitoring %d coverage channels for follow-up questions", len(coverage_channel_map))

    cycle = 0
    while True:
        # ── Always: poll #bot-commands ────────────────────────────────
        try:
            msgs = _get_messages(channel_id, after=last_message_id)
            for msg in msgs:
                last_message_id = msg["id"]
                author_id = msg.get("author", {}).get("id", "")
                if owner_id and author_id != owner_id:
                    continue
                content = msg.get("content", "").strip()
                if content.startswith("!"):
                    log.info("Command from %s: %s", author_id, content)
                    dispatch(content, channel_id)
        except Exception as exc:
            log.error("Bot-commands poll error: %s", exc)

        # ── Every Nth cycle: poll coverage channels for follow-ups ───
        if cycle % CHANNEL_POLL_MOD == 0 and coverage_channel_map:
            for cid, cov_key in coverage_channel_map.items():
                try:
                    msgs = _get_messages(cid, after=channel_last_ids.get(cid))
                    for msg in msgs:
                        channel_last_ids[cid] = msg["id"]
                        author_id = msg.get("author", {}).get("id", "")
                        # Skip non-owner messages (also skips bot's own messages)
                        if owner_id and author_id != owner_id:
                            continue
                        content = msg.get("content", "").strip()
                        # Skip empty, commands, and bot-generated embeds
                        if not content or content.startswith("!"):
                            continue
                        log.info("Follow-up in #%s: %.80s", cov_key, content)
                        _handle_followup(cid, cov_key, content)
                except Exception as exc:
                    log.debug("Coverage channel poll error (%s): %s", cov_key, exc)

        cycle += 1
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
