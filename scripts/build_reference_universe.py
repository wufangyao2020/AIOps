#!/usr/bin/env python3
"""Build the final 5,550-company disclosed A-share H1 universe.

Stage A removes post-cutoff and pre-listing IPO rows. Stage B removes the sole
Chinese Depositary Receipt (CDR), because the requested and widely reported
5,550-company universe is the pure A-share company universe. The CDR remains in
an audit file; no record is silently discarded.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import finalize_reference as base

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "2026H1"
CURRENT_DIR = ROOT / "data" / "current"
META_DIR = ROOT / "meta"
STATUS_DIR = ROOT / "status"
REFERENCE_CSV = DATA_DIR / "a_share_2026_h1_5550_master.csv"
REFERENCE_JSONL = DATA_DIR / "a_share_2026_h1_5550_master.jsonl"
CDR_AUDIT = META_DIR / "excluded_cdr_from_pure_a_share_universe.csv"
EXPECTED_PRE_CDR_COUNT = 5551
EXPECTED_FINAL_COUNT = 5550
A_SHARE_TYPE_CODE = "058001001"
CDR_TYPE_CODE = "058001008"


def main() -> int:
    # The base stage returns 1 while it still has the one CDR in its 5,551-row
    # interim universe. The wrapper validates that exact state and finalizes the
    # pure A-share archive.
    base_result = base.main()
    columns, interim_rows = base.read_csv(REFERENCE_CSV)
    if len(interim_rows) != EXPECTED_PRE_CDR_COUNT:
        raise RuntimeError(
            f"IPO/date reconciliation produced {len(interim_rows)} rows; "
            f"expected {EXPECTED_PRE_CDR_COUNT}. Base return={base_result}"
        )

    cdr_rows = [
        row for row in interim_rows
        if row.get("security_type_code") == CDR_TYPE_CODE
    ]
    final_rows = [
        row for row in interim_rows
        if row.get("security_type_code") == A_SHARE_TYPE_CODE
    ]
    unexpected_types = [
        row for row in interim_rows
        if row.get("security_type_code") not in {A_SHARE_TYPE_CODE, CDR_TYPE_CODE}
    ]
    if unexpected_types:
        raise RuntimeError(f"Unexpected security types in interim universe: {unexpected_types[:5]}")
    if len(cdr_rows) != 1:
        raise RuntimeError(f"Expected one CDR audit row, got {len(cdr_rows)}: {cdr_rows}")
    if len(final_rows) != EXPECTED_FINAL_COUNT:
        raise RuntimeError(f"Final A-share count is {len(final_rows)}, expected {EXPECTED_FINAL_COUNT}")

    audit_rows = [
        {
            **row,
            "exclusion_reason": "Chinese Depositary Receipt; excluded from the pure A-share company universe",
        }
        for row in cdr_rows
    ]

    base.write_csv(REFERENCE_CSV, columns, final_rows)
    base.write_jsonl(REFERENCE_JSONL, final_rows)
    base.write_csv(CDR_AUDIT, columns + ["exclusion_reason"], audit_rows)
    shutil.copy2(REFERENCE_CSV, CURRENT_DIR / "a_share_master.csv")

    status_columns = [
        "security_code", "security_name", "exchange", "board", "industry",
        "report_period", "research_stage", "fundamental_acceleration_status",
        "new_profit_pool_status", "cyclical_turn_status", "capital_event_status",
        "announcement_review_status", "peer_comparison_status", "data_quality_flag",
    ]
    status_path = STATUS_DIR / "research_status_2026_h1.csv"
    base.write_csv(status_path, status_columns, final_rows)
    shutil.copy2(status_path, CURRENT_DIR / "research_status.csv")

    for exchange in ("SSE", "SZSE", "BSE"):
        base.write_csv(
            DATA_DIR / "by_exchange" / f"{exchange.lower()}_2026_h1.csv",
            columns,
            [row for row in final_rows if row.get("exchange") == exchange],
        )

    summary_path = META_DIR / "snapshot_2026_h1.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update({
        "cdr_excluded_from_pure_a_share_count": len(cdr_rows),
        "cdr_excluded_codes": [row["security_code"] for row in cdr_rows],
        "cdr_audit_file": CDR_AUDIT.relative_to(ROOT).as_posix(),
        "archived_reference_universe_count": len(final_rows),
        "reference_universe_matches_5550": len(final_rows) == EXPECTED_FINAL_COUNT,
        "reference_universe_file": REFERENCE_CSV.relative_to(ROOT).as_posix(),
        "reference_universe_jsonl": REFERENCE_JSONL.relative_to(ROOT).as_posix(),
    })
    summary["notes"] = [
        "The source-derived 5,566 A-share/CDR rows partition into 5,550 disclosed A-share companies, 14 not-yet-listed IPO records, one CDR, and one post-cutoff record.",
        "The currently listed company 002731 (*ST Cuihua) did not disclose a completed 2026 H1 report and therefore is absent from the report-source universe.",
        "The 5,550-company archive is produced by announcement-date, IPO-listing-date, and security-type rules; no arbitrary truncation is used.",
        "Every excluded record remains in an audit file.",
        "Official exchange filings remain the legal source of record.",
        "Prior conversation frequency never affects candidate generation or scoring."
    ]
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    checksum_paths = [
        REFERENCE_CSV,
        REFERENCE_JSONL,
        DATA_DIR / "a_share_2026_h1_source_current.csv",
        DATA_DIR / "a_share_2026_h1_master.jsonl",
        META_DIR / "post_cutoff_records_2026-09-01.csv",
        META_DIR / "excluded_not_listed_by_2026-08-31.csv",
        CDR_AUDIT,
        META_DIR / "recent_ipo_listing_date_map.json",
        status_path,
    ]
    (META_DIR / "checksums_2026_h1.sha256").write_text(
        "\n".join(
            f"{base.sha256(path)}  {path.relative_to(ROOT).as_posix()}"
            for path in checksum_paths if path.exists()
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "base_return_code": base_result,
        "interim_after_date_and_ipo_filters": len(interim_rows),
        "cdr_excluded": [
            {"code": row["security_code"], "name": row["security_name"]}
            for row in cdr_rows
        ],
        "final_archived_a_share_count": len(final_rows),
        "matches_expected_5550": len(final_rows) == EXPECTED_FINAL_COUNT,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
