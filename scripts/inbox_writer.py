"""Inbox handoff helpers for external research processing.

Writes markdown files into the OneDrive inbox folder using the schema expected
by the user's downstream twice-daily processor.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from config import INBOX_DIR, INBOX_PRIMARY_TICKER_OVERRIDES

log = logging.getLogger(__name__)


_INLINE_LIST_MAX = 8


def _slugify(text: str, fallback: str = "update", max_len: int = 64) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if not slug:
        slug = fallback
    return slug[:max_len].strip("-") or fallback


def _clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(f"- {item}" for item in parts)
    text = str(value).strip()
    return text


def _inline_yaml_list(values: list[str]) -> str:
    cleaned: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in cleaned:
            continue
        cleaned.append(text)
    if not cleaned:
        return "[]"
    clipped = cleaned[:_INLINE_LIST_MAX]
    rendered = ", ".join(f'"{item.replace("\\", "\\\\").replace("\"", "\\\"")}"' for item in clipped)
    return f"[{rendered}]"


def _coverage_primary_ticker(coverage_key: str) -> str:
    if coverage_key in INBOX_PRIMARY_TICKER_OVERRIDES:
        return INBOX_PRIMARY_TICKER_OVERRIDES[coverage_key]
    if coverage_key.startswith("tickers/"):
        return coverage_key.split("/", 1)[1]
    if coverage_key.startswith("markets/") or coverage_key.startswith("sectors/"):
        return "SECTOR"
    return "NOTE"


def _daily_priority(analyzed: list[dict]) -> str:
    impacts = {str(item.get("impact_type", "")).lower() for item in analyzed}
    if "view-changing" in impacts:
        return "high"
    if "confirming" in impacts or len(analyzed) >= 2:
        return "medium"
    return "low"


def _macro_priority(summary: dict) -> str:
    affected = summary.get("affected_names") or []
    risk_type = str(summary.get("risk_type", "none")).lower()
    if affected and risk_type not in {"", "none"}:
        return "high"
    if affected or risk_type not in {"", "none"}:
        return "medium"
    return "low"


def _next_available_path(base_dir: Path, filename: str) -> Path:
    candidate = base_dir / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        alt = base_dir / f"{stem}-{counter}{suffix}"
        if not alt.exists():
            return alt
        counter += 1


def write_daily_inbox_item(
    coverage_key: str,
    name: str,
    brief: dict,
    analyzed: list[dict],
    output_dir: Path | None = None,
) -> Path:
    out_dir = output_dir or INBOX_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ticker = _coverage_primary_ticker(coverage_key)
    headline = _clean_text(brief.get("headline")) or f"{name} material update"
    slug = _slugify(headline, fallback=_slugify(name))
    filename = f"{today}_{ticker}_commentary_{slug}.md"
    out_path = _next_available_path(out_dir, filename)

    tickers_mentioned: list[str] = []
    if ticker not in {"SECTOR", "NOTE"}:
        tickers_mentioned.append(ticker)

    tags = [
        "daily-brief",
        coverage_key.split("/", 1)[0],
        _slugify(name, fallback="coverage", max_len=24),
    ]
    impact_tags = [_slugify(str(item.get("impact_type", "update")), fallback="update", max_len=24) for item in analyzed]
    tags.extend(tag for tag in impact_tags if tag)

    frontmatter = "\n".join([
        "---",
        "type: commentary",
        f"ticker: {ticker}",
        f"tickers_mentioned: {_inline_yaml_list(tickers_mentioned)}",
        "source: OpenClaw daily brief",
        f"priority: {_daily_priority(analyzed)}",
        f"tags: {_inline_yaml_list(tags)}",
        "status: inbox",
        f"date: {today}",
        "---",
        "",
    ])

    lines = [frontmatter, f"# {name} daily brief", ""]
    if headline:
        lines.extend(["## TL;DR", headline, ""])

    thesis = _clean_text(brief.get("thesis_line"))
    if thesis:
        lines.extend(["## Thesis / answer", thesis, ""])

    what_changed = _clean_text(brief.get("what_changed"))
    if what_changed:
        lines.extend(["## What changed", what_changed, ""])

    key_debate = _clean_text(brief.get("key_debate"))
    if key_debate:
        lines.extend(["## Key debate", key_debate, ""])

    watchpoints = [str(item).strip() for item in (brief.get("watchpoints") or []) if str(item).strip()]
    if watchpoints:
        lines.append("## Watchpoints")
        lines.extend(f"- {item}" for item in watchpoints[:6])
        lines.append("")

    lines.append("## Supporting developments")
    for item in analyzed:
        direction = str(item.get("direction", "neutral")).upper()
        impact = str(item.get("impact_type", "update")).upper()
        title = _clean_text(item.get("title")) or _clean_text(item.get("event")) or "Untitled development"
        lines.append(f"### [{impact} | {direction}] {title}")
        event = _clean_text(item.get("event"))
        kpi = _clean_text(item.get("kpi_node"))
        why = _clean_text(item.get("why_it_matters"))
        thesis_link = _clean_text(item.get("thesis_link"))
        watch = _clean_text(item.get("watch_next"))
        source = _clean_text(item.get("source")) or "Unknown"
        url = _clean_text(item.get("url")) or "N/A"
        if event:
            lines.append(f"- Event: {event}")
        if kpi:
            lines.append(f"- KPI node: {kpi}")
        if why:
            lines.append(f"- Why it matters: {why}")
        if thesis_link:
            lines.append(f"- Thesis link: {thesis_link}")
        if watch:
            lines.append(f"- Watch: {watch}")
        lines.append(f"- Source: {source}")
        lines.append(f"- URL: {url}")
        lines.append("")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    log.info("Inbox item written: %s", out_path)
    return out_path


def write_macro_inbox_item(
    summary: dict,
    articles: list[dict],
    mode: str,
    output_dir: Path | None = None,
) -> Path:
    out_dir = output_dir or INBOX_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    headline = _clean_text(summary.get("headline")) or f"Macro {mode} update"
    slug = _slugify(f"{mode}-{headline}", fallback=f"macro-{mode}")
    filename = f"{today}_MACRO_commentary_{slug}.md"
    out_path = _next_available_path(out_dir, filename)

    affected_names = [str(item).strip() for item in (summary.get("affected_names") or []) if str(item).strip()]
    tags = ["macro", _slugify(mode, fallback="session", max_len=16)]
    risk_type = _slugify(str(summary.get("risk_type", "none")), fallback="none", max_len=24)
    if risk_type and risk_type != "none":
        tags.append(risk_type)
    tags.extend(_slugify(name, fallback="name", max_len=20) for name in affected_names)

    frontmatter = "\n".join([
        "---",
        "type: commentary",
        "ticker: MACRO",
        f"tickers_mentioned: {_inline_yaml_list(affected_names)}",
        "source: OpenClaw macro synthesis",
        f"priority: {_macro_priority(summary)}",
        f"tags: {_inline_yaml_list(tags)}",
        "status: inbox",
        f"date: {today}",
        "---",
        "",
    ])

    lines = [frontmatter, f"# Macro {mode} update", ""]
    lines.extend(["## TL;DR", headline, ""])

    for key, label in [
        ("rates", "Rates & yields"),
        ("fx", "FX"),
        ("equities", "Equities"),
        ("commodities", "Commodities"),
        ("credit", "Credit / risk"),
        ("portfolio_watch", "Portfolio watch"),
    ]:
        text = _clean_text(summary.get(key))
        if text:
            lines.extend([f"## {label}", text, ""])

    themes = [str(item).strip() for item in (summary.get("themes") or []) if str(item).strip()]
    if themes:
        lines.append("## Themes")
        lines.extend(f"- {item}" for item in themes[:6])
        lines.append("")

    lines.append("## Source headlines")
    for article in articles[:12]:
        title = _clean_text(article.get("title")) or "Untitled headline"
        source = _clean_text(article.get("source")) or "Unknown"
        url = _clean_text(article.get("url")) or "N/A"
        lines.append(f"- {title} | {source} | {url}")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    log.info("Inbox item written: %s", out_path)
    return out_path
