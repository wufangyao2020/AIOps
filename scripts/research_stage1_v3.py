#!/usr/bin/env python3
"""Production Stage-1 runner with resilient public-data fallbacks.

In addition to the v2 data-quality controls, this runner:
- caches every slow statement-table response as compressed JSON;
- retrieves quotes through Eastmoney clist, then Eastmoney batched ulist,
  and finally Sina Market Center instead of relying on one numbered host;
- degrades analyst-coverage data gracefully while recording the failure;
- never silently proceeds without broad quote coverage.
"""
from __future__ import annotations

import gzip
import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

import research_stage1 as base
import research_stage1_v2 as v2  # noqa: F401  # applies market/field/scoring hardening

CACHE_DIR = base.RAW_DIR / "stage1_inputs"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

EASTMONEY_QUOTE_HOSTS = [
    "https://push2.eastmoney.com",
    "https://7.push2.eastmoney.com",
    "https://72.push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://88.push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
]
QUOTE_FIELDS = "f2,f3,f5,f6,f8,f9,f10,f12,f13,f14,f20,f21,f23,f24,f25,f100"


def cache_name(report_name: str, date_field: str, report_date: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{report_name}_{date_field}_{report_date}")
    return CACHE_DIR / f"{safe}.json.gz"


_original_fetch_datacenter = base.fetch_datacenter


def fetch_datacenter_cached(report_name: str, date_field: str, report_date: str):
    path = cache_name(report_name, date_field, report_date)
    if path.exists() and path.stat().st_size > 100:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, list) and payload:
            return payload
    rows = _original_fetch_datacenter(report_name, date_field, report_date)
    temp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temp, "wt", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, separators=(",", ":"))
    temp.replace(path)
    return rows


def fresh_json_get(url: str, params: dict[str, Any], attempts: int = 3) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            headers = {
                **base.HEADERS,
                "Connection": "close",
                "Referer": "https://quote.eastmoney.com/center/gridlist.html#hs_a_board",
            }
            response = requests.get(url, params={**params, "_": str(int(time.time() * 1000) + random.randint(0, 999))}, headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("quote endpoint returned non-object JSON")
            return payload
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(1.5 ** attempt, 5))
    raise RuntimeError(f"quote request failed: {url}: {last}")


def normalize_eastmoney_quote_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    mapping = {
        "f12": "security_code", "f14": "quote_name", "f2": "price", "f3": "daily_return_pct",
        "f5": "volume", "f6": "turnover_amount_cny", "f8": "turnover_rate_pct", "f9": "pe_dynamic",
        "f10": "volume_ratio", "f20": "market_cap_cny", "f21": "float_market_cap_cny", "f23": "pb",
        "f24": "return_60d_pct", "f25": "return_ytd_pct", "f100": "quote_industry",
    }
    frame = pd.DataFrame(rows).rename(columns=mapping)
    columns = list(mapping.values())
    for col in columns:
        if col not in frame:
            frame[col] = np.nan if col not in {"security_code", "quote_name", "quote_industry"} else ""
    frame = frame[columns]
    frame["security_code"] = frame["security_code"].astype(str).str.zfill(6)
    for col in [c for c in columns if c not in {"security_code", "quote_name", "quote_industry"}]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.drop_duplicates("security_code", keep="last")


def eastmoney_clist_quotes() -> pd.DataFrame:
    groups = [
        "m:0 t:6,m:0 t:80",
        "m:1 t:2,m:1 t:23",
        "m:0 t:81 s:2048",
    ]
    all_rows: list[dict[str, Any]] = []
    for fs in groups:
        page = 1
        total = None
        while True:
            params = {
                "pn": str(page), "pz": "100", "po": "1", "np": "1",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
                "fid": "f12", "fs": fs, "fields": QUOTE_FIELDS,
            }
            payload = None
            errors = []
            for host in EASTMONEY_QUOTE_HOSTS:
                try:
                    payload = fresh_json_get(f"{host}/api/qt/clist/get", params, attempts=2)
                    if (payload.get("data") or {}).get("diff") is not None:
                        break
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
            if payload is None:
                raise RuntimeError("all Eastmoney clist hosts failed: " + " | ".join(errors[-3:]))
            data = payload.get("data") or {}
            diff = data.get("diff") or []
            all_rows.extend(diff)
            total = int(data.get("total") or len(all_rows)) if total is None else total
            if not diff or page * 100 >= total:
                break
            page += 1
            if page > 80:
                raise RuntimeError(f"clist paging exceeded safety limit for fs={fs}")
            time.sleep(0.05)
    result = normalize_eastmoney_quote_rows(all_rows)
    if len(result) < 4800:
        raise RuntimeError(f"Eastmoney clist quote coverage too low: {len(result)}")
    return result


def market_to_secid(exchange: str, code: str) -> str:
    return f"1.{code}" if exchange == "SSE" else f"0.{code}"


def eastmoney_ulist_quotes() -> pd.DataFrame:
    master = pd.read_csv(base.MASTER, dtype={"security_code": str}, usecols=["security_code", "exchange"])
    master["security_code"] = master["security_code"].astype(str).str.zfill(6)
    secids = [market_to_secid(str(r.exchange), str(r.security_code)) for r in master.itertuples(index=False)]
    all_rows: list[dict[str, Any]] = []
    for start in range(0, len(secids), 40):
        batch = secids[start:start + 40]
        params = {
            "fltt": "2", "invt": "2", "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fields": QUOTE_FIELDS, "secids": ",".join(batch),
        }
        payload = None
        errors = []
        for host in EASTMONEY_QUOTE_HOSTS:
            try:
                payload = fresh_json_get(f"{host}/api/qt/ulist.np/get", params, attempts=2)
                if (payload.get("data") or {}).get("diff") is not None:
                    break
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        if payload is None:
            raise RuntimeError(f"all ulist hosts failed at batch {start}: {' | '.join(errors[-3:])}")
        all_rows.extend((payload.get("data") or {}).get("diff") or [])
        time.sleep(0.04)
    result = normalize_eastmoney_quote_rows(all_rows)
    if len(result) < 4800:
        raise RuntimeError(f"Eastmoney ulist quote coverage too low: {len(result)}")
    return result


def parse_sina_rows(raw: str) -> list[dict[str, Any]]:
    value = raw.strip().replace("'", '"')
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        # Legacy endpoint sometimes emits JavaScript object literals with unquoted keys.
        fixed = re.sub(r"([\{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', value)
        fixed = re.sub(r"\b(?:NaN|undefined)\b", "null", fixed)
        parsed = json.loads(fixed)
        return parsed if isinstance(parsed, list) else []


def sina_quotes() -> pd.DataFrame:
    urls = [
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
    ]
    rows: list[dict[str, Any]] = []
    for page in range(1, 90):
        params = {"page": page, "num": 80, "sort": "symbol", "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "auto"}
        page_rows = None
        for url in urls:
            try:
                response = requests.get(
                    url, params=params,
                    headers={"User-Agent": base.HEADERS["User-Agent"], "Referer": "https://vip.stock.finance.sina.com.cn/"},
                    timeout=30,
                )
                response.raise_for_status()
                response.encoding = response.apparent_encoding or "utf-8"
                page_rows = parse_sina_rows(response.text)
                if page_rows is not None:
                    break
            except Exception:  # noqa: BLE001
                continue
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < 80:
            break
        time.sleep(0.05)
    records = []
    for row in rows:
        records.append({
            "security_code": str(row.get("code") or "").zfill(6),
            "quote_name": row.get("name") or "",
            "price": base.to_num(row.get("trade")),
            "daily_return_pct": base.to_num(row.get("changepercent")),
            "volume": base.to_num(row.get("volume")),
            "turnover_amount_cny": base.to_num(row.get("amount")),
            "turnover_rate_pct": base.to_num(row.get("turnoverratio")),
            "pe_dynamic": base.to_num(row.get("per")),
            "volume_ratio": np.nan,
            "market_cap_cny": base.to_num(row.get("mktcap")) * 10000 if np.isfinite(base.to_num(row.get("mktcap"))) else np.nan,
            "float_market_cap_cny": base.to_num(row.get("nmc") or row.get("nmcap")) * 10000 if np.isfinite(base.to_num(row.get("nmc") or row.get("nmcap"))) else np.nan,
            "pb": base.to_num(row.get("pb")),
            "return_60d_pct": np.nan,
            "return_ytd_pct": np.nan,
            "quote_industry": "",
        })
    result = pd.DataFrame(records).drop_duplicates("security_code", keep="last")
    if len(result) < 4800:
        raise RuntimeError(f"Sina quote coverage too low: {len(result)}")
    return result


def fetch_quote_snapshot_resilient() -> pd.DataFrame:
    cache = CACHE_DIR / "quote_snapshot.csv.gz"
    if cache.exists() and cache.stat().st_size > 1000:
        frame = pd.read_csv(cache, dtype={"security_code": str})
        if len(frame) >= 4800:
            frame["security_code"] = frame["security_code"].astype(str).str.zfill(6)
            return frame
    attempts = [eastmoney_clist_quotes, eastmoney_ulist_quotes, sina_quotes]
    errors = []
    for method in attempts:
        try:
            frame = method()
            frame.to_csv(cache, index=False, compression="gzip", encoding="utf-8")
            (CACHE_DIR / "quote_source.json").write_text(
                json.dumps({"method": method.__name__, "rows": len(frame), "as_of": "2026-09-02"}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return frame
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{method.__name__}: {exc}")
    raise RuntimeError("all quote sources failed: " + " || ".join(errors))


_original_fetch_research_reports = base.fetch_research_reports


def fetch_research_reports_resilient() -> pd.DataFrame:
    cache = CACHE_DIR / "research_report_aggregate.csv.gz"
    if cache.exists() and cache.stat().st_size > 100:
        return pd.read_csv(cache, dtype={"security_code": str})
    try:
        frame = _original_fetch_research_reports()
        frame.to_csv(cache, index=False, compression="gzip", encoding="utf-8")
        return frame
    except Exception as exc:  # noqa: BLE001
        (CACHE_DIR / "research_report_error.txt").write_text(str(exc), encoding="utf-8")
        # Coverage is a non-consensus proxy, not a core accounting fact.  Missing
        # coverage stays explicit as zero and is disclosed in the run summary.
        return pd.DataFrame(columns=["security_code", "report_count_12m", "report_org_count_12m"])


base.fetch_datacenter = fetch_datacenter_cached
base.fetch_quote_snapshot = fetch_quote_snapshot_resilient
base.fetch_research_reports = fetch_research_reports_resilient

if __name__ == "__main__":
    raise SystemExit(base.main())
