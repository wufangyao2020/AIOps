#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "meta" / "research_endpoint_diagnostics.json"
HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/124 Safari/537.36", "Referer": "https://data.eastmoney.com/"}


def get(url, params):
    r = requests.get(url, params=params, headers={**HEADERS, "Connection": "close"}, timeout=30)
    r.raise_for_status()
    return r.json()


def main():
    result = {"as_of_utc": datetime.now(timezone.utc).isoformat(), "events": {}, "quotes": {}, "reports": {}}
    notice = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    for node in (2, 4, 5, 6, 7):
        try:
            payload = get(notice, {"sr": -1, "page_size": 1, "page_index": 1, "ann_type": "A", "client_source": "web", "f_node": node, "s_node": 0, "begin_time": "2026-01-01", "end_time": "2026-09-02"})
            data = payload.get("data") or {}
            result["events"][str(node)] = {"total_hits": data.get("total_hits"), "first_title": ((data.get("list") or [{}])[0].get("title"))}
        except Exception as exc:
            result["events"][str(node)] = {"error": str(exc)}
    for host in ("https://push2.eastmoney.com", "https://7.push2.eastmoney.com", "https://82.push2.eastmoney.com"):
        try:
            payload = get(host + "/api/qt/clist/get", {"pn": 1, "pz": 10, "po": 1, "np": 1, "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2, "fid": "f12", "fs": "m:0 t:6,m:0 t:80", "fields": "f2,f12,f14,f20,f9,f23"})
            data = payload.get("data") or {}
            result["quotes"][host] = {"total": data.get("total"), "rows": len(data.get("diff") or [])}
        except Exception as exc:
            result["quotes"][host] = {"error": str(exc)}
    try:
        r = requests.get("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData", params={"page": 1, "num": 10, "sort": "symbol", "asc": 1, "node": "hs_a", "symbol": "", "_s_r_a": "auto"}, headers={"User-Agent": HEADERS["User-Agent"], "Referer": "https://vip.stock.finance.sina.com.cn/"}, timeout=30)
        result["quotes"]["sina"] = {"status": r.status_code, "text_prefix": r.text[:120], "content_length": len(r.text)}
    except Exception as exc:
        result["quotes"]["sina"] = {"error": str(exc)}
    try:
        payload = get("https://reportapi.eastmoney.com/report/list", {"industryCode": "*", "pageSize": 10, "industry": "*", "rating": "*", "ratingChange": "*", "beginTime": "2025-09-01", "endTime": "2026-09-03", "pageNo": 1, "fields": "", "qType": 0, "orgCode": "", "code": "", "rcode": "", "p": 1, "pageNum": 1, "pageNumber": 1})
        result["reports"] = {"total_page": payload.get("TotalPage"), "total_count": payload.get("TotalCount"), "rows": len(payload.get("data") or [])}
    except Exception as exc:
        result["reports"] = {"error": str(exc)}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
