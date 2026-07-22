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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_4_cluster_confirmation_policy.json"
V43_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v4_3_failure_state.py"
VERSION = "4.4.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.4 cluster-confirmed rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V4.4 policy JSON.")
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
    normalize_summary(raw_trades, raw_summary, "v3_7_raw_model_reference", "V3.7原始概率模型参考", "实时仿真")
    failure_trades, failure_summary = v34.run_realtime_simulation(panel, predictions, policy)
    normalize_summary(failure_trades, failure_summary, "v4_3_failure_state_reference", "V4.3失败状态过滤参考", "失败状态过滤参考")
    cluster_trades, cluster_summary, cluster_log = run_cluster_confirmation(panel, predictions, policy)

    cluster_audit = build_cluster_audit(policy, predictions, failure_summary, cluster_summary, cluster_log)
    failure_profile = v43.build_failure_profile(predictions, policy)
    flag_profile = v43.build_flag_profile(predictions, policy)
    annual_distribution = build_annual_distribution(predictions, raw_trades, failure_trades, cluster_trades)
    top_candidates = build_top_candidates(raw_summary, failure_summary, cluster_summary, model_summary)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions, threshold_audit, cluster_log)
    notes = build_notes(cluster_summary, failure_summary, raw_summary, cluster_audit)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, cluster_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v44_cluster_feature_panel.csv", index=False, encoding="utf-8-sig")
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
    cluster_log.to_csv(debug_dir / "cluster_confirmation_log.csv", index=False, encoding="utf-8-sig")
    cluster_audit.to_csv(debug_dir / "cluster_confirmation_audit.csv", index=False, encoding="utf-8-sig")
    flag_profile.to_csv(debug_dir / "failure_state_flag_profile.csv", index=False, encoding="utf-8-sig")
    failure_profile.to_csv(debug_dir / "failure_case_profile.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, year_summary, cluster_audit, flag_profile, failure_profile, raw_summary, failure_summary, cluster_summary, cluster_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V4.4信号簇内延迟确认反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"簇确认实时交易数={run_summary['primary_realtime_events']}")
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


def run_cluster_confirmation(panel: pd.DataFrame, predictions: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if predictions.empty:
        return pd.DataFrame(), pd.DataFrame([empty_cluster_summary()]), pd.DataFrame()
    full = panel.sort_values("trade_date").reset_index(drop=True).copy()
    full["trade_date"] = pd.to_datetime(full["trade_date"], errors="coerce")
    pred_cols = [
        "trade_date",
        "model_signal",
        "raw_model_signal",
        "model_probability",
        "model_threshold",
        "probability_margin",
        "failure_flag_count",
        "failure_state_reject",
    ]
    pred = predictions[[col for col in pred_cols if col in predictions.columns]].copy()
    pred["trade_date"] = pd.to_datetime(pred["trade_date"], errors="coerce")
    full = full.merge(pred, on="trade_date", how="left")
    full["model_signal"] = full["model_signal"].fillna(False).astype(bool)
    nav = pd.to_numeric(full["market_nav"], errors="coerce").reset_index(drop=True)
    candidates = list(full.index[full["model_signal"].astype(bool)])
    cfg = policy["cluster_confirmation"]
    rows: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    last_exit = -1
    blocked_until = -1
    for raw_idx in candidates:
        if raw_idx <= last_exit or raw_idx <= blocked_until:
            continue
        raw_date = date_text(full.loc[raw_idx, "trade_date"])
        confirm_idx = -1
        last_reason = "no_confirmation_in_window"
        for obs_idx in range(raw_idx + int(cfg["min_wait_days"]), min(raw_idx + int(cfg["max_wait_days"]), len(full) - 2) + 1):
            ok, reason = confirmation_passed(full.iloc[obs_idx], cfg)
            if ok:
                confirm_idx = obs_idx
                last_reason = "confirmed"
                break
            last_reason = reason
        if confirm_idx < 0:
            logs.append(log_row(raw_date, raw_idx, None, None, None, last_reason, False))
            blocked_until = min(raw_idx + int(cfg["max_wait_days"]), len(full) - 1)
            continue
        entry_idx = confirm_idx + 1
        exit_idx = entry_idx + int(cfg["holding_days"])
        if exit_idx >= len(full):
            logs.append(log_row(raw_date, raw_idx, confirm_idx, None, None, "insufficient_forward_path", False))
            blocked_until = confirm_idx
            continue
        entry_nav = nav.iloc[entry_idx]
        exit_nav = nav.iloc[exit_idx]
        if pd.isna(entry_nav) or pd.isna(exit_nav):
            logs.append(log_row(raw_date, raw_idx, confirm_idx, None, None, "missing_nav_path", False))
            blocked_until = confirm_idx
            continue
        path = nav.iloc[entry_idx : exit_idx + 1] / entry_nav - 1.0
        trade_return = float(exit_nav / entry_nav - 1.0)
        rows.append(
            {
                "signal_id": "v4_4_cluster_confirmation_realtime",
                "signal_date": raw_date,
                "confirm_date": date_text(full.loc[confirm_idx, "trade_date"]),
                "entry_date": date_text(full.loc[entry_idx, "trade_date"]),
                "exit_date": date_text(full.loc[exit_idx, "trade_date"]),
                "holding_days": int(cfg["holding_days"]),
                "confirm_delay_days": int(confirm_idx - raw_idx),
                "trade_return": trade_return,
                "max_adverse_return": float(path.min()) if len(path) else math.nan,
                "is_win": bool(trade_return > 0),
                "is_bad_window": bool(trade_return <= float(policy["bad_window_threshold"])),
                "year": int(pd.Timestamp(full.loc[raw_idx, "trade_date"]).year),
                "model_probability": float_or_none(full.loc[confirm_idx, "model_probability"]),
                "probability_margin": float_or_none(full.loc[confirm_idx, "probability_margin"]),
                "failure_flag_count": float_or_none(full.loc[confirm_idx, "failure_flag_count"]),
                "market_return_5d": float_or_none(full.loc[confirm_idx, "market_return_5d"]),
                "industry_positive_5d_ratio": float_or_none(full.loc[confirm_idx, "industry_positive_5d_ratio"]),
            }
        )
        logs.append(log_row(raw_date, raw_idx, confirm_idx, entry_idx, exit_idx, "confirmed", True))
        last_exit = exit_idx
    trades = pd.DataFrame(rows)
    return trades, summarize_cluster_trades(trades, predictions, policy), pd.DataFrame(logs)


def confirmation_passed(row: pd.Series, cfg: dict[str, Any]) -> tuple[bool, str]:
    if not bool(row.get("model_signal", False)):
        return False, "filtered_signal_not_persistent"
    if float_or_nan(row.get("failure_flag_count"), 99.0) > float(cfg["max_failure_flag_count_on_confirm"]):
        return False, "failure_flags_too_high"
    if float_or_nan(row.get("probability_margin")) < float(cfg["min_model_probability_margin"]):
        return False, "probability_margin_too_low"
    if float_or_nan(row.get("market_return_5d")) < float(cfg["min_market_return_5d"]):
        return False, "market_return_5d_too_low"
    if float_or_nan(row.get("industry_positive_5d_ratio")) < float(cfg["min_industry_positive_5d_ratio"]):
        return False, "industry_positive_5d_ratio_too_low"
    return True, "confirmed"


def summarize_cluster_trades(trades: pd.DataFrame, predictions: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    summary = empty_cluster_summary()
    summary["signal_dates"] = int(predictions["model_signal"].astype(bool).sum()) if not predictions.empty and "model_signal" in predictions.columns else 0
    summary["nonoverlap_events"] = int(len(trades))
    if trades.empty:
        return pd.DataFrame([summary])
    annual = trades["year"].value_counts(normalize=True)
    summary.update(
        {
            "event_mean_return": float(pd.to_numeric(trades["trade_return"], errors="coerce").mean()),
            "event_win_rate": float(trades["is_win"].mean()),
            "event_bad_window_rate": float(trades["is_bad_window"].mean()),
            "event_worst_return": float(pd.to_numeric(trades["trade_return"], errors="coerce").min()),
            "max_single_year_concentration": float(annual.max()),
            "active_years": int(trades["year"].nunique()),
            "avg_confirm_delay_days": float(pd.to_numeric(trades["confirm_delay_days"], errors="coerce").mean()),
        }
    )
    summary["status"] = classify_summary(summary, policy)
    return pd.DataFrame([summary])


def empty_cluster_summary() -> dict[str, Any]:
    return {
        "signal_id": "v4_4_cluster_confirmation_realtime",
        "signal_name_zh": "V4.4信号簇内延迟确认实时仿真",
        "signal_type": "簇内确认实时仿真",
        "status": "样本不足",
        "signal_dates": 0,
        "nonoverlap_events": 0,
    }


def classify_summary(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    checks = {
        "signal_dates": nz(row.get("signal_dates")) >= float(th["min_signal_dates"]),
        "events": nz(row.get("nonoverlap_events")) >= float(th["min_nonoverlap_events"]),
        "active_years": nz(row.get("active_years")) >= float(th["min_active_years"]),
        "concentration": nz(row.get("max_single_year_concentration"), 1.0) <= float(th["max_single_year_concentration"]),
        "event_return": nz(row.get("event_mean_return")) >= float(th["min_event_mean_return"]),
        "event_win": nz(row.get("event_win_rate")) >= float(th["min_event_win_rate"]),
        "event_bad": nz(row.get("event_bad_window_rate"), 1.0) <= float(th["max_event_bad_window_rate"]),
    }
    if all(checks.values()):
        return "反弹窗口候选"
    if not checks["signal_dates"] or not checks["events"] or not checks["active_years"]:
        return "样本不足"
    if checks["event_return"] and checks["event_bad"] and checks["event_win"]:
        return "状态观察"
    return "拒绝"


def build_cluster_audit(policy: dict[str, Any], predictions: pd.DataFrame, failure_summary: pd.DataFrame, cluster_summary: pd.DataFrame, cluster_log: pd.DataFrame) -> pd.DataFrame:
    failure = failure_summary.iloc[0].to_dict() if not failure_summary.empty else {}
    cluster = cluster_summary.iloc[0].to_dict() if not cluster_summary.empty else {}
    return pd.DataFrame(
        [
            {
                "audit_item": "cluster_confirmation_filter",
                "status": "pass",
                "base_filtered_signal_dates": int(predictions["model_signal"].astype(bool).sum()) if not predictions.empty else 0,
                "cluster_watch_rows": int(len(cluster_log)),
                "confirmed_clusters": int(cluster_log["confirmed"].sum()) if not cluster_log.empty and "confirmed" in cluster_log.columns else 0,
                "base_nonoverlap_events": int(nz(failure.get("nonoverlap_events", 0))),
                "cluster_nonoverlap_events": int(nz(cluster.get("nonoverlap_events", 0))),
                "base_event_mean_return": float_or_none(failure.get("event_mean_return")),
                "cluster_event_mean_return": float_or_none(cluster.get("event_mean_return")),
                "base_bad_window_rate": float_or_none(failure.get("event_bad_window_rate")),
                "cluster_bad_window_rate": float_or_none(cluster.get("event_bad_window_rate")),
                "audit_note": "簇内确认只使用确认日已知特征；确认后下一交易日收盘入场。",
            }
        ]
    )


def build_annual_distribution(predictions: pd.DataFrame, raw_trades: pd.DataFrame, failure_trades: pd.DataFrame, cluster_trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not predictions.empty:
        for signal_col, source, signal_id in [
            ("raw_model_signal", "raw_model_signal_dates", "raw_probability_model"),
            ("model_signal", "failure_state_filtered_signal_dates", "failure_state_filtered_probability_model"),
        ]:
            signals = predictions[predictions[signal_col].astype(bool)]
            for year, group in signals.groupby("year"):
                rows.append({"source": source, "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    for frame, source, signal_id in [
        (raw_trades, "raw_realtime_trades", "v3_7_raw_model_reference"),
        (failure_trades, "failure_state_realtime_trades", "v4_3_failure_state_reference"),
        (cluster_trades, "cluster_confirmed_realtime_trades", "v4_4_cluster_confirmation_realtime"),
    ]:
        if not frame.empty:
            for year, group in frame.groupby("year"):
                rows.append({"source": source, "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def build_top_candidates(raw_summary: pd.DataFrame, failure_summary: pd.DataFrame, cluster_summary: pd.DataFrame, model_summary: pd.DataFrame) -> pd.DataFrame:
    combined = concat_frames([cluster_summary, failure_summary, raw_summary, model_summary])
    if combined.empty:
        return combined
    priority = {"反弹窗口候选": 0, "状态观察": 1, "样本不足": 2, "拒绝": 3}
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
        "signal_dates",
        "nonoverlap_events",
        "active_years",
        "max_single_year_concentration",
        "event_mean_return",
        "event_win_rate",
        "event_bad_window_rate",
        "event_worst_return",
        "avg_confirm_delay_days",
    ]
    return combined[[col for col in columns if col in combined.columns]]


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame, threshold_audit: pd.DataFrame, cluster_log: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "failure-state and cluster-confirmation fields use trade-date close/amount-derived data",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "cluster_confirmation_execution_lag",
                "status": "pass",
                "evidence": f"confirmed_clusters={int(cluster_log['confirmed'].sum()) if not cluster_log.empty and 'confirmed' in cluster_log.columns else 0}",
                "action": "确认日只观察状态，下一交易日收盘入场。",
            },
            {
                "audit_item": "train_only_failure_thresholds",
                "status": "pass" if not threshold_audit.empty else "fail",
                "evidence": f"threshold_rows={len(threshold_audit)}; test_years={predictions['year'].nunique() if not predictions.empty else 0}",
                "action": "每年失败状态阈值只用训练期原始信号分布生成。",
            },
            {
                "audit_item": "target_used_only_for_training_and_evaluation",
                "status": "pass",
                "evidence": "test-period future returns and bad-window labels are not used to decide cluster entry",
                "action": "测试期未来收益只进入评价，不进入信号。",
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


def build_notes(cluster_summary: pd.DataFrame, failure_summary: pd.DataFrame, raw_summary: pd.DataFrame, cluster_audit: pd.DataFrame) -> dict[str, Any]:
    cluster = cluster_summary.iloc[0].to_dict() if not cluster_summary.empty else {}
    failure = failure_summary.iloc[0].to_dict() if not failure_summary.empty else {}
    raw = raw_summary.iloc[0].to_dict() if not raw_summary.empty else {}
    audit = cluster_audit.iloc[0].to_dict() if not cluster_audit.empty else {}
    notes: list[str] = []
    if str(cluster.get("status", "")) == "反弹窗口候选":
        notes.append("V4.4信号簇内延迟确认达到研究候选状态，但仍必须保持research_only并等待更严格复核。")
    else:
        notes.append("V4.4信号簇内延迟确认仍未证明能有效找到反弹窗口。")
    notes.append(
        f"簇确认实时仿真：非重叠事件 {int(nz(cluster.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(cluster.get('event_mean_return'))}，胜率 {fmt_pct(cluster.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(cluster.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"V4.3过滤参考：非重叠事件 {int(nz(failure.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(failure.get('event_mean_return'))}，坏窗口 {fmt_pct(failure.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"V3.7原始参考：非重叠事件 {int(nz(raw.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(raw.get('event_mean_return'))}，坏窗口 {fmt_pct(raw.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"簇确认：观察记录 {int(nz(audit.get('cluster_watch_rows', 0)))}，"
        f"确认簇 {int(nz(audit.get('confirmed_clusters', 0)))}。"
    )
    notes.append("若 V4.4 仍失败，说明当前日频价格/广度特征已经很难继续通过入场机制修复，应转向新数据源或阶段性停止调参。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "优先接入独立外生风险偏好或交易拥挤度数据；若不接入新数据，建议停止在当前特征族上继续小步调参。",
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
        "primary_signal_id": "v4_4_cluster_confirmation_realtime",
        "primary_realtime_events": int(nz(cluster.get("nonoverlap_events", 0))),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_signal_id": best.get("signal_id", ""),
        "best_status": best.get("status", ""),
        "best_nonoverlap_events": int(nz(best.get("nonoverlap_events", 0))) if best else 0,
        "best_event_mean_return": float_or_none(best.get("event_mean_return")) if best else None,
        "best_event_bad_window_rate": float_or_none(best.get("event_bad_window_rate")) if best else None,
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在数据或泄漏审计失败"
    if candidates.empty:
        return "research_only；信号簇内延迟确认尚未证明能有效找到反弹窗口"
    return "research_only；存在簇确认候选但仍需未来样本验证"


def render_report(
    v34: Any,
    summary: dict[str, Any],
    top: pd.DataFrame,
    data_audit: pd.DataFrame,
    target_audit: pd.DataFrame,
    year_summary: pd.DataFrame,
    cluster_audit: pd.DataFrame,
    flag_profile: pd.DataFrame,
    failure_profile: pd.DataFrame,
    raw_summary: pd.DataFrame,
    failure_summary: pd.DataFrame,
    cluster_summary: pd.DataFrame,
    cluster_trades: pd.DataFrame,
    leakage: pd.DataFrame,
    notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines = ["# V4.4 信号簇内延迟确认反弹窗口研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V4.4 沿用 V4.3 失败状态过滤后的基础信号，再对同一信号簇做1到5日延迟确认，确认后下一交易日收盘入场。",
        "",
        f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
        f"- 行业数：{summary['industry_count']}",
        f"- 簇确认实时交易数：{summary['primary_realtime_events']}",
        f"- 反弹窗口候选数：{summary['candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 候选排序",
        "",
    ]
    lines.extend(v34.table_or_empty(top, {"signal_id": "信号ID", "signal_name_zh": "名称", "signal_type": "类型", "status": "状态", "signal_dates": "信号日", "nonoverlap_events": "非重叠事件", "event_mean_return": "事件收益", "event_win_rate": "事件胜率", "event_bad_window_rate": "坏窗口", "event_worst_return": "最差事件", "avg_confirm_delay_days": "平均确认等待"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 数据可得性", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 目标标签", ""]
    lines.extend(v34.table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 年度基础信号", ""]
    lines.extend(v34.table_or_empty(year_summary, {"year": "年份", "status": "状态", "train_rows": "训练样本", "raw_signal_dates": "原始信号", "signal_dates": "V4.3过滤信号", "rejected_signal_dates": "拒绝信号", "signal_mean_return": "信号收益", "signal_bad_window_rate": "坏窗口率"}, {"signal_mean_return", "signal_bad_window_rate"}))
    lines += ["", "## 簇确认审计", ""]
    lines.extend(v34.table_or_empty(cluster_audit, {"audit_item": "项目", "status": "状态", "base_filtered_signal_dates": "基础信号", "cluster_watch_rows": "观察记录", "confirmed_clusters": "确认簇", "base_event_mean_return": "基础事件收益", "cluster_event_mean_return": "簇确认收益", "base_bad_window_rate": "基础坏窗口", "cluster_bad_window_rate": "簇确认坏窗口"}, {"base_event_mean_return", "cluster_event_mean_return", "base_bad_window_rate", "cluster_bad_window_rate"}))
    lines += ["", "## 旗标画像", ""]
    lines.extend(v34.table_or_empty(flag_profile, {"profile": "画像", "rows": "行数", "mean_return": "均值收益", "win_rate": "胜率", "target_rate": "目标率", "bad_window_rate": "坏窗口率", "failure_flag_count_mean": "失败旗标均值"}, {"mean_return", "win_rate", "target_rate", "bad_window_rate"}))
    lines += ["", "## 失败样本画像", ""]
    lines.extend(v34.table_or_empty(failure_profile, {"profile": "画像", "rows": "行数", "mean_return": "均值收益", "win_rate": "胜率", "target_rate": "目标率", "bad_window_rate": "坏窗口率", "model_probability_mean": "反弹概率", "failure_flag_count_mean": "失败旗标均值"}, {"mean_return", "win_rate", "target_rate", "bad_window_rate", "model_probability_mean"}))
    lines += ["", "## 原始、V4.3与V4.4对比", ""]
    lines.extend(v34.table_or_empty(concat_frames([raw_summary, failure_summary, cluster_summary]), {"signal_name_zh": "名称", "status": "状态", "signal_dates": "信号日", "nonoverlap_events": "交易数", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate"}))
    lines += ["", "## 簇确认交易明细", ""]
    lines.extend(v34.table_or_empty(cluster_trades.head(40), {"signal_date": "信号日", "confirm_date": "确认日", "entry_date": "入场日", "exit_date": "退出日", "holding_days": "持有日", "confirm_delay_days": "确认等待", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
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
        "- `report.md`：中文 V4.4 研究报告，优先打开。",
        "- `top_candidates.csv`：原始模型、V4.3失败状态过滤和V4.4簇确认排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：特征面板、基础信号、簇确认日志、实时仿真、年度分布、泄漏审计和冻结策略。",
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


def log_row(raw_date: str, raw_idx: int, confirm_idx: int | None, entry_idx: int | None, exit_idx: int | None, status: str, confirmed: bool) -> dict[str, Any]:
    return {
        "raw_signal_date": raw_date,
        "raw_signal_index": int(raw_idx),
        "confirm_index": None if confirm_idx is None else int(confirm_idx),
        "entry_index": None if entry_idx is None else int(entry_idx),
        "exit_index": None if exit_idx is None else int(exit_idx),
        "watch_status": status,
        "confirmed": bool(confirmed),
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
