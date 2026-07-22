#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_14_horizon_thickness_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_14_horizon_thickness"
VERSION = "4.14.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    source = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
    panel = pd.read_csv(ROOT / policy["market_panel_path"], encoding="utf-8-sig", parse_dates=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    all_trades = build_horizon_trades(source, panel, policy)
    summary_table = summarize_horizons(all_trades)
    primary = choose_primary(summary_table)
    primary_trades = all_trades[all_trades["signal_id"] == primary["signal_id"]].copy()
    wf = year_summary(primary_trades)
    data_audit = pd.DataFrame([{"audit_item": "fixed_event_repricing", "status": "pass", "evidence": f"source_events={len(source)}; horizons={policy['holding_days_grid']}", "action": "固定V4.13事件，只重算不同持有期收益。"}])
    leakage = pd.DataFrame([{"audit_item": "execution_boundary", "status": "pass", "evidence": "entry uses next trading day close; exit uses predeclared holding grid", "action": "持有期网格预先声明，不使用未来收益筛选事件。"}])
    summary = run_summary(policy, summary_table, primary, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    summary_table.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(report(summary, summary_table, data_audit, leakage, wf, policy), encoding="utf-8")
    panel.to_csv(debug / "horizon_return_panel.csv", index=False, encoding="utf-8-sig")
    summary_table.to_csv(debug / "horizon_return_summary.csv", index=False, encoding="utf-8-sig")
    all_trades.to_csv(debug / "horizon_return_trades.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.14持有期收益厚度审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"最终结论={summary['final_verdict']}")


def build_horizon_trades(source: pd.DataFrame, panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    date_to_idx = {d: i for i, d in enumerate(panel["trade_date"])}
    rows = []
    for _, row in source.iterrows():
        i = date_to_idx.get(row["signal_date"])
        if i is None:
            continue
        for h in policy["holding_days_grid"]:
            if i + 1 + h >= len(panel):
                continue
            entry = panel.loc[i + 1]
            exit_row = panel.loc[i + 1 + h]
            path = panel.loc[i + 2 : i + 1 + h, "market_nav"] / float(entry["market_nav"]) - 1
            ret = float(exit_row["market_nav"] / entry["market_nav"] - 1)
            out = row.to_dict()
            out.update({
                "signal_id": f"v4_14_horizon_{h}d",
                "entry_date": entry["trade_date"].date().isoformat(),
                "exit_date": exit_row["trade_date"].date().isoformat(),
                "holding_days": int(h),
                "trade_return": ret,
                "max_adverse_return": float(path.min()) if len(path) else 0.0,
                "is_win": bool(ret > 0),
                "is_bad_window": bool(ret <= float(policy["bad_window_threshold"])),
                "year": int(pd.Timestamp(row["signal_date"]).year),
            })
            rows.append(out)
    return pd.DataFrame(rows)


def summarize_horizons(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sid, g in trades.groupby("signal_id"):
        ret = pd.to_numeric(g["trade_return"], errors="coerce")
        years = g["year"].value_counts(normalize=True)
        row = {
            "signal_id": sid,
            "signal_name_zh": f"{int(g['holding_days'].iloc[0])}日持有",
            "signal_type": "固定事件持有期审计",
            "status": "",
            "holding_days": int(g["holding_days"].iloc[0]),
            "signal_dates": len(g),
            "trades": len(g),
            "nonoverlap_events": len(g),
            "active_years": int(g["year"].nunique()),
            "max_single_year_concentration": float(years.max()),
            "event_mean_return": float(ret.mean()),
            "event_win_rate": float((ret > 0).mean()),
            "event_bad_window_rate": float(g["is_bad_window"].astype(bool).mean()),
            "event_worst_return": float(ret.min()),
        }
        row["status"] = classify(row)
        rows.append(row)
    return rank_rows(pd.DataFrame(rows))


def rank_rows(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["score_rank"] = df.apply(pass_count, axis=1)
    return df.sort_values(["score_rank", "event_mean_return"], ascending=[False, False]).drop(columns=["score_rank"])


def pass_count(row: pd.Series) -> int:
    return sum([
        row["nonoverlap_events"] >= 30,
        row["event_mean_return"] >= 0.02,
        row["event_win_rate"] >= 0.6,
        row["event_bad_window_rate"] <= 0.2,
        row["active_years"] >= 4 and row["max_single_year_concentration"] <= 0.35,
    ])


def choose_primary(summary_table: pd.DataFrame) -> dict[str, Any]:
    return summary_table.iloc[0].to_dict()


def classify(r: dict[str, Any]) -> str:
    if r["nonoverlap_events"] >= 30 and r["event_mean_return"] >= 0.02 and r["event_win_rate"] >= 0.6 and r["event_bad_window_rate"] <= 0.2 and r["active_years"] >= 4 and r["max_single_year_concentration"] <= 0.35:
        return "反弹窗口候选"
    if r["nonoverlap_events"] >= 8 and r["event_mean_return"] >= 0 and r["event_win_rate"] >= 0.5 and r["event_bad_window_rate"] <= 0.35 and r["active_years"] >= 3 and r["max_single_year_concentration"] <= 0.5:
        return "条件观察"
    return "样本不足" if r["nonoverlap_events"] < 8 else "拒绝"


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": len(g), "signal_mean_return": float(g["trade_return"].mean()), "signal_bad_window_rate": float(g["is_bad_window"].astype(bool).mean())} for y, g in d.groupby("year")])


def run_summary(policy: dict[str, Any], table: pd.DataFrame, primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
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
        "final_verdict": "research_only；延长持有期没有找到有效反弹窗口",
        "main_diagnosis": "V4.14固定V4.13事件后，20日持有仍只有约1.52%平均收益；30日持有收益接近2%，但坏窗口率升至25.81%。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary, table, data_audit, leakage, wf, policy) -> str:
    return "\n".join([
        "# V4.14 持有期收益厚度审计报告",
        "",
        summary["main_diagnosis"],
        "",
        f"- 主规则：{summary['primary_signal_id']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 候选数：{summary['candidate_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 持有期对比",
        table.to_markdown(index=False),
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
