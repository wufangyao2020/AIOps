#!/usr/bin/env python3
"""Validate the generated A-share archive before committing it."""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "data" / "2026H1" / "a_share_2026_h1_master.csv"
SUMMARY = ROOT / "meta" / "snapshot_2026_h1.json"
MIN_ACCEPTABLE_ROWS = 5400


def main() -> int:
    if not MASTER.exists() or not SUMMARY.exists():
        print("Missing archive outputs", file=sys.stderr)
        return 1

    with MASTER.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))

    codes = [row["security_code"] for row in rows]
    duplicates = [code for code, count in Counter(codes).items() if count > 1]
    checks = {
        "row_count": len(rows),
        "summary_row_count": summary.get("archived_unique_company_count"),
        "minimum_rows_ok": len(rows) >= MIN_ACCEPTABLE_ROWS,
        "row_count_matches_summary": len(rows) == summary.get("archived_unique_company_count"),
        "duplicate_code_count": len(duplicates),
        "unknown_exchange_count": sum(row["exchange"] == "UNKNOWN" for row in rows),
        "missing_name_count": sum(not row["security_name"] for row in rows),
        "missing_announcement_date_count": sum(not row["announcement_date"] for row in rows),
        "invalid_code_count": sum(len(code) != 6 or not code.isdigit() for code in codes),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))

    hard_fail = (
        not checks["minimum_rows_ok"]
        or not checks["row_count_matches_summary"]
        or checks["duplicate_code_count"] > 0
        or checks["missing_name_count"] > 0
        or checks["invalid_code_count"] > 0
    )
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
