#!/usr/bin/env python3
"""Hardened Stage-2 runner.

Controls added before evidence extraction:
- parse Eastmoney's announcement-content JSONP instead of assuming plain JSON;
- merge every declared content page (bounded for safety);
- only mark the H1 report as read when substantive text was actually obtained;
- ensure negative/zero PE observations cannot depress peer valuation anchors.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd

import research_stage2 as base


def parse_json_or_jsonp(raw: str) -> dict:
    value = (raw or "").strip()
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        first = value.find("(")
        last = value.rfind(")")
        if first >= 0 and last > first:
            parsed = json.loads(value[first + 1:last])
            return parsed if isinstance(parsed, dict) else {}
        raise


def fetch_content_page(art_code: str, page_index: int) -> dict:
    params = {
        "art_code": art_code,
        "client_source": "web",
        "page_index": str(page_index),
        "cb": "callback",
    }
    last_error = None
    for attempt in range(1, 6):
        try:
            response = base.session().get(
                base.NOTICE_CONTENT,
                params=params,
                headers={**base.HEADERS, "Referer": "https://data.eastmoney.com/notices/"},
                timeout=45,
            )
            response.raise_for_status()
            payload = parse_json_or_jsonp(response.text)
            data = payload.get("data") or payload.get("result") or {}
            if not isinstance(data, dict):
                raise RuntimeError("announcement content payload lacks a data object")
            return data
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 5:
                time.sleep(min(1.6 ** attempt, 8))
    raise RuntimeError(f"announcement content fetch failed for {art_code} page {page_index}: {last_error}")


def paginated_announcement_content(art_code: str):
    first = fetch_content_page(art_code, 1)
    declared_pages = first.get("page_size") or first.get("total_page") or first.get("pages") or 1
    try:
        page_count = max(1, min(int(float(declared_pages)), 200))
    except (TypeError, ValueError):
        page_count = 1
    pages = [first]
    for page in range(2, page_count + 1):
        data = fetch_content_page(art_code, page)
        if not data:
            break
        pages.append(data)
    raw_parts = []
    for data in pages:
        content = data.get("notice_content") or data.get("content") or ""
        if content:
            raw_parts.append(str(content))
    merged = base.clean_text("\n".join(raw_parts))[:600_000]
    metadata = dict(first)
    metadata["content_pages_fetched"] = len(pages)
    metadata["content_pages_declared"] = page_count
    metadata["attach_url_web"] = first.get("attach_url_web")
    return merged, metadata


_original_review_one = base.review_one


def review_one_strict(code: str, name: str):
    result = _original_review_one(code, name)
    try:
        manifest = json.loads(result.get("source_manifest_json") or "[]")
    except Exception:
        manifest = []
    h1_chars = max(
        [int(item.get("character_count") or 0) for item in manifest if item.get("label") == "h1_2026" and not item.get("error")]
        or [0]
    )
    result["h1_report_character_count"] = h1_chars
    result["h1_report_found"] = int(h1_chars >= 1000)
    substantive = [item for item in manifest if int(item.get("character_count") or 0) >= 200]
    result["substantive_documents_read"] = len(substantive)
    if result.get("review_status") == "completed" and h1_chars < 1000:
        result["review_warning"] = "H1 report metadata was found but substantive full text was not retrieved"
    return result


_original_refine = base.refine


def refine_sanitized(reviewed: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    clean_universe = universe.copy()
    clean_universe["pe_dynamic"] = pd.to_numeric(clean_universe.get("pe_dynamic"), errors="coerce").where(lambda x: x > 0)
    clean_universe["pb"] = pd.to_numeric(clean_universe.get("pb"), errors="coerce").where(lambda x: x > 0)
    reviewed = reviewed.copy()
    reviewed["pe_dynamic"] = pd.to_numeric(reviewed.get("pe_dynamic"), errors="coerce").where(lambda x: x > 0)
    reviewed["pb"] = pd.to_numeric(reviewed.get("pb"), errors="coerce").where(lambda x: x > 0)
    return _original_refine(reviewed, clean_universe)


base.announcement_content = paginated_announcement_content
base.review_one = review_one_strict
base.refine = refine_sanitized

if __name__ == "__main__":
    raise SystemExit(base.main())
