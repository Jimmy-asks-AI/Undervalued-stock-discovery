#!/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

try:
    from valuation_pit_contract import (
        NON_PIT_HISTORY_STATUS,
        mark_trade_date_only_history,
        official_valuation_cutoff,
        official_valuation_history,
    )
except ModuleNotFoundError:  # package-style imports in tests and audits
    from scripts.valuation_pit_contract import (
        NON_PIT_HISTORY_STATUS,
        mark_trade_date_only_history,
        official_valuation_cutoff,
        official_valuation_history,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CURRENT_PANEL = ROOT / "outputs" / "industry_fundamental_pressure_v2_4" / "debug" / "current_fundamental_pressure_panel.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_quality_proxy_v2_5"
DEFAULT_CACHE = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second"
SWS_API = "https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_report/"
VERSION = "2.5.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.5 industry quality proxy and valuation data route audit.")
    parser.add_argument("--start-date", default="20150101", help="Start date for public SWS historical valuation collection.")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"), help="End date for public SWS historical valuation collection.")
    parser.add_argument("--current-panel", default=str(DEFAULT_CURRENT_PANEL), help="V2.4 current fundamental pressure panel.")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE), help="Historical valuation cache directory.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--force-refresh", action="store_true", help="Refresh public SWS yearly valuation cache.")
    parser.add_argument("--sleep-seconds", type=float, default=0.15, help="Pause between SWS requests.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    output_dir = Path(args.output)
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    vendor_audit = audit_vendor_route()
    reconstruction_audit = audit_stock_reconstruction_route()
    valuation_panel, public_audit, collection_log = collect_public_sws_valuation_history(
        start_date=args.start_date,
        end_date=args.end_date,
        cache_dir=Path(args.cache_dir),
        force_refresh=args.force_refresh,
        sleep_seconds=args.sleep_seconds,
    )
    current_panel = load_current_panel(Path(args.current_panel))
    quality_panel, quality_components = build_quality_proxy_panel(current_panel=current_panel, valuation_panel=valuation_panel)
    top_candidates = build_top_candidates(quality_panel)
    route_audit = pd.concat([vendor_audit, reconstruction_audit, public_audit], ignore_index=True)
    pit_coverage = build_pit_coverage(valuation_panel)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    quality_panel.to_csv(debug_dir / "industry_quality_proxy_panel.csv", index=False, encoding="utf-8-sig")
    quality_components.to_csv(debug_dir / "quality_proxy_components.csv", index=False, encoding="utf-8-sig")
    route_audit.to_csv(debug_dir / "valuation_data_route_audit.csv", index=False, encoding="utf-8-sig")
    vendor_audit.to_csv(debug_dir / "vendor_connector_audit.csv", index=False, encoding="utf-8-sig")
    reconstruction_audit.to_csv(debug_dir / "stock_reconstruction_route_audit.csv", index=False, encoding="utf-8-sig")
    public_audit.to_csv(debug_dir / "public_source_collection_audit.csv", index=False, encoding="utf-8-sig")
    pit_coverage.to_csv(debug_dir / "pit_valuation_coverage.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "data_collection_log.json", collection_log)

    summary = {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_boundary": "V2.5 使用公开申万历史行业估值构建行业质量代理；当前仍不生成交易指令，不把未审计数据标记为 validated_alpha。",
        "start_date_requested": args.start_date,
        "end_date_requested": args.end_date,
        "public_valuation_rows": int(len(valuation_panel)),
        "public_valuation_start": date_to_str(valuation_panel["trade_date"].min()) if not valuation_panel.empty else "",
        "public_valuation_end": date_to_str(valuation_panel["trade_date"].max()) if not valuation_panel.empty else "",
        "public_valuation_industries": int(valuation_panel["industry_code"].nunique()) if not valuation_panel.empty else 0,
        "current_quality_rows": int(len(quality_panel)),
        "quality_proxy_pass_count": int((quality_panel["quality_status"] == "quality_proxy_pass_current").sum()) if not quality_panel.empty else 0,
        "sector_data_required_count": int((quality_panel["quality_status"] == "proxy_pass_but_sector_data_required").sum())
        if not quality_panel.empty
        else 0,
        "v2_5_current_observation_count": int(
            quality_panel["v2_5_status"].isin(["v2_5_quality_confirmed_current_observation", "v2_5_sector_data_required_observation"]).sum()
        )
        if not quality_panel.empty
        else 0,
        "route_public_status": first_matching_status(route_audit, "public_website_historical_industry_valuation"),
        "valuation_history_data_status": NON_PIT_HISTORY_STATUS,
        "pit_eligible_history_rows": 0,
        "route_vendor_status": first_matching_status(route_audit, "licensed_vendor_historical_valuation"),
        "route_reconstruction_status": first_matching_status(route_audit, "stock_level_reconstruction"),
    }
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            route_audit=route_audit,
            pit_coverage=pit_coverage,
            quality_components=quality_components,
        ),
        encoding="utf-8",
    )

    print(f"V{VERSION} 行业质量代理与历史估值数据审计完成")
    print(f"公开历史估值行数={summary['public_valuation_rows']}")
    print(f"估值区间={summary['public_valuation_start']} 至 {summary['public_valuation_end']}")
    print(f"覆盖行业数={summary['public_valuation_industries']}")
    print(f"V2.5当前观察数={summary['v2_5_current_observation_count']}")
    print(f"输出目录={output_dir.resolve()}")


def audit_vendor_route() -> pd.DataFrame:
    installed = {name: importlib.util.find_spec(name) is not None for name in ["tushare", "jqdatasdk", "WindPy", "EmQuantAPI", "iFinDPy"]}
    env_present = {
        "TUSHARE_TOKEN": bool(os.environ.get("TUSHARE_TOKEN")),
        "JQDATA_USERNAME": bool(os.environ.get("JQDATA_USERNAME")),
        "JQDATA_PASSWORD": bool(os.environ.get("JQDATA_PASSWORD")),
        "JOINQUANT_USERNAME": bool(os.environ.get("JOINQUANT_USERNAME")),
        "JOINQUANT_PASSWORD": bool(os.environ.get("JOINQUANT_PASSWORD")),
    }
    tushare_saved_token = False
    if installed.get("tushare"):
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                import tushare as ts

                tushare_saved_token = bool(ts.get_token())
        except Exception:
            tushare_saved_token = False
    jq_credentials = (env_present["JQDATA_USERNAME"] and env_present["JQDATA_PASSWORD"]) or (
        env_present["JOINQUANT_USERNAME"] and env_present["JOINQUANT_PASSWORD"]
    )
    rows = [
        {
            "route_id": "licensed_vendor_historical_valuation",
            "source": "Tushare Pro",
            "attempted": True,
            "status": "not_collected_credentials_missing" if not (env_present["TUSHARE_TOKEN"] or tushare_saved_token) else "credentials_detected_not_used",
            "evidence": f"package_installed={installed.get('tushare')}; token_present={env_present['TUSHARE_TOKEN'] or tushare_saved_token}",
            "expected_fields": "daily_basic: pe_ttm|pb|dv_ttm|total_mv|circ_mv; requires industry membership for reconstruction",
            "next_action": "配置 TUSHARE_TOKEN 后可拉个股 daily_basic，再按行业成分重建行业估值。",
        },
        {
            "route_id": "licensed_vendor_historical_valuation",
            "source": "JoinQuant/JQData",
            "attempted": True,
            "status": "not_collected_credentials_missing" if not jq_credentials else "credentials_detected_not_used",
            "evidence": f"package_installed={installed.get('jqdatasdk')}; credentials_present={jq_credentials}",
            "expected_fields": "valuation table plus historical industry membership",
            "next_action": "配置 JQData 账号后验证是否能按 trade_date 获取估值和行业分类。",
        },
        {
            "route_id": "licensed_vendor_historical_valuation",
            "source": "Wind/Choice/iFinD",
            "attempted": True,
            "status": "not_collected_terminal_or_sdk_missing",
            "evidence": f"WindPy={installed.get('WindPy')}; EmQuantAPI={installed.get('EmQuantAPI')}; iFinDPy={installed.get('iFinDPy')}",
            "expected_fields": "industry index PE/PB/dividend yield or stock-level PIT fundamentals",
            "next_action": "安装并登录对应终端 SDK 后再接入，当前环境不能直接调用。",
        },
    ]
    return pd.DataFrame(rows)


def audit_stock_reconstruction_route() -> pd.DataFrame:
    component_sample_rows = 0
    component_sample_status = "not_attempted"
    try:
        import akshare as ak

        sample = ak.index_component_sw(symbol="801194")
        component_sample_rows = int(len(sample))
        component_sample_status = "current_component_sample_collected"
    except Exception as exc:  # noqa: BLE001
        component_sample_status = f"current_component_sample_failed:{type(exc).__name__}"
    rows = [
        {
            "route_id": "stock_level_reconstruction",
            "source": "AkShare current SW components plus stock data",
            "attempted": True,
            "status": "not_collected_pit_membership_and_stock_fundamental_history_missing",
            "evidence": f"current_component_sample_status={component_sample_status}; sample_rows={component_sample_rows}",
            "expected_fields": "PIT industry membership|stock daily market cap|net profit TTM|book value|dividend TTM|available_date",
            "next_action": "当前公开成分接口可辅助现状复核，但不足以重建历史 PIT 行业估值；需要 Tushare/JQData/Wind 等底层数据。",
        }
    ]
    return pd.DataFrame(rows)


def collect_public_sws_valuation_history(
    *,
    start_date: str,
    end_date: str,
    cache_dir: Path,
    force_refresh: bool,
    sleep_seconds: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    yearly_dir = cache_dir / "yearly"
    yearly_dir.mkdir(parents=True, exist_ok=True)
    combined_path = cache_dir / "sws_second_industry_daily_valuation_2015_present.csv"
    existing_combined = read_cached_frame(combined_path)
    start_year = int(start_date[:4])
    end_year = int(end_date[:4])
    yearly_frames: list[pd.DataFrame] = []
    log_rows: list[dict[str, Any]] = []

    for year in range(start_year, end_year + 1):
        year_start = max(f"{year}0101", start_date)
        year_end = min(f"{year}1231", end_date)
        path = yearly_dir / f"{year}.csv"
        cached_frame = pd.DataFrame()
        if path.exists() and not force_refresh:
            cached_frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
            if year < end_year or cache_covers_end_date(cached_frame, year_end):
                yearly_frames.append(cached_frame)
                log_rows.append({"year": year, "status": "cache_hit", "rows": int(len(cached_frame)), "path": str(path)})
                continue
        downloaded = fetch_sws_daily_range(year_start, year_end, sleep_seconds=sleep_seconds)
        frame, refresh_status = prefer_downloaded_or_cached(downloaded, cached_frame)
        if refresh_status == "refresh_empty_cache_retained":
            yearly_frames.append(frame)
            log_rows.append({"year": year, "status": refresh_status, "rows": int(len(frame)), "path": str(path)})
            continue
        write_csv_atomic(frame, path)
        yearly_frames.append(frame)
        log_rows.append({"year": year, "status": "downloaded", "rows": int(len(frame)), "path": str(path)})

    if yearly_frames:
        valuation_panel = pd.concat(yearly_frames, ignore_index=True)
        valuation_panel["trade_date"] = pd.to_datetime(valuation_panel["trade_date"], errors="coerce")
        valuation_panel = valuation_panel.dropna(subset=["trade_date", "industry_code"]).drop_duplicates(
            ["trade_date", "industry_code"], keep="last"
        )
        valuation_panel = valuation_panel.sort_values(["trade_date", "industry_code"]).reset_index(drop=True)
    else:
        valuation_panel = pd.DataFrame()
    valuation_panel, combined_status = prefer_complete_cache(valuation_panel, existing_combined)
    before_official_filter = len(valuation_panel)
    valuation_panel = official_valuation_history(valuation_panel)
    excluded_recovered_rows = before_official_filter - len(valuation_panel)
    # The SWS payload exposes the valuation trade date but no source publication
    # timestamp.  Preserve the useful values while explicitly blocking PIT use.
    valuation_panel = mark_trade_date_only_history(valuation_panel)
    write_csv_atomic(valuation_panel, combined_path)

    public_audit = pd.DataFrame(
        [
            {
                "route_id": "public_website_historical_industry_valuation",
                "source": "SWS Research index analysis daily API via direct requests",
                "attempted": True,
                "status": "collected_non_pit_public_sws_daily_analysis" if not valuation_panel.empty else "collection_failed_no_rows",
                "evidence": f"rows={len(valuation_panel)}; start={date_to_str(valuation_panel['trade_date'].min()) if not valuation_panel.empty else ''}; end={date_to_str(valuation_panel['trade_date'].max()) if not valuation_panel.empty else ''}; industries={valuation_panel['industry_code'].nunique() if not valuation_panel.empty else 0}",
                "expected_fields": "trade_date|industry_code|industry_name|pe|pb|dividend_yield|float_market_cap|turnover_rate; published_at unavailable",
                "next_action": "仅作非PIT历史研究输入；取得带时区的真实发布时间或供应商可用时间后，才可进入历史PIT回测。",
            }
        ]
    )
    log = {
        "source": "SWS Research index analysis daily API",
        "api": SWS_API,
        "requested_start_date": start_date,
        "requested_end_date": end_date,
        "combined_path": str(combined_path.resolve()),
        "yearly_logs": log_rows,
        "row_count": int(len(valuation_panel)),
        "industry_count": int(valuation_panel["industry_code"].nunique()) if not valuation_panel.empty else 0,
        "combined_cache_status": combined_status,
        "official_source_cutoff": official_valuation_cutoff(valuation_panel),
        "excluded_recovered_current_component_rows": excluded_recovered_rows,
        "data_status": NON_PIT_HISTORY_STATUS,
        "pit_eligible": False,
    }
    return valuation_panel, public_audit, log


def fetch_sws_daily_range(start_date: str, end_date: str, sleep_seconds: float) -> pd.DataFrame:
    start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
    params = {
        "page": "1",
        "page_size": "5000",
        "index_type": "二级行业",
        "start_date": start_fmt,
        "end_date": end_fmt,
        "type": "DAY",
        "swindexcode": "all",
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    first = requests.get(SWS_API, params=params, headers=headers, verify=False, timeout=60)
    first.raise_for_status()
    data_json = first.json()
    total = int(data_json.get("data", {}).get("count", 0))
    results = list(data_json.get("data", {}).get("results", []))
    pages = int(math.ceil(total / int(params["page_size"]))) if total else 0
    for page in range(2, pages + 1):
        params["page"] = str(page)
        time.sleep(max(sleep_seconds, 0.0))
        response = requests.get(SWS_API, params=params, headers=headers, verify=False, timeout=60)
        response.raise_for_status()
        results.extend(response.json().get("data", {}).get("results", []))
    return normalize_sws_results(results)


def cache_covers_end_date(frame: pd.DataFrame, requested_end: str) -> bool:
    if frame.empty or "trade_date" not in frame.columns:
        return False
    cached_end = pd.to_datetime(frame["trade_date"], errors="coerce").max()
    return pd.notna(cached_end) and cached_end >= pd.to_datetime(requested_end)


def prefer_downloaded_or_cached(downloaded: pd.DataFrame, cached: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    if downloaded.empty and not cached.empty:
        return cached, "refresh_empty_cache_retained"
    return downloaded, "downloaded"


def read_cached_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}, low_memory=False)


def prefer_complete_cache(candidate: pd.DataFrame, cached: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    if cached.empty:
        return candidate, "combined_written"
    if candidate.empty or len(candidate) < len(cached):
        return cached, "combined_regression_blocked"
    return candidate, "combined_written"


def write_csv_atomic(frame: pd.DataFrame, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def self_check() -> None:
    frame = pd.DataFrame({"trade_date": ["2026-06-12"]})
    assert cache_covers_end_date(frame, "20260612")
    assert not cache_covers_end_date(frame, "20260711")
    assert not cache_covers_end_date(pd.DataFrame(), "20260711")
    kept, status = prefer_downloaded_or_cached(pd.DataFrame(), frame)
    assert status == "refresh_empty_cache_retained" and kept.equals(frame)
    complete, status = prefer_complete_cache(pd.DataFrame(), frame)
    assert status == "combined_regression_blocked" and complete.equals(frame)
    current = pd.DataFrame(
        {
            "industry_code": ["801010"],
            "industry_name": ["农业"],
            "parent_industry": ["农林牧渔"],
            "price_quality_composite_current": [0.5],
            "current_fundamental_pressure_score": [0.5],
            "candidate_status": ["research_watchlist"],
        }
    )
    valuation = pd.DataFrame(
        {
            "trade_date": ["2025-01-01", "2025-01-02", "2025-01-03"],
            "industry_code": ["801010"] * 3,
            "industry_name": ["农业"] * 3,
            "pe": ["10", "11", "12"],
            "pb": ["1.0", "1.1", "1.2"],
            "dividend_yield": ["0.01", "0.02", "0.03"],
            "float_market_cap": ["100", "101", "102"],
            "avg_float_market_cap": ["10", "11", "12"],
            "turnover_rate": ["0.1", "0.2", "0.3"],
        }
    )
    quality, _ = build_quality_proxy_panel(current, valuation)
    assert len(quality) == 1 and math.isfinite(float(quality.iloc[0]["industry_quality_proxy_score"]))
    print("self_check=pass")


def normalize_sws_results(results: list[dict[str, Any]]) -> pd.DataFrame:
    if not results:
        return pd.DataFrame(
            columns=[
                "trade_date",
                "industry_code",
                "industry_name",
                "close_index",
                "volume_100m_shares",
                "return_pct",
                "turnover_rate",
                "pe",
                "pb",
                "mean_price",
                "amount_share_pct",
                "float_market_cap",
                "avg_float_market_cap",
                "dividend_yield",
                "source",
                "available_date",
                "published_at",
                "fetched_at",
                "source_version",
                "source_hash",
                "revision_status",
                "availability_basis",
                "data_status",
                "pit_eligible",
            ]
        )
    raw = pd.DataFrame(results)
    frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(raw.get("bargaindate"), errors="coerce").dt.strftime("%Y-%m-%d"),
            "industry_code": raw.get("swindexcode", "").astype(str).str.replace(".0", "", regex=False).str.zfill(6),
            "industry_name": raw.get("swindexname", ""),
            "close_index": pd.to_numeric(raw.get("closeindex"), errors="coerce"),
            "volume_100m_shares": pd.to_numeric(raw.get("bargainamount"), errors="coerce"),
            "return_pct": pd.to_numeric(raw.get("markup"), errors="coerce"),
            "turnover_rate": pd.to_numeric(raw.get("turnoverrate"), errors="coerce"),
            "pe": pd.to_numeric(raw.get("pe"), errors="coerce"),
            "pb": pd.to_numeric(raw.get("pb"), errors="coerce"),
            "mean_price": pd.to_numeric(raw.get("meanprice"), errors="coerce"),
            "amount_share_pct": pd.to_numeric(raw.get("bargainsumrate"), errors="coerce"),
            "float_market_cap": pd.to_numeric(raw.get("negotiablessharesum1"), errors="coerce"),
            "avg_float_market_cap": pd.to_numeric(raw.get("negotiablessharesum2"), errors="coerce"),
            "dividend_yield": pd.to_numeric(raw.get("dp"), errors="coerce") / 100.0,
            "source": "sws_index_analysis_daily",
        }
    )
    return mark_trade_date_only_history(frame)


def load_current_panel(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    frame["industry_code"] = frame["industry_code"].map(lambda value: str(value).zfill(6))
    return frame


def build_quality_proxy_panel(current_panel: pd.DataFrame, valuation_panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if current_panel.empty:
        return pd.DataFrame(), pd.DataFrame()
    if valuation_panel.empty:
        frame = current_panel.copy()
        frame["industry_quality_proxy_score"] = 0.0
        frame["quality_status"] = "quality_proxy_no_history"
        return frame, frame

    valuation = valuation_panel.copy()
    valuation["trade_date"] = pd.to_datetime(valuation["trade_date"], errors="coerce")
    for column in ["pe", "pb", "dividend_yield", "float_market_cap", "avg_float_market_cap", "turnover_rate"]:
        valuation[column] = pd.to_numeric(valuation.get(column), errors="coerce")
    valuation = valuation.dropna(subset=["trade_date", "industry_code"]).sort_values(["industry_code", "trade_date"])
    latest_date = valuation["trade_date"].max()
    latest = valuation[valuation["trade_date"] == latest_date].copy()
    latest = latest.drop_duplicates("industry_code", keep="last")

    rows: list[dict[str, Any]] = []
    for industry_code, group in valuation.groupby("industry_code", sort=True):
        ordered = group.sort_values("trade_date")
        trailing = ordered.tail(252)
        pe_valid = trailing["pe"].where((trailing["pe"] > 0) & (trailing["pe"] <= 100))
        pb_valid = trailing["pb"].where((trailing["pb"] > 0) & (trailing["pb"] <= 10))
        row_latest = ordered.iloc[-1]
        pe_log_std = float(np.log(pe_valid.dropna()).std()) if pe_valid.dropna().nunique() >= 3 else math.nan
        pb_log_std = float(np.log(pb_valid.dropna()).std()) if pb_valid.dropna().nunique() >= 3 else math.nan
        rows.append(
            {
                "industry_code": industry_code,
                "valuation_history_start": date_to_str(ordered["trade_date"].min()),
                "valuation_history_end": date_to_str(ordered["trade_date"].max()),
                "valuation_history_rows": int(len(ordered)),
                "trailing_valuation_rows": int(len(trailing)),
                "current_sws_industry_name": row_latest.get("industry_name", ""),
                "current_sws_pe": row_latest.get("pe"),
                "current_sws_pb": row_latest.get("pb"),
                "current_sws_dividend_yield": row_latest.get("dividend_yield"),
                "current_float_market_cap": row_latest.get("float_market_cap"),
                "current_avg_float_market_cap": row_latest.get("avg_float_market_cap"),
                "current_turnover_rate": row_latest.get("turnover_rate"),
                "pe_positive_valid_ratio": float(((trailing["pe"] > 0) & (trailing["pe"] <= 100)).mean()),
                "pb_valid_ratio": float(((trailing["pb"] > 0) & (trailing["pb"] <= 10)).mean()),
                "dividend_positive_ratio": float((trailing["dividend_yield"].fillna(0.0) > 0).mean()),
                "pe_log_std_252d": pe_log_std,
                "pb_log_std_252d": pb_log_std,
                "valuation_pit_coverage_score": min(len(trailing) / 252.0, 1.0),
            }
        )
    components = pd.DataFrame(rows)
    components["pe_stability_score"] = inverse_rank(components["pe_log_std_252d"])
    components["pb_stability_score"] = inverse_rank(components["pb_log_std_252d"])
    components["valuation_stability_score"] = (
        0.60 * components["pe_stability_score"].fillna(0.0) + 0.40 * components["pb_stability_score"].fillna(0.0)
    )
    components["dividend_level_score"] = rank01(components["current_sws_dividend_yield"])
    components["dividend_continuity_score"] = (
        0.65 * components["dividend_positive_ratio"].fillna(0.0) + 0.35 * components["dividend_level_score"].fillna(0.0)
    )
    components["earnings_sanity_score"] = components["pe_positive_valid_ratio"].fillna(0.0)
    components["book_value_sanity_score"] = components["pb_valid_ratio"].fillna(0.0)
    components["market_depth_score"] = (
        0.65 * rank01(np.log1p(components["current_float_market_cap"].clip(lower=0)))
        + 0.35 * rank01(np.log1p(components["current_avg_float_market_cap"].clip(lower=0)))
    )

    frame = current_panel.merge(components, on="industry_code", how="left")
    frame["sector_quality_data_required_flag"] = frame["parent_industry"].isin(["银行", "非银金融", "房地产", "建筑装饰"])
    frame["industry_quality_proxy_score_raw"] = (
        0.20 * frame["earnings_sanity_score"].fillna(0.0)
        + 0.15 * frame["book_value_sanity_score"].fillna(0.0)
        + 0.16 * frame["dividend_continuity_score"].fillna(0.0)
        + 0.14 * frame["valuation_stability_score"].fillna(0.0)
        + 0.12 * frame["market_depth_score"].fillna(0.0)
        + 0.13 * frame["price_quality_composite_current"].fillna(0.0)
        + 0.10 * frame["valuation_pit_coverage_score"].fillna(0.0)
        - 0.08 * frame["sector_quality_data_required_flag"].astype(float)
    )
    frame["industry_quality_proxy_score"] = frame["industry_quality_proxy_score_raw"].clip(lower=0.0, upper=1.0)
    frame["v2_5_quality_adjusted_score"] = (
        0.50 * frame["current_fundamental_pressure_score"].fillna(0.0)
        + 0.35 * frame["industry_quality_proxy_score"].fillna(0.0)
        + 0.15 * frame["valuation_pit_coverage_score"].fillna(0.0)
        - 0.08 * frame["sector_quality_data_required_flag"].astype(float)
    ).clip(lower=0.0, upper=1.0)
    frame["v2_5_quality_rank"] = frame["v2_5_quality_adjusted_score"].rank(ascending=False, method="first").astype(int)
    frame["quality_status"] = frame.apply(classify_quality_status, axis=1)
    frame["v2_5_status"] = frame.apply(classify_v25_status, axis=1)
    frame["quality_review_reason"] = frame.apply(build_quality_reason, axis=1)
    return frame.sort_values("v2_5_quality_adjusted_score", ascending=False).reset_index(drop=True), components


def classify_quality_status(row: pd.Series) -> str:
    if safe_number(row.get("valuation_history_rows")) < 120:
        return "quality_proxy_insufficient_history"
    score = safe_number(row.get("industry_quality_proxy_score"))
    if bool(row.get("sector_quality_data_required_flag")) and score >= 0.62:
        return "proxy_pass_but_sector_data_required"
    if score >= 0.68:
        return "quality_proxy_pass_current"
    if score >= 0.55:
        return "quality_watchlist"
    return "quality_proxy_weak"


def classify_v25_status(row: pd.Series) -> str:
    status = str(row.get("candidate_status", ""))
    quality_status = str(row.get("quality_status", ""))
    if status == "current_snapshot_candidate_not_pit_validated" and quality_status == "quality_proxy_pass_current":
        return "v2_5_quality_confirmed_current_observation"
    if status == "current_snapshot_candidate_not_pit_validated" and quality_status == "proxy_pass_but_sector_data_required":
        return "v2_5_sector_data_required_observation"
    if status == "current_snapshot_candidate_not_pit_validated":
        return "v2_5_candidate_quality_not_confirmed"
    if quality_status in {"quality_proxy_pass_current", "proxy_pass_but_sector_data_required"}:
        return "quality_only_watchlist"
    return "research_watchlist"


def build_quality_reason(row: pd.Series) -> str:
    reasons: list[str] = []
    if safe_number(row.get("valuation_pit_coverage_score")) >= 0.95:
        reasons.append("历史估值覆盖充足")
    else:
        reasons.append("历史估值覆盖不足")
    if safe_number(row.get("earnings_sanity_score")) >= 0.80:
        reasons.append("PE正值有效比例较高")
    else:
        reasons.append("PE有效性偏弱")
    if safe_number(row.get("dividend_continuity_score")) >= 0.60:
        reasons.append("分红连续性较好")
    else:
        reasons.append("分红连续性一般")
    if safe_number(row.get("valuation_stability_score")) >= 0.60:
        reasons.append("估值波动相对稳定")
    else:
        reasons.append("估值波动偏高")
    if bool(row.get("sector_quality_data_required_flag")):
        reasons.append("该上级行业仍需专项质量数据确认")
    return "；".join(reasons)


def build_top_candidates(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in frame.sort_values("v2_5_quality_adjusted_score", ascending=False).head(30).to_dict("records"):
        rows.append(
            {
                "排名": int(row.get("v2_5_quality_rank", 0)),
                "行业代码": row.get("industry_code", ""),
                "行业": row.get("industry_name", ""),
                "上级行业": row.get("parent_industry", ""),
                "V2.5状态": translate_v25_status(row.get("v2_5_status", "")),
                "V2.4状态": translate_candidate_status(row.get("candidate_status", "")),
                "V2.5质量调整分": fmt_float(row.get("v2_5_quality_adjusted_score"), 3),
                "质量代理分": fmt_pct(row.get("industry_quality_proxy_score")),
                "V2.4综合分": fmt_float(row.get("current_fundamental_pressure_score"), 3),
                "估值分": fmt_pct(row.get("valuation_score_blended")),
                "超跌分": fmt_pct(row.get("oversold_score_current")),
                "历史估值覆盖": fmt_pct(row.get("valuation_pit_coverage_score")),
                "PE有效比例": fmt_pct(row.get("earnings_sanity_score")),
                "PB有效比例": fmt_pct(row.get("book_value_sanity_score")),
                "分红连续性": fmt_pct(row.get("dividend_continuity_score")),
                "估值稳定性": fmt_pct(row.get("valuation_stability_score")),
                "行业专项数据缺口": "是" if row.get("sector_quality_data_required_flag") else "否",
                "说明": row.get("quality_review_reason", ""),
            }
        )
    return pd.DataFrame(rows)


def build_pit_coverage(panel: pd.DataFrame) -> pd.DataFrame:
    if panel.empty:
        return pd.DataFrame()
    grouped = panel.groupby("trade_date").agg(
        rows=("industry_code", "count"),
        industries=("industry_code", "nunique"),
        pe_non_null=("pe", lambda s: int(s.notna().sum())),
        pb_non_null=("pb", lambda s: int(s.notna().sum())),
        dividend_non_null=("dividend_yield", lambda s: int(s.notna().sum())),
    )
    yearly = grouped.reset_index()
    yearly["year"] = pd.to_datetime(yearly["trade_date"]).dt.year
    return (
        yearly.groupby("year")
        .agg(
            trading_dates=("trade_date", "nunique"),
            rows=("rows", "sum"),
            mean_industries_per_date=("industries", "mean"),
            min_industries_per_date=("industries", "min"),
            pe_non_null=("pe_non_null", "sum"),
            pb_non_null=("pb_non_null", "sum"),
            dividend_non_null=("dividend_non_null", "sum"),
        )
        .reset_index()
    )


def first_matching_status(frame: pd.DataFrame, route_id: str) -> str:
    matched = frame[frame["route_id"] == route_id]
    if matched.empty:
        return ""
    return str(matched.iloc[0]["status"])


def rank01(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").rank(pct=True, method="average")


def inverse_rank(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.rank(pct=True, method="average", ascending=False)


def safe_number(value: Any) -> float:
    try:
        if value is None or str(value).strip() == "":
            return 0.0
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) else number


def fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(number):
        return ""
    return f"{number * 100:.2f}%"


def fmt_float(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(number):
        return ""
    return f"{number:.{digits}f}"


def date_to_str(value: Any) -> str:
    if pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    def default(obj: Any) -> Any:
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, pd.Timestamp):
            return obj.strftime("%Y-%m-%d")
        return str(obj)

    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=default)


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    route_audit: pd.DataFrame,
    pit_coverage: pd.DataFrame,
    quality_components: pd.DataFrame,
) -> str:
    lines = [
        "# V2.5 行业质量代理与历史估值数据审计报告",
        "",
        f"版本：{summary['version']}",
        "",
        "## 研究结论",
        "",
        "V2.5 完成了行业质量代理的第一版，并实际尝试了三条历史估值数据路线。当前结论是：公开申万指数分析日报表路线可以收集到 2015 年以来的申万二级行业历史 PE、PB、股息率；授权数据源和个股级重建路线在当前环境下受凭证和 PIT 成分/个股基本面数据限制，不能直接完成。",
        "",
        f"- 公开历史估值行数：{summary['public_valuation_rows']}",
        f"- 历史估值区间：{summary['public_valuation_start']} 至 {summary['public_valuation_end']}",
        f"- 覆盖行业数：{summary['public_valuation_industries']}",
        f"- 当前质量代理行业数：{summary['current_quality_rows']}",
        f"- V2.5 当前观察数：{summary['v2_5_current_observation_count']}",
        "",
        f"- 历史估值数据状态：`{summary['valuation_history_data_status']}`；PIT 合格行数：{summary['pit_eligible_history_rows']}",
        "",
        "V2.5 仍然不生成交易指令，也不把条件观察标记为 validated_alpha。公开历史估值缺少真实发布时间，只能用于描述性研究；不得按 trade_date 或推定 lag 冒充 available_date，也不得进入历史 PIT 回测。",
        "",
        "## 三条数据路线审计",
        "",
    ]
    lines.extend(render_markdown_table(route_audit))
    lines.extend(
        [
            "",
            "## 行业质量代理方法",
            "",
            "质量代理分由以下组件构成：PE 正值有效比例、PB 有效比例、分红连续性、估值稳定性、流通市值深度、V2.4 价格质量代理、历史估值覆盖，并对银行、非银金融、房地产、建筑装饰等需要专项质量数据的上级行业施加数据缺口标记。",
            "",
            "这个代理不是完整基本面质量。它只能减少明显的估值陷阱和数据质量问题，不能替代银行不良率、保险内含价值、地产杠杆和建筑现金流等专项指标。",
            "",
            "## V2.5 当前质量调整候选",
            "",
        ]
    )
    lines.extend(render_markdown_table(top_candidates.head(20)))
    lines.extend(["", "## 历史估值覆盖年度摘要", ""])
    lines.extend(render_markdown_table(pit_coverage.tail(12)))
    lines.extend(["", "## 复现文件", ""])
    lines.extend(
        [
            "- `debug/industry_quality_proxy_panel.csv`",
            "- `debug/quality_proxy_components.csv`",
            "- `debug/valuation_data_route_audit.csv`",
            "- `debug/vendor_connector_audit.csv`",
            "- `debug/stock_reconstruction_route_audit.csv`",
            "- `debug/public_source_collection_audit.csv`",
            "- `debug/pit_valuation_coverage.csv`",
            "- `debug/data_collection_log.json`",
            "",
            "## 主要来源",
            "",
            "- AKShare 申万二级行业信息与申万指数分析日报表文档：https://akshare.akfamily.xyz/data/index/index.html",
            "- Tushare Pro daily_basic 文档：https://tushare.pro/wctapi/documents/32.md",
            "- JoinQuant/JQData 估值数据文档：https://www.joinquant.com/help/api/doc?id=9884&name=JQDatadoc",
            "- 投资数据网公开说明：https://www.touzid.com/",
        ]
    )
    return "\n".join(lines)


def render_markdown_table(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    columns = list(frame.columns)
    rows = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for record in frame.to_dict("records"):
        rows.append("| " + " | ".join(str(record.get(column, "")) for column in columns) + " |")
    return rows


def translate_candidate_status(status: Any) -> str:
    return {
        "current_snapshot_candidate_not_pit_validated": "当前快照候选，未PIT验证",
        "valuation_watchlist_not_oversold": "估值观察，未超跌",
        "oversold_without_valuation_support": "超跌但估值支持不足",
        "research_watchlist": "研究观察",
        "blocked_no_current_valuation": "缺少当前估值",
    }.get(str(status), str(status))


def translate_v25_status(status: Any) -> str:
    return {
        "v2_5_quality_confirmed_current_observation": "V2.5质量代理确认观察",
        "v2_5_sector_data_required_observation": "V2.5观察但需专项行业数据",
        "v2_5_candidate_quality_not_confirmed": "V2.5候选但质量未确认",
        "quality_only_watchlist": "质量观察",
        "research_watchlist": "研究观察",
    }.get(str(status), str(status))


if __name__ == "__main__":
    main()
