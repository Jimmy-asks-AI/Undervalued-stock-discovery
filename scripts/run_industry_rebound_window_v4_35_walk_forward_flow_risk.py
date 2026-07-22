#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_industry_rebound_window_v4_31_wide_index_state_boundary import fmt_pct, none_if_nan, read_json, write_json
from run_industry_rebound_window_v4_34_flow_risk_relative_frontier import apply_conditions


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_35_walk_forward_flow_risk_policy.json"
VERSION = "4.35.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    enriched = pd.read_csv(ROOT / "outputs/industry_rebound_window_v4_34_flow_risk_relative_frontier/debug/flow_risk_enriched_trades.csv", encoding="utf-8-sig")
    enriched["signal_date_dt"] = pd.to_datetime(enriched["signal_date_dt"], errors="coerce")
    enriched["year"] = pd.to_numeric(enriched["year"], errors="coerce").astype("Int64")
    enriched["relative_return_5d"] = pd.to_numeric(enriched["trade_return"], errors="coerce") - pd.to_numeric(enriched["market_return_5d"], errors="coerce")
    year_rules, trades = walk_forward(enriched, policy)
    summary = summary_row(trades, year_rules, policy)
    wf = year_summary(year_rules, trades)
    data_audit = build_data_audit(enriched, year_rules, policy)
    leakage = build_leakage_audit()
    notes = build_notes(summary, year_rules)
    run = run_summary(policy, summary, data_audit, leakage, notes)

    year_rules.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, summary, year_rules, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    enriched.to_csv(debug / "flow_risk_enriched_trades.csv", index=False, encoding="utf-8-sig")
    year_rules.to_csv(debug / "walk_forward_selected_rules.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([summary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.35资金风险偏好年前滚验证完成")
    print(f"实时事件={summary['nonoverlap_events']}")
    print(f"绝对收益={fmt_pct(summary['event_mean_return'])}")
    print(f"相对收益={fmt_pct(summary['event_relative_mean_return'])}")


def walk_forward(df: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    trade_parts: list[pd.DataFrame] = []
    for year in sorted(int(x) for x in df["year"].dropna().unique()):
        train = df[df["year"] < year].copy()
        test = df[df["year"] == year].copy()
        best = select_rule(train, policy)
        if not best:
            rows.append({"year": year, "status": "no_rule", "reason": "训练年份或样本不足"})
            continue
        picked = apply_conditions(test, json.loads(best["conditions_json"])).copy()
        picked["signal_id"] = f"wf_flow_risk_{year}"
        picked["signal_name_zh"] = best["signal_name_zh"]
        picked["signal_type"] = "walk_forward_flow_risk_rule"
        if len(picked):
            trade_parts.append(picked)
        rows.append(
            {
                "year": year,
                "status": "pass",
                "signal_id": f"wf_flow_risk_{year}",
                "signal_name_zh": best["signal_name_zh"],
                "signal_type": "walk_forward_flow_risk_rule",
                "train_events": best["train_events"],
                "train_mean_return": best["train_mean_return"],
                "train_relative_mean_return": best["train_relative_mean_return"],
                "train_bad_window_rate": best["train_bad_window_rate"],
                "test_events": int(len(picked)),
                "test_mean_return": none_if_nan(pd.to_numeric(picked.get("trade_return", pd.Series(dtype=float)), errors="coerce").mean()) if len(picked) else None,
                "test_relative_mean_return": none_if_nan(pd.to_numeric(picked.get("relative_return_5d", pd.Series(dtype=float)), errors="coerce").mean()) if len(picked) else None,
                "conditions_json": best["conditions_json"],
            }
        )
    return pd.DataFrame(rows), pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame(columns=list(df.columns))


def select_rule(train: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    if train["year"].nunique() < int(policy["min_train_years"]):
        return {}
    conditions = single_conditions(train, policy)
    rows: list[dict[str, Any]] = []
    for cond in conditions:
        picked = apply_conditions(train, [cond])
        if len(picked) < int(policy["min_train_events"]):
            continue
        ret = pd.to_numeric(picked["trade_return"], errors="coerce")
        rel = pd.to_numeric(picked["relative_return_5d"], errors="coerce")
        bad = to_bool(picked["is_bad_window"]).mean()
        if bad > float(policy["max_train_bad_window_rate"]):
            continue
        rows.append(
            {
                "signal_name_zh": f"{cond['feature']} {cond['op']} {cond['threshold']:.6g}",
                "train_events": int(len(picked)),
                "train_mean_return": float(ret.mean()),
                "train_relative_mean_return": float(rel.mean()),
                "train_bad_window_rate": float(bad),
                "conditions_json": json.dumps([cond], ensure_ascii=False),
            }
        )
    return max(rows, key=lambda x: (x["train_relative_mean_return"], x["train_mean_return"], -x["train_bad_window_rate"])) if rows else {}


def single_conditions(df: pd.DataFrame, policy: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feature in policy["features"]:
        if feature not in df.columns:
            continue
        s = pd.to_numeric(df[feature], errors="coerce")
        for threshold in sorted(set(float(x) for x in s.quantile(policy["quantiles"]).dropna())):
            out.append({"feature": feature, "op": ">=", "threshold": threshold})
            out.append({"feature": feature, "op": "<=", "threshold": threshold})
    return out


def summary_row(trades: pd.DataFrame, year_rules: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    ret = pd.to_numeric(trades.get("trade_return", pd.Series(dtype=float)), errors="coerce")
    rel = pd.to_numeric(trades.get("relative_return_5d", pd.Series(dtype=float)), errors="coerce")
    years = trades.get("year", pd.Series(dtype=float)).dropna().astype(int)
    count = len(trades)
    concentration = float(years.value_counts(normalize=True).max()) if len(years) else math.nan
    mean = float(ret.mean()) if count else math.nan
    rel_mean = float(rel.mean()) if count else math.nan
    win = float((ret > 0).mean()) if count else math.nan
    bad = float(to_bool(trades["is_bad_window"]).mean()) if count else math.nan
    return {
        "signal_id": "v4_35_walk_forward_flow_risk",
        "signal_name_zh": "年前滚资金风险偏好规则",
        "signal_type": "walk_forward_flow_risk_rule",
        "status": classify(count, mean, rel_mean, win, bad, years.nunique(), concentration, policy),
        "nonoverlap_events": int(count),
        "event_mean_return": none_if_nan(mean),
        "event_relative_mean_return": none_if_nan(rel_mean),
        "event_win_rate": none_if_nan(win),
        "event_bad_window_rate": none_if_nan(bad),
        "event_worst_return": none_if_nan(float(ret.min())) if count else None,
        "active_years": int(years.nunique()) if len(years) else 0,
        "max_single_year_concentration": none_if_nan(concentration),
        "years_with_rule": int((year_rules.get("status", pd.Series(dtype=str)) == "pass").sum()),
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
        return "有效候选待复核"
    if count >= 8 and mean >= 0 and bad <= 0.35:
        return "条件观察"
    return "拒绝"


def year_summary(year_rules: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, rule in year_rules.iterrows():
        year = int(rule["year"])
        g = trades[trades["year"] == year] if not trades.empty else pd.DataFrame()
        rows.append(
            {
                "year": year,
                "status": rule["status"],
                "signal_dates": int(len(g)),
                "signal_mean_return": none_if_nan(pd.to_numeric(g.get("trade_return", pd.Series(dtype=float)), errors="coerce").mean()) if len(g) else None,
                "signal_relative_mean_return": none_if_nan(pd.to_numeric(g.get("relative_return_5d", pd.Series(dtype=float)), errors="coerce").mean()) if len(g) else None,
                "signal_bad_window_rate": none_if_nan(to_bool(g["is_bad_window"]).mean()) if len(g) else None,
            }
        )
    return pd.DataFrame(rows)


def build_data_audit(enriched: pd.DataFrame, year_rules: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    pass_years = int((year_rules.get("status", pd.Series(dtype=str)) == "pass").sum())
    return pd.DataFrame(
        [
            {"audit_item": "flow_risk_panel_loaded", "status": "pass" if len(enriched) else "fail", "evidence": f"events={len(enriched)}", "action": "缺少 V4.34 特征面板时不得验证。"},
            {"audit_item": "relative_return_available", "status": "pass" if "relative_return_5d" in enriched.columns else "fail", "evidence": "relative_return_5d = trade_return - market_return_5d", "action": "缺少市场基准收益时不得评价相对收益。"},
            {"audit_item": "year_forward_rules_exist", "status": "pass" if pass_years > 0 else "fail", "evidence": f"years_with_rule={pass_years}; min_train_years={policy['min_train_years']}", "action": "每年规则必须来自此前年份训练样本。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "year_forward_selection_only", "status": "pass", "evidence": "each test year selects rule from prior years only", "action": "不允许用测试年份收益选择规则。"},
            {"audit_item": "no_trade_instruction", "status": "pass", "evidence": "research_only output", "action": "不生成买卖指令。"},
        ]
    )


def build_notes(summary: dict[str, Any], year_rules: pd.DataFrame) -> dict[str, Any]:
    return {
        "main_diagnosis": "V4.35 将 V4.34 的资金风险偏好边界改为年前滚冻结验证。",
        "next_iterations": [
            f"年前滚后事件 {summary['nonoverlap_events']} 个，绝对收益 {fmt_pct(summary['event_mean_return'])}，相对收益 {fmt_pct(summary['event_relative_mean_return'])}，坏窗口率 {fmt_pct(summary['event_bad_window_rate'])}。",
            f"有规则年份 {int((year_rules.get('status', pd.Series(dtype=str)) == 'pass').sum())} 个。",
            "如果本版明显弱于 V4.34，说明资金风险偏好特征的边界改善主要来自后验选择。",
        ],
    }


def run_summary(policy: dict[str, Any], summary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": summary["signal_id"],
        "primary_realtime_events": int(summary["nonoverlap_events"]),
        "candidate_count": 0,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": summary["signal_id"],
        "best_status": summary["status"],
        "best_nonoverlap_events": int(summary["nonoverlap_events"]),
        "best_event_mean_return": summary["event_mean_return"],
        "best_event_relative_mean_return": summary["event_relative_mean_return"],
        "best_event_bad_window_rate": summary["event_bad_window_rate"],
        "final_verdict": "research_only；资金风险偏好年前滚规则未证明有效反弹窗口",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], summary: dict[str, Any], year_rules: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.35 资金风险偏好年前滚验证报告",
            "",
            notes["main_diagnosis"],
            "",
            f"- 实时事件：{summary['nonoverlap_events']}",
            f"- 绝对收益：{fmt_pct(summary['event_mean_return'])}",
            f"- 相对收益：{fmt_pct(summary['event_relative_mean_return'])}",
            f"- 坏窗口率：{fmt_pct(summary['event_bad_window_rate'])}",
            f"- 最终结论：{run['final_verdict']}",
            "",
            "## 关键判断",
            *[f"- {item}" for item in notes["next_iterations"]],
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


def to_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "yes"])


if __name__ == "__main__":
    main()
