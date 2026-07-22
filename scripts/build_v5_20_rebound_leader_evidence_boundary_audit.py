#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from valuation_pit_contract import methodology_route_ready


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "rebound_leader_evidence_boundary_audit_v5_20"
DEBUG = OUT / "debug"
PIT_METHODOLOGY_SUMMARY = ROOT / "outputs" / "audit" / "pit_universe_methodology_remediation" / "run_summary.json"

SOURCES = [
    ("PIT绝对估值", "rebound_leader_pit_valuation_audit_v5_11", "valuation"),
    ("PIT估值历史分位", "rebound_leader_pit_valuation_percentile_audit_v5_12", "valuation"),
    ("早期相对强弱确认", "rebound_leader_early_confirmation_audit_v5_13", "price_confirmation"),
    ("确认期过滤", "rebound_leader_confirmation_filter_audit_v5_14", "price_confirmation"),
    ("失败归因", "rebound_leader_failure_diagnosis_v5_15", "diagnosis"),
    ("窗口质量代理", "rebound_window_quality_proxy_audit_v5_16", "window_quality"),
    ("压力恢复阶段扩样", "rebound_phase_sample_expansion_audit_v5_17", "sample_expansion"),
    ("滚动失败隔离", "rebound_leader_rolling_quarantine_audit_v5_18", "failure_guardrail"),
    ("量能确认", "rebound_leader_volume_confirmation_audit_v5_19", "volume"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.20 evidence boundary audit for rebound-leader goal.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    methodology = read_json(PIT_METHODOLOGY_SUMMARY)
    rows = build_rows(methodology)
    boundary = build_boundary_checks(rows, methodology)
    summary = build_summary(rows, boundary, methodology)
    write_outputs(summary, rows, boundary)
    print(f"output_dir={OUT}")
    print(f"goal_ready={summary['goal_ready']}")
    print(f"evidence_boundary={summary['evidence_boundary']}")


def build_rows(methodology: dict[str, Any] | None = None) -> pd.DataFrame:
    methodology = methodology or {}
    rows = []
    for label, directory, family in SOURCES:
        path = ROOT / "outputs" / "audit" / directory / "run_summary.json"
        data = read_json(path)
        is_valuation = family == "valuation"
        rows.append({
            "evidence_label": label,
            "feature_family": family,
            "version": data.get("version", ""),
            "policy_id": data.get("policy_id", directory),
            "source_path": str(path.relative_to(ROOT)),
            "tested_count": data.get("tested_feature_count", data.get("tested_rule_count", data.get("tested_filter_count", data.get("tested_phase_count", "")))),
            "passing_rule_count": int(data.get("passing_rule_count", 0) or 0),
            "can_claim_strong_rebound_industries": bool(data.get("can_claim_strong_rebound_industries", False)),
            "best_status": data.get("best_status", ""),
            "best_signal": data.get("best_feature", data.get("best_rule", data.get("best_filter", data.get("best_phase_variant", "")))),
            "best_mean_relative_return": data.get("best_mean_relative_return", ""),
            "best_top_quintile_hit_rate": data.get("best_top_quintile_hit_rate", ""),
            "final_verdict": data.get("final_verdict", ""),
            "historical_valuation_pit_gate_passed": methodology.get("historical_valuation_pit_gate_passed", "") if is_valuation else "",
            "valuation_availability_status": methodology.get("valuation_availability_status", "") if is_valuation else "",
            "promotion_eligible_valuation_row_count": methodology.get("promotion_eligible_valuation_row_count", "") if is_valuation else "",
            "legacy_oos_label_corrected": methodology.get("legacy_oos_label_corrected", "") if is_valuation else "",
        })
    return pd.DataFrame(rows)


def build_boundary_checks(rows: pd.DataFrame, methodology: dict[str, Any] | None = None) -> pd.DataFrame:
    methodology = methodology or {}
    promotion_gate_passed = methodology_route_ready(methodology, {})
    audit_passed = bool(methodology.get("audit_passed", False))
    eligible_rows = int(methodology.get("promotion_eligible_valuation_row_count", 0) or 0)
    checks = [
        {
            "check": "pit_methodology_control_enforced",
            "status": "pass" if audit_passed else "fail",
            "evidence": f"audit_passed={audit_passed}; legacy_oos_label_corrected={methodology.get('legacy_oos_label_corrected')}",
            "meaning": "方法审计必须先证明旧 OOS 标签已纠正、缺证据会失败关闭。",
        },
        {
            "check": "historical_valuation_promotion_gate",
            "status": "pass" if promotion_gate_passed else "fail",
            "evidence": f"promotion_gate_passed={promotion_gate_passed}; eligible_rows={eligible_rows}; availability={methodology.get('valuation_availability_status')}; classification={methodology.get('classification_history_status')}",
            "meaning": "历史估值缺少逐行可得日期或同期分类时，不得进入晋级证据。",
        },
        {
            "check": "no_passing_rule_after_v5_11",
            "status": "fail",
            "evidence": f"passing_rule_count_total={int(rows['passing_rule_count'].sum())}",
            "meaning": "V5.11-V5.19 没有任何规则通过完整强反弹行业门槛。",
        },
        {
            "check": "best_candidate_still_research_only",
            "status": "fail",
            "evidence": best_candidate_evidence(rows),
            "meaning": "最接近规则仍是研究观察，不能声称目标完成。",
        },
        {
            "check": "local_historical_feature_families_exhausted",
            "status": "fail",
            "evidence": "tested=valuation,price_confirmation,window_quality,sample_expansion,failure_guardrail,volume",
            "meaning": "现有本地历史字段已覆盖估值、价格确认、窗口质量、扩样、失败隔离和成交额，但都未通过。",
        },
        {
            "check": "next_evidence_needed",
            "status": "pending",
            "evidence": "needs=forward_settled_samples_or_new_pit_source",
            "meaning": "下一步需要冻结规则后的前推样本，或真正新增的 PIT 信息源。",
        },
    ]
    return pd.DataFrame(checks)


def best_candidate_evidence(rows: pd.DataFrame) -> str:
    numeric = rows.copy()
    numeric["best_mean_relative_return_num"] = pd.to_numeric(numeric["best_mean_relative_return"], errors="coerce")
    best = numeric.sort_values("best_mean_relative_return_num", ascending=False).iloc[0]
    return f"{best['version']} {best['best_signal']} mean={best['best_mean_relative_return']} hit={best['best_top_quintile_hit_rate']} status={best['best_status']}"


def build_summary(rows: pd.DataFrame, boundary: pd.DataFrame, methodology: dict[str, Any] | None = None) -> dict[str, Any]:
    methodology = methodology or {}
    methodology_ready = methodology_route_ready(methodology, {})
    passing = int(rows["passing_rule_count"].sum())
    claim = bool(rows["can_claim_strong_rebound_industries"].any())
    return {
        "version": "5.20.0",
        "policy_id": "rebound_leader_evidence_boundary_audit_v5_20",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "audited_versions": int(len(rows)),
        "passing_rule_count_total": passing,
        "any_version_can_claim_strong_rebound_industries": claim,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "pit_methodology_audit_passed": bool(methodology.get("audit_passed", False)),
        "historical_valuation_pit_gate_passed": methodology_ready and bool(methodology.get("historical_valuation_pit_gate_passed", False)),
        "historical_classification_gate_passed": methodology_ready and bool(methodology.get("historical_classification_gate_passed", False)),
        "promotion_eligible_valuation_row_count": int(methodology.get("promotion_eligible_valuation_row_count", 0) or 0),
        "valuation_availability_status": methodology.get("valuation_availability_status", "unknown"),
        "legacy_oos_label_corrected": bool(methodology.get("legacy_oos_label_corrected", False)),
        "evidence_boundary": "historical_valuation_and_classification_unavailable_for_promotion",
        "best_status": "research_only_evidence_boundary_reached",
        "final_verdict": "V5.20 证据边界审计显示：历史估值缺少可验证的逐行可得日期，历史行业分类也不完整；旧回测只能作迭代历史审查，不能用于晋级。",
    }


def write_outputs(summary: dict[str, Any], rows: pd.DataFrame, boundary: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    rows.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, rows, boundary), encoding="utf-8")
    rows.to_csv(DEBUG / "evidence_version_summary.csv", index=False, encoding="utf-8-sig")
    boundary.to_csv(DEBUG / "evidence_boundary_checks.csv", index=False, encoding="utf-8-sig")
    next_actions().to_csv(DEBUG / "next_evidence_actions.csv", index=False, encoding="utf-8-sig")


def next_actions() -> pd.DataFrame:
    return pd.DataFrame([
        {"priority": "P0", "action": "冻结 V5.14 最接近规则进入前推观察", "why": "本地历史回测未过稳健门槛，继续微调价值低。"},
        {"priority": "P0", "action": "积累或接入新的 PIT 行业信息源", "why": "当前价格、估值、成交额字段均未证明强行业 alpha。"},
        {"priority": "P1", "action": "等冻结后新增窗口结算后再做晋级评价", "why": "目标完成需要真实前推或独立新样本证据。"},
    ])


def render_report(summary: dict[str, Any], rows: pd.DataFrame, boundary: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.20 强反弹行业证据边界审计",
        "",
        summary["final_verdict"],
        "",
        f"- 审计版本数：{summary['audited_versions']}",
        f"- 通过规则总数：{summary['passing_rule_count_total']}",
        f"- 是否有版本可声称找到强反弹行业：`{str(summary['any_version_can_claim_strong_rebound_industries']).lower()}`",
        f"- 证据边界：`{summary['evidence_boundary']}`",
        f"- 历史估值晋级门：`{str(summary['historical_valuation_pit_gate_passed']).lower()}`（可晋级估值行={summary['promotion_eligible_valuation_row_count']}）",
        f"- 历史分类门：`{str(summary['historical_classification_gate_passed']).lower()}`",
        f"- 目标是否完成：`{str(summary['goal_ready']).lower()}`",
        "",
        "## 版本证据",
        "",
        rows.to_markdown(index=False),
        "",
        "## 边界检查",
        "",
        boundary.to_markdown(index=False),
        "",
        "边界：V5.20 不新增策略，只判断当前本地历史数据和已测试字段是否足以完成目标。",
    ])


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    rows = pd.DataFrame([
        {"passing_rule_count": 0, "can_claim_strong_rebound_industries": False, "best_mean_relative_return": 0.01, "version": "x", "best_signal": "a", "best_top_quintile_hit_rate": 0.2, "best_status": "research_only"}
    ])
    methodology = {
        "audit_passed": True,
        "methodology_remediation_complete": True,
        "promotion_gate_passed": False,
        "historical_valuation_pit_gate_passed": False,
        "historical_classification_gate_passed": False,
        "promotion_eligible_valuation_row_count": 0,
        "valuation_availability_status": "unavailable_for_promotion",
        "legacy_oos_label_corrected": True,
    }
    boundary = build_boundary_checks(rows, methodology)
    summary = build_summary(rows, boundary, methodology)
    assert summary["goal_ready"] is False
    assert summary["evidence_boundary"] == "historical_valuation_and_classification_unavailable_for_promotion"
    assert summary["historical_valuation_pit_gate_passed"] is False
    assert summary["promotion_eligible_valuation_row_count"] == 0
    forged = build_boundary_checks(rows, {**methodology, "audit_passed": False, "promotion_gate_passed": True})
    assert forged.set_index("check").loc["historical_valuation_promotion_gate", "status"] == "fail"
    print("self_check=pass")


if __name__ == "__main__":
    main()
