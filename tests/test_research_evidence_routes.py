from __future__ import annotations

from scripts.research_evidence_routes import (
    verified_forward_evidence_ready,
    verified_forward_industry_ready,
    verified_forward_timing_ready,
)


def valid_summary() -> dict[str, object]:
    return {
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
        "forward_timing_median_return": 0.005,
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


def test_self_declared_forward_integrity_cannot_unlock_route() -> None:
    summary = valid_summary()
    assert not verified_forward_timing_ready(summary)
    assert not verified_forward_industry_ready(summary)
    assert not verified_forward_evidence_ready(summary)


def test_contradictory_or_under_sampled_summary_fails_closed() -> None:
    summary = valid_summary()
    summary.update({
        "can_claim_strong_rebound_industries": False,
        "best_status": "research_only_not_promoted",
        "best_forward_event_count": 0,
    })
    assert not verified_forward_industry_ready(summary)
    assert not verified_forward_evidence_ready(summary)

    timing = valid_summary()
    timing["forward_timing_event_count"] = 19
    assert not verified_forward_timing_ready(timing)
    assert not verified_forward_evidence_ready(timing)

    mutable_legacy = valid_summary()
    mutable_legacy["forward_evidence_integrity_model"] = "mutable_csv"
    assert not verified_forward_timing_ready(mutable_legacy)
    assert not verified_forward_evidence_ready(mutable_legacy)
