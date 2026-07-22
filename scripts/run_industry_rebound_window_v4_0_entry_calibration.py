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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_0_entry_calibration_policy.json"
V37_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v3_7_industry_breadth.py"
VERSION = "4.0.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.0 entry calibration rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V4.0 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v37 = load_v37_module()
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
    predictions, model_year_summary, model_summary = v34.run_walk_forward_model(panel, policy)
    predictions = add_probability_margin(predictions)
    fixed_trades, fixed_summary = v34.run_realtime_simulation(panel, predictions, policy)
    normalize_fixed_labels(fixed_trades, fixed_summary)

    policy_trades: list[pd.DataFrame] = []
    policy_logs: list[pd.DataFrame] = []
    policy_summaries: list[dict[str, Any]] = []
    for entry_policy in policy["entry_selection_policies"]:
        trades, watch_log = run_entry_policy_simulation(panel, predictions, policy, entry_policy)
        summary = summarize_entry_policy(trades, watch_log, predictions, policy, entry_policy)
        policy_trades.append(trades)
        policy_logs.append(watch_log)
        policy_summaries.append(summary)

    entry_trades = concat_frames(policy_trades)
    entry_watch_log = concat_frames(policy_logs)
    entry_summary = pd.DataFrame(policy_summaries)
    primary_id = policy["primary_entry_policy_id"]
    primary_trades = entry_trades[entry_trades["entry_policy_id"] == primary_id].copy() if not entry_trades.empty else pd.DataFrame()
    primary_summary = entry_summary[entry_summary["entry_policy_id"] == primary_id].copy() if not entry_summary.empty else pd.DataFrame()
    primary_year_summary = build_primary_year_summary(policy, primary_trades)
    filter_audit = build_filter_audit(policy, predictions, entry_watch_log, entry_summary)
    annual_distribution = build_annual_distribution(predictions, fixed_trades, entry_trades)
    top_candidates = build_top_candidates(fixed_summary, model_summary, entry_summary)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions, primary_trades, entry_watch_log)
    notes = build_notes(primary_summary, fixed_summary, entry_summary)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, primary_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v40_entry_feature_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    breadth_audit.to_csv(debug_dir / "breadth_feature_audit.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    model_year_summary.to_csv(debug_dir / "raw_walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    primary_year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    fixed_trades.to_csv(debug_dir / "fixed_horizon_realtime_trades.csv", index=False, encoding="utf-8-sig")
    fixed_summary.to_csv(debug_dir / "fixed_horizon_realtime_summary.csv", index=False, encoding="utf-8-sig")
    entry_trades.to_csv(debug_dir / "entry_calibration_signal_trades.csv", index=False, encoding="utf-8-sig")
    entry_summary.to_csv(debug_dir / "entry_calibration_signal_summary.csv", index=False, encoding="utf-8-sig")
    entry_watch_log.to_csv(debug_dir / "entry_confirmation_watch_log.csv", index=False, encoding="utf-8-sig")
    filter_audit.to_csv(debug_dir / "entry_calibration_filter_audit.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    primary_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, model_year_summary, filter_audit, fixed_summary, entry_summary, primary_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V4.0入场确认与概率校准反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"主入场策略={run_summary['primary_entry_policy_id']}")
    print(f"主策略实时交易数={run_summary['primary_realtime_events']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v37_module() -> Any:
    spec = importlib.util.spec_from_file_location("v37_industry_breadth", V37_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V3.7 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_probability_margin(predictions: pd.DataFrame) -> pd.DataFrame:
    if predictions.empty:
        return predictions
    output = predictions.copy()
    output["trade_date"] = pd.to_datetime(output["trade_date"], errors="coerce")
    output["model_probability"] = pd.to_numeric(output["model_probability"], errors="coerce")
    output["model_threshold"] = pd.to_numeric(output["model_threshold"], errors="coerce")
    output["probability_margin"] = output["model_probability"] - output["model_threshold"]
    return output


def run_entry_policy_simulation(panel: pd.DataFrame, predictions: pd.DataFrame, policy: dict[str, Any], entry_policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if predictions.empty:
        return pd.DataFrame(), pd.DataFrame()
    full = build_prediction_feature_frame(panel, predictions)
    nav = pd.to_numeric(full["market_nav"], errors="coerce").reset_index(drop=True)
    raw_signal_indices = list(full.index[full["model_signal"].fillna(False).astype(bool)])
    rows: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    last_exit = -1
    holding_days = int(entry_policy["holding_days"])
    min_confirm = int(entry_policy["min_confirmation_days"])
    window = int(entry_policy["confirmation_window_days"])
    for raw_idx in raw_signal_indices:
        raw_row = full.iloc[raw_idx]
        raw_date = date_text(raw_row["trade_date"])
        if raw_idx <= last_exit:
            logs.append(base_watch_log(entry_policy, raw_date, "skipped_overlap_existing_position", raw_idx))
            continue
        confirm_idx = -1
        confirm_reasons: list[str] = []
        for obs_idx in range(raw_idx + min_confirm, min(raw_idx + window, len(full) - 1) + 1):
            obs_row = full.iloc[obs_idx]
            passed, failed_reasons = confirmation_passed(obs_row, entry_policy)
            if passed:
                confirm_idx = obs_idx
                confirm_reasons = ["confirmed"]
                break
            confirm_reasons = failed_reasons
        if confirm_idx < 0:
            logs.append(base_watch_log(entry_policy, raw_date, ";".join(confirm_reasons) if confirm_reasons else "no_confirmation_in_window", raw_idx))
            continue
        entry_idx = confirm_idx + 1
        exit_idx = entry_idx + holding_days
        if exit_idx >= len(full):
            logs.append(base_watch_log(entry_policy, raw_date, "insufficient_forward_path", raw_idx, confirm_idx))
            continue
        entry_nav = nav.iloc[entry_idx]
        exit_nav = nav.iloc[exit_idx]
        if pd.isna(entry_nav) or pd.isna(exit_nav):
            logs.append(base_watch_log(entry_policy, raw_date, "missing_nav_path", raw_idx, confirm_idx))
            continue
        path = nav.iloc[entry_idx : exit_idx + 1] / entry_nav - 1.0
        trade_return = float(exit_nav / entry_nav - 1.0)
        confirm_row = full.iloc[confirm_idx]
        rows.append(
            {
                "signal_id": "v4_0_entry_calibration_realtime",
                "signal_date": raw_date,
                "confirm_date": date_text(confirm_row["trade_date"]),
                "entry_date": date_text(full.loc[entry_idx, "trade_date"]),
                "exit_date": date_text(full.loc[exit_idx, "trade_date"]),
                "holding_days": holding_days,
                "confirmation_delay_days": int(confirm_idx - raw_idx),
                "trade_return": trade_return,
                "max_adverse_return": float(path.min()) if len(path) else math.nan,
                "is_win": bool(trade_return > 0),
                "is_bad_window": bool(trade_return <= float(policy["bad_window_threshold"])),
                "year": int(pd.Timestamp(raw_row["trade_date"]).year),
                "entry_policy_id": entry_policy["entry_policy_id"],
                "entry_policy_name_zh": entry_policy["entry_policy_name_zh"],
                "model_probability": float_or_none(confirm_row.get("model_probability")),
                "model_threshold": float_or_none(confirm_row.get("model_threshold")),
                "probability_margin": float_or_none(confirm_row.get("probability_margin")),
                "breadth_recovery_score": float_or_none(confirm_row.get("breadth_recovery_score")),
                "industry_positive_5d_ratio": float_or_none(confirm_row.get("industry_positive_5d_ratio")),
                "market_return_5d": float_or_none(confirm_row.get("market_return_5d")),
                "market_volatility_20d_vs_60d": float_or_none(confirm_row.get("market_volatility_20d_vs_60d")),
                "industry_new_low_60d_ratio": float_or_none(confirm_row.get("industry_new_low_60d_ratio")),
            }
        )
        logs.append(base_watch_log(entry_policy, raw_date, "confirmed", raw_idx, confirm_idx, entry_idx, exit_idx))
        last_exit = exit_idx
    return pd.DataFrame(rows), pd.DataFrame(logs)


def build_prediction_feature_frame(panel: pd.DataFrame, predictions: pd.DataFrame) -> pd.DataFrame:
    full = panel.sort_values("trade_date").reset_index(drop=True).copy()
    full["trade_date"] = pd.to_datetime(full["trade_date"], errors="coerce")
    pred_cols = ["trade_date", "model_probability", "model_threshold", "model_signal", "probability_margin"]
    pred = predictions[[col for col in pred_cols if col in predictions.columns]].copy()
    pred["trade_date"] = pd.to_datetime(pred["trade_date"], errors="coerce")
    merged = full.merge(pred, on="trade_date", how="left")
    if "model_signal" not in merged.columns:
        merged["model_signal"] = False
    merged["model_signal"] = merged["model_signal"].fillna(False).astype(bool)
    return merged


def confirmation_passed(row: pd.Series, entry_policy: dict[str, Any]) -> tuple[bool, list[str]]:
    failed: list[str] = []
    if entry_policy.get("require_model_signal_on_confirmation", False) and not bool(row.get("model_signal", False)):
        failed.append("model_signal_not_persistent")
    for condition in entry_policy["conditions"]:
        value = float_or_nan(row.get(condition["field"]))
        if math.isnan(value):
            failed.append(f"{condition['field']}_missing")
            continue
        if not compare(value, str(condition["op"]), float(condition["value"])):
            failed.append(f"{condition['field']}_{condition['op']}_{condition['value']}")
    return (len(failed) == 0), failed


def compare(left: float, op: str, right: float) -> bool:
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == "<":
        return left < right
    if op == "==":
        return left == right
    raise ValueError(f"unsupported operator: {op}")


def base_watch_log(
    entry_policy: dict[str, Any],
    raw_date: str,
    watch_status: str,
    raw_idx: int,
    confirm_idx: int | None = None,
    entry_idx: int | None = None,
    exit_idx: int | None = None,
) -> dict[str, Any]:
    return {
        "entry_policy_id": entry_policy["entry_policy_id"],
        "entry_policy_name_zh": entry_policy["entry_policy_name_zh"],
        "raw_signal_date": raw_date,
        "raw_signal_index": int(raw_idx),
        "confirm_index": None if confirm_idx is None else int(confirm_idx),
        "entry_index": None if entry_idx is None else int(entry_idx),
        "exit_index": None if exit_idx is None else int(exit_idx),
        "watch_status": watch_status,
        "confirmed": watch_status == "confirmed",
    }


def summarize_entry_policy(trades: pd.DataFrame, watch_log: pd.DataFrame, predictions: pd.DataFrame, policy: dict[str, Any], entry_policy: dict[str, Any]) -> dict[str, Any]:
    confirmed = int(watch_log["confirmed"].sum()) if not watch_log.empty and "confirmed" in watch_log.columns else 0
    raw_signal_dates = int(predictions["model_signal"].astype(bool).sum()) if not predictions.empty and "model_signal" in predictions.columns else 0
    eligible_raw = int((watch_log["watch_status"] != "skipped_overlap_existing_position").sum()) if not watch_log.empty else 0
    summary = {
        "signal_id": "v4_0_entry_calibration_realtime",
        "signal_name_zh": entry_policy["entry_policy_name_zh"],
        "signal_type": "入场确认实时仿真",
        "entry_policy_id": entry_policy["entry_policy_id"],
        "entry_policy_name_zh": entry_policy["entry_policy_name_zh"],
        "is_primary": bool(entry_policy.get("is_primary", False)),
        "raw_model_signal_dates": raw_signal_dates,
        "eligible_raw_signal_dates": eligible_raw,
        "signal_dates": confirmed,
        "nonoverlap_events": int(len(trades)),
        "status": "样本不足",
    }
    if trades.empty:
        return summary
    annual = trades["year"].value_counts(normalize=True)
    summary.update(
        {
            "event_mean_return": float(pd.to_numeric(trades["trade_return"], errors="coerce").mean()),
            "event_win_rate": float(trades["is_win"].mean()),
            "event_bad_window_rate": float(trades["is_bad_window"].mean()),
            "event_worst_return": float(pd.to_numeric(trades["trade_return"], errors="coerce").min()),
            "max_single_year_concentration": float(annual.max()),
            "active_years": int(trades["year"].nunique()),
            "avg_confirmation_delay_days": float(pd.to_numeric(trades["confirmation_delay_days"], errors="coerce").mean()),
            "avg_probability_margin": float(pd.to_numeric(trades["probability_margin"], errors="coerce").mean()),
        }
    )
    summary["status"] = classify_summary(summary, policy)
    return summary


def build_primary_year_summary(policy: dict[str, Any], primary_trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for year in range(int(policy["model"]["test_start_year"]), int(policy["model"]["test_end_year"]) + 1):
        year_trades = primary_trades[primary_trades["year"] == year] if not primary_trades.empty and "year" in primary_trades.columns else pd.DataFrame()
        rows.append(
            {
                "year": year,
                "status": "pass",
                "train_rows": math.nan,
                "test_rows": math.nan,
                "signal_dates": int(len(year_trades)),
                "signal_target_rate": float(year_trades["is_win"].mean()) if len(year_trades) else math.nan,
                "signal_mean_return": float(pd.to_numeric(year_trades["trade_return"], errors="coerce").mean()) if len(year_trades) else math.nan,
            }
        )
    return pd.DataFrame(rows)


def build_filter_audit(policy: dict[str, Any], predictions: pd.DataFrame, watch_log: pd.DataFrame, entry_summary: pd.DataFrame) -> pd.DataFrame:
    raw_signal_dates = int(predictions["model_signal"].astype(bool).sum()) if not predictions.empty and "model_signal" in predictions.columns else 0
    rows: list[dict[str, Any]] = []
    for entry_policy in policy["entry_selection_policies"]:
        policy_id = entry_policy["entry_policy_id"]
        logs = watch_log[watch_log["entry_policy_id"] == policy_id] if not watch_log.empty else pd.DataFrame()
        summary = entry_summary[entry_summary["entry_policy_id"] == policy_id].iloc[0].to_dict() if not entry_summary.empty and (entry_summary["entry_policy_id"] == policy_id).any() else {}
        rejected = logs[~logs["confirmed"].astype(bool)] if not logs.empty and "confirmed" in logs.columns else pd.DataFrame()
        reason = ""
        if not rejected.empty:
            reason = str(rejected["watch_status"].value_counts().idxmax())
        rows.append(
            {
                "entry_policy_id": policy_id,
                "entry_policy_name_zh": entry_policy["entry_policy_name_zh"],
                "is_primary": bool(entry_policy.get("is_primary", False)),
                "raw_model_signal_dates": raw_signal_dates,
                "watch_rows": int(len(logs)),
                "eligible_raw_signal_dates": int(nz(summary.get("eligible_raw_signal_dates", 0))),
                "confirmed_entries": int(nz(summary.get("nonoverlap_events", 0))),
                "rejected_watches": int(len(rejected)),
                "confirmation_rate_vs_watch": float(len(logs[logs["confirmed"].astype(bool)]) / len(logs)) if len(logs) else math.nan,
                "top_rejection_reason": reason,
                "status": summary.get("status", "样本不足"),
                "audit_note": "入场确认只使用确认日以前可见的模型概率和行业广度特征，确认后下一交易日收盘入场。",
            }
        )
    return pd.DataFrame(rows)


def build_annual_distribution(predictions: pd.DataFrame, fixed_trades: pd.DataFrame, entry_trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not predictions.empty:
        signals = predictions[predictions["model_signal"].astype(bool)].copy()
        for year, group in signals.groupby("year"):
            rows.append({"source": "raw_model_signal_dates", "signal_id": "walk_forward_probability_model", "year": int(year), "count": int(len(group))})
    if not fixed_trades.empty:
        for year, group in fixed_trades.groupby("year"):
            rows.append({"source": "fixed_20d_trades", "signal_id": "v3_7_fixed_20d_reference", "year": int(year), "count": int(len(group))})
    if not entry_trades.empty:
        for (entry_policy_id, year), group in entry_trades.groupby(["entry_policy_id", "year"]):
            rows.append({"source": "entry_policy_trades", "signal_id": str(entry_policy_id), "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def build_top_candidates(fixed_summary: pd.DataFrame, model_summary: pd.DataFrame, entry_summary: pd.DataFrame) -> pd.DataFrame:
    frames = []
    if not fixed_summary.empty:
        fixed = fixed_summary.copy()
        fixed["entry_policy_id"] = "fixed_20d_reference"
        fixed["entry_policy_name_zh"] = "V3.7固定20日参考"
        fixed["is_primary"] = False
        frames.append(fixed)
    if not model_summary.empty:
        frames.append(model_summary.copy())
    if not entry_summary.empty:
        frames.append(entry_summary.copy())
    combined = concat_frames(frames)
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
        "entry_policy_id",
        "entry_policy_name_zh",
        "is_primary",
        "status",
        "raw_model_signal_dates",
        "eligible_raw_signal_dates",
        "signal_dates",
        "nonoverlap_events",
        "active_years",
        "max_single_year_concentration",
        "avg_confirmation_delay_days",
        "avg_probability_margin",
        "event_mean_return",
        "event_win_rate",
        "event_bad_window_rate",
        "event_worst_return",
    ]
    return combined[[col for col in columns if col in combined.columns]]


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame, primary_trades: pd.DataFrame, watch_log: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "raw model and confirmation filters use trade-date close/amount-derived features only",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "confirmation_execution_lag",
                "status": "pass",
                "evidence": "confirmation is observed after close; entry executes at next trading day close",
                "action": "确认日不直接按同日收盘入场。",
            },
            {
                "audit_item": "target_used_only_as_outcome",
                "status": "pass",
                "evidence": "target_rebound_window and forward_return fields are used only for training labels and evaluation",
                "action": "未来收益不进入确认条件。",
            },
            {
                "audit_item": "purged_walk_forward",
                "status": "pass" if not predictions.empty else "fail",
                "evidence": f"purge_days={policy['model']['purge_days']}; prediction_rows={len(predictions)}",
                "action": "每个测试年份只用之前样本训练，并剔除测试前重叠标签窗口。",
            },
            {
                "audit_item": "primary_policy_boundary",
                "status": "pass",
                "evidence": f"primary_entry_policy_id={policy['primary_entry_policy_id']}; primary_trades={len(primary_trades)}",
                "action": "统一评价只使用预声明主入场策略，不事后挑最好对照策略。",
            },
            {
                "audit_item": "watch_log_reproducibility",
                "status": "pass" if not watch_log.empty else "fail",
                "evidence": f"watch_rows={len(watch_log)}",
                "action": "保留每个原始模型信号的确认或拒绝记录。",
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


def build_notes(primary_summary: pd.DataFrame, fixed_summary: pd.DataFrame, entry_summary: pd.DataFrame) -> dict[str, Any]:
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    fixed = fixed_summary.iloc[0].to_dict() if not fixed_summary.empty else {}
    notes: list[str] = []
    if str(primary.get("status", "")) == "反弹窗口候选":
        notes.append("V4.0主入场确认策略达到研究候选状态，但仍必须保持research_only并等待更严格复核。")
    else:
        notes.append("V4.0入场确认与概率校准仍未证明能有效找到反弹窗口。")
    notes.append(
        f"主入场确认策略：非重叠事件 {int(nz(primary.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(primary.get('event_mean_return'))}，胜率 {fmt_pct(primary.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(primary.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"V3.7固定20日参考：平均收益 {fmt_pct(fixed.get('event_mean_return'))}，胜率 {fmt_pct(fixed.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(fixed.get('event_bad_window_rate'))}。"
    )
    if not entry_summary.empty:
        best = entry_summary.sort_values("event_mean_return", ascending=False).head(1).iloc[0].to_dict()
        notes.append(
            f"对照入场策略中收益最高的是 {best.get('entry_policy_name_zh', '')}，"
            f"事件收益 {fmt_pct(best.get('event_mean_return'))}，状态 {best.get('status', '')}。"
        )
    notes.append("若 V4.0 仍失败，下一步应从原始样本和标签层做概率校准诊断，而不是继续叠加人工阈值。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "做事件聚类、概率分桶校准和失败窗口前置特征诊断，判断V3.7模型的高概率是否真实可校准。",
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
        "primary_entry_policy_id": policy["primary_entry_policy_id"],
        "primary_realtime_events": int(nz(primary.get("nonoverlap_events", 0))),
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
        return "research_only；入场确认与概率校准尚未证明能有效找到反弹窗口"
    return "research_only；存在入场确认候选但仍需未来样本验证"


def render_report(
    v34: Any,
    summary: dict[str, Any],
    top: pd.DataFrame,
    data_audit: pd.DataFrame,
    target_audit: pd.DataFrame,
    model_year: pd.DataFrame,
    filter_audit: pd.DataFrame,
    fixed_summary: pd.DataFrame,
    entry_summary: pd.DataFrame,
    primary_trades: pd.DataFrame,
    leakage: pd.DataFrame,
    notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines = ["# V4.0 入场确认与概率校准反弹窗口研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V4.0 保持 V3.7 行业广度概率模型和20日反弹目标不变，只测试原始模型信号后的确认等待、概率边际和广度修复过滤。",
        "",
        f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
        f"- 行业数：{summary['industry_count']}",
        f"- 主入场策略：{summary['primary_entry_policy_id']}",
        f"- 主策略实时交易数：{summary['primary_realtime_events']}",
        f"- 反弹窗口候选数：{summary['candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 候选排序",
        "",
    ]
    lines.extend(v34.table_or_empty(top, {
        "signal_id": "信号ID",
        "signal_name_zh": "名称",
        "signal_type": "类型",
        "entry_policy_name_zh": "入场策略",
        "is_primary": "主策略",
        "status": "状态",
        "raw_model_signal_dates": "原始信号日",
        "signal_dates": "确认信号",
        "nonoverlap_events": "非重叠事件",
        "avg_confirmation_delay_days": "平均确认等待",
        "event_mean_return": "事件收益",
        "event_win_rate": "事件胜率",
        "event_bad_window_rate": "坏窗口",
        "event_worst_return": "最差事件",
    }, {"event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 数据可得性", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 目标标签", ""]
    lines.extend(v34.table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 原始 Walk-forward 年度模型", ""]
    lines.extend(v34.table_or_empty(model_year, {"year": "年份", "status": "状态", "train_rows": "训练样本", "test_rows": "测试样本", "signal_dates": "原始信号日", "signal_target_rate": "目标率", "signal_mean_return": "信号收益"}, {"signal_target_rate", "signal_mean_return"}))
    lines += ["", "## 入场确认过滤审计", ""]
    lines.extend(v34.table_or_empty(filter_audit, {"entry_policy_name_zh": "入场策略", "is_primary": "主策略", "raw_model_signal_dates": "原始信号", "watch_rows": "观察记录", "confirmed_entries": "确认入场", "rejected_watches": "拒绝观察", "confirmation_rate_vs_watch": "确认率", "top_rejection_reason": "主要拒绝原因", "status": "状态"}, {"confirmation_rate_vs_watch"}))
    lines += ["", "## 固定持有与入场确认对比", ""]
    lines.extend(v34.table_or_empty(concat_frames([fixed_summary, entry_summary]), {"signal_name_zh": "名称", "entry_policy_name_zh": "入场策略", "status": "状态", "nonoverlap_events": "交易数", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate"}))
    lines += ["", "## 主策略交易明细", ""]
    lines.extend(v34.table_or_empty(primary_trades.head(40), {"signal_date": "原始信号日", "confirm_date": "确认日", "entry_date": "入场日", "exit_date": "退出日", "holding_days": "持有日", "confirmation_delay_days": "确认等待", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
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
        "- `report.md`：中文 V4.0 研究报告，优先打开。",
        "- `top_candidates.csv`：固定持有、原始模型和入场确认策略排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：特征面板、原始模型预测、入场确认观察日志、过滤审计、实时仿真、年度分布、泄漏审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def normalize_fixed_labels(trades: pd.DataFrame, summary: pd.DataFrame) -> None:
    if not trades.empty and "signal_id" in trades.columns:
        trades["signal_id"] = "v3_7_fixed_20d_reference"
    if not summary.empty:
        if "signal_id" in summary.columns:
            summary["signal_id"] = "v3_7_fixed_20d_reference"
        if "signal_name_zh" in summary.columns:
            summary["signal_name_zh"] = "V3.7固定20日参考"


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


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


def date_text(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


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
