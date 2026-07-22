"""Shared fail-closed predicates for governed forward research evidence."""
from __future__ import annotations

from typing import Any, Mapping


MIN_FORWARD_INDUSTRY_EVENTS = 12
MIN_FORWARD_TIMING_EVENTS = 20
MIN_TOTAL_RULE_EVENTS = 30

# V5.07 currently materialises evidence from a mutable CSV and does not expose
# an append-only ledger that this module can replay and verify.  Summary fields
# are therefore evidence descriptions, not an integrity receipt.  Keep every
# forward route closed until a verifier is wired to an immutable ledger.
FORWARD_EVIDENCE_INTEGRITY_VERIFIER_AVAILABLE = False
FORWARD_EVIDENCE_ROUTE_BLOCKER = "append_only_forward_ledger_verifier_missing"


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _base_summary_valid(summary: Mapping[str, Any]) -> bool:
    if not FORWARD_EVIDENCE_INTEGRITY_VERIFIER_AVAILABLE:
        return False
    ledger_head = str(summary.get("forward_ledger_head_hash", ""))
    return (
        summary.get("version") == "5.07.0"
        and summary.get("policy_id") == "rebound_leader_promotion_evaluator_v5_07"
        and summary.get("policy_status") == "research_only"
        and summary.get("auto_execution_allowed") is False
        and summary.get("forward_evidence_integrity_model") == "append_only_hash_chain_v1"
        and summary.get("forward_evidence_integrity_passed") is True
        and summary.get("forward_evidence_start") == "2026-07-12"
        and summary.get("historical_backfill_eligible_count") == 0
        and summary.get("rule_mutation_detected") is False
        and len(ledger_head) == 64
        and all(character in "0123456789abcdef" for character in ledger_head)
    )


def verified_forward_timing_ready(summary: Mapping[str, Any]) -> bool:
    return (
        _base_summary_valid(summary)
        and summary.get("forward_timing_gate_passed") is True
        and int(_number(summary.get("forward_timing_event_count"))) >= MIN_FORWARD_TIMING_EVENTS
        and _number(summary.get("forward_timing_mean_return")) > 0
        and _number(summary.get("forward_timing_median_return")) > 0
        and _number(summary.get("forward_timing_win_rate")) >= 0.55
    )


def verified_forward_industry_ready(summary: Mapping[str, Any]) -> bool:
    best_forward = int(_number(summary.get("best_forward_event_count")))
    settled = int(_number(summary.get("forward_settled_event_count")))
    return (
        _base_summary_valid(summary)
        and int(_number(summary.get("passing_rule_count"))) > 0
        and summary.get("can_claim_strong_rebound_industries") is True
        and summary.get("best_status") == "pass_rebound_leader_promotion_gate"
        and int(_number(summary.get("best_event_count"))) >= MIN_TOTAL_RULE_EVENTS
        and best_forward >= MIN_FORWARD_INDUSTRY_EVENTS
        and settled >= best_forward
        and _number(summary.get("best_mean_relative_return")) > 0
        and _number(summary.get("best_top_quintile_hit_rate")) >= 0.30
    )


def verified_forward_evidence_ready(summary: Mapping[str, Any]) -> bool:
    return verified_forward_timing_ready(summary) and verified_forward_industry_ready(summary)
