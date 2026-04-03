"""
Backfill research/council-sessions/index.json from existing session directories.

Reads SUMMARY.md and 00-decision-contract.json from each session to reconstruct
the index entries.  Run once after deploying council_ui; subsequent runs by
council.py keep the index up to date automatically.

Usage:
  python council_ui/backfill_index.py
  python council_ui/backfill_index.py --dry-run
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR   = WORKSPACE_ROOT / "research" / "council-sessions"
INDEX_PATH     = SESSIONS_DIR / "index.json"


def _extract_verdict(summary_md: str) -> tuple[str, str]:
    verdict = "UNKNOWN"
    m = re.search(r"^## Answer\s*\n+\*?\*?([A-Z]+)\b", summary_md, re.MULTILINE)
    if m:
        verdict = m.group(1)

    confidence = "UNKNOWN"
    m = re.search(r"[Oo]verall confidence[:\s]+([A-Z][A-Z\-]+)", summary_md)
    if m:
        confidence = m.group(1).rstrip(".")

    return verdict, confidence


def backfill(dry_run: bool = False) -> None:
    entries: list[dict] = []

    for session_dir in sorted(SESSIONS_DIR.iterdir()):
        if not session_dir.is_dir():
            continue
        if session_dir.name == "index.json":
            continue

        summary_path  = session_dir / "SUMMARY.md"
        contract_path = session_dir / "00-decision-contract.json"

        if not summary_path.exists():
            print(f"  skip {session_dir.name} — no SUMMARY.md")
            continue

        summary_md = summary_path.read_text(encoding="utf-8")
        verdict, confidence = _extract_verdict(summary_md)

        # Parse date and slug from directory name (YYYY-MM-DD-<slug>)
        parts = session_dir.name.split("-", 3)
        date_str = "-".join(parts[:3]) if len(parts) >= 3 else "unknown"

        # Extract question from SUMMARY.md title or contract
        question = ""
        m = re.match(r"^#\s+Council Summary[:\s]*(.+)$", summary_md, re.MULTILINE)
        if m:
            question = m.group(1).strip()

        if not question and contract_path.exists():
            try:
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                # contract doesn't store the raw question directly, derive from slug
            except Exception:
                pass

        if not question:
            # Reconstruct question from slug
            question = session_dir.name[11:].replace("-", " ") if len(session_dir.name) > 11 else session_dir.name

        # Depth/agents from contract if available
        depth, n_agents, n_rounds = "FULL", 5, 2
        if contract_path.exists():
            try:
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                dc = contract.get("decision_contract", {})
                depth   = dc.get("council_depth", "FULL")
                n_rounds_map = {"LIGHT": 1, "STANDARD": 1, "FULL": 2}
                n_agents_map = {"LIGHT": 3, "STANDARD": 5, "FULL": 5}
                n_rounds = n_rounds_map.get(depth, 2)
                n_agents = n_agents_map.get(depth, 5)
            except Exception:
                pass

        entry = {
            "session_id":   session_dir.name,
            "date":         date_str,
            "question":     question,
            "depth":        depth,
            "n_agents":     n_agents,
            "n_rounds":     n_rounds,
            "verdict":      verdict,
            "confidence":   confidence,
            "session_dir":  str(session_dir.relative_to(WORKSPACE_ROOT)),
            "summary_path": str(summary_path.relative_to(WORKSPACE_ROOT)),
            "has_dashboard": (session_dir / "dashboard.html").exists(),
            "has_passover":  (session_dir / "passover.json").exists(),
            "created_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        entries.append(entry)
        print(f"  indexed {session_dir.name}  verdict={verdict}  confidence={confidence}")

    entries.sort(key=lambda e: (e.get("date", ""), e.get("session_id", "")), reverse=True)

    print(f"\nTotal: {len(entries)} session(s)")

    if dry_run:
        print("\n[dry-run] Would write:")
        print(json.dumps(entries, indent=2))
        return

    INDEX_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    print(f"Written: {INDEX_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill council index.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
