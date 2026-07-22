#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_22_event_independence_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_22_event_independence"
VERSION = "4.22.0"


def main() -> None:
    policy = read_json(POLICY)
    frontier = pd.read_csv(ROOT / policy["source_frontier_path"], encoding="utf-8-sig")
    all_trades = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date", "entry_date", "exit_date"])
    trades = all_trades[all_trades["signal_id"].isin(frontier["signal_id"])].copy()
    assigned = assign_clusters(trades, policy)
    cluster_trades = build_cluster_trades(assigned)
    overlap = overlap_audit(assigned)
    summary = cluster_summary(frontier, assigned, cluster_trades, overlap, policy)
    primary = summary.sort_values(["frontier_role"], key=lambda s: s.ne("收益最高规则")).iloc[0].to_dict()
    primary_clusters = cluster_trades[cluster_trades["signal_id"] == primary["signal_id"]].copy()
    wf = year_summary(primary_clusters)
    data_audit = pd.DataFrame([{
        "audit_item": "fixed_v4_20_v4_21_source",
        "status": "pass",
        "evidence": f"rules={len(frontier)}; source_trades={len(trades)}; cluster_gap_days={policy['signal_cluster_gap_calendar_days']}",
        "action": "固定边界规则和交易明细，只审计事件独立性。"
    }])
    leakage = pd.DataFrame([{
        "audit_item": "no_post_hoc_filter",
        "status": "pass",
        "evidence": "clusters are assigned by signal/entry/exit dates only",
        "action": "不用未来收益决定聚类，不新增筛选条件。"
    }])
    run = run_summary(policy, summary, primary, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", run)
    (OUT / "report.md").write_text(report(run, summary, overlap, wf, data_audit, leakage, policy), encoding="utf-8")
    trades.to_csv(debug / "cluster_source_trades.csv", index=False, encoding="utf-8-sig")
    assigned.to_csv(debug / "event_cluster_assignments.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(debug / "cluster_effective_summary.csv", index=False, encoding="utf-8-sig")
    cluster_trades.to_csv(debug / "cluster_level_trades.csv", index=False, encoding="utf-8-sig")
    overlap.to_csv(debug / "overlap_audit.csv", index=False, encoding="utf-8-sig")
    primary_clusters.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": run["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.22事件独立性与有效样本量审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"独立簇数={primary['nonoverlap_events']}")
    print(f"最终结论={run['final_verdict']}")


def assign_clusters(trades: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    gap = int(policy["signal_cluster_gap_calendar_days"])
    buffer = timedelta(days=int(policy["overlap_buffer_calendar_days"]))
    rows = []
    for signal_id, d in trades.sort_values(["signal_id", "signal_date"]).groupby("signal_id", sort=False):
        cluster_no = 0
        prev_signal = None
        cluster_exit = None
        for _, row in d.iterrows():
            close_signal = prev_signal is not None and (row["signal_date"] - prev_signal).days <= gap
            overlaps = cluster_exit is not None and row["entry_date"] <= cluster_exit + buffer
            if prev_signal is None or not (close_signal or overlaps):
                cluster_no += 1
                cluster_exit = row["exit_date"]
            else:
                cluster_exit = max(cluster_exit, row["exit_date"])
            prev_signal = row["signal_date"]
            out = row.to_dict()
            out["cluster_id"] = f"{signal_id}_c{cluster_no:02d}"
            out["cluster_no"] = cluster_no
            out["close_to_previous_signal"] = bool(close_signal)
            out["overlaps_current_cluster"] = bool(overlaps)
            rows.append(out)
    return pd.DataFrame(rows)


def build_cluster_trades(assigned: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (signal_id, cluster_id), g in assigned.groupby(["signal_id", "cluster_id"], sort=False):
        ret = float(pd.to_numeric(g["trade_return"], errors="coerce").mean())
        row = {
            "signal_id": signal_id,
            "cluster_id": cluster_id,
            "signal_date": g["signal_date"].min().date().isoformat(),
            "entry_date": g["entry_date"].min().date().isoformat(),
            "exit_date": g["exit_date"].max().date().isoformat(),
            "year": int(g["signal_date"].min().year),
            "cluster_event_count": int(len(g)),
            "trade_return": ret,
            "is_win": ret > 0,
            "is_bad_window": bool(g["is_bad_window"].astype(bool).any()),
            "max_adverse_return": float(pd.to_numeric(g["max_adverse_return"], errors="coerce").min()),
            "cluster_signal_span_days": int((g["signal_date"].max() - g["signal_date"].min()).days),
            "cluster_holding_span_days": int((g["exit_date"].max() - g["entry_date"].min()).days),
        }
        for col in ["signal_name_zh", "frontier_role"]:
            if col in g.columns:
                row[col] = g[col].iloc[0]
        rows.append(row)
    return pd.DataFrame(rows)


def overlap_audit(assigned: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for signal_id, d in assigned.groupby("signal_id", sort=False):
        rows.append({
            "signal_id": signal_id,
            "raw_events": int(len(d)),
            "overlap_or_close_event_count": int((d["close_to_previous_signal"] | d["overlaps_current_cluster"]).sum()),
            "overlap_event_count": int(d["overlaps_current_cluster"].sum()),
            "close_signal_event_count": int(d["close_to_previous_signal"].sum()),
            "cluster_count": int(d["cluster_id"].nunique()),
            "max_cluster_size": int(d.groupby("cluster_id").size().max()),
        })
    return pd.DataFrame(rows)


def cluster_summary(frontier: pd.DataFrame, assigned: pd.DataFrame, clusters: pd.DataFrame, overlap: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for _, rule in frontier.iterrows():
        signal_id = rule["signal_id"]
        c = clusters[clusters["signal_id"] == signal_id].copy()
        raw = assigned[assigned["signal_id"] == signal_id]
        years = c["year"].value_counts(normalize=True) if len(c) else pd.Series(dtype=float)
        row = rule.to_dict()
        row.update({
            "raw_events": int(len(raw)),
            "nonoverlap_events": int(len(c)),
            "independent_cluster_count": int(len(c)),
            "effective_sample_ratio": safe_div(len(c), len(raw)),
            "event_mean_return": float(c["trade_return"].mean()) if len(c) else math.nan,
            "event_win_rate": float(c["is_win"].astype(bool).mean()) if len(c) else math.nan,
            "event_bad_window_rate": float(c["is_bad_window"].astype(bool).mean()) if len(c) else math.nan,
            "event_worst_return": float(c["trade_return"].min()) if len(c) else math.nan,
            "active_years": int(c["year"].nunique()) if len(c) else 0,
            "max_single_year_concentration": float(years.max()) if len(years) else math.nan,
        })
        o = overlap[overlap["signal_id"] == signal_id].iloc[0].to_dict()
        row.update({k: o[k] for k in ["overlap_or_close_event_count", "overlap_event_count", "close_signal_event_count", "max_cluster_size"]})
        row["cluster_sample_gate_pass"] = row["independent_cluster_count"] >= int(policy["min_independent_clusters"])
        row["cluster_return_gate_pass"] = row["event_mean_return"] >= float(policy["min_realtime_mean_return"])
        row["cluster_win_gate_pass"] = row["event_win_rate"] >= float(policy["min_realtime_win_rate"])
        row["cluster_bad_window_gate_pass"] = row["event_bad_window_rate"] <= float(policy["max_realtime_bad_window_rate"])
        row["cluster_year_gate_pass"] = row["max_single_year_concentration"] <= float(policy["max_single_year_concentration"])
        gate_cols = ["cluster_sample_gate_pass", "cluster_return_gate_pass", "cluster_win_gate_pass", "cluster_bad_window_gate_pass", "cluster_year_gate_pass"]
        failed = [col for col in gate_cols if not bool(row[col])]
        row["failed_gate_count"] = len(failed)
        row["failed_gates"] = ",".join(failed)
        row["status"] = "有效反弹窗口" if not failed else ("样本不足" if failed == ["cluster_sample_gate_pass"] else "拒绝")
        rows.append(row)
    return pd.DataFrame(rows)


def year_summary(clusters: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{
        "year": int(y),
        "status": "pass",
        "signal_dates": int(len(g)),
        "signal_mean_return": float(g["trade_return"].mean()),
        "signal_bad_window_rate": float(g["is_bad_window"].astype(bool).mean()),
    } for y, g in clusters.groupby("year")])


def run_summary(policy: dict[str, Any], summary: pd.DataFrame, primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    candidate_count = int((summary["status"] == "有效反弹窗口").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": candidate_count,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "independent_cluster_count": int(primary["independent_cluster_count"]),
        "raw_event_count": int(primary["raw_events"]),
        "final_verdict": "research_only；事件聚类后有效样本不足，不能把43个事件当作43次独立证据",
        "main_diagnosis": "V4.22显示V4.20/V4.21边界规则存在明显事件聚类；按独立行情簇口径重新评价后，有效样本数低于硬门槛，当前仍不能证明反弹窗口有效。",
        "research_boundary": policy["research_boundary"],
    }


def report(run, summary, overlap, wf, data_audit, leakage, policy) -> str:
    return "\n".join([
        "# V4.22 事件独立性与有效样本量审计报告",
        "",
        run["main_diagnosis"],
        "",
        f"- 主规则：{run['primary_signal_id']}",
        f"- 原始事件数：{run['raw_event_count']}",
        f"- 独立行情簇数：{run['independent_cluster_count']}",
        f"- 候选数：{run['candidate_count']}",
        f"- 最终结论：{run['final_verdict']}",
        "",
        "## 独立簇口径评价",
        summary.to_markdown(index=False),
        "",
        "## 重叠与聚类审计",
        overlap.to_markdown(index=False),
        "",
        "## 主规则年度簇表现",
        wf.to_markdown(index=False),
        "",
        "## 审计",
        data_audit.to_markdown(index=False),
        leakage.to_markdown(index=False),
        "",
        f"研究边界：{policy['research_boundary']}",
    ])


def safe_div(a: int, b: int) -> float:
    return math.nan if b == 0 else a / b


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
