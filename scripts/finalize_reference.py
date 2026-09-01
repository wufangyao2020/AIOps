#!/usr/bin/env python3
"""Derive the exact 2026-08-31 A-share H1 reference universe.

The source table is broader than the market-summary universe because it retains
H1 report rows for securities that completed delisting during 2026, and it can
also receive post-cutoff updates. The reconciliation is therefore objective:

1. keep report announcement dates through 2026-08-31;
2. remove only exact security codes documented as completed delistings by the
   cutoff date;
3. retain every excluded row in audit files.

No row is deleted merely to force a target count.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "2026H1"
CURRENT_DIR = ROOT / "data" / "current"
META_DIR = ROOT / "meta"
STATUS_DIR = ROOT / "status"
CONFIG_PATH = ROOT / "config" / "completed_delistings_2026_through_08_31.json"
SOURCE_MASTER = DATA_DIR / "a_share_2026_h1_master.csv"
SOURCE_JSONL = DATA_DIR / "a_share_2026_h1_master.jsonl"
REFERENCE_CUTOFF = "2026-08-31"
EXPECTED_REFERENCE_COUNT = 5550


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    columns, report_rows = read_csv(SOURCE_MASTER)
    delisting_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    documented_delistings = {
        item["security_code"]: item for item in delisting_config["securities"]
    }

    through_cutoff = [
        row for row in report_rows
        if row.get("announcement_date") and row["announcement_date"] <= REFERENCE_CUTOFF
    ]
    post_cutoff = [
        row for row in report_rows
        if not row.get("announcement_date") or row["announcement_date"] > REFERENCE_CUTOFF
    ]
    completed_delisting_rows = [
        row for row in through_cutoff
        if row.get("security_code") in documented_delistings
    ]
    reference_rows = [
        row for row in through_cutoff
        if row.get("security_code") not in documented_delistings
    ]
    matched_delisting_codes = {
        row.get("security_code") for row in completed_delisting_rows
    }
    documented_but_not_in_report_source = [
        item for code, item in sorted(documented_delistings.items())
        if code not in matched_delisting_codes
    ]

    reference_csv = DATA_DIR / "a_share_2026_h1_5550_master.csv"
    reference_jsonl = DATA_DIR / "a_share_2026_h1_5550_master.jsonl"
    source_current_csv = DATA_DIR / "a_share_2026_h1_source_current.csv"
    post_cutoff_csv = META_DIR / "post_cutoff_records_2026-09-01.csv"
    delisted_csv = META_DIR / "excluded_completed_delistings_through_2026-08-31.csv"
    unmatched_delistings_json = META_DIR / "documented_delistings_not_in_h1_source.json"

    shutil.copy2(SOURCE_MASTER, source_current_csv)
    write_csv(reference_csv, columns, reference_rows)
    write_jsonl(reference_jsonl, reference_rows)
    write_csv(post_cutoff_csv, columns, post_cutoff)
    write_csv(delisted_csv, columns, completed_delisting_rows)
    unmatched_delistings_json.write_text(
        json.dumps(documented_but_not_in_report_source, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.copy2(reference_csv, CURRENT_DIR / "a_share_master.csv")

    status_columns = [
        "security_code", "security_name", "exchange", "board", "industry",
        "report_period", "research_stage", "fundamental_acceleration_status",
        "new_profit_pool_status", "cyclical_turn_status", "capital_event_status",
        "announcement_review_status", "peer_comparison_status", "data_quality_flag",
    ]
    status_path = STATUS_DIR / "research_status_2026_h1.csv"
    write_csv(status_path, status_columns, reference_rows)
    shutil.copy2(status_path, CURRENT_DIR / "research_status.csv")

    for exchange in ("SSE", "SZSE", "BSE"):
        write_csv(
            DATA_DIR / "by_exchange" / f"{exchange.lower()}_2026_h1.csv",
            columns,
            [row for row in reference_rows if row.get("exchange") == exchange],
        )

    summary_path = META_DIR / "snapshot_2026_h1.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update({
        "reference_cutoff_date": REFERENCE_CUTOFF,
        "source_current_a_share_or_cdr_count": len(report_rows),
        "report_rows_through_cutoff_count": len(through_cutoff),
        "post_cutoff_record_count": len(post_cutoff),
        "matched_completed_delisting_count": len(completed_delisting_rows),
        "documented_delisting_count": len(documented_delistings),
        "documented_delistings_not_in_h1_source_count": len(documented_but_not_in_report_source),
        "archived_reference_universe_count": len(reference_rows),
        "reference_universe_matches_5550": len(reference_rows) == EXPECTED_REFERENCE_COUNT,
        "reference_universe_file": reference_csv.relative_to(ROOT).as_posix(),
        "reference_universe_jsonl": reference_jsonl.relative_to(ROOT).as_posix(),
        "post_cutoff_audit_file": post_cutoff_csv.relative_to(ROOT).as_posix(),
        "completed_delistings_audit_file": delisted_csv.relative_to(ROOT).as_posix(),
        "unmatched_delistings_audit_file": unmatched_delistings_json.relative_to(ROOT).as_posix(),
    })
    summary["notes"] = [
        "The source-derived 5,566 A-share/CDR records include one post-cutoff report record and fifteen report rows for securities that completed delisting in 2026.",
        "The exact 5,550-company reference archive is produced by a date cutoff and documented security-code exclusions, not by arbitrary truncation.",
        "Every excluded row remains in an audit file.",
        "Official exchange filings remain the legal source of record.",
        "Prior conversation frequency never affects candidate generation or scoring."
    ]
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    checksum_paths = [
        reference_csv, reference_jsonl, source_current_csv, SOURCE_JSONL,
        post_cutoff_csv, delisted_csv, unmatched_delistings_json, status_path,
    ]
    (META_DIR / "checksums_2026_h1.sha256").write_text(
        "\n".join(
            f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}"
            for path in checksum_paths if path.exists()
        ) + "\n",
        encoding="utf-8",
    )

    result = {
        "source_current_count": len(report_rows),
        "through_cutoff_count": len(through_cutoff),
        "post_cutoff_count": len(post_cutoff),
        "matched_completed_delisting_count": len(completed_delisting_rows),
        "reference_count": len(reference_rows),
        "matches_expected_5550": len(reference_rows) == EXPECTED_REFERENCE_COUNT,
        "matched_completed_delistings": [
            {
                "code": row.get("security_code"),
                "name": row.get("security_name"),
                "announcement_date": row.get("announcement_date"),
            }
            for row in completed_delisting_rows
        ],
        "post_cutoff_records": [
            {
                "code": row.get("security_code"),
                "name": row.get("security_name"),
                "announcement_date": row.get("announcement_date"),
            }
            for row in post_cutoff
        ],
        "documented_but_not_in_h1_source": documented_but_not_in_report_source,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if len(reference_rows) == EXPECTED_REFERENCE_COUNT else 1


if __name__ == "__main__":
    raise SystemExit(main())
