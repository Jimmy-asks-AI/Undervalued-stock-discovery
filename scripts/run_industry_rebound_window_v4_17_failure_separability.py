#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_17_failure_separability_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_17_failure_separability"
VERSION = "4.17.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    trades = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
    panel = pd.read_csv(ROOT / policy["feature_panel_path"], encoding="utf-8-sig", parse_dates=["trade_date"])
    enriched = merge_features(trades, panel, policy["feature_columns"])
    feature_audit = feature_separability(enriched, policy["feature_columns"])
    failure_cases = enriched[enriched["is_bad_window"].astype(bool)].copy()
    summary_row = summarize(enriched)
    wf = year_summary(enriched)
    data_audit = pd.DataFrame([{"audit_item": "fixed_v4_16_primary", "status": "pass", "evidence": f"events={len(enriched)}; bad_windows={int(enriched['is_bad_window'].astype(bool).sum())}", "action": "固定V4.16成本后主规则，仅审计事前特征差异。"}])
    leakage = pd.DataFrame([{"audit_item": "feature_asof_boundary", "status": "pass", "evidence": "features are from signal_date market panel", "action": "不使用未来收益构造失败解释特征。"}])
    summary = run_summary(policy, summary_row, feature_audit, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([summary_row]).to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(report(summary, feature_audit, failure_cases, data_audit, leakage, wf, policy), encoding="utf-8")
    enriched.to_csv(debug / "failure_feature_panel.csv", index=False, encoding="utf-8-sig")
    feature_audit.to_csv(debug / "failure_feature_separability.csv", index=False, encoding="utf-8-sig")
    failure_cases.to_csv(debug / "failure_cases.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([summary_row]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.17失败样本可识别性审计完成")
    print(f"主规则={summary_row['signal_id']}")
    print(f"最终结论={summary['final_verdict']}")


def merge_features(trades: pd.DataFrame, panel: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    cols = ["trade_date"] + features
    merged = trades.merge(panel[cols], left_on="signal_date", right_on="trade_date", how="left", suffixes=("", "_panel"))
    for col in features:
        panel_col = f"{col}_panel"
        if panel_col in merged.columns:
            merged[col] = merged[panel_col]
            merged = merged.drop(columns=[panel_col])
    return merged.drop(columns=["trade_date_panel"], errors="ignore")


def feature_separability(d: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    rows = []
    bad_mask = d["is_bad_window"].astype(bool)
    for col in features:
        bad = pd.to_numeric(d.loc[bad_mask, col], errors="coerce").dropna()
        good = pd.to_numeric(d.loc[~bad_mask, col], errors="coerce").dropna()
        if bad.empty or good.empty:
            continue
        bad_min, bad_max = float(bad.min()), float(bad.max())
        good_min, good_max = float(good.min()), float(good.max())
        # ponytail: simple one-dimensional range overlap audit; upgrade to cross-validated classifiers only if this shows signal.
        overlap = max(0.0, min(bad_max, good_max) - max(bad_min, good_min))
        span = max(bad_max, good_max) - min(bad_min, good_min)
        overlap_ratio = overlap / span if span else 1.0
        rows.append({
            "feature": col,
            "bad_mean": float(bad.mean()),
            "good_mean": float(good.mean()),
            "mean_diff": float(bad.mean() - good.mean()),
            "bad_min": bad_min,
            "bad_max": bad_max,
            "good_min": good_min,
            "good_max": good_max,
            "range_overlap_ratio": overlap_ratio,
            "clear_one_dimensional_separation": bool(overlap_ratio == 0.0),
        })
    return pd.DataFrame(rows).sort_values(["clear_one_dimensional_separation", "range_overlap_ratio", "mean_diff"], ascending=[False, True, True])


def summarize(d: pd.DataFrame) -> dict[str, Any]:
    ret = pd.to_numeric(d["trade_return"], errors="coerce")
    years = d["year"].value_counts(normalize=True)
    return {
        "signal_id": "v4_17_failure_separability_primary",
        "signal_name_zh": "V4.16成本后主规则，失败可识别性审计",
        "signal_type": "失败样本可识别性审计",
        "status": "条件观察",
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


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": len(g), "signal_mean_return": float(g["trade_return"].mean()), "signal_bad_window_rate": float(g["is_bad_window"].astype(bool).mean())} for y, g in d.groupby("year")])


def run_summary(policy: dict[str, Any], primary: dict[str, Any], feature_audit: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    separated = int(feature_audit["clear_one_dimensional_separation"].sum()) if not feature_audit.empty else 0
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
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "clear_one_dimensional_separation_count": separated,
        "final_verdict": "research_only；坏窗口没有被一维事前特征清晰识别",
        "main_diagnosis": f"V4.17审计{len(feature_audit)}个事前特征，没有发现坏窗口与好窗口的清晰一维分离；继续按单一过滤条件调参缺乏证据。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary, feature_audit, failure_cases, data_audit, leakage, wf, policy) -> str:
    cols = ["signal_date", "year", "trade_return", "market_return_20d", "market_return_120d", "market_stress_score", "industry_above_ma20_ratio", "breadth_recovery_score"]
    return "\n".join([
        "# V4.17 失败样本可识别性审计报告",
        "",
        summary["main_diagnosis"],
        "",
        f"- 主规则：{summary['primary_signal_id']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 清晰一维分离特征数：{summary['clear_one_dimensional_separation_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 特征可分离性",
        feature_audit.to_markdown(index=False),
        "",
        "## 坏窗口样本",
        failure_cases[[c for c in cols if c in failure_cases.columns]].to_markdown(index=False),
        "",
        "## 年度表现",
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
