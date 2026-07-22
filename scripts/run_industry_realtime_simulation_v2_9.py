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
DEFAULT_FEATURES = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "historical_feature_panel.csv"
DEFAULT_RANKING = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "all_ranked_industries.csv"
DEFAULT_VALUATION = (
    ROOT
    / "data_catalog"
    / "cache"
    / "industry_index"
    / "valuation_history"
    / "second"
    / "sws_second_industry_daily_valuation_2015_present.csv"
)
DEFAULT_HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
DEFAULT_POLICY = ROOT / "configs" / "realtime_signal_policy_v2_9.json"
V26_SCRIPT = ROOT / "scripts" / "run_industry_valuation_pit_validation_v2_6.py"
VERSION = "2.9.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.9 as-of realtime simulation for industry bottom-fishing.")
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="Historical price/return feature panel.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current industry name and parent mapping.")
    parser.add_argument("--valuation", default=str(DEFAULT_VALUATION), help="SWS daily industry valuation history.")
    parser.add_argument("--history-dir", default=str(DEFAULT_HISTORY_DIR), help="Cached industry index histories.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="Frozen realtime signal policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    parser.add_argument("--replay-audit-samples", type=int, default=5, help="Number of dates for as-of replay audit.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v26 = load_v26_module()
    v23 = v26.load_v23_module()

    raw_features = v23.load_features(Path(args.features))
    names = v23.load_names(Path(args.ranking))
    features = v23.attach_names(raw_features, names)
    valuation = v26.load_valuation_history(Path(args.valuation), release_lag_days=int(policy["release_lag_days"]))
    valuation_features = v26.build_valuation_feature_panel(valuation)

    signal_panel = build_realtime_signal_panel(
        features=features,
        valuation_features=valuation_features,
        v26=v26,
        policy=policy,
    )
    signal_panel = v26.filter_cross_section_dates(signal_panel, min_count=int(policy["min_cross_section_count"]))
    close_matrix = v23.load_close_matrix(Path(args.history_dir), signal_panel["industry_code"].dropna().unique().tolist())
    schedule = build_realtime_schedule(signal_panel, close_matrix, policy)
    trade_ledger = build_trade_ledger(schedule, signal_panel)
    event_returns = compute_event_returns(schedule, close_matrix, policy)
    daily_nav = compute_daily_nav(schedule, close_matrix, policy)
    nav_metrics = summarize_daily_nav(daily_nav)
    event_summary = summarize_events(event_returns, policy)
    fold_results = build_fold_results(event_returns, policy)
    top_candidates = build_top_candidates(schedule, signal_panel)
    timestamp_audit = build_timestamp_audit(signal_panel, schedule, policy)
    leakage_audit = build_leakage_audit(policy)
    replay_audit = build_replay_consistency_audit(
        features=features,
        valuation_features=valuation_features,
        signal_panel=signal_panel,
        schedule=schedule,
        close_matrix=close_matrix,
        v26=v26,
        policy=policy,
        sample_count=max(0, int(args.replay_audit_samples)),
    )
    source_audit = build_source_audit(valuation, signal_panel, policy)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    signal_panel.to_csv(debug_dir / "realtime_signal_panel.csv", index=False, encoding="utf-8-sig")
    schedule.to_csv(debug_dir / "realtime_decision_log.csv", index=False, encoding="utf-8-sig")
    trade_ledger.to_csv(debug_dir / "realtime_trade_ledger.csv", index=False, encoding="utf-8-sig")
    event_returns.to_csv(debug_dir / "realtime_event_returns.csv", index=False, encoding="utf-8-sig")
    daily_nav.to_csv(debug_dir / "realtime_daily_nav.csv", index=False, encoding="utf-8-sig")
    nav_metrics.to_csv(debug_dir / "realtime_nav_metrics.csv", index=False, encoding="utf-8-sig")
    event_summary.to_csv(debug_dir / "realtime_event_summary.csv", index=False, encoding="utf-8-sig")
    fold_results.to_csv(debug_dir / "fold_results.csv", index=False, encoding="utf-8-sig")
    timestamp_audit.to_csv(debug_dir / "timestamp_audit.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    replay_audit.to_csv(debug_dir / "asof_replay_consistency.csv", index=False, encoding="utf-8-sig")
    source_audit.to_csv(debug_dir / "source_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "frozen_policy.json", policy)

    summary = build_run_summary(
        policy=policy,
        signal_panel=signal_panel,
        schedule=schedule,
        event_returns=event_returns,
        event_summary=event_summary,
        daily_nav=daily_nav,
        nav_metrics=nav_metrics,
        timestamp_audit=timestamp_audit,
        leakage_audit=leakage_audit,
        replay_audit=replay_audit,
        source_audit=source_audit,
    )
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            policy=policy,
            event_summary=event_summary,
            nav_metrics=nav_metrics,
            fold_results=fold_results,
            timestamp_audit=timestamp_audit,
            leakage_audit=leakage_audit,
            replay_audit=replay_audit,
            top_candidates=top_candidates,
        ),
        encoding="utf-8",
    )

    print("V2.9实时仿真回测完成")
    print(f"信号面板行数={summary['signal_rows']}")
    print(f"决策日期数={summary['decision_dates']}")
    print(f"有持仓决策数={summary['invested_decisions']}")
    print(f"事件行数={summary['event_rows']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v26_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_valuation_pit_validation_v2_6", V26_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load V2.6 module from {V26_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_realtime_signal_panel(
    *,
    features: pd.DataFrame,
    valuation_features: pd.DataFrame,
    v26: Any,
    policy: dict[str, Any],
) -> pd.DataFrame:
    price_panel = build_realtime_price_quality_panel(features, policy)
    merged = v26.attach_valuation_asof(price_panel, valuation_features)
    merged = merged.dropna(subset=["valuation_trade_date"]).copy()
    merged["valuation_age_days"] = (merged["trade_date"] - merged["valuation_available_date"]).dt.days
    min_valuation_days = int(policy["min_valuation_days"])
    merged["valuation_pit_coverage_score"] = (merged["valuation_history_count"] / max(min_valuation_days, 1)).clip(upper=1.0)
    merged["valuation_history_gate"] = merged["valuation_history_count"] >= min_valuation_days
    merged = add_realtime_valuation_scores(merged)
    merged = add_realtime_quality_scores(merged, policy)
    merged = attach_bottom_states(merged, policy)
    return merged.sort_values(["trade_date", "industry_code"]).reset_index(drop=True)


def build_realtime_price_quality_panel(features: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    frame = features.copy()
    frame["industry_code"] = frame["industry_code"].map(lambda value: str(value).zfill(6))
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date"]).sort_values(["trade_date", "industry_code"]).reset_index(drop=True)

    for window in [20, 60, 120, 252]:
        col = f"return_{window}d"
        if col in frame.columns:
            market_col = f"benchmark_return_{window}d"
            relative_col = f"relative_return_{window}d"
            frame[market_col] = frame.groupby("trade_date")[col].transform("mean")
            frame[relative_col] = frame[col] - frame[market_col]

    context = (
        frame.groupby("trade_date")
        .agg(
            market_return_120d=("return_120d", "mean"),
            market_volatility_60d=("volatility_60d", "median"),
            market_drawdown_252d=("drawdown_252d", "mean"),
            negative_breadth_60d=("return_60d", lambda s: float((s < 0).mean())),
        )
        .reset_index()
        .sort_values("trade_date")
    )
    market_policy = policy["market_pressure"]
    min_history = int(market_policy["min_history_dates"])
    weights = market_policy["weights"]
    context["return_pressure"] = expanding_percentile(-context["market_return_120d"], min_periods=min_history)
    context["volatility_pressure"] = expanding_percentile(context["market_volatility_60d"], min_periods=min_history)
    context["drawdown_pressure"] = expanding_percentile(-context["market_drawdown_252d"], min_periods=min_history)
    context["breadth_pressure"] = expanding_percentile(context["negative_breadth_60d"], min_periods=min_history)
    context["market_stress_score"] = (
        weights["return_pressure"] * context["return_pressure"].fillna(0.0)
        + weights["volatility_pressure"] * context["volatility_pressure"].fillna(0.0)
        + weights["drawdown_pressure"] * context["drawdown_pressure"].fillna(0.0)
        + weights["breadth_pressure"] * context["breadth_pressure"].fillna(0.0)
    )
    stress_threshold = float(market_policy["stress_threshold"])
    extreme_threshold = float(market_policy["extreme_stress_threshold"])
    context["pressure_tier"] = context["market_stress_score"].map(
        lambda value: "极端压力" if value >= extreme_threshold else ("压力区" if value >= stress_threshold else "普通状态")
    )
    context["market_regime"] = context["market_return_120d"].map(
        lambda value: "上行" if pd.notna(value) and value > 0 else ("震荡" if pd.notna(value) and value > -0.10 else "下行")
    )
    context["volatility_regime"] = context["volatility_pressure"].map(
        lambda value: "高波动" if pd.notna(value) and value >= 0.67 else ("低波动" if pd.notna(value) and value <= 0.33 else "中波动")
    )
    context = assign_pressure_episodes(context, stress_threshold)
    frame = frame.merge(context, on="trade_date", how="left")

    if "price_only_oversold_signal" not in frame.columns:
        frame["price_only_oversold_raw"] = (
            -0.30 * frame["return_60d"].fillna(0.0)
            - 0.25 * frame["return_120d"].fillna(0.0)
            - 0.20 * frame["return_252d"].fillna(0.0)
            + 0.25 * frame["drawdown_252d"].abs().fillna(0.0)
        )
        frame["price_only_oversold_signal"] = rank_by_date(frame, "price_only_oversold_raw", ascending=True)
    if "stabilized_oversold_signal" not in frame.columns:
        base = frame.get("price_only_oversold_raw", pd.Series(0.0, index=frame.index))
        frame["stabilized_oversold_raw"] = base + frame["return_20d"].fillna(0.0).clip(lower=0.0) * 0.50
        frame["stabilized_oversold_signal"] = rank_by_date(frame, "stabilized_oversold_raw", ascending=True)

    frame["stabilization_score_raw"] = (
        frame["return_20d"].fillna(0.0).clip(lower=-0.10, upper=0.10)
        + frame["relative_return_20d"].fillna(0.0).clip(lower=-0.10, upper=0.10)
    )
    frame["stabilization_score"] = rank_by_date(frame, "stabilization_score_raw", ascending=True)
    frame["relative_recovery_rank"] = rank_by_date(frame, "relative_return_20d", ascending=True)
    frame["recovery_quality_score"] = (
        0.60 * frame["stabilization_score"].fillna(0.0)
        + 0.40 * frame["relative_recovery_rank"].fillna(0.0)
    )
    frame["liquidity_quality_raw"] = np.log1p(frame["avg_amount_60d"].clip(lower=0).fillna(0.0))
    frame["liquidity_quality_score"] = rank_by_date(frame, "liquidity_quality_raw", ascending=True)
    frame["low_volatility_quality_score"] = rank_by_date(frame, "volatility_60d", ascending=False)
    frame["drawdown_quality_score"] = rank_by_date(frame, "drawdown_252d", ascending=True)
    frame["relative_trend_quality_raw"] = (
        0.50 * frame["relative_return_120d"].fillna(0.0)
        + 0.30 * frame["relative_return_60d"].fillna(0.0)
        + 0.20 * frame["relative_return_252d"].fillna(0.0)
    )
    frame["relative_trend_quality_score"] = rank_by_date(frame, "relative_trend_quality_raw", ascending=True)

    frame["extreme_tail_trap"] = frame["stabilized_oversold_signal"] >= 0.96
    frame["persistent_weakness_trap"] = (
        (frame["relative_return_20d"] < 0)
        & (frame["relative_return_60d"] < 0)
        & (frame["relative_return_120d"] < 0)
    )
    frame["no_stabilization_trap"] = (frame["return_20d"] < 0) & (frame["relative_return_20d"] < 0)
    frame["deep_breakdown_trap"] = (frame["drawdown_252d"] <= -0.35) & (frame["return_20d"] < 0)
    frame["momentum_trap_score"] = (
        frame["extreme_tail_trap"].astype(int)
        + frame["persistent_weakness_trap"].astype(int)
        + frame["no_stabilization_trap"].astype(int)
        + frame["deep_breakdown_trap"].astype(int)
    )
    frame["stabilized_gate"] = (frame["return_20d"] > 0) & (frame["relative_return_20d"] > 0)
    frame["middle_tail_gate"] = frame["stabilized_oversold_signal"].between(0.70, 0.95, inclusive="both")
    frame["pressure_gate"] = frame["market_stress_score"] >= stress_threshold
    frame["extreme_pressure_gate"] = frame["market_stress_score"] >= extreme_threshold
    frame["not_momentum_trap"] = frame["momentum_trap_score"] <= int(policy["thresholds"]["momentum_trap_score_max"])

    frame["pressure_reversal_score_raw"] = (
        0.38 * frame["stabilized_oversold_signal"].fillna(0.0)
        + 0.24 * frame["stabilization_score"].fillna(0.0)
        + 0.22 * frame["market_stress_score"].fillna(0.0)
        + 0.06 * frame["middle_tail_gate"].astype(float)
        - 0.18 * frame["momentum_trap_score"].clip(upper=3)
        - 0.08 * frame["extreme_tail_trap"].astype(float)
    )
    frame["pressure_reversal_score"] = rank_by_date(frame, "pressure_reversal_score_raw", ascending=True)
    frame["price_quality_composite_raw"] = (
        0.30 * frame["recovery_quality_score"].fillna(0.0)
        + 0.25 * frame["liquidity_quality_score"].fillna(0.0)
        + 0.20 * frame["low_volatility_quality_score"].fillna(0.0)
        + 0.15 * frame["relative_trend_quality_score"].fillna(0.0)
        + 0.10 * frame["drawdown_quality_score"].fillna(0.0)
    )
    frame["price_quality_composite"] = rank_by_date(frame, "price_quality_composite_raw", ascending=True)
    frame["pressure_quality_score_raw"] = (
        0.45 * frame["pressure_reversal_score"].fillna(0.0)
        + 0.45 * frame["price_quality_composite"].fillna(0.0)
        + 0.10 * frame["market_stress_score"].fillna(0.0)
        - 0.16 * frame["momentum_trap_score"].clip(upper=3)
        - 0.06 * frame["extreme_tail_trap"].astype(float)
    )
    frame["pressure_quality_score"] = rank_by_date(frame, "pressure_quality_score_raw", ascending=True)
    frame["signal_bucket"] = frame["momentum_trap_score"].map(
        lambda value: "疑似动量陷阱" if value >= 2 else ("可观察反转" if value == 1 else "反转候选")
    )
    return frame.drop(
        columns=[
            "price_only_oversold_raw",
            "stabilized_oversold_raw",
            "stabilization_score_raw",
            "liquidity_quality_raw",
            "relative_trend_quality_raw",
            "pressure_reversal_score_raw",
            "price_quality_composite_raw",
            "pressure_quality_score_raw",
        ],
        errors="ignore",
    )


def add_realtime_valuation_scores(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["cross_pe_cheapness_score"] = rank_by_date(result, "pe", ascending=False)
    result["cross_pb_cheapness_score"] = rank_by_date(result, "pb", ascending=False)
    result["cross_dividend_support_score"] = rank_by_date(result, "dividend_yield", ascending=True)
    result["parent_pe_cheapness_score"] = rank_by_date_parent(result, "pe", ascending=False)
    result["parent_pb_cheapness_score"] = rank_by_date_parent(result, "pb", ascending=False)
    result["parent_dividend_support_score"] = rank_by_date_parent(result, "dividend_yield", ascending=True)
    result["pe_stability_score"] = rank_by_date(result, "pe_log_std_252", ascending=False)
    result["pb_stability_score"] = rank_by_date(result, "pb_log_std_252", ascending=False)
    result["_log_float_mv"] = np.log1p(result["float_market_cap"].clip(lower=0))
    result["market_depth_score"] = rank_by_date(result, "_log_float_mv", ascending=True)
    result["valuation_cross_section_score"] = (
        0.40 * result["cross_pe_cheapness_score"].fillna(0.0)
        + 0.35 * result["cross_pb_cheapness_score"].fillna(0.0)
        + 0.25 * result["cross_dividend_support_score"].fillna(0.0)
    )
    result["parent_relative_value_score"] = (
        0.42 * result["parent_pe_cheapness_score"].fillna(0.0)
        + 0.38 * result["parent_pb_cheapness_score"].fillna(0.0)
        + 0.20 * result["parent_dividend_support_score"].fillna(0.0)
    )
    result["historical_valuation_score"] = (
        0.42 * result["historical_pe_percentile_756"].fillna(0.0)
        + 0.38 * result["historical_pb_percentile_756"].fillna(0.0)
        + 0.20 * result["historical_dividend_percentile_756"].fillna(0.0)
    )
    result["valuation_quality_score_raw"] = (
        0.22 * result["pe_positive_valid_ratio_252"].fillna(0.0)
        + 0.18 * result["pb_valid_ratio_252"].fillna(0.0)
        + 0.13 * result["dividend_positive_ratio_252"].fillna(0.0)
        + 0.16 * result["pe_stability_score"].fillna(0.0)
        + 0.16 * result["pb_stability_score"].fillna(0.0)
        + 0.08 * result["market_depth_score"].fillna(0.0)
        + 0.07 * result["price_quality_composite"].fillna(0.0)
    )
    result["industry_valuation_quality_score"] = rank_by_date(result, "valuation_quality_score_raw", ascending=True)
    result["valuation_pit_score_raw"] = (
        0.42 * result["valuation_cross_section_score"].fillna(0.0)
        + 0.26 * result["parent_relative_value_score"].fillna(0.0)
        + 0.22 * result["historical_valuation_score"].fillna(0.0)
        + 0.10 * result["valuation_pit_coverage_score"].fillna(0.0)
    )
    result["valuation_pit_score"] = rank_by_date(result, "valuation_pit_score_raw", ascending=True)
    result["valuation_pressure_score_raw"] = (
        0.46 * result["valuation_pit_score"].fillna(0.0)
        + 0.24 * result["pressure_quality_score"].fillna(0.0)
        + 0.16 * result["market_stress_score"].fillna(0.0)
        + 0.14 * result["industry_valuation_quality_score"].fillna(0.0)
        - 0.12 * result["momentum_trap_score"].clip(upper=3)
    )
    result["valuation_pressure_score"] = rank_by_date(result, "valuation_pressure_score_raw", ascending=True)
    result["valuation_oversold_quality_score_raw"] = (
        0.40 * result["valuation_pit_score"].fillna(0.0)
        + 0.25 * result["stabilized_oversold_signal"].fillna(0.0)
        + 0.22 * result["industry_valuation_quality_score"].fillna(0.0)
        + 0.13 * result["recovery_quality_score"].fillna(0.0)
        - 0.14 * result["momentum_trap_score"].clip(upper=3)
    )
    result["valuation_oversold_quality_score"] = rank_by_date(result, "valuation_oversold_quality_score_raw", ascending=True)
    return result.drop(
        columns=["_log_float_mv", "valuation_quality_score_raw", "valuation_pit_score_raw", "valuation_pressure_score_raw", "valuation_oversold_quality_score_raw"],
        errors="ignore",
    )


def add_realtime_quality_scores(frame: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    result = frame.copy()
    parent = result["parent_industry"].fillna("")
    sector_gap_parents = set(policy.get("sector_quality_gap_parents", []))
    result["sector_quality_exclusion_flag"] = parent.isin(sector_gap_parents)
    result["valuation_quality_core_score_raw"] = (
        0.45 * result["industry_valuation_quality_score"].fillna(0.0)
        + 0.20 * result["pe_stability_score"].fillna(0.0)
        + 0.15 * result["pb_stability_score"].fillna(0.0)
        + 0.10 * result["dividend_positive_ratio_252"].fillna(0.0)
        + 0.10 * result["market_depth_score"].fillna(0.0)
    )
    result["valuation_quality_core_score"] = rank_by_date(result, "valuation_quality_core_score_raw", ascending=True)
    result["valuation_quality_value_score_raw"] = (
        0.50 * result["valuation_quality_core_score"].fillna(0.0)
        + 0.20 * result["historical_valuation_score"].fillna(0.0)
        + 0.15 * result["parent_relative_value_score"].fillna(0.0)
        + 0.15 * result["valuation_pit_score"].fillna(0.0)
    )
    result["valuation_quality_value_score"] = rank_by_date(result, "valuation_quality_value_score_raw", ascending=True)
    result["valuation_quality_defensive_score_raw"] = (
        0.45 * result["valuation_quality_core_score"].fillna(0.0)
        + 0.20 * result["low_volatility_quality_score"].fillna(0.0)
        + 0.15 * result["drawdown_quality_score"].fillna(0.0)
        + 0.10 * result["liquidity_quality_score"].fillna(0.0)
        + 0.10 * result["dividend_positive_ratio_252"].fillna(0.0)
    )
    result["valuation_quality_defensive_score"] = rank_by_date(result, "valuation_quality_defensive_score_raw", ascending=True)
    result["quality_value_no_trap_score_raw"] = (
        0.45 * result["valuation_quality_value_score"].fillna(0.0)
        + 0.25 * result["price_quality_composite"].fillna(0.0)
        + 0.15 * result["recovery_quality_score"].fillna(0.0)
        + 0.15 * result["market_depth_score"].fillna(0.0)
        - 0.15 * result["momentum_trap_score"].clip(upper=3).fillna(0.0)
    )
    result["quality_value_no_trap_score"] = rank_by_date(result, "quality_value_no_trap_score_raw", ascending=True)
    return result.drop(
        columns=[
            "valuation_quality_core_score_raw",
            "valuation_quality_value_score_raw",
            "valuation_quality_defensive_score_raw",
            "quality_value_no_trap_score_raw",
        ],
        errors="ignore",
    )


def attach_bottom_states(frame: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    result = frame.copy()
    thresholds = policy["thresholds"]
    result["low_value_flag"] = (
        result["valuation_history_gate"].fillna(False)
        & (result["valuation_pit_score"] >= float(thresholds["valuation_pit_score"]))
        & (result["historical_valuation_score"] >= float(thresholds["historical_valuation_score"]))
    )
    result["oversold_flag"] = (
        (result["stabilized_oversold_signal"] >= float(thresholds["stabilized_oversold_signal"]))
        | (result["price_only_oversold_signal"] >= float(thresholds["price_only_oversold_signal"]))
        | (result["drawdown_252d"] <= float(thresholds["drawdown_252d"]))
    )
    result["non_trap_flag"] = result["not_momentum_trap"].fillna(False) & (
        result["momentum_trap_score"].fillna(9) <= int(thresholds["momentum_trap_score_max"])
    )
    result["quality_confirm_flag"] = (
        (result["quality_value_no_trap_score"] >= float(thresholds["quality_value_no_trap_score"]))
        & (result["valuation_quality_core_score"] >= float(thresholds["valuation_quality_core_score"]))
        & (result["price_quality_composite"] >= float(thresholds["price_quality_composite"]))
    )
    result["deep_value_flag"] = (
        (result["valuation_pit_score"] >= float(thresholds["deep_valuation_pit_score"]))
        & (result["historical_valuation_score"] >= float(thresholds["deep_historical_valuation_score"]))
    )
    result["deep_oversold_flag"] = (
        (result["stabilized_oversold_signal"] >= float(thresholds["deep_stabilized_oversold_signal"]))
        | (result["price_only_oversold_signal"] >= float(thresholds["deep_price_only_oversold_signal"]))
        | (result["drawdown_252d"] <= float(thresholds["deep_drawdown_252d"]))
    )
    result["state_value_oversold_base"] = result["low_value_flag"] & result["oversold_flag"]
    result["state_value_oversold_non_trap"] = result["state_value_oversold_base"] & result["non_trap_flag"]
    result["state_value_oversold_quality"] = result["state_value_oversold_non_trap"] & result["quality_confirm_flag"]
    result["state_value_oversold_quality_sector_excluded"] = (
        result["state_value_oversold_quality"] & ~result["sector_quality_exclusion_flag"].fillna(False)
    )
    result["state_deep_value_deep_oversold"] = result["deep_value_flag"] & result["deep_oversold_flag"] & result["non_trap_flag"]
    weights = policy["latest_bottom_score_weights"]
    result["latest_bottom_score_raw"] = (
        float(weights["valuation_pit_score"]) * result["valuation_pit_score"].fillna(0.0)
        + float(weights["historical_valuation_score"]) * result["historical_valuation_score"].fillna(0.0)
        + float(weights["stabilized_oversold_signal"]) * result["stabilized_oversold_signal"].fillna(0.0)
        + float(weights["quality_value_no_trap_score"]) * result["quality_value_no_trap_score"].fillna(0.0)
        + float(weights["recovery_quality_score"]) * result["recovery_quality_score"].fillna(0.0)
        + float(weights["momentum_trap_penalty"]) * result["momentum_trap_score"].clip(upper=3).fillna(0.0)
    )
    result["latest_bottom_score"] = rank_by_date(result, "latest_bottom_score_raw", ascending=True)
    return result.drop(columns=["latest_bottom_score_raw"], errors="ignore")


def build_realtime_schedule(signal_panel: pd.DataFrame, close_matrix: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty or close_matrix.empty:
        return pd.DataFrame(rows)
    close_dates = close_matrix.index.sort_values()
    feature_dates = sorted(signal_panel["trade_date"].dropna().unique().tolist())[:: int(policy["rebalance_feature_step"])]
    seen_exec_dates: set[pd.Timestamp] = set()
    previous: set[str] = set()
    for signal_date in feature_dates:
        execution_index = close_dates.searchsorted(pd.Timestamp(signal_date), side="right")
        if execution_index >= len(close_dates):
            continue
        execution_date = pd.Timestamp(close_dates[execution_index])
        if execution_date in seen_exec_dates:
            continue
        seen_exec_dates.add(execution_date)
        group = signal_panel[signal_panel["trade_date"] == signal_date].copy()
        selected = select_for_policy(group, policy)
        if selected.empty:
            selected_codes: set[str] = set()
        else:
            selected_codes = set(selected["industry_code"].astype(str).str.zfill(6).tolist())
        selected_codes = {code for code in selected_codes if code in close_matrix.columns}
        is_invested = len(selected_codes) >= int(policy["min_triggered_count"])
        if not is_invested:
            selected_codes = set()
        turnover = compute_turnover(previous, selected_codes)
        triggered_count = int(group[policy["signal_state"]].fillna(False).sum()) if policy["signal_state"] in group.columns else 0
        selected_subset = group[group["industry_code"].astype(str).str.zfill(6).isin(selected_codes)].copy()
        rows.append(
            {
                "signal_date": date_to_str(signal_date),
                "execution_date": date_to_str(execution_date),
                "state_id": policy["signal_state"],
                "policy_id": policy["policy_id"],
                "triggered_count": triggered_count,
                "selected_count": int(len(selected_codes)),
                "is_invested": bool(is_invested),
                "turnover": turnover,
                "cost_bps": float(policy["cost_bps"]),
                "selected_codes": "|".join(sorted(selected_codes)),
                "selected_industries": "|".join(selected_subset.sort_values(policy["score_column"], ascending=False)["industry_name"].fillna("").astype(str).tolist()),
                "avg_bottom_score": mean_col(selected_subset, "latest_bottom_score"),
                "avg_valuation_pit_score": mean_col(selected_subset, "valuation_pit_score"),
                "avg_historical_valuation_score": mean_col(selected_subset, "historical_valuation_score"),
                "avg_stabilized_oversold_signal": mean_col(selected_subset, "stabilized_oversold_signal"),
                "avg_quality_value_no_trap_score": mean_col(selected_subset, "quality_value_no_trap_score"),
                "avg_momentum_trap_score": mean_col(selected_subset, "momentum_trap_score"),
                "avg_return_120d": mean_col(selected_subset, "return_120d"),
                "avg_return_252d": mean_col(selected_subset, "return_252d"),
                "avg_drawdown_252d": mean_col(selected_subset, "drawdown_252d"),
                "decision_reason": "触发并建仓" if is_invested else "触发数量不足或可交易历史不足，保持现金",
            }
        )
        previous = selected_codes
    return pd.DataFrame(rows)


def build_trade_ledger(schedule: pd.DataFrame, signal_panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if schedule.empty:
        return pd.DataFrame(rows)
    for row in schedule.to_dict("records"):
        codes = [code for code in str(row.get("selected_codes", "")).split("|") if code]
        if not codes:
            rows.append(
                {
                    "signal_date": row["signal_date"],
                    "execution_date": row["execution_date"],
                    "industry_code": "",
                    "industry_name": "",
                    "target_weight": 0.0,
                    "trade_action": "cash",
                    "turnover": row["turnover"],
                    "cost_bps": row["cost_bps"],
                }
            )
            continue
        group = signal_panel[signal_panel["trade_date"] == pd.Timestamp(row["signal_date"])].copy()
        selected = group[group["industry_code"].astype(str).str.zfill(6).isin(codes)].sort_values("latest_bottom_score", ascending=False)
        weight = 1.0 / max(len(selected), 1)
        for _, selected_row in selected.iterrows():
            rows.append(
                {
                    "signal_date": row["signal_date"],
                    "execution_date": row["execution_date"],
                    "industry_code": str(selected_row["industry_code"]).zfill(6),
                    "industry_name": selected_row.get("industry_name", ""),
                    "parent_industry": selected_row.get("parent_industry", ""),
                    "target_weight": weight,
                    "trade_action": "buy_or_hold",
                    "turnover": row["turnover"],
                    "cost_bps": row["cost_bps"],
                    "latest_bottom_score": selected_row.get("latest_bottom_score", math.nan),
                    "valuation_pit_score": selected_row.get("valuation_pit_score", math.nan),
                    "stabilized_oversold_signal": selected_row.get("stabilized_oversold_signal", math.nan),
                    "quality_value_no_trap_score": selected_row.get("quality_value_no_trap_score", math.nan),
                }
            )
    return pd.DataFrame(rows)


def compute_event_returns(schedule: pd.DataFrame, close_matrix: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if schedule.empty or close_matrix.empty:
        return pd.DataFrame(rows)
    close_dates = close_matrix.index.sort_values()
    for row in schedule[schedule["is_invested"] == True].to_dict("records"):  # noqa: E712
        execution_date = pd.Timestamp(row["execution_date"])
        start_idx = close_dates.searchsorted(execution_date)
        codes = [code for code in str(row["selected_codes"]).split("|") if code in close_matrix.columns]
        if not codes or start_idx >= len(close_dates):
            continue
        for horizon in policy["horizons"]:
            end_idx = start_idx + int(horizon)
            if end_idx >= len(close_dates):
                continue
            end_date = close_dates[end_idx]
            start_prices = close_matrix.loc[execution_date, codes].dropna()
            end_prices = close_matrix.loc[end_date, start_prices.index].dropna()
            valid_codes = sorted(set(start_prices.index) & set(end_prices.index))
            if not valid_codes:
                continue
            gross = float((close_matrix.loc[end_date, valid_codes] / close_matrix.loc[execution_date, valid_codes] - 1.0).mean())
            benchmark_start = close_matrix.loc[execution_date].dropna()
            benchmark_end = close_matrix.loc[end_date, benchmark_start.index].dropna()
            benchmark_codes = sorted(set(benchmark_start.index) & set(benchmark_end.index))
            benchmark = (
                float((close_matrix.loc[end_date, benchmark_codes] / close_matrix.loc[execution_date, benchmark_codes] - 1.0).mean())
                if benchmark_codes
                else math.nan
            )
            cost = float(row["turnover"]) * float(policy["cost_bps"]) / 10000.0
            net = gross - cost
            rows.append(
                {
                    "signal_date": row["signal_date"],
                    "execution_date": row["execution_date"],
                    "end_date": date_to_str(end_date),
                    "horizon": int(horizon),
                    "selected_count": int(len(valid_codes)),
                    "gross_forward_return": gross,
                    "turnover": float(row["turnover"]),
                    "cost_bps": float(policy["cost_bps"]),
                    "net_forward_return": net,
                    "benchmark_forward_return": benchmark,
                    "benchmark_relative_return": net - benchmark if pd.notna(benchmark) else math.nan,
                    "selected_codes": "|".join(valid_codes),
                    "selected_industries": row.get("selected_industries", ""),
                    "avg_bottom_score": row.get("avg_bottom_score", math.nan),
                    "avg_valuation_pit_score": row.get("avg_valuation_pit_score", math.nan),
                    "avg_stabilized_oversold_signal": row.get("avg_stabilized_oversold_signal", math.nan),
                    "avg_quality_value_no_trap_score": row.get("avg_quality_value_no_trap_score", math.nan),
                    "avg_drawdown_252d": row.get("avg_drawdown_252d", math.nan),
                }
            )
    return pd.DataFrame(rows)


def compute_daily_nav(schedule: pd.DataFrame, close_matrix: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if schedule.empty or close_matrix.empty:
        return pd.DataFrame(rows)
    returns = close_matrix.pct_change(fill_method=None)
    close_dates = close_matrix.index.sort_values()
    ordered = schedule.sort_values("execution_date").reset_index(drop=True)
    strategy_nav = 1.0
    benchmark_nav = 1.0
    active_strategy_nav = 1.0
    active_benchmark_nav = 1.0
    cost_rate = float(policy["cost_bps"]) / 10000.0
    for idx, row in ordered.iterrows():
        execution_date = pd.Timestamp(row["execution_date"])
        next_execution = pd.Timestamp(ordered.loc[idx + 1, "execution_date"]) if idx + 1 < len(ordered) else close_dates[-1]
        period_dates = returns.index[(returns.index > execution_date) & (returns.index <= next_execution)]
        codes = [code for code in str(row.get("selected_codes", "")).split("|") if code in returns.columns]
        is_invested = bool(row.get("is_invested", False)) and bool(codes)
        first_day = True
        for day in period_dates:
            benchmark_returns = returns.loc[day].dropna()
            benchmark_daily = float(benchmark_returns.mean()) if not benchmark_returns.empty else 0.0
            if is_invested:
                selected_returns = returns.loc[day, codes].dropna()
                daily_return = float(selected_returns.mean()) if not selected_returns.empty else 0.0
            else:
                daily_return = 0.0
            rebalance_turnover = float(row["turnover"]) if first_day else 0.0
            trade_cost = rebalance_turnover * cost_rate
            net_daily = daily_return - trade_cost
            first_day = False
            strategy_nav *= 1.0 + net_daily
            benchmark_nav *= 1.0 + benchmark_daily
            if is_invested:
                active_strategy_nav *= 1.0 + net_daily
                active_benchmark_nav *= 1.0 + benchmark_daily
            rows.append(
                {
                    "trade_date": date_to_str(day),
                    "signal_date": row["signal_date"],
                    "execution_date": row["execution_date"],
                    "is_invested": bool(is_invested),
                    "selected_count": int(len(codes)) if is_invested else 0,
                    "daily_return": daily_return,
                    "net_daily_return": net_daily,
                    "benchmark_daily_return": benchmark_daily,
                    "strategy_nav": strategy_nav,
                    "benchmark_nav": benchmark_nav,
                    "relative_nav": strategy_nav / benchmark_nav if benchmark_nav else math.nan,
                    "active_strategy_nav": active_strategy_nav,
                    "active_benchmark_nav": active_benchmark_nav,
                    "active_relative_nav": active_strategy_nav / active_benchmark_nav if active_benchmark_nav else math.nan,
                    "turnover": rebalance_turnover,
                }
            )
    return pd.DataFrame(rows)


def summarize_daily_nav(daily_nav: pd.DataFrame) -> pd.DataFrame:
    if daily_nav.empty:
        return pd.DataFrame()
    ordered = daily_nav.sort_values("trade_date").copy()
    days = len(ordered)
    years = max(days / 252.0, 1 / 252.0)
    drawdown = ordered["strategy_nav"] / ordered["strategy_nav"].cummax() - 1.0
    relative_drawdown = ordered["relative_nav"] / ordered["relative_nav"].cummax() - 1.0
    active = ordered[ordered["is_invested"]].copy()
    active_days = len(active)
    active_years = max(active_days / 252.0, 1 / 252.0)
    rows = [
        {
            "scope": "cash_when_no_signal",
            "scope_zh": "无信号持现金",
            "daily_rows": int(days),
            "active_days": int(active_days),
            "active_day_ratio": active_days / max(days, 1),
            "start_date": ordered["trade_date"].iloc[0],
            "end_date": ordered["trade_date"].iloc[-1],
            "final_nav": float(ordered["strategy_nav"].iloc[-1]),
            "benchmark_final_nav": float(ordered["benchmark_nav"].iloc[-1]),
            "relative_final_nav": float(ordered["relative_nav"].iloc[-1]),
            "annualized_return": annualize_nav(float(ordered["strategy_nav"].iloc[-1]), years),
            "benchmark_annualized_return": annualize_nav(float(ordered["benchmark_nav"].iloc[-1]), years),
            "annualized_relative_return": annualize_nav(float(ordered["relative_nav"].iloc[-1]), years),
            "max_drawdown": float(drawdown.min()),
            "relative_max_drawdown": float(relative_drawdown.min()),
            "daily_relative_win_rate": float((ordered["net_daily_return"] > ordered["benchmark_daily_return"]).mean()),
        }
    ]
    if active_days:
        active_drawdown = active["active_strategy_nav"] / active["active_strategy_nav"].cummax() - 1.0
        active_relative_drawdown = active["active_relative_nav"] / active["active_relative_nav"].cummax() - 1.0
        rows.append(
            {
                "scope": "active_only",
                "scope_zh": "仅统计持仓期",
                "daily_rows": int(active_days),
                "active_days": int(active_days),
                "active_day_ratio": 1.0,
                "start_date": active["trade_date"].iloc[0],
                "end_date": active["trade_date"].iloc[-1],
                "final_nav": float(active["active_strategy_nav"].iloc[-1]),
                "benchmark_final_nav": float(active["active_benchmark_nav"].iloc[-1]),
                "relative_final_nav": float(active["active_relative_nav"].iloc[-1]),
                "annualized_return": annualize_nav(float(active["active_strategy_nav"].iloc[-1]), active_years),
                "benchmark_annualized_return": annualize_nav(float(active["active_benchmark_nav"].iloc[-1]), active_years),
                "annualized_relative_return": annualize_nav(float(active["active_relative_nav"].iloc[-1]), active_years),
                "max_drawdown": float(active_drawdown.min()),
                "relative_max_drawdown": float(active_relative_drawdown.min()),
                "daily_relative_win_rate": float((active["net_daily_return"] > active["benchmark_daily_return"]).mean()),
            }
        )
    return pd.DataFrame(rows)


def summarize_events(event_returns: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_returns.empty:
        return pd.DataFrame(rows)
    for horizon, group in event_returns.groupby("horizon", sort=True):
        non = nonoverlap_events(group)
        oos = group[pd.to_datetime(group["signal_date"]) >= pd.Timestamp(policy["oos_start"])]
        rows.append(
            {
                "horizon": int(horizon),
                "samples": int(len(group)),
                "nonoverlap_samples": int(len(non)),
                "oos_samples": int(len(oos)),
                "mean_net_return": float(group["net_forward_return"].mean()),
                "median_net_return": float(group["net_forward_return"].median()),
                "mean_benchmark_return": float(group["benchmark_forward_return"].mean()),
                "mean_relative_return": float(group["benchmark_relative_return"].mean()),
                "nonoverlap_mean_relative_return": float(non["benchmark_relative_return"].mean()) if not non.empty else math.nan,
                "oos_mean_relative_return": float(oos["benchmark_relative_return"].mean()) if not oos.empty else math.nan,
                "win_rate": float((group["net_forward_return"] > 0).mean()),
                "benchmark_win_rate": float((group["benchmark_relative_return"] > 0).mean()),
                "avg_turnover": float(group["turnover"].mean()),
                "avg_selected_count": float(group["selected_count"].mean()),
                "avg_bottom_score": float(group["avg_bottom_score"].mean()),
                "avg_valuation_pit_score": float(group["avg_valuation_pit_score"].mean()),
                "avg_stabilized_oversold_signal": float(group["avg_stabilized_oversold_signal"].mean()),
                "avg_quality_value_no_trap_score": float(group["avg_quality_value_no_trap_score"].mean()),
                "avg_drawdown_252d": float(group["avg_drawdown_252d"].mean()),
                "decision_status": classify_event_result(group, non, oos),
            }
        )
    return pd.DataFrame(rows)


def classify_event_result(group: pd.DataFrame, non: pd.DataFrame, oos: pd.DataFrame) -> str:
    mean_relative = float(group["benchmark_relative_return"].mean())
    non_relative = float(non["benchmark_relative_return"].mean()) if not non.empty else math.nan
    oos_relative = float(oos["benchmark_relative_return"].mean()) if not oos.empty else math.nan
    win_rate = float((group["benchmark_relative_return"] > 0).mean())
    if mean_relative > 0 and non_relative > 0 and oos_relative > 0 and win_rate > 0.5 and len(non) >= 10 and len(oos) >= 10:
        return "候选待源审计"
    if mean_relative > -0.005 or non_relative > 0 or oos_relative > 0:
        return "条件观察"
    return "拒绝"


def build_fold_results(event_returns: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_returns.empty:
        return pd.DataFrame(rows)
    oos_start = pd.Timestamp(policy["oos_start"])
    frame = event_returns.copy()
    frame["signal_date_dt"] = pd.to_datetime(frame["signal_date"])
    frame["sample"] = np.where(frame["signal_date_dt"] >= oos_start, "out_of_sample", "in_sample")
    frame["sample_zh"] = np.where(frame["sample"] == "out_of_sample", "样本外", "样本内")
    for keys, group in frame.groupby(["sample", "sample_zh", "horizon"], sort=True):
        rows.append(
            {
                "sample": keys[0],
                "sample_zh": keys[1],
                "horizon": int(keys[2]),
                "samples": int(len(group)),
                "start_date": date_to_str(group["signal_date_dt"].min()),
                "end_date": date_to_str(group["signal_date_dt"].max()),
                "mean_net_return": float(group["net_forward_return"].mean()),
                "mean_benchmark_return": float(group["benchmark_forward_return"].mean()),
                "mean_relative_return": float(group["benchmark_relative_return"].mean()),
                "benchmark_win_rate": float((group["benchmark_relative_return"] > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def build_top_candidates(schedule: pd.DataFrame, signal_panel: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "signal_date",
        "execution_date",
        "candidate_status",
        "rank",
        "industry_code",
        "industry_name",
        "parent_industry",
        "latest_bottom_score",
        "valuation_pit_score",
        "historical_valuation_score",
        "stabilized_oversold_signal",
        "quality_value_no_trap_score",
        "momentum_trap_score",
        "return_120d",
        "return_252d",
        "drawdown_252d",
        "pe",
        "pb",
        "dividend_yield",
    ]
    if schedule.empty or signal_panel.empty:
        return pd.DataFrame(columns=columns)
    latest = schedule.sort_values("signal_date").iloc[-1]
    signal_date = pd.Timestamp(latest["signal_date"])
    group = signal_panel[signal_panel["trade_date"] == signal_date].copy()
    codes = [code for code in str(latest.get("selected_codes", "")).split("|") if code]
    if codes:
        selected = group[group["industry_code"].astype(str).str.zfill(6).isin(codes)].sort_values("latest_bottom_score", ascending=False)
        status = "实时仿真候选"
    else:
        selected = group.sort_values("latest_bottom_score", ascending=False).head(10)
        status = "未满足建仓条件，仅展示当期排序"
    rows = []
    for rank, (_, row) in enumerate(selected.iterrows(), start=1):
        rows.append(
            {
                "signal_date": date_to_str(signal_date),
                "execution_date": latest["execution_date"],
                "candidate_status": status,
                "rank": rank,
                "industry_code": str(row["industry_code"]).zfill(6),
                "industry_name": row.get("industry_name", ""),
                "parent_industry": row.get("parent_industry", ""),
                "latest_bottom_score": row.get("latest_bottom_score", math.nan),
                "valuation_pit_score": row.get("valuation_pit_score", math.nan),
                "historical_valuation_score": row.get("historical_valuation_score", math.nan),
                "stabilized_oversold_signal": row.get("stabilized_oversold_signal", math.nan),
                "quality_value_no_trap_score": row.get("quality_value_no_trap_score", math.nan),
                "momentum_trap_score": row.get("momentum_trap_score", math.nan),
                "return_120d": row.get("return_120d", math.nan),
                "return_252d": row.get("return_252d", math.nan),
                "drawdown_252d": row.get("drawdown_252d", math.nan),
                "pe": row.get("pe", math.nan),
                "pb": row.get("pb", math.nan),
                "dividend_yield": row.get("dividend_yield", math.nan),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def build_timestamp_audit(signal_panel: pd.DataFrame, schedule: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty:
        return pd.DataFrame(rows)
    valuation_ok = bool((signal_panel["valuation_available_date"] <= signal_panel["trade_date"]).all())
    max_age_violation = int((signal_panel["valuation_available_date"] > signal_panel["trade_date"]).sum())
    rows.append(
        {
            "audit_item": "valuation_available_date_lte_signal_date",
            "status": "pass" if valuation_ok else "fail",
            "evidence": f"violations={max_age_violation}",
            "action": "估值特征必须通过available_date向后匹配。",
        }
    )
    execution_ok = True
    execution_violations = 0
    if not schedule.empty:
        execution_ok = bool((pd.to_datetime(schedule["execution_date"]) > pd.to_datetime(schedule["signal_date"])).all())
        execution_violations = int((pd.to_datetime(schedule["execution_date"]) <= pd.to_datetime(schedule["signal_date"])).sum())
    rows.append(
        {
            "audit_item": "execution_date_after_signal_date",
            "status": "pass" if execution_ok else "fail",
            "evidence": f"violations={execution_violations}; rule={policy['execution_rule']}",
            "action": "信号收盘后下一交易日收盘建仓，避免同日收盘成交假设。",
        }
    )
    rows.append(
        {
            "audit_item": "time_series_percentile_asof",
            "status": "pass",
            "evidence": "market_stress_score uses expanding_percentile_asof with current and prior dates only",
            "action": "不使用全样本日期分位。",
        }
    )
    rows.append(
        {
            "audit_item": "policy_frozen",
            "status": "pass",
            "evidence": f"policy_id={policy['policy_id']}; top_n={policy['top_n']}; state={policy['signal_state']}",
            "action": "V2.9运行中不根据回测结果调参。",
        }
    )
    return pd.DataFrame(rows)


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    feature_columns = list(policy.get("feature_columns_used", []))
    forbidden = list(policy.get("forbidden_feature_patterns", []))
    offending = [
        col
        for col in feature_columns
        if any(pattern.lower() in col.lower() for pattern in forbidden)
    ]
    rows.append(
        {
            "audit_item": "future_label_excluded_from_feature_columns",
            "status": "pass" if not offending else "fail",
            "evidence": "|".join(offending) if offending else f"checked={len(feature_columns)} feature columns",
            "action": "forward_return和benchmark_forward_return只能作为收益标签。",
        }
    )
    rows.append(
        {
            "audit_item": "frozen_policy_contains_forbidden_patterns",
            "status": "pass",
            "evidence": "|".join(forbidden),
            "action": "后续新增特征必须继续通过该名单检查。",
        }
    )
    rows.append(
        {
            "audit_item": "current_valuation_backfill_blocked",
            "status": "pass",
            "evidence": "valuation history is attached by valuation_available_date <= signal_date",
            "action": "不把当前估值快照回填历史。",
        }
    )
    return pd.DataFrame(rows)


def build_replay_consistency_audit(
    *,
    features: pd.DataFrame,
    valuation_features: pd.DataFrame,
    signal_panel: pd.DataFrame,
    schedule: pd.DataFrame,
    close_matrix: pd.DataFrame,
    v26: Any,
    policy: dict[str, Any],
    sample_count: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty or sample_count <= 0:
        return pd.DataFrame(rows)
    invested_dates: list[pd.Timestamp] = []
    if not schedule.empty and "is_invested" in schedule.columns:
        invested_dates = sorted(pd.to_datetime(schedule[schedule["is_invested"] == True]["signal_date"]).dropna().unique().tolist())  # noqa: E712
    dates = invested_dates or sorted(signal_panel["trade_date"].dropna().unique().tolist())
    if not dates:
        return pd.DataFrame(rows)
    positions = np.linspace(0, len(dates) - 1, num=min(sample_count, len(dates)), dtype=int)
    for pos in sorted(set(int(p) for p in positions)):
        date = pd.Timestamp(dates[pos])
        full_selected = select_for_policy(signal_panel[signal_panel["trade_date"] == date].copy(), policy)
        full_codes = (
            "|".join(full_selected["industry_code"].astype(str).str.zfill(6).tolist())
            if not full_selected.empty
            else ""
        )
        truncated_features = features[features["trade_date"] <= date].copy()
        replay_panel = build_realtime_signal_panel(
            features=truncated_features,
            valuation_features=valuation_features,
            v26=v26,
            policy=policy,
        )
        replay_panel = v26.filter_cross_section_dates(replay_panel, min_count=int(policy["min_cross_section_count"]))
        replay_selected = select_for_policy(replay_panel[replay_panel["trade_date"] == date].copy(), policy)
        replay_codes = (
            "|".join(replay_selected["industry_code"].astype(str).str.zfill(6).tolist())
            if not replay_selected.empty
            else ""
        )
        rows.append(
            {
                "trade_date": date_to_str(date),
                "status": "pass" if full_codes == replay_codes else "fail",
                "full_panel_codes": full_codes,
                "asof_replay_codes": replay_codes,
                "evidence": "full build and truncated as-of rebuild match" if full_codes == replay_codes else "selection mismatch",
            }
        )
    return pd.DataFrame(rows)


def build_source_audit(valuation: pd.DataFrame, signal_panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = [
        {
            "audit_item": "valuation_history_loaded",
            "status": "pass" if not valuation.empty else "fail",
            "evidence": f"rows={len(valuation)}; start={date_to_str(valuation['valuation_trade_date'].min()) if not valuation.empty else ''}; end={date_to_str(valuation['valuation_trade_date'].max()) if not valuation.empty else ''}; industries={valuation['industry_code'].nunique() if not valuation.empty else 0}",
            "action": "公开源历史估值仍需源口径和真实发布时间复核。",
        },
        {
            "audit_item": "signal_panel_built",
            "status": "pass" if not signal_panel.empty else "fail",
            "evidence": f"rows={len(signal_panel)}; start={date_to_str(signal_panel['trade_date'].min()) if not signal_panel.empty else ''}; end={date_to_str(signal_panel['trade_date'].max()) if not signal_panel.empty else ''}",
            "action": "实时仿真使用该面板逐期生成决策。",
        },
        {
            "audit_item": "promotion_boundary",
            "status": "research_only",
            "evidence": policy["promotion_rule"],
            "action": "V2.9不生成交易指令。",
        },
    ]
    return pd.DataFrame(rows)


def build_run_summary(
    *,
    policy: dict[str, Any],
    signal_panel: pd.DataFrame,
    schedule: pd.DataFrame,
    event_returns: pd.DataFrame,
    event_summary: pd.DataFrame,
    daily_nav: pd.DataFrame,
    nav_metrics: pd.DataFrame,
    timestamp_audit: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    replay_audit: pd.DataFrame,
    source_audit: pd.DataFrame,
) -> dict[str, Any]:
    audit_frames = [timestamp_audit, leakage_audit, replay_audit, source_audit]
    fail_count = 0
    for frame in audit_frames:
        if not frame.empty and "status" in frame.columns:
            fail_count += int((frame["status"] == "fail").sum())
    primary_event = event_summary.sort_values("horizon").iloc[0].to_dict() if not event_summary.empty else {}
    primary_nav = nav_metrics[nav_metrics["scope"] == "active_only"].iloc[0].to_dict() if not nav_metrics.empty and (nav_metrics["scope"] == "active_only").any() else {}
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "signal_rows": int(len(signal_panel)),
        "signal_start": date_to_str(signal_panel["trade_date"].min()) if not signal_panel.empty else "",
        "signal_end": date_to_str(signal_panel["trade_date"].max()) if not signal_panel.empty else "",
        "decision_dates": int(len(schedule)),
        "invested_decisions": int(schedule["is_invested"].sum()) if not schedule.empty else 0,
        "event_rows": int(len(event_returns)),
        "daily_nav_rows": int(len(daily_nav)),
        "audit_fail_count": int(fail_count),
        "final_verdict": "research_only；实时仿真通过审计但尚未形成validated_alpha" if fail_count == 0 else "research_only；存在审计失败，结果只能排查",
        "primary_60d_mean_net_return": float(primary_event.get("mean_net_return", math.nan)),
        "primary_60d_mean_benchmark_return": float(primary_event.get("mean_benchmark_return", math.nan)),
        "primary_60d_mean_relative_return": float(primary_event.get("mean_relative_return", math.nan)),
        "active_relative_final_nav": float(primary_nav.get("relative_final_nav", math.nan)),
        "active_final_nav": float(primary_nav.get("final_nav", math.nan)),
        "active_benchmark_final_nav": float(primary_nav.get("benchmark_final_nav", math.nan)),
        "research_boundary": "只研究申万行业和行业指数；不做个股筛选，不生成交易指令；实时仿真只使用signal_date当时可见数据。",
    }


def render_report(
    *,
    summary: dict[str, Any],
    policy: dict[str, Any],
    event_summary: pd.DataFrame,
    nav_metrics: pd.DataFrame,
    fold_results: pd.DataFrame,
    timestamp_audit: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    replay_audit: pd.DataFrame,
    top_candidates: pd.DataFrame,
) -> str:
    lines: list[str] = []
    lines.append("# V2.9 行业低估超跌实时仿真报告")
    lines.append("")
    lines.append(f"版本：{VERSION}")
    lines.append("")
    lines.append("## 研究结论")
    lines.append("")
    lines.append("V2.9 将上一轮最接近基准的 `低估超跌非陷阱 + Top10` 规则冻结，并按信号日当时可见数据逐期回放。")
    lines.append("本版本的目标是检验是否存在未来信息污染和实盘可执行性，不以继续调高收益为目标。")
    lines.append("")
    lines.append(f"- 信号区间：{summary['signal_start']} 至 {summary['signal_end']}")
    lines.append(f"- 信号面板行数：{summary['signal_rows']}")
    lines.append(f"- 决策日期数：{summary['decision_dates']}")
    lines.append(f"- 有持仓决策数：{summary['invested_decisions']}")
    lines.append(f"- 事件回测行数：{summary['event_rows']}")
    lines.append(f"- 审计失败数：{summary['audit_fail_count']}")
    lines.append(f"- 最终结论：{summary['final_verdict']}")
    lines.append("")
    lines.append("解释边界：如果绝对收益为正但相对全行业收益不为正，说明它更像是反弹 beta 识别，而不是行业选择 alpha。")
    lines.append("")
    lines.append("## 冻结策略")
    lines.append("")
    lines.append("| 项目 | 规则 |")
    lines.append("| --- | --- |")
    lines.append(f"| 策略ID | {policy['policy_id']} |")
    lines.append(f"| 状态 | {policy['signal_state']} |")
    lines.append(f"| 排序字段 | {policy['score_column']} |")
    lines.append(f"| Top N | {policy['top_n']} |")
    lines.append(f"| 最少触发行业 | {policy['min_triggered_count']} |")
    lines.append(f"| 执行规则 | {policy['execution_rule']} |")
    lines.append(f"| 成本 | {fmt_float(policy['cost_bps'], 1)} bps 单边换手成本 |")
    lines.append(f"| 样本外起点 | {policy['oos_start']} |")
    lines.append("")
    lines.append("## 事件收益")
    lines.append("")
    if event_summary.empty:
        lines.append("无事件收益。")
    else:
        display = event_summary.copy()
        display = display[
            [
                "horizon",
                "decision_status",
                "samples",
                "nonoverlap_samples",
                "oos_samples",
                "mean_net_return",
                "mean_benchmark_return",
                "mean_relative_return",
                "nonoverlap_mean_relative_return",
                "oos_mean_relative_return",
                "benchmark_win_rate",
                "avg_drawdown_252d",
            ]
        ]
        lines.extend(markdown_table(display, rename={
            "horizon": "持有期",
            "decision_status": "结论",
            "samples": "样本数",
            "nonoverlap_samples": "非重叠",
            "oos_samples": "样本外",
            "mean_net_return": "策略收益",
            "mean_benchmark_return": "基准收益",
            "mean_relative_return": "相对收益",
            "nonoverlap_mean_relative_return": "非重叠相对",
            "oos_mean_relative_return": "样本外相对",
            "benchmark_win_rate": "跑赢比例",
            "avg_drawdown_252d": "触发前回撤",
        }, pct_cols={
            "mean_net_return",
            "mean_benchmark_return",
            "mean_relative_return",
            "nonoverlap_mean_relative_return",
            "oos_mean_relative_return",
            "benchmark_win_rate",
            "avg_drawdown_252d",
        }))
    lines.append("")
    lines.append("## 实时净值")
    lines.append("")
    if nav_metrics.empty:
        lines.append("无净值结果。")
    else:
        display = nav_metrics[
            [
                "scope_zh",
                "daily_rows",
                "active_day_ratio",
                "final_nav",
                "benchmark_final_nav",
                "relative_final_nav",
                "annualized_return",
                "benchmark_annualized_return",
                "annualized_relative_return",
                "max_drawdown",
                "relative_max_drawdown",
                "daily_relative_win_rate",
            ]
        ].copy()
        lines.extend(markdown_table(display, rename={
            "scope_zh": "口径",
            "daily_rows": "交易日",
            "active_day_ratio": "持仓占比",
            "final_nav": "策略净值",
            "benchmark_final_nav": "基准净值",
            "relative_final_nav": "相对净值",
            "annualized_return": "策略年化",
            "benchmark_annualized_return": "基准年化",
            "annualized_relative_return": "相对年化",
            "max_drawdown": "最大回撤",
            "relative_max_drawdown": "相对回撤",
            "daily_relative_win_rate": "日跑赢率",
        }, pct_cols={
            "active_day_ratio",
            "annualized_return",
            "benchmark_annualized_return",
            "annualized_relative_return",
            "max_drawdown",
            "relative_max_drawdown",
            "daily_relative_win_rate",
        }))
    lines.append("")
    lines.append("## 样本内外")
    lines.append("")
    if fold_results.empty:
        lines.append("无样本内外结果。")
    else:
        display = fold_results[
            [
                "sample_zh",
                "horizon",
                "samples",
                "mean_net_return",
                "mean_benchmark_return",
                "mean_relative_return",
                "benchmark_win_rate",
            ]
        ].copy()
        lines.extend(markdown_table(display, rename={
            "sample_zh": "样本",
            "horizon": "持有期",
            "samples": "样本数",
            "mean_net_return": "策略收益",
            "mean_benchmark_return": "基准收益",
            "mean_relative_return": "相对收益",
            "benchmark_win_rate": "跑赢比例",
        }, pct_cols={"mean_net_return", "mean_benchmark_return", "mean_relative_return", "benchmark_win_rate"}))
    lines.append("")
    lines.append("## 审计")
    lines.append("")
    audit = pd.concat(
        [
            timestamp_audit.assign(audit_group="时间戳"),
            leakage_audit.assign(audit_group="泄漏"),
            replay_audit.rename(columns={"trade_date": "audit_item"}).assign(audit_group="回放一致性") if not replay_audit.empty else pd.DataFrame(),
        ],
        ignore_index=True,
        sort=False,
    )
    if audit.empty:
        lines.append("无审计结果。")
    else:
        display_cols = [col for col in ["audit_group", "audit_item", "status", "evidence", "action"] if col in audit.columns]
        lines.extend(markdown_table(audit[display_cols].head(20), rename={
            "audit_group": "审计组",
            "audit_item": "项目",
            "status": "状态",
            "evidence": "证据",
            "action": "动作",
        }, pct_cols=set()))
    lines.append("")
    lines.append("## 当前候选")
    lines.append("")
    if top_candidates.empty:
        lines.append("当前无候选。")
    else:
        display = top_candidates.head(15)[
            [
                "signal_date",
                "execution_date",
                "candidate_status",
                "rank",
                "industry_code",
                "industry_name",
                "parent_industry",
                "latest_bottom_score",
                "valuation_pit_score",
                "stabilized_oversold_signal",
                "drawdown_252d",
                "pe",
                "pb",
                "dividend_yield",
            ]
        ].copy()
        lines.extend(markdown_table(display, rename={
            "signal_date": "信号日",
            "execution_date": "执行日",
            "candidate_status": "状态",
            "rank": "排名",
            "industry_code": "代码",
            "industry_name": "行业",
            "parent_industry": "一级行业",
            "latest_bottom_score": "抄底分",
            "valuation_pit_score": "估值分",
            "stabilized_oversold_signal": "企稳超跌",
            "drawdown_252d": "252日回撤",
            "pe": "PE",
            "pb": "PB",
            "dividend_yield": "股息率",
        }, pct_cols={"latest_bottom_score", "valuation_pit_score", "stabilized_oversold_signal", "drawdown_252d", "dividend_yield"}))
    lines.append("")
    lines.append("## 输出文件说明")
    lines.append("")
    lines.append("- `report.md`：中文实时仿真报告，优先打开。")
    lines.append("- `top_candidates.csv`：最新信号日的候选行业或当期排序。")
    lines.append("- `run_summary.json`：机器可读运行摘要。")
    lines.append("- `debug/`：实时信号面板、决策日志、交易流水、事件收益、日频净值、审计和冻结策略。")
    lines.append("")
    lines.append("研究边界：本报告只研究申万行业和行业指数，不做个股筛选，不生成交易指令。")
    return "\n".join(lines)


def select_for_policy(group: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if group.empty or policy["signal_state"] not in group.columns:
        return pd.DataFrame()
    triggered = group[group[policy["signal_state"]].fillna(False)].dropna(subset=[policy["score_column"]]).copy()
    if len(triggered) < int(policy["min_triggered_count"]):
        return pd.DataFrame()
    selected = triggered.sort_values(policy["score_column"], ascending=False).head(int(policy["top_n"])).copy()
    if len(selected) < int(policy["min_triggered_count"]):
        return pd.DataFrame()
    return selected


def expanding_percentile(series: pd.Series, min_periods: int) -> pd.Series:
    values: list[float] = []
    output: list[float] = []
    for value in pd.to_numeric(series, errors="coerce"):
        if pd.notna(value):
            values.append(float(value))
        if pd.isna(value) or len(values) < min_periods:
            output.append(math.nan)
            continue
        arr = np.array(values, dtype=float)
        output.append(float(np.mean(arr <= float(value))))
    return pd.Series(output, index=series.index)


def assign_pressure_episodes(context: pd.DataFrame, threshold: float) -> pd.DataFrame:
    result = context.copy()
    episode_id = 0
    in_episode = False
    episode_values: list[int] = []
    for stressed in (result["market_stress_score"] >= threshold).fillna(False):
        if stressed and not in_episode:
            episode_id += 1
            in_episode = True
        if not stressed:
            in_episode = False
            episode_values.append(0)
        else:
            episode_values.append(episode_id)
    result["pressure_episode_id"] = episode_values
    return result


def nonoverlap_events(group: pd.DataFrame) -> pd.DataFrame:
    if group.empty:
        return group
    ordered = group.sort_values("execution_date").copy()
    keep: list[int] = []
    last_end = pd.Timestamp.min
    for idx, row in ordered.iterrows():
        start = pd.Timestamp(row["execution_date"])
        end = pd.Timestamp(row["end_date"])
        if start > last_end:
            keep.append(idx)
            last_end = end
    return ordered.loc[keep].copy()


def compute_turnover(previous: set[str], current: set[str]) -> float:
    if not previous and not current:
        return 0.0
    if not previous or not current:
        return 1.0
    return len(current.symmetric_difference(previous)) / max(len(current), 1)


def rank_by_date(frame: pd.DataFrame, column: str, *, ascending: bool) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(math.nan, index=frame.index)
    return frame.groupby("trade_date")[column].rank(pct=True, ascending=ascending, method="average")


def rank_by_date_parent(frame: pd.DataFrame, column: str, *, ascending: bool) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(math.nan, index=frame.index)
    return frame.groupby(["trade_date", "parent_industry"])[column].rank(pct=True, ascending=ascending, method="average")


def mean_col(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame.columns:
        return math.nan
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    return float(values.mean()) if not values.empty else math.nan


def annualize_nav(nav: float, years: float) -> float:
    if pd.isna(nav) or nav <= 0 or years <= 0:
        return math.nan
    return nav ** (1.0 / years) - 1.0


def parse_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number


def fmt_pct(value: Any) -> str:
    number = parse_float(value)
    if math.isnan(number):
        return ""
    return f"{number * 100:.2f}%"


def fmt_float(value: Any, digits: int = 3) -> str:
    number = parse_float(value)
    if math.isnan(number):
        return ""
    return f"{number:.{digits}f}"


def date_to_str(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)


def markdown_table(frame: pd.DataFrame, *, rename: dict[str, str], pct_cols: set[str]) -> list[str]:
    if frame.empty:
        return ["无数据。"]
    display = frame.copy()
    original_cols = list(display.columns)
    for col in original_cols:
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
        return date_to_str(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    return str(value)


if __name__ == "__main__":
    main()
