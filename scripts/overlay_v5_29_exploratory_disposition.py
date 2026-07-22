from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_v5_31_fund_flow_evidence_freeze_manifest as v531
import fund_flow_exploratory_disposition as exploratory_disposition
from research_integrity import canonical_csv_bytes


# This script decorates the frozen V5.29 output; it is not that version's
# producer.  Keep the binding name outside the governance inventory's
# OUT/OUTPUT producer-discovery convention so the original producer remains
# unambiguous.
V529_OUTPUT_DIR = ROOT / "outputs" / "audit" / "fund_flow_evidence_calendar_v5_29"
SUMMARY_PATH = V529_OUTPUT_DIR / "run_summary.json"
REPORT_PATH = V529_OUTPUT_DIR / "report.md"
DEBUG = V529_OUTPUT_DIR / "debug"
EVENT_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"

OVERLAY_FIELDS = (
    "exploratory_disposition_artifact_present",
    "exploratory_disposition_valid",
    "exploratory_completion_status",
    "exploratory_observation_count",
    "exploratory_settled_count",
    "exploratory_terminal_blocked_count",
    "exploratory_pending_count",
    "exploratory_qualified_settled_count",
    "exploratory_calendar_rows",
    "exploratory_evidence_scope",
    "exploratory_disposition_generated_at",
    "exploratory_disposition_error",
)

ACTIVE_INVARIANT_FIELDS = (
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

MARKER_START = "<!-- exploratory-disposition-overlay:start -->"
MARKER_END = "<!-- exploratory-disposition-overlay:end -->"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Overlay the independently validated legacy exploratory disposition onto V5.29 ignored outputs."
    )
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0

    summary = read_summary(SUMMARY_PATH)
    active = v531.validated_active_cohort()
    validate_base_summary(summary, active)
    authoritative_rows = exploratory_disposition.load_authoritative_observations(ROOT)
    authoritative_ids = {
        str(row.get("observation_id", "")) for row in authoritative_rows
    }
    observation_count = len(authoritative_rows)
    presence = exploratory_disposition.artifact_presence()
    try:
        loaded = exploratory_disposition.load_optional_disposition(active)
    except exploratory_disposition.ExploratoryDispositionError as exc:
        state = exploratory_disposition.pending_disposition(observation_count, error=str(exc))
        state["artifact_present"] = any(presence.values())
        state["artifact_presence"] = presence
        write_overlay(summary, state)
        print(f"output_dir={V529_OUTPUT_DIR}")
        print("exploratory_disposition_valid=false")
        print(f"error={exc}")
        return 2

    if loaded is None:
        state = exploratory_disposition.pending_disposition(observation_count)
        state["artifact_presence"] = presence
    elif (
        int(loaded.get("observation_count", -1)) != observation_count
        or {
            str(row.get("observation_id", ""))
            for row in loaded.get("rows", [])
            if isinstance(row, Mapping)
        }
        != authoritative_ids
    ):
        error = (
            "formal disposition observations do not exactly match the verified ledger: "
            f"disposition_count={loaded.get('observation_count')}; ledger_count={observation_count}; "
            f"ledger_ids={sorted(authoritative_ids)}"
        )
        state = exploratory_disposition.pending_disposition(observation_count, error=error)
        state["artifact_present"] = True
        state["artifact_presence"] = presence
        write_overlay(summary, state)
        print(f"output_dir={V529_OUTPUT_DIR}")
        print("exploratory_disposition_valid=false")
        print(f"error={error}")
        return 2
    else:
        state = loaded
        state["artifact_presence"] = presence

    write_overlay(summary, state)
    print(f"output_dir={V529_OUTPUT_DIR}")
    print(f"exploratory_disposition_valid={str(state.get('valid') is True).lower()}")
    print(
        "exploratory="
        f"{state.get('observation_count', 0)}/"
        f"{state.get('settled_count', 0)}/"
        f"{state.get('terminal_blocked_count', 0)}/"
        f"{state.get('pending_count', 0)}"
    )
    return 0


def read_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError("V5.29 run_summary.json is missing; run the frozen V5.29 generator first")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, Mapping):
        raise RuntimeError("V5.29 run_summary.json must be a JSON object")
    return dict(value)


def validate_base_summary(summary: Mapping[str, Any], active: Mapping[str, Any]) -> None:
    if summary.get("policy_id") != "fund_flow_evidence_calendar_v5_29":
        raise RuntimeError("overlay requires the original V5.29 evidence-calendar summary")
    if summary.get("as_of_date") != "2026-07-21":
        raise RuntimeError(
            f"V5.29 must be rebuilt with --as-of-date 2026-07-21; got {summary.get('as_of_date')!r}"
        )
    active_pair = (str(active.get("cohort_id", "")), str(active.get("manifest_hash", "")))
    summary_pair = (
        str(summary.get("active_cohort_id", "")),
        str(summary.get("active_cohort_manifest_hash", "")),
    )
    if active.get("freeze_passed") is not True or summary_pair != active_pair:
        raise RuntimeError(
            f"V5.29 active pair mismatch; summary={summary_pair}; active={active_pair}"
        )
    expected_fail_closed = {
        "ledger_rows": 0,
        "global_history_settled_rows": 0,
        "promotion_ready": False,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
    }
    for field, expected in expected_fail_closed.items():
        actual = summary.get(field)
        if type(actual) is not type(expected) or actual != expected:
            raise RuntimeError(
                f"V5.29 field {field!r} must remain {expected!r} before exploratory overlay; got {actual!r}"
            )


def apply_overlay_fields(summary: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(summary)
    before = {field: summary.get(field) for field in ACTIVE_INVARIANT_FIELDS}
    result.update({
        "exploratory_disposition_artifact_present": state.get("artifact_present") is True,
        "exploratory_disposition_valid": state.get("valid") is True,
        "exploratory_completion_status": str(state.get("completion_status", "")),
        "exploratory_observation_count": int(state.get("observation_count", 0) or 0),
        "exploratory_settled_count": int(state.get("settled_count", 0) or 0),
        "exploratory_terminal_blocked_count": int(state.get("terminal_blocked_count", 0) or 0),
        "exploratory_pending_count": int(state.get("pending_count", 0) or 0),
        "exploratory_qualified_settled_count": int(state.get("qualified_settled_count", 0) or 0),
        "exploratory_calendar_rows": 1 if state.get("valid") is True else 0,
        "exploratory_evidence_scope": "exploratory_fund_flow_only",
        "exploratory_disposition_generated_at": str(state.get("generated_at", "")),
        "exploratory_disposition_error": str(state.get("error", "")),
    })
    after = {field: result.get(field) for field in ACTIVE_INVARIANT_FIELDS}
    if before != after:
        raise RuntimeError(f"exploratory overlay changed active V5.29 fields: before={before}; after={after}")
    return result


def calendar_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    if state.get("valid") is not True:
        return []
    return [{
        "event_date": "2026-07-21",
        "event_type": "exploratory_settlement_disposition",
        "row_count": int(state.get("observation_count", 0) or 0),
        "status": "completed_terminal_exclusions",
        "command": "python .\\scripts\\audit_fund_flow_exploratory_settlement_readiness.py",
        "action": (
            "四条 legacy 探索观察已终局排除：settled 0 / terminal blocked 4 / pending 0；"
            "0 条进入目标样本或晋级评价。"
        ),
        "evidence_scope": "exploratory_fund_flow_only",
    }]


def write_overlay(summary: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    updated = apply_overlay_fields(summary, state)
    active_before = {field: summary.get(field) for field in ACTIVE_INVARIANT_FIELDS}
    active_after = {field: updated.get(field) for field in ACTIVE_INVARIANT_FIELDS}
    if active_before != active_after:
        raise RuntimeError("active V5.29 state changed while writing exploratory overlay")

    # Build and validate every payload before creating a directory or replacing
    # any file.  A malformed/duplicated marker therefore cannot leave a new
    # summary beside an old completed report.
    rows = calendar_rows(state)
    if not REPORT_PATH.is_file():
        raise RuntimeError(
            "V5.29 report.md is missing; rebuild the frozen V5.29 output before applying the overlay"
        )
    base_report = REPORT_PATH.read_text(encoding="utf-8-sig")
    rendered_report = replace_overlay_block(base_report, render_overlay_block(state, rows))
    overlay_debug = {
        "state": {key: value for key, value in state.items() if key != "rows"},
        "active_invariants_before": active_before,
        "active_invariants_after": active_after,
        "active_invariants_unchanged": True,
    }
    fieldnames = [
        "event_date",
        "event_type",
        "row_count",
        "status",
        "command",
        "action",
        "evidence_scope",
    ]
    payloads = [
        (REPORT_PATH, rendered_report.encode("utf-8")),
        (
            DEBUG / "exploratory_disposition_calendar.csv",
            b"\xef\xbb\xbf" + canonical_csv_bytes(rows, fieldnames=fieldnames),
        ),
        (
            DEBUG / "exploratory_disposition_overlay.json",
            pretty_json_bytes(overlay_debug),
        ),
        # Commit the machine-readable completion claim last.  The transaction
        # rolls every earlier replacement back if this final step fails.
        (SUMMARY_PATH, pretty_json_bytes(updated)),
    ]
    validate_overlay_payloads(payloads, state=state)
    transactional_write_files(payloads)


def replace_overlay_block(report: str, block: str) -> str:
    start_count = report.count(MARKER_START)
    end_count = report.count(MARKER_END)
    if start_count != end_count or start_count not in {0, 1}:
        raise RuntimeError(
            "V5.29 report contains incomplete or duplicate exploratory overlay markers"
        )
    base = report
    if start_count == 1:
        start = base.find(MARKER_START)
        end = base.find(MARKER_END)
        if end < start:
            raise RuntimeError(
                "V5.29 report contains exploratory overlay markers in the wrong order"
            )
        base = (base[:start] + base[end + len(MARKER_END):]).rstrip()
    return f"{base.rstrip()}\n\n{MARKER_START}\n{block.rstrip()}\n{MARKER_END}\n"


def pretty_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def validate_overlay_payloads(
    payloads: Sequence[tuple[Path, bytes]],
    *,
    state: Mapping[str, Any],
) -> None:
    by_path = {path: data for path, data in payloads}
    if len(by_path) != len(payloads):
        raise RuntimeError("exploratory overlay transaction contains duplicate targets")
    summary = json.loads(by_path[SUMMARY_PATH].decode("utf-8"))
    debug = json.loads(
        by_path[DEBUG / "exploratory_disposition_overlay.json"].decode("utf-8")
    )
    report = by_path[REPORT_PATH].decode("utf-8")
    if report.count(MARKER_START) != 1 or report.count(MARKER_END) != 1:
        raise RuntimeError("rendered V5.29 report does not contain exactly one overlay block")
    expected_valid = state.get("valid") is True
    if summary.get("exploratory_disposition_valid") is not expected_valid:
        raise RuntimeError("rendered V5.29 summary does not match the overlay state")
    if debug.get("active_invariants_unchanged") is not True:
        raise RuntimeError("rendered V5.29 debug payload lost the active-invariant assertion")


def transactional_write_files(
    payloads: Sequence[tuple[Path, bytes]],
    *,
    replace_file: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> None:
    """Replace a small fixed file set with rollback on every handled failure."""

    if not payloads:
        return
    transaction_parent = Path(
        os.path.commonpath([str(target.parent.resolve()) for target, _data in payloads])
    )
    transaction_parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=".v529-overlay-", dir=transaction_parent))
    staged: list[tuple[Path, Path, Path]] = []
    installed: set[Path] = set()
    backups: set[Path] = set()
    cleanup_stage = True
    try:
        for index, (target, data) in enumerate(payloads):
            staged_path = stage_root / f"payload-{index}.tmp"
            backup_path = stage_root / f"backup-{index}.bak"
            with staged_path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if staged_path.read_bytes() != data:
                raise RuntimeError(f"staged overlay payload verification failed: {target}")
            staged.append((target, staged_path, backup_path))

        for target, staged_path, backup_path in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                replace_file(target, backup_path)
                backups.add(target)
            replace_file(staged_path, target)
            installed.add(target)
    except BaseException:
        rollback_errors: list[str] = []
        for target, _staged_path, backup_path in reversed(staged):
            try:
                if target in installed and target.exists():
                    target.unlink()
                if target in backups and backup_path.exists():
                    replace_file(backup_path, target)
            except BaseException as rollback_exc:  # pragma: no cover - catastrophic filesystem failure
                rollback_errors.append(f"{target}: {rollback_exc}")
        if rollback_errors:
            cleanup_stage = False
            raise RuntimeError(
                "exploratory overlay transaction failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
                + f"; recovery files retained at {stage_root}"
            )
        raise
    finally:
        if cleanup_stage:
            shutil.rmtree(stage_root, ignore_errors=True)


def render_overlay_block(state: Mapping[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        "## 历史探索观察终局处置",
        "",
    ]
    if rows:
        row = rows[0]
        lines.extend([
            "| 日期 | 证据范围 | 观察数 | 终局 | settled | terminal blocked | pending | qualified settled |",
            "|---|---|---:|---|---:|---:|---:|---:|",
            f"| {row['event_date']} | `{row['evidence_scope']}` | {state.get('observation_count', 0)} | "
            f"`{row['status']}` | {state.get('settled_count', 0)} | {state.get('terminal_blocked_count', 0)} | "
            f"{state.get('pending_count', 0)} | {state.get('qualified_settled_count', 0)} |",
            "",
            "该行只记录四条 legacy 探索观察的独立终局；不改写 active ledger、晋级缺口、下一动作、promotion_ready 或 goal_ready，也不包含收益。",
        ])
    else:
        lines.append(
            "正式处置产物尚未完整通过校验；四条探索观察继续保持 pending，不得提前写成已结算或已终局排除。"
        )
        error = str(state.get("error", ""))
        if error:
            lines.extend(["", f"失败关闭原因：`{error}`"])
    return "\n".join(lines)


def self_check() -> None:
    summary = {
        "active_cohort_id": "c1",
        "active_cohort_manifest_hash": "h1",
        "active_cohort_validated": True,
        "ledger_rows": 0,
        "global_history_ledger_rows": 4,
        "global_history_settled_rows": 0,
        "calendar_rows": 0,
        "fail_count": 6,
        "pending_count": 4,
        "next_action_date": "",
        "next_action": "",
        "next_command": "",
        "promotion_ready": False,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_evidence_collection_pending",
        "final_verdict": "blocked",
    }
    state = {
        "artifact_present": True,
        "valid": True,
        "generated_at": "2026-07-21T15:30:00+08:00",
        "completion_status": "complete_terminal_exclusions",
        "observation_count": 4,
        "settled_count": 0,
        "terminal_blocked_count": 4,
        "pending_count": 0,
        "qualified_settled_count": 0,
    }
    updated = apply_overlay_fields(summary, state)
    assert all(updated.get(field) == summary.get(field) for field in ACTIVE_INVARIANT_FIELDS)
    assert updated["exploratory_terminal_blocked_count"] == 4
    assert len(calendar_rows(state)) == 1
    first = replace_overlay_block("base\n", render_overlay_block(state, calendar_rows(state)))
    second = replace_overlay_block(first, render_overlay_block(state, calendar_rows(state)))
    assert second.count(MARKER_START) == 1 and second.count(MARKER_END) == 1
    print("self_check=pass")


if __name__ == "__main__":
    raise SystemExit(main())
