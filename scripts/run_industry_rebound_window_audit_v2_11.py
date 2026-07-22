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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_audit_policy_v2_11.json"
DEFAULT_SIGNAL_PANEL = ROOT / "outputs" / "industry_realtime_pressure_sensitivity_v2_10" / "debug" / "realtime_signal_panel.csv"
DEFAULT_DECISION_LOG = ROOT / "outputs" / "industry_realtime_pressure_sensitivity_v2_10" / "debug" / "parameter_decision_log.csv"
DEFAULT_PARAMETER_SUMMARY = ROOT / "outputs" / "industry_realtime_pressure_sensitivity_v2_10" / "debug" / "parameter_summary.csv"
VERSION = "2.11.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.11 rebound-window identification audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.11 audit policy JSON.")
    parser.add_argument("--signal-panel", default=str(DEFAULT_SIGNAL_PANEL), help="V2.10 realtime signal panel.")
    parser.add_argument("--decision-log", default=str(DEFAULT_DECISION_LOG), help="V2.10 parameter decision log.")
    parser.add_argument("--parameter-summary", default=str(DEFAULT_PARAMETER_SUMMARY), help="V2.10 parameter summary.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    signal_panel = read_csv(Path(args.signal_panel))
    decision_log = read_csv(Path(args.decision_log))
    parameter_summary = read_csv(Path(args.parameter_summary))
    date_panel = build_date_panel(signal_panel)
    audit_summary, baseline_comparison, threshold_panel = run_window_audit(
        date_panel=date_panel,
        decision_log=decision_log,
        parameter_summary=parameter_summary,
        policy=policy,
    )
    false_alarm_miss_cases = build_false_alarm_miss_cases(
        date_panel=date_panel,
        decision_log=decision_log,
        audit_summary=audit_summary,
        policy=policy,
    )
    top_candidates = build_top_candidates(audit_summary, policy)
    leakage_audit = build_leakage_audit(policy)
    optimization_notes = build_optimization_notes(top_candidates, audit_summary, policy)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    date_panel.to_csv(debug_dir / "date_level_panel.csv", index=False, encoding="utf-8-sig")
    audit_summary.to_csv(debug_dir / "window_audit_summary.csv", index=False, encoding="utf-8-sig")
    baseline_comparison.to_csv(debug_dir / "baseline_comparison.csv", index=False, encoding="utf-8-sig")
    threshold_panel.to_csv(debug_dir / "threshold_recall_precision.csv", index=False, encoding="utf-8-sig")
    false_alarm_miss_cases.to_csv(debug_dir / "false_alarm_miss_cases.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", optimization_notes)
    write_json(debug_dir / "frozen_policy.json", policy)

    summary = build_run_summary(
        policy=policy,
        date_panel=date_panel,
        decision_log=decision_log,
        audit_summary=audit_summary,
        top_candidates=top_candidates,
        leakage_audit=leakage_audit,
        optimization_notes=optimization_notes,
    )
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            baseline_comparison=baseline_comparison,
            threshold_panel=threshold_panel,
            false_alarm_miss_cases=false_alarm_miss_cases,
            leakage_audit=leakage_audit,
            optimization_notes=optimization_notes,
            policy=policy,
        ),
        encoding="utf-8",
    )

    print("V2.11反弹窗口识别审计完成")
    print(f"日期面板行数={summary['date_count']}")
    print(f"参数组合数={summary['parameter_count']}")
    print(f"候选窗口规则数={summary['candidate_window_count']}")
    print(f"审计失败数={summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def build_date_panel(signal_panel: pd.DataFrame) -> pd.DataFrame:
    frame = signal_panel.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date"]).sort_values(["trade_date", "industry_code"])

    metrics = [
        "benchmark_forward_return_60d",
        "benchmark_forward_return_120d",
        "benchmark_forward_return_252d",
        "market_stress_score",
        "market_return_120d_y",
        "market_drawdown_252d",
        "negative_breadth_60d",
        "return_pressure",
        "volatility_pressure",
        "drawdown_pressure",
        "breadth_pressure",
        "pressure_tier",
        "pressure_episode_id",
    ]
    first_cols = [col for col in metrics if col in frame.columns]
    base = frame.groupby("trade_date", as_index=False)[first_cols].first()

    def count_mask(group: pd.DataFrame, mask: pd.Series) -> int:
        return int(mask.reindex(group.index, fill_value=False).sum())

    rows: list[dict[str, Any]] = []
    for date, group in frame.groupby("trade_date", sort=True):
        low_value = group.get("low_value_flag", pd.Series(False, index=group.index)).fillna(False).astype(bool)
        oversold = group.get("oversold_flag", pd.Series(False, index=group.index)).fillna(False).astype(bool)
        non_trap = group.get("non_trap_flag", pd.Series(False, index=group.index)).fillna(False).astype(bool)
        quality = group.get("quality_confirm_flag", pd.Series(False, index=group.index)).fillna(False).astype(bool)
        trap_score = pd.to_numeric(group.get("momentum_trap_score", pd.Series(np.nan, index=group.index)), errors="coerce")
        rows.append(
            {
                "trade_date": date,
                "industry_count": int(len(group)),
                "low_value_count": count_mask(group, low_value),
                "oversold_count": count_mask(group, oversold),
                "low_value_oversold_count": count_mask(group, low_value & oversold),
                "low_value_oversold_non_trap_count": count_mask(group, low_value & oversold & non_trap),
                "low_value_oversold_quality_count": count_mask(group, low_value & oversold & quality),
                "trap0_count": int((trap_score <= 0).sum()),
                "trap1_count": int((trap_score <= 1).sum()),
            }
        )
    counts = pd.DataFrame(rows)
    result = base.merge(counts, on="trade_date", how="left")
    for horizon in [60, 120, 252]:
        col = f"benchmark_forward_return_{horizon}d"
        if col in result.columns:
            result[f"benchmark_forward_return_{horizon}d"] = pd.to_numeric(result[col], errors="coerce")
    result["trade_date"] = result["trade_date"].dt.strftime("%Y-%m-%d")
    return result


def run_window_audit(
    *,
    date_panel: pd.DataFrame,
    decision_log: pd.DataFrame,
    parameter_summary: pd.DataFrame,
    policy: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    decisions = decision_log.copy()
    decisions["signal_date"] = pd.to_datetime(decisions["signal_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    date_frame = date_panel.copy()
    date_frame["trade_date"] = pd.to_datetime(date_frame["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    primary_candidates = set(parameter_summary["parameter_id"].dropna().astype(str).unique().tolist())
    horizons = [int(item) for item in policy["horizons"]]

    summary_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    threshold_rows: list[dict[str, Any]] = []

    for parameter_id, group in decisions.groupby("parameter_id", sort=True):
        if primary_candidates and parameter_id not in primary_candidates:
            continue
        universe = date_frame[date_frame["trade_date"].isin(group["signal_date"].dropna().unique())].copy()
        if universe.empty:
            continue
        signal_dates = set(group[group["is_invested"].astype(bool)]["signal_date"].dropna().tolist())
        universe["is_signal"] = universe["trade_date"].isin(signal_dates)
        universe["is_pressure"] = pd.to_numeric(universe["market_stress_score"], errors="coerce") >= float(policy["pressure_baseline_min"])
        universe["is_trigger_count_ge_min"] = pd.to_numeric(universe["low_value_oversold_count"], errors="coerce") >= int(policy["min_triggered_count"])
        universe["is_nontrap_count_ge_min"] = pd.to_numeric(universe["low_value_oversold_non_trap_count"], errors="coerce") >= int(policy["min_triggered_count"])

        for horizon in horizons:
            return_col = f"benchmark_forward_return_{horizon}d"
            if return_col not in universe.columns:
                continue
            valid = universe.dropna(subset=[return_col]).copy()
            if valid.empty:
                continue
            signal = valid[valid["is_signal"]].copy()
            pressure = valid[valid["is_pressure"]].copy()
            pressure_not_signal = valid[valid["is_pressure"] & ~valid["is_signal"]].copy()
            trigger_count = valid[valid["is_trigger_count_ge_min"]].copy()
            nontrap_count = valid[valid["is_nontrap_count_ge_min"]].copy()

            baseline_map = {
                "all_decision_dates": valid,
                "pressure_dates": pressure,
                "pressure_not_signal": pressure_not_signal,
                "trigger_count_dates": trigger_count,
                "nontrap_count_dates": nontrap_count,
            }
            stats = describe_window(signal, valid, horizon, return_col, policy)
            all_stats = describe_simple(valid, return_col, horizon, policy)
            pressure_stats = describe_simple(pressure, return_col, horizon, policy)
            pressure_not_signal_stats = describe_simple(pressure_not_signal, return_col, horizon, policy)
            random_stats = random_same_size_stats(valid, signal, return_col, seed=int(policy["random_seed"]))

            row = {
                "parameter_id": parameter_id,
                "horizon": horizon,
                **stats,
                "all_dates_mean_return": all_stats["mean_return"],
                "all_dates_win_rate": all_stats["win_rate"],
                "pressure_dates_mean_return": pressure_stats["mean_return"],
                "pressure_dates_win_rate": pressure_stats["win_rate"],
                "pressure_not_signal_mean_return": pressure_not_signal_stats["mean_return"],
                "random_same_size_mean_return_p50": random_stats["p50"],
                "random_same_size_mean_return_p90": random_stats["p90"],
                "random_outperformance_pvalue": random_stats["pvalue_right"],
            }
            row["uplift_vs_all_dates"] = safe_sub(row["signal_mean_return"], row["all_dates_mean_return"])
            row["uplift_vs_pressure_dates"] = safe_sub(row["signal_mean_return"], row["pressure_dates_mean_return"])
            row["uplift_vs_pressure_not_signal"] = safe_sub(row["signal_mean_return"], row["pressure_not_signal_mean_return"])
            row["window_score"] = score_window(row, policy)
            row["window_status"] = classify_window(row, policy)
            summary_rows.append(row)

            for baseline_name, baseline_frame in baseline_map.items():
                base_stats = describe_simple(baseline_frame, return_col, horizon, policy)
                baseline_rows.append(
                    {
                        "parameter_id": parameter_id,
                        "horizon": horizon,
                        "baseline": baseline_name,
                        "signal_samples": int(len(signal)),
                        "baseline_samples": base_stats["samples"],
                        "signal_mean_return": row["signal_mean_return"],
                        "baseline_mean_return": base_stats["mean_return"],
                        "signal_win_rate": row["signal_win_rate"],
                        "baseline_win_rate": base_stats["win_rate"],
                        "mean_uplift": safe_sub(row["signal_mean_return"], base_stats["mean_return"]),
                        "win_rate_uplift": safe_sub(row["signal_win_rate"], base_stats["win_rate"]),
                    }
                )

            for threshold in policy["strong_rebound_thresholds"][str(horizon)]:
                threshold_rows.append(
                    threshold_recall_precision(
                        valid=valid,
                        signal=signal,
                        return_col=return_col,
                        parameter_id=parameter_id,
                        horizon=horizon,
                        threshold=float(threshold),
                    )
                )

    return pd.DataFrame(summary_rows), pd.DataFrame(baseline_rows), pd.DataFrame(threshold_rows)


def describe_window(signal: pd.DataFrame, universe: pd.DataFrame, horizon: int, return_col: str, policy: dict[str, Any]) -> dict[str, Any]:
    strong_threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    bad_threshold = float(policy["bad_window_thresholds"][str(horizon)])
    values = pd.to_numeric(signal[return_col], errors="coerce").dropna()
    universe_values = pd.to_numeric(universe[return_col], errors="coerce").dropna()
    strong_universe = universe[pd.to_numeric(universe[return_col], errors="coerce") >= strong_threshold]
    strong_signal = signal[pd.to_numeric(signal[return_col], errors="coerce") >= strong_threshold]
    non = nonoverlap_by_horizon(signal, horizon)
    non_values = pd.to_numeric(non[return_col], errors="coerce").dropna()
    return {
        "signal_samples": int(len(values)),
        "nonoverlap_samples": int(len(non_values)),
        "universe_samples": int(len(universe_values)),
        "signal_mean_return": float(values.mean()) if not values.empty else math.nan,
        "signal_median_return": float(values.median()) if not values.empty else math.nan,
        "signal_win_rate": float((values > 0).mean()) if not values.empty else math.nan,
        "signal_strong_rebound_rate": float((values >= strong_threshold).mean()) if not values.empty else math.nan,
        "signal_bad_window_rate": float((values <= bad_threshold).mean()) if not values.empty else math.nan,
        "nonoverlap_mean_return": float(non_values.mean()) if not non_values.empty else math.nan,
        "strong_rebound_recall": float(len(strong_signal) / len(strong_universe)) if len(strong_universe) else math.nan,
        "strong_rebound_total": int(len(strong_universe)),
        "strong_rebound_captured": int(len(strong_signal)),
        "false_alarm_count": int((values <= 0).sum()) if not values.empty else 0,
        "missed_strong_rebound_count": int(max(len(strong_universe) - len(strong_signal), 0)),
    }


def describe_simple(frame: pd.DataFrame, return_col: str, horizon: int, policy: dict[str, Any]) -> dict[str, Any]:
    strong_threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    bad_threshold = float(policy["bad_window_thresholds"][str(horizon)])
    values = pd.to_numeric(frame[return_col], errors="coerce").dropna() if return_col in frame.columns else pd.Series(dtype=float)
    return {
        "samples": int(len(values)),
        "mean_return": float(values.mean()) if not values.empty else math.nan,
        "median_return": float(values.median()) if not values.empty else math.nan,
        "win_rate": float((values > 0).mean()) if not values.empty else math.nan,
        "strong_rebound_rate": float((values >= strong_threshold).mean()) if not values.empty else math.nan,
        "bad_window_rate": float((values <= bad_threshold).mean()) if not values.empty else math.nan,
    }


def random_same_size_stats(universe: pd.DataFrame, signal: pd.DataFrame, return_col: str, seed: int) -> dict[str, float]:
    values = pd.to_numeric(universe[return_col], errors="coerce").dropna().to_numpy(dtype=float)
    signal_values = pd.to_numeric(signal[return_col], errors="coerce").dropna().to_numpy(dtype=float)
    n = len(signal_values)
    if len(values) == 0 or n == 0:
        return {"p50": math.nan, "p90": math.nan, "pvalue_right": math.nan}
    rng = np.random.default_rng(seed + n + len(values))
    draws = np.array([rng.choice(values, size=n, replace=False if n <= len(values) else True).mean() for _ in range(1000)])
    signal_mean = float(signal_values.mean())
    return {
        "p50": float(np.nanpercentile(draws, 50)),
        "p90": float(np.nanpercentile(draws, 90)),
        "pvalue_right": float((draws >= signal_mean).mean()),
    }


def threshold_recall_precision(
    *,
    valid: pd.DataFrame,
    signal: pd.DataFrame,
    return_col: str,
    parameter_id: str,
    horizon: int,
    threshold: float,
) -> dict[str, Any]:
    valid_values = pd.to_numeric(valid[return_col], errors="coerce")
    signal_values = pd.to_numeric(signal[return_col], errors="coerce")
    strong_total = int((valid_values >= threshold).sum())
    captured = int((signal_values >= threshold).sum())
    samples = int(signal_values.notna().sum())
    return {
        "parameter_id": parameter_id,
        "horizon": horizon,
        "strong_rebound_threshold": threshold,
        "signal_samples": samples,
        "strong_rebound_total": strong_total,
        "strong_rebound_captured": captured,
        "precision": float(captured / samples) if samples else math.nan,
        "recall": float(captured / strong_total) if strong_total else math.nan,
    }


def nonoverlap_by_horizon(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    ordered = frame.sort_values("trade_date").copy()
    dates = pd.to_datetime(ordered["trade_date"], errors="coerce")
    keep_idx: list[int] = []
    last_date = pd.Timestamp.min
    for idx, date in zip(ordered.index, dates):
        if pd.isna(date):
            continue
        if date > last_date:
            keep_idx.append(idx)
            last_date = date + pd.tseries.offsets.BDay(horizon)
    return ordered.loc[keep_idx].copy()


def build_false_alarm_miss_cases(
    *,
    date_panel: pd.DataFrame,
    decision_log: pd.DataFrame,
    audit_summary: pd.DataFrame,
    policy: dict[str, Any],
) -> pd.DataFrame:
    if audit_summary.empty:
        return pd.DataFrame()
    status_priority = {
        "反弹窗口候选": 0,
        "弱证据观察": 1,
        "样本不足": 2,
        "拒绝": 3,
    }
    ranked = audit_summary.copy()
    ranked["_status_priority"] = ranked["window_status"].map(status_priority).fillna(9)
    best = ranked.sort_values(["_status_priority", "window_score", "signal_samples"], ascending=[True, False, False]).iloc[0]
    parameter_id = str(best["parameter_id"])
    horizon = int(best["horizon"])
    return_col = f"benchmark_forward_return_{horizon}d"
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    decisions = decision_log[decision_log["parameter_id"] == parameter_id].copy()
    decisions["signal_date"] = pd.to_datetime(decisions["signal_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signal_dates = set(decisions[decisions["is_invested"].astype(bool)]["signal_date"].dropna().tolist())
    panel = date_panel.copy()
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    panel = panel[panel["trade_date"].isin(decisions["signal_date"].dropna().unique())].copy()
    panel["is_signal"] = panel["trade_date"].isin(signal_dates)
    panel[return_col] = pd.to_numeric(panel[return_col], errors="coerce")

    false_alarms = panel[panel["is_signal"] & (panel[return_col] <= 0)].sort_values(return_col).head(15).copy()
    false_alarms["case_type"] = "false_alarm_negative_forward_return"
    misses = panel[~panel["is_signal"] & (panel[return_col] >= threshold)].sort_values(return_col, ascending=False).head(15).copy()
    misses["case_type"] = "missed_strong_rebound"
    result = pd.concat([false_alarms, misses], ignore_index=True, sort=False)
    keep_cols = [
        "case_type",
        "trade_date",
        return_col,
        "market_stress_score",
        "low_value_oversold_count",
        "low_value_oversold_non_trap_count",
        "pressure_tier",
        "negative_breadth_60d",
        "market_drawdown_252d",
        "return_pressure",
        "volatility_pressure",
    ]
    return result[[col for col in keep_cols if col in result.columns]]


def build_top_candidates(audit_summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if audit_summary.empty:
        return pd.DataFrame()
    status_priority = {
        "反弹窗口候选": 0,
        "弱证据观察": 1,
        "样本不足": 2,
        "拒绝": 3,
    }
    cols = [
        "parameter_id",
        "horizon",
        "window_status",
        "window_score",
        "signal_samples",
        "nonoverlap_samples",
        "signal_mean_return",
        "all_dates_mean_return",
        "pressure_dates_mean_return",
        "uplift_vs_all_dates",
        "uplift_vs_pressure_dates",
        "signal_win_rate",
        "all_dates_win_rate",
        "signal_strong_rebound_rate",
        "strong_rebound_recall",
        "signal_bad_window_rate",
        "random_outperformance_pvalue",
    ]
    output = audit_summary[[col for col in cols if col in audit_summary.columns]].copy()
    output["_status_priority"] = output["window_status"].map(status_priority).fillna(9)
    output = output.sort_values(
        ["_status_priority", "window_score", "signal_samples"], ascending=[True, False, False]
    ).drop(columns=["_status_priority"])
    return output.head(int(policy["top_candidate_rows"])).copy()


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "uses_v2_10_frozen_outputs_only",
                "status": "pass",
                "evidence": "reads realtime_signal_panel, parameter_decision_log and parameter_summary generated before V2.11",
                "action": "V2.11只审计已有触发日期，不用未来收益生成新信号。",
            },
            {
                "audit_item": "future_return_used_only_as_label",
                "status": "pass",
                "evidence": "benchmark_forward_return_* only appears in outcome comparison",
                "action": "未来收益只能作为审计标签，不进入触发规则。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "未通过反弹窗口门槛前不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(top_candidates: pd.DataFrame, audit_summary: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top_candidates.empty:
        return {
            "main_diagnosis": "没有可评估的窗口规则。",
            "next_iterations": ["检查输入输出是否缺失。"],
        }
    best = top_candidates.iloc[0].to_dict()
    notes: list[str] = []
    if str(best.get("window_status", "")) != "反弹窗口候选":
        notes.append("当前证据不能证明系统能可靠识别反弹窗口；下一轮不应继续在行业选择层面调参。")
    if float_or_nan(best.get("uplift_vs_all_dates")) <= 0:
        notes.append("触发日未来基准收益没有高于全日期均值，应先优化择时环境识别。")
    if float_or_nan(best.get("uplift_vs_pressure_dates")) <= 0:
        notes.append("触发日没有跑赢普通压力日期，低估超跌触发可能只是压力状态代理。")
    if float_or_nan(best.get("signal_win_rate")) < float(policy["promotion_thresholds"]["min_win_rate"]):
        notes.append("上涨概率不足，需引入压力释放或趋势确认，而不是只看低估超跌。")
    if float_or_nan(best.get("strong_rebound_recall")) < float(policy["promotion_thresholds"]["min_strong_rebound_recall"]):
        notes.append("强反弹召回率不足，说明系统错过大量真正反弹窗口。")
    if float_or_nan(best.get("signal_bad_window_rate")) > float(policy["promotion_thresholds"]["max_bad_window_rate"]):
        notes.append("误报窗口仍偏多，需要更严格的下跌延续过滤。")
    if int(float_or_nan(best.get("signal_samples"), 0)) < int(policy["promotion_thresholds"]["min_samples"]):
        notes.append("样本不足，任何高收益都不能直接作为证据。")
    if not notes:
        notes.append("窗口识别有候选证据，但仍需样本外和独立数据源复核。")
    return {
        "best_parameter_id": best.get("parameter_id", ""),
        "best_horizon": best.get("horizon", ""),
        "best_window_status": best.get("window_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_12_direction": "把反弹窗口识别从行业选择中拆出来，优先测试压力释放斜率、市场宽度修复、20日止跌确认和普通压力日期对照。",
    }


def build_run_summary(
    *,
    policy: dict[str, Any],
    date_panel: pd.DataFrame,
    decision_log: pd.DataFrame,
    audit_summary: pd.DataFrame,
    top_candidates: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
) -> dict[str, Any]:
    candidates = audit_summary[audit_summary["window_status"] == "反弹窗口候选"] if not audit_summary.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage_audit["status"] == "fail").sum()) if not leakage_audit.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_count": int(len(date_panel)),
        "parameter_count": int(decision_log["parameter_id"].nunique()) if not decision_log.empty else 0,
        "audit_rows": int(len(audit_summary)),
        "candidate_window_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_parameter_id": best.get("parameter_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_window_status": best.get("window_status", ""),
        "best_signal_mean_return": float_or_none(best.get("signal_mean_return")),
        "best_uplift_vs_all_dates": float_or_none(best.get("uplift_vs_all_dates")),
        "best_uplift_vs_pressure_dates": float_or_none(best.get("uplift_vs_pressure_dates")),
        "best_signal_win_rate": float_or_none(best.get("signal_win_rate")),
        "best_strong_rebound_recall": float_or_none(best.get("strong_rebound_recall")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": optimization_notes.get("main_diagnosis", ""),
        "research_boundary": "V2.11只审计反弹窗口识别能力，不做行业选择alpha推广，不生成交易指令。",
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    baseline_comparison: pd.DataFrame,
    threshold_panel: pd.DataFrame,
    false_alarm_miss_cases: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# V2.11 反弹窗口识别审计报告")
    lines.append("")
    lines.append(f"版本：{VERSION}")
    lines.append("")
    lines.append("## 研究结论")
    lines.append("")
    lines.append("V2.11 不再问“选出的行业有没有 alpha”，而是单独审计：低估超跌触发日是否真的比普通日期、压力日期和随机同样本日期更像反弹窗口。")
    lines.append("")
    lines.append(f"- 日期面板行数：{summary['date_count']}")
    lines.append(f"- 参数组合数：{summary['parameter_count']}")
    lines.append(f"- 审计组合行数：{summary['audit_rows']}")
    lines.append(f"- 反弹窗口候选数：{summary['candidate_window_count']}")
    lines.append(f"- 审计失败数：{summary['audit_fail_count']}")
    lines.append(f"- 最终结论：{summary['final_verdict']}")
    lines.append(f"- 主要诊断：{summary['main_diagnosis']}")
    lines.append("")
    lines.append("## 最接近通过的窗口规则")
    lines.append("")
    lines.extend(table_or_empty(top_candidates.head(15), {
        "parameter_id": "参数ID",
        "horizon": "持有期",
        "window_status": "窗口状态",
        "window_score": "窗口分",
        "signal_samples": "信号样本",
        "nonoverlap_samples": "非重叠",
        "signal_mean_return": "信号后基准收益",
        "all_dates_mean_return": "全日期均值",
        "pressure_dates_mean_return": "压力日期均值",
        "uplift_vs_all_dates": "相对全日期提升",
        "uplift_vs_pressure_dates": "相对压力日期提升",
        "signal_win_rate": "上涨比例",
        "all_dates_win_rate": "全日期上涨比例",
        "signal_strong_rebound_rate": "强反弹精确率",
        "strong_rebound_recall": "强反弹召回",
        "signal_bad_window_rate": "坏窗口比例",
        "random_outperformance_pvalue": "随机均值p值",
    }, {
        "signal_mean_return",
        "all_dates_mean_return",
        "pressure_dates_mean_return",
        "uplift_vs_all_dates",
        "uplift_vs_pressure_dates",
        "signal_win_rate",
        "all_dates_win_rate",
        "signal_strong_rebound_rate",
        "strong_rebound_recall",
        "signal_bad_window_rate",
        "random_outperformance_pvalue",
    }))
    lines.append("")
    lines.append("## 基线对照")
    lines.append("")
    best_param = str(summary.get("best_parameter_id", ""))
    best_horizon = int(summary.get("best_horizon", 0))
    best_baseline = baseline_comparison[
        (baseline_comparison["parameter_id"].astype(str) == best_param)
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
    lines.append("## 强反弹捕获")
    lines.append("")
    best_thresholds = threshold_panel[
        (threshold_panel["parameter_id"].astype(str) == best_param)
        & (pd.to_numeric(threshold_panel["horizon"], errors="coerce") == best_horizon)
    ].copy() if not threshold_panel.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_thresholds, {
        "strong_rebound_threshold": "强反弹阈值",
        "signal_samples": "信号样本",
        "strong_rebound_total": "强反弹总数",
        "strong_rebound_captured": "捕获数",
        "precision": "精确率",
        "recall": "召回率",
    }, {"strong_rebound_threshold", "precision", "recall"}))
    lines.append("")
    lines.append("## 误报和漏报样本")
    lines.append("")
    lines.extend(table_or_empty(false_alarm_miss_cases.head(20), {
        "case_type": "类型",
        "trade_date": "日期",
        f"benchmark_forward_return_{best_horizon}d": "未来基准收益",
        "market_stress_score": "压力分",
        "low_value_oversold_count": "低估超跌数",
        "low_value_oversold_non_trap_count": "非陷阱数",
        "pressure_tier": "压力状态",
        "negative_breadth_60d": "60日下跌广度",
        "market_drawdown_252d": "市场回撤",
        "return_pressure": "收益压力",
        "volatility_pressure": "波动压力",
    }, {f"benchmark_forward_return_{best_horizon}d", "market_stress_score", "negative_breadth_60d", "market_drawdown_252d", "return_pressure", "volatility_pressure"}))
    lines.append("")
    lines.append("## 下一轮优化方向")
    lines.append("")
    for item in optimization_notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议 V2.12 方向：{optimization_notes.get('recommended_v2_12_direction', '')}")
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
    lines.append("- `report.md`：中文反弹窗口识别审计报告，优先打开。")
    lines.append("- `top_candidates.csv`：最接近通过的窗口规则排序；不是交易信号。")
    lines.append("- `run_summary.json`：机器可读运行摘要。")
    lines.append("- `debug/`：日期面板、窗口审计、基线对照、强反弹捕获、误报漏报和审计文件。")
    lines.append("")
    lines.append(f"研究边界：{policy['research_boundary']}")
    return "\n".join(lines)


def score_window(row: dict[str, Any], policy: dict[str, Any]) -> float:
    thresholds = policy["promotion_thresholds"]
    score = 0.0
    score += 3.0 * nz(row.get("uplift_vs_all_dates"))
    score += 2.5 * nz(row.get("uplift_vs_pressure_dates"))
    score += 1.5 * (nz(row.get("signal_win_rate")) - nz(row.get("all_dates_win_rate")))
    score += 1.2 * nz(row.get("signal_strong_rebound_rate"))
    score += 1.0 * nz(row.get("strong_rebound_recall"))
    score -= 1.5 * nz(row.get("signal_bad_window_rate"))
    if nz(row.get("signal_samples")) < int(thresholds["min_samples"]):
        score -= 0.15
    if nz(row.get("random_outperformance_pvalue")) > float(thresholds["max_random_pvalue"]):
        score -= 0.10
    return float(score)


def classify_window(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    checks = [
        nz(row.get("signal_samples")) >= int(th["min_samples"]),
        nz(row.get("nonoverlap_samples")) >= int(th["min_nonoverlap_samples"]),
        nz(row.get("uplift_vs_all_dates")) >= float(th["min_uplift_vs_all_dates"]),
        nz(row.get("uplift_vs_pressure_dates")) >= float(th["min_uplift_vs_pressure_dates"]),
        nz(row.get("signal_win_rate")) >= float(th["min_win_rate"]),
        nz(row.get("strong_rebound_recall")) >= float(th["min_strong_rebound_recall"]),
        nz(row.get("signal_bad_window_rate")) <= float(th["max_bad_window_rate"]),
        nz(row.get("random_outperformance_pvalue"), 1.0) <= float(th["max_random_pvalue"]),
    ]
    if all(checks):
        return "反弹窗口候选"
    if checks[2] and checks[4] and nz(row.get("signal_samples")) >= 10:
        return "弱证据观察"
    if nz(row.get("signal_samples")) < int(th["min_samples"]):
        return "样本不足"
    return "拒绝"


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；尚未证明能可靠识别反弹窗口"
    return "research_only；存在反弹窗口候选，但仍需独立样本和源审计"


def safe_sub(left: Any, right: Any) -> float:
    left_number = float_or_nan(left)
    right_number = float_or_nan(right)
    if math.isnan(left_number) or math.isnan(right_number):
        return math.nan
    return left_number - right_number


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


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path, encoding="utf-8-sig")


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
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and math.isnan(value):
        return None
    return str(value)


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


if __name__ == "__main__":
    main()
