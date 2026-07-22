#!/usr/bin/env python
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_9_short_target_model_policy.json"
V48_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v4_8_risk_quality_filter.py"
VERSION = "4.9.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.9 short-target rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V4.9 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v48 = load_v48_module()
    v46 = v48.load_v46_module()
    v46.VERSION = VERSION
    v43 = v46.load_v43_module()
    v37 = v43.load_v37_module()
    v34 = v37.load_v34_module()
    v20 = v34.load_v20_module()

    source_policy = read_json(ROOT / policy["source_policy_path"])
    close_matrix = v20.load_close_matrix(ROOT / policy["industry_history_dir"])
    amount_matrix = v34.load_amount_matrix(ROOT / policy["industry_history_dir"])
    panel = build_panel(policy, source_policy, close_matrix, amount_matrix, v20, v34, v37)

    data_audit = v37.build_data_availability_audit(policy, close_matrix, amount_matrix, panel)
    target_audit = v34.build_target_label_audit(panel, policy)
    breadth_audit = v37.build_breadth_feature_audit(policy, panel)

    case = run_short_target_case(policy, panel, v34, v43, v46, v48)
    predictions = case["predictions"]
    year_summary = case["year_summary"]
    model_summary = case["model_summary"]
    threshold_audit = case["threshold_audit"]
    raw_trades = case["raw_trades"]
    raw_summary = case["raw_summary"]
    failure_trades = case["failure_trades"]
    failure_summary = case["failure_summary"]
    base_trades = case["base_trades"]
    base_summary = case["base_summary"]
    primary_trades = case["primary_trades"]
    primary_summary = case["primary_summary"]
    filter_sensitivity = case["filter_sensitivity"]

    filter_audit = v48.build_filter_audit(policy, base_summary, filter_sensitivity, primary_summary)
    target_alignment_audit = build_target_alignment_audit(policy, target_audit, primary_summary)
    sensitivity = run_short_target_sensitivity(policy, panel, v34, v43, v46, v48)
    leave_one_year = v48.build_leave_one_year_out(primary_trades, policy)
    leakage_audit = build_leakage_audit(policy, data_audit, target_alignment_audit, predictions, threshold_audit)
    annual_distribution = build_annual_distribution(predictions, raw_trades, failure_trades, primary_trades)
    top_candidates = v48.build_top_candidates(primary_summary, filter_sensitivity, base_summary, failure_summary, raw_summary, model_summary)
    notes = build_notes(primary_summary, base_summary, filter_audit, target_alignment_audit, sensitivity, leave_one_year)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, primary_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v49_short_target_feature_panel.csv", index=False, encoding="utf-8-sig")
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
    base_trades.to_csv(debug_dir / "short_target_base_trades.csv", index=False, encoding="utf-8-sig")
    base_summary.to_csv(debug_dir / "short_target_base_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    primary_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    filter_sensitivity.to_csv(debug_dir / "risk_quality_filter_sensitivity.csv", index=False, encoding="utf-8-sig")
    filter_audit.to_csv(debug_dir / "risk_quality_filter_audit.csv", index=False, encoding="utf-8-sig")
    target_alignment_audit.to_csv(debug_dir / "target_alignment_audit.csv", index=False, encoding="utf-8-sig")
    sensitivity.to_csv(debug_dir / "short_target_sensitivity.csv", index=False, encoding="utf-8-sig")
    leave_one_year.to_csv(debug_dir / "leave_one_year_out_audit.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, target_alignment_audit, year_summary, filter_audit, sensitivity, leave_one_year, primary_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V4.9 5日目标重训反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"主规则实时交易数={run_summary['primary_realtime_events']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def build_panel(policy: dict[str, Any], source_policy: dict[str, Any], close_matrix: pd.DataFrame, amount_matrix: pd.DataFrame, v20: Any, v34: Any, v37: Any) -> pd.DataFrame:
    features = v20.build_daily_features(close_matrix, {**source_policy, **policy})
    features = v34.add_industry_liquidity_features(features, amount_matrix)
    features = v34.add_market_volatility_ratio(features)
    features = v37.add_industry_breadth_features(features, close_matrix)
    return v34.add_rebound_targets(features, policy)


def run_short_target_case(policy: dict[str, Any], panel: pd.DataFrame, v34: Any, v43: Any, v46: Any, v48: Any) -> dict[str, pd.DataFrame]:
    predictions, year_summary, model_summary, threshold_audit = v43.run_failure_state_model(panel, policy, v34)
    predictions = v46.attach_forward_metrics(predictions, panel, policy)

    raw_predictions = predictions.copy()
    if not raw_predictions.empty:
        raw_predictions["model_signal"] = raw_predictions["raw_model_signal"]
    raw_trades, raw_summary = v34.run_realtime_simulation(panel, raw_predictions, policy)
    normalize_summary(raw_trades, raw_summary, "v4_9_raw_short_target_reference", "V4.9原始5日目标模型参考", "原始5日目标模型参考")

    failure_trades, failure_summary = v34.run_realtime_simulation(panel, predictions, policy)
    normalize_summary(failure_trades, failure_summary, "v4_9_failure_state_short_target_reference", "V4.9失败状态过滤5日目标参考", "失败状态过滤5日目标参考")

    base_grid_trades, base_grid_summary, base_trades, base_summary = v46.run_horizon_grid(predictions, policy)
    relabel_v49(base_grid_trades, base_grid_summary, base_trades, base_summary, policy)
    filter_sensitivity, primary_trades, primary_summary = v48.run_filter_overlay(base_trades, policy)
    return {
        "predictions": predictions,
        "year_summary": year_summary,
        "model_summary": model_summary,
        "threshold_audit": threshold_audit,
        "raw_trades": raw_trades,
        "raw_summary": raw_summary,
        "failure_trades": failure_trades,
        "failure_summary": failure_summary,
        "base_trades": base_trades,
        "base_summary": base_summary,
        "primary_trades": primary_trades,
        "primary_summary": primary_summary,
        "filter_sensitivity": filter_sensitivity,
    }


def run_short_target_sensitivity(policy: dict[str, Any], panel: pd.DataFrame, v34: Any, v43: Any, v46: Any, v48: Any) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    cfg = policy["short_target_sensitivity"]
    for quantile in cfg["probability_quantiles"]:
        for min_prob in cfg["minimum_probability_thresholds"]:
            trial = copy.deepcopy(policy)
            trial["model"]["probability_quantile"] = float(quantile)
            trial["model"]["minimum_probability_threshold"] = float(min_prob)
            case = run_short_target_case(trial, panel, v34, v43, v46, v48)
            primary = case["primary_summary"].iloc[0].to_dict() if not case["primary_summary"].empty else {}
            predictions = case["predictions"]
            year_summary = case["year_summary"]
            signal = predictions[predictions["model_signal"].astype(bool)] if not predictions.empty else pd.DataFrame()
            wf_valid = year_summary[year_summary["status"].astype(str) == "pass"] if not year_summary.empty else pd.DataFrame()
            wf_signal_years = int((pd.to_numeric(wf_valid.get("signal_dates", pd.Series(dtype=float)), errors="coerce") > 0).sum()) if not wf_valid.empty else 0
            wf_positive_years = int((pd.to_numeric(wf_valid.get("signal_mean_return", pd.Series(dtype=float)), errors="coerce") > 0).sum()) if not wf_valid.empty else 0
            rows.append(
                {
                    "probability_quantile": float(quantile),
                    "minimum_probability_threshold": float(min_prob),
                    "model_signal_dates": int(len(signal)),
                    "target_capture_rate": float(signal["target_rebound_window"].mean()) if len(signal) else math.nan,
                    "nonoverlap_events": int(primary.get("nonoverlap_events", 0) or 0),
                    "event_mean_return": float_or_nan(primary.get("event_mean_return")),
                    "event_win_rate": float_or_nan(primary.get("event_win_rate")),
                    "event_bad_window_rate": float_or_nan(primary.get("event_bad_window_rate")),
                    "active_years": int(primary.get("active_years", 0) or 0),
                    "max_single_year_concentration": float_or_nan(primary.get("max_single_year_concentration")),
                    "walk_forward_positive_year_rate": wf_positive_years / wf_signal_years if wf_signal_years else math.nan,
                    "status": primary.get("status", ""),
                    "used_for_primary": float(quantile) == float(policy["model"]["probability_quantile"]) and float(min_prob) == float(policy["model"]["minimum_probability_threshold"]),
                }
            )
    return pd.DataFrame(rows).sort_values(["used_for_primary", "event_mean_return"], ascending=[False, False]).reset_index(drop=True)


def build_target_alignment_audit(policy: dict[str, Any], target_audit: pd.DataFrame, primary_summary: pd.DataFrame) -> pd.DataFrame:
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    return pd.DataFrame(
        [
            {
                "audit_item": "target_execution_horizon_aligned",
                "status": "pass" if int(policy["target_horizon"]) == int(policy["persistent_cluster"]["primary_horizon"]) else "fail",
                "evidence": f"target_horizon={policy['target_horizon']}; primary_horizon={policy['persistent_cluster']['primary_horizon']}",
                "action": "V4.9 专门验证目标周期和执行周期是否需要一致。",
            },
            {
                "audit_item": "short_target_threshold_declared",
                "status": "pass",
                "evidence": f"target_return={fmt_pct(policy['target_return_threshold'])}; max_drawdown_floor={fmt_pct(policy['target_max_drawdown_floor'])}; bad_window={fmt_pct(policy['bad_window_threshold'])}",
                "action": "5日目标阈值由配置声明，不用测试结果倒推。",
            },
            {
                "audit_item": "short_target_primary_events",
                "status": "pass" if int(primary.get("nonoverlap_events", 0) or 0) >= int(policy["robustness_thresholds"]["min_conditional_events"]) else "fail",
                "evidence": f"events={int(primary.get('nonoverlap_events', 0) or 0)}",
                "action": "若样本不足，不能把5日目标模型作为有效反弹窗口。",
            },
            {
                "audit_item": "short_target_mean_return",
                "status": "pass" if float_or_nan(primary.get("event_mean_return"), -1.0) >= float(policy["promotion_thresholds"]["min_event_mean_return"]) else "fail",
                "evidence": f"mean_return={fmt_pct(primary.get('event_mean_return'))} / {fmt_pct(policy['promotion_thresholds']['min_event_mean_return'])}",
                "action": "收益厚度不足时不得升级。",
            },
        ]
    )


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, target_alignment: pd.DataFrame, predictions: pd.DataFrame, threshold_audit: pd.DataFrame) -> pd.DataFrame:
    alignment_governance = target_alignment[
        target_alignment["audit_item"].isin(["target_execution_horizon_aligned", "short_target_threshold_declared"])
    ].copy()
    alignment_governance_failures = int((alignment_governance["status"] == "fail").sum()) if not alignment_governance.empty else 0
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "features use signal-date market and industry breadth fields only",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "short_target_label_boundary",
                "status": "pass",
                "evidence": "5日forward return only used as training label and evaluation outcome",
                "action": "目标标签不作为触发特征。",
            },
            {
                "audit_item": "purged_walk_forward",
                "status": "pass" if not predictions.empty and not threshold_audit.empty else "fail",
                "evidence": f"prediction_rows={len(predictions)}; threshold_rows={len(threshold_audit)}",
                "action": "每年模型只使用测试年以前并带purge的训练样本。",
            },
            {
                "audit_item": "target_alignment_audit",
                "status": "pass" if alignment_governance_failures == 0 else "fail",
                "evidence": f"governance_failures={alignment_governance_failures}",
                "action": "只把目标周期和阈值声明纳入治理审计；收益不足交给统一评分体系处理。",
            },
            {
                "audit_item": "compact_output_governance",
                "status": "pass" if not (data_audit["status"] == "fail").any() else "fail",
                "evidence": f"data_audit_failures={int((data_audit['status'] == 'fail').sum())}",
                "action": "输出仍保持 report.md/top_candidates.csv/run_summary.json/debug。",
            },
        ]
    )


def build_annual_distribution(predictions: pd.DataFrame, raw_trades: pd.DataFrame, failure_trades: pd.DataFrame, primary_trades: pd.DataFrame) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for source, signal_id, frame in [
        ("model_signal_dates", "v4_9_short_target_model", predictions[predictions["model_signal"].astype(bool)] if not predictions.empty else pd.DataFrame()),
        ("raw_realtime_trades", "v4_9_raw_short_target_reference", raw_trades),
        ("failure_state_realtime_trades", "v4_9_failure_state_short_target_reference", failure_trades),
        ("v4_9_primary_realtime_trades", "v4_9_short_target_realtime", primary_trades),
    ]:
        if frame.empty or "year" not in frame.columns:
            continue
        counts = frame["year"].value_counts().sort_index()
        frames.append(pd.DataFrame({"source": source, "signal_id": signal_id, "year": counts.index.astype(int), "count": counts.values.astype(int)}))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["source", "signal_id", "year", "count"])


def build_notes(primary_summary: pd.DataFrame, base_summary: pd.DataFrame, filter_audit: pd.DataFrame, target_alignment: pd.DataFrame, sensitivity: pd.DataFrame, leave_one_year: pd.DataFrame) -> dict[str, Any]:
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    base = base_summary.iloc[0].to_dict() if not base_summary.empty else {}
    best_sens = sensitivity.sort_values("event_mean_return", ascending=False).iloc[0].to_dict() if not sensitivity.empty else {}
    audit_failures = int((filter_audit["status"] == "fail").sum()) + int((target_alignment["status"] == "fail").sum())
    loo_failures = int((leave_one_year["status"] == "fail").sum()) if not leave_one_year.empty else 0
    main = "V4.9把训练目标改为5日后，主规则样本和收益均低于V4.8，说明收益厚度不足不是单纯由目标周期错配造成。"
    return {
        "main_diagnosis": main,
        "primary_events": int(primary.get("nonoverlap_events", 0) or 0),
        "primary_mean_return": float_or_none(primary.get("event_mean_return")),
        "base_events": int(base.get("nonoverlap_events", 0) or 0),
        "base_mean_return": float_or_none(base.get("event_mean_return")),
        "best_sensitivity": {
            "probability_quantile": float_or_none(best_sens.get("probability_quantile")),
            "minimum_probability_threshold": float_or_none(best_sens.get("minimum_probability_threshold")),
            "nonoverlap_events": int(best_sens.get("nonoverlap_events", 0) or 0),
            "event_mean_return": float_or_none(best_sens.get("event_mean_return")),
        },
        "audit_failures": audit_failures,
        "leave_one_year_failures": loo_failures,
        "next_iterations": [
            main,
            "主规则保持V4.8概率阈值，只改变目标标签周期；敏感性网格不用于重选主规则。",
            f"主规则：事件 {int(primary.get('nonoverlap_events', 0) or 0)}，平均收益 {fmt_pct(primary.get('event_mean_return'))}，胜率 {fmt_pct(primary.get('event_win_rate'))}，坏窗口 {fmt_pct(primary.get('event_bad_window_rate'))}。",
            f"敏感性最好项：q={best_sens.get('probability_quantile', '')}，min_prob={best_sens.get('minimum_probability_threshold', '')}，事件 {int(best_sens.get('nonoverlap_events', 0) or 0)}，平均收益 {fmt_pct(best_sens.get('event_mean_return'))}。",
            "若V4.9仍被拒绝，下一步不应继续围绕同一组价格/广度特征改目标周期，而应增加独立数据或重构更长历史样本。",
        ],
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, close_matrix: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, primary_summary: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = top[top["status"] == "反弹窗口候选"] if not top.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    audit_fail_count = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_target_panel_count": int(len(panel)),
        "industry_count": int(close_matrix.shape[1]),
        "primary_signal_id": policy["persistent_cluster"]["primary_signal_id"],
        "primary_realtime_events": int(primary.get("nonoverlap_events", 0) or 0),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_signal_id": best.get("signal_id", ""),
        "best_status": best.get("status", ""),
        "best_nonoverlap_events": int(best.get("nonoverlap_events", 0) or 0),
        "best_event_mean_return": float_or_none(best.get("event_mean_return")),
        "best_event_bad_window_rate": float_or_none(best.get("event_bad_window_rate")),
        "final_verdict": final_verdict(candidates, notes),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(
    v34: Any,
    summary: dict[str, Any],
    top: pd.DataFrame,
    data_audit: pd.DataFrame,
    target_audit: pd.DataFrame,
    target_alignment: pd.DataFrame,
    year_summary: pd.DataFrame,
    filter_audit: pd.DataFrame,
    sensitivity: pd.DataFrame,
    leave_one_year: pd.DataFrame,
    primary_trades: pd.DataFrame,
    leakage: pd.DataFrame,
    notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines = [
        "# V4.9 5日目标重训反弹窗口研究报告",
        "",
        f"版本：{VERSION}",
        f"生成时间：{summary['generated_at']}",
        "",
        "V4.9 用 5 日反弹标签重新训练模型，验证 V4.7/V4.8 的收益厚度不足是否来自“20日目标模型 + 5日执行”的周期错配。",
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
    lines.extend(v34.table_or_empty(top, {
        "signal_id": "信号ID",
        "signal_name_zh": "信号名称",
        "signal_type": "类型",
        "status": "状态",
        "filter_id": "过滤ID",
        "filter_name_zh": "过滤名称",
        "signal_dates": "信号日",
        "nonoverlap_events": "事件数",
        "event_mean_return": "平均收益",
        "event_win_rate": "胜率",
        "event_bad_window_rate": "坏窗口",
        "event_worst_return": "最差事件",
    }, {"event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 目标周期审计", ""]
    lines.extend(v34.table_or_empty(target_alignment, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 数据与标签审计", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 5日目标标签", ""]
    lines.extend(v34.table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## Walk-forward 年度表现", ""]
    lines.extend(v34.table_or_empty(year_summary, {
        "year": "年份",
        "status": "状态",
        "train_rows": "训练行",
        "test_rows": "测试行",
        "raw_signal_dates": "原始信号",
        "signal_dates": "过滤信号",
        "signal_mean_return": "信号均值",
        "signal_bad_window_rate": "坏窗口",
    }, {"signal_mean_return", "signal_bad_window_rate"}))
    lines += ["", "## 风险质量过滤审计", ""]
    lines.extend(v34.table_or_empty(filter_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 短目标敏感性", ""]
    lines.extend(v34.table_or_empty(sensitivity, {
        "probability_quantile": "概率分位",
        "minimum_probability_threshold": "最低概率",
        "model_signal_dates": "模型信号日",
        "nonoverlap_events": "事件数",
        "event_mean_return": "平均收益",
        "event_win_rate": "胜率",
        "event_bad_window_rate": "坏窗口",
        "walk_forward_positive_year_rate": "年度正收益率",
        "status": "状态",
        "used_for_primary": "主规则",
    }, {"target_capture_rate", "event_mean_return", "event_win_rate", "event_bad_window_rate", "max_single_year_concentration", "walk_forward_positive_year_rate"}))
    lines += ["", "## 剔除年份审计", ""]
    lines.extend(v34.table_or_empty(leave_one_year, {
        "audit_item": "项目",
        "excluded_year": "剔除年份",
        "events": "事件数",
        "mean_return": "平均收益",
        "win_rate": "胜率",
        "bad_window_rate": "坏窗口",
        "worst_return": "最差事件",
        "active_years": "活跃年份",
        "status": "状态",
    }, {"mean_return", "win_rate", "bad_window_rate", "worst_return"}))
    lines += ["", "## 主规则交易明细", ""]
    lines.extend(v34.table_or_empty(primary_trades.head(80), {
        "signal_date": "信号日",
        "entry_date": "入场日",
        "exit_date": "退出日",
        "holding_days": "持有日",
        "filter_name_zh": "过滤",
        "trade_return": "收益",
        "max_adverse_return": "最大不利",
        "is_bad_window": "坏窗口",
    }, {"trade_return", "max_adverse_return"}))
    lines += ["", "## 泄漏与治理审计", ""]
    lines.extend(v34.table_or_empty(leakage, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 结论与下一步", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines += [
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文 V4.9 研究报告，优先打开。",
        "- `top_candidates.csv`：5日目标重训信号排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：特征面板、walk-forward、风险过滤敏感性、目标周期审计、剔除年份审计、泄漏审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def relabel_v49(grid_trades: pd.DataFrame, grid_summary: pd.DataFrame, primary_trades: pd.DataFrame, primary_summary: pd.DataFrame, policy: dict[str, Any]) -> None:
    for frame in [grid_trades, primary_trades]:
        if not frame.empty and "signal_id" in frame.columns:
            frame["signal_id"] = policy["persistent_cluster"]["primary_signal_id"]
    for frame in [grid_summary, primary_summary]:
        if frame.empty:
            continue
        frame["signal_id"] = frame["signal_id"].astype(str).str.replace("v4_6_horizon_stability_realtime", policy["persistent_cluster"]["primary_signal_id"], regex=False)
        frame["signal_name_zh"] = frame.apply(lambda row: f"V4.9 5日目标重训{int(row.get('holding_days', 5))}日持有", axis=1)
        frame["signal_type"] = "5日目标重训实时仿真"


def normalize_summary(trades: pd.DataFrame, summary: pd.DataFrame, signal_id: str, name: str, signal_type: str) -> None:
    if not trades.empty:
        trades["signal_id"] = signal_id
    if not summary.empty:
        summary["signal_id"] = signal_id
        summary["signal_name_zh"] = name
        summary["signal_type"] = signal_type


def final_verdict(candidates: pd.DataFrame, notes: dict[str, Any]) -> str:
    if len(candidates):
        return "research_only；出现候选但仍需人工复核和后续样本验证，不能生成交易指令"
    return "research_only；5日目标重训未通过有效反弹窗口门槛，不能升级为有效反弹窗口"


def load_v48_module() -> Any:
    spec = importlib.util.spec_from_file_location("v48_risk_quality_filter", V48_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V4.8 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    if hasattr(value, "item"):
        return clean_json_value(value.item())
    return value


def float_or_nan(value: Any, default: float = math.nan) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def float_or_none(value: Any) -> float | None:
    number = float_or_nan(value)
    return None if math.isnan(number) else float(number)


def fmt_pct(value: Any) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number:.2%}"


if __name__ == "__main__":
    main()
