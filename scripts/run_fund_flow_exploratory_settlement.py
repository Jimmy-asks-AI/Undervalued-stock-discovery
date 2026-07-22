#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import audit_fund_flow_exploratory_settlement_readiness as settlement_audit
import fund_flow_exploratory_disposition as disposition_contract
from fund_flow_forward_evidence import checkpoint_path_for
from research_integrity import atomic_write_json, atomic_write_text, file_sha256


ROOT = Path(__file__).resolve().parents[1]
AS_OF_DATE = settlement_audit.EXIT_DATE
FROZEN_V529_SHA256 = "7babd6a5fff591fa9dbbbccab4f5110b8f828284d016c6868523b88bf113e144"
FINAL_REQUIRED_DEBUG = (
    "pre_settlement_snapshot.json",
    "sha256_manifest.csv",
    "settlement_dispositions.csv",
    "command_results.json",
    "post_settlement_snapshot.json",
    "date_coverage_audit.json",
)
V529_INVARIANT_FIELDS = (
    "active_cohort_id",
    "active_cohort_manifest_hash",
    "active_cohort_validated",
    "ledger_rows",
    "global_history_ledger_rows",
    "global_history_settled_rows",
    "calendar_rows",
    "fail_count",
    "pending_count",
    "next_action_date",
    "next_action",
    "next_command",
    "promotion_ready",
    "goal_ready",
    "can_claim_strong_rebound_industries",
    "production_ready",
    "auto_execution_allowed",
    "best_status",
    "final_verdict",
)


class OrchestrationError(RuntimeError):
    """A fail-closed orchestration or evidence-contract failure."""


@dataclass(frozen=True)
class SettlementPaths:
    root: Path

    @property
    def scripts(self) -> Path:
        return self.root / "scripts"

    @property
    def price_dir(self) -> Path:
        return (
            self.root
            / "data_catalog"
            / "cache"
            / "industry_index"
            / "history"
            / "settlement_2026_07_21"
            / "second"
        )

    @property
    def baseline_price_dir(self) -> Path:
        return (
            self.root
            / "data_catalog"
            / "cache"
            / "industry_index"
            / "history"
            / "second"
        )

    @property
    def preflight_out(self) -> Path:
        return self.root / "outputs" / "audit" / "fund_flow_exploratory_settlement_preflight"

    @property
    def final_out(self) -> Path:
        return self.root / "outputs" / "audit" / "fund_flow_exploratory_settlement_2026_07_21"

    @property
    def v531_summary(self) -> Path:
        return self.root / "outputs" / "audit" / "fund_flow_evidence_freeze_manifest_v5_31" / "run_summary.json"

    @property
    def v530_summary(self) -> Path:
        return self.root / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30" / "run_summary.json"

    @property
    def v527_summary(self) -> Path:
        return self.root / "outputs" / "audit" / "fund_flow_forward_settlement_v5_27" / "run_summary.json"

    @property
    def v528_summary(self) -> Path:
        return self.root / "outputs" / "audit" / "fund_flow_promotion_evaluator_v5_28" / "run_summary.json"

    @property
    def v529_out(self) -> Path:
        return self.root / "outputs" / "audit" / "fund_flow_evidence_calendar_v5_29"

    @property
    def v529_summary(self) -> Path:
        return self.v529_out / "run_summary.json"

    @property
    def v535_summary(self) -> Path:
        return self.root / "outputs" / "audit" / "fund_flow_waiting_room_v5_35" / "run_summary.json"

    @property
    def current_state_summary(self) -> Path:
        return self.root / "outputs" / "audit" / "current_state_consistency" / "run_summary.json"

    @property
    def current_runner_summary(self) -> Path:
        return self.root / "outputs" / "etf_assisted_trading_current" / "run_summary.json"

    @property
    def current_status_summary(self) -> Path:
        return self.root / "outputs" / "audit" / "current_status" / "run_summary.json"

    @property
    def active_pointer(self) -> Path:
        return self.root / "logs" / "v5_31_fund_flow_evidence_freeze_active.json"

    @property
    def formal_commit(self) -> Path:
        return self.final_out / "debug" / "formal_commit.json"


@dataclass(frozen=True)
class CommandSpec:
    step_id: str
    script: Path
    arguments: tuple[str, ...]
    expected_exit_codes: tuple[int, ...]
    expected_exit_semantics: str

    def argv(self, python_executable: str) -> list[str]:
        return [python_executable, "-B", str(self.script), *self.arguments]


CommandExecutor = Callable[[CommandSpec], dict[str, Any]]
StepValidator = Callable[[str, SettlementPaths, Mapping[str, Any]], dict[str, Any]]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the deterministic, fail-closed terminal-disposition chain for the four "
            "legacy exploratory fund-flow observations."
        )
    )
    parser.add_argument(
        "--calendar-overlay-script",
        default="",
        help="Optional root-relative path to the V5.29 exploratory-disposition overlay.",
    )
    parser.add_argument(
        "--command-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for each child command; defaults to 600 seconds.",
    )
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    if args.command_timeout_seconds <= 0:
        parser.error("--command-timeout-seconds must be positive")

    paths = SettlementPaths(ROOT)
    overlay = resolve_overlay_script(paths, args.calendar_overlay_script or None)
    executor = subprocess_executor(
        root=paths.root,
        python_executable=sys.executable,
        timeout_seconds=args.command_timeout_seconds,
    )
    exit_code, result = orchestrate(
        paths,
        now=datetime.now(settlement_audit.SHANGHAI),
        overlay_script=overlay,
        executor=executor,
    )
    print(f"completion_status={result['completion_status']}")
    print(f"chain_started={str(result['chain_started']).lower()}")
    print(f"chain_completed={str(result['chain_completed']).lower()}")
    print(f"command_results={relative_path(result_path(paths, result), paths.root)}")
    if result.get("failure"):
        print(f"failure={result['failure']['message']}")
    return exit_code


def orchestrate(
    paths: SettlementPaths,
    *,
    now: datetime,
    overlay_script: Path,
    executor: CommandExecutor,
    coverage_scanner: Callable[[Path], dict[str, Any]] = settlement_audit.scan_exact_date_coverage,
    step_validator: StepValidator | None = None,
) -> tuple[int, dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("now must carry an explicit timezone")
    local_now = now.astimezone(settlement_audit.SHANGHAI)
    validator = step_validator or validate_step
    immutable_paths = authoritative_immutable_paths(paths)
    before_hashes = snapshot_hashes(immutable_paths, paths.root)
    active_pointer_before = read_json(paths.active_pointer, allow_missing=True)
    result: dict[str, Any] = {
        "schema_version": "fund-flow-exploratory-settlement-orchestration-v1",
        "generated_at": local_now.isoformat(timespec="seconds"),
        "as_of_date": AS_OF_DATE,
        "policy_status": "research_only",
        "current_action": "NO_ACTION",
        "completion_status": "orchestration_started",
        "chain_started": False,
        "chain_completed": False,
        "formal_package_committed": False,
        "compact_output_audit_passed": False,
        "price_coverage": {"checked": False},
        "authoritative_snapshot_scope": [relative_path(path, paths.root) for path in immutable_paths],
        "mutable_verification_pointer_excluded": relative_path(paths.active_pointer, paths.root),
        "active_pointer_before": active_pointer_before,
        "active_pointer_after": {},
        "active_pointer_governance_unchanged": False,
        "orchestrator_script": relative_path(Path(__file__), paths.root),
        "orchestrator_script_sha256": file_sha256(Path(__file__)),
        "authoritative_hashes_before": before_hashes,
        "authoritative_hashes_after": {},
        "authoritative_hashes_unchanged": False,
        "commands": [],
        "failure": None,
    }

    if local_now < settlement_audit.START_GATE:
        return pending_only(
            paths,
            result,
            executor=executor,
            before_hashes=before_hashes,
            step_id="readiness_pending_preclose",
            arguments=("--preflight",),
            expected_exit_codes=(0,),
            expected_semantics="pre-close preflight: no price file is opened and all four records remain pending",
            expected_completion_status="blocked_pre_start",
        )

    try:
        coverage = dict(coverage_scanner(paths.price_dir))
    except Exception as exc:  # pragma: no cover - exercised through the fail-closed result contract
        return fail_result(paths, result, before_hashes, "exact_price_coverage", exc)
    result["price_coverage"] = coverage
    if not exact_coverage_ready(coverage):
        return pending_only(
            paths,
            result,
            executor=executor,
            before_hashes=before_hashes,
            step_id="readiness_pending_exact_coverage",
            arguments=(),
            expected_exit_codes=(3,),
            expected_semantics=(
                "post-close exact-date coverage is incomplete: write only the canonical pending package "
                "and do not start the settlement chain"
            ),
            expected_completion_status="pending_exact_price_coverage",
        )

    # Every CSV inspected by the exact-coverage scanner is fixed for the whole
    # chain.  This closes the gap between pre-chain coverage and both formal
    # readiness audits, including newly added or removed cache files.
    price_files = sorted(paths.price_dir.glob("*.csv"))
    if len(price_files) != as_int(coverage.get("source_file_count")):
        return fail_result(
            paths,
            result,
            before_hashes,
            "exact_price_coverage",
            OrchestrationError("price file set changed immediately after the exact-coverage scan"),
        )
    immutable_paths = chain_immutable_paths(paths)
    before_hashes = snapshot_hashes(immutable_paths, paths.root)
    result["authoritative_snapshot_scope"] = [relative_path(path, paths.root) for path in immutable_paths]
    result["authoritative_hashes_before"] = before_hashes
    result["verified_price_file_count"] = len(price_files)
    result["chain_started"] = True
    context: dict[str, Any] = {}
    plan = build_command_plan(paths, overlay_script)
    try:
        for spec in plan:
            if spec.step_id == "v5_29_exploratory_overlay":
                context["v5_29_before_overlay"] = read_json(paths.v529_summary)
            if spec.step_id == "compact_output_audit":
                result["completion_status"] = "compact_output_audit_pending"
                result["authoritative_hashes_after"] = snapshot_hashes(chain_immutable_paths(paths), paths.root)
                result["authoritative_hashes_unchanged"] = result["authoritative_hashes_after"] == before_hashes
                write_command_results(paths.final_out, result)

            command_result = execute_one(spec, executor)
            result["commands"].append(command_result)
            if command_result.get("script_sha256_unchanged") is not True:
                raise OrchestrationError(f"{spec.step_id} script changed while it was executing")
            if not command_result["exit_code_expected"]:
                raise OrchestrationError(
                    f"{spec.step_id} returned {command_result['exit_code']}; expected {list(spec.expected_exit_codes)}"
                )
            evidence = validator(spec.step_id, paths, context)
            command_result["semantic_validation"] = {"passed": True, "evidence": evidence}
            if spec.step_id == "v5_29":
                context["v5_29_before_overlay"] = read_json(paths.v529_summary)

            current_hashes = snapshot_hashes(chain_immutable_paths(paths), paths.root)
            command_result["authoritative_hashes_unchanged"] = current_hashes == before_hashes
            if current_hashes != before_hashes:
                command_result["authoritative_hash_diff"] = hash_diff(before_hashes, current_hashes)
                raise OrchestrationError(
                    f"{spec.step_id} changed an immutable observation, freeze, checkpoint, or cohort-history artifact"
                )
            active_pointer_after = read_json(paths.active_pointer, allow_missing=True)
            pointer_unchanged = (
                active_pointer_governance(active_pointer_after)
                == active_pointer_governance(active_pointer_before)
            )
            command_result["active_pointer_governance_unchanged"] = pointer_unchanged
            command_result["active_pointer_verified_at_utc"] = {
                "before": str(active_pointer_before.get("verified_at_utc", "")),
                "after": str(active_pointer_after.get("verified_at_utc", "")),
            }
            if not pointer_unchanged:
                raise OrchestrationError(
                    f"{spec.step_id} changed active cohort governance fields beyond verified_at_utc"
                )

            if spec.step_id == "compact_output_audit":
                result.update({
                    "completion_status": "formal_commit_ready",
                    "formal_package_committed": True,
                    "compact_output_audit_passed": True,
                    "authoritative_hashes_after": current_hashes,
                    "authoritative_hashes_unchanged": True,
                    "active_pointer_after": active_pointer_after,
                    "active_pointer_governance_unchanged": True,
                })
                write_command_results(paths.final_out, result)
                write_formal_commit_marker(paths, result)
                validate_formal_artifacts(paths)

        final_evidence = validate_formal_artifacts(paths)
        after_hashes = snapshot_hashes(chain_immutable_paths(paths), paths.root)
        if after_hashes != before_hashes:
            raise OrchestrationError("authoritative immutable hashes changed after the command chain")
        active_pointer_after = read_json(paths.active_pointer, allow_missing=True)
        pointer_unchanged = (
            active_pointer_governance(active_pointer_after)
            == active_pointer_governance(active_pointer_before)
        )
        if not pointer_unchanged:
            raise OrchestrationError("active cohort governance fields changed during the command chain")
        result.update({
            "completion_status": "complete_terminal_exclusions",
            "completed_at": datetime.now(settlement_audit.SHANGHAI).isoformat(timespec="seconds"),
            "chain_completed": True,
            "authoritative_hashes_after": after_hashes,
            "authoritative_hashes_unchanged": True,
            "active_pointer_after": active_pointer_after,
            "active_pointer_governance_unchanged": True,
            "formal_package_committed": True,
            "compact_output_audit_passed": True,
            "final_artifact_validation": {"passed": True, "evidence": final_evidence},
            "qualified_settled_count": 0,
            "promotion_ready": False,
            "can_claim_strong_rebound_industries": False,
            "manual_decision_support_ready": False,
            "production_ready": False,
            "auto_execution_allowed": False,
        })
        write_command_results(paths.final_out, result)
        command_results_evidence = validate_command_results_file(paths, [spec.step_id for spec in plan])
        result["command_results_validation"] = {"passed": True, "evidence": command_results_evidence}
        write_command_results(paths.final_out, result)
        validate_command_results_file(paths, [spec.step_id for spec in plan])
        validate_formal_artifacts(paths)
        return 0, result
    except BaseException as exc:
        return fail_result(paths, result, before_hashes, current_step_id(result), exc)


def pending_only(
    paths: SettlementPaths,
    result: dict[str, Any],
    *,
    executor: CommandExecutor,
    before_hashes: Mapping[str, Any],
    step_id: str,
    arguments: tuple[str, ...],
    expected_exit_codes: tuple[int, ...],
    expected_semantics: str,
    expected_completion_status: str,
) -> tuple[int, dict[str, Any]]:
    spec = CommandSpec(
        step_id=step_id,
        script=paths.scripts / "audit_fund_flow_exploratory_settlement_readiness.py",
        arguments=arguments,
        expected_exit_codes=expected_exit_codes,
        expected_exit_semantics=expected_semantics,
    )
    try:
        command_result = execute_one(spec, executor)
        result["commands"].append(command_result)
        if command_result.get("script_sha256_unchanged") is not True:
            raise OrchestrationError(f"{step_id} script changed while it was executing")
        if not command_result["exit_code_expected"]:
            raise OrchestrationError(
                f"{step_id} returned {command_result['exit_code']}; expected {list(expected_exit_codes)}"
            )
        evidence = validate_pending_artifact(paths, expected_completion_status)
        command_result["semantic_validation"] = {"passed": True, "evidence": evidence}
        after_hashes = snapshot_hashes(authoritative_immutable_paths(paths), paths.root)
        command_result["authoritative_hashes_unchanged"] = after_hashes == before_hashes
        if after_hashes != before_hashes:
            command_result["authoritative_hash_diff"] = hash_diff(before_hashes, after_hashes)
            raise OrchestrationError("pending-only audit changed immutable authoritative evidence")
        active_pointer_after = read_json(paths.active_pointer, allow_missing=True)
        pointer_unchanged = (
            active_pointer_governance(active_pointer_after)
            == active_pointer_governance(result.get("active_pointer_before", {}))
        )
        if not pointer_unchanged:
            raise OrchestrationError("pending-only audit changed active cohort governance")
        command_result["active_pointer_governance_unchanged"] = True
        result.update({
            "completion_status": expected_completion_status,
            "completed_at": datetime.now(settlement_audit.SHANGHAI).isoformat(timespec="seconds"),
            "authoritative_hashes_after": after_hashes,
            "authoritative_hashes_unchanged": True,
            "active_pointer_after": active_pointer_after,
            "active_pointer_governance_unchanged": True,
            "pending_count": 4,
            "terminal_blocked_count": 0,
            "settled_count": 0,
            "qualified_settled_count": 0,
            "promotion_ready": False,
            "can_claim_strong_rebound_industries": False,
            "manual_decision_support_ready": False,
            "production_ready": False,
            "auto_execution_allowed": False,
        })
        write_command_results(paths.preflight_out, result)
        return 3, result
    except BaseException as exc:
        return fail_result(paths, result, before_hashes, step_id, exc)


def fail_result(
    paths: SettlementPaths,
    result: dict[str, Any],
    before_hashes: Mapping[str, Any],
    step_id: str,
    exc: BaseException,
) -> tuple[int, dict[str, Any]]:
    snapshot_paths = chain_immutable_paths(paths) if result.get("chain_started") else authoritative_immutable_paths(paths)
    after_hashes = snapshot_hashes(snapshot_paths, paths.root)
    active_pointer_after = read_json(paths.active_pointer, allow_missing=True)
    result.update({
        "completion_status": "orchestration_failed",
        "completed_at": datetime.now(settlement_audit.SHANGHAI).isoformat(timespec="seconds"),
        "chain_completed": False,
        "authoritative_hashes_after": after_hashes,
        "authoritative_hashes_unchanged": after_hashes == before_hashes,
        "active_pointer_after": active_pointer_after,
        "active_pointer_governance_unchanged": (
            active_pointer_governance(active_pointer_after)
            == active_pointer_governance(result.get("active_pointer_before", {}))
        ),
        "failure": {
            "step_id": step_id,
            "exception_type": type(exc).__name__,
            "message": str(exc),
        },
        "qualified_settled_count": 0,
        "promotion_ready": False,
        "can_claim_strong_rebound_industries": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    })
    if result.get("formal_package_committed") is True:
        result["downstream_invalidation"] = invalidate_downstream_completion(
            paths,
            reason=f"{step_id}: {type(exc).__name__}: {exc}",
        )
    out = paths.final_out if paths.final_out.joinpath("run_summary.json").is_file() else paths.preflight_out
    write_command_results(out, result)
    return 2, result


def invalidate_downstream_completion(paths: SettlementPaths, *, reason: str) -> dict[str, Any]:
    """Fail closed any published integration claim while preserving the committed formal core."""

    outcome: dict[str, Any] = {
        "attempted": True,
        "v5_29_invalidated": False,
        "current_status_invalidated": False,
        "errors": [],
    }
    try:
        if paths.v529_summary.is_file():
            summary = read_json(paths.v529_summary)
            summary.update({
                "exploratory_disposition_artifact_present": True,
                "exploratory_disposition_valid": False,
                "exploratory_completion_status": "integration_failed_fail_closed",
                "exploratory_observation_count": 4,
                "exploratory_settled_count": 0,
                "exploratory_terminal_blocked_count": 0,
                "exploratory_pending_count": 4,
                "exploratory_qualified_settled_count": 0,
                "exploratory_calendar_row_count": 0,
                "exploratory_disposition_error": reason,
            })
            atomic_write_json(paths.v529_summary, summary)
            notice = (
                "# V5.29 探索处置覆盖层｜失败关闭\n\n"
                "正式核心包已经提交，但后续集成未完整完成。"
                "四条探索记录暂按 pending 处理，继续保持 `research_only / NO_ACTION`。\n\n"
                f"原因：`{reason}`\n"
            )
            atomic_write_text(paths.v529_out / "report.md", notice)
            atomic_write_text(
                paths.v529_out / "debug" / "exploratory_disposition_calendar.csv",
                "observation_id,disposition_status,error\n",
            )
            outcome["v5_29_invalidated"] = True
    except BaseException as invalidation_exc:
        outcome["errors"].append(
            f"v5_29:{type(invalidation_exc).__name__}:{invalidation_exc}"
        )

    try:
        summary_path = paths.current_status_summary
        if summary_path.is_file():
            summary = read_json(summary_path)
            sample_counts = summary.get("sample_counts")
            if not isinstance(sample_counts, Mapping):
                sample_counts = {}
            updated_counts = dict(sample_counts)
            updated_counts.update({
                "exploratory_fund_flow_settled": 0,
                "exploratory_fund_flow_terminal_blocked": 0,
                "exploratory_fund_flow_pending": 4,
                "exploratory_fund_flow_qualified_settled": 0,
            })
            summary.update({
                "status_valid": False,
                "state_source_consistency_passed": False,
                "exploratory_disposition_valid": False,
                "exploratory_completion_status": "integration_failed_fail_closed",
                "exploratory_settled_count": 0,
                "exploratory_terminal_blocked_count": 0,
                "exploratory_pending_count": 4,
                "exploratory_qualified_settled_count": 0,
                "exploratory_disposition_error": reason,
                "sample_counts": updated_counts,
                "final_verdict": "探索处置下游集成失败；状态页已失效并恢复为 pending / NO_ACTION。",
            })
            fail_count = as_int(summary.get("fail_count"))
            summary["fail_count"] = max(1, fail_count if fail_count >= 0 else 1)
            atomic_write_json(summary_path, summary)
            notice = (
                "# CURRENT_STATUS｜失败关闭\n\n"
                "探索处置的下游集成未完整完成；本页已主动失效。"
                "四条记录暂按 pending 处理，继续保持 `research_only / NO_ACTION`。\n\n"
                f"原因：`{reason}`\n"
            )
            atomic_write_text(paths.root / "CURRENT_STATUS.md", notice)
            atomic_write_text(summary_path.parent / "report.md", notice)
            outcome["current_status_invalidated"] = True
    except BaseException as invalidation_exc:
        outcome["errors"].append(
            f"current_status:{type(invalidation_exc).__name__}:{invalidation_exc}"
        )
    outcome["all_succeeded"] = not outcome["errors"]
    return outcome


def build_command_plan(paths: SettlementPaths, overlay_script: Path) -> list[CommandSpec]:
    script = paths.scripts.__truediv__
    final_relative = relative_path(paths.final_out, paths.root)
    return [
        command("v5_31", script("build_v5_31_fund_flow_evidence_freeze_manifest.py"), (), (0,), "active immutable cohort baseline revalidates without creating or updating a cohort"),
        command("v5_30_pre", script("audit_v5_30_fund_flow_forward_ledger_integrity.py"), ("--as-of-date", AS_OF_DATE), (2,), "expected fail-closed result: active rows 0, global rows 4, global late backfills 8, claim disabled"),
        command("v5_27", script("settle_v5_27_fund_flow_forward_samples.py"), ("--as-of-date", AS_OF_DATE, "--read-only"), (0,), "read-only audit confirms the active cohort remains empty and no authoritative settlement artifact is written"),
        command("v5_30_post", script("audit_v5_30_fund_flow_forward_ledger_integrity.py"), ("--as-of-date", AS_OF_DATE), (2,), "expected fail-closed result remains explicit after V5.27; exit 2 is not converted to pass"),
        command("v5_28", script("build_v5_28_fund_flow_promotion_evaluator.py"), (), (0,), "promotion remains false with zero qualified settled observations"),
        command("current_state_pre", script("audit_current_state_consistency.py"), (), (0,), "pre-disposition state sources agree on the validated active pair and NO_ACTION boundary"),
        command("readiness_initial", script("audit_fund_flow_exploratory_settlement_readiness.py"), (), (0,), "first canonical formal four-record terminal-disposition artifact is written"),
        command("v5_29", script("build_v5_29_fund_flow_evidence_calendar.py"), ("--as-of-date", AS_OF_DATE), (0,), "frozen V5.29 active calendar is rebuilt unchanged in scope"),
        command("v5_35", script("build_v5_35_fund_flow_waiting_room.py"), ("--as-of-date", AS_OF_DATE), (0,), "waiting room remains non-tradeable and claim-disabled"),
        command("current_state_post", script("audit_current_state_consistency.py"), (), (0,), "post-chain state sources remain consistent and fail closed before the final formal package"),
        command("readiness_final", script("audit_fund_flow_exploratory_settlement_readiness.py"), (), (0,), "canonical formal artifact is rebuilt against the post-chain current-state snapshot"),
        command(
            "compact_output_audit",
            script("audit_compact_output_layout.py"),
            ("--output-dir", final_relative, "--required-debug-files", *FINAL_REQUIRED_DEBUG),
            (0,),
            "formal output obeys the compact four-part contract and contains every required debug artifact",
        ),
        command("v5_29_exploratory_overlay", overlay_script, (), (0,), "after formal commit, overlay the independent exploratory terminal disposition without changing active V5.29 fields"),
        command("build_current_status", script("build_current_status.py"), (), (0,), "after formal commit, CURRENT_STATUS records 4 terminal blocked, 0 pending, 0 settled, and preserves NO_ACTION"),
    ]


def command(
    step_id: str,
    script: Path,
    arguments: Sequence[str],
    expected_exit_codes: Sequence[int],
    semantics: str,
) -> CommandSpec:
    return CommandSpec(step_id, script, tuple(arguments), tuple(expected_exit_codes), semantics)


def subprocess_executor(
    *,
    root: Path,
    python_executable: str,
    timeout_seconds: int,
) -> CommandExecutor:
    def execute(spec: CommandSpec) -> dict[str, Any]:
        argv = spec.argv(python_executable)
        started = datetime.now(settlement_audit.SHANGHAI)
        monotonic_start = time.monotonic()
        env = dict(os.environ)
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        try:
            completed = subprocess.run(
                argv,
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = int(completed.returncode)
            stdout = completed.stdout
            stderr = completed.stderr
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            stdout = text_value(exc.stdout)
            stderr = text_value(exc.stderr) + f"\ncommand timed out after {timeout_seconds} seconds"
            timed_out = True
        finished = datetime.now(settlement_audit.SHANGHAI)
        return {
            "step_id": spec.step_id,
            "command": argv,
            "command_display": subprocess.list2cmdline(argv),
            "working_directory": str(root),
            "started_at": started.isoformat(timespec="milliseconds"),
            "finished_at": finished.isoformat(timespec="milliseconds"),
            "duration_ms": round((time.monotonic() - monotonic_start) * 1000),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": stderr,
        }

    return execute


def execute_one(spec: CommandSpec, executor: CommandExecutor) -> dict[str, Any]:
    script_exists = spec.script.is_file()
    script_sha = file_sha256(spec.script) if script_exists else "MISSING"
    wrapper_started = datetime.now(settlement_audit.SHANGHAI)
    monotonic_start = time.monotonic()
    raw = dict(executor(spec))
    wrapper_finished = datetime.now(settlement_audit.SHANGHAI)
    script_exists_after = spec.script.is_file()
    script_sha_after = file_sha256(spec.script) if script_exists_after else "MISSING"
    if "exit_code" not in raw:
        raise OrchestrationError(f"executor did not return an exit_code for {spec.step_id}")
    exit_code = int(raw["exit_code"])
    raw.update({
        "step_id": spec.step_id,
        "expected_exit_codes": list(spec.expected_exit_codes),
        "expected_exit_semantics": spec.expected_exit_semantics,
        "exit_code_expected": exit_code in spec.expected_exit_codes,
        "script_path": str(spec.script.resolve()),
        "script_bytes": spec.script.stat().st_size if script_exists else 0,
        "script_sha256": script_sha,
        "script_sha256_after": script_sha_after,
        "script_sha256_unchanged": script_sha_after == script_sha,
    })
    raw.setdefault("stdout", "")
    raw.setdefault("stderr", "")
    raw.setdefault("command", spec.argv(sys.executable))
    raw.setdefault("command_display", subprocess.list2cmdline(spec.argv(sys.executable)))
    raw.setdefault("working_directory", str(spec.script.resolve().parents[1]))
    raw.setdefault("started_at", wrapper_started.isoformat(timespec="milliseconds"))
    raw.setdefault("finished_at", wrapper_finished.isoformat(timespec="milliseconds"))
    raw.setdefault("duration_ms", round((time.monotonic() - monotonic_start) * 1000))
    raw.setdefault("timed_out", False)
    return raw


def validate_step(
    step_id: str,
    paths: SettlementPaths,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    if step_id == "v5_31":
        return validate_v531(read_json(paths.v531_summary), read_json(paths.active_pointer))
    if step_id in {"v5_30_pre", "v5_30_post"}:
        return validate_v530_fail_closed(read_json(paths.v530_summary), read_json(paths.active_pointer))
    if step_id == "v5_27":
        return validate_v527(read_json(paths.v527_summary), read_json(paths.active_pointer))
    if step_id == "v5_28":
        return validate_v528(read_json(paths.v528_summary), read_json(paths.active_pointer))
    if step_id in {"current_state_pre", "current_state_post"}:
        return validate_current_state(
            read_json(paths.current_state_summary),
            read_json(paths.active_pointer),
            read_json(paths.current_runner_summary),
        )
    if step_id in {"readiness_initial", "readiness_final"}:
        return validate_formal_artifacts(paths, require_final_commit=False)
    if step_id == "v5_29":
        return validate_v529_base(
            read_json(paths.v529_summary),
            read_json(paths.active_pointer),
            paths.scripts / "build_v5_29_fund_flow_evidence_calendar.py",
        )
    if step_id == "v5_29_exploratory_overlay":
        before = context.get("v5_29_before_overlay")
        if not isinstance(before, Mapping):
            raise OrchestrationError("V5.29 pre-overlay snapshot is missing")
        return validate_v529_overlay(
            before,
            read_json(paths.v529_summary),
            paths.v529_out / "debug" / "exploratory_disposition_calendar.csv",
        )
    if step_id == "v5_35":
        return validate_v535(read_json(paths.v535_summary), read_json(paths.active_pointer))
    if step_id == "build_current_status":
        return validate_current_status(read_json(paths.current_status_summary))
    if step_id == "compact_output_audit":
        return {"compact_output_layout": "pass"}
    raise OrchestrationError(f"no semantic validator is registered for {step_id}")


def validate_v531(summary: Mapping[str, Any], active: Mapping[str, Any]) -> dict[str, Any]:
    require(summary.get("freeze_passed") is True, "V5.31 freeze_passed must be true")
    require(as_int(summary.get("changed_count")) == 0, "V5.31 changed_count must be 0")
    require(as_int(summary.get("missing_artifact_count")) == 0, "V5.31 missing_artifact_count must be 0")
    require(summary.get("baseline_created") is False, "V5.31 must not create a cohort during settlement")
    require(summary.get("verification_required") is False, "V5.31 verification_required must be false")
    validate_pair(summary, active, summary_hash_field="manifest_hash")
    validate_boundary(summary, require_goal=False)
    return {"freeze_passed": True, "changed_count": 0, "missing_artifact_count": 0}


def validate_v530_fail_closed(summary: Mapping[str, Any], active: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "as_of_date": AS_OF_DATE,
        "ledger_rows": 0,
        "global_ledger_rows": 4,
        "global_late_backfill_count": 8,
        "integrity_passed": False,
        "global_ledger_integrity_passed": False,
        "can_claim_strong_rebound_industries": False,
        "goal_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_ledger_integrity_failed",
    }
    require_exact_fields(summary, expected, "V5.30")
    require(as_int(summary.get("global_violation_count")) > 0, "V5.30 global violations must remain explicit")
    validate_pair(summary, active)
    validate_all_ready_fields_false(summary, "V5.30")
    return {
        "expected_fail_closed": True,
        "active_ledger_rows": 0,
        "global_ledger_rows": 4,
        "global_late_backfill_count": 8,
        "strong_industry_claim": False,
    }


def validate_v527(summary: Mapping[str, Any], active: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "as_of_date": AS_OF_DATE,
        "execution_mode": "read_only_audit",
        "read_only": True,
        "proposed_settlement_count": 0,
        "event_ledger_write_invoked": False,
        "materialized_ledger_write_invoked": False,
        "checkpoint_write_invoked": False,
        "authoritative_ledger_files_unchanged": True,
        "ledger_rows": 0,
        "global_history_ledger_rows": 4,
        "settled_rows": 0,
        "pending_rows": 0,
        "qualified_settled_rows": 0,
        "exploratory_settled_rows": 0,
        "input_integrity_passed": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    }
    require_exact_fields(summary, expected, "V5.27")
    validate_pair(summary, active)
    require(summary.get("mean_realized_relative_return") is None, "V5.27 must not expose a mean return")
    require(summary.get("top_quintile_hit_rate") is None, "V5.27 must not expose a hit rate")
    return {
        "settled_rows": 0,
        "authoritative_event_append_expected": False,
        "execution_mode": "read_only_audit",
        "read_only": True,
        "proposed_settlement_count": 0,
        "event_ledger_write_invoked": False,
        "materialized_ledger_write_invoked": False,
        "checkpoint_write_invoked": False,
        "authoritative_ledger_files_unchanged": True,
    }


def validate_v528(summary: Mapping[str, Any], active: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "settled_batch_count": 0,
        "settled_industry_count": 0,
        "global_history_qualified_settled_rows": 0,
        "integrity_passed": False,
        "promotion_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    }
    require_exact_fields(summary, expected, "V5.28")
    validate_pair(summary, active)
    return {"promotion_ready": False, "qualified_settled_rows": 0}


def validate_current_state(
    summary: Mapping[str, Any],
    active: Mapping[str, Any],
    current_runner: Mapping[str, Any],
) -> dict[str, Any]:
    current_as_of = str(current_runner.get("as_of_date", ""))
    require(bool(current_as_of), "current runner as_of_date is missing")
    expected = {
        "current_as_of_date": current_as_of,
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
    require_exact_fields(summary, expected, "current_state_consistency")
    require(
        strict_shanghai_timestamp(summary.get("generated_at"), expected_date=AS_OF_DATE) is not None,
        "current_state_consistency must be rerun on 2026-07-21 with a timezone-aware Shanghai timestamp "
        "without changing the main decision as-of",
    )
    validate_pair(summary, active)
    validate_all_ready_fields_false(summary, "current_state_consistency")
    return {
        "state_consistent": True,
        "fail_count": 0,
        "current_action": "NO_ACTION",
        "current_as_of_date": current_as_of,
        "audit_generated_on": AS_OF_DATE,
    }


def validate_v529_base(
    summary: Mapping[str, Any],
    active: Mapping[str, Any],
    script_path: Path,
) -> dict[str, Any]:
    expected = {
        "as_of_date": AS_OF_DATE,
        "ledger_rows": 0,
        "global_history_ledger_rows": 4,
        "global_history_settled_rows": 0,
        "promotion_ready": False,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    }
    require_exact_fields(summary, expected, "V5.29")
    validate_pair(summary, active)
    actual_sha = file_sha256(script_path)
    require(actual_sha == FROZEN_V529_SHA256, f"frozen V5.29 script hash changed: {actual_sha}")
    return {
        "active_calendar_rows": as_int(summary.get("calendar_rows")),
        "exploratory_overlay_present": False,
        "frozen_script_sha256": actual_sha,
    }


def validate_v529_overlay(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    sidecar_path: Path,
) -> dict[str, Any]:
    before_active = {field: before.get(field) for field in V529_INVARIANT_FIELDS}
    after_active = {field: after.get(field) for field in V529_INVARIANT_FIELDS}
    require(before_active == after_active, f"V5.29 overlay changed active fields: {mapping_diff(before_active, after_active)}")
    expected = {
        "exploratory_disposition_artifact_present": True,
        "exploratory_disposition_valid": True,
        "exploratory_completion_status": "complete_terminal_exclusions",
        "exploratory_observation_count": 4,
        "exploratory_settled_count": 0,
        "exploratory_terminal_blocked_count": 4,
        "exploratory_pending_count": 0,
        "exploratory_qualified_settled_count": 0,
        "exploratory_calendar_rows": 1,
        "exploratory_evidence_scope": "exploratory_fund_flow_only",
        "exploratory_disposition_error": "",
    }
    require_exact_fields(after, expected, "V5.29 exploratory overlay")
    require(bool(str(after.get("exploratory_disposition_generated_at", "")).strip()), "overlay generated_at is missing")
    rows = read_csv(sidecar_path)
    require(len(rows) == 1, "V5.29 exploratory sidecar must contain exactly one row")
    require_exact_fields(
        rows[0],
        {
            "status": "completed_terminal_exclusions",
            "evidence_scope": "exploratory_fund_flow_only",
            "row_count": "4",
        },
        "V5.29 exploratory sidecar",
    )
    return {"active_fields_unchanged": True, "exploratory_calendar_rows": 1, "terminal_blocked": 4}


def validate_v535(summary: Mapping[str, Any], active: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "as_of_date": AS_OF_DATE,
        "observation_rows": 0,
        "global_history_observation_rows": 4,
        "global_history_settled_rows": 0,
        "current_tradeable": False,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    }
    require_exact_fields(summary, expected, "V5.35")
    validate_pair(summary, active)
    return {"current_tradeable": False, "strong_industry_claim": False}


def validate_current_status(summary: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "current_date": AS_OF_DATE,
        "status_valid": True,
        "fail_count": 0,
        "policy_status": "research_only",
        "current_action": "NO_ACTION",
        "strong_industry_alpha_validated": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "exploratory_disposition_artifact_present": True,
        "exploratory_disposition_valid": True,
        "exploratory_completion_status": "complete_terminal_exclusions",
        "exploratory_observation_count": 4,
        "exploratory_settled_count": 0,
        "exploratory_terminal_blocked_count": 4,
        "exploratory_pending_count": 0,
        "exploratory_qualified_settled_count": 0,
        "exploratory_disposition_error": "",
    }
    require_exact_fields(summary, expected, "CURRENT_STATUS")
    counts = summary.get("sample_counts")
    require(isinstance(counts, Mapping), "CURRENT_STATUS sample_counts is missing")
    require_exact_fields(
        counts,
        {
            "exploratory_fund_flow_observations": 4,
            "active_fund_flow_settled_qualified": 0,
            "exploratory_fund_flow_settled": 0,
            "exploratory_fund_flow_terminal_blocked": 4,
            "exploratory_fund_flow_pending": 0,
            "exploratory_fund_flow_qualified_settled": 0,
        },
        "CURRENT_STATUS sample counts",
    )
    return {
        "status_valid": True,
        "current_action": "NO_ACTION",
        "exploratory_observations": 4,
        "terminal_blocked": 4,
        "pending": 0,
        "settled": 0,
    }


def validate_pending_artifact(paths: SettlementPaths, expected_status: str) -> dict[str, Any]:
    summary = read_json(paths.preflight_out / "run_summary.json")
    require_exact_fields(
        summary,
        {
            "completion_status": expected_status,
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
        },
        "pending settlement audit",
    )
    return {"completion_status": expected_status, "pending_count": 4, "chain_started": False}


def validate_formal_artifacts(
    paths: SettlementPaths,
    *,
    require_final_commit: bool = True,
) -> dict[str, Any]:
    out = paths.final_out
    for name in ("report.md", "run_summary.json", "top_candidates.csv"):
        require(out.joinpath(name).is_file(), f"formal artifact is missing: {name}")
    require(out.joinpath("debug").is_dir(), "formal debug directory is missing")
    for name in FINAL_REQUIRED_DEBUG:
        require(out.joinpath("debug", name).is_file(), f"formal debug artifact is missing: {name}")

    summary = read_json(out / "run_summary.json")
    expected_summary = {
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
        "all_return_fields_empty": True,
        "state_gate_passed": True,
        "active_cohort_gate_passed": True,
        "v5_30_summary_gate_passed": True,
        "current_state_consistency_gate_passed": True,
        "price_source_gate_passed": True,
        "promotion_ready": False,
        "can_claim_strong_rebound_industries": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    }
    require_exact_fields(summary, expected_summary, "formal settlement summary")
    coverage = summary.get("price_coverage")
    require(isinstance(coverage, Mapping) and exact_coverage_ready(coverage), "formal exact-date price coverage is not ready")

    active = read_json(paths.active_pointer)
    try:
        normalized = disposition_contract.load_optional_disposition(
            active,
            summary_path=out / "run_summary.json",
            dispositions_path=out / "debug" / "settlement_dispositions.csv",
            project_root=paths.root,
            require_final_commit=require_final_commit,
        )
    except disposition_contract.ExploratoryDispositionError as exc:
        raise OrchestrationError(f"shared formal disposition contract failed: {exc}") from exc
    require(normalized is not None and normalized.get("valid") is True, "shared formal disposition contract did not load")

    rows = read_csv(out / "top_candidates.csv")
    debug_rows = read_csv(out / "debug" / "settlement_dispositions.csv")
    require(rows == debug_rows, "top_candidates.csv and debug settlement dispositions differ")
    require(len(rows) == 4, "formal disposition table must contain exactly four rows")
    expected_ids = set(settlement_audit.EXPECTED)
    require({row.get("observation_id", "") for row in rows} == expected_ids, "formal disposition allowlist differs")
    for row in rows:
        require(row.get("disposition") == "blocked", "each exploratory record must be terminal blocked")
        require(
            row.get("disposition_status") == "blocked_terminal_late_freeze_excluded",
            "each exploratory record must retain the late-freeze terminal status",
        )
        require(row.get("sample_scope") == "exploratory_fund_flow_only", "sample scope changed")
        for field in ("qualified_for_goal", "integrity_eligible", "promotion_eligible"):
            require(is_false_text(row.get(field)), f"{field} must remain false")
        require(row.get("candidate_entry_freeze_status") == "late_backfill_excluded", "candidate freeze status changed")
        require(row.get("benchmark_entry_freeze_status") == "late_backfill_excluded", "benchmark freeze status changed")
        for field in settlement_audit.RETURN_FIELDS:
            require(not str(row.get(field, "")).strip(), f"return field {field} must remain empty")

    report = (out / "report.md").read_text(encoding="utf-8-sig")
    require("NO_ACTION" in report, "formal report must retain the NO_ACTION boundary")
    return {
        "four_part_contract": True,
        "record_count": 4,
        "terminal_blocked_count": 4,
        "pending_count": 0,
        "settled_count": 0,
        "all_return_fields_empty": True,
        "current_action": "NO_ACTION",
        "shared_disposition_contract": True,
        "formal_commit_required": require_final_commit,
    }


def exact_coverage_ready(coverage: Mapping[str, Any]) -> bool:
    try:
        disposition_contract.validate_price_coverage(coverage)
        recomputed = settlement_audit.recompute_exact_coverage(coverage)
    except (disposition_contract.ExploratoryDispositionError, TypeError, ValueError):
        return False
    return bool(
        coverage.get("exact_coverage_ready") is True
        and recomputed.get("exact_coverage_ready") is True
    )


def resolve_overlay_script(paths: SettlementPaths, configured: str | None = None) -> Path:
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = paths.root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(paths.root.resolve())
        except ValueError as exc:
            raise OrchestrationError("calendar overlay script must stay inside the project root") from exc
        if candidate.suffix.lower() != ".py" or not candidate.is_file():
            raise OrchestrationError(f"calendar overlay script is missing or not Python: {candidate}")
        return candidate

    preferred = paths.scripts / "overlay_v5_29_exploratory_disposition.py"
    if preferred.is_file():
        return preferred
    candidates = sorted(paths.scripts.glob("*exploratory*calendar*overlay*.py"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise OrchestrationError(
            "V5.29 exploratory calendar overlay is missing; pass --calendar-overlay-script after it is installed"
        )
    raise OrchestrationError(f"multiple exploratory calendar overlays found: {[path.name for path in candidates]}")


def authoritative_immutable_paths(paths: SettlementPaths) -> list[Path]:
    observation = paths.root / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
    candidate = paths.root / "logs" / "v5_33_fund_flow_entry_price_freeze.jsonl"
    benchmark = paths.root / "logs" / "v5_34_fund_flow_benchmark_entry_freeze.jsonl"
    history = paths.root / "logs" / "v5_31_fund_flow_evidence_freeze_history.jsonl"
    result = [
        observation,
        paths.root / "logs" / "v5_25_fund_flow_forward_ledger.csv",
        checkpoint_path_for(observation),
        candidate,
        checkpoint_path_for(candidate),
        benchmark,
        checkpoint_path_for(benchmark),
        history,
        checkpoint_path_for(history),
    ]
    active = read_json(paths.active_pointer, allow_missing=True)
    manifest_text = str(active.get("manifest_path", "")).strip()
    if manifest_text:
        manifest = (paths.root / manifest_text).resolve()
        try:
            manifest.relative_to(paths.root.resolve())
        except ValueError as exc:
            raise OrchestrationError("active cohort manifest points outside the project root") from exc
        result.append(manifest)
    return dedupe_paths(result)


def chain_immutable_paths(paths: SettlementPaths) -> list[Path]:
    """Return immutable ledgers plus settlement and mainline price CSVs.

    Both globs are deliberately repeated after every command.  A newly added
    file therefore changes the key set and fails the same hash comparison as an
    edited or removed file.
    """

    return dedupe_paths([
        *authoritative_immutable_paths(paths),
        *sorted(paths.price_dir.glob("*.csv")),
        *sorted(paths.baseline_price_dir.glob("*.csv")),
    ])


def active_pointer_governance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if key != "verified_at_utc"}


def snapshot_hashes(paths: Sequence[Path], root: Path) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for path in paths:
        key = relative_path(path, root)
        if path.is_file():
            snapshot[key] = {"exists": True, "bytes": path.stat().st_size, "sha256": file_sha256(path)}
        else:
            snapshot[key] = {"exists": False, "bytes": 0, "sha256": "MISSING"}
    return snapshot


def write_command_results(out: Path, result: Mapping[str, Any]) -> None:
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    if isinstance(result, dict):
        result["command_results_path"] = str((debug / "command_results.json").resolve())
    atomic_write_json(debug / "command_results.json", dict(result))


def write_formal_commit_marker(paths: SettlementPaths, result: Mapping[str, Any]) -> None:
    commands = result.get("commands")
    require(isinstance(commands, list), "formal commit requires the completed base command list")
    expected_base_ids = [
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
    actual_ids = [str(item.get("step_id", "")) for item in commands if isinstance(item, Mapping)]
    require(actual_ids == expected_base_ids, f"formal commit base command order differs: {actual_ids}")
    summary = read_json(paths.final_out / "run_summary.json")
    active = read_json(paths.active_pointer)
    bound = {
        "report.md": paths.final_out / "report.md",
        "run_summary.json": paths.final_out / "run_summary.json",
        "top_candidates.csv": paths.final_out / "top_candidates.csv",
        "debug/settlement_dispositions.csv": paths.final_out / "debug" / "settlement_dispositions.csv",
        "debug/sha256_manifest.csv": paths.final_out / "debug" / "sha256_manifest.csv",
        "debug/pre_settlement_snapshot.json": paths.final_out / "debug" / "pre_settlement_snapshot.json",
        "debug/post_settlement_snapshot.json": paths.final_out / "debug" / "post_settlement_snapshot.json",
        "debug/date_coverage_audit.json": paths.final_out / "debug" / "date_coverage_audit.json",
    }
    missing = [name for name, path in bound.items() if not path.is_file()]
    require(not missing, f"formal commit cannot bind missing artifacts: {missing}")
    marker = {
        "schema_version": "fund-flow-exploratory-formal-commit-v1",
        "formal_disposition_committed": True,
        "committed_at": datetime.now(settlement_audit.SHANGHAI).isoformat(timespec="seconds"),
        "completion_status": "complete_terminal_exclusions",
        "compact_output_audit_passed": True,
        "active_cohort_id": str(active.get("cohort_id", "")),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", "")),
        "formal_summary_generated_at": str(summary.get("generated_at", "")),
        "artifact_sha256": {name: file_sha256(path) for name, path in bound.items()},
        "base_commands": json.loads(json.dumps(commands, ensure_ascii=False)),
    }
    atomic_write_json(paths.formal_commit, marker)


def result_path(paths: SettlementPaths, result: Mapping[str, Any]) -> Path:
    explicit = str(result.get("command_results_path", "")).strip()
    if explicit:
        return Path(explicit)
    out = paths.final_out if result.get("chain_started") else paths.preflight_out
    return out / "debug" / "command_results.json"


def validate_command_results_file(
    paths: SettlementPaths,
    expected_step_ids: Sequence[str],
) -> dict[str, Any]:
    payload = read_json(paths.final_out / "debug" / "command_results.json")
    require(
        strict_shanghai_timestamp(payload.get("generated_at"), expected_date=AS_OF_DATE) is not None,
        "final command_results generated_at must be an aware Asia/Shanghai timestamp on 2026-07-21",
    )
    require(payload.get("chain_completed") is True, "final command_results chain_completed must be true")
    require(
        payload.get("completion_status") == "complete_terminal_exclusions",
        "final command_results completion status is not terminal complete",
    )
    require(payload.get("authoritative_hashes_unchanged") is True, "final authoritative hashes changed")
    require(payload.get("active_pointer_governance_unchanged") is True, "active pointer governance changed")
    require(payload.get("formal_package_committed") is True, "formal package commit marker is absent")
    require(payload.get("compact_output_audit_passed") is True, "compact output audit did not pass")
    require(paths.formal_commit.is_file(), "formal commit marker file is missing")
    require(payload.get("final_artifact_validation", {}).get("passed") is True, "final artifact validation is absent")
    commands = payload.get("commands")
    require(isinstance(commands, list), "final command_results commands must be a list")
    actual_ids = [str(item.get("step_id", "")) for item in commands if isinstance(item, Mapping)]
    require(actual_ids == list(expected_step_ids), f"final command order differs: {actual_ids}")
    for item in commands:
        require(isinstance(item, Mapping), "final command result must be an object")
        step_id = str(item.get("step_id", ""))
        require(item.get("exit_code_expected") is True, f"{step_id} exit code was not expected")
        require(
            item.get("semantic_validation", {}).get("passed") is True,
            f"{step_id} semantic validation did not pass",
        )
        require(item.get("authoritative_hashes_unchanged") is True, f"{step_id} changed immutable hashes")
        require(item.get("active_pointer_governance_unchanged") is True, f"{step_id} changed active governance")
        script_sha = str(item.get("script_sha256", ""))
        require(len(script_sha) == 64 and script_sha != "MISSING", f"{step_id} script SHA256 is missing")
        require(item.get("script_sha256_unchanged") is True, f"{step_id} script changed during execution")
        require(isinstance(item.get("command"), list) and bool(item.get("command")), f"{step_id} command is missing")
        require(bool(str(item.get("started_at", ""))), f"{step_id} started_at is missing")
        require(bool(str(item.get("finished_at", ""))), f"{step_id} finished_at is missing")
        require(as_int(item.get("duration_ms")) >= 0, f"{step_id} duration_ms is invalid")
        require("stdout" in item and "stderr" in item, f"{step_id} stdout/stderr capture is missing")
        require(bool(str(item.get("expected_exit_semantics", ""))), f"{step_id} expected exit semantics are missing")
    by_id = {str(item.get("step_id", "")): item for item in commands}
    for step_id in ("v5_30_pre", "v5_30_post"):
        require(as_int(by_id.get(step_id, {}).get("exit_code")) == 2, f"{step_id} must preserve exit code 2")
        require(by_id.get(step_id, {}).get("expected_exit_codes") == [2], f"{step_id} expected exit contract changed")
    return {
        "command_count": len(commands),
        "order_matches": True,
        "all_expected_exits": True,
        "all_semantic_validations": True,
        "all_authoritative_hashes_unchanged": True,
        "v5_30_exit_code": 2,
    }


def validate_pair(
    summary: Mapping[str, Any],
    active: Mapping[str, Any],
    *,
    summary_hash_field: str = "active_cohort_manifest_hash",
) -> None:
    active_id = str(active.get("cohort_id", ""))
    active_hash = str(active.get("manifest_hash", ""))
    summary_id = str(summary.get("active_cohort_id") or summary.get("cohort_id") or "")
    summary_hash = str(summary.get(summary_hash_field) or summary.get("cohort_manifest_hash") or "")
    require(active.get("freeze_passed") is True, "active cohort pointer is not verified")
    require(bool(active_id) and len(active_hash) == 64, "active cohort pair is incomplete")
    require((summary_id, summary_hash) == (active_id, active_hash), "summary and active cohort pairs differ")


def validate_boundary(summary: Mapping[str, Any], *, require_goal: bool = True) -> None:
    expected = {
        "policy_status": "research_only",
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    }
    if require_goal:
        expected["goal_ready"] = False
    require_exact_fields(summary, expected, "research boundary")


def validate_all_ready_fields_false(summary: Mapping[str, Any], label: str) -> None:
    ready_fields = [key for key in summary if key.endswith("_ready")]
    require(bool(ready_fields), f"{label} has no explicit readiness fields")
    require(all(summary.get(key) is False for key in ready_fields), f"{label} contains a true or invalid readiness field")


def require_exact_fields(
    source: Mapping[str, Any],
    expected: Mapping[str, Any],
    label: str,
) -> None:
    for field, expected_value in expected.items():
        actual = source.get(field)
        if isinstance(expected_value, bool):
            passed = type(actual) is bool and actual is expected_value
        elif isinstance(expected_value, int):
            passed = type(actual) is int and actual == expected_value
        else:
            passed = actual == expected_value
        require(passed, f"{label} field {field!r}: expected {expected_value!r}, got {actual!r}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise OrchestrationError(message)


def read_json(path: Path, *, allow_missing: bool = False) -> dict[str, Any]:
    if not path.is_file():
        if allow_missing:
            return {}
        raise OrchestrationError(f"required JSON artifact is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestrationError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise OrchestrationError(f"JSON artifact must be an object: {path}")
    return dict(value)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise OrchestrationError(f"required CSV artifact is missing: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error) as exc:
        raise OrchestrationError(f"invalid CSV artifact {path}: {exc}") from exc


def hash_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    keys = sorted(set(before) | set(after))
    return {key: {"before": before.get(key), "after": after.get(key)} for key in keys if before.get(key) != after.get(key)}


def mapping_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    return hash_diff(before, after)


def current_step_id(result: Mapping[str, Any]) -> str:
    commands = result.get("commands")
    if isinstance(commands, list) and commands:
        last = commands[-1]
        if isinstance(last, Mapping):
            return str(last.get("step_id", "unknown"))
    return "pre_chain"


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def dedupe_paths(paths: Sequence[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def as_int(value: Any) -> int:
    if isinstance(value, bool):
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def is_false_text(value: Any) -> bool:
    return str(value).strip().lower() in {"false", "0", "no"}


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def strict_shanghai_timestamp(value: Any, *, expected_date: str = "") -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(hours=8):
        return None
    normalized = parsed.astimezone(settlement_audit.SHANGHAI)
    if expected_date and normalized.date().isoformat() != expected_date:
        return None
    return normalized


def self_check() -> None:
    coverage = {
        "checked": True,
        "exact_coverage_ready": True,
        "source_file_count": 100,
        "valid_source_file_count": 100,
        "invalid_file_count": 0,
        "invalid_files": [],
        "entry_industry_count": 100,
        "exit_industry_count": 100,
        "entry_exit_common_count": 100,
        "candidate_entry_count": 4,
        "candidate_exit_count": 4,
        "candidate_common_count": 4,
        "minimum_benchmark_industries": 100,
        "overall_max_date": AS_OF_DATE,
        "price_values_retained": False,
    }
    assert exact_coverage_ready(coverage)
    assert not exact_coverage_ready({**coverage, "entry_exit_common_count": 99})
    sample = {"ledger_rows": 0, "global_ledger_rows": 4, "global_late_backfill_count": 8}
    require_exact_fields(sample, sample, "self-check")
    assert hash_diff({"a": 1}, {"a": 2}) == {"a": {"before": 1, "after": 2}}
    print("self_check=pass")


if __name__ == "__main__":
    raise SystemExit(main())
