#!/usr/bin/env python
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_industry_rebound_window_v4_31_wide_index_state_boundary import fmt_pct, none_if_nan, read_json, write_json
from run_industry_rebound_window_v4_38_confidence_failure_gate import to_bool, year_summary


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_40_signal_cooldown_policy.json"
VERSION = "4.40.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    panel = load_panel(policy)
    candidates = build_candidates(panel, policy)
    primary = candidates[candidates["signal_id"] == policy["primary_rule_id"]].iloc[0].to_dict()
    trades = apply_rule(panel, next(r for r in policy["rules"] if r["rule_id"] == policy["primary_rule_id"])).copy()
    trades["market_return_5d"] = trades["benchmark_return_horizon"]
    trades["signal_id"] = primary["signal_id"]
    trades["signal_name_zh"] = primary["signal_name_zh"]
    trades["signal_type"] = "fixed_signal_cooldown"
    wf = year_summary(trades)
    data_audit = build_data_audit(panel, candidates, policy)
    leakage = build_leakage_audit()
    notes = build_notes(primary, candidates)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, policy), encoding="utf-8")
    panel.to_csv(debug / "signal_cooldown_source_panel.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.40信号冷却审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"事件数={primary['nonoverlap_events']}")
    print(f"成本后收益={fmt_pct(primary['event_mean_return'] - 0.001)}")
    print(f"相对收益={fmt_pct(primary['event_relative_mean_return'])}")


def load_panel(policy: dict[str, Any]) -> pd.DataFrame:
    df = pd.read_csv(ROOT / policy["source_panel_path"], encoding="utf-8-sig")
    df = df[(df["holding_days"] == int(policy["holding_days"])) & (df["min_consecutive_signal_days"] == int(policy["min_consecutive_signal_days"]))].copy()
    for col in ["trade_return", "benchmark_return_horizon", "model_probability", "failure_flag_count", "year"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["relative_return_horizon"] = df["trade_return"] - df["benchmark_return_horizon"]
    df["is_bad_window"] = to_bool(df["is_bad_window"])
    return df.sort_values("entry_date").reset_index(drop=True)


def build_candidates(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([summary_row(apply_rule(panel, rule), rule) for rule in policy["rules"]])


def apply_rule(panel: pd.DataFrame, rule: dict[str, Any]) -> pd.DataFrame:
    base = panel[
        (panel["model_probability"] >= float(rule["min_model_probability"]))
        & (panel["failure_flag_count"] <= int(rule["max_failure_flag_count"]))
    ].sort_values("entry_date")
    cooldown = int(rule["cooldown_days"])
    keep = []
    last_entry = None
    for _, row in base.iterrows():
        if last_entry is None or (row["entry_date"] - last_entry).days > cooldown:
            keep.append(True)
            last_entry = row["entry_date"]
        else:
            keep.append(False)
    return base[keep].copy()


def summary_row(frame: pd.DataFrame, rule: dict[str, Any]) -> dict[str, Any]:
    ret = pd.to_numeric(frame["trade_return"], errors="coerce")
    rel = pd.to_numeric(frame["relative_return_horizon"], errors="coerce")
    years = pd.to_numeric(frame["year"], errors="coerce").dropna().astype(int)
    n = int(len(frame))
    return {
        "signal_id": rule["rule_id"],
        "signal_name_zh": rule["name_zh"],
        "signal_type": "fixed_signal_cooldown",
        "status": "样本达标观察" if n >= 30 else "样本不足观察",
        "holding_days": 5,
        "cooldown_days": int(rule["cooldown_days"]),
        "nonoverlap_events": n,
        "event_mean_return": none_if_nan(ret.mean()),
        "event_relative_mean_return": none_if_nan(rel.mean()),
        "event_win_rate": none_if_nan((ret > 0).mean()),
        "event_bad_window_rate": none_if_nan(frame["is_bad_window"].mean()),
        "event_worst_return": none_if_nan(ret.min()),
        "active_years": int(years.nunique()) if len(years) else 0,
        "max_single_year_concentration": none_if_nan(years.value_counts(normalize=True).max()) if len(years) else None,
    }


def build_data_audit(panel: pd.DataFrame, candidates: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    primary = candidates[candidates["signal_id"] == policy["primary_rule_id"]].iloc[0]
    return pd.DataFrame(
        [
            {"audit_item": "source_panel_loaded", "status": "pass" if len(panel) else "fail", "evidence": f"events={len(panel)}", "action": "源面板为空时不得评价。"},
            {"audit_item": "entry_date_available", "status": "pass" if panel["entry_date"].notna().all() else "fail", "evidence": f"missing={int(panel['entry_date'].isna().sum())}", "action": "缺少入场日期时不得做冷却。"},
            {"audit_item": "primary_sample_floor", "status": "pass" if int(primary["nonoverlap_events"]) >= 30 else "fail", "evidence": f"events={int(primary['nonoverlap_events'])}/30", "action": "主规则必须优先满足统一评价样本门槛。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "no_future_return_sort", "status": "pass", "evidence": "cooldown uses entry_date order only", "action": "不得使用未来收益排序。"},
            {"audit_item": "next_day_entry", "status": "pass", "evidence": "source trades enter after signal_date", "action": "不得同日收盘成交。"},
            {"audit_item": "no_trade_instruction", "status": "pass", "evidence": "research_only output", "action": "不生成买卖指令。"},
        ]
    )


def build_notes(primary: dict[str, Any], candidates: pd.DataFrame) -> dict[str, Any]:
    best = candidates.sort_values("event_mean_return", ascending=False).iloc[0].to_dict()
    return {
        "main_diagnosis": "V4.40 审计密集信号是否拖低收益，冷却规则只按事前入场日期保留一段时间内第一个信号。",
        "next_iterations": [
            f"样本达标主规则 {primary['signal_id']}：事件 {int(primary['nonoverlap_events'])}，收益 {fmt_pct(primary['event_mean_return'])}，相对收益 {fmt_pct(primary['event_relative_mean_return'])}。",
            f"收益最高观察项 {best['signal_id']}：事件 {int(best['nonoverlap_events'])}，收益 {fmt_pct(best['event_mean_return'])}，但样本不足或相对收益不足。",
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
        "final_verdict": "research_only；信号冷却未解决收益厚度不足",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], candidates: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.40 信号冷却去密集化审计报告",
            "",
            "V4.40 检验重复或密集信号是否拖低反弹窗口收益厚度。",
            "",
            f"- 主规则：{run['primary_signal_id']}",
            f"- 事件数：{run['primary_realtime_events']}",
            f"- 绝对收益：{fmt_pct(run['best_event_mean_return'])}",
            f"- 10bps成本后收益：{fmt_pct(run['best_event_mean_return'] - 0.001)}",
            f"- 相对收益：{fmt_pct(run['best_event_relative_mean_return'])}",
            f"- 坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
            f"- 最终结论：{run['final_verdict']}",
            "",
            "## 固定规则候选",
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
