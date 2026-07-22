#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategy_lab"))

from fundamental_value_os import run_fundamental_value_agents, write_outputs


DEFAULT_OUTPUT = ROOT / "outputs" / "current_a_share_value_snapshot"
EASTMONEY_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_FIELDS = (
    "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f23,f24,f25,f26,f62,f115,f152"
)
EASTMONEY_FS_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a research-only current A-share value snapshot.")
    parser.add_argument("--report-date", default="20260331", help="Eastmoney earnings report date, e.g. 20260331.")
    parser.add_argument("--trade-date", default=datetime.now().date().isoformat(), help="Decision date YYYY-MM-DD.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output directory.")
    parser.add_argument("--top", type=int, default=30, help="Number of top research candidates in the report.")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    spot = fetch_eastmoney_a_spot()
    earnings = fetch_earnings_report(args.report_date)
    dividend = fetch_dividend_snapshot()
    panel, profile = build_current_research_panel(spot, earnings, dividend, args.trade_date)

    panel_path = output_dir / "asset_panel_current_research_only.csv"
    panel.to_csv(panel_path, index=False, encoding="utf-8-sig")

    records = panel.to_dict("records")
    result = run_fundamental_value_agents(records)
    write_outputs(result, output_dir)

    ranking = pd.DataFrame(result["candidate_ranking"])
    enriched = ranking.merge(
        panel[
            [
                "asset",
                "pe_ttm",
                "pb",
                "dividend_yield_ttm",
                "roe_ttm",
                "revenue_cagr_3y",
                "net_profit_cagr_3y",
                "ocf_to_net_income",
                "market_cap",
                "avg_amount_20d",
                "proxy_field_count",
                "source_warning",
            ]
        ],
        on="asset",
        how="left",
    )
    enriched["obvious_value_flag"] = enriched.apply(is_obvious_value_candidate, axis=1)
    enriched["research_confidence"] = enriched.apply(classify_research_confidence, axis=1)
    enriched.sort_values(["obvious_value_flag", "composite_score"], ascending=[False, False], inplace=True)

    enriched_path = output_dir / "current_value_candidates_enriched.csv"
    enriched.to_csv(enriched_path, index=False, encoding="utf-8-sig")

    obvious_candidates = enriched[enriched["obvious_value_flag"]].copy()
    top_candidates = obvious_candidates.head(args.top).copy()
    top_candidates_path = output_dir / "top_obvious_value_candidates.csv"
    top_candidates.to_csv(top_candidates_path, index=False, encoding="utf-8-sig")

    profile.update(
        {
            "version": "0.6.0",
            "trade_date": args.trade_date,
            "report_date": args.report_date,
            "spot_rows": int(len(spot)),
            "earnings_rows": int(len(earnings)),
            "dividend_rows": int(len(dividend)),
            "panel_rows": int(len(panel)),
            "ranked_rows": int(len(enriched)),
            "obvious_value_candidates": int(len(obvious_candidates)),
            "top_obvious_value_output_rows": int(len(top_candidates)),
            "relative_value_trap_flags": int(enriched["relative_value_trap_flag"].sum()),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "research_boundary": "research_only current snapshot; not PIT backtest evidence and not investment advice",
        }
    )
    write_json(output_dir / "snapshot_profile.json", profile)
    write_snapshot_report(output_dir / "current_snapshot_report.md", enriched, top_candidates, profile, args.top)

    print(f"spot_rows={len(spot)}")
    print(f"panel_rows={len(panel)}")
    print(f"obvious_value_candidates={len(obvious_candidates)}")
    print(f"top_obvious_value_output_rows={len(top_candidates)}")
    print(f"output={output_dir.resolve()}")
    for row in top_candidates.head(10).to_dict("records"):
        print(
            "{asset} {name} score={score:.4f} pe={pe:.2f} pb={pb:.2f} roe={roe:.2%} dy={dy:.2%}".format(
                asset=row["asset"],
                name=row["name"],
                score=float(row["composite_score"]),
                pe=float(row["pe_ttm"]),
                pb=float(row["pb"]),
                roe=float(row["roe_ttm"]),
                dy=float(row["dividend_yield_ttm"]),
            )
        )


def fetch_eastmoney_a_spot(page_size: int = 500) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total = None
    page = 1
    while total is None or len(rows) < total:
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": EASTMONEY_FS_A,
            "fields": EASTMONEY_FIELDS,
        }
        response = requests.get(
            EASTMONEY_CLIST_URL,
            params=params,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        total = int(data.get("total") or 0)
        diff = data.get("diff") or []
        if not diff:
            break
        rows.extend(diff)
        page += 1
    spot = pd.DataFrame(rows)
    rename = {
        "f2": "price",
        "f3": "pct_change",
        "f6": "amount",
        "f8": "turnover_rate",
        "f9": "pe_dynamic",
        "f12": "asset",
        "f14": "name",
        "f20": "market_cap",
        "f21": "float_market_cap",
        "f23": "pb",
        "f26": "list_date_raw",
        "f115": "pe_ttm",
    }
    spot = spot.rename(columns=rename)
    keep = list(rename.values())
    return spot[[col for col in keep if col in spot.columns]].copy()


def fetch_earnings_report(report_date: str) -> pd.DataFrame:
    earnings = ak.stock_yjbb_em(date=report_date)
    return earnings.copy()


def fetch_dividend_snapshot() -> pd.DataFrame:
    try:
        return ak.stock_fhps_em().copy()
    except Exception:
        return pd.DataFrame()


def build_current_research_panel(
    spot: pd.DataFrame,
    earnings: pd.DataFrame,
    dividend: pd.DataFrame,
    trade_date: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    spot = spot.copy()
    spot["asset"] = spot["asset"].astype(str).str.zfill(6)
    spot["price"] = pd.to_numeric(spot.get("price"), errors="coerce")
    spot["pe_ttm"] = pd.to_numeric(spot.get("pe_ttm"), errors="coerce")
    spot["pe_dynamic"] = pd.to_numeric(spot.get("pe_dynamic"), errors="coerce")
    spot["pe_ttm"] = spot["pe_ttm"].where(spot["pe_ttm"].gt(0), spot["pe_dynamic"])
    spot["pb"] = pd.to_numeric(spot.get("pb"), errors="coerce")
    spot["market_cap"] = pd.to_numeric(spot.get("market_cap"), errors="coerce")
    spot["amount"] = pd.to_numeric(spot.get("amount"), errors="coerce")

    earnings = earnings.copy()
    earnings["asset"] = earnings["股票代码"].astype(str).str.zfill(6)
    earnings = earnings.rename(
        columns={
            "股票简称": "earnings_name",
            "每股收益": "eps",
            "营业总收入-同比增长": "revenue_yoy_pct",
            "净利润-同比增长": "net_profit_yoy_pct",
            "净资产收益率": "roe_pct",
            "每股经营现金流量": "ocf_per_share",
            "销售毛利率": "gross_margin_pct",
            "所处行业": "industry",
            "最新公告日期": "latest_announcement_date",
        }
    )
    earnings_cols = [
        "asset",
        "eps",
        "revenue_yoy_pct",
        "net_profit_yoy_pct",
        "roe_pct",
        "ocf_per_share",
        "gross_margin_pct",
        "industry",
        "latest_announcement_date",
    ]
    earnings = earnings[[col for col in earnings_cols if col in earnings.columns]].copy()

    if dividend.empty:
        dividend = pd.DataFrame(columns=["asset", "dividend_yield"])
    else:
        dividend = dividend.copy()
        dividend["asset"] = dividend["代码"].astype(str).str.zfill(6)
        dividend = dividend.rename(columns={"现金分红-股息率": "dividend_yield"})
        dividend = dividend[["asset", "dividend_yield"]].copy()
        dividend["dividend_yield"] = pd.to_numeric(dividend["dividend_yield"], errors="coerce")
        dividend = dividend.sort_values("dividend_yield", ascending=False).drop_duplicates("asset")

    merged = spot.merge(earnings, on="asset", how="left").merge(dividend, on="asset", how="left")
    merged = merged[merged["price"].gt(0) & merged["pe_ttm"].gt(0) & merged["pb"].gt(0)].copy()
    merged = merged[~merged["name"].astype(str).str.contains("ST|退", regex=True, na=False)].copy()

    for col in ["eps", "revenue_yoy_pct", "net_profit_yoy_pct", "roe_pct", "ocf_per_share", "gross_margin_pct"]:
        merged[col] = pd.to_numeric(merged.get(col), errors="coerce")
    merged["dividend_yield"] = pd.to_numeric(merged.get("dividend_yield"), errors="coerce").fillna(0.0)
    merged["industry"] = merged["industry"].fillna("UNKNOWN")
    merged["latest_announcement_date"] = merged["latest_announcement_date"].fillna(trade_date)

    ocf_yield = merged["ocf_per_share"] / merged["price"]
    pcf = merged["price"] / merged["ocf_per_share"]
    ocf_to_ni = merged["ocf_per_share"] / merged["eps"]
    report_period = infer_report_period_from_date(trade_date)
    roe_annualization_factor = annualization_factor_for_report_period(report_period)
    roe_annualized_proxy = (merged["roe_pct"] / 100.0) * roe_annualization_factor

    proxy_notes = [
        "avg_amount_20d uses current-day amount proxy",
        f"roe_ttm annualizes reported period ROE with factor {roe_annualization_factor:g}",
        "roic_ttm uses annualized roe_ttm proxy",
        "fcf_yield_ttm uses operating-cash-flow yield proxy",
        "debt_to_assets interest_coverage payout_ratio use neutral placeholders",
    ]
    panel = pd.DataFrame(
        {
            "asset": merged["asset"],
            "name": merged["name"],
            "trade_date": trade_date,
            "available_date": merged["latest_announcement_date"].astype(str).str[:10],
            "report_period": report_period,
            "source": "eastmoney_spot+eastmoney_yjbb+eastmoney_dividend_via_akshare",
            "source_vintage": datetime.now().isoformat(timespec="seconds"),
            "data_status": "research_only",
            "industry": merged["industry"],
            "industry_available_date": merged["latest_announcement_date"].astype(str).str[:10],
            "market_cap": merged["market_cap"],
            "avg_amount_20d": merged["amount"],
            "st_flag": 0,
            "suspend_flag": 0,
            "limit_status": "unknown",
            "list_date": parse_eastmoney_list_date(merged.get("list_date_raw")),
            "delist_date": "",
            "pe_ttm": merged["pe_ttm"],
            "pb": merged["pb"],
            "pcf_ocf_ttm": pcf.where(pcf.gt(0) & pcf.replace([math.inf, -math.inf], math.nan).notna()),
            "fcf_yield_ttm": ocf_yield.where(ocf_yield.replace([math.inf, -math.inf], math.nan).notna()),
            "dividend_yield_ttm": merged["dividend_yield"],
            "ev_ebitda": "",
            "roe_ttm": roe_annualized_proxy,
            "roic_ttm": roe_annualized_proxy,
            "gross_margin_stability_3y": "",
            "ocf_to_net_income": ocf_to_ni.where(ocf_to_ni.replace([math.inf, -math.inf], math.nan).notna()),
            "accruals_to_assets": "",
            "debt_to_assets": 0.50,
            "interest_coverage": 4.0,
            "payout_ratio": 0.45,
            "revenue_cagr_3y": merged["revenue_yoy_pct"] / 100.0,
            "net_profit_cagr_3y": merged["net_profit_yoy_pct"] / 100.0,
            "eps_growth_stability_5y": "",
            "price": merged["price"],
            "pct_change": merged["pct_change"],
            "turnover_rate": merged["turnover_rate"],
            "proxy_field_count": 5,
            "source_warning": "; ".join(proxy_notes),
        }
    )

    numeric_required = [
        "pe_ttm",
        "pb",
        "pcf_ocf_ttm",
        "fcf_yield_ttm",
        "dividend_yield_ttm",
        "roe_ttm",
        "roic_ttm",
        "revenue_cagr_3y",
        "net_profit_cagr_3y",
        "ocf_to_net_income",
        "debt_to_assets",
        "interest_coverage",
        "payout_ratio",
        "market_cap",
        "avg_amount_20d",
    ]
    for col in numeric_required:
        panel[col] = pd.to_numeric(panel[col], errors="coerce")

    panel = panel.dropna(
        subset=["pe_ttm", "pb", "roe_ttm", "revenue_cagr_3y", "net_profit_cagr_3y", "avg_amount_20d"]
    ).copy()
    panel["pcf_ocf_ttm"] = panel["pcf_ocf_ttm"].fillna(999.0)
    panel["fcf_yield_ttm"] = panel["fcf_yield_ttm"].fillna(-0.20)
    panel["ocf_to_net_income"] = panel["ocf_to_net_income"].fillna(0.0)
    panel["available_date"] = panel["available_date"].where(panel["available_date"].str.len().eq(10), trade_date)
    panel["industry_available_date"] = panel["industry_available_date"].where(panel["industry_available_date"].str.len().eq(10), trade_date)

    profile = {
        "data_sources": [
            "Eastmoney quote clist API direct request",
            "AkShare stock_yjbb_em Eastmoney earnings report",
            "AkShare stock_fhps_em Eastmoney dividend and distribution",
        ],
        "proxy_fields": proxy_notes,
        "hard_filters": [
            "positive price",
            "positive PE TTM or dynamic PE fallback",
            "positive PB",
            "exclude names containing ST or delisting marker",
            "require earnings report rows for ROE and growth fields",
        ],
    }
    return panel, profile


def is_obvious_value_candidate(row: pd.Series) -> bool:
    return bool(
        not bool(row.get("hard_block", False))
        and not bool(row.get("relative_value_trap_flag", False))
        and float(row.get("composite_score", 0)) >= 0.60
        and 0 < float(row.get("pe_ttm", 999)) <= 12
        and 0 < float(row.get("pb", 999)) <= 1.2
        and float(row.get("roe_ttm", 0)) >= 0.06
        and float(row.get("ocf_to_net_income", 0)) >= 0.60
        and float(row.get("avg_amount_20d", 0)) >= 20_000_000
        and float(row.get("revenue_cagr_3y", -9)) >= -0.10
        and float(row.get("net_profit_cagr_3y", -9)) >= -0.30
        and str(row.get("data_status", "")) == "research_only"
    )


def classify_research_confidence(row: pd.Series) -> str:
    if not row.get("obvious_value_flag"):
        return "watchlist_or_rejected"
    if float(row.get("proxy_field_count", 99)) >= 5:
        return "medium_low_current_snapshot"
    return "medium_current_snapshot"


def infer_report_period_from_date(trade_date: str) -> str:
    year = int(trade_date[:4])
    month = int(trade_date[5:7])
    if month <= 4:
        return f"{year - 1}-12-31"
    if month <= 8:
        return f"{year}-03-31"
    if month <= 10:
        return f"{year}-06-30"
    return f"{year}-09-30"


def annualization_factor_for_report_period(report_period: str) -> float:
    month_day = report_period[5:]
    if month_day == "03-31":
        return 4.0
    if month_day == "06-30":
        return 2.0
    if month_day == "09-30":
        return 4.0 / 3.0
    return 1.0


def parse_eastmoney_list_date(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series([""] * 0)
    text = series.fillna("").astype(str).str.replace(".0", "", regex=False)
    return text.where(text.str.len().eq(8), "").str.replace(r"(\d{4})(\d{2})(\d{2})", r"\1-\2-\3", regex=True)


def write_snapshot_report(
    path: Path,
    enriched: pd.DataFrame,
    top_candidates: pd.DataFrame,
    profile: dict[str, Any],
    top_n: int,
) -> None:
    lines = [
        "# Current A-Share Fundamental Value Snapshot",
        "",
        f"Version: {profile['version']}",
        f"Trade date: {profile['trade_date']}",
        f"Report date: {profile['report_date']}",
        "",
        "## Boundary",
        "",
        profile["research_boundary"],
        "",
        "This run uses current snapshot data. It is useful for watchlist discovery only. It does not prove historical factor efficacy.",
        "",
        "## Data Coverage",
        "",
        f"- Spot rows: {profile['spot_rows']}",
        f"- Earnings rows: {profile['earnings_rows']}",
        f"- Dividend rows: {profile['dividend_rows']}",
        f"- Research panel rows: {profile['panel_rows']}",
        f"- Obvious-value current-snapshot candidates: {profile['obvious_value_candidates']}",
        f"- Top obvious-value output rows: {profile.get('top_obvious_value_output_rows', profile['obvious_value_candidates'])}",
        f"- Industry-relative cheapness risk flags: {profile.get('relative_value_trap_flags', 0)}",
        "",
        "## Proxy Fields",
        "",
    ]
    lines.extend(f"- {item}" for item in profile["proxy_fields"])
    lines.extend(
        [
            "",
            f"## Top {min(top_n, len(top_candidates))} Current-Snapshot Candidates",
            "",
            "| Rank | Asset | Name | Industry | Score | PE | PB | ROE | Dividend Yield | OCF/NI | Bucket | Confidence |",
            "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for rank, row in enumerate(top_candidates.head(top_n).to_dict("records"), start=1):
        lines.append(
            "| {rank} | {asset} | {name} | {industry} | {score:.4f} | {pe:.2f} | {pb:.2f} | {roe:.2%} | {dy:.2%} | {ocfni:.2f} | {bucket} | {confidence} |".format(
                rank=rank,
                asset=row["asset"],
                name=row["name"],
                industry=row["industry"],
                score=float(row["composite_score"]),
                pe=float(row["pe_ttm"]),
                pb=float(row["pb"]),
                roe=float(row["roe_ttm"]),
                dy=float(row["dividend_yield_ttm"]),
                ocfni=float(row["ocf_to_net_income"]),
                bucket=row["bucket"],
                confidence=row["research_confidence"],
            )
        )
    lines.extend(
        [
            "",
            "## Required Next Tests",
            "",
            "- Replace neutral placeholder fields with PIT balance-sheet, interest-coverage, payout, and FCF fields.",
            "- Validate RankIC, grouped returns, neutralized contribution, OOS, costs, turnover, and capacity before promotion.",
            "- Review each candidate manually for sector accounting comparability, one-off earnings, related-party issues, pledges, and liquidity.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
