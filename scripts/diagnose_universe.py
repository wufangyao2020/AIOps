#!/usr/bin/env python3
"""Profile source dimensions to make exchange/security filters auditable."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "2026H1" / "a_share_2026_h1_raw.jsonl"
OUTPUT = ROOT / "meta" / "raw_dimension_profile.json"


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def main() -> int:
    rows: list[dict[str, Any]] = []
    with RAW.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))

    dimensions = [
        "TRADE_MARKET_CODE",
        "TRADE_MARKET",
        "SECURITY_TYPE_CODE",
        "SECURITY_TYPE",
        "ORG_CODE",
    ]
    result: dict[str, Any] = {
        "row_count": len(rows),
        "dimension_counts": {},
        "code_prefix_counts": Counter(text(row.get("SECURITY_CODE"))[:3] for row in rows).most_common(),
        "samples_by_trade_market_code": {},
    }
    for dimension in dimensions:
        result["dimension_counts"][dimension] = Counter(
            text(row.get(dimension)) or "<EMPTY>" for row in rows
        ).most_common()

    samples: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = text(row.get("TRADE_MARKET_CODE")) or "<EMPTY>"
        if len(samples[key]) < 8:
            samples[key].append(
                {
                    "security_code": text(row.get("SECURITY_CODE")),
                    "security_name": text(row.get("SECURITY_NAME_ABBR")),
                    "trade_market": text(row.get("TRADE_MARKET")),
                    "security_type_code": text(row.get("SECURITY_TYPE_CODE")),
                    "security_type": text(row.get("SECURITY_TYPE")),
                }
            )
    result["samples_by_trade_market_code"] = dict(sorted(samples.items()))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["dimension_counts"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
