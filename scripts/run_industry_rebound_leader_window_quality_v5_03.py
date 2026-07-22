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
BASE_EVENTS = ROOT / "outputs" / "industry_rebound_leader_beta_guardrail_v5_02" / "debug" / "beta_guardrail_event_panel.csv"
WINDOWS = ROOT / "outputs" / "industry_rebound_leader_expanded_window_v4_97" / "debug" / "expanded_window_trades.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_window_quality_v5_03"
DEBUG = OUT / "debug"
TOP_N = 5


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.03 window quality gate for beta rebound leaders.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    events = load_events()
    labeled = add_quality_labels(events)
    panel = evaluate_quality_rules(labeled)
    results = summarize(panel)
    gate = gate_audit(results)
    summary = build_summary(results, gate)
    write_outputs(summary, labeled, panel, results, gate)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_rule={summary['best_rule']}")


def load_events() -> pd.DataFrame:
    events = pd.read_csv(BASE_EVENTS, encoding="utf-8-sig")
    events = events[events["rule"].eq("baseline_beta_top5")].copy()
    windows = pd.read_csv(WINDOWS, encoding="utf-8-sig")
    cols = [
        "signal_date", "entry_date", "exit_date", "liquidity_repair_5d",
        "industry_positive_10d_ratio", "industry_downside_concentration_20d",
        "negative_breadth_60d", "market_stress_score", "market_volatility_20d_vs_60d",
    ]
    return events.merge(windows[cols], on=["signal_date", "entry_date", "exit_date"], how="left")


def add_quality_labels(events: pd.DataFrame) -> pd.DataFrame:
    out = events.copy()
    out["q_low_liquidity_repair"] = out["liquidity_repair_5d"].lt(0.08)
    out["q_low_positive_10d"] = out["industry_positive_10d_ratio"].lt(0.30)
    out["q_high_downside_concentration"] = out["industry_downside_concentration_20d"].ge(0.50)
    out["q_high_breadth_pressure"] = out["negative_breadth_60d"].ge(0.50)
    out["q_high_stress"] = out["market_stress_score"].ge(0.60)
    out["q_high_vol_ratio"] = out["market_volatility_20d_vs_60d"].ge(1.20)
    labels = [
        "q_low_liquidity_repair", "q_low_positive_10d", "q_high_downside_concentration",
        "q_high_breadth_pressure", "q_high_stress", "q_high_vol_ratio",
    ]
    out["window_quality_score"] = out[labels].sum(axis=1)
    return out


def evaluate_quality_rules(events: pd.DataFrame) -> pd.DataFrame:
    rules = {
        "baseline_beta_top5": events.index == events.index,
        "low_liquidity_repair": events["q_low_liquidity_repair"],
        "low_positive_10d": events["q_low_positive_10d"],
        "high_downside_concentration": events["q_high_downside_concentration"],
        "high_breadth_pressure": events["q_high_breadth_pressure"],
        "quality_score_ge1": events["window_quality_score"].ge(1),
        "quality_score_ge2": events["window_quality_score"].ge(2),
        "quality_score_ge3": events["window_quality_score"].ge(3),
    }
    rows = []
    for rule, mask in rules.items():
        sample = events[mask].copy()
        sample["quality_rule"] = rule
        rows.append(sample)
    return pd.concat(rows, ignore_index=True)


def summarize(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rule, group in panel.groupby("quality_rule"):
        oos = group[group["year"].ge(2022)]
        yearly = group.groupby("year")["relative_return"].mean()
        row = {
            "quality_rule": rule,
            "top_n": TOP_N,
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
        robust = v480.robustness_metrics(group, TOP_N) if row["point_gate_passed"] else {}
        row.update(robust)
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        ["passes_gate", "point_gate_passed", "mean_relative_return"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


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


def gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0].to_dict()
    failed = set(str(best.get("failed_metrics", "")).split(";"))
    metrics = [
        "event_count", "year_count", "mean_relative_return", "median_relative_return",
        "relative_win_rate", "top_quintile_hit_rate", "oos_event_count",
        "oos_mean_relative_return", "oos_relative_win_rate", "robust_gate_passed",
        "leave_one_year_gate_passed", "bootstrap_top_quintile_hit_p05",
        "bootstrap_positive_year_p05", "leave_one_year_min_hit_rate",
        "leave_one_year_min_mean_relative_return",
    ]
    return pd.DataFrame([
        {"quality_rule": best.get("quality_rule", ""), "metric": metric, "current": best.get(metric, ""), "status": "fail" if metric in failed else "pass"}
        for metric in metrics
    ])


def build_summary(results: pd.DataFrame, gate: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_gate", False))
    return {
        "version": "5.03.0",
        "policy_id": "industry_rebound_leader_window_quality_v5_03",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "best_rule": best.get("quality_rule", ""),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_oos_mean_relative_return": float(best.get("oos_mean_relative_return", 0.0) or 0.0),
        "best_bootstrap_top_quintile_hit_p05": num(best.get("bootstrap_top_quintile_hit_p05")),
        "best_bootstrap_positive_year_p05": num(best.get("bootstrap_positive_year_p05")),
        "passing_rule_count": int(results["passes_gate"].sum()) if len(results) else 0,
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_window_quality_leader_gate" if passed else "research_only_no_window_quality_alpha",
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "V5.03 的窗口质量过滤未通过完整稳健门槛，不能声称已经找到稳定强反弹行业。" if not passed else "V5.03 的窗口质量过滤通过完整稳健门槛，但仍需前推观察。",
    }


def write_outputs(summary: dict[str, Any], labeled: pd.DataFrame, panel: pd.DataFrame, results: pd.DataFrame, gate: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, results, gate), encoding="utf-8")
    labeled.to_csv(DEBUG / "window_quality_labeled_events.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(DEBUG / "window_quality_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "window_quality_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.03 窗口质量强行业回测",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 最优窗口质量规则：`{summary['best_rule']}`",
        f"- 最优事件数：{summary['best_event_count']}",
        f"- 最优平均相对收益：{pct(summary['best_mean_relative_return'])}",
        f"- 最优 Top20% 命中率：{pct(summary['best_top_quintile_hit_rate'])}",
        f"- 最优样本外相对收益：{pct(summary['best_oos_mean_relative_return'])}",
        f"- bootstrap Top20% 命中率 5% 下界：{pct(summary['best_bootstrap_top_quintile_hit_p05'])}",
        f"- bootstrap 正收益年份 5% 下界：{pct(summary['best_bootstrap_positive_year_p05'])}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 最优规则门槛",
        "",
        gate.to_markdown(index=False) if len(gate) else "无数据",
        "",
        "## 规则结果",
        "",
        results.to_markdown(index=False) if len(results) else "无数据",
        "",
        "## 研究边界",
        "",
        "V5.03 只用信号日前可见的窗口状态做事件过滤，不重新调行业因子。未通过完整门槛前不生成交易指令。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def num(value: Any) -> float:
    return float(value) if pd.notna(value) else 0.0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    sample = pd.DataFrame({
        "liquidity_repair_5d": [0.07], "industry_positive_10d_ratio": [0.20],
        "industry_downside_concentration_20d": [0.60], "negative_breadth_60d": [0.40],
        "market_stress_score": [0.50], "market_volatility_20d_vs_60d": [1.30],
    })
    scored = add_quality_labels(sample)
    assert int(scored["window_quality_score"].iloc[0]) == 4
    print("self_check=pass")


if __name__ == "__main__":
    main()
