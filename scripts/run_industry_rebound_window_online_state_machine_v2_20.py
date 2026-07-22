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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_online_state_machine_policy_v2_20.json"
VERSION = "2.20.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.20 daily online rebound-window state-machine audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.20 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    close_matrix = load_close_matrix(ROOT / policy["history_dir"])
    features = build_daily_features(close_matrix, policy)
    strategy_summary, trades, transitions, baseline_comparison = run_state_machine_audit(features, policy)
    top_candidates = build_top_candidates(strategy_summary)
    leakage_audit = build_leakage_audit(policy, close_matrix, features)
    notes = build_optimization_notes(top_candidates, strategy_summary, baseline_comparison, policy)
    run_summary = build_run_summary(policy, features, strategy_summary, top_candidates, leakage_audit, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    features.to_csv(debug_dir / "daily_state_features.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug_dir / "state_machine_trades.csv", index=False, encoding="utf-8-sig")
    transitions.to_csv(debug_dir / "state_transition_log.csv", index=False, encoding="utf-8-sig")
    baseline_comparison.to_csv(debug_dir / "baseline_comparison.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(run_summary, top_candidates, trades, transitions, baseline_comparison, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V2.20逐日在线状态机审计完成")
    print(f"日频特征行数={run_summary['daily_feature_count']}")
    print(f"行业数={run_summary['industry_count']}")
    print(f"策略数={run_summary['strategy_count']}")
    print(f"状态机候选数={run_summary['state_machine_candidate_count']}")
    print(f"最佳策略={run_summary['best_strategy_id']}")
    print(f"审计失败数={run_summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_close_matrix(history_dir: Path) -> pd.DataFrame:
    frames: list[pd.Series] = []
    for path in sorted(history_dir.glob("*.csv")):
        raw = pd.read_csv(path, encoding="utf-8-sig")
        if "日期" not in raw.columns or "收盘" not in raw.columns:
            continue
        dates = pd.to_datetime(raw["日期"], errors="coerce")
        close = pd.to_numeric(raw["收盘"], errors="coerce")
        series = pd.Series(close.values, index=dates, name=path.stem.zfill(6)).dropna()
        series = series[~series.index.duplicated(keep="last")]
        if not series.empty:
            frames.append(series)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1, sort=True).sort_index()


def build_daily_features(close_matrix: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if close_matrix.empty:
        return pd.DataFrame()
    close = close_matrix.sort_index().copy()
    industry_returns = close.pct_change(fill_method=None)
    market_return = industry_returns.mean(axis=1, skipna=True).fillna(0.0)
    market_nav = (1.0 + market_return).cumprod()
    ret5 = market_nav / market_nav.shift(5) - 1.0
    ret10 = market_nav / market_nav.shift(10) - 1.0
    ret20 = market_nav / market_nav.shift(20) - 1.0
    ret60 = market_nav / market_nav.shift(60) - 1.0
    ret120 = market_nav / market_nav.shift(120) - 1.0
    rolling_high = market_nav.rolling(252, min_periods=120).max()
    drawdown252 = market_nav / rolling_high - 1.0
    industry_ret60 = close / close.shift(60) - 1.0
    negative_breadth60 = (industry_ret60 < 0).mean(axis=1, skipna=True)
    vol60 = market_return.rolling(60, min_periods=40).std() * math.sqrt(252)

    output = pd.DataFrame(
        {
            "trade_date": close.index,
            "market_daily_return": market_return.to_numpy(dtype=float),
            "market_nav": market_nav.to_numpy(dtype=float),
            "market_return_5d": ret5.to_numpy(dtype=float),
            "market_return_10d": ret10.to_numpy(dtype=float),
            "market_return_20d": ret20.to_numpy(dtype=float),
            "market_return_60d": ret60.to_numpy(dtype=float),
            "market_return_120d": ret120.to_numpy(dtype=float),
            "market_drawdown_252d": drawdown252.to_numpy(dtype=float),
            "negative_breadth_60d": negative_breadth60.to_numpy(dtype=float),
            "market_volatility_60d": vol60.to_numpy(dtype=float),
            "industry_count": int(close.shape[1]),
        }
    )
    output["return_pressure"] = clip01(-pd.to_numeric(output["market_return_60d"], errors="coerce") / 0.25)
    output["drawdown_pressure"] = clip01(-pd.to_numeric(output["market_drawdown_252d"], errors="coerce") / 0.35)
    output["volatility_pressure"] = clip01(pd.to_numeric(output["market_volatility_60d"], errors="coerce") / 0.35)
    output["breadth_pressure"] = clip01(pd.to_numeric(output["negative_breadth_60d"], errors="coerce"))
    output["market_stress_score"] = output[["return_pressure", "drawdown_pressure", "volatility_pressure", "breadth_pressure"]].mean(axis=1)
    output["breadth_repair_5d"] = pd.to_numeric(output["negative_breadth_60d"], errors="coerce").shift(5) - pd.to_numeric(output["negative_breadth_60d"], errors="coerce")
    output["stress_release_5d"] = pd.to_numeric(output["market_stress_score"], errors="coerce").shift(5) - pd.to_numeric(output["market_stress_score"], errors="coerce")
    output["trade_date_text"] = pd.to_datetime(output["trade_date"]).dt.strftime("%Y-%m-%d")
    start = pd.Timestamp(policy["feature_start_date"])
    output = output[output["trade_date"] >= start].dropna(subset=["market_stress_score", "negative_breadth_60d", "market_daily_return"]).reset_index(drop=True)
    return output


def run_state_machine_audit(features: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    transition_frames: list[pd.DataFrame] = []
    baseline_rows: list[dict[str, Any]] = []
    for strategy in policy["strategies"]:
        trades, transitions = run_strategy(features, strategy, policy)
        baseline_trades = run_baseline(features, strategy, policy)
        summary = summarize_trades(strategy, trades, baseline_trades, policy)
        summary_rows.append(summary)
        if not trades.empty:
            trade_frames.append(trades)
        if not transitions.empty:
            transition_frames.append(transitions)
        baseline_rows.append(build_baseline_row(strategy, trades, baseline_trades, policy))
    return pd.DataFrame(summary_rows), concat_frames(trade_frames), concat_frames(transition_frames), pd.DataFrame(baseline_rows)


def run_strategy(features: pd.DataFrame, strategy: dict[str, Any], policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    i = 0
    cooldown_until = -1
    dates = features["trade_date_text"].tolist()
    while i < len(features) - 2:
        if i <= cooldown_until:
            i += 1
            continue
        row = features.iloc[i]
        if not conditions_met(row, strategy["watch_conditions"]):
            i += 1
            continue

        watch_start_i = i
        transitions.append(
            transition(strategy, "watch_start", dates[i], "进入观察", row)
        )
        confirmed_i: int | None = None
        expired_i = min(watch_start_i + int(strategy["max_observation_days"]), len(features) - 3)
        j = watch_start_i
        while j <= expired_i:
            obs_days = j - watch_start_i
            current = features.iloc[j]
            if obs_days >= int(strategy["min_observation_days"]) and conditions_met(current, strategy["confirm_conditions"]):
                confirmed_i = j
                transitions.append(transition(strategy, "confirmed", dates[j], f"观察{obs_days}日后确认", current))
                break
            j += 1

        if confirmed_i is None:
            transitions.append(transition(strategy, "watch_expired", dates[expired_i], "观察期内未确认", features.iloc[expired_i]))
            i = expired_i + 1
            continue

        entry_i = confirmed_i + 1
        start_return_i = entry_i + 1
        trade = simulate_trade(features, strategy, entry_i, start_return_i, policy)
        if trade:
            rows.append(trade)
            transitions.append(transition(strategy, "trade_exit", trade["exit_date"], trade["exit_reason"], features.iloc[min(trade["exit_index"], len(features) - 1)]))
            cooldown_until = int(trade["exit_index"]) + int(strategy["cooldown_days"])
            i = cooldown_until + 1
        else:
            transitions.append(transition(strategy, "entry_skipped", dates[confirmed_i], "确认后剩余路径不足", features.iloc[confirmed_i]))
            i = confirmed_i + 1
    return pd.DataFrame(rows), pd.DataFrame(transitions)


def run_baseline(features: pd.DataFrame, strategy: dict[str, Any], policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    i = 0
    cooldown_until = -1
    while i < len(features) - 2:
        if i <= cooldown_until:
            i += 1
            continue
        row = features.iloc[i]
        if not conditions_met(row, strategy["watch_conditions"]):
            i += 1
            continue
        entry_i = i + 1
        start_return_i = entry_i + 1
        baseline_strategy = {**strategy, "stop_loss": policy["baseline"].get("stop_loss")}
        trade = simulate_trade(features, baseline_strategy, entry_i, start_return_i, policy, trade_type="baseline_immediate")
        if trade:
            rows.append(trade)
            cooldown_until = int(trade["exit_index"]) + int(policy["baseline"]["cooldown_days"])
            i = cooldown_until + 1
        else:
            i += 1
    return pd.DataFrame(rows)


def simulate_trade(
    features: pd.DataFrame,
    strategy: dict[str, Any],
    entry_i: int,
    start_return_i: int,
    policy: dict[str, Any],
    trade_type: str = "state_machine",
) -> dict[str, Any] | None:
    horizon = int(strategy["horizon"])
    if start_return_i >= len(features):
        return None
    end_i = min(start_return_i + horizon - 1, len(features) - 1)
    path = features.iloc[start_return_i : end_i + 1].copy()
    if path.empty:
        return None
    daily = pd.to_numeric(path["market_daily_return"], errors="coerce").fillna(0.0)
    nav = (1.0 + daily).cumprod()
    drawdown = nav / nav.cummax() - 1.0
    stop_loss = strategy.get("stop_loss")
    exit_reason = "到期"
    if stop_loss is not None:
        hit = nav[nav - 1.0 <= float(stop_loss)]
        if not hit.empty:
            hit_pos = hit.index[0]
            path = path.loc[:hit_pos].copy()
            nav = nav.loc[:hit_pos]
            drawdown = drawdown.loc[:hit_pos]
            exit_reason = "撤退阈值触发"
    min_nav_loss = float(nav.min() - 1.0)
    max_drawdown = float(drawdown.min())
    bad_threshold = float(policy["bad_window_thresholds"][str(horizon)])
    return {
        "strategy_id": strategy["strategy_id"],
        "strategy_name_zh": strategy["strategy_name_zh"],
        "trade_type": trade_type,
        "horizon": horizon,
        "entry_date": str(features.iloc[entry_i]["trade_date_text"]),
        "return_start_date": str(features.iloc[start_return_i]["trade_date_text"]),
        "exit_date": str(path.iloc[-1]["trade_date_text"]),
        "entry_index": int(entry_i),
        "start_return_index": int(start_return_i),
        "exit_index": int(path.index[-1]),
        "exit_reason": exit_reason,
        "trade_days": int(len(path)),
        "trade_return": float(nav.iloc[-1] - 1.0),
        "min_nav_loss": min_nav_loss,
        "max_drawdown": max_drawdown,
        "is_win": bool(nav.iloc[-1] > 1.0),
        "is_bad_window": bool(nav.iloc[-1] - 1.0 <= bad_threshold),
        "is_severe_path": bool(
            min_nav_loss < float(policy["promotion_thresholds"]["max_worst_min_nav_loss"])
            or max_drawdown < float(policy["promotion_thresholds"]["max_worst_min_nav_loss"])
        ),
    }


def summarize_trades(strategy: dict[str, Any], trades: pd.DataFrame, baseline: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    row = {
        "strategy_id": strategy["strategy_id"],
        "strategy_name_zh": strategy["strategy_name_zh"],
        "horizon": int(strategy["horizon"]),
        "trade_count": int(len(trades)),
        "baseline_trade_count": int(len(baseline)),
    }
    row.update(prefix_metrics(metrics(trades), "state"))
    row.update(prefix_metrics(metrics(baseline), "baseline"))
    row["delta_mean_return_vs_baseline"] = safe_sub(row.get("state_mean_return"), row.get("baseline_mean_return"))
    row["delta_win_rate_vs_baseline"] = safe_sub(row.get("state_win_rate"), row.get("baseline_win_rate"))
    row["delta_bad_window_rate_vs_baseline"] = safe_sub(row.get("baseline_bad_window_rate"), row.get("state_bad_window_rate"))
    row["delta_worst_min_nav_loss_vs_baseline"] = safe_sub(row.get("state_worst_min_nav_loss"), row.get("baseline_worst_min_nav_loss"))
    row["state_machine_score"] = score_state_machine(row)
    row["state_machine_status"] = classify_state_machine(row, policy)
    return row


def metrics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "mean_return": math.nan,
            "median_return": math.nan,
            "worst_return": math.nan,
            "win_rate": math.nan,
            "bad_window_rate": math.nan,
            "severe_path_rate": math.nan,
            "worst_min_nav_loss": math.nan,
            "worst_max_drawdown": math.nan,
            "stop_trigger_rate": math.nan,
            "compound_nav": math.nan,
        }
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    return {
        "mean_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "worst_return": float(returns.min()),
        "win_rate": float(trades["is_win"].mean()),
        "bad_window_rate": float(trades["is_bad_window"].mean()),
        "severe_path_rate": float(trades["is_severe_path"].mean()),
        "worst_min_nav_loss": float(pd.to_numeric(trades["min_nav_loss"], errors="coerce").min()),
        "worst_max_drawdown": float(pd.to_numeric(trades["max_drawdown"], errors="coerce").min()),
        "stop_trigger_rate": float((trades["exit_reason"] == "撤退阈值触发").mean()),
        "compound_nav": float((1.0 + returns).prod()),
    }


def build_baseline_row(strategy: dict[str, Any], trades: pd.DataFrame, baseline: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    summary = summarize_trades(strategy, trades, baseline, policy)
    return {
        "strategy_id": strategy["strategy_id"],
        "strategy_name_zh": strategy["strategy_name_zh"],
        "horizon": int(strategy["horizon"]),
        "state_trade_count": summary["trade_count"],
        "baseline_trade_count": summary["baseline_trade_count"],
        "state_mean_return": summary["state_mean_return"],
        "baseline_mean_return": summary["baseline_mean_return"],
        "state_worst_min_nav_loss": summary["state_worst_min_nav_loss"],
        "baseline_worst_min_nav_loss": summary["baseline_worst_min_nav_loss"],
        "state_bad_window_rate": summary["state_bad_window_rate"],
        "baseline_bad_window_rate": summary["baseline_bad_window_rate"],
        "delta_mean_return_vs_baseline": summary["delta_mean_return_vs_baseline"],
        "delta_worst_min_nav_loss_vs_baseline": summary["delta_worst_min_nav_loss_vs_baseline"],
        "delta_bad_window_rate_vs_baseline": summary["delta_bad_window_rate_vs_baseline"],
    }


def score_state_machine(row: dict[str, Any]) -> float:
    return float(
        2.0 * nz(row.get("state_mean_return"))
        + 1.0 * (nz(row.get("state_win_rate")) - 0.5)
        - 1.5 * nz(row.get("state_bad_window_rate"))
        - 1.2 * nz(row.get("state_severe_path_rate"))
        + 1.2 * nz(row.get("delta_worst_min_nav_loss_vs_baseline"))
        + 0.8 * nz(row.get("delta_bad_window_rate_vs_baseline"))
        + 0.4 * nz(row.get("delta_mean_return_vs_baseline"))
    )


def classify_state_machine(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    checks = {
        "sample": nz(row.get("trade_count")) >= int(th["min_trades"]),
        "return": nz(row.get("state_mean_return")) >= float(th["min_mean_trade_return"]),
        "win": nz(row.get("state_win_rate")) >= float(th["min_win_rate"]),
        "bad": nz(row.get("state_bad_window_rate"), 1.0) <= float(th["max_bad_window_rate"]),
        "severe": nz(row.get("state_severe_path_rate"), 1.0) <= float(th["max_severe_path_rate"]),
        "worst_return": nz(row.get("state_worst_return"), -1.0) >= float(th["max_worst_trade_return"]),
        "worst_path": nz(row.get("state_worst_min_nav_loss"), -1.0) >= float(th["max_worst_min_nav_loss"]),
        "path_delta": nz(row.get("delta_worst_min_nav_loss_vs_baseline")) >= float(th["min_delta_worst_min_nav_loss_vs_baseline"]),
        "bad_delta": nz(row.get("delta_bad_window_rate_vs_baseline")) >= float(th["min_delta_bad_window_rate_vs_baseline"]),
    }
    if all(checks.values()):
        return "逐日状态机候选"
    if not checks["sample"]:
        return "样本不足"
    if checks["return"] and checks["win"] and checks["worst_path"]:
        return "状态机观察"
    return "拒绝"


def build_top_candidates(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    priority = {"逐日状态机候选": 0, "状态机观察": 1, "样本不足": 2, "拒绝": 3}
    output = summary.copy()
    output["_priority"] = output["state_machine_status"].map(priority).fillna(9)
    output = output.sort_values(["_priority", "state_machine_score"], ascending=[True, False]).drop(columns=["_priority"])
    columns = [
        "strategy_id",
        "strategy_name_zh",
        "horizon",
        "state_machine_status",
        "trade_count",
        "baseline_trade_count",
        "state_mean_return",
        "state_worst_return",
        "state_win_rate",
        "state_bad_window_rate",
        "state_severe_path_rate",
        "state_worst_min_nav_loss",
        "state_worst_max_drawdown",
        "state_stop_trigger_rate",
        "baseline_mean_return",
        "baseline_bad_window_rate",
        "baseline_worst_min_nav_loss",
        "delta_mean_return_vs_baseline",
        "delta_bad_window_rate_vs_baseline",
        "delta_worst_min_nav_loss_vs_baseline",
        "state_machine_score",
    ]
    return output[[col for col in columns if col in output.columns]]


def build_leakage_audit(policy: dict[str, Any], close_matrix: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "daily_features_use_price_history_only",
                "status": "pass",
                "evidence": "features are built from close_matrix rolling returns, drawdown, breadth and volatility",
                "action": "不使用未来收益作为触发特征。",
            },
            {
                "audit_item": "next_close_entry_no_same_day_execution",
                "status": "pass",
                "evidence": "entry_date is one trading day after confirmation; realized return starts after entry_date",
                "action": "避免同日收盘执行。",
            },
            {
                "audit_item": "price_matrix_available",
                "status": "pass" if not close_matrix.empty else "fail",
                "evidence": f"dates={len(close_matrix)}; industries={len(close_matrix.columns)}; features={len(features)}",
                "action": "使用本地申万二级行业日频价格缓存。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(top_candidates: pd.DataFrame, summary: pd.DataFrame, baseline: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top_candidates.empty:
        return {"main_diagnosis": "V2.20没有可评估策略。", "next_iterations": ["检查日频价格缓存。"]}
    best = top_candidates.iloc[0].to_dict()
    candidates = summary[summary["state_machine_status"] == "逐日状态机候选"] if not summary.empty else pd.DataFrame()
    notes: list[str] = []
    if candidates.empty:
        notes.append("V2.20逐日在线状态机没有通过候选门槛。")
    else:
        notes.append("V2.20出现逐日状态机候选，但仍必须保持research_only。")
    notes.append(
        f"最佳策略为 {best.get('strategy_id', '')}，平均收益 {fmt_pct(best.get('state_mean_return'))}，"
        f"坏窗口比例 {fmt_pct(best.get('state_bad_window_rate'))}，最差入场后浮亏 {fmt_pct(best.get('state_worst_min_nav_loss'))}。"
    )
    notes.append(
        f"相对立即入场基线，平均收益变化 {fmt_pct(best.get('delta_mean_return_vs_baseline'))}，"
        f"最差浮亏变化 {fmt_pct(best.get('delta_worst_min_nav_loss_vs_baseline'))}。"
    )
    if nz(best.get("trade_count")) < float(policy["promotion_thresholds"]["min_trades"]):
        notes.append("样本数量仍不足，不能把状态机结果当作稳定规律。")
    if nz(best.get("state_mean_return")) <= 0:
        notes.append("状态机没有解决收益端问题，可能只是风险控制而非反弹窗口识别。")
    return {
        "best_strategy_id": best.get("strategy_id", ""),
        "best_status": best.get("state_machine_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "若继续推进，应冻结V2.20状态机后做walk-forward年度递推，并增加失败样本归因，而不是继续放宽参数。",
    }


def build_run_summary(policy: dict[str, Any], features: pd.DataFrame, summary: pd.DataFrame, top_candidates: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = summary[summary["state_machine_status"] == "逐日状态机候选"] if not summary.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage["status"] == "fail").sum()) if not leakage.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "daily_feature_count": int(len(features)),
        "industry_count": int(features["industry_count"].iloc[0]) if not features.empty else 0,
        "strategy_count": int(len(policy["strategies"])),
        "state_machine_candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_strategy_id": best.get("strategy_id", ""),
        "best_state_machine_status": best.get("state_machine_status", ""),
        "best_trade_count": int(best.get("trade_count", 0)) if pd.notna(best.get("trade_count", math.nan)) else 0,
        "best_mean_trade_return": float_or_none(best.get("state_mean_return")),
        "best_win_rate": float_or_none(best.get("state_win_rate")),
        "best_bad_window_rate": float_or_none(best.get("state_bad_window_rate")),
        "best_worst_min_nav_loss": float_or_none(best.get("state_worst_min_nav_loss")),
        "best_delta_mean_return_vs_baseline": float_or_none(best.get("delta_mean_return_vs_baseline")),
        "best_delta_worst_min_nav_loss_vs_baseline": float_or_none(best.get("delta_worst_min_nav_loss_vs_baseline")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(summary: dict[str, Any], top_candidates: pd.DataFrame, trades: pd.DataFrame, transitions: pd.DataFrame, baseline: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V2.20 逐日在线状态机反弹窗口审计报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines.append("V2.20 把反弹窗口识别改成逐日在线状态机：空仓、观察、确认、下一交易日入场、持有/撤退、冷却。")
    lines += [
        "",
        f"- 日频特征行数：{summary['daily_feature_count']}",
        f"- 行业数：{summary['industry_count']}",
        f"- 策略数：{summary['strategy_count']}",
        f"- 逐日状态机候选数：{summary['state_machine_candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 策略排序",
        "",
    ]
    lines.extend(table_or_empty(top_candidates, {
        "strategy_id": "策略ID",
        "strategy_name_zh": "策略",
        "horizon": "持有期",
        "state_machine_status": "状态",
        "trade_count": "交易数",
        "baseline_trade_count": "基线交易数",
        "state_mean_return": "状态机平均收益",
        "state_worst_return": "状态机最差收益",
        "state_win_rate": "状态机胜率",
        "state_bad_window_rate": "坏窗口比例",
        "state_severe_path_rate": "严重路径比例",
        "state_worst_min_nav_loss": "最差入场后浮亏",
        "state_worst_max_drawdown": "最差路径回撤",
        "state_stop_trigger_rate": "撤退触发比例",
        "baseline_mean_return": "立即入场平均收益",
        "baseline_bad_window_rate": "立即入场坏窗口",
        "baseline_worst_min_nav_loss": "立即入场最差浮亏",
        "delta_mean_return_vs_baseline": "平均收益变化",
        "delta_bad_window_rate_vs_baseline": "坏窗口改善",
        "delta_worst_min_nav_loss_vs_baseline": "最差浮亏改善",
    }, {
        "state_mean_return",
        "state_worst_return",
        "state_win_rate",
        "state_bad_window_rate",
        "state_severe_path_rate",
        "state_worst_min_nav_loss",
        "state_worst_max_drawdown",
        "state_stop_trigger_rate",
        "baseline_mean_return",
        "baseline_bad_window_rate",
        "baseline_worst_min_nav_loss",
        "delta_mean_return_vs_baseline",
        "delta_bad_window_rate_vs_baseline",
        "delta_worst_min_nav_loss_vs_baseline",
    }))
    best_strategy = str(summary.get("best_strategy_id", ""))
    best_trades = trades[trades["strategy_id"].astype(str) == best_strategy].copy() if not trades.empty else pd.DataFrame()
    lines += ["", "## 最佳策略交易明细", ""]
    lines.extend(table_or_empty(best_trades, {
        "entry_date": "入场日",
        "return_start_date": "收益开始日",
        "exit_date": "退出日",
        "exit_reason": "退出原因",
        "trade_days": "交易天数",
        "trade_return": "交易收益",
        "min_nav_loss": "入场后最大浮亏",
        "max_drawdown": "路径回撤",
        "is_bad_window": "坏窗口",
        "is_severe_path": "严重路径",
    }, {"trade_return", "min_nav_loss", "max_drawdown"}))
    best_transitions = transitions[transitions["strategy_id"].astype(str) == best_strategy].copy() if not transitions.empty else pd.DataFrame()
    lines += ["", "## 最佳策略状态转换", ""]
    lines.extend(table_or_empty(best_transitions.head(40), {
        "trade_date": "日期",
        "event": "状态事件",
        "message": "说明",
        "market_stress_score": "压力分",
        "negative_breadth_60d": "下跌广度",
        "market_return_5d": "5日收益",
        "breadth_repair_5d": "广度修复",
        "stress_release_5d": "压力释放",
    }, {
        "market_stress_score",
        "negative_breadth_60d",
        "market_return_5d",
        "breadth_repair_5d",
        "stress_release_5d",
    }))
    lines += ["", "## 下一轮优化方向", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议方向：{notes.get('recommended_next_direction', '')}")
    lines += ["", "## 审计", ""]
    lines.extend(table_or_empty(leakage, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += [
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文逐日在线状态机审计报告，优先打开。",
        "- `top_candidates.csv`：状态机策略排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：日频特征、交易明细、状态转换、基线对照、审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def conditions_met(row: pd.Series, conditions: list[dict[str, Any]]) -> bool:
    for condition in conditions:
        value = float_or_nan(row.get(str(condition["field"])))
        target = float(condition["value"])
        op = str(condition["op"])
        if math.isnan(value):
            return False
        if op == ">=" and not value >= target:
            return False
        if op == ">" and not value > target:
            return False
        if op == "<=" and not value <= target:
            return False
        if op == "<" and not value < target:
            return False
    return True


def transition(strategy: dict[str, Any], event: str, date: str, message: str, row: pd.Series) -> dict[str, Any]:
    return {
        "strategy_id": strategy["strategy_id"],
        "strategy_name_zh": strategy["strategy_name_zh"],
        "trade_date": date,
        "event": event,
        "message": message,
        "market_stress_score": row.get("market_stress_score", math.nan),
        "negative_breadth_60d": row.get("negative_breadth_60d", math.nan),
        "market_return_5d": row.get("market_return_5d", math.nan),
        "market_return_10d": row.get("market_return_10d", math.nan),
        "breadth_repair_5d": row.get("breadth_repair_5d", math.nan),
        "stress_release_5d": row.get("stress_release_5d", math.nan),
        "market_drawdown_252d": row.get("market_drawdown_252d", math.nan),
    }


def prefix_metrics(values: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {f"{prefix}_{key}": value for key, value in values.items()}


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；逐日在线状态机未通过候选门槛"
    return "research_only；存在逐日状态机候选，但仍需未来新增样本和walk-forward验证"


def clip01(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").clip(lower=0.0, upper=1.0)


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


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


def fmt_float(value: Any, digits: int = 3) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number:.{digits}f}"


def fmt_pct(value: Any) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number * 100:.2f}%"


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
