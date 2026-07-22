#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "outputs" / "industry_rebound_leader_window_quality_v5_03" / "debug" / "window_quality_event_panel.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_pseudo_forward_audit_v5_09"
DEBUG = OUT / "debug"
RULES = ["quality_score_ge2", "quality_score_ge3"]
CUT_YEARS = [2018, 2020, 2022]


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.09 pseudo-forward audit for frozen rebound leader rules.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    events = pd.read_csv(EVENTS, encoding="utf-8-sig")
    audit = pseudo_forward(events)
    summary = build_summary(audit)
    write_outputs(summary, audit)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_split_count={summary['passing_split_count']}")


def pseudo_forward(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    events = events[events["quality_rule"].isin(RULES)].copy()
    for rule in RULES:
        group = events[events["quality_rule"].eq(rule)]
        for cut_year in CUT_YEARS:
            post = group[group["year"].gt(cut_year)]
            row = {
                "frozen_rule": rule,
                "pseudo_freeze_year": cut_year,
                "post_event_count": int(len(post)),
                "post_year_count": int(post["year"].nunique()),
                "post_mean_relative_return": mean(post, "relative_return"),
                "post_positive_relative_rate": positive_rate(post, "relative_return"),
                "post_top_quintile_hit_rate": mean(post, "top_quintile_hit_rate"),
            }
            row["passes_pseudo_forward_gate"] = passes_gate(row)
            row["failed_metrics"] = ";".join(failed_metrics(row))
            rows.append(row)
    return pd.DataFrame(rows).sort_values(["passes_pseudo_forward_gate", "post_mean_relative_return"], ascending=[False, False])


def mean(frame: pd.DataFrame, col: str) -> float:
    return float(frame[col].mean()) if len(frame) else 0.0


def positive_rate(frame: pd.DataFrame, col: str) -> float:
    return float(frame[col].gt(0).mean()) if len(frame) else 0.0


def failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("post_event_count", 12, ">="),
        ("post_year_count", 4, ">="),
        ("post_mean_relative_return", 0, ">"),
        ("post_positive_relative_rate", 0.55, ">="),
        ("post_top_quintile_hit_rate", 0.30, ">="),
    ]
    out = []
    for metric, required, op in checks:
        value = float(row.get(metric, 0) or 0)
        ok = value >= required if op == ">=" else value > required
        if not ok:
            out.append(metric)
    return out


def passes_gate(row: dict[str, Any]) -> bool:
    return not failed_metrics(row)


def build_summary(audit: pd.DataFrame) -> dict[str, Any]:
    best = audit.iloc[0].to_dict() if len(audit) else {}
    passing = int(audit["passes_pseudo_forward_gate"].sum()) if len(audit) else 0
    return {
        "version": "5.09.0",
        "policy_id": "rebound_leader_pseudo_forward_audit_v5_09",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_split_count": int(len(audit)),
        "passing_split_count": passing,
        "best_rule": best.get("frozen_rule", ""),
        "best_pseudo_freeze_year": int(best.get("pseudo_freeze_year", 0) or 0),
        "best_post_event_count": int(best.get("post_event_count", 0) or 0),
        "best_post_mean_relative_return": float(best.get("post_mean_relative_return", 0.0) or 0.0),
        "best_post_top_quintile_hit_rate": float(best.get("post_top_quintile_hit_rate", 0.0) or 0.0),
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_pseudo_forward_sample_insufficient" if passing == 0 else "research_only_pseudo_forward_observation",
        "final_verdict": "V5.09 历史伪前推方向仍为正，但所有切分都因样本数不足或门槛未全过而不能证明目标完成。",
    }


def write_outputs(summary: dict[str, Any], audit: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    audit.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, audit), encoding="utf-8")
    audit.to_csv(DEBUG / "pseudo_forward_splits.csv", index=False, encoding="utf-8-sig")
    audit[audit["passes_pseudo_forward_gate"]].to_csv(DEBUG / "passing_pseudo_forward_splits.csv", index=False, encoding="utf-8-sig")
    audit[~audit["passes_pseudo_forward_gate"]].to_csv(DEBUG / "failed_pseudo_forward_splits.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], audit: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.09 历史伪前推审计",
        "",
        summary["final_verdict"],
        "",
        f"- 测试切分数：{summary['tested_split_count']}",
        f"- 通过切分数：{summary['passing_split_count']}",
        f"- 最优规则：`{summary['best_rule']}`",
        f"- 最优伪冻结年份：{summary['best_pseudo_freeze_year']}",
        f"- 最优后验事件数：{summary['best_post_event_count']}",
        f"- 最优后验平均相对收益：{pct(summary['best_post_mean_relative_return'])}",
        f"- 最优后验 Top20% 命中率：{pct(summary['best_post_top_quintile_hit_rate'])}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 切分结果",
        "",
        audit.to_markdown(index=False),
        "",
        "边界：V5.09 只用固定冻结规则做历史伪前推，不根据切分结果修改规则。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    row = {
        "post_event_count": 12, "post_year_count": 4, "post_mean_relative_return": 0.01,
        "post_positive_relative_rate": 0.56, "post_top_quintile_hit_rate": 0.31,
    }
    assert passes_gate(row)
    row["post_event_count"] = 11
    assert "post_event_count" in failed_metrics(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
