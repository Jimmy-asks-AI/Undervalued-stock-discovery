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
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_latest_undervalued_oversold_bottom_backtest"
V28_SCRIPT = ROOT / "scripts" / "run_industry_rankic_portfolio_bridge_v2_8.py"
VERSION = "latest_v2_8_bottom_test"


@dataclass(frozen=True)
class StateSpec:
    state_id: str
    state_zh: str
    gate_col: str
    description: str


@dataclass(frozen=True)
class BasketSpec:
    basket_id: str
    basket_zh: str
    top_n: int | None
    min_count: int


STATE_SPECS = [
    StateSpec(
        "value_oversold_base",
        "低估超跌基础",
        "state_value_oversold_base",
        "PIT估值较便宜且价格处于超跌/回撤状态，不排除动量陷阱。",
    ),
    StateSpec(
        "value_oversold_non_trap",
        "低估超跌非陷阱",
        "state_value_oversold_non_trap",
        "基础低估超跌，同时排除持续弱势和极端动量陷阱。",
    ),
    StateSpec(
        "value_oversold_quality",
        "低估超跌质量确认",
        "state_value_oversold_quality",
        "低估超跌非陷阱，同时要求估值质量、价格质量和质量价值非陷阱分数确认。",
    ),
    StateSpec(
        "value_oversold_quality_sector_excluded",
        "低估超跌质量确认-剔除专项板块",
        "state_value_oversold_quality_sector_excluded",
        "质量确认后再剔除银行、非银金融、地产、建筑等需要专项基本面的板块。",
    ),
    StateSpec(
        "deep_value_deep_oversold",
        "深度低估深度超跌",
        "state_deep_value_deep_oversold",
        "更严格的低估和超跌触发，用于检验是否只有极端状态才有反弹。",
    ),
]

BASKET_SPECS = [
    BasketSpec("all_triggered", "全部触发行业", None, 3),
    BasketSpec("top10", "触发行业Top10", 10, 10),
    BasketSpec("top20", "触发行业Top20", 20, 20),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest bottom-fishing returns after latest undervalued-oversold states.")
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="Historical price/return feature panel.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current industry name and parent mapping.")
    parser.add_argument("--valuation", default=str(DEFAULT_VALUATION), help="SWS daily industry valuation history.")
    parser.add_argument("--history-dir", default=str(DEFAULT_HISTORY_DIR), help="Cached industry index histories for daily NAV.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--horizons", default="60,120,252", help="Forward holding horizons.")
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

    horizons = parse_int_list(args.horizons)
    v28 = load_v28_module()
    v27 = v28.load_v27_module()
    v26 = v27.load_v26_module()
    v23 = v26.load_v23_module()
    signal_panel, valuation = v28.build_signal_panel(v27, v26, v23, args)
    signal_panel = attach_bottom_states(signal_panel)

    event_backtest = compute_event_backtest(signal_panel, horizons, args.cost_bps)
    nonoverlap = compute_nonoverlap_backtest(event_backtest)
    walk_forward = compute_walk_forward_oos(event_backtest, args.oos_split_ratio)
    bootstrap = compute_bootstrap_confidence(event_backtest, args.bootstrap_rounds)
    close_matrix = v23.load_close_matrix(Path(args.history_dir), signal_panel["industry_code"].dropna().unique().tolist())
    daily_nav = compute_daily_bottom_nav(signal_panel, close_matrix, args.cost_bps)
    nav_metrics = compute_nav_metrics(daily_nav)
    parameter_summary = summarize_parameters(event_backtest, nonoverlap, walk_forward, bootstrap, nav_metrics)
    rejection_log = build_signal_rejection_log(parameter_summary)
    top_candidates = build_top_candidates(parameter_summary, rejection_log)
    trigger_coverage = build_trigger_coverage(signal_panel)
    state_audit = build_state_definition_audit()
    current_snapshot = build_current_state_snapshot(signal_panel)
    source_audit = build_source_audit(valuation, signal_panel, args)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    signal_panel.to_csv(debug_dir / "bottom_state_signal_panel.csv", index=False, encoding="utf-8-sig")
    event_backtest.to_csv(debug_dir / "bottom_event_backtest.csv", index=False, encoding="utf-8-sig")
    nonoverlap.to_csv(debug_dir / "nonoverlap_backtest.csv", index=False, encoding="utf-8-sig")
    walk_forward.to_csv(debug_dir / "walk_forward_oos.csv", index=False, encoding="utf-8-sig")
    bootstrap.to_csv(debug_dir / "bootstrap_confidence.csv", index=False, encoding="utf-8-sig")
    daily_nav.to_csv(debug_dir / "daily_bottom_nav.csv", index=False, encoding="utf-8-sig")
    nav_metrics.to_csv(debug_dir / "bottom_nav_metrics.csv", index=False, encoding="utf-8-sig")
    parameter_summary.to_csv(debug_dir / "parameter_summary.csv", index=False, encoding="utf-8-sig")
    rejection_log.to_csv(debug_dir / "signal_rejection_log.csv", index=False, encoding="utf-8-sig")
    trigger_coverage.to_csv(debug_dir / "state_trigger_coverage.csv", index=False, encoding="utf-8-sig")
    state_audit.to_csv(debug_dir / "state_definition_audit.csv", index=False, encoding="utf-8-sig")
    source_audit.to_csv(debug_dir / "source_audit.csv", index=False, encoding="utf-8-sig")
    current_snapshot.to_csv(debug_dir / "current_state_snapshot.csv", index=False, encoding="utf-8-sig")

    summary = build_run_summary(
        signal_panel=signal_panel,
        valuation=valuation,
        event_backtest=event_backtest,
        parameter_summary=parameter_summary,
        rejection_log=rejection_log,
        trigger_coverage=trigger_coverage,
        source_audit=source_audit,
        horizons=horizons,
    )
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            trigger_coverage=trigger_coverage,
            nav_metrics=nav_metrics,
            state_audit=state_audit,
            source_audit=source_audit,
            current_snapshot=current_snapshot,
        ),
        encoding="utf-8",
    )

    print("最新框架低估超跌抄底回测完成")
    print(f"信号面板行数={summary['signal_rows']}")
    print(f"事件行数={summary['event_rows']}")
    print(f"候选待源审计信号数={summary['candidate_requires_source_audit_count']}")
    print(f"条件观察信号数={summary['conditional_observation_count']}")
    print(f"拒绝信号数={summary['rejected_signal_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v28_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_rankic_portfolio_bridge_v2_8", V28_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load V2.8 module from {V28_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def attach_bottom_states(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["low_value_flag"] = (
        result["valuation_history_gate"].fillna(False)
        & (result["valuation_pit_score"] >= 0.55)
        & (result["historical_valuation_score"] >= 0.45)
    )
    result["oversold_flag"] = (
        (result["stabilized_oversold_signal"] >= 0.55)
        | (result["price_only_oversold_signal"] >= 0.65)
        | (result["drawdown_252d"] <= -0.18)
    )
    result["non_trap_flag"] = result["not_momentum_trap"].fillna(False) & (result["momentum_trap_score"].fillna(9) <= 1)
    result["quality_confirm_flag"] = (
        (result["quality_value_no_trap_score"] >= 0.55)
        & (result["valuation_quality_core_score"] >= 0.50)
        & (result["price_quality_composite"] >= 0.40)
    )
    result["deep_value_flag"] = (
        (result["valuation_pit_score"] >= 0.65)
        & (result["historical_valuation_score"] >= 0.55)
    )
    result["deep_oversold_flag"] = (
        (result["stabilized_oversold_signal"] >= 0.65)
        | (result["price_only_oversold_signal"] >= 0.75)
        | (result["drawdown_252d"] <= -0.25)
    )
    result["state_value_oversold_base"] = result["low_value_flag"] & result["oversold_flag"]
    result["state_value_oversold_non_trap"] = result["state_value_oversold_base"] & result["non_trap_flag"]
    result["state_value_oversold_quality"] = result["state_value_oversold_non_trap"] & result["quality_confirm_flag"]
    result["state_value_oversold_quality_sector_excluded"] = (
        result["state_value_oversold_quality"] & ~result["sector_quality_exclusion_flag"].fillna(False)
    )
    result["state_deep_value_deep_oversold"] = result["deep_value_flag"] & result["deep_oversold_flag"] & result["non_trap_flag"]
    result["latest_bottom_score"] = (
        0.30 * result["valuation_pit_score"].fillna(0.0)
        + 0.20 * result["historical_valuation_score"].fillna(0.0)
        + 0.20 * result["stabilized_oversold_signal"].fillna(0.0)
        + 0.20 * result["quality_value_no_trap_score"].fillna(0.0)
        + 0.10 * result["recovery_quality_score"].fillna(0.0)
        - 0.08 * result["momentum_trap_score"].clip(upper=3).fillna(0.0)
    )
    result["latest_bottom_score"] = result.groupby("trade_date")["latest_bottom_score"].rank(pct=True, method="average")
    return result.sort_values(["trade_date", "industry_code"])


def compute_event_backtest(signal_panel: pd.DataFrame, horizons: list[int], cost_bps: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty:
        return pd.DataFrame(rows)
    grouped = list(signal_panel.groupby("trade_date", sort=True))
    for state in STATE_SPECS:
        for basket in BASKET_SPECS:
            for horizon in horizons:
                label = f"forward_return_{horizon}d"
                benchmark_label = f"benchmark_forward_return_{horizon}d"
                if label not in signal_panel.columns:
                    continue
                previous: set[str] = set()
                for trade_date, group in grouped:
                    triggered = group[group[state.gate_col].fillna(False)].dropna(subset=[label, "latest_bottom_score"]).copy()
                    if len(triggered) < basket.min_count:
                        continue
                    if basket.top_n is None:
                        selected = triggered.sort_values("latest_bottom_score", ascending=False).copy()
                    else:
                        selected = triggered.sort_values("latest_bottom_score", ascending=False).head(basket.top_n).copy()
                    if len(selected) < basket.min_count:
                        continue
                    current = set(selected["industry_code"].astype(str).tolist())
                    turnover = len(current.symmetric_difference(previous)) / max(len(current), 1) if previous else 1.0
                    cost = turnover * cost_bps / 10000.0
                    gross = float(selected[label].mean())
                    benchmark = float(group[benchmark_label].dropna().mean()) if benchmark_label in group.columns else float(group[label].mean())
                    rows.append(
                        {
                            "trade_date": date_to_str(trade_date),
                            "state_id": state.state_id,
                            "state_zh": state.state_zh,
                            "basket_id": basket.basket_id,
                            "basket_zh": basket.basket_zh,
                            "horizon": int(horizon),
                            "triggered_count": int(len(triggered)),
                            "selected_count": int(len(selected)),
                            "gross_forward_return": gross,
                            "turnover": turnover,
                            "cost_bps": cost_bps,
                            "net_forward_return": gross - cost,
                            "benchmark_forward_return": benchmark,
                            "benchmark_relative_return": gross - cost - benchmark,
                            "market_stress_score": mean_col(selected, "market_stress_score"),
                            "market_regime": first_mode(selected, "market_regime"),
                            "volatility_regime": first_mode(selected, "volatility_regime"),
                            "period": "2022-2026新行业体系" if pd.Timestamp(trade_date).year >= 2022 else "2015-2021旧行业体系",
                            "avg_bottom_score": mean_col(selected, "latest_bottom_score"),
                            "avg_valuation_pit_score": mean_col(selected, "valuation_pit_score"),
                            "avg_historical_valuation_score": mean_col(selected, "historical_valuation_score"),
                            "avg_stabilized_oversold_signal": mean_col(selected, "stabilized_oversold_signal"),
                            "avg_quality_value_no_trap_score": mean_col(selected, "quality_value_no_trap_score"),
                            "avg_momentum_trap_score": mean_col(selected, "momentum_trap_score"),
                            "avg_return_60d": mean_col(selected, "return_60d"),
                            "avg_return_120d": mean_col(selected, "return_120d"),
                            "avg_return_252d": mean_col(selected, "return_252d"),
                            "avg_drawdown_252d": mean_col(selected, "drawdown_252d"),
                            "avg_pe": mean_col(selected, "pe"),
                            "avg_pb": mean_col(selected, "pb"),
                            "avg_dividend_yield": mean_col(selected, "dividend_yield"),
                            "selected_industries": "|".join(selected["industry_name"].fillna(selected["industry_code"]).astype(str).tolist()),
                        }
                    )
                    previous = current
    return pd.DataFrame(rows)


def compute_nonoverlap_backtest(events: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if events.empty:
        return pd.DataFrame(rows)
    for keys, group in events.groupby(["state_id", "state_zh", "basket_id", "basket_zh", "horizon"], sort=True):
        ordered = group.sort_values("trade_date").reset_index(drop=True)
        stride = max(1, int(math.ceil(int(keys[4]) / 20)))
        sample = ordered.iloc[::stride].copy()
        for row in sample.to_dict("records"):
            row["nonoverlap_stride"] = stride
            rows.append(row)
    return pd.DataFrame(rows)


def compute_walk_forward_oos(events: pd.DataFrame, split_ratio: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if events.empty:
        return pd.DataFrame(rows)
    dates = sorted(events["trade_date"].dropna().unique().tolist())
    split_index = min(max(1, int(len(dates) * min(max(split_ratio, 0.10), 0.90))), len(dates) - 1)
    split_date = dates[split_index - 1]
    for sample, sample_zh, sample_dates in [
        ("in_sample", "样本内", set(dates[:split_index])),
        ("out_of_sample", "样本外", set(dates[split_index:])),
    ]:
        frame = events[events["trade_date"].isin(sample_dates)]
        for keys, group in frame.groupby(["state_id", "state_zh", "basket_id", "basket_zh", "horizon"], sort=True):
            row = metric_row(group, *keys)
            row["sample"] = sample
            row["sample_zh"] = sample_zh
            row["split_date"] = split_date
            rows.append(row)
    return pd.DataFrame(rows)


def compute_bootstrap_confidence(events: pd.DataFrame, rounds: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if events.empty:
        return pd.DataFrame(rows)
    rng = np.random.default_rng(20260614)
    frame = events.copy()
    frame["year_block"] = pd.to_datetime(frame["trade_date"]).dt.year.astype(str)
    for keys, group in frame.groupby(["state_id", "basket_id", "horizon"], sort=True):
        blocks = sorted(group["year_block"].dropna().unique().tolist())
        samples: list[float] = []
        if len(blocks) >= 2:
            block_map = {block: group[group["year_block"] == block]["benchmark_relative_return"].dropna().to_numpy() for block in blocks}
            for _ in range(rounds):
                sampled_blocks = rng.choice(blocks, size=len(blocks), replace=True)
                values = np.concatenate([block_map[block] for block in sampled_blocks if len(block_map[block])])
                if len(values):
                    samples.append(float(np.mean(values)))
        rows.append(
            {
                "state_id": keys[0],
                "basket_id": keys[1],
                "horizon": int(keys[2]),
                "block_count": int(len(blocks)),
                "ci_5": float(np.quantile(samples, 0.05)) if samples else math.nan,
                "ci_50": float(np.quantile(samples, 0.50)) if samples else math.nan,
                "ci_95": float(np.quantile(samples, 0.95)) if samples else math.nan,
                "probability_positive": float(np.mean(np.array(samples) > 0)) if samples else math.nan,
            }
        )
    return pd.DataFrame(rows)


def compute_daily_bottom_nav(signal_panel: pd.DataFrame, close_matrix: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty or close_matrix.empty:
        return pd.DataFrame(rows)
    returns = close_matrix.pct_change(fill_method=None)
    close_dates = close_matrix.index.sort_values()
    feature_dates = sorted(signal_panel["trade_date"].dropna().unique().tolist())
    actual_rebalances: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    seen_dates: set[pd.Timestamp] = set()
    for feature_date in feature_dates:
        idx = close_dates.searchsorted(pd.Timestamp(feature_date))
        if idx >= len(close_dates):
            continue
        actual_date = pd.Timestamp(close_dates[idx])
        if actual_date in seen_dates:
            continue
        seen_dates.add(actual_date)
        actual_rebalances.append((pd.Timestamp(feature_date), actual_date))

    for state in STATE_SPECS:
        for basket in BASKET_SPECS:
            nav = 1.0
            benchmark_nav = 1.0
            previous: set[str] = set()
            for idx, (feature_date, actual_date) in enumerate(actual_rebalances):
                group = signal_panel[signal_panel["trade_date"] == feature_date].copy()
                triggered = group[group[state.gate_col].fillna(False)].dropna(subset=["latest_bottom_score"]).copy()
                if len(triggered) < basket.min_count:
                    continue
                if basket.top_n is None:
                    selected = triggered.sort_values("latest_bottom_score", ascending=False).copy()
                else:
                    selected = triggered.sort_values("latest_bottom_score", ascending=False).head(basket.top_n).copy()
                current = set(selected["industry_code"].astype(str).str.zfill(6).tolist())
                current = {code for code in current if code in returns.columns}
                if len(current) < max(1, basket.min_count // 2):
                    continue
                next_date = actual_rebalances[idx + 1][1] if idx + 1 < len(actual_rebalances) else close_dates[-1]
                period_dates = returns.index[(returns.index > actual_date) & (returns.index <= next_date)]
                if len(period_dates) == 0:
                    continue
                turnover = len(current.symmetric_difference(previous)) / max(len(current), 1) if previous else 1.0
                cost_return = turnover * cost_bps / 10000.0
                first_period_day = True
                for day in period_dates:
                    selected_returns = returns.loc[day, sorted(current)].dropna()
                    benchmark_returns = returns.loc[day].dropna()
                    if selected_returns.empty or benchmark_returns.empty:
                        continue
                    daily_return = float(selected_returns.mean())
                    benchmark_return = float(benchmark_returns.mean())
                    net_daily_return = daily_return - cost_return if first_period_day else daily_return
                    first_period_day = False
                    nav *= 1.0 + net_daily_return
                    benchmark_nav *= 1.0 + benchmark_return
                    rows.append(
                        {
                            "trade_date": date_to_str(day),
                            "feature_date": date_to_str(feature_date),
                            "state_id": state.state_id,
                            "state_zh": state.state_zh,
                            "basket_id": basket.basket_id,
                            "basket_zh": basket.basket_zh,
                            "selected_count": int(len(current)),
                            "daily_return": daily_return,
                            "net_daily_return": net_daily_return,
                            "benchmark_daily_return": benchmark_return,
                            "strategy_nav": nav,
                            "benchmark_nav": benchmark_nav,
                            "relative_nav": nav / benchmark_nav if benchmark_nav else math.nan,
                            "turnover": turnover,
                        }
                    )
                previous = current
    return pd.DataFrame(rows)


def compute_nav_metrics(daily_nav: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if daily_nav.empty:
        return pd.DataFrame(rows)
    for keys, group in daily_nav.groupby(["state_id", "state_zh", "basket_id", "basket_zh"], sort=True):
        ordered = group.sort_values("trade_date").copy()
        days = max(1, len(ordered))
        years = days / 252.0
        final_nav = float(ordered["strategy_nav"].iloc[-1])
        benchmark_nav = float(ordered["benchmark_nav"].iloc[-1])
        relative_nav = float(ordered["relative_nav"].iloc[-1])
        drawdown = ordered["strategy_nav"] / ordered["strategy_nav"].cummax() - 1.0
        relative_drawdown = ordered["relative_nav"] / ordered["relative_nav"].cummax() - 1.0
        rows.append(
            {
                "state_id": keys[0],
                "state_zh": keys[1],
                "basket_id": keys[2],
                "basket_zh": keys[3],
                "daily_rows": int(days),
                "start_date": ordered["trade_date"].iloc[0],
                "end_date": ordered["trade_date"].iloc[-1],
                "final_nav": final_nav,
                "benchmark_final_nav": benchmark_nav,
                "relative_final_nav": relative_nav,
                "annualized_return": final_nav ** (1 / years) - 1 if final_nav > 0 else math.nan,
                "benchmark_annualized_return": benchmark_nav ** (1 / years) - 1 if benchmark_nav > 0 else math.nan,
                "annualized_relative_return": relative_nav ** (1 / years) - 1 if relative_nav > 0 else math.nan,
                "max_drawdown": float(drawdown.min()),
                "relative_max_drawdown": float(relative_drawdown.min()),
                "daily_relative_win_rate": float((ordered["net_daily_return"] > ordered["benchmark_daily_return"]).mean()),
                "mean_selected_count": float(ordered["selected_count"].mean()),
            }
        )
    return pd.DataFrame(rows)


def summarize_parameters(
    events: pd.DataFrame,
    nonoverlap: pd.DataFrame,
    walk_forward: pd.DataFrame,
    bootstrap: pd.DataFrame,
    nav_metrics: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if events.empty:
        return pd.DataFrame(rows)
    for keys, group in events.groupby(["state_id", "state_zh", "basket_id", "basket_zh", "horizon"], sort=True):
        row = metric_row(group, *keys)
        state_id, _, basket_id, _, horizon = keys
        non = nonoverlap[
            (nonoverlap["state_id"] == state_id)
            & (nonoverlap["basket_id"] == basket_id)
            & (nonoverlap["horizon"] == horizon)
        ]
        row["nonoverlap_samples"] = int(len(non))
        row["nonoverlap_mean_relative_return"] = float(non["benchmark_relative_return"].mean()) if not non.empty else math.nan
        row["nonoverlap_win_rate"] = float((non["benchmark_relative_return"] > 0).mean()) if not non.empty else math.nan
        oos = walk_forward[
            (walk_forward["state_id"] == state_id)
            & (walk_forward["basket_id"] == basket_id)
            & (walk_forward["horizon"] == horizon)
            & (walk_forward["sample"] == "out_of_sample")
        ]
        row["oos_samples"] = int(oos.iloc[0]["samples"]) if not oos.empty else 0
        row["oos_mean_relative_return"] = float(oos.iloc[0]["mean_relative_return"]) if not oos.empty else math.nan
        row["oos_mean_net_return"] = float(oos.iloc[0]["mean_net_return"]) if not oos.empty else math.nan
        boot = bootstrap[(bootstrap["state_id"] == state_id) & (bootstrap["basket_id"] == basket_id) & (bootstrap["horizon"] == horizon)]
        row["bootstrap_ci_5"] = float(boot.iloc[0]["ci_5"]) if not boot.empty else math.nan
        row["bootstrap_probability_positive"] = float(boot.iloc[0]["probability_positive"]) if not boot.empty else math.nan
        nav = nav_metrics[(nav_metrics["state_id"] == state_id) & (nav_metrics["basket_id"] == basket_id)]
        if not nav.empty:
            nav_row = nav.iloc[0]
            for col in [
                "daily_rows",
                "relative_final_nav",
                "annualized_relative_return",
                "relative_max_drawdown",
                "daily_relative_win_rate",
                "mean_selected_count",
            ]:
                row[col] = float(nav_row[col])
        else:
            row["daily_rows"] = 0
            row["relative_final_nav"] = math.nan
            row["annualized_relative_return"] = math.nan
            row["relative_max_drawdown"] = math.nan
            row["daily_relative_win_rate"] = math.nan
            row["mean_selected_count"] = math.nan
        row["sample_strength"] = min(
            safe_number(row["samples"]) / 60.0,
            safe_number(row["nonoverlap_samples"]) / 12.0,
            safe_number(row["oos_samples"]) / 12.0 if safe_number(row["oos_samples"]) else 0.0,
            safe_number(row["daily_rows"]) / 252.0 if safe_number(row["daily_rows"]) else 0.0,
            1.0,
        )
        row["robust_score"] = (
            safe_number(row["mean_relative_return"])
            + safe_number(row["oos_mean_relative_return"])
            + safe_number(row["nonoverlap_mean_relative_return"])
            + 0.25 * safe_number(row["annualized_relative_return"])
            + 0.01 * safe_number(row["benchmark_win_rate"])
            + 0.01 * safe_number(row["bootstrap_probability_positive"])
        ) * (0.2 + 0.8 * row["sample_strength"])
        rows.append(row)
    return pd.DataFrame(rows).sort_values("robust_score", ascending=False)


def metric_row(group: pd.DataFrame, state_id: str, state_zh: str, basket_id: str, basket_zh: str, horizon: int) -> dict[str, Any]:
    return {
        "state_id": state_id,
        "state_zh": state_zh,
        "basket_id": basket_id,
        "basket_zh": basket_zh,
        "horizon": int(horizon),
        "samples": int(len(group)),
        "mean_net_return": float(group["net_forward_return"].mean()),
        "median_net_return": float(group["net_forward_return"].median()),
        "mean_benchmark_return": float(group["benchmark_forward_return"].mean()),
        "mean_relative_return": float(group["benchmark_relative_return"].mean()),
        "win_rate": float((group["net_forward_return"] > 0).mean()),
        "benchmark_win_rate": float((group["benchmark_relative_return"] > 0).mean()),
        "mean_turnover": float(group["turnover"].mean()),
        "mean_triggered_count": float(group["triggered_count"].mean()),
        "mean_selected_count_event": float(group["selected_count"].mean()),
        "avg_bottom_score": float(group["avg_bottom_score"].mean()),
        "avg_valuation_pit_score": float(group["avg_valuation_pit_score"].mean()),
        "avg_stabilized_oversold_signal": float(group["avg_stabilized_oversold_signal"].mean()),
        "avg_quality_value_no_trap_score": float(group["avg_quality_value_no_trap_score"].mean()),
        "avg_momentum_trap_score": float(group["avg_momentum_trap_score"].mean()),
        "avg_return_120d": float(group["avg_return_120d"].mean()),
        "avg_return_252d": float(group["avg_return_252d"].mean()),
        "avg_drawdown_252d": float(group["avg_drawdown_252d"].mean()),
        "avg_pe": float(group["avg_pe"].mean()),
        "avg_pb": float(group["avg_pb"].mean()),
        "avg_dividend_yield": float(group["avg_dividend_yield"].mean()),
    }


def build_signal_rejection_log(summary: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if summary.empty:
        return pd.DataFrame(rows)
    for row in summary.to_dict("records"):
        checks = {
            "full_relative_positive": row["mean_relative_return"] > 0,
            "oos_relative_positive": row["oos_mean_relative_return"] > 0,
            "nonoverlap_positive": row["nonoverlap_mean_relative_return"] > 0,
            "benchmark_win_rate": row["benchmark_win_rate"] > 0.50,
            "nonoverlap_win_rate": row["nonoverlap_win_rate"] > 0.50,
            "bootstrap_ci": row["bootstrap_ci_5"] > 0,
            "bootstrap_prob": row["bootstrap_probability_positive"] > 0.60,
            "nav_positive": row["relative_final_nav"] > 1.0,
            "sample_enough": row["sample_strength"] >= 0.75,
        }
        reasons = []
        reason_map = {
            "full_relative_positive": "全样本相对收益不为正",
            "oos_relative_positive": "样本外相对收益不为正",
            "nonoverlap_positive": "非重叠相对收益不为正",
            "benchmark_win_rate": "全样本跑赢比例不足50%",
            "nonoverlap_win_rate": "非重叠跑赢比例不足50%",
            "bootstrap_ci": "bootstrap置信下沿不为正",
            "bootstrap_prob": "bootstrap正收益概率不足60%",
            "nav_positive": "逐日相对净值不大于1",
            "sample_enough": "样本强度不足",
        }
        for key, passed in checks.items():
            if not passed:
                reasons.append(reason_map[key])
        if all(checks.values()):
            status = "candidate_requires_source_audit"
            reasons.append("量化门槛通过，但公开估值源仍需口径和发布时间审计")
        elif any([checks["full_relative_positive"], checks["oos_relative_positive"], checks["nonoverlap_positive"], checks["nav_positive"]]):
            status = "conditional_observation"
        else:
            status = "rejected_signal"
        rows.append(
            {
                "state_id": row["state_id"],
                "state_zh": row["state_zh"],
                "basket_id": row["basket_id"],
                "basket_zh": row["basket_zh"],
                "horizon": int(row["horizon"]),
                "signal_status": status,
                "mean_relative_return": row["mean_relative_return"],
                "oos_mean_relative_return": row["oos_mean_relative_return"],
                "nonoverlap_mean_relative_return": row["nonoverlap_mean_relative_return"],
                "relative_final_nav": row["relative_final_nav"],
                "sample_strength": row["sample_strength"],
                "rejection_reasons": "；".join(reasons),
            }
        )
    return pd.DataFrame(rows)


def build_top_candidates(summary: pd.DataFrame, rejection_log: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    merged = summary.merge(
        rejection_log[["state_id", "basket_id", "horizon", "signal_status", "rejection_reasons"]],
        on=["state_id", "basket_id", "horizon"],
        how="left",
    )
    rows: list[dict[str, Any]] = []
    for row in merged.sort_values("robust_score", ascending=False).head(30).to_dict("records"):
        rows.append(
            {
                "状态": row["state_zh"],
                "组合": row["basket_zh"],
                "持有期": int(row["horizon"]),
                "结论": translate_status(str(row.get("signal_status", ""))),
                "绝对收益": fmt_pct(row["mean_net_return"]),
                "基准收益": fmt_pct(row["mean_benchmark_return"]),
                "相对收益": fmt_pct(row["mean_relative_return"]),
                "样本外相对收益": fmt_pct(row["oos_mean_relative_return"]),
                "非重叠相对收益": fmt_pct(row["nonoverlap_mean_relative_return"]),
                "逐日相对净值": fmt_float(row["relative_final_nav"], 3),
                "年化相对收益": fmt_pct(row["annualized_relative_return"]),
                "Bootstrap下沿": fmt_pct(row["bootstrap_ci_5"]),
                "Bootstrap为正概率": fmt_pct(row["bootstrap_probability_positive"]),
                "全样本跑赢比例": fmt_pct(row["benchmark_win_rate"]),
                "样本强度": fmt_pct(row["sample_strength"]),
                "平均触发行业数": fmt_float(row["mean_triggered_count"], 1),
                "平均120日收益": fmt_pct(row["avg_return_120d"]),
                "平均252日收益": fmt_pct(row["avg_return_252d"]),
                "平均回撤": fmt_pct(row["avg_drawdown_252d"]),
                "平均估值分": fmt_pct(row["avg_valuation_pit_score"]),
                "平均质量非陷阱分": fmt_pct(row["avg_quality_value_no_trap_score"]),
                "平均PE": fmt_float(row["avg_pe"], 2),
                "平均PB": fmt_float(row["avg_pb"], 2),
                "样本数": int(row["samples"]),
                "拒绝或保留原因": row["rejection_reasons"],
            }
        )
    return pd.DataFrame(rows)


def build_trigger_coverage(signal_panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty:
        return pd.DataFrame(rows)
    for state in STATE_SPECS:
        for period, group in signal_panel.assign(
            period=np.where(signal_panel["trade_date"].dt.year >= 2022, "2022-2026新行业体系", "2015-2021旧行业体系")
        ).groupby("period", sort=True):
            dated = (
                group.groupby("trade_date")
                .agg(triggered_count=(state.gate_col, lambda s: int(s.fillna(False).sum())), eligible_count=("industry_code", "nunique"))
                .reset_index()
            )
            rows.append(
                {
                    "state_id": state.state_id,
                    "state_zh": state.state_zh,
                    "period": period,
                    "date_count": int(len(dated)),
                    "triggered_date_count": int((dated["triggered_count"] > 0).sum()),
                    "avg_triggered_count": float(dated["triggered_count"].mean()),
                    "median_triggered_count": float(dated["triggered_count"].median()),
                    "max_triggered_count": int(dated["triggered_count"].max()),
                    "triggered_date_ratio": float((dated["triggered_count"] > 0).mean()),
                }
            )
    return pd.DataFrame(rows)


def build_state_definition_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "item": "low_value_flag",
                "definition": "valuation_history_gate 且 valuation_pit_score >= 0.55 且 historical_valuation_score >= 0.45",
                "purpose": "确认低估来自 PIT 历史估值，不使用当前快照回填。",
            },
            {
                "item": "oversold_flag",
                "definition": "stabilized_oversold_signal >= 0.55 或 price_only_oversold_signal >= 0.65 或 drawdown_252d <= -18%",
                "purpose": "确认处于超跌/回撤状态。",
            },
            {
                "item": "non_trap_flag",
                "definition": "not_momentum_trap 且 momentum_trap_score <= 1",
                "purpose": "排除 V2.x 已反复证伪的动量陷阱。",
            },
            {
                "item": "quality_confirm_flag",
                "definition": "quality_value_no_trap_score >= 0.55 且 valuation_quality_core_score >= 0.50 且 price_quality_composite >= 0.40",
                "purpose": "使用 V2.7/V2.8 的估值质量和非陷阱信息确认。",
            },
            {
                "item": "latest_bottom_score",
                "definition": "0.30*valuation_pit + 0.20*historical_valuation + 0.20*stabilized_oversold + 0.20*quality_value_no_trap + 0.10*recovery - 0.08*momentum_trap",
                "purpose": "只用于触发行业内部排序，不作为已验证 alpha。",
            },
        ]
    )


def build_current_state_snapshot(signal_panel: pd.DataFrame) -> pd.DataFrame:
    if signal_panel.empty:
        return pd.DataFrame()
    latest = signal_panel["trade_date"].max()
    frame = signal_panel[signal_panel["trade_date"] == latest].copy()
    cols = [
        "trade_date",
        "industry_code",
        "industry_name",
        "parent_industry",
        "latest_bottom_score",
        "state_value_oversold_base",
        "state_value_oversold_non_trap",
        "state_value_oversold_quality",
        "state_value_oversold_quality_sector_excluded",
        "state_deep_value_deep_oversold",
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
    return frame[[col for col in cols if col in frame.columns]].sort_values("latest_bottom_score", ascending=False).head(80)


def build_source_audit(valuation: pd.DataFrame, signal_panel: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "latest_version_boundary",
                "status": "pass",
                "evidence": "复用 V2.8/V2.7 信号面板和 V2.6 PIT 历史估值源。",
                "action": "本回测是最新框架下的专项低估超跌抄底验证。",
            },
            {
                "audit_item": "valuation_history_loaded",
                "status": "pass" if not valuation.empty else "fail",
                "evidence": f"rows={len(valuation)}; start={date_to_str(valuation['valuation_trade_date'].min())}; end={date_to_str(valuation['valuation_trade_date'].max())}; industries={valuation['industry_code'].nunique()}",
                "action": "低估状态只使用 PIT 候选估值源。",
            },
            {
                "audit_item": "available_date_rule",
                "status": "pass",
                "evidence": f"valuation_available_date = valuation_trade_date + {args.release_lag_days} calendar day(s)",
                "action": "仍需确认公开源真实发布时间。",
            },
            {
                "audit_item": "no_future_label_in_state",
                "status": "pass",
                "evidence": "触发状态只使用 valuation_pit_score、historical_valuation_score、oversold signals、quality scores、momentum trap flags。",
                "action": "forward_return_* 只作为收益标签。",
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
    event_backtest: pd.DataFrame,
    parameter_summary: pd.DataFrame,
    rejection_log: pd.DataFrame,
    trigger_coverage: pd.DataFrame,
    source_audit: pd.DataFrame,
    horizons: list[int],
) -> dict[str, Any]:
    status_counts = rejection_log["signal_status"].value_counts().to_dict() if not rejection_log.empty else {}
    final_verdict = "research_only_no_alpha_promotion"
    if status_counts.get("candidate_requires_source_audit", 0):
        final_verdict = "quant_candidate_but_source_audit_required"
    elif status_counts.get("conditional_observation", 0):
        final_verdict = "conditional_observation_only"
    best = parameter_summary.iloc[0] if not parameter_summary.empty else {}
    return {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_boundary": "使用最新 V2.8/V2.7 信号面板，专项检验系统判断低估超跌后抄底的收益。结果仍为 research_only，不生成交易指令。",
        "valuation_start": date_to_str(valuation["valuation_trade_date"].min()) if not valuation.empty else "",
        "valuation_end": date_to_str(valuation["valuation_trade_date"].max()) if not valuation.empty else "",
        "valuation_rows": int(len(valuation)),
        "signal_rows": int(len(signal_panel)),
        "date_start": date_to_str(signal_panel["trade_date"].min()) if not signal_panel.empty else "",
        "date_end": date_to_str(signal_panel["trade_date"].max()) if not signal_panel.empty else "",
        "event_rows": int(len(event_backtest)),
        "parameter_count": int(len(parameter_summary)),
        "horizons": horizons,
        "candidate_requires_source_audit_count": int(status_counts.get("candidate_requires_source_audit", 0)),
        "conditional_observation_count": int(status_counts.get("conditional_observation", 0)),
        "rejected_signal_count": int(status_counts.get("rejected_signal", 0)),
        "source_audit_pending_count": int((source_audit["status"] == "pending").sum()),
        "triggered_state_count": int(trigger_coverage["triggered_date_count"].gt(0).sum()) if not trigger_coverage.empty else 0,
        "best_state": best.get("state_zh", "") if isinstance(best, pd.Series) else "",
        "best_basket": best.get("basket_zh", "") if isinstance(best, pd.Series) else "",
        "best_horizon": int(best.get("horizon", 0)) if isinstance(best, pd.Series) else 0,
        "best_mean_net_return": float(best.get("mean_net_return", math.nan)) if isinstance(best, pd.Series) else math.nan,
        "best_mean_benchmark_return": float(best.get("mean_benchmark_return", math.nan)) if isinstance(best, pd.Series) else math.nan,
        "best_mean_relative_return": float(best.get("mean_relative_return", math.nan)) if isinstance(best, pd.Series) else math.nan,
        "best_oos_mean_relative_return": float(best.get("oos_mean_relative_return", math.nan)) if isinstance(best, pd.Series) else math.nan,
        "best_relative_final_nav": float(best.get("relative_final_nav", math.nan)) if isinstance(best, pd.Series) else math.nan,
        "final_verdict": final_verdict,
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    trigger_coverage: pd.DataFrame,
    nav_metrics: pd.DataFrame,
    state_audit: pd.DataFrame,
    source_audit: pd.DataFrame,
    current_snapshot: pd.DataFrame,
) -> str:
    lines = [
        "# 最新框架低估超跌抄底收益回测报告",
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
        f"- 参数组合数：{summary['parameter_count']}",
        f"- 候选待源审计信号数：{summary['candidate_requires_source_audit_count']}",
        f"- 条件观察信号数：{summary['conditional_observation_count']}",
        f"- 拒绝信号数：{summary['rejected_signal_count']}",
        f"- 最终结论：{translate_final_verdict(summary['final_verdict'])}",
        "",
        "这里的“低估超跌”不是事后找最低点，而是用当时可得的 PIT 估值、价格超跌、质量确认和动量陷阱过滤触发。收益用未来 60/120/252 日行业指数收益评估，并和全行业等权基准比较。",
        "",
        "## 参数排序",
        "",
    ]
    lines.extend(render_markdown_table(top_candidates.head(18)))

    lines.extend(["", "## 状态触发覆盖", ""])
    if trigger_coverage.empty:
        lines.append("未生成触发覆盖。")
    else:
        display = trigger_coverage.copy()
        display["triggered_date_ratio"] = display["triggered_date_ratio"].map(fmt_pct)
        display["avg_triggered_count"] = display["avg_triggered_count"].map(lambda x: fmt_float(x, 2))
        display["median_triggered_count"] = display["median_triggered_count"].map(lambda x: fmt_float(x, 1))
        lines.extend(render_markdown_table(display.rename(columns={
            "state_zh": "状态",
            "period": "阶段",
            "date_count": "日期数",
            "triggered_date_count": "触发日期数",
            "avg_triggered_count": "平均触发行业数",
            "median_triggered_count": "中位触发行业数",
            "max_triggered_count": "最大触发行业数",
            "triggered_date_ratio": "触发日期占比",
        })))

    lines.extend(["", "## 逐日净值摘要", ""])
    if nav_metrics.empty:
        lines.append("未生成逐日净值。")
    else:
        display = nav_metrics.sort_values("relative_final_nav", ascending=False).head(15).copy()
        for col in ["annualized_return", "benchmark_annualized_return", "annualized_relative_return", "max_drawdown", "relative_max_drawdown", "daily_relative_win_rate"]:
            display[col] = display[col].map(fmt_pct)
        lines.extend(render_markdown_table(display.rename(columns={
            "state_zh": "状态",
            "basket_zh": "组合",
            "daily_rows": "交易日",
            "relative_final_nav": "相对净值",
            "annualized_relative_return": "年化相对",
            "relative_max_drawdown": "相对回撤",
            "daily_relative_win_rate": "日跑赢率",
            "mean_selected_count": "平均持仓数",
        })))

    lines.extend(["", "## 状态定义审计", ""])
    lines.extend(render_markdown_table(state_audit))

    lines.extend(["", "## 数据与治理审计", ""])
    lines.extend(render_markdown_table(source_audit))

    lines.extend(["", "## 当前截面状态", ""])
    if current_snapshot.empty:
        lines.append("未生成当前截面状态。")
    else:
        display = current_snapshot.head(25).copy()
        for col in ["latest_bottom_score", "valuation_pit_score", "historical_valuation_score", "stabilized_oversold_signal", "quality_value_no_trap_score", "return_120d", "return_252d", "drawdown_252d", "dividend_yield"]:
            if col in display.columns:
                display[col] = display[col].map(fmt_pct)
        for col in ["momentum_trap_score", "pe", "pb"]:
            if col in display.columns:
                display[col] = display[col].map(lambda x: fmt_float(x, 2))
        lines.extend(render_markdown_table(display))

    lines.extend(
        [
            "",
            "## 输出文件说明",
            "",
            "- `top_candidates.csv`：低估超跌状态触发后的收益排序，适合先看。",
            "- `run_summary.json`：机器可读运行摘要。",
            "- `debug/`：完整状态面板、事件回测、非重叠、样本外、bootstrap、逐日净值、状态定义和当前截面状态。",
            "",
            "研究边界：本报告只研究申万行业和行业指数，不做个股筛选，不生成交易指令。公开源历史估值仍需源口径和发布时间审计，因此不能升级 validated_alpha。",
            "",
        ]
    )
    return "\n".join(lines)


def parse_int_list(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def mean_col(frame: pd.DataFrame, column: str) -> float:
    return float(frame[column].mean()) if column in frame.columns and not frame[column].dropna().empty else math.nan


def first_mode(frame: pd.DataFrame, column: str) -> str:
    if column not in frame.columns or frame.empty:
        return ""
    mode = frame[column].dropna().mode()
    return str(mode.iloc[0]) if not mode.empty else ""


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
