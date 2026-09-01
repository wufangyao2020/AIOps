#!/usr/bin/env python3
"""Validate the exact 5,550-company archive and all audit partitions."""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "data" / "2026H1" / "a_share_2026_h1_5550_master.csv"
SOURCE_CURRENT = ROOT / "data" / "2026H1" / "a_share_2026_h1_source_current.csv"
POST_CUTOFF = ROOT / "meta" / "post_cutoff_records_2026-09-01.csv"
DELISTED = ROOT / "meta" / "excluded_completed_delistings_through_2026-08-31.csv"
SUMMARY = ROOT / "meta" / "snapshot_2026_h1.json"
EXPECTED_REFERENCE_COUNT = 5550
EXPECTED_SOURCE_COUNT = 5566
EXPECTED_POST_CUTOFF_COUNT = 1
EXPECTED_DELISTED_MATCH_COUNT = 15
ALLOWED_SECURITY_TYPES = {"058001001", "058001008"}
ALLOWED_MARKETS = {
    "069001001001", "069001001003", "069001001006",
    "069001002001", "069001002002", "069001002005",
    "069001017",
}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    required = (REFERENCE, SOURCE_CURRENT, POST_CUTOFF, DELISTED, SUMMARY)
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    if missing:
        print(json.dumps({"missing_files": missing}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    reference_rows = read_rows(REFERENCE)
    source_rows = read_rows(SOURCE_CURRENT)
    post_cutoff_rows = read_rows(POST_CUTOFF)
    delisted_rows = read_rows(DELISTED)
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))

    reference_codes = [row["security_code"] for row in reference_rows]
    source_codes = [row["security_code"] for row in source_rows]
    post_codes = {row["security_code"] for row in post_cutoff_rows}
    delisted_codes = {row["security_code"] for row in delisted_rows}

    duplicate_codes = [
        code for code, count in Counter(reference_codes).items() if count > 1
    ]
    invalid_security_types = [
        row for row in reference_rows
        if row.get("security_type_code") not in ALLOWED_SECURITY_TYPES
    ]
    invalid_markets = [
        row for row in reference_rows
        if row.get("trade_market_code") not in ALLOWED_MARKETS
    ]
    third_board_rows = [
        row for row in reference_rows
        if "三板" in (row.get("security_type") or "")
        or "三板" in (row.get("trade_market") or "")
    ]
    after_cutoff_rows = [
        row for row in reference_rows
        if not row.get("announcement_date")
        or row["announcement_date"] > "2026-08-31"
    ]
    incorrectly_partitioned_post_cutoff = [
        row for row in post_cutoff_rows
        if row.get("announcement_date")
        and row["announcement_date"] <= "2026-08-31"
    ]

    partition_codes = set(reference_codes) | post_codes | delisted_codes
    overlap_count = (
        len(set(reference_codes) & post_codes)
        + len(set(reference_codes) & delisted_codes)
        + len(post_codes & delisted_codes)
    )

    checks = {
        "reference_row_count": len(reference_rows),
        "reference_count_exact_5550": len(reference_rows) == EXPECTED_REFERENCE_COUNT,
        "source_current_row_count": len(source_rows),
        "source_count_exact_5566": len(source_rows) == EXPECTED_SOURCE_COUNT,
        "post_cutoff_row_count": len(post_cutoff_rows),
        "post_cutoff_count_exact_1": len(post_cutoff_rows) == EXPECTED_POST_CUTOFF_COUNT,
        "delisted_match_row_count": len(delisted_rows),
        "delisted_match_count_exact_15": len(delisted_rows) == EXPECTED_DELISTED_MATCH_COUNT,
        "partition_union_matches_source": partition_codes == set(source_codes),
        "partition_overlap_count": overlap_count,
        "summary_reference_count": summary.get("archived_reference_universe_count"),
        "summary_count_matches_file": len(reference_rows) == summary.get("archived_reference_universe_count"),
        "duplicate_code_count": len(duplicate_codes),
        "invalid_security_type_count": len(invalid_security_types),
        "invalid_market_count": len(invalid_markets),
        "third_board_row_count": len(third_board_rows),
        "after_cutoff_in_reference_count": len(after_cutoff_rows),
        "pre_cutoff_in_post_cutoff_count": len(incorrectly_partitioned_post_cutoff),
        "unknown_exchange_count": sum(row.get("exchange") == "UNKNOWN" for row in reference_rows),
        "missing_name_count": sum(not row.get("security_name") for row in reference_rows),
        "invalid_code_count": sum(len(code) != 6 or not code.isdigit() for code in reference_codes),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))

    hard_fail = (
        not checks["reference_count_exact_5550"]
        or not checks["source_count_exact_5566"]
        or not checks["post_cutoff_count_exact_1"]
        or not checks["delisted_match_count_exact_15"]
        or not checks["partition_union_matches_source"]
        or checks["partition_overlap_count"] > 0
        or not checks["summary_count_matches_file"]
        or checks["duplicate_code_count"] > 0
        or checks["invalid_security_type_count"] > 0
        or checks["invalid_market_count"] > 0
        or checks["third_board_row_count"] > 0
        or checks["after_cutoff_in_reference_count"] > 0
        or checks["pre_cutoff_in_post_cutoff_count"] > 0
        or checks["unknown_exchange_count"] > 0
        or checks["missing_name_count"] > 0
        or checks["invalid_code_count"] > 0
    )
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
