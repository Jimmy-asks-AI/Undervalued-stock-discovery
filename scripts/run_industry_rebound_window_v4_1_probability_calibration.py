#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_1_probability_calibration_policy.json"
V37_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v3_7_industry_breadth.py"
VERSION = "4.1.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.1 probability calibration rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V4.1 policy JSON.")
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
    predictions, model_year_summary, model_summary, calibration_bins = run_calibrated_walk_forward_model(panel, policy, v34)
    raw_predictions = predictions.copy()
    if not raw_predictions.empty:
        raw_predictions["model_signal"] = raw_predictions["raw_model_signal"]
    raw_trades, raw_summary = v34.run_realtime_simulation(panel, raw_predictions, policy)
    normalize_raw_labels(raw_trades, raw_summary)
    calibrated_trades, calibrated_summary = v34.run_realtime_simulation(panel, predictions, policy)
    normalize_calibrated_labels(calibrated_trades, calibrated_summary)

    filter_audit = build_filter_audit(policy, predictions, calibration_bins, calibrated_summary)
    annual_distribution = build_annual_distribution(predictions, raw_trades, calibrated_trades)
    top_candidates = build_top_candidates(raw_summary, model_summary, calibrated_summary)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions, calibration_bins)
    notes = build_notes(calibrated_summary, raw_summary, filter_audit)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, calibrated_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v41_calibration_feature_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    breadth_audit.to_csv(debug_dir / "breadth_feature_audit.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    model_year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    calibration_bins.to_csv(debug_dir / "probability_calibration_bins.csv", index=False, encoding="utf-8-sig")
    raw_trades.to_csv(debug_dir / "raw_model_realtime_trades.csv", index=False, encoding="utf-8-sig")
    raw_summary.to_csv(debug_dir / "raw_model_realtime_summary.csv", index=False, encoding="utf-8-sig")
    calibrated_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    calibrated_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    filter_audit.to_csv(debug_dir / "probability_calibration_filter_audit.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, model_year_summary, calibration_bins, filter_audit, raw_summary, calibrated_summary, calibrated_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V4.1概率分桶校准反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"校准后实时交易数={run_summary['primary_realtime_events']}")
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


def run_calibrated_walk_forward_model(panel: pd.DataFrame, policy: dict[str, Any], v34: Any) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    model_policy = policy["model"]
    features = list(model_policy["features"])
    valid = v34.valid_panel(panel, policy).copy()
    rows: list[pd.DataFrame] = []
    year_rows: list[dict[str, Any]] = []
    bin_rows: list[pd.DataFrame] = []
    horizon = int(policy["target_horizon"])
    return_col = f"forward_return_{horizon}d_next_close"
    dd_col = f"forward_max_drawdown_{horizon}d_next_close"
    for year in range(int(model_policy["test_start_year"]), int(model_policy["test_end_year"]) + 1):
        test_start = pd.Timestamp(f"{year}-01-01")
        test_end = pd.Timestamp(f"{year}-12-31")
        train_end = test_start - pd.Timedelta(days=int(model_policy["purge_days"]))
        train = valid[valid["trade_date"] < train_end].dropna(subset=features + ["target_rebound_window"]).copy()
        test = valid[(valid["trade_date"] >= test_start) & (valid["trade_date"] <= test_end)].dropna(subset=features + ["target_rebound_window"]).copy()
        if len(train) < int(model_policy["train_min_rows"]) or test.empty:
            year_rows.append({"year": year, "status": "skip", "train_rows": len(train), "test_rows": len(test), "signal_dates": 0})
            continue
        x_train = train[features].astype(float).to_numpy()
        y_train = train["target_rebound_window"].astype(float).to_numpy()
        x_test = test[features].astype(float).to_numpy()
        mean = np.nanmean(x_train, axis=0)
        std = np.nanstd(x_train, axis=0)
        std = np.where(std < 1e-9, 1.0, std)
        x_train_z = np.nan_to_num((x_train - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)
        x_test_z = np.nan_to_num((x_test - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)
        weights = v34.fit_logistic(x_train_z, y_train, model_policy)
        train_prob = v34.predict_logistic(x_train_z, weights)
        test_prob = v34.predict_logistic(x_test_z, weights)
        pressure_train = v34.conditions_mask(train, policy["baseline_pressure_conditions"], logic="all").to_numpy(dtype=bool)
        threshold_source = train_prob[pressure_train] if pressure_train.any() else train_prob
        threshold = max(float(model_policy["minimum_probability_threshold"]), float(np.nanquantile(threshold_source, float(model_policy["probability_quantile"]))))
        bins, edges, train_target_rate = build_probability_bins(train, train_prob, policy, year)
        test_bins = assign_probability_bins(test_prob, edges)
        test_out = test[["trade_date", "year", "market_nav", "target_rebound_window", return_col, dd_col, "is_bad_window"]].copy()
        test_out["model_probability"] = test_prob
        test_out["model_threshold"] = threshold
        test_out["probability_margin"] = test_out["model_probability"] - test_out["model_threshold"]
        test_out["raw_model_signal"] = test_out["model_probability"] >= threshold
        test_out["calibration_bin"] = test_bins
        test_out = test_out.merge(
            bins[[
                "year",
                "calibration_bin",
                "train_bin_count",
                "train_bin_target_rate",
                "train_bin_mean_return",
                "train_bin_bad_window_rate",
                "calibration_bin_pass",
            ]],
            on=["year", "calibration_bin"],
            how="left",
        )
        calibrated_signal = test_out["raw_model_signal"].astype(bool) & test_out["calibration_bin_pass"].fillna(False).astype(bool)
        if not bool(policy["probability_calibration"].get("require_raw_model_signal", True)):
            calibrated_signal = test_out["calibration_bin_pass"].fillna(False).astype(bool)
        test_out["model_signal"] = calibrated_signal
        test_out["signal_id"] = "calibrated_probability_model"
        rows.append(test_out)
        bin_rows.append(bins)
        signal = test_out[test_out["model_signal"]]
        raw_signal = test_out[test_out["raw_model_signal"]]
        year_rows.append(
            {
                "year": year,
                "status": "pass",
                "train_rows": len(train),
                "train_target_rate": float(train_target_rate),
                "test_rows": len(test),
                "threshold": threshold,
                "raw_signal_dates": int(len(raw_signal)),
                "signal_dates": int(len(signal)),
                "signal_target_rate": float(signal["target_rebound_window"].mean()) if len(signal) else math.nan,
                "signal_mean_return": float(signal[return_col].mean()) if len(signal) else math.nan,
                "calibration_pass_bins": int(bins["calibration_bin_pass"].sum()) if not bins.empty else 0,
            }
        )
    predictions = concat_frames(rows)
    year_summary = pd.DataFrame(year_rows)
    calibration_bins = concat_frames(bin_rows)
    if predictions.empty:
        model_summary = pd.DataFrame([{"signal_id": "calibrated_probability_model", "signal_name_zh": "概率分桶校准模型", "signal_type": "概率校准模型", "signal_dates": 0, "status": "样本不足"}])
        return predictions, year_summary, model_summary, calibration_bins
    valid_pred = predictions.dropna(subset=[return_col]).copy()
    mask = valid_pred["model_signal"].astype(bool)
    base = v34.valid_panel(panel, policy)[["trade_date", "market_stress_score", "negative_breadth_60d"]].copy()
    merged = valid_pred.merge(base, on="trade_date", how="left")
    pressure_mask = v34.conditions_mask(merged, policy["baseline_pressure_conditions"], logic="all")
    summary = v34.summarize_signal(merged, mask, pressure_mask, "calibrated_probability_model", "概率分桶校准模型", "概率校准模型", policy)
    events = v34.build_nonoverlap_events(merged, mask, "calibrated_probability_model", "概率分桶校准模型", "概率校准模型", policy)
    summary.update(v34.summarize_event_frame(events))
    summary["status"] = v34.classify_summary(summary, policy)
    return predictions, year_summary, pd.DataFrame([summary]), calibration_bins


def build_probability_bins(train: pd.DataFrame, train_prob: np.ndarray, policy: dict[str, Any], year: int) -> tuple[pd.DataFrame, np.ndarray, float]:
    cal = policy["probability_calibration"]
    horizon = int(policy["target_horizon"])
    return_col = f"forward_return_{horizon}d_next_close"
    frame = train[["target_rebound_window", return_col, "is_bad_window"]].copy()
    frame["model_probability"] = train_prob
    train_target_rate = float(frame["target_rebound_window"].mean()) if len(frame) else math.nan
    quantiles = np.linspace(0.0, 1.0, int(cal["bin_count"]) + 1)
    raw_edges = np.nanquantile(frame["model_probability"], quantiles)
    edges = np.unique(raw_edges)
    if len(edges) < 2:
        edges = np.array([float(frame["model_probability"].min()) - 1e-9, float(frame["model_probability"].max()) + 1e-9])
    edges = edges.astype(float)
    edges[0] = -np.inf
    edges[-1] = np.inf
    frame["calibration_bin"] = assign_probability_bins(frame["model_probability"].to_numpy(), edges)
    rows: list[dict[str, Any]] = []
    for bin_id, group in frame.groupby("calibration_bin", dropna=False):
        if pd.isna(bin_id):
            continue
        target_rate = float(group["target_rebound_window"].mean()) if len(group) else math.nan
        mean_return = float(pd.to_numeric(group[return_col], errors="coerce").mean()) if len(group) else math.nan
        bad_rate = float(group["is_bad_window"].mean()) if len(group) else math.nan
        count = int(len(group))
        passed = (
            count >= int(cal["min_train_bin_count"])
            and mean_return >= float(cal["min_bin_mean_return"])
            and bad_rate <= float(cal["max_bin_bad_window_rate"])
            and target_rate >= train_target_rate + float(cal["min_bin_target_rate_edge_vs_train"])
        )
        rows.append(
            {
                "year": int(year),
                "calibration_bin": int(bin_id),
                "probability_left_edge": float(edges[int(bin_id)]) if math.isfinite(float(edges[int(bin_id)])) else None,
                "probability_right_edge": float(edges[int(bin_id) + 1]) if math.isfinite(float(edges[int(bin_id) + 1])) else None,
                "train_bin_count": count,
                "train_bin_target_rate": target_rate,
                "train_bin_mean_return": mean_return,
                "train_bin_bad_window_rate": bad_rate,
                "train_target_rate": train_target_rate,
                "calibration_bin_pass": bool(passed),
            }
        )
    return pd.DataFrame(rows), edges, train_target_rate


def assign_probability_bins(probabilities: np.ndarray | pd.Series, edges: np.ndarray) -> pd.Series:
    values = pd.Series(probabilities)
    labels = list(range(len(edges) - 1))
    return pd.cut(values, bins=edges, labels=labels, include_lowest=True).astype("float").astype("Int64")


def build_filter_audit(policy: dict[str, Any], predictions: pd.DataFrame, calibration_bins: pd.DataFrame, calibrated_summary: pd.DataFrame) -> pd.DataFrame:
    raw_signals = int(predictions["raw_model_signal"].astype(bool).sum()) if not predictions.empty and "raw_model_signal" in predictions.columns else 0
    calibrated_signals = int(predictions["model_signal"].astype(bool).sum()) if not predictions.empty and "model_signal" in predictions.columns else 0
    rejected = raw_signals - calibrated_signals
    passed_bins = int(calibration_bins["calibration_bin_pass"].sum()) if not calibration_bins.empty else 0
    total_bins = int(len(calibration_bins))
    summary = calibrated_summary.iloc[0].to_dict() if not calibrated_summary.empty else {}
    return pd.DataFrame(
        [
            {
                "audit_item": "raw_to_calibrated_signal_filter",
                "status": "pass",
                "raw_model_signal_dates": raw_signals,
                "calibrated_signal_dates": calibrated_signals,
                "rejected_raw_signal_dates": rejected,
                "passed_calibration_bins": passed_bins,
                "total_calibration_bins": total_bins,
                "nonoverlap_events": int(nz(summary.get("nonoverlap_events", 0))),
                "event_mean_return": float_or_none(summary.get("event_mean_return")),
                "event_bad_window_rate": float_or_none(summary.get("event_bad_window_rate")),
                "audit_note": "校准桶表现只用训练期未来收益计算；测试期只能读取训练期桶统计。"
            }
        ]
    )


def build_annual_distribution(predictions: pd.DataFrame, raw_trades: pd.DataFrame, calibrated_trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not predictions.empty:
        for signal_col, source, signal_id in [
            ("raw_model_signal", "raw_model_signal_dates", "raw_probability_model"),
            ("model_signal", "calibrated_signal_dates", "calibrated_probability_model"),
        ]:
            signals = predictions[predictions[signal_col].astype(bool)].copy()
            for year, group in signals.groupby("year"):
                rows.append({"source": source, "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    if not raw_trades.empty:
        for year, group in raw_trades.groupby("year"):
            rows.append({"source": "raw_realtime_trades", "signal_id": "v3_7_raw_model_reference", "year": int(year), "count": int(len(group))})
    if not calibrated_trades.empty:
        for year, group in calibrated_trades.groupby("year"):
            rows.append({"source": "calibrated_realtime_trades", "signal_id": "v4_1_calibrated_probability_realtime", "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def build_top_candidates(raw_summary: pd.DataFrame, model_summary: pd.DataFrame, calibrated_summary: pd.DataFrame) -> pd.DataFrame:
    frames = [frame.copy() for frame in [calibrated_summary, raw_summary, model_summary] if not frame.empty]
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
        "status",
        "signal_dates",
        "nonoverlap_events",
        "active_years",
        "max_single_year_concentration",
        "target_capture_rate",
        "mean_return",
        "pressure_mean_return",
        "mean_edge_vs_pressure",
        "bad_window_rate",
        "event_mean_return",
        "event_win_rate",
        "event_bad_window_rate",
        "event_worst_return",
    ]
    return combined[[col for col in columns if col in combined.columns]]


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame, calibration_bins: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "features use trade-date close/amount-derived fields; realtime entry executes next trading day close",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "train_only_probability_calibration",
                "status": "pass" if not calibration_bins.empty else "fail",
                "evidence": f"calibration_rows={len(calibration_bins)}; test_years={predictions['year'].nunique() if not predictions.empty else 0}",
                "action": "每年概率桶只由训练期收益、坏窗口和目标率生成，测试期不参与桶质量计算。",
            },
            {
                "audit_item": "target_used_only_for_training_and_evaluation",
                "status": "pass",
                "evidence": "test-period future returns are not used to decide calibrated_model_signal",
                "action": "测试期未来收益只进入评价，不进入信号。",
            },
            {
                "audit_item": "purged_walk_forward",
                "status": "pass" if not predictions.empty else "fail",
                "evidence": f"purge_days={policy['model']['purge_days']}; prediction_rows={len(predictions)}",
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


def build_notes(calibrated_summary: pd.DataFrame, raw_summary: pd.DataFrame, filter_audit: pd.DataFrame) -> dict[str, Any]:
    calibrated = calibrated_summary.iloc[0].to_dict() if not calibrated_summary.empty else {}
    raw = raw_summary.iloc[0].to_dict() if not raw_summary.empty else {}
    audit = filter_audit.iloc[0].to_dict() if not filter_audit.empty else {}
    notes: list[str] = []
    if str(calibrated.get("status", "")) == "反弹窗口候选":
        notes.append("V4.1概率分桶校准达到研究候选状态，但仍必须保持research_only并等待更严格复核。")
    else:
        notes.append("V4.1概率分桶校准仍未证明能有效找到反弹窗口。")
    notes.append(
        f"校准后实时仿真：非重叠事件 {int(nz(calibrated.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(calibrated.get('event_mean_return'))}，胜率 {fmt_pct(calibrated.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(calibrated.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"原始模型参考：非重叠事件 {int(nz(raw.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(raw.get('event_mean_return'))}，胜率 {fmt_pct(raw.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(raw.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"概率桶过滤：原始信号 {int(nz(audit.get('raw_model_signal_dates', 0)))}，"
        f"校准信号 {int(nz(audit.get('calibrated_signal_dates', 0)))}，"
        f"拒绝 {int(nz(audit.get('rejected_raw_signal_dates', 0)))}。"
    )
    notes.append("若 V4.1 仍失败，下一步应审计概率桶内失败样本的共同前置特征，判断模型是否在把下跌趋势误判为反弹概率。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "进入V4.2失败样本诊断：按概率桶、年份、压力状态和行业广度组合拆解坏窗口，优先找反向特征而不是继续加阈值。",
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, close_matrix: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, calibrated_summary: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = top[top["status"] == "反弹窗口候选"] if not top.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    calibrated = calibrated_summary.iloc[0].to_dict() if not calibrated_summary.empty else {}
    audit_fail_count = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_target_panel_count": int(len(panel)),
        "industry_count": int(close_matrix.shape[1]),
        "primary_signal_id": "v4_1_calibrated_probability_realtime",
        "primary_realtime_events": int(nz(calibrated.get("nonoverlap_events", 0))),
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
        return "research_only；概率分桶校准尚未证明能有效找到反弹窗口"
    return "research_only；存在概率校准候选但仍需未来样本验证"


def render_report(
    v34: Any,
    summary: dict[str, Any],
    top: pd.DataFrame,
    data_audit: pd.DataFrame,
    target_audit: pd.DataFrame,
    model_year: pd.DataFrame,
    calibration_bins: pd.DataFrame,
    filter_audit: pd.DataFrame,
    raw_summary: pd.DataFrame,
    calibrated_summary: pd.DataFrame,
    calibrated_trades: pd.DataFrame,
    leakage: pd.DataFrame,
    notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines = ["# V4.1 概率分桶校准反弹窗口研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V4.1 不新增人工入场确认阈值，而是检验 V3.7 概率模型是否能用训练期概率桶做实时校准。",
        "",
        f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
        f"- 行业数：{summary['industry_count']}",
        f"- 校准后实时交易数：{summary['primary_realtime_events']}",
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
        "status": "状态",
        "signal_dates": "信号日",
        "nonoverlap_events": "非重叠事件",
        "event_mean_return": "事件收益",
        "event_win_rate": "事件胜率",
        "event_bad_window_rate": "坏窗口",
        "event_worst_return": "最差事件",
    }, {"event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 数据可得性", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 目标标签", ""]
    lines.extend(v34.table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 年度概率校准模型", ""]
    lines.extend(v34.table_or_empty(model_year, {"year": "年份", "status": "状态", "train_rows": "训练样本", "test_rows": "测试样本", "raw_signal_dates": "原始信号", "signal_dates": "校准信号", "signal_target_rate": "目标率", "signal_mean_return": "信号收益", "calibration_pass_bins": "通过桶"}, {"signal_target_rate", "signal_mean_return"}))
    lines += ["", "## 概率桶审计", ""]
    lines.extend(v34.table_or_empty(calibration_bins.head(40), {"year": "年份", "calibration_bin": "桶", "train_bin_count": "训练样本", "train_bin_target_rate": "目标率", "train_bin_mean_return": "均值收益", "train_bin_bad_window_rate": "坏窗口", "calibration_bin_pass": "通过"}, {"train_bin_target_rate", "train_bin_mean_return", "train_bin_bad_window_rate"}))
    lines += ["", "## 过滤审计", ""]
    lines.extend(v34.table_or_empty(filter_audit, {"audit_item": "项目", "status": "状态", "raw_model_signal_dates": "原始信号", "calibrated_signal_dates": "校准信号", "rejected_raw_signal_dates": "拒绝信号", "passed_calibration_bins": "通过桶", "event_mean_return": "事件收益", "event_bad_window_rate": "坏窗口"}, {"event_mean_return", "event_bad_window_rate"}))
    lines += ["", "## 原始模型与校准模型对比", ""]
    lines.extend(v34.table_or_empty(concat_frames([raw_summary, calibrated_summary]), {"signal_name_zh": "名称", "status": "状态", "signal_dates": "信号日", "nonoverlap_events": "交易数", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate"}))
    lines += ["", "## 校准后交易明细", ""]
    lines.extend(v34.table_or_empty(calibrated_trades.head(40), {"signal_date": "信号日", "entry_date": "入场日", "exit_date": "退出日", "holding_days": "持有日", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
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
        "- `report.md`：中文 V4.1 研究报告，优先打开。",
        "- `top_candidates.csv`：原始模型、校准模型和实时仿真排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：特征面板、概率桶、校准过滤审计、实时仿真、年度分布、泄漏审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def normalize_raw_labels(trades: pd.DataFrame, summary: pd.DataFrame) -> None:
    if not trades.empty and "signal_id" in trades.columns:
        trades["signal_id"] = "v3_7_raw_model_reference"
    if not summary.empty:
        if "signal_id" in summary.columns:
            summary["signal_id"] = "v3_7_raw_model_reference"
        if "signal_name_zh" in summary.columns:
            summary["signal_name_zh"] = "V3.7原始概率模型参考"


def normalize_calibrated_labels(trades: pd.DataFrame, summary: pd.DataFrame) -> None:
    if not trades.empty and "signal_id" in trades.columns:
        trades["signal_id"] = "v4_1_calibrated_probability_realtime"
    if not summary.empty:
        if "signal_id" in summary.columns:
            summary["signal_id"] = "v4_1_calibrated_probability_realtime"
        if "signal_name_zh" in summary.columns:
            summary["signal_name_zh"] = "V4.1概率分桶校准实时仿真"
        if "signal_type" in summary.columns:
            summary["signal_type"] = "概率校准实时仿真"


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
