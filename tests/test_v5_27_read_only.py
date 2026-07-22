from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import settle_v5_27_fund_flow_forward_samples as v527
from fund_flow_forward_evidence import (
    append_events,
    checkpoint_path_for,
    materialize_observations,
    read_events,
    with_schema_fields,
    write_materialized_csv,
)
from research_integrity import file_sha256


ACTIVE = {
    "freeze_passed": True,
    "cohort_id": "active-fixture",
    "manifest_hash": "a" * 64,
}


def observation(*, cohort_id: str = "legacy", manifest_hash: str = "b" * 64) -> dict[str, Any]:
    return with_schema_fields({
        "event_type": "observation",
        "event_id": "obs-read-only:observation",
        "observation_id": "obs-read-only",
        "parent_event_id": "",
        "batch_id": "fixture-batch",
        "industry_code": "801194",
        "industry_name": "保险Ⅱ",
        "signal_date": "2026-06-22",
        "planned_entry_date": "2026-06-23",
        "planned_exit_date": "2026-07-21",
        "settlement_status": "not_due",
        "qualified_for_goal": "False",
        "integrity_eligible": "False",
        "promotion_eligible": "False",
        "late_backfill_excluded": "False",
        "sample_scope": "exploratory_fund_flow_only",
        "cohort_id": cohort_id,
        "cohort_manifest_hash": manifest_hash,
    })


def install_materialized_ledger(
    tmp_path: Path,
    *,
    row: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    event_ledger = tmp_path / "logs" / "ledger.jsonl"
    materialized_csv = tmp_path / "logs" / "ledger.csv"
    append_events(event_ledger, [row or observation()])
    rows = materialize_observations(read_events(event_ledger))
    write_materialized_csv(materialized_csv, rows)
    return event_ledger, materialized_csv, checkpoint_path_for(event_ledger)


def fingerprint(paths: list[Path]) -> dict[str, tuple[int, str]]:
    return {
        str(path): (path.stat().st_size, file_sha256(path))
        for path in paths
        if path.is_file()
    }


def install_read_only_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    row: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    event_ledger, materialized_csv, checkpoint = install_materialized_ledger(
        tmp_path,
        row=row,
    )
    monkeypatch.setattr(v527, "EVENT_LEDGER", event_ledger)
    monkeypatch.setattr(v527, "LEDGER", materialized_csv)
    monkeypatch.setattr(v527, "ENTRY_FREEZE_LEDGER", tmp_path / "logs" / "entry.jsonl")
    monkeypatch.setattr(v527, "BENCHMARK_ENTRY_FREEZE_LEDGER", tmp_path / "logs" / "benchmark.jsonl")
    monkeypatch.setattr(v527, "OUT", tmp_path / "outputs" / "v527")
    monkeypatch.setattr(v527, "DEBUG", tmp_path / "outputs" / "v527" / "debug")
    monkeypatch.setattr(v527, "validated_active_cohort", lambda: dict(ACTIVE))
    monkeypatch.setattr(
        v527,
        "load_history",
        lambda _path: pd.DataFrame(
            columns=["trade_date", "industry_code", "industry_name", "close_index"]
        ),
    )
    monkeypatch.setattr(v527, "load_entry_freeze", lambda _path: {})
    monkeypatch.setattr(v527, "load_benchmark_entry_freeze", lambda _path: {})
    monkeypatch.setattr(v527, "read_json", lambda _path: {})
    monkeypatch.setattr(
        v527,
        "integrity_inputs_current",
        lambda _integrity, required_as_of=None: (False, "fixture_fail_closed"),
    )
    return event_ledger, materialized_csv, checkpoint


def test_read_only_loader_verifies_without_rewriting_ledger_or_checkpoint(tmp_path: Path) -> None:
    event_ledger, materialized_csv, checkpoint = install_materialized_ledger(tmp_path)
    before = fingerprint([event_ledger, materialized_csv, checkpoint])

    events, rows = v527.load_authoritative_state_read_only(event_ledger, materialized_csv)

    assert len(events) == 1
    assert len(rows) == 1
    assert fingerprint([event_ledger, materialized_csv, checkpoint]) == before


def test_read_only_loader_refuses_stale_materialized_csv_without_repair(tmp_path: Path) -> None:
    event_ledger, materialized_csv, checkpoint = install_materialized_ledger(tmp_path)
    materialized_csv.write_text("stale\n", encoding="utf-8")
    before = fingerprint([event_ledger, materialized_csv, checkpoint])

    with pytest.raises(RuntimeError, match="refuses to rewrite"):
        v527.load_authoritative_state_read_only(event_ledger, materialized_csv)

    assert fingerprint([event_ledger, materialized_csv, checkpoint]) == before


def test_read_only_execution_with_zero_active_rows_writes_only_audit_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_ledger, materialized_csv, checkpoint = install_read_only_runtime(
        tmp_path,
        monkeypatch,
    )
    before = fingerprint([event_ledger, materialized_csv, checkpoint])

    summary = v527.execute_settlement(date(2026, 7, 21), read_only=True)

    assert summary["execution_mode"] == "read_only_audit"
    assert summary["ledger_rows"] == 0
    assert summary["settled_rows"] == 0
    assert summary["pending_rows"] == 0
    assert summary["proposed_settlement_count"] == 0
    assert summary["event_ledger_write_invoked"] is False
    assert summary["materialized_ledger_write_invoked"] is False
    assert summary["checkpoint_write_invoked"] is False
    assert summary["authoritative_ledger_files_unchanged"] is True
    assert summary["authoritative_ledger_snapshot_before"] == summary["authoritative_ledger_snapshot_after"]
    assert fingerprint([event_ledger, materialized_csv, checkpoint]) == before
    assert (v527.OUT / "run_summary.json").is_file()
    assert (v527.OUT / "report.md").is_file()
    assert (v527.DEBUG / "settlement_audit.csv").is_file()


def test_read_only_execution_fails_before_any_writer_when_settlement_is_proposed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_row = observation(
        cohort_id=str(ACTIVE["cohort_id"]),
        manifest_hash=str(ACTIVE["manifest_hash"]),
    )
    event_ledger, materialized_csv, checkpoint = install_read_only_runtime(
        tmp_path,
        monkeypatch,
        row=active_row,
    )
    before = fingerprint([event_ledger, materialized_csv, checkpoint])
    proposed = {**active_row, "settlement_status": "settled", "actual_exit_date": "2026-07-21"}
    monkeypatch.setattr(
        v527,
        "settle_rows",
        lambda *_args, **_kwargs: ([proposed], [proposed]),
    )

    def forbidden_writer(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a ledger, checkpoint, source-snapshot, or audit writer was invoked")

    monkeypatch.setattr(v527, "append_events", forbidden_writer)
    monkeypatch.setattr(v527, "write_materialized_csv", forbidden_writer)
    monkeypatch.setattr(v527, "build_settlement_source_snapshot", forbidden_writer)
    monkeypatch.setattr(v527, "write_outputs", forbidden_writer)

    with pytest.raises(v527.ReadOnlySettlementProposalError, match="proposed_count=1"):
        v527.execute_settlement(date(2026, 7, 21), read_only=True)

    assert fingerprint([event_ledger, materialized_csv, checkpoint]) == before
    assert not v527.OUT.exists()
