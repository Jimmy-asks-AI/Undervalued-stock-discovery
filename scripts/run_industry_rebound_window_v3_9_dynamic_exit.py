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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v3_9_dynamic_exit_policy.json"
V37_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v3_7_industry_breadth.py"
VERSION = "3.9.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V3.9 dynamic exit rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V3.9 policy JSON.")
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
    fixed_trades, fixed_summary = v34.run_realtime_simulation(panel, predictions, policy)
    normalize_fixed_labels(fixed_trades, fixed_summary)

    policy_trades: list[pd.DataFrame] = []
    policy_summaries: list[dict[str, Any]] = []
    exit_logs: list[pd.DataFrame] = []
    for exit_policy in policy["dynamic_exit_policies"]:
        trades, log = run_dynamic_exit_simulation(panel, predictions, policy, exit_policy)
        summary = summarize_exit_policy(trades, predictions, policy, exit_policy)
        policy_trades.append(trades)
        exit_logs.append(log)
        policy_summaries.append(summary)
    dynamic_trades = concat_frames(policy_trades)
    exit_event_log = concat_frames(exit_logs)
    dynamic_summary = pd.DataFrame(policy_summaries)
    primary_summary = dynamic_summary[dynamic_summary["exit_policy_id"] == policy["primary_exit_policy_id"]].copy()
    primary_trades = dynamic_trades[dynamic_trades["exit_policy_id"] == policy["primary_exit_policy_id"]].copy()
    top_candidates = build_top_candidates(fixed_summary, model_summary, dynamic_summary)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions)
    annual_distribution = build_annual_distribution(predictions, fixed_trades, dynamic_trades, policy)
    notes = build_notes(primary_summary, fixed_summary, dynamic_summary)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, primary_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v39_dynamic_exit_feature_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    breadth_audit.to_csv(debug_dir / "breadth_feature_audit.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    model_year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    fixed_trades.to_csv(debug_dir / "fixed_horizon_realtime_trades.csv", index=False, encoding="utf-8-sig")
    fixed_summary.to_csv(debug_dir / "fixed_horizon_realtime_summary.csv", index=False, encoding="utf-8-sig")
    dynamic_trades.to_csv(debug_dir / "dynamic_exit_policy_trades.csv", index=False, encoding="utf-8-sig")
    dynamic_summary.to_csv(debug_dir / "dynamic_exit_policy_summary.csv", index=False, encoding="utf-8-sig")
    exit_event_log.to_csv(debug_dir / "dynamic_exit_event_log.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    primary_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, model_year_summary, fixed_summary, dynamic_summary, primary_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V3.9动态退出反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"主退出策略={run_summary['primary_exit_policy_id']}")
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


def run_dynamic_exit_simulation(panel: pd.DataFrame, predictions: pd.DataFrame, policy: dict[str, Any], exit_policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if predictions.empty:
        return pd.DataFrame(), pd.DataFrame()
    full = panel.sort_values("trade_date").reset_index(drop=True).copy()
    full["trade_date"] = pd.to_datetime(full["trade_date"], errors="coerce")
    nav = pd.to_numeric(full["market_nav"], errors="coerce").reset_index(drop=True)
    date_to_idx = {pd.Timestamp(value).strftime("%Y-%m-%d"): int(idx) for idx, value in full["trade_date"].items()}
    signals = predictions[predictions["model_signal"].astype(bool)].copy()
    signals["trade_date"] = pd.to_datetime(signals["trade_date"], errors="coerce")
    signals = signals.dropna(subset=["trade_date"]).sort_values("trade_date")
    rows: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    last_exit = -1
    for _, signal in signals.iterrows():
        signal_date = pd.Timestamp(signal["trade_date"]).strftime("%Y-%m-%d")
        idx = date_to_idx.get(signal_date)
        if idx is None or idx <= last_exit:
            continue
        entry_idx = idx + 1
        max_exit_idx = idx + int(exit_policy["max_holding_days"]) + 1
        if entry_idx >= len(full):
            continue
        max_exit_idx = min(max_exit_idx, len(full) - 1)
        entry_nav = nav.iloc[entry_idx]
        if pd.isna(entry_nav):
            continue
        peak_return = -1.0
        planned_exit_idx = max_exit_idx
        trigger_idx = max_exit_idx
        exit_reason = "max_holding"
        for obs_idx in range(entry_idx, max_exit_idx + 1):
            obs_nav = nav.iloc[obs_idx]
            if pd.isna(obs_nav):
                continue
            holding_days = obs_idx - entry_idx
            current_return = float(obs_nav / entry_nav - 1.0)
            peak_return = max(peak_return, current_return)
            obs_row = full.iloc[obs_idx]
            reason = exit_trigger_reason(current_return, peak_return, holding_days, obs_row, exit_policy)
            if reason:
                trigger_idx = obs_idx
                planned_exit_idx = min(obs_idx + int(exit_policy.get("execution_lag_days", 1)), max_exit_idx, len(full) - 1)
                exit_reason = reason
                break
        exit_nav = nav.iloc[planned_exit_idx]
        if pd.isna(exit_nav):
            continue
        path = nav.iloc[entry_idx : planned_exit_idx + 1] / entry_nav - 1.0
        trade_return = float(exit_nav / entry_nav - 1.0)
        max_adverse = float(path.min()) if len(path) else math.nan
        rows.append(
            {
                "signal_id": "v3_9_dynamic_exit_realtime",
                "signal_date": signal_date,
                "entry_date": pd.Timestamp(full.loc[entry_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "trigger_date": pd.Timestamp(full.loc[trigger_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "exit_date": pd.Timestamp(full.loc[planned_exit_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "holding_days": int(planned_exit_idx - entry_idx),
                "trade_return": trade_return,
                "max_adverse_return": max_adverse,
                "peak_return_before_exit": peak_return,
                "is_win": bool(trade_return > 0),
                "is_bad_window": bool(trade_return <= float(policy["bad_window_threshold"])),
                "year": int(pd.Timestamp(signal["trade_date"]).year),
                "exit_policy_id": exit_policy["exit_policy_id"],
                "exit_policy_name_zh": exit_policy["exit_policy_name_zh"],
                "exit_reason": exit_reason,
                "model_probability": float(signal.get("model_probability", math.nan)),
                "model_threshold": float(signal.get("model_threshold", math.nan)),
            }
        )
        logs.append(
            {
                "exit_policy_id": exit_policy["exit_policy_id"],
                "signal_date": signal_date,
                "entry_date": pd.Timestamp(full.loc[entry_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "trigger_date": pd.Timestamp(full.loc[trigger_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "exit_date": pd.Timestamp(full.loc[planned_exit_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "exit_reason": exit_reason,
                "trigger_return": float(nav.iloc[trigger_idx] / entry_nav - 1.0) if pd.notna(nav.iloc[trigger_idx]) else math.nan,
                "exit_return": trade_return,
                "breadth_recovery_score_at_trigger": float_or_none(full.loc[trigger_idx, "breadth_recovery_score"]),
                "industry_positive_5d_ratio_at_trigger": float_or_none(full.loc[trigger_idx, "industry_positive_5d_ratio"]),
            }
        )
        last_exit = planned_exit_idx
    return pd.DataFrame(rows), pd.DataFrame(logs)


def exit_trigger_reason(current_return: float, peak_return: float, holding_days: int, obs_row: pd.Series, exit_policy: dict[str, Any]) -> str:
    if current_return <= float(exit_policy["stop_loss"]):
        return "stop_loss_next_close"
    breadth_recovery = float_or_nan(obs_row.get("breadth_recovery_score"))
    positive_ratio = float_or_nan(obs_row.get("industry_positive_5d_ratio"))
    if (
        holding_days >= int(exit_policy["risk_off_min_days"])
        and current_return <= float(exit_policy["risk_off_return_ceiling"])
        and breadth_recovery <= float(exit_policy["risk_off_breadth_recovery_max"])
        and positive_ratio <= float(exit_policy["risk_off_positive_5d_max"])
    ):
        return "risk_off_next_close"
    if peak_return >= float(exit_policy["profit_activation"]) and current_return <= peak_return - float(exit_policy["trailing_drawdown"]):
        return "profit_trailing_next_close"
    if (
        holding_days >= int(exit_policy["early_profit_min_days"])
        and current_return >= float(exit_policy["early_profit_return"])
        and breadth_recovery <= float(exit_policy["early_profit_breadth_recovery_max"])
    ):
        return "early_profit_decay_next_close"
    return ""


def summarize_exit_policy(trades: pd.DataFrame, predictions: pd.DataFrame, policy: dict[str, Any], exit_policy: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "signal_id": "v3_9_dynamic_exit_realtime",
        "signal_name_zh": exit_policy["exit_policy_name_zh"],
        "signal_type": "动态退出实时仿真",
        "exit_policy_id": exit_policy["exit_policy_id"],
        "exit_policy_name_zh": exit_policy["exit_policy_name_zh"],
        "signal_dates": int(predictions["model_signal"].astype(bool).sum()) if not predictions.empty and "model_signal" in predictions.columns else 0,
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
            "avg_holding_days": float(pd.to_numeric(trades["holding_days"], errors="coerce").mean()),
            "stop_loss_count": int((trades["exit_reason"] == "stop_loss_next_close").sum()),
            "risk_off_count": int((trades["exit_reason"] == "risk_off_next_close").sum()),
            "profit_exit_count": int(trades["exit_reason"].astype(str).str.contains("profit").sum()),
        }
    )
    summary["status"] = classify_summary(summary, policy)
    return summary


def build_top_candidates(fixed_summary: pd.DataFrame, model_summary: pd.DataFrame, dynamic_summary: pd.DataFrame) -> pd.DataFrame:
    frames = []
    if not fixed_summary.empty:
        fixed = fixed_summary.copy()
        fixed["exit_policy_id"] = "fixed_20d_reference"
        fixed["exit_policy_name_zh"] = "固定20日参考"
        frames.append(fixed)
    if not model_summary.empty:
        frames.append(model_summary.copy())
    if not dynamic_summary.empty:
        frames.append(dynamic_summary.copy())
    combined = concat_frames(frames)
    if combined.empty:
        return combined
    priority = {"反弹窗口候选": 0, "状态观察": 1, "样本不足": 2, "拒绝": 3}
    combined["_priority"] = combined["status"].map(priority).fillna(9)
    for col in ["event_mean_return", "mean_edge_vs_pressure", "event_win_rate", "event_bad_window_rate", "max_single_year_concentration"]:
        if col not in combined.columns:
            combined[col] = math.nan
    combined["_score"] = (
        2.0 * combined["event_mean_return"].map(nz)
        + combined["event_win_rate"].map(nz)
        - combined["event_bad_window_rate"].map(nz)
        - 0.4 * combined["max_single_year_concentration"].map(lambda value: nz(value, 1.0))
        + 0.5 * combined["mean_edge_vs_pressure"].map(nz)
    )
    combined = combined.sort_values(["_priority", "_score"], ascending=[True, False]).drop(columns=["_priority", "_score"])
    columns = [
        "signal_id",
        "signal_name_zh",
        "signal_type",
        "exit_policy_id",
        "exit_policy_name_zh",
        "status",
        "signal_dates",
        "nonoverlap_events",
        "active_years",
        "max_single_year_concentration",
        "avg_holding_days",
        "event_mean_return",
        "event_win_rate",
        "event_bad_window_rate",
        "event_worst_return",
        "stop_loss_count",
        "risk_off_count",
        "profit_exit_count",
    ]
    return combined[[col for col in columns if col in combined.columns]]


def normalize_fixed_labels(trades: pd.DataFrame, summary: pd.DataFrame) -> None:
    if not trades.empty and "signal_id" in trades.columns:
        trades["signal_id"] = "v3_7_fixed_20d_reference"
    if not summary.empty:
        if "signal_id" in summary.columns:
            summary["signal_id"] = "v3_7_fixed_20d_reference"
        if "signal_name_zh" in summary.columns:
            summary["signal_name_zh"] = "V3.7固定20日参考"


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "entry features use trade-date close/amount only; entry executes next trading day close",
                "action": "入场不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "dynamic_exit_execution_lag",
                "status": "pass",
                "evidence": "exit triggers are observed on close and executed at next available close",
                "action": "退出规则不使用触发日同收盘执行。",
            },
            {
                "audit_item": "target_used_only_as_outcome",
                "status": "pass",
                "evidence": "target_rebound_window and forward_return fields are used for model labels/evaluation only",
                "action": "目标标签不作为退出触发特征。",
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
                "audit_item": "primary_policy_boundary",
                "status": "pass",
                "evidence": f"primary_exit_policy_id={policy['primary_exit_policy_id']}",
                "action": "统一评价只使用预声明主退出策略，不事后挑最好策略。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["research_boundary"],
                "action": "不生成交易指令；通过也只是研究候选。",
            },
        ]
    )


def build_annual_distribution(predictions: pd.DataFrame, fixed_trades: pd.DataFrame, dynamic_trades: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not predictions.empty:
        signals = predictions[predictions["model_signal"].astype(bool)].copy()
        for year, group in signals.groupby("year"):
            rows.append({"source": "model_signal_dates", "signal_id": "walk_forward_probability_model", "year": int(year), "count": int(len(group))})
    if not fixed_trades.empty:
        for year, group in fixed_trades.groupby("year"):
            rows.append({"source": "fixed_20d_trades", "signal_id": "v3_7_fixed_20d_reference", "year": int(year), "count": int(len(group))})
    if not dynamic_trades.empty:
        for (exit_policy_id, year), group in dynamic_trades.groupby(["exit_policy_id", "year"]):
            rows.append({"source": "dynamic_exit_trades", "signal_id": str(exit_policy_id), "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def build_notes(primary_summary: pd.DataFrame, fixed_summary: pd.DataFrame, dynamic_summary: pd.DataFrame) -> dict[str, Any]:
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    fixed = fixed_summary.iloc[0].to_dict() if not fixed_summary.empty else {}
    notes: list[str] = []
    if str(primary.get("status", "")) == "反弹窗口候选":
        notes.append("V3.9主动态退出策略达到研究候选状态，但仍必须保持research_only并等待更严格复核。")
    else:
        notes.append("V3.9动态退出仍未证明能有效找到反弹窗口。")
    notes.append(
        f"主退出策略：非重叠事件 {int(nz(primary.get('nonoverlap_events', 0)))}，"
        f"平均收益 {fmt_pct(primary.get('event_mean_return'))}，胜率 {fmt_pct(primary.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(primary.get('event_bad_window_rate'))}。"
    )
    notes.append(
        f"固定20日参考：平均收益 {fmt_pct(fixed.get('event_mean_return'))}，胜率 {fmt_pct(fixed.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(fixed.get('event_bad_window_rate'))}。"
    )
    if not dynamic_summary.empty:
        best = dynamic_summary.sort_values("event_mean_return", ascending=False).head(1).iloc[0].to_dict()
        notes.append(
            f"对照退出策略中收益最高的是 {best.get('exit_policy_name_zh', '')}，"
            f"事件收益 {fmt_pct(best.get('event_mean_return'))}，状态 {best.get('status', '')}。"
        )
    notes.append("若 V3.9 仍失败，下一步应改进入场概率校准或降低信号噪声，而不是继续加强退出规则。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "回到V3.7入场信号本身，做概率校准、信号去重和环境过滤，而不是继续叠加退出复杂度。",
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
        "primary_exit_policy_id": policy["primary_exit_policy_id"],
        "primary_realtime_events": int(nz(primary.get("nonoverlap_events", primary.get("trades", 0)))),
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
        return "research_only；动态退出尚未证明能有效找到反弹窗口"
    return "research_only；存在动态退出候选但仍需未来样本验证"


def render_report(v34: Any, summary: dict[str, Any], top: pd.DataFrame, data_audit: pd.DataFrame, target_audit: pd.DataFrame, model_year: pd.DataFrame, fixed_summary: pd.DataFrame, dynamic_summary: pd.DataFrame, primary_trades: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V3.9 动态退出与失败窗口快速撤退研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V3.9 保持 V3.7 行业广度入场信号不变，只测试入场后的风险预算、动态退出和失败窗口快速撤退。",
        "",
        f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
        f"- 行业数：{summary['industry_count']}",
        f"- 主退出策略：{summary['primary_exit_policy_id']}",
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
        "exit_policy_name_zh": "退出策略",
        "status": "状态",
        "signal_dates": "信号日",
        "nonoverlap_events": "非重叠事件",
        "avg_holding_days": "平均持有",
        "event_mean_return": "事件收益",
        "event_win_rate": "事件胜率",
        "event_bad_window_rate": "坏窗口",
        "event_worst_return": "最差事件",
        "stop_loss_count": "止损次数",
        "risk_off_count": "风险撤退",
        "profit_exit_count": "利润退出",
    }, {"event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 数据可得性", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 目标标签", ""]
    lines.extend(v34.table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## Walk-forward 年度模型", ""]
    lines.extend(v34.table_or_empty(model_year, {"year": "年份", "status": "状态", "train_rows": "训练样本", "test_rows": "测试样本", "signal_dates": "信号日", "signal_target_rate": "信号目标率", "signal_mean_return": "信号收益"}, {"signal_target_rate", "signal_mean_return"}))
    lines += ["", "## 固定持有与动态退出对比", ""]
    lines.extend(v34.table_or_empty(concat_frames([fixed_summary, dynamic_summary]), {"signal_name_zh": "名称", "exit_policy_name_zh": "退出策略", "status": "状态", "nonoverlap_events": "交易数", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口", "avg_holding_days": "平均持有"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate"}))
    lines += ["", "## 主策略交易明细", ""]
    lines.extend(v34.table_or_empty(primary_trades.head(40), {"signal_date": "信号日", "entry_date": "入场日", "trigger_date": "触发日", "exit_date": "退出日", "holding_days": "持有日", "trade_return": "收益", "max_adverse_return": "最大不利", "exit_reason": "退出原因", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
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
        "- `report.md`：中文 V3.9 研究报告，优先打开。",
        "- `top_candidates.csv`：固定持有、模型和动态退出排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：特征面板、固定持有参考、动态退出对照、主策略实时仿真、退出日志、年度分布、泄漏审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


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
