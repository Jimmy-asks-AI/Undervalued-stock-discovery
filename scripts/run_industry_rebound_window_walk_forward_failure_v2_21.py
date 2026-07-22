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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_walk_forward_failure_policy_v2_21.json"
V20_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_online_state_machine_v2_20.py"
VERSION = "2.21.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.21 walk-forward and failure-attribution audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.21 policy JSON.")
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
    state_summary, trades, transitions, baseline_summary = v20.run_state_machine_audit(features, source_policy)
    baseline_trades = rebuild_baseline_trades(v20, features, source_policy)
    enriched_trades = enrich_trades(pd.concat([trades, baseline_trades], ignore_index=True, sort=False), features, policy)
    annual_summary = build_annual_summary(enriched_trades)
    walk_forward_summary = build_walk_forward_summary(annual_summary, policy)
    failure_cases = build_failure_cases(enriched_trades, policy)
    failure_attribution = build_failure_attribution(failure_cases)
    top_candidates = build_top_candidates(walk_forward_summary)
    leakage_audit = build_leakage_audit(policy, source_policy, features, enriched_trades)
    notes = build_optimization_notes(top_candidates, failure_attribution, failure_cases, policy)
    run_summary = build_run_summary(policy, features, walk_forward_summary, top_candidates, leakage_audit, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    annual_summary.to_csv(debug_dir / "annual_walk_forward_summary.csv", index=False, encoding="utf-8-sig")
    walk_forward_summary.to_csv(debug_dir / "walk_forward_summary.csv", index=False, encoding="utf-8-sig")
    enriched_trades.to_csv(debug_dir / "walk_forward_trades.csv", index=False, encoding="utf-8-sig")
    failure_cases.to_csv(debug_dir / "failure_cases.csv", index=False, encoding="utf-8-sig")
    failure_attribution.to_csv(debug_dir / "failure_attribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", {"v2_21_policy": policy, "source_v2_20_policy": source_policy})
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(run_summary, top_candidates, annual_summary, failure_attribution, failure_cases, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V2.21年度Walk-forward与失败归因审计完成")
    print(f"日频特征行数={run_summary['daily_feature_count']}")
    print(f"状态机策略数={run_summary['strategy_count']}")
    print(f"稳定候选数={run_summary['walk_forward_candidate_count']}")
    print(f"最佳策略={run_summary['best_strategy_id']}")
    print(f"审计失败数={run_summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v20_module() -> Any:
    spec = importlib.util.spec_from_file_location("v20_state_machine", V20_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V2.20 state-machine module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rebuild_baseline_trades(v20: Any, features: pd.DataFrame, source_policy: dict[str, Any]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for strategy in source_policy["strategies"]:
        frame = v20.run_baseline(features, strategy, source_policy)
        if not frame.empty:
            frames.append(frame)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def enrich_trades(trades: pd.DataFrame, features: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if trades.empty:
        return trades
    output = trades.copy()
    features_by_index = features.reset_index(drop=True)
    feature_cols = [
        "market_stress_score",
        "negative_breadth_60d",
        "market_return_5d",
        "market_return_10d",
        "market_return_20d",
        "market_drawdown_252d",
        "breadth_repair_5d",
        "stress_release_5d",
        "return_pressure",
        "drawdown_pressure",
        "volatility_pressure",
        "breadth_pressure",
    ]
    for prefix, index_col in [("entry", "entry_index"), ("return_start", "start_return_index")]:
        for col in feature_cols:
            output[f"{prefix}_{col}"] = output[index_col].map(lambda idx, c=col: feature_value(features_by_index, idx, c))
    output["entry_year"] = pd.to_datetime(output["entry_date"], errors="coerce").dt.year
    output["is_state_trade"] = output["trade_type"].astype(str) == "state_machine"
    output["is_baseline_trade"] = output["trade_type"].astype(str) == "baseline_immediate"
    post_rows: list[dict[str, Any]] = []
    for _, trade in output.iterrows():
        post_rows.append(post_entry_snapshot(features_by_index, trade, policy))
    post = pd.DataFrame(post_rows)
    output = pd.concat([output.reset_index(drop=True), post.reset_index(drop=True)], axis=1)
    output["failure_tags"] = output.apply(lambda row: ";".join(failure_tags(row, policy)), axis=1)
    return output


def feature_value(features: pd.DataFrame, idx: Any, col: str) -> float:
    try:
        pos = int(idx)
    except (TypeError, ValueError):
        return math.nan
    if pos < 0 or pos >= len(features) or col not in features.columns:
        return math.nan
    return float_or_nan(features.iloc[pos].get(col))


def post_entry_snapshot(features: pd.DataFrame, trade: pd.Series, policy: dict[str, Any]) -> dict[str, Any]:
    try:
        start = int(trade["start_return_index"])
        end = min(start + 4, len(features) - 1)
    except (TypeError, ValueError):
        return {}
    if start >= len(features):
        return {}
    path = features.iloc[start : end + 1].copy()
    daily = pd.to_numeric(path["market_daily_return"], errors="coerce").fillna(0.0)
    post5_return = float((1.0 + daily).prod() - 1.0) if not daily.empty else math.nan
    return {
        "post5_return": post5_return,
        "post5_min_daily_return": float(daily.min()) if not daily.empty else math.nan,
        "post5_max_stress": float(pd.to_numeric(path["market_stress_score"], errors="coerce").max()) if "market_stress_score" in path else math.nan,
        "post5_max_negative_breadth": float(pd.to_numeric(path["negative_breadth_60d"], errors="coerce").max()) if "negative_breadth_60d" in path else math.nan,
        "post5_min_breadth_repair": float(pd.to_numeric(path["breadth_repair_5d"], errors="coerce").min()) if "breadth_repair_5d" in path else math.nan,
        "post5_min_stress_release": float(pd.to_numeric(path["stress_release_5d"], errors="coerce").min()) if "stress_release_5d" in path else math.nan,
    }


def failure_tags(row: pd.Series, policy: dict[str, Any]) -> list[str]:
    cfg = policy["failure_attribution"]
    tags: list[str] = []
    if bool(row.get("is_bad_window", False)):
        tags.append("坏窗口")
    if bool(row.get("is_severe_path", False)):
        tags.append("严重路径")
    if float_or_nan(row.get("post5_return")) <= float(cfg["large_5d_loss_after_entry"]):
        tags.append("入场后5日急跌")
    if float_or_nan(row.get("post5_max_stress")) >= float(cfg["high_stress_after_entry"]):
        tags.append("入场后压力再升")
    if float_or_nan(row.get("post5_max_negative_breadth")) >= float(cfg["high_negative_breadth_after_entry"]):
        tags.append("入场后普跌未解")
    if float_or_nan(row.get("entry_breadth_repair_5d")) < float(cfg["weak_repair_threshold"]):
        tags.append("确认时广度修复不足")
    if float_or_nan(row.get("entry_stress_release_5d")) < float(cfg["weak_repair_threshold"]):
        tags.append("确认时压力释放不足")
    return tags


def build_annual_summary(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for (strategy_id, strategy_name, horizon, trade_type, year), frame in trades.groupby(
        ["strategy_id", "strategy_name_zh", "horizon", "trade_type", "entry_year"], dropna=True
    ):
        returns = pd.to_numeric(frame["trade_return"], errors="coerce")
        rows.append(
            {
                "strategy_id": strategy_id,
                "strategy_name_zh": strategy_name,
                "horizon": int(horizon),
                "trade_type": trade_type,
                "entry_year": int(year),
                "trade_count": int(len(frame)),
                "mean_return": float(returns.mean()),
                "median_return": float(returns.median()),
                "worst_return": float(returns.min()),
                "win_rate": float(frame["is_win"].mean()),
                "bad_window_rate": float(frame["is_bad_window"].mean()),
                "severe_path_rate": float(frame["is_severe_path"].mean()),
                "worst_min_nav_loss": float(pd.to_numeric(frame["min_nav_loss"], errors="coerce").min()),
                "compound_nav": float((1.0 + returns).prod()),
            }
        )
    return pd.DataFrame(rows)


def build_walk_forward_summary(annual: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if annual.empty:
        return pd.DataFrame()
    state = annual[annual["trade_type"] == "state_machine"].copy()
    rows: list[dict[str, Any]] = []
    for (strategy_id, strategy_name, horizon), frame in state.groupby(["strategy_id", "strategy_name_zh", "horizon"], dropna=True):
        total_trades = int(frame["trade_count"].sum())
        active_years = int(len(frame))
        positive_years = int((pd.to_numeric(frame["mean_return"], errors="coerce") > 0).sum())
        bad_years = int((pd.to_numeric(frame["bad_window_rate"], errors="coerce") > 0).sum())
        max_concentration = float(frame["trade_count"].max() / total_trades) if total_trades else math.nan
        row = {
            "strategy_id": strategy_id,
            "strategy_name_zh": strategy_name,
            "horizon": int(horizon),
            "total_trades": total_trades,
            "active_years": active_years,
            "positive_years": positive_years,
            "positive_year_rate": float(positive_years / active_years) if active_years else math.nan,
            "bad_years": bad_years,
            "bad_year_rate": float(bad_years / active_years) if active_years else math.nan,
            "max_single_year_trade_concentration": max_concentration,
            "mean_year_return": float(pd.to_numeric(frame["mean_return"], errors="coerce").mean()),
            "worst_year_mean_return": float(pd.to_numeric(frame["mean_return"], errors="coerce").min()),
            "worst_year_bad_window_rate": float(pd.to_numeric(frame["bad_window_rate"], errors="coerce").max()),
            "worst_year_min_nav_loss": float(pd.to_numeric(frame["worst_min_nav_loss"], errors="coerce").min()),
            "annual_compound_nav": float(pd.to_numeric(frame["compound_nav"], errors="coerce").prod()),
        }
        row["walk_forward_score"] = score_walk_forward(row)
        row["walk_forward_status"] = classify_walk_forward(row, policy)
        rows.append(row)
    return pd.DataFrame(rows)


def score_walk_forward(row: dict[str, Any]) -> float:
    return float(
        2.0 * nz(row.get("mean_year_return"))
        + 1.2 * (nz(row.get("positive_year_rate")) - 0.5)
        - 1.4 * nz(row.get("bad_year_rate"))
        - 0.8 * nz(row.get("max_single_year_trade_concentration"))
        + 1.0 * nz(row.get("worst_year_mean_return"))
        + 0.6 * nz(row.get("worst_year_min_nav_loss"))
    )


def classify_walk_forward(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["walk_forward"]
    checks = {
        "trades": nz(row.get("total_trades")) >= int(th["min_total_trades"]),
        "years": nz(row.get("active_years")) >= int(th["min_active_years"]),
        "positive": nz(row.get("positive_year_rate")) >= float(th["min_positive_year_rate"]),
        "bad": nz(row.get("bad_year_rate"), 1.0) <= float(th["max_bad_year_rate"]),
        "concentration": nz(row.get("max_single_year_trade_concentration"), 1.0) <= float(th["max_single_year_trade_concentration"]),
        "worst_return": nz(row.get("worst_year_mean_return"), -1.0) >= float(th["min_worst_year_mean_return"]),
        "worst_bad": nz(row.get("worst_year_bad_window_rate"), 1.0) <= float(th["max_worst_year_bad_window_rate"]),
    }
    if all(checks.values()):
        return "年度稳定候选"
    if not checks["trades"] or not checks["years"]:
        return "样本不足"
    if checks["positive"] and checks["worst_return"]:
        return "年度观察"
    return "拒绝"


def build_failure_cases(trades: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    state = trades[trades["trade_type"] == "state_machine"].copy()
    if state.empty:
        return pd.DataFrame()
    state["is_failure_case"] = state["is_bad_window"].astype(bool) | state["is_severe_path"].astype(bool) | state["failure_tags"].astype(str).str.len().gt(0)
    columns = [
        "strategy_id",
        "strategy_name_zh",
        "horizon",
        "entry_date",
        "return_start_date",
        "exit_date",
        "exit_reason",
        "trade_return",
        "min_nav_loss",
        "max_drawdown",
        "is_bad_window",
        "is_severe_path",
        "entry_market_stress_score",
        "entry_negative_breadth_60d",
        "entry_market_return_5d",
        "entry_breadth_repair_5d",
        "entry_stress_release_5d",
        "post5_return",
        "post5_max_stress",
        "post5_max_negative_breadth",
        "failure_tags",
    ]
    return state[state["is_failure_case"]].sort_values(["strategy_id", "entry_date"])[[col for col in columns if col in state.columns]]


def build_failure_attribution(failure_cases: pd.DataFrame) -> pd.DataFrame:
    if failure_cases.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for strategy_id, frame in failure_cases.groupby("strategy_id", dropna=True):
        tag_counts: dict[str, int] = {}
        for tags in frame["failure_tags"].astype(str):
            for tag in [item for item in tags.split(";") if item]:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0])):
            tagged = frame[frame["failure_tags"].astype(str).str.split(";").map(lambda tags: tag in tags)].copy()
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "failure_tag": tag,
                    "case_count": int(count),
                    "case_rate_in_failures": float(count / len(frame)) if len(frame) else math.nan,
                    "mean_failure_return": float(pd.to_numeric(tagged["trade_return"], errors="coerce").mean()),
                    "worst_failure_return": float(pd.to_numeric(tagged["trade_return"], errors="coerce").min()),
                    "mean_entry_stress": float(pd.to_numeric(tagged["entry_market_stress_score"], errors="coerce").mean()),
                    "mean_entry_breadth": float(pd.to_numeric(tagged["entry_negative_breadth_60d"], errors="coerce").mean()),
                    "mean_post5_return": float(pd.to_numeric(tagged["post5_return"], errors="coerce").mean()),
                }
            )
    return pd.DataFrame(rows)


def build_top_candidates(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    priority = {"年度稳定候选": 0, "年度观察": 1, "样本不足": 2, "拒绝": 3}
    output = summary.copy()
    output["_priority"] = output["walk_forward_status"].map(priority).fillna(9)
    columns = [
        "strategy_id",
        "strategy_name_zh",
        "horizon",
        "walk_forward_status",
        "total_trades",
        "active_years",
        "positive_year_rate",
        "bad_year_rate",
        "max_single_year_trade_concentration",
        "mean_year_return",
        "worst_year_mean_return",
        "worst_year_bad_window_rate",
        "worst_year_min_nav_loss",
        "annual_compound_nav",
        "walk_forward_score",
    ]
    return output.sort_values(["_priority", "walk_forward_score"], ascending=[True, False]).drop(columns=["_priority"])[[col for col in columns if col in output.columns]]


def build_leakage_audit(policy: dict[str, Any], source_policy: dict[str, Any], features: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "frozen_v2_20_state_machine",
                "status": "pass",
                "evidence": f"source_policy={source_policy['policy_id']}; strategies={len(source_policy['strategies'])}",
                "action": "V2.21不新增触发条件，只审计稳定性和失败归因。",
            },
            {
                "audit_item": "walk_forward_uses_entry_year_only",
                "status": "pass",
                "evidence": "annual splits are grouped by realized entry_date year after frozen replay",
                "action": "年度切分不参与参数搜索。",
            },
            {
                "audit_item": "features_available",
                "status": "pass" if not features.empty and not trades.empty else "fail",
                "evidence": f"features={len(features)}; trades={len(trades)}",
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


def build_optimization_notes(top_candidates: pd.DataFrame, attribution: pd.DataFrame, failures: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top_candidates.empty:
        return {"main_diagnosis": "V2.21没有可排序策略。", "next_iterations": ["检查V2.20状态机输出。"]}
    best = top_candidates.iloc[0].to_dict()
    candidates = top_candidates[top_candidates["walk_forward_status"] == "年度稳定候选"]
    notes: list[str] = []
    if candidates.empty:
        notes.append("V2.21没有发现年度稳定候选；V2.20状态机的结果仍主要受少数年份驱动。")
    else:
        notes.append("V2.21出现年度稳定候选，但仍必须保持research_only并等待未来样本。")
    notes.append(
        f"最佳策略 {best.get('strategy_id', '')} 活跃年份 {best.get('active_years', 0)}，"
        f"总交易 {best.get('total_trades', 0)}，坏年份比例 {fmt_pct(best.get('bad_year_rate'))}，"
        f"单一年份交易集中度 {fmt_pct(best.get('max_single_year_trade_concentration'))}。"
    )
    if not attribution.empty:
        best_attr = attribution[attribution["strategy_id"].astype(str) == str(best.get("strategy_id", ""))]
        if not best_attr.empty:
            top_tag = best_attr.iloc[0].to_dict()
            notes.append(f"最佳策略最常见失败标签是“{top_tag.get('failure_tag', '')}”，出现 {top_tag.get('case_count', 0)} 次。")
    if not failures.empty:
        notes.append("失败样本显示，状态机确认后仍可能出现压力再升或普跌未解，下一轮应先验证“确认后不再恶化”的撤销机制。")
    return {
        "best_strategy_id": best.get("strategy_id", ""),
        "best_status": best.get("walk_forward_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_22_direction": "冻结当前状态机，增加确认后撤销条件审计：确认后若5日急跌、压力再升或下跌广度重新扩大，则取消入场而不是入场后止损。",
    }


def build_run_summary(policy: dict[str, Any], features: pd.DataFrame, summary: pd.DataFrame, top: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = summary[summary["walk_forward_status"] == "年度稳定候选"] if not summary.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    audit_fail_count = int((leakage["status"] == "fail").sum()) if not leakage.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "daily_feature_count": int(len(features)),
        "strategy_count": int(len(summary)) if not summary.empty else 0,
        "walk_forward_candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_strategy_id": best.get("strategy_id", ""),
        "best_walk_forward_status": best.get("walk_forward_status", ""),
        "best_total_trades": int(best.get("total_trades", 0)) if pd.notna(best.get("total_trades", math.nan)) else 0,
        "best_active_years": int(best.get("active_years", 0)) if pd.notna(best.get("active_years", math.nan)) else 0,
        "best_positive_year_rate": float_or_none(best.get("positive_year_rate")),
        "best_bad_year_rate": float_or_none(best.get("bad_year_rate")),
        "best_max_single_year_trade_concentration": float_or_none(best.get("max_single_year_trade_concentration")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(summary: dict[str, Any], top: pd.DataFrame, annual: pd.DataFrame, attribution: pd.DataFrame, failures: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V2.21 年度Walk-forward与失败归因审计报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines.append("V2.21 冻结 V2.20 逐日在线状态机，只审计年度稳定性、年份集中风险和失败样本共同特征。")
    lines += [
        "",
        f"- 日频特征行数：{summary['daily_feature_count']}",
        f"- 策略数：{summary['strategy_count']}",
        f"- 年度稳定候选数：{summary['walk_forward_candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 年度稳定性排序",
        "",
    ]
    lines.extend(table_or_empty(top, {
        "strategy_id": "策略ID",
        "strategy_name_zh": "策略",
        "horizon": "持有期",
        "walk_forward_status": "状态",
        "total_trades": "总交易",
        "active_years": "活跃年份",
        "positive_year_rate": "正收益年份比例",
        "bad_year_rate": "坏年份比例",
        "max_single_year_trade_concentration": "单年交易集中度",
        "mean_year_return": "年度平均收益",
        "worst_year_mean_return": "最差年度均值",
        "worst_year_bad_window_rate": "最差年度坏窗口",
        "worst_year_min_nav_loss": "最差年度浮亏",
        "annual_compound_nav": "年度复合净值",
    }, {
        "positive_year_rate",
        "bad_year_rate",
        "max_single_year_trade_concentration",
        "mean_year_return",
        "worst_year_mean_return",
        "worst_year_bad_window_rate",
        "worst_year_min_nav_loss",
    }))
    best_strategy = str(summary.get("best_strategy_id", ""))
    best_annual = annual[(annual["strategy_id"].astype(str) == best_strategy) & (annual["trade_type"].astype(str) == "state_machine")].copy() if not annual.empty else pd.DataFrame()
    lines += ["", "## 最佳策略年度表现", ""]
    lines.extend(table_or_empty(best_annual, {
        "entry_year": "年份",
        "trade_count": "交易数",
        "mean_return": "平均收益",
        "worst_return": "最差收益",
        "win_rate": "胜率",
        "bad_window_rate": "坏窗口比例",
        "severe_path_rate": "严重路径比例",
        "worst_min_nav_loss": "最差浮亏",
        "compound_nav": "年度复合净值",
    }, {
        "mean_return",
        "worst_return",
        "win_rate",
        "bad_window_rate",
        "severe_path_rate",
        "worst_min_nav_loss",
    }))
    best_attr = attribution[attribution["strategy_id"].astype(str) == best_strategy].copy() if not attribution.empty else pd.DataFrame()
    lines += ["", "## 失败归因", ""]
    lines.extend(table_or_empty(best_attr, {
        "failure_tag": "失败标签",
        "case_count": "次数",
        "case_rate_in_failures": "失败样本占比",
        "mean_failure_return": "失败平均收益",
        "worst_failure_return": "失败最差收益",
        "mean_entry_stress": "平均入场压力",
        "mean_entry_breadth": "平均入场下跌广度",
        "mean_post5_return": "入场后5日平均收益",
    }, {
        "case_rate_in_failures",
        "mean_failure_return",
        "worst_failure_return",
        "mean_entry_stress",
        "mean_entry_breadth",
        "mean_post5_return",
    }))
    best_failures = failures[failures["strategy_id"].astype(str) == best_strategy].copy() if not failures.empty else pd.DataFrame()
    lines += ["", "## 最佳策略失败样本", ""]
    lines.extend(table_or_empty(best_failures, {
        "entry_date": "入场日",
        "exit_date": "退出日",
        "exit_reason": "退出原因",
        "trade_return": "交易收益",
        "min_nav_loss": "最大浮亏",
        "max_drawdown": "路径回撤",
        "entry_market_stress_score": "入场压力",
        "entry_negative_breadth_60d": "入场下跌广度",
        "entry_breadth_repair_5d": "入场广度修复",
        "entry_stress_release_5d": "入场压力释放",
        "post5_return": "入场后5日收益",
        "failure_tags": "失败标签",
    }, {
        "trade_return",
        "min_nav_loss",
        "max_drawdown",
        "entry_market_stress_score",
        "entry_negative_breadth_60d",
        "entry_breadth_repair_5d",
        "entry_stress_release_5d",
        "post5_return",
    }))
    lines += ["", "## 下一轮优化方向", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议 V2.22 方向：{notes.get('recommended_v2_22_direction', '')}")
    lines += ["", "## 审计", ""]
    lines.extend(table_or_empty(leakage, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += [
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文年度稳定性与失败归因报告，优先打开。",
        "- `top_candidates.csv`：年度稳定性排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：年度表现、交易明细、失败样本、归因、审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；冻结状态机未通过年度稳定性候选门槛"
    return "research_only；存在年度稳定候选，但仍需未来新增样本验证"


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
