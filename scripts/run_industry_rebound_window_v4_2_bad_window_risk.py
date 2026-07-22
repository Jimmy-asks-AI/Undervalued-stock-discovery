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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_2_bad_window_risk_policy.json"
V37_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v3_7_industry_breadth.py"
VERSION = "4.2.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.2 bad-window risk rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V4.2 policy JSON.")
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
    predictions, year_summary, model_summary, threshold_audit, bad_risk_bins = run_bad_window_risk_model(panel, policy, v34)

    raw_predictions = predictions.copy()
    if not raw_predictions.empty:
        raw_predictions["model_signal"] = raw_predictions["raw_model_signal"]
    raw_trades, raw_summary = v34.run_realtime_simulation(panel, raw_predictions, policy)
    normalize_raw_labels(raw_trades, raw_summary)
    filtered_trades, filtered_summary = v34.run_realtime_simulation(panel, predictions, policy)
    normalize_filtered_labels(filtered_trades, filtered_summary)

    failure_profile = build_failure_profile(predictions, policy)
    feature_contrast = build_failure_feature_contrast(predictions, policy)
    filter_audit = build_filter_audit(policy, predictions, raw_summary, filtered_summary)
    annual_distribution = build_annual_distribution(predictions, raw_trades, filtered_trades)
    top_candidates = build_top_candidates(raw_summary, model_summary, filtered_summary)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions, threshold_audit)
    notes = build_notes(filtered_summary, raw_summary, filter_audit, failure_profile)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, filtered_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v42_bad_window_feature_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    breadth_audit.to_csv(debug_dir / "breadth_feature_audit.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    threshold_audit.to_csv(debug_dir / "bad_window_threshold_audit.csv", index=False, encoding="utf-8-sig")
    bad_risk_bins.to_csv(debug_dir / "bad_window_risk_bins.csv", index=False, encoding="utf-8-sig")
    raw_trades.to_csv(debug_dir / "raw_model_realtime_trades.csv", index=False, encoding="utf-8-sig")
    raw_summary.to_csv(debug_dir / "raw_model_realtime_summary.csv", index=False, encoding="utf-8-sig")
    filtered_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    filtered_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    filter_audit.to_csv(debug_dir / "bad_window_filter_audit.csv", index=False, encoding="utf-8-sig")
    failure_profile.to_csv(debug_dir / "failure_case_profile.csv", index=False, encoding="utf-8-sig")
    feature_contrast.to_csv(debug_dir / "failure_feature_contrast.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, year_summary, threshold_audit, bad_risk_bins, filter_audit, failure_profile, raw_summary, filtered_summary, filtered_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V4.2坏窗口风险识别反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"过滤后实时交易数={run_summary['primary_realtime_events']}")
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


def run_bad_window_risk_model(panel: pd.DataFrame, policy: dict[str, Any], v34: Any) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    model_policy = policy["model"]
    bad_policy = policy["bad_window_model"]
    features = list(model_policy["features"])
    bad_features = list(bad_policy["features"])
    valid = v34.valid_panel(panel, policy).copy()
    horizon = int(policy["target_horizon"])
    return_col = f"forward_return_{horizon}d_next_close"
    dd_col = f"forward_max_drawdown_{horizon}d_next_close"
    rows: list[pd.DataFrame] = []
    year_rows: list[dict[str, Any]] = []
    threshold_rows: list[dict[str, Any]] = []
    bad_bin_rows: list[pd.DataFrame] = []

    for year in range(int(model_policy["test_start_year"]), int(model_policy["test_end_year"]) + 1):
        test_start = pd.Timestamp(f"{year}-01-01")
        test_end = pd.Timestamp(f"{year}-12-31")
        train_end = test_start - pd.Timedelta(days=int(model_policy["purge_days"]))
        train = valid[valid["trade_date"] < train_end].dropna(subset=features + bad_features + ["target_rebound_window", "is_bad_window"]).copy()
        test = valid[(valid["trade_date"] >= test_start) & (valid["trade_date"] <= test_end)].dropna(subset=features + bad_features + ["target_rebound_window", "is_bad_window"]).copy()
        if len(train) < int(model_policy["train_min_rows"]) or test.empty:
            year_rows.append({"year": year, "status": "skip", "train_rows": len(train), "test_rows": len(test), "signal_dates": 0})
            continue

        x_train, x_test = standardize_train_test(train, test, features)
        rebound_weights = v34.fit_logistic(x_train, train["target_rebound_window"].astype(float).to_numpy(), model_policy)
        train_rebound_prob = v34.predict_logistic(x_train, rebound_weights)
        test_rebound_prob = v34.predict_logistic(x_test, rebound_weights)

        x_bad_train, x_bad_test = standardize_train_test(train, test, bad_features)
        bad_weights = v34.fit_logistic(x_bad_train, train["is_bad_window"].astype(float).to_numpy(), bad_policy)
        train_bad_prob = v34.predict_logistic(x_bad_train, bad_weights)
        test_bad_prob = v34.predict_logistic(x_bad_test, bad_weights)

        pressure_train = v34.conditions_mask(train, policy["baseline_pressure_conditions"], logic="all").to_numpy(dtype=bool)
        threshold_source = train_rebound_prob[pressure_train] if pressure_train.any() else train_rebound_prob
        rebound_threshold = max(float(model_policy["minimum_probability_threshold"]), float(np.nanquantile(threshold_source, float(model_policy["probability_quantile"]))))
        train_raw_signal = train_rebound_prob >= rebound_threshold
        bad_threshold = build_bad_risk_threshold(train_bad_prob, train_raw_signal, policy)

        output_cols = unique_columns(["trade_date", "year", "market_nav", "target_rebound_window", return_col, dd_col, "is_bad_window"] + features)
        test_out = test[output_cols].copy()
        test_out["model_probability"] = test_rebound_prob
        test_out["model_threshold"] = rebound_threshold
        test_out["probability_margin"] = test_out["model_probability"] - test_out["model_threshold"]
        test_out["bad_window_probability"] = test_bad_prob
        test_out["bad_window_threshold"] = bad_threshold
        test_out["raw_model_signal"] = test_out["model_probability"] >= rebound_threshold
        test_out["bad_risk_pass"] = test_out["bad_window_probability"] <= bad_threshold
        test_out["model_signal"] = test_out["raw_model_signal"].astype(bool) & test_out["bad_risk_pass"].astype(bool)
        test_out["signal_id"] = "bad_risk_filtered_probability_model"
        rows.append(test_out)

        raw_signal = test_out[test_out["raw_model_signal"]]
        filtered_signal = test_out[test_out["model_signal"]]
        year_rows.append(
            {
                "year": year,
                "status": "pass",
                "train_rows": len(train),
                "train_target_rate": float(train["target_rebound_window"].mean()),
                "train_bad_window_rate": float(train["is_bad_window"].mean()),
                "test_rows": len(test),
                "threshold": rebound_threshold,
                "bad_window_threshold": bad_threshold,
                "raw_signal_dates": int(len(raw_signal)),
                "signal_dates": int(len(filtered_signal)),
                "signal_target_rate": float(filtered_signal["target_rebound_window"].mean()) if len(filtered_signal) else math.nan,
                "signal_mean_return": float(filtered_signal[return_col].mean()) if len(filtered_signal) else math.nan,
                "signal_bad_window_rate": float(filtered_signal["is_bad_window"].mean()) if len(filtered_signal) else math.nan,
            }
        )
        threshold_rows.append(
            {
                "year": year,
                "train_rows": len(train),
                "train_raw_signal_count": int(train_raw_signal.sum()),
                "rebound_threshold": rebound_threshold,
                "bad_window_threshold": bad_threshold,
                "train_bad_probability_mean": float(np.nanmean(train_bad_prob)),
                "train_raw_bad_probability_mean": float(np.nanmean(train_bad_prob[train_raw_signal])) if train_raw_signal.any() else math.nan,
                "threshold_source": "train_raw_signals" if int(train_raw_signal.sum()) >= int(policy["bad_window_filter"]["min_train_raw_signals_for_threshold"]) else "all_train_dates",
                "allowed_bad_risk_quantile": float(policy["bad_window_filter"]["allowed_bad_risk_quantile"]),
                "maximum_bad_probability_threshold": float(policy["bad_window_filter"]["maximum_bad_probability_threshold"]),
            }
        )
        bad_bin_rows.append(build_bad_risk_bins_for_year(test_out, policy, year))

    predictions = concat_frames(rows)
    year_summary = pd.DataFrame(year_rows)
    threshold_audit = pd.DataFrame(threshold_rows)
    bad_risk_bins = concat_frames(bad_bin_rows)
    if predictions.empty:
        model_summary = pd.DataFrame([empty_model_summary()])
        return predictions, year_summary, model_summary, threshold_audit, bad_risk_bins

    valid_pred = predictions.dropna(subset=[return_col]).copy()
    mask = valid_pred["model_signal"].astype(bool)
    base = v34.valid_panel(panel, policy)[["trade_date", "market_stress_score", "negative_breadth_60d"]].copy()
    merged = valid_pred.merge(base, on="trade_date", how="left")
    pressure_mask = v34.conditions_mask(merged, policy["baseline_pressure_conditions"], logic="all")
    summary = v34.summarize_signal(merged, mask, pressure_mask, "bad_risk_filtered_probability_model", "坏窗口风险过滤概率模型", "失败风险模型", policy)
    events = v34.build_nonoverlap_events(merged, mask, "bad_risk_filtered_probability_model", "坏窗口风险过滤概率模型", "失败风险模型", policy)
    summary.update(v34.summarize_event_frame(events))
    summary["status"] = v34.classify_summary(summary, policy)
    return predictions, year_summary, pd.DataFrame([summary]), threshold_audit, bad_risk_bins


def standardize_train_test(train: pd.DataFrame, test: pd.DataFrame, features: list[str]) -> tuple[np.ndarray, np.ndarray]:
    x_train = train[features].astype(float).to_numpy()
    x_test = test[features].astype(float).to_numpy()
    mean = np.nanmean(x_train, axis=0)
    std = np.nanstd(x_train, axis=0)
    std = np.where(std < 1e-9, 1.0, std)
    x_train_z = np.nan_to_num((x_train - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)
    x_test_z = np.nan_to_num((x_test - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)
    return x_train_z, x_test_z


def unique_columns(columns: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for column in columns:
        if column in seen:
            continue
        seen.add(column)
        output.append(column)
    return output


def build_bad_risk_threshold(train_bad_prob: np.ndarray, train_raw_signal: np.ndarray, policy: dict[str, Any]) -> float:
    filt = policy["bad_window_filter"]
    if int(train_raw_signal.sum()) >= int(filt["min_train_raw_signals_for_threshold"]):
        source = train_bad_prob[train_raw_signal]
    else:
        source = train_bad_prob
    quantile_threshold = float(np.nanquantile(source, float(filt["allowed_bad_risk_quantile"])))
    return min(quantile_threshold, float(filt["maximum_bad_probability_threshold"]))


def build_bad_risk_bins_for_year(frame: pd.DataFrame, policy: dict[str, Any], year: int) -> pd.DataFrame:
    raw = frame[frame["raw_model_signal"]].copy()
    if raw.empty:
        return pd.DataFrame()
    try:
        raw["bad_risk_bin"] = pd.qcut(raw["bad_window_probability"], q=4, labels=False, duplicates="drop")
    except ValueError:
        raw["bad_risk_bin"] = 0
    rows: list[dict[str, Any]] = []
    horizon = int(policy["target_horizon"])
    return_col = f"forward_return_{horizon}d_next_close"
    for bin_id, group in raw.groupby("bad_risk_bin", dropna=False):
        rows.append(
            {
                "year": int(year),
                "bad_risk_bin": int(bin_id) if not pd.isna(bin_id) else -1,
                "rows": int(len(group)),
                "bad_window_probability_mean": float(pd.to_numeric(group["bad_window_probability"], errors="coerce").mean()),
                "target_rate": float(group["target_rebound_window"].mean()),
                "mean_return": float(pd.to_numeric(group[return_col], errors="coerce").mean()),
                "bad_window_rate": float(group["is_bad_window"].mean()),
                "model_signal_count": int(group["model_signal"].astype(bool).sum()),
            }
        )
    return pd.DataFrame(rows)


def build_failure_profile(predictions: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if predictions.empty:
        return pd.DataFrame()
    raw = predictions[predictions["raw_model_signal"].astype(bool)].copy()
    if raw.empty:
        return pd.DataFrame()
    horizon = int(policy["target_horizon"])
    return_col = f"forward_return_{horizon}d_next_close"
    rows: list[dict[str, Any]] = []
    groups = [
        ("all_raw_signals", raw),
        ("passed_bad_risk_filter", raw[raw["model_signal"].astype(bool)]),
        ("rejected_by_bad_risk_filter", raw[~raw["model_signal"].astype(bool)]),
        ("realized_bad_windows", raw[raw["is_bad_window"].astype(bool)]),
        ("realized_non_bad_windows", raw[~raw["is_bad_window"].astype(bool)]),
    ]
    for name, frame in groups:
        rows.append(
            {
                "profile": name,
                "rows": int(len(frame)),
                "mean_return": float(pd.to_numeric(frame[return_col], errors="coerce").mean()) if len(frame) else math.nan,
                "win_rate": float((pd.to_numeric(frame[return_col], errors="coerce") > 0).mean()) if len(frame) else math.nan,
                "target_rate": float(frame["target_rebound_window"].mean()) if len(frame) else math.nan,
                "bad_window_rate": float(frame["is_bad_window"].mean()) if len(frame) else math.nan,
                "model_probability_mean": float(pd.to_numeric(frame["model_probability"], errors="coerce").mean()) if len(frame) else math.nan,
                "bad_window_probability_mean": float(pd.to_numeric(frame["bad_window_probability"], errors="coerce").mean()) if len(frame) else math.nan,
                "market_return_20d_mean": float(pd.to_numeric(frame.get("market_return_20d", pd.Series(dtype=float)), errors="coerce").mean()) if len(frame) else math.nan,
                "breadth_recovery_score_mean": float(pd.to_numeric(frame.get("breadth_recovery_score", pd.Series(dtype=float)), errors="coerce").mean()) if len(frame) else math.nan,
            }
        )
    return pd.DataFrame(rows)


def build_failure_feature_contrast(predictions: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if predictions.empty:
        return pd.DataFrame()
    raw = predictions[predictions["raw_model_signal"].astype(bool)].copy()
    bad = raw[raw["is_bad_window"].astype(bool)]
    good = raw[~raw["is_bad_window"].astype(bool)]
    rows: list[dict[str, Any]] = []
    for feature in policy["model"]["features"] + ["bad_window_probability", "model_probability", "probability_margin"]:
        bad_series = pd.to_numeric(bad.get(feature, pd.Series(dtype=float)), errors="coerce")
        good_series = pd.to_numeric(good.get(feature, pd.Series(dtype=float)), errors="coerce")
        bad_mean = float_or_none(bad_series.mean())
        good_mean = float_or_none(good_series.mean())
        rows.append(
            {
                "feature": feature,
                "bad_window_mean": bad_mean,
                "non_bad_window_mean": good_mean,
                "bad_minus_non_bad": None if bad_mean is None or good_mean is None else bad_mean - good_mean,
                "bad_count": int(bad_series.notna().sum()),
                "non_bad_count": int(good_series.notna().sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("bad_minus_non_bad", key=lambda series: series.abs(), ascending=False, na_position="last")


def build_filter_audit(policy: dict[str, Any], predictions: pd.DataFrame, raw_summary: pd.DataFrame, filtered_summary: pd.DataFrame) -> pd.DataFrame:
    raw_signals = int(predictions["raw_model_signal"].astype(bool).sum()) if not predictions.empty else 0
    filtered_signals = int(predictions["model_signal"].astype(bool).sum()) if not predictions.empty else 0
    raw = raw_summary.iloc[0].to_dict() if not raw_summary.empty else {}
    filtered = filtered_summary.iloc[0].to_dict() if not filtered_summary.empty else {}
    return pd.DataFrame(
        [
            {
                "audit_item": "bad_window_risk_filter",
                "status": "pass",
                "raw_model_signal_dates": raw_signals,
                "filtered_signal_dates": filtered_signals,
                "rejected_signal_dates": raw_signals - filtered_signals,
                "raw_nonoverlap_events": int(nz(raw.get("nonoverlap_events", 0))),
                "filtered_nonoverlap_events": int(nz(filtered.get("nonoverlap_events", 0))),
                "raw_event_mean_return": float_or_none(raw.get("event_mean_return")),
                "filtered_event_mean_return": float_or_none(filtered.get("event_mean_return")),
                "raw_bad_window_rate": float_or_none(raw.get("event_bad_window_rate")),
                "filtered_bad_window_rate": float_or_none(filtered.get("event_bad_window_rate")),
                "audit_note": "坏窗口风险阈值只用训练期预测概率决定，测试期未来收益不参与过滤。",
            }
        ]
    )


def build_annual_distribution(predictions: pd.DataFrame, raw_trades: pd.DataFrame, filtered_trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not predictions.empty:
        for signal_col, source, signal_id in [
            ("raw_model_signal", "raw_model_signal_dates", "raw_probability_model"),
            ("model_signal", "bad_risk_filtered_signal_dates", "bad_risk_filtered_probability_model"),
        ]:
            signals = predictions[predictions[signal_col].astype(bool)]
            for year, group in signals.groupby("year"):
                rows.append({"source": source, "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    if not raw_trades.empty:
        for year, group in raw_trades.groupby("year"):
            rows.append({"source": "raw_realtime_trades", "signal_id": "v3_7_raw_model_reference", "year": int(year), "count": int(len(group))})
    if not filtered_trades.empty:
        for year, group in filtered_trades.groupby("year"):
            rows.append({"source": "filtered_realtime_trades", "signal_id": "v4_2_bad_window_risk_realtime", "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def build_top_candidates(raw_summary: pd.DataFrame, model_summary: pd.DataFrame, filtered_summary: pd.DataFrame) -> pd.DataFrame:
    combined = concat_frames([filtered_summary, raw_summary, model_summary])
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


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame, threshold_audit: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "rebound and bad-window models use trade-date close/amount-derived fields only",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "train_only_bad_risk_threshold",
                "status": "pass" if not threshold_audit.empty else "fail",
                "evidence": f"threshold_rows={len(threshold_audit)}; test_years={predictions['year'].nunique() if not predictions.empty else 0}",
                "action": "每年坏窗口阈值只用训练期坏窗口概率生成。",
            },
            {
                "audit_item": "target_used_only_for_training_and_evaluation",
                "status": "pass",
                "evidence": "test-period future returns and bad-window labels are not used to decide model_signal",
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


def build_notes(filtered_summary: pd.DataFrame, raw_summary: pd.DataFrame, filter_audit: pd.DataFrame, failure_profile: pd.DataFrame) -> dict[str, Any]:
    filtered = filtered_summary.iloc[0].to_dict() if not filtered_summary.empty else {}
    raw = raw_summary.iloc[0].to_dict() if not raw_summary.empty else {}
    audit = filter_audit.iloc[0].to_dict() if not filter_audit.empty else {}
    notes: list[str] = []
    if str(filtered.get("status", "")) == "反弹窗口候选":
        notes.append("V4.2坏窗口风险过滤达到研究候选状态，但仍必须保持research_only并等待更严格复核。")
    else:
        notes.append("V4.2坏窗口风险过滤仍未证明能有效找到反弹窗口。")
    notes.append(
        f"过滤后实时仿真：非重叠事件 {int(nz(filtered.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(filtered.get('event_mean_return'))}，胜率 {fmt_pct(filtered.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(filtered.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"原始模型参考：非重叠事件 {int(nz(raw.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(raw.get('event_mean_return'))}，胜率 {fmt_pct(raw.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(raw.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"坏风险过滤：原始信号 {int(nz(audit.get('raw_model_signal_dates', 0)))}，"
        f"过滤后信号 {int(nz(audit.get('filtered_signal_dates', 0)))}，"
        f"拒绝 {int(nz(audit.get('rejected_signal_dates', 0)))}。"
    )
    if not failure_profile.empty:
        bad_row = failure_profile[failure_profile["profile"] == "realized_bad_windows"]
        if not bad_row.empty:
            item = bad_row.iloc[0].to_dict()
            notes.append(
                f"坏窗口画像：坏窗口平均坏风险概率 {fmt_pct(item.get('bad_window_probability_mean'))}，"
                f"平均20日市场收益 {fmt_pct(item.get('market_return_20d_mean'))}。"
            )
    notes.append("若 V4.2 仍失败，下一步应停止在同一组价格广度特征上训练更多模型，转向更独立的数据或承认当前可得数据不足。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "优先评估是否接入更独立的外生风险偏好、政策/信用或交易拥挤度数据；若仍使用当前数据，应转为失败归因报告而不是继续堆模型。",
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, close_matrix: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, filtered_summary: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = top[top["status"] == "反弹窗口候选"] if not top.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    filtered = filtered_summary.iloc[0].to_dict() if not filtered_summary.empty else {}
    audit_fail_count = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_target_panel_count": int(len(panel)),
        "industry_count": int(close_matrix.shape[1]),
        "primary_signal_id": "v4_2_bad_window_risk_realtime",
        "primary_realtime_events": int(nz(filtered.get("nonoverlap_events", 0))),
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
        return "research_only；坏窗口风险过滤尚未证明能有效找到反弹窗口"
    return "research_only；存在坏窗口风险过滤候选但仍需未来样本验证"


def render_report(
    v34: Any,
    summary: dict[str, Any],
    top: pd.DataFrame,
    data_audit: pd.DataFrame,
    target_audit: pd.DataFrame,
    year_summary: pd.DataFrame,
    threshold_audit: pd.DataFrame,
    bad_risk_bins: pd.DataFrame,
    filter_audit: pd.DataFrame,
    failure_profile: pd.DataFrame,
    raw_summary: pd.DataFrame,
    filtered_summary: pd.DataFrame,
    filtered_trades: pd.DataFrame,
    leakage: pd.DataFrame,
    notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines = ["# V4.2 坏窗口风险识别反弹窗口研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V4.2 在 V3.7 原始反弹概率模型基础上，另行训练坏窗口风险模型，只允许坏窗口风险低于训练期阈值的原始信号触发。",
        "",
        f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
        f"- 行业数：{summary['industry_count']}",
        f"- 过滤后实时交易数：{summary['primary_realtime_events']}",
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
    lines += ["", "## 年度坏风险模型", ""]
    lines.extend(v34.table_or_empty(year_summary, {"year": "年份", "status": "状态", "train_rows": "训练样本", "test_rows": "测试样本", "raw_signal_dates": "原始信号", "signal_dates": "过滤后信号", "signal_mean_return": "信号收益", "signal_bad_window_rate": "坏窗口率", "bad_window_threshold": "坏风险阈值"}, {"signal_mean_return", "signal_bad_window_rate", "bad_window_threshold"}))
    lines += ["", "## 坏风险阈值审计", ""]
    lines.extend(v34.table_or_empty(threshold_audit, {"year": "年份", "train_rows": "训练样本", "train_raw_signal_count": "训练原始信号", "bad_window_threshold": "坏风险阈值", "threshold_source": "阈值来源", "train_raw_bad_probability_mean": "训练原始信号坏风险"}, {"bad_window_threshold", "train_raw_bad_probability_mean"}))
    lines += ["", "## 坏风险分桶", ""]
    lines.extend(v34.table_or_empty(bad_risk_bins.head(40), {"year": "年份", "bad_risk_bin": "坏风险桶", "rows": "行数", "bad_window_probability_mean": "坏风险均值", "target_rate": "目标率", "mean_return": "均值收益", "bad_window_rate": "坏窗口率", "model_signal_count": "过滤后信号"}, {"bad_window_probability_mean", "target_rate", "mean_return", "bad_window_rate"}))
    lines += ["", "## 过滤审计", ""]
    lines.extend(v34.table_or_empty(filter_audit, {"audit_item": "项目", "status": "状态", "raw_model_signal_dates": "原始信号", "filtered_signal_dates": "过滤后信号", "rejected_signal_dates": "拒绝信号", "raw_event_mean_return": "原始收益", "filtered_event_mean_return": "过滤后收益", "raw_bad_window_rate": "原始坏窗口", "filtered_bad_window_rate": "过滤后坏窗口"}, {"raw_event_mean_return", "filtered_event_mean_return", "raw_bad_window_rate", "filtered_bad_window_rate"}))
    lines += ["", "## 失败样本画像", ""]
    lines.extend(v34.table_or_empty(failure_profile, {"profile": "画像", "rows": "行数", "mean_return": "均值收益", "win_rate": "胜率", "target_rate": "目标率", "bad_window_rate": "坏窗口率", "model_probability_mean": "反弹概率", "bad_window_probability_mean": "坏风险概率", "market_return_20d_mean": "20日市场收益", "breadth_recovery_score_mean": "广度修复"}, {"mean_return", "win_rate", "target_rate", "bad_window_rate", "model_probability_mean", "bad_window_probability_mean", "market_return_20d_mean"}))
    lines += ["", "## 原始模型与过滤模型对比", ""]
    lines.extend(v34.table_or_empty(concat_frames([raw_summary, filtered_summary]), {"signal_name_zh": "名称", "status": "状态", "signal_dates": "信号日", "nonoverlap_events": "交易数", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate"}))
    lines += ["", "## 过滤后交易明细", ""]
    lines.extend(v34.table_or_empty(filtered_trades.head(40), {"signal_date": "信号日", "entry_date": "入场日", "exit_date": "退出日", "holding_days": "持有日", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
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
        "- `report.md`：中文 V4.2 研究报告，优先打开。",
        "- `top_candidates.csv`：原始模型、坏风险过滤模型和实时仿真排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：特征面板、坏风险阈值、坏风险分桶、失败画像、实时仿真、年度分布、泄漏审计和冻结策略。",
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


def normalize_filtered_labels(trades: pd.DataFrame, summary: pd.DataFrame) -> None:
    if not trades.empty and "signal_id" in trades.columns:
        trades["signal_id"] = "v4_2_bad_window_risk_realtime"
    if not summary.empty:
        if "signal_id" in summary.columns:
            summary["signal_id"] = "v4_2_bad_window_risk_realtime"
        if "signal_name_zh" in summary.columns:
            summary["signal_name_zh"] = "V4.2坏窗口风险过滤实时仿真"
        if "signal_type" in summary.columns:
            summary["signal_type"] = "坏风险过滤实时仿真"


def empty_model_summary() -> dict[str, Any]:
    return {"signal_id": "bad_risk_filtered_probability_model", "signal_name_zh": "坏窗口风险过滤概率模型", "signal_type": "失败风险模型", "signal_dates": 0, "status": "样本不足"}


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
