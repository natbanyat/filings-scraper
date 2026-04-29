"""
Official document corpus manager.

Builds a local, file-backed corpus of official company documents for first-look /
deep-dive workflows, plus incremental follow-on runs over time.

Storage layout (default):
  /mnt/c/Users/natba/OneDrive/@ Cowork/openclaw-investing-context/
    _meta/
      corpus.db
    <COMPANY_FOLDER>/
      annual_reports/
      quarterly_reports/
      presentations/
      transcripts/
      major_announcements/
      data_packs/
      other/
      _meta/
        site_learning.md
        site_learning.json
        parses/
          <doc_id>.json

Schema v2 changes (backward compatible):
  documents table: added content_length, etag, last_modified, first_seen_at,
                   download_status, parse_status, parse_quality_flags,
                   delta_state, failure_type, retry_count, retry_after
  New tables: document_parses, jobs

Delta states:
  new              : first time we've seen this URL
  unchanged        : same SHA256 as last download — file not re-saved
  updated          : SHA256 changed — new version saved
  duplicate        : same SHA256 as another document from same company
  moved            : URL changed but title+date point to same document
  failed_download  : all download attempts failed
  failed_parse     : download succeeded but parser returned an error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from config import OFFICIAL_DOC_CORPUS_DIR, TICKER_META
from official_docs_fetcher import fetch_ir_recent_documents, fetch_sec_recent_documents
from official_docs_probe import _infer_storage_patterns
from official_source_adapters import _http_probe, fetch_exchange_documents, probe_exchange_adapter
from transcript_fetcher import HEADERS_EDGAR, HEADERS_WEB
from utils import setup_logging

log = setup_logging("official_doc_corpus")

DB_FILENAME = "corpus.db"
META_DIRNAME = "_meta"
PARSES_DIRNAME = "parses"
DERIVED_DIRNAME = "derived"
MAX_DOWNLOAD_BYTES = 75 * 1024 * 1024
DEFAULT_INCREMENTAL_DAYS = 400
DEFAULT_INCREMENTAL_MAX_DOCS = 25
DEFAULT_BACKFILL_DAYS = 3650
DEFAULT_BACKFILL_MAX_DOCS = 120
DEFAULT_STALE_MINUTES = 180

DOC_FAMILY_DIRS = {
    "annual_report": "annual_reports",
    "quarterly_report": "quarterly_reports",
    "quarterly_filing": "quarterly_reports",
    "presentation": "presentations",
    "investor_day": "presentations",
    "transcript": "transcripts",
    "major_announcement": "major_announcements",
    "filing": "major_announcements",
    "data_pack": "data_packs",
    "other": "other",
    "webcast": "other",
}

CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "application/json": ".json",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
}

PARSEABLE_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".html", ".htm", ".txt"}
PARSEABLE_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/html",
    "application/xhtml+xml",
    "text/plain",
}
DIRECT_HTTP_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".html", ".htm", ".txt", ".json"}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BrowserFallbackRequired(Exception):
    """
    Raised when a source is configured as download_mode=browser but the
    browser automation backend is not available.

    The caller should record download_status='browser_required' and
    delta_state='failed_download' with failure_type='transient'.
    """

    def __init__(self, url: str, source_id: str | None = None) -> None:
        self.url = url
        self.source_id = source_id
        super().__init__(f"browser required for {url}")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CompanySpec:
    company_name: str
    ticker: str | None = None
    coverage_key: str | None = None
    sec_cik: str | None = None
    ir_page: str | None = None
    exchange_adapter: str | None = None
    exchange_symbol: str | None = None
    exchange_code: str | None = None
    exchange_slug: str | None = None
    website_probe_urls: list[str] = field(default_factory=list)
    company_key: str | None = None
    company_folder: str | None = None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def slugify(value: str, *, upper: bool = False, max_len: int = 80) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-") or "company"
    text = text[:max_len].strip("-") or "company"
    return text.upper() if upper else text.lower()


def sanitize_filename(value: str, max_len: int = 140) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value or "document")
    value = re.sub(r"\s+", " ", value).strip(" .") or "document"
    if len(value) <= max_len:
        return value
    stem, dot, suffix = value.rpartition(".")
    if dot:
        keep = max_len - len(suffix) - 1
        return f"{stem[:keep].rstrip(' .')}.{suffix}"
    return value[:max_len].rstrip(" .")


def ensure_corpus_dirs(root: Path) -> tuple[Path, Path]:
    meta_dir = root / META_DIRNAME
    meta_dir.mkdir(parents=True, exist_ok=True)
    return root, meta_dir


def db_path(root: Path) -> Path:
    _, meta_dir = ensure_corpus_dirs(root)
    return meta_dir / DB_FILENAME


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------


def open_db(root: Path) -> sqlite3.Connection:
    path = db_path(root)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    _migrate_schema(conn)
    _backfill_legacy_documents(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables that didn't exist yet. Safe to call repeatedly."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            status TEXT NOT NULL,
            company_key TEXT NOT NULL,
            company_name TEXT NOT NULL,
            company_folder TEXT NOT NULL,
            ticker TEXT,
            coverage_key TEXT,
            mode TEXT NOT NULL,
            days_back INTEGER,
            max_docs INTEGER,
            sec_cik TEXT,
            ir_page TEXT,
            exchange_adapter TEXT,
            exchange_symbol TEXT,
            discovered_count INTEGER DEFAULT 0,
            downloaded_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            progress_message TEXT,
            error_message TEXT,
            payload_json TEXT
        );

        CREATE TABLE IF NOT EXISTS documents (
            doc_id TEXT PRIMARY KEY,
            company_key TEXT NOT NULL,
            company_name TEXT NOT NULL,
            company_folder TEXT NOT NULL,
            ticker TEXT,
            coverage_key TEXT,
            source_url TEXT NOT NULL UNIQUE,
            final_url TEXT,
            source TEXT,
            origin TEXT,
            form_type TEXT,
            doc_type TEXT,
            doc_family TEXT,
            published_at TEXT,
            title TEXT,
            filename TEXT,
            relative_path TEXT,
            absolute_path TEXT,
            content_type TEXT,
            sha256 TEXT,
            size_bytes INTEGER,
            downloaded_at TEXT,
            first_run_id TEXT,
            last_run_id TEXT,
            last_seen_at TEXT,
            status TEXT,
            snippet TEXT,
            metadata_json TEXT
        );

        CREATE TABLE IF NOT EXISTS document_parses (
            parse_id TEXT PRIMARY KEY,
            doc_id TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            parser_name TEXT NOT NULL,
            parse_version INTEGER NOT NULL DEFAULT 1,
            text_chars INTEGER,
            page_count INTEGER,
            table_count INTEGER,
            quality_flags TEXT,
            error TEXT,
            artifact_path TEXT,
            derived_artifact_path TEXT,
            FOREIGN KEY (doc_id) REFERENCES documents(doc_id)
        );

        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            company_key TEXT,
            coverage_key TEXT,
            doc_id TEXT,
            params_json TEXT,
            result_json TEXT,
            error TEXT,
            failure_type TEXT,
            retry_count INTEGER DEFAULT 0,
            retry_after TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_runs_company_created ON runs(company_key, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_docs_company_seen ON documents(company_key, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_docs_family ON documents(company_key, doc_family, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_docs_sha256 ON documents(sha256);
        CREATE INDEX IF NOT EXISTS idx_parses_doc ON document_parses(doc_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_key, job_type, status);
        """
    )
    conn.commit()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """
    Add v2 columns to the documents table if they don't exist yet.
    Safe to run against v1 databases.
    """
    cursor = conn.execute("PRAGMA table_info(documents)")
    existing = {row[1] for row in cursor.fetchall()}

    new_cols: list[tuple[str, str]] = [
        ("content_length", "INTEGER"),
        ("etag", "TEXT"),
        ("last_modified", "TEXT"),
        ("first_seen_at", "TEXT"),
        ("download_status", "TEXT"),
        ("parse_status", "TEXT"),
        ("parse_quality_flags", "TEXT"),
        ("delta_state", "TEXT"),
        ("failure_type", "TEXT"),
        ("retry_count", "INTEGER DEFAULT 0"),
        ("retry_after", "TEXT"),
    ]
    for col_name, col_type in new_cols:
        if col_name not in existing:
            try:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError as exc:
                log.debug("Column migration skipped (%s): %s", col_name, exc)

    parse_cursor = conn.execute("PRAGMA table_info(document_parses)")
    parse_existing = {row[1] for row in parse_cursor.fetchall()}
    if "derived_artifact_path" not in parse_existing:
        try:
            conn.execute("ALTER TABLE document_parses ADD COLUMN derived_artifact_path TEXT")
        except sqlite3.OperationalError as exc:
            log.debug("Parse column migration skipped (derived_artifact_path): %s", exc)
    conn.commit()


def _infer_download_status(*, status: str | None, absolute_path: str | None) -> str:
    if status == "error":
        return "error"
    if absolute_path:
        return "downloaded"
    return status or "unknown"


def _infer_parse_status(
    *,
    absolute_path: str | None,
    filename: str | None,
    content_type: str | None,
    download_status: str | None,
) -> str:
    if download_status not in {"downloaded", "existing"} and not absolute_path:
        return "not_applicable"
    suffix = Path(filename or absolute_path or "").suffix.lower()
    if suffix in PARSEABLE_EXTENSIONS:
        return "unparsed"
    if (content_type or "").lower() in PARSEABLE_CONTENT_TYPES:
        return "unparsed"
    return "not_applicable"


def _infer_delta_state(*, status: str | None, download_status: str | None, absolute_path: str | None) -> str:
    if status == "error" or download_status in {"error", "browser_required"}:
        return "failed_download"
    if absolute_path or download_status in {"downloaded", "existing"}:
        return "unchanged"
    return "new"


def _backfill_legacy_documents(conn: sqlite3.Connection) -> None:
    needs_backfill = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM documents
        WHERE first_seen_at IS NULL
           OR download_status IS NULL
           OR parse_status IS NULL
           OR parse_status = 'not_applicable'
           OR parse_quality_flags IS NULL
           OR delta_state IS NULL
           OR retry_count IS NULL
        """
    ).fetchone()["cnt"]
    if not needs_backfill:
        return

    parse_rows = conn.execute(
        "SELECT doc_id, MAX(CASE WHEN error IS NULL OR error = '' THEN 1 ELSE 0 END) AS has_success FROM document_parses GROUP BY doc_id"
    ).fetchall()
    parse_map = {row["doc_id"]: bool(row["has_success"]) for row in parse_rows}

    rows = conn.execute(
        """
        SELECT doc_id, absolute_path, filename, content_type, status, download_status,
               parse_status, parse_quality_flags, delta_state, first_seen_at,
               downloaded_at, last_seen_at, retry_count
        FROM documents
        WHERE first_seen_at IS NULL
           OR download_status IS NULL
           OR parse_status IS NULL
           OR parse_status = 'not_applicable'
           OR parse_quality_flags IS NULL
           OR delta_state IS NULL
           OR retry_count IS NULL
        """
    ).fetchall()

    for row in rows:
        item = dict(row)
        updates: dict[str, Any] = {}
        absolute_path = item.get("absolute_path") or ""
        inferred_download = item.get("download_status") or _infer_download_status(
            status=item.get("status"),
            absolute_path=absolute_path,
        )

        if item.get("first_seen_at") is None:
            updates["first_seen_at"] = item.get("downloaded_at") or item.get("last_seen_at") or utc_now_iso()
        if item.get("download_status") is None:
            updates["download_status"] = inferred_download
        if item.get("parse_status") is None or item.get("parse_status") == "not_applicable":
            if item["doc_id"] in parse_map:
                updates["parse_status"] = "parsed" if parse_map[item["doc_id"]] else "parse_failed"
            else:
                updates["parse_status"] = _infer_parse_status(
                    absolute_path=absolute_path,
                    filename=item.get("filename"),
                    content_type=item.get("content_type"),
                    download_status=inferred_download,
                )
        if item.get("parse_quality_flags") is None:
            updates["parse_quality_flags"] = "[]"
        if item.get("delta_state") is None:
            updates["delta_state"] = _infer_delta_state(
                status=item.get("status"),
                download_status=inferred_download,
                absolute_path=absolute_path,
            )
        if item.get("retry_count") is None:
            updates["retry_count"] = 0

        if updates:
            fields = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE documents SET {fields} WHERE doc_id = ?",
                (*updates.values(), item["doc_id"]),
            )
    conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _path_exists(path_value: str | None) -> bool:
    return bool(path_value and Path(path_value).exists())


def _path_relative_to_root(path_value: str | None, root: Path) -> str | None:
    if not path_value:
        return None
    try:
        resolved = Path(path_value).resolve()
        root_resolved = root.resolve()
        return str(resolved.relative_to(root_resolved))
    except Exception:
        return None


def _attach_artifact_fields(item: dict, root: Path) -> dict:
    parse_path = item.get("parse_artifact_path") or item.get("artifact_path")
    derived_path = item.get("derived_artifact_path")
    item["parse_artifact_path"] = parse_path
    item["parse_artifact_relative_path"] = _path_relative_to_root(parse_path, root)
    item["parse_artifact_available"] = _path_exists(parse_path)
    item["derived_artifact_path"] = derived_path
    item["derived_artifact_relative_path"] = _path_relative_to_root(derived_path, root)
    item["derived_artifact_available"] = _path_exists(derived_path)
    return item


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def list_runs(root: Path, *, limit: int = 50) -> list[dict]:
    with open_db(root) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_documents(root: Path, *, company_key: str | None = None, limit: int = 500) -> list[dict]:
    query = """
        SELECT
          d.*,
          p.parse_id AS latest_parse_id,
          p.parsed_at AS latest_parsed_at,
          p.parser_name AS latest_parser_name,
          p.text_chars AS latest_text_chars,
          p.page_count AS latest_page_count,
          p.table_count AS latest_table_count,
          p.quality_flags AS latest_quality_flags,
          p.error AS latest_parse_error,
          p.artifact_path AS parse_artifact_path,
          p.derived_artifact_path AS derived_artifact_path
        FROM documents d
        LEFT JOIN document_parses p
          ON p.parse_id = (
            SELECT p2.parse_id
            FROM document_parses p2
            WHERE p2.doc_id = d.doc_id
            ORDER BY p2.parsed_at DESC, p2.rowid DESC
            LIMIT 1
          )
    """
    params: list[Any] = []
    if company_key:
        query += " WHERE d.company_key = ?"
        params.append(company_key)
    query += " ORDER BY COALESCE(d.published_at, d.last_seen_at, d.downloaded_at) DESC LIMIT ?"
    params.append(limit)
    with open_db(root) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_attach_artifact_fields(dict(row), root) for row in rows]


def list_companies(root: Path) -> list[dict]:
    with open_db(root) as conn:
        rows = conn.execute(
            """
            SELECT
              company_key,
              company_name,
              company_folder,
              MAX(ticker) AS ticker,
              MAX(coverage_key) AS coverage_key,
              COUNT(*) AS document_count,
              MAX(last_seen_at) AS last_seen_at,
              MAX(downloaded_at) AS downloaded_at
            FROM documents
            GROUP BY company_key, company_name, company_folder
            ORDER BY company_name ASC
            """
        ).fetchall()
        run_rows = conn.execute(
            """
            SELECT company_key, MAX(created_at) AS last_run_at,
                   MAX(CASE WHEN status = 'running' THEN created_at END) AS active_run_at
            FROM runs
            GROUP BY company_key
            """
        ).fetchall()
    run_map = {row["company_key"]: dict(row) for row in run_rows}
    companies: list[dict] = []
    for row in rows:
        item = dict(row)
        item.update(run_map.get(item["company_key"], {}))
        companies.append(item)
    return companies


def list_parse_records(root: Path, *, doc_id: str | None = None, limit: int = 200) -> list[dict]:
    query = "SELECT * FROM document_parses"
    params: list[Any] = []
    if doc_id:
        query += " WHERE doc_id = ?"
        params.append(doc_id)
    query += " ORDER BY parsed_at DESC LIMIT ?"
    params.append(limit)
    with open_db(root) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_attach_artifact_fields(dict(row), root) for row in rows]


def list_jobs(root: Path, *, status: str | None = None, job_type: str | None = None, limit: int = 100) -> list[dict]:
    conditions: list[str] = []
    params: list[Any] = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if job_type:
        conditions.append("job_type = ?")
        params.append(job_type)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    with open_db(root) as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs{where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def get_parse_stats(root: Path) -> dict:
    """Return aggregate parse status counts."""
    with open_db(root) as conn:
        rows = conn.execute(
            "SELECT parse_status, COUNT(*) AS cnt FROM documents GROUP BY parse_status"
        ).fetchall()
        delta_rows = conn.execute(
            "SELECT delta_state, COUNT(*) AS cnt FROM documents GROUP BY delta_state"
        ).fetchall()
        job_rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status"
        ).fetchall()
        derived_rows = conn.execute(
            """
            SELECT
              CASE
                WHEN p.derived_artifact_path IS NOT NULL AND p.derived_artifact_path != '' THEN 'available'
                ELSE 'missing'
              END AS derived_state,
              COUNT(*) AS cnt
            FROM documents d
            LEFT JOIN document_parses p
              ON p.parse_id = (
                SELECT p2.parse_id
                FROM document_parses p2
                WHERE p2.doc_id = d.doc_id
                ORDER BY p2.parsed_at DESC, p2.rowid DESC
                LIMIT 1
              )
            WHERE d.parse_status = 'parsed'
            GROUP BY derived_state
            """
        ).fetchall()
    return {
        "parse_status": {row["parse_status"] or "null": row["cnt"] for row in rows},
        "delta_state": {row["delta_state"] or "null": row["cnt"] for row in delta_rows},
        "derived_artifacts": {row["derived_state"]: row["cnt"] for row in derived_rows},
        "jobs": {row["status"]: row["cnt"] for row in job_rows},
    }


def repair_stale_state(
    root: Path,
    *,
    stale_minutes: int = DEFAULT_STALE_MINUTES,
    requeue_running_parse_jobs: bool = True,
) -> dict:
    """
    Repair stale corpus state caused by interrupted UI/process runs.

    - marks long-running scrape runs as failed
    - optionally resets long-running parse jobs back to pending so they can be drained
    """
    cutoff_iso = datetime.fromtimestamp(
        utc_now().timestamp() - max(1, stale_minutes) * 60,
        tz=timezone.utc,
    ).isoformat(timespec="seconds")
    repaired_at = utc_now_iso()

    summary = {
        "stale_minutes": stale_minutes,
        "cutoff": cutoff_iso,
        "runs_failed": 0,
        "run_ids": [],
        "jobs_requeued": 0,
        "job_ids": [],
    }

    with open_db(root) as conn:
        stale_runs = conn.execute(
            """
            SELECT run_id
            FROM runs
            WHERE status = 'running'
              AND COALESCE(started_at, created_at) <= ?
            """,
            (cutoff_iso,),
        ).fetchall()
        for row in stale_runs:
            run_id = row["run_id"]
            conn.execute(
                """
                UPDATE runs
                SET status = 'failed',
                    finished_at = ?,
                    error_message = COALESCE(error_message, ?),
                    progress_message = ?
                WHERE run_id = ?
                """,
                (
                    repaired_at,
                    f"Marked failed by repair after exceeding {stale_minutes} minutes without completion.",
                    f"stale run repaired after {stale_minutes} minutes",
                    run_id,
                ),
            )
            summary["runs_failed"] += 1
            summary["run_ids"].append(run_id)

        if requeue_running_parse_jobs:
            stale_jobs = conn.execute(
                """
                SELECT job_id
                FROM jobs
                WHERE job_type = 'parse'
                  AND status = 'running'
                  AND COALESCE(started_at, created_at) <= ?
                """,
                (cutoff_iso,),
            ).fetchall()
            for row in stale_jobs:
                job_id = row["job_id"]
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'pending',
                        started_at = NULL,
                        finished_at = NULL,
                        result_json = NULL,
                        error = ?,
                        failure_type = 'transient',
                        retry_count = COALESCE(retry_count, 0) + 1,
                        retry_after = NULL
                    WHERE job_id = ?
                    """,
                    (
                        f"Re-queued by repair after exceeding {stale_minutes} minutes in running state.",
                        job_id,
                    ),
                )
                summary["jobs_requeued"] += 1
                summary["job_ids"].append(job_id)

        conn.commit()

    return summary


def get_run(root: Path, run_id: str) -> dict | None:
    with open_db(root) as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return row_to_dict(row)


# ---------------------------------------------------------------------------
# Company spec
# ---------------------------------------------------------------------------


def normalize_company_spec(
    *,
    coverage_key: str | None = None,
    company_name: str | None = None,
    ticker: str | None = None,
    sec_cik: str | None = None,
    ir_page: str | None = None,
    exchange_adapter: str | None = None,
    exchange_symbol: str | None = None,
    exchange_code: str | None = None,
    exchange_slug: str | None = None,
    website_probe_urls: list[str] | None = None,
) -> CompanySpec:
    meta = dict(TICKER_META.get(coverage_key or "", {}))
    name = company_name or meta.get("company_name") or (coverage_key.split("/")[-1] if coverage_key else None)
    if not name:
        raise ValueError("company_name or coverage_key is required")

    ticker_final = ticker or (coverage_key.split("/")[-1] if coverage_key and coverage_key.startswith("tickers/") else None)
    ticker_final = ticker_final or meta.get("exchange_symbol") or meta.get("finnhub_symbol") or meta.get("av_symbol")
    sec_cik_final = sec_cik or meta.get("sec_cik")
    ir_page_final = ir_page or meta.get("ir_page")
    adapter_final = exchange_adapter or meta.get("exchange_adapter")
    symbol_final = exchange_symbol or meta.get("exchange_symbol")
    code_final = exchange_code or meta.get("exchange_code")
    slug_final = exchange_slug or meta.get("exchange_slug")
    probe_urls = website_probe_urls or meta.get("website_probe_urls") or ([ir_page_final] if ir_page_final else [])

    company_key = coverage_key or f"custom/{slugify(ticker_final or name)}"
    folder_parts = []
    if ticker_final:
        folder_parts.append(slugify(ticker_final, upper=True, max_len=18))
    folder_parts.append(slugify(name, max_len=60))
    company_folder = "_".join(part for part in folder_parts if part)

    spec = CompanySpec(
        company_name=name,
        ticker=ticker_final,
        coverage_key=coverage_key,
        sec_cik=sec_cik_final,
        ir_page=ir_page_final,
        exchange_adapter=adapter_final,
        exchange_symbol=symbol_final,
        exchange_code=code_final,
        exchange_slug=slug_final,
        website_probe_urls=list(dict.fromkeys([url for url in probe_urls if url])),
        company_key=company_key,
        company_folder=company_folder,
    )

    # Save to known company database if it's new
    if company_key not in TICKER_META:
        try:
            from config import save_custom_ticker
            save_custom_ticker(company_key, {
                "company_name": name,
                "ticker": ticker_final,
                "sec_cik": sec_cik_final,
                "ir_page": ir_page_final,
                "exchange_adapter": adapter_final,
                "exchange_symbol": symbol_final,
                "exchange_code": code_final,
                "exchange_slug": slug_final,
                "website_probe_urls": spec.website_probe_urls,
            })
        except Exception as e:
            log.warning("Failed to auto-save custom ticker %s: %s", company_key, e)

    return spec


# ---------------------------------------------------------------------------
# Document classification
# ---------------------------------------------------------------------------


def family_for_document(doc: dict) -> str:
    text = " ".join(
        str(doc.get(key) or "")
        for key in ("doc_type", "title", "url", "form_type")
    ).lower()
    if any(token in text for token in ["data pack", "datapack", ".xlsx", ".xls"]):
        return "data_pack"
    if "transcript" in text:
        return "transcript"
    if any(token in text for token in ["presentation", "slide", "investor day", "agm", "capital markets day"]):
        return "presentation"
    if any(token in text for token in ["annual report", "integrated report", "10-k", "20-f", "40-f"]):
        return "annual_report"
    if any(token in text for token in ["quarterly", "interim", "10-q", "q1", "q2", "q3", "q4", "half year"]):
        return "quarterly_report"
    if any(token in text for token in ["8-k", "6-k", "announcement", "press release", "rns", "filing"]):
        return "major_announcement"
    return doc.get("doc_type") or "other"


def family_dir_for_document(doc: dict) -> str:
    family = family_for_document(doc)
    return DOC_FAMILY_DIRS.get(family, "other")


def run_defaults(mode: str) -> tuple[int, int]:
    if mode == "incremental":
        return DEFAULT_INCREMENTAL_DAYS, DEFAULT_INCREMENTAL_MAX_DOCS
    return DEFAULT_BACKFILL_DAYS, DEFAULT_BACKFILL_MAX_DOCS


def dedupe_documents(documents: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for doc in documents:
        url = doc.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(doc)
    out.sort(key=lambda d: (d.get("published_at") or "", d.get("origin") == "sec"), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Document collection
# ---------------------------------------------------------------------------


def collect_candidate_documents(
    spec: CompanySpec,
    *,
    mode: str,
    days_back: int,
    max_docs: int,
    observations: dict | None = None,
) -> list[dict]:
    """
    Collect candidate documents from every configured source for this company.

    Sources are complementary, not exclusive: a name listed on HKEX with an
    SEC ADR (e.g. HSBC) needs both HKEX and SEC discovery. The previous
    if/elif gated SEC behind exchange_adapter and silently dropped SEC docs
    for every dual-listed name.

    When `observations` is supplied, per-source counts and IR-page rejection
    histograms are recorded into it for site_learning persistence.
    """
    from official_docs_fetcher import _new_ir_observations  # late import: avoid module-load-time cycle

    docs: list[dict] = []

    # Exchange adapter (LSE / HKEX / TSE / SEC-via-adapter). May return [] for
    # probe-only adapters today; complementary to direct SEC fetch below.
    exchange_count = 0
    if spec.coverage_key and spec.exchange_adapter:
        exchange_docs = fetch_exchange_documents(spec.coverage_key, days_back=days_back, limit=max_docs)
        exchange_count = len(exchange_docs)
        docs.extend(exchange_docs)

    # Direct SEC EDGAR fetch — runs whenever a CIK is configured, regardless
    # of whether an exchange_adapter is also set. Foreign filers (HSBC, MUFG,
    # MMYT, etc.) file 6-K / 20-F here in addition to home-exchange filings.
    sec_count = 0
    if spec.sec_cik:
        sec_docs = fetch_sec_recent_documents(spec.sec_cik, days_back=days_back, limit=max_docs)
        sec_count = len(sec_docs)
        docs.extend(sec_docs)

    ir_pages = []
    if spec.ir_page:
        ir_pages.append(spec.ir_page)
    for p in spec.website_probe_urls:
        if p not in ir_pages:
            ir_pages.append(p)

    ir_page_obs: list[dict] = []
    for page in ir_pages:
        page_obs = _new_ir_observations(page) if observations is not None else None
        ir_docs = fetch_ir_recent_documents(
            page, spec.company_name, days_back=days_back, limit=max_docs, observations=page_obs
        )
        if page_obs is not None:
            ir_page_obs.append(page_obs)
        docs.extend(ir_docs)

    pre_dedupe_count = len(docs)
    docs = dedupe_documents(docs)
    docs.sort(key=lambda d: (d.get("published_at") or "0000-00-00", d.get("origin") == "sec"), reverse=True)
    capped = False
    if len(docs) > max_docs:
        docs = docs[:max_docs]
        capped = True

    if observations is not None:
        observations["exchange_count"] = exchange_count
        observations["sec_count"] = sec_count
        observations["ir_page_observations"] = ir_page_obs
        observations["pre_dedupe_total"] = pre_dedupe_count
        observations["post_dedupe_total"] = len(docs) if not capped else pre_dedupe_count - (pre_dedupe_count - max_docs)
        observations["capped_at_max_docs"] = capped
        observations["mode"] = mode
        observations["days_back"] = days_back
        observations["max_docs"] = max_docs

    return docs


# ---------------------------------------------------------------------------
# DB lookups
# ---------------------------------------------------------------------------


def lookup_existing_document(conn: sqlite3.Connection, url: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE source_url = ? OR final_url = ? LIMIT 1",
        (url, url),
    ).fetchone()
    return row_to_dict(row)


def find_by_sha256(conn: sqlite3.Connection, sha256: str, *, company_key: str) -> dict | None:
    """Find an existing document with the same SHA256 hash for the same company."""
    if not sha256:
        return None
    row = conn.execute(
        "SELECT * FROM documents WHERE sha256 = ? AND company_key = ? LIMIT 1",
        (sha256, company_key),
    ).fetchone()
    return row_to_dict(row)


# ---------------------------------------------------------------------------
# Delta state computation
# ---------------------------------------------------------------------------


def compute_delta_state(
    *,
    existing: dict | None,
    new_sha256: str,
    conn: sqlite3.Connection,
    company_key: str,
) -> str:
    """
    Determine the delta state for a newly downloaded document.

    States:
      new        : no existing record for this URL
      unchanged  : same sha256 as the existing record
      updated    : different sha256 — content changed
      duplicate  : sha256 matches a *different* document in this company corpus
    """
    if existing is None:
        # Check if it's a cross-URL duplicate within the same company
        dupe = find_by_sha256(conn, new_sha256, company_key=company_key)
        if dupe:
            return "duplicate"
        return "new"
    existing_sha = existing.get("sha256") or ""
    if existing_sha and existing_sha == new_sha256:
        return "unchanged"
    return "updated"


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


def content_extension(final_url: str, content_type: str, title: str) -> str:
    path = urlparse(final_url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".pdf", ".html", ".htm", ".txt", ".json", ".xlsx", ".xls"}:
        return ".html" if suffix == ".htm" else suffix
    for key, ext in CONTENT_TYPE_EXTENSIONS.items():
        if (content_type or "").lower().startswith(key):
            return ext
    if title.lower().endswith(".xlsx"):
        return ".xlsx"
    if title.lower().endswith(".xls"):
        return ".xls"
    return ".bin"


def guess_basename(doc: dict, final_url: str, content_type: str) -> str:
    parsed = urlparse(final_url)
    basename = Path(parsed.path).name
    if basename:
        return sanitize_filename(basename)
    title = sanitize_filename(doc.get("title") or family_for_document(doc).replace("_", "-"))
    ext = content_extension(final_url, content_type, title)
    if not title.lower().endswith(ext):
        title += ext
    return title


def canonical_filename(doc: dict, basename: str) -> str:
    published = doc.get("published_at") or "undated"
    title = sanitize_filename(Path(basename).stem)
    suffix = Path(basename).suffix.lower()
    return sanitize_filename(f"{published}_{title}{suffix}")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def prefer_edgar_headers(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("sec.gov") or host.endswith("data.sec.gov")


def header_variants(url: str) -> list[dict]:
    primary = HEADERS_EDGAR if prefer_edgar_headers(url) else HEADERS_WEB
    secondary = HEADERS_WEB if primary is HEADERS_EDGAR else HEADERS_EDGAR
    variants: list[dict] = []
    for candidate in (primary, secondary):
        if candidate not in variants:
            variants.append(candidate)
    return variants


def conditional_request_headers(existing: dict | None) -> dict[str, str]:
    if not existing:
        return {}
    headers: dict[str, str] = {}
    etag = (existing.get("etag") or "").strip()
    last_modified = (existing.get("last_modified") or "").strip()
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def _domain_matches(hostname: str, allowed_domains: list[str]) -> bool:
    host = (hostname or "").lower()
    for domain in allowed_domains:
        candidate = (domain or "").lower()
        if host == candidate or host.endswith(f".{candidate}"):
            return True
    return False


def resolve_source_entry_for_document(doc: dict, source_entries: list[Any]) -> Any | None:
    doc_url = doc.get("url") or ""
    hostname = (urlparse(doc_url).hostname or "").lower()
    origin = (doc.get("origin") or "").lower()

    for entry in source_entries:
        allowed = list(dict.fromkeys((entry.allowed_domains or []) + ([entry.domain] if getattr(entry, "domain", None) else [])))
        if hostname and _domain_matches(hostname, allowed):
            return entry

    for entry in source_entries:
        source_id = getattr(entry, "source_id", "")
        if origin == "sec" and source_id.endswith("/sec"):
            return entry
        if origin == "ir" and source_id.endswith("/ir"):
            return entry
    return None


def effective_download_mode_for_document(doc: dict, source_entry: Any | None) -> str:
    mode = getattr(source_entry, "download_mode", "http") if source_entry else "http"
    if mode != "browser":
        return mode

    parsed = urlparse(doc.get("url") or "")
    hostname = (parsed.hostname or "").lower()
    suffix = Path(parsed.path).suffix.lower()
    if suffix in DIRECT_HTTP_EXTENSIONS:
        return "http"
    if hostname.startswith("rns-pdf."):
        return "http"
    return "browser"


def _content_type_from_name(name: str) -> str:
    suffix = Path(name or "").suffix.lower()
    mapping = {
        ".pdf": "application/pdf",
        ".html": "text/html",
        ".htm": "text/html",
        ".txt": "text/plain",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
    }
    return mapping.get(suffix, "application/octet-stream")


def download_with_browser(url: str, target_dir: Path, *, existing: dict | None = None) -> dict:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError as exc:
        raise BrowserFallbackRequired(url) from exc

    target_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[dict] = []
    conditional_headers = conditional_request_headers(existing)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, extra_http_headers=conditional_headers or None)
        page = context.new_page()
        download_holder: dict[str, Any] = {}
        page.on("download", lambda download: download_holder.setdefault("download", download))
        try:
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:
                if "Download is starting" not in str(exc):
                    raise
                page.wait_for_timeout(1_000)
                download = download_holder.get("download")
                if download is None:
                    raise
                temp_path = str(Path(tempfile.NamedTemporaryFile(delete=False, dir=target_dir).name))
                download.save_as(temp_path)
                body = Path(temp_path).read_bytes()
                if len(body) > MAX_DOWNLOAD_BYTES:
                    Path(temp_path).unlink(missing_ok=True)
                    raise ValueError(f"download exceeded {MAX_DOWNLOAD_BYTES} bytes")
                final_url = getattr(download, "url", None) or page.url or url
                suggested_name = getattr(download, "suggested_filename", None) or Path(final_url).name
                return {
                    "result": "downloaded",
                    "temp_path": temp_path,
                    "final_url": final_url,
                    "content_type": _content_type_from_name(suggested_name or final_url),
                    "content_length": len(body),
                    "etag": (existing or {}).get("etag"),
                    "last_modified": (existing or {}).get("last_modified"),
                    "size_bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "attempts": [{"url": url, "headers": "browser", "status": 200, "result": "download"}],
                }

            if response is None:
                raise RuntimeError(f"browser navigation produced no response for {url}")

            etag = response.headers.get("etag") or (existing or {}).get("etag")
            last_modified = response.headers.get("last-modified") or (existing or {}).get("last_modified")
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            content_length_str = response.headers.get("content-length") or ""
            content_length = int(content_length_str) if content_length_str.isdigit() else (existing or {}).get("content_length")

            if response.status == 304:
                return {
                    "result": "not_modified",
                    "temp_path": None,
                    "final_url": page.url,
                    "content_type": (existing or {}).get("content_type") or content_type,
                    "content_length": content_length,
                    "etag": etag,
                    "last_modified": last_modified,
                    "size_bytes": (existing or {}).get("size_bytes") or 0,
                    "sha256": (existing or {}).get("sha256") or "",
                    "attempts": [{"url": url, "headers": "browser", "status": 304, "result": "not_modified"}],
                }

            body = response.body()
            if not body and content_type.startswith("text/html"):
                page.wait_for_load_state("networkidle", timeout=15_000)
                body = page.content().encode("utf-8")
            if not body:
                raise RuntimeError(f"browser fetch returned empty body for {url}")

            if len(body) > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"download exceeded {MAX_DOWNLOAD_BYTES} bytes")

            with tempfile.NamedTemporaryFile(delete=False, dir=target_dir) as tmp:
                tmp.write(body)
                temp_path = tmp.name
            return {
                "result": "downloaded",
                "temp_path": temp_path,
                "final_url": page.url,
                "content_type": content_type or "text/html",
                "content_length": content_length or len(body),
                "etag": etag,
                "last_modified": last_modified,
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "attempts": [{"url": url, "headers": "browser", "status": response.status, "result": "ok"}],
            }
        finally:
            context.close()
            browser.close()


def url_variants(url: str) -> list[str]:
    variants = [url]
    parsed = urlparse(url)
    if parsed.query:
        stripped = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", parsed.fragment))
        if stripped not in variants:
            variants.append(stripped)
    return variants


def download_with_fallbacks(
    url: str,
    target_dir: Path,
    *,
    download_mode: str = "http",
    existing: dict | None = None,
) -> dict:
    """
    Download a URL to a temp file in target_dir.

    Returns a dict with:
      temp_path, final_url, content_type, content_length, etag, last_modified,
      size_bytes, sha256, attempts

    Raises:
      BrowserFallbackRequired  if download_mode is 'browser'
      RuntimeError             if all HTTP attempts fail
    """
    if download_mode == "browser":
        return download_with_browser(url, target_dir, existing=existing)

    attempts: list[dict] = []
    last_error: str | None = None
    target_dir.mkdir(parents=True, exist_ok=True)

    conditional_headers = conditional_request_headers(existing)

    for candidate_url in url_variants(url):
        for headers in header_variants(candidate_url):
            resp = None
            temp_path: str | None = None
            header_label = "edgar" if headers is HEADERS_EDGAR else "web"
            try:
                request_headers = dict(headers)
                request_headers.update(conditional_headers)
                resp = requests.get(candidate_url, headers=request_headers, stream=True, timeout=30, allow_redirects=True)

                etag = resp.headers.get("ETag") or resp.headers.get("etag") or (existing or {}).get("etag")
                last_modified = resp.headers.get("Last-Modified") or resp.headers.get("last-modified") or (existing or {}).get("last_modified")
                content_length_str = resp.headers.get("Content-Length") or resp.headers.get("content-length")
                content_length: int | None = int(content_length_str) if content_length_str and content_length_str.isdigit() else (existing or {}).get("content_length")

                if resp.status_code == 304:
                    return {
                        "result": "not_modified",
                        "temp_path": None,
                        "final_url": resp.url or candidate_url,
                        "content_type": ((existing or {}).get("content_type") or resp.headers.get("Content-Type", "").split(";")[0].strip().lower()),
                        "content_length": content_length,
                        "etag": etag,
                        "last_modified": last_modified,
                        "size_bytes": (existing or {}).get("size_bytes") or 0,
                        "sha256": (existing or {}).get("sha256") or "",
                        "attempts": attempts + [{"url": candidate_url, "headers": header_label, "status": resp.status_code, "result": "not_modified"}],
                    }

                resp.raise_for_status()

                total = 0
                sha = hashlib.sha256()
                with tempfile.NamedTemporaryFile(delete=False, dir=target_dir) as tmp:
                    temp_path = tmp.name
                    for chunk in resp.iter_content(chunk_size=128 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError(f"download exceeded {MAX_DOWNLOAD_BYTES} bytes")
                        sha.update(chunk)
                        tmp.write(chunk)
                return {
                    "result": "downloaded",
                    "temp_path": temp_path,
                    "final_url": resp.url,
                    "content_type": resp.headers.get("Content-Type", "").split(";")[0].strip().lower(),
                    "content_length": content_length,
                    "etag": etag,
                    "last_modified": last_modified,
                    "size_bytes": total,
                    "sha256": sha.hexdigest(),
                    "attempts": attempts + [{"url": candidate_url, "headers": header_label, "status": resp.status_code, "result": "ok"}],
                }
            except BrowserFallbackRequired:
                raise
            except Exception as exc:
                last_error = str(exc)
                attempts.append({"url": candidate_url, "headers": header_label, "result": "error", "error": str(exc)})
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
            finally:
                if resp is not None:
                    resp.close()

    if download_mode == "mixed":
        try:
            return download_with_browser(url, target_dir, existing=existing)
        except BrowserFallbackRequired as exc:
            raise BrowserFallbackRequired(url) from exc
        except Exception as exc:
            last_error = f"{last_error or 'http failed'}; browser fallback failed: {exc}"

    raise RuntimeError(last_error or f"download failed for {url}")


# ---------------------------------------------------------------------------
# Site learning
# ---------------------------------------------------------------------------


def _serialize_observations(observations: dict | None) -> dict | None:
    """Convert Counter objects in run observations to plain dicts for JSON
    serialization. Preserves nested ir_page_observations."""
    if not observations:
        return None
    out = dict(observations)
    pages = []
    for page_obs in out.get("ir_page_observations", []) or []:
        page_copy = dict(page_obs)
        for key in ("rejected_counts", "accepted_doc_types", "date_source_counts"):
            val = page_copy.get(key)
            if val is not None:
                page_copy[key] = dict(val)
        pages.append(page_copy)
    out["ir_page_observations"] = pages
    return out


def save_site_learning(
    root: Path,
    spec: CompanySpec,
    documents: list[dict],
    *,
    observations: dict | None = None,
) -> None:
    company_dir = root / spec.company_folder
    meta_dir = company_dir / META_DIRNAME
    meta_dir.mkdir(parents=True, exist_ok=True)

    if spec.coverage_key and spec.exchange_adapter:
        exchange_probe = probe_exchange_adapter(spec.coverage_key)
    else:
        exchange_probe = {
            "adapter": spec.exchange_adapter,
            "label": spec.exchange_adapter or "custom",
            "endpoints": [],
            "notes": [
                "Ad hoc company, no built-in exchange adapter metadata beyond user-provided fields.",
            ],
        }

    website_probes = [_http_probe(url) for url in spec.website_probe_urls]
    pattern_notes = _infer_storage_patterns(documents, website_probes)
    fallback_notes = [
        "Primary source order is exchange/filer adapter first when available, then company IR archive.",
        "Download fallbacks per file try direct URL first, then alternate request headers, then a queryless URL variant when the source URL includes query parameters.",
        "Current limitation: LSE, HKEX, and TSE adapters are still probe/discovery layers, so historical corpus builds for those names rely mostly on IR archives today.",
        "Sources configured as download_mode=browser can use the Playwright lane when Playwright + Chromium are installed; direct asset URLs such as PDFs still prefer HTTP when they are already addressable.",
    ]

    serialized_obs = _serialize_observations(observations)

    payload = {
        "company": asdict(spec),
        "generated_at": utc_now_iso(),
        "exchange_probe": exchange_probe,
        "website_probes": website_probes,
        "pattern_notes": pattern_notes,
        "fallback_notes": fallback_notes,
        "run_observations": serialized_obs,
    }
    (meta_dir / "site_learning.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        f"# Site learning — {spec.company_name}",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Coverage key: `{spec.coverage_key or 'N/A'}`",
        f"- Exchange adapter: `{spec.exchange_adapter or 'none'}`",
        f"- IR page: `{spec.ir_page or 'N/A'}`",
        "",
        "## Endpoint checks",
        "",
        f"**Exchange / filing endpoints ({exchange_probe.get('label')})**",
        "",
    ]
    if exchange_probe.get("endpoints"):
        lines.extend(f"- `{item.get('status')}` {item.get('url')}" for item in exchange_probe["endpoints"])
    else:
        lines.append("- No exchange endpoints recorded.")
    lines.extend(["", "**Website endpoints**", ""])
    if website_probes:
        for probe in website_probes:
            lines.append(f"- `{probe.get('status')}` {probe.get('url')}")
    else:
        lines.append("- No website probes configured.")
    lines.extend(["", "## Recorded learnings", ""])
    for note in pattern_notes + exchange_probe.get("notes", []) + fallback_notes:
        lines.append(f"- {note}")

    if serialized_obs:
        lines.extend(["", "## Last run observations", ""])
        lines.append(f"- Mode: `{serialized_obs.get('mode')}`, days_back: `{serialized_obs.get('days_back')}`, max_docs: `{serialized_obs.get('max_docs')}`")
        lines.append(f"- Source counts — exchange: {serialized_obs.get('exchange_count', 0)}, sec: {serialized_obs.get('sec_count', 0)}, ir_pages: {len(serialized_obs.get('ir_page_observations') or [])}")
        lines.append(f"- Pre-dedupe total: {serialized_obs.get('pre_dedupe_total', 0)} → post-dedupe: {serialized_obs.get('post_dedupe_total', 0)} (capped at max_docs: {serialized_obs.get('capped_at_max_docs', False)})")
        for page_obs in serialized_obs.get("ir_page_observations") or []:
            lines.extend(["", f"### IR page: {page_obs.get('page_url')}"])
            lines.append(f"- Page status: `{page_obs.get('page_status')}`, links seen: {page_obs.get('links_seen')}, candidates kept: {page_obs.get('candidates_kept')}, accepted: {page_obs.get('accepted_count')}")
            rejected = page_obs.get("rejected_counts") or {}
            if rejected:
                rejected_pairs = sorted(rejected.items(), key=lambda kv: kv[1], reverse=True)
                lines.append("- Rejection reasons:")
                for reason, count in rejected_pairs:
                    lines.append(f"  - `{reason}`: {count}")
            accepted_types = page_obs.get("accepted_doc_types") or {}
            if accepted_types:
                lines.append("- Accepted by family: " + ", ".join(f"`{k}`={v}" for k, v in sorted(accepted_types.items(), key=lambda kv: kv[1], reverse=True)))
            date_sources = page_obs.get("date_source_counts") or {}
            if date_sources:
                lines.append("- Date inference source: " + ", ".join(f"`{k}`={v}" for k, v in sorted(date_sources.items(), key=lambda kv: kv[1], reverse=True)))
            for note in page_obs.get("notes") or []:
                lines.append(f"- {note}")

    lines.append("")
    (meta_dir / "site_learning.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Run management
# ---------------------------------------------------------------------------


def ensure_run(conn: sqlite3.Connection, spec: CompanySpec, *, run_id: str, mode: str, days_back: int, max_docs: int) -> None:
    payload = json.dumps(asdict(spec), ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO runs (
            run_id, created_at, started_at, status, company_key, company_name,
            company_folder, ticker, coverage_key, mode, days_back, max_docs,
            sec_cik, ir_page, exchange_adapter, exchange_symbol, progress_message,
            payload_json
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            utc_now_iso(),
            utc_now_iso(),
            spec.company_key,
            spec.company_name,
            spec.company_folder,
            spec.ticker,
            spec.coverage_key,
            mode,
            days_back,
            max_docs,
            spec.sec_cik,
            spec.ir_page,
            spec.exchange_adapter,
            spec.exchange_symbol,
            "discovering documents",
            payload,
        ),
    )
    conn.commit()


def update_run(conn: sqlite3.Connection, run_id: str, **fields: Any) -> None:
    if not fields:
        return
    keys = list(fields.keys())
    values = [fields[key] for key in keys]
    sets = ", ".join(f"{key} = ?" for key in keys)
    conn.execute(f"UPDATE runs SET {sets} WHERE run_id = ?", (*values, run_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Document upsert
# ---------------------------------------------------------------------------


def upsert_document(
    conn: sqlite3.Connection,
    *,
    spec: CompanySpec,
    run_id: str,
    doc: dict,
    stored_path: Path,
    relative_path: str,
    content_type: str,
    sha256: str,
    size_bytes: int,
    final_url: str,
    filename: str,
    status: str,
    metadata: dict,
    # v2 fields
    content_length: int | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    download_status: str | None = None,
    parse_status: str | None = None,
    parse_quality_flags: list[str] | None = None,
    delta_state: str | None = None,
    failure_type: str | None = None,
) -> str:
    """Insert or update a document record. Returns the doc_id."""
    now = utc_now_iso()
    existing = lookup_existing_document(conn, doc.get("url") or final_url)
    payload = json.dumps(metadata, ensure_ascii=False)
    family = family_for_document(doc)
    quality_flags_json = json.dumps(parse_quality_flags or [])

    # Compute download_status from legacy status if not provided
    if download_status is None:
        download_status = _infer_download_status(status=status, absolute_path=str(stored_path) if stored_path else None)
    if parse_status is None:
        parse_status = _infer_parse_status(
            absolute_path=str(stored_path) if stored_path else None,
            filename=filename,
            content_type=content_type,
            download_status=download_status,
        )

    if existing:
        conn.execute(
            """
            UPDATE documents SET
              company_key = ?, company_name = ?, company_folder = ?, ticker = ?, coverage_key = ?,
              final_url = ?, source = ?, origin = ?, form_type = ?, doc_type = ?, doc_family = ?,
              published_at = ?, title = ?, filename = ?, relative_path = ?, absolute_path = ?,
              content_type = ?, sha256 = ?, size_bytes = ?, last_run_id = ?, last_seen_at = ?,
              status = ?, snippet = ?, metadata_json = ?,
              content_length = ?, etag = ?, last_modified = ?,
              download_status = ?, parse_status = ?, parse_quality_flags = ?,
              delta_state = ?, failure_type = ?
            WHERE doc_id = ?
            """,
            (
                spec.company_key, spec.company_name, spec.company_folder, spec.ticker, spec.coverage_key,
                final_url, doc.get("source"), doc.get("origin"), doc.get("form_type"),
                doc.get("doc_type"), family, doc.get("published_at"), doc.get("title"),
                filename, relative_path, str(stored_path), content_type, sha256, size_bytes,
                run_id, now, status, doc.get("text_snippet"), payload,
                content_length, etag, last_modified,
                download_status, parse_status, quality_flags_json,
                delta_state, failure_type,
                existing["doc_id"],
            ),
        )
        conn.commit()
        return existing["doc_id"]
    else:
        doc_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO documents (
              doc_id, company_key, company_name, company_folder, ticker, coverage_key,
              source_url, final_url, source, origin, form_type, doc_type, doc_family,
              published_at, title, filename, relative_path, absolute_path, content_type,
              sha256, size_bytes, downloaded_at, first_run_id, last_run_id, last_seen_at,
              status, snippet, metadata_json, first_seen_at,
              content_length, etag, last_modified,
              download_status, parse_status, parse_quality_flags,
              delta_state, failure_type, retry_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id, spec.company_key, spec.company_name, spec.company_folder,
                spec.ticker, spec.coverage_key, doc.get("url"), final_url,
                doc.get("source"), doc.get("origin"), doc.get("form_type"),
                doc.get("doc_type"), family, doc.get("published_at"), doc.get("title"),
                filename, relative_path, str(stored_path), content_type,
                sha256, size_bytes, now, run_id, run_id, now, status,
                doc.get("text_snippet"), payload, now,
                content_length, etag, last_modified,
                download_status, parse_status, quality_flags_json,
                delta_state, failure_type, 0,
            ),
        )
        conn.commit()
        return doc_id


def mark_existing_seen(
    conn: sqlite3.Connection,
    existing: dict,
    run_id: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    content_length: int | None = None,
) -> None:
    download_status = existing.get("download_status") or _infer_download_status(
        status=existing.get("status"),
        absolute_path=existing.get("absolute_path"),
    )
    parse_status = existing.get("parse_status") or _infer_parse_status(
        absolute_path=existing.get("absolute_path"),
        filename=existing.get("filename"),
        content_type=existing.get("content_type"),
        download_status=download_status,
    )
    conn.execute(
        """
        UPDATE documents
        SET last_run_id = ?, last_seen_at = ?, status = ?, delta_state = ?,
            download_status = ?, parse_status = ?,
            parse_quality_flags = COALESCE(parse_quality_flags, '[]'),
            first_seen_at = COALESCE(first_seen_at, downloaded_at, last_seen_at, ?),
            etag = COALESCE(?, etag),
            last_modified = COALESCE(?, last_modified),
            content_length = COALESCE(?, content_length),
            retry_count = COALESCE(retry_count, 0)
        WHERE doc_id = ?
        """,
        (
            run_id,
            utc_now_iso(),
            "existing",
            "unchanged",
            download_status,
            parse_status,
            utc_now_iso(),
            etag,
            last_modified,
            content_length,
            existing["doc_id"],
        ),
    )
    conn.commit()


def is_parseable_document(*, filename: str | None, absolute_path: str | None, content_type: str | None) -> bool:
    suffix = Path(filename or absolute_path or "").suffix.lower()
    if suffix in PARSEABLE_EXTENSIONS:
        return True
    return (content_type or "").lower() in PARSEABLE_CONTENT_TYPES


def ensure_parse_job_for_document(conn: sqlite3.Connection, doc: dict, *, force: bool = False) -> str | None:
    if not is_parseable_document(
        filename=doc.get("filename"),
        absolute_path=doc.get("absolute_path"),
        content_type=doc.get("content_type"),
    ):
        return None
    parse_status = doc.get("parse_status") or "unparsed"
    if parse_status == "parsed" and not force:
        return None
    existing_job = conn.execute(
        "SELECT job_id FROM jobs WHERE doc_id = ? AND job_type = 'parse' AND status IN ('pending', 'running') LIMIT 1",
        (doc["doc_id"],),
    ).fetchone()
    if existing_job:
        return existing_job["job_id"]
    return queue_parse_job(
        conn,
        doc_id=doc["doc_id"],
        company_key=doc.get("company_key") or "",
        coverage_key=doc.get("coverage_key"),
    )


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------


def queue_parse_job(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    company_key: str,
    coverage_key: str | None,
    params: dict | None = None,
) -> str:
    """Enqueue a parse job for a newly downloaded document."""
    job_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO jobs (job_id, job_type, status, created_at, company_key, coverage_key, doc_id, params_json)
        VALUES (?, 'parse', 'pending', ?, ?, ?, ?, ?)
        """,
        (job_id, utc_now_iso(), company_key, coverage_key, doc_id, json.dumps(params or {})),
    )
    conn.commit()
    return job_id


def run_parse_job(
    conn: sqlite3.Connection,
    job: dict,
    *,
    root: Path,
) -> None:
    """
    Execute a pending parse job.

    Reads the document record, invokes the appropriate parser, saves the
    parse artifact, and updates document_parses + documents tables.
    """
    from official_doc_parsers import (
        build_derived_artifact,
        load_parse_artifact,
        parse_document,
        save_derived_artifact,
        save_parse_artifact,
    )

    job_id = job["job_id"]
    doc_id = job["doc_id"]

    # Mark started
    conn.execute(
        "UPDATE jobs SET status = 'running', started_at = ? WHERE job_id = ?",
        (utc_now_iso(), job_id),
    )
    conn.commit()

    # Load document record
    row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if not row:
        conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE job_id = ?",
            (utc_now_iso(), "document record not found", job_id),
        )
        conn.commit()
        return

    doc = dict(row)
    abs_path = doc.get("absolute_path") or ""
    content_type = doc.get("content_type") or ""
    company_folder = doc.get("company_folder") or ""
    artifact_dir = root / company_folder / META_DIRNAME / PARSES_DIRNAME
    artifact_path = artifact_dir / f"{doc_id}.json"
    previous_result = load_parse_artifact(artifact_path)

    if not abs_path or not Path(abs_path).exists():
        conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, error = ?, failure_type = 'permanent' WHERE job_id = ?",
            (utc_now_iso(), f"file not found: {abs_path}", job_id),
        )
        conn.commit()
        return

    try:
        result = parse_document(Path(abs_path), content_type)
    except Exception as exc:
        if previous_result and previous_result.ok:
            log.warning("Reusing prior parse artifact for %s after parser exception: %s", doc_id, exc)
            result = previous_result
            reused_previous_parse = True
        else:
            conn.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, error = ?, failure_type = 'transient' WHERE job_id = ?",
                (utc_now_iso(), str(exc), job_id),
            )
            conn.commit()
            return
    else:
        reused_previous_parse = False

    if previous_result and previous_result.ok and not result.ok:
        log.warning(
            "Reusing prior parse artifact for %s after parser failure: %s",
            doc_id,
            result.error or "unknown parse error",
        )
        result = previous_result
        reused_previous_parse = True

    # Save artifact
    try:
        save_parse_artifact(result, artifact_path)
    except Exception as exc:
        log.warning("Failed to save parse artifact for %s: %s", doc_id, exc)

    derived_dir = root / company_folder / META_DIRNAME / DERIVED_DIRNAME
    derived_path = derived_dir / f"{doc_id}.json"
    try:
        derived_artifact = build_derived_artifact(result, previous_result=previous_result)
        save_derived_artifact(derived_artifact, derived_path)
    except Exception as exc:
        log.warning("Failed to save derived artifact for %s: %s", doc_id, exc)

    # Upsert parse record
    parse_id = str(uuid.uuid4())
    quality_flags_json = json.dumps(result.quality_flags)
    conn.execute(
        """
        INSERT OR REPLACE INTO document_parses
        (parse_id, doc_id, parsed_at, parser_name, parse_version,
         text_chars, page_count, table_count, quality_flags, error, artifact_path, derived_artifact_path)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            parse_id, doc_id, utc_now_iso(), result.parser_name,
            len(result.text), result.page_count, result.table_count,
            quality_flags_json, result.error,
            str(artifact_path) if artifact_path.exists() else None,
            str(derived_path) if derived_path.exists() else None,
        ),
    )

    # Update document parse_status and quality flags
    parse_status = "parsed" if result.ok else "parse_failed"
    delta_state_update = "failed_parse" if not result.ok else None
    if delta_state_update:
        conn.execute(
            "UPDATE documents SET parse_status = ?, parse_quality_flags = ?, delta_state = ? WHERE doc_id = ?",
            (parse_status, quality_flags_json, delta_state_update, doc_id),
        )
    else:
        conn.execute(
            """
            UPDATE documents
            SET parse_status = ?,
                parse_quality_flags = ?,
                delta_state = CASE WHEN delta_state = 'failed_parse' THEN 'unchanged' ELSE delta_state END
            WHERE doc_id = ?
            """,
            (parse_status, quality_flags_json, doc_id),
        )
    conn.commit()

    # Mark job complete
    result_summary = {
        "parse_id": parse_id,
        "parser_name": result.parser_name,
        "ok": result.ok,
        "reused_previous_parse": reused_previous_parse,
        "text_chars": len(result.text),
        "page_count": result.page_count,
        "table_count": result.table_count,
        "quality_flags": result.quality_flags,
    }
    conn.execute(
        "UPDATE jobs SET status = 'completed', finished_at = ?, result_json = ? WHERE job_id = ?",
        (utc_now_iso(), json.dumps(result_summary), job_id),
    )
    conn.commit()


def process_pending_parse_jobs(root: Path, *, limit: int = 50) -> dict:
    """Run all pending parse jobs up to limit. Returns summary dict."""
    repair_summary = repair_stale_state(root)
    completed = failed = 0
    with open_db(root) as conn:
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE job_type = 'parse' AND status = 'pending' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        for job in jobs:
            try:
                run_parse_job(conn, dict(job), root=root)
                completed += 1
            except Exception as exc:
                failed += 1
                log.warning("Parse job %s failed: %s", job["job_id"], exc)
    return {
        "completed": completed,
        "failed": failed,
        "processed": len(jobs),
        "repair": repair_summary,
    }


def list_parse_backfill_candidates(
    root: Path,
    *,
    company_key: str | None = None,
    limit: int = 100,
    parse_statuses: list[str] | None = None,
    include_missing_derived: bool = True,
) -> list[dict]:
    statuses = list(dict.fromkeys(parse_statuses or ["unparsed", "parse_failed"]))
    with open_db(root) as conn:
        query = """
            SELECT
              d.*,
              p.parse_id AS latest_parse_id,
              p.parsed_at AS latest_parsed_at,
              p.parser_name AS latest_parser_name,
              p.artifact_path AS parse_artifact_path,
              p.derived_artifact_path AS derived_artifact_path
            FROM documents d
            LEFT JOIN document_parses p
              ON p.parse_id = (
                SELECT p2.parse_id
                FROM document_parses p2
                WHERE p2.doc_id = d.doc_id
                ORDER BY p2.parsed_at DESC, p2.rowid DESC
                LIMIT 1
              )
        """
        conditions: list[str] = ["d.absolute_path IS NOT NULL", "d.absolute_path != ''"]
        params: list[Any] = []
        if company_key:
            conditions.append("d.company_key = ?")
            params.append(company_key)
        status_conditions: list[str] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            status_conditions.append(f"d.parse_status IN ({placeholders})")
            params.extend(statuses)
        if include_missing_derived:
            status_conditions.append(
                "(d.parse_status = 'parsed' AND (p.derived_artifact_path IS NULL OR p.derived_artifact_path = ''))"
            )
        if status_conditions:
            conditions.append("(" + " OR ".join(status_conditions) + ")")
        query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY COALESCE(d.published_at, d.last_seen_at, d.downloaded_at) DESC"
        rows = conn.execute(query, tuple(params)).fetchall()

    candidates: list[dict] = []
    for row in rows:
        item = _attach_artifact_fields(dict(row), root)
        if not is_parseable_document(
            filename=item.get("filename"),
            absolute_path=item.get("absolute_path"),
            content_type=item.get("content_type"),
        ):
            continue
        if not _path_exists(item.get("absolute_path")):
            continue
        item["backfill_reason"] = "missing_derived" if (
            item.get("parse_status") == "parsed" and not item.get("derived_artifact_available")
        ) else (item.get("parse_status") or "unparsed")
        candidates.append(item)
        if len(candidates) >= limit:
            break
    return candidates


def queue_parse_backfill_jobs(
    root: Path,
    *,
    company_key: str | None = None,
    limit: int = 100,
    parse_statuses: list[str] | None = None,
    include_missing_derived: bool = True,
    process_now: bool = False,
) -> dict:
    candidates = list_parse_backfill_candidates(
        root,
        company_key=company_key,
        limit=limit,
        parse_statuses=parse_statuses,
        include_missing_derived=include_missing_derived,
    )
    queued_job_ids: list[str] = []
    existing_job_ids: list[str] = []

    with open_db(root) as conn:
        for doc in candidates:
            existing_job = conn.execute(
                "SELECT job_id FROM jobs WHERE doc_id = ? AND job_type = 'parse' AND status IN ('pending', 'running') LIMIT 1",
                (doc["doc_id"],),
            ).fetchone()
            if existing_job:
                existing_job_ids.append(existing_job["job_id"])
                continue
            force = doc.get("parse_status") == "parsed"
            job_id = ensure_parse_job_for_document(conn, doc, force=force)
            if job_id:
                queued_job_ids.append(job_id)

    processed_summary: dict[str, Any] | None = None
    if process_now and queued_job_ids:
        processed_summary = {"completed": 0, "failed": 0, "processed": len(queued_job_ids)}
        with open_db(root) as conn:
            for job_id in queued_job_ids:
                job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                if not job:
                    continue
                try:
                    run_parse_job(conn, dict(job), root=root)
                    processed_summary["completed"] += 1
                except Exception as exc:
                    processed_summary["failed"] += 1
                    log.warning("Backfill parse job %s failed: %s", job_id, exc)

    return {
        "company_key": company_key,
        "candidate_count": len(candidates),
        "queued_count": len(queued_job_ids),
        "existing_job_count": len(existing_job_ids),
        "queued_job_ids": queued_job_ids,
        "existing_job_ids": existing_job_ids,
        "processed": processed_summary,
        "candidates": [
            {
                "doc_id": item["doc_id"],
                "company_key": item.get("company_key"),
                "title": item.get("title"),
                "filename": item.get("filename"),
                "parse_status": item.get("parse_status"),
                "backfill_reason": item.get("backfill_reason"),
                "relative_path": item.get("relative_path"),
            }
            for item in candidates
        ],
    }


# ---------------------------------------------------------------------------
# Main scrape orchestrator
# ---------------------------------------------------------------------------


def run_scrape(
    spec: CompanySpec,
    *,
    root: Path,
    mode: str,
    days_back: int | None = None,
    max_docs: int | None = None,
    run_id: str | None = None,
    parse_after_download: bool = False,
) -> str:
    repair_summary = repair_stale_state(root)
    if repair_summary["runs_failed"] or repair_summary["jobs_requeued"]:
        log.info("Pre-run repair applied: %s", repair_summary)

    default_days, default_max = run_defaults(mode)
    days_back = days_back or default_days
    if not max_docs:
        # Scale max_docs if user requests a long horizon but forgets to raise the limit
        max_docs = default_max
        if days_back > 365 and max_docs < 50:
            max_docs = 50
        if days_back > 1000 and max_docs < 120:
            max_docs = 120
        if days_back > 3000 and max_docs < 200:
            max_docs = 200

    ensure_corpus_dirs(root)

    run_id = run_id or str(uuid.uuid4())
    conn = open_db(root)
    ensure_run(conn, spec, run_id=run_id, mode=mode, days_back=days_back, max_docs=max_docs)

    # Check if any source is browser-mode
    try:
        from official_source_registry import get_registry
        registry = get_registry()
        source_entries = registry.for_coverage_key(spec.coverage_key or "") if spec.coverage_key else []
    except Exception:
        source_entries = []
    browser_sources = {e.source_id for e in source_entries if e.download_mode == "browser"}
    if browser_sources:
        log.info("Note: sources %s may require browser automation for rendered pages; direct document URLs still use HTTP when possible", browser_sources)

    discovered = downloaded = skipped = errors = 0
    documents: list[dict] = []

    try:
        run_observations: dict = {}
        documents = collect_candidate_documents(
            spec, mode=mode, days_back=days_back, max_docs=max_docs, observations=run_observations
        )
        discovered = len(documents)
        update_run(conn, run_id, discovered_count=discovered, progress_message=f"discovered {discovered} candidate documents")

        save_site_learning(root, spec, documents, observations=run_observations)

        company_dir = root / spec.company_folder
        company_dir.mkdir(parents=True, exist_ok=True)

        for idx, doc in enumerate(documents, start=1):
            update_run(conn, run_id, progress_message=f"processing {idx}/{discovered}: {doc.get('title') or doc.get('url')}")
            existing = lookup_existing_document(conn, doc.get("url") or "")
            existing_file_present = bool(existing and existing.get("absolute_path") and Path(existing["absolute_path"]).exists())
            has_conditional_validators = bool(existing_file_present and conditional_request_headers(existing))

            source_entry = resolve_source_entry_for_document(doc, source_entries)
            doc_download_mode = effective_download_mode_for_document(doc, source_entry)

            # Legacy fast-path when we have a file but no validators to revalidate with.
            if existing_file_present and not has_conditional_validators:
                if parse_after_download and existing:
                    ensure_parse_job_for_document(conn, existing)
                skipped += 1
                mark_existing_seen(conn, existing, run_id)
                update_run(conn, run_id, skipped_count=skipped)
                continue

            family_dir = company_dir / family_dir_for_document(doc)
            family_dir.mkdir(parents=True, exist_ok=True)

            try:
                result = download_with_fallbacks(
                    doc["url"],
                    family_dir,
                    download_mode=doc_download_mode,
                    existing=existing if has_conditional_validators else None,
                )

                if result.get("result") == "not_modified":
                    log.info("Not modified (304): %s", doc.get("url"))
                    skipped += 1
                    if existing:
                        if parse_after_download:
                            ensure_parse_job_for_document(conn, existing)
                        mark_existing_seen(
                            conn,
                            existing,
                            run_id,
                            etag=result.get("etag"),
                            last_modified=result.get("last_modified"),
                            content_length=result.get("content_length"),
                        )
                    update_run(conn, run_id, skipped_count=skipped)
                    continue

                new_sha256 = result["sha256"]

                # Delta tracking
                delta_state = compute_delta_state(
                    existing=existing,
                    new_sha256=new_sha256,
                    conn=conn,
                    company_key=spec.company_key,
                )

                # Only save to disk if truly new or updated content
                if delta_state == "unchanged" and existing:
                    # Content unchanged — update metadata but don't re-save file
                    if os.path.exists(result["temp_path"]):
                        os.unlink(result["temp_path"])
                    skipped += 1
                    mark_existing_seen(
                        conn,
                        existing,
                        run_id,
                        etag=result.get("etag"),
                        last_modified=result.get("last_modified"),
                        content_length=result.get("content_length"),
                    )
                    update_run(conn, run_id, skipped_count=skipped)
                    continue

                basename = guess_basename(doc, result["final_url"], result["content_type"])
                filename = canonical_filename(doc, basename)
                destination = family_dir / filename
                if destination.exists():
                    stem = destination.stem
                    suffix = destination.suffix
                    destination = family_dir / f"{stem}_{new_sha256[:8]}{suffix}"
                shutil.move(result["temp_path"], destination)
                relative_path = str(destination.relative_to(root))

                doc_id = upsert_document(
                    conn,
                    spec=spec,
                    run_id=run_id,
                    doc=doc,
                    stored_path=destination,
                    relative_path=relative_path,
                    content_type=result["content_type"],
                    sha256=new_sha256,
                    size_bytes=result["size_bytes"],
                    final_url=result["final_url"],
                    filename=destination.name,
                    status="downloaded",
                    metadata={
                        "document": doc,
                        "attempts": result["attempts"],
                    },
                    content_length=result.get("content_length"),
                    etag=result.get("etag"),
                    last_modified=result.get("last_modified"),
                    download_status="downloaded",
                    delta_state=delta_state,
                )

                # Queue parse job for parseable content types
                suffix = destination.suffix.lower()
                if suffix in PARSEABLE_EXTENSIONS:
                    queue_parse_job(
                        conn,
                        doc_id=doc_id,
                        company_key=spec.company_key,
                        coverage_key=spec.coverage_key,
                    )

                downloaded += 1
                update_run(conn, run_id, downloaded_count=downloaded)

            except BrowserFallbackRequired as exc:
                log.info("Browser required for %s (recording as browser_required)", exc.url)
                upsert_document(
                    conn,
                    spec=spec,
                    run_id=run_id,
                    doc=doc,
                    stored_path=root / spec.company_folder / META_DIRNAME / "missing",
                    relative_path="",
                    content_type="",
                    sha256="",
                    size_bytes=0,
                    final_url=doc.get("url") or "",
                    filename="",
                    status="error",
                    metadata={"document": doc, "error": "browser_required"},
                    download_status="browser_required",
                    delta_state="failed_download",
                    failure_type="transient",
                )
                errors += 1
                update_run(conn, run_id, error_count=errors)

            except Exception as exc:
                errors += 1
                log.warning("Failed to download %s: %s", doc.get("url"), exc)
                upsert_document(
                    conn,
                    spec=spec,
                    run_id=run_id,
                    doc=doc,
                    stored_path=root / spec.company_folder / META_DIRNAME / "missing",
                    relative_path="",
                    content_type="",
                    sha256="",
                    size_bytes=0,
                    final_url=doc.get("url") or "",
                    filename="",
                    status="error",
                    metadata={"document": doc, "error": str(exc)},
                    download_status="error",
                    delta_state="failed_download",
                    failure_type="transient",
                )
                update_run(conn, run_id, error_count=errors)

        status = "completed" if errors == 0 else ("completed_with_errors" if downloaded or skipped else "failed")
        update_run(
            conn,
            run_id,
            status=status,
            finished_at=utc_now_iso(),
            discovered_count=discovered,
            downloaded_count=downloaded,
            skipped_count=skipped,
            error_count=errors,
            progress_message=f"finished: {downloaded} downloaded, {skipped} skipped, {errors} errors",
        )

        # Optionally run pending parse jobs inline
        if parse_after_download:
            parse_summary = process_pending_parse_jobs(root)
            log.info("Inline parse: %s", parse_summary)

        return run_id
    except Exception as exc:
        update_run(
            conn,
            run_id,
            status="failed",
            finished_at=utc_now_iso(),
            discovered_count=discovered,
            downloaded_count=downloaded,
            skipped_count=skipped,
            error_count=errors + 1,
            error_message=str(exc),
            progress_message="run failed",
        )
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the official document corpus")
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_parser = sub.add_parser("run", help="Run a corpus scrape")
    run_parser.add_argument("--coverage-key")
    run_parser.add_argument("--company-name")
    run_parser.add_argument("--ticker")
    run_parser.add_argument("--sec-cik")
    run_parser.add_argument("--ir-page")
    run_parser.add_argument("--exchange-adapter")
    run_parser.add_argument("--exchange-symbol")
    run_parser.add_argument("--exchange-code")
    run_parser.add_argument("--exchange-slug")
    run_parser.add_argument("--website-probe-url", action="append", dest="website_probe_urls")
    run_parser.add_argument("--mode", choices=["incremental", "backfill"], default="incremental")
    run_parser.add_argument("--days-back", type=int)
    run_parser.add_argument("--max-docs", type=int)
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--root", type=Path, default=OFFICIAL_DOC_CORPUS_DIR)
    run_parser.add_argument("--parse", action="store_true", help="Parse downloaded PDFs/XLSX after scrape")
    run_parser.add_argument("--json", action="store_true")

    # parse
    parse_parser = sub.add_parser("parse", help="Parse downloaded documents (run pending parse jobs)")
    parse_parser.add_argument("--root", type=Path, default=OFFICIAL_DOC_CORPUS_DIR)
    parse_parser.add_argument("--limit", type=int, default=50)
    parse_parser.add_argument("--doc-id", help="Parse a specific document by ID")
    parse_parser.add_argument("--company-key", help="Limit parse backfill to one company")
    parse_parser.add_argument(
        "--status",
        dest="parse_statuses",
        action="append",
        choices=["unparsed", "parse_failed", "parsed", "not_applicable"],
        help="When using --backfill-existing, include documents with this parse status (repeatable)",
    )
    parse_parser.add_argument("--backfill-existing", action="store_true", help="Queue parse jobs for existing corpus files, not just newly downloaded docs")
    parse_parser.add_argument("--missing-derived", action="store_true", help="When backfilling, also include parsed docs whose latest derived artifact is missing")
    parse_parser.add_argument("--process", action="store_true", help="Process the queued backfill jobs immediately")

    # list-runs
    list_runs_parser = sub.add_parser("list-runs", help="List recent runs")
    list_runs_parser.add_argument("--root", type=Path, default=OFFICIAL_DOC_CORPUS_DIR)
    list_runs_parser.add_argument("--limit", type=int, default=20)

    # list-docs
    list_docs_parser = sub.add_parser("list-docs", help="List stored documents")
    list_docs_parser.add_argument("--root", type=Path, default=OFFICIAL_DOC_CORPUS_DIR)
    list_docs_parser.add_argument("--company-key")
    list_docs_parser.add_argument("--limit", type=int, default=50)

    # list-companies
    list_companies_parser = sub.add_parser("list-companies", help="List companies in corpus")
    list_companies_parser.add_argument("--root", type=Path, default=OFFICIAL_DOC_CORPUS_DIR)

    # list-jobs
    list_jobs_parser = sub.add_parser("list-jobs", help="List job queue")
    list_jobs_parser.add_argument("--root", type=Path, default=OFFICIAL_DOC_CORPUS_DIR)
    list_jobs_parser.add_argument("--status", choices=["pending", "running", "completed", "failed"])
    list_jobs_parser.add_argument("--job-type")
    list_jobs_parser.add_argument("--limit", type=int, default=50)

    # repair
    repair_parser = sub.add_parser("repair", help="Repair stale running runs/jobs")
    repair_parser.add_argument("--root", type=Path, default=OFFICIAL_DOC_CORPUS_DIR)
    repair_parser.add_argument("--stale-minutes", type=int, default=DEFAULT_STALE_MINUTES)
    repair_parser.add_argument("--no-requeue-running-parse-jobs", action="store_true")

    # stats
    stats_parser = sub.add_parser("stats", help="Parse/delta state statistics")
    stats_parser.add_argument("--root", type=Path, default=OFFICIAL_DOC_CORPUS_DIR)

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.command == "run":
        spec = normalize_company_spec(
            coverage_key=args.coverage_key,
            company_name=args.company_name,
            ticker=args.ticker,
            sec_cik=args.sec_cik,
            ir_page=args.ir_page,
            exchange_adapter=args.exchange_adapter,
            exchange_symbol=args.exchange_symbol,
            exchange_code=args.exchange_code,
            exchange_slug=args.exchange_slug,
            website_probe_urls=args.website_probe_urls,
        )
        run_id = run_scrape(
            spec,
            root=args.root,
            mode=args.mode,
            days_back=args.days_back,
            max_docs=args.max_docs,
            run_id=args.run_id,
            parse_after_download=getattr(args, "parse", False),
        )
        payload = {"run_id": run_id, "root": str(args.root), "company_key": spec.company_key}
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"Run complete: {run_id}")
        return

    if args.command == "parse":
        if args.doc_id:
            with open_db(args.root) as conn:
                job_row = conn.execute(
                    "SELECT * FROM jobs WHERE doc_id = ? AND job_type = 'parse' AND status = 'pending' LIMIT 1",
                    (args.doc_id,),
                ).fetchone()
                if not job_row:
                    # Create ad-hoc job
                    doc_row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (args.doc_id,)).fetchone()
                    if not doc_row:
                        print(f"Document not found: {args.doc_id}", file=sys.stderr)
                        sys.exit(1)
                    doc = dict(doc_row)
                    queue_parse_job(conn, doc_id=args.doc_id, company_key=doc["company_key"], coverage_key=doc.get("coverage_key"))
                    job_row = conn.execute(
                        "SELECT * FROM jobs WHERE doc_id = ? AND job_type = 'parse' ORDER BY created_at DESC LIMIT 1",
                        (args.doc_id,),
                    ).fetchone()
                run_parse_job(conn, dict(job_row), root=args.root)
            print(f"Parsed document {args.doc_id}")
        elif args.backfill_existing:
            result = queue_parse_backfill_jobs(
                args.root,
                company_key=args.company_key,
                limit=args.limit,
                parse_statuses=args.parse_statuses,
                include_missing_derived=args.missing_derived or not args.parse_statuses,
                process_now=args.process,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            result = process_pending_parse_jobs(args.root, limit=args.limit)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.command == "list-runs":
        print(json.dumps(list_runs(args.root, limit=args.limit), indent=2, ensure_ascii=False))
        return

    if args.command == "list-docs":
        print(json.dumps(list_documents(args.root, company_key=args.company_key, limit=args.limit), indent=2, ensure_ascii=False))
        return

    if args.command == "list-companies":
        print(json.dumps(list_companies(args.root), indent=2, ensure_ascii=False))
        return

    if args.command == "list-jobs":
        print(json.dumps(list_jobs(args.root, status=args.status, job_type=args.job_type, limit=args.limit), indent=2, ensure_ascii=False))
        return

    if args.command == "repair":
        print(json.dumps(
            repair_stale_state(
                args.root,
                stale_minutes=args.stale_minutes,
                requeue_running_parse_jobs=not args.no_requeue_running_parse_jobs,
            ),
            indent=2,
            ensure_ascii=False,
        ))
        return

    if args.command == "stats":
        print(json.dumps(get_parse_stats(args.root), indent=2, ensure_ascii=False))
        return

    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
