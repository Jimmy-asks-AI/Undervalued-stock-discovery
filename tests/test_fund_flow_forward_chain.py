from __future__ import annotations

import shutil
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_v5_30_fund_flow_forward_ledger_integrity as v530
import build_v5_25_fund_flow_forward_observer as v525
import build_v5_26_fund_flow_forward_entry_gate as v526
import build_v5_28_fund_flow_promotion_evaluator as v528
import build_v5_29_fund_flow_evidence_calendar as v529
import build_v5_31_fund_flow_evidence_freeze_manifest as v531
import build_v5_32_fund_flow_holding_observation as v532
import build_v5_33_fund_flow_entry_price_freeze as v533
import build_v5_34_fund_flow_benchmark_entry_freeze as v534
import build_v5_35_fund_flow_waiting_room as v535
import research_integrity
import settle_v5_27_fund_flow_forward_samples as v527
from fund_flow_forward_evidence import (
    append_events,
    materialize_observations,
    migrate_legacy_csv,
    persist_immutable_freezes,
    read_events,
    record_ledger_checkpoint,
    verify_ledger_checkpoint,
)
from research_integrity import AShareTradingCalendar, DuplicateRecordError, hash_chain_record


def calendar_fixture() -> AShareTradingCalendar:
    return AShareTradingCalendar([
        "2026-06-18",
        # 2026-06-19 is an exchange holiday and is deliberately absent.
        "2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26",
        "2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03",
        "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10",
        "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
        "2026-07-20", "2026-07-21", "2026-07-22",
    ], source="self_check_fixture")


def qualified_source() -> pd.DataFrame:
    return pd.DataFrame([{
        "dual_positive_flow": True,
        "industry_code": "801001",
        "industry_name": "测试行业",
        "window_signal_pass": True,
        "valuation_gate_pass": True,
        "stabilization_gate_pass": True,
        "window_id": "w1",
        "frozen_selection_rule_id": "r1",
    }])


def observation_kwargs(detected_at: datetime) -> dict:
    return {
        "calendar": calendar_fixture(),
        "detected_at": detected_at,
        "source_artifact": "fixture.csv",
        "source_fingerprint": "a" * 64,
        "source_fingerprint_status": "verified_snapshot",
        "calendar_fingerprint": "b" * 64,
        "calendar_artifact": "self_check_fixture",
        "cohort": {"cohort_id": "c1", "manifest_hash": "c" * 64, "freeze_passed": True},
    }


def test_a_share_calendar_skips_exchange_holiday_and_holding_uses_sessions() -> None:
    calendar = calendar_fixture()
    assert calendar.next_trading_day("2026-06-18") == date(2026, 6, 22)
    assert calendar.holding_exit(date(2026, 6, 23), 20) == date(2026, 7, 21)


def test_integrity_audit_recomputes_calendar_schedule_and_rejects_holiday_signal(tmp_path: Path) -> None:
    calendar = calendar_fixture()
    calendar_path = tmp_path / "calendar.csv"
    research_integrity.atomic_write_csv(
        calendar_path,
        ({"trade_date": item.isoformat()} for item in calendar.dates),
        fieldnames=["trade_date"],
    )
    row = {
        "calendar_source": "calendar.csv",
        "signal_date": "2026-06-22",
        "planned_entry_date": "2026-06-23",
        "planned_exit_date": "2026-07-21",
    }
    assert v530.calendar_schedule_valid(row, tmp_path) is True
    assert v530.calendar_schedule_valid({**row, "planned_exit_date": "2026-07-22"}, tmp_path) is False
    holiday_signal = {**row, "signal_date": "2026-06-19", "planned_entry_date": "2026-06-22", "planned_exit_date": "2026-07-20"}
    assert v530.calendar_schedule_valid(holiday_signal, tmp_path) is False


def test_observation_source_bundle_recomputes_signal_and_candidate_fields(tmp_path: Path) -> None:
    candidate_path = tmp_path / "candidate.csv"
    summary_path = tmp_path / "summary.json"
    manifest_path = tmp_path / "manifest.json"
    source_row = {
        "industry_code": "801001", "industry_name": "A", "dual_positive_flow": True,
        "today_flow_positive": True, "five_day_flow_positive": True,
        "window_signal_pass": True, "valuation_gate_pass": True, "stabilization_gate_pass": True,
        "window_id": "w1", "frozen_selection_rule_id": "r1", "selection_score": "1.0",
        "fund_flow_research_status": "verified", "fund_flow_overlay_status": "pass",
        "ths_industry_name": "A", "ths_today_net_flow": "10.0", "ths_5d_net_flow": "20.0",
        "historical_failure_flag": False,
    }
    research_integrity.atomic_write_csv(candidate_path, [source_row], fieldnames=list(source_row))
    research_integrity.atomic_write_json(summary_path, {"latest_cache_date": "2026-01-02"})
    manifest = {
        "bundle_version": "fund_flow_observation_source_bundle_v1",
        "signal_date": "2026-01-02",
        "candidate_artifact": candidate_path.name,
        "candidate_fingerprint": research_integrity.file_sha256(candidate_path),
        "candidate_row_count": 1,
        "summary_artifact": summary_path.name,
        "summary_fingerprint": research_integrity.file_sha256(summary_path),
    }
    research_integrity.atomic_write_json(manifest_path, manifest)
    row = {
        **source_row,
        "record_schema_version": "2.1",
        "signal_date": "2026-01-02",
        "source_artifact": manifest_path.name,
        "source_fingerprint": research_integrity.file_sha256(manifest_path),
        "source_fingerprint_status": "verified_bundle",
    }
    assert v530.source_fingerprint_valid(row, tmp_path)
    research_integrity.atomic_write_json(summary_path, {"latest_cache_date": "2026-01-03"})
    assert not v530.source_fingerprint_valid(row, tmp_path)


def test_price_source_snapshot_reproduces_candidate_and_full_benchmark(tmp_path: Path) -> None:
    source_path = tmp_path / "prices.csv"
    source_rows = [
        {"trade_date": "2026-01-02", "industry_code": f"{index:06d}", "close_index": f"{99 + index:.10f}"}
        for index in range(1, 101)
    ]
    research_integrity.atomic_write_csv(source_path, source_rows, fieldnames=list(source_rows[0]))
    fingerprint = research_integrity.file_sha256(source_path)
    candidate = {
        "planned_entry_date": "2026-01-02", "industry_code": "000001",
        "entry_close_index": "100.0000000000", "freeze_source": source_path.name,
        "source_fingerprint": fingerprint,
    }
    assert v530.price_source_contains_freeze(candidate, tmp_path)
    benchmark = [
        {
            "planned_entry_date": "2026-01-02", "industry_code": f"{index:06d}",
            "entry_close_index": f"{99 + index:.10f}", "freeze_source": source_path.name,
            "source_fingerprint": fingerprint,
        }
        for index in range(1, 101)
    ]
    assert v530.benchmark_source_reproduces_group(benchmark, tmp_path)
    manifest_before, count = v530.evidence_artifact_manifest([], pd.DataFrame([candidate]), pd.DataFrame(benchmark), tmp_path)
    assert count == 1
    benchmark[0]["entry_close_index"] = "101.0000000000"
    assert not v530.benchmark_source_reproduces_group(benchmark, tmp_path)
    research_integrity.atomic_write_csv(source_path, [{**source_rows[0], "close_index": "999.0"}, *source_rows[1:]], fieldnames=list(source_rows[0]))
    manifest_after, _ = v530.evidence_artifact_manifest([], pd.DataFrame([candidate]), pd.DataFrame(benchmark), tmp_path)
    assert manifest_after != manifest_before


def test_v21_event_schema_is_executed_and_requires_verified_bundle() -> None:
    _, events, _, _ = v530._self_check_fixture()
    event = {
        **events[0],
        "record_schema_version": "2.1",
        "source_fingerprint_status": "verified_bundle",
    }
    valid, detail = v530.event_schema_valid([event])
    assert valid, detail
    invalid, detail = v530.event_schema_valid([{**event, "source_fingerprint_status": "verified_snapshot"}])
    assert not invalid
    assert "conditional-const:source_fingerprint_status" in detail


def test_source_gate_failure_cannot_be_labeled_goal_qualified() -> None:
    row = {
        "detected_at_utc": "2026-01-01T08:00:00Z",
        "evidence_cutoff": "2026-01-01T07:00:00Z",
        "entry_cutoff": "2026-01-02T01:30:00Z",
        "cohort_id": "c1", "cohort_manifest_hash": "c" * 64,
        "window_signal_pass": False, "valuation_gate_pass": True, "stabilization_gate_pass": True,
        "window_id": "w1", "frozen_selection_rule_id": "r1",
        "integrity_eligible": True, "qualified_for_goal": True, "promotion_eligible": True,
        "sample_scope": "goal_qualified",
    }
    valid, reason = v530.qualification_flags_consistent(row, source_ok=True, calendar_ok=True)
    assert not valid
    assert "base_qualified=False" in reason


def test_v530_rejects_old_signal_bound_to_newer_active_cohort() -> None:
    row, events, entry, benchmark = v530._self_check_fixture()
    checks, violations = v530.audit_rows(
        [row],
        date(2026, 1, 2),
        entry,
        benchmark,
        events=events,
        allow_fixture_fingerprints=True,
        cohort_created_at="2026-01-01T08:00:00Z",
    )
    assert any(item["violation"] == "retroactive_cohort_ownership" for item in violations)
    assert checks.loc[checks["check"].eq("cohort_creation_precedes_evidence"), "status"].eq("fail").all()
    assert checks.loc[checks["check"].eq("qualification_flags_consistent"), "status"].eq("fail").all()


def test_materialized_csv_must_match_authoritative_materialization(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    rows = [v530.with_schema_fields({"batch_id": "b1", "industry_code": "801001"})]
    research_integrity.atomic_write_csv(path, rows, fieldnames=v530.LEDGER_FIELDS)
    matched, detail = v530.materialized_csv_consistency(rows, path)
    assert matched, detail
    tampered = [{**rows[0], "batch_id": "changed"}]
    research_integrity.atomic_write_csv(path, tampered, fieldnames=v530.LEDGER_FIELDS)
    matched, _ = v530.materialized_csv_consistency(rows, path)
    assert not matched


def test_post_entry_registration_is_permanently_excluded() -> None:
    rows = v525.build_observations(
        qualified_source(),
        "2026-06-22",
        **observation_kwargs(datetime.fromisoformat("2026-06-23T02:00:00+00:00")),
    )
    assert rows.loc[0, "late_backfill_excluded"] is True or str(rows.loc[0, "late_backfill_excluded"]).lower() == "true"
    assert not v525.is_true(rows.loc[0, "qualified_for_goal"])
    assert rows.loc[0, "sample_scope"] == "exploratory_fund_flow_only"
    assert "late_backfill_excluded" in rows.loc[0, "qualification_reason"]


def test_pre_evidence_observation_waits_without_emitting_authoritative_row() -> None:
    before_window = v525.build_observations(
        qualified_source(),
        "2026-06-22",
        **observation_kwargs(datetime.fromisoformat("2026-06-22T06:59:59+00:00")),
    )
    assert before_window.empty
    at_window = v525.build_observations(
        qualified_source(),
        "2026-06-22",
        **observation_kwargs(datetime.fromisoformat("2026-06-22T07:00:00+00:00")),
    )
    assert len(at_window) == 1
    assert at_window.loc[0, "late_backfill_excluded"] is False or str(at_window.loc[0, "late_backfill_excluded"]).lower() == "false"


def test_observation_retry_is_idempotent_but_changed_evidence_conflicts() -> None:
    first = v525.build_observations(
        qualified_source(), "2026-06-22",
        **observation_kwargs(datetime.fromisoformat("2026-06-22T08:00:00+00:00")),
    )
    retry = v525.build_observations(
        qualified_source(), "2026-06-22",
        **observation_kwargs(datetime.fromisoformat("2026-06-22T08:05:00+00:00")),
    )
    assert v525.first_write_wins_observations(retry, first.to_dict("records")).empty
    changed = retry.copy()
    changed.loc[0, "source_fingerprint"] = "f" * 64
    with pytest.raises(DuplicateRecordError, match="changed immutable evidence"):
        v525.first_write_wins_observations(changed, first.to_dict("records"))


def test_exploratory_observation_ids_are_cohort_scoped() -> None:
    exploratory = pd.DataFrame([{"dual_positive_flow": True, "industry_code": "801001", "industry_name": "A"}])
    kwargs = observation_kwargs(datetime.fromisoformat("2026-06-22T08:00:00+00:00"))
    first = v525.build_observations(exploratory, "2026-06-22", **kwargs)
    second = v525.build_observations(
        exploratory,
        "2026-06-22",
        **{**kwargs, "cohort": {"cohort_id": "c2", "manifest_hash": "d" * 64, "freeze_passed": True}},
    )
    assert first.loc[0, "observation_id"] != second.loc[0, "observation_id"]
    assert first.loc[0, "batch_id"] != second.loc[0, "batch_id"]


def test_new_cohort_cannot_retroactively_own_an_old_signal() -> None:
    kwargs = observation_kwargs(datetime.fromisoformat("2026-06-23T02:00:00+00:00"))
    rows = v525.build_observations(
        qualified_source(),
        "2026-06-22",
        **{
            **kwargs,
            "cohort": {
                "cohort_id": "created-too-late",
                "manifest_hash": "d" * 64,
                "freeze_passed": True,
                "created_at_utc": "2026-06-22T08:00:00Z",
            },
        },
    )
    assert rows.empty


def test_tampered_active_creation_time_cannot_retroactively_own_old_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    history_path = root / "logs" / "cohort_history.jsonl"
    active_path = root / "logs" / "active.json"
    manifest_path = root / "logs" / "cohorts" / "late" / "manifest.csv"
    manifest_rows = [{"artifact_id": "method.py", "artifact_type": "script_sha256", "fingerprint": "a" * 64}]
    manifest_hash = v531.manifest_fingerprint(manifest_rows)
    research_integrity.atomic_write_csv(
        manifest_path,
        manifest_rows,
        fieldnames=["artifact_id", "artifact_type", "fingerprint"],
    )
    monkeypatch.setattr(v531, "ROOT", root)
    monkeypatch.setattr(v531, "HISTORY", history_path)
    monkeypatch.setattr(v531, "ACTIVE", active_path)
    monkeypatch.setattr(v531, "LEGACY_BASELINE", root / "logs" / "legacy.csv")
    monkeypatch.setattr(v531, "current_manifest", lambda: list(manifest_rows))
    monkeypatch.setattr(v531, "utc_now_text", lambda: "2026-06-22T08:00:00Z")
    history_record = v531.append_history(
        cohort_id="late",
        manifest_hash=manifest_hash,
        manifest_path="logs/cohorts/late/manifest.csv",
        operator="tester",
        reason="test",
        previous={},
    )
    research_integrity.atomic_write_json(active_path, {
        "cohort_id": "late",
        "manifest_hash": manifest_hash,
        "manifest_path": "logs/cohorts/late/manifest.csv",
        "freeze_passed": True,
        "created_at_utc": "2026-06-22T06:00:00Z",
        "history_record_hash": history_record["record_hash"],
    })

    validated = v531.validated_active_cohort()
    assert validated["freeze_passed"] is False
    assert validated["created_at_utc"] == "2026-06-22T08:00:00Z"
    assert "creation timestamp differs" in validated["validation_reason"]
    kwargs = observation_kwargs(datetime.fromisoformat("2026-06-23T02:00:00+00:00"))
    rows = v525.build_observations(
        qualified_source(),
        "2026-06-22",
        **{**kwargs, "cohort": validated},
    )
    assert rows.empty


def test_entry_freeze_window_boundaries_are_pending_on_time_then_late() -> None:
    holding = pd.DataFrame([{
        "batch_id": "b", "observation_id": "o", "industry_code": "000001", "industry_name": "A",
        "planned_entry_date": "2026-01-02", "planned_exit_date": "2026-01-10",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }])
    current = pd.DataFrame({
        "trade_date": [date(2026, 1, 2)] * 100,
        "industry_code": [f"{index:06d}" for index in range(1, 101)],
        "industry_name": [str(index) for index in range(1, 101)],
        "close_index": [100.0] * 100,
    })
    attempts = {
        "before": (datetime.fromisoformat("2026-01-02T06:59:00+00:00"), "freeze_window_pending"),
        "inside": (datetime.fromisoformat("2026-01-02T07:30:00+00:00"), "frozen_on_time"),
        "after": (datetime.fromisoformat("2026-01-02T08:01:00+00:00"), "late_backfill_excluded"),
    }
    for _, (attempted_at, expected) in attempts.items():
        candidate = v533.build_freeze(holding, current, current, date(2026, 1, 2), freeze_at=attempted_at)
        assert candidate.loc[0, "entry_price_freeze_status"] == expected
        benchmark = v534.build_freeze(candidate, current, current, date(2026, 1, 2), freeze_at=attempted_at)
        assert benchmark["benchmark_entry_freeze_status"].eq(expected).all()
        persistable_candidate = v533.terminal_freezes(candidate, "entry_price_freeze_status")
        persistable_benchmark = v534.terminal_freezes(benchmark, "benchmark_entry_freeze_status")
        if expected == "freeze_window_pending":
            assert persistable_candidate.empty
            assert persistable_benchmark.empty
        else:
            assert not persistable_candidate.empty
            assert not persistable_benchmark.empty


def test_post_entry_candidate_and_benchmark_freezes_are_excluded() -> None:
    holding = pd.DataFrame([{
        "batch_id": "b", "industry_code": "000001", "industry_name": "A",
        "planned_entry_date": "2026-01-02", "planned_exit_date": "2026-01-10",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }])
    current = pd.DataFrame({
        "trade_date": [date(2026, 1, 2)] * 100,
        "industry_code": [f"{index:06d}" for index in range(1, 101)],
        "industry_name": [str(index) for index in range(1, 101)],
        "close_index": [100.0] * 100,
    })
    candidate = v533.build_freeze(
        holding,
        current,
        current,
        date(2026, 1, 5),
        freeze_at=datetime.fromisoformat("2026-01-05T07:30:00+00:00"),
    )
    assert candidate.loc[0, "entry_price_freeze_status"] == "late_backfill_excluded"
    benchmark = v534.build_freeze(
        candidate,
        current,
        current,
        date(2026, 1, 5),
        freeze_at=datetime.fromisoformat("2026-01-05T07:30:00+00:00"),
    )
    assert len(benchmark) == 100
    assert benchmark["benchmark_entry_freeze_status"].eq("late_backfill_excluded").all()


def test_next_day_rerun_cannot_rewrite_on_time_freeze_as_late(tmp_path: Path) -> None:
    ledger = tmp_path / "candidate_freezes.jsonl"
    base = {
        "cohort_id": "c1",
        "cohort_manifest_hash": "h1",
        "batch_id": "b1",
        "observation_id": "o1",
        "industry_code": "801001",
        "planned_entry_date": "2026-01-02",
        "actual_entry_date": "2026-01-02",
        "entry_close_index": "100.0000000000",
    }
    on_time = {**base, "entry_price_freeze_status": "frozen_on_time", "freeze_at_utc": "2026-01-02T07:30:00Z"}
    events, appended = persist_immutable_freezes(
        ledger,
        [on_time],
        freeze_kind="candidate_entry_freeze",
        key_fields=["cohort_id", "cohort_manifest_hash", "batch_id", "observation_id", "industry_code", "planned_entry_date"],
        status_field="entry_price_freeze_status",
    )
    assert appended == 1
    assert events[0]["entry_price_freeze_status"] == "frozen_on_time"

    recomputed_late = {**base, "entry_price_freeze_status": "late_backfill_excluded", "freeze_at_utc": "2026-01-05T07:30:00Z"}
    events, appended = persist_immutable_freezes(
        ledger,
        [recomputed_late],
        freeze_kind="candidate_entry_freeze",
        key_fields=["cohort_id", "cohort_manifest_hash", "batch_id", "observation_id", "industry_code", "planned_entry_date"],
        status_field="entry_price_freeze_status",
    )
    assert appended == 0
    assert len(events) == 1
    assert events[0]["entry_price_freeze_status"] == "frozen_on_time"
    assert events[0]["freeze_at_utc"] == "2026-01-02T07:30:00Z"


def test_missing_baseline_and_newly_created_baseline_do_not_pass() -> None:
    sample = [{"artifact_id": "a", "artifact_type": "script_sha256", "fingerprint": "1"}]
    comparison = v531.compare(sample, sample)
    missing = v531.build_summary(
        sample, comparison, baseline_exists=False, created=False, cohort_id="c1",
        manifest_hash="h1", baseline_file=v531.BASELINE_DIR / "c1" / "manifest.csv", previous={},
    )
    created = v531.build_summary(
        sample, comparison, baseline_exists=True, created=True, cohort_id="c1",
        manifest_hash="h1", baseline_file=v531.BASELINE_DIR / "c1" / "manifest.csv", previous={},
    )
    assert missing["freeze_passed"] is False
    assert created["freeze_passed"] is False
    assert created["verification_required"] is True


def test_successful_second_verification_clears_transient_invalidation_metadata() -> None:
    verified = v531.verified_active_pointer({
        "cohort_id": "c1",
        "freeze_passed": False,
        "verification_required": True,
        "invalidated_at_utc": "2026-01-01T00:00:00Z",
        "invalidation_reason": "new cohort requires independent verification",
    }, "2026-01-01T00:01:00Z")
    assert verified["freeze_passed"] is True
    assert verified["verification_required"] is False
    assert verified["verified_at_utc"] == "2026-01-01T00:01:00Z"
    assert "invalidated_at_utc" not in verified
    assert "invalidation_reason" not in verified


def test_empty_cohort_aware_summaries_still_declare_the_active_pair() -> None:
    active = {"cohort_id": "c1", "manifest_hash": "h1", "freeze_passed": True}
    checks = pd.DataFrame([{"status": "pending"}])
    observer_columns = ["qualified_for_goal", "late_backfill_excluded", "planned_entry_date", "planned_exit_date"]
    observer = v525.build_summary(
        pd.DataFrame(columns=observer_columns),
        pd.DataFrame(columns=observer_columns),
        pd.DataFrame(columns=observer_columns),
        checks,
        False,
        active_cohort=active,
    )
    entry = v533.build_summary(pd.DataFrame(), checks, date(2026, 7, 18), active_cohort=active)
    benchmark = v534.build_summary(pd.DataFrame(), checks, date(2026, 7, 18), active_cohort=active)
    for summary in (observer, entry, benchmark):
        assert summary["active_cohort_id"] == "c1"
        assert summary["active_cohort_manifest_hash"] == "h1"
        assert summary["active_cohort_freeze_passed"] is True


def test_existing_baseline_cannot_be_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "cohort" / "manifest.csv"
    rows = [{"artifact_id": "a", "artifact_type": "script_sha256", "fingerprint": "1"}]
    v531.write_manifest(path, rows)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        v531.write_manifest(path, [{**rows[0], "fingerprint": "changed"}])
    assert path.read_bytes() == original


def test_active_cohort_pointer_is_revalidated_against_current_manifest_and_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    active_path = tmp_path / "active.json"
    history_path = tmp_path / "history.jsonl"
    baseline_path = tmp_path / "baseline.csv"
    manifest = [{"artifact_id": "a", "artifact_type": "script_sha256", "fingerprint": "1"}]
    research_integrity.atomic_write_csv(baseline_path, manifest, fieldnames=["artifact_id", "artifact_type", "fingerprint"])
    manifest_hash = v531.manifest_fingerprint(manifest)
    history_record = research_integrity.HashChainLedger(history_path).append({
        "event_type": "cohort_created",
        "event_id": "cohort:c1",
        "created_at_utc": "2026-01-01T06:00:00Z",
        "cohort_id": "c1",
        "manifest_hash": manifest_hash,
        "manifest_path": str(baseline_path),
    }, unique_fields=["event_id"])
    record_ledger_checkpoint(history_path)
    research_integrity.atomic_write_json(active_path, {
        "cohort_id": "c1",
        "manifest_hash": manifest_hash,
        "manifest_path": str(baseline_path),
        "freeze_passed": True,
        "created_at_utc": "2026-01-01T06:00:00Z",
        "history_record_hash": history_record["record_hash"],
    })
    monkeypatch.setattr(v531, "ACTIVE", active_path)
    monkeypatch.setattr(v531, "HISTORY", history_path)
    monkeypatch.setattr(v531, "current_manifest", lambda: manifest)
    assert v531.validated_active_cohort()["freeze_passed"] is True
    monkeypatch.setattr(v531, "current_manifest", lambda: [{**manifest[0], "fingerprint": "changed"}])
    invalid = v531.validated_active_cohort()
    assert invalid["freeze_passed"] is False
    assert "drifted" in invalid["validation_reason"]


def settlement_fixture(benchmark_count: int) -> tuple[dict, pd.DataFrame, dict, dict]:
    hist_rows = []
    for index in range(1, 101):
        code = f"{index:06d}"
        hist_rows.extend([
            {"trade_date": date(2026, 1, 2), "industry_code": code, "industry_name": code, "close_index": 100.0},
            {"trade_date": date(2026, 1, 10), "industry_code": code, "industry_name": code, "close_index": 105.0},
        ])
    hist = pd.DataFrame(hist_rows)
    row = {
        "batch_id": "b", "industry_code": "000001", "planned_entry_date": "2026-01-02",
        "planned_exit_date": "2026-01-10", "settlement_status": "not_due",
        "qualified_for_goal": "True", "late_backfill_excluded": "False",
        "integrity_eligible": "True", "promotion_eligible": "True",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }
    entry = {"c1|h1|b|000001": {
        "entry_price_freeze_status": "frozen_on_time", "entry_close_index": "100",
        "actual_entry_date": "2026-01-02", "planned_entry_date": "2026-01-02",
        "as_of_date": "2026-01-02", "freeze_at_utc": "2026-01-02T07:30:00Z",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }}
    template = {
        "benchmark_entry_freeze_status": "frozen_on_time", "entry_close_index": "100",
        "actual_entry_date": "2026-01-02", "planned_entry_date": "2026-01-02",
        "as_of_date": "2026-01-02", "freeze_at_utc": "2026-01-02T07:30:00Z",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }
    benchmark = {"c1|h1|b|2026-01-02": {f"{index:06d}": dict(template) for index in range(1, benchmark_count + 1)}}
    return row, hist, entry, benchmark


@pytest.mark.parametrize("count", [2, 99])
def test_benchmark_below_100_stays_pending(count: int) -> None:
    row, hist, entry, benchmark = settlement_fixture(count)
    result = v527.settle_one(row, hist, date(2026, 1, 10), entry, benchmark, integrity_passed=True, eligible_cohorts={("c1", "h1")})
    assert result["settlement_status"] == "pending_insufficient_benchmark_universe"


def test_missing_exact_exit_date_does_not_roll_forward() -> None:
    row, hist, entry, benchmark = settlement_fixture(100)
    hist.loc[hist["trade_date"].eq(date(2026, 1, 10)), "trade_date"] = date(2026, 1, 11)
    result = v527.settle_one(row, hist, date(2026, 1, 11), entry, benchmark, integrity_passed=True, eligible_cohorts={("c1", "h1")})
    assert result["settlement_status"] == "pending_missing_price"


def test_direct_settlement_cannot_bypass_integrity_gate() -> None:
    row, hist, entry, benchmark = settlement_fixture(100)
    result = v527.settle_one(row, hist, date(2026, 1, 10), entry, benchmark)
    assert result["settlement_status"] == "pending_integrity_gate_failed"


def test_same_manifest_hash_from_different_cohort_cannot_authorize_settlement() -> None:
    row, hist, entry, benchmark = settlement_fixture(100)
    row["cohort_id"] = "old-cohort"
    result = v527.settle_one(
        row,
        hist,
        date(2026, 1, 10),
        entry,
        benchmark,
        integrity_passed=True,
        eligible_cohorts={("active-cohort", "h1")},
    )
    assert result["settlement_status"] == "pending_integrity_gate_failed"


def test_operational_views_and_promotion_only_use_active_cohort_pair() -> None:
    frame = pd.DataFrame([
        {"cohort_id": "old", "cohort_manifest_hash": "same", "batch_id": "old-b", "industry_code": "000001"},
        {"cohort_id": "active", "cohort_manifest_hash": "same", "batch_id": "new-b", "industry_code": "000002"},
    ])
    active = {"freeze_passed": True, "cohort_id": "active", "manifest_hash": "same"}
    for scoped in [
        v526.filter_active_cohort(frame, active),
        v528.filter_active_cohort(frame, active),
        v532.filter_active_cohort(frame, active),
    ]:
        assert len(scoped) == 1
        assert scoped.iloc[0]["batch_id"] == "new-b"
    invalid = {"freeze_passed": False, "cohort_id": "active", "manifest_hash": "same"}
    assert v526.filter_active_cohort(frame, invalid).empty
    assert v528.filter_active_cohort(frame, invalid).empty
    assert v532.filter_active_cohort(frame, invalid).empty


@pytest.mark.parametrize("module", [v526, v529, v532, v535])
def test_operational_views_never_fallback_to_compatibility_csv(
    module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_ledger = tmp_path / "missing.jsonl"
    compatibility = tmp_path / "ledger.csv"
    research_integrity.atomic_write_csv(compatibility, [{"batch_id": "legacy"}], fieldnames=["batch_id"])
    monkeypatch.setattr(module, "EVENT_LEDGER", event_ledger)
    monkeypatch.setattr(module, "LEDGER", compatibility)
    with pytest.raises(RuntimeError, match="authoritative V5.25 JSONL ledger is missing"):
        module.read_ledger()


def test_exit_day_before_shanghai_close_cannot_settle_or_create_future_fields() -> None:
    row, hist, entry, benchmark = settlement_fixture(100)
    before_close = datetime.fromisoformat("2026-01-10T06:59:59+00:00")
    result = v527.settle_one(
        row,
        hist,
        date(2026, 1, 10),
        entry,
        benchmark,
        integrity_passed=True,
        eligible_cohorts={("c1", "h1")},
        now_utc=before_close,
    )
    assert result["settlement_status"] == "pending_exit_market_close"
    assert "realized_return" not in result
    assert "actual_exit_date" not in result
    rows, new_settled = v527.settle_rows(
        [row],
        hist,
        date(2026, 1, 10),
        entry,
        benchmark,
        integrity={
            "integrity_passed": True,
            "eligible_cohorts": [{"cohort_id": "c1", "manifest_hash": "h1"}],
        },
        active_cohort={"freeze_passed": True, "cohort_id": "c1", "manifest_hash": "h1"},
        now_utc=before_close,
    )
    assert rows[0]["settlement_status"] == "pending_exit_market_close"
    assert new_settled == []

    at_close = datetime.fromisoformat("2026-01-10T07:00:00+00:00")
    settled = v527.settle_one(
        row,
        hist,
        date(2026, 1, 10),
        entry,
        benchmark,
        integrity_passed=True,
        eligible_cohorts={("c1", "h1")},
        now_utc=at_close,
    )
    assert settled["settlement_status"] == "settled"


def test_candidate_and_benchmark_selected_entry_prices_must_match() -> None:
    row, hist, entry, benchmark = settlement_fixture(100)
    benchmark["c1|h1|b|2026-01-02"]["000001"]["entry_close_index"] = "101"
    result = v527.settle_one(
        row,
        hist,
        date(2026, 1, 10),
        entry,
        benchmark,
        integrity_passed=True,
        eligible_cohorts={("c1", "h1")},
        now_utc=datetime.fromisoformat("2026-01-10T07:00:00+00:00"),
    )
    assert result["settlement_status"] == "pending_entry_freeze_price_mismatch"
    assert "realized_return" not in result


def test_settlement_persists_exact_exit_source_snapshot(tmp_path: Path) -> None:
    row, hist, entry, benchmark = settlement_fixture(100)
    result = v527.settle_one(
        row,
        hist,
        date(2026, 1, 10),
        entry,
        benchmark,
        integrity_passed=True,
        eligible_cohorts={("c1", "h1")},
    )
    settled = {**row, **result}
    source = v527.build_settlement_source_snapshot(settled, hist, entry, benchmark, output_dir=tmp_path)
    retry_source = v527.build_settlement_source_snapshot(settled, hist, entry, benchmark, output_dir=tmp_path)
    assert retry_source == source
    artifact = Path(source["settlement_source_artifact"])
    assert artifact.is_file()
    assert source["settlement_source_row_count"] == "100"
    assert source["settlement_source_fingerprint"] == research_integrity.file_sha256(artifact)


def test_duplicate_append_is_idempotent_but_conflict_is_rejected(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    payload = {"event_type": "observation", "event_id": "e1", "observation_id": "o1", "value": "1"}
    assert len(append_events(ledger, [payload])) == 1
    assert append_events(ledger, [payload]) == []
    with pytest.raises(DuplicateRecordError):
        append_events(ledger, [{**payload, "value": "changed"}])
    assert len(read_events(ledger)) == 1


def test_nonempty_ledger_without_checkpoint_refuses_ordinary_append(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    first = {"event_type": "observation", "event_id": "e1", "observation_id": "o1"}
    append_events(ledger, [first])
    original = ledger.read_bytes()
    checkpoint = Path(verify_ledger_checkpoint(ledger)["checkpoint_path"])
    checkpoint.unlink()

    with pytest.raises(research_integrity.HashChainError, match="checkpoint is missing"):
        append_events(
            ledger,
            [{"event_type": "observation", "event_id": "e2", "observation_id": "o2"}],
        )
    assert ledger.read_bytes() == original
    assert not checkpoint.exists()

    # Even an otherwise idempotent duplicate may not bypass the missing
    # checkpoint gate, because the verified prefix itself is no longer anchored.
    with pytest.raises(research_integrity.HashChainError, match="checkpoint is missing"):
        append_events(ledger, [first])
    assert not checkpoint.exists()


def test_explicit_checkpoint_bootstrap_restores_appendability(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    research_integrity.HashChainLedger(ledger).append(
        {"event_type": "observation", "event_id": "e1", "observation_id": "o1"},
        unique_fields=["event_id"],
    )

    with pytest.raises(research_integrity.HashChainError, match="checkpoint is missing"):
        append_events(
            ledger,
            [{"event_type": "observation", "event_id": "e2", "observation_id": "o2"}],
        )

    record_ledger_checkpoint(ledger)
    appended = append_events(
        ledger,
        [{"event_type": "observation", "event_id": "e2", "observation_id": "o2"}],
    )
    assert len(appended) == 1
    assert verify_ledger_checkpoint(ledger)["event_count"] == 2


def test_v525_noop_read_still_requires_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = tmp_path / "observations.jsonl"
    append_events(ledger, [{"event_type": "observation", "event_id": "o1:observation", "observation_id": "o1"}])
    Path(verify_ledger_checkpoint(ledger)["checkpoint_path"]).unlink()
    monkeypatch.setattr(v525, "EVENT_LEDGER", ledger)
    monkeypatch.setattr(v525, "LEDGER", tmp_path / "compatibility.csv")
    with pytest.raises(research_integrity.HashChainError, match="checkpoint is missing"):
        v525.read_ledger()


def test_v531_history_missing_checkpoint_cannot_be_silently_reanchored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = tmp_path / "cohorts.jsonl"
    monkeypatch.setattr(v531, "HISTORY", history)
    monkeypatch.setattr(v531, "LEGACY_BASELINE", tmp_path / "legacy.csv")
    v531.append_history(
        cohort_id="c1", manifest_hash="a" * 64, manifest_path="logs/c1.csv",
        operator="tester", reason="bootstrap", previous={},
    )
    Path(verify_ledger_checkpoint(history)["checkpoint_path"]).unlink()
    with pytest.raises(research_integrity.HashChainError, match="checkpoint is missing"):
        v531.append_history(
            cohort_id="c2", manifest_hash="b" * 64, manifest_path="logs/c2.csv",
            operator="tester", reason="must fail", previous={"cohort_id": "c1"},
        )


def test_immutable_freeze_persistence_does_not_rebuild_missing_checkpoint(tmp_path: Path) -> None:
    ledger = tmp_path / "freeze.jsonl"
    rows = [{"industry_code": "801001", "freeze_status": "frozen_on_time"}]
    persisted, appended_count = persist_immutable_freezes(
        ledger,
        rows,
        freeze_kind="entry_price_freeze",
        key_fields=["industry_code"],
        status_field="freeze_status",
    )
    assert len(persisted) == 1
    assert appended_count == 1
    checkpoint = Path(verify_ledger_checkpoint(ledger)["checkpoint_path"])
    checkpoint.unlink()

    with pytest.raises(research_integrity.HashChainError, match="checkpoint is missing"):
        persist_immutable_freezes(
            ledger,
            rows,
            freeze_kind="entry_price_freeze",
            key_fields=["industry_code"],
            status_field="freeze_status",
        )
    assert not checkpoint.exists()


def test_interrupted_append_preserves_verified_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_events(ledger, [{"event_type": "observation", "event_id": "e1", "observation_id": "o1"}])
    original = ledger.read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated interruption")

    monkeypatch.setattr(research_integrity.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        append_events(ledger, [{"event_type": "observation", "event_id": "e2", "observation_id": "o2"}])
    assert ledger.read_bytes() == original


def test_valid_hash_prefix_rollback_is_detected_by_independent_head_checkpoint(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_events(ledger, [{"event_type": "observation", "event_id": "e1", "observation_id": "o1"}])
    append_events(ledger, [{"event_type": "observation", "event_id": "e2", "observation_id": "o2"}])
    assert verify_ledger_checkpoint(ledger)["event_count"] == 2
    lines = ledger.read_bytes().splitlines(keepends=True)
    ledger.write_bytes(lines[0])
    research_integrity.verify_hash_chain(ledger)
    with pytest.raises(research_integrity.HashChainError, match="rolled back|checkpoint"):
        verify_ledger_checkpoint(ledger)


def test_v529_refuses_legal_prefix_rollback_before_materializing_calendar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = tmp_path / "calendar_input.jsonl"
    append_events(ledger, [{"event_type": "observation", "event_id": "e1", "observation_id": "o1"}])
    append_events(ledger, [{"event_type": "observation", "event_id": "e2", "observation_id": "o2"}])
    lines = ledger.read_bytes().splitlines(keepends=True)
    ledger.write_bytes(lines[0])
    monkeypatch.setattr(v529, "EVENT_LEDGER", ledger)
    monkeypatch.setattr(v529, "LEDGER", tmp_path / "fallback.csv")
    with pytest.raises(research_integrity.HashChainError, match="rolled back|checkpoint"):
        v529.read_ledger()


def test_legacy_four_rows_migrate_without_becoming_qualified(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.csv"
    shutil.copyfile(ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv", legacy)
    event_ledger = tmp_path / "ledger.jsonl"
    backup = tmp_path / "backup.csv"
    events = migrate_legacy_csv(legacy, event_ledger, backup_path=backup)
    rows = materialize_observations(events)
    assert len(rows) == 4
    assert {row["industry_name"] for row in rows} == {"保险Ⅱ", "白酒Ⅱ", "游戏Ⅱ", "一般零售"}
    assert all(row["sample_scope"] == "exploratory_fund_flow_only" for row in rows)
    assert all(str(row["qualified_for_goal"]).lower() == "false" for row in rows)
    assert all(not str(row["realized_return"]).strip() for row in rows)
    assert verify_ledger_checkpoint(event_ledger)["event_count"] == 4


def rehash_single_event(row: dict) -> dict:
    event = dict(row)
    event["previous_hash"] = research_integrity.GENESIS_HASH
    event["record_hash"] = ""
    event["record_hash"] = hash_chain_record(event)
    return event


def settlement_event_from(observation: dict, previous_hash: str, *, event_id: str = "obs1:settlement:2026-01-10") -> dict:
    event = dict(observation)
    event.update({
        "event_type": "settlement",
        "event_id": event_id,
        "parent_event_id": observation["event_id"],
        "previous_hash": previous_hash,
        "ledger_sequence": "2",
        "event_recorded_at_utc": "2026-01-10T08:00:00Z",
        "settlement_status": "settled",
        "outcome_status": "settled_forward_observation",
        "actual_entry_date": "2026-01-02",
        "actual_exit_date": "2026-01-10",
        "realized_return": "0.1000000000",
        "benchmark_return": "0.0500000000",
        "realized_relative_return": "0.0500000000",
        "future_return_rank_pct": "0.9000000000",
        "future_top_quintile": "True",
        "entry_price_freeze_status": "frozen_entry_price_used",
        "benchmark_entry_freeze_status": "frozen_benchmark_entry_used:101",
        "entry_date_exact": "True",
        "exit_date_exact": "True",
        "benchmark_universe_count_used": "101",
        "settlement_source_artifact": "logs/fixture_settlement.csv",
        "settlement_source_fingerprint": "e" * 64,
        "settlement_source_row_count": "101",
        "settlement_calculation_version": "fund_flow_forward_settlement_exact_v2",
        "record_hash": "",
    })
    event["record_hash"] = hash_chain_record(event)
    return event


def test_settlement_event_cannot_mutate_observation_identity_or_be_repeated() -> None:
    _row, observation_events, _entry, _benchmark = v530._self_check_fixture()
    observation = observation_events[0]
    settlement = settlement_event_from(observation, observation["record_hash"])
    materialized = materialize_observations([observation, settlement])
    assert materialized[0]["settlement_status"] == "settled"
    assert materialized[0]["cohort_id"] == observation["cohort_id"]

    mutated = settlement_event_from(observation, observation["record_hash"])
    mutated["cohort_id"] = "rewritten-cohort"
    mutated["record_hash"] = hash_chain_record({**mutated, "record_hash": ""})
    with pytest.raises(research_integrity.HashChainError, match="immutable field cohort_id"):
        materialize_observations([observation, mutated])

    duplicate = settlement_event_from(observation, settlement["record_hash"], event_id="obs1:settlement:revision")
    duplicate["ledger_sequence"] = "3"
    duplicate["record_hash"] = hash_chain_record({**duplicate, "record_hash": ""})
    with pytest.raises(DuplicateRecordError, match="duplicate settlement"):
        materialize_observations([observation, settlement, duplicate])

    non_terminal = settlement_event_from(observation, observation["record_hash"])
    non_terminal["settlement_status"] = "pending"
    non_terminal["record_hash"] = hash_chain_record({**non_terminal, "record_hash": ""})
    with pytest.raises(research_integrity.HashChainError, match="not terminal"):
        materialize_observations([observation, non_terminal])


def test_v527_retry_repairs_materialized_csv_after_event_append_crash(tmp_path: Path) -> None:
    event_ledger = tmp_path / "ledger.jsonl"
    materialized_csv = tmp_path / "ledger.csv"
    _row, fixture_events, _entry, _benchmark = v530._self_check_fixture()
    observation_payload = {
        key: value for key, value in fixture_events[0].items()
        if key not in {"ledger_sequence", "previous_hash", "record_hash"}
    }
    append_events(event_ledger, [observation_payload])
    observation = read_events(event_ledger)[0]
    settlement = settlement_event_from(observation, observation["record_hash"])
    settlement_payload = {
        key: value for key, value in settlement.items()
        if key not in {"ledger_sequence", "previous_hash", "record_hash"}
    }
    append_events(event_ledger, [settlement_payload])
    v527.write_materialized_csv(materialized_csv, [observation])
    _events, rows = v527.load_authoritative_state(event_ledger, materialized_csv)
    assert rows[0]["settlement_status"] == "settled"
    matched, detail = v530.materialized_csv_consistency(rows, materialized_csv)
    assert matched, detail


def test_rehashed_but_inflated_settlement_return_fails_independent_recomputation(tmp_path: Path) -> None:
    snapshot = tmp_path / "settlement.csv"
    rows = []
    for index in range(1, 101):
        rows.append({
            "industry_code": f"{index:06d}",
            "planned_entry_date": "2026-01-02",
            "planned_exit_date": "2026-01-10",
            "benchmark_entry_close_index": "100.0000000000",
            "candidate_entry_close_index": "100.0000000000" if index == 1 else "",
            "exit_close_index": "110.0000000000" if index == 1 else "105.0000000000",
            "selected_candidate": str(index == 1),
        })
    fields = list(rows[0])
    research_integrity.atomic_write_csv(snapshot, rows, fieldnames=fields, sort_rows_by=["industry_code"])
    benchmark_returns = pd.Series([0.10] + [0.05] * 99)
    benchmark = float(benchmark_returns.mean())
    rank_pct = float(benchmark_returns.rank(pct=True).iloc[0])
    row = {
        "industry_code": "000001",
        "planned_entry_date": "2026-01-02",
        "planned_exit_date": "2026-01-10",
        "realized_return": f"{0.10:.10f}",
        "benchmark_return": f"{benchmark:.10f}",
        "realized_relative_return": f"{0.10 - benchmark:.10f}",
        "future_return_rank_pct": f"{rank_pct:.10f}",
        "future_top_quintile": str(rank_pct >= 0.8),
        "benchmark_universe_count_used": "100",
        "settlement_source_artifact": str(snapshot),
        "settlement_source_fingerprint": research_integrity.file_sha256(snapshot),
        "settlement_source_row_count": "100",
        "settlement_calculation_version": "fund_flow_forward_settlement_exact_v2",
    }
    assert v530.settlement_values_valid(row, tmp_path)[0] is True
    inflated = {**row, "realized_return": "0.5000000000"}
    ok, reason = v530.settlement_values_valid(inflated, tmp_path)
    assert ok is False
    assert "realized_return" in reason

    duplicated = rows + [dict(rows[1])]
    research_integrity.atomic_write_csv(snapshot, duplicated, fieldnames=fields, sort_rows_by=["industry_code"])
    duplicate_row = {
        **row,
        "benchmark_universe_count_used": "101",
        "settlement_source_row_count": "101",
        "settlement_source_fingerprint": research_integrity.file_sha256(snapshot),
    }
    ok, reason = v530.settlement_values_valid(duplicate_row, tmp_path)
    assert ok is False
    assert "duplicate industry" in reason


def test_recorded_and_freeze_timestamps_after_entry_fail_integrity() -> None:
    row, _events, entry, benchmark = v530._self_check_fixture()
    row.update({
        "detected_at_utc": "2026-01-03T02:00:00Z",
        "event_recorded_at_utc": "2026-01-03T02:01:00Z",
        "late_backfill_excluded": "False",
    })
    event = rehash_single_event(row)
    row["record_hash"] = event["record_hash"]
    entry.loc[:, "as_of_date"] = "2026-01-03"
    entry.loc[:, "freeze_at_utc"] = "2026-01-03T07:30:00Z"
    entry.loc[:, "entry_price_freeze_status"] = "late_backfill_excluded"
    checks, violations = v530.audit_rows([row], date(2026, 1, 3), entry, benchmark, events=[event], allow_fixture_fingerprints=True)
    kinds = {item["violation"] for item in violations}
    assert "observation_timestamp_inversion" in kinds
    assert "late_candidate_entry_freeze" in kinds
    assert checks["status"].eq("fail").any()


def test_spoofed_early_detection_cannot_hide_post_entry_event_recording() -> None:
    row, _events, entry, benchmark = v530._self_check_fixture()
    row["detected_at_utc"] = "2026-01-01T12:00:00Z"
    row["event_recorded_at_utc"] = "2026-01-02T02:00:00Z"
    event = rehash_single_event(row)
    row["record_hash"] = event["record_hash"]
    _checks, violations = v530.audit_rows(
        [row], date(2026, 1, 2), entry, benchmark, events=[event], allow_fixture_fingerprints=True,
    )
    assert any(item["violation"] == "observation_timestamp_inversion" for item in violations)


def test_self_check_fingerprint_sentinels_are_rejected_in_production_mode(tmp_path: Path) -> None:
    row, _events, _entry, _benchmark = v530._self_check_fixture()
    assert v530.source_fingerprint_valid(row, tmp_path) is False
    assert v530.calendar_fingerprint_valid(row, tmp_path) is False
    assert v530.source_fingerprint_valid(row, tmp_path, allow_fixture_fingerprints=True) is True
    assert v530.calendar_fingerprint_valid(row, tmp_path, allow_fixture_fingerprints=True) is True


def test_legacy_violations_are_reported_globally_without_poisoning_clean_active_cohort() -> None:
    active_row, active_events, entry, benchmark = v530._self_check_fixture()
    legacy_row = dict(active_row)
    legacy_row.update({
        "event_id": "legacy:observation",
        "observation_id": "legacy",
        "batch_id": "legacy-batch",
        "industry_code": "801999",
        "cohort_id": "legacy",
        "cohort_manifest_hash": "UNVERIFIED_LEGACY_COHORT",
        "detected_at_utc": "2026-01-02T02:00:00Z",
        "event_recorded_at_utc": "2026-01-02T02:01:00Z",
        "late_backfill_excluded": "True",
        "integrity_eligible": "False",
        "promotion_eligible": "False",
        "qualified_for_goal": "False",
        "sample_scope": "exploratory_fund_flow_only",
        "source_fingerprint_status": "legacy_unverified",
    })
    legacy_event = dict(legacy_row, previous_hash=active_events[0]["record_hash"], record_hash="")
    legacy_event["record_hash"] = hash_chain_record(legacy_event)
    legacy_row["record_hash"] = legacy_event["record_hash"]
    events = [active_events[0], legacy_event]

    active_checks, active_violations = v530.audit_rows(
        [active_row], date(2026, 1, 2), entry, benchmark, events=events, allow_fixture_fingerprints=True,
    )
    global_checks, global_violations = v530.audit_rows(
        [active_row, legacy_row], date(2026, 1, 2), entry, benchmark, events=events, allow_fixture_fingerprints=True,
    )
    assert active_checks["status"].eq("pass").all()
    assert not active_violations
    assert global_checks["status"].eq("fail").any()
    assert global_violations

    summary = v530.build_summary(
        [active_row],
        active_checks,
        active_violations,
        date(2026, 1, 2),
        events=events,
        active_cohort={"cohort_id": "c1", "manifest_hash": "c" * 64, "freeze_passed": True},
        global_rows=[active_row, legacy_row],
        global_checks=global_checks,
        global_violations=global_violations,
    )
    assert summary["integrity_passed"] is True
    assert summary["eligible_cohort_hashes"] == ["c" * 64]
    assert summary["global_ledger_integrity_passed"] is False
    assert summary["global_late_backfill_count"] > 0


def test_exploratory_or_integrity_failed_rows_cannot_promote() -> None:
    passing = pd.DataFrame({
        "batch_id": [f"b{index}" for index in range(30)],
        "industry_code": [str(index) for index in range(30)],
        "realized_relative_return": [0.01] * 30,
        "future_top_quintile": [True] * 30,
        "entry_price_freeze_status": ["frozen_entry_price_used"] * 30,
        "benchmark_entry_freeze_status": ["frozen_benchmark_entry_used:131"] * 30,
        "settlement_status": ["settled"] * 30,
        "qualified_for_goal": [False] * 30,
        "promotion_eligible": [False] * 30,
        "integrity_eligible": [False] * 30,
        "late_backfill_excluded": [False] * 30,
        "entry_date_exact": [True] * 30,
        "exit_date_exact": [True] * 30,
        "benchmark_universe_count_used": [131] * 30,
        "cohort_id": ["c1"] * 30,
        "cohort_manifest_hash": ["h1"] * 30,
    })
    assert v528.filter_qualified_settled(passing).empty
    dependencies = v528.integrity_dependency_checks(
        passing.iloc[0:0],
        {"integrity_passed": False, "eligible_cohort_hashes": [], "late_backfill_count": 1},
        {"freeze_passed": True, "cohort_id": "c1", "manifest_hash": "h1"},
    )
    metrics = v528.promotion_checks(passing, v528.batch_metrics(passing))
    summary = v528.build_summary(
        passing,
        v528.batch_metrics(passing),
        pd.concat([dependencies, metrics], ignore_index=True),
        integrity={"integrity_passed": False, "late_backfill_count": 1},
        freeze_manifest={"freeze_passed": True, "cohort_id": "c1", "manifest_hash": "h1"},
    )
    assert summary["promotion_ready"] is False


def test_v527_current_summary_uses_only_revalidated_active_cohort() -> None:
    active = {"freeze_passed": True, "cohort_id": "active", "manifest_hash": "hash-active"}
    active_row = {
        "observation_id": "active-observation",
        "cohort_id": "active",
        "cohort_manifest_hash": "hash-active",
        "planned_exit_date": "2026-01-10",
        "settlement_status": "not_due",
        "qualified_for_goal": "True",
    }
    legacy_settled = {
        **active_row,
        "observation_id": "legacy-observation",
        "cohort_id": "legacy",
        "cohort_manifest_hash": "hash-legacy",
        "settlement_status": "settled",
    }
    integrity = {
        "integrity_passed": True,
        "eligible_cohorts": [
            {"cohort_id": "active", "manifest_hash": "hash-active"},
            {"cohort_id": "legacy", "manifest_hash": "hash-legacy"},
        ],
    }
    rows, settled = v527.settle_rows(
        [active_row, legacy_settled],
        pd.DataFrame(),
        date(2026, 1, 1),
        integrity=integrity,
        active_cohort=active,
    )
    assert [row["observation_id"] for row in rows] == ["active-observation"]
    assert settled == []

    active_settled = {**active_row, "settlement_status": "settled"}
    summary = v527.build_summary(
        [active_settled, legacy_settled],
        [active_settled, legacy_settled],
        date(2026, 1, 10),
        integrity=integrity,
        integrity_snapshot_current=True,
        active_cohort=active,
        global_rows=[active_settled, legacy_settled],
    )
    assert summary["ledger_rows"] == 1
    assert summary["settled_rows"] == 1
    assert summary["global_history_ledger_rows"] == 2
    assert summary["global_history_settled_rows"] == 2
    assert summary["eligible_cohorts"] == [{"cohort_id": "active", "manifest_hash": "hash-active"}]

    invalid_active = {"freeze_passed": False, "cohort_id": "active", "manifest_hash": "hash-active"}
    assert v527.settle_rows(
        [active_row], pd.DataFrame(), date(2026, 1, 1), integrity=integrity, active_cohort=invalid_active,
    ) == ([], [])


def test_v529_calendar_and_sources_fail_closed_outside_active_cohort() -> None:
    active = {"freeze_passed": True, "cohort_id": "active", "manifest_hash": "hash-active"}
    active_row = {
        "cohort_id": "active",
        "cohort_manifest_hash": "hash-active",
        "planned_entry_date": "2026-01-02",
        "planned_exit_date": "2026-01-10",
        "settlement_status": "not_due",
    }
    legacy_row = {
        **active_row,
        "cohort_id": "legacy",
        "cohort_manifest_hash": "hash-legacy",
        "settlement_status": "settled",
    }
    assert v529.filter_rows_to_active_cohort([active_row, legacy_row], active) == [active_row]
    assert v529.filter_rows_to_active_cohort(
        [active_row], {**active, "freeze_passed": False},
    ) == []

    scoped, matches = v529.scope_sources_to_active_cohort({
        "ledger_integrity": {
            "active_cohort_id": "active",
            "active_cohort_manifest_hash": "hash-active",
            "integrity_passed": True,
        },
        "promotion": {
            "cohort_id": "legacy",
            "cohort_manifest_hash": "hash-legacy",
            "promotion_ready": True,
        },
    }, active)
    assert matches == {"ledger_integrity": True, "promotion": False}
    assert scoped["ledger_integrity"]["integrity_passed"] is True
    assert scoped["promotion"] == {}

    calendar = v529.build_calendar([active_row], date(2026, 1, 1), scoped)
    gaps = pd.DataFrame([v529.gap("scope", 1, 1, "==", "ok")])
    summary = v529.build_summary(
        date(2026, 1, 1),
        [active_row],
        calendar,
        gaps,
        scoped,
        active_cohort=active,
        global_ledger=[active_row, legacy_row],
    )
    assert summary["ledger_rows"] == 1
    assert summary["global_history_ledger_rows"] == 2
    assert summary["goal_ready"] is False


def test_v535_waiting_room_uses_only_revalidated_active_cohort() -> None:
    active = {"freeze_passed": True, "cohort_id": "active", "manifest_hash": "hash-active"}
    active_row = {
        "batch_id": "active-batch",
        "industry_code": "1",
        "industry_name": "Active",
        "signal_date": "2026-01-01",
        "planned_entry_date": "2026-01-02",
        "planned_exit_date": "2026-01-10",
        "settlement_status": "not_due",
        "integrity_eligible": True,
        "late_backfill_excluded": False,
        "cohort_id": "active",
        "cohort_manifest_hash": "hash-active",
    }
    legacy_row = {
        **active_row,
        "batch_id": "legacy-batch",
        "industry_code": "2",
        "industry_name": "Legacy",
        "cohort_id": "legacy",
        "cohort_manifest_hash": "hash-legacy",
        "settlement_status": "settled",
    }
    ledger = pd.DataFrame([active_row, legacy_row])
    waiting = v535.build_waiting_rows(
        ledger,
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        {"pending_rows": 1},
        date(2026, 1, 5),
        active_cohort=active,
    )
    assert len(waiting) == 1
    assert waiting.iloc[0]["batch_id"] == "active-batch"
    assert v535.build_waiting_rows(
        ledger,
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        {"pending_rows": 1},
        date(2026, 1, 5),
        active_cohort={**active, "freeze_passed": False},
    ).empty

    scoped, matches = v535.scope_sources_to_active_cohort({
        "calendar": {
            "active_cohort_id": "legacy",
            "active_cohort_manifest_hash": "hash-legacy",
            "next_action_date": "2026-01-10",
        },
        "mapping": {"mapping_gate_passed": True},
    }, active, global_source_names={"mapping"})
    assert matches == {"calendar": False, "mapping": True}
    assert scoped["calendar"] == {}
    assert scoped["mapping"]["mapping_gate_passed"] is True

    checks = pd.DataFrame([{"status": "blocked"}])
    summary = v535.build_summary(
        waiting,
        checks,
        {"calendar": scoped["calendar"]},
        date(2026, 1, 5),
        active_cohort=active,
        global_ledger=ledger,
    )
    assert summary["observation_rows"] == 1
    assert summary["global_history_observation_rows"] == 2
    assert summary["global_history_settled_rows"] == 1
    assert summary["next_action_date"] == ""
    assert summary["can_claim_strong_rebound_industries"] is False
