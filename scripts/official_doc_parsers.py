"""
Parser/normalization workers for official document corpus.

Supports:
  - PDF: native text extraction (pypdf), optional table extraction (pdfplumber),
         OCR-needed detection via text-density heuristic.
  - XLSX: workbook metadata, per-sheet extraction, hidden/protected sheet
           detection (openpyxl).

Parse artifacts are saved as JSON files alongside the corpus manifest, under
<company_folder>/_meta/parses/<doc_id>.json.

Usage:
    from official_doc_parsers import parse_document, save_parse_artifact
    from pathlib import Path

    result = parse_document(Path("/corpus/JPM/annual_reports/2025-01-15_annual.pdf"))
    print(result.quality_flags)    # e.g. ["tables_found", "low_text_density"]
    save_parse_artifact(result, Path("/corpus/JPM/_meta/parses/abc123.json"))
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("official_doc_parsers")

# Text density threshold: pages with fewer chars/page than this are flagged
# as likely scanned / low-text.
PDF_LOW_TEXT_DENSITY_CHARS_PER_PAGE = 150
# Maximum pages to read text from (performance guard for very large PDFs)
PDF_MAX_PAGES = 500
# Maximum rows to preview per sheet in XLSX output
XLSX_PREVIEW_ROWS = 10


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """Normalized output from any document parser."""

    parser_name: str
    """Which parser produced this result: pdf_native | xlsx | unknown."""

    ok: bool
    """True if parse succeeded (even partially)."""

    text: str = ""
    """Extracted full text (may be truncated for very large docs)."""

    page_count: int | None = None
    """Page count for PDFs; None for non-paginated formats."""

    table_count: int | None = None
    """Number of tables/sheets extracted; None if not attempted."""

    quality_flags: list[str] = field(default_factory=list)
    """
    Flags describing parse quality or characteristics:
      - ocr_needed         : PDF has too little native text (likely scanned)
      - low_text_density   : < PDF_LOW_TEXT_DENSITY_CHARS_PER_PAGE chars/page on average
      - tables_found       : pdfplumber found >=1 table
      - has_hidden_sheets  : XLSX has at least one hidden sheet
      - has_protected_sheet: XLSX has at least one protected sheet
      - partial_text       : text was truncated due to page/size limit
      - empty_output       : parser returned no text at all
    """

    metadata: dict = field(default_factory=dict)
    """File-level metadata: author, title, creator, dates, sheet names, etc."""

    tables: list[dict] = field(default_factory=list)
    """
    Extracted tables. For PDFs: list of {page, table_index, headers, rows}.
    For XLSX: list of {sheet, headers, rows, row_count}.
    Kept lean (preview rows only) to avoid bloating the artifact.
    """

    error: str | None = None
    """Error message if parse failed or was partial."""

    source_path: str = ""
    """Absolute path of the source file that was parsed."""


# ---------------------------------------------------------------------------
# PDF parser
# ---------------------------------------------------------------------------


class PDFParser:
    """
    Parse PDFs using pypdf for text, optional pdfplumber for tables.

    Gracefully degrades if pdfplumber is not installed (text-only mode).
    """

    def parse(self, path: Path) -> ParseResult:
        source = str(path)
        try:
            import pypdf  # type: ignore[import-untyped]
        except ImportError:
            return ParseResult(
                parser_name="pdf_native",
                ok=False,
                error="pypdf not installed",
                source_path=source,
            )

        pages_text: list[str] = []
        metadata: dict = {}
        page_count: int | None = None
        quality_flags: list[str] = []
        error: str | None = None

        try:
            reader = pypdf.PdfReader(str(path))
            page_count = len(reader.pages)
            # Extract metadata
            meta = reader.metadata or {}
            metadata = {
                "title": str(meta.get("/Title") or ""),
                "author": str(meta.get("/Author") or ""),
                "creator": str(meta.get("/Creator") or ""),
                "producer": str(meta.get("/Producer") or ""),
                "created": str(meta.get("/CreationDate") or ""),
                "modified": str(meta.get("/ModDate") or ""),
                "page_count": page_count,
            }
            # Extract text (up to PDF_MAX_PAGES)
            pages_to_read = min(page_count, PDF_MAX_PAGES)
            for i in range(pages_to_read):
                try:
                    pages_text.append(reader.pages[i].extract_text() or "")
                except Exception:
                    pages_text.append("")
            if page_count > PDF_MAX_PAGES:
                quality_flags.append("partial_text")
        except Exception as exc:
            error = f"pypdf extraction error: {exc}"
            log.warning("PDF parse failed for %s: %s", path.name, exc)

        full_text = "\n".join(pages_text)
        if not full_text.strip():
            quality_flags.append("empty_output")

        # Text density check
        if page_count and page_count > 0:
            chars_per_page = len(full_text) / max(page_count, 1)
            if chars_per_page < PDF_LOW_TEXT_DENSITY_CHARS_PER_PAGE:
                quality_flags.append("low_text_density")
                quality_flags.append("ocr_needed")

        # Table extraction via pdfplumber (optional)
        tables: list[dict] = []
        table_count: int | None = None
        try:
            import pdfplumber  # type: ignore[import-untyped]
            tables, table_count = self._extract_tables_pdfplumber(path)
            if table_count and table_count > 0:
                quality_flags.append("tables_found")
        except ImportError:
            pass  # pdfplumber not installed — text-only mode
        except Exception as exc:
            log.debug("pdfplumber table extraction failed for %s: %s", path.name, exc)

        return ParseResult(
            parser_name="pdf_native",
            ok=error is None or bool(full_text),
            text=full_text,
            page_count=page_count,
            table_count=table_count,
            quality_flags=quality_flags,
            metadata=metadata,
            tables=tables,
            error=error,
            source_path=source,
        )

    def _extract_tables_pdfplumber(self, path: Path) -> tuple[list[dict], int]:
        """Return (table_list, total_count). Each entry has page, table_index, headers, rows."""
        import pdfplumber  # type: ignore[import-untyped]

        tables: list[dict] = []
        total = 0
        try:
            with pdfplumber.open(str(path)) as pdf:
                for page_num, page in enumerate(pdf.pages[:PDF_MAX_PAGES], start=1):
                    page_tables = page.extract_tables() or []
                    for t_idx, raw_table in enumerate(page_tables):
                        total += 1
                        if not raw_table:
                            continue
                        rows = [[str(cell or "") for cell in row] for row in raw_table]
                        headers = rows[0] if rows else []
                        data_rows = rows[1:XLSX_PREVIEW_ROWS + 1] if len(rows) > 1 else []
                        tables.append({
                            "page": page_num,
                            "table_index": t_idx,
                            "headers": headers,
                            "rows": data_rows,
                            "total_rows": len(rows) - 1,
                        })
        except Exception as exc:
            log.debug("pdfplumber open failed for %s: %s", path.name, exc)
        return tables, total


# ---------------------------------------------------------------------------
# XLSX parser
# ---------------------------------------------------------------------------


class XLSXParser:
    """Parse XLSX/XLS workbooks using openpyxl."""

    def parse(self, path: Path) -> ParseResult:
        source = str(path)
        try:
            import openpyxl  # type: ignore[import-untyped]
        except ImportError:
            return ParseResult(
                parser_name="xlsx",
                ok=False,
                error="openpyxl not installed",
                source_path=source,
            )

        quality_flags: list[str] = []
        metadata: dict = {}
        tables: list[dict] = []
        text_parts: list[str] = []
        table_count = 0

        try:
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            props = wb.properties
            metadata = {
                "title": str(props.title or ""),
                "creator": str(props.creator or ""),
                "description": str(props.description or ""),
                "created": str(props.created or ""),
                "modified": str(props.modified or ""),
                "sheet_names": wb.sheetnames,
                "sheet_count": len(wb.sheetnames),
            }

            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                # Check hidden
                state = getattr(ws, "sheet_state", "visible")
                is_hidden = state != "visible"
                if is_hidden:
                    quality_flags.append("has_hidden_sheets")

                # Check protected (openpyxl may not expose this on read_only, try anyway)
                protected = getattr(getattr(ws, "protection", None), "sheet", False)
                if protected:
                    quality_flags.append("has_protected_sheet")

                # Extract rows (up to XLSX_PREVIEW_ROWS for tables list, full text for text)
                all_rows: list[list[str]] = []
                row_count = 0
                for row in ws.iter_rows(values_only=True):
                    if not any(cell is not None for cell in row):
                        continue
                    all_rows.append([str(cell if cell is not None else "") for cell in row])
                    row_count += 1

                # Text representation of sheet
                sheet_text = f"[Sheet: {sheet_name}]\n"
                sheet_text += "\n".join("\t".join(row) for row in all_rows)
                text_parts.append(sheet_text)

                # Table record (preview)
                headers = all_rows[0] if all_rows else []
                preview_rows = all_rows[1:XLSX_PREVIEW_ROWS + 1] if len(all_rows) > 1 else []
                tables.append({
                    "sheet": sheet_name,
                    "hidden": is_hidden,
                    "headers": headers,
                    "rows": preview_rows,
                    "row_count": row_count - 1 if row_count > 0 else 0,
                })
                table_count += 1

            wb.close()
        except Exception as exc:
            err = f"openpyxl parse error: {exc}\n{traceback.format_exc()}"
            log.warning("XLSX parse failed for %s: %s", path.name, exc)
            return ParseResult(
                parser_name="xlsx",
                ok=False,
                error=err,
                source_path=source,
            )

        full_text = "\n\n".join(text_parts)
        if not full_text.strip():
            quality_flags.append("empty_output")

        return ParseResult(
            parser_name="xlsx",
            ok=True,
            text=full_text,
            page_count=None,
            table_count=table_count,
            quality_flags=list(dict.fromkeys(quality_flags)),  # deduplicate
            metadata=metadata,
            tables=tables,
            error=None,
            source_path=source,
        )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_PDF_PARSER = PDFParser()
_XLSX_PARSER = XLSXParser()


def parse_document(path: Path, content_type: str = "") -> ParseResult:
    """
    Route a document to the appropriate parser.

    Dispatch order:
      1. Extension-based (.pdf → PDFParser, .xlsx/.xls → XLSXParser)
      2. Content-type based if extension is ambiguous
      3. Unknown → returns ParseResult(ok=False, parser_name="unknown")
    """
    suffix = path.suffix.lower()
    ct_lower = content_type.lower()

    if suffix == ".pdf" or "pdf" in ct_lower:
        return _PDF_PARSER.parse(path)

    if suffix in {".xlsx", ".xls"} or any(
        token in ct_lower for token in ["spreadsheetml", "excel", "ms-excel"]
    ):
        return _XLSX_PARSER.parse(path)

    # HTML/text: return a stub (text parsing is handled inline in fetcher)
    if suffix in {".html", ".htm", ".txt"} or "html" in ct_lower or "text/" in ct_lower:
        return ParseResult(
            parser_name="text",
            ok=True,
            text="",
            quality_flags=["text_not_parsed"],
            source_path=str(path),
            error=None,
        )

    return ParseResult(
        parser_name="unknown",
        ok=False,
        error=f"No parser for extension '{suffix}' / content-type '{content_type}'",
        source_path=str(path),
    )


# ---------------------------------------------------------------------------
# Artifact persistence
# ---------------------------------------------------------------------------


def save_parse_artifact(result: ParseResult, artifact_path: Path) -> None:
    """
    Save a ParseResult as a JSON file.

    Truncates the full text field to 100 KB in the artifact to keep files
    manageable; the tables list is kept as-is (already preview-limited).
    """
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(result)
    # Truncate full text in artifact — full text lives in the raw file
    max_text = 100_000
    if len(data.get("text", "")) > max_text:
        data["text"] = data["text"][:max_text] + "\n... [truncated]"
        data["quality_flags"] = list(dict.fromkeys(data.get("quality_flags", []) + ["artifact_text_truncated"]))
    artifact_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_parse_artifact(artifact_path: Path) -> ParseResult | None:
    """Load a previously saved ParseResult artifact. Returns None if not found."""
    if not artifact_path.exists():
        return None
    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
        return ParseResult(**{k: v for k, v in data.items() if k in ParseResult.__dataclass_fields__})
    except Exception as exc:
        log.warning("Failed to load parse artifact %s: %s", artifact_path, exc)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Parse a document and print parse result")
    parser.add_argument("path", type=Path, help="Path to document to parse")
    parser.add_argument("--content-type", default="", help="Content-Type hint")
    parser.add_argument("--save", type=Path, metavar="ARTIFACT_PATH", help="Save result JSON to path")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"File not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    result = parse_document(args.path, args.content_type)
    data = asdict(result)
    # Don't dump all text to stdout
    data["text"] = data["text"][:2000] + ("..." if len(data["text"]) > 2000 else "")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    if args.save:
        save_parse_artifact(result, args.save)
        print(f"\nArtifact saved to {args.save}", file=sys.stderr)
