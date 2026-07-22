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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_path_risk_policy_v2_18.json"
VERSION = "2.18.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.18 rebound-window daily path risk audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.18 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    events = pd.read_csv(ROOT / policy["event_source_path"], encoding="utf-8-sig")
    close_matrix = load_close_matrix(ROOT / policy["history_dir"])
    path_summary, event_daily_paths, path_risk_events = run_path_audit(events, close_matrix, policy)
    top_candidates = build_top_candidates(path_summary)
    leakage_audit = build_leakage_audit(policy, close_matrix)
    optimization_notes = build_optimization_notes(top_candidates, policy)
    summary = build_run_summary(policy, close_matrix, path_summary, top_candidates, leakage_audit, optimization_notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    path_summary.to_csv(debug_dir / "daily_path_summary.csv", index=False, encoding="utf-8-sig")
    event_daily_paths.to_csv(debug_dir / "event_daily_paths.csv", index=False, encoding="utf-8-sig")
    path_risk_events.to_csv(debug_dir / "path_risk_events.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", optimization_notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(summary, top_candidates, path_risk_events, leakage_audit, optimization_notes, policy),
        encoding="utf-8",
    )

    print("V2.18逐日路径与最大回撤审计完成")
    print(f"价格日期数={summary['price_date_count']}")
    print(f"价格行业数={summary['price_industry_count']}")
    print(f"路径候选数={summary['path_candidate_count']}")
    print(f"审计失败数={summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_close_matrix(history_dir: Path) -> pd.DataFrame:
    frames: list[pd.Series] = []
    for path in sorted(history_dir.glob("*.csv")):
        raw = pd.read_csv(path, encoding="utf-8-sig")
        if "日期" not in raw.columns or "收盘" not in raw.columns:
            continue
        dates = pd.to_datetime(raw["日期"], errors="coerce")
        close = pd.to_numeric(raw["收盘"], errors="coerce")
        series = pd.Series(close.values, index=dates, name=path.stem.zfill(6)).dropna()
        series = series[~series.index.duplicated(keep="last")]
        if not series.empty:
            frames.append(series)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1, sort=True).sort_index()


def run_path_audit(events: pd.DataFrame, close_matrix: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    all_paths: list[pd.DataFrame] = []
    event_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    returns = close_matrix.pct_change(fill_method=None).mean(axis=1, skipna=True).dropna()
    close_dates = returns.index.sort_values()
    for rule in policy["rules"]:
        rule_id = str(rule["rule_id"])
        horizon = int(rule["horizon"])
        rule_events = events[(events["rule_id"].astype(str) == rule_id) & (pd.to_numeric(events["horizon"], errors="coerce") == horizon)].copy()
        for _, event in rule_events.iterrows():
            path = build_event_path(event, returns, close_dates, horizon, policy)
            if path.empty:
                continue
            all_paths.append(path)
            event_rows.append(summarize_path(path, event, policy))
        rule_event_rows = [row for row in event_rows if row["rule_id"] == rule_id and int(row["horizon"]) == horizon]
        summary_rows.append(summarize_rule(rule_id, horizon, rule_event_rows, policy))
    return pd.DataFrame(summary_rows), concat_frames(all_paths), pd.DataFrame(event_rows)


def build_event_path(event: pd.Series, returns: pd.Series, close_dates: pd.DatetimeIndex, horizon: int, policy: dict[str, Any]) -> pd.DataFrame:
    signal_date = pd.Timestamp(event["signal_date"])
    start_pos = close_dates.searchsorted(signal_date, side="right")
    if start_pos >= len(close_dates):
        return pd.DataFrame()
    end_pos = min(start_pos + horizon - 1, len(close_dates) - 1)
    period_dates = close_dates[start_pos : end_pos + 1]
    if len(period_dates) == 0:
        return pd.DataFrame()
    period_returns = returns.reindex(period_dates).fillna(0.0)
    nav = (1.0 + period_returns).cumprod()
    running_max = nav.cummax()
    drawdown = nav / running_max - 1.0
    return pd.DataFrame(
        {
            "rule_id": event["rule_id"],
            "rule_name_zh": event.get("rule_name_zh", ""),
            "horizon": int(event["horizon"]),
            "signal_date": pd.Timestamp(event["signal_date"]).strftime("%Y-%m-%d"),
            "path_date": period_dates.strftime("%Y-%m-%d"),
            "day_index": np.arange(1, len(period_dates) + 1),
            "daily_return": period_returns.to_numpy(dtype=float),
            "path_nav": nav.to_numpy(dtype=float),
            "path_drawdown": drawdown.to_numpy(dtype=float),
        }
    )


def summarize_path(path: pd.DataFrame, event: pd.Series, policy: dict[str, Any]) -> dict[str, Any]:
    nav = pd.to_numeric(path["path_nav"], errors="coerce")
    drawdown = pd.to_numeric(path["path_drawdown"], errors="coerce")
    min_nav_idx = int(nav.idxmin())
    max_dd_idx = int(drawdown.idxmin())
    thresholds = policy["path_thresholds"]
    min_nav_loss = float(nav.min() - 1.0)
    max_drawdown = float(drawdown.min())
    return {
        "rule_id": str(event["rule_id"]),
        "rule_name_zh": event.get("rule_name_zh", ""),
        "horizon": int(event["horizon"]),
        "signal_date": pd.Timestamp(event["signal_date"]).strftime("%Y-%m-%d"),
        "path_start_date": str(path["path_date"].iloc[0]),
        "path_end_date": str(path["path_date"].iloc[-1]),
        "path_days": int(len(path)),
        "event_forward_return": float(event.get("event_return", math.nan)),
        "path_final_return": float(nav.iloc[-1] - 1.0),
        "min_nav_loss": min_nav_loss,
        "max_path_drawdown": max_drawdown,
        "day_to_min_nav": int(path.loc[min_nav_idx, "day_index"]),
        "day_to_max_drawdown": int(path.loc[max_dd_idx, "day_index"]),
        "is_final_positive": bool(nav.iloc[-1] > 1.0),
        "is_positive_path": bool(min_nav_loss >= float(thresholds["max_allowed_min_nav_drawdown"])),
        "is_severe_path": bool(
            (min_nav_loss < float(thresholds["max_allowed_min_nav_drawdown"]))
            or (max_drawdown < float(thresholds["max_allowed_peak_drawdown"]))
        ),
    }


def summarize_rule(rule_id: str, horizon: int, rows: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    frame = pd.DataFrame(rows)
    thresholds = policy["path_thresholds"]
    if frame.empty:
        return {
            "rule_id": rule_id,
            "horizon": horizon,
            "event_count": 0,
            "path_status": "样本不足",
        }
    row = {
        "rule_id": rule_id,
        "horizon": horizon,
        "event_count": int(len(frame)),
        "mean_path_final_return": float(frame["path_final_return"].mean()),
        "median_path_final_return": float(frame["path_final_return"].median()),
        "worst_path_final_return": float(frame["path_final_return"].min()),
        "mean_min_nav_loss": float(frame["min_nav_loss"].mean()),
        "worst_min_nav_loss": float(frame["min_nav_loss"].min()),
        "mean_max_path_drawdown": float(frame["max_path_drawdown"].mean()),
        "worst_max_path_drawdown": float(frame["max_path_drawdown"].min()),
        "final_win_rate": float(frame["is_final_positive"].mean()),
        "positive_path_rate": float(frame["is_positive_path"].mean()),
        "severe_path_rate": float(frame["is_severe_path"].mean()),
        "avg_day_to_min_nav": float(frame["day_to_min_nav"].mean()),
    }
    row["path_score"] = score_path(row)
    row["path_status"] = classify_path(row, thresholds)
    return row


def score_path(row: dict[str, Any]) -> float:
    return float(
        2.0 * nz(row.get("mean_path_final_return"))
        + 1.0 * (nz(row.get("final_win_rate")) - 0.5)
        + 0.8 * (nz(row.get("positive_path_rate")) - 0.5)
        - 1.5 * nz(row.get("severe_path_rate"))
        + 1.2 * nz(row.get("worst_min_nav_loss"))
    )


def classify_path(row: dict[str, Any], thresholds: dict[str, Any]) -> str:
    checks = [
        nz(row.get("event_count")) >= int(thresholds["min_event_count"]),
        nz(row.get("final_win_rate")) >= float(thresholds["min_final_win_rate"]),
        nz(row.get("positive_path_rate")) >= float(thresholds["min_positive_path_rate"]),
        nz(row.get("severe_path_rate")) <= float(thresholds["max_severe_path_rate"]),
        nz(row.get("worst_min_nav_loss")) >= float(thresholds["max_allowed_min_nav_drawdown"]),
        nz(row.get("worst_max_path_drawdown")) >= float(thresholds["max_allowed_peak_drawdown"]),
    ]
    if all(checks):
        return "路径风险候选"
    if not checks[0]:
        return "样本不足"
    if checks[1] and checks[3]:
        return "路径风险观察"
    return "拒绝"


def build_top_candidates(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame()
    priority = {"路径风险候选": 0, "路径风险观察": 1, "样本不足": 2, "拒绝": 3}
    output = summary.copy()
    output["_priority"] = output["path_status"].map(priority).fillna(9)
    return output.sort_values(["_priority", "path_score"], ascending=[True, False]).drop(columns=["_priority"])


def build_leakage_audit(policy: dict[str, Any], close_matrix: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "daily_path_uses_post_signal_prices_only_as_outcome",
                "status": "pass",
                "evidence": "close matrix is used only after signal_date to measure realized path",
                "action": "日频路径只作为结果审计，不作为触发条件。",
            },
            {
                "audit_item": "price_matrix_available",
                "status": "pass" if not close_matrix.empty else "fail",
                "evidence": f"dates={len(close_matrix)}; industries={len(close_matrix.columns)}",
                "action": "价格矩阵用于全行业等权路径。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(top_candidates: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top_candidates.empty:
        return {"main_diagnosis": "V2.18没有可排序结果。", "next_iterations": ["检查价格矩阵。"]}
    best = top_candidates.iloc[0].to_dict()
    notes: list[str] = []
    if best.get("path_status") == "路径风险候选":
        notes.append("V2.17小样本候选通过逐日路径风险审计，但仍需未来新增样本验证。")
        notes.append("下一轮应做延迟入场和止损/撤退规则的保守审计。")
    else:
        notes.append("V2.17小样本候选没有通过逐日路径风险审计。")
        notes.append("下一轮应优先控制入场后最大浮亏，而不是继续优化最终收益。")
    return {
        "best_rule_id": best.get("rule_id", ""),
        "best_path_status": best.get("path_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_19_direction": "对最佳规则做延迟入场、撤退阈值和新增样本监控框架。"
    }


def build_run_summary(policy: dict[str, Any], close_matrix: pd.DataFrame, path_summary: pd.DataFrame, top_candidates: pd.DataFrame, leakage_audit: pd.DataFrame, optimization_notes: dict[str, Any]) -> dict[str, Any]:
    candidates = path_summary[path_summary["path_status"] == "路径风险候选"] if not path_summary.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage_audit["status"] == "fail").sum()) if not leakage_audit.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "price_date_count": int(len(close_matrix)),
        "price_industry_count": int(len(close_matrix.columns)),
        "path_candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_path_status": best.get("path_status", ""),
        "best_event_count": int(best.get("event_count", 0)) if pd.notna(best.get("event_count", math.nan)) else 0,
        "best_final_win_rate": float_or_none(best.get("final_win_rate")),
        "best_worst_min_nav_loss": float_or_none(best.get("worst_min_nav_loss")),
        "best_severe_path_rate": float_or_none(best.get("severe_path_rate")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": optimization_notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(summary: dict[str, Any], top_candidates: pd.DataFrame, path_risk_events: pd.DataFrame, leakage_audit: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V2.18 逐日路径与最大回撤审计报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines.append("V2.18 对 V2.17 小样本候选做日频路径审计，重点检查最终收益背后的最大浮亏和路径回撤。")
    lines += [
        "",
        f"- 价格日期数：{summary['price_date_count']}",
        f"- 价格行业数：{summary['price_industry_count']}",
        f"- 路径风险候选数：{summary['path_candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 规则排序",
        "",
    ]
    lines.extend(table_or_empty(top_candidates, {
        "rule_id": "规则ID",
        "horizon": "持有期",
        "path_status": "状态",
        "event_count": "事件数",
        "mean_path_final_return": "平均最终收益",
        "worst_path_final_return": "最差最终收益",
        "worst_min_nav_loss": "最大入场浮亏",
        "worst_max_path_drawdown": "最大路径回撤",
        "final_win_rate": "最终胜率",
        "positive_path_rate": "路径可接受比例",
        "severe_path_rate": "严重路径比例",
        "avg_day_to_min_nav": "平均最低点天数",
    }, {"mean_path_final_return", "worst_path_final_return", "worst_min_nav_loss", "worst_max_path_drawdown", "final_win_rate", "positive_path_rate", "severe_path_rate"}))
    lines += ["", "## 最佳规则事件路径风险", ""]
    best_rule = str(summary.get("best_rule_id", ""))
    best_events = path_risk_events[path_risk_events["rule_id"].astype(str) == best_rule].copy() if not path_risk_events.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_events, {
        "signal_date": "信号日",
        "path_start_date": "路径开始",
        "path_end_date": "路径结束",
        "path_final_return": "最终收益",
        "min_nav_loss": "最大入场浮亏",
        "max_path_drawdown": "最大路径回撤",
        "day_to_min_nav": "最低点天数",
        "is_severe_path": "严重路径",
    }, {"path_final_return", "min_nav_loss", "max_path_drawdown"}))
    lines += ["", "## 下一轮优化方向", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议 V2.19 方向：{notes.get('recommended_v2_19_direction', '')}")
    lines += ["", "## 审计", ""]
    lines.extend(table_or_empty(leakage_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += ["", "## 输出文件说明", "", "- `report.md`：中文路径风险审计报告，优先打开。", "- `top_candidates.csv`：路径风险规则排序；不是交易信号。", "- `run_summary.json`：机器可读运行摘要。", "- `debug/`：逐日路径、事件路径风险、审计和冻结策略。", "", f"研究边界：{policy['research_boundary']}"]
    return "\n".join(lines)


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；小样本候选未通过逐日路径风险升级"
    return "research_only；存在路径风险候选，但仍需未来新增样本验证"


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
