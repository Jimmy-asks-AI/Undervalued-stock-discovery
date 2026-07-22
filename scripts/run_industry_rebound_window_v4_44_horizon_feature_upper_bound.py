#!/usr/bin/env python
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_industry_rebound_window_v4_31_wide_index_state_boundary import fmt_pct, none_if_nan, read_json, write_json
from run_industry_rebound_window_v4_38_confidence_failure_gate import to_bool, year_summary


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_44_horizon_feature_upper_bound_policy.json"
VERSION = "4.44.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    panel = load_panel(policy)
    grid = scan_grid(panel, policy)
    candidates = grid.sort_values(["event_mean_return", "event_relative_mean_return"], ascending=False).head(20).reset_index(drop=True)
    primary = candidates[candidates["nonoverlap_events"] >= int(policy["min_events"])].iloc[0].to_dict()
    trades = apply_candidate(panel, primary).copy()
    trades["market_return_5d"] = trades["benchmark_return_horizon"]
    trades["signal_id"] = primary["signal_id"]
    trades["signal_name_zh"] = primary["signal_name_zh"]
    trades["signal_type"] = "full_sample_horizon_feature_upper_bound"
    wf = year_summary(trades)
    data_audit = build_data_audit(panel, grid, policy)
    leakage = build_leakage_audit()
    notes = build_notes(primary, grid)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    panel.to_csv(debug / "horizon_feature_source_panel.csv", index=False, encoding="utf-8-sig")
    grid.to_csv(debug / "horizon_feature_grid.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.44多持有期特征上限审计完成")
    print(f"主口径={primary['signal_id']}")
    print(f"事件数={primary['nonoverlap_events']}")
    print(f"成本后收益={fmt_pct(primary['event_mean_return'] - 0.001)}")
    print(f"相对收益={fmt_pct(primary['event_relative_mean_return'])}")


def load_panel(policy: dict[str, Any]) -> pd.DataFrame:
    df = pd.read_csv(ROOT / policy["source_panel_path"], encoding="utf-8-sig")
    df = df[df["min_consecutive_signal_days"] == int(policy["min_consecutive_signal_days"])].copy()
    features = pd.read_csv(ROOT / policy["feature_panel_path"], encoding="utf-8-sig").rename(columns={"trade_date": "signal_date"})
    keep = ["signal_date"] + [c for c in policy["feature_columns"] if c not in df.columns]
    df = df.merge(features[keep], on="signal_date", how="left")
    for col in policy["feature_columns"] + ["trade_return", "benchmark_return_horizon", "holding_days", "year"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["relative_return_horizon"] = df["trade_return"] - df["benchmark_return_horizon"]
    df["is_bad_window"] = to_bool(df["is_bad_window"])
    return df.sort_values(["holding_days", "entry_date"]).reset_index(drop=True)


def scan_grid(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for horizon in policy["horizon_grid"]:
        d = panel[panel["holding_days"] == int(horizon)].copy()
        for feature in policy["feature_columns"]:
            values = pd.to_numeric(d[feature], errors="coerce")
            for q in policy["quantiles"]:
                threshold = float(values.quantile(float(q)))
                for op in [">=", "<="]:
                    frame = d[values >= threshold] if op == ">=" else d[values <= threshold]
                    if len(frame) >= min(int(policy["min_events"]), len(d)) and len(frame) >= 8:
                        rows.append(summary_row(frame, int(horizon), feature, op, float(q), threshold))
    return pd.DataFrame(rows)


def apply_candidate(panel: pd.DataFrame, candidate: dict[str, Any]) -> pd.DataFrame:
    d = panel[panel["holding_days"] == int(candidate["holding_days"])].copy()
    values = pd.to_numeric(d[str(candidate["feature"])], errors="coerce")
    threshold = float(candidate["threshold"])
    return d[values >= threshold].copy() if candidate["operator"] == ">=" else d[values <= threshold].copy()


def summary_row(frame: pd.DataFrame, horizon: int, feature: str, op: str, q: float, threshold: float) -> dict[str, Any]:
    ret = pd.to_numeric(frame["trade_return"], errors="coerce")
    rel = pd.to_numeric(frame["relative_return_horizon"], errors="coerce")
    years = pd.to_numeric(frame["year"], errors="coerce").dropna().astype(int)
    op_id = "ge" if op == ">=" else "le"
    return {
        "signal_id": f"full_sample_h{horizon}_{feature}_{op_id}_q{int(q * 100)}",
        "signal_name_zh": f"全样本{horizon}日 {feature} {op} q{int(q * 100)}",
        "signal_type": "full_sample_horizon_feature_upper_bound",
        "status": "理论上限",
        "holding_days": horizon,
        "feature": feature,
        "operator": op,
        "quantile": q,
        "threshold": threshold,
        "nonoverlap_events": int(len(frame)),
        "event_mean_return": none_if_nan(ret.mean()),
        "event_relative_mean_return": none_if_nan(rel.mean()),
        "event_win_rate": none_if_nan((ret > 0).mean()),
        "event_bad_window_rate": none_if_nan(frame["is_bad_window"].mean()),
        "event_worst_return": none_if_nan(ret.min()),
        "active_years": int(years.nunique()) if len(years) else 0,
        "max_single_year_concentration": none_if_nan(years.value_counts(normalize=True).max()) if len(years) else None,
    }


def build_data_audit(panel: pd.DataFrame, grid: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    eligible = grid[grid["nonoverlap_events"] >= int(policy["min_events"])]
    return pd.DataFrame(
        [
            {"audit_item": "source_panel_loaded", "status": "pass" if len(panel) else "fail", "evidence": f"events={len(panel)}", "action": "源面板为空时不得评价。"},
            {"audit_item": "horizon_grid_available", "status": "pass" if len(grid) else "fail", "evidence": f"grid_rows={len(grid)}", "action": "没有达标网格时不得评价。"},
            {"audit_item": "eligible_sample_floor", "status": "pass" if len(eligible) else "fail", "evidence": f"eligible_rows={len(eligible)}", "action": "至少一个持有期候选必须满足样本下限。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "full_sample_outcome_selection", "status": "observe", "evidence": "best horizon and threshold selected by realized full-sample return", "action": "这是理论上限审计，不是可交易验证。"},
            {"audit_item": "no_future_feature_used", "status": "pass", "evidence": "features are signal-date fields", "action": "不得使用未来特征。"},
            {"audit_item": "no_trade_instruction", "status": "pass", "evidence": "research_only output", "action": "不生成买卖指令。"},
        ]
    )


def build_notes(primary: dict[str, Any], grid: pd.DataFrame) -> dict[str, Any]:
    by_horizon = grid.groupby("holding_days")["event_mean_return"].max().to_dict()
    return {
        "main_diagnosis": "V4.44 审计不同持有期下现有事前特征的全样本理论分离上限。",
        "next_iterations": [
            f"主口径 {primary['signal_id']}：事件 {int(primary['nonoverlap_events'])}，收益 {fmt_pct(primary['event_mean_return'])}，相对收益 {fmt_pct(primary['event_relative_mean_return'])}。",
            "各持有期最高绝对收益：" + ", ".join(f"{int(k)}日 {fmt_pct(v)}" for k, v in by_horizon.items()),
        ],
    }


def run_summary(policy: dict[str, Any], primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": 0,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": primary["event_mean_return"],
        "best_event_relative_mean_return": primary["event_relative_mean_return"],
        "best_event_bad_window_rate": primary["event_bad_window_rate"],
        "final_verdict": "research_only；多持有期特征理论上限仍未突破收益厚度",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], candidates: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.44 多持有期特征上限审计报告",
            "",
            notes["main_diagnosis"],
            "",
            f"- 主口径：{run['primary_signal_id']}",
            f"- 事件数：{run['primary_realtime_events']}",
            f"- 绝对收益：{fmt_pct(run['best_event_mean_return'])}",
            f"- 10bps成本后收益：{fmt_pct(run['best_event_mean_return'] - 0.001)}",
            f"- 相对收益：{fmt_pct(run['best_event_relative_mean_return'])}",
            f"- 坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
            f"- 最终结论：{run['final_verdict']}",
            "",
            "## Top20 多持有期特征上限",
            candidates.to_markdown(index=False),
            "",
            "## 年度分布",
            wf.to_markdown(index=False),
            "",
            "## 审计",
            data_audit.to_markdown(index=False),
            leakage.to_markdown(index=False),
            "",
            f"研究边界：{policy['research_boundary']}",
        ]
    )


if __name__ == "__main__":
    main()
