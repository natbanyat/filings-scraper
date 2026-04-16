"""
Two-pass materiality filter — portfolio-context design (v2).

Context source: PORTFOLIO_CONTEXT.md (symlink to Cowork OneDrive, updated daily)
  + per-ticker update_log.md for recent event history

Pass 1 (Haiku, lean context):
  Input:  headline + snippet for all fetched articles
  Context: PM thesis line + sector drivers + recent events from update_log
  Output: which articles are material, direction
  ~400-600 tokens per coverage item

Pass 2 (Sonnet, batched):
  Input:  all material articles in ONE call
  Context: full portfolio context section + macro regime + recent events
  Output: top-line daily brief + structured event analysis per article
  ~1 Sonnet call per coverage item regardless of article count
"""

import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import anthropic

from utils import retry
from bounded_router import run_json_task

log = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_CONTEXT_PATH = WORKSPACE_ROOT / "PORTFOLIO_CONTEXT.md"

# Tickers that attract broad macro noise — extra specificity rules injected into Pass 1.
# Add entries here when a ticker routinely gets macro articles via loose KPI node linkage.
TICKER_SPECIFICITY_HINTS: dict[str, str] = {
    "tickers/FUTU": (
        "FUTU-specific: Only include articles about Futu Holdings, moomoo, Chinese retail brokerage, "
        "Hong Kong/US trading volumes, China capital markets regulation, or competing brokers "
        "(Tiger, Webull). Exclude all general EM macro, commodity news, or geopolitics not directly "
        "referencing Futu or Chinese retail brokerage market structure."
    ),
    "tickers/MMYT": (
        "MMYT-specific: Only include articles about MakeMyTrip, India OTA industry, Indian aviation "
        "(IndiGo, Air India, SpiceJet), Indian hotel chains, India outbound travel, Yatra, or direct "
        "India consumer travel data. Exclude geopolitical macro (oil, West Asia tensions, FX moves) "
        "unless it reports actual India flight cancellations, route suspensions, or India fare data."
    ),
    "sectors/korea-memory": (
        "KOREA-MEMORY-specific: Only include articles about Samsung Electronics memory, SK Hynix, Micron, "
        "HBM, DRAM, NAND, wafer pricing, memory capex, fab utilization, packaging constraints, CXMT, or "
        "AI server memory demand. Exclude broad geopolitics, oil, tariffs, or macro risk-off stories unless "
        "they explicitly discuss semiconductor supply chains, fab costs, memory pricing, or Korean chipmakers."
    ),
}

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        import os
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    return _client


# ── Context loaders ───────────────────────────────────────────────────────────

def _load_portfolio_context() -> str:
    """Load PORTFOLIO_CONTEXT.md content."""
    if not PORTFOLIO_CONTEXT_PATH.exists():
        log.warning("PORTFOLIO_CONTEXT.md not found at %s", PORTFOLIO_CONTEXT_PATH)
        return ""
    return PORTFOLIO_CONTEXT_PATH.read_text(encoding="utf-8").strip()


def _extract_name_context(portfolio_text: str, name: str, coverage_key: str) -> str:
    """Extract relevant context for a specific name from PORTFOLIO_CONTEXT.md.

    Returns a compact block with: thesis line, sector drivers, macro, catalysts.
    """
    if not portfolio_text:
        return f"Coverage: {name}. No portfolio context available."

    parts = [f"Coverage: {name}"]

    # Extract the coverage table row for this name (check ticker symbols)
    ticker_symbol = coverage_key.split("/")[-1] if "/" in coverage_key else name
    for line in portfolio_text.split("\n"):
        if "|" in line and (
            ticker_symbol in line
            or name.upper() in line.upper()
            or name.lower() in line.lower()
        ):
            parts.append(f"Position: {line.strip()}")
            break

    # Extract "What Matters by Sector" for relevant sector
    sector_map = {
        "tickers/JPM": "Japan banks",  # not really, but JPM is US financials
        "tickers/8316": "Japan banks",
        "tickers/1299": "Insurance",
        "tickers/STAN": "UK/Asia banks",
        "tickers/GRAB": "SE Asia",
        "tickers/SE": "SE Asia",
        "tickers/FUTU": "SE Asia",
        "tickers/MMYT": "India",
        "tickers/GOOG": "Tech",
        "tickers/TMX": "exchanges",
        "sectors/gold-miners": "Gold miners",
        "sectors/uranium-miners": "Uranium",
        "sectors/japan-banks": "Japan banks",
        "sectors/exchanges": "exchanges",
        "markets/japan": "Japan banks",
        "markets/korea": "Korea",
        "sectors/korea-memory": "Korea",
    }
    sector_hint = sector_map.get(coverage_key, "")

    in_sector_block = False
    for line in portfolio_text.split("\n"):
        if line.startswith("## What Matters by Sector"):
            in_sector_block = True
            continue
        if in_sector_block and line.startswith("## "):
            break
        if in_sector_block and line.startswith("- **") and (
            sector_hint.lower() in line.lower()
            or ticker_symbol.lower() in line.lower()
            or name.lower() in line.lower()
        ):
            parts.append(f"Sector drivers: {line.strip().lstrip('- ')}")

    # Extract macro regime
    in_macro = False
    macro_lines = []
    for line in portfolio_text.split("\n"):
        if line.startswith("## Macro Regime"):
            in_macro = True
            continue
        if in_macro and line.startswith("## "):
            break
        if in_macro and line.strip():
            macro_lines.append(line.strip())
    if macro_lines:
        parts.append(f"Macro: {' '.join(macro_lines[:3])}")

    # Extract rates/FX
    in_rates = False
    for line in portfolio_text.split("\n"):
        if line.startswith("## Rates & FX"):
            in_rates = True
            continue
        if in_rates and line.startswith("## "):
            break
        if in_rates and line.strip():
            parts.append(f"Rates: {line.strip()}")

    # Extract today's key items
    in_today = False
    for line in portfolio_text.split("\n"):
        if line.startswith("## Today's Key Items"):
            in_today = True
            continue
        if in_today and line.startswith("## "):
            break
        if in_today and line.strip() and not line.startswith("[Pending"):
            parts.append(f"Today: {line.strip()}")

    # Extract upcoming catalysts
    in_catalysts = False
    for line in portfolio_text.split("\n"):
        if line.startswith("## Upcoming Catalysts"):
            in_catalysts = True
            continue
        if in_catalysts and line.startswith("## "):
            break
        if in_catalysts and line.strip() and not line.startswith("[Pending"):
            parts.append(f"Catalyst: {line.strip()}")

    return "\n".join(parts)


def _load_recent_events(coverage_path: Path, days: int = 7) -> str:
    """Load recent entries from update_log.md for a coverage item."""
    log_path = coverage_path / "update_log.md"
    if not log_path.exists():
        return ""

    text = log_path.read_text(encoding="utf-8").strip()
    if not text:
        return ""

    # Try to extract only entries from the last N days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    lines = text.split("\n")
    recent = []
    include = False
    for line in lines:
        # Detect date headers like "## 2026-03-20" or "### 2026-03-20"
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", line)
        if date_match:
            include = date_match.group(1) >= cutoff
        if include:
            recent.append(line)

    result = "\n".join(recent).strip() if recent else text[-2000:]  # fallback: last 2000 chars
    return result[:2000]  # cap at 2000 chars


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


def _fallback_brief(name: str, articles: list[dict]) -> dict:
    if not articles:
        return {
            "headline": f"No material developments flagged for {name}.",
            "thesis_line": "No change to the current thesis.",
            "what_changed": "No incremental change identified from today's article set.",
            "key_debate": "No debate update identified.",
            "watchpoints": [],
            "overall_direction": "neutral",
        }

    first = articles[0]
    direction = str(first.get("direction", "neutral")).lower()
    return {
        "headline": first.get("title", f"{name} material update"),
        "thesis_line": "Material news was flagged, but the synthesized brief was unavailable.",
        "what_changed": "Review the linked developments below for the KPI and thesis impact.",
        "key_debate": "Debate linkage unavailable.",
        "watchpoints": [],
        "overall_direction": direction if direction in {"bull", "bear", "neutral", "mixed"} else "neutral",
    }


def _article_sort_key(article: dict) -> tuple[int, int, str]:
    impact_rank = {"view-changing": 0, "confirming": 1, "incremental": 2}
    direction_rank = {"bear": 0, "bull": 1, "neutral": 2, "mixed": 3}
    return (
        impact_rank.get(str(article.get("impact_type", "")).lower(), 3),
        direction_rank.get(str(article.get("direction", "")).lower(), 3),
        article.get("title", ""),
    )


def _run_pass_two_request(prompt: str, max_tokens: int) -> tuple[str, str | None]:
    """Execute the Sonnet pass-two request and return (raw_text, stop_reason)."""
    raw_parts: list[str] = []
    with _get_client().messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            raw_parts.append(text)
        final_message = stream.get_final_message()
    return "".join(raw_parts).strip(), final_message.stop_reason


# ── Catalyst context loader ──────────────────────────────────────────────────

_EARNINGS_CALENDAR_PATH = WORKSPACE_ROOT / "coverage" / "earnings_calendar.json"

# Central bank meeting keywords mapped to affected coverage keys
_CB_BOOST_MAP = {
    "BOJ": ["tickers/8316", "sectors/japan-banks", "markets/japan"],
    "Fed": ["tickers/JPM", "sectors/exchanges"],
    "BOK": ["markets/korea"],
}


def _load_catalyst_boost(coverage_key: str, portfolio_text: str = "") -> str:
    """
    Check if today is near an earnings date or central bank meeting for this
    coverage key.  Returns a sensitivity boost instruction for Pass 1, or "".

    Accepts pre-loaded portfolio_text to avoid redundant file reads.
    """
    from datetime import date

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today = date.fromisoformat(today_str)
    parts = []

    # Check earnings calendar
    try:
        if _EARNINGS_CALENDAR_PATH.exists():
            cal = json.loads(_EARNINGS_CALENDAR_PATH.read_text(encoding="utf-8"))
            entry = cal.get(coverage_key, {})
            for e in entry.get("entries", []):
                if not e.get("expected_date") or e.get("confirmed"):
                    continue
                exp = date.fromisoformat(e["expected_date"])
                delta = abs((exp - today).days)
                if delta <= 1:
                    parts.append(
                        f"SENSITIVITY BOOST: {coverage_key.split('/')[-1].upper()} "
                        f"earnings expected {e['expected_date']}. "
                        f"Lower your materiality threshold for all earnings-adjacent articles."
                    )
                    break
    except Exception:
        pass

    # Check central bank meetings from PORTFOLIO_CONTEXT.md "Upcoming Catalysts" section
    if not portfolio_text:
        portfolio_text = _load_portfolio_context()
    try:
        in_catalysts = False
        for line in portfolio_text.split("\n"):
            if line.startswith("## Upcoming Catalysts"):
                in_catalysts = True
                continue
            if in_catalysts and line.startswith("## "):
                break
            if in_catalysts and today_str in line:
                for cb_name, affected_keys in _CB_BOOST_MAP.items():
                    if cb_name.lower() in line.lower() and coverage_key in affected_keys:
                        parts.append(
                            f"SENSITIVITY BOOST: {cb_name} meeting today. "
                            f"Lower your materiality threshold for rate, yield, "
                            f"and monetary policy articles."
                        )
    except Exception:
        pass

    return "\n".join(parts)


# ── Pass 1: fast materiality triage ──────────────────────────────────────────

@retry(max_attempts=3, backoff=3.0, exceptions=(anthropic.APIError, anthropic.APIConnectionError, anthropic.APIStatusError))
def pass_one(
    articles: list[dict],
    coverage_path: Path,
    coverage_key: str = "",
    name: str = "",
    price_context: str | None = None,
    macro_context: str = "",
    portfolio_text: str = "",
) -> list[dict]:
    """
    Haiku pass: filter articles down to material hits.
    Returns subset with added 'kpi_node' and 'direction' fields.
    """
    if not articles:
        return []

    if not portfolio_text:
        portfolio_text = _load_portfolio_context()
    name_context = _extract_name_context(portfolio_text, name or coverage_path.name, coverage_key)
    recent_events = _load_recent_events(coverage_path, days=7)

    # Use body text when available (Trafilatura-enriched), fall back to description
    article_block = "\n".join(
        f"[{i}] {a['title']} | {a['source']} | {a.get('body', a['description'])[:400]}"
        for i, a in enumerate(articles)
    )

    events_section = f"\nRECENT EVENTS (past 7 days):\n{recent_events}" if recent_events else ""
    market_section = ""
    if price_context or macro_context:
        parts = [p for p in [price_context, macro_context] if p]
        market_section = f"\nMARKET CONTEXT:\n" + "\n".join(parts)

    catalyst_boost = _load_catalyst_boost(coverage_key, portfolio_text=portfolio_text)
    catalyst_section = f"\n{catalyst_boost}" if catalyst_boost else ""

    # Load open watchpoints for this coverage key
    watchpoint_section = ""
    try:
        from cache import get_open_watchpoints
        open_wps = get_open_watchpoints(coverage_key)
        if open_wps:
            wp_lines = "\n".join(f"- {wp['watchpoint']}" for wp in open_wps[:5])
            watchpoint_section = f"\nOPEN WATCHPOINTS (flag articles that address these):\n{wp_lines}"
    except Exception:
        pass

    specificity_hint = TICKER_SPECIFICITY_HINTS.get(coverage_key, "")
    specificity_line = f"\n- {specificity_hint}" if specificity_hint else ""

    prompt = f"""You are a buy-side financial materiality filter. Output ONLY valid JSON — no prose.

PORTFOLIO CONTEXT:
{name_context}
{events_section}
{market_section}
{catalyst_section}
{watchpoint_section}

ARTICLES ([index] title | source | content):
{article_block}

Return a JSON array of material articles only (omit immaterial ones entirely):
[{{"index": 0, "kpi_node": "short label for the affected driver", "direction": "bull|bear|neutral"}}, ...]

Rules:
- Include only articles that can change near-term earnings power, terminal value, catalyst timing, management credibility, balance-sheet risk, or a key market debate
- The event must directly affect a driver listed in the portfolio context, trigger a catalyst, or be material to the investment thesis
- Exclude: general market color, unrelated macro, earnings recaps with no new information, price-action commentary, opinion/valuation pieces, and ownership or 13F stake changes
- MACRO SPECIFICITY TEST: Before including any article, ask: does this article name the company, a direct competitor, or report actual data specific to this company's market? If the connection is only via a broad macro variable (oil, inflation, FX, geopolitics), EXCLUDE it — macro belongs in the daily macro brief, not ticker briefs. A macro article should only appear in a ticker brief if it directly quotes the company, reports sector-specific volume/revenue data, or is a regulatory/policy action specifically targeting this company's industry.{specificity_line}
- CRITICAL: If an article covers the same event already listed in RECENT EVENTS above, EXCLUDE it unless it contains materially new information (revised numbers, changed outcome, new data point). Follow-up coverage and commentary on previously reported events is NOT material.
- If multiple articles cover the exact same event, include only the one from the best source (prefer bloomberg.com, reuters.com, wsj.com, ft.com)
- If a SENSITIVITY BOOST is active, include borderline articles you would normally exclude for the boosted driver — err on the side of inclusion
- If an article directly addresses an OPEN WATCHPOINT, include it even if it would otherwise be borderline
- direction: bull = positive for the thesis, bear = negative, neutral = watch but unclear
- kpi_node: a short label for the key driver affected (e.g. "NII", "BOJ rate path", "GCP revenue")"""

    try:
        hits, route = run_json_task(
            task_class="materiality_filter",
            prompt=prompt,
            expected="array",
            max_tokens=512,
        )
    except Exception as e:
        log.warning("Pass 1: bounded routing failed for %s: %s", name, e)
        return []

    log.info("Pass 1 route (%s): %s via %s", name, route.tier, route.model)

    result = []
    for h in hits:
        idx = h.get("index")
        if idx is not None and 0 <= idx < len(articles):
            article = dict(articles[idx])
            article["kpi_node"]  = h.get("kpi_node", "")
            article["direction"] = h.get("direction", "neutral")
            result.append(article)

    log.info("Pass 1 (%s): %d -> %d material", name, len(articles), len(result))
    return result


# ── Pass 2: batched structured analysis ──────────────────────────────────────

@retry(max_attempts=3, backoff=5.0, exceptions=(anthropic.APIError, anthropic.APIConnectionError, anthropic.APIStatusError))
def pass_two(
    articles: list[dict],
    coverage_path: Path,
    name: str,
    coverage_key: str = "",
    price_context: str | None = None,
    macro_context: str = "",
    portfolio_text: str = "",
) -> dict:
    """
    Sonnet pass: write a top-line daily brief plus structured analysis for ALL
    material articles in one call.
    """
    if not articles:
        return {"brief": _fallback_brief(name, []), "articles": []}

    if not portfolio_text:
        portfolio_text = _load_portfolio_context()
    name_context = _extract_name_context(portfolio_text, name, coverage_key)
    recent_events = _load_recent_events(coverage_path, days=14)

    # Use body text when available, fall back to description
    articles_block = "\n\n".join(
        f"[{i}] Title: {a['title']}\n"
        f"    Source: {a['source']}\n"
        f"    Content: {a.get('body', a['description'])[:500]}\n"
        f"    Driver: {a['kpi_node']}\n"
        f"    Direction: {a['direction'].upper()}"
        for i, a in enumerate(articles)
    )

    events_section = f"\nRECENT EVENTS (past 14 days):\n{recent_events}" if recent_events else ""
    market_section = ""
    if price_context or macro_context:
        parts = [p for p in [price_context, macro_context] if p]
        market_section = f"\nMARKET CONTEXT:\n" + "\n".join(parts)

    prompt = f"""You are a skeptical buy-side analyst covering {name}.
Write the daily brief a portfolio manager actually needs: what changed, why it matters now,
which driver moved, and what to watch next.

PORTFOLIO CONTEXT:
{name_context}
{events_section}
{market_section}

ARTICLES TO ANALYZE:
{articles_block}

Return ONLY valid JSON (no prose before or after):
{{
  "brief": {{
    "headline": "one sentence capturing the single most important development for {name} today",
    "thesis_line": "one sentence: what the article set means for the current thesis right now",
    "what_changed": "2-3 bullet points: what matters, why now, which driver moved. Lead each bullet with the fact, not filler.",
    "key_debate": "one sentence naming the debate now most in focus",
    "watchpoints": [
      "specific leading indicator / disclosure / catalyst to watch next",
      "second watchpoint",
      "third watchpoint"
    ],
    "overall_direction": "bull|bear|neutral|mixed"
  }},
  "articles": [
    {{
      "index": 0,
      "event": "one sentence -- what happened",
      "kpi_node": "short driver label",
      "direction": "BULL|BEAR|NEUTRAL",
      "impact_type": "incremental|confirming|view-changing",
      "why_it_matters": "1-2 sentences on earnings power / terminal value impact",
      "thesis_link": "1 sentence tying the event to the thesis or a key debate",
      "watch_next": "1 sentence on the next leading indicator or data point"
    }}
  ]
}}

Rules:
- Be concise: conclusion upfront, then supporting facts with numbers
- Focus on what changed vs. what was already known
- Tie each article to the investment thesis and relevant drivers
- If the signal is mixed, say so explicitly rather than forcing a directional take
- Watchpoints must be concrete and observable (date, metric, threshold)
- Do not mention share-price moves unless they are part of the supplied article facts
- Each why_it_matters and thesis_link: max 2 sentences, lead with the conclusion not setup"""

    # Floor at 2048: the brief section needs ~500-700 tokens (headline, thesis_line,
    # what_changed bullets, key_debate, 3 watchpoints, direction).
    # Each article adds ~250-350 tokens of structured analysis.
    # Previous floor of 1536 still truncated 3-article runs when Sonnet was detailed.
    max_tokens = min(6144, max(2560, 700 + 425 * len(articles)))

    raw, stop_reason = _run_pass_two_request(prompt, max_tokens)

    if stop_reason == "max_tokens":
        log.warning("Pass 2 (%s): response truncated at %d tokens — output may be incomplete", name, max_tokens)

    json_str = _extract_json_object(raw)
    payload = None
    if json_str:
        json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
        try:
            payload = json.loads(json_str)
        except json.JSONDecodeError as e:
            log.warning("Pass 2: JSON parse error for %s: %s. Raw: %.500s", name, e, json_str)

    if payload is None:
        retry_articles = articles[: min(3, len(articles))]
        retry_block = "\n\n".join(
            f"[{i}] Title: {a['title']}\n"
            f"    Source: {a['source']}\n"
            f"    Content: {a.get('body', a['description'])[:320]}\n"
            f"    Driver: {a['kpi_node']}\n"
            f"    Direction: {a['direction'].upper()}"
            for i, a in enumerate(retry_articles)
        )
        retry_prompt = f"""You are a skeptical buy-side analyst covering {name}.
Return ONLY valid JSON. Be terse. Analyze only the supplied articles.
Every field must be short. Use at most 2 watchpoints and one sentence per field.

PORTFOLIO CONTEXT:
{name_context}
{events_section}
{market_section}

ARTICLES TO ANALYZE:
{retry_block}

Return exactly this schema:
{{
  "brief": {{
    "headline": "single sentence",
    "thesis_line": "single sentence",
    "what_changed": "1-2 short bullets or sentences",
    "key_debate": "single sentence",
    "watchpoints": ["watchpoint 1", "watchpoint 2"],
    "overall_direction": "bull|bear|neutral|mixed"
  }},
  "articles": [
    {{
      "index": 0,
      "event": "single sentence",
      "kpi_node": "short driver label",
      "direction": "BULL|BEAR|NEUTRAL",
      "impact_type": "incremental|confirming|view-changing",
      "why_it_matters": "single sentence",
      "thesis_link": "single sentence",
      "watch_next": "single sentence"
    }}
  ]
}}"""
        retry_max_tokens = 1800
        retry_raw, retry_stop_reason = _run_pass_two_request(retry_prompt, retry_max_tokens)
        if retry_stop_reason == "max_tokens":
            log.warning("Pass 2 compact retry (%s): response truncated at %d tokens", name, retry_max_tokens)
        retry_json = _extract_json_object(retry_raw)
        if retry_json:
            retry_json = re.sub(r",\s*([}\]])", r"\1", retry_json)
            try:
                payload = json.loads(retry_json)
                articles = retry_articles
                log.info("Pass 2 (%s): compact retry succeeded with %d article(s)", name, len(articles))
            except json.JSONDecodeError as e:
                log.warning("Pass 2 compact retry: JSON parse error for %s: %s. Raw: %.500s", name, e, retry_json)
        if payload is None:
            log.warning("Pass 2: no JSON object returned for %s after retry. Raw: %.300s", name, retry_raw or raw)
            return {"brief": _fallback_brief(name, articles), "articles": articles}

    if isinstance(payload, list):
        brief = _fallback_brief(name, articles)
        analyses = payload
    else:
        brief = payload.get("brief", {}) or _fallback_brief(name, articles)
        analyses = payload.get("articles", [])

    # Merge analysis back into article dicts
    analysis_map = {item["index"]: item for item in analyses if "index" in item}
    result = []
    for i, article in enumerate(articles):
        a = dict(article)
        info = analysis_map.get(i, {})
        direction = str(info.get("direction", a.get("direction", "neutral"))).lower()
        a["event"] = info.get("event", a.get("event", ""))
        a["kpi_node"] = info.get("kpi_node", a.get("kpi_node", ""))
        a["direction"] = direction
        a["impact_type"] = str(info.get("impact_type", a.get("impact_type", "incremental"))).lower()
        a["why_it_matters"] = info.get("why_it_matters", "")
        a["thesis_link"] = info.get("thesis_link", "")
        a["watch_next"] = info.get("watch_next", "")
        a["analysis"] = (
            f"**Event:** {a.get('event', '')}\n"
            f"**Driver:** {a.get('kpi_node', '')}\n"
            f"**Direction:** {direction.upper()}\n"
            f"**Impact type:** {a.get('impact_type', '')}\n"
            f"**Why it matters:** {a.get('why_it_matters', '')}\n"
            f"**Thesis link:** {a.get('thesis_link', '')}\n"
            f"**Watch next:** {a.get('watch_next', '')}"
        )
        result.append(a)

    result.sort(key=_article_sort_key)
    fallback_brief = _fallback_brief(name, result)
    for key in ("headline", "thesis_line", "what_changed", "key_debate"):
        if not str(brief.get(key, "")).strip():
            brief[key] = fallback_brief[key]
    brief["watchpoints"] = [w for w in brief.get("watchpoints", []) if isinstance(w, str) and w.strip()][:4]
    overall_direction = str(brief.get("overall_direction", "")).lower()
    if overall_direction not in {"bull", "bear", "neutral", "mixed"}:
        brief["overall_direction"] = fallback_brief["overall_direction"]

    log.info("Pass 2 (%s): %d articles analyzed in 1 Sonnet call", name, len(result))
    return {"brief": brief, "articles": result}
