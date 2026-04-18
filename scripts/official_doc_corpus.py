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
MAX_DOWNLOAD_BYTES = 75 * 1024 * 1024
DEFAULT_INCREMENTAL_DAYS = 400
DEFAULT_INCREMENTAL_MAX_DOCS = 25
DEFAULT_BACKFILL_DAYS = 3650
DEFAULT_BACKFILL_MAX_DOCS = 120

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
    conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


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
    query = "SELECT * FROM documents"
    params: list[Any] = []
    if company_key:
        query += " WHERE company_key = ?"
        params.append(company_key)
    query += " ORDER BY COALESCE(published_at, last_seen_at, downloaded_at) DESC LIMIT ?"
    params.append(limit)
    with open_db(root) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


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
    return [dict(row) for row in rows]


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
    return {
        "parse_status": {row["parse_status"] or "null": row["cnt"] for row in rows},
        "delta_state": {row["delta_state"] or "null": row["cnt"] for row in delta_rows},
        "jobs": {row["status"]: row["cnt"] for row in job_rows},
    }


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

    return CompanySpec(
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


def collect_candidate_documents(spec: CompanySpec, *, mode: str, days_back: int, max_docs: int) -> list[dict]:
    docs: list[dict] = []

    if spec.coverage_key and spec.exchange_adapter:
        docs.extend(fetch_exchange_documents(spec.coverage_key, days_back=days_back, limit=max_docs))
    elif spec.sec_cik:
        docs.extend(fetch_sec_recent_documents(spec.sec_cik, days_back=days_back, limit=max_docs))

    if spec.ir_page:
        docs.extend(fetch_ir_recent_documents(spec.ir_page, spec.company_name, days_back=days_back, limit=max_docs))

    docs = dedupe_documents(docs)
    if len(docs) > max_docs:
        docs = docs[:max_docs]
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
        raise BrowserFallbackRequired(url)

    attempts: list[dict] = []
    last_error: str | None = None
    target_dir.mkdir(parents=True, exist_ok=True)

    for candidate_url in url_variants(url):
        for headers in header_variants(candidate_url):
            resp = None
            temp_path: str | None = None
            header_label = "edgar" if headers is HEADERS_EDGAR else "web"
            try:
                resp = requests.get(candidate_url, headers=headers, stream=True, timeout=30, allow_redirects=True)
                resp.raise_for_status()

                # Capture response metadata
                etag = resp.headers.get("ETag") or resp.headers.get("etag")
                last_modified = resp.headers.get("Last-Modified") or resp.headers.get("last-modified")
                content_length_str = resp.headers.get("Content-Length") or resp.headers.get("content-length")
                content_length: int | None = int(content_length_str) if content_length_str and content_length_str.isdigit() else None

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

    raise RuntimeError(last_error or f"download failed for {url}")


# ---------------------------------------------------------------------------
# Site learning
# ---------------------------------------------------------------------------


def save_site_learning(root: Path, spec: CompanySpec, documents: list[dict]) -> None:
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
        "Sources configured as download_mode=browser are scaffolded but not yet fetched; set download_mode=http or mixed to enable HTTP-first download.",
    ]

    payload = {
        "company": asdict(spec),
        "generated_at": utc_now_iso(),
        "exchange_probe": exchange_probe,
        "website_probes": website_probes,
        "pattern_notes": pattern_notes,
        "fallback_notes": fallback_notes,
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
    parse_status: str = "unparsed",
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
        download_status = status

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


def mark_existing_seen(conn: sqlite3.Connection, existing: dict, run_id: str) -> None:
    conn.execute(
        "UPDATE documents SET last_run_id = ?, last_seen_at = ?, status = ?, delta_state = ? WHERE doc_id = ?",
        (run_id, utc_now_iso(), "existing", "unchanged", existing["doc_id"]),
    )
    conn.commit()


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
    from official_doc_parsers import parse_document, save_parse_artifact

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
        conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, error = ?, failure_type = 'transient' WHERE job_id = ?",
            (utc_now_iso(), str(exc), job_id),
        )
        conn.commit()
        return

    # Save artifact
    company_folder = doc.get("company_folder") or ""
    artifact_dir = root / company_folder / META_DIRNAME / PARSES_DIRNAME
    artifact_path = artifact_dir / f"{doc_id}.json"
    try:
        save_parse_artifact(result, artifact_path)
    except Exception as exc:
        log.warning("Failed to save parse artifact for %s: %s", doc_id, exc)

    # Upsert parse record
    parse_id = str(uuid.uuid4())
    quality_flags_json = json.dumps(result.quality_flags)
    conn.execute(
        """
        INSERT OR REPLACE INTO document_parses
        (parse_id, doc_id, parsed_at, parser_name, parse_version,
         text_chars, page_count, table_count, quality_flags, error, artifact_path)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
        """,
        (
            parse_id, doc_id, utc_now_iso(), result.parser_name,
            len(result.text), result.page_count, result.table_count,
            quality_flags_json, result.error, str(artifact_path) if artifact_path.exists() else None,
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
            "UPDATE documents SET parse_status = ?, parse_quality_flags = ? WHERE doc_id = ?",
            (parse_status, quality_flags_json, doc_id),
        )
    conn.commit()

    # Mark job complete
    result_summary = {
        "parse_id": parse_id,
        "parser_name": result.parser_name,
        "ok": result.ok,
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
    return {"completed": completed, "failed": failed, "processed": len(jobs)}


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
    default_days, default_max = run_defaults(mode)
    days_back = days_back or default_days
    max_docs = max_docs or default_max
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
        log.info("Note: sources %s require browser automation (scaffolded, not yet active)", browser_sources)

    discovered = downloaded = skipped = errors = 0
    documents: list[dict] = []

    try:
        documents = collect_candidate_documents(spec, mode=mode, days_back=days_back, max_docs=max_docs)
        discovered = len(documents)
        update_run(conn, run_id, discovered_count=discovered, progress_message=f"discovered {discovered} candidate documents")

        save_site_learning(root, spec, documents)

        company_dir = root / spec.company_folder
        company_dir.mkdir(parents=True, exist_ok=True)

        for idx, doc in enumerate(documents, start=1):
            update_run(conn, run_id, progress_message=f"processing {idx}/{discovered}: {doc.get('title') or doc.get('url')}")
            existing = lookup_existing_document(conn, doc.get("url") or "")

            # If unchanged (file exists + sha256 known), skip download
            if existing and existing.get("absolute_path") and Path(existing["absolute_path"]).exists():
                skipped += 1
                mark_existing_seen(conn, existing, run_id)
                update_run(conn, run_id, skipped_count=skipped)
                continue

            family_dir = company_dir / family_dir_for_document(doc)
            family_dir.mkdir(parents=True, exist_ok=True)

            # Determine download_mode from source registry
            doc_download_mode = "http"
            # (Can be extended: look up source entry by URL domain and check download_mode)

            try:
                result = download_with_fallbacks(doc["url"], family_dir, download_mode=doc_download_mode)
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
                    mark_existing_seen(conn, existing, run_id)
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
                    parse_status="unparsed",
                    delta_state=delta_state,
                )

                # Queue parse job for parseable content types
                suffix = destination.suffix.lower()
                if suffix in {".pdf", ".xlsx", ".xls"}:
                    queue_parse_job(
                        conn,
                        doc_id=doc_id,
                        company_key=spec.company_key,
                        coverage_key=spec.coverage_key,
                    )

                downloaded += 1
                update_run(conn, run_id, downloaded_count=downloaded)

            except BrowserFallbackRequired as exc:
                log.info("Browser required for %s (not yet active; recording as browser_required)", exc.url)
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
    list_jobs_parser.add_argument("--limit", type=int, default=50)

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
        print(json.dumps(list_jobs(args.root, status=args.status, limit=args.limit), indent=2, ensure_ascii=False))
        return

    if args.command == "stats":
        print(json.dumps(get_parse_stats(args.root), indent=2, ensure_ascii=False))
        return

    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
