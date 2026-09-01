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
MIN_ACCEPTABLE_ROWS = 5500
MAX_ACCEPTABLE_ROWS = 5600
ALLOWED_SECURITY_TYPES = {"058001001", "058001008"}
ALLOWED_MARKETS = {
    "069001001001", "069001001003", "069001001006",
    "069001002001", "069001002002", "069001002005",
    "069001017",
}


def main() -> int:
    if not MASTER.exists() or not SUMMARY.exists():
        print("Missing archive outputs", file=sys.stderr)
        return 1

    with MASTER.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))

    codes = [row["security_code"] for row in rows]
    duplicates = [code for code, count in Counter(codes).items() if count > 1]
    invalid_security_types = [
        row for row in rows if row.get("security_type_code") not in ALLOWED_SECURITY_TYPES
    ]
    invalid_markets = [
        row for row in rows if row.get("trade_market_code") not in ALLOWED_MARKETS
    ]
    third_board_rows = [
        row for row in rows
        if "三板" in (row.get("security_type") or "")
        or "三板" in (row.get("trade_market") or "")
    ]

    checks = {
        "row_count": len(rows),
        "summary_row_count": summary.get("archived_unique_a_share_or_cdr_count"),
        "row_count_in_expected_band": MIN_ACCEPTABLE_ROWS <= len(rows) <= MAX_ACCEPTABLE_ROWS,
        "row_count_matches_summary": len(rows) == summary.get("archived_unique_a_share_or_cdr_count"),
        "public_reference_count": summary.get("public_reference_disclosed_count"),
        "public_reference_gap": summary.get("reference_count_gap"),
        "duplicate_code_count": len(duplicates),
        "invalid_security_type_count": len(invalid_security_types),
        "invalid_market_count": len(invalid_markets),
        "third_board_row_count": len(third_board_rows),
        "unknown_exchange_count": sum(row.get("exchange") == "UNKNOWN" for row in rows),
        "missing_name_count": sum(not row.get("security_name") for row in rows),
        "missing_announcement_date_count": sum(not row.get("announcement_date") for row in rows),
        "invalid_code_count": sum(len(code) != 6 or not code.isdigit() for code in codes),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))

    hard_fail = (
        not checks["row_count_in_expected_band"]
        or not checks["row_count_matches_summary"]
        or checks["duplicate_code_count"] > 0
        or checks["invalid_security_type_count"] > 0
        or checks["invalid_market_count"] > 0
        or checks["third_board_row_count"] > 0
        or checks["unknown_exchange_count"] > 0
        or checks["missing_name_count"] > 0
        or checks["invalid_code_count"] > 0
    )
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
