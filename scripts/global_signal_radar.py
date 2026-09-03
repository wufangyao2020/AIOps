#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A-share overseas industry signal radar.

The radar converts overseas industry data and public events into auditable A-share
review alerts. It never emits automatic buy/sell orders. A signal must be
cross-checked against company filings, quarterly operating data, or relative price
strength before it can affect a position.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "global_signal_catalog.json"
STOCK_MAP = ROOT / "config" / "global_signal_stock_map.csv"
FINAL20 = ROOT / "config" / "final20_overseas_sensitivity.csv"
DATA_DIR = ROOT / "data" / "global_signals"
REPORT_DIR = ROOT / "reports" / "global_signals"
NUMERIC_HISTORY = DATA_DIR / "numeric_history.csv"
EVENT_HISTORY = DATA_DIR / "event_history.jsonl"
STATE_FILE = DATA_DIR / "state.json"
LATEST_FILE = DATA_DIR / "latest.json"
ALERT_FILE = DATA_DIR / "latest_alert.json"

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime.now(TZ)
RUN_SLOT = "am" if NOW.hour < 12 else "pm"
UA = (
    "AIOps-Global-Signal-Radar/1.0 "
    "(https://github.com/wufangyao2020/AIOps; "
    "contact: wufangyao2020@users.noreply.github.com)"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json,text/html,application/xhtml+xml,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
BALTIC_CODES = ("BDI", "BCI", "BPI", "BSI", "BDTI", "BCTI", "BLNG", "BLPG", "FBX", "BAI00")
LEVEL_NUM = {"NONE": 0, "P3": 1, "P2": 2, "P1": 3}


def log(message: str) -> None:
    print(f"[{datetime.now(TZ).isoformat(timespec='seconds')}] {message}", flush=True)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def request(url: str, *, params: dict[str, Any] | None = None, timeout: int = 35,
            retries: int = 4, headers: dict[str, str] | None = None) -> requests.Response:
    last: Exception | None = None
    merged = {**HEADERS, **(headers or {})}
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, headers=merged, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f"GET failed after {retries} attempts: {url}: {last}")


def num(value: Any) -> float:
    if value is None:
        return float("nan")
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in {"", "-", "—", "nan", "None", "null"}:
        return float("nan")
    try:
        return float(text)
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(match.group()) if match else float("nan")


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def pct_change(current: float, previous: float) -> float:
    if not finite(current) or not finite(previous) or abs(previous) < 1e-12:
        return float("nan")
    return (current / previous - 1.0) * 100.0


def fingerprint(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def fetch_baltic() -> dict[str, list[dict[str, Any]]]:
    url = "https://www.balticexchange.com/en/data-services/routes.html"
    text = BeautifulSoup(request(url).text, "html.parser").get_text(" ", strip=True)
    pattern = re.compile(r"\b(" + "|".join(BALTIC_CODES) + r")\s*([0-9][0-9,]*(?:\.\d+)?)\b")
    found: dict[str, float] = {}
    for code, value in pattern.findall(text):
        if code not in found and finite(num(value)):
            found[code] = num(value)
    if not found:
        raise RuntimeError("Baltic Exchange headline values were not parsed")
    today = NOW.date().isoformat()
    return {
        code.lower(): [{"date": today, "value": value, "provider": "Baltic Exchange", "source_url": url}]
        for code, value in found.items()
    }


def fetch_fred(series: str) -> list[dict[str, Any]]:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    frame = pd.read_csv(io.StringIO(request(url, params={"id": series}).text))
    if frame.empty or len(frame.columns) < 2:
        raise RuntimeError(f"FRED returned empty data for {series}")
    dcol, vcol = frame.columns[:2]
    frame[vcol] = pd.to_numeric(frame[vcol], errors="coerce")
    frame = frame.dropna(subset=[vcol]).tail(200)
    return [
        {"date": str(row[dcol])[:10], "value": float(row[vcol]), "provider": "FRED",
         "source_url": f"https://fred.stlouisfed.org/series/{series}"}
        for _, row in frame.iterrows()
    ]


def fetch_yahoo(ticker: str) -> list[dict[str, Any]]:
    end = int(datetime.now(timezone.utc).timestamp())
    start = int((datetime.now(timezone.utc) - timedelta(days=220)).timestamp())
    errors: list[str] = []
    for host in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
        try:
            url = f"{host}/v8/finance/chart/{quote(ticker, safe='')}"
            payload = request(url, params={
                "period1": start, "period2": end, "interval": "1d",
                "events": "history", "includeAdjustedClose": "true",
            }, headers={"Referer": "https://finance.yahoo.com/"}).json()
            chart = payload.get("chart") or {}
            if chart.get("error"):
                raise RuntimeError(str(chart["error"]))
            result = (chart.get("result") or [None])[0] or {}
            timestamps = result.get("timestamp") or []
            closes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
            rows = []
            for ts, close in zip(timestamps, closes):
                if not finite(close):
                    continue
                day = datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat()
                rows.append({
                    "date": day, "value": float(close),
                    "provider": "Yahoo public chart endpoint",
                    "source_url": f"https://finance.yahoo.com/quote/{ticker}",
                })
            if rows:
                return list({row["date"]: row for row in rows}.values())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{host}: {exc}")
    raise RuntimeError("Yahoo chart failed: " + " | ".join(errors))


def fetch_tsmc(url: str) -> list[dict[str, Any]]:
    tables = pd.read_html(io.StringIO(request(url).text))
    year_match = re.search(r"/(20\d{2})/?$", url)
    year = int(year_match.group(1)) if year_match else NOW.year
    months = {m.lower()[:3]: i for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
    rows: list[dict[str, Any]] = []
    for table in tables:
        table = table.copy()
        table.columns = [" ".join(str(x) for x in col if str(x) != "nan") if isinstance(col, tuple) else str(col)
                         for col in table.columns]
        yoy_col = next((c for c in table.columns if "YoY" in c or "Change" in c), None)
        if not yoy_col:
            continue
        month_col = table.columns[0]
        for _, row in table.iterrows():
            key = str(row[month_col]).strip().lower()[:3]
            month = months.get(key)
            value = num(row[yoy_col])
            if month and finite(value):
                day = (pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)).date().isoformat()
                rows.append({"date": day, "value": value, "provider": "TSMC investor relations", "source_url": url})
    if not rows:
        raise RuntimeError("TSMC monthly revenue table was not parsed")
    return list({row["date"]: row for row in rows}.values())


def fetch_sia() -> list[dict[str, Any]]:
    feed_urls = [
        "https://www.semiconductors.org/feed/",
        "https://www.semiconductors.org/category/industry-statistics/feed/",
    ]
    candidates: list[tuple[str, str, str]] = []
    for feed in feed_urls:
        try:
            root = ET.fromstring(request(feed).content)
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if "Global Semiconductor Sales" in title and link:
                    candidates.append((pub, title, link))
        except Exception:
            continue
    if not candidates:
        raise RuntimeError("No SIA global-sales post found")
    pub, title, link = candidates[0]
    match = re.search(r"(?:Increase|Up)\s+([0-9]+(?:\.[0-9]+)?)%\s+(?:Year|in)", title, re.I)
    if not match:
        text = BeautifulSoup(request(link).text, "html.parser").get_text(" ", strip=True)
        match = re.search(r"increase of\s+([0-9]+(?:\.[0-9]+)?)%\s+compared", text, re.I)
    if not match:
        raise RuntimeError(f"SIA YoY change was not parsed: {title}")
    try:
        day = pd.to_datetime(pub, utc=True).date().isoformat()
    except Exception:
        day = NOW.date().isoformat()
    return [{"date": day, "value": num(match.group(1)), "provider": "SIA", "source_url": link}]


def fetch_acea() -> list[dict[str, Any]]:
    index = "https://www.acea.auto/press-releases/"
    soup = BeautifulSoup(request(index).text, "html.parser")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if "/pc-registrations/" in href:
            links.append(href if href.startswith("http") else "https://www.acea.auto" + href)
    if not links:
        raise RuntimeError("ACEA passenger-registration post was not found")
    url = links[0]
    text = BeautifulSoup(request(url).text, "html.parser").get_text(" ", strip=True)
    match = re.search(r"battery-electric(?: cars?)?.{0,70}?(?:accounted for|share.{0,10}?reached|share.{0,10}?was)\s*([0-9]+(?:\.[0-9]+)?)%", text, re.I)
    if not match:
        raise RuntimeError(f"ACEA BEV share was not parsed: {url}")
    return [{"date": NOW.date().isoformat(), "value": num(match.group(1)), "provider": "ACEA", "source_url": url}]


def clinical_events(signal: dict[str, Any], prior: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    old_all = prior.get(signal["id"]) or {}
    for nct in signal.get("study_ids", []):
        payload = request(f"https://clinicaltrials.gov/api/v2/studies/{nct}").json()
        protocol = payload.get("protocolSection") or {}
        ident = protocol.get("identificationModule") or {}
        status = protocol.get("statusModule") or {}
        design = protocol.get("designModule") or {}
        current = {
            "title": ident.get("briefTitle") or ident.get("officialTitle") or "",
            "status": status.get("overallStatus") or "",
            "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
            "primary_completion": (status.get("primaryCompletionDateStruct") or {}).get("date"),
            "completion": (status.get("completionDateStruct") or {}).get("date"),
            "has_results": bool(payload.get("resultsSection")),
        }
        snapshot[nct] = current
        old = old_all.get(nct)
        if old is None:
            events.append(make_event(signal, nct, f"{nct}纳入临床监控：{current['title']}",
                                     f"状态={current['status']}，入组={current['enrollment']}，主要完成={current['primary_completion']}",
                                     f"https://clinicaltrials.gov/study/{nct}", 0, "P3"))
        elif old != current:
            changed = {k: {"old": old.get(k), "new": current.get(k)} for k in current if old.get(k) != current.get(k)}
            status_text = str(current["status"]).upper()
            polarity = -1 if status_text in {"TERMINATED", "WITHDRAWN", "SUSPENDED"} else 1 if current["has_results"] or status_text == "COMPLETED" else 0
            level = "P1" if polarity < 0 or current["has_results"] else "P2"
            events.append(make_event(signal, nct + fingerprint(changed), f"{nct}临床记录更新：{current['title']}",
                                     json.dumps(changed, ensure_ascii=False), f"https://clinicaltrials.gov/study/{nct}", polarity, level))
    return events, snapshot


def make_event(signal: dict[str, Any], event_id: str, title: str, summary: str,
               source_url: str, polarity: int, level: str, event_date: str | None = None) -> dict[str, Any]:
    return {
        "event_key": f"{signal['id']}:{event_id}", "signal_id": signal["id"], "signal_name": signal["name"],
        "date": event_date or NOW.date().isoformat(), "title": title, "summary": summary[:1200],
        "source_url": source_url, "polarity": polarity, "level": level, "fetched_at": NOW.isoformat(),
    }


def classify_event(text: str) -> int:
    lower = text.lower()
    positive = ("approval", "approved", "grant", "funding", "investment", "expansion", "reduction", "phase down", "ban on plastic")
    negative = ("terminated", "withdrawn", "suspended", "tariff", "antidumping", "countervailing", "restriction", "delay", "cancel")
    pos = sum(token in lower for token in positive)
    neg = sum(token in lower for token in negative)
    return 1 if pos > neg else -1 if neg > pos else 0


def federal_register_events(signal: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    start = (NOW.date() - timedelta(days=8)).isoformat()
    for term in signal.get("terms", []):
        payload = request("https://www.federalregister.gov/api/v1/documents.json", params={
            "per_page": 100, "order": "newest", "conditions[term]": term,
            "conditions[publication_date][gte]": start,
        }).json()
        for row in payload.get("results") or []:
            title = str(row.get("title") or "")
            abstract = str(row.get("abstract") or "")
            combined = title + " " + abstract
            level = "P1" if any(x in combined.lower() for x in ("final rule", "antidumping", "countervailing", "tariff")) else "P2"
            events.append(make_event(signal, str(row.get("document_number") or fingerprint(combined)), title, abstract,
                                     str(row.get("html_url") or row.get("pdf_url") or ""), classify_event(combined), level,
                                     str(row.get("publication_date") or NOW.date().isoformat())))
    return list({event["event_key"]: event for event in events}.values())


def sec_events(signal: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    forms_allowed = set(signal.get("forms", []))
    cutoff = NOW.date() - timedelta(days=10)
    for cik in signal.get("ciks", []):
        cik10 = str(cik).zfill(10)
        payload = request(f"https://data.sec.gov/submissions/CIK{cik10}.json", headers={"Host": "data.sec.gov"}).json()
        company = payload.get("name") or cik10
        recent = ((payload.get("filings") or {}).get("recent") or {})
        forms = recent.get("form") or []
        days = recent.get("filingDate") or []
        accession = recent.get("accessionNumber") or []
        docs = recent.get("primaryDocument") or []
        for form, day, acc, doc in zip(forms, days, accession, docs):
            try:
                filing_day = date.fromisoformat(day)
            except Exception:
                continue
            if form not in forms_allowed or filing_day < cutoff:
                continue
            acc_flat = str(acc).replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc_flat}/{doc}"
            events.append(make_event(signal, str(acc), f"{company}提交{form}", f"SEC filing {form}, accession {acc}", url, 0,
                                     "P2" if form in {"10-Q", "10-K"} else "P3", day))
    return events


def usda_event(signal: dict[str, Any], prior_state: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    url = signal["source_url"]
    soup = BeautifulSoup(request(url).text, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    old = prior_state.get(signal["id"])
    events: list[dict[str, Any]] = []
    if old and old != digest:
        events.append(make_event(signal, digest[:16], "USDA WASDE页面出现更新", "需比较小麦、粗粮、油籽、糖和棉花的产量及库存消费比修正。", url, 0, "P2"))
    elif not old:
        events.append(make_event(signal, digest[:16], "USDA WASDE监控建立基线", "首次运行仅建立页面基线，不视为利好或利空。", url, 0, "P3"))
    return events, digest


def merge_numeric(new_rows: list[dict[str, Any]]) -> pd.DataFrame:
    columns = ["signal_id", "date", "value", "provider", "source_url", "fetched_at"]
    old = pd.read_csv(NUMERIC_HISTORY, dtype={"signal_id": str, "date": str}) if NUMERIC_HISTORY.exists() else pd.DataFrame(columns=columns)
    new = pd.DataFrame(new_rows)
    merged = pd.concat([old, new], ignore_index=True) if not new.empty else old
    if not merged.empty:
        merged["value"] = pd.to_numeric(merged["value"], errors="coerce")
        merged = merged.dropna(subset=["signal_id", "date", "value"])
        merged = merged.sort_values(["signal_id", "date", "fetched_at"]).drop_duplicates(["signal_id", "date"], keep="last")
    merged.to_csv(NUMERIC_HISTORY, index=False, encoding="utf-8-sig")
    return merged


def merge_events(new_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    if EVENT_HISTORY.exists():
        for line in EVENT_HISTORY.read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
                existing[row["event_key"]] = row
            except Exception:
                continue
    for event in new_events:
        existing[event["event_key"]] = event
    rows = sorted(existing.values(), key=lambda x: (x.get("date", ""), x.get("event_key", "")))
    atomic_write(EVENT_HISTORY, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return rows


def step_change(values: pd.Series, steps: int, absolute: bool = False) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if len(clean) <= steps:
        return float("nan")
    current, previous = float(clean.iloc[-1]), float(clean.iloc[-1 - steps])
    return current - previous if absolute else pct_change(current, previous)


def evaluate_numeric(signal: dict[str, Any], rows: pd.DataFrame) -> dict[str, Any]:
    rows = rows.sort_values("date").drop_duplicates("date", keep="last")
    values = pd.to_numeric(rows["value"], errors="coerce").dropna()
    latest = float(values.iloc[-1])
    pct1, pct5 = step_change(values, 1), step_change(values, min(5, max(len(values) - 1, 1)))
    abs1, abs5 = step_change(values, 1, True), step_change(values, min(5, max(len(values) - 1, 1)), True)
    hits: list[dict[str, Any]] = []
    thresholds = signal.get("thresholds") or {}

    def threshold_hit(metric: str, value: float, spec: dict[str, Any], signed_level: bool = False) -> None:
        if not finite(value):
            return
        magnitude = value if signed_level else abs(value)
        p1, p2 = num(spec.get("p1")), num(spec.get("p2"))
        if finite(p1) and magnitude >= p1:
            hits.append({"metric": metric, "value": value, "level": "P1"})
        elif finite(p2) and magnitude >= p2:
            hits.append({"metric": metric, "value": value, "level": "P2"})

    if "pct_1" in thresholds: threshold_hit("单周期涨跌", pct1, thresholds["pct_1"])
    if "pct_5" in thresholds: threshold_hit("五周期涨跌", pct5, thresholds["pct_5"])
    if "abs_1" in thresholds: threshold_hit("单周期绝对变化", abs1, thresholds["abs_1"])
    if "abs_5" in thresholds: threshold_hit("五周期绝对变化", abs5, thresholds["abs_5"])
    if "level" in thresholds: threshold_hit("绝对水平", latest, thresholds["level"], True)

    lookback = int(thresholds.get("breakout") or 0)
    breakout = breakdown = False
    if lookback >= 3 and len(values) > lookback:
        prior = values.iloc[-lookback - 1:-1]
        breakout, breakdown = latest > float(prior.max()), latest < float(prior.min())
        if breakout or breakdown:
            hits.append({"metric": f"{lookback}周期新{'高' if breakout else '低'}", "value": latest, "level": "P2"})

    level = max((h["level"] for h in hits), key=lambda x: LEVEL_NUM[x], default="NONE")
    base_change = pct1 if finite(pct1) else pct5 if finite(pct5) else abs1
    trend = 1 if finite(base_change) and base_change > 0 else -1 if finite(base_change) and base_change < 0 else 0
    if "level" in thresholds and trend == 0:
        trend = 1
    last = rows.iloc[-1]
    return {
        "signal_id": signal["id"], "name": signal["name"], "group": signal["group"], "type": "numeric",
        "latest": latest, "latest_date": str(last["date"]), "pct_1": pct1, "pct_5": pct5,
        "abs_1": abs1, "abs_5": abs5, "breakout": breakout, "breakdown": breakdown,
        "level": level, "severity": LEVEL_NUM[level], "trend_sign": trend, "hits": hits,
        "provider": str(last.get("provider", "")), "source_url": str(last.get("source_url", "")),
        "source_tier": signal.get("source_tier", ""), "frequency": signal.get("frequency", ""),
    }


def evaluate_events(signal: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    cutoff = NOW.date() - timedelta(days=8)
    recent = []
    for event in events:
        try:
            if date.fromisoformat(str(event.get("date", ""))[:10]) < cutoff:
                continue
        except Exception:
            pass
        recent.append(event)
    level = max((e.get("level", "P3") for e in recent), key=lambda x: LEVEL_NUM.get(x, 0), default="NONE")
    polarity = sum(int(e.get("polarity") or 0) for e in recent)
    return {
        "signal_id": signal["id"], "name": signal["name"], "group": signal["group"], "type": "event",
        "latest": None, "latest_date": max((str(e.get("date", "")) for e in recent), default=""),
        "level": level, "severity": LEVEL_NUM.get(level, 0),
        "trend_sign": 1 if polarity > 0 else -1 if polarity < 0 else 0,
        "event_count": len(recent), "events": recent[:20], "source_url": signal.get("source_url", ""),
        "source_tier": signal.get("source_tier", ""), "frequency": signal.get("frequency", ""),
    }


def map_stocks(results: dict[str, dict[str, Any]], mapping: pd.DataFrame) -> list[dict[str, Any]]:
    stocks: dict[str, dict[str, Any]] = {}
    for _, link in mapping.iterrows():
        result = results.get(str(link["signal_id"]))
        if not result or result.get("severity", 0) <= 0:
            continue
        code = str(link["security_code"]).zfill(6)
        item = stocks.setdefault(code, {
            "security_code": code, "security_name": str(link["security_name"]), "score": 0.0,
            "positive": 0.0, "negative": 0.0, "mixed": 0.0, "signals": [],
            "in_final20": bool(int(link.get("in_final20", 0))),
        })
        direction = int(link["direction"])
        trend = int(result.get("trend_sign") or 0)
        raw = float(result["severity"]) * float(link["weight"])
        signed = raw * direction * trend if direction and trend else 0.0
        item["score"] += signed
        if signed > 0: item["positive"] += signed
        elif signed < 0: item["negative"] += abs(signed)
        else: item["mixed"] += raw
        item["signals"].append({
            "signal_id": str(link["signal_id"]), "signal_name": result["name"], "level": result["level"],
            "impact": round(signed, 3), "weight": float(link["weight"]), "directness": str(link["directness"]),
            "rationale": str(link["rationale"]),
        })
    return sorted(stocks.values(), key=lambda x: abs(x["score"]) + x["mixed"], reverse=True)


def fmt(value: Any) -> str:
    return "—" if not finite(value) else f"{float(value):,.2f}"


def build_report(catalog: dict[str, Any], results: dict[str, dict[str, Any]], stocks: list[dict[str, Any]],
                 final20: pd.DataFrame, mapping: pd.DataFrame, errors: list[dict[str, Any]]) -> str:
    active = sorted([r for r in results.values() if r.get("severity", 0) > 0],
                    key=lambda x: (x["severity"], abs(x.get("pct_1") or 0), x.get("event_count", 0)), reverse=True)
    lines = [
        f"# A股海外产业信号雷达｜{NOW:%Y-%m-%d %H:%M}（北京时间）", "",
        "> 海外信号只负责提前发现变化，不直接生成买卖指令。必须再用公司公告、季度扣非利润、经营现金流或相对强弱交叉验证。", "",
        "## 今日摘要", "",
        f"- 触发P1：{sum(r['level']=='P1' for r in active)}条；P2：{sum(r['level']=='P2' for r in active)}条；P3：{sum(r['level']=='P3' for r in active)}条。",
        f"- 数据源成功：{len(results)}条；失败：{len(errors)}条。",
    ]
    if active:
        lines.append("- 最强信号：" + "；".join(f"{r['name']}（{r['level']}）" for r in active[:5]) + "。")
    lines += ["", "## 触发信号", "", "|级别|信号|最新值/事件|单周期|五周期|方向|来源|", "|---|---|---:|---:|---:|---|---|"]
    for r in active:
        if r["type"] == "numeric":
            latest, one, five = fmt(r["latest"]), fmt(r.get("pct_1")) + "%", fmt(r.get("pct_5")) + "%"
        else:
            latest, one, five = f"{r.get('event_count',0)}条事件", "—", "—"
        direction = "上行" if r.get("trend_sign") == 1 else "下行" if r.get("trend_sign") == -1 else "待判定"
        lines.append(f"|{r['level']}|{r['name']}|{latest}|{one}|{five}|{direction}|{r.get('provider') or r.get('source_tier','')}|")
    if not active: lines.append("|—|无|—|—|—|—|—|")

    lines += ["", "## A股优先复核", "", "|代码|公司|方向分|正向|负向|待判定|主要触发|最终20|", "|---|---|---:|---:|---:|---:|---|---|"]
    for stock in stocks[:30]:
        major = "；".join(f"{s['signal_name']}({s['level']}/{s['directness']})" for s in sorted(stock["signals"], key=lambda x: abs(x["impact"]) + x["weight"], reverse=True)[:3])
        lines.append(f"|{stock['security_code']}|{stock['security_name']}|{stock['score']:.2f}|{stock['positive']:.2f}|{stock['negative']:.2f}|{stock['mixed']:.2f}|{major}|{'是' if stock['in_final20'] else '否'}|")
    if not stocks: lines.append("|—|无|—|—|—|—|—|—|")

    signal_names = {s["id"]: s["name"] for s in catalog["signals"]}
    links_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for _, row in mapping[mapping["in_final20"] == 1].iterrows():
        links_by_code[str(row["security_code"]).zfill(6)].append(row.to_dict())
    lines += ["", "## 最终20只的海外敏感度", "", "|代码|公司|敏感度|重点变量|说明|", "|---|---|---|---|---|"]
    for _, row in final20.iterrows():
        code = str(row["security_code"]).zfill(6)
        links = sorted(links_by_code.get(code, []), key=lambda x: float(x["weight"]), reverse=True)
        names = "、".join(signal_names.get(str(x["signal_id"]), str(x["signal_id"])) for x in links[:4]) or "无强直接变量"
        lines.append(f"|{code}|{row['security_name']}|{row['overseas_sensitivity']}|{names}|{row.get('note','')}|")

    lines += ["", "## 新事件", ""]
    count = 0
    for r in active:
        if r["type"] != "event": continue
        for event in r.get("events", [])[:10]:
            count += 1
            polarity = "正向" if event.get("polarity") == 1 else "负向" if event.get("polarity") == -1 else "待判定"
            lines.append(f"- **{r['name']}｜{event.get('date','')}｜{polarity}**：{event.get('title','')}  \n  {event.get('summary','')[:400]}  \n  来源：{event.get('source_url','')}")
    if count == 0: lines.append("- 无新增重大事件。")

    lines += ["", "## 数据健康", ""]
    for error in errors:
        lines.append(f"- `{error['signal_id']}`：{error['error']}")
    if not errors: lines.append("- 所有配置数据源均正常。")
    lines += ["", "## 执行纪律", "",
              "1. P1只表示开盘前必须复核，不表示无条件追涨。",
              "2. A类映射优先；B类需要合同、套保和产品结构确认；C类只作宏观或价格确认。",
              "3. 商品必须看利润差：铝价同时看氧化铝和电力，油价同时看炼化价差，运价同时看船型与实际TCE。",
              "4. 同一信号连续增强但公司不再跑赢行业，可能已经定价；基本面增强而价格尚未反应，才可能形成预期差。"]
    return "\n".join(lines) + "\n"


def build_alert(results: dict[str, dict[str, Any]], stocks: list[dict[str, Any]], errors: list[dict[str, Any]]) -> dict[str, Any]:
    active = sorted([r for r in results.values() if r.get("level") in {"P1", "P2"}], key=lambda x: x["severity"], reverse=True)
    highest = max((r["level"] for r in active), key=lambda x: LEVEL_NUM[x], default="NONE")
    fp = fingerprint([NOW.date().isoformat(), [(r["signal_id"], r["level"], r.get("latest_date"), r.get("latest")) for r in active], [e["signal_id"] for e in errors]])
    names = " / ".join(r["name"] for r in active[:3]) or "数据源健康告警"
    body = [f"自动监控时间：{NOW:%Y-%m-%d %H:%M}（北京时间）", "", f"最高等级：**{highest}**", "", "重点A股映射："]
    for stock in stocks[:10]:
        body.append(f"- {stock['security_code']} {stock['security_name']}：方向分{stock['score']:.2f}；" + "、".join(s["signal_name"] for s in stock["signals"][:3]))
    body += ["", "完整报告：`reports/global_signals/latest.md`", "", f"<!-- global-signal-fingerprint:{fp} -->"]
    return {
        "generated_at": NOW.isoformat(), "issue_required": bool(active) or len(errors) >= 10,
        "max_level": highest, "title": f"[海外产业信号][{highest}] {NOW:%Y-%m-%d} {names}"[:180],
        "body": "\n".join(body), "fingerprint": fp, "active_signal_count": len(active),
    }


def main() -> int:
    ensure_dirs()
    catalog = load_json(CATALOG, None)
    if not catalog:
        raise FileNotFoundError(CATALOG)
    mapping = pd.read_csv(STOCK_MAP, dtype={"security_code": str})
    final20 = pd.read_csv(FINAL20, dtype={"security_code": str})
    state = load_json(STATE_FILE, {})
    event_state = state.get("event_state") or {}
    page_state = state.get("page_state") or {}
    new_event_state, new_page_state = dict(event_state), dict(page_state)
    numeric_rows: list[dict[str, Any]] = []
    new_events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        baltic_cache = fetch_baltic()
    except Exception as exc:  # noqa: BLE001
        baltic_cache, baltic_error = {}, str(exc)
    else:
        baltic_error = ""

    for signal in catalog["signals"]:
        sid, provider = signal["id"], signal["provider"]
        log(f"fetch {sid} via {provider}")
        try:
            observations: list[dict[str, Any]] = []
            events: list[dict[str, Any]] = []
            if provider == "baltic":
                observations = baltic_cache.get(sid, [])
                if not observations: raise RuntimeError(baltic_error or f"{sid} absent from Baltic page")
            elif provider == "fred": observations = fetch_fred(signal["series"])
            elif provider == "yahoo": observations = fetch_yahoo(signal["ticker"])
            elif provider == "tsmc": observations = fetch_tsmc(signal["source_url"])
            elif provider == "sia": observations = fetch_sia()
            elif provider == "acea": observations = fetch_acea()
            elif provider == "clinicaltrials":
                events, snapshot = clinical_events(signal, event_state)
                new_event_state[sid] = snapshot
            elif provider == "federal_register": events = federal_register_events(signal)
            elif provider == "sec": events = sec_events(signal)
            elif provider == "usda":
                events, digest = usda_event(signal, page_state)
                new_page_state[sid] = digest
            else: raise ValueError(f"unknown provider {provider}")
            for row in observations:
                numeric_rows.append({"signal_id": sid, "date": row["date"], "value": row["value"],
                                     "provider": row.get("provider", provider), "source_url": row.get("source_url", signal.get("source_url", "")),
                                     "fetched_at": NOW.isoformat()})
            new_events.extend(events)
        except Exception as exc:  # noqa: BLE001
            errors.append({"signal_id": sid, "provider": provider, "error": str(exc)})
            log(f"ERROR {sid}: {exc}")

    history = merge_numeric(numeric_rows)
    events = merge_events(new_events)
    results: dict[str, dict[str, Any]] = {}
    for signal in catalog["signals"]:
        sid = signal["id"]
        if signal["provider"] in {"clinicaltrials", "federal_register", "sec", "usda"}:
            results[sid] = evaluate_events(signal, [e for e in events if e.get("signal_id") == sid])
        else:
            rows = history[history["signal_id"] == sid]
            if not rows.empty: results[sid] = evaluate_numeric(signal, rows)

    stocks = map_stocks(results, mapping)
    report = build_report(catalog, results, stocks, final20, mapping, errors)
    dated = REPORT_DIR / f"{NOW:%Y-%m-%d}-{RUN_SLOT}.md"
    atomic_write(dated, report)
    atomic_write(REPORT_DIR / "latest.md", report)
    latest = {
        "generated_at": NOW.isoformat(), "run_slot": RUN_SLOT, "catalog_version": catalog.get("version"),
        "signal_results": results, "stock_results": stocks, "errors": errors,
        "data_health": {"configured": len(catalog["signals"]), "successful": len(results), "failed": len(errors)},
    }
    write_json(LATEST_FILE, latest)
    alert = build_alert(results, stocks, errors)
    write_json(ALERT_FILE, alert)
    write_json(STATE_FILE, {"updated_at": NOW.isoformat(), "event_state": new_event_state, "page_state": new_page_state})
    log(json.dumps({"configured": len(catalog["signals"]), "successful": len(results), "failed": len(errors),
                    "active": alert["active_signal_count"], "max_level": alert["max_level"], "report": str(dated.relative_to(ROOT))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
