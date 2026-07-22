#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategy_lab"))

from fundamental_value_os import load_csv_records, run_fundamental_value_agents, write_outputs


SAMPLE_RECORDS = [
    {
        "asset": "600001",
        "name": "Sample Quality Value",
        "trade_date": "2026-06-12",
        "available_date": "2026-04-30",
        "industry": "Utilities",
        "pe_ttm": 8.5,
        "pb": 0.95,
        "pcf_ocf_ttm": 6.1,
        "dividend_yield_ttm": 0.058,
        "roe_ttm": 0.135,
        "roic_ttm": 0.105,
        "revenue_cagr_3y": 0.055,
        "net_profit_cagr_3y": 0.062,
        "ocf_to_net_income": 1.18,
        "fcf_yield_ttm": 0.071,
        "debt_to_assets": 0.48,
        "interest_coverage": 8.2,
        "payout_ratio": 0.48,
        "st_flag": 0,
        "suspend_flag": 0,
        "avg_amount_20d": 185000000,
        "data_status": "research_only",
    },
    {
        "asset": "600002",
        "name": "Sample Deep Value",
        "trade_date": "2026-06-12",
        "available_date": "2026-04-28",
        "industry": "Utilities",
        "pe_ttm": 6.2,
        "pb": 0.68,
        "pcf_ocf_ttm": 4.8,
        "dividend_yield_ttm": 0.066,
        "roe_ttm": 0.098,
        "roic_ttm": 0.082,
        "revenue_cagr_3y": 0.018,
        "net_profit_cagr_3y": 0.012,
        "ocf_to_net_income": 0.92,
        "fcf_yield_ttm": 0.083,
        "debt_to_assets": 0.55,
        "interest_coverage": 5.1,
        "payout_ratio": 0.56,
        "st_flag": 0,
        "suspend_flag": 0,
        "avg_amount_20d": 96000000,
        "data_status": "research_only",
    },
    {
        "asset": "600003",
        "name": "Sample Value Trap",
        "trade_date": "2026-06-12",
        "available_date": "2026-04-29",
        "industry": "Utilities",
        "pe_ttm": 5.4,
        "pb": 0.52,
        "pcf_ocf_ttm": 18.5,
        "dividend_yield_ttm": 0.092,
        "roe_ttm": 0.041,
        "roic_ttm": 0.018,
        "revenue_cagr_3y": -0.055,
        "net_profit_cagr_3y": -0.118,
        "ocf_to_net_income": 0.35,
        "fcf_yield_ttm": -0.021,
        "debt_to_assets": 0.84,
        "interest_coverage": 1.1,
        "payout_ratio": 1.12,
        "st_flag": 1,
        "suspend_flag": 0,
        "avg_amount_20d": 12000000,
        "data_status": "research_only",
    },
    {
        "asset": "000001",
        "name": "Sample Bank Value",
        "trade_date": "2026-06-12",
        "available_date": "2026-04-25",
        "industry": "Banks",
        "pe_ttm": 5.8,
        "pb": 0.58,
        "pcf_ocf_ttm": 7.0,
        "dividend_yield_ttm": 0.071,
        "roe_ttm": 0.112,
        "roic_ttm": 0.078,
        "revenue_cagr_3y": 0.021,
        "net_profit_cagr_3y": 0.016,
        "ocf_to_net_income": 0.88,
        "fcf_yield_ttm": 0.052,
        "debt_to_assets": 0.91,
        "interest_coverage": 4.4,
        "payout_ratio": 0.42,
        "st_flag": 0,
        "suspend_flag": 0,
        "avg_amount_20d": 520000000,
        "data_status": "research_only",
    },
    {
        "asset": "000002",
        "name": "Sample Bank Peer",
        "trade_date": "2026-06-12",
        "available_date": "2026-04-25",
        "industry": "Banks",
        "pe_ttm": 7.4,
        "pb": 0.72,
        "pcf_ocf_ttm": 8.5,
        "dividend_yield_ttm": 0.049,
        "roe_ttm": 0.094,
        "roic_ttm": 0.071,
        "revenue_cagr_3y": 0.012,
        "net_profit_cagr_3y": 0.006,
        "ocf_to_net_income": 0.81,
        "fcf_yield_ttm": 0.041,
        "debt_to_assets": 0.90,
        "interest_coverage": 4.0,
        "payout_ratio": 0.36,
        "st_flag": 0,
        "suspend_flag": 0,
        "avg_amount_20d": 270000000,
        "data_status": "research_only",
    },
    {
        "asset": "300001",
        "name": "Sample Expensive Quality",
        "trade_date": "2026-06-12",
        "available_date": "2026-04-27",
        "industry": "Technology",
        "pe_ttm": 35.0,
        "pb": 5.8,
        "pcf_ocf_ttm": 28.0,
        "dividend_yield_ttm": 0.006,
        "roe_ttm": 0.215,
        "roic_ttm": 0.180,
        "revenue_cagr_3y": 0.224,
        "net_profit_cagr_3y": 0.188,
        "ocf_to_net_income": 1.05,
        "fcf_yield_ttm": 0.018,
        "debt_to_assets": 0.28,
        "interest_coverage": 22.0,
        "payout_ratio": 0.12,
        "st_flag": 0,
        "suspend_flag": 0,
        "avg_amount_20d": 410000000,
        "data_status": "research_only",
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Fundamental Value Research OS smoke test.")
    parser.add_argument("--input", help="Optional UTF-8 CSV input asset panel.")
    parser.add_argument(
        "--output",
        default=str(ROOT / "outputs" / "test" / "fundamental_value_smoke"),
        help="Output directory.",
    )
    args = parser.parse_args()

    records = load_csv_records(args.input) if args.input else SAMPLE_RECORDS
    result = run_fundamental_value_agents(records)
    write_outputs(result, args.output)

    print(f"assets={result['run_manifest']['asset_count']}")
    print(f"output={Path(args.output).resolve()}")
    for row in result["candidate_ranking"][:5]:
        print(f"{row['asset']} {row['name']} {row['bucket']} score={row['composite_score']}")


if __name__ == "__main__":
    main()
