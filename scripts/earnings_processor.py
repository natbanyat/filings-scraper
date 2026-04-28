"""
Earnings event pipeline — six stages.

Stage 1: The Print   — SEC EDGAR 8-K/6-K press release + Alpha Vantage estimates
Stage 2: The Call    — transcript_fetcher cascade (EDGAR → IR → SA → Brave → Discord)
Stage 3: Synthesis   — Claude Sonnet structured JSON
Stage 4: Discord     — rich embed to ticker channel
Stage 5: Event log   — events/earnings/<key>_<period>_<date>.md
Stage 6: Coverage    — kpi_tree + catalysts auto-applied; thesis + debates proposed

Usage:
  python scripts/earnings_processor.py --pending            # process all flagged tickers
  python scripts/earnings_processor.py --coverage-key tickers/JPM
  python scripts/earnings_processor.py --coverage-key tickers/JPM --period "Q1 FY2026"
  python scripts/earnings_processor.py --coverage-key tickers/JPM --dry-run
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from utils import setup_logging, extract_json_object

log = setup_logging("earnings_processor")
import requests

from cache import earnings_processed, mark_earnings_processed
from config import COVERAGE, COVERAGE_ROOT, TICKER_META, resolve_coverage_file
from openclaw_gateway_model import CHEAP_MODEL, DEEP_MODEL, run_text
from transcript_fetcher import (
    TranscriptUnavailable,
    fetch_call_transcript,
    fetch_press_release,
)
from post_discord import send_embed, send_text

WORKSPACE_ROOT   = Path(__file__).resolve().parent.parent
EARNINGS_DIR     = WORKSPACE_ROOT / "events" / "earnings"
UPDATE_FLAGS_DIR = WORKSPACE_ROOT / "events" / "pending_updates"
CHANNEL_MAP_PATH = Path(__file__).parent / "channel_map.json"

EDGAR_BASE    = "https://data.sec.gov"
HEADERS_EDGAR = {"User-Agent": "investing-research-bot contact@example.com"}

def _discord_auto_apply_enabled() -> bool:
    return os.environ.get("ALLOW_DISCORD_TRANSCRIPT_AUTO_APPLY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        raise FileNotFoundError(
            "channel_map.json not found. Run 'python scripts/discord_setup.py' first."
        )
    return json.loads(CHANNEL_MAP_PATH.read_text())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


# ── Stage 1: The Print ────────────────────────────────────────────────────────

def _fetch_alpha_vantage_estimates(av_symbol: str) -> dict:
    """
    Fetch most recent quarterly actuals + estimates from Alpha Vantage.
    Returns dict with eps_actual, eps_estimate, revenue_actual, revenue_estimate,
    surprise_pct_eps, surprise_pct_rev — or empty dict on failure.
    """
    api_key = os.environ.get("ALPHA_VANTAGE_KEY")
    if not api_key or not av_symbol:
        return {}

    try:
        resp = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "EARNINGS", "symbol": av_symbol, "apikey": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        quarterly = data.get("quarterlyEarnings", [])
        if not quarterly:
            return {}

        q = quarterly[0]  # most recent quarter

        def _num(v: str | None) -> float | None:
            try:
                return float(v) if v and v not in ("None", "-") else None
            except (ValueError, TypeError):
                return None

        eps_actual   = _num(q.get("reportedEPS"))
        eps_estimate = _num(q.get("estimatedEPS"))
        surprise_pct = _num(q.get("surprisePercentage"))

        result: dict = {
            "period_label": q.get("fiscalDateEnding", ""),
            "eps_actual":   eps_actual,
            "eps_estimate": eps_estimate,
            "surprise_pct_eps": surprise_pct,
        }

        # Revenue is in annual earnings only on free tier; skip if absent
        log.info("Alpha Vantage: EPS actual=%s estimate=%s surprise=%s%%",
                 eps_actual, eps_estimate, surprise_pct)
        return result

    except Exception as e:
        log.warning("Alpha Vantage fetch failed for %s: %s", av_symbol, e)
        return {}


def _extract_print_metrics(press_release_text: str, name: str, av_estimates: dict) -> dict:
    """
    Use Claude Haiku to extract structured earnings metrics from the press release.
    Returns dict with print_data keys.
    """
    av_block = ""
    if av_estimates:
        av_block = f"""
Alpha Vantage estimates for context:
  EPS actual: {av_estimates.get('eps_actual')}
  EPS estimate: {av_estimates.get('eps_estimate')}
  EPS surprise: {av_estimates.get('surprise_pct_eps')}%
"""

    prompt = f"""You are extracting earnings metrics from a press release for {name}.
{av_block}
Press release text (first 6000 chars):
{press_release_text[:6000]}

Extract and return ONLY valid JSON with these keys (use null if unavailable):
{{
  "period": "Q1 FY2026 or similar",
  "EPS": {{"actual": 4.44, "estimate": 4.20, "beat_pct": "+5.7%"}},
  "revenue": {{"actual_bn": 45.3, "estimate_bn": 44.1, "beat_pct": "+2.7%", "currency": "USD"}},
  "key_metrics": [{{"name": "NII", "actual": "23.2bn", "vs_prior_year": "+3%"}}],
  "guidance": "one sentence summary of updated guidance or null"
}}

Return ONLY the JSON object, no explanation."""

    try:
        raw = run_text(prompt, model=CHEAP_MODEL, timeout=180).strip()
        json_str = extract_json_object(raw)
        if json_str:
            json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
            return json.loads(json_str)
    except Exception as e:
        log.warning("%s: print metrics extraction failed: %s", name, e)

    return {}


# ── Stage 2: The Call ─────────────────────────────────────────────────────────

def _summarize_transcript(transcript_text: str, transcript_source: str, name: str) -> dict:
    """
    Use Claude Haiku to summarize the earnings call transcript.
    Returns dict with call_data keys.
    """
    prompt = f"""You are summarizing an earnings call transcript for {name}.
Source: {transcript_source}

Transcript text (first 7000 chars):
{transcript_text[:7000]}

Return ONLY valid JSON:
{{
  "tone": "cautious|confident|mixed",
  "key_themes": ["theme1", "theme2", "theme3"],
  "guidance_detail": "specific numerical guidance or forward statements",
  "notable_quotes": ["CEO: ...", "CFO: ..."],
  "analyst_pushback": ["question or concern raised by analysts"],
  "mgmt_credibility": "any signs of hedging, vagueness, or contradictions"
}}

Return ONLY the JSON object."""

    try:
        raw = run_text(prompt, model=CHEAP_MODEL, timeout=180).strip()
        json_str = extract_json_object(raw)
        if json_str:
            json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
            return json.loads(json_str)
    except Exception as e:
        log.warning("%s: transcript summarization failed: %s", name, e)

    return {}


# ── Stage 3: Synthesis ────────────────────────────────────────────────────────

def _synthesize(
    coverage_key: str,
    name: str,
    period_label: str,
    print_data: dict,
    call_data: dict,
    transcript_source: str,
    transcript_method: str,
    today: str,
) -> dict:
    """
    Claude Sonnet: combine print + call + coverage context into a synthesis report.
    Returns structured synthesis dict.
    """
    coverage_path = COVERAGE_ROOT / coverage_key
    kpi_text = _read(resolve_coverage_file(coverage_path, "kpi_tree.md"))
    thesis_text  = _read(coverage_path / "thesis.md")
    catalysts_text = _read(resolve_coverage_file(coverage_path, "catalysts.md"))

    from config import MAX_KPI_CHARS, MAX_CATALYST_CHARS, MAX_THESIS_CHARS

    # Use stripped/truncated versions for context economy
    kpi_block       = kpi_text[:MAX_KPI_CHARS]
    catalyst_block  = catalysts_text[:MAX_CATALYST_CHARS]
    # Thesis: variant view + risks section only
    thesis_block    = thesis_text[:MAX_THESIS_CHARS]

    compact_print = {
        "period": print_data.get("period"),
        "EPS": print_data.get("EPS"),
        "revenue": print_data.get("revenue"),
        "key_metrics": print_data.get("key_metrics", [])[:4],
        "guidance": print_data.get("guidance"),
    }
    compact_call = {
        "tone": call_data.get("tone"),
        "key_themes": call_data.get("key_themes", [])[:4],
        "guidance_detail": call_data.get("guidance_detail"),
        "notable_quotes": call_data.get("notable_quotes", [])[:2],
        "analyst_pushback": call_data.get("analyst_pushback", [])[:2],
        "mgmt_credibility": call_data.get("mgmt_credibility"),
    }

    prompt = f"""You are synthesizing earnings results for {name} — {period_label}.
Today: {today}
Call source: {transcript_source} ({transcript_method})

━━━ PRINT DATA ━━━
{json.dumps(compact_print, indent=2)}

━━━ CALL SUMMARY ━━━
{json.dumps(compact_call, indent=2)}

━━━ KPI TREE (current) ━━━
{kpi_block}

━━━ CATALYSTS (current) ━━━
{catalyst_block}

━━━ THESIS CONTEXT ━━━
{thesis_block}

━━━ TASK ━━━
Synthesize the above into a structured earnings report. Return ONLY valid JSON:
{{
  "headline": "one-line summary of the quarter",
  "direction": "bull|bear|neutral",
  "conviction_change": "higher|lower|unchanged",
  "print_vs_estimates": {{
    "EPS": {{"actual": null, "estimate": null, "beat_pct": null}},
    "revenue": {{"actual_bn": null, "estimate_bn": null, "beat_pct": null}},
    "key_metrics": []
  }},
  "guidance_summary": "one paragraph on guidance",
  "call_themes": ["theme1", "theme2", "theme3"],
  "notable_quotes": ["quote1", "quote2"],
  "thesis_check": "1-2 sentences on whether results confirm/challenge the core thesis",
  "kpi_actuals": {{"metric_name": "value"}},
  "catalyst_updates": ["catalyst that was triggered or deferred"],
  "debate_updates": ["which debate was informed and how"],
  "auto_apply_notes": "what to update in kpi_tree.md and catalysts.md",
  "propose_notes": "what to propose for thesis.md and debates.md"
}}

Direction: bull = results meaningfully above expectations or thesis confirmed.
           bear = results meaningfully below or thesis challenged.
           neutral = in-line, no meaningful thesis change."""

    try:
        raw = run_text(prompt, model=DEEP_MODEL, timeout=240).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
            result["transcript_source"] = transcript_source
            result["transcript_method"] = transcript_method
            result["period"] = period_label
            result["coverage_key"] = coverage_key
            result["date"] = today
            return result
    except Exception as e:
        log.error("%s: synthesis failed: %s", name, e)

    return {
        "headline": f"{name} {period_label} — synthesis failed",
        "direction": "neutral",
        "conviction_change": "unchanged",
        "transcript_source": transcript_source,
        "transcript_method": transcript_method,
        "period": period_label,
        "coverage_key": coverage_key,
        "date": today,
    }


# ── Stage 4: Discord post ─────────────────────────────────────────────────────

DIRECTION_COLOR = {
    "bull":    0x2ECC71,   # green
    "bear":    0xE74C3C,   # red
    "neutral": 0xF39C12,   # amber
}


def _build_earnings_embed(name: str, period_label: str, synthesis: dict) -> dict:
    direction  = synthesis.get("direction", "neutral").lower()
    color      = DIRECTION_COLOR.get(direction, DIRECTION_COLOR["neutral"])
    headline   = synthesis.get("headline", f"{name} {period_label}")
    conv_label = synthesis.get("conviction_change", "unchanged").upper()

    title = f"{name} | {period_label} Earnings — {direction.upper()} [{conv_label}]"

    fields = []

    # Print summary
    pve = synthesis.get("print_vs_estimates", {})
    eps = pve.get("EPS", {})
    rev = pve.get("revenue", {})
    print_lines = []
    if eps.get("actual") is not None:
        print_lines.append(f"EPS: {eps['actual']} vs est {eps.get('estimate','?')} ({eps.get('beat_pct','?')})")
    if rev.get("actual_bn") is not None:
        cur = rev.get("currency", "USD")
        print_lines.append(f"Rev: {rev['actual_bn']}bn {cur} vs est {rev.get('estimate_bn','?')}bn ({rev.get('beat_pct','?')})")
    for km in pve.get("key_metrics", [])[:4]:
        print_lines.append(f"{km.get('name','')}: {km.get('actual','')} ({km.get('vs_prior_year','')} YoY)")
    if print_lines:
        fields.append({"name": "Print", "value": "\n".join(print_lines)[:1024], "inline": False})

    # Guidance
    guidance = synthesis.get("guidance_summary", "")
    if guidance:
        fields.append({"name": "Guidance", "value": guidance[:1024], "inline": False})

    # Call themes
    themes = synthesis.get("call_themes", [])
    if themes:
        fields.append({"name": "Call Themes", "value": "\n".join(f"• {t}" for t in themes[:5])[:1024], "inline": False})

    # Thesis check
    thesis_check = synthesis.get("thesis_check", "")
    if thesis_check:
        fields.append({"name": "Thesis Check", "value": thesis_check[:1024], "inline": False})

    # Notable quotes
    quotes = synthesis.get("notable_quotes", [])
    if quotes:
        fields.append({"name": "Notable Quotes", "value": "\n".join(f'"{q}"' for q in quotes[:2])[:1024], "inline": False})

    source = synthesis.get("transcript_source", "unknown")
    footer = f"Source: {source} | {synthesis.get('date', '')}"

    # Enforce 6000 char total limit
    TOTAL_LIMIT = 5900
    used = len(title) + len(headline) + len(footer)
    trimmed_fields = []
    for f in fields:
        cost = len(f["name"]) + len(f["value"])
        if used + cost > TOTAL_LIMIT:
            break
        used += cost
        trimmed_fields.append(f)

    return {
        "title":       title[:256],
        "description": headline[:4096],
        "color":       color,
        "fields":      trimmed_fields,
        "footer":      {"text": footer[:2048]},
    }


# ── Stage 5: Event log ────────────────────────────────────────────────────────

def _write_event_log(name: str, period_label: str, synthesis: dict, today: str) -> Path:
    EARNINGS_DIR.mkdir(parents=True, exist_ok=True)
    safe_key    = synthesis.get("coverage_key", name).replace("/", "_")
    safe_period = period_label.replace(" ", "_")
    fname       = f"{safe_key}_{safe_period}_{today}.md"
    path        = EARNINGS_DIR / fname

    lines = [
        f"# {name} — {period_label} Earnings",
        f"Date: {today}",
        f"Direction: {synthesis.get('direction', 'neutral').upper()}",
        f"Conviction: {synthesis.get('conviction_change', 'unchanged')}",
        f"Transcript source: {synthesis.get('transcript_source', 'unknown')}",
        "",
        f"## Headline",
        synthesis.get("headline", ""),
        "",
        f"## Print vs Estimates",
        json.dumps(synthesis.get("print_vs_estimates", {}), indent=2),
        "",
        f"## Guidance",
        synthesis.get("guidance_summary", ""),
        "",
        f"## Call Themes",
        "\n".join(f"- {t}" for t in synthesis.get("call_themes", [])),
        "",
        f"## Thesis Check",
        synthesis.get("thesis_check", ""),
        "",
        f"## Notable Quotes",
        "\n".join(f'> "{q}"' for q in synthesis.get("notable_quotes", [])),
        "",
        f"## KPI Actuals",
        json.dumps(synthesis.get("kpi_actuals", {}), indent=2),
        "",
        f"## Catalyst Updates",
        "\n".join(f"- {c}" for c in synthesis.get("catalyst_updates", [])),
        "",
        f"## Debate Updates",
        "\n".join(f"- {d}" for d in synthesis.get("debate_updates", [])),
        "",
        f"## Coverage Update Notes",
        f"**Auto-apply (kpi_tree, catalysts):** {synthesis.get('auto_apply_notes', '')}",
        f"**Propose (thesis, debates):** {synthesis.get('propose_notes', '')}",
        "",
        "---",
        f"*Full synthesis JSON:*",
        "```json",
        json.dumps(synthesis, indent=2),
        "```",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("%s: event log written to %s", name, path)
    return path


# ── Stage 6: Coverage update ──────────────────────────────────────────────────

def _trigger_coverage_update(
    coverage_key: str, synthesis: dict, apply_auto: bool, dry_run: bool
) -> None:
    """
    Invoke coverage_updater with earnings synthesis context.
    Auto-applies kpi_tree + catalysts; proposes thesis + debates.
    """
    import coverage_updater as cu

    name = coverage_key.split("/")[-1].upper()
    today = synthesis.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    coverage_path = COVERAGE_ROOT / coverage_key

    # ── Auto-apply files (factual: actuals and expired catalysts) ──────────────
    auto_files = list(dict.fromkeys(
        resolve_coverage_file(coverage_path, fname).name
        for fname in ("kpi_tree.md", "catalysts.md")
    ))
    auto_current = {f: cu._read(coverage_path / f) for f in auto_files}

    if not dry_run:
        auto_updates = _generate_earnings_update(
            coverage_key, name, synthesis, auto_files, auto_current, today,
            mode="auto"
        )
        if auto_updates:
            if apply_auto:
                cu._apply_updates(coverage_key, auto_updates)
                log.info("%s: auto-applied %s", name, list(auto_updates.keys()))
            else:
                out_dir = cu._write_proposals(coverage_key, auto_updates, today + "-auto")
                log.info("%s: auto-apply proposals written to %s", name, out_dir)
    else:
        log.info("%s [DRY RUN] would auto-apply: %s", name, auto_files)

    # ── Propose files (interpretive: thesis and debates) ──────────────────────
    propose_files = ["thesis.md", "debates.md"]
    propose_current = {f: cu._read(coverage_path / f) for f in propose_files}

    if not dry_run:
        propose_updates = _generate_earnings_update(
            coverage_key, name, synthesis, propose_files, propose_current, today,
            mode="propose"
        )
        if propose_updates:
            out_dir = cu._write_proposals(coverage_key, propose_updates, today + "-propose")
            log.info("%s: proposals written to %s", name, out_dir)

            # Post to #coverage-updates
            try:
                channel_map = _load_channel_map()
                channel_id = channel_map.get("special/coverage-updates")
                if channel_id:
                    msg = (
                        f"**EARNINGS COVERAGE UPDATE — {name}** ({synthesis.get('period', '')})\n"
                        f"Direction: {synthesis.get('direction','').upper()} | "
                        f"Conviction: {synthesis.get('conviction_change','unchanged')}\n"
                        f"**Auto-applied:** {', '.join(auto_files)}\n"
                        f"**Proposed (review needed):** thesis.md, debates.md\n"
                        f"Path: `{out_dir.relative_to(WORKSPACE_ROOT)}`\n"
                        f"Run `coverage_updater.py --coverage-key {coverage_key} --trigger earnings --apply` to apply proposed changes."
                    )
                    send_text(channel_id, msg)
            except Exception as e:
                log.warning("%s: coverage-updates Discord post failed: %s", name, e)
    else:
        log.info("%s [DRY RUN] would propose: %s", name, propose_files)


def _generate_earnings_update(
    coverage_key: str,
    name: str,
    synthesis: dict,
    files_to_update: list[str],
    current_files: dict[str, str],
    today: str,
    mode: str,  # "auto" or "propose"
) -> dict[str, str]:
    """Generate updated coverage file content based on earnings synthesis."""
    current_block = "\n\n".join(
        f"=== CURRENT {fname} ===\n{content}"
        for fname, content in current_files.items()
        if content
    )

    files_list = ", ".join(files_to_update)
    period = synthesis.get("period", "")

    if mode == "auto":
        synthesis_payload = {
            "period": synthesis.get("period"),
            "headline": synthesis.get("headline"),
            "direction": synthesis.get("direction"),
            "conviction_change": synthesis.get("conviction_change"),
            "print_vs_estimates": synthesis.get("print_vs_estimates", {}),
            "kpi_actuals": synthesis.get("kpi_actuals", {}),
            "catalyst_updates": synthesis.get("catalyst_updates", []),
            "auto_apply_notes": synthesis.get("auto_apply_notes", ""),
            "transcript_source": synthesis.get("transcript_source", ""),
            "transcript_method": synthesis.get("transcript_method", ""),
        }
        current_limit = 2200
        instructions = """
For KPI files (kpi_tree.md or kpis.md): fill in actuals from kpi_actuals; update any KPI nodes with new data.
For catalyst/watchlist files (catalysts.md or watchlist.md): mark triggered catalysts as completed; update dates and status for upcoming ones.
These are factual updates — apply exactly what the synthesis says, do not add interpretation."""
    else:
        synthesis_payload = {
            "period": synthesis.get("period"),
            "headline": synthesis.get("headline"),
            "direction": synthesis.get("direction"),
            "conviction_change": synthesis.get("conviction_change"),
            "guidance_summary": synthesis.get("guidance_summary", ""),
            "call_themes": synthesis.get("call_themes", [])[:3],
            "notable_quotes": synthesis.get("notable_quotes", [])[:1],
            "thesis_check": synthesis.get("thesis_check", ""),
            "debate_updates": synthesis.get("debate_updates", []),
            "propose_notes": synthesis.get("propose_notes", ""),
            "transcript_source": synthesis.get("transcript_source", ""),
            "transcript_method": synthesis.get("transcript_method", ""),
        }
        current_limit = 2800
        instructions = """
For thesis.md: only update if the earnings genuinely shift the variant view or confirm/challenge core thesis.
  Add a note at top: <!-- Updated {today}: {one-line reason} -->
  If in-line with thesis, make minimal update to reflect the data point.
For debates.md: update current position on any debate directly informed by these results.
  Note what data point moved which debate and in which direction.
These are interpretive updates — exercise judgment."""

    synthesis_block = json.dumps(synthesis_payload, indent=2)

    prompt = f"""You are updating coverage files for {name} based on {period} earnings results.
Today: {today}

━━━ EARNINGS SYNTHESIS ━━━
{synthesis_block}

━━━ CURRENT FILES ━━━
{current_block[:current_limit]}

━━━ INSTRUCTIONS ━━━
Files to update: {files_list}
{instructions}

Return ONLY valid JSON with one key per file:
{{
  "{files_to_update[0]}": "full updated file content",
  ...
}}
Only include keys for files in "{files_list}"."""

    try:
        raw = run_text(prompt, model=DEEP_MODEL, timeout=240 if mode == "auto" else 300).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            updates = json.loads(match.group())
            return {k: v for k, v in updates.items() if k in files_to_update}
    except Exception as e:
        log.warning("%s: coverage update generation failed (%s mode): %s", name, mode, e)

    return {}


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_one(
    coverage_key: str,
    period_label: str | None = None,
    apply_auto: bool = True,
    dry_run: bool = False,
) -> dict | None:
    """
    Run the full earnings pipeline for one coverage key.
    Returns the synthesis dict or None on failure.
    """
    if coverage_key not in COVERAGE:
        log.error("Unknown coverage key: %s", coverage_key)
        return None

    name = coverage_key.split("/")[-1].upper()
    meta = TICKER_META.get(coverage_key, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Determine period label
    if not period_label:
        period_label = _infer_period_label(coverage_key)
    if not period_label:
        log.error("%s: could not determine period label — use --period", name)
        return None

    log.info("=" * 60)
    log.info("Earnings pipeline | %s | %s | dry_run=%s", name, period_label, dry_run)
    log.info("=" * 60)

    # Dedup guard
    if earnings_processed(coverage_key, period_label):
        log.info("%s %s: already processed — skipping", name, period_label)
        return None

    # ── Stage 1: The Print ────────────────────────────────────────────────────
    log.info("%s: Stage 1 — The Print", name)

    press_release = None
    press_release_text = ""
    transcript_source_stage1 = "none"
    transcript_method_stage1 = "none"
    try:
        press_release = fetch_press_release(
            coverage_key,
            period_label,
            sec_cik=meta.get("sec_cik"),
        )
        press_release_text = press_release.get("text", "")
        transcript_source_stage1 = press_release.get("source", "SEC EDGAR")
        transcript_method_stage1 = press_release.get("method", "edgar")
        log.info("%s: press release fetched via %s (%d chars)",
                 name, transcript_source_stage1, len(press_release_text))
    except TranscriptUnavailable:
        log.warning("%s: press release not available — continuing with empty print", name)
    except Exception as e:
        log.warning("%s: press release fetch error: %s", name, e)

    av_estimates = _fetch_alpha_vantage_estimates(meta.get("av_symbol", ""))
    print_data = _extract_print_metrics(press_release_text, name, av_estimates) if press_release_text else {}
    log.info("%s: Stage 1 complete — print_data keys: %s", name, list(print_data.keys()))

    # ── Stage 2: The Call ─────────────────────────────────────────────────────
    log.info("%s: Stage 2 — The Call", name)

    call_data: dict = {}
    transcript_source = transcript_source_stage1
    transcript_method = transcript_method_stage1

    try:
        channel_map = _load_channel_map()
        uploads_channel = channel_map.get("special/earnings-uploads")
    except Exception:
        uploads_channel = None

    try:
        transcript = fetch_call_transcript(
            coverage_key,
            period_label,
            ir_page=meta.get("ir_page"),
            earnings_uploads_channel_id=uploads_channel,
            skip_discord=dry_run,   # don't block on Discord polling in dry-run
        )
        transcript_source = transcript.get("source", "unknown")
        transcript_method = transcript.get("method", "unknown")
        log.info("%s: transcript fetched via %s (%d chars)",
                 name, transcript_source, len(transcript.get("text", "")))
        call_data = _summarize_transcript(transcript.get("text", ""), transcript_source, name)
    except TranscriptUnavailable:
        log.warning("%s: transcript unavailable — continuing with empty call data", name)
    except Exception as e:
        log.warning("%s: transcript fetch error: %s", name, e)

    log.info("%s: Stage 2 complete — call_data keys: %s", name, list(call_data.keys()))

    # ── Stage 3: Synthesis ────────────────────────────────────────────────────
    log.info("%s: Stage 3 — Synthesis", name)

    synthesis = _synthesize(
        coverage_key, name, period_label,
        print_data, call_data, transcript_source, transcript_method, today,
    )

    log.info("%s: synthesis — direction=%s conviction_change=%s",
             name, synthesis.get("direction"), synthesis.get("conviction_change"))

    if dry_run:
        log.info("%s [DRY RUN] Synthesis:\n%s", name, json.dumps(synthesis, indent=2)[:2000])
        return synthesis

    effective_apply_auto = apply_auto
    if transcript_method == "discord" and effective_apply_auto and not _discord_auto_apply_enabled():
        effective_apply_auto = False
        log.warning(
            "%s: transcript source is Discord upload — auto-apply disabled. "
            "Set ALLOW_DISCORD_TRANSCRIPT_AUTO_APPLY=true to override.",
            name,
        )

    # ── Stage 4: Discord post ─────────────────────────────────────────────────
    log.info("%s: Stage 4 — Discord post", name)

    try:
        channel_map = _load_channel_map()
        coverage_cfg = COVERAGE[coverage_key]
        channel_key = coverage_key  # e.g. "tickers/JPM"
        channel_id  = channel_map.get(channel_key)
        if not channel_id:
            # Try by channel name as fallback
            channel_name = coverage_cfg.get("channel", "")
            channel_id = next(
                (cid for k, cid in channel_map.items()
                 if k.endswith("/" + channel_name) or k == channel_key),
                None,
            )

        if channel_id:
            embed = _build_earnings_embed(name, period_label, synthesis)
            send_embed(channel_id, embed)
            log.info("%s: Discord embed sent to channel %s", name, channel_id)
        else:
            log.warning("%s: no Discord channel found for %s", name, coverage_key)
    except Exception as e:
        log.error("%s: Discord post failed: %s", name, e)

    # ── Stage 5: Event log ────────────────────────────────────────────────────
    log.info("%s: Stage 5 — Event log", name)
    _write_event_log(name, period_label, synthesis, today)

    # ── Stage 6: Coverage update ──────────────────────────────────────────────
    log.info("%s: Stage 6 — Coverage update", name)
    try:
        _trigger_coverage_update(coverage_key, synthesis, apply_auto=effective_apply_auto, dry_run=False)
    except Exception as e:
        log.error("%s: coverage update failed: %s", name, e)

    # Mark as processed
    mark_earnings_processed(
        coverage_key,
        period_label,
        direction=synthesis.get("direction", ""),
        transcript_source=transcript_source,
    )
    log.info("%s %s: pipeline complete", name, period_label)
    return synthesis


def _infer_period_label(coverage_key: str) -> str | None:
    """
    Attempt to infer the current earnings period from earnings_calendar.json.
    Returns the most recent unprocessed period label or None.
    """
    cal_path = WORKSPACE_ROOT / "coverage" / "earnings_calendar.json"
    if not cal_path.exists():
        return None
    try:
        cal = json.loads(cal_path.read_text())
        entry = cal.get(coverage_key, {})
        today = datetime.now(timezone.utc).date()
        for e in entry.get("entries", []):
            if e.get("confirmed") and not e.get("processed"):
                return e.get("period")
            # If expected_date is within last 5 days and not yet processed
            expected = e.get("expected_date")
            if expected and not e.get("processed"):
                try:
                    exp_date = datetime.strptime(expected, "%Y-%m-%d").date()
                    delta = (today - exp_date).days
                    if 0 <= delta <= 5:
                        return e.get("period")
                except ValueError:
                    continue
    except Exception as e:
        log.debug("Calendar period inference failed for %s: %s", coverage_key, e)
    return None


def process_pending(apply_auto: bool = True, dry_run: bool = False) -> None:
    """Process all earnings flags from events/pending_updates/ with trigger=earnings."""
    if not UPDATE_FLAGS_DIR.exists():
        log.info("No pending update flags found.")
        return

    flags = sorted(UPDATE_FLAGS_DIR.glob("*.json"))
    processed_keys: set[str] = set()

    for flag_file in flags:
        try:
            payload = json.loads(flag_file.read_text())
            key     = payload.get("coverage_key", "")
            trigger = payload.get("trigger", "")
            period  = payload.get("period")

            if trigger != "earnings":
                continue  # non-earnings flags handled by coverage_updater.py
            if key in processed_keys:
                continue

            result = process_one(key, period_label=period, apply_auto=apply_auto, dry_run=dry_run)
            if result:
                processed_keys.add(key)
                if not dry_run:
                    flag_file.unlink()
        except Exception as e:
            log.error("Failed to process flag %s: %s", flag_file.name, e)


def main() -> None:
    parser = argparse.ArgumentParser(description="Earnings event pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--coverage-key", help="Single coverage key, e.g. tickers/JPM")
    group.add_argument("--pending",      action="store_true",
                       help="Process all pending earnings flags")

    parser.add_argument("--period",    help="Period label e.g. 'Q1 FY2026' (inferred from calendar if omitted)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Full pipeline, no writes or Discord posts")
    parser.add_argument("--no-auto-apply", action="store_true",
                        help="Propose kpi_tree + catalysts updates instead of auto-applying them")

    args = parser.parse_args()

    apply_auto = not args.no_auto_apply
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("Earnings processor | dry_run=%s | apply_auto=%s | date=%s",
             args.dry_run, apply_auto, today)

    if args.pending:
        process_pending(apply_auto=apply_auto, dry_run=args.dry_run)
    else:
        if args.coverage_key not in COVERAGE:
            log.error("Unknown coverage key: %s. Valid keys: %s",
                      args.coverage_key, list(COVERAGE.keys()))
            sys.exit(1)
        process_one(
            args.coverage_key,
            period_label=args.period,
            apply_auto=apply_auto,
            dry_run=args.dry_run,
        )

    log.info("Earnings processor complete.")


if __name__ == "__main__":
    main()
