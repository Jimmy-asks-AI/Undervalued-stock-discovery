#!/usr/bin/env python
from __future__ import annotations

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
    clean,
    fmt_pct,
    normalize_trades,
    none_if_nan,
    read_json,
    single_conditions,
    to_bool,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_32_walk_forward_wide_index_policy.json"
VERSION = "4.32.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    source = normalize_trades(pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig"))
    daily = build_wide_index_state(ROOT / policy["wide_index_dir"])
    enriched = attach_state(source, daily)
    year_rules, realtime_trades = walk_forward_rules(enriched, policy)
    realtime_summary = pd.DataFrame([summary_row(realtime_trades, year_rules, policy)])
    wf = year_summary(year_rules, realtime_trades)
    data_audit = build_data_audit(source, daily, enriched, year_rules, policy)
    leakage = build_leakage_audit()
    notes = build_notes(realtime_summary.iloc[0].to_dict(), year_rules)
    run = run_summary(policy, realtime_summary.iloc[0].to_dict(), data_audit, leakage, notes)

    year_rules.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, realtime_summary, year_rules, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    source.to_csv(debug / "wide_index_source_trades.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(debug / "wide_index_daily_state.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(debug / "wide_index_enriched_trades.csv", index=False, encoding="utf-8-sig")
    year_rules.to_csv(debug / "walk_forward_selected_rules.csv", index=False, encoding="utf-8-sig")
    realtime_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    realtime_summary.to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.32年前滚宽基状态验证完成")
    print(f"实时事件={int(realtime_summary.iloc[0]['nonoverlap_events'])}")
    print(f"平均收益={fmt_pct(realtime_summary.iloc[0]['event_mean_return'])}")
    print(f"最终结论={run['final_verdict']}")


def walk_forward_rules(enriched: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    trades: list[pd.DataFrame] = []
    years = sorted(int(x) for x in enriched["year"].dropna().unique())
    for year in years:
        train = enriched[enriched["year"] < year].copy()
        test = enriched[enriched["year"] == year].copy()
        best = select_rule(train, policy)
        if not best:
            rows.append({"year": year, "status": "no_rule", "reason": "train_years_or_events不足"})
            continue
        conditions = json.loads(best["conditions_json"])
        picked = apply_conditions(test, conditions).copy()
        signal_id = f"wf_wide_index_{year}"
        picked["signal_id"] = signal_id
        picked["signal_name_zh"] = best["signal_name_zh"]
        picked["signal_type"] = "walk_forward_wide_index_rule"
        if len(picked):
            trades.append(picked)
        rows.append(
            {
                "year": year,
                "status": "pass",
                "signal_id": signal_id,
                "signal_name_zh": best["signal_name_zh"],
                "signal_type": "walk_forward_wide_index_rule",
                "train_events": best["train_events"],
                "train_mean_return": best["train_mean_return"],
                "train_bad_window_rate": best["train_bad_window_rate"],
                "test_events": len(picked),
                "test_mean_return": none_if_nan(pd.to_numeric(picked["trade_return"], errors="coerce").mean()) if len(picked) else None,
                "conditions_json": best["conditions_json"],
            }
        )
    all_trades = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame(columns=list(enriched.columns))
    return pd.DataFrame(rows), all_trades


def select_rule(train: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if train["year"].nunique() < int(policy["min_train_years"]):
        return {}
    rows: list[dict[str, Any]] = []
    for cond in single_conditions(train, policy):
        d = apply_conditions(train, [cond])
        if len(d) < int(policy["min_train_events"]):
            continue
        returns = pd.to_numeric(d["trade_return"], errors="coerce")
        bad = float(to_bool(d["is_bad_window"]).mean())
        if bad > float(policy["max_train_bad_window_rate"]):
            continue
        rows.append(
            {
                "signal_name_zh": f"{cond['feature']} {cond['op']} {cond['threshold']:.6g}",
                "train_events": len(d),
                "train_mean_return": float(returns.mean()),
                "train_bad_window_rate": bad,
                "conditions_json": json.dumps([cond], ensure_ascii=False),
            }
        )
    if not rows:
        return {}
    return sorted(rows, key=lambda x: (x["train_mean_return"], -x["train_bad_window_rate"], x["train_events"]), reverse=True)[0]


def summary_row(trades: pd.DataFrame, year_rules: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    returns = pd.to_numeric(trades.get("trade_return", pd.Series(dtype=float)), errors="coerce")
    years = trades.get("year", pd.Series(dtype=float)).dropna().astype(int)
    count = len(trades)
    mean = float(returns.mean()) if count else math.nan
    win = float((returns > 0).mean()) if count else math.nan
    bad = float(to_bool(trades["is_bad_window"]).mean()) if count else math.nan
    concentration = float(years.value_counts(normalize=True).max()) if len(years) else math.nan
    return {
        "signal_id": "v4_32_year_forward_wide_index",
        "signal_name_zh": "年前滚冻结宽基状态规则",
        "signal_type": "walk_forward_wide_index_rule",
        "status": classify(count, mean, win, bad, years.nunique(), concentration, policy),
        "nonoverlap_events": int(count),
        "event_mean_return": none_if_nan(mean),
        "event_win_rate": none_if_nan(win),
        "event_bad_window_rate": none_if_nan(bad),
        "event_worst_return": none_if_nan(float(returns.min())) if count else None,
        "active_years": int(years.nunique()) if len(years) else 0,
        "max_single_year_concentration": none_if_nan(concentration),
        "years_with_rule": int((year_rules.get("status", pd.Series(dtype=str)) == "pass").sum()),
    }


def classify(count: int, mean: float, win: float, bad: float, active_years: int, concentration: float, policy: dict[str, Any]) -> str:
    if (
        count >= int(policy["min_realtime_events"])
        and mean >= float(policy["min_realtime_mean_return"])
        and win >= float(policy["min_realtime_win_rate"])
        and bad <= float(policy["max_realtime_bad_window_rate"])
        and active_years >= int(policy["min_active_years"])
        and concentration <= float(policy["max_single_year_concentration"])
    ):
        return "有效候选待复核"
    if count >= int(policy["min_realtime_events"]) and mean >= 0 and win >= 0.5 and bad <= float(policy["max_realtime_bad_window_rate"]):
        return "条件观察"
    return "拒绝"


def year_summary(year_rules: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, rule in year_rules.iterrows():
        year = int(rule["year"])
        g = trades[trades["year"] == year].copy() if not trades.empty else pd.DataFrame()
        rows.append(
            {
                "year": year,
                "status": rule["status"],
                "signal_dates": int(len(g)),
                "signal_mean_return": none_if_nan(pd.to_numeric(g.get("trade_return", pd.Series(dtype=float)), errors="coerce").mean()) if len(g) else None,
                "signal_bad_window_rate": none_if_nan(to_bool(g["is_bad_window"]).mean()) if len(g) else None,
            }
        )
    return pd.DataFrame(rows)


def build_data_audit(source: pd.DataFrame, daily: pd.DataFrame, enriched: pd.DataFrame, year_rules: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    missing = int(enriched["wide_state_date"].isna().sum())
    pass_years = int((year_rules.get("status", pd.Series(dtype=str)) == "pass").sum())
    return pd.DataFrame(
        [
            {"audit_item": "wide_index_history_loaded", "status": "pass" if len(daily) > 0 else "fail", "evidence": f"wide_days={len(daily)}; source_events={len(source)}", "action": "无宽基指数历史时不得做状态验证。"},
            {"audit_item": "asof_previous_wide_index_state", "status": "pass" if missing == 0 else "fail", "evidence": f"missing_matches={missing}; allow_exact_matches=false", "action": "信号日只能使用此前已存在宽基状态。"},
            {"audit_item": "year_forward_rules_exist", "status": "pass" if pass_years > 0 else "fail", "evidence": f"years_with_rule={pass_years}; min_train_years={policy['min_train_years']}", "action": "每年规则必须来自此前年份训练样本。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "no_same_day_wide_index_state", "status": "pass", "evidence": "merge_asof uses allow_exact_matches=false", "action": "防止把信号日当日宽基收盘状态回填到信号。"},
            {"audit_item": "year_forward_selection_only", "status": "pass", "evidence": "each test year selects rule from prior years only", "action": "不允许用测试年份收益选择规则。"},
        ]
    )


def build_notes(summary: dict[str, Any], year_rules: pd.DataFrame) -> dict[str, Any]:
    notes = [
        "V4.32 将 V4.31 的宽基状态边界搜索改为年前滚冻结验证。",
        f"年前滚规则共触发 {int(summary['nonoverlap_events'])} 个事件，平均收益 {fmt_pct(summary['event_mean_return'])}，坏窗口率 {fmt_pct(summary['event_bad_window_rate'])}。",
        "如果本版低于 V4.31，说明 V4.31 的优势主要来自全样本后验筛选；如果接近 V4.31，说明宽基状态线索至少有一定可迁移性。",
    ]
    return {"main_diagnosis": notes[0], "next_iterations": notes, "years_with_rule": int((year_rules.get("status", pd.Series(dtype=str)) == "pass").sum())}


def run_summary(policy: dict[str, Any], summary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    audit_fail = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": summary["signal_id"],
        "primary_realtime_events": int(summary["nonoverlap_events"]),
        "candidate_count": 0,
        "audit_fail_count": audit_fail,
        "best_signal_id": summary["signal_id"],
        "best_status": summary["status"],
        "best_nonoverlap_events": int(summary["nonoverlap_events"]),
        "best_event_mean_return": summary["event_mean_return"],
        "best_event_bad_window_rate": summary["event_bad_window_rate"],
        "final_verdict": "research_only；年前滚宽基状态规则未证明有效反弹窗口" if summary["status"] != "有效候选待复核" else "research_only；年前滚宽基状态规则达到候选门槛但仍需复核",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], realtime_summary: pd.DataFrame, year_rules: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.32 年前滚宽基状态规则验证报告",
            "",
            notes["main_diagnosis"],
            "",
            f"- 实时事件：{run['primary_realtime_events']}",
            f"- 平均收益：{fmt_pct(run['best_event_mean_return'])}",
            f"- 坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
            f"- 最终结论：{run['final_verdict']}",
            "",
            "## 关键判断",
            *[f"- {item}" for item in notes["next_iterations"]],
            "",
            "## 汇总",
            realtime_summary.to_markdown(index=False),
            "",
            "## 年度规则",
            year_rules.to_markdown(index=False),
            "",
            "## 年度表现",
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
