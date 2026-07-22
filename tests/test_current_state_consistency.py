from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_current_state_consistency as state_audit


def base_inputs() -> dict:
    active = {
        "cohort_id": "active-v3",
        "manifest_hash": "a" * 64,
        "freeze_passed": True,
        "validation_reason": "verified",
        "created_at_utc": "2026-07-18T10:00:00Z",
    }
    return {
        "raw_active": {**active, "verification_required": False},
        "active": active,
        "cohort_summaries": {
            "V5.26": {
                "active_cohort_id": "active-v3",
                "active_cohort_manifest_hash": "a" * 64,
                "generated_at": "2026-07-18T10:01:00",
            }
        },
        "goal": {"generated_at": "2026-07-18T10:01:00", "goal_ready": False, "true_forward_route_ready": False, "pit_universe_methodology_gate_passed": False, "promotion_eligible_valuation_row_count": 0},
        "pit_discovery": {"generated_at": "2026-07-18T10:00:00"},
        "pit_methodology": {"generated_at": "2026-07-18T10:00:30", "audit_passed": True, "methodology_remediation_complete": True, "legacy_oos_label_corrected": True, "historical_review_set_label": "historical_review_used_in_iteration", "valuation_required_fields": ["trade_date", "published_at", "available_date", "fetched_at", "source_version", "revision_status"], "policy_status": "research_only", "production_ready": False, "auto_execution_allowed": False, "promotion_gate_passed": False, "historical_valuation_pit_gate_passed": False, "historical_classification_gate_passed": False, "promotion_eligible_valuation_row_count": 0, "valuation_availability_status": "unavailable_for_promotion", "classification_history_status": "unavailable", "valuation_direct_source_max_trade_date": "2025-12-31"},
        "current": {
            "policy_status": "research_only",
            "action": "NO_ACTION",
            "production_ready": False,
            "auto_execution_allowed": False,
            "pit_universe_methodology_gate_passed": False,
            "forward_timing_gate_passed": False,
            "forward_industry_gate_passed": False,
            "promotion_eligible_valuation_row_count": 0,
        },
        "promotion": {},
        "completion": {"manual_decision_support_ready": False},
        "current_runner_source": "def refresh_input_commands\naudit_pit_universe_methodology.py build_v5_07_rebound_leader_promotion_evaluator.py build_v5_21_rebound_leader_new_pit_source_discovery.py build_v5_11_rebound_leader_pit_valuation_audit.py build_v5_12_rebound_leader_pit_valuation_percentile_audit.py build_v5_20_rebound_leader_evidence_boundary_audit.py build_v5_10_rebound_leader_goal_completion_audit.py\ndef run_commands",
        "full_refresh_source": "def refresh_commands\naudit_pit_universe_methodology.py build_v5_11_rebound_leader_pit_valuation_audit.py build_v5_12_rebound_leader_pit_valuation_percentile_audit.py build_v5_20_rebound_leader_evidence_boundary_audit.py build_v5_21_rebound_leader_new_pit_source_discovery.py build_v5_10_rebound_leader_goal_completion_audit.py\ndef self_check",
    }


def test_consistent_current_state_passes() -> None:
    checks = state_audit.build_checks(**base_inputs())
    assert checks
    assert all(row["status"] == "pass" for row in checks)


def test_cohort_mismatch_fails_closed() -> None:
    inputs = base_inputs()
    inputs["cohort_summaries"]["V5.26"]["active_cohort_id"] = "superseded-v2"
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "V5.26_active_pair")
    assert row["status"] == "fail"


def test_same_pair_summary_older_than_active_cohort_is_stale() -> None:
    inputs = base_inputs()
    inputs["cohort_summaries"]["V5.26"]["generated_at"] = "2026-07-18T09:59:59"
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "V5.26_not_stale")
    assert row["status"] == "fail"


def test_verified_active_pointer_cannot_retain_invalidation() -> None:
    inputs = base_inputs()
    inputs["raw_active"]["invalidated_at_utc"] = "2026-07-18T09:00:00Z"
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "active_metadata_single_state")
    assert row["status"] == "fail"


def test_goal_audit_must_follow_pit_discovery() -> None:
    inputs = base_inputs()
    inputs["goal"]["generated_at"] = "2026-07-18T09:59:59"
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "goal_audit_after_pit_discovery")
    assert row["status"] == "fail"


def test_current_runner_must_rebuild_v521_before_final_v510() -> None:
    inputs = base_inputs()
    inputs["current_runner_source"] = "def refresh_input_commands\nbuild_v5_07_rebound_leader_promotion_evaluator.py build_v5_10_rebound_leader_goal_completion_audit.py\ndef run_commands"
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "current_runner_rebuilds_goal_audit")
    assert row["status"] == "fail"


def test_missing_checkpoint_validation_fails_closed_without_enabling_execution() -> None:
    inputs = base_inputs()
    inputs["active"] = {}
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "active_cohort_verified")
    summary = state_audit.build_summary(checks, inputs["active"], inputs["current"], inputs["goal"], inputs["pit_discovery"], inputs["pit_methodology"])
    assert row["status"] == "fail"
    assert summary["state_consistent"] is False
    assert summary["current_action"] == "NO_ACTION"
    assert summary["auto_execution_allowed"] is False


def test_wrong_manifest_hash_in_current_summary_fails_closed() -> None:
    inputs = base_inputs()
    inputs["cohort_summaries"]["V5.26"]["active_cohort_manifest_hash"] = "b" * 64
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "V5.26_active_pair")
    assert row["status"] == "fail"


def test_current_boundaries_are_hard_requirements() -> None:
    inputs = base_inputs()
    inputs["current"]["auto_execution_allowed"] = True
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "current_research_boundary")
    assert row["status"] == "fail"


def test_forged_historical_gate_cannot_bypass_failed_methodology_audit() -> None:
    inputs = base_inputs()
    inputs["pit_methodology"].update({"audit_passed": False, "promotion_gate_passed": True})
    inputs["goal"]["pit_universe_methodology_gate_passed"] = True
    inputs["current"]["pit_universe_methodology_gate_passed"] = True
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "pit_methodology_propagated_to_goal_and_current")
    assert row["status"] == "fail"


def test_self_declared_forward_route_cannot_bypass_missing_ledger_verifier() -> None:
    inputs = base_inputs()
    inputs["promotion"] = {
        "version": "5.07.0",
        "policy_id": "rebound_leader_promotion_evaluator_v5_07",
        "policy_status": "research_only",
        "auto_execution_allowed": False,
        "forward_evidence_integrity_model": "append_only_hash_chain_v1",
        "forward_evidence_integrity_passed": True,
        "forward_evidence_start": "2026-07-12",
        "historical_backfill_eligible_count": 0,
        "rule_mutation_detected": False,
        "forward_ledger_head_hash": "a" * 64,
        "forward_timing_gate_passed": True,
        "forward_timing_event_count": 20,
        "forward_timing_mean_return": 0.01,
        "forward_timing_median_return": 0.01,
        "forward_timing_win_rate": 0.60,
        "passing_rule_count": 1,
        "can_claim_strong_rebound_industries": True,
        "best_status": "pass_rebound_leader_promotion_gate",
        "best_event_count": 30,
        "best_forward_event_count": 12,
        "forward_settled_event_count": 20,
        "best_mean_relative_return": 0.02,
        "best_top_quintile_hit_rate": 0.35,
    }
    inputs["goal"].update({"true_forward_route_ready": True, "pit_universe_methodology_gate_passed": True})
    inputs["current"].update({
        "forward_timing_gate_passed": True,
        "forward_industry_gate_passed": True,
        "pit_universe_methodology_gate_passed": True,
    })
    checks = state_audit.build_checks(**inputs)
    row = next(item for item in checks if item["check_id"] == "pit_methodology_propagated_to_goal_and_current")
    assert row["status"] == "fail"
