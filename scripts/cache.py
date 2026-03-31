"""
SQLite article cache — prevents reprocessing the same article twice,
and prevents double-posting to the same channel on the same day.

Tables:
  seen_articles(url, coverage_key, date)                    — per-article dedup
  daily_runs(coverage_key, date)                            — per-channel run dedup
  earnings_runs(coverage_key, period, ...)                  — per-quarter earnings dedup

Entries older than 7 days are pruned automatically on each write (except earnings_runs
which are retained for 1 year as a historical record).

Usage:
    from cache import filter_unseen, mark_seen, posted_today, mark_posted
    from cache import earnings_processed, mark_earnings_processed
"""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "article_cache.db"

log = logging.getLogger(__name__)


def _utc_today() -> str:
    """Use UTC consistently with the rest of the pipeline's event dating."""
    return datetime.now(timezone.utc).date().isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_articles (
            url          TEXT NOT NULL,
            coverage_key TEXT NOT NULL,
            date         TEXT NOT NULL,
            PRIMARY KEY (url, coverage_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_runs (
            coverage_key TEXT NOT NULL,
            date         TEXT NOT NULL,
            PRIMARY KEY (coverage_key, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS earnings_runs (
            coverage_key      TEXT NOT NULL,
            period            TEXT NOT NULL,
            date_processed    TEXT NOT NULL,
            direction         TEXT,
            transcript_source TEXT,
            PRIMARY KEY (coverage_key, period)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchpoints (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            coverage_key    TEXT NOT NULL,
            watchpoint      TEXT NOT NULL,
            source_date     TEXT NOT NULL,
            source_headline TEXT,
            status          TEXT DEFAULT 'open',
            resolved_date   TEXT,
            resolved_by     TEXT,
            created_at      TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS twitter_account_stats (
            handle        TEXT NOT NULL,
            category      TEXT NOT NULL,
            fetched_count INTEGER NOT NULL DEFAULT 0,
            passed_count  INTEGER NOT NULL DEFAULT 0,
            last_run      TEXT NOT NULL,
            PRIMARY KEY (handle)
        )
    """)
    conn.commit()
    return conn


# ── Watchpoint functions ─────────────────────────────────────────────────────

def store_watchpoints(
    coverage_key: str,
    watchpoints: list[str],
    source_date: str,
    source_headline: str = "",
) -> None:
    """Store new watchpoints from a Pass 2 brief. Deduplicates by text."""
    if not watchpoints:
        return
    conn = _connect()
    try:
        # Get existing open watchpoints for this key to avoid dupes
        existing = {
            row[0] for row in conn.execute(
                "SELECT watchpoint FROM watchpoints WHERE coverage_key=? AND status='open'",
                (coverage_key,),
            )
        }
        now = datetime.now(timezone.utc).isoformat()
        new_count = 0
        for wp in watchpoints:
            wp = wp.strip()
            if not wp or wp in existing:
                continue
            conn.execute(
                """INSERT INTO watchpoints
                   (coverage_key, watchpoint, source_date, source_headline, status, created_at)
                   VALUES (?, ?, ?, ?, 'open', ?)""",
                (coverage_key, wp, source_date, source_headline, now),
            )
            new_count += 1
        # Auto-expire old watchpoints
        conn.execute(
            "UPDATE watchpoints SET status='expired' WHERE status='open' AND created_at < date('now', '-30 days')"
        )
        conn.commit()
        if new_count:
            log.debug("Stored %d new watchpoint(s) for %s", new_count, coverage_key)
    finally:
        conn.close()


def get_open_watchpoints(coverage_key: str | None = None) -> list[dict]:
    """Get open watchpoints, optionally filtered by coverage key."""
    conn = _connect()
    try:
        if coverage_key:
            rows = conn.execute(
                "SELECT id, coverage_key, watchpoint, source_date, source_headline "
                "FROM watchpoints WHERE status='open' AND coverage_key=? ORDER BY created_at DESC",
                (coverage_key,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, coverage_key, watchpoint, source_date, source_headline "
                "FROM watchpoints WHERE status='open' ORDER BY created_at DESC",
            ).fetchall()
        return [
            {"id": r[0], "coverage_key": r[1], "watchpoint": r[2],
             "source_date": r[3], "source_headline": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def resolve_watchpoint(watchpoint_id: int, resolved_by: str = "") -> None:
    """Mark a watchpoint as resolved."""
    conn = _connect()
    try:
        today = _utc_today()
        conn.execute(
            "UPDATE watchpoints SET status='resolved', resolved_date=?, resolved_by=? WHERE id=?",
            (today, resolved_by, watchpoint_id),
        )
        conn.commit()
    finally:
        conn.close()


def earnings_processed(coverage_key: str, period: str) -> bool:
    """Return True if this earnings quarter has already been processed."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM earnings_runs WHERE coverage_key=? AND period=?",
            (coverage_key, period),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_earnings_processed(
    coverage_key: str,
    period: str,
    direction: str = "",
    transcript_source: str = "",
) -> None:
    """Record that an earnings quarter has been fully processed."""
    today = _utc_today()
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO earnings_runs
               (coverage_key, period, date_processed, direction, transcript_source)
               VALUES (?, ?, ?, ?, ?)""",
            (coverage_key, period, today, direction, transcript_source),
        )
        # Retain earnings records for 1 year (365 days)
        conn.execute("DELETE FROM earnings_runs WHERE date_processed < date('now', '-365 days')")
        conn.commit()
    finally:
        conn.close()


def posted_today(coverage_key: str) -> bool:
    """Return True if a successful post was already made today for this coverage key."""
    today = _utc_today()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM daily_runs WHERE coverage_key=? AND date=?",
            (coverage_key, today),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_posted(coverage_key: str) -> None:
    """Record that a successful post was made today for this coverage key."""
    today = _utc_today()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO daily_runs (coverage_key, date) VALUES (?, ?)",
            (coverage_key, today),
        )
        conn.execute("DELETE FROM daily_runs WHERE date < date('now', '-7 days')")
        conn.commit()
    finally:
        conn.close()


def filter_unseen(articles: list[dict], coverage_key: str) -> list[dict]:
    """Return only articles not yet processed in the last 3 days for this coverage key.

    Previously checked only today's date, which let the same article re-enter
    the next day from a different source.  Now checks the full 3-day window so
    day-over-day duplicates are caught.
    """
    if not articles:
        return []

    conn = _connect()
    try:
        urls = {a["url"] for a in articles}
        placeholders = ",".join("?" * len(urls))
        seen_urls = {
            row[0] for row in conn.execute(
                f"SELECT url FROM seen_articles WHERE coverage_key=? AND date >= date('now', '-3 days') AND url IN ({placeholders})",
                [coverage_key, *urls],
            )
        }
        unseen = [a for a in articles if a["url"] not in seen_urls]
        log.debug("%s: %d/%d articles are new (cache filtered %d)", coverage_key, len(unseen), len(articles), len(articles) - len(unseen))
        return unseen
    finally:
        conn.close()


def mark_seen(articles: list[dict], coverage_key: str) -> None:
    """Record articles as processed. Prunes entries older than 7 days."""
    if not articles:
        return

    today = _utc_today()
    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO seen_articles (url, coverage_key, date) VALUES (?, ?, ?)",
            [(a["url"], coverage_key, today) for a in articles],
        )
        conn.execute("DELETE FROM seen_articles WHERE date < date('now', '-7 days')")
        conn.commit()
    finally:
        conn.close()


# ── Twitter signal stats ──────────────────────────────────────────────────────

def upsert_twitter_stats(handle: str, category: str, fetched: int, passed: int) -> None:
    """Upsert per-account stats for the twitter signal feed."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO twitter_account_stats (handle, category, fetched_count, passed_count, last_run)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(handle) DO UPDATE SET
                 category      = excluded.category,
                 fetched_count = fetched_count + excluded.fetched_count,
                 passed_count  = passed_count  + excluded.passed_count,
                 last_run      = excluded.last_run""",
            (handle, category, fetched, passed, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_twitter_leaderboard(limit: int = 10) -> list[dict]:
    """Return top accounts by pass rate (passed / fetched), descending."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT handle, category, fetched_count, passed_count, last_run
               FROM twitter_account_stats
               WHERE fetched_count > 0
               ORDER BY CAST(passed_count AS REAL) / fetched_count DESC,
                        passed_count DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "handle":        r[0],
                "category":      r[1],
                "fetched_count": r[2],
                "passed_count":  r[3],
                "last_run":      r[4],
                "pass_rate":     r[3] / r[2] if r[2] > 0 else 0.0,
            }
            for r in rows
        ]
    finally:
        conn.close()


def reset_twitter_weekly_stats() -> None:
    """Zero out weekly counters after the weekly digest is posted."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE twitter_account_stats SET fetched_count = 0, passed_count = 0"
        )
        conn.commit()
    finally:
        conn.close()
