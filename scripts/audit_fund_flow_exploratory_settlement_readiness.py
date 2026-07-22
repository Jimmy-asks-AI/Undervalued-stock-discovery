#!/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from fund_flow_exploratory_price_contract import price_cache_lock, price_cache_snapshot
from fund_flow_forward_evidence import (
    checkpoint_path_for,
    materialize_observations,
    read_events,
    verify_ledger_checkpoint,
)
from research_integrity import atomic_write_csv, atomic_write_json, atomic_write_text, file_sha256


ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
START_GATE = datetime(2026, 7, 21, 15, 0, 0, tzinfo=SHANGHAI)
SIGNAL_DATE = "2026-06-22"
ENTRY_DATE = "2026-06-23"
EXIT_DATE = "2026-07-21"
MIN_BENCHMARK_INDUSTRIES = 100
EXPECTED_QUARANTINED_HISTORY_CODES = {"801156"}
EXPECTED_QUARANTINE_REASON = "provider_history_incompatible_with_append_only_cache"

LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
LEDGER_CSV = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv"
ENTRY_FREEZE = ROOT / "logs" / "v5_33_fund_flow_entry_price_freeze.jsonl"
BENCHMARK_FREEZE = ROOT / "logs" / "v5_34_fund_flow_benchmark_entry_freeze.jsonl"
ACTIVE_COHORT = ROOT / "logs" / "v5_31_fund_flow_evidence_freeze_active.json"
COHORT_HISTORY = ROOT / "logs" / "v5_31_fund_flow_evidence_freeze_history.jsonl"
INTEGRITY = ROOT / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30" / "run_summary.json"
CURRENT_STATE = ROOT / "outputs" / "audit" / "current_state_consistency" / "run_summary.json"
PRICE_REFRESH = ROOT / "outputs" / "audit" / "fund_flow_exploratory_settlement_price_refresh_2026_07_21" / "run_summary.json"
PRICE_DIR = (
    ROOT
    / "data_catalog"
    / "cache"
    / "industry_index"
    / "history"
    / "settlement_2026_07_21"
    / "second"
)
BASELINE_PRICE_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
REFRESH_PRODUCER_PATHS = (
    ROOT / "scripts" / "refresh_fund_flow_exploratory_settlement_prices.py",
    ROOT / "scripts" / "run_industry_index_research_validation.py",
    ROOT / "scripts" / "fund_flow_exploratory_price_contract.py",
)
EXPECTED_REFRESH_PRODUCER_RELATIVE_PATHS = {
    path.resolve().relative_to(ROOT.resolve()).as_posix()
    for path in REFRESH_PRODUCER_PATHS
}
POLICY = ROOT / "configs" / "fund_flow_forward_chain_policy.json"
SCHEMA = ROOT / "configs" / "fund_flow_forward_ledger_schema.json"
CALENDAR = ROOT / "logs" / "v5_25_fund_flow_forward_sources" / "calendars" / "f348dd4c8863a5f2a5ff543a427c36198dbe3ca00f01a268f736490b2989d975.csv"
V527 = ROOT / "scripts" / "settle_v5_27_fund_flow_forward_samples.py"
THIS_SCRIPT = Path(__file__).resolve()

PREFLIGHT_OUT = ROOT / "outputs" / "audit" / "fund_flow_exploratory_settlement_preflight"
FINAL_OUT = ROOT / "outputs" / "audit" / "fund_flow_exploratory_settlement_2026_07_21"

EXPECTED: dict[str, tuple[str, str]] = {
    "ffobs-fef718698a6c9566ae3c49c7": ("801194", "保险Ⅱ"),
    "ffobs-dfe49086cdac49809130aedd": ("801125", "白酒Ⅱ"),
    "ffobs-6cc0c1c106273b1f038a3abd": ("801764", "游戏Ⅱ"),
    "ffobs-4f4f00430698719565ca9676": ("801203", "一般零售"),
}

RETURN_FIELDS = (
    "actual_entry_date",
    "actual_exit_date",
    "realized_return",
    "benchmark_return",
    "realized_relative_return",
    "future_return_rank_pct",
    "future_top_quintile",
)

SENSITIVE_PATHS = (
    LEDGER,
    LEDGER_CSV,
    checkpoint_path_for(LEDGER),
    ENTRY_FREEZE,
    checkpoint_path_for(ENTRY_FREEZE),
    BENCHMARK_FREEZE,
    checkpoint_path_for(BENCHMARK_FREEZE),
    ACTIVE_COHORT,
    COHORT_HISTORY,
    checkpoint_path_for(COHORT_HISTORY),
    INTEGRITY,
    CURRENT_STATE,
    PRICE_REFRESH,
    CALENDAR,
    *REFRESH_PRODUCER_PATHS,
)

EVIDENCE_PATHS = (
    LEDGER,
    LEDGER_CSV,
    ENTRY_FREEZE,
    BENCHMARK_FREEZE,
    ACTIVE_COHORT,
    COHORT_HISTORY,
    INTEGRITY,
    CURRENT_STATE,
    PRICE_REFRESH,
    POLICY,
    SCHEMA,
    CALENDAR,
    V527,
    THIS_SCRIPT,
    *REFRESH_PRODUCER_PATHS,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed readiness and terminal-disposition audit for the four legacy exploratory observations."
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Never inspect price files; write the blocked pre-start evidence package only.",
    )
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0

    summary, out = execute_readiness_audit(
        observed_at=datetime.now(SHANGHAI),
        preflight_requested=args.preflight,
    )
    print(f"output_dir={relative_path(out)}")
    print(f"completion_status={summary['completion_status']}")
    print(f"records={summary['record_count']}; settled=0; blocked={summary['blocked_count']}; pending={summary['pending_count']}")
    return 0 if args.preflight or summary["settlement_disposition_complete"] else 3


def execute_readiness_audit(
    *,
    observed_at: datetime,
    preflight_requested: bool,
    before_publish_hook: Callable[[], None] | None = None,
    output_publisher: Callable[[Path, Path], None] | None = None,
) -> tuple[dict[str, Any], Path]:
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must carry an explicit timezone")
    observed_at = observed_at.astimezone(SHANGHAI)
    output_publisher = output_publisher or publish_output_directory
    lock_dirs = sorted(
        {PRICE_DIR.resolve(), BASELINE_PRICE_DIR.resolve()},
        key=lambda path: path.as_posix(),
    )
    with contextlib.ExitStack() as stack:
        for lock_dir in lock_dirs:
            stack.enter_context(price_cache_lock(lock_dir))
        active_manifest = resolve_active_manifest_path()
        sensitive_paths = list(SENSITIVE_PATHS) + ([active_manifest] if active_manifest else [])
        inspect_prices = not preflight_requested and observed_at >= START_GATE
        if inspect_prices:
            sensitive_paths.extend(sorted(PRICE_DIR.glob("*.csv")))
            sensitive_paths.extend(sorted(BASELINE_PRICE_DIR.glob("*.csv")))
        before_hashes = snapshot_hashes(sensitive_paths)
        price_before = price_cache_snapshot(PRICE_DIR) if inspect_prices else {}
        baseline_before = (
            price_cache_snapshot(BASELINE_PRICE_DIR) if inspect_prices else {}
        )
        baseline_quarantine_before = (
            quarantined_file_hashes(BASELINE_PRICE_DIR) if inspect_prices else {}
        )
        source = collect_readonly_sources()
        source["price_cache_snapshot"] = dict(price_before)
        source["baseline_price_cache_snapshot"] = dict(baseline_before)
        source["baseline_quarantined_file_sha256"] = dict(
            baseline_quarantine_before
        )
        price_loader: Callable[[], dict[str, Any]] | None = None
        if not preflight_requested:
            price_loader = lambda: scan_exact_date_coverage(PRICE_DIR)
        summary, dispositions, date_coverage = assess(
            observed_at,
            source,
            price_coverage_loader=price_loader,
        )
        audit_mode = select_audit_mode(
            observed_at,
            preflight_requested=preflight_requested,
            settlement_disposition_complete=bool(summary["settlement_disposition_complete"]),
        )
        summary["audit_mode"] = audit_mode
        out = output_for_mode(audit_mode)
        evidence_paths = list(EVIDENCE_PATHS)
        evidence_paths.extend(
            checkpoint_path_for(path)
            for path in (LEDGER, ENTRY_FREEZE, BENCHMARK_FREEZE, COHORT_HISTORY)
        )
        if active_manifest:
            evidence_paths.append(active_manifest)
        if summary["exit_data_read"]:
            evidence_paths.extend(sorted(PRICE_DIR.glob("*.csv")))
            evidence_paths.extend(sorted(BASELINE_PRICE_DIR.glob("*.csv")))
        evidence = evidence_manifest(evidence_paths)
        pre_snapshot = build_source_snapshot(source, before_hashes, phase="pre")
        pre_snapshot["price_cache_snapshot"] = dict(price_before)
        pre_snapshot["baseline_price_cache_snapshot"] = dict(baseline_before)
        post_source = collect_readonly_sources()
        after_hashes = snapshot_hashes(sensitive_paths)
        price_after = price_cache_snapshot(PRICE_DIR) if inspect_prices else {}
        baseline_after = (
            price_cache_snapshot(BASELINE_PRICE_DIR) if inspect_prices else {}
        )
        assert_audit_inputs_unchanged(
            before_hashes,
            after_hashes,
            price_before,
            price_after,
            baseline_before,
            baseline_after,
        )
        post_source["price_cache_snapshot"] = dict(price_after)
        post_source["baseline_price_cache_snapshot"] = dict(baseline_after)
        post_source["baseline_quarantined_file_sha256"] = dict(
            baseline_quarantine_before
        )
        post_snapshot = build_source_snapshot(post_source, after_hashes, phase="post")
        post_snapshot["price_cache_snapshot"] = dict(price_after)
        post_snapshot["baseline_price_cache_snapshot"] = dict(baseline_after)
        post_snapshot["authoritative_hashes_unchanged"] = True

        out.parent.mkdir(parents=True, exist_ok=True)
        staged_out = Path(
            tempfile.mkdtemp(prefix=f".{out.name}.staging-", dir=out.parent)
        )
        try:
            write_outputs(
                staged_out,
                summary,
                dispositions,
                evidence,
                date_coverage,
                pre_snapshot,
                post_snapshot,
            )
            if before_publish_hook is not None:
                before_publish_hook()
            final_hashes = snapshot_hashes(sensitive_paths)
            final_price = price_cache_snapshot(PRICE_DIR) if inspect_prices else {}
            final_baseline = (
                price_cache_snapshot(BASELINE_PRICE_DIR) if inspect_prices else {}
            )
            assert_audit_inputs_unchanged(
                before_hashes,
                final_hashes,
                price_before,
                final_price,
                baseline_before,
                final_baseline,
            )
            output_publisher(staged_out, out)
            if staged_out.exists() or not (out / "run_summary.json").is_file():
                raise RuntimeError("audit output publisher did not commit the staged package")
        finally:
            if staged_out.exists():
                safe_remove_output_tree(staged_out, out.parent, f".{out.name}.staging-")
        return summary, out


def select_audit_mode(
    observed_at: datetime,
    *,
    preflight_requested: bool,
    settlement_disposition_complete: bool,
) -> str:
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must carry an explicit timezone")
    if preflight_requested or observed_at.astimezone(SHANGHAI) < START_GATE:
        return "preflight"
    return "formal_disposition" if settlement_disposition_complete else "due_date_blocked_preflight"


def output_for_mode(audit_mode: str) -> Path:
    if audit_mode == "formal_disposition":
        return FINAL_OUT
    if audit_mode in {"preflight", "due_date_blocked_preflight"}:
        return PREFLIGHT_OUT
    raise ValueError(f"unknown audit mode: {audit_mode}")


def collect_readonly_sources() -> dict[str, Any]:
    checkpoints = {
        "observation_ledger": verify_ledger_checkpoint(LEDGER),
        "candidate_entry_freeze": verify_ledger_checkpoint(ENTRY_FREEZE),
        "benchmark_entry_freeze": verify_ledger_checkpoint(BENCHMARK_FREEZE),
        "cohort_history": verify_ledger_checkpoint(COHORT_HISTORY),
    }
    observations = materialize_observations(read_events(LEDGER))
    target_rows = [row for row in observations if str(row.get("observation_id", "")) in EXPECTED]
    entry_rows = read_events(ENTRY_FREEZE)
    benchmark_rows = read_events(BENCHMARK_FREEZE)
    active = validated_active_cohort()
    return {
        "observations": target_rows,
        "entry_freezes": entry_rows,
        "benchmark_freezes": benchmark_rows,
        "active_cohort": active,
        "integrity": read_json(INTEGRITY),
        "current_state": read_json(CURRENT_STATE),
        "price_refresh": read_json(PRICE_REFRESH),
        "calendar_validation": validate_calendar_dates(CALENDAR),
        "checkpoints": checkpoints,
        "global_observation_count": len(observations),
    }


def assess(
    observed_at: datetime,
    source: Mapping[str, Any],
    *,
    price_coverage_loader: Callable[[], dict[str, Any]] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must carry an explicit timezone")
    local_time = observed_at.astimezone(SHANGHAI)
    time_gate_passed = local_time >= START_GATE
    price_coverage: dict[str, Any] = {
        "checked": False,
        "reason": "time gate not passed; no price file opened",
        "source_file_count": 0,
        "valid_source_file_count": 0,
        "invalid_file_count": 0,
        "invalid_files": [],
        "entry_industry_count": 0,
        "exit_industry_count": 0,
        "entry_exit_common_count": 0,
        "candidate_entry_count": 0,
        "candidate_exit_count": 0,
        "candidate_common_count": 0,
        "minimum_benchmark_industries": MIN_BENCHMARK_INDUSTRIES,
        "overall_max_date": "",
        "price_values_retained": False,
        "exact_coverage_ready": False,
    }
    if time_gate_passed and price_coverage_loader is not None:
        price_coverage = recompute_exact_coverage(dict(price_coverage_loader()))

    observations = list(source.get("observations", []))
    entry_index, duplicate_entry_keys = index_rows(source.get("entry_freezes", []))
    benchmark_index, duplicate_benchmark_keys = index_rows(source.get("benchmark_freezes", []))
    by_id = {str(row.get("observation_id", "")): row for row in observations}
    duplicate_ids = duplicate_values(str(row.get("observation_id", "")) for row in observations)
    missing_ids = sorted(set(EXPECTED) - set(by_id))
    unexpected_ids = sorted(set(by_id) - set(EXPECTED))
    active = dict(source.get("active_cohort", {}))
    integrity = dict(source.get("integrity", {}))
    current_state = dict(source.get("current_state", {}))
    calendar_validation = dict(source.get("calendar_validation", {}))
    state_gates = assess_state_gates(active, integrity, current_state)
    price_source_gate = assess_price_source_gate(
        source.get("price_refresh", {}),
        source.get("price_cache_snapshot", {}),
        price_coverage,
        source.get("baseline_price_cache_snapshot", {}),
        source.get("baseline_quarantined_file_sha256", {}),
    )

    dispositions: list[dict[str, Any]] = []
    for observation_id, (expected_code, expected_name) in EXPECTED.items():
        row = dict(by_id.get(observation_id, {}))
        key = logical_key(row, expected_code)
        entry = dict(entry_index.get(key, {}))
        benchmark = dict(benchmark_index.get(key, {}))
        dispositions.append(
            classify_record(
                observation_id,
                expected_code,
                expected_name,
                row,
                entry,
                benchmark,
                candidate_freeze_unique=key not in duplicate_entry_keys,
                benchmark_freeze_unique=key not in duplicate_benchmark_keys,
                time_gate_passed=time_gate_passed,
                price_coverage=price_coverage,
            )
        )

    record_contract_passed = (
        not missing_ids
        and not unexpected_ids
        and not duplicate_ids
        and not duplicate_entry_keys
        and not duplicate_benchmark_keys
        and all(
        row["record_contract_passed"] for row in dispositions
        )
    )
    pending_count = sum(row["disposition"] == "pending" for row in dispositions)
    blocked_count = sum(row["disposition"] == "blocked" for row in dispositions)
    exact_ready = bool(price_coverage.get("exact_coverage_ready"))
    disposition_complete = bool(
        time_gate_passed
        and exact_ready
        and record_contract_passed
        and calendar_validation.get("calendar_dates_valid") is True
        and state_gates["all_passed"]
        and price_source_gate["all_passed"]
        and blocked_count == len(EXPECTED)
    )
    if not time_gate_passed:
        completion_status = "blocked_pre_start"
    elif not exact_ready:
        completion_status = "pending_exact_price_coverage"
    elif not record_contract_passed:
        completion_status = "blocked_source_contract"
    elif calendar_validation.get("calendar_dates_valid") is not True:
        completion_status = "blocked_calendar_dates_gate"
    elif not state_gates["active_cohort_gate_passed"]:
        completion_status = "blocked_active_cohort_gate"
    elif not state_gates["v5_30_summary_gate_passed"]:
        completion_status = "blocked_v5_30_summary_gate"
    elif not state_gates["current_state_consistency_gate_passed"]:
        completion_status = "blocked_current_state_consistency_gate"
    elif not price_source_gate["all_passed"]:
        completion_status = "blocked_price_refresh_attestation_gate"
    elif disposition_complete:
        completion_status = "complete_terminal_exclusions"
    else:
        completion_status = "blocked_unresolved"

    returns_empty = all(not str(item.get(field, "")).strip() for item in dispositions for field in RETURN_FIELDS)
    summary = {
        "schema_version": "fund-flow-exploratory-settlement-readiness-v1",
        "generated_at": local_time.isoformat(timespec="seconds"),
        "observed_at": local_time.isoformat(timespec="seconds"),
        "required_start_at": START_GATE.isoformat(timespec="seconds"),
        "policy_status": "research_only",
        "current_action": "NO_ACTION",
        "completion_status": completion_status,
        "time_gate_passed": time_gate_passed,
        "exit_data_read": bool(price_coverage.get("checked")),
        "return_values_read_or_written": False,
        "authoritative_ledgers_mutated": False,
        "record_count": len(dispositions),
        "records_found": len(observations),
        "missing_observation_ids": missing_ids,
        "unexpected_observation_ids": unexpected_ids,
        "duplicate_observation_ids": duplicate_ids,
        "duplicate_candidate_freeze_keys": ["|".join(key) for key in sorted(duplicate_entry_keys)],
        "duplicate_benchmark_freeze_keys": ["|".join(key) for key in sorted(duplicate_benchmark_keys)],
        "record_contract_passed": record_contract_passed,
        "calendar_dates_valid": calendar_validation.get("calendar_dates_valid") is True,
        "calendar_validation": calendar_validation,
        "pending_count": pending_count,
        "blocked_count": blocked_count,
        "settled_count": 0,
        "qualified_settled_count": 0,
        "exploratory_settled_count": 0,
        "settlement_disposition_complete": disposition_complete,
        "all_return_fields_empty": returns_empty,
        "active_cohort_id": str(active.get("cohort_id", "")),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", "")),
        "active_cohort_validated": state_gates["active_cohort_gate_passed"],
        "active_cohort_validation_reason": str(active.get("validation_reason", "")),
        "state_gate_passed": state_gates["all_passed"],
        "state_gate_reason_codes": state_gates["reason_codes"],
        "price_source_gate_passed": price_source_gate["all_passed"],
        "price_source_gate_checks": price_source_gate["checks"],
        "price_source_gate_reason_codes": price_source_gate["reason_codes"],
        "active_cohort_gate_passed": state_gates["active_cohort_gate_passed"],
        "active_cohort_gate_checks": state_gates["active_cohort_checks"],
        "v5_30_summary_gate_passed": state_gates["v5_30_summary_gate_passed"],
        "v5_30_summary_gate_checks": state_gates["v5_30_checks"],
        "v5_30_integrity_passed": integrity.get("integrity_passed") is True,
        "v5_30_global_integrity_passed": integrity.get("global_ledger_integrity_passed") is True,
        "v5_30_global_violation_count": as_int(integrity.get("global_violation_count")),
        "v5_30_global_late_backfill_count": as_int(integrity.get("global_late_backfill_count")),
        "v5_30_as_of_date": str(integrity.get("as_of_date", "")),
        "v5_30_generated_at": str(integrity.get("generated_at", "")),
        "v5_30_active_cohort_id": str(integrity.get("active_cohort_id", "")),
        "v5_30_active_cohort_manifest_hash": str(integrity.get("active_cohort_manifest_hash", "")),
        "current_state_consistency_gate_passed": state_gates["current_state_consistency_gate_passed"],
        "current_state_consistency_gate_checks": state_gates["current_state_checks"],
        "current_state_generated_at": str(current_state.get("generated_at", "")),
        "current_state_as_of_date": str(current_state.get("current_as_of_date", "")),
        "current_state_active_cohort_id": str(current_state.get("active_cohort_id", "")),
        "current_state_active_cohort_manifest_hash": str(current_state.get("active_cohort_manifest_hash", "")),
        "state_gate_evidence_paths": [
            relative_path(INTEGRITY),
            relative_path(CURRENT_STATE),
            relative_path(PRICE_REFRESH),
        ],
        "promotion_ready": False,
        "can_claim_strong_rebound_industries": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "price_coverage": price_coverage,
        "final_verdict": verdict(completion_status),
    }
    return summary, dispositions, price_coverage


def assess_state_gates(
    active: Mapping[str, Any],
    integrity: Mapping[str, Any],
    current_state: Mapping[str, Any],
) -> dict[str, Any]:
    active_id = str(active.get("cohort_id", ""))
    active_hash = str(active.get("manifest_hash", ""))
    active_checks = {
        "validated_active_cohort": active.get("freeze_passed") is True,
        "active_cohort_id_present": bool(active_id),
        "active_manifest_hash_is_sha256": is_sha256(active_hash),
    }

    integrity_ready_fields = [key for key in integrity if key.endswith("_ready")]
    v5_30_checks = {
        "summary_present": bool(integrity),
        "active_pair_matches": (
            bool(active_id)
            and str(integrity.get("active_cohort_id", "")) == active_id
            and str(integrity.get("active_cohort_manifest_hash", "")) == active_hash
        ),
        "active_cohort_freeze_passed": integrity.get("active_cohort_freeze_passed") is True,
        "as_of_date_is_exit_date": str(integrity.get("as_of_date", "")) == EXIT_DATE,
        "generated_on_exit_date": timestamp_is_on_date(integrity.get("generated_at"), EXIT_DATE),
        "policy_is_research_only": str(integrity.get("policy_status", "")) == "research_only",
        "integrity_is_fail_closed": integrity.get("integrity_passed") is False,
        "global_integrity_is_fail_closed": integrity.get("global_ledger_integrity_passed") is False,
        "global_legacy_rows_present": as_int(integrity.get("global_ledger_rows")) >= len(EXPECTED),
        "global_violations_present": as_int(integrity.get("global_violation_count")) > 0,
        "global_late_backfills_present": as_int(integrity.get("global_late_backfill_count")) >= 2 * len(EXPECTED),
        "failure_status_is_explicit": str(integrity.get("best_status", "")) == "research_only_ledger_integrity_failed",
        "strong_industry_claim_disabled": strict_bool(integrity.get("can_claim_strong_rebound_industries")) is False,
        "promotion_not_ready": strict_bool(integrity.get("promotion_ready")) is not True,
        "goal_not_ready": strict_bool(integrity.get("goal_ready")) is False,
        "production_not_ready": strict_bool(integrity.get("production_ready")) is False,
        "all_ready_fields_false": bool(integrity_ready_fields)
        and all(strict_bool(integrity.get(key)) is False for key in integrity_ready_fields),
        "automatic_execution_disabled": strict_bool(integrity.get("auto_execution_allowed")) is False,
    }

    current_ready_fields = [key for key in current_state if key.endswith("_ready")]
    current_state_checks = {
        "summary_present": bool(current_state),
        "active_pair_matches": (
            bool(active_id)
            and str(current_state.get("active_cohort_id", "")) == active_id
            and str(current_state.get("active_cohort_manifest_hash", "")) == active_hash
        ),
        "active_cohort_validated": current_state.get("active_cohort_validated") is True,
        "state_consistent": current_state.get("state_consistent") is True,
        "fail_count_zero": strict_int(current_state.get("fail_count")) == 0,
        "generated_on_exit_date": timestamp_is_on_date(current_state.get("generated_at"), EXIT_DATE),
        "policy_is_research_only": str(current_state.get("policy_status", "")) == "research_only",
        "current_action_is_no_action": str(current_state.get("current_action", "")) == "NO_ACTION",
        "strong_industry_alpha_not_validated": strict_bool(current_state.get("strong_industry_alpha_validated")) is False,
        "manual_decision_support_not_ready": strict_bool(current_state.get("manual_decision_support_ready")) is False,
        "production_not_ready": strict_bool(current_state.get("production_ready")) is False,
        "true_forward_route_not_ready": strict_bool(current_state.get("true_forward_route_ready")) is False,
        "all_ready_fields_false": bool(current_ready_fields)
        and all(strict_bool(current_state.get(key)) is False for key in current_ready_fields),
        "automatic_execution_disabled": strict_bool(current_state.get("auto_execution_allowed")) is False,
    }

    active_passed = all(active_checks.values())
    v5_30_passed = active_passed and all(v5_30_checks.values())
    current_state_passed = active_passed and all(current_state_checks.values())
    reason_codes = [
        *(f"active_cohort:{key}" for key, passed in active_checks.items() if not passed),
        *(f"v5_30:{key}" for key, passed in v5_30_checks.items() if not passed),
        *(f"current_state:{key}" for key, passed in current_state_checks.items() if not passed),
    ]
    return {
        "active_cohort_checks": active_checks,
        "v5_30_checks": v5_30_checks,
        "current_state_checks": current_state_checks,
        "active_cohort_gate_passed": active_passed,
        "v5_30_summary_gate_passed": v5_30_passed,
        "current_state_consistency_gate_passed": current_state_passed,
        "all_passed": active_passed and v5_30_passed and current_state_passed,
        "reason_codes": reason_codes,
    }


def assess_price_source_gate(
    refresh_summary: Mapping[str, Any],
    current_cache_snapshot: Mapping[str, Any],
    price_coverage: Mapping[str, Any],
    live_baseline_cache_snapshot: Mapping[str, Any],
    live_baseline_quarantined_file_sha256: Mapping[str, Any],
    *,
    project_root: Path | None = None,
    settlement_price_dir: Path | None = None,
    baseline_price_dir: Path | None = None,
    producer_paths: Iterable[Path] | None = None,
) -> dict[str, Any]:
    resolved_root = (project_root or ROOT).resolve()
    resolved_settlement_price_dir = (settlement_price_dir or PRICE_DIR).resolve()
    resolved_baseline_price_dir = (baseline_price_dir or BASELINE_PRICE_DIR).resolve()
    selected_producer_paths = producer_paths or REFRESH_PRODUCER_PATHS
    resolved_producer_paths = tuple(path.resolve() for path in selected_producer_paths)
    def path_label(path: Path) -> str:
        try:
            return path.relative_to(resolved_root).as_posix()
        except ValueError:
            return path.as_posix()

    expected_producer_relative_paths = {
        path_label(path) for path in resolved_producer_paths
    }
    refresh = dict(refresh_summary)
    current = dict(current_cache_snapshot)
    live_baseline = dict(live_baseline_cache_snapshot)
    refresh_coverage = dict(refresh.get("coverage", {}))
    fetch = dict(refresh.get("fetch", {}))
    continuity = dict(refresh.get("history_continuity", {}))
    commit = dict(refresh.get("commit", {}))
    staged_attestation = dict(commit.get("staged_universe_attestation", {}))
    committed_attestation = dict(commit.get("committed_universe_attestation", {}))
    authoritative_before = dict(refresh.get("authoritative_before", {}))
    authoritative_after = dict(refresh.get("authoritative_after", {}))
    cache_bootstrap = dict(refresh.get("cache_bootstrap", {}))
    baseline_before = dict(cache_bootstrap.get("baseline_before", {}))
    baseline_after = dict(cache_bootstrap.get("baseline_after", {}))
    baseline_after_refresh = dict(cache_bootstrap.get("baseline_after_refresh", {}))
    settlement_before = dict(cache_bootstrap.get("settlement_before", {}))
    settlement_after_bootstrap = dict(
        cache_bootstrap.get("settlement_after_bootstrap", {})
    )
    bootstrap_action = str(cache_bootstrap.get("action", ""))
    bootstrap_baseline_quarantine_values = cache_bootstrap.get(
        "baseline_quarantined_file_sha256"
    )
    bootstrap_baseline_quarantine_hashes = (
        dict(bootstrap_baseline_quarantine_values)
        if isinstance(bootstrap_baseline_quarantine_values, Mapping)
        else {}
    )
    bootstrap_settlement_quarantine_values = cache_bootstrap.get(
        "settlement_quarantined_file_sha256"
    )
    bootstrap_settlement_quarantine_hashes = (
        dict(bootstrap_settlement_quarantine_values)
        if isinstance(bootstrap_settlement_quarantine_values, Mapping)
        else {}
    )
    live_baseline_quarantine_hashes = dict(
        live_baseline_quarantined_file_sha256
    )
    producer_values = refresh.get("producer_attestations")
    producer_items = producer_values if isinstance(producer_values, list) else []
    producer_paths = [
        str(item.get("path", ""))
        for item in producer_items
        if isinstance(item, Mapping)
    ]
    producer_by_path = {
        str(item.get("path", "")): item
        for item in producer_items
        if isinstance(item, Mapping)
    }
    producer_attestations_valid = (
        len(producer_items) == len(expected_producer_relative_paths)
        and len(producer_paths) == len(set(producer_paths))
        and set(producer_paths) == expected_producer_relative_paths
        and all(
            canonical_int(producer_by_path[relative].get("bytes"))
            == (resolved_root / relative).stat().st_size
            and is_sha256(producer_by_path[relative].get("sha256"))
            and producer_by_path[relative].get("sha256")
            == file_sha256(resolved_root / relative)
            for relative in expected_producer_relative_paths
        )
    )
    source_count = canonical_int(refresh_coverage.get("source_file_count"))
    entry_count = canonical_int(refresh_coverage.get("entry_industry_count"))
    exit_count = canonical_int(refresh_coverage.get("exit_industry_count"))
    common_count = canonical_int(refresh_coverage.get("entry_exit_common_count"))
    target_entry = canonical_int(refresh_coverage.get("target_entry_count"))
    target_exit = canonical_int(refresh_coverage.get("target_exit_count"))
    target_common = canonical_int(refresh_coverage.get("target_common_count"))
    verified_count = canonical_int(continuity.get("verified_industry_count"))
    failed_count = canonical_int(continuity.get("failed_industry_count"))
    changed_count = canonical_int(continuity.get("historical_rows_changed"))
    target_values = refresh.get("target_industry_codes")
    target_codes = (
        {str(value) for value in target_values}
        if isinstance(target_values, list) and all(isinstance(value, str) for value in target_values)
        else set()
    )
    fetch_expected = canonical_int(fetch.get("expected_industry_count"))
    fetch_succeeded = canonical_int(fetch.get("succeeded_industry_count"))
    fetch_failed = canonical_int(fetch.get("failed_industry_count"))
    fetch_quarantined = canonical_int(fetch.get("quarantined_industry_count"))
    fetch_accounted = canonical_int(fetch.get("source_accounted_industry_count"))
    quarantine_values = fetch.get("quarantined_industry_codes")
    quarantine_list_valid = (
        isinstance(quarantine_values, list)
        and all(isinstance(value, str) for value in quarantine_values)
        and len(quarantine_values) == len(set(quarantine_values))
    )
    quarantine_codes = (
        {str(value) for value in quarantine_values}
        if quarantine_list_valid
        else set()
    )
    quarantine_attestation_values = fetch.get("quarantine_attestations")
    quarantine_attestations = (
        quarantine_attestation_values
        if isinstance(quarantine_attestation_values, list)
        else []
    )
    quarantine_attestation_codes = [
        str(item.get("industry_code", ""))
        for item in quarantine_attestations
        if isinstance(item, Mapping)
    ]
    live_quarantine_hash_values = price_coverage.get("quarantined_file_sha256")
    live_quarantine_hashes = (
        dict(live_quarantine_hash_values)
        if isinstance(live_quarantine_hash_values, Mapping)
        else {}
    )
    quarantine_byte_attestation_valid = (
        fetch.get("quarantine_attestation_complete") is True
        and quarantine_attestation_codes == sorted(EXPECTED_QUARANTINED_HISTORY_CODES)
        and len(quarantine_attestations) == len(EXPECTED_QUARANTINED_HISTORY_CODES)
        and all(
            isinstance(item, Mapping)
            and item.get("source_unchanged_during_staging") is True
            and item.get("staged_matches_source") is True
            and item.get("committed_matches_source") is True
            and is_sha256(item.get("source_sha256_before"))
            and item.get("source_sha256_before")
            == item.get("source_sha256_after_copy")
            == item.get("staged_sha256")
            == item.get("committed_sha256")
            == live_quarantine_hashes.get(str(item.get("industry_code", "")))
            for item in quarantine_attestations
        )
        and set(live_quarantine_hashes) == EXPECTED_QUARANTINED_HISTORY_CODES
    )
    quarantine_bootstrap_attestation_valid = (
        cache_bootstrap.get("quarantined_files_match_baseline") is True
        and set(bootstrap_baseline_quarantine_hashes)
        == EXPECTED_QUARANTINED_HISTORY_CODES
        and set(bootstrap_settlement_quarantine_hashes)
        == EXPECTED_QUARANTINED_HISTORY_CODES
        and set(live_baseline_quarantine_hashes)
        == EXPECTED_QUARANTINED_HISTORY_CODES
        and all(
            is_sha256(bootstrap_baseline_quarantine_hashes.get(code))
            and bootstrap_baseline_quarantine_hashes.get(code)
            == bootstrap_settlement_quarantine_hashes.get(code)
            == live_baseline_quarantine_hashes.get(code)
            == live_quarantine_hashes.get(code)
            for code in EXPECTED_QUARANTINED_HISTORY_CODES
        )
    )
    live_entry_count = canonical_int(price_coverage.get("entry_industry_count"))
    live_exit_count = canonical_int(price_coverage.get("exit_industry_count"))
    live_common_count = canonical_int(price_coverage.get("entry_exit_common_count"))
    checks = {
        "summary_present": bool(refresh),
        "mode_is_price_only_refresh": str(refresh.get("audit_mode", ""))
        == "fund_flow_exploratory_settlement_price_only_refresh",
        "completion_is_committed": str(refresh.get("completion_status", "")) == "committed",
        "generated_at_is_aware_and_after_gate": timestamp_at_or_after(
            refresh.get("generated_at"), START_GATE
        ),
        "time_gate_passed": refresh.get("time_gate_passed") is True,
        "dates_match": (
            str(refresh.get("entry_date", "")) == ENTRY_DATE
            and str(refresh.get("exit_date", "")) == EXIT_DATE
        ),
        "dedicated_cache_scope": (
            str(refresh.get("cache_scope", ""))
            == "dedicated_exploratory_settlement_only"
            and refresh.get("mainline_price_cache_write_invoked") is False
            and resolved_settlement_price_dir != resolved_baseline_price_dir
        ),
        "mainline_cache_unchanged_through_bootstrap_and_refresh": (
            cache_bootstrap.get("checked") is True
            and cache_bootstrap.get("mainline_write_invoked") is False
            and str(cache_bootstrap.get("baseline_path", ""))
            == path_label(resolved_baseline_price_dir)
            and str(cache_bootstrap.get("settlement_path", ""))
            == path_label(resolved_settlement_price_dir)
            and bootstrap_action in {"created_from_mainline", "reused_existing"}
            and cache_bootstrap.get("baseline_unchanged") is True
            and cache_bootstrap.get("baseline_unchanged_through_refresh") is True
            and baseline_before == baseline_after == baseline_after_refresh
            and baseline_after_refresh == live_baseline
            and baseline_before.get("directory_exists") is True
            and canonical_int(baseline_before.get("csv_file_count")) is not None
            and canonical_int(baseline_before.get("csv_file_count"))
            >= MIN_BENCHMARK_INDUSTRIES
            and is_sha256(baseline_before.get("aggregate_sha256"))
            and canonical_int(live_baseline.get("csv_file_count"))
            == canonical_int(baseline_before.get("csv_file_count"))
            and is_sha256(live_baseline.get("aggregate_sha256"))
            and (
                (
                    bootstrap_action == "created_from_mainline"
                    and cache_bootstrap.get("settlement_copied_from_baseline") is True
                    and settlement_before.get("directory_exists") is False
                    and settlement_after_bootstrap == baseline_before
                )
                or (
                    bootstrap_action == "reused_existing"
                    and cache_bootstrap.get("settlement_unchanged_during_bootstrap") is True
                    and settlement_after_bootstrap == settlement_before
                    and settlement_before.get("directory_exists") is True
                )
            )
        ),
        "bootstrap_output_matches_refresh_input": (
            authoritative_before.get("directory_exists") is True
            and authoritative_before == settlement_after_bootstrap
        ),
        "quarantined_bootstrap_files_match_live_mainline": (
            quarantine_bootstrap_attestation_valid
        ),
        "refresh_producer_attestations_match_live_files": producer_attestations_valid,
        "target_allowlist_matches": target_codes == {code for code, _name in EXPECTED.values()},
        "refresh_fetch_completed_without_failure": (
            fetch.get("attempted") is True
            and fetch_failed == 0
            and fetch.get("failed_industry_codes") == []
            and fetch.get("failure_phase") == ""
            and fetch.get("failure_type") == ""
        ),
        "refresh_source_accounting_and_quarantine": (
            fetch_expected == source_count
            and fetch_expected is not None
            and fetch_succeeded == fetch_expected - len(EXPECTED_QUARANTINED_HISTORY_CODES)
            and fetch_failed == 0
            and fetch_quarantined == len(EXPECTED_QUARANTINED_HISTORY_CODES)
            and fetch_accounted == fetch_expected
            and fetch_accounted == fetch_succeeded + fetch_quarantined
            and quarantine_list_valid
            and quarantine_codes == EXPECTED_QUARANTINED_HISTORY_CODES
            and not quarantine_codes & {code for code, _name in EXPECTED.values()}
            and str(fetch.get("quarantine_reason", "")) == EXPECTED_QUARANTINE_REASON
        ),
        "quarantine_byte_attestation": quarantine_byte_attestation_valid,
        "refresh_quarantine_excluded_from_exact_coverage": (
            fetch_succeeded is not None
            and refresh_coverage.get("quarantine_exact_date_exclusion_passed") is True
            and refresh_coverage.get("quarantined_required_date_codes") == []
            and entry_count is not None
            and exit_count is not None
            and common_count is not None
            and entry_count <= fetch_succeeded
            and exit_count <= fetch_succeeded
            and common_count <= fetch_succeeded
        ),
        "refresh_exact_coverage": (
            refresh_coverage.get("exact_coverage_ready") is True
            and source_count is not None
            and source_count >= MIN_BENCHMARK_INDUSTRIES
            and canonical_int(refresh_coverage.get("expected_source_file_count")) == source_count
            and canonical_int(refresh_coverage.get("invalid_file_count")) == 0
            and entry_count is not None
            and entry_count >= MIN_BENCHMARK_INDUSTRIES
            and exit_count is not None
            and exit_count >= MIN_BENCHMARK_INDUSTRIES
            and common_count is not None
            and common_count >= MIN_BENCHMARK_INDUSTRIES
            and common_count <= entry_count
            and common_count <= exit_count
            and target_entry == len(EXPECTED)
            and target_exit == len(EXPECTED)
            and target_common == len(EXPECTED)
        ),
        "append_only_history_contract": (
            continuity.get("checked") is True
            and continuity.get("history_continuity_ready") is True
            and continuity.get("historical_rows_unchanged") is True
            and continuity.get("append_only_contract_passed") is True
            and continuity.get("validation_inputs_unchanged") is True
            and failed_count == 0
            and changed_count == 0
            and verified_count is not None
            and verified_count == source_count
        ),
        "commit_attestation_matches": (
            commit.get("succeeded") is True
            and canonical_int(commit.get("replaced_file_count")) == source_count
            and commit.get("staged_and_committed_hashes_match") is True
            and is_sha256(staged_attestation.get("aggregate_sha256"))
            and staged_attestation.get("aggregate_sha256")
            == committed_attestation.get("aggregate_sha256")
            and staged_attestation.get("aggregate_sha256")
            == continuity.get("staged_universe_aggregate_sha256")
            and is_sha256(continuity.get("existing_universe_aggregate_sha256"))
            and canonical_int(staged_attestation.get("expected_file_count")) == source_count
            and canonical_int(staged_attestation.get("observed_file_count")) == source_count
            and staged_attestation.get("missing_industry_codes") == []
            and canonical_int(committed_attestation.get("expected_file_count")) == source_count
            and canonical_int(committed_attestation.get("observed_file_count")) == source_count
            and committed_attestation.get("missing_industry_codes") == []
        ),
        "current_full_cache_matches_committed_refresh": (
            current.get("directory_exists") is True
            and canonical_int(current.get("csv_file_count")) == source_count
            and current == authoritative_after
            and is_sha256(current.get("aggregate_sha256"))
            and current.get("aggregate_sha256")
            == committed_attestation.get("aggregate_sha256")
        ),
        "live_scan_matches_refresh_universe": (
            price_coverage.get("checked") is True
            and price_coverage.get("exact_coverage_ready") is True
            and price_coverage.get("quarantine_exact_date_exclusion_passed") is True
            and price_coverage.get("quarantined_required_date_codes") == []
            and canonical_int(price_coverage.get("source_file_count"))
            == canonical_int(current.get("csv_file_count"))
            and fetch_succeeded is not None
            and live_entry_count is not None
            and live_entry_count <= fetch_succeeded
            and live_exit_count is not None
            and live_exit_count <= fetch_succeeded
            and live_common_count is not None
            and live_common_count <= fetch_succeeded
        ),
        "price_values_not_retained": (
            refresh.get("price_values_retained_in_audit") is False
            and refresh_coverage.get("price_values_retained") is False
            and continuity.get("price_values_retained") is False
            and price_coverage.get("price_values_retained") is False
        ),
        "non_price_side_effects_disabled": (
            refresh.get("candidate_generation_invoked") is False
            and refresh.get("ledger_write_invoked") is False
            and refresh.get("account_or_trade_write_invoked") is False
        ),
        "official_cache_write_is_explicit": (
            refresh.get("official_cache_write_attempted") is True
            and refresh.get("official_cache_touched") is True
            and refresh.get("official_cache_restored") is False
        ),
    }
    return {
        "checks": checks,
        "all_passed": all(checks.values()),
        "reason_codes": [key for key, passed in checks.items() if not passed],
    }


def classify_record(
    observation_id: str,
    expected_code: str,
    expected_name: str,
    row: Mapping[str, Any],
    entry: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    *,
    candidate_freeze_unique: bool,
    benchmark_freeze_unique: bool,
    time_gate_passed: bool,
    price_coverage: Mapping[str, Any],
) -> dict[str, Any]:
    checks = {
        "record_present": bool(row),
        "identity_matches": (
            str(row.get("industry_code", "")).zfill(6) == expected_code
            and str(row.get("industry_name", "")) == expected_name
        ),
        "dates_match": (
            str(row.get("signal_date", "")) == SIGNAL_DATE
            and str(row.get("planned_entry_date", "")) == ENTRY_DATE
            and str(row.get("planned_exit_date", "")) == EXIT_DATE
        ),
        "scope_exploratory": str(row.get("sample_scope", "")) == "exploratory_fund_flow_only",
        "qualified_false": strict_bool(row.get("qualified_for_goal")) is False,
        "integrity_false": strict_bool(row.get("integrity_eligible")) is False,
        "promotion_false": strict_bool(row.get("promotion_eligible")) is False,
        "candidate_freeze_unique": candidate_freeze_unique,
        "benchmark_freeze_unique": benchmark_freeze_unique,
        "candidate_freeze_is_late_excluded": str(entry.get("entry_price_freeze_status", "")) == "late_backfill_excluded",
        "benchmark_freeze_is_late_excluded": str(benchmark.get("benchmark_entry_freeze_status", "")) == "late_backfill_excluded",
        "benchmark_universe_count_is_canonical_zero": canonical_zero(
            benchmark.get("benchmark_universe_count")
        ),
        "returns_empty": all(not str(row.get(field, "")).strip() for field in RETURN_FIELDS),
    }
    contract_passed = all(checks.values())
    reasons: list[str] = []
    if not contract_passed:
        reasons.append("source_record_contract_failed")
    if str(row.get("source_fingerprint_status", "")) != "verified_bundle":
        reasons.append("legacy_source_unverified")
    if str(row.get("calendar_fingerprint", "")).startswith("UNVERIFIED"):
        reasons.append("legacy_calendar_unverified")
    if str(row.get("code_version", "")).startswith("UNVERIFIED"):
        reasons.append("legacy_code_unverified")
    if str(row.get("cohort_manifest_hash", "")).startswith("UNVERIFIED"):
        reasons.append("legacy_cohort_unverified")
    candidate_status = str(entry.get("entry_price_freeze_status", "missing"))
    benchmark_status = str(benchmark.get("benchmark_entry_freeze_status", "missing"))
    benchmark_count_raw = benchmark.get("benchmark_universe_count")
    benchmark_count = 0 if canonical_zero(benchmark_count_raw) else None
    if candidate_status == "late_backfill_excluded":
        reasons.append("candidate_entry_freeze_late_backfill_excluded")
    elif candidate_status != "frozen_on_time":
        reasons.append("candidate_entry_freeze_missing")
    if benchmark_status == "late_backfill_excluded":
        reasons.append("benchmark_entry_freeze_late_backfill_excluded")
    elif benchmark_status != "frozen_on_time":
        reasons.append("benchmark_entry_freeze_missing")
    if benchmark_count != MIN_BENCHMARK_INDUSTRIES:
        reasons.append("benchmark_universe_below_100")
    if not candidate_freeze_unique:
        reasons.append("duplicate_candidate_entry_freeze_key")
    if not benchmark_freeze_unique:
        reasons.append("duplicate_benchmark_entry_freeze_key")
    if strict_bool(row.get("qualified_for_goal")) is False:
        reasons.append("not_qualified_for_goal")
    if strict_bool(row.get("integrity_eligible")) is False:
        reasons.append("integrity_ineligible")
    if strict_bool(row.get("promotion_eligible")) is False:
        reasons.append("promotion_ineligible")
    if not time_gate_passed:
        disposition = "pending"
        status = "not_due_before_2026_07_21_15_00_asia_shanghai"
        reasons.insert(0, "time_gate_not_reached")
    elif not bool(price_coverage.get("exact_coverage_ready")):
        disposition = "pending"
        status = "pending_exact_price_coverage"
        reasons.insert(0, "exact_entry_exit_coverage_incomplete")
    elif not contract_passed:
        disposition = "blocked"
        status = "blocked_source_record_contract"
    elif candidate_status == "late_backfill_excluded" and benchmark_status == "late_backfill_excluded":
        disposition = "blocked"
        status = "blocked_terminal_late_freeze_excluded"
    else:
        disposition = "pending"
        status = "pending_standard_settlement_route"
        reasons.append("exploratory_audit_does_not_calculate_returns")
    return {
        "record_type": "exploratory_settlement_disposition",
        "observation_id": observation_id,
        "industry_code": expected_code,
        "industry_name": expected_name,
        "signal_date": str(row.get("signal_date", SIGNAL_DATE)),
        "planned_entry_date": str(row.get("planned_entry_date", ENTRY_DATE)),
        "planned_exit_date": str(row.get("planned_exit_date", EXIT_DATE)),
        "ledger_settlement_status": str(row.get("settlement_status", "")),
        "selection_score": str(row.get("selection_score", "")),
        "sample_scope": str(row.get("sample_scope", "")),
        "qualified_for_goal": strict_bool(row.get("qualified_for_goal")),
        "integrity_eligible": strict_bool(row.get("integrity_eligible")),
        "promotion_eligible": strict_bool(row.get("promotion_eligible")),
        "source_cohort_id": str(row.get("cohort_id", "")),
        "source_cohort_manifest_hash": str(row.get("cohort_manifest_hash", "")),
        "source_fingerprint_status": str(row.get("source_fingerprint_status", "")),
        "calendar_fingerprint": str(row.get("calendar_fingerprint", "")),
        "code_version": str(row.get("code_version", "")),
        "candidate_entry_freeze_status": candidate_status,
        "benchmark_entry_freeze_status": benchmark_status,
        "benchmark_universe_count": benchmark_count,
        "benchmark_universe_count_raw": str(benchmark_count_raw),
        "record_contract_passed": contract_passed,
        "disposition": disposition,
        "disposition_status": status,
        "reason_codes": "|".join(dict.fromkeys(reasons)),
        "actual_entry_date": "",
        "actual_exit_date": "",
        "realized_return": "",
        "benchmark_return": "",
        "realized_relative_return": "",
        "future_return_rank_pct": "",
        "future_top_quintile": "",
    }


def validate_calendar_dates(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": relative_path(path),
        "checked": True,
        "calendar_dates_valid": False,
        "date_field": "",
        "row_count": 0,
        "entry_date": ENTRY_DATE,
        "exit_date": EXIT_DATE,
        "entry_date_present": False,
        "exit_date_present": False,
        "reason": "calendar file missing",
    }
    if not path.is_file():
        return result
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            date_field = next((name for name in ("trade_date", "date", "日期") if name in fields), None)
            if date_field is None:
                result["reason"] = "calendar date column missing"
                return result
            dates: set[str] = set()
            row_count = 0
            for row in reader:
                row_count += 1
                value = strict_iso_date(row.get(date_field, ""))
                if value:
                    dates.add(value)
    except (OSError, csv.Error) as exc:
        result["reason"] = f"calendar read failed: {type(exc).__name__}"
        return result
    entry_present = ENTRY_DATE in dates
    exit_present = EXIT_DATE in dates
    result.update({
        "date_field": date_field,
        "row_count": row_count,
        "entry_date_present": entry_present,
        "exit_date_present": exit_present,
        "calendar_dates_valid": entry_present and exit_present,
        "reason": "exact entry and exit dates present" if entry_present and exit_present else "exact entry or exit date missing",
    })
    return result


def quarantined_file_hashes(price_dir: Path) -> dict[str, str]:
    return {
        code: file_sha256(price_dir / f"{code}.csv")
        for code in sorted(EXPECTED_QUARANTINED_HISTORY_CODES)
        if (price_dir / f"{code}.csv").is_file()
    }


def scan_exact_date_coverage(price_dir: Path) -> dict[str, Any]:
    files = sorted(price_dir.glob("*.csv"))
    entry_codes: set[str] = set()
    exit_codes: set[str] = set()
    invalid_files: list[dict[str, Any]] = []
    quarantined_required_date_codes: set[str] = set()
    quarantined_file_sha256: dict[str, str] = {}
    max_date = ""
    for path in files:
        reasons: list[str] = []
        code = path.stem
        if code in EXPECTED_QUARANTINED_HISTORY_CODES:
            try:
                quarantined_file_sha256[code] = file_sha256(path)
            except OSError:
                reasons.append("quarantined_history_hash_failed")
        if re.fullmatch(r"[0-9]{6}", code) is None:
            reasons.append("invalid_six_digit_filename")
        required_rows: dict[str, list[Any]] = {ENTRY_DATE: [], EXIT_DATE: []}
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fields = reader.fieldnames or []
                code_field = next((name for name in ("industry_code", "code", "代码") if name in fields), None)
                date_field = next((name for name in ("trade_date", "date", "日期") if name in fields), None)
                close_field = next((name for name in ("close_index", "close", "收盘") if name in fields), None)
                if code_field is None:
                    reasons.append("missing_industry_code_column")
                if date_field is None:
                    reasons.append("missing_date_column")
                if close_field is None:
                    reasons.append("missing_close_column")
                if code_field is not None and date_field is not None and close_field is not None:
                    for row in reader:
                        observed_code = normalize_price_code(row.get(code_field))
                        if observed_code != code:
                            reasons.append("industry_code_mismatch")
                        value = strict_iso_date(row.get(date_field))
                        if value:
                            max_date = max(max_date, value)
                        if value in required_rows:
                            required_rows[value].append(row.get(close_field))
        except (OSError, csv.Error, UnicodeError):
            reasons.append("history_read_failed")

        for required_date, target_set in ((ENTRY_DATE, entry_codes), (EXIT_DATE, exit_codes)):
            values = required_rows[required_date]
            if code in EXPECTED_QUARANTINED_HISTORY_CODES and values:
                reasons.append(f"quarantined_history_contains_settlement_date:{required_date}")
                quarantined_required_date_codes.add(code)
            if len(values) > 1:
                reasons.append(f"duplicate_required_date:{required_date}")
            elif len(values) == 1 and not valid_price(values[0]):
                reasons.append(f"invalid_required_close:{required_date}")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            invalid_files.append({"file": path.name, "reason_codes": reasons})
            continue
        if len(required_rows[ENTRY_DATE]) == 1 and valid_price(required_rows[ENTRY_DATE][0]):
            entry_codes.add(code)
        if len(required_rows[EXIT_DATE]) == 1 and valid_price(required_rows[EXIT_DATE][0]):
            exit_codes.add(code)
    candidates = {code for code, _name in EXPECTED.values()}
    common_codes = entry_codes & exit_codes
    coverage = {
        "checked": True,
        "reason": "exact dates only; no price or return value retained",
        "source_file_count": len(files),
        "valid_source_file_count": len(files) - len(invalid_files),
        "invalid_file_count": len(invalid_files),
        "invalid_files": invalid_files,
        "quarantined_required_date_codes": sorted(quarantined_required_date_codes),
        "quarantined_file_sha256": quarantined_file_sha256,
        "quarantine_exact_date_exclusion_passed": not quarantined_required_date_codes,
        "entry_industry_count": len(entry_codes),
        "exit_industry_count": len(exit_codes),
        "entry_exit_common_count": len(common_codes),
        "candidate_entry_count": len(candidates & entry_codes),
        "candidate_exit_count": len(candidates & exit_codes),
        "candidate_common_count": len(candidates & common_codes),
        "overall_max_date": max_date,
        "minimum_benchmark_industries": MIN_BENCHMARK_INDUSTRIES,
        "price_values_retained": False,
        "exact_coverage_ready": False,
    }
    return recompute_exact_coverage(coverage)


def normalize_price_code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".SI"):
        text = text[:-3]
    return text.zfill(6) if text.isdigit() else text


def strict_iso_date(value: Any) -> str:
    text = str(value).strip()
    if len(text) < 10 or (len(text) > 10 and text[10] not in {"T", " "}):
        return ""
    candidate = text[:10]
    try:
        parsed = (
            datetime.strptime(candidate, "%Y-%m-%d")
            if len(text) == 10
            else datetime.fromisoformat(text.replace("Z", "+00:00"))
        )
    except ValueError:
        return ""
    return parsed.date().isoformat()


def recompute_exact_coverage(payload: Mapping[str, Any]) -> dict[str, Any]:
    coverage = dict(payload)
    counts = {
        key: canonical_int(coverage.get(key))
        for key in (
            "source_file_count",
            "valid_source_file_count",
            "invalid_file_count",
            "entry_industry_count",
            "exit_industry_count",
            "entry_exit_common_count",
            "candidate_entry_count",
            "candidate_exit_count",
            "candidate_common_count",
            "minimum_benchmark_industries",
        )
    }
    checks = {
        "checked": coverage.get("checked") is True,
        "all_counts_are_canonical_integers": all(value is not None for value in counts.values()),
        "minimum_is_100": counts["minimum_benchmark_industries"] == MIN_BENCHMARK_INDUSTRIES,
        "source_file_count_at_least_100": (
            counts["source_file_count"] is not None
            and counts["source_file_count"] >= MIN_BENCHMARK_INDUSTRIES
        ),
        "all_source_files_valid": (
            counts["source_file_count"] is not None
            and counts["valid_source_file_count"] == counts["source_file_count"]
            and counts["invalid_file_count"] == 0
        ),
        "entry_count_at_least_100": (
            counts["entry_industry_count"] is not None
            and counts["entry_industry_count"] >= MIN_BENCHMARK_INDUSTRIES
        ),
        "exit_count_at_least_100": (
            counts["exit_industry_count"] is not None
            and counts["exit_industry_count"] >= MIN_BENCHMARK_INDUSTRIES
        ),
        "common_count_at_least_100": (
            counts["entry_exit_common_count"] is not None
            and counts["entry_exit_common_count"] >= MIN_BENCHMARK_INDUSTRIES
        ),
        "common_not_above_entry_or_exit": (
            counts["entry_exit_common_count"] is not None
            and counts["entry_industry_count"] is not None
            and counts["exit_industry_count"] is not None
            and counts["entry_exit_common_count"] <= counts["entry_industry_count"]
            and counts["entry_exit_common_count"] <= counts["exit_industry_count"]
        ),
        "candidate_counts_are_exactly_four": (
            counts["candidate_entry_count"] == len(EXPECTED)
            and counts["candidate_exit_count"] == len(EXPECTED)
            and counts["candidate_common_count"] == len(EXPECTED)
        ),
        "candidate_common_not_above_entry_or_exit": (
            counts["candidate_common_count"] is not None
            and counts["candidate_entry_count"] is not None
            and counts["candidate_exit_count"] is not None
            and counts["candidate_common_count"] <= counts["candidate_entry_count"]
            and counts["candidate_common_count"] <= counts["candidate_exit_count"]
        ),
        "overall_max_date_reaches_exit": str(coverage.get("overall_max_date", "")) >= EXIT_DATE,
        "price_values_not_retained": coverage.get("price_values_retained") is False,
    }
    if "reported_exact_coverage_ready" not in coverage:
        coverage["reported_exact_coverage_ready"] = (
            coverage.get("exact_coverage_ready") is True
        )
    coverage["coverage_gate_checks"] = checks
    coverage["exact_coverage_ready"] = all(checks.values())
    return coverage


def assert_audit_inputs_unchanged(
    before_hashes: Mapping[str, str],
    after_hashes: Mapping[str, str],
    price_before: Mapping[str, Any],
    price_after: Mapping[str, Any],
    baseline_before: Mapping[str, Any],
    baseline_after: Mapping[str, Any],
) -> None:
    if dict(before_hashes) != dict(after_hashes):
        raise RuntimeError("authoritative ledger, freeze, state, or refresh evidence changed during audit")
    if dict(price_before) != dict(price_after):
        raise RuntimeError("price-cache filename/hash manifest changed during audit")
    if dict(baseline_before) != dict(baseline_after):
        raise RuntimeError("mainline price-cache filename/hash manifest changed during audit")


def safe_remove_output_tree(path: Path, expected_parent: Path, expected_prefix: str) -> None:
    resolved = path.resolve()
    parent = expected_parent.resolve()
    if path.is_symlink() or resolved.parent != parent or not resolved.name.startswith(expected_prefix):
        raise ValueError("refusing_to_remove_unrecognized_audit_staging_path")
    shutil.rmtree(resolved)


def publish_output_directory(staged_out: Path, final_out: Path) -> None:
    staged = staged_out.resolve()
    final_parent = final_out.parent.resolve()
    final = final_out.resolve()
    expected_prefix = f".{final_out.name}.staging-"
    if (
        staged_out.is_symlink()
        or staged.parent != final_parent
        or not staged.name.startswith(expected_prefix)
        or final.parent != final_parent
        or final_out.is_symlink()
    ):
        raise ValueError("invalid audit publication path")

    backup_placeholder = Path(
        tempfile.mkdtemp(prefix=f".{final_out.name}.previous-", dir=final_parent)
    )
    backup_placeholder.rmdir()
    backup = backup_placeholder.resolve()
    moved_old = False
    try:
        if final_out.exists():
            os.replace(final_out, backup)
            moved_old = True
        os.replace(staged_out, final_out)
    except BaseException:
        if moved_old and backup.exists() and not final_out.exists():
            os.replace(backup, final_out)
        raise
    if moved_old and backup.exists():
        try:
            safe_remove_output_tree(backup, final_parent, f".{final_out.name}.previous-")
        except OSError:
            pass


def write_outputs(
    out: Path,
    summary: Mapping[str, Any],
    dispositions: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    date_coverage: Mapping[str, Any],
    pre_snapshot: Mapping[str, Any],
    post_snapshot: Mapping[str, Any],
) -> None:
    debug = out / "debug"
    out.mkdir(parents=True, exist_ok=True)
    debug.mkdir(parents=True, exist_ok=True)
    fields = list(dispositions[0]) if dispositions else ["record_type", "observation_id", "disposition"]
    atomic_write_csv(out / "top_candidates.csv", dispositions, fieldnames=fields, sort_rows_by=["industry_code"])
    atomic_write_json(out / "run_summary.json", dict(summary))
    atomic_write_text(out / "report.md", render_report(summary, dispositions))
    atomic_write_csv(debug / "settlement_dispositions.csv", dispositions, fieldnames=fields, sort_rows_by=["industry_code"])
    atomic_write_csv(debug / "sha256_manifest.csv", evidence, fieldnames=["path", "bytes", "sha256"])
    atomic_write_json(debug / "pre_settlement_snapshot.json", dict(pre_snapshot))
    atomic_write_json(debug / "post_settlement_snapshot.json", dict(post_snapshot))
    atomic_write_json(debug / "date_coverage_audit.json", dict(date_coverage))
    atomic_write_json(debug / "command_results.json", {
        "audit_mode": summary.get("audit_mode", "unassigned"),
        "exit_data_read": summary.get("exit_data_read"),
        "return_values_read_or_written": False,
        "authoritative_ledgers_mutated": False,
        "state_gate_passed": summary.get("state_gate_passed"),
        "state_gate_reason_codes": summary.get("state_gate_reason_codes", []),
        "price_source_gate_passed": summary.get("price_source_gate_passed"),
        "price_source_gate_reason_codes": summary.get("price_source_gate_reason_codes", []),
    })


def render_report(summary: Mapping[str, Any], dispositions: list[Mapping[str, Any]]) -> str:
    final_disposition = bool(summary.get("settlement_disposition_complete"))
    coverage_value = summary.get("price_coverage")
    price_coverage = coverage_value if isinstance(coverage_value, Mapping) else {}
    lines = [
        "# 四条探索性资金流记录终局处置" if final_disposition else "# 四条探索性资金流记录结算预检",
        "",
        "## 技术结论",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 现场时间：`{summary['observed_at']}`",
        f"- 最早启动时间：`{summary['required_start_at']}`",
        f"- 完成状态：`{summary['completion_status']}`",
        f"- 四条处置：settled `0` / blocked `{summary['blocked_count']}` / pending `{summary['pending_count']}`",
        "- 强行业结论：保持 `research_only / NO_ACTION`，合格结算数仍为 `0`。",
        f"- 精确入场/退出同一行业交集：`{price_coverage.get('entry_exit_common_count', 0)}`（门槛 `{MIN_BENCHMARK_INDUSTRIES}`）",
        f"- 交易日历精确日期：`{str(summary['calendar_dates_valid']).lower()}`（{ENTRY_DATE} / {EXIT_DATE}）",
        f"- 状态门禁：active cohort `{str(summary['active_cohort_gate_passed']).lower()}` / V5.30 失败关闭摘要门 `{str(summary['v5_30_summary_gate_passed']).lower()}`（integrity `{str(summary['v5_30_integrity_passed']).lower()}`） / current state `{str(summary['current_state_consistency_gate_passed']).lower()}` / price refresh `{str(summary['price_source_gate_passed']).lower()}`",
        f"- 状态门禁原因：`{'|'.join(summary['state_gate_reason_codes']) or 'none'}`",
        "",
        "## 四条记录的当前处置",
        "",
        "| 行业 | 计划区间 | 当前处置 | 候选冻结 | 基准冻结 | 原因代码 |",
        "|---|---|---|---|---|---|",
    ]
    for row in dispositions:
        lines.append(
            f"| {row['industry_code']} {row['industry_name']} | {row['planned_entry_date']} → {row['planned_exit_date']} | "
            f"`{row['disposition_status']}` | `{row['candidate_entry_freeze_status']}` | "
            f"`{row['benchmark_entry_freeze_status']}` | `{row['reason_codes']}` |"
        )
    lines.extend([
        "",
        "## 口径与方法",
        "",
        "本审计只处理固定 allowlist 中的四条 `exploratory_fund_flow_only` 记录。时间门禁通过前不会打开行情文件；即使以后行情补齐，晚冻结也不能改写成按时冻结。本审计不计算收益、不追加 settlement event，也不物化兼容 CSV。",
        "",
        (
            f"上方 `{price_coverage.get('entry_exit_common_count', 0)}` 个同一行业精确行情只衡量 `{ENTRY_DATE}` 与 `{EXIT_DATE}` 的结算日期数据可用性；"
            "逐条记录的 `benchmark_universe_count=0` 描述入场时没有形成按时冻结的基准宇宙。"
            "两个口径不能互相替代，事后行情补齐也不能修复历史冻结。"
        ),
        "",
        "收益图表被有意省略：当前没有可合法结算的收益值，画图会把未到期或事后补录数据包装成有效结果。逐条审计表比图表更适合本阶段的证据任务。",
        "",
        "## 结论边界与状态同步" if final_disposition else "## 限制、恢复条件与下一步",
        "",
        (
            "时间与精确日期覆盖门禁已经通过；四条终局均为 terminal blocked。该处置只确认旧观察缺少可验证的事前冻结，不包含收益，也不能转作强行业 Alpha 的正面或负面证据。独立的 exploratory settled / blocked / pending 计数与状态一致性由正式编排的后置步骤同步；同步不修改筛选规则，也不改变本终局。"
            if final_disposition
            else "正式处置只能在 2026-07-21 15:00（Asia/Shanghai）后启动，并且精确 2026-06-23 与 2026-07-21 行情覆盖至少 100 个同一行业、四个候选均在入场/退出交集中；validated active pair、2026-07-21 当日 V5.30 摘要和当日 current_state_consistency 也必须互相一致。缺口未清零时继续保持 pending/blocked；门禁齐全后，现有四条仍应因永久晚冻结和 legacy 完整性缺口形成 terminal blocked 处置，不得补价或晋级。"
        ),
        "",
        "`qualified_settled_count=0`、`promotion_ready=false`、`manual_decision_support_ready=false`、`production_ready=false`、`auto_execution_allowed=false` 均为硬不变量。",
        "",
    ])
    return "\n".join(lines)


def verdict(completion_status: str) -> str:
    if completion_status == "blocked_pre_start":
        return "正式结算尚未启动：现场时钟早于 2026-07-21 15:00。四条记录保持 pending，未读取退出数据或收益。"
    if completion_status == "pending_exact_price_coverage":
        return "时间门禁已到，但精确入场日、退出日或至少 100 个同日行业的覆盖不足；四条继续 pending。"
    if completion_status == "complete_terminal_exclusions":
        return "逐条处置已完成：四条均因不可逆晚冻结与 legacy 证据缺口终止排除，0 条写入收益，0 条晋级。"
    if completion_status == "blocked_active_cohort_gate":
        return "validated active cohort 未通过现场校验；正式处置失败关闭，不读取或写入收益。"
    if completion_status == "blocked_calendar_dates_gate":
        return "交易日历未能同时验证精确计划入场日与退出日；正式处置失败关闭，不寻找替代日期。"
    if completion_status == "blocked_v5_30_summary_gate":
        return "V5.30 摘要未与 validated active pair、2026-07-21 时点或 legacy 失败关闭语义对齐；正式处置失败关闭。"
    if completion_status == "blocked_current_state_consistency_gate":
        return "current_state_consistency 未在 2026-07-21 当日以同一 active pair 通过；正式处置失败关闭。"
    if completion_status == "blocked_price_refresh_attestation_gate":
        return "行情刷新审计、append-only 历史合同或当前整库哈希未能互相印证；正式处置失败关闭。"
    return "源记录合同未能完整复核；保持失败关闭，不读取或写入收益。"


def build_source_snapshot(
    source: Mapping[str, Any],
    hashes: Mapping[str, str],
    *,
    phase: str,
) -> dict[str, Any]:
    checkpoints = source.get("checkpoints", {})
    return {
        "snapshot_phase": phase,
        "captured_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "sensitive_file_sha256": dict(hashes),
        "checkpoint_verification": checkpoints,
        "global_observation_count": int(source.get("global_observation_count", 0)),
        "target_observation_count": len(source.get("observations", [])),
        "candidate_freeze_count": len(source.get("entry_freezes", [])),
        "benchmark_freeze_count": len(source.get("benchmark_freezes", [])),
        "active_cohort_id": str(source.get("active_cohort", {}).get("cohort_id", "")),
        "active_cohort_manifest_hash": str(source.get("active_cohort", {}).get("manifest_hash", "")),
        "active_cohort_validated": source.get("active_cohort", {}).get("freeze_passed") is True,
        "v5_30_active_cohort_id": str(source.get("integrity", {}).get("active_cohort_id", "")),
        "v5_30_active_cohort_manifest_hash": str(source.get("integrity", {}).get("active_cohort_manifest_hash", "")),
        "v5_30_as_of_date": str(source.get("integrity", {}).get("as_of_date", "")),
        "v5_30_generated_at": str(source.get("integrity", {}).get("generated_at", "")),
        "current_state_active_cohort_id": str(source.get("current_state", {}).get("active_cohort_id", "")),
        "current_state_active_cohort_manifest_hash": str(source.get("current_state", {}).get("active_cohort_manifest_hash", "")),
        "current_state_generated_at": str(source.get("current_state", {}).get("generated_at", "")),
        "price_refresh_completion_status": str(source.get("price_refresh", {}).get("completion_status", "")),
        "price_refresh_generated_at": str(source.get("price_refresh", {}).get("generated_at", "")),
        "price_refresh_authoritative_after": dict(
            source.get("price_refresh", {}).get("authoritative_after", {})
        ),
        "price_cache_snapshot": dict(source.get("price_cache_snapshot", {})),
        "baseline_price_cache_snapshot": dict(
            source.get("baseline_price_cache_snapshot", {})
        ),
        "baseline_quarantined_file_sha256": dict(
            source.get("baseline_quarantined_file_sha256", {})
        ),
        "calendar_dates_valid": source.get("calendar_validation", {}).get("calendar_dates_valid") is True,
        "calendar_validation": dict(source.get("calendar_validation", {})),
    }


def evidence_manifest(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        if path.is_file():
            rows.append({"path": relative_path(path), "bytes": path.stat().st_size, "sha256": file_sha256(path)})
    return rows


def snapshot_hashes(paths: Iterable[Path]) -> dict[str, str]:
    return {relative_path(path): file_sha256(path) for path in paths if path.is_file()}


def index_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], Mapping[str, Any]], set[tuple[str, str]]]:
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    duplicates: set[tuple[str, str]] = set()
    for row in rows:
        key = logical_key(row)
        if key in index:
            duplicates.add(key)
            continue
        index[key] = row
    return index, duplicates


def logical_key(row: Mapping[str, Any], fallback_code: str = "") -> tuple[str, str]:
    return str(row.get("batch_id", "v5_25_fund_flow_dual_positive_2026-06-22")), str(row.get("industry_code", fallback_code)).zfill(6)


def duplicate_values(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def as_int(value: Any) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def strict_int(value: Any) -> int | None:
    text = str(value).strip()
    if not text or text.startswith("+"):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def canonical_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def canonical_zero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    return isinstance(value, str) and value == "0"


def valid_price(value: Any) -> bool:
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation:
        return False
    return parsed.is_finite() and parsed > 0


def is_sha256(value: Any) -> bool:
    text = str(value).strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def timestamp_is_on_date(value: Any, expected_date: str) -> bool:
    parsed = parse_aware_shanghai_timestamp(value)
    return parsed is not None and parsed.date().isoformat() == expected_date


def timestamp_at_or_after(value: Any, minimum: datetime) -> bool:
    parsed = parse_aware_shanghai_timestamp(value)
    if parsed is None or minimum.tzinfo is None:
        return False
    return parsed >= minimum.astimezone(SHANGHAI)


def parse_aware_shanghai_timestamp(value: Any) -> datetime | None:
    text = str(value).strip()
    if len(text) <= 10 or text[10] not in {"T", " "}:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(SHANGHAI)


def strict_bool(value: Any) -> bool | None:
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "是"}:
        return True
    if normalized in {"false", "0", "no", "否"}:
        return False
    return None


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


def resolve_active_manifest_path() -> Path | None:
    active = read_json(ACTIVE_COHORT)
    value = str(active.get("manifest_path", "")).strip()
    if not value:
        return None
    path = (ROOT / value).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError:
        raise ValueError("active cohort manifest path escapes the repository")
    return path


def relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def self_check() -> None:
    fixture = fixture_source()
    called = False

    def forbidden_loader() -> dict[str, Any]:
        nonlocal called
        called = True
        raise AssertionError("price loader called before market-close gate")

    before = datetime(2026, 7, 21, 14, 59, 59, tzinfo=SHANGHAI)
    summary, rows, coverage = assess(before, fixture, price_coverage_loader=forbidden_loader)
    assert not called and not coverage["checked"]
    assert summary["completion_status"] == "blocked_pre_start"
    assert summary["pending_count"] == 4 and summary["blocked_count"] == 0
    assert all(row["disposition"] == "pending" for row in rows)
    assert all(not row["realized_return"] for row in rows)

    at_gate = datetime(2026, 7, 21, 15, 0, 0, tzinfo=SHANGHAI)
    summary, rows, _coverage = assess(at_gate, fixture, price_coverage_loader=lambda: coverage_fixture(100))
    assert summary["completion_status"] == "complete_terminal_exclusions"
    assert summary["blocked_count"] == 4 and summary["qualified_settled_count"] == 0
    assert all(row["disposition_status"] == "blocked_terminal_late_freeze_excluded" for row in rows)
    print("self_check=pass")


def fixture_source() -> dict[str, Any]:
    observations, entry, benchmark = [], [], []
    for observation_id, (code, name) in EXPECTED.items():
        batch = "v5_25_fund_flow_dual_positive_2026-06-22"
        observations.append({
            "observation_id": observation_id,
            "batch_id": batch,
            "industry_code": code,
            "industry_name": name,
            "signal_date": SIGNAL_DATE,
            "planned_entry_date": ENTRY_DATE,
            "planned_exit_date": EXIT_DATE,
            "sample_scope": "exploratory_fund_flow_only",
            "qualified_for_goal": "False",
            "integrity_eligible": "False",
            "promotion_eligible": "False",
            "source_fingerprint_status": "unverified_legacy",
            "calendar_fingerprint": "UNVERIFIED_LEGACY_CALENDAR",
            "code_version": "UNVERIFIED_LEGACY_CODE",
            "cohort_id": "legacy_exploratory_20260622",
            "cohort_manifest_hash": "UNVERIFIED_LEGACY_COHORT",
        })
        entry.append({
            "batch_id": batch,
            "industry_code": code,
            "entry_price_freeze_status": "late_backfill_excluded",
        })
        benchmark.append({
            "batch_id": batch,
            "industry_code": code,
            "benchmark_entry_freeze_status": "late_backfill_excluded",
            "benchmark_universe_count": "0",
        })
    active_id = "active"
    active_hash = "a" * 64
    cache_snapshot = {
        "directory_exists": True,
        "csv_file_count": 131,
        "aggregate_sha256": "c" * 64,
    }
    baseline_snapshot = {
        "directory_exists": True,
        "csv_file_count": 131,
        "aggregate_sha256": "b" * 64,
    }
    missing_settlement_snapshot = {
        "directory_exists": False,
        "csv_file_count": 0,
        "aggregate_sha256": "0" * 64,
    }
    producer_evidence = [
        {
            "path": relative_path(path),
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        for path in REFRESH_PRODUCER_PATHS
    ]
    source_universe_attestation = {
        "expected_file_count": 131,
        "observed_file_count": 131,
        "missing_industry_codes": [],
        "aggregate_sha256": "c" * 64,
    }
    return {
        "observations": observations,
        "entry_freezes": entry,
        "benchmark_freezes": benchmark,
        "active_cohort": {
            "freeze_passed": True,
            "cohort_id": active_id,
            "manifest_hash": active_hash,
            "validation_reason": "fixture validated",
        },
        "integrity": {
            "active_cohort_freeze_passed": True,
            "active_cohort_id": active_id,
            "active_cohort_manifest_hash": active_hash,
            "as_of_date": EXIT_DATE,
            "generated_at": f"{EXIT_DATE}T15:01:00+08:00",
            "policy_status": "research_only",
            "integrity_passed": False,
            "global_ledger_integrity_passed": False,
            "global_ledger_rows": len(EXPECTED),
            "global_violation_count": 16,
            "global_late_backfill_count": 2 * len(EXPECTED),
            "best_status": "research_only_ledger_integrity_failed",
            "can_claim_strong_rebound_industries": False,
            "goal_ready": False,
            "production_ready": False,
            "auto_execution_allowed": False,
        },
        "current_state": {
            "active_cohort_id": active_id,
            "active_cohort_manifest_hash": active_hash,
            "active_cohort_validated": True,
            "state_consistent": True,
            "fail_count": 0,
            "generated_at": f"{EXIT_DATE}T15:02:00+08:00",
            "current_as_of_date": EXIT_DATE,
            "policy_status": "research_only",
            "current_action": "NO_ACTION",
            "strong_industry_alpha_validated": False,
            "manual_decision_support_ready": False,
            "production_ready": False,
            "true_forward_route_ready": False,
            "auto_execution_allowed": False,
        },
        "price_refresh": {
            "audit_mode": "fund_flow_exploratory_settlement_price_only_refresh",
            "cache_scope": "dedicated_exploratory_settlement_only",
            "mainline_price_cache_write_invoked": False,
            "cache_bootstrap": {
                "checked": True,
                "action": "created_from_mainline",
                "baseline_path": relative_path(BASELINE_PRICE_DIR),
                "settlement_path": relative_path(PRICE_DIR),
                "baseline_before": dict(baseline_snapshot),
                "baseline_after": dict(baseline_snapshot),
                "baseline_after_refresh": dict(baseline_snapshot),
                "baseline_unchanged": True,
                "baseline_unchanged_through_refresh": True,
                "settlement_before": dict(missing_settlement_snapshot),
                "settlement_after_bootstrap": dict(baseline_snapshot),
                "settlement_unchanged_during_bootstrap": False,
                "settlement_copied_from_baseline": True,
                "baseline_quarantined_file_sha256": {"801156": "e" * 64},
                "settlement_quarantined_file_sha256": {"801156": "e" * 64},
                "quarantined_files_match_baseline": True,
                "mainline_write_invoked": False,
            },
            "producer_attestations": producer_evidence,
            "generated_at": f"{EXIT_DATE}T15:03:00+08:00",
            "time_gate_passed": True,
            "entry_date": ENTRY_DATE,
            "exit_date": EXIT_DATE,
            "target_industry_codes": sorted(code for code, _name in EXPECTED.values()),
            "candidate_generation_invoked": False,
            "ledger_write_invoked": False,
            "account_or_trade_write_invoked": False,
            "price_values_retained_in_audit": False,
            "completion_status": "committed",
            "official_cache_write_attempted": True,
            "official_cache_touched": True,
            "official_cache_restored": False,
            "authoritative_before": dict(baseline_snapshot),
            "authoritative_after": dict(cache_snapshot),
            "fetch": {
                "attempted": True,
                "expected_industry_count": 131,
                "succeeded_industry_count": 130,
                "failed_industry_count": 0,
                "failed_industry_codes": [],
                "failure_phase": "",
                "failure_type": "",
                "quarantined_industry_count": 1,
                "quarantined_industry_codes": ["801156"],
                "quarantine_reason": EXPECTED_QUARANTINE_REASON,
                "quarantine_attestations": [{
                    "industry_code": "801156",
                    "source_sha256_before": "e" * 64,
                    "source_sha256_after_copy": "e" * 64,
                    "staged_sha256": "e" * 64,
                    "committed_sha256": "e" * 64,
                    "source_unchanged_during_staging": True,
                    "staged_matches_source": True,
                    "committed_matches_source": True,
                }],
                "quarantine_attestation_complete": True,
                "source_accounted_industry_count": 131,
            },
            "coverage": {
                "source_file_count": 131,
                "expected_source_file_count": 131,
                "invalid_file_count": 0,
                "quarantined_required_date_codes": [],
                "quarantine_exact_date_exclusion_passed": True,
                "entry_industry_count": 130,
                "exit_industry_count": 130,
                "entry_exit_common_count": 130,
                "target_entry_count": 4,
                "target_exit_count": 4,
                "target_common_count": 4,
                "exact_coverage_ready": True,
                "price_values_retained": False,
            },
            "history_continuity": {
                "checked": True,
                "verified_industry_count": 131,
                "failed_industry_count": 0,
                "historical_rows_changed": 0,
                "historical_rows_unchanged": True,
                "append_only_contract_passed": True,
                "validation_inputs_unchanged": True,
                "existing_universe_aggregate_sha256": "d" * 64,
                "staged_universe_aggregate_sha256": "c" * 64,
                "history_continuity_ready": True,
                "price_values_retained": False,
            },
            "commit": {
                "succeeded": True,
                "replaced_file_count": 131,
                "staged_and_committed_hashes_match": True,
                "staged_universe_attestation": source_universe_attestation,
                "committed_universe_attestation": source_universe_attestation,
            },
        },
        "price_cache_snapshot": dict(cache_snapshot),
        "baseline_price_cache_snapshot": dict(baseline_snapshot),
        "baseline_quarantined_file_sha256": {"801156": "e" * 64},
        "calendar_validation": {
            "checked": True,
            "calendar_dates_valid": True,
            "entry_date_present": True,
            "exit_date_present": True,
            "reason": "fixture",
        },
        "checkpoints": {},
        "global_observation_count": 4,
    }


def coverage_fixture(count: int) -> dict[str, Any]:
    exact_count = min(count, 130)
    return {
        "checked": True,
        "reason": "fixture",
        "source_file_count": 131,
        "valid_source_file_count": 131,
        "invalid_file_count": 0,
        "invalid_files": [],
        "quarantined_required_date_codes": [],
        "quarantined_file_sha256": {"801156": "e" * 64},
        "quarantine_exact_date_exclusion_passed": True,
        "entry_industry_count": exact_count,
        "exit_industry_count": exact_count,
        "entry_exit_common_count": exact_count,
        "candidate_entry_count": 4 if exact_count >= MIN_BENCHMARK_INDUSTRIES else 0,
        "candidate_exit_count": 4 if exact_count >= MIN_BENCHMARK_INDUSTRIES else 0,
        "candidate_common_count": 4 if exact_count >= MIN_BENCHMARK_INDUSTRIES else 0,
        "minimum_benchmark_industries": MIN_BENCHMARK_INDUSTRIES,
        "overall_max_date": EXIT_DATE,
        "price_values_retained": False,
        "exact_coverage_ready": exact_count >= MIN_BENCHMARK_INDUSTRIES,
    }


if __name__ == "__main__":
    raise SystemExit(main())
