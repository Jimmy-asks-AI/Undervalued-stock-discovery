from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refresh_fund_flow_exploratory_settlement_prices as refresh


def source_codes() -> list[str]:
    extras = [f"{810000 + index:06d}" for index in range(96)]
    return sorted([*refresh.TARGET_CODES, *refresh.QUARANTINED_HISTORY_CODES, *extras])


def fundamentals_loader(_level: str) -> pd.DataFrame:
    return pd.DataFrame({"行业代码": source_codes()})


def exact_history(code: str, *, exit_date: str = refresh.EXIT_DATE, exit_price: float = 101.0) -> pd.DataFrame:
    dates = ["2026-06-20", refresh.ENTRY_DATE, "2026-07-15"]
    closes = [98.0, 100.0, 99.0]
    if exit_date in dates:
        closes[dates.index(exit_date)] = exit_price
    else:
        dates.append(exit_date)
        closes.append(exit_price)
    return pd.DataFrame({
        "代码": [code] * len(dates),
        "日期": dates,
        "收盘": closes,
        "开盘": closes,
        "最高": [value + 1.0 for value in closes],
        "最低": [value - 1.0 for value in closes],
        "成交量": [1_000.0] * len(dates),
        "成交额": [10_000.0] * len(dates),
    })


def quarantined_history(code: str = "801156") -> pd.DataFrame:
    return pd.DataFrame({
        "代码": [code, code],
        "日期": ["2026-06-10", "2026-06-12"],
        "收盘": [98.0, 99.0],
        "成交量": [1_000.0, 1_100.0],
        "成交额": [10_000.0, 11_000.0],
    })


def prepared_price_dir(tmp_path: Path, *, all_codes: bool = False) -> Path:
    price_dir = tmp_path / "history" / "second"
    price_dir.mkdir(parents=True)
    codes = source_codes() if all_codes else [refresh.TARGET_CODES[0]]
    for code in codes:
        frame = (
            quarantined_history(code)
            if code in refresh.QUARANTINED_HISTORY_CODES
            else exact_history(code, exit_date="2026-07-15", exit_price=99.0)
        )
        frame.to_csv(
            price_dir / f"{code}.csv", index=False, encoding="utf-8-sig"
        )
    return price_dir


def after_gate() -> datetime:
    return datetime(2026, 7, 21, 15, 0, 0, tzinfo=refresh.SHANGHAI)


def assert_no_staging(price_dir: Path) -> None:
    assert not list(price_dir.parent.glob(f"{refresh.STAGING_PREFIX}*"))


def assert_no_bootstrap_staging(price_dir: Path) -> None:
    assert not list(price_dir.parent.glob(f"{refresh.BOOTSTRAP_PREFIX}*"))


def test_bootstrap_clones_complete_baseline_without_mutating_it(tmp_path: Path) -> None:
    baseline_dir = prepared_price_dir(tmp_path / "mainline", all_codes=True)
    settlement_dir = tmp_path / "settlement" / "second"
    baseline_before = refresh.cache_snapshot(baseline_dir)
    baseline_bytes = {
        path.name: path.read_bytes() for path in sorted(baseline_dir.glob("*.csv"))
    }

    attestation = refresh.bootstrap_settlement_price_cache(
        settlement_dir, baseline_dir
    )

    assert attestation["action"] == "created_from_mainline"
    assert attestation["baseline_unchanged"] is True
    assert attestation["settlement_copied_from_baseline"] is True
    assert attestation["mainline_write_invoked"] is False
    assert refresh.cache_snapshot(baseline_dir) == baseline_before
    assert refresh.cache_snapshot(settlement_dir) == baseline_before
    assert {
        path.name: path.read_bytes() for path in sorted(baseline_dir.glob("*.csv"))
    } == baseline_bytes
    assert_no_bootstrap_staging(settlement_dir)


def test_bootstrap_reuses_complete_existing_settlement_cache(tmp_path: Path) -> None:
    baseline_dir = prepared_price_dir(tmp_path / "mainline", all_codes=True)
    settlement_dir = prepared_price_dir(tmp_path / "settlement", all_codes=True)
    marker = settlement_dir / f"{refresh.TARGET_CODES[0]}.csv"
    marker.write_bytes(marker.read_bytes() + b"\n")
    settlement_before = refresh.cache_snapshot(settlement_dir)

    attestation = refresh.bootstrap_settlement_price_cache(
        settlement_dir, baseline_dir
    )

    assert attestation["action"] == "reused_existing"
    assert attestation["settlement_unchanged_during_bootstrap"] is True
    assert attestation["settlement_copied_from_baseline"] is False
    assert refresh.cache_snapshot(settlement_dir) == settlement_before
    assert_no_bootstrap_staging(settlement_dir)


def test_bootstrap_incomplete_baseline_fails_without_partial_cache(tmp_path: Path) -> None:
    baseline_dir = prepared_price_dir(tmp_path / "mainline", all_codes=False)
    settlement_dir = tmp_path / "settlement" / "second"
    baseline_before = refresh.cache_snapshot(baseline_dir)

    with pytest.raises(refresh.SourceContractError, match="mainline_baseline_cache_incomplete"):
        refresh.bootstrap_settlement_price_cache(settlement_dir, baseline_dir)

    assert not settlement_dir.exists()
    assert refresh.cache_snapshot(baseline_dir) == baseline_before
    assert_no_bootstrap_staging(settlement_dir)


def test_settlement_refresh_attests_mainline_unchanged_and_producers(
    tmp_path: Path,
) -> None:
    baseline_dir = prepared_price_dir(tmp_path / "mainline", all_codes=True)
    settlement_dir = tmp_path / "settlement" / "second"
    baseline_before = refresh.cache_snapshot(baseline_dir)
    audit_path = tmp_path / "audit" / "run_summary.json"

    summary = refresh.execute_settlement_price_refresh(
        now=after_gate(),
        price_dir=settlement_dir,
        baseline_dir=baseline_dir,
        audit_json=audit_path,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
    )

    assert summary["completion_status"] == "committed"
    assert summary["cache_scope"] == "dedicated_exploratory_settlement_only"
    assert summary["mainline_price_cache_write_invoked"] is False
    assert summary["cache_bootstrap"]["action"] == "created_from_mainline"
    assert summary["cache_bootstrap"]["baseline_unchanged_through_refresh"] is True
    assert refresh.cache_snapshot(baseline_dir) == baseline_before
    assert {
        item["path"] for item in summary["producer_attestations"]
    } == {refresh.relative_path(path) for path in refresh.PRODUCER_PATHS}
    assert all(len(item["sha256"]) == 64 for item in summary["producer_attestations"])
    assert json.loads(audit_path.read_text(encoding="utf-8"))["cache_bootstrap"] == summary[
        "cache_bootstrap"
    ]


def test_bootstrap_and_refresh_share_one_continuous_dual_lock_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_dir = prepared_price_dir(tmp_path / "mainline", all_codes=True)
    settlement_dir = tmp_path / "settlement" / "second"
    audit_path = tmp_path / "audit" / "run_summary.json"
    expected = {baseline_dir.resolve(), settlement_dir.resolve()}
    active: set[Path] = set()
    entered: list[Path] = []

    @contextmanager
    def observed_lock(path: Path):
        resolved = path.resolve()
        entered.append(resolved)
        active.add(resolved)
        try:
            yield
        finally:
            active.remove(resolved)

    def fetch_while_locked(code: str) -> pd.DataFrame:
        assert active == expected
        return exact_history(code)

    monkeypatch.setattr(refresh, "price_cache_lock", observed_lock)
    summary = refresh.execute_settlement_price_refresh(
        now=after_gate(),
        price_dir=settlement_dir,
        baseline_dir=baseline_dir,
        audit_json=audit_path,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=fetch_while_locked,
    )

    assert summary["completion_status"] == "committed"
    assert set(entered) == expected
    assert len(entered) == 2
    assert active == set()


def test_preclose_gate_refuses_without_calling_any_source(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path)
    before = refresh.cache_snapshot(price_dir)

    def forbidden_fundamentals(_level: str) -> pd.DataFrame:
        raise AssertionError("fundamentals must not be read before market close")

    def forbidden_history(_code: str) -> pd.DataFrame:
        raise AssertionError("history must not be read before market close")

    summary = refresh.run_price_refresh(
        now=datetime(2026, 7, 21, 14, 59, 59, tzinfo=refresh.SHANGHAI),
        price_dir=price_dir,
        fundamentals_loader=forbidden_fundamentals,
        history_fetcher=forbidden_history,
    )

    assert summary["completion_status"] == "blocked_pre_start"
    assert summary["fetch"]["attempted"] is False
    assert summary["official_cache_write_attempted"] is False
    assert summary["authoritative_cache_unchanged"] is True
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_fetch_failure_leaves_authoritative_cache_byte_identical(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)

    def fail_one(code: str) -> pd.DataFrame:
        if code == refresh.TARGET_CODES[2]:
            raise ConnectionError("offline fixture failure")
        return exact_history(code)

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=fail_one,
    )

    assert summary["completion_status"] == "fetch_or_staging_failed"
    assert summary["fetch"]["failed_industry_codes"] == [refresh.TARGET_CODES[2]]
    assert summary["coverage"]["checked"] is False
    assert summary["official_cache_write_attempted"] is False
    assert summary["authoritative_cache_unchanged"] is True
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_quarantine_allowlist_is_fixed_non_target_and_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fundamentals = fundamentals_loader("second")
    assert "801156" in refresh.normalize_source_codes(fundamentals)

    monkeypatch.setattr(
        refresh,
        "QUARANTINED_HISTORY_CODES",
        (refresh.TARGET_CODES[0],),
    )
    with pytest.raises(refresh.SourceContractError, match="target_industry_cannot_be_quarantined"):
        refresh.normalize_source_codes(fundamentals)

    monkeypatch.setattr(refresh, "QUARANTINED_HISTORY_CODES", ("801156", "801157"))
    with pytest.raises(refresh.SourceContractError, match="quarantine_allowlist_changed"):
        refresh.normalize_source_codes(fundamentals)

    monkeypatch.setattr(refresh, "QUARANTINED_HISTORY_CODES", ("801156",))
    without_quarantine = fundamentals[fundamentals["行业代码"] != "801156"]
    with pytest.raises(refresh.SourceContractError, match="quarantined_industry_missing_from_source"):
        refresh.normalize_source_codes(without_quarantine)


def test_quarantined_file_cannot_contain_settlement_dates(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    exact_history("801156").to_csv(
        price_dir / "801156.csv", index=False, encoding="utf-8-sig"
    )
    before = refresh.cache_snapshot(price_dir)

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
    )

    assert summary["completion_status"] == "fetch_or_staging_failed"
    assert summary["fetch"]["failure_phase"] == "quarantine"
    assert summary["fetch"]["failed_industry_codes"] == ["801156"]
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_quarantined_staging_copy_must_remain_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    real_copy2 = refresh.shutil.copy2

    def corrupt_quarantine_copy(source: Path, target: Path, *args: object, **kwargs: object) -> object:
        result = real_copy2(source, target, *args, **kwargs)
        if Path(source).name == "801156.csv":
            exact_history("801156").to_csv(target, index=False, encoding="utf-8-sig")
        return result

    monkeypatch.setattr(refresh.shutil, "copy2", corrupt_quarantine_copy)
    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
    )

    assert summary["completion_status"] == "fetch_or_staging_failed"
    assert summary["fetch"]["failure_phase"] == "quarantine"
    assert summary["fetch"]["failed_industry_codes"] == ["801156"]
    assert summary["fetch"]["quarantine_attestation_complete"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_nearest_prior_date_cannot_satisfy_exact_exit_coverage(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=lambda code: exact_history(code, exit_date="2026-07-20"),
    )

    assert summary["completion_status"] == "exact_coverage_failed"
    assert summary["coverage"]["entry_industry_count"] == 100
    assert summary["coverage"]["exit_industry_count"] == 0
    assert summary["coverage"]["target_exit_count"] == 0
    assert summary["coverage"]["exact_coverage_ready"] is False
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_nonpositive_target_close_fails_closed_even_with_99_valid_exits(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    bad_target = refresh.TARGET_CODES[0]

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=lambda code: exact_history(code, exit_price=0.0 if code == bad_target else 101.0),
    )

    assert summary["completion_status"] == "exact_coverage_failed"
    assert summary["coverage"]["exit_industry_count"] == 99
    assert summary["coverage"]["target_exit_count"] == 3
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_nonfinite_close_cannot_pass_exact_coverage(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    bad_target = refresh.TARGET_CODES[0]

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=lambda code: exact_history(
            code, exit_price=float("inf") if code == bad_target else 101.0
        ),
    )

    assert summary["completion_status"] == "exact_coverage_failed"
    assert summary["coverage"]["exit_industry_count"] == 99
    assert summary["coverage"]["target_exit_count"] == 3
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_entry_and_exit_counts_cannot_hide_common_universe_below_100(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path)
    extras = [f"{820000 + index:06d}" for index in range(116)]
    codes = sorted([*refresh.TARGET_CODES, *refresh.QUARANTINED_HISTORY_CODES, *extras])
    entry_codes = set(refresh.TARGET_CODES) | set(extras[:96])
    exit_codes = set(refresh.TARGET_CODES) | set(extras[20:])
    quarantined_history().to_csv(
        price_dir / "801156.csv", index=False, encoding="utf-8-sig"
    )
    before = refresh.cache_snapshot(price_dir)

    def shifted_fundamentals(_level: str) -> pd.DataFrame:
        return pd.DataFrame({"行业代码": codes})

    def shifted_history(code: str) -> pd.DataFrame:
        dates: list[str] = []
        closes: list[float] = []
        if code in entry_codes:
            dates.append(refresh.ENTRY_DATE)
            closes.append(100.0)
        if code in exit_codes:
            dates.append(refresh.EXIT_DATE)
            closes.append(101.0)
        return pd.DataFrame({"代码": [code] * len(dates), "日期": dates, "收盘": closes})

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=shifted_fundamentals,
        history_fetcher=shifted_history,
    )

    assert summary["completion_status"] == "exact_coverage_failed"
    assert summary["coverage"]["entry_industry_count"] == 100
    assert summary["coverage"]["exit_industry_count"] == 100
    assert summary["coverage"]["entry_exit_common_count"] == 80
    assert summary["coverage"]["target_common_count"] == 4
    assert summary["coverage"]["exact_coverage_ready"] is False
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_two_point_source_cannot_replace_full_history(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)

    def truncated_history(code: str) -> pd.DataFrame:
        frame = exact_history(code)
        return frame[frame["日期"].isin([refresh.ENTRY_DATE, refresh.EXIT_DATE])].reset_index(drop=True)

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=truncated_history,
    )

    assert summary["coverage"]["exact_coverage_ready"] is True
    assert summary["completion_status"] == "history_continuity_failed"
    assert summary["history_continuity"]["failed_industry_count"] == 100
    reasons = summary["history_continuity"]["failure_reason_counts"]
    assert reasons["row_count_decreased"] == 100
    assert reasons["existing_valid_date_missing_from_staged"] == 100
    assert reasons["history_start_boundary_regressed"] == 100
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_missing_existing_date_fails_before_any_commit(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    bad_code = refresh.TARGET_CODES[0]

    def missing_one_date(code: str) -> pd.DataFrame:
        frame = exact_history(code)
        if code == bad_code:
            frame = frame[frame["日期"] != "2026-07-15"]
        return frame.reset_index(drop=True)

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=missing_one_date,
    )

    assert summary["completion_status"] == "history_continuity_failed"
    assert summary["history_continuity"]["failed_industry_codes"] == [bad_code]
    detail = summary["history_continuity"]["failure_details"][0]
    assert "existing_valid_date_missing_from_staged" in detail["reason_codes"]
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_duplicate_history_date_fails_before_any_commit(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    bad_code = refresh.TARGET_CODES[1]

    def duplicate_one_date(code: str) -> pd.DataFrame:
        frame = exact_history(code)
        if code == bad_code:
            frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
        return frame

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=duplicate_one_date,
    )

    assert summary["completion_status"] == "history_continuity_failed"
    assert summary["history_continuity"]["duplicate_date_industry_count"] == 1
    assert summary["history_continuity"]["failed_industry_codes"] == [bad_code]
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_missing_existing_target_file_fails_closed(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    missing_code = refresh.TARGET_CODES[2]
    (price_dir / f"{missing_code}.csv").unlink()
    before = refresh.cache_snapshot(price_dir)

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
    )

    assert summary["coverage"]["exact_coverage_ready"] is True
    assert summary["completion_status"] == "history_continuity_failed"
    assert summary["history_continuity"]["existing_file_count"] == 100
    assert summary["history_continuity"]["failed_industry_codes"] == [missing_code]
    assert summary["history_continuity"]["failure_reason_counts"]["existing_history_missing"] == 1
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_missing_code_identity_fails_closed(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    bad_code = refresh.TARGET_CODES[3]

    def schema_regression(code: str) -> pd.DataFrame:
        frame = exact_history(code)
        if code == bad_code:
            frame = frame.drop(columns=["代码"])
        return frame

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=schema_regression,
    )

    assert summary["coverage"]["exact_coverage_ready"] is True
    assert summary["completion_status"] == "history_continuity_failed"
    assert summary["history_continuity"]["failed_industry_codes"] == [bad_code]
    assert summary["history_continuity"]["failure_reason_counts"]["staged_missing_identity_or_price_columns"] == 1
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_existing_schema_column_cannot_disappear(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    bad_code = refresh.TARGET_CODES[3]

    def schema_regression(code: str) -> pd.DataFrame:
        frame = exact_history(code)
        return frame.drop(columns=["开盘"]) if code == bad_code else frame

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=schema_regression,
    )

    assert summary["completion_status"] == "history_continuity_failed"
    assert summary["history_continuity"]["failure_reason_counts"]["history_schema_regressed"] == 1
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


@pytest.mark.parametrize(("column", "replacement"), [("收盘", 777777.0), ("开盘", float("nan"))])
def test_existing_historical_row_is_append_only(
    tmp_path: Path,
    column: str,
    replacement: float,
) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    bad_code = refresh.TARGET_CODES[0]

    def revised_history(code: str) -> pd.DataFrame:
        frame = exact_history(code)
        if code == bad_code:
            frame.loc[frame["日期"] == refresh.ENTRY_DATE, column] = replacement
        return frame

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=revised_history,
    )

    assert summary["completion_status"] == "history_continuity_failed"
    assert summary["history_continuity"]["historical_rows_changed"] == 1
    assert summary["history_continuity"]["historical_rows_unchanged"] is False
    assert summary["history_continuity"]["append_only_contract_passed"] is False
    assert summary["history_continuity"]["failure_reason_counts"]["historical_row_changed"] == 1
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_staged_files_cannot_change_after_continuity_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    real_validate = refresh.validate_history_continuity

    def validate_then_truncate(**kwargs: object) -> dict:
        result = real_validate(**kwargs)
        staging_dir = Path(kwargs["staging_dir"])
        code = refresh.TARGET_CODES[0]
        path = staging_dir / f"{code}.csv"
        frame = pd.read_csv(path, encoding="utf-8-sig")
        frame = frame[frame["日期"].isin([refresh.ENTRY_DATE, refresh.EXIT_DATE])]
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        return result

    monkeypatch.setattr(refresh, "validate_history_continuity", validate_then_truncate)
    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
    )

    assert summary["completion_status"] == "commit_validation_failed_no_write"
    assert summary["commit"]["attempted"] is False
    assert summary["official_cache_write_attempted"] is False
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_full_exact_coverage_atomically_replaces_only_history_files(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    unrelated = tmp_path / "observation_ledger.jsonl"
    unrelated.write_text("immutable-ledger\n", encoding="utf-8")
    before_ledger = unrelated.read_bytes()
    quarantine_before = (price_dir / "801156.csv").read_bytes()
    replace_calls: list[tuple[str, str]] = []

    def observed_replace(source: Path, target: Path) -> None:
        replace_calls.append((source.name, target.name))
        os.replace(source, target)

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
        replace_file=observed_replace,
    )

    assert summary["completion_status"] == "committed"
    assert summary["coverage"]["entry_industry_count"] == 100
    assert summary["coverage"]["exit_industry_count"] == 100
    assert summary["coverage"]["target_entry_count"] == 4
    assert summary["coverage"]["target_exit_count"] == 4
    assert summary["fetch"]["expected_industry_count"] == 101
    assert summary["fetch"]["succeeded_industry_count"] == 100
    assert summary["fetch"]["quarantined_industry_count"] == 1
    assert summary["fetch"]["quarantined_industry_codes"] == ["801156"]
    assert summary["fetch"]["quarantine_reason"] == refresh.QUARANTINE_REASON
    assert summary["fetch"]["source_accounted_industry_count"] == 101
    assert summary["fetch"]["failed_industry_count"] == 0
    assert summary["fetch"]["quarantine_attestation_complete"] is True
    quarantine_attestation = summary["fetch"]["quarantine_attestations"][0]
    assert quarantine_attestation["industry_code"] == "801156"
    assert quarantine_attestation["source_unchanged_during_staging"] is True
    assert quarantine_attestation["staged_matches_source"] is True
    assert quarantine_attestation["committed_matches_source"] is True
    assert len({
        quarantine_attestation["source_sha256_before"],
        quarantine_attestation["source_sha256_after_copy"],
        quarantine_attestation["staged_sha256"],
        quarantine_attestation["committed_sha256"],
    }) == 1
    assert summary["coverage"]["quarantine_exact_date_exclusion_passed"] is True
    assert summary["coverage"]["quarantined_required_date_codes"] == []
    assert summary["history_continuity"]["history_continuity_ready"] is True
    assert summary["history_continuity"]["verified_industry_count"] == 101
    assert summary["history_continuity"]["failed_industry_count"] == 0
    assert summary["history_continuity"]["historical_rows_unchanged"] is True
    assert summary["history_continuity"]["append_only_contract_passed"] is True
    assert summary["history_continuity"]["validation_inputs_unchanged"] is True
    assert summary["history_continuity"]["existing_row_count"] == 302
    assert summary["history_continuity"]["staged_row_count"] == 402
    assert summary["history_continuity"]["retained_existing_date_count"] == 302
    assert summary["commit"]["replaced_file_count"] == 101
    assert summary["commit"]["staged_and_committed_hashes_match"] is True
    assert (
        summary["commit"]["staged_universe_attestation"]["aggregate_sha256"]
        == summary["commit"]["committed_universe_attestation"]["aggregate_sha256"]
    )
    assert len(replace_calls) == 101
    assert summary["official_cache_touched"] is True
    assert summary["candidate_generation_invoked"] is False
    assert summary["ledger_write_invoked"] is False
    assert summary["account_or_trade_write_invoked"] is False
    assert unrelated.read_bytes() == before_ledger
    assert (price_dir / "801156.csv").read_bytes() == quarantine_before
    target = pd.read_csv(price_dir / f"{refresh.TARGET_CODES[0]}.csv", encoding="utf-8-sig")
    assert refresh.EXIT_DATE in target["日期"].astype(str).tolist()
    assert {"2026-06-20", refresh.ENTRY_DATE, "2026-07-15"}.issubset(
        set(target["日期"].astype(str))
    )
    assert {"代码", "日期", "收盘", "开盘", "最高", "最低", "成交量", "成交额"}.issubset(target.columns)
    assert_no_staging(price_dir)


def test_mid_commit_failure_rolls_every_file_back_to_original_hash(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    calls = 0

    def fail_third_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated atomic replace failure")
        os.replace(source, target)

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
        replace_file=fail_third_replace,
    )

    assert summary["completion_status"] == "commit_failed_rolled_back"
    assert summary["commit"]["replaced_file_count"] == 2
    assert summary["commit"]["rollback_performed"] is True
    assert summary["commit"]["rollback_error_count"] == 0
    assert summary["official_cache_write_attempted"] is True
    assert summary["official_cache_restored"] is True
    assert summary["official_cache_touched"] is False
    assert summary["authoritative_cache_unchanged"] is True
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_keyboard_interrupt_during_commit_rolls_back_before_staging_cleanup(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    before = refresh.cache_snapshot(price_dir)
    calls = 0

    def interrupt_third_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt("simulated operator interrupt")
        os.replace(source, target)

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
        replace_file=interrupt_third_replace,
    )

    assert summary["completion_status"] == "commit_failed_rolled_back"
    assert summary["commit"]["failure_type"] == "KeyboardInterrupt"
    assert summary["commit"]["rollback_performed"] is True
    assert summary["commit"]["rollback_error_count"] == 0
    assert refresh.cache_snapshot(price_dir) == before
    assert_no_staging(price_dir)


def test_keyboard_interrupt_during_rollback_retains_recovery_directory(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    calls = 0

    def fail_third_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated commit failure")
        os.replace(source, target)

    def interrupted_rollback(_source: Path, _target: Path) -> None:
        raise KeyboardInterrupt("simulated rollback interrupt")

    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
        replace_file=fail_third_replace,
        rollback_replace_file=interrupted_rollback,
    )

    assert summary["completion_status"] == "commit_failed_rollback_incomplete"
    assert summary["commit"]["rollback_error_count"] == 3
    recovery = Path(summary["staging_recovery_path"])
    assert recovery.is_dir()
    assert (recovery / "backup").is_dir()


def test_audit_json_contains_counts_and_hashes_but_no_price_values(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    summary = refresh.run_price_refresh(
        now=after_gate(),
        price_dir=price_dir,
        fundamentals_loader=fundamentals_loader,
        history_fetcher=exact_history,
    )
    audit_path = tmp_path / "audit" / "run_summary.json"
    refresh.atomic_write_json(audit_path, summary)
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["price_values_retained_in_audit"] is False
    assert payload["coverage"]["price_values_retained"] is False
    assert payload["history_continuity"]["price_values_retained"] is False
    assert payload["coverage"]["exit_industry_count"] == 100
    assert "收盘" not in serialized
    assert '"close"' not in serialized
    for price_value in ("98.0", "99.0", "100.0", "101.0", "777777"):
        assert price_value not in serialized


def test_failed_final_audit_write_leaves_noncommitted_sentinel(tmp_path: Path) -> None:
    price_dir = prepared_price_dir(tmp_path, all_codes=True)
    audit_path = tmp_path / "audit" / "run_summary.json"
    calls = 0

    def fail_second_write(path: Path, payload: dict) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated final audit write failure")
        refresh.atomic_write_json(path, payload)

    with pytest.raises(OSError, match="final audit write failure"):
        refresh.execute_price_refresh(
            now=after_gate(),
            price_dir=price_dir,
            audit_json=audit_path,
            fundamentals_loader=fundamentals_loader,
            history_fetcher=exact_history,
            audit_writer=fail_second_write,
        )

    saved = json.loads(audit_path.read_text(encoding="utf-8"))
    assert calls == 2
    assert saved["completion_status"] == "refresh_in_progress"
    assert saved["commit"]["succeeded"] is False
    assert saved["authoritative_cache_unchanged"] is True
