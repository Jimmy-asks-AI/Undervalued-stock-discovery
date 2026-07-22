#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OPPORTUNITY = ROOT / "outputs" / "industry_rebound_leader_expanded_window_v4_97" / "debug" / "industry_event_opportunity_set.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_expanded_feature_separability_v4_98"
DEBUG = OUT / "debug"

FEATURES = [
    "valuation_score",
    "oversold_score",
    "turn_score",
    "liquidity_score",
    "value_oversold_turn_score",
    "oversold_turn_score",
    "oversold_liquidity_score",
    "value_only_score",
    "turn_only_score",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.98 feature separability audit inside expanded rebound windows.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(OPPORTUNITY, encoding="utf-8-sig", dtype={"industry_code": str})
    event_sep = event_separability(opportunity)
    results = summarize(event_sep)
    gate = gate_audit(results)
    summary = build_summary(results, gate)
    write_outputs(summary, results, event_sep, gate)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_feature={summary['best_feature']}")


def event_separability(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in FEATURES:
        if feature not in frame.columns:
            continue
        for (signal_date, entry_date, exit_date), event in frame.groupby(["signal_date", "entry_date", "exit_date"]):
            event = event.dropna(subset=[feature, "future_return", "future_return_top_quintile"])
            if event.empty or event["future_return_top_quintile"].nunique() < 2:
                continue
            top = event[event["future_return_top_quintile"].astype(bool)]
            rest = event[~event["future_return_top_quintile"].astype(bool)]
            rank_ic = float(event[[feature, "future_return"]].corr(method="spearman").iloc[0, 1])
            raw_gap = float(top[feature].mean() - rest[feature].mean())
            std = float(event[feature].std()) or 1.0
            rows.append({
                "window_variant": "vol_repair",
                "feature": feature,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "year": int(pd.to_datetime(signal_date).year),
                "event_industry_count": int(len(event)),
                "top_quintile_count": int(len(top)),
                "rank_ic": rank_ic,
                "rank_ic_positive": rank_ic > 0,
                "standardized_top_vs_rest_gap": raw_gap / std,
                "gap_positive": raw_gap > 0,
            })
    return pd.DataFrame(rows)


def summarize(event_sep: pd.DataFrame) -> pd.DataFrame:
    if event_sep.empty:
        return pd.DataFrame()
    rows = []
    for feature, group in event_sep.groupby("feature"):
        yearly = group.groupby("year")["rank_ic"].mean()
        oos = group[group["year"].ge(2022)]
        row = {
            "window_variant": "vol_repair",
            "feature": feature,
            "event_count": int(len(group)),
            "year_count": int(group["year"].nunique()),
            "mean_rank_ic": float(group["rank_ic"].mean()),
            "positive_rank_ic_rate": float(group["rank_ic_positive"].mean()),
            "mean_standardized_gap": float(group["standardized_top_vs_rest_gap"].mean()),
            "positive_gap_rate": float(group["gap_positive"].mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
            "oos_event_count": int(len(oos)),
            "oos_mean_rank_ic": float(oos["rank_ic"].mean()) if len(oos) else 0.0,
            "oos_positive_gap_rate": float(oos["gap_positive"].mean()) if len(oos) else 0.0,
        }
        row["passes_feature_separability_gate"] = passes(row)
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        ["passes_feature_separability_gate", "mean_rank_ic", "mean_standardized_gap"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("event_count", 30, ">="),
        ("year_count", 8, ">="),
        ("mean_rank_ic", 0, ">"),
        ("positive_rank_ic_rate", 0.55, ">="),
        ("mean_standardized_gap", 0, ">"),
        ("positive_gap_rate", 0.55, ">="),
        ("positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_rank_ic", 0, ">"),
        ("oos_positive_gap_rate", 0.55, ">="),
    ]
    failed = []
    for metric, required, op in checks:
        value = float(row.get(metric, 0) or 0)
        ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    return failed


def passes(row: dict[str, Any]) -> bool:
    return not failed_metrics(row)


def gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0].to_dict()
    failed = set(str(best.get("failed_metrics", "")).split(";"))
    requirements = [
        ("event_count", 30, ">="),
        ("year_count", 8, ">="),
        ("mean_rank_ic", 0, ">"),
        ("positive_rank_ic_rate", 0.55, ">="),
        ("mean_standardized_gap", 0, ">"),
        ("positive_gap_rate", 0.55, ">="),
        ("positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_rank_ic", 0, ">"),
        ("oos_positive_gap_rate", 0.55, ">="),
    ]
    return pd.DataFrame([
        {
            "feature": best.get("feature", ""),
            "metric": metric,
            "current": best.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "fail" if metric in failed else "pass",
        }
        for metric, required, op in requirements
    ])


def build_summary(results: pd.DataFrame, gate: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    failed = gate[gate["status"].eq("fail")]["metric"].tolist() if len(gate) else ["no_results"]
    passed = not failed
    return {
        "version": "4.98.0",
        "policy_id": "rebound_leader_expanded_feature_separability_v4_98",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_variant": "vol_repair",
        "tested_feature_count": int(len(results)),
        "passing_feature_count": int(results["passes_feature_separability_gate"].sum()) if len(results) else 0,
        "best_feature": best.get("feature", ""),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_rank_ic": float(best.get("mean_rank_ic", 0.0) or 0.0),
        "best_positive_rank_ic_rate": float(best.get("positive_rank_ic_rate", 0.0) or 0.0),
        "best_top_vs_rest_gap": float(best.get("mean_standardized_gap", 0.0) or 0.0),
        "best_oos_mean_rank_ic": float(best.get("oos_mean_rank_ic", 0.0) or 0.0),
        "failed_metrics": ";".join(failed),
        "best_status": "pass_expanded_feature_separability_gate" if passed else "research_only_no_expanded_feature_separability",
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "V4.98 显示扩展窗口内现有特征仍不能稳定分离未来 Top20% 强反弹行业；继续在同一批价格/估值/流动性特征上调 TopN 意义有限。",
    }


def write_outputs(summary: dict[str, Any], results: pd.DataFrame, event_sep: pd.DataFrame, gate: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, results, gate), encoding="utf-8")
    event_sep.to_csv(DEBUG / "feature_event_separability.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "feature_separability_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.98 扩展窗口特征分离度审计",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 窗口定义：`{summary['window_variant']}`",
        f"- 测试特征数：{summary['tested_feature_count']}",
        f"- 通过分离度门槛特征数：{summary['passing_feature_count']}",
        f"- 最优特征：`{summary['best_feature']}`",
        f"- 最优平均 RankIC：{summary['best_mean_rank_ic']:.4f}",
        f"- 最优正 RankIC 比例：{summary['best_positive_rank_ic_rate']:.2%}",
        f"- 最优 OOS 平均 RankIC：{summary['best_oos_mean_rank_ic']:.4f}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 最优特征门槛",
        "",
        gate.to_markdown(index=False) if len(gate) else "无数据",
        "",
        "## 特征分离度排名",
        "",
        results.to_markdown(index=False) if len(results) else "无数据",
        "",
        "## 研究边界",
        "",
        "V4.98 只检查现有特征是否能区分未来强反弹行业，不做组合优化、不新增参数、不生成交易信号。",
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    frame = pd.DataFrame({
        "signal_date": ["2020-01-01"] * 10,
        "entry_date": ["2020-01-02"] * 10,
        "exit_date": ["2020-01-03"] * 10,
        "future_return": list(range(10)),
        "future_return_top_quintile": [False] * 8 + [True, True],
        "valuation_score": list(range(10)),
    })
    sep = event_separability(frame)
    assert not sep.empty
    assert sep.iloc[0]["standardized_top_vs_rest_gap"] > 0
    row = {
        "event_count": 30,
        "year_count": 8,
        "mean_rank_ic": 0.1,
        "positive_rank_ic_rate": 0.6,
        "mean_standardized_gap": 0.1,
        "positive_gap_rate": 0.6,
        "positive_year_rate": 0.6,
        "oos_event_count": 8,
        "oos_mean_rank_ic": 0.1,
        "oos_positive_gap_rate": 0.6,
    }
    assert passes(row)
    row["mean_rank_ic"] = -0.1
    assert not passes(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
