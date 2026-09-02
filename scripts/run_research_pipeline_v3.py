#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quality-hardened launcher for the 5,550-company full research pipeline.

Adds three material controls on top of V2:
- negative/zero PE and PB never receive a cheapness reward;
- announcement scan reads up to 120 recent notices per company when necessary;
- the doubling stress test uses a conservative median earnings run-rate rather
  than the most optimistic annualisation.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import math
import os
import statistics
from typing import Any

import numpy as np
import pandas as pd

import run_research_pipeline as p
import run_research_pipeline_v2 as v2  # applies statement cache, quote and PDF hardening


def sanitized_quotes() -> pd.DataFrame:
    frame = v2.resilient_fetch_quotes().copy()
    for column in ("pe_dynamic", "pb"):
        values = pd.to_numeric(frame[column], errors="coerce")
        frame[column] = values.where(values > 0)
    return frame


def deep_fetch_announcements(code: str, page_size: int = 60):
    all_items: list[dict[str, Any]] = []
    try:
        for page_index in (1, 2):
            payload = p.request_json(p.ANN_URL, {
                "sr": -1,
                "page_size": page_size,
                "page_index": page_index,
                "ann_type": "A",
                "client_source": "web",
                "stock_list": code,
            }, timeout=18, retries=3)
            data = payload.get("data") or {}
            items = data.get("list") or []
            if not items:
                break
            stop = False
            for it in items:
                d = p.text(it.get("notice_date") or it.get("display_time") or it.get("eiTime"))[:10]
                try:
                    parsed = p.datetime.fromisoformat(d).date()
                except Exception:
                    parsed = None
                if parsed and parsed < p.EVENT_START_DATE:
                    stop = True
                    continue
                attach = ""
                for key in ("attach_url", "pdf_url", "adjunctUrl", "url"):
                    if p.text(it.get(key)):
                        attach = p.text(it.get(key))
                        break
                all_items.append({
                    "code": code,
                    "title": p.text(it.get("title")),
                    "date": d,
                    "art_code": p.text(it.get("art_code")),
                    "columns": [p.text(x.get("column_name")) for x in (it.get("columns") or [])],
                    "attach_url": attach,
                })
            total_hits = int(data.get("total_hits") or len(items))
            if stop or total_hits <= page_index * page_size or len(items) < page_size:
                break
        dedup: dict[str, dict[str, Any]] = {}
        for item in all_items:
            key = item.get("art_code") or f"{item.get('date')}|{item.get('title')}"
            dedup[str(key)] = item
        return code, list(dedup.values()), ""
    except Exception as exc:  # noqa: BLE001
        return code, all_items, str(exc)


def deep_scan_all_announcements(codes: list[str]):
    cache = p.CACHE_DIR / "announcements_latest_compact_v3.jsonl"
    error_file = p.CACHE_DIR / "announcement_errors_compact_v3.json"
    use_cache = os.getenv("REFRESH_ANNOUNCEMENTS", "0") != "1" and cache.exists()
    if use_cache:
        result: dict[str, list[dict[str, Any]]] = {}
        with cache.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    obj = json.loads(line)
                    result[obj["code"]] = obj.get("announcements", [])
        if len(result) >= int(len(codes) * 0.98):
            errors = json.loads(error_file.read_text("utf-8")) if error_file.exists() else {}
            return result, errors

    result: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    workers = int(os.getenv("ANNOUNCEMENT_WORKERS", "24"))
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(deep_fetch_announcements, code): code for code in codes}
        for done, future in enumerate(cf.as_completed(futures), 1):
            code, items, error = future.result()
            result[code] = items
            if error:
                errors[code] = error
            if done % 500 == 0:
                print(f"deep announcement scan: {done}/{len(codes)}, errors={len(errors)}", flush=True)
    p.write_jsonl(cache, ({"code": code, "announcements": result.get(code, [])} for code in codes))
    p.write_json(error_file, errors)
    return result, errors


def conservative_double_math(row: pd.Series, industry_median_pe: float) -> dict[str, Any]:
    mcap = p.num(row.get("market_cap"))
    fy25 = p.num(row.get("net_profit_fy_2025"))
    h125 = p.num(row.get("net_profit_h1_2025"))
    h126 = p.num(row.get("net_profit_h1_2026"))
    q2 = p.num(row.get("net_profit_q2_2026"))
    ttm = fy25 - h125 + h126 if all(p.finite(x) for x in (fy25, h125, h126)) else float("nan")
    annual_h1 = h126 * 2 if p.finite(h126) else float("nan")
    annual_q2 = q2 * 4 if p.finite(q2) else float("nan")
    anchors = [x for x in (ttm, annual_h1) if p.finite(x) and x > 0]
    if p.finite(annual_q2) and annual_q2 > 0:
        if anchors:
            annual_q2 = min(annual_q2, 1.5 * max(anchors))
        anchors.append(annual_q2)
    run_rate = statistics.median(anchors) if anchors else float("nan")

    primary = row.get("primary_model", "")
    if primary == "周期拐点":
        target_pe = p.clip(industry_median_pe if p.finite(industry_median_pe) else 15, 8, 20)
    elif primary == "资本事件":
        target_pe = p.clip(industry_median_pe if p.finite(industry_median_pe) else 25, 12, 35)
    else:
        target_pe = p.clip(industry_median_pe if p.finite(industry_median_pe) else 28, 15, 40)
    required_profit = 2 * mcap / target_pe if p.finite(mcap) and target_pe else float("nan")
    required_growth = (required_profit / run_rate - 1) * 100 if p.finite(required_profit) and p.finite(run_rate) and run_rate > 0 else float("nan")
    if not p.finite(required_growth):
        feasibility = 10.0
    elif required_growth <= 0:
        feasibility = 100.0
    else:
        feasibility = max(0.0, 100.0 - min(100.0, required_growth / 1.25))
    return {
        "ttm_profit_est": ttm,
        "profit_run_rate_est": run_rate,
        "double_target_pe": target_pe,
        "double_required_profit": required_profit,
        "double_required_profit_growth_pct": required_growth,
        "double_math_score": feasibility,
    }


p.fetch_quotes = sanitized_quotes
p.fetch_announcements = deep_fetch_announcements
p.scan_all_announcements = deep_scan_all_announcements
p.calculate_double_math = conservative_double_math

if __name__ == "__main__":
    raise SystemExit(p.main())
