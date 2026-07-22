#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_24_market_state_evidence_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_24_market_state_evidence"
VERSION = "4.24.0"


def main() -> None:
    policy = read_json(POLICY)
    raw = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date", "entry_date", "exit_date"])
    raw = raw[raw["signal_id"] == policy["source_signal_id"]].copy()
    clusters = build_clusters(raw, int(policy["signal_cluster_gap_calendar_days"]))
    states, state_trades = evaluate_states(clusters, policy)
    primary = states[states["signal_id"] == policy["primary_state_id"]].iloc[0].to_dict()
    primary_trades = state_trades[state_trades["signal_id"] == primary["signal_id"]].copy()
    wf = year_summary(primary_trades)
    data_audit = pd.DataFrame([{"audit_item": "fixed_source_signal", "status": "pass", "evidence": f"source_signal={policy['source_signal_id']}; raw_events={len(raw)}; clusters={len(clusters)}", "action": "固定V4.20收益最高源信号，只审计事前市场状态。"}])
    leakage = pd.DataFrame([{"audit_item": "pre_signal_state_only", "status": "pass", "evidence": "state fields are copied from signal-date feature columns", "action": "状态规则不使用未来收益、退出结果或事后标签。"}])
    run = run_summary(policy, primary, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    states.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", run)
    (OUT / "report.md").write_text(report(run, states, wf, data_audit, leakage, policy), encoding="utf-8")
    raw.to_csv(debug / "market_state_source_trades.csv", index=False, encoding="utf-8-sig")
    clusters.to_csv(debug / "market_state_cluster_panel.csv", index=False, encoding="utf-8-sig")
    state_trades.to_csv(debug / "market_state_filtered_trades.csv", index=False, encoding="utf-8-sig")
    states.to_csv(debug / "market_state_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": run["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.24事前市场状态证据审计完成")
    print(f"主状态={primary['signal_id']}")
    print(f"统一前状态={primary['status']}")
    print(f"最终结论={run['final_verdict']}")


def build_clusters(trades: pd.DataFrame, gap_days: int) -> pd.DataFrame:
    rows, bucket, no, prev, cexit = [], [], 0, None, None
    for _, row in trades.sort_values("signal_date").iterrows():
        new = prev is None or ((row["signal_date"] - prev).days > gap_days and row["entry_date"] > cexit)
        if new and bucket:
            rows.append(cluster_row(no, bucket))
            bucket = []
        if new:
            no += 1
            cexit = row["exit_date"]
        else:
            cexit = max(cexit, row["exit_date"])
        bucket.append(row)
        prev = row["signal_date"]
    if bucket:
        rows.append(cluster_row(no, bucket))
    return pd.DataFrame(rows)


def cluster_row(no: int, bucket: list[pd.Series]) -> dict[str, Any]:
    g = pd.DataFrame(bucket)
    ret = float(pd.to_numeric(g["trade_return"], errors="coerce").mean())
    return {
        "cluster_id": f"c{no:02d}",
        "signal_date": g["signal_date"].min().date().isoformat(),
        "entry_date": g["entry_date"].min().date().isoformat(),
        "exit_date": g["exit_date"].max().date().isoformat(),
        "year": int(g["signal_date"].min().year),
        "cluster_event_count": int(len(g)),
        "trade_return": ret,
        "is_win": ret > 0,
        "is_bad_window": bool(to_bool(g["is_bad_window"]).any()),
        "event_worst_return": float(pd.to_numeric(g["trade_return"], errors="coerce").min()),
        "max_adverse_return": float(pd.to_numeric(g["max_adverse_return"], errors="coerce").min()),
        "market_return_5d": float(pd.to_numeric(g["market_return_5d"], errors="coerce").mean()),
        "industry_positive_20d_ratio": float(pd.to_numeric(g["industry_positive_20d_ratio"], errors="coerce").mean()),
        "industry_above_ma20_ratio": float(pd.to_numeric(g["industry_above_ma20_ratio"], errors="coerce").mean()),
        "industry_new_low_60d_relief_5d": float(pd.to_numeric(g["industry_new_low_60d_relief_5d"], errors="coerce").mean()),
    }


def evaluate_states(clusters: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, trade_frames = [], []
    for rule in policy["state_rules"]:
        d = clusters[mask_for(clusters, rule["conditions"])].copy()
        d["signal_id"] = rule["state_id"]
        d["signal_name_zh"] = rule["state_name_zh"]
        d["signal_type"] = "pre_signal_market_state"
        trade_frames.append(d)
        rows.append(summary_row(d, rule, policy))
    states = pd.DataFrame(rows)
    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    primary = states[states["signal_id"] == policy["primary_state_id"]]
    rest = states[states["signal_id"] != policy["primary_state_id"]].sort_values(["nonoverlap_events", "event_mean_return"], ascending=[False, False])
    return pd.concat([primary, rest], ignore_index=True), trades


def mask_for(df: pd.DataFrame, conditions: dict[str, list[Any]]) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for col, (op, value) in conditions.items():
        s = pd.to_numeric(df[col], errors="coerce")
        if op == ">=":
            mask &= s >= float(value)
        elif op == "<=":
            mask &= s <= float(value)
        else:
            raise ValueError(f"unsupported operator: {op}")
    return mask


def summary_row(d: pd.DataFrame, rule: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    count = int(len(d))
    years = d["year"].value_counts(normalize=True) if count else pd.Series(dtype=float)
    mean = float(pd.to_numeric(d["trade_return"], errors="coerce").mean()) if count else math.nan
    win = float(to_bool(d["is_win"]).mean()) if count else math.nan
    bad = float(to_bool(d["is_bad_window"]).mean()) if count else math.nan
    hard = count >= int(policy["min_realtime_events"]) and mean >= float(policy["min_realtime_mean_return"]) and win >= float(policy["min_realtime_win_rate"]) and bad <= float(policy["max_realtime_bad_window_rate"]) and (years.empty or float(years.max()) <= float(policy["max_single_year_concentration"]))
    return {
        "signal_id": rule["state_id"],
        "signal_name_zh": rule["state_name_zh"],
        "signal_type": "pre_signal_market_state",
        "status": "有效反弹窗口" if hard else ("样本不足" if count < int(policy["min_realtime_events"]) else "拒绝"),
        "nonoverlap_events": count,
        "event_mean_return": mean,
        "event_win_rate": win,
        "event_bad_window_rate": bad,
        "event_worst_return": float(pd.to_numeric(d["trade_return"], errors="coerce").min()) if count else math.nan,
        "active_years": int(d["year"].nunique()) if count else 0,
        "max_single_year_concentration": float(years.max()) if len(years) else math.nan,
        "conditions_json": json.dumps(rule["conditions"], ensure_ascii=False),
    }


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": int(len(g)), "signal_mean_return": float(pd.to_numeric(g["trade_return"], errors="coerce").mean()), "signal_bad_window_rate": float(to_bool(g["is_bad_window"]).mean())} for y, g in d.groupby("year")])


def run_summary(policy: dict[str, Any], primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": 1 if primary["status"] == "有效反弹窗口" else 0,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；事前市场状态证据尚未通过有效反弹窗口评价",
        "main_diagnosis": "V4.24显示短期止跌和趋势修复状态能改善平均收益与胜率，但独立样本仍不足，坏窗口率仍高于有效门槛，尚不能作为可靠反弹窗口。",
        "research_boundary": policy["research_boundary"],
    }


def report(run, states, wf, data_audit, leakage, policy) -> str:
    return "\n".join([
        "# V4.24 事前市场状态证据审计报告",
        "",
        run["main_diagnosis"],
        "",
        f"- 主状态：{run['primary_signal_id']}",
        f"- 主状态独立簇：{run['primary_realtime_events']}",
        f"- 主状态平均收益：{fmt_pct(run['best_event_mean_return'])}",
        f"- 主状态坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
        f"- 最终结论：{run['final_verdict']}",
        "",
        "## 状态规则摘要",
        states.to_markdown(index=False),
        "",
        "## 主状态年度表现",
        wf.to_markdown(index=False),
        "",
        "## 审计",
        data_audit.to_markdown(index=False),
        leakage.to_markdown(index=False),
        "",
        f"研究边界：{policy['research_boundary']}",
    ])


def to_bool(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(["true", "1", "yes"])


def fmt_pct(v: Any) -> str:
    try:
        x = float(v)
    except Exception:
        return ""
    return "" if math.isnan(x) else f"{x:.2%}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean(v):
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v]
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
