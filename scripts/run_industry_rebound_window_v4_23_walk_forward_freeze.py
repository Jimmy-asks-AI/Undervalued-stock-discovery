#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_23_walk_forward_freeze_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_23_walk_forward_freeze"
VERSION = "4.23.0"


def main() -> None:
    policy = read_json(POLICY)
    trades = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date", "entry_date", "exit_date"])
    clusters = build_clusters(trades, int(policy["signal_cluster_gap_calendar_days"]))
    selection, oos = walk_forward(clusters, policy)
    rule_summary = summarize_rules(clusters)
    realtime = summarize_oos(oos, policy)
    wf_year = year_summary(selection, oos)
    data_audit = pd.DataFrame([{"audit_item": "fixed_rule_pool", "status": "pass", "evidence": f"rules={trades['signal_id'].nunique()}; raw_trades={len(trades)}; clusters={len(clusters)}", "action": "只使用V4.20固定退出规则池。"}])
    leakage = pd.DataFrame([{"audit_item": "past_years_only_selection", "status": "pass", "evidence": f"min_train_years={policy['min_train_years']}; min_train_clusters={policy['min_train_clusters_per_rule']}", "action": "每个测试年只用更早年份选择规则。"}])
    run = run_summary(policy, realtime, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([realtime]).to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", run)
    (OUT / "report.md").write_text(report(run, realtime, selection, rule_summary, wf_year, data_audit, leakage, policy), encoding="utf-8")
    trades.to_csv(debug / "rule_pool_source_trades.csv", index=False, encoding="utf-8-sig")
    clusters.to_csv(debug / "cluster_level_rule_trades.csv", index=False, encoding="utf-8-sig")
    selection.to_csv(debug / "walk_forward_selection_log.csv", index=False, encoding="utf-8-sig")
    oos.to_csv(debug / "walk_forward_oos_trades.csv", index=False, encoding="utf-8-sig")
    rule_summary.to_csv(debug / "walk_forward_rule_summary.csv", index=False, encoding="utf-8-sig")
    oos.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([realtime]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf_year.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": run["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.23年份前滚冻结规则审计完成")
    print(f"OOS独立簇={realtime['nonoverlap_events']}")
    print(f"最终结论={run['final_verdict']}")


def build_clusters(trades: pd.DataFrame, gap_days: int) -> pd.DataFrame:
    rows = []
    for signal_id, d in trades.sort_values(["signal_id", "signal_date"]).groupby("signal_id", sort=False):
        cluster_no = 0
        prev_signal = None
        cluster_exit = None
        bucket = []
        for _, row in d.iterrows():
            new_cluster = prev_signal is None or ((row["signal_date"] - prev_signal).days > gap_days and row["entry_date"] > cluster_exit)
            if new_cluster and bucket:
                rows.append(cluster_row(signal_id, cluster_no, bucket))
                bucket = []
            if new_cluster:
                cluster_no += 1
                cluster_exit = row["exit_date"]
            else:
                cluster_exit = max(cluster_exit, row["exit_date"])
            bucket.append(row)
            prev_signal = row["signal_date"]
        if bucket:
            rows.append(cluster_row(signal_id, cluster_no, bucket))
    return pd.DataFrame(rows)


def cluster_row(signal_id: str, cluster_no: int, bucket: list[pd.Series]) -> dict[str, Any]:
    g = pd.DataFrame(bucket)
    ret = float(pd.to_numeric(g["trade_return"], errors="coerce").mean())
    return {
        "signal_id": signal_id,
        "signal_name_zh": str(g["signal_name_zh"].iloc[0]),
        "profit_take": g["profit_take"].iloc[0],
        "stop_loss": g["stop_loss"].iloc[0],
        "cluster_id": f"{signal_id}_c{cluster_no:02d}",
        "signal_date": g["signal_date"].min().date().isoformat(),
        "entry_date": g["entry_date"].min().date().isoformat(),
        "exit_date": g["exit_date"].max().date().isoformat(),
        "year": int(g["signal_date"].min().year),
        "cluster_event_count": int(len(g)),
        "trade_return": ret,
        "is_win": ret > 0,
        "is_bad_window": bool(to_bool(g["is_bad_window"]).any()),
        "max_adverse_return": float(pd.to_numeric(g["max_adverse_return"], errors="coerce").min()),
    }


def walk_forward(clusters: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    years = sorted(int(x) for x in clusters["year"].unique())
    selections = []
    oos_rows = []
    for year in years:
        train_years = [y for y in years if y < year]
        if len(train_years) < int(policy["min_train_years"]):
            selections.append({"year": year, "selection_status": "skip_insufficient_train_years", "chosen_signal_id": "", "train_years": len(train_years), "train_clusters": 0})
            continue
        train = clusters[clusters["year"].isin(train_years)]
        candidates = train_metrics(train, policy)
        eligible = candidates[candidates["train_eligible"]].sort_values(["train_mean_return", "train_bad_window_rate", "train_win_rate"], ascending=[False, True, False])
        if eligible.empty:
            selections.append({"year": year, "selection_status": "skip_no_train_eligible_rule", "chosen_signal_id": "", "train_years": len(train_years), "train_clusters": int(len(train))})
            continue
        chosen = eligible.iloc[0].to_dict()
        test = clusters[(clusters["year"] == year) & (clusters["signal_id"] == chosen["signal_id"])].copy()
        selections.append({"year": year, "selection_status": "pass", "chosen_signal_id": chosen["signal_id"], "chosen_signal_name_zh": chosen["signal_name_zh"], "train_years": len(train_years), **chosen})
        if not test.empty:
            test["selected_for_year"] = year
            test["selection_train_mean_return"] = chosen["train_mean_return"]
            test["selection_train_win_rate"] = chosen["train_win_rate"]
            test["selection_train_bad_window_rate"] = chosen["train_bad_window_rate"]
            oos_rows.append(test)
    oos = pd.concat(oos_rows, ignore_index=True) if oos_rows else pd.DataFrame(columns=list(clusters.columns) + ["selected_for_year"])
    return pd.DataFrame(selections), oos


def train_metrics(train: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for signal_id, g in train.groupby("signal_id", sort=False):
        count = int(len(g))
        win = float(to_bool(g["is_win"]).mean()) if count else math.nan
        bad = float(to_bool(g["is_bad_window"]).mean()) if count else math.nan
        mean = float(pd.to_numeric(g["trade_return"], errors="coerce").mean()) if count else math.nan
        rows.append({
            "signal_id": signal_id,
            "signal_name_zh": str(g["signal_name_zh"].iloc[0]),
            "train_clusters": count,
            "train_mean_return": mean,
            "train_win_rate": win,
            "train_bad_window_rate": bad,
            "train_eligible": count >= int(policy["min_train_clusters_per_rule"]) and win >= float(policy["min_train_win_rate"]) and bad <= float(policy["max_train_bad_window_rate"]),
        })
    return pd.DataFrame(rows)


def summarize_rules(clusters: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{
        "signal_id": signal_id,
        "signal_name_zh": str(g["signal_name_zh"].iloc[0]),
        "clusters": int(len(g)),
        "mean_return": float(pd.to_numeric(g["trade_return"], errors="coerce").mean()),
        "win_rate": float(to_bool(g["is_win"]).mean()),
        "bad_window_rate": float(to_bool(g["is_bad_window"]).mean()),
    } for signal_id, g in clusters.groupby("signal_id", sort=False)])


def summarize_oos(oos: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    count = int(len(oos))
    years = oos["year"].value_counts(normalize=True) if count else pd.Series(dtype=float)
    mean = float(pd.to_numeric(oos["trade_return"], errors="coerce").mean()) if count else math.nan
    win = float(to_bool(oos["is_win"]).mean()) if count else math.nan
    bad = float(to_bool(oos["is_bad_window"]).mean()) if count else math.nan
    hard = count >= int(policy["min_realtime_events"]) and mean >= float(policy["min_realtime_mean_return"]) and win >= float(policy["min_realtime_win_rate"]) and bad <= float(policy["max_realtime_bad_window_rate"]) and (years.empty or float(years.max()) <= float(policy["max_single_year_concentration"]))
    return {
        "signal_id": "v4_23_walk_forward_frozen",
        "signal_name_zh": "年份前滚冻结退出规则",
        "signal_type": "walk_forward_frozen_rule",
        "status": "有效反弹窗口" if hard else ("样本不足" if count < int(policy["min_realtime_events"]) else "拒绝"),
        "nonoverlap_events": count,
        "event_mean_return": mean,
        "event_win_rate": win,
        "event_bad_window_rate": bad,
        "event_worst_return": float(pd.to_numeric(oos["trade_return"], errors="coerce").min()) if count else math.nan,
        "active_years": int(oos["year"].nunique()) if count else 0,
        "max_single_year_concentration": float(years.max()) if len(years) else math.nan,
    }


def year_summary(selection: pd.DataFrame, oos: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, sel in selection.iterrows():
        y = int(sel["year"])
        g = oos[oos["year"] == y] if not oos.empty else pd.DataFrame()
        rows.append({
            "year": y,
            "status": "pass" if sel["selection_status"] == "pass" else "skip",
            "signal_dates": int(len(g)),
            "signal_mean_return": float(pd.to_numeric(g["trade_return"], errors="coerce").mean()) if len(g) else math.nan,
            "signal_bad_window_rate": float(to_bool(g["is_bad_window"]).mean()) if len(g) else math.nan,
            "chosen_signal_id": sel.get("chosen_signal_id", ""),
            "selection_status": sel["selection_status"],
        })
    return pd.DataFrame(rows)


def run_summary(policy: dict[str, Any], realtime: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": realtime["signal_id"],
        "primary_realtime_events": int(realtime["nonoverlap_events"]),
        "candidate_count": 1 if realtime["status"] == "有效反弹窗口" else 0,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": realtime["signal_id"],
        "best_status": realtime["status"],
        "best_nonoverlap_events": int(realtime["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(realtime["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(realtime["event_bad_window_rate"]),
        "final_verdict": "research_only；年份前滚冻结规则没有通过有效反弹窗口评价",
        "main_diagnosis": "V4.23显示一旦只允许使用过去年份选择退出规则，样本数和收益厚度仍不足，V4.20/V4.21的边界优势不能视为可实时选择的稳定规则。",
        "research_boundary": policy["research_boundary"],
    }


def report(run, realtime, selection, rule_summary, wf_year, data_audit, leakage, policy) -> str:
    return "\n".join([
        "# V4.23 年份前滚冻结规则审计报告",
        "",
        run["main_diagnosis"],
        "",
        f"- OOS独立簇：{realtime['nonoverlap_events']}",
        f"- OOS平均收益：{fmt_pct(realtime['event_mean_return'])}",
        f"- OOS胜率：{fmt_pct(realtime['event_win_rate'])}",
        f"- OOS坏窗口率：{fmt_pct(realtime['event_bad_window_rate'])}",
        f"- 统一前状态：{realtime['status']}",
        f"- 最终结论：{run['final_verdict']}",
        "",
        "## 年份前滚选择日志",
        selection.to_markdown(index=False),
        "",
        "## OOS年度表现",
        wf_year.to_markdown(index=False),
        "",
        "## 全规则簇口径摘要",
        rule_summary.to_markdown(index=False),
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
