#!/usr/bin/env python3
"""Build a compact, human-readable evidence book for the reviewed 100 companies."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "data" / "2026H1" / "research"
INPUT = RESEARCH / "top100_reviewed_peer_compared.csv"
OUTPUT = RESEARCH / "top100_review_book.md"


def text(value, limit=560):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "无有效摘录"
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s[:limit] if s else "无有效摘录"


def num(value, digits=1):
    try:
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return "NA"
        return f"{x:.{digits}f}"
    except Exception:
        return "NA"


def manifest_summary(value):
    try:
        items = json.loads(str(value or "[]"))
    except Exception:
        return "来源清单无法解析"
    good = [x for x in items if not x.get("error")]
    labels = {}
    for item in good:
        labels[item.get("label", "unknown")] = labels.get(item.get("label", "unknown"), 0) + 1
    return "、".join(f"{k}:{v}" for k, v in labels.items()) or "无成功读取来源"


def main():
    df = pd.read_csv(INPUT, dtype={"security_code": str}, low_memory=False)
    df["security_code"] = df["security_code"].astype(str).str.zfill(6)
    df = df.sort_values("final_convexity_score", ascending=False).reset_index(drop=True)
    lines = [
        "# 2026H1 A股前100家公司公告证据复核册", "",
        "> 每家公司均来自5,550家四模型盲筛。本册用于人工复核自动抽取结果；得分不是买入指令。", "",
    ]
    for idx, r in df.iterrows():
        lines += [
            f"## {idx + 1}. {r.get('security_name')}（{r.get('security_code')}）｜{r.get('industry')}｜{r.get('refined_primary_model')}", "",
            f"- **分数：** 最终 {num(r.get('final_convexity_score'))}；主模型 {num(r.get('refined_primary_score'))}；同行优势 {num(r.get('peer_superiority_score'))}；风险 {num(r.get('announcement_risk_score'))}；公告证据 {num(r.get('announcement_evidence_score'))}。",
            f"- **关键财务：** Q2收入同比 {num(r.get('q2_revenue_yoy_pct'))}%；Q2归母同比 {num(r.get('q2_net_profit_yoy_pct'))}%；Q2扣非同比 {num(r.get('q2_deduct_net_profit_yoy_pct'))}%；毛利率变化 {num(r.get('gross_margin_delta_pp'))}pp；经营现金/净利润 {num(r.get('cash_conversion'), 2)}；ROE {num(r.get('2026h1_roe'))}%。",
            f"- **市场与翻倍压力：** 市值 {num(r.get('market_cap_100m'))}亿元；PE {num(r.get('pe_dynamic'))}；60日涨幅 {num(r.get('return_60d_pct'))}%；YTD {num(r.get('return_ytd_pct'))}%；所需利润增长 {num(float(r.get('required_profit_growth_for_2x')) * 100 if pd.notna(r.get('required_profit_growth_for_2x')) else None)}%；波次 {r.get('wave_stage')}。",
            f"- **新利润池等级：** Level {int(r.get('new_profit_pool_evidence_level') or 0)}。{text(r.get('new_profit_pool_level_evidence'))}",
            f"- **增长证据：** {text(r.get('growth_evidence'), 760)}",
            f"- **周期证据：** {text(r.get('cycle_evidence'), 620)}",
            f"- **资本事件证据：** {text(r.get('capital_evidence'), 620)}",
            f"- **12个月里程碑：** {text(r.get('milestone_evidence'), 620)}",
            f"- **反证与风险：** {text(r.get('risk_evidence'), 760)}",
            f"- **同行：** {text(r.get('top_peer_context'), 620)}",
            f"- **文件覆盖：** {manifest_summary(r.get('source_manifest_json'))}", "",
        ]
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} with {len(df)} company sections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
