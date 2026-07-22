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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_7_short_horizon_expansion_policy.json"
V46_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v4_6_horizon_stability.py"
VERSION = "4.7.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.7 short-horizon expansion rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V4.7 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v46 = load_v46_module()
    v46.VERSION = VERSION
    v43 = v46.load_v43_module()
    v37 = v43.load_v37_module()
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
    predictions, year_summary, model_summary, threshold_audit = v43.run_failure_state_model(panel, policy, v34)
    predictions = v46.attach_forward_metrics(predictions, panel, policy)

    raw_predictions = predictions.copy()
    if not raw_predictions.empty:
        raw_predictions["model_signal"] = raw_predictions["raw_model_signal"]
    raw_trades, raw_summary = v34.run_realtime_simulation(panel, raw_predictions, policy)
    v46.normalize_summary(raw_trades, raw_summary, "v3_7_raw_model_extended_reference", "V3.7原始概率模型扩展样本参考", "原始概率模型参考")

    failure_trades, failure_summary = v34.run_realtime_simulation(panel, predictions, policy)
    v46.normalize_summary(failure_trades, failure_summary, "v4_3_failure_state_extended_reference", "V4.3失败状态过滤扩展样本参考", "失败状态过滤参考")

    all_trades, horizon_sensitivity, primary_trades, primary_summary = v46.run_horizon_grid(predictions, policy)
    relabel_v47(all_trades, horizon_sensitivity, primary_trades, primary_summary)
    leave_one_year = v46.build_leave_one_year_out(primary_trades, policy)
    horizon_audit = v46.build_horizon_audit(policy, predictions, horizon_sensitivity, primary_summary, leave_one_year, failure_summary)
    top_candidates = v46.build_top_candidates(primary_summary, failure_summary, raw_summary, model_summary)
    relabel_v47(top_candidates)
    leakage_audit = v46.build_leakage_audit(policy, data_audit, predictions, threshold_audit, horizon_audit)
    annual_distribution = v46.build_annual_distribution(predictions, raw_trades, failure_trades, primary_trades)
    notes = build_notes(primary_summary, horizon_sensitivity, leave_one_year, horizon_audit)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, primary_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v47_short_horizon_feature_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    breadth_audit.to_csv(debug_dir / "breadth_feature_audit.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    threshold_audit.to_csv(debug_dir / "failure_state_threshold_audit.csv", index=False, encoding="utf-8-sig")
    raw_trades.to_csv(debug_dir / "raw_model_realtime_trades.csv", index=False, encoding="utf-8-sig")
    raw_summary.to_csv(debug_dir / "raw_model_realtime_summary.csv", index=False, encoding="utf-8-sig")
    failure_trades.to_csv(debug_dir / "failure_state_realtime_trades.csv", index=False, encoding="utf-8-sig")
    failure_summary.to_csv(debug_dir / "failure_state_realtime_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    primary_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    all_trades.to_csv(debug_dir / "horizon_grid_realtime_trades.csv", index=False, encoding="utf-8-sig")
    horizon_sensitivity.to_csv(debug_dir / "short_horizon_sensitivity.csv", index=False, encoding="utf-8-sig")
    leave_one_year.to_csv(debug_dir / "leave_one_year_out_audit.csv", index=False, encoding="utf-8-sig")
    horizon_audit.to_csv(debug_dir / "short_horizon_audit.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    report = v46.render_report(
        v34,
        run_summary,
        top_candidates,
        data_audit,
        target_audit,
        year_summary,
        horizon_audit,
        horizon_sensitivity,
        leave_one_year,
        primary_trades,
        leakage_audit,
        notes,
        policy,
    )
    report = report.replace("# V4.6 持久性信号簇多持有期稳定性研究报告", "# V4.7 短持有期扩样反弹窗口研究报告")
    report = report.replace(
        "V4.6 不重新寻找最优参数，而是固定 V4.5 的连续信号簇思路，检查同一信号在 10/20/30 日持有期和剔除年份后是否仍稳定。",
        "V4.7 固定失败状态过滤信号，测试更短持有期是否能自然增加非重叠样本，同时保持胜率和坏窗口可控。",
    )
    report = report.replace("## 多持有期稳定性审计", "## 短持有期扩样审计")
    report = report.replace("## 多持有期敏感性", "## 短持有期敏感性")
    (output_dir / "report.md").write_text(report, encoding="utf-8")

    print("V4.7短持有期扩样反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"主规则实时交易数={run_summary['primary_realtime_events']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v46_module() -> Any:
    spec = importlib.util.spec_from_file_location("v46_horizon_stability", V46_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V4.6 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def relabel_v47(*frames: pd.DataFrame) -> None:
    for frame in frames:
        if frame.empty:
            continue
        for col in frame.columns:
            dtype_name = str(frame[col].dtype)
            if frame[col].dtype == object or dtype_name.startswith("string") or dtype_name == "str":
                frame[col] = frame[col].astype(str).str.replace("V4.6", "V4.7", regex=False)


def build_notes(primary_summary: pd.DataFrame, sensitivity: pd.DataFrame, leave_one_year: pd.DataFrame, horizon_audit: pd.DataFrame) -> dict[str, Any]:
    primary = primary_summary.iloc[0].to_dict() if not primary_summary.empty else {}
    notes: list[str] = []
    events = int(nz(primary.get("nonoverlap_events", 0)))
    mean_return = nz(primary.get("event_mean_return", math.nan), math.nan)
    if events >= 30 and mean_return < 0.02:
        notes.append("V4.7短持有期扩样成功增加样本，但平均收益厚度不足，不能升级为有效反弹窗口。")
    elif str(primary.get("status", "")) == "反弹窗口候选":
        notes.append("V4.7短持有期扩样达到内部候选状态，但仍需统一评价体系确认。")
    elif str(primary.get("status", "")) == "条件观察":
        notes.append("V4.7短持有期扩样为条件观察，尚未证明能有效找到反弹窗口。")
    else:
        notes.append("V4.7短持有期扩样未能证明有效反弹窗口。")
    notes.append(
        f"主规则：非重叠事件 {events}，平均收益 {fmt_pct(primary.get('event_mean_return'))}，"
        f"胜率 {fmt_pct(primary.get('event_win_rate'))}，坏窗口 {fmt_pct(primary.get('event_bad_window_rate'))}。"
    )
    if not sensitivity.empty:
        primary_h = int(primary.get("holding_days", 0) or 0)
        grid = sensitivity[sensitivity["holding_days"].astype(int) == primary_h]
        for _, row in grid.iterrows():
            notes.append(
                f"连续{int(row['min_consecutive_signal_days'])}日、{int(row['holding_days'])}日持有："
                f"事件 {int(row['nonoverlap_events'])}，平均收益 {fmt_pct(row['event_mean_return'])}，"
                f"胜率 {fmt_pct(row['event_win_rate'])}，坏窗口 {fmt_pct(row['event_bad_window_rate'])}。"
            )
    if not leave_one_year.empty:
        failed = leave_one_year[leave_one_year["status"].astype(str) == "fail"]
        notes.append(f"剔除年份审计：失败项 {len(failed)} / {len(leave_one_year)}。")
    sample_audit = horizon_audit[horizon_audit["audit_item"] == "primary_sample_size"] if not horizon_audit.empty else pd.DataFrame()
    if not sample_audit.empty:
        notes.append(f"样本审计：{sample_audit.iloc[0]['evidence']}。")
    notes.append("V4.7的结论是：短持有期可以解决样本数问题，但收益厚度不足；下一步需要寻找能保留样本数同时提升收益厚度的外生过滤。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "下一步做V4.8：在V4.7短持有期样本池上引入外生风险偏好或市场结构过滤，目标是在事件数不低于30的前提下把平均收益提升到2%以上。",
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
        "primary_signal_id": policy["persistent_cluster"]["primary_signal_id"],
        "primary_realtime_events": int(nz(primary.get("nonoverlap_events", 0))),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_signal_id": best.get("signal_id", ""),
        "best_status": best.get("status", ""),
        "best_nonoverlap_events": int(nz(best.get("nonoverlap_events", 0))) if best else 0,
        "best_event_mean_return": float_or_none(best.get("event_mean_return")) if best else None,
        "best_event_bad_window_rate": float_or_none(best.get("event_bad_window_rate")) if best else None,
        "final_verdict": final_verdict(candidates, audit_fail_count, primary),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int, primary: dict[str, Any]) -> str:
    if audit_fail_count:
        return "research_only；存在数据或泄漏审计失败"
    if not candidates.empty:
        return "research_only；存在短持有期扩样候选但仍需统一评价和未来样本验证"
    if int(nz(primary.get("nonoverlap_events", 0))) >= 30:
        return "research_only；短持有期扩样达到样本数但收益厚度不足，不能升级为有效反弹窗口"
    if str(primary.get("status", "")) == "条件观察":
        return "research_only；短持有期扩样为条件观察，不能升级为有效反弹窗口"
    return "research_only；短持有期扩样尚未证明能有效找到反弹窗口"


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
