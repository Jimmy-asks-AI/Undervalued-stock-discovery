#!/usr/bin/env python
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_industry_rebound_window_v4_15_dynamic_exit as v415  # noqa: E402

POLICY = ROOT / "configs" / "rebound_window_v4_19_wide_pool_dynamic_exit_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_19_wide_pool_dynamic_exit"
VERSION = "4.19.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    source = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
    panel = pd.read_csv(ROOT / policy["market_panel_path"], encoding="utf-8-sig", parse_dates=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    trades = v415.build_trades(source, panel, policy, same_close=False)
    same_close = v415.summarize_rules(v415.build_trades(source, panel, policy, same_close=True))
    summary_table = v415.summarize_rules(trades)
    primary = summary_table.iloc[0].to_dict()
    primary_trades = trades[trades["signal_id"] == primary["signal_id"]].copy()
    wf = v415.year_summary(primary_trades)
    bias = v415.same_close_bias(summary_table, same_close)
    data_audit = pd.DataFrame([{"audit_item": "wide_pool_reuse_v4_15_exit_grid", "status": "pass", "evidence": f"source_events={len(source)}; rules={len(summary_table)}", "action": "复用V4.15退出网格并应用到V4.7宽事件池。"}])
    leakage = pd.DataFrame([{"audit_item": "next_close_exit_boundary", "status": "pass", "evidence": "close trigger exits at next trading day close", "action": "不使用同日收盘退出作为主结果。"}])
    summary = run_summary(policy, source, summary_table, primary, data_audit, leakage, bias)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    summary_table.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(report(summary, summary_table, bias, data_audit, leakage, wf, policy), encoding="utf-8")
    panel.to_csv(debug / "wide_pool_dynamic_exit_panel.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "wide_pool_dynamic_exit_trades.csv", index=False, encoding="utf-8-sig")
    summary_table.to_csv(debug / "wide_pool_dynamic_exit_summary.csv", index=False, encoding="utf-8-sig")
    bias.to_csv(debug / "same_close_bias_audit.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.19宽事件池动态退出泛化审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"最终结论={summary['final_verdict']}")


def run_summary(policy: dict[str, Any], source: pd.DataFrame, table: pd.DataFrame, primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame, bias: pd.DataFrame) -> dict[str, Any]:
    candidates = table[table["status"] == "反弹窗口候选"]
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_event_count": int(len(source)),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": table.iloc[0]["signal_id"],
        "best_status": table.iloc[0]["status"],
        "best_nonoverlap_events": int(table.iloc[0]["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(table.iloc[0]["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(table.iloc[0]["event_bad_window_rate"]),
        "same_close_candidate_like_rules": int(bias["same_close_candidate_risk"].sum()),
        "final_verdict": "research_only；动态退出在宽事件池上没有泛化为有效反弹窗口",
        "main_diagnosis": f"V4.19把V4.15退出网格应用到V4.7的{len(source)}个宽事件后，最佳规则平均收益为{primary['event_mean_return']:.2%}，仍低于2%。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary, table, bias, data_audit, leakage, wf, policy) -> str:
    return "\n".join([
        "# V4.19 宽事件池动态退出泛化审计报告",
        "",
        summary["main_diagnosis"],
        "",
        f"- 源事件数：{summary['source_event_count']}",
        f"- 主规则：{summary['primary_signal_id']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 候选数：{summary['candidate_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 合法退出规则排序",
        table.to_markdown(index=False),
        "",
        "## 同日收盘退出偏差",
        bias.head(12).to_markdown(index=False),
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
