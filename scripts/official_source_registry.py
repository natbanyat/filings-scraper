"""
Source registry for the official document ingestion pipeline.

Provides a durable, YAML-backed registry of sources that supplements
TICKER_META with crawl-specific configuration (seed URLs, allowed domains,
crawl mode, download mode, poll frequency, parse profile, etc.).

Registry file: scripts/official_source_registry.yaml
Auto-bootstrapped from TICKER_META on first run if the YAML is missing.

Usage:
    from official_source_registry import get_registry, SourceEntry

    registry = get_registry()
    entries = registry.for_coverage_key("tickers/JPM")
    all_sources = registry.all_enabled()
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("official_source_registry")

REGISTRY_PATH = Path(__file__).parent / "official_source_registry.yaml"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SourceEntry:
    """A single crawlable source in the official-document pipeline."""

    source_id: str
    """Stable identifier: '<coverage_key>/<role>', e.g. 'tickers/JPM/sec'."""

    coverage_key: str
    """Owning coverage key, e.g. 'tickers/JPM'."""

    company: str
    """Human-readable company/entity name."""

    domain: str
    """Primary domain being crawled, e.g. 'sec.gov'."""

    seed_urls: list[str] = field(default_factory=list)
    """URLs to start discovery from."""

    allowed_domains: list[str] = field(default_factory=list)
    """Domains that may be followed during crawl. Empty = domain only."""

    crawl_mode: str = "listing"
    """Discovery strategy: api | listing | sitemap | browser."""

    download_mode: str = "http"
    """Download strategy: http | browser | mixed."""

    file_types: list[str] = field(default_factory=lambda: ["pdf", "html"])
    """Accepted file extensions (without dot)."""

    auth_profile: str | None = None
    """Named auth profile for authenticated sources (None = public)."""

    poll_frequency: str = "daily"
    """How often to poll: daily | weekly | on_event."""

    parse_profile: str = "generic"
    """Parser hint: sec_filing | ir_page | generic."""

    priority: int = 5
    """Lower number = higher priority (1 = highest)."""

    owner: str = "auto"
    """Which adapter owns this source: sec_adapter | lse_adapter | hkex_adapter | tse_adapter | ir_scraper | auto."""

    enabled: bool = True
    """Set False to disable without deleting the entry."""

    notes: list[str] = field(default_factory=list)
    """Human-readable notes about this source."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SourceRegistry:
    """Loads and queries the YAML source registry."""

    def __init__(self, path: Path = REGISTRY_PATH) -> None:
        self._path = path
        self._entries: dict[str, SourceEntry] = {}
        self._load()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            log.info("Registry file not found at %s, bootstrapping from TICKER_META", self._path)
            self._bootstrap()
            return
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            log.warning("PyYAML not installed; using empty registry. Install pyyaml to enable registry.")
            return
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            log.error("Failed to load registry from %s: %s", self._path, exc)
            return
        _fields = set(SourceEntry.__dataclass_fields__.keys())
        for item in raw.get("sources", []):
            if not isinstance(item, dict):
                continue
            filtered = {k: v for k, v in item.items() if k in _fields}
            try:
                entry = SourceEntry(**filtered)
                self._entries[entry.source_id] = entry
            except TypeError as exc:
                log.warning("Skipping malformed registry entry %r: %s", item.get("source_id"), exc)

    def _bootstrap(self) -> None:
        """Build default entries from TICKER_META and save to YAML."""
        from config import TICKER_META  # lazy import to avoid circular
        entries = _build_entries_from_ticker_meta(TICKER_META)
        self._save_entries(entries)
        for entry in entries:
            self._entries[entry.source_id] = entry
        log.info("Bootstrapped %d source entries from TICKER_META", len(entries))

    def reload(self) -> None:
        """Re-read the registry from disk."""
        self._entries.clear()
        self._load()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, source_id: str) -> SourceEntry | None:
        return self._entries.get(source_id)

    def for_coverage_key(self, coverage_key: str) -> list[SourceEntry]:
        return [e for e in self._entries.values() if e.coverage_key == coverage_key]

    def all_enabled(self) -> list[SourceEntry]:
        return [e for e in self._entries.values() if e.enabled]

    def all(self) -> list[SourceEntry]:
        return list(self._entries.values())

    def as_list(self) -> list[dict]:
        return [asdict(e) for e in self._entries.values()]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_entries(self, entries: list[SourceEntry]) -> None:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            log.warning("PyYAML not installed; cannot save registry YAML")
            return
        payload = {"sources": [asdict(e) for e in entries]}
        self._path.write_text(
            yaml.dump(payload, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    def save(self) -> None:
        self._save_entries(list(self._entries.values()))


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


def _primary_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lstrip("www.") or url
    except Exception:
        return url


def _build_entries_from_ticker_meta(ticker_meta: dict[str, dict]) -> list[SourceEntry]:
    """Convert TICKER_META entries into SourceEntry objects."""
    entries: list[SourceEntry] = []

    for coverage_key, meta in ticker_meta.items():
        company = meta.get("company_name") or coverage_key.split("/")[-1]
        adapter = meta.get("exchange_adapter")
        sec_cik = meta.get("sec_cik")
        ir_page = meta.get("ir_page")
        exchange_code = meta.get("exchange_code")
        exchange_symbol = meta.get("exchange_symbol")
        exchange_slug = meta.get("exchange_slug")
        probe_urls = meta.get("website_probe_urls") or ([ir_page] if ir_page else [])

        # --- Exchange / filing adapter source ---
        if adapter == "sec" and sec_cik:
            cik_padded = sec_cik.lstrip("0").zfill(10)
            entries.append(SourceEntry(
                source_id=f"{coverage_key}/sec",
                coverage_key=coverage_key,
                company=company,
                domain="sec.gov",
                seed_urls=[f"https://data.sec.gov/submissions/CIK{cik_padded}.json"],
                allowed_domains=["sec.gov", "data.sec.gov", "www.sec.gov"],
                crawl_mode="api",
                download_mode="http",
                file_types=["pdf", "html", "htm", "txt"],
                poll_frequency="daily",
                parse_profile="sec_filing",
                priority=1,
                owner="sec_adapter",
                notes=[f"SEC EDGAR CIK: {sec_cik}"],
            ))
        elif adapter == "lse":
            lse_seeds = []
            if exchange_symbol and exchange_slug:
                lse_seeds.append(
                    f"https://www.londonstockexchange.com/stock/{exchange_symbol}/{exchange_slug}/company-page"
                )
            entries.append(SourceEntry(
                source_id=f"{coverage_key}/lse",
                coverage_key=coverage_key,
                company=company,
                domain="londonstockexchange.com",
                seed_urls=lse_seeds,
                allowed_domains=["londonstockexchange.com", "rns-pdf.londonstockexchange.com"],
                crawl_mode="listing",
                download_mode="browser",  # LSE company pages are JS-rendered
                file_types=["pdf"],
                poll_frequency="daily",
                parse_profile="generic",
                priority=2,
                owner="lse_adapter",
                enabled=True,
                notes=[
                    "LSE/RNS company pages are JS-rendered; use browser mode for rendered pages, but direct RNS PDF asset URLs can still download over HTTP once discovered.",
                    "IR archive is currently the practical source for results, presentations, and data packs.",
                ],
            ))
        elif adapter == "hkex":
            entries.append(SourceEntry(
                source_id=f"{coverage_key}/hkex",
                coverage_key=coverage_key,
                company=company,
                domain="hkexnews.hk",
                seed_urls=["https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en"],
                allowed_domains=["hkexnews.hk", "www1.hkexnews.hk"],
                crawl_mode="listing",
                download_mode="http",
                file_types=["pdf"],
                poll_frequency="daily",
                parse_profile="generic",
                priority=2,
                owner="hkex_adapter",
                enabled=True,
                notes=[f"HKEX company code: {exchange_code or 'unknown'}"],
            ))
        elif adapter == "tse":
            entries.append(SourceEntry(
                source_id=f"{coverage_key}/tse",
                coverage_key=coverage_key,
                company=company,
                domain="jpx.co.jp",
                seed_urls=[
                    "https://www.jpx.co.jp/english/listing/disclosure/index.html",
                ],
                allowed_domains=["jpx.co.jp", "www.jpx.co.jp", "disclosure2.edinet-fsa.go.jp"],
                crawl_mode="listing",
                download_mode="http",
                file_types=["pdf"],
                poll_frequency="daily",
                parse_profile="generic",
                priority=2,
                owner="tse_adapter",
                enabled=True,
                notes=[
                    f"TSE/TDnet code: {exchange_code or 'unknown'}",
                    "Full TDnet API is paid; IR archive is the practical source today.",
                ],
            ))

        # --- IR page source (always add if ir_page exists) ---
        if ir_page:
            ir_domain = _primary_domain(ir_page)
            entries.append(SourceEntry(
                source_id=f"{coverage_key}/ir",
                coverage_key=coverage_key,
                company=company,
                domain=ir_domain,
                seed_urls=list(dict.fromkeys([ir_page] + list(probe_urls))),
                allowed_domains=[urlparse(ir_page).netloc] if ir_page else [],
                crawl_mode="listing",
                download_mode="http",
                file_types=["pdf", "xlsx", "xls", "html"],
                poll_frequency="daily",
                parse_profile="ir_page",
                priority=3 if adapter else 2,
                owner="ir_scraper",
                enabled=True,
                notes=[f"IR page: {ir_page}"],
            ))

    return entries


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: SourceRegistry | None = None


def get_registry(*, reload: bool = False) -> SourceRegistry:
    """Return the module-level registry singleton, loading on first call."""
    global _registry
    if _registry is None or reload:
        _registry = SourceRegistry(REGISTRY_PATH)
    return _registry


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Official source registry management")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all registry entries")
    sub.add_parser("bootstrap", help="Re-bootstrap registry from TICKER_META")

    show_parser = sub.add_parser("show", help="Show entries for a coverage key")
    show_parser.add_argument("coverage_key")

    args = parser.parse_args()

    reg = get_registry()

    if args.command == "list":
        print(json.dumps(reg.as_list(), indent=2, ensure_ascii=False))

    elif args.command == "bootstrap":
        if REGISTRY_PATH.exists():
            REGISTRY_PATH.unlink()
        reg = get_registry(reload=True)
        print(f"Bootstrapped {len(reg.all())} entries to {REGISTRY_PATH}")

    elif args.command == "show":
        entries = reg.for_coverage_key(args.coverage_key)
        if not entries:
            print(f"No entries for {args.coverage_key}")
        else:
            print(json.dumps([asdict(e) for e in entries], indent=2, ensure_ascii=False))
