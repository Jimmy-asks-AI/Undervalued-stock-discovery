#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_leader_robust_grid_v4_80 as v480


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "outputs" / "audit" / "rebound_leader_confirmation_filter_audit_v5_14" / "debug" / "confirmation_filter_opportunity_set.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_rolling_quarantine_audit_v5_18"
DEBUG = OUT / "debug"
FEATURE = "early_beta_score"
TOP_N = 5
CONFIRM_DAYS = 5
FILTER_COLUMN = "filter_no_severe_early_selloff"
RULES = {
    "baseline_no_quarantine": 999,
    "quarantine_after_1_prior_loss": 1,
    "quarantine_after_2_prior_losses": 2,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.18 rolling failure quarantine audit.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    panel = pd.read_csv(PANEL, encoding="utf-8-sig", dtype={"industry_code": str})
    events, selected = evaluate(panel)
    results = summarize(events)
    summary = build_summary(results)
    write_outputs(summary, panel, events, selected, results)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def evaluate(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    panel = panel[(panel["confirm_days"].eq(CONFIRM_DAYS)) & (panel[FILTER_COLUMN].astype(bool))].copy()
    event_keys = ["signal_date", "entry_date", "confirm_date", "exit_date"]
    grouped = [(keys, event.copy()) for keys, event in panel.groupby(event_keys, sort=True)]
    rows = []
    selected_rows = []
    for rule_name, loss_threshold in RULES.items():
        ledger = pd.DataFrame(columns=["industry_code", "known_after", "selected_relative_return"])
        for keys, event in grouped:
            signal_date, entry_date, confirm_date, exit_date = keys
            known = ledger[pd.to_datetime(ledger["known_after"]) < pd.Timestamp(signal_date)]
            loss_counts = known[known["selected_relative_return"] < 0].groupby("industry_code").size()
            blocked = set(loss_counts[loss_counts >= loss_threshold].index) if loss_threshold < 999 else set()
            candidates = event[~event["industry_code"].isin(blocked)].copy()
            if len(candidates) < TOP_N:
                candidates = event.copy()
            selected = candidates.sort_values(FEATURE, ascending=False).head(TOP_N).copy()
            top_cut = event["future_return_after_confirm"].quantile(0.8)
            relative = float(selected["relative_return_after_confirm"].mean())
            hit = float((selected["future_return_after_confirm"] >= top_cut).mean())
            rows.append({
                "rule_name": rule_name,
                "feature": FEATURE,
                "top_n": TOP_N,
                "confirm_days": CONFIRM_DAYS,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "confirm_date": confirm_date,
                "exit_date": exit_date,
                "year": int(pd.to_datetime(signal_date).year),
                "relative_return": relative,
                "relative_win": relative > 0,
                "top_quintile_hit_rate": hit,
                "blocked_industry_count": len(blocked),
                "used_fallback": len(candidates) < TOP_N,
            })
            out = selected.copy()
            out["rule_name"] = rule_name
            out["selected_rank"] = range(1, len(out) + 1)
            out["event_relative_return"] = relative
            out["event_top_quintile_hit_rate"] = hit
            out["blocked_industry_count"] = len(blocked)
            selected_rows.append(out)
            ledger = pd.concat([
                ledger,
                pd.DataFrame({
                    "industry_code": selected["industry_code"].astype(str),
                    "known_after": exit_date,
                    "selected_relative_return": selected["relative_return_after_confirm"].astype(float),
                }),
            ], ignore_index=True)
    return pd.DataFrame(rows), pd.concat(selected_rows, ignore_index=True)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rule_name, group in events.groupby("rule_name"):
        oos = group[group["year"].ge(2022)]
        yearly = group.groupby("year")["relative_return"].mean()
        row = {
            "rule_name": rule_name,
            "feature": FEATURE,
            "top_n": TOP_N,
            "confirm_days": CONFIRM_DAYS,
            "event_count": int(len(group)),
            "year_count": int(group["year"].nunique()),
            "mean_relative_return": float(group["relative_return"].mean()),
            "median_relative_return": float(group["relative_return"].median()),
            "relative_win_rate": float(group["relative_win"].mean()),
            "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
            "avg_blocked_industry_count": float(group["blocked_industry_count"].mean()),
        }
        row["point_gate_passed"] = passes_point_gate(row)
        row.update(v480.robustness_metrics(group, TOP_N) if row["point_gate_passed"] else {})
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["passes_gate", "point_gate_passed", "mean_relative_return"], ascending=[False, False, False])


def failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("event_count", 30, ">="), ("year_count", 8, ">="),
        ("mean_relative_return", 0, ">"), ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="), ("top_quintile_hit_rate", 0.30, ">="),
        ("oos_event_count", 8, ">="), ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="), ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="), ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="), ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0, ">"),
    ]
    failed = []
    for metric, required, op in checks:
        if op == "==":
            ok = row.get(metric) == required
        else:
            value = float(row.get(metric, 0) or 0)
            ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    return failed


def passes_point_gate(row: dict[str, Any]) -> bool:
    point = {
        "event_count", "year_count", "mean_relative_return", "median_relative_return",
        "relative_win_rate", "top_quintile_hit_rate", "oos_event_count",
        "oos_mean_relative_return", "oos_relative_win_rate",
    }
    return not (point & set(failed_metrics(row)))


def build_summary(results: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_gate", False))
    return {
        "version": "5.18.0",
        "policy_id": "rebound_leader_rolling_quarantine_audit_v5_18",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_rule_count": int(len(results)),
        "best_rule": best.get("rule_name", ""),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "passing_rule_count": int(results["passes_gate"].sum()) if len(results) else 0,
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "pass_rolling_quarantine_gate" if passed else "research_only_no_rolling_quarantine_alpha",
        "final_verdict": "V5.18 滚动失败隔离未通过完整强行业门槛，不能声称目标完成。" if not passed else "V5.18 滚动失败隔离通过强行业门槛，但仍需前推验证。",
    }


def write_outputs(summary: dict[str, Any], panel: pd.DataFrame, events: pd.DataFrame, selected: pd.DataFrame, results: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, results), encoding="utf-8")
    panel.to_csv(DEBUG / "rolling_quarantine_source_panel.csv", index=False, encoding="utf-8-sig")
    events.to_csv(DEBUG / "rolling_quarantine_event_panel.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(DEBUG / "rolling_quarantine_selected_rows.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "rolling_quarantine_results.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.18 滚动失败隔离审计",
        "",
        summary["final_verdict"],
        "",
        f"- 测试规则数：{summary['tested_rule_count']}",
        f"- 最优规则：`{summary['best_rule']}`",
        f"- 最优事件数：{summary['best_event_count']}",
        f"- 最优平均相对收益：{pct(summary['best_mean_relative_return'])}",
        f"- 最优 Top20% 命中率：{pct(summary['best_top_quintile_hit_rate'])}",
        f"- 通过规则数：{summary['passing_rule_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 结果",
        "",
        results.to_markdown(index=False) if len(results) else "无数据",
        "",
        "边界：V5.18 每个事件只使用当时已经结算的历史失败记录，不能使用全样本事后失败行业名单。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    row = {
        "event_count": 30, "year_count": 8, "mean_relative_return": 0.01,
        "median_relative_return": 0.01, "relative_win_rate": 0.56,
        "top_quintile_hit_rate": 0.31, "oos_event_count": 8,
        "oos_mean_relative_return": 0.01, "oos_relative_win_rate": 0.50,
        "robust_gate_passed": True, "leave_one_year_gate_passed": True,
        "bootstrap_top_quintile_hit_p05": 0.31, "bootstrap_positive_year_p05": 0.60,
        "leave_one_year_min_hit_rate": 0.25, "leave_one_year_min_mean_relative_return": 0.01,
    }
    assert passes_point_gate(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
