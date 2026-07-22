#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_DIR = ROOT / "outputs" / "current_a_share_value_snapshot"
DEFAULT_OUTPUT = ROOT / "outputs" / "current_a_share_quality_value_snapshot"


QUALITY_THRESHOLDS = {
    "composite_score_min": 0.70,
    "profitability_quality_min": 0.70,
    "growth_stability_min": 0.50,
    "cash_flow_safety_min": 0.55,
    "shareholder_return_min": 0.85,
    "roe_ttm_min": 0.10,
    "ocf_to_net_income_min": 0.80,
    "total_penalty_max": 0.05,
}


FINANCIAL_KEYWORDS = ("银行", "证券", "保险", "多元金融")


def main() -> None:
    parser = argparse.ArgumentParser(description="Filter obvious-value candidates into undervalued-and-quality candidates.")
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR), help="Directory from V0.6 current snapshot.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output directory.")
    parser.add_argument("--top", type=int, default=30, help="Top rows to write in report table.")
    args = parser.parse_args()

    snapshot_dir = Path(args.snapshot_dir)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    enriched = pd.read_csv(snapshot_dir / "current_value_candidates_enriched.csv", dtype={"asset": str})
    previous_top = pd.read_csv(snapshot_dir / "top_obvious_value_candidates.csv", dtype={"asset": str})
    enriched["asset"] = enriched["asset"].astype(str).str.zfill(6)
    previous_top["asset"] = previous_top["asset"].astype(str).str.zfill(6)
    obvious_all = enriched[enriched["obvious_value_flag"] == True].copy()

    quality_all = obvious_all[quality_mask(obvious_all)].copy()
    quality_all["asset"] = quality_all["asset"].astype(str).str.zfill(6)
    quality_all["quality_value_flag"] = True
    quality_all["sector_quality_status"] = quality_all["industry"].apply(classify_sector_quality_status)
    quality_all["quality_gate_notes"] = quality_all.apply(build_quality_gate_notes, axis=1)
    quality_all.sort_values(
        [
            "composite_score",
            "profitability_quality",
            "growth_stability",
            "cash_flow_safety",
        ],
        ascending=[False, False, False, False],
        inplace=True,
    )

    previous_top_assets = set(previous_top["asset"].astype(str).str.zfill(6))
    quality_assets = set(quality_all["asset"].astype(str).str.zfill(6))
    obvious_assets = set(obvious_all["asset"].astype(str).str.zfill(6))

    previous_top_cmp = previous_top.copy()
    previous_top_cmp["asset"] = previous_top_cmp["asset"].astype(str).str.zfill(6)
    previous_top_cmp["quality_value_flag"] = previous_top_cmp["asset"].isin(quality_assets)
    previous_top_cmp["quality_filter_status"] = previous_top_cmp.apply(
        lambda row: "kept_quality_value" if row["quality_value_flag"] else explain_drop(row),
        axis=1,
    )

    quality_new_vs_top = quality_all[~quality_all["asset"].astype(str).str.zfill(6).isin(previous_top_assets)].copy()
    quality_financial = quality_all[
        quality_all["sector_quality_status"] == "financial_sector_proxy_quality_needs_special_metrics"
    ].copy()
    quality_nonfinancial = quality_all[
        quality_all["sector_quality_status"] == "standard_current_snapshot_quality_proxy"
    ].copy()

    top_quality_path = output_dir / "top_quality_value_candidates.csv"
    comparison_path = output_dir / "quality_vs_previous_obvious_comparison.csv"
    newly_promoted_path = output_dir / "quality_candidates_outside_previous_top30.csv"
    financial_path = output_dir / "quality_value_financial_proxy_candidates.csv"
    nonfinancial_path = output_dir / "quality_value_nonfinancial_candidates.csv"
    quality_all.to_csv(top_quality_path, index=False, encoding="utf-8-sig")
    previous_top_cmp.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    quality_new_vs_top.to_csv(newly_promoted_path, index=False, encoding="utf-8-sig")
    quality_financial.to_csv(financial_path, index=False, encoding="utf-8-sig")
    quality_nonfinancial.to_csv(nonfinancial_path, index=False, encoding="utf-8-sig")

    summary = {
        "version": "0.7.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_dir": str(snapshot_dir.resolve()),
        "previous_obvious_full_count": len(obvious_assets),
        "previous_top_output_count": len(previous_top_assets),
        "quality_value_count": len(quality_assets),
        "quality_financial_proxy_count": int(len(quality_financial)),
        "quality_nonfinancial_count": int(len(quality_nonfinancial)),
        "quality_inside_previous_top30": len(quality_assets & previous_top_assets),
        "quality_outside_previous_top30": len(quality_assets - previous_top_assets),
        "previous_top_removed_by_quality_gate": len(previous_top_assets - quality_assets),
        "thresholds": QUALITY_THRESHOLDS,
        "research_boundary": "research_only current snapshot; quality gate uses proxy fields and is not PIT validation or investment advice",
    }
    write_json(output_dir / "quality_value_summary.json", summary)
    write_report(output_dir / "quality_value_comparison_report.md", quality_all, previous_top_cmp, quality_new_vs_top, summary, args.top)

    print(f"previous_obvious_full_count={summary['previous_obvious_full_count']}")
    print(f"previous_top_output_count={summary['previous_top_output_count']}")
    print(f"quality_value_count={summary['quality_value_count']}")
    print(f"quality_financial_proxy_count={summary['quality_financial_proxy_count']}")
    print(f"quality_nonfinancial_count={summary['quality_nonfinancial_count']}")
    print(f"quality_inside_previous_top30={summary['quality_inside_previous_top30']}")
    print(f"quality_outside_previous_top30={summary['quality_outside_previous_top30']}")
    print(f"previous_top_removed_by_quality_gate={summary['previous_top_removed_by_quality_gate']}")
    print(f"output={output_dir.resolve()}")
    for row in quality_all.head(10).to_dict("records"):
        print(
            "{asset} {name} score={score:.4f} profit={profit:.4f} growth={growth:.4f} safety={safety:.4f}".format(
                asset=row["asset"],
                name=row["name"],
                score=float(row["composite_score"]),
                profit=float(row["profitability_quality"]),
                growth=float(row["growth_stability"]),
                safety=float(row["cash_flow_safety"]),
            )
        )


def quality_mask(df: pd.DataFrame) -> pd.Series:
    return (
        (df["obvious_value_flag"] == True)
        & (df["relative_value_trap_flag"] == False)
        & (df["hard_block"] == False)
        & (df["composite_score"] >= QUALITY_THRESHOLDS["composite_score_min"])
        & (df["profitability_quality"] >= QUALITY_THRESHOLDS["profitability_quality_min"])
        & (df["growth_stability"] >= QUALITY_THRESHOLDS["growth_stability_min"])
        & (df["cash_flow_safety"] >= QUALITY_THRESHOLDS["cash_flow_safety_min"])
        & (df["shareholder_return"] >= QUALITY_THRESHOLDS["shareholder_return_min"])
        & (df["roe_ttm"] >= QUALITY_THRESHOLDS["roe_ttm_min"])
        & (df["ocf_to_net_income"] >= QUALITY_THRESHOLDS["ocf_to_net_income_min"])
        & (df["total_penalty"] <= QUALITY_THRESHOLDS["total_penalty_max"])
    )


def explain_drop(row: pd.Series) -> str:
    reasons: list[str] = []
    for column, threshold_key, label in [
        ("composite_score", "composite_score_min", "score"),
        ("profitability_quality", "profitability_quality_min", "profitability"),
        ("growth_stability", "growth_stability_min", "growth"),
        ("cash_flow_safety", "cash_flow_safety_min", "cash_flow_safety"),
        ("shareholder_return", "shareholder_return_min", "shareholder_return"),
        ("roe_ttm", "roe_ttm_min", "roe"),
        ("ocf_to_net_income", "ocf_to_net_income_min", "ocf_to_net_income"),
    ]:
        if float(row.get(column, 0)) < QUALITY_THRESHOLDS[threshold_key]:
            reasons.append(f"{label}_below_gate")
    if float(row.get("total_penalty", 1)) > QUALITY_THRESHOLDS["total_penalty_max"]:
        reasons.append("penalty_above_gate")
    if bool(row.get("relative_value_trap_flag", False)):
        reasons.append("relative_value_trap_flag")
    return ";".join(reasons) if reasons else "not_in_quality_set"


def classify_sector_quality_status(industry: str) -> str:
    if any(keyword in str(industry) for keyword in FINANCIAL_KEYWORDS):
        return "financial_sector_proxy_quality_needs_special_metrics"
    return "standard_current_snapshot_quality_proxy"


def build_quality_gate_notes(row: pd.Series) -> str:
    notes = [
        f"profitability={float(row['profitability_quality']):.4f}",
        f"growth={float(row['growth_stability']):.4f}",
        f"safety={float(row['cash_flow_safety']):.4f}",
        f"roe={float(row['roe_ttm']):.2%}",
        f"ocf_to_net_income={float(row['ocf_to_net_income']):.2f}",
    ]
    return "; ".join(notes)


def write_report(
    path: Path,
    quality_all: pd.DataFrame,
    previous_top_cmp: pd.DataFrame,
    quality_new_vs_top: pd.DataFrame,
    summary: dict[str, Any],
    top: int,
) -> None:
    lines = [
        "# Current A-Share Undervalued And Quality Snapshot",
        "",
        f"Version: {summary['version']}",
        "",
        "## Boundary",
        "",
        summary["research_boundary"],
        "",
        "## Quality Gate",
        "",
    ]
    for key, value in summary["thresholds"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Comparison With Previous Obvious-Value Test",
            "",
            f"- Previous obvious-value full count: {summary['previous_obvious_full_count']}",
            f"- Previous Top output count: {summary['previous_top_output_count']}",
            f"- Undervalued-and-quality count: {summary['quality_value_count']}",
            f"- Financial-sector proxy-quality count: {summary['quality_financial_proxy_count']}",
            f"- Non-financial quality count: {summary['quality_nonfinancial_count']}",
            f"- Quality candidates inside previous Top output: {summary['quality_inside_previous_top30']}",
            f"- Quality candidates outside previous Top output: {summary['quality_outside_previous_top30']}",
            f"- Previous Top output removed by quality gate: {summary['previous_top_removed_by_quality_gate']}",
            "",
            f"## Top {min(top, len(quality_all))} Undervalued-And-Quality Candidates",
            "",
            "| Rank | Asset | Name | Industry | Score | PE | PB | Profit | Growth | Safety | ROE | OCF/NI | Sector Quality Status |",
            "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for rank, row in enumerate(quality_all.head(top).to_dict("records"), start=1):
        lines.append(
            "| {rank} | {asset} | {name} | {industry} | {score:.4f} | {pe:.2f} | {pb:.2f} | {profit:.4f} | {growth:.4f} | {safety:.4f} | {roe:.2%} | {ocfni:.2f} | {sector} |".format(
                rank=rank,
                asset=str(row["asset"]).zfill(6),
                name=row["name"],
                industry=row["industry"],
                score=float(row["composite_score"]),
                pe=float(row["pe_ttm"]),
                pb=float(row["pb"]),
                profit=float(row["profitability_quality"]),
                growth=float(row["growth_stability"]),
                safety=float(row["cash_flow_safety"]),
                roe=float(row["roe_ttm"]),
                ocfni=float(row["ocf_to_net_income"]),
                sector=row["sector_quality_status"],
            )
        )
    lines.extend(
        [
            "",
            "## Previous Top Rows Removed By Quality Gate",
            "",
            "| Asset | Name | Industry | Score | Removed Reason |",
            "|---|---|---|---:|---|",
        ]
    )
    removed = previous_top_cmp[previous_top_cmp["quality_value_flag"] == False]
    for row in removed.to_dict("records"):
        lines.append(
            "| {asset} | {name} | {industry} | {score:.4f} | {reason} |".format(
                asset=str(row["asset"]).zfill(6),
                name=row["name"],
                industry=row["industry"],
                score=float(row["composite_score"]),
                reason=row["quality_filter_status"],
            )
        )
    if len(quality_new_vs_top):
        lines.extend(
            [
                "",
                "## Quality Candidates Outside Previous Top Output",
                "",
                "| Asset | Name | Industry | Score | Profit | Growth | Safety |",
                "|---|---|---|---:|---:|---:|---:|",
            ]
        )
        for row in quality_new_vs_top.to_dict("records"):
            lines.append(
                "| {asset} | {name} | {industry} | {score:.4f} | {profit:.4f} | {growth:.4f} | {safety:.4f} |".format(
                    asset=str(row["asset"]).zfill(6),
                    name=row["name"],
                    industry=row["industry"],
                    score=float(row["composite_score"]),
                    profit=float(row["profitability_quality"]),
                    growth=float(row["growth_stability"]),
                    safety=float(row["cash_flow_safety"]),
                )
            )
    lines.extend(
        [
            "",
            "## Required Next Tests",
            "",
            "- Replace current proxy fields with PIT balance-sheet, payout, interest coverage, non-performing loan, capital adequacy, pledge, audit-opinion, litigation, and related-party data.",
            "- Run RankIC, group returns, neutralized contribution, OOS, costs, turnover, and capacity before any promotion.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
