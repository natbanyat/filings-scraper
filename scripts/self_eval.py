"""
Weekly self-evaluation — reviews pipeline outputs and suggests improvements.

Reads the past week of event logs, the current search queries, and the Pass 1/2
prompt templates, then uses Claude Opus to critique output quality across five
dimensions and produce specific, file-referenced improvement suggestions.

Evaluation dimensions:
  1. Materiality precision  — were flagged articles genuinely thesis-relevant?
  2. Analysis depth         — were Pass 2 analyses specific or generic boilerplate?
  3. Source quality         — are high-quality sources dominating, or junk slipping through?
  4. Dedup effectiveness    — are same-story duplicates still reaching the output?
  5. Query coverage         — are search queries surfacing the right stories?

Output:
  - Written to events/eval/YYYY-MM-DD-eval.md
  - Posted to #self-eval Discord channel
  - Printed to stdout

Scheduled: Monday ~06:00 UTC (reviewing the prior week).

Manual:
  python scripts/self_eval.py
  python scripts/self_eval.py --dry-run
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
log = setup_logging("self_eval")

from config import SEARCH_QUERIES
from openclaw_gateway_model import DEEP_MODEL, GatewayModelError, run_text
from post_discord import fetch_messages, send_text
from utils import retry

CHANNEL_MAP_PATH = Path(__file__).parent / "channel_map.json"
WORKSPACE_ROOT   = Path(__file__).resolve().parent.parent
EVENTS_TICKER    = WORKSPACE_ROOT / "events" / "ticker_news"
EVENTS_MACRO     = WORKSPACE_ROOT / "events" / "macro"
EVENTS_EVAL      = WORKSPACE_ROOT / "events" / "eval"
SCRIPTS_DIR      = Path(__file__).resolve().parent

def _load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        raise FileNotFoundError(
            "channel_map.json not found. Run 'python scripts/discord_setup.py' first."
        )
    return json.loads(CHANNEL_MAP_PATH.read_text())


def _collect_logs(events_dir: Path, days: int = 7) -> str:
    """Concatenate event log files from the past N days."""
    if not events_dir.exists():
        return ""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    parts = []
    for f in sorted(events_dir.glob("*.md")):
        try:
            date_str = f.stem[:10]
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if file_date >= cutoff:
                content = f.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"=== {f.stem} ===\n{content}")
        except (ValueError, OSError):
            continue
    return "\n\n".join(parts)


def _collect_human_feedback(channel_id: str | None) -> str:
    """Fetch recent human messages from #self-eval to include as operator feedback."""
    if not channel_id:
        return ""
    try:
        messages = fetch_messages(channel_id, limit=100)
        if not messages:
            return ""
        # Filter to last 7 days
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        recent = []
        for m in messages:
            try:
                ts = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
                if ts >= cutoff and m["content"].strip():
                    recent.append(f"[{ts.strftime('%Y-%m-%d')}] {m['author']}: {m['content']}")
            except (ValueError, KeyError):
                continue
        if not recent:
            return ""
        # Oldest first
        recent.reverse()
        return "\n".join(recent)
    except Exception as e:
        log.warning("Failed to fetch human feedback from Discord: %s", e)
        return ""


def _read_script_excerpt(filename: str, max_chars: int = 2000) -> str:
    path = SCRIPTS_DIR / filename
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")[:max_chars]


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


@retry(max_attempts=3, backoff=5.0, exceptions=(GatewayModelError,))
def _evaluate(ticker_logs: str, macro_logs: str, search_queries: dict, pass1_prompt_excerpt: str, human_feedback: str = "") -> dict:
    """
    Use Claude Opus to evaluate output quality and generate improvement suggestions.
    Returns a structured dict with scores and recommendations.
    """
    week_end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Truncate inputs to fit context
    ticker_logs_trunc = ticker_logs[:8000]  if ticker_logs  else "No ticker event logs this week."
    macro_logs_trunc  = macro_logs[:3000]   if macro_logs   else "No macro close logs this week."
    queries_block     = "\n".join(f"  {k}: {v}" for k, v in search_queries.items())

    prompt = f"""You are evaluating the output quality of an automated financial news pipeline.
Today is {week_end}. Review the past week's outputs and the pipeline configuration,
then provide a rigorous critique with specific, actionable improvement suggestions.

═══ PIPELINE OUTPUTS (past 7 days) ═══

TICKER EVENT LOGS:
{ticker_logs_trunc}

MACRO CLOSE LOGS:
{macro_logs_trunc}

═══ PIPELINE CONFIGURATION ═══

SEARCH QUERIES (config.py):
{queries_block}

PASS 1 PROMPT STRUCTURE (filter_material.py excerpt):
{pass1_prompt_excerpt}

═══ OPERATOR FEEDBACK (from #self-eval Discord channel) ═══

{human_feedback if human_feedback else "No operator feedback this period."}

═══ EVALUATION TASK ═══

Evaluate across these five dimensions. Be harsh — a useful self-evaluation finds real problems.
If operator feedback is provided above, treat it as the highest-priority input. Address each
piece of feedback explicitly in your evaluation and prioritize improvements that respond to it.

1. MATERIALITY PRECISION: Were the flagged articles genuinely thesis-relevant, or were there
   false positives (articles that are general market noise, not specific to the KPI tree)?

2. ANALYSIS DEPTH: Were Pass 2 analyses specific and thesis-linked (referencing actual KPI nodes,
   quantitative thresholds, variant views)? Or generic boilerplate that adds no insight?

3. SOURCE QUALITY: Do the sources look credible? Any low-quality aggregators or copy sites
   that slipped through despite the blocklist?

4. DEDUP EFFECTIVENESS: Were there cases where multiple articles covered the exact same event?
   How many "same story, different outlet" patterns are visible in the logs?

5. QUERY COVERAGE: Based on what was flagged, do the search queries seem well-targeted?
   Are there obvious story types that should appear for a given ticker but didn't?

Return ONLY valid JSON (no prose before or after):
{{
  "week": "{week_end}",
  "scores": {{
    "materiality_precision": {{"score": 1-5, "comment": "specific observation from the logs"}},
    "analysis_depth":        {{"score": 1-5, "comment": "specific observation from the logs"}},
    "source_quality":        {{"score": 1-5, "comment": "specific observation from the logs"}},
    "dedup_effectiveness":   {{"score": 1-5, "comment": "specific observation from the logs"}},
    "query_coverage":        {{"score": 1-5, "comment": "specific observation from the logs"}}
  }},
  "overall_score": 1-5,
  "top_issues": [
    "Issue 1 — specific, concrete, references actual output if possible",
    "Issue 2",
    "Issue 3"
  ],
  "improvements": [
    {{
      "priority": "high|medium|low",
      "file": "scripts/config.py",
      "change": "what to change and why",
      "example": "concrete example of the change (e.g., new search query text, new prompt rule)"
    }},
    ...
  ],
  "positive_observations": [
    "What is working well (be specific)"
  ]
}}

Score guide: 5=excellent, 4=good, 3=acceptable, 2=needs work, 1=broken.
Provide 3-7 improvement items. Prioritize by impact on output quality."""

    raw = run_text(prompt, model=DEEP_MODEL, timeout=300).strip()

    # Extract JSON using balanced-brace matching (handles nested braces in values)
    json_str = _extract_json_object(raw)
    if not json_str:
        log.warning("self_eval: no JSON returned. Raw: %.300s", raw)
        return {"week": week_end, "error": "evaluation failed — no JSON returned"}

    # Strip trailing commas before } or ] (common LLM JSON mistake)
    json_str = re.sub(r",\s*([}\]])", r"\1", json_str)

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        log.warning("self_eval: JSON parse error: %s  Raw excerpt: %.500s", e, json_str)
        return {"week": week_end, "error": f"JSON parse error: {e}"}


def _format_report(ev: dict) -> str:
    """Format the evaluation dict as a human-readable markdown report."""
    week = ev.get("week", "unknown")
    overall = ev.get("overall_score", "?")
    lines = [
        f"# Pipeline Self-Evaluation — {week}",
        f"\n**Overall score: {overall}/5**\n",
    ]

    scores = ev.get("scores", {})
    if scores:
        lines.append("## Dimension Scores\n")
        labels = {
            "materiality_precision": "Materiality precision",
            "analysis_depth":        "Analysis depth",
            "source_quality":        "Source quality",
            "dedup_effectiveness":   "Dedup effectiveness",
            "query_coverage":        "Query coverage",
        }
        for key, label in labels.items():
            d = scores.get(key, {})
            lines.append(f"**{label}:** {d.get('score', '?')}/5 — {d.get('comment', '')}")
        lines.append("")

    issues = ev.get("top_issues", [])
    if issues:
        lines.append("## Top Issues\n")
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append("")

    improvements = ev.get("improvements", [])
    if improvements:
        lines.append("## Improvement Recommendations\n")
        for imp in improvements:
            priority = imp.get("priority", "").upper()
            file_ref = imp.get("file", "")
            change   = imp.get("change", "")
            example  = imp.get("example", "")
            lines.append(f"### [{priority}] {file_ref}")
            lines.append(change)
            if example:
                lines.append(f"\n```\n{example}\n```")
            lines.append("")

    positives = ev.get("positive_observations", [])
    if positives:
        lines.append("## What's Working\n")
        for p in positives:
            lines.append(f"- {p}")

    return "\n".join(lines)


def _build_discord_summary(ev: dict) -> str:
    """Compact text summary for Discord (fits within 2000 chars per message)."""
    week    = ev.get("week", "unknown")
    overall = ev.get("overall_score", "?")
    scores  = ev.get("scores", {})

    score_line = " | ".join(
        f"{k.split('_')[0].title()}: {v.get('score', '?')}/5"
        for k, v in scores.items()
    )

    issues = ev.get("top_issues", [])
    issue_block = "\n".join(f"  • {i}" for i in issues[:3])

    improvements = ev.get("improvements", [])
    high_pri = [i for i in improvements if i.get("priority") == "high"]
    imp_block = "\n".join(
        f"  [{i.get('priority','').upper()}] {i.get('file','')} — {i.get('change','')[:100]}"
        for i in improvements[:4]
    )

    return (
        f"**SELF-EVAL — {week}  |  Overall: {overall}/5**\n"
        f"{score_line}\n\n"
        f"**Top issues:**\n{issue_block}\n\n"
        f"**Recommended changes:**\n{imp_block}\n\n"
        f"_Full report: events/eval/{week}-eval.md_"
    )


def _write_report(report_md: str, week: str) -> Path:
    EVENTS_EVAL.mkdir(parents=True, exist_ok=True)
    report_file = EVENTS_EVAL / f"{week}-eval.md"
    report_file.write_text(report_md, encoding="utf-8")
    log.info("Evaluation report written to %s", report_file.name)
    return report_file


def run(dry_run: bool = False) -> None:
    week_end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("=" * 60)
    log.info("Self-evaluation | week_end=%s | dry_run=%s", week_end, dry_run)
    log.info("=" * 60)

    # Collect inputs
    ticker_logs = _collect_logs(EVENTS_TICKER, days=7)
    macro_logs  = _collect_logs(EVENTS_MACRO, days=7)

    if not ticker_logs:
        log.warning("No ticker event logs found for the past 7 days — nothing to evaluate")
        log.warning("Run the daily pipeline for a week first, then re-run self_eval.py")
        return

    # Read Pass 1 prompt excerpt for context
    pass1_excerpt = _read_script_excerpt("filter_material.py", max_chars=1500)

    # Collect human feedback from #self-eval Discord channel
    channel_map = _load_channel_map()
    eval_channel_id = channel_map.get("special/self-eval")
    human_feedback = _collect_human_feedback(eval_channel_id)
    if human_feedback:
        log.info("Collected %d chars of operator feedback from #self-eval", len(human_feedback))

    log.info("Running evaluation on %d chars of ticker logs, %d chars of macro logs",
             len(ticker_logs), len(macro_logs))

    # Evaluate
    evaluation = _evaluate(ticker_logs, macro_logs, SEARCH_QUERIES, pass1_excerpt, human_feedback)

    if "error" in evaluation:
        log.error("Evaluation failed: %s", evaluation["error"])
        return

    # Format and write report
    report_md = _format_report(evaluation)
    report_file = _write_report(report_md, week_end)

    # Print to stdout always (useful even in non-dry-run)
    print("\n" + report_md)

    if dry_run:
        log.info("[DRY RUN] Report written to %s — Discord post skipped", report_file.name)
        return

    # Post summary to Discord
    channel_map = _load_channel_map()
    channel_id  = channel_map.get("special/self-eval")
    if not channel_id:
        log.warning("No channel ID for special/self-eval — run discord_setup.py")
    else:
        summary = _build_discord_summary(evaluation)
        send_text(channel_id, summary)
        log.info("Self-eval summary posted to #self-eval")

    log.info("Self-evaluation complete.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)
