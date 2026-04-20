# X API migration draft for `twitter_signal.py`

## TL;DR

Replace the current `twitterapi.io` dependency with X's official API.

Recommended path:
1. **Phase 1, ship first:** keep the current scheduled polling model, but swap fetches to X official **user timeline** endpoints.
2. **Phase 2, optional:** move the watchlist into one curated **X List** to reduce request count and simplify source management.
3. **Not recommended as the first migration:** a single recent-search query, because the current account universe is too large for the query-length limit.

## Why change

Current implementation in `scripts/twitter_signal.py`:
- fetches recent tweets from `twitterapi.io`
- does one request per watched handle
- sleeps between requests because of vendor burst limits
- then runs LLM triage and posts the best items to Discord

Relevant current behavior:
- 68 watched handles across `investing` and `tech`
- exact run windows driven by `--since-ts`
- per-handle fetch, then Haiku triage, then top-N Discord post selection

This is workable, but the transport layer is third-party and brittle. The official API should be more stable, more transparent, and easier to reason about long term.

## What the official X API can cover

### Option A, recommended first: per-user timelines

Use:
- `GET /2/users/by/username/:username`
- `GET /2/users/:id/tweets`

Why this is the best first migration:
- closest to current architecture
- preserves the exact scheduled window model already used by cron
- requires the smallest code delta
- supports app-only bearer auth for reads
- supports `exclude=retweets,replies`
- supports `since_id`, `until_id`, `start_time`, `end_time`
- supports up to 100 posts per request

This is the cleanest drop-in replacement for the current `fetch_tweets()` function.

### Option B, recommended second: one curated X List

Use:
- `GET /2/lists/:id/tweets`
- optionally `GET /2/lists/:id/members`
- optionally `POST /2/lists/:id/members` and `DELETE /2/lists/:id/members/:user_id`

Why it is attractive:
- much lower request count
- a better abstraction for a curated feed
- easier source management once the list exists

Why it is not the best first move:
- it changes the unit of ingestion from per-user to aggregated feed
- it pushes more responsibility onto local dedupe and last-seen tracking
- list member management requires user-context auth if we want to automate membership changes

Recommendation: use Lists only after the direct user-timeline migration works.

### Option C, possible but not the best fit: search or filtered stream

Recent search is not a good fit for the current watch universe.

From the current 68-handle set:
- one combined `from:` query is about **1,388 chars**
- recent search query limit is **512 chars**
- filtered-stream rule length is **1,024 chars**

Implications:
- one recent-search query cannot cover the current source set
- one filtered-stream rule also cannot cover the full source set
- the source set can fit into **2 filtered-stream rules** if we want a streaming design later

Filtered stream is interesting for near-real-time ingestion, but it is a different operating model and should not be the first migration.

## Recommended target design

### Phase 1 architecture

Keep the pipeline shape the same:

`X API user timelines -> normalize -> LLM relevance filter -> top-N selection -> Discord`

Only replace the transport layer.

### Request flow

#### 1. Resolve and cache user IDs
At startup or via a periodic refresh job:
- map each username to X user ID using `GET /2/users/by/username/:username`
- store the cache locally, for example in:
  - SQLite table, or
  - JSON file under `scripts/` or `data/`

Why:
- timeline endpoint is user-ID based
- usernames can change, but rarely
- we should avoid paying repeated lookup cost every run

Suggested cache fields:
- `username`
- `user_id`
- `resolved_at`
- `last_success_at`
- `status`

#### 2. Fetch timeline posts per account
For each cached user ID, call:
- `GET /2/users/:id/tweets`

Suggested parameters:
- `exclude=retweets,replies`
- `max_results=20` for first parity, or `100` if we want safer coverage
- `start_time` based on current run window, or `since_id` if we adopt per-account state
- `tweet.fields=created_at,public_metrics,referenced_tweets,entities,lang,author_id`

#### 3. Normalize into current internal shape
Convert the X response into the current fields expected by the rest of the script.

Current script mainly needs:
- `id`
- `text`
- a retweet signal
- author handle for display

Suggested normalized internal object:

```python
{
  "id": post["id"],
  "text": post.get("text", ""),
  "created_at": post.get("created_at"),
  "author_handle": handle,
  "isRetweet": any(ref.get("type") == "retweeted" for ref in post.get("referenced_tweets", [])),
  "public_metrics": post.get("public_metrics", {}),
  "raw": post,
}
```

That minimizes churn in the Haiku triage and Discord formatting layers.

#### 4. Keep current selection logic unchanged
Do not change yet:
- prompt structure
- tag taxonomy
- top-N priority sorting
- Discord posting format

That keeps the migration narrow and makes attribution of regressions much easier.

## Polling state, recommended approach

### Near-term
Use `start_time` derived from the current scheduled run window.

This matches the current `--since-ts` design and is the fastest parity path.

### Better medium-term
Move to per-account `since_id` state.

Why:
- cleaner idempotency
- less overlap across runs
- less sensitivity to clock skew and backfill edge cases
- better behavior if a run is delayed

Suggested state table:
- `username`
- `user_id`
- `last_seen_post_id`
- `last_seen_created_at`
- `updated_at`

Recommendation:
- ship `start_time` first
- move to `since_id` after parity is confirmed

## Rate-limit fit

Official limits appear comfortably above current needs.

Most relevant endpoints:
- `GET /2/users/:id/tweets`: high per-app read capacity
- `GET /2/lists/:id/tweets`: also comfortably above current three-times-daily usage

Current script does 68 per-handle fetches across three scheduled runs, which is operationally tiny relative to documented official limits.

This should let us remove the current vendor-specific 8-second pacing logic and replace it with much lighter backoff handling around 429s.

## Cost view

What is confirmed from the docs:
- X API is now pay-per-usage
- pricing is per endpoint
- exact rates are in the Developer Console
- billable resources are deduplicated within a 24-hour UTC window in most cases

What is not yet confirmed in this draft:
- exact per-request or per-post cost for the endpoints we need

So the migration recommendation is **architecturally yes**, but finance signoff should wait until the Developer Console confirms the exact cost for:
- user lookup by username
- user timeline reads
- list timeline reads, if we later use Lists

## Proposed code changes

### 1. Replace transport constants and auth
Current:
- `TWITTERAPI_BASE = "https://api.twitterapi.io"`
- `TWITTERAPI_IO_KEY`

Proposed:
- `X_API_BASE = "https://api.x.com/2"`
- `X_BEARER_TOKEN`

New helper:

```python
def _x_headers() -> dict:
    token = os.environ.get("X_BEARER_TOKEN", "")
    if not token:
        raise EnvironmentError("X_BEARER_TOKEN not set in environment.")
    return {"Authorization": f"Bearer {token}"}
```

### 2. Add username -> user_id resolver
Suggested functions:
- `load_x_user_cache()`
- `save_x_user_cache()`
- `resolve_user_id(username)`

### 3. Replace `fetch_tweets()` implementation
Current `fetch_tweets(handle, since_ts)` would become:
- resolve `handle -> user_id`
- call `GET /2/users/:id/tweets`
- pass windowing params
- normalize response into existing internal tweet shape

### 4. Keep the rest of the script stable
Do not touch initially:
- account lists
- Haiku filter prompt
- Discord formatting
- channel creation logic
- top-N ranking

## Rollout plan

### Phase 0, prep
- confirm X Developer account access
- confirm pricing in console
- provision bearer token
- add env var to `.env.example`
- decide cache location for user ID map and per-account state

### Phase 1, parity migration
- implement official X user lookup and timeline fetch
- keep current run schedule and triage unchanged
- run side-by-side dry runs against the current vendor for several windows
- compare:
  - raw post counts
  - overlap rate
  - missing important posts
  - latency and failure rate

### Phase 2, harden
- move from `start_time` to `since_id`
- add local user-ID cache refresh policy
- add structured error handling for:
  - 401/403 auth failures
  - 404 suspended or renamed users
  - 429 rate limits
  - partial failures across accounts

### Phase 3, optional optimization
Choose one:
- curated X List feed, if we want fewer calls and simpler source management
- filtered stream, if we want near-real-time ingestion instead of scheduled windows

## Risks and open questions

### 1. Pricing uncertainty
The public docs confirm pay-per-use, but the exact economics for this workflow must be validated in the console before production cutover.

### 2. Protected, suspended, or renamed accounts
We need graceful handling for accounts that:
- no longer exist
- are suspended
- are protected
- changed handle

### 3. Retweet semantics
The current vendor returns `isRetweet` directly. With the official API, we should infer retweets from `referenced_tweets` type.

### 4. Long posts and note-style content
We should verify whether any watched accounts frequently use post shapes whose full content requires additional fields or special handling.

### 5. List operations require more auth if automated
If we later want the script to manage List membership itself, that likely requires user-context auth rather than app-only bearer reads.

## Recommendation

Proceed with a **Phase 1 parity migration** using:
- `GET /2/users/by/username/:username`
- `GET /2/users/:id/tweets`

Do **not** start with recent search.
Do **not** start with filtered stream.
Use a curated X List only as a later optimization after parity is proven.

## Concrete next step

Draft and implement a narrow patch to `scripts/twitter_signal.py` that:
1. adds official X bearer auth
2. resolves and caches user IDs
3. replaces `twitterapi.io` timeline pulls with `GET /2/users/:id/tweets`
4. leaves the triage and Discord logic unchanged
