#!/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import hashlib
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

try:
    from fund_flow_exploratory_price_contract import (
        price_cache_lock,
        price_cache_snapshot,
        price_universe_snapshot,
    )
    from research_integrity import atomic_write_json, file_sha256
    from run_industry_index_research_validation import (
        clean_history,
        fetch_industry_fundamentals,
        fetch_industry_history,
        normalize_industry_code,
    )
    from valuation_pit_contract import SHANGHAI
except ModuleNotFoundError:  # package-style imports in tests
    from scripts.fund_flow_exploratory_price_contract import (
        price_cache_lock,
        price_cache_snapshot,
        price_universe_snapshot,
    )
    from scripts.research_integrity import atomic_write_json, file_sha256
    from scripts.run_industry_index_research_validation import (
        clean_history,
        fetch_industry_fundamentals,
        fetch_industry_history,
        normalize_industry_code,
    )
    from scripts.valuation_pit_contract import SHANGHAI


VERSION = "1.4.0"
ENTRY_DATE = "2026-06-23"
EXIT_DATE = "2026-07-21"
MIN_INDUSTRY_COUNT = 100
TARGET_CODES = ("801125", "801194", "801203", "801764")
QUARANTINED_HISTORY_CODES = ("801156",)
QUARANTINE_REASON = "provider_history_incompatible_with_append_only_cache"
TIME_GATE = datetime(2026, 7, 21, 15, 0, 0, tzinfo=SHANGHAI)
BASELINE_PRICE_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
PRICE_DIR = (
    ROOT
    / "data_catalog"
    / "cache"
    / "industry_index"
    / "history"
    / "settlement_2026_07_21"
    / "second"
)
AUDIT_JSON = (
    ROOT
    / "outputs"
    / "audit"
    / "fund_flow_exploratory_settlement_price_refresh_2026_07_21"
    / "run_summary.json"
)
STAGING_PREFIX = ".fund-flow-settlement-price-refresh-"
BOOTSTRAP_PREFIX = ".fund-flow-settlement-price-bootstrap-"
PRODUCER_PATHS = (
    Path(__file__).resolve(),
    ROOT / "scripts" / "run_industry_index_research_validation.py",
    ROOT / "scripts" / "fund_flow_exploratory_price_contract.py",
)

FundamentalsLoader = Callable[[str], pd.DataFrame]
HistoryFetcher = Callable[[str], pd.DataFrame]
HistoryCleaner = Callable[[pd.DataFrame, str], pd.DataFrame]
ReplaceFile = Callable[[Path, Path], None]
AuditWriter = Callable[[Path, dict[str, Any]], None]


class SourceContractError(ValueError):
    pass


def cache_snapshot(price_dir: Path) -> dict[str, Any]:
    return price_cache_snapshot(price_dir)


def relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def producer_attestations() -> list[dict[str, Any]]:
    return [
        {
            "path": relative_path(path),
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        for path in PRODUCER_PATHS
    ]


def bootstrap_settlement_price_cache(
    price_dir: Path, baseline_dir: Path, *, locks_held: bool = False
) -> dict[str, Any]:
    """Clone the mainline cache once and attest that mainline bytes were untouched."""

    if price_dir.resolve() == baseline_dir.resolve():
        raise ValueError("settlement_cache_must_be_separate_from_mainline_cache")
    price_dir.parent.mkdir(parents=True, exist_ok=True)
    if locks_held:
        return _bootstrap_settlement_price_cache_locked(price_dir, baseline_dir)
    lock_dirs = sorted({price_dir.resolve(), baseline_dir.resolve()}, key=lambda path: path.as_posix())
    with contextlib.ExitStack() as stack:
        for lock_dir in lock_dirs:
            stack.enter_context(price_cache_lock(lock_dir))
        return _bootstrap_settlement_price_cache_locked(price_dir, baseline_dir)


def _bootstrap_settlement_price_cache_locked(
    price_dir: Path, baseline_dir: Path
) -> dict[str, Any]:
    baseline_before = cache_snapshot(baseline_dir)
    settlement_before = cache_snapshot(price_dir)
    baseline_files = sorted(baseline_dir.glob("*.csv")) if baseline_dir.is_dir() else []
    baseline_codes = {path.stem for path in baseline_files}
    if (
        baseline_before.get("directory_exists") is not True
        or int(baseline_before.get("csv_file_count", 0)) < MIN_INDUSTRY_COUNT
        or not set(TARGET_CODES).issubset(baseline_codes)
        or not set(QUARANTINED_HISTORY_CODES).issubset(baseline_codes)
    ):
        raise SourceContractError("mainline_baseline_cache_incomplete")
    baseline_quarantine_hashes = {
        code: file_sha256(baseline_dir / f"{code}.csv")
        for code in QUARANTINED_HISTORY_CODES
    }

    if price_dir.is_dir():
        settlement_codes = {path.stem for path in price_dir.glob("*.csv")}
        if (
            int(settlement_before.get("csv_file_count", 0)) < MIN_INDUSTRY_COUNT
            or not set(TARGET_CODES).issubset(settlement_codes)
            or not set(QUARANTINED_HISTORY_CODES).issubset(settlement_codes)
        ):
            raise SourceContractError("settlement_cache_existing_incomplete")
        baseline_after = cache_snapshot(baseline_dir)
        settlement_after = cache_snapshot(price_dir)
        settlement_quarantine_hashes = {
            code: file_sha256(price_dir / f"{code}.csv")
            for code in QUARANTINED_HISTORY_CODES
        }
        return {
            "checked": True,
            "action": "reused_existing",
            "baseline_path": relative_path(baseline_dir),
            "settlement_path": relative_path(price_dir),
            "baseline_before": baseline_before,
            "baseline_after": baseline_after,
            "baseline_unchanged": baseline_after == baseline_before,
            "settlement_before": settlement_before,
            "settlement_after_bootstrap": settlement_after,
            "settlement_unchanged_during_bootstrap": settlement_after
            == settlement_before,
            "settlement_copied_from_baseline": False,
            "baseline_quarantined_file_sha256": baseline_quarantine_hashes,
            "settlement_quarantined_file_sha256": settlement_quarantine_hashes,
            "quarantined_files_match_baseline": (
                settlement_quarantine_hashes == baseline_quarantine_hashes
            ),
            "mainline_write_invoked": False,
        }

    stage_root = Path(
        tempfile.mkdtemp(prefix=BOOTSTRAP_PREFIX, dir=price_dir.parent)
    )
    staged_dir = stage_root / "second"
    try:
        staged_dir.mkdir()
        for source in baseline_files:
            shutil.copy2(source, staged_dir / source.name)
        if cache_snapshot(staged_dir) != baseline_before:
            raise SourceContractError("settlement_cache_bootstrap_hash_mismatch")
        if cache_snapshot(baseline_dir) != baseline_before:
            raise SourceContractError("mainline_baseline_changed_during_bootstrap")
        os.replace(staged_dir, price_dir)
        baseline_after = cache_snapshot(baseline_dir)
        settlement_after = cache_snapshot(price_dir)
        settlement_quarantine_hashes = {
            code: file_sha256(price_dir / f"{code}.csv")
            for code in QUARANTINED_HISTORY_CODES
        }
        return {
            "checked": True,
            "action": "created_from_mainline",
            "baseline_path": relative_path(baseline_dir),
            "settlement_path": relative_path(price_dir),
            "baseline_before": baseline_before,
            "baseline_after": baseline_after,
            "baseline_unchanged": baseline_after == baseline_before,
            "settlement_before": settlement_before,
            "settlement_after_bootstrap": settlement_after,
            "settlement_unchanged_during_bootstrap": False,
            "settlement_copied_from_baseline": settlement_after == baseline_before,
            "baseline_quarantined_file_sha256": baseline_quarantine_hashes,
            "settlement_quarantined_file_sha256": settlement_quarantine_hashes,
            "quarantined_files_match_baseline": (
                settlement_quarantine_hashes == baseline_quarantine_hashes
            ),
            "mainline_write_invoked": False,
        }
    finally:
        if stage_root.exists():
            resolved = stage_root.resolve()
            if (
                resolved.parent != price_dir.parent.resolve()
                or not resolved.name.startswith(BOOTSTRAP_PREFIX)
            ):
                raise ValueError("invalid_settlement_cache_bootstrap_path")
            shutil.rmtree(resolved)


def normalize_source_codes(fundamentals: pd.DataFrame) -> list[str]:
    if not isinstance(fundamentals, pd.DataFrame) or "行业代码" not in fundamentals.columns:
        raise SourceContractError("missing_industry_code_column")
    codes = [normalize_industry_code(value) for value in fundamentals["行业代码"].tolist()]
    if any(len(code) != 6 or not code.isdigit() for code in codes):
        raise SourceContractError("invalid_industry_code")
    if len(codes) != len(set(codes)):
        raise SourceContractError("duplicate_industry_code")
    if len(codes) < MIN_INDUSTRY_COUNT:
        raise SourceContractError("source_universe_below_minimum")
    if not set(TARGET_CODES).issubset(codes):
        raise SourceContractError("target_industry_missing_from_source")
    quarantine = set(QUARANTINED_HISTORY_CODES)
    if quarantine & set(TARGET_CODES):
        raise SourceContractError("target_industry_cannot_be_quarantined")
    if quarantine != {"801156"}:
        raise SourceContractError("quarantine_allowlist_changed")
    if not quarantine.issubset(codes):
        raise SourceContractError("quarantined_industry_missing_from_source")
    return sorted(codes)


def validate_cleaned_history(frame: pd.DataFrame, code: str) -> str | None:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return "empty_clean_history"
    if not {"日期", "收盘"}.issubset(frame.columns):
        return "missing_required_history_columns"
    if "代码" in frame.columns:
        observed = {
            normalize_industry_code(value)
            for value in frame["代码"].dropna().tolist()
            if str(value).strip()
        }
        if observed and observed != {code}:
            return "history_code_mismatch"
    return None


def empty_history_continuity_audit(expected_count: int = 0) -> dict[str, Any]:
    return {
        "checked": False,
        "expected_industry_count": expected_count,
        "existing_file_count": 0,
        "staged_file_count": 0,
        "verified_industry_count": 0,
        "failed_industry_count": 0,
        "failed_industry_codes": [],
        "failure_reason_counts": {},
        "failure_details": [],
        "existing_row_count": 0,
        "staged_row_count": 0,
        "existing_valid_row_count": 0,
        "staged_valid_row_count": 0,
        "retained_existing_date_count": 0,
        "duplicate_date_industry_count": 0,
        "historical_rows_compared": 0,
        "historical_rows_changed": 0,
        "historical_rows_unchanged": False,
        "append_only_contract_passed": False,
        "existing_universe_aggregate_sha256": "",
        "staged_universe_aggregate_sha256": "",
        "validation_inputs_unchanged": False,
        "history_continuity_ready": False,
        "price_values_retained": False,
    }


def history_contract(frame: pd.DataFrame, code: str) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    required = {"代码", "日期", "收盘"}
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {
            "row_count": 0,
            "valid_row_count": 0,
            "date_set": set(),
            "minimum_date": "",
            "maximum_date": "",
            "columns": set(),
        }, ["empty_history"]
    columns = set(frame.columns)
    if not required.issubset(columns):
        return {
            "row_count": len(frame),
            "valid_row_count": 0,
            "date_set": set(),
            "minimum_date": "",
            "maximum_date": "",
            "columns": columns,
        }, ["missing_identity_or_price_columns"]

    dates = pd.to_datetime(frame["日期"], errors="coerce")
    closes = pd.to_numeric(frame["收盘"], errors="coerce")
    finite_positive = closes.map(
        lambda value: pd.notna(value) and math.isfinite(float(value)) and float(value) > 0
    )
    valid_mask = dates.notna() & finite_positive
    normalized_dates = dates.dt.strftime("%Y-%m-%d")
    valid_dates = set(normalized_dates[valid_mask].tolist())
    parsed_dates = normalized_dates[dates.notna()]
    if not bool(valid_mask.all()):
        reasons.append("invalid_history_row")
    if parsed_dates.duplicated(keep=False).any():
        reasons.append("duplicate_history_date")

    observed_codes = frame["代码"].map(normalize_industry_code)
    if observed_codes.empty or observed_codes.ne(code).any():
        reasons.append("history_code_mismatch_or_missing")

    return {
        "row_count": len(frame),
        "valid_row_count": int(valid_mask.sum()),
        "date_set": valid_dates,
        "minimum_date": min(valid_dates) if valid_dates else "",
        "maximum_date": max(valid_dates) if valid_dates else "",
        "columns": columns,
    }, reasons


def canonical_history_row_hashes(
    frame: pd.DataFrame,
    *,
    columns: set[str],
) -> dict[str, str]:
    ordered_columns = sorted(columns)
    dates = pd.to_datetime(frame["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    result: dict[str, str] = {}
    for index, date_value in dates.items():
        if not isinstance(date_value, str):
            continue
        cells: list[str] = []
        for column in ordered_columns:
            value = frame.at[index, column]
            if column == "日期":
                normalized = date_value
            elif column == "代码":
                normalized = normalize_industry_code(value)
            elif pd.isna(value):
                normalized = "<NULL>"
            else:
                text = str(value).strip()
                try:
                    number = Decimal(text)
                    normalized = format(number.normalize(), "f") if number.is_finite() else text
                except InvalidOperation:
                    normalized = text
            cells.append(f"{column}={normalized}")
        result[date_value] = hashlib.sha256("\x1f".join(cells).encode("utf-8")).hexdigest()
    return result


def validate_history_continuity(
    *,
    staging_dir: Path,
    price_dir: Path,
    expected_codes: list[str],
) -> dict[str, Any]:
    audit = empty_history_continuity_audit(len(expected_codes))
    audit["checked"] = True
    existing_before = price_universe_snapshot(price_dir, expected_codes)
    staged_before = price_universe_snapshot(staging_dir, expected_codes)
    reason_counts: dict[str, int] = {}
    failure_details: list[dict[str, Any]] = []
    verified_codes: list[str] = []

    for code in expected_codes:
        existing_path = price_dir / f"{code}.csv"
        staged_path = staging_dir / f"{code}.csv"
        reasons: list[str] = []
        if existing_path.is_file():
            audit["existing_file_count"] += 1
        else:
            reasons.append("existing_history_missing")
        if staged_path.is_file():
            audit["staged_file_count"] += 1
        else:
            reasons.append("staged_history_missing")

        existing = pd.DataFrame()
        staged = pd.DataFrame()
        if not reasons:
            try:
                existing = pd.read_csv(existing_path, encoding="utf-8-sig")
            except Exception:
                reasons.append("existing_history_read_failed")
            try:
                staged = pd.read_csv(staged_path, encoding="utf-8-sig")
            except Exception:
                reasons.append("staged_history_read_failed")

        existing_contract: dict[str, Any] = {}
        staged_contract: dict[str, Any] = {}
        if not reasons:
            existing_contract, existing_reasons = history_contract(existing, code)
            staged_contract, staged_reasons = history_contract(staged, code)
            reasons.extend(f"existing_{reason}" for reason in existing_reasons)
            reasons.extend(f"staged_{reason}" for reason in staged_reasons)

        if not reasons:
            existing_dates = set(existing_contract["date_set"])
            staged_dates = set(staged_contract["date_set"])
            audit["existing_row_count"] += int(existing_contract["row_count"])
            audit["staged_row_count"] += int(staged_contract["row_count"])
            audit["existing_valid_row_count"] += int(existing_contract["valid_row_count"])
            audit["staged_valid_row_count"] += int(staged_contract["valid_row_count"])
            audit["retained_existing_date_count"] += len(existing_dates & staged_dates)
            if int(staged_contract["row_count"]) < int(existing_contract["row_count"]):
                reasons.append("row_count_decreased")
            if int(staged_contract["valid_row_count"]) < int(existing_contract["valid_row_count"]):
                reasons.append("valid_row_count_decreased")
            if not existing_dates.issubset(staged_dates):
                reasons.append("existing_valid_date_missing_from_staged")
            if str(staged_contract["minimum_date"]) > str(existing_contract["minimum_date"]):
                reasons.append("history_start_boundary_regressed")
            if str(staged_contract["maximum_date"]) < str(existing_contract["maximum_date"]):
                reasons.append("history_end_boundary_regressed")
            if not set(existing_contract["columns"]).issubset(staged_contract["columns"]):
                reasons.append("history_schema_regressed")
            if not reasons:
                existing_row_hashes = canonical_history_row_hashes(
                    existing,
                    columns=set(existing_contract["columns"]),
                )
                staged_row_hashes = canonical_history_row_hashes(
                    staged,
                    columns=set(existing_contract["columns"]),
                )
                changed_dates = [
                    date
                    for date, existing_hash in existing_row_hashes.items()
                    if staged_row_hashes.get(date) != existing_hash
                ]
                audit["historical_rows_compared"] += len(existing_row_hashes)
                audit["historical_rows_changed"] += len(changed_dates)
                if changed_dates:
                    reasons.append("historical_row_changed")

        reasons = list(dict.fromkeys(reasons))
        if reasons:
            if any("duplicate_history_date" in reason for reason in reasons):
                audit["duplicate_date_industry_count"] += 1
            failure_details.append({"industry_code": code, "reason_codes": reasons})
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        else:
            verified_codes.append(code)

    audit["verified_industry_count"] = len(verified_codes)
    audit["failed_industry_count"] = len(failure_details)
    audit["failed_industry_codes"] = [item["industry_code"] for item in failure_details]
    audit["failure_reason_counts"] = dict(sorted(reason_counts.items()))
    audit["failure_details"] = failure_details
    existing_after = price_universe_snapshot(price_dir, expected_codes)
    staged_after = price_universe_snapshot(staging_dir, expected_codes)
    inputs_unchanged = existing_before == existing_after and staged_before == staged_after
    audit["existing_universe_aggregate_sha256"] = existing_after["aggregate_sha256"]
    audit["staged_universe_aggregate_sha256"] = staged_after["aggregate_sha256"]
    audit["validation_inputs_unchanged"] = inputs_unchanged
    audit["historical_rows_unchanged"] = audit["historical_rows_changed"] == 0
    audit["append_only_contract_passed"] = (
        len(verified_codes) == len(expected_codes)
        and audit["historical_rows_unchanged"]
        and inputs_unchanged
    )
    audit["history_continuity_ready"] = audit["append_only_contract_passed"]
    return audit


def stage_histories(
    *,
    codes: list[str],
    staging_dir: Path,
    price_dir: Path,
    history_fetcher: HistoryFetcher,
    history_cleaner: HistoryCleaner,
) -> dict[str, Any]:
    staging_dir.mkdir(parents=True, exist_ok=False)
    audit: dict[str, Any] = {
        "attempted": True,
        "expected_industry_count": len(codes),
        "succeeded_industry_count": 0,
        "failed_industry_count": 0,
        "failed_industry_codes": [],
        "failure_phase": "",
        "failure_type": "",
        "quarantined_industry_count": 0,
        "quarantined_industry_codes": [],
        "quarantine_reason": QUARANTINE_REASON,
        "quarantine_attestations": [],
        "quarantine_attestation_complete": False,
        "source_accounted_industry_count": 0,
    }
    for code in codes:
        if code in QUARANTINED_HISTORY_CODES:
            try:
                existing_path = price_dir / f"{code}.csv"
                staged_path = staging_dir / f"{code}.csv"
                source_sha256_before = file_sha256(existing_path)
                existing = pd.read_csv(existing_path, encoding="utf-8-sig")
                contract, reasons = history_contract(existing, code)
                if reasons:
                    raise SourceContractError(
                        "quarantined_history_contract_failed:" + ",".join(reasons)
                    )
                settlement_dates = {ENTRY_DATE, EXIT_DATE} & set(contract["date_set"])
                if settlement_dates:
                    raise SourceContractError(
                        "quarantined_history_contains_settlement_date:"
                        + ",".join(sorted(settlement_dates))
                    )
                shutil.copy2(existing_path, staged_path)
                source_sha256_after_copy = file_sha256(existing_path)
                staged_sha256 = file_sha256(staged_path)
                if not (
                    source_sha256_before
                    == source_sha256_after_copy
                    == staged_sha256
                ):
                    raise SourceContractError("quarantined_history_copy_hash_mismatch")
                staged_existing = pd.read_csv(staged_path, encoding="utf-8-sig")
                staged_contract, staged_reasons = history_contract(staged_existing, code)
                if staged_reasons:
                    raise SourceContractError(
                        "staged_quarantined_history_contract_failed:"
                        + ",".join(staged_reasons)
                    )
                staged_settlement_dates = {ENTRY_DATE, EXIT_DATE} & set(
                    staged_contract["date_set"]
                )
                if staged_settlement_dates:
                    raise SourceContractError(
                        "staged_quarantined_history_contains_settlement_date:"
                        + ",".join(sorted(staged_settlement_dates))
                    )
            except Exception as exc:
                audit.update(
                    failed_industry_count=1,
                    failed_industry_codes=[code],
                    failure_phase="quarantine",
                    failure_type=type(exc).__name__,
                )
                return audit
            audit["quarantine_attestations"].append({
                "industry_code": code,
                "source_sha256_before": source_sha256_before,
                "source_sha256_after_copy": source_sha256_after_copy,
                "staged_sha256": staged_sha256,
                "source_unchanged_during_staging": (
                    source_sha256_before == source_sha256_after_copy
                ),
                "staged_matches_source": staged_sha256 == source_sha256_before,
            })
            audit["quarantined_industry_count"] += 1
            audit["quarantined_industry_codes"].append(code)
            audit["source_accounted_industry_count"] += 1
            continue
        try:
            raw = history_fetcher(code)
        except Exception as exc:  # network/provider failure must fail closed
            audit.update(
                failed_industry_count=1,
                failed_industry_codes=[code],
                failure_phase="fetch",
                failure_type=type(exc).__name__,
            )
            return audit
        try:
            cleaned = history_cleaner(raw, EXIT_DATE)
            validation_error = validate_cleaned_history(cleaned, code)
            if validation_error:
                raise SourceContractError(validation_error)
            cleaned.to_csv(staging_dir / f"{code}.csv", index=False, encoding="utf-8-sig")
        except Exception as exc:
            audit.update(
                failed_industry_count=1,
                failed_industry_codes=[code],
                failure_phase="clean_or_stage",
                failure_type=type(exc).__name__,
            )
            return audit
        audit["succeeded_industry_count"] += 1
        audit["source_accounted_industry_count"] += 1
    expected_quarantine = sorted(QUARANTINED_HISTORY_CODES)
    accounting_valid = (
        audit["succeeded_industry_count"] == len(codes) - len(expected_quarantine)
        and audit["quarantined_industry_count"] == len(expected_quarantine)
        and sorted(audit["quarantined_industry_codes"]) == expected_quarantine
        and audit["source_accounted_industry_count"] == len(codes)
        and audit["failed_industry_count"] == 0
        and len(audit["quarantine_attestations"]) == len(expected_quarantine)
    )
    audit["quarantine_attestation_complete"] = accounting_valid
    if not accounting_valid:
        audit.update(
            failed_industry_count=1,
            failed_industry_codes=["source_accounting"],
            failure_phase="accounting",
            failure_type="SourceContractError",
        )
    return audit


def staged_exact_date_coverage(staging_dir: Path, expected_codes: list[str]) -> dict[str, Any]:
    entry_codes: set[str] = set()
    exit_codes: set[str] = set()
    invalid_codes: list[str] = []
    max_date = ""
    files = sorted(staging_dir.glob("*.csv"))
    expected_set = set(expected_codes)
    observed_files = {path.stem for path in files}
    quarantine_set = set(QUARANTINED_HISTORY_CODES)
    quarantined_required_date_codes: set[str] = set()

    for code in expected_codes:
        path = staging_dir / f"{code}.csv"
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig")
        except Exception:
            invalid_codes.append(code)
            continue
        if not {"日期", "收盘"}.issubset(frame.columns):
            invalid_codes.append(code)
            continue
        dates = pd.to_datetime(frame["日期"], errors="coerce")
        closes = pd.to_numeric(frame["收盘"], errors="coerce")
        valid_dates = dates.dropna()
        if not valid_dates.empty:
            max_date = max(max_date, valid_dates.max().strftime("%Y-%m-%d"))
        for required_date, target_set in ((ENTRY_DATE, entry_codes), (EXIT_DATE, exit_codes)):
            required = closes[dates.dt.strftime("%Y-%m-%d") == required_date]
            if code in quarantine_set and len(required) > 0:
                quarantined_required_date_codes.add(code)
                continue
            if len(required) == 1 and pd.notna(required.iloc[0]):
                value = float(required.iloc[0])
                if math.isfinite(value) and value > 0:
                    target_set.add(code)

    target_set = set(TARGET_CODES)
    common_codes = entry_codes & exit_codes
    exact_ready = (
        observed_files == expected_set
        and not invalid_codes
        and not quarantined_required_date_codes
        and len(entry_codes) >= MIN_INDUSTRY_COUNT
        and len(exit_codes) >= MIN_INDUSTRY_COUNT
        and len(common_codes) >= MIN_INDUSTRY_COUNT
        and len(entry_codes) <= len(expected_codes) - len(quarantine_set)
        and len(exit_codes) <= len(expected_codes) - len(quarantine_set)
        and len(common_codes) <= len(expected_codes) - len(quarantine_set)
        and target_set.issubset(common_codes)
    )
    return {
        "checked": True,
        "source_file_count": len(files),
        "expected_source_file_count": len(expected_codes),
        "invalid_file_count": len(invalid_codes),
        "invalid_industry_codes": sorted(invalid_codes),
        "quarantined_required_date_codes": sorted(quarantined_required_date_codes),
        "quarantine_exact_date_exclusion_passed": not quarantined_required_date_codes,
        "entry_industry_count": len(entry_codes),
        "exit_industry_count": len(exit_codes),
        "entry_exit_common_count": len(common_codes),
        "target_entry_count": len(target_set & entry_codes),
        "target_exit_count": len(target_set & exit_codes),
        "target_common_count": len(target_set & common_codes),
        "minimum_industry_count": MIN_INDUSTRY_COUNT,
        "overall_max_date": max_date,
        "exact_coverage_ready": exact_ready,
        "price_values_retained": False,
    }


def transactional_replace(
    *,
    staging_dir: Path,
    backup_dir: Path,
    price_dir: Path,
    codes: list[str],
    replace_file: ReplaceFile | None = None,
    rollback_replace_file: ReplaceFile | None = None,
    expected_existing_aggregate_sha256: str = "",
    expected_staged_aggregate_sha256: str = "",
) -> dict[str, Any]:
    replace_file = replace_file or os.replace
    rollback_replace_file = rollback_replace_file or os.replace
    backup_dir.mkdir(parents=True, exist_ok=False)
    staged_hashes = {code: file_sha256(staging_dir / f"{code}.csv") for code in codes}
    staged_attestation = price_universe_snapshot(staging_dir, codes)
    if (
        staged_attestation["observed_file_count"] != len(codes)
        or staged_attestation["missing_industry_codes"]
    ):
        raise OSError("staged_universe_attestation_incomplete")
    existing_attestation = price_universe_snapshot(price_dir, codes)
    if (
        not expected_existing_aggregate_sha256
        or existing_attestation["aggregate_sha256"] != expected_existing_aggregate_sha256
    ):
        raise OSError("official_universe_changed_after_continuity_validation")
    if (
        not expected_staged_aggregate_sha256
        or staged_attestation["aggregate_sha256"] != expected_staged_aggregate_sha256
    ):
        raise OSError("staged_universe_changed_after_continuity_validation")
    original_exists: dict[str, bool] = {}
    replaced: list[str] = []
    current_code = ""

    try:
        for code in codes:
            target = price_dir / f"{code}.csv"
            backup = backup_dir / f"{code}.csv"
            original_exists[code] = target.exists()
            if target.exists():
                shutil.copy2(target, backup)
                if file_sha256(target) != file_sha256(backup):
                    raise OSError("backup_hash_mismatch")

        if price_universe_snapshot(price_dir, codes)["aggregate_sha256"] != expected_existing_aggregate_sha256:
            raise OSError("official_universe_changed_during_backup")
        if price_universe_snapshot(staging_dir, codes)["aggregate_sha256"] != expected_staged_aggregate_sha256:
            raise OSError("staged_universe_changed_during_backup")

        for code in codes:
            current_code = code
            replace_file(staging_dir / f"{code}.csv", price_dir / f"{code}.csv")
            replaced.append(code)
            current_code = ""

        for code in codes:
            target = price_dir / f"{code}.csv"
            if not target.is_file() or file_sha256(target) != staged_hashes[code]:
                raise OSError("post_replace_hash_mismatch")
        committed_attestation = price_universe_snapshot(price_dir, codes)
        if committed_attestation["aggregate_sha256"] != staged_attestation["aggregate_sha256"]:
            raise OSError("committed_universe_attestation_mismatch")
        return {
            "attempted": True,
            "succeeded": True,
            "replaced_file_count": len(replaced),
            "rollback_performed": False,
            "rollback_error_count": 0,
            "rollback_error_codes": [],
            "failure_type": "",
            "staged_universe_attestation": staged_attestation,
            "committed_universe_attestation": committed_attestation,
            "staged_and_committed_hashes_match": True,
        }
    except BaseException as exc:
        possibly_changed = list(dict.fromkeys([*replaced, *([current_code] if current_code else [])]))
        rollback_errors: list[str] = []
        for code in reversed(possibly_changed):
            target = price_dir / f"{code}.csv"
            backup = backup_dir / f"{code}.csv"
            try:
                if original_exists.get(code, False):
                    rollback_replace_file(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except BaseException:
                rollback_errors.append(code)
        return {
            "attempted": bool(possibly_changed),
            "succeeded": False,
            "replaced_file_count": len(replaced),
            "rollback_performed": bool(possibly_changed),
            "rollback_error_count": len(rollback_errors),
            "rollback_error_codes": sorted(rollback_errors),
            "failure_type": type(exc).__name__,
            "staged_universe_attestation": staged_attestation,
            "committed_universe_attestation": {},
            "staged_and_committed_hashes_match": False,
        }


def safe_remove_staging(stage_root: Path, expected_parent: Path) -> bool:
    resolved = stage_root.resolve()
    parent = expected_parent.resolve()
    if resolved.parent != parent or not resolved.name.startswith(STAGING_PREFIX):
        raise ValueError("refusing_to_remove_unrecognized_staging_path")
    shutil.rmtree(resolved)
    return True


def base_summary(now: datetime, before: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "script_version": VERSION,
        "audit_mode": "fund_flow_exploratory_settlement_price_only_refresh",
        "generated_at": now.isoformat(),
        "time_gate": TIME_GATE.isoformat(),
        "time_gate_passed": now >= TIME_GATE,
        "entry_date": ENTRY_DATE,
        "exit_date": EXIT_DATE,
        "minimum_industry_count": MIN_INDUSTRY_COUNT,
        "target_industry_codes": list(TARGET_CODES),
        "source_route": "run_industry_index_research_validation.fetch_industry_history",
        "cache_scope": "dedicated_exploratory_settlement_only",
        "mainline_price_cache_write_invoked": False,
        "cache_bootstrap": {},
        "producer_attestations": producer_attestations(),
        "candidate_generation_invoked": False,
        "ledger_write_invoked": False,
        "account_or_trade_write_invoked": False,
        "price_values_retained_in_audit": False,
        "completion_status": "not_started",
        "official_cache_write_attempted": False,
        "official_cache_touched": False,
        "official_cache_restored": False,
        "authoritative_before": before,
        "authoritative_after": before,
        "authoritative_cache_unchanged": True,
        "staging_cleaned": True,
        "staging_recovery_path": "",
        "fetch": {
            "attempted": False,
            "expected_industry_count": 0,
            "succeeded_industry_count": 0,
            "failed_industry_count": 0,
            "failed_industry_codes": [],
            "failure_phase": "",
            "failure_type": "",
            "quarantined_industry_count": 0,
            "quarantined_industry_codes": [],
            "quarantine_reason": QUARANTINE_REASON,
            "source_accounted_industry_count": 0,
            "quarantine_attestations": [],
            "quarantine_attestation_complete": False,
        },
        "coverage": {
            "checked": False,
            "source_file_count": 0,
            "expected_source_file_count": 0,
            "invalid_file_count": 0,
            "invalid_industry_codes": [],
            "quarantined_required_date_codes": [],
            "quarantine_exact_date_exclusion_passed": False,
            "entry_industry_count": 0,
            "exit_industry_count": 0,
            "entry_exit_common_count": 0,
            "target_entry_count": 0,
            "target_exit_count": 0,
            "target_common_count": 0,
            "minimum_industry_count": MIN_INDUSTRY_COUNT,
            "overall_max_date": "",
            "exact_coverage_ready": False,
            "price_values_retained": False,
        },
        "history_continuity": empty_history_continuity_audit(),
        "commit": {
            "attempted": False,
            "succeeded": False,
            "replaced_file_count": 0,
            "rollback_performed": False,
            "rollback_error_count": 0,
            "rollback_error_codes": [],
            "failure_type": "",
            "staged_universe_attestation": {},
            "committed_universe_attestation": {},
            "staged_and_committed_hashes_match": False,
        },
    }


def finalize_quarantine_attestations(
    fetch: dict[str, Any], price_dir: Path
) -> None:
    raw_items = fetch.get("quarantine_attestations")
    items = raw_items if isinstance(raw_items, list) else []
    expected_codes = sorted(QUARANTINED_HISTORY_CODES)
    observed_codes: list[str] = []
    all_match = len(items) == len(expected_codes)
    for raw_item in items:
        if not isinstance(raw_item, dict):
            all_match = False
            continue
        item = raw_item
        code = str(item.get("industry_code", ""))
        observed_codes.append(code)
        path = price_dir / f"{code}.csv"
        committed_sha256 = file_sha256(path) if path.is_file() else ""
        item["committed_sha256"] = committed_sha256
        item["committed_matches_source"] = (
            bool(committed_sha256)
            and committed_sha256 == item.get("source_sha256_before")
        )
        all_match = bool(
            all_match
            and item.get("source_unchanged_during_staging") is True
            and item.get("staged_matches_source") is True
            and item["committed_matches_source"] is True
            and item.get("source_sha256_before")
            == item.get("source_sha256_after_copy")
            == item.get("staged_sha256")
            == committed_sha256
        )
    fetch["quarantine_attestations"] = items
    fetch["quarantine_attestation_complete"] = bool(
        all_match and observed_codes == expected_codes
    )


def finalize_summary(summary: dict[str, Any], price_dir: Path) -> dict[str, Any]:
    finalize_quarantine_attestations(summary["fetch"], price_dir)
    after = cache_snapshot(price_dir)
    unchanged = after == summary["authoritative_before"]
    summary["authoritative_after"] = after
    summary["authoritative_cache_unchanged"] = unchanged
    if summary["completion_status"] == "committed":
        summary["official_cache_touched"] = True
    elif summary["official_cache_write_attempted"] and unchanged:
        summary["official_cache_restored"] = True
        summary["official_cache_touched"] = False
    elif summary["official_cache_write_attempted"]:
        summary["official_cache_touched"] = True
    elif not unchanged:
        summary["official_cache_touched"] = True
    return summary


def run_price_refresh(
    *,
    now: datetime,
    price_dir: Path,
    fundamentals_loader: FundamentalsLoader = fetch_industry_fundamentals,
    history_fetcher: HistoryFetcher = fetch_industry_history,
    history_cleaner: HistoryCleaner = clean_history,
    replace_file: ReplaceFile | None = None,
    rollback_replace_file: ReplaceFile | None = None,
    before_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if now.tzinfo is None:
        raise ValueError("now_must_be_timezone_aware")
    now = now.astimezone(SHANGHAI)
    before = dict(before_snapshot) if before_snapshot is not None else cache_snapshot(price_dir)
    summary = base_summary(now, before)
    if now < TIME_GATE:
        summary["completion_status"] = "blocked_pre_start"
        return finalize_summary(summary, price_dir)
    if not price_dir.is_dir():
        summary["completion_status"] = "official_cache_missing"
        return finalize_summary(summary, price_dir)

    try:
        fundamentals = fundamentals_loader("second")
        codes = normalize_source_codes(fundamentals)
    except Exception as exc:
        summary["completion_status"] = "source_contract_failed"
        summary["fetch"]["failure_phase"] = "fundamentals"
        summary["fetch"]["failure_type"] = type(exc).__name__
        return finalize_summary(summary, price_dir)

    stage_root = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=price_dir.parent))
    preserve_staging = False
    try:
        staging_dir = stage_root / "new"
        backup_dir = stage_root / "backup"
        summary["fetch"] = stage_histories(
            codes=codes,
            staging_dir=staging_dir,
            price_dir=price_dir,
            history_fetcher=history_fetcher,
            history_cleaner=history_cleaner,
        )
        if summary["fetch"]["failed_industry_count"]:
            summary["completion_status"] = "fetch_or_staging_failed"
            return finalize_summary(summary, price_dir)

        summary["coverage"] = staged_exact_date_coverage(staging_dir, codes)
        if not summary["coverage"]["exact_coverage_ready"]:
            summary["completion_status"] = "exact_coverage_failed"
            return finalize_summary(summary, price_dir)

        summary["history_continuity"] = validate_history_continuity(
            staging_dir=staging_dir,
            price_dir=price_dir,
            expected_codes=codes,
        )
        if not summary["history_continuity"]["history_continuity_ready"]:
            summary["completion_status"] = "history_continuity_failed"
            return finalize_summary(summary, price_dir)

        try:
            summary["commit"] = transactional_replace(
                staging_dir=staging_dir,
                backup_dir=backup_dir,
                price_dir=price_dir,
                codes=codes,
                replace_file=replace_file,
                rollback_replace_file=rollback_replace_file,
                expected_existing_aggregate_sha256=summary["history_continuity"][
                    "existing_universe_aggregate_sha256"
                ],
                expected_staged_aggregate_sha256=summary["history_continuity"][
                    "staged_universe_aggregate_sha256"
                ],
            )
        except Exception as exc:
            summary["commit"] = {
                "attempted": False,
                "succeeded": False,
                "replaced_file_count": 0,
                "rollback_performed": False,
                "rollback_error_count": 0,
                "rollback_error_codes": [],
                "failure_type": type(exc).__name__,
                "staged_universe_attestation": {},
                "committed_universe_attestation": {},
                "staged_and_committed_hashes_match": False,
            }
        summary["official_cache_write_attempted"] = bool(summary["commit"]["attempted"])
        if summary["commit"]["succeeded"]:
            summary["completion_status"] = "committed"
        else:
            current = cache_snapshot(price_dir)
            restored = current == before and summary["commit"]["rollback_error_count"] == 0
            if restored and not summary["commit"]["attempted"]:
                summary["completion_status"] = "commit_validation_failed_no_write"
            else:
                summary["completion_status"] = (
                    "commit_failed_rolled_back" if restored else "commit_failed_rollback_incomplete"
                )
            preserve_staging = not restored
            if preserve_staging:
                summary["staging_recovery_path"] = str(stage_root.resolve())
        return finalize_summary(summary, price_dir)
    finally:
        if not preserve_staging:
            try:
                safe_remove_staging(stage_root, price_dir.parent)
            except Exception:
                summary["staging_cleaned"] = False
                summary["staging_recovery_path"] = str(stage_root.resolve())


def execute_price_refresh(
    *,
    now: datetime,
    price_dir: Path,
    audit_json: Path,
    fundamentals_loader: FundamentalsLoader = fetch_industry_fundamentals,
    history_fetcher: HistoryFetcher = fetch_industry_history,
    history_cleaner: HistoryCleaner = clean_history,
    replace_file: ReplaceFile | None = None,
    rollback_replace_file: ReplaceFile | None = None,
    audit_writer: AuditWriter = atomic_write_json,
    lock_held: bool = False,
) -> dict[str, Any]:
    if now.tzinfo is None:
        raise ValueError("now_must_be_timezone_aware")
    now = now.astimezone(SHANGHAI)
    lock_context = (
        contextlib.nullcontext()
        if lock_held
        else price_cache_lock(price_dir)
    )
    with lock_context:
        initial_before = cache_snapshot(price_dir)
        in_progress = base_summary(now, initial_before)
        in_progress["completion_status"] = "refresh_in_progress"
        audit_writer(audit_json, in_progress)
        try:
            summary = run_price_refresh(
                now=now,
                price_dir=price_dir,
                fundamentals_loader=fundamentals_loader,
                history_fetcher=history_fetcher,
                history_cleaner=history_cleaner,
                replace_file=replace_file,
                rollback_replace_file=rollback_replace_file,
                before_snapshot=initial_before,
            )
        except Exception as exc:
            summary = base_summary(now, initial_before)
            summary["completion_status"] = "internal_error"
            summary["fetch"]["failure_phase"] = "internal"
            summary["fetch"]["failure_type"] = type(exc).__name__
            summary = finalize_summary(summary, price_dir)
        audit_writer(audit_json, summary)
        return summary


def execute_settlement_price_refresh(
    *,
    now: datetime,
    price_dir: Path,
    baseline_dir: Path,
    audit_json: Path,
    fundamentals_loader: FundamentalsLoader = fetch_industry_fundamentals,
    history_fetcher: HistoryFetcher = fetch_industry_history,
    history_cleaner: HistoryCleaner = clean_history,
    replace_file: ReplaceFile | None = None,
    rollback_replace_file: ReplaceFile | None = None,
    audit_writer: AuditWriter = atomic_write_json,
) -> dict[str, Any]:
    if price_dir.resolve() == baseline_dir.resolve():
        raise ValueError("settlement_cache_must_be_separate_from_mainline_cache")
    price_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_dirs = sorted(
        {price_dir.resolve(), baseline_dir.resolve()},
        key=lambda path: path.as_posix(),
    )
    with contextlib.ExitStack() as stack:
        for lock_dir in lock_dirs:
            stack.enter_context(price_cache_lock(lock_dir))
        bootstrap = bootstrap_settlement_price_cache(
            price_dir, baseline_dir, locks_held=True
        )
        summary = execute_price_refresh(
            now=now,
            price_dir=price_dir,
            audit_json=audit_json,
            fundamentals_loader=fundamentals_loader,
            history_fetcher=history_fetcher,
            history_cleaner=history_cleaner,
            replace_file=replace_file,
            rollback_replace_file=rollback_replace_file,
            audit_writer=audit_writer,
            lock_held=True,
        )
        baseline_after_refresh = cache_snapshot(baseline_dir)
        bootstrap["baseline_after_refresh"] = baseline_after_refresh
        bootstrap["baseline_unchanged_through_refresh"] = (
            baseline_after_refresh == bootstrap["baseline_before"]
        )
        summary["cache_bootstrap"] = bootstrap
        summary["mainline_price_cache_write_invoked"] = False
        audit_writer(audit_json, summary)
        return summary


def self_check() -> None:
    assert TIME_GATE.isoformat() == "2026-07-21T15:00:00+08:00"
    assert len(TARGET_CODES) == 4
    assert QUARANTINED_HISTORY_CODES == ("801156",)
    assert not set(QUARANTINED_HISTORY_CODES) & set(TARGET_CODES)
    assert QUARANTINE_REASON == "provider_history_incompatible_with_append_only_cache"
    assert MIN_INDUSTRY_COUNT == 100
    assert PRICE_DIR.name == "second"
    assert PRICE_DIR.resolve() != BASELINE_PRICE_DIR.resolve()
    assert {relative_path(path) for path in PRODUCER_PATHS} == {
        "scripts/refresh_fund_flow_exploratory_settlement_prices.py",
        "scripts/run_industry_index_research_validation.py",
        "scripts/fund_flow_exploratory_price_contract.py",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transactionally refresh only the SW2 histories needed by the exploratory settlement."
    )
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        print("self_check=pass")
        return

    try:
        summary = execute_settlement_price_refresh(
            now=datetime.now(SHANGHAI),
            price_dir=PRICE_DIR,
            baseline_dir=BASELINE_PRICE_DIR,
            audit_json=AUDIT_JSON,
        )
    except Exception as exc:
        print(f"completion_status=audit_write_or_lock_failed")
        print(f"failure_type={type(exc).__name__}")
        print(f"audit_json={AUDIT_JSON.resolve()}")
        raise SystemExit(4) from exc
    print(f"completion_status={summary['completion_status']}")
    print(f"time_gate_passed={str(summary['time_gate_passed']).lower()}")
    print(f"entry_industry_count={summary['coverage']['entry_industry_count']}")
    print(f"exit_industry_count={summary['coverage']['exit_industry_count']}")
    print(f"target_exit_count={summary['coverage']['target_exit_count']}")
    print(f"official_cache_touched={str(summary['official_cache_touched']).lower()}")
    print(f"audit_json={AUDIT_JSON.resolve()}")
    if summary["completion_status"] != "committed":
        raise SystemExit(3)


if __name__ == "__main__":
    main()
