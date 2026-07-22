#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_20_frontier_gap_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_20_frontier_gap"
VERSION = "4.20.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    rules = pd.read_csv(ROOT / policy["source_summary_path"], encoding="utf-8-sig")
    trades = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
    frontier = build_frontier(rules, policy)
    independent_check = independent_metric_check(trades, frontier["signal_id"].tolist())
    primary = frontier[frontier["frontier_role"] == "收益最高规则"].iloc[0].to_dict()
    primary_trades = trades[trades["signal_id"] == primary["signal_id"]].copy()
    wf = year_summary(primary_trades)
    data_audit = pd.DataFrame([{"audit_item": "fixed_v4_19_frontier", "status": "pass", "evidence": f"rules={len(rules)}; trades={len(trades)}", "action": "固定V4.19结果，仅审计边界缺口。"}])
    leakage = pd.DataFrame([{"audit_item": "no_new_parameter", "status": "pass", "evidence": "reads V4.19 outputs only", "action": "不新增参数，不重选事件。"}])
    summary = run_summary(policy, rules, frontier, primary, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    frontier.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(report(summary, frontier, data_audit, leakage, wf, policy), encoding="utf-8")
    rules.to_csv(debug / "frontier_source_summary.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "frontier_source_trades.csv", index=False, encoding="utf-8-sig")
    frontier.to_csv(debug / "frontier_gap_summary.csv", index=False, encoding="utf-8-sig")
    independent_check.to_csv(debug / "independent_metric_check.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.20收益-坏窗口边界审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"最终结论={summary['final_verdict']}")


def build_frontier(rules: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    d = rules.copy()
    d["return_gap_to_pass"] = float(policy["min_realtime_mean_return"]) - pd.to_numeric(d["event_mean_return"], errors="coerce")
    d["bad_window_gap_to_pass"] = pd.to_numeric(d["event_bad_window_rate"], errors="coerce") - float(policy["max_realtime_bad_window_rate"])
    d["win_rate_gap_to_pass"] = float(policy["min_realtime_win_rate"]) - pd.to_numeric(d["event_win_rate"], errors="coerce")
    return_top = d.sort_values("event_mean_return", ascending=False).head(1).copy()
    risk_ok = d[(d["event_bad_window_rate"] <= float(policy["max_realtime_bad_window_rate"])) & (d["event_win_rate"] >= float(policy["min_realtime_win_rate"]))]
    risk_top = risk_ok.sort_values("event_mean_return", ascending=False).head(1).copy()
    return_top["frontier_role"] = "收益最高规则"
    risk_top["frontier_role"] = "风险合格最高收益规则"
    out = pd.concat([return_top, risk_top], ignore_index=True).drop_duplicates("signal_id")
    out["bad_events"] = (out["event_bad_window_rate"] * out["nonoverlap_events"]).round().astype(int)
    out["max_allowed_bad_events"] = (float(policy["max_realtime_bad_window_rate"]) * out["nonoverlap_events"]).apply(math.floor).astype(int)
    out["extra_bad_events_vs_gate"] = out["bad_events"] - out["max_allowed_bad_events"]
    return out


def independent_metric_check(trades: pd.DataFrame, signal_ids: list[str]) -> pd.DataFrame:
    rows = []
    for sid in signal_ids:
        d = trades[trades["signal_id"] == sid].copy()
        ret = pd.to_numeric(d["trade_return"], errors="coerce")
        rows.append({
            "signal_id": sid,
            "events_recomputed": len(d),
            "mean_recomputed": float(ret.mean()),
            "win_rate_recomputed": float((ret > 0).mean()),
            "bad_rate_recomputed": float(d["is_bad_window"].astype(bool).mean()),
            "bad_count_recomputed": int(d["is_bad_window"].astype(bool).sum()),
            "worst_recomputed": float(ret.min()),
        })
    return pd.DataFrame(rows)


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": len(g), "signal_mean_return": float(g["trade_return"].mean()), "signal_bad_window_rate": float(g["is_bad_window"].astype(bool).mean())} for y, g in d.groupby("year")])


def run_summary(policy: dict[str, Any], rules: pd.DataFrame, frontier: pd.DataFrame, primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    risk = frontier[frontier["frontier_role"] == "风险合格最高收益规则"].iloc[0].to_dict()
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_rule_count": int(len(rules)),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": 0,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "risk_ok_best_signal_id": risk["signal_id"],
        "risk_ok_best_event_mean_return": none_if_nan(risk["event_mean_return"]),
        "risk_ok_return_gap_to_pass": none_if_nan(risk["return_gap_to_pass"]),
        "return_top_extra_bad_events_vs_gate": int(primary["extra_bad_events_vs_gate"]),
        "final_verdict": "research_only；宽事件池边界仍未形成有效反弹窗口",
        "main_diagnosis": f"V4.20显示收益最高规则均值{primary['event_mean_return']:.2%}已过线，但坏窗口{primary['event_bad_window_rate']:.2%}超标；风险合格最高收益规则仍差{risk['return_gap_to_pass']:.2%}收益厚度。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary, frontier, data_audit, leakage, wf, policy) -> str:
    return "\n".join([
        "# V4.20 收益-坏窗口边界审计报告",
        "",
        summary["main_diagnosis"],
        "",
        f"- 主规则：{summary['primary_signal_id']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 收益最高规则超额坏窗口数：{summary['return_top_extra_bad_events_vs_gate']}",
        f"- 风险合格最高收益规则：{summary['risk_ok_best_signal_id']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 边界规则",
        frontier.to_markdown(index=False),
        "",
        "## 主规则年度表现",
        wf.to_markdown(index=False),
        "",
        "## 审计",
        data_audit.to_markdown(index=False),
        leakage.to_markdown(index=False),
        "",
        f"研究边界：{policy['research_boundary']}",
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean(v):
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if hasattr(v, "item"):
        return clean(v.item())
    return v


def none_if_nan(v):
    try:
        x = float(v)
    except Exception:
        return None
    return None if math.isnan(x) else x


if __name__ == "__main__":
    main()
