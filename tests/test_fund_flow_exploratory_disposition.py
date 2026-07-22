from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fund_flow_exploratory_disposition as disposition
import audit_fund_flow_exploratory_settlement_readiness as settlement_audit
from fund_flow_exploratory_price_contract import price_cache_snapshot
from fund_flow_forward_evidence import append_events, verify_ledger_checkpoint


ACTIVE = {
    "cohort_id": "active-v1",
    "manifest_hash": "a" * 64,
    "manifest_path": "logs/v5_31_fund_flow_evidence_freeze/active-v1/manifest.csv",
    "freeze_passed": True,
}


def valid_summary() -> dict[str, object]:
    return {
        "audit_mode": "formal_disposition",
        "completion_status": "complete_terminal_exclusions",
        "record_count": 4,
        "settled_count": 0,
        "blocked_count": 4,
        "pending_count": 0,
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
        "policy_status": "research_only",
        "current_action": "NO_ACTION",
        "promotion_ready": False,
        "can_claim_strong_rebound_industries": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "active_cohort_id": ACTIVE["cohort_id"],
        "active_cohort_manifest_hash": ACTIVE["manifest_hash"],
        "active_cohort_validated": True,
        "required_start_at": "2026-07-21T15:00:00+08:00",
        "price_coverage": {
            "checked": True,
            "reason": "exact dates only; no price or return value retained",
            "source_file_count": 101,
            "valid_source_file_count": 101,
            "invalid_file_count": 0,
            "invalid_files": [],
            "entry_industry_count": 100,
            "exit_industry_count": 100,
            "exact_coverage_ready": True,
            "entry_exit_common_count": 100,
            "candidate_entry_count": 4,
            "candidate_exit_count": 4,
            "candidate_common_count": 4,
            "overall_max_date": "2026-07-21",
            "minimum_benchmark_industries": 100,
            "price_values_retained": False,
        },
        "generated_at": "2026-07-21T15:30:00+08:00",
        "observed_at": "2026-07-21T15:30:00+08:00",
    }


def valid_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for observation_id, (industry_code, industry_name) in disposition.EXPECTED.items():
        row: dict[str, object] = {
            "record_type": "exploratory_settlement_disposition",
            "observation_id": observation_id,
            "industry_code": industry_code,
            "industry_name": industry_name,
            "signal_date": "2026-06-22",
            "planned_entry_date": "2026-06-23",
            "planned_exit_date": "2026-07-21",
            "ledger_settlement_status": "not_due",
            "selection_score": "1",
            "sample_scope": "exploratory_fund_flow_only",
            "qualified_for_goal": False,
            "integrity_eligible": False,
            "promotion_eligible": False,
            "source_cohort_id": "legacy_exploratory_20260622",
            "source_cohort_manifest_hash": "UNVERIFIED_LEGACY_COHORT",
            "source_fingerprint_status": "unverified_legacy",
            "calendar_fingerprint": "UNVERIFIED_LEGACY_CALENDAR",
            "code_version": "UNVERIFIED_LEGACY_CODE",
            "candidate_entry_freeze_status": "late_backfill_excluded",
            "benchmark_entry_freeze_status": "late_backfill_excluded",
            "record_contract_passed": True,
            "benchmark_universe_count": 0,
            "disposition": "blocked",
            "disposition_status": "blocked_terminal_late_freeze_excluded",
            "reason_codes": (
                "candidate_entry_freeze_late_backfill_excluded|"
                "benchmark_entry_freeze_late_backfill_excluded|"
                "not_qualified_for_goal|integrity_ineligible|promotion_ineligible"
            ),
        }
        row.update({field: "" for field in disposition.RETURN_FIELDS})
        rows.append(row)
    return rows


def write_package(
    root: Path,
    *,
    summary: dict[str, object] | None = None,
    rows: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    install_authoritative_fixture(root)
    summary_path = root / "run_summary.json"
    rows_path = root / "debug" / "settlement_dispositions.csv"
    rows_path.parent.mkdir(parents=True)
    saved_summary = summary or valid_summary()
    refresh = json.loads(
        (
            root
            / "outputs/audit/fund_flow_exploratory_settlement_price_refresh_2026_07_21/run_summary.json"
        ).read_text(encoding="utf-8")
    )
    live_coverage = settlement_audit.scan_exact_date_coverage(
        root / disposition.PRICE_DIRECTORY_RELATIVE
    )
    gate = settlement_audit.assess_price_source_gate(
        refresh,
        price_cache_snapshot(root / disposition.PRICE_DIRECTORY_RELATIVE),
        live_coverage,
        price_cache_snapshot(
            root / disposition.BASELINE_PRICE_DIRECTORY_RELATIVE
        ),
        settlement_audit.quarantined_file_hashes(
            root / disposition.BASELINE_PRICE_DIRECTORY_RELATIVE
        ),
        project_root=root,
        settlement_price_dir=root / disposition.PRICE_DIRECTORY_RELATIVE,
        baseline_price_dir=root / "data_catalog/cache/industry_index/history/second",
        producer_paths=(
            root / "scripts/refresh_fund_flow_exploratory_settlement_prices.py",
            root / "scripts/run_industry_index_research_validation.py",
            root / "scripts/fund_flow_exploratory_price_contract.py",
        ),
    )
    saved_summary.setdefault("price_source_gate_passed", gate["all_passed"])
    saved_summary["price_source_gate_checks"] = gate["checks"]
    saved_summary["price_source_gate_reason_codes"] = gate["reason_codes"]
    summary_path.write_text(json.dumps(saved_summary, ensure_ascii=False), encoding="utf-8")
    saved_rows = rows or valid_rows()
    with rows_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(saved_rows[0]))
        writer.writeheader()
        writer.writerows(saved_rows)
    write_csv(root / "top_candidates.csv", saved_rows)
    (root / "report.md").write_text(
        "# 四条探索性资金流记录终局处置\n\n"
        "research_only / NO_ACTION\n\ncompletion_status=complete_terminal_exclusions\n"
        "settled `0` / blocked `4` / pending `0`\nqualified_settled_count=0\n",
        encoding="utf-8",
    )
    write_json(root / "debug" / "date_coverage_audit.json", saved_summary["price_coverage"])
    write_json(
        root / "debug" / "command_results.json",
        valid_orchestration_results(),
    )
    write_snapshots_and_manifest(root)
    return summary_path, rows_path


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def append_fixture_events(path: Path, rows: list[dict[str, object]]) -> None:
    append_events(path, rows)


def install_authoritative_fixture(root: Path) -> None:
    ledger = root / disposition.EVENT_LEDGER_RELATIVE
    observations: list[dict[str, object]] = []
    for observation_id, (industry_code, industry_name) in disposition.EXPECTED.items():
        row: dict[str, object] = {
            "event_type": "observation",
            "event_id": f"{observation_id}:observation",
            "observation_id": observation_id,
            "industry_code": industry_code,
            "industry_name": industry_name,
            "signal_date": "2026-06-22",
            "planned_entry_date": disposition.ENTRY_DATE,
            "planned_exit_date": disposition.EXIT_DATE,
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
        row.update({field: "" for field in disposition.RETURN_FIELDS})
        observations.append(row)
    append_fixture_events(ledger, observations)

    chain_counts = {
        "logs/v5_33_fund_flow_entry_price_freeze.jsonl": 4,
        "logs/v5_34_fund_flow_benchmark_entry_freeze.jsonl": 4,
        "logs/v5_31_fund_flow_evidence_freeze_history.jsonl": 1,
    }
    for relative, count in chain_counts.items():
        append_fixture_events(
            root / relative,
            [
                {
                    "event_type": "fixture",
                    "event_id": f"{Path(relative).stem}:{index}",
                }
                for index in range(count)
            ],
        )

    static_files = {
        "logs/v5_25_fund_flow_forward_ledger.csv": "fixture\n",
        "logs/v5_31_fund_flow_evidence_freeze_active.json": json.dumps(ACTIVE),
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
        str(ACTIVE["manifest_path"]): "artifact_id,artifact_type,fingerprint\n",
    }
    for relative, text in static_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    price_dir = root / disposition.PRICE_DIRECTORY_RELATIVE
    price_dir.mkdir(parents=True, exist_ok=True)
    candidates = [code for code, _name in disposition.EXPECTED.values()]
    extras = [f"{810000 + index:06d}" for index in range(100 - len(candidates))]
    for code in [*candidates, *extras]:
        (price_dir / f"{code}.csv").write_text(
            f"industry_code,trade_date,close\n{code},2026-06-23,100\n"
            f"{code},2026-07-21,101\n",
            encoding="utf-8",
        )
    (price_dir / "801156.csv").write_text(
        "industry_code,trade_date,close\n801156,2026-06-10,100\n"
        "801156,2026-06-12,101\n",
        encoding="utf-8",
    )
    baseline_dir = root / disposition.BASELINE_PRICE_DIRECTORY_RELATIVE
    baseline_dir.mkdir(parents=True, exist_ok=True)
    for source_path in price_dir.glob("*.csv"):
        (baseline_dir / source_path.name).write_bytes(source_path.read_bytes())
    snapshot = price_cache_snapshot(price_dir)
    aggregate = snapshot["aggregate_sha256"]
    count = snapshot["csv_file_count"]
    quarantine_sha256 = disposition.file_sha256(price_dir / "801156.csv")
    baseline_snapshot = price_cache_snapshot(baseline_dir)
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
    refresh = {
        "audit_mode": "fund_flow_exploratory_settlement_price_only_refresh",
        "cache_scope": "dedicated_exploratory_settlement_only",
        "mainline_price_cache_write_invoked": False,
        "cache_bootstrap": {
            "checked": True,
            "action": "created_from_mainline",
            "baseline_path": "data_catalog/cache/industry_index/history/second",
            "settlement_path": disposition.PRICE_DIRECTORY_RELATIVE,
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
                "bytes": (root / relative).stat().st_size,
                "sha256": disposition.file_sha256(root / relative),
            }
            for relative in producer_relatives
        ],
        "completion_status": "committed",
        "generated_at": "2026-07-21T15:10:00+08:00",
        "time_gate_passed": True,
        "entry_date": disposition.ENTRY_DATE,
        "exit_date": disposition.EXIT_DATE,
        "target_industry_codes": [code for code, _name in disposition.EXPECTED.values()],
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
            "source_accounted_industry_count": count,
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
            "existing_universe_aggregate_sha256": "b" * 64,
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
    }
    write_json(
        root
        / "outputs/audit/fund_flow_exploratory_settlement_price_refresh_2026_07_21/run_summary.json",
        refresh,
    )


def valid_orchestration_results() -> dict[str, object]:
    return {
        "schema_version": "fund-flow-exploratory-settlement-orchestration-v1",
        "generated_at": "2026-07-21T15:40:00+08:00",
        "policy_status": "research_only",
        "current_action": "NO_ACTION",
        "completion_status": "complete_terminal_exclusions",
        "chain_started": True,
        "chain_completed": True,
        "formal_package_committed": True,
        "compact_output_audit_passed": True,
        "authoritative_hashes_unchanged": True,
        "active_pointer_governance_unchanged": True,
        "failure": None,
        "qualified_settled_count": 0,
        "promotion_ready": False,
        "can_claim_strong_rebound_industries": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    }


def valid_base_commands() -> list[dict[str, object]]:
    step_ids = [
        "v5_31", "v5_30_pre", "v5_27", "v5_30_post", "v5_28",
        "current_state_pre", "readiness_initial", "v5_29", "v5_35",
        "current_state_post", "readiness_final", "compact_output_audit",
    ]
    commands: list[dict[str, object]] = []
    for step_id in step_ids:
        expected_exit = 2 if step_id in {"v5_30_pre", "v5_30_post"} else 0
        semantic: dict[str, object] = {"passed": True, "evidence": {}}
        command = ["python", "-B", f"scripts/{step_id}.py"]
        if step_id == "v5_27":
            command = [
                "python",
                "-B",
                "scripts/settle_v5_27_fund_flow_forward_samples.py",
                "--as-of-date",
                disposition.EXIT_DATE,
                "--read-only",
            ]
            semantic["evidence"] = {
                "execution_mode": "read_only_audit",
                "read_only": True,
                "proposed_settlement_count": 0,
                "event_ledger_write_invoked": False,
                "materialized_ledger_write_invoked": False,
                "checkpoint_write_invoked": False,
                "authoritative_ledger_files_unchanged": True,
            }
        commands.append({
            "step_id": step_id,
            "exit_code": expected_exit,
            "expected_exit_codes": [expected_exit],
            "exit_code_expected": True,
            "script_sha256": "c" * 64,
            "script_sha256_unchanged": True,
            "authoritative_hashes_unchanged": True,
            "active_pointer_governance_unchanged": True,
            "semantic_validation": semantic,
            "command": command,
        })
    return commands


def write_formal_commit_fixture(root: Path) -> None:
    paths = disposition.package_artifact_paths(
        root / "run_summary.json",
        root / "debug/settlement_dispositions.csv",
    )
    bound = {
        "report.md": paths["report"],
        "run_summary.json": paths["summary"],
        "top_candidates.csv": paths["top_candidates"],
        "debug/settlement_dispositions.csv": paths["dispositions"],
        "debug/sha256_manifest.csv": paths["sha256_manifest"],
        "debug/pre_settlement_snapshot.json": paths["pre_snapshot"],
        "debug/post_settlement_snapshot.json": paths["post_snapshot"],
        "debug/date_coverage_audit.json": paths["date_coverage_audit"],
    }
    write_json(
        paths["formal_commit"],
        {
            "schema_version": "fund-flow-exploratory-formal-commit-v1",
            "formal_disposition_committed": True,
            "committed_at": "2026-07-21T15:39:00+08:00",
            "completion_status": "complete_terminal_exclusions",
            "compact_output_audit_passed": True,
            "active_cohort_id": ACTIVE["cohort_id"],
            "active_cohort_manifest_hash": ACTIVE["manifest_hash"],
            "formal_summary_generated_at": "2026-07-21T15:30:00+08:00",
            "artifact_sha256": {
                name: disposition.file_sha256(path) for name, path in bound.items()
            },
            "base_commands": valid_base_commands(),
        },
    )


def write_snapshots_and_manifest(root: Path) -> None:
    price_paths = sorted((root / disposition.PRICE_DIRECTORY_RELATIVE).glob("*.csv"))
    baseline_paths = sorted(
        (root / disposition.BASELINE_PRICE_DIRECTORY_RELATIVE).glob("*.csv")
    )
    required = [root / relative for relative in disposition.CORE_SNAPSHOT_RELATIVE_PATHS]
    required.append(root / str(ACTIVE["manifest_path"]))
    snapshot_paths = [*required, *price_paths, *baseline_paths]
    hashes = {
        path.relative_to(root).as_posix(): disposition.file_sha256(path)
        for path in snapshot_paths
    }
    checkpoint_paths = {
        "observation_ledger": root / disposition.EVENT_LEDGER_RELATIVE,
        "candidate_entry_freeze": root / "logs/v5_33_fund_flow_entry_price_freeze.jsonl",
        "benchmark_entry_freeze": root / "logs/v5_34_fund_flow_benchmark_entry_freeze.jsonl",
        "cohort_history": root / "logs/v5_31_fund_flow_evidence_freeze_history.jsonl",
    }
    checkpoints = {
        key: verify_ledger_checkpoint(path) for key, path in checkpoint_paths.items()
    }
    calendar_validation = {
        "checked": True,
        "calendar_dates_valid": True,
        "entry_date": disposition.ENTRY_DATE,
        "exit_date": disposition.EXIT_DATE,
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
        "active_cohort_id": ACTIVE["cohort_id"],
        "active_cohort_manifest_hash": ACTIVE["manifest_hash"],
        "active_cohort_validated": True,
        "v5_30_active_cohort_id": ACTIVE["cohort_id"],
        "v5_30_active_cohort_manifest_hash": ACTIVE["manifest_hash"],
        "v5_30_as_of_date": disposition.EXIT_DATE,
        "v5_30_generated_at": "2026-07-21T15:15:00+08:00",
        "current_state_active_cohort_id": ACTIVE["cohort_id"],
        "current_state_active_cohort_manifest_hash": ACTIVE["manifest_hash"],
        "current_state_generated_at": "2026-07-21T15:20:00+08:00",
        "calendar_dates_valid": True,
        "calendar_validation": calendar_validation,
        "baseline_price_cache_snapshot": price_cache_snapshot(
            root / disposition.BASELINE_PRICE_DIRECTORY_RELATIVE
        ),
        "baseline_quarantined_file_sha256": {
            "801156": disposition.file_sha256(
                root
                / disposition.BASELINE_PRICE_DIRECTORY_RELATIVE
                / "801156.csv"
            )
        },
    }
    write_json(
        root / "debug" / "pre_settlement_snapshot.json",
        {**base, "snapshot_phase": "pre", "captured_at": "2026-07-21T15:31:00+08:00"},
    )
    write_json(
        root / "debug" / "post_settlement_snapshot.json",
        {
            **base,
            "snapshot_phase": "post",
            "captured_at": "2026-07-21T15:32:00+08:00",
            "authoritative_hashes_unchanged": True,
        },
    )
    manifest_paths = list(snapshot_paths)
    for relative in disposition.CORE_MANIFEST_RELATIVE_PATHS:
        path = root / relative
        if path not in manifest_paths:
            manifest_paths.append(path)
    manifest_rows = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": disposition.file_sha256(path),
        }
        for path in manifest_paths
    ]
    write_csv(root / "debug" / "sha256_manifest.csv", manifest_rows)
    if (root / "run_summary.json").is_file() and (
        root / "debug/settlement_dispositions.csv"
    ).is_file():
        write_formal_commit_fixture(root)


def test_missing_formal_package_is_optional() -> None:
    assert disposition.load_optional_disposition(
        ACTIVE,
        summary_path=Path("missing-summary.json"),
        dispositions_path=Path("missing-dispositions.csv"),
    ) is None


def test_valid_formal_package_normalizes_only_terminal_exclusions(tmp_path: Path) -> None:
    summary_path, rows_path = write_package(tmp_path)

    result = disposition.load_optional_disposition(
        ACTIVE,
        summary_path=summary_path,
        dispositions_path=rows_path,
        project_root=tmp_path,
    )

    assert result is not None
    assert result["valid"] is True
    assert result["observation_count"] == 4
    assert result["settled_count"] == 0
    assert result["terminal_blocked_count"] == 4
    assert result["pending_count"] == 0
    assert result["qualified_settled_count"] == 0


def test_readiness_only_package_is_not_publicly_consumable_without_commit_marker(
    tmp_path: Path,
) -> None:
    summary_path, rows_path = write_package(tmp_path)
    (tmp_path / "debug/formal_commit.json").unlink()

    with pytest.raises(disposition.ExploratoryDispositionError, match="formal_commit"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=rows_path,
            project_root=tmp_path,
        )

    internal = disposition.load_optional_disposition(
        ACTIVE,
        summary_path=summary_path,
        dispositions_path=rows_path,
        project_root=tmp_path,
        require_final_commit=False,
    )
    assert internal is not None and internal["valid"] is True


def test_compact_pending_command_results_are_rejected_even_with_a_marker(tmp_path: Path) -> None:
    summary_path, rows_path = write_package(tmp_path)
    payload = valid_orchestration_results()
    payload.update({"completion_status": "compact_output_audit_pending", "chain_completed": False})
    write_json(tmp_path / "debug/command_results.json", payload)

    with pytest.raises(disposition.ExploratoryDispositionError, match="post-compact phase"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=rows_path,
            project_root=tmp_path,
        )


def test_formal_commit_rejects_v527_without_mandatory_read_only_mode(tmp_path: Path) -> None:
    summary_path, rows_path = write_package(tmp_path)
    marker_path = tmp_path / "debug/formal_commit.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    v527 = next(item for item in marker["base_commands"] if item["step_id"] == "v5_27")
    v527["command"] = v527["command"][:-1]
    write_json(marker_path, marker)

    with pytest.raises(disposition.ExploratoryDispositionError, match="mandatory read-only"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=rows_path,
            project_root=tmp_path,
        )


def test_formal_commit_is_bound_to_the_formal_summary_timestamp(tmp_path: Path) -> None:
    summary_path, rows_path = write_package(tmp_path)
    marker_path = tmp_path / "debug/formal_commit.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["formal_summary_generated_at"] = "2026-07-21T15:31:00+08:00"
    write_json(marker_path, marker)

    with pytest.raises(disposition.ExploratoryDispositionError, match="summary timestamp differs"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=rows_path,
            project_root=tmp_path,
        )


def test_refresh_attestation_is_semantically_revalidated(tmp_path: Path) -> None:
    summary_path, rows_path = write_package(tmp_path)
    refresh_path = (
        tmp_path
        / "outputs/audit/fund_flow_exploratory_settlement_price_refresh_2026_07_21/run_summary.json"
    )
    refresh = json.loads(refresh_path.read_text(encoding="utf-8"))
    refresh["history_continuity"]["append_only_contract_passed"] = False
    write_json(refresh_path, refresh)
    # Rebind every outer hash so this exercises semantic verification rather
    # than merely detecting a stale manifest.
    write_snapshots_and_manifest(tmp_path)

    with pytest.raises(disposition.ExploratoryDispositionError, match="append-only continuity"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=rows_path,
            project_root=tmp_path,
        )


def test_partial_formal_package_fails_closed(tmp_path: Path) -> None:
    summary_path = tmp_path / "run_summary.json"
    summary_path.write_text(json.dumps(valid_summary()), encoding="utf-8")

    with pytest.raises(disposition.ExploratoryDispositionError, match="partial"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=tmp_path / "debug" / "settlement_dispositions.csv",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda summary, rows: summary.update({"blocked_count": 3}), "blocked_count"),
        (lambda summary, rows: summary.update({"active_cohort_manifest_hash": "b" * 64}), "pair mismatch"),
        (lambda summary, rows: rows[0].update({"qualified_for_goal": True}), "must remain false"),
        (lambda summary, rows: rows[0].update({"realized_return": "0.10"}), "prohibited return"),
        (lambda summary, rows: rows[0].update({"disposition": "settled"}), "disposition"),
        (lambda summary, rows: summary.update({"state_gate_passed": False}), "state_gate_passed"),
        (lambda summary, rows: summary["price_coverage"].update({"entry_exit_common_count": 99}), "at least 100"),  # type: ignore[union-attr]
        (lambda summary, rows: summary["price_coverage"].update({"candidate_common_count": 3}), "must equal 4"),  # type: ignore[union-attr]
        (lambda summary, rows: summary.update({"observed_at": "2026-07-21T14:59:59+08:00"}), "at or after"),
        (lambda summary, rows: rows[0].update({"benchmark_universe_count": 1}), "must remain 0"),
        (lambda summary, rows: rows[0].update({"reason_codes": "not_qualified_for_goal"}), "missing terminal reason"),
    ],
)
def test_tampered_formal_package_fails_closed(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    summary = valid_summary()
    rows = valid_rows()
    mutation(summary, rows)  # type: ignore[operator]
    summary_path, rows_path = write_package(tmp_path, summary=summary, rows=rows)

    with pytest.raises(disposition.ExploratoryDispositionError, match=message):
            disposition.load_optional_disposition(
                ACTIVE,
                summary_path=summary_path,
                dispositions_path=rows_path,
                project_root=tmp_path,
            )


@pytest.mark.parametrize(
    "relative",
    [
        "report.md",
        "top_candidates.csv",
        "debug/sha256_manifest.csv",
        "debug/pre_settlement_snapshot.json",
        "debug/command_results.json",
    ],
)
def test_any_missing_formal_component_fails_closed(tmp_path: Path, relative: str) -> None:
    summary_path, rows_path = write_package(tmp_path)
    (tmp_path / relative).unlink()

    with pytest.raises(disposition.ExploratoryDispositionError, match="partial"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=rows_path,
            project_root=tmp_path,
        )


def test_public_and_debug_disposition_tables_must_match(tmp_path: Path) -> None:
    summary_path, rows_path = write_package(tmp_path)
    top_rows = valid_rows()
    top_rows[0]["disposition_status"] = "tampered"
    write_csv(tmp_path / "top_candidates.csv", top_rows)

    with pytest.raises(disposition.ExploratoryDispositionError, match="top_candidates"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=rows_path,
            project_root=tmp_path,
        )


def test_current_authoritative_hash_drift_invalidates_formal_package(tmp_path: Path) -> None:
    summary_path, rows_path = write_package(tmp_path)
    current_state = tmp_path / "outputs/audit/current_state_consistency/run_summary.json"
    current_state.write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(disposition.ExploratoryDispositionError, match="manifest drift"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=rows_path,
            project_root=tmp_path,
        )


def test_exact_authoritative_observation_ids_are_required_even_with_fresh_hashes(
    tmp_path: Path,
) -> None:
    summary_path, rows_path = write_package(tmp_path)
    ledger = tmp_path / disposition.EVENT_LEDGER_RELATIVE
    append_events(
        ledger,
        [
            {
                "event_type": "observation",
                "event_id": "unexpected:observation",
                "observation_id": "unexpected",
                "sample_scope": "exploratory_fund_flow_only",
            }
        ],
    )
    write_snapshots_and_manifest(tmp_path)

    with pytest.raises(disposition.ExploratoryDispositionError, match="exact four observation IDs"):
        disposition.load_optional_disposition(
            ACTIVE,
            summary_path=summary_path,
            dispositions_path=rows_path,
            project_root=tmp_path,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda coverage: coverage.update({"checked": False}), "must be checked"),
        (lambda coverage: coverage.update({"entry_industry_count": 99}), "entry_industry_count"),
        (lambda coverage: coverage.update({"exit_industry_count": 99}), "exit_industry_count"),
        (lambda coverage: coverage.update({"entry_exit_common_count": 101}), "exceeds"),
        (lambda coverage: coverage.update({"candidate_entry_count": 3}), "candidate_entry_count"),
        (lambda coverage: coverage.update({"candidate_exit_count": 3}), "candidate_exit_count"),
        (lambda coverage: coverage.update({"overall_max_date": "2026-07-20"}), "overall_max_date"),
        (lambda coverage: coverage.update({"source_file_count": "100"}), "JSON integer"),
    ],
)
def test_price_coverage_relationships_fail_closed(mutation: object, message: str) -> None:
    coverage = dict(valid_summary()["price_coverage"])  # type: ignore[arg-type]
    mutation(coverage)  # type: ignore[operator]

    with pytest.raises(disposition.ExploratoryDispositionError, match=message):
        disposition.validate_price_coverage(coverage)


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-21T14:59:59+08:00",
        "2026-07-21T15:00:00",
        "2026-07-21T15:00:00Z",
        "2026-07-21T15:00:00+09:00",
    ],
)
def test_gate_timestamp_rejects_preclose_naive_or_wrong_offset(value: str) -> None:
    with pytest.raises(disposition.ExploratoryDispositionError):
        disposition.parse_gate_timestamp(value, field="observed_at")


def test_gate_timestamp_accepts_exact_close_and_later_dates() -> None:
    assert disposition.parse_gate_timestamp(
        "2026-07-21T15:00:00+08:00", field="observed_at"
    ) == disposition.START_GATE
    assert disposition.parse_gate_timestamp(
        "2026-07-22T09:00:00+08:00", field="observed_at"
    ) > disposition.START_GATE


def test_generated_at_cannot_precede_observed_at() -> None:
    summary = valid_summary()
    summary["observed_at"] = "2026-07-21T15:31:00+08:00"
    summary["generated_at"] = "2026-07-21T15:30:00+08:00"

    with pytest.raises(disposition.ExploratoryDispositionError, match="must not precede"):
        disposition.validate_summary(summary, ACTIVE)
