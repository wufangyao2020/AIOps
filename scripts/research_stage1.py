#!/usr/bin/env python3
"""Stage 1: enrich and blindly score the 5,550-company 2026H1 A-share universe.

The candidate generator intentionally ignores prior conversation frequency.  It
fetches comparable quarterly statements, current market data, research-report
coverage and public capital-event announcements, then runs four separate models:

1. fundamental acceleration;
2. new profit-pool migration proxy;
3. cyclical turn;
4. capital events.

Stage 1 does not make a final investment recommendation.  It creates an audited
100-company review pool for announcement reading and peer comparison in stage 2.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
PERIOD = "2026H1"
DATA_DIR = ROOT / "data" / PERIOD
RESEARCH_DIR = DATA_DIR / "research"
RAW_DIR = RESEARCH_DIR / "raw"
MODEL_DIR = RESEARCH_DIR / "models"
META_DIR = ROOT / "meta"
MASTER = DATA_DIR / "a_share_2026_h1_5550_master.csv"

DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
QUOTE_API = "https://82.push2.eastmoney.com/api/qt/clist/get"
REPORT_API = "https://reportapi.eastmoney.com/report/list"
NOTICE_API = "https://np-anotice-stock.eastmoney.com/api/security/ann"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://data.eastmoney.com/",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

PERFORMANCE_PERIODS = {
    "2024h1": "2024-06-30",
    "2024fy": "2024-12-31",
    "2025q1": "2025-03-31",
    "2025h1": "2025-06-30",
    "2025fy": "2025-12-31",
    "2026q1": "2026-03-31",
    "2026h1": "2026-06-30",
}
STATEMENT_PERIODS = {
    "2025q1": "2025-03-31",
    "2025h1": "2025-06-30",
    "2026q1": "2026-03-31",
    "2026h1": "2026-06-30",
}

CYCLICAL_KEYWORDS = (
    "煤炭", "石油", "天然气", "油气", "有色", "贵金属", "工业金属", "小金属", "钢铁", "普钢",
    "特钢", "化学原料", "化学制品", "化纤", "塑料", "橡胶", "水泥", "玻璃玻纤", "造纸",
    "航运", "港口", "航空机场", "养殖", "种植", "农产品加工", "饲料", "半导体材料", "面板",
    "光伏设备", "电池", "房地产", "装修建材", "工程机械", "通用设备", "专用设备",
)

POSITIVE_EVENT_RULES: list[tuple[str, float]] = [
    (r"实际控制人.*变更|控制权.*变更|权益变动.*控制权", 22),
    (r"重大资产重组|发行股份购买资产|资产注入|借壳", 20),
    (r"收购.*股权|购买资产|并购", 12),
    (r"出售.*资产|剥离.*业务|处置.*低效", 8),
    (r"回购.*注销|注销.*回购", 12),
    (r"回购股份", 6),
    (r"增持计划|董事长.*增持|实际控制人.*增持", 6),
    (r"股权激励|员工持股计划", 8),
    (r"重大合同|中标通知|项目中标|订单", 10),
    (r"投产|量产|通车|试生产|竣工", 9),
    (r"定向增发|向特定对象发行.*获批|注册批复", 6),
    (r"上调.*业绩|业绩预增|扭亏为盈", 5),
]
NEGATIVE_EVENT_RULES: list[tuple[str, float]] = [
    (r"终止.*重组|终止.*收购|终止.*发行|项目终止", -18),
    (r"立案调查|涉嫌违法|行政处罚", -25),
    (r"退市风险|终止上市|暂停上市", -30),
    (r"监管问询|关注函|问询函", -6),
    (r"减持计划|减持股份", -8),
    (r"股份质押|质押展期", -5),
    (r"重大诉讼|重大仲裁", -7),
    (r"债务逾期|无法清偿|资金占用", -20),
    (r"业绩预减|预计亏损|由盈转亏", -8),
]

MODEL_NAMES = [
    "fundamental_acceleration",
    "new_profit_pool",
    "cyclical_turn",
    "capital_event",
]


def ensure_dirs() -> None:
    for p in (RESEARCH_DIR, RAW_DIR, MODEL_DIR, META_DIR):
        p.mkdir(parents=True, exist_ok=True)


def to_num(value: Any) -> float:
    if value is None or value == "" or value == "-":
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def safe_div(a: Any, b: Any) -> float:
    x, y = to_num(a), to_num(b)
    if not np.isfinite(x) or not np.isfinite(y) or abs(y) < 1e-9:
        return float("nan")
    return x / y


def pct_change(current: Any, prior: Any) -> float:
    x, y = to_num(current), to_num(prior)
    if not np.isfinite(x) or not np.isfinite(y) or abs(y) < 1e-9:
        return float("nan")
    return (x / abs(y) - 1.0) * 100.0


def clip(value: Any, low: float, high: float) -> float:
    x = to_num(value)
    if not np.isfinite(x):
        return float("nan")
    return float(min(max(x, low), high))


def json_dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def request_json(url: str, params: dict[str, Any], attempts: int = 6, timeout: int = 45) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(1.5 ** attempt, 12))
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last}")


def fetch_datacenter(report_name: str, date_field: str, report_date: str) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "sortColumns": "NOTICE_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1",
        "pageSize": "500",
        "pageNumber": "1",
        "reportName": report_name,
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "filter": f"({date_field}='{report_date}')",
    }
    first = request_json(DATACENTER, params)
    result = first.get("result") or {}
    pages = int(result.get("pages") or 1)
    rows: list[dict[str, Any]] = list(result.get("data") or [])
    for page in range(2, pages + 1):
        params["pageNumber"] = str(page)
        payload = request_json(DATACENTER, params)
        rows.extend((payload.get("result") or {}).get("data") or [])
        time.sleep(0.04)
    return rows


def latest_by_code(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("SECURITY_CODE") or "").strip()
        if not (len(code) == 6 and code.isdigit()):
            continue
        stamp = str(row.get("UPDATE_DATE") or row.get("NOTICE_DATE") or "")
        old = out.get(code)
        if old is None or stamp > str(old.get("UPDATE_DATE") or old.get("NOTICE_DATE") or ""):
            out[code] = row
    return out


def fetch_quote_snapshot() -> pd.DataFrame:
    params: dict[str, Any] = {
        "pn": "1", "pz": "500", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
        "fid": "f12",
        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
        "fields": "f2,f3,f5,f6,f8,f9,f10,f12,f13,f14,f20,f21,f23,f24,f25,f100",
    }
    rows: list[dict[str, Any]] = []
    page = 1
    total = None
    while True:
        params["pn"] = str(page)
        payload = request_json(QUOTE_API, params)
        data = payload.get("data") or {}
        diff = data.get("diff") or []
        rows.extend(diff)
        total = int(data.get("total") or len(rows))
        if not diff or len(rows) >= total:
            break
        page += 1
        if page > 30:
            break
    mapping = {
        "f12": "security_code", "f14": "quote_name", "f2": "price", "f3": "daily_return_pct",
        "f5": "volume", "f6": "turnover_amount_cny", "f8": "turnover_rate_pct", "f9": "pe_dynamic",
        "f10": "volume_ratio", "f20": "market_cap_cny", "f21": "float_market_cap_cny", "f23": "pb",
        "f24": "return_60d_pct", "f25": "return_ytd_pct", "f100": "quote_industry",
    }
    frame = pd.DataFrame(rows).rename(columns=mapping)
    keep = [c for c in mapping.values() if c in frame.columns]
    frame = frame[keep].copy()
    frame["security_code"] = frame["security_code"].astype(str).str.zfill(6)
    for col in [c for c in keep if c not in {"security_code", "quote_name", "quote_industry"}]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.drop_duplicates("security_code", keep="last")


def fetch_research_reports() -> pd.DataFrame:
    params: dict[str, Any] = {
        "industryCode": "*", "pageSize": "5000", "industry": "*", "rating": "*",
        "ratingChange": "*", "beginTime": "2025-09-01", "endTime": "2026-09-03",
        "pageNo": "1", "fields": "", "qType": "0", "orgCode": "", "code": "", "rcode": "",
        "p": "1", "pageNum": "1", "pageNumber": "1",
    }
    rows: list[dict[str, Any]] = []
    first = request_json(REPORT_API, params)
    pages = int(first.get("TotalPage") or 1)
    rows.extend(first.get("data") or [])
    for page in range(2, min(pages, 80) + 1):
        for key in ("pageNo", "p", "pageNum", "pageNumber"):
            params[key] = str(page)
        payload = request_json(REPORT_API, params)
        rows.extend(payload.get("data") or [])
        time.sleep(0.04)
    if not rows:
        return pd.DataFrame(columns=["security_code", "report_count_12m", "report_org_count_12m"])
    df = pd.DataFrame(rows)
    if "stockCode" not in df:
        return pd.DataFrame(columns=["security_code", "report_count_12m", "report_org_count_12m"])
    df["security_code"] = df["stockCode"].astype(str).str.zfill(6)
    df["publishDate"] = pd.to_datetime(df.get("publishDate"), errors="coerce")
    grouped = df.groupby("security_code", dropna=False).agg(
        report_count_12m=("security_code", "size"),
        report_org_count_12m=("orgSName", lambda s: s.dropna().nunique()),
        latest_report_date=("publishDate", "max"),
        latest_report_title=("title", lambda s: s.dropna().iloc[0] if not s.dropna().empty else ""),
        latest_rating=("emRatingName", lambda s: s.dropna().iloc[0] if not s.dropna().empty else ""),
        latest_eps_2026=("predictThisYearEps", lambda s: pd.to_numeric(s, errors="coerce").dropna().iloc[0] if not pd.to_numeric(s, errors="coerce").dropna().empty else np.nan),
        latest_eps_2027=("predictNextYearEps", lambda s: pd.to_numeric(s, errors="coerce").dropna().iloc[0] if not pd.to_numeric(s, errors="coerce").dropna().empty else np.nan),
    ).reset_index()
    return grouped


def fetch_event_announcements(begin: str = "2026-01-01", end: str = "2026-09-02") -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for f_node in (2, 4, 5, 6, 7):
        params: dict[str, Any] = {
            "sr": "-1", "page_size": "100", "page_index": "1", "ann_type": "A",
            "client_source": "web", "f_node": str(f_node), "s_node": "0",
            "begin_time": begin, "end_time": end,
        }
        first = request_json(NOTICE_API, params)
        data = first.get("data") or {}
        hits = int(data.get("total_hits") or 0)
        pages = int(math.ceil(hits / 100.0)) if hits else 0
        batches = [data.get("list") or []]
        for page in range(2, pages + 1):
            params["page_index"] = str(page)
            payload = request_json(NOTICE_API, params)
            batches.append((payload.get("data") or {}).get("list") or [])
            time.sleep(0.035)
        for batch in batches:
            for item in batch:
                code = ""
                name = ""
                for c in item.get("codes") or []:
                    candidate = str(c.get("stock_code") or "")
                    if len(candidate) == 6 and candidate.isdigit() and str(c.get("ann_type") or "").startswith("A"):
                        code = candidate
                        name = str(c.get("short_name") or "")
                        break
                if not code:
                    continue
                columns = item.get("columns") or []
                rows.append({
                    "security_code": code,
                    "security_name": name,
                    "notice_date": str(item.get("notice_date") or "")[:10],
                    "title": str(item.get("title") or ""),
                    "art_code": str(item.get("art_code") or ""),
                    "category": str(columns[0].get("column_name") if columns else ""),
                    "f_node": f_node,
                })
    if not rows:
        return pd.DataFrame(columns=["security_code", "notice_date", "title", "art_code", "category"])
    df = pd.DataFrame(rows).drop_duplicates(["security_code", "art_code"])
    df["notice_date"] = pd.to_datetime(df["notice_date"], errors="coerce")
    return df


def aggregate_events(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame(columns=["security_code", "event_score_raw", "event_positive_count", "event_negative_count", "event_titles"])
    today = pd.Timestamp("2026-09-02")
    records: list[dict[str, Any]] = []
    for code, group in events.groupby("security_code"):
        score = 0.0
        pos = 0
        neg = 0
        matched: list[str] = []
        for row in group.sort_values("notice_date", ascending=False).itertuples(index=False):
            title = str(row.title)
            age = max((today - row.notice_date).days if pd.notna(row.notice_date) else 365, 0)
            recency = max(0.35, 1.0 - age / 500.0)
            item_score = 0.0
            for pattern, weight in POSITIVE_EVENT_RULES:
                if re.search(pattern, title):
                    item_score += weight
            for pattern, weight in NEGATIVE_EVENT_RULES:
                if re.search(pattern, title):
                    item_score += weight
            if item_score > 0:
                pos += 1
                matched.append(f"+{item_score:.0f}|{str(row.notice_date)[:10]}|{title}")
            elif item_score < 0:
                neg += 1
                matched.append(f"{item_score:.0f}|{str(row.notice_date)[:10]}|{title}")
            score += item_score * recency
        records.append({
            "security_code": code,
            "event_score_raw": score,
            "event_positive_count": pos,
            "event_negative_count": neg,
            "event_titles": " || ".join(matched[:12]),
        })
    return pd.DataFrame(records)


def build_period_frame(rows: list[dict[str, Any]], prefix: str, kind: str) -> pd.DataFrame:
    selected = latest_by_code(rows)
    records: list[dict[str, Any]] = []
    for code, r in selected.items():
        base: dict[str, Any] = {"security_code": code}
        if kind == "performance":
            fields = {
                "revenue": "TOTAL_OPERATE_INCOME", "net_profit": "PARENT_NETPROFIT",
                "revenue_yoy": "YSTZ", "net_profit_yoy": "SJLTZ", "roe": "WEIGHTAVG_ROE",
                "eps": "BASIC_EPS", "bps": "BPS", "gross_margin": "XSMLL", "ocf_per_share": "MGJYXJJE",
                "industry_report": "INDUSTRY_NAME",
            }
        elif kind == "income":
            fields = {
                "operating_cost": "OPERATE_COST", "operating_profit": "OPERATE_PROFIT",
                "total_profit": "TOTAL_PROFIT", "deduct_net_profit": "DEDUCT_PARENT_NETPROFIT",
                "sale_expense": "SALE_EXPENSE", "manage_expense": "MANAGE_EXPENSE",
                "finance_expense": "FINANCE_EXPENSE", "income_tax": "INCOME_TAX",
            }
        elif kind == "balance":
            fields = {
                "total_assets": "TOTAL_ASSETS", "total_equity": "TOTAL_EQUITY",
                "total_liabilities": "TOTAL_LIABILITIES", "inventory": "INVENTORY",
                "accounts_receivable": "ACCOUNTS_RECE", "monetary_funds": "MONETARYFUNDS",
                "fixed_assets": "FIXED_ASSET", "goodwill": "GOODWILL",
                "current_assets": "TOTAL_CURRENT_ASSETS", "current_liabilities": "TOTAL_CURRENT_LIAB",
                "short_loan": "SHORT_LOAN", "long_loan": "LONG_LOAN", "contract_liabilities": "CONTRACT_LIAB",
                "construction_in_progress": "CONSTRUCTION_IN_PROGRESS",
            }
        elif kind == "cashflow":
            fields = {
                "ocf": "NETCASH_OPERATE", "investing_cf": "NETCASH_INVEST", "financing_cf": "NETCASH_FINANCE",
                "capex": "CONSTRUCT_LONG_ASSET", "ending_cash": "END_CCE",
            }
        else:
            raise ValueError(kind)
        for dst, src in fields.items():
            val = r.get(src)
            base[f"{prefix}_{dst}"] = str(val or "") if dst.startswith("industry") else to_num(val)
        records.append(base)
    return pd.DataFrame(records)


def percentile(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    rank = s.rank(pct=True, method="average")
    if not higher_is_better:
        rank = 1.0 - rank
    return rank.fillna(0.35).clip(0, 1)


def group_percentile(df: pd.DataFrame, col: str, higher_is_better: bool = True) -> pd.Series:
    s = pd.to_numeric(df[col], errors="coerce") if col in df else pd.Series(np.nan, index=df.index)
    out = pd.Series(index=df.index, dtype=float)
    for _, idx in df.groupby("industry", dropna=False).groups.items():
        vals = s.loc[idx]
        if vals.notna().sum() >= 5:
            ranks = vals.rank(pct=True, method="average")
            if not higher_is_better:
                ranks = 1.0 - ranks
            out.loc[idx] = ranks
    fallback = percentile(s, higher_is_better)
    return out.fillna(fallback).fillna(0.35).clip(0, 1)


def size_score(market_cap_cny: pd.Series) -> pd.Series:
    bn = pd.to_numeric(market_cap_cny, errors="coerce") / 1e8
    score = pd.Series(0.25, index=bn.index, dtype=float)
    score = score.where(~((bn >= 20) & (bn < 40)), 0.60)
    score = score.where(~((bn >= 40) & (bn < 80)), 0.85)
    score = score.where(~((bn >= 80) & (bn <= 250)), 1.00)
    score = score.where(~((bn > 250) & (bn <= 500)), 0.80)
    score = score.where(~((bn > 500) & (bn <= 1000)), 0.55)
    score = score.where(~(bn > 1000), 0.25)
    score = score.where(~(bn < 20), 0.35)
    return score.fillna(0.25)


def calculate_metrics(df: pd.DataFrame) -> pd.DataFrame:
    # Single-quarter values derived from cumulative Q1/H1 statements.
    for metric in ("revenue", "net_profit"):
        df[f"2026q2_{metric}"] = df[f"2026h1_{metric}"] - df[f"2026q1_{metric}"]
        df[f"2025q2_{metric}"] = df[f"2025h1_{metric}"] - df[f"2025q1_{metric}"]
        df[f"q2_{metric}_yoy_pct"] = (df[f"2026q2_{metric}"] / df[f"2025q2_{metric}"].abs() - 1) * 100
        df.loc[df[f"2025q2_{metric}"].abs() < 5e6, f"q2_{metric}_yoy_pct"] = np.nan
    for metric in ("deduct_net_profit", "ocf", "capex"):
        df[f"2026q2_{metric}"] = df.get(f"2026h1_{metric}") - df.get(f"2026q1_{metric}")
        df[f"2025q2_{metric}"] = df.get(f"2025h1_{metric}") - df.get(f"2025q1_{metric}")
        df[f"q2_{metric}_yoy_pct"] = (df[f"2026q2_{metric}"] / df[f"2025q2_{metric}"].abs() - 1) * 100
        df.loc[df[f"2025q2_{metric}"].abs() < 5e6, f"q2_{metric}_yoy_pct"] = np.nan

    df["q1_revenue_yoy_pct_calc"] = (df["2026q1_revenue"] / df["2025q1_revenue"].abs() - 1) * 100
    df["q1_net_profit_yoy_pct_calc"] = (df["2026q1_net_profit"] / df["2025q1_net_profit"].abs() - 1) * 100
    df.loc[df["2025q1_net_profit"].abs() < 5e6, "q1_net_profit_yoy_pct_calc"] = np.nan
    df["revenue_acceleration_pp"] = df["q2_revenue_yoy_pct"] - df["q1_revenue_yoy_pct_calc"]
    df["profit_acceleration_pp"] = df["q2_net_profit_yoy_pct"] - df["q1_net_profit_yoy_pct_calc"]

    for p in ("2025h1", "2026h1"):
        df[f"{p}_gross_margin_calc_pct"] = (1 - df[f"{p}_operating_cost"] / df[f"{p}_revenue"]) * 100
        df[f"{p}_net_margin_pct"] = df[f"{p}_net_profit"] / df[f"{p}_revenue"] * 100
        df[f"{p}_deduct_margin_pct"] = df[f"{p}_deduct_net_profit"] / df[f"{p}_revenue"] * 100
    df["gross_margin_delta_pp"] = df["2026h1_gross_margin_calc_pct"] - df["2025h1_gross_margin_calc_pct"]
    df["net_margin_delta_pp"] = df["2026h1_net_margin_pct"] - df["2025h1_net_margin_pct"]
    df["deduct_profit_yoy_pct"] = (df["2026h1_deduct_net_profit"] / df["2025h1_deduct_net_profit"].abs() - 1) * 100
    df.loc[df["2025h1_deduct_net_profit"].abs() < 1e7, "deduct_profit_yoy_pct"] = np.nan
    df["cash_conversion"] = df["2026h1_ocf"] / df["2026h1_net_profit"].abs()
    df["free_cash_flow_cny"] = df["2026h1_ocf"] - df["2026h1_capex"]
    df["nonrecurring_ratio"] = (df["2026h1_net_profit"] - df["2026h1_deduct_net_profit"]).abs() / df["2026h1_net_profit"].abs()
    df["accounts_receivable_yoy_pct"] = (df["2026h1_accounts_receivable"] / df["2025h1_accounts_receivable"].abs() - 1) * 100
    df["inventory_yoy_pct"] = (df["2026h1_inventory"] / df["2025h1_inventory"].abs() - 1) * 100
    df["fixed_assets_yoy_pct"] = (df["2026h1_fixed_assets"] / df["2025h1_fixed_assets"].abs() - 1) * 100
    df["cip_yoy_pct"] = (df["2026h1_construction_in_progress"] / df["2025h1_construction_in_progress"].abs() - 1) * 100
    df["contract_liabilities_yoy_pct"] = (df["2026h1_contract_liabilities"] / df["2025h1_contract_liabilities"].abs() - 1) * 100
    df["debt_ratio_pct_calc"] = df["2026h1_total_liabilities"] / df["2026h1_total_assets"] * 100
    df["goodwill_equity_ratio"] = df["2026h1_goodwill"] / df["2026h1_total_equity"].abs()
    df["current_ratio"] = df["2026h1_current_assets"] / df["2026h1_current_liabilities"]
    df["interest_bearing_debt_cny"] = df["2026h1_short_loan"].fillna(0) + df["2026h1_long_loan"].fillna(0)
    df["net_cash_cny"] = df["2026h1_monetary_funds"] - df["interest_bearing_debt_cny"]
    df["capex_yoy_pct"] = (df["2026h1_capex"] / df["2025h1_capex"].abs() - 1) * 100
    df["capex_to_revenue_pct"] = df["2026h1_capex"] / df["2026h1_revenue"].abs() * 100
    df["market_cap_100m"] = df["market_cap_cny"] / 1e8
    df["is_st"] = df["security_name"].astype(str).str.contains(r"ST|退", regex=True).astype(int)
    return df.replace([np.inf, -np.inf], np.nan)


def score_models(df: pd.DataFrame) -> pd.DataFrame:
    # Peer-relative percentiles keep giant banks and tiny manufacturers comparable.
    p = {}
    for col, better in {
        "q2_revenue_yoy_pct": True,
        "q2_net_profit_yoy_pct": True,
        "q2_deduct_net_profit_yoy_pct": True,
        "revenue_acceleration_pp": True,
        "profit_acceleration_pp": True,
        "deduct_profit_yoy_pct": True,
        "gross_margin_delta_pp": True,
        "net_margin_delta_pp": True,
        "cash_conversion": True,
        "free_cash_flow_cny": True,
        "2026h1_roe": True,
        "accounts_receivable_yoy_pct": False,
        "inventory_yoy_pct": False,
        "debt_ratio_pct_calc": False,
        "capex_yoy_pct": True,
        "fixed_assets_yoy_pct": True,
        "cip_yoy_pct": True,
        "contract_liabilities_yoy_pct": True,
        "pe_dynamic": False,
        "pb": False,
    }.items():
        p[col] = group_percentile(df, col, better)
        df[f"peer_pct_{col}"] = p[col]

    positive_profit = (df["2026h1_net_profit"] > 1e7).astype(float)
    positive_deduct = (df["2026h1_deduct_net_profit"] > 5e6).astype(float)
    positive_ocf = (df["2026h1_ocf"] > 0).astype(float)
    ar_discipline = (df["accounts_receivable_yoy_pct"] <= df["2026h1_revenue_yoy"] + 10).fillna(False).astype(float)
    inv_discipline = (df["inventory_yoy_pct"] <= df["2026h1_revenue_yoy"] + 15).fillna(False).astype(float)

    fundamental = (
        12 * p["q2_revenue_yoy_pct"] + 18 * p["q2_net_profit_yoy_pct"]
        + 8 * p["q2_deduct_net_profit_yoy_pct"] + 9 * p["revenue_acceleration_pp"]
        + 12 * p["profit_acceleration_pp"] + 8 * p["deduct_profit_yoy_pct"]
        + 9 * p["gross_margin_delta_pp"] + 4 * p["net_margin_delta_pp"]
        + 8 * p["cash_conversion"] + 4 * positive_ocf + 4 * p["2026h1_roe"]
        + 2 * ar_discipline + 2 * inv_discipline
    )
    fundamental -= np.where(df["nonrecurring_ratio"] > 0.35, 12, 0)
    fundamental -= np.where(df["cash_conversion"] < 0, 10, 0)
    fundamental -= np.where((positive_profit + positive_deduct) < 2, 18, 0)
    fundamental -= 25 * df["is_st"]
    df["fundamental_acceleration_score"] = fundamental.clip(0, 100)

    event_pos = percentile(df["event_score_raw"], True)
    capex_signal = (p["capex_yoy_pct"] + p["fixed_assets_yoy_pct"] + p["cip_yoy_pct"]) / 3
    demand_signal = (p["contract_liabilities_yoy_pct"] + p["q2_revenue_yoy_pct"]) / 2
    new_pool = (
        14 * capex_signal + 13 * demand_signal + 13 * p["profit_acceleration_pp"]
        + 12 * p["gross_margin_delta_pp"] + 9 * p["q2_deduct_net_profit_yoy_pct"]
        + 8 * p["cash_conversion"] + 14 * event_pos + 7 * positive_deduct + 5 * positive_ocf
    )
    new_pool -= np.where(df["capex_to_revenue_pct"] > 35, 6, 0)
    new_pool -= np.where(df["goodwill_equity_ratio"] > 0.35, 10, 0)
    new_pool -= np.where(df["nonrecurring_ratio"] > 0.5, 12, 0)
    new_pool -= 20 * df["is_st"]
    df["new_profit_pool_score_stage1"] = new_pool.clip(0, 100)

    cyclical_flag = df["industry"].fillna("").astype(str).apply(lambda x: any(k in x for k in CYCLICAL_KEYWORDS)).astype(float)
    industry_cycle = df.groupby("industry", dropna=False)["gross_margin_delta_pp"].transform("median")
    industry_profit = df.groupby("industry", dropna=False)["q2_net_profit_yoy_pct"].transform("median")
    df["industry_cycle_margin_median"] = industry_cycle
    df["industry_cycle_profit_median"] = industry_profit
    cycle = (
        12 * cyclical_flag + 14 * p["q2_net_profit_yoy_pct"] + 10 * p["profit_acceleration_pp"]
        + 13 * p["gross_margin_delta_pp"] + 9 * p["net_margin_delta_pp"]
        + 12 * p["cash_conversion"] + 7 * positive_ocf + 6 * ar_discipline + 5 * inv_discipline
        + 7 * percentile(industry_cycle, True) + 5 * percentile(industry_profit, True)
    )
    cycle += 5 * p["pe_dynamic"]
    cycle -= np.where(df["return_ytd_pct"] > 120, 10, 0)
    cycle -= np.where(df["cash_conversion"] < 0.5, 8, 0)
    cycle -= np.where(df["debt_ratio_pct_calc"] > 75, 8, 0)
    cycle -= 25 * df["is_st"]
    df["cyclical_turn_score"] = cycle.clip(0, 100)

    capital = (
        55 * percentile(df["event_score_raw"], True)
        + 10 * np.minimum(df["event_positive_count"].fillna(0), 3) / 3
        + 8 * positive_profit + 7 * positive_ocf + 6 * p["cash_conversion"]
        + 6 * size_score(df["market_cap_cny"]) + 8 * p["pe_dynamic"]
    )
    capital -= np.minimum(df["event_negative_count"].fillna(0), 4) * 7
    capital -= np.where(df["goodwill_equity_ratio"] > 0.5, 12, 0)
    capital -= 30 * df["is_st"]
    df["capital_event_score"] = capital.clip(0, 100)

    model_cols = [
        "fundamental_acceleration_score", "new_profit_pool_score_stage1",
        "cyclical_turn_score", "capital_event_score",
    ]
    model_map = dict(zip(model_cols, MODEL_NAMES))
    values = df[model_cols].to_numpy(dtype=float)
    order = np.argsort(values, axis=1)
    df["primary_model_score"] = np.take_along_axis(values, order[:, -1:], axis=1).ravel()
    df["secondary_model_score"] = np.take_along_axis(values, order[:, -2:-1], axis=1).ravel()
    primary_idx = order[:, -1]
    df["primary_model"] = [model_map[model_cols[i]] for i in primary_idx]

    size = size_score(df["market_cap_cny"])
    valuation = (p["pe_dynamic"] * 0.65 + p["pb"] * 0.35)
    coverage = pd.to_numeric(df["report_count_12m"], errors="coerce").fillna(0)
    nonconsensus = (1 - coverage.clip(0, 20) / 20) * 0.7
    nonconsensus += np.where(df["return_ytd_pct"].between(-20, 45, inclusive="both"), 0.3, 0)
    price_confirmation = pd.Series(0.35, index=df.index)
    price_confirmation = np.where(df["return_60d_pct"].between(3, 45, inclusive="both"), 1.0, price_confirmation)
    price_confirmation = np.where(df["return_60d_pct"].between(-8, 3, inclusive="left"), 0.65, price_confirmation)
    price_confirmation = np.where(df["return_60d_pct"] > 90, 0.15, price_confirmation)
    quality_anchor = (
        0.35 * p["cash_conversion"] + 0.25 * p["2026h1_roe"]
        + 0.20 * (1 - p["debt_ratio_pct_calc"]) + 0.10 * ar_discipline + 0.10 * inv_discipline
    )
    risk_penalty = (
        np.where(df["is_st"] == 1, 35, 0)
        + np.where(df["nonrecurring_ratio"] > 0.6, 8, 0)
        + np.where(df["goodwill_equity_ratio"] > 0.6, 8, 0)
        + np.where(df["return_ytd_pct"] > 180, 12, 0)
        + np.where(df["pe_dynamic"] > 150, 8, 0)
    )
    df["convexity_score_stage1"] = (
        0.53 * df["primary_model_score"] + 0.14 * df["secondary_model_score"]
        + 10 * size + 7 * valuation + 7 * nonconsensus + 5 * price_confirmation
        + 8 * quality_anchor - risk_penalty
    ).clip(0, 100)
    df["nonconsensus_proxy_score"] = (100 * nonconsensus).clip(0, 100)
    df["size_convexity_score"] = 100 * size
    df["valuation_score"] = 100 * valuation
    return df


def select_review_pool(df: pd.DataFrame, n: int = 100) -> pd.DataFrame:
    eligible = df[
        (df["is_st"] == 0)
        & (df["market_cap_100m"].between(15, 1500, inclusive="both"))
        & (df["turnover_amount_cny"].fillna(0) >= 8e6)
    ].copy()
    chosen: list[int] = []
    per_model = {
        "fundamental_acceleration": "fundamental_acceleration_score",
        "new_profit_pool": "new_profit_pool_score_stage1",
        "cyclical_turn": "cyclical_turn_score",
        "capital_event": "capital_event_score",
    }
    for model, col in per_model.items():
        block = eligible.sort_values([col, "convexity_score_stage1"], ascending=False).head(28)
        chosen.extend(block.index.tolist())
    seen: set[int] = set()
    ordered: list[int] = []
    for idx in chosen + eligible.sort_values("convexity_score_stage1", ascending=False).index.tolist():
        if idx not in seen:
            seen.add(idx)
            ordered.append(idx)
        if len(ordered) >= n:
            break
    review = eligible.loc[ordered].copy()
    review["stage1_review_rank"] = np.arange(1, len(review) + 1)
    return review.sort_values("stage1_review_rank")


def main() -> int:
    ensure_dirs()
    if not MASTER.exists():
        raise FileNotFoundError(MASTER)
    master = pd.read_csv(MASTER, dtype={"security_code": str}, low_memory=False)
    master["security_code"] = master["security_code"].astype(str).str.zfill(6)
    master = master.rename(columns={
        "revenue_cny": "2026h1_revenue",
        "revenue_yoy_pct": "2026h1_revenue_yoy",
        "parent_net_profit_cny": "2026h1_net_profit",
        "parent_net_profit_yoy_pct": "2026h1_net_profit_yoy",
        "weighted_roe_pct": "2026h1_roe",
        "gross_margin_pct": "2026h1_gross_margin_reported",
    })
    base_cols = [
        "security_code", "security_name", "exchange", "board", "industry", "announcement_date",
        "2026h1_revenue", "2026h1_revenue_yoy", "2026h1_net_profit", "2026h1_net_profit_yoy",
        "2026h1_roe", "2026h1_gross_margin_reported", "basic_eps", "book_value_per_share",
    ]
    df = master[[c for c in base_cols if c in master.columns]].copy()

    fetch_log: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat(), "datasets": {}}
    perf_frames: list[pd.DataFrame] = []
    for prefix, period_date in PERFORMANCE_PERIODS.items():
        rows = fetch_datacenter("RPT_LICO_FN_CPD", "REPORTDATE", period_date)
        fetch_log["datasets"][f"performance_{prefix}"] = len(rows)
        perf_frames.append(build_period_frame(rows, prefix, "performance"))
    # 2026H1 already exists, but merge fills source-side industry and consistency fields.
    for frame in perf_frames:
        df = df.merge(frame, on="security_code", how="left", suffixes=("", "_src"))
        for col in [c for c in frame.columns if c != "security_code"]:
            src = f"{col}_src"
            if src in df.columns:
                if col in df.columns:
                    df[col] = df[col].combine_first(df[src])
                    df = df.drop(columns=[src])
                else:
                    df = df.rename(columns={src: col})

    for kind, report_name in (("income", "RPT_DMSK_FN_INCOME"), ("balance", "RPT_DMSK_FN_BALANCE"), ("cashflow", "RPT_DMSK_FN_CASHFLOW")):
        periods = STATEMENT_PERIODS if kind != "balance" else {"2025h1": "2025-06-30", "2026h1": "2026-06-30"}
        for prefix, period_date in periods.items():
            rows = fetch_datacenter(report_name, "REPORT_DATE", period_date)
            fetch_log["datasets"][f"{kind}_{prefix}"] = len(rows)
            frame = build_period_frame(rows, prefix, kind)
            df = df.merge(frame, on="security_code", how="left")

    quote = fetch_quote_snapshot()
    fetch_log["datasets"]["quote_snapshot"] = len(quote)
    df = df.merge(quote, on="security_code", how="left")
    reports = fetch_research_reports()
    fetch_log["datasets"]["research_report_aggregate"] = len(reports)
    df = df.merge(reports, on="security_code", how="left")
    events = fetch_event_announcements()
    fetch_log["datasets"]["capital_event_announcements"] = len(events)
    event_agg = aggregate_events(events)
    df = df.merge(event_agg, on="security_code", how="left")
    df["event_score_raw"] = df["event_score_raw"].fillna(0)
    df["event_positive_count"] = df["event_positive_count"].fillna(0)
    df["event_negative_count"] = df["event_negative_count"].fillna(0)
    df["report_count_12m"] = df["report_count_12m"].fillna(0)
    df["report_org_count_12m"] = df["report_org_count_12m"].fillna(0)

    df["industry"] = df.get("quote_industry").replace({"-": np.nan, "": np.nan}).combine_first(df.get("2026h1_industry_report")).combine_first(df.get("industry"))
    df["industry"] = df["industry"].fillna("UNKNOWN")
    df = calculate_metrics(df)
    df = score_models(df)
    review = select_review_pool(df, 100)

    # Sort full universe only after names have been restored; scoring was code/data-only.
    full = df.sort_values("convexity_score_stage1", ascending=False).reset_index(drop=True)
    full["stage1_global_rank"] = np.arange(1, len(full) + 1)
    full.to_csv(RESEARCH_DIR / "universe_scored_stage1.csv", index=False, encoding="utf-8-sig")
    review.to_csv(RESEARCH_DIR / "top100_stage1.csv", index=False, encoding="utf-8-sig")
    events.to_csv(RESEARCH_DIR / "capital_event_announcements_2026.csv", index=False, encoding="utf-8-sig")
    for model, col in {
        "fundamental_acceleration": "fundamental_acceleration_score",
        "new_profit_pool": "new_profit_pool_score_stage1",
        "cyclical_turn": "cyclical_turn_score",
        "capital_event": "capital_event_score",
    }.items():
        full.sort_values(col, ascending=False).head(250).to_csv(MODEL_DIR / f"{model}_top250.csv", index=False, encoding="utf-8-sig")

    columns_for_md = [
        "stage1_review_rank", "security_code", "security_name", "industry", "primary_model",
        "convexity_score_stage1", "fundamental_acceleration_score", "new_profit_pool_score_stage1",
        "cyclical_turn_score", "capital_event_score", "market_cap_100m", "pe_dynamic",
        "q2_revenue_yoy_pct", "q2_net_profit_yoy_pct", "gross_margin_delta_pp", "cash_conversion",
        "return_60d_pct", "return_ytd_pct", "report_count_12m",
    ]
    md = ["# 2026H1 A股四模型盲扫：Stage 1 前100家公司", "", "本文件只表示进入公告复核池，不是最终推荐。", ""]
    md.append(review[columns_for_md].round(2).to_markdown(index=False))
    (RESEARCH_DIR / "top100_stage1.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    fetch_log["completed_at"] = datetime.now(timezone.utc).isoformat()
    fetch_log["universe_count"] = len(df)
    fetch_log["review_pool_count"] = len(review)
    fetch_log["primary_model_counts_top100"] = review["primary_model"].value_counts().to_dict()
    fetch_log["industry_counts_top100"] = review["industry"].value_counts().head(20).to_dict()
    fetch_log["missingness_top20"] = full.isna().mean().sort_values(ascending=False).head(20).to_dict()
    json_dump(META_DIR / "research_stage1_summary.json", fetch_log)
    print(json.dumps(fetch_log, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
