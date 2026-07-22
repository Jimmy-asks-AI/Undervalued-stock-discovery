#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_industry_rebound_leader_selection_v4_72 import (
    V471_SUMMARY,
    current_snapshot_features,
    read_json,
)


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
OUT = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_quarantine_overlay"
DEBUG = OUT / "debug"

FIELDS = [
    "candidate_status",
    "rank_after_quarantine",
    "original_rank",
    "industry_code",
    "industry_name",
    "selection_strategy",
    "selection_score",
    "quarantine_status",
    "quarantine_reason",
    "manual_action",
    "auto_execution_allowed",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build V4.72 current rebound-leader quarantine overlay.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    rows, full = build_overlay()
    write_outputs(rows, full)
    print(f"output_dir={OUT}")
    print(f"replacement_rows={len(rows)}")


def build_overlay() -> tuple[list[dict[str, Any]], pd.DataFrame]:
    v471 = read_json(V471_SUMMARY)
    v472 = read_json(V472 / "run_summary.json")
    diagnosis = pd.read_csv(V472 / "debug" / "failure_diagnosis.csv", encoding="utf-8-sig")
    repeated = repeated_worst_industries(diagnosis)
    strategy = str(v472.get("best_strategy") or "oversold_liquidity")
    score_col = f"{strategy}_score"
    features = current_snapshot_features(v471)
    if features.empty or score_col not in features.columns:
        return [], pd.DataFrame()
    ranked = features.sort_values(score_col, ascending=False).reset_index(drop=True).copy()
    ranked["original_rank"] = ranked.index + 1
    ranked["selection_strategy"] = strategy
    ranked["selection_score"] = ranked[score_col]
    ranked["quarantine_status"] = ranked["industry_name"].map(lambda name: "quarantined" if str(name) in repeated else "eligible_observation")
    ranked["quarantine_reason"] = ranked["industry_name"].map(lambda name: repeated.get(str(name), ""))
    ranked["manual_action"] = ranked["quarantine_status"].map({
        "quarantined": "隔离观察，不进入强反弹候选替补池",
        "eligible_observation": "仅作为替补观察，仍需载体/资金流/入场日前复核",
    })
    ranked["auto_execution_allowed"] = "否"
    eligible = ranked[ranked["quarantine_status"].eq("eligible_observation")].head(20).copy()
    eligible.insert(0, "candidate_status", "research_only_quarantine_replacement_observation")
    eligible.insert(1, "rank_after_quarantine", range(1, len(eligible) + 1))
    return eligible[FIELDS].to_dict("records"), ranked


def repeated_worst_industries(diagnosis: pd.DataFrame) -> dict[str, str]:
    out: dict[str, str] = {}
    if diagnosis.empty:
        return out
    rows = diagnosis[diagnosis["category"].eq("repeated_worst_event_industry")].copy()
    rows["value_num"] = pd.to_numeric(rows["value"], errors="coerce").fillna(0)
    for item in rows[rows["value_num"].ge(3)].to_dict("records"):
        out[str(item["item"])] = f"appears_in_worst_5_events={int(item['value_num'])}"
    return out


def write_outputs(rows: list[dict[str, Any]], full: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    if full.empty:
        write_rows(DEBUG / "full_ranked_quarantine_overlay.csv", [])
    else:
        keep = ["original_rank", "industry_code", "industry_name", "selection_strategy", "selection_score", "quarantine_status", "quarantine_reason", "manual_action", "auto_execution_allowed"]
        full[keep].to_csv(DEBUG / "full_ranked_quarantine_overlay.csv", index=False, encoding="utf-8-sig")
    asof = asof_filter_evidence()
    summary = {
        "version": "v4_72_rebound_leader_quarantine_overlay_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "full_universe_count": int(len(full)),
        "quarantined_industry_count": int(full["quarantine_status"].eq("quarantined").sum()) if not full.empty else 0,
        "replacement_observation_count": len(rows),
        "top_replacement_industries": "、".join(str(row["industry_name"]) for row in rows[:10]),
        **asof,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "已生成历史失败隔离后的替补观察池；只用于人工复核，不证明强反弹 alpha。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "# V4.72 强行业风险隔离与替补观察池",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 全行业数量：{summary['full_universe_count']}",
        f"- 历史失败隔离行业：{summary['quarantined_industry_count']}",
        f"- 替补观察池：{summary['replacement_observation_count']}",
        f"- 历史 as-of 失败过滤：{summary.get('asof_best_variant', '')}",
        f"- as-of Top20% 命中率：{summary.get('asof_best_top_quintile_hit_rate', '')}",
        f"- as-of 正年份率：{summary.get('asof_best_positive_year_rate', '')}",
        f"- as-of 过滤是否过强行业门槛：`{str(summary.get('asof_failure_filter_passes_gate', False)).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "| rank_after_quarantine | industry_name | original_rank | selection_score | manual_action |",
        "|:---|:---|:---|:---|:---|",
    ]
    for row in rows[:20]:
        lines.append(f"| {row['rank_after_quarantine']} | {row['industry_name']} | {row['original_rank']} | {row['selection_score']} | {row['manual_action']} |")
    return "\n".join(lines)


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def asof_filter_evidence() -> dict[str, Any]:
    path = V472 / "debug" / "asof_failure_filter_sensitivity.csv"
    if not path.exists():
        return {
            "asof_best_variant": "",
            "asof_failure_filter_passes_gate": False,
        }
    rows = list(csv.DictReader(path.open("r", encoding="utf-8-sig", newline="")))
    if not rows:
        return {
            "asof_best_variant": "",
            "asof_failure_filter_passes_gate": False,
        }
    best = max(rows, key=lambda row: float_value(row.get("mean_relative_return")))
    return {
        "asof_best_variant": best.get("variant", ""),
        "asof_best_mean_relative_return": float_value(best.get("mean_relative_return")),
        "asof_best_top_quintile_hit_rate": float_value(best.get("top_quintile_hit_rate")),
        "asof_best_positive_year_rate": float_value(best.get("positive_year_rate")),
        "asof_best_mean_excluded_industry_count": float_value(best.get("mean_excluded_industry_count")),
        "asof_failure_filter_passes_gate": str(best.get("passes_strong_rebound_gate", "")).lower() == "true",
    }


def float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def self_check() -> None:
    diagnosis = pd.DataFrame([
        {"category": "repeated_worst_event_industry", "item": "A", "value": "3"},
        {"category": "repeated_worst_event_industry", "item": "B", "value": "2"},
    ])
    repeated = repeated_worst_industries(diagnosis)
    assert repeated == {"A": "appears_in_worst_5_events=3"}
    assert float_value("0.12") == 0.12
    assert float_value("") == 0.0
    print("self_check=pass")


if __name__ == "__main__":
    main()
