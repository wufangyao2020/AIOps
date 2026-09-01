#!/usr/bin/env python3
"""Hardened runner for research_stage1.

Material controls before candidate generation:
- filter all statement rows by A-share market/security identifiers so a same
  six-digit NEEQ code cannot overwrite an A-share row;
- use Eastmoney's actual `CIP` balance-sheet field for construction in progress;
- never reward negative/zero PE or PB as 'cheap';
- treat lower leverage as higher quality;
- explicitly recognize genuine loss-to-profit operating turnarounds.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import research_stage1 as base

ALLOWED_MARKETS = {
    "069001001001", "069001001003", "069001001006",
    "069001002001", "069001002002", "069001002005", "069001017",
}
ALLOWED_TYPES = {"058001001", "058001008"}
ALLOWED_SUFFIXES = (".SH", ".SZ", ".BJ")


def source_is_a_share(row: dict) -> bool:
    market = str(row.get("TRADE_MARKET_CODE") or "").strip()
    sec_type = str(row.get("SECURITY_TYPE_CODE") or "").strip()
    secucode = str(row.get("SECUCODE") or "").strip().upper()
    if market:
        return market in ALLOWED_MARKETS and (not sec_type or sec_type in ALLOWED_TYPES)
    return secucode.endswith(ALLOWED_SUFFIXES)


def latest_by_code_filtered(rows):
    out = {}
    for row in rows:
        if not source_is_a_share(row):
            continue
        code = str(row.get("SECURITY_CODE") or "").strip()
        if not (len(code) == 6 and code.isdigit()):
            continue
        stamp = str(row.get("UPDATE_DATE") or row.get("NOTICE_DATE") or "")
        old = out.get(code)
        if old is None or stamp > str(old.get("UPDATE_DATE") or old.get("NOTICE_DATE") or ""):
            out[code] = row
    return out


_original_build_period_frame = base.build_period_frame


def build_period_frame_fixed(rows, prefix: str, kind: str) -> pd.DataFrame:
    if kind != "balance":
        return _original_build_period_frame(rows, prefix, kind)
    selected = latest_by_code_filtered(rows)
    mapping = {
        "total_assets": "TOTAL_ASSETS",
        "total_equity": "TOTAL_EQUITY",
        "total_liabilities": "TOTAL_LIABILITIES",
        "inventory": "INVENTORY",
        "accounts_receivable": "ACCOUNTS_RECE",
        "monetary_funds": "MONETARYFUNDS",
        "fixed_assets": "FIXED_ASSET",
        "goodwill": "GOODWILL",
        "current_assets": "TOTAL_CURRENT_ASSETS",
        "current_liabilities": "TOTAL_CURRENT_LIAB",
        "short_loan": "SHORT_LOAN",
        "long_loan": "LONG_LOAN",
        "contract_liabilities": "CONTRACT_LIAB",
        "construction_in_progress": "CIP",
    }
    records = []
    for code, row in selected.items():
        record = {"security_code": code}
        for dst, src in mapping.items():
            record[f"{prefix}_{dst}"] = base.to_num(row.get(src))
        records.append(record)
    columns = ["security_code"] + [f"{prefix}_{key}" for key in mapping]
    return pd.DataFrame(records, columns=columns)


_original_score_models = base.score_models


def score_models_fixed(df: pd.DataFrame) -> pd.DataFrame:
    # Loss-making companies and negative-equity observations are not 'cheap'.
    df["pe_dynamic"] = pd.to_numeric(df.get("pe_dynamic"), errors="coerce").where(lambda x: x > 0)
    df["pb"] = pd.to_numeric(df.get("pb"), errors="coerce").where(lambda x: x > 0)
    out = _original_score_models(df)

    q2_turnaround = (
        (pd.to_numeric(out["2025q2_net_profit"], errors="coerce") < -5e6)
        & (pd.to_numeric(out["2026q2_net_profit"], errors="coerce") > 5e6)
    ).astype(float)
    h1_turnaround = (
        (pd.to_numeric(out["2025h1_net_profit"], errors="coerce") < -1e7)
        & (pd.to_numeric(out["2026h1_net_profit"], errors="coerce") > 1e7)
    ).astype(float)
    q2_deduct_turnaround = (
        (pd.to_numeric(out["2025q2_deduct_net_profit"], errors="coerce") < -5e6)
        & (pd.to_numeric(out["2026q2_deduct_net_profit"], errors="coerce") > 5e6)
    ).astype(float)
    out["q2_turnaround_flag"] = q2_turnaround
    out["h1_turnaround_flag"] = h1_turnaround
    out["q2_deduct_turnaround_flag"] = q2_deduct_turnaround
    out["fundamental_acceleration_score"] = (
        out["fundamental_acceleration_score"] + 10 * q2_turnaround + 7 * q2_deduct_turnaround + 5 * h1_turnaround
    ).clip(0, 100)
    out["new_profit_pool_score_stage1"] = (
        out["new_profit_pool_score_stage1"] + 5 * q2_turnaround + 4 * q2_deduct_turnaround
    ).clip(0, 100)
    cyclical_flag = out["industry"].fillna("").astype(str).apply(
        lambda x: any(k in x for k in base.CYCLICAL_KEYWORDS)
    ).astype(float)
    out["cyclical_turn_score"] = (
        out["cyclical_turn_score"] + 9 * q2_turnaround * cyclical_flag + 5 * h1_turnaround * cyclical_flag
    ).clip(0, 100)

    model_cols = [
        "fundamental_acceleration_score", "new_profit_pool_score_stage1",
        "cyclical_turn_score", "capital_event_score",
    ]
    labels = ["fundamental_acceleration", "new_profit_pool", "cyclical_turn", "capital_event"]
    values = out[model_cols].to_numpy(dtype=float)
    order = np.argsort(values, axis=1)
    out["primary_model_score"] = np.take_along_axis(values, order[:, -1:], axis=1).ravel()
    out["secondary_model_score"] = np.take_along_axis(values, order[:, -2:-1], axis=1).ravel()
    out["primary_model"] = [labels[i] for i in order[:, -1]]

    def peer(col: str, high: bool = True) -> pd.Series:
        key = f"peer_pct_{col}"
        if key in out:
            return pd.to_numeric(out[key], errors="coerce").fillna(0.35)
        return base.group_percentile(out, col, high)

    positive_profit = (pd.to_numeric(out["2026h1_net_profit"], errors="coerce") > 1e7).astype(float)
    positive_deduct = (pd.to_numeric(out["2026h1_deduct_net_profit"], errors="coerce") > 5e6).astype(float)
    ar_discipline = (
        pd.to_numeric(out["accounts_receivable_yoy_pct"], errors="coerce")
        <= pd.to_numeric(out["2026h1_revenue_yoy"], errors="coerce") + 10
    ).fillna(False).astype(float)
    inv_discipline = (
        pd.to_numeric(out["inventory_yoy_pct"], errors="coerce")
        <= pd.to_numeric(out["2026h1_revenue_yoy"], errors="coerce") + 15
    ).fillna(False).astype(float)

    size = base.size_score(out["market_cap_cny"])
    valuation = 0.65 * peer("pe_dynamic", False) + 0.35 * peer("pb", False)
    coverage = pd.to_numeric(out["report_count_12m"], errors="coerce").fillna(0)
    nonconsensus = (1 - coverage.clip(0, 20) / 20) * 0.7
    nonconsensus += np.where(pd.to_numeric(out["return_ytd_pct"], errors="coerce").between(-20, 45), 0.3, 0)

    r60 = pd.to_numeric(out["return_60d_pct"], errors="coerce")
    price_confirmation = pd.Series(0.35, index=out.index, dtype=float)
    price_confirmation.loc[r60.between(3, 45)] = 1.0
    price_confirmation.loc[r60.between(-8, 3, inclusive="left")] = 0.65
    price_confirmation.loc[r60 > 90] = 0.15

    # peer_pct_debt_ratio is already inverted: high score means lower leverage.
    quality_anchor = (
        0.35 * peer("cash_conversion")
        + 0.25 * peer("2026h1_roe")
        + 0.20 * peer("debt_ratio_pct_calc", False)
        + 0.10 * ar_discipline
        + 0.10 * inv_discipline
    )
    risk_penalty = (
        np.where(out["is_st"] == 1, 35, 0)
        + np.where(pd.to_numeric(out["nonrecurring_ratio"], errors="coerce") > 0.6, 8, 0)
        + np.where(pd.to_numeric(out["goodwill_equity_ratio"], errors="coerce") > 0.6, 8, 0)
        + np.where(pd.to_numeric(out["return_ytd_pct"], errors="coerce") > 180, 12, 0)
        + np.where(pd.to_numeric(out["pe_dynamic"], errors="coerce") > 150, 8, 0)
        + np.where((positive_profit + positive_deduct) == 0, 8, 0)
    )
    out["convexity_score_stage1"] = (
        0.53 * out["primary_model_score"]
        + 0.14 * out["secondary_model_score"]
        + 10 * size + 7 * valuation + 7 * nonconsensus
        + 5 * price_confirmation + 8 * quality_anchor - risk_penalty
    ).clip(0, 100)
    out["valuation_score"] = (100 * valuation).clip(0, 100)
    return out


base.latest_by_code = latest_by_code_filtered
base.build_period_frame = build_period_frame_fixed
base.score_models = score_models_fixed

if __name__ == "__main__":
    raise SystemExit(base.main())
