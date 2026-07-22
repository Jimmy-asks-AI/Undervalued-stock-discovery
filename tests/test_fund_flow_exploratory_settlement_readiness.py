from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_fund_flow_exploratory_settlement_readiness as audit


def test_preclose_gate_never_calls_price_loader() -> None:
    called = False

    def forbidden_loader() -> dict:
        nonlocal called
        called = True
        raise AssertionError("price coverage was read before the gate")

    now = datetime(2026, 7, 21, 14, 59, 59, tzinfo=audit.SHANGHAI)
    summary, rows, coverage = audit.assess(
        now,
        audit.fixture_source(),
        price_coverage_loader=forbidden_loader,
    )

    assert called is False
    assert coverage["checked"] is False
    assert summary["completion_status"] == "blocked_pre_start"
    assert summary["exit_data_read"] is False
    assert summary["return_values_read_or_written"] is False
    assert summary["pending_count"] == 4
    assert summary["blocked_count"] == 0
    assert all(row["disposition"] == "pending" for row in rows)
    assert all(row["reason_codes"].split("|")[0] == "time_gate_not_reached" for row in rows)


def test_exact_market_close_boundary_enables_coverage_check() -> None:
    called = 0

    def loader() -> dict:
        nonlocal called
        called += 1
        return audit.coverage_fixture(100)

    at_gate = datetime(2026, 7, 21, 15, 0, 0, tzinfo=audit.SHANGHAI)
    summary, rows, coverage = audit.assess(
        at_gate,
        audit.fixture_source(),
        price_coverage_loader=loader,
    )

    assert called == 1
    assert coverage["checked"] is True
    assert summary["time_gate_passed"] is True
    assert summary["completion_status"] == "complete_terminal_exclusions"
    assert summary["settlement_disposition_complete"] is True
    assert summary["blocked_count"] == 4
    assert all(row["disposition_status"] == "blocked_terminal_late_freeze_excluded" for row in rows)


def test_final_report_separates_fail_closed_integrity_and_settlement_price_scope() -> None:
    summary, rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, 0, tzinfo=audit.SHANGHAI),
        audit.fixture_source(),
        price_coverage_loader=lambda: audit.coverage_fixture(123),
    )

    report = audit.render_report(summary, rows)

    assert "V5.30 失败关闭摘要门 `true`（integrity `false`）" in report
    assert "/ V5.30 `true` /" not in report
    assert "`123` 个同一行业精确行情只衡量" in report
    assert "`benchmark_universe_count=0` 描述入场时没有形成按时冻结的基准宇宙" in report
    assert "事后行情补齐也不能修复历史冻结" in report
    assert "## 结论边界与状态同步" in report
    assert "由正式编排的后置步骤同步" in report
    assert "下一步只更新" not in report


def test_reported_exact_ready_cannot_override_zero_counts() -> None:
    coverage = audit.coverage_fixture(0)
    coverage["exact_coverage_ready"] = True
    summary, rows, normalized = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        audit.fixture_source(),
        price_coverage_loader=lambda: coverage,
    )

    assert normalized["reported_exact_coverage_ready"] is True
    assert normalized["exact_coverage_ready"] is False
    assert summary["completion_status"] == "pending_exact_price_coverage"
    assert summary["settlement_disposition_complete"] is False
    assert all(row["disposition"] == "pending" for row in rows)


def test_preflight_and_formal_output_modes_are_reserved() -> None:
    before = datetime(2026, 7, 21, 14, 59, 59, tzinfo=audit.SHANGHAI)
    at_gate = datetime(2026, 7, 21, 15, 0, 0, tzinfo=audit.SHANGHAI)
    assert audit.select_audit_mode(
        before, preflight_requested=False, settlement_disposition_complete=False,
    ) == "preflight"
    assert audit.select_audit_mode(
        at_gate, preflight_requested=True, settlement_disposition_complete=True,
    ) == "preflight"
    assert audit.select_audit_mode(
        at_gate, preflight_requested=False, settlement_disposition_complete=False,
    ) == "due_date_blocked_preflight"
    assert audit.select_audit_mode(
        at_gate, preflight_requested=False, settlement_disposition_complete=True,
    ) == "formal_disposition"
    assert audit.output_for_mode("preflight") == audit.PREFLIGHT_OUT
    assert audit.output_for_mode("due_date_blocked_preflight") == audit.PREFLIGHT_OUT
    assert audit.output_for_mode("formal_disposition") == audit.FINAL_OUT
    with pytest.raises(ValueError, match="unknown audit mode"):
        audit.output_for_mode("mixed")


@pytest.mark.parametrize("industry_count, expected", [(99, False), (100, True), (131, True)])
def test_benchmark_coverage_boundary(industry_count: int, expected: bool) -> None:
    at_gate = datetime(2026, 7, 21, 15, 0, 0, tzinfo=audit.SHANGHAI)
    summary, rows, coverage = audit.assess(
        at_gate,
        audit.fixture_source(),
        price_coverage_loader=lambda: audit.coverage_fixture(industry_count),
    )

    assert coverage["exact_coverage_ready"] is expected
    if expected:
        assert summary["completion_status"] == "complete_terminal_exclusions"
        assert all(row["disposition"] == "blocked" for row in rows)
    else:
        assert summary["completion_status"] == "pending_exact_price_coverage"
        assert all(row["disposition_status"] == "pending_exact_price_coverage" for row in rows)


def test_exact_coverage_requires_same_industry_intersection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "MIN_BENCHMARK_INDUSTRIES", 5)

    def write_price(code: str, dates: list[str]) -> None:
        body = "code,trade_date,close\n" + "".join(f"{code},{date},100\n" for date in dates)
        (tmp_path / f"{code}.csv").write_text(body, encoding="utf-8")

    for code, _name in audit.EXPECTED.values():
        write_price(code, [audit.ENTRY_DATE, audit.EXIT_DATE])
    write_price("900001", [audit.ENTRY_DATE])
    write_price("900002", [audit.EXIT_DATE])

    coverage = audit.scan_exact_date_coverage(tmp_path)
    assert coverage["entry_industry_count"] == 5
    assert coverage["exit_industry_count"] == 5
    assert coverage["entry_exit_common_count"] == 4
    assert coverage["candidate_common_count"] == 4
    assert coverage["exact_coverage_ready"] is False

    write_price("900003", [audit.ENTRY_DATE, audit.EXIT_DATE])
    coverage = audit.scan_exact_date_coverage(tmp_path)
    assert coverage["entry_exit_common_count"] == 5
    assert coverage["exact_coverage_ready"] is True


@pytest.mark.parametrize(
    ("filename", "internal_code", "duplicate_exit", "expected_reason"),
    [
        ("not-an-industry", "not-an-industry", False, "invalid_six_digit_filename"),
        ("900003", "000000", False, "industry_code_mismatch"),
        ("900003", "900003", True, f"duplicate_required_date:{audit.EXIT_DATE}"),
    ],
)
def test_strict_price_file_identity_and_exact_date_uniqueness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    internal_code: str,
    duplicate_exit: bool,
    expected_reason: str,
) -> None:
    monkeypatch.setattr(audit, "MIN_BENCHMARK_INDUSTRIES", 5)
    for code, _name in audit.EXPECTED.values():
        (tmp_path / f"{code}.csv").write_text(
            f"code,trade_date,close\n{code},{audit.ENTRY_DATE},100\n{code},{audit.EXIT_DATE},101\n",
            encoding="utf-8",
        )
    rows = [
        f"{internal_code},{audit.ENTRY_DATE},100",
        f"{internal_code},{audit.EXIT_DATE},101",
    ]
    if duplicate_exit:
        rows.append(f"{internal_code},{audit.EXIT_DATE},102")
    (tmp_path / f"{filename}.csv").write_text(
        "code,trade_date,close\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )

    coverage = audit.scan_exact_date_coverage(tmp_path)
    reasons = coverage["invalid_files"][0]["reason_codes"]
    assert expected_reason in reasons
    assert coverage["invalid_file_count"] == 1
    assert coverage["entry_exit_common_count"] == 4
    assert coverage["exact_coverage_ready"] is False


def test_quarantined_history_cannot_enter_live_exact_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit, "MIN_BENCHMARK_INDUSTRIES", 5)
    exact_codes = [code for code, _name in audit.EXPECTED.values()] + ["900003"]
    for code in exact_codes + ["801156"]:
        (tmp_path / f"{code}.csv").write_text(
            f"code,trade_date,close\n{code},{audit.ENTRY_DATE},100\n"
            f"{code},{audit.EXIT_DATE},101\n",
            encoding="utf-8",
        )

    coverage = audit.scan_exact_date_coverage(tmp_path)

    assert coverage["quarantined_required_date_codes"] == ["801156"]
    assert set(coverage["quarantined_file_sha256"]) == {"801156"}
    assert len(coverage["quarantined_file_sha256"]["801156"]) == 64
    assert coverage["quarantine_exact_date_exclusion_passed"] is False
    assert coverage["invalid_file_count"] == 1
    assert coverage["entry_exit_common_count"] == 5
    assert coverage["exact_coverage_ready"] is False


def test_late_freezes_remain_terminal_even_when_exact_dates_arrive() -> None:
    now = datetime(2026, 7, 22, 9, 0, 0, tzinfo=audit.SHANGHAI)
    summary, rows, _coverage = audit.assess(
        now,
        audit.fixture_source(),
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["settled_count"] == 0
    assert summary["qualified_settled_count"] == 0
    assert summary["promotion_ready"] is False
    assert summary["can_claim_strong_rebound_industries"] is False
    assert summary["manual_decision_support_ready"] is False
    assert summary["production_ready"] is False
    assert summary["auto_execution_allowed"] is False
    for row in rows:
        assert row["candidate_entry_freeze_status"] == "late_backfill_excluded"
        assert row["benchmark_entry_freeze_status"] == "late_backfill_excluded"
        assert row["qualified_for_goal"] is False
        assert row["integrity_eligible"] is False
        assert row["promotion_eligible"] is False
        assert all(row[field] == "" for field in audit.RETURN_FIELDS)


@pytest.mark.parametrize(
    ("candidate_status", "benchmark_status", "benchmark_count"),
    [
        ("late_backfill_excluded", "frozen_on_time", "131"),
        ("frozen_on_time", "late_backfill_excluded", "0"),
        ("late_backfill_excluded", "late_backfill_excluded", "00"),
        ("late_backfill_excluded", "late_backfill_excluded", "0.0"),
        ("late_backfill_excluded", "late_backfill_excluded", 0.0),
        ("late_backfill_excluded", "late_backfill_excluded", False),
    ],
)
def test_terminal_contract_requires_both_late_freezes_and_canonical_zero(
    candidate_status: str,
    benchmark_status: str,
    benchmark_count: object,
) -> None:
    source = audit.fixture_source()
    for row in source["entry_freezes"]:
        row["entry_price_freeze_status"] = candidate_status
    for row in source["benchmark_freezes"]:
        row["benchmark_entry_freeze_status"] = benchmark_status
        row["benchmark_universe_count"] = benchmark_count
    summary, rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["record_contract_passed"] is False
    assert summary["settlement_disposition_complete"] is False
    assert summary["completion_status"] == "blocked_source_contract"
    assert all(row["disposition_status"] == "blocked_source_record_contract" for row in rows)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("price_refresh", "completion_status"), "refresh_in_progress"),
        (("price_refresh", "cache_scope"), "shared_mainline"),
        (("price_refresh", "mainline_price_cache_write_invoked"), True),
        (("price_refresh", "cache_bootstrap", "baseline_unchanged_through_refresh"), False),
        (("price_refresh", "cache_bootstrap", "settlement_quarantined_file_sha256", "801156"), "f" * 64),
        (("price_refresh", "authoritative_before", "aggregate_sha256"), "f" * 64),
        (("baseline_price_cache_snapshot", "aggregate_sha256"), "f" * 64),
        (("price_refresh", "history_continuity", "append_only_contract_passed"), False),
        (("price_refresh", "history_continuity", "historical_rows_changed"), 1),
        (("price_refresh", "commit", "staged_and_committed_hashes_match"), False),
        (("price_refresh", "fetch", "attempted"), False),
        (("price_refresh", "fetch", "succeeded_industry_count"), 131),
        (("price_refresh", "fetch", "failed_industry_codes"), ["801010"]),
        (("price_refresh", "fetch", "failure_phase"), "fetch"),
        (("price_refresh", "fetch", "quarantined_industry_count"), 2),
        (("price_refresh", "fetch", "quarantined_industry_codes"), ["801156", "801156"]),
        (("price_refresh", "fetch", "quarantined_industry_codes"), ["801156", "801194"]),
        (("price_refresh", "fetch", "source_accounted_industry_count"), 130),
        (("price_refresh", "fetch", "quarantine_reason"), "unapproved"),
        (("price_refresh", "fetch", "quarantine_attestation_complete"), False),
        (("price_refresh", "coverage", "entry_industry_count"), 131),
        (("price_refresh", "coverage", "exit_industry_count"), 131),
        (("price_refresh", "coverage", "entry_exit_common_count"), 131),
        (("price_refresh", "coverage", "quarantined_required_date_codes"), ["801156"]),
        (("price_cache_snapshot", "aggregate_sha256"), "d" * 64),
    ],
)
def test_price_refresh_attestation_is_required_for_formal_disposition(
    path: tuple[str, ...],
    value: object,
) -> None:
    source = audit.fixture_source()
    cursor: dict = source
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["price_source_gate_passed"] is False
    assert summary["settlement_disposition_complete"] is False
    assert summary["completion_status"] == "blocked_price_refresh_attestation_gate"


def test_live_quarantine_hash_must_match_refresh_attestation() -> None:
    coverage = audit.coverage_fixture(131)
    coverage["quarantined_file_sha256"]["801156"] = "f" * 64
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        audit.fixture_source(),
        price_coverage_loader=lambda: coverage,
    )

    assert summary["price_source_gate_passed"] is False
    assert "quarantine_byte_attestation" in summary["price_source_gate_reason_codes"]


def test_bootstrap_quarantine_hash_must_match_live_mainline() -> None:
    source = audit.fixture_source()
    source["price_refresh"]["cache_bootstrap"][
        "settlement_quarantined_file_sha256"
    ]["801156"] = "f" * 64
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["price_source_gate_passed"] is False
    assert (
        "quarantined_bootstrap_files_match_live_mainline"
        in summary["price_source_gate_reason_codes"]
    )


def test_refresh_producer_hash_must_match_live_script() -> None:
    source = audit.fixture_source()
    source["price_refresh"]["producer_attestations"][0]["sha256"] = "f" * 64
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["price_source_gate_passed"] is False
    assert (
        "refresh_producer_attestations_match_live_files"
        in summary["price_source_gate_reason_codes"]
    )


def test_refresh_input_hash_must_match_bootstrap_output() -> None:
    source = audit.fixture_source()
    source["price_refresh"]["authoritative_before"]["aggregate_sha256"] = "f" * 64
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["price_source_gate_passed"] is False
    assert (
        "bootstrap_output_matches_refresh_input"
        in summary["price_source_gate_reason_codes"]
    )


def test_mainline_attestation_must_match_live_cache_snapshot() -> None:
    source = audit.fixture_source()
    source["baseline_price_cache_snapshot"]["aggregate_sha256"] = "f" * 64
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["price_source_gate_passed"] is False
    assert (
        "mainline_cache_unchanged_through_bootstrap_and_refresh"
        in summary["price_source_gate_reason_codes"]
    )


def test_live_exact_coverage_cannot_include_quarantined_file() -> None:
    coverage = audit.coverage_fixture(130)
    coverage.update({
        "entry_industry_count": 131,
        "exit_industry_count": 131,
        "entry_exit_common_count": 131,
    })
    summary, _rows, normalized = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        audit.fixture_source(),
        price_coverage_loader=lambda: coverage,
    )

    assert normalized["exact_coverage_ready"] is True
    assert summary["price_source_gate_passed"] is False
    assert "live_scan_matches_refresh_universe" in summary["price_source_gate_reason_codes"]
    assert summary["completion_status"] == "blocked_price_refresh_attestation_gate"


def test_invalid_active_cohort_cannot_open_formal_disposition() -> None:
    source = audit.fixture_source()
    source["active_cohort"]["freeze_passed"] = False
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )
    assert summary["active_cohort_gate_passed"] is False
    assert summary["state_gate_passed"] is False
    assert summary["settlement_disposition_complete"] is False
    assert summary["completion_status"] == "blocked_active_cohort_gate"
    assert "active_cohort:validated_active_cohort" in summary["state_gate_reason_codes"]


@pytest.mark.parametrize(("field", "invalid"), [
    ("active_cohort_id", "wrong"),
    ("active_cohort_manifest_hash", "b" * 64),
    ("as_of_date", "2026-07-18"),
    ("generated_at", "2026-07-18T20:15:14"),
    ("policy_status", "production"),
    ("global_ledger_integrity_passed", True),
    ("global_violation_count", 0),
    ("global_late_backfill_count", 0),
    ("can_claim_strong_rebound_industries", True),
    ("auto_execution_allowed", True),
])
def test_v5_30_summary_must_match_current_fail_closed_state(field: str, invalid: object) -> None:
    source = audit.fixture_source()
    source["integrity"][field] = invalid
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )
    assert summary["active_cohort_gate_passed"] is True
    assert summary["v5_30_summary_gate_passed"] is False
    assert summary["settlement_disposition_complete"] is False
    assert summary["completion_status"] == "blocked_v5_30_summary_gate"
    assert any(reason.startswith("v5_30:") for reason in summary["state_gate_reason_codes"])


@pytest.mark.parametrize(("source_key", "expected_status"), [
    ("integrity", "blocked_v5_30_summary_gate"),
    ("current_state", "blocked_current_state_consistency_gate"),
])
def test_missing_state_summary_fails_closed(source_key: str, expected_status: str) -> None:
    source = audit.fixture_source()
    source[source_key] = {}
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )
    assert summary["settlement_disposition_complete"] is False
    assert summary["completion_status"] == expected_status


@pytest.mark.parametrize(("source_key", "field"), [
    ("integrity", "goal_ready"),
    ("integrity", "production_ready"),
    ("current_state", "manual_decision_support_ready"),
    ("current_state", "production_ready"),
    ("current_state", "true_forward_route_ready"),
])
def test_required_ready_field_cannot_be_missing(source_key: str, field: str) -> None:
    source = audit.fixture_source()
    source[source_key].pop(field)
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )
    assert summary["settlement_disposition_complete"] is False


@pytest.mark.parametrize(("field", "invalid"), [
    ("active_cohort_manifest_hash", "b" * 64),
    ("active_cohort_validated", False),
    ("state_consistent", False),
    ("fail_count", 1),
    ("generated_at", "2026-07-18T20:46:48"),
    ("policy_status", "production"),
    ("current_action", "BUY"),
    ("strong_industry_alpha_validated", True),
    ("manual_decision_support_ready", True),
    ("production_ready", True),
    ("true_forward_route_ready", True),
    ("auto_execution_allowed", True),
])
def test_current_state_consistency_must_be_same_day_same_pair_and_no_action(field: str, invalid: object) -> None:
    source = audit.fixture_source()
    source["current_state"][field] = invalid
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )
    assert summary["v5_30_summary_gate_passed"] is True
    assert summary["current_state_consistency_gate_passed"] is False
    assert summary["settlement_disposition_complete"] is False
    assert summary["completion_status"] == "blocked_current_state_consistency_gate"
    assert any(reason.startswith("current_state:") for reason in summary["state_gate_reason_codes"])


def test_calendar_requires_exact_entry_and_exit_dates(tmp_path: Path) -> None:
    missing = audit.validate_calendar_dates(tmp_path / "missing.csv")
    assert missing["calendar_dates_valid"] is False
    assert missing["reason"] == "calendar file missing"

    calendar = tmp_path / "calendar.csv"
    calendar.write_text("wrong_column\n2026-06-23\n", encoding="utf-8")
    assert audit.validate_calendar_dates(calendar)["reason"] == "calendar date column missing"

    calendar.write_text(f"trade_date\n{audit.ENTRY_DATE}\n", encoding="utf-8")
    incomplete = audit.validate_calendar_dates(calendar)
    assert incomplete["entry_date_present"] is True
    assert incomplete["exit_date_present"] is False
    assert incomplete["calendar_dates_valid"] is False

    calendar.write_text(f"trade_date\n{audit.ENTRY_DATE}\n{audit.EXIT_DATE}\n", encoding="utf-8")
    complete = audit.validate_calendar_dates(calendar)
    assert complete["calendar_dates_valid"] is True

    calendar.write_text(
        f"trade_date\n{audit.ENTRY_DATE}-garbage\n{audit.EXIT_DATE}-garbage\n",
        encoding="utf-8",
    )
    assert audit.validate_calendar_dates(calendar)["calendar_dates_valid"] is False

    source = audit.fixture_source()
    source["calendar_validation"] = incomplete
    summary, _rows, _coverage = audit.assess(
        datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )
    assert summary["calendar_dates_valid"] is False
    assert summary["settlement_disposition_complete"] is False
    assert summary["completion_status"] == "blocked_calendar_dates_gate"


def test_source_contract_fails_closed_for_missing_record() -> None:
    source = audit.fixture_source()
    source["observations"] = source["observations"][:-1]
    now = datetime(2026, 7, 21, 15, 0, 0, tzinfo=audit.SHANGHAI)
    summary, rows, _coverage = audit.assess(
        now,
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["record_contract_passed"] is False
    assert summary["completion_status"] == "blocked_source_contract"
    assert len(summary["missing_observation_ids"]) == 1
    missing = next(row for row in rows if not row["record_contract_passed"])
    assert missing["disposition_status"] == "blocked_source_record_contract"
    assert all(missing[field] == "" for field in audit.RETURN_FIELDS)


@pytest.mark.parametrize("field", ["qualified_for_goal", "integrity_eligible", "promotion_eligible"])
@pytest.mark.parametrize("invalid", ["", "unknown"])
def test_false_invariants_must_be_explicit(field: str, invalid: str) -> None:
    source = audit.fixture_source()
    source["observations"][0][field] = invalid
    now = datetime(2026, 7, 21, 15, 0, 0, tzinfo=audit.SHANGHAI)
    summary, rows, _coverage = audit.assess(
        now,
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["record_contract_passed"] is False
    assert summary["completion_status"] == "blocked_source_contract"
    affected = next(row for row in rows if row["industry_code"] == "801194")
    assert affected[field] is None
    assert affected["disposition_status"] == "blocked_source_record_contract"


@pytest.mark.parametrize("source_name, duplicate_summary_key, reason", [
    ("entry_freezes", "duplicate_candidate_freeze_keys", "duplicate_candidate_entry_freeze_key"),
    ("benchmark_freezes", "duplicate_benchmark_freeze_keys", "duplicate_benchmark_entry_freeze_key"),
])
def test_duplicate_freeze_key_cannot_override_terminal_late_status(
    source_name: str,
    duplicate_summary_key: str,
    reason: str,
) -> None:
    source = audit.fixture_source()
    duplicate = dict(source[source_name][0])
    if source_name == "entry_freezes":
        duplicate["entry_price_freeze_status"] = "frozen_on_time"
    else:
        duplicate["benchmark_entry_freeze_status"] = "frozen_on_time"
        duplicate["benchmark_universe_count"] = "131"
    source[source_name].append(duplicate)
    now = datetime(2026, 7, 21, 15, 0, 0, tzinfo=audit.SHANGHAI)
    summary, rows, _coverage = audit.assess(
        now,
        source,
        price_coverage_loader=lambda: audit.coverage_fixture(131),
    )

    assert summary["record_contract_passed"] is False
    assert summary["completion_status"] == "blocked_source_contract"
    assert len(summary[duplicate_summary_key]) == 1
    affected = next(row for row in rows if row["industry_code"] == "801194")
    assert affected["candidate_entry_freeze_status"] == "late_backfill_excluded" or affected["benchmark_entry_freeze_status"] == "late_backfill_excluded"
    assert reason in affected["reason_codes"]


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="explicit timezone"):
        audit.assess(
            datetime(2026, 7, 21, 15, 0, 0),
            audit.fixture_source(),
            price_coverage_loader=lambda: audit.coverage_fixture(131),
        )


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-21T15:00:00",
        "2026-07-21T00:30:00+14:00",
        "2026-07-21T23:30:00-10:00",
    ],
)
def test_state_timestamp_must_be_aware_and_on_shanghai_exit_date(value: str) -> None:
    assert audit.timestamp_is_on_date(value, audit.EXIT_DATE) is False


def test_refresh_timestamp_must_be_at_or_after_shanghai_gate() -> None:
    assert audit.timestamp_at_or_after("2026-07-21T14:59:59+08:00", audit.START_GATE) is False
    assert audit.timestamp_at_or_after("2026-07-21T15:00:00+08:00", audit.START_GATE) is True
    assert audit.timestamp_at_or_after("2026-07-21T07:00:00Z", audit.START_GATE) is True


def test_exact_date_coverage_requires_a_positive_close(tmp_path: Path) -> None:
    bad = tmp_path / "801194.csv"
    bad.write_text(
        "代码,日期,收盘\n801194,2026-06-23,\n801194,2026-07-21,0\n",
        encoding="utf-8",
    )
    coverage = audit.scan_exact_date_coverage(tmp_path)
    assert coverage["entry_industry_count"] == 0
    assert coverage["exit_industry_count"] == 0
    assert coverage["exact_coverage_ready"] is False

    bad.write_text(
        "代码,日期,收盘\n801194,2026-06-23,100.0\n801194,2026-07-21,101.0\n",
        encoding="utf-8",
    )
    coverage = audit.scan_exact_date_coverage(tmp_path)
    assert coverage["entry_industry_count"] == 1
    assert coverage["exit_industry_count"] == 1


def test_blocked_four_piece_output_is_complete(tmp_path: Path) -> None:
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=audit.SHANGHAI)
    source = audit.fixture_source()
    summary, rows, coverage = audit.assess(now, source, price_coverage_loader=None)
    summary["audit_mode"] = "preflight"
    evidence = [{"path": "logs/ledger.jsonl", "bytes": 10, "sha256": "a" * 64}]
    pre_snapshot = {
        "snapshot_phase": "pre",
        "target_observation_count": 4,
        "sensitive_file_sha256": {"logs/ledger.jsonl": "a" * 64},
    }
    post_snapshot = {
        "snapshot_phase": "post",
        "target_observation_count": 4,
        "sensitive_file_sha256": {"logs/ledger.jsonl": "a" * 64},
        "authoritative_hashes_unchanged": True,
    }

    audit.write_outputs(tmp_path, summary, rows, evidence, coverage, pre_snapshot, post_snapshot)

    assert {path.name for path in tmp_path.iterdir()} == {
        "report.md",
        "run_summary.json",
        "top_candidates.csv",
        "debug",
    }
    required_debug = {
        "settlement_dispositions.csv",
        "sha256_manifest.csv",
        "pre_settlement_snapshot.json",
        "post_settlement_snapshot.json",
        "date_coverage_audit.json",
        "command_results.json",
    }
    assert required_debug.issubset({path.name for path in (tmp_path / "debug").iterdir()})
    saved_pre = json.loads((tmp_path / "debug" / "pre_settlement_snapshot.json").read_text(encoding="utf-8"))
    saved_post = json.loads((tmp_path / "debug" / "post_settlement_snapshot.json").read_text(encoding="utf-8"))
    assert saved_pre["snapshot_phase"] == "pre"
    assert saved_post["snapshot_phase"] == "post"
    assert saved_post["authoritative_hashes_unchanged"] is True
    saved_summary = json.loads((tmp_path / "run_summary.json").read_text(encoding="utf-8"))
    assert saved_summary["completion_status"] == "blocked_pre_start"
    assert saved_summary["all_return_fields_empty"] is True
    with (tmp_path / "top_candidates.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        saved_rows = list(csv.DictReader(handle))
    assert len(saved_rows) == 4
    assert {row["record_type"] for row in saved_rows} == {"exploratory_settlement_disposition"}
    assert all(not row["realized_return"] for row in saved_rows)


def test_output_directory_publication_replaces_old_package(tmp_path: Path) -> None:
    final = tmp_path / "formal"
    final.mkdir()
    (final / "run_summary.json").write_text('{"old":true}', encoding="utf-8")
    staged = tmp_path / ".formal.staging-fixture"
    staged.mkdir()
    (staged / "run_summary.json").write_text('{"new":true}', encoding="utf-8")

    audit.publish_output_directory(staged, final)

    assert not staged.exists()
    assert json.loads((final / "run_summary.json").read_text(encoding="utf-8")) == {"new": True}
    assert not list(tmp_path.glob(".formal.previous-*"))


def test_output_publication_failure_restores_previous_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = tmp_path / "formal"
    final.mkdir()
    old_bytes = b'{"old":true}'
    (final / "run_summary.json").write_bytes(old_bytes)
    staged = tmp_path / ".formal.staging-fixture"
    staged.mkdir()
    (staged / "run_summary.json").write_text('{"new":true}', encoding="utf-8")
    real_replace = os.replace

    def fail_new_publish(source: object, target: object) -> None:
        if Path(source) == staged and Path(target) == final:
            raise OSError("simulated publish failure")
        real_replace(source, target)

    monkeypatch.setattr(audit.os, "replace", fail_new_publish)
    with pytest.raises(OSError, match="publish failure"):
        audit.publish_output_directory(staged, final)

    assert (final / "run_summary.json").read_bytes() == old_bytes
    assert staged.exists()


def test_output_publication_keyboard_interrupt_restores_previous_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = tmp_path / "formal"
    final.mkdir()
    old_bytes = b'{"old":true}'
    (final / "run_summary.json").write_bytes(old_bytes)
    staged = tmp_path / ".formal.staging-fixture"
    staged.mkdir()
    (staged / "run_summary.json").write_text('{"new":true}', encoding="utf-8")
    real_replace = os.replace

    def interrupt_new_publish(source: object, target: object) -> None:
        if Path(source) == staged and Path(target) == final:
            raise KeyboardInterrupt("simulated publish interrupt")
        real_replace(source, target)

    monkeypatch.setattr(audit.os, "replace", interrupt_new_publish)
    with pytest.raises(KeyboardInterrupt, match="publish interrupt"):
        audit.publish_output_directory(staged, final)

    assert (final / "run_summary.json").read_bytes() == old_bytes
    assert staged.exists()
    assert not list(tmp_path.glob(".formal.previous-*"))


def test_source_drift_before_publish_preserves_old_formal_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price_dir = tmp_path / "history" / "second"
    price_dir.mkdir(parents=True)
    monkeypatch.setattr(audit, "MIN_BENCHMARK_INDUSTRIES", 5)
    codes = [*(code for code, _name in audit.EXPECTED.values()), "900003"]
    for code in codes:
        (price_dir / f"{code}.csv").write_text(
            f"code,trade_date,close\n{code},{audit.ENTRY_DATE},100\n{code},{audit.EXIT_DATE},101\n",
            encoding="utf-8",
        )
    sensitive = tmp_path / "authoritative.json"
    sensitive.write_text('{"version":1}', encoding="utf-8")
    final = tmp_path / "formal"
    final.mkdir()
    old_bytes = b'{"old":true}'
    (final / "run_summary.json").write_bytes(old_bytes)

    def source_fixture() -> dict:
        source = audit.fixture_source()
        source["price_refresh"]["authoritative_after"] = audit.price_cache_snapshot(price_dir)
        return source

    monkeypatch.setattr(audit, "PRICE_DIR", price_dir)
    monkeypatch.setattr(audit, "FINAL_OUT", final)
    monkeypatch.setattr(audit, "PREFLIGHT_OUT", tmp_path / "preflight")
    monkeypatch.setattr(audit, "SENSITIVE_PATHS", (sensitive,))
    monkeypatch.setattr(audit, "EVIDENCE_PATHS", (sensitive,))
    monkeypatch.setattr(audit, "resolve_active_manifest_path", lambda: None)
    monkeypatch.setattr(audit, "collect_readonly_sources", source_fixture)

    def mutate_source() -> None:
        staged = next(tmp_path.glob(".*.staging-*"))
        snapshot = json.loads(
            (staged / "debug" / "pre_settlement_snapshot.json").read_text(encoding="utf-8")
        )
        expected_prices = {
            audit.relative_path(path) for path in price_dir.glob("*.csv")
        }
        snapshotted_prices = {
            key
            for key in snapshot["sensitive_file_sha256"]
            if key in expected_prices
        }
        assert snapshotted_prices == expected_prices
        sensitive.write_text('{"version":2}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="evidence changed"):
        audit.execute_readiness_audit(
            observed_at=datetime(2026, 7, 21, 15, 0, tzinfo=audit.SHANGHAI),
            preflight_requested=False,
            before_publish_hook=mutate_source,
        )

    assert (final / "run_summary.json").read_bytes() == old_bytes
    assert not list(tmp_path.glob(".formal.staging-*"))


def test_live_sources_match_the_fixed_allowlist_without_price_read() -> None:
    source = audit.collect_readonly_sources()
    assert source["global_observation_count"] == 4
    assert len(source["observations"]) == 4
    assert len(source["entry_freezes"]) == 4
    assert len(source["benchmark_freezes"]) == 4
    assert source["calendar_validation"]["calendar_dates_valid"] is True

    observed = datetime(2026, 7, 18, 12, 0, 0, tzinfo=audit.SHANGHAI)
    summary, rows, coverage = audit.assess(observed, source, price_coverage_loader=None)
    assert coverage["checked"] is False
    assert summary["records_found"] == 4
    assert summary["record_contract_passed"] is True
    if audit.INTEGRITY.is_file():
        assert summary["v5_30_global_violation_count"] == 16
    else:
        assert source["integrity"] == {}
        assert summary["v5_30_global_violation_count"] == 0
        assert summary["v5_30_summary_gate_passed"] is False
    assert all(row["candidate_entry_freeze_status"] == "late_backfill_excluded" for row in rows)
    assert all(row["benchmark_entry_freeze_status"] == "late_backfill_excluded" for row in rows)
    assert all(row["benchmark_universe_count"] == 0 for row in rows)
