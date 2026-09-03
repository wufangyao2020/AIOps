#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "global_signal_catalog.json"
STOCK_MAP = ROOT / "config" / "global_signal_stock_map.csv"
FINAL20 = ROOT / "config" / "final20_overseas_sensitivity.csv"


def main() -> int:
    catalog = json.loads(CATALOG.read_text("utf-8"))
    signals = catalog.get("signals") or []
    mapping = pd.read_csv(STOCK_MAP, dtype={"security_code": str})
    final20 = pd.read_csv(FINAL20, dtype={"security_code": str})

    errors: list[str] = []
    signal_ids = [str(s.get("id", "")) for s in signals]
    if len(signal_ids) != len(set(signal_ids)):
        errors.append("signal id重复")
    if len(final20) != 20 or final20["security_code"].nunique() != 20:
        errors.append("最终20敏感度表不是20个唯一代码")

    required_signal = {"id", "name", "group", "provider", "frequency", "source_tier", "source_url"}
    for i, signal in enumerate(signals, 1):
        missing = required_signal - set(signal)
        if missing:
            errors.append(f"signal#{i}缺少字段：{sorted(missing)}")

    valid_ids = set(signal_ids)
    required_map = {"signal_id", "security_code", "security_name", "direction", "weight", "directness", "rationale", "in_final20"}
    if not required_map.issubset(mapping.columns):
        errors.append(f"映射表缺少字段：{sorted(required_map - set(mapping.columns))}")
    else:
        bad_signal = mapping[~mapping["signal_id"].isin(valid_ids)]
        if not bad_signal.empty:
            errors.append(f"映射引用不存在的signal_id：{bad_signal['signal_id'].unique().tolist()}")
        bad_code = ~mapping["security_code"].astype(str).str.fullmatch(r"\d{6}")
        if bad_code.any():
            errors.append("映射表存在非法股票代码")
        if not mapping["direction"].isin([-1, 0, 1]).all():
            errors.append("direction必须为-1/0/1")
        weight = pd.to_numeric(mapping["weight"], errors="coerce")
        if weight.isna().any() or not weight.between(0.05, 1.5).all():
            errors.append("weight必须在0.05到1.5之间")
        if not mapping["directness"].isin(["A", "B", "C"]).all():
            errors.append("directness必须为A/B/C")

    mapped_codes = set(mapping["security_code"])
    final_codes = set(final20["security_code"])
    mapped_final = mapped_codes & final_codes
    low_count = int((final20["overseas_sensitivity"] == "低").sum())

    if len(signals) < 30:
        errors.append("信号数量少于30")
    if mapping["security_code"].nunique() < 80:
        errors.append("映射A股少于80家")
    if len(mapped_final) < 15:
        errors.append("最终20中至少15家应有海外变量映射")
    if low_count < 1:
        errors.append("必须明确保留海外敏感度低的公司，避免强行叙事")

    summary = {
        "valid": not errors,
        "signal_count": len(signals),
        "signal_groups": dict(Counter(s["group"] for s in signals)),
        "provider_count": len({s["provider"] for s in signals}),
        "mapping_rows": len(mapping),
        "mapped_stock_count": mapping["security_code"].nunique(),
        "directness": mapping["directness"].value_counts().to_dict(),
        "final20_count": len(final20),
        "mapped_final20_count": len(mapped_final),
        "low_sensitivity_final20_count": low_count,
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
