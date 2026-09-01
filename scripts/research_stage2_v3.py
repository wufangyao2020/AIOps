#!/usr/bin/env python3
"""Final Stage-2 runner with top-100 market-cycle evidence.

Sina's stable all-market snapshot does not include 60-day/YTD returns.  Before
classifying discovery, diffusion or late emotion, this runner fetches daily
adjusted price history for each reviewed company, caches the result and computes
returns from actual closes.  It also neutralizes (rather than rewards) analyst
coverage when the report API was unavailable in Stage 1.
"""
from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

import research_stage2 as base
import research_stage2_v2 as v2  # noqa: F401  # JSONP/pagination/full-text hardening

CACHE = base.RESEARCH_DIR / "raw" / "stage2_inputs"
CACHE.mkdir(parents=True, exist_ok=True)
HOSTS = [
    "https://push2his.eastmoney.com",
    "https://7.push2his.eastmoney.com",
    "https://72.push2his.eastmoney.com",
]
HEADERS = {
    **base.HEADERS,
    "Connection": "close",
    "Referer": "https://quote.eastmoney.com/",
}
_thread = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread.s = s
    return _thread.s


def secid(exchange: str, code: str) -> str:
    return f"1.{code}" if str(exchange) == "SSE" else f"0.{code}"


def fetch_one_history(code: str, exchange: str) -> dict[str, Any]:
    params = {
        "secid": secid(exchange, code), "klt": "101", "fqt": "1", "lmt": "360",
        "end": "20260902", "iscca": "1", "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    last_error: Exception | None = None
    data = None
    for attempt in range(1, 5):
        for host in HOSTS:
            try:
                r = get_session().get(f"{host}/api/qt/stock/kline/get", params=params, timeout=25)
                r.raise_for_status()
                payload = r.json()
                data = payload.get("data") or {}
                if data.get("klines"):
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                data = None
        if data and data.get("klines"):
            break
        time.sleep(min(1.5 ** attempt, 6))
    if not data or not data.get("klines"):
        return {"security_code": code, "price_history_status": "failed", "price_history_error": str(last_error or "empty klines")}
    rows = []
    for item in data.get("klines") or []:
        parts = str(item).split(",")
        if len(parts) < 3:
            continue
        try:
            rows.append((pd.Timestamp(parts[0]), float(parts[2])))
        except Exception:
            continue
    if len(rows) < 30:
        return {"security_code": code, "price_history_status": "failed", "price_history_error": "insufficient rows"}
    prices = pd.DataFrame(rows, columns=["date", "close"]).sort_values("date").drop_duplicates("date")
    prices = prices[prices["date"] <= pd.Timestamp("2026-09-02")]
    last_close = float(prices.iloc[-1]["close"])
    return60 = np.nan
    if len(prices) >= 61:
        return60 = (last_close / float(prices.iloc[-61]["close"]) - 1) * 100
    prior_year = prices[prices["date"] <= pd.Timestamp("2025-12-31")]
    if not prior_year.empty:
        ytd = (last_close / float(prior_year.iloc[-1]["close"]) - 1) * 100
    else:
        ytd = np.nan
    first_date = str(prices.iloc[0]["date"].date())
    last_date = str(prices.iloc[-1]["date"].date())
    return {
        "security_code": code, "price_history_status": "ok", "history_rows": len(prices),
        "history_first_date": first_date, "history_last_date": last_date,
        "history_last_close": last_close, "return_60d_pct_history": return60,
        "return_ytd_pct_history": ytd,
    }


def load_price_history(reviewed: pd.DataFrame) -> pd.DataFrame:
    path = CACHE / "top100_price_returns.csv"
    wanted = reviewed[["security_code", "exchange"]].copy()
    wanted["security_code"] = wanted["security_code"].astype(str).str.zfill(6)
    existing = pd.DataFrame()
    if path.exists():
        existing = pd.read_csv(path, dtype={"security_code": str})
        existing["security_code"] = existing["security_code"].astype(str).str.zfill(6)
    done = set(existing.loc[existing.get("price_history_status", "") == "ok", "security_code"]) if not existing.empty else set()
    todo = [(str(r.security_code), str(r.exchange)) for r in wanted.itertuples(index=False) if str(r.security_code) not in done]
    results = []
    if todo:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(fetch_one_history, code, exchange): code for code, exchange in todo}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    results.append({"security_code": futures[future], "price_history_status": "failed", "price_history_error": str(exc)})
    combined = pd.concat([existing, pd.DataFrame(results)], ignore_index=True) if not existing.empty or results else pd.DataFrame()
    if combined.empty:
        return combined
    combined = combined.drop_duplicates("security_code", keep="last")
    combined.to_csv(path, index=False, encoding="utf-8-sig")
    return combined


_original_refine = base.refine


def refine_with_market_cycle(reviewed: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    reviewed = reviewed.copy()
    history = load_price_history(reviewed)
    if not history.empty:
        reviewed = reviewed.merge(history, on="security_code", how="left")
        observed60 = pd.to_numeric(reviewed.get("return_60d_pct_history"), errors="coerce")
        observedytd = pd.to_numeric(reviewed.get("return_ytd_pct_history"), errors="coerce")
        reviewed["return_60d_pct"] = observed60.combine_first(pd.to_numeric(reviewed.get("return_60d_pct"), errors="coerce"))
        reviewed["return_ytd_pct"] = observedytd.combine_first(pd.to_numeric(reviewed.get("return_ytd_pct"), errors="coerce"))
        for col in ("return_60d_pct", "return_ytd_pct"):
            mapping = reviewed.set_index("security_code")[col]
            universe[col] = universe["security_code"].map(mapping).combine_first(pd.to_numeric(universe.get(col), errors="coerce"))
    report_error = base.ROOT / "data" / "2026H1" / "research" / "raw" / "stage1_inputs" / "research_report_error.txt"
    report_counts = pd.to_numeric(reviewed.get("report_count_12m"), errors="coerce").fillna(0)
    if report_error.exists() and report_counts.sum() == 0:
        # Ten reports means neutral in the 0..20 coverage proxy used downstream.
        reviewed["report_count_12m"] = 10.0
        universe["report_count_12m"] = 10.0
        reviewed["analyst_coverage_status"] = "unavailable_neutralized"
    else:
        reviewed["analyst_coverage_status"] = "observed"
    return _original_refine(reviewed, universe)


base.refine = refine_with_market_cycle

if __name__ == "__main__":
    raise SystemExit(base.main())
