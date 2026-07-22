#!/usr/bin/env python
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import unicodedata
import zipfile
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "data_catalog" / "etf_pit_master.csv"
CACHE = ROOT / "data_catalog" / "cache" / "etf_master"
OUTPUT = ROOT / "outputs" / "audit" / "etf_pit_master"
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://fund.szse.cn/api/report/ShowReport"
SZSE_PCF_LIST_URL = "https://www.szse.cn/api/report/ShowReport/data"
SZSE_PCF_ROOT = "https://reportdocs.static.szse.cn/files/text/ETFDown"
SSE_INDEX_URL = "https://query.sse.com.cn/commonSoaQuery.do"
CSI_INDEX_URL = "https://www.csindex.com.cn/csindex-home/index-list/query-index-item"
CSI_PRODUCT_URL = "https://www.csindex.com.cn/csindex-home/index-list/funds-tracking-index"
HEADERS = {"User-Agent": "Mozilla/5.0"}
FIELDS = [
    "snapshot_date", "available_date", "etf_code", "exchange", "fund_name",
    "fund_type", "investment_type", "tracked_index_code", "tracked_index_name",
    "list_date", "delist_date", "manager", "scale_cny_100m", "source",
    "source_url", "mapping_status", "mapping_source", "eligible_stock_etf", "record_hash",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="构建境内 ETF 官方 PIT 主表。")
    parser.add_argument("--snapshot-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    snapshot_date = date.fromisoformat(args.snapshot_date)
    summary, rows = refresh_snapshot(snapshot_date)
    write_audit(rows, summary)
    print(f"snapshot_rows={len(rows)}")
    print(f"eligible_stock_etf_count={summary['eligible_stock_etf_count']}")
    print(f"exact_index_code_coverage={summary['exact_index_code_coverage']:.4f}")
    print(f"pit_master_ready={str(summary['pit_master_ready']).lower()}")
    if not summary["refresh_accepted"] and not rows:
        raise SystemExit(f"ETF PIT refresh rejected: {summary['refresh_reason']}")


def fetch_snapshot(snapshot_date: date) -> tuple[list[dict[str, Any]], list[Path]]:
    raw_dir = CACHE / snapshot_date.isoformat()
    raw_dir.mkdir(parents=True, exist_ok=True)
    sse_response = requests.get(
        SSE_URL,
        params={"sqlId": "COMMON_JJZWZ_JJLB_L", "CATEGORY": "F110", "type": "inParams"},
        headers={**HEADERS, "Referer": "https://etf.sse.com.cn/fundlist/"},
        timeout=60,
    )
    sse_response.raise_for_status()
    sse_raw = raw_dir / "sse_stock_etf.json"
    sse_raw.write_bytes(sse_response.content)

    szse_response = requests.get(
        SZSE_URL,
        params={"SHOWTYPE": "xlsx", "CATALOGID": "1000_lf", "TABKEY": "tab1"},
        headers={**HEADERS, "Referer": "https://fund.szse.cn/marketdata/fundslist/index.html"},
        timeout=60,
    )
    szse_response.raise_for_status()
    szse_raw = raw_dir / "szse_fund_list.xlsx"
    szse_raw.write_bytes(szse_response.content)

    sse_records = sse_response.json().get("result", [])
    szse_frame = pd.read_excel(BytesIO(szse_response.content), engine="openpyxl", dtype=str)
    index_map, product_map, index_raw = fetch_official_index_catalog(raw_dir)
    szse_codes = set(szse_frame.iloc[:, 0].dropna().astype(str).str.zfill(6))
    szse_index_map, pcf_zip = fetch_szse_pcf_headers(szse_codes - set(product_map), raw_dir)
    rows = normalize_sse(sse_records, snapshot_date, index_map, product_map)
    rows.extend(normalize_szse(szse_frame, snapshot_date, product_map | szse_index_map, set(product_map)))
    return rows, [sse_raw, szse_raw, index_raw, pcf_zip]


def refresh_snapshot(
    snapshot_date: date,
    *,
    fetcher: Callable[[date], tuple[list[dict[str, Any]], list[Path]]] = fetch_snapshot,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    network_error = ""
    try:
        fetched_rows, raw_files = fetcher(snapshot_date)
    except requests.RequestException as exc:
        fetched_rows, raw_files = [], []
        network_error = f"{type(exc).__name__}: {exc}"

    merged, rows, refresh_accepted, refresh_reason = append_snapshot(fetched_rows)
    if network_error:
        reason_suffix = {
            "degraded_refresh_last_good_retained": "last_good_retained",
            "degraded_refresh_same_date_retained": "same_date_retained",
            "no_qualified_snapshot_available": "no_qualified_snapshot_available",
        }.get(refresh_reason, refresh_reason)
        refresh_reason = f"network_failure_{reason_suffix}"
        refresh_accepted = False
    effective_date = date.fromisoformat(rows[0]["snapshot_date"]) if rows else snapshot_date
    summary = build_summary(rows, merged, effective_date, raw_files)
    summary.update(
        {
            "requested_snapshot_date": snapshot_date.isoformat(),
            "refresh_accepted": refresh_accepted,
            "refresh_reason": refresh_reason,
            "network_fetch_succeeded": not network_error,
            "network_error": network_error,
        }
    )
    return summary, rows


def normalize_sse(records: list[dict[str, Any]], snapshot_date: date, index_map: dict[str, str], product_map: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for item in records:
        category = clean(item.get("CATEGORY"))
        name = clean(item.get("FUND_EXPANSION_ABBR")) or clean(item.get("FUND_ABBR"))
        eligible = category in {"F111", "F112", "F114", "F115"} and not excluded_name(name)
        index_name = clean(item.get("INDEX_NAME"))
        code = clean(item.get("FUND_CODE")).zfill(6)
        index_code = product_map.get(code) or index_map.get(normalize_index_name(index_name), "")
        row = base_row(
            snapshot_date=snapshot_date,
            code=code,
            exchange="SSE",
            name=name,
            fund_type=category,
            investment_type="境内股票ETF" if eligible else "股票ETF其他类别",
            index_code=index_code,
            index_name=index_name,
            list_date=clean(item.get("LISTING_DATE")),
            manager=clean(item.get("COMPANY_NAME")),
            scale=to_number(item.get("SCALE")),
            source="上海证券交易所ETF列表",
            source_url="https://etf.sse.com.cn/fundlist/",
            eligible=eligible,
            mapping_source="official_csi_product_catalog" if code in product_map else ("official_unique_name_match" if index_code else ""),
        )
        rows.append(row)
    return rows


def normalize_szse(frame: pd.DataFrame, snapshot_date: date, index_map: dict[str, str], product_codes: set[str]) -> list[dict[str, Any]]:
    # 深交所 XLSX 在部分 openpyxl/控制台组合中会显示乱码，列位置是官方固定合同。
    frame = frame.copy()
    frame.columns = [
        "fund_code", "fund_name", "fund_type", "investment_type", "list_date",
        "shares", "manager", "sponsor", "custodian", "nav",
    ]
    rows = []
    for item in frame.to_dict("records"):
        name = clean(item.get("fund_name"))
        fund_type = clean(item.get("fund_type"))
        investment_type = clean(item.get("investment_type"))
        eligible = fund_type == "ETF" and investment_type == "股票基金" and not excluded_name(name)
        code = clean(item.get("fund_code")).zfill(6)
        rows.append(base_row(
            snapshot_date=snapshot_date,
            code=code,
            exchange="SZSE",
            name=name,
            fund_type=fund_type,
            investment_type=investment_type,
            index_code=index_map.get(clean(item.get("fund_code")).zfill(6), ""),
            index_name="",
            list_date=clean(item.get("list_date"))[:10],
            manager=clean(item.get("manager")),
            scale=None,
            source="深圳证券交易所基金列表",
            source_url="https://fund.szse.cn/marketdata/fundslist/index.html",
            eligible=eligible,
            mapping_source="official_csi_product_catalog" if code in product_codes else ("official_pcf_header" if code in index_map else ""),
        ))
    return rows


def base_row(*, snapshot_date: date, code: str, exchange: str, name: str, fund_type: str,
             investment_type: str, index_name: str, list_date: str, manager: str,
             scale: float | None, source: str, source_url: str, eligible: bool,
             index_code: str = "", mapping_source: str = "") -> dict[str, Any]:
    mapping_status = "exact_index_code" if index_code else ("index_name_only" if index_name else "missing_index_identity")
    row = {
        "snapshot_date": snapshot_date.isoformat(),
        "available_date": snapshot_date.isoformat(),
        "etf_code": code.zfill(6),
        "exchange": exchange,
        "fund_name": name,
        "fund_type": fund_type,
        "investment_type": investment_type,
        "tracked_index_code": index_code,
        "tracked_index_name": index_name,
        "list_date": list_date,
        "delist_date": "",
        "manager": manager,
        "scale_cny_100m": scale,
        "source": source,
        "source_url": source_url,
        "mapping_status": mapping_status,
        "mapping_source": mapping_source,
        "eligible_stock_etf": bool(eligible),
    }
    row["record_hash"] = fingerprint(row)
    return row


def fetch_official_index_catalog(raw_dir: Path) -> tuple[dict[str, str], dict[str, str], Path]:
    sse = requests.get(
        SSE_INDEX_URL,
        params={"isPagination": "false", "sqlId": "DB_SZZSLB_ZSLB"},
        headers={**HEADERS, "Referer": "https://www.sse.com.cn/market/sseindex/indexlist/"},
        timeout=60,
    )
    sse.raise_for_status()
    csi = requests.post(
        CSI_INDEX_URL,
        json={"sorter": {"sortField": "null", "sortOrder": None}, "pager": {"pageNum": 1, "pageSize": 10000}, "searchInput": None, "indexFilter": {}},
        headers={**HEADERS, "Referer": "https://www.csindex.com.cn/"},
        timeout=60,
    )
    csi.raise_for_status()
    products = requests.post(
        CSI_PRODUCT_URL,
        json={"lang": "cn", "pager": {"pageNum": 1, "pageSize": 10000}, "searchInput": None, "sortField": None, "sortOrder": None, "fundsFilter": {}},
        headers={**HEADERS, "Referer": "https://www.csindex.com.cn/"},
        timeout=60,
    )
    products.raise_for_status()
    catalog = []
    for item in sse.json().get("result", []):
        code = clean(item.get("indexCode"))
        for key in ("indexFullName", "indexName"):
            if code and clean(item.get(key)):
                catalog.append({"name": clean(item.get(key)), "code": code, "source": "SSE"})
    for item in csi.json().get("data", []):
        code = clean(item.get("indexCode"))
        if code and clean(item.get("indexName")):
            catalog.append({"name": clean(item.get("indexName")), "code": code, "source": "CSI"})
    grouped: dict[str, set[str]] = {}
    for item in catalog:
        grouped.setdefault(normalize_index_name(item["name"]), set()).add(item["code"])
    unique = {name: next(iter(codes)) for name, codes in grouped.items() if name and len(codes) == 1}
    product_rows = products.json().get("data", [])
    product_grouped: dict[str, set[str]] = {}
    for item in product_rows:
        code = clean(item.get("productCode")).zfill(6)
        index_code = clean(item.get("indexCode"))
        if re.fullmatch(r"\d{6}", code) and clean(item.get("fundType")) == "ETF" and index_code:
            product_grouped.setdefault(code, set()).add(index_code)
    product_map = {code: next(iter(values)) for code, values in product_grouped.items() if len(values) == 1}
    raw_path = raw_dir / "official_index_catalog.json"
    raw_path.write_text(json.dumps({"indexes": catalog, "tracking_products": product_rows}, ensure_ascii=False), encoding="utf-8")
    return unique, product_map, raw_path


def fetch_szse_pcf_headers(codes: set[str], raw_dir: Path) -> tuple[dict[str, str], Path]:
    headers = {**HEADERS, "Referer": "https://www.szse.cn/disclosure/fund/currency/index.html"}
    first = requests.get(SZSE_PCF_LIST_URL, params={"CATALOGID": "sgshqd", "loading": "first", "txtJCorDH": "", "PAGENO": 1}, headers=headers, timeout=60)
    first.raise_for_status()
    payload = first.json()[0]
    pages = int(payload.get("metadata", {}).get("pagecount", 1))
    rows = list(payload.get("data", []))
    for page in range(2, pages + 1):
        response = requests.get(SZSE_PCF_LIST_URL, params={"CATALOGID": "sgshqd", "loading": "first", "txtJCorDH": "", "PAGENO": page}, headers=headers, timeout=60)
        response.raise_for_status()
        rows.extend(response.json()[0].get("data", []))
    downloads = {}
    for row in rows:
        html = str(row.get("jjdm") or "")
        for encoded in re.findall(r"filename=([^&'\"\s>]+)", html):
            names = unquote(encoded).split(";")
            pcf = next((name for name in names if name.lower().startswith("pcf_")), "")
            match = re.search(r"pcf_(\d{6})_", pcf, re.I)
            if match and match.group(1) in codes:
                downloads[match.group(1)] = f"{SZSE_PCF_ROOT}/{pcf}.xml"

    def fetch(item: tuple[str, str]) -> tuple[str, str, bytes, str]:
        code, url = item
        try:
            response = requests.get(url, headers=headers, timeout=60)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            ns = {"f": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
            path = "f:UnderlyingSecurityID" if ns else "UnderlyingSecurityID"
            index_code = clean(root.findtext(path, default="", namespaces=ns))
            return code, index_code, response.content, ""
        except Exception as exc:
            return code, "", b"", f"{type(exc).__name__}: {exc}"

    mapped: dict[str, str] = {}
    failures = []
    archive = raw_dir / "szse_pcf_headers.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for code, index_code, content, error in pool.map(fetch, downloads.items()):
                if index_code:
                    mapped[code] = index_code
                    zipped.writestr(f"{code}.xml", content)
                if error:
                    failures.append({"etf_code": code, "url": downloads[code], "error": error})
        zipped.writestr("manifest.json", json.dumps({"download_count": len(downloads), "mapped_count": len(mapped), "failures": failures}, ensure_ascii=False, indent=2))
    return mapped, archive


def normalize_index_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", clean(value))
    value = re.sub(r"\s+", "", value)
    return value[:-2] if value.endswith("指数") else value


def append_snapshot(rows: list[dict[str, Any]]) -> tuple[pd.DataFrame, list[dict[str, Any]], bool, str]:
    current = pd.read_csv(MASTER, dtype=str, keep_default_na=False).reindex(columns=FIELDS, fill_value="") if MASTER.exists() else pd.DataFrame(columns=FIELDS)
    incoming = pd.DataFrame(rows, columns=FIELDS).fillna("")
    effective, accepted, reason = select_effective_snapshot(current, incoming)
    incoming_date = str(incoming["snapshot_date"].iloc[0]) if len(incoming) else ""
    if accepted:
        kept = current[current["snapshot_date"].ne(incoming_date)] if incoming_date else current
        merged = pd.concat([kept, incoming], ignore_index=True)
    else:
        merged = current.copy()
    merged = merged.drop_duplicates(["snapshot_date", "exchange", "etf_code"], keep="last")
    merged = merged.sort_values(["snapshot_date", "exchange", "etf_code"])
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    temporary = MASTER.with_suffix(MASTER.suffix + ".tmp")
    merged.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(MASTER)
    return merged, effective.to_dict("records"), accepted, reason


def select_effective_snapshot(current: pd.DataFrame, incoming: pd.DataFrame) -> tuple[pd.DataFrame, bool, str]:
    qualified = []
    for snapshot_date, frame in current.groupby("snapshot_date"):
        metrics = snapshot_metrics(frame)
        if metrics["coverage"] >= 0.95:
            qualified.append((snapshot_date, frame, metrics))
    baseline = max(qualified, key=lambda item: item[0]) if qualified else None
    incoming_metrics = snapshot_metrics(incoming)
    size_ok = baseline is None or (incoming_metrics["rows"] >= baseline[2]["rows"] * 0.95 and incoming_metrics["eligible"] >= baseline[2]["eligible"] * 0.95)
    if incoming_metrics["coverage"] >= 0.95 and size_ok:
        return incoming, True, "accepted"
    incoming_date = str(incoming["snapshot_date"].iloc[0]) if len(incoming) else ""
    same_date = current[current["snapshot_date"].eq(incoming_date)]
    if len(same_date) and snapshot_metrics(same_date)["coverage"] >= 0.95:
        return same_date, False, "degraded_refresh_same_date_retained"
    if baseline:
        return baseline[1], False, "degraded_refresh_last_good_retained"
    return incoming.iloc[0:0], False, "no_qualified_snapshot_available"


def snapshot_metrics(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty:
        return {"rows": 0, "eligible": 0, "coverage": 0.0}
    eligible = frame["eligible_stock_etf"].astype(str).str.lower().eq("true")
    exact = eligible & frame["mapping_status"].eq("exact_index_code")
    count = int(eligible.sum())
    return {"rows": len(frame), "eligible": count, "coverage": float(exact.sum() / count) if count else 0.0}


def build_summary(rows: list[dict[str, Any]], merged: pd.DataFrame, snapshot_date: date, raw_files: list[Path]) -> dict[str, Any]:
    eligible = [row for row in rows if is_true(row["eligible_stock_etf"])]
    exact = [row for row in eligible if row["mapping_status"] == "exact_index_code"]
    coverage = len(exact) / len(eligible) if eligible else 0.0
    return {
        "version": "etf-pit-master-1.0",
        "policy_status": "research_only",
        "snapshot_date": snapshot_date.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_row_count": len(rows),
        "historical_row_count": len(merged),
        "exchange_count": len({row["exchange"] for row in rows}),
        "eligible_stock_etf_count": len(eligible),
        "exact_index_code_count": len(exact),
        "exact_index_code_coverage": coverage,
        "lifecycle_coverage": sum(bool(row["list_date"]) for row in rows) / len(rows) if rows else 0.0,
        "raw_snapshot_count": len(raw_files),
        "raw_snapshot_hashes": {path.name: sha256(path) for path in raw_files},
        "current_mapping_ready": bool(eligible) and coverage >= 0.95,
        "historical_pit_ready": merged["snapshot_date"].nunique() >= 60,
        "pit_master_ready": bool(eligible) and coverage >= 0.95 and merged["snapshot_date"].nunique() >= 60,
        "blocking_reasons": (["跟踪指数代码覆盖率低于95%"] if coverage < 0.95 else []) + (["PIT日快照少于60个，尚不能证明历史ETF宇宙可重建"] if merged["snapshot_date"].nunique() < 60 else []),
        "production_ready": False,
    }


def write_audit(rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    debug = OUTPUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows, columns=FIELDS).to_csv(debug / "snapshot_rows.csv", index=False, encoding="utf-8-sig")
    candidates = pd.DataFrame([row for row in rows if is_true(row["eligible_stock_etf"])], columns=FIELDS)
    candidates.to_csv(OUTPUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    status = "未通过" if not summary["pit_master_ready"] else "通过"
    report = f"""# ETF PIT 主表审计

审计状态：**{status}**。当前主表可保存官方 ETF 生命周期快照，但跟踪指数代码覆盖不足，不能用于历史精确映射或买卖建议。

- 快照日期：{summary['snapshot_date']}
- 快照记录：{summary['snapshot_row_count']}
- 股票 ETF 候选：{summary['eligible_stock_etf_count']}
- 跟踪指数代码精确覆盖率：{summary['exact_index_code_coverage']:.2%}
- 上市日期覆盖率：{summary['lifecycle_coverage']:.2%}
- PIT 主表就绪：`{str(summary['pit_master_ready']).lower()}`
- 本次刷新接受：`{str(summary.get('refresh_accepted', True)).lower()}`
- 刷新质量状态：`{summary.get('refresh_reason', 'accepted')}`

边界：不使用 ETF 名称猜测指数代码；缺少官方指数代码时保持硬阻断。
"""
    (OUTPUT / "report.md").write_text(report, encoding="utf-8")


def excluded_name(name: str) -> bool:
    return any(token in name.upper() for token in ("QDII", "主动", "增强", "联接", "杠杆", "反向"))


def is_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def clean(value: Any) -> str:
    return "" if pd.isna(value) else str(value).strip()


def to_number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fingerprint(row: dict[str, Any]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def self_check() -> None:
    sample = base_row(snapshot_date=date(2026, 7, 11), code="510300", exchange="SSE", name="测试ETF",
                      fund_type="F111", investment_type="境内股票ETF", index_name="沪深300指数",
                      list_date="2012-05-28", manager="测试", scale=1.0, source="official",
                      source_url="https://example.invalid", eligible=True)
    assert sample["mapping_status"] == "index_name_only"
    assert sample["tracked_index_code"] == ""
    assert not excluded_name("沪深300ETF") and excluded_name("主动ETF")
    assert normalize_index_name(" 沪深３００指数 ") == "沪深300"
    summary = build_summary([sample], pd.DataFrame([sample]), date(2026, 7, 11), [])
    assert not summary["pit_master_ready"]
    good = pd.DataFrame([{**sample, "snapshot_date": "2026-07-11", "mapping_status": "exact_index_code", "tracked_index_code": "000300"}])
    bad = pd.DataFrame([{**sample, "snapshot_date": "2026-07-13"}])
    effective, accepted, reason = select_effective_snapshot(good, bad)
    assert not accepted and reason == "degraded_refresh_last_good_retained" and effective.iloc[0]["snapshot_date"] == "2026-07-11"
    print("self_check=pass")


if __name__ == "__main__":
    main()
