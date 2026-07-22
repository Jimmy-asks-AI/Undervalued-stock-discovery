#!/usr/bin/env python
from __future__ import annotations

import argparse
import copy
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
DEFAULT_POLICY = ROOT / "configs" / "realtime_pressure_sensitivity_policy_v2_10.json"
V29_SCRIPT = ROOT / "scripts" / "run_industry_realtime_simulation_v2_9.py"
VERSION = "2.10.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.10 pressure-gated realtime sensitivity validation.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.10 pressure sensitivity policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    parser.add_argument("--replay-audit-samples", type=int, default=5, help="Number of invested dates for as-of replay audit.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v29 = load_v29_module()
    base_policy_path = ROOT / policy["base_policy_path"]
    base_policy = v29.read_json(base_policy_path)
    v26 = v29.load_v26_module()
    v23 = v26.load_v23_module()

    raw_features = v23.load_features(v29.DEFAULT_FEATURES)
    names = v23.load_names(v29.DEFAULT_RANKING)
    features = v23.attach_names(raw_features, names)
    valuation = v26.load_valuation_history(v29.DEFAULT_VALUATION, release_lag_days=int(base_policy["release_lag_days"]))
    valuation_features = v26.build_valuation_feature_panel(valuation)
    signal_panel = v29.build_realtime_signal_panel(
        features=features,
        valuation_features=valuation_features,
        v26=v26,
        policy=base_policy,
    )
    signal_panel = v26.filter_cross_section_dates(signal_panel, min_count=int(base_policy["min_cross_section_count"]))
    close_matrix = v23.load_close_matrix(v29.DEFAULT_HISTORY_DIR, signal_panel["industry_code"].dropna().unique().tolist())

    parameter_grid = build_parameter_grid(policy)
    schedules: list[pd.DataFrame] = []
    ledgers: list[pd.DataFrame] = []
    event_frames: list[pd.DataFrame] = []
    nav_frames: list[pd.DataFrame] = []
    nav_metric_frames: list[pd.DataFrame] = []
    event_summary_frames: list[pd.DataFrame] = []

    for param in parameter_grid.to_dict("records"):
        run_policy = build_run_policy(base_policy, policy, param)
        scored_panel = attach_parameter_score(signal_panel, param)
        schedule = build_parameter_schedule(scored_panel, close_matrix, run_policy, param)
        ledger = v29.build_trade_ledger(schedule, scored_panel)
        events = v29.compute_event_returns(schedule, close_matrix, run_policy)
        daily_nav = v29.compute_daily_nav(schedule, close_matrix, run_policy)
        nav_metrics = v29.summarize_daily_nav(daily_nav)
        event_summary = summarize_parameter_events(events, nav_metrics, run_policy, param)

        schedule = add_param_columns(schedule, param)
        ledger = add_param_columns(ledger, param)
        events = add_param_columns(events, param)
        daily_nav = add_param_columns(daily_nav, param)
        nav_metrics = add_param_columns(nav_metrics, param)
        event_summary = add_param_columns(event_summary, param)

        schedules.append(schedule)
        ledgers.append(ledger)
        event_frames.append(events)
        nav_frames.append(daily_nav)
        nav_metric_frames.append(nav_metrics)
        event_summary_frames.append(event_summary)

    decision_log = concat_frames(schedules)
    trade_ledger = concat_frames(ledgers)
    event_returns = concat_frames(event_frames)
    daily_nav = concat_frames(nav_frames)
    nav_metrics = concat_frames(nav_metric_frames)
    parameter_summary = concat_frames(event_summary_frames)
    parameter_summary = attach_robust_scores(parameter_summary, policy)
    top_candidates = build_top_candidates(parameter_summary, policy)
    pressure_gate_effect = summarize_dimension(parameter_summary, ["pressure_gate_id", "pressure_gate_zh"], policy)
    momentum_trap_effect = summarize_dimension(parameter_summary, ["momentum_trap_max"], policy)
    weight_sensitivity = summarize_dimension(parameter_summary, ["weight_variant", "weight_variant_zh"], policy)
    cash_sensitivity = compute_cash_sensitivity(daily_nav, policy)
    timestamp_audit = v29.build_timestamp_audit(signal_panel, decision_log, base_policy)
    timestamp_audit.loc[timestamp_audit["audit_item"] == "policy_frozen", "evidence"] = (
        f"policy_id={policy['policy_id']}; parameter_count={len(parameter_grid)}"
    )
    timestamp_audit.loc[timestamp_audit["audit_item"] == "policy_frozen", "action"] = (
        "V2.10参数网格来自配置文件，不根据回测结果动态扩展。"
    )
    leakage_audit = build_v210_leakage_audit(policy, base_policy)
    replay_audit = build_v210_replay_audit(
        v29=v29,
        v26=v26,
        features=features,
        valuation_features=valuation_features,
        signal_panel=signal_panel,
        close_matrix=close_matrix,
        base_policy=base_policy,
        parameter_grid=parameter_grid,
        sample_count=max(0, int(args.replay_audit_samples)),
    )
    source_audit = v29.build_source_audit(valuation, signal_panel, base_policy)
    source_audit.loc[source_audit["audit_item"] == "promotion_boundary", "evidence"] = policy["promotion_rule"]
    source_audit.loc[source_audit["audit_item"] == "promotion_boundary", "action"] = "V2.10不生成交易指令。"

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    signal_panel.to_csv(debug_dir / "realtime_signal_panel.csv", index=False, encoding="utf-8-sig")
    parameter_grid.to_csv(debug_dir / "parameter_grid.csv", index=False, encoding="utf-8-sig")
    decision_log.to_csv(debug_dir / "parameter_decision_log.csv", index=False, encoding="utf-8-sig")
    trade_ledger.to_csv(debug_dir / "parameter_trade_ledger.csv", index=False, encoding="utf-8-sig")
    event_returns.to_csv(debug_dir / "parameter_event_returns.csv", index=False, encoding="utf-8-sig")
    daily_nav.to_csv(debug_dir / "parameter_daily_nav.csv", index=False, encoding="utf-8-sig")
    nav_metrics.to_csv(debug_dir / "parameter_nav_metrics.csv", index=False, encoding="utf-8-sig")
    parameter_summary.to_csv(debug_dir / "parameter_summary.csv", index=False, encoding="utf-8-sig")
    pressure_gate_effect.to_csv(debug_dir / "pressure_gate_effect.csv", index=False, encoding="utf-8-sig")
    momentum_trap_effect.to_csv(debug_dir / "momentum_trap_effect.csv", index=False, encoding="utf-8-sig")
    weight_sensitivity.to_csv(debug_dir / "weight_sensitivity.csv", index=False, encoding="utf-8-sig")
    cash_sensitivity.to_csv(debug_dir / "cash_sensitivity.csv", index=False, encoding="utf-8-sig")
    timestamp_audit.to_csv(debug_dir / "timestamp_audit.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    replay_audit.to_csv(debug_dir / "asof_replay_consistency.csv", index=False, encoding="utf-8-sig")
    source_audit.to_csv(debug_dir / "source_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "frozen_policy.json", {"v2_10_policy": policy, "base_v2_9_policy": base_policy})

    summary = build_run_summary(
        policy=policy,
        signal_panel=signal_panel,
        parameter_grid=parameter_grid,
        decision_log=decision_log,
        event_returns=event_returns,
        parameter_summary=parameter_summary,
        cash_sensitivity=cash_sensitivity,
        timestamp_audit=timestamp_audit,
        leakage_audit=leakage_audit,
        replay_audit=replay_audit,
        source_audit=source_audit,
    )
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            pressure_gate_effect=pressure_gate_effect,
            momentum_trap_effect=momentum_trap_effect,
            weight_sensitivity=weight_sensitivity,
            cash_sensitivity=cash_sensitivity,
            timestamp_audit=timestamp_audit,
            leakage_audit=leakage_audit,
            replay_audit=replay_audit,
        ),
        encoding="utf-8",
    )

    print("V2.10压力门控实时仿真与参数敏感性完成")
    print(f"信号面板行数={summary['signal_rows']}")
    print(f"参数组合数={summary['parameter_count']}")
    print(f"有持仓参数组合数={summary['invested_parameter_count']}")
    print(f"事件行数={summary['event_rows']}")
    print(f"候选待源审计组合数={summary['candidate_requires_source_audit_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v29_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_realtime_simulation_v2_9", V29_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load V2.9 module from {V29_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_parameter_grid(policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for pressure in policy["pressure_gates"]:
        for trap_max in policy["momentum_trap_max_values"]:
            for variant in policy["score_weight_variants"]:
                parameter_id = f"{pressure['pressure_gate_id']}__trap{trap_max}__{variant['weight_variant']}"
                rows.append(
                    {
                        "parameter_id": parameter_id,
                        "pressure_gate_id": pressure["pressure_gate_id"],
                        "pressure_gate_zh": pressure["pressure_gate_zh"],
                        "market_stress_min": pressure["market_stress_min"],
                        "momentum_trap_max": int(trap_max),
                        "weight_variant": variant["weight_variant"],
                        "weight_variant_zh": variant["weight_variant_zh"],
                        "weights_json": json.dumps(variant["weights"], ensure_ascii=False, sort_keys=True),
                    }
                )
    return pd.DataFrame(rows)


def build_run_policy(base_policy: dict[str, Any], policy: dict[str, Any], param: dict[str, Any]) -> dict[str, Any]:
    run_policy = copy.deepcopy(base_policy)
    run_policy["version"] = VERSION
    run_policy["policy_id"] = str(param["parameter_id"])
    run_policy["policy_name_zh"] = f"{param['pressure_gate_zh']} / 陷阱<={param['momentum_trap_max']} / {param['weight_variant_zh']}"
    run_policy["top_n"] = int(policy["top_n"])
    run_policy["min_triggered_count"] = int(policy["min_triggered_count"])
    run_policy["score_column"] = "parameter_bottom_score"
    return run_policy


def attach_parameter_score(signal_panel: pd.DataFrame, param: dict[str, Any]) -> pd.DataFrame:
    frame = signal_panel.copy()
    weights = json.loads(param["weights_json"])
    frame["parameter_bottom_score_raw"] = (
        float(weights["valuation_pit_score"]) * frame["valuation_pit_score"].fillna(0.0)
        + float(weights["historical_valuation_score"]) * frame["historical_valuation_score"].fillna(0.0)
        + float(weights["stabilized_oversold_signal"]) * frame["stabilized_oversold_signal"].fillna(0.0)
        + float(weights["quality_value_no_trap_score"]) * frame["quality_value_no_trap_score"].fillna(0.0)
        + float(weights["recovery_quality_score"]) * frame["recovery_quality_score"].fillna(0.0)
        + float(weights["momentum_trap_penalty"]) * frame["momentum_trap_score"].clip(upper=3).fillna(0.0)
    )
    frame["parameter_bottom_score"] = frame.groupby("trade_date")["parameter_bottom_score_raw"].rank(pct=True, method="average")
    return frame.drop(columns=["parameter_bottom_score_raw"], errors="ignore")


def build_parameter_schedule(
    signal_panel: pd.DataFrame,
    close_matrix: pd.DataFrame,
    run_policy: dict[str, Any],
    param: dict[str, Any],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if signal_panel.empty or close_matrix.empty:
        return pd.DataFrame(rows)
    close_dates = close_matrix.index.sort_values()
    feature_dates = sorted(signal_panel["trade_date"].dropna().unique().tolist())[:: int(run_policy["rebalance_feature_step"])]
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
        triggered = select_triggered(group, param)
        selected = triggered.sort_values("parameter_bottom_score", ascending=False).head(int(run_policy["top_n"]))
        if len(triggered) < int(run_policy["min_triggered_count"]) or len(selected) < int(run_policy["min_triggered_count"]):
            selected = pd.DataFrame()
        if selected.empty:
            selected_codes: set[str] = set()
        else:
            selected_codes = set(selected["industry_code"].astype(str).str.zfill(6).tolist())
        selected_codes = {code for code in selected_codes if code in close_matrix.columns}
        is_invested = len(selected_codes) >= int(run_policy["min_triggered_count"])
        if not is_invested:
            selected_codes = set()
        turnover = compute_turnover(previous, selected_codes)
        selected_subset = group[group["industry_code"].astype(str).str.zfill(6).isin(selected_codes)].copy()
        rows.append(
            {
                "signal_date": date_to_str(signal_date),
                "execution_date": date_to_str(execution_date),
                "state_id": "value_oversold_pressure_sensitivity",
                "policy_id": run_policy["policy_id"],
                "triggered_count": int(len(triggered)),
                "selected_count": int(len(selected_codes)),
                "is_invested": bool(is_invested),
                "turnover": turnover,
                "cost_bps": float(run_policy["cost_bps"]),
                "selected_codes": "|".join(sorted(selected_codes)),
                "selected_industries": "|".join(selected_subset.sort_values("parameter_bottom_score", ascending=False)["industry_name"].fillna("").astype(str).tolist()),
                "avg_bottom_score": mean_col(selected_subset, "parameter_bottom_score"),
                "avg_valuation_pit_score": mean_col(selected_subset, "valuation_pit_score"),
                "avg_historical_valuation_score": mean_col(selected_subset, "historical_valuation_score"),
                "avg_stabilized_oversold_signal": mean_col(selected_subset, "stabilized_oversold_signal"),
                "avg_quality_value_no_trap_score": mean_col(selected_subset, "quality_value_no_trap_score"),
                "avg_momentum_trap_score": mean_col(selected_subset, "momentum_trap_score"),
                "avg_market_stress_score": mean_col(selected_subset, "market_stress_score"),
                "avg_return_120d": mean_col(selected_subset, "return_120d"),
                "avg_return_252d": mean_col(selected_subset, "return_252d"),
                "avg_drawdown_252d": mean_col(selected_subset, "drawdown_252d"),
                "decision_reason": "触发并建仓" if is_invested else "触发数量不足或可交易历史不足，保持现金",
            }
        )
        previous = selected_codes
    return pd.DataFrame(rows)


def select_triggered(group: pd.DataFrame, param: dict[str, Any]) -> pd.DataFrame:
    if group.empty:
        return group.copy()
    mask = (
        group["low_value_flag"].fillna(False)
        & group["oversold_flag"].fillna(False)
        & (group["momentum_trap_score"].fillna(9) <= int(param["momentum_trap_max"]))
    )
    market_stress_min = param.get("market_stress_min")
    if pd.notna(market_stress_min):
        mask = mask & (group["market_stress_score"].fillna(-1.0) >= float(market_stress_min))
    return group[mask].dropna(subset=["parameter_bottom_score"]).copy()


def summarize_parameter_events(
    events: pd.DataFrame,
    nav_metrics: pd.DataFrame,
    run_policy: dict[str, Any],
    param: dict[str, Any],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    active_nav = nav_metrics[nav_metrics["scope"] == "active_only"].iloc[0].to_dict() if not nav_metrics.empty and (nav_metrics["scope"] == "active_only").any() else {}
    cash_nav = nav_metrics[nav_metrics["scope"] == "cash_when_no_signal"].iloc[0].to_dict() if not nav_metrics.empty and (nav_metrics["scope"] == "cash_when_no_signal").any() else {}
    for horizon in run_policy["horizons"]:
        group = events[events["horizon"] == int(horizon)] if not events.empty else pd.DataFrame()
        if group.empty:
            rows.append(empty_summary_row(horizon, active_nav, cash_nav))
            continue
        non = nonoverlap_events(group)
        oos = group[pd.to_datetime(group["signal_date"]) >= pd.Timestamp(run_policy["oos_start"])]
        row = {
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
            "active_relative_final_nav": float(active_nav.get("relative_final_nav", math.nan)),
            "active_final_nav": float(active_nav.get("final_nav", math.nan)),
            "active_benchmark_final_nav": float(active_nav.get("benchmark_final_nav", math.nan)),
            "cash_relative_final_nav": float(cash_nav.get("relative_final_nav", math.nan)),
        }
        row["signal_status"] = classify_parameter(row)
        rows.append(row)
    return pd.DataFrame(rows)


def empty_summary_row(horizon: int, active_nav: dict[str, Any], cash_nav: dict[str, Any]) -> dict[str, Any]:
    return {
        "horizon": int(horizon),
        "samples": 0,
        "nonoverlap_samples": 0,
        "oos_samples": 0,
        "mean_net_return": math.nan,
        "median_net_return": math.nan,
        "mean_benchmark_return": math.nan,
        "mean_relative_return": math.nan,
        "nonoverlap_mean_relative_return": math.nan,
        "oos_mean_relative_return": math.nan,
        "win_rate": math.nan,
        "benchmark_win_rate": math.nan,
        "avg_turnover": math.nan,
        "avg_selected_count": math.nan,
        "avg_bottom_score": math.nan,
        "avg_valuation_pit_score": math.nan,
        "avg_stabilized_oversold_signal": math.nan,
        "avg_quality_value_no_trap_score": math.nan,
        "avg_drawdown_252d": math.nan,
        "active_relative_final_nav": float(active_nav.get("relative_final_nav", math.nan)),
        "active_final_nav": float(active_nav.get("final_nav", math.nan)),
        "active_benchmark_final_nav": float(active_nav.get("benchmark_final_nav", math.nan)),
        "cash_relative_final_nav": float(cash_nav.get("relative_final_nav", math.nan)),
        "signal_status": "无样本",
    }


def classify_parameter(row: dict[str, Any]) -> str:
    if int(row.get("samples", 0)) < 8 or int(row.get("oos_samples", 0)) < 3:
        return "样本不足"
    checks = [
        row.get("mean_relative_return", math.nan) > 0,
        row.get("nonoverlap_mean_relative_return", math.nan) > 0,
        row.get("oos_mean_relative_return", math.nan) > 0,
        row.get("benchmark_win_rate", math.nan) > 0.5,
        row.get("active_relative_final_nav", math.nan) > 1.0,
    ]
    if all(checks) and int(row.get("nonoverlap_samples", 0)) >= 8:
        return "候选待源审计"
    if any(checks[:3]):
        return "条件观察"
    return "拒绝"


def attach_robust_scores(parameter_summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if parameter_summary.empty:
        return parameter_summary
    frame = parameter_summary.copy()
    primary = int(policy["primary_horizon"])
    frame["primary_horizon_flag"] = frame["horizon"] == primary
    frame["robust_score"] = (
        frame["mean_relative_return"].fillna(-1.0)
        + 0.7 * frame["oos_mean_relative_return"].fillna(-1.0)
        + 0.5 * frame["nonoverlap_mean_relative_return"].fillna(-1.0)
        + 0.02 * (frame["benchmark_win_rate"].fillna(0.0) - 0.5)
        + 0.01 * (frame["active_relative_final_nav"].fillna(0.0) - 1.0)
    )
    frame.loc[~frame["primary_horizon_flag"], "robust_score"] -= 0.02
    return frame.sort_values(["primary_horizon_flag", "robust_score"], ascending=[False, False]).reset_index(drop=True)


def build_top_candidates(parameter_summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if parameter_summary.empty:
        return pd.DataFrame()
    primary = int(policy["primary_horizon"])
    cols = [
        "parameter_id",
        "pressure_gate_zh",
        "market_stress_min",
        "momentum_trap_max",
        "weight_variant_zh",
        "horizon",
        "signal_status",
        "samples",
        "nonoverlap_samples",
        "oos_samples",
        "mean_net_return",
        "mean_benchmark_return",
        "mean_relative_return",
        "oos_mean_relative_return",
        "nonoverlap_mean_relative_return",
        "benchmark_win_rate",
        "active_relative_final_nav",
        "robust_score",
    ]
    top = parameter_summary[parameter_summary["horizon"] == primary].sort_values("robust_score", ascending=False).head(20)
    return top[[col for col in cols if col in top.columns]].copy()


def summarize_dimension(parameter_summary: pd.DataFrame, group_cols: list[str], policy: dict[str, Any]) -> pd.DataFrame:
    if parameter_summary.empty:
        return pd.DataFrame()
    primary = parameter_summary[parameter_summary["horizon"] == int(policy["primary_horizon"])].copy()
    rows: list[dict[str, Any]] = []
    for keys, group in primary.groupby(group_cols, dropna=False, sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(group_cols, keys)}
        row.update(
            {
                "parameter_count": int(len(group)),
                "candidate_count": int((group["signal_status"] == "候选待源审计").sum()),
                "conditional_count": int((group["signal_status"] == "条件观察").sum()),
                "mean_samples": float(group["samples"].mean()),
                "mean_relative_return": float(group["mean_relative_return"].mean()),
                "best_relative_return": float(group["mean_relative_return"].max()),
                "mean_oos_relative_return": float(group["oos_mean_relative_return"].mean()),
                "mean_nonoverlap_relative_return": float(group["nonoverlap_mean_relative_return"].mean()),
                "mean_active_relative_nav": float(group["active_relative_final_nav"].mean()),
                "best_robust_score": float(group["robust_score"].max()),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values("best_robust_score", ascending=False)


def compute_cash_sensitivity(daily_nav: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if daily_nav.empty:
        return pd.DataFrame(rows)
    for parameter_id, group in daily_nav.groupby("parameter_id", sort=True):
        ordered = group.sort_values("trade_date").copy()
        for cash in policy["cash_return_assumptions"]:
            daily_cash = (1.0 + float(cash["annual_cash_return"])) ** (1 / 252.0) - 1.0
            strategy_nav = 1.0
            benchmark_nav = 1.0
            for _, row in ordered.iterrows():
                strategy_return = float(row["net_daily_return"]) if bool(row["is_invested"]) else daily_cash
                benchmark_return = float(row["benchmark_daily_return"])
                strategy_nav *= 1.0 + strategy_return
                benchmark_nav *= 1.0 + benchmark_return
            meta = ordered.iloc[0].to_dict()
            rows.append(
                {
                    "parameter_id": parameter_id,
                    "pressure_gate_id": meta.get("pressure_gate_id", ""),
                    "pressure_gate_zh": meta.get("pressure_gate_zh", ""),
                    "momentum_trap_max": meta.get("momentum_trap_max", math.nan),
                    "weight_variant": meta.get("weight_variant", ""),
                    "weight_variant_zh": meta.get("weight_variant_zh", ""),
                    "cash_return_id": cash["cash_return_id"],
                    "cash_return_zh": cash["cash_return_zh"],
                    "annual_cash_return": float(cash["annual_cash_return"]),
                    "final_nav": strategy_nav,
                    "benchmark_final_nav": benchmark_nav,
                    "relative_final_nav": strategy_nav / benchmark_nav if benchmark_nav else math.nan,
                }
            )
    return pd.DataFrame(rows)


def build_v210_leakage_audit(policy: dict[str, Any], base_policy: dict[str, Any]) -> pd.DataFrame:
    feature_columns = list(base_policy.get("feature_columns_used", []))
    forbidden = list(policy.get("forbidden_feature_patterns", []))
    offending = [
        col
        for col in feature_columns
        if any(pattern.lower() in col.lower() for pattern in forbidden)
    ]
    rows = [
        {
            "audit_item": "future_label_excluded_from_feature_columns",
            "status": "pass" if not offending else "fail",
            "evidence": "|".join(offending) if offending else f"checked={len(feature_columns)} base feature columns",
            "action": "参数敏感性只扰动已冻结特征权重和门槛，不引入forward_return特征。",
        },
        {
            "audit_item": "pressure_gate_asof",
            "status": "pass",
            "evidence": "market_stress_score inherited from V2.9 expanding_percentile_asof",
            "action": "压力门槛只读取信号日已生成的as-of压力分。",
        },
        {
            "audit_item": "parameter_grid_predeclared",
            "status": "pass",
            "evidence": f"pressure_gates={len(policy['pressure_gates'])}; trap_values={len(policy['momentum_trap_max_values'])}; weight_variants={len(policy['score_weight_variants'])}",
            "action": "所有参数组合来自配置文件，不根据结果动态扩展。",
        },
    ]
    return pd.DataFrame(rows)


def build_v210_replay_audit(
    *,
    v29: Any,
    v26: Any,
    features: pd.DataFrame,
    valuation_features: pd.DataFrame,
    signal_panel: pd.DataFrame,
    close_matrix: pd.DataFrame,
    base_policy: dict[str, Any],
    parameter_grid: pd.DataFrame,
    sample_count: int,
) -> pd.DataFrame:
    if parameter_grid.empty:
        return pd.DataFrame()
    base_param = parameter_grid.iloc[0].to_dict()
    run_policy = build_run_policy(base_policy, {"top_n": base_policy["top_n"], "min_triggered_count": base_policy["min_triggered_count"]}, base_param)
    scored_panel = attach_parameter_score(signal_panel, base_param)
    schedule = build_parameter_schedule(scored_panel, close_matrix, run_policy, base_param)
    rows: list[dict[str, Any]] = []
    invested_dates = sorted(pd.to_datetime(schedule[schedule["is_invested"] == True]["signal_date"]).dropna().unique().tolist()) if not schedule.empty else []  # noqa: E712
    if not invested_dates:
        invested_dates = sorted(signal_panel["trade_date"].dropna().unique().tolist())
    if not invested_dates or sample_count <= 0:
        return pd.DataFrame(rows)
    positions = np.linspace(0, len(invested_dates) - 1, num=min(sample_count, len(invested_dates)), dtype=int)
    for pos in sorted(set(int(p) for p in positions)):
        date = pd.Timestamp(invested_dates[pos])
        full_selected = select_triggered(scored_panel[scored_panel["trade_date"] == date].copy(), base_param).sort_values("parameter_bottom_score", ascending=False).head(int(run_policy["top_n"]))
        full_codes = "|".join(full_selected["industry_code"].astype(str).str.zfill(6).tolist()) if not full_selected.empty else ""
        truncated_features = features[features["trade_date"] <= date].copy()
        replay_panel = v29.build_realtime_signal_panel(
            features=truncated_features,
            valuation_features=valuation_features,
            v26=v26,
            policy=base_policy,
        )
        replay_panel = v26.filter_cross_section_dates(replay_panel, min_count=int(base_policy["min_cross_section_count"]))
        replay_scored = attach_parameter_score(replay_panel, base_param)
        replay_selected = select_triggered(replay_scored[replay_scored["trade_date"] == date].copy(), base_param).sort_values("parameter_bottom_score", ascending=False).head(int(run_policy["top_n"]))
        replay_codes = "|".join(replay_selected["industry_code"].astype(str).str.zfill(6).tolist()) if not replay_selected.empty else ""
        rows.append(
            {
                "trade_date": date_to_str(date),
                "parameter_id": base_param["parameter_id"],
                "status": "pass" if full_codes == replay_codes else "fail",
                "full_panel_codes": full_codes,
                "asof_replay_codes": replay_codes,
                "evidence": "full build and truncated as-of rebuild match" if full_codes == replay_codes else "selection mismatch",
            }
        )
    return pd.DataFrame(rows)


def build_run_summary(
    *,
    policy: dict[str, Any],
    signal_panel: pd.DataFrame,
    parameter_grid: pd.DataFrame,
    decision_log: pd.DataFrame,
    event_returns: pd.DataFrame,
    parameter_summary: pd.DataFrame,
    cash_sensitivity: pd.DataFrame,
    timestamp_audit: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    replay_audit: pd.DataFrame,
    source_audit: pd.DataFrame,
) -> dict[str, Any]:
    audit_fail_count = 0
    for frame in [timestamp_audit, leakage_audit, replay_audit, source_audit]:
        if not frame.empty and "status" in frame.columns:
            audit_fail_count += int((frame["status"] == "fail").sum())
    primary = parameter_summary[parameter_summary["horizon"] == int(policy["primary_horizon"])].copy()
    candidates = primary[primary["signal_status"] == "候选待源审计"].copy()
    best = primary.sort_values("robust_score", ascending=False).iloc[0].to_dict() if not primary.empty else {}
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "signal_rows": int(len(signal_panel)),
        "signal_start": date_to_str(signal_panel["trade_date"].min()) if not signal_panel.empty else "",
        "signal_end": date_to_str(signal_panel["trade_date"].max()) if not signal_panel.empty else "",
        "parameter_count": int(len(parameter_grid)),
        "invested_parameter_count": int(decision_log.groupby("parameter_id")["is_invested"].sum().gt(0).sum()) if not decision_log.empty else 0,
        "event_rows": int(len(event_returns)),
        "candidate_requires_source_audit_count": int(len(candidates)),
        "conditional_observation_count": int((primary["signal_status"] == "条件观察").sum()) if not primary.empty else 0,
        "rejected_count": int((primary["signal_status"] == "拒绝").sum()) if not primary.empty else 0,
        "sample_limited_count": int((primary["signal_status"] == "样本不足").sum()) if not primary.empty else 0,
        "audit_fail_count": int(audit_fail_count),
        "best_parameter_id": best.get("parameter_id", ""),
        "best_60d_mean_relative_return": float(best.get("mean_relative_return", math.nan)),
        "best_60d_oos_relative_return": float(best.get("oos_mean_relative_return", math.nan)),
        "best_60d_nonoverlap_relative_return": float(best.get("nonoverlap_mean_relative_return", math.nan)),
        "best_active_relative_final_nav": float(best.get("active_relative_final_nav", math.nan)),
        "final_verdict": final_verdict(candidates, primary, audit_fail_count),
        "research_boundary": "只研究申万行业和行业指数；V2.10只验证压力门控和参数敏感性，不生成交易指令。",
    }


def final_verdict(candidates: pd.DataFrame, primary: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；无参数组合通过候选门槛"
    stable = bool((primary["mean_relative_return"] > 0).mean() >= 0.60) if not primary.empty else False
    if stable:
        return "research_only；存在候选组合但仍需源审计和独立复核"
    return "research_only；存在局部候选但参数邻域不稳定，不能升级alpha"


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    pressure_gate_effect: pd.DataFrame,
    momentum_trap_effect: pd.DataFrame,
    weight_sensitivity: pd.DataFrame,
    cash_sensitivity: pd.DataFrame,
    timestamp_audit: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    replay_audit: pd.DataFrame,
) -> str:
    lines: list[str] = []
    lines.append("# V2.10 压力门控实时仿真与参数敏感性报告")
    lines.append("")
    lines.append(f"版本：{VERSION}")
    lines.append("")
    lines.append("## 研究结论")
    lines.append("")
    lines.append("V2.10 在 V2.9 实时仿真基础上，固定检验显式市场压力门槛、动量陷阱阈值和抄底分权重扰动。")
    lines.append("本版本不是调参优化，而是检查 V2.9 的负相对收益是否能被压力门控和参数邻域稳定性修复。")
    lines.append("")
    lines.append(f"- 信号区间：{summary['signal_start']} 至 {summary['signal_end']}")
    lines.append(f"- 参数组合数：{summary['parameter_count']}")
    lines.append(f"- 有持仓参数组合数：{summary['invested_parameter_count']}")
    lines.append(f"- 事件行数：{summary['event_rows']}")
    lines.append(f"- 候选待源审计组合数：{summary['candidate_requires_source_audit_count']}")
    lines.append(f"- 条件观察组合数：{summary['conditional_observation_count']}")
    lines.append(f"- 审计失败数：{summary['audit_fail_count']}")
    lines.append(f"- 最终结论：{summary['final_verdict']}")
    lines.append("")
    lines.append("## 60日参数排序")
    lines.append("")
    lines.extend(table_or_empty(top_candidates.head(15), {
        "parameter_id": "参数ID",
        "pressure_gate_zh": "压力门槛",
        "market_stress_min": "压力阈值",
        "momentum_trap_max": "陷阱上限",
        "weight_variant_zh": "权重扰动",
        "signal_status": "结论",
        "samples": "样本",
        "nonoverlap_samples": "非重叠",
        "oos_samples": "样本外",
        "mean_net_return": "策略收益",
        "mean_benchmark_return": "基准收益",
        "mean_relative_return": "相对收益",
        "oos_mean_relative_return": "样本外相对",
        "nonoverlap_mean_relative_return": "非重叠相对",
        "benchmark_win_rate": "跑赢比例",
        "active_relative_final_nav": "持仓相对净值",
        "robust_score": "稳健分",
    }, {"mean_net_return", "mean_benchmark_return", "mean_relative_return", "oos_mean_relative_return", "nonoverlap_mean_relative_return", "benchmark_win_rate"}))
    lines.append("")
    lines.append("## 压力门槛效果")
    lines.append("")
    lines.extend(table_or_empty(pressure_gate_effect, {
        "pressure_gate_id": "压力ID",
        "pressure_gate_zh": "压力门槛",
        "parameter_count": "参数数",
        "candidate_count": "候选数",
        "conditional_count": "观察数",
        "mean_samples": "平均样本",
        "mean_relative_return": "平均相对",
        "best_relative_return": "最佳相对",
        "mean_oos_relative_return": "平均样本外相对",
        "mean_nonoverlap_relative_return": "平均非重叠相对",
        "mean_active_relative_nav": "平均持仓相对净值",
    }, {"mean_relative_return", "best_relative_return", "mean_oos_relative_return", "mean_nonoverlap_relative_return"}))
    lines.append("")
    lines.append("## 动量陷阱阈值")
    lines.append("")
    lines.extend(table_or_empty(momentum_trap_effect, {
        "momentum_trap_max": "陷阱上限",
        "parameter_count": "参数数",
        "candidate_count": "候选数",
        "conditional_count": "观察数",
        "mean_samples": "平均样本",
        "mean_relative_return": "平均相对",
        "best_relative_return": "最佳相对",
        "mean_oos_relative_return": "平均样本外相对",
        "mean_active_relative_nav": "平均持仓相对净值",
    }, {"mean_relative_return", "best_relative_return", "mean_oos_relative_return"}))
    lines.append("")
    lines.append("## 权重敏感性")
    lines.append("")
    lines.extend(table_or_empty(weight_sensitivity, {
        "weight_variant": "权重ID",
        "weight_variant_zh": "权重扰动",
        "parameter_count": "参数数",
        "candidate_count": "候选数",
        "conditional_count": "观察数",
        "mean_samples": "平均样本",
        "mean_relative_return": "平均相对",
        "best_relative_return": "最佳相对",
        "mean_oos_relative_return": "平均样本外相对",
        "mean_active_relative_nav": "平均持仓相对净值",
    }, {"mean_relative_return", "best_relative_return", "mean_oos_relative_return"}))
    lines.append("")
    lines.append("## 现金收益敏感性")
    lines.append("")
    best_param = summary.get("best_parameter_id", "")
    cash_display = cash_sensitivity[cash_sensitivity["parameter_id"] == best_param].copy() if not cash_sensitivity.empty else pd.DataFrame()
    lines.extend(table_or_empty(cash_display, {
        "cash_return_zh": "现金假设",
        "final_nav": "策略净值",
        "benchmark_final_nav": "基准净值",
        "relative_final_nav": "相对净值",
    }, set()))
    lines.append("")
    lines.append("## 审计")
    lines.append("")
    audit = pd.concat(
        [
            timestamp_audit.assign(audit_group="时间戳"),
            leakage_audit.assign(audit_group="泄漏"),
            replay_audit.assign(audit_group="回放一致性") if not replay_audit.empty else pd.DataFrame(),
        ],
        ignore_index=True,
        sort=False,
    )
    lines.extend(table_or_empty(audit[[col for col in ["audit_group", "audit_item", "trade_date", "status", "evidence", "action"] if col in audit.columns]].head(20), {
        "audit_group": "审计组",
        "audit_item": "项目",
        "trade_date": "日期",
        "status": "状态",
        "evidence": "证据",
        "action": "动作",
    }, set()))
    lines.append("")
    lines.append("## 输出文件说明")
    lines.append("")
    lines.append("- `report.md`：中文压力门控和参数敏感性报告，优先打开。")
    lines.append("- `top_candidates.csv`：60日主检验下排序靠前的参数组合。")
    lines.append("- `run_summary.json`：机器可读运行摘要。")
    lines.append("- `debug/`：完整参数网格、决策日志、交易流水、事件收益、逐日净值、归因和审计文件。")
    lines.append("")
    lines.append("研究边界：本报告只研究申万行业和行业指数，不做个股筛选，不生成交易指令。")
    return "\n".join(lines)


def add_param_columns(frame: pd.DataFrame, param: dict[str, Any]) -> pd.DataFrame:
    result = frame.copy()
    for key in ["parameter_id", "pressure_gate_id", "pressure_gate_zh", "market_stress_min", "momentum_trap_max", "weight_variant", "weight_variant_zh"]:
        result[key] = param.get(key, "")
    return result


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


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


def mean_col(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame.columns:
        return math.nan
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    return float(values.mean()) if not values.empty else math.nan


def date_to_str(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)


def fmt_float(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(number):
        return ""
    return f"{number:.{digits}f}"


def fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
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
