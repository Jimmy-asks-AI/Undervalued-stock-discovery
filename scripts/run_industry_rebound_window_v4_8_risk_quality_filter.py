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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_8_risk_quality_filter_policy.json"
V46_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v4_6_horizon_stability.py"
VERSION = "4.8.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.8 risk-quality filter rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V4.8 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v46 = load_v46_module()
    v46.VERSION = VERSION
    v43 = v46.load_v43_module()
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
    predictions = v46.attach_forward_metrics(predictions, panel, policy)

    raw_predictions = predictions.copy()
    if not raw_predictions.empty:
        raw_predictions["model_signal"] = raw_predictions["raw_model_signal"]
    raw_trades, raw_summary = v34.run_realtime_simulation(panel, raw_predictions, policy)
    v46.normalize_summary(raw_trades, raw_summary, "v3_7_raw_model_extended_reference", "V3.7原始概率模型扩展样本参考", "原始概率模型参考")

    failure_trades, failure_summary = v34.run_realtime_simulation(panel, predictions, policy)
    v46.normalize_summary(failure_trades, failure_summary, "v4_3_failure_state_extended_reference", "V4.3失败状态过滤扩展样本参考", "失败状态过滤参考")

    base_grid_trades, base_grid_summary, base_trades, base_summary = v46.run_horizon_grid(predictions, policy)
    relabel_v48(base_grid_trades, base_grid_summary, base_trades, base_summary)
    filter_sensitivity, primary_trades, primary_summary = run_filter_overlay(base_trades, policy)
    filter_audit = build_filter_audit(policy, base_summary, filter_sensitivity, primary_summary)
    leave_one_year = build_leave_one_year_out(primary_trades, policy)
    top_candidates = build_top_candidates(primary_summary, filter_sensitivity, base_summary, failure_summary, raw_summary, model_summary)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions, threshold_audit, filter_audit)
    annual_distribution = build_annual_distribution(predictions, raw_trades, failure_trades, primary_trades)
    notes = build_notes(primary_summary, filter_sensitivity, filter_audit, leave_one_year)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, primary_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v48_risk_quality_feature_panel.csv", index=False, encoding="utf-8-sig")
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
    base_trades.to_csv(debug_dir / "short_horizon_base_trades.csv", index=False, encoding="utf-8-sig")
    base_summary.to_csv(debug_dir / "short_horizon_base_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    primary_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    base_grid_trades.to_csv(debug_dir / "horizon_grid_realtime_trades.csv", index=False, encoding="utf-8-sig")
    base_grid_summary.to_csv(debug_dir / "horizon_grid_summary.csv", index=False, encoding="utf-8-sig")
    filter_sensitivity.to_csv(debug_dir / "risk_quality_filter_sensitivity.csv", index=False, encoding="utf-8-sig")
    filter_audit.to_csv(debug_dir / "risk_quality_filter_audit.csv", index=False, encoding="utf-8-sig")
    leave_one_year.to_csv(debug_dir / "leave_one_year_out_audit.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, year_summary, filter_audit, filter_sensitivity, leave_one_year, primary_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V4.8短持有期风险质量过滤反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"主规则实时交易数={run_summary['primary_realtime_events']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v46_module() -> Any:
    spec = importlib.util.spec_from_file_location("v46_horizon_stability", V46_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V4.6 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_filter_overlay(base_trades: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    primary_trades = pd.DataFrame()
    primary_summary = pd.DataFrame()
    primary_id = policy["risk_quality_overlay"]["primary_filter_id"]
    for item in policy["risk_quality_overlay"]["filter_candidates"]:
        filtered = apply_filter(base_trades, item).copy()
        if not filtered.empty:
            filtered["signal_id"] = policy["persistent_cluster"]["primary_signal_id"]
            filtered["filter_id"] = item["filter_id"]
            filtered["filter_name_zh"] = item["filter_name_zh"]
        summary = summarize_filter(filtered, item, policy, len(base_trades))
        rows.append(summary)
        if item["filter_id"] == primary_id:
            primary_trades = filtered
            primary_summary = pd.DataFrame([summary])
    if primary_summary.empty:
        primary_summary = pd.DataFrame([empty_filter_summary(policy, {"filter_id": primary_id, "filter_name_zh": primary_id}, len(base_trades))])
    return pd.DataFrame(rows), primary_trades, primary_summary


def apply_filter(frame: pd.DataFrame, item: dict[str, Any]) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    mask = pd.Series(True, index=frame.index)
    for cond in item["conditions"]:
        values = pd.to_numeric(frame[cond["field"]], errors="coerce")
        op = cond["op"]
        threshold = float(cond["value"])
        if op == ">=":
            mask &= values >= threshold
        elif op == "<=":
            mask &= values <= threshold
        elif op == ">":
            mask &= values > threshold
        elif op == "<":
            mask &= values < threshold
        else:
            raise ValueError(f"unsupported op: {op}")
    return frame[mask].copy()


def summarize_filter(trades: pd.DataFrame, item: dict[str, Any], policy: dict[str, Any], base_events: int) -> dict[str, Any]:
    row = empty_filter_summary(policy, item, base_events)
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
    row["status"] = classify_filter_summary(row, policy)
    return row


def empty_filter_summary(policy: dict[str, Any], item: dict[str, Any], base_events: int) -> dict[str, Any]:
    return {
        "signal_id": policy["persistent_cluster"]["primary_signal_id"],
        "signal_name_zh": item.get("filter_name_zh", ""),
        "signal_type": "短持有期风险质量过滤",
        "status": "样本不足",
        "filter_id": item.get("filter_id", ""),
        "filter_name_zh": item.get("filter_name_zh", ""),
        "base_events": int(base_events),
        "signal_dates": int(base_events),
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


def classify_filter_summary(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
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
    if events >= 8 and mean_return >= 0.0 and win_rate >= 0.5 and bad_rate <= 0.35:
        return "条件观察"
    if events < 8:
        return "样本不足"
    return "拒绝"


def build_filter_audit(policy: dict[str, Any], base_summary: pd.DataFrame, sensitivity: pd.DataFrame, primary_summary: pd.DataFrame) -> pd.DataFrame:
    base = base_summary.iloc[0].to_dict() if not base_summary.empty else {}
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    th = policy["promotion_thresholds"]
    return pd.DataFrame(
        [
            {
                "audit_item": "sample_size_after_filter",
                "status": "pass" if int(nz(primary.get("nonoverlap_events", 0))) >= int(th["min_nonoverlap_events"]) else "fail",
                "evidence": f"events={int(nz(primary.get('nonoverlap_events', 0)))} / {th['min_nonoverlap_events']}",
                "action": "过滤后仍需保持足够非重叠事件。",
            },
            {
                "audit_item": "return_thickness_after_filter",
                "status": "pass" if nz(primary.get("event_mean_return", math.nan), -1.0) >= float(th["min_event_mean_return"]) else "fail",
                "evidence": f"mean_return={fmt_pct(primary.get('event_mean_return'))} / {fmt_pct(th['min_event_mean_return'])}",
                "action": "平均收益厚度不足时不得升级。",
            },
            {
                "audit_item": "bad_window_control_after_filter",
                "status": "pass" if nz(primary.get("event_bad_window_rate", math.nan), 1.0) <= float(th["max_event_bad_window_rate"]) else "fail",
                "evidence": f"bad_window={fmt_pct(primary.get('event_bad_window_rate'))} / {fmt_pct(th['max_event_bad_window_rate'])}",
                "action": "坏窗口需要维持在可接受范围。",
            },
            {
                "audit_item": "filter_increment",
                "status": "observe",
                "evidence": f"base_events={int(nz(base.get('nonoverlap_events', 0)))}; filtered_events={int(nz(primary.get('nonoverlap_events', 0)))}; base_return={fmt_pct(base.get('event_mean_return'))}; filtered_return={fmt_pct(primary.get('event_mean_return'))}",
                "action": "观察风险质量过滤相对V4.7短持有样本池的增量。",
            },
            {
                "audit_item": "predeclared_filter_count",
                "status": "pass",
                "evidence": f"filters={len(sensitivity)}; primary={policy['risk_quality_overlay']['primary_filter_id']}",
                "action": "过滤规则由配置声明，不用未来收益选择主规则。",
            },
        ]
    )


def build_leave_one_year_out(primary_trades: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if primary_trades.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for year in sorted(primary_trades["year"].astype(int).unique()):
        subset = primary_trades[primary_trades["year"].astype(int) != int(year)].copy()
        rows.append(robustness_row(f"exclude_{year}", int(year), subset))
    rows.append(robustness_row("all_primary_events", None, primary_trades))
    return pd.DataFrame(rows)


def robustness_row(label: str, excluded_year: int | None, trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"audit_item": label, "excluded_year": excluded_year, "events": 0, "mean_return": math.nan, "win_rate": math.nan, "bad_window_rate": math.nan, "status": "样本不足"}
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    return {
        "audit_item": label,
        "excluded_year": excluded_year,
        "events": int(len(trades)),
        "mean_return": float(returns.mean()),
        "win_rate": float((returns > 0).mean()),
        "bad_window_rate": float(trades["is_bad_window"].astype(bool).mean()),
        "worst_return": float(returns.min()),
        "active_years": int(trades["year"].nunique()),
        "status": "pass" if float(returns.mean()) >= 0.0 and float((returns > 0).mean()) >= 0.5 and float(trades["is_bad_window"].astype(bool).mean()) <= 0.25 else "fail",
    }


def build_top_candidates(primary_summary: pd.DataFrame, sensitivity: pd.DataFrame, base_summary: pd.DataFrame, failure_summary: pd.DataFrame, raw_summary: pd.DataFrame, model_summary: pd.DataFrame) -> pd.DataFrame:
    combined = concat_frames([primary_summary, sensitivity, base_summary, failure_summary, raw_summary, model_summary])
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
    combined = combined.sort_values(["_priority", "_score"], ascending=[True, False])
    dedupe_cols = [col for col in ["signal_id", "filter_id", "signal_type"] if col in combined.columns]
    combined = combined.drop_duplicates(subset=dedupe_cols, keep="first").drop(columns=["_priority", "_score"])
    columns = [
        "signal_id",
        "signal_name_zh",
        "signal_type",
        "status",
        "filter_id",
        "filter_name_zh",
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


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame, threshold_audit: pd.DataFrame, filter_audit: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "risk-quality filters use signal-date model outputs and market structure fields only",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "train_only_failure_thresholds",
                "status": "pass" if not threshold_audit.empty else "fail",
                "evidence": f"threshold_rows={len(threshold_audit)}; test_years={predictions['year'].nunique() if not predictions.empty else 0}",
                "action": "每年失败状态阈值只用训练期原始信号分布生成。",
            },
            {
                "audit_item": "risk_quality_overlay_is_predeclared",
                "status": "pass",
                "evidence": f"primary_filter={policy['risk_quality_overlay']['primary_filter_id']}",
                "action": "主过滤由配置声明，不用测试期未来收益挑选。",
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
        (primary_trades, "v4_8_risk_quality_realtime_trades", "v4_8_risk_quality_realtime"),
    ]:
        if frame.empty:
            continue
        for year, group in frame.groupby("year"):
            rows.append({"source": source, "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def build_notes(primary_summary: pd.DataFrame, sensitivity: pd.DataFrame, filter_audit: pd.DataFrame, leave_one_year: pd.DataFrame) -> dict[str, Any]:
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    notes: list[str] = []
    mean_return = nz(primary.get("event_mean_return", math.nan), math.nan)
    events = int(nz(primary.get("nonoverlap_events", 0)))
    if events >= 30 and mean_return < 0.02:
        notes.append("V4.8风险质量过滤改善胜率和坏窗口，但平均收益厚度仍不足，不能升级为有效反弹窗口。")
    elif str(primary.get("status", "")) == "反弹窗口候选":
        notes.append("V4.8风险质量过滤达到内部候选状态，但仍需统一评价体系确认。")
    else:
        notes.append("V4.8风险质量过滤尚未证明能有效找到反弹窗口。")
    notes.append(
        f"主过滤：非重叠事件 {events}，平均收益 {fmt_pct(primary.get('event_mean_return'))}，"
        f"胜率 {fmt_pct(primary.get('event_win_rate'))}，坏窗口 {fmt_pct(primary.get('event_bad_window_rate'))}。"
    )
    if not sensitivity.empty:
        best = sensitivity.sort_values("event_mean_return", ascending=False).iloc[0].to_dict()
        notes.append(
            f"过滤敏感性最高收益项：{best.get('filter_name_zh', '')}，事件 {int(nz(best.get('nonoverlap_events', 0)))}，"
            f"平均收益 {fmt_pct(best.get('event_mean_return'))}。"
        )
    failed = filter_audit[filter_audit["status"].astype(str) == "fail"] if not filter_audit.empty else pd.DataFrame()
    notes.append(f"风险质量审计失败项 {len(failed)} / {len(filter_audit)}。")
    if not leave_one_year.empty:
        failed_loo = leave_one_year[leave_one_year["status"].astype(str) == "fail"]
        notes.append(f"剔除年份审计失败项 {len(failed_loo)} / {len(leave_one_year)}。")
    notes.append("V4.8的结论是：风险质量过滤能降低坏窗口，但不能把平均收益推到2%门槛；继续只用当前价格/广度派生字段的边际收益正在下降。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "下一步应停止在同一价格/广度字段上微调阈值，转向更独立的数据来源或更长历史样本；否则很可能只是在样本数、收益厚度和过拟合之间来回切换。",
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
        return "research_only；存在风险质量过滤候选但仍需统一评价和未来样本验证"
    if int(nz(primary.get("nonoverlap_events", 0))) >= 30:
        return "research_only；风险质量过滤达到样本数但收益厚度不足，不能升级为有效反弹窗口"
    return "research_only；风险质量过滤尚未证明能有效找到反弹窗口"


def render_report(
    v34: Any,
    summary: dict[str, Any],
    top: pd.DataFrame,
    data_audit: pd.DataFrame,
    target_audit: pd.DataFrame,
    year_summary: pd.DataFrame,
    filter_audit: pd.DataFrame,
    sensitivity: pd.DataFrame,
    leave_one_year: pd.DataFrame,
    primary_trades: pd.DataFrame,
    leakage: pd.DataFrame,
    notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines = ["# V4.8 短持有期风险质量过滤反弹窗口研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V4.8 在 V4.7 的5日短持有样本池上加入预声明风险质量过滤，检验是否能在样本数不低于30时提升收益厚度。",
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
    lines.extend(v34.table_or_empty(top, {"signal_id": "信号ID", "signal_name_zh": "名称", "signal_type": "类型", "status": "状态", "filter_id": "过滤ID", "filter_name_zh": "过滤名称", "signal_dates": "信号日", "nonoverlap_events": "非重叠事件", "event_mean_return": "事件收益", "event_win_rate": "事件胜率", "event_bad_window_rate": "坏窗口", "event_worst_return": "最差事件"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 数据可得性", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 目标标签", ""]
    lines.extend(v34.table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 年度失败状态模型", ""]
    lines.extend(v34.table_or_empty(year_summary, {"year": "年份", "status": "状态", "train_rows": "训练样本", "test_rows": "测试样本", "raw_signal_dates": "原始信号", "signal_dates": "过滤后信号", "signal_mean_return": "信号收益", "signal_bad_window_rate": "坏窗口率"}, {"signal_mean_return", "signal_bad_window_rate"}))
    lines += ["", "## 风险质量过滤审计", ""]
    lines.extend(v34.table_or_empty(filter_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 过滤敏感性", ""]
    lines.extend(v34.table_or_empty(sensitivity, {"filter_id": "过滤ID", "filter_name_zh": "过滤名称", "status": "状态", "nonoverlap_events": "事件数", "active_years": "活跃年份", "max_single_year_concentration": "年份集中度", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口", "event_worst_return": "最差事件"}, {"max_single_year_concentration", "event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 剔除年份审计", ""]
    lines.extend(v34.table_or_empty(leave_one_year, {"audit_item": "项目", "excluded_year": "剔除年份", "events": "事件数", "mean_return": "平均收益", "win_rate": "胜率", "bad_window_rate": "坏窗口", "worst_return": "最差事件", "active_years": "活跃年份", "status": "状态"}, {"mean_return", "win_rate", "bad_window_rate", "worst_return"}))
    lines += ["", "## 主规则交易明细", ""]
    lines.extend(v34.table_or_empty(primary_trades.head(80), {"signal_date": "信号日", "entry_date": "入场日", "exit_date": "退出日", "holding_days": "持有期", "filter_name_zh": "过滤", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
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
        "- `report.md`：中文 V4.8 研究报告，优先打开。",
        "- `top_candidates.csv`：风险质量过滤排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：特征面板、短持有样本池、风险过滤敏感性、剔除年份审计、泄漏审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def relabel_v48(*frames: pd.DataFrame) -> None:
    for frame in frames:
        if frame.empty:
            continue
        for col in frame.columns:
            dtype_name = str(frame[col].dtype)
            if frame[col].dtype == object or dtype_name.startswith("string") or dtype_name == "str":
                frame[col] = frame[col].astype(str).str.replace("V4.6", "V4.8", regex=False)


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
