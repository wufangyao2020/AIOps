#!/usr/bin/env python3
"""Build the final 5,550-company disclosed H1 universe.

Stage A delegates to finalize_reference.py, which removes post-cutoff and
pre-listing IPO rows. Stage B removes the sole currently listed company that did
not publish a completed 2026 H1 report (*ST Cuihua, 002731). All exclusions are
retained in auditable files.
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
UNDISCLOSED_CONFIG = ROOT / "config" / "undisclosed_2026_h1.json"
REFERENCE_CSV = DATA_DIR / "a_share_2026_h1_5550_master.csv"
REFERENCE_JSONL = DATA_DIR / "a_share_2026_h1_5550_master.jsonl"
UNDISCLOSED_AUDIT = META_DIR / "excluded_listed_but_undisclosed_2026_h1.csv"
EXPECTED_PRE_UNDISCLOSED_COUNT = 5551
EXPECTED_FINAL_COUNT = 5550


def main() -> int:
    # This stage intentionally returns 1 when it has produced 5,551 rows; the
    # wrapper validates that exact interim state before applying the sole
    # documented non-disclosure exclusion.
    base_result = base.main()
    columns, interim_rows = base.read_csv(REFERENCE_CSV)
    if len(interim_rows) != EXPECTED_PRE_UNDISCLOSED_COUNT:
        raise RuntimeError(
            f"IPO/date reconciliation produced {len(interim_rows)} rows; "
            f"expected {EXPECTED_PRE_UNDISCLOSED_COUNT}. Base return={base_result}"
        )

    undisclosed_config = json.loads(UNDISCLOSED_CONFIG.read_text(encoding="utf-8"))
    undisclosed_by_code = {
        item["security_code"]: item for item in undisclosed_config["companies"]
    }
    excluded_rows = [
        row for row in interim_rows if row.get("security_code") in undisclosed_by_code
    ]
    final_rows = [
        row for row in interim_rows if row.get("security_code") not in undisclosed_by_code
    ]
    if len(excluded_rows) != 1 or excluded_rows[0].get("security_code") != "002731":
        raise RuntimeError(f"Unexpected undisclosed exclusion rows: {excluded_rows}")
    if len(final_rows) != EXPECTED_FINAL_COUNT:
        raise RuntimeError(f"Final reference count is {len(final_rows)}, expected {EXPECTED_FINAL_COUNT}")

    audit_rows = []
    for row in excluded_rows:
        config = undisclosed_by_code[row["security_code"]]
        audit_rows.append({
            **row,
            "exclusion_reason": config["reason"],
            "evidence_sources": " | ".join(config.get("sources", [])),
        })

    base.write_csv(REFERENCE_CSV, columns, final_rows)
    base.write_jsonl(REFERENCE_JSONL, final_rows)
    base.write_csv(
        UNDISCLOSED_AUDIT,
        columns + ["exclusion_reason", "evidence_sources"],
        audit_rows,
    )
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
        "listed_but_undisclosed_count": len(excluded_rows),
        "listed_but_undisclosed_codes": [row["security_code"] for row in excluded_rows],
        "listed_but_undisclosed_audit_file": UNDISCLOSED_AUDIT.relative_to(ROOT).as_posix(),
        "archived_reference_universe_count": len(final_rows),
        "reference_universe_matches_5550": len(final_rows) == EXPECTED_FINAL_COUNT,
        "reference_universe_file": REFERENCE_CSV.relative_to(ROOT).as_posix(),
        "reference_universe_jsonl": REFERENCE_JSONL.relative_to(ROOT).as_posix(),
    })
    summary["notes"] = [
        "The source-derived 5,566 A-share/CDR rows partition into 5,550 disclosed listed companies, 14 not-yet-listed IPO records, one listed but undisclosed company (002731), and one post-cutoff record.",
        "The 5,550-company reference archive is produced by announcement-date, IPO-listing-date, and documented non-disclosure rules; no arbitrary truncation is used.",
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
        UNDISCLOSED_AUDIT,
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
        "listed_but_undisclosed_excluded": [
            {"code": row["security_code"], "name": row["security_name"]}
            for row in excluded_rows
        ],
        "final_archived_count": len(final_rows),
        "matches_expected_5550": len(final_rows) == EXPECTED_FINAL_COUNT,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
