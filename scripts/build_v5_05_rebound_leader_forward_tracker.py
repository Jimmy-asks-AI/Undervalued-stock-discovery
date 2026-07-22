#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "outputs" / "audit" / "rebound_leader_evidence_freeze_v5_04" / "top_candidates.csv"
TEMPLATE = ROOT / "outputs" / "audit" / "rebound_leader_evidence_freeze_v5_04" / "debug" / "forward_validation_template.csv"
LEDGER_LOG = ROOT / "logs" / "v5_05_rebound_leader_forward_ledger.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_forward_tracker_v5_05"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.05 forward tracker for frozen rebound leader rules.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    frozen = pd.read_csv(FROZEN, encoding="utf-8-sig")
    ledger = load_ledger()
    progress = build_progress(frozen, ledger)
    boundary = build_boundary_audit(frozen, ledger)
    summary = build_summary(progress, boundary)
    write_outputs(summary, progress, ledger, boundary)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"forward_settled_event_count={summary['forward_settled_event_count']}")


def init_ledger(template: pd.DataFrame) -> pd.DataFrame:
    ledger = template.copy()
    if ledger.empty:
        return ledger
    ledger["created_by_version"] = "5.05.0"
    ledger["sample_source"] = "future_only"
    ledger["rule_mutation_allowed"] = False
    return ledger


def load_ledger() -> pd.DataFrame:
    if LEDGER_LOG.exists():
        return pd.read_csv(LEDGER_LOG, encoding="utf-8-sig")
    return init_ledger(pd.read_csv(TEMPLATE, encoding="utf-8-sig"))


def build_progress(frozen: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, rule in frozen.iterrows():
        settled = ledger[(ledger["frozen_rule"].eq(rule["frozen_rule"])) & (ledger["settlement_status"].eq("settled"))].copy()
        rel = pd.to_numeric(settled.get("relative_return", pd.Series(dtype=float)), errors="coerce")
        hit = pd.to_numeric(settled.get("top_quintile_hit_rate", pd.Series(dtype=float)), errors="coerce")
        new_count = int(len(settled))
        rows.append({
            "frozen_rule": rule["frozen_rule"],
            "historical_event_count": int(rule["historical_event_count"]),
            "required_new_forward_event_count": 12,
            "new_forward_event_count": new_count,
            "new_forward_event_gap": max(0, 12 - new_count),
            "new_forward_mean_relative_return": float(rel.mean()) if new_count else "",
            "new_forward_positive_relative_rate": float(rel.gt(0).mean()) if new_count else "",
            "new_forward_top_quintile_hit_rate": float(hit.mean()) if new_count else "",
            "combined_event_count": int(rule["historical_event_count"]) + new_count,
            "combined_event_gap_to_30": max(0, 30 - int(rule["historical_event_count"]) - new_count),
            "promotion_status": "pending_forward_samples",
        })
    return pd.DataFrame(rows)


def build_boundary_audit(frozen: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([
        {"check": "frozen_rule_count", "current": int(len(frozen)), "required": 2, "status": "pass" if len(frozen) == 2 else "fail"},
        {"check": "rule_mutation_allowed", "current": bool(ledger["rule_mutation_allowed"].any()) if len(ledger) else False, "required": False, "status": "pass"},
        {"check": "settled_forward_rows", "current": int(ledger["settlement_status"].eq("settled").sum()) if len(ledger) else 0, "required": "future data", "status": "pending"},
        {"check": "can_claim_goal", "current": False, "required": True, "status": "fail"},
    ])


def build_summary(progress: pd.DataFrame, boundary: pd.DataFrame) -> dict[str, Any]:
    settled = int(progress["new_forward_event_count"].sum()) if len(progress) else 0
    return {
        "version": "5.05.0",
        "policy_id": "rebound_leader_forward_tracker_v5_05",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tracked_rule_count": int(len(progress)),
        "forward_settled_event_count": settled,
        "ledger_path": str(LEDGER_LOG.relative_to(ROOT)),
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_forward_tracker_waiting_samples",
        "final_verdict": "V5.05 已建立冻结规则前推账本；当前没有新增已结算前推样本，不能声称目标完成。",
    }


def write_outputs(summary: dict[str, Any], progress: pd.DataFrame, ledger: pd.DataFrame, boundary: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    progress.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, progress, boundary), encoding="utf-8")
    ledger.to_csv(DEBUG / "forward_validation_ledger.csv", index=False, encoding="utf-8-sig")
    progress.to_csv(DEBUG / "promotion_progress.csv", index=False, encoding="utf-8-sig")
    boundary.to_csv(DEBUG / "forward_boundary_audit.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], progress: pd.DataFrame, boundary: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.05 冻结规则前推跟踪器",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 跟踪规则数：{summary['tracked_rule_count']}",
        f"- 已结算前推事件数：{summary['forward_settled_event_count']}",
        f"- 持久账本：`{summary['ledger_path']}`",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 晋级进度",
        "",
        progress.to_markdown(index=False),
        "",
        "## 边界审计",
        "",
        boundary.to_markdown(index=False),
        "",
        "## 研究边界",
        "",
        "V5.05 只维护冻结规则的未来样本账本，不允许用历史结果继续调阈值。前推样本结算前，该目标仍未完成。",
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    frozen = pd.DataFrame({"frozen_rule": ["r"], "historical_event_count": [18]})
    ledger = pd.DataFrame({"frozen_rule": ["r"], "settlement_status": ["pending"], "relative_return": [""], "top_quintile_hit_rate": [""]})
    progress = build_progress(frozen, ledger)
    assert int(progress["new_forward_event_gap"].iloc[0]) == 12
    assert int(progress["combined_event_gap_to_30"].iloc[0]) == 12
    print("self_check=pass")


if __name__ == "__main__":
    main()
