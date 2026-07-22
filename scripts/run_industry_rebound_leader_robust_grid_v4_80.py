#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_oos_factor_v4_74 as v474


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_robust_grid_v4_80"
DEBUG = OUT / "debug"

TOP_NS = [5, 8, 10, 12, 15, 20, 25, 30]
BASE_FEATURES = [
    "oversold_score",
    "oversold_liquidity_score",
    "oversold_turn_score",
    "liquidity_score",
    "oversold_quality_balance",
    "oversold_liquidity_turn",
    "oversold_value_liquidity",
    "liquidity_confirmed_oversold",
]
VARIANT_NAMES = [
    "deep_or_high_vol",
    "any_passed_state_bucket",
    "all_rebound_windows",
    "deep_negative_breadth_only",
    "mid_or_high_vol",
]
GATE_TEXT = (
    "point gate: events>=30, years>=5, mean/median relative>0, win>=55%, hit>=30%, "
    "positive_year>=60%, OOS mean>0, OOS win>=50%; "
    "robust gate: Wilson hit lower>20%, bootstrap mean p05>0, bootstrap hit p05>=30%, "
    "bootstrap positive-year p05>=60%; "
    "leave-one-year gate: min hit>=25%, min mean relative>0"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.80 robust grid audit for strong rebound industry selection.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    frame = add_candidate_features(v474.attach_state(opportunity, trades))
    event_panel = build_event_panel(frame)
    grid = summarize_grid(event_panel)
    robust = robustness_for_point_passes(event_panel, grid)
    merged = merge_results(grid, robust)
    best = select_best(merged)
    gate = gate_audit(best)
    latest = latest_rule_candidates(merged)
    summary = build_summary(merged, best, gate)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    latest.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, latest, gate), encoding="utf-8")
    event_panel.to_csv(DEBUG / "robust_grid_event_panel.csv", index=False, encoding="utf-8-sig")
    merged.to_csv(DEBUG / "robust_grid_results.csv", index=False, encoding="utf-8-sig")
    robust.to_csv(DEBUG / "robustness_detail.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def add_candidate_features(frame: pd.DataFrame) -> pd.DataFrame:
    pieces = []
    rank_inputs = [
        "valuation_score",
        "oversold_score",
        "turn_score",
        "liquidity_score",
        "oversold_liquidity_score",
        "oversold_turn_score",
        "value_oversold_turn_score",
    ]
    for _, event in frame.groupby(["signal_date", "entry_date", "exit_date"], sort=False):
        event = event.copy()
        for column in rank_inputs:
            event[f"{column}_rank"] = pd.to_numeric(event[column], errors="coerce").rank(pct=True, ascending=True)
        event["oversold_quality_balance"] = (
            0.50 * event["oversold_score_rank"]
            + 0.25 * event["turn_score_rank"]
            + 0.25 * event["liquidity_score_rank"]
        )
        event["oversold_liquidity_turn"] = (
            0.60 * event["oversold_score_rank"]
            + 0.25 * event["liquidity_score_rank"]
            + 0.15 * event["turn_score_rank"]
        )
        event["oversold_value_liquidity"] = (
            0.55 * event["oversold_score_rank"]
            + 0.25 * event["valuation_score_rank"]
            + 0.20 * event["liquidity_score_rank"]
        )
        event["liquidity_confirmed_oversold"] = event["oversold_score_rank"].where(
            event["liquidity_score_rank"].ge(0.35),
            event["oversold_score_rank"] - 0.25,
        )
        pieces.append(event)
    return pd.concat(pieces, ignore_index=True)


def build_event_panel(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for variant in VARIANT_NAMES:
        source = frame[variant_mask(frame, variant)].copy()
        for feature in BASE_FEATURES:
            for top_n in TOP_NS:
                rows.extend(v474.evaluate_factor(source, variant, feature, top_n))
    return pd.DataFrame(rows)


def variant_mask(frame: pd.DataFrame, variant: str) -> pd.Series:
    if variant == "deep_or_high_vol":
        return frame["deep_negative_breadth"].fillna(False) | frame["high_volatility_protection"].fillna(False)
    if variant == "any_passed_state_bucket":
        return frame["any_passed_state_bucket"].fillna(False)
    if variant == "deep_negative_breadth_only":
        return frame["deep_negative_breadth"].fillna(False)
    if variant == "mid_or_high_vol":
        return frame["mid_high_stress"].fillna(False) | frame["high_volatility_protection"].fillna(False)
    return pd.Series(True, index=frame.index)


def summarize_grid(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (variant, feature, top_n), group in panel.groupby(["state_gate_variant", "factor", "top_n"]):
        yearly = group.groupby("year")["relative_return"].mean()
        oos = group[group["year"] >= 2022]
        row = {
            "state_gate_variant": variant,
            "feature": feature,
            "top_n": int(top_n),
            "event_count": int(len(group)),
            "year_count": int(group["year"].nunique()),
            "mean_relative_return": float(group["relative_return"].mean()),
            "median_relative_return": float(group["relative_return"].median()),
            "relative_win_rate": float(group["relative_win"].mean()),
            "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
        }
        row["point_gate_passed"] = point_gate_passed(row)
        row["point_failed_metrics"] = ";".join(point_failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows)


def robustness_for_point_passes(panel: pd.DataFrame, grid: pd.DataFrame) -> pd.DataFrame:
    rows = []
    point_passed = grid[grid["point_gate_passed"].eq(True)]
    for _, rule in point_passed.iterrows():
        mask = (
            panel["state_gate_variant"].eq(rule["state_gate_variant"])
            & panel["factor"].eq(rule["feature"])
            & panel["top_n"].eq(rule["top_n"])
        )
        events = panel[mask].copy()
        robust = robustness_metrics(events, int(rule["top_n"]))
        robust.update({
            "state_gate_variant": rule["state_gate_variant"],
            "feature": rule["feature"],
            "top_n": int(rule["top_n"]),
        })
        rows.append(robust)
    return pd.DataFrame(rows)


def robustness_metrics(events: pd.DataFrame, top_n: int) -> dict[str, object]:
    hit_successes = int(round(float(events["top_quintile_hit_rate"].sum()) * top_n))
    hit_trials = int(len(events) * top_n)
    bootstrap = bootstrap_event_metrics(events)
    loo = leave_one_year_metrics(events)
    out = {
        "top_quintile_wilson_lower_bound": wilson_lower(hit_successes, hit_trials),
        "bootstrap_mean_relative_p05": float(bootstrap["mean_relative_return"].quantile(0.05)),
        "bootstrap_top_quintile_hit_p05": float(bootstrap["top_quintile_hit_rate"].quantile(0.05)),
        "bootstrap_positive_year_p05": float(bootstrap["positive_year_rate"].quantile(0.05)),
        "leave_one_year_min_hit_rate": float(loo["top_quintile_hit_rate"].min()) if len(loo) else 0.0,
        "leave_one_year_min_mean_relative_return": float(loo["mean_relative_return"].min()) if len(loo) else 0.0,
    }
    out["robust_gate_passed"] = robust_gate_passed(out)
    out["leave_one_year_gate_passed"] = leave_one_year_gate_passed(out)
    out["robust_failed_metrics"] = ";".join(robust_failed_metrics(out))
    out["leave_one_year_failed_metrics"] = ";".join(leave_one_year_failed_metrics(out))
    return out


def bootstrap_event_metrics(frame: pd.DataFrame, iterations: int = 1000) -> pd.DataFrame:
    rng = random.Random(20260620)
    rows = []
    for _ in range(iterations):
        sample = frame.sample(n=len(frame), replace=True, random_state=rng.randrange(1_000_000_000))
        yearly = sample.groupby("year")["relative_return"].mean()
        rows.append({
            "mean_relative_return": float(sample["relative_return"].mean()),
            "top_quintile_hit_rate": float(sample["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
        })
    return pd.DataFrame(rows)


def leave_one_year_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year in sorted(frame["year"].unique()):
        sample = frame[frame["year"].ne(year)]
        if sample.empty:
            continue
        yearly = sample.groupby("year")["relative_return"].mean()
        rows.append({
            "left_out_year": int(year),
            "mean_relative_return": float(sample["relative_return"].mean()),
            "top_quintile_hit_rate": float(sample["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
        })
    return pd.DataFrame(rows)


def wilson_lower(successes: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0:
        return 0.0
    phat = successes / trials
    denominator = 1 + z * z / trials
    center = (phat + z * z / (2 * trials)) / denominator
    half = z * math.sqrt((phat * (1 - phat) + z * z / (4 * trials)) / trials) / denominator
    return float(center - half)


def merge_results(grid: pd.DataFrame, robust: pd.DataFrame) -> pd.DataFrame:
    if robust.empty:
        merged = grid.copy()
    else:
        merged = grid.merge(robust, on=["state_gate_variant", "feature", "top_n"], how="left")
    for column in [
        "top_quintile_wilson_lower_bound",
        "bootstrap_mean_relative_p05",
        "bootstrap_top_quintile_hit_p05",
        "bootstrap_positive_year_p05",
        "leave_one_year_min_hit_rate",
        "leave_one_year_min_mean_relative_return",
    ]:
        if column not in merged.columns:
            merged[column] = 0.0
        merged[column] = merged[column].fillna(0.0)
    for column in ["robust_gate_passed", "leave_one_year_gate_passed"]:
        if column not in merged.columns:
            merged[column] = False
        merged[column] = merged[column].fillna(False).astype(bool)
    for column in ["robust_failed_metrics", "leave_one_year_failed_metrics"]:
        if column not in merged.columns:
            merged[column] = ""
        merged[column] = merged[column].fillna("")
    merged["passes_v4_80_gate"] = (
        merged["point_gate_passed"]
        & merged["robust_gate_passed"]
        & merged["leave_one_year_gate_passed"]
    )
    merged["failed_gate_groups"] = merged.apply(failed_gate_groups, axis=1)
    return merged.sort_values(
        [
            "passes_v4_80_gate",
            "robust_gate_passed",
            "point_gate_passed",
            "bootstrap_top_quintile_hit_p05",
            "top_quintile_hit_rate",
            "mean_relative_return",
        ],
        ascending=[False, False, False, False, False, False],
    ).reset_index(drop=True)


def select_best(results: pd.DataFrame) -> pd.Series:
    return results.iloc[0] if len(results) else pd.Series(dtype=object)


def latest_rule_candidates(results: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "state_gate_variant",
        "feature",
        "top_n",
        "passes_v4_80_gate",
        "point_gate_passed",
        "robust_gate_passed",
        "leave_one_year_gate_passed",
        "event_count",
        "year_count",
        "mean_relative_return",
        "top_quintile_hit_rate",
        "positive_year_rate",
        "bootstrap_top_quintile_hit_p05",
        "bootstrap_positive_year_p05",
        "leave_one_year_min_hit_rate",
        "leave_one_year_min_mean_relative_return",
        "failed_gate_groups",
    ]
    return results[[column for column in columns if column in results.columns]].head(20).copy()


def gate_audit(best: pd.Series) -> pd.DataFrame:
    if best.empty:
        return pd.DataFrame()
    requirements = [
        ("point_gate_passed", True, "=="),
        ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="),
        ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="),
        ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0.0, ">"),
    ]
    return pd.DataFrame([
        {
            "state_gate_variant": best.get("state_gate_variant", ""),
            "feature": best.get("feature", ""),
            "top_n": best.get("top_n", ""),
            "metric": metric,
            "current": best.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "pass" if compare(best.get(metric, ""), required, op) else "fail",
        }
        for metric, required, op in requirements
    ])


def build_summary(results: pd.DataFrame, best: pd.Series, gate: pd.DataFrame) -> dict[str, object]:
    passing = results[results["passes_v4_80_gate"].eq(True)] if len(results) else pd.DataFrame()
    point = results[results["point_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    robust = results[results["robust_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    passed = bool(len(passing))
    return {
        "version": "4.80.0",
        "policy_id": "industry_rebound_leader_robust_grid_v4_80",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_rule_count": int(len(results)),
        "point_gate_pass_count": int(len(point)),
        "robust_gate_pass_count": int(len(robust)),
        "passing_rule_count": int(len(passing)),
        "best_state_gate_variant": best.get("state_gate_variant", ""),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_bootstrap_top_quintile_hit_p05": float(best.get("bootstrap_top_quintile_hit_p05", 0.0) or 0.0),
        "best_bootstrap_positive_year_p05": float(best.get("bootstrap_positive_year_p05", 0.0) or 0.0),
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_robust_stronger_industry_gate" if passed else "research_only_no_robust_stronger_industry_rule",
        "production_ready": False,
        "auto_execution_allowed": False,
        "evaluation_gate": GATE_TEXT,
        "final_verdict": (
            "V4.80 找到通过点估计、bootstrap 和留一年验证的强反弹行业规则；仍需实盘前推。"
            if passed else
            "V4.80 未找到同时通过点估计、bootstrap 和留一年验证的强反弹行业规则。"
        ),
    }


def point_failed_metrics(row: dict[str, object] | pd.Series) -> list[str]:
    checks = [
        ("event_count", 30, ">="),
        ("year_count", 5, ">="),
        ("mean_relative_return", 0, ">"),
        ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="),
    ]
    return [metric for metric, required, op in checks if not compare(row.get(metric, 0), required, op)]


def robust_failed_metrics(row: dict[str, object] | pd.Series) -> list[str]:
    checks = [
        ("top_quintile_wilson_lower_bound", 0.20, ">"),
        ("bootstrap_mean_relative_p05", 0, ">"),
        ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="),
    ]
    return [metric for metric, required, op in checks if not compare(row.get(metric, 0), required, op)]


def leave_one_year_failed_metrics(row: dict[str, object] | pd.Series) -> list[str]:
    checks = [
        ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0, ">"),
    ]
    return [metric for metric, required, op in checks if not compare(row.get(metric, 0), required, op)]


def point_gate_passed(row: dict[str, object] | pd.Series) -> bool:
    return not point_failed_metrics(row)


def robust_gate_passed(row: dict[str, object] | pd.Series) -> bool:
    return not robust_failed_metrics(row)


def leave_one_year_gate_passed(row: dict[str, object] | pd.Series) -> bool:
    return not leave_one_year_failed_metrics(row)


def failed_gate_groups(row: pd.Series) -> str:
    failed = []
    if not bool(row.get("point_gate_passed", False)):
        failed.append("point")
    if not bool(row.get("robust_gate_passed", False)):
        failed.append("robust")
    if not bool(row.get("leave_one_year_gate_passed", False)):
        failed.append("leave_one_year")
    return ";".join(failed)


def compare(value: object, required: object, op: str) -> bool:
    if op == "==":
        return value == required
    current = float(value or 0)
    target = float(required)
    return current >= target if op == ">=" else current > target


def render_report(summary: dict[str, object], latest: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.80 稳健强反弹行业规则网格审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 只测试少量事前可解释状态门控、现有价格/估值/企稳/流动性特征和 TopN。",
        "- 评价分三层：点估计门槛、bootstrap 5% 下界、留一年验证。",
        "- 只有三层全部通过，才允许称为找到稳定强反弹行业规则。",
        "- 本版本仍不使用 ETF、不使用个股、不生成交易指令。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 最接近通过的规则",
        "",
        table(latest),
        "",
        "## 最优规则门槛审计",
        "",
        table(gate),
        "",
        "## 研究边界",
        "",
        "如果 V4.80 没有规则通过，说明在当前可用的价格/估值/流动性特征里，还不能证明能稳定选出反弹更猛的行业；下一步应优先补充新的 PIT 信息源或等待前推样本，而不是继续细调 TopN。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    row = {
        "event_count": 30,
        "year_count": 5,
        "mean_relative_return": 0.01,
        "median_relative_return": 0.01,
        "relative_win_rate": 0.56,
        "top_quintile_hit_rate": 0.31,
        "positive_year_rate": 0.60,
        "oos_event_count": 8,
        "oos_mean_relative_return": 0.01,
        "oos_relative_win_rate": 0.50,
        "top_quintile_wilson_lower_bound": 0.21,
        "bootstrap_mean_relative_p05": 0.001,
        "bootstrap_top_quintile_hit_p05": 0.30,
        "bootstrap_positive_year_p05": 0.60,
        "leave_one_year_min_hit_rate": 0.25,
        "leave_one_year_min_mean_relative_return": 0.001,
    }
    assert point_gate_passed(row)
    assert robust_gate_passed(row)
    assert leave_one_year_gate_passed(row)
    row["bootstrap_top_quintile_hit_p05"] = 0.29
    assert "bootstrap_top_quintile_hit_p05" in robust_failed_metrics(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
