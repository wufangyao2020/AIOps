#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reliability and interpretation fixes for the overseas-industry signal radar.

This launcher keeps the audited V1 catalog/mapping but patches four weaknesses
found by the first two live GitHub Actions runs:

1. BDI/BCI/BPI/BSI are fetched from several official Baltic pages, with a
   clearly-labelled public secondary fallback instead of silently disappearing.
2. TSMC/SIA/ACEA level indicators distinguish a still-strong absolute level
   from a slowing growth rate; deceleration alone cannot flip a strong level
   into a P1 bearish signal.
3. An already TERMINATED/WITHDRAWN/SUSPENDED clinical study is negative even on
   the first baseline run.
4. SIA, ACEA and SEC collection have additional official-page fallbacks.
"""
from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

import global_signal_radar as base


DRY_FALLBACKS = {
    "BDI": ("https://www.balticdryindex.com/", r"BDI\s*(?:Today)?\s*([0-9][0-9,]*(?:\.\d+)?)"),
    "BCI": ("https://sc.macromicro.me/series/3469/baltic-capesize-index", r"BCI[^\d]{0,80}(20\d{2}-\d{2}-\d{2})\s*([0-9][0-9,]*(?:\.\d+)?)"),
    "BPI": ("https://sc.macromicro.me/series/3470/baltic-panamax-index", r"BPI[^\d]{0,80}(20\d{2}-\d{2}-\d{2})\s*([0-9][0-9,]*(?:\.\d+)?)"),
    "BSI": ("https://sc.macromicro.me/series/3471/baltic-supramax-index", r"BSI[^\d]{0,80}(20\d{2}-\d{2}-\d{2})\s*([0-9][0-9,]*(?:\.\d+)?)"),
    "BDTI": ("https://sc.macromicro.me/series/3659/baltic-dirty-tanker-index", r"BDTI[^\d]{0,80}(20\d{2}-\d{2}-\d{2})\s*([0-9][0-9,]*(?:\.\d+)?)"),
}


def _observation(code: str, value: float, day: str, provider: str, url: str) -> dict[str, list[dict[str, Any]]]:
    return {code.lower(): [{"date": day, "value": float(value), "provider": provider, "source_url": url}]}


def robust_fetch_baltic() -> dict[str, list[dict[str, Any]]]:
    values: dict[str, list[dict[str, Any]]] = {}
    official_urls = [
        "https://www.balticexchange.com/en/data-services/routes.html",
        "https://www.balticexchange.com/en/data-services/freight-derivatives-.html",
        "https://www.balticexchange.com/en/data-services/WeeklyRoundup.html",
    ]
    pattern = re.compile(r"\b(" + "|".join(base.BALTIC_CODES) + r")\s*[:：]?\s*([0-9][0-9,]*(?:\.\d+)?)\b")
    for url in official_urls:
        try:
            text = BeautifulSoup(base.request(url, retries=1, timeout=12).text, "html.parser").get_text(" ", strip=True)
            for code, raw in pattern.findall(text):
                value = base.num(raw)
                if base.finite(value) and code.lower() not in values:
                    values.update(_observation(code, value, base.NOW.date().isoformat(), "Baltic Exchange", url))
        except Exception as exc:  # noqa: BLE001
            base.log(f"Baltic official page unavailable: {url}: {exc}")

    # Secondary public pages are used only when an official headline value is
    # absent. Provider/source are explicit, so the report never mislabels them.
    for code, (url, regex) in DRY_FALLBACKS.items():
        if code.lower() in values:
            continue
        try:
            text = re.sub(r"\s+", " ", BeautifulSoup(base.request(url, retries=1, timeout=12).text, "html.parser").get_text(" ", strip=True))
            match = re.search(regex, text, re.I | re.S)
            if not match:
                # BalticDryIndex.com exposes all four dry indices in prose/table
                # with the code immediately adjacent to a value.
                match2 = re.search(rf"\b{code}\b[^0-9]{{0,80}}([0-9][0-9,]*(?:\.\d+)?)", text, re.I)
                if not match2:
                    continue
                day, raw = base.NOW.date().isoformat(), match2.group(1)
            elif len(match.groups()) == 2:
                day, raw = match.group(1), match.group(2)
            else:
                day, raw = base.NOW.date().isoformat(), match.group(1)
            value = base.num(raw)
            if base.finite(value):
                provider = "BalticDryIndex.com (Baltic Exchange sourced)" if "balticdryindex.com" in url else "MacroMicro public page (Baltic Exchange sourced)"
                values.update(_observation(code, value, day, provider, url))
        except Exception as exc:  # noqa: BLE001
            base.log(f"Baltic fallback unavailable: {code}: {exc}")

    if not any(code in values for code in ("bdi", "bci", "bpi", "bsi")):
        raise RuntimeError("BDI/BCI/BPI/BSI unavailable from official and explicit secondary sources")
    return values


def robust_fetch_sia() -> list[dict[str, Any]]:
    index_urls = [
        "https://www.semiconductors.org/news-events/latest-news/",
        "https://www.semiconductors.org/category/industry-statistics/",
        "https://www.semiconductors.org/",
    ]
    links: list[str] = []
    for index in index_urls:
        try:
            soup = BeautifulSoup(base.request(index, retries=2, timeout=15).text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                label = anchor.get_text(" ", strip=True)
                href = str(anchor["href"])
                if "Global Semiconductor Sales" in label or "global-semiconductor-sales" in href:
                    if href.startswith("/"):
                        href = "https://www.semiconductors.org" + href
                    links.append(href)
        except Exception:
            continue
    for url in dict.fromkeys(links):
        try:
            soup = BeautifulSoup(base.request(url, retries=1, timeout=12).text, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
            match = re.search(r"(?:increase|up)\s+([0-9]+(?:\.[0-9]+)?)%\s+(?:year-to-year|year-over-year)", title + " " + text, re.I)
            if not match:
                match = re.search(r"([0-9]+(?:\.[0-9]+)?)%\s+(?:compared to|more than).{0,40}(?:year earlier|last year)", text, re.I)
            if match:
                date_match = re.search(r"\b([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})\b", text)
                day = pd.to_datetime(date_match.group(1)).date().isoformat() if date_match else base.NOW.date().isoformat()
                return [{"date": day, "value": base.num(match.group(1)), "provider": "Semiconductor Industry Association", "source_url": url}]
        except Exception:
            continue
    raise RuntimeError("SIA official pages did not expose a parseable latest global-sales release")


def robust_fetch_acea() -> list[dict[str, Any]]:
    index_urls = ["https://www.acea.auto/", "https://www.acea.auto/press-releases/"]
    links: list[str] = []
    for index in index_urls:
        try:
            soup = BeautifulSoup(base.request(index, retries=2, timeout=15).text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                href = str(anchor["href"])
                label = anchor.get_text(" ", strip=True)
                if "/pc-registrations/" in href or ("registrations" in label.lower() and "battery-electric" in label.lower()):
                    if href.startswith("/"):
                        href = "https://www.acea.auto" + href
                    links.append(href)
        except Exception:
            continue
    for url in dict.fromkeys(links):
        try:
            text = re.sub(r"\s+", " ", BeautifulSoup(base.request(url, retries=1, timeout=12).text, "html.parser").get_text(" ", strip=True))
            match = re.search(r"battery-electric(?: cars?)?.{0,100}?(?:accounted for|market share.{0,20}?(?:reached|was|stood at)|capturing)\s*([0-9]+(?:\.[0-9]+)?)%", text, re.I)
            if not match:
                title_match = re.search(r"battery-electric\s+([0-9]+(?:\.[0-9]+)?)%\s+market share", text, re.I)
                match = title_match
            if match:
                date_match = re.search(r"\b(\d{1,2}\s+[A-Z][a-z]+\s+20\d{2})\b", text)
                day = pd.to_datetime(date_match.group(1)).date().isoformat() if date_match else base.NOW.date().isoformat()
                return [{"date": day, "value": base.num(match.group(1)), "provider": "ACEA", "source_url": url}]
        except Exception:
            continue
    raise RuntimeError("ACEA official pages did not expose a parseable passenger-registration release")


def robust_sec_events(signal: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cutoff = base.NOW.date() - timedelta(days=10)
    allowed = set(signal.get("forms", []))
    atom_ns = {"a": "http://www.w3.org/2005/Atom"}
    for cik in signal.get("ciks", []):
        params = {"action": "getcompany", "CIK": str(cik), "type": "", "owner": "exclude", "count": 40, "output": "atom"}
        xml = base.request("https://www.sec.gov/cgi-bin/browse-edgar", params=params, headers={"Accept": "application/atom+xml,application/xml,text/xml"}, retries=2, timeout=15).content
        root = ET.fromstring(xml)
        company = root.findtext("a:title", default=str(cik), namespaces=atom_ns)
        for entry in root.findall("a:entry", atom_ns):
            category = entry.find("a:category", atom_ns)
            form = category.get("term", "") if category is not None else ""
            updated = entry.findtext("a:updated", default="", namespaces=atom_ns)[:10]
            try:
                filing_day = date.fromisoformat(updated)
            except Exception:
                continue
            if form not in allowed or filing_day < cutoff:
                continue
            link = entry.find("a:link", atom_ns)
            url = link.get("href", "") if link is not None else ""
            event_id = entry.findtext("a:id", default=base.fingerprint([cik, form, updated, url]), namespaces=atom_ns)
            events.append(base.make_event(signal, event_id, f"{company}提交{form}", f"SEC filing {form}", url, 0, "P2" if form in {"10-Q", "10-K"} else "P3", updated))
    return events


def fixed_clinical_events(signal: dict[str, Any], prior: dict[str, Any]):
    events, snapshot = base.clinical_events(signal, prior)
    existing_titles = " | ".join(str(event.get("title", "")) for event in events)
    old_all = prior.get(signal["id"]) or {}
    adjusted: list[dict[str, Any]] = list(events)
    if not old_all:
        by_nct = {event["title"].split("纳入")[0]: event for event in events}
        adjusted = []
        for nct, current in snapshot.items():
            status = str(current.get("status", "")).upper()
            if status in {"TERMINATED", "WITHDRAWN", "SUSPENDED"}:
                adjusted.append(base.make_event(signal, nct + ":standing-negative", f"{nct}状态为{status}：{current.get('title','')}", "该状态本身已经构成负面事实，不能仅作为中性建档。", f"https://clinicaltrials.gov/study/{nct}", -1, "P1"))
            else:
                adjusted.append(by_nct.get(nct) or base.make_event(signal, nct, f"{nct}纳入临床监控", json.dumps(current, ensure_ascii=False), f"https://clinicaltrials.gov/study/{nct}", 0, "P3"))
    else:
        for nct, current in snapshot.items():
            status = str(current.get("status", "")).upper()
            if status in {"TERMINATED", "WITHDRAWN", "SUSPENDED"} and nct not in existing_titles:
                adjusted.append(base.make_event(signal, nct + ":standing-negative", f"{nct}状态为{status}：{current.get('title','')}", "持续性负面状态；在状态恢复或试验替代前维持风险提示。", f"https://clinicaltrials.gov/study/{nct}", -1, "P1"))
    return adjusted, snapshot


def fixed_evaluate_numeric(signal: dict[str, Any], rows: pd.DataFrame) -> dict[str, Any]:
    result = base.evaluate_numeric(signal, rows)
    level_spec = (signal.get("thresholds") or {}).get("level")
    if not level_spec:
        return result
    latest = float(result["latest"])
    p1, p2 = base.num(level_spec.get("p1")), base.num(level_spec.get("p2"))
    level = "P1" if base.finite(p1) and latest >= p1 else "P2" if base.finite(p2) and latest >= p2 else "NONE"
    # For positive-level indicators such as TSMC revenue YoY, SIA sales YoY and
    # ACEA BEV share, a high positive level remains positive even if the growth
    # rate decelerates from an unusually high prior month.
    if level != "NONE":
        result["level"] = level
        result["severity"] = base.LEVEL_NUM[level]
        result["trend_sign"] = 1
        result["level_interpretation"] = "absolute level positive; momentum evaluated separately"
    result["momentum_sign"] = 1 if base.finite(result.get("abs_1")) and result["abs_1"] > 0 else -1 if base.finite(result.get("abs_1")) and result["abs_1"] < 0 else 0
    return result


def main() -> int:
    base.fetch_baltic = robust_fetch_baltic
    base.fetch_sia = robust_fetch_sia
    base.fetch_acea = robust_fetch_acea
    base.sec_events = robust_sec_events
    base.clinical_events = fixed_clinical_events
    base.evaluate_numeric = fixed_evaluate_numeric
    rc = base.main()

    # Correct data-health semantics: a signal that raised a fetch error is not a
    # successful source merely because an empty event result was materialised.
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
        report = (base.REPORT_DIR / "latest.md").read_text("utf-8")
        report = re.sub(r"- 数据源成功：\d+条；失败：\d+条。", f"- 数据源成功：{latest['data_health']['successful']}条；失败：{latest['data_health']['failed']}条。", report)
        base.atomic_write(base.REPORT_DIR / "latest.md", report)
        dated = base.REPORT_DIR / f"{base.NOW:%Y-%m-%d}-{base.RUN_SLOT}.md"
        if dated.exists():
            base.atomic_write(dated, report)
    except Exception as exc:  # noqa: BLE001
        base.log(f"data-health report patch failed: {exc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
