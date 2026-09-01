#!/usr/bin/env python3
"""Hardened Stage-2 runner.

The Eastmoney announcement-content endpoint paginates long reports.  This runner
merges every declared content page before evidence extraction and ensures that
negative/zero PE observations cannot depress an industry's re-rating benchmark.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import research_stage2 as base


def paginated_announcement_content(art_code: str):
    first_payload = base.get_json(
        base.NOTICE_CONTENT,
        {"art_code": art_code, "client_source": "web", "page_index": "1"},
    )
    first = first_payload.get("data") or {}
    declared_pages = first.get("page_size") or first.get("total_page") or first.get("pages") or 1
    try:
        page_count = max(1, min(int(declared_pages), 500))
    except (TypeError, ValueError):
        page_count = 1
    pages = [first]
    for page in range(2, page_count + 1):
        payload = base.get_json(
            base.NOTICE_CONTENT,
            {"art_code": art_code, "client_source": "web", "page_index": str(page)},
        )
        data = payload.get("data") or {}
        if not data:
            break
        pages.append(data)
    raw_parts = []
    for data in pages:
        content = data.get("notice_content") or data.get("content") or ""
        if content:
            raw_parts.append(str(content))
    merged = base.clean_text("\n".join(raw_parts))
    metadata = dict(first)
    metadata["content_pages_fetched"] = len(pages)
    metadata["content_pages_declared"] = page_count
    metadata["attach_url_web"] = first.get("attach_url_web")
    return merged, metadata


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
base.refine = refine_sanitized

if __name__ == "__main__":
    raise SystemExit(base.main())
