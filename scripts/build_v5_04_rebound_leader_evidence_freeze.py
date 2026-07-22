#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
V503_RESULTS = ROOT / "outputs" / "industry_rebound_leader_window_quality_v5_03" / "top_candidates.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_evidence_freeze_v5_04"
DEBUG = OUT / "debug"
FROZEN_RULES = ["quality_score_ge2", "quality_score_ge3"]


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.04 freeze small-sample rebound leader evidence.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    results = pd.read_csv(V503_RESULTS, encoding="utf-8-sig")
    frozen = build_frozen_rules(results)
    checklist = build_promotion_checklist(frozen)
    template = build_forward_template(frozen)
    summary = build_summary(frozen, checklist)
    write_outputs(summary, frozen, checklist, template)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"frozen_rule_count={summary['frozen_rule_count']}")


def build_frozen_rules(results: pd.DataFrame) -> pd.DataFrame:
    rows = []
    indexed = results.set_index("quality_rule")
    for rule in FROZEN_RULES:
        row = indexed.loc[rule].to_dict()
        rows.append({
            "frozen_rule": rule,
            "source_version": "5.03.0",
            "rule_definition": rule_definition(rule),
            "historical_event_count": int(row.get("event_count", 0)),
            "historical_year_count": int(row.get("year_count", 0)),
            "historical_mean_relative_return": float(row.get("mean_relative_return", 0.0)),
            "historical_top_quintile_hit_rate": float(row.get("top_quintile_hit_rate", 0.0)),
            "historical_oos_event_count": int(row.get("oos_event_count", 0)),
            "historical_oos_mean_relative_return": float(row.get("oos_mean_relative_return", 0.0)),
            "historical_failed_metrics": row.get("failed_metrics", ""),
            "frozen_status": "forward_observation_only",
            "allowed_next_action": "append_new_forward_samples_only",
            "forbidden_next_action": "do_not_change_thresholds_from_historical_results",
        })
    return pd.DataFrame(rows)


def rule_definition(rule: str) -> str:
    if rule == "quality_score_ge2":
        return "vol_repair window + beta_120_rank Top5 + window_quality_score >= 2"
    if rule == "quality_score_ge3":
        return "vol_repair window + beta_120_rank Top5 + window_quality_score >= 3"
    return rule


def build_promotion_checklist(frozen: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in frozen.iterrows():
        for metric, required, current in [
            ("new_forward_event_count", 12, 0),
            ("new_forward_positive_relative_rate", 0.55, ""),
            ("new_forward_top_quintile_hit_rate", 0.30, ""),
            ("new_forward_mean_relative_return", 0.0, ""),
            ("combined_event_count", 30, row["historical_event_count"]),
            ("combined_bootstrap_top_quintile_hit_p05", 0.30, ""),
            ("combined_bootstrap_positive_year_p05", 0.60, ""),
        ]:
            rows.append({
                "frozen_rule": row["frozen_rule"],
                "metric": metric,
                "current": current,
                "required": required,
                "status": "pending_forward_sample" if metric.startswith("new_forward") else "not_passed",
            })
    return pd.DataFrame(rows)


def build_forward_template(frozen: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in frozen.iterrows():
        rows.append({
            "frozen_rule": row["frozen_rule"],
            "signal_date": "",
            "entry_date": "",
            "exit_date": "",
            "selected_industries": "",
            "benchmark_return": "",
            "selected_net_return": "",
            "relative_return": "",
            "top_quintile_hit_rate": "",
            "settlement_status": "pending",
        })
    return pd.DataFrame(rows)


def build_summary(frozen: pd.DataFrame, checklist: pd.DataFrame) -> dict[str, Any]:
    return {
        "version": "5.04.0",
        "policy_id": "rebound_leader_evidence_freeze_v5_04",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "frozen_rule_count": int(len(frozen)),
        "pending_check_count": int(len(checklist)),
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_frozen_forward_observation",
        "final_verdict": "V5.04 冻结 quality_score_ge2/ge3 + beta Top5 为前推观察规则；历史样本不足，不能继续调参声称完成目标。",
    }


def write_outputs(summary: dict[str, Any], frozen: pd.DataFrame, checklist: pd.DataFrame, template: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    frozen.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, frozen, checklist), encoding="utf-8")
    frozen.to_csv(DEBUG / "frozen_rule_spec.csv", index=False, encoding="utf-8-sig")
    checklist.to_csv(DEBUG / "promotion_checklist.csv", index=False, encoding="utf-8-sig")
    template.to_csv(DEBUG / "forward_validation_template.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], frozen: pd.DataFrame, checklist: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.04 小样本证据冻结",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 冻结规则数：{summary['frozen_rule_count']}",
        f"- 待前推验证项：{summary['pending_check_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 冻结规则",
        "",
        frozen.to_markdown(index=False),
        "",
        "## 晋级检查表",
        "",
        checklist.to_markdown(index=False),
        "",
        "## 研究边界",
        "",
        "从 V5.04 开始，冻结规则只能追加未来样本验证，不允许根据历史结果再调窗口质量阈值、TopN 或 beta 定义。",
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    assert "window_quality_score >= 2" in rule_definition("quality_score_ge2")
    assert "window_quality_score >= 3" in rule_definition("quality_score_ge3")
    print("self_check=pass")


if __name__ == "__main__":
    main()
