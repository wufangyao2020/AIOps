#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production launcher using a global, paginated announcement scan.

Why this version exists
-----------------------
The per-company announcement endpoint starts throttling after several hundred
requests.  That creates a systematic capital-event blind spot.  This launcher
instead scans Eastmoney's public announcement categories by month, maps every
notice back to the archived 5,550-code universe, keeps only research-relevant
items, and then performs full-text review only for the blind Top 100.

It inherits the hardened statement cache, resilient full-market quotes,
negative-valuation cleaning and conservative doubling mathematics from V3.
"""
from __future__ import annotations

import calendar
import concurrent.futures as cf
import hashlib
import io
import json
import math
import os
import re
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from pypdf import PdfReader

import run_research_pipeline as p
import run_research_pipeline_v3 as v3  # noqa: F401  # applies V2/V3 hardening to p


GLOBAL_CACHE = p.CACHE_DIR / "announcements_global_relevant_2025-09_to_2026-09.jsonl"
GLOBAL_META = p.CACHE_DIR / "announcements_global_scan_meta.json"
CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"

# Category 4 is largely duplicated by category 5 for investable events.  The
# retained set covers reports, financing/restructuring, risk/trading events,
# broad material events, buybacks/M&A, and holder reductions/transfers.
GLOBAL_NODES = (1, 2, 3, 5, 6, 7)
REPORT_BEGIN = date(2026, 1, 1)
CAPITAL_BEGIN = p.EVENT_START_DATE
GLOBAL_END = p.CUTOFF_DATE
PAGE_SIZE = 100

REPORT_TITLE_PATTERNS = (
    r"2025年年度报告(?!摘要)",
    r"2026年第一季度报告(?!摘要)",
    r"2026年半年度报告(?!摘要)",
    r"2026年中期报告(?!摘要)",
    r"投资者关系活动记录",
    r"业绩说明会",
)


def month_ranges(start: date, end: date):
    current = date(start.year, start.month, 1)
    while current <= end:
        last = calendar.monthrange(current.year, current.month)[1]
        segment_start = max(start, current)
        segment_end = min(end, date(current.year, current.month, last))
        yield segment_start, segment_end
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def relevant_title(title: str) -> bool:
    title = p.normalize_title(title)
    if any(re.search(pattern, title) for pattern in REPORT_TITLE_PATTERNS):
        return True
    score = p.score_event_titles([{"title": title}])
    return bool(
        abs(float(score.get("event_net_raw", 0) or 0)) > 0
        or float(score.get("new_pool_event_raw", 0) or 0) > 0
        or int(score.get("event_positive_count", 0) or 0) > 0
        or int(score.get("event_negative_count", 0) or 0) > 0
    )


def compact_global_item(item: dict[str, Any], code: str, node: int) -> dict[str, Any]:
    columns = [p.text(x.get("column_name")) for x in (item.get("columns") or [])]
    attach = ""
    for key in ("attach_url_web", "attach_url", "pdf_url", "adjunctUrl", "url"):
        value = p.text(item.get(key))
        if value:
            attach = value
            break
    return {
        "code": code,
        "title": p.text(item.get("title")),
        "date": p.text(item.get("notice_date") or item.get("display_time") or item.get("eiTime"))[:10],
        "art_code": p.text(item.get("art_code") or item.get("artCode")),
        "columns": columns,
        "attach_url": attach,
        "global_node": node,
    }


def fetch_global_segment(node: int, begin: date, end: date, valid_codes: set[str]):
    params = {
        "sr": -1,
        "page_size": PAGE_SIZE,
        "page_index": 1,
        "ann_type": "A",
        "client_source": "web",
        "f_node": node,
        "s_node": 0,
        "begin_time": begin.isoformat(),
        "end_time": end.isoformat(),
    }
    first = p.request_json(p.ANN_URL, params, timeout=30, retries=6)
    data = first.get("data") or {}
    total_hits = int(data.get("total_hits") or 0)
    pages = int(math.ceil(total_hits / PAGE_SIZE)) if total_hits else 0
    # Monthly slicing keeps every segment far below the service's 50k cap.
    if total_hits >= 50_000:
        raise RuntimeError(f"announcement segment hit service cap: node={node} {begin}..{end} hits={total_hits}")

    kept: list[dict[str, Any]] = []
    seen_art: set[tuple[str, str]] = set()

    def consume(items: list[dict[str, Any]]) -> None:
        for item in items:
            title = p.text(item.get("title"))
            if not title or not relevant_title(title):
                continue
            art = p.text(item.get("art_code") or item.get("artCode"))
            for code_info in (item.get("codes") or []):
                code = p.code6(code_info.get("stock_code") or code_info.get("security_code"))
                if code not in valid_codes:
                    continue
                key = (code, art or f"{title}|{p.text(item.get('notice_date'))[:10]}")
                if key in seen_art:
                    continue
                seen_art.add(key)
                kept.append(compact_global_item(item, code, node))

    consume(data.get("list") or [])
    for page in range(2, pages + 1):
        params["page_index"] = page
        payload = p.request_json(p.ANN_URL, params, timeout=30, retries=6)
        consume(((payload.get("data") or {}).get("list") or []))
        if page % 50 == 0:
            print(f"global notices node={node} {begin:%Y-%m} page={page}/{pages} kept={len(kept)}", flush=True)
        time.sleep(0.025)
    return kept, {
        "node": node,
        "begin": begin.isoformat(),
        "end": end.isoformat(),
        "total_hits": total_hits,
        "pages": pages,
        "kept": len(kept),
    }


def load_global_cache(valid_codes: set[str]):
    if not GLOBAL_CACHE.exists() or os.getenv("REFRESH_ANNOUNCEMENTS", "0") == "1":
        return None
    result: dict[str, list[dict[str, Any]]] = {code: [] for code in valid_codes}
    try:
        with GLOBAL_CACHE.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                obj = json.loads(line)
                code = p.code6(obj.get("code"))
                if code in result:
                    result[code].append(obj)
        coverage = sum(bool(items) for items in result.values())
        if coverage >= 5_000:
            print(f"use global announcement cache: company coverage={coverage}/{len(valid_codes)}", flush=True)
            return result, {}
    except Exception as exc:  # noqa: BLE001
        print(f"global announcement cache ignored: {exc}", flush=True)
    return None


def global_scan_all_announcements(codes: list[str]):
    valid_codes = {p.code6(code) for code in codes}
    cached = load_global_cache(valid_codes)
    if cached is not None:
        return cached

    tasks: list[tuple[int, date, date]] = []
    for node in GLOBAL_NODES:
        start = REPORT_BEGIN if node == 1 else CAPITAL_BEGIN
        tasks.extend((node, begin, end) for begin, end in month_ranges(start, GLOBAL_END))

    records: list[dict[str, Any]] = []
    segment_meta: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    workers = int(os.getenv("GLOBAL_NOTICE_WORKERS", "3"))
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(fetch_global_segment, node, begin, end, valid_codes): (node, begin, end)
            for node, begin, end in tasks
        }
        for done, future in enumerate(cf.as_completed(future_map), 1):
            node, begin, end = future_map[future]
            key = f"node{node}:{begin}:{end}"
            try:
                kept, meta = future.result()
                records.extend(kept)
                segment_meta.append(meta)
            except Exception as exc:  # noqa: BLE001
                errors[key] = str(exc)
            print(f"global announcement segments: {done}/{len(tasks)} records={len(records)} errors={len(errors)}", flush=True)

    # Deduplicate notices that appear under multiple f_node categories.
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for item in records:
        code = p.code6(item.get("code"))
        art = p.text(item.get("art_code")) or f"{item.get('date')}|{item.get('title')}"
        key = (code, art)
        previous = dedup.get(key)
        if previous is None:
            dedup[key] = item
        else:
            old_node = int(previous.get("global_node") or 99)
            new_node = int(item.get("global_node") or 99)
            if new_node < old_node:
                dedup[key] = item

    by_code: dict[str, list[dict[str, Any]]] = {code: [] for code in valid_codes}
    for item in dedup.values():
        code = p.code6(item.get("code"))
        if code in by_code:
            by_code[code].append(item)
    for code in by_code:
        by_code[code].sort(key=lambda x: (x.get("date", ""), x.get("art_code", "")), reverse=True)

    GLOBAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    p.write_jsonl(GLOBAL_CACHE, (item for code in sorted(by_code) for item in by_code[code]))
    coverage = sum(bool(items) for items in by_code.values())
    report_coverage = sum(
        any(re.search(r"2026年半年度报告(?!摘要)|2026年中期报告(?!摘要)", p.normalize_title(x.get("title", ""))) for x in items)
        for items in by_code.values()
    )
    meta = {
        "generated_at": p.utc_now(),
        "company_count": len(valid_codes),
        "company_announcement_coverage": coverage,
        "company_h1_full_report_coverage": report_coverage,
        "retained_notice_count": len(dedup),
        "segment_count": len(tasks),
        "segment_success_count": len(segment_meta),
        "segment_error_count": len(errors),
        "segments": sorted(segment_meta, key=lambda x: (x["node"], x["begin"])),
        "errors": errors,
    }
    p.write_json(GLOBAL_META, meta)
    print(json.dumps({k: v for k, v in meta.items() if k not in {"segments", "errors"}}, ensure_ascii=False, indent=2), flush=True)
    return by_code, errors


def parse_content_payload(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    first = raw.find("(")
    last = raw.rfind(")")
    if first >= 0 and last > first:
        return json.loads(raw[first + 1:last])
    return json.loads(raw)


def content_api_detail(art_code: str) -> tuple[str, str]:
    if not art_code:
        return "", ""
    params = {
        "art_code": art_code,
        "client_source": "web",
        "page_index": 1,
        "cb": "callback",
    }
    response = requests.get(CONTENT_URL, params=params, headers=p.HEADERS, timeout=30)
    response.raise_for_status()
    payload = parse_content_payload(response.text)
    first = payload.get("data") or payload.get("result") or {}
    declared = first.get("page_size") or first.get("total_page") or first.get("pages") or 1
    try:
        page_count = max(1, min(int(declared), 50))
    except Exception:
        page_count = 1
    pages = [first]
    for page_index in range(2, page_count + 1):
        params["page_index"] = page_index
        response = requests.get(CONTENT_URL, params=params, headers=p.HEADERS, timeout=30)
        response.raise_for_status()
        payload = parse_content_payload(response.text)
        page = payload.get("data") or payload.get("result") or {}
        if not page:
            break
        pages.append(page)
    inline = "\n".join(
        re.sub(r"<[^>]+>", " ", p.text(page.get("notice_content") or page.get("content")))
        for page in pages
    )
    inline = re.sub(r"\s+", " ", inline).strip()

    attachment = p.text(first.get("attach_url_web") or first.get("attach_url"))
    if not attachment:
        attach_list = first.get("attach_list") or []
        for attach in attach_list:
            candidate = p.text(attach.get("attach_url") or attach.get("url"))
            if candidate:
                attachment = candidate
                break
    if attachment.startswith("//"):
        attachment = "https:" + attachment
    elif attachment.startswith("/"):
        attachment = "https://pdf.dfcfw.com" + attachment
    return inline, attachment


def extract_pdf_url(url: str, max_bytes: int, max_pages: int) -> tuple[str, str]:
    if not url:
        return "", "empty url"
    try:
        response = requests.get(url, headers=p.HEADERS, timeout=50)
        if response.status_code != 200:
            return "", f"HTTP {response.status_code}"
        if not response.content.startswith(b"%PDF"):
            return "", "not a PDF"
        if len(response.content) > max_bytes:
            return "", f"PDF too large: {len(response.content)}"
        reader = PdfReader(io.BytesIO(response.content))
        parts: list[str] = []
        for page in reader.pages[:max_pages]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts)
        return (text[:2_000_000], "") if len(text) >= 1000 else ("", f"extracted only {len(text)} chars")
    except Exception as exc:  # noqa: BLE001
        return "", str(exc)


def robust_download_pdf_text(code: str, item: dict[str, Any], max_bytes: int = 30_000_000,
                             max_pages: int = 300):
    art_code = p.text(item.get("art_code"))
    errors: list[str] = []
    try:
        inline, attachment = content_api_detail(art_code)
        # Actual narrative text is accepted; short 'see attachment' placeholders are not.
        compact_inline = re.sub(r"\s+", "", inline)
        if len(inline) >= 1000 and not re.fullmatch(r"(?:公告)?内容?详见附件[。.]?", compact_inline):
            return inline[:2_000_000], p.announcement_detail_url(code, art_code), ""
        if attachment:
            text, error = extract_pdf_url(attachment, max_bytes, max_pages)
            if text:
                return text, attachment, ""
            errors.append(f"content-api attachment: {error}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"content api: {exc}")

    for candidate in p.pdf_candidates(code, item):
        text, error = extract_pdf_url(candidate, max_bytes, max_pages)
        if text:
            return text, candidate, ""
        if error:
            errors.append(f"{candidate}: {error}")
    return "", "", " | ".join(errors[:8]) or "document unavailable"


p.scan_all_announcements = global_scan_all_announcements
p.download_pdf_text = robust_download_pdf_text

if __name__ == "__main__":
    raise SystemExit(p.main())
