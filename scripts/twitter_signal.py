"""
Twitter signal curation — fetches recent tweets from a curated account list,
filters with Haiku for investing relevance, posts to #twitter-signal on Discord.

Accounts are grouped into two categories (investing, tech) with category-specific
filter thresholds applied inside the Haiku prompt.

Schedule (HKT):
  09:15 — Morning brief: covers 23:00 prior night → 09:15 (US overnight)
  17:00 — Asia close: covers 09:15 → 17:00 (Asia session)
  23:00 — End of day: covers 17:00 → 23:00 (US session open/afternoon)

Each run uses an exact lookback window based on --since-ts (Unix timestamp).
Cron passes --since-ts explicitly so each run covers exactly its window.

Manual:
  python scripts/twitter_signal.py                    # uses TWITTER_SIGNAL_LOOKBACK_HOURS fallback
  python scripts/twitter_signal.py --dry-run
  python scripts/twitter_signal.py --since-ts 1712000000
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure scripts/ is on sys.path so bare imports (utils, config, etc.) work
# whether this module is run directly or imported as scripts.twitter_signal.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(_SCRIPTS_DIR.parent / ".env")
except ImportError:
    pass

from utils import setup_logging
log = setup_logging("twitter_signal")
import requests

from config import (
    GUILD_ID,
    TWITTER_SIGNAL_MAX_PER_RUN,
    TWITTER_SIGNAL_LOOKBACK_HOURS,
)
from post_discord import (
    get_guild_channels,
    create_category,
    create_text_channel,
    send_text,
)
from cache import upsert_twitter_stats
from utils import retry
from bounded_router import BoundedRoutingError, run_json_task

CHANNEL_MAP_PATH = Path(__file__).parent / "channel_map.json"
TWITTERAPI_BASE  = "https://api.twitterapi.io"
CHANNEL_KEY      = "special/twitter-signal"

ACCOUNTS: dict[str, list[str]] = {
    "investing": [
        "value_invest12", "markoinny", "neilksethi", "citrini", "Rory_Johnston",
        "KobeissiLetter", "abcampbell", "borrowed_ideas", "KarelMercx", "marketplunger1",
        "sadandlonely_69", "buccocapital", "JavierBlas", "HayekAndKeynes", "LukeGromen",
        "LynAldenContact", "SCapStrategist", "scuttleblurb", "riteshmjn", "BobEUnlimited",
        "Brad_Setser", "biancoresearch", "RayDalio", "TMTLongShort", "MetacriticCap",
        "michaeljmcnair", "gave_vincent", "GavinSBaker", "AltimeterCap", "MacroCharts",
        "pmje73", "Ole_S_Hansen", "pati_marins64", "shanaka86", "niubi", "ErikSTownsend",
        "BudaghyanArthur", "leopoldasch", "bondesnmoney", "ResearchQf", "pradeeepk",
        "CapitalValor", "TMTBreakout", "TimmerFidelity", "MichaelKantro", "PeterBerezinBCA",
        "michaelxpettis", "Trinhomics", "anasalhajji", "FundamentEdge", "Geo_papic",
        "LT3000Lyall", "compound248", "DynamicMoats",
    ],
    "tech": [
        "aakashgupta", "SemiAnalysis_", "dwarkesh_sp", "emollick", "RubenHassid",
        "AnthropicAI", "claudeai", "mstockton", "AndrewYNg", "karpathy",
        "viratt", "sama", "satyanadella", "dylan522p",
    ],
}

# Tag priority for selecting top N when more than MAX pass
TAG_PRIORITY = {"BREAKING": 0, "MACRO": 1, "IDEA": 2, "INSIGHT": 3, "WISDOM": 4}


def _twitterapi_headers() -> dict:
    key = os.environ.get("TWITTERAPI_IO_KEY", "")
    if not key:
        raise EnvironmentError("TWITTERAPI_IO_KEY not set in environment.")
    return {"X-API-Key": key}


# ── Channel setup ─────────────────────────────────────────────────────────────

def _load_channel_map() -> dict[str, str]:
    if not CHANNEL_MAP_PATH.exists():
        return {}
    return json.loads(CHANNEL_MAP_PATH.read_text())


def _save_channel_map(channel_map: dict[str, str]) -> None:
    CHANNEL_MAP_PATH.write_text(json.dumps(channel_map, indent=2))


def ensure_twitter_channel() -> str:
    """Return channel ID for #twitter-signal, creating it if needed."""
    channel_map = _load_channel_map()
    if CHANNEL_KEY in channel_map:
        return channel_map[CHANNEL_KEY]

    log.info("twitter-signal channel not in channel_map.json — creating it")
    existing  = get_guild_channels()
    ex_cats   = {c["name"].upper(): c for c in existing if c["type"] == 4}
    ex_chans  = {c["name"]:         c for c in existing if c["type"] == 0}

    cat_name = "SIGNALS"
    if cat_name in ex_cats:
        cat_id = ex_cats[cat_name]["id"]
    else:
        cat = create_category(cat_name)
        cat_id = cat["id"]

    ch_name = "twitter-signal"
    if ch_name in ex_chans:
        ch_id = ex_chans[ch_name]["id"]
    else:
        ch = create_text_channel(ch_name, cat_id)
        ch_id = ch["id"]

    channel_map[CHANNEL_KEY] = ch_id
    _save_channel_map(channel_map)
    log.info("Saved twitter-signal channel ID %s to channel_map.json", ch_id)
    return ch_id


# ── Tweet fetching ────────────────────────────────────────────────────────────

@retry(max_attempts=3, backoff=2.0, exceptions=(requests.RequestException,))
def fetch_tweets(handle: str, since_ts: int | None = None) -> list[dict]:
    """Fetch recent tweets for a single account since since_ts (Unix seconds).
    Falls back to TWITTER_SIGNAL_LOOKBACK_HOURS if since_ts is not provided."""
    if since_ts is None:
        since_ts = int(datetime.now(timezone.utc).timestamp()) - TWITTER_SIGNAL_LOOKBACK_HOURS * 3600
    url    = f"{TWITTERAPI_BASE}/twitter/user/last_tweets"
    params = {"userName": handle, "sinceTime": since_ts}

    try:
        resp = requests.get(
            url,
            headers=_twitterapi_headers(),
            params=params,
            timeout=15,
        )
    except requests.RequestException as e:
        log.warning("fetch_tweets(%s): request error — %s", handle, e)
        return []

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 5))
        log.warning("fetch_tweets(%s): 429 rate limit — sleeping %ds", handle, retry_after)
        time.sleep(retry_after + 1)
        return []

    if resp.status_code != 200:
        log.warning("fetch_tweets(%s): HTTP %d — skipping", handle, resp.status_code)
        return []

    try:
        data = resp.json()
    except Exception as e:
        log.warning("fetch_tweets(%s): JSON parse error — %s", handle, e)
        return []

    tweets = data.get("data", {}).get("tweets", [])
    if tweets is None:
        tweets = []
    # Keep max 20, skip pure retweets (retweets with no additional comment)
    result = []
    for t in tweets[:20]:
        if t.get("isRetweet", False):
            # Keep if the tweet text extends significantly beyond "RT @..."
            text = t.get("text", "")
            rt_idx = text.find("RT @")
            if rt_idx != -1:
                # Check if there's a substantial prefix comment (>20 chars before RT)
                prefix = text[:rt_idx].strip()
                if len(prefix) < 20:
                    continue
        result.append(t)

    return result


# ── Haiku filter ──────────────────────────────────────────────────────────────

@retry(max_attempts=3, backoff=5.0, exceptions=(BoundedRoutingError,))
def filter_tweets_haiku(handle: str, category: str, tweets: list[dict]) -> list[dict]:
    """
    Run a single Haiku pass over one account's tweets.
    Returns the kept tweets, each augmented with a "tag" field.
    """
    if not tweets:
        return []

    # Build tweet list for the prompt
    tweet_lines = []
    for i, t in enumerate(tweets):
        text = t.get("text", "").replace("\n", " ").strip()
        tweet_lines.append(f"[{i}] {text}")
    tweets_block = "\n".join(tweet_lines)

    prompt = f"""You are an experienced buy-side investor curating a very high-signal daily feed.
Account: @{handle} (category: {category})

Only keep tweets that clear a HIGH bar and would be worth sending in a once-daily PM brief.

Keep only tweets that are one of:
- Breaking market news or important macro/geopolitical developments with portfolio implications
- Non-obvious data points or observations that could change odds on a thesis
- Actionable investing ideas or industry/company developments that matter to valuation, KPIs, or positioning
- Genuine mental models / investing wisdom worth saving

Exclude aggressively:
- Retweets of obvious news
- Generic market commentary or color
- Interesting but non-actionable threads
- AI/tech chatter without direct investing relevance
- Hot takes without supporting data or reasoning
- Self-promotion, engagement bait, or thread hooks
- Content a well-informed PM would already know

For tech/AI accounts: keep only if it has direct investing relevance
(model economics, capex implications, infrastructure costs, competitive positioning, or specific company impact).

Tweets:
{tweets_block}

Return ONLY a JSON array (no prose). For each kept tweet include:
- index: the tweet index number
- tag: one of BREAKING|MACRO|INSIGHT|IDEA|WISDOM
- context: 1-2 sentences explaining why this matters to a portfolio manager (what it changes, what it affects, why it is non-obvious or timely). Be specific — name the asset class, sector, KPI, or ticker affected if clear.

Important:
- Be selective. It is better to return [] than include marginal content.
- Prefer quality over quantity.
- Assume only the top few tweets across all accounts will survive.

Example:
[{{"index": 0, "tag": "MACRO", "context": "Signals Fed may pause longer than priced — bearish for rate-sensitive longs, supportive for short-duration. Watch JPM/BAC NII guidance sensitivity."}}]

If no tweets pass, return an empty array: []"""

    try:
        kept_meta, route = run_json_task(
            task_class="tweet_triage",
            prompt=prompt,
            expected="array",
            max_tokens=512,
        )
    except Exception as e:
        log.warning("filter_tweets_haiku(%s): bounded routing failed — %s", handle, e)
        return []

    log.info("filter_tweets_haiku(%s): route %s via %s", handle, route.tier, route.model)

    kept = []
    for item in kept_meta:
        idx = item.get("index")
        tag = item.get("tag", "INSIGHT").upper()
        if tag not in TAG_PRIORITY:
            tag = "INSIGHT"
        if isinstance(idx, int) and 0 <= idx < len(tweets):
            tweet = dict(tweets[idx])
            tweet["_tag"] = tag
            tweet["_context"] = item.get("context", "").strip()
            kept.append(tweet)

    return kept


# ── Discord formatting ────────────────────────────────────────────────────────

def _format_tweet(tweet: dict, handle: str, category: str) -> str:
    """Format a single kept tweet as a plain-text Discord message."""
    tag      = tweet.get("_tag", "INSIGHT")
    text     = tweet.get("text", "").strip()
    context  = tweet.get("_context", "").strip()
    tweet_id = tweet.get("id", "")

    # Truncate tweet text to 280 chars
    if len(text) > 280:
        text = text[:277] + "..."

    url = f"https://x.com/{handle}/status/{tweet_id}" if tweet_id else ""

    lines = [f"[{tag}] @{handle} ({category})", text]
    if context:
        lines.append(f"> {context}")
    if url:
        lines.append(url)
    return "\n".join(lines)


def _select_top(kept: list[tuple[str, str, dict]], max_count: int) -> list[tuple[str, str, dict]]:
    """Select up to max_count items, prioritising by tag priority then order."""
    if len(kept) <= max_count:
        return kept
    return sorted(kept, key=lambda x: TAG_PRIORITY.get(x[2].get("_tag", "INSIGHT"), 99))[:max_count]


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, since_ts: int | None = None) -> None:
    channel_id = None if dry_run else ensure_twitter_channel()

    # Log the effective window being covered
    if since_ts is not None:
        window_start = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log.info("Coverage window: %s → now", window_start)

    all_kept: list[tuple[str, str, dict]] = []  # (handle, category, tweet)

    # Flatten all accounts with their category, process in batches of 5
    all_accounts: list[tuple[str, str]] = []
    for category, handles in ACCOUNTS.items():
        for handle in handles:
            all_accounts.append((handle, category))

    BATCH_SIZE = 5
    INTER_FETCH_DELAY = 8.0  # seconds between individual fetches — twitterapi.io burst limit
    for batch_start in range(0, len(all_accounts), BATCH_SIZE):
        batch = all_accounts[batch_start : batch_start + BATCH_SIZE]
        for i, (handle, category) in enumerate(batch):
            if i > 0:
                time.sleep(INTER_FETCH_DELAY)
            try:
                tweets = fetch_tweets(handle, since_ts=since_ts)
                if not tweets:
                    log.debug("%s: no tweets in lookback window", handle)
                    upsert_twitter_stats(handle, category, 0, 0)
                    continue

                log.info("%s: fetched %d tweet(s)", handle, len(tweets))
                kept = filter_tweets_haiku(handle, category, tweets)
                log.info("%s: %d/%d passed filter", handle, len(kept), len(tweets))

                upsert_twitter_stats(handle, category, len(tweets), len(kept))

                for t in kept:
                    all_kept.append((handle, category, t))

            except Exception as e:
                log.warning("Error processing @%s: %s — skipping", handle, e)
                continue

        # Pause between batches
        if batch_start + BATCH_SIZE < len(all_accounts):
            time.sleep(3)

    if not all_kept:
        log.info("No tweets passed filter this run.")
        return

    top = _select_top(all_kept, TWITTER_SIGNAL_MAX_PER_RUN)
    log.info("Posting %d tweet(s) (of %d that passed)", len(top), len(all_kept))

    for handle, category, tweet in top:
        msg = _format_tweet(tweet, handle, category)
        if dry_run:
            print(msg)
            print()
        else:
            try:
                send_text(channel_id, msg)
                time.sleep(0.5)  # avoid Discord rate limits between posts
            except Exception as e:
                log.warning("Failed to post tweet from @%s: %s", handle, e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Twitter signal curation feed")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and filter but do not post to Discord")
    parser.add_argument("--since-ts", type=int, default=None,
                        help="Fetch tweets since this Unix timestamp (seconds). "
                             "Overrides TWITTER_SIGNAL_LOOKBACK_HOURS. Pass from cron to "
                             "ensure exact window coverage with no overlap or gap.")
    args = parser.parse_args()
    run(dry_run=args.dry_run, since_ts=args.since_ts)
