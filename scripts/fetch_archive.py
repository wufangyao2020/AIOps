#!/usr/bin/env python3
"""Archive the 2026 H1 A-share/CDR universe before any research scoring.

Eastmoney's source report also contains New Third Board and Old Third Board
securities. This script fetches the full source table, then uses explicit
SECURITY_TYPE_CODE and TRADE_MARKET_CODE allow-lists. No company is selected,
ranked, or favored by prior conversation context at this stage.
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
REFERENCE_DISCLOSED_COUNT = 5550
PAGE_SIZE = 500
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / REPORT_PERIOD
CURRENT_DIR = ROOT / "data" / "current"
META_DIR = ROOT / "meta"
STATUS_DIR = ROOT / "status"

ENDPOINTS = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get",
    "https://datacenter.eastmoney.com/securities/api/data/v1/get",
)
A_SHARE_TYPES = {"058001001", "058001008"}  # A股 + 中国存托凭证
ALLOWED_MARKETS = {
    "069001001001": ("SSE", "Main Board"),
    "069001001003": ("SSE", "Risk Warning Board"),
    "069001001006": ("SSE", "STAR Market"),
    "069001002001": ("SZSE", "Main Board"),
    "069001002002": ("SZSE", "ChiNext"),
    "069001002005": ("SZSE", "Risk Warning Board"),
    "069001017": ("BSE", "Beijing Stock Exchange"),
}

COLUMNS = [
    "security_code", "security_name", "secucode", "org_code",
    "security_type_code", "security_type", "trade_market_code", "trade_market",
    "exchange", "board", "industry", "report_period", "report_date",
    "announcement_date", "update_date", "basic_eps", "book_value_per_share",
    "revenue_cny", "revenue_yoy_pct", "revenue_qoq_pct",
    "parent_net_profit_cny", "parent_net_profit_yoy_pct",
    "parent_net_profit_qoq_pct", "weighted_roe_pct",
    "operating_cash_flow_per_share", "gross_margin_pct",
    "profit_distribution_description", "disclosure_status", "source_name",
    "source_page_url", "source_api_report", "research_stage",
    "fundamental_acceleration_status", "new_profit_pool_status",
    "cyclical_turn_status", "capital_event_status",
    "announcement_review_status", "peer_comparison_status", "data_quality_flag",
]


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def number(value: Any) -> float | int | str:
    if value is None or value == "":
        return ""
    try:
        parsed = float(value)
        return int(parsed) if parsed.is_integer() else parsed
    except (TypeError, ValueError):
        return text(value)


def date_text(value: Any) -> str:
    value_text = text(value)
    return value_text[:10] if len(value_text) >= 10 else value_text


def request_json(url: str, params: dict[str, str], attempts: int = 5) -> dict[str, Any]:
    full_url = f"{url}?{urlencode(params)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://data.eastmoney.com/bbsj/202606/yjbb.html",
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(Request(full_url, headers=headers), timeout=45) as response:
                payload = response.read().decode("utf-8")
            result = json.loads(payload)
            if result.get("result") is None:
                raise RuntimeError(result.get("message") or result.get("msg") or "empty API result")
            return result
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"Failed fetching {full_url}: {last_error}")


def fetch_source_rows() -> tuple[list[dict[str, Any]], str]:
    errors: list[str] = []
    for endpoint in ENDPOINTS:
        try:
            params = {
                "sortColumns": "UPDATE_DATE,SECURITY_CODE",
                "sortTypes": "-1,-1",
                "pageSize": str(PAGE_SIZE),
                "reportName": "RPT_LICO_FN_CPD",
                "columns": "ALL",
                "source": "WEB",
                "client": "WEB",
                "filter": f"(REPORTDATE='{REPORT_DATE}')",
            }
            first = request_json(endpoint, {**params, "pageNumber": "1"})
            first_result = first["result"]
            pages = int(first_result.get("pages") or 1)
            rows: list[dict[str, Any]] = list(first_result.get("data") or [])
            for page in range(2, pages + 1):
                payload = request_json(endpoint, {**params, "pageNumber": str(page)})
                rows.extend(payload["result"].get("data") or [])
            if rows:
                return rows, endpoint
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("All source endpoints failed:\n" + "\n".join(errors))


def is_a_share_or_cdr(row: dict[str, Any]) -> bool:
    code = text(row.get("SECURITY_CODE"))
    return (
        text(row.get("SECURITY_TYPE_CODE")) in A_SHARE_TYPES
        and text(row.get("TRADE_MARKET_CODE")) in ALLOWED_MARKETS
        and len(code) == 6
        and code.isdigit()
    )


def select_latest_unique(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not is_a_share_or_cdr(row):
            continue
        code = text(row.get("SECURITY_CODE"))
        existing = latest.get(code)
        if existing is None:
            latest[code] = row
            continue
        new_stamp = text(row.get("UPDATE_DATE")) or text(row.get("NOTICE_DATE"))
        old_stamp = text(existing.get("UPDATE_DATE")) or text(existing.get("NOTICE_DATE"))
        if new_stamp > old_stamp:
            latest[code] = row
    return [latest[code] for code in sorted(latest)]


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    market_code = text(row.get("TRADE_MARKET_CODE"))
    exchange, board = ALLOWED_MARKETS[market_code]
    core = (row.get("TOTAL_OPERATE_INCOME"), row.get("PARENT_NETPROFIT"), row.get("NOTICE_DATE"))
    missing_core = sum(value in (None, "") for value in core)
    return {
        "security_code": text(row.get("SECURITY_CODE")),
        "security_name": text(row.get("SECURITY_NAME_ABBR")),
        "secucode": text(row.get("SECUCODE")),
        "org_code": text(row.get("ORG_CODE")),
        "security_type_code": text(row.get("SECURITY_TYPE_CODE")),
        "security_type": text(row.get("SECURITY_TYPE")),
        "trade_market_code": market_code,
        "trade_market": text(row.get("TRADE_MARKET")),
        "exchange": exchange,
        "board": board,
        "industry": text(row.get("INDUSTRY_NAME")),
        "report_period": REPORT_PERIOD,
        "report_date": date_text(row.get("REPORTDATE") or REPORT_DATE),
        "announcement_date": date_text(row.get("NOTICE_DATE")),
        "update_date": date_text(row.get("UPDATE_DATE")),
        "basic_eps": number(row.get("BASIC_EPS")),
        "book_value_per_share": number(row.get("BPS")),
        "revenue_cny": number(row.get("TOTAL_OPERATE_INCOME")),
        "revenue_yoy_pct": number(row.get("YSTZ")),
        "revenue_qoq_pct": number(row.get("YSHZ")),
        "parent_net_profit_cny": number(row.get("PARENT_NETPROFIT")),
        "parent_net_profit_yoy_pct": number(row.get("SJLTZ")),
        "parent_net_profit_qoq_pct": number(row.get("SJLHZ")),
        "weighted_roe_pct": number(row.get("WEIGHTAVG_ROE")),
        "operating_cash_flow_per_share": number(row.get("MGJYXJJE")),
        "gross_margin_pct": number(row.get("XSMLL")),
        "profit_distribution_description": text(row.get("ASSIGNDSCRPT")),
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
        "data_quality_flag": "ok" if missing_core == 0 else f"missing_core_{missing_core}",
    }


def write_csv(path: Path, records: list[dict[str, Any]], columns: list[str] = COLUMNS) -> None:
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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    for directory in (DATA_DIR, CURRENT_DIR, META_DIR, STATUS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    source_rows, endpoint = fetch_source_rows()
    selected_rows = select_latest_unique(source_rows)
    records = [normalize(row) for row in selected_rows]
    excluded_rows = [row for row in source_rows if not is_a_share_or_cdr(row)]

    master_csv = DATA_DIR / "a_share_2026_h1_master.csv"
    master_jsonl = DATA_DIR / "a_share_2026_h1_master.jsonl"
    raw_jsonl = DATA_DIR / "a_share_2026_h1_raw.jsonl"
    excluded_jsonl = DATA_DIR / "excluded_non_a_share_rows.jsonl"
    status_csv = STATUS_DIR / "research_status_2026_h1.csv"

    write_csv(master_csv, records)
    write_jsonl(master_jsonl, records)
    write_jsonl(raw_jsonl, selected_rows)
    write_jsonl(excluded_jsonl, excluded_rows)

    status_columns = [
        "security_code", "security_name", "exchange", "board", "industry",
        "report_period", "research_stage", "fundamental_acceleration_status",
        "new_profit_pool_status", "cyclical_turn_status", "capital_event_status",
        "announcement_review_status", "peer_comparison_status", "data_quality_flag",
    ]
    write_csv(status_csv, records, status_columns)

    for exchange in ("SSE", "SZSE", "BSE"):
        write_csv(
            DATA_DIR / "by_exchange" / f"{exchange.lower()}_2026_h1.csv",
            [record for record in records if record["exchange"] == exchange],
        )

    shutil.copy2(master_csv, CURRENT_DIR / "a_share_master.csv")
    shutil.copy2(status_csv, CURRENT_DIR / "research_status.csv")

    announcement_dates = [record["announcement_date"] for record in records if record["announcement_date"]]
    summary = {
        "dataset": "A-share 2026 H1 company universe",
        "report_period": REPORT_PERIOD,
        "report_date": REPORT_DATE,
        "snapshot_created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_endpoint": endpoint,
        "source_page": "https://data.eastmoney.com/bbsj/202606/yjbb.html",
        "source_report_name": "RPT_LICO_FN_CPD",
        "source_total_row_count": len(source_rows),
        "excluded_non_a_share_row_count": len(excluded_rows),
        "archived_unique_a_share_or_cdr_count": len(records),
        "public_reference_disclosed_count": REFERENCE_DISCLOSED_COUNT,
        "reference_count_gap": len(records) - REFERENCE_DISCLOSED_COUNT,
        "reference_count_matches": len(records) == REFERENCE_DISCLOSED_COUNT,
        "included_security_type_counts": dict(Counter(record["security_type"] for record in records)),
        "included_exchange_counts": dict(Counter(record["exchange"] for record in records)),
        "included_board_counts": dict(Counter(record["board"] for record in records)),
        "source_security_type_counts": dict(Counter(text(row.get("SECURITY_TYPE")) or "UNKNOWN" for row in source_rows)),
        "source_trade_market_counts": dict(Counter(text(row.get("TRADE_MARKET")) or "UNKNOWN" for row in source_rows)),
        "announcement_date_min": min(announcement_dates) if announcement_dates else "",
        "announcement_date_max": max(announcement_dates) if announcement_dates else "",
        "data_quality_counts": dict(Counter(record["data_quality_flag"] for record in records)),
        "research_state": {
            "raw_universe_archived": True,
            "four_model_scoring_completed": False,
            "announcement_review_completed": False,
            "peer_comparison_completed": False,
            "final_20_selected": False,
        },
        "notes": [
            "Third Board securities are explicitly excluded by source security and market codes.",
            "The source-derived count is retained even if it differs from the media-reported 5550 reference; rows are never arbitrarily deleted to force a match.",
            "Official exchange filings remain the legal source of record.",
            "Prior conversation frequency never affects candidate generation or scoring."
        ],
    }
    summary_path = META_DIR / "snapshot_2026_h1.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    checksum_paths = (master_csv, master_jsonl, raw_jsonl, excluded_jsonl, status_csv)
    (META_DIR / "checksums_2026_h1.sha256").write_text(
        "\n".join(f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}" for path in checksum_paths) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if len(records) != REFERENCE_DISCLOSED_COUNT:
        print(
            f"NOTICE: source-derived count {len(records)} differs from public reference {REFERENCE_DISCLOSED_COUNT}; gap retained for reconciliation.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
