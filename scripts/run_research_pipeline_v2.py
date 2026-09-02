#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hardened launcher for the full 5,550-company research pipeline.

This wrapper fixes the two operational weaknesses observed in the first V2 run:
- reuse the audited multi-period statement caches already archived in the repo;
- replace the single unstable numbered quote host with a Sina-first, multi-host
  quote adapter and retain explicit coverage checks.
"""
from __future__ import annotations

import concurrent.futures as cf
import gzip
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import run_research_pipeline as p
import research_stage1_v3 as quote_tools


STAGE1_INPUT_CACHE = p.ROOT / "data" / "2026H1" / "research" / "raw" / "stage1_inputs"
_original_fetch_periodic = p.fetch_periodic


def cached_fetch_periodic(report_name: str, report_date: str) -> pd.DataFrame:
    """Read the previously archived public statement table before going online."""
    for date_field in ("REPORTDATE", "REPORT_DATE"):
        path = STAGE1_INPUT_CACHE / f"{report_name}_{date_field}_{report_date}.json.gz"
        if not path.exists() or path.stat().st_size < 100:
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                rows = json.load(handle)
            if isinstance(rows, list) and rows:
                print(f"use cached {report_name} {report_date}: {len(rows)} rows", flush=True)
                return pd.DataFrame(rows)
        except Exception as exc:  # noqa: BLE001
            print(f"cached input unreadable {path.name}: {exc}", flush=True)
    frame = _original_fetch_periodic(report_name, report_date)
    if not frame.empty:
        STAGE1_INPUT_CACHE.mkdir(parents=True, exist_ok=True)
        path = STAGE1_INPUT_CACHE / f"{report_name}_REPORT_DATE_{report_date}.json.gz"
        try:
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump(frame.to_dict("records"), handle, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception as exc:  # noqa: BLE001
            print(f"could not persist periodic cache {path.name}: {exc}", flush=True)
    return frame


def resilient_fetch_quotes() -> pd.DataFrame:
    """Adapt the hardened full-market quote source to this pipeline's schema."""
    frame = quote_tools.fetch_quote_snapshot_resilient().copy()
    rename = {
        "security_code": "code",
        "daily_return_pct": "day_change_pct",
        "turnover_amount_cny": "turnover_amount",
        "turnover_rate_pct": "turnover_rate",
        "market_cap_cny": "market_cap",
        "float_market_cap_cny": "float_market_cap",
        "return_60d_pct": "change_60d_pct",
        "return_ytd_pct": "change_ytd_pct",
    }
    frame = frame.rename(columns=rename)
    required = [
        "code", "quote_name", "price", "day_change_pct", "volume",
        "turnover_amount", "turnover_rate", "pe_dynamic", "volume_ratio",
        "market_cap", "float_market_cap", "pb", "change_60d_pct",
        "change_ytd_pct", "quote_industry", "quote_roe", "total_shares",
    ]
    for column in required:
        if column not in frame:
            frame[column] = "" if column in {"code", "quote_name", "quote_industry"} else np.nan
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    numeric = [c for c in required if c not in {"code", "quote_name", "quote_industry"}]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[required].drop_duplicates("code", keep="last")
    if len(frame) < 4800:
        raise RuntimeError(f"full-market quote coverage too low after adapter: {len(frame)}")
    print(f"resilient quote coverage: {len(frame)}", flush=True)
    return frame.set_index("code")


# Always materialize statement columns, even when an optional source table is unavailable.
def safe_add_statement(base: pd.DataFrame, cur: pd.DataFrame, prev: pd.DataFrame, prefix: str) -> pd.DataFrame:
    cur_columns = list(cur.columns)
    prev_columns = list(prev.columns)
    for c in cur_columns:
        name = f"{prefix}_{c}_cur"
        if not cur.empty:
            base = base.join(cur[[c]].rename(columns={c: name}), how="left")
        elif name not in base:
            base[name] = np.nan
    for c in prev_columns:
        name = f"{prefix}_{c}_prev"
        if not prev.empty:
            base = base.join(prev[[c]].rename(columns={c: name}), how="left")
        elif name not in base:
            base[name] = np.nan
    return base


# Cache only fields required by scoring/review; do not archive the API's repeated raw payload.
def compact_fetch_announcements(code: str, page_size: int = 24):
    try:
        payload = p.request_json(p.ANN_URL, {
            "sr": -1,
            "page_size": page_size,
            "page_index": 1,
            "ann_type": "A",
            "client_source": "web",
            "stock_list": code,
        }, timeout=15, retries=3)
        data = payload.get("data") or {}
        items = data.get("list") or []
        clean = []
        for it in items:
            d = p.text(it.get("notice_date") or it.get("display_time") or it.get("eiTime"))[:10]
            try:
                parsed = p.datetime.fromisoformat(d).date()
            except Exception:
                parsed = None
            if parsed and parsed < p.EVENT_START_DATE:
                continue
            attach = ""
            for key in ["attach_url", "pdf_url", "adjunctUrl", "url"]:
                if p.text(it.get(key)):
                    attach = p.text(it.get(key))
                    break
            clean.append({
                "code": code,
                "title": p.text(it.get("title")),
                "date": d,
                "art_code": p.text(it.get("art_code")),
                "columns": [p.text(x.get("column_name")) for x in (it.get("columns") or [])],
                "attach_url": attach,
            })
        return code, clean, ""
    except Exception as exc:
        return code, [], str(exc)


def compact_scan_all_announcements(codes: list[str]):
    cache = p.CACHE_DIR / "announcements_latest_compact.jsonl"
    error_file = p.CACHE_DIR / "announcement_errors_compact.json"
    use_cache = os.getenv("REFRESH_ANNOUNCEMENTS", "0") != "1" and cache.exists()
    if use_cache:
        result = {}
        with cache.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    obj = json.loads(line)
                    result[obj["code"]] = obj.get("announcements", [])
        if len(result) >= int(len(codes) * 0.95):
            errors = json.loads(error_file.read_text("utf-8")) if error_file.exists() else {}
            return result, errors

    result: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    workers = int(os.getenv("ANNOUNCEMENT_WORKERS", "32"))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(compact_fetch_announcements, c): c for c in codes}
        done = 0
        for fut in cf.as_completed(futures):
            code, items, err = fut.result()
            result[code] = items
            if err:
                errors[code] = err
            done += 1
            if done % 500 == 0:
                print(f"compact announcement scan: {done}/{len(codes)}, errors={len(errors)}", flush=True)
    p.write_jsonl(cache, ({"code": c, "announcements": result.get(c, [])} for c in codes))
    p.write_json(error_file, errors)
    return result, errors


def compact_pdf_candidates(code: str, item: dict[str, Any]):
    art = p.text(item.get("art_code"))
    candidates = []
    u = p.text(item.get("attach_url"))
    if u:
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = "https://pdf.dfcfw.com" + u
        candidates.append(u)
    if art:
        for prefix in ["H2", "H3", "H1"]:
            candidates.append(f"https://pdf.dfcfw.com/pdf/{prefix}_{art}_1.pdf")
    try:
        html = p.requests.get(p.announcement_detail_url(code, art), headers=p.HEADERS, timeout=12).text
        html = html.replace("\\/", "/")
        for match in p.re.finditer(r"https?://pdf\.dfcfw\.com/pdf/[^\"'<> ]+?\.pdf(?:\?[^\"'<> ]*)?", html):
            candidates.insert(0, match.group(0))
    except Exception:
        pass
    seen = set()
    output = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            output.append(candidate)
    return output


p.fetch_periodic = cached_fetch_periodic
p.fetch_quotes = resilient_fetch_quotes
p.add_statement = safe_add_statement
p.fetch_announcements = compact_fetch_announcements
p.scan_all_announcements = compact_scan_all_announcements
p.pdf_candidates = compact_pdf_candidates

if __name__ == "__main__":
    raise SystemExit(p.main())
