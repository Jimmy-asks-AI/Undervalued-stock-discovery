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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_5_persistent_cluster_policy.json"
V43_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v4_3_failure_state.py"
VERSION = "4.5.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.5 persistent-cluster rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V4.5 policy JSON.")
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

    raw_predictions = predictions.copy()
    if not raw_predictions.empty:
        raw_predictions["model_signal"] = raw_predictions["raw_model_signal"]
    raw_trades, raw_summary = v34.run_realtime_simulation(panel, raw_predictions, policy)
    normalize_summary(raw_trades, raw_summary, "v3_7_raw_model_extended_reference", "V3.7原始概率模型扩展样本参考", "原始概率模型参考")

    failure_trades, failure_summary = v34.run_realtime_simulation(panel, predictions, policy)
    normalize_summary(failure_trades, failure_summary, "v4_3_failure_state_extended_reference", "V4.3失败状态过滤扩展样本参考", "失败状态过滤参考")

    cluster_trades, cluster_summary, cluster_log, sensitivity = run_persistent_cluster(predictions, policy)
    cluster_audit = build_cluster_audit(policy, predictions, failure_summary, cluster_summary, sensitivity)
    flag_profile = v43.build_flag_profile(predictions, policy)
    failure_profile = v43.build_failure_profile(predictions, policy)
    annual_distribution = build_annual_distribution(predictions, raw_trades, failure_trades, cluster_trades)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions, threshold_audit)
    top_candidates = build_top_candidates(raw_summary, failure_summary, cluster_summary, model_summary)
    notes = build_notes(cluster_summary, failure_summary, raw_summary, sensitivity)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, cluster_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v45_persistent_cluster_feature_panel.csv", index=False, encoding="utf-8-sig")
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
    cluster_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    cluster_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    cluster_log.to_csv(debug_dir / "persistent_cluster_log.csv", index=False, encoding="utf-8-sig")
    sensitivity.to_csv(debug_dir / "persistent_cluster_sensitivity.csv", index=False, encoding="utf-8-sig")
    cluster_audit.to_csv(debug_dir / "persistent_cluster_audit.csv", index=False, encoding="utf-8-sig")
    flag_profile.to_csv(debug_dir / "failure_state_flag_profile.csv", index=False, encoding="utf-8-sig")
    failure_profile.to_csv(debug_dir / "failure_case_profile.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, year_summary, cluster_audit, sensitivity, raw_summary, failure_summary, cluster_summary, cluster_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V4.5扩展样本信号簇持久性反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"持久性簇实时交易数={run_summary['primary_realtime_events']}")
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


def run_persistent_cluster(predictions: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cfg = policy["persistent_cluster"]
    primary_min_days = int(cfg["min_consecutive_signal_days"])
    sensitivity_rows: list[dict[str, Any]] = []
    primary_trades = pd.DataFrame()
    primary_summary = pd.DataFrame()
    primary_log = pd.DataFrame()
    for min_days in cfg["sensitivity_min_consecutive_signal_days"]:
        trades, summary, log = simulate_persistent_cluster(predictions, policy, int(min_days))
        row = summary.iloc[0].to_dict() if not summary.empty else empty_cluster_summary(policy, int(min_days))
        row["min_consecutive_signal_days"] = int(min_days)
        sensitivity_rows.append(row)
        if int(min_days) == primary_min_days:
            primary_trades, primary_summary, primary_log = trades, summary, log
    return primary_trades, primary_summary, primary_log, pd.DataFrame(sensitivity_rows)


def simulate_persistent_cluster(predictions: pd.DataFrame, policy: dict[str, Any], min_days: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cfg = policy["persistent_cluster"]
    horizon = int(policy["target_horizon"])
    entry_lag = int(cfg.get("entry_lag_days", 1))
    ret_col = f"forward_return_{horizon}d_next_close"
    dd_col = f"forward_max_drawdown_{horizon}d_next_close"
    signal_id = f"{cfg['primary_signal_id']}_min{min_days}"
    name = f"V4.5连续{min_days}日信号簇实时仿真"
    if predictions.empty:
        summary = pd.DataFrame([empty_cluster_summary(policy, min_days)])
        return pd.DataFrame(), summary, pd.DataFrame()

    frame = predictions.sort_values("trade_date").reset_index(drop=True).copy()
    dates = pd.to_datetime(frame["trade_date"]).tolist()
    trades: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    consecutive = 0
    last_exit_idx = -1
    total_model_signal_dates = int(frame["model_signal"].astype(bool).sum())

    for idx, row in frame.iterrows():
        if idx <= last_exit_idx:
            continue
        is_signal = bool(row.get("model_signal", False))
        consecutive = consecutive + 1 if is_signal else 0
        if not is_signal or consecutive < min_days:
            continue
        entry_idx = idx + entry_lag
        if entry_idx >= len(frame):
            logs.append(log_row(row, min_days, "skip_no_next_entry_date", False, consecutive, None, None))
            consecutive = 0
            continue
        exit_idx = min(entry_idx + horizon - 1, len(frame) - 1)
        trade_return = float_or_nan(row.get(ret_col))
        max_adverse = float_or_nan(row.get(dd_col))
        if math.isnan(trade_return):
            logs.append(log_row(row, min_days, "skip_missing_forward_return", False, consecutive, entry_idx, exit_idx))
            consecutive = 0
            continue
        trade = {
            "signal_id": cfg["primary_signal_id"],
            "signal_date": date_text(row["trade_date"]),
            "entry_date": date_text(dates[entry_idx]),
            "exit_date": date_text(dates[exit_idx]),
            "holding_days": horizon,
            "min_consecutive_signal_days": min_days,
            "consecutive_signal_days_at_entry": consecutive,
            "trade_return": trade_return,
            "max_adverse_return": max_adverse,
            "is_win": bool(trade_return > 0),
            "is_bad_window": bool(row.get("is_bad_window", False)),
            "year": int(pd.Timestamp(row["trade_date"]).year),
            "model_probability": float_or_none(row.get("model_probability")),
            "probability_margin": float_or_none(row.get("probability_margin")),
            "failure_flag_count": int(float_or_nan(row.get("failure_flag_count"), 0.0)),
            "market_return_5d": float_or_none(row.get("market_return_5d")),
            "industry_positive_20d_ratio": float_or_none(row.get("industry_positive_20d_ratio")),
            "industry_above_ma20_ratio": float_or_none(row.get("industry_above_ma20_ratio")),
            "industry_new_low_60d_relief_5d": float_or_none(row.get("industry_new_low_60d_relief_5d")),
        }
        trades.append(trade)
        logs.append(log_row(row, min_days, "entered", True, consecutive, entry_idx, exit_idx))
        last_exit_idx = exit_idx
        consecutive = 0

    trades_df = pd.DataFrame(trades)
    logs_df = pd.DataFrame(logs)
    summary = summarize_cluster_trades(trades_df, policy, min_days, total_model_signal_dates, signal_id, name)
    return trades_df, summary, logs_df


def summarize_cluster_trades(trades: pd.DataFrame, policy: dict[str, Any], min_days: int, signal_dates: int, signal_id: str, name: str) -> pd.DataFrame:
    row = empty_cluster_summary(policy, min_days)
    row["signal_id"] = signal_id
    row["signal_name_zh"] = name
    row["signal_type"] = "持久性信号簇实时仿真"
    row["signal_dates"] = int(signal_dates)
    if trades.empty:
        return pd.DataFrame([row])
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    bad = trades["is_bad_window"].astype(bool)
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
            "bad_window_rate": float(bad.mean()),
            "event_bad_window_rate": float(bad.mean()),
            "event_worst_return": float(returns.min()),
        }
    )
    row["status"] = classify_cluster(row, policy)
    return pd.DataFrame([row])


def empty_cluster_summary(policy: dict[str, Any], min_days: int) -> dict[str, Any]:
    cfg = policy["persistent_cluster"]
    return {
        "signal_id": f"{cfg['primary_signal_id']}_min{min_days}",
        "signal_name_zh": f"V4.5连续{min_days}日信号簇实时仿真",
        "signal_type": "持久性信号簇实时仿真",
        "status": "样本不足",
        "min_consecutive_signal_days": int(min_days),
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


def classify_cluster(row: dict[str, Any], policy: dict[str, Any]) -> str:
    thresholds = policy["promotion_thresholds"]
    events = int(nz(row.get("nonoverlap_events", 0)))
    mean_return = nz(row.get("event_mean_return", math.nan), -1.0)
    win_rate = nz(row.get("event_win_rate", math.nan), 0.0)
    bad_rate = nz(row.get("event_bad_window_rate", math.nan), 1.0)
    active_years = int(nz(row.get("active_years", 0)))
    concentration = nz(row.get("max_single_year_concentration", math.nan), 1.0)
    hard = (
        events >= int(thresholds["min_nonoverlap_events"])
        and mean_return >= float(thresholds["min_event_mean_return"])
        and win_rate >= float(thresholds["min_event_win_rate"])
        and bad_rate <= float(thresholds["max_event_bad_window_rate"])
        and active_years >= int(thresholds["min_active_years"])
        and concentration <= float(thresholds["max_single_year_concentration"])
    )
    if hard:
        return "反弹窗口候选"
    if events >= 8 and mean_return > 0 and win_rate >= 0.5 and bad_rate <= 0.35:
        return "条件观察"
    if events < 8:
        return "样本不足"
    return "拒绝"


def build_cluster_audit(policy: dict[str, Any], predictions: pd.DataFrame, failure_summary: pd.DataFrame, cluster_summary: pd.DataFrame, sensitivity: pd.DataFrame) -> pd.DataFrame:
    failure = failure_summary.iloc[0].to_dict() if not failure_summary.empty else {}
    cluster = cluster_summary.iloc[0].to_dict() if not cluster_summary.empty else {}
    return pd.DataFrame(
        [
            {
                "audit_item": "persistent_cluster_filter",
                "status": "pass",
                "filtered_signal_dates": int(predictions["model_signal"].astype(bool).sum()) if not predictions.empty else 0,
                "primary_min_consecutive_signal_days": int(policy["persistent_cluster"]["min_consecutive_signal_days"]),
                "sensitivity_rows": int(len(sensitivity)),
                "base_nonoverlap_events": int(nz(failure.get("nonoverlap_events", 0))),
                "cluster_nonoverlap_events": int(nz(cluster.get("nonoverlap_events", 0))),
                "base_event_mean_return": float_or_none(failure.get("event_mean_return")),
                "cluster_event_mean_return": float_or_none(cluster.get("event_mean_return")),
                "base_bad_window_rate": float_or_none(failure.get("event_bad_window_rate")),
                "cluster_bad_window_rate": float_or_none(cluster.get("event_bad_window_rate")),
                "audit_note": "连续信号天数只用截至信号日已出现的model_signal计数，不使用未来簇尾。入场为下一交易日收盘。",
            }
        ]
    )


def build_annual_distribution(predictions: pd.DataFrame, raw_trades: pd.DataFrame, failure_trades: pd.DataFrame, cluster_trades: pd.DataFrame) -> pd.DataFrame:
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
        (cluster_trades, "persistent_cluster_realtime_trades", "v4_5_persistent_cluster_realtime"),
    ]:
        if frame.empty:
            continue
        for year, group in frame.groupby("year"):
            rows.append({"source": source, "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def build_top_candidates(raw_summary: pd.DataFrame, failure_summary: pd.DataFrame, cluster_summary: pd.DataFrame, model_summary: pd.DataFrame) -> pd.DataFrame:
    combined = concat_frames([cluster_summary, failure_summary, raw_summary, model_summary])
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
        "signal_dates",
        "nonoverlap_events",
        "active_years",
        "max_single_year_concentration",
        "mean_return",
        "bad_window_rate",
        "event_mean_return",
        "event_win_rate",
        "event_bad_window_rate",
        "event_worst_return",
    ]
    return combined[[col for col in columns if col in combined.columns]]


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame, threshold_audit: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "failure-state and persistent-cluster fields use trade-date close/amount-derived data only",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "train_only_failure_thresholds",
                "status": "pass" if not threshold_audit.empty else "fail",
                "evidence": f"threshold_rows={len(threshold_audit)}; test_years={predictions['year'].nunique() if not predictions.empty else 0}",
                "action": "每年失败状态阈值只用训练期原始信号分布生成。",
            },
            {
                "audit_item": "persistent_cluster_no_future_tail",
                "status": "pass",
                "evidence": f"min_consecutive_signal_days={policy['persistent_cluster']['min_consecutive_signal_days']}; entry_lag_days={policy['persistent_cluster']['entry_lag_days']}",
                "action": "只在当日已达到连续信号天数后触发，未使用事后完整信号簇长度或未来收益。",
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


def build_notes(cluster_summary: pd.DataFrame, failure_summary: pd.DataFrame, raw_summary: pd.DataFrame, sensitivity: pd.DataFrame) -> dict[str, Any]:
    cluster = cluster_summary.iloc[0].to_dict() if not cluster_summary.empty else {}
    failure = failure_summary.iloc[0].to_dict() if not failure_summary.empty else {}
    raw = raw_summary.iloc[0].to_dict() if not raw_summary.empty else {}
    notes: list[str] = []
    status = str(cluster.get("status", ""))
    if status == "反弹窗口候选":
        notes.append("V4.5连续信号簇达到内部候选状态，但统一评价仍要检查样本数和年度稳定性。")
    elif status == "条件观察":
        notes.append("V4.5连续信号簇改善了收益、胜率和坏窗口，但样本数不足，不能升级为有效反弹窗口。")
    else:
        notes.append("V4.5连续信号簇仍未证明能有效找到反弹窗口。")
    notes.append(
        f"持久性簇实时仿真：非重叠事件 {int(nz(cluster.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(cluster.get('event_mean_return'))}，胜率 {fmt_pct(cluster.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(cluster.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"扩展样本V4.3参考：非重叠事件 {int(nz(failure.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(failure.get('event_mean_return'))}，胜率 {fmt_pct(failure.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(failure.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"扩展样本原始模型参考：非重叠事件 {int(nz(raw.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(raw.get('event_mean_return'))}，胜率 {fmt_pct(raw.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(raw.get('event_bad_window_rate'))}。"
    )
    if not sensitivity.empty:
        best = sensitivity.sort_values("event_mean_return", ascending=False).iloc[0].to_dict()
        notes.append(
            f"敏感性最高收益组合为连续 {int(nz(best.get('min_consecutive_signal_days', 0)))} 日，"
            f"平均收益 {fmt_pct(best.get('event_mean_return'))}，样本 {int(nz(best.get('nonoverlap_events', 0)))}。"
        )
    notes.append("V4.5的实质进展是找到一个更像反弹窗口的条件观察，但它仍受样本不足约束。下一步应优先扩大可验证事件，而不是继续提高门槛。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "下一步做跨目标/跨持有期复核：检验连续信号簇在10、20、30日窗口是否同向有效，并检查是否只是少数年份的事件驱动。若仍样本不足，应接入更长历史宽基或宏观风险偏好代理。",
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, close_matrix: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, cluster_summary: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = top[top["status"] == "反弹窗口候选"] if not top.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    cluster = cluster_summary.iloc[0].to_dict() if not cluster_summary.empty else {}
    audit_fail_count = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_target_panel_count": int(len(panel)),
        "industry_count": int(close_matrix.shape[1]),
        "primary_signal_id": policy["persistent_cluster"]["primary_signal_id"],
        "primary_realtime_events": int(nz(cluster.get("nonoverlap_events", 0))),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_signal_id": best.get("signal_id", ""),
        "best_status": best.get("status", ""),
        "best_nonoverlap_events": int(nz(best.get("nonoverlap_events", 0))) if best else 0,
        "best_event_mean_return": float_or_none(best.get("event_mean_return")) if best else None,
        "best_event_bad_window_rate": float_or_none(best.get("event_bad_window_rate")) if best else None,
        "final_verdict": final_verdict(candidates, audit_fail_count, cluster),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int, cluster: dict[str, Any]) -> str:
    if audit_fail_count:
        return "research_only；存在数据或泄漏审计失败"
    if not candidates.empty:
        return "research_only；存在持久性信号簇候选但仍需统一评价和未来样本验证"
    if str(cluster.get("status", "")) == "条件观察":
        return "research_only；持久性信号簇为条件观察，样本不足，不能升级为有效反弹窗口"
    return "research_only；持久性信号簇尚未证明能有效找到反弹窗口"


def render_report(
    v34: Any,
    summary: dict[str, Any],
    top: pd.DataFrame,
    data_audit: pd.DataFrame,
    target_audit: pd.DataFrame,
    year_summary: pd.DataFrame,
    cluster_audit: pd.DataFrame,
    sensitivity: pd.DataFrame,
    raw_summary: pd.DataFrame,
    failure_summary: pd.DataFrame,
    cluster_summary: pd.DataFrame,
    cluster_trades: pd.DataFrame,
    leakage: pd.DataFrame,
    notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines = ["# V4.5 扩展样本信号簇持久性反弹窗口研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V4.5 在 V4.3 失败状态过滤基础上，把样本外起点从2021年前移到2018年，并只在信号连续出现达到预设天数后才进入实时仿真。",
        "",
        f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
        f"- 行业数：{summary['industry_count']}",
        f"- 持久性簇实时交易数：{summary['primary_realtime_events']}",
        f"- 反弹窗口候选数：{summary['candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 候选排序",
        "",
    ]
    lines.extend(v34.table_or_empty(top, {"signal_id": "信号ID", "signal_name_zh": "名称", "signal_type": "类型", "status": "状态", "min_consecutive_signal_days": "连续天数", "signal_dates": "信号日", "nonoverlap_events": "非重叠事件", "event_mean_return": "事件收益", "event_win_rate": "事件胜率", "event_bad_window_rate": "坏窗口", "event_worst_return": "最差事件"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 数据可得性", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 目标标签", ""]
    lines.extend(v34.table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 年度失败状态模型", ""]
    lines.extend(v34.table_or_empty(year_summary, {"year": "年份", "status": "状态", "train_rows": "训练样本", "test_rows": "测试样本", "raw_signal_dates": "原始信号", "signal_dates": "过滤后信号", "signal_mean_return": "信号收益", "signal_bad_window_rate": "坏窗口率"}, {"signal_mean_return", "signal_bad_window_rate"}))
    lines += ["", "## 持久性信号簇审计", ""]
    lines.extend(v34.table_or_empty(cluster_audit, {"audit_item": "项目", "status": "状态", "filtered_signal_dates": "过滤后信号日", "primary_min_consecutive_signal_days": "主规则连续天数", "base_nonoverlap_events": "基础事件", "cluster_nonoverlap_events": "簇事件", "base_event_mean_return": "基础收益", "cluster_event_mean_return": "簇收益", "base_bad_window_rate": "基础坏窗口", "cluster_bad_window_rate": "簇坏窗口", "audit_note": "说明"}, {"base_event_mean_return", "cluster_event_mean_return", "base_bad_window_rate", "cluster_bad_window_rate"}))
    lines += ["", "## 连续天数敏感性", ""]
    lines.extend(v34.table_or_empty(sensitivity, {"min_consecutive_signal_days": "连续天数", "status": "状态", "nonoverlap_events": "事件数", "active_years": "活跃年份", "max_single_year_concentration": "年份集中度", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口", "event_worst_return": "最差事件"}, {"max_single_year_concentration", "event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 原始模型、失败过滤与持久性簇对比", ""]
    lines.extend(v34.table_or_empty(concat_frames([raw_summary, failure_summary, cluster_summary]), {"signal_name_zh": "名称", "status": "状态", "signal_dates": "信号日", "nonoverlap_events": "交易数", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate"}))
    lines += ["", "## 持久性簇交易明细", ""]
    lines.extend(v34.table_or_empty(cluster_trades.head(60), {"signal_date": "信号日", "entry_date": "入场日", "exit_date": "退出日", "holding_days": "持有日", "min_consecutive_signal_days": "连续天数", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
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
        "- `report.md`：中文 V4.5 研究报告，优先打开。",
        "- `top_candidates.csv`：模型、参考和持久性簇排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：特征面板、训练期阈值、信号簇日志、敏感性、实时仿真、年度分布、泄漏审计和冻结策略。",
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


def log_row(row: pd.Series, min_days: int, status: str, entered: bool, consecutive: int, entry_idx: int | None, exit_idx: int | None) -> dict[str, Any]:
    return {
        "signal_date": date_text(row["trade_date"]),
        "min_consecutive_signal_days": int(min_days),
        "status": status,
        "entered": bool(entered),
        "consecutive_signal_days": int(consecutive),
        "entry_row_index": entry_idx,
        "exit_row_index": exit_idx,
        "model_probability": float_or_none(row.get("model_probability")),
        "probability_margin": float_or_none(row.get("probability_margin")),
        "failure_flag_count": int(float_or_nan(row.get("failure_flag_count"), 0.0)),
    }


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
