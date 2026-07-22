#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_leader_robust_grid_v4_80 as v480


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "outputs" / "audit" / "rebound_leader_early_confirmation_audit_v5_13" / "debug" / "early_confirmation_opportunity_set.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_confirmation_filter_audit_v5_14"
DEBUG = OUT / "debug"
FEATURES = ["early_strength_rank", "early_beta_score"]
TOP_NS = [5, 10, 20]
FILTERS = {
    "all": lambda df: pd.Series(True, index=df.index),
    "no_severe_early_selloff": lambda df: df["early_benchmark_return"] > -0.05,
    "calm_confirmation": lambda df: df["early_relative_dispersion"] < 0.03,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.14 confirmation filter audit.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    panel = pd.read_csv(PANEL, encoding="utf-8-sig")
    panel = attach_event_filters(panel)
    events = evaluate(panel)
    results = summarize(events)
    summary = build_summary(results)
    write_outputs(summary, panel, events, results)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def attach_event_filters(panel: pd.DataFrame) -> pd.DataFrame:
    metrics = panel.groupby(["signal_date", "entry_date", "confirm_date", "exit_date", "confirm_days"]).agg(
        early_benchmark_return=("early_benchmark_return", "first"),
        early_relative_dispersion=("early_relative_return", "std"),
    ).reset_index()
    for name, func in FILTERS.items():
        metrics[f"filter_{name}"] = func(metrics)
    return panel.merge(metrics.drop(columns=["early_benchmark_return", "early_relative_dispersion"]), on=["signal_date", "entry_date", "confirm_date", "exit_date", "confirm_days"], how="left")


def evaluate(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for filter_name in FILTERS:
        filtered = panel[panel[f"filter_{filter_name}"]].copy()
        for feature in FEATURES:
            for top_n in TOP_NS:
                for confirm_days, subset in filtered.groupby("confirm_days"):
                    for keys, event in subset.groupby(["signal_date", "entry_date", "confirm_date", "exit_date"], sort=False):
                        signal_date, entry_date, confirm_date, exit_date = keys
                        event = event.dropna(subset=[feature, "future_return_after_confirm"])
                        if event.empty:
                            continue
                        selected = event.sort_values(feature, ascending=False).head(top_n)
                        top_cut = event["future_return_after_confirm"].quantile(0.8)
                        rows.append({
                            "filter_name": filter_name,
                            "feature": feature,
                            "top_n": int(top_n),
                            "confirm_days": int(confirm_days),
                            "signal_date": signal_date,
                            "entry_date": entry_date,
                            "confirm_date": confirm_date,
                            "exit_date": exit_date,
                            "year": int(pd.to_datetime(signal_date).year),
                            "relative_return": float(selected["relative_return_after_confirm"].mean()),
                            "relative_win": float(selected["relative_return_after_confirm"].mean()) > 0,
                            "top_quintile_hit_rate": float((selected["future_return_after_confirm"] >= top_cut).mean()),
                        })
    return pd.DataFrame(rows)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, group in events.groupby(["filter_name", "feature", "top_n", "confirm_days"]):
        filter_name, feature, top_n, confirm_days = keys
        oos = group[group["year"].ge(2022)]
        yearly = group.groupby("year")["relative_return"].mean()
        row = {
            "filter_name": filter_name,
            "feature": feature,
            "top_n": int(top_n),
            "confirm_days": int(confirm_days),
            "event_count": int(len(group)),
            "year_count": int(group["year"].nunique()),
            "mean_relative_return": float(group["relative_return"].mean()),
            "median_relative_return": float(group["relative_return"].median()),
            "relative_win_rate": float(group["relative_win"].mean()),
            "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
        }
        row["point_gate_passed"] = passes_point_gate(row)
        row.update(v480.robustness_metrics(group, int(top_n)) if row["point_gate_passed"] else {})
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["passes_gate", "point_gate_passed", "mean_relative_return"], ascending=[False, False, False])


def failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("event_count", 30, ">="), ("year_count", 8, ">="),
        ("mean_relative_return", 0, ">"), ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="), ("top_quintile_hit_rate", 0.30, ">="),
        ("oos_event_count", 8, ">="), ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="), ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="), ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="), ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0, ">"),
    ]
    failed = []
    for metric, required, op in checks:
        if op == "==":
            ok = row.get(metric) == required
        else:
            value = float(row.get(metric, 0) or 0)
            ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    return failed


def passes_point_gate(row: dict[str, Any]) -> bool:
    point = {
        "event_count", "year_count", "mean_relative_return", "median_relative_return",
        "relative_win_rate", "top_quintile_hit_rate", "oos_event_count",
        "oos_mean_relative_return", "oos_relative_win_rate",
    }
    return not (point & set(failed_metrics(row)))


def build_summary(results: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_gate", False))
    return {
        "version": "5.14.0",
        "policy_id": "rebound_leader_confirmation_filter_audit_v5_14",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_rule_count": int(len(results)),
        "best_filter": best.get("filter_name", ""),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_confirm_days": int(best.get("confirm_days", 0) or 0),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "passing_rule_count": int(results["passes_gate"].sum()) if len(results) else 0,
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "pass_confirmation_filter_gate" if passed else "research_only_no_confirmation_filter_alpha",
        "final_verdict": "V5.14 确认期过滤未通过完整强行业门槛，不能声称目标完成。" if not passed else "V5.14 确认期过滤通过强行业门槛，但仍需前推验证。",
    }


def write_outputs(summary: dict[str, Any], panel: pd.DataFrame, events: pd.DataFrame, results: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, results), encoding="utf-8")
    panel.to_csv(DEBUG / "confirmation_filter_opportunity_set.csv", index=False, encoding="utf-8-sig")
    events.to_csv(DEBUG / "confirmation_filter_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "confirmation_filter_results.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.14 确认期过滤审计",
        "",
        summary["final_verdict"],
        "",
        f"- 测试规则数：{summary['tested_rule_count']}",
        f"- 最优过滤：`{summary['best_filter']}`",
        f"- 最优特征：`{summary['best_feature']}`",
        f"- 最优 TopN：{summary['best_top_n']}",
        f"- 最优确认等待：{summary['best_confirm_days']} 个交易日",
        f"- 最优事件数：{summary['best_event_count']}",
        f"- 最优平均相对收益：{pct(summary['best_mean_relative_return'])}",
        f"- 最优 Top20% 命中率：{pct(summary['best_top_quintile_hit_rate'])}",
        f"- 通过规则数：{summary['passing_rule_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 结果",
        "",
        results.to_markdown(index=False) if len(results) else "无数据",
        "",
        "边界：V5.14 只测试确认日已经可见的固定过滤：不过度早期下跌、低分化确认；不使用确认日之后收益构造过滤。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    df = pd.DataFrame({
        "signal_date": ["2020-01-01", "2020-01-01"],
        "entry_date": ["2020-01-02", "2020-01-02"],
        "confirm_date": ["2020-01-09", "2020-01-09"],
        "exit_date": ["2020-02-01", "2020-02-01"],
        "confirm_days": [5, 5],
        "early_benchmark_return": [-0.04, -0.04],
        "early_relative_return": [0.01, -0.01],
    })
    out = attach_event_filters(df)
    assert bool(out["filter_no_severe_early_selloff"].all())
    assert bool(out["filter_calm_confirmation"].all())
    print("self_check=pass")


if __name__ == "__main__":
    main()
