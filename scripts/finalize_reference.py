#!/usr/bin/env python3
"""Derive the exact 2026-08-31 A-share H1 reference universe.

The Eastmoney H1 performance table includes securities with assigned A-share
codes whose IPO financial data are already present even though their listing
date is after the statutory half-year-report cutoff. Market-wide summaries say
that 5,551 companies were listed at 2026-08-31 and 5,550 had disclosed (all but
*ST Cuihua). Therefore this script reconciles the report source by:

1. retaining report announcements through 2026-08-31;
2. querying Eastmoney's IPO tables for listing dates;
3. excluding exact codes whose listing date is after 2026-08-31 (or whose IPO
   record still has no listing date);
4. retaining every excluded row in audit files.

No arbitrary truncation is performed.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
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
DATACENTER_ENDPOINTS = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get",
    "https://datacenter.eastmoney.com/securities/api/data/v1/get",
)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def request_json(url: str, params: dict[str, str], attempts: int = 5) -> dict[str, Any]:
    full_url = f"{url}?{urlencode(params)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://data.eastmoney.com/xg/xg/default_2.html",
        "Connection": "close",
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(Request(full_url, headers=headers), timeout=45) as response:
                payload = response.read().decode("utf-8")
            result = json.loads(payload)
            if result.get("result") is None:
                raise RuntimeError(result.get("message") or result.get("msg") or "empty result")
            return result
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 12))
    raise RuntimeError(f"Failed fetching {full_url}: {last_error}")


def fetch_report_rows(
    report_name: str,
    *,
    filter_expression: str = "",
    source: str = "WEB",
    page_size: int = 500,
) -> list[dict[str, Any]]:
    errors: list[str] = []
    for endpoint in DATACENTER_ENDPOINTS:
        try:
            base = {
                "sortColumns": "APPLY_DATE,SECURITY_CODE",
                "sortTypes": "-1,-1",
                "pageSize": str(page_size),
                "reportName": report_name,
                "columns": "ALL",
                "source": source,
                "client": "WEB",
            }
            if filter_expression:
                base["filter"] = filter_expression
            first = request_json(endpoint, {**base, "pageNumber": "1"})
            result = first["result"]
            pages = int(result.get("pages") or 1)
            rows = list(result.get("data") or [])
            for page in range(2, pages + 1):
                payload = request_json(endpoint, {**base, "pageNumber": str(page)})
                rows.extend(payload["result"].get("data") or [])
            return rows
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError(f"Unable to fetch {report_name}:\n" + "\n".join(errors))


def date_text(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return text[:10] if len(text) >= 10 else text


def fetch_recent_ipo_map() -> dict[str, dict[str, Any]]:
    # Shanghai/Shenzhen (including STAR/ChiNext and CDR records).
    main_rows = fetch_report_rows(
        "RPTA_APP_IPOAPPLY",
        filter_expression="(APPLY_DATE>'2025-01-01')",
        page_size=500,
    )
    # Beijing Stock Exchange uses a separate issuance table.
    bse_rows = fetch_report_rows(
        "RPT_NEEQ_ISSUEINFO_LIST",
        source="NEEQSELECT",
        page_size=500,
    )

    ipo_map: dict[str, dict[str, Any]] = {}
    for row in main_rows:
        code = str(row.get("SECURITY_CODE") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        ipo_map[code] = {
            "security_code": code,
            "security_name": str(row.get("SECURITY_NAME") or row.get("SECURITY_NAME_ABBR") or row.get("f14") or "").strip(),
            "apply_date": date_text(row.get("APPLY_DATE")),
            "listing_date": date_text(row.get("LISTING_DATE")),
            "ipo_source_report": "RPTA_APP_IPOAPPLY",
        }
    for row in bse_rows:
        code = str(row.get("SECURITY_CODE") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        ipo_map[code] = {
            "security_code": code,
            "security_name": str(row.get("SECURITY_NAME_ABBR") or row.get("SECURITY_NAME") or "").strip(),
            "apply_date": date_text(row.get("APPLY_DATE")),
            "listing_date": date_text(row.get("SELECT_LISTING_DATE") or row.get("LISTING_DATE")),
            "ipo_source_report": "RPT_NEEQ_ISSUEINFO_LIST",
        }
    return ipo_map


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    columns, report_rows = read_csv(SOURCE_MASTER)
    ipo_map = fetch_recent_ipo_map()

    through_cutoff = [
        row for row in report_rows
        if row.get("announcement_date") and row["announcement_date"] <= REFERENCE_CUTOFF
    ]
    post_cutoff = [
        row for row in report_rows
        if not row.get("announcement_date") or row["announcement_date"] > REFERENCE_CUTOFF
    ]

    prelisting_rows: list[dict[str, str]] = []
    prelisting_audit_rows: list[dict[str, Any]] = []
    reference_rows: list[dict[str, str]] = []
    for row in through_cutoff:
        code = row.get("security_code") or ""
        ipo = ipo_map.get(code)
        listing_date = (ipo or {}).get("listing_date", "")
        # A recent IPO record with a future or missing listing date was not an
        # A-share listed company at the 2026-08-31 cutoff.
        is_prelisting = bool(ipo) and (not listing_date or listing_date > REFERENCE_CUTOFF)
        if is_prelisting:
            prelisting_rows.append(row)
            prelisting_audit_rows.append({
                **row,
                "ipo_apply_date": ipo.get("apply_date", ""),
                "ipo_listing_date": listing_date,
                "ipo_source_report": ipo.get("ipo_source_report", ""),
                "exclusion_reason": "listing_date_after_cutoff" if listing_date else "ipo_listing_date_not_yet_set",
            })
        else:
            reference_rows.append(row)

    reference_csv = DATA_DIR / "a_share_2026_h1_5550_master.csv"
    reference_jsonl = DATA_DIR / "a_share_2026_h1_5550_master.jsonl"
    source_current_csv = DATA_DIR / "a_share_2026_h1_source_current.csv"
    post_cutoff_csv = META_DIR / "post_cutoff_records_2026-09-01.csv"
    prelisting_csv = META_DIR / "excluded_not_listed_by_2026-08-31.csv"
    ipo_map_json = META_DIR / "recent_ipo_listing_date_map.json"

    shutil.copy2(SOURCE_MASTER, source_current_csv)
    write_csv(reference_csv, columns, reference_rows)
    write_jsonl(reference_jsonl, reference_rows)
    write_csv(post_cutoff_csv, columns, post_cutoff)
    audit_columns = columns + [
        "ipo_apply_date", "ipo_listing_date", "ipo_source_report", "exclusion_reason"
    ]
    write_csv(prelisting_csv, audit_columns, prelisting_audit_rows)
    ipo_map_json.write_text(json.dumps(ipo_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
        "excluded_not_listed_by_cutoff_count": len(prelisting_rows),
        "archived_reference_universe_count": len(reference_rows),
        "reference_universe_matches_5550": len(reference_rows) == EXPECTED_REFERENCE_COUNT,
        "recent_ipo_map_count": len(ipo_map),
        "reference_universe_file": reference_csv.relative_to(ROOT).as_posix(),
        "reference_universe_jsonl": reference_jsonl.relative_to(ROOT).as_posix(),
        "post_cutoff_audit_file": post_cutoff_csv.relative_to(ROOT).as_posix(),
        "prelisting_audit_file": prelisting_csv.relative_to(ROOT).as_posix(),
        "ipo_map_audit_file": ipo_map_json.relative_to(ROOT).as_posix(),
    })
    summary["notes"] = [
        "The source-derived 5,566 A-share/CDR records include one post-cutoff record and pre-listing IPO records whose listing date is later than 2026-08-31.",
        "The exact 5,550-company reference archive is produced by announcement-date and IPO-listing-date rules, not arbitrary truncation.",
        "Every excluded record remains in an audit file.",
        "Official exchange filings remain the legal source of record.",
        "Prior conversation frequency never affects candidate generation or scoring."
    ]
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    checksum_paths = [
        reference_csv, reference_jsonl, source_current_csv, SOURCE_JSONL,
        post_cutoff_csv, prelisting_csv, ipo_map_json, status_path,
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
        "excluded_not_listed_by_cutoff_count": len(prelisting_rows),
        "reference_count": len(reference_rows),
        "matches_expected_5550": len(reference_rows) == EXPECTED_REFERENCE_COUNT,
        "excluded_not_listed_by_cutoff": [
            {
                "code": row.get("security_code"),
                "name": row.get("security_name"),
                "apply_date": audit.get("ipo_apply_date"),
                "listing_date": audit.get("ipo_listing_date"),
                "reason": audit.get("exclusion_reason"),
            }
            for row, audit in zip(prelisting_rows, prelisting_audit_rows)
        ],
        "post_cutoff_records": [
            {
                "code": row.get("security_code"),
                "name": row.get("security_name"),
                "announcement_date": row.get("announcement_date"),
            }
            for row in post_cutoff
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if len(reference_rows) == EXPECTED_REFERENCE_COUNT else 1


if __name__ == "__main__":
    raise SystemExit(main())
