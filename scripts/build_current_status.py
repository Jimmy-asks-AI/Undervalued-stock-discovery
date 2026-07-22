from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_v5_31_fund_flow_evidence_freeze_manifest as v531
import fund_flow_forward_evidence as fund_flow_evidence
import fund_flow_exploratory_disposition as exploratory_disposition
from research_integrity import atomic_write_csv, atomic_write_json, atomic_write_text
from research_evidence_routes import (
    FORWARD_EVIDENCE_ROUTE_BLOCKER,
    verified_forward_evidence_ready,
    verified_forward_industry_ready,
    verified_forward_timing_ready,
)
from valuation_pit_contract import methodology_route_ready


STATUS_PATH = ROOT / "CURRENT_STATUS.md"
OUT = ROOT / "outputs" / "audit" / "current_status"
DEBUG = OUT / "debug"

JSON_PATHS = {
    "state_audit": ROOT / "outputs" / "audit" / "current_state_consistency" / "run_summary.json",
    "state_sources": ROOT / "outputs" / "audit" / "current_state_consistency" / "debug" / "state_sources.json",
    "current": ROOT / "outputs" / "etf_assisted_trading_current" / "run_summary.json",
    "recommendation": ROOT / "outputs" / "etf_assisted_trading_current" / "debug" / "recommendation.json",
    "completion": ROOT / "outputs" / "audit" / "etf_assisted_trading_completion" / "run_summary.json",
    "v470": ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "run_summary.json",
    "v471": ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "run_summary.json",
    "v485": ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85" / "run_summary.json",
    "v507": ROOT / "outputs" / "audit" / "rebound_leader_promotion_evaluator_v5_07" / "run_summary.json",
    "v508": ROOT / "outputs" / "audit" / "rebound_leader_forward_signal_detector_v5_08" / "run_summary.json",
    "v510": ROOT / "outputs" / "audit" / "rebound_leader_goal_completion_audit_v5_10" / "run_summary.json",
    "pit_methodology": ROOT / "outputs" / "audit" / "pit_universe_methodology_remediation" / "run_summary.json",
    "v531": ROOT / "outputs" / "audit" / "fund_flow_evidence_freeze_manifest_v5_31" / "run_summary.json",
    "v535": ROOT / "outputs" / "audit" / "fund_flow_waiting_room_v5_35" / "run_summary.json",
    "experiment_audit": ROOT / "outputs" / "audit" / "research_experiment_ledger" / "run_summary.json",
    "active_pointer": ROOT / "logs" / "v5_31_fund_flow_evidence_freeze_active.json",
    "account": ROOT / "portfolio_lab" / "current_account_state.json",
    "inventory": ROOT / "logs" / "research_version_inventory.json",
    "governance_coverage": ROOT / "outputs" / "audit" / "research_governance_coverage" / "run_summary.json",
}

SOURCE_MANIFEST_PATH = ROOT / "outputs" / "etf_assisted_trading_current" / "debug" / "source_manifest.csv"
GATE_RESULTS_PATH = ROOT / "outputs" / "etf_assisted_trading_current" / "debug" / "gate_results.csv"
EXPERIMENT_LEDGER_PATH = ROOT / "logs" / "research_experiment_ledger.jsonl"
FUND_FLOW_LEDGER_PATH = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
ENTRY_FREEZE_LEDGER_PATH = ROOT / "logs" / "v5_33_fund_flow_entry_price_freeze.jsonl"
BENCHMARK_FREEZE_LEDGER_PATH = ROOT / "logs" / "v5_34_fund_flow_benchmark_entry_freeze.jsonl"

OPERATING_SOURCE_KEYS = (
    "state_audit",
    "state_sources",
    "current",
    "recommendation",
    "completion",
    "v470",
    "v471",
    "v485",
    "v507",
    "v508",
    "v510",
    "pit_methodology",
    "v531",
    "v535",
    "experiment_audit",
    "active_pointer",
    "account",
    "inventory",
)

GATE_LABELS = {
    "data_freshness": "数据完整性与时点",
    "pit_universe_methodology": "PIT估值与行业历史方法门",
    "timing_robustness": "V4.71 择时稳健性",
    "industry_selection": "强行业选择",
    "etf_pit_master": "ETF PIT 主表",
    "account_state": "账户状态",
    "portfolio_risk": "现有组合风险",
    "goal_evidence": "V5.10 目标证据",
    "agent_veto_chain": "六角色确定性否决链",
    "projected_portfolio_risk": "建议后组合风险",
    "forward_timing_evidence": "择时前推证据",
    "forward_industry_evidence": "强行业前推证据",
}

GATE_FIELDS = (
    ("data_freshness", "data_gate_passed"),
    ("pit_universe_methodology", "pit_universe_methodology_gate_passed"),
    ("timing_robustness", "timing_gate_passed"),
    ("industry_selection", "industry_selection_gate_passed"),
    ("etf_pit_master", "etf_pit_gate_passed"),
    ("account_state", "account_state_gate_passed"),
    ("portfolio_risk", "portfolio_risk_gate_passed"),
    ("goal_evidence", "goal_evidence_gate_passed"),
    ("agent_veto_chain", ""),
    ("projected_portfolio_risk", "projected_portfolio_risk_gate_passed"),
    ("forward_timing_evidence", "forward_timing_gate_passed"),
    ("forward_industry_evidence", "forward_industry_gate_passed"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the fail-closed project CURRENT_STATUS snapshot.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    sources = load_sources()
    status = build_status(sources, current_date=date.today())
    checks = build_checks(status, sources)
    summary = build_summary(status, checks)
    write_outputs(status, checks, summary, sources)
    print(f"status_path={STATUS_PATH}")
    print(f"output_dir={OUT}")
    print(f"status_valid={str(summary['status_valid']).lower()}")
    print(f"governance_ready={str(summary['governance_ready']).lower()}")
    print(f"fail_count={summary['fail_count']}")
    if not summary["status_valid"]:
        raise SystemExit(2)


def load_sources() -> dict[str, Any]:
    sources: dict[str, Any] = {key: read_json(path) for key, path in JSON_PATHS.items()}
    sources["source_manifest"] = read_csv(SOURCE_MANIFEST_PATH)
    sources["gate_results"] = read_csv(GATE_RESULTS_PATH)
    sources["experiment_records"] = read_jsonl(EXPERIMENT_LEDGER_PATH)
    sources["entry_freeze_records"] = read_jsonl(ENTRY_FREEZE_LEDGER_PATH)
    sources["benchmark_freeze_records"] = read_jsonl(BENCHMARK_FREEZE_LEDGER_PATH)
    sources["source_presence"] = {
        **{key: path.is_file() for key, path in JSON_PATHS.items()},
        "source_manifest": SOURCE_MANIFEST_PATH.is_file(),
        "gate_results": GATE_RESULTS_PATH.is_file(),
        "experiment_records": EXPERIMENT_LEDGER_PATH.is_file(),
        "fund_flow_records": FUND_FLOW_LEDGER_PATH.is_file(),
    }

    try:
        sources["validated_active"] = v531.validated_active_cohort()
        sources["active_validation_error"] = ""
    except Exception as exc:  # fail closed; the error is preserved in ignored audit output
        sources["validated_active"] = {}
        sources["active_validation_error"] = f"{type(exc).__name__}: {exc}"

    try:
        events = fund_flow_evidence.read_events(FUND_FLOW_LEDGER_PATH)
        sources["fund_flow_records"] = fund_flow_evidence.materialize_observations(events)
        sources["fund_flow_ledger_verified"] = True
        sources["fund_flow_ledger_error"] = ""
    except Exception as exc:  # fail closed; never count an unverified ledger
        sources["fund_flow_records"] = []
        sources["fund_flow_ledger_verified"] = False
        sources["fund_flow_ledger_error"] = f"{type(exc).__name__}: {exc}"

    disposition_presence = exploratory_disposition.artifact_presence()
    sources["exploratory_disposition_artifact_present"] = any(disposition_presence.values())
    sources["exploratory_disposition_artifact_presence"] = disposition_presence
    try:
        loaded_disposition = (
            exploratory_disposition.load_optional_disposition(sources.get("validated_active", {})) or {}
        )
        sources["exploratory_disposition"] = loaded_disposition
        sources["exploratory_disposition_summary"] = (
            read_json(exploratory_disposition.SUMMARY_PATH)
            if loaded_disposition.get("valid") is True
            else {}
        )
        sources["exploratory_disposition_error"] = ""
    except (exploratory_disposition.ExploratoryDispositionError, OSError, json.JSONDecodeError) as exc:
        sources["exploratory_disposition"] = {}
        sources["exploratory_disposition_summary"] = {}
        sources["exploratory_disposition_error"] = str(exc)
    return sources


def build_status(sources: Mapping[str, Any], *, current_date: date) -> dict[str, Any]:
    current = as_mapping(sources.get("current"))
    recommendation = as_mapping(sources.get("recommendation"))
    active = as_mapping(sources.get("validated_active"))
    inventory = as_mapping(sources.get("inventory"))
    coverage = as_mapping(sources.get("governance_coverage"))
    experiment_records = as_rows(sources.get("experiment_records"))
    source_manifest = as_rows(sources.get("source_manifest"))
    samples = classify_fund_flow_observations(as_rows(sources.get("fund_flow_records")), active)
    samples["late_backfill_excluded_observation_count"] = late_backfill_observation_count(
        as_rows(sources.get("entry_freeze_records")),
        as_rows(sources.get("benchmark_freeze_records")),
    )
    disposition_state = resolve_exploratory_disposition(
        sources,
        observation_count=int_or_zero(samples.get("exploratory_fund_flow_observations")),
    )
    settlement_data_boundary = resolve_exploratory_settlement_data_boundary(
        sources,
        disposition_state,
    )
    samples.update({
        "exploratory_fund_flow_settled": int_or_zero(disposition_state.get("settled_count")),
        "exploratory_fund_flow_terminal_blocked": int_or_zero(disposition_state.get("terminal_blocked_count")),
        "exploratory_fund_flow_pending": int_or_zero(disposition_state.get("pending_count")),
        "exploratory_fund_flow_qualified_settled": int_or_zero(disposition_state.get("qualified_settled_count")),
    })

    source_cutoffs = {
        str(row.get("source", "")): str(row.get("latest_date", ""))
        for row in source_manifest
        if str(row.get("source", ""))
    }
    recommendation_cutoffs = {
        str(key): str(value or "")
        for key, value in as_mapping(recommendation.get("data_cutoff_by_source")).items()
    }
    decision_as_of = str(current.get("as_of_date", ""))
    gate_details = {str(row.get("gate", "")): str(row.get("evidence", "")) for row in as_rows(sources.get("gate_results"))}
    hard_gates = build_hard_gates(current, gate_details)
    registrations = build_registration_snapshot(experiment_records)
    inventory_state = governance_inventory_state(inventory)
    coverage_state = governance_coverage_state(coverage)
    governance_ready = inventory_state["passed"] and coverage_state["status"] == "pass"

    v507 = as_mapping(sources.get("v507"))
    verified_timing_ready = verified_forward_timing_ready(v507)
    verified_industry_ready = verified_forward_industry_ready(v507)
    verified_forward_ready = verified_forward_evidence_ready(v507)
    strong_forward_count = int_or_zero(v507.get("best_forward_event_count")) if verified_industry_ready else 0
    strong_forward_required = max(
        [int_or_zero(item.get("required_new_forward_event_count")) for item in registrations] or [0]
    )
    state_audit = as_mapping(sources.get("state_audit"))
    completion = as_mapping(sources.get("completion"))
    goal = as_mapping(sources.get("v510"))
    pit_methodology = as_mapping(sources.get("pit_methodology"))
    account = as_mapping(sources.get("account"))

    return {
        "schema_version": "1.0.0",
        "source_snapshot_generated_at": latest_source_generated_at(sources, current_date),
        "current_date": current_date.isoformat(),
        "decision_as_of": decision_as_of,
        "run_boundary_data_cutoff_date": str(recommendation.get("data_cutoff_date", "")),
        "source_cutoffs": source_cutoffs,
        "recommendation_source_cutoffs": recommendation_cutoffs,
        "policy_status": str(current.get("policy_status", "missing")),
        "action": str(current.get("action", "NO_ACTION") or "NO_ACTION"),
        "manual_decision_support_ready": current.get("manual_decision_support_ready") is True,
        "production_ready": current.get("production_ready") is True,
        "auto_execution_allowed": current.get("auto_execution_allowed") is True,
        "strong_industry_alpha_validated": goal.get("goal_ready") is True and goal.get("can_claim_strong_rebound_industries") is True,
        "state_consistent": state_audit.get("state_consistent") is True,
        "state_audit_generated_at": str(state_audit.get("generated_at", "")),
        "current_generated_at": str(current.get("generated_at", "")),
        "goal_audit_generated_at": str(goal.get("generated_at", "")),
        "pit_methodology": {
            "generated_at": str(pit_methodology.get("generated_at", "")),
            "audit_passed": pit_methodology.get("audit_passed") is True,
            "methodology_remediation_complete": pit_methodology.get("methodology_remediation_complete") is True,
            "promotion_gate_passed": pit_methodology.get("promotion_gate_passed") is True,
            "route_gate_passed": current.get("pit_universe_methodology_gate_passed") is True,
            "true_forward_route_ready": verified_forward_ready,
            "forward_timing_evidence_verified": verified_timing_ready,
            "forward_industry_evidence_verified": verified_industry_ready,
            "forward_route_integrity_status": "verified" if verified_forward_ready else "blocked",
            "forward_route_integrity_blocker": "" if verified_forward_ready else FORWARD_EVIDENCE_ROUTE_BLOCKER,
            "valuation_availability_status": str(pit_methodology.get("valuation_availability_status", "unknown")),
            "promotion_eligible_valuation_row_count": int_or_zero(pit_methodology.get("promotion_eligible_valuation_row_count")),
            "direct_source_cutoff": str(pit_methodology.get("valuation_direct_source_max_trade_date", "")),
            "raw_max_trade_date": str(pit_methodology.get("valuation_raw_max_trade_date", "")),
            "recovered_snapshot_row_count": int_or_zero(pit_methodology.get("recovered_snapshot_row_count")),
            "classification_history_status": str(pit_methodology.get("classification_history_status", "unknown")),
            "industry_history_file_count": int_or_zero(pit_methodology.get("industry_history_file_count")),
            "industry_history_fresh_file_count": int_or_zero(pit_methodology.get("industry_history_fresh_file_count")),
            "industry_history_long_tail_gap_file_count": int_or_zero(pit_methodology.get("industry_history_long_tail_gap_file_count")),
            "industry_history_ordinary_stale_file_count": int_or_zero(pit_methodology.get("industry_history_ordinary_stale_file_count")),
            "industry_history_ordinary_stale_codes": list(pit_methodology.get("industry_history_ordinary_stale_codes", [])) if isinstance(pit_methodology.get("industry_history_ordinary_stale_codes"), list) else [],
            "identity_episode_count": int_or_zero(pit_methodology.get("identity_episode_count")),
            "observed_name_episode_count": int_or_zero(pit_methodology.get("observed_name_episode_count")),
            "name_changed_industry_code_count": int_or_zero(pit_methodology.get("name_changed_industry_code_count")),
            "reused_industry_code_count": int_or_zero(pit_methodology.get("reused_industry_code_count")),
            "historical_beta_identity_safe": pit_methodology.get("historical_beta_identity_safe") is True,
            "excluded_identity_unsafe_features": list(pit_methodology.get("excluded_identity_unsafe_features", [])) if isinstance(pit_methodology.get("excluded_identity_unsafe_features"), list) else [],
            "historical_review_set_label": str(pit_methodology.get("historical_review_set_label", "")),
            "true_forward_earliest_evidence_date": str(pit_methodology.get("true_forward_earliest_evidence_date", "")),
            "blocking_reasons": list(pit_methodology.get("blocking_reasons", [])) if isinstance(pit_methodology.get("blocking_reasons"), list) else [],
        },
        "versions": build_versions(sources, active),
        "freeze_layers": {
            "strong_industry_forward_rules": registrations,
            "fund_flow_evidence_cohort": {
                "cohort_id": str(active.get("cohort_id", "")),
                "manifest_hash": str(active.get("manifest_hash", "")),
                "freeze_passed": active.get("freeze_passed") is True,
                "verified_at_utc": str(active.get("verified_at_utc", "")),
                "validation_reason": str(active.get("validation_reason", "")),
            },
        },
        "exploratory_disposition": disposition_state,
        "exploratory_settlement_data_boundary": settlement_data_boundary,
        "sample_counts": {
            "strong_industry_qualified_forward": strong_forward_count,
            "strong_industry_required_per_rule": strong_forward_required,
            **samples,
        },
        "hard_gates": hard_gates,
        "blocking_gates": [row["gate_id"] for row in hard_gates if row["status"] != "pass"],
        "account_state": {
            "configured": account.get("configured") is True,
            "as_of_date": str(account.get("as_of_date", "")),
            "position_count": len(account.get("positions", [])) if isinstance(account.get("positions"), list) else 0,
            "gate_passed": current.get("account_state_gate_passed") is True,
        },
        "completion": {
            "generated_at": str(completion.get("generated_at", "")),
            "implementation": f"{int_or_zero(completion.get('implementation_pass_count'))}/{int_or_zero(completion.get('implementation_check_count'))}",
            "readiness": f"{int_or_zero(completion.get('readiness_pass_count'))}/{int_or_zero(completion.get('readiness_check_count'))}",
            "behavior": f"{int_or_zero(completion.get('behavior_test_pass_count'))}/{int_or_zero(completion.get('behavior_test_count'))}",
        },
        "governance": {
            "inventory": inventory_state,
            "coverage": coverage_state,
            "ready": governance_ready,
        },
        "next_allowed_actions": next_allowed_actions(decision_as_of, governance_ready=governance_ready),
        "forbidden_actions": forbidden_actions(),
        "recovery_conditions": recovery_conditions(),
    }


def build_versions(sources: Mapping[str, Any], active: Mapping[str, Any]) -> list[dict[str, str]]:
    current = as_mapping(sources.get("current"))
    v531_summary = as_mapping(sources.get("v531"))
    v535_summary = as_mapping(sources.get("v535"))
    return [
        version_row("策略版本", "V4.70", sources.get("v470"), "冻结市场反弹窗口；不等于强行业选择 Alpha。"),
        version_row("稳健性审计版本", "V4.71", sources.get("v471"), "V4.70 的参数扰动、独立样本与实盘辅助审计。"),
        version_row("强行业研究版本", "V4.85", sources.get("v485"), "父行业中性候选规则；当前无稳健通过规则。"),
        version_row("前推评价版本", "V5.07", sources.get("v507"), "只评价冻结规则与已结算前推样本。"),
        version_row("前推检测版本", "V5.08", sources.get("v508"), "只检测冻结日后的自然触发，不回填历史。"),
        version_row("研究审计版本", "V5.10", sources.get("v510"), "目标完成度审计；不是策略版本。"),
        {
            "kind": "数据治理版本",
            "name": "V5.31 / V5.35",
            "artifact_version": f"{v531_summary.get('version', 'missing')} / {v535_summary.get('version', 'missing')}",
            "boundary": "V5.31 固定不可变证据 cohort，V5.35 只管理等待室；二者都不证明 Alpha。",
        },
        {
            "kind": "当前 runner 版本",
            "name": "CURRENT_MAINLINE",
            "artifact_version": str(current.get("version", "missing")),
            "boundary": "ETF 辅助人工决策聚合层；硬门禁未清零时只允许 NO_ACTION。",
        },
        {
            "kind": "前推 cohort（数据批次）",
            "name": str(active.get("cohort_id", "missing")),
            "artifact_version": short_hash(str(active.get("manifest_hash", ""))),
            "boundary": "cohort 是证据冻结批次，不是软件版本、策略版本或 Alpha 结论。",
        },
    ]


def version_row(kind: str, name: str, payload: Any, boundary: str) -> dict[str, str]:
    row = as_mapping(payload)
    return {
        "kind": kind,
        "name": name,
        "artifact_version": str(row.get("version", "missing")),
        "boundary": boundary,
    }


def build_registration_snapshot(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in records:
        required = 0
        for criterion in record.get("promotion_criteria", []) if isinstance(record.get("promotion_criteria"), list) else []:
            if isinstance(criterion, Mapping) and criterion.get("metric") == "new_forward_event_count":
                required = int_or_zero(criterion.get("required"))
        result.append({
            "experiment_id": str(record.get("experiment_id", "")),
            "registration_status": str(record.get("registration_status", "")),
            "registered_at": str(record.get("registered_at", "")),
            "evidence_start_date": str(record.get("evidence_start_date", "")),
            "frozen_rule": str(record.get("frozen_rule", "")),
            "rule_definition": str(record.get("rule_definition", "")),
            "required_new_forward_event_count": required,
            "allowed_next_action": str(record.get("allowed_next_action", "")),
            "forbidden_next_action": str(record.get("forbidden_next_action", "")),
        })
    return result


def resolve_exploratory_disposition(
    sources: Mapping[str, Any],
    *,
    observation_count: int,
) -> dict[str, Any]:
    loaded = as_mapping(sources.get("exploratory_disposition"))
    artifact_present = sources.get("exploratory_disposition_artifact_present") is True
    load_error = str(sources.get("exploratory_disposition_error", "")).strip()
    if loaded.get("valid") is True:
        loaded_count = int_or_zero(loaded.get("observation_count"))
        if loaded_count == observation_count:
            return {key: value for key, value in loaded.items() if key != "rows"}
        load_error = (
            "formal disposition observation count does not match the verified ledger: "
            f"disposition={loaded_count}; ledger={observation_count}"
        )
        artifact_present = True
    state = exploratory_disposition.pending_disposition(observation_count, error=load_error)
    state["artifact_present"] = artifact_present
    return state


def resolve_exploratory_settlement_data_boundary(
    sources: Mapping[str, Any],
    disposition: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose the already-validated formal coverage as a presentation-only boundary."""

    summary = as_mapping(sources.get("exploratory_disposition_summary"))
    coverage = as_mapping(summary.get("price_coverage"))
    calendar = as_mapping(summary.get("calendar_validation"))
    observation_count = int_or_zero(disposition.get("observation_count"))
    common_count = int_or_none(coverage.get("entry_exit_common_count"))
    target_common_count = int_or_none(coverage.get("candidate_common_count"))
    entry_date = str(calendar.get("entry_date", ""))
    exit_date = str(calendar.get("exit_date", ""))
    available = bool(
        disposition.get("valid") is True
        and summary.get("settlement_disposition_complete") is True
        and summary.get("completion_status") == "complete_terminal_exclusions"
        and summary.get("calendar_dates_valid") is True
        and coverage.get("exact_coverage_ready") is True
        and coverage.get("price_values_retained") is False
        and entry_date
        and exit_date
        and common_count is not None
        and common_count > 0
        and target_common_count == observation_count
        and observation_count > 0
    )
    return {
        "available": available,
        "scope": "settlement_only" if available else "",
        "entry_date": entry_date if available else "",
        "exit_date": exit_date if available else "",
        "exact_common_industry_count": common_count if available else 0,
        "target_common_count": target_common_count if available else 0,
        "target_count": observation_count if available else 0,
        "return_values_present": disposition.get("return_values_present") is True,
    }


def classify_fund_flow_observations(rows: Sequence[Mapping[str, Any]], active: Mapping[str, Any]) -> dict[str, int]:
    cohort_id = str(active.get("cohort_id", ""))
    manifest_hash = str(active.get("manifest_hash", ""))
    active_rows: list[Mapping[str, Any]] = []
    exploratory_rows: list[Mapping[str, Any]] = []
    wrong_active_pair = 0
    for row in rows:
        row_cohort = str(row.get("cohort_id", ""))
        row_hash = str(row.get("cohort_manifest_hash", ""))
        exact_pair = bool(cohort_id and manifest_hash and row_cohort == cohort_id and row_hash == manifest_hash)
        if exact_pair:
            active_rows.append(row)
        else:
            exploratory_rows.append(row)
            if cohort_id and row_cohort == cohort_id and row_hash != manifest_hash:
                wrong_active_pair += 1
    active_integrity = [row for row in active_rows if is_true(row.get("integrity_eligible"))]
    active_goal = [
        row for row in active_integrity
        if is_true(row.get("qualified_for_goal")) and is_true(row.get("promotion_eligible"))
    ]
    active_settled = [
        row for row in active_goal
        if str(row.get("settlement_status", "")) == "settled"
        and str(row.get("outcome_status", "")) == "settled_forward_observation"
    ]
    return {
        "active_fund_flow_observations": len(active_rows),
        "active_fund_flow_integrity_eligible": len(active_integrity),
        "active_fund_flow_goal_qualified": len(active_goal),
        "active_fund_flow_settled_qualified": len(active_settled),
        "exploratory_fund_flow_observations": len(exploratory_rows),
        "stale_active_pair_observations": wrong_active_pair,
        "global_fund_flow_observations": len(rows),
    }


def late_backfill_observation_count(*record_groups: Sequence[Mapping[str, Any]]) -> int:
    observation_ids: set[str] = set()
    for records in record_groups:
        for row in records:
            if is_true(row.get("late_backfill_excluded")):
                observation_id = str(row.get("observation_id", ""))
                if observation_id:
                    observation_ids.add(observation_id)
    return len(observation_ids)


def build_hard_gates(current: Mapping[str, Any], details: Mapping[str, str]) -> list[dict[str, str]]:
    blocking = {str(item) for item in current.get("blocking_gates", []) if str(item)} if isinstance(current.get("blocking_gates"), list) else set()
    rows: list[dict[str, str]] = []
    for gate_id, field in GATE_FIELDS:
        if field:
            passed = current.get(field) is True
        else:
            passed = gate_id not in blocking
        rows.append({
            "gate_id": gate_id,
            "label": GATE_LABELS[gate_id],
            "status": "pass" if passed else "blocked",
            "evidence": str(details.get(gate_id, "")) or (f"{field}={current.get(field)}" if field else f"blocking_gates contains {gate_id}"),
        })
    return rows


def governance_inventory_state(inventory: Mapping[str, Any]) -> dict[str, Any]:
    summary = as_mapping(inventory.get("summary"))
    record_count = int_or_zero(summary.get("record_count"))
    expected_count = int_or_zero(summary.get("expected_record_count"))
    governance_counts = as_mapping(summary.get("governance_status_counts"))
    fail_count = int_or_zero(governance_counts.get("fail"))
    pass_count = int_or_zero(governance_counts.get("pass"))
    passed = (
        bool(inventory)
        and expected_count == 65
        and record_count == expected_count
        and pass_count == record_count
        and fail_count == 0
    )
    return {
        "present": bool(inventory),
        "schema_version": str(inventory.get("schema_version", "")),
        "inventory_as_of": str(inventory.get("inventory_as_of", "")),
        "record_count": record_count,
        "expected_record_count": expected_count,
        "governance_pass_count": pass_count,
        "governance_fail_count": fail_count,
        "passed": passed,
        "status": "pass" if passed else ("fail" if inventory else "pending_missing"),
    }


def governance_coverage_state(coverage: Mapping[str, Any]) -> dict[str, Any]:
    if not coverage:
        return {"present": False, "status": "pending_missing", "fail_count": None, "check_count": None, "generated_at": ""}
    fail_count_value = first_present(coverage, "fail_count", "governance_fail_count", "coverage_fail_count")
    check_count_value = first_present(coverage, "check_count", "governance_check_count", "coverage_check_count", "version_count")
    explicit_pass = first_present(
        coverage,
        "governance_coverage_passed",
        "coverage_passed",
        "governance_passed",
        "governance_ready",
        "coverage_ready",
        "audit_passed",
        "all_passed",
    )
    explicit_status = str(first_present(coverage, "status", "governance_status", "final_status") or "").lower()
    fail_count = int_or_none(fail_count_value)
    check_count = int_or_none(check_count_value)
    passed = (
        explicit_pass is True
        or explicit_status in {"pass", "passed", "complete"}
        or (fail_count == 0 and check_count is not None and check_count > 0)
    )
    if explicit_pass is False or explicit_status in {"fail", "failed", "blocked", "pending"} or (fail_count is not None and fail_count > 0):
        passed = False
    return {
        "present": True,
        "status": "pass" if passed else "fail_closed",
        "fail_count": fail_count,
        "check_count": check_count,
        "generated_at": str(coverage.get("generated_at", "")),
        "policy_id": str(coverage.get("policy_id", "")),
    }


def build_checks(status: Mapping[str, Any], sources: Mapping[str, Any]) -> list[dict[str, str]]:
    current = as_mapping(sources.get("current"))
    recommendation = as_mapping(sources.get("recommendation"))
    state_audit = as_mapping(sources.get("state_audit"))
    completion = as_mapping(sources.get("completion"))
    goal = as_mapping(sources.get("v510"))
    pit_methodology = as_mapping(sources.get("pit_methodology"))
    v507 = as_mapping(sources.get("v507"))
    v508 = as_mapping(sources.get("v508"))
    active = as_mapping(sources.get("validated_active"))
    account = as_mapping(sources.get("account"))
    presence = as_mapping(sources.get("source_presence"))
    decision_as_of = str(status.get("decision_as_of", ""))
    missing_operating = [key for key in OPERATING_SOURCE_KEYS if not presence.get(key, bool(sources.get(key)))]
    required_sidecars = [key for key in ("source_manifest", "gate_results", "experiment_records", "fund_flow_records") if not presence.get(key, bool(sources.get(key)))]

    state_pair = (
        str(state_audit.get("active_cohort_id", "")),
        str(state_audit.get("active_cohort_manifest_hash", "")),
    )
    active_pair = (str(active.get("cohort_id", "")), str(active.get("manifest_hash", "")))
    source_cutoffs = as_mapping(status.get("source_cutoffs"))
    recommendation_cutoffs = as_mapping(status.get("recommendation_source_cutoffs"))
    cutoff_future = [f"{key}={value}" for key, value in source_cutoffs.items() if value and decision_as_of and value > decision_as_of]
    account_stale = str(account.get("as_of_date", "")) != decision_as_of
    account_gate_consistent = not account_stale or current.get("account_state_gate_passed") is True
    if account_stale:
        account_gate_consistent = current.get("account_state_gate_passed") is False

    registrations = as_mapping(status.get("freeze_layers")).get("strong_industry_forward_rules", [])
    registrations_valid = (
        isinstance(registrations, list)
        and len(registrations) >= 2
        and all(
            isinstance(row, Mapping)
            and row.get("registration_status") == "preregistered_forward_only"
            and int_or_zero(row.get("required_new_forward_event_count")) > 0
            and row.get("allowed_next_action") == "append_new_forward_samples_only"
            and row.get("forbidden_next_action") == "do_not_change_thresholds_from_historical_results"
            for row in registrations
        )
    )
    sample_counts = as_mapping(status.get("sample_counts"))
    disposition_state = as_mapping(status.get("exploratory_disposition"))
    settlement_data_boundary = as_mapping(status.get("exploratory_settlement_data_boundary"))
    disposition_present = disposition_state.get("artifact_present") is True
    disposition_valid = disposition_state.get("valid") is True
    disposition_pair_matches_active = (
        str(disposition_state.get("active_cohort_id", "")),
        str(disposition_state.get("active_cohort_manifest_hash", "")),
    ) == active_pair
    disposition_counts_consistent = (
        int_or_zero(disposition_state.get("observation_count"))
        == int_or_zero(sample_counts.get("exploratory_fund_flow_observations"))
        and int_or_zero(disposition_state.get("settled_count"))
        + int_or_zero(disposition_state.get("terminal_blocked_count"))
        + int_or_zero(disposition_state.get("pending_count"))
        == int_or_zero(disposition_state.get("observation_count"))
        and int_or_zero(disposition_state.get("qualified_settled_count")) == 0
    )
    governance = as_mapping(status.get("governance"))
    inventory = as_mapping(governance.get("inventory"))
    coverage = as_mapping(governance.get("coverage"))
    verified_timing_ready = verified_forward_timing_ready(v507)
    verified_industry_ready = verified_forward_industry_ready(v507)
    verified_forward_ready = verified_forward_evidence_ready(v507)
    current_forward_ready = verified_forward_ready
    goal_forward_ready = verified_forward_ready
    expected_current_methodology_gate = methodology_route_ready(pit_methodology, v507)
    expected_goal_methodology_gate = methodology_route_ready(pit_methodology, v507)
    methodology_blocker_present = "pit_universe_methodology" in current.get("blocking_gates", [])

    checks = [
        audit_check("operating_sources_present", not missing_operating and not required_sidecars, "source_integrity", f"missing={missing_operating + required_sidecars}", "所有 CURRENT_STATUS 权威输入均须存在。"),
        audit_check("current_state_audit_passed", state_audit.get("state_consistent") is True and int_or_zero(state_audit.get("fail_count")) == 0, "source_integrity", f"state_consistent={state_audit.get('state_consistent')}; fail_count={state_audit.get('fail_count')}", "当前状态一致性审计必须通过。"),
        audit_check("state_audit_not_stale", timestamp_not_older(state_audit.get("generated_at"), current.get("generated_at")), "source_integrity", f"state_audit={state_audit.get('generated_at')}; current={current.get('generated_at')}", "状态一致性审计不得早于当前 runner 快照。"),
        audit_check("state_audit_matches_current", state_audit.get("current_as_of_date") == current.get("as_of_date") and state_audit.get("current_action") == current.get("action"), "source_integrity", f"audit=({state_audit.get('current_as_of_date')},{state_audit.get('current_action')}); current=({current.get('as_of_date')},{current.get('action')})", "状态审计和 runner 必须指向同一决策快照。"),
        audit_check("current_boundary_safe", current.get("policy_status") == "research_only" and current.get("action") == "NO_ACTION" and current.get("manual_decision_support_ready") is False and current.get("production_ready") is False and current.get("auto_execution_allowed") is False, "boundary", f"policy={current.get('policy_status')}; action={current.get('action')}; manual={current.get('manual_decision_support_ready')}; production={current.get('production_ready')}; auto={current.get('auto_execution_allowed')}", "当前显著边界必须保持 research_only / NO_ACTION / 人工未就绪 / 自动禁止。"),
        audit_check("pit_methodology_control_audited", pit_methodology.get("audit_passed") is True and pit_methodology.get("methodology_remediation_complete") is True and pit_methodology.get("legacy_oos_label_corrected") is True, "source_integrity", f"audit={pit_methodology.get('audit_passed')}; remediation={pit_methodology.get('methodology_remediation_complete')}; label_corrected={pit_methodology.get('legacy_oos_label_corrected')}", "方法整改控制应通过，且 2022+ 旧 OOS 标签必须降级为迭代历史审查。"),
        audit_check(
            "pit_methodology_route_consistent",
            current.get("pit_universe_methodology_gate_passed") is expected_current_methodology_gate
            and goal.get("pit_universe_methodology_gate_passed") is expected_goal_methodology_gate
            and current.get("forward_timing_gate_passed") is verified_timing_ready
            and current.get("forward_industry_gate_passed") is verified_industry_ready
            and goal.get("true_forward_route_ready") is verified_forward_ready
            and methodology_blocker_present is (not expected_current_methodology_gate),
            "boundary",
            f"audit={pit_methodology.get('audit_passed')}; historical={pit_methodology.get('promotion_gate_passed')}; verified_timing={verified_timing_ready}; verified_industry={verified_industry_ready}; verified_forward={verified_forward_ready}; current={current.get('pit_universe_methodology_gate_passed')}/{expected_current_methodology_gate}; goal={goal.get('pit_universe_methodology_gate_passed')}/{expected_goal_methodology_gate}; blocker={methodology_blocker_present}",
            "方法门必须统一使用“控制审计通过，且历史或独立前推路线通过”的真值表；无合格路线时继续失败关闭。",
        ),
        audit_check("valuation_cutoff_excludes_recovered_snapshot", pit_methodology.get("valuation_direct_source_max_trade_date") == "2025-12-31" and source_cutoffs.get("valuation_history") == "2025-12-31" and source_cutoffs.get("pit_valuation_methodology") == "2025-12-31" and int_or_zero(pit_methodology.get("recovered_snapshot_row_count")) > 0, "source_integrity", f"method={pit_methodology.get('valuation_direct_source_max_trade_date')}; manifest_history={source_cutoffs.get('valuation_history')}; manifest_method={source_cutoffs.get('pit_valuation_methodology')}; recovered={pit_methodology.get('recovered_snapshot_row_count')}", "回收快照不得伪装为官方历史估值截止；真实直接来源截止必须保持 2025-12-31。"),
        audit_check("recommendation_matches_current", recommendation.get("action") == current.get("action") and recommendation.get("policy_id") == current.get("policy_id") and recommendation.get("data_cutoff_date") == current.get("as_of_date") and recommendation.get("auto_execution_allowed") is False, "source_integrity", f"recommendation=({recommendation.get('policy_id')},{recommendation.get('action')},{recommendation.get('data_cutoff_date')}); current=({current.get('policy_id')},{current.get('action')},{current.get('as_of_date')})", "推荐合同必须绑定当前 runner；data_cutoff_date 只作请求边界。"),
        audit_check("source_cutoffs_match_manifest", source_cutoffs == recommendation_cutoffs and bool(source_cutoffs), "source_integrity", f"manifest={dict(source_cutoffs)}; recommendation={dict(recommendation_cutoffs)}", "各源真实截止日必须由 source_manifest 与 recommendation 双向一致证明。"),
        audit_check("source_cutoffs_not_future", not cutoff_future, "source_integrity", f"decision_as_of={decision_as_of}; future={cutoff_future}", "任何真实源截止日不得晚于决策 as-of。"),
        audit_check("current_after_goal_audit", timestamp_not_older(current.get("generated_at"), goal.get("generated_at")), "source_integrity", f"current={current.get('generated_at')}; V5.10={goal.get('generated_at')}", "当前 runner 不得读取生成时间更晚的目标审计。"),
        audit_check("forward_sources_not_newer_than_goal_audit", timestamp_not_older(goal.get("generated_at"), v507.get("generated_at")) and timestamp_not_older(goal.get("generated_at"), v508.get("generated_at")), "source_integrity", f"V5.07={v507.get('generated_at')}; V5.08={v508.get('generated_at')}; V5.10={goal.get('generated_at')}", "V5.10 必须晚于或等于其前推评价与检测输入。"),
        audit_check("completion_boundary_consistent", completion.get("manual_decision_support_ready") is False and completion.get("production_ready") is False and completion.get("auto_execution_allowed") is False and completion.get("current_action") == "NO_ACTION", "boundary", f"generated={completion.get('generated_at')}; action={completion.get('current_action')}; manual={completion.get('manual_decision_support_ready')}", "工程完成度不能替代研究与实盘就绪。"),
        audit_check("strong_industry_alpha_unvalidated", goal.get("goal_ready") is False and goal.get("can_claim_strong_rebound_industries") is False and status.get("strong_industry_alpha_validated") is False, "boundary", f"goal_ready={goal.get('goal_ready')}; can_claim={goal.get('can_claim_strong_rebound_industries')}", "强行业 Alpha 未通过 V5.10 前必须保持未验证。"),
        audit_check("strong_forward_count_matches_evaluator", int_or_zero(sample_counts.get("strong_industry_qualified_forward")) == (int_or_zero(v507.get("best_forward_event_count")) if verified_industry_ready else 0), "source_integrity", f"status={sample_counts.get('strong_industry_qualified_forward')}; V5.07_raw_settled={v507.get('best_forward_event_count')}; independently_verified={verified_industry_ready}; V5.08_appended_pending={v508.get('appended_signal_count')}", "强行业合格前推样本只有在独立账本复验后才可从 V5.07 计入；V5.08 待结算追加数不得提前计入。"),
        audit_check("active_cohort_validated", active.get("freeze_passed") is True and bool(active_pair[0]) and len(active_pair[1]) == 64, "source_integrity", f"pair={active_pair}; reason={active.get('validation_reason', sources.get('active_validation_error', ''))}", "active cohort 必须重新计算并验证，而非只信任可变指针。"),
        audit_check("state_audit_matches_active_cohort", state_pair == active_pair and state_audit.get("active_cohort_validated") is True, "source_integrity", f"state={state_pair}; active={active_pair}", "状态审计和当前文档必须绑定同一 active pair。"),
        audit_check("fund_flow_ledger_verified", sources.get("fund_flow_ledger_verified") is True, "source_integrity", str(sources.get("fund_flow_ledger_error", "verified")), "资金流样本只允许从通过哈希链复验的 JSONL 物化。"),
        audit_check("no_stale_pair_counted_active", int_or_zero(sample_counts.get("stale_active_pair_observations")) == 0, "source_integrity", f"stale_pair={sample_counts.get('stale_active_pair_observations')}; active={sample_counts.get('active_fund_flow_observations')}; exploratory={sample_counts.get('exploratory_fund_flow_observations')}", "同 cohort_id 但 manifest_hash 不同的样本不得计入 active。"),
        audit_check(
            "exploratory_disposition_valid_if_present",
            not disposition_present or (
                disposition_valid
                and disposition_state.get("completion_status") == "complete_terminal_exclusions"
                and disposition_state.get("settlement_disposition_complete") is True
                and disposition_counts_consistent
                and disposition_pair_matches_active
            ),
            "source_integrity",
            f"present={disposition_present}; valid={disposition_valid}; status={disposition_state.get('completion_status')}; "
            f"total={disposition_state.get('observation_count')}; settled={disposition_state.get('settled_count')}; "
            f"blocked={disposition_state.get('terminal_blocked_count')}; pending={disposition_state.get('pending_count')}; "
            f"qualified={disposition_state.get('qualified_settled_count')}; pair_match={disposition_pair_matches_active}; "
            f"error={disposition_state.get('error', '')}",
            "正式探索处置缺失时保持 pending；一旦任一正式产物出现，摘要与四行处置必须完整、相互一致并通过严格校验。",
        ),
        audit_check(
            "exploratory_disposition_boundary_safe",
            not disposition_valid or (
                int_or_zero(sample_counts.get("exploratory_fund_flow_settled")) == 0
                and int_or_zero(sample_counts.get("exploratory_fund_flow_terminal_blocked")) == 4
                and int_or_zero(sample_counts.get("exploratory_fund_flow_pending")) == 0
                and int_or_zero(sample_counts.get("exploratory_fund_flow_qualified_settled")) == 0
                and disposition_state.get("return_values_present") is False
            ),
            "boundary",
            f"valid={disposition_valid}; settled={sample_counts.get('exploratory_fund_flow_settled')}; "
            f"blocked={sample_counts.get('exploratory_fund_flow_terminal_blocked')}; "
            f"pending={sample_counts.get('exploratory_fund_flow_pending')}; "
            f"qualified={sample_counts.get('exploratory_fund_flow_qualified_settled')}; "
            f"returns={disposition_state.get('return_values_present')}",
            "四条 legacy 探索记录只能形成 0 settled、4 terminal blocked、0 pending、0 qualified settled，且不得出现收益。",
        ),
        audit_check(
            "exploratory_settlement_data_boundary_present",
            not disposition_valid or (
                settlement_data_boundary.get("available") is True
                and settlement_data_boundary.get("scope") == "settlement_only"
                and int_or_zero(settlement_data_boundary.get("target_common_count"))
                == int_or_zero(settlement_data_boundary.get("target_count"))
                == int_or_zero(disposition_state.get("observation_count"))
                and int_or_zero(settlement_data_boundary.get("exact_common_industry_count"))
                >= int_or_zero(settlement_data_boundary.get("target_common_count"))
                and settlement_data_boundary.get("return_values_present") is False
            ),
            "source_integrity",
            f"valid={disposition_valid}; boundary={dict(settlement_data_boundary)}",
            "正式探索处置有效时，CURRENT_STATUS 必须同时披露结算专用日期、同一行业覆盖、目标覆盖及零收益边界。",
        ),
        audit_check("experiment_ledger_preregistered_forward_only", as_mapping(sources.get("experiment_audit")).get("integrity_passed") is True and as_mapping(sources.get("experiment_audit")).get("historical_results_preregistered") is False and registrations_valid, "source_integrity", f"audit={dict(as_mapping(sources.get('experiment_audit')))}; rule_count={len(registrations) if isinstance(registrations, list) else 0}", "冻结规则只预注册未来样本，不能倒称历史结果已预注册。"),
        audit_check("account_gate_represents_account_date", account_gate_consistent, "source_integrity", f"account_as_of={account.get('as_of_date')}; decision_as_of={decision_as_of}; gate={current.get('account_state_gate_passed')}", "账户快照陈旧时账户门禁必须失败。"),
        audit_check("governance_inventory_complete", inventory.get("present") is True and int_or_zero(inventory.get("record_count")) == 65 and int_or_zero(inventory.get("expected_record_count")) == 65, "governance", f"record_count={inventory.get('record_count')}; expected={inventory.get('expected_record_count')}", "研究库存必须覆盖 V4.72—V5.35 和 CURRENT_MAINLINE 共 65 条。"),
        audit_check("governance_inventory_passed", inventory.get("passed") is True, "governance", f"status={inventory.get('status')}; fail_count={inventory.get('governance_fail_count')}", "库存内缺 task brief、登记/追认、变更记录或标准输出时必须失败关闭。"),
        audit_check("governance_coverage_present", coverage.get("present") is True, "governance", f"status={coverage.get('status')}", "必须生成 research_governance_coverage 标准审计。"),
        audit_check("governance_coverage_passed", coverage.get("status") == "pass", "governance", f"status={coverage.get('status')}; fail_count={coverage.get('fail_count')}; check_count={coverage.get('check_count')}", "治理覆盖存在缺口时 CURRENT_STATUS 自身不得标记为有效。"),
    ]
    return checks


def audit_check(check_id: str, passed: bool, layer: str, evidence: str, requirement: str) -> dict[str, str]:
    return {
        "check_id": check_id,
        "layer": layer,
        "status": "pass" if passed else "fail",
        "evidence": evidence,
        "requirement": requirement,
    }


def build_summary(status: Mapping[str, Any], checks: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    failed = [row for row in checks if row.get("status") != "pass"]
    source_failed = [row for row in failed if row.get("layer") != "governance"]
    governance_failed = [row for row in failed if row.get("layer") == "governance"]
    samples = as_mapping(status.get("sample_counts"))
    disposition = as_mapping(status.get("exploratory_disposition"))
    settlement_data_boundary = as_mapping(status.get("exploratory_settlement_data_boundary"))
    return {
        "schema_version": "1.0.0",
        "policy_id": "current_status",
        "policy_status": "research_only",
        "generated_at": status.get("source_snapshot_generated_at", ""),
        "current_date": status.get("current_date", ""),
        "decision_as_of": status.get("decision_as_of", ""),
        "current_action": status.get("action", "NO_ACTION"),
        "state_source_consistency_passed": not source_failed,
        "governance_ready": not governance_failed and as_mapping(status.get("governance")).get("ready") is True,
        "status_valid": not failed,
        "check_count": len(checks),
        "pass_count": len(checks) - len(failed),
        "fail_count": len(failed),
        "source_fail_count": len(source_failed),
        "governance_fail_count": len(governance_failed),
        "strong_industry_alpha_validated": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "active_cohort_id": as_mapping(as_mapping(status.get("freeze_layers")).get("fund_flow_evidence_cohort")).get("cohort_id", ""),
        "sample_counts": dict(samples),
        "exploratory_disposition_artifact_present": disposition.get("artifact_present") is True,
        "exploratory_disposition_valid": disposition.get("valid") is True,
        "exploratory_completion_status": str(disposition.get("completion_status", "")),
        "exploratory_observation_count": int_or_zero(samples.get("exploratory_fund_flow_observations")),
        "exploratory_settled_count": int_or_zero(samples.get("exploratory_fund_flow_settled")),
        "exploratory_terminal_blocked_count": int_or_zero(samples.get("exploratory_fund_flow_terminal_blocked")),
        "exploratory_pending_count": int_or_zero(samples.get("exploratory_fund_flow_pending")),
        "exploratory_qualified_settled_count": int_or_zero(samples.get("exploratory_fund_flow_qualified_settled")),
        "exploratory_disposition_error": str(disposition.get("error", "")),
        "exploratory_settlement_data_boundary": dict(settlement_data_boundary),
        "blocking_gates": list(status.get("blocking_gates", [])) if isinstance(status.get("blocking_gates"), list) else [],
        "final_verdict": (
            "状态源与研究治理均可复核；当前结论仍为 research_only / NO_ACTION。"
            if not failed
            else "CURRENT_STATUS 输入或研究治理尚有缺口；失败关闭并保持 research_only / NO_ACTION。"
        ),
    }


def write_outputs(
    status: Mapping[str, Any],
    checks: Sequence[Mapping[str, str]],
    summary: Mapping[str, Any],
    sources: Mapping[str, Any],
) -> None:
    DEBUG.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATUS_PATH, render_status(status, checks, summary))
    atomic_write_json(OUT / "run_summary.json", dict(summary))
    atomic_write_csv(
        OUT / "top_candidates.csv",
        checks,
        fieldnames=["check_id", "layer", "status", "evidence", "requirement"],
    )
    atomic_write_csv(
        DEBUG / "status_checks.csv",
        checks,
        fieldnames=["check_id", "layer", "status", "evidence", "requirement"],
    )
    atomic_write_json(DEBUG / "status_snapshot.json", status_snapshot(status, sources))
    atomic_write_text(OUT / "report.md", render_audit_report(status, checks, summary))


def render_status(status: Mapping[str, Any], checks: Sequence[Mapping[str, str]], summary: Mapping[str, Any]) -> str:
    source_cutoffs = as_mapping(status.get("source_cutoffs"))
    samples = as_mapping(status.get("sample_counts"))
    disposition = as_mapping(status.get("exploratory_disposition"))
    settlement_data_boundary = as_mapping(status.get("exploratory_settlement_data_boundary"))
    governance = as_mapping(status.get("governance"))
    inventory = as_mapping(governance.get("inventory"))
    coverage = as_mapping(governance.get("coverage"))
    account = as_mapping(status.get("account_state"))
    freeze_layers = as_mapping(status.get("freeze_layers"))
    cohort = as_mapping(freeze_layers.get("fund_flow_evidence_cohort"))
    pit_methodology = as_mapping(status.get("pit_methodology"))
    registrations = freeze_layers.get("strong_industry_forward_rules", [])
    governance_label = "pass" if governance.get("ready") is True else "fail-closed"
    validity_label = "valid" if summary.get("status_valid") is True else "fail-closed"

    lines = [
        "# CURRENT_STATUS｜当前项目状态",
        "",
        "> 本页由 `scripts/build_current_status.py` 从当前运行摘要、状态一致性审计、冻结账本和研究治理审计生成。生成成功不等于研究或交易就绪。",
        "",
        "**当前唯一结论：`research_only / NO_ACTION`。强行业 Alpha 未验证；人工辅助交易未就绪；自动交易禁止。**",
        "",
        "## 一眼看清",
        "",
        "| 项目 | 当前值 |",
        "|---|---|",
        f"| 状态生成时间 | `{summary.get('generated_at', '')}` |",
        f"| 当前日期 | `{status.get('current_date', '')}` |",
        f"| 决策 as-of | `{status.get('decision_as_of', '')}` |",
        f"| 当前动作 | `{status.get('action', '')}` |",
        f"| 证据口径 | `{status.get('policy_status', '')}` |",
        f"| 强行业 Alpha | `未验证`（合格前推样本 {samples.get('strong_industry_qualified_forward', 0)}） |",
        f"| PIT估值/行业历史方法门 | `{'通过' if pit_methodology.get('promotion_gate_passed') else '阻断'}`（可晋级估值行 {pit_methodology.get('promotion_eligible_valuation_row_count', 0)}） |",
        f"| 人工辅助交易 | `未就绪` |",
        f"| 自动交易 | `禁止` |",
        f"| 状态源一致性 | `{'pass' if summary.get('state_source_consistency_passed') else 'fail-closed'}` |",
        f"| 研究治理覆盖 | `{governance_label}` |",
        f"| 本页有效性 | `{validity_label}` |",
        "",
        "`V4.70` 的历史框架分数不能覆盖 `V4.71 production_ready=false`、`V5.10 goal_ready=false` 和当前硬门禁；它不是当前结论。",
        "",
        "## 日期与真实数据截止日",
        "",
        f"runner 合同中的 `data_cutoff_date={status.get('run_boundary_data_cutoff_date', '')}` 只是本次请求的决策边界，表示不得读取此日之后的数据；它**不是**各数据源都更新到该日的声明。实际截止日如下。",
        "",
        "| 数据源 | 真实截止日 |",
        "|---|---|",
    ]
    for key, value in source_cutoffs.items():
        lines.append(f"| `{key}` | `{value or '无可用日期'}` |")
    lines.extend([
        f"| `account_state` | `{account.get('as_of_date', '无可用日期')}`（{'门禁通过' if account.get('gate_passed') else '门禁失败'}） |",
        "",
        "空日期表示该源在本轮没有可用于决策的证据；不能用运行日补齐。",
    ])
    if settlement_data_boundary.get("available") is True:
        lines.extend([
            "",
            (
                "探索性资金流终局另行使用结算专用行情：精确入场日 "
                f"`{settlement_data_boundary.get('entry_date', '')}` 与退出日 "
                f"`{settlement_data_boundary.get('exit_date', '')}` 的同一行业交集为 "
                f"{settlement_data_boundary.get('exact_common_industry_count', 0)}，目标 "
                f"{settlement_data_boundary.get('target_common_count', 0)}/"
                f"{settlement_data_boundary.get('target_count', 0)}。这批数据只用于旧观察的终局核验，"
                f"不改写决策 as-of `{status.get('decision_as_of', '')}`，不改变上表主线 "
                f"`industry_history={source_cutoffs.get('industry_history', '')}` 的截止口径，也不计算或补录收益。"
            ),
        ])
    lines.extend([
        "",
        "## PIT估值与行业历史口径",
        "",
        f"- 方法控制审计：`{'pass' if pit_methodology.get('audit_passed') else 'fail-closed'}`；历史晋级门：`{'pass' if pit_methodology.get('promotion_gate_passed') else 'blocked'}`。",
        f"- 官方直接来源估值截止：`{pit_methodology.get('direct_source_cutoff', '无')}`；原表最大日期 `{pit_methodology.get('raw_max_trade_date', '无')}` 中含回收快照 {pit_methodology.get('recovered_snapshot_row_count', 0)} 行，已隔离。",
        f"- 估值可得性：`{pit_methodology.get('valuation_availability_status', 'unknown')}`；可晋级估值行：{pit_methodology.get('promotion_eligible_valuation_row_count', 0)}。",
        f"- 行业历史文件：{pit_methodology.get('industry_history_file_count', 0)}；当前新鲜文件：{pit_methodology.get('industry_history_fresh_file_count', 0)}；长尾缺口：{pit_methodology.get('industry_history_long_tail_gap_file_count', 0)}；普通陈旧：{pit_methodology.get('industry_history_ordinary_stale_file_count', 0)}（{','.join(str(item) for item in pit_methodology.get('industry_history_ordinary_stale_codes', [])) or '无'}）。",
        f"- 观察名称分段：{pit_methodology.get('observed_name_episode_count', pit_methodology.get('identity_episode_count', 0))}；名称或口径发生变化的代码：{pit_methodology.get('name_changed_industry_code_count', 0)}；已确认语义复用代码：{pit_methodology.get('reused_industry_code_count', 0)}。",
        f"- 历史 beta 身份安全：`{'pass' if pit_methodology.get('historical_beta_identity_safe') else 'blocked'}`；已排除指标：{','.join(str(item) for item in pit_methodology.get('excluded_identity_unsafe_features', [])) or '无'}。",
        f"- 独立前推证据复验：`{pit_methodology.get('forward_route_integrity_status', 'blocked')}`；缺口：`{pit_methodology.get('forward_route_integrity_blocker', FORWARD_EVIDENCE_ROUTE_BLOCKER) or '无'}`。",
        f"- 旧回测口径：`{pit_methodology.get('historical_review_set_label', '')}`；真正前推证据最早日期：`{pit_methodology.get('true_forward_earliest_evidence_date', '')}`。",
        "- 当前缺口：" + ("、".join(str(item) for item in pit_methodology.get("blocking_reasons", [])) or "无记录") + "。",
        "",
        "## 版本坐标",
        "",
        "| 类型 | 名称 | 产物版本 | 证据边界 |",
        "|---|---|---|---|",
    ])
    for row in status.get("versions", []) if isinstance(status.get("versions"), list) else []:
        if isinstance(row, Mapping):
            lines.append(f"| {row.get('kind', '')} | `{row.get('name', '')}` | `{row.get('artifact_version', '')}` | {row.get('boundary', '')} |")
    lines.extend([
        "",
        "策略版本、研究审计版本、数据治理版本和前推 cohort 是四类不同对象，禁止相互替代。",
        "",
        "## 两层冻结规则",
        "",
        "### 第一层：强行业规则前推冻结",
        "",
        "`logs/research_experiment_ledger.jsonl` 只预注册冻结日之后的新前推样本，不追认历史结果。",
        "",
        "| 冻结规则 | 证据起点 | 每条规则最低新样本 | 允许动作 | 禁止动作 |",
        "|---|---|---:|---|---|",
    ])
    for row in registrations if isinstance(registrations, list) else []:
        if isinstance(row, Mapping):
            lines.append(
                f"| `{row.get('rule_definition', row.get('frozen_rule', ''))}` | `{row.get('evidence_start_date', '')}` | "
                f"{row.get('required_new_forward_event_count', 0)} | `{row.get('allowed_next_action', '')}` | `{row.get('forbidden_next_action', '')}` |"
            )
    lines.extend([
        "",
        "### 第二层：资金流证据 cohort 冻结",
        "",
        f"- active cohort：`{cohort.get('cohort_id', '')}`",
        f"- manifest：`{cohort.get('manifest_hash', '')}`",
        f"- 复验：`{'pass' if cohort.get('freeze_passed') else 'fail-closed'}`；`{cohort.get('validation_reason', '')}`",
        "- 只有 cohort_id 与 manifest_hash 同时匹配、且在冻结时限内取得的证据才可能进入 active 样本；legacy、错配 hash 和迟到回填一律隔离。",
        "- cohort 冻结只证明证据未漂移，不证明强行业 Alpha。",
        "",
        "## 样本账本",
        "",
        "| 样本口径 | 数量 | 能否用于强行业结论 |",
        "|---|---:|---|",
        f"| 强行业冻结规则合格前推样本 | {samples.get('strong_industry_qualified_forward', 0)} | 否；每条冻结规则最低要求 {samples.get('strong_industry_required_per_rule', 0)} |",
        f"| active fund-flow 观察样本 | {samples.get('active_fund_flow_observations', 0)} | 仅进入证据链，不自动合格 |",
        f"| active fund-flow 完整性合格样本 | {samples.get('active_fund_flow_integrity_eligible', 0)} | 否；仍须目标资格、结算和晋级 |",
        f"| active fund-flow 已结算且目标合格样本 | {samples.get('active_fund_flow_settled_qualified', 0)} | 只能进入后续批次评价 |",
        f"| 探索性 fund-flow 样本总数 | {samples.get('exploratory_fund_flow_observations', 0)} | 否，永久与合格前推样本分栏 |",
        f"| 探索性 fund-flow 已结算 | {samples.get('exploratory_fund_flow_settled', 0)} | 否，不计入目标样本 |",
        f"| 探索性 fund-flow terminal blocked | {samples.get('exploratory_fund_flow_terminal_blocked', 0)} | 否，永久排除且不得补价转正 |",
        f"| 探索性 fund-flow pending | {samples.get('exploratory_fund_flow_pending', 0)} | 否，只等待合法终局处置 |",
        f"| 探索性 fund-flow qualified settled | {samples.get('exploratory_fund_flow_qualified_settled', 0)} | 必须保持 0 |",
        f"| 迟到回填排除观察 | {samples.get('late_backfill_excluded_observation_count', 0)} | 否，不得事后转正 |",
        "",
        (
            "四条探索观察的独立终局处置已经通过摘要、逐行记录和 active pair 复核："
            f"settled {samples.get('exploratory_fund_flow_settled', 0)} / terminal blocked {samples.get('exploratory_fund_flow_terminal_blocked', 0)} / "
            f"pending {samples.get('exploratory_fund_flow_pending', 0)} / qualified settled {samples.get('exploratory_fund_flow_qualified_settled', 0)}。"
            "终局不含收益，不证明或否定强行业 Alpha。"
            if disposition.get("valid") is True
            else "正式探索处置尚未形成完整有效的摘要与逐行记录；探索样本继续按 pending 分栏，不能提前写成已结算或已终局排除。"
        ),
        "",
        "V4.71 与 V4.85 的旧计划观察不在上述“强行业合格前推样本”内；历史候选、探索观察和真实前推证据不能混算。",
        "",
        "## 当前硬门禁",
        "",
        "| 门禁 | 状态 | 当前证据 |",
        "|---|---|---|",
    ])
    for row in status.get("hard_gates", []) if isinstance(status.get("hard_gates"), list) else []:
        if isinstance(row, Mapping):
            lines.append(f"| {row.get('label', '')} | `{'通过' if row.get('status') == 'pass' else '阻断'}` | {escape_table(str(row.get('evidence', '')))} |")
    lines.extend([
        "",
        "“六角色确定性否决链”是当前统一外部术语；任何一个角色否决，都不能输出买卖建议。",
        "",
        "## 下一步允许做什么",
        "",
    ])
    lines.extend(f"- {item}" for item in status.get("next_allowed_actions", []) if str(item))
    lines.extend([
        "",
        "## 明确禁止",
        "",
    ])
    lines.extend(f"- {item}" for item in status.get("forbidden_actions", []) if str(item))
    lines.extend([
        "",
        "## 恢复人工辅助资格的条件",
        "",
    ])
    lines.extend(f"- {item}" for item in status.get("recovery_conditions", []) if str(item))
    lines.extend([
        "",
        "这些条件必须在同一决策快照中同时成立。即便人工辅助资格恢复，自动交易仍然禁止，除非另有经过审查的独立授权与执行治理。",
        "",
        "## 研究治理状态",
        "",
        f"- 版本库存：`{inventory.get('status', 'missing')}`；{inventory.get('record_count', 0)} / {inventory.get('expected_record_count', 0)} 条；治理失败 {inventory.get('governance_fail_count', 0)} 条。",
        f"- 覆盖审计：`{coverage.get('status', 'missing')}`；fail_count={coverage.get('fail_count')}；check_count={coverage.get('check_count')}。",
        f"- 工程完成度快照：{as_mapping(status.get('completion')).get('implementation', '')}；就绪门禁：{as_mapping(status.get('completion')).get('readiness', '')}；行为测试：{as_mapping(status.get('completion')).get('behavior', '')}。工程通过不能代替研究证据通过。",
        "",
        "治理覆盖缺失或失败时，本页只能作为失败关闭的状态快照，不能作为发布、实盘或研究完成的验收凭证。",
        "",
        "## 可复核入口",
        "",
        "- [项目全量审查报告](notes/项目全量审查报告_2026-07-17.md)",
        "- [可复现性与测试报告](notes/reproducibility_and_test_report.md)",
        "- 当前 runner 摘要：`outputs/etf_assisted_trading_current/run_summary.json`",
        "- 当前状态一致性审计：`outputs/audit/current_state_consistency/run_summary.json`",
        "- 当前状态生成审计：`outputs/audit/current_status/run_summary.json`",
        "- 研究治理覆盖审计：`outputs/audit/research_governance_coverage/run_summary.json`",
        "",
    ])
    failed = [row for row in checks if row.get("status") != "pass"]
    if failed:
        lines.extend(["## 本页失败关闭项", ""])
        lines.extend(f"- `{row.get('check_id', '')}`：{row.get('requirement', '')}" for row in failed)
        lines.append("")
    return "\n".join(lines)


def render_audit_report(status: Mapping[str, Any], checks: Sequence[Mapping[str, str]], summary: Mapping[str, Any]) -> str:
    failed = [row for row in checks if row.get("status") != "pass"]
    lines = [
        "# CURRENT_STATUS 生成审计",
        "",
        str(summary.get("final_verdict", "")),
        "",
        f"- 当前日期：`{status.get('current_date', '')}`",
        f"- 决策 as-of：`{status.get('decision_as_of', '')}`",
        f"- 当前动作：`{status.get('action', '')}`",
        f"- 检查：{summary.get('pass_count', 0)} / {summary.get('check_count', 0)}",
        f"- 状态源一致：`{str(summary.get('state_source_consistency_passed', False)).lower()}`",
        f"- 治理就绪：`{str(summary.get('governance_ready', False)).lower()}`",
        f"- 本页有效：`{str(summary.get('status_valid', False)).lower()}`",
        "",
        "## 失败项",
        "",
    ]
    lines.extend(
        [f"- `{row.get('check_id', '')}` [{row.get('layer', '')}]：{row.get('evidence', '')}；要求：{row.get('requirement', '')}" for row in failed]
        or ["- 无。"]
    )
    lines.extend([
        "",
        "## 边界",
        "",
        "本审计只证明 CURRENT_STATUS 的来源、分类和治理覆盖可复核；它不证明策略有效，也不解除任何交易门禁。",
        "",
    ])
    return "\n".join(lines)


def status_snapshot(status: Mapping[str, Any], sources: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": dict(status),
        "source_generated_at": {
            key: as_mapping(sources.get(key)).get("generated_at", "")
            for key in ("state_audit", "current", "completion", "v470", "v471", "v485", "v507", "v508", "v510", "v531", "v535", "experiment_audit", "governance_coverage", "exploratory_disposition")
        },
        "source_presence": dict(as_mapping(sources.get("source_presence"))),
        "active_validation_error": str(sources.get("active_validation_error", "")),
        "fund_flow_ledger_error": str(sources.get("fund_flow_ledger_error", "")),
        "exploratory_disposition_error": str(sources.get("exploratory_disposition_error", "")),
        "exploratory_disposition_artifact_presence": dict(
            as_mapping(sources.get("exploratory_disposition_artifact_presence"))
        ),
    }


def next_allowed_actions(decision_as_of: str, *, governance_ready: bool) -> list[str]:
    actions = [
        f"把账户快照和必需数据源更新到真实可得日期，再以 `{decision_as_of or '<YYYY-MM-DD>'}` 重新运行当前 runner 与状态一致性审计；更新日期不能替代实际源截止日。",
        "先把 V5.07 的可变 CSV 证据改为可重放的 append-only 哈希账本，并在消费端现场复验；在此之前新增样本也不能解除前推门。",
        "只在 V5.04 已冻结规则自然触发后，按 `append_new_forward_samples_only` 追加新前推样本；不得从历史窗口回填。",
        "只在计划退出日到达、入场价与基准价均按时冻结且 PIT 来源完整时结算收益；不满足则保持 pending 或永久排除。",
    ]
    actions.append(
        "研究治理覆盖已通过；新增或改动研究版本时必须同步维护 task brief、登记或 post-hoc、变更记录和标准输出，并重跑覆盖审计。"
        if governance_ready
        else "补齐版本 task brief、预注册或明确 post-hoc、变更记录和标准输出，再重跑研究治理覆盖审计。"
    )
    actions.append("继续观察和审计；在所有硬门禁清零前维持 `NO_ACTION`。")
    return actions


def forbidden_actions() -> list[str]:
    return [
        "禁止生成或执行 ETF 买卖指令，禁止把 `WATCH`、候选清单或框架分数写成买入建议。",
        "禁止宣称强行业 Alpha 已验证，禁止把历史候选、探索样本、迟到回填或 cohort 冻结通过混入合格前推样本。",
        "禁止根据已经看到的历史结果修改 V5.04 冻结阈值；新规则必须另立实验并重新登记。",
        "禁止把工程测试通过、数据门禁局部通过或 `V4.70=100` 写成人工辅助交易已就绪。",
        "禁止自动交易；当前 `auto_execution_allowed=false` 是硬边界，不随单个研究门禁通过而解除。",
        "禁止在状态一致性或研究治理覆盖失败时把任一旧摘要当作当前权威状态。",
    ]


def recovery_conditions() -> list[str]:
    return [
        "当前状态一致性审计和研究治理覆盖审计同时通过，所有当前摘要绑定同一 active cohort pair。",
        "必需数据源在决策 as-of 下通过真实时点、覆盖率和 PIT 检查；不得用运行日冒充源截止日。",
        "V4.71 或其受治理的后续审计通过择时稳健性，且择时前推门禁通过。",
        "V5.07 前推证据由可重放的 append-only 哈希账本承载，并由消费端现场复验账本头、顺序、规则冻结时间和零历史回填。",
        "强行业规则取得预登记要求的新前推样本并通过 V5.07/V5.10；当前最低门槛为每条冻结规则 12 个新事件。",
        "账户快照与决策 as-of 一致，现有组合风险及建议后组合风险均通过。",
        "六角色确定性否决链在同一决策快照内全部通过，runner 不再产生任何 blocking gate。",
    ]


def latest_source_generated_at(sources: Mapping[str, Any], current_date: date) -> str:
    """Return a deterministic timestamp owned by the latest governed input.

    CURRENT_STATUS is a derived view.  Its timestamp must therefore come from
    its inputs rather than from wall-clock time; otherwise identical inputs
    would produce different audit artifacts.
    """

    candidates: list[tuple[datetime, str]] = []
    for key in (
        "state_audit",
        "current",
        "completion",
        "v470",
        "v471",
        "v485",
        "v507",
        "v508",
        "v510",
        "v531",
        "v535",
        "experiment_audit",
        "governance_coverage",
        "exploratory_disposition",
    ):
        text = str(as_mapping(sources.get(key)).get("generated_at", ""))
        parsed = parse_timestamp(text)
        if parsed is None:
            continue
        comparable = parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
        candidates.append((comparable, text))
    if not candidates:
        return f"{current_date.isoformat()}T00:00:00"
    return max(candidates, key=lambda item: (item[0], item[1]))[1]


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return dict(value) if isinstance(value, Mapping) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, Mapping):
            rows.append(dict(value))
    return rows


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def as_rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def int_or_zero(value: Any) -> int:
    parsed = int_or_none(value)
    return parsed if parsed is not None else 0


def int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def is_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1", "yes"}


def timestamp_not_older(newer: Any, older: Any) -> bool:
    newer_value = parse_timestamp(newer)
    older_value = parse_timestamp(older)
    if newer_value is None or older_value is None:
        return False
    if newer_value.tzinfo is not None:
        newer_value = newer_value.replace(tzinfo=None)
    if older_value.tzinfo is not None:
        older_value = older_value.replace(tzinfo=None)
    return newer_value >= older_value


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def short_hash(value: str) -> str:
    return f"{value[:12]}…" if len(value) > 12 else (value or "missing")


def escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def self_check_sources() -> dict[str, Any]:
    active_hash = "a" * 64
    source_manifest = [
        {"source": "industry_history", "latest_date": "2026-07-17", "status": "pass"},
        {"source": "valuation_history", "latest_date": "2025-12-31", "status": "pass"},
        {"source": "pit_valuation_methodology", "latest_date": "2025-12-31", "status": "fail"},
    ]
    cutoffs = {row["source"]: row["latest_date"] for row in source_manifest}
    sources: dict[str, Any] = {
        "state_audit": {
            "generated_at": "2026-07-18T10:00:02",
            "state_consistent": True,
            "fail_count": 0,
            "current_as_of_date": "2026-07-18",
            "current_action": "NO_ACTION",
            "active_cohort_id": "active-v1",
            "active_cohort_manifest_hash": active_hash,
            "active_cohort_validated": True,
        },
        "state_sources": {"validated_active": {"cohort_id": "active-v1"}},
        "current": {
            "version": "1.0.0",
            "policy_id": "etf_assisted_trading_current",
            "policy_status": "research_only",
            "generated_at": "2026-07-18T10:00:01",
            "as_of_date": "2026-07-18",
            "action": "NO_ACTION",
            "blocking_gates": ["pit_universe_methodology", "timing_robustness", "industry_selection", "account_state", "portfolio_risk", "goal_evidence", "agent_veto_chain", "projected_portfolio_risk"],
            "data_gate_passed": True,
            "pit_universe_methodology_gate_passed": False,
            "promotion_eligible_valuation_row_count": 0,
            "timing_gate_passed": False,
            "industry_selection_gate_passed": False,
            "etf_pit_gate_passed": True,
            "account_state_gate_passed": False,
            "portfolio_risk_gate_passed": False,
            "projected_portfolio_risk_gate_passed": False,
            "goal_evidence_gate_passed": False,
            "forward_timing_gate_passed": False,
            "forward_industry_gate_passed": False,
            "manual_decision_support_ready": False,
            "production_ready": False,
            "auto_execution_allowed": False,
        },
        "recommendation": {
            "policy_id": "etf_assisted_trading_current",
            "action": "NO_ACTION",
            "data_cutoff_date": "2026-07-18",
            "data_cutoff_by_source": cutoffs,
            "auto_execution_allowed": False,
        },
        "completion": {
            "generated_at": "2026-07-18T09:00:00",
            "current_action": "NO_ACTION",
            "implementation_pass_count": 28,
            "implementation_check_count": 28,
            "readiness_pass_count": 6,
            "readiness_check_count": 12,
            "behavior_test_pass_count": 23,
            "behavior_test_count": 23,
            "manual_decision_support_ready": False,
            "production_ready": False,
            "auto_execution_allowed": False,
        },
        "v470": {"version": "4.70.0"},
        "v471": {"version": "4.71.0"},
        "v485": {"version": "4.85.0"},
        "v507": {"version": "5.07.0", "generated_at": "2026-07-18T09:50:00", "best_forward_event_count": 0},
        "v508": {"version": "5.08.0", "generated_at": "2026-07-18T09:51:00", "appended_signal_count": 0},
        "v510": {"version": "5.10.0", "generated_at": "2026-07-18T10:00:00", "goal_ready": False, "can_claim_strong_rebound_industries": False, "true_forward_route_ready": False, "pit_universe_methodology_gate_passed": False, "promotion_eligible_valuation_row_count": 0},
        "pit_methodology": {
            "generated_at": "2026-07-18T09:59:00",
            "policy_status": "research_only",
            "audit_passed": True,
            "methodology_remediation_complete": True,
            "legacy_oos_label_corrected": True,
            "historical_review_set_label": "historical_review_used_in_iteration",
            "valuation_required_fields": ["trade_date", "published_at", "available_date", "fetched_at", "source_version", "revision_status"],
            "production_ready": False,
            "auto_execution_allowed": False,
            "promotion_gate_passed": False,
            "historical_valuation_pit_gate_passed": False,
            "historical_classification_gate_passed": False,
            "promotion_eligible_valuation_row_count": 0,
            "valuation_direct_source_max_trade_date": "2025-12-31",
            "valuation_raw_max_trade_date": "2026-06-12",
            "recovered_snapshot_row_count": 131,
            "valuation_availability_status": "unavailable_for_promotion",
            "classification_history_status": "unavailable",
            "industry_history_file_count": 131,
            "industry_history_fresh_file_count": 123,
            "industry_history_long_tail_gap_file_count": 7,
            "industry_history_ordinary_stale_file_count": 1,
            "industry_history_ordinary_stale_codes": ["801156"],
            "identity_episode_count": 166,
            "observed_name_episode_count": 166,
            "name_changed_industry_code_count": 35,
            "reused_industry_code_count": 2,
            "historical_beta_identity_safe": False,
            "excluded_identity_unsafe_features": ["beta_low_pb_score"],
            "historical_review_set_label": "historical_review_used_in_iteration",
            "true_forward_earliest_evidence_date": "2026-07-12",
            "blocking_reasons": ["valuation_available_date_unproved"],
        },
        "v531": {"version": "5.31.2"},
        "v535": {"version": "5.35.1"},
        "experiment_audit": {"integrity_passed": True, "historical_results_preregistered": False},
        "active_pointer": {"cohort_id": "active-v1"},
        "validated_active": {"cohort_id": "active-v1", "manifest_hash": active_hash, "freeze_passed": True, "validation_reason": "verified"},
        "account": {"configured": True, "as_of_date": "2026-07-13", "positions": []},
        "inventory": {"schema_version": "1", "inventory_as_of": "2026-07-18", "summary": {"record_count": 65, "expected_record_count": 65, "governance_status_counts": {"pass": 65}}},
        "governance_coverage": {"generated_at": "2026-07-18T10:00:03", "check_count": 65, "fail_count": 0},
        "source_manifest": source_manifest,
        "gate_results": [],
        "experiment_records": [
            {
                "experiment_id": f"rule-{index}",
                "registration_status": "preregistered_forward_only",
                "evidence_start_date": "2026-07-12",
                "frozen_rule": f"quality_score_ge{index + 1}",
                "rule_definition": f"rule {index}",
                "promotion_criteria": [{"metric": "new_forward_event_count", "required": "12"}],
                "allowed_next_action": "append_new_forward_samples_only",
                "forbidden_next_action": "do_not_change_thresholds_from_historical_results",
            }
            for index in (1, 2)
        ],
        "fund_flow_records": [
            {"observation_id": "legacy-1", "cohort_id": "legacy", "cohort_manifest_hash": "legacy", "sample_scope": "exploratory_fund_flow_only"},
        ],
        "entry_freeze_records": [],
        "benchmark_freeze_records": [],
        "fund_flow_ledger_verified": True,
        "fund_flow_ledger_error": "",
        "active_validation_error": "",
    }
    sources["source_presence"] = {key: True for key in (*OPERATING_SOURCE_KEYS, "source_manifest", "gate_results", "experiment_records", "fund_flow_records", "governance_coverage")}
    return sources


def self_check() -> None:
    sources = self_check_sources()
    status = build_status(sources, current_date=date(2026, 7, 18))
    checks = build_checks(status, sources)
    assert checks and all(row["status"] == "pass" for row in checks), [row for row in checks if row["status"] != "pass"]
    assert status["source_cutoffs"]["valuation_history"] == "2025-12-31"
    assert status["pit_methodology"]["promotion_eligible_valuation_row_count"] == 0
    assert status["run_boundary_data_cutoff_date"] == "2026-07-18"
    assert status["sample_counts"]["exploratory_fund_flow_observations"] == 1
    rendered = render_status(status, checks, build_summary(status, checks))
    assert "只是本次请求的决策边界" in rendered
    assert "强行业 Alpha 未验证" in rendered

    stale = copy.deepcopy(sources)
    stale["current"]["generated_at"] = "2026-07-18T09:59:59"
    stale_status = build_status(stale, current_date=date(2026, 7, 18))
    stale_failed = {row["check_id"] for row in build_checks(stale_status, stale) if row["status"] == "fail"}
    assert "current_after_goal_audit" in stale_failed

    missing_coverage = copy.deepcopy(sources)
    missing_coverage["governance_coverage"] = {}
    missing_coverage["source_presence"]["governance_coverage"] = False
    pending_status = build_status(missing_coverage, current_date=date(2026, 7, 18))
    pending_checks = build_checks(pending_status, missing_coverage)
    assert any(row["check_id"] == "governance_coverage_present" and row["status"] == "fail" for row in pending_checks)
    assert not build_summary(pending_status, pending_checks)["status_valid"]
    print("self_check=pass")


if __name__ == "__main__":
    main()
