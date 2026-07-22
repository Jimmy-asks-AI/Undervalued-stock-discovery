from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_fund_flow_exploratory_settlement_readiness as audit
import run_fund_flow_exploratory_settlement as runner
from fund_flow_exploratory_price_contract import price_cache_snapshot
from fund_flow_forward_evidence import append_events, verify_ledger_checkpoint
from research_integrity import file_sha256


ACTIVE_HASH = "9" * 64


def make_paths(tmp_path: Path) -> runner.SettlementPaths:
    paths = runner.SettlementPaths(tmp_path)
    paths.scripts.mkdir(parents=True)
    return paths


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def active_pointer() -> dict[str, Any]:
    return {
        "cohort_id": "active-fixture",
        "manifest_hash": ACTIVE_HASH,
        "manifest_path": "logs/v5_31_fund_flow_evidence_freeze/active-fixture/manifest.csv",
        "freeze_passed": True,
        "verification_required": False,
        "created_at_utc": "2026-07-18T12:00:00Z",
        "verified_at_utc": "2026-07-18T12:01:00Z",
        "operator": "fixture",
        "reason": "offline test",
    }


def install_active_pointer(paths: runner.SettlementPaths) -> None:
    pointer = active_pointer()
    write_json(paths.active_pointer, pointer)
    manifest = paths.root / pointer["manifest_path"]
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("artifact_id,artifact_type,fingerprint\n", encoding="utf-8")


def price_codes(count: int = 100) -> list[str]:
    target_codes = [code for code, _name in audit.EXPECTED.values()]
    extras = [f"{810000 + index:06d}" for index in range(count - len(target_codes))]
    return sorted([*target_codes, *extras])


def install_prices(paths: runner.SettlementPaths, *, count: int = 100, exact_exit: bool = True) -> None:
    paths.price_dir.mkdir(parents=True, exist_ok=True)
    exit_date = audit.EXIT_DATE if exact_exit else "2026-07-20"
    for code in price_codes(count):
        (paths.price_dir / f"{code}.csv").write_text(
            f"industry_code,trade_date,close\n{code},{audit.ENTRY_DATE},100\n{code},{exit_date},101\n",
            encoding="utf-8",
        )
    (paths.price_dir / "801156.csv").write_text(
        "industry_code,trade_date,close\n801156,2026-06-10,100\n"
        "801156,2026-06-12,101\n",
        encoding="utf-8",
    )


def install_authoritative_sources(paths: runner.SettlementPaths) -> None:
    ledger = paths.root / runner.disposition_contract.EVENT_LEDGER_RELATIVE
    observations: list[dict[str, Any]] = []
    for observation_id, (code, name) in audit.EXPECTED.items():
        row: dict[str, Any] = {
            "event_type": "observation",
            "event_id": f"{observation_id}:observation",
            "observation_id": observation_id,
            "industry_code": code,
            "industry_name": name,
            "signal_date": audit.SIGNAL_DATE,
            "planned_entry_date": audit.ENTRY_DATE,
            "planned_exit_date": audit.EXIT_DATE,
            "sample_scope": "exploratory_fund_flow_only",
            "cohort_id": "legacy_exploratory_20260622",
            "cohort_manifest_hash": "UNVERIFIED_LEGACY_COHORT",
            "settlement_status": "not_due",
            "selection_score": "1",
            "qualified_for_goal": False,
            "integrity_eligible": False,
            "promotion_eligible": False,
            "source_fingerprint_status": "unverified_legacy",
            "calendar_fingerprint": "UNVERIFIED_LEGACY_CALENDAR",
            "code_version": "UNVERIFIED_LEGACY_CODE",
        }
        row.update({field: "" for field in audit.RETURN_FIELDS})
        observations.append(row)
    append_events(ledger, observations)

    for relative, count in {
        "logs/v5_33_fund_flow_entry_price_freeze.jsonl": 4,
        "logs/v5_34_fund_flow_benchmark_entry_freeze.jsonl": 4,
        "logs/v5_31_fund_flow_evidence_freeze_history.jsonl": 1,
    }.items():
        append_events(
            paths.root / relative,
            [
                {"event_type": "fixture", "event_id": f"{Path(relative).stem}:{index}"}
                for index in range(count)
            ],
        )

    static_files = {
        "logs/v5_25_fund_flow_forward_ledger.csv": "fixture\n",
        "outputs/audit/fund_flow_forward_ledger_integrity_v5_30/run_summary.json": "{}\n",
        "outputs/audit/current_state_consistency/run_summary.json": "{}\n",
        "outputs/audit/fund_flow_exploratory_settlement_price_refresh_2026_07_21/run_summary.json": "{}\n",
        "configs/fund_flow_forward_chain_policy.json": "{}\n",
        "configs/fund_flow_forward_ledger_schema.json": "{}\n",
        "scripts/settle_v5_27_fund_flow_forward_samples.py": "# fixture\n",
        "scripts/audit_fund_flow_exploratory_settlement_readiness.py": "# fixture\n",
        "scripts/refresh_fund_flow_exploratory_settlement_prices.py": "# fixture refresh\n",
        "scripts/run_industry_index_research_validation.py": "# fixture source\n",
        "scripts/fund_flow_exploratory_price_contract.py": "# fixture price contract\n",
        "logs/v5_25_fund_flow_forward_sources/calendars/"
        "f348dd4c8863a5f2a5ff543a427c36198dbe3ca00f01a268f736490b2989d975.csv": (
            "trade_date\n2026-06-23\n2026-07-21\n"
        ),
    }
    for relative, text in static_files.items():
        path = paths.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    paths.baseline_price_dir.mkdir(parents=True, exist_ok=True)
    for source_path in paths.price_dir.glob("*.csv"):
        (paths.baseline_price_dir / source_path.name).write_bytes(
            source_path.read_bytes()
        )
    snapshot = price_cache_snapshot(paths.price_dir)
    aggregate = snapshot["aggregate_sha256"]
    count = snapshot["csv_file_count"]
    quarantine_sha256 = file_sha256(paths.price_dir / "801156.csv")
    baseline_snapshot = price_cache_snapshot(paths.baseline_price_dir)
    missing_settlement_snapshot = {
        "directory_exists": False,
        "csv_file_count": 0,
        "aggregate_sha256": "0" * 64,
    }
    producer_relatives = (
        "scripts/refresh_fund_flow_exploratory_settlement_prices.py",
        "scripts/run_industry_index_research_validation.py",
        "scripts/fund_flow_exploratory_price_contract.py",
    )
    write_json(
        paths.root
        / "outputs/audit/fund_flow_exploratory_settlement_price_refresh_2026_07_21/run_summary.json",
        {
            "audit_mode": "fund_flow_exploratory_settlement_price_only_refresh",
            "cache_scope": "dedicated_exploratory_settlement_only",
            "mainline_price_cache_write_invoked": False,
            "cache_bootstrap": {
                "checked": True,
                "action": "created_from_mainline",
                "baseline_path": "data_catalog/cache/industry_index/history/second",
                "settlement_path": runner.disposition_contract.PRICE_DIRECTORY_RELATIVE,
                "baseline_before": baseline_snapshot,
                "baseline_after": baseline_snapshot,
                "baseline_after_refresh": baseline_snapshot,
                "baseline_unchanged": True,
                "baseline_unchanged_through_refresh": True,
                "settlement_before": missing_settlement_snapshot,
                "settlement_after_bootstrap": baseline_snapshot,
                "settlement_unchanged_during_bootstrap": False,
                "settlement_copied_from_baseline": True,
                "baseline_quarantined_file_sha256": {"801156": quarantine_sha256},
                "settlement_quarantined_file_sha256": {"801156": quarantine_sha256},
                "quarantined_files_match_baseline": True,
                "mainline_write_invoked": False,
            },
            "producer_attestations": [
                {
                    "path": relative,
                    "bytes": (paths.root / relative).stat().st_size,
                    "sha256": file_sha256(paths.root / relative),
                }
                for relative in producer_relatives
            ],
            "completion_status": "committed",
            "generated_at": "2026-07-21T15:10:00+08:00",
            "time_gate_passed": True,
            "entry_date": audit.ENTRY_DATE,
            "exit_date": audit.EXIT_DATE,
            "target_industry_codes": [code for code, _name in audit.EXPECTED.values()],
            "price_values_retained_in_audit": False,
            "candidate_generation_invoked": False,
            "ledger_write_invoked": False,
            "account_or_trade_write_invoked": False,
            "official_cache_write_attempted": True,
            "official_cache_touched": True,
            "official_cache_restored": False,
            "authoritative_before": baseline_snapshot,
            "authoritative_after": snapshot,
            "fetch": {
                "attempted": True,
                "expected_industry_count": count,
                "succeeded_industry_count": count - 1,
                "failed_industry_count": 0,
                "failed_industry_codes": [],
                "failure_phase": "",
                "failure_type": "",
                "quarantined_industry_count": 1,
                "quarantined_industry_codes": ["801156"],
                "quarantine_reason": "provider_history_incompatible_with_append_only_cache",
                "source_accounted_industry_count": count,
                "quarantine_attestations": [{
                    "industry_code": "801156",
                    "source_sha256_before": quarantine_sha256,
                    "source_sha256_after_copy": quarantine_sha256,
                    "staged_sha256": quarantine_sha256,
                    "committed_sha256": quarantine_sha256,
                    "source_unchanged_during_staging": True,
                    "staged_matches_source": True,
                    "committed_matches_source": True,
                }],
                "quarantine_attestation_complete": True,
            },
            "coverage": {
                "source_file_count": count,
                "expected_source_file_count": count,
                "invalid_file_count": 0,
                "quarantined_required_date_codes": [],
                "quarantine_exact_date_exclusion_passed": True,
                "entry_industry_count": count - 1,
                "exit_industry_count": count - 1,
                "entry_exit_common_count": count - 1,
                "target_entry_count": 4,
                "target_exit_count": 4,
                "target_common_count": 4,
                "exact_coverage_ready": True,
                "price_values_retained": False,
            },
            "history_continuity": {
                "checked": True,
                "history_continuity_ready": True,
                "historical_rows_unchanged": True,
                "append_only_contract_passed": True,
                "validation_inputs_unchanged": True,
                "failed_industry_count": 0,
                "historical_rows_changed": 0,
                "verified_industry_count": count,
                "price_values_retained": False,
                "existing_universe_aggregate_sha256": "8" * 64,
                "staged_universe_aggregate_sha256": aggregate,
            },
            "commit": {
                "succeeded": True,
                "replaced_file_count": count,
                "staged_and_committed_hashes_match": True,
                "staged_universe_attestation": {
                    "expected_file_count": count,
                    "observed_file_count": count,
                    "missing_industry_codes": [],
                    "aggregate_sha256": aggregate,
                },
                "committed_universe_attestation": {
                    "expected_file_count": count,
                    "observed_file_count": count,
                    "missing_industry_codes": [],
                    "aggregate_sha256": aggregate,
                },
            },
        },
    )


def install_command_scripts(paths: runner.SettlementPaths, overlay: Path) -> None:
    plan = runner.build_command_plan(paths, overlay)
    for spec in plan:
        spec.script.parent.mkdir(parents=True, exist_ok=True)
        if not spec.script.exists():
            spec.script.write_text(f"# fixture {spec.step_id}\n", encoding="utf-8")


def disposition_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reasons = "|".join(
        [
            "legacy_source_unverified",
            "candidate_entry_freeze_late_backfill_excluded",
            "benchmark_entry_freeze_late_backfill_excluded",
            "benchmark_universe_below_100",
            "not_qualified_for_goal",
            "integrity_ineligible",
            "promotion_ineligible",
        ]
    )
    for observation_id, (code, name) in audit.EXPECTED.items():
        rows.append(
            {
                "record_type": "exploratory_settlement_disposition",
                "observation_id": observation_id,
                "industry_code": code,
                "industry_name": name,
                "signal_date": audit.SIGNAL_DATE,
                "planned_entry_date": audit.ENTRY_DATE,
                "planned_exit_date": audit.EXIT_DATE,
                "ledger_settlement_status": "not_due",
                "selection_score": "1",
                "sample_scope": "exploratory_fund_flow_only",
                "qualified_for_goal": "False",
                "integrity_eligible": "False",
                "promotion_eligible": "False",
                "source_cohort_id": "legacy_exploratory_20260622",
                "source_cohort_manifest_hash": "UNVERIFIED_LEGACY_COHORT",
                "source_fingerprint_status": "unverified_legacy",
                "calendar_fingerprint": "UNVERIFIED_LEGACY_CALENDAR",
                "code_version": "UNVERIFIED_LEGACY_CODE",
                "candidate_entry_freeze_status": "late_backfill_excluded",
                "benchmark_entry_freeze_status": "late_backfill_excluded",
                "benchmark_universe_count": "0",
                "record_contract_passed": "True",
                "disposition": "blocked",
                "disposition_status": "blocked_terminal_late_freeze_excluded",
                "reason_codes": reasons,
                "actual_entry_date": "",
                "actual_exit_date": "",
                "realized_return": "",
                "benchmark_return": "",
                "realized_relative_return": "",
                "future_return_rank_pct": "",
                "future_top_quintile": "",
            }
        )
    return rows


def write_formal_fixture(paths: runner.SettlementPaths, *, industry_count: int = 100) -> None:
    out = paths.final_out
    summary = {
        "schema_version": "fund-flow-exploratory-settlement-readiness-v1",
        "generated_at": "2026-07-21T15:30:00+08:00",
        "observed_at": "2026-07-21T15:30:00+08:00",
        "required_start_at": "2026-07-21T15:00:00+08:00",
        "audit_mode": "formal_disposition",
        "completion_status": "complete_terminal_exclusions",
        "policy_status": "research_only",
        "current_action": "NO_ACTION",
        "record_count": 4,
        "records_found": 4,
        "pending_count": 0,
        "blocked_count": 4,
        "settled_count": 0,
        "qualified_settled_count": 0,
        "exploratory_settled_count": 0,
        "settlement_disposition_complete": True,
        "record_contract_passed": True,
        "time_gate_passed": True,
        "calendar_dates_valid": True,
        "state_gate_passed": True,
        "active_cohort_gate_passed": True,
        "v5_30_summary_gate_passed": True,
        "current_state_consistency_gate_passed": True,
        "price_source_gate_passed": True,
        "exit_data_read": True,
        "all_return_fields_empty": True,
        "return_values_read_or_written": False,
        "authoritative_ledgers_mutated": False,
        "active_cohort_id": "active-fixture",
        "active_cohort_manifest_hash": ACTIVE_HASH,
        "active_cohort_validated": True,
        "promotion_ready": False,
        "can_claim_strong_rebound_industries": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "price_coverage": {
            "checked": True,
            "source_file_count": industry_count + 1,
            "valid_source_file_count": industry_count + 1,
            "invalid_file_count": 0,
            "invalid_files": [],
            "quarantined_required_date_codes": [],
            "quarantine_exact_date_exclusion_passed": True,
            "entry_industry_count": industry_count,
            "exit_industry_count": industry_count,
            "entry_exit_common_count": industry_count,
            "candidate_entry_count": 4,
            "candidate_exit_count": 4,
            "candidate_common_count": 4,
            "overall_max_date": audit.EXIT_DATE,
            "minimum_benchmark_industries": audit.MIN_BENCHMARK_INDUSTRIES,
            "price_values_retained": False,
            "exact_coverage_ready": True,
        },
    }
    rows = disposition_rows()
    refresh = json.loads(
        (
            paths.root
            / "outputs/audit/fund_flow_exploratory_settlement_price_refresh_2026_07_21/run_summary.json"
        ).read_text(encoding="utf-8")
    )
    live_coverage = audit.scan_exact_date_coverage(paths.price_dir)
    gate = audit.assess_price_source_gate(
        refresh,
        price_cache_snapshot(paths.price_dir),
        live_coverage,
        price_cache_snapshot(paths.baseline_price_dir),
        audit.quarantined_file_hashes(paths.baseline_price_dir),
        project_root=paths.root,
        settlement_price_dir=paths.price_dir,
        baseline_price_dir=(
            paths.root / "data_catalog/cache/industry_index/history/second"
        ),
        producer_paths=(
            paths.root / "scripts/refresh_fund_flow_exploratory_settlement_prices.py",
            paths.root / "scripts/run_industry_index_research_validation.py",
            paths.root / "scripts/fund_flow_exploratory_price_contract.py",
        ),
    )
    summary["price_source_gate_checks"] = gate["checks"]
    summary["price_source_gate_reason_codes"] = gate["reason_codes"]
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(
        "# 四条探索性资金流记录终局处置\n\nresearch_only / NO_ACTION\n\n"
        "completion_status=complete_terminal_exclusions\n"
        "settled `0` / blocked `4` / pending `0`\nqualified_settled_count=0\n",
        encoding="utf-8",
    )
    write_csv(out / "top_candidates.csv", rows)
    write_csv(out / "debug" / "settlement_dispositions.csv", rows)
    write_json(out / "debug" / "date_coverage_audit.json", summary["price_coverage"])
    write_json(
        out / "debug" / "command_results.json",
        {
            "audit_mode": "formal_disposition",
            "exit_data_read": True,
            "return_values_read_or_written": False,
            "authoritative_ledgers_mutated": False,
            "state_gate_passed": True,
        },
    )
    write_formal_evidence_snapshots(paths)


def write_formal_evidence_snapshots(paths: runner.SettlementPaths) -> None:
    root = paths.root
    price_paths = sorted(paths.price_dir.glob("*.csv"))
    baseline_paths = sorted(paths.baseline_price_dir.glob("*.csv"))
    snapshot_paths = [
        *(root / relative for relative in runner.disposition_contract.CORE_SNAPSHOT_RELATIVE_PATHS),
        root / str(active_pointer()["manifest_path"]),
        *price_paths,
        *baseline_paths,
    ]
    hashes = {
        path.relative_to(root).as_posix(): file_sha256(path) for path in snapshot_paths
    }
    checkpoints = {
        "observation_ledger": verify_ledger_checkpoint(
            root / runner.disposition_contract.EVENT_LEDGER_RELATIVE
        ),
        "candidate_entry_freeze": verify_ledger_checkpoint(
            root / "logs/v5_33_fund_flow_entry_price_freeze.jsonl"
        ),
        "benchmark_entry_freeze": verify_ledger_checkpoint(
            root / "logs/v5_34_fund_flow_benchmark_entry_freeze.jsonl"
        ),
        "cohort_history": verify_ledger_checkpoint(
            root / "logs/v5_31_fund_flow_evidence_freeze_history.jsonl"
        ),
    }
    calendar_validation = {
        "checked": True,
        "calendar_dates_valid": True,
        "entry_date": audit.ENTRY_DATE,
        "exit_date": audit.EXIT_DATE,
        "entry_date_present": True,
        "exit_date_present": True,
    }
    base = {
        "sensitive_file_sha256": hashes,
        "checkpoint_verification": checkpoints,
        "global_observation_count": 4,
        "target_observation_count": 4,
        "candidate_freeze_count": 4,
        "benchmark_freeze_count": 4,
        "active_cohort_id": "active-fixture",
        "active_cohort_manifest_hash": ACTIVE_HASH,
        "active_cohort_validated": True,
        "v5_30_active_cohort_id": "active-fixture",
        "v5_30_active_cohort_manifest_hash": ACTIVE_HASH,
        "v5_30_as_of_date": audit.EXIT_DATE,
        "v5_30_generated_at": "2026-07-21T15:15:00+08:00",
        "current_state_active_cohort_id": "active-fixture",
        "current_state_active_cohort_manifest_hash": ACTIVE_HASH,
        "current_state_generated_at": "2026-07-21T15:20:00+08:00",
        "calendar_dates_valid": True,
        "calendar_validation": calendar_validation,
        "baseline_price_cache_snapshot": price_cache_snapshot(
            paths.baseline_price_dir
        ),
        "baseline_quarantined_file_sha256": {
            "801156": file_sha256(paths.baseline_price_dir / "801156.csv")
        },
    }
    write_json(
        paths.final_out / "debug" / "pre_settlement_snapshot.json",
        {**base, "snapshot_phase": "pre", "captured_at": "2026-07-21T15:31:00+08:00"},
    )
    write_json(
        paths.final_out / "debug" / "post_settlement_snapshot.json",
        {
            **base,
            "snapshot_phase": "post",
            "captured_at": "2026-07-21T15:32:00+08:00",
            "authoritative_hashes_unchanged": True,
        },
    )
    manifest_paths = list(snapshot_paths)
    for relative in runner.disposition_contract.CORE_MANIFEST_RELATIVE_PATHS:
        path = root / relative
        if path not in manifest_paths:
            manifest_paths.append(path)
    write_csv(
        paths.final_out / "debug" / "sha256_manifest.csv",
        [
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
            for path in manifest_paths
        ],
    )


def pending_summary(status: str) -> dict[str, Any]:
    return {
        "completion_status": status,
        "record_count": 4,
        "pending_count": 4,
        "blocked_count": 0,
        "settled_count": 0,
        "qualified_settled_count": 0,
        "settlement_disposition_complete": False,
        "all_return_fields_empty": True,
        "promotion_ready": False,
        "can_claim_strong_rebound_industries": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    }


def fixture_step_validation(
    step_id: str,
    _paths: runner.SettlementPaths,
    _context: object,
) -> dict[str, Any]:
    if step_id == "v5_27":
        return {
            "execution_mode": "read_only_audit",
            "read_only": True,
            "proposed_settlement_count": 0,
            "event_ledger_write_invoked": False,
            "materialized_ledger_write_invoked": False,
            "checkpoint_write_invoked": False,
            "authoritative_ledger_files_unchanged": True,
        }
    return {"fixture_step": step_id}


def at_gate() -> datetime:
    return datetime(2026, 7, 21, 15, 30, 0, tzinfo=audit.SHANGHAI)


def test_command_plan_has_the_required_order_and_preserves_v530_exit_two(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    overlay = paths.scripts / "overlay_v5_29_exploratory_disposition.py"
    overlay.write_text("# fixture\n", encoding="utf-8")
    plan = runner.build_command_plan(paths, overlay)

    assert [item.step_id for item in plan] == [
        "v5_31",
        "v5_30_pre",
        "v5_27",
        "v5_30_post",
        "v5_28",
        "current_state_pre",
        "readiness_initial",
        "v5_29",
        "v5_35",
        "current_state_post",
        "readiness_final",
        "compact_output_audit",
        "v5_29_exploratory_overlay",
        "build_current_status",
    ]
    assert plan[1].expected_exit_codes == (2,)
    assert plan[3].expected_exit_codes == (2,)
    assert plan[12].script == overlay
    assert plan[7].arguments == ("--as-of-date", "2026-07-21")
    assert plan[2].arguments == ("--as-of-date", "2026-07-21", "--read-only")
    assert "command_results.json" in plan[11].arguments


def test_incomplete_coverage_writes_pending_and_never_starts_chain(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    overlay = paths.scripts / "overlay_v5_29_exploratory_disposition.py"
    overlay.write_text("# fixture\n", encoding="utf-8")
    readiness = paths.scripts / "audit_fund_flow_exploratory_settlement_readiness.py"
    readiness.write_text("# fixture\n", encoding="utf-8")
    install_prices(paths, count=100, exact_exit=False)
    called: list[str] = []

    def execute(spec: runner.CommandSpec) -> dict[str, Any]:
        called.append(spec.step_id)
        write_json(paths.preflight_out / "run_summary.json", pending_summary("pending_exact_price_coverage"))
        return {"exit_code": 3, "stdout": "pending", "stderr": ""}

    exit_code, result = runner.orchestrate(
        paths,
        now=at_gate(),
        overlay_script=overlay,
        executor=execute,
    )

    assert exit_code == 3
    assert called == ["readiness_pending_exact_coverage"]
    assert result["chain_started"] is False
    assert result["chain_completed"] is False
    assert result["completion_status"] == "pending_exact_price_coverage"
    assert result["price_coverage"]["entry_exit_common_count"] == 0
    assert Path(result["command_results_path"]) == paths.preflight_out / "debug" / "command_results.json"


def test_fake_offline_chain_records_all_commands_and_self_validates_results(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    install_active_pointer(paths)
    install_prices(paths, count=100)
    install_authoritative_sources(paths)
    overlay = paths.scripts / "overlay_v5_29_exploratory_disposition.py"
    overlay.write_text("# fixture overlay\n", encoding="utf-8")
    install_command_scripts(paths, overlay)

    def execute(spec: runner.CommandSpec) -> dict[str, Any]:
        if spec.step_id in {"readiness_initial", "readiness_final"}:
            write_formal_fixture(paths)
        if spec.step_id == "v5_29":
            write_json(paths.v529_summary, {"fixture": True})
        return {
            "exit_code": 2 if spec.step_id in {"v5_30_pre", "v5_30_post"} else 0,
            "stdout": f"fixture {spec.step_id}",
            "stderr": "",
        }

    exit_code, result = runner.orchestrate(
        paths,
        now=at_gate(),
        overlay_script=overlay,
        executor=execute,
        step_validator=fixture_step_validation,
    )

    assert exit_code == 0
    assert result["chain_completed"] is True
    assert result["completion_status"] == "complete_terminal_exclusions"
    assert result["verified_price_file_count"] == 101
    assert len(result["commands"]) == 14
    assert [item["exit_code"] for item in result["commands"] if item["step_id"].startswith("v5_30_")] == [2, 2]
    assert all(item["exit_code_expected"] for item in result["commands"])
    assert all(item["semantic_validation"]["passed"] for item in result["commands"])
    assert all(item["authoritative_hashes_unchanged"] for item in result["commands"])
    assert all(item["active_pointer_governance_unchanged"] for item in result["commands"])
    assert all(len(item["script_sha256"]) == 64 for item in result["commands"])
    stored = json.loads((paths.final_out / "debug" / "command_results.json").read_text(encoding="utf-8"))
    assert stored["command_results_validation"]["passed"] is True
    assert stored["command_results_validation"]["evidence"]["command_count"] == 14


def test_authoritative_mutation_stops_after_first_command_and_reports_real_path(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    install_prices(paths, count=100)
    overlay = paths.scripts / "overlay_v5_29_exploratory_disposition.py"
    overlay.write_text("# fixture overlay\n", encoding="utf-8")
    install_command_scripts(paths, overlay)
    ledger = paths.root / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("immutable\n", encoding="utf-8")

    def execute(spec: runner.CommandSpec) -> dict[str, Any]:
        ledger.write_text("mutated\n", encoding="utf-8")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    exit_code, result = runner.orchestrate(
        paths,
        now=at_gate(),
        overlay_script=overlay,
        executor=execute,
        step_validator=fixture_step_validation,
    )

    assert exit_code == 2
    assert len(result["commands"]) == 1
    assert result["failure"]["step_id"] == "v5_31"
    assert result["authoritative_hashes_unchanged"] is False
    actual = paths.preflight_out / "debug" / "command_results.json"
    assert Path(result["command_results_path"]) == actual
    assert actual.is_file()
    assert not paths.final_out.exists()


def test_v530_exit_two_requires_exact_fail_closed_counts() -> None:
    active = active_pointer()
    summary = {
        "as_of_date": "2026-07-21",
        "active_cohort_id": active["cohort_id"],
        "active_cohort_manifest_hash": active["manifest_hash"],
        "ledger_rows": 0,
        "global_ledger_rows": 4,
        "global_late_backfill_count": 8,
        "global_violation_count": 16,
        "integrity_passed": False,
        "global_ledger_integrity_passed": False,
        "can_claim_strong_rebound_industries": False,
        "goal_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_ledger_integrity_failed",
    }
    evidence = runner.validate_v530_fail_closed(summary, active)
    assert evidence["expected_fail_closed"] is True
    with pytest.raises(runner.OrchestrationError, match="global_late_backfill_count"):
        runner.validate_v530_fail_closed({**summary, "global_late_backfill_count": 7}, active)


def test_current_state_keeps_main_decision_as_of_and_only_refreshes_audit_date() -> None:
    active = active_pointer()
    current_runner = {"as_of_date": "2026-07-18"}
    summary = {
        "generated_at": "2026-07-21T16:00:00+08:00",
        "current_as_of_date": "2026-07-18",
        "active_cohort_id": active["cohort_id"],
        "active_cohort_manifest_hash": active["manifest_hash"],
        "state_consistent": True,
        "fail_count": 0,
        "policy_status": "research_only",
        "current_action": "NO_ACTION",
        "strong_industry_alpha_validated": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "true_forward_route_ready": False,
        "auto_execution_allowed": False,
    }
    evidence = runner.validate_current_state(summary, active, current_runner)
    assert evidence["current_as_of_date"] == "2026-07-18"
    assert evidence["audit_generated_on"] == "2026-07-21"
    with pytest.raises(runner.OrchestrationError, match="current_as_of_date"):
        runner.validate_current_state({**summary, "current_as_of_date": "2026-07-21"}, active, current_runner)


@pytest.mark.parametrize(
    "generated_at",
    [
        "2026-07-21T16:00:00",
        "2026-07-21T08:00:00Z",
        "2026-07-21T17:00:00+09:00",
        "2026-07-22T00:00:00+08:00",
    ],
)
def test_current_state_rejects_non_shanghai_or_wrong_date_timestamp(generated_at: str) -> None:
    active = active_pointer()
    current_runner = {"as_of_date": "2026-07-18"}
    summary = {
        "generated_at": generated_at,
        "current_as_of_date": "2026-07-18",
        "active_cohort_id": active["cohort_id"],
        "active_cohort_manifest_hash": active["manifest_hash"],
        "state_consistent": True,
        "fail_count": 0,
        "policy_status": "research_only",
        "current_action": "NO_ACTION",
        "strong_industry_alpha_validated": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "true_forward_route_ready": False,
        "auto_execution_allowed": False,
    }
    with pytest.raises(runner.OrchestrationError, match="timezone-aware Shanghai timestamp"):
        runner.validate_current_state(summary, active, current_runner)


def test_active_pointer_allows_only_verified_at_timestamp_change() -> None:
    before = active_pointer()
    after = {**before, "verified_at_utc": "2026-07-21T08:00:00Z"}
    assert runner.active_pointer_governance(before) == runner.active_pointer_governance(after)
    changed_pair = {**after, "manifest_hash": "8" * 64}
    assert runner.active_pointer_governance(before) != runner.active_pointer_governance(changed_pair)


@pytest.mark.parametrize("mutation", ["add", "delete", "modify"])
def test_price_file_set_and_content_are_immutable_during_chain(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths = make_paths(tmp_path)
    install_prices(paths, count=100)
    overlay = paths.scripts / "overlay_v5_29_exploratory_disposition.py"
    overlay.write_text("# fixture overlay\n", encoding="utf-8")
    install_command_scripts(paths, overlay)
    target = paths.price_dir / f"{price_codes()[0]}.csv"

    def execute(_spec: runner.CommandSpec) -> dict[str, Any]:
        if mutation == "add":
            (paths.price_dir / "999999.csv").write_text(
                "industry_code,trade_date,close\n999999,2026-06-23,100\n999999,2026-07-21,101\n",
                encoding="utf-8",
            )
        elif mutation == "delete":
            target.unlink()
        else:
            target.write_text(target.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    exit_code, result = runner.orchestrate(
        paths,
        now=at_gate(),
        overlay_script=overlay,
        executor=execute,
        step_validator=fixture_step_validation,
    )

    assert exit_code == 2
    assert len(result["commands"]) == 1
    assert result["failure"]["step_id"] == "v5_31"
    assert result["authoritative_hashes_unchanged"] is False
    diff_paths = set(result["commands"][0]["authoritative_hash_diff"])
    assert any(
        path.startswith(
            "data_catalog/cache/industry_index/history/settlement_2026_07_21/second/"
        )
        for path in diff_paths
    )


def test_mainline_price_cache_is_immutable_during_chain(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    install_prices(paths, count=100)
    paths.baseline_price_dir.mkdir(parents=True, exist_ok=True)
    for source_path in paths.price_dir.glob("*.csv"):
        (paths.baseline_price_dir / source_path.name).write_bytes(
            source_path.read_bytes()
        )
    overlay = paths.scripts / "overlay_v5_29_exploratory_disposition.py"
    overlay.write_text("# fixture overlay\n", encoding="utf-8")
    install_command_scripts(paths, overlay)
    target = paths.baseline_price_dir / f"{price_codes()[0]}.csv"

    def execute(_spec: runner.CommandSpec) -> dict[str, Any]:
        target.write_text(
            target.read_text(encoding="utf-8") + "# changed\n",
            encoding="utf-8",
        )
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    exit_code, result = runner.orchestrate(
        paths,
        now=at_gate(),
        overlay_script=overlay,
        executor=execute,
        step_validator=fixture_step_validation,
    )

    assert exit_code == 2
    assert result["failure"]["step_id"] == "v5_31"
    assert result["authoritative_hashes_unchanged"] is False
    diff_paths = set(result["commands"][0]["authoritative_hash_diff"])
    assert any(
        path.startswith("data_catalog/cache/industry_index/history/second/")
        for path in diff_paths
    )


def test_non_timestamp_active_pointer_drift_stops_chain(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    install_active_pointer(paths)
    install_prices(paths, count=100)
    overlay = paths.scripts / "overlay_v5_29_exploratory_disposition.py"
    overlay.write_text("# fixture overlay\n", encoding="utf-8")
    install_command_scripts(paths, overlay)

    def execute(_spec: runner.CommandSpec) -> dict[str, Any]:
        write_json(paths.active_pointer, {**active_pointer(), "operator": "unexpected"})
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    exit_code, result = runner.orchestrate(
        paths,
        now=at_gate(),
        overlay_script=overlay,
        executor=execute,
        step_validator=lambda step_id, _paths, _context: {"fixture_step": step_id},
    )

    assert exit_code == 2
    assert len(result["commands"]) == 1
    assert result["failure"]["step_id"] == "v5_31"
    assert result["active_pointer_governance_unchanged"] is False
    assert "beyond verified_at_utc" in result["failure"]["message"]


def test_command_results_timestamp_requires_explicit_shanghai_offset_and_exact_date() -> None:
    assert runner.strict_shanghai_timestamp(
        "2026-07-21T15:30:00+08:00",
        expected_date="2026-07-21",
    ) is not None
    for value in (
        "2026-07-21T15:30:00",
        "2026-07-21T07:30:00Z",
        "2026-07-21T15:30:00+09:00",
        "2026-07-22T15:30:00+08:00",
    ):
        assert runner.strict_shanghai_timestamp(value, expected_date="2026-07-21") is None


def test_post_commit_overlay_failure_invalidates_downstream_claim_but_keeps_formal_core(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    install_active_pointer(paths)
    install_prices(paths, count=100)
    install_authoritative_sources(paths)
    overlay = paths.scripts / "overlay_v5_29_exploratory_disposition.py"
    overlay.write_text("# fixture overlay\n", encoding="utf-8")
    install_command_scripts(paths, overlay)

    def execute(spec: runner.CommandSpec) -> dict[str, Any]:
        if spec.step_id in {"readiness_initial", "readiness_final"}:
            write_formal_fixture(paths)
        if spec.step_id == "v5_29":
            write_json(
                paths.v529_summary,
                {
                    "active_cohort_id": "active-fixture",
                    "active_cohort_manifest_hash": ACTIVE_HASH,
                    "exploratory_disposition_valid": True,
                    "exploratory_completion_status": "complete_terminal_exclusions",
                    "exploratory_terminal_blocked_count": 4,
                    "exploratory_pending_count": 0,
                },
            )
        exit_code = 2 if spec.step_id in {"v5_30_pre", "v5_30_post"} else 0
        if spec.step_id == "v5_29_exploratory_overlay":
            exit_code = 9
        return {"exit_code": exit_code, "stdout": f"fixture {spec.step_id}", "stderr": ""}

    exit_code, result = runner.orchestrate(
        paths,
        now=at_gate(),
        overlay_script=overlay,
        executor=execute,
        step_validator=fixture_step_validation,
    )

    assert exit_code == 2
    assert result["formal_package_committed"] is True
    assert result["compact_output_audit_passed"] is True
    assert result["chain_completed"] is False
    assert result["completion_status"] == "orchestration_failed"
    assert paths.formal_commit.is_file()
    assert result["downstream_invalidation"]["v5_29_invalidated"] is True
    invalidated = json.loads(paths.v529_summary.read_text(encoding="utf-8"))
    assert invalidated["exploratory_disposition_valid"] is False
    assert invalidated["exploratory_completion_status"] == "integration_failed_fail_closed"
    assert invalidated["exploratory_terminal_blocked_count"] == 0
    assert invalidated["exploratory_pending_count"] == 4
    normalized = runner.disposition_contract.load_optional_disposition(
        active_pointer(),
        summary_path=paths.final_out / "run_summary.json",
        dispositions_path=paths.final_out / "debug" / "settlement_dispositions.csv",
        project_root=paths.root,
    )
    assert normalized is not None and normalized["valid"] is True


def test_formal_validator_uses_shared_contract_and_rejects_any_return_value(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    install_active_pointer(paths)
    install_prices(paths, count=100)
    install_authoritative_sources(paths)
    write_formal_fixture(paths)
    evidence = runner.validate_formal_artifacts(paths, require_final_commit=False)
    assert evidence["shared_disposition_contract"] is True
    rows = disposition_rows()
    rows[0]["realized_return"] = "0.01"
    write_csv(paths.final_out / "debug" / "settlement_dispositions.csv", rows)
    with pytest.raises(runner.OrchestrationError, match="shared formal disposition contract failed"):
        runner.validate_formal_artifacts(paths, require_final_commit=False)


def test_frozen_v529_source_hash_is_still_bound() -> None:
    assert file_sha256(ROOT / "scripts" / "build_v5_29_fund_flow_evidence_calendar.py") == runner.FROZEN_V529_SHA256
