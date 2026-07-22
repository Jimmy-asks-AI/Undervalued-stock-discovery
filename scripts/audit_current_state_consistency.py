from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_v5_31_fund_flow_evidence_freeze_manifest as v531
from research_evidence_routes import (
    FORWARD_EVIDENCE_ROUTE_BLOCKER,
    verified_forward_evidence_ready,
    verified_forward_industry_ready,
    verified_forward_timing_ready,
)
from research_integrity import atomic_write_csv, atomic_write_json, atomic_write_text
from valuation_pit_contract import SHANGHAI, methodology_route_ready


OUT = ROOT / "outputs" / "audit" / "current_state_consistency"
DEBUG = OUT / "debug"

COHORT_SUMMARIES = {
    "V5.25": ROOT / "outputs" / "audit" / "fund_flow_forward_observer_v5_25" / "run_summary.json",
    "V5.26": ROOT / "outputs" / "audit" / "fund_flow_forward_entry_gate_v5_26" / "run_summary.json",
    "V5.27": ROOT / "outputs" / "audit" / "fund_flow_forward_settlement_v5_27" / "run_summary.json",
    "V5.28": ROOT / "outputs" / "audit" / "fund_flow_promotion_evaluator_v5_28" / "run_summary.json",
    "V5.29": ROOT / "outputs" / "audit" / "fund_flow_evidence_calendar_v5_29" / "run_summary.json",
    "V5.30": ROOT / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30" / "run_summary.json",
    "V5.31": ROOT / "outputs" / "audit" / "fund_flow_evidence_freeze_manifest_v5_31" / "run_summary.json",
    "V5.32": ROOT / "outputs" / "audit" / "fund_flow_holding_observation_v5_32" / "run_summary.json",
    "V5.33": ROOT / "outputs" / "audit" / "fund_flow_entry_price_freeze_v5_33" / "run_summary.json",
    "V5.34": ROOT / "outputs" / "audit" / "fund_flow_benchmark_entry_freeze_v5_34" / "run_summary.json",
    "V5.35": ROOT / "outputs" / "audit" / "fund_flow_waiting_room_v5_35" / "run_summary.json",
}

GOAL_SUMMARY = ROOT / "outputs" / "audit" / "rebound_leader_goal_completion_audit_v5_10" / "run_summary.json"
PIT_DISCOVERY_SUMMARY = ROOT / "outputs" / "audit" / "rebound_leader_new_pit_source_discovery_v5_21" / "run_summary.json"
PIT_METHODOLOGY_SUMMARY = ROOT / "outputs" / "audit" / "pit_universe_methodology_remediation" / "run_summary.json"
PROMOTION_SUMMARY = ROOT / "outputs" / "audit" / "rebound_leader_promotion_evaluator_v5_07" / "run_summary.json"
CURRENT_SUMMARY = ROOT / "outputs" / "etf_assisted_trading_current" / "run_summary.json"
COMPLETION_SUMMARY = ROOT / "outputs" / "audit" / "etf_assisted_trading_completion" / "run_summary.json"
CURRENT_RUNNER = ROOT / "scripts" / "run_etf_assisted_trading_current.py"
FULL_REFRESH_RUNNER = ROOT / "scripts" / "run_v4_71_live_refresh.py"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail-closed audit of current research state sources.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    raw_active = read_json(v531.ACTIVE)
    active = v531.validated_active_cohort()
    cohort_summaries = {version: read_json(path) for version, path in COHORT_SUMMARIES.items()}
    goal = read_json(GOAL_SUMMARY)
    pit_discovery = read_json(PIT_DISCOVERY_SUMMARY)
    pit_methodology = read_json(PIT_METHODOLOGY_SUMMARY)
    promotion = read_json(PROMOTION_SUMMARY)
    current = read_json(CURRENT_SUMMARY)
    completion = read_json(COMPLETION_SUMMARY)
    checks = build_checks(
        raw_active=raw_active,
        active=active,
        cohort_summaries=cohort_summaries,
        goal=goal,
        pit_discovery=pit_discovery,
        pit_methodology=pit_methodology,
        promotion=promotion,
        current=current,
        completion=completion,
        current_runner_source=read_text(CURRENT_RUNNER),
        full_refresh_source=read_text(FULL_REFRESH_RUNNER),
    )
    summary = build_summary(checks, active, current, goal, pit_discovery, pit_methodology, promotion=promotion)
    write_outputs(summary, checks, raw_active, active, cohort_summaries, current, goal, pit_discovery, pit_methodology, promotion)
    print(f"output_dir={OUT}")
    print(f"state_consistent={str(summary['state_consistent']).lower()}")
    print(f"fail_count={summary['fail_count']}")
    if not summary["state_consistent"]:
        raise SystemExit(2)


def build_checks(
    *,
    raw_active: Mapping[str, Any],
    active: Mapping[str, Any],
    cohort_summaries: Mapping[str, Mapping[str, Any]],
    goal: Mapping[str, Any],
    pit_discovery: Mapping[str, Any],
    pit_methodology: Mapping[str, Any],
    promotion: Mapping[str, Any],
    current: Mapping[str, Any],
    completion: Mapping[str, Any],
    current_runner_source: str,
    full_refresh_source: str,
) -> list[dict[str, str]]:
    cohort_id = str(active.get("cohort_id", ""))
    manifest_hash = str(active.get("manifest_hash", ""))
    active_verified = active.get("freeze_passed") is True and bool(cohort_id) and len(manifest_hash) == 64
    active_created_at = parse_timestamp(active.get("created_at_utc"))
    checks = [
        check(
            "active_cohort_verified",
            active_verified,
            f"cohort={cohort_id}; manifest={manifest_hash}; reason={active.get('validation_reason', '')}",
            "活动 cohort 必须由不可变 history、checkpoint、当前 manifest 共同复验通过。",
        ),
        check(
            "active_metadata_single_state",
            not active_verified or (
                not raw_active.get("invalidated_at_utc")
                and not raw_active.get("invalidation_reason")
                and raw_active.get("verification_required") is False
            ),
            f"freeze_passed={raw_active.get('freeze_passed')}; verification_required={raw_active.get('verification_required')}; "
            f"invalidated_at={raw_active.get('invalidated_at_utc', '')}; reason={raw_active.get('invalidation_reason', '')}",
            "已验证 active 指针不得同时残留失效元数据。",
        ),
    ]
    for version, payload in cohort_summaries.items():
        summary_id, summary_hash = cohort_pair(payload, version)
        summary_generated_at = parse_timestamp(payload.get("generated_at"))
        checks.append(check(
            f"{version}_active_pair",
            active_verified and summary_id == cohort_id and summary_hash == manifest_hash,
            f"summary=({summary_id},{summary_hash}); active=({cohort_id},{manifest_hash}); generated_at={payload.get('generated_at', '')}",
            "所有声明为当前的 cohort-aware 摘要必须绑定同一经复验 active pair。",
        ))
        checks.append(check(
            f"{version}_not_stale",
            active_verified
            and active_created_at is not None
            and summary_generated_at is not None
            and summary_generated_at >= active_created_at,
            f"summary_generated_at={payload.get('generated_at', '')}; active_created_at={active.get('created_at_utc', '')}",
            "当前 cohort-aware 摘要必须在 active cohort 建立后重建；旧快照不得仅靠同名字段冒充当前。",
        ))

    goal_time = parse_timestamp(goal.get("generated_at"))
    pit_time = parse_timestamp(pit_discovery.get("generated_at"))
    methodology_time = parse_timestamp(pit_methodology.get("generated_at"))
    verified_timing_ready = verified_forward_timing_ready(promotion)
    verified_industry_ready = verified_forward_industry_ready(promotion)
    verified_forward_ready = verified_forward_evidence_ready(promotion)
    current_forward_ready = verified_forward_ready
    goal_forward_ready = verified_forward_ready
    expected_current_methodology_gate = methodology_route_ready(pit_methodology, promotion)
    expected_goal_methodology_gate = methodology_route_ready(pit_methodology, promotion)
    checks.extend([
        check(
            "goal_audit_after_pit_discovery",
            goal_time is not None and pit_time is not None and goal_time >= pit_time,
            f"V5.10={goal.get('generated_at', '')}; V5.21={pit_discovery.get('generated_at', '')}",
            "V5.10 必须读取本轮已完成的 V5.21，不能读取上一轮摘要。",
        ),
        check(
            "goal_audit_after_pit_methodology",
            goal_time is not None and methodology_time is not None and goal_time >= methodology_time,
            f"V5.10={goal.get('generated_at', '')}; PIT_methodology={pit_methodology.get('generated_at', '')}",
            "V5.10 必须读取本轮 PIT/行业历史方法审计，不能沿用整改前摘要。",
        ),
        check(
            "pit_methodology_control_valid",
            pit_methodology.get("audit_passed") is True
            and pit_methodology.get("methodology_remediation_complete") is True
            and pit_methodology.get("legacy_oos_label_corrected") is True
            and pit_methodology.get("historical_review_set_label") == "historical_review_used_in_iteration",
            f"audit={pit_methodology.get('audit_passed')}; remediation={pit_methodology.get('methodology_remediation_complete')}; label_corrected={pit_methodology.get('legacy_oos_label_corrected')}; label={pit_methodology.get('historical_review_set_label')}",
            "方法控制、旧 OOS 标签纠正和失败关闭合同必须同时有效。",
        ),
        check(
            "pit_methodology_propagated_to_goal_and_current",
            goal.get("pit_universe_methodology_gate_passed") is expected_goal_methodology_gate
            and current.get("pit_universe_methodology_gate_passed") is expected_current_methodology_gate
            and goal.get("true_forward_route_ready") is verified_forward_ready
            and current.get("forward_timing_gate_passed") is verified_timing_ready
            and current.get("forward_industry_gate_passed") is verified_industry_ready
            and int(goal.get("promotion_eligible_valuation_row_count", -1) or 0)
            == int(pit_methodology.get("promotion_eligible_valuation_row_count", -1) or 0)
            and int(current.get("promotion_eligible_valuation_row_count", -1) or 0)
            == int(pit_methodology.get("promotion_eligible_valuation_row_count", -1) or 0),
            f"goal_gate={goal.get('pit_universe_methodology_gate_passed')}/{expected_goal_methodology_gate}; current_gate={current.get('pit_universe_methodology_gate_passed')}/{expected_current_methodology_gate}; historical={pit_methodology.get('promotion_gate_passed')}; goal_forward={goal_forward_ready}; current_forward={current_forward_ready}",
            "V5.10 与当前主线必须共用“控制审计通过，且历史或独立前推路线通过”的真值表。",
        ),
        check(
            "full_refresh_dependency_order",
            source_order_once(function_block(full_refresh_source, "def refresh_commands", "def self_check"), "build_v5_21_rebound_leader_new_pit_source_discovery.py", "build_v5_10_rebound_leader_goal_completion_audit.py"),
            "V5.21 must precede the final V5.10 command",
            "完整刷新链必须先生成 V5.21，再生成唯一的 V5.10。",
        ),
        check(
            "current_runner_rebuilds_goal_audit",
            source_order_once(function_block(current_runner_source, "def refresh_input_commands", "def run_commands"), "audit_pit_universe_methodology.py", "build_v5_11_rebound_leader_pit_valuation_audit.py", "build_v5_12_rebound_leader_pit_valuation_percentile_audit.py", "build_v5_20_rebound_leader_evidence_boundary_audit.py", "build_v5_10_rebound_leader_goal_completion_audit.py"),
            "current runner rebuilds PIT methodology, V5.11, V5.12, V5.20, then the single final V5.10",
            "当前输入刷新链必须先审计方法门，再重建估值证据与证据边界，最后生成唯一 V5.10。",
        ),
        check(
            "full_refresh_pit_methodology_order",
            source_order_once(function_block(full_refresh_source, "def refresh_commands", "def self_check"), "audit_pit_universe_methodology.py", "build_v5_11_rebound_leader_pit_valuation_audit.py", "build_v5_12_rebound_leader_pit_valuation_percentile_audit.py", "build_v5_20_rebound_leader_evidence_boundary_audit.py", "build_v5_10_rebound_leader_goal_completion_audit.py"),
            "full refresh orders PIT methodology before V5.11/V5.12/V5.20/V5.10",
            "完整刷新链也必须遵守同一方法门依赖顺序。",
        ),
        check(
            "current_research_boundary",
            current.get("policy_status") == "research_only"
            and current.get("action") == "NO_ACTION"
            and current.get("production_ready") is False
            and current.get("auto_execution_allowed") is False,
            f"policy={current.get('policy_status')}; action={current.get('action')}; production={current.get('production_ready')}; auto={current.get('auto_execution_allowed')}",
            "当前主线必须保持 research_only、NO_ACTION、production_ready=false、auto=false。",
        ),
        check(
            "manual_support_not_ready",
            completion.get("manual_decision_support_ready") is False,
            f"manual_decision_support_ready={completion.get('manual_decision_support_ready')}",
            "人工辅助交易未满足全部门禁时必须保持未就绪。",
        ),
        check(
            "strong_industry_alpha_unvalidated",
            goal.get("goal_ready") is False,
            f"goal_ready={goal.get('goal_ready')}; nonpass={goal.get('blocking_nonpass_count')}",
            "强行业 Alpha 未经完整目标审计通过时必须保持未验证。",
        ),
    ])
    return checks


def cohort_pair(payload: Mapping[str, Any], version: str = "") -> tuple[str, str]:
    cohort_id = str(payload.get("active_cohort_id") or payload.get("cohort_id") or "")
    manifest_hash = str(
        payload.get("active_cohort_manifest_hash")
        or payload.get("cohort_manifest_hash")
        or (payload.get("manifest_hash") if version == "V5.31" else "")
        or ""
    )
    return cohort_id, manifest_hash


def source_order(source: str, earlier: str, later: str) -> bool:
    first = source.find(earlier)
    second = source.rfind(later)
    return first >= 0 and second >= 0 and first < second


def source_order_once(source: str, *ordered_items: str) -> bool:
    if not ordered_items or any(source.count(item) != 1 for item in ordered_items):
        return False
    positions = [source.find(item) for item in ordered_items]
    return positions == sorted(positions)


def function_block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    end = source.find(end_marker, start + len(start_marker)) if start >= 0 else -1
    return source[start:end] if start >= 0 and end > start else ""


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def check(check_id: str, passed: bool, evidence: str, requirement: str) -> dict[str, str]:
    return {
        "check_id": check_id,
        "status": "pass" if passed else "fail",
        "evidence": evidence,
        "requirement": requirement,
    }


def build_summary(
    checks: list[dict[str, str]],
    active: Mapping[str, Any],
    current: Mapping[str, Any],
    goal: Mapping[str, Any],
    pit_discovery: Mapping[str, Any],
    pit_methodology: Mapping[str, Any],
    *,
    promotion: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fail_count = sum(row["status"] != "pass" for row in checks)
    promotion = promotion or {}
    verified_timing = verified_forward_timing_ready(promotion)
    verified_industry = verified_forward_industry_ready(promotion)
    verified_forward = verified_forward_evidence_ready(promotion)
    return {
        "schema_version": "1.0.0",
        "policy_id": "current_state_consistency",
        "policy_status": "research_only",
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "current_date": date.today().isoformat(),
        "active_cohort_id": str(active.get("cohort_id", "")),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", "")),
        "active_cohort_validated": active.get("freeze_passed") is True,
        "current_as_of_date": str(current.get("as_of_date", "")),
        "current_action": str(current.get("action", "")),
        "goal_audit_generated_at": str(goal.get("generated_at", "")),
        "pit_discovery_generated_at": str(pit_discovery.get("generated_at", "")),
        "pit_methodology_generated_at": str(pit_methodology.get("generated_at", "")),
        "pit_universe_methodology_gate_passed": bool(current.get("pit_universe_methodology_gate_passed", False)),
        "historical_pit_universe_promotion_gate_passed": bool(pit_methodology.get("promotion_gate_passed", False)),
        "forward_timing_evidence_verified": verified_timing,
        "forward_industry_evidence_verified": verified_industry,
        "true_forward_route_ready": verified_forward,
        "forward_route_integrity_blocker": "" if verified_forward else FORWARD_EVIDENCE_ROUTE_BLOCKER,
        "promotion_eligible_valuation_row_count": int(pit_methodology.get("promotion_eligible_valuation_row_count", 0) or 0),
        "check_count": len(checks),
        "pass_count": len(checks) - fail_count,
        "fail_count": fail_count,
        "state_consistent": fail_count == 0,
        "manual_decision_support_ready": False,
        "strong_industry_alpha_validated": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": (
            "当前状态源已对齐；研究结论仍为 research_only / NO_ACTION。"
            if fail_count == 0
            else "当前状态源存在冲突或陈旧依赖；失败关闭并保持 NO_ACTION。"
        ),
    }


def write_outputs(
    summary: Mapping[str, Any],
    checks: list[dict[str, str]],
    raw_active: Mapping[str, Any],
    active: Mapping[str, Any],
    cohort_summaries: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Any],
    goal: Mapping[str, Any],
    pit_discovery: Mapping[str, Any],
    pit_methodology: Mapping[str, Any],
    promotion: Mapping[str, Any],
) -> None:
    DEBUG.mkdir(parents=True, exist_ok=True)
    fields = ["check_id", "status", "evidence", "requirement"]
    atomic_write_csv(OUT / "top_candidates.csv", checks, fieldnames=fields)
    atomic_write_csv(DEBUG / "state_source_checks.csv", checks, fieldnames=fields)
    atomic_write_json(OUT / "run_summary.json", dict(summary))
    atomic_write_json(DEBUG / "state_sources.json", {
        "raw_active": dict(raw_active),
        "validated_active": dict(active),
        "cohort_summaries": {key: dict(value) for key, value in cohort_summaries.items()},
        "current": dict(current),
        "goal": dict(goal),
        "pit_discovery": dict(pit_discovery),
        "pit_methodology": dict(pit_methodology),
        "promotion": dict(promotion),
    })
    failed = [row for row in checks if row["status"] != "pass"]
    lines = [
        "# 当前状态一致性审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- active cohort：`{summary['active_cohort_id']}`",
        f"- current as-of：`{summary['current_as_of_date']}`",
        f"- 当前动作：`{summary['current_action']}`",
        f"- 检查：{summary['pass_count']} / {summary['check_count']}",
        f"- 自动执行：`false`",
        "",
        "## 失败项",
        "",
    ]
    lines.extend(
        [f"- `{row['check_id']}`：{row['evidence']}；要求：{row['requirement']}" for row in failed]
        or ["- 无。状态源在本次快照中一致。"]
    )
    lines += [
        "",
        "## 证据边界",
        "",
        "本审计只证明状态源、刷新依赖和 cohort 绑定一致。它不证明策略有效，不解除择时、强行业、账户或组合风险门禁。",
    ]
    atomic_write_text(OUT / "report.md", "\n".join(lines) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def self_check() -> None:
    active = {"cohort_id": "c1", "manifest_hash": "a" * 64, "freeze_passed": True, "validation_reason": "verified", "created_at_utc": "2026-01-01T00:00:00Z"}
    raw_active = {**active, "verification_required": False}
    cohort = {"active_cohort_id": "c1", "active_cohort_manifest_hash": "a" * 64, "generated_at": "2026-01-02T00:00:00"}
    checks = build_checks(
        raw_active=raw_active,
        active=active,
        cohort_summaries={"V5.26": cohort, "V5.31": {"cohort_id": "c1", "manifest_hash": "a" * 64, "generated_at": "2026-01-02T00:00:00"}},
        goal={"generated_at": "2026-01-02T00:00:00", "goal_ready": False, "true_forward_route_ready": False, "pit_universe_methodology_gate_passed": False, "promotion_eligible_valuation_row_count": 0},
        pit_discovery={"generated_at": "2026-01-01T00:00:00"},
        pit_methodology={"generated_at": "2026-01-01T01:00:00", "audit_passed": True, "methodology_remediation_complete": True, "legacy_oos_label_corrected": True, "historical_review_set_label": "historical_review_used_in_iteration", "valuation_required_fields": ["trade_date", "published_at", "available_date", "fetched_at", "source_version", "revision_status"], "policy_status": "research_only", "production_ready": False, "auto_execution_allowed": False, "promotion_gate_passed": False, "historical_valuation_pit_gate_passed": False, "historical_classification_gate_passed": False, "promotion_eligible_valuation_row_count": 0, "valuation_availability_status": "unavailable_for_promotion", "classification_history_status": "unavailable", "valuation_direct_source_max_trade_date": "2025-12-31"},
        current={"policy_status": "research_only", "action": "NO_ACTION", "production_ready": False, "auto_execution_allowed": False, "forward_timing_gate_passed": False, "forward_industry_gate_passed": False, "pit_universe_methodology_gate_passed": False, "promotion_eligible_valuation_row_count": 0},
        promotion={},
        completion={"manual_decision_support_ready": False},
        current_runner_source="def refresh_input_commands\naudit_pit_universe_methodology.py build_v5_11_rebound_leader_pit_valuation_audit.py build_v5_12_rebound_leader_pit_valuation_percentile_audit.py build_v5_20_rebound_leader_evidence_boundary_audit.py build_v5_10_rebound_leader_goal_completion_audit.py\ndef run_commands",
        full_refresh_source="def refresh_commands\naudit_pit_universe_methodology.py build_v5_11_rebound_leader_pit_valuation_audit.py build_v5_12_rebound_leader_pit_valuation_percentile_audit.py build_v5_20_rebound_leader_evidence_boundary_audit.py build_v5_21_rebound_leader_new_pit_source_discovery.py build_v5_10_rebound_leader_goal_completion_audit.py\ndef self_check",
    )
    assert all(row["status"] == "pass" for row in checks)
    stale = dict(cohort)
    stale["active_cohort_id"] = "old"
    stale_checks = build_checks(
        raw_active={**raw_active, "invalidated_at_utc": "old"},
        active=active,
        cohort_summaries={"V5.26": stale},
        goal={"generated_at": "2026-01-01T00:00:00", "goal_ready": False, "true_forward_route_ready": False, "pit_universe_methodology_gate_passed": False, "promotion_eligible_valuation_row_count": 0},
        pit_discovery={"generated_at": "2026-01-02T00:00:00"},
        pit_methodology={"generated_at": "2026-01-02T01:00:00", "audit_passed": True, "methodology_remediation_complete": True, "legacy_oos_label_corrected": True, "historical_review_set_label": "historical_review_used_in_iteration", "valuation_required_fields": ["trade_date", "published_at", "available_date", "fetched_at", "source_version", "revision_status"], "policy_status": "research_only", "production_ready": False, "auto_execution_allowed": False, "promotion_gate_passed": False, "historical_valuation_pit_gate_passed": False, "historical_classification_gate_passed": False, "promotion_eligible_valuation_row_count": 0, "valuation_availability_status": "unavailable_for_promotion", "classification_history_status": "unavailable", "valuation_direct_source_max_trade_date": "2025-12-31"},
        current={"policy_status": "research_only", "action": "NO_ACTION", "production_ready": False, "auto_execution_allowed": False, "forward_timing_gate_passed": False, "forward_industry_gate_passed": False, "pit_universe_methodology_gate_passed": False, "promotion_eligible_valuation_row_count": 0},
        promotion={},
        completion={"manual_decision_support_ready": False},
        current_runner_source="def refresh_input_commands\nbuild_v5_10_rebound_leader_goal_completion_audit.py build_v5_07_rebound_leader_promotion_evaluator.py\ndef run_commands",
        full_refresh_source="def refresh_commands\nbuild_v5_10_rebound_leader_goal_completion_audit.py build_v5_21_rebound_leader_new_pit_source_discovery.py\ndef self_check",
    )
    failed = {row["check_id"] for row in stale_checks if row["status"] == "fail"}
    assert {"active_metadata_single_state", "V5.26_active_pair", "goal_audit_after_pit_discovery", "full_refresh_dependency_order", "current_runner_rebuilds_goal_audit"}.issubset(failed)
    print("self_check=pass")


if __name__ == "__main__":
    main()
