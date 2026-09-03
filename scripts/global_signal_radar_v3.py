#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final reliability launcher for the overseas-industry signal radar.

V2 correctly added source fallbacks and interpretation rules, but two wrappers
called the monkey-patched function through the shared module object and could
recurse.  V3 captures the original implementations before installing any
wrapper, then applies the same source/semantics improvements safely.
"""
from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd

import global_signal_radar as base
import global_signal_radar_v2 as v2


ORIGINAL_CLINICAL_EVENTS = base.clinical_events
ORIGINAL_EVALUATE_NUMERIC = base.evaluate_numeric


def fixed_clinical_events(signal: dict[str, Any], prior: dict[str, Any]):
    events, snapshot = ORIGINAL_CLINICAL_EVENTS(signal, prior)
    existing_titles = " | ".join(str(event.get("title", "")) for event in events)
    old_all = prior.get(signal["id"]) or {}
    adjusted: list[dict[str, Any]] = list(events)

    if not old_all:
        by_nct = {event["title"].split("纳入")[0]: event for event in events}
        adjusted = []
        for nct, current in snapshot.items():
            status = str(current.get("status", "")).upper()
            if status in {"TERMINATED", "WITHDRAWN", "SUSPENDED"}:
                adjusted.append(base.make_event(
                    signal,
                    nct + ":standing-negative",
                    f"{nct}状态为{status}：{current.get('title', '')}",
                    "该状态本身已经构成负面事实，不能仅作为中性建档。",
                    f"https://clinicaltrials.gov/study/{nct}",
                    -1,
                    "P1",
                ))
            else:
                adjusted.append(by_nct.get(nct) or base.make_event(
                    signal,
                    nct,
                    f"{nct}纳入临床监控",
                    json.dumps(current, ensure_ascii=False),
                    f"https://clinicaltrials.gov/study/{nct}",
                    0,
                    "P3",
                ))
    else:
        for nct, current in snapshot.items():
            status = str(current.get("status", "")).upper()
            if status in {"TERMINATED", "WITHDRAWN", "SUSPENDED"} and nct not in existing_titles:
                adjusted.append(base.make_event(
                    signal,
                    nct + ":standing-negative",
                    f"{nct}状态为{status}：{current.get('title', '')}",
                    "持续性负面状态；在状态恢复或替代试验明确前维持风险提示。",
                    f"https://clinicaltrials.gov/study/{nct}",
                    -1,
                    "P1",
                ))
    return adjusted, snapshot


def fixed_evaluate_numeric(signal: dict[str, Any], rows: pd.DataFrame) -> dict[str, Any]:
    result = ORIGINAL_EVALUATE_NUMERIC(signal, rows)
    level_spec = (signal.get("thresholds") or {}).get("level")
    if not level_spec:
        return result

    latest = float(result["latest"])
    p1 = base.num(level_spec.get("p1"))
    p2 = base.num(level_spec.get("p2"))
    level = (
        "P1" if base.finite(p1) and latest >= p1
        else "P2" if base.finite(p2) and latest >= p2
        else "NONE"
    )

    # Positive-level indicators remain positive while their absolute level is
    # strong.  Month-on-month deceleration is retained as a separate momentum
    # field instead of incorrectly reversing the fundamental direction.
    if level != "NONE":
        result["level"] = level
        result["severity"] = base.LEVEL_NUM[level]
        result["trend_sign"] = 1
        result["level_interpretation"] = "absolute level positive; momentum evaluated separately"
    abs_change = result.get("abs_1")
    result["momentum_sign"] = (
        1 if base.finite(abs_change) and float(abs_change) > 0
        else -1 if base.finite(abs_change) and float(abs_change) < 0
        else 0
    )
    return result


def patch_data_health() -> None:
    latest = base.load_json(base.LATEST_FILE, {})
    errors = latest.get("errors") or []
    failed_ids = {str(error.get("signal_id")) for error in errors}
    results = latest.get("signal_results") or {}
    latest["data_health"] = {
        "configured": len(base.load_json(base.CATALOG, {}).get("signals") or []),
        "successful": len(set(results) - failed_ids),
        "failed": len(failed_ids),
    }
    base.write_json(base.LATEST_FILE, latest)

    try:
        report_path = base.REPORT_DIR / "latest.md"
        report = report_path.read_text("utf-8")
        report = re.sub(
            r"- 数据源成功：\d+条；失败：\d+条。",
            f"- 数据源成功：{latest['data_health']['successful']}条；失败：{latest['data_health']['failed']}条。",
            report,
        )
        base.atomic_write(report_path, report)
        dated = base.REPORT_DIR / f"{base.NOW:%Y-%m-%d}-{base.RUN_SLOT}.md"
        if dated.exists():
            base.atomic_write(dated, report)
    except Exception as exc:  # noqa: BLE001
        base.log(f"data-health report patch failed: {exc}")


def main() -> int:
    base.fetch_baltic = v2.robust_fetch_baltic
    base.fetch_sia = v2.robust_fetch_sia
    base.fetch_acea = v2.robust_fetch_acea
    base.sec_events = v2.robust_sec_events
    base.clinical_events = fixed_clinical_events
    base.evaluate_numeric = fixed_evaluate_numeric

    result = base.main()
    patch_data_health()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
