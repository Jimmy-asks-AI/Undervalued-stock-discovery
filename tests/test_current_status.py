from __future__ import annotations

import copy
import json
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_current_status as current_status


def test_consistent_status_keeps_dates_boundaries_and_versions_separate() -> None:
    sources = current_status.self_check_sources()
    status = current_status.build_status(sources, current_date=date(2026, 7, 18))
    checks = current_status.build_checks(status, sources)
    summary = current_status.build_summary(status, checks)
    rendered = current_status.render_status(status, checks, summary)

    assert summary["status_valid"] is True
    assert status["current_date"] == "2026-07-18"
    assert status["decision_as_of"] == "2026-07-18"
    assert status["run_boundary_data_cutoff_date"] == "2026-07-18"
    assert status["source_cutoffs"]["valuation_history"] == "2025-12-31"
    assert status["source_cutoffs"]["pit_valuation_methodology"] == "2025-12-31"
    assert status["pit_methodology"]["promotion_gate_passed"] is False
    assert f"| 状态生成时间 | `{summary['generated_at']}` |" in rendered
    assert "只是本次请求的决策边界" in rendered
    assert "| `valuation_history` | `2025-12-31` |" in rendered
    assert "PIT估值与行业历史口径" in rendered
    assert "强行业 Alpha 未验证" in rendered
    assert "人工辅助交易未就绪" in rendered
    assert "自动交易禁止" in rendered
    assert "六角色确定性否决链" in rendered
    assert "策略版本、研究审计版本、数据治理版本和前推 cohort 是四类不同对象" in rendered
    assert status["sample_counts"]["exploratory_fund_flow_settled"] == 0
    assert status["sample_counts"]["exploratory_fund_flow_terminal_blocked"] == 0
    assert status["sample_counts"]["exploratory_fund_flow_pending"] == 1
    assert status["sample_counts"]["exploratory_fund_flow_qualified_settled"] == 0
    assert "正式探索处置尚未形成完整有效" in rendered
    assert "探索性资金流终局另行使用结算专用行情" not in rendered


def test_same_inputs_produce_byte_stable_status_and_audit_artifacts() -> None:
    sources = current_status.self_check_sources()

    first_status = current_status.build_status(copy.deepcopy(sources), current_date=date(2026, 7, 18))
    first_checks = current_status.build_checks(first_status, sources)
    first_summary = current_status.build_summary(first_status, first_checks)
    first_payloads = (
        json.dumps(first_summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
        current_status.render_status(first_status, first_checks, first_summary).encode("utf-8"),
        current_status.render_audit_report(first_status, first_checks, first_summary).encode("utf-8"),
    )

    second_status = current_status.build_status(copy.deepcopy(sources), current_date=date(2026, 7, 18))
    second_checks = current_status.build_checks(second_status, sources)
    second_summary = current_status.build_summary(second_status, second_checks)
    second_payloads = (
        json.dumps(second_summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
        current_status.render_status(second_status, second_checks, second_summary).encode("utf-8"),
        current_status.render_audit_report(second_status, second_checks, second_summary).encode("utf-8"),
    )

    assert first_summary["generated_at"] == "2026-07-18T10:00:03"
    assert first_payloads == second_payloads


def test_fund_flow_samples_are_classified_by_exact_active_pair() -> None:
    active = {"cohort_id": "active-v2", "manifest_hash": "a" * 64}
    rows = [
        {
            "observation_id": "active-settled",
            "cohort_id": "active-v2",
            "cohort_manifest_hash": "a" * 64,
            "integrity_eligible": True,
            "qualified_for_goal": True,
            "promotion_eligible": True,
            "settlement_status": "settled",
            "outcome_status": "settled_forward_observation",
        },
        {
            "observation_id": "active-pending",
            "cohort_id": "active-v2",
            "cohort_manifest_hash": "a" * 64,
            "integrity_eligible": True,
            "qualified_for_goal": False,
            "promotion_eligible": False,
        },
        {
            "observation_id": "legacy",
            "cohort_id": "legacy",
            "cohort_manifest_hash": "legacy",
            "integrity_eligible": False,
        },
        {
            "observation_id": "same-id-wrong-hash",
            "cohort_id": "active-v2",
            "cohort_manifest_hash": "b" * 64,
            "integrity_eligible": True,
            "qualified_for_goal": True,
            "promotion_eligible": True,
        },
    ]

    counts = current_status.classify_fund_flow_observations(rows, active)

    assert counts == {
        "active_fund_flow_observations": 2,
        "active_fund_flow_integrity_eligible": 2,
        "active_fund_flow_goal_qualified": 1,
        "active_fund_flow_settled_qualified": 1,
        "exploratory_fund_flow_observations": 2,
        "stale_active_pair_observations": 1,
        "global_fund_flow_observations": 4,
    }


def test_stale_current_snapshot_fails_dependency_order() -> None:
    sources = current_status.self_check_sources()
    sources["current"]["generated_at"] = "2026-07-18T09:59:59"

    status = current_status.build_status(sources, current_date=date(2026, 7, 18))
    checks = current_status.build_checks(status, sources)
    summary = current_status.build_summary(status, checks)
    stale_check = next(row for row in checks if row["check_id"] == "current_after_goal_audit")

    assert stale_check["status"] == "fail"
    assert summary["state_source_consistency_passed"] is False
    assert summary["status_valid"] is False


def test_same_cohort_id_with_stale_hash_fails_closed() -> None:
    sources = current_status.self_check_sources()
    sources["fund_flow_records"] = [
        {
            "observation_id": "stale-pair",
            "cohort_id": "active-v1",
            "cohort_manifest_hash": "b" * 64,
            "integrity_eligible": True,
            "qualified_for_goal": True,
            "promotion_eligible": True,
        }
    ]

    status = current_status.build_status(sources, current_date=date(2026, 7, 18))
    checks = current_status.build_checks(status, sources)
    row = next(item for item in checks if item["check_id"] == "no_stale_pair_counted_active")

    assert status["sample_counts"]["active_fund_flow_observations"] == 0
    assert status["sample_counts"]["exploratory_fund_flow_observations"] == 1
    assert status["sample_counts"]["stale_active_pair_observations"] == 1
    assert row["status"] == "fail"


def test_missing_governance_coverage_is_pending_but_status_invalid() -> None:
    sources = copy.deepcopy(current_status.self_check_sources())
    sources["governance_coverage"] = {}
    sources["source_presence"]["governance_coverage"] = False

    status = current_status.build_status(sources, current_date=date(2026, 7, 18))
    checks = current_status.build_checks(status, sources)
    summary = current_status.build_summary(status, checks)

    assert status["governance"]["coverage"]["status"] == "pending_missing"
    assert status["governance"]["ready"] is False
    assert next(row for row in checks if row["check_id"] == "governance_coverage_present")["status"] == "fail"
    assert summary["state_source_consistency_passed"] is True
    assert summary["governance_ready"] is False
    assert summary["status_valid"] is False


def test_late_backfill_count_deduplicates_candidate_and_benchmark_events() -> None:
    candidate = [{"observation_id": "obs-1", "late_backfill_excluded": True}]
    benchmark = [
        {"observation_id": "obs-1", "late_backfill_excluded": True},
        {"observation_id": "obs-2", "late_backfill_excluded": "True"},
    ]

    assert current_status.late_backfill_observation_count(candidate, benchmark) == 2


def test_valid_exploratory_disposition_is_rendered_without_promoting_samples() -> None:
    sources = copy.deepcopy(current_status.self_check_sources())
    sources["fund_flow_records"] = [
        {
            "observation_id": f"legacy-{index}",
            "cohort_id": "legacy",
            "cohort_manifest_hash": "legacy",
            "sample_scope": "exploratory_fund_flow_only",
            "integrity_eligible": False,
            "qualified_for_goal": False,
            "promotion_eligible": False,
        }
        for index in range(4)
    ]
    sources["exploratory_disposition_artifact_present"] = True
    sources["exploratory_disposition_error"] = ""
    sources["exploratory_disposition"] = {
        "artifact_present": True,
        "valid": True,
        "generated_at": "2026-07-21T15:30:00+08:00",
        "completion_status": "complete_terminal_exclusions",
        "observation_count": 4,
        "settled_count": 0,
        "terminal_blocked_count": 4,
        "pending_count": 0,
        "qualified_settled_count": 0,
        "settlement_disposition_complete": True,
        "return_values_present": False,
        "active_cohort_id": "active-v1",
        "active_cohort_manifest_hash": "a" * 64,
    }
    sources["exploratory_disposition_summary"] = {
        "completion_status": "complete_terminal_exclusions",
        "settlement_disposition_complete": True,
        "calendar_dates_valid": True,
        "calendar_validation": {
            "entry_date": "2026-06-23",
            "exit_date": "2026-07-21",
        },
        "price_coverage": {
            "exact_coverage_ready": True,
            "price_values_retained": False,
            "entry_exit_common_count": 123,
            "candidate_common_count": 4,
        },
    }

    status = current_status.build_status(sources, current_date=date(2026, 7, 21))
    checks = current_status.build_checks(status, sources)
    summary = current_status.build_summary(status, checks)
    rendered = current_status.render_status(status, checks, summary)

    assert summary["status_valid"] is True
    assert summary["exploratory_disposition_valid"] is True
    assert summary["exploratory_completion_status"] == "complete_terminal_exclusions"
    assert summary["exploratory_observation_count"] == 4
    assert summary["exploratory_settled_count"] == 0
    assert summary["exploratory_terminal_blocked_count"] == 4
    assert summary["exploratory_pending_count"] == 0
    assert summary["exploratory_qualified_settled_count"] == 0
    assert status["sample_counts"]["exploratory_fund_flow_observations"] == 4
    assert status["sample_counts"]["exploratory_fund_flow_settled"] == 0
    assert status["sample_counts"]["exploratory_fund_flow_terminal_blocked"] == 4
    assert status["sample_counts"]["exploratory_fund_flow_pending"] == 0
    assert status["sample_counts"]["exploratory_fund_flow_qualified_settled"] == 0
    assert status["exploratory_settlement_data_boundary"] == {
        "available": True,
        "scope": "settlement_only",
        "entry_date": "2026-06-23",
        "exit_date": "2026-07-21",
        "exact_common_industry_count": 123,
        "target_common_count": 4,
        "target_count": 4,
        "return_values_present": False,
    }
    assert summary["exploratory_settlement_data_boundary"]["available"] is True
    assert "settled 0 / terminal blocked 4 / pending 0 / qualified settled 0" in rendered
    assert "探索性资金流终局另行使用结算专用行情" in rendered
    assert "`2026-06-23` 与退出日 `2026-07-21`" in rendered
    assert "同一行业交集为 123，目标 4/4" in rendered
    assert "不改写决策 as-of `2026-07-18`" in rendered
    assert f"`industry_history={status['source_cutoffs']['industry_history']}`" in rendered
    assert "不计算或补录收益" in rendered
    assert status["strong_industry_alpha_validated"] is False


def test_present_but_invalid_exploratory_disposition_fails_closed() -> None:
    sources = copy.deepcopy(current_status.self_check_sources())
    sources["exploratory_disposition_artifact_present"] = True
    sources["exploratory_disposition_error"] = "formal package tampered"
    sources["exploratory_disposition"] = {}

    status = current_status.build_status(sources, current_date=date(2026, 7, 21))
    checks = current_status.build_checks(status, sources)
    summary = current_status.build_summary(status, checks)
    row = next(
        item for item in checks
        if item["check_id"] == "exploratory_disposition_valid_if_present"
    )

    assert row["status"] == "fail"
    assert summary["status_valid"] is False
    assert summary["exploratory_disposition_artifact_present"] is True
    assert summary["exploratory_disposition_valid"] is False
    assert summary["exploratory_completion_status"] == "invalid_fail_closed"
    assert status["sample_counts"]["exploratory_fund_flow_terminal_blocked"] == 0
    assert status["sample_counts"]["exploratory_fund_flow_pending"] == 1


def test_forged_normalized_disposition_with_wrong_active_pair_fails_closed() -> None:
    sources = copy.deepcopy(current_status.self_check_sources())
    sources["fund_flow_records"] = [
        {
            "observation_id": f"legacy-{index}",
            "cohort_id": "legacy",
            "cohort_manifest_hash": "legacy",
            "sample_scope": "exploratory_fund_flow_only",
        }
        for index in range(4)
    ]
    sources["exploratory_disposition_artifact_present"] = True
    sources["exploratory_disposition_error"] = ""
    sources["exploratory_disposition"] = {
        "artifact_present": True,
        "valid": True,
        "completion_status": "complete_terminal_exclusions",
        "observation_count": 4,
        "settled_count": 0,
        "terminal_blocked_count": 4,
        "pending_count": 0,
        "qualified_settled_count": 0,
        "settlement_disposition_complete": True,
        "return_values_present": False,
        "active_cohort_id": "active-v1",
        "active_cohort_manifest_hash": "b" * 64,
    }

    status = current_status.build_status(sources, current_date=date(2026, 7, 21))
    checks = current_status.build_checks(status, sources)
    row = next(
        item for item in checks
        if item["check_id"] == "exploratory_disposition_valid_if_present"
    )

    assert row["status"] == "fail"


def test_status_rejects_forged_promotion_when_methodology_control_failed() -> None:
    sources = copy.deepcopy(current_status.self_check_sources())
    sources["pit_methodology"].update({"audit_passed": False, "promotion_gate_passed": True})
    sources["current"]["pit_universe_methodology_gate_passed"] = True
    sources["v510"]["pit_universe_methodology_gate_passed"] = True
    sources["current"]["blocking_gates"] = [
        gate for gate in sources["current"]["blocking_gates"] if gate != "pit_universe_methodology"
    ]

    status = current_status.build_status(sources, current_date=date(2026, 7, 18))
    checks = current_status.build_checks(status, sources)
    row = next(item for item in checks if item["check_id"] == "pit_methodology_route_consistent")
    assert row["status"] == "fail"


def test_status_rejects_self_declared_forward_route_without_ledger_verifier() -> None:
    sources = copy.deepcopy(current_status.self_check_sources())
    sources["current"].update({
        "forward_timing_gate_passed": True,
        "forward_industry_gate_passed": True,
        "pit_universe_methodology_gate_passed": True,
    })
    sources["current"]["blocking_gates"] = [
        gate for gate in sources["current"]["blocking_gates"] if gate != "pit_universe_methodology"
    ]
    sources["v510"].update({"true_forward_route_ready": True, "pit_universe_methodology_gate_passed": True})

    status = current_status.build_status(sources, current_date=date(2026, 7, 18))
    checks = current_status.build_checks(status, sources)
    row = next(item for item in checks if item["check_id"] == "pit_methodology_route_consistent")
    assert row["status"] == "fail"
