#!/usr/bin/env python3
"""Derive the 2026-08-31 disclosed universe from the current source snapshot.

The source API was refreshed on 2026-09-01 and contains records whose announced
release date is 2026-09-01. Public market summaries counted 5,550 companies as
of the statutory 2026-08-31 cutoff. This step partitions the source-derived
A-share/CDR universe by announcement date rather than deleting arbitrary rows.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "2026H1"
CURRENT_DIR = ROOT / "data" / "current"
META_DIR = ROOT / "meta"
STATUS_DIR = ROOT / "status"
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
    columns, rows = read_csv(SOURCE_MASTER)
    reference_rows = [
        row for row in rows
        if row.get("announcement_date") and row["announcement_date"] <= REFERENCE_CUTOFF
    ]
    post_cutoff_rows = [
        row for row in rows
        if not row.get("announcement_date") or row["announcement_date"] > REFERENCE_CUTOFF
    ]

    reference_csv = DATA_DIR / "a_share_2026_h1_disclosed_through_2026-08-31.csv"
    reference_jsonl = DATA_DIR / "a_share_2026_h1_disclosed_through_2026-08-31.jsonl"
    source_current_csv = DATA_DIR / "a_share_2026_h1_source_current.csv"
    post_cutoff_csv = META_DIR / "post_cutoff_records_2026-09-01.csv"

    shutil.copy2(SOURCE_MASTER, source_current_csv)
    write_csv(reference_csv, columns, reference_rows)
    write_jsonl(reference_jsonl, reference_rows)
    write_csv(post_cutoff_csv, columns, post_cutoff_rows)
    shutil.copy2(reference_csv, CURRENT_DIR / "a_share_master.csv")

    status_columns = [
        "security_code", "security_name", "exchange", "board", "industry",
        "report_period", "research_stage", "fundamental_acceleration_status",
        "new_profit_pool_status", "cyclical_turn_status", "capital_event_status",
        "announcement_review_status", "peer_comparison_status", "data_quality_flag",
    ]
    write_csv(STATUS_DIR / "research_status_2026_h1.csv", status_columns, reference_rows)
    shutil.copy2(STATUS_DIR / "research_status_2026_h1.csv", CURRENT_DIR / "research_status.csv")

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
        "source_current_a_share_or_cdr_count": len(rows),
        "archived_reference_universe_count": len(reference_rows),
        "post_cutoff_record_count": len(post_cutoff_rows),
        "reference_universe_matches_5550": len(reference_rows) == EXPECTED_REFERENCE_COUNT,
        "reference_universe_file": reference_csv.relative_to(ROOT).as_posix(),
        "post_cutoff_audit_file": post_cutoff_csv.relative_to(ROOT).as_posix(),
    })
    summary["notes"] = [
        "The source API snapshot contains post-cutoff records dated 2026-09-01.",
        "The requested 5,550-company archive is defined objectively as A-share/CDR records announced through 2026-08-31.",
        "Post-cutoff records are retained in a separate audit file; no row is deleted without a date-based rule.",
        "Official exchange filings remain the legal source of record.",
        "Prior conversation frequency never affects candidate generation or scoring."
    ]
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    checksum_paths = [
        reference_csv, reference_jsonl, source_current_csv, SOURCE_JSONL,
        post_cutoff_csv, STATUS_DIR / "research_status_2026_h1.csv",
    ]
    (META_DIR / "checksums_2026_h1.sha256").write_text(
        "\n".join(
            f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}"
            for path in checksum_paths if path.exists()
        ) + "\n",
        encoding="utf-8",
    )

    result = {
        "source_current_count": len(rows),
        "reference_cutoff": REFERENCE_CUTOFF,
        "reference_count": len(reference_rows),
        "post_cutoff_count": len(post_cutoff_rows),
        "matches_expected_5550": len(reference_rows) == EXPECTED_REFERENCE_COUNT,
        "post_cutoff_codes": [
            {"code": row.get("security_code"), "name": row.get("security_name"), "announcement_date": row.get("announcement_date")}
            for row in post_cutoff_rows
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if len(reference_rows) == EXPECTED_REFERENCE_COUNT else 1


if __name__ == "__main__":
    raise SystemExit(main())
