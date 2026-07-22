#!/usr/bin/env python
from __future__ import annotations

import argparse
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
DEFAULT_HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_pressure_reversal_v2_2"
VERSION = "2.2.0"


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    strategy_zh: str
    signal_col: str
    gate_col: str
    status_hint: str
    description: str


STRATEGIES = [
    StrategySpec(
        "pure_oversold_baseline",
        "基线：纯超跌",
        "price_only_oversold_signal",
        "eligible_all",
        "rejected_standalone_signal",
        "V1.x 已证伪的纯超跌基线。",
    ),
    StrategySpec(
        "stabilized_oversold_baseline",
        "基线：企稳超跌",
        "stabilized_oversold_signal",
        "eligible_all",
        "rejected_standalone_signal",
        "加入短期企稳的超跌基线。",
    ),
    StrategySpec(
        "pressure_reversal",
        "V2.1：压力扩样反转",
        "pressure_reversal_score",
        "eligible_pressure_not_trap",
        "conditional_only_signal",
        "使用连续市场压力分数扩样，并排除动量陷阱。",
    ),
    StrategySpec(
        "pressure_stabilized_reversal",
        "V2.1：压力企稳反转",
        "pressure_reversal_score",
        "eligible_pressure_stabilized",
        "conditional_only_signal",
        "市场压力较高时要求短期企稳和相对动量改善。",
    ),
    StrategySpec(
        "pressure_middle_tail_reversal",
        "V2.1：压力非极端尾部",
        "pressure_reversal_score",
        "eligible_pressure_middle_tail",
        "conditional_only_signal",
        "压力区间中只保留超跌但非极端尾部的行业。",
    ),
    StrategySpec(
        "extreme_pressure_reversal",
        "V2.1：极端压力反转",
        "pressure_reversal_score",
        "eligible_extreme_pressure_not_trap",
        "conditional_only_signal",
        "只在极端市场压力下检验反转。",
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.1/V2.2 pressure reversal validation.")
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="Historical feature panel.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current industry ranking for names.")
    parser.add_argument("--history-dir", default=str(DEFAULT_HISTORY_DIR), help="Cached industry daily histories.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--horizons", default="60,120,252", help="Forward holding horizons.")
    parser.add_argument("--top-ns", default="5,10,20", help="Top N baskets.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="One-way rebalance cost in bps.")
    parser.add_argument("--stress-threshold", type=float, default=0.65, help="Expanded pressure gate.")
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

    features = attach_names(load_features(Path(args.features)), load_names(Path(args.ranking)))
    signal_panel = build_pressure_signal_panel(
        features,
        stress_threshold=args.stress_threshold,
        extreme_stress_threshold=args.extreme_stress_threshold,
    )
    event_backtest = compute_event_backtest(signal_panel, horizons, top_ns, args.cost_bps)
    nonoverlap = compute_nonoverlap_backtest(event_backtest, args.rebalance_step_days)
    walk_forward = compute_walk_forward_oos(event_backtest, args.oos_split_ratio)
    bootstrap = compute_bootstrap_confidence(event_backtest, args.bootstrap_rounds)
    episode_report = compute_episode_report(event_backtest)
    episode_summary = summarize_pressure_episodes(signal_panel)

    close_matrix = load_close_matrix(Path(args.history_dir), signal_panel["industry_code"].dropna().unique().tolist())
    daily_nav = compute_daily_portfolio_nav(signal_panel, close_matrix, top_ns, args.cost_bps)
    nav_metrics = compute_nav_metrics(daily_nav)

    parameter_sensitivity = compute_parameter_sensitivity(
        event_backtest=event_backtest,
        nonoverlap=nonoverlap,
        walk_forward=walk_forward,
        bootstrap=bootstrap,
        nav_metrics=nav_metrics,
    )
    rejection_log = build_signal_rejection_log(parameter_sensitivity)
    top_candidates = build_top_candidates(parameter_sensitivity, rejection_log)
    trap_cases = build_momentum_trap_cases(signal_panel)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    signal_panel.to_csv(debug_dir / "pressure_signal_panel.csv", index=False, encoding="utf-8-sig")
    event_backtest.to_csv(debug_dir / "event_backtest.csv", index=False, encoding="utf-8-sig")
    nonoverlap.to_csv(debug_dir / "nonoverlap_backtest.csv", index=False, encoding="utf-8-sig")
    walk_forward.to_csv(debug_dir / "walk_forward_oos.csv", index=False, encoding="utf-8-sig")
    bootstrap.to_csv(debug_dir / "bootstrap_confidence.csv", index=False, encoding="utf-8-sig")
    episode_report.to_csv(debug_dir / "stress_episode_report.csv", index=False, encoding="utf-8-sig")
    episode_summary.to_csv(debug_dir / "pressure_episode_summary.csv", index=False, encoding="utf-8-sig")
    daily_nav.to_csv(debug_dir / "daily_portfolio_nav.csv", index=False, encoding="utf-8-sig")
    nav_metrics.to_csv(debug_dir / "portfolio_nav_metrics.csv", index=False, encoding="utf-8-sig")
    parameter_sensitivity.to_csv(debug_dir / "parameter_sensitivity.csv", index=False, encoding="utf-8-sig")
    trap_cases.to_csv(debug_dir / "momentum_trap_cases.csv", index=False, encoding="utf-8-sig")
    rejection_log.to_csv(debug_dir / "signal_rejection_log.csv", index=False, encoding="utf-8-sig")

    summary = {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "features_path": str(Path(args.features).resolve()),
        "history_dir": str(Path(args.history_dir).resolve()),
        "research_boundary": "V2.1/V2.2 只研究申万行业指数条件化反转；不做个股筛选，不生成交易指令，不使用当前估值回填历史。",
        "date_start": date_to_str(signal_panel["trade_date"].min()) if not signal_panel.empty else "",
        "date_end": date_to_str(signal_panel["trade_date"].max()) if not signal_panel.empty else "",
        "feature_rows": int(len(signal_panel)),
        "event_rows": int(len(event_backtest)),
        "nonoverlap_rows": int(len(nonoverlap)),
        "daily_nav_rows": int(len(daily_nav)),
        "pressure_episode_count": int(len(episode_summary)),
        "candidate_signal_count": int((rejection_log["signal_status"] == "candidate_signal").sum()) if not rejection_log.empty else 0,
        "conditional_signal_count": int((rejection_log["signal_status"] == "conditional_only_signal").sum()) if not rejection_log.empty else 0,
        "rejected_signal_count": int((rejection_log["signal_status"] == "rejected_standalone_signal").sum()) if not rejection_log.empty else 0,
        "stress_threshold": args.stress_threshold,
        "extreme_stress_threshold": args.extreme_stress_threshold,
        "bootstrap_rounds": args.bootstrap_rounds,
        "cost_bps": args.cost_bps,
        "horizons": horizons,
        "top_ns": top_ns,
    }
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            parameter_sensitivity=parameter_sensitivity,
            rejection_log=rejection_log,
            bootstrap=bootstrap,
            episode_summary=episode_summary,
            nav_metrics=nav_metrics,
            trap_cases=trap_cases,
        ),
        encoding="utf-8",
    )

    print(f"V{VERSION} 压力反转验证完成")
    print(f"样本区间={summary['date_start']} 至 {summary['date_end']}")
    print(f"压力episode数={summary['pressure_episode_count']}")
    print(f"事件行数={summary['event_rows']}")
    print(f"逐日净值行数={summary['daily_nav_rows']}")
    print(f"候选信号={summary['candidate_signal_count']}")
    print(f"条件信号={summary['conditional_signal_count']}")
    print(f"拒绝信号={summary['rejected_signal_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_features(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    frame["industry_code"] = frame["industry_code"].map(lambda value: str(value).zfill(6))
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame.dropna(subset=["trade_date"]).sort_values(["trade_date", "industry_code"]).reset_index(drop=True)


def load_names(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["industry_code", "industry_name", "parent_industry"])
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    frame["industry_code"] = frame["industry_code"].map(lambda value: str(value).zfill(6))
    return frame[["industry_code", "industry_name", "parent_industry"]].drop_duplicates("industry_code")


def attach_names(features: pd.DataFrame, names: pd.DataFrame) -> pd.DataFrame:
    if names.empty:
        frame = features.copy()
        frame["industry_name"] = frame["industry_code"]
        frame["parent_industry"] = ""
        return frame
    return features.merge(names, on="industry_code", how="left")


def build_pressure_signal_panel(features: pd.DataFrame, stress_threshold: float, extreme_stress_threshold: float) -> pd.DataFrame:
    frame = features.copy()
    for window in [20, 60, 120, 252]:
        col = f"return_{window}d"
        if col in frame.columns:
            market_col = f"benchmark_return_{window}d"
            relative_col = f"relative_return_{window}d"
            frame[market_col] = frame.groupby("trade_date")[col].transform("mean")
            frame[relative_col] = frame[col] - frame[market_col]

    date_context = (
        frame.groupby("trade_date")
        .agg(
            market_return_120d=("return_120d", "mean"),
            market_volatility_60d=("volatility_60d", "median"),
            market_drawdown_252d=("drawdown_252d", "mean"),
            negative_breadth_60d=("return_60d", lambda s: float((s < 0).mean())),
        )
        .reset_index()
    )
    date_context["return_pressure"] = (-date_context["market_return_120d"]).rank(pct=True, method="average")
    date_context["volatility_pressure"] = date_context["market_volatility_60d"].rank(pct=True, method="average")
    date_context["drawdown_pressure"] = (-date_context["market_drawdown_252d"]).rank(pct=True, method="average")
    date_context["breadth_pressure"] = date_context["negative_breadth_60d"].rank(pct=True, method="average")
    date_context["market_stress_score"] = (
        0.35 * date_context["return_pressure"].fillna(0.0)
        + 0.25 * date_context["volatility_pressure"].fillna(0.0)
        + 0.25 * date_context["drawdown_pressure"].fillna(0.0)
        + 0.15 * date_context["breadth_pressure"].fillna(0.0)
    )
    date_context["pressure_tier"] = date_context["market_stress_score"].map(
        lambda value: "极端压力" if value >= extreme_stress_threshold else ("压力区" if value >= stress_threshold else "普通状态")
    )
    date_context = assign_pressure_episodes(date_context, stress_threshold)
    frame = frame.merge(date_context, on="trade_date", how="left")

    frame["stabilization_score_raw"] = (
        frame["return_20d"].fillna(0.0).clip(lower=-0.10, upper=0.10)
        + frame["relative_return_20d"].fillna(0.0).clip(lower=-0.10, upper=0.10)
    )
    frame["stabilization_score"] = frame.groupby("trade_date")["stabilization_score_raw"].rank(pct=True, method="average")

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
    frame["extreme_pressure_gate"] = frame["market_stress_score"] >= extreme_stress_threshold
    frame["not_momentum_trap"] = frame["momentum_trap_score"] <= 1

    frame["pressure_reversal_score_raw"] = (
        0.38 * frame["stabilized_oversold_signal"].fillna(0.0)
        + 0.24 * frame["stabilization_score"].fillna(0.0)
        + 0.22 * frame["market_stress_score"].fillna(0.0)
        + 0.06 * frame["middle_tail_gate"].astype(float)
        - 0.18 * frame["momentum_trap_score"].clip(upper=3)
        - 0.08 * frame["extreme_tail_trap"].astype(float)
    )
    frame["pressure_reversal_score"] = frame.groupby("trade_date")["pressure_reversal_score_raw"].rank(pct=True, method="average")

    frame["eligible_all"] = True
    frame["eligible_pressure_not_trap"] = frame["pressure_gate"] & frame["not_momentum_trap"] & (frame["stabilized_oversold_signal"] >= 0.55)
    frame["eligible_pressure_stabilized"] = frame["eligible_pressure_not_trap"] & frame["stabilized_gate"]
    frame["eligible_pressure_middle_tail"] = frame["pressure_gate"] & frame["middle_tail_gate"] & frame["not_momentum_trap"]
    frame["eligible_extreme_pressure_not_trap"] = frame["extreme_pressure_gate"] & frame["not_momentum_trap"] & (
        frame["stabilized_oversold_signal"] >= 0.55
    )
    frame["signal_bucket"] = frame["momentum_trap_score"].map(
        lambda value: "疑似动量陷阱" if value >= 2 else ("可观察反转" if value == 1 else "反转候选")
    )
    return frame.drop(columns=["stabilization_score_raw", "pressure_reversal_score_raw"])


def assign_pressure_episodes(date_context: pd.DataFrame, stress_threshold: float) -> pd.DataFrame:
    ordered = date_context.sort_values("trade_date").copy()
    episode_number = 0
    active = False
    episode_ids: list[str] = []
    for is_pressure in (ordered["market_stress_score"] >= stress_threshold).tolist():
        if is_pressure and not active:
            episode_number += 1
            active = True
        elif not is_pressure:
            active = False
        episode_ids.append(f"stress_{episode_number:03d}" if is_pressure else "")
    ordered["pressure_episode_id"] = episode_ids
    return ordered


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
                    current = set(selected["industry_code"].tolist())
                    turnover = len(current.symmetric_difference(previous)) / max(len(current), 1) if previous else 1.0
                    gross = float(selected[label].mean())
                    benchmark = float(group[benchmark_label].dropna().mean()) if benchmark_label in group.columns else float(group[label].mean())
                    cost = turnover * cost_bps / 10000.0
                    rows.append(
                        {
                            "trade_date": date_to_str(trade_date),
                            "strategy_id": strategy.strategy_id,
                            "strategy_zh": strategy.strategy_zh,
                            "status_hint": strategy.status_hint,
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
                            "selected_industry_codes": "|".join(selected["industry_code"].tolist()),
                            "selected_industries": "|".join(selected["industry_name"].fillna(selected["industry_code"]).tolist()),
                        }
                    )
                    previous = current
    return pd.DataFrame(rows)


def compute_nonoverlap_backtest(event_backtest: pd.DataFrame, rebalance_step_days: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_backtest.empty:
        return pd.DataFrame(rows)
    for keys, group in event_backtest.groupby(["strategy_id", "strategy_zh", "top_n", "horizon"], sort=True):
        strategy_id, strategy_zh, top_n, horizon = keys
        ordered = group.sort_values("trade_date").reset_index(drop=True)
        stride = max(1, int(math.ceil(int(horizon) / max(rebalance_step_days, 1))))
        sample = ordered.iloc[::stride].copy()
        for row in sample.to_dict("records"):
            row["nonoverlap_stride"] = stride
            rows.append(row)
    return pd.DataFrame(rows)


def compute_walk_forward_oos(event_backtest: pd.DataFrame, split_ratio: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_backtest.empty:
        return pd.DataFrame(rows)
    dates = sorted(event_backtest["trade_date"].dropna().unique().tolist())
    split_index = min(max(1, int(len(dates) * min(max(split_ratio, 0.10), 0.90))), len(dates) - 1)
    split_date = dates[split_index - 1]
    for sample, sample_zh, sample_dates in [
        ("in_sample", "样本内", set(dates[:split_index])),
        ("out_of_sample", "样本外", set(dates[split_index:])),
    ]:
        frame = event_backtest[event_backtest["trade_date"].isin(sample_dates)]
        for keys, group in frame.groupby(["strategy_id", "strategy_zh", "top_n", "horizon"], sort=True):
            row = metric_row(group, *keys)
            row["sample"] = sample
            row["sample_zh"] = sample_zh
            row["split_date"] = split_date
            rows.append(row)
    return pd.DataFrame(rows)


def compute_bootstrap_confidence(event_backtest: pd.DataFrame, rounds: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_backtest.empty:
        return pd.DataFrame(rows)
    rng = np.random.default_rng(20260613)
    frame = event_backtest.copy()
    frame["year_block"] = pd.to_datetime(frame["trade_date"]).dt.year.astype(str)
    frame["bootstrap_block"] = frame["pressure_episode_id"].where(frame["pressure_episode_id"].astype(str) != "", frame["year_block"])
    for keys, group in frame.groupby(["strategy_id", "strategy_zh", "top_n", "horizon"], sort=True):
        strategy_id, strategy_zh, top_n, horizon = keys
        blocks = sorted(group["bootstrap_block"].dropna().unique().tolist())
        samples: list[float] = []
        if len(blocks) >= 2:
            block_map = {block: group[group["bootstrap_block"] == block]["benchmark_relative_return"].dropna().to_numpy() for block in blocks}
            for _ in range(rounds):
                sampled_blocks = rng.choice(blocks, size=len(blocks), replace=True)
                values = np.concatenate([block_map[block] for block in sampled_blocks if len(block_map[block])])
                if len(values):
                    samples.append(float(np.mean(values)))
        observed = float(group["benchmark_relative_return"].mean())
        rows.append(
            {
                "strategy_id": strategy_id,
                "strategy_zh": strategy_zh,
                "top_n": int(top_n),
                "horizon": int(horizon),
                "block_count": int(len(blocks)),
                "bootstrap_rounds": int(len(samples)),
                "observed_mean_relative_return": observed,
                "ci_5": float(np.quantile(samples, 0.05)) if samples else math.nan,
                "ci_50": float(np.quantile(samples, 0.50)) if samples else math.nan,
                "ci_95": float(np.quantile(samples, 0.95)) if samples else math.nan,
                "probability_positive": float(np.mean(np.array(samples) > 0)) if samples else math.nan,
            }
        )
    return pd.DataFrame(rows)


def compute_episode_report(event_backtest: pd.DataFrame) -> pd.DataFrame:
    if event_backtest.empty:
        return pd.DataFrame()
    frame = event_backtest[event_backtest["pressure_episode_id"].astype(str) != ""].copy()
    if frame.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for keys, group in frame.groupby(["strategy_id", "strategy_zh", "top_n", "horizon", "pressure_episode_id"], sort=True):
        strategy_id, strategy_zh, top_n, horizon, episode_id = keys
        row = metric_row(group, strategy_id, strategy_zh, top_n, horizon)
        row["pressure_episode_id"] = episode_id
        row["start_date"] = group["trade_date"].min()
        row["end_date"] = group["trade_date"].max()
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_pressure_episodes(signal_panel: pd.DataFrame) -> pd.DataFrame:
    frame = signal_panel[signal_panel["pressure_episode_id"].astype(str) != ""].copy()
    if frame.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    date_frame = frame.drop_duplicates(["trade_date", "pressure_episode_id"])
    for episode_id, group in date_frame.groupby("pressure_episode_id", sort=True):
        rows.append(
            {
                "pressure_episode_id": episode_id,
                "start_date": date_to_str(group["trade_date"].min()),
                "end_date": date_to_str(group["trade_date"].max()),
                "rebalance_dates": int(group["trade_date"].nunique()),
                "mean_market_stress_score": float(group["market_stress_score"].mean()),
                "max_market_stress_score": float(group["market_stress_score"].max()),
                "dominant_pressure_tier": group["pressure_tier"].mode().iloc[0] if not group["pressure_tier"].mode().empty else "",
            }
        )
    return pd.DataFrame(rows)


def load_close_matrix(history_dir: Path, codes: list[str]) -> pd.DataFrame:
    frames: list[pd.Series] = []
    for code in sorted({str(code).zfill(6) for code in codes}):
        path = history_dir / f"{code}.csv"
        if not path.exists():
            continue
        raw = pd.read_csv(path, encoding="utf-8-sig")
        if "日期" not in raw.columns or "收盘" not in raw.columns:
            continue
        dates = pd.to_datetime(raw["日期"], errors="coerce")
        close = pd.to_numeric(raw["收盘"], errors="coerce")
        series = pd.Series(close.values, index=dates, name=code).dropna()
        series = series[~series.index.duplicated(keep="last")]
        if not series.empty:
            frames.append(series)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1).sort_index()


def compute_daily_portfolio_nav(signal_panel: pd.DataFrame, close_matrix: pd.DataFrame, top_ns: list[int], cost_bps: float) -> pd.DataFrame:
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
    for strategy in STRATEGIES:
        for top_n in top_ns:
            nav = 1.0
            benchmark_nav = 1.0
            previous: set[str] = set()
            for idx, (feature_date, actual_date) in enumerate(actual_rebalances):
                group = signal_panel[signal_panel["trade_date"] == feature_date]
                eligible = group[group[strategy.gate_col].fillna(False)].dropna(subset=[strategy.signal_col]).copy()
                if len(eligible) < top_n:
                    continue
                selected = eligible.sort_values(strategy.signal_col, ascending=False).head(top_n).copy()
                current = set(selected["industry_code"].astype(str).str.zfill(6).tolist())
                current = {code for code in current if code in returns.columns}
                if len(current) < max(1, top_n // 2):
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
                    if selected_returns.empty:
                        continue
                    daily_return = float(selected_returns.mean())
                    benchmark_return = float(returns.loc[day].dropna().mean())
                    net_return = daily_return - cost_return if first_period_day else daily_return
                    first_period_day = False
                    nav *= 1.0 + net_return
                    benchmark_nav *= 1.0 + benchmark_return
                    rows.append(
                        {
                            "trade_date": date_to_str(day),
                            "feature_date": date_to_str(feature_date),
                            "strategy_id": strategy.strategy_id,
                            "strategy_zh": strategy.strategy_zh,
                            "top_n": int(top_n),
                            "selection_count": int(len(current)),
                            "daily_return": daily_return,
                            "net_daily_return": net_return,
                            "benchmark_daily_return": benchmark_return,
                            "strategy_nav": nav,
                            "benchmark_nav": benchmark_nav,
                            "relative_nav": nav / benchmark_nav if benchmark_nav else math.nan,
                            "turnover": turnover if not first_period_day else 0.0,
                            "selected_industry_codes": "|".join(sorted(current)),
                        }
                    )
                previous = current
    return pd.DataFrame(rows)


def compute_nav_metrics(daily_nav: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if daily_nav.empty:
        return pd.DataFrame(rows)
    for keys, group in daily_nav.groupby(["strategy_id", "strategy_zh", "top_n"], sort=True):
        strategy_id, strategy_zh, top_n = keys
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
                "strategy_id": strategy_id,
                "strategy_zh": strategy_zh,
                "top_n": int(top_n),
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
                "daily_win_rate": float((ordered["net_daily_return"] > 0).mean()),
                "daily_relative_win_rate": float((ordered["net_daily_return"] > ordered["benchmark_daily_return"]).mean()),
                "mean_rebalance_turnover": float(ordered.loc[ordered["turnover"] > 0, "turnover"].mean()),
            }
        )
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
    for keys, group in event_backtest.groupby(["strategy_id", "strategy_zh", "status_hint", "top_n", "horizon"], sort=True):
        strategy_id, strategy_zh, status_hint, top_n, horizon = keys
        row = metric_row(group, strategy_id, strategy_zh, top_n, horizon)
        row["status_hint"] = status_hint
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


def build_signal_rejection_log(parameter_sensitivity: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if parameter_sensitivity.empty:
        return pd.DataFrame(rows)
    for row in parameter_sensitivity.to_dict("records"):
        reasons: list[str] = []
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
        if row.get("status_hint") == "rejected_standalone_signal":
            signal_status = "rejected_standalone_signal"
            reasons.append("基线信号已证伪，不允许升级")
        elif all(checks.values()):
            signal_status = "candidate_signal"
        elif any([checks["full_positive"], checks["oos_positive"], checks["nonoverlap_positive"]]):
            signal_status = "conditional_only_signal"
        else:
            signal_status = "rejected_standalone_signal"
        rows.append(
            {
                "strategy_id": row["strategy_id"],
                "strategy_zh": row["strategy_zh"],
                "top_n": int(row["top_n"]),
                "horizon": int(row["horizon"]),
                "signal_status": signal_status,
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
                "rejection_reasons": "；".join(reasons) if reasons else "通过 V2.2 候选门槛",
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
                "净值交易日数": int(row.get("nav_daily_rows", 0)),
                "样本强度": fmt_pct(row.get("sample_strength", 0.0)),
                "全样本跑赢比例": fmt_pct(row["benchmark_win_rate"]),
                "样本数": int(row["samples"]),
                "非重叠样本数": int(row["nonoverlap_samples"]),
                "拒绝或保留原因": row.get("rejection_reasons", ""),
            }
        )
    return pd.DataFrame(rows)


def build_momentum_trap_cases(signal_panel: pd.DataFrame) -> pd.DataFrame:
    trap = signal_panel[signal_panel["momentum_trap_score"] >= 2].copy()
    if trap.empty:
        return pd.DataFrame()
    cols = [
        "trade_date",
        "industry_code",
        "industry_name",
        "parent_industry",
        "return_60d",
        "return_120d",
        "drawdown_252d",
        "market_stress_score",
        "momentum_trap_score",
        "pressure_tier",
        "forward_return_60d",
        "benchmark_relative_return_60d",
        "forward_return_120d",
        "benchmark_relative_return_120d",
        "forward_return_252d",
        "benchmark_relative_return_252d",
    ]
    return trap[[col for col in cols if col in trap.columns]].sort_values(
        ["momentum_trap_score", "benchmark_relative_return_252d"], ascending=[False, True]
    ).head(200)


def metric_row(group: pd.DataFrame, strategy_id: str, strategy_zh: str, top_n: int, horizon: int) -> dict[str, Any]:
    return {
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


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    parameter_sensitivity: pd.DataFrame,
    rejection_log: pd.DataFrame,
    bootstrap: pd.DataFrame,
    episode_summary: pd.DataFrame,
    nav_metrics: pd.DataFrame,
    trap_cases: pd.DataFrame,
) -> str:
    lines = [
        "# V2.1/V2.2 行业压力反转验证报告",
        "",
        f"版本：{VERSION}",
        "",
        "## 研究结论",
        "",
        summary["research_boundary"],
        "",
        f"- 样本区间：{summary['date_start']} 至 {summary['date_end']}",
        f"- 压力 episode 数：{summary['pressure_episode_count']}",
        f"- 事件回测行数：{summary['event_rows']}",
        f"- 非重叠事件行数：{summary['nonoverlap_rows']}",
        f"- 逐日净值行数：{summary['daily_nav_rows']}",
        f"- 候选信号数：{summary['candidate_signal_count']}",
        f"- 条件观察信号数：{summary['conditional_signal_count']}",
        f"- 拒绝独立信号数：{summary['rejected_signal_count']}",
        "",
        "V2.1 用连续市场压力分数替代硬分类，V2.2 增加逐日组合净值。当前严格门槛下，若候选信号仍为 0，则不能升级为 alpha；只能保留为条件观察或拒绝基线。",
        "",
        "## 策略排序",
        "",
    ]
    lines.extend(render_markdown_table(top_candidates.head(15)))

    lines.extend(["", "## Bootstrap 置信区间", ""])
    if bootstrap.empty:
        lines.append("未生成 bootstrap 结果。")
    else:
        focus = bootstrap.sort_values("probability_positive", ascending=False).head(10).copy()
        display = focus[
            ["strategy_zh", "top_n", "horizon", "block_count", "observed_mean_relative_return", "ci_5", "ci_50", "ci_95", "probability_positive"]
        ].rename(
            columns={
                "strategy_zh": "策略",
                "top_n": "TopN",
                "horizon": "持有期",
                "block_count": "分块数",
                "observed_mean_relative_return": "观察均值",
                "ci_5": "5%下沿",
                "ci_50": "中位数",
                "ci_95": "95%上沿",
                "probability_positive": "为正概率",
            }
        )
        for col in ["观察均值", "5%下沿", "中位数", "95%上沿", "为正概率"]:
            display[col] = display[col].map(fmt_pct)
        lines.extend(render_markdown_table(display))

    lines.extend(["", "## 压力 Episode", ""])
    if episode_summary.empty:
        lines.append("未识别到压力 episode。")
    else:
        display = episode_summary.sort_values("max_market_stress_score", ascending=False).head(12).copy()
        for col in ["mean_market_stress_score", "max_market_stress_score"]:
            display[col] = display[col].map(lambda value: fmt_float(value, 3))
        lines.extend(
            render_markdown_table(
                display.rename(
                    columns={
                        "pressure_episode_id": "Episode",
                        "start_date": "开始",
                        "end_date": "结束",
                        "rebalance_dates": "样本日数",
                        "mean_market_stress_score": "平均压力",
                        "max_market_stress_score": "最高压力",
                        "dominant_pressure_tier": "主要状态",
                    }
                )
            )
        )

    lines.extend(["", "## 逐日净值表现", ""])
    if nav_metrics.empty:
        lines.append("未生成逐日净值。")
    else:
        lines.append("交易日数少于 252 的净值结果只作为条件观察，不能用年化收益排序升级为 alpha。")

        enough_nav = nav_metrics[nav_metrics["daily_rows"] >= 252].copy()
        if enough_nav.empty:
            lines.append("")
            lines.append("没有交易日数达到 252 的条件策略净值样本。")
        else:
            lines.extend(["", "### 样本足够的逐日净值"])
            display = enough_nav.sort_values("relative_final_nav", ascending=False).head(12).copy()
            display = display[
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
            display["最终相对净值"] = display["最终相对净值"].map(lambda value: fmt_float(value, 3))
            for col in ["年化相对收益", "相对最大回撤", "日跑赢比例"]:
                display[col] = display[col].map(fmt_pct)
            lines.extend(render_markdown_table(display))

        thin_nav = nav_metrics[nav_metrics["daily_rows"] < 252].copy()
        if not thin_nav.empty:
            lines.extend(["", "### 低样本条件观察"])
            display = thin_nav.sort_values(["daily_rows", "relative_final_nav"], ascending=[False, False]).head(12).copy()
            display = display[
                [
                    "strategy_zh",
                    "top_n",
                    "daily_rows",
                    "relative_final_nav",
                    "relative_max_drawdown",
                    "daily_relative_win_rate",
                ]
            ].rename(
                columns={
                    "strategy_zh": "策略",
                    "top_n": "TopN",
                    "daily_rows": "交易日数",
                    "relative_final_nav": "最终相对净值",
                    "relative_max_drawdown": "相对最大回撤",
                    "daily_relative_win_rate": "日跑赢比例",
                }
            )
            display["最终相对净值"] = display["最终相对净值"].map(lambda value: fmt_float(value, 3))
            for col in ["相对最大回撤", "日跑赢比例"]:
                display[col] = display[col].map(fmt_pct)
            lines.extend(render_markdown_table(display))

    lines.extend(["", "## 动量陷阱样本", ""])
    if trap_cases.empty:
        lines.append("未识别到动量陷阱样本。")
    else:
        display = trap_cases.head(10).copy()
        display["trade_date"] = pd.to_datetime(display["trade_date"]).dt.strftime("%Y-%m-%d")
        for col in ["return_60d", "return_120d", "drawdown_252d", "benchmark_relative_return_252d"]:
            if col in display.columns:
                display[col] = display[col].map(fmt_pct)
        lines.extend(
            render_markdown_table(
                display.rename(
                    columns={
                        "trade_date": "日期",
                        "industry_code": "行业代码",
                        "industry_name": "行业",
                        "parent_industry": "上级行业",
                        "return_60d": "60日收益",
                        "return_120d": "120日收益",
                        "drawdown_252d": "252日回撤",
                        "market_stress_score": "压力分",
                        "momentum_trap_score": "陷阱分",
                        "pressure_tier": "压力状态",
                        "benchmark_relative_return_252d": "未来252日相对收益",
                    }
                )
            )
        )

    lines.extend(
        [
            "",
            "## 复现文件",
            "",
            "- `debug/pressure_signal_panel.csv`",
            "- `debug/event_backtest.csv`",
            "- `debug/nonoverlap_backtest.csv`",
            "- `debug/walk_forward_oos.csv`",
            "- `debug/bootstrap_confidence.csv`",
            "- `debug/stress_episode_report.csv`",
            "- `debug/pressure_episode_summary.csv`",
            "- `debug/daily_portfolio_nav.csv`",
            "- `debug/portfolio_nav_metrics.csv`",
            "- `debug/parameter_sensitivity.csv`",
            "- `debug/momentum_trap_cases.csv`",
            "- `debug/signal_rejection_log.csv`",
            "",
        ]
    )
    return "\n".join(lines)


def render_markdown_table(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    columns = list(frame.columns)
    rows = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for record in frame.to_dict("records"):
        rows.append("| " + " | ".join(str(record.get(column, "")) for column in columns) + " |")
    return rows


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


def translate_status(status: str) -> str:
    return {
        "candidate_signal": "候选信号",
        "conditional_only_signal": "条件观察",
        "rejected_standalone_signal": "拒绝独立使用",
    }.get(status, status)


def parse_int_list(value: str) -> list[int]:
    items = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not items:
        raise ValueError("empty integer list")
    return items


def safe_number(value: Any) -> float:
    number = to_float(value)
    return 0.0 if number is None else number


def to_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def fmt_pct(value: Any) -> str:
    number = to_float(value)
    if number is None:
        return ""
    return f"{number * 100:.2f}%"


def fmt_float(value: Any, digits: int) -> str:
    number = to_float(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}"


def date_to_str(value: Any) -> str:
    if pd.isna(value):
        return ""
    return pd.to_datetime(value).strftime("%Y-%m-%d")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    main()
