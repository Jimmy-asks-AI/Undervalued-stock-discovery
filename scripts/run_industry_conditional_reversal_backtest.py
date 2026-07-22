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

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEATURES = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "historical_feature_panel.csv"
DEFAULT_RANKING = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "all_ranked_industries.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_conditional_reversal_backtest"
VERSION = "2.0.0"


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
        strategy_id="pure_oversold_baseline",
        strategy_zh="基线：纯超跌",
        signal_col="price_only_oversold_signal",
        gate_col="eligible_all",
        status_hint="rejected_standalone_signal",
        description="V1.x 已证伪的纯超跌基线，用于对照，不再作为候选信号。",
    ),
    StrategySpec(
        strategy_id="stabilized_oversold_baseline",
        strategy_zh="基线：企稳超跌",
        signal_col="stabilized_oversold_signal",
        gate_col="eligible_all",
        status_hint="rejected_standalone_signal",
        description="加入短期企稳的基线信号，用于检验企稳是否足以修复纯超跌缺陷。",
    ),
    StrategySpec(
        strategy_id="filtered_middle_tail_reversal",
        strategy_zh="过滤：非极端尾部反转",
        signal_col="conditional_reversal_score",
        gate_col="eligible_middle_tail_not_trap",
        status_hint="conditional_only_signal",
        description="只保留超跌但非最极端尾部、且未触发动量陷阱的行业。",
    ),
    StrategySpec(
        strategy_id="downtrend_filtered_reversal",
        strategy_zh="条件：下行市场反转",
        signal_col="conditional_reversal_score",
        gate_col="eligible_downtrend_not_trap",
        status_hint="conditional_only_signal",
        description="仅在市场状态为下行时启用超跌反转，并排除动量陷阱。",
    ),
    StrategySpec(
        strategy_id="stress_filtered_reversal",
        strategy_zh="条件：下行高波动反转",
        signal_col="conditional_reversal_score",
        gate_col="eligible_stress_not_trap",
        status_hint="conditional_only_signal",
        description="仅检验历史上唯一较强的下行加高波动状态，样本数不足时保持观察。",
    ),
    StrategySpec(
        strategy_id="stress_stabilized_reversal",
        strategy_zh="条件：压力释放后企稳",
        signal_col="conditional_reversal_score",
        gate_col="eligible_stress_stabilized",
        status_hint="conditional_only_signal",
        description="下行高波动中要求短期企稳和相对动量改善，尝试避免继续下跌行业。",
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.0 conditional industry reversal backtest.")
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="Historical feature panel from V1.8.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current industry ranking file for names.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--horizons", default="60,120,252", help="Holding horizons.")
    parser.add_argument("--top-ns", default="5,10,20", help="Top N baskets.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="One-way turnover cost in basis points.")
    parser.add_argument("--oos-split-ratio", type=float, default=0.70, help="Chronological out-of-sample split.")
    parser.add_argument("--rebalance-step-days", type=int, default=20, help="Feature panel rebalance step approximation.")
    args = parser.parse_args()

    output_dir = Path(args.output)
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    horizons = parse_int_list(args.horizons)
    top_ns = parse_int_list(args.top_ns)

    features = attach_names(load_features(Path(args.features)), load_names(Path(args.ranking)))
    signal_panel = build_conditional_signal_panel(features)
    event_backtest = compute_event_backtest(signal_panel, horizons, top_ns, args.cost_bps)
    nonoverlap = compute_nonoverlap_backtest(event_backtest, args.rebalance_step_days)
    portfolio_nav = compute_portfolio_nav(nonoverlap)
    regime_breakdown = compute_regime_breakdown(event_backtest)
    walk_forward = compute_walk_forward_oos(event_backtest, args.oos_split_ratio)
    parameter_sensitivity = compute_parameter_sensitivity(event_backtest, nonoverlap, walk_forward)
    rejection_log = build_signal_rejection_log(parameter_sensitivity)
    top_candidates = build_top_candidates(parameter_sensitivity, rejection_log)
    trap_cases = build_momentum_trap_cases(signal_panel, event_backtest)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    signal_panel.to_csv(debug_dir / "conditional_signal_panel.csv", index=False, encoding="utf-8-sig")
    event_backtest.to_csv(debug_dir / "event_backtest.csv", index=False, encoding="utf-8-sig")
    nonoverlap.to_csv(debug_dir / "nonoverlap_backtest.csv", index=False, encoding="utf-8-sig")
    portfolio_nav.to_csv(debug_dir / "portfolio_nav.csv", index=False, encoding="utf-8-sig")
    portfolio_nav.to_csv(debug_dir / "benchmark_relative_nav.csv", index=False, encoding="utf-8-sig")
    regime_breakdown.to_csv(debug_dir / "regime_breakdown.csv", index=False, encoding="utf-8-sig")
    walk_forward.to_csv(debug_dir / "walk_forward_oos.csv", index=False, encoding="utf-8-sig")
    parameter_sensitivity.to_csv(debug_dir / "parameter_sensitivity.csv", index=False, encoding="utf-8-sig")
    trap_cases.to_csv(debug_dir / "momentum_trap_cases.csv", index=False, encoding="utf-8-sig")
    rejection_log.to_csv(debug_dir / "signal_rejection_log.csv", index=False, encoding="utf-8-sig")

    summary = {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "features_path": str(Path(args.features).resolve()),
        "research_boundary": "V2.0 只研究申万行业指数的条件化反转；不做个股筛选，不生成交易指令，不使用当前估值回填历史。",
        "hypothesis_update": "纯超跌单独作为行业选择信号已被证伪；V2.0 检验市场状态门控、动量陷阱过滤和非重叠组合净值。",
        "date_start": date_to_str(signal_panel["trade_date"].min()) if not signal_panel.empty else "",
        "date_end": date_to_str(signal_panel["trade_date"].max()) if not signal_panel.empty else "",
        "feature_rows": int(len(signal_panel)),
        "event_rows": int(len(event_backtest)),
        "nonoverlap_rows": int(len(nonoverlap)),
        "strategy_count": int(parameter_sensitivity[["strategy_id", "top_n", "horizon"]].drop_duplicates().shape[0])
        if not parameter_sensitivity.empty
        else 0,
        "candidate_signal_count": int((rejection_log["signal_status"] == "candidate_signal").sum())
        if not rejection_log.empty
        else 0,
        "conditional_signal_count": int((rejection_log["signal_status"] == "conditional_only_signal").sum())
        if not rejection_log.empty
        else 0,
        "rejected_signal_count": int((rejection_log["signal_status"] == "rejected_standalone_signal").sum())
        if not rejection_log.empty
        else 0,
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
            regime_breakdown=regime_breakdown,
            walk_forward=walk_forward,
            trap_cases=trap_cases,
        ),
        encoding="utf-8",
    )

    print(f"V{VERSION} 条件化反转回测完成")
    print(f"样本区间={summary['date_start']} 至 {summary['date_end']}")
    print(f"事件行数={summary['event_rows']}")
    print(f"非重叠行数={summary['nonoverlap_rows']}")
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


def build_conditional_signal_panel(features: pd.DataFrame) -> pd.DataFrame:
    frame = features.copy()
    for window in [20, 60, 120, 252]:
        col = f"return_{window}d"
        if col in frame.columns:
            market_col = f"benchmark_return_{window}d"
            relative_col = f"relative_return_{window}d"
            frame[market_col] = frame.groupby("trade_date")[col].transform("mean")
            frame[relative_col] = frame[col] - frame[market_col]

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
    frame["market_down_gate"] = frame["market_regime"] == "下行"
    frame["stress_gate"] = (frame["market_regime"] == "下行") & (frame["volatility_regime"] == "高波动")
    frame["stabilized_gate"] = (frame["return_20d"] > 0) & (frame["relative_return_20d"] > 0)
    frame["middle_tail_gate"] = frame["stabilized_oversold_signal"].between(0.70, 0.95, inclusive="both")

    frame["conditional_reversal_score_raw"] = (
        0.45 * frame["stabilized_oversold_signal"].fillna(0.0)
        + 0.25 * frame["stabilization_score"].fillna(0.0)
        + 0.15 * frame["market_down_gate"].astype(float)
        + 0.10 * frame["stress_gate"].astype(float)
        + 0.05 * frame["middle_tail_gate"].astype(float)
        - 0.18 * frame["momentum_trap_score"].clip(upper=3)
        - 0.08 * frame["extreme_tail_trap"].astype(float)
    )
    frame["conditional_reversal_score"] = frame.groupby("trade_date")["conditional_reversal_score_raw"].rank(pct=True, method="average")

    frame["eligible_all"] = True
    frame["eligible_middle_tail_not_trap"] = frame["middle_tail_gate"] & (frame["momentum_trap_score"] <= 1)
    frame["eligible_downtrend_not_trap"] = (
        frame["market_down_gate"] & (frame["stabilized_oversold_signal"] >= 0.60) & (frame["momentum_trap_score"] <= 1)
    )
    frame["eligible_stress_not_trap"] = (
        frame["stress_gate"] & (frame["stabilized_oversold_signal"] >= 0.60) & (frame["momentum_trap_score"] <= 1)
    )
    frame["eligible_stress_stabilized"] = frame["eligible_stress_not_trap"] & frame["stabilized_gate"]
    frame["signal_bucket"] = frame["momentum_trap_score"].map(
        lambda value: "疑似动量陷阱" if value >= 2 else ("可观察反转" if value == 1 else "反转候选")
    )

    return frame.drop(columns=["stabilization_score_raw", "conditional_reversal_score_raw"])


def compute_event_backtest(
    signal_panel: pd.DataFrame,
    horizons: list[int],
    top_ns: list[int],
    cost_bps: float,
) -> pd.DataFrame:
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
                    eligible = group[group[strategy.gate_col].fillna(False)].copy()
                    required = ["industry_code", "industry_name", "parent_industry", strategy.signal_col, label]
                    eligible = eligible.dropna(subset=[strategy.signal_col, label])
                    if len(eligible) < top_n:
                        continue
                    selected = eligible.sort_values(strategy.signal_col, ascending=False).head(top_n).copy()
                    current = set(selected["industry_code"].tolist())
                    turnover = len(current.symmetric_difference(previous)) / max(len(current), 1) if previous else 1.0
                    gross = float(selected[label].mean())
                    benchmark = float(group[benchmark_label].dropna().mean()) if benchmark_label in group.columns else float(group[label].mean())
                    cost = turnover * cost_bps / 10000.0
                    net = gross - cost
                    rows.append(
                        {
                            "trade_date": date_to_str(trade_date),
                            "strategy_id": strategy.strategy_id,
                            "strategy_zh": strategy.strategy_zh,
                            "status_hint": strategy.status_hint,
                            "signal_col": strategy.signal_col,
                            "gate_col": strategy.gate_col,
                            "top_n": int(top_n),
                            "horizon": int(horizon),
                            "eligible_count": int(len(eligible)),
                            "gross_forward_return": gross,
                            "turnover": turnover,
                            "cost_bps": cost_bps,
                            "net_forward_return": net,
                            "benchmark_forward_return": benchmark,
                            "benchmark_relative_return": net - benchmark,
                            "market_regime": first_value(selected, "market_regime"),
                            "volatility_regime": first_value(selected, "volatility_regime"),
                            "avg_momentum_trap_score": float(selected["momentum_trap_score"].mean()),
                            "trap_industry_count": int((selected["momentum_trap_score"] >= 2).sum()),
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


def compute_portfolio_nav(nonoverlap: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if nonoverlap.empty:
        return pd.DataFrame(rows)
    for keys, group in nonoverlap.groupby(["strategy_id", "strategy_zh", "top_n", "horizon"], sort=True):
        strategy_id, strategy_zh, top_n, horizon = keys
        nav = 1.0
        benchmark_nav = 1.0
        for row in group.sort_values("trade_date").to_dict("records"):
            nav *= 1.0 + float(row["net_forward_return"])
            benchmark_nav *= 1.0 + float(row["benchmark_forward_return"])
            rows.append(
                {
                    "trade_date": row["trade_date"],
                    "strategy_id": strategy_id,
                    "strategy_zh": strategy_zh,
                    "top_n": int(top_n),
                    "horizon": int(horizon),
                    "strategy_nav": nav,
                    "benchmark_nav": benchmark_nav,
                    "relative_nav": nav / benchmark_nav if benchmark_nav else math.nan,
                    "net_forward_return": row["net_forward_return"],
                    "benchmark_forward_return": row["benchmark_forward_return"],
                    "benchmark_relative_return": row["benchmark_relative_return"],
                }
            )
    return pd.DataFrame(rows)


def compute_regime_breakdown(event_backtest: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_backtest.empty:
        return pd.DataFrame(rows)
    for keys, group in event_backtest.groupby(
        ["strategy_id", "strategy_zh", "top_n", "horizon", "market_regime", "volatility_regime"],
        dropna=False,
    ):
        strategy_id, strategy_zh, top_n, horizon, market_regime, volatility_regime = keys
        row = metric_row(group, strategy_id, strategy_zh, top_n, horizon)
        row["market_regime"] = market_regime
        row["volatility_regime"] = volatility_regime
        rows.append(row)
    return pd.DataFrame(rows)


def compute_walk_forward_oos(event_backtest: pd.DataFrame, split_ratio: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_backtest.empty:
        return pd.DataFrame(rows)
    dates = sorted(event_backtest["trade_date"].dropna().unique().tolist())
    split_index = min(max(1, int(len(dates) * min(max(split_ratio, 0.10), 0.90))), len(dates) - 1)
    split_date = dates[split_index - 1]
    samples = [
        ("in_sample", "样本内", set(dates[:split_index])),
        ("out_of_sample", "样本外", set(dates[split_index:])),
    ]
    for sample, sample_zh, sample_dates in samples:
        frame = event_backtest[event_backtest["trade_date"].isin(sample_dates)]
        for keys, group in frame.groupby(["strategy_id", "strategy_zh", "top_n", "horizon"], sort=True):
            strategy_id, strategy_zh, top_n, horizon = keys
            row = metric_row(group, strategy_id, strategy_zh, top_n, horizon)
            row["sample"] = sample
            row["sample_zh"] = sample_zh
            row["split_date"] = split_date
            rows.append(row)
    return pd.DataFrame(rows)


def compute_parameter_sensitivity(
    event_backtest: pd.DataFrame,
    nonoverlap: pd.DataFrame,
    walk_forward: pd.DataFrame,
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
        if not non.empty:
            row["nonoverlap_samples"] = int(len(non))
            row["nonoverlap_mean_relative_return"] = float(non["benchmark_relative_return"].mean())
            row["nonoverlap_benchmark_win_rate"] = float((non["benchmark_relative_return"] > 0).mean())
            row["nonoverlap_mean_net_return"] = float(non["net_forward_return"].mean())
        else:
            row["nonoverlap_samples"] = 0
            row["nonoverlap_mean_relative_return"] = math.nan
            row["nonoverlap_benchmark_win_rate"] = math.nan
            row["nonoverlap_mean_net_return"] = math.nan
        oos = walk_forward[
            (walk_forward["strategy_id"] == strategy_id)
            & (walk_forward["top_n"] == top_n)
            & (walk_forward["horizon"] == horizon)
            & (walk_forward["sample"] == "out_of_sample")
        ]
        if not oos.empty:
            row["oos_samples"] = int(oos.iloc[0]["samples"])
            row["oos_mean_relative_return"] = float(oos.iloc[0]["mean_relative_return"])
            row["oos_benchmark_win_rate"] = float(oos.iloc[0]["benchmark_win_rate"])
            row["oos_mean_net_return"] = float(oos.iloc[0]["mean_net_return"])
        else:
            row["oos_samples"] = 0
            row["oos_mean_relative_return"] = math.nan
            row["oos_benchmark_win_rate"] = math.nan
            row["oos_mean_net_return"] = math.nan
        row["robust_score"] = (
            safe_number(row["mean_relative_return"])
            + safe_number(row["oos_mean_relative_return"])
            + safe_number(row["nonoverlap_mean_relative_return"])
            + 0.02 * safe_number(row["benchmark_win_rate"])
            + 0.02 * safe_number(row["nonoverlap_benchmark_win_rate"])
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values("robust_score", ascending=False)


def build_signal_rejection_log(parameter_sensitivity: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if parameter_sensitivity.empty:
        return pd.DataFrame(rows)
    for row in parameter_sensitivity.to_dict("records"):
        reasons: list[str] = []
        pass_full = row["mean_relative_return"] > 0
        pass_oos = row["oos_mean_relative_return"] > 0
        pass_non = row["nonoverlap_mean_relative_return"] > 0
        pass_win = row["benchmark_win_rate"] > 0.50
        pass_non_win = row["nonoverlap_benchmark_win_rate"] > 0.50
        pass_sample = row["samples"] >= 80 and row["nonoverlap_samples"] >= 20 and row["oos_samples"] >= 20
        if not pass_full:
            reasons.append("全样本相对收益不为正")
        if not pass_oos:
            reasons.append("样本外相对收益不为正")
        if not pass_non:
            reasons.append("非重叠相对收益不为正")
        if not pass_win:
            reasons.append("全样本跑赢比例不足50%")
        if not pass_non_win:
            reasons.append("非重叠跑赢比例不足50%")
        if not pass_sample:
            reasons.append("有效样本不足")

        if all([pass_full, pass_oos, pass_non, pass_win, pass_non_win, pass_sample]):
            signal_status = "candidate_signal"
        elif row.get("status_hint") == "rejected_standalone_signal":
            signal_status = "rejected_standalone_signal"
            if "纯超跌" in str(row.get("strategy_zh")) or "企稳超跌" in str(row.get("strategy_zh")):
                reasons.append("V1.x 已证伪为独立信号")
        elif pass_oos or pass_non or pass_full:
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
                "benchmark_win_rate": row["benchmark_win_rate"],
                "nonoverlap_benchmark_win_rate": row["nonoverlap_benchmark_win_rate"],
                "samples": int(row["samples"]),
                "nonoverlap_samples": int(row["nonoverlap_samples"]),
                "oos_samples": int(row["oos_samples"]),
                "rejection_reasons": "；".join(reasons) if reasons else "通过 V2.0 候选门槛",
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
                "状态": translate_status(row.get("signal_status", "")),
                "全样本相对收益": fmt_pct(row["mean_relative_return"]),
                "样本外相对收益": fmt_pct(row["oos_mean_relative_return"]),
                "非重叠相对收益": fmt_pct(row["nonoverlap_mean_relative_return"]),
                "全样本跑赢比例": fmt_pct(row["benchmark_win_rate"]),
                "非重叠跑赢比例": fmt_pct(row["nonoverlap_benchmark_win_rate"]),
                "全样本成本后收益": fmt_pct(row["mean_net_return"]),
                "样本数": int(row["samples"]),
                "非重叠样本数": int(row["nonoverlap_samples"]),
                "拒绝或保留原因": row.get("rejection_reasons", ""),
            }
        )
    return pd.DataFrame(rows)


def build_momentum_trap_cases(signal_panel: pd.DataFrame, event_backtest: pd.DataFrame) -> pd.DataFrame:
    if signal_panel.empty:
        return pd.DataFrame()
    trap = signal_panel[signal_panel["momentum_trap_score"] >= 2].copy()
    if trap.empty:
        return pd.DataFrame()
    cols = [
        "trade_date",
        "industry_code",
        "industry_name",
        "parent_industry",
        "return_20d",
        "return_60d",
        "return_120d",
        "return_252d",
        "drawdown_252d",
        "stabilized_oversold_signal",
        "momentum_trap_score",
        "market_regime",
        "volatility_regime",
        "forward_return_60d",
        "benchmark_relative_return_60d",
        "forward_return_120d",
        "benchmark_relative_return_120d",
        "forward_return_252d",
        "benchmark_relative_return_252d",
    ]
    available = [col for col in cols if col in trap.columns]
    return (
        trap[available]
        .sort_values(["momentum_trap_score", "benchmark_relative_return_252d"], ascending=[False, True])
        .head(200)
    )


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
        "avg_momentum_trap_score": float(group["avg_momentum_trap_score"].mean()),
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    parameter_sensitivity: pd.DataFrame,
    rejection_log: pd.DataFrame,
    regime_breakdown: pd.DataFrame,
    walk_forward: pd.DataFrame,
    trap_cases: pd.DataFrame,
) -> str:
    lines = [
        "# V2.0 条件化行业反转回测报告",
        "",
        f"版本：{VERSION}",
        "",
        "## 研究结论",
        "",
        summary["research_boundary"],
        "",
        f"- 样本区间：{summary['date_start']} 至 {summary['date_end']}",
        f"- 回测事件数：{summary['event_rows']}",
        f"- 非重叠事件数：{summary['nonoverlap_rows']}",
        f"- 候选信号数：{summary['candidate_signal_count']}",
        f"- 条件观察信号数：{summary['conditional_signal_count']}",
        f"- 拒绝独立信号数：{summary['rejected_signal_count']}",
        "",
        "V2.0 的基准结论是：纯超跌和简单企稳超跌仍不能升级为独立行业 alpha。真正值得继续研究的是“市场下行或下行高波动之后，过滤动量陷阱后的条件化反转”。",
        "",
    ]

    lines.extend(["## 策略状态排序", ""])
    lines.extend(render_markdown_table(top_candidates.head(15)))

    lines.extend(["", "## V2.0 门槛判定", ""])
    status_counts = rejection_log["signal_status"].value_counts().to_dict() if not rejection_log.empty else {}
    lines.append(f"- `candidate_signal`：{status_counts.get('candidate_signal', 0)}")
    lines.append(f"- `conditional_only_signal`：{status_counts.get('conditional_only_signal', 0)}")
    lines.append(f"- `rejected_standalone_signal`：{status_counts.get('rejected_standalone_signal', 0)}")
    lines.append("")
    lines.append("候选门槛要求全样本、样本外、非重叠相对收益均为正，且全样本和非重叠跑赢比例都超过 50%。")

    lines.extend(["", "## 样本外摘要", ""])
    if walk_forward.empty:
        lines.append("未生成样本外结果。")
    else:
        oos = walk_forward[walk_forward["sample"] == "out_of_sample"].copy()
        focus = oos[oos["strategy_id"].isin(["downtrend_filtered_reversal", "stress_filtered_reversal", "stress_stabilized_reversal"])]
        display = focus.sort_values("mean_relative_return", ascending=False).head(12)
        lines.extend(render_metric_table(display))

    lines.extend(["", "## 市场状态摘要", ""])
    if regime_breakdown.empty:
        lines.append("未生成市场状态拆分。")
    else:
        focus = regime_breakdown[
            (regime_breakdown["strategy_id"].isin(["stress_filtered_reversal", "stress_stabilized_reversal"]))
            & (regime_breakdown["top_n"].isin([5, 10]))
        ].copy()
        display = focus.sort_values("mean_relative_return", ascending=False).head(12)
        lines.extend(render_regime_table(display))

    lines.extend(["", "## 动量陷阱样本", ""])
    if trap_cases.empty:
        lines.append("未识别到动量陷阱样本。")
    else:
        display = trap_cases.head(10).copy()
        display["trade_date"] = pd.to_datetime(display["trade_date"]).dt.strftime("%Y-%m-%d")
        for col in [
            "return_60d",
            "return_120d",
            "drawdown_252d",
            "benchmark_relative_return_60d",
            "benchmark_relative_return_120d",
            "benchmark_relative_return_252d",
        ]:
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
                        "stabilized_oversold_signal": "企稳超跌分",
                        "momentum_trap_score": "陷阱分",
                        "market_regime": "市场状态",
                        "volatility_regime": "波动状态",
                        "benchmark_relative_return_60d": "未来60日相对收益",
                        "benchmark_relative_return_120d": "未来120日相对收益",
                        "benchmark_relative_return_252d": "未来252日相对收益",
                    }
                )
            )
        )

    lines.extend(
        [
            "",
            "## 使用限制",
            "",
            "- V2.0 仍是研究验证，不是交易系统。",
            "- 条件状态样本更少，尤其下行高波动区域不能因为单个参数表现好就升级。",
            "- 当前版本不使用历史估值因子；估值因子要等每日估值快照积累后再做 PIT 验证。",
            "",
            "## 复现文件",
            "",
            "- `debug/conditional_signal_panel.csv`",
            "- `debug/event_backtest.csv`",
            "- `debug/nonoverlap_backtest.csv`",
            "- `debug/portfolio_nav.csv`",
            "- `debug/benchmark_relative_nav.csv`",
            "- `debug/regime_breakdown.csv`",
            "- `debug/walk_forward_oos.csv`",
            "- `debug/parameter_sensitivity.csv`",
            "- `debug/momentum_trap_cases.csv`",
            "- `debug/signal_rejection_log.csv`",
            "",
        ]
    )
    return "\n".join(lines)


def render_metric_table(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    display = frame[
        [
            "strategy_zh",
            "top_n",
            "horizon",
            "samples",
            "mean_relative_return",
            "benchmark_win_rate",
            "mean_net_return",
            "mean_turnover",
        ]
    ].copy()
    display = display.rename(
        columns={
            "strategy_zh": "策略",
            "top_n": "TopN",
            "horizon": "持有期",
            "samples": "样本数",
            "mean_relative_return": "相对收益",
            "benchmark_win_rate": "跑赢比例",
            "mean_net_return": "成本后收益",
            "mean_turnover": "换手",
        }
    )
    for col in ["相对收益", "跑赢比例", "成本后收益", "换手"]:
        display[col] = display[col].map(fmt_pct)
    return render_markdown_table(display)


def render_regime_table(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    display = frame[
        [
            "strategy_zh",
            "top_n",
            "horizon",
            "market_regime",
            "volatility_regime",
            "samples",
            "mean_relative_return",
            "benchmark_win_rate",
            "mean_net_return",
        ]
    ].copy()
    display = display.rename(
        columns={
            "strategy_zh": "策略",
            "top_n": "TopN",
            "horizon": "持有期",
            "market_regime": "市场状态",
            "volatility_regime": "波动状态",
            "samples": "样本数",
            "mean_relative_return": "相对收益",
            "benchmark_win_rate": "跑赢比例",
            "mean_net_return": "成本后收益",
        }
    )
    for col in ["相对收益", "跑赢比例", "成本后收益"]:
        display[col] = display[col].map(fmt_pct)
    return render_markdown_table(display)


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
