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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v3_7_industry_breadth_policy.json"
V34_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v3_4_realtime_model.py"
VERSION = "3.7.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V3.7 industry breadth rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V3.7 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v34 = load_v34_module()
    v20 = v34.load_v20_module()
    source_policy = read_json(ROOT / policy["source_policy_path"])
    close_matrix = v20.load_close_matrix(ROOT / policy["industry_history_dir"])
    amount_matrix = v34.load_amount_matrix(ROOT / policy["industry_history_dir"])

    features = v20.build_daily_features(close_matrix, {**source_policy, **policy})
    features = v34.add_industry_liquidity_features(features, amount_matrix)
    features = v34.add_market_volatility_ratio(features)
    features = add_industry_breadth_features(features, close_matrix)
    panel = v34.add_rebound_targets(features, policy)

    data_audit = build_data_availability_audit(policy, close_matrix, amount_matrix, panel)
    target_audit = v34.build_target_label_audit(panel, policy)
    breadth_audit = build_breadth_feature_audit(policy, panel)
    rule_summary, rule_events = v34.run_rule_audit(panel, policy)
    predictions, model_year_summary, model_summary = v34.run_walk_forward_model(panel, policy)
    realtime_trades, realtime_summary = v34.run_realtime_simulation(panel, predictions, policy)
    normalize_realtime_labels(realtime_trades, realtime_summary)
    annual_distribution = v34.build_annual_distribution(rule_events, predictions, realtime_trades)
    normalize_annual_distribution(annual_distribution)
    top_candidates = v34.build_top_candidates(rule_summary, model_summary, realtime_summary, policy)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions)
    notes = build_notes(top_candidates, realtime_summary, rule_summary, model_summary)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v37_breadth_feature_target_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    breadth_audit.to_csv(debug_dir / "breadth_feature_audit.csv", index=False, encoding="utf-8-sig")
    rule_summary.to_csv(debug_dir / "industry_breadth_rule_summary.csv", index=False, encoding="utf-8-sig")
    rule_events.to_csv(debug_dir / "industry_breadth_rule_events.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    model_year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    realtime_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    realtime_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, breadth_audit, model_year_summary, realtime_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V3.7行业广度反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"行业数={run_summary['industry_count']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最佳信号={run_summary['best_signal_id']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v34_module() -> Any:
    spec = importlib.util.spec_from_file_location("v34_realtime_model", V34_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V3.4 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_industry_breadth_features(features: pd.DataFrame, close_matrix: pd.DataFrame) -> pd.DataFrame:
    output = features.copy()
    output["trade_date"] = pd.to_datetime(output["trade_date"], errors="coerce")
    close = close_matrix.sort_index().copy()
    ret5 = close / close.shift(5) - 1.0
    ret10 = close / close.shift(10) - 1.0
    ret20 = close / close.shift(20) - 1.0
    ma20 = close.rolling(20, min_periods=15).mean()
    ma60 = close.rolling(60, min_periods=40).mean()
    high252 = close.rolling(252, min_periods=120).max()
    low60 = close.rolling(60, min_periods=40).min()
    low120 = close.rolling(120, min_periods=80).min()
    drawdown252 = close / high252 - 1.0

    breadth = pd.DataFrame(index=close.index)
    breadth["trade_date"] = breadth.index
    breadth["industry_positive_5d_ratio"] = ratio(ret5 > 0, ret5.notna())
    breadth["industry_positive_10d_ratio"] = ratio(ret10 > 0, ret10.notna())
    breadth["industry_positive_20d_ratio"] = ratio(ret20 > 0, ret20.notna())
    breadth["industry_positive_turn_5d"] = breadth["industry_positive_5d_ratio"] - breadth["industry_positive_5d_ratio"].shift(5)
    breadth["industry_above_ma20_ratio"] = ratio(close > ma20, ma20.notna())
    breadth["industry_above_ma60_ratio"] = ratio(close > ma60, ma60.notna())
    breadth["industry_reclaim_ma20_5d"] = breadth["industry_above_ma20_ratio"] - breadth["industry_above_ma20_ratio"].shift(5)
    breadth["industry_reclaim_ma20_10d"] = breadth["industry_above_ma20_ratio"] - breadth["industry_above_ma20_ratio"].shift(10)
    breadth["industry_new_low_60d_ratio"] = ratio(close <= low60 * 1.01, low60.notna())
    breadth["industry_new_low_120d_ratio"] = ratio(close <= low120 * 1.01, low120.notna())
    breadth["industry_new_low_60d_relief_5d"] = breadth["industry_new_low_60d_ratio"].shift(5) - breadth["industry_new_low_60d_ratio"]
    breadth["industry_new_low_120d_relief_5d"] = breadth["industry_new_low_120d_ratio"].shift(5) - breadth["industry_new_low_120d_ratio"]
    breadth["industry_deep_drawdown_252d_ratio"] = ratio(drawdown252 <= -0.30, drawdown252.notna())
    breadth["industry_drawdown_median_252d"] = drawdown252.median(axis=1, skipna=True)
    breadth["industry_drawdown_q25_252d"] = drawdown252.quantile(0.25, axis=1, interpolation="linear")
    breadth["industry_return_5d_dispersion"] = ret5.std(axis=1, skipna=True)
    breadth["industry_return_20d_dispersion"] = ret20.std(axis=1, skipna=True)
    breadth["industry_downside_concentration_20d"] = ratio(ret20 <= -0.10, ret20.notna())

    merged = output.merge(breadth, on="trade_date", how="left")
    merged["panic_exhaustion_score"] = pd.concat(
        [
            clip01(merged["negative_breadth_60d"]),
            clip01(merged["industry_new_low_120d_ratio"] / 0.35),
            clip01(merged["industry_deep_drawdown_252d_ratio"] / 0.50),
            clip01(-merged["market_return_60d"] / 0.25),
            clip01(merged["industry_downside_concentration_20d"] / 0.45),
        ],
        axis=1,
    ).mean(axis=1, skipna=True)
    merged["breadth_recovery_score"] = pd.concat(
        [
            clip01(merged["industry_positive_5d_ratio"]),
            clip01(merged["industry_positive_turn_5d"] / 0.30),
            clip01(merged["industry_reclaim_ma20_5d"] / 0.15),
            clip01(merged["industry_new_low_60d_relief_5d"] / 0.10),
            clip01(merged["market_return_5d"] / 0.05),
        ],
        axis=1,
    ).mean(axis=1, skipna=True)
    return merged


def ratio(condition: pd.DataFrame, valid: pd.DataFrame) -> pd.Series:
    numerator = condition.where(valid, False).sum(axis=1)
    denominator = valid.sum(axis=1).replace(0, np.nan)
    return numerator / denominator


def build_data_availability_audit(policy: dict[str, Any], close_matrix: pd.DataFrame, amount_matrix: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    feature_cols = list(policy["model"]["features"])
    min_feature_coverage = panel[feature_cols].notna().mean().min() if not panel.empty else 0.0
    return pd.DataFrame(
        [
            {
                "audit_item": "industry_price_history",
                "status": "pass" if close_matrix.shape[1] >= 100 and len(close_matrix) >= 1200 else "fail",
                "evidence": f"industries={close_matrix.shape[1]}; rows={len(close_matrix)}",
                "action": "申万二级行业价格用于重建市场层行业广度。",
            },
            {
                "audit_item": "industry_amount_history",
                "status": "pass" if amount_matrix.shape[1] >= 100 and len(amount_matrix) >= 1200 else "fail",
                "evidence": f"industries={amount_matrix.shape[1]}; rows={len(amount_matrix)}",
                "action": "行业成交额继续作为流动性代理。",
            },
            {
                "audit_item": "industry_breadth_feature_coverage",
                "status": "pass" if min_feature_coverage >= 0.65 else "fail",
                "evidence": f"min_feature_coverage={min_feature_coverage:.2%}; features={len(feature_cols)}",
                "action": "广度特征覆盖不足时不得升级为有效模型。",
            },
            {
                "audit_item": "no_short_microstructure_dependency",
                "status": "pass",
                "evidence": "V3.7不使用V3.6中短历史资金流、涨跌停池或市场活跃度接口。",
                "action": "避免把单日或短样本微观数据伪装成长历史信号。",
            },
        ]
    )


def build_breadth_feature_audit(policy: dict[str, Any], panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for field in policy["model"]["features"]:
        series = pd.to_numeric(panel.get(field, pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "feature": field,
                "coverage": float(series.notna().mean()) if len(series) else 0.0,
                "mean": float_or_none(series.mean()) if len(series) else None,
                "p10": float_or_none(series.quantile(0.10)) if len(series) else None,
                "p90": float_or_none(series.quantile(0.90)) if len(series) else None,
            }
        )
    return pd.DataFrame(rows)


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "industry breadth features use trade-date close/amount only; simulation enters next trading day close",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "target_used_only_as_outcome",
                "status": "pass",
                "evidence": "target_rebound_window and forward_return fields are generated after features and only used for labels/evaluation",
                "action": "目标标签不作为规则触发特征。",
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


def normalize_realtime_labels(realtime_trades: pd.DataFrame, realtime_summary: pd.DataFrame) -> None:
    if not realtime_trades.empty and "signal_id" in realtime_trades.columns:
        realtime_trades["signal_id"] = "v3_7_realtime_simulation"
    if not realtime_summary.empty:
        if "signal_id" in realtime_summary.columns:
            realtime_summary["signal_id"] = "v3_7_realtime_simulation"
        if "signal_name_zh" in realtime_summary.columns:
            realtime_summary["signal_name_zh"] = "V3.7实时仿真"


def normalize_annual_distribution(annual_distribution: pd.DataFrame) -> None:
    if not annual_distribution.empty and "signal_id" in annual_distribution.columns:
        annual_distribution.loc[annual_distribution["signal_id"] == "v3_4_realtime_simulation", "signal_id"] = "v3_7_realtime_simulation"


def build_notes(top: pd.DataFrame, realtime_summary: pd.DataFrame, rule_summary: pd.DataFrame, model_summary: pd.DataFrame) -> dict[str, Any]:
    if top.empty:
        return {
            "main_diagnosis": "V3.7没有可排序结果。",
            "next_iterations": ["检查行业历史价格缓存。"],
            "recommended_next_direction": "先恢复数据，再继续反弹窗口研究。",
        }
    best = top.iloc[0].to_dict()
    candidates = top[top["status"] == "反弹窗口候选"]
    notes: list[str] = []
    if candidates.empty:
        notes.append("V3.7没有发现可升级的反弹窗口候选。")
    else:
        notes.append("V3.7发现研究候选，但仍必须保持research_only并等待未来样本。")
    notes.append(
        f"最佳项 {best.get('signal_id', '')}：状态 {best.get('status', '')}，"
        f"非重叠事件 {int(nz(best.get('nonoverlap_events', 0)))}，事件收益 {fmt_pct(best.get('event_mean_return'))}，"
        f"坏窗口 {fmt_pct(best.get('event_bad_window_rate'))}。"
    )
    if not realtime_summary.empty:
        rt = realtime_summary.iloc[0].to_dict()
        notes.append(
            f"实时仿真：交易 {int(nz(rt.get('nonoverlap_events', rt.get('trades', 0))))} 次，"
            f"平均收益 {fmt_pct(rt.get('event_mean_return'))}，坏窗口 {fmt_pct(rt.get('event_bad_window_rate'))}。"
        )
    if not rule_summary.empty:
        observation = rule_summary.sort_values("event_mean_return", ascending=False).head(1).iloc[0].to_dict()
        notes.append(
            f"预声明广度规则中最好的是 {observation.get('signal_id', '')}，"
            f"事件收益 {fmt_pct(observation.get('event_mean_return'))}，状态 {observation.get('status', '')}。"
        )
    notes.append("若 V3.7 仍失败，下一步应把目标从单一20日收益改成多目标标签，分离急跌后技术反抽、震荡修复和趋势反转。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "继续使用可长测的行业广度，但改进目标标签和状态分型，而不是继续扩大规则参数。",
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, close_matrix: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = top[top["status"] == "反弹窗口候选"] if not top.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    audit_fail_count = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_target_panel_count": int(len(panel)),
        "industry_count": int(close_matrix.shape[1]),
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
        return "research_only；行业广度尚未证明能有效找到反弹窗口"
    return "research_only；存在行业广度候选但仍需未来样本验证"


def render_report(v34: Any, summary: dict[str, Any], top: pd.DataFrame, data_audit: pd.DataFrame, target_audit: pd.DataFrame, breadth_audit: pd.DataFrame, model_year: pd.DataFrame, realtime_trades: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V3.7 行业广度反弹窗口研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V3.7 不再依赖短历史微观情绪接口，而是用长期可得的申万二级行业指数重建市场层行业广度。",
        "",
        f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
        f"- 行业数：{summary['industry_count']}",
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
        "active_years": "活跃年份",
        "max_single_year_concentration": "单年集中度",
        "target_capture_rate": "目标命中率",
        "mean_edge_vs_pressure": "相对压力日",
        "event_mean_return": "事件收益",
        "event_win_rate": "事件胜率",
        "event_bad_window_rate": "坏窗口",
        "event_worst_return": "最差事件",
    }, {"max_single_year_concentration", "target_capture_rate", "mean_edge_vs_pressure", "event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 数据可得性", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 目标标签", ""]
    lines.extend(v34.table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 行业广度特征审计", ""]
    lines.extend(v34.table_or_empty(breadth_audit.head(40), {"feature": "特征", "coverage": "覆盖率", "mean": "均值", "p10": "P10", "p90": "P90"}, {"coverage"}))
    lines += ["", "## Walk-forward 年度模型", ""]
    lines.extend(v34.table_or_empty(model_year, {"year": "年份", "status": "状态", "train_rows": "训练样本", "train_target_rate": "训练目标率", "test_rows": "测试样本", "threshold": "阈值", "signal_dates": "信号日", "signal_target_rate": "信号目标率", "signal_mean_return": "信号收益"}, {"train_target_rate", "threshold", "signal_target_rate", "signal_mean_return"}))
    lines += ["", "## 实时仿真交易", ""]
    lines.extend(v34.table_or_empty(realtime_trades.head(30), {"signal_date": "信号日", "entry_date": "入场日", "exit_date": "退出日", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
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
        "- `report.md`：中文 V3.7 研究报告，优先打开。",
        "- `top_candidates.csv`：规则、模型和实时仿真排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：行业广度特征面板、数据审计、目标标签、规则事件、walk-forward、实时仿真、年度分布、泄漏审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def clip01(series: Any) -> pd.Series:
    return pd.Series(series).clip(lower=0.0, upper=1.0)


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
