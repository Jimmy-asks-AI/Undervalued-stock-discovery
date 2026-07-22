#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from dataclasses import dataclass
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
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_valuation_quality_v2_7"
V26_SCRIPT = ROOT / "scripts" / "run_industry_valuation_pit_validation_v2_6.py"
VERSION = "2.7.0"


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    strategy_zh: str
    signal_col: str
    gate_col: str
    description: str


STRATEGIES = [
    StrategySpec(
        "quality_core",
        "V2.7：估值质量核心",
        "valuation_quality_core_score",
        "eligible_quality_core",
        "只验证估值数据质量、估值稳定性、股息连续性、市值深度和价格质量，不强行抄底。",
    ),
    StrategySpec(
        "quality_value_blend",
        "V2.7：质量+适度估值",
        "valuation_quality_value_score",
        "eligible_quality_value",
        "在质量核心上加入自身历史低估、父行业相对低估和绝对低估，但不让低估值单独主导。",
    ),
    StrategySpec(
        "quality_defensive",
        "V2.7：质量+防守",
        "valuation_quality_defensive_score",
        "eligible_quality_defensive",
        "估值质量叠加低波动、回撤控制、流动性和股息支持，检验防守型行业选择能力。",
    ),
    StrategySpec(
        "post_2022_quality_core",
        "V2.7：新体系估值质量",
        "post_2022_quality_score",
        "eligible_post_2022_quality",
        "只在 2022 年后申万二级行业覆盖更完整的样本内验证估值质量信号。",
    ),
    StrategySpec(
        "quality_value_no_trap",
        "V2.7：质量价值非陷阱",
        "quality_value_no_trap_score",
        "eligible_quality_value_no_trap",
        "质量和适度估值叠加非动量陷阱过滤，检验能否避开低估值陷阱。",
    ),
    StrategySpec(
        "quality_broad_no_sector_filter",
        "V2.7：质量宽口径对照",
        "valuation_quality_core_score",
        "eligible_quality_broad",
        "不剔除金融地产建筑的宽口径对照，用于观察行业结构偏差。",
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.7 industry valuation quality validation.")
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="Historical price/return feature panel.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current industry name and parent mapping.")
    parser.add_argument("--valuation", default=str(DEFAULT_VALUATION), help="SWS daily industry valuation history.")
    parser.add_argument("--history-dir", default=str(DEFAULT_HISTORY_DIR), help="Cached industry index histories for daily NAV.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--horizons", default="60,120,252", help="Forward holding horizons.")
    parser.add_argument("--top-ns", default="20,30", help="Top N baskets. V2.7 avoids Top5/Top10 tail fitting by default.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="One-way rebalance cost in bps.")
    parser.add_argument("--release-lag-days", type=int, default=1, help="Conservative lag from valuation trade_date to available_date.")
    parser.add_argument("--min-valuation-days", type=int, default=252, help="Minimum trailing valuation rows for PIT valuation gates.")
    parser.add_argument("--min-cross-section-count", type=int, default=20, help="Minimum industries per feature date.")
    parser.add_argument("--stress-threshold", type=float, default=0.65, help="Inherited market pressure threshold.")
    parser.add_argument("--extreme-stress-threshold", type=float, default=0.80, help="Inherited extreme pressure threshold.")
    parser.add_argument("--post-2022-start", default="2022-01-01", help="Start date for the more complete industry universe.")
    parser.add_argument("--bootstrap-rounds", type=int, default=500, help="Block bootstrap rounds.")
    parser.add_argument("--oos-split-ratio", type=float, default=0.70, help="Walk-forward split ratio.")
    parser.add_argument("--rebalance-step-days", type=int, default=20, help="Feature-panel rebalance step approximation.")
    args = parser.parse_args()

    output_dir = Path(args.output)
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    horizons = parse_int_list(args.horizons)
    top_ns = parse_int_list(args.top_ns)
    v26 = load_v26_module()
    v23 = v26.load_v23_module()

    raw_features = v23.load_features(Path(args.features))
    names = v23.load_names(Path(args.ranking))
    features = v23.attach_names(raw_features, names)
    valuation = v26.load_valuation_history(Path(args.valuation), release_lag_days=args.release_lag_days)
    valuation_features = v26.build_valuation_feature_panel(valuation)
    base_signal = v26.build_v26_signal_panel(
        v23=v23,
        features=features,
        valuation_features=valuation_features,
        stress_threshold=args.stress_threshold,
        extreme_stress_threshold=args.extreme_stress_threshold,
        min_valuation_days=args.min_valuation_days,
    )
    signal_panel = build_v27_signal_panel(base_signal, post_2022_start=args.post_2022_start)
    signal_panel = v26.filter_cross_section_dates(signal_panel, min_count=args.min_cross_section_count)

    event_backtest = compute_event_backtest(signal_panel, horizons, top_ns, args.cost_bps)
    previous_strategies = v23.STRATEGIES
    v23.STRATEGIES = [
        v23.StrategySpec(
            strategy.strategy_id,
            strategy.strategy_zh,
            strategy.signal_col,
            strategy.gate_col,
            "valuation_quality_candidate",
            strategy.description,
        )
        for strategy in STRATEGIES
    ]
    try:
        nonoverlap = v23.compute_nonoverlap_backtest(event_backtest, args.rebalance_step_days)
        walk_forward = v23.compute_walk_forward_oos(event_backtest, args.oos_split_ratio)
        bootstrap = v23.compute_bootstrap_confidence(event_backtest, args.bootstrap_rounds)
        close_matrix = v23.load_close_matrix(Path(args.history_dir), signal_panel["industry_code"].dropna().unique().tolist())
        daily_nav = v23.compute_daily_portfolio_nav(signal_panel, close_matrix, top_ns, args.cost_bps)
        nav_metrics = v23.compute_nav_metrics(daily_nav)
    finally:
        v23.STRATEGIES = previous_strategies

    parameter_sensitivity = compute_parameter_sensitivity(event_backtest, nonoverlap, walk_forward, bootstrap, nav_metrics)
    rejection_log = build_signal_rejection_log(parameter_sensitivity)
    top_candidates = build_top_candidates(parameter_sensitivity, rejection_log)
    rankic_report = compute_rankic_report(signal_panel, horizons)
    group_return_report = compute_group_return_report(signal_panel, horizons)
    period_validation = compute_period_validation(event_backtest)
    yearly_validation = compute_yearly_validation(event_backtest)
    universe_breaks = v26.compute_universe_breaks(valuation)
    source_audit = build_source_audit(
        valuation=valuation,
        signal_panel=signal_panel,
        args=args,
        universe_breaks=universe_breaks,
    )
    current_snapshot = build_current_signal_snapshot(signal_panel)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    signal_panel.to_csv(debug_dir / "valuation_quality_signal_panel.csv", index=False, encoding="utf-8-sig")
    event_backtest.to_csv(debug_dir / "event_backtest.csv", index=False, encoding="utf-8-sig")
    nonoverlap.to_csv(debug_dir / "nonoverlap_backtest.csv", index=False, encoding="utf-8-sig")
    walk_forward.to_csv(debug_dir / "walk_forward_oos.csv", index=False, encoding="utf-8-sig")
    bootstrap.to_csv(debug_dir / "bootstrap_confidence.csv", index=False, encoding="utf-8-sig")
    daily_nav.to_csv(debug_dir / "daily_portfolio_nav.csv", index=False, encoding="utf-8-sig")
    nav_metrics.to_csv(debug_dir / "portfolio_nav_metrics.csv", index=False, encoding="utf-8-sig")
    parameter_sensitivity.to_csv(debug_dir / "parameter_sensitivity.csv", index=False, encoding="utf-8-sig")
    rejection_log.to_csv(debug_dir / "signal_rejection_log.csv", index=False, encoding="utf-8-sig")
    rankic_report.to_csv(debug_dir / "rankic_report.csv", index=False, encoding="utf-8-sig")
    group_return_report.to_csv(debug_dir / "group_return_report.csv", index=False, encoding="utf-8-sig")
    period_validation.to_csv(debug_dir / "period_validation_report.csv", index=False, encoding="utf-8-sig")
    yearly_validation.to_csv(debug_dir / "yearly_validation_report.csv", index=False, encoding="utf-8-sig")
    universe_breaks.to_csv(debug_dir / "valuation_universe_breaks.csv", index=False, encoding="utf-8-sig")
    source_audit.to_csv(debug_dir / "source_audit.csv", index=False, encoding="utf-8-sig")
    current_snapshot.to_csv(debug_dir / "current_signal_snapshot.csv", index=False, encoding="utf-8-sig")

    summary = build_summary(
        signal_panel=signal_panel,
        valuation=valuation,
        event_backtest=event_backtest,
        nonoverlap=nonoverlap,
        daily_nav=daily_nav,
        parameter_sensitivity=parameter_sensitivity,
        rejection_log=rejection_log,
        source_audit=source_audit,
        args=args,
        horizons=horizons,
        top_ns=top_ns,
    )
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            rankic_report=rankic_report,
            group_return_report=group_return_report,
            period_validation=period_validation,
            nav_metrics=nav_metrics,
            source_audit=source_audit,
            universe_breaks=universe_breaks,
            current_snapshot=current_snapshot,
        ),
        encoding="utf-8",
    )

    print(f"V{VERSION} 行业估值质量验证完成")
    print(f"信号面板行数={summary['signal_rows']}")
    print(f"事件回测行数={summary['event_rows']}")
    print(f"候选待源审计信号数={summary['candidate_requires_source_audit_count']}")
    print(f"条件观察信号数={summary['conditional_observation_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v26_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_valuation_pit_validation_v2_6", V26_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load V2.6 module from {V26_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_v27_signal_panel(base: pd.DataFrame, *, post_2022_start: str) -> pd.DataFrame:
    frame = base.copy()
    frame["post_2022_sample_flag"] = frame["trade_date"] >= pd.Timestamp(post_2022_start)
    frame["legacy_universe_sample_flag"] = ~frame["post_2022_sample_flag"]

    sector_gap_parents = {"银行", "非银金融", "房地产", "建筑装饰"}
    parent = frame["parent_industry"].fillna("")
    frame["sector_quality_exclusion_flag"] = parent.isin(sector_gap_parents) | frame.get(
        "sector_quality_data_required_flag", False
    )

    frame["valuation_quality_core_score_raw"] = (
        0.45 * frame["industry_valuation_quality_score"].fillna(0.0)
        + 0.20 * frame["pe_stability_score"].fillna(0.0)
        + 0.15 * frame["pb_stability_score"].fillna(0.0)
        + 0.10 * frame["dividend_positive_ratio_252"].fillna(0.0)
        + 0.10 * frame["market_depth_score"].fillna(0.0)
    )
    frame["valuation_quality_core_score"] = rank_by_date(frame, "valuation_quality_core_score_raw", ascending=True)

    frame["valuation_quality_value_score_raw"] = (
        0.50 * frame["valuation_quality_core_score"].fillna(0.0)
        + 0.20 * frame["historical_valuation_score"].fillna(0.0)
        + 0.15 * frame["parent_relative_value_score"].fillna(0.0)
        + 0.15 * frame["valuation_pit_score"].fillna(0.0)
    )
    frame["valuation_quality_value_score"] = rank_by_date(frame, "valuation_quality_value_score_raw", ascending=True)

    frame["valuation_quality_defensive_score_raw"] = (
        0.45 * frame["valuation_quality_core_score"].fillna(0.0)
        + 0.20 * frame["low_volatility_quality_score"].fillna(0.0)
        + 0.15 * frame["drawdown_quality_score"].fillna(0.0)
        + 0.10 * frame["liquidity_quality_score"].fillna(0.0)
        + 0.10 * frame["dividend_positive_ratio_252"].fillna(0.0)
    )
    frame["valuation_quality_defensive_score"] = rank_by_date(frame, "valuation_quality_defensive_score_raw", ascending=True)

    frame["post_2022_quality_score_raw"] = (
        0.55 * frame["valuation_quality_core_score"].fillna(0.0)
        + 0.20 * frame["valuation_quality_value_score"].fillna(0.0)
        + 0.15 * frame["price_quality_composite"].fillna(0.0)
        + 0.10 * frame["market_depth_score"].fillna(0.0)
    )
    frame["post_2022_quality_score"] = rank_by_date(frame, "post_2022_quality_score_raw", ascending=True)

    frame["quality_value_no_trap_score_raw"] = (
        0.45 * frame["valuation_quality_value_score"].fillna(0.0)
        + 0.25 * frame["price_quality_composite"].fillna(0.0)
        + 0.15 * frame["recovery_quality_score"].fillna(0.0)
        + 0.15 * frame["market_depth_score"].fillna(0.0)
        - 0.15 * frame["momentum_trap_score"].clip(upper=3).fillna(0.0)
    )
    frame["quality_value_no_trap_score"] = rank_by_date(frame, "quality_value_no_trap_score_raw", ascending=True)

    frame["eligible_quality_broad"] = frame["valuation_history_gate"] & (frame["industry_valuation_quality_score"] >= 0.45)
    frame["eligible_quality_core"] = (
        frame["valuation_history_gate"]
        & (frame["valuation_quality_core_score"] >= 0.55)
        & (~frame["sector_quality_exclusion_flag"])
    )
    frame["eligible_quality_value"] = (
        frame["eligible_quality_core"]
        & (frame["valuation_quality_value_score"] >= 0.55)
        & (frame["historical_valuation_score"] >= 0.35)
    )
    frame["eligible_quality_defensive"] = (
        frame["eligible_quality_core"]
        & (frame["low_volatility_quality_score"] >= 0.45)
        & (frame["drawdown_quality_score"] >= 0.40)
        & (frame["liquidity_quality_score"] >= 0.35)
    )
    frame["eligible_post_2022_quality"] = frame["eligible_quality_core"] & frame["post_2022_sample_flag"]
    frame["eligible_quality_value_no_trap"] = (
        frame["eligible_quality_value"]
        & frame["not_momentum_trap"]
        & (frame["momentum_trap_score"] <= 1)
        & (frame["price_quality_composite"] >= 0.40)
    )

    drop_cols = [
        "valuation_quality_core_score_raw",
        "valuation_quality_value_score_raw",
        "valuation_quality_defensive_score_raw",
        "post_2022_quality_score_raw",
        "quality_value_no_trap_score_raw",
    ]
    return frame.drop(columns=drop_cols, errors="ignore").sort_values(["trade_date", "industry_code"])


def rank_by_date(frame: pd.DataFrame, column: str, *, ascending: bool) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index)
    return frame.groupby("trade_date")[column].rank(pct=True, ascending=ascending, method="average")


def compute_event_backtest(signal_panel: pd.DataFrame, horizons: list[int], top_ns: list[int], cost_bps: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty:
        return pd.DataFrame(rows)
    grouped = list(signal_panel.groupby("trade_date", sort=True))
    for strategy in STRATEGIES:
        for top_n in top_ns:
            for horizon in horizons:
                label = f"forward_return_{horizon}d"
                benchmark_label = f"benchmark_forward_return_{horizon}d"
                if label not in signal_panel.columns:
                    continue
                previous: set[str] = set()
                for trade_date, group in grouped:
                    eligible = group[group[strategy.gate_col].fillna(False)].dropna(subset=[strategy.signal_col, label]).copy()
                    if len(eligible) < top_n:
                        continue
                    selected = eligible.sort_values(strategy.signal_col, ascending=False).head(top_n).copy()
                    current = set(selected["industry_code"].astype(str).tolist())
                    turnover = len(current.symmetric_difference(previous)) / max(len(current), 1) if previous else 1.0
                    gross = float(selected[label].mean())
                    benchmark = float(group[benchmark_label].dropna().mean()) if benchmark_label in group.columns else float(group[label].mean())
                    cost = turnover * cost_bps / 10000.0
                    rows.append(
                        {
                            "trade_date": date_to_str(trade_date),
                            "strategy_id": strategy.strategy_id,
                            "strategy_zh": strategy.strategy_zh,
                            "top_n": int(top_n),
                            "horizon": int(horizon),
                            "eligible_count": int(len(eligible)),
                            "gross_forward_return": gross,
                            "turnover": turnover,
                            "cost_bps": cost_bps,
                            "net_forward_return": gross - cost,
                            "benchmark_forward_return": benchmark,
                            "benchmark_relative_return": gross - cost - benchmark,
                            "market_stress_score": mean_col(selected, "market_stress_score"),
                            "pressure_tier": first_value(selected, "pressure_tier"),
                            "pressure_episode_id": first_nonempty(selected, "pressure_episode_id"),
                            "market_regime": first_value(selected, "market_regime"),
                            "volatility_regime": first_value(selected, "volatility_regime"),
                            "post_2022_sample_flag": bool(pd.Timestamp(trade_date) >= pd.Timestamp("2022-01-01")),
                            "sector_exclusion_rate": mean_bool(selected, "sector_quality_exclusion_flag"),
                            "avg_momentum_trap_score": mean_col(selected, "momentum_trap_score"),
                            "avg_price_quality_composite": mean_col(selected, "price_quality_composite"),
                            "avg_liquidity_quality_score": mean_col(selected, "liquidity_quality_score"),
                            "avg_low_volatility_quality_score": mean_col(selected, "low_volatility_quality_score"),
                            "avg_recovery_quality_score": mean_col(selected, "recovery_quality_score"),
                            "avg_valuation_pit_score": mean_col(selected, "valuation_pit_score"),
                            "avg_valuation_quality_score": mean_col(selected, "industry_valuation_quality_score"),
                            "avg_quality_core_score": mean_col(selected, "valuation_quality_core_score"),
                            "avg_quality_value_score": mean_col(selected, "valuation_quality_value_score"),
                            "avg_quality_defensive_score": mean_col(selected, "valuation_quality_defensive_score"),
                            "avg_post_2022_quality_score": mean_col(selected, "post_2022_quality_score"),
                            "avg_parent_relative_value_score": mean_col(selected, "parent_relative_value_score"),
                            "avg_historical_valuation_score": mean_col(selected, "historical_valuation_score"),
                            "avg_pe": mean_col(selected, "pe"),
                            "avg_pb": mean_col(selected, "pb"),
                            "avg_dividend_yield": mean_col(selected, "dividend_yield"),
                            "avg_valuation_age_days": mean_col(selected, "valuation_age_days"),
                            "selected_industry_codes": "|".join(selected["industry_code"].astype(str).tolist()),
                            "selected_industries": "|".join(selected["industry_name"].fillna(selected["industry_code"]).astype(str).tolist()),
                        }
                    )
                    previous = current
    return pd.DataFrame(rows)


def compute_parameter_sensitivity(
    event_backtest: pd.DataFrame,
    nonoverlap: pd.DataFrame,
    walk_forward: pd.DataFrame,
    bootstrap: pd.DataFrame,
    nav_metrics: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_backtest.empty:
        return pd.DataFrame(rows)
    for keys, group in event_backtest.groupby(["strategy_id", "strategy_zh", "top_n", "horizon"], sort=True):
        strategy_id, strategy_zh, top_n, horizon = keys
        row = metric_row(group, strategy_id, strategy_zh, top_n, horizon)
        non = nonoverlap[
            (nonoverlap["strategy_id"] == strategy_id)
            & (nonoverlap["top_n"] == top_n)
            & (nonoverlap["horizon"] == horizon)
        ]
        row["nonoverlap_samples"] = int(len(non))
        row["nonoverlap_mean_relative_return"] = float(non["benchmark_relative_return"].mean()) if not non.empty else math.nan
        row["nonoverlap_benchmark_win_rate"] = float((non["benchmark_relative_return"] > 0).mean()) if not non.empty else math.nan

        oos = walk_forward[
            (walk_forward["strategy_id"] == strategy_id)
            & (walk_forward["top_n"] == top_n)
            & (walk_forward["horizon"] == horizon)
            & (walk_forward["sample"] == "out_of_sample")
        ]
        row["oos_samples"] = int(oos.iloc[0]["samples"]) if not oos.empty else 0
        row["oos_mean_relative_return"] = float(oos.iloc[0]["mean_relative_return"]) if not oos.empty else math.nan
        row["oos_benchmark_win_rate"] = float(oos.iloc[0]["benchmark_win_rate"]) if not oos.empty else math.nan

        boot = bootstrap[
            (bootstrap["strategy_id"] == strategy_id)
            & (bootstrap["top_n"] == top_n)
            & (bootstrap["horizon"] == horizon)
        ]
        for col in ["block_count", "ci_5", "ci_50", "ci_95", "probability_positive"]:
            row[f"bootstrap_{col}"] = boot.iloc[0][col] if not boot.empty else math.nan

        nav = nav_metrics[(nav_metrics["strategy_id"] == strategy_id) & (nav_metrics["top_n"] == top_n)]
        if not nav.empty:
            nav_row = nav.iloc[0]
            row["nav_daily_rows"] = int(nav_row["daily_rows"])
            row["relative_final_nav"] = float(nav_row["relative_final_nav"])
            row["annualized_relative_return"] = float(nav_row["annualized_relative_return"])
            row["relative_max_drawdown"] = float(nav_row["relative_max_drawdown"])
            row["daily_relative_win_rate"] = float(nav_row["daily_relative_win_rate"])
        else:
            row["nav_daily_rows"] = 0
            row["relative_final_nav"] = math.nan
            row["annualized_relative_return"] = math.nan
            row["relative_max_drawdown"] = math.nan
            row["daily_relative_win_rate"] = math.nan

        raw_score = (
            safe_number(row["mean_relative_return"])
            + safe_number(row["oos_mean_relative_return"])
            + safe_number(row["nonoverlap_mean_relative_return"])
            + (safe_number(row["annualized_relative_return"]) if row["nav_daily_rows"] >= 252 else 0.0)
            + 0.02 * safe_number(row["benchmark_win_rate"])
            + 0.02 * safe_number(row["bootstrap_probability_positive"])
        )
        sample_strength = min(
            safe_number(row["samples"]) / 80.0,
            safe_number(row["nonoverlap_samples"]) / 20.0,
            safe_number(row["oos_samples"]) / 20.0 if safe_number(row["oos_samples"]) > 0 else 0.0,
            safe_number(row["nav_daily_rows"]) / 252.0 if safe_number(row["nav_daily_rows"]) > 0 else 0.0,
            1.0,
        )
        row["sample_strength"] = sample_strength
        row["robust_score"] = raw_score * (0.15 + 0.85 * sample_strength)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("robust_score", ascending=False)


def metric_row(group: pd.DataFrame, strategy_id: str, strategy_zh: str, top_n: int, horizon: int) -> dict[str, Any]:
    row = {
        "strategy_id": strategy_id,
        "strategy_zh": strategy_zh,
        "top_n": int(top_n),
        "horizon": int(horizon),
        "samples": int(len(group)),
        "mean_net_return": float(group["net_forward_return"].mean()),
        "median_net_return": float(group["net_forward_return"].median()),
        "mean_benchmark_return": float(group["benchmark_forward_return"].mean()),
        "mean_relative_return": float(group["benchmark_relative_return"].mean()),
        "win_rate": float((group["net_forward_return"] > 0).mean()),
        "benchmark_win_rate": float((group["benchmark_relative_return"] > 0).mean()),
        "mean_turnover": float(group["turnover"].mean()),
        "worst_net_return": float(group["net_forward_return"].min()),
        "best_net_return": float(group["net_forward_return"].max()),
        "mean_eligible_count": float(group["eligible_count"].mean()),
        "mean_market_stress_score": float(group["market_stress_score"].mean()),
    }
    for col in [
        "avg_momentum_trap_score",
        "avg_price_quality_composite",
        "avg_liquidity_quality_score",
        "avg_low_volatility_quality_score",
        "avg_recovery_quality_score",
        "avg_valuation_pit_score",
        "avg_valuation_quality_score",
        "avg_quality_core_score",
        "avg_quality_value_score",
        "avg_quality_defensive_score",
        "avg_post_2022_quality_score",
        "avg_parent_relative_value_score",
        "avg_historical_valuation_score",
        "avg_pe",
        "avg_pb",
        "avg_dividend_yield",
        "avg_valuation_age_days",
        "sector_exclusion_rate",
    ]:
        row[col] = float(group[col].mean()) if col in group.columns else math.nan
    return row


def build_signal_rejection_log(parameter_sensitivity: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if parameter_sensitivity.empty:
        return pd.DataFrame(rows)
    for row in parameter_sensitivity.to_dict("records"):
        checks = {
            "full_positive": row["mean_relative_return"] > 0,
            "oos_positive": row["oos_mean_relative_return"] > 0,
            "nonoverlap_positive": row["nonoverlap_mean_relative_return"] > 0,
            "full_win_rate": row["benchmark_win_rate"] > 0.50,
            "nonoverlap_win_rate": row["nonoverlap_benchmark_win_rate"] > 0.50,
            "bootstrap_ci": row["bootstrap_ci_5"] > 0,
            "bootstrap_prob": row["bootstrap_probability_positive"] > 0.60,
            "nav_positive": row["relative_final_nav"] > 1.0,
            "sample_enough": row["samples"] >= 80
            and row["nonoverlap_samples"] >= 20
            and row["oos_samples"] >= 20
            and row.get("nav_daily_rows", 0) >= 252,
        }
        reasons = []
        if not checks["full_positive"]:
            reasons.append("全样本相对收益不为正")
        if not checks["oos_positive"]:
            reasons.append("样本外相对收益不为正")
        if not checks["nonoverlap_positive"]:
            reasons.append("非重叠相对收益不为正")
        if not checks["full_win_rate"]:
            reasons.append("全样本跑赢比例不足50%")
        if not checks["nonoverlap_win_rate"]:
            reasons.append("非重叠跑赢比例不足50%")
        if not checks["bootstrap_ci"]:
            reasons.append("bootstrap置信下沿不为正")
        if not checks["bootstrap_prob"]:
            reasons.append("bootstrap正收益概率不足60%")
        if not checks["nav_positive"]:
            reasons.append("逐日相对净值不大于1")
        if not checks["sample_enough"]:
            reasons.append("有效样本不足")

        if all(checks.values()):
            status = "candidate_requires_source_audit"
            reasons.append("量化门槛通过，但公开估值源仍需口径、发布时间和授权源交叉审计")
        elif any([checks["full_positive"], checks["oos_positive"], checks["nonoverlap_positive"]]):
            status = "conditional_observation"
        else:
            status = "rejected_signal"

        rows.append(
            {
                "strategy_id": row["strategy_id"],
                "strategy_zh": row["strategy_zh"],
                "top_n": int(row["top_n"]),
                "horizon": int(row["horizon"]),
                "signal_status": status,
                "mean_relative_return": row["mean_relative_return"],
                "oos_mean_relative_return": row["oos_mean_relative_return"],
                "nonoverlap_mean_relative_return": row["nonoverlap_mean_relative_return"],
                "bootstrap_ci_5": row["bootstrap_ci_5"],
                "bootstrap_probability_positive": row["bootstrap_probability_positive"],
                "relative_final_nav": row["relative_final_nav"],
                "annualized_relative_return": row["annualized_relative_return"],
                "nav_daily_rows": int(row.get("nav_daily_rows", 0)),
                "sample_strength": row.get("sample_strength", 0.0),
                "samples": int(row["samples"]),
                "nonoverlap_samples": int(row["nonoverlap_samples"]),
                "oos_samples": int(row["oos_samples"]),
                "rejection_reasons": "；".join(reasons) if reasons else "通过",
            }
        )
    return pd.DataFrame(rows)


def build_top_candidates(parameter_sensitivity: pd.DataFrame, rejection_log: pd.DataFrame) -> pd.DataFrame:
    if parameter_sensitivity.empty:
        return pd.DataFrame()
    merged = parameter_sensitivity.merge(
        rejection_log[["strategy_id", "top_n", "horizon", "signal_status", "rejection_reasons"]],
        on=["strategy_id", "top_n", "horizon"],
        how="left",
    )
    rows: list[dict[str, Any]] = []
    for row in merged.sort_values("robust_score", ascending=False).head(30).to_dict("records"):
        rows.append(
            {
                "策略": row["strategy_zh"],
                "TopN": int(row["top_n"]),
                "持有期": int(row["horizon"]),
                "状态": translate_status(str(row.get("signal_status", ""))),
                "全样本相对收益": fmt_pct(row["mean_relative_return"]),
                "样本外相对收益": fmt_pct(row["oos_mean_relative_return"]),
                "非重叠相对收益": fmt_pct(row["nonoverlap_mean_relative_return"]),
                "Bootstrap下沿": fmt_pct(row["bootstrap_ci_5"]),
                "Bootstrap为正概率": fmt_pct(row["bootstrap_probability_positive"]),
                "逐日相对净值": fmt_float(row["relative_final_nav"], 3),
                "年化相对收益": fmt_pct(row["annualized_relative_return"]),
                "样本强度": fmt_pct(row.get("sample_strength", 0.0)),
                "全样本跑赢比例": fmt_pct(row["benchmark_win_rate"]),
                "平均质量核心分": fmt_pct(row.get("avg_quality_core_score", math.nan)),
                "平均估值质量分": fmt_pct(row.get("avg_valuation_quality_score", math.nan)),
                "平均估值分": fmt_pct(row.get("avg_valuation_pit_score", math.nan)),
                "平均PE": fmt_float(row.get("avg_pe", math.nan), 2),
                "平均PB": fmt_float(row.get("avg_pb", math.nan), 2),
                "平均股息率": fmt_pct(row.get("avg_dividend_yield", math.nan)),
                "样本数": int(row["samples"]),
                "非重叠样本数": int(row["nonoverlap_samples"]),
                "拒绝或保留原因": row.get("rejection_reasons", ""),
            }
        )
    return pd.DataFrame(rows)


def compute_rankic_report(signal_panel: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    factor_map = {
        "industry_valuation_quality_score": "原始估值质量代理",
        "valuation_quality_core_score": "V2.7估值质量核心",
        "valuation_quality_value_score": "V2.7质量+适度估值",
        "valuation_quality_defensive_score": "V2.7质量+防守",
        "post_2022_quality_score": "V2.7新体系质量",
        "quality_value_no_trap_score": "V2.7质量价值非陷阱",
        "valuation_pit_score": "V2.6纯估值对照",
    }
    rows: list[dict[str, Any]] = []
    for factor, factor_zh in factor_map.items():
        if factor not in signal_panel.columns:
            continue
        for horizon in horizons:
            label = f"benchmark_relative_return_{horizon}d"
            if label not in signal_panel.columns:
                continue
            rankics: list[float] = []
            for _, group in signal_panel.groupby("trade_date", sort=True):
                sample = group[[factor, label]].dropna()
                if len(sample) < 8:
                    continue
                factor_rank = sample[factor].rank()
                label_rank = sample[label].rank()
                if factor_rank.nunique() < 2 or label_rank.nunique() < 2:
                    continue
                corr = factor_rank.corr(label_rank)
                if not pd.isna(corr):
                    rankics.append(float(corr))
            rows.append(
                {
                    "factor": factor,
                    "factor_zh": factor_zh,
                    "horizon": int(horizon),
                    "mean_rankic": float(np.mean(rankics)) if rankics else math.nan,
                    "rankic_t_stat": mean_t_stat(rankics),
                    "positive_ratio": float(np.mean(np.array(rankics) > 0)) if rankics else math.nan,
                    "date_count": int(len(rankics)),
                }
            )
    return pd.DataFrame(rows)


def compute_group_return_report(signal_panel: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    factors = [
        "valuation_quality_core_score",
        "valuation_quality_value_score",
        "valuation_quality_defensive_score",
        "post_2022_quality_score",
        "quality_value_no_trap_score",
        "valuation_pit_score",
    ]
    rows: list[dict[str, Any]] = []
    for factor in factors:
        if factor not in signal_panel.columns:
            continue
        for horizon in horizons:
            label = f"benchmark_relative_return_{horizon}d"
            if label not in signal_panel.columns:
                continue
            dated: list[pd.DataFrame] = []
            for trade_date, group in signal_panel.groupby("trade_date", sort=True):
                sample = group[[factor, label]].dropna().copy()
                if len(sample) < 20:
                    continue
                try:
                    sample["quantile"] = pd.qcut(sample[factor], 5, labels=False, duplicates="drop") + 1
                except ValueError:
                    continue
                sample["trade_date"] = trade_date
                dated.append(sample)
            if not dated:
                continue
            all_groups = pd.concat(dated, ignore_index=True)
            for quantile, group in all_groups.groupby("quantile", sort=True):
                rows.append(
                    {
                        "factor": factor,
                        "horizon": int(horizon),
                        "quantile": int(quantile),
                        "mean_relative_return": float(group[label].mean()),
                        "win_rate": float((group[label] > 0).mean()),
                        "samples": int(len(group)),
                    }
                )
    return pd.DataFrame(rows)


def compute_period_validation(event_backtest: pd.DataFrame) -> pd.DataFrame:
    if event_backtest.empty:
        return pd.DataFrame()
    frame = event_backtest.copy()
    year = pd.to_datetime(frame["trade_date"]).dt.year
    frame["period"] = np.where(year <= 2021, "2015-2021旧行业体系", "2022-2026新行业体系")
    rows = []
    for keys, group in frame.groupby(["strategy_id", "strategy_zh", "top_n", "horizon", "period"], sort=True):
        rows.append(metric_row(group, *keys[:4]) | {"period": keys[4]})
    return pd.DataFrame(rows)


def compute_yearly_validation(event_backtest: pd.DataFrame) -> pd.DataFrame:
    if event_backtest.empty:
        return pd.DataFrame()
    frame = event_backtest.copy()
    frame["year"] = pd.to_datetime(frame["trade_date"]).dt.year
    rows = []
    for keys, group in frame.groupby(["strategy_id", "strategy_zh", "top_n", "horizon", "year"], sort=True):
        rows.append(metric_row(group, *keys[:4]) | {"year": int(keys[4])})
    return pd.DataFrame(rows)


def build_source_audit(
    *,
    valuation: pd.DataFrame,
    signal_panel: pd.DataFrame,
    args: argparse.Namespace,
    universe_breaks: pd.DataFrame,
) -> pd.DataFrame:
    old_universe = universe_breaks[universe_breaks["year"] <= 2021]["mean_industries_per_date"].mean()
    new_universe = universe_breaks[universe_breaks["year"] >= 2022]["mean_industries_per_date"].mean()
    sector_exclusion_rate = mean_bool(signal_panel, "sector_quality_exclusion_flag") if not signal_panel.empty else math.nan
    return pd.DataFrame(
        [
            {
                "audit_item": "valuation_history_loaded",
                "status": "pass" if not valuation.empty else "fail",
                "evidence": f"rows={len(valuation)}; start={date_to_str(valuation['valuation_trade_date'].min())}; end={date_to_str(valuation['valuation_trade_date'].max())}; industries={valuation['industry_code'].nunique()}",
                "action": "继续作为 PIT 候选估值源使用。",
            },
            {
                "audit_item": "conservative_available_date_rule",
                "status": "pass",
                "evidence": f"valuation_available_date = valuation_trade_date + {args.release_lag_days} calendar day(s)",
                "action": "仍需后续确认申万日报真实发布时间；当前规则是保守假设。",
            },
            {
                "audit_item": "matched_signal_panel",
                "status": "pass" if len(signal_panel) > 0 else "fail",
                "evidence": f"signal_rows={len(signal_panel)}; date_start={date_to_str(signal_panel['trade_date'].min())}; date_end={date_to_str(signal_panel['trade_date'].max())}; min_cross_section_count={args.min_cross_section_count}",
                "action": "用于 RankIC、分组收益、事件回测、非重叠、样本外和逐日净值。",
            },
            {
                "audit_item": "industry_universe_break",
                "status": "warning" if new_universe > old_universe * 1.5 else "pass",
                "evidence": f"mean_industries_2015_2021={old_universe:.2f}; mean_industries_2022_2026={new_universe:.2f}",
                "action": "V2.7 必须分段看 2015-2021 与 2022-2026，避免行业体系扩容污染结论。",
            },
            {
                "audit_item": "sector_quality_exclusion",
                "status": "pass",
                "evidence": f"excluded_parent_rate={sector_exclusion_rate:.2%}; excluded_parents=银行|非银金融|房地产|建筑装饰",
                "action": "低估值常驻板块不再天然进入质量核心策略，宽口径对照单独保留。",
            },
            {
                "audit_item": "source_mouth_and_license",
                "status": "pending",
                "evidence": "公开源历史估值字段尚未通过授权源抽样复核。",
                "action": "完成字段口径、发布时间和授权源交叉审计前，不能升级 validated_alpha。",
            },
        ]
    )


def build_current_signal_snapshot(signal_panel: pd.DataFrame) -> pd.DataFrame:
    if signal_panel.empty:
        return pd.DataFrame()
    latest = signal_panel["trade_date"].max()
    frame = signal_panel[signal_panel["trade_date"] == latest].copy()
    cols = [
        "trade_date",
        "industry_code",
        "industry_name",
        "parent_industry",
        "valuation_quality_core_score",
        "valuation_quality_value_score",
        "valuation_quality_defensive_score",
        "quality_value_no_trap_score",
        "industry_valuation_quality_score",
        "valuation_pit_score",
        "historical_valuation_score",
        "parent_relative_value_score",
        "pe",
        "pb",
        "dividend_yield",
        "price_quality_composite",
        "momentum_trap_score",
        "sector_quality_exclusion_flag",
        "eligible_quality_core",
        "eligible_quality_value",
        "eligible_quality_defensive",
    ]
    return frame[[col for col in cols if col in frame.columns]].sort_values(
        "valuation_quality_core_score", ascending=False
    ).head(60)


def build_summary(
    *,
    signal_panel: pd.DataFrame,
    valuation: pd.DataFrame,
    event_backtest: pd.DataFrame,
    nonoverlap: pd.DataFrame,
    daily_nav: pd.DataFrame,
    parameter_sensitivity: pd.DataFrame,
    rejection_log: pd.DataFrame,
    source_audit: pd.DataFrame,
    args: argparse.Namespace,
    horizons: list[int],
    top_ns: list[int],
) -> dict[str, Any]:
    status_counts = rejection_log["signal_status"].value_counts().to_dict() if not rejection_log.empty else {}
    final_verdict = "research_only_no_alpha_promotion"
    if status_counts.get("candidate_requires_source_audit", 0):
        final_verdict = "quant_candidate_but_source_audit_required"
    elif status_counts.get("conditional_observation", 0):
        final_verdict = "conditional_observation_only"
    return {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_boundary": "V2.7 将 V2.6 的低估值抄底框架改为估值质量验证：重点检验估值数据稳定性、股息连续性、市值深度、价格质量和行业体系分段。所有结论仍为 research_only，公开源口径和发布时间未完成审计前不能升级 alpha。",
        "valuation_start": date_to_str(valuation["valuation_trade_date"].min()) if not valuation.empty else "",
        "valuation_end": date_to_str(valuation["valuation_trade_date"].max()) if not valuation.empty else "",
        "valuation_rows": int(len(valuation)),
        "valuation_industries": int(valuation["industry_code"].nunique()) if not valuation.empty else 0,
        "release_lag_days": int(args.release_lag_days),
        "min_cross_section_count": int(args.min_cross_section_count),
        "date_start": date_to_str(signal_panel["trade_date"].min()) if not signal_panel.empty else "",
        "date_end": date_to_str(signal_panel["trade_date"].max()) if not signal_panel.empty else "",
        "signal_rows": int(len(signal_panel)),
        "event_rows": int(len(event_backtest)),
        "nonoverlap_rows": int(len(nonoverlap)),
        "daily_nav_rows": int(len(daily_nav)),
        "strategy_count": len(STRATEGIES),
        "horizons": horizons,
        "top_ns": top_ns,
        "candidate_requires_source_audit_count": int(status_counts.get("candidate_requires_source_audit", 0)),
        "conditional_observation_count": int(status_counts.get("conditional_observation", 0)),
        "rejected_signal_count": int(status_counts.get("rejected_signal", 0)),
        "source_audit_pending_count": int((source_audit["status"] == "pending").sum()),
        "source_audit_warning_count": int((source_audit["status"] == "warning").sum()),
        "best_strategy": parameter_sensitivity.iloc[0]["strategy_zh"] if not parameter_sensitivity.empty else "",
        "best_top_n": int(parameter_sensitivity.iloc[0]["top_n"]) if not parameter_sensitivity.empty else 0,
        "best_horizon": int(parameter_sensitivity.iloc[0]["horizon"]) if not parameter_sensitivity.empty else 0,
        "best_mean_relative_return": float(parameter_sensitivity.iloc[0]["mean_relative_return"]) if not parameter_sensitivity.empty else math.nan,
        "best_oos_mean_relative_return": float(parameter_sensitivity.iloc[0]["oos_mean_relative_return"]) if not parameter_sensitivity.empty else math.nan,
        "best_nonoverlap_mean_relative_return": float(parameter_sensitivity.iloc[0]["nonoverlap_mean_relative_return"]) if not parameter_sensitivity.empty else math.nan,
        "best_relative_final_nav": float(parameter_sensitivity.iloc[0]["relative_final_nav"]) if not parameter_sensitivity.empty else math.nan,
        "final_verdict": final_verdict,
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    rankic_report: pd.DataFrame,
    group_return_report: pd.DataFrame,
    period_validation: pd.DataFrame,
    nav_metrics: pd.DataFrame,
    source_audit: pd.DataFrame,
    universe_breaks: pd.DataFrame,
    current_snapshot: pd.DataFrame,
) -> str:
    lines = [
        "# V2.7 行业估值质量验证报告",
        "",
        f"版本：{VERSION}",
        "",
        "## 研究结论",
        "",
        summary["research_boundary"],
        "",
        f"- 历史估值区间：{summary['valuation_start']} 至 {summary['valuation_end']}",
        f"- 信号样本区间：{summary['date_start']} 至 {summary['date_end']}",
        f"- 信号面板行数：{summary['signal_rows']}",
        f"- 事件回测行数：{summary['event_rows']}",
        f"- 非重叠事件行数：{summary['nonoverlap_rows']}",
        f"- 逐日净值行数：{summary['daily_nav_rows']}",
        f"- 候选待源审计信号数：{summary['candidate_requires_source_audit_count']}",
        f"- 条件观察信号数：{summary['conditional_observation_count']}",
        f"- 拒绝信号数：{summary['rejected_signal_count']}",
        f"- 最终结论：{translate_final_verdict(summary['final_verdict'])}",
        "",
        "V2.7 的判定重点不是找更会抄底的尾部行业，而是回答：估值质量本身能否在更大组合规模和分段样本中产生稳健行业选择收益。TopN 默认改为 20/30，避免 V2.0-V2.6 已暴露的 Top5/Top10 尾部过拟合。",
        "",
        "## 策略参数排序",
        "",
    ]
    lines.extend(render_markdown_table(top_candidates.head(18)))

    lines.extend(["", "## RankIC 摘要", ""])
    if rankic_report.empty:
        lines.append("未生成 RankIC 结果。")
    else:
        display = rankic_report.sort_values(["mean_rankic"], ascending=False).head(18).copy()
        for col in ["mean_rankic", "positive_ratio"]:
            display[col] = display[col].map(fmt_pct)
        display["rankic_t_stat"] = display["rankic_t_stat"].map(lambda x: fmt_float(x, 2))
        lines.extend(
            render_markdown_table(
                display[["factor_zh", "horizon", "mean_rankic", "rankic_t_stat", "positive_ratio", "date_count"]].rename(
                    columns={
                        "factor_zh": "因子",
                        "horizon": "周期",
                        "mean_rankic": "平均RankIC",
                        "rankic_t_stat": "T值",
                        "positive_ratio": "正IC比例",
                        "date_count": "日期数",
                    }
                )
            )
        )

    lines.extend(["", "## 分组收益检查", ""])
    if group_return_report.empty:
        lines.append("未生成分组收益结果。")
    else:
        display = group_return_report[group_return_report["horizon"].isin([252])].copy()
        if display.empty:
            display = group_return_report.copy()
        display = display.sort_values(["factor", "horizon", "quantile"]).head(40)
        display["mean_relative_return"] = display["mean_relative_return"].map(fmt_pct)
        display["win_rate"] = display["win_rate"].map(fmt_pct)
        lines.extend(
            render_markdown_table(
                display.rename(
                    columns={
                        "factor": "因子",
                        "horizon": "周期",
                        "quantile": "分组",
                        "mean_relative_return": "平均相对收益",
                        "win_rate": "跑赢比例",
                        "samples": "样本数",
                    }
                )
            )
        )

    lines.extend(["", "## 分阶段验证", ""])
    if period_validation.empty:
        lines.append("未生成分阶段验证。")
    else:
        best_ids = top_candidates.head(8)["策略"].dropna().unique().tolist() if not top_candidates.empty else []
        display = period_validation[period_validation["strategy_zh"].isin(best_ids)].copy()
        if display.empty:
            display = period_validation.sort_values("mean_relative_return", ascending=False).head(16).copy()
        else:
            display = display.sort_values(["strategy_zh", "top_n", "horizon", "period"]).head(24)
        for col in ["mean_relative_return", "benchmark_win_rate", "avg_quality_core_score", "avg_valuation_quality_score"]:
            if col in display.columns:
                display[col] = display[col].map(fmt_pct)
        lines.extend(
            render_markdown_table(
                display[
                    [
                        "strategy_zh",
                        "top_n",
                        "horizon",
                        "period",
                        "samples",
                        "mean_relative_return",
                        "benchmark_win_rate",
                        "avg_quality_core_score",
                        "avg_valuation_quality_score",
                    ]
                ].rename(
                    columns={
                        "strategy_zh": "策略",
                        "top_n": "TopN",
                        "horizon": "周期",
                        "period": "阶段",
                        "samples": "样本数",
                        "mean_relative_return": "平均相对收益",
                        "benchmark_win_rate": "跑赢比例",
                        "avg_quality_core_score": "质量核心分",
                        "avg_valuation_quality_score": "估值质量分",
                    }
                )
            )
        )

    lines.extend(["", "## 逐日净值摘要", ""])
    if nav_metrics.empty:
        lines.append("未生成逐日净值。")
    else:
        display = nav_metrics.sort_values("relative_final_nav", ascending=False).head(12).copy()
        for col in ["annualized_return", "benchmark_annualized_return", "annualized_relative_return", "max_drawdown", "relative_max_drawdown", "daily_relative_win_rate"]:
            if col in display.columns:
                display[col] = display[col].map(fmt_pct)
        lines.extend(
            render_markdown_table(
                display[
                    [
                        "strategy_zh",
                        "top_n",
                        "daily_rows",
                        "start_date",
                        "end_date",
                        "relative_final_nav",
                        "annualized_relative_return",
                        "relative_max_drawdown",
                        "daily_relative_win_rate",
                    ]
                ].rename(
                    columns={
                        "strategy_zh": "策略",
                        "top_n": "TopN",
                        "daily_rows": "交易日",
                        "start_date": "开始",
                        "end_date": "结束",
                        "relative_final_nav": "相对净值",
                        "annualized_relative_return": "年化相对",
                        "relative_max_drawdown": "相对回撤",
                        "daily_relative_win_rate": "日跑赢率",
                    }
                )
            )
        )

    lines.extend(["", "## 数据与治理审计", ""])
    lines.extend(render_markdown_table(source_audit))

    lines.extend(["", "## 行业覆盖分年审计", ""])
    if universe_breaks.empty:
        lines.append("未生成行业覆盖审计。")
    else:
        lines.extend(render_markdown_table(universe_breaks.tail(8)))

    lines.extend(["", "## 当前截面观察", ""])
    if current_snapshot.empty:
        lines.append("未生成当前截面观察。")
    else:
        display = current_snapshot.head(20).copy()
        for col in [
            "valuation_quality_core_score",
            "valuation_quality_value_score",
            "valuation_quality_defensive_score",
            "quality_value_no_trap_score",
            "industry_valuation_quality_score",
            "valuation_pit_score",
            "dividend_yield",
            "price_quality_composite",
        ]:
            if col in display.columns:
                display[col] = display[col].map(fmt_pct)
        for col in ["pe", "pb", "momentum_trap_score"]:
            if col in display.columns:
                display[col] = display[col].map(lambda x: fmt_float(x, 2))
        lines.extend(
            render_markdown_table(
                display.rename(
                    columns={
                        "trade_date": "日期",
                        "industry_code": "行业代码",
                        "industry_name": "行业",
                        "parent_industry": "父行业",
                        "valuation_quality_core_score": "质量核心",
                        "valuation_quality_value_score": "质量价值",
                        "valuation_quality_defensive_score": "质量防守",
                        "quality_value_no_trap_score": "非陷阱",
                        "industry_valuation_quality_score": "估值质量",
                        "valuation_pit_score": "估值分",
                        "dividend_yield": "股息率",
                        "price_quality_composite": "价格质量",
                        "sector_quality_exclusion_flag": "板块剔除",
                    }
                )
            )
        )

    lines.extend(
        [
            "",
            "## 输出文件说明",
            "",
            "- `top_candidates.csv`：策略参数排序和状态判断，适合先看。",
            "- `run_summary.json`：机器可读运行摘要。",
            "- `debug/`：完整信号面板、事件明细、非重叠、样本外、bootstrap、逐日净值、分组收益和审计文件。",
            "",
            "研究边界：本报告只研究申万行业与行业指数，不做个股筛选，不生成交易指令。历史估值来自公开源，虽然已经使用 `available_date` 保守滞后，但源口径和真实发布时间仍需独立审计。",
            "",
        ]
    )
    return "\n".join(lines)


def parse_int_list(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def mean_col(frame: pd.DataFrame, column: str) -> float:
    return float(frame[column].mean()) if column in frame.columns and not frame[column].dropna().empty else math.nan


def mean_bool(frame: pd.DataFrame, column: str) -> float:
    if column not in frame.columns or frame.empty:
        return math.nan
    return float(frame[column].fillna(False).astype(bool).mean())


def first_value(frame: pd.DataFrame, column: str) -> Any:
    if column not in frame.columns or frame.empty:
        return ""
    value = frame[column].mode()
    return value.iloc[0] if not value.empty else ""


def first_nonempty(frame: pd.DataFrame, column: str) -> str:
    if column not in frame.columns or frame.empty:
        return ""
    values = [str(value) for value in frame[column].dropna().tolist() if str(value)]
    return values[0] if values else ""


def mean_t_stat(values: list[float]) -> float:
    if len(values) < 2:
        return math.nan
    arr = np.array(values, dtype=float)
    std = float(np.std(arr, ddof=1))
    if std == 0:
        return math.nan
    return float(np.mean(arr) / (std / math.sqrt(len(arr))))


def safe_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(number):
        return ""
    return f"{number:.2%}"


def fmt_float(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(number):
        return ""
    return f"{number:.{digits}f}"


def date_to_str(value: Any) -> str:
    if pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def translate_status(status: str) -> str:
    return {
        "candidate_requires_source_audit": "候选待源审计",
        "conditional_observation": "条件观察",
        "rejected_signal": "拒绝",
    }.get(status, status)


def translate_final_verdict(status: str) -> str:
    return {
        "quant_candidate_but_source_audit_required": "存在量化候选，但源口径审计未完成，仍为research_only",
        "conditional_observation_only": "仅有条件观察，不能升级alpha",
        "research_only_no_alpha_promotion": "research_only，无可升级alpha",
    }.get(status, status)


def render_markdown_table(frame: pd.DataFrame) -> list[str]:
    if frame is None or frame.empty:
        return ["无数据。"]
    display = frame.copy()
    display = display.astype(object).where(pd.notna(display), "")
    headers = list(display.columns)
    rows = display.astype(str).values.tolist()
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value).replace("\n", " ") for value in row) + " |")
    return lines


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
