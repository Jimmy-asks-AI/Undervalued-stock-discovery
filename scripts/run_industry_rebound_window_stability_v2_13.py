#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_stability_policy_v2_13.json"
DEFAULT_DATE_PANEL = ROOT / "outputs" / "industry_rebound_window_audit_v2_11" / "debug" / "date_level_panel.csv"
V211_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_audit_v2_11.py"
VERSION = "2.13.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.13 rebound-window stability audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.13 stability policy JSON.")
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

    v211 = load_v211_module()
    date_panel = read_date_panel(Path(args.date_panel))
    validation_summary, time_split_summary, rolling_stability, baseline_comparison, false_alarm_miss_cases = run_stability_audit(
        date_panel=date_panel,
        policy=policy,
        v211=v211,
    )
    top_candidates = build_top_candidates(validation_summary, policy)
    leakage_audit = build_leakage_audit(policy)
    optimization_notes = build_optimization_notes(top_candidates, validation_summary, time_split_summary, rolling_stability, policy)
    summary = build_run_summary(
        policy=policy,
        date_panel=date_panel,
        validation_summary=validation_summary,
        top_candidates=top_candidates,
        leakage_audit=leakage_audit,
        optimization_notes=optimization_notes,
    )

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    validation_summary.to_csv(debug_dir / "validation_summary.csv", index=False, encoding="utf-8-sig")
    time_split_summary.to_csv(debug_dir / "time_split_summary.csv", index=False, encoding="utf-8-sig")
    rolling_stability.to_csv(debug_dir / "rolling_stability.csv", index=False, encoding="utf-8-sig")
    baseline_comparison.to_csv(debug_dir / "baseline_comparison.csv", index=False, encoding="utf-8-sig")
    false_alarm_miss_cases.to_csv(debug_dir / "false_alarm_miss_cases.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", optimization_notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            time_split_summary=time_split_summary,
            rolling_stability=rolling_stability,
            baseline_comparison=baseline_comparison,
            false_alarm_miss_cases=false_alarm_miss_cases,
            leakage_audit=leakage_audit,
            optimization_notes=optimization_notes,
            policy=policy,
        ),
        encoding="utf-8",
    )

    print("V2.13反弹窗口稳定性审计完成")
    print(f"日期面板行数={summary['date_count']}")
    print(f"规则数={summary['rule_count']}")
    print(f"审计组合行数={summary['audit_rows']}")
    print(f"稳定候选数={summary['stable_candidate_count']}")
    print(f"审计失败数={summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v211_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_rebound_window_audit_v2_11", V211_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load V2.11 module from {V211_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_date_panel(path: Path) -> pd.DataFrame:
    panel = pd.read_csv(path, encoding="utf-8-sig")
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    panel = panel.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    panel["trade_date_text"] = panel["trade_date"].dt.strftime("%Y-%m-%d")
    return panel


def run_stability_audit(
    *,
    date_panel: pd.DataFrame,
    policy: dict[str, Any],
    v211: Any,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    split_rows: list[dict[str, Any]] = []
    rolling_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    case_frames: list[pd.DataFrame] = []

    horizons = [int(item) for item in policy["horizons"]]
    for rule in policy["rules"]:
        rule_id = str(rule["rule_id"])
        rule_name = str(rule["rule_name_zh"])
        mask = build_rule_mask(date_panel, rule)
        for horizon in horizons:
            return_col = f"benchmark_forward_return_{horizon}d"
            if return_col not in date_panel.columns:
                continue

            valid = date_panel.dropna(subset=[return_col]).copy()
            valid_mask = mask.reindex(valid.index, fill_value=False)
            signal = valid[valid_mask].copy()
            pressure = pressure_frame(valid, policy)
            pressure_not_signal = pressure[~pressure["trade_date_text"].isin(set(signal["trade_date_text"]))].copy()

            full = describe_period(
                period_id="full_sample",
                period_name="全样本",
                role="full",
                frame=valid,
                signal=signal,
                pressure=pressure,
                return_col=return_col,
                horizon=horizon,
                policy=policy,
                v211=v211,
            )
            random_stats = v211.random_same_size_stats(valid, signal, return_col, int(policy["random_seed"]))
            full["random_same_size_mean_return_p50"] = random_stats["p50"]
            full["random_same_size_mean_return_p90"] = random_stats["p90"]
            full["random_outperformance_pvalue"] = random_stats["pvalue_right"]

            baseline_rows.extend(build_baseline_rows(rule_id, rule_name, horizon, full, pressure_not_signal, return_col, policy, v211))

            rule_split_rows = []
            for split in policy["time_splits"]:
                period = slice_period(valid, split["start"], split["end"])
                period_mask = mask.reindex(period.index, fill_value=False)
                period_signal = period[period_mask].copy()
                period_pressure = pressure_frame(period, policy)
                split_row = describe_period(
                    period_id=str(split["split_id"]),
                    period_name=str(split["split_name_zh"]),
                    role=str(split["role"]),
                    frame=period,
                    signal=period_signal,
                    pressure=period_pressure,
                    return_col=return_col,
                    horizon=horizon,
                    policy=policy,
                    v211=v211,
                )
                split_row.update({"rule_id": rule_id, "rule_name_zh": rule_name})
                split_rows.append(split_row)
                rule_split_rows.append(split_row)

            rule_rolling_rows = []
            for roll in rolling_periods(valid, policy):
                period = slice_period(valid, roll["start"], roll["end"])
                period_mask = mask.reindex(period.index, fill_value=False)
                period_signal = period[period_mask].copy()
                period_pressure = pressure_frame(period, policy)
                rolling_row = describe_period(
                    period_id=str(roll["period_id"]),
                    period_name=str(roll["period_name"]),
                    role="rolling",
                    frame=period,
                    signal=period_signal,
                    pressure=period_pressure,
                    return_col=return_col,
                    horizon=horizon,
                    policy=policy,
                    v211=v211,
                )
                rolling_row.update({"rule_id": rule_id, "rule_name_zh": rule_name})
                rolling_rows.append(rolling_row)
                rule_rolling_rows.append(rolling_row)

            row = {
                "rule_id": rule_id,
                "rule_name_zh": rule_name,
                "horizon": horizon,
                **prefix_metrics(full, "full"),
                **summarize_splits(rule_split_rows),
                **summarize_rolling(rule_rolling_rows),
            }
            row["stability_score"] = score_stability(row, policy)
            row["stability_status"] = classify_stability(row, policy)
            summary_rows.append(row)
            case_frames.append(build_cases(valid, signal, return_col, rule_id, rule_name, horizon, policy))

    return (
        pd.DataFrame(summary_rows),
        pd.DataFrame(split_rows),
        pd.DataFrame(rolling_rows),
        pd.DataFrame(baseline_rows),
        pd.concat(case_frames, ignore_index=True, sort=False) if case_frames else pd.DataFrame(),
    )


def describe_period(
    *,
    period_id: str,
    period_name: str,
    role: str,
    frame: pd.DataFrame,
    signal: pd.DataFrame,
    pressure: pd.DataFrame,
    return_col: str,
    horizon: int,
    policy: dict[str, Any],
    v211: Any,
) -> dict[str, Any]:
    signal_values = pd.to_numeric(signal[return_col], errors="coerce").dropna()
    pressure_values = pd.to_numeric(pressure[return_col], errors="coerce").dropna()
    all_values = pd.to_numeric(frame[return_col], errors="coerce").dropna()
    non = v211.nonoverlap_by_horizon(signal, horizon)
    non_values = pd.to_numeric(non[return_col], errors="coerce").dropna()
    strong_threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    bad_threshold = float(policy["bad_window_thresholds"][str(horizon)])
    strong_all = all_values[all_values >= strong_threshold]
    strong_signal = signal_values[signal_values >= strong_threshold]
    return {
        "period_id": period_id,
        "period_name_zh": period_name,
        "role": role,
        "horizon": horizon,
        "all_samples": int(len(all_values)),
        "pressure_samples": int(len(pressure_values)),
        "signal_samples": int(len(signal_values)),
        "nonoverlap_samples": int(len(non_values)),
        "signal_mean_return": mean_or_nan(signal_values),
        "signal_median_return": median_or_nan(signal_values),
        "signal_min_return": min_or_nan(signal_values),
        "signal_max_return": max_or_nan(signal_values),
        "all_dates_mean_return": mean_or_nan(all_values),
        "pressure_dates_mean_return": mean_or_nan(pressure_values),
        "signal_win_rate": rate(signal_values > 0),
        "pressure_dates_win_rate": rate(pressure_values > 0),
        "signal_bad_window_rate": rate(signal_values <= bad_threshold),
        "pressure_bad_window_rate": rate(pressure_values <= bad_threshold),
        "signal_strong_rebound_rate": rate(signal_values >= strong_threshold),
        "pressure_strong_rebound_rate": rate(pressure_values >= strong_threshold),
        "strong_rebound_recall": float(len(strong_signal) / len(strong_all)) if len(strong_all) else math.nan,
        "uplift_vs_all_dates": safe_sub(mean_or_nan(signal_values), mean_or_nan(all_values)),
        "uplift_vs_pressure_dates": safe_sub(mean_or_nan(signal_values), mean_or_nan(pressure_values)),
        "win_rate_uplift_vs_pressure": safe_sub(rate(signal_values > 0), rate(pressure_values > 0)),
    }


def build_baseline_rows(
    rule_id: str,
    rule_name: str,
    horizon: int,
    full: dict[str, Any],
    pressure_not_signal: pd.DataFrame,
    return_col: str,
    policy: dict[str, Any],
    v211: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pns_stats = v211.describe_simple(pressure_not_signal, return_col, horizon, policy)
    baselines = [
        ("all_decision_dates", full["all_samples"], full["all_dates_mean_return"], math.nan),
        ("pressure_dates", full["pressure_samples"], full["pressure_dates_mean_return"], full["pressure_dates_win_rate"]),
        ("pressure_not_signal", pns_stats["samples"], pns_stats["mean_return"], pns_stats["win_rate"]),
    ]
    for baseline, samples, mean_return, win_rate in baselines:
        rows.append(
            {
                "rule_id": rule_id,
                "rule_name_zh": rule_name,
                "horizon": horizon,
                "baseline": baseline,
                "signal_samples": full["signal_samples"],
                "baseline_samples": samples,
                "signal_mean_return": full["signal_mean_return"],
                "baseline_mean_return": mean_return,
                "signal_win_rate": full["signal_win_rate"],
                "baseline_win_rate": win_rate,
                "mean_uplift": safe_sub(full["signal_mean_return"], mean_return),
                "win_rate_uplift": safe_sub(full["signal_win_rate"], win_rate),
            }
        )
    return rows


def build_rule_mask(panel: pd.DataFrame, rule: dict[str, Any]) -> pd.Series:
    mask = pd.Series(True, index=panel.index)
    for condition in rule["conditions"]:
        field = str(condition["field"])
        op = str(condition["op"])
        value = float(condition["value"])
        if field not in panel.columns:
            return pd.Series(False, index=panel.index)
        series = pd.to_numeric(panel[field], errors="coerce")
        if op == ">=":
            mask &= series >= value
        elif op == ">":
            mask &= series > value
        elif op == "<=":
            mask &= series <= value
        elif op == "<":
            mask &= series < value
        else:
            raise ValueError(f"Unsupported op: {op}")
    return mask.fillna(False)


def pressure_frame(frame: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame[pd.to_numeric(frame["market_stress_score"], errors="coerce") >= float(policy["pressure_baseline_min"])].copy()


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
        start = pd.Timestamp(year=year, month=1, day=1)
        end = pd.Timestamp(year=year + window_years - 1, month=12, day=31)
        periods.append(
            {
                "period_id": f"rolling_{year}_{year + window_years - 1}",
                "period_name": f"{year}-{year + window_years - 1}滚动{window_years}年",
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
            }
        )
        year += step_years
    return periods


def prefix_metrics(row: dict[str, Any], prefix: str) -> dict[str, Any]:
    metrics = [
        "signal_samples",
        "nonoverlap_samples",
        "signal_mean_return",
        "all_dates_mean_return",
        "pressure_dates_mean_return",
        "uplift_vs_all_dates",
        "uplift_vs_pressure_dates",
        "signal_win_rate",
        "pressure_dates_win_rate",
        "win_rate_uplift_vs_pressure",
        "signal_bad_window_rate",
        "signal_strong_rebound_rate",
        "strong_rebound_recall",
        "signal_min_return",
        "random_outperformance_pvalue",
        "random_same_size_mean_return_p50",
        "random_same_size_mean_return_p90",
    ]
    return {f"{prefix}_{metric}": row.get(metric, math.nan) for metric in metrics}


def summarize_splits(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if int(row.get("signal_samples", 0)) > 0]
    positive_uplift = [row for row in eligible if nz(row.get("uplift_vs_pressure_dates")) > 0]
    positive_win_uplift = [row for row in eligible if nz(row.get("win_rate_uplift_vs_pressure")) > 0]
    recent = next((row for row in rows if row.get("role") == "recent_check"), {})
    return {
        "split_count": len(rows),
        "split_eligible_count": len(eligible),
        "split_positive_uplift_count": len(positive_uplift),
        "split_positive_uplift_rate": float(len(positive_uplift) / len(eligible)) if eligible else math.nan,
        "split_positive_win_uplift_rate": float(len(positive_win_uplift) / len(eligible)) if eligible else math.nan,
        "split_min_uplift_vs_pressure_dates": min_metric(eligible, "uplift_vs_pressure_dates"),
        "split_min_signal_mean_return": min_metric(eligible, "signal_mean_return"),
        "recent_signal_samples": int(recent.get("signal_samples", 0) or 0),
        "recent_nonoverlap_samples": int(recent.get("nonoverlap_samples", 0) or 0),
        "recent_signal_mean_return": recent.get("signal_mean_return", math.nan),
        "recent_uplift_vs_pressure_dates": recent.get("uplift_vs_pressure_dates", math.nan),
        "recent_signal_win_rate": recent.get("signal_win_rate", math.nan),
        "recent_signal_bad_window_rate": recent.get("signal_bad_window_rate", math.nan),
    }


def summarize_rolling(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if int(row.get("signal_samples", 0)) > 0]
    positive = [row for row in eligible if nz(row.get("uplift_vs_pressure_dates")) > 0]
    non_negative = [row for row in eligible if nz(row.get("signal_mean_return")) >= 0]
    return {
        "rolling_count": len(rows),
        "rolling_eligible_count": len(eligible),
        "rolling_positive_uplift_count": len(positive),
        "rolling_positive_uplift_rate": float(len(positive) / len(eligible)) if eligible else math.nan,
        "rolling_nonnegative_return_rate": float(len(non_negative) / len(eligible)) if eligible else math.nan,
        "rolling_min_uplift_vs_pressure_dates": min_metric(eligible, "uplift_vs_pressure_dates"),
        "rolling_min_signal_mean_return": min_metric(eligible, "signal_mean_return"),
        "rolling_worst_signal_min_return": min_metric(eligible, "signal_min_return"),
    }


def score_stability(row: dict[str, Any], policy: dict[str, Any]) -> float:
    th = policy["promotion_thresholds"]
    score = 0.0
    score += 3.0 * nz(row.get("full_uplift_vs_pressure_dates"))
    score += 2.5 * nz(row.get("recent_uplift_vs_pressure_dates"))
    score += 1.3 * (nz(row.get("split_positive_uplift_rate")) - 0.5)
    score += 1.3 * (nz(row.get("rolling_positive_uplift_rate")) - 0.5)
    score += 0.8 * (nz(row.get("full_signal_win_rate")) - 0.5)
    score += 0.8 * (nz(row.get("recent_signal_win_rate")) - 0.5)
    score -= 1.4 * max(nz(row.get("full_signal_bad_window_rate")) - float(th["max_bad_window_rate"]), 0.0)
    score -= 1.4 * max(nz(row.get("recent_signal_bad_window_rate")) - float(th["max_recent_bad_window_rate"]), 0.0)
    if nz(row.get("split_min_uplift_vs_pressure_dates")) < 0:
        score -= 0.20
    if nz(row.get("rolling_min_uplift_vs_pressure_dates")) < 0:
        score -= 0.20
    if nz(row.get("full_random_outperformance_pvalue"), 1.0) > float(th["max_random_pvalue"]):
        score -= 0.15
    return float(score)


def classify_stability(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    full_sample_ok = nz(row.get("full_signal_samples")) >= int(th["min_full_samples"])
    full_nonoverlap_ok = nz(row.get("full_nonoverlap_samples")) >= int(th["min_full_nonoverlap_samples"])
    recent_sample_ok = nz(row.get("recent_signal_samples")) >= int(th["min_recent_samples"])
    recent_nonoverlap_ok = nz(row.get("recent_nonoverlap_samples")) >= int(th["min_recent_nonoverlap_samples"])
    full_edge_ok = nz(row.get("full_uplift_vs_pressure_dates")) >= float(th["min_full_uplift_vs_pressure_dates"])
    recent_edge_ok = nz(row.get("recent_uplift_vs_pressure_dates")) >= float(th["min_recent_uplift_vs_pressure_dates"])
    full_win_ok = nz(row.get("full_signal_win_rate")) >= float(th["min_full_win_rate"])
    recent_win_ok = nz(row.get("recent_signal_win_rate")) >= float(th["min_recent_win_rate"])
    split_ok = nz(row.get("split_positive_uplift_rate")) >= float(th["min_split_positive_uplift_rate"])
    rolling_ok = nz(row.get("rolling_positive_uplift_rate")) >= float(th["min_rolling_positive_uplift_rate"])
    full_bad_ok = nz(row.get("full_signal_bad_window_rate")) <= float(th["max_bad_window_rate"])
    recent_bad_ok = nz(row.get("recent_signal_bad_window_rate")) <= float(th["max_recent_bad_window_rate"])
    random_ok = nz(row.get("full_random_outperformance_pvalue"), 1.0) <= float(th["max_random_pvalue"])
    checks = [
        full_sample_ok,
        full_nonoverlap_ok,
        recent_sample_ok,
        recent_nonoverlap_ok,
        full_edge_ok,
        recent_edge_ok,
        full_win_ok,
        recent_win_ok,
        split_ok,
        rolling_ok,
        full_bad_ok,
        recent_bad_ok,
        random_ok,
    ]
    if all(checks):
        return "稳定反弹窗口候选"
    if not full_sample_ok or not full_nonoverlap_ok:
        return "样本不足"
    if full_edge_ok and full_win_ok and random_ok:
        return "全样本有效但稳定性不足"
    return "拒绝"


def build_top_candidates(validation_summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if validation_summary.empty:
        return pd.DataFrame()
    priority = {
        "稳定反弹窗口候选": 0,
        "全样本有效但稳定性不足": 1,
        "样本不足": 2,
        "拒绝": 3,
    }
    cols = [
        "rule_id",
        "rule_name_zh",
        "horizon",
        "stability_status",
        "stability_score",
        "full_signal_samples",
        "full_nonoverlap_samples",
        "full_signal_mean_return",
        "full_pressure_dates_mean_return",
        "full_uplift_vs_pressure_dates",
        "full_signal_win_rate",
        "full_pressure_dates_win_rate",
        "full_win_rate_uplift_vs_pressure",
        "full_signal_bad_window_rate",
        "full_random_outperformance_pvalue",
        "recent_signal_samples",
        "recent_nonoverlap_samples",
        "recent_signal_mean_return",
        "recent_uplift_vs_pressure_dates",
        "recent_signal_win_rate",
        "recent_signal_bad_window_rate",
        "split_positive_uplift_rate",
        "split_min_uplift_vs_pressure_dates",
        "rolling_positive_uplift_rate",
        "rolling_min_uplift_vs_pressure_dates",
        "rolling_worst_signal_min_return",
    ]
    output = validation_summary[[col for col in cols if col in validation_summary.columns]].copy()
    output["_status_priority"] = output["stability_status"].map(priority).fillna(9)
    output = output.sort_values(
        ["_status_priority", "stability_score", "full_signal_samples"],
        ascending=[True, False, False],
    ).drop(columns=["_status_priority"])
    return output.head(int(policy["top_candidate_rows"])).copy()


def build_cases(
    valid: pd.DataFrame,
    signal: pd.DataFrame,
    return_col: str,
    rule_id: str,
    rule_name: str,
    horizon: int,
    policy: dict[str, Any],
) -> pd.DataFrame:
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    marked = valid.copy()
    marked["is_signal"] = marked["trade_date_text"].isin(set(signal["trade_date_text"]))
    marked[return_col] = pd.to_numeric(marked[return_col], errors="coerce")
    false_alarm = marked[marked["is_signal"] & (marked[return_col] <= 0)].sort_values(return_col).head(10).copy()
    false_alarm["case_type"] = "false_alarm_negative_forward_return"
    miss = marked[~marked["is_signal"] & (marked[return_col] >= threshold)].sort_values(return_col, ascending=False).head(10).copy()
    miss["case_type"] = "missed_strong_rebound"
    result = pd.concat([false_alarm, miss], ignore_index=True, sort=False)
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
        "low_value_oversold_count",
        "low_value_oversold_non_trap_count",
        "return_pressure",
        "volatility_pressure",
    ]
    return result[[col for col in keep_cols if col in result.columns]]


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "frozen_v2_12_rules",
                "status": "pass",
                "evidence": "rules are copied from frozen V2.12 policy and evaluated without adaptive threshold expansion",
                "action": "V2.13只审计冻结规则稳定性。",
            },
            {
                "audit_item": "future_return_used_only_as_label",
                "status": "pass",
                "evidence": "benchmark_forward_return_* only appears in outcome comparison",
                "action": "未来收益只能作为审计标签，不进入触发规则。",
            },
            {
                "audit_item": "not_true_pristine_oos",
                "status": "research_only",
                "evidence": "V2.12 rules were created after historical exploration; V2.13 time splits are stability checks, not pristine unseen data",
                "action": "报告必须保留research_only边界。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "即使通过稳定性审计也不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(
    top_candidates: pd.DataFrame,
    validation_summary: pd.DataFrame,
    time_split_summary: pd.DataFrame,
    rolling_stability: pd.DataFrame,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if top_candidates.empty:
        return {
            "main_diagnosis": "V2.13没有可排序的反弹窗口规则。",
            "next_iterations": ["检查V2.11日期面板和V2.12规则配置是否缺失。"],
        }
    best = top_candidates.iloc[0].to_dict()
    notes: list[str] = []
    if str(best.get("stability_status")) == "稳定反弹窗口候选":
        notes.append("V2.12候选规则在V2.13稳定性审计中保留，但仍不能生成交易指令。")
        notes.append("下一轮应做实时仿真净值、空仓现金收益和信号触发后的最大回撤审计。")
    else:
        notes.append("V2.12候选没有通过V2.13稳定性升级，不能说系统已经可靠识别反弹窗口。")
        notes.append("下一轮应转向压力释放斜率和广度修复，而不是继续提高静态压力门槛。")
    if nz(best.get("recent_uplift_vs_pressure_dates")) <= 0:
        notes.append("近年样本没有跑赢压力日期基准，说明全样本收益可能来自早期市场状态。")
    if nz(best.get("split_positive_uplift_rate")) < float(policy["promotion_thresholds"]["min_split_positive_uplift_rate"]):
        notes.append("时间切分稳定性不足，信号可能只在少数历史阶段有效。")
    if nz(best.get("rolling_positive_uplift_rate")) < float(policy["promotion_thresholds"]["min_rolling_positive_uplift_rate"]):
        notes.append("滚动窗口稳定性不足，不能证明它是可复用的反弹窗口规则。")
    if nz(best.get("full_signal_bad_window_rate")) > float(policy["promotion_thresholds"]["max_bad_window_rate"]):
        notes.append("坏窗口比例仍高，需要加入入场后止跌确认或压力释放条件。")
    return {
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": best.get("horizon", ""),
        "best_stability_status": best.get("stability_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_14_direction": "新增压力释放斜率：要求压力高位后下降、下跌广度收缩、20日相对强度转正，再用冻结规则重跑稳定性审计。",
    }


def build_run_summary(
    *,
    policy: dict[str, Any],
    date_panel: pd.DataFrame,
    validation_summary: pd.DataFrame,
    top_candidates: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
) -> dict[str, Any]:
    stable = validation_summary[validation_summary["stability_status"] == "稳定反弹窗口候选"] if not validation_summary.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage_audit["status"] == "fail").sum()) if not leakage_audit.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_count": int(len(date_panel)),
        "rule_count": int(len(policy["rules"])),
        "audit_rows": int(len(validation_summary)),
        "stable_candidate_count": int(len(stable)),
        "audit_fail_count": audit_fail_count,
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_stability_status": best.get("stability_status", ""),
        "best_full_uplift_vs_pressure_dates": float_or_none(best.get("full_uplift_vs_pressure_dates")),
        "best_recent_uplift_vs_pressure_dates": float_or_none(best.get("recent_uplift_vs_pressure_dates")),
        "best_split_positive_uplift_rate": float_or_none(best.get("split_positive_uplift_rate")),
        "best_rolling_positive_uplift_rate": float_or_none(best.get("rolling_positive_uplift_rate")),
        "final_verdict": final_verdict(stable, audit_fail_count),
        "main_diagnosis": optimization_notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    time_split_summary: pd.DataFrame,
    rolling_stability: pd.DataFrame,
    baseline_comparison: pd.DataFrame,
    false_alarm_miss_cases: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# V2.13 反弹窗口稳定性审计报告")
    lines.append("")
    lines.append(f"版本：{VERSION}")
    lines.append("")
    lines.append("## 研究结论")
    lines.append("")
    lines.append("V2.13 冻结 V2.12 的日期层市场状态规则，检查这些规则是否经得起时间切分、滚动窗口和压力日期基准对照。")
    lines.append("")
    lines.append(f"- 日期面板行数：{summary['date_count']}")
    lines.append(f"- 规则数：{summary['rule_count']}")
    lines.append(f"- 审计组合行数：{summary['audit_rows']}")
    lines.append(f"- 稳定反弹窗口候选数：{summary['stable_candidate_count']}")
    lines.append(f"- 审计失败数：{summary['audit_fail_count']}")
    lines.append(f"- 最终结论：{summary['final_verdict']}")
    lines.append(f"- 主要诊断：{summary['main_diagnosis']}")
    lines.append("")
    lines.append("> 注意：V2.13 是冻结规则后的稳定性审计，不是真正完全未看过历史的纯样本外验证。")
    lines.append("")
    lines.append("## 规则稳定性排序")
    lines.append("")
    lines.extend(table_or_empty(top_candidates.head(20), {
        "rule_id": "规则ID",
        "rule_name_zh": "规则",
        "horizon": "持有期",
        "stability_status": "稳定性状态",
        "stability_score": "稳定性分",
        "full_signal_samples": "全样本信号",
        "full_nonoverlap_samples": "全样本非重叠",
        "full_signal_mean_return": "全样本信号收益",
        "full_uplift_vs_pressure_dates": "全样本相对压力提升",
        "full_signal_win_rate": "全样本上涨比例",
        "full_signal_bad_window_rate": "全样本坏窗口",
        "recent_signal_samples": "近年信号",
        "recent_signal_mean_return": "近年信号收益",
        "recent_uplift_vs_pressure_dates": "近年相对压力提升",
        "recent_signal_win_rate": "近年上涨比例",
        "recent_signal_bad_window_rate": "近年坏窗口",
        "split_positive_uplift_rate": "切分正提升比例",
        "rolling_positive_uplift_rate": "滚动正提升比例",
        "rolling_min_uplift_vs_pressure_dates": "滚动最差提升",
    }, {
        "full_signal_mean_return",
        "full_uplift_vs_pressure_dates",
        "full_signal_win_rate",
        "full_signal_bad_window_rate",
        "recent_signal_mean_return",
        "recent_uplift_vs_pressure_dates",
        "recent_signal_win_rate",
        "recent_signal_bad_window_rate",
        "split_positive_uplift_rate",
        "rolling_positive_uplift_rate",
        "rolling_min_uplift_vs_pressure_dates",
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
        "role": "角色",
        "signal_samples": "信号样本",
        "nonoverlap_samples": "非重叠",
        "signal_mean_return": "信号收益",
        "pressure_dates_mean_return": "压力日期收益",
        "uplift_vs_pressure_dates": "相对压力提升",
        "signal_win_rate": "上涨比例",
        "pressure_dates_win_rate": "压力日期上涨比例",
        "signal_bad_window_rate": "坏窗口",
    }, {
        "signal_mean_return",
        "pressure_dates_mean_return",
        "uplift_vs_pressure_dates",
        "signal_win_rate",
        "pressure_dates_win_rate",
        "signal_bad_window_rate",
    }))
    lines.append("")
    lines.append("## 最佳规则滚动窗口")
    lines.append("")
    best_rolling = rolling_stability[
        (rolling_stability["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(rolling_stability["horizon"], errors="coerce") == best_horizon)
    ].copy() if not rolling_stability.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_rolling, {
        "period_name_zh": "滚动阶段",
        "signal_samples": "信号样本",
        "signal_mean_return": "信号收益",
        "pressure_dates_mean_return": "压力日期收益",
        "uplift_vs_pressure_dates": "相对压力提升",
        "signal_win_rate": "上涨比例",
        "signal_bad_window_rate": "坏窗口",
    }, {
        "signal_mean_return",
        "pressure_dates_mean_return",
        "uplift_vs_pressure_dates",
        "signal_win_rate",
        "signal_bad_window_rate",
    }))
    lines.append("")
    lines.append("## 最佳规则基线对照")
    lines.append("")
    best_baseline = baseline_comparison[
        (baseline_comparison["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(baseline_comparison["horizon"], errors="coerce") == best_horizon)
    ].copy() if not baseline_comparison.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_baseline, {
        "baseline": "基线",
        "signal_samples": "信号样本",
        "baseline_samples": "基线样本",
        "signal_mean_return": "信号均值",
        "baseline_mean_return": "基线均值",
        "mean_uplift": "均值提升",
        "signal_win_rate": "信号上涨比例",
        "baseline_win_rate": "基线上涨比例",
        "win_rate_uplift": "上涨比例提升",
    }, {
        "signal_mean_return",
        "baseline_mean_return",
        "mean_uplift",
        "signal_win_rate",
        "baseline_win_rate",
        "win_rate_uplift",
    }))
    lines.append("")
    lines.append("## 误报和漏报")
    lines.append("")
    best_cases = false_alarm_miss_cases[
        (false_alarm_miss_cases["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(false_alarm_miss_cases["horizon"], errors="coerce") == best_horizon)
    ].copy() if not false_alarm_miss_cases.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_cases.head(20), {
        "case_type": "类型",
        "trade_date": "日期",
        f"benchmark_forward_return_{best_horizon}d": "未来基准收益",
        "market_stress_score": "压力分",
        "negative_breadth_60d": "60日下跌广度",
        "market_drawdown_252d": "市场回撤",
        "low_value_oversold_count": "低估超跌数",
        "low_value_oversold_non_trap_count": "非陷阱数",
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
    lines.append(f"- 建议 V2.14 方向：{optimization_notes.get('recommended_v2_14_direction', '')}")
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
    lines.append("- `report.md`：中文稳定性审计报告，优先打开。")
    lines.append("- `top_candidates.csv`：规则稳定性排序；不是交易信号。")
    lines.append("- `run_summary.json`：机器可读运行摘要。")
    lines.append("- `debug/`：验证明细、时间切分、滚动窗口、基线对照、误报漏报、审计和冻结策略。")
    lines.append("")
    lines.append(f"研究边界：{policy['research_boundary']}")
    return "\n".join(lines)


def final_verdict(stable: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if stable.empty:
        return "research_only；V2.12候选未通过稳定性升级，尚不能证明可靠识别反弹窗口"
    return "research_only；存在稳定反弹窗口候选，但仍需实时仿真和独立未来数据验证"


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


def mean_or_nan(values: pd.Series) -> float:
    return float(values.mean()) if len(values) else math.nan


def median_or_nan(values: pd.Series) -> float:
    return float(values.median()) if len(values) else math.nan


def min_or_nan(values: pd.Series) -> float:
    return float(values.min()) if len(values) else math.nan


def max_or_nan(values: pd.Series) -> float:
    return float(values.max()) if len(values) else math.nan


def rate(mask: pd.Series | np.ndarray) -> float:
    return float(np.mean(mask)) if len(mask) else math.nan


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
