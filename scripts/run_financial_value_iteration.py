#!/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "outputs" / "current_a_share_quality_value_snapshot" / "quality_value_financial_proxy_candidates.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "current_a_share_financial_value_iteration"

CRITICAL_METRICS = {
    "bank": [
        "npl_ratio",
        "provision_coverage_ratio",
        "core_tier1_capital_adequacy_ratio",
        "capital_adequacy_ratio",
        "net_interest_margin",
    ],
    "securities": [
        "risk_coverage_ratio",
        "capital_leverage_ratio",
        "liquidity_coverage_ratio",
        "net_stable_funding_ratio",
        "net_capital",
    ],
    "insurance": [
        "price_to_embedded_value",
        "embedded_value_growth",
        "new_business_value_growth",
        "core_solvency_ratio",
        "comprehensive_solvency_ratio",
        "combined_ratio",
    ],
    "financial_other": [
        "sector_specific_capital_metric",
        "sector_specific_asset_quality_metric",
        "sector_specific_liquidity_metric",
    ],
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run V0.8 financial-sector value iteration on V0.7 financial proxy candidates."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="V0.7 financial proxy candidate CSV.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output directory.")
    parser.add_argument("--trade-date", default="2026-06-12", help="Decision date for current snapshot filtering.")
    parser.add_argument("--top", type=int, default=20, help="Rows to show in report.")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    source = pd.read_csv(Path(args.input), dtype={"asset": str})
    source["asset"] = source["asset"].astype(str).str.zfill(6)
    source["v0_7_rank"] = range(1, len(source) + 1)

    scored_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    for row in source.to_dict("records"):
        enriched, gap = score_financial_candidate(row, args.trade_date)
        scored_rows.append(enriched)
        gap_rows.append(gap)

    scored = pd.DataFrame(scored_rows)
    scored.sort_values(
        ["financial_value_score", "capital_risk_proxy_score", "valuation_context_score"],
        ascending=[False, False, False],
        inplace=True,
    )
    scored["v0_8_financial_rank"] = range(1, len(scored) + 1)
    scored["rank_change_vs_v0_7"] = scored["v0_7_rank"] - scored["v0_8_financial_rank"]

    comparison_cols = [
        "asset",
        "name",
        "industry",
        "financial_subsector",
        "v0_7_rank",
        "v0_8_financial_rank",
        "rank_change_vs_v0_7",
        "composite_score",
        "financial_value_score",
        "valuation_context_score",
        "earnings_quality_score",
        "capital_risk_proxy_score",
        "financial_value_status",
        "missing_critical_metric_count",
        "missing_critical_metrics",
    ]
    comparison = scored[comparison_cols].copy()
    gaps = pd.DataFrame(gap_rows)

    scored_path = output_dir / "financial_sector_value_candidates.csv"
    comparison_path = output_dir / "financial_sector_value_comparison.csv"
    gap_path = output_dir / "financial_sector_data_gap_report.csv"
    report_path = output_dir / "financial_sector_value_report.md"
    summary_path = output_dir / "financial_sector_value_summary.json"

    scored.to_csv(scored_path, index=False, encoding="utf-8-sig")
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    gaps.to_csv(gap_path, index=False, encoding="utf-8-sig")

    summary = {
        "version": "0.8.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input": str(Path(args.input).resolve()),
        "trade_date": args.trade_date,
        "financial_candidates_from_v0_7": int(len(source)),
        "proxy_pass_count": int((scored["proxy_gate_pass"] == True).sum()),
        "proxy_fail_count": int((scored["proxy_gate_pass"] == False).sum()),
        "confirmed_financial_undervaluation_count": int(
            (scored["confirmed_financial_undervaluation_flag"] == True).sum()
        ),
        "status_counts": scored["financial_value_status"].value_counts().to_dict(),
        "research_boundary": (
            "research_only current snapshot; financial-sector scores are proxy ranks until "
            "bank, broker, and insurer regulatory metrics are connected."
        ),
    }
    write_json(summary_path, summary)
    write_report(report_path, scored, gaps, summary, args.top)

    print(f"financial_candidates_from_v0_7={summary['financial_candidates_from_v0_7']}")
    print(f"proxy_pass_count={summary['proxy_pass_count']}")
    print(f"proxy_fail_count={summary['proxy_fail_count']}")
    print(f"confirmed_financial_undervaluation_count={summary['confirmed_financial_undervaluation_count']}")
    print(f"output={output_dir.resolve()}")
    for row in scored.head(10).to_dict("records"):
        print(
            "{asset} {name} financial_score={score:.4f} capital_proxy={capital:.4f} status={status}".format(
                asset=row["asset"],
                name=row["name"],
                score=float(row["financial_value_score"]),
                capital=float(row["capital_risk_proxy_score"]),
                status=row["financial_value_status"],
            )
        )


def score_financial_candidate(row: dict[str, Any], trade_date: str) -> tuple[dict[str, Any], dict[str, Any]]:
    asset = str(row["asset"]).zfill(6)
    subsector = classify_subsector(str(row.get("industry", "")), str(row.get("name", "")))
    balance_metrics = fetch_balance_sheet_metrics(asset, trade_date, subsector)
    missing_critical = CRITICAL_METRICS.get(subsector, CRITICAL_METRICS["financial_other"])

    row.update(balance_metrics)
    row["financial_subsector"] = subsector
    row["missing_critical_metrics"] = "|".join(missing_critical)
    row["missing_critical_metric_count"] = len(missing_critical)
    row["critical_metric_coverage_score"] = 0.0

    valuation_score = valuation_context_score(row, subsector)
    earnings_score = earnings_quality_score(row)
    capital_score = capital_risk_proxy_score(row, subsector)
    shareholder_score = _higher_score(_to_float(row.get("dividend_yield_ttm")), 0.01, 0.05)
    data_coverage_score = row["critical_metric_coverage_score"]

    financial_score = _clamp(
        0.30 * valuation_score
        + 0.25 * earnings_score
        + 0.25 * capital_score
        + 0.10 * shareholder_score
        + 0.10 * data_coverage_score
    )
    proxy_gate = financial_proxy_gate(row, subsector, balance_metrics.get("balance_sheet_fetch_status") == "ok")

    if balance_metrics.get("balance_sheet_fetch_status") != "ok":
        status = "data_fetch_failed"
    elif proxy_gate:
        status = "proxy_pass_regulatory_data_required"
    else:
        status = "cheap_but_proxy_risk_not_cleared"

    row["valuation_context_score"] = round(valuation_score, 4)
    row["earnings_quality_score"] = round(earnings_score, 4)
    row["capital_risk_proxy_score"] = round(capital_score, 4)
    row["financial_shareholder_score"] = round(shareholder_score, 4)
    row["financial_value_score"] = round(financial_score, 4)
    row["proxy_gate_pass"] = bool(proxy_gate)
    row["confirmed_financial_undervaluation_flag"] = False
    row["financial_value_status"] = status
    row["financial_value_notes"] = build_notes(row, subsector)

    gap = {
        "asset": asset,
        "name": row.get("name", ""),
        "financial_subsector": subsector,
        "balance_sheet_fetch_status": balance_metrics.get("balance_sheet_fetch_status", ""),
        "missing_critical_metric_count": len(missing_critical),
        "missing_critical_metrics": "|".join(missing_critical),
        "confirmation_blocked": True,
        "blocking_reason": "sector-critical regulatory metrics not connected in V0.8 current snapshot",
    }
    return row, gap


def fetch_balance_sheet_metrics(asset: str, trade_date: str, subsector: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "balance_sheet_fetch_status": "not_attempted",
        "financial_report_date": "",
        "financial_notice_date": "",
        "total_assets": math.nan,
        "total_liabilities": math.nan,
        "total_equity": math.nan,
        "equity_to_assets": math.nan,
        "asset_leverage": math.nan,
        "balance_sheet_debt_to_assets": math.nan,
        "loan_to_deposit": math.nan,
        "loan_growth_yoy": math.nan,
        "deposit_growth_yoy": math.nan,
        "financial_market_risk_assets_to_equity": math.nan,
        "repo_funding_to_equity": math.nan,
        "customer_deposit_to_liabilities": math.nan,
    }
    try:
        import akshare as ak
    except Exception as exc:  # pragma: no cover - environment dependent
        metrics["balance_sheet_fetch_status"] = f"akshare_import_failed:{type(exc).__name__}"
        return metrics

    symbol = market_symbol(asset)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            balance = ak.stock_balance_sheet_by_report_em(symbol=symbol)
    except Exception as exc:  # pragma: no cover - network dependent
        metrics["balance_sheet_fetch_status"] = f"fetch_failed:{type(exc).__name__}"
        return metrics

    if balance is None or balance.empty:
        metrics["balance_sheet_fetch_status"] = "empty"
        return metrics

    latest = select_latest_available_row(balance, trade_date)
    if latest is None:
        metrics["balance_sheet_fetch_status"] = "no_available_row"
        return metrics

    total_assets = _first_numeric(latest, ["TOTAL_ASSETS"])
    total_liabilities = _first_numeric(latest, ["TOTAL_LIABILITIES"])
    total_equity = _first_numeric(latest, ["TOTAL_PARENT_EQUITY", "TOTAL_EQUITY"])
    metrics.update(
        {
            "balance_sheet_fetch_status": "ok",
            "financial_report_date": _date_string(latest.get("REPORT_DATE")),
            "financial_notice_date": _date_string(latest.get("NOTICE_DATE")),
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "total_equity": total_equity,
            "equity_to_assets": _safe_div(total_equity, total_assets),
            "asset_leverage": _safe_div(total_assets, total_equity),
            "balance_sheet_debt_to_assets": _safe_div(total_liabilities, total_assets),
        }
    )

    if subsector == "bank":
        loan = _first_numeric(latest, ["LOAN_ADVANCE"])
        deposit = _first_numeric(latest, ["ACCEPT_DEPOSIT"])
        metrics.update(
            {
                "loan_to_deposit": _safe_div(loan, deposit),
                "loan_growth_yoy": _percent_to_decimal(_first_numeric(latest, ["LOAN_ADVANCE_YOY"])),
                "deposit_growth_yoy": _percent_to_decimal(_first_numeric(latest, ["ACCEPT_DEPOSIT_YOY"])),
            }
        )
    elif subsector == "securities":
        market_risk_assets = _sum_numeric(
            latest,
            [
                "FVTPL_FINASSET",
                "TRADE_FINASSET",
                "APPOINT_FVTPL_FINASSET",
                "DERIVE_FINASSET",
                "TRADE_FINASSET_NOTFVTPL",
                "CREDITOR_INVEST",
                "OTHER_CREDITOR_INVEST",
                "OTHER_EQUITY_INVEST",
            ],
        )
        repo_funding = _first_numeric(latest, ["SELL_REPO_FINASSET"])
        customer_deposit = _first_numeric(latest, ["CUSTOMER_DEPOSIT"])
        metrics.update(
            {
                "financial_market_risk_assets_to_equity": _safe_div(market_risk_assets, total_equity),
                "repo_funding_to_equity": _safe_div(repo_funding, total_equity),
                "customer_deposit_to_liabilities": _safe_div(customer_deposit, total_liabilities),
            }
        )
    return metrics


def select_latest_available_row(balance: pd.DataFrame, trade_date: str) -> pd.Series | None:
    frame = balance.copy()
    frame["__report_date"] = pd.to_datetime(frame.get("REPORT_DATE"), errors="coerce")
    frame["__notice_date"] = pd.to_datetime(frame.get("NOTICE_DATE"), errors="coerce")
    trade_ts = pd.to_datetime(trade_date)
    if "__notice_date" in frame.columns:
        available = frame[(frame["__notice_date"].isna()) | (frame["__notice_date"] <= trade_ts)].copy()
    else:
        available = frame
    if available.empty:
        return None
    available.sort_values(["__report_date", "__notice_date"], ascending=[False, False], inplace=True)
    return available.iloc[0]


def valuation_context_score(row: dict[str, Any], subsector: str) -> float:
    pe = _to_float(row.get("pe_ttm"))
    pb = _to_float(row.get("pb"))
    roe = _to_float(row.get("roe_ttm"))
    dividend = _to_float(row.get("dividend_yield_ttm"))
    roe_to_pb = _safe_div(roe, pb)

    if subsector == "bank":
        return _mean(
            [
                _lower_score(pb, 0.55, 1.00),
                _lower_score(pe, 6.0, 10.0),
                _higher_score(roe_to_pb, 0.10, 0.22),
                _higher_score(dividend, 0.02, 0.05),
            ]
        )
    if subsector == "securities":
        return _mean(
            [
                _lower_score(pb, 1.00, 1.60),
                _lower_score(pe, 10.0, 16.0),
                _higher_score(roe_to_pb, 0.06, 0.12),
                _higher_score(dividend, 0.015, 0.04),
            ]
        )
    if subsector == "insurance":
        return _mean(
            [
                _lower_score(pb, 0.80, 1.40),
                _lower_score(pe, 8.0, 16.0),
                _higher_score(roe_to_pb, 0.08, 0.16),
                _higher_score(dividend, 0.015, 0.05),
            ]
        )
    return _mean(
        [
            _lower_score(pb, 0.80, 1.50),
            _lower_score(pe, 8.0, 16.0),
            _higher_score(roe_to_pb, 0.06, 0.14),
            _higher_score(dividend, 0.01, 0.04),
        ]
    )


def earnings_quality_score(row: dict[str, Any]) -> float:
    roe = _to_float(row.get("roe_ttm"))
    profitability = _to_float(row.get("profitability_quality"))
    growth = _to_float(row.get("growth_stability"))
    revenue_growth = _to_float(row.get("revenue_cagr_3y"))
    profit_growth = _to_float(row.get("net_profit_cagr_3y"))
    return _mean(
        [
            _higher_score(roe, 0.08, 0.15),
            profitability,
            growth,
            _higher_score(revenue_growth, -0.05, 0.20),
            _higher_score(profit_growth, -0.05, 0.20),
        ]
    )


def capital_risk_proxy_score(row: dict[str, Any], subsector: str) -> float:
    equity_to_assets = _to_float(row.get("equity_to_assets"))
    asset_leverage = _to_float(row.get("asset_leverage"))
    if subsector == "bank":
        return _mean(
            [
                _higher_score(equity_to_assets, 0.055, 0.085),
                _lower_score(asset_leverage, 11.0, 18.0),
                _target_band_score(_to_float(row.get("loan_to_deposit")), 0.55, 0.90, 0.40, 1.05),
                _target_band_score(_to_float(row.get("loan_growth_yoy")), 0.02, 0.12, -0.05, 0.25),
                _higher_score(_to_float(row.get("deposit_growth_yoy")), -0.03, 0.08),
            ]
        )
    if subsector == "securities":
        return _mean(
            [
                _higher_score(equity_to_assets, 0.12, 0.22),
                _lower_score(asset_leverage, 4.5, 8.0),
                _lower_score(_to_float(row.get("financial_market_risk_assets_to_equity")), 2.5, 4.5),
                _lower_score(_to_float(row.get("repo_funding_to_equity")), 0.7, 1.6),
                _higher_score(_to_float(row.get("customer_deposit_to_liabilities")), 0.08, 0.20),
            ]
        )
    return _mean(
        [
            _higher_score(equity_to_assets, 0.08, 0.20),
            _lower_score(asset_leverage, 5.0, 12.0),
        ]
    )


def financial_proxy_gate(row: dict[str, Any], subsector: str, balance_ok: bool) -> bool:
    if not balance_ok:
        return False
    pb = _to_float(row.get("pb"))
    pe = _to_float(row.get("pe_ttm"))
    roe = _to_float(row.get("roe_ttm"))
    equity_to_assets = _to_float(row.get("equity_to_assets"))
    asset_leverage = _to_float(row.get("asset_leverage"))

    if subsector == "bank":
        return all(
            [
                _lte(pb, 0.85),
                _lte(pe, 9.0),
                _gte(roe, 0.10),
                _gte(equity_to_assets, 0.055),
                _between(_to_float(row.get("loan_to_deposit")), 0.45, 0.95),
                _gte(_to_float(row.get("loan_growth_yoy")), -0.05),
                _gte(_to_float(row.get("deposit_growth_yoy")), -0.05),
            ]
        )
    if subsector == "securities":
        return all(
            [
                _lte(pb, 1.25),
                _lte(pe, 12.0),
                _gte(roe, 0.10),
                _lte(asset_leverage, 7.5),
                _lte(_to_float(row.get("financial_market_risk_assets_to_equity")), 4.5),
                _lte(_to_float(row.get("repo_funding_to_equity")), 1.6),
            ]
        )
    if subsector == "insurance":
        return all([_lte(pb, 1.20), _lte(pe, 12.0), _gte(roe, 0.08), _lte(asset_leverage, 12.0)])
    return all([_lte(pb, 1.20), _lte(pe, 12.0), _gte(roe, 0.08)])


def build_notes(row: dict[str, Any], subsector: str) -> str:
    if row.get("balance_sheet_fetch_status") != "ok":
        return f"balance_sheet={row.get('balance_sheet_fetch_status')}"
    common = [
        f"PB={_fmt(row.get('pb'))}",
        f"PE={_fmt(row.get('pe_ttm'))}",
        f"ROE={_fmt_pct(row.get('roe_ttm'))}",
        f"equity/assets={_fmt_pct(row.get('equity_to_assets'))}",
        f"leverage={_fmt(row.get('asset_leverage'))}",
    ]
    if subsector == "bank":
        common.extend(
            [
                f"LDR={_fmt_pct(row.get('loan_to_deposit'))}",
                f"loan_yoy={_fmt_pct(row.get('loan_growth_yoy'))}",
                f"deposit_yoy={_fmt_pct(row.get('deposit_growth_yoy'))}",
            ]
        )
    elif subsector == "securities":
        common.extend(
            [
                f"market_risk_assets/equity={_fmt(row.get('financial_market_risk_assets_to_equity'))}",
                f"repo/equity={_fmt(row.get('repo_funding_to_equity'))}",
            ]
        )
    common.append("blocked_confirmation=missing_sector_regulatory_metrics")
    return "; ".join(common)


def write_report(path: Path, scored: pd.DataFrame, gaps: pd.DataFrame, summary: dict[str, Any], top: int) -> None:
    lines = [
        "# Financial-Sector Value Iteration",
        "",
        f"Version: {summary['version']}",
        "",
        "## Boundary",
        "",
        summary["research_boundary"],
        "",
        "## Why This Iteration Exists",
        "",
        "Financial stocks were dominating the value-quality list. That can mean the sector is generally cheap, but it can also mean the market is pricing credit, capital, liquidity, trading-book, or insurance-liability risk.",
        "",
        "V0.8 therefore splits financial candidates into proxy candidates and confirmed candidates. With the current data, no financial stock can be confirmed as undervalued because sector-critical regulatory metrics are still missing.",
        "",
        "## Summary",
        "",
        f"- V0.7 financial proxy candidates: {summary['financial_candidates_from_v0_7']}",
        f"- V0.8 proxy gate pass: {summary['proxy_pass_count']}",
        f"- V0.8 proxy gate fail: {summary['proxy_fail_count']}",
        f"- Confirmed financial undervaluation: {summary['confirmed_financial_undervaluation_count']}",
        "",
        "## Financial Factors Added",
        "",
        "- Common: PB, PE, ROE/PB, ROE, dividend yield, profitability, growth.",
        "- Banks: loan-to-deposit, loan growth, deposit growth, equity/assets, asset leverage.",
        "- Securities: asset leverage, equity/assets, market-risk assets/equity, repo funding/equity, customer deposits/liabilities.",
        "- Blocking data gaps: bank NPL/provision/capital adequacy/NIM; broker net-capital/risk/liquidity ratios; insurer EV/NBV/solvency metrics.",
        "",
        f"## Top {min(top, len(scored))} Financial Proxy Ranking",
        "",
        "| Rank | Asset | Name | Subsector | V0.7 Rank | Score | PB | PE | ROE | Capital Proxy | Key Proxy | Status |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in scored.head(top).to_dict("records"):
        lines.append(
            "| {rank} | {asset} | {name} | {subsector} | {old_rank} | {score:.4f} | {pb:.2f} | {pe:.2f} | {roe} | {capital:.4f} | {proxy} | {status} |".format(
                rank=int(row["v0_8_financial_rank"]),
                asset=str(row["asset"]).zfill(6),
                name=row["name"],
                subsector=row["financial_subsector"],
                old_rank=int(row["v0_7_rank"]),
                score=float(row["financial_value_score"]),
                pb=float(row["pb"]),
                pe=float(row["pe_ttm"]),
                roe=_fmt_pct(row.get("roe_ttm")),
                capital=float(row["capital_risk_proxy_score"]),
                proxy=key_proxy_text(row),
                status=row["financial_value_status"],
            )
        )
    lines.extend(
        [
            "",
            "## Data Gaps Blocking Confirmation",
            "",
            "| Asset | Name | Subsector | Missing Critical Metrics |",
            "|---|---|---|---|",
        ]
    )
    for row in gaps.to_dict("records"):
        lines.append(
            "| {asset} | {name} | {subsector} | {missing} |".format(
                asset=str(row["asset"]).zfill(6),
                name=row["name"],
                subsector=row["financial_subsector"],
                missing=row["missing_critical_metrics"],
            )
        )
    lines.extend(
        [
            "",
            "## Required Next Step",
            "",
            "Connect bank regulatory metrics, broker risk-control metrics, and insurer EV/solvency metrics from PIT annual/interim reports or a licensed data provider before promoting any financial candidate beyond `research_only`.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def key_proxy_text(row: dict[str, Any]) -> str:
    subsector = row.get("financial_subsector")
    if subsector == "bank":
        return "LDR={ldr}; equity/assets={eq}".format(
            ldr=_fmt_pct(row.get("loan_to_deposit")),
            eq=_fmt_pct(row.get("equity_to_assets")),
        )
    if subsector == "securities":
        return "risk/equity={risk}; repo/equity={repo}".format(
            risk=_fmt(row.get("financial_market_risk_assets_to_equity")),
            repo=_fmt(row.get("repo_funding_to_equity")),
        )
    return "equity/assets={eq}; leverage={lev}".format(
        eq=_fmt_pct(row.get("equity_to_assets")),
        lev=_fmt(row.get("asset_leverage")),
    )


def classify_subsector(industry: str, name: str) -> str:
    text = f"{industry} {name}"
    if "银行" in text:
        return "bank"
    if "证券" in text or "券商" in text:
        return "securities"
    if "保险" in text:
        return "insurance"
    return "financial_other"


def market_symbol(asset: str) -> str:
    if asset.startswith(("6", "9")):
        return f"SH{asset}"
    if asset.startswith(("0", "2", "3")):
        return f"SZ{asset}"
    if asset.startswith(("4", "8")):
        return f"BJ{asset}"
    return asset


def _first_numeric(row: pd.Series, columns: list[str]) -> float:
    for column in columns:
        if column in row:
            value = _to_float(row.get(column))
            if value is not None:
                return value
    return math.nan


def _sum_numeric(row: pd.Series, columns: list[str]) -> float:
    values = [_to_float(row.get(column)) for column in columns if column in row]
    usable = [value for value in values if value is not None]
    return sum(usable) if usable else math.nan


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _safe_div(numerator: float | None, denominator: float | None) -> float:
    if numerator is None or denominator is None:
        return math.nan
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0:
        return math.nan
    return numerator / denominator


def _percent_to_decimal(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return math.nan
    return value / 100.0


def _date_string(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return ""
    return ts.strftime("%Y-%m-%d")


def _lower_score(value: float | None, best: float, worst: float) -> float:
    if value is None or not math.isfinite(value):
        return 0.35
    if value <= best:
        return 1.0
    if value >= worst:
        return 0.0
    return _clamp((worst - value) / (worst - best))


def _higher_score(value: float | None, worst: float, best: float) -> float:
    if value is None or not math.isfinite(value):
        return 0.35
    if value >= best:
        return 1.0
    if value <= worst:
        return 0.0
    return _clamp((value - worst) / (best - worst))


def _target_band_score(value: float | None, low: float, high: float, hard_low: float, hard_high: float) -> float:
    if value is None or not math.isfinite(value):
        return 0.35
    if low <= value <= high:
        return 1.0
    if hard_low <= value < low:
        return _clamp((value - hard_low) / (low - hard_low))
    if high < value <= hard_high:
        return _clamp((hard_high - value) / (hard_high - high))
    return 0.0


def _mean(values: list[float | None]) -> float:
    usable = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not usable:
        return 0.35
    return _clamp(sum(usable) / len(usable))


def _gte(value: float | None, threshold: float) -> bool:
    return value is not None and math.isfinite(value) and value >= threshold


def _lte(value: float | None, threshold: float) -> bool:
    return value is not None and math.isfinite(value) and value <= threshold


def _between(value: float | None, lower: float, upper: float) -> bool:
    return value is not None and math.isfinite(value) and lower <= value <= upper


def _fmt(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return "NA"
    return f"{number:.2f}"


def _fmt_pct(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return "NA"
    return f"{number:.2%}"


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
