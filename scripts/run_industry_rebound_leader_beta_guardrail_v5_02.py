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
OPPORTUNITY = ROOT / "outputs" / "industry_rebound_leader_market_sensitivity_v4_99" / "debug" / "market_sensitivity_opportunity_set.csv"
WINDOWS = ROOT / "outputs" / "industry_rebound_leader_expanded_window_v4_97" / "debug" / "expanded_window_trades.csv"
PARENT_PANEL = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "raw_industry_panel.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_beta_guardrail_v5_02"
DEBUG = OUT / "debug"

FEATURE = "beta_120_rank"
TOP_N = 5
BAD_PARENT = "医药生物"

RULES = [
    {"rule": "baseline_beta_top5", "low_liquidity_repair_only": False, "exclude_bad_parent": False},
    {"rule": "low_liquidity_repair_beta_top5", "low_liquidity_repair_only": True, "exclude_bad_parent": False},
    {"rule": "exclude_medical_beta_top5", "low_liquidity_repair_only": False, "exclude_bad_parent": True},
    {"rule": "low_liquidity_repair_exclude_medical_beta_top5", "low_liquidity_repair_only": True, "exclude_bad_parent": True},
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.02 beta guardrail backtest.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = load_opportunity()
    event_panel = evaluate_rules(opportunity)
    results = summarize_rules(event_panel)
    gate = gate_audit(results)
    summary = build_summary(results, gate)
    write_outputs(summary, event_panel, results, gate)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_rule={summary['best_rule']}")


def load_opportunity() -> pd.DataFrame:
    frame = pd.read_csv(OPPORTUNITY, encoding="utf-8-sig", dtype={"industry_code": str})
    windows = pd.read_csv(WINDOWS, encoding="utf-8-sig")
    parent = pd.read_csv(PARENT_PANEL, encoding="utf-8-sig", dtype={"industry_code": str})
    parent = parent[["industry_code", "parent_industry"]].drop_duplicates()
    parent["industry_code"] = parent["industry_code"].str.zfill(6)
    frame["industry_code"] = frame["industry_code"].astype(str).str.zfill(6)
    frame["year"] = pd.to_datetime(frame["signal_date"]).dt.year
    frame = frame.merge(parent, on="industry_code", how="left")
    return frame.merge(windows, on=["signal_date", "entry_date", "exit_date", "year"], how="left", suffixes=("", "_window"))


def evaluate_rules(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    keys = ["signal_date", "entry_date", "exit_date"]
    for rule in RULES:
        for (signal_date, entry_date, exit_date), event in frame.groupby(keys):
            event = event.dropna(subset=[FEATURE, "future_return"]).copy()
            if event.empty:
                continue
            if rule["low_liquidity_repair_only"] and float(event["liquidity_repair_5d"].iloc[0]) >= 0.08:
                continue
            benchmark = float(event["future_return"].mean())
            top_cut = event["future_return"].quantile(0.8)
            tradable = event.copy()
            if rule["exclude_bad_parent"]:
                tradable = tradable[tradable["parent_industry"].ne(BAD_PARENT)]
            selected = tradable.sort_values(FEATURE, ascending=False).head(TOP_N)
            if len(selected) < TOP_N:
                continue
            relative = float(selected["future_return"].mean()) - 0.001 - benchmark
            rows.append({
                "rule": rule["rule"],
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "year": int(pd.to_datetime(signal_date).year),
                "event_counted": True,
                "benchmark_return": benchmark,
                "selected_net_return": float(selected["future_return"].mean()) - 0.001,
                "relative_return": relative,
                "relative_win": relative > 0,
                "top_quintile_hit_rate": float((selected["future_return"] >= top_cut).mean()),
                "selected_parents": "|".join(selected["parent_industry"].fillna("未知").astype(str)),
                "selected_industries": "|".join(selected["industry_name"].astype(str)),
            })
    return pd.DataFrame(rows)


def summarize_rules(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rule, group in panel.groupby("rule"):
        oos = group[group["year"].ge(2022)]
        yearly = group.groupby("year")["relative_return"].mean()
        row = {
            "rule": rule,
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
        ["passes_gate", "robust_gate_passed", "point_gate_passed", "mean_relative_return"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("event_count", 30, ">="),
        ("year_count", 8, ">="),
        ("mean_relative_return", 0, ">"),
        ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="),
        ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="),
        ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="),
        ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0, ">"),
    ]
    out = []
    for metric, required, op in checks:
        if op == "==":
            ok = row.get(metric) == required
        else:
            value = float(row.get(metric, 0) or 0)
            ok = value >= required if op == ">=" else value > required
        if not ok:
            out.append(metric)
    return out


def passes_point_gate(row: dict[str, Any]) -> bool:
    point_metrics = {
        "event_count", "year_count", "mean_relative_return", "median_relative_return",
        "relative_win_rate", "top_quintile_hit_rate", "oos_event_count",
        "oos_mean_relative_return", "oos_relative_win_rate",
    }
    return not (point_metrics & set(failed_metrics(row)))


def gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0].to_dict()
    failed = set(str(best.get("failed_metrics", "")).split(";"))
    return pd.DataFrame([
        {"rule": best.get("rule", ""), "metric": metric, "current": best.get(metric, ""), "status": "fail" if metric in failed else "pass"}
        for metric in [
            "event_count", "year_count", "mean_relative_return", "median_relative_return",
            "relative_win_rate", "top_quintile_hit_rate", "oos_event_count",
            "oos_mean_relative_return", "oos_relative_win_rate", "robust_gate_passed",
            "leave_one_year_gate_passed", "bootstrap_top_quintile_hit_p05",
            "bootstrap_positive_year_p05", "leave_one_year_min_hit_rate",
            "leave_one_year_min_mean_relative_return",
        ]
    ])


def build_summary(results: pd.DataFrame, gate: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_gate", False))
    return {
        "version": "5.02.0",
        "policy_id": "industry_rebound_leader_beta_guardrail_v5_02",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_feature": FEATURE,
        "tested_rule_count": len(RULES),
        "best_rule": best.get("rule", ""),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_oos_mean_relative_return": float(best.get("oos_mean_relative_return", 0.0) or 0.0),
        "best_bootstrap_top_quintile_hit_p05": num(best.get("bootstrap_top_quintile_hit_p05")),
        "best_bootstrap_positive_year_p05": num(best.get("bootstrap_positive_year_p05")),
        "passing_rule_count": int(results["passes_gate"].sum()) if len(results) else 0,
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_beta_guardrail_leader_gate" if passed else "research_only_no_beta_guardrail_alpha",
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "V5.02 的 beta 守门过滤未通过完整稳健门槛，不能声称已经找到稳定强反弹行业。" if not passed else "V5.02 的 beta 守门过滤通过完整稳健门槛，但仍需前推观察后才能进入实盘辅助。",
    }


def write_outputs(summary: dict[str, Any], panel: pd.DataFrame, results: pd.DataFrame, gate: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, results, gate), encoding="utf-8")
    panel.to_csv(DEBUG / "beta_guardrail_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "beta_guardrail_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.02 Beta 守门强行业回测",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 测试规则数：{summary['tested_rule_count']}",
        f"- 最优规则：`{summary['best_rule']}`",
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
        "V5.02 只测试 V5.01 指出的两个失败源：高流动性修复状态和医药生物父行业暴露。未通过完整门槛前不生成交易指令。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def num(value: Any) -> float:
    return float(value) if pd.notna(value) else 0.0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    assert "top_quintile_hit_rate" in failed_metrics({"event_count": 30, "year_count": 8})
    ok = {
        "event_count": 30, "year_count": 8, "mean_relative_return": 0.01,
        "median_relative_return": 0.01, "relative_win_rate": 0.56,
        "top_quintile_hit_rate": 0.31, "oos_event_count": 8,
        "oos_mean_relative_return": 0.01, "oos_relative_win_rate": 0.50,
        "robust_gate_passed": True, "leave_one_year_gate_passed": True,
        "bootstrap_top_quintile_hit_p05": 0.31, "bootstrap_positive_year_p05": 0.60,
        "leave_one_year_min_hit_rate": 0.25, "leave_one_year_min_mean_relative_return": 0.01,
    }
    assert passes_point_gate(ok)
    print("self_check=pass")


if __name__ == "__main__":
    main()
