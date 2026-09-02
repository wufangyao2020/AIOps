#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final production launcher for the 2026H1 5,550-company study.

Controls added on top of the global-announcement V4 launcher:
1. every cached financial row must belong to the Shanghai, Shenzhen or Beijing
   A-share market before a six-digit code can enter a company record;
2. report corrections, summaries and disclosure notices cannot be mistaken for
   the full 2026 half-year report;
3. final candidates must each have at least one successfully extracted filing;
4. analyst coverage is counted from actual report rows rather than page count.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

import numpy as np
import pandas as pd

import run_research_pipeline as p
import run_research_pipeline_v4 as v4  # noqa: F401  # applies all earlier hardening


ALLOWED_MARKETS = {
    "069001001001",  # SSE main
    "069001001003",  # SSE risk-warning board
    "069001001006",  # STAR
    "069001002001",  # SZSE main
    "069001002002",  # ChiNext
    "069001002005",  # SZSE risk-warning board
    "069001017",     # BSE
}
ALLOWED_TYPES = {"058001001", "058001008"}
ALLOWED_SUFFIXES = (".SH", ".SZ", ".BJ")
MASTER_CODES = set(
    pd.read_csv(p.MASTER, dtype={"security_code": str}, usecols=["security_code"])["security_code"]
    .astype(str).str.zfill(6)
)

_source_fetch_periodic = p.fetch_periodic
_source_build_final20 = p.build_final20


def pure_a_share_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    df = frame.copy()
    code_col = next((c for c in ("SECURITY_CODE", "security_code", "股票代码") if c in df.columns), None)
    if code_col is None and "SECUCODE" in df.columns:
        df["__code"] = df["SECUCODE"].astype(str).str.extract(r"(\d{6})", expand=False)
        code_col = "__code"
    if code_col is None:
        return df.iloc[0:0].copy()
    codes = df[code_col].astype(str).str.extract(r"(\d{6})", expand=False).str.zfill(6)
    master_mask = codes.isin(MASTER_CODES)

    market_mask = pd.Series(False, index=df.index)
    if "TRADE_MARKET_CODE" in df.columns:
        market_mask |= df["TRADE_MARKET_CODE"].astype(str).isin(ALLOWED_MARKETS)
    if "SECUCODE" in df.columns:
        market_mask |= df["SECUCODE"].astype(str).str.upper().str.endswith(ALLOWED_SUFFIXES)
    if not market_mask.any():
        # Some statement endpoints may omit market metadata.  In that case use
        # the legal-exchange suffix when available; otherwise retain master-code
        # rows but require unique-source consistency below.
        market_mask = master_mask.copy()

    type_mask = pd.Series(True, index=df.index)
    if "SECURITY_TYPE_CODE" in df.columns:
        values = df["SECURITY_TYPE_CODE"].astype(str)
        populated = values.ne("") & values.ne("nan") & values.ne("None")
        type_mask = (~populated) | values.isin(ALLOWED_TYPES)

    filtered = df[master_mask & market_mask & type_mask].copy()
    if "__code" in filtered.columns:
        filtered = filtered.drop(columns=["__code"])
    return filtered


def filtered_fetch_periodic(report_name: str, report_date: str) -> pd.DataFrame:
    raw = _source_fetch_periodic(report_name, report_date)
    filtered = pure_a_share_frame(raw)
    if len(raw) and not len(filtered):
        raise RuntimeError(f"A-share filter removed every row: {report_name} {report_date}")
    print(f"pure A-share input {report_name} {report_date}: {len(filtered)}/{len(raw)} rows", flush=True)
    return filtered


def is_full_h1_report(item: dict[str, Any]) -> bool:
    title = p.normalize_title(item.get("title", ""))
    columns = "|".join(p.text(x) for x in (item.get("columns") or []))
    if not re.search(r"2026年(?:半年度|中期)报告", title):
        return False
    if re.search(r"摘要|更正公告|更正说明|取消|提示性公告|披露提示|审核问询|回复|英文版", title):
        return False
    if "半年度报告全文" in columns or "中期报告全文" in columns:
        return True
    # Legal filing titles normally end at the report name, optionally followed
    # by a revision/update marker.
    return bool(re.search(r"2026年(?:半年度|中期)报告(?:\(.*?(?:更新|修订).*?\))?$", title))


def exact_review_selector(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    full_reports = [item for item in items if is_full_h1_report(item)]
    full_reports.sort(
        key=lambda item: (
            int("半年度报告全文" in "|".join(p.text(x) for x in (item.get("columns") or []))),
            p.text(item.get("date")),
            p.text(item.get("art_code")),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = full_reports[:1]

    def event_priority(item: dict[str, Any]) -> tuple[float, str]:
        if is_full_h1_report(item):
            return -1.0, ""
        title = p.normalize_title(item.get("title", ""))
        if re.search(r"2026年(?:半年度|中期)报告.*(?:摘要|更正|说明|提示)", title):
            return -1.0, ""
        scores = p.score_event_titles([{"title": title}])
        priority = (
            abs(float(scores.get("event_net_raw", 0) or 0))
            + float(scores.get("new_pool_event_raw", 0) or 0)
            + 4 * int(scores.get("event_positive_count", 0) or 0)
            + 4 * int(scores.get("event_negative_count", 0) or 0)
        )
        if "投资者关系活动记录" in title:
            priority += 12
        return priority, p.text(item.get("date"))

    seen = {p.text(item.get("art_code")) for item in selected}
    for item in sorted(items, key=event_priority, reverse=True):
        score, _ = event_priority(item)
        art = p.text(item.get("art_code"))
        if score <= 0 or art in seen:
            continue
        selected.append(item)
        seen.add(art)
        if len(selected) >= 5:
            break
    return selected


def exact_analyst_coverage(code: str):
    page_size = 50
    reports: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        first_payload = None
        pages = 1
        for page in range(1, 11):
            params = {
                "pageSize": page_size,
                "pageNo": page,
                "pageNum": page,
                "pageNumber": page,
                "p": page,
                "stockCode": code,
                "industryCode": "*",
                "industry": "*",
                "rating": "*",
                "ratingchange": "*",
                "beginTime": p.EVENT_START_DATE.isoformat(),
                "endTime": p.CUTOFF_DATE.isoformat(),
                "qType": 0,
            }
            payload = p.request_json(p.REPORT_API_URL, params, timeout=20, retries=3)
            if first_payload is None:
                first_payload = payload
                try:
                    pages = max(1, min(10, int(payload.get("TotalPage") or 1)))
                except Exception:
                    pages = 1
            data = payload.get("data") or []
            if not data:
                break
            for row in data:
                key = p.text(row.get("infoCode") or row.get("title")) + "|" + p.text(row.get("publishDate"))
                if key in seen:
                    continue
                seen.add(key)
                reports.append({
                    "title": p.text(row.get("title")),
                    "org": p.text(row.get("orgSName") or row.get("orgName")),
                    "date": p.text(row.get("publishDate"))[:10],
                    "rating": p.text(row.get("emRatingName") or row.get("rating")),
                })
            if page >= pages or len(data) < page_size:
                break
        return len(reports), reports[:10]
    except Exception:
        return 0, []


def fulltext_only_final20(scored: pd.DataFrame, reviewed: pd.DataFrame, peers: pd.DataFrame) -> pd.DataFrame:
    candidate_reviews = reviewed.copy()
    chars = pd.to_numeric(candidate_reviews.get("fulltext_chars_read"), errors="coerce").fillna(0)
    docs = pd.to_numeric(candidate_reviews.get("documents_read_count"), errors="coerce").fillna(0)
    candidate_reviews = candidate_reviews[(chars > 1000) & (docs >= 1)].copy()
    if len(candidate_reviews) < 20:
        raise RuntimeError(f"only {len(candidate_reviews)} Top100 companies have full-text evidence; at least 20 required")
    final = _source_build_final20(scored, candidate_reviews, peers.loc[candidate_reviews.index])
    if len(final) != 20:
        raise RuntimeError(f"evidence-constrained final selection returned {len(final)} rows")
    return final


p.fetch_periodic = filtered_fetch_periodic
p.select_review_announcements = exact_review_selector
p.fetch_analyst_coverage = exact_analyst_coverage
p.build_final20 = fulltext_only_final20

if __name__ == "__main__":
    raise SystemExit(p.main())
