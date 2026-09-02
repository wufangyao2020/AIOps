#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "meta" / "research_endpoint_diagnostics.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36",
    "Referer": "https://data.eastmoney.com/notices/",
    "Accept": "application/json,text/plain,*/*",
}


def get(url, params):
    r = requests.get(url, params=params, headers={**HEADERS, "Connection": "close"}, timeout=30)
    r.raise_for_status()
    return r.json()


def compact_notice(item):
    codes = [
        {
            "stock_code": x.get("stock_code"),
            "short_name": x.get("short_name"),
            "ann_type": x.get("ann_type"),
        }
        for x in (item.get("codes") or [])[:3]
    ]
    return {
        "title": item.get("title"),
        "notice_date": str(item.get("notice_date") or "")[:10],
        "art_code": item.get("art_code"),
        "columns": [x.get("column_name") for x in (item.get("columns") or [])],
        "codes": codes,
    }


def main():
    result = {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "events": {},
        "quotes": {},
        "reports": {},
    }
    notice = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    common = {
        "sr": -1,
        "page_size": 5,
        "ann_type": "A",
        "client_source": "web",
        "s_node": 0,
        "begin_time": "2026-08-01",
        "end_time": "2026-09-02",
    }
    for node in range(1, 11):
        entry = {}
        for page in (1, 2):
            try:
                payload = get(notice, {**common, "page_index": page, "f_node": node})
                data = payload.get("data") or {}
                entry[f"page_{page}"] = {
                    "total_hits": data.get("total_hits"),
                    "items": [compact_notice(x) for x in (data.get("list") or [])[:5]],
                }
            except Exception as exc:
                entry[f"page_{page}"] = {"error": str(exc)}
        result["events"][str(node)] = entry

    # Probe the same node without f_node, which may represent the complete notice pool.
    for label, extra in {
        "all_no_node": {},
        "stock_sample_600519": {"stock_list": "600519"},
    }.items():
        try:
            payload = get(notice, {**common, **extra, "page_index": 1})
            data = payload.get("data") or {}
            result["events"][label] = {
                "total_hits": data.get("total_hits"),
                "items": [compact_notice(x) for x in (data.get("list") or [])[:5]],
            }
        except Exception as exc:
            result["events"][label] = {"error": str(exc)}

    for host in ("https://push2.eastmoney.com", "https://7.push2.eastmoney.com", "https://82.push2.eastmoney.com"):
        try:
            payload = get(host + "/api/qt/clist/get", {
                "pn": 1, "pz": 10, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2,
                "fid": "f12", "fs": "m:0 t:6,m:0 t:80", "fields": "f2,f12,f14,f20,f9,f23",
            })
            data = payload.get("data") or {}
            result["quotes"][host] = {"total": data.get("total"), "rows": len(data.get("diff") or [])}
        except Exception as exc:
            result["quotes"][host] = {"error": str(exc)}
    try:
        r = requests.get(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
            params={"page": 1, "num": 10, "sort": "symbol", "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "auto"},
            headers={"User-Agent": HEADERS["User-Agent"], "Referer": "https://vip.stock.finance.sina.com.cn/"},
            timeout=30,
        )
        result["quotes"]["sina"] = {"status": r.status_code, "text_prefix": r.text[:120], "content_length": len(r.text)}
    except Exception as exc:
        result["quotes"]["sina"] = {"error": str(exc)}
    try:
        payload = get("https://reportapi.eastmoney.com/report/list", {
            "industryCode": "*", "pageSize": 10, "industry": "*", "rating": "*", "ratingChange": "*",
            "beginTime": "2025-09-01", "endTime": "2026-09-03", "pageNo": 1, "fields": "", "qType": 0,
            "orgCode": "", "code": "", "rcode": "", "p": 1, "pageNum": 1, "pageNumber": 1,
        })
        result["reports"] = {
            "total_page": payload.get("TotalPage"),
            "total_count": payload.get("TotalCount"),
            "rows": len(payload.get("data") or []),
        }
    except Exception as exc:
        result["reports"] = {"error": str(exc)}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
