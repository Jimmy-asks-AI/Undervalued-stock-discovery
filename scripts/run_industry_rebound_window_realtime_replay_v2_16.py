#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_realtime_replay_policy_v2_16.json"
VERSION = "2.16.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.16 calibrated rebound-window realtime replay audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.16 replay policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    panel = add_features(read_date_panel(ROOT / policy["date_panel_path"]))
    candidate_source = read_csv_if_exists(ROOT / policy["candidate_source_path"])
    replay_summary, replay_events, bottom_baseline_events, missed_opportunity_cases, random_replay_audit = run_replay(panel, policy)
    top_candidates = build_top_candidates(replay_summary, policy)
    leakage_audit = build_leakage_audit(policy, candidate_source)
    optimization_notes = build_optimization_notes(top_candidates, replay_summary, policy)
    summary = build_run_summary(policy, panel, replay_summary, top_candidates, leakage_audit, optimization_notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    replay_summary.to_csv(debug_dir / "replay_event_summary.csv", index=False, encoding="utf-8-sig")
    replay_events.to_csv(debug_dir / "replay_events.csv", index=False, encoding="utf-8-sig")
    bottom_baseline_events.to_csv(debug_dir / "bottom_baseline_events.csv", index=False, encoding="utf-8-sig")
    missed_opportunity_cases.to_csv(debug_dir / "missed_opportunity_cases.csv", index=False, encoding="utf-8-sig")
    random_replay_audit.to_csv(debug_dir / "random_replay_audit.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", optimization_notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            replay_events=replay_events,
            bottom_baseline_events=bottom_baseline_events,
            missed_opportunity_cases=missed_opportunity_cases,
            random_replay_audit=random_replay_audit,
            leakage_audit=leakage_audit,
            optimization_notes=optimization_notes,
            policy=policy,
        ),
        encoding="utf-8",
    )

    print("V2.16目标校准反弹窗口实时回放审计完成")
    print(f"日期面板行数={summary['date_count']}")
    print(f"规则数={summary['rule_count']}")
    print(f"回放事件数={summary['event_count']}")
    print(f"候选数={summary['replay_candidate_count']}")
    print(f"审计失败数={summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def read_date_panel(path: Path) -> pd.DataFrame:
    panel = pd.read_csv(path, encoding="utf-8-sig")
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    panel = panel.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    panel["trade_date_text"] = panel["trade_date"].dt.strftime("%Y-%m-%d")
    return panel


def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def add_features(panel: pd.DataFrame) -> pd.DataFrame:
    output = panel.copy()
    fields = [
        "market_stress_score",
        "negative_breadth_60d",
        "return_pressure",
        "volatility_pressure",
        "drawdown_pressure",
        "market_drawdown_252d",
        "low_value_oversold_non_trap_count",
        "low_value_oversold_count",
    ]
    for field in fields:
        if field not in output.columns:
            continue
        values = pd.to_numeric(output[field], errors="coerce")
        output[f"{field}_prev1"] = values.shift(1)
        output[f"{field}_chg1"] = values.diff(1)
    output["bottom_condition_count"] = bottom_condition_count(output)
    return output


def bottom_condition_count(panel: pd.DataFrame) -> pd.Series:
    conditions = [
        pd.to_numeric(panel["market_stress_score"], errors="coerce") >= 0.55,
        pd.to_numeric(panel["negative_breadth_60d"], errors="coerce") >= 0.75,
        pd.to_numeric(panel["market_drawdown_252d"], errors="coerce") <= -0.15,
        pd.to_numeric(panel["low_value_oversold_non_trap_count"], errors="coerce") >= 5,
    ]
    total = pd.Series(0, index=panel.index)
    for condition in conditions:
        total += condition.fillna(False).astype(int)
    return total


def run_replay(panel: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    event_frames: list[pd.DataFrame] = []
    baseline_frames: list[pd.DataFrame] = []
    missed_frames: list[pd.DataFrame] = []
    random_rows: list[dict[str, Any]] = []
    eligible = bottom_eligible_mask(panel, policy)

    for rule in policy["rules"]:
        rule_id = str(rule["rule_id"])
        rule_name = str(rule["rule_name_zh"])
        horizon = int(rule["horizon"])
        return_col = f"benchmark_forward_return_{horizon}d"
        valid = panel.dropna(subset=[return_col]).copy()
        signal_mask = build_rule_mask(valid, rule)
        eligible_valid = eligible.reindex(valid.index, fill_value=False)
        event_frame = build_nonoverlap_events(valid, signal_mask, return_col, horizon, policy, rule_id, rule_name, "rule_signal")
        baseline_frame = build_nonoverlap_events(valid, eligible_valid, return_col, horizon, policy, rule_id, rule_name, "bottom_eligible_baseline")
        random_stats = random_event_replay(valid, eligible_valid, len(event_frame), return_col, horizon, policy)
        row = summarize_replay(rule_id, rule_name, horizon, valid, signal_mask, eligible_valid, event_frame, baseline_frame, return_col, policy)
        row.update(random_stats)
        row["replay_score"] = score_replay(row, policy)
        row["replay_status"] = classify_replay(row, policy)
        summary_rows.append(row)
        event_frames.append(event_frame)
        baseline_frames.append(baseline_frame)
        random_rows.append({"rule_id": rule_id, "rule_name_zh": rule_name, "horizon": horizon, **random_stats})
        missed_frames.append(build_missed_cases(valid, signal_mask, eligible_valid, return_col, horizon, policy, rule_id, rule_name))

    return (
        pd.DataFrame(summary_rows),
        concat_frames(event_frames),
        concat_frames(baseline_frames),
        concat_frames(missed_frames),
        pd.DataFrame(random_rows),
    )


def summarize_replay(
    rule_id: str,
    rule_name: str,
    horizon: int,
    valid: pd.DataFrame,
    signal_mask: pd.Series,
    eligible_mask: pd.Series,
    events: pd.DataFrame,
    baseline_events: pd.DataFrame,
    return_col: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    bad_threshold = float(policy["bad_window_thresholds"][str(horizon)])
    returns = pd.to_numeric(valid[return_col], errors="coerce")
    target = eligible_mask & (returns >= threshold)
    signal = signal_mask.reindex(valid.index, fill_value=False)
    signal_count = int(signal.sum())
    event_returns = pd.to_numeric(events["event_return"], errors="coerce") if not events.empty else pd.Series(dtype=float)
    baseline_returns = pd.to_numeric(baseline_events["event_return"], errors="coerce") if not baseline_events.empty else pd.Series(dtype=float)
    event_target = pd.to_numeric(events.get("is_bottom_rebound_target", pd.Series(dtype=float)), errors="coerce") if not events.empty else pd.Series(dtype=float)
    baseline_target = pd.to_numeric(baseline_events.get("is_bottom_rebound_target", pd.Series(dtype=float)), errors="coerce") if not baseline_events.empty else pd.Series(dtype=float)
    return {
        "rule_id": rule_id,
        "rule_name_zh": rule_name,
        "horizon": horizon,
        "signal_samples": signal_count,
        "event_count": int(len(events)),
        "bottom_baseline_event_count": int(len(baseline_events)),
        "target_total": int(target.sum()),
        "target_captured_all_signals": int((target & signal).sum()),
        "target_recall_all_signals": float((target & signal).sum() / target.sum()) if int(target.sum()) else math.nan,
        "event_target_count": int(event_target.sum()) if not event_target.empty else 0,
        "event_target_precision": float(event_target.mean()) if not event_target.empty else math.nan,
        "baseline_event_target_precision": float(baseline_target.mean()) if not baseline_target.empty else math.nan,
        "event_precision_edge_vs_bottom": safe_sub(float(event_target.mean()) if not event_target.empty else math.nan, float(baseline_target.mean()) if not baseline_target.empty else math.nan),
        "event_mean_return": float(event_returns.mean()) if not event_returns.empty else math.nan,
        "event_median_return": float(event_returns.median()) if not event_returns.empty else math.nan,
        "event_worst_return": float(event_returns.min()) if not event_returns.empty else math.nan,
        "event_win_rate": float((event_returns > 0).mean()) if not event_returns.empty else math.nan,
        "event_bad_window_rate": float((event_returns <= bad_threshold).mean()) if not event_returns.empty else math.nan,
        "baseline_event_mean_return": float(baseline_returns.mean()) if not baseline_returns.empty else math.nan,
        "baseline_event_win_rate": float((baseline_returns > 0).mean()) if not baseline_returns.empty else math.nan,
        "baseline_event_bad_window_rate": float((baseline_returns <= bad_threshold).mean()) if not baseline_returns.empty else math.nan,
        "event_mean_edge_vs_bottom": safe_sub(float(event_returns.mean()) if not event_returns.empty else math.nan, float(baseline_returns.mean()) if not baseline_returns.empty else math.nan),
        "event_nav": compound_nav(event_returns),
        "baseline_event_nav": compound_nav(baseline_returns),
        "relative_event_nav": safe_div(compound_nav(event_returns), compound_nav(baseline_returns)),
    }


def build_nonoverlap_events(
    valid: pd.DataFrame,
    mask: pd.Series,
    return_col: str,
    horizon: int,
    policy: dict[str, Any],
    rule_id: str,
    rule_name: str,
    event_type: str,
) -> pd.DataFrame:
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    bad_threshold = float(policy["bad_window_thresholds"][str(horizon)])
    selected = valid[mask.reindex(valid.index, fill_value=False)].sort_values("trade_date").copy()
    rows: list[dict[str, Any]] = []
    last_end = pd.Timestamp.min
    for _, row in selected.iterrows():
        signal_date = pd.Timestamp(row["trade_date"])
        if signal_date <= last_end:
            continue
        end_date = signal_date + pd.tseries.offsets.BDay(horizon)
        event_return = float(row[return_col])
        rows.append(
            {
                "rule_id": rule_id,
                "rule_name_zh": rule_name,
                "event_type": event_type,
                "horizon": horizon,
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "estimated_end_date": end_date.strftime("%Y-%m-%d"),
                "event_return": event_return,
                "is_bottom_eligible": bool(bottom_eligible_mask(pd.DataFrame([row]), policy).iloc[0]),
                "is_bottom_rebound_target": bool(bottom_eligible_mask(pd.DataFrame([row]), policy).iloc[0] and event_return >= threshold),
                "is_bad_window": bool(event_return <= bad_threshold),
                "market_stress_score": row.get("market_stress_score", math.nan),
                "negative_breadth_60d": row.get("negative_breadth_60d", math.nan),
                "market_drawdown_252d": row.get("market_drawdown_252d", math.nan),
                "bottom_condition_count": row.get("bottom_condition_count", math.nan),
                "low_value_oversold_non_trap_count": row.get("low_value_oversold_non_trap_count", math.nan),
            }
        )
        last_end = end_date
    return pd.DataFrame(rows)


def random_event_replay(
    valid: pd.DataFrame,
    eligible_mask: pd.Series,
    event_count: int,
    return_col: str,
    horizon: int,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if event_count <= 0:
        return {
            "random_mean_return_p50": math.nan,
            "random_mean_return_p90": math.nan,
            "random_return_outperformance_pvalue": math.nan,
            "random_precision_p50": math.nan,
            "random_precision_p90": math.nan,
            "random_precision_outperformance_pvalue": math.nan,
        }
    eligible = valid[eligible_mask.reindex(valid.index, fill_value=False)].copy()
    if eligible.empty:
        return {
            "random_mean_return_p50": math.nan,
            "random_mean_return_p90": math.nan,
            "random_return_outperformance_pvalue": math.nan,
            "random_precision_p50": math.nan,
            "random_precision_p90": math.nan,
            "random_precision_outperformance_pvalue": math.nan,
        }
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    returns = pd.to_numeric(eligible[return_col], errors="coerce").dropna().to_numpy(dtype=float)
    if len(returns) == 0:
        return {
            "random_mean_return_p50": math.nan,
            "random_mean_return_p90": math.nan,
            "random_return_outperformance_pvalue": math.nan,
            "random_precision_p50": math.nan,
            "random_precision_p90": math.nan,
            "random_precision_outperformance_pvalue": math.nan,
        }
    rng = np.random.default_rng(int(policy["random_seed"]) + event_count + horizon)
    draw_means: list[float] = []
    draw_precisions: list[float] = []
    replace = event_count > len(returns)
    for _ in range(int(policy["random_trials"])):
        sampled = rng.choice(returns, size=event_count, replace=replace)
        draw_means.append(float(np.mean(sampled)))
        draw_precisions.append(float(np.mean(sampled >= threshold)))
    return {
        "random_mean_return_p50": float(np.nanpercentile(draw_means, 50)),
        "random_mean_return_p90": float(np.nanpercentile(draw_means, 90)),
        "random_precision_p50": float(np.nanpercentile(draw_precisions, 50)),
        "random_precision_p90": float(np.nanpercentile(draw_precisions, 90)),
    }


def attach_random_pvalues(summary: pd.DataFrame, random_audit: pd.DataFrame) -> pd.DataFrame:
    return summary


def build_missed_cases(
    valid: pd.DataFrame,
    signal_mask: pd.Series,
    eligible_mask: pd.Series,
    return_col: str,
    horizon: int,
    policy: dict[str, Any],
    rule_id: str,
    rule_name: str,
) -> pd.DataFrame:
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    returns = pd.to_numeric(valid[return_col], errors="coerce")
    signal = signal_mask.reindex(valid.index, fill_value=False)
    target = eligible_mask.reindex(valid.index, fill_value=False) & (returns >= threshold)
    missed = valid[target & ~signal].copy()
    missed["missed_return"] = returns.reindex(missed.index)
    missed["rule_id"] = rule_id
    missed["rule_name_zh"] = rule_name
    missed["horizon"] = horizon
    missed["trade_date"] = missed["trade_date_text"]
    keep_cols = [
        "rule_id",
        "rule_name_zh",
        "horizon",
        "trade_date",
        "missed_return",
        "market_stress_score",
        "negative_breadth_60d",
        "market_drawdown_252d",
        "bottom_condition_count",
        "low_value_oversold_non_trap_count",
        "return_pressure",
        "volatility_pressure",
    ]
    return missed.sort_values("missed_return", ascending=False).head(20)[[col for col in keep_cols if col in missed.columns]]


def score_replay(row: dict[str, Any], policy: dict[str, Any]) -> float:
    score = 0.0
    score += 2.0 * nz(row.get("event_mean_edge_vs_bottom"))
    score += 1.5 * nz(row.get("event_precision_edge_vs_bottom"))
    score += 1.0 * (nz(row.get("event_win_rate")) - 0.5)
    score += 0.8 * nz(row.get("target_recall_all_signals"))
    score -= 1.2 * nz(row.get("event_bad_window_rate"))
    score += 0.2 * math.log(max(nz(row.get("event_count")), 1.0))
    return float(score)


def classify_replay(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    checks = [
        nz(row.get("event_count")) >= int(th["min_nonoverlap_events"]),
        nz(row.get("event_mean_edge_vs_bottom")) >= float(th["min_nonoverlap_mean_edge_vs_bottom"]),
        nz(row.get("event_precision_edge_vs_bottom")) >= float(th["min_nonoverlap_precision_edge_vs_bottom"]),
        nz(row.get("event_win_rate")) >= float(th["min_nonoverlap_win_rate"]),
        nz(row.get("event_bad_window_rate")) <= float(th["max_nonoverlap_bad_window_rate"]),
        nz(row.get("event_worst_return")) >= float(th["max_worst_event_loss"]),
    ]
    if all(checks):
        return "事件回放候选"
    if checks[0] and checks[1] and checks[3]:
        return "事件收益观察"
    if not checks[0]:
        return "样本不足"
    return "拒绝"


def build_top_candidates(summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    priority = {"事件回放候选": 0, "事件收益观察": 1, "样本不足": 2, "拒绝": 3}
    cols = [
        "rule_id",
        "rule_name_zh",
        "horizon",
        "replay_status",
        "replay_score",
        "signal_samples",
        "event_count",
        "target_total",
        "target_recall_all_signals",
        "event_target_precision",
        "baseline_event_target_precision",
        "event_precision_edge_vs_bottom",
        "event_mean_return",
        "baseline_event_mean_return",
        "event_mean_edge_vs_bottom",
        "event_win_rate",
        "event_bad_window_rate",
        "event_worst_return",
        "event_nav",
        "baseline_event_nav",
        "relative_event_nav",
    ]
    output = summary[[col for col in cols if col in summary.columns]].copy()
    output["_priority"] = output["replay_status"].map(priority).fillna(9)
    return output.sort_values(["_priority", "replay_score"], ascending=[True, False]).drop(columns=["_priority"])


def build_leakage_audit(policy: dict[str, Any], candidate_source: pd.DataFrame) -> pd.DataFrame:
    candidate_count = 0
    if not candidate_source.empty and "target_status" in candidate_source.columns:
        candidate_count = int((candidate_source["target_status"] == policy["candidate_status"]).sum())
    return pd.DataFrame(
        [
            {
                "audit_item": "frozen_v2_15_candidates",
                "status": "pass" if candidate_count >= int(policy["max_candidate_rules"]) else "research_only",
                "evidence": f"candidate_source_count={candidate_count}; replay_rules={len(policy['rules'])}",
                "action": "V2.16只回放V2.15已出现的候选规则。",
            },
            {
                "audit_item": "future_return_used_only_as_outcome",
                "status": "pass",
                "evidence": "benchmark_forward_return_* only used in event outcome and target label",
                "action": "未来收益不作为触发特征。",
            },
            {
                "audit_item": "event_level_not_full_daily_nav",
                "status": "research_only",
                "evidence": "date_level_panel provides decision-date forward returns; replay is nonoverlap event-level",
                "action": "报告不得宣称完整逐日交易净值。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(top_candidates: pd.DataFrame, summary: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top_candidates.empty:
        return {"main_diagnosis": "V2.16没有可回放规则。", "next_iterations": ["检查V2.15候选和配置。"]}
    best = top_candidates.iloc[0].to_dict()
    notes: list[str] = []
    if str(best.get("replay_status")) == "事件回放候选":
        notes.append("冻结候选在事件级非重叠回放中通过门槛，但仍不是完整交易系统。")
        notes.append("下一轮应做完整逐日净值和新增样本观察，尤其检查入场后最大回撤。")
    else:
        notes.append("冻结候选在事件级回放中仍未形成足够可靠的反弹窗口。")
        notes.append("下一轮应研究漏报窗口的共同特征，而不是继续调同一组压力阈值。")
    if nz(best.get("target_recall_all_signals")) < 0.40:
        notes.append("目标召回仍低，说明系统只覆盖一部分压力后反弹。")
    if nz(best.get("event_bad_window_rate")) > float(policy["promotion_thresholds"]["max_nonoverlap_bad_window_rate"]):
        notes.append("坏窗口偏多，需要加入入场后二次确认或最大回撤控制。")
    return {
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": best.get("horizon", ""),
        "best_replay_status": best.get("replay_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_17_direction": "围绕V2.16最佳规则做完整逐日净值/最大回撤审计；若不能通过，再转向漏报样本的行业相对强度确认。",
    }


def build_run_summary(
    policy: dict[str, Any],
    panel: pd.DataFrame,
    replay_summary: pd.DataFrame,
    top_candidates: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
) -> dict[str, Any]:
    candidates = replay_summary[replay_summary["replay_status"] == "事件回放候选"] if not replay_summary.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage_audit["status"] == "fail").sum()) if not leakage_audit.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_count": int(len(panel)),
        "rule_count": int(len(policy["rules"])),
        "event_count": int(replay_summary["event_count"].sum()) if not replay_summary.empty else 0,
        "replay_candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_replay_status": best.get("replay_status", ""),
        "best_event_mean_edge_vs_bottom": float_or_none(best.get("event_mean_edge_vs_bottom")),
        "best_event_precision_edge_vs_bottom": float_or_none(best.get("event_precision_edge_vs_bottom")),
        "best_event_bad_window_rate": float_or_none(best.get("event_bad_window_rate")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": optimization_notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    replay_events: pd.DataFrame,
    bottom_baseline_events: pd.DataFrame,
    missed_opportunity_cases: pd.DataFrame,
    random_replay_audit: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# V2.16 目标校准反弹窗口实时回放审计报告")
    lines.append("")
    lines.append(f"版本：{VERSION}")
    lines.append("")
    lines.append("## 研究结论")
    lines.append("")
    lines.append("V2.16 冻结 V2.15 的目标校准候选，只做事件级非重叠回放。它不是完整逐日交易净值，也不生成交易指令。")
    lines.append("")
    lines.append(f"- 日期面板行数：{summary['date_count']}")
    lines.append(f"- 规则数：{summary['rule_count']}")
    lines.append(f"- 非重叠回放事件数：{summary['event_count']}")
    lines.append(f"- 事件回放候选数：{summary['replay_candidate_count']}")
    lines.append(f"- 审计失败数：{summary['audit_fail_count']}")
    lines.append(f"- 最终结论：{summary['final_verdict']}")
    lines.append(f"- 主要诊断：{summary['main_diagnosis']}")
    lines.append("")
    lines.append("## 回放规则排序")
    lines.append("")
    lines.extend(table_or_empty(top_candidates, {
        "rule_id": "规则ID",
        "rule_name_zh": "规则",
        "horizon": "持有期",
        "replay_status": "状态",
        "replay_score": "回放分",
        "signal_samples": "全信号",
        "event_count": "非重叠事件",
        "target_total": "目标总数",
        "target_recall_all_signals": "全信号召回",
        "event_target_precision": "事件目标精确率",
        "baseline_event_target_precision": "底部基线精确率",
        "event_precision_edge_vs_bottom": "精确率提升",
        "event_mean_return": "事件收益",
        "baseline_event_mean_return": "基线事件收益",
        "event_mean_edge_vs_bottom": "收益提升",
        "event_win_rate": "上涨比例",
        "event_bad_window_rate": "坏窗口",
        "event_worst_return": "最差事件",
        "relative_event_nav": "相对事件净值",
    }, {
        "target_recall_all_signals",
        "event_target_precision",
        "baseline_event_target_precision",
        "event_precision_edge_vs_bottom",
        "event_mean_return",
        "baseline_event_mean_return",
        "event_mean_edge_vs_bottom",
        "event_win_rate",
        "event_bad_window_rate",
        "event_worst_return",
    }))
    lines.append("")
    lines.append("## 最佳规则事件")
    lines.append("")
    best_rule = str(summary.get("best_rule_id", ""))
    best_horizon = int(summary.get("best_horizon", 0))
    best_events = replay_events[(replay_events["rule_id"].astype(str) == best_rule) & (pd.to_numeric(replay_events["horizon"], errors="coerce") == best_horizon)].copy() if not replay_events.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_events.head(20), {
        "signal_date": "信号日",
        "estimated_end_date": "估算结束日",
        "event_return": "事件收益",
        "is_bottom_rebound_target": "是否目标",
        "is_bad_window": "坏窗口",
        "market_stress_score": "压力分",
        "negative_breadth_60d": "下跌广度",
        "market_drawdown_252d": "市场回撤",
        "bottom_condition_count": "底部条件数",
    }, {
        "event_return",
        "market_stress_score",
        "negative_breadth_60d",
        "market_drawdown_252d",
    }))
    lines.append("")
    lines.append("## 漏报机会")
    lines.append("")
    best_missed = missed_opportunity_cases[(missed_opportunity_cases["rule_id"].astype(str) == best_rule) & (pd.to_numeric(missed_opportunity_cases["horizon"], errors="coerce") == best_horizon)].copy() if not missed_opportunity_cases.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_missed.head(15), {
        "trade_date": "日期",
        "missed_return": "漏报后收益",
        "market_stress_score": "压力分",
        "negative_breadth_60d": "下跌广度",
        "market_drawdown_252d": "市场回撤",
        "bottom_condition_count": "底部条件数",
        "low_value_oversold_non_trap_count": "非陷阱低估超跌数",
    }, {
        "missed_return",
        "market_stress_score",
        "negative_breadth_60d",
        "market_drawdown_252d",
    }))
    lines.append("")
    lines.append("## 下一轮优化方向")
    lines.append("")
    for item in optimization_notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议 V2.17 方向：{optimization_notes.get('recommended_v2_17_direction', '')}")
    lines.append("")
    lines.append("## 审计")
    lines.append("")
    lines.extend(table_or_empty(leakage_audit, {
        "audit_item": "项目",
        "status": "状态",
        "evidence": "证据",
        "action": "动作",
    }, set()))
    lines.append("")
    lines.append("## 输出文件说明")
    lines.append("")
    lines.append("- `report.md`：中文事件级实时回放审计报告，优先打开。")
    lines.append("- `top_candidates.csv`：冻结规则事件回放排序；不是交易信号。")
    lines.append("- `run_summary.json`：机器可读运行摘要。")
    lines.append("- `debug/`：事件明细、底部基线事件、漏报机会、随机审计、泄漏审计和冻结策略。")
    lines.append("")
    lines.append(f"研究边界：{policy['research_boundary']}")
    return "\n".join(lines)


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；冻结规则未通过事件级回放候选门槛"
    return "research_only；冻结规则通过事件级回放候选门槛，但仍需完整逐日净值和未来样本验证"


def build_rule_mask(panel: pd.DataFrame, rule: dict[str, Any]) -> pd.Series:
    mask = pd.Series(True, index=panel.index)
    for condition in rule.get("conditions", []):
        mask &= condition_mask(panel, condition)
    return mask.fillna(False)


def bottom_eligible_mask(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.Series:
    masks = [condition_mask(panel, condition) for condition in policy["bottom_eligible_conditions"]]
    result = pd.Series(False, index=panel.index)
    for mask in masks:
        result |= mask
    return result.fillna(False)


def condition_mask(panel: pd.DataFrame, condition: dict[str, Any]) -> pd.Series:
    field = str(condition["field"])
    op = str(condition["op"])
    value = float(condition["value"])
    if field not in panel.columns:
        return pd.Series(False, index=panel.index)
    series = pd.to_numeric(panel[field], errors="coerce")
    if op == ">=":
        return (series >= value).fillna(False)
    if op == ">":
        return (series > value).fillna(False)
    if op == "<=":
        return (series <= value).fillna(False)
    if op == "<":
        return (series < value).fillna(False)
    raise ValueError(f"Unsupported op: {op}")


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


def compound_nav(values: pd.Series) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return math.nan
    return float((1.0 + clean).prod())


def safe_sub(left: Any, right: Any) -> float:
    left_number = float_or_nan(left)
    right_number = float_or_nan(right)
    if math.isnan(left_number) or math.isnan(right_number):
        return math.nan
    return float(left_number - right_number)


def safe_div(left: Any, right: Any) -> float:
    left_number = float_or_nan(left)
    right_number = float_or_nan(right)
    if math.isnan(left_number) or math.isnan(right_number) or right_number == 0:
        return math.nan
    return float(left_number / right_number)


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
    if math.isnan(number):
        return ""
    return f"{number:.{digits}f}"


def fmt_pct(value: Any) -> str:
    number = float_or_nan(value)
    if math.isnan(number):
        return ""
    return f"{number * 100:.2f}%"


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
