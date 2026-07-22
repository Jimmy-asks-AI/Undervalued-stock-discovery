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

try:
    from valuation_pit_contract import ValuationPITContractError, prepare_pit_valuation_history
except ModuleNotFoundError:  # package-style imports in tests and audits
    from scripts.valuation_pit_contract import ValuationPITContractError, prepare_pit_valuation_history


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
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_valuation_pit_validation_v2_6"
V23_SCRIPT = ROOT / "scripts" / "run_industry_pressure_quality_v2_3.py"
VERSION = "2.6.0"


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    strategy_zh: str
    signal_col: str
    gate_col: str
    description: str


STRATEGIES = [
    StrategySpec(
        "valuation_only_pit",
        "V2.6：纯估值PIT",
        "valuation_pit_score",
        "eligible_valuation_only",
        "只用历史可得估值便宜程度，不叠加压力或质量。",
    ),
    StrategySpec(
        "valuation_pressure_combo",
        "V2.6：估值+压力",
        "valuation_pressure_score",
        "eligible_valuation_pressure",
        "估值便宜叠加市场压力和非动量陷阱过滤。",
    ),
    StrategySpec(
        "valuation_pressure_quality_combo",
        "V2.6：估值+压力+质量",
        "valuation_pressure_quality_score",
        "eligible_valuation_pressure_quality",
        "估值便宜、压力反转和行业质量代理同时确认。",
    ),
    StrategySpec(
        "valuation_oversold_quality_combo",
        "V2.6：估值+超跌+质量",
        "valuation_oversold_quality_score",
        "eligible_valuation_oversold_quality",
        "不强制压力区，但要求便宜、超跌、质量和非动量陷阱。",
    ),
    StrategySpec(
        "parent_relative_value_pressure",
        "V2.6：父行业相对低估+压力",
        "parent_relative_value_pressure_score",
        "eligible_parent_relative_value_pressure",
        "在同一申万一级内部寻找相对便宜且处于压力环境的二级行业。",
    ),
    StrategySpec(
        "historical_value_extreme_reversal",
        "V2.6：自身历史低估极值",
        "historical_value_extreme_score",
        "eligible_historical_value_extreme",
        "行业相对自身历史估值处于低位，并排除明显动量陷阱。",
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.6 PIT valuation factor validation for SW second-level industries.")
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="Historical price/return feature panel.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current industry name and parent mapping.")
    parser.add_argument("--valuation", default=str(DEFAULT_VALUATION), help="SWS daily industry valuation history.")
    parser.add_argument("--history-dir", default=str(DEFAULT_HISTORY_DIR), help="Cached industry index histories for daily NAV.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--horizons", default="60,120,252", help="Forward holding horizons.")
    parser.add_argument("--top-ns", default="5,10,20", help="Top N baskets.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="One-way rebalance cost in bps.")
    parser.add_argument("--release-lag-days", type=int, default=None, help="Deprecated compatibility option; never used to infer available_date.")
    parser.add_argument("--min-valuation-days", type=int, default=252, help="Minimum trailing valuation rows for PIT valuation gates.")
    parser.add_argument("--min-cross-section-count", type=int, default=20, help="Minimum industries per feature date for cross-sectional validation.")
    parser.add_argument("--stress-threshold", type=float, default=0.65, help="Market pressure gate.")
    parser.add_argument("--extreme-stress-threshold", type=float, default=0.80, help="Extreme pressure gate.")
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
    v23 = load_v23_module()

    raw_features = v23.load_features(Path(args.features))
    names = v23.load_names(Path(args.ranking))
    features = v23.attach_names(raw_features, names)
    try:
        valuation = load_valuation_history(Path(args.valuation), release_lag_days=args.release_lag_days)
    except ValuationPITContractError as exc:
        write_blocked_pit_outputs(
            output_dir=output_dir,
            debug_dir=debug_dir,
            valuation_path=Path(args.valuation),
            reason=str(exc),
        )
        print("V2.6 blocked_non_pit_valuation_history")
        print(f"reason={exc}")
        print(f"output_dir={output_dir.resolve()}")
        return
    valuation_features = build_valuation_feature_panel(valuation)
    signal_panel = build_v26_signal_panel(
        v23=v23,
        features=features,
        valuation_features=valuation_features,
        stress_threshold=args.stress_threshold,
        extreme_stress_threshold=args.extreme_stress_threshold,
        min_valuation_days=args.min_valuation_days,
    )
    signal_panel = filter_cross_section_dates(signal_panel, min_count=args.min_cross_section_count)

    event_backtest = compute_event_backtest(signal_panel, horizons, top_ns, args.cost_bps)
    previous_strategies = v23.STRATEGIES
    v23.STRATEGIES = [v23.StrategySpec(s.strategy_id, s.strategy_zh, s.signal_col, s.gate_col, "valuation_pit_candidate", s.description) for s in STRATEGIES]
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
    universe_breaks = compute_universe_breaks(valuation)
    source_audit = build_source_audit(
        valuation=valuation,
        signal_panel=signal_panel,
        args=args,
        universe_breaks=universe_breaks,
    )
    current_snapshot = build_current_signal_snapshot(signal_panel)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    signal_panel.to_csv(debug_dir / "valuation_pit_signal_panel.csv", index=False, encoding="utf-8-sig")
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
    source_audit.to_csv(debug_dir / "valuation_pit_source_audit.csv", index=False, encoding="utf-8-sig")
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
            period_validation=period_validation,
            nav_metrics=nav_metrics,
            source_audit=source_audit,
            universe_breaks=universe_breaks,
            current_snapshot=current_snapshot,
        ),
        encoding="utf-8",
    )

    print(f"V{VERSION} 行业历史估值 PIT 验证完成")
    print(f"估值区间={summary['valuation_start']} 至 {summary['valuation_end']}")
    print(f"信号面板行数={summary['signal_rows']}")
    print(f"事件回测行数={summary['event_rows']}")
    print(f"候选待源审计信号数={summary['candidate_requires_source_audit_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v23_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_pressure_quality_v2_3", V23_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load V2.3 helper module: {V23_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_valuation_history(path: Path, release_lag_days: int | None) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    # Compatibility parameter is intentionally ignored.  A numeric lag is not
    # publication evidence and must never manufacture historical availability.
    _ = release_lag_days
    frame = prepare_pit_valuation_history(frame, source=str(path))
    numeric_cols = [
        "close_index",
        "return_pct",
        "turnover_rate",
        "pe",
        "pb",
        "float_market_cap",
        "avg_float_market_cap",
        "dividend_yield",
    ]
    for col in numeric_cols:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.dropna(subset=["valuation_trade_date", "valuation_available_date"]).sort_values(
        ["industry_code", "valuation_available_date"]
    )


def write_blocked_pit_outputs(
    *,
    output_dir: Path,
    debug_dir: Path,
    valuation_path: Path,
    reason: str,
) -> None:
    """Replace authoritative outputs with an explicit fail-closed verdict."""

    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=["status", "reason"]).to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    audit = pd.DataFrame(
        [
            {
                "audit_item": "valuation_pit_contract",
                "status": "fail",
                "evidence": reason,
                "action": "补齐 published_at、available_date、fetched_at、source_version、source_hash、revision_status，并按冻结A股交易日历重建。",
            }
        ]
    )
    audit.to_csv(debug_dir / "valuation_pit_source_audit.csv", index=False, encoding="utf-8-sig")
    summary = {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "policy_status": "research_only",
        "data_status": "blocked_non_pit_valuation_history",
        "pit_eligible": False,
        "valuation_path": str(valuation_path),
        "block_reason": reason,
        "signal_rows": 0,
        "event_rows": 0,
        "candidate_requires_source_audit_count": 0,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "blocked_non_pit_valuation_history",
        "research_boundary": "缺少真实发布时间及版本证据时停止估值PIT验证；不得用trade_date加推定lag替代。",
    }
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        "\n".join(
            [
                "# V2.6 行业历史估值 PIT 验证报告",
                "",
                "本轮失败关闭：输入历史不满足 PIT 数据合同，未生成任何估值信号或回测结论。",
                "",
                f"- 原因：{reason}",
                f"- 输入：`{valuation_path}`",
                "- 要求：真实发布时间、抓取时间、来源版本/哈希、修订状态，以及冻结A股交易日历推导的可用交易日。",
            ]
        ),
        encoding="utf-8",
    )


def build_valuation_feature_panel(valuation: pd.DataFrame) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for _, group in valuation.groupby("industry_code", sort=True):
        g = group.sort_values("valuation_available_date").copy()
        g["pe_valid"] = g["pe"].between(0.01, 100.0)
        g["pb_valid"] = g["pb"].between(0.01, 20.0)
        g["dividend_valid"] = g["dividend_yield"].ge(0)
        g["dividend_positive"] = g["dividend_yield"].gt(0)
        g["valuation_history_count"] = np.arange(1, len(g) + 1)
        g["pe_valid_value"] = g["pe"].where(g["pe_valid"])
        g["pb_valid_value"] = g["pb"].where(g["pb_valid"])
        g["dividend_valid_value"] = g["dividend_yield"].where(g["dividend_valid"])
        g["pe_positive_valid_ratio_252"] = g["pe_valid"].rolling(252, min_periods=1).mean()
        g["pb_valid_ratio_252"] = g["pb_valid"].rolling(252, min_periods=1).mean()
        g["dividend_positive_ratio_252"] = g["dividend_positive"].rolling(252, min_periods=1).mean()
        g["pe_log_std_252"] = np.log(g["pe_valid_value"]).rolling(252, min_periods=30).std()
        g["pb_log_std_252"] = np.log(g["pb_valid_value"]).rolling(252, min_periods=30).std()
        g["historical_pe_percentile_756"] = rolling_last_percentile(g["pe_valid_value"], window=756, higher_is_better=False)
        g["historical_pb_percentile_756"] = rolling_last_percentile(g["pb_valid_value"], window=756, higher_is_better=False)
        g["historical_dividend_percentile_756"] = rolling_last_percentile(g["dividend_valid_value"], window=756, higher_is_better=True)
        rows.append(g)
    frame = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    keep = [
        "industry_code",
        "industry_name",
        "valuation_trade_date",
        "valuation_available_date",
        "pe",
        "pb",
        "dividend_yield",
        "turnover_rate",
        "float_market_cap",
        "avg_float_market_cap",
        "valuation_history_count",
        "pe_positive_valid_ratio_252",
        "pb_valid_ratio_252",
        "dividend_positive_ratio_252",
        "pe_log_std_252",
        "pb_log_std_252",
        "historical_pe_percentile_756",
        "historical_pb_percentile_756",
        "historical_dividend_percentile_756",
    ]
    return frame[[col for col in keep if col in frame.columns]].sort_values(["industry_code", "valuation_available_date"])


def rolling_last_percentile(series: pd.Series, *, window: int, higher_is_better: bool) -> pd.Series:
    def percentile(values: np.ndarray) -> float:
        values = values[~np.isnan(values)]
        if len(values) < 60:
            return math.nan
        last = values[-1]
        rank = float(np.mean(values <= last))
        return rank if higher_is_better else 1.0 - rank

    return series.rolling(window, min_periods=60).apply(percentile, raw=True)


def build_v26_signal_panel(
    *,
    v23: Any,
    features: pd.DataFrame,
    valuation_features: pd.DataFrame,
    stress_threshold: float,
    extreme_stress_threshold: float,
    min_valuation_days: int,
) -> pd.DataFrame:
    base = v23.build_pressure_quality_signal_panel(
        features,
        stress_threshold=stress_threshold,
        extreme_stress_threshold=extreme_stress_threshold,
    )
    merged = attach_valuation_asof(base, valuation_features)
    merged = merged.dropna(subset=["valuation_trade_date"]).copy()
    merged["valuation_age_days"] = (merged["trade_date"] - merged["valuation_available_date"]).dt.days
    merged["valuation_pit_coverage_score"] = (merged["valuation_history_count"] / max(min_valuation_days, 1)).clip(upper=1.0)
    merged["valuation_history_gate"] = merged["valuation_history_count"] >= min_valuation_days

    merged["cross_pe_cheapness_score"] = rank_by_date(merged, "pe", ascending=False)
    merged["cross_pb_cheapness_score"] = rank_by_date(merged, "pb", ascending=False)
    merged["cross_dividend_support_score"] = rank_by_date(merged, "dividend_yield", ascending=True)
    merged["parent_pe_cheapness_score"] = rank_by_date_parent(merged, "pe", ascending=False)
    merged["parent_pb_cheapness_score"] = rank_by_date_parent(merged, "pb", ascending=False)
    merged["parent_dividend_support_score"] = rank_by_date_parent(merged, "dividend_yield", ascending=True)
    merged["pe_stability_score"] = rank_by_date(merged, "pe_log_std_252", ascending=False)
    merged["pb_stability_score"] = rank_by_date(merged, "pb_log_std_252", ascending=False)
    merged["market_depth_score"] = rank_by_date(merged.assign(_log_float_mv=np.log1p(merged["float_market_cap"].clip(lower=0))), "_log_float_mv", ascending=True)

    merged["valuation_cross_section_score"] = (
        0.40 * merged["cross_pe_cheapness_score"].fillna(0.0)
        + 0.35 * merged["cross_pb_cheapness_score"].fillna(0.0)
        + 0.25 * merged["cross_dividend_support_score"].fillna(0.0)
    )
    merged["parent_relative_value_score"] = (
        0.42 * merged["parent_pe_cheapness_score"].fillna(0.0)
        + 0.38 * merged["parent_pb_cheapness_score"].fillna(0.0)
        + 0.20 * merged["parent_dividend_support_score"].fillna(0.0)
    )
    merged["historical_valuation_score"] = (
        0.42 * merged["historical_pe_percentile_756"].fillna(0.0)
        + 0.38 * merged["historical_pb_percentile_756"].fillna(0.0)
        + 0.20 * merged["historical_dividend_percentile_756"].fillna(0.0)
    )
    merged["valuation_quality_score_raw"] = (
        0.22 * merged["pe_positive_valid_ratio_252"].fillna(0.0)
        + 0.18 * merged["pb_valid_ratio_252"].fillna(0.0)
        + 0.13 * merged["dividend_positive_ratio_252"].fillna(0.0)
        + 0.16 * merged["pe_stability_score"].fillna(0.0)
        + 0.16 * merged["pb_stability_score"].fillna(0.0)
        + 0.08 * merged["market_depth_score"].fillna(0.0)
        + 0.07 * merged["price_quality_composite"].fillna(0.0)
    )
    merged["industry_valuation_quality_score"] = merged.groupby("trade_date")["valuation_quality_score_raw"].rank(pct=True, method="average")
    merged["valuation_pit_score_raw"] = (
        0.42 * merged["valuation_cross_section_score"].fillna(0.0)
        + 0.26 * merged["parent_relative_value_score"].fillna(0.0)
        + 0.22 * merged["historical_valuation_score"].fillna(0.0)
        + 0.10 * merged["valuation_pit_coverage_score"].fillna(0.0)
    )
    merged["valuation_pit_score"] = merged.groupby("trade_date")["valuation_pit_score_raw"].rank(pct=True, method="average")
    merged["valuation_pressure_score_raw"] = (
        0.46 * merged["valuation_pit_score"].fillna(0.0)
        + 0.24 * merged["pressure_quality_score"].fillna(0.0)
        + 0.16 * merged["market_stress_score"].fillna(0.0)
        + 0.14 * merged["industry_valuation_quality_score"].fillna(0.0)
        - 0.12 * merged["momentum_trap_score"].clip(upper=3)
    )
    merged["valuation_pressure_score"] = merged.groupby("trade_date")["valuation_pressure_score_raw"].rank(pct=True, method="average")
    merged["valuation_pressure_quality_score_raw"] = (
        0.38 * merged["valuation_pit_score"].fillna(0.0)
        + 0.24 * merged["pressure_quality_score"].fillna(0.0)
        + 0.22 * merged["industry_valuation_quality_score"].fillna(0.0)
        + 0.10 * merged["market_stress_score"].fillna(0.0)
        + 0.06 * merged["market_depth_score"].fillna(0.0)
        - 0.14 * merged["momentum_trap_score"].clip(upper=3)
    )
    merged["valuation_pressure_quality_score"] = merged.groupby("trade_date")["valuation_pressure_quality_score_raw"].rank(
        pct=True, method="average"
    )
    merged["valuation_oversold_quality_score_raw"] = (
        0.40 * merged["valuation_pit_score"].fillna(0.0)
        + 0.25 * merged["stabilized_oversold_signal"].fillna(0.0)
        + 0.22 * merged["industry_valuation_quality_score"].fillna(0.0)
        + 0.13 * merged["recovery_quality_score"].fillna(0.0)
        - 0.14 * merged["momentum_trap_score"].clip(upper=3)
    )
    merged["valuation_oversold_quality_score"] = merged.groupby("trade_date")["valuation_oversold_quality_score_raw"].rank(
        pct=True, method="average"
    )
    merged["parent_relative_value_pressure_score_raw"] = (
        0.52 * merged["parent_relative_value_score"].fillna(0.0)
        + 0.22 * merged["pressure_quality_score"].fillna(0.0)
        + 0.16 * merged["market_stress_score"].fillna(0.0)
        + 0.10 * merged["industry_valuation_quality_score"].fillna(0.0)
        - 0.12 * merged["momentum_trap_score"].clip(upper=3)
    )
    merged["parent_relative_value_pressure_score"] = merged.groupby("trade_date")[
        "parent_relative_value_pressure_score_raw"
    ].rank(pct=True, method="average")
    merged["historical_value_extreme_score_raw"] = (
        0.54 * merged["historical_valuation_score"].fillna(0.0)
        + 0.18 * merged["valuation_cross_section_score"].fillna(0.0)
        + 0.16 * merged["industry_valuation_quality_score"].fillna(0.0)
        + 0.12 * merged["stabilized_oversold_signal"].fillna(0.0)
        - 0.12 * merged["momentum_trap_score"].clip(upper=3)
    )
    merged["historical_value_extreme_score"] = merged.groupby("trade_date")["historical_value_extreme_score_raw"].rank(
        pct=True, method="average"
    )

    sector_gap_parents = {"银行", "非银金融", "房地产", "建筑装饰"}
    merged["sector_quality_data_required_flag"] = merged["parent_industry"].isin(sector_gap_parents)
    merged["eligible_valuation_only"] = merged["valuation_history_gate"] & (merged["valuation_pit_score"] >= 0.55)
    merged["eligible_valuation_pressure"] = (
        merged["eligible_valuation_only"]
        & merged["pressure_gate"]
        & merged["not_momentum_trap"]
        & (merged["stabilized_oversold_signal"] >= 0.45)
    )
    merged["eligible_valuation_pressure_quality"] = (
        merged["eligible_valuation_pressure"]
        & (merged["industry_valuation_quality_score"] >= 0.50)
        & (merged["price_quality_composite"] >= 0.45)
        & (~merged["sector_quality_data_required_flag"])
    )
    merged["eligible_valuation_oversold_quality"] = (
        merged["eligible_valuation_only"]
        & merged["not_momentum_trap"]
        & merged["stabilized_oversold_signal"].between(0.55, 0.95, inclusive="both")
        & (merged["industry_valuation_quality_score"] >= 0.45)
        & (merged["recovery_quality_score"] >= 0.35)
        & (~merged["sector_quality_data_required_flag"])
    )
    merged["eligible_parent_relative_value_pressure"] = (
        merged["valuation_history_gate"]
        & merged["pressure_gate"]
        & merged["not_momentum_trap"]
        & (merged["parent_relative_value_score"] >= 0.55)
        & (merged["industry_valuation_quality_score"] >= 0.45)
    )
    merged["eligible_historical_value_extreme"] = (
        merged["valuation_history_gate"]
        & merged["not_momentum_trap"]
        & (merged["historical_valuation_score"] >= 0.65)
        & (merged["industry_valuation_quality_score"] >= 0.45)
        & (merged["valuation_pit_score"] >= 0.50)
    )
    return merged.drop(
        columns=[
            "_log_float_mv",
            "valuation_quality_score_raw",
            "valuation_pit_score_raw",
            "valuation_pressure_score_raw",
            "valuation_pressure_quality_score_raw",
            "valuation_oversold_quality_score_raw",
            "parent_relative_value_pressure_score_raw",
            "historical_value_extreme_score_raw",
        ],
        errors="ignore",
    ).sort_values(["trade_date", "industry_code"])


def attach_valuation_asof(features: pd.DataFrame, valuation_features: pd.DataFrame) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for code, left in features.groupby("industry_code", sort=True):
        right = valuation_features[valuation_features["industry_code"] == code].copy()
        if right.empty:
            continue
        left_sorted = left.sort_values("trade_date").copy()
        right_sorted = right.sort_values("valuation_available_date").drop(columns=["industry_code"], errors="ignore")
        merged = pd.merge_asof(
            left_sorted,
            right_sorted,
            left_on="trade_date",
            right_on="valuation_available_date",
            direction="backward",
        )
        rows.append(merged)
    if not rows:
        return pd.DataFrame()
    frame = pd.concat(rows, ignore_index=True)
    frame["industry_code"] = frame["industry_code"].map(lambda value: str(value).zfill(6))
    if "industry_name_x" in frame.columns:
        frame["industry_name"] = frame["industry_name_x"]
    if "industry_name_y" in frame.columns:
        frame["valuation_industry_name"] = frame["industry_name_y"]
    frame = frame.drop(columns=["industry_name_x", "industry_name_y"], errors="ignore")
    return frame


def filter_cross_section_dates(signal_panel: pd.DataFrame, min_count: int) -> pd.DataFrame:
    if signal_panel.empty or min_count <= 1:
        return signal_panel
    counts = signal_panel.groupby("trade_date")["industry_code"].transform("nunique")
    return signal_panel[counts >= min_count].copy()


def rank_by_date(frame: pd.DataFrame, column: str, *, ascending: bool) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index)
    return frame.groupby("trade_date")[column].rank(pct=True, ascending=ascending, method="average")


def rank_by_date_parent(frame: pd.DataFrame, column: str, *, ascending: bool) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index)
    return frame.groupby(["trade_date", "parent_industry"])[column].rank(pct=True, ascending=ascending, method="average")


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
                            "market_stress_score": float(selected["market_stress_score"].mean()),
                            "pressure_tier": first_value(selected, "pressure_tier"),
                            "pressure_episode_id": first_nonempty(selected, "pressure_episode_id"),
                            "market_regime": first_value(selected, "market_regime"),
                            "volatility_regime": first_value(selected, "volatility_regime"),
                            "avg_momentum_trap_score": float(selected["momentum_trap_score"].mean()),
                            "avg_price_quality_composite": mean_col(selected, "price_quality_composite"),
                            "avg_liquidity_quality_score": mean_col(selected, "liquidity_quality_score"),
                            "avg_low_volatility_quality_score": mean_col(selected, "low_volatility_quality_score"),
                            "avg_recovery_quality_score": mean_col(selected, "recovery_quality_score"),
                            "avg_valuation_pit_score": mean_col(selected, "valuation_pit_score"),
                            "avg_valuation_quality_score": mean_col(selected, "industry_valuation_quality_score"),
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
        "avg_momentum_trap_score": float(group["avg_momentum_trap_score"].mean()),
    }
    for col in [
        "avg_price_quality_composite",
        "avg_liquidity_quality_score",
        "avg_low_volatility_quality_score",
        "avg_recovery_quality_score",
        "avg_valuation_pit_score",
        "avg_valuation_quality_score",
        "avg_parent_relative_value_score",
        "avg_historical_valuation_score",
        "avg_pe",
        "avg_pb",
        "avg_dividend_yield",
        "avg_valuation_age_days",
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
            reasons.append("量化门槛通过，但公开估值源仍需口径和release lag审计，不能升级alpha")
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
                "平均估值分": fmt_pct(row.get("avg_valuation_pit_score", math.nan)),
                "平均质量分": fmt_pct(row.get("avg_valuation_quality_score", math.nan)),
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
        "valuation_pit_score": "估值PIT综合分",
        "valuation_cross_section_score": "当日横截面低估",
        "parent_relative_value_score": "父行业内相对低估",
        "historical_valuation_score": "自身历史低估",
        "industry_valuation_quality_score": "估值质量代理",
        "valuation_pressure_score": "估值+压力",
        "valuation_pressure_quality_score": "估值+压力+质量",
        "valuation_oversold_quality_score": "估值+超跌+质量",
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
            for trade_date, group in signal_panel.groupby("trade_date", sort=True):
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
            t_stat = mean_t_stat(rankics)
            rows.append(
                {
                    "factor": factor,
                    "factor_zh": factor_zh,
                    "horizon": int(horizon),
                    "mean_rankic": float(np.mean(rankics)) if rankics else math.nan,
                    "rankic_t_stat": t_stat,
                    "positive_ratio": float(np.mean(np.array(rankics) > 0)) if rankics else math.nan,
                    "date_count": int(len(rankics)),
                }
            )
    return pd.DataFrame(rows)


def compute_group_return_report(signal_panel: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    factors = ["valuation_pit_score", "valuation_pressure_quality_score", "valuation_oversold_quality_score"]
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


def compute_universe_breaks(valuation: pd.DataFrame) -> pd.DataFrame:
    date_counts = (
        valuation.groupby("valuation_trade_date")
        .agg(
            industries=("industry_code", "nunique"),
            rows=("industry_code", "size"),
            pe_non_null=("pe", lambda s: int(s.notna().sum())),
            pb_non_null=("pb", lambda s: int(s.notna().sum())),
            dividend_non_null=("dividend_yield", lambda s: int(s.notna().sum())),
        )
        .reset_index()
    )
    date_counts["year"] = date_counts["valuation_trade_date"].dt.year
    return (
        date_counts.groupby("year")
        .agg(
            trading_dates=("valuation_trade_date", "nunique"),
            rows=("rows", "sum"),
            mean_industries_per_date=("industries", "mean"),
            min_industries_per_date=("industries", "min"),
            max_industries_per_date=("industries", "max"),
            pe_non_null=("pe_non_null", "sum"),
            pb_non_null=("pb_non_null", "sum"),
            dividend_non_null=("dividend_non_null", "sum"),
        )
        .reset_index()
    )


def build_source_audit(
    *,
    valuation: pd.DataFrame,
    signal_panel: pd.DataFrame,
    args: argparse.Namespace,
    universe_breaks: pd.DataFrame,
) -> pd.DataFrame:
    old_universe = universe_breaks[universe_breaks["year"] <= 2021]["mean_industries_per_date"].mean()
    new_universe = universe_breaks[universe_breaks["year"] >= 2022]["mean_industries_per_date"].mean()
    rows = [
        {
            "audit_item": "valuation_history_loaded",
            "status": "pass" if not valuation.empty else "fail",
            "evidence": f"rows={len(valuation)}; start={date_to_str(valuation['valuation_trade_date'].min())}; end={date_to_str(valuation['valuation_trade_date'].max())}; industries={valuation['industry_code'].nunique()}",
            "action": "作为V2.6 PIT候选估值源使用。",
        },
        {
            "audit_item": "verified_available_date_rule",
            "status": "pass",
            "evidence": "published_at + frozen A-share trading calendar; fetched_at/source_version/source_hash/revision_status verified",
            "action": "持续保留原始版本和修订链；禁止改回trade_date加自然日推定。",
        },
        {
            "audit_item": "matched_signal_panel",
            "status": "pass" if len(signal_panel) > 0 else "fail",
            "evidence": f"signal_rows={len(signal_panel)}; date_start={date_to_str(signal_panel['trade_date'].min())}; date_end={date_to_str(signal_panel['trade_date'].max())}; min_cross_section_count={args.min_cross_section_count}",
            "action": "用于RankIC、事件回测、非重叠、样本外和逐日净值。",
        },
        {
            "audit_item": "industry_universe_break",
            "status": "warning" if new_universe > old_universe * 1.5 else "pass",
            "evidence": f"mean_industries_2015_2021={old_universe:.2f}; mean_industries_2022_2026={new_universe:.2f}",
            "action": "报告中必须分段看2015-2021与2022-2026，避免行业体系扩容污染结论。",
        },
        {
            "audit_item": "source_mouth_and_license",
            "status": "pending",
            "evidence": "数据来自公开申万指数分析日报表接口；未接Wind/Choice/iFinD/JQData/Tushare授权源复核。",
            "action": "通过授权源或人工抽样交叉校验字段口径后，才允许升级为正式PIT估值源。",
        },
    ]
    return pd.DataFrame(rows)


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
        "valuation_pit_score",
        "valuation_pressure_quality_score",
        "valuation_oversold_quality_score",
        "valuation_cross_section_score",
        "parent_relative_value_score",
        "historical_valuation_score",
        "industry_valuation_quality_score",
        "pe",
        "pb",
        "dividend_yield",
        "market_stress_score",
        "pressure_tier",
        "stabilized_oversold_signal",
        "momentum_trap_score",
        "sector_quality_data_required_flag",
    ]
    return frame[[col for col in cols if col in frame.columns]].sort_values(
        "valuation_pressure_quality_score", ascending=False
    ).head(50)


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
        "research_boundary": "V2.6 只接受带真实发布时间、抓取时间、来源版本/哈希和修订状态的估值历史；available_date 由冻结A股交易日历推导，仍保持 research_only，不升级 validated_alpha。",
        "valuation_start": date_to_str(valuation["valuation_trade_date"].min()) if not valuation.empty else "",
        "valuation_end": date_to_str(valuation["valuation_trade_date"].max()) if not valuation.empty else "",
        "valuation_rows": int(len(valuation)),
        "valuation_industries": int(valuation["industry_code"].nunique()) if not valuation.empty else 0,
        "release_lag_days": None,
        "availability_rule": "published_at_to_first_eligible_session_via_frozen_a_share_calendar",
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
    period_validation: pd.DataFrame,
    nav_metrics: pd.DataFrame,
    source_audit: pd.DataFrame,
    universe_breaks: pd.DataFrame,
    current_snapshot: pd.DataFrame,
) -> str:
    lines = [
        "# V2.6 行业历史估值 PIT 验证报告",
        "",
        f"版本：{VERSION}",
        "",
        "## 研究结论",
        "",
        summary["research_boundary"],
        "",
        f"- 历史估值区间：{summary['valuation_start']} 至 {summary['valuation_end']}",
        f"- 历史估值行数：{summary['valuation_rows']}",
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
        "V2.6 的重点不是宣布估值因子有效，而是第一次把历史 PE/PB/股息率纳入严格回测框架。即使有策略通过量化门槛，也仍需完成公开源口径、真实发布时间和授权源交叉验证后，才允许进入下一层级。",
        "",
        "## 策略参数排序",
        "",
    ]
    lines.extend(render_markdown_table(top_candidates.head(15)))

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

    lines.extend(["", "## 分阶段验证", ""])
    if period_validation.empty:
        lines.append("未生成分阶段验证。")
    else:
        best_ids = top_candidates.head(6)["策略"].dropna().unique().tolist() if not top_candidates.empty else []
        display = period_validation[period_validation["strategy_zh"].isin(best_ids)].copy()
        if display.empty:
            display = period_validation.sort_values("mean_relative_return", ascending=False).head(12).copy()
        else:
            display = display.sort_values(["strategy_zh", "top_n", "horizon", "period"]).head(18)
        for col in ["mean_relative_return", "benchmark_win_rate", "avg_valuation_pit_score"]:
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
                        "avg_valuation_pit_score",
                    ]
                ].rename(
                    columns={
                        "strategy_zh": "策略",
                        "top_n": "TopN",
                        "horizon": "持有期",
                        "period": "阶段",
                        "samples": "样本数",
                        "mean_relative_return": "相对收益",
                        "benchmark_win_rate": "跑赢比例",
                        "avg_valuation_pit_score": "平均估值分",
                    }
                )
            )
        )

    lines.extend(["", "## 逐日净值摘要", ""])
    if nav_metrics.empty:
        lines.append("未生成逐日净值。")
    else:
        display = nav_metrics.sort_values("relative_final_nav", ascending=False).head(12).copy()
        for col in ["annualized_relative_return", "relative_max_drawdown", "daily_relative_win_rate"]:
            if col in display.columns:
                display[col] = display[col].map(fmt_pct)
        display["relative_final_nav"] = display["relative_final_nav"].map(lambda value: fmt_float(value, 3))
        lines.extend(
            render_markdown_table(
                display[
                    [
                        "strategy_zh",
                        "top_n",
                        "daily_rows",
                        "relative_final_nav",
                        "annualized_relative_return",
                        "relative_max_drawdown",
                        "daily_relative_win_rate",
                    ]
                ].rename(
                    columns={
                        "strategy_zh": "策略",
                        "top_n": "TopN",
                        "daily_rows": "交易日数",
                        "relative_final_nav": "最终相对净值",
                        "annualized_relative_return": "年化相对收益",
                        "relative_max_drawdown": "相对最大回撤",
                        "daily_relative_win_rate": "日跑赢比例",
                    }
                )
            )
        )

    lines.extend(["", "## 数据源审计", ""])
    lines.extend(render_markdown_table(source_audit.rename(columns={"audit_item": "审计项", "status": "状态", "evidence": "证据", "action": "处理"})))

    lines.extend(["", "## 行业体系断点", ""])
    display_breaks = universe_breaks.copy()
    for col in ["mean_industries_per_date"]:
        if col in display_breaks.columns:
            display_breaks[col] = display_breaks[col].map(lambda value: fmt_float(value, 2))
    lines.extend(render_markdown_table(display_breaks.tail(8)))

    lines.extend(["", "## 当前信号快照", ""])
    if current_snapshot.empty:
        lines.append("未生成当前快照。")
    else:
        display = current_snapshot.head(15).copy()
        for col in [
            "valuation_pit_score",
            "valuation_pressure_quality_score",
            "historical_valuation_score",
            "industry_valuation_quality_score",
            "dividend_yield",
        ]:
            if col in display.columns:
                display[col] = display[col].map(fmt_pct)
        for col in ["pe", "pb"]:
            if col in display.columns:
                display[col] = display[col].map(lambda value: fmt_float(value, 2))
        lines.extend(
            render_markdown_table(
                display[
                    [
                        "industry_code",
                        "industry_name",
                        "parent_industry",
                        "valuation_pressure_quality_score",
                        "valuation_pit_score",
                        "historical_valuation_score",
                        "industry_valuation_quality_score",
                        "pe",
                        "pb",
                        "dividend_yield",
                        "pressure_tier",
                        "sector_quality_data_required_flag",
                    ]
                ].rename(
                    columns={
                        "industry_code": "行业代码",
                        "industry_name": "行业",
                        "parent_industry": "上级行业",
                        "valuation_pressure_quality_score": "估值压力质量分",
                        "valuation_pit_score": "估值PIT分",
                        "historical_valuation_score": "自身历史低估",
                        "industry_valuation_quality_score": "估值质量",
                        "pe": "PE",
                        "pb": "PB",
                        "dividend_yield": "股息率",
                        "pressure_tier": "压力状态",
                        "sector_quality_data_required_flag": "专项数据缺口",
                    }
                )
            )
        )

    lines.extend(
        [
            "",
            "## 复现文件",
            "",
            "- `debug/valuation_pit_signal_panel.csv`",
            "- `debug/event_backtest.csv`",
            "- `debug/nonoverlap_backtest.csv`",
            "- `debug/walk_forward_oos.csv`",
            "- `debug/bootstrap_confidence.csv`",
            "- `debug/daily_portfolio_nav.csv`",
            "- `debug/portfolio_nav_metrics.csv`",
            "- `debug/parameter_sensitivity.csv`",
            "- `debug/signal_rejection_log.csv`",
            "- `debug/rankic_report.csv`",
            "- `debug/group_return_report.csv`",
            "- `debug/period_validation_report.csv`",
            "- `debug/yearly_validation_report.csv`",
            "- `debug/valuation_universe_breaks.csv`",
            "- `debug/valuation_pit_source_audit.csv`",
            "- `debug/current_signal_snapshot.csv`",
        ]
    )
    return "\n".join(lines)


def parse_int_list(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def mean_col(frame: pd.DataFrame, column: str) -> float:
    return float(frame[column].mean()) if column in frame.columns and not frame[column].dropna().empty else math.nan


def first_value(frame: pd.DataFrame, column: str) -> Any:
    if column not in frame.columns or frame.empty:
        return ""
    value = frame[column].iloc[0]
    return "" if pd.isna(value) else value


def first_nonempty(frame: pd.DataFrame, column: str) -> str:
    if column not in frame.columns or frame.empty:
        return ""
    values = [str(value) for value in frame[column].dropna().tolist() if str(value)]
    return values[0] if values else ""


def mean_t_stat(values: list[float]) -> float:
    if len(values) < 2:
        return math.nan
    series = pd.Series(values, dtype=float).dropna()
    if len(series) < 2:
        return math.nan
    std = series.std(ddof=1)
    return float(series.mean() / (std / math.sqrt(len(series)))) if std and not pd.isna(std) else math.nan


def safe_number(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def fmt_pct(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
        return f"{float(value):.2%}"
    except Exception:
        return ""


def fmt_float(value: Any, digits: int = 3) -> str:
    try:
        if pd.isna(value):
            return ""
        return f"{float(value):.{digits}f}"
    except Exception:
        return ""


def date_to_str(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def translate_status(status: str) -> str:
    mapping = {
        "candidate_requires_source_audit": "量化候选，仍需源审计",
        "conditional_observation": "条件观察，禁止升级",
        "rejected_signal": "拒绝升级",
    }
    return mapping.get(status, status)


def translate_final_verdict(status: str) -> str:
    mapping = {
        "research_only_no_alpha_promotion": "研究观察，不升级alpha",
        "quant_candidate_but_source_audit_required": "出现量化候选，但公开估值源仍需审计",
        "conditional_observation_only": "仅条件观察，不升级alpha",
    }
    return mapping.get(status, status)


def render_markdown_table(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    columns = list(frame.columns)
    rows = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for record in frame.to_dict("records"):
        rows.append("| " + " | ".join(str(record.get(column, "")) for column in columns) + " |")
    return rows


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
