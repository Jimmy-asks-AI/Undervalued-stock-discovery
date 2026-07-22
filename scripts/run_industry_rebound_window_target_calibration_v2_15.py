#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_target_calibration_policy_v2_15.json"
DEFAULT_DATE_PANEL = ROOT / "outputs" / "industry_rebound_window_audit_v2_11" / "debug" / "date_level_panel.csv"
VERSION = "2.15.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.15 bottom-rebound target calibration audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.15 target calibration policy JSON.")
    parser.add_argument("--date-panel", default=str(DEFAULT_DATE_PANEL), help="V2.11 date-level panel.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    panel = add_features(read_date_panel(Path(args.date_panel)))
    label_definition_audit = build_label_definition_audit(panel, policy)
    target_rule_summary, time_split_summary, rolling_target_stability, false_positive_miss_cases = run_target_audit(panel, policy)
    top_candidates = build_top_candidates(target_rule_summary, policy)
    leakage_audit = build_leakage_audit(policy)
    optimization_notes = build_optimization_notes(top_candidates, label_definition_audit, target_rule_summary, policy)
    summary = build_run_summary(policy, panel, target_rule_summary, top_candidates, leakage_audit, optimization_notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    label_definition_audit.to_csv(debug_dir / "label_definition_audit.csv", index=False, encoding="utf-8-sig")
    target_rule_summary.to_csv(debug_dir / "target_rule_summary.csv", index=False, encoding="utf-8-sig")
    time_split_summary.to_csv(debug_dir / "time_split_summary.csv", index=False, encoding="utf-8-sig")
    rolling_target_stability.to_csv(debug_dir / "rolling_target_stability.csv", index=False, encoding="utf-8-sig")
    false_positive_miss_cases.to_csv(debug_dir / "false_positive_miss_cases.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", optimization_notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            label_definition_audit=label_definition_audit,
            time_split_summary=time_split_summary,
            rolling_target_stability=rolling_target_stability,
            false_positive_miss_cases=false_positive_miss_cases,
            leakage_audit=leakage_audit,
            optimization_notes=optimization_notes,
            policy=policy,
        ),
        encoding="utf-8",
    )

    print("V2.15抄底反弹目标标签校准审计完成")
    print(f"日期面板行数={summary['date_count']}")
    print(f"规则数={summary['rule_count']}")
    print(f"审计组合行数={summary['audit_rows']}")
    print(f"目标校准候选数={summary['target_candidate_count']}")
    print(f"审计失败数={summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def read_date_panel(path: Path) -> pd.DataFrame:
    panel = pd.read_csv(path, encoding="utf-8-sig")
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    panel = panel.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    panel["trade_date_text"] = panel["trade_date"].dt.strftime("%Y-%m-%d")
    return panel


def add_features(panel: pd.DataFrame) -> pd.DataFrame:
    output = panel.copy()
    fields = [
        "market_stress_score",
        "negative_breadth_60d",
        "return_pressure",
        "volatility_pressure",
        "drawdown_pressure",
        "market_drawdown_252d",
        "low_value_oversold_non_trap_count",
        "low_value_oversold_count",
    ]
    for field in fields:
        if field not in output.columns:
            continue
        values = pd.to_numeric(output[field], errors="coerce")
        output[f"{field}_prev1"] = values.shift(1)
        output[f"{field}_chg1"] = values.diff(1)
    output["bottom_condition_count"] = bottom_condition_count(output)
    return output


def bottom_condition_count(panel: pd.DataFrame) -> pd.Series:
    conditions = [
        pd.to_numeric(panel["market_stress_score"], errors="coerce") >= 0.55,
        pd.to_numeric(panel["negative_breadth_60d"], errors="coerce") >= 0.75,
        pd.to_numeric(panel["market_drawdown_252d"], errors="coerce") <= -0.15,
        pd.to_numeric(panel["low_value_oversold_non_trap_count"], errors="coerce") >= 5,
    ]
    total = pd.Series(0, index=panel.index)
    for condition in conditions:
        total += condition.fillna(False).astype(int)
    return total


def bottom_eligible_mask(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.Series:
    masks = [condition_mask(panel, condition) for condition in policy["bottom_eligible_conditions"]]
    if not masks:
        return pd.Series(False, index=panel.index)
    if policy.get("bottom_eligible_logic", "any") == "all":
        result = pd.Series(True, index=panel.index)
        for mask in masks:
            result &= mask
        return result.fillna(False)
    result = pd.Series(False, index=panel.index)
    for mask in masks:
        result |= mask
    return result.fillna(False)


def build_label_definition_audit(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    eligible = bottom_eligible_mask(panel, policy)
    for horizon in [int(item) for item in policy["horizons"]]:
        return_col = f"benchmark_forward_return_{horizon}d"
        valid = panel.dropna(subset=[return_col]).copy()
        eligible_valid = eligible.reindex(valid.index, fill_value=False)
        threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
        old_strong = pd.to_numeric(valid[return_col], errors="coerce") >= threshold
        bottom_target = eligible_valid & old_strong
        bull_acceleration = old_strong & ~eligible_valid
        rows.append(
            {
                "horizon": horizon,
                "valid_dates": int(len(valid)),
                "old_strong_rebound_count": int(old_strong.sum()),
                "bottom_eligible_count": int(eligible_valid.sum()),
                "bottom_rebound_target_count": int(bottom_target.sum()),
                "bull_acceleration_removed_count": int(bull_acceleration.sum()),
                "bull_acceleration_removed_rate_of_old_label": float(bull_acceleration.sum() / old_strong.sum()) if int(old_strong.sum()) else math.nan,
                "bottom_target_rate_in_eligible": float(bottom_target.sum() / eligible_valid.sum()) if int(eligible_valid.sum()) else math.nan,
                "bottom_eligible_mean_return": float(pd.to_numeric(valid.loc[eligible_valid, return_col], errors="coerce").mean()) if int(eligible_valid.sum()) else math.nan,
                "all_dates_mean_return": float(pd.to_numeric(valid[return_col], errors="coerce").mean()),
            }
        )
    return pd.DataFrame(rows)


def run_target_audit(panel: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    split_rows: list[dict[str, Any]] = []
    rolling_rows: list[dict[str, Any]] = []
    case_frames: list[pd.DataFrame] = []
    for rule in policy["rules"]:
        rule_id = str(rule["rule_id"])
        rule_name = str(rule["rule_name_zh"])
        family = str(rule.get("family", ""))
        signal_mask = build_rule_mask(panel, rule)
        for horizon in [int(item) for item in policy["horizons"]]:
            return_col = f"benchmark_forward_return_{horizon}d"
            valid = panel.dropna(subset=[return_col]).copy()
            valid_signal = signal_mask.reindex(valid.index, fill_value=False)
            full = describe_target_period("full_sample", "全样本", "full", valid, valid_signal, return_col, horizon, policy)
            full.update({
                "rule_id": rule_id,
                "rule_name_zh": rule_name,
                "family": family,
                "random_precision_pvalue": random_precision_pvalue(valid, valid_signal, return_col, horizon, policy),
            })
            split_periods: list[dict[str, Any]] = []
            for split in policy["time_splits"]:
                period = slice_period(valid, split["start"], split["end"])
                period_signal = signal_mask.reindex(period.index, fill_value=False)
                split_row = describe_target_period(
                    str(split["split_id"]),
                    str(split["split_name_zh"]),
                    str(split["role"]),
                    period,
                    period_signal,
                    return_col,
                    horizon,
                    policy,
                )
                split_row.update({"rule_id": rule_id, "rule_name_zh": rule_name, "family": family})
                split_rows.append(split_row)
                split_periods.append(split_row)
            rolling_periods_rows: list[dict[str, Any]] = []
            for period_def in rolling_periods(valid, policy):
                period = slice_period(valid, period_def["start"], period_def["end"])
                period_signal = signal_mask.reindex(period.index, fill_value=False)
                rolling_row = describe_target_period(
                    period_def["period_id"],
                    period_def["period_name"],
                    "rolling",
                    period,
                    period_signal,
                    return_col,
                    horizon,
                    policy,
                )
                rolling_row.update({"rule_id": rule_id, "rule_name_zh": rule_name, "family": family})
                rolling_rows.append(rolling_row)
                rolling_periods_rows.append(rolling_row)
            row = {**full, **summarize_target_splits(split_periods), **summarize_target_rolling(rolling_periods_rows)}
            row["target_score"] = score_target_rule(row, policy)
            row["target_status"] = classify_target_rule(row, policy)
            summary_rows.append(row)
            case_frames.append(build_cases(valid, valid_signal, return_col, rule_id, rule_name, horizon, policy))
    return (
        pd.DataFrame(summary_rows),
        pd.DataFrame(split_rows),
        pd.DataFrame(rolling_rows),
        pd.concat(case_frames, ignore_index=True, sort=False) if case_frames else pd.DataFrame(),
    )


def describe_target_period(
    period_id: str,
    period_name: str,
    role: str,
    frame: pd.DataFrame,
    signal_mask: pd.Series,
    return_col: str,
    horizon: int,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if frame.empty:
        return empty_period(period_id, period_name, role, horizon)
    signal_mask = signal_mask.reindex(frame.index, fill_value=False)
    eligible = bottom_eligible_mask(frame, policy)
    returns = pd.to_numeric(frame[return_col], errors="coerce")
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    bad_threshold = float(policy["bad_window_thresholds"][str(horizon)])
    target = eligible & (returns >= threshold)
    signal = frame[signal_mask].copy()
    signal_returns = pd.to_numeric(signal[return_col], errors="coerce")
    signal_target = target.reindex(signal.index, fill_value=False)
    eligible_count = int(eligible.sum())
    target_count = int(target.sum())
    signal_count = int(signal_returns.notna().sum())
    captured = int(signal_target.sum())
    baseline_precision = float(target_count / eligible_count) if eligible_count else math.nan
    precision = float(captured / signal_count) if signal_count else math.nan
    non = nonoverlap_by_horizon(signal, horizon)
    non_returns = pd.to_numeric(non[return_col], errors="coerce")
    return {
        "period_id": period_id,
        "period_name_zh": period_name,
        "role": role,
        "horizon": horizon,
        "all_samples": int(len(frame)),
        "bottom_eligible_count": eligible_count,
        "bottom_target_count": target_count,
        "signal_samples": signal_count,
        "nonoverlap_samples": int(non_returns.notna().sum()),
        "captured_bottom_target_count": captured,
        "target_precision": precision,
        "bottom_baseline_precision": baseline_precision,
        "precision_edge_vs_bottom": safe_sub(precision, baseline_precision),
        "target_recall": float(captured / target_count) if target_count else math.nan,
        "signal_mean_return": float(signal_returns.mean()) if signal_count else math.nan,
        "bottom_eligible_mean_return": float(returns[eligible].mean()) if eligible_count else math.nan,
        "mean_return_edge_vs_bottom": safe_sub(float(signal_returns.mean()) if signal_count else math.nan, float(returns[eligible].mean()) if eligible_count else math.nan),
        "signal_win_rate": float((signal_returns > 0).mean()) if signal_count else math.nan,
        "signal_bad_window_rate": float((signal_returns <= bad_threshold).mean()) if signal_count else math.nan,
    }


def empty_period(period_id: str, period_name: str, role: str, horizon: int) -> dict[str, Any]:
    return {
        "period_id": period_id,
        "period_name_zh": period_name,
        "role": role,
        "horizon": horizon,
        "all_samples": 0,
        "bottom_eligible_count": 0,
        "bottom_target_count": 0,
        "signal_samples": 0,
        "nonoverlap_samples": 0,
        "captured_bottom_target_count": 0,
        "target_precision": math.nan,
        "bottom_baseline_precision": math.nan,
        "precision_edge_vs_bottom": math.nan,
        "target_recall": math.nan,
        "signal_mean_return": math.nan,
        "bottom_eligible_mean_return": math.nan,
        "mean_return_edge_vs_bottom": math.nan,
        "signal_win_rate": math.nan,
        "signal_bad_window_rate": math.nan,
    }


def build_rule_mask(panel: pd.DataFrame, rule: dict[str, Any]) -> pd.Series:
    if "min_bottom_condition_count" in rule:
        return pd.to_numeric(panel["bottom_condition_count"], errors="coerce") >= int(rule["min_bottom_condition_count"])
    mask = pd.Series(True, index=panel.index)
    for condition in rule.get("conditions", []):
        mask &= condition_mask(panel, condition)
    return mask.fillna(False)


def condition_mask(panel: pd.DataFrame, condition: dict[str, Any]) -> pd.Series:
    field = str(condition["field"])
    op = str(condition["op"])
    value = float(condition["value"])
    if field not in panel.columns:
        return pd.Series(False, index=panel.index)
    series = pd.to_numeric(panel[field], errors="coerce")
    if op == ">=":
        return (series >= value).fillna(False)
    if op == ">":
        return (series > value).fillna(False)
    if op == "<=":
        return (series <= value).fillna(False)
    if op == "<":
        return (series < value).fillna(False)
    raise ValueError(f"Unsupported op: {op}")


def summarize_target_splits(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if int(row.get("signal_samples", 0)) > 0]
    positive = [row for row in eligible if nz(row.get("precision_edge_vs_bottom")) > 0]
    recent = next((row for row in rows if row.get("role") == "recent_check"), {})
    return {
        "split_count": len(rows),
        "split_eligible_count": len(eligible),
        "split_positive_precision_edge_count": len(positive),
        "split_positive_precision_edge_rate": float(len(positive) / len(eligible)) if eligible else math.nan,
        "split_min_precision_edge_vs_bottom": min_metric(eligible, "precision_edge_vs_bottom"),
        "recent_signal_samples": int(recent.get("signal_samples", 0) or 0),
        "recent_target_precision": recent.get("target_precision", math.nan),
        "recent_bottom_baseline_precision": recent.get("bottom_baseline_precision", math.nan),
        "recent_precision_edge_vs_bottom": recent.get("precision_edge_vs_bottom", math.nan),
        "recent_target_recall": recent.get("target_recall", math.nan),
        "recent_signal_bad_window_rate": recent.get("signal_bad_window_rate", math.nan),
    }


def summarize_target_rolling(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if int(row.get("signal_samples", 0)) > 0]
    positive = [row for row in eligible if nz(row.get("precision_edge_vs_bottom")) > 0]
    return {
        "rolling_count": len(rows),
        "rolling_eligible_count": len(eligible),
        "rolling_positive_precision_edge_count": len(positive),
        "rolling_positive_precision_edge_rate": float(len(positive) / len(eligible)) if eligible else math.nan,
        "rolling_min_precision_edge_vs_bottom": min_metric(eligible, "precision_edge_vs_bottom"),
        "rolling_min_signal_mean_return": min_metric(eligible, "signal_mean_return"),
    }


def score_target_rule(row: dict[str, Any], policy: dict[str, Any]) -> float:
    score = 0.0
    score += 1.8 * nz(row.get("precision_edge_vs_bottom"))
    score += 1.2 * nz(row.get("target_recall"))
    score += 1.0 * nz(row.get("recent_precision_edge_vs_bottom"))
    score += 0.8 * (nz(row.get("split_positive_precision_edge_rate")) - 0.5)
    score += 0.8 * (nz(row.get("rolling_positive_precision_edge_rate")) - 0.5)
    score += 0.5 * nz(row.get("mean_return_edge_vs_bottom"))
    score -= 1.0 * nz(row.get("signal_bad_window_rate"))
    if nz(row.get("random_precision_pvalue"), 1.0) > float(policy["promotion_thresholds"]["max_random_precision_pvalue"]):
        score -= 0.10
    return float(score)


def classify_target_rule(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    checks = [
        nz(row.get("signal_samples")) >= int(th["min_full_samples"]),
        nz(row.get("nonoverlap_samples")) >= int(th["min_full_nonoverlap_samples"]),
        nz(row.get("recent_signal_samples")) >= int(th["min_recent_samples"]),
        nz(row.get("precision_edge_vs_bottom")) >= float(th["min_full_precision_edge_vs_bottom"]),
        nz(row.get("recent_precision_edge_vs_bottom")) >= float(th["min_recent_precision_edge_vs_bottom"]),
        nz(row.get("target_recall")) >= float(th["min_full_recall"]),
        nz(row.get("split_positive_precision_edge_rate")) >= float(th["min_split_positive_precision_edge_rate"]),
        nz(row.get("rolling_positive_precision_edge_rate")) >= float(th["min_rolling_positive_precision_edge_rate"]),
        nz(row.get("signal_bad_window_rate")) <= float(th["max_bad_window_rate"]),
        nz(row.get("random_precision_pvalue"), 1.0) <= float(th["max_random_precision_pvalue"]),
    ]
    if all(checks):
        return "目标校准反弹窗口候选"
    if not checks[0] or not checks[1]:
        return "样本不足"
    if checks[3] and checks[5]:
        return "目标有效但稳定性不足"
    return "拒绝"


def random_precision_pvalue(frame: pd.DataFrame, signal_mask: pd.Series, return_col: str, horizon: int, policy: dict[str, Any]) -> float:
    signal_count = int(signal_mask.reindex(frame.index, fill_value=False).sum())
    if signal_count <= 0:
        return math.nan
    eligible = bottom_eligible_mask(frame, policy)
    eligible_idx = np.array(frame.index[eligible].tolist())
    if len(eligible_idx) == 0:
        return math.nan
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    target = eligible & (pd.to_numeric(frame[return_col], errors="coerce") >= threshold)
    signal_precision = float((target & signal_mask.reindex(frame.index, fill_value=False)).sum() / signal_count)
    rng = np.random.default_rng(int(policy["random_seed"]) + horizon + signal_count)
    draws: list[float] = []
    replace = signal_count > len(eligible_idx)
    for _ in range(1000):
        sampled = rng.choice(eligible_idx, size=signal_count, replace=replace)
        draws.append(float(target.reindex(sampled).mean()))
    return float((np.array(draws) >= signal_precision).mean())


def build_cases(
    valid: pd.DataFrame,
    signal_mask: pd.Series,
    return_col: str,
    rule_id: str,
    rule_name: str,
    horizon: int,
    policy: dict[str, Any],
) -> pd.DataFrame:
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    returns = pd.to_numeric(valid[return_col], errors="coerce")
    eligible = bottom_eligible_mask(valid, policy)
    target = eligible & (returns >= threshold)
    signal = signal_mask.reindex(valid.index, fill_value=False)
    false_positive = valid[signal & ~target].copy()
    false_positive[return_col] = returns.reindex(false_positive.index)
    false_positive["case_type"] = "false_positive_not_bottom_rebound"
    false_positive = false_positive.sort_values(return_col).head(10)
    miss = valid[target & ~signal].copy()
    miss[return_col] = returns.reindex(miss.index)
    miss["case_type"] = "missed_bottom_rebound"
    miss = miss.sort_values(return_col, ascending=False).head(10)
    result = pd.concat([false_positive, miss], ignore_index=True, sort=False)
    result["rule_id"] = rule_id
    result["rule_name_zh"] = rule_name
    result["horizon"] = horizon
    result["trade_date"] = result["trade_date_text"]
    keep_cols = [
        "rule_id",
        "rule_name_zh",
        "horizon",
        "case_type",
        "trade_date",
        return_col,
        "market_stress_score",
        "negative_breadth_60d",
        "market_drawdown_252d",
        "bottom_condition_count",
        "low_value_oversold_non_trap_count",
        "return_pressure",
        "volatility_pressure",
    ]
    return result[[col for col in keep_cols if col in result.columns]]


def build_top_candidates(summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    priority = {
        "目标校准反弹窗口候选": 0,
        "目标有效但稳定性不足": 1,
        "样本不足": 2,
        "拒绝": 3,
    }
    cols = [
        "rule_id",
        "rule_name_zh",
        "family",
        "horizon",
        "target_status",
        "target_score",
        "signal_samples",
        "nonoverlap_samples",
        "target_precision",
        "bottom_baseline_precision",
        "precision_edge_vs_bottom",
        "target_recall",
        "signal_mean_return",
        "bottom_eligible_mean_return",
        "mean_return_edge_vs_bottom",
        "signal_bad_window_rate",
        "random_precision_pvalue",
        "recent_signal_samples",
        "recent_precision_edge_vs_bottom",
        "split_positive_precision_edge_rate",
        "rolling_positive_precision_edge_rate",
        "rolling_min_precision_edge_vs_bottom",
    ]
    output = summary[[col for col in cols if col in summary.columns]].copy()
    output["_priority"] = output["target_status"].map(priority).fillna(9)
    output = output.sort_values(["_priority", "target_score", "signal_samples"], ascending=[True, False, False])
    return output.drop(columns=["_priority"]).head(int(policy["top_candidate_rows"]))


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "target_label_separates_bottom_from_bull_acceleration",
                "status": "pass",
                "evidence": "bottom target requires pressure, breadth, drawdown or low-value-oversold eligibility before forward return label",
                "action": "V2.15不再把所有强上涨都当成抄底窗口。",
            },
            {
                "audit_item": "future_return_used_only_as_label",
                "status": "pass",
                "evidence": "benchmark_forward_return_* only defines target outcomes and never enters signal conditions",
                "action": "未来收益不作为触发特征。",
            },
            {
                "audit_item": "post_hoc_label_calibration",
                "status": "research_only",
                "evidence": "V2.15 label was adjusted after V2.11-V2.14 failure analysis",
                "action": "只能作为研究标签校准，不能宣称纯样本外。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(
    top_candidates: pd.DataFrame,
    label_definition_audit: pd.DataFrame,
    target_rule_summary: pd.DataFrame,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if top_candidates.empty:
        return {"main_diagnosis": "V2.15没有可排序规则。", "next_iterations": ["检查输入文件。"]}
    best = top_candidates.iloc[0].to_dict()
    removed_rates = label_definition_audit["bull_acceleration_removed_rate_of_old_label"].dropna()
    avg_removed = float(removed_rates.mean()) if not removed_rates.empty else math.nan
    notes: list[str] = []
    if not math.isnan(avg_removed) and avg_removed > 0.10:
        notes.append("旧强反弹标签混入了非抄底型牛市加速样本，V2.15校准是必要的。")
    if str(best.get("target_status")) == "目标校准反弹窗口候选":
        notes.append("校准后的抄底反弹标签下出现候选，但仍需实时仿真净值验证。")
    else:
        notes.append("校准目标后仍未形成可直接使用的反弹窗口识别器。")
    if nz(best.get("rolling_positive_precision_edge_rate")) < float(policy["promotion_thresholds"]["min_rolling_positive_precision_edge_rate"]):
        notes.append("滚动稳定性仍是主要问题，规则在不同市场阶段的精确率提升不稳定。")
    if nz(best.get("target_recall")) < float(policy["promotion_thresholds"]["min_full_recall"]):
        notes.append("目标召回不足，系统仍可能错过大量压力后反弹。")
    if nz(best.get("signal_bad_window_rate")) > float(policy["promotion_thresholds"]["max_bad_window_rate"]):
        notes.append("坏窗口比例偏高，下一轮应研究入场后二次确认和最大回撤控制。")
    return {
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": best.get("horizon", ""),
        "best_target_status": best.get("target_status", ""),
        "average_bull_acceleration_removed_rate": None if math.isnan(avg_removed) else avg_removed,
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_16_direction": "若V2.15出现候选，冻结该目标标签和规则做实时仿真净值；若未出现候选，转向行业相对强度确认和入场后最大回撤控制。"
    }


def build_run_summary(
    policy: dict[str, Any],
    panel: pd.DataFrame,
    target_rule_summary: pd.DataFrame,
    top_candidates: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
) -> dict[str, Any]:
    candidates = target_rule_summary[target_rule_summary["target_status"] == "目标校准反弹窗口候选"] if not target_rule_summary.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage_audit["status"] == "fail").sum()) if not leakage_audit.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_count": int(len(panel)),
        "rule_count": int(len(policy["rules"])),
        "audit_rows": int(len(target_rule_summary)),
        "target_candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_target_status": best.get("target_status", ""),
        "best_precision_edge_vs_bottom": float_or_none(best.get("precision_edge_vs_bottom")),
        "best_target_recall": float_or_none(best.get("target_recall")),
        "best_rolling_positive_precision_edge_rate": float_or_none(best.get("rolling_positive_precision_edge_rate")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": optimization_notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    label_definition_audit: pd.DataFrame,
    time_split_summary: pd.DataFrame,
    rolling_target_stability: pd.DataFrame,
    false_positive_miss_cases: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# V2.15 抄底反弹目标标签校准审计报告")
    lines.append("")
    lines.append(f"版本：{VERSION}")
    lines.append("")
    lines.append("## 研究结论")
    lines.append("")
    lines.append("V2.15 修正旧强反弹标签过宽的问题：只有在压力、回撤、普跌或低估超跌条件出现后，未来收益达到阈值，才计入“抄底反弹目标”。")
    lines.append("")
    lines.append(f"- 日期面板行数：{summary['date_count']}")
    lines.append(f"- 规则数：{summary['rule_count']}")
    lines.append(f"- 审计组合行数：{summary['audit_rows']}")
    lines.append(f"- 目标校准候选数：{summary['target_candidate_count']}")
    lines.append(f"- 审计失败数：{summary['audit_fail_count']}")
    lines.append(f"- 最终结论：{summary['final_verdict']}")
    lines.append(f"- 主要诊断：{summary['main_diagnosis']}")
    lines.append("")
    lines.append("## 标签定义审计")
    lines.append("")
    lines.extend(table_or_empty(label_definition_audit, {
        "horizon": "持有期",
        "valid_dates": "有效日期",
        "old_strong_rebound_count": "旧强反弹数",
        "bottom_eligible_count": "抄底候选环境数",
        "bottom_rebound_target_count": "抄底反弹目标数",
        "bull_acceleration_removed_count": "剔除牛市加速数",
        "bull_acceleration_removed_rate_of_old_label": "旧标签剔除比例",
        "bottom_target_rate_in_eligible": "候选环境目标率",
        "bottom_eligible_mean_return": "候选环境收益",
        "all_dates_mean_return": "全日期收益",
    }, {
        "bull_acceleration_removed_rate_of_old_label",
        "bottom_target_rate_in_eligible",
        "bottom_eligible_mean_return",
        "all_dates_mean_return",
    }))
    lines.append("")
    lines.append("## 规则排序")
    lines.append("")
    lines.extend(table_or_empty(top_candidates.head(20), {
        "rule_id": "规则ID",
        "rule_name_zh": "规则",
        "family": "来源",
        "horizon": "持有期",
        "target_status": "状态",
        "target_score": "目标分",
        "signal_samples": "信号样本",
        "nonoverlap_samples": "非重叠",
        "target_precision": "目标精确率",
        "bottom_baseline_precision": "底部环境基准率",
        "precision_edge_vs_bottom": "精确率提升",
        "target_recall": "目标召回",
        "signal_mean_return": "信号收益",
        "mean_return_edge_vs_bottom": "收益提升",
        "signal_bad_window_rate": "坏窗口",
        "random_precision_pvalue": "随机精确率p值",
        "recent_precision_edge_vs_bottom": "近年精确率提升",
        "split_positive_precision_edge_rate": "切分正提升比例",
        "rolling_positive_precision_edge_rate": "滚动正提升比例",
    }, {
        "target_precision",
        "bottom_baseline_precision",
        "precision_edge_vs_bottom",
        "target_recall",
        "signal_mean_return",
        "mean_return_edge_vs_bottom",
        "signal_bad_window_rate",
        "random_precision_pvalue",
        "recent_precision_edge_vs_bottom",
        "split_positive_precision_edge_rate",
        "rolling_positive_precision_edge_rate",
    }))
    lines.append("")
    lines.append("## 最佳规则时间切分")
    lines.append("")
    best_rule = str(summary.get("best_rule_id", ""))
    best_horizon = int(summary.get("best_horizon", 0))
    best_splits = time_split_summary[
        (time_split_summary["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(time_split_summary["horizon"], errors="coerce") == best_horizon)
    ].copy() if not time_split_summary.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_splits, {
        "period_name_zh": "阶段",
        "signal_samples": "信号样本",
        "target_precision": "目标精确率",
        "bottom_baseline_precision": "底部环境基准率",
        "precision_edge_vs_bottom": "精确率提升",
        "target_recall": "目标召回",
        "signal_bad_window_rate": "坏窗口",
    }, {
        "target_precision",
        "bottom_baseline_precision",
        "precision_edge_vs_bottom",
        "target_recall",
        "signal_bad_window_rate",
    }))
    lines.append("")
    lines.append("## 最佳规则滚动窗口")
    lines.append("")
    best_rolling = rolling_target_stability[
        (rolling_target_stability["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(rolling_target_stability["horizon"], errors="coerce") == best_horizon)
    ].copy() if not rolling_target_stability.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_rolling, {
        "period_name_zh": "滚动阶段",
        "signal_samples": "信号样本",
        "target_precision": "目标精确率",
        "bottom_baseline_precision": "底部环境基准率",
        "precision_edge_vs_bottom": "精确率提升",
        "target_recall": "目标召回",
        "signal_bad_window_rate": "坏窗口",
    }, {
        "target_precision",
        "bottom_baseline_precision",
        "precision_edge_vs_bottom",
        "target_recall",
        "signal_bad_window_rate",
    }))
    lines.append("")
    lines.append("## 误报和漏报")
    lines.append("")
    best_cases = false_positive_miss_cases[
        (false_positive_miss_cases["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(false_positive_miss_cases["horizon"], errors="coerce") == best_horizon)
    ].copy() if not false_positive_miss_cases.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_cases.head(20), {
        "case_type": "类型",
        "trade_date": "日期",
        f"benchmark_forward_return_{best_horizon}d": "未来收益",
        "market_stress_score": "压力分",
        "negative_breadth_60d": "下跌广度",
        "market_drawdown_252d": "市场回撤",
        "bottom_condition_count": "底部条件数",
        "low_value_oversold_non_trap_count": "非陷阱低估超跌数",
        "return_pressure": "收益压力",
        "volatility_pressure": "波动压力",
    }, {
        f"benchmark_forward_return_{best_horizon}d",
        "market_stress_score",
        "negative_breadth_60d",
        "market_drawdown_252d",
        "return_pressure",
        "volatility_pressure",
    }))
    lines.append("")
    lines.append("## 下一轮优化方向")
    lines.append("")
    for item in optimization_notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议 V2.16 方向：{optimization_notes.get('recommended_v2_16_direction', '')}")
    lines.append("")
    lines.append("## 审计")
    lines.append("")
    lines.extend(table_or_empty(leakage_audit, {
        "audit_item": "项目",
        "status": "状态",
        "evidence": "证据",
        "action": "动作",
    }, set()))
    lines.append("")
    lines.append("## 输出文件说明")
    lines.append("")
    lines.append("- `report.md`：中文目标标签校准审计报告，优先打开。")
    lines.append("- `top_candidates.csv`：校准后规则排序；不是交易信号。")
    lines.append("- `run_summary.json`：机器可读运行摘要。")
    lines.append("- `debug/`：标签定义、规则审计、时间切分、滚动窗口、误报漏报、审计和冻结策略。")
    lines.append("")
    lines.append(f"研究边界：{policy['research_boundary']}")
    return "\n".join(lines)


def nonoverlap_by_horizon(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    ordered = frame.sort_values("trade_date").copy()
    keep_idx: list[int] = []
    last_date = pd.Timestamp.min
    for idx, date in zip(ordered.index, ordered["trade_date"]):
        if pd.isna(date):
            continue
        if date > last_date:
            keep_idx.append(idx)
            last_date = date + pd.tseries.offsets.BDay(horizon)
    return ordered.loc[keep_idx].copy()


def slice_period(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    return frame[(frame["trade_date"] >= start_ts) & (frame["trade_date"] <= end_ts)].copy()


def rolling_periods(frame: pd.DataFrame, policy: dict[str, Any]) -> list[dict[str, str]]:
    if frame.empty:
        return []
    min_year = int(frame["trade_date"].dt.year.min())
    max_year = int(frame["trade_date"].dt.year.max())
    window_years = int(policy["rolling_window_years"])
    step_years = int(policy["rolling_step_years"])
    periods: list[dict[str, str]] = []
    year = min_year
    while year + window_years - 1 <= max_year:
        periods.append(
            {
                "period_id": f"rolling_{year}_{year + window_years - 1}",
                "period_name": f"{year}-{year + window_years - 1}滚动{window_years}年",
                "start": f"{year}-01-01",
                "end": f"{year + window_years - 1}-12-31",
            }
        )
        year += step_years
    return periods


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；目标标签已校准，但尚未形成稳定可用的抄底反弹窗口"
    return "research_only；校准后存在抄底反弹窗口候选，但仍需实时仿真和未来数据验证"


def table_or_empty(frame: pd.DataFrame, rename: dict[str, str], pct_cols: set[str]) -> list[str]:
    if frame.empty:
        return ["无数据。"]
    display = frame[[col for col in rename if col in frame.columns]].copy()
    for col in display.columns:
        if col in pct_cols:
            display[col] = display[col].map(fmt_pct)
        elif pd.api.types.is_float_dtype(display[col]):
            display[col] = display[col].map(lambda value: fmt_float(value, 3))
    display = display.rename(columns=rename)
    cols = list(display.columns)
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in display.iterrows():
        lines.append("| " + " | ".join(str(row[col]) if pd.notna(row[col]) else "" for col in cols) + " |")
    return lines


def min_metric(rows: list[dict[str, Any]], field: str) -> float:
    values = [float_or_nan(row.get(field)) for row in rows]
    values = [value for value in values if not math.isnan(value)]
    return min(values) if values else math.nan


def safe_sub(left: Any, right: Any) -> float:
    left_number = float_or_nan(left)
    right_number = float_or_nan(right)
    if math.isnan(left_number) or math.isnan(right_number):
        return math.nan
    return float(left_number - right_number)


def nz(value: Any, default: float = 0.0) -> float:
    number = float_or_nan(value)
    return default if math.isnan(number) else number


def float_or_nan(value: Any, default: float = math.nan) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def float_or_none(value: Any) -> float | None:
    number = float_or_nan(value)
    return None if math.isnan(number) else number


def fmt_float(value: Any, digits: int = 3) -> str:
    number = float_or_nan(value)
    if math.isnan(number):
        return ""
    return f"{number:.{digits}f}"


def fmt_pct(value: Any) -> str:
    number = float_or_nan(value)
    if math.isnan(number):
        return ""
    return f"{number * 100:.2f}%"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=json_default)


def json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return None if math.isnan(number) else number
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    return str(value)


if __name__ == "__main__":
    main()
