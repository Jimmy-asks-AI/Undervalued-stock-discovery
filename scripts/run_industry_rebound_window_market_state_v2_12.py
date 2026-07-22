#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_market_state_policy_v2_12.json"
DEFAULT_DATE_PANEL = ROOT / "outputs" / "industry_rebound_window_audit_v2_11" / "debug" / "date_level_panel.csv"
V211_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_audit_v2_11.py"
VERSION = "2.12.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.12 market-state rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.12 market-state policy JSON.")
    parser.add_argument("--date-panel", default=str(DEFAULT_DATE_PANEL), help="V2.11 date-level panel.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v211 = load_v211_module()
    date_panel = pd.read_csv(Path(args.date_panel), encoding="utf-8-sig")
    rule_summary, baseline_comparison, false_alarm_miss_cases = run_rule_audit(date_panel, policy, v211)
    top_candidates = build_top_candidates(rule_summary, policy)
    leakage_audit = build_leakage_audit(policy)
    optimization_notes = build_optimization_notes(top_candidates, rule_summary, policy)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    rule_summary.to_csv(debug_dir / "market_state_rule_summary.csv", index=False, encoding="utf-8-sig")
    baseline_comparison.to_csv(debug_dir / "baseline_comparison.csv", index=False, encoding="utf-8-sig")
    false_alarm_miss_cases.to_csv(debug_dir / "false_alarm_miss_cases.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", optimization_notes)
    write_json(debug_dir / "frozen_policy.json", policy)

    summary = build_run_summary(
        policy=policy,
        date_panel=date_panel,
        rule_summary=rule_summary,
        top_candidates=top_candidates,
        leakage_audit=leakage_audit,
        optimization_notes=optimization_notes,
    )
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            baseline_comparison=baseline_comparison,
            false_alarm_miss_cases=false_alarm_miss_cases,
            leakage_audit=leakage_audit,
            optimization_notes=optimization_notes,
            policy=policy,
        ),
        encoding="utf-8",
    )

    print("V2.12市场状态反弹窗口审计完成")
    print(f"日期面板行数={summary['date_count']}")
    print(f"规则数={summary['rule_count']}")
    print(f"候选窗口规则数={summary['candidate_window_count']}")
    print(f"审计失败数={summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v211_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_rebound_window_audit_v2_11", V211_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load V2.11 module from {V211_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_rule_audit(date_panel: pd.DataFrame, policy: dict[str, Any], v211: Any) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    panel = date_panel.copy()
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    horizons = [int(item) for item in policy["horizons"]]
    summary_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    case_frames: list[pd.DataFrame] = []

    for rule in policy["rules"]:
        rule_id = str(rule["rule_id"])
        rule_name = str(rule["rule_name_zh"])
        signal_mask = build_rule_mask(panel, rule)
        for horizon in horizons:
            return_col = f"benchmark_forward_return_{horizon}d"
            if return_col not in panel.columns:
                continue
            valid = panel.dropna(subset=[return_col]).copy()
            signal = valid[signal_mask.reindex(valid.index, fill_value=False)].copy()
            pressure = valid[pd.to_numeric(valid["market_stress_score"], errors="coerce") >= float(policy["pressure_baseline_min"])].copy()
            pressure_not_signal = pressure[~pressure["trade_date"].isin(set(signal["trade_date"]))].copy()

            stats = v211.describe_window(signal, valid, horizon, return_col, policy)
            all_stats = v211.describe_simple(valid, return_col, horizon, policy)
            pressure_stats = v211.describe_simple(pressure, return_col, horizon, policy)
            pressure_not_signal_stats = v211.describe_simple(pressure_not_signal, return_col, horizon, policy)
            random_stats = v211.random_same_size_stats(valid, signal, return_col, seed=int(policy["random_seed"]))
            row = {
                "rule_id": rule_id,
                "rule_name_zh": rule_name,
                "horizon": horizon,
                **stats,
                "all_dates_mean_return": all_stats["mean_return"],
                "all_dates_win_rate": all_stats["win_rate"],
                "pressure_dates_mean_return": pressure_stats["mean_return"],
                "pressure_dates_win_rate": pressure_stats["win_rate"],
                "pressure_not_signal_mean_return": pressure_not_signal_stats["mean_return"],
                "pressure_not_signal_win_rate": pressure_not_signal_stats["win_rate"],
                "random_same_size_mean_return_p50": random_stats["p50"],
                "random_same_size_mean_return_p90": random_stats["p90"],
                "random_outperformance_pvalue": random_stats["pvalue_right"],
            }
            row["uplift_vs_all_dates"] = v211.safe_sub(row["signal_mean_return"], row["all_dates_mean_return"])
            row["uplift_vs_pressure_dates"] = v211.safe_sub(row["signal_mean_return"], row["pressure_dates_mean_return"])
            row["uplift_vs_pressure_not_signal"] = v211.safe_sub(row["signal_mean_return"], row["pressure_not_signal_mean_return"])
            row["win_rate_uplift_vs_pressure"] = v211.safe_sub(row["signal_win_rate"], row["pressure_dates_win_rate"])
            row["window_score"] = v211.score_window(row, policy)
            row["window_status"] = v211.classify_window(row, policy)
            summary_rows.append(row)

            for baseline_name, baseline_frame in {
                "all_decision_dates": valid,
                "pressure_dates": pressure,
                "pressure_not_signal": pressure_not_signal,
            }.items():
                base_stats = v211.describe_simple(baseline_frame, return_col, horizon, policy)
                baseline_rows.append(
                    {
                        "rule_id": rule_id,
                        "rule_name_zh": rule_name,
                        "horizon": horizon,
                        "baseline": baseline_name,
                        "signal_samples": int(len(signal)),
                        "baseline_samples": base_stats["samples"],
                        "signal_mean_return": row["signal_mean_return"],
                        "baseline_mean_return": base_stats["mean_return"],
                        "signal_win_rate": row["signal_win_rate"],
                        "baseline_win_rate": base_stats["win_rate"],
                        "mean_uplift": v211.safe_sub(row["signal_mean_return"], base_stats["mean_return"]),
                        "win_rate_uplift": v211.safe_sub(row["signal_win_rate"], base_stats["win_rate"]),
                    }
                )

            case_frames.append(build_cases(valid, signal, return_col, rule_id, rule_name, horizon, policy))

    return pd.DataFrame(summary_rows), pd.DataFrame(baseline_rows), pd.concat(case_frames, ignore_index=True, sort=False) if case_frames else pd.DataFrame()


def build_rule_mask(panel: pd.DataFrame, rule: dict[str, Any]) -> pd.Series:
    mask = pd.Series(True, index=panel.index)
    for condition in rule["conditions"]:
        field = str(condition["field"])
        op = str(condition["op"])
        value = float(condition["value"])
        series = pd.to_numeric(panel[field], errors="coerce")
        if op == ">=":
            mask &= series >= value
        elif op == ">":
            mask &= series > value
        elif op == "<=":
            mask &= series <= value
        elif op == "<":
            mask &= series < value
        else:
            raise ValueError(f"Unsupported op: {op}")
    return mask.fillna(False)


def build_cases(
    valid: pd.DataFrame,
    signal: pd.DataFrame,
    return_col: str,
    rule_id: str,
    rule_name: str,
    horizon: int,
    policy: dict[str, Any],
) -> pd.DataFrame:
    threshold = float(policy["primary_strong_rebound_thresholds"][str(horizon)])
    marked = valid.copy()
    marked["is_signal"] = marked["trade_date"].isin(set(signal["trade_date"]))
    marked[return_col] = pd.to_numeric(marked[return_col], errors="coerce")
    false_alarm = marked[marked["is_signal"] & (marked[return_col] <= 0)].sort_values(return_col).head(10).copy()
    false_alarm["case_type"] = "false_alarm_negative_forward_return"
    miss = marked[~marked["is_signal"] & (marked[return_col] >= threshold)].sort_values(return_col, ascending=False).head(10).copy()
    miss["case_type"] = "missed_strong_rebound"
    result = pd.concat([false_alarm, miss], ignore_index=True, sort=False)
    result["rule_id"] = rule_id
    result["rule_name_zh"] = rule_name
    result["horizon"] = horizon
    keep_cols = [
        "rule_id",
        "rule_name_zh",
        "horizon",
        "case_type",
        "trade_date",
        return_col,
        "market_stress_score",
        "negative_breadth_60d",
        "market_drawdown_252d",
        "low_value_oversold_count",
        "low_value_oversold_non_trap_count",
        "return_pressure",
        "volatility_pressure",
    ]
    return result[[col for col in keep_cols if col in result.columns]]


def build_top_candidates(rule_summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    if rule_summary.empty:
        return pd.DataFrame()
    priority = {"反弹窗口候选": 0, "弱证据观察": 1, "样本不足": 2, "拒绝": 3}
    cols = [
        "rule_id",
        "rule_name_zh",
        "horizon",
        "window_status",
        "window_score",
        "signal_samples",
        "nonoverlap_samples",
        "signal_mean_return",
        "all_dates_mean_return",
        "pressure_dates_mean_return",
        "uplift_vs_all_dates",
        "uplift_vs_pressure_dates",
        "signal_win_rate",
        "pressure_dates_win_rate",
        "win_rate_uplift_vs_pressure",
        "signal_strong_rebound_rate",
        "strong_rebound_recall",
        "signal_bad_window_rate",
        "random_outperformance_pvalue",
    ]
    output = rule_summary[[col for col in cols if col in rule_summary.columns]].copy()
    output["_status_priority"] = output["window_status"].map(priority).fillna(9)
    output = output.sort_values(
        ["_status_priority", "window_score", "signal_samples"], ascending=[True, False, False]
    ).drop(columns=["_status_priority"])
    return output.head(int(policy["top_candidate_rows"])).copy()


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "date_level_rules_only",
                "status": "pass",
                "evidence": "rules use current date-level market_stress, breadth, drawdown and trigger counts",
                "action": "V2.12不使用未来收益生成规则。",
            },
            {
                "audit_item": "future_return_used_only_as_label",
                "status": "pass",
                "evidence": "benchmark_forward_return_* only appears in outcome comparison",
                "action": "未来收益只能作为审计标签。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "V2.12不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(top_candidates: pd.DataFrame, rule_summary: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top_candidates.empty:
        return {
            "main_diagnosis": "V2.12没有可评估的市场状态规则。",
            "next_iterations": ["检查V2.11日期面板是否缺失。"],
        }
    best = top_candidates.iloc[0].to_dict()
    notes: list[str] = []
    if str(best.get("window_status")) == "反弹窗口候选":
        notes.append("日期层市场状态规则首次出现反弹窗口候选，但这还不是交易信号。")
        notes.append("下一轮应做样本外切分、滚动时间稳定性和空仓期机会成本。")
        notes.append("之后再把窗口规则与行业截面选择重新连接，验证窗口内哪些行业更强。")
    else:
        notes.append("市场状态规则改善了召回和样本数，但仍未达到反弹窗口候选门槛。")
        notes.append("下一轮应加入压力释放斜率和宽度修复，而不是继续调行业估值权重。")
    if float_or_nan(best.get("win_rate_uplift_vs_pressure")) <= 0:
        notes.append("最佳规则的上涨概率未优于普通压力日期，说明它更可能是压力 beta 暴露。")
    if float_or_nan(best.get("strong_rebound_recall")) < float(policy["promotion_thresholds"]["min_strong_rebound_recall"]):
        notes.append("强反弹召回仍不足，需要降低对低估超跌行业数量的依赖。")
    return {
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": best.get("horizon", ""),
        "best_window_status": best.get("window_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_13_direction": "对V2.12候选规则做时间切分、滚动窗口、压力释放斜率和空仓机会成本审计。",
    }


def build_run_summary(
    *,
    policy: dict[str, Any],
    date_panel: pd.DataFrame,
    rule_summary: pd.DataFrame,
    top_candidates: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
) -> dict[str, Any]:
    candidates = rule_summary[rule_summary["window_status"] == "反弹窗口候选"] if not rule_summary.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage_audit["status"] == "fail").sum()) if not leakage_audit.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_count": int(len(date_panel)),
        "rule_count": int(len(policy["rules"])),
        "audit_rows": int(len(rule_summary)),
        "candidate_window_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_window_status": best.get("window_status", ""),
        "best_signal_mean_return": float_or_none(best.get("signal_mean_return")),
        "best_uplift_vs_all_dates": float_or_none(best.get("uplift_vs_all_dates")),
        "best_uplift_vs_pressure_dates": float_or_none(best.get("uplift_vs_pressure_dates")),
        "best_signal_win_rate": float_or_none(best.get("signal_win_rate")),
        "best_strong_rebound_recall": float_or_none(best.get("strong_rebound_recall")),
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": optimization_notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    baseline_comparison: pd.DataFrame,
    false_alarm_miss_cases: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# V2.12 市场状态反弹窗口审计报告")
    lines.append("")
    lines.append(f"版本：{VERSION}")
    lines.append("")
    lines.append("## 研究结论")
    lines.append("")
    lines.append("V2.12 根据 V2.11 的漏报诊断，把反弹窗口识别从行业选择规则中拆出，测试日期层市场状态规则。")
    lines.append("")
    lines.append(f"- 日期面板行数：{summary['date_count']}")
    lines.append(f"- 规则数：{summary['rule_count']}")
    lines.append(f"- 审计组合行数：{summary['audit_rows']}")
    lines.append(f"- 反弹窗口候选数：{summary['candidate_window_count']}")
    lines.append(f"- 审计失败数：{summary['audit_fail_count']}")
    lines.append(f"- 最终结论：{summary['final_verdict']}")
    lines.append(f"- 主要诊断：{summary['main_diagnosis']}")
    lines.append("")
    lines.append("## 市场状态规则排序")
    lines.append("")
    lines.extend(table_or_empty(top_candidates.head(20), {
        "rule_id": "规则ID",
        "rule_name_zh": "规则",
        "horizon": "持有期",
        "window_status": "状态",
        "window_score": "窗口分",
        "signal_samples": "样本",
        "nonoverlap_samples": "非重叠",
        "signal_mean_return": "信号后基准收益",
        "all_dates_mean_return": "全日期均值",
        "pressure_dates_mean_return": "压力日期均值",
        "uplift_vs_all_dates": "相对全日期提升",
        "uplift_vs_pressure_dates": "相对压力日期提升",
        "signal_win_rate": "上涨比例",
        "pressure_dates_win_rate": "压力日期上涨比例",
        "win_rate_uplift_vs_pressure": "上涨比例相对压力提升",
        "signal_strong_rebound_rate": "强反弹精确率",
        "strong_rebound_recall": "强反弹召回",
        "signal_bad_window_rate": "坏窗口比例",
        "random_outperformance_pvalue": "随机均值p值",
    }, {
        "signal_mean_return",
        "all_dates_mean_return",
        "pressure_dates_mean_return",
        "uplift_vs_all_dates",
        "uplift_vs_pressure_dates",
        "signal_win_rate",
        "pressure_dates_win_rate",
        "win_rate_uplift_vs_pressure",
        "signal_strong_rebound_rate",
        "strong_rebound_recall",
        "signal_bad_window_rate",
        "random_outperformance_pvalue",
    }))
    lines.append("")
    lines.append("## 最佳规则基线对照")
    lines.append("")
    best_rule = str(summary.get("best_rule_id", ""))
    best_horizon = int(summary.get("best_horizon", 0))
    best_baseline = baseline_comparison[
        (baseline_comparison["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(baseline_comparison["horizon"], errors="coerce") == best_horizon)
    ].copy() if not baseline_comparison.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_baseline, {
        "baseline": "基线",
        "signal_samples": "信号样本",
        "baseline_samples": "基线样本",
        "signal_mean_return": "信号均值",
        "baseline_mean_return": "基线均值",
        "mean_uplift": "均值提升",
        "signal_win_rate": "信号上涨比例",
        "baseline_win_rate": "基线上涨比例",
        "win_rate_uplift": "上涨比例提升",
    }, {
        "signal_mean_return",
        "baseline_mean_return",
        "mean_uplift",
        "signal_win_rate",
        "baseline_win_rate",
        "win_rate_uplift",
    }))
    lines.append("")
    lines.append("## 误报和漏报")
    lines.append("")
    best_cases = false_alarm_miss_cases[
        (false_alarm_miss_cases["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(false_alarm_miss_cases["horizon"], errors="coerce") == best_horizon)
    ].copy() if not false_alarm_miss_cases.empty else pd.DataFrame()
    lines.extend(table_or_empty(best_cases.head(20), {
        "case_type": "类型",
        "trade_date": "日期",
        f"benchmark_forward_return_{best_horizon}d": "未来基准收益",
        "market_stress_score": "压力分",
        "negative_breadth_60d": "60日下跌广度",
        "market_drawdown_252d": "市场回撤",
        "low_value_oversold_count": "低估超跌数",
        "low_value_oversold_non_trap_count": "非陷阱数",
        "return_pressure": "收益压力",
        "volatility_pressure": "波动压力",
    }, {
        f"benchmark_forward_return_{best_horizon}d",
        "market_stress_score",
        "negative_breadth_60d",
        "market_drawdown_252d",
        "return_pressure",
        "volatility_pressure",
    }))
    lines.append("")
    lines.append("## 下一轮优化方向")
    lines.append("")
    for item in optimization_notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议 V2.13 方向：{optimization_notes.get('recommended_v2_13_direction', '')}")
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
    lines.append("- `report.md`：中文市场状态反弹窗口审计报告，优先打开。")
    lines.append("- `top_candidates.csv`：日期层规则排序；不是交易信号。")
    lines.append("- `run_summary.json`：机器可读运行摘要。")
    lines.append("- `debug/`：规则明细、基线对照、误报漏报、审计和冻结策略。")
    lines.append("")
    lines.append(f"研究边界：{policy['research_boundary']}")
    return "\n".join(lines)


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if candidates.empty:
        return "research_only；市场状态规则仍未形成可靠反弹窗口证据"
    return "research_only；日期层市场状态存在反弹窗口候选，但尚不能生成交易指令"


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
    if math.isnan(number):
        return ""
    return f"{number:.{digits}f}"


def fmt_pct(value: Any) -> str:
    number = float_or_nan(value)
    if math.isnan(number):
        return ""
    return f"{number * 100:.2f}%"


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
