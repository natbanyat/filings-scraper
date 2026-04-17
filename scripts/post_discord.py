"""
Discord HTTP API wrapper — channel setup and message posting.

Uses the Discord REST API directly (no discord.py dependency needed).

Changes from v1:
- retry decorator on all network calls
- structured logging (no print statements)
- rate-limit awareness (429 → retry after Retry-After header)
"""

import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

from config import GUILD_ID
from utils import retry

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

CHANNEL_TYPE_TEXT     = 0
CHANNEL_TYPE_CATEGORY = 4

DIRECTION_COLOR = {
    "bull":    0x2ECC71,  # green
    "bear":    0xE74C3C,  # red
    "neutral": 0x95A5A6,  # grey
    "mixed":   0xE67E22,  # orange
}

DIRECTION_EMOJI = {
    "bull":    "🟢",
    "bear":    "🔴",
    "neutral": "🟡",
    "mixed":   "🟠",
}

DIRECTION_LABEL = {
    "bull":    "BULL",
    "bear":    "BEAR",
    "neutral": "NEUTRAL",
    "mixed":   "MIXED",
}

IMPACT_LABEL = {
    "view-changing": "VIEW-CHANGING",
    "confirming": "CONFIRMING",
    "incremental": "INCREMENTAL",
}


def _headers() -> dict:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise EnvironmentError("DISCORD_BOT_TOKEN not set in environment.")
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """
    Make a Discord API request with rate-limit handling.
    Retries once automatically on 429 (respects Retry-After header).
    """
    resp = requests.request(method, url, headers=_headers(), timeout=15, **kwargs)
    if resp.status_code == 429:
        retry_after = float(resp.json().get("retry_after", 1.0))
        log.warning("Discord rate limited — waiting %.1fs", retry_after)
        time.sleep(retry_after + 0.1)
        resp = requests.request(method, url, headers=_headers(), timeout=15, **kwargs)
    resp.raise_for_status()
    return resp


# ── Channel management ────────────────────────────────────────────────────────

@retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
def get_guild_channels() -> list[dict]:
    resp = _request("GET", f"{DISCORD_API}/guilds/{GUILD_ID}/channels")
    return resp.json()


@retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
def create_category(name: str) -> dict:
    resp = _request("POST", f"{DISCORD_API}/guilds/{GUILD_ID}/channels",
                    json={"name": name, "type": CHANNEL_TYPE_CATEGORY})
    log.info("Created Discord category: %s", name)
    return resp.json()


@retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
def create_text_channel(name: str, category_id: str) -> dict:
    resp = _request("POST", f"{DISCORD_API}/guilds/{GUILD_ID}/channels",
                    json={"name": name, "type": CHANNEL_TYPE_TEXT, "parent_id": category_id})
    log.info("Created Discord channel: #%s", name)
    return resp.json()


# ── Message formatting ────────────────────────────────────────────────────────

def _dominant_direction(articles: list[dict]) -> str:
    counts = {"bull": 0, "bear": 0, "neutral": 0}
    for a in articles:
        d = a.get("direction", "neutral").lower()
        counts[d] = counts.get(d, 0) + 1
    # Use weighted dominance: only call "top" if it holds >50% of articles
    total = sum(counts.values())
    top = max(counts, key=counts.get)
    return top if total > 0 and counts[top] / total > 0.5 else "mixed"


def _clean_article_url(article: dict) -> tuple[str, str]:
    """Return (display_label, url) for an article, replacing raw Finnhub API URLs."""
    url = article.get("url", "")
    source = article.get("source", "")
    if "finnhub.io/api" in url:
        # Raw API endpoint — link to publisher homepage instead
        if source:
            return source, f"https://{source}"
        return "source", "#"
    return source or "source", url or "#"


def _normalize_text(value) -> str:
    """Normalize model output into a display-safe string."""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(parts)
    return str(value).strip()


def _split_bullets(text) -> list[str]:
    """Split a bullet-list string or list into individual bullet strings."""
    if isinstance(text, list):
        return [str(item).strip() for item in text if str(item).strip()]

    normalized = _normalize_text(text)
    if not normalized:
        return []

    lines = normalized.split("\n")
    bullets: list[str] = []
    for line in lines:
        line = line.strip()
        if line.startswith(("• ", "- ", "* ")):
            bullets.append(line[2:].strip())
        elif line and re.match(r"^\d+\.\s", line):
            bullets.append(re.sub(r"^\d+\.\s*", "", line).strip())
        elif line:
            bullets.append(line)
    return [b for b in bullets if b]


def build_embed(name: str, brief: dict, analyzed_articles: list[dict], close_window: str = "") -> dict:
    direction = str(brief.get("overall_direction", "")).lower()
    if direction not in DIRECTION_COLOR:
        direction = _dominant_direction(analyzed_articles)
    color  = DIRECTION_COLOR.get(direction, DIRECTION_COLOR["mixed"])
    emoji  = DIRECTION_EMOJI.get(direction, "🟠")

    title = f"{emoji} {name.upper()} — Daily Briefing"
    description = _normalize_text(brief.get("headline")) or f"{len(analyzed_articles)} material item(s) flagged"
    n_dev = len(analyzed_articles)
    footer_parts = ["investing-agent", f"{n_dev} development{'s' if n_dev != 1 else ''}"]
    if close_window:
        footer_parts.append(close_window.upper().replace("_", " "))
    footer_text = " | ".join(footer_parts)

    # Discord hard limits: field name ≤256, field value ≤1024, total embed ≤6000 chars.
    TOTAL_LIMIT     = 6000
    FIELD_NAME_MAX  = 256
    FIELD_VALUE_MAX = 1024
    used = len(title) + len(description) + len(footer_text)

    fields = []

    # Build summary fields — "What Matters Now" is split into per-bullet fields (max 3)
    what_changed = _normalize_text(brief.get("what_changed"))
    wc_bullets = _split_bullets(what_changed)[:3] if what_changed else []

    base_summary_fields: list[tuple[str, str]] = [
        ("Thesis / Answer", _normalize_text(brief.get("thesis_line"))),
    ]
    if len(wc_bullets) > 1:
        for bullet in wc_bullets:
            base_summary_fields.append(("What Matters Now", bullet))
    elif what_changed:
        base_summary_fields.append(("What Matters Now", what_changed))

    base_summary_fields.append(("Key Debate", _normalize_text(brief.get("key_debate"))))

    watchpoints = [w for w in brief.get("watchpoints", []) if isinstance(w, str) and w.strip()]
    if watchpoints:
        base_summary_fields.append(("Watchpoints", "\n".join(f"• {item}" for item in watchpoints[:4])))

    for label, text in base_summary_fields:
        if not text:
            continue
        field_name  = label[:FIELD_NAME_MAX]
        field_value = text[:FIELD_VALUE_MAX]
        if used + len(field_name) + len(field_value) > TOTAL_LIMIT:
            log.warning("build_embed: embed size limit reached before summary field '%s'", label)
            break
        used += len(field_name) + len(field_value)
        fields.append({"name": field_name, "value": field_value, "inline": False})

    for a in analyzed_articles[:4]:
        d_label      = DIRECTION_LABEL.get(a.get("direction", "neutral").lower(), "WATCH")
        impact_label = IMPACT_LABEL.get(a.get("impact_type", "").lower(), "UPDATE")
        raw_name     = f"{impact_label} | {d_label} | {a['title']}"
        display_src, clean_url = _clean_article_url(a)
        raw_value = (
            f"**KPI:** {a.get('kpi_node', '')}\n"
            f"**Why it matters:** {a.get('why_it_matters', '')}\n"
            f"**Thesis link:** {a.get('thesis_link', '')}\n"
            f"**Watch:** {a.get('watch_next', '')}\n"
            f"[{display_src}]({clean_url})"
        )

        field_name  = raw_name[:FIELD_NAME_MAX]
        field_value = raw_value[:FIELD_VALUE_MAX]

        if used + len(field_name) + len(field_value) > TOTAL_LIMIT:
            log.warning(
                "build_embed: embed size limit reached after %d summary/development fields",
                len(fields),
            )
            break

        used += len(field_name) + len(field_value)
        fields.append({"name": field_name, "value": field_value, "inline": False})

    return {
        "title":       title,
        "description": description,
        "color":       color,
        "fields":      fields[:25],
        "footer":      {"text": footer_text},
    }


# ── Send message ──────────────────────────────────────────────────────────────

@retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
def send_embed(channel_id: str, embed: dict) -> None:
    """Post an embed to a channel."""
    _request("POST", f"{DISCORD_API}/channels/{channel_id}/messages", json={"embeds": [embed]})
    log.debug("Embed posted to channel %s", channel_id)


@retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
def send_text(channel_id: str, content: str) -> None:
    """Post plain text, splitting at Discord's 2000-char limit."""
    chunks = [content[i:i+1990] for i in range(0, len(content), 1990)]
    for chunk in chunks:
        _request("POST", f"{DISCORD_API}/channels/{channel_id}/messages", json={"content": chunk})


@retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
def fetch_messages(channel_id: str, limit: int = 50, after: str | None = None) -> list[dict]:
    """Fetch recent messages from a channel.

    Returns list of message dicts (newest first) with keys: id, content, author,
    timestamp. Filters to human messages only (ignores bot posts).

    Args:
        channel_id: Discord channel ID.
        limit: Max messages to fetch (1-100).
        after: Only fetch messages after this message ID (snowflake).
    """
    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    resp = _request("GET", f"{DISCORD_API}/channels/{channel_id}/messages", params=params)
    messages = resp.json()
    # Filter to human messages only (bot=False or missing bot flag)
    human = []
    for m in messages:
        author = m.get("author", {})
        if author.get("bot", False):
            continue
        human.append({
            "id": m["id"],
            "content": m.get("content", ""),
            "author": author.get("username", "unknown"),
            "timestamp": m.get("timestamp", ""),
        })
    return human


def post_run_summary(
    channel_id: str,
    close_window: str,
    coverage_processed: list[str],
    material_names: list[tuple[str, int]],
    no_development_names: list[str],
    total_fetched: int,
    total_passed: int,
    macro_deduped: int,
) -> None:
    """Post a run-completion summary embed to the macro-close channel."""
    fields = []

    if coverage_processed:
        fields.append({
            "name": "Coverage processed",
            "value": ", ".join(coverage_processed),
            "inline": False,
        })

    if material_names:
        value = ", ".join(f"{n} ({c})" for n, c in material_names)
        fields.append({"name": "Material developments", "value": value, "inline": False})

    if no_development_names:
        fields.append({
            "name": "No developments",
            "value": ", ".join(no_development_names),
            "inline": False,
        })

    fields.append({
        "name": "Articles processed",
        "value": f"{total_fetched} fetched \u2192 {total_passed} passed filter",
        "inline": True,
    })

    if macro_deduped > 0:
        fields.append({
            "name": "Macro stories deduped",
            "value": str(macro_deduped),
            "inline": True,
        })

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    embed = {
        "title": f"Run Summary \u2014 {close_window.upper().replace('_', ' ')}",
        "color": 0x5865F2,
        "fields": fields[:25],
        "footer": {"text": f"investing-agent | {now_utc}"},
    }
    send_embed(channel_id, embed)
    log.info("Run summary posted to macro-close (window=%s)", close_window)


def send_briefing(channel_id: str, title: str, sections: list[tuple[str, str]]) -> None:
    """Post a long-form briefing as a sequence of plain-text messages.

    sections: list of (section_header, body_text) tuples posted in order.
    Each section is sent via send_text(), which handles 2000-char chunking.
    """
    send_text(channel_id, f"**{title}**")
    for header, body in sections:
        send_text(channel_id, f"__**{header}**__\n{body}")
