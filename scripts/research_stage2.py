#!/usr/bin/env python3
"""Stage 2: read announcements for the Stage-1 top 100, compare peers, select 20.

This program performs an auditable, programmatic full-text review.  For each of
100 companies it reads the 2026 half-year report, 2026 Q1 report, 2025 annual
report, recent investor-relations records and relevant capital-event notices
when available.  It stores source identifiers, SHA-256 hashes and ranked
verbatim evidence excerpts, but not entire copyrighted reports.

Stage 2 refines the four models, computes within-industry ranks and produces a
20-company high-convexity research shortlist.  It is a research shortlist, not
a promise that any security will appreciate or a personalized trade order.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "data" / "2026H1" / "research"
DOSSIER_DIR = RESEARCH_DIR / "dossiers"
META_DIR = ROOT / "meta"
TOP100 = RESEARCH_DIR / "top100_stage1.csv"
UNIVERSE = RESEARCH_DIR / "universe_scored_stage1.csv"
NOTICE_LIST = "https://np-anotice-stock.eastmoney.com/api/security/ann"
NOTICE_CONTENT = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
AS_OF = pd.Timestamp("2026-09-02")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://data.eastmoney.com/",
}
_thread = threading.local()

EVIDENCE_GROUPS: dict[str, tuple[str, ...]] = {
    "growth": (
        "在手订单", "新增订单", "合同负债", "销量", "产量", "产能利用率", "市占率", "市场份额",
        "新增客户", "客户认证", "海外收入", "出口", "量产", "批量供货", "投产", "达产", "增长",
    ),
    "new_profit_pool": (
        "新业务", "新产品", "新领域", "第二增长曲线", "转型", "升级", "高端化", "智能化", "数字化",
        "国产替代", "海外市场", "产业化", "新产能", "新材料", "新客户", "新应用", "平台化",
    ),
    "cycle": (
        "供需", "库存", "去库存", "产能退出", "开工率", "利用率", "价格上涨", "价格下降", "价差",
        "原材料价格", "单位成本", "产品价格", "行业景气", "周期", "减产", "满产", "扩产",
    ),
    "capital": (
        "控制权", "实际控制人", "收购", "并购", "重大资产重组", "资产注入", "剥离", "出售资产",
        "回购注销", "回购股份", "增持", "减持", "定向增发", "向特定对象发行", "股权激励",
    ),
    "risk": (
        "亏损", "下降", "下滑", "减值", "应收账款", "存货", "逾期", "诉讼", "仲裁", "调查", "处罚",
        "质押", "担保", "产能过剩", "价格战", "客户集中", "汇率风险", "资金占用", "债务", "终止",
    ),
    "milestone": (
        "预计", "计划", "将于", "力争", "目标", "建设期", "投产", "达产", "量产", "交付", "认证",
        "2026年下半年", "2027年", "2028年", "未来十二个月", "募投项目",
    ),
}

NEW_LEVEL_RULES: list[tuple[int, tuple[str, ...]]] = [
    (5, ("收入占比", "毛利率", "利润贡献", "分部利润")),
    (4, ("实现营业收入", "实现销售收入", "销售收入", "形成收入")),
    (3, ("批量供货", "批量交付", "正式订单", "中标", "签订合同", "规模量产")),
    (2, ("客户认证", "送样", "试装", "小批量", "试生产", "验证通过")),
    (1, ("布局", "研发", "规划", "拟建设", "技术储备", "战略合作")),
]

DOC_PATTERNS: dict[str, tuple[str, ...]] = {
    "h1_2026": (r"2026年半年度报告(?!摘要)", r"2026年半年报(?!摘要)"),
    "q1_2026": (r"2026年第一季度报告",),
    "annual_2025": (r"2025年年度报告(?!摘要)",),
    "ir": (r"投资者关系活动记录", r"投资者调研", r"业绩说明会"),
}

RISK_WEIGHTS = {
    "立案调查": 18, "退市风险": 25, "资金占用": 20, "债务逾期": 18,
    "重大诉讼": 10, "终止": 8, "减值": 5, "亏损": 7, "下滑": 3,
    "客户集中": 3, "应收账款": 2, "存货": 2, "质押": 5, "减持": 4,
}


def session() -> requests.Session:
    if not hasattr(_thread, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread.session = s
    return _thread.session


def get_json(url: str, params: dict[str, Any], attempts: int = 5, timeout: int = 45) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = session().get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts:
                time.sleep(min(1.6 ** attempt, 10))
    raise RuntimeError(f"GET {url} failed: {last}")


def clean_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>|</p>|</tr>|</li>|</div>|</h\d>", "。", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n+", "。", text)
    text = re.sub(r"。{2,}", "。", text)
    return text.strip()


def sentence_split(text: str) -> list[str]:
    chunks = re.split(r"(?<=[。！？；])", text)
    out: list[str] = []
    for sentence in chunks:
        s = re.sub(r"\s+", " ", sentence).strip(" 。")
        if 25 <= len(s) <= 600:
            out.append(s)
        elif len(s) > 600:
            for part in re.split(r"[；。]", s):
                part = part.strip()
                if 25 <= len(part) <= 600:
                    out.append(part)
    return out


def sentence_score(sentence: str, keywords: tuple[str, ...]) -> float:
    matches = sum(1 for k in keywords if k in sentence)
    number_bonus = min(len(re.findall(r"\d+(?:\.\d+)?%|\d+(?:\.\d+)?万|\d+(?:\.\d+)?亿", sentence)), 4)
    future_bonus = 1 if any(k in sentence for k in EVIDENCE_GROUPS["milestone"]) else 0
    explicit_bonus = 1 if any(k in sentence for k in ("同比", "环比", "占比", "毛利率", "订单", "收入", "利润")) else 0
    return matches * 3 + number_bonus * 1.5 + future_bonus + explicit_bonus


def top_sentences(sentences: list[str], keywords: tuple[str, ...], n: int = 8) -> list[str]:
    candidates = [(sentence_score(s, keywords), s) for s in sentences if any(k in s for k in keywords)]
    candidates.sort(key=lambda x: (x[0], len(x[1])), reverse=True)
    result: list[str] = []
    seen: set[str] = set()
    for score, s in candidates:
        signature = re.sub(r"\d+", "#", s[:90])
        if signature in seen:
            continue
        seen.add(signature)
        result.append(s[:500])
        if len(result) >= n:
            break
    return result


def announcement_list(code: str) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "sr": "-1", "page_size": "100", "page_index": "1", "ann_type": "A",
        "client_source": "web", "f_node": "0", "s_node": "0", "stock_list": code,
        "begin_time": "2025-01-01", "end_time": "2026-09-02",
    }
    first = get_json(NOTICE_LIST, params)
    data = first.get("data") or {}
    pages = int(math.ceil(int(data.get("total_hits") or 0) / 100.0))
    rows = list(data.get("list") or [])
    for page in range(2, min(pages, 6) + 1):
        params["page_index"] = str(page)
        payload = get_json(NOTICE_LIST, params)
        rows.extend((payload.get("data") or {}).get("list") or [])
    return rows


def announcement_content(art_code: str) -> tuple[str, dict[str, Any]]:
    payload = get_json(NOTICE_CONTENT, {"art_code": art_code, "client_source": "web", "page_index": "1"})
    data = payload.get("data") or {}
    raw = data.get("content") or data.get("notice_content") or ""
    return clean_text(str(raw)), data


def flatten_notice(item: dict[str, Any]) -> dict[str, str]:
    code = ""
    name = ""
    for c in item.get("codes") or []:
        candidate = str(c.get("stock_code") or "")
        if len(candidate) == 6 and candidate.isdigit():
            code = candidate
            name = str(c.get("short_name") or "")
            break
    return {
        "security_code": code,
        "security_name": name,
        "art_code": str(item.get("art_code") or ""),
        "title": str(item.get("title") or ""),
        "notice_date": str(item.get("notice_date") or "")[:10],
    }


def choose_documents(notices: list[dict[str, Any]]) -> list[tuple[str, dict[str, str]]]:
    flat = [flatten_notice(n) for n in notices]
    flat = [x for x in flat if x["art_code"]]
    flat.sort(key=lambda x: x["notice_date"], reverse=True)
    chosen: list[tuple[str, dict[str, str]]] = []
    used: set[str] = set()
    for label in ("h1_2026", "q1_2026", "annual_2025"):
        for doc in flat:
            title = doc["title"]
            if "摘要" in title or "英文" in title or "取消" in title:
                continue
            if any(re.search(p, title) for p in DOC_PATTERNS[label]):
                chosen.append((label, doc))
                used.add(doc["art_code"])
                break
    ir_count = 0
    for doc in flat:
        if doc["art_code"] in used:
            continue
        if any(re.search(p, doc["title"]) for p in DOC_PATTERNS["ir"]):
            chosen.append(("ir", doc))
            used.add(doc["art_code"])
            ir_count += 1
            if ir_count >= 2:
                break
    event_patterns = "|".join(p for p, _ in [
        (r"重大资产重组", 0), (r"收购", 0), (r"控制权", 0), (r"回购", 0), (r"股权激励", 0),
        (r"重大合同", 0), (r"中标", 0), (r"投产", 0), (r"量产", 0), (r"定向增发", 0),
        (r"向特定对象发行", 0), (r"减持计划", 0), (r"立案调查", 0),
    ])
    event_count = 0
    for doc in flat:
        if doc["art_code"] in used:
            continue
        if re.search(event_patterns, doc["title"]):
            chosen.append(("event", doc))
            used.add(doc["art_code"])
            event_count += 1
            if event_count >= 4:
                break
    return chosen


def detect_new_level(sentences: list[str]) -> tuple[int, str]:
    new_context = [s for s in sentences if any(k in s for k in EVIDENCE_GROUPS["new_profit_pool"])]
    best_level = 0
    best_sentence = ""
    for sentence in new_context:
        for level, markers in NEW_LEVEL_RULES:
            if any(m in sentence for m in markers) and level > best_level:
                best_level = level
                best_sentence = sentence[:500]
    return best_level, best_sentence


def review_one(code: str, name: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "security_code": code, "security_name": name, "review_status": "completed",
        "documents_read": 0, "h1_report_found": 0, "q1_report_found": 0, "annual_report_found": 0,
        "ir_docs_found": 0, "event_docs_found": 0, "source_manifest": [],
    }
    try:
        notices = announcement_list(code)
        docs = choose_documents(notices)
        all_sentences: list[str] = []
        title_text = " ".join(flatten_notice(n)["title"] for n in notices[:80])
        for label, doc in docs:
            try:
                text, metadata = announcement_content(doc["art_code"])
            except Exception as exc:  # noqa: BLE001
                result["source_manifest"].append({**doc, "label": label, "error": str(exc)})
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            sentences = sentence_split(text)
            all_sentences.extend(sentences)
            result["source_manifest"].append({
                **doc, "label": label, "text_sha256": digest, "character_count": len(text),
                "sentence_count": len(sentences),
                "source_url": f"https://data.eastmoney.com/notices/detail/{code}/{doc['art_code']}.html",
            })
            result["documents_read"] += 1
            if label == "h1_2026": result["h1_report_found"] = 1
            elif label == "q1_2026": result["q1_report_found"] = 1
            elif label == "annual_2025": result["annual_report_found"] = 1
            elif label == "ir": result["ir_docs_found"] += 1
            elif label == "event": result["event_docs_found"] += 1

        grouped: dict[str, list[str]] = {}
        for group, keywords in EVIDENCE_GROUPS.items():
            if group == "milestone":
                grouped[group] = top_sentences(all_sentences, keywords, 8)
            else:
                grouped[group] = top_sentences(all_sentences, keywords, 10 if group == "risk" else 8)
            result[f"{group}_evidence"] = " || ".join(grouped[group])
            result[f"{group}_evidence_count"] = len(grouped[group])

        level, level_sentence = detect_new_level(all_sentences)
        result["new_profit_pool_evidence_level"] = level
        result["new_profit_pool_level_evidence"] = level_sentence
        numeric_evidence = sum(bool(re.search(r"\d+(?:\.\d+)?%|\d+(?:\.\d+)?亿", s)) for s in all_sentences)
        doc_coverage = result["h1_report_found"] * 12 + result["annual_report_found"] * 5 + result["q1_report_found"] * 3
        result["announcement_evidence_score"] = min(
            100,
            doc_coverage + level * 8 + len(grouped["growth"]) * 2 + len(grouped["milestone"]) * 1.5
            + min(numeric_evidence, 20) * 0.7,
        )
        risk_score = 0.0
        corpus = title_text + " " + " ".join(grouped["risk"])
        for marker, weight in RISK_WEIGHTS.items():
            risk_score += corpus.count(marker) * weight
        result["announcement_risk_score"] = min(risk_score, 100)
        result["positive_evidence_consistency"] = min(
            100,
            len(grouped["growth"]) * 7 + len(grouped["new_profit_pool"]) * 6 + len(grouped["cycle"]) * 3
            + result["h1_report_found"] * 15,
        )
    except Exception as exc:  # noqa: BLE001
        result["review_status"] = "failed"
        result["review_error"] = str(exc)
    result["source_manifest_json"] = json.dumps(result.pop("source_manifest"), ensure_ascii=False, separators=(",", ":"))
    return result


def industry_percentile(df: pd.DataFrame, col: str, higher: bool = True) -> pd.Series:
    values = pd.to_numeric(df[col], errors="coerce")
    result = pd.Series(index=df.index, dtype=float)
    for _, idx in df.groupby("industry", dropna=False).groups.items():
        block = values.loc[idx]
        if block.notna().sum() >= 5:
            rank = block.rank(pct=True, method="average")
            result.loc[idx] = rank if higher else 1 - rank
    fallback = values.rank(pct=True, method="average")
    if not higher:
        fallback = 1 - fallback
    return result.fillna(fallback).fillna(0.35).clip(0, 1)


def reasonable_pe(row: pd.Series, industry_median: float) -> float:
    model = str(row.get("primary_model"))
    if not np.isfinite(industry_median) or industry_median <= 0:
        industry_median = 24.0
    if model == "cyclical_turn":
        return float(np.clip(industry_median, 9, 20))
    if model == "capital_event":
        return float(np.clip(industry_median, 15, 35))
    if model == "new_profit_pool":
        return float(np.clip(industry_median, 22, 45))
    return float(np.clip(industry_median, 18, 40))


def doubling_math_score(gap: float) -> float:
    if not np.isfinite(gap): return 10.0
    if gap <= 0.25: return 100.0
    if gap <= 0.50: return 90.0
    if gap <= 0.80: return 78.0
    if gap <= 1.20: return 62.0
    if gap <= 1.80: return 43.0
    if gap <= 2.50: return 25.0
    return 10.0


def wave_stage(row: pd.Series) -> str:
    r60 = float(row.get("return_60d_pct") or 0)
    ry = float(row.get("return_ytd_pct") or 0)
    coverage = float(row.get("report_count_12m") or 0)
    evidence = float(row.get("new_profit_pool_evidence_level") or 0)
    if r60 > 90 or ry > 200 or (r60 > 60 and coverage > 12): return "emotion_or_late_diffusion"
    if r60 > 35 or coverage > 10: return "diffusion"
    if evidence >= 3 and -5 <= r60 <= 45: return "validation"
    return "discovery_or_unconfirmed"


def compact(value: Any, limit: int = 460) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def create_peer_context(reviewed: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for r in reviewed.itertuples(index=False):
        peers = universe[universe["industry"] == r.industry].copy()
        peers = peers.sort_values("convexity_score_stage1", ascending=False)
        top = peers[["security_code", "security_name", "convexity_score_stage1", "q2_net_profit_yoy_pct", "gross_margin_delta_pp", "cash_conversion"]].head(6)
        rows.append({
            "security_code": r.security_code,
            "peer_count": len(peers),
            "top_peer_context": " || ".join(
                f"{x.security_code}-{x.security_name}:S1={x.convexity_score_stage1:.1f},Q2NP={x.q2_net_profit_yoy_pct:.1f}%,GMΔ={x.gross_margin_delta_pp:.1f}pp,CF={x.cash_conversion:.2f}"
                for x in top.itertuples(index=False)
            ),
        })
    return pd.DataFrame(rows)


def refine(reviewed: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    df = reviewed.copy()
    for col, high in {
        "q2_revenue_yoy_pct": True, "q2_net_profit_yoy_pct": True, "q2_deduct_net_profit_yoy_pct": True,
        "gross_margin_delta_pp": True, "cash_conversion": True, "2026h1_roe": True,
        "accounts_receivable_yoy_pct": False, "inventory_yoy_pct": False, "debt_ratio_pct_calc": False,
        "pe_dynamic": False, "pb": False,
    }.items():
        universe[f"peer2_{col}"] = industry_percentile(universe, col, high)
        mapping = universe.set_index("security_code")[f"peer2_{col}"]
        df[f"peer2_{col}"] = df["security_code"].map(mapping).fillna(0.35)

    df["peer_superiority_score"] = 100 * (
        0.18 * df["peer2_q2_revenue_yoy_pct"] + 0.24 * df["peer2_q2_net_profit_yoy_pct"]
        + 0.10 * df["peer2_q2_deduct_net_profit_yoy_pct"] + 0.14 * df["peer2_gross_margin_delta_pp"]
        + 0.14 * df["peer2_cash_conversion"] + 0.10 * df["peer2_2026h1_roe"]
        + 0.05 * df["peer2_accounts_receivable_yoy_pct"] + 0.05 * df["peer2_inventory_yoy_pct"]
    )
    industry_median_pe = universe.groupby("industry")["pe_dynamic"].median()
    df["industry_median_pe"] = df["industry"].map(industry_median_pe)
    df["assumed_re_rating_pe"] = df.apply(lambda r: reasonable_pe(r, float(r["industry_median_pe"]) if pd.notna(r["industry_median_pe"]) else np.nan), axis=1)
    df["ttm_net_profit_cny"] = df["2025fy_net_profit"] - df["2025h1_net_profit"] + df["2026h1_net_profit"]
    df["required_profit_for_2x_cny"] = 2 * df["market_cap_cny"] / df["assumed_re_rating_pe"]
    df["required_profit_growth_for_2x"] = df["required_profit_for_2x_cny"] / df["ttm_net_profit_cny"].abs() - 1
    df.loc[df["ttm_net_profit_cny"] <= 2e7, "required_profit_growth_for_2x"] = np.nan
    df["doubling_math_score"] = df["required_profit_growth_for_2x"].apply(doubling_math_score)

    df["refined_fundamental_score"] = (
        0.72 * df["fundamental_acceleration_score"] + 0.16 * df["peer_superiority_score"]
        + 0.12 * df["announcement_evidence_score"] - 0.16 * df["announcement_risk_score"]
    ).clip(0, 100)
    df["refined_new_profit_pool_score"] = (
        0.55 * df["new_profit_pool_score_stage1"] + 0.18 * df["peer_superiority_score"]
        + 0.17 * df["announcement_evidence_score"] + 2.2 * df["new_profit_pool_evidence_level"]
        - 0.14 * df["announcement_risk_score"]
    ).clip(0, 100)
    df["refined_cycle_score"] = (
        0.68 * df["cyclical_turn_score"] + 0.18 * df["peer_superiority_score"]
        + 0.14 * df["announcement_evidence_score"] - 0.16 * df["announcement_risk_score"]
    ).clip(0, 100)
    df["refined_capital_event_score"] = (
        0.62 * df["capital_event_score"] + 0.19 * df["announcement_evidence_score"]
        + 0.10 * df["peer_superiority_score"] + 1.8 * df["event_positive_count"]
        - 0.20 * df["announcement_risk_score"] - 2.5 * df["event_negative_count"]
    ).clip(0, 100)
    refined_cols = ["refined_fundamental_score", "refined_new_profit_pool_score", "refined_cycle_score", "refined_capital_event_score"]
    vals = df[refined_cols].to_numpy(float)
    order = np.argsort(vals, axis=1)
    label_map = {
        0: "fundamental_acceleration", 1: "new_profit_pool", 2: "cyclical_turn", 3: "capital_event",
    }
    df["refined_primary_model"] = [label_map[i] for i in order[:, -1]]
    df["refined_primary_score"] = np.take_along_axis(vals, order[:, -1:], axis=1).ravel()
    df["refined_secondary_score"] = np.take_along_axis(vals, order[:, -2:-1], axis=1).ravel()

    downside = 100 * (
        0.24 * df["peer2_cash_conversion"] + 0.19 * df["peer2_2026h1_roe"]
        + 0.16 * df["peer2_debt_ratio_pct_calc"] + 0.10 * df["peer2_accounts_receivable_yoy_pct"]
        + 0.08 * df["peer2_inventory_yoy_pct"] + 0.13 * df["peer2_pe_dynamic"] + 0.10 * df["peer2_pb"]
    )
    downside -= np.where(df["goodwill_equity_ratio"] > 0.5, 12, 0)
    downside -= np.where(df["cash_conversion"] < 0, 15, 0)
    df["downside_anchor_score"] = downside.clip(0, 100)

    df["wave_stage"] = df.apply(wave_stage, axis=1)
    wave_score = df["wave_stage"].map({
        "validation": 100, "discovery_or_unconfirmed": 68, "diffusion": 48, "emotion_or_late_diffusion": 15,
    }).fillna(40)
    report_coverage = pd.to_numeric(df["report_count_12m"], errors="coerce").fillna(0)
    nonconsensus = (1 - report_coverage.clip(0, 20) / 20) * 100
    nonconsensus = 0.65 * nonconsensus + 0.35 * df["nonconsensus_proxy_score"]
    df["refined_nonconsensus_score"] = nonconsensus.clip(0, 100)

    df["final_convexity_score"] = (
        0.30 * df["refined_primary_score"] + 0.10 * df["refined_secondary_score"]
        + 0.15 * df["peer_superiority_score"] + 0.11 * df["announcement_evidence_score"]
        + 0.11 * df["doubling_math_score"] + 0.08 * df["refined_nonconsensus_score"]
        + 0.08 * df["downside_anchor_score"] + 0.07 * wave_score
        - 0.18 * df["announcement_risk_score"]
    ).clip(0, 100)
    return df


def select_final20(df: pd.DataFrame) -> pd.DataFrame:
    eligible = df[
        (df["review_status"] == "completed")
        & (df["h1_report_found"] == 1)
        & (df["market_cap_100m"].between(20, 1200, inclusive="both"))
        & (df["announcement_risk_score"] < 72)
        & (~df["wave_stage"].eq("emotion_or_late_diffusion"))
        & ((df["ttm_net_profit_cny"] > 2e7) | (df["refined_capital_event_score"] >= 72))
    ].sort_values("final_convexity_score", ascending=False)
    selected: list[pd.Series] = []
    industry_counts: dict[str, int] = {}
    for _, row in eligible.iterrows():
        industry = str(row["industry"])
        if industry_counts.get(industry, 0) >= 4:
            continue
        selected.append(row)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if len(selected) == 20:
            break
    final = pd.DataFrame(selected).copy()
    final["final_rank"] = np.arange(1, len(final) + 1)
    final["initial_position_class"] = np.select(
        [
            (final["final_convexity_score"] >= 76) & (final["announcement_risk_score"] < 30),
            final["final_convexity_score"] >= 66,
        ],
        ["A_round_1.0_to_1.5_pct", "seed_0.5_to_1.0_pct"],
        default="watch_only",
    )
    return final


def build_markdown(final: pd.DataFrame) -> str:
    lines = [
        "# A股2026H1全市场研究：最终20只高凸性候选", "",
        "> 由5,550家公司盲筛、四模型评分、前100家公告全文读取和同行比较产生。名单是研究候选，不是翻倍承诺。", "",
    ]
    summary_cols = [
        "final_rank", "security_code", "security_name", "industry", "refined_primary_model",
        "final_convexity_score", "peer_superiority_score", "announcement_evidence_score",
        "doubling_math_score", "wave_stage", "market_cap_100m", "pe_dynamic", "initial_position_class",
    ]
    lines.append(final[summary_cols].round(2).to_markdown(index=False))
    lines.append("")
    for r in final.sort_values("final_rank").itertuples(index=False):
        lines.extend([
            f"## {r.final_rank}. {r.security_name}（{r.security_code}）", "",
            f"- **主模型：** {r.refined_primary_model}；最终分 {r.final_convexity_score:.1f}；同行优势 {r.peer_superiority_score:.1f}。",
            f"- **估值与翻倍数学：** 市值约 {r.market_cap_100m:.1f} 亿元，动态PE {r.pe_dynamic:.1f}；压力测试采用PE {r.assumed_re_rating_pe:.1f}，实现2倍市值所需利润增幅约 {r.required_profit_growth_for_2x * 100 if pd.notna(r.required_profit_growth_for_2x) else float('nan'):.1f}%。",
            f"- **经营证据：** Q2营收同比 {r.q2_revenue_yoy_pct:.1f}%，Q2归母净利润同比 {r.q2_net_profit_yoy_pct:.1f}%，毛利率同比变化 {r.gross_margin_delta_pp:.1f}个百分点，经营现金/净利润 {r.cash_conversion:.2f}。",
            f"- **新利润池证据等级：** Level {int(r.new_profit_pool_evidence_level)}。{compact(r.new_profit_pool_level_evidence)}",
            f"- **公告中的关键证据：** {compact(r.growth_evidence, 700)}",
            f"- **未来里程碑：** {compact(r.milestone_evidence, 600)}",
            f"- **主要反证与风险：** {compact(r.risk_evidence, 600)}",
            f"- **市场波次：** {r.wave_stage}；近12个月研报约 {int(r.report_count_12m)} 篇；初始仓位级别：{r.initial_position_class}。",
            f"- **同行上下文：** {compact(r.top_peer_context, 650)}", "",
        ])
    return "\n".join(lines) + "\n"


def main() -> int:
    DOSSIER_DIR.mkdir(parents=True, exist_ok=True)
    if not TOP100.exists() or not UNIVERSE.exists():
        raise FileNotFoundError("Stage 1 outputs are missing")
    top100 = pd.read_csv(TOP100, dtype={"security_code": str}, low_memory=False)
    universe = pd.read_csv(UNIVERSE, dtype={"security_code": str}, low_memory=False)
    top100["security_code"] = top100["security_code"].astype(str).str.zfill(6)
    universe["security_code"] = universe["security_code"].astype(str).str.zfill(6)

    review_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(review_one, str(row.security_code), str(row.security_name)): str(row.security_code)
            for row in top100[["security_code", "security_name"]].itertuples(index=False)
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                review_results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                review_results.append({"security_code": code, "review_status": "failed", "review_error": str(exc)})
    reviews = pd.DataFrame(review_results)
    reviews.to_json(DOSSIER_DIR / "top100_announcement_review.jsonl", orient="records", lines=True, force_ascii=False)
    reviewed = top100.merge(reviews, on=["security_code", "security_name"], how="left")
    peer_context = create_peer_context(reviewed, universe)
    reviewed = reviewed.merge(peer_context, on="security_code", how="left")
    refined = refine(reviewed, universe)
    refined = refined.sort_values("final_convexity_score", ascending=False).reset_index(drop=True)
    refined["reviewed_rank"] = np.arange(1, len(refined) + 1)
    final = select_final20(refined)

    refined.to_csv(RESEARCH_DIR / "top100_reviewed_peer_compared.csv", index=False, encoding="utf-8-sig")
    final.to_csv(RESEARCH_DIR / "final20_high_convexity.csv", index=False, encoding="utf-8-sig")
    (RESEARCH_DIR / "final20_high_convexity.md").write_text(build_markdown(final), encoding="utf-8")

    summary = {
        "stage": "stage2_announcement_review_peer_comparison_final20",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_universe_count": len(universe),
        "review_pool_count": len(top100),
        "completed_reviews": int((reviews.get("review_status") == "completed").sum()),
        "failed_reviews": int((reviews.get("review_status") == "failed").sum()),
        "h1_reports_found": int(pd.to_numeric(reviews.get("h1_report_found"), errors="coerce").fillna(0).sum()),
        "documents_read": int(pd.to_numeric(reviews.get("documents_read"), errors="coerce").fillna(0).sum()),
        "final_candidate_count": len(final),
        "final_model_counts": final["refined_primary_model"].value_counts().to_dict(),
        "final_industry_counts": final["industry"].value_counts().to_dict(),
        "method_notes": [
            "All 100 companies were selected before announcement text was read.",
            "Source identifiers and SHA-256 hashes are retained for auditability.",
            "No prior-conversation-frequency feature is present.",
            "Industry peer ranks are computed from the entire 5,550-company scored universe.",
            "The final 20 are research candidates, not guaranteed winners or personalized trade instructions.",
        ],
    }
    (META_DIR / "research_stage2_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
