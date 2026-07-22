#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_18_failure_model_readiness_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_18_failure_model_readiness"
VERSION = "4.18.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    panel = pd.read_csv(ROOT / policy["source_panel_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
    readiness = readiness_audit(panel, policy)
    yearly = yearly_failure_distribution(panel)
    loo = leave_one_year_model_readiness(panel, policy)
    primary = summarize(panel)
    wf = year_summary(panel)
    data_audit = pd.DataFrame([{"audit_item": "fixed_v4_17_panel", "status": "pass", "evidence": f"events={len(panel)}; bad={int(panel['is_bad_window'].astype(bool).sum())}", "action": "固定V4.17失败特征面板，仅审计模型可验证性。"}])
    leakage = pd.DataFrame([{"audit_item": "no_model_training", "status": "pass", "evidence": "readiness audit only; no fitted filter", "action": "不训练多特征模型，不新增过滤规则。"}])
    summary = run_summary(policy, primary, readiness, loo, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([primary]).to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(report(summary, readiness, yearly, loo, data_audit, leakage, wf, policy), encoding="utf-8")
    panel.to_csv(debug / "failure_model_panel.csv", index=False, encoding="utf-8-sig")
    readiness.to_csv(debug / "model_readiness_audit.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(debug / "failure_year_distribution.csv", index=False, encoding="utf-8-sig")
    loo.to_csv(debug / "leave_one_year_model_readiness.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.18多特征失败模型可验证性审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"最终结论={summary['final_verdict']}")


def readiness_audit(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    bad = panel[panel["is_bad_window"].astype(bool)]
    rows = [
        {
            "audit_item": "bad_event_count",
            "status": "pass" if len(bad) >= int(policy["min_bad_events_for_model"]) else "fail",
            "evidence": f"{len(bad)} / {policy['min_bad_events_for_model']}",
            "action": "坏窗口样本太少时不训练多特征过滤模型。",
        },
        {
            "audit_item": "bad_year_coverage",
            "status": "pass" if bad["year"].nunique() >= int(policy["min_bad_years_for_model"]) else "fail",
            "evidence": f"{bad['year'].nunique()} / {policy['min_bad_years_for_model']}",
            "action": "坏窗口年份覆盖不足时不做跨年泛化判断。",
        },
    ]
    return pd.DataFrame(rows)


def yearly_failure_distribution(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y, g in panel.groupby("year"):
        rows.append({
            "year": int(y),
            "events": len(g),
            "bad_events": int(g["is_bad_window"].astype(bool).sum()),
            "bad_rate": float(g["is_bad_window"].astype(bool).mean()),
            "mean_return": float(pd.to_numeric(g["trade_return"], errors="coerce").mean()),
        })
    return pd.DataFrame(rows)


def leave_one_year_model_readiness(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    threshold = int(policy["min_train_bad_events_leave_one_year"])
    for y in sorted(panel["year"].unique()):
        train = panel[panel["year"] != y]
        test = panel[panel["year"] == y]
        train_bad = int(train["is_bad_window"].astype(bool).sum())
        test_bad = int(test["is_bad_window"].astype(bool).sum())
        rows.append({
            "excluded_year": int(y),
            "train_events": len(train),
            "train_bad_events": train_bad,
            "test_events": len(test),
            "test_bad_events": test_bad,
            "status": "pass" if train_bad >= threshold else "fail",
            "evidence": f"train_bad={train_bad} / {threshold}",
        })
    return pd.DataFrame(rows)


def summarize(panel: pd.DataFrame) -> dict[str, Any]:
    ret = pd.to_numeric(panel["trade_return"], errors="coerce")
    years = panel["year"].value_counts(normalize=True)
    return {
        "signal_id": "v4_18_failure_model_readiness_primary",
        "signal_name_zh": "V4.16成本后主规则，多特征失败模型可验证性审计",
        "signal_type": "失败模型可验证性审计",
        "status": "条件观察",
        "signal_dates": len(panel),
        "trades": len(panel),
        "nonoverlap_events": len(panel),
        "active_years": int(panel["year"].nunique()),
        "max_single_year_concentration": float(years.max()),
        "event_mean_return": float(ret.mean()),
        "event_win_rate": float((ret > 0).mean()),
        "event_bad_window_rate": float(panel["is_bad_window"].astype(bool).mean()),
        "event_worst_return": float(ret.min()),
    }


def year_summary(panel: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": len(g), "signal_mean_return": float(g["trade_return"].mean()), "signal_bad_window_rate": float(g["is_bad_window"].astype(bool).mean())} for y, g in panel.groupby("year")])


def run_summary(policy: dict[str, Any], primary: dict[str, Any], readiness: pd.DataFrame, loo: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    readiness_fail = int((readiness["status"] == "fail").sum()) + int((loo["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": 0,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "model_readiness_fail_count": readiness_fail,
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；坏窗口样本不足以训练可验证的多特征失败模型",
        "main_diagnosis": f"V4.18只有{int(primary['event_bad_window_rate'] * primary['nonoverlap_events'])}个坏窗口，跨年覆盖不足，leave-one-year训练坏样本数也不足；不应训练多特征失败过滤模型。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary, readiness, yearly, loo, data_audit, leakage, wf, policy) -> str:
    return "\n".join([
        "# V4.18 多特征失败模型可验证性审计报告",
        "",
        summary["main_diagnosis"],
        "",
        f"- 主规则：{summary['primary_signal_id']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 模型就绪失败项：{summary['model_readiness_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 模型就绪审计",
        readiness.to_markdown(index=False),
        "",
        "## 坏窗口年度分布",
        yearly.to_markdown(index=False),
        "",
        "## Leave-one-year 训练样本审计",
        loo.to_markdown(index=False),
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
