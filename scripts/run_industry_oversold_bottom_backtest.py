#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEATURES = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "historical_feature_panel.csv"
DEFAULT_RANKING = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "all_ranked_industries.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_oversold_bottom_backtest"
VERSION = "1.0.0"

SIGNAL_LABELS_ZH = {
    "price_only_oversold_signal": "纯超跌抄底",
    "stabilized_oversold_signal": "企稳后超跌抄底",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest SW industry index oversold bottom-fishing returns.")
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="Historical validation feature panel CSV.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current ranking CSV for industry names.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--signals", default="price_only_oversold_signal,stabilized_oversold_signal")
    parser.add_argument("--horizons", default="60,120,252", help="Forward return horizons.")
    parser.add_argument("--top-ns", default="5,10,20", help="Top N industry baskets to test.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="One-way turnover cost in basis points.")
    parser.add_argument("--oos-split-ratio", type=float, default=0.70, help="Chronological in-sample split ratio.")
    args = parser.parse_args()

    output_dir = Path(args.output)
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    signals = parse_str_list(args.signals)
    horizons = parse_int_list(args.horizons)
    top_ns = parse_int_list(args.top_ns)

    features = load_features(Path(args.features))
    names = load_industry_names(Path(args.ranking))
    features = attach_names(features, names)

    event_backtest = compute_event_backtest(
        features=features,
        signals=signals,
        horizons=horizons,
        top_ns=top_ns,
        cost_bps=args.cost_bps,
    )
    strategy_summary = summarize_event_backtest(event_backtest)
    quantile_report = compute_quantile_report(features, signals, horizons)
    oos_report = compute_oos_report(event_backtest, args.oos_split_ratio)
    regime_report = compute_regime_report(event_backtest)
    yearly_report = compute_yearly_report(event_backtest)
    current_top_oversold = build_current_top_oversold(features, signals, top=30)

    top_candidates = build_top_candidates(strategy_summary, oos_report)
    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    event_backtest.to_csv(debug_dir / "event_backtest.csv", index=False, encoding="utf-8-sig")
    strategy_summary.to_csv(debug_dir / "strategy_summary.csv", index=False, encoding="utf-8-sig")
    quantile_report.to_csv(debug_dir / "quantile_return_report.csv", index=False, encoding="utf-8-sig")
    oos_report.to_csv(debug_dir / "oos_strategy_report.csv", index=False, encoding="utf-8-sig")
    regime_report.to_csv(debug_dir / "regime_strategy_report.csv", index=False, encoding="utf-8-sig")
    yearly_report.to_csv(debug_dir / "yearly_strategy_report.csv", index=False, encoding="utf-8-sig")
    current_top_oversold.to_csv(debug_dir / "current_top_oversold_industries.csv", index=False, encoding="utf-8-sig")

    summary = {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "features_path": str(Path(args.features).resolve()),
        "output_dir": str(output_dir.resolve()),
        "research_boundary": "仅使用申万行业指数 PIT 价格衍生特征做超跌抄底持有期回测；不使用当前估值回填历史，不生成交易指令。",
        "backtest_method": "每 20 个交易日形成一次横截面样本，按超跌信号等权选择 Top N 行业，观察未来 60/120/252 个交易日收益。",
        "cost_bps": args.cost_bps,
        "signals": signals,
        "horizons": horizons,
        "top_ns": top_ns,
        "date_start": date_to_str(features["trade_date"].min()) if not features.empty else "",
        "date_end": date_to_str(features["trade_date"].max()) if not features.empty else "",
        "rebalance_date_count": int(features["trade_date"].nunique()) if not features.empty else 0,
        "feature_rows": int(len(features)),
        "event_rows": int(len(event_backtest)),
        "strategy_rows": int(len(strategy_summary)),
        "positive_relative_strategy_count": int((strategy_summary["mean_relative_return"] > 0).sum())
        if not strategy_summary.empty
        else 0,
        "positive_oos_relative_strategy_count": int(
            (oos_report[(oos_report["sample"] == "out_of_sample")]["mean_relative_return"] > 0).sum()
        )
        if not oos_report.empty
        else 0,
        "best_strategy": top_candidates.iloc[0].to_dict() if not top_candidates.empty else {},
    }
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            strategy_summary=strategy_summary,
            quantile_report=quantile_report,
            oos_report=oos_report,
            regime_report=regime_report,
            yearly_report=yearly_report,
            current_top_oversold=current_top_oversold,
        ),
        encoding="utf-8",
    )

    print(f"样本日期={summary['date_start']} 至 {summary['date_end']}")
    print(f"历史特征行数={summary['feature_rows']}")
    print(f"回测事件行数={summary['event_rows']}")
    print(f"策略组合数={summary['strategy_rows']}")
    if not top_candidates.empty:
        best = top_candidates.iloc[0]
        print(
            "排序第一组合={signal} Top{top_n} {horizon}日 成本后收益={ret} 相对收益={rel}".format(
                signal=best["信号"],
                top_n=best["TopN"],
                horizon=best["持有期"],
                ret=best["平均成本后收益"],
                rel=best["相对全行业收益"],
            )
        )
    print(f"输出目录={output_dir.resolve()}")


def load_features(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    frame["industry_code"] = frame["industry_code"].map(lambda value: str(value).zfill(6))
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame.dropna(subset=["trade_date"]).sort_values(["trade_date", "industry_code"]).reset_index(drop=True)


def load_industry_names(path: Path) -> pd.DataFrame:
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


def compute_event_backtest(
    *,
    features: pd.DataFrame,
    signals: list[str],
    horizons: list[int],
    top_ns: list[int],
    cost_bps: float,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if features.empty:
        return pd.DataFrame(rows)
    grouped = list(features.groupby("trade_date", sort=True))
    for signal in signals:
        if signal not in features.columns:
            continue
        for top_n in top_ns:
            for horizon in horizons:
                label = f"forward_return_{horizon}d"
                benchmark_label = f"benchmark_forward_return_{horizon}d"
                if label not in features.columns:
                    continue
                previous: set[str] = set()
                for trade_date, group in grouped:
                    required = ["industry_code", "industry_name", "parent_industry", signal, label]
                    sub = group[required + optional_columns(group, [benchmark_label, "avg_amount_60d", "market_regime", "volatility_regime"])].dropna(
                        subset=[signal, label]
                    )
                    if len(sub) < max(5, min(top_n, 5)):
                        continue
                    selected = sub.sort_values(signal, ascending=False).head(top_n).copy()
                    current = set(selected["industry_code"].tolist())
                    if not current:
                        continue
                    turnover = len(current.symmetric_difference(previous)) / max(len(current), 1) if previous else 1.0
                    gross = float(selected[label].mean())
                    benchmark = float(group[benchmark_label].dropna().mean()) if benchmark_label in group.columns else float(group[label].mean())
                    cost = turnover * cost_bps / 10000.0
                    net = gross - cost
                    rows.append(
                        {
                            "trade_date": trade_date.strftime("%Y-%m-%d"),
                            "signal": signal,
                            "signal_zh": SIGNAL_LABELS_ZH.get(signal, signal),
                            "top_n": int(len(selected)),
                            "horizon": int(horizon),
                            "gross_forward_return": gross,
                            "turnover": turnover,
                            "cost_bps": cost_bps,
                            "cost_return": cost,
                            "net_forward_return": net,
                            "benchmark_forward_return": benchmark,
                            "benchmark_relative_return": net - benchmark,
                            "market_regime": first_value(selected, "market_regime"),
                            "volatility_regime": first_value(selected, "volatility_regime"),
                            "avg_selected_amount_60d": float(selected["avg_amount_60d"].mean())
                            if "avg_amount_60d" in selected.columns
                            else math.nan,
                            "selected_industry_codes": "|".join(selected["industry_code"].tolist()),
                            "selected_industries": "|".join(selected["industry_name"].fillna(selected["industry_code"]).tolist()),
                        }
                    )
                    previous = current
    return pd.DataFrame(rows)


def summarize_event_backtest(event_backtest: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_backtest.empty:
        return pd.DataFrame(rows)
    for keys, group in event_backtest.groupby(["signal", "signal_zh", "top_n", "horizon"]):
        signal, signal_zh, top_n, horizon = keys
        ordered = group.sort_values("trade_date")
        equity = (1.0 + ordered["net_forward_return"]).cumprod()
        drawdown = equity / equity.cummax() - 1.0
        rows.append(
            {
                "signal": signal,
                "signal_zh": signal_zh,
                "top_n": int(top_n),
                "horizon": int(horizon),
                "start_date": ordered["trade_date"].iloc[0],
                "end_date": ordered["trade_date"].iloc[-1],
                "samples": int(len(ordered)),
                "mean_gross_return": float(ordered["gross_forward_return"].mean()),
                "mean_net_return": float(ordered["net_forward_return"].mean()),
                "median_net_return": float(ordered["net_forward_return"].median()),
                "annualized_net_return_proxy": annualize_return(float(ordered["net_forward_return"].mean()), int(horizon)),
                "mean_benchmark_return": float(ordered["benchmark_forward_return"].mean()),
                "mean_relative_return": float(ordered["benchmark_relative_return"].mean()),
                "win_rate": float((ordered["net_forward_return"] > 0).mean()),
                "benchmark_win_rate": float((ordered["benchmark_relative_return"] > 0).mean()),
                "mean_turnover": float(ordered["turnover"].mean()),
                "best_net_return": float(ordered["net_forward_return"].max()),
                "worst_net_return": float(ordered["net_forward_return"].min()),
                "event_sequence_max_drawdown": float(drawdown.min()) if not drawdown.empty else math.nan,
                "avg_selected_amount_60d": float(ordered["avg_selected_amount_60d"].mean())
                if "avg_selected_amount_60d" in ordered.columns
                else math.nan,
                "latest_selected_industries": ordered["selected_industries"].iloc[-1],
            }
        )
    return pd.DataFrame(rows).sort_values(["horizon", "mean_relative_return"], ascending=[True, False])


def compute_quantile_report(features: pd.DataFrame, signals: list[str], horizons: list[int]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if features.empty:
        return pd.DataFrame(rows)
    for signal in signals:
        if signal not in features.columns:
            continue
        for horizon in horizons:
            label = f"forward_return_{horizon}d"
            relative_label = f"benchmark_relative_return_{horizon}d"
            if label not in features.columns:
                continue
            bucket_values: dict[int, list[float]] = {idx: [] for idx in range(1, 6)}
            bucket_relative: dict[int, list[float]] = {idx: [] for idx in range(1, 6)}
            for _, group in features.groupby("trade_date", sort=True):
                sub = group[[signal, label] + ([relative_label] if relative_label in group.columns else [])].dropna(subset=[signal, label]).copy()
                if len(sub) < 10:
                    continue
                sub["quantile"] = pd.qcut(sub[signal].rank(method="first"), q=5, labels=False) + 1
                for quantile, qgroup in sub.groupby("quantile"):
                    q = int(quantile)
                    bucket_values[q].append(float(qgroup[label].mean()))
                    if relative_label in qgroup.columns:
                        bucket_relative[q].append(float(qgroup[relative_label].mean()))
            for quantile in range(1, 6):
                values = pd.Series(bucket_values[quantile], dtype=float)
                relatives = pd.Series(bucket_relative[quantile], dtype=float)
                rows.append(
                    {
                        "signal": signal,
                        "signal_zh": SIGNAL_LABELS_ZH.get(signal, signal),
                        "horizon": int(horizon),
                        "quantile": quantile,
                        "quantile_zh": "最不超跌" if quantile == 1 else ("最超跌" if quantile == 5 else f"第{quantile}组"),
                        "mean_forward_return": float(values.mean()) if not values.empty else math.nan,
                        "mean_relative_return": float(relatives.mean()) if not relatives.empty else math.nan,
                        "win_rate": float((values > 0).mean()) if not values.empty else math.nan,
                        "samples": int(len(values)),
                    }
                )
    return pd.DataFrame(rows)


def compute_oos_report(event_backtest: pd.DataFrame, split_ratio: float) -> pd.DataFrame:
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
        for keys, group in frame.groupby(["signal", "signal_zh", "top_n", "horizon"]):
            signal, signal_zh, top_n, horizon = keys
            rows.append(metric_row(group, signal, signal_zh, top_n, horizon, sample=sample, sample_zh=sample_zh, split_date=split_date))
    return pd.DataFrame(rows)


def compute_regime_report(event_backtest: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_backtest.empty:
        return pd.DataFrame(rows)
    for keys, group in event_backtest.groupby(["signal", "signal_zh", "top_n", "horizon", "market_regime", "volatility_regime"], dropna=False):
        signal, signal_zh, top_n, horizon, market_regime, volatility_regime = keys
        row = metric_row(group, signal, signal_zh, top_n, horizon)
        row["market_regime"] = market_regime
        row["volatility_regime"] = volatility_regime
        rows.append(row)
    return pd.DataFrame(rows)


def compute_yearly_report(event_backtest: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if event_backtest.empty:
        return pd.DataFrame(rows)
    frame = event_backtest.copy()
    frame["year"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.year
    for keys, group in frame.dropna(subset=["year"]).groupby(["signal", "signal_zh", "top_n", "horizon", "year"]):
        signal, signal_zh, top_n, horizon, year = keys
        row = metric_row(group, signal, signal_zh, top_n, horizon)
        row["year"] = int(year)
        rows.append(row)
    return pd.DataFrame(rows)


def metric_row(
    group: pd.DataFrame,
    signal: str,
    signal_zh: str,
    top_n: int,
    horizon: int,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "signal": signal,
        "signal_zh": signal_zh,
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
    }
    row.update(extra)
    return row


def build_current_top_oversold(features: pd.DataFrame, signals: list[str], top: int) -> pd.DataFrame:
    if features.empty:
        return pd.DataFrame()
    latest = features.sort_values("trade_date").groupby("industry_code", as_index=False).tail(1).copy()
    preferred_signal = "stabilized_oversold_signal" if "stabilized_oversold_signal" in signals else signals[0]
    columns = [
        "trade_date",
        "industry_code",
        "industry_name",
        "parent_industry",
        "return_20d",
        "return_60d",
        "return_120d",
        "return_252d",
        "drawdown_252d",
        "price_only_oversold_signal",
        "stabilized_oversold_signal",
        "market_regime",
        "volatility_regime",
    ]
    available = [column for column in columns if column in latest.columns]
    return latest[available].sort_values(preferred_signal, ascending=False).head(top)


def build_top_candidates(strategy_summary: pd.DataFrame, oos_report: pd.DataFrame) -> pd.DataFrame:
    if strategy_summary.empty:
        return pd.DataFrame()
    frame = strategy_summary.copy()
    oos = oos_report[oos_report["sample"] == "out_of_sample"].copy() if not oos_report.empty else pd.DataFrame()
    if not oos.empty:
        oos = oos[
            [
                "signal",
                "top_n",
                "horizon",
                "mean_net_return",
                "mean_relative_return",
                "win_rate",
                "benchmark_win_rate",
            ]
        ].rename(
            columns={
                "mean_net_return": "oos_mean_net_return",
                "mean_relative_return": "oos_mean_relative_return",
                "win_rate": "oos_win_rate",
                "benchmark_win_rate": "oos_benchmark_win_rate",
            }
        )
        frame = frame.merge(oos, on=["signal", "top_n", "horizon"], how="left")
    else:
        frame["oos_mean_relative_return"] = math.nan
    frame["robust_score"] = (
        frame["mean_relative_return"].fillna(0.0)
        + frame.get("oos_mean_relative_return", pd.Series(0.0, index=frame.index)).fillna(0.0)
        + 0.02 * frame["benchmark_win_rate"].fillna(0.0)
    )
    frame = frame.sort_values("robust_score", ascending=False).head(30)
    rows: list[dict[str, Any]] = []
    for row in frame.to_dict("records"):
        rows.append(
            {
                "信号": row["signal_zh"],
                "TopN": int(row["top_n"]),
                "持有期": int(row["horizon"]),
                "样本数": int(row["samples"]),
                "平均成本后收益": fmt_pct(row["mean_net_return"]),
                "相对全行业收益": fmt_pct(row["mean_relative_return"]),
                "胜率": fmt_pct(row["win_rate"]),
                "跑赢全行业比例": fmt_pct(row["benchmark_win_rate"]),
                "样本外成本后收益": fmt_pct(row.get("oos_mean_net_return")),
                "样本外相对收益": fmt_pct(row.get("oos_mean_relative_return")),
                "平均换手": fmt_pct(row["mean_turnover"]),
                "最差单期收益": fmt_pct(row["worst_net_return"]),
                "最近一期行业": row.get("latest_selected_industries", ""),
            }
        )
    return pd.DataFrame(rows)


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    strategy_summary: pd.DataFrame,
    quantile_report: pd.DataFrame,
    oos_report: pd.DataFrame,
    regime_report: pd.DataFrame,
    yearly_report: pd.DataFrame,
    current_top_oversold: pd.DataFrame,
) -> str:
    lines = [
        "# 行业指数超跌抄底历史回测报告",
        "",
        f"版本：{VERSION}",
        "",
        "## 结论摘要",
        "",
        summary["research_boundary"],
        "",
        f"- 样本区间：{summary['date_start']} 至 {summary['date_end']}",
        f"- 横截面样本日数量：{summary['rebalance_date_count']}",
        f"- 行业特征行数：{summary['feature_rows']}",
        f"- 成本假设：单边换手 {summary['cost_bps']} bps",
        f"- 回测口径：{summary['backtest_method']}",
        "",
    ]
    if top_candidates.empty:
        lines.append("未生成可用回测结果。")
    else:
        best = top_candidates.iloc[0]
        if summary["positive_relative_strategy_count"] == 0:
            lines.extend(
                [
                    "核心判断：这些超跌抄底组合的平均绝对收益大多为正，但没有一个参数组合在全样本平均相对收益上跑赢全行业等权基准。",
                    "因此，历史证据更支持“超跌后有市场反弹收益”，不支持“单靠超跌能稳定选出跑赢行业”。",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    f"核心判断：共有 {summary['positive_relative_strategy_count']} 个参数组合在全样本平均相对收益上为正。",
                    f"其中 {summary['positive_oos_relative_strategy_count']} 个参数组合在样本外平均相对收益上为正。",
                    "",
                ]
            )
        lines.extend(
            [
                "下表按“全样本相对收益 + 样本外相对收益 + 跑赢比例”排序：",
                "",
                f"- 排序第一组合：{best['信号']}，Top{best['TopN']}，持有 {best['持有期']} 个交易日。",
                f"- 平均成本后收益：{best['平均成本后收益']}，相对全行业收益：{best['相对全行业收益']}。",
                f"- 样本外成本后收益：{best['样本外成本后收益']}，样本外相对收益：{best['样本外相对收益']}。",
                "",
            ]
        )
    lines.extend(["## 策略表现排序", ""])
    lines.extend(render_markdown_table(top_candidates.head(12)))

    lines.extend(["", "## 分组检验", ""])
    if quantile_report.empty:
        lines.append("未生成分组检验。")
    else:
        q5 = quantile_report[quantile_report["quantile"] == 5].copy()
        q1 = quantile_report[quantile_report["quantile"] == 1].copy()
        spread = q5.merge(q1, on=["signal", "signal_zh", "horizon"], suffixes=("_q5", "_q1"))
        spread["top_minus_bottom"] = spread["mean_forward_return_q5"] - spread["mean_forward_return_q1"]
        display = spread[["signal_zh", "horizon", "top_minus_bottom", "mean_relative_return_q5", "samples_q5"]].rename(
            columns={
                "signal_zh": "信号",
                "horizon": "持有期",
                "top_minus_bottom": "最超跌-最不超跌",
                "mean_relative_return_q5": "最超跌相对收益",
                "samples_q5": "样本数",
            }
        )
        display["最超跌-最不超跌"] = display["最超跌-最不超跌"].map(fmt_pct)
        display["最超跌相对收益"] = display["最超跌相对收益"].map(fmt_pct)
        lines.extend(render_markdown_table(display))

    lines.extend(["", "## 样本外检验", ""])
    if oos_report.empty:
        lines.append("未生成样本外检验。")
    else:
        main_oos = oos_report[
            (oos_report["sample"] == "out_of_sample")
            & (oos_report["signal"] == "stabilized_oversold_signal")
            & (oos_report["top_n"].isin([5, 10]))
        ].copy()
        lines.extend(render_metric_table(main_oos, include_sample=False))

    lines.extend(["", "## 市场状态检验", ""])
    if regime_report.empty:
        lines.append("未生成市场状态检验。")
    else:
        main_regime = regime_report[
            (regime_report["signal"] == "stabilized_oversold_signal")
            & (regime_report["top_n"] == 5)
        ].copy()
        lines.extend(render_regime_table(main_regime))

    lines.extend(["", "## 最近一期最超跌行业", ""])
    if current_top_oversold.empty:
        lines.append("未生成最近一期超跌行业列表。")
    else:
        display = current_top_oversold.head(10).copy()
        display["trade_date"] = display["trade_date"].dt.strftime("%Y-%m-%d")
        for column in ["return_20d", "return_60d", "return_120d", "return_252d", "drawdown_252d"]:
            if column in display.columns:
                display[column] = display[column].map(fmt_pct)
        lines.extend(
            render_markdown_table(
                display.rename(
                    columns={
                        "trade_date": "日期",
                        "industry_code": "行业代码",
                        "industry_name": "行业",
                        "parent_industry": "上级行业",
                        "return_20d": "20日收益",
                        "return_60d": "60日收益",
                        "return_120d": "120日收益",
                        "return_252d": "252日收益",
                        "drawdown_252d": "252日回撤",
                        "price_only_oversold_signal": "纯超跌分",
                        "stabilized_oversold_signal": "企稳超跌分",
                        "market_regime": "市场状态",
                        "volatility_regime": "波动状态",
                    }
                )
            )
        )

    lines.extend(
        [
            "",
            "## 使用限制",
            "",
            "- 这是持有期事件回测，不是逐日调仓净值曲线；60/120/252 日样本存在重叠。",
            "- 当前 PE/PB/股息率没有进入历史回测，避免把当前估值回填到历史。",
            "- 结果只能说明历史上“行业指数超跌后买入”的统计表现，不构成买入建议。",
            "",
            "## 复现文件",
            "",
            "- `debug/event_backtest.csv`",
            "- `debug/strategy_summary.csv`",
            "- `debug/quantile_return_report.csv`",
            "- `debug/oos_strategy_report.csv`",
            "- `debug/regime_strategy_report.csv`",
            "- `debug/yearly_strategy_report.csv`",
            "- `debug/current_top_oversold_industries.csv`",
            "",
        ]
    )
    return "\n".join(lines)


def render_metric_table(frame: pd.DataFrame, include_sample: bool) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    display = frame.copy()
    columns = ["signal_zh", "top_n", "horizon"]
    if include_sample:
        columns.append("sample_zh")
    columns.extend(["samples", "mean_net_return", "mean_relative_return", "win_rate", "benchmark_win_rate", "mean_turnover"])
    display = display[columns].rename(
        columns={
            "signal_zh": "信号",
            "top_n": "TopN",
            "horizon": "持有期",
            "sample_zh": "样本",
            "samples": "样本数",
            "mean_net_return": "成本后收益",
            "mean_relative_return": "相对收益",
            "win_rate": "胜率",
            "benchmark_win_rate": "跑赢比例",
            "mean_turnover": "换手",
        }
    )
    for column in ["成本后收益", "相对收益", "胜率", "跑赢比例", "换手"]:
        display[column] = display[column].map(fmt_pct)
    return render_markdown_table(display)


def render_regime_table(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    display = frame[
        [
            "signal_zh",
            "top_n",
            "horizon",
            "market_regime",
            "volatility_regime",
            "samples",
            "mean_net_return",
            "mean_relative_return",
            "benchmark_win_rate",
        ]
    ].copy()
    display = display.rename(
        columns={
            "signal_zh": "信号",
            "top_n": "TopN",
            "horizon": "持有期",
            "market_regime": "市场状态",
            "volatility_regime": "波动状态",
            "samples": "样本数",
            "mean_net_return": "成本后收益",
            "mean_relative_return": "相对收益",
            "benchmark_win_rate": "跑赢比例",
        }
    )
    for column in ["成本后收益", "相对收益", "跑赢比例"]:
        display[column] = display[column].map(fmt_pct)
    return render_markdown_table(display)


def render_markdown_table(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    columns = list(frame.columns)
    rows = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for record in frame.to_dict("records"):
        rows.append("| " + " | ".join(str(record.get(column, "")) for column in columns) + " |")
    return rows


def optional_columns(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    return [column for column in columns if column in frame.columns]


def first_value(frame: pd.DataFrame, column: str) -> Any:
    if column not in frame.columns or frame.empty:
        return ""
    value = frame[column].iloc[0]
    return "" if pd.isna(value) else value


def annualize_return(value: float, horizon: int) -> float:
    if pd.isna(value) or value <= -1.0:
        return math.nan
    return (1.0 + value) ** (252.0 / horizon) - 1.0


def date_to_str(value: Any) -> str:
    if pd.isna(value):
        return ""
    return pd.to_datetime(value).strftime("%Y-%m-%d")


def fmt_pct(value: Any) -> str:
    number = to_float(value)
    if number is None:
        return ""
    return f"{number * 100:.2f}%"


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


def parse_int_list(value: str) -> list[int]:
    items = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not items:
        raise ValueError("empty integer list")
    return items


def parse_str_list(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise ValueError("empty string list")
    return items


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    main()
