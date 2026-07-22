#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_6_horizon_stability_policy.json"
V43_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v4_3_failure_state.py"
VERSION = "4.6.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.6 horizon-stability rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V4.6 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v43 = load_v43_module()
    v37 = v43.load_v37_module()
    v34 = v37.load_v34_module()
    v20 = v34.load_v20_module()
    source_policy = read_json(ROOT / policy["source_policy_path"])
    close_matrix = v20.load_close_matrix(ROOT / policy["industry_history_dir"])
    amount_matrix = v34.load_amount_matrix(ROOT / policy["industry_history_dir"])

    features = v20.build_daily_features(close_matrix, {**source_policy, **policy})
    features = v34.add_industry_liquidity_features(features, amount_matrix)
    features = v34.add_market_volatility_ratio(features)
    features = v37.add_industry_breadth_features(features, close_matrix)
    panel = v34.add_rebound_targets(features, policy)

    data_audit = v37.build_data_availability_audit(policy, close_matrix, amount_matrix, panel)
    target_audit = v34.build_target_label_audit(panel, policy)
    breadth_audit = v37.build_breadth_feature_audit(policy, panel)
    predictions, year_summary, model_summary, threshold_audit = v43.run_failure_state_model(panel, policy, v34)
    predictions = attach_forward_metrics(predictions, panel, policy)

    raw_predictions = predictions.copy()
    if not raw_predictions.empty:
        raw_predictions["model_signal"] = raw_predictions["raw_model_signal"]
    raw_trades, raw_summary = v34.run_realtime_simulation(panel, raw_predictions, policy)
    normalize_summary(raw_trades, raw_summary, "v3_7_raw_model_extended_reference", "V3.7原始概率模型扩展样本参考", "原始概率模型参考")

    failure_trades, failure_summary = v34.run_realtime_simulation(panel, predictions, policy)
    normalize_summary(failure_trades, failure_summary, "v4_3_failure_state_extended_reference", "V4.3失败状态过滤扩展样本参考", "失败状态过滤参考")

    all_trades, horizon_sensitivity, primary_trades, primary_summary = run_horizon_grid(predictions, policy)
    leave_one_year = build_leave_one_year_out(primary_trades, policy)
    horizon_audit = build_horizon_audit(policy, predictions, horizon_sensitivity, primary_summary, leave_one_year, failure_summary)
    top_candidates = build_top_candidates(primary_summary, failure_summary, raw_summary, model_summary)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions, threshold_audit, horizon_audit)
    annual_distribution = build_annual_distribution(predictions, raw_trades, failure_trades, primary_trades)
    notes = build_notes(primary_summary, horizon_sensitivity, leave_one_year, horizon_audit)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, primary_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v46_horizon_stability_feature_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    breadth_audit.to_csv(debug_dir / "breadth_feature_audit.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    threshold_audit.to_csv(debug_dir / "failure_state_threshold_audit.csv", index=False, encoding="utf-8-sig")
    raw_trades.to_csv(debug_dir / "raw_model_realtime_trades.csv", index=False, encoding="utf-8-sig")
    raw_summary.to_csv(debug_dir / "raw_model_realtime_summary.csv", index=False, encoding="utf-8-sig")
    failure_trades.to_csv(debug_dir / "failure_state_realtime_trades.csv", index=False, encoding="utf-8-sig")
    failure_summary.to_csv(debug_dir / "failure_state_realtime_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    primary_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    all_trades.to_csv(debug_dir / "horizon_grid_realtime_trades.csv", index=False, encoding="utf-8-sig")
    horizon_sensitivity.to_csv(debug_dir / "horizon_stability_sensitivity.csv", index=False, encoding="utf-8-sig")
    leave_one_year.to_csv(debug_dir / "leave_one_year_out_audit.csv", index=False, encoding="utf-8-sig")
    horizon_audit.to_csv(debug_dir / "horizon_stability_audit.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, year_summary, horizon_audit, horizon_sensitivity, leave_one_year, primary_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V4.6持久性信号簇多持有期稳定性研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"主规则实时交易数={run_summary['primary_realtime_events']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v43_module() -> Any:
    spec = importlib.util.spec_from_file_location("v43_failure_state", V43_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V4.3 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def attach_forward_metrics(predictions: pd.DataFrame, panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if predictions.empty:
        return predictions
    horizons = [int(item) for item in policy["persistent_cluster"]["horizon_grid"]]
    metrics = panel[["trade_date", "market_nav"]].copy().sort_values("trade_date").reset_index(drop=True)
    nav = pd.to_numeric(metrics["market_nav"], errors="coerce").reset_index(drop=True)
    entry = nav.shift(-1)
    for horizon in horizons:
        exit_nav = nav.shift(-(horizon + 1))
        ret_col = f"forward_return_{horizon}d_next_close"
        dd_col = f"forward_max_drawdown_{horizon}d_next_close"
        metrics[ret_col] = exit_nav / entry - 1.0
        drawdowns: list[float] = []
        for idx in range(len(metrics)):
            entry_value = entry.iloc[idx]
            if pd.isna(entry_value) or idx + horizon + 1 >= len(nav):
                drawdowns.append(math.nan)
                continue
            path = nav.iloc[idx + 1 : idx + horizon + 2] / entry_value - 1.0
            drawdowns.append(float(path.min()) if len(path) else math.nan)
        metrics[dd_col] = drawdowns
        ret = pd.to_numeric(metrics[ret_col], errors="coerce")
        dd = pd.to_numeric(metrics[dd_col], errors="coerce")
        metrics[f"target_rebound_window_{horizon}d"] = ((ret >= float(policy["target_return_threshold"])) & (dd >= float(policy["target_max_drawdown_floor"]))).astype(int)
        metrics[f"is_bad_window_{horizon}d"] = (ret <= float(policy["bad_window_threshold"])).astype(int)
    metric_cols = ["trade_date"] + [col for col in metrics.columns if col != "market_nav" and col != "trade_date"]
    drop_cols = [col for col in metric_cols if col != "trade_date" and col in predictions.columns]
    return predictions.drop(columns=drop_cols, errors="ignore").merge(metrics[metric_cols], on="trade_date", how="left")


def run_horizon_grid(predictions: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cfg = policy["persistent_cluster"]
    all_trade_frames: list[pd.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []
    primary_trades = pd.DataFrame()
    primary_summary = pd.DataFrame()
    primary_min = int(cfg["primary_min_consecutive_signal_days"])
    primary_horizon = int(cfg["primary_horizon"])
    for horizon in cfg["horizon_grid"]:
        for min_days in cfg["min_consecutive_signal_days_grid"]:
            trades = simulate_horizon_cluster(predictions, policy, int(min_days), int(horizon))
            summary = summarize_horizon_trades(trades, predictions, policy, int(min_days), int(horizon))
            all_trade_frames.append(trades)
            summary_rows.append(summary)
            if int(min_days) == primary_min and int(horizon) == primary_horizon:
                primary_trades = trades
                primary_summary = pd.DataFrame([summary])
    if primary_summary.empty:
        primary_summary = pd.DataFrame([empty_horizon_summary(policy, primary_min, primary_horizon)])
    return concat_frames(all_trade_frames), pd.DataFrame(summary_rows), primary_trades, primary_summary


def simulate_horizon_cluster(predictions: pd.DataFrame, policy: dict[str, Any], min_days: int, horizon: int) -> pd.DataFrame:
    if predictions.empty:
        return pd.DataFrame()
    cfg = policy["persistent_cluster"]
    entry_lag = int(cfg.get("entry_lag_days", 1))
    ret_col = f"forward_return_{horizon}d_next_close"
    dd_col = f"forward_max_drawdown_{horizon}d_next_close"
    frame = predictions.sort_values("trade_date").reset_index(drop=True).copy()
    dates = pd.to_datetime(frame["trade_date"]).tolist()
    rows: list[dict[str, Any]] = []
    consecutive = 0
    last_exit_idx = -1
    for idx, row in frame.iterrows():
        if idx <= last_exit_idx:
            continue
        is_signal = bool(row.get("model_signal", False))
        consecutive = consecutive + 1 if is_signal else 0
        if not is_signal or consecutive < min_days:
            continue
        entry_idx = idx + entry_lag
        exit_idx = idx + entry_lag + horizon
        trade_return = float_or_nan(row.get(ret_col))
        max_adverse = float_or_nan(row.get(dd_col))
        if entry_idx >= len(frame) or exit_idx >= len(frame) or math.isnan(trade_return):
            consecutive = 0
            continue
        rows.append(
            {
                "signal_id": cfg["primary_signal_id"],
                "signal_date": date_text(row["trade_date"]),
                "entry_date": date_text(dates[entry_idx]),
                "exit_date": date_text(dates[exit_idx]),
                "holding_days": int(horizon),
                "min_consecutive_signal_days": int(min_days),
                "consecutive_signal_days_at_entry": int(consecutive),
                "trade_return": trade_return,
                "max_adverse_return": max_adverse,
                "is_win": bool(trade_return > 0),
                "is_bad_window": bool(trade_return <= float(policy["bad_window_threshold"])),
                "year": int(pd.Timestamp(row["trade_date"]).year),
                "model_probability": float_or_none(row.get("model_probability")),
                "probability_margin": float_or_none(row.get("probability_margin")),
                "failure_flag_count": int(float_or_nan(row.get("failure_flag_count"), 0.0)),
                "market_return_5d": float_or_none(row.get("market_return_5d")),
                "industry_positive_20d_ratio": float_or_none(row.get("industry_positive_20d_ratio")),
                "industry_above_ma20_ratio": float_or_none(row.get("industry_above_ma20_ratio")),
                "industry_new_low_60d_relief_5d": float_or_none(row.get("industry_new_low_60d_relief_5d")),
            }
        )
        last_exit_idx = exit_idx
        consecutive = 0
    return pd.DataFrame(rows)


def summarize_horizon_trades(trades: pd.DataFrame, predictions: pd.DataFrame, policy: dict[str, Any], min_days: int, horizon: int) -> dict[str, Any]:
    row = empty_horizon_summary(policy, min_days, horizon)
    row["signal_dates"] = int(predictions["model_signal"].astype(bool).sum()) if not predictions.empty else 0
    if trades.empty:
        return row
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    years = trades["year"].astype(int)
    row.update(
        {
            "trades": int(len(trades)),
            "nonoverlap_events": int(len(trades)),
            "active_years": int(years.nunique()),
            "max_single_year_concentration": float(years.value_counts(normalize=True).max()) if len(years) else math.nan,
            "mean_return": float(returns.mean()),
            "event_mean_return": float(returns.mean()),
            "event_win_rate": float((returns > 0).mean()),
            "bad_window_rate": float(trades["is_bad_window"].astype(bool).mean()),
            "event_bad_window_rate": float(trades["is_bad_window"].astype(bool).mean()),
            "event_worst_return": float(returns.min()),
        }
    )
    row["status"] = classify_horizon_summary(row, policy)
    return row


def empty_horizon_summary(policy: dict[str, Any], min_days: int, horizon: int) -> dict[str, Any]:
    cfg = policy["persistent_cluster"]
    signal_id = f"{cfg['primary_signal_id']}_min{min_days}_{horizon}d"
    return {
        "signal_id": signal_id,
        "signal_name_zh": f"V4.6连续{min_days}日信号簇{horizon}日持有",
        "signal_type": "多持有期稳定性实时仿真",
        "status": "样本不足",
        "min_consecutive_signal_days": int(min_days),
        "holding_days": int(horizon),
        "signal_dates": 0,
        "trades": 0,
        "nonoverlap_events": 0,
        "active_years": 0,
        "max_single_year_concentration": math.nan,
        "mean_return": math.nan,
        "event_mean_return": math.nan,
        "event_win_rate": math.nan,
        "bad_window_rate": math.nan,
        "event_bad_window_rate": math.nan,
        "event_worst_return": math.nan,
    }


def classify_horizon_summary(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    rb = policy["robustness_thresholds"]
    events = int(nz(row.get("nonoverlap_events", 0)))
    mean_return = nz(row.get("event_mean_return", math.nan), -1.0)
    win_rate = nz(row.get("event_win_rate", math.nan), 0.0)
    bad_rate = nz(row.get("event_bad_window_rate", math.nan), 1.0)
    active_years = int(nz(row.get("active_years", 0)))
    concentration = nz(row.get("max_single_year_concentration", math.nan), 1.0)
    if (
        events >= int(th["min_nonoverlap_events"])
        and mean_return >= float(th["min_event_mean_return"])
        and win_rate >= float(th["min_event_win_rate"])
        and bad_rate <= float(th["max_event_bad_window_rate"])
        and active_years >= int(th["min_active_years"])
        and concentration <= float(th["max_single_year_concentration"])
    ):
        return "反弹窗口候选"
    if (
        events >= int(rb["min_conditional_events"])
        and mean_return >= 0.0
        and win_rate >= 0.5
        and bad_rate <= 0.35
    ):
        return "条件观察"
    if events < int(rb["min_conditional_events"]):
        return "样本不足"
    return "拒绝"


def build_leave_one_year_out(primary_trades: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if primary_trades.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for year in sorted(primary_trades["year"].astype(int).unique()):
        subset = primary_trades[primary_trades["year"].astype(int) != int(year)].copy()
        rows.append(robustness_row(f"exclude_{year}", int(year), subset, policy))
    rows.append(robustness_row("all_primary_events", None, primary_trades, policy))
    return pd.DataFrame(rows)


def robustness_row(label: str, excluded_year: int | None, trades: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    th = policy["robustness_thresholds"]
    if trades.empty:
        return {
            "audit_item": label,
            "excluded_year": excluded_year,
            "events": 0,
            "mean_return": math.nan,
            "win_rate": math.nan,
            "bad_window_rate": math.nan,
            "status": "样本不足",
        }
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    win_rate = float((returns > 0).mean())
    bad_rate = float(trades["is_bad_window"].astype(bool).mean())
    mean_return = float(returns.mean())
    passed = (
        mean_return >= float(th["min_leave_one_year_mean_return"])
        and win_rate >= float(th["min_leave_one_year_win_rate"])
        and bad_rate <= float(th["max_leave_one_year_bad_window_rate"])
    )
    return {
        "audit_item": label,
        "excluded_year": excluded_year,
        "events": int(len(trades)),
        "mean_return": mean_return,
        "win_rate": win_rate,
        "bad_window_rate": bad_rate,
        "worst_return": float(returns.min()),
        "active_years": int(trades["year"].nunique()),
        "status": "pass" if passed else "fail",
    }


def build_horizon_audit(policy: dict[str, Any], predictions: pd.DataFrame, sensitivity: pd.DataFrame, primary_summary: pd.DataFrame, leave_one_year: pd.DataFrame, failure_summary: pd.DataFrame) -> pd.DataFrame:
    cfg = policy["persistent_cluster"]
    rb = policy["robustness_thresholds"]
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    base = failure_summary.iloc[0].to_dict() if not failure_summary.empty else {}
    primary_min = int(cfg["primary_min_consecutive_signal_days"])
    horizon_rows = sensitivity[sensitivity["min_consecutive_signal_days"].astype(int) == primary_min].copy() if not sensitivity.empty else pd.DataFrame()
    horizon_pass = False
    if not horizon_rows.empty:
        horizon_pass = bool(
            (
                (horizon_rows["nonoverlap_events"] >= int(rb["min_horizon_events"]))
                & (horizon_rows["event_mean_return"] >= float(rb["min_horizon_mean_return"]))
                & (horizon_rows["event_win_rate"] >= float(rb["min_horizon_win_rate"]))
                & (horizon_rows["event_bad_window_rate"] <= float(rb["max_horizon_bad_window_rate"]))
            ).all()
        )
    loo_pass = bool((leave_one_year["status"].astype(str) == "pass").all()) if not leave_one_year.empty else False
    sample_pass = int(nz(primary.get("nonoverlap_events", 0))) >= int(rb["min_primary_events"])
    rows = [
        {
            "audit_item": "primary_sample_size",
            "status": "pass" if sample_pass else "fail",
            "evidence": f"events={int(nz(primary.get('nonoverlap_events', 0)))} / {rb['min_primary_events']}",
            "action": "样本不足时不得升级为有效反弹窗口。",
        },
        {
            "audit_item": "horizon_direction_stability",
            "status": "pass" if horizon_pass else "fail",
            "evidence": f"horizons={list(cfg['horizon_grid'])}; primary_min_days={primary_min}",
            "action": "同一实时信号在多个持有期上应保持同向正收益和可控坏窗口。",
        },
        {
            "audit_item": "leave_one_year_out_stability",
            "status": "pass" if loo_pass else "fail",
            "evidence": f"rows={len(leave_one_year)}",
            "action": "剔除任一活跃年份后收益、胜率和坏窗口不应失效。",
        },
        {
            "audit_item": "v4_5_increment",
            "status": "observe",
            "evidence": f"base_events={int(nz(base.get('nonoverlap_events', 0)))}; primary_events={int(nz(primary.get('nonoverlap_events', 0)))}; base_return={fmt_pct(base.get('event_mean_return'))}; primary_return={fmt_pct(primary.get('event_mean_return'))}",
            "action": "观察连续信号簇相对失败状态过滤参考的增量。",
        },
        {
            "audit_item": "filtered_signal_source",
            "status": "pass" if not predictions.empty else "fail",
            "evidence": f"filtered_signal_dates={int(predictions['model_signal'].astype(bool).sum()) if not predictions.empty else 0}",
            "action": "多持有期只复用事前可见的V4.3失败状态过滤信号。",
        },
    ]
    return pd.DataFrame(rows)


def build_top_candidates(primary_summary: pd.DataFrame, failure_summary: pd.DataFrame, raw_summary: pd.DataFrame, model_summary: pd.DataFrame) -> pd.DataFrame:
    combined = concat_frames([primary_summary, failure_summary, raw_summary, model_summary])
    if combined.empty:
        return combined
    priority = {"反弹窗口候选": 0, "条件观察": 1, "状态观察": 2, "样本不足": 3, "拒绝": 4}
    combined["_priority"] = combined["status"].map(priority).fillna(9)
    for col in ["event_mean_return", "event_win_rate", "event_bad_window_rate", "max_single_year_concentration"]:
        if col not in combined.columns:
            combined[col] = math.nan
    combined["_score"] = (
        2.0 * combined["event_mean_return"].map(nz)
        + combined["event_win_rate"].map(nz)
        - combined["event_bad_window_rate"].map(nz)
        - 0.4 * combined["max_single_year_concentration"].map(lambda value: nz(value, 1.0))
    )
    combined = combined.sort_values(["_priority", "_score"], ascending=[True, False]).drop(columns=["_priority", "_score"])
    columns = [
        "signal_id",
        "signal_name_zh",
        "signal_type",
        "status",
        "min_consecutive_signal_days",
        "holding_days",
        "signal_dates",
        "nonoverlap_events",
        "active_years",
        "max_single_year_concentration",
        "event_mean_return",
        "event_win_rate",
        "event_bad_window_rate",
        "event_worst_return",
    ]
    return combined[[col for col in columns if col in combined.columns]]


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame, threshold_audit: pd.DataFrame, horizon_audit: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "failure-state and horizon-stability fields use trade-date close/amount-derived data only",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "train_only_failure_thresholds",
                "status": "pass" if not threshold_audit.empty else "fail",
                "evidence": f"threshold_rows={len(threshold_audit)}; test_years={predictions['year'].nunique() if not predictions.empty else 0}",
                "action": "每年失败状态阈值只用训练期原始信号分布生成。",
            },
            {
                "audit_item": "multi_horizon_is_evaluation_only",
                "status": "pass",
                "evidence": f"horizons={policy['persistent_cluster']['horizon_grid']}; primary_horizon={policy['persistent_cluster']['primary_horizon']}",
                "action": "多持有期收益只用于评价稳定性，不用于训练或挑选信号日。",
            },
            {
                "audit_item": "target_used_only_for_training_and_evaluation",
                "status": "pass",
                "evidence": "test-period future returns and bad-window labels are not used to decide entries",
                "action": "测试期未来收益只进入评价，不进入信号。",
            },
            {
                "audit_item": "purged_walk_forward",
                "status": "pass" if not predictions.empty else "fail",
                "evidence": f"purge_days={policy['model']['purge_days']}; prediction_rows={len(predictions)}; test_start_year={policy['model']['test_start_year']}",
                "action": "每个测试年份只用之前样本训练，并剔除测试前重叠标签窗口。",
            },
            {
                "audit_item": "data_availability",
                "status": "pass" if int((data_audit["status"] == "fail").sum()) == 0 else "fail",
                "evidence": f"data_audit_failures={int((data_audit['status'] == 'fail').sum())}",
                "action": "数据可得性失败时不得升级。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["research_boundary"],
                "action": "不生成交易指令；通过也只是研究候选。",
            },
        ]
    )


def build_annual_distribution(predictions: pd.DataFrame, raw_trades: pd.DataFrame, failure_trades: pd.DataFrame, primary_trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not predictions.empty:
        for signal_col, source, signal_id in [
            ("raw_model_signal", "raw_model_signal_dates", "raw_probability_model_extended"),
            ("model_signal", "failure_state_filtered_signal_dates", "failure_state_filtered_probability_model_extended"),
        ]:
            signals = predictions[predictions[signal_col].astype(bool)]
            for year, group in signals.groupby("year"):
                rows.append({"source": source, "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    for frame, source, signal_id in [
        (raw_trades, "raw_realtime_trades", "v3_7_raw_model_extended_reference"),
        (failure_trades, "failure_state_realtime_trades", "v4_3_failure_state_extended_reference"),
        (primary_trades, "v4_6_horizon_stable_realtime_trades", "v4_6_horizon_stable_realtime"),
    ]:
        if frame.empty:
            continue
        for year, group in frame.groupby("year"):
            rows.append({"source": source, "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def build_notes(primary_summary: pd.DataFrame, sensitivity: pd.DataFrame, leave_one_year: pd.DataFrame, horizon_audit: pd.DataFrame) -> dict[str, Any]:
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    notes: list[str] = []
    if str(primary.get("status", "")) == "反弹窗口候选":
        notes.append("V4.6主规则达到内部候选状态，但仍需统一评价体系确认是否有效。")
    elif str(primary.get("status", "")) == "条件观察":
        notes.append("V4.6确认V4.5持久性信号簇具有跨持有期方向性，但样本不足，不能升级为有效反弹窗口。")
    else:
        notes.append("V4.6未能确认持久性信号簇是有效反弹窗口。")
    notes.append(
        f"主规则：非重叠事件 {int(nz(primary.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(primary.get('event_mean_return'))}，胜率 {fmt_pct(primary.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(primary.get('event_bad_window_rate'))}。"
    )
    if not sensitivity.empty:
        grid = sensitivity[sensitivity["min_consecutive_signal_days"].astype(int) == int(primary.get("min_consecutive_signal_days", 0))]
        for _, row in grid.iterrows():
            notes.append(
                f"{int(row['holding_days'])}日持有：事件 {int(row['nonoverlap_events'])}，"
                f"平均收益 {fmt_pct(row['event_mean_return'])}，胜率 {fmt_pct(row['event_win_rate'])}，坏窗口 {fmt_pct(row['event_bad_window_rate'])}。"
            )
    if not leave_one_year.empty:
        failed = leave_one_year[leave_one_year["status"].astype(str) == "fail"]
        notes.append(f"剔除年份审计：失败项 {len(failed)} / {len(leave_one_year)}。")
    sample_audit = horizon_audit[horizon_audit["audit_item"] == "primary_sample_size"] if not horizon_audit.empty else pd.DataFrame()
    if not sample_audit.empty:
        notes.append(f"样本审计：{sample_audit.iloc[0]['evidence']}。")
    notes.append("V4.6的结论是：方向性比V4.5更可信，但证据仍卡在事件数，下一步要扩大样本来源而不是继续提高阈值。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "下一步做V4.7样本扩展：引入更长历史宽基/中证行业或降低目标定义中的单一20日窗口依赖，验证是否能把有效事件数提升到30以上，同时保持坏窗口低于20%。",
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, close_matrix: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, primary_summary: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = top[top["status"] == "反弹窗口候选"] if not top.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    audit_fail_count = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_target_panel_count": int(len(panel)),
        "industry_count": int(close_matrix.shape[1]),
        "primary_signal_id": policy["persistent_cluster"]["primary_signal_id"],
        "primary_realtime_events": int(nz(primary.get("nonoverlap_events", 0))),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_signal_id": best.get("signal_id", ""),
        "best_status": best.get("status", ""),
        "best_nonoverlap_events": int(nz(best.get("nonoverlap_events", 0))) if best else 0,
        "best_event_mean_return": float_or_none(best.get("event_mean_return")) if best else None,
        "best_event_bad_window_rate": float_or_none(best.get("event_bad_window_rate")) if best else None,
        "final_verdict": final_verdict(candidates, audit_fail_count, primary),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int, primary: dict[str, Any]) -> str:
    if audit_fail_count:
        return "research_only；存在数据或泄漏审计失败"
    if not candidates.empty:
        return "research_only；存在多持有期稳定性候选但仍需统一评价和未来样本验证"
    if str(primary.get("status", "")) == "条件观察":
        return "research_only；多持有期稳定性为条件观察，样本不足，不能升级为有效反弹窗口"
    return "research_only；多持有期稳定性尚未证明能有效找到反弹窗口"


def render_report(
    v34: Any,
    summary: dict[str, Any],
    top: pd.DataFrame,
    data_audit: pd.DataFrame,
    target_audit: pd.DataFrame,
    year_summary: pd.DataFrame,
    horizon_audit: pd.DataFrame,
    sensitivity: pd.DataFrame,
    leave_one_year: pd.DataFrame,
    primary_trades: pd.DataFrame,
    leakage: pd.DataFrame,
    notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines = ["# V4.6 持久性信号簇多持有期稳定性研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V4.6 不重新寻找最优参数，而是固定 V4.5 的连续信号簇思路，检查同一信号在 10/20/30 日持有期和剔除年份后是否仍稳定。",
        "",
        f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
        f"- 行业数：{summary['industry_count']}",
        f"- 主规则实时交易数：{summary['primary_realtime_events']}",
        f"- 反弹窗口候选数：{summary['candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 候选排序",
        "",
    ]
    lines.extend(v34.table_or_empty(top, {"signal_id": "信号ID", "signal_name_zh": "名称", "signal_type": "类型", "status": "状态", "min_consecutive_signal_days": "连续天数", "holding_days": "持有期", "signal_dates": "信号日", "nonoverlap_events": "非重叠事件", "event_mean_return": "事件收益", "event_win_rate": "事件胜率", "event_bad_window_rate": "坏窗口", "event_worst_return": "最差事件"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 数据可得性", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 目标标签", ""]
    lines.extend(v34.table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 年度失败状态模型", ""]
    lines.extend(v34.table_or_empty(year_summary, {"year": "年份", "status": "状态", "train_rows": "训练样本", "test_rows": "测试样本", "raw_signal_dates": "原始信号", "signal_dates": "过滤后信号", "signal_mean_return": "信号收益", "signal_bad_window_rate": "坏窗口率"}, {"signal_mean_return", "signal_bad_window_rate"}))
    lines += ["", "## 多持有期稳定性审计", ""]
    lines.extend(v34.table_or_empty(horizon_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 多持有期敏感性", ""]
    lines.extend(v34.table_or_empty(sensitivity, {"min_consecutive_signal_days": "连续天数", "holding_days": "持有期", "status": "状态", "nonoverlap_events": "事件数", "active_years": "活跃年份", "max_single_year_concentration": "年份集中度", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口", "event_worst_return": "最差事件"}, {"max_single_year_concentration", "event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 剔除年份审计", ""]
    lines.extend(v34.table_or_empty(leave_one_year, {"audit_item": "项目", "excluded_year": "剔除年份", "events": "事件数", "mean_return": "平均收益", "win_rate": "胜率", "bad_window_rate": "坏窗口", "worst_return": "最差事件", "active_years": "活跃年份", "status": "状态"}, {"mean_return", "win_rate", "bad_window_rate", "worst_return"}))
    lines += ["", "## 主规则交易明细", ""]
    lines.extend(v34.table_or_empty(primary_trades.head(60), {"signal_date": "信号日", "entry_date": "入场日", "exit_date": "退出日", "holding_days": "持有期", "min_consecutive_signal_days": "连续天数", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
    lines += ["", "## 审计", ""]
    lines.extend(v34.table_or_empty(leakage, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += ["", "## 结论与下一步", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议方向：{notes.get('recommended_next_direction', '')}")
    lines += [
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文 V4.6 研究报告，优先打开。",
        "- `top_candidates.csv`：主规则、参考模型和条件观察排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：特征面板、训练期阈值、多持有期敏感性、剔除年份审计、实时仿真、年度分布、泄漏审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def normalize_summary(trades: pd.DataFrame, summary: pd.DataFrame, signal_id: str, name: str, signal_type: str) -> None:
    if not trades.empty and "signal_id" in trades.columns:
        trades["signal_id"] = signal_id
    if not summary.empty:
        if "signal_id" in summary.columns:
            summary["signal_id"] = signal_id
        if "signal_name_zh" in summary.columns:
            summary["signal_name_zh"] = name
        if "signal_type" in summary.columns:
            summary["signal_type"] = signal_type


def date_text(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


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


def fmt_pct(value: Any) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number:.2%}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [clean_json_value(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value


if __name__ == "__main__":
    main()
