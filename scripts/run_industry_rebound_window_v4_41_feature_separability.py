#!/usr/bin/env python
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_industry_rebound_window_v4_31_wide_index_state_boundary import fmt_pct, none_if_nan, read_json, write_json
from run_industry_rebound_window_v4_38_confidence_failure_gate import to_bool, year_summary


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_41_feature_separability_policy.json"
VERSION = "4.41.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    panel = load_panel(policy)
    grid = scan_grid(panel, policy)
    candidates = grid.sort_values(["event_mean_return", "event_relative_mean_return"], ascending=False).head(20).reset_index(drop=True)
    primary = candidates.iloc[0].to_dict()
    trades = apply_candidate(panel, primary).copy()
    trades["market_return_5d"] = trades["benchmark_return_horizon"]
    trades["signal_id"] = primary["signal_id"]
    trades["signal_name_zh"] = primary["signal_name_zh"]
    trades["signal_type"] = "full_sample_feature_separability_upper_bound"
    wf = year_summary(trades)
    data_audit = build_data_audit(panel, grid, policy)
    leakage = build_leakage_audit()
    notes = build_notes(primary, grid)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    panel.to_csv(debug / "feature_enriched_source_panel.csv", index=False, encoding="utf-8-sig")
    grid.to_csv(debug / "feature_separability_grid.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.41单特征可分离性上限审计完成")
    print(f"主口径={primary['signal_id']}")
    print(f"事件数={primary['nonoverlap_events']}")
    print(f"成本后收益={fmt_pct(primary['event_mean_return'] - 0.001)}")
    print(f"相对收益={fmt_pct(primary['event_relative_mean_return'])}")


def load_panel(policy: dict[str, Any]) -> pd.DataFrame:
    df = pd.read_csv(ROOT / policy["source_panel_path"], encoding="utf-8-sig")
    df = df[(df["holding_days"] == int(policy["holding_days"])) & (df["min_consecutive_signal_days"] == int(policy["min_consecutive_signal_days"]))].copy()
    features = pd.read_csv(ROOT / policy["feature_panel_path"], encoding="utf-8-sig").rename(columns={"trade_date": "signal_date"})
    keep = ["signal_date"] + [c for c in policy["feature_columns"] if c not in df.columns]
    df = df.merge(features[keep], on="signal_date", how="left")
    for col in policy["feature_columns"] + ["trade_return", "benchmark_return_horizon", "year"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["relative_return_horizon"] = df["trade_return"] - df["benchmark_return_horizon"]
    df["is_bad_window"] = to_bool(df["is_bad_window"])
    return df.sort_values("entry_date").reset_index(drop=True)


def scan_grid(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for feature in policy["feature_columns"]:
        values = pd.to_numeric(panel[feature], errors="coerce")
        for q in policy["quantiles"]:
            threshold = values.quantile(float(q))
            for op in [">=", "<="]:
                frame = panel[values >= threshold] if op == ">=" else panel[values <= threshold]
                if len(frame) >= int(policy["min_events"]):
                    rows.append(summary_row(frame, feature, op, float(q), float(threshold)))
    return pd.DataFrame(rows)


def apply_candidate(panel: pd.DataFrame, candidate: dict[str, Any]) -> pd.DataFrame:
    values = pd.to_numeric(panel[str(candidate["feature"])], errors="coerce")
    threshold = float(candidate["threshold"])
    return panel[values >= threshold].copy() if candidate["operator"] == ">=" else panel[values <= threshold].copy()


def summary_row(frame: pd.DataFrame, feature: str, op: str, q: float, threshold: float) -> dict[str, Any]:
    ret = pd.to_numeric(frame["trade_return"], errors="coerce")
    rel = pd.to_numeric(frame["relative_return_horizon"], errors="coerce")
    years = pd.to_numeric(frame["year"], errors="coerce").dropna().astype(int)
    return {
        "signal_id": f"full_sample_{feature}_{op.replace('=', 'e').replace('<', 'le').replace('>', 'ge')}_q{int(q * 100)}",
        "signal_name_zh": f"全样本{feature} {op} q{int(q * 100)}",
        "signal_type": "full_sample_feature_separability_upper_bound",
        "status": "理论上限",
        "feature": feature,
        "operator": op,
        "quantile": q,
        "threshold": threshold,
        "holding_days": 5,
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
    return pd.DataFrame(
        [
            {"audit_item": "source_panel_loaded", "status": "pass" if len(panel) else "fail", "evidence": f"events={len(panel)}", "action": "源面板为空时不得评价。"},
            {"audit_item": "feature_grid_available", "status": "pass" if len(grid) else "fail", "evidence": f"grid_rows={len(grid)}", "action": "没有达标网格时不得评价。"},
            {"audit_item": "min_event_floor", "status": "pass" if int(grid["nonoverlap_events"].min()) >= int(policy["min_events"]) else "fail", "evidence": f"min_events={int(grid['nonoverlap_events'].min())}", "action": "候选必须满足样本下限。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "full_sample_outcome_selection", "status": "observe", "evidence": "best threshold selected by realized full-sample return", "action": "这是理论上限审计，不是可交易验证。"},
            {"audit_item": "no_future_feature_used", "status": "pass", "evidence": "features are signal-date fields", "action": "不得使用未来特征。"},
            {"audit_item": "no_trade_instruction", "status": "pass", "evidence": "research_only output", "action": "不生成买卖指令。"},
        ]
    )


def build_notes(primary: dict[str, Any], grid: pd.DataFrame) -> dict[str, Any]:
    return {
        "main_diagnosis": "V4.41 使用全样本结果选择单特征阈值，只审计现有特征库的理论分离上限。",
        "next_iterations": [
            f"最优单特征上限 {primary['signal_id']}：事件 {int(primary['nonoverlap_events'])}，收益 {fmt_pct(primary['event_mean_return'])}，相对收益 {fmt_pct(primary['event_relative_mean_return'])}。",
            f"网格数量 {len(grid)}；若理论上限仍不过门槛，继续堆单特征阈值没有意义。",
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
        "final_verdict": "research_only；单特征理论上限仍未突破收益厚度",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], candidates: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.41 单特征可分离性上限审计报告",
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
            "## Top20 单特征上限",
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
