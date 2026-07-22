#!/usr/bin/env python
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "audit" / "official_etf_lifecycle_sources"
CACHE = ROOT / "data_catalog" / "cache" / "etf_lifecycle_announcements"
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://www.szse.cn/api/search/content"
HEADERS = {"User-Agent": "Mozilla/5.0"}
CODE_PATTERN = re.compile(r"(?<!\d)(159\d{3}|51\d{4}|530\d{3}|56\d{4}|58[89]\d{3})(?!\d)")
EVENTS = {"listing": "\u4e0a\u5e02\u4ea4\u6613\u516c\u544a\u4e66", "delisting": "\u7ec8\u6b62\u4e0a\u5e02"}


def main() -> None:
    parser = argparse.ArgumentParser(description="\u5ba1\u8ba1\u4e0a\u4ea4\u6240\u3001\u6df1\u4ea4\u6240 ETF \u5386\u53f2\u751f\u547d\u5468\u671f\u6570\u636e\u6e90\u3002")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--refresh-observed-ranges", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    as_of = date.fromisoformat(args.as_of_date)
    cache_dir = CACHE / as_of.isoformat()
    cache_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for event_type, keyword in EVENTS.items():
        records.extend(fetch_sse(as_of, event_type, keyword, cache_dir, args.refresh))
        records.extend(fetch_szse(event_type, keyword, cache_dir, args.refresh))
    frame = pd.DataFrame(records).drop_duplicates(
        ["event_type", "exchange", "announcement_date", "etf_code", "source_url"]
    ).sort_values(["event_type", "exchange", "announcement_date", "etf_code", "title"])
    frame, pdf_code_audit = fill_pdf_codes(frame, cache_dir, args.refresh)
    frame, title_prefix_fill_count = fill_unique_title_prefix_codes(frame)
    inventory = build_lifecycle_inventory(frame, fetch_sse_current_categories(cache_dir, args.refresh), as_of)
    inventory = add_observed_trade_ranges(inventory, args.refresh_observed_ranges)
    summary = summarize(frame, inventory, pdf_code_audit, title_prefix_fill_count, current_target_master_stats(), as_of)
    write_outputs(frame, inventory, pdf_code_audit, summary)
    print(f"announcement_count={summary['announcement_count']}")
    print(f"unique_etf_code_count={summary['unique_etf_code_count']}")
    print(f"lifecycle_reconstruction_ready={str(summary['lifecycle_reconstruction_ready']).lower()}")


def fetch_sse(as_of: date, event_type: str, keyword: str, cache_dir: Path, refresh: bool) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "sqlId": "COMMON_PL_JJXX_JJGG_L",
        "isPagination": "true",
        "type": "inParams",
        "SECURITY_CODE": "",
        "ORG_BULLETIN_TYPE": "",
        "TITLE": keyword,
        "OTHER_TYPE": "",
        "START_DATE": "2000-01-01",
        "END_DATE": as_of.isoformat(),
        "pageHelp.pageSize": "1000",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "5",
    }
    records = []
    page = 1
    while True:
        cache_path = cache_dir / f"{event_type}_sse_{page}.json"
        if cache_path.exists() and not refresh:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        else:
            params.update({"pageHelp.pageNo": page, "pageHelp.beginPage": page})
            response = requests.get(
                SSE_URL, params=params,
                headers={**HEADERS, "Referer": "https://etf.sse.com.cn/disclosure/"}, timeout=60,
            )
            response.raise_for_status()
            payload = json.loads(response.content.decode("utf-8"))
            write_json(cache_path, payload)
        items = payload.get("result", [])
        records.extend(normalize_record(
            event_type=event_type,
            exchange="SSE",
            code=str(item.get("SECURITY_CODE") or "").zfill(6),
            announcement_date=str(item.get("SSEDATE") or ""),
            title=str(item.get("TITLE") or ""),
            content="",
            source_url="https://www.sse.com.cn" + str(item.get("URL") or ""),
        ) for item in items)
        total = int(payload.get("pageHelp", {}).get("total", 0) or 0)
        if page * 1000 >= total or not items:
            return records
        page += 1


def fetch_szse(event_type: str, keyword: str, cache_dir: Path, refresh: bool) -> list[dict[str, Any]]:
    payload = {
        "keyword": keyword,
        "time": 0,
        "range": "title",
        "channelCode": "etfNotice_disc",
        "currentPage": 1,
        "pageSize": 50,
        "scope": 0,
    }
    records = []
    page = 1
    while True:
        cache_path = cache_dir / f"{event_type}_szse_{page}.json"
        if cache_path.exists() and not refresh:
            result = json.loads(cache_path.read_text(encoding="utf-8"))
        else:
            payload["currentPage"] = page
            response = requests.post(
                SZSE_URL, data=payload,
                headers={**HEADERS, "Referer": "https://www.szse.cn/disclosure/notice/fund/index.html"}, timeout=60,
            )
            response.raise_for_status()
            result = response.json()
            write_json(cache_path, result)
        items = result.get("data", [])
        for item in items:
            title = strip_html(str(item.get("doctitle") or ""))
            content = strip_html(str(item.get("doccontent") or ""))
            codes = sorted(set(CODE_PATTERN.findall(title + " " + content))) or [""]
            announcement_date = datetime.fromtimestamp(int(item.get("docpubtime", 0)) / 1000).date().isoformat()
            for code in codes:
                records.append(normalize_record(
                    event_type=event_type,
                    exchange="SZSE",
                    code=code,
                    announcement_date=announcement_date,
                    title=title,
                    content=content,
                    source_url=str(item.get("docpuburl") or "").replace("http://", "https://"),
                ))
        total = int(result.get("totalSize", 0) or 0)
        if page * 50 >= total or not items:
            return records
        page += 1


def fetch_sse_current_categories(cache_dir: Path, refresh: bool) -> dict[str, str]:
    categories: dict[str, str] = {}
    for category in ("F110", "F120", "F130", "F140", "F150"):
        cache_path = cache_dir / f"current_sse_{category}.json"
        if cache_path.exists() and not refresh:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        else:
            response = requests.get(
                SSE_URL,
                params={"sqlId": "COMMON_JJZWZ_JJLB_L", "CATEGORY": category, "type": "inParams"},
                headers={**HEADERS, "Referer": "https://etf.sse.com.cn/fundlist/"}, timeout=60,
            )
            response.raise_for_status()
            payload = json.loads(response.content.decode("utf-8"))
            write_json(cache_path, payload)
        categories.update({str(item.get("FUND_CODE") or "").zfill(6): str(item.get("CATEGORY") or "") for item in payload.get("result", [])})
    return categories


def normalize_record(*, event_type: str, exchange: str, code: str, announcement_date: str, title: str,
                     content: str, source_url: str) -> dict[str, Any]:
    effective_date = extract_effective_date(content, event_type)
    return {
        "event_type": event_type,
        "exchange": exchange,
        "etf_code": code,
        "announcement_date": announcement_date,
        "effective_date": effective_date,
        "title": title,
        "source_url": source_url,
        "etf_announcement": is_etf_title(title),
        "code_available": bool(CODE_PATTERN.fullmatch(code)),
        "code_source": "structured_api" if CODE_PATTERN.fullmatch(code) else "",
        "effective_date_available": bool(effective_date),
    }


def extract_effective_date(content: str, event_type: str) -> str:
    phrase = "\u7ec8\u6b62\u4e0a\u5e02" if event_type == "delisting" else "\u4e0a\u5e02\u4ea4\u6613"
    date_expr = r"(20\d{2})\s*\u5e74\s*(\d{1,2})\s*\u6708\s*(\d{1,2})\s*\u65e5"
    matches = re.findall(date_expr + r".{0,20}?" + phrase, content) + re.findall(phrase + r".{0,20}?" + date_expr, content)
    dates = [f"{int(y):04d}-{int(m):02d}-{int(d):02d}" for y, m, d in matches]
    return max(dates) if dates else ""


def fill_pdf_codes(frame: pd.DataFrame, cache_dir: Path, refresh: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = frame.copy()
    missing = frame[
        frame["etf_announcement"] & ~frame["code_available"]
        & frame["source_url"].str.lower().str.endswith(".pdf")
    ]
    tasks = []
    for url in missing["source_url"].drop_duplicates():
        event_type = str(missing.loc[missing["source_url"].eq(url), "event_type"].iloc[0])
        pdf_dir = cache_dir / f"{event_type}_pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        path = pdf_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:20]}.pdf"
        result_path = path.with_suffix(".json")
        if refresh or not result_path.exists():
            tasks.append((url, str(path), str(result_path), refresh))
    if tasks:
        with concurrent.futures.ProcessPoolExecutor(max_workers=4, max_tasks_per_child=10) as pool:
            list(pool.map(extract_pdf_code_task, tasks))

    audits = []
    for url in missing["source_url"].drop_duplicates():
        event_type = str(missing.loc[missing["source_url"].eq(url), "event_type"].iloc[0])
        path = cache_dir / f"{event_type}_pdfs" / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:20]}.pdf"
        result_path = path.with_suffix(".json")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            codes, status = result["codes"], result["status"]
            if status == "unique":
                mask = frame["source_url"].eq(url)
                frame.loc[mask, "etf_code"] = codes[0]
                frame.loc[mask, "code_available"] = True
                frame.loc[mask, "code_source"] = "pdf_numeric_text"
            audits.append({"event_type": event_type, "source_url": url, "cached_pdf": path.name, "codes": "|".join(codes), "status": status})
        except Exception as exc:
            audits.append({"event_type": event_type, "source_url": url, "cached_pdf": path.name, "codes": "", "status": f"error:{type(exc).__name__}"})
    return frame, pd.DataFrame(audits)


def extract_pdf_code_task(task: tuple[str, str, str, bool]) -> None:
    import pdfplumber

    url, path_text, result_text, refresh = task
    path, result_path = Path(path_text), Path(result_text)
    try:
        if refresh or not path.exists():
            response = requests.get(url, headers=HEADERS, timeout=60)
            response.raise_for_status()
            path.write_bytes(response.content)
        with pdfplumber.open(path) as document:
            text = "\n".join((page.extract_text() or "") for page in document.pages[:5])
        codes = extract_etf_codes(text)
        status = "unique" if len(codes) == 1 else ("ambiguous" if len(codes) > 1 else "missing")
    except Exception as exc:
        codes, status = [], f"error:{type(exc).__name__}"
    write_json(result_path, {"source_url": url, "codes": codes, "status": status})


def extract_etf_codes(text: str) -> list[str]:
    return sorted(set(CODE_PATTERN.findall(text)))


def fill_unique_title_prefix_codes(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    frame = frame.copy()
    frame["_title_prefix"] = frame["title"].str.split("：", n=1).str[0].str.strip()
    known = frame[frame["code_available"] & frame["_title_prefix"].ne("")]
    grouped = known.groupby("_title_prefix")["etf_code"].agg(lambda values: sorted(set(values)))
    unique = {prefix: codes[0] for prefix, codes in grouped.items() if len(codes) == 1}
    mask = ~frame["code_available"] & frame["_title_prefix"].isin(unique)
    frame.loc[mask, "etf_code"] = frame.loc[mask, "_title_prefix"].map(unique)
    frame.loc[mask, "code_available"] = True
    frame.loc[mask, "code_source"] = "unique_title_prefix"
    return frame.drop(columns="_title_prefix"), int(mask.sum())


def build_lifecycle_inventory(frame: pd.DataFrame, current_categories: dict[str, str], as_of: date) -> pd.DataFrame:
    valid = frame[frame["code_available"] & frame["etf_announcement"]].copy()
    latest_master = pd.DataFrame()
    master_path = ROOT / "data_catalog" / "etf_pit_master.csv"
    if master_path.exists():
        master = pd.read_csv(master_path, dtype=str, keep_default_na=False)
        snapshot_dates = pd.to_datetime(master["snapshot_date"], errors="coerce")
        eligible_master = master[snapshot_dates.dt.date.le(as_of)].copy()
        if not eligible_master.empty:
            eligible_dates = pd.to_datetime(eligible_master["snapshot_date"], errors="coerce")
            latest_date = eligible_dates.max()
            latest_master = eligible_master[eligible_dates.eq(latest_date)].drop_duplicates(["exchange", "etf_code"])
    current = {(row.exchange, row.etf_code): row.list_date for row in latest_master.itertuples()}
    target_current = {
        (row.exchange, row.etf_code) for row in latest_master.itertuples()
        if str(row.eligible_stock_etf).lower() == "true"
    }
    rows = []
    for (exchange, code), group in valid.groupby(["exchange", "etf_code"]):
        events = set(group["event_type"])
        listing_dates = group.loc[group["event_type"].eq("listing"), "announcement_date"]
        latest_listing_announcement = listing_dates.max() if len(listing_dates) else ""
        current_list_date = current.get((exchange, code), "")
        exchange_category = current_categories.get(code, "") if exchange == "SSE" else ""
        status = "current_active" if (exchange, code) in target_current else (
            "current_out_of_scope" if (exchange, code) in current or exchange_category else
            "historical_delisted_candidate" if "delisting" in events else "historical_status_unknown"
        )
        if status == "historical_status_unknown" and latest_listing_announcement >= (as_of - timedelta(days=30)).isoformat():
            status = "pending_listing"
        rows.append({
            "exchange": exchange,
            "etf_code": code,
            "has_listing_announcement": "listing" in events,
            "has_delisting_announcement": "delisting" in events,
            "current_master_present": (exchange, code) in current,
            "current_target_present": (exchange, code) in target_current,
            "official_current_list_date": current_list_date,
            "official_current_category": exchange_category,
            "latest_listing_announcement_date": latest_listing_announcement,
            "lifecycle_status": status,
        })
    return pd.DataFrame(rows).sort_values(["lifecycle_status", "exchange", "etf_code"])


def current_target_master_stats() -> dict[str, Any]:
    master_path = ROOT / "data_catalog" / "etf_pit_master.csv"
    if not master_path.exists():
        return {"count": 0, "list_date_count": 0, "list_date_coverage": 0.0}
    master = pd.read_csv(master_path, dtype=str, keep_default_na=False)
    latest = master[master["snapshot_date"] == master["snapshot_date"].max()]
    target = latest[latest["eligible_stock_etf"].str.lower().eq("true")].drop_duplicates(["exchange", "etf_code"])
    list_date_count = int(target["list_date"].ne("").sum())
    return {
        "count": len(target),
        "list_date_count": list_date_count,
        "list_date_coverage": list_date_count / len(target) if len(target) else 0.0,
    }


def add_observed_trade_ranges(inventory: pd.DataFrame, refresh: bool) -> pd.DataFrame:
    cache_path = CACHE / "observed_trade_ranges.csv"
    cached = pd.read_csv(cache_path, dtype=str, keep_default_na=False) if cache_path.exists() and not refresh else pd.DataFrame()
    cached_codes = set(cached["etf_code"]) if not cached.empty else set()
    unresolved = inventory[inventory["lifecycle_status"].isin(["historical_delisted_candidate", "historical_status_unknown"])]
    targets = [row for row in unresolved.to_dict("records") if row["etf_code"] not in cached_codes]
    if targets:
        # ponytail: AkShare/Sina embeds a process-global V8 runtime; sequential fetch is slow but stable, then cached once.
        additions = [fetch_observed_trade_range(item) for item in targets]
        cached = pd.concat([cached, pd.DataFrame(additions)], ignore_index=True)
        cached = cached.drop_duplicates(["exchange", "etf_code"], keep="last").sort_values(["exchange", "etf_code"])
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp")
        cached.to_csv(temporary, index=False, encoding="utf-8-sig")
        temporary.replace(cache_path)
    columns = ["exchange", "etf_code", "observed_first_trade_date", "observed_last_trade_date", "observed_trade_day_count", "observed_range_status"]
    return inventory.merge(cached[columns] if not cached.empty else pd.DataFrame(columns=columns), on=["exchange", "etf_code"], how="left").fillna("")


def fetch_observed_trade_range(item: dict[str, Any]) -> dict[str, Any]:
    code = str(item["etf_code"])
    try:
        import akshare as ak
        history = ak.fund_etf_hist_sina(symbol=("sh" if item["exchange"] == "SSE" else "sz") + code)
        dates = pd.to_datetime(history["date"], errors="coerce").dropna() if not history.empty else pd.Series(dtype="datetime64[ns]")
        return {
            "exchange": item["exchange"], "etf_code": code,
            "observed_first_trade_date": dates.min().date().isoformat() if len(dates) else "",
            "observed_last_trade_date": dates.max().date().isoformat() if len(dates) else "",
            "observed_trade_day_count": len(dates),
            "observed_range_status": "available" if len(dates) else "missing",
        }
    except Exception as exc:
        return {
            "exchange": item["exchange"], "etf_code": code,
            "observed_first_trade_date": "", "observed_last_trade_date": "", "observed_trade_day_count": 0,
            "observed_range_status": f"error:{type(exc).__name__}",
        }


def summarize(frame: pd.DataFrame, inventory: pd.DataFrame, pdf_code_audit: pd.DataFrame,
              title_prefix_fill_count: int, target_master: dict[str, Any], as_of: date) -> dict[str, Any]:
    etf = frame[frame["etf_announcement"]]
    valid_codes = etf.loc[etf["code_available"], "etf_code"] if not etf.empty else pd.Series(dtype=str)
    code_coverage = float(etf["code_available"].mean()) if not etf.empty else 0.0
    listing = etf[etf["event_type"] == "listing"]
    delisting = etf[etf["event_type"] == "delisting"]
    historical = inventory["lifecycle_status"].isin(["historical_delisted_candidate", "historical_status_unknown"])
    unknown_count = int((inventory["lifecycle_status"] == "historical_status_unknown").sum())
    blocking_reasons = [
        "\u5c1a\u672a\u5efa\u7acb\u5168\u91cf ETF \u5386\u53f2\u4e0a\u5e02\u65e5\u6e05\u5355",
        "\u516c\u544a\u65e5\u4e0d\u7b49\u4e8e\u7ec8\u6b62\u4e0a\u5e02\u751f\u6548\u65e5\uff0c\u751f\u6548\u65e5\u8986\u76d6\u672a\u8bc1\u660e\u5b8c\u6574",
    ]
    if unknown_count:
        blocking_reasons.append(f"{unknown_count}\u4e2a\u5386\u53f2 ETF \u516c\u544a\u4ee3\u7801\u65e0\u5f53\u524d\u4e3b\u8868\u8bb0\u5f55\u4e14\u65e0\u9000\u5e02\u516c\u544a\uff0c\u5b58\u7eed\u72b6\u6001\u672a\u77e5")
    historical_count = int((inventory["lifecycle_status"] == "historical_delisted_candidate").sum())
    historical_observed_count = int(((inventory["lifecycle_status"] == "historical_delisted_candidate") & (inventory["observed_range_status"] == "available")).sum())
    observed_ready = bool(
        len(delisting) and float(delisting["code_available"].mean()) == 1.0
        and historical_count == historical_observed_count and unknown_count == 0
        and target_master["count"] > 0 and target_master["list_date_coverage"] == 1.0
    )
    return {
        "version": "official-etf-lifecycle-source-audit-1.2",
        "as_of_date": as_of.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "raw_fund_announcement_count": int(len(frame)),
        "announcement_count": int(len(etf)),
        "listing_announcement_count": int(len(listing)),
        "delisting_announcement_count": int(len(delisting)),
        "unique_etf_code_count": int(valid_codes.nunique()),
        "exchange_count": int(frame["exchange"].nunique()) if not frame.empty else 0,
        "code_coverage": code_coverage,
        "listing_code_coverage": float(listing["code_available"].mean()) if len(listing) else 0.0,
        "delisting_code_coverage": float(delisting["code_available"].mean()) if len(delisting) else 0.0,
        "delisting_pdf_code_unique_count": int(((pdf_code_audit["event_type"] == "delisting") & (pdf_code_audit["status"] == "unique")).sum()) if len(pdf_code_audit) else 0,
        "delisting_pdf_code_unresolved_count": int(((pdf_code_audit["event_type"] == "delisting") & (pdf_code_audit["status"] != "unique")).sum()) if len(pdf_code_audit) else 0,
        "listing_pdf_code_unique_count": int(((pdf_code_audit["event_type"] == "listing") & (pdf_code_audit["status"] == "unique")).sum()) if len(pdf_code_audit) else 0,
        "listing_pdf_code_unresolved_count": int(((pdf_code_audit["event_type"] == "listing") & (pdf_code_audit["status"] != "unique")).sum()) if len(pdf_code_audit) else 0,
        "unique_title_prefix_fill_count": title_prefix_fill_count,
        "effective_listing_date_coverage": float(listing["effective_date_available"].mean()) if len(listing) else 0.0,
        "effective_delist_date_coverage": float(delisting["effective_date_available"].mean()) if len(delisting) else 0.0,
        "official_bulk_sources_found": bool(not frame.empty and frame["exchange"].nunique() == 2),
        "official_listing_announcement_count_sse": int((listing["exchange"] == "SSE").sum()),
        "official_listing_announcement_count_szse": int((listing["exchange"] == "SZSE").sum()),
        "current_active_crosscheck_count": int((inventory["lifecycle_status"] == "current_active").sum()),
        "historical_delisted_candidate_count": historical_count,
        "historical_status_unknown_count": unknown_count,
        "current_out_of_scope_count": int((inventory["lifecycle_status"] == "current_out_of_scope").sum()),
        "pending_listing_count": int((inventory["lifecycle_status"] == "pending_listing").sum()),
        "unresolved_observed_range_count": int((historical & (inventory["observed_range_status"] == "available")).sum()),
        "out_of_scope_observed_range_count": int(((inventory["lifecycle_status"] == "current_out_of_scope") & (inventory["observed_range_status"] == "available")).sum()),
        "historical_delisted_observed_range_count": historical_observed_count,
        "historical_unknown_observed_range_count": int(((inventory["lifecycle_status"] == "historical_status_unknown") & (inventory["observed_range_status"] == "available")).sum()),
        "listing_date_history_ready": False,
        "delist_date_history_ready": False,
        "current_target_master_count": target_master["count"],
        "current_target_list_date_coverage": target_master["list_date_coverage"],
        "observed_tradability_universe_ready": observed_ready,
        "lifecycle_reconstruction_ready": False,
        "status": "source_crosschecked_not_reconstruction_ready",
        "blocking_reasons": blocking_reasons,
    }


def write_outputs(frame: pd.DataFrame, inventory: pd.DataFrame, pdf_code_audit: pd.DataFrame, summary: dict[str, Any]) -> None:
    debug = OUTPUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    frame.to_csv(debug / "official_lifecycle_announcements.csv", index=False, encoding="utf-8-sig")
    inventory.to_csv(debug / "lifecycle_inventory_crosscheck.csv", index=False, encoding="utf-8-sig")
    pdf_code_audit.to_csv(debug / "pdf_code_extraction.csv", index=False, encoding="utf-8-sig")
    inventory.to_csv(OUTPUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUTPUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = f"""# ETF \u5b98\u65b9\u751f\u547d\u5468\u671f\u6570\u636e\u6e90\u5ba1\u8ba1

\u7ed3\u8bba\uff1a**\u5df2\u627e\u5230\u4e24\u4ea4\u6613\u6240\u5b98\u65b9\u6279\u91cf\u516c\u544a\u63a5\u53e3\uff0c\u4f46\u5c1a\u4e0d\u80fd\u5b8c\u6574\u91cd\u5efa ETF \u5386\u53f2\u5b58\u7eed\u533a\u95f4**\u3002

- \u622a\u6b62\u65e5\uff1a{summary['as_of_date']}
- \u57fa\u91d1\u516c\u544a\u539f\u59cb\u68c0\u7d22\u8bb0\u5f55\uff1a{summary['raw_fund_announcement_count']}
- \u6807\u9898\u4e25\u683c\u8bc6\u522b ETF \u516c\u544a\uff1a{summary['announcement_count']}
- \u4e0a\u5e02\u516c\u544a\uff1a{summary['listing_announcement_count']}
- \u7ec8\u6b62\u4e0a\u5e02\u516c\u544a\uff1a{summary['delisting_announcement_count']}
- \u53ef\u8bc6\u522b ETF \u4ee3\u7801\uff1a{summary['unique_etf_code_count']}
- \u4ee3\u7801\u8986\u76d6\u7387\uff1a{summary['code_coverage']:.2%}
- \u53ef\u89e3\u6790\u7ec8\u6b62\u4e0a\u5e02\u751f\u6548\u65e5\u8986\u76d6\u7387\uff1a{summary['effective_delist_date_coverage']:.2%}
- PDF \u6570\u5b57\u5c42\u552f\u4e00\u8865\u7801\uff1a{summary['delisting_pdf_code_unique_count']}\uff1b\u672a\u89e3\u51b3\uff1a{summary['delisting_pdf_code_unresolved_count']}
- \u4e0a\u5e02 PDF \u6570\u5b57\u5c42\u552f\u4e00\u8865\u7801\uff1a{summary['listing_pdf_code_unique_count']}\uff1b\u672a\u89e3\u51b3\uff1a{summary['listing_pdf_code_unresolved_count']}
- \u552f\u4e00\u573a\u5185\u7b80\u79f0\u8865\u7801\uff1a{summary['unique_title_prefix_fill_count']}
- \u53ef\u89e3\u6790\u4e0a\u5e02\u751f\u6548\u65e5\u8986\u76d6\u7387\uff1a{summary['effective_listing_date_coverage']:.2%}
- \u4e0a\u4ea4\u6240 ETF \u4e0a\u5e02\u516c\u544a\uff1a{summary['official_listing_announcement_count_sse']}
- \u6df1\u4ea4\u6240 ETF \u4e0a\u5e02\u516c\u544a\uff1a{summary['official_listing_announcement_count_szse']}
- \u5f53\u524d\u4e3b\u8868\u5b58\u7eed ETF \u4ea4\u53c9\u547d\u4e2d\uff1a{summary['current_active_crosscheck_count']}
- \u5386\u53f2\u9000\u5e02\u5019\u9009\uff1a{summary['historical_delisted_candidate_count']}
- \u5386\u53f2\u72b6\u6001\u672a\u77e5\uff1a{summary['historical_status_unknown_count']}
- \u5f53\u524d\u5b58\u7eed\u4f46\u975e\u76ee\u6807\u7c7b\u522b\uff1a{summary['current_out_of_scope_count']}
- \u5f85\u4e0a\u5e02\uff1a{summary['pending_listing_count']}
- \u672a\u5b58\u7eed\u4ee3\u7801\u53ef\u89c2\u6d4b\u884c\u60c5\u533a\u95f4\uff1a{summary['unresolved_observed_range_count']}
- \u5f53\u524d\u76ee\u6807 ETF \u4e0a\u5e02\u65e5\u8986\u76d6\uff1a{summary['current_target_list_date_coverage']:.2%}
- \u53ef\u89c2\u6d4b\u53ef\u4ea4\u6613\u5b87\u5b99\u5c31\u7eea\uff1a`{str(summary['observed_tradability_universe_ready']).lower()}`
- \u751f\u547d\u5468\u671f\u91cd\u5efa\u5c31\u7eea\uff1a`false`

\u8bc1\u636e\u8fb9\u754c\uff1a\u5f53\u524d\u7ed3\u679c\u53ea\u8bc1\u660e\u5b98\u65b9\u6570\u636e\u6e90\u53ef\u8bbf\u95ee\uff0c\u4e0d\u8bc1\u660e\u516c\u544a\u68c0\u7d22\u65e0\u9057\u6f0f\uff0c\u4e5f\u4e0d\u7528\u516c\u544a\u65e5\u66ff\u4ee3\u9000\u5e02\u751f\u6548\u65e5\u3002
\n\u5df2\u5b8c\u6210\uff1a\u5b98\u65b9\u516c\u544a\u5b8c\u6574\u5206\u9875\u3001\u53bb\u91cd\u548c\u5f53\u524d ETF \u4e3b\u8868\u4ea4\u53c9\u6821\u9a8c\u3002\u672a\u91c7\u7528\u7684\u8def\u5f84\uff1a\u539f\u751f PDF \u6587\u672c\u62bd\u53d6\u56e0\u5386\u53f2\u5b57\u4f53\u6620\u5c04\u4e71\u7801\uff0c\u4e0d\u80fd\u7528\u4e8e\u751f\u6548\u65e5\u8bc6\u522b\u3002
"""
    (OUTPUT / "report.md").write_text(report, encoding="utf-8")


def strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).replace("&nbsp;", " ").strip()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def is_etf_title(title: str) -> bool:
    return "ETF" in title.upper() or "\u4ea4\u6613\u578b\u5f00\u653e\u5f0f" in title


def self_check() -> None:
    row = normalize_record(
        event_type="delisting",
        exchange="SZSE",
        code="159522",
        announcement_date="2024-08-23",
        title="ETF\u7ec8\u6b62\u4e0a\u5e02",
        content="\u81ea2024\u5e748\u670828\u65e5\u8d77\u7ec8\u6b62\u4e0a\u5e02",
        source_url="https://example.invalid",
    )
    assert row["effective_date"] == "2024-08-28"
    assert row["code_available"] and row["effective_date_available"]
    assert CODE_PATTERN.findall("\u8bc1\u5238\u4ee3\u7801 159522") == ["159522"]
    assert CODE_PATTERN.fullmatch("530001") and CODE_PATTERN.fullmatch("589001")
    assert not CODE_PATTERN.fullmatch("501302") and not CODE_PATTERN.fullmatch("160001")
    assert extract_etf_codes("\u8bc1\u5238\u4ee3\u7801 159522; \u8054\u63a5\u57fa\u91d1 005639") == ["159522"]
    assert extract_effective_date("\u5c06\u4e8e2025\u5e741\u67082\u65e5\u4e0a\u5e02\u4ea4\u6613", "listing") == "2025-01-02"
    assert is_etf_title("\u4e0a\u8bc150\u4ea4\u6613\u578b\u5f00\u653e\u5f0f\u6307\u6570\u8bc1\u5238\u6295\u8d44\u57fa\u91d1")
    assert not is_etf_title("\u57fa\u91d1\u666e\u4e30\u7ec8\u6b62\u4e0a\u5e02\u516c\u544a")
    sample = pd.DataFrame([
        {"title": "AIETF\uff1a\u4e0a\u5e02\u516c\u544a", "etf_code": "159702", "code_available": True, "code_source": "pdf_numeric_text"},
        {"title": "AIETF\uff1a\u9000\u5e02\u516c\u544a", "etf_code": "", "code_available": False, "code_source": ""},
    ])
    filled, count = fill_unique_title_prefix_codes(sample)
    assert count == 1 and filled.iloc[1]["etf_code"] == "159702"


if __name__ == "__main__":
    main()
