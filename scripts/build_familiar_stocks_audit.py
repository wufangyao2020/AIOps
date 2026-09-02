#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a transparency-only audit for previously discussed stocks.

This script runs strictly after the blind research pipeline.  Its config is not
imported by any scoring script.  The output shows where familiar stocks ranked,
whether they entered Top100/final20, and the mechanical reason for exclusion.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "2026H1"
CONFIG = ROOT / "config" / "familiar_stocks_audit.json"


def reason(row: pd.Series) -> str:
    if bool(row.get("final20_selected")):
        return "通过全市场盲筛、Top100全文复核、同行比较与最终高凸性约束。"
    if not bool(row.get("top100_selected")):
        rank = row.get("global_rank")
        penalty = row.get("quality_penalty")
        parts = [f"全市场综合排名{int(rank)}，未进入盲筛Top100" if pd.notna(rank) else "未取得有效全市场排名"]
        if pd.notna(penalty) and float(penalty) > 0:
            parts.append(f"质量惩罚{float(penalty):.1f}分")
        return "；".join(parts) + "。"
    if float(row.get("fulltext_chars_read") or 0) <= 1000:
        return "进入Top100，但未成功读取足够公告/半年报全文，按证据门槛淘汰。"
    if float(row.get("documents_read_count") or 0) < 1:
        return "进入Top100，但没有成功复核的完整文档，按证据门槛淘汰。"
    final_rank = row.get("reviewed_rank")
    peer = row.get("peer_quality_score")
    reason_text = "进入Top100并完成全文复核，但复核后综合分未进入最终20"
    details = []
    if pd.notna(final_rank):
        details.append(f"复核池排名{int(final_rank)}")
    if pd.notna(peer):
        details.append(f"同行质量分{float(peer):.1f}")
    if details:
        reason_text += "（" + "，".join(details) + "）"
    return reason_text + "。"


def main() -> int:
    config = json.loads(CONFIG.read_text("utf-8"))
    familiar = pd.DataFrame(config["securities"])
    familiar["code"] = familiar["code"].astype(str).str.zfill(6)

    scored = pd.read_csv(OUT / "universe_four_model_scores.csv", dtype={"security_code": str}, low_memory=False)
    scored["security_code"] = scored["security_code"].astype(str).str.zfill(6)
    scored["global_rank"] = pd.to_numeric(scored["preliminary_convexity_score"], errors="coerce").rank(method="min", ascending=False)

    blind = pd.read_csv(OUT / "top100_blind_preliminary.csv", dtype={"security_code": str}, low_memory=False)
    blind_code = "security_code" if "security_code" in blind.columns else None
    if blind_code:
        blind[blind_code] = blind[blind_code].astype(str).str.zfill(6)
        top100_codes = set(blind[blind_code])
    else:
        # Blind file intentionally may omit clear codes; reviewed output is the authoritative mapping.
        top100_codes = set()

    reviewed = pd.read_csv(OUT / "top100_announcement_reviews.csv", dtype={"code": str}, low_memory=False)
    reviewed["code"] = reviewed["code"].astype(str).str.zfill(6)
    top100_codes |= set(reviewed["code"])
    if "reviewed_composite_score" in reviewed:
        reviewed["reviewed_rank"] = pd.to_numeric(reviewed["reviewed_composite_score"], errors="coerce").rank(method="min", ascending=False)
    else:
        reviewed["reviewed_rank"] = np.nan

    peers = pd.read_csv(OUT / "top100_peer_comparison.csv", dtype={"code": str}, low_memory=False)
    peers["code"] = peers["code"].astype(str).str.zfill(6)
    final = pd.read_csv(OUT / "final20_high_convexity.csv", dtype={"security_code": str}, low_memory=False)
    final["security_code"] = final["security_code"].astype(str).str.zfill(6)
    final_codes = set(final["security_code"])

    score_cols = [
        "security_code", "name", "industry", "primary_model",
        "fundamental_acceleration_score", "new_profit_pool_score",
        "cycle_inflection_score", "capital_event_score",
        "preliminary_convexity_score", "global_rank", "quality_penalty",
        "market_cap", "pe_dynamic", "revenue_q2_yoy", "net_profit_q2_yoy",
        "gross_margin_delta", "ocf_total_ratio",
    ]
    score_cols = [c for c in score_cols if c in scored.columns]
    audit = familiar.merge(scored[score_cols], left_on="code", right_on="security_code", how="left", suffixes=("_configured", "_actual"))
    review_cols = [
        "code", "fulltext_chars_read", "documents_read_count", "evidence_strength",
        "nonconsensus_score", "overheat_penalty", "oneoff_penalty", "reviewed_composite_score",
        "reviewed_rank", "milestones", "invalidation", "review_errors",
    ]
    audit = audit.merge(reviewed[[c for c in review_cols if c in reviewed.columns]], on="code", how="left")
    peer_cols = ["code", "peer_count", "peer_quality_score", "top_peers_json"]
    audit = audit.merge(peers[[c for c in peer_cols if c in peers.columns]], on="code", how="left")
    audit["top100_selected"] = audit["code"].isin(top100_codes)
    audit["final20_selected"] = audit["code"].isin(final_codes)
    audit["audit_conclusion"] = audit.apply(reason, axis=1)
    audit = audit.sort_values(["final20_selected", "top100_selected", "global_rank"], ascending=[False, False, True])

    out_csv = OUT / "familiar_stocks_audit.csv"
    audit.to_csv(out_csv, index=False, encoding="utf-8-sig")

    lines = [
        "# 熟悉标的反偏见审计",
        "",
        "> 本表在盲筛完成后生成；名单配置从未进入评分、Top100生成或最终20选择。",
        "",
        "|代码|配置名称|实际名称|行业|全市场排名|Top100|最终20|主模型|结论|",
        "|---|---|---|---|---:|---|---|---|---|",
    ]
    for _, row in audit.iterrows():
        rank = "—" if pd.isna(row.get("global_rank")) else str(int(row["global_rank"]))
        lines.append(
            f"|{row['code']}|{row.get('name_configured','')}|{row.get('name_actual',row.get('name',''))}|"
            f"{row.get('industry','')}|{rank}|{'是' if row['top100_selected'] else '否'}|"
            f"{'是' if row['final20_selected'] else '否'}|{row.get('primary_model','')}|{row['audit_conclusion']}|"
        )
    lines += [
        "",
        "## 使用纪律",
        "",
        "- 本表只回答熟悉标的在统一规则下为何入选或淘汰。",
        "- 不能因为历史上讨论过某家公司而提高其分数、补录证据或绕过全文门槛。",
        "- 最终20之外的熟悉标的仍可作为研究对象，但不能被表述为全市场最优候选。",
    ]
    (OUT / "familiar_stocks_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "configured": len(audit),
        "found_in_universe": int(audit["security_code"].notna().sum()),
        "entered_top100": int(audit["top100_selected"].sum()),
        "entered_final20": int(audit["final20_selected"].sum()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
