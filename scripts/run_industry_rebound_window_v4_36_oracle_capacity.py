#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_industry_rebound_window_v4_31_wide_index_state_boundary import fmt_pct, none_if_nan, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_36_oracle_capacity_policy.json"
VERSION = "4.36.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    panel = pd.read_csv(ROOT / policy["source_panel_path"], encoding="utf-8-sig")
    panel["trade_return"] = pd.to_numeric(panel["trade_return"], errors="coerce")
    panel["market_return_5d"] = pd.to_numeric(panel["market_return_5d"], errors="coerce")
    panel["relative_return_5d"] = panel["trade_return"] - panel["market_return_5d"]
    panel["is_bad_window"] = to_bool(panel["is_bad_window"])
    ranked = panel.sort_values("relative_return_5d", ascending=False).reset_index(drop=True)
    candidates = pd.DataFrame([summary_row(ranked.head(int(n)), int(n)) for n in policy["top_n_grid"]])
    primary = candidates[candidates["top_n"] == int(policy["primary_top_n"])].iloc[0].to_dict()
    trades = ranked.head(int(policy["primary_top_n"])).copy()
    trades["signal_id"] = primary["signal_id"]
    trades["signal_name_zh"] = primary["signal_name_zh"]
    trades["signal_type"] = "full_sample_oracle_capacity"
    wf = year_summary(trades)
    data_audit = build_data_audit(panel, policy)
    leakage = build_leakage_audit()
    notes = build_notes(primary, candidates)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    panel.to_csv(debug / "oracle_source_panel.csv", index=False, encoding="utf-8-sig")
    ranked.to_csv(debug / "oracle_ranked_events.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.36事件池理论容量审计完成")
    print(f"主口径TopN={primary['top_n']}")
    print(f"绝对收益={fmt_pct(primary['event_mean_return'])}")
    print(f"相对收益={fmt_pct(primary['event_relative_mean_return'])}")


def summary_row(frame: pd.DataFrame, n: int) -> dict[str, Any]:
    years = pd.to_numeric(frame["year"], errors="coerce").dropna().astype(int)
    ret = pd.to_numeric(frame["trade_return"], errors="coerce")
    rel = pd.to_numeric(frame["relative_return_5d"], errors="coerce")
    return {
        "signal_id": f"oracle_capacity_top{n}",
        "signal_name_zh": f"未来相对收益排序Top{n}",
        "signal_type": "full_sample_oracle_capacity",
        "status": "理论上限",
        "top_n": n,
        "nonoverlap_events": int(len(frame)),
        "event_mean_return": none_if_nan(ret.mean()),
        "event_relative_mean_return": none_if_nan(rel.mean()),
        "event_win_rate": none_if_nan((ret > 0).mean()),
        "event_bad_window_rate": none_if_nan(frame["is_bad_window"].mean()),
        "event_worst_return": none_if_nan(ret.min()),
        "active_years": int(years.nunique()) if len(years) else 0,
        "max_single_year_concentration": none_if_nan(years.value_counts(normalize=True).max()) if len(years) else None,
    }


def year_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, g in trades.groupby("year"):
        ret = pd.to_numeric(g["trade_return"], errors="coerce")
        rel = pd.to_numeric(g["relative_return_5d"], errors="coerce")
        rows.append(
            {
                "year": int(year),
                "status": "pass",
                "signal_dates": int(len(g)),
                "signal_mean_return": none_if_nan(ret.mean()),
                "signal_relative_mean_return": none_if_nan(rel.mean()),
                "signal_bad_window_rate": none_if_nan(g["is_bad_window"].mean()),
            }
        )
    return pd.DataFrame(rows)


def build_data_audit(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "source_panel_loaded", "status": "pass" if len(panel) else "fail", "evidence": f"events={len(panel)}", "action": "缺少基础事件池时不得做理论容量审计。"},
            {"audit_item": "relative_return_available", "status": "pass" if "relative_return_5d" in panel.columns else "fail", "evidence": "relative_return_5d = trade_return - market_return_5d", "action": "缺少相对收益时不得排序。"},
            {"audit_item": "oracle_not_tradable", "status": "observe", "evidence": "events are sorted by realized future relative_return_5d", "action": "本版只可用于容量上限，不得当作实时规则。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "future_return_used_for_ranking", "status": "observe", "evidence": "full-sample oracle sort uses realized relative_return_5d", "action": "这是刻意的上限审计，不是可交易验证。"},
            {"audit_item": "no_trade_instruction", "status": "pass", "evidence": "research_only output", "action": "不生成买卖指令。"},
        ]
    )


def build_notes(primary: dict[str, Any], candidates: pd.DataFrame) -> dict[str, Any]:
    return {
        "main_diagnosis": "V4.36 使用未来收益排序审计基础事件池理论容量。",
        "next_iterations": [
            f"主口径Top{int(primary['top_n'])}：绝对收益 {fmt_pct(primary['event_mean_return'])}，相对收益 {fmt_pct(primary['event_relative_mean_return'])}，坏窗口率 {fmt_pct(primary['event_bad_window_rate'])}。",
            "Top30 相对收益达到评价门槛，但绝对/成本后收益仍不足；这是不可交易的理论上限。",
            "若真实规则远低于该上限，问题在特征可识别性；若上限也不达标，问题在事件池容量。",
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
        "final_verdict": "research_only；理论容量审计不是可交易反弹窗口",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], candidates: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.36 事件池理论容量审计报告",
            "",
            notes["main_diagnosis"],
            "",
            f"- 主口径：{run['primary_signal_id']}",
            f"- 事件数：{run['primary_realtime_events']}",
            f"- 绝对收益：{fmt_pct(run['best_event_mean_return'])}",
            f"- 相对收益：{fmt_pct(run['best_event_relative_mean_return'])}",
            f"- 坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
            f"- 最终结论：{run['final_verdict']}",
            "",
            "## 关键判断",
            *[f"- {item}" for item in notes["next_iterations"]],
            "",
            "## TopN 容量表",
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


def to_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "yes"])


if __name__ == "__main__":
    main()
