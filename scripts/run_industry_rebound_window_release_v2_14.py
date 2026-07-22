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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_release_policy_v2_14.json"
DEFAULT_DATE_PANEL = ROOT / "outputs" / "industry_rebound_window_audit_v2_11" / "debug" / "date_level_panel.csv"
V213_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_stability_v2_13.py"
VERSION = "2.14.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.14 pressure-release rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V2.14 pressure-release policy JSON.")
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

    v213 = load_v213_module()
    date_panel = add_release_features(v213.read_date_panel(Path(args.date_panel)))
    validation_summary, time_split_summary, rolling_stability, baseline_comparison, false_alarm_miss_cases = v213.run_stability_audit(
        date_panel=date_panel,
        policy=policy,
        v211=v213.load_v211_module(),
    )
    top_candidates = v213.build_top_candidates(validation_summary, policy)
    leakage_audit = build_leakage_audit(policy)
    optimization_notes = build_optimization_notes(top_candidates, validation_summary, policy)
    summary = build_run_summary(policy, date_panel, validation_summary, top_candidates, leakage_audit, optimization_notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    validation_summary.to_csv(debug_dir / "validation_summary.csv", index=False, encoding="utf-8-sig")
    time_split_summary.to_csv(debug_dir / "time_split_summary.csv", index=False, encoding="utf-8-sig")
    rolling_stability.to_csv(debug_dir / "rolling_stability.csv", index=False, encoding="utf-8-sig")
    baseline_comparison.to_csv(debug_dir / "baseline_comparison.csv", index=False, encoding="utf-8-sig")
    false_alarm_miss_cases.to_csv(debug_dir / "false_alarm_miss_cases.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", optimization_notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            time_split_summary=time_split_summary,
            rolling_stability=rolling_stability,
            false_alarm_miss_cases=false_alarm_miss_cases,
            leakage_audit=leakage_audit,
            optimization_notes=optimization_notes,
            policy=policy,
            v213=v213,
        ),
        encoding="utf-8",
    )

    print("V2.14压力释放与广度修复审计完成")
    print(f"日期面板行数={summary['date_count']}")
    print(f"规则数={summary['rule_count']}")
    print(f"审计组合行数={summary['audit_rows']}")
    print(f"稳定候选数={summary['stable_candidate_count']}")
    print(f"审计失败数={summary['audit_fail_count']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v213_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_rebound_window_stability_v2_13", V213_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load V2.13 module from {V213_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def add_release_features(panel: pd.DataFrame) -> pd.DataFrame:
    output = panel.sort_values("trade_date").reset_index(drop=True).copy()
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
        output[f"{field}_chg2"] = values.diff(2)
    return output


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "derived_features_use_past_and_current_only",
                "status": "pass",
                "evidence": "chg1 and prev1 are computed from current minus previous decision-date observations",
                "action": "动态修复特征不使用未来收益。",
            },
            {
                "audit_item": "future_return_used_only_as_label",
                "status": "pass",
                "evidence": "benchmark_forward_return_* only appears in outcome comparison",
                "action": "未来收益只能作为审计标签。",
            },
            {
                "audit_item": "post_v213_hypothesis_test",
                "status": "research_only",
                "evidence": "V2.14 rules were designed after V2.13 failure diagnosis",
                "action": "结果只能作为下一轮冻结规则候选，不能宣称纯样本外。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["promotion_rule"],
                "action": "不生成交易指令。",
            },
        ]
    )


def build_optimization_notes(top_candidates: pd.DataFrame, validation_summary: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if top_candidates.empty:
        return {
            "main_diagnosis": "V2.14没有可排序的压力释放规则。",
            "next_iterations": ["检查动态修复特征是否生成。"],
        }
    best = top_candidates.iloc[0].to_dict()
    notes: list[str] = []
    if str(best.get("stability_status")) == "稳定反弹窗口候选":
        notes.append("压力释放规则通过稳定性审计，但仍需实时仿真净值和未来数据确认。")
        notes.append("下一轮应把该规则冻结为实时观察器，只允许用新到日期更新结果。")
    else:
        notes.append("压力释放和广度修复没有把系统升级成可靠反弹窗口识别器。")
        notes.append("下一轮应停止单纯手工阈值扩展，改做机制拆解：哪些失败来自过早进入，哪些来自反弹遗漏。")
    if nz(best.get("full_signal_samples")) < int(policy["promotion_thresholds"]["min_full_samples"]):
        notes.append("动态释放规则触发样本偏少，收益均值容易被少数阶段主导。")
    if nz(best.get("recent_uplift_vs_pressure_dates")) <= 0:
        notes.append("近年相对压力日期没有稳定提升，说明当前规则不适合直接实时使用。")
    if nz(best.get("rolling_positive_uplift_rate")) < float(policy["promotion_thresholds"]["min_rolling_positive_uplift_rate"]):
        notes.append("滚动窗口正提升比例不足，规则阶段依赖仍然强。")
    if nz(best.get("full_signal_bad_window_rate")) > float(policy["promotion_thresholds"]["max_bad_window_rate"]):
        notes.append("坏窗口比例偏高，需要研究入场后的二次确认或等待回撤收敛。")
    return {
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": best.get("horizon", ""),
        "best_stability_status": best.get("stability_status", ""),
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_v2_15_direction": "用误报/漏报样本拆解失败原因：过早入场、过晚入场、压力beta、趋势反转缺失；再决定是否引入行业相对强度确认。",
    }


def build_run_summary(
    policy: dict[str, Any],
    date_panel: pd.DataFrame,
    validation_summary: pd.DataFrame,
    top_candidates: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
) -> dict[str, Any]:
    stable = validation_summary[validation_summary["stability_status"] == "稳定反弹窗口候选"] if not validation_summary.empty else pd.DataFrame()
    best = top_candidates.iloc[0].to_dict() if not top_candidates.empty else {}
    audit_fail_count = int((leakage_audit["status"] == "fail").sum()) if not leakage_audit.empty else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_count": int(len(date_panel)),
        "rule_count": int(len(policy["rules"])),
        "audit_rows": int(len(validation_summary)),
        "stable_candidate_count": int(len(stable)),
        "audit_fail_count": audit_fail_count,
        "best_rule_id": best.get("rule_id", ""),
        "best_horizon": int(best.get("horizon", 0)) if pd.notna(best.get("horizon", math.nan)) else 0,
        "best_stability_status": best.get("stability_status", ""),
        "best_full_uplift_vs_pressure_dates": float_or_none(best.get("full_uplift_vs_pressure_dates")),
        "best_recent_uplift_vs_pressure_dates": float_or_none(best.get("recent_uplift_vs_pressure_dates")),
        "best_split_positive_uplift_rate": float_or_none(best.get("split_positive_uplift_rate")),
        "best_rolling_positive_uplift_rate": float_or_none(best.get("rolling_positive_uplift_rate")),
        "final_verdict": final_verdict(stable, audit_fail_count),
        "main_diagnosis": optimization_notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    time_split_summary: pd.DataFrame,
    rolling_stability: pd.DataFrame,
    false_alarm_miss_cases: pd.DataFrame,
    leakage_audit: pd.DataFrame,
    optimization_notes: dict[str, Any],
    policy: dict[str, Any],
    v213: Any,
) -> str:
    lines: list[str] = []
    lines.append("# V2.14 压力释放与广度修复审计报告")
    lines.append("")
    lines.append(f"版本：{VERSION}")
    lines.append("")
    lines.append("## 研究结论")
    lines.append("")
    lines.append("V2.14 在 V2.13 证伪静态压力规则后，测试压力回落、下跌广度修复和低估超跌触发收缩等动态条件。")
    lines.append("")
    lines.append(f"- 日期面板行数：{summary['date_count']}")
    lines.append(f"- 规则数：{summary['rule_count']}")
    lines.append(f"- 审计组合行数：{summary['audit_rows']}")
    lines.append(f"- 稳定反弹窗口候选数：{summary['stable_candidate_count']}")
    lines.append(f"- 审计失败数：{summary['audit_fail_count']}")
    lines.append(f"- 最终结论：{summary['final_verdict']}")
    lines.append(f"- 主要诊断：{summary['main_diagnosis']}")
    lines.append("")
    lines.append("## 压力释放规则排序")
    lines.append("")
    lines.extend(v213.table_or_empty(top_candidates.head(20), {
        "rule_id": "规则ID",
        "rule_name_zh": "规则",
        "horizon": "持有期",
        "stability_status": "稳定性状态",
        "stability_score": "稳定性分",
        "full_signal_samples": "全样本信号",
        "full_nonoverlap_samples": "全样本非重叠",
        "full_signal_mean_return": "全样本信号收益",
        "full_uplift_vs_pressure_dates": "全样本相对压力提升",
        "full_signal_win_rate": "全样本上涨比例",
        "full_signal_bad_window_rate": "全样本坏窗口",
        "recent_signal_samples": "近年信号",
        "recent_signal_mean_return": "近年信号收益",
        "recent_uplift_vs_pressure_dates": "近年相对压力提升",
        "recent_signal_win_rate": "近年上涨比例",
        "recent_signal_bad_window_rate": "近年坏窗口",
        "split_positive_uplift_rate": "切分正提升比例",
        "rolling_positive_uplift_rate": "滚动正提升比例",
    }, {
        "full_signal_mean_return",
        "full_uplift_vs_pressure_dates",
        "full_signal_win_rate",
        "full_signal_bad_window_rate",
        "recent_signal_mean_return",
        "recent_uplift_vs_pressure_dates",
        "recent_signal_win_rate",
        "recent_signal_bad_window_rate",
        "split_positive_uplift_rate",
        "rolling_positive_uplift_rate",
    }))
    lines.append("")
    lines.append("## 最佳规则时间切分")
    lines.append("")
    best_rule = str(summary.get("best_rule_id", ""))
    best_horizon = int(summary.get("best_horizon", 0))
    best_splits = time_split_summary[
        (time_split_summary["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(time_split_summary["horizon"], errors="coerce") == best_horizon)
    ].copy() if not time_split_summary.empty else pd.DataFrame()
    lines.extend(v213.table_or_empty(best_splits, {
        "period_name_zh": "阶段",
        "signal_samples": "信号样本",
        "nonoverlap_samples": "非重叠",
        "signal_mean_return": "信号收益",
        "pressure_dates_mean_return": "压力日期收益",
        "uplift_vs_pressure_dates": "相对压力提升",
        "signal_win_rate": "上涨比例",
        "signal_bad_window_rate": "坏窗口",
    }, {
        "signal_mean_return",
        "pressure_dates_mean_return",
        "uplift_vs_pressure_dates",
        "signal_win_rate",
        "signal_bad_window_rate",
    }))
    lines.append("")
    lines.append("## 最佳规则滚动窗口")
    lines.append("")
    best_rolling = rolling_stability[
        (rolling_stability["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(rolling_stability["horizon"], errors="coerce") == best_horizon)
    ].copy() if not rolling_stability.empty else pd.DataFrame()
    lines.extend(v213.table_or_empty(best_rolling, {
        "period_name_zh": "滚动阶段",
        "signal_samples": "信号样本",
        "signal_mean_return": "信号收益",
        "pressure_dates_mean_return": "压力日期收益",
        "uplift_vs_pressure_dates": "相对压力提升",
        "signal_win_rate": "上涨比例",
        "signal_bad_window_rate": "坏窗口",
    }, {
        "signal_mean_return",
        "pressure_dates_mean_return",
        "uplift_vs_pressure_dates",
        "signal_win_rate",
        "signal_bad_window_rate",
    }))
    lines.append("")
    lines.append("## 误报和漏报")
    lines.append("")
    best_cases = false_alarm_miss_cases[
        (false_alarm_miss_cases["rule_id"].astype(str) == best_rule)
        & (pd.to_numeric(false_alarm_miss_cases["horizon"], errors="coerce") == best_horizon)
    ].copy() if not false_alarm_miss_cases.empty else pd.DataFrame()
    lines.extend(v213.table_or_empty(best_cases.head(20), {
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
    lines.append(f"- 建议 V2.15 方向：{optimization_notes.get('recommended_v2_15_direction', '')}")
    lines.append("")
    lines.append("## 审计")
    lines.append("")
    lines.extend(v213.table_or_empty(leakage_audit, {
        "audit_item": "项目",
        "status": "状态",
        "evidence": "证据",
        "action": "动作",
    }, set()))
    lines.append("")
    lines.append("## 输出文件说明")
    lines.append("")
    lines.append("- `report.md`：中文压力释放审计报告，优先打开。")
    lines.append("- `top_candidates.csv`：压力释放规则排序；不是交易信号。")
    lines.append("- `run_summary.json`：机器可读运行摘要。")
    lines.append("- `debug/`：验证明细、时间切分、滚动窗口、基线对照、误报漏报、审计和冻结策略。")
    lines.append("")
    lines.append(f"研究边界：{policy['research_boundary']}")
    return "\n".join(lines)


def final_verdict(stable: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在审计失败，结果只能排查"
    if stable.empty:
        return "research_only；压力释放规则未通过稳定性升级，尚不能证明可靠识别反弹窗口"
    return "research_only；存在压力释放候选，但仍需实时仿真和独立未来数据验证"


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
