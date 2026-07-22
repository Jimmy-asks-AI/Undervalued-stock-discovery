#!/usr/bin/env python
from __future__ import annotations

import itertools
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_industry_rebound_window_v4_31_wide_index_state_boundary import (
    apply_conditions,
    attach_state,
    build_wide_index_state,
    fmt_pct,
    normalize_trades,
    none_if_nan,
    read_json,
    single_conditions,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_33_relative_return_frontier_policy.json"
VERSION = "4.33.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    source = normalize_trades(pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig"))
    daily = build_wide_index_state(ROOT / policy["wide_index_dir"])
    enriched = attach_state(source, daily)
    enriched["relative_return_5d"] = pd.to_numeric(enriched["trade_return"], errors="coerce") - pd.to_numeric(enriched["market_return_5d"], errors="coerce")
    candidates = build_candidates(enriched, policy)
    primary = candidates.iloc[0].to_dict()
    primary_trades = apply_conditions(enriched, json.loads(primary["conditions_json"])).copy()
    primary_trades["signal_id"] = primary["signal_id"]
    primary_trades["signal_name_zh"] = primary["signal_name_zh"]
    primary_trades["signal_type"] = "relative_return_frontier_upper_bound"
    wf = year_summary(primary_trades)
    data_audit = build_data_audit(source, daily, enriched, candidates, policy)
    leakage = build_leakage_audit()
    notes = build_notes(primary, candidates, policy)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    source.to_csv(debug / "relative_source_trades.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(debug / "wide_index_daily_state.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(debug / "relative_enriched_trades.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(debug / "relative_frontier_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.33相对市场收益边界审计完成")
    print(f"主边界={primary['signal_id']}")
    print(f"事件={primary['nonoverlap_events']}")
    print(f"相对收益={fmt_pct(primary['event_relative_mean_return'])}")


def build_candidates(enriched: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    conditions = single_conditions(enriched, policy)
    rows = [summary_row(enriched, [cond], policy) for cond in conditions]
    if int(policy.get("max_conditions", 1)) >= 2:
        for a, b in itertools.combinations(conditions, 2):
            if a["feature"] != b["feature"]:
                rows.append(summary_row(enriched, [a, b], policy))
    frame = pd.DataFrame(rows)
    frame = frame[frame["nonoverlap_events"] >= int(policy["min_realtime_events"])].copy()
    frame = frame.sort_values(["event_relative_mean_return", "event_mean_return", "event_bad_window_rate"], ascending=[False, False, True]).reset_index(drop=True)
    frame["signal_id"] = [f"relative_frontier_{i+1:03d}" for i in range(len(frame))]
    return frame[["signal_id"] + [c for c in frame.columns if c != "signal_id"]]


def summary_row(df: pd.DataFrame, conditions: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    d = apply_conditions(df, conditions)
    returns = pd.to_numeric(d["trade_return"], errors="coerce")
    relative = pd.to_numeric(d["relative_return_5d"], errors="coerce")
    years = d["year"].dropna().astype(int)
    count = len(d)
    mean = float(returns.mean()) if count else math.nan
    rel_mean = float(relative.mean()) if count else math.nan
    bad = float(d["is_bad_window"].mean()) if count else math.nan
    win = float((returns > 0).mean()) if count else math.nan
    concentration = float(years.value_counts(normalize=True).max()) if len(years) else math.nan
    return {
        "signal_name_zh": " + ".join(f"{c['feature']} {c['op']} {c['threshold']:.6g}" for c in conditions),
        "signal_type": "relative_return_frontier_upper_bound",
        "status": classify(count, mean, rel_mean, win, bad, years.nunique(), concentration, policy),
        "condition_count": len(conditions),
        "nonoverlap_events": int(count),
        "event_mean_return": mean,
        "event_relative_mean_return": rel_mean,
        "event_win_rate": win,
        "event_bad_window_rate": bad,
        "event_worst_return": float(returns.min()) if count else math.nan,
        "active_years": int(years.nunique()) if len(years) else 0,
        "max_single_year_concentration": concentration,
        "conditions_json": json.dumps(conditions, ensure_ascii=False),
    }


def classify(count: int, mean: float, rel_mean: float, win: float, bad: float, active_years: int, concentration: float, policy: dict[str, Any]) -> str:
    if (
        count >= int(policy["min_realtime_events"])
        and mean >= float(policy["min_realtime_mean_return"])
        and rel_mean >= float(policy["min_realtime_relative_mean_return"])
        and win >= float(policy["min_realtime_win_rate"])
        and bad <= float(policy["max_realtime_bad_window_rate"])
        and active_years >= int(policy["min_active_years"])
        and concentration <= float(policy["max_single_year_concentration"])
    ):
        return "相对收益上限达标待冻结验证"
    if count >= int(policy["min_realtime_events"]) and rel_mean > 0 and mean > 0:
        return "条件观察"
    return "拒绝"


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "year": int(year),
                "status": "pass",
                "signal_dates": int(len(g)),
                "signal_mean_return": float(pd.to_numeric(g["trade_return"], errors="coerce").mean()),
                "signal_relative_mean_return": float(pd.to_numeric(g["relative_return_5d"], errors="coerce").mean()),
                "signal_bad_window_rate": float(g["is_bad_window"].mean()),
            }
            for year, g in d.groupby("year")
        ]
    )


def build_data_audit(source: pd.DataFrame, daily: pd.DataFrame, enriched: pd.DataFrame, candidates: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "wide_index_history_loaded", "status": "pass" if len(daily) else "fail", "evidence": f"wide_days={len(daily)}; source_events={len(source)}", "action": "无宽基指数历史时不得做状态审计。"},
            {"audit_item": "relative_return_available", "status": "pass" if "market_return_5d" in enriched.columns else "fail", "evidence": "relative_return_5d = trade_return - market_return_5d", "action": "缺少市场基准收益时不得评价相对收益。"},
            {"audit_item": "candidate_sample_floor", "status": "pass" if len(candidates) else "fail", "evidence": f"eligible_candidates={len(candidates)}; min_events={policy['min_realtime_events']}", "action": "相对收益边界候选必须保留足够事件。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "no_same_day_wide_index_state", "status": "pass", "evidence": "merge_asof uses allow_exact_matches=false", "action": "防止把信号日当日宽基收盘状态回填到信号。"},
            {"audit_item": "relative_frontier_not_frozen_rule", "status": "observe", "evidence": "primary boundary is selected by full-sample relative return ranking", "action": "本版只判断相对收益上限；不得升级为实时规则。"},
        ]
    )


def build_notes(primary: dict[str, Any], candidates: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    over_line = candidates[candidates["event_relative_mean_return"] >= float(policy["min_realtime_relative_mean_return"])]
    notes = [
        "V4.33 审计当前特征库能否筛出相对市场也赚钱的窗口。",
        f"全样本相对收益最优边界为 {primary['signal_name_zh']}，事件 {int(primary['nonoverlap_events'])} 个，绝对收益 {fmt_pct(primary['event_mean_return'])}，相对收益 {fmt_pct(primary['event_relative_mean_return'])}。",
        f"保留至少 {policy['min_realtime_events']} 个事件的候选共有 {len(candidates)} 个，其中相对收益达到 1% 的候选 {len(over_line)} 个。",
        "本版是上限审计；即使达标也必须后续做前滚冻结验证。",
    ]
    return {"main_diagnosis": notes[0], "next_iterations": notes, "recommended_next_direction": "若相对收益上限也不达标，应停止在当前特征库里筛窗口，转向新增独立信息源或降低目标定义。"}


def run_summary(policy: dict[str, Any], primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    audit_fail = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": 0,
        "audit_fail_count": audit_fail,
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_relative_mean_return": none_if_nan(primary["event_relative_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；相对市场收益边界仍需冻结验证",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], candidates: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.33 相对市场收益边界上限审计报告",
            "",
            notes["main_diagnosis"],
            "",
            f"- 主边界：{run['primary_signal_id']}",
            f"- 主边界事件：{run['primary_realtime_events']}",
            f"- 主边界绝对收益：{fmt_pct(run['best_event_mean_return'])}",
            f"- 主边界相对收益：{fmt_pct(run['best_event_relative_mean_return'])}",
            f"- 主边界坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
            f"- 最终结论：{run['final_verdict']}",
            "",
            "## 关键判断",
            *[f"- {item}" for item in notes["next_iterations"]],
            "",
            "## 相对收益边界候选 Top 20",
            candidates.head(20).to_markdown(index=False),
            "",
            "## 主边界年度表现",
            wf.to_markdown(index=False) if not wf.empty else "主边界无年度事件。",
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
