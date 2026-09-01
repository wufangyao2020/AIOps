#!/usr/bin/env python3
"""Validate Stage 1/2 research outputs and fail loudly on silent data defects."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "data" / "2026H1" / "research"
META = ROOT / "meta"


def check(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_stage1(errors: list[str]) -> dict:
    universe_path = RESEARCH / "universe_scored_stage1.csv"
    top100_path = RESEARCH / "top100_stage1.csv"
    check(universe_path.exists(), f"missing {universe_path}", errors)
    check(top100_path.exists(), f"missing {top100_path}", errors)
    if errors:
        return {}
    universe = pd.read_csv(universe_path, dtype={"security_code": str}, low_memory=False)
    top100 = pd.read_csv(top100_path, dtype={"security_code": str}, low_memory=False)
    check(len(universe) == 5550, f"stage1 universe count {len(universe)} != 5550", errors)
    check(universe["security_code"].nunique() == 5550, "stage1 duplicate security codes", errors)
    check(len(top100) == 100, f"top100 count {len(top100)} != 100", errors)
    check(top100["security_code"].nunique() == 100, "top100 duplicate security codes", errors)
    check(not top100["security_name"].astype(str).str.contains(r"ST|退", regex=True).any(), "ST/退 company entered top100", errors)
    for col in [
        "fundamental_acceleration_score", "new_profit_pool_score_stage1",
        "cyclical_turn_score", "capital_event_score", "convexity_score_stage1",
    ]:
        check(col in universe.columns, f"missing score column {col}", errors)
        if col in universe:
            numeric = pd.to_numeric(universe[col], errors="coerce")
            check(numeric.notna().mean() > 0.98, f"{col} missingness too high", errors)
            check(numeric.between(0, 100).mean() > 0.999, f"{col} outside 0..100", errors)
    unknown_share = (universe.get("industry", pd.Series(dtype=str)).fillna("UNKNOWN") == "UNKNOWN").mean()
    check(unknown_share < 0.25, f"industry UNKNOWN share too high: {unknown_share:.2%}", errors)
    quote_share = pd.to_numeric(universe.get("market_cap_cny"), errors="coerce").notna().mean()
    check(quote_share > 0.90, f"market-cap coverage too low: {quote_share:.2%}", errors)
    q2_share = pd.to_numeric(universe.get("q2_revenue_yoy_pct"), errors="coerce").notna().mean()
    check(q2_share > 0.75, f"Q2 revenue YoY coverage too low: {q2_share:.2%}", errors)
    return {
        "stage1_universe": len(universe), "stage1_top100": len(top100),
        "industry_unknown_share": unknown_share, "market_cap_coverage": quote_share,
        "q2_revenue_yoy_coverage": q2_share,
    }


def validate_stage2(errors: list[str]) -> dict:
    reviewed_path = RESEARCH / "top100_reviewed_peer_compared.csv"
    final_path = RESEARCH / "final20_high_convexity.csv"
    dossiers_path = RESEARCH / "dossiers" / "top100_announcement_review.jsonl"
    for path in (reviewed_path, final_path, dossiers_path):
        check(path.exists(), f"missing {path}", errors)
    if errors:
        return {}
    reviewed = pd.read_csv(reviewed_path, dtype={"security_code": str}, low_memory=False)
    final = pd.read_csv(final_path, dtype={"security_code": str}, low_memory=False)
    dossier_lines = sum(1 for line in dossiers_path.open("r", encoding="utf-8") if line.strip())
    check(len(reviewed) == 100, f"reviewed pool {len(reviewed)} != 100", errors)
    check(dossier_lines == 100, f"dossier count {dossier_lines} != 100", errors)
    check(len(final) == 20, f"final candidate count {len(final)} != 20", errors)
    check(final["security_code"].nunique() == len(final), "final20 duplicate codes", errors)
    check((final["h1_report_found"] == 1).all(), "final20 includes company without H1 report", errors)
    h1_chars = pd.to_numeric(final.get("h1_report_character_count"), errors="coerce").fillna(0)
    check((h1_chars >= 1000).all(), "final20 includes company without substantive H1 full text", errors)
    check((final["review_status"] == "completed").all(), "final20 includes failed review", errors)
    check((pd.to_numeric(final["final_convexity_score"], errors="coerce").between(0, 100)).all(), "invalid final score", errors)
    check((pd.to_numeric(final["announcement_risk_score"], errors="coerce") < 72).all(), "final20 includes excessive-risk dossier", errors)
    max_industry = final.groupby("industry").size().max()
    check(max_industry <= 4, f"industry concentration exceeds 4: {max_industry}", errors)
    source_manifest_coverage = reviewed.get("source_manifest_json", pd.Series(dtype=str)).fillna("").str.len().gt(10).mean()
    check(source_manifest_coverage > 0.90, f"source manifest coverage too low: {source_manifest_coverage:.2%}", errors)
    substantive_h1_coverage = pd.to_numeric(reviewed.get("h1_report_character_count"), errors="coerce").fillna(0).ge(1000).mean()
    check(substantive_h1_coverage > 0.80, f"top100 substantive H1 coverage too low: {substantive_h1_coverage:.2%}", errors)
    return {
        "stage2_reviewed": len(reviewed), "dossier_count": dossier_lines,
        "final20_count": len(final), "max_single_industry": int(max_industry),
        "source_manifest_coverage": source_manifest_coverage,
        "substantive_h1_coverage": substantive_h1_coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("1", "2", "all"), default="all")
    args = parser.parse_args()
    errors: list[str] = []
    summary = {}
    if args.stage in ("1", "all"):
        summary.update(validate_stage1(errors))
    if args.stage in ("2", "all"):
        summary.update(validate_stage2(errors))
    summary["errors"] = errors
    summary["valid"] = not errors
    output = META / f"research_validation_stage_{args.stage}.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
