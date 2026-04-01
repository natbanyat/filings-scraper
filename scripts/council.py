"""
LLM Council — Adversarial Multi-Agent Investment Research Framework
Phase 2: 5 agents, async parallel Layer 1, 2 debate rounds, depth router

Pipeline:
  Question Parser → Evidence Registry
  → Layer 1 (Fundamentals, Valuation, Risk [, Macro, Positioning]) — parallel
  → Bull/Bear Synthesizers
  → Debate Rounds (1 or 2 depending on depth)
  → Referee → PM Synthesizer → SUMMARY.md

Depth routing:
  LIGHT    — 3 agents (Fundamentals, Valuation, Risk), 1 debate round
  STANDARD — 5 agents, 1 debate round
  FULL     — 5 agents, 2 debate rounds (default)

Usage:
  python scripts/council.py "Is NVDA a buy here?"
  python scripts/council.py "Is NVDA a buy here?" --context "Q4 earnings beat, stock up 15%"
  python scripts/council.py "Is NVDA a buy here?" --portfolio-context --depth FULL
  python scripts/council.py "Is JPM a hold or trim after the recent rally?" --depth LIGHT
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# Add scripts/ to path so utils can be imported when run from workspace root
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import setup_logging, retry

log = setup_logging("council")

import anthropic

# ── Constants ──────────────────────────────────────────────────────────────────

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR   = WORKSPACE_ROOT / "research" / "council-sessions"
PORTFOLIO_CONTEXT_PATH = WORKSPACE_ROOT / "PORTFOLIO_CONTEXT.md"

MODEL = "claude-sonnet-4-6"

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.error("ANTHROPIC_API_KEY not set in environment / .env")
            sys.exit(1)
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


# ── Streaming call ─────────────────────────────────────────────────────────────

@retry(max_attempts=3, backoff=5.0, exceptions=(anthropic.APIError, anthropic.APIConnectionError))
def call_claude(system: str, user: str, max_tokens: int = 4096) -> str:
    """
    Streaming call to Claude Sonnet.
    Streaming keeps WSL2 NAT TCP connections alive.
    Returns the complete text response.
    """
    parts: list[str] = []
    with _get_client().messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        for text in stream.text_stream:
            parts.append(text)
    return "".join(parts).strip()


async def async_call_claude(system: str, user: str, max_tokens: int = 4096) -> str:
    """Async wrapper: runs call_claude in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(call_claude, system, user, max_tokens)


# ── Slug generation ────────────────────────────────────────────────────────────

def _make_slug(question: str) -> str:
    """Convert question to a filesystem-safe slug (max 50 chars)."""
    slug = re.sub(r"[^\w\s-]", "", question.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:50]


# ── Session directory setup ────────────────────────────────────────────────────

def create_session_dir(question: str, date_str: str) -> Path:
    slug = _make_slug(question)
    session_dir = SESSIONS_DIR / f"{date_str}-{slug}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def save(session_dir: Path, filename: str, content: str) -> None:
    (session_dir / filename).write_text(content, encoding="utf-8")
    log.info("Saved %s", filename)


# ── Evidence registry ──────────────────────────────────────────────────────────

def build_evidence_registry(
    question: str,
    user_context: str | None,
    portfolio_context: str | None,
) -> list[dict]:
    """
    Phase 1/2: Evidence registry from user-supplied context only.
    No live data fetching — agents will flag [ASSUMED] claims for anything else.
    """
    registry: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    if user_context:
        registry.append({
            "evidence_id": "E001",
            "source_type": "USER_SUPPLIED",
            "source_name": "User-provided context",
            "source_date": datetime.now(timezone.utc).date().isoformat(),
            "retrieved_at": now_iso,
            "claim_type": "FACT",
            "statement": user_context,
            "raw_excerpt": user_context,
            "freshness": "CURRENT",
            "trust_weight": 0.8,
        })

    if portfolio_context:
        # Trim to first 2000 chars to avoid overwhelming the registry display
        excerpt = portfolio_context[:2000]
        registry.append({
            "evidence_id": "E002",
            "source_type": "PORTFOLIO_CONTEXT",
            "source_name": "PORTFOLIO_CONTEXT.md (PM's daily snapshot)",
            "source_date": datetime.now(timezone.utc).date().isoformat(),
            "retrieved_at": now_iso,
            "claim_type": "FACT",
            "statement": "PM portfolio context snapshot (macro regime, thesis, coverage)",
            "raw_excerpt": excerpt,
            "freshness": "CURRENT",
            "trust_weight": 1.0,
        })

    if not registry:
        registry.append({
            "evidence_id": "E000",
            "source_type": "NONE",
            "source_name": "No external evidence supplied",
            "source_date": datetime.now(timezone.utc).date().isoformat(),
            "retrieved_at": now_iso,
            "claim_type": "ASSUMED",
            "statement": "No user-supplied context. Agents must rely on training knowledge and flag all factual claims as [ASSUMED].",
            "raw_excerpt": "",
            "freshness": "UNKNOWN",
            "trust_weight": 0.0,
        })

    return registry


def format_evidence_pack(registry: list[dict]) -> str:
    """Format the evidence registry for inclusion in agent prompts."""
    lines = ["## Evidence Registry\n"]
    for e in registry:
        lines.append(
            f"**{e['evidence_id']}** [{e['claim_type']}] — {e['source_name']} ({e['source_date']})\n"
            f"> {e['raw_excerpt'][:400]}\n"
        )
    return "\n".join(lines)


# ── Layer 0: Question Parser ───────────────────────────────────────────────────

def run_question_parser(
    question: str,
    user_context: str | None,
    portfolio_context: str | None,
    depth: str,
) -> tuple[dict, str]:
    """
    Parse the question into a structured decision contract.
    Returns (contract_dict, brief_markdown).
    """
    print("\n[Layer 0] Running Question Parser...")

    context_block = ""
    if user_context:
        context_block += f"\nAdditional context: {user_context}"
    if portfolio_context:
        context_block += f"\nPortfolio context excerpt:\n{portfolio_context[:1500]}"

    system = (
        "You are a research director at a fundamental investment fund. "
        "You structure investment questions into rigorous decision contracts before any research begins. "
        "You are precise, direct, and do not editorialize. "
        "Output valid JSON only — no prose before or after the JSON block."
    )

    user = f"""A key investment question has been posed. Your job is to:
1. Classify the question type: [MACRO | THEMATIC | SINGLE_NAME | CROSS_ASSET]
2. Define the decision contract
3. Identify the core testable hypothesis embedded in the question
4. List the 3-5 most important sub-questions that must be answered to resolve it
5. Identify what data/evidence would most move the answer
6. Flag any ambiguities, frame dependencies, and missing context
7. Confirm council depth: {depth}

Question: {question}{context_block}

Return ONLY this JSON structure (no prose before or after):
{{
  "question_type": "SINGLE_NAME",
  "decision_contract": {{
    "scope": "...",
    "decision_type": "ADD|TRIM|EXIT|HOLD|HEDGE|SCREEN|MONITOR",
    "time_horizon": "...",
    "benchmark": "...",
    "freshness_requirement": "INTRADAY|DAILY|WEEKLY|TIMELESS",
    "council_depth": "{depth}"
  }},
  "core_hypothesis": "...",
  "sub_questions": ["...", "...", "..."],
  "key_evidence_types": ["..."],
  "ambiguities": ["..."],
  "fundamentals_focus": "...",
  "valuation_focus": "...",
  "risk_focus": "...",
  "macro_focus": "...",
  "positioning_focus": "..."
}}"""

    raw = call_claude(system, user, max_tokens=2048)

    # Extract JSON
    json_str = _extract_json(raw)
    contract: dict = {}
    if json_str:
        try:
            contract = json.loads(json_str)
        except json.JSONDecodeError:
            log.warning("Question parser JSON parse failed; using raw text")

    # Generate brief markdown
    brief_md = f"# Question Brief\n\n**Question:** {question}\n\n"
    if contract:
        brief_md += f"**Type:** {contract.get('question_type', 'UNKNOWN')}\n\n"
        dc = contract.get("decision_contract", {})
        brief_md += "## Decision Contract\n"
        for k, v in dc.items():
            brief_md += f"- **{k}:** {v}\n"
        brief_md += f"\n## Core Hypothesis\n{contract.get('core_hypothesis', '')}\n\n"
        brief_md += "## Sub-Questions\n"
        for q in contract.get("sub_questions", []):
            brief_md += f"- {q}\n"
        brief_md += "\n## Key Evidence Types\n"
        for e in contract.get("key_evidence_types", []):
            brief_md += f"- {e}\n"
        brief_md += "\n## Ambiguities\n"
        for a in contract.get("ambiguities", []):
            brief_md += f"- {a}\n"
    else:
        brief_md += raw

    print(f"  -> Type: {contract.get('question_type', 'UNKNOWN')}")
    print(f"  -> Scope: {contract.get('decision_contract', {}).get('scope', '?')}")
    return contract, brief_md


# ── Layer 1: Independent Evidence Agents ──────────────────────────────────────

_EVIDENCE_TAG_REMINDER = (
    "Tag EVERY factual claim with one of: [FACT], [INFERRED], [ASSUMED], [MANAGEMENT_CLAIM].\n"
    "Use [ASSUMED] for any claim not directly evidenced in the Evidence Registry.\n"
    "Use [MANAGEMENT_CLAIM] for statements from company/management communications.\n"
    "Use [FACT] only for claims verifiable from a primary source in the registry.\n"
    "Use [INFERRED] for logical deductions from evidenced facts.\n"
)

_CONFIDENCE_REMINDER = (
    "Express confidence as:\n"
    "- HIGH: Would bet significant position; evidence is clear and convergent\n"
    "- MEDIUM: Directionally confident but key uncertainties remain\n"
    "- LOW: More likely than not but meaningful probability of being wrong\n"
    "- OPEN: Genuinely unresolved; requires more evidence\n"
)

_RETRIEVAL_BUDGET_NOTE = (
    "You have a focused retrieval mandate. Do not attempt to cover other agents' domains. "
    "If you encounter a finding outside your scope, flag it as a cross-domain note for the orchestrator."
)


async def run_fundamentals_agent(
    question: str,
    contract: dict,
    evidence_pack: str,
) -> str:
    """Layer 1: Fundamentals Agent — evidence gathering only, no investment conclusion."""
    focus = contract.get("fundamentals_focus", "business model, unit economics, financial trajectory")
    sub_qs = contract.get("sub_questions", [])
    hypothesis = contract.get("core_hypothesis", question)

    system = (
        "You are a senior fundamental analyst at a long-only investment fund. "
        "Your track record is built on deep business analysis: reading filings, stress-testing unit economics, "
        "and identifying earnings trajectory ahead of consensus. "
        "You are evidence-first: you do not form investment views without grounding in primary sources. "
        "You are skeptical of management guidance and test it against disclosed financials. "
        + _RETRIEVAL_BUDGET_NOTE
    )

    user = f"""## Research Mandate: Fundamentals Analysis

Question under investigation: {question}
Core hypothesis: {hypothesis}
Your focus area: {focus}

{evidence_pack}

Sub-questions assigned to you:
{chr(10).join(f'- {q}' for q in sub_qs[:3])}

{_EVIDENCE_TAG_REMINDER}
{_CONFIDENCE_REMINDER}

Your task:
1. Analyze the fundamental business and sector dynamics relevant to this question
2. Identify the 3-5 most important findings, each tagged with [FACT], [INFERRED], [ASSUMED], or [MANAGEMENT_CLAIM]
3. For each finding, note: the fact, source/basis, confidence level (HIGH/MEDIUM/LOW/OPEN), and whether it supports or undermines the core hypothesis
4. Assess unit economics and financial trajectory where relevant
5. List your top 3 open questions you could NOT resolve with available evidence
6. Do NOT form a buy/sell/hold conclusion — evidence gathering only

Output: Structured markdown with explicit evidence tags and confidence levels.
Reference evidence IDs from the registry where applicable (e.g., "per E001").
Any claim not in the registry must be marked [ASSUMED]."""

    return await async_call_claude(system, user, max_tokens=3000)


async def run_valuation_agent(
    question: str,
    contract: dict,
    evidence_pack: str,
) -> str:
    """Layer 1: Valuation Agent — quantitative valuation and scenario analysis."""
    focus = contract.get("valuation_focus", "multiples, DCF inputs, scenario analysis, comp set")
    hypothesis = contract.get("core_hypothesis", question)
    sub_qs = contract.get("sub_questions", [])

    system = (
        "You are a senior equity analyst specializing in quantitative valuation. "
        "You are rigorous about inputs: you distinguish between consensus estimates, management guidance, "
        "and your own derived assumptions. "
        "You build scenario analyses around the key swing factors rather than point estimates. "
        "You are skeptical of DCF models that are reverse-engineered to justify a price; "
        "you focus on what the market is implying and whether that is defensible. "
        + _RETRIEVAL_BUDGET_NOTE
    )

    user = f"""## Research Mandate: Valuation Analysis

Question under investigation: {question}
Core hypothesis: {hypothesis}
Your focus area: {focus}

{evidence_pack}

Sub-questions for valuation context:
{chr(10).join(f'- {q}' for q in sub_qs)}

{_EVIDENCE_TAG_REMINDER}
{_CONFIDENCE_REMINDER}

Your task:
1. Assess the current valuation: multiples, what the market is implying, how that compares to history and peers
2. Identify the 2-3 key swing factors in the valuation (what drives the bull vs. bear scenario)
3. Construct a bull/bear/base scenario with explicit assumptions for each
4. Flag where consensus estimates may be wrong and in which direction
5. List your top 3 open questions you could NOT resolve (e.g., missing data, uncertain inputs)
6. Do NOT form a buy/sell/hold conclusion — evidence and scenario framing only

Output: Structured markdown with evidence tags and confidence levels.
Reference evidence IDs from the registry where applicable.
Mark all non-registry claims as [ASSUMED]."""

    return await async_call_claude(system, user, max_tokens=3000)


async def run_risk_agent(
    question: str,
    contract: dict,
    evidence_pack: str,
) -> str:
    """Layer 1: Risk Agent — tail risks, regulatory, capital structure, and structural concerns."""
    focus = contract.get("risk_focus", "tail risks, regulatory/legal exposure, capital structure, structural risks")
    hypothesis = contract.get("core_hypothesis", question)
    sub_qs = contract.get("sub_questions", [])

    system = (
        "You are a risk analyst and forensic specialist at a hedge fund. "
        "Your mandate is to identify what can go structurally wrong — not generic market risk, "
        "but specific, embedded, underappreciated risks. "
        "You focus on: regulatory and legal exposure, capital structure fragility, "
        "management credibility and incentive alignment, competitive moat erosion, "
        "and any red flags in disclosed financials. "
        "You are not a short-seller by default, but you are paid to find what the market is ignoring. "
        + _RETRIEVAL_BUDGET_NOTE
    )

    user = f"""## Research Mandate: Risk Analysis

Question under investigation: {question}
Core hypothesis: {hypothesis}
Your focus area: {focus}

{evidence_pack}

Sub-questions for risk context:
{chr(10).join(f'- {q}' for q in sub_qs)}

{_EVIDENCE_TAG_REMINDER}
{_CONFIDENCE_REMINDER}

Your task:
1. Identify the 3-5 most material risks relevant to this question
2. For each risk: describe it specifically, assess the probability (HIGH/MEDIUM/LOW), assess the potential impact (HIGH/MEDIUM/LOW), and note whether the market appears to be pricing it in
3. Identify any regulatory, legal, or capital structure concerns
4. Assess management credibility relative to stated guidance and disclosures
5. Note any red flags or soft signals that deserve monitoring
6. List your top 3 open questions you could NOT resolve
7. Do NOT form a buy/sell/hold conclusion — risk identification only

Output: Structured markdown with evidence tags and confidence levels.
Reference evidence IDs from the registry where applicable.
Mark all non-registry claims as [ASSUMED]."""

    return await async_call_claude(system, user, max_tokens=3000)


async def run_macro_agent(
    question: str,
    contract: dict,
    evidence_pack: str,
) -> str:
    """Layer 1: Macro/Rates Agent — top-down macro overlay, rates, credit cycle, Fed path, USD."""
    focus = contract.get(
        "macro_focus",
        "macro regime, interest rates, credit cycle, Fed path, USD, cross-asset backdrop",
    )
    hypothesis = contract.get("core_hypothesis", question)
    sub_qs = contract.get("sub_questions", [])

    system = (
        "You are a macro strategist at a global investment firm. "
        "Your mandate is the top-down overlay: interest rate trajectory, credit cycle positioning, "
        "central bank reaction functions, USD direction, and how the macro regime affects sector and single-name outcomes. "
        "You think in regimes, not point forecasts. You identify where the macro consensus is likely wrong "
        "and what that means for risk assets. "
        "You do not do bottom-up fundamental analysis — that is another agent's domain. "
        + _RETRIEVAL_BUDGET_NOTE
    )

    user = f"""## Research Mandate: Macro/Rates Analysis

Question under investigation: {question}
Core hypothesis: {hypothesis}
Your focus area: {focus}

{evidence_pack}

Sub-questions for macro context:
{chr(10).join(f'- {q}' for q in sub_qs)}

{_EVIDENCE_TAG_REMINDER}
{_CONFIDENCE_REMINDER}

Your task:
1. Assess the current macro regime and where we are in the credit/rate cycle
2. Identify the 3-5 most important macro factors bearing on this question (rates, USD, credit spreads, growth outlook, liquidity)
3. For each factor: state the current level/trend, confidence in your assessment, and whether it is a tailwind or headwind for the thesis
4. Assess the Fed/central bank path and how far market pricing diverges from your baseline
5. Note any macro regime shift scenarios that would materially change the investment case
6. List your top 3 open questions you could NOT resolve
7. Do NOT form a buy/sell/hold conclusion — macro evidence gathering only

Output: Structured markdown with evidence tags and confidence levels.
Reference evidence IDs from the registry where applicable.
Mark all non-registry claims as [ASSUMED]."""

    return await async_call_claude(system, user, max_tokens=3000)


async def run_positioning_agent(
    question: str,
    contract: dict,
    evidence_pack: str,
) -> str:
    """Layer 1: Positioning/Sentiment Agent — crowding, short interest, fund flows, what is priced in."""
    focus = contract.get(
        "positioning_focus",
        "investor positioning, crowding, short interest, fund flows, sentiment, what is priced in",
    )
    hypothesis = contract.get("core_hypothesis", question)
    sub_qs = contract.get("sub_questions", [])

    system = (
        "You are a market structure and positioning analyst at a hedge fund. "
        "Your mandate is to understand who owns the stock/asset, how crowded the trade is, "
        "what the short interest tells us, where fund flows are going, and — critically — "
        "what the market has already priced in versus what is still a surprise. "
        "You are not a sentiment cheerleader: crowded longs are a risk, not a positive signal. "
        "You distinguish between retail sentiment (noise) and institutional positioning (signal). "
        "You do not do fundamental analysis — that is another agent's domain. "
        + _RETRIEVAL_BUDGET_NOTE
    )

    user = f"""## Research Mandate: Positioning/Sentiment Analysis

Question under investigation: {question}
Core hypothesis: {hypothesis}
Your focus area: {focus}

{evidence_pack}

Sub-questions for positioning context:
{chr(10).join(f'- {q}' for q in sub_qs)}

{_EVIDENCE_TAG_REMINDER}
{_CONFIDENCE_REMINDER}

Your task:
1. Assess current investor positioning: is this trade crowded or under-owned? What does short interest indicate?
2. Identify recent fund flow trends (sector/thematic rotation) and what they imply
3. Assess what the market currently has priced in — where does consensus sit relative to intrinsic value?
4. Identify key sentiment extremes or positioning inflection points (both bullish and bearish signals)
5. Note any technical or flow-driven factors that could amplify or dampen a fundamental move
6. List your top 3 open questions you could NOT resolve
7. Do NOT form a buy/sell/hold conclusion — positioning evidence gathering only

Output: Structured markdown with evidence tags and confidence levels.
Reference evidence IDs from the registry where applicable.
Mark all non-registry claims as [ASSUMED]."""

    return await async_call_claude(system, user, max_tokens=3000)


async def _run_agent_with_progress(name: str, coro) -> tuple[str, str]:
    """Wrap an agent coroutine with timing and progress printing."""
    t0 = time.monotonic()
    result = await coro
    elapsed = time.monotonic() - t0
    print(f"[Layer 1] {name} Agent complete ({elapsed:.0f}s)")
    return name, result


# ── Layer 2: Bull and Bear Synthesizers ───────────────────────────────────────

_FALSIFIER_REMINDER = (
    "State 3 specific, observable conditions that would prove your thesis wrong. "
    "These must be concrete and falsifiable within a defined time horizon. "
    "Vague risk factors ('macro deterioration') do not count."
)


def run_bull_synthesizer(
    question: str,
    contract: dict,
    layer1_outputs: dict[str, str],
) -> str:
    """Layer 2: Bull Synthesizer — construct the strongest possible bull case."""
    print("\n[Layer 2] Running Bull Synthesizer...")

    n_memos = len(layer1_outputs)
    memos_block = "\n\n---\n\n".join(
        f"## {name} Memo\n\n{content}"
        for name, content in layer1_outputs.items()
    )

    system = (
        "You are a long-only portfolio manager at a fundamental equity fund. "
        "You have built your career on identifying high-quality businesses trading at a discount to intrinsic value. "
        "You are an advocate, not a neutral analyst. Your job is to construct the strongest bull case "
        "from the evidence in front of you. "
        "You steelman the positive thesis — you are not lying, but you are arguing for the best outcome "
        "that the evidence can support. "
        "You are direct and take positions. You do not hedge every statement with 'on the other hand'."
    )

    user = f"""## Synthesis Mandate: Bull Case

You have received {n_memos} independent research memos on the following question:

Question: {question}
Core hypothesis: {contract.get('core_hypothesis', '')}

{memos_block}

Your mandate: Construct the strongest possible bull case.

{_FALSIFIER_REMINDER}

{_EVIDENCE_TAG_REMINDER}
{_CONFIDENCE_REMINDER}

Output:
1. **Bull Thesis** (3-5 key points, each tagged with evidence type)
2. **Supporting Evidence** (cite memo findings — reference by agent name and key finding)
3. **Key Assumptions** (what must be true for the bull case to hold)
4. **Manageable Risks** (risks the bulls acknowledge but view as overblown — explain why)
5. **Confidence Level**: [HIGH | MEDIUM | LOW] + explicit rationale
6. **Falsifiers**: 3 specific, observable conditions that would prove this wrong"""

    return call_claude(system, user, max_tokens=3000)


def run_bear_synthesizer(
    question: str,
    contract: dict,
    layer1_outputs: dict[str, str],
) -> str:
    """Layer 2: Bear Synthesizer — construct the strongest possible bear case."""
    print("\n[Layer 2] Running Bear Synthesizer...")

    n_memos = len(layer1_outputs)
    memos_block = "\n\n---\n\n".join(
        f"## {name} Memo\n\n{content}"
        for name, content in layer1_outputs.items()
    )

    system = (
        "You are a short-seller and forensic analyst at a hedge fund. "
        "Your track record depends on finding structural earnings disappointments, management credibility failures, "
        "and situations where consensus has the wrong earnings model. "
        "You are not a perma-bear — you are paid to find real embedded risk, not manufacture risk. "
        "You focus on: unit economics degradation, capital allocation quality, "
        "whether the market has the right assumptions, and red flags in disclosed information. "
        "You are direct and take positions. You do not defer to management narratives."
    )

    user = f"""## Synthesis Mandate: Bear Case

You have received {n_memos} independent research memos on the following question:

Question: {question}
Core hypothesis: {contract.get('core_hypothesis', '')}

{memos_block}

Your mandate: Construct the strongest possible bear case.
Find what the bulls are missing, minimizing, or getting structurally wrong.
Identify the most credible path to disappointment.
Be rigorous — do not manufacture risk, find real embedded risk.

{_FALSIFIER_REMINDER}

{_EVIDENCE_TAG_REMINDER}
{_CONFIDENCE_REMINDER}

Output:
1. **Bear Thesis** (3-5 key points, each tagged with evidence type)
2. **Supporting Evidence** (cite memo findings — reference by agent name and key finding)
3. **Key Assumptions** (what must be true for the bear case to hold)
4. **Bull Errors** (specifically what the bull narrative gets wrong or overstates)
5. **Confidence Level**: [HIGH | MEDIUM | LOW] + explicit rationale
6. **Falsifiers**: 3 specific, observable conditions that would prove this wrong"""

    return call_claude(system, user, max_tokens=3000)


# ── Layer 3: Debate Engine ─────────────────────────────────────────────────────

_ANTI_CAPITULATION = (
    "When revising your view, you must explicitly distinguish between:\n"
    "A. GENUINE REVISION: You found the counterargument persuasive on the evidence\n"
    "B. PARTIAL CONCESSION: One specific point is valid but does not change your overall view\n"
    "C. MAINTAINED POSITION: The argument was not persuasive — explain why\n\n"
    "Do NOT change your confidence simply because the other analyst expressed confidence. "
    "Evidence and logic, not social pressure, should drive revisions."
)


def _extract_view_revisions(
    round_num: int,
    presenter_text: str,
    responder_text: str,
    presenter_role: str = "bull",
    responder_role: str = "bear",
) -> list[dict]:
    """
    Extract confidence levels and produce view revision log entries for one debate round.
    """
    def _find_confidence(text: str) -> str:
        m = re.search(r"Confidence:\s*(HIGH|MEDIUM|LOW|OPEN)", text, re.IGNORECASE)
        return m.group(1).upper() if m else "UNKNOWN"

    presenter_conf = _find_confidence(presenter_text)
    responder_conf = _find_confidence(responder_text)

    if round_num == 1:
        return [
            {
                "round": 1,
                "agent": presenter_role,
                "role": "presenter",
                "confidence_stated": presenter_conf,
                "what_presented": "Full thesis presented",
                "what_did_not_change": f"{presenter_role.capitalize()} maintained full thesis in opening",
            },
            {
                "round": 1,
                "agent": responder_role,
                "role": "rebutter",
                "confidence_stated": responder_conf,
                "what_changed": f"{responder_role.capitalize()} engaged with specific {presenter_role} claims",
                "what_did_not_change": f"{responder_role.capitalize()} maintained core thesis",
            },
        ]
    else:
        return [
            {
                "round": 2,
                "agent": presenter_role,
                "role": "responder",
                "confidence_stated": presenter_conf,
                "what_changed": f"{presenter_role.capitalize()} responded to round 1 rebuttal",
                "what_did_not_change": "See round 2 debate transcript for details",
            },
            {
                "round": 2,
                "agent": responder_role,
                "role": "responder",
                "confidence_stated": responder_conf,
                "what_changed": f"{responder_role.capitalize()} responded to round 2 bull statement",
                "what_did_not_change": "See round 2 debate transcript for details",
            },
        ]


def run_debate_round1(
    question: str,
    bull_synthesis: str,
    bear_synthesis: str,
) -> tuple[str, str, str, list[dict]]:
    """
    Layer 3 Round 1: Bull presents, Bear rebuts.
    Returns (transcript, bull_presentation, bear_rebuttal, view_revisions).
    """
    print("\n[Layer 3] Running Debate Round 1...")

    # Round 1A: Bull presents
    print("  -> Round 1A: Bull presents thesis...")
    system_bull = (
        "You are a long-only portfolio manager presenting your bull investment thesis "
        "to an adversarial short-seller. You are confident, specific, and cite evidence. "
        "You do not hedge every statement. You anticipate the strongest objections."
    )
    bull_presentation_prompt = f"""## Debate: Bull Presentation (Round 1A)

Question: {question}

Your bull thesis:
{bull_synthesis}

Present your full bull thesis to the Bear analyst. Be specific, cite evidence, and explicitly anticipate the 2-3 strongest objections the bear will raise. Do not repeat your thesis verbatim — make the strongest possible affirmative case.

{_EVIDENCE_TAG_REMINDER}

Output: Structured debate statement (markdown). End with: "Confidence: [HIGH|MEDIUM|LOW]"
"""
    bull_presentation = call_claude(system_bull, bull_presentation_prompt, max_tokens=2000)

    # Round 1B: Bear rebuts
    print("  -> Round 1B: Bear rebuts...")
    system_bear = (
        "You are a short-seller rebutting a bull investment thesis. "
        "You are specific and adversarial. You engage directly with their claims. "
        "You do not repeat your full thesis — you focus entirely on rebuttal."
    )
    bear_rebuttal_prompt = f"""## Debate: Bear Rebuttal (Round 1B)

Question: {question}

Bull's presentation:
{bull_presentation}

Your bear thesis (background):
{bear_synthesis}

Your task:
1. Identify the 2-3 strongest weaknesses in the bull argument — be specific, not generic
2. State which of the bull's evidence you accept, partially accept, or reject (and why)
3. Present your bear rebuttal with direct reference to their specific claims
4. Do not repeat your full thesis — focus on rebuttal

{_ANTI_CAPITULATION}
{_EVIDENCE_TAG_REMINDER}

Output: Structured rebuttal (markdown). End with: "Confidence: [HIGH|MEDIUM|LOW]" and a one-sentence view revision statement.
"""
    bear_rebuttal = call_claude(system_bear, bear_rebuttal_prompt, max_tokens=2000)

    view_revisions = _extract_view_revisions(
        round_num=1,
        presenter_text=bull_presentation,
        responder_text=bear_rebuttal,
        presenter_role="bull",
        responder_role="bear",
    )

    debate_transcript = (
        f"# Debate Round 1\n\n"
        f"**Question:** {question}\n\n"
        f"---\n\n"
        f"## Round 1A — Bull Presentation\n\n{bull_presentation}\n\n"
        f"---\n\n"
        f"## Round 1B — Bear Rebuttal\n\n{bear_rebuttal}\n"
    )

    return debate_transcript, bull_presentation, bear_rebuttal, view_revisions


def run_debate_round2(
    question: str,
    bull_presentation_r1: str,
    bear_rebuttal_r1: str,
    bull_synthesis: str,
    bear_synthesis: str,
) -> tuple[str, str, str, list[dict]]:
    """
    Layer 3 Round 2: Bull responds to bear rebuttal, Bear responds to bull's response.
    Returns (transcript, bull_response, bear_response, view_revisions).
    """
    print("\n[Layer 3] Running Debate Round 2...")

    # Round 2A: Bull responds
    print("  -> Round 2A: Bull responds...")
    system_bull = (
        "You are a long-only portfolio manager responding to an adversarial bear rebuttal. "
        "You have heard the bear's critique and must now respond directly. "
        "Concede points you cannot defend. Double down on your strongest remaining arguments. "
        "Be specific and evidence-driven."
    )
    bull_response_prompt = f"""## Debate: Bull Response (Round 2A)

Question: {question}

Your original bull presentation (Round 1A):
{bull_presentation_r1}

Bear's rebuttal (Round 1B):
{bear_rebuttal_r1}

Your task:
1. Concede any points you cannot defend — be explicit about what you are giving up and why
2. Double down on your strongest remaining arguments — what is the bear most wrong about?
3. Identify where the bear case is weakest — the most fragile assumption or weakest evidence in their rebuttal
4. State if your confidence level has changed since Round 1 and why

{_ANTI_CAPITULATION}
{_EVIDENCE_TAG_REMINDER}

Output: Structured response (markdown). End with: "Confidence: [HIGH|MEDIUM|LOW]" and a one-sentence statement on whether your overall view has changed.
"""
    bull_response = call_claude(system_bull, bull_response_prompt, max_tokens=2000)

    # Round 2B: Bear responds
    print("  -> Round 2B: Bear responds...")
    system_bear = (
        "You are a short-seller responding to a bull's second-round defence. "
        "You have heard their response to your rebuttal. Now you must address it directly. "
        "Be specific. Acknowledge any valid concessions. Maintain your position where evidence supports it."
    )
    bear_response_prompt = f"""## Debate: Bear Response (Round 2B)

Question: {question}

Bear's Round 1B rebuttal (your prior statement):
{bear_rebuttal_r1}

Bull's Round 2A response:
{bull_response}

Your task:
1. State which of the bull's Round 2A points, if any, have partially changed your view — be explicit
2. Identify what would make you less bearish (concrete falsifiers for the bear case)
3. Double down on the core bear thesis points the bull has NOT addressed convincingly
4. State your current confidence level and whether it has changed since Round 1

{_ANTI_CAPITULATION}
{_EVIDENCE_TAG_REMINDER}

Output: Structured response (markdown). End with: "Confidence: [HIGH|MEDIUM|LOW]" and a one-sentence summary of where the debate stands.
"""
    bear_response = call_claude(system_bear, bear_response_prompt, max_tokens=2000)

    view_revisions = _extract_view_revisions(
        round_num=2,
        presenter_text=bull_response,
        responder_text=bear_response,
        presenter_role="bull",
        responder_role="bear",
    )

    debate_transcript = (
        f"# Debate Round 2\n\n"
        f"**Question:** {question}\n\n"
        f"---\n\n"
        f"## Round 2A — Bull Response\n\n{bull_response}\n\n"
        f"---\n\n"
        f"## Round 2B — Bear Response\n\n{bear_response}\n"
    )

    return debate_transcript, bull_response, bear_response, view_revisions


# ── Layer 4a: Referee ──────────────────────────────────────────────────────────

def run_referee(
    question: str,
    contract: dict,
    evidence_pack: str,
    layer1_outputs: dict[str, str],
    bull_synthesis: str,
    bear_synthesis: str,
    full_debate_transcript: str,
    view_revisions: list[dict],
) -> str:
    """Layer 4a: Referee — claim support, freshness, crux map, contradiction check."""
    print("\n[Layer 4a] Running Referee Agent...")

    memos_block = "\n\n---\n\n".join(
        f"## {name} Memo\n\n{content}"
        for name, content in layer1_outputs.items()
    )

    freshness_req = contract.get("decision_contract", {}).get("freshness_requirement", "DAILY")

    system = (
        "You are a research process auditor. You do not make the investment call. "
        "You verify claim support, evidence freshness, contradiction resolution, "
        "and whether the bull and bear are actually engaging the same empirical question. "
        "You are rigorous and mechanical. You flag issues without editorializing."
    )

    user = f"""## Referee Report

Question: {question}
Freshness requirement: {freshness_req}

You have access to:
1. Evidence Registry
2. Layer 1 research memos ({', '.join(layer1_outputs.keys())})
3. Bull and Bear syntheses
4. Full debate transcript (all rounds)
5. View revision logs

{evidence_pack}

---

## Layer 1 Memos
{memos_block}

---

## Bull Synthesis
{bull_synthesis}

---

## Bear Synthesis
{bear_synthesis}

---

## Full Debate Transcript
{full_debate_transcript}

---

## View Revisions
{json.dumps(view_revisions, indent=2)}

---

Your job is to produce a referee report that:

1. **Claim Support Issues**: Flag any unsupported or weakly supported claims in the debate (cite specific claims and why they lack support)
2. **Freshness Issues**: Flag evidence that is stale relative to the freshness requirement ({freshness_req})
3. **Crux Map**: Identify the 3-5 crux disagreements between bull and bear — the factual or interpretive differences that most drive the outcome
4. **Resolved vs Unresolved**: Which cruxes were resolved by the debate? Which remain open?
5. **Evidence Engagement**: Note where bull or bear ignored materially disconfirming evidence from the memos
6. **Evidence Quality Scorecard**: Rate overall citation coverage (HIGH/MEDIUM/LOW), evidence quality (HIGH/MEDIUM/LOW), and debate quality (SUBSTANTIVE/MIXED/VACUOUS)
7. **Unresolved Questions**: List the most important questions the council could not resolve

Output: Structured markdown referee report. Be specific — cite claims and evidence IDs where possible."""

    return call_claude(system, user, max_tokens=3500)


# ── Layer 4b: PM Synthesizer ───────────────────────────────────────────────────

def run_pm_synthesizer(
    question: str,
    contract: dict,
    referee_report: str,
    bull_synthesis: str,
    bear_synthesis: str,
    full_debate_transcript: str,
    view_revisions: list[dict],
    layer1_outputs: dict[str, str],
) -> str:
    """Layer 4b: PM Synthesizer — final investment memo. Runs AFTER referee."""
    print("\n[Layer 4b] Running PM Synthesizer...")

    dc = contract.get("decision_contract", {})

    system = (
        "You are a senior portfolio manager and research director. "
        "You have observed a structured adversarial investment debate. "
        "Your job is to produce the final synthesis: take a position and defend it. "
        "Do NOT average the views. "
        "Do NOT write a balanced 'on one hand... on the other hand' memo. "
        "Acknowledge the strongest counter-argument, but state your position clearly. "
        "If the referee flagged material evidence gaps, downgrade your confidence and say so explicitly. "
        "The minority view is preserved only if it contains genuine signal — summarize it, do not give it equal weight."
    )

    user = f"""## PM Synthesis: Final Investment Memo

Question: {question}

Decision Contract:
{json.dumps(dc, indent=2)}

---

## Referee Report
{referee_report}

---

## Bull Thesis
{bull_synthesis}

---

## Bear Thesis
{bear_synthesis}

---

## Full Debate Transcript
{full_debate_transcript}

---

## View Revisions
{json.dumps(view_revisions, indent=2)}

---

Your task is to produce a final synthesis that:

1. **Answer**: State the most defensible answer to the key question — directly and specifically
2. **Position**: State your actionable recommendation (with time horizon and benchmark from the decision contract)
3. **Strongest Arguments**: Which arguments (bull or bear) were most compelling and why — be specific
4. **Confidence**: State your overall confidence level (HIGH/MEDIUM/LOW/OPEN) with explicit rationale. If the referee flagged evidence gaps, reflect that.
5. **Key Uncertainties**: Quantify 2-3 key uncertainties with explicit confidence levels
6. **Falsifiers / What Would Change the View**: 3 specific, observable, time-bounded conditions that would reverse your recommendation
7. **Minority View**: If the losing side contains genuine signal worth monitoring, summarize it in 1-2 sentences
8. **Unresolved Cruxes**: From the referee report — what remains open and how that affects your confidence

Output format: Investment committee memo (structured markdown). Be direct. Take a position.

{_EVIDENCE_TAG_REMINDER}"""

    return call_claude(system, user, max_tokens=4096)


# ── SUMMARY.md ─────────────────────────────────────────────────────────────────

def generate_summary(
    question: str,
    date_str: str,
    contract: dict,
    bull_synthesis: str,
    bear_synthesis: str,
    pm_synthesis: str,
    view_revisions: list[dict],
    referee_report: str,
    num_rounds: int,
) -> str:
    """Generate SUMMARY.md — one-page quick reference."""
    print("\n[Summary] Generating SUMMARY.md...")

    dc = contract.get("decision_contract", {})
    question_type = contract.get("question_type", "UNKNOWN")

    system = (
        "You are a research editor. You distill a full investment council output into a concise one-page summary. "
        "Be direct. No filler. Include actual content (thesis points, confidence levels, falsifiers). "
        "Do not use vague placeholders."
    )

    user = f"""Generate a SUMMARY.md for this investment council session.

Question: {question}
Date: {date_str}
Question Type: {question_type}
Council Depth: {dc.get('council_depth', 'FULL')}
Rounds: {num_rounds}

## PM Synthesis (primary source)
{pm_synthesis}

## Bull Synthesis
{bull_synthesis[:800]}

## Bear Synthesis
{bear_synthesis[:800]}

## View Revisions
{json.dumps(view_revisions, indent=2)}

## Referee Report excerpt
{referee_report[:600]}

Generate the SUMMARY.md using this exact template:

# Council Summary: {question}
**Date:** {date_str} | **Rounds:** {num_rounds} | **Question Type:** {question_type}

## Answer
[one paragraph — PM's conclusion]

## Bull Case (confidence: X)
- [point 1]
- [point 2]
- [point 3]

## Bear Case (confidence: X)
- [point 1]
- [point 2]
- [point 3]

## PM Position
[position taken, rationale, confidence level]

## Key Uncertainties
- [uncertainty 1]
- [uncertainty 2]

## Falsifiers / What Would Change the View
- [falsifier 1 — specific and time-bounded]
- [falsifier 2]
- [falsifier 3]

## View Evolution
[How did confidence levels shift through the debate? What caused changes?]

## Process Quality
- Citation coverage: [HIGH/MEDIUM/LOW]
- Freshness breaches: [list or "None identified"]
- Main unresolved crux: [from referee report]"""

    return call_claude(system, user, max_tokens=2000)


# ── JSON extraction helper ─────────────────────────────────────────────────────

def _extract_json(text: str) -> str | None:
    """Extract first top-level JSON object from text."""
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
                return text[start:i + 1]
    return None


# ── Main pipeline ──────────────────────────────────────────────────────────────

async def run_council(
    question: str,
    user_context: str | None = None,
    use_portfolio_context: bool = False,
    depth: str = "FULL",
) -> Path:
    """
    Run the LLM Council pipeline (Phase 2).

    Depth routing:
      LIGHT    — 3 agents (Fundamentals, Valuation, Risk), 1 debate round
      STANDARD — 5 agents (+ Macro, Positioning), 1 debate round
      FULL     — 5 agents, 2 debate rounds (default)

    Returns the session directory path.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_dir = create_session_dir(question, date_str)

    # Determine routing from depth
    use_extended_agents = depth in ("STANDARD", "FULL")
    num_debate_rounds   = 2 if depth == "FULL" else 1
    n_agents            = 5 if use_extended_agents else 3

    print(f"\n{'='*60}")
    print(f"LLM Council — Phase 2")
    print(f"Question: {question}")
    print(f"Depth: {depth} ({n_agents} agents, {num_debate_rounds} debate round{'s' if num_debate_rounds > 1 else ''})")
    print(f"Session: {session_dir.name}")
    print(f"{'='*60}")

    # Load portfolio context if requested
    portfolio_context: str | None = None
    if use_portfolio_context:
        if PORTFOLIO_CONTEXT_PATH.exists():
            portfolio_context = PORTFOLIO_CONTEXT_PATH.read_text(encoding="utf-8")
            print(f"\n[Context] Loaded PORTFOLIO_CONTEXT.md ({len(portfolio_context)} chars)")
        else:
            print(f"\n[Context] PORTFOLIO_CONTEXT.md not found — skipping")

    # ── Layer 0: Question Parser ──────────────────────────────────────────────
    contract, brief_md = run_question_parser(question, user_context, portfolio_context, depth)
    save(session_dir, "00-question-brief.md", brief_md)
    save(session_dir, "00-decision-contract.json", json.dumps(contract, indent=2))
    print(f"  -> Saved 00-question-brief.md, 00-decision-contract.json")

    # ── Layer 0: Evidence Registry ────────────────────────────────────────────
    registry = build_evidence_registry(question, user_context, portfolio_context)
    registry_json = json.dumps(registry, indent=2)
    save(session_dir, "00-evidence-registry.json", registry_json)
    evidence_pack = format_evidence_pack(registry)
    print(f"  -> Evidence registry: {len(registry)} item(s)")

    # ── Layer 1: Independent Agents (async parallel) ──────────────────────────
    print(f"\n[Layer 1] Starting {n_agents} agents in parallel...")

    agent_coros = [
        _run_agent_with_progress("Fundamentals", run_fundamentals_agent(question, contract, evidence_pack)),
        _run_agent_with_progress("Valuation",    run_valuation_agent(question, contract, evidence_pack)),
        _run_agent_with_progress("Risk",         run_risk_agent(question, contract, evidence_pack)),
    ]
    if use_extended_agents:
        agent_coros += [
            _run_agent_with_progress("Macro",       run_macro_agent(question, contract, evidence_pack)),
            _run_agent_with_progress("Positioning", run_positioning_agent(question, contract, evidence_pack)),
        ]

    agent_results = await asyncio.gather(*agent_coros)
    layer1_outputs: dict[str, str] = {name: memo for name, memo in agent_results}

    # Save Layer 1 memos
    filename_map = {
        "Fundamentals": "01-fundamentals-memo.md",
        "Valuation":    "01-valuation-memo.md",
        "Risk":         "01-risk-memo.md",
        "Macro":        "01-macro-memo.md",
        "Positioning":  "01-positioning-memo.md",
    }
    for agent_name, memo in layer1_outputs.items():
        fname = filename_map[agent_name]
        save(session_dir, fname, f"# {agent_name} Agent Memo\n\n{memo}")
        print(f"  -> Saved {fname} ({len(memo)} chars)")

    # ── Layer 2: Adversarial Synthesizers ─────────────────────────────────────
    bull_synthesis = run_bull_synthesizer(question, contract, layer1_outputs)
    save(session_dir, "02-bull-synthesis.md", f"# Bull Synthesis\n\n{bull_synthesis}")
    print(f"  -> Saved 02-bull-synthesis.md")

    bear_synthesis = run_bear_synthesizer(question, contract, layer1_outputs)
    save(session_dir, "02-bear-synthesis.md", f"# Bear Synthesis\n\n{bear_synthesis}")
    print(f"  -> Saved 02-bear-synthesis.md")

    # ── Layer 3: Debate Rounds ────────────────────────────────────────────────
    debate_r1, bull_pres_r1, bear_reb_r1, revisions_r1 = run_debate_round1(
        question, bull_synthesis, bear_synthesis
    )
    save(session_dir, "03-debate-round1.md", debate_r1)
    print(f"  -> Saved 03-debate-round1.md")

    all_revisions = list(revisions_r1)
    full_debate_transcript = debate_r1

    if num_debate_rounds >= 2:
        debate_r2, bull_resp_r2, bear_resp_r2, revisions_r2 = run_debate_round2(
            question, bull_pres_r1, bear_reb_r1, bull_synthesis, bear_synthesis
        )
        save(session_dir, "03-debate-round2.md", debate_r2)
        print(f"  -> Saved 03-debate-round2.md")
        all_revisions += revisions_r2
        full_debate_transcript = debate_r1 + "\n\n" + debate_r2

    save(session_dir, "03-view-revisions.json", json.dumps(all_revisions, indent=2))
    print(f"  -> Saved 03-view-revisions.json ({len(all_revisions)} revision entries)")

    # ── Layer 4a: Referee ─────────────────────────────────────────────────────
    referee_report = run_referee(
        question=question,
        contract=contract,
        evidence_pack=evidence_pack,
        layer1_outputs=layer1_outputs,
        bull_synthesis=bull_synthesis,
        bear_synthesis=bear_synthesis,
        full_debate_transcript=full_debate_transcript,
        view_revisions=all_revisions,
    )
    save(session_dir, "04-referee-report.md", f"# Referee Report\n\n{referee_report}")
    print(f"  -> Saved 04-referee-report.md")

    # ── Layer 4b: PM Synthesizer ──────────────────────────────────────────────
    pm_synthesis = run_pm_synthesizer(
        question=question,
        contract=contract,
        referee_report=referee_report,
        bull_synthesis=bull_synthesis,
        bear_synthesis=bear_synthesis,
        full_debate_transcript=full_debate_transcript,
        view_revisions=all_revisions,
        layer1_outputs=layer1_outputs,
    )
    save(session_dir, "05-pm-synthesis.md", f"# PM Synthesis\n\n{pm_synthesis}")
    print(f"  -> Saved 05-pm-synthesis.md")

    # Event log snippet for optional coverage integration
    event_snippet = (
        f"## Council Session — {date_str}\n\n"
        f"**Question:** {question}\n\n"
        f"**Session:** {session_dir.name}\n\n"
        f"[Full output in research/council-sessions/{session_dir.name}/]\n"
    )
    save(session_dir, "05-event-log-snippet.md", event_snippet)

    # ── SUMMARY.md ────────────────────────────────────────────────────────────
    summary_md = generate_summary(
        question=question,
        date_str=date_str,
        contract=contract,
        bull_synthesis=bull_synthesis,
        bear_synthesis=bear_synthesis,
        pm_synthesis=pm_synthesis,
        view_revisions=all_revisions,
        referee_report=referee_report,
        num_rounds=num_debate_rounds,
    )
    save(session_dir, "SUMMARY.md", summary_md)
    print(f"  -> Saved SUMMARY.md")

    print(f"\n{'='*60}")
    print(f"Council complete.")
    print(f"Session: {session_dir}")
    print(f"{'='*60}\n")

    # Print SUMMARY.md to stdout
    print(summary_md)

    return session_dir


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM Council — Adversarial multi-agent investment research (Phase 2)"
    )
    parser.add_argument(
        "question",
        help='Investment question to research. E.g.: "Is NVDA a buy here?"',
    )
    parser.add_argument(
        "--context",
        default=None,
        metavar="TEXT",
        help="Additional context string to supply as evidence (e.g. recent earnings, price action)",
    )
    parser.add_argument(
        "--portfolio-context",
        action="store_true",
        help="Load PORTFOLIO_CONTEXT.md (PM's portfolio snapshot) into the evidence registry",
    )
    parser.add_argument(
        "--depth",
        choices=["LIGHT", "STANDARD", "FULL"],
        default="FULL",
        help=(
            "Council depth (default: FULL). "
            "LIGHT: 3 agents, 1 debate round. "
            "STANDARD: 5 agents, 1 debate round. "
            "FULL: 5 agents, 2 debate rounds."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pipeline plan without calling the API",
    )

    args = parser.parse_args()

    if args.dry_run:
        depth = args.depth
        use_extended = depth in ("STANDARD", "FULL")
        n_agents = 5 if use_extended else 3
        n_rounds = 2 if depth == "FULL" else 1
        agents = ["Fundamentals", "Valuation", "Risk"]
        if use_extended:
            agents += ["Macro", "Positioning"]
        print(f"[dry-run] Council plan for: {args.question!r}")
        print(f"  depth:             {depth}")
        print(f"  agents ({n_agents}):      {', '.join(agents)}")
        print(f"  debate rounds:     {n_rounds}")
        print(f"  context:           {args.context!r}")
        print(f"  portfolio-context: {args.portfolio_context}")
        print(f"  output:            research/council-sessions/YYYY-MM-DD-<slug>/")
        return

    asyncio.run(run_council(
        question=args.question,
        user_context=args.context,
        use_portfolio_context=args.portfolio_context,
        depth=args.depth,
    ))


if __name__ == "__main__":
    main()
