from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import overlay_v5_29_exploratory_disposition as overlay


def base_summary() -> dict[str, object]:
    return {
        "version": "5.29.1",
        "policy_id": "fund_flow_evidence_calendar_v5_29",
        "as_of_date": "2026-07-21",
        "active_cohort_id": "active-v1",
        "active_cohort_manifest_hash": "a" * 64,
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
        "final_verdict": "active verdict",
    }


def valid_state() -> dict[str, object]:
    return {
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


def test_overlay_adds_independent_fields_without_changing_active_state() -> None:
    summary = base_summary()
    updated = overlay.apply_overlay_fields(summary, valid_state())

    assert all(updated.get(field) == summary.get(field) for field in overlay.ACTIVE_INVARIANT_FIELDS)
    assert updated["exploratory_disposition_valid"] is True
    assert updated["exploratory_observation_count"] == 4
    assert updated["exploratory_settled_count"] == 0
    assert updated["exploratory_terminal_blocked_count"] == 4
    assert updated["exploratory_pending_count"] == 0
    assert updated["exploratory_qualified_settled_count"] == 0
    assert updated["exploratory_calendar_rows"] == 1


def test_completed_exploratory_row_is_not_an_active_next_action() -> None:
    state = valid_state()
    rows = overlay.calendar_rows(state)

    assert rows == [{
        "event_date": "2026-07-21",
        "event_type": "exploratory_settlement_disposition",
        "row_count": 4,
        "status": "completed_terminal_exclusions",
        "command": "python .\\scripts\\audit_fund_flow_exploratory_settlement_readiness.py",
        "action": "四条 legacy 探索观察已终局排除：settled 0 / terminal blocked 4 / pending 0；0 条进入目标样本或晋级评价。",
        "evidence_scope": "exploratory_fund_flow_only",
    }]
    assert "next_action" not in rows[0]
    assert "goal_ready" not in rows[0]


def test_invalid_asserted_package_has_no_completed_calendar_row() -> None:
    state = {
        "artifact_present": True,
        "valid": False,
        "completion_status": "invalid_fail_closed",
        "observation_count": 4,
        "settled_count": 0,
        "terminal_blocked_count": 0,
        "pending_count": 4,
        "qualified_settled_count": 0,
        "error": "tampered",
    }

    updated = overlay.apply_overlay_fields(base_summary(), state)

    assert updated["exploratory_disposition_valid"] is False
    assert updated["exploratory_calendar_rows"] == 0
    assert overlay.calendar_rows(state) == []
    assert updated["goal_ready"] is False
    assert updated["next_action"] == ""


def test_write_overlay_is_idempotent_and_keeps_original_active_calendar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary_path = tmp_path / "run_summary.json"
    report_path = tmp_path / "report.md"
    debug = tmp_path / "debug"
    summary = base_summary()
    summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    report_path.write_text("# V5.29\n\n## 证据日历\n\n无前推日程\n", encoding="utf-8")
    monkeypatch.setattr(overlay, "SUMMARY_PATH", summary_path)
    monkeypatch.setattr(overlay, "REPORT_PATH", report_path)
    monkeypatch.setattr(overlay, "DEBUG", debug)

    overlay.write_overlay(summary, valid_state())
    first_report = report_path.read_text(encoding="utf-8")
    saved_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    overlay.write_overlay(saved_summary, valid_state())
    second_report = report_path.read_text(encoding="utf-8")

    assert first_report == second_report
    assert second_report.count(overlay.MARKER_START) == 1
    assert "无前推日程" in second_report
    assert all(saved_summary.get(field) == summary.get(field) for field in overlay.ACTIVE_INVARIANT_FIELDS)
    with (debug / "exploratory_disposition_calendar.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["evidence_scope"] == "exploratory_fund_flow_only"


def test_base_summary_requires_due_date_and_current_active_pair() -> None:
    active = {"cohort_id": "active-v1", "manifest_hash": "a" * 64, "freeze_passed": True}
    overlay.validate_base_summary(base_summary(), active)

    with pytest.raises(RuntimeError, match="2026-07-21"):
        overlay.validate_base_summary({**base_summary(), "as_of_date": "2026-07-18"}, active)
    with pytest.raises(RuntimeError, match="pair mismatch"):
        overlay.validate_base_summary(
            {**base_summary(), "active_cohort_manifest_hash": "b" * 64}, active
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ledger_rows", 1),
        ("global_history_settled_rows", 1),
        ("promotion_ready", True),
        ("goal_ready", True),
        ("can_claim_strong_rebound_industries", True),
        ("production_ready", True),
        ("auto_execution_allowed", True),
    ],
)
def test_base_summary_rejects_any_active_promotion_or_settlement_claim(
    field: str,
    value: object,
) -> None:
    active = {"cohort_id": "active-v1", "manifest_hash": "a" * 64, "freeze_passed": True}

    with pytest.raises(RuntimeError, match=field):
        overlay.validate_base_summary({**base_summary(), field: value}, active)


@pytest.mark.parametrize(
    "report",
    [
        f"base\n{overlay.MARKER_START}\nold completed\n",
        f"base\n{overlay.MARKER_END}\n",
        (
            f"base\n{overlay.MARKER_START}\none\n{overlay.MARKER_END}\n"
            f"{overlay.MARKER_START}\ntwo\n{overlay.MARKER_END}\n"
        ),
        f"base\n{overlay.MARKER_END}\nold\n{overlay.MARKER_START}\n",
    ],
)
def test_malformed_or_duplicate_markers_fail_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report: str,
) -> None:
    summary_path = tmp_path / "run_summary.json"
    report_path = tmp_path / "report.md"
    debug = tmp_path / "debug"
    summary_path.write_text(json.dumps(base_summary()), encoding="utf-8")
    report_path.write_text(report, encoding="utf-8")
    summary_before = summary_path.read_bytes()
    report_before = report_path.read_bytes()
    monkeypatch.setattr(overlay, "SUMMARY_PATH", summary_path)
    monkeypatch.setattr(overlay, "REPORT_PATH", report_path)
    monkeypatch.setattr(overlay, "DEBUG", debug)

    with pytest.raises(RuntimeError, match="marker"):
        overlay.write_overlay(base_summary(), valid_state())

    assert summary_path.read_bytes() == summary_before
    assert report_path.read_bytes() == report_before
    assert not debug.exists()


def test_transaction_rolls_back_every_target_on_mid_commit_failure(tmp_path: Path) -> None:
    targets = [tmp_path / f"file-{index}.txt" for index in range(4)]
    originals = {}
    for index, target in enumerate(targets):
        target.write_bytes(f"old-{index}".encode())
        originals[target] = target.read_bytes()
    payloads = [(target, f"new-{index}".encode()) for index, target in enumerate(targets)]
    calls = 0

    def fail_once(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected replacement failure")
        os.replace(source, target)

    with pytest.raises(OSError, match="injected"):
        overlay.transactional_write_files(payloads, replace_file=fail_once)

    assert {target: target.read_bytes() for target in targets} == originals


def test_keyboard_interrupt_rolls_back_every_overlay_target(tmp_path: Path) -> None:
    targets = [tmp_path / f"file-{index}.txt" for index in range(4)]
    originals = {}
    for index, target in enumerate(targets):
        target.write_bytes(f"old-{index}".encode())
        originals[target] = target.read_bytes()
    payloads = [(target, f"new-{index}".encode()) for index, target in enumerate(targets)]
    calls = 0

    def interrupt_once(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise KeyboardInterrupt("injected interrupt")
        os.replace(source, target)

    with pytest.raises(KeyboardInterrupt, match="injected"):
        overlay.transactional_write_files(payloads, replace_file=interrupt_once)

    assert {target: target.read_bytes() for target in targets} == originals
    assert not list(tmp_path.glob(".v529-overlay-*"))


def test_keyboard_interrupt_during_overlay_rollback_retains_recovery_files(tmp_path: Path) -> None:
    targets = [tmp_path / f"file-{index}.txt" for index in range(3)]
    for index, target in enumerate(targets):
        target.write_bytes(f"old-{index}".encode())
    payloads = [(target, f"new-{index}".encode()) for index, target in enumerate(targets)]
    calls = 0

    def interrupt_commit_and_rollback(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
    ) -> None:
        nonlocal calls
        calls += 1
        if calls in {4, 5}:
            raise KeyboardInterrupt(f"injected interrupt {calls}")
        os.replace(source, target)

    with pytest.raises(RuntimeError, match="recovery files retained"):
        overlay.transactional_write_files(payloads, replace_file=interrupt_commit_and_rollback)

    recovery_dirs = list(tmp_path.glob(".v529-overlay-*"))
    assert len(recovery_dirs) == 1
    assert list(recovery_dirs[0].glob("backup-*.bak"))


def test_invalid_state_removes_previous_completed_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary_path = tmp_path / "run_summary.json"
    report_path = tmp_path / "report.md"
    debug = tmp_path / "debug"
    summary = base_summary()
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    report_path.write_text("# V5.29\n", encoding="utf-8")
    monkeypatch.setattr(overlay, "SUMMARY_PATH", summary_path)
    monkeypatch.setattr(overlay, "REPORT_PATH", report_path)
    monkeypatch.setattr(overlay, "DEBUG", debug)

    overlay.write_overlay(summary, valid_state())
    completed_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    invalid = {
        "artifact_present": True,
        "valid": False,
        "completion_status": "invalid_fail_closed",
        "observation_count": 4,
        "settled_count": 0,
        "terminal_blocked_count": 0,
        "pending_count": 4,
        "qualified_settled_count": 0,
        "error": "formal package drifted",
    }
    overlay.write_overlay(completed_summary, invalid)

    saved = json.loads(summary_path.read_text(encoding="utf-8"))
    report = report_path.read_text(encoding="utf-8")
    assert saved["exploratory_disposition_valid"] is False
    assert saved["exploratory_calendar_rows"] == 0
    assert "completed_terminal_exclusions" not in report
    assert "正式处置产物尚未完整通过校验" in report
    assert report.count(overlay.MARKER_START) == 1


def test_write_overlay_commits_summary_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary_path = tmp_path / "run_summary.json"
    report_path = tmp_path / "report.md"
    debug = tmp_path / "debug"
    summary_path.write_text(json.dumps(base_summary()), encoding="utf-8")
    report_path.write_text("# V5.29\n", encoding="utf-8")
    monkeypatch.setattr(overlay, "SUMMARY_PATH", summary_path)
    monkeypatch.setattr(overlay, "REPORT_PATH", report_path)
    monkeypatch.setattr(overlay, "DEBUG", debug)
    captured: list[Path] = []

    def capture(payloads: object) -> None:
        captured.extend(path for path, _data in payloads)  # type: ignore[union-attr]

    monkeypatch.setattr(overlay, "transactional_write_files", capture)
    overlay.write_overlay(base_summary(), valid_state())

    assert captured[-1] == summary_path
