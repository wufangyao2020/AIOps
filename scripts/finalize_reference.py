#!/usr/bin/env python3
"""Derive the exact 2026-08-31 listed-company reference universe.

Objective inclusion rules:
1. 2026 H1 report announcement date <= 2026-08-31;
2. security code appears in the active Shanghai/Shenzhen/Beijing A-share quote
   universe at the snapshot time.

Rows excluded by either rule remain in audit files. Nothing is truncated merely
to force a target count.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "2026H1"
CURRENT_DIR = ROOT / "data" / "current"
META_DIR = ROOT / "meta"
STATUS_DIR = ROOT / "status"
SOURCE_MASTER = DATA_DIR / "a_share_2026_h1_master.csv"
SOURCE_JSONL = DATA_DIR / "a_share_2026_h1_master.jsonl"
REFERENCE_CUTOFF = "2026-08-31"
EXPECTED_REFERENCE_COUNT = 5550
ACTIVE_ENDPOINTS = (
    "https://82.push2.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://75.push2.eastmoney.com/api/qt/clist/get",
)
ACTIVE_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"


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


def request_json(params: dict[str, str], attempts: int = 8) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/center/gridlist.html#hs_a_board",
        "Connection": "close",
    }
    errors: list[str] = []
    for attempt in range(attempts):
        endpoint = ACTIVE_ENDPOINTS[attempt % len(ACTIVE_ENDPOINTS)]
        full_url = f"{endpoint}?{urlencode(params)}"
        try:
            with urlopen(Request(full_url, headers=headers), timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # network endpoint may reset without HTTP response
            errors.append(f"attempt={attempt + 1} endpoint={endpoint} error={type(exc).__name__}: {exc}")
            time.sleep(min(1.5 * (attempt + 1), 10))
    raise RuntimeError("Active A-share quote fetch failed:\n" + "\n".join(errors))


def extract_diff(payload: dict[str, Any]) -> list[dict[str, Any]]:
    diff = (payload.get("data") or {}).get("diff") or []
    return list(diff.values()) if isinstance(diff, dict) else list(diff)


def active_params(page: int, page_size: int) -> dict[str, str]:
    return {
        "pn": str(page),
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": ACTIVE_FS,
        "fields": "f12,f14",
    }


def fetch_active_a_share_codes() -> dict[str, str]:
    # Prefer one large request to avoid the endpoint's intermittent resets during pagination.
    first = request_json(active_params(page=1, page_size=10000))
    total = int((first.get("data") or {}).get("total") or 0)
    rows = extract_diff(first)

    # Fallback only when the endpoint caps the page size.
    if total and len(rows) < total:
        page_size = 500
        first = request_json(active_params(page=1, page_size=page_size))
        total = int((first.get("data") or {}).get("total") or 0)
        rows = extract_diff(first)
        pages = max(1, (total + page_size - 1) // page_size)
        for page in range(2, pages + 1):
            time.sleep(0.5)
            rows.extend(extract_diff(request_json(active_params(page=page, page_size=page_size))))

    active: dict[str, str] = {}
    for row in rows:
        code = str(row.get("f12") or "").strip()
        name = str(row.get("f14") or "").strip()
        if len(code) == 6 and code.isdigit():
            active[code] = name
    if len(active) < 5400:
        raise RuntimeError(f"Active A-share universe unexpectedly small: total={total}, parsed={len(active)}")
    return active


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    columns, report_rows = read_csv(SOURCE_MASTER)
    active_codes = fetch_active_a_share_codes()
    report_codes = {row.get("security_code") for row in report_rows}

    through_cutoff = [
        row for row in report_rows
        if row.get("announcement_date") and row["announcement_date"] <= REFERENCE_CUTOFF
    ]
    post_cutoff = [
        row for row in report_rows
        if not row.get("announcement_date") or row["announcement_date"] > REFERENCE_CUTOFF
    ]
    reported_not_active = [
        row for row in through_cutoff if row.get("security_code") not in active_codes
    ]
    reference_rows = [
        row for row in through_cutoff if row.get("security_code") in active_codes
    ]
    active_without_report = [
        {"security_code": code, "security_name": name}
        for code, name in sorted(active_codes.items()) if code not in report_codes
    ]

    reference_csv = DATA_DIR / "a_share_2026_h1_disclosed_through_2026-08-31.csv"
    reference_jsonl = DATA_DIR / "a_share_2026_h1_disclosed_through_2026-08-31.jsonl"
    source_current_csv = DATA_DIR / "a_share_2026_h1_source_current.csv"
    post_cutoff_csv = META_DIR / "post_cutoff_records_2026-09-01.csv"
    inactive_csv = META_DIR / "reported_but_not_active_listed_at_snapshot.csv"
    missing_report_csv = META_DIR / "active_listed_without_h1_report_in_source.csv"

    shutil.copy2(SOURCE_MASTER, source_current_csv)
    write_csv(reference_csv, columns, reference_rows)
    write_jsonl(reference_jsonl, reference_rows)
    write_csv(post_cutoff_csv, columns, post_cutoff)
    write_csv(inactive_csv, columns, reported_not_active)
    write_csv(missing_report_csv, ["security_code", "security_name"], active_without_report)
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
        "active_quotation_universe_count_at_snapshot": len(active_codes),
        "source_current_a_share_or_cdr_count": len(report_rows),
        "report_rows_through_cutoff_count": len(through_cutoff),
        "reported_but_not_active_listed_count": len(reported_not_active),
        "archived_reference_universe_count": len(reference_rows),
        "post_cutoff_record_count": len(post_cutoff),
        "active_listed_without_h1_report_source_count": len(active_without_report),
        "reference_universe_matches_5550": len(reference_rows) == EXPECTED_REFERENCE_COUNT,
        "reference_universe_file": reference_csv.relative_to(ROOT).as_posix(),
        "post_cutoff_audit_file": post_cutoff_csv.relative_to(ROOT).as_posix(),
        "inactive_audit_file": inactive_csv.relative_to(ROOT).as_posix(),
        "active_without_report_audit_file": missing_report_csv.relative_to(ROOT).as_posix(),
    })
    summary["notes"] = [
        "Reference inclusion requires report announcement through 2026-08-31 and presence in the active Shanghai/Shenzhen/Beijing A-share quote universe.",
        "Post-cutoff and inactive/delisted report rows are preserved in audit files; no arbitrary truncation is applied.",
        "Official exchange filings remain the legal source of record.",
        "Prior conversation frequency never affects candidate generation or scoring."
    ]
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    checksum_paths = [
        reference_csv, reference_jsonl, source_current_csv, SOURCE_JSONL,
        post_cutoff_csv, inactive_csv, missing_report_csv, status_path,
    ]
    (META_DIR / "checksums_2026_h1.sha256").write_text(
        "\n".join(
            f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}"
            for path in checksum_paths if path.exists()
        ) + "\n",
        encoding="utf-8",
    )

    result = {
        "active_quotation_count": len(active_codes),
        "source_current_report_count": len(report_rows),
        "through_cutoff_count": len(through_cutoff),
        "reported_not_active_count": len(reported_not_active),
        "post_cutoff_count": len(post_cutoff),
        "reference_count": len(reference_rows),
        "active_without_report_count": len(active_without_report),
        "matches_expected_5550": len(reference_rows) == EXPECTED_REFERENCE_COUNT,
        "reported_not_active_records": [
            {"code": row.get("security_code"), "name": row.get("security_name"), "announcement_date": row.get("announcement_date")}
            for row in reported_not_active
        ],
        "post_cutoff_records": [
            {"code": row.get("security_code"), "name": row.get("security_name"), "announcement_date": row.get("announcement_date")}
            for row in post_cutoff
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if len(reference_rows) == EXPECTED_REFERENCE_COUNT else 1


if __name__ == "__main__":
    raise SystemExit(main())
