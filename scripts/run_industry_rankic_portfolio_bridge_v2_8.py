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
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_rankic_portfolio_bridge_v2_8"
V27_SCRIPT = ROOT / "scripts" / "run_industry_valuation_quality_v2_7.py"
VERSION = "2.8.0"


@dataclass(frozen=True)
class FactorSpec:
    factor_id: str
    factor_zh: str
    signal_col: str
    description: str


@dataclass(frozen=True)
class UniversePolicy:
    policy_id: str
    policy_zh: str
    description: str


@dataclass(frozen=True)
class RebalancePolicy:
    rebalance_id: str
    rebalance_zh: str
    step: int
    description: str


FACTOR_SPECS = [
    FactorSpec("quality_value_no_trap", "质量价值非陷阱", "quality_value_no_trap_score", "V2.7 最强 252 日 RankIC 因子。"),
    FactorSpec("quality_defensive", "质量防守", "valuation_quality_defensive_score", "低波动、回撤控制、流动性和股息支持。"),
    FactorSpec("post_2022_quality", "新体系质量", "post_2022_quality_score", "2022 年后完整行业体系质量分。"),
    FactorSpec("quality_core", "估值质量核心", "valuation_quality_core_score", "估值稳定性、股息连续性、市值深度和估值质量。"),
    FactorSpec("quality_value_blend", "质量+适度估值", "valuation_quality_value_score", "质量核心叠加自身历史和父行业相对估值。"),
    FactorSpec("valuation_pit_control", "V2.6纯估值对照", "valuation_pit_score", "低估值 PIT 对照因子。"),
]

UNIVERSE_POLICIES = [
    UniversePolicy("broad", "宽口径", "只要求 PIT 估值历史有效，不剔除金融地产建筑。"),
    UniversePolicy("sector_excluded", "剔除专项板块", "剔除银行、非银金融、房地产和建筑装饰等需要专项基本面数据的板块。"),
    UniversePolicy("post_2022_sector_excluded", "2022后剔除专项板块", "只看 2022 年后完整行业体系，并剔除专项板块。"),
]

REBALANCE_POLICIES = [
    RebalancePolicy("feature_monthly", "特征月频", 1, "使用每个特征日期调仓；基础特征面板约为 20 个交易日一步。"),
    RebalancePolicy("feature_quarterly", "特征季频", 3, "每 3 个特征日期调仓一次，降低换手。"),
    RebalancePolicy("feature_semiannual", "特征半年频", 6, "每 6 个特征日期调仓一次，检验低频可转译性。"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.8 RankIC-to-portfolio bridge validation.")
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="Historical price/return feature panel.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current industry name and parent mapping.")
    parser.add_argument("--valuation", default=str(DEFAULT_VALUATION), help="SWS daily industry valuation history.")
    parser.add_argument("--history-dir", default=str(DEFAULT_HISTORY_DIR), help="Cached industry index histories for daily NAV.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--horizon", type=int, default=252, help="Forward return horizon for bridge validation.")
    parser.add_argument("--top-ns", default="20,30", help="Top/Bottom basket sizes.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="One-way rebalance cost in bps.")
    parser.add_argument("--release-lag-days", type=int, default=1, help="Conservative lag from valuation trade_date to available_date.")
    parser.add_argument("--min-valuation-days", type=int, default=252, help="Minimum trailing valuation rows for PIT valuation gates.")
    parser.add_argument("--min-cross-section-count", type=int, default=20, help="Minimum industries per feature date.")
    parser.add_argument("--stress-threshold", type=float, default=0.65, help="Inherited market pressure threshold.")
    parser.add_argument("--extreme-stress-threshold", type=float, default=0.80, help="Inherited extreme pressure threshold.")
    parser.add_argument("--post-2022-start", default="2022-01-01", help="Start date for complete-universe validation.")
    parser.add_argument("--oos-split-ratio", type=float, default=0.70, help="Chronological OOS split ratio.")
    parser.add_argument("--bootstrap-rounds", type=int, default=500, help="Bootstrap rounds.")
    args = parser.parse_args()

    output_dir = Path(args.output)
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    top_ns = parse_int_list(args.top_ns)
    v27 = load_v27_module()
    v26 = v27.load_v26_module()
    v23 = v26.load_v23_module()

    signal_panel, valuation = build_signal_panel(v27, v26, v23, args)
    bridge_events = compute_bridge_events(signal_panel, top_ns, args.horizon, args.cost_bps)
    bridge_summary = summarize_bridge_events(bridge_events, args.oos_split_ratio)
    bootstrap = compute_bootstrap_confidence(bridge_events, args.bootstrap_rounds)
    rankic_decay = compute_rankic_decay(signal_panel)
    quantile_report = compute_quantile_monotonicity(signal_panel, args.horizon)
    factor_correlation = compute_factor_correlation(signal_panel)
    period_attribution = compute_period_attribution(bridge_events)
    sector_attribution = compute_sector_policy_attribution(bridge_summary)
    cross_section_coverage = compute_cross_section_coverage(signal_panel, top_ns)
    close_matrix = v23.load_close_matrix(Path(args.history_dir), signal_panel["industry_code"].dropna().unique().tolist())
    daily_nav = compute_daily_bridge_nav(signal_panel, close_matrix, top_ns, args.cost_bps)
    nav_metrics = compute_nav_metrics(daily_nav)
    final_summary = merge_validation_tables(bridge_summary, bootstrap, nav_metrics, quantile_report)
    rejection_log = build_signal_rejection_log(final_summary)
    top_candidates = build_top_candidates(final_summary, rejection_log)
    current_snapshot = build_current_snapshot(signal_panel)
    source_audit = build_source_audit(valuation, signal_panel, args, top_ns)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    signal_panel.to_csv(debug_dir / "rankic_bridge_signal_panel.csv", index=False, encoding="utf-8-sig")
    bridge_events.to_csv(debug_dir / "rebalance_portfolio_events.csv", index=False, encoding="utf-8-sig")
    final_summary.to_csv(debug_dir / "portfolio_bridge_summary.csv", index=False, encoding="utf-8-sig")
    daily_nav.to_csv(debug_dir / "daily_long_short_nav.csv", index=False, encoding="utf-8-sig")
    nav_metrics.to_csv(debug_dir / "nav_metrics.csv", index=False, encoding="utf-8-sig")
    rankic_decay.to_csv(debug_dir / "rankic_decay_report.csv", index=False, encoding="utf-8-sig")
    quantile_report.to_csv(debug_dir / "quantile_monotonicity_report.csv", index=False, encoding="utf-8-sig")
    period_attribution.to_csv(debug_dir / "period_attribution_report.csv", index=False, encoding="utf-8-sig")
    sector_attribution.to_csv(debug_dir / "sector_policy_attribution.csv", index=False, encoding="utf-8-sig")
    cross_section_coverage.to_csv(debug_dir / "same_date_cross_section_coverage.csv", index=False, encoding="utf-8-sig")
    factor_correlation.to_csv(debug_dir / "factor_correlation_report.csv", index=False, encoding="utf-8-sig")
    bootstrap.to_csv(debug_dir / "bootstrap_confidence.csv", index=False, encoding="utf-8-sig")
    rejection_log.to_csv(debug_dir / "signal_rejection_log.csv", index=False, encoding="utf-8-sig")
    source_audit.to_csv(debug_dir / "source_audit.csv", index=False, encoding="utf-8-sig")
    current_snapshot.to_csv(debug_dir / "current_signal_snapshot.csv", index=False, encoding="utf-8-sig")

    summary = build_run_summary(
        signal_panel=signal_panel,
        valuation=valuation,
        bridge_events=bridge_events,
        final_summary=final_summary,
        rejection_log=rejection_log,
        source_audit=source_audit,
        args=args,
        top_ns=top_ns,
    )
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            rankic_decay=rankic_decay,
            quantile_report=quantile_report,
            sector_attribution=sector_attribution,
            period_attribution=period_attribution,
            nav_metrics=nav_metrics,
        source_audit=source_audit,
        cross_section_coverage=cross_section_coverage,
        current_snapshot=current_snapshot,
        ),
        encoding="utf-8",
    )

    print(f"V{VERSION} RankIC 到组合收益桥接验证完成")
    print(f"信号面板行数={summary['signal_rows']}")
    print(f"桥接事件行数={summary['bridge_event_rows']}")
    print(f"候选待源审计信号数={summary['candidate_requires_source_audit_count']}")
    print(f"条件观察信号数={summary['conditional_observation_count']}")
    print(f"拒绝信号数={summary['rejected_signal_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v27_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_valuation_quality_v2_7", V27_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load V2.7 module from {V27_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_signal_panel(v27: Any, v26: Any, v23: Any, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
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
    signal_panel = v27.build_v27_signal_panel(base_signal, post_2022_start=args.post_2022_start)
    signal_panel = v26.filter_cross_section_dates(signal_panel, min_count=args.min_cross_section_count)
    return signal_panel.sort_values(["trade_date", "industry_code"]), valuation


def compute_bridge_events(signal_panel: pd.DataFrame, top_ns: list[int], horizon: int, cost_bps: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty:
        return pd.DataFrame(rows)
    label = f"forward_return_{horizon}d"
    benchmark_label = f"benchmark_forward_return_{horizon}d"
    if label not in signal_panel.columns:
        return pd.DataFrame(rows)

    all_dates = sorted(signal_panel["trade_date"].dropna().unique().tolist())
    for factor in FACTOR_SPECS:
        if factor.signal_col not in signal_panel.columns:
            continue
        for policy in UNIVERSE_POLICIES:
            for rebalance in REBALANCE_POLICIES:
                rebalance_dates = all_dates[:: rebalance.step]
                for top_n in top_ns:
                    previous_top: set[str] = set()
                    previous_bottom: set[str] = set()
                    for trade_date in rebalance_dates:
                        group = signal_panel[signal_panel["trade_date"] == trade_date].copy()
                        eligible = apply_universe_policy(group, policy).dropna(subset=[factor.signal_col, label])
                        min_count = max(top_n * 2, 20)
                        if len(eligible) < min_count:
                            continue
                        ranked = eligible.sort_values(factor.signal_col, ascending=False).copy()
                        top = ranked.head(top_n)
                        bottom = ranked.tail(top_n)
                        top_codes = set(top["industry_code"].astype(str).tolist())
                        bottom_codes = set(bottom["industry_code"].astype(str).tolist())
                        top_turnover = len(top_codes.symmetric_difference(previous_top)) / max(len(top_codes), 1) if previous_top else 1.0
                        bottom_turnover = (
                            len(bottom_codes.symmetric_difference(previous_bottom)) / max(len(bottom_codes), 1)
                            if previous_bottom
                            else 1.0
                        )
                        top_cost = top_turnover * cost_bps / 10000.0
                        bottom_cost = bottom_turnover * cost_bps / 10000.0
                        top_return = float(top[label].mean())
                        bottom_return = float(bottom[label].mean())
                        benchmark_return = (
                            float(eligible[benchmark_label].dropna().mean())
                            if benchmark_label in eligible.columns and not eligible[benchmark_label].dropna().empty
                            else float(eligible[label].mean())
                        )
                        long_net_return = top_return - top_cost
                        long_relative_return = long_net_return - benchmark_return
                        long_short_return = top_return - bottom_return - top_cost - bottom_cost
                        rows.append(
                            {
                                "trade_date": date_to_str(trade_date),
                                "factor_id": factor.factor_id,
                                "factor_zh": factor.factor_zh,
                                "universe_policy": policy.policy_id,
                                "universe_policy_zh": policy.policy_zh,
                                "rebalance_id": rebalance.rebalance_id,
                                "rebalance_zh": rebalance.rebalance_zh,
                                "rebalance_step": int(rebalance.step),
                                "horizon": int(horizon),
                                "top_n": int(top_n),
                                "eligible_count": int(len(eligible)),
                                "top_forward_return": top_return,
                                "bottom_forward_return": bottom_return,
                                "benchmark_forward_return": benchmark_return,
                                "top_turnover": top_turnover,
                                "bottom_turnover": bottom_turnover,
                                "cost_bps": cost_bps,
                                "long_net_return": long_net_return,
                                "long_relative_return": long_relative_return,
                                "long_short_return": long_short_return,
                                "top_mean_score": mean_col(top, factor.signal_col),
                                "bottom_mean_score": mean_col(bottom, factor.signal_col),
                                "score_spread": mean_col(top, factor.signal_col) - mean_col(bottom, factor.signal_col),
                                "top_sector_exclusion_rate": mean_bool(top, "sector_quality_exclusion_flag"),
                                "bottom_sector_exclusion_rate": mean_bool(bottom, "sector_quality_exclusion_flag"),
                                "market_regime": first_mode(eligible, "market_regime"),
                                "volatility_regime": first_mode(eligible, "volatility_regime"),
                                "period": "2022-2026新行业体系" if pd.Timestamp(trade_date).year >= 2022 else "2015-2021旧行业体系",
                                "selected_top_industries": "|".join(top["industry_name"].fillna(top["industry_code"]).astype(str).tolist()),
                                "selected_bottom_industries": "|".join(bottom["industry_name"].fillna(bottom["industry_code"]).astype(str).tolist()),
                                "selected_top_parents": "|".join(top["parent_industry"].fillna("").astype(str).tolist()),
                                "selected_bottom_parents": "|".join(bottom["parent_industry"].fillna("").astype(str).tolist()),
                            }
                        )
                        previous_top = top_codes
                        previous_bottom = bottom_codes
    return pd.DataFrame(rows)


def apply_universe_policy(frame: pd.DataFrame, policy: UniversePolicy) -> pd.DataFrame:
    eligible = frame[frame["valuation_history_gate"].fillna(False)].copy()
    if policy.policy_id == "sector_excluded":
        eligible = eligible[~eligible["sector_quality_exclusion_flag"].fillna(False)]
    elif policy.policy_id == "post_2022_sector_excluded":
        eligible = eligible[
            eligible["post_2022_sample_flag"].fillna(False) & ~eligible["sector_quality_exclusion_flag"].fillna(False)
        ]
    return eligible


def summarize_bridge_events(events: pd.DataFrame, split_ratio: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if events.empty:
        return pd.DataFrame(rows)
    dates = sorted(events["trade_date"].dropna().unique().tolist())
    split_index = min(max(1, int(len(dates) * min(max(split_ratio, 0.10), 0.90))), len(dates) - 1)
    oos_dates = set(dates[split_index:])
    for keys, group in events.groupby(
        ["factor_id", "factor_zh", "universe_policy", "universe_policy_zh", "rebalance_id", "rebalance_zh", "top_n", "horizon"],
        sort=True,
    ):
        row = metric_row(group, keys)
        oos = group[group["trade_date"].isin(oos_dates)]
        row["oos_samples"] = int(len(oos))
        row["oos_mean_long_relative_return"] = float(oos["long_relative_return"].mean()) if not oos.empty else math.nan
        row["oos_mean_long_short_return"] = float(oos["long_short_return"].mean()) if not oos.empty else math.nan
        row["oos_long_short_win_rate"] = float((oos["long_short_return"] > 0).mean()) if not oos.empty else math.nan
        old = group[group["period"] == "2015-2021旧行业体系"]
        new = group[group["period"] == "2022-2026新行业体系"]
        row["legacy_samples"] = int(len(old))
        row["legacy_mean_long_short_return"] = float(old["long_short_return"].mean()) if not old.empty else math.nan
        row["post_2022_samples"] = int(len(new))
        row["post_2022_mean_long_short_return"] = float(new["long_short_return"].mean()) if not new.empty else math.nan
        rows.append(row)
    return pd.DataFrame(rows).sort_values("mean_long_short_return", ascending=False)


def metric_row(group: pd.DataFrame, keys: tuple[Any, ...]) -> dict[str, Any]:
    factor_id, factor_zh, universe_policy, universe_policy_zh, rebalance_id, rebalance_zh, top_n, horizon = keys
    return {
        "factor_id": factor_id,
        "factor_zh": factor_zh,
        "universe_policy": universe_policy,
        "universe_policy_zh": universe_policy_zh,
        "rebalance_id": rebalance_id,
        "rebalance_zh": rebalance_zh,
        "top_n": int(top_n),
        "horizon": int(horizon),
        "samples": int(len(group)),
        "mean_long_relative_return": float(group["long_relative_return"].mean()),
        "mean_long_short_return": float(group["long_short_return"].mean()),
        "median_long_short_return": float(group["long_short_return"].median()),
        "long_relative_win_rate": float((group["long_relative_return"] > 0).mean()),
        "long_short_win_rate": float((group["long_short_return"] > 0).mean()),
        "mean_top_return": float(group["top_forward_return"].mean()),
        "mean_bottom_return": float(group["bottom_forward_return"].mean()),
        "mean_benchmark_return": float(group["benchmark_forward_return"].mean()),
        "mean_top_turnover": float(group["top_turnover"].mean()),
        "mean_bottom_turnover": float(group["bottom_turnover"].mean()),
        "mean_eligible_count": float(group["eligible_count"].mean()),
        "mean_score_spread": float(group["score_spread"].mean()),
        "top_sector_exclusion_rate": float(group["top_sector_exclusion_rate"].mean()),
        "bottom_sector_exclusion_rate": float(group["bottom_sector_exclusion_rate"].mean()),
        "worst_long_short_return": float(group["long_short_return"].min()),
        "best_long_short_return": float(group["long_short_return"].max()),
    }


def compute_bootstrap_confidence(events: pd.DataFrame, rounds: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if events.empty:
        return pd.DataFrame(rows)
    rng = np.random.default_rng(20260614)
    frame = events.copy()
    frame["year_block"] = pd.to_datetime(frame["trade_date"]).dt.year.astype(str)
    for keys, group in frame.groupby(
        ["factor_id", "universe_policy", "rebalance_id", "top_n", "horizon"],
        sort=True,
    ):
        blocks = sorted(group["year_block"].dropna().unique().tolist())
        samples: list[float] = []
        if len(blocks) >= 2:
            block_map = {block: group[group["year_block"] == block]["long_short_return"].dropna().to_numpy() for block in blocks}
            for _ in range(rounds):
                sampled_blocks = rng.choice(blocks, size=len(blocks), replace=True)
                values = np.concatenate([block_map[block] for block in sampled_blocks if len(block_map[block])])
                if len(values):
                    samples.append(float(np.mean(values)))
        rows.append(
            {
                "factor_id": keys[0],
                "universe_policy": keys[1],
                "rebalance_id": keys[2],
                "top_n": int(keys[3]),
                "horizon": int(keys[4]),
                "block_count": int(len(blocks)),
                "bootstrap_rounds": int(len(samples)),
                "ci_5": float(np.quantile(samples, 0.05)) if samples else math.nan,
                "ci_50": float(np.quantile(samples, 0.50)) if samples else math.nan,
                "ci_95": float(np.quantile(samples, 0.95)) if samples else math.nan,
                "probability_positive": float(np.mean(np.array(samples) > 0)) if samples else math.nan,
            }
        )
    return pd.DataFrame(rows)


def compute_rankic_decay(signal_panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for factor in FACTOR_SPECS:
        if factor.signal_col not in signal_panel.columns:
            continue
        for horizon in [60, 120, 252]:
            label = f"benchmark_relative_return_{horizon}d"
            if label not in signal_panel.columns:
                continue
            for policy in UNIVERSE_POLICIES:
                rankics: list[float] = []
                for _, group in signal_panel.groupby("trade_date", sort=True):
                    eligible = apply_universe_policy(group, policy)
                    sample = eligible[[factor.signal_col, label]].dropna()
                    if len(sample) < 12:
                        continue
                    factor_rank = sample[factor.signal_col].rank()
                    label_rank = sample[label].rank()
                    if factor_rank.nunique() < 2 or label_rank.nunique() < 2:
                        continue
                    corr = factor_rank.corr(label_rank)
                    if not pd.isna(corr):
                        rankics.append(float(corr))
                rows.append(
                    {
                        "factor_id": factor.factor_id,
                        "factor_zh": factor.factor_zh,
                        "universe_policy": policy.policy_id,
                        "universe_policy_zh": policy.policy_zh,
                        "horizon": int(horizon),
                        "mean_rankic": float(np.mean(rankics)) if rankics else math.nan,
                        "rankic_t_stat": mean_t_stat(rankics),
                        "positive_ratio": float(np.mean(np.array(rankics) > 0)) if rankics else math.nan,
                        "date_count": int(len(rankics)),
                    }
                )
    return pd.DataFrame(rows)


def compute_quantile_monotonicity(signal_panel: pd.DataFrame, horizon: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    label = f"benchmark_relative_return_{horizon}d"
    if label not in signal_panel.columns:
        return pd.DataFrame(rows)
    for factor in FACTOR_SPECS:
        if factor.signal_col not in signal_panel.columns:
            continue
        for policy in UNIVERSE_POLICIES:
            dated: list[pd.DataFrame] = []
            for trade_date, group in signal_panel.groupby("trade_date", sort=True):
                eligible = apply_universe_policy(group, policy)
                sample = eligible[[factor.signal_col, label]].dropna().copy()
                if len(sample) < 25:
                    continue
                try:
                    sample["quantile"] = pd.qcut(sample[factor.signal_col], 5, labels=False, duplicates="drop") + 1
                except ValueError:
                    continue
                sample["trade_date"] = trade_date
                dated.append(sample)
            if not dated:
                continue
            combined = pd.concat(dated, ignore_index=True)
            quantile_means = combined.groupby("quantile")[label].mean()
            top = float(quantile_means.get(5, math.nan))
            bottom = float(quantile_means.get(1, math.nan))
            quantile_frame = quantile_means.reset_index()
            monotonic_corr = quantile_frame["quantile"].rank().corr(quantile_frame[label].rank())
            for quantile, group in combined.groupby("quantile", sort=True):
                rows.append(
                    {
                        "factor_id": factor.factor_id,
                        "factor_zh": factor.factor_zh,
                        "universe_policy": policy.policy_id,
                        "universe_policy_zh": policy.policy_zh,
                        "horizon": int(horizon),
                        "quantile": int(quantile),
                        "mean_relative_return": float(group[label].mean()),
                        "win_rate": float((group[label] > 0).mean()),
                        "samples": int(len(group)),
                        "top_minus_bottom": top - bottom if not math.isnan(top) and not math.isnan(bottom) else math.nan,
                        "monotonic_spearman": float(monotonic_corr) if not pd.isna(monotonic_corr) else math.nan,
                    }
                )
    return pd.DataFrame(rows)


def compute_factor_correlation(signal_panel: pd.DataFrame) -> pd.DataFrame:
    cols = [factor.signal_col for factor in FACTOR_SPECS if factor.signal_col in signal_panel.columns]
    rows: list[dict[str, Any]] = []
    for trade_date, group in signal_panel.groupby("trade_date", sort=True):
        sample = group[cols].dropna(how="all")
        if len(sample) < 12:
            continue
        corr = sample.corr(method="spearman")
        for left in cols:
            for right in cols:
                if left >= right:
                    continue
                value = corr.loc[left, right]
                if not pd.isna(value):
                    rows.append({"trade_date": date_to_str(trade_date), "left_factor": left, "right_factor": right, "spearman_corr": float(value)})
    if not rows:
        return pd.DataFrame()
    raw = pd.DataFrame(rows)
    return (
        raw.groupby(["left_factor", "right_factor"])
        .agg(mean_spearman_corr=("spearman_corr", "mean"), median_spearman_corr=("spearman_corr", "median"), date_count=("trade_date", "nunique"))
        .reset_index()
        .sort_values("mean_spearman_corr", ascending=False)
    )


def compute_period_attribution(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for keys, group in events.groupby(["factor_id", "factor_zh", "universe_policy_zh", "rebalance_zh", "top_n", "period"], sort=True):
        rows.append(
            {
                "factor_id": keys[0],
                "factor_zh": keys[1],
                "universe_policy_zh": keys[2],
                "rebalance_zh": keys[3],
                "top_n": int(keys[4]),
                "period": keys[5],
                "samples": int(len(group)),
                "mean_long_relative_return": float(group["long_relative_return"].mean()),
                "mean_long_short_return": float(group["long_short_return"].mean()),
                "long_short_win_rate": float((group["long_short_return"] > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def compute_sector_policy_attribution(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    key_cols = ["factor_id", "factor_zh", "rebalance_id", "rebalance_zh", "top_n", "horizon"]
    broad = summary[summary["universe_policy"] == "broad"][key_cols + ["mean_long_short_return", "mean_long_relative_return"]].copy()
    excluded = summary[summary["universe_policy"] == "sector_excluded"][key_cols + ["mean_long_short_return", "mean_long_relative_return"]].copy()
    if broad.empty or excluded.empty:
        return pd.DataFrame()
    merged = broad.merge(excluded, on=key_cols, suffixes=("_broad", "_sector_excluded"))
    merged["long_short_sector_exclusion_delta"] = (
        merged["mean_long_short_return_sector_excluded"] - merged["mean_long_short_return_broad"]
    )
    merged["long_relative_sector_exclusion_delta"] = (
        merged["mean_long_relative_return_sector_excluded"] - merged["mean_long_relative_return_broad"]
    )
    return merged.sort_values("long_short_sector_exclusion_delta", ascending=False)


def compute_cross_section_coverage(signal_panel: pd.DataFrame, top_ns: list[int]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty:
        return pd.DataFrame(rows)
    max_top_n = max(top_ns) if top_ns else 0
    for policy in UNIVERSE_POLICIES:
        dated_rows: list[dict[str, Any]] = []
        for trade_date, group in signal_panel.groupby("trade_date", sort=True):
            eligible = apply_universe_policy(group, policy).dropna(subset=["forward_return_252d"])
            if eligible.empty:
                continue
            row = {
                "trade_date": pd.Timestamp(trade_date),
                "universe_policy": policy.policy_id,
                "universe_policy_zh": policy.policy_zh,
                "eligible_count": int(len(eligible)),
                "year": int(pd.Timestamp(trade_date).year),
                "period": "2022-2026新行业体系" if pd.Timestamp(trade_date).year >= 2022 else "2015-2021旧行业体系",
            }
            for top_n in top_ns:
                row[f"enough_for_top_bottom_{top_n}"] = len(eligible) >= top_n * 2
            row["enough_for_max_top_bottom"] = len(eligible) >= max_top_n * 2 if max_top_n else False
            dated_rows.append(row)
        if not dated_rows:
            continue
        dated = pd.DataFrame(dated_rows)
        agg_map: dict[str, Any] = {
            "date_count": ("trade_date", "nunique"),
            "mean_eligible_count": ("eligible_count", "mean"),
            "min_eligible_count": ("eligible_count", "min"),
            "max_eligible_count": ("eligible_count", "max"),
        }
        for top_n in top_ns:
            agg_map[f"dates_enough_top_bottom_{top_n}"] = (f"enough_for_top_bottom_{top_n}", "sum")
        summary = dated.groupby(["universe_policy", "universe_policy_zh", "period"], sort=True).agg(**agg_map).reset_index()
        for top_n in top_ns:
            summary[f"share_enough_top_bottom_{top_n}"] = (
                summary[f"dates_enough_top_bottom_{top_n}"] / summary["date_count"]
            )
        rows.extend(summary.to_dict("records"))
    return pd.DataFrame(rows)


def compute_daily_bridge_nav(signal_panel: pd.DataFrame, close_matrix: pd.DataFrame, top_ns: list[int], cost_bps: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty or close_matrix.empty:
        return pd.DataFrame(rows)
    returns = close_matrix.pct_change(fill_method=None)
    close_dates = close_matrix.index.sort_values()
    feature_dates = sorted(signal_panel["trade_date"].dropna().unique().tolist())
    for factor in FACTOR_SPECS:
        if factor.signal_col not in signal_panel.columns:
            continue
        for policy in UNIVERSE_POLICIES:
            for rebalance in REBALANCE_POLICIES:
                selected_dates = feature_dates[:: rebalance.step]
                actual_rebalances: list[tuple[pd.Timestamp, pd.Timestamp]] = []
                seen_dates: set[pd.Timestamp] = set()
                for feature_date in selected_dates:
                    idx = close_dates.searchsorted(pd.Timestamp(feature_date))
                    if idx >= len(close_dates):
                        continue
                    actual_date = pd.Timestamp(close_dates[idx])
                    if actual_date in seen_dates:
                        continue
                    seen_dates.add(actual_date)
                    actual_rebalances.append((pd.Timestamp(feature_date), actual_date))
                for top_n in top_ns:
                    long_nav = 1.0
                    benchmark_nav = 1.0
                    long_short_nav = 1.0
                    previous_top: set[str] = set()
                    previous_bottom: set[str] = set()
                    for idx, (feature_date, actual_date) in enumerate(actual_rebalances):
                        group = signal_panel[signal_panel["trade_date"] == feature_date].copy()
                        eligible = apply_universe_policy(group, policy).dropna(subset=[factor.signal_col])
                        min_count = max(top_n * 2, 20)
                        if len(eligible) < min_count:
                            continue
                        ranked = eligible.sort_values(factor.signal_col, ascending=False)
                        top_codes = set(ranked.head(top_n)["industry_code"].astype(str).str.zfill(6).tolist())
                        bottom_codes = set(ranked.tail(top_n)["industry_code"].astype(str).str.zfill(6).tolist())
                        top_codes = {code for code in top_codes if code in returns.columns}
                        bottom_codes = {code for code in bottom_codes if code in returns.columns}
                        if len(top_codes) < max(1, top_n // 2) or len(bottom_codes) < max(1, top_n // 2):
                            continue
                        next_date = actual_rebalances[idx + 1][1] if idx + 1 < len(actual_rebalances) else close_dates[-1]
                        period_dates = returns.index[(returns.index > actual_date) & (returns.index <= next_date)]
                        if len(period_dates) == 0:
                            continue
                        top_turnover = len(top_codes.symmetric_difference(previous_top)) / max(len(top_codes), 1) if previous_top else 1.0
                        bottom_turnover = (
                            len(bottom_codes.symmetric_difference(previous_bottom)) / max(len(bottom_codes), 1)
                            if previous_bottom
                            else 1.0
                        )
                        cost_return = (top_turnover + bottom_turnover) * cost_bps / 10000.0
                        long_cost_return = top_turnover * cost_bps / 10000.0
                        first_period_day = True
                        for day in period_dates:
                            top_returns = returns.loc[day, sorted(top_codes)].dropna()
                            bottom_returns = returns.loc[day, sorted(bottom_codes)].dropna()
                            benchmark_returns = returns.loc[day].dropna()
                            if top_returns.empty or bottom_returns.empty or benchmark_returns.empty:
                                continue
                            long_daily = float(top_returns.mean())
                            bottom_daily = float(bottom_returns.mean())
                            benchmark_daily = float(benchmark_returns.mean())
                            long_net_daily = long_daily - long_cost_return if first_period_day else long_daily
                            long_short_daily = long_daily - bottom_daily - cost_return if first_period_day else long_daily - bottom_daily
                            first_period_day = False
                            long_nav *= 1.0 + long_net_daily
                            benchmark_nav *= 1.0 + benchmark_daily
                            long_short_nav *= 1.0 + long_short_daily
                            rows.append(
                                {
                                    "trade_date": date_to_str(day),
                                    "feature_date": date_to_str(feature_date),
                                    "factor_id": factor.factor_id,
                                    "factor_zh": factor.factor_zh,
                                    "universe_policy": policy.policy_id,
                                    "universe_policy_zh": policy.policy_zh,
                                    "rebalance_id": rebalance.rebalance_id,
                                    "rebalance_zh": rebalance.rebalance_zh,
                                    "top_n": int(top_n),
                                    "long_daily_return": long_daily,
                                    "bottom_daily_return": bottom_daily,
                                    "benchmark_daily_return": benchmark_daily,
                                    "long_net_daily_return": long_net_daily,
                                    "long_short_daily_return": long_short_daily,
                                    "long_nav": long_nav,
                                    "benchmark_nav": benchmark_nav,
                                    "long_relative_nav": long_nav / benchmark_nav if benchmark_nav else math.nan,
                                    "long_short_nav": long_short_nav,
                                    "top_turnover": top_turnover if not first_period_day else 0.0,
                                    "bottom_turnover": bottom_turnover if not first_period_day else 0.0,
                                }
                            )
                        previous_top = top_codes
                        previous_bottom = bottom_codes
    return pd.DataFrame(rows)


def compute_nav_metrics(daily_nav: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if daily_nav.empty:
        return pd.DataFrame(rows)
    for keys, group in daily_nav.groupby(
        ["factor_id", "factor_zh", "universe_policy", "universe_policy_zh", "rebalance_id", "rebalance_zh", "top_n"],
        sort=True,
    ):
        ordered = group.sort_values("trade_date")
        days = max(1, len(ordered))
        years = days / 252.0
        long_relative_nav = float(ordered["long_relative_nav"].iloc[-1])
        long_short_nav = float(ordered["long_short_nav"].iloc[-1])
        long_short_dd = ordered["long_short_nav"] / ordered["long_short_nav"].cummax() - 1.0
        long_relative_dd = ordered["long_relative_nav"] / ordered["long_relative_nav"].cummax() - 1.0
        rows.append(
            {
                "factor_id": keys[0],
                "factor_zh": keys[1],
                "universe_policy": keys[2],
                "universe_policy_zh": keys[3],
                "rebalance_id": keys[4],
                "rebalance_zh": keys[5],
                "top_n": int(keys[6]),
                "daily_rows": int(days),
                "start_date": ordered["trade_date"].iloc[0],
                "end_date": ordered["trade_date"].iloc[-1],
                "long_relative_final_nav": long_relative_nav,
                "long_short_final_nav": long_short_nav,
                "annualized_long_relative_return": long_relative_nav ** (1 / years) - 1 if long_relative_nav > 0 else math.nan,
                "annualized_long_short_return": long_short_nav ** (1 / years) - 1 if long_short_nav > 0 else math.nan,
                "long_relative_max_drawdown": float(long_relative_dd.min()),
                "long_short_max_drawdown": float(long_short_dd.min()),
                "daily_long_short_win_rate": float((ordered["long_short_daily_return"] > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def merge_validation_tables(
    summary: pd.DataFrame,
    bootstrap: pd.DataFrame,
    nav_metrics: pd.DataFrame,
    quantile_report: pd.DataFrame,
) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    merged = summary.merge(
        bootstrap,
        on=["factor_id", "universe_policy", "rebalance_id", "top_n", "horizon"],
        how="left",
    )
    merged = merged.merge(
        nav_metrics,
        on=["factor_id", "factor_zh", "universe_policy", "universe_policy_zh", "rebalance_id", "rebalance_zh", "top_n"],
        how="left",
    )
    if not quantile_report.empty:
        q = (
            quantile_report.groupby(["factor_id", "universe_policy", "horizon"])
            .agg(top_minus_bottom=("top_minus_bottom", "first"), monotonic_spearman=("monotonic_spearman", "first"))
            .reset_index()
        )
        merged = merged.merge(q, on=["factor_id", "universe_policy", "horizon"], how="left")
    else:
        merged["top_minus_bottom"] = math.nan
        merged["monotonic_spearman"] = math.nan
    merged["sample_strength"] = merged.apply(
        lambda row: min(
            safe_number(row.get("samples")) / 40.0,
            safe_number(row.get("oos_samples")) / 12.0,
            safe_number(row.get("legacy_samples")) / 12.0 if safe_number(row.get("legacy_samples")) else 0.0,
            safe_number(row.get("post_2022_samples")) / 12.0 if safe_number(row.get("post_2022_samples")) else 0.0,
            safe_number(row.get("daily_rows")) / 252.0 if safe_number(row.get("daily_rows")) else 0.0,
            1.0,
        ),
        axis=1,
    )
    merged["robust_score"] = (
        safe_series(merged["mean_long_short_return"])
        + safe_series(merged["oos_mean_long_short_return"])
        + safe_series(merged["legacy_mean_long_short_return"])
        + safe_series(merged["post_2022_mean_long_short_return"])
        + 0.25 * safe_series(merged["annualized_long_short_return"])
        + 0.01 * safe_series(merged["long_short_win_rate"])
        + 0.01 * safe_series(merged["probability_positive"])
    ) * (0.20 + 0.80 * safe_series(merged["sample_strength"]))
    return merged.sort_values("robust_score", ascending=False)


def build_signal_rejection_log(summary: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if summary.empty:
        return pd.DataFrame(rows)
    for row in summary.to_dict("records"):
        checks = {
            "full_long_short_positive": row["mean_long_short_return"] > 0,
            "oos_long_short_positive": row["oos_mean_long_short_return"] > 0,
            "legacy_positive": row["legacy_mean_long_short_return"] > 0,
            "post_2022_positive": row["post_2022_mean_long_short_return"] > 0,
            "long_relative_positive": row["mean_long_relative_return"] > 0,
            "win_rate": row["long_short_win_rate"] > 0.50,
            "bootstrap_ci": row["ci_5"] > 0,
            "bootstrap_prob": row["probability_positive"] > 0.60,
            "nav_positive": row["long_short_final_nav"] > 1.0,
            "quantile_spread": row.get("top_minus_bottom", math.nan) > 0,
            "sample_enough": row["sample_strength"] >= 0.75,
        }
        reasons: list[str] = []
        reason_map = {
            "full_long_short_positive": "全样本Top-Bottom不为正",
            "oos_long_short_positive": "样本外Top-Bottom不为正",
            "legacy_positive": "2015-2021阶段Top-Bottom不为正",
            "post_2022_positive": "2022后阶段Top-Bottom不为正",
            "long_relative_positive": "多头相对全行业不为正",
            "win_rate": "Top-Bottom跑赢比例不足50%",
            "bootstrap_ci": "bootstrap置信下沿不为正",
            "bootstrap_prob": "bootstrap正收益概率不足60%",
            "nav_positive": "逐日Top-Bottom净值不大于1",
            "quantile_spread": "分组Top-Bottom均值不为正",
            "sample_enough": "样本强度不足",
        }
        for key, passed in checks.items():
            if not passed:
                reasons.append(reason_map[key])
        if all(checks.values()):
            status = "candidate_requires_source_audit"
            reasons.append("量化桥接门槛通过，但公开估值源仍需口径和发布时间审计")
        elif any([checks["full_long_short_positive"], checks["oos_long_short_positive"], checks["post_2022_positive"], checks["nav_positive"]]):
            status = "conditional_observation"
        else:
            status = "rejected_signal"
        rows.append(
            {
                "factor_id": row["factor_id"],
                "factor_zh": row["factor_zh"],
                "universe_policy": row["universe_policy"],
                "universe_policy_zh": row["universe_policy_zh"],
                "rebalance_id": row["rebalance_id"],
                "rebalance_zh": row["rebalance_zh"],
                "top_n": int(row["top_n"]),
                "horizon": int(row["horizon"]),
                "signal_status": status,
                "mean_long_short_return": row["mean_long_short_return"],
                "oos_mean_long_short_return": row["oos_mean_long_short_return"],
                "legacy_mean_long_short_return": row["legacy_mean_long_short_return"],
                "post_2022_mean_long_short_return": row["post_2022_mean_long_short_return"],
                "long_short_final_nav": row.get("long_short_final_nav", math.nan),
                "ci_5": row.get("ci_5", math.nan),
                "probability_positive": row.get("probability_positive", math.nan),
                "sample_strength": row.get("sample_strength", math.nan),
                "rejection_reasons": "；".join(reasons),
            }
        )
    return pd.DataFrame(rows)


def build_top_candidates(summary: pd.DataFrame, rejection_log: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    merged = summary.merge(
        rejection_log[
            [
                "factor_id",
                "universe_policy",
                "rebalance_id",
                "top_n",
                "horizon",
                "signal_status",
                "rejection_reasons",
            ]
        ],
        on=["factor_id", "universe_policy", "rebalance_id", "top_n", "horizon"],
        how="left",
    )
    rows: list[dict[str, Any]] = []
    for row in merged.sort_values("robust_score", ascending=False).head(30).to_dict("records"):
        rows.append(
            {
                "因子": row["factor_zh"],
                "样本口径": row["universe_policy_zh"],
                "调仓": row["rebalance_zh"],
                "TopN": int(row["top_n"]),
                "持有期": int(row["horizon"]),
                "状态": translate_status(str(row.get("signal_status", ""))),
                "Top-Bottom": fmt_pct(row["mean_long_short_return"]),
                "样本外Top-Bottom": fmt_pct(row["oos_mean_long_short_return"]),
                "旧体系Top-Bottom": fmt_pct(row["legacy_mean_long_short_return"]),
                "2022后Top-Bottom": fmt_pct(row["post_2022_mean_long_short_return"]),
                "多头相对收益": fmt_pct(row["mean_long_relative_return"]),
                "分组Top-Bottom": fmt_pct(row.get("top_minus_bottom", math.nan)),
                "Bootstrap下沿": fmt_pct(row.get("ci_5", math.nan)),
                "Bootstrap为正概率": fmt_pct(row.get("probability_positive", math.nan)),
                "逐日Top-Bottom净值": fmt_float(row.get("long_short_final_nav", math.nan), 3),
                "年化Top-Bottom": fmt_pct(row.get("annualized_long_short_return", math.nan)),
                "样本强度": fmt_pct(row.get("sample_strength", math.nan)),
                "样本数": int(row["samples"]),
                "拒绝或保留原因": row.get("rejection_reasons", ""),
            }
        )
    return pd.DataFrame(rows)


def build_current_snapshot(signal_panel: pd.DataFrame) -> pd.DataFrame:
    if signal_panel.empty:
        return pd.DataFrame()
    latest = signal_panel["trade_date"].max()
    frame = signal_panel[signal_panel["trade_date"] == latest].copy()
    cols = [
        "trade_date",
        "industry_code",
        "industry_name",
        "parent_industry",
        "quality_value_no_trap_score",
        "valuation_quality_defensive_score",
        "post_2022_quality_score",
        "valuation_quality_core_score",
        "valuation_quality_value_score",
        "valuation_pit_score",
        "sector_quality_exclusion_flag",
        "pe",
        "pb",
        "dividend_yield",
        "momentum_trap_score",
    ]
    return frame[[col for col in cols if col in frame.columns]].sort_values("quality_value_no_trap_score", ascending=False).head(60)


def build_source_audit(valuation: pd.DataFrame, signal_panel: pd.DataFrame, args: argparse.Namespace, top_ns: list[int]) -> pd.DataFrame:
    old_count = signal_panel[signal_panel["trade_date"].dt.year <= 2021]["industry_code"].nunique() if not signal_panel.empty else 0
    new_count = signal_panel[signal_panel["trade_date"].dt.year >= 2022]["industry_code"].nunique() if not signal_panel.empty else 0
    top_n = min(top_ns) if top_ns else 20
    broad_counts = []
    if not signal_panel.empty:
        for trade_date, group in signal_panel.groupby("trade_date", sort=True):
            eligible = apply_universe_policy(group, UNIVERSE_POLICIES[0]).dropna(subset=["forward_return_252d"])
            broad_counts.append(
                {
                    "trade_date": pd.Timestamp(trade_date),
                    "eligible_count": len(eligible),
                    "period": "post_2022" if pd.Timestamp(trade_date).year >= 2022 else "legacy",
                }
            )
    count_frame = pd.DataFrame(broad_counts)
    legacy_dates_enough = 0
    post_dates_enough = 0
    if not count_frame.empty:
        legacy_dates_enough = int(
            ((count_frame["period"] == "legacy") & (count_frame["eligible_count"] >= top_n * 2)).sum()
        )
        post_dates_enough = int(
            ((count_frame["period"] == "post_2022") & (count_frame["eligible_count"] >= top_n * 2)).sum()
        )
    return pd.DataFrame(
        [
            {
                "audit_item": "valuation_history_loaded",
                "status": "pass" if not valuation.empty else "fail",
                "evidence": f"rows={len(valuation)}; start={date_to_str(valuation['valuation_trade_date'].min())}; end={date_to_str(valuation['valuation_trade_date'].max())}; industries={valuation['industry_code'].nunique()}",
                "action": "继续作为 V2.8 桥接验证的 PIT 候选估值源。",
            },
            {
                "audit_item": "available_date_rule",
                "status": "pass",
                "evidence": f"valuation_available_date = valuation_trade_date + {args.release_lag_days} calendar day(s)",
                "action": "仍需确认公开源真实发布时间。",
            },
            {
                "audit_item": "rankic_to_portfolio_bridge_scope",
                "status": "pass",
                "evidence": "V2.8 只评估 V2.7 已有因子，不新增信号挖掘参数。",
                "action": "用于解释 RankIC 与组合收益差异。",
            },
            {
                "audit_item": "industry_universe_split",
                "status": "warning" if new_count > old_count else "pass",
                "evidence": f"legacy_unique_industries={old_count}; post_2022_unique_industries={new_count}",
                "action": "桥接结论必须同时看旧体系和 2022 后体系。",
            },
            {
                "audit_item": "same_date_portfolio_capacity",
                "status": "warning" if legacy_dates_enough == 0 and post_dates_enough > 0 else "pass",
                "evidence": f"min_top_n={top_n}; legacy_dates_enough_top_bottom={legacy_dates_enough}; post_2022_dates_enough_top_bottom={post_dates_enough}",
                "action": "若旧体系同日行业数不足以构造 Top-Bottom，旧体系只能看 RankIC 和分组，不应强行做组合桥接。",
            },
            {
                "audit_item": "source_mouth_and_license",
                "status": "pending",
                "evidence": "公开源历史估值字段尚未通过授权源抽样复核。",
                "action": "完成字段口径、发布时间和授权源交叉审计前，不能升级 validated_alpha。",
            },
        ]
    )


def build_run_summary(
    *,
    signal_panel: pd.DataFrame,
    valuation: pd.DataFrame,
    bridge_events: pd.DataFrame,
    final_summary: pd.DataFrame,
    rejection_log: pd.DataFrame,
    source_audit: pd.DataFrame,
    args: argparse.Namespace,
    top_ns: list[int],
) -> dict[str, Any]:
    status_counts = rejection_log["signal_status"].value_counts().to_dict() if not rejection_log.empty else {}
    final_verdict = "research_only_no_alpha_promotion"
    if status_counts.get("candidate_requires_source_audit", 0):
        final_verdict = "quant_candidate_but_source_audit_required"
    elif status_counts.get("conditional_observation", 0):
        final_verdict = "conditional_observation_only"
    best = final_summary.iloc[0] if not final_summary.empty else {}
    return {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_boundary": "V2.8 不新增选行业因子，只把 V2.7 的 252 日 RankIC 信息转译到低频 Top-Bottom、宽口径/剔除口径、旧体系/2022后体系和逐日净值验证。所有结论仍为 research_only。",
        "valuation_start": date_to_str(valuation["valuation_trade_date"].min()) if not valuation.empty else "",
        "valuation_end": date_to_str(valuation["valuation_trade_date"].max()) if not valuation.empty else "",
        "valuation_rows": int(len(valuation)),
        "valuation_industries": int(valuation["industry_code"].nunique()) if not valuation.empty else 0,
        "date_start": date_to_str(signal_panel["trade_date"].min()) if not signal_panel.empty else "",
        "date_end": date_to_str(signal_panel["trade_date"].max()) if not signal_panel.empty else "",
        "signal_rows": int(len(signal_panel)),
        "bridge_event_rows": int(len(bridge_events)),
        "portfolio_combo_count": int(len(final_summary)),
        "factor_count": len(FACTOR_SPECS),
        "universe_policy_count": len(UNIVERSE_POLICIES),
        "rebalance_policy_count": len(REBALANCE_POLICIES),
        "top_ns": top_ns,
        "horizon": int(args.horizon),
        "candidate_requires_source_audit_count": int(status_counts.get("candidate_requires_source_audit", 0)),
        "conditional_observation_count": int(status_counts.get("conditional_observation", 0)),
        "rejected_signal_count": int(status_counts.get("rejected_signal", 0)),
        "source_audit_pending_count": int((source_audit["status"] == "pending").sum()),
        "source_audit_warning_count": int((source_audit["status"] == "warning").sum()),
        "best_factor": best.get("factor_zh", "") if isinstance(best, pd.Series) else "",
        "best_universe_policy": best.get("universe_policy_zh", "") if isinstance(best, pd.Series) else "",
        "best_rebalance": best.get("rebalance_zh", "") if isinstance(best, pd.Series) else "",
        "best_top_n": int(best.get("top_n", 0)) if isinstance(best, pd.Series) else 0,
        "best_mean_long_short_return": float(best.get("mean_long_short_return", math.nan)) if isinstance(best, pd.Series) else math.nan,
        "best_oos_mean_long_short_return": float(best.get("oos_mean_long_short_return", math.nan)) if isinstance(best, pd.Series) else math.nan,
        "best_long_short_final_nav": float(best.get("long_short_final_nav", math.nan)) if isinstance(best, pd.Series) else math.nan,
        "final_verdict": final_verdict,
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    rankic_decay: pd.DataFrame,
    quantile_report: pd.DataFrame,
    sector_attribution: pd.DataFrame,
    period_attribution: pd.DataFrame,
    nav_metrics: pd.DataFrame,
    source_audit: pd.DataFrame,
    cross_section_coverage: pd.DataFrame,
    current_snapshot: pd.DataFrame,
) -> str:
    lines = [
        "# V2.8 RankIC 到组合收益桥接验证报告",
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
        f"- 桥接事件行数：{summary['bridge_event_rows']}",
        f"- 组合参数组合数：{summary['portfolio_combo_count']}",
        f"- 候选待源审计信号数：{summary['candidate_requires_source_audit_count']}",
        f"- 条件观察信号数：{summary['conditional_observation_count']}",
        f"- 拒绝信号数：{summary['rejected_signal_count']}",
        f"- 最终结论：{translate_final_verdict(summary['final_verdict'])}",
        "",
        "V2.8 的核心问题是：V2.7 的 252 日 RankIC 强，为什么没有转成稳健组合收益。这里不新增打分公式，只对同一批因子做低频调仓、Top-Bottom、多头相对、宽口径/剔除口径和分阶段验证。",
        "",
        "## 参数排序",
        "",
    ]
    lines.extend(render_markdown_table(top_candidates.head(18)))

    lines.extend(["", "## RankIC 衰减", ""])
    if rankic_decay.empty:
        lines.append("未生成 RankIC 衰减结果。")
    else:
        display = rankic_decay.sort_values("mean_rankic", ascending=False).head(24).copy()
        for col in ["mean_rankic", "positive_ratio"]:
            display[col] = display[col].map(fmt_pct)
        display["rankic_t_stat"] = display["rankic_t_stat"].map(lambda x: fmt_float(x, 2))
        lines.extend(
            render_markdown_table(
                display[["factor_zh", "universe_policy_zh", "horizon", "mean_rankic", "rankic_t_stat", "positive_ratio", "date_count"]].rename(
                    columns={
                        "factor_zh": "因子",
                        "universe_policy_zh": "样本口径",
                        "horizon": "周期",
                        "mean_rankic": "平均RankIC",
                        "rankic_t_stat": "T值",
                        "positive_ratio": "正IC比例",
                        "date_count": "日期数",
                    }
                )
            )
        )

    lines.extend(["", "## 分组单调性", ""])
    if quantile_report.empty:
        lines.append("未生成分组单调性结果。")
    else:
        display = quantile_report.sort_values(["factor_zh", "universe_policy_zh", "quantile"]).head(60).copy()
        for col in ["mean_relative_return", "win_rate", "top_minus_bottom", "monotonic_spearman"]:
            display[col] = display[col].map(fmt_pct if col != "monotonic_spearman" else lambda x: fmt_float(x, 2))
        lines.extend(
            render_markdown_table(
                display[
                    [
                        "factor_zh",
                        "universe_policy_zh",
                        "quantile",
                        "mean_relative_return",
                        "win_rate",
                        "top_minus_bottom",
                        "monotonic_spearman",
                        "samples",
                    ]
                ].rename(
                    columns={
                        "factor_zh": "因子",
                        "universe_policy_zh": "样本口径",
                        "quantile": "分组",
                        "mean_relative_return": "平均相对收益",
                        "win_rate": "跑赢比例",
                        "top_minus_bottom": "Top-Bottom",
                        "monotonic_spearman": "单调性",
                        "samples": "样本数",
                    }
                )
            )
        )

    lines.extend(["", "## 板块剔除归因", ""])
    if sector_attribution.empty:
        lines.append("未生成板块剔除归因。")
    else:
        display = sector_attribution.sort_values("long_short_sector_exclusion_delta", ascending=False).head(18).copy()
        for col in [
            "mean_long_short_return_broad",
            "mean_long_short_return_sector_excluded",
            "long_short_sector_exclusion_delta",
            "long_relative_sector_exclusion_delta",
        ]:
            display[col] = display[col].map(fmt_pct)
        lines.extend(
            render_markdown_table(
                display[
                    [
                        "factor_zh",
                        "rebalance_zh",
                        "top_n",
                        "mean_long_short_return_broad",
                        "mean_long_short_return_sector_excluded",
                        "long_short_sector_exclusion_delta",
                    ]
                ].rename(
                    columns={
                        "factor_zh": "因子",
                        "rebalance_zh": "调仓",
                        "top_n": "TopN",
                        "mean_long_short_return_broad": "宽口径Top-Bottom",
                        "mean_long_short_return_sector_excluded": "剔除后Top-Bottom",
                        "long_short_sector_exclusion_delta": "剔除贡献",
                    }
                )
            )
        )

    lines.extend(["", "## 分阶段归因", ""])
    if period_attribution.empty:
        lines.append("未生成分阶段归因。")
    else:
        display = period_attribution.sort_values("mean_long_short_return", ascending=False).head(24).copy()
        for col in ["mean_long_relative_return", "mean_long_short_return", "long_short_win_rate"]:
            display[col] = display[col].map(fmt_pct)
        lines.extend(
            render_markdown_table(
                display[
                    [
                        "factor_zh",
                        "universe_policy_zh",
                        "rebalance_zh",
                        "top_n",
                        "period",
                        "samples",
                        "mean_long_short_return",
                        "long_short_win_rate",
                    ]
                ].rename(
                    columns={
                        "factor_zh": "因子",
                        "universe_policy_zh": "样本口径",
                        "rebalance_zh": "调仓",
                        "top_n": "TopN",
                        "period": "阶段",
                        "samples": "样本数",
                        "mean_long_short_return": "Top-Bottom",
                        "long_short_win_rate": "胜率",
                    }
                )
            )
        )

    lines.extend(["", "## 同日截面容量审计", ""])
    if cross_section_coverage.empty:
        lines.append("未生成同日截面容量审计。")
    else:
        display = cross_section_coverage.copy()
        for col in display.columns:
            if col.startswith("share_enough_top_bottom"):
                display[col] = display[col].map(fmt_pct)
        for col in ["mean_eligible_count"]:
            if col in display.columns:
                display[col] = display[col].map(lambda x: fmt_float(x, 2))
        lines.extend(
            render_markdown_table(
                display.rename(
                    columns={
                        "universe_policy_zh": "样本口径",
                        "period": "阶段",
                        "date_count": "日期数",
                        "mean_eligible_count": "平均同日行业数",
                        "min_eligible_count": "最少同日行业数",
                        "max_eligible_count": "最多同日行业数",
                        "dates_enough_top_bottom_20": "可做Top20-Bottom20日期",
                        "dates_enough_top_bottom_30": "可做Top30-Bottom30日期",
                        "share_enough_top_bottom_20": "Top20容量占比",
                        "share_enough_top_bottom_30": "Top30容量占比",
                    }
                )
            )
        )

    lines.extend(["", "## 逐日净值摘要", ""])
    if nav_metrics.empty:
        lines.append("未生成逐日净值。")
    else:
        display = nav_metrics.sort_values("long_short_final_nav", ascending=False).head(18).copy()
        for col in [
            "annualized_long_relative_return",
            "annualized_long_short_return",
            "long_relative_max_drawdown",
            "long_short_max_drawdown",
            "daily_long_short_win_rate",
        ]:
            display[col] = display[col].map(fmt_pct)
        lines.extend(
            render_markdown_table(
                display[
                    [
                        "factor_zh",
                        "universe_policy_zh",
                        "rebalance_zh",
                        "top_n",
                        "daily_rows",
                        "long_short_final_nav",
                        "annualized_long_short_return",
                        "long_short_max_drawdown",
                        "daily_long_short_win_rate",
                    ]
                ].rename(
                    columns={
                        "factor_zh": "因子",
                        "universe_policy_zh": "样本口径",
                        "rebalance_zh": "调仓",
                        "top_n": "TopN",
                        "daily_rows": "交易日",
                        "long_short_final_nav": "Top-Bottom净值",
                        "annualized_long_short_return": "年化Top-Bottom",
                        "long_short_max_drawdown": "Top-Bottom回撤",
                        "daily_long_short_win_rate": "日胜率",
                    }
                )
            )
        )

    lines.extend(["", "## 数据与治理审计", ""])
    lines.extend(render_markdown_table(source_audit))

    lines.extend(["", "## 当前截面观察", ""])
    if current_snapshot.empty:
        lines.append("未生成当前截面观察。")
    else:
        display = current_snapshot.head(20).copy()
        for col in [
            "quality_value_no_trap_score",
            "valuation_quality_defensive_score",
            "post_2022_quality_score",
            "valuation_quality_core_score",
            "valuation_quality_value_score",
            "valuation_pit_score",
            "dividend_yield",
        ]:
            if col in display.columns:
                display[col] = display[col].map(fmt_pct)
        for col in ["pe", "pb", "momentum_trap_score"]:
            if col in display.columns:
                display[col] = display[col].map(lambda x: fmt_float(x, 2))
        lines.extend(render_markdown_table(display))

    lines.extend(
        [
            "",
            "## 输出文件说明",
            "",
            "- `top_candidates.csv`：桥接验证参数排序和状态判断，适合先看。",
            "- `run_summary.json`：机器可读运行摘要。",
            "- `debug/`：完整桥接事件、逐日净值、RankIC 衰减、分组单调性、分阶段和板块归因。",
            "",
            "研究边界：本报告只研究申万行业和行业指数，不做个股筛选，不生成交易指令。V2.8 不是新因子挖掘，而是对 V2.7 因子排序信息能否转成组合收益做反证式验证。",
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


def first_mode(frame: pd.DataFrame, column: str) -> str:
    if column not in frame.columns or frame.empty:
        return ""
    mode = frame[column].dropna().mode()
    return str(mode.iloc[0]) if not mode.empty else ""


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


def safe_series(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)


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
