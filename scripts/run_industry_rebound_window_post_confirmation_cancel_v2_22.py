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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_post_confirmation_cancel_policy_v2_22.json"
V20_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_online_state_machine_v2_20.py"
VERSION = "2.22.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.22 post-confirmation cancellation audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.22 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    source_policy = read_json(ROOT / policy["source_policy_path"])
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v20 = load_v20_module()
    close_matrix = v20.load_close_matrix(ROOT / policy["history_dir"])
    features = v20.build_daily_features(close_matrix, source_policy)
    v20_summary, v20_trades, _, _ = v20.run_state_machine_audit(features, source_policy)
    guard_summary, guard_trades, cancelled_entries, transition_log = run_guard_audit(v20, features, source_policy, policy)
    comparison = build_guard_comparison(guard_summary, v20_summary, policy)
    top_candidates = build_top_candidates(comparison)
    leakage_audit = build_leakage_audit(policy, source_policy, features, guard_trades, cancelled_entries)
    notes = build_optimization_notes(top_candidates, comparison, cancelled_entries, policy)
    run_summary = build_run_summary(policy, features, comparison, top_candidates, leakage_audit, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    guard_summary.to_csv(debug_dir / "guard_summary.csv", index=False, encoding="utf-8-sig")
    guard_trades.to_csv(debug_dir / "guard_trades.csv", index=False, encoding="utf-8-sig")
    cancelled_entries.to_csv(debug_dir / "cancelled_entries.csv", index=False, encoding="utf-8-sig")
    comparison.to_csv(debug_dir / "guard_comparison.csv", index=False, encoding="utf-8-sig")
    transition_log.to_csv(debug_dir / "guard_transition_log.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", {"v2_22_policy": policy, "source_v2_20_policy": source_policy})
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(run_summary, top_candidates, guard_trades, cancelled_entries, comparison, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V2.22确认后撤销机制审计完成")
    print(f"日频特征行数={run_summary['daily_feature_count']}")
    print(f"守门场景数={run_summary['guard_scenario_count']}")
    print(f"撤销候选数={run_summary['guard_candidate_count']}")
    print(f"最佳策略={run_summary['best_strategy_id']}")
    print(f"最佳守门={run_summary['best_guard_id']}")
    print(f"审计失败数={run_summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v20_module() -> Any:
    spec = importlib.util.spec_from_file_location("v20_state_machine", V20_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V2.20 state-machine module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_guard_audit(v20: Any, features: pd.DataFrame, source_policy: dict[str, Any], policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    cancel_frames: list[pd.DataFrame] = []
    transition_frames: list[pd.DataFrame] = []
    for strategy in source_policy["strategies"]:
        for guard in policy["guard_scenarios"]:
            trades, cancelled, transitions = run_strategy_with_guard(v20, features, strategy, guard, source_policy, policy)
            summary_rows.append(summarize_guard(strategy, guard, trades, cancelled, policy))
            if not trades.empty:
                trade_frames.append(trades)
            if not cancelled.empty:
                cancel_frames.append(cancelled)
            if not transitions.empty:
                transition_frames.append(transitions)
    return pd.DataFrame(summary_rows), concat_frames(trade_frames), concat_frames(cancel_frames), concat_frames(transition_frames)


def run_strategy_with_guard(
    v20: Any,
    features: pd.DataFrame,
    strategy: dict[str, Any],
    guard: dict[str, Any],
    source_policy: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    trades: list[dict[str, Any]] = []
    cancelled: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    i = 0
    cooldown_until = -1
    dates = features["trade_date_text"].tolist()
    while i < len(features) - 2:
        if i <= cooldown_until:
            i += 1
            continue
        row = features.iloc[i]
        if not v20.conditions_met(row, strategy["watch_conditions"]):
            i += 1
            continue

        watch_start_i = i
        transitions.append(transition_row(strategy, guard, "watch_start", dates[i], "进入观察", row))
        confirmed_i: int | None = None
        expired_i = min(watch_start_i + int(strategy["max_observation_days"]), len(features) - 3)
        j = watch_start_i
        while j <= expired_i:
            obs_days = j - watch_start_i
            current = features.iloc[j]
            if obs_days >= int(strategy["min_observation_days"]) and v20.conditions_met(current, strategy["confirm_conditions"]):
                confirmed_i = j
                transitions.append(transition_row(strategy, guard, "confirmed", dates[j], f"观察{obs_days}日后确认", current))
                break
            j += 1

        if confirmed_i is None:
            transitions.append(transition_row(strategy, guard, "watch_expired", dates[expired_i], "观察期内未确认", features.iloc[expired_i]))
            i = expired_i + 1
            continue

        decision = guard_decision(features, strategy, guard, confirmed_i)
        if decision["cancelled"]:
            cancelled.append({**decision, "strategy_id": strategy["strategy_id"], "strategy_name_zh": strategy["strategy_name_zh"], "horizon": int(strategy["horizon"]), "guard_id": guard["guard_id"], "guard_name_zh": guard["guard_name_zh"]})
            transitions.append(transition_row(strategy, guard, "cancelled", decision["guard_end_date"], decision["cancel_reason"], features.iloc[int(decision["guard_end_index"])]))
            i = int(decision["guard_end_index"]) + 1
            continue

        entry_i = int(decision["entry_index"])
        start_return_i = entry_i + 1
        trade = v20.simulate_trade(features, strategy, entry_i, start_return_i, source_policy)
        if trade:
            trades.append({**trade, "guard_id": guard["guard_id"], "guard_name_zh": guard["guard_name_zh"], **guard_metrics_prefix(decision)})
            transitions.append(transition_row(strategy, guard, "trade_exit", trade["exit_date"], trade["exit_reason"], features.iloc[min(trade["exit_index"], len(features) - 1)]))
            cooldown_until = int(trade["exit_index"]) + int(strategy["cooldown_days"])
            i = cooldown_until + 1
        else:
            cancelled.append({**decision, "cancelled": True, "cancel_reason": "守门后剩余路径不足", "strategy_id": strategy["strategy_id"], "strategy_name_zh": strategy["strategy_name_zh"], "horizon": int(strategy["horizon"]), "guard_id": guard["guard_id"], "guard_name_zh": guard["guard_name_zh"]})
            i = int(decision["guard_end_index"]) + 1
    return pd.DataFrame(trades), pd.DataFrame(cancelled), pd.DataFrame(transitions)


def guard_decision(features: pd.DataFrame, strategy: dict[str, Any], guard: dict[str, Any], confirmed_i: int) -> dict[str, Any]:
    guard_days = int(guard["guard_days"])
    guard_start = confirmed_i + 1
    guard_end = min(confirmed_i + guard_days, len(features) - 2)
    if guard_start > guard_end:
        return {
            "cancelled": True,
            "cancel_reason": "守门期不足",
            "confirmed_index": confirmed_i,
            "confirmed_date": str(features.iloc[confirmed_i]["trade_date_text"]),
            "guard_start_index": guard_start,
            "guard_end_index": confirmed_i,
            "guard_end_date": str(features.iloc[confirmed_i]["trade_date_text"]),
        }
    segment = features.iloc[guard_start : guard_end + 1].copy()
    confirmed = features.iloc[confirmed_i]
    daily = pd.to_numeric(segment["market_daily_return"], errors="coerce").fillna(0.0)
    guard_return = float((1.0 + daily).prod() - 1.0)
    max_stress = float(pd.to_numeric(segment["market_stress_score"], errors="coerce").max())
    max_breadth = float(pd.to_numeric(segment["negative_breadth_60d"], errors="coerce").max())
    stress_increase = max_stress - float_or_nan(confirmed.get("market_stress_score"))
    breadth_increase = max_breadth - float_or_nan(confirmed.get("negative_breadth_60d"))
    reasons: list[str] = []
    if guard.get("cancel_guard_return_lte") is not None and guard_return <= float(guard["cancel_guard_return_lte"]):
        reasons.append("守门期收益急跌")
    if guard.get("cancel_stress_increase_gte") is not None and stress_increase >= float(guard["cancel_stress_increase_gte"]):
        reasons.append("守门期压力再升")
    if guard.get("cancel_breadth_increase_gte") is not None and breadth_increase >= float(guard["cancel_breadth_increase_gte"]):
        reasons.append("守门期下跌广度扩大")
    if guard.get("cancel_max_stress_gte") is not None and max_stress >= float(guard["cancel_max_stress_gte"]):
        reasons.append("守门期压力绝对值过高")
    if guard.get("cancel_max_breadth_gte") is not None and max_breadth >= float(guard["cancel_max_breadth_gte"]):
        reasons.append("守门期普跌绝对值过高")
    entry_i = guard_end + 1
    return {
        "cancelled": bool(reasons),
        "cancel_reason": ";".join(reasons),
        "confirmed_index": int(confirmed_i),
        "confirmed_date": str(features.iloc[confirmed_i]["trade_date_text"]),
        "guard_start_index": int(guard_start),
        "guard_start_date": str(features.iloc[guard_start]["trade_date_text"]),
        "guard_end_index": int(guard_end),
        "guard_end_date": str(features.iloc[guard_end]["trade_date_text"]),
        "entry_index": int(entry_i),
        "entry_date_after_guard": str(features.iloc[entry_i]["trade_date_text"]) if entry_i < len(features) else "",
        "guard_return": guard_return,
        "guard_max_stress": max_stress,
        "guard_max_negative_breadth": max_breadth,
        "guard_stress_increase": stress_increase,
        "guard_breadth_increase": breadth_increase,
    }


def guard_metrics_prefix(decision: dict[str, Any]) -> dict[str, Any]:
    fields = [
        "confirmed_date",
        "guard_start_date",
        "guard_end_date",
        "guard_return",
        "guard_max_stress",
        "guard_max_negative_breadth",
        "guard_stress_increase",
        "guard_breadth_increase",
    ]
    return {field: decision.get(field) for field in fields}


def summarize_guard(strategy: dict[str, Any], guard: dict[str, Any], trades: pd.DataFrame, cancelled: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    attempted = len(trades) + len(cancelled)
    row = {
        "strategy_id": strategy["strategy_id"],
        "strategy_name_zh": strategy["strategy_name_zh"],
        "horizon": int(strategy["horizon"]),
        "guard_id": guard["guard_id"],
        "guard_name_zh": guard["guard_name_zh"],
        "attempted_count": int(attempted),
        "entered_count": int(len(trades)),
        "cancelled_count": int(len(cancelled)),
        "cancel_rate": float(len(cancelled) / attempted) if attempted else math.nan,
    }
    row.update(metrics(trades))
    row["guard_score"] = score_guard(row)
    row["guard_status_raw"] = classify_guard_raw(row, policy)
    return row


def metrics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "mean_return": math.nan,
            "worst_return": math.nan,
            "win_rate": math.nan,
            "bad_window_rate": math.nan,
            "severe_path_rate": math.nan,
            "worst_min_nav_loss": math.nan,
            "worst_max_drawdown": math.nan,
            "stop_trigger_rate": math.nan,
        }
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    return {
        "mean_return": float(returns.mean()),
        "worst_return": float(returns.min()),
        "win_rate": float(trades["is_win"].mean()),
        "bad_window_rate": float(trades["is_bad_window"].mean()),
        "severe_path_rate": float(trades["is_severe_path"].mean()),
        "worst_min_nav_loss": float(pd.to_numeric(trades["min_nav_loss"], errors="coerce").min()),
        "worst_max_drawdown": float(pd.to_numeric(trades["max_drawdown"], errors="coerce").min()),
        "stop_trigger_rate": float((trades["exit_reason"] == "撤退阈值触发").mean()),
    }


def build_guard_comparison(guard_summary: pd.DataFrame, v20_summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if guard_summary.empty:
        return pd.DataFrame()
    baseline = v20_summary[
        [
            "strategy_id",
            "state_mean_return",
            "state_worst_return",
            "state_win_rate",
            "state_bad_window_rate",
            "state_severe_path_rate",
            "state_worst_min_nav_loss",
            "state_worst_max_drawdown",
            "trade_count",
        ]
    ].rename(columns={"trade_count": "v20_trade_count"})
    output = guard_summary.merge(baseline, on="strategy_id", how="left")
    output["delta_mean_return_vs_v20"] = output["mean_return"] - output["state_mean_return"]
    output["delta_bad_window_rate_vs_v20"] = output["state_bad_window_rate"] - output["bad_window_rate"]
    output["delta_severe_path_rate_vs_v20"] = output["state_severe_path_rate"] - output["severe_path_rate"]
    output["delta_worst_min_nav_loss_vs_v20"] = output["worst_min_nav_loss"] - output["state_worst_min_nav_loss"]
    output["delta_worst_return_vs_v20"] = output["worst_return"] - output["state_worst_return"]
    output["guard_status"] = output.apply(lambda row: classify_guard(row, policy), axis=1)
    return output


def score_guard(row: dict[str, Any]) -> float:
    return float(
        2.0 * nz(row.get("mean_return"))
        + 1.0 * (nz(row.get("win_rate")) - 0.5)
        - 1.4 * nz(row.get("bad_window_rate"))
        - 1.1 * nz(row.get("severe_path_rate"))
        + 0.8 * nz(row.get("worst_min_nav_loss"))
        - 0.5 * nz(row.get("cancel_rate"))
    )


def classify_guard_raw(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    if nz(row.get("entered_count")) < int(th["min_entered_trades"]):
        return "样本不足"
    if nz(row.get("cancel_rate"), 1.0) > float(th["max_cancel_rate"]):
        return "撤销过多"
    if nz(row.get("mean_return")) >= 0 and nz(row.get("win_rate")) >= 0.6:
        return "撤销观察"
    return "拒绝"


def classify_guard(row: pd.Series, policy: dict[str, Any]) -> str:
    # Final classification with V2.20 improvement deltas included.
    th = policy["promotion_thresholds"]
    if str(row.get("guard_status_raw")) == "样本不足":
        return "样本不足"
    if str(row.get("guard_status_raw")) == "撤销过多":
        return "撤销过多"
    checks = [
        nz(row.get("mean_return")) >= float(th["min_mean_trade_return"]),
        nz(row.get("win_rate")) >= float(th["min_win_rate"]),
        nz(row.get("bad_window_rate"), 1.0) <= float(th["max_bad_window_rate"]),
        nz(row.get("severe_path_rate"), 1.0) <= float(th["max_severe_path_rate"]),
        nz(row.get("worst_return"), -1.0) >= float(th["max_worst_trade_return"]),
        nz(row.get("worst_min_nav_loss"), -1.0) >= float(th["max_worst_min_nav_loss"]),
        nz(row.get("delta_bad_window_rate_vs_v20")) >= float(th["min_delta_bad_window_rate_vs_v20"]),
        nz(row.get("delta_worst_min_nav_loss_vs_v20")) >= float(th["min_delta_worst_min_nav_loss_vs_v20"]),
    ]
    if all(checks):
        return "确认后撤销候选"
    if checks[0] and checks[1] and checks[2]:
        return "撤销观察"
    return "拒绝"


def build_top_candidates(comparison: pd.DataFrame) -> pd.DataFrame:
    if comparison.empty:
        return pd.DataFrame()
    priority = {"确认后撤销候选": 0, "撤销观察": 1, "样本不足": 2, "撤销过多": 3, "拒绝": 4}
    output = comparison.copy()
    output["_priority"] = output["guard_status"].map(priority).fillna(9)
    output = output.sort_values(["_priority", "guard_score"], ascending=[True, False]).drop(columns=["_priority"])
    columns = [
        "strategy_id",
        "strategy_name_zh",
        "horizon",
        "guard_id",
        "guard_name_zh",
        "guard_status",
        "attempted_count",
        "entered_count",
        "cancelled_count",
        "cancel_rate",
        "mean_return",
        "worst_return",
        "win_rate",
        "bad_window_rate",
        "severe_path_rate",
        "worst_min_nav_loss",
        "worst_max_drawdown",
        "stop_trigger_rate",
        "delta_mean_return_vs_v20",
        "delta_bad_window_rate_vs_v20",
        "delta_worst_min_nav_loss_vs_v20",
        "guard_score",
    ]
    return output[[col for col in columns if col in output.columns]]


def build_leakage_audit(policy: dict[str, Any], source_policy: dict[str, Any], features: pd.DataFrame, trades: pd.DataFrame, cancelled: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "frozen_v2_20_state_machine",
                "status": "pass",
                "evidence": f"source_policy={source_policy['policy_id']}; strategies={len(source_policy['strategies'])}",
                "action": "V2.22只在确认后加入守门期，不改变原始观察和确认条件。",
            },
            {
                "audit_item": "guard_uses_post_confirmation_pre_entry_information",
                "status": "pass",
                "evidence": "guard segment is after confirmation and before guarded entry",
                "action": "守门期信息在正式入场前可观察。",
            },
            {
                "audit_item": "features_available",
                "status": "pass" if not features.empty and (not trades.empty or not cancelled.empty) else "fail",
                "evidence": f"features={len(features)}; trades={len(trades)}; cancelled={len(cancelled)}",
                "action": "复用本地价格构建的日频状态特征。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(top: pd.DataFrame, comparison: pd.DataFrame, cancelled: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top.empty:
        return {"main_diagnosis": "V2.22没有可排序结果。", "next_iterations": ["检查V2.20状态机和守门配置。"]}
    best = top.iloc[0].to_dict()
    candidates = comparison[comparison["guard_status"] == "确认后撤销候选"] if not comparison.empty else pd.DataFrame()
    notes: list[str] = []
    if candidates.empty:
        notes.append("V2.22没有发现确认后撤销候选；撤销机制仍未把系统升级为可靠反弹窗口识别器。")
    else:
        notes.append("V2.22出现确认后撤销候选，但仍必须保持research_only。")
    notes.append(
        f"最佳组合 {best.get('strategy_id', '')}+{best.get('guard_id', '')} 入场 {best.get('entered_count', 0)} 次，"
        f"撤销比例 {fmt_pct(best.get('cancel_rate'))}，坏窗口改善 {fmt_pct(best.get('delta_bad_window_rate_vs_v20'))}。"
    )
    if nz(best.get("entered_count")) < float(policy["promotion_thresholds"]["min_entered_trades"]):
        notes.append("入场样本不足仍是主要障碍，不能把结果作为稳定规律。")
    if nz(best.get("cancel_rate"), 1.0) > float(policy["promotion_thresholds"]["max_cancel_rate"]):
        notes.append("撤销比例过高，说明守门机制可能只是通过少交易来改善指标。")
    if not cancelled.empty:
        reason_counts = cancelled["cancel_reason"].astype(str).str.get_dummies(sep=";").sum().sort_values(ascending=False)
        if not reason_counts.empty:
            notes.append(f"最常见撤销原因是“{reason_counts.index[0]}”，出现 {int(reason_counts.iloc[0])} 次。")
    return {
        "best_strategy_id": best.get("strategy_id", ""),
        "best_guard_id": best.get("guard_id", ""),
        "best_status": best.get("guard_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "如果V2.22仍不通过，应停止围绕同一价格状态机做参数微调，改为构建更长样本的市场状态标签或引入外生风险偏好/流动性代理。"
    }


def build_run_summary(policy: dict[str, Any], features: pd.DataFrame, comparison: pd.DataFrame, top: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = comparison[comparison["guard_status"] == "确认后撤销候选"] if not comparison.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    audit_fail_count = int((leakage["status"] == "fail").sum()) if not leakage.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "daily_feature_count": int(len(features)),
        "guard_scenario_count": int(len(policy["guard_scenarios"])),
        "guard_candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_strategy_id": best.get("strategy_id", ""),
        "best_guard_id": best.get("guard_id", ""),
        "best_guard_status": best.get("guard_status", ""),
        "best_entered_count": int(best.get("entered_count", 0)) if pd.notna(best.get("entered_count", math.nan)) else 0,
        "best_cancel_rate": float_or_none(best.get("cancel_rate")),
        "best_mean_return": float_or_none(best.get("mean_return")),
        "best_bad_window_rate": float_or_none(best.get("bad_window_rate")),
        "best_delta_bad_window_rate_vs_v20": float_or_none(best.get("delta_bad_window_rate_vs_v20")),
        "best_delta_worst_min_nav_loss_vs_v20": float_or_none(best.get("delta_worst_min_nav_loss_vs_v20")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(summary: dict[str, Any], top: pd.DataFrame, trades: pd.DataFrame, cancelled: pd.DataFrame, comparison: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V2.22 确认后撤销机制审计报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines.append("V2.22 冻结 V2.20 状态机，在确认后加入守门期：若守门期内急跌、压力再升或下跌广度扩大，则取消入场。")
    lines += [
        "",
        f"- 日频特征行数：{summary['daily_feature_count']}",
        f"- 守门场景数：{summary['guard_scenario_count']}",
        f"- 确认后撤销候选数：{summary['guard_candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 守门场景排序",
        "",
    ]
    lines.extend(table_or_empty(top, {
        "strategy_id": "策略ID",
        "strategy_name_zh": "策略",
        "guard_id": "守门场景",
        "guard_name_zh": "守门说明",
        "guard_status": "状态",
        "attempted_count": "尝试次数",
        "entered_count": "入场次数",
        "cancelled_count": "撤销次数",
        "cancel_rate": "撤销比例",
        "mean_return": "平均收益",
        "worst_return": "最差收益",
        "win_rate": "胜率",
        "bad_window_rate": "坏窗口",
        "severe_path_rate": "严重路径",
        "worst_min_nav_loss": "最差浮亏",
        "delta_mean_return_vs_v20": "收益较V2.20变化",
        "delta_bad_window_rate_vs_v20": "坏窗口改善",
        "delta_worst_min_nav_loss_vs_v20": "最差浮亏改善",
    }, {
        "cancel_rate",
        "mean_return",
        "worst_return",
        "win_rate",
        "bad_window_rate",
        "severe_path_rate",
        "worst_min_nav_loss",
        "delta_mean_return_vs_v20",
        "delta_bad_window_rate_vs_v20",
        "delta_worst_min_nav_loss_vs_v20",
    }))
    best_strategy = str(summary.get("best_strategy_id", ""))
    best_guard = str(summary.get("best_guard_id", ""))
    best_trades = trades[(trades["strategy_id"].astype(str) == best_strategy) & (trades["guard_id"].astype(str) == best_guard)].copy() if not trades.empty else pd.DataFrame()
    lines += ["", "## 最佳守门实际入场", ""]
    lines.extend(table_or_empty(best_trades, {
        "confirmed_date": "确认日",
        "guard_start_date": "守门开始",
        "guard_end_date": "守门结束",
        "entry_date": "入场日",
        "exit_date": "退出日",
        "exit_reason": "退出原因",
        "guard_return": "守门期收益",
        "guard_stress_increase": "压力增量",
        "guard_breadth_increase": "广度增量",
        "trade_return": "交易收益",
        "min_nav_loss": "最大浮亏",
        "is_bad_window": "坏窗口",
        "is_severe_path": "严重路径",
    }, {
        "guard_return",
        "guard_stress_increase",
        "guard_breadth_increase",
        "trade_return",
        "min_nav_loss",
    }))
    best_cancelled = cancelled[(cancelled["strategy_id"].astype(str) == best_strategy) & (cancelled["guard_id"].astype(str) == best_guard)].copy() if not cancelled.empty else pd.DataFrame()
    lines += ["", "## 最佳守门撤销记录", ""]
    lines.extend(table_or_empty(best_cancelled, {
        "confirmed_date": "确认日",
        "guard_start_date": "守门开始",
        "guard_end_date": "守门结束",
        "cancel_reason": "撤销原因",
        "guard_return": "守门期收益",
        "guard_max_stress": "最高压力",
        "guard_max_negative_breadth": "最高下跌广度",
        "guard_stress_increase": "压力增量",
        "guard_breadth_increase": "广度增量",
    }, {
        "guard_return",
        "guard_max_stress",
        "guard_max_negative_breadth",
        "guard_stress_increase",
        "guard_breadth_increase",
    }))
    lines += ["", "## 下一轮优化方向", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议方向：{notes.get('recommended_next_direction', '')}")
    lines += ["", "## 审计", ""]
    lines.extend(table_or_empty(leakage, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += [
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文确认后撤销机制审计报告，优先打开。",
        "- `top_candidates.csv`：守门场景排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：守门汇总、实际入场、撤销记录、对比、状态转换、审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def transition_row(strategy: dict[str, Any], guard: dict[str, Any], event: str, date: str, message: str, row: pd.Series) -> dict[str, Any]:
    return {
        "strategy_id": strategy["strategy_id"],
        "strategy_name_zh": strategy["strategy_name_zh"],
        "guard_id": guard["guard_id"],
        "guard_name_zh": guard["guard_name_zh"],
        "trade_date": date,
        "event": event,
        "message": message,
        "market_stress_score": row.get("market_stress_score", math.nan),
        "negative_breadth_60d": row.get("negative_breadth_60d", math.nan),
        "market_return_5d": row.get("market_return_5d", math.nan),
        "breadth_repair_5d": row.get("breadth_repair_5d", math.nan),
        "stress_release_5d": row.get("stress_release_5d", math.nan),
    }


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；确认后撤销机制未通过候选门槛"
    return "research_only；存在确认后撤销候选，但仍需年度稳定性和未来样本验证"


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


def table_or_empty(frame: pd.DataFrame, rename: dict[str, str], pct_cols: set[str]) -> list[str]:
    if frame.empty:
        return ["无数据。"]
    display = frame[[col for col in rename if col in frame.columns]].copy()
    for col in display.columns:
        if col in pct_cols:
            display[col] = display[col].map(fmt_pct)
        elif pd.api.types.is_float_dtype(display[col]):
            display[col] = display[col].map(lambda value: fmt_float(value, 3))
    display = display.rename(columns=rename)
    cols = list(display.columns)
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in display.iterrows():
        lines.append("| " + " | ".join(str(row[col]) if pd.notna(row[col]) else "" for col in cols) + " |")
    return lines


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


def fmt_float(value: Any, digits: int = 3) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number:.{digits}f}"


def fmt_pct(value: Any) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number * 100:.2f}%"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=json_default)


def json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return None if math.isnan(number) else number
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    return str(value)


if __name__ == "__main__":
    main()
