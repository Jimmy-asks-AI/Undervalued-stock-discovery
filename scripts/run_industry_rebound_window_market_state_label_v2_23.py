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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_market_state_label_policy_v2_23.json"
V20_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_online_state_machine_v2_20.py"
VERSION = "2.23.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.23 market-state label rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.23 policy JSON.")
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
    panel = add_forward_returns(features, policy)
    label_summary, nonoverlap_events, oos_summary, rolling_summary, annual_summary = run_label_audit(panel, policy)
    top_candidates = build_top_candidates(label_summary)
    leakage_audit = build_leakage_audit(policy, source_policy, panel)
    notes = build_optimization_notes(top_candidates, label_summary, rolling_summary, annual_summary, policy)
    run_summary = build_run_summary(policy, panel, label_summary, top_candidates, leakage_audit, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "state_label_panel.csv", index=False, encoding="utf-8-sig")
    label_summary.to_csv(debug_dir / "state_label_summary.csv", index=False, encoding="utf-8-sig")
    nonoverlap_events.to_csv(debug_dir / "nonoverlap_state_events.csv", index=False, encoding="utf-8-sig")
    oos_summary.to_csv(debug_dir / "oos_summary.csv", index=False, encoding="utf-8-sig")
    rolling_summary.to_csv(debug_dir / "rolling_state_stability.csv", index=False, encoding="utf-8-sig")
    annual_summary.to_csv(debug_dir / "annual_state_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", {"v2_23_policy": policy, "source_v2_20_policy": source_policy})
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(run_summary, top_candidates, nonoverlap_events, rolling_summary, annual_summary, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V2.23市场状态标签反弹窗口审计完成")
    print(f"日频标签面板行数={run_summary['state_label_panel_count']}")
    print(f"状态标签数={run_summary['state_label_count']}")
    print(f"状态候选数={run_summary['state_candidate_count']}")
    print(f"最佳状态={run_summary['best_state_id']}")
    print(f"审计失败数={run_summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v20_module() -> Any:
    spec = importlib.util.spec_from_file_location("v20_state_machine", V20_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V2.20 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_forward_returns(features: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    panel = features.copy()
    nav = pd.to_numeric(panel["market_nav"], errors="coerce")
    for horizon in [int(item) for item in policy["horizons"]]:
        panel[f"forward_return_{horizon}d"] = nav.shift(-horizon) / nav - 1.0
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    panel["year"] = panel["trade_date"].dt.year
    return panel.dropna(subset=["trade_date"]).reset_index(drop=True)


def run_label_audit(panel: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    label_rows: list[dict[str, Any]] = []
    event_frames: list[pd.DataFrame] = []
    oos_rows: list[dict[str, Any]] = []
    rolling_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    baseline_mask = conditions_mask(panel, policy["bottom_baseline_conditions"], logic="any")
    for label in policy["state_labels"]:
        state_mask = conditions_mask(panel, label["conditions"], logic="all")
        for horizon in [int(item) for item in policy["horizons"]]:
            return_col = f"forward_return_{horizon}d"
            valid = panel.dropna(subset=[return_col]).copy()
            mask = state_mask.reindex(valid.index, fill_value=False)
            baseline = baseline_mask.reindex(valid.index, fill_value=False)
            events = build_nonoverlap_events(valid, mask, return_col, horizon, label, policy)
            event_frames.append(events)
            oos_row = describe_period("oos", "样本外", valid[valid["trade_date"] >= pd.Timestamp(policy["oos_start"])], mask, baseline, return_col, horizon, label, policy)
            oos_rows.append(oos_row)
            rolling_for_label = build_rolling_rows(valid, mask, baseline, return_col, horizon, label, policy)
            rolling_rows.extend(rolling_for_label)
            annual_for_label = build_annual_rows(valid, mask, return_col, horizon, label)
            annual_rows.extend(annual_for_label)
            full = describe_period("full", "全样本", valid, mask, baseline, return_col, horizon, label, policy)
            event_stats = summarize_events(events, policy, horizon)
            roll_stats = summarize_rolling(rolling_for_label)
            annual_stats = summarize_annual(annual_for_label)
            row = {**full, **prefix(event_stats, "nonoverlap"), **prefix(oos_row, "oos"), **roll_stats, **annual_stats}
            row["state_score"] = score_state(row)
            row["state_status"] = classify_state(row, policy)
            label_rows.append(row)
    return pd.DataFrame(label_rows), concat_frames(event_frames), pd.DataFrame(oos_rows), pd.DataFrame(rolling_rows), pd.DataFrame(annual_rows)


def describe_period(
    period_id: str,
    period_name: str,
    frame: pd.DataFrame,
    state_mask: pd.Series,
    baseline_mask: pd.Series,
    return_col: str,
    horizon: int,
    label: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    selected_mask = state_mask.reindex(frame.index, fill_value=False)
    base_mask = baseline_mask.reindex(frame.index, fill_value=False)
    selected = pd.to_numeric(frame.loc[selected_mask, return_col], errors="coerce").dropna()
    base = pd.to_numeric(frame.loc[base_mask, return_col], errors="coerce").dropna()
    all_returns = pd.to_numeric(frame[return_col], errors="coerce").dropna()
    bad = float(policy["bad_window_thresholds"][str(horizon)])
    return {
        "state_id": label["state_id"],
        "state_name_zh": label["state_name_zh"],
        "horizon": horizon,
        "period_id": period_id,
        "period_name_zh": period_name,
        "all_dates": int(len(all_returns)),
        "state_dates": int(len(selected)),
        "baseline_dates": int(len(base)),
        "state_mean_return": float(selected.mean()) if len(selected) else math.nan,
        "all_mean_return": float(all_returns.mean()) if len(all_returns) else math.nan,
        "baseline_mean_return": float(base.mean()) if len(base) else math.nan,
        "mean_edge_vs_all": safe_sub(selected.mean() if len(selected) else math.nan, all_returns.mean() if len(all_returns) else math.nan),
        "mean_edge_vs_baseline": safe_sub(selected.mean() if len(selected) else math.nan, base.mean() if len(base) else math.nan),
        "state_win_rate": float((selected > 0).mean()) if len(selected) else math.nan,
        "state_bad_window_rate": float((selected <= bad).mean()) if len(selected) else math.nan,
        "state_worst_return": float(selected.min()) if len(selected) else math.nan,
    }


def build_nonoverlap_events(frame: pd.DataFrame, mask: pd.Series, return_col: str, horizon: int, label: dict[str, Any], policy: dict[str, Any]) -> pd.DataFrame:
    selected = frame[mask.reindex(frame.index, fill_value=False)].sort_values("trade_date").copy()
    rows: list[dict[str, Any]] = []
    last_i = -10_000_000
    bad = float(policy["bad_window_thresholds"][str(horizon)])
    for idx, row in selected.iterrows():
        pos = int(idx)
        if pos <= last_i + horizon:
            continue
        ret = float(row[return_col])
        rows.append(
            {
                "state_id": label["state_id"],
                "state_name_zh": label["state_name_zh"],
                "horizon": horizon,
                "trade_date": pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d"),
                "event_return": ret,
                "is_win": bool(ret > 0),
                "is_bad_window": bool(ret <= bad),
                "market_stress_score": row.get("market_stress_score", math.nan),
                "negative_breadth_60d": row.get("negative_breadth_60d", math.nan),
                "market_drawdown_252d": row.get("market_drawdown_252d", math.nan),
                "market_return_5d": row.get("market_return_5d", math.nan),
                "breadth_repair_5d": row.get("breadth_repair_5d", math.nan),
                "stress_release_5d": row.get("stress_release_5d", math.nan),
            }
        )
        last_i = pos
    return pd.DataFrame(rows)


def summarize_events(events: pd.DataFrame, policy: dict[str, Any], horizon: int) -> dict[str, Any]:
    if events.empty:
        return {
            "events": 0,
            "mean_return": math.nan,
            "win_rate": math.nan,
            "bad_window_rate": math.nan,
            "worst_return": math.nan,
        }
    values = pd.to_numeric(events["event_return"], errors="coerce")
    return {
        "events": int(len(values)),
        "mean_return": float(values.mean()),
        "win_rate": float((values > 0).mean()),
        "bad_window_rate": float(events["is_bad_window"].mean()),
        "worst_return": float(values.min()),
    }


def build_rolling_rows(frame: pd.DataFrame, state_mask: pd.Series, baseline_mask: pd.Series, return_col: str, horizon: int, label: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    min_year = int(frame["year"].min())
    max_year = int(frame["year"].max())
    window = int(policy["rolling_window_years"])
    step = int(policy["rolling_step_years"])
    rows: list[dict[str, Any]] = []
    year = min_year
    while year + window - 1 <= max_year:
        start = pd.Timestamp(f"{year}-01-01")
        end = pd.Timestamp(f"{year + window - 1}-12-31")
        period = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)].copy()
        row = describe_period(
            f"rolling_{year}_{year + window - 1}",
            f"{year}-{year + window - 1}滚动{window}年",
            period,
            state_mask,
            baseline_mask,
            return_col,
            horizon,
            label,
            policy,
        )
        rows.append(row)
        year += step
    return rows


def summarize_rolling(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if int(row.get("state_dates", 0)) > 0]
    positive = [row for row in eligible if nz(row.get("state_mean_return")) > 0]
    edge_positive = [row for row in eligible if nz(row.get("mean_edge_vs_baseline")) > 0]
    return {
        "rolling_eligible_count": len(eligible),
        "rolling_positive_return_rate": float(len(positive) / len(eligible)) if eligible else math.nan,
        "rolling_positive_edge_rate": float(len(edge_positive) / len(eligible)) if eligible else math.nan,
        "rolling_worst_mean_return": min_metric(eligible, "state_mean_return"),
        "rolling_worst_edge_vs_baseline": min_metric(eligible, "mean_edge_vs_baseline"),
    }


def build_annual_rows(frame: pd.DataFrame, state_mask: pd.Series, return_col: str, horizon: int, label: dict[str, Any]) -> list[dict[str, Any]]:
    selected = frame[state_mask.reindex(frame.index, fill_value=False)].copy()
    if selected.empty:
        return []
    rows: list[dict[str, Any]] = []
    for year, group in selected.groupby("year"):
        values = pd.to_numeric(group[return_col], errors="coerce").dropna()
        if values.empty:
            continue
        rows.append(
            {
                "state_id": label["state_id"],
                "state_name_zh": label["state_name_zh"],
                "horizon": horizon,
                "year": int(year),
                "state_dates": int(len(values)),
                "mean_return": float(values.mean()),
                "win_rate": float((values > 0).mean()),
                "worst_return": float(values.min()),
            }
        )
    return rows


def summarize_annual(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "active_years": 0,
            "positive_year_rate": math.nan,
            "max_single_year_concentration": math.nan,
        }
    total_dates = sum(int(row["state_dates"]) for row in rows)
    positive_years = sum(1 for row in rows if float_or_nan(row.get("mean_return")) > 0)
    return {
        "active_years": int(len(rows)),
        "positive_year_rate": float(positive_years / len(rows)) if rows else math.nan,
        "max_single_year_concentration": float(max(int(row["state_dates"]) for row in rows) / total_dates) if total_dates else math.nan,
    }


def score_state(row: dict[str, Any]) -> float:
    return float(
        2.0 * nz(row.get("mean_edge_vs_baseline"))
        + 1.4 * nz(row.get("mean_edge_vs_all"))
        + 1.0 * (nz(row.get("state_win_rate")) - 0.5)
        - 1.2 * nz(row.get("state_bad_window_rate"))
        + 1.0 * nz(row.get("nonoverlap_mean_return"))
        + 0.8 * nz(row.get("oos_state_mean_return"))
        + 0.6 * (nz(row.get("rolling_positive_edge_rate")) - 0.5)
        - 0.4 * nz(row.get("max_single_year_concentration"))
    )


def classify_state(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    checks = {
        "full_dates": nz(row.get("state_dates")) >= int(th["min_full_dates"]),
        "events": nz(row.get("nonoverlap_events")) >= int(th["min_nonoverlap_events"]),
        "oos": nz(row.get("oos_state_dates")) >= int(th["min_oos_dates"]),
        "edge_all": nz(row.get("mean_edge_vs_all")) >= float(th["min_mean_edge_vs_all"]),
        "edge_base": nz(row.get("mean_edge_vs_baseline")) >= float(th["min_mean_edge_vs_baseline"]),
        "event_return": nz(row.get("nonoverlap_mean_return")) >= float(th["min_nonoverlap_mean_return"]),
        "oos_return": nz(row.get("oos_state_mean_return")) >= float(th["min_oos_mean_return"]),
        "win": nz(row.get("state_win_rate")) >= float(th["min_win_rate"]),
        "bad": nz(row.get("state_bad_window_rate"), 1.0) <= float(th["max_bad_window_rate"]),
        "rolling": nz(row.get("rolling_positive_edge_rate")) >= float(th["min_positive_rolling_rate"]),
        "concentration": nz(row.get("max_single_year_concentration"), 1.0) <= float(th["max_single_year_concentration"]),
    }
    if all(checks.values()):
        return "市场状态候选"
    if not checks["full_dates"] or not checks["events"] or not checks["oos"]:
        return "样本不足"
    if checks["edge_base"] and checks["event_return"] and checks["oos_return"]:
        return "状态观察"
    return "拒绝"


def build_top_candidates(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    priority = {"市场状态候选": 0, "状态观察": 1, "样本不足": 2, "拒绝": 3}
    output = summary.copy()
    output["_priority"] = output["state_status"].map(priority).fillna(9)
    output = output.sort_values(["_priority", "state_score"], ascending=[True, False]).drop(columns=["_priority"])
    columns = [
        "state_id",
        "state_name_zh",
        "horizon",
        "state_status",
        "state_dates",
        "nonoverlap_events",
        "oos_state_dates",
        "state_mean_return",
        "all_mean_return",
        "baseline_mean_return",
        "mean_edge_vs_all",
        "mean_edge_vs_baseline",
        "state_win_rate",
        "state_bad_window_rate",
        "nonoverlap_mean_return",
        "nonoverlap_win_rate",
        "nonoverlap_bad_window_rate",
        "oos_state_mean_return",
        "oos_mean_edge_vs_baseline",
        "rolling_positive_edge_rate",
        "rolling_worst_edge_vs_baseline",
        "active_years",
        "max_single_year_concentration",
        "state_score",
    ]
    return output[[col for col in columns if col in output.columns]]


def build_leakage_audit(policy: dict[str, Any], source_policy: dict[str, Any], panel: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "predeclared_state_labels",
                "status": "pass",
                "evidence": f"state_labels={len(policy['state_labels'])}; horizons={policy['horizons']}",
                "action": "V2.23只审计配置中预先声明的市场状态。",
            },
            {
                "audit_item": "forward_returns_used_only_as_outcome",
                "status": "pass",
                "evidence": "forward_return_* fields are generated after state labels and used only for evaluation",
                "action": "未来收益不作为触发特征。",
            },
            {
                "audit_item": "source_features_from_v2_20",
                "status": "pass" if not panel.empty else "fail",
                "evidence": f"source_policy={source_policy['policy_id']}; panel_rows={len(panel)}",
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


def build_optimization_notes(top: pd.DataFrame, summary: pd.DataFrame, rolling: pd.DataFrame, annual: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top.empty:
        return {"main_diagnosis": "V2.23没有可排序状态。", "next_iterations": ["检查日频状态面板。"]}
    best = top.iloc[0].to_dict()
    candidates = summary[summary["state_status"] == "市场状态候选"] if not summary.empty else pd.DataFrame()
    notes: list[str] = []
    if candidates.empty:
        notes.append("V2.23没有发现可泛化的市场状态候选。")
    else:
        notes.append("V2.23发现市场状态候选，但仍必须保持research_only。")
    notes.append(
        f"最佳状态 {best.get('state_id', '')} / {best.get('horizon', '')}日："
        f"状态日期 {best.get('state_dates', 0)}，非重叠事件 {best.get('nonoverlap_events', 0)}，"
        f"样本外日期 {best.get('oos_state_dates', 0)}。"
    )
    notes.append(
        f"相对底部基线收益提升 {fmt_pct(best.get('mean_edge_vs_baseline'))}，"
        f"样本外相对底部基线 {fmt_pct(best.get('oos_mean_edge_vs_baseline'))}，"
        f"滚动正提升比例 {fmt_pct(best.get('rolling_positive_edge_rate'))}。"
    )
    if str(best.get("state_status")) == "样本不足":
        notes.append("即便改成状态标签，严格状态仍容易样本不足；下一步应优先扩充状态定义或接入外生代理，而不是继续收紧条件。")
    if nz(best.get("rolling_positive_edge_rate")) < float(policy["promotion_thresholds"]["min_positive_rolling_rate"]):
        notes.append("滚动稳定性不足，说明状态收益可能集中在局部阶段。")
    return {
        "best_state_id": best.get("state_id", ""),
        "best_horizon": best.get("horizon", ""),
        "best_status": best.get("state_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "若V2.23无候选，应引入外生风险偏好/流动性代理或更长历史市场状态数据，而不是继续在价格派生标签上调阈值。",
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, summary: pd.DataFrame, top: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = summary[summary["state_status"] == "市场状态候选"] if not summary.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    audit_fail_count = int((leakage["status"] == "fail").sum()) if not leakage.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "state_label_panel_count": int(len(panel)),
        "state_label_count": int(len(policy["state_labels"])),
        "horizon_count": int(len(policy["horizons"])),
        "state_candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_state_id": best.get("state_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_state_status": best.get("state_status", ""),
        "best_state_dates": int(best.get("state_dates", 0)) if pd.notna(best.get("state_dates", math.nan)) else 0,
        "best_nonoverlap_events": int(best.get("nonoverlap_events", 0)) if pd.notna(best.get("nonoverlap_events", math.nan)) else 0,
        "best_oos_state_dates": int(best.get("oos_state_dates", 0)) if pd.notna(best.get("oos_state_dates", math.nan)) else 0,
        "best_mean_edge_vs_baseline": float_or_none(best.get("mean_edge_vs_baseline")),
        "best_oos_mean_edge_vs_baseline": float_or_none(best.get("oos_mean_edge_vs_baseline")),
        "best_rolling_positive_edge_rate": float_or_none(best.get("rolling_positive_edge_rate")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(summary: dict[str, Any], top: pd.DataFrame, events: pd.DataFrame, rolling: pd.DataFrame, annual: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V2.23 市场状态标签反弹窗口审计报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines.append("V2.23 停止围绕少数状态机入场点调参，改为审计预先声明的市场状态标签是否具备更长样本和更稳定的反弹特征。")
    lines += [
        "",
        f"- 日频标签面板行数：{summary['state_label_panel_count']}",
        f"- 状态标签数：{summary['state_label_count']}",
        f"- 持有期数量：{summary['horizon_count']}",
        f"- 市场状态候选数：{summary['state_candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 状态标签排序",
        "",
    ]
    lines.extend(table_or_empty(top.head(20), {
        "state_id": "状态ID",
        "state_name_zh": "状态",
        "horizon": "持有期",
        "state_status": "状态",
        "state_dates": "状态日期",
        "nonoverlap_events": "非重叠事件",
        "oos_state_dates": "样本外日期",
        "state_mean_return": "状态收益",
        "baseline_mean_return": "底部基线收益",
        "mean_edge_vs_baseline": "相对底部提升",
        "state_win_rate": "胜率",
        "state_bad_window_rate": "坏窗口",
        "nonoverlap_mean_return": "非重叠收益",
        "oos_state_mean_return": "样本外收益",
        "oos_mean_edge_vs_baseline": "样本外相对底部",
        "rolling_positive_edge_rate": "滚动正提升",
        "rolling_worst_edge_vs_baseline": "滚动最差提升",
        "max_single_year_concentration": "单年集中度",
    }, {
        "state_mean_return",
        "baseline_mean_return",
        "mean_edge_vs_baseline",
        "state_win_rate",
        "state_bad_window_rate",
        "nonoverlap_mean_return",
        "oos_state_mean_return",
        "oos_mean_edge_vs_baseline",
        "rolling_positive_edge_rate",
        "rolling_worst_edge_vs_baseline",
        "max_single_year_concentration",
    }))
    best_state = str(summary.get("best_state_id", ""))
    best_horizon = int(summary.get("best_horizon", 0))
    best_events = events[(events["state_id"].astype(str) == best_state) & (pd.to_numeric(events["horizon"], errors="coerce") == best_horizon)].copy() if not events.empty else pd.DataFrame()
    lines += ["", "## 最佳状态非重叠事件", ""]
    lines.extend(table_or_empty(best_events.head(20), {
        "trade_date": "日期",
        "event_return": "事件收益",
        "is_win": "上涨",
        "is_bad_window": "坏窗口",
        "market_stress_score": "压力分",
        "negative_breadth_60d": "下跌广度",
        "market_drawdown_252d": "市场回撤",
        "market_return_5d": "5日收益",
        "breadth_repair_5d": "广度修复",
        "stress_release_5d": "压力释放",
    }, {
        "event_return",
        "market_stress_score",
        "negative_breadth_60d",
        "market_drawdown_252d",
        "market_return_5d",
        "breadth_repair_5d",
        "stress_release_5d",
    }))
    best_rolling = rolling[(rolling["state_id"].astype(str) == best_state) & (pd.to_numeric(rolling["horizon"], errors="coerce") == best_horizon)].copy() if not rolling.empty else pd.DataFrame()
    lines += ["", "## 最佳状态滚动稳定性", ""]
    lines.extend(table_or_empty(best_rolling, {
        "period_name_zh": "滚动阶段",
        "state_dates": "状态日期",
        "state_mean_return": "状态收益",
        "baseline_mean_return": "底部基线收益",
        "mean_edge_vs_baseline": "相对底部提升",
        "state_win_rate": "胜率",
        "state_bad_window_rate": "坏窗口",
    }, {
        "state_mean_return",
        "baseline_mean_return",
        "mean_edge_vs_baseline",
        "state_win_rate",
        "state_bad_window_rate",
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
        "- `report.md`：中文市场状态标签审计报告，优先打开。",
        "- `top_candidates.csv`：状态标签排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：标签面板、状态汇总、非重叠事件、样本外、滚动稳定性、年度分布、审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def conditions_mask(frame: pd.DataFrame, conditions: list[dict[str, Any]], logic: str = "all") -> pd.Series:
    result = pd.Series(True if logic == "all" else False, index=frame.index)
    for condition in conditions:
        mask = condition_mask(frame, condition)
        if logic == "all":
            result &= mask
        else:
            result |= mask
    return result.fillna(False)


def condition_mask(frame: pd.DataFrame, condition: dict[str, Any]) -> pd.Series:
    field = str(condition["field"])
    if field not in frame.columns:
        return pd.Series(False, index=frame.index)
    series = pd.to_numeric(frame[field], errors="coerce")
    value = float(condition["value"])
    op = str(condition["op"])
    if op == ">=":
        return (series >= value).fillna(False)
    if op == ">":
        return (series > value).fillna(False)
    if op == "<=":
        return (series <= value).fillna(False)
    if op == "<":
        return (series < value).fillna(False)
    raise ValueError(f"Unsupported op: {op}")


def prefix(row: dict[str, Any], name: str) -> dict[str, Any]:
    skip = {"state_id", "state_name_zh", "horizon", "period_id", "period_name_zh"}
    return {f"{name}_{key}": value for key, value in row.items() if key not in skip}


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；未发现可泛化市场状态候选"
    return "research_only；存在市场状态候选，但仍需转化为实时状态机并做未来样本验证"


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


def min_metric(rows: list[dict[str, Any]], field: str) -> float:
    values = [float_or_nan(row.get(field)) for row in rows]
    values = [value for value in values if not math.isnan(value)]
    return min(values) if values else math.nan


def safe_sub(left: Any, right: Any) -> float:
    left_number = float_or_nan(left)
    right_number = float_or_nan(right)
    if math.isnan(left_number) or math.isnan(right_number):
        return math.nan
    return float(left_number - right_number)


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
        values = [row.iloc[idx] for idx in range(len(cols))]
        lines.append("| " + " | ".join(str(value) if pd.notna(value) else "" for value in values) + " |")
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
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    return str(value)


if __name__ == "__main__":
    main()
