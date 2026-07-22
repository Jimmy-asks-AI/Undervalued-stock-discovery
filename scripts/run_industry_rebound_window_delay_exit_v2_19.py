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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_delay_exit_policy_v2_19.json"
VERSION = "2.19.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.19 rebound-window delay-entry and exit-threshold audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.19 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    event_paths = load_event_paths(ROOT / policy["event_daily_path_source"])
    summary, trades, skipped = run_delay_exit_audit(event_paths, policy)
    comparison = build_path_control_comparison(summary)
    top_candidates = build_top_candidates(comparison)
    leakage_audit = build_leakage_audit(policy, event_paths)
    notes = build_optimization_notes(top_candidates, comparison, trades, skipped, policy)
    run_summary = build_run_summary(policy, event_paths, top_candidates, comparison, leakage_audit, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(debug_dir / "delay_exit_summary.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug_dir / "delay_exit_trades.csv", index=False, encoding="utf-8-sig")
    skipped.to_csv(debug_dir / "skipped_events.csv", index=False, encoding="utf-8-sig")
    comparison.to_csv(debug_dir / "path_control_comparison.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(run_summary, top_candidates, comparison, trades, skipped, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V2.19延迟入场与撤退阈值审计完成")
    print(f"源路径行数={run_summary['source_path_row_count']}")
    print(f"路径控制候选数={run_summary['path_control_candidate_count']}")
    print(f"最佳规则={run_summary['best_rule_id']}")
    print(f"最佳场景={run_summary['best_scenario_id']}")
    print(f"审计失败数={run_summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_event_paths(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    required = {"rule_id", "horizon", "signal_date", "path_date", "day_index", "daily_return", "path_nav", "path_drawdown"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"event path source missing columns: {sorted(missing)}")
    output = frame.copy()
    output["rule_id"] = output["rule_id"].astype(str)
    output["horizon"] = pd.to_numeric(output["horizon"], errors="coerce").astype("Int64")
    output["signal_date"] = pd.to_datetime(output["signal_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    output["path_date"] = pd.to_datetime(output["path_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    output["day_index"] = pd.to_numeric(output["day_index"], errors="coerce").astype("Int64")
    output["daily_return"] = pd.to_numeric(output["daily_return"], errors="coerce")
    output["path_nav"] = pd.to_numeric(output["path_nav"], errors="coerce")
    output["path_drawdown"] = pd.to_numeric(output["path_drawdown"], errors="coerce")
    return output.dropna(subset=["horizon", "signal_date", "path_date", "day_index", "path_nav"]).sort_values(
        ["rule_id", "horizon", "signal_date", "day_index"]
    )


def run_delay_exit_audit(event_paths: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    trade_rows: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    thresholds = policy["path_control_thresholds"]
    for rule in policy["rules"]:
        rule_id = str(rule["rule_id"])
        horizon = int(rule["horizon"])
        rule_paths = event_paths[
            (event_paths["rule_id"] == rule_id)
            & (pd.to_numeric(event_paths["horizon"], errors="coerce") == horizon)
        ].copy()
        event_groups = list(rule_paths.groupby(["rule_id", "horizon", "signal_date"], sort=True))

        for scenario in policy["scenarios"]:
            scenario_id = str(scenario["scenario_id"])
            scenario_trades: list[dict[str, Any]] = []
            scenario_skips: list[dict[str, Any]] = []
            for _, path in event_groups:
                result = evaluate_event(path, scenario, thresholds)
                if result["entered"]:
                    row = {
                        "rule_id": rule_id,
                        "rule_name_zh": str(path.get("rule_name_zh", pd.Series([""])).iloc[0]) if "rule_name_zh" in path.columns else "",
                        "horizon": horizon,
                        "scenario_id": scenario_id,
                        "scenario_name_zh": scenario["scenario_name_zh"],
                        **result,
                    }
                    scenario_trades.append(row)
                    trade_rows.append(row)
                else:
                    row = {
                        "rule_id": rule_id,
                        "rule_name_zh": str(path.get("rule_name_zh", pd.Series([""])).iloc[0]) if "rule_name_zh" in path.columns else "",
                        "horizon": horizon,
                        "scenario_id": scenario_id,
                        "scenario_name_zh": scenario["scenario_name_zh"],
                        **result,
                    }
                    scenario_skips.append(row)
                    skipped_rows.append(row)
            summary_rows.append(
                summarize_scenario(
                    rule_id=rule_id,
                    horizon=horizon,
                    scenario=scenario,
                    source_event_count=len(event_groups),
                    trade_rows=scenario_trades,
                    skipped_rows=scenario_skips,
                    thresholds=thresholds,
                )
            )

    return pd.DataFrame(summary_rows), pd.DataFrame(trade_rows), pd.DataFrame(skipped_rows)


def evaluate_event(path: pd.DataFrame, scenario: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    path = path.sort_values("day_index").copy()
    signal_date = str(path["signal_date"].iloc[0])
    baseline_final_return = float(path["path_nav"].iloc[-1] - 1.0)
    delay_days = int(scenario["delay_days"])
    stop_loss = scenario.get("stop_loss")

    if delay_days == 0:
        observation_return = 0.0
        observation_min_return = 0.0
        entry_base_nav = 1.0
        trade_path = path.copy()
        entry_date = str(trade_path["path_date"].iloc[0]) if not trade_path.empty else ""
    else:
        observation = path[pd.to_numeric(path["day_index"], errors="coerce") <= delay_days].copy()
        trade_path = path[pd.to_numeric(path["day_index"], errors="coerce") > delay_days].copy()
        if observation.empty or len(observation) < delay_days or trade_path.empty:
            return skipped_event(signal_date, baseline_final_return, "观察期或剩余持有期不足", delay_days)
        observation_return = float(observation["path_nav"].iloc[-1] - 1.0)
        observation_min_return = float(observation["path_nav"].min() - 1.0)
        entry_base_nav = float(observation["path_nav"].iloc[-1])
        entry_date = str(trade_path["path_date"].iloc[0])

    if bool(scenario.get("require_positive_observation")) and observation_return <= 0:
        return skipped_event(signal_date, baseline_final_return, "观察期收益未转正", delay_days, observation_return, observation_min_return)

    max_observation_loss = scenario.get("max_observation_loss")
    if max_observation_loss is not None and observation_min_return < float(max_observation_loss):
        return skipped_event(signal_date, baseline_final_return, "观察期浮亏超过阈值", delay_days, observation_return, observation_min_return)

    if trade_path.empty or entry_base_nav <= 0:
        return skipped_event(signal_date, baseline_final_return, "无可交易路径", delay_days, observation_return, observation_min_return)

    trade_path = trade_path.copy()
    trade_path["trade_nav"] = trade_path["path_nav"] / entry_base_nav
    trade_path["trade_drawdown"] = trade_path["trade_nav"] / trade_path["trade_nav"].cummax() - 1.0
    trade_path["trade_return"] = trade_path["trade_nav"] - 1.0

    exit_reason = "到期"
    if stop_loss is not None:
        stop_hits = trade_path[trade_path["trade_return"] <= float(stop_loss)]
        if not stop_hits.empty:
            first_hit_index = stop_hits.index[0]
            trade_path = trade_path.loc[:first_hit_index].copy()
            exit_reason = "撤退阈值触发"

    nav = pd.to_numeric(trade_path["trade_nav"], errors="coerce")
    drawdown = pd.to_numeric(trade_path["trade_drawdown"], errors="coerce")
    min_nav_loss = float(nav.min() - 1.0)
    max_drawdown = float(drawdown.min())
    min_nav_idx = nav.idxmin()
    max_dd_idx = drawdown.idxmin()

    return {
        "entered": True,
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": str(trade_path["path_date"].iloc[-1]),
        "exit_reason": exit_reason,
        "delay_days": delay_days,
        "trade_days": int(len(trade_path)),
        "observation_return": observation_return,
        "observation_min_return": observation_min_return,
        "baseline_final_return": baseline_final_return,
        "trade_final_return": float(nav.iloc[-1] - 1.0),
        "opportunity_delta_vs_immediate": float(nav.iloc[-1] - 1.0 - baseline_final_return),
        "min_trade_nav_loss": min_nav_loss,
        "max_trade_drawdown": max_drawdown,
        "day_to_min_nav_after_entry": int(trade_path.loc[min_nav_idx, "day_index"]) - delay_days,
        "day_to_max_drawdown_after_entry": int(trade_path.loc[max_dd_idx, "day_index"]) - delay_days,
        "is_trade_positive": bool(nav.iloc[-1] > 1.0),
        "is_severe_path": bool(
            (min_nav_loss < float(thresholds["max_allowed_min_nav_drawdown"]))
            or (max_drawdown < float(thresholds["max_allowed_peak_drawdown"]))
        ),
        "skip_reason": "",
    }


def skipped_event(
    signal_date: str,
    baseline_final_return: float,
    reason: str,
    delay_days: int,
    observation_return: float | None = None,
    observation_min_return: float | None = None,
) -> dict[str, Any]:
    return {
        "entered": False,
        "signal_date": signal_date,
        "entry_date": "",
        "exit_date": "",
        "exit_reason": "",
        "delay_days": delay_days,
        "trade_days": 0,
        "observation_return": observation_return,
        "observation_min_return": observation_min_return,
        "baseline_final_return": baseline_final_return,
        "trade_final_return": math.nan,
        "opportunity_delta_vs_immediate": math.nan,
        "min_trade_nav_loss": math.nan,
        "max_trade_drawdown": math.nan,
        "day_to_min_nav_after_entry": math.nan,
        "day_to_max_drawdown_after_entry": math.nan,
        "is_trade_positive": False,
        "is_severe_path": False,
        "skip_reason": reason,
    }


def summarize_scenario(
    rule_id: str,
    horizon: int,
    scenario: dict[str, Any],
    source_event_count: int,
    trade_rows: list[dict[str, Any]],
    skipped_rows: list[dict[str, Any]],
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    trades = pd.DataFrame(trade_rows)
    skipped = pd.DataFrame(skipped_rows)
    entered_count = int(len(trades))
    skipped_count = int(len(skipped))
    row: dict[str, Any] = {
        "rule_id": rule_id,
        "horizon": horizon,
        "scenario_id": scenario["scenario_id"],
        "scenario_name_zh": scenario["scenario_name_zh"],
        "source_event_count": source_event_count,
        "entered_event_count": entered_count,
        "skipped_event_count": skipped_count,
        "skip_rate": skipped_count / source_event_count if source_event_count else math.nan,
        "delay_days": int(scenario["delay_days"]),
        "stop_loss": scenario.get("stop_loss"),
        "require_positive_observation": bool(scenario.get("require_positive_observation")),
        "max_observation_loss": scenario.get("max_observation_loss"),
    }
    if trades.empty:
        row.update(
            {
                "mean_trade_return": math.nan,
                "median_trade_return": math.nan,
                "worst_trade_return": math.nan,
                "trade_win_rate": math.nan,
                "worst_min_trade_nav_loss": math.nan,
                "worst_max_trade_drawdown": math.nan,
                "severe_path_rate": math.nan,
                "stop_trigger_rate": math.nan,
                "mean_opportunity_delta_vs_immediate": math.nan,
                "skipped_positive_event_count": int((skipped.get("baseline_final_return", pd.Series(dtype=float)) > 0).sum()),
                "skipped_mean_baseline_return": float_or_none(skipped.get("baseline_final_return", pd.Series(dtype=float)).mean()),
                "path_control_score": -999.0,
                "path_control_status": "样本不足",
            }
        )
        return row

    row.update(
        {
            "mean_trade_return": float(trades["trade_final_return"].mean()),
            "median_trade_return": float(trades["trade_final_return"].median()),
            "worst_trade_return": float(trades["trade_final_return"].min()),
            "trade_win_rate": float(trades["is_trade_positive"].mean()),
            "worst_min_trade_nav_loss": float(trades["min_trade_nav_loss"].min()),
            "worst_max_trade_drawdown": float(trades["max_trade_drawdown"].min()),
            "severe_path_rate": float(trades["is_severe_path"].mean()),
            "stop_trigger_rate": float((trades["exit_reason"] == "撤退阈值触发").mean()),
            "mean_opportunity_delta_vs_immediate": float(trades["opportunity_delta_vs_immediate"].mean()),
            "skipped_positive_event_count": int((skipped.get("baseline_final_return", pd.Series(dtype=float)) > 0).sum()),
            "skipped_mean_baseline_return": float_or_none(skipped.get("baseline_final_return", pd.Series(dtype=float)).mean()),
        }
    )
    row["path_control_score"] = score_path_control(row)
    row["path_control_status"] = classify_path_control(row, thresholds)
    return row


def score_path_control(row: dict[str, Any]) -> float:
    return float(
        2.0 * nz(row.get("mean_trade_return"))
        + 1.2 * (nz(row.get("trade_win_rate")) - 0.5)
        - 1.7 * nz(row.get("severe_path_rate"))
        + 1.4 * nz(row.get("worst_min_trade_nav_loss"))
        + 0.8 * nz(row.get("worst_max_trade_drawdown"))
        - 0.4 * nz(row.get("skip_rate"))
    )


def classify_path_control(row: dict[str, Any], thresholds: dict[str, Any]) -> str:
    checks = {
        "sample": nz(row.get("entered_event_count")) >= int(thresholds["min_entered_events"]),
        "return": nz(row.get("mean_trade_return")) >= float(thresholds["min_mean_trade_return"]),
        "win": nz(row.get("trade_win_rate")) >= float(thresholds["min_trade_win_rate"]),
        "severe": nz(row.get("severe_path_rate"), 1.0) <= float(thresholds["max_severe_path_rate"]),
        "min_loss": nz(row.get("worst_min_trade_nav_loss"), -1.0) >= float(thresholds["max_allowed_min_nav_drawdown"]),
        "peak_dd": nz(row.get("worst_max_trade_drawdown"), -1.0) >= float(thresholds["max_allowed_peak_drawdown"]),
        "skip": nz(row.get("skip_rate"), 1.0) <= float(thresholds["max_skip_rate"]),
    }
    if all(checks.values()):
        return "小样本路径控制候选"
    if not checks["sample"]:
        return "样本不足"
    if checks["return"] and checks["win"] and checks["min_loss"]:
        return "路径控制观察"
    return "拒绝"


def build_path_control_comparison(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    output = summary.copy()
    baseline = output[output["scenario_id"] == "immediate_hold"][
        ["rule_id", "horizon", "mean_trade_return", "trade_win_rate", "worst_min_trade_nav_loss", "worst_max_trade_drawdown", "severe_path_rate"]
    ].rename(
        columns={
            "mean_trade_return": "baseline_mean_trade_return",
            "trade_win_rate": "baseline_trade_win_rate",
            "worst_min_trade_nav_loss": "baseline_worst_min_trade_nav_loss",
            "worst_max_trade_drawdown": "baseline_worst_max_trade_drawdown",
            "severe_path_rate": "baseline_severe_path_rate",
        }
    )
    output = output.merge(baseline, on=["rule_id", "horizon"], how="left")
    for col in ["mean_trade_return", "trade_win_rate", "worst_min_trade_nav_loss", "worst_max_trade_drawdown", "severe_path_rate"]:
        base_col = f"baseline_{col}"
        output[f"delta_{col}_vs_baseline"] = output[col] - output[base_col]
    return output


def build_top_candidates(comparison: pd.DataFrame) -> pd.DataFrame:
    if comparison.empty:
        return pd.DataFrame()
    priority = {"小样本路径控制候选": 0, "路径控制观察": 1, "样本不足": 2, "拒绝": 3}
    output = comparison.copy()
    output["_priority"] = output["path_control_status"].map(priority).fillna(9)
    output = output.sort_values(["_priority", "path_control_score"], ascending=[True, False]).drop(columns=["_priority"])
    columns = [
        "rule_id",
        "horizon",
        "scenario_id",
        "scenario_name_zh",
        "path_control_status",
        "source_event_count",
        "entered_event_count",
        "skipped_event_count",
        "skip_rate",
        "mean_trade_return",
        "worst_trade_return",
        "trade_win_rate",
        "worst_min_trade_nav_loss",
        "worst_max_trade_drawdown",
        "severe_path_rate",
        "stop_trigger_rate",
        "delta_mean_trade_return_vs_baseline",
        "delta_worst_min_trade_nav_loss_vs_baseline",
        "delta_severe_path_rate_vs_baseline",
        "path_control_score",
    ]
    return output[[col for col in columns if col in output.columns]]


def build_leakage_audit(policy: dict[str, Any], event_paths: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "delay_confirmation_uses_post_signal_observation_before_entry",
                "status": "pass",
                "evidence": "delay_days observation is used only to decide whether to enter after the observation window",
                "action": "延迟确认视为信号日之后、入场之前可观察信息。",
            },
            {
                "audit_item": "future_returns_are_outcome_not_feature",
                "status": "pass",
                "evidence": "trade path after entry is used only for realized outcome and risk audit",
                "action": "入场后的收益不参与触发条件。",
            },
            {
                "audit_item": "event_path_source_available",
                "status": "pass" if not event_paths.empty else "fail",
                "evidence": f"rows={len(event_paths)}; events={event_paths[['rule_id', 'horizon', 'signal_date']].drop_duplicates().shape[0] if not event_paths.empty else 0}",
                "action": "复用V2.18逐日路径。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(
    top_candidates: pd.DataFrame,
    comparison: pd.DataFrame,
    trades: pd.DataFrame,
    skipped: pd.DataFrame,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if top_candidates.empty:
        return {
            "main_diagnosis": "V2.19没有可评估结果。",
            "next_iterations": ["检查V2.18逐日路径输入。"],
        }
    best = top_candidates.iloc[0].to_dict()
    candidates = comparison[comparison["path_control_status"] == "小样本路径控制候选"] if not comparison.empty else pd.DataFrame()
    notes: list[str] = []
    if candidates.empty:
        notes.append("V2.19没有找到同时满足样本、收益、胜率、深浮亏和严重路径比例的路径控制候选。")
        notes.append("延迟入场或撤退阈值仍不能把反弹窗口升级为可靠识别器。")
    else:
        notes.append("V2.19出现小样本路径控制候选，但仍不能升级为交易信号。")
        notes.append("候选改善的是入场后的路径风险，不等于已经证明行业反弹窗口 alpha。")

    if pd.notna(best.get("delta_worst_min_trade_nav_loss_vs_baseline", math.nan)):
        notes.append(
            "最佳场景相对立即入场的最差浮亏变化："
            f"{fmt_pct(best.get('delta_worst_min_trade_nav_loss_vs_baseline'))}。"
        )
    if pd.notna(best.get("delta_mean_trade_return_vs_baseline", math.nan)):
        notes.append(
            "最佳场景相对立即入场的平均收益变化："
            f"{fmt_pct(best.get('delta_mean_trade_return_vs_baseline'))}。"
        )

    best_skipped = skipped[
        (skipped["rule_id"].astype(str) == str(best.get("rule_id", "")))
        & (skipped["scenario_id"].astype(str) == str(best.get("scenario_id", "")))
    ].copy() if not skipped.empty else pd.DataFrame()
    best_skipped_positive = 0 if best_skipped.empty else int((pd.to_numeric(best_skipped["baseline_final_return"], errors="coerce") > 0).sum())
    if best_skipped_positive:
        notes.append(f"最佳场景跳过 {best_skipped_positive} 个事后为正的事件，存在错过反弹的机会成本。")

    return {
        "best_rule_id": best.get("rule_id", ""),
        "best_scenario_id": best.get("scenario_id", ""),
        "best_path_control_status": best.get("path_control_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_20_direction": "若V2.19仍不合格，应从事件级路径转向逐日在线状态机：观察、确认、入场、撤退、冷却期逐日回放。",
    }


def build_run_summary(
    policy: dict[str, Any],
    event_paths: pd.DataFrame,
    top_candidates: pd.DataFrame,
    comparison: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    notes: dict[str, Any],
) -> dict[str, Any]:
    candidates = comparison[comparison["path_control_status"] == "小样本路径控制候选"] if not comparison.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage_audit["status"] == "fail").sum()) if not leakage_audit.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_path_row_count": int(len(event_paths)),
        "source_event_count": int(event_paths[["rule_id", "horizon", "signal_date"]].drop_duplicates().shape[0]) if not event_paths.empty else 0,
        "scenario_count": int(len(policy["scenarios"])),
        "path_control_candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_scenario_id": best.get("scenario_id", ""),
        "best_path_control_status": best.get("path_control_status", ""),
        "best_entered_event_count": int(best.get("entered_event_count", 0)) if pd.notna(best.get("entered_event_count", math.nan)) else 0,
        "best_mean_trade_return": float_or_none(best.get("mean_trade_return")),
        "best_trade_win_rate": float_or_none(best.get("trade_win_rate")),
        "best_worst_min_trade_nav_loss": float_or_none(best.get("worst_min_trade_nav_loss")),
        "best_severe_path_rate": float_or_none(best.get("severe_path_rate")),
        "best_delta_worst_min_trade_nav_loss_vs_baseline": float_or_none(best.get("delta_worst_min_trade_nav_loss_vs_baseline")),
        "best_delta_mean_trade_return_vs_baseline": float_or_none(best.get("delta_mean_trade_return_vs_baseline")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    comparison: pd.DataFrame,
    trades: pd.DataFrame,
    skipped: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines = ["# V2.19 延迟入场与撤退阈值审计报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines.append("V2.19 不再继续优化最终收益，而是审计延迟确认和撤退阈值能否降低 V2.18 暴露的深浮亏。")
    lines += [
        "",
        f"- 源路径行数：{summary['source_path_row_count']}",
        f"- 源事件数：{summary['source_event_count']}",
        f"- 场景数量：{summary['scenario_count']}",
        f"- 路径控制候选数：{summary['path_control_candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 场景排序",
        "",
    ]
    lines.extend(
        table_or_empty(
            top_candidates,
            {
                "rule_id": "规则ID",
                "horizon": "持有期",
                "scenario_id": "场景",
                "scenario_name_zh": "场景说明",
                "path_control_status": "状态",
                "entered_event_count": "入场事件",
                "skipped_event_count": "跳过事件",
                "skip_rate": "跳过比例",
                "mean_trade_return": "平均交易收益",
                "worst_trade_return": "最差交易收益",
                "trade_win_rate": "交易胜率",
                "worst_min_trade_nav_loss": "最差入场后浮亏",
                "worst_max_trade_drawdown": "最差交易回撤",
                "severe_path_rate": "严重路径比例",
                "stop_trigger_rate": "撤退触发比例",
                "delta_mean_trade_return_vs_baseline": "平均收益较立即入场变化",
                "delta_worst_min_trade_nav_loss_vs_baseline": "最差浮亏较立即入场变化",
                "delta_severe_path_rate_vs_baseline": "严重路径较立即入场变化",
            },
            {
                "skip_rate",
                "mean_trade_return",
                "worst_trade_return",
                "trade_win_rate",
                "worst_min_trade_nav_loss",
                "worst_max_trade_drawdown",
                "severe_path_rate",
                "stop_trigger_rate",
                "delta_mean_trade_return_vs_baseline",
                "delta_worst_min_trade_nav_loss_vs_baseline",
                "delta_severe_path_rate_vs_baseline",
            },
        )
    )

    best_rule = str(summary.get("best_rule_id", ""))
    best_scenario = str(summary.get("best_scenario_id", ""))
    best_trades = trades[(trades["rule_id"].astype(str) == best_rule) & (trades["scenario_id"].astype(str) == best_scenario)].copy() if not trades.empty else pd.DataFrame()
    lines += ["", "## 最佳场景事件明细", ""]
    lines.extend(
        table_or_empty(
            best_trades,
            {
                "signal_date": "信号日",
                "entry_date": "入场日",
                "exit_date": "退出日",
                "exit_reason": "退出原因",
                "observation_return": "观察期收益",
                "baseline_final_return": "立即入场最终收益",
                "trade_final_return": "场景交易收益",
                "opportunity_delta_vs_immediate": "相对立即入场差异",
                "min_trade_nav_loss": "入场后最大浮亏",
                "max_trade_drawdown": "交易最大回撤",
                "is_severe_path": "严重路径",
            },
            {
                "observation_return",
                "baseline_final_return",
                "trade_final_return",
                "opportunity_delta_vs_immediate",
                "min_trade_nav_loss",
                "max_trade_drawdown",
            },
        )
    )

    best_skipped = skipped[(skipped["rule_id"].astype(str) == best_rule) & (skipped["scenario_id"].astype(str) == best_scenario)].copy() if not skipped.empty else pd.DataFrame()
    lines += ["", "## 最佳场景跳过事件", ""]
    lines.extend(
        table_or_empty(
            best_skipped,
            {
                "signal_date": "信号日",
                "skip_reason": "跳过原因",
                "observation_return": "观察期收益",
                "observation_min_return": "观察期最大浮亏",
                "baseline_final_return": "若立即入场最终收益",
            },
            {"observation_return", "observation_min_return", "baseline_final_return"},
        )
    )

    lines += ["", "## 下一轮优化方向", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议 V2.20 方向：{notes.get('recommended_v2_20_direction', '')}")

    lines += ["", "## 审计", ""]
    lines.extend(table_or_empty(leakage_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += [
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文延迟入场与撤退阈值审计报告，优先打开。",
        "- `top_candidates.csv`：路径控制场景排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：场景汇总、交易明细、跳过事件、路径控制对比、审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；延迟入场和撤退阈值未能通过路径控制升级"
    return "research_only；存在小样本路径控制候选，但仍需新增实时样本验证"


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
    return str(value)


if __name__ == "__main__":
    main()
