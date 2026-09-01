#!/usr/bin/env python3
"""Build the 2026 H1 A-share company archive from Eastmoney's public data API.

The script performs a blind universe pull before any research scoring. It writes
one normalized master CSV, a raw JSONL snapshot, exchange splits, metadata, and
an all-company research-status table.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPORT_DATE = "2026-06-30"
REPORT_PERIOD = "2026H1"
EXPECTED_PUBLIC_COUNT = 5550
PAGE_SIZE = 500
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / REPORT_PERIOD
CURRENT_DIR = ROOT / "data" / "current"
META_DIR = ROOT / "meta"
STATUS_DIR = ROOT / "status"

ENDPOINTS = [
    "https://datacenter-web.eastmoney.com/api/data/v1/get",
    "https://datacenter.eastmoney.com/securities/api/data/v1/get",
]
A_SHARE_TYPES = {"058001001", "058001008"}

NORMALIZED_COLUMNS = [
    "security_code", "security_name", "exchange", "board", "industry",
    "report_period", "report_date", "announcement_date", "update_date",
    "basic_eps", "book_value_per_share", "revenue_cny", "revenue_yoy_pct",
    "revenue_qoq_pct", "parent_net_profit_cny",
    "parent_net_profit_yoy_pct", "parent_net_profit_qoq_pct",
    "weighted_roe_pct", "operating_cash_flow_per_share", "gross_margin_pct",
    "profit_distribution_description", "disclosure_status", "source_name",
    "source_page_url", "source_api_report", "research_stage",
    "fundamental_acceleration_status", "new_profit_pool_status",
    "cyclical_turn_status", "capital_event_status",
    "announcement_review_status", "peer_comparison_status",
    "data_quality_flag",
]


def _request_json(url: str, params: dict[str, str], attempts: int = 5) -> dict[str, Any]:
    query = urlencode(params, safe="(),'=\"")
    full_url = f"{url}?{query}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://data.eastmoney.com/bbsj/202606/yjbb.html",
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = Request(full_url, headers=headers)
            with urlopen(req, timeout=45) as resp:
                payload = resp.read().decode("utf-8")
            data = json.loads(payload)
            if not data.get("success", False) and data.get("result") is None:
                raise RuntimeError(f"API returned failure: {data.get('message') or data.get('msg')}")
            return data
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"Failed fetching {full_url}: {last_error}")


def fetch_rows() -> tuple[list[dict[str, Any]], str, str]:
    filters = [
        f"(REPORTDATE='{REPORT_DATE}')(SECURITY_TYPE_CODE in (\"058001001\",\"058001008\"))",
        f"(REPORTDATE='{REPORT_DATE}')",
    ]
    errors: list[str] = []
    for endpoint in ENDPOINTS:
        for filter_expr in filters:
            try:
                base_params = {
                    "sortColumns": "UPDATE_DATE,SECURITY_CODE",
                    "sortTypes": "-1,-1",
                    "pageSize": str(PAGE_SIZE),
                    "reportName": "RPT_LICO_FN_CPD",
                    "columns": "ALL",
                    "source": "WEB",
                    "client": "WEB",
                    "filter": filter_expr,
                }
                first = _request_json(endpoint, {**base_params, "pageNumber": "1"})
                result = first.get("result") or {}
                pages = int(result.get("pages") or 1)
                rows: list[dict[str, Any]] = list(result.get("data") or [])
                for page in range(2, pages + 1):
                    payload = _request_json(endpoint, {**base_params, "pageNumber": str(page)})
                    rows.extend((payload.get("result") or {}).get("data") or [])
                if rows:
                    return rows, endpoint, filter_expr
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{endpoint} | {filter_expr} | {exc}")
    raise RuntimeError("All endpoint/filter combinations failed:\n" + "\n".join(errors))


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> float | int | str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return _text(value)


def _date(value: Any) -> str:
    text = _text(value)
    return text[:10] if len(text) >= 10 else text


def classify_market(code: str, row: dict[str, Any]) -> tuple[str, str]:
    market = _text(row.get("TRADE_MARKET")) or _text(row.get("TRADE_MARKET_CODE"))
    if code.startswith(("688", "689")):
        return "SSE", "STAR Market"
    if code.startswith(("600", "601", "603", "605")):
        return "SSE", "Main Board"
    if code.startswith(("300", "301")):
        return "SZSE", "ChiNext"
    if code.startswith(("000", "001", "002", "003")):
        return "SZSE", "Main Board"
    if code.startswith(("4", "8", "9")) and not code.startswith("900"):
        return "BSE", "Beijing Stock Exchange"
    if "上海" in market:
        return "SSE", "Unknown"
    if "深圳" in market:
        return "SZSE", "Unknown"
    if "北京" in market or "北交" in market:
        return "BSE", "Beijing Stock Exchange"
    return "UNKNOWN", "Unknown"


def looks_like_a_share(row: dict[str, Any]) -> bool:
    code = _text(row.get("SECURITY_CODE"))
    name = _text(row.get("SECURITY_NAME_ABBR"))
    sec_type = _text(row.get("SECURITY_TYPE_CODE"))
    if len(code) != 6 or not code.isdigit():
        return False
    if code.startswith(("200", "201", "900")) or name.endswith("B"):
        return False
    if sec_type and sec_type in A_SHARE_TYPES:
        return True
    return code.startswith((
        "000", "001", "002", "003", "300", "301", "600", "601",
        "603", "605", "688", "689", "4", "8", "92",
    ))


def deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not looks_like_a_share(row):
            continue
        code = _text(row.get("SECURITY_CODE"))
        existing = by_code.get(code)
        if existing is None:
            by_code[code] = row
            continue
        new_stamp = _text(row.get("UPDATE_DATE")) or _text(row.get("NOTICE_DATE"))
        old_stamp = _text(existing.get("UPDATE_DATE")) or _text(existing.get("NOTICE_DATE"))
        if new_stamp > old_stamp:
            by_code[code] = row
    return [by_code[code] for code in sorted(by_code)]


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    code = _text(row.get("SECURITY_CODE"))
    exchange, board = classify_market(code, row)
    core_values = [row.get("TOTAL_OPERATE_INCOME"), row.get("PARENT_NETPROFIT"), row.get("NOTICE_DATE")]
    missing_core = sum(value in (None, "") for value in core_values)
    quality = "ok" if missing_core == 0 else f"missing_core_{missing_core}"
    return {
        "security_code": code,
        "security_name": _text(row.get("SECURITY_NAME_ABBR")),
        "exchange": exchange,
        "board": board,
        "industry": _text(row.get("INDUSTRY_NAME")),
        "report_period": REPORT_PERIOD,
        "report_date": _date(row.get("REPORTDATE") or REPORT_DATE),
        "announcement_date": _date(row.get("NOTICE_DATE")),
        "update_date": _date(row.get("UPDATE_DATE")),
        "basic_eps": _number(row.get("BASIC_EPS")),
        "book_value_per_share": _number(row.get("BPS")),
        "revenue_cny": _number(row.get("TOTAL_OPERATE_INCOME")),
        "revenue_yoy_pct": _number(row.get("YSTZ")),
        "revenue_qoq_pct": _number(row.get("YSHZ")),
        "parent_net_profit_cny": _number(row.get("PARENT_NETPROFIT")),
        "parent_net_profit_yoy_pct": _number(row.get("SJLTZ")),
        "parent_net_profit_qoq_pct": _number(row.get("SJLHZ")),
        "weighted_roe_pct": _number(row.get("WEIGHTAVG_ROE")),
        "operating_cash_flow_per_share": _number(row.get("MGJYXJJE")),
        "gross_margin_pct": _number(row.get("XSMLL")),
        "profit_distribution_description": _text(row.get("ASSIGNDSCRPT")),
        "disclosure_status": "disclosed",
        "source_name": "Eastmoney Data Center / Choice public web aggregation",
        "source_page_url": "https://data.eastmoney.com/bbsj/202606/yjbb.html",
        "source_api_report": "RPT_LICO_FN_CPD",
        "research_stage": "stage_0_raw_archive",
        "fundamental_acceleration_status": "pending",
        "new_profit_pool_status": "pending",
        "cyclical_turn_status": "pending",
        "capital_event_status": "pending",
        "announcement_review_status": "pending",
        "peer_comparison_status": "pending",
        "data_quality_flag": quality,
    }


def write_csv(path: Path, records: list[dict[str, Any]], columns: list[str] = NORMALIZED_COLUMNS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    for directory in (DATA_DIR, CURRENT_DIR, META_DIR, STATUS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    raw_rows, endpoint, filter_expr = fetch_rows()
    rows = deduplicate(raw_rows)
    records = [normalize(row) for row in rows]

    master_csv = DATA_DIR / "a_share_2026_h1_master.csv"
    normalized_jsonl = DATA_DIR / "a_share_2026_h1_master.jsonl"
    raw_jsonl = DATA_DIR / "a_share_2026_h1_raw.jsonl"
    status_csv = STATUS_DIR / "research_status_2026_h1.csv"

    write_csv(master_csv, records)
    write_jsonl(normalized_jsonl, records)
    write_jsonl(raw_jsonl, rows)

    status_columns = [
        "security_code", "security_name", "exchange", "board", "industry",
        "report_period", "research_stage", "fundamental_acceleration_status",
        "new_profit_pool_status", "cyclical_turn_status", "capital_event_status",
        "announcement_review_status", "peer_comparison_status", "data_quality_flag",
    ]
    write_csv(status_csv, records, status_columns)

    by_exchange: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_exchange.setdefault(str(record["exchange"]), []).append(record)
    for exchange, subset in sorted(by_exchange.items()):
        write_csv(DATA_DIR / "by_exchange" / f"{exchange.lower()}_2026_h1.csv", subset)

    shutil.copy2(master_csv, CURRENT_DIR / "a_share_master.csv")
    shutil.copy2(status_csv, CURRENT_DIR / "research_status.csv")

    board_counts = Counter(str(item["board"]) for item in records)
    exchange_counts = Counter(str(item["exchange"]) for item in records)
    industry_counts = Counter(str(item["industry"] or "UNKNOWN") for item in records)
    quality_counts = Counter(str(item["data_quality_flag"]) for item in records)
    snapshot_time = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    count_matches = len(records) == EXPECTED_PUBLIC_COUNT

    summary = {
        "dataset": "A-share 2026 H1 company universe",
        "report_period": REPORT_PERIOD,
        "report_date": REPORT_DATE,
        "snapshot_created_at_utc": snapshot_time,
        "source_endpoint": endpoint,
        "source_filter": filter_expr,
        "source_page": "https://data.eastmoney.com/bbsj/202606/yjbb.html",
        "publicly_reported_universe_count": EXPECTED_PUBLIC_COUNT,
        "archived_unique_company_count": len(records),
        "count_matches_public_report": count_matches,
        "raw_api_row_count": len(raw_rows),
        "exchange_counts": dict(sorted(exchange_counts.items())),
        "board_counts": dict(sorted(board_counts.items())),
        "industry_count": len(industry_counts),
        "top_industries_by_company_count": industry_counts.most_common(30),
        "data_quality_counts": dict(sorted(quality_counts.items())),
        "research_state": {
            "raw_universe_archived": True,
            "four_model_scoring_completed": False,
            "announcement_review_completed": False,
            "peer_comparison_completed": False,
            "final_20_selected": False
        },
        "notes": [
            "This snapshot archives the full public 2026 H1 A-share performance table before model scoring.",
            "Eastmoney is a public aggregator; official exchange filings remain the legal source of record.",
            "No company receives a score boost from prior conversation frequency or familiarity."
        ]
    }
    (META_DIR / "snapshot_2026_h1.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    files_for_checksum = [master_csv, normalized_jsonl, raw_jsonl, status_csv]
    checksum_lines = [
        f"{sha256_file(path)}  {path.relative_to(ROOT).as_posix()}"
        for path in files_for_checksum
    ]
    (META_DIR / "checksums_2026_h1.sha256").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not count_matches:
        print(
            f"WARNING: archived {len(records)} companies; public report count is {EXPECTED_PUBLIC_COUNT}. "
            "The archive is retained with a mismatch flag for review.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
