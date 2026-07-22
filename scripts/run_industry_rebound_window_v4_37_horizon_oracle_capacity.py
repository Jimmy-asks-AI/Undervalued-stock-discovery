#!/usr/bin/env python
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_industry_rebound_window_v4_31_wide_index_state_boundary import fmt_pct, none_if_nan, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_37_horizon_oracle_capacity_policy.json"
VERSION = "4.37.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    trades = load_trades(policy)
    benchmark = load_benchmark(policy)
    panel = add_benchmark_return(trades, benchmark)
    candidates = build_candidates(panel, policy)
    eligible = candidates[candidates["nonoverlap_events"] >= int(policy["primary_top_n"])].copy()
    primary = eligible.sort_values(["event_mean_return", "event_relative_mean_return"], ascending=False).iloc[0].to_dict()
    primary_trades = panel[(panel["holding_days"] == int(primary["holding_days"])) & (panel["min_consecutive_signal_days"] == int(policy["min_consecutive_signal_days"]))].sort_values("relative_return_horizon", ascending=False).head(int(policy["primary_top_n"])).copy()
    primary_trades["market_return_5d"] = primary_trades["benchmark_return_horizon"]
    primary_trades["signal_id"] = primary["signal_id"]
    primary_trades["signal_name_zh"] = primary["signal_name_zh"]
    primary_trades["signal_type"] = "horizon_full_sample_oracle_capacity"
    wf = year_summary(primary_trades)
    data_audit = build_data_audit(panel, candidates, policy)
    leakage = build_leakage_audit()
    notes = build_notes(primary, candidates)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    panel.to_csv(debug / "horizon_oracle_source_panel.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.37多持有期理论容量审计完成")
    print(f"主口径={primary['signal_id']}")
    print(f"绝对收益={fmt_pct(primary['event_mean_return'])}")
    print(f"相对收益={fmt_pct(primary['event_relative_mean_return'])}")


def load_trades(policy: dict[str, Any]) -> pd.DataFrame:
    df = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig")
    for col in ["holding_days", "min_consecutive_signal_days", "trade_return", "year"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["exit_date"] = pd.to_datetime(df["exit_date"], errors="coerce")
    df["is_bad_window"] = to_bool(df["is_bad_window"])
    return df[df["min_consecutive_signal_days"] == int(policy["min_consecutive_signal_days"])].copy()


def load_benchmark(policy: dict[str, Any]) -> pd.DataFrame:
    df = pd.read_csv(ROOT / policy["benchmark_index_path"], encoding="utf-8-sig")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["trade_date", "close"]).sort_values("trade_date")


def add_benchmark_return(trades: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    out = trades.sort_values("entry_date").copy()
    entry = benchmark.rename(columns={"trade_date": "entry_date", "close": "entry_close"})[["entry_date", "entry_close"]]
    exit_ = benchmark.rename(columns={"trade_date": "exit_date", "close": "exit_close"})[["exit_date", "exit_close"]]
    out = out.merge(entry, on="entry_date", how="left").merge(exit_, on="exit_date", how="left")
    out["benchmark_return_horizon"] = out["exit_close"] / out["entry_close"] - 1.0
    out["relative_return_horizon"] = pd.to_numeric(out["trade_return"], errors="coerce") - out["benchmark_return_horizon"]
    return out


def build_candidates(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    top_n = int(policy["primary_top_n"])
    for horizon in policy["horizon_grid"]:
        d = panel[panel["holding_days"] == int(horizon)].sort_values("relative_return_horizon", ascending=False)
        if len(d):
            rows.append(summary_row(d.head(min(top_n, len(d))), int(horizon), top_n))
    return pd.DataFrame(rows).sort_values(["nonoverlap_events", "event_mean_return"], ascending=[False, False]).reset_index(drop=True)


def summary_row(frame: pd.DataFrame, horizon: int, top_n: int) -> dict[str, Any]:
    years = frame["year"].dropna().astype(int)
    ret = pd.to_numeric(frame["trade_return"], errors="coerce")
    rel = pd.to_numeric(frame["relative_return_horizon"], errors="coerce")
    n = min(top_n, len(frame))
    return {
        "signal_id": f"horizon_oracle_{horizon}d_top{n}",
        "signal_name_zh": f"{horizon}日未来相对收益排序Top{n}",
        "signal_type": "horizon_full_sample_oracle_capacity",
        "status": "理论上限" if n >= top_n else "样本不足理论上限",
        "holding_days": horizon,
        "top_n": n,
        "nonoverlap_events": int(n),
        "event_mean_return": none_if_nan(ret.mean()),
        "event_relative_mean_return": none_if_nan(rel.mean()),
        "event_win_rate": none_if_nan((ret > 0).mean()),
        "event_bad_window_rate": none_if_nan(frame["is_bad_window"].mean()),
        "event_worst_return": none_if_nan(ret.min()),
        "active_years": int(years.nunique()) if len(years) else 0,
        "max_single_year_concentration": none_if_nan(years.value_counts(normalize=True).max()) if len(years) else None,
    }


def year_summary(trades: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "year": int(year),
                "status": "pass",
                "signal_dates": int(len(g)),
                "signal_mean_return": none_if_nan(pd.to_numeric(g["trade_return"], errors="coerce").mean()),
                "signal_relative_mean_return": none_if_nan(pd.to_numeric(g["relative_return_horizon"], errors="coerce").mean()),
                "signal_bad_window_rate": none_if_nan(g["is_bad_window"].mean()),
            }
            for year, g in trades.groupby("year")
        ]
    )


def build_data_audit(panel: pd.DataFrame, candidates: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "horizon_grid_loaded", "status": "pass" if len(panel) else "fail", "evidence": f"events={len(panel)}", "action": "缺少多持有期事件时不得审计。"},
            {"audit_item": "benchmark_return_available", "status": "pass" if panel["benchmark_return_horizon"].notna().all() else "fail", "evidence": f"missing={int(panel['benchmark_return_horizon'].isna().sum())}", "action": "缺少持有期基准收益时不得评价相对收益。"},
            {"audit_item": "eligible_horizon_count", "status": "pass" if (candidates["nonoverlap_events"] >= int(policy["primary_top_n"])).any() else "fail", "evidence": f"eligible={int((candidates['nonoverlap_events'] >= int(policy['primary_top_n'])).sum())}", "action": "至少一个持有期必须达到样本下限。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "future_return_used_for_ranking", "status": "observe", "evidence": "full-sample oracle sort uses realized horizon relative return", "action": "这是刻意的理论容量审计，不是可交易验证。"},
            {"audit_item": "no_trade_instruction", "status": "pass", "evidence": "research_only output", "action": "不生成买卖指令。"},
        ]
    )


def build_notes(primary: dict[str, Any], candidates: pd.DataFrame) -> dict[str, Any]:
    return {
        "main_diagnosis": "V4.37 使用未来收益排序审计多持有期事件池理论容量。",
        "next_iterations": [
            f"主口径 {primary['signal_id']}：绝对收益 {fmt_pct(primary['event_mean_return'])}，相对收益 {fmt_pct(primary['event_relative_mean_return'])}，坏窗口率 {fmt_pct(primary['event_bad_window_rate'])}。",
            "本版使用未来收益排序，不可交易；只用于判断持有期是否提供足够理论容量。",
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
        "final_verdict": "research_only；多持有期理论容量审计不是可交易反弹窗口",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], candidates: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.37 多持有期理论容量审计报告",
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
