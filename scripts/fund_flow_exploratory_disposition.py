from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from fund_flow_exploratory_price_contract import price_cache_lock, price_cache_snapshot
from fund_flow_forward_evidence import materialize_observations, read_events, verify_ledger_checkpoint


ROOT = Path(__file__).resolve().parents[1]
FINAL_OUT = ROOT / "outputs" / "audit" / "fund_flow_exploratory_settlement_2026_07_21"
SUMMARY_PATH = FINAL_OUT / "run_summary.json"
DISPOSITIONS_PATH = FINAL_OUT / "debug" / "settlement_dispositions.csv"
REPORT_PATH = FINAL_OUT / "report.md"
TOP_CANDIDATES_PATH = FINAL_OUT / "top_candidates.csv"
DEBUG_PATH = FINAL_OUT / "debug"
MANIFEST_PATH = DEBUG_PATH / "sha256_manifest.csv"
PRE_SNAPSHOT_PATH = DEBUG_PATH / "pre_settlement_snapshot.json"
POST_SNAPSHOT_PATH = DEBUG_PATH / "post_settlement_snapshot.json"
COVERAGE_AUDIT_PATH = DEBUG_PATH / "date_coverage_audit.json"
COMMAND_RESULTS_PATH = DEBUG_PATH / "command_results.json"
FORMAL_COMMIT_PATH = DEBUG_PATH / "formal_commit.json"

SHANGHAI_OFFSET = timezone(timedelta(hours=8))
START_GATE = datetime(2026, 7, 21, 15, 0, 0, tzinfo=SHANGHAI_OFFSET)
ENTRY_DATE = "2026-06-23"
EXIT_DATE = "2026-07-21"
MIN_BENCHMARK_INDUSTRIES = 100
EXPECTED_QUARANTINED_HISTORY_CODES = frozenset({"801156"})
PRICE_DIRECTORY_RELATIVE = (
    "data_catalog/cache/industry_index/history/settlement_2026_07_21/second"
)
BASELINE_PRICE_DIRECTORY_RELATIVE = (
    "data_catalog/cache/industry_index/history/second"
)
EVENT_LEDGER_RELATIVE = "logs/v5_25_fund_flow_forward_ledger.jsonl"

CORE_SNAPSHOT_RELATIVE_PATHS = frozenset({
    EVENT_LEDGER_RELATIVE,
    "logs/v5_25_fund_flow_forward_ledger.csv",
    "logs/v5_25_fund_flow_forward_ledger_head_checkpoints.jsonl",
    "logs/v5_33_fund_flow_entry_price_freeze.jsonl",
    "logs/v5_33_fund_flow_entry_price_freeze_head_checkpoints.jsonl",
    "logs/v5_34_fund_flow_benchmark_entry_freeze.jsonl",
    "logs/v5_34_fund_flow_benchmark_entry_freeze_head_checkpoints.jsonl",
    "logs/v5_31_fund_flow_evidence_freeze_active.json",
    "logs/v5_31_fund_flow_evidence_freeze_history.jsonl",
    "logs/v5_31_fund_flow_evidence_freeze_history_head_checkpoints.jsonl",
    "outputs/audit/fund_flow_forward_ledger_integrity_v5_30/run_summary.json",
    "outputs/audit/current_state_consistency/run_summary.json",
    "outputs/audit/fund_flow_exploratory_settlement_price_refresh_2026_07_21/run_summary.json",
    "logs/v5_25_fund_flow_forward_sources/calendars/"
    "f348dd4c8863a5f2a5ff543a427c36198dbe3ca00f01a268f736490b2989d975.csv",
})
CORE_MANIFEST_RELATIVE_PATHS = CORE_SNAPSHOT_RELATIVE_PATHS | frozenset({
    "configs/fund_flow_forward_chain_policy.json",
    "configs/fund_flow_forward_ledger_schema.json",
    "scripts/settle_v5_27_fund_flow_forward_samples.py",
    "scripts/audit_fund_flow_exploratory_settlement_readiness.py",
    "scripts/refresh_fund_flow_exploratory_settlement_prices.py",
    "scripts/run_industry_index_research_validation.py",
    "scripts/fund_flow_exploratory_price_contract.py",
})
CHECKPOINT_LEDGER_RELATIVE_PATHS = {
    "observation_ledger": EVENT_LEDGER_RELATIVE,
    "candidate_entry_freeze": "logs/v5_33_fund_flow_entry_price_freeze.jsonl",
    "benchmark_entry_freeze": "logs/v5_34_fund_flow_benchmark_entry_freeze.jsonl",
    "cohort_history": "logs/v5_31_fund_flow_evidence_freeze_history.jsonl",
}

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


class ExploratoryDispositionError(RuntimeError):
    """Raised when a present formal disposition package fails closed validation."""


def artifact_presence(
    summary_path: Path = SUMMARY_PATH,
    dispositions_path: Path = DISPOSITIONS_PATH,
    *,
    require_final_commit: bool = True,
) -> dict[str, bool]:
    paths = package_artifact_paths(summary_path, dispositions_path)
    if not require_final_commit:
        paths.pop("formal_commit", None)
    return {name: path.is_file() for name, path in paths.items()}


def load_optional_disposition(
    active_cohort: Mapping[str, Any],
    *,
    summary_path: Path = SUMMARY_PATH,
    dispositions_path: Path = DISPOSITIONS_PATH,
    project_root: Path = ROOT,
    require_final_commit: bool = True,
) -> dict[str, Any] | None:
    """Load the formal four-row disposition, or return None when it does not exist.

    The package is optional until it is formally generated. Once either artifact is
    present, however, the pair is treated as an asserted current-state source and
    every contract below must pass; partial or edited packages raise instead of
    silently falling back to the old pending view.
    """

    presence = artifact_presence(
        summary_path,
        dispositions_path,
        require_final_commit=require_final_commit,
    )
    if not any(presence.values()):
        return None
    if not all(presence.values()):
        missing = [name for name, present in presence.items() if not present]
        raise ExploratoryDispositionError(
            f"formal exploratory disposition package is partial; missing={missing}"
        )

    try:
        summary_value = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExploratoryDispositionError(f"cannot read formal disposition summary: {exc}") from exc
    if not isinstance(summary_value, Mapping):
        raise ExploratoryDispositionError("formal disposition summary must be a JSON object")
    summary = dict(summary_value)

    try:
        with dispositions_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error) as exc:
        raise ExploratoryDispositionError(f"cannot read formal disposition rows: {exc}") from exc

    validate_summary(summary, active_cohort)
    validate_rows(rows)
    validate_summary_rows_consistency(summary, rows)
    validate_complete_package(
        summary,
        rows,
        active_cohort,
        summary_path=summary_path,
        dispositions_path=dispositions_path,
        project_root=project_root,
        require_final_commit=require_final_commit,
    )
    return normalized_disposition(summary, rows)


def package_artifact_paths(summary_path: Path, dispositions_path: Path) -> dict[str, Path]:
    package_root = summary_path.parent
    debug_root = dispositions_path.parent
    return {
        "report": package_root / "report.md",
        "summary": summary_path,
        "top_candidates": package_root / "top_candidates.csv",
        "dispositions": dispositions_path,
        "sha256_manifest": debug_root / "sha256_manifest.csv",
        "pre_snapshot": debug_root / "pre_settlement_snapshot.json",
        "post_snapshot": debug_root / "post_settlement_snapshot.json",
        "date_coverage_audit": debug_root / "date_coverage_audit.json",
        "command_results": debug_root / "command_results.json",
        "formal_commit": debug_root / "formal_commit.json",
    }


def validate_summary(summary: Mapping[str, Any], active_cohort: Mapping[str, Any]) -> None:
    expected_values: dict[str, Any] = {
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
        "required_start_at": "2026-07-21T15:00:00+08:00",
    }
    for field, expected in expected_values.items():
        actual = summary.get(field)
        if type(actual) is not type(expected) or actual != expected:
            raise ExploratoryDispositionError(
                f"formal disposition summary field {field!r} must be {expected!r}; got {actual!r}"
            )

    active_id = str(active_cohort.get("cohort_id", ""))
    active_hash = str(active_cohort.get("manifest_hash", ""))
    if active_cohort.get("freeze_passed") is not True or not active_id or len(active_hash) != 64:
        raise ExploratoryDispositionError("active cohort must be independently validated before disposition use")
    validate_sha256(active_hash, field="active cohort manifest_hash")
    summary_pair = (
        str(summary.get("active_cohort_id", "")),
        str(summary.get("active_cohort_manifest_hash", "")),
    )
    if summary_pair != (active_id, active_hash) or summary.get("active_cohort_validated") is not True:
        raise ExploratoryDispositionError(
            f"formal disposition active pair mismatch; summary={summary_pair}; active={(active_id, active_hash)}"
        )

    price_coverage = summary.get("price_coverage")
    if not isinstance(price_coverage, Mapping):
        raise ExploratoryDispositionError("formal disposition price_coverage must be an object")
    validate_price_coverage(price_coverage)

    observed_at = parse_gate_timestamp(summary.get("observed_at"), field="observed_at")
    generated_at = parse_gate_timestamp(summary.get("generated_at"), field="generated_at")
    if generated_at < observed_at:
        raise ExploratoryDispositionError(
            "formal disposition generated_at must not precede observed_at"
        )


def validate_rows(rows: list[dict[str, str]]) -> None:
    if len(rows) != len(EXPECTED):
        raise ExploratoryDispositionError(
            f"formal disposition must contain exactly four rows; got {len(rows)}"
        )
    by_id: dict[str, dict[str, str]] = {}
    for row in rows:
        observation_id = str(row.get("observation_id", ""))
        if not observation_id or observation_id in by_id:
            raise ExploratoryDispositionError(
                f"formal disposition observation_id is missing or duplicated: {observation_id!r}"
            )
        by_id[observation_id] = row
    if set(by_id) != set(EXPECTED):
        raise ExploratoryDispositionError(
            f"formal disposition observation allowlist mismatch; got={sorted(by_id)}"
        )

    for observation_id, (industry_code, industry_name) in EXPECTED.items():
        row = by_id[observation_id]
        expected_text = {
            "record_type": "exploratory_settlement_disposition",
            "industry_code": industry_code,
            "industry_name": industry_name,
            "signal_date": "2026-06-22",
            "planned_entry_date": "2026-06-23",
            "planned_exit_date": "2026-07-21",
            "sample_scope": "exploratory_fund_flow_only",
            "source_cohort_id": "legacy_exploratory_20260622",
            "source_cohort_manifest_hash": "UNVERIFIED_LEGACY_COHORT",
            "candidate_entry_freeze_status": "late_backfill_excluded",
            "benchmark_entry_freeze_status": "late_backfill_excluded",
            "disposition": "blocked",
            "disposition_status": "blocked_terminal_late_freeze_excluded",
        }
        for field, expected in expected_text.items():
            if str(row.get(field, "")) != expected:
                raise ExploratoryDispositionError(
                    f"row {observation_id} field {field!r} must be {expected!r}; got {row.get(field)!r}"
                )
        for field in ("qualified_for_goal", "integrity_eligible", "promotion_eligible"):
            if parse_csv_bool(row.get(field), observation_id=observation_id, field=field) is not False:
                raise ExploratoryDispositionError(f"row {observation_id} field {field!r} must remain false")
        if parse_csv_bool(row.get("record_contract_passed"), observation_id=observation_id, field="record_contract_passed") is not True:
            raise ExploratoryDispositionError(f"row {observation_id} record contract did not pass")
        if strict_int(
            row.get("benchmark_universe_count"),
            field="benchmark_universe_count",
            observation_id=observation_id,
        ) != 0:
            raise ExploratoryDispositionError(
                f"row {observation_id} benchmark_universe_count must remain 0 for the late freeze"
            )
        reason_codes = {
            item for item in str(row.get("reason_codes", "")).split("|") if item
        }
        required_reasons = {
            "candidate_entry_freeze_late_backfill_excluded",
            "benchmark_entry_freeze_late_backfill_excluded",
            "not_qualified_for_goal",
            "integrity_ineligible",
            "promotion_ineligible",
        }
        missing_reasons = sorted(required_reasons - reason_codes)
        if missing_reasons:
            raise ExploratoryDispositionError(
                f"row {observation_id} is missing terminal reason codes: {missing_reasons}"
            )
        nonempty_returns = [field for field in RETURN_FIELDS if str(row.get(field, "")).strip()]
        if nonempty_returns:
            raise ExploratoryDispositionError(
                f"row {observation_id} contains prohibited return fields: {nonempty_returns}"
            )


def validate_summary_rows_consistency(
    summary: Mapping[str, Any],
    rows: list[dict[str, str]],
) -> None:
    derived = {
        "record_count": len(rows),
        "settled_count": sum(row.get("disposition") == "settled" for row in rows),
        "blocked_count": sum(row.get("disposition") == "blocked" for row in rows),
        "pending_count": sum(row.get("disposition") == "pending" for row in rows),
        "qualified_settled_count": sum(
            row.get("disposition") == "settled"
            and parse_csv_bool(
                row.get("qualified_for_goal"),
                observation_id=str(row.get("observation_id", "")),
                field="qualified_for_goal",
            )
            for row in rows
        ),
    }
    mismatches = {
        field: (summary.get(field), value)
        for field, value in derived.items()
        if summary.get(field) != value
    }
    if mismatches:
        raise ExploratoryDispositionError(
            f"formal disposition summary/row counts disagree: {mismatches}"
        )


def validate_complete_package(
    summary: Mapping[str, Any],
    rows: list[dict[str, str]],
    active_cohort: Mapping[str, Any],
    *,
    summary_path: Path,
    dispositions_path: Path,
    project_root: Path,
    require_final_commit: bool,
) -> None:
    """Bind the normalized disposition to the complete formal evidence package.

    A summary/CSV pair is not sufficient proof.  This validator checks the
    public report and table, every required debug artifact, the pre/post source
    snapshots, their manifest hashes against the current repository, the exact
    materialized legacy observations, and the current exact-date price files.
    """

    paths = package_artifact_paths(summary_path, dispositions_path)
    top_rows = read_csv_artifact(paths["top_candidates"], label="top_candidates.csv")
    if top_rows != rows:
        raise ExploratoryDispositionError(
            "formal top_candidates.csv and debug settlement_dispositions.csv differ"
        )

    try:
        report = paths["report"].read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ExploratoryDispositionError(f"cannot read formal report: {exc}") from exc
    for required_text in (
        "# 四条探索性资金流记录终局处置",
        "research_only / NO_ACTION",
        "complete_terminal_exclusions",
        "settled `0` / blocked `4` / pending `0`",
        "qualified_settled_count=0",
    ):
        if required_text not in report:
            raise ExploratoryDispositionError(
                f"formal report is missing the required boundary text {required_text!r}"
            )

    coverage_audit = read_json_artifact(
        paths["date_coverage_audit"], label="date_coverage_audit.json"
    )
    if coverage_audit != dict(summary.get("price_coverage", {})):
        raise ExploratoryDispositionError(
            "formal date_coverage_audit.json differs from summary price_coverage"
        )

    command_results = read_json_artifact(
        paths["command_results"], label="command_results.json"
    )
    formal_commit: dict[str, Any] | None = None
    if require_final_commit:
        formal_commit = read_json_artifact(paths["formal_commit"], label="formal_commit.json")
        validate_formal_commit(
            formal_commit,
            paths=paths,
            summary=summary,
            active_cohort=active_cohort,
        )
    validate_command_results(
        command_results,
        require_final_commit=require_final_commit,
        formal_commit_validated=formal_commit is not None,
    )

    pre_snapshot = read_json_artifact(paths["pre_snapshot"], label="pre_settlement_snapshot.json")
    post_snapshot = read_json_artifact(paths["post_snapshot"], label="post_settlement_snapshot.json")
    manifest_rows = read_csv_artifact(paths["sha256_manifest"], label="sha256_manifest.csv")
    price_dir = project_root / Path(*PurePosixPath(PRICE_DIRECTORY_RELATIVE).parts)
    with price_cache_lock(price_dir):
        manifest = validate_manifest(manifest_rows, project_root=project_root)
        active_manifest = canonical_relative_path(
            active_cohort.get("manifest_path"), label="active cohort manifest_path"
        )
        missing_manifest_sources = sorted(
            (set(CORE_MANIFEST_RELATIVE_PATHS) | {active_manifest}) - set(manifest)
        )
        if missing_manifest_sources:
            raise ExploratoryDispositionError(
                f"formal evidence manifest is missing required sources: {missing_manifest_sources}"
            )
        validate_source_snapshots(
            pre_snapshot,
            post_snapshot,
            manifest,
            active_cohort=active_cohort,
            project_root=project_root,
            observed_at=parse_gate_timestamp(summary.get("observed_at"), field="observed_at"),
            expected_price_file_count=strict_int(
                summary.get("price_coverage", {}).get("source_file_count"),  # type: ignore[union-attr]
                field="source_file_count",
            ),
        )

        authoritative_rows = load_authoritative_observations(project_root)
        validate_rows_against_authoritative(rows, authoritative_rows)

        actual_coverage = scan_exact_date_coverage(price_dir)
        validate_coverage_matches_current(summary.get("price_coverage", {}), actual_coverage)
        validate_price_refresh_semantics(
            summary,
            project_root=project_root,
            price_dir=price_dir,
        )


def read_json_artifact(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExploratoryDispositionError(f"cannot read formal {label}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ExploratoryDispositionError(f"formal {label} must be a JSON object")
    return dict(value)


def read_csv_artifact(path: Path, *, label: str) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            if not fields or len(fields) != len(set(fields)):
                raise ExploratoryDispositionError(
                    f"formal {label} must have a non-empty, unique CSV header"
                )
            return [dict(row) for row in reader]
    except ExploratoryDispositionError:
        raise
    except (OSError, csv.Error) as exc:
        raise ExploratoryDispositionError(f"cannot read formal {label}: {exc}") from exc


def validate_command_results(
    payload: Mapping[str, Any],
    *,
    require_final_commit: bool = True,
    formal_commit_validated: bool = False,
) -> None:
    if payload.get("schema_version") == "fund-flow-exploratory-settlement-orchestration-v1":
        parse_gate_timestamp(payload.get("generated_at"), field="command_results.generated_at")
        expected = {
            "policy_status": "research_only",
            "current_action": "NO_ACTION",
            "authoritative_hashes_unchanged": True,
            "formal_package_committed": True,
            "compact_output_audit_passed": True,
        }
        require_exact_mapping(payload, expected, label="formal orchestration command_results")
        if require_final_commit and not formal_commit_validated:
            raise ExploratoryDispositionError(
                "formal orchestration command_results is not accompanied by a validated commit marker"
            )
        status = payload.get("completion_status")
        if status not in {
            "formal_commit_ready",
            "complete_terminal_exclusions",
            "orchestration_failed",
        }:
            raise ExploratoryDispositionError(
                "formal orchestration command_results is not at a committed post-compact phase"
            )
        if status == "formal_commit_ready":
            if (
                payload.get("chain_started") is not True
                or payload.get("chain_completed") is not False
                or payload.get("failure") is not None
            ):
                raise ExploratoryDispositionError(
                    "formal commit-ready command_results has an invalid chain state"
                )
            return
        if status == "orchestration_failed":
            if (
                payload.get("chain_started") is not True
                or payload.get("chain_completed") is not False
                or not isinstance(payload.get("failure"), Mapping)
            ):
                raise ExploratoryDispositionError(
                    "post-commit orchestration failure has an invalid fail-closed state"
                )
            return
        if payload.get("failure") is not None:
            raise ExploratoryDispositionError("final formal orchestration command_results contains a failure")
        if payload.get("chain_completed") is not True:
            raise ExploratoryDispositionError(
                "final formal orchestration command_results must have chain_completed=true"
            )
        require_exact_mapping(
            payload,
            {
                "active_pointer_governance_unchanged": True,
                "qualified_settled_count": 0,
                "promotion_ready": False,
                "can_claim_strong_rebound_industries": False,
                "manual_decision_support_ready": False,
                "production_ready": False,
                "auto_execution_allowed": False,
            },
            label="final formal orchestration command_results",
        )
        return

    if require_final_commit:
        raise ExploratoryDispositionError(
            "readiness-only command_results cannot satisfy the final formal commit contract"
        )
    require_exact_mapping(
        payload,
        {
            "audit_mode": "formal_disposition",
            "exit_data_read": True,
            "return_values_read_or_written": False,
            "authoritative_ledgers_mutated": False,
            "state_gate_passed": True,
        },
        label="formal readiness command_results",
    )


def validate_formal_commit(
    payload: Mapping[str, Any],
    *,
    paths: Mapping[str, Path],
    summary: Mapping[str, Any],
    active_cohort: Mapping[str, Any],
) -> None:
    require_exact_mapping(
        payload,
        {
            "schema_version": "fund-flow-exploratory-formal-commit-v1",
            "formal_disposition_committed": True,
            "completion_status": "complete_terminal_exclusions",
            "compact_output_audit_passed": True,
            "active_cohort_id": str(active_cohort.get("cohort_id", "")),
            "active_cohort_manifest_hash": str(active_cohort.get("manifest_hash", "")),
        },
        label="formal commit marker",
    )
    committed_at = parse_gate_timestamp(payload.get("committed_at"), field="formal_commit.committed_at")
    generated_at = parse_gate_timestamp(summary.get("generated_at"), field="generated_at")
    if committed_at < generated_at:
        raise ExploratoryDispositionError(
            "formal commit marker must not predate the formal disposition summary"
        )
    if payload.get("formal_summary_generated_at") != summary.get("generated_at"):
        raise ExploratoryDispositionError(
            "formal commit marker summary timestamp differs from the bound formal summary"
        )
    recorded_hashes = payload.get("artifact_sha256")
    if not isinstance(recorded_hashes, Mapping):
        raise ExploratoryDispositionError("formal commit marker artifact_sha256 must be an object")
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
    if set(recorded_hashes) != set(bound):
        raise ExploratoryDispositionError(
            "formal commit marker artifact set differs from the required immutable formal package"
        )
    for name, path in bound.items():
        recorded = validate_sha256(recorded_hashes.get(name), field=f"formal commit hash for {name}")
        if not path.is_file() or file_sha256(path) != recorded:
            raise ExploratoryDispositionError(f"formal commit marker hash drift for {name}")

    commands = payload.get("base_commands")
    if not isinstance(commands, list):
        raise ExploratoryDispositionError("formal commit marker base_commands must be a list")
    expected_ids = [
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
    ]
    actual_ids = [
        str(item.get("step_id", "")) if isinstance(item, Mapping) else ""
        for item in commands
    ]
    if actual_ids != expected_ids:
        raise ExploratoryDispositionError(
            f"formal commit marker base command order differs: {actual_ids}"
        )
    for item in commands:
        assert isinstance(item, Mapping)
        step_id = str(item.get("step_id", ""))
        required_true = (
            "exit_code_expected",
            "script_sha256_unchanged",
            "authoritative_hashes_unchanged",
            "active_pointer_governance_unchanged",
        )
        if any(item.get(field) is not True for field in required_true):
            raise ExploratoryDispositionError(
                f"formal commit marker base command {step_id!r} failed an execution invariant"
            )
        if not isinstance(item.get("semantic_validation"), Mapping) or item.get(
            "semantic_validation", {}
        ).get("passed") is not True:
            raise ExploratoryDispositionError(
                f"formal commit marker base command {step_id!r} lacks semantic validation"
            )
        validate_sha256(item.get("script_sha256"), field=f"base command {step_id} script_sha256")
    by_id = {str(item.get("step_id", "")): item for item in commands if isinstance(item, Mapping)}
    for step_id, item in by_id.items():
        expected_exit = 2 if step_id in {"v5_30_pre", "v5_30_post"} else 0
        if strict_int(item.get("exit_code"), field="exit_code", observation_id=step_id) != expected_exit:
            raise ExploratoryDispositionError(
                f"formal commit marker base command {step_id} must preserve exit code {expected_exit}"
            )
        if item.get("expected_exit_codes") != [expected_exit]:
            raise ExploratoryDispositionError(
                f"formal commit marker base command {step_id} has the wrong expected-exit contract"
            )

    v527 = by_id["v5_27"]
    command = v527.get("command")
    safe_suffix = ["--as-of-date", EXIT_DATE, "--read-only"]
    if (
        not isinstance(command, list)
        or len(command) < 6
        or Path(str(command[2])).name != "settle_v5_27_fund_flow_forward_samples.py"
        or [str(value) for value in command[-3:]] != safe_suffix
    ):
        raise ExploratoryDispositionError(
            "formal commit marker V5.27 command did not use the mandatory read-only audit mode"
        )
    semantic = v527.get("semantic_validation")
    evidence = semantic.get("evidence") if isinstance(semantic, Mapping) else None
    if not isinstance(evidence, Mapping):
        raise ExploratoryDispositionError(
            "formal commit marker V5.27 read-only semantic evidence is missing"
        )
    require_exact_mapping(
        evidence,
        {
            "execution_mode": "read_only_audit",
            "read_only": True,
            "proposed_settlement_count": 0,
            "event_ledger_write_invoked": False,
            "materialized_ledger_write_invoked": False,
            "checkpoint_write_invoked": False,
            "authoritative_ledger_files_unchanged": True,
        },
        label="formal commit marker V5.27 semantic evidence",
    )


def validate_manifest(
    rows: list[dict[str, str]],
    *,
    project_root: Path,
) -> dict[str, dict[str, Any]]:
    if not rows:
        raise ExploratoryDispositionError("formal sha256_manifest.csv must not be empty")
    root = project_root.resolve()
    manifest: dict[str, dict[str, Any]] = {}
    for row in rows:
        relative = canonical_relative_path(row.get("path"), label="manifest path")
        if relative in manifest:
            raise ExploratoryDispositionError(f"formal evidence manifest duplicates {relative!r}")
        path = resolve_relative_path(root, relative)
        if not path.is_file():
            raise ExploratoryDispositionError(f"formal evidence manifest source is missing: {relative}")
        size = strict_int(row.get("bytes"), field="bytes", observation_id=relative)
        sha = validate_sha256(row.get("sha256"), field=f"manifest sha256 for {relative}")
        actual_size = path.stat().st_size
        actual_sha = file_sha256(path)
        if (size, sha) != (actual_size, actual_sha):
            raise ExploratoryDispositionError(
                f"formal evidence manifest drift for {relative}: "
                f"recorded=({size},{sha}); current=({actual_size},{actual_sha})"
            )
        manifest[relative] = {"bytes": size, "sha256": sha}
    return manifest


def validate_source_snapshots(
    pre_snapshot: Mapping[str, Any],
    post_snapshot: Mapping[str, Any],
    manifest: Mapping[str, Mapping[str, Any]],
    *,
    active_cohort: Mapping[str, Any],
    project_root: Path,
    observed_at: datetime,
    expected_price_file_count: int,
) -> None:
    active_pair = (
        str(active_cohort.get("cohort_id", "")),
        str(active_cohort.get("manifest_hash", "")),
    )
    for phase, snapshot in (("pre", pre_snapshot), ("post", post_snapshot)):
        if snapshot.get("snapshot_phase") != phase:
            raise ExploratoryDispositionError(
                f"formal {phase} snapshot has the wrong snapshot_phase"
            )
        captured_at = parse_gate_timestamp(
            snapshot.get("captured_at"), field=f"{phase}_snapshot.captured_at"
        )
        if captured_at < observed_at:
            raise ExploratoryDispositionError(
                f"formal {phase} snapshot predates observed_at"
            )
        require_exact_mapping(
            snapshot,
            {
                "target_observation_count": 4,
                "active_cohort_id": active_pair[0],
                "active_cohort_manifest_hash": active_pair[1],
                "active_cohort_validated": True,
                "v5_30_active_cohort_id": active_pair[0],
                "v5_30_active_cohort_manifest_hash": active_pair[1],
                "v5_30_as_of_date": EXIT_DATE,
                "current_state_active_cohort_id": active_pair[0],
                "current_state_active_cohort_manifest_hash": active_pair[1],
                "calendar_dates_valid": True,
            },
            label=f"formal {phase} snapshot",
        )
        parse_gate_timestamp(
            snapshot.get("v5_30_generated_at"),
            field=f"{phase}_snapshot.v5_30_generated_at",
        )
        parse_gate_timestamp(
            snapshot.get("current_state_generated_at"),
            field=f"{phase}_snapshot.current_state_generated_at",
        )
        if strict_int(snapshot.get("global_observation_count"), field="global_observation_count") < 4:
            raise ExploratoryDispositionError(
                f"formal {phase} snapshot global_observation_count must be at least 4"
            )
        for field in ("candidate_freeze_count", "benchmark_freeze_count"):
            if strict_int(snapshot.get(field), field=field) < 4:
                raise ExploratoryDispositionError(
                    f"formal {phase} snapshot {field} must be at least 4"
                )
        validate_checkpoint_snapshot(
            snapshot.get("checkpoint_verification"),
            phase=phase,
            project_root=project_root,
        )
        validate_calendar_snapshot(snapshot.get("calendar_validation"), phase=phase)
        baseline_snapshot = snapshot.get("baseline_price_cache_snapshot")
        baseline_dir = project_root / Path(
            *PurePosixPath(BASELINE_PRICE_DIRECTORY_RELATIVE).parts
        )
        if not isinstance(baseline_snapshot, Mapping) or dict(
            baseline_snapshot
        ) != price_cache_snapshot(baseline_dir):
            raise ExploratoryDispositionError(
                f"formal {phase} snapshot mainline price cache differs from live files"
            )
        baseline_quarantine_hashes = snapshot.get(
            "baseline_quarantined_file_sha256"
        )
        live_baseline_quarantine_hashes = {
            code: file_sha256(baseline_dir / f"{code}.csv")
            for code in EXPECTED_QUARANTINED_HISTORY_CODES
            if (baseline_dir / f"{code}.csv").is_file()
        }
        if (
            not isinstance(baseline_quarantine_hashes, Mapping)
            or dict(baseline_quarantine_hashes)
            != live_baseline_quarantine_hashes
            or set(live_baseline_quarantine_hashes)
            != EXPECTED_QUARANTINED_HISTORY_CODES
        ):
            raise ExploratoryDispositionError(
                f"formal {phase} snapshot quarantined mainline file hashes differ from live files"
            )

    if post_snapshot.get("authoritative_hashes_unchanged") is not True:
        raise ExploratoryDispositionError(
            "formal post snapshot must assert authoritative_hashes_unchanged=true"
        )

    pre_hashes = normalize_snapshot_hashes(pre_snapshot.get("sensitive_file_sha256"), phase="pre")
    post_hashes = normalize_snapshot_hashes(post_snapshot.get("sensitive_file_sha256"), phase="post")
    if pre_hashes != post_hashes:
        raise ExploratoryDispositionError("formal pre/post authoritative snapshot hashes differ")

    active_manifest = canonical_relative_path(
        active_cohort.get("manifest_path"), label="active cohort manifest_path"
    )
    required = set(CORE_SNAPSHOT_RELATIVE_PATHS) | {active_manifest}
    missing_required = sorted(required - set(pre_hashes))
    if missing_required:
        raise ExploratoryDispositionError(
            f"formal authoritative snapshot is missing required paths: {missing_required}"
        )

    price_prefix = PRICE_DIRECTORY_RELATIVE.rstrip("/") + "/"
    price_paths = {
        key for key in pre_hashes
        if key.startswith(price_prefix) and key.lower().endswith(".csv")
    }
    current_price_paths = {
        path.relative_to(project_root.resolve()).as_posix()
        for path in sorted((project_root / Path(*PurePosixPath(PRICE_DIRECTORY_RELATIVE).parts)).glob("*.csv"))
    }
    if price_paths != current_price_paths or len(price_paths) != expected_price_file_count:
        raise ExploratoryDispositionError(
            "formal authoritative snapshot price-file set differs from current exact-coverage sources"
        )

    baseline_prefix = BASELINE_PRICE_DIRECTORY_RELATIVE.rstrip("/") + "/"
    baseline_paths = {
        key for key in pre_hashes
        if key.startswith(baseline_prefix) and key.lower().endswith(".csv")
    }
    current_baseline_paths = {
        path.relative_to(project_root.resolve()).as_posix()
        for path in sorted(
            (
                project_root
                / Path(*PurePosixPath(BASELINE_PRICE_DIRECTORY_RELATIVE).parts)
            ).glob("*.csv")
        )
    }
    if (
        baseline_paths != current_baseline_paths
        or len(baseline_paths) < MIN_BENCHMARK_INDUSTRIES
    ):
        raise ExploratoryDispositionError(
            "formal authoritative snapshot mainline price-file set differs from current baseline"
        )

    missing_manifest = sorted(set(pre_hashes) - set(manifest))
    if missing_manifest:
        raise ExploratoryDispositionError(
            f"formal evidence manifest omits snapshot paths: {missing_manifest}"
        )
    for relative, sha in pre_hashes.items():
        current = resolve_relative_path(project_root.resolve(), relative)
        if not current.is_file():
            raise ExploratoryDispositionError(
                f"formal authoritative snapshot source is missing: {relative}"
            )
        actual = file_sha256(current)
        manifest_sha = str(manifest[relative].get("sha256", ""))
        if sha != actual or manifest_sha != actual:
            raise ExploratoryDispositionError(
                f"formal authoritative snapshot drift for {relative}"
            )


def normalize_snapshot_hashes(value: Any, *, phase: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ExploratoryDispositionError(
            f"formal {phase} snapshot sensitive_file_sha256 must be a non-empty object"
        )
    normalized: dict[str, str] = {}
    for raw_path, raw_sha in value.items():
        relative = canonical_relative_path(raw_path, label=f"{phase} snapshot path")
        if relative in normalized:
            raise ExploratoryDispositionError(
                f"formal {phase} snapshot duplicates {relative!r}"
            )
        normalized[relative] = validate_sha256(
            raw_sha, field=f"{phase} snapshot sha256 for {relative}"
        )
    return normalized


def validate_checkpoint_snapshot(value: Any, *, phase: str, project_root: Path) -> None:
    if not isinstance(value, Mapping):
        raise ExploratoryDispositionError(
            f"formal {phase} snapshot checkpoint_verification must be an object"
        )
    minimum_counts = {
        "observation_ledger": 4,
        "candidate_entry_freeze": 4,
        "benchmark_entry_freeze": 4,
        "cohort_history": 1,
    }
    for key, minimum in minimum_counts.items():
        item = value.get(key)
        if not isinstance(item, Mapping):
            raise ExploratoryDispositionError(
                f"formal {phase} snapshot checkpoint {key!r} is missing"
            )
        if strict_int(item.get("event_count"), field="event_count", observation_id=key) < minimum:
            raise ExploratoryDispositionError(
                f"formal {phase} snapshot checkpoint {key!r} has too few events"
            )
        validate_sha256(item.get("file_sha256"), field=f"{key} checkpoint file_sha256")
        validate_sha256(item.get("head_hash"), field=f"{key} checkpoint head_hash")
        ledger_path = resolve_relative_path(
            project_root.resolve(), CHECKPOINT_LEDGER_RELATIVE_PATHS[key]
        )
        try:
            current = verify_ledger_checkpoint(ledger_path)
        except Exception as exc:
            raise ExploratoryDispositionError(
                f"current authoritative checkpoint {key!r} did not verify: {exc}"
            ) from exc
        comparison_fields = (
            "event_count",
            "head_hash",
            "file_sha256",
            "checkpoint_head_hash",
            "checkpoint_count",
        )
        mismatches = {
            field: (item.get(field), current.get(field))
            for field in comparison_fields
            if item.get(field) != current.get(field)
        }
        if mismatches:
            raise ExploratoryDispositionError(
                f"formal {phase} snapshot checkpoint {key!r} differs from current evidence: {mismatches}"
            )


def validate_calendar_snapshot(value: Any, *, phase: str) -> None:
    if not isinstance(value, Mapping):
        raise ExploratoryDispositionError(
            f"formal {phase} snapshot calendar_validation must be an object"
        )
    require_exact_mapping(
        value,
        {
            "checked": True,
            "calendar_dates_valid": True,
            "entry_date": ENTRY_DATE,
            "exit_date": EXIT_DATE,
            "entry_date_present": True,
            "exit_date_present": True,
        },
        label=f"formal {phase} calendar validation",
    )


def load_authoritative_observations(project_root: Path) -> list[dict[str, Any]]:
    ledger_path = resolve_relative_path(project_root.resolve(), EVENT_LEDGER_RELATIVE)
    try:
        verify_ledger_checkpoint(ledger_path)
        materialized = materialize_observations(read_events(ledger_path))
    except Exception as exc:
        raise ExploratoryDispositionError(
            f"current authoritative exploratory ledger did not verify: {exc}"
        ) from exc
    rows = [
        dict(row)
        for row in materialized
        if str(row.get("sample_scope", "")) == "exploratory_fund_flow_only"
    ]
    by_id = {str(row.get("observation_id", "")): row for row in rows}
    if len(by_id) != len(rows) or set(by_id) != set(EXPECTED):
        raise ExploratoryDispositionError(
            "current authoritative exploratory ledger must contain the exact four observation IDs"
        )
    for observation_id, (industry_code, industry_name) in EXPECTED.items():
        row = by_id[observation_id]
        expected = {
            "industry_code": industry_code,
            "industry_name": industry_name,
            "signal_date": "2026-06-22",
            "planned_entry_date": ENTRY_DATE,
            "planned_exit_date": EXIT_DATE,
            "sample_scope": "exploratory_fund_flow_only",
            "cohort_id": "legacy_exploratory_20260622",
            "cohort_manifest_hash": "UNVERIFIED_LEGACY_COHORT",
            "settlement_status": "not_due",
        }
        for field, expected_value in expected.items():
            if str(row.get(field, "")) != expected_value:
                raise ExploratoryDispositionError(
                    f"current ledger row {observation_id} field {field!r} changed"
                )
        for field in ("qualified_for_goal", "integrity_eligible", "promotion_eligible"):
            if parse_csv_bool(row.get(field), observation_id=observation_id, field=field) is not False:
                raise ExploratoryDispositionError(
                    f"current ledger row {observation_id} field {field!r} must remain false"
                )
        if any(str(row.get(field, "")).strip() for field in RETURN_FIELDS):
            raise ExploratoryDispositionError(
                f"current ledger row {observation_id} contains a prohibited return value"
            )
    return rows


def validate_rows_against_authoritative(
    dispositions: list[dict[str, str]],
    authoritative_rows: list[dict[str, Any]],
) -> None:
    source_by_id = {
        str(row.get("observation_id", "")): row for row in authoritative_rows
    }
    disposition_by_id = {
        str(row.get("observation_id", "")): row for row in dispositions
    }
    field_pairs = {
        "industry_code": "industry_code",
        "industry_name": "industry_name",
        "signal_date": "signal_date",
        "planned_entry_date": "planned_entry_date",
        "planned_exit_date": "planned_exit_date",
        "sample_scope": "sample_scope",
        "source_cohort_id": "cohort_id",
        "source_cohort_manifest_hash": "cohort_manifest_hash",
        "ledger_settlement_status": "settlement_status",
        "selection_score": "selection_score",
        "source_fingerprint_status": "source_fingerprint_status",
        "calendar_fingerprint": "calendar_fingerprint",
        "code_version": "code_version",
    }
    for observation_id in EXPECTED:
        disposition = disposition_by_id[observation_id]
        source = source_by_id[observation_id]
        for disposition_field, source_field in field_pairs.items():
            if str(disposition.get(disposition_field, "")) != str(source.get(source_field, "")):
                raise ExploratoryDispositionError(
                    f"formal disposition row {observation_id} is not bound to current ledger field "
                    f"{source_field!r}"
                )


def validate_price_coverage(coverage: Mapping[str, Any]) -> None:
    if coverage.get("checked") is not True:
        raise ExploratoryDispositionError("formal disposition price coverage must be checked")
    if coverage.get("exact_coverage_ready") is not True:
        raise ExploratoryDispositionError("formal disposition requires exact entry/exit price coverage")
    count_fields = (
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
    counts: dict[str, int] = {}
    for field in count_fields:
        value = coverage.get(field)
        if type(value) is not int:
            raise ExploratoryDispositionError(
                f"formal disposition {field} must be a JSON integer; got {value!r}"
            )
        counts[field] = value
    if counts["minimum_benchmark_industries"] != MIN_BENCHMARK_INDUSTRIES:
        raise ExploratoryDispositionError(
            "formal disposition minimum_benchmark_industries must equal 100"
        )
    if (
        counts["valid_source_file_count"] != counts["source_file_count"]
        or counts["invalid_file_count"] != 0
        or coverage.get("invalid_files") != []
    ):
        raise ExploratoryDispositionError(
            "formal disposition requires every price source file to pass identity/schema validation"
        )
    if coverage.get("price_values_retained") is not False:
        raise ExploratoryDispositionError(
            "formal disposition coverage audit must not retain price values"
        )
    for field in ("entry_industry_count", "exit_industry_count", "entry_exit_common_count"):
        if counts[field] < MIN_BENCHMARK_INDUSTRIES:
            raise ExploratoryDispositionError(
                f"formal disposition {field} must be at least {MIN_BENCHMARK_INDUSTRIES}"
            )
    common = counts["entry_exit_common_count"]
    if common > min(counts["entry_industry_count"], counts["exit_industry_count"]):
        raise ExploratoryDispositionError(
            "formal disposition entry_exit_common_count exceeds entry/exit coverage"
        )
    if counts["source_file_count"] < max(
        counts["entry_industry_count"], counts["exit_industry_count"]
    ):
        raise ExploratoryDispositionError(
            "formal disposition source_file_count is smaller than entry/exit coverage"
        )
    for field in ("candidate_entry_count", "candidate_exit_count", "candidate_common_count"):
        if counts[field] != len(EXPECTED):
            raise ExploratoryDispositionError(
                f"formal disposition {field} must equal {len(EXPECTED)}"
            )
    overall_max_date = str(coverage.get("overall_max_date", ""))
    if overall_max_date < EXIT_DATE:
        raise ExploratoryDispositionError(
            f"formal disposition overall_max_date must reach {EXIT_DATE}"
        )


def validate_price_refresh_semantics(
    summary: Mapping[str, Any],
    *,
    project_root: Path,
    price_dir: Path,
) -> None:
    """Recompute the append-only refresh gate instead of trusting its recorded hash alone."""

    import audit_fund_flow_exploratory_settlement_readiness as settlement_audit

    refresh_path = project_root / (
        "outputs/audit/fund_flow_exploratory_settlement_price_refresh_2026_07_21/"
        "run_summary.json"
    )
    refresh = read_json_artifact(refresh_path, label="price refresh run_summary.json")
    try:
        live_coverage = settlement_audit.scan_exact_date_coverage(price_dir)
        baseline_price_dir = (
            project_root / "data_catalog/cache/industry_index/history/second"
        )
        gate = settlement_audit.assess_price_source_gate(
            refresh,
            price_cache_snapshot(price_dir),
            live_coverage,
            price_cache_snapshot(baseline_price_dir),
            settlement_audit.quarantined_file_hashes(baseline_price_dir),
            project_root=project_root,
            settlement_price_dir=price_dir,
            baseline_price_dir=baseline_price_dir,
            producer_paths=(
                project_root
                / "scripts/refresh_fund_flow_exploratory_settlement_prices.py",
                project_root / "scripts/run_industry_index_research_validation.py",
                project_root / "scripts/fund_flow_exploratory_price_contract.py",
            ),
        )
    except Exception as exc:
        raise ExploratoryDispositionError(
            f"cannot independently recompute the formal price-refresh gate: {exc}"
        ) from exc
    if gate.get("all_passed") is not True:
        raise ExploratoryDispositionError(
            "formal price refresh is not committed with append-only continuity and matching attestations: "
            f"{gate.get('reason_codes', [])}"
        )
    if summary.get("price_source_gate_passed") is not True:
        raise ExploratoryDispositionError("formal summary price_source_gate_passed must be true")
    recorded_checks = summary.get("price_source_gate_checks")
    if not isinstance(recorded_checks, Mapping) or dict(recorded_checks) != dict(gate.get("checks", {})):
        raise ExploratoryDispositionError(
            "formal summary price_source_gate_checks differ from the independently recomputed gate"
        )
    if summary.get("price_source_gate_reason_codes") != []:
        raise ExploratoryDispositionError(
            "formal summary price_source_gate_reason_codes must be empty"
        )


def scan_exact_date_coverage(price_dir: Path) -> dict[str, Any]:
    # Keep one exact-date parser and identity/schema contract across the formal
    # generator, orchestrator, overlay, and CURRENT_STATUS consumers.  The
    # import stays lazy so the readiness module can continue importing this
    # shared disposition module in future refactors without an eager cycle.
    import audit_fund_flow_exploratory_settlement_readiness as settlement_audit

    try:
        scanned = settlement_audit.scan_exact_date_coverage(price_dir)
    except Exception as exc:
        raise ExploratoryDispositionError(
            f"cannot rescan current exact-date price sources: {exc}"
        ) from exc
    core_fields = (
        "checked",
        "source_file_count",
        "entry_industry_count",
        "exit_industry_count",
        "entry_exit_common_count",
        "candidate_entry_count",
        "candidate_exit_count",
        "candidate_common_count",
        "overall_max_date",
        "minimum_benchmark_industries",
        "exact_coverage_ready",
    )
    return {field: scanned.get(field) for field in core_fields}


def validate_coverage_matches_current(recorded: Any, actual: Mapping[str, Any]) -> None:
    if not isinstance(recorded, Mapping):
        raise ExploratoryDispositionError("formal price_coverage must be an object")
    fields = tuple(actual)
    mismatches = {
        field: (recorded.get(field), actual.get(field))
        for field in fields
        if recorded.get(field) != actual.get(field)
    }
    if mismatches:
        raise ExploratoryDispositionError(
            f"formal price coverage differs from current exact-date sources: {mismatches}"
        )


def canonical_relative_path(value: Any, *, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    candidate = PurePosixPath(text)
    if (
        not text
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.as_posix() != text
    ):
        raise ExploratoryDispositionError(f"formal {label} must be a canonical relative path; got {value!r}")
    return candidate.as_posix()


def resolve_relative_path(root: Path, relative: str) -> Path:
    path = (root / Path(*PurePosixPath(relative).parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ExploratoryDispositionError(
            f"formal evidence path escapes the project root: {relative!r}"
        ) from exc
    return path


def validate_sha256(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ExploratoryDispositionError(f"formal {field} must be a SHA-256 value")
    return text


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_exact_mapping(
    value: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    label: str,
) -> None:
    for field, expected_value in expected.items():
        actual = value.get(field)
        if type(actual) is not type(expected_value) or actual != expected_value:
            raise ExploratoryDispositionError(
                f"{label} field {field!r} must be {expected_value!r}; got {actual!r}"
            )


def normalized_disposition(
    summary: Mapping[str, Any],
    rows: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "artifact_present": True,
        "valid": True,
        "generated_at": str(summary.get("generated_at", "")),
        "completion_status": "complete_terminal_exclusions",
        "observation_count": 4,
        "settled_count": 0,
        "terminal_blocked_count": 4,
        "pending_count": 0,
        "qualified_settled_count": 0,
        "active_cohort_id": str(summary.get("active_cohort_id", "")),
        "active_cohort_manifest_hash": str(summary.get("active_cohort_manifest_hash", "")),
        "settlement_disposition_complete": True,
        "return_values_present": False,
        "rows": [dict(row) for row in rows],
    }


def pending_disposition(observation_count: int, *, error: str = "") -> dict[str, Any]:
    """Return the fail-closed pre-formal view without claiming completion."""

    return {
        "artifact_present": bool(error),
        "valid": False,
        "generated_at": "",
        "completion_status": "invalid_fail_closed" if error else "pending_formal_disposition",
        "observation_count": observation_count,
        "settled_count": 0,
        "terminal_blocked_count": 0,
        "pending_count": observation_count,
        "qualified_settled_count": 0,
        "active_cohort_id": "",
        "active_cohort_manifest_hash": "",
        "settlement_disposition_complete": False,
        "return_values_present": False,
        "error": error,
        "rows": [],
    }


def parse_csv_bool(value: Any, *, observation_id: str, field: str) -> bool:
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise ExploratoryDispositionError(
        f"row {observation_id} field {field!r} must be an explicit true/false value; got {value!r}"
    )


def strict_int(value: Any, *, field: str, observation_id: str = "summary") -> int:
    if isinstance(value, bool):
        raise ExploratoryDispositionError(
            f"{observation_id} field {field!r} must be an integer; got {value!r}"
        )
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ExploratoryDispositionError(
            f"{observation_id} field {field!r} must be an integer; got {value!r}"
        ) from exc
    if str(value).strip() != str(parsed):
        raise ExploratoryDispositionError(
            f"{observation_id} field {field!r} must be a canonical integer; got {value!r}"
        )
    return parsed


def parse_timestamp(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ExploratoryDispositionError(f"formal disposition {field} is required")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExploratoryDispositionError(
            f"formal disposition {field} must be ISO-8601; got {value!r}"
        ) from exc


def parse_gate_timestamp(value: Any, *, field: str) -> datetime:
    timestamp = parse_timestamp(value, field=field)
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(hours=8):
        raise ExploratoryDispositionError(
            f"formal disposition {field} must carry an explicit +08:00 timezone; got {value!r}"
        )
    normalized = timestamp.astimezone(SHANGHAI_OFFSET)
    if normalized < START_GATE:
        raise ExploratoryDispositionError(
            f"formal disposition {field} must be at or after {START_GATE.isoformat()}; got {value!r}"
        )
    return normalized
