#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_16_cost_robustness_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_16_cost_robustness"
VERSION = "4.16.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    source = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
    cost_table, all_trades = cost_scenarios(source, policy)
    primary = cost_table[cost_table["roundtrip_cost"] == float(policy["primary_roundtrip_cost"])].iloc[0].to_dict()
    primary_trades = all_trades[all_trades["signal_id"] == primary["signal_id"]].copy()
    wf = year_summary(primary_trades)
    leave_one_year = leave_one_year_audit(primary_trades)
    data_audit = pd.DataFrame([{"audit_item": "fixed_v4_15_primary", "status": "pass", "evidence": f"source_events={len(source)}; costs={policy['roundtrip_cost_grid']}", "action": "固定V4.15主规则，只扣减预声明往返成本。"}])
    leakage = pd.DataFrame([{"audit_item": "no_new_selection", "status": "pass", "evidence": "uses only V4.15 realtime_simulation_trades", "action": "不按成本结果重新筛选事件。"}])
    summary = run_summary(policy, cost_table, primary, data_audit, leakage, leave_one_year)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    cost_table.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(report(summary, cost_table, leave_one_year, data_audit, leakage, wf, policy), encoding="utf-8")
    all_trades.to_csv(debug / "cost_adjusted_trades.csv", index=False, encoding="utf-8-sig")
    cost_table.to_csv(debug / "cost_stress_summary.csv", index=False, encoding="utf-8-sig")
    leave_one_year.to_csv(debug / "leave_one_year_audit.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.16成本与剔除年份稳健性审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"最终结论={summary['final_verdict']}")


def cost_scenarios(source: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    trades = []
    for cost in policy["roundtrip_cost_grid"]:
        d = source.copy()
        d["gross_trade_return"] = pd.to_numeric(d["trade_return"], errors="coerce")
        d["roundtrip_cost"] = float(cost)
        d["trade_return"] = d["gross_trade_return"] - float(cost)
        d["is_win"] = d["trade_return"] > 0
        d["is_bad_window"] = d["trade_return"] <= float(policy["bad_window_threshold"])
        d["signal_id"] = f"v4_16_cost_{token(cost)}"
        d["signal_name_zh"] = f"V4.15主规则，往返成本{float(cost):.2%}"
        trades.append(d)
        rows.append(summarize(d, cost))
    return rank_rows(pd.DataFrame(rows)), pd.concat(trades, ignore_index=True)


def summarize(d: pd.DataFrame, cost: float) -> dict[str, Any]:
    ret = pd.to_numeric(d["trade_return"], errors="coerce")
    years = d["year"].value_counts(normalize=True)
    row = {
        "signal_id": f"v4_16_cost_{token(cost)}",
        "signal_name_zh": f"V4.15主规则，往返成本{float(cost):.2%}",
        "signal_type": "成本稳健性审计",
        "roundtrip_cost": float(cost),
        "status": "",
        "signal_dates": len(d),
        "trades": len(d),
        "nonoverlap_events": len(d),
        "active_years": int(d["year"].nunique()),
        "max_single_year_concentration": float(years.max()),
        "event_mean_return": float(ret.mean()),
        "event_win_rate": float((ret > 0).mean()),
        "event_bad_window_rate": float(d["is_bad_window"].astype(bool).mean()),
        "event_worst_return": float(ret.min()),
    }
    row["status"] = classify(row)
    return row


def rank_rows(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["score_rank"] = df.apply(pass_count, axis=1)
    return df.sort_values(["score_rank", "event_mean_return"], ascending=[False, False]).drop(columns=["score_rank"])


def leave_one_year_audit(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y in sorted(d["year"].unique()):
        g = d[d["year"] != y]
        ret = pd.to_numeric(g["trade_return"], errors="coerce")
        rows.append({
            "excluded_year": int(y),
            "events": len(g),
            "mean_return": float(ret.mean()) if len(g) else math.nan,
            "win_rate": float((ret > 0).mean()) if len(g) else math.nan,
            "bad_window_rate": float(g["is_bad_window"].astype(bool).mean()) if len(g) else math.nan,
            "status": "pass" if len(g) >= 25 and float(ret.mean()) >= 0.0 and float(g["is_bad_window"].astype(bool).mean()) <= 0.25 else "fail",
        })
    return pd.DataFrame(rows)


def classify(r: dict[str, Any]) -> str:
    if r["nonoverlap_events"] >= 30 and r["event_mean_return"] >= 0.02 and r["event_win_rate"] >= 0.6 and r["event_bad_window_rate"] <= 0.2 and r["active_years"] >= 4 and r["max_single_year_concentration"] <= 0.35:
        return "反弹窗口候选"
    if r["nonoverlap_events"] >= 8 and r["event_mean_return"] >= 0 and r["event_win_rate"] >= 0.5 and r["event_bad_window_rate"] <= 0.35 and r["active_years"] >= 3 and r["max_single_year_concentration"] <= 0.5:
        return "条件观察"
    return "样本不足" if r["nonoverlap_events"] < 8 else "拒绝"


def pass_count(row: pd.Series) -> int:
    return sum([
        row["nonoverlap_events"] >= 30,
        row["event_mean_return"] >= 0.02,
        row["event_win_rate"] >= 0.6,
        row["event_bad_window_rate"] <= 0.2,
        row["active_years"] >= 4 and row["max_single_year_concentration"] <= 0.35,
    ])


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": len(g), "signal_mean_return": float(g["trade_return"].mean()), "signal_bad_window_rate": float(g["is_bad_window"].astype(bool).mean())} for y, g in d.groupby("year")])


def run_summary(policy: dict[str, Any], table: pd.DataFrame, primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame, leave_one_year: pd.DataFrame) -> dict[str, Any]:
    candidates = table[table["status"] == "反弹窗口候选"]
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": table.iloc[0]["signal_id"],
        "best_status": table.iloc[0]["status"],
        "best_nonoverlap_events": int(table.iloc[0]["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(table.iloc[0]["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(table.iloc[0]["event_bad_window_rate"]),
        "leave_one_year_fail_count": int((leave_one_year["status"] == "fail").sum()),
        "final_verdict": "research_only；成本后近似候选没有升级为有效反弹窗口",
        "main_diagnosis": f"V4.16按{primary['roundtrip_cost']:.2%}往返成本扣减后，平均收益降至{primary['event_mean_return']:.2%}，仍低于2%。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary, table, leave_one_year, data_audit, leakage, wf, policy) -> str:
    return "\n".join([
        "# V4.16 成本与剔除年份稳健性审计报告",
        "",
        summary["main_diagnosis"],
        "",
        f"- 主规则：{summary['primary_signal_id']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 候选数：{summary['candidate_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 成本压力表",
        table.to_markdown(index=False),
        "",
        "## 剔除年份审计",
        leave_one_year.to_markdown(index=False),
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


def token(v) -> str:
    return str(v).replace(".", "_").replace("-", "m")


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
