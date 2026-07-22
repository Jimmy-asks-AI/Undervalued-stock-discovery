#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from append_v5_05_rebound_leader_forward_sample import LEDGER, read_rows


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = ROOT / "outputs" / "industry_rebound_leader_window_quality_v5_03" / "debug" / "window_quality_event_panel.csv"
FROZEN = ROOT / "outputs" / "audit" / "rebound_leader_evidence_freeze_v5_04" / "top_candidates.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_promotion_evaluator_v5_07"
DEBUG = OUT / "debug"
TOP_N = 5
MIN_FORWARD_TIMING_EVENTS = 20


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.07 promotion evaluator for frozen rebound-leader rules.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    frozen = pd.read_csv(FROZEN, encoding="utf-8-sig")
    historical = load_historical(frozen)
    forward = load_forward()
    combined = pd.concat([historical, forward], ignore_index=True)
    results = evaluate(combined)
    gate = gate_audit(results)
    summary = build_summary(results, forward)
    write_outputs(summary, results, gate, historical, forward, combined)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def load_historical(frozen: pd.DataFrame) -> pd.DataFrame:
    rules = set(frozen["frozen_rule"].astype(str))
    data = pd.read_csv(HISTORICAL, encoding="utf-8-sig")
    data = data[data["quality_rule"].isin(rules)].copy()
    data["sample_source"] = "historical_frozen"
    data["frozen_rule"] = data["quality_rule"]
    return data[["frozen_rule", "sample_source", "signal_date", "entry_date", "exit_date", "year", "relative_return", "top_quintile_hit_rate"]]


def load_forward() -> pd.DataFrame:
    rows = [row for row in read_rows(LEDGER) if row.get("settlement_status") == "settled"]
    if not rows:
        return pd.DataFrame(columns=["frozen_rule", "sample_source", "signal_date", "entry_date", "exit_date", "year", "benchmark_return", "relative_return", "top_quintile_hit_rate"])
    frame = pd.DataFrame(rows)
    frame["sample_source"] = "forward_settled"
    frame["year"] = pd.to_datetime(frame["signal_date"]).dt.year
    frame["benchmark_return"] = pd.to_numeric(frame["benchmark_return"], errors="coerce")
    frame["relative_return"] = pd.to_numeric(frame["relative_return"], errors="coerce")
    frame["top_quintile_hit_rate"] = pd.to_numeric(frame["top_quintile_hit_rate"], errors="coerce")
    return frame[["frozen_rule", "sample_source", "signal_date", "entry_date", "exit_date", "year", "benchmark_return", "relative_return", "top_quintile_hit_rate"]]


def evaluate_forward_timing(forward: pd.DataFrame) -> dict[str, Any]:
    events = forward.dropna(subset=["benchmark_return"]).drop_duplicates(["signal_date", "entry_date", "exit_date"])
    count = len(events)
    mean = float(events["benchmark_return"].mean()) if count else 0.0
    median = float(events["benchmark_return"].median()) if count else 0.0
    win_rate = float(events["benchmark_return"].gt(0).mean()) if count else 0.0
    return {
        "forward_timing_event_count": count,
        "forward_timing_mean_return": mean,
        "forward_timing_median_return": median,
        "forward_timing_win_rate": win_rate,
        "forward_timing_gate_passed": count >= MIN_FORWARD_TIMING_EVENTS and mean > 0 and median > 0 and win_rate >= 0.55,
    }


def evaluate(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rule, group in events.dropna(subset=["relative_return", "top_quintile_hit_rate"]).groupby("frozen_rule"):
        yearly = group.groupby("year")["relative_return"].mean()
        forward = group[group["sample_source"].eq("forward_settled")]
        row = {
            "frozen_rule": rule,
            "event_count": int(len(group)),
            "historical_event_count": int(group["sample_source"].eq("historical_frozen").sum()),
            "forward_event_count": int(len(forward)),
            "year_count": int(group["year"].nunique()),
            "mean_relative_return": float(group["relative_return"].mean()),
            "median_relative_return": float(group["relative_return"].median()),
            "relative_win_rate": float(group["relative_return"].gt(0).mean()),
            "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "forward_mean_relative_return": float(forward["relative_return"].mean()) if len(forward) else 0.0,
            "forward_positive_relative_rate": float(forward["relative_return"].gt(0).mean()) if len(forward) else 0.0,
            "forward_top_quintile_hit_rate": float(forward["top_quintile_hit_rate"].mean()) if len(forward) else 0.0,
        }
        row["point_gate_passed"] = passes_point_gate(row)
        robust = robustness_metrics(group, TOP_N) if row["point_gate_passed"] else {}
        row.update(robust)
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["passes_gate", "mean_relative_return"], ascending=[False, False])


def robustness_metrics(events: pd.DataFrame, top_n: int) -> dict[str, Any]:
    rng = random.Random(20260620)
    samples = []
    for _ in range(1000):
        sample = events.sample(n=len(events), replace=True, random_state=rng.randrange(1_000_000_000))
        yearly = sample.groupby("year")["relative_return"].mean()
        samples.append({
            "mean_relative_return": float(sample["relative_return"].mean()),
            "top_quintile_hit_rate": float(sample["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
        })
    bootstrap = pd.DataFrame(samples)
    leave_one_year = []
    for year in sorted(events["year"].unique()):
        sample = events[events["year"].ne(year)]
        if not sample.empty:
            leave_one_year.append({"mean_relative_return": float(sample["relative_return"].mean()),
                                   "top_quintile_hit_rate": float(sample["top_quintile_hit_rate"].mean())})
    loo = pd.DataFrame(leave_one_year)
    hit_successes = int(round(float(events["top_quintile_hit_rate"].sum()) * top_n))
    hit_trials = len(events) * top_n
    phat = hit_successes / hit_trials if hit_trials else 0.0
    denominator = 1 + 1.96 ** 2 / hit_trials if hit_trials else 1.0
    wilson = (phat + 1.96 ** 2 / (2 * hit_trials) - 1.96 * math.sqrt((phat * (1 - phat) + 1.96 ** 2 / (4 * hit_trials)) / hit_trials)) / denominator if hit_trials else 0.0
    out = {
        "top_quintile_wilson_lower_bound": wilson,
        "bootstrap_mean_relative_p05": float(bootstrap["mean_relative_return"].quantile(0.05)),
        "bootstrap_top_quintile_hit_p05": float(bootstrap["top_quintile_hit_rate"].quantile(0.05)),
        "bootstrap_positive_year_p05": float(bootstrap["positive_year_rate"].quantile(0.05)),
        "leave_one_year_min_hit_rate": float(loo["top_quintile_hit_rate"].min()) if len(loo) else 0.0,
        "leave_one_year_min_mean_relative_return": float(loo["mean_relative_return"].min()) if len(loo) else 0.0,
    }
    out["robust_gate_passed"] = out["top_quintile_wilson_lower_bound"] > 0.20 and out["bootstrap_mean_relative_p05"] > 0 and out["bootstrap_top_quintile_hit_p05"] >= 0.30 and out["bootstrap_positive_year_p05"] >= 0.60
    out["leave_one_year_gate_passed"] = out["leave_one_year_min_hit_rate"] >= 0.25 and out["leave_one_year_min_mean_relative_return"] > 0
    return out


def failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("event_count", 30, ">="), ("forward_event_count", 12, ">="),
        ("mean_relative_return", 0, ">"), ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="), ("top_quintile_hit_rate", 0.30, ">="),
        ("forward_mean_relative_return", 0, ">"), ("forward_positive_relative_rate", 0.55, ">="),
        ("forward_top_quintile_hit_rate", 0.30, ">="), ("robust_gate_passed", True, "=="),
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
        "event_count", "forward_event_count", "mean_relative_return", "median_relative_return",
        "relative_win_rate", "top_quintile_hit_rate", "forward_mean_relative_return",
        "forward_positive_relative_rate", "forward_top_quintile_hit_rate",
    }
    return not (point & set(failed_metrics(row)))


def gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0].to_dict()
    failed = set(str(best.get("failed_metrics", "")).split(";"))
    metrics = [
        "event_count", "forward_event_count", "mean_relative_return", "median_relative_return",
        "relative_win_rate", "top_quintile_hit_rate", "forward_mean_relative_return",
        "forward_positive_relative_rate", "forward_top_quintile_hit_rate", "robust_gate_passed",
        "leave_one_year_gate_passed", "bootstrap_top_quintile_hit_p05",
        "bootstrap_positive_year_p05", "leave_one_year_min_hit_rate",
        "leave_one_year_min_mean_relative_return",
    ]
    return pd.DataFrame([
        {"frozen_rule": best.get("frozen_rule", ""), "metric": metric, "current": best.get(metric, ""), "status": "fail" if metric in failed else "pass"}
        for metric in metrics
    ])


def build_summary(results: pd.DataFrame, forward: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_gate", False))
    return {
        "version": "5.07.0",
        "policy_id": "rebound_leader_promotion_evaluator_v5_07",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "forward_settled_event_count": int(len(forward)),
        "best_rule": best.get("frozen_rule", ""),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_forward_event_count": int(best.get("forward_event_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "passing_rule_count": int(results["passes_gate"].sum()) if len(results) else 0,
        **evaluate_forward_timing(forward),
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "pass_rebound_leader_promotion_gate" if passed else "research_only_not_promoted",
        "final_verdict": "V5.07 合并历史冻结样本和已结算前推样本后仍未通过晋级门槛，不能声称目标完成。" if not passed else "V5.07 已通过强反弹行业晋级门槛，但仍不代表自动交易许可。",
    }


def write_outputs(summary: dict[str, Any], results: pd.DataFrame, gate: pd.DataFrame, historical: pd.DataFrame, forward: pd.DataFrame, combined: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, results, gate), encoding="utf-8")
    results.to_csv(DEBUG / "promotion_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "promotion_gate_audit.csv", index=False, encoding="utf-8-sig")
    historical.to_csv(DEBUG / "historical_frozen_events.csv", index=False, encoding="utf-8-sig")
    forward.to_csv(DEBUG / "forward_settled_events.csv", index=False, encoding="utf-8-sig")
    combined.to_csv(DEBUG / "combined_evaluation_events.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.07 强反弹行业晋级评价器",
        "",
        summary["final_verdict"],
        "",
        f"- 最优冻结规则：`{summary['best_rule']}`",
        f"- 最优总事件数：{summary['best_event_count']}",
        f"- 已结算前推事件数：{summary['best_forward_event_count']}",
        f"- 平均相对收益：{pct(summary['best_mean_relative_return'])}",
        f"- Top20% 命中率：{pct(summary['best_top_quintile_hit_rate'])}",
        f"- 独立前推窗口数：{summary['forward_timing_event_count']} / {MIN_FORWARD_TIMING_EVENTS}",
        f"- 前推窗口平均收益：{pct(summary['forward_timing_mean_return'])}",
        f"- 前推窗口胜率：{pct(summary['forward_timing_win_rate'])}",
        f"- 前推择时门禁：`{str(summary['forward_timing_gate_passed']).lower()}`",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 晋级门槛",
        "",
        gate.to_markdown(index=False) if len(gate) else "无数据",
        "",
        "## 规则评价",
        "",
        results.to_markdown(index=False) if len(results) else "无数据",
        "",
        "边界：V5.07 只评价冻结规则，不允许修改阈值、TopN 或 beta 定义。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    row = {
        "event_count": 30, "forward_event_count": 12, "mean_relative_return": 0.01,
        "median_relative_return": 0.01, "relative_win_rate": 0.56,
        "top_quintile_hit_rate": 0.31, "forward_mean_relative_return": 0.01,
        "forward_positive_relative_rate": 0.56, "forward_top_quintile_hit_rate": 0.31,
        "robust_gate_passed": True, "leave_one_year_gate_passed": True,
        "bootstrap_top_quintile_hit_p05": 0.31, "bootstrap_positive_year_p05": 0.60,
        "leave_one_year_min_hit_rate": 0.25, "leave_one_year_min_mean_relative_return": 0.01,
    }
    assert passes_point_gate(row)
    row["forward_event_count"] = 0
    assert "forward_event_count" in failed_metrics(row)
    timing = evaluate_forward_timing(pd.DataFrame([
        {"signal_date": f"2026-{index + 1:02d}-01", "entry_date": f"2026-{index + 1:02d}-03", "exit_date": f"2026-{index + 1:02d}-23", "benchmark_return": 0.01}
        for index in range(MIN_FORWARD_TIMING_EVENTS)
    ]))
    assert timing["forward_timing_gate_passed"]
    robust = robustness_metrics(pd.DataFrame([
        {"year": 2020 + index % 6, "relative_return": 0.02, "top_quintile_hit_rate": 0.40}
        for index in range(30)
    ]), TOP_N)
    assert robust["robust_gate_passed"] and robust["leave_one_year_gate_passed"]
    print("self_check=pass")


if __name__ == "__main__":
    main()
