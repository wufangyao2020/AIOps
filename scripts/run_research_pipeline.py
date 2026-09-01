#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a blind, auditable 2026H1 A-share research pipeline.

Outputs:
- 5,550-company four-model score table
- blind preliminary top 100
- one markdown review per top-100 company
- industry peer comparison
- final 20 high-convexity candidates with explicit milestones/invalidation

The program deliberately does not use prior conversation frequency, manually supplied
watchlists, or company-name popularity as model inputs.
"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
PERIOD = "2026H1"
REPORT_DATE = "2026-06-30"
CUTOFF_DATE = date(2026, 9, 2)
EVENT_START_DATE = date(2025, 9, 1)
MASTER = ROOT / "data" / "2026H1" / "a_share_2026_h1_5550_master.csv"
OUT = ROOT / "results" / PERIOD
REVIEW_DIR = OUT / "reviews" / "top100"
CACHE_DIR = ROOT / "data" / "research" / PERIOD
META_DIR = ROOT / "meta"
STATUS_FILE = ROOT / "status" / "research_status_2026_h1.csv"

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
QUOTE_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
REPORT_API_URL = "https://reportapi.eastmoney.com/report/list"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}

for p in [OUT, REVIEW_DIR, CACHE_DIR, META_DIR]:
    p.mkdir(parents=True, exist_ok=True)


# ----------------------------- generic helpers -----------------------------

def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def text(v: Any) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def num(v: Any) -> float:
    try:
        if v is None or v == "" or str(v).lower() in {"none", "nan", "null", "-"}:
            return float("nan")
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return float("nan")


def code6(v: Any) -> str:
    s = re.sub(r"\D", "", text(v))
    return s[-6:].zfill(6) if s else ""


def clip(v: float, lo: float, hi: float) -> float:
    if not math.isfinite(v):
        return float("nan")
    return min(max(v, lo), hi)


def finite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except Exception:
        return False


def get_first(mapping: dict[str, Any] | pd.Series, aliases: list[str], default: Any = None) -> Any:
    for key in aliases:
        if key in mapping:
            v = mapping[key]
            if v is not None and text(v) not in {"", "nan", "None"}:
                return v
    upper_map = {str(k).upper(): k for k in mapping.keys()}
    for key in aliases:
        real = upper_map.get(key.upper())
        if real is not None:
            v = mapping[real]
            if v is not None and text(v) not in {"", "nan", "None"}:
                return v
    return default


def pct_rank(series: pd.Series, higher_is_better: bool = True, fill: float = 0.35) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    valid = s.dropna()
    if valid.empty:
        return pd.Series(fill, index=series.index, dtype=float)
    lo, hi = valid.quantile([0.01, 0.99])
    if finite(lo) and finite(hi) and hi > lo:
        s = s.clip(lo, hi)
    r = s.rank(pct=True, method="average")
    if not higher_is_better:
        r = 1.0 - r + (1.0 / max(len(valid), 1))
    return r.fillna(fill).clip(0, 1)


def signed_growth(cur: pd.Series, prev: pd.Series) -> pd.Series:
    cur = pd.to_numeric(cur, errors="coerce")
    prev = pd.to_numeric(prev, errors="coerce")
    result = pd.Series(np.nan, index=cur.index, dtype=float)
    normal = prev.abs() > 1e-9
    result.loc[normal] = (cur.loc[normal] / prev.loc[normal].abs() - np.sign(prev.loc[normal])) * 100
    turnaround = (prev <= 0) & (cur > 0)
    deterioration = (prev > 0) & (cur <= 0)
    both_loss = (prev < 0) & (cur < 0)
    result.loc[turnaround] = 300 + np.log1p(cur.loc[turnaround].abs()).clip(0, 30)
    result.loc[deterioration] = -300 - np.log1p(cur.loc[deterioration].abs()).clip(0, 30)
    result.loc[both_loss] = ((cur.loc[both_loss] - prev.loc[both_loss]) / prev.loc[both_loss].abs() * 100).clip(-200, 200)
    return result.clip(-400, 400)


def safe_ratio(a: pd.Series, b: pd.Series) -> pd.Series:
    a = pd.to_numeric(a, errors="coerce")
    b = pd.to_numeric(b, errors="coerce")
    return (a / b.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", "", title or "")


def clean_filename(s: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", s)[:80]


def request_json(url: str, params: dict[str, Any], method: str = "get", data: dict[str, Any] | None = None,
                 retries: int = 3, timeout: int = 20) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            if method == "post":
                r = requests.post(url, params=params, data=data, headers=HEADERS, timeout=timeout)
            else:
                r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            raw = r.text.strip()
            if raw.startswith("{") or raw.startswith("["):
                return r.json()
            m = re.search(r"\((\{.*\})\)\s*;?\s*$", raw, re.S)
            if m:
                return json.loads(m.group(1))
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"request failed: {url}: {last}")


# ----------------------------- source acquisition -----------------------------

def fetch_dc(report_name: str, filter_expr: str, sort_columns: str = "NOTICE_DATE,SECURITY_CODE",
             sort_types: str = "-1,1", page_size: int = 500) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    page = 1
    pages = 1
    while page <= pages:
        params = {
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
            "pageSize": str(page_size),
            "pageNumber": str(page),
            "reportName": report_name,
            "columns": "ALL",
            "source": "WEB",
            "client": "WEB",
            "filter": filter_expr,
        }
        payload = request_json(DC_URL, params, retries=4, timeout=30)
        result = payload.get("result") or {}
        data = result.get("data") or []
        if page == 1:
            pages = int(result.get("pages") or 1)
        rows.extend(data)
        if not data and page == 1:
            break
        page += 1
        time.sleep(0.05)
    return pd.DataFrame(rows)


def fetch_periodic(report_name: str, report_date: str) -> pd.DataFrame:
    for field in ["REPORTDATE", "REPORT_DATE"]:
        try:
            df = fetch_dc(report_name, f"({field}='{report_date}')")
            if not df.empty:
                return df
        except Exception:
            continue
    return pd.DataFrame()


PERF_ALIASES = {
    "code": ["SECURITY_CODE", "股票代码", "security_code"],
    "name": ["SECURITY_NAME_ABBR", "股票简称", "security_name"],
    "notice_date": ["NOTICE_DATE", "最新公告日期", "announcement_date"],
    "revenue": ["TOTAL_OPERATE_INCOME", "营业总收入-营业总收入", "revenue"],
    "revenue_yoy": ["YSTZ", "营业总收入-同比增长", "revenue_yoy"],
    "revenue_qoq": ["YSHZ", "营业总收入-季度环比增长", "revenue_qoq"],
    "net_profit": ["PARENT_NETPROFIT", "净利润-净利润", "net_profit"],
    "profit_yoy": ["SJLTZ", "净利润-同比增长", "profit_yoy"],
    "profit_qoq": ["SJLHZ", "净利润-季度环比增长", "profit_qoq"],
    "deduct_profit": ["DEDUCT_PARENT_NETPROFIT", "KCFJCXSYJLR", "deduct_net_profit"],
    "deduct_profit_yoy": ["DJDYSHZ", "DEDUCT_PARENT_NETPROFIT_YOY", "deduct_profit_yoy"],
    "eps": ["BASIC_EPS", "每股收益", "eps"],
    "bps": ["BPS", "每股净资产", "bps"],
    "roe": ["WEIGHTAVG_ROE", "净资产收益率", "roe"],
    "ocfps": ["MGJYXJJE", "每股经营现金流量", "ocf_per_share"],
    "gross_margin": ["XSMLL", "销售毛利率", "gross_margin"],
    "industry": ["PUBLISHNAME", "INDUSTRY", "所处行业", "industry"],
}


def normalize_performance(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=list(PERF_ALIASES))
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        item: dict[str, Any] = {}
        for out, aliases in PERF_ALIASES.items():
            item[out] = get_first(r, aliases)
        item["code"] = code6(item["code"])
        item["name"] = text(item["name"])
        for c in ["revenue", "revenue_yoy", "revenue_qoq", "net_profit", "profit_yoy", "profit_qoq",
                  "deduct_profit", "deduct_profit_yoy", "eps", "bps", "roe", "ocfps", "gross_margin"]:
            item[c] = num(item[c])
        rows.append(item)
    out = pd.DataFrame(rows)
    out = out[out["code"].str.match(r"^\d{6}$", na=False)]
    out = out.sort_values("notice_date", ascending=False).drop_duplicates("code", keep="first")
    return out.set_index("code")


STATEMENT_ALIASES = {
    "accounts_receivable": ["ACCOUNTS_RECE", "ACCOUNTS_RECEIVABLE"],
    "inventory": ["INVENTORY"],
    "contract_liability": ["CONTRACT_LIAB", "CONTRACT_LIABILITY"],
    "construction_in_progress": ["CONSTRUCTION_IN_PROCESS", "CIP"],
    "goodwill": ["GOODWILL"],
    "total_assets": ["TOTAL_ASSETS"],
    "total_liabilities": ["TOTAL_LIABILITIES"],
    "cash": ["MONETARYFUNDS", "MONETARY_FUNDS"],
    "short_loan": ["SHORT_LOAN", "SHORT_TERM_LOAN"],
    "long_loan": ["LONG_LOAN", "LONG_TERM_LOAN"],
    "bond_payable": ["BOND_PAYABLE"],
    "netcash_operate": ["NETCASH_OPERATE", "NET_CASH_OPERATE"],
    "capex_cash": ["CONSTRUCT_LONG_ASSET", "CASH_PAY_ACQ_CONST_FIOLTA"],
    "rd_expense": ["RESEARCH_EXPENSE", "R_AND_D_EXPENSE"],
    "deduct_profit_stmt": ["DEDUCT_PARENT_NETPROFIT"],
}


def normalize_statement(df: pd.DataFrame, fields: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=fields)
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        code = code6(get_first(r, ["SECURITY_CODE", "SECUCODE", "股票代码"]))
        if not code:
            continue
        item = {"code": code, "notice_date_stmt": text(get_first(r, ["NOTICE_DATE", "UPDATE_DATE"]))}
        for field in fields:
            item[field] = num(get_first(r, STATEMENT_ALIASES[field]))
        rows.append(item)
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=fields)
    out = out.sort_values("notice_date_stmt", ascending=False).drop_duplicates("code", keep="first")
    return out.set_index("code")[fields]


def fetch_quotes() -> pd.DataFrame:
    fs = ",".join([
        "m:1+t:2", "m:1+t:23", "m:0+t:6", "m:0+t:80",
        "m:0+t:81+s:2048", "m:0+t:81+s:2048",
    ])
    fields = "f12,f14,f2,f3,f5,f6,f8,f9,f10,f20,f21,f23,f24,f25,f37,f38,f100"
    rows: list[dict[str, Any]] = []
    page = 1
    pages = 1
    while page <= pages:
        params = {
            "pn": page, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": fs, "fields": fields,
        }
        payload = request_json(QUOTE_URL, params, timeout=25)
        data = payload.get("data") or {}
        diff = data.get("diff") or []
        total = int(data.get("total") or len(diff))
        pages = max(1, math.ceil(total / 500))
        rows.extend(diff)
        page += 1
    q = pd.DataFrame(rows)
    if q.empty:
        return pd.DataFrame()
    q = q.rename(columns={
        "f12": "code", "f14": "quote_name", "f2": "price", "f3": "day_change_pct",
        "f5": "volume", "f6": "turnover_amount", "f8": "turnover_rate", "f9": "pe_dynamic",
        "f10": "volume_ratio", "f20": "market_cap", "f21": "float_market_cap", "f23": "pb",
        "f24": "change_60d_pct", "f25": "change_ytd_pct", "f37": "quote_roe",
        "f38": "total_shares", "f100": "quote_industry",
    })
    q["code"] = q["code"].map(code6)
    for c in ["price", "day_change_pct", "volume", "turnover_amount", "turnover_rate", "pe_dynamic",
              "volume_ratio", "market_cap", "float_market_cap", "pb", "change_60d_pct", "change_ytd_pct",
              "quote_roe", "total_shares"]:
        q[c] = pd.to_numeric(q.get(c), errors="coerce")
    return q.sort_values("turnover_amount", ascending=False).drop_duplicates("code").set_index("code")


# ----------------------------- announcement/event scan -----------------------------
POS_EVENT_PATTERNS: list[tuple[str, int, str]] = [
    (r"实际控制人.{0,6}(变更|拟变更)|控制权.{0,6}(变更|转让)", 24, "control_change"),
    (r"发行股份购买资产|重大资产重组|吸收合并", 22, "major_restructuring"),
    (r"资产注入|注入资产", 18, "asset_injection"),
    (r"收购|增资|受让股权|对外投资", 8, "acquisition_investment"),
    (r"回购注销|注销回购", 12, "buyback_cancel"),
    (r"回购股份|股份回购", 7, "buyback"),
    (r"控股股东.{0,8}增持|实际控制人.{0,8}增持|董事长.{0,8}增持", 9, "insider_buy"),
    (r"股权激励|限制性股票激励|员工持股计划", 6, "incentive"),
    (r"中标|重大合同|框架合同|销售合同", 6, "major_order"),
    (r"投产|试生产|竣工投产|产线通车|达产", 7, "capacity_launch"),
    (r"取得认证|客户认证|进入供应商|定点通知", 7, "customer_validation"),
    (r"上调.{0,8}业绩|修正后.{0,8}增长|提高.{0,8}指引", 9, "guidance_raise"),
    (r"分红|特别分红|中期分红", 3, "shareholder_return"),
]
NEG_EVENT_PATTERNS: list[tuple[str, int, str]] = [
    (r"立案调查|立案告知|行政处罚|刑事", -24, "investigation_penalty"),
    (r"终止.{0,8}(重组|收购|发行|项目)|重组终止", -14, "deal_terminated"),
    (r"退市风险|终止上市|可能被终止上市", -30, "delisting_risk"),
    (r"司法冻结|轮候冻结|被执行人", -12, "freeze_enforcement"),
    (r"控股股东.{0,8}减持|实际控制人.{0,8}减持|董事长.{0,8}减持", -10, "insider_sell"),
    (r"减持计划|减持股份", -5, "shareholder_sell"),
    (r"商誉减值|大额减值|计提资产减值", -8, "impairment"),
    (r"违规担保|资金占用", -24, "governance_risk"),
]
NEW_POOL_PATTERNS: list[tuple[str, int, str]] = [
    (r"新产品|新业务|第二增长曲线|战略转型", 4, "new_business"),
    (r"量产|批量交付|规模化交付", 7, "mass_production"),
    (r"扩产|产能建设|技改项目|新建项目", 4, "capacity_expansion"),
    (r"客户认证|定点通知|供应商资格|进入供应链", 7, "customer_entry"),
    (r"获得订单|签订合同|中标", 5, "order_validation"),
    (r"商业化|获批上市|注册证|生产许可", 6, "commercialization"),
]


def fetch_announcements(code: str, page_size: int = 35) -> tuple[str, list[dict[str, Any]], str]:
    try:
        payload = request_json(ANN_URL, {
            "sr": -1, "page_size": page_size, "page_index": 1, "ann_type": "A",
            "client_source": "web", "stock_list": code,
        }, timeout=15, retries=3)
        data = payload.get("data") or {}
        items = data.get("list") or []
        clean: list[dict[str, Any]] = []
        for it in items:
            d = text(it.get("notice_date") or it.get("display_time") or it.get("eiTime"))[:10]
            try:
                parsed = datetime.fromisoformat(d).date()
            except Exception:
                parsed = None
            if parsed and parsed < EVENT_START_DATE:
                continue
            clean.append({
                "code": code,
                "title": text(it.get("title")),
                "date": d,
                "art_code": text(it.get("art_code")),
                "columns": [text(x.get("column_name")) for x in (it.get("columns") or [])],
                "raw": it,
            })
        return code, clean, ""
    except Exception as exc:
        return code, [], str(exc)


def scan_all_announcements(codes: list[str]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    cache = CACHE_DIR / "announcements_latest.jsonl"
    error_file = CACHE_DIR / "announcement_errors.json"
    use_cache = os.getenv("REFRESH_ANNOUNCEMENTS", "0") != "1" and cache.exists()
    if use_cache:
        result: dict[str, list[dict[str, Any]]] = {}
        with cache.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    obj = json.loads(line)
                    result[obj["code"]] = obj.get("announcements", [])
        if len(result) >= int(len(codes) * 0.95):
            return result, json.loads(error_file.read_text("utf-8")) if error_file.exists() else {}

    result: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    workers = int(os.getenv("ANNOUNCEMENT_WORKERS", "32"))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_announcements, c): c for c in codes}
        done = 0
        for fut in cf.as_completed(futures):
            code, items, err = fut.result()
            result[code] = items
            if err:
                errors[code] = err
            done += 1
            if done % 500 == 0:
                print(f"announcement scan: {done}/{len(codes)}, errors={len(errors)}", flush=True)
    write_jsonl(cache, ({"code": c, "announcements": result.get(c, [])} for c in codes))
    write_json(error_file, errors)
    return result, errors


def score_event_titles(items: list[dict[str, Any]]) -> dict[str, Any]:
    positive = 0.0
    negative = 0.0
    new_pool = 0.0
    tags: Counter[str] = Counter()
    evidence: list[dict[str, Any]] = []
    for item in items:
        title = normalize_title(item.get("title", ""))
        try:
            d = datetime.fromisoformat(item.get("date", "")[:10]).date()
            age_days = max(0, (CUTOFF_DATE - d).days)
        except Exception:
            age_days = 365
        recency = max(0.35, 1.0 - age_days / 540.0)
        for pattern, weight, tag in POS_EVENT_PATTERNS:
            if re.search(pattern, title):
                val = weight * recency
                positive += val
                tags[tag] += 1
                evidence.append({"date": item.get("date"), "title": item.get("title"), "tag": tag, "weight": round(val, 2), "art_code": item.get("art_code")})
        for pattern, weight, tag in NEG_EVENT_PATTERNS:
            if re.search(pattern, title):
                val = weight * recency
                negative += val
                tags[tag] += 1
                evidence.append({"date": item.get("date"), "title": item.get("title"), "tag": tag, "weight": round(val, 2), "art_code": item.get("art_code")})
        for pattern, weight, tag in NEW_POOL_PATTERNS:
            if re.search(pattern, title):
                val = weight * recency
                new_pool += val
                tags[tag] += 1
    evidence.sort(key=lambda x: abs(x["weight"]), reverse=True)
    return {
        "event_positive_raw": positive,
        "event_negative_raw": negative,
        "event_net_raw": positive + negative,
        "new_pool_event_raw": new_pool,
        "event_tags": dict(tags),
        "event_evidence": evidence[:12],
    }


# ----------------------------- PDF/filing review -----------------------------
REPORT_KEYWORDS = [
    "主营业务", "分产品", "分行业", "毛利率", "新增", "新业务", "新产品", "量产", "客户认证",
    "中标", "重大合同", "在建工程", "产能", "投产", "订单", "经营现金流", "应收账款", "存货",
    "政府补助", "公允价值", "资产处置", "减值转回", "汇兑收益", "控制权", "实际控制人",
]
ONE_OFF_PATTERNS = {
    "government_subsidy": r"政府补助|财政补贴",
    "fair_value": r"公允价值变动收益|公允价值变动损益",
    "asset_disposal": r"资产处置收益|处置子公司|出售资产",
    "impairment_reversal": r"减值转回|信用减值损失转回",
    "exchange_gain": r"汇兑收益",
}


def announcement_detail_url(code: str, art_code: str) -> str:
    return f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"


def pdf_candidates(code: str, item: dict[str, Any]) -> list[str]:
    art = text(item.get("art_code"))
    raw = item.get("raw") or {}
    candidates: list[str] = []
    for key in ["attach_url", "pdf_url", "adjunctUrl", "url"]:
        u = text(raw.get(key))
        if u:
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("/"):
                u = "https://pdf.dfcfw.com" + u
            candidates.append(u)
    if art:
        for prefix in ["H2", "H3", "H1"]:
            candidates.append(f"https://pdf.dfcfw.com/pdf/{prefix}_{art}_1.pdf")
    try:
        html = requests.get(announcement_detail_url(code, art), headers=HEADERS, timeout=12).text
        html = html.replace("\\/", "/")
        for m in re.finditer(r"https?://pdf\.dfcfw\.com/pdf/[^\"'<> ]+?\.pdf(?:\?[^\"'<> ]*)?", html):
            candidates.insert(0, m.group(0))
    except Exception:
        pass
    seen: set[str] = set()
    return [u for u in candidates if u and not (u in seen or seen.add(u))]


def download_pdf_text(code: str, item: dict[str, Any], max_bytes: int = 22_000_000,
                      max_pages: int = 280) -> tuple[str, str, str]:
    for u in pdf_candidates(code, item):
        try:
            r = requests.get(u, headers=HEADERS, timeout=35)
            if r.status_code != 200 or not r.content.startswith(b"%PDF") or len(r.content) > max_bytes:
                continue
            reader = PdfReader(io.BytesIO(r.content))
            parts: list[str] = []
            for page in reader.pages[:max_pages]:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    continue
            all_text = "\n".join(parts)
            if len(all_text) >= 1000:
                return all_text[:2_000_000], u, ""
        except Exception as exc:
            last = str(exc)
            continue
    return "", "", locals().get("last", "pdf unavailable")


def extract_contexts(full_text: str, keywords: list[str], max_items: int = 18, radius: int = 180) -> list[str]:
    compact = re.sub(r"[\t\r ]+", " ", full_text)
    contexts: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        for m in re.finditer(re.escape(keyword), compact, re.I):
            start = max(0, m.start() - radius)
            end = min(len(compact), m.end() + radius)
            snippet = re.sub(r"\n+", " ", compact[start:end]).strip()
            sig = snippet[:80]
            if sig not in seen:
                seen.add(sig)
                contexts.append(snippet)
            if len(contexts) >= max_items:
                return contexts
    return contexts


def select_review_announcements(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def priority(it: dict[str, Any]) -> tuple[int, str]:
        title = normalize_title(it.get("title", ""))
        p = 0
        if "2026年半年度报告" in title and "摘要" not in title:
            p += 100
        if any(re.search(pat, title) for pat, _, _ in POS_EVENT_PATTERNS + NEG_EVENT_PATTERNS):
            p += 50
        if any(re.search(pat, title) for pat, _, _ in NEW_POOL_PATTERNS):
            p += 30
        if "投资者关系活动记录" in title:
            p += 25
        return p, it.get("date", "")
    ranked = sorted(items, key=priority, reverse=True)
    selected: list[dict[str, Any]] = []
    full_report_added = False
    for it in ranked:
        title = normalize_title(it.get("title", ""))
        if "2026年半年度报告" in title and "摘要" not in title:
            if full_report_added:
                continue
            full_report_added = True
            selected.append(it)
        elif priority(it)[0] >= 30 and len(selected) < 5:
            selected.append(it)
    return selected[:5]


def fetch_analyst_coverage(code: str) -> tuple[int, list[dict[str, Any]]]:
    params = {
        "pageSize": 50, "pageNo": 1, "stockCode": code, "industryCode": "*", "industry": "*",
        "rating": "*", "ratingchange": "*", "beginTime": EVENT_START_DATE.isoformat(),
        "endTime": CUTOFF_DATE.isoformat(), "qType": 0,
    }
    try:
        payload = request_json(REPORT_API_URL, params, timeout=15)
        data = payload.get("data") or []
        total = int(payload.get("TotalPage") or payload.get("total") or len(data))
        reports = [{
            "title": text(x.get("title")), "org": text(x.get("orgSName") or x.get("orgName")),
            "date": text(x.get("publishDate"))[:10], "rating": text(x.get("emRatingName") or x.get("rating")),
        } for x in data[:10]]
        return total if total >= len(data) else len(data), reports
    except Exception:
        return 0, []


def secid(code: str) -> str:
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code


def fetch_kline_metrics(code: str) -> dict[str, Any]:
    params = {
        "secid": secid(code), "klt": 101, "fqt": 1, "lmt": 320, "end": 20500101,
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    try:
        payload = request_json(KLINE_URL, params, timeout=15)
        klines = ((payload.get("data") or {}).get("klines") or [])
        closes: list[float] = []
        amounts: list[float] = []
        dates: list[str] = []
        for line in klines:
            p = line.split(",")
            if len(p) >= 7:
                dates.append(p[0]); closes.append(num(p[2])); amounts.append(num(p[6]))
        def ret(n: int) -> float:
            if len(closes) <= n or not closes[-n-1]:
                return float("nan")
            return (closes[-1] / closes[-n-1] - 1) * 100
        max_dd = float("nan")
        if closes:
            arr = np.array(closes, dtype=float)
            peaks = np.maximum.accumulate(arr)
            max_dd = float(np.min(arr / peaks - 1) * 100)
        return {
            "return_20d_pct": ret(20), "return_60d_pct": ret(60), "return_120d_pct": ret(120),
            "return_250d_pct": ret(250), "max_drawdown_pct": max_dd,
            "avg_amount_20d": float(np.nanmean(amounts[-20:])) if amounts else float("nan"),
            "last_trade_date": dates[-1] if dates else "",
        }
    except Exception:
        return {k: float("nan") for k in ["return_20d_pct", "return_60d_pct", "return_120d_pct", "return_250d_pct", "max_drawdown_pct", "avg_amount_20d"]}


# ----------------------------- feature construction -----------------------------

def load_universe() -> pd.DataFrame:
    if not MASTER.exists():
        raise FileNotFoundError(MASTER)
    raw = pd.read_csv(MASTER, dtype=str, low_memory=False)
    code_col = next((c for c in raw.columns if c.lower() in {"security_code", "code", "股票代码"}), None)
    if not code_col:
        code_col = next(c for c in raw.columns if "代码" in c or "CODE" in c.upper())
    raw["code"] = raw[code_col].map(code6)
    raw = raw[raw["code"].str.match(r"^\d{6}$")].drop_duplicates("code").set_index("code")
    if len(raw) != 5550:
        raise RuntimeError(f"expected 5550 universe rows, got {len(raw)}")
    return raw


def join_period(base: pd.DataFrame, perf: pd.DataFrame, suffix: str) -> pd.DataFrame:
    if perf.empty:
        return base
    renamed = perf.rename(columns={c: f"{c}_{suffix}" for c in perf.columns})
    return base.join(renamed, how="left")


def add_statement(base: pd.DataFrame, cur: pd.DataFrame, prev: pd.DataFrame, prefix: str) -> pd.DataFrame:
    if not cur.empty:
        base = base.join(cur.rename(columns={c: f"{prefix}_{c}_cur" for c in cur.columns}), how="left")
    if not prev.empty:
        base = base.join(prev.rename(columns={c: f"{prefix}_{c}_prev" for c in prev.columns}), how="left")
    return base


def build_financial_features(universe: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    dates = ["2024-06-30", "2024-12-31", "2025-03-31", "2025-06-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    perf_map: dict[str, pd.DataFrame] = {}
    for d in dates:
        print(f"fetch performance {d}", flush=True)
        perf_map[d] = normalize_performance(fetch_periodic("RPT_LICO_FN_CPD", d))

    base = pd.DataFrame(index=universe.index)
    for d, suffix in [
        ("2024-06-30", "h1_2024"), ("2024-12-31", "fy_2024"),
        ("2025-03-31", "q1_2025"), ("2025-06-30", "h1_2025"), ("2025-12-31", "fy_2025"),
        ("2026-03-31", "q1_2026"), ("2026-06-30", "h1_2026"),
    ]:
        base = join_period(base, perf_map[d], suffix)

    # statement data, best effort
    statement_meta: dict[str, Any] = {}
    for report_name, fields, prefix in [
        ("RPT_DMSK_FN_BALANCE", ["accounts_receivable", "inventory", "contract_liability", "construction_in_progress", "goodwill", "total_assets", "total_liabilities", "cash", "short_loan", "long_loan", "bond_payable"], "bs"),
        ("RPT_DMSK_FN_CASHFLOW", ["netcash_operate", "capex_cash"], "cf"),
        ("RPT_DMSK_FN_INCOME", ["rd_expense", "deduct_profit_stmt"], "is"),
    ]:
        cur_raw = fetch_periodic(report_name, REPORT_DATE)
        prev_raw = fetch_periodic(report_name, "2025-06-30")
        statement_meta[report_name] = {"current_rows": len(cur_raw), "prior_rows": len(prev_raw)}
        cur = normalize_statement(cur_raw, fields)
        prev = normalize_statement(prev_raw, fields)
        base = add_statement(base, cur, prev, prefix)

    # copy primary identity
    base["name"] = base.get("name_h1_2026", "")
    base["industry"] = base.get("industry_h1_2026", "UNKNOWN").replace("", np.nan).fillna("UNKNOWN")
    base["announcement_date"] = base.get("notice_date_h1_2026", "")

    # derived quarters
    for metric in ["revenue", "net_profit", "deduct_profit"]:
        h1c = pd.to_numeric(base.get(f"{metric}_h1_2026"), errors="coerce")
        q1c = pd.to_numeric(base.get(f"{metric}_q1_2026"), errors="coerce")
        h1p = pd.to_numeric(base.get(f"{metric}_h1_2025"), errors="coerce")
        q1p = pd.to_numeric(base.get(f"{metric}_q1_2025"), errors="coerce")
        base[f"{metric}_q2_2026"] = h1c - q1c
        base[f"{metric}_q2_2025"] = h1p - q1p
        base[f"{metric}_q2_yoy"] = signed_growth(base[f"{metric}_q2_2026"], base[f"{metric}_q2_2025"])

    base["revenue_acceleration"] = base["revenue_q2_yoy"] - pd.to_numeric(base.get("revenue_yoy_q1_2026"), errors="coerce")
    base["profit_acceleration"] = base["net_profit_q2_yoy"] - pd.to_numeric(base.get("profit_yoy_q1_2026"), errors="coerce")
    base["gross_margin_delta"] = pd.to_numeric(base.get("gross_margin_h1_2026"), errors="coerce") - pd.to_numeric(base.get("gross_margin_h1_2025"), errors="coerce")
    base["roe_delta"] = pd.to_numeric(base.get("roe_h1_2026"), errors="coerce") - pd.to_numeric(base.get("roe_h1_2025"), errors="coerce")
    base["cash_profit_ratio_ps"] = safe_ratio(base.get("ocfps_h1_2026"), base.get("eps_h1_2026"))

    # statement deltas normalized by revenue/assets
    rev = pd.to_numeric(base.get("revenue_h1_2026"), errors="coerce")
    assets = pd.to_numeric(base.get("bs_total_assets_cur"), errors="coerce")
    for f in ["accounts_receivable", "inventory", "contract_liability", "construction_in_progress", "goodwill", "cash", "short_loan", "long_loan", "bond_payable"]:
        cur = pd.to_numeric(base.get(f"bs_{f}_cur"), errors="coerce")
        prev = pd.to_numeric(base.get(f"bs_{f}_prev"), errors="coerce")
        base[f"{f}_growth"] = signed_growth(cur, prev)
    base["receivable_to_revenue"] = safe_ratio(base.get("bs_accounts_receivable_cur"), rev)
    base["inventory_to_revenue"] = safe_ratio(base.get("bs_inventory_cur"), rev)
    base["contract_liability_to_revenue"] = safe_ratio(base.get("bs_contract_liability_cur"), rev)
    base["cip_to_assets"] = safe_ratio(base.get("bs_construction_in_progress_cur"), assets)
    base["goodwill_to_assets"] = safe_ratio(base.get("bs_goodwill_cur"), assets)
    base["debt_ratio"] = safe_ratio(base.get("bs_total_liabilities_cur"), assets)
    debt = sum(pd.to_numeric(base.get(f"bs_{f}_cur"), errors="coerce").fillna(0) for f in ["short_loan", "long_loan", "bond_payable"])
    base["net_debt"] = debt - pd.to_numeric(base.get("bs_cash_cur"), errors="coerce").fillna(0)
    base["net_debt_to_assets"] = safe_ratio(base["net_debt"], assets)
    base["ocf_total_ratio"] = safe_ratio(base.get("cf_netcash_operate_cur"), base.get("net_profit_h1_2026"))
    base["capex_to_revenue"] = safe_ratio(base.get("cf_capex_cash_cur"), rev)
    base["rd_to_revenue"] = safe_ratio(base.get("is_rd_expense_cur"), rev)
    base["rd_growth"] = signed_growth(base.get("is_rd_expense_cur"), base.get("is_rd_expense_prev"))
    return base, statement_meta


def convexity_market_cap_score(mcap: pd.Series) -> pd.Series:
    # mcap in RMB: peak score roughly 4-30bn; still useful to 80bn; penalize shells and mega caps.
    bn = pd.to_numeric(mcap, errors="coerce") / 1e9
    score = pd.Series(0.25, index=mcap.index, dtype=float)
    score[(bn >= 4) & (bn <= 30)] = 1.0
    score[(bn > 30) & (bn <= 80)] = 0.82
    score[(bn > 80) & (bn <= 150)] = 0.62
    score[(bn > 150) & (bn <= 300)] = 0.45
    score[bn > 300] = 0.22
    score[(bn >= 2) & (bn < 4)] = 0.65
    score[bn < 2] = 0.20
    return score


def build_model_scores(df: pd.DataFrame, announcements: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    event_rows: dict[str, dict[str, Any]] = {}
    for code in df.index:
        event_rows[code] = score_event_titles(announcements.get(code, []))
    ev = pd.DataFrame.from_dict(event_rows, orient="index")
    df = df.join(ev[["event_positive_raw", "event_negative_raw", "event_net_raw", "new_pool_event_raw"]], how="left")
    df["event_tags_json"] = [json.dumps(event_rows[c]["event_tags"], ensure_ascii=False) for c in df.index]
    df["event_evidence_json"] = [json.dumps(event_rows[c]["event_evidence"], ensure_ascii=False) for c in df.index]

    # Industry relative measurements.
    industry = df["industry"].fillna("UNKNOWN").replace("", "UNKNOWN")
    group = df.groupby(industry)
    for col in ["revenue_q2_yoy", "net_profit_q2_yoy", "gross_margin_delta", "profit_yoy_h1_2026", "roe_h1_2026"]:
        vals = pd.to_numeric(df.get(col), errors="coerce")
        df[f"{col}_industry_pct"] = vals.groupby(industry).rank(pct=True).fillna(0.5)
    df["industry_profit_median"] = group["net_profit_q2_yoy"].transform("median")
    df["industry_revenue_median"] = group["revenue_q2_yoy"].transform("median")
    df["industry_breadth"] = group["net_profit_q2_yoy"].transform(lambda s: (pd.to_numeric(s, errors="coerce") > 0).mean())

    # Feature ranks.
    r = {}
    for col, high in [
        ("revenue_q2_yoy", True), ("net_profit_q2_yoy", True), ("deduct_profit_q2_yoy", True),
        ("revenue_acceleration", True), ("profit_acceleration", True), ("gross_margin_delta", True),
        ("roe_h1_2026", True), ("roe_delta", True), ("cash_profit_ratio_ps", True),
        ("ocf_total_ratio", True), ("receivable_to_revenue", False), ("inventory_to_revenue", False),
        ("contract_liability_growth", True), ("construction_in_progress_growth", True),
        ("capex_to_revenue", True), ("rd_to_revenue", True), ("rd_growth", True),
        ("debt_ratio", False), ("net_debt_to_assets", False), ("event_net_raw", True),
        ("new_pool_event_raw", True), ("pe_dynamic", False), ("pb", False),
    ]:
        r[col] = pct_rank(df.get(col, pd.Series(np.nan, index=df.index)), high)

    # Fundamental acceleration: audited operating acceleration and earnings quality.
    fundamental = (
        0.12*r["revenue_q2_yoy"] + 0.18*r["net_profit_q2_yoy"] + 0.10*r["deduct_profit_q2_yoy"] +
        0.12*r["profit_acceleration"] + 0.07*r["revenue_acceleration"] + 0.10*r["gross_margin_delta"] +
        0.09*r["roe_h1_2026"] + 0.04*r["roe_delta"] + 0.10*np.maximum(r["cash_profit_ratio_ps"], r["ocf_total_ratio"]) +
        0.04*r["receivable_to_revenue"] + 0.04*r["inventory_to_revenue"]
    ) * 100

    # New profit pool: capital/R&D/customer milestones plus improving unit economics.
    new_pool = (
        0.12*r["revenue_acceleration"] + 0.13*r["profit_acceleration"] + 0.12*r["gross_margin_delta"] +
        0.10*r["contract_liability_growth"] + 0.09*r["construction_in_progress_growth"] +
        0.07*r["capex_to_revenue"] + 0.10*r["rd_to_revenue"] + 0.07*r["rd_growth"] +
        0.12*r["new_pool_event_raw"] + 0.08*df["net_profit_q2_yoy_industry_pct"]
    ) * 100

    # Cycle inflection: turnarounds, spread/margin recovery, cash and inexpensive valuation.
    turnaround = ((pd.to_numeric(df.get("net_profit_q2_2025"), errors="coerce") <= 0) &
                  (pd.to_numeric(df.get("net_profit_q2_2026"), errors="coerce") > 0)).astype(float)
    loss_reduction = ((pd.to_numeric(df.get("net_profit_q2_2025"), errors="coerce") < 0) &
                      (pd.to_numeric(df.get("net_profit_q2_2026"), errors="coerce") < 0) &
                      (pd.to_numeric(df.get("net_profit_q2_2026"), errors="coerce") > pd.to_numeric(df.get("net_profit_q2_2025"), errors="coerce"))).astype(float)
    cycle = (
        0.18*r["net_profit_q2_yoy"] + 0.12*r["profit_acceleration"] + 0.15*r["gross_margin_delta"] +
        0.10*np.maximum(r["cash_profit_ratio_ps"], r["ocf_total_ratio"]) + 0.08*r["inventory_to_revenue"] +
        0.08*r["debt_ratio"] + 0.08*r["pe_dynamic"] + 0.04*r["pb"] +
        0.10*turnaround + 0.04*loss_reduction + 0.03*df["net_profit_q2_yoy_industry_pct"]
    ) * 100

    # Capital events: event evidence dominates; fundamentals and balance sheet limit false positives.
    capital = (
        0.54*r["event_net_raw"] + 0.12*r["new_pool_event_raw"] + 0.08*r["net_profit_q2_yoy"] +
        0.06*r["cash_profit_ratio_ps"] + 0.06*r["debt_ratio"] + 0.05*r["net_debt_to_assets"] +
        0.05*r["gross_margin_delta"] + 0.04*r["roe_h1_2026"]
    ) * 100

    # Hard quality penalties.
    rev = pd.to_numeric(df.get("revenue_h1_2026"), errors="coerce")
    profit = pd.to_numeric(df.get("net_profit_h1_2026"), errors="coerce")
    deduct = pd.to_numeric(df.get("deduct_profit_h1_2026"), errors="coerce")
    name = df["name"].fillna("")
    st = name.str.contains(r"\*?ST|退", case=False, regex=True).astype(float)
    tiny = ((rev < 2e8) | (profit.abs() < 1e7)).astype(float)
    loss = (profit <= 0).astype(float)
    cash_bad = ((pd.to_numeric(df.get("ocf_total_ratio"), errors="coerce") < -0.5) |
                (pd.to_numeric(df.get("cash_profit_ratio_ps"), errors="coerce") < -0.5)).astype(float)
    nonrec_gap = ((deduct.notna()) & (profit > 0) & (deduct / profit.replace(0, np.nan) < 0.5)).astype(float)
    leverage = (pd.to_numeric(df.get("debt_ratio"), errors="coerce") > 0.8).astype(float)
    quality_penalty = 10*st + 5*tiny + 8*loss + 5*cash_bad + 5*nonrec_gap + 4*leverage

    df["fundamental_acceleration_score"] = (fundamental - quality_penalty).clip(0, 100)
    df["new_profit_pool_score"] = (new_pool - 0.65*quality_penalty).clip(0, 100)
    df["cycle_inflection_score"] = (cycle - 0.45*quality_penalty).clip(0, 100)
    df["capital_event_score"] = (capital - 0.55*quality_penalty).clip(0, 100)
    df["quality_penalty"] = quality_penalty
    df["is_st"] = st.astype(int)
    df["market_cap_convexity"] = convexity_market_cap_score(df.get("market_cap", pd.Series(np.nan, index=df.index))) * 100

    scores = df[["fundamental_acceleration_score", "new_profit_pool_score", "cycle_inflection_score", "capital_event_score"]]
    sorted_scores = np.sort(scores.to_numpy(float), axis=1)
    df["best_model_score"] = sorted_scores[:, -1]
    df["second_model_score"] = sorted_scores[:, -2]
    df["primary_model"] = scores.idxmax(axis=1).map({
        "fundamental_acceleration_score": "基本面加速",
        "new_profit_pool_score": "新利润池迁移",
        "cycle_inflection_score": "周期拐点",
        "capital_event_score": "资本事件",
    })
    # Preliminary score deliberately excludes company-name popularity and analyst opinions.
    price_not_overheated = 1 - pct_rank(df.get("change_60d_pct", pd.Series(np.nan, index=df.index)), True)
    df["preliminary_convexity_score"] = (
        0.44*df["best_model_score"] + 0.22*df["second_model_score"] +
        0.12*df["market_cap_convexity"] + 0.10*price_not_overheated*100 +
        0.07*df["net_profit_q2_yoy_industry_pct"]*100 + 0.05*df["gross_margin_delta_industry_pct"]*100
        - quality_penalty
    ).clip(0, 100)
    df["blind_id"] = [hashlib.sha256(("2026H1:" + c).encode()).hexdigest()[:12] for c in df.index]
    return df


def choose_top100(df: pd.DataFrame) -> pd.DataFrame:
    selected: set[str] = set()
    # Each model can surface its own anomalies; final count is ranking based, not a fixed quota in final 20.
    for col in ["fundamental_acceleration_score", "new_profit_pool_score", "cycle_inflection_score", "capital_event_score"]:
        selected.update(df.nlargest(45, col).index)
    selected.update(df.nlargest(120, "preliminary_convexity_score").index)
    candidates = df.loc[list(selected)].copy()
    candidates = candidates[candidates["is_st"] == 0]
    candidates = candidates.sort_values("preliminary_convexity_score", ascending=False).head(100)
    candidates["preliminary_rank"] = range(1, len(candidates) + 1)
    return candidates


# ----------------------------- reviews, peers, final selection -----------------------------

def format_pct(v: Any) -> str:
    return "—" if not finite(v) else f"{float(v):.1f}%"


def format_yi(v: Any) -> str:
    return "—" if not finite(v) else f"{float(v)/1e8:.2f}亿元"


def report_review_worker(code: str, row: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    selected = select_review_announcements(items)
    full_text = ""
    filing_url = ""
    read_docs: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, it in enumerate(selected):
        txt, url, err = download_pdf_text(code, it, max_pages=300 if i == 0 else 100)
        read_docs.append({"date": it.get("date"), "title": it.get("title"), "url": url or announcement_detail_url(code, it.get("art_code", "")), "text_chars": len(txt), "error": err})
        if txt:
            full_text += "\n" + txt
        elif err:
            errors.append(f"{it.get('title')}: {err}")
    contexts = extract_contexts(full_text, REPORT_KEYWORDS)
    one_off_flags = {k: len(re.findall(p, full_text)) for k, p in ONE_OFF_PATTERNS.items()}
    analyst_count, analyst_reports = fetch_analyst_coverage(code)
    kline = fetch_kline_metrics(code)

    event_info = score_event_titles(items)
    evidence_points = 0
    evidence_points += min(22, len(full_text) / 20_000 * 6)
    evidence_points += min(18, len([x for x in read_docs if x["text_chars"] > 1000]) * 5)
    evidence_points += min(18, event_info["new_pool_event_raw"] * 0.9)
    evidence_points += min(18, max(0, event_info["event_net_raw"]) * 0.7)
    evidence_points += min(12, max(0, num(row.get("gross_margin_delta"))) * 2)
    evidence_points += min(12, max(0, num(row.get("profit_acceleration"))) / 15)
    evidence_strength = float(min(100, evidence_points))

    overheat = 0.0
    if finite(kline.get("return_60d_pct")):
        overheat += max(0, (kline["return_60d_pct"] - 40) * 0.30)
    if finite(kline.get("return_250d_pct")):
        overheat += max(0, (kline["return_250d_pct"] - 120) * 0.10)
    oneoff_count = sum(one_off_flags.values())
    oneoff_penalty = min(15, oneoff_count * 0.4)
    nonconsensus = 100.0
    nonconsensus -= min(55, analyst_count * 4.5)
    if finite(kline.get("return_60d_pct")):
        nonconsensus -= max(0, min(25, (kline["return_60d_pct"] - 20) * 0.35))
    nonconsensus = max(0, min(100, nonconsensus))

    primary = row.get("primary_model", "")
    milestones = {
        "基本面加速": "下一季度扣非利润继续快于收入增长；毛利率与经营现金流不反向；应收和存货不快于收入。",
        "新利润池迁移": "新产品/新业务形成可量化收入与更高毛利；产能投放后利用率、客户数和现金回款同步提升。",
        "周期拐点": "产品—原料价差、产能利用率和行业库存继续改善；公司成本曲线优势转化为现金流。",
        "资本事件": "交易获得必要审批并完成交割；资产真实并表且增厚每股利润，稀释与商誉风险受控。",
    }.get(primary, "下一季度主营、扣非利润和现金流继续同步改善。")
    invalidation = {
        "基本面加速": "扣非利润增速连续两个季度低于收入；毛利率回落；现金流转差或应收、存货异常膨胀。",
        "新利润池迁移": "只有战略合作/送样，没有批量订单；新增产能低利用率；新业务增收不增利。",
        "周期拐点": "产品价格或价差反转；行业重新扩产；库存上升且公司现金流先恶化。",
        "资本事件": "交易终止、审批失败、高溢价并购、关联输送或大幅稀释后每股利润不增。",
    }.get(primary, "主营和现金流同时恶化。")

    return {
        "code": code,
        "name": row.get("name", ""),
        "industry": row.get("industry", "UNKNOWN"),
        "primary_model": primary,
        "analyst_report_count_12m": analyst_count,
        "analyst_reports_json": json.dumps(analyst_reports, ensure_ascii=False),
        "fulltext_chars_read": len(full_text),
        "documents_read_count": len([x for x in read_docs if x["text_chars"] > 1000]),
        "documents_json": json.dumps(read_docs, ensure_ascii=False),
        "contexts_json": json.dumps(contexts, ensure_ascii=False),
        "one_off_flags_json": json.dumps(one_off_flags, ensure_ascii=False),
        "evidence_strength": round(evidence_strength, 2),
        "nonconsensus_score": round(nonconsensus, 2),
        "overheat_penalty": round(overheat, 2),
        "oneoff_penalty": round(oneoff_penalty, 2),
        "milestones": milestones,
        "invalidation": invalidation,
        "review_errors": " | ".join(errors[:6]),
        **kline,
    }


def write_company_review(row: pd.Series) -> None:
    code = row.name if isinstance(row.name, str) and len(row.name) == 6 else row.get("code", "")
    name = text(row.get("name"))
    try:
        docs = json.loads(row.get("documents_json") or "[]")
    except Exception:
        docs = []
    try:
        contexts = json.loads(row.get("contexts_json") or "[]")
    except Exception:
        contexts = []
    try:
        events = json.loads(row.get("event_evidence_json") or "[]")
    except Exception:
        events = []
    lines = [
        f"# {code} {name} — Top100公告复核",
        "",
        f"- 行业：{row.get('industry', 'UNKNOWN')}",
        f"- 初筛排名：{int(row.get('preliminary_rank', 0))}",
        f"- 主模型：{row.get('primary_model', '')}",
        f"- 四模型分：基本面 {row.get('fundamental_acceleration_score', 0):.1f} / 新利润池 {row.get('new_profit_pool_score', 0):.1f} / 周期 {row.get('cycle_inflection_score', 0):.1f} / 资本事件 {row.get('capital_event_score', 0):.1f}",
        f"- 全文读取字符：{int(row.get('fulltext_chars_read', 0) or 0):,}",
        f"- 成功读取文件数：{int(row.get('documents_read_count', 0) or 0)}",
        f"- 过去12个月分析师报告数：{int(row.get('analyst_report_count_12m', 0) or 0)}",
        f"- 证据强度：{row.get('evidence_strength', 0):.1f}/100",
        f"- 非共识分：{row.get('nonconsensus_score', 0):.1f}/100",
        "",
        "## 财务异常",
        "",
        f"- 2026H1营收：{format_yi(row.get('revenue_h1_2026'))}，同比 {format_pct(row.get('revenue_yoy_h1_2026'))}",
        f"- 2026H1归母净利润：{format_yi(row.get('net_profit_h1_2026'))}，同比 {format_pct(row.get('profit_yoy_h1_2026'))}",
        f"- 2026Q2营收同比：{format_pct(row.get('revenue_q2_yoy'))}；归母净利润同比：{format_pct(row.get('net_profit_q2_yoy'))}",
        f"- 毛利率同比变化：{format_pct(row.get('gross_margin_delta'))}；ROE：{format_pct(row.get('roe_h1_2026'))}",
        f"- 现金利润比：{row.get('ocf_total_ratio', row.get('cash_profit_ratio_ps', float('nan'))):.2f}" if finite(row.get('ocf_total_ratio', row.get('cash_profit_ratio_ps'))) else "- 现金利润比：—",
        "",
        "## 已读取公告/报告",
        "",
    ]
    if docs:
        for d in docs:
            lines.append(f"- {d.get('date','')} [{d.get('title','')}]({d.get('url','')}) — 提取 {d.get('text_chars',0):,} 字符")
    else:
        lines.append("- 未成功取得全文；仅保留公告题名和财务表复核，最终评分已降低证据强度。")
    lines += ["", "## 资本事件与经营里程碑题名", ""]
    for e in events[:10]:
        lines.append(f"- {e.get('date','')} {e.get('title','')}（{e.get('tag','')}，权重 {e.get('weight','')}）")
    if not events:
        lines.append("- 过去12个月未识别到高权重资本事件题名。")
    lines += ["", "## 报告原文证据片段", ""]
    for i, c in enumerate(contexts[:12], 1):
        lines.append(f"{i}. {c}")
    if not contexts:
        lines.append("- 未抽取到可用全文片段。")
    lines += [
        "", "## 下一轮验证", "", row.get("milestones", ""),
        "", "## 证伪条件", "", row.get("invalidation", ""),
        "", "> 本页由统一程序生成；股票名称不参与初筛评分，公告链接用于人工复核。",
    ]
    path = REVIEW_DIR / f"{code}_{clean_filename(name)}.md"
    atomic_write_text(path, "\n".join(lines) + "\n")


def peer_comparison(df: pd.DataFrame, top100: pd.DataFrame) -> pd.DataFrame:
    metrics = [
        ("revenue_q2_yoy", True), ("net_profit_q2_yoy", True), ("gross_margin_delta", True),
        ("roe_h1_2026", True), ("ocf_total_ratio", True), ("preliminary_convexity_score", True),
        ("pe_dynamic", False),
    ]
    rows: list[dict[str, Any]] = []
    for code, r in top100.iterrows():
        ind = r.get("industry", "UNKNOWN")
        peers = df[df["industry"] == ind].copy()
        rec: dict[str, Any] = {"code": code, "name": r.get("name", ""), "industry": ind, "peer_count": len(peers)}
        peer_score_parts = []
        for col, high in metrics:
            s = pd.to_numeric(peers.get(col), errors="coerce")
            rank = s.rank(ascending=not high, method="min")
            pctv = s.rank(pct=True, ascending=high)
            rec[f"{col}_peer_rank"] = int(rank.get(code)) if finite(rank.get(code)) else None
            rec[f"{col}_peer_pct"] = float(pctv.get(code)) if finite(pctv.get(code)) else None
            if finite(pctv.get(code)):
                peer_score_parts.append(float(pctv.get(code)))
        rec["peer_quality_score"] = round(100 * statistics.mean(peer_score_parts), 2) if peer_score_parts else 50.0
        top_peers = peers.nlargest(5, "preliminary_convexity_score")[["name", "preliminary_convexity_score"]]
        rec["top_peers_json"] = json.dumps([
            {"code": c, "name": x["name"], "score": round(float(x["preliminary_convexity_score"]), 2)}
            for c, x in top_peers.iterrows()
        ], ensure_ascii=False)
        rows.append(rec)
    return pd.DataFrame(rows).set_index("code")


def calculate_double_math(row: pd.Series, industry_median_pe: float) -> dict[str, Any]:
    mcap = num(row.get("market_cap"))
    fy25 = num(row.get("net_profit_fy_2025"))
    h125 = num(row.get("net_profit_h1_2025"))
    h126 = num(row.get("net_profit_h1_2026"))
    q2 = num(row.get("net_profit_q2_2026"))
    ttm = fy25 - h125 + h126 if all(finite(x) for x in [fy25, h125, h126]) else float("nan")
    run_rate = max([x for x in [ttm, h126*2 if finite(h126) else np.nan, q2*4 if finite(q2) else np.nan] if finite(x)] or [np.nan])
    primary = row.get("primary_model", "")
    if primary == "周期拐点":
        target_pe = clip(industry_median_pe if finite(industry_median_pe) else 15, 8, 20)
    elif primary == "资本事件":
        target_pe = clip(industry_median_pe if finite(industry_median_pe) else 25, 12, 35)
    else:
        target_pe = clip(industry_median_pe if finite(industry_median_pe) else 28, 15, 40)
    required_profit = 2*mcap/target_pe if finite(mcap) and target_pe else float("nan")
    required_growth = (required_profit/run_rate - 1)*100 if finite(required_profit) and finite(run_rate) and run_rate > 0 else float("nan")
    feasibility = 100 - max(0, min(100, required_growth / 1.5)) if finite(required_growth) else 15
    return {
        "ttm_profit_est": ttm, "profit_run_rate_est": run_rate, "double_target_pe": target_pe,
        "double_required_profit": required_profit, "double_required_profit_growth_pct": required_growth,
        "double_math_score": max(0, min(100, feasibility)),
    }


def build_final20(scored: pd.DataFrame, reviewed: pd.DataFrame, peers: pd.DataFrame) -> pd.DataFrame:
    pool = reviewed.join(peers.drop(columns=["name", "industry"], errors="ignore"), how="left")
    industry_pe = scored[(pd.to_numeric(scored["pe_dynamic"], errors="coerce") > 0) & (pd.to_numeric(scored["pe_dynamic"], errors="coerce") < 150)].groupby("industry")["pe_dynamic"].median()
    math_rows = {}
    for code, row in pool.iterrows():
        math_rows[code] = calculate_double_math(row, num(industry_pe.get(row.get("industry", "UNKNOWN"))))
    pool = pool.join(pd.DataFrame.from_dict(math_rows, orient="index"))

    pool["reviewed_composite_score"] = (
        0.29*pool["best_model_score"] + 0.14*pool["second_model_score"] +
        0.13*pool["evidence_strength"] + 0.10*pool["peer_quality_score"].fillna(50) +
        0.10*pool["nonconsensus_score"] + 0.11*pool["market_cap_convexity"] +
        0.08*pool["double_math_score"] + 0.05*pool["fundamental_acceleration_score"] -
        pool["overheat_penalty"] - pool["oneoff_penalty"] - pool["quality_penalty"]*0.5
    ).clip(0, 100)

    # Require at least some document/title evidence, positive revenue scale, and no ST.
    eligible = pool[(pool["is_st"] == 0) &
                    (pd.to_numeric(pool["revenue_h1_2026"], errors="coerce") >= 2e8) &
                    (pool["evidence_strength"] >= 12)].copy()
    eligible = eligible.sort_values("reviewed_composite_score", ascending=False)

    # Avoid one industry crowding out the entire result; no model quota is imposed.
    selected: list[str] = []
    industry_count: Counter[str] = Counter()
    for code, row in eligible.iterrows():
        ind = row.get("industry", "UNKNOWN")
        if industry_count[ind] >= 3:
            continue
        selected.append(code)
        industry_count[ind] += 1
        if len(selected) == 20:
            break
    final = eligible.loc[selected].copy()
    final["final_rank"] = range(1, len(final) + 1)
    final["selection_stage"] = np.where(final["evidence_strength"] >= 55, "B轮验证", np.where(final["evidence_strength"] >= 32, "A轮验证", "种子观察"))
    final["initial_position_pct"] = np.select(
        [final["selection_stage"].eq("B轮验证"), final["selection_stage"].eq("A轮验证")],
        [1.5, 0.8], default=0.4,
    )
    return final


def final20_markdown(final: pd.DataFrame, announcement_success: int, fulltext_success: int) -> str:
    lines = [
        "# 2026H1 A股全市场高凸性候选20只",
        "",
        f"> 生成时间：{utc_now()}。母池5,550家；四模型全量评分；初筛100家逐家公司公告/半年报自动全文复核；成功取得公告列表 {announcement_success}/5550，Top100中成功全文读取 {fulltext_success}/100。",
        "",
        "## 先看结论",
        "",
        "本表是公开市场VC候选池，不是20只等权买入清单。初始仓位买的是可能性，后续仓位只在里程碑完成后增加。任何候选一旦触发证伪条件，不以股价下跌为理由续命。",
        "",
        "|排名|代码|公司|主模型|综合分|市值(亿)|PE|Q2利润同比|毛利率变化|证据强度|非共识|阶段|初始仓位%|",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for code, r in final.iterrows():
        lines.append(
            f"|{int(r['final_rank'])}|{code}|{r['name']}|{r['primary_model']}|{r['reviewed_composite_score']:.1f}|"
            f"{num(r.get('market_cap'))/1e8:.1f}|{num(r.get('pe_dynamic')):.1f}|{format_pct(r.get('net_profit_q2_yoy'))}|"
            f"{format_pct(r.get('gross_margin_delta'))}|{r['evidence_strength']:.1f}|{r['nonconsensus_score']:.1f}|"
            f"{r['selection_stage']}|{r['initial_position_pct']:.1f}|"
        )
    lines += ["", "## 逐只审判", ""]
    for code, r in final.iterrows():
        req = format_yi(r.get("double_required_profit"))
        req_growth = format_pct(r.get("double_required_profit_growth_pct"))
        review_link = f"reviews/top100/{code}_{clean_filename(text(r.get('name')))}.md"
        lines += [
            f"### {int(r['final_rank'])}. {code} {r['name']}", "",
            f"- **主模型**：{r['primary_model']}；四模型分为 基本面{r['fundamental_acceleration_score']:.1f} / 新利润池{r['new_profit_pool_score']:.1f} / 周期{r['cycle_inflection_score']:.1f} / 资本事件{r['capital_event_score']:.1f}。",
            f"- **原始异常**：2026Q2营收同比{format_pct(r.get('revenue_q2_yoy'))}，利润同比{format_pct(r.get('net_profit_q2_yoy'))}，利润加速度{format_pct(r.get('profit_acceleration'))}，毛利率同比变化{format_pct(r.get('gross_margin_delta'))}。",
            f"- **同行位置**：行业样本{int(r.get('peer_count',0) or 0)}家，同行综合分{r.get('peer_quality_score',50):.1f}/100。",
            f"- **非共识与价格**：近12个月分析师报告约{int(r.get('analyst_report_count_12m',0) or 0)}篇；60日涨幅{format_pct(r.get('return_60d_pct'))}；非共识分{r['nonconsensus_score']:.1f}。",
            f"- **翻倍数学压力测试**：按{r.get('double_target_pe',0):.1f}倍PE，翻倍市值需年度利润约{req}，较当前估算利润运行速度需增长{req_growth}。这不是预测，只用于识别数学难度。",
            f"- **下一里程碑**：{r['milestones']}",
            f"- **证伪条件**：{r['invalidation']}",
            f"- **公开市场VC动作**：{r['selection_stage']}，初始仓位上限建议{r['initial_position_pct']:.1f}%；只有证据增强才加仓。",
            f"- [查看公告全文复核页]({review_link})", "",
        ]
    lines += [
        "## 模型边界", "",
        "- 四模型用于发现异常，不替代法定公告和人工投资判断。",
        "- 公告全文读取失败的公司已降低证据强度，不会伪装成已人工确认。",
        "- 分析师覆盖仅作为非共识代理，不把券商评级当成买入信号。",
        "- 市值、PE和行情来自运行时公开行情接口；正式交易前必须重新核验。",
    ]
    return "\n".join(lines) + "\n"


def update_status(scored: pd.DataFrame, top100: pd.DataFrame, final: pd.DataFrame) -> None:
    status = pd.DataFrame(index=scored.index)
    status["security_code"] = status.index
    status["security_name"] = scored["name"]
    status["fundamental_acceleration_score"] = scored["fundamental_acceleration_score"]
    status["new_profit_pool_score"] = scored["new_profit_pool_score"]
    status["cycle_inflection_score"] = scored["cycle_inflection_score"]
    status["capital_event_score"] = scored["capital_event_score"]
    status["model_scoring_status"] = "completed"
    status["top100_selected"] = status.index.isin(top100.index)
    status["announcement_review_status"] = np.where(status.index.isin(top100.index), "completed_or_best_effort", "not_required")
    status["peer_comparison_status"] = np.where(status.index.isin(top100.index), "completed", "not_required")
    status["final20_selected"] = status.index.isin(final.index)
    status["research_updated_at_utc"] = utc_now()
    status.to_csv(STATUS_FILE, index=False, encoding="utf-8-sig")


def main() -> int:
    print("stage 1/7: load exact 5550 universe", flush=True)
    universe = load_universe()

    print("stage 2/7: fetch multi-period financials and market data", flush=True)
    features, statement_meta = build_financial_features(universe)
    quotes = fetch_quotes()
    features = features.join(quotes, how="left")
    features["industry"] = features["industry"].replace("UNKNOWN", np.nan).fillna(features.get("quote_industry")).fillna("UNKNOWN")
    features["name"] = features["name"].replace("", np.nan).fillna(features.get("quote_name")).fillna("")

    print("stage 3/7: scan recent announcements for all 5550 companies", flush=True)
    announcements, ann_errors = scan_all_announcements(list(features.index))

    print("stage 4/7: calculate four independent model scores", flush=True)
    scored = build_model_scores(features, announcements)
    scored_out = scored.copy()
    scored_out.insert(0, "security_code", scored_out.index)
    scored_out.to_csv(OUT / "universe_four_model_scores.csv", index=False, encoding="utf-8-sig")

    top100 = choose_top100(scored)
    blind_cols = ["blind_id", "preliminary_rank", "primary_model", "fundamental_acceleration_score", "new_profit_pool_score", "cycle_inflection_score", "capital_event_score", "preliminary_convexity_score"]
    top100[blind_cols].to_csv(OUT / "top100_blind_preliminary.csv", encoding="utf-8-sig")

    print("stage 5/7: read top100 filings and material announcements", flush=True)
    review_rows: dict[str, dict[str, Any]] = {}
    workers = int(os.getenv("REVIEW_WORKERS", "8"))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(report_review_worker, code, row.to_dict(), announcements.get(code, [])): code
            for code, row in top100.iterrows()
        }
        done = 0
        for fut in cf.as_completed(futures):
            code = futures[fut]
            try:
                review_rows[code] = fut.result()
            except Exception as exc:
                review_rows[code] = {"code": code, "name": top100.loc[code, "name"], "industry": top100.loc[code, "industry"], "review_errors": str(exc), "evidence_strength": 0, "nonconsensus_score": 50, "overheat_penalty": 0, "oneoff_penalty": 0, "milestones": "需补充公告全文后再判断。", "invalidation": "公告证据不足，不得升仓。"}
            done += 1
            if done % 10 == 0:
                print(f"filing review: {done}/100", flush=True)
    reviews = pd.DataFrame.from_dict(review_rows, orient="index")
    reviews.index.name = "code"
    reviewed = top100.join(reviews.drop(columns=["name", "industry", "primary_model"], errors="ignore"), how="left")
    reviewed.to_csv(OUT / "top100_announcement_reviews.csv", encoding="utf-8-sig")

    print("stage 6/7: industry peer comparison", flush=True)
    peers = peer_comparison(scored, top100)
    peers.to_csv(OUT / "top100_peer_comparison.csv", encoding="utf-8-sig")
    reviewed_with_peers = reviewed.join(peers.drop(columns=["name", "industry"], errors="ignore"), how="left")
    for code, row in reviewed_with_peers.iterrows():
        write_company_review(row)

    print("stage 7/7: select final20 with convexity and evidence constraints", flush=True)
    final = build_final20(scored, reviewed, peers)
    final_out = final.copy()
    final_out.insert(0, "security_code", final_out.index)
    final_out.to_csv(OUT / "final20_high_convexity.csv", index=False, encoding="utf-8-sig")
    write_jsonl(OUT / "final20_high_convexity.jsonl", ({"security_code": c, **r.dropna().to_dict()} for c, r in final.iterrows()))
    ann_success = sum(1 for c in scored.index if c in announcements and announcements[c])
    fulltext_success = int((pd.to_numeric(reviews.get("fulltext_chars_read"), errors="coerce") > 1000).sum())
    atomic_write_text(OUT / "final20_high_convexity.md", final20_markdown(final, ann_success, fulltext_success))

    update_status(scored, top100, final)
    manifest = {
        "dataset": "2026H1 A-share full-market four-model research",
        "generated_at_utc": utc_now(),
        "universe_count": len(scored),
        "announcement_company_success_count": ann_success,
        "announcement_company_error_count": len(ann_errors),
        "top100_count": len(top100),
        "top100_fulltext_success_count": fulltext_success,
        "final20_count": len(final),
        "statement_fetch_rows": statement_meta,
        "model_status": {
            "fundamental_acceleration": "completed",
            "new_profit_pool": "completed_with_fulltext_confirmation_for_top100",
            "cycle_inflection": "completed",
            "capital_event": "completed_from_recent_announcement_scan",
            "top100_announcement_review": "completed_best_effort",
            "peer_comparison": "completed",
            "final20": "completed",
        },
        "known_limitations": [
            "Eastmoney is an aggregation source; official exchange filings remain the legal source of record.",
            "Some PDFs may reject automated download; those companies receive lower evidence scores and retain error logs.",
            "New-profit-pool identification is a structured proxy until segment disclosures and customer evidence are manually signed off.",
            "Current market fields are point-in-time and must be refreshed before trade execution.",
        ],
    }
    files = [OUT / "universe_four_model_scores.csv", OUT / "top100_blind_preliminary.csv", OUT / "top100_announcement_reviews.csv", OUT / "top100_peer_comparison.csv", OUT / "final20_high_convexity.csv", OUT / "final20_high_convexity.jsonl", OUT / "final20_high_convexity.md", STATUS_FILE]
    manifest["checksums"] = {str(p.relative_to(ROOT)): sha256(p) for p in files if p.exists()}
    write_json(OUT / "audit_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        write_json(OUT / "pipeline_failure.json", {"failed_at_utc": utc_now(), "error": str(exc)})
        raise
