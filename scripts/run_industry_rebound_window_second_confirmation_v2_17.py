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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_second_confirmation_policy_v2_17.json"
VERSION = "2.17.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.17 rebound-window second confirmation audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.17 policy JSON.")
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
    rule_summary, second_confirm_events, baseline_comparison, failure_attribution = run_audit(panel, policy)
    top_candidates = build_top_candidates(rule_summary)
    leakage_audit = build_leakage_audit(policy)
    optimization_notes = build_optimization_notes(top_candidates, policy)
    summary = build_run_summary(policy, panel, rule_summary, top_candidates, leakage_audit, optimization_notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    rule_summary.to_csv(debug_dir / "second_confirmation_summary.csv", index=False, encoding="utf-8-sig")
    second_confirm_events.to_csv(debug_dir / "second_confirmation_events.csv", index=False, encoding="utf-8-sig")
    baseline_comparison.to_csv(debug_dir / "baseline_comparison.csv", index=False, encoding="utf-8-sig")
    failure_attribution.to_csv(debug_dir / "failure_attribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", optimization_notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(summary, top_candidates, second_confirm_events, baseline_comparison, failure_attribution, leakage_audit, optimization_notes, policy),
        encoding="utf-8",
    )

    print("V2.17二次确认与坏窗口控制审计完成")
    print(f"日期面板行数={summary['date_count']}")
    print(f"规则数={summary['rule_count']}")
    print(f"二次确认候选数={summary['second_confirmation_candidate_count']}")
    print(f"审计失败数={summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def read_date_panel(path: Path) -> pd.DataFrame:
    panel = pd.read_csv(path, encoding="utf-8-sig")
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    panel = panel.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    panel["trade_date_text"] = panel["trade_date"].dt.strftime("%Y-%m-%d")
    return panel


def add_features(panel: pd.DataFrame) -> pd.DataFrame:
    output = panel.copy()
    for field in [
        "market_stress_score",
        "negative_breadth_60d",
        "return_pressure",
        "volatility_pressure",
        "market_drawdown_252d",
        "low_value_oversold_non_trap_count",
    ]:
        if field in output.columns:
            values = pd.to_numeric(output[field], errors="coerce")
            output[f"{field}_prev1"] = values.shift(1)
            output[f"{field}_chg1"] = values.diff(1)
    return output


def run_audit(panel: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    event_frames: list[pd.DataFrame] = []
    baseline_rows: list[dict[str, Any]] = []
    attribution_frames: list[pd.DataFrame] = []
    for rule in policy["rules"]:
        rule_id = str(rule["rule_id"])
        base_rule_id = str(rule["base_rule_id"])
        horizon = int(rule["horizon"])
        return_col = f"benchmark_forward_return_{horizon}d"
        valid = panel.dropna(subset=[return_col]).copy()
        bottom_mask = bottom_eligible_mask(valid, policy)
        base_mask = build_base_mask(valid, base_rule_id)
        confirm_mask = build_rule_mask(valid, rule)
        base_events = build_events(valid, base_mask, return_col, horizon, policy, base_rule_id, "base_rule")
        confirm_events = build_events(valid, confirm_mask, return_col, horizon, policy, rule_id, "second_confirmation")
        bottom_events = build_events(valid, bottom_mask, return_col, horizon, policy, "bottom_eligible", "bottom_baseline")
        base_stats = summarize_events(base_events, bottom_events, horizon, policy)
        confirm_stats = summarize_events(confirm_events, bottom_events, horizon, policy)
        row = {
            "rule_id": rule_id,
            "rule_name_zh": rule["rule_name_zh"],
            "base_rule_id": base_rule_id,
            "horizon": horizon,
            **prefix(base_stats, "base"),
            **prefix(confirm_stats, "confirm"),
        }
        row["delta_bad_window_rate"] = safe_sub(row["confirm_bad_window_rate"], row["base_bad_window_rate"])
        row["delta_mean_return"] = safe_sub(row["confirm_mean_return"], row["base_mean_return"])
        row["delta_precision_edge"] = safe_sub(row["confirm_precision_edge_vs_bottom"], row["base_precision_edge_vs_bottom"])
        row["confirmation_score"] = score_rule(row, policy)
        row["confirmation_status"] = classify_rule(row, policy)
        rows.append(row)
        event_frames.append(confirm_events.assign(rule_name_zh=rule["rule_name_zh"], base_rule_id=base_rule_id))
        baseline_rows.extend([
            {"rule_id": rule_id, "baseline": "base_rule", **base_stats},
            {"rule_id": rule_id, "baseline": "bottom_eligible", **summarize_events(bottom_events, bottom_events, horizon, policy)},
        ])
        attribution_frames.append(attribute_failures(valid, base_mask, confirm_mask, return_col, horizon, policy, rule_id, rule["rule_name_zh"]))
    return pd.DataFrame(rows), concat_frames(event_frames), pd.DataFrame(baseline_rows), concat_frames(attribution_frames)


def summarize_events(events: pd.DataFrame, bottom_events: pd.DataFrame, horizon: int, policy: dict[str, Any]) -> dict[str, Any]:
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    bad = float(policy["bad_window_thresholds"][str(horizon)])
    values = pd.to_numeric(events["event_return"], errors="coerce") if not events.empty else pd.Series(dtype=float)
    bottom_values = pd.to_numeric(bottom_events["event_return"], errors="coerce") if not bottom_events.empty else pd.Series(dtype=float)
    precision = float((values >= threshold).mean()) if len(values) else math.nan
    bottom_precision = float((bottom_values >= threshold).mean()) if len(bottom_values) else math.nan
    return {
        "event_count": int(len(values)),
        "mean_return": float(values.mean()) if len(values) else math.nan,
        "median_return": float(values.median()) if len(values) else math.nan,
        "worst_return": float(values.min()) if len(values) else math.nan,
        "win_rate": float((values > 0).mean()) if len(values) else math.nan,
        "bad_window_rate": float((values <= bad).mean()) if len(values) else math.nan,
        "target_precision": precision,
        "bottom_precision": bottom_precision,
        "precision_edge_vs_bottom": safe_sub(precision, bottom_precision),
        "mean_edge_vs_bottom": safe_sub(float(values.mean()) if len(values) else math.nan, float(bottom_values.mean()) if len(bottom_values) else math.nan),
        "event_nav": float((1.0 + values).prod()) if len(values) else math.nan,
    }


def build_events(frame: pd.DataFrame, mask: pd.Series, return_col: str, horizon: int, policy: dict[str, Any], rule_id: str, event_type: str) -> pd.DataFrame:
    selected = frame[mask.reindex(frame.index, fill_value=False)].sort_values("trade_date").copy()
    rows: list[dict[str, Any]] = []
    last_end = pd.Timestamp.min
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    bad = float(policy["bad_window_thresholds"][str(horizon)])
    for _, row in selected.iterrows():
        date = pd.Timestamp(row["trade_date"])
        if date <= last_end:
            continue
        end_date = date + pd.tseries.offsets.BDay(horizon)
        event_return = float(row[return_col])
        rows.append({
            "rule_id": rule_id,
            "event_type": event_type,
            "horizon": horizon,
            "signal_date": date.strftime("%Y-%m-%d"),
            "estimated_end_date": end_date.strftime("%Y-%m-%d"),
            "event_return": event_return,
            "is_target": bool(event_return >= threshold),
            "is_bad_window": bool(event_return <= bad),
            "market_stress_score": row.get("market_stress_score", math.nan),
            "negative_breadth_60d": row.get("negative_breadth_60d", math.nan),
            "return_pressure": row.get("return_pressure", math.nan),
            "volatility_pressure": row.get("volatility_pressure", math.nan),
            "market_drawdown_252d": row.get("market_drawdown_252d", math.nan),
            "low_value_oversold_non_trap_count": row.get("low_value_oversold_non_trap_count", math.nan),
        })
        last_end = end_date
    return pd.DataFrame(rows)


def attribute_failures(frame: pd.DataFrame, base_mask: pd.Series, confirm_mask: pd.Series, return_col: str, horizon: int, policy: dict[str, Any], rule_id: str, rule_name: str) -> pd.DataFrame:
    bad = float(policy["bad_window_thresholds"][str(horizon)])
    base_events = build_events(frame, base_mask, return_col, horizon, policy, rule_id, "base_rule")
    kept = build_events(frame, confirm_mask, return_col, horizon, policy, rule_id, "second_confirmation")
    kept_dates = set(kept["signal_date"].tolist()) if not kept.empty else set()
    base_events["rule_name_zh"] = rule_name
    base_events["kept_by_confirmation"] = base_events["signal_date"].isin(kept_dates)
    base_events["failure_type"] = np.where(base_events["event_return"] <= bad, "base_bad_window", np.where(base_events["event_return"] <= 0, "base_nonpositive", "base_positive"))
    return base_events


def build_top_candidates(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    priority = {"小样本二次确认候选": 0, "二次确认观察": 1, "样本不足": 2, "拒绝": 3}
    output = summary.copy()
    output["_priority"] = output["confirmation_status"].map(priority).fillna(9)
    return output.sort_values(["_priority", "confirmation_score"], ascending=[True, False]).drop(columns=["_priority"])


def score_rule(row: dict[str, Any], policy: dict[str, Any]) -> float:
    return float(
        2.0 * nz(row.get("confirm_mean_edge_vs_bottom"))
        + 1.5 * nz(row.get("confirm_precision_edge_vs_bottom"))
        + 1.0 * (nz(row.get("confirm_win_rate")) - 0.5)
        - 1.2 * nz(row.get("confirm_bad_window_rate"))
        - 0.8 * max(nz(row.get("confirm_event_count"), 0) < int(policy["promotion_thresholds"]["min_events"]), 0)
        - 0.5 * max(nz(row.get("delta_bad_window_rate")), 0)
    )


def classify_rule(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    checks = [
        nz(row.get("confirm_event_count")) >= int(th["min_events"]),
        nz(row.get("confirm_mean_edge_vs_bottom")) >= float(th["min_mean_edge_vs_bottom"]),
        nz(row.get("confirm_precision_edge_vs_bottom")) >= float(th["min_precision_edge_vs_bottom"]),
        nz(row.get("confirm_win_rate")) >= float(th["min_win_rate"]),
        nz(row.get("confirm_bad_window_rate")) <= float(th["max_bad_window_rate"]),
        nz(row.get("confirm_worst_return")) >= float(th["max_worst_event_loss"]),
    ]
    if all(checks):
        return "小样本二次确认候选"
    if not checks[0]:
        return "样本不足"
    if checks[1] and checks[3] and checks[4]:
        return "二次确认观察"
    return "拒绝"


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([
        {"audit_item": "predeclared_second_confirmation_rules", "status": "pass", "evidence": f"rule_count={len(policy['rules'])}", "action": "V2.17只测试配置中声明的少量规则。"},
        {"audit_item": "future_return_used_only_as_outcome", "status": "pass", "evidence": "benchmark_forward_return_* only used in event outcome", "action": "未来收益不作为触发特征。"},
        {"audit_item": "small_sample_boundary", "status": "research_only", "evidence": policy["promotion_rule"], "action": "候选只能标记为小样本观察。"},
    ])


def build_optimization_notes(top_candidates: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top_candidates.empty:
        return {"main_diagnosis": "V2.17没有可排序结果。", "next_iterations": ["检查输入。"]}
    best = top_candidates.iloc[0].to_dict()
    notes = []
    if best.get("confirmation_status") == "小样本二次确认候选":
        notes.append("二次确认降低了坏窗口并保留正收益，但样本仍小，不能升级为可靠窗口。")
        notes.append("下一轮必须做完整逐日净值和最大回撤路径审计。")
    else:
        notes.append("二次确认没有形成足够可靠的坏窗口控制。")
        notes.append("下一轮应回到漏报样本，研究是否需要趋势修复或行业相对强度确认。")
    return {
        "best_rule_id": best.get("rule_id", ""),
        "best_status": best.get("confirmation_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_18_direction": "对V2.17最佳小样本候选做逐日路径、最大回撤和入场后二次确认延迟审计。",
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, rule_summary: pd.DataFrame, top_candidates: pd.DataFrame, leakage_audit: pd.DataFrame, optimization_notes: dict[str, Any]) -> dict[str, Any]:
    candidates = rule_summary[rule_summary["confirmation_status"] == "小样本二次确认候选"] if not rule_summary.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage_audit["status"] == "fail").sum()) if not leakage_audit.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_count": int(len(panel)),
        "rule_count": int(len(policy["rules"])),
        "second_confirmation_candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_status": best.get("confirmation_status", ""),
        "best_confirm_event_count": int(best.get("confirm_event_count", 0)) if pd.notna(best.get("confirm_event_count", math.nan)) else 0,
        "best_confirm_mean_return": float_or_none(best.get("confirm_mean_return")),
        "best_confirm_bad_window_rate": float_or_none(best.get("confirm_bad_window_rate")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": optimization_notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(summary: dict[str, Any], top_candidates: pd.DataFrame, events: pd.DataFrame, baseline: pd.DataFrame, attribution: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V2.17 二次确认与坏窗口控制审计报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines.append("V2.17 基于 V2.16 的误报/漏报，只测试少量二次确认过滤，目标是降低坏窗口。")
    lines += [
        "",
        f"- 日期面板行数：{summary['date_count']}",
        f"- 规则数：{summary['rule_count']}",
        f"- 小样本二次确认候选数：{summary['second_confirmation_candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 二次确认排序",
        "",
    ]
    lines.extend(table_or_empty(top_candidates, {
        "rule_id": "规则ID",
        "rule_name_zh": "规则",
        "base_rule_id": "基础规则",
        "horizon": "持有期",
        "confirmation_status": "状态",
        "confirm_event_count": "确认事件",
        "confirm_mean_return": "确认收益",
        "confirm_mean_edge_vs_bottom": "相对底部收益提升",
        "confirm_target_precision": "目标精确率",
        "confirm_precision_edge_vs_bottom": "精确率提升",
        "confirm_win_rate": "上涨比例",
        "confirm_bad_window_rate": "坏窗口",
        "confirm_worst_return": "最差事件",
        "delta_bad_window_rate": "坏窗口变化",
        "delta_mean_return": "收益变化",
    }, {"confirm_mean_return", "confirm_mean_edge_vs_bottom", "confirm_target_precision", "confirm_precision_edge_vs_bottom", "confirm_win_rate", "confirm_bad_window_rate", "confirm_worst_return", "delta_bad_window_rate", "delta_mean_return"}))
    lines += ["", "## 最佳规则事件", ""]
    best_rule = str(summary.get("best_rule_id", ""))
    best_events = events[events["rule_id"].astype(str) == best_rule].copy() if not events.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_events.head(20), {
        "signal_date": "信号日",
        "estimated_end_date": "估算结束日",
        "event_return": "事件收益",
        "is_target": "是否目标",
        "is_bad_window": "坏窗口",
        "market_stress_score": "压力分",
        "negative_breadth_60d": "下跌广度",
        "return_pressure": "收益压力",
        "volatility_pressure": "波动压力",
        "low_value_oversold_non_trap_count": "非陷阱数",
    }, {"event_return", "market_stress_score", "negative_breadth_60d", "return_pressure", "volatility_pressure"}))
    lines += ["", "## 下一轮优化方向", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议 V2.18 方向：{notes.get('recommended_v2_18_direction', '')}")
    lines += ["", "## 审计", ""]
    lines.extend(table_or_empty(leakage, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += ["", "## 输出文件说明", "", "- `report.md`：中文二次确认审计报告，优先打开。", "- `top_candidates.csv`：二次确认规则排序；不是交易信号。", "- `run_summary.json`：机器可读运行摘要。", "- `debug/`：规则明细、事件、基线对照、失败归因、审计和冻结策略。", "", f"研究边界：{policy['research_boundary']}"]
    return "\n".join(lines)


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；二次确认未形成候选"
    return "research_only；出现小样本二次确认候选，但仍需逐日路径和未来样本验证"


def build_base_mask(frame: pd.DataFrame, base_rule_id: str) -> pd.Series:
    if base_rule_id == "stress55_breadth75":
        return (pd.to_numeric(frame["market_stress_score"], errors="coerce") >= 0.55) & (pd.to_numeric(frame["negative_breadth_60d"], errors="coerce") >= 0.75)
    if base_rule_id == "capitulation_lowvol":
        return (
            (pd.to_numeric(frame["market_drawdown_252d"], errors="coerce") <= -0.20)
            & (pd.to_numeric(frame["negative_breadth_60d"], errors="coerce") >= 0.80)
            & (pd.to_numeric(frame["volatility_pressure"], errors="coerce") <= 0.70)
        )
    return pd.Series(False, index=frame.index)


def bottom_eligible_mask(frame: pd.DataFrame, policy: dict[str, Any]) -> pd.Series:
    result = pd.Series(False, index=frame.index)
    for condition in policy["bottom_eligible_conditions"]:
        result |= condition_mask(frame, condition)
    return result.fillna(False)


def build_rule_mask(frame: pd.DataFrame, rule: dict[str, Any]) -> pd.Series:
    mask = pd.Series(True, index=frame.index)
    for condition in rule["conditions"]:
        mask &= condition_mask(frame, condition)
    return mask.fillna(False)


def condition_mask(frame: pd.DataFrame, condition: dict[str, Any]) -> pd.Series:
    series = pd.to_numeric(frame[str(condition["field"])], errors="coerce")
    value = float(condition["value"])
    op = str(condition["op"])
    if op == ">=":
        return (series >= value).fillna(False)
    if op == "<=":
        return (series <= value).fillna(False)
    if op == ">":
        return (series > value).fillna(False)
    if op == "<":
        return (series < value).fillna(False)
    raise ValueError(f"Unsupported op: {op}")


def prefix(row: dict[str, Any], name: str) -> dict[str, Any]:
    return {f"{name}_{key}": value for key, value in row.items()}


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


def safe_sub(left: Any, right: Any) -> float:
    lnum, rnum = float_or_nan(left), float_or_nan(right)
    if math.isnan(lnum) or math.isnan(rnum):
        return math.nan
    return float(lnum - rnum)


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
