#!/usr/bin/env python3
"""Production Stage-1 runner with Sina-first quote acquisition.

Eastmoney's numbered push2 hosts can close long all-market sessions.  Sina's
Market Center currently exposes the same core price, market-cap, PE, PB,
turnover and liquidity fields for the full A-share market.  We prefer that
stable list source and retain Eastmoney clist/ulist as fallbacks.
"""
from __future__ import annotations

import json

import pandas as pd

import research_stage1 as base
import research_stage1_v3 as quote_tools
import research_stage1_v5 as v5  # noqa: F401  # applies every prior correction


def fetch_quote_snapshot_sina_first() -> pd.DataFrame:
    cache = quote_tools.CACHE_DIR / "quote_snapshot.csv.gz"
    if cache.exists() and cache.stat().st_size > 1000:
        frame = pd.read_csv(cache, dtype={"security_code": str})
        if len(frame) >= 4800:
            frame["security_code"] = frame["security_code"].astype(str).str.zfill(6)
            return frame
    errors = []
    for method in (quote_tools.sina_quotes, quote_tools.eastmoney_clist_quotes, quote_tools.eastmoney_ulist_quotes):
        try:
            frame = method()
            frame.to_csv(cache, index=False, compression="gzip", encoding="utf-8")
            (quote_tools.CACHE_DIR / "quote_source.json").write_text(
                json.dumps({"method": method.__name__, "rows": len(frame), "as_of": "2026-09-02"}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return frame
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{method.__name__}: {exc}")
    raise RuntimeError("all full-market quote sources failed: " + " || ".join(errors))


base.fetch_quote_snapshot = fetch_quote_snapshot_sina_first

if __name__ == "__main__":
    raise SystemExit(base.main())
