#!/usr/bin/env python3
"""Final production wrapper: persist the capital-event announcement input.

The capital-event model must be reproducible and must not disappear merely
because a public endpoint is temporarily unstable.  Successful event scans are
stored as compressed CSV and reused by subsequent audited runs.
"""
from __future__ import annotations

import pandas as pd

import research_stage1 as base
import research_stage1_v4 as v4  # noqa: F401  # applies v2/v3/v4 hardening

_original_fetch_events = base.fetch_event_announcements


def fetch_event_announcements_cached(begin: str = "2026-01-01", end: str = "2026-09-02") -> pd.DataFrame:
    cache = base.RAW_DIR / "stage1_inputs" / f"capital_events_{begin}_{end}.csv.gz"
    if cache.exists() and cache.stat().st_size > 500:
        frame = pd.read_csv(cache, dtype={"security_code": str, "art_code": str})
        frame["security_code"] = frame["security_code"].astype(str).str.zfill(6)
        frame["notice_date"] = pd.to_datetime(frame["notice_date"], errors="coerce")
        return frame
    frame = _original_fetch_events(begin, end)
    frame.to_csv(cache, index=False, compression="gzip", encoding="utf-8")
    return frame


base.fetch_event_announcements = fetch_event_announcements_cached

if __name__ == "__main__":
    raise SystemExit(base.main())
