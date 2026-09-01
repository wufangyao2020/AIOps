#!/usr/bin/env python3
"""Final Stage-1 data-field correction.

Eastmoney's full income statement exposes consolidated operating cost as
`TOTAL_OPERATE_COST`.  Earlier code used `OPERATE_COST`, which can leave the
margin calculation empty.  This wrapper applies all v3 resilience controls and
uses the correct field with a backwards-compatible fallback.
"""
from __future__ import annotations

import pandas as pd

import research_stage1 as base
import research_stage1_v3 as v3  # noqa: F401  # applies all prior hardening/caching/fallbacks

_prior_build_period_frame = base.build_period_frame


def build_period_frame_final(rows, prefix: str, kind: str) -> pd.DataFrame:
    if kind != "income":
        return _prior_build_period_frame(rows, prefix, kind)
    selected = base.latest_by_code(rows)
    mapping = {
        "operating_profit": ("OPERATE_PROFIT",),
        "total_profit": ("TOTAL_PROFIT",),
        "deduct_net_profit": ("DEDUCT_PARENT_NETPROFIT",),
        "sale_expense": ("SALE_EXPENSE",),
        "manage_expense": ("MANAGE_EXPENSE",),
        "finance_expense": ("FINANCE_EXPENSE",),
        "income_tax": ("INCOME_TAX",),
    }
    records = []
    for code, row in selected.items():
        record = {"security_code": code}
        operating_cost = row.get("TOTAL_OPERATE_COST")
        if operating_cost is None:
            operating_cost = row.get("OPERATE_COST")
        record[f"{prefix}_operating_cost"] = base.to_num(operating_cost)
        for dst, source_fields in mapping.items():
            value = None
            for src in source_fields:
                if row.get(src) is not None:
                    value = row.get(src)
                    break
            record[f"{prefix}_{dst}"] = base.to_num(value)
        records.append(record)
    columns = [
        "security_code", f"{prefix}_operating_cost",
        *[f"{prefix}_{key}" for key in mapping],
    ]
    return pd.DataFrame(records, columns=columns)


base.build_period_frame = build_period_frame_final

if __name__ == "__main__":
    raise SystemExit(base.main())
