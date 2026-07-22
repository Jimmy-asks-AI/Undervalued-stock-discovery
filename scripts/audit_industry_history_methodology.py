#!/usr/bin/env python
"""Audit the SW second-level industry price-history universe without inventing PIT data.

The current monitoring gate and the historical-research gate intentionally answer
different questions:

* current monitoring needs exactly the governed 131 files and at least 120 fresh
  files;
* historical promotion additionally needs row-level availability and a dated
  classification history.  A current SW snapshot is never accepted as a
  substitute for that history.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
DEFAULT_SNAPSHOT_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_snapshots" / "second"
DEFAULT_CLASSIFICATION_HISTORY = (
    ROOT / "data_catalog" / "cache" / "industry_index" / "classification_history" / "second.csv"
)
DEFAULT_OUTPUT = ROOT / "outputs" / "audit" / "industry_history_methodology"
DEFAULT_TRADING_CALENDAR = ROOT / "data_catalog" / "cache" / "trading_calendar" / "a_share_trade_calendar.csv"

SCHEMA_VERSION = "1.0.0"
REQUIRED_INDUSTRY_COUNT = 131
MINIMUM_FRESH_INDUSTRY_COUNT = 120
MAX_STALE_CALENDAR_DAYS = 4
SW_SECOND_CODE_PATTERN = re.compile(r"^801\d{3}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FROZEN_TRADING_CALENDAR_SHA256 = "f348dd4c8863a5f2a5ff543a427c36198dbe3ca00f01a268f736490b2989d975"
SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_DECISION_CUTOFF = time(15, 0)
VERIFIED_AVAILABILITY_BASES = {"source_publication_timestamp", "vendor_available_timestamp"}
ACTIVE_REVISION_STATUSES = {"original", "restated"}
SOURCE_HASH_BASIS = "immutable_raw_artifact_sha256"
VERIFIED_DATA_STATUS = "pit_verified"

HISTORY_CODE_COLUMNS = ("industry_code", "代码")
HISTORY_DATE_COLUMNS = ("trade_date", "日期")
HISTORY_CLOSE_COLUMNS = ("close", "收盘")
HISTORY_PIT_REQUIRED_COLUMNS = (
    "published_at",
    "available_date",
    "fetched_at",
    "source",
    "source_version",
    "source_hash",
    "source_hash_basis",
    "source_artifact_path",
    "revision_status",
    "availability_basis",
    "data_status",
)
CURRENT_SNAPSHOT_REQUIRED_COLUMNS = ("行业代码", "行业名称", "上级行业")
CLASSIFICATION_HISTORY_REQUIRED_COLUMNS = (
    "industry_code",
    "industry_name",
    "industry_level",
    "parent_industry",
    "effective_from",
    "effective_to",
    "published_at",
    "available_date",
    "fetched_at",
    "source",
    "source_version",
    "source_hash",
    "source_hash_basis",
    "source_artifact_path",
    "revision_status",
    "availability_basis",
    "data_status",
)
UNSAFE_HISTORY_LABEL_COLUMNS = {
    "industry_name",
    "parent_industry",
    "行业名称",
    "上级行业",
}
LABEL_PROVENANCE_COLUMNS = {
    "classification_available_date",
    "classification_source_version",
}

# The local source re-used these numeric identifiers after a multi-year gap.
# These are observed source episodes, not a claim of complete official SW
# classification history.  The hard boundary prevents one return window from
# treating two different identities as a continuous price series.
KNOWN_IDENTITY_EPISODES: dict[str, tuple[dict[str, str], ...]] = {
    "801951": (
        {
            "episode_id": "801951:legacy_imp_computer",
            "observed_name": "Imp_计算机",
            "start_date": "2014-02-21",
            "end_date": "2017-01-20",
        },
        {
            "episode_id": "801951:sw2021_coal_mining",
            "observed_name": "煤炭开采",
            "start_date": "2021-12-13",
            "end_date": "9999-12-31",
        },
    ),
    "801952": (
        {
            "episode_id": "801952:legacy_imp_media",
            "observed_name": "Imp_传媒",
            "start_date": "2014-02-21",
            "end_date": "2017-01-20",
        },
        {
            "episode_id": "801952:sw2021_coke_second",
            "observed_name": "焦炭Ⅱ",
            "start_date": "2021-12-13",
            "end_date": "9999-12-31",
        },
    ),
}


@dataclass
class HistoryFileAudit:
    file_name: str
    industry_code: str
    row_count: int = 0
    required_schema_ok: bool = False
    code_format_ok: bool = False
    code_column_matches_file: bool = False
    current_snapshot_member: bool = False
    valid_trade_date_count: int = 0
    invalid_trade_date_count: int = 0
    duplicate_trade_date_count: int = 0
    sorted_trade_dates: bool = False
    future_trade_date_count: int = 0
    earliest_trade_date: str = ""
    latest_trade_date: str = ""
    latest_age_calendar_days: int | str = ""
    tail_gap_status: str = "unknown"
    fresh_at_as_of: bool = False
    identity_reuse_guard_required: bool = False
    identity_episode_count: int = 0
    identity_episode_ids: str = ""
    identity_unassigned_date_count: int = 0
    cross_episode_boundary_count: int = 0
    identity_reuse_guard_status: str = "not_required"
    available_date_column_present: bool = False
    available_date_complete: bool = False
    available_date_order_violation_count: int = 0
    pit_required_columns_present: bool = False
    pit_key_fields_complete: bool = False
    frozen_calendar_verified: bool = False
    trade_dates_on_frozen_calendar: bool = False
    available_dates_on_frozen_calendar: bool = False
    available_date_rule_violation_count: int = 0
    source_provenance_complete: bool = False
    source_artifact_hash_verified: bool = False
    historical_pit_chain_complete: bool = False
    unverified_label_columns: str = ""
    current_monitoring_file_valid: bool = False
    historical_promotion_file_eligible: bool = False
    issues: str = ""
    _valid_dates: list[date] = field(default_factory=list, repr=False)

    def output_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("_valid_dates", None)
        return payload


def parse_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_aware_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(SHANGHAI)


def _finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def audit_frozen_trading_calendar(path: Path = DEFAULT_TRADING_CALENDAR) -> tuple[dict[str, Any], tuple[date, ...]]:
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "expected_sha256": FROZEN_TRADING_CALENDAR_SHA256,
        "actual_sha256": "",
        "verified": False,
        "row_count": 0,
        "issues": [],
    }
    if not path.exists():
        result["issues"] = ["frozen_trading_calendar_missing"]
        return result, ()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result["actual_sha256"] = digest
    if digest != FROZEN_TRADING_CALENDAR_SHA256:
        result["issues"] = ["frozen_trading_calendar_hash_mismatch"]
        return result, ()
    try:
        columns, rows = read_csv(path)
    except (OSError, UnicodeError, csv.Error) as exc:
        result["issues"] = [f"frozen_trading_calendar_unreadable:{type(exc).__name__}"]
        return result, ()
    dates = tuple(parse_iso_date(row.get("trade_date")) for row in rows)
    issues: list[str] = []
    if columns != ["trade_date"]:
        issues.append("frozen_trading_calendar_schema_invalid")
    if any(value is None for value in dates):
        issues.append("frozen_trading_calendar_date_invalid")
    valid_dates = tuple(value for value in dates if value is not None)
    if not valid_dates or valid_dates != tuple(sorted(set(valid_dates))):
        issues.append("frozen_trading_calendar_not_unique_sorted")
    result["row_count"] = len(valid_dates)
    result["issues"] = issues
    result["verified"] = not issues
    return result, valid_dates if result["verified"] else ()


def first_eligible_trade_date(published_at: datetime, trading_dates: Sequence[date]) -> date | None:
    published = published_at.astimezone(SHANGHAI)
    published_date = published.date()
    trading_set = set(trading_dates)
    if published_date in trading_set and published.time().replace(tzinfo=None) < DAILY_DECISION_CUTOFF:
        return published_date
    index = bisect_right(trading_dates, published_date)
    return trading_dates[index] if index < len(trading_dates) else None


def source_artifact_matches(
    row: dict[str, str],
    *,
    artifact_root: Path,
    hash_cache: dict[str, str] | None = None,
) -> bool:
    source_hash = str(row.get("source_hash", "")).strip().lower()
    relative = Path(str(row.get("source_artifact_path", "")).strip())
    if not SHA256_PATTERN.fullmatch(source_hash) or not str(relative) or relative.is_absolute():
        return False
    root = artifact_root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return False
    key = resolved.as_posix()
    cache = hash_cache if hash_cache is not None else {}
    if key not in cache:
        cache[key] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return cache[key] == source_hash


def pit_row_valid(
    row: dict[str, str],
    *,
    trade_date: date | None,
    available_date: date | None,
    trading_dates: Sequence[date],
    artifact_root: Path,
    trading_set: set[date] | None = None,
    artifact_hash_cache: dict[str, str] | None = None,
) -> tuple[bool, bool, bool]:
    """Return (time/calendar valid, provenance valid, artifact hash valid)."""
    published = parse_aware_datetime(row.get("published_at"))
    fetched = parse_aware_datetime(row.get("fetched_at"))
    expected_available = first_eligible_trade_date(published, trading_dates) if published else None
    sessions = trading_set if trading_set is not None else set(trading_dates)
    time_valid = bool(
        trade_date
        and available_date
        and trade_date in sessions
        and available_date in sessions
        and published
        and fetched
        and published.date() >= trade_date
        and fetched >= published
        and available_date == expected_available
    )
    source_hash = str(row.get("source_hash", "")).strip().lower()
    source = str(row.get("source", "")).strip().lower()
    provenance_valid = bool(
        source not in {"", "unknown", "unverified", "synthetic"}
        and SHA256_PATTERN.fullmatch(source_hash)
        and str(row.get("source_version", "")).strip() == f"sha256:{source_hash}"
        and str(row.get("source_hash_basis", "")).strip() == SOURCE_HASH_BASIS
        and str(row.get("revision_status", "")).strip() in ACTIVE_REVISION_STATUSES
        and str(row.get("availability_basis", "")).strip() in VERIFIED_AVAILABILITY_BASES
        and str(row.get("data_status", "")).strip() == VERIFIED_DATA_STATUS
    )
    artifact_valid = source_artifact_matches(
        row,
        artifact_root=artifact_root,
        hash_cache=artifact_hash_cache,
    )
    return time_valid, provenance_valid, artifact_valid


def normalize_industry_code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text


def identity_episode_for_date(industry_code: str, trade_date: date) -> str | None:
    """Return an observed identity episode; never bridge a known reuse gap."""
    code = normalize_industry_code(industry_code)
    episodes = KNOWN_IDENTITY_EPISODES.get(code)
    if not episodes:
        return f"{code}:observed_series"
    for episode in episodes:
        start = date.fromisoformat(episode["start_date"])
        end = date.fromisoformat(episode["end_date"])
        if start <= trade_date <= end:
            return episode["episode_id"]
    return None


def episode_safe_return(
    industry_code: str,
    trade_dates: Sequence[date],
    values: Sequence[float],
    start_index: int,
    end_index: int,
) -> float | None:
    """Calculate a return only when both endpoints belong to one identity episode."""
    if len(trade_dates) != len(values) or not (0 <= start_index < end_index < len(values)):
        return None
    window_dates = trade_dates[start_index : end_index + 1]
    if any(left >= right for left, right in zip(window_dates, window_dates[1:])):
        return None
    window_episodes = {identity_episode_for_date(industry_code, value) for value in window_dates}
    if None in window_episodes or len(window_episodes) != 1:
        return None
    start_value = float(values[start_index])
    end_value = float(values[end_index])
    if start_value == 0:
        return None
    return end_value / start_value - 1.0


def first_present(columns: Iterable[str], candidates: Sequence[str]) -> str | None:
    available = set(columns)
    return next((candidate for candidate in candidates if candidate in available), None)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def latest_dated_snapshot(snapshot_dir: Path, as_of: date) -> tuple[Path | None, date | None]:
    choices: list[tuple[date, Path]] = []
    if snapshot_dir.exists():
        for path in snapshot_dir.glob("*.csv"):
            snapshot_date = parse_iso_date(path.stem)
            if snapshot_date and snapshot_date <= as_of:
                choices.append((snapshot_date, path))
    if not choices:
        return None, None
    snapshot_date, path = max(choices, key=lambda item: (item[0], item[1].name))
    return path, snapshot_date


def audit_current_snapshot(path: Path | None, snapshot_date: date | None) -> dict[str, Any]:
    base = {
        "path": str(path.resolve()) if path else "",
        "snapshot_date": snapshot_date.isoformat() if snapshot_date else "",
        "status": "fail",
        "current_only": True,
        "historical_use_allowed": False,
        "row_count": 0,
        "unique_code_count": 0,
        "duplicate_code_count": 0,
        "invalid_code_count": 0,
        "blank_identity_count": 0,
        "codes": [],
        "issues": [],
    }
    if path is None or not path.exists():
        base["issues"] = ["current_snapshot_missing"]
        return base
    try:
        columns, rows = read_csv(path)
    except (OSError, UnicodeError, csv.Error) as exc:
        base["issues"] = [f"current_snapshot_unreadable:{type(exc).__name__}"]
        return base
    missing = [column for column in CURRENT_SNAPSHOT_REQUIRED_COLUMNS if column not in columns]
    codes = [normalize_industry_code(row.get("行业代码")) for row in rows]
    unique_codes = {code for code in codes if code}
    base.update(
        {
            "row_count": len(rows),
            "unique_code_count": len(unique_codes),
            "duplicate_code_count": len(codes) - len(unique_codes),
            "invalid_code_count": sum(not SW_SECOND_CODE_PATTERN.fullmatch(code or "") for code in codes),
            "blank_identity_count": sum(
                not str(row.get("行业名称", "")).strip() or not str(row.get("上级行业", "")).strip()
                for row in rows
            ),
            "codes": sorted(unique_codes),
        }
    )
    issues = []
    if missing:
        issues.append("missing_columns:" + ",".join(missing))
    for key in ("duplicate_code_count", "invalid_code_count", "blank_identity_count"):
        if base[key]:
            issues.append(f"{key}:{base[key]}")
    base["issues"] = issues
    base["status"] = "pass" if not issues else "fail"
    return base


def audit_history_file(
    path: Path,
    *,
    expected_codes: set[str],
    as_of: date,
    max_stale_days: int,
    trading_calendar: Sequence[date] | None = None,
    frozen_calendar_verified: bool | None = None,
    artifact_root: Path = ROOT,
    artifact_hash_cache: dict[str, str] | None = None,
) -> HistoryFileAudit:
    code = normalize_industry_code(path.stem)
    audit = HistoryFileAudit(file_name=path.name, industry_code=code)
    issues: list[str] = []
    try:
        columns, rows = read_csv(path)
    except (OSError, UnicodeError, csv.Error) as exc:
        audit.issues = f"unreadable:{type(exc).__name__}"
        return audit

    audit.row_count = len(rows)
    code_column = first_present(columns, HISTORY_CODE_COLUMNS)
    date_column = first_present(columns, HISTORY_DATE_COLUMNS)
    close_column = first_present(columns, HISTORY_CLOSE_COLUMNS)
    audit.required_schema_ok = all((code_column, date_column, close_column))
    audit.code_format_ok = bool(SW_SECOND_CODE_PATTERN.fullmatch(code))
    audit.current_snapshot_member = code in expected_codes
    if not audit.required_schema_ok:
        issues.append("missing_required_history_columns")

    row_codes = [normalize_industry_code(row.get(code_column)) for row in rows] if code_column else []
    audit.code_column_matches_file = bool(rows) and set(row_codes) == {code}
    if not audit.code_column_matches_file:
        issues.append("row_code_does_not_match_filename")

    parsed_dates = [parse_iso_date(row.get(date_column)) for row in rows] if date_column else []
    valid_dates = [value for value in parsed_dates if value]
    audit._valid_dates = valid_dates
    audit.valid_trade_date_count = len(valid_dates)
    audit.invalid_trade_date_count = len(parsed_dates) - len(valid_dates)
    audit.duplicate_trade_date_count = len(valid_dates) - len(set(valid_dates))
    audit.sorted_trade_dates = bool(valid_dates) and valid_dates == sorted(valid_dates)
    audit.future_trade_date_count = sum(value > as_of for value in valid_dates)
    if valid_dates:
        earliest, latest = min(valid_dates), max(valid_dates)
        audit.earliest_trade_date = earliest.isoformat()
        audit.latest_trade_date = latest.isoformat()
        audit.latest_age_calendar_days = (as_of - latest).days
        audit.fresh_at_as_of = 0 <= (as_of - latest).days <= max_stale_days
        audit.tail_gap_status = (
            "fresh"
            if audit.fresh_at_as_of
            else "long_tail_gap"
            if (as_of - latest).days > 365
            else "stale"
        )
    for key, count in (
        ("invalid_trade_date", audit.invalid_trade_date_count),
        ("duplicate_trade_date", audit.duplicate_trade_date_count),
        ("future_trade_date", audit.future_trade_date_count),
    ):
        if count:
            issues.append(f"{key}:{count}")
    if not audit.sorted_trade_dates:
        issues.append("trade_dates_not_strictly_sorted")
    if not audit.current_snapshot_member:
        issues.append("not_in_current_second_level_snapshot")
    if not audit.code_format_ok:
        issues.append("invalid_sw_second_code_format")

    if trading_calendar is None:
        calendar_audit, loaded_calendar = audit_frozen_trading_calendar()
        trading_calendar = loaded_calendar
        frozen_calendar_verified = bool(calendar_audit["verified"])
    audit.frozen_calendar_verified = bool(frozen_calendar_verified and trading_calendar)
    trading_set = set(trading_calendar or ())
    audit.trade_dates_on_frozen_calendar = bool(valid_dates) and all(value in trading_set for value in valid_dates)
    if not audit.frozen_calendar_verified:
        issues.append("frozen_trading_calendar_not_verified")
    elif not audit.trade_dates_on_frozen_calendar:
        issues.append("trade_date_not_on_frozen_trading_calendar")

    audit.identity_reuse_guard_required = code in KNOWN_IDENTITY_EPISODES
    episode_assignments = [identity_episode_for_date(code, value) for value in valid_dates]
    episode_ids = {value for value in episode_assignments if value}
    audit.identity_episode_count = len(episode_ids)
    audit.identity_episode_ids = ";".join(sorted(episode_ids))
    audit.identity_unassigned_date_count = sum(value is None for value in episode_assignments)
    audit.cross_episode_boundary_count = sum(
        left is not None and right is not None and left != right
        for left, right in zip(episode_assignments, episode_assignments[1:])
    )
    if audit.identity_reuse_guard_required:
        audit.identity_reuse_guard_status = (
            "segmented"
            if audit.identity_unassigned_date_count == 0 and audit.identity_episode_count >= 1
            else "fail"
        )
        if audit.identity_reuse_guard_status != "segmented":
            issues.append("identity_episode_assignment_failed")

    audit.available_date_column_present = "available_date" in columns
    available_dates: list[date | None] = []
    if audit.available_date_column_present:
        available_dates = [parse_iso_date(row.get("available_date")) for row in rows]
        audit.available_date_complete = bool(rows) and all(available_dates)
        audit.available_date_order_violation_count = sum(
            available is not None and trade is not None and available < trade
            for available, trade in zip(available_dates, parsed_dates)
        )
        if not audit.available_date_complete:
            issues.append("available_date_incomplete")
        if audit.available_date_order_violation_count:
            issues.append(f"available_date_before_trade_date:{audit.available_date_order_violation_count}")
    else:
        issues.append("available_date_missing_for_historical_promotion")

    missing_pit_columns = [column for column in HISTORY_PIT_REQUIRED_COLUMNS if column not in columns]
    audit.pit_required_columns_present = not missing_pit_columns
    if missing_pit_columns:
        issues.append("missing_historical_pit_columns:" + ",".join(missing_pit_columns))
    audit.available_dates_on_frozen_calendar = bool(available_dates) and all(
        value is not None and value in trading_set for value in available_dates
    )
    close_values_valid = bool(rows) and close_column is not None and all(
        str(row.get(close_column, "")).strip()
        and _finite_number(row.get(close_column))
        for row in rows
    )
    audit.pit_key_fields_complete = bool(
        rows
        and audit.pit_required_columns_present
        and close_values_valid
        and all(
            all(str(row.get(column, "")).strip() for column in HISTORY_PIT_REQUIRED_COLUMNS)
            for row in rows
        )
    )
    if audit.pit_required_columns_present and audit.frozen_calendar_verified:
        pit_results = [
            pit_row_valid(
                row,
                trade_date=trade_date,
                available_date=available_date,
                trading_dates=trading_calendar or (),
                artifact_root=artifact_root,
                trading_set=trading_set,
                artifact_hash_cache=artifact_hash_cache,
            )
            for row, trade_date, available_date in zip(rows, parsed_dates, available_dates, strict=True)
        ]
        audit.available_date_rule_violation_count = sum(not result[0] for result in pit_results)
        audit.source_provenance_complete = bool(pit_results) and all(result[1] for result in pit_results)
        audit.source_artifact_hash_verified = bool(pit_results) and all(result[2] for result in pit_results)
    if not audit.available_dates_on_frozen_calendar:
        issues.append("available_date_not_on_frozen_trading_calendar")
    if audit.available_date_rule_violation_count:
        issues.append(f"published_available_calendar_rule_invalid:{audit.available_date_rule_violation_count}")
    if not audit.pit_key_fields_complete:
        issues.append("historical_pit_key_fields_incomplete")
    if not audit.source_provenance_complete:
        issues.append("historical_source_provenance_unverified")
    if not audit.source_artifact_hash_verified:
        issues.append("historical_source_artifact_hash_unverified")
    audit.historical_pit_chain_complete = all(
        (
            audit.pit_required_columns_present,
            audit.pit_key_fields_complete,
            audit.frozen_calendar_verified,
            audit.trade_dates_on_frozen_calendar,
            audit.available_dates_on_frozen_calendar,
            audit.available_date_rule_violation_count == 0,
            audit.source_provenance_complete,
            audit.source_artifact_hash_verified,
        )
    )

    unsafe_labels = sorted(UNSAFE_HISTORY_LABEL_COLUMNS.intersection(columns))
    if unsafe_labels and not LABEL_PROVENANCE_COLUMNS.issubset(columns):
        audit.unverified_label_columns = ";".join(unsafe_labels)
        issues.append("current_or_unversioned_labels_in_history")

    audit.current_monitoring_file_valid = all(
        (
            audit.required_schema_ok,
            audit.code_format_ok,
            audit.code_column_matches_file,
            audit.current_snapshot_member,
            audit.valid_trade_date_count > 0,
            audit.invalid_trade_date_count == 0,
            audit.duplicate_trade_date_count == 0,
            audit.sorted_trade_dates,
            audit.future_trade_date_count == 0,
        )
    )
    audit.historical_promotion_file_eligible = all(
        (
            audit.current_monitoring_file_valid,
            audit.available_date_column_present,
            audit.available_date_complete,
            audit.available_date_order_violation_count == 0,
            audit.historical_pit_chain_complete,
            not audit.unverified_label_columns,
            audit.identity_reuse_guard_status != "fail",
        )
    )
    audit.issues = ";".join(issues)
    return audit


def audit_classification_history(
    path: Path,
    *,
    expected_codes: set[str] | None = None,
    trading_calendar: Sequence[date] | None = None,
    frozen_calendar_verified: bool | None = None,
    artifact_root: Path = ROOT,
    artifact_hash_cache: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = {
        "path": str(path.resolve()),
        "status": "unavailable_for_promotion",
        "verified": False,
        "pit_chain_verified": False,
        "governed_code_coverage_verified": False,
        "row_count": 0,
        "unique_code_count": 0,
        "expected_code_count": len(expected_codes or ()),
        "missing_governed_codes": [],
        "unexpected_codes": [],
        "issues": [],
    }
    if not path.exists():
        result["issues"] = [
            "classification_history_missing",
            "current_snapshot_must_not_be_used_as_historical_classification",
        ]
        return result
    try:
        columns, rows = read_csv(path)
    except (OSError, UnicodeError, csv.Error) as exc:
        result["issues"] = [f"classification_history_unreadable:{type(exc).__name__}"]
        return result
    result["row_count"] = len(rows)
    missing = [column for column in CLASSIFICATION_HISTORY_REQUIRED_COLUMNS if column not in columns]
    issues: list[str] = []
    if missing:
        issues.append("missing_columns:" + ",".join(missing))
    if trading_calendar is None:
        calendar_audit, loaded_calendar = audit_frozen_trading_calendar()
        trading_calendar = loaded_calendar
        frozen_calendar_verified = bool(calendar_audit["verified"])
    if not frozen_calendar_verified or not trading_calendar:
        issues.append("frozen_trading_calendar_not_verified")
    trading_set = set(trading_calendar or ())
    parsed: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    codes: set[str] = set()
    pit_row_results: list[bool] = []
    for row_number, row in enumerate(rows, start=2):
        code = normalize_industry_code(row.get("industry_code"))
        codes.add(code)
        start = parse_iso_date(row.get("effective_from"))
        end = parse_iso_date(row.get("effective_to")) if str(row.get("effective_to", "")).strip() else None
        available = parse_iso_date(row.get("available_date"))
        if not SW_SECOND_CODE_PATTERN.fullmatch(code or ""):
            issues.append(f"row_{row_number}:invalid_code")
        if str(row.get("industry_level", "")).strip() != "second":
            issues.append(f"row_{row_number}:wrong_industry_level")
        if not start or not available:
            issues.append(f"row_{row_number}:invalid_effective_or_available_date")
        if any(
            not str(row.get(column, "")).strip()
            for column in CLASSIFICATION_HISTORY_REQUIRED_COLUMNS
            if column != "effective_to"
        ):
            issues.append(f"row_{row_number}:blank_required_classification_field")
        if start and end and end < start:
            issues.append(f"row_{row_number}:effective_to_before_effective_from")
        time_valid, provenance_valid, artifact_valid = pit_row_valid(
            row,
            trade_date=start,
            available_date=available,
            trading_dates=trading_calendar or (),
            artifact_root=artifact_root,
            trading_set=trading_set,
            artifact_hash_cache=artifact_hash_cache,
        )
        pit_row_results.append(time_valid and provenance_valid and artifact_valid)
        if not time_valid:
            issues.append(f"row_{row_number}:classification_publication_or_calendar_rule_invalid")
        if not provenance_valid:
            issues.append(f"row_{row_number}:classification_source_provenance_unverified")
        if not artifact_valid:
            issues.append(f"row_{row_number}:classification_source_artifact_hash_unverified")
        if start:
            parsed[code].append((start, end))
    for code, intervals in parsed.items():
        ordered = sorted(intervals, key=lambda item: item[0])
        for previous, current in zip(ordered, ordered[1:]):
            previous_end = previous[1]
            if previous_end is None or previous_end >= current[0]:
                issues.append(f"{code}:overlapping_effective_intervals")
                break
    governed_codes = {code for code in codes if code}
    expected = governed_codes if expected_codes is None else set(expected_codes)
    missing_codes = sorted(expected - governed_codes)
    unexpected_codes = sorted(governed_codes - expected)
    result["unique_code_count"] = len(governed_codes)
    result["missing_governed_codes"] = missing_codes
    result["unexpected_codes"] = unexpected_codes
    result["governed_code_coverage_verified"] = bool(expected) and not missing_codes and not unexpected_codes
    if not result["governed_code_coverage_verified"]:
        issues.append("historical_classification_does_not_exactly_cover_governed_codes")
    result["pit_chain_verified"] = bool(rows) and bool(frozen_calendar_verified) and all(pit_row_results)
    if not result["pit_chain_verified"]:
        issues.append("historical_classification_pit_chain_unverified")
    result["issues"] = sorted(set(issues))
    result["verified"] = bool(rows) and not issues and result["pit_chain_verified"] and result["governed_code_coverage_verified"]
    result["status"] = "verified" if result["verified"] else "unavailable_for_promotion"
    return result


def build_universe_period_audit(audits: Sequence[HistoryFileAudit], as_of: date) -> list[dict[str, Any]]:
    valid = {row.industry_code: sorted(set(row._valid_dates)) for row in audits if row.current_monitoring_file_valid}
    if not valid:
        return []
    all_dates = sorted({value for dates in valid.values() for value in dates if value <= as_of})
    if not all_dates:
        return []
    first_year = min(value.year for value in all_dates)
    rows: list[dict[str, Any]] = []
    previously_started: set[str] = set()
    for year in range(first_year, as_of.year + 1):
        cutoff = as_of if year == as_of.year else date(year, 12, 31)
        year_dates = [value for value in all_dates if value.year == year and value <= cutoff]
        if not year_dates:
            continue
        reference_date = max(year_dates)
        observed_in_year = {
            code for code, dates in valid.items() if any(value.year == year and value <= cutoff for value in dates)
        }
        observed_on_reference = {code for code, dates in valid.items() if reference_date in dates}
        started_by_reference = {code for code, dates in valid.items() if dates and dates[0] <= reference_date}
        first_observed = started_by_reference - previously_started
        previously_started = started_by_reference
        not_observed_on_reference = set(valid) - observed_on_reference
        rows.append(
            {
                "period": str(year),
                "reference_trade_date": reference_date.isoformat(),
                "observed_in_period_count": len(observed_in_year),
                "observed_on_reference_date_count": len(observed_on_reference),
                "started_by_reference_count": len(started_by_reference),
                "first_observed_code_count": len(first_observed),
                "first_observed_codes": ";".join(sorted(first_observed)),
                "not_observed_on_reference_count": len(not_observed_on_reference),
                "not_observed_on_reference_codes": ";".join(sorted(not_observed_on_reference)),
                "official_entry_exit_status": "unverified_without_classification_history",
                "benchmark_comparability": "not_comparable_for_promotion_current_code_survivorship",
            }
        )
    return rows


def run_audit(
    *,
    history_dir: Path,
    snapshot_dir: Path,
    classification_history_path: Path,
    as_of: date,
    required_industry_count: int = REQUIRED_INDUSTRY_COUNT,
    minimum_fresh_industry_count: int = MINIMUM_FRESH_INDUSTRY_COUNT,
    max_stale_days: int = MAX_STALE_CALENDAR_DAYS,
    trading_calendar_path: Path = DEFAULT_TRADING_CALENDAR,
    artifact_root: Path = ROOT,
) -> tuple[dict[str, Any], list[HistoryFileAudit], list[dict[str, Any]]]:
    snapshot_path, snapshot_date = latest_dated_snapshot(snapshot_dir, as_of)
    snapshot = audit_current_snapshot(snapshot_path, snapshot_date)
    expected_codes = set(snapshot["codes"])
    calendar_audit, trading_calendar = audit_frozen_trading_calendar(trading_calendar_path)
    artifact_hash_cache: dict[str, str] = {}
    files = sorted(history_dir.glob("*.csv")) if history_dir.exists() else []
    audits = [
        audit_history_file(
            path,
            expected_codes=expected_codes,
            as_of=as_of,
            max_stale_days=max_stale_days,
            trading_calendar=trading_calendar,
            frozen_calendar_verified=bool(calendar_audit["verified"]),
            artifact_root=artifact_root,
            artifact_hash_cache=artifact_hash_cache,
        )
        for path in files
    ]
    valid_codes = {row.industry_code for row in audits if row.current_monitoring_file_valid}
    fresh_codes = {row.industry_code for row in audits if row.current_monitoring_file_valid and row.fresh_at_as_of}
    classification = audit_classification_history(
        classification_history_path,
        expected_codes=expected_codes,
        trading_calendar=trading_calendar,
        frozen_calendar_verified=bool(calendar_audit["verified"]),
        artifact_root=artifact_root,
        artifact_hash_cache=artifact_hash_cache,
    )
    period_rows = build_universe_period_audit(audits, as_of)

    snapshot_exact = snapshot["status"] == "pass" and snapshot["unique_code_count"] == required_industry_count
    history_exact = len(valid_codes) == required_industry_count and valid_codes == expected_codes
    coverage_passed = bool(snapshot_exact and history_exact)
    freshness_passed = len(fresh_codes) >= minimum_fresh_industry_count
    available_date_eligible_files = sum(row.historical_promotion_file_eligible for row in audits)
    raw_label_backfill_detected = any(bool(row.unverified_label_columns) for row in audits)
    identity_reuse_rows = [row for row in audits if row.identity_reuse_guard_required]
    identity_reuse_guard_passed = bool(identity_reuse_rows) and all(
        row.identity_reuse_guard_status == "segmented" for row in identity_reuse_rows
    )
    long_tail_gap_codes = sorted(row.industry_code for row in audits if row.tail_gap_status == "long_tail_gap")
    promotion_eligible = bool(
        coverage_passed
        and calendar_audit["verified"]
        and classification["verified"]
        and available_date_eligible_files == required_industry_count
        and not raw_label_backfill_detected
    )
    blocked_reasons: list[str] = []
    if not coverage_passed:
        blocked_reasons.append("governed_131_file_universe_not_proven")
    if not calendar_audit["verified"]:
        blocked_reasons.append("frozen_trading_calendar_not_verified")
    if not classification["verified"]:
        blocked_reasons.append("historical_classification_and_effective_dates_unavailable")
    if not classification["governed_code_coverage_verified"]:
        blocked_reasons.append("historical_classification_governed_code_coverage_unproven")
    if not classification["pit_chain_verified"]:
        blocked_reasons.append("historical_classification_pit_chain_unproven")
    if available_date_eligible_files != required_industry_count:
        blocked_reasons.append("row_level_available_date_not_proven_for_all_history_files")
    if raw_label_backfill_detected:
        blocked_reasons.append("unversioned_current_labels_detected_in_history")

    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "policy": {
            "industry_level": "SW_second",
            "required_industry_count": required_industry_count,
            "minimum_fresh_industry_count": minimum_fresh_industry_count,
            "max_stale_calendar_days": max_stale_days,
            "thresholds_are_independent": True,
        },
        "current_snapshot": snapshot,
        "frozen_trading_calendar": calendar_audit,
        "history_file_count": len(files),
        "valid_current_history_file_count": len(valid_codes),
        "fresh_history_file_count": len(fresh_codes),
        "stale_or_invalid_history_file_count": len(files) - len(fresh_codes),
        "coverage_gate_passed": coverage_passed,
        "freshness_gate_passed": freshness_passed,
        "current_monitoring_gate_passed": coverage_passed and freshness_passed,
        "history_files_with_complete_available_date": available_date_eligible_files,
        "history_files_with_complete_pit_chain": available_date_eligible_files,
        "classification_history": classification,
        "raw_history_current_label_backfill_detected": raw_label_backfill_detected,
        "identity_reuse_codes": sorted(row.industry_code for row in identity_reuse_rows),
        "identity_episode_policy": KNOWN_IDENTITY_EPISODES,
        "identity_episode_evidence_status": "observed_local_source_names_not_official_classification_history",
        "identity_reuse_guard_passed": identity_reuse_guard_passed,
        "cross_identity_episode_return_allowed": False,
        "long_tail_gap_history_file_count": len(long_tail_gap_codes),
        "long_tail_gap_history_codes": long_tail_gap_codes,
        "current_snapshot_historical_use_allowed": False,
        "historical_universe_basis": "current_snapshot_code_set_only",
        "historical_universe_status": "unverified_current_code_survivorship",
        "historical_promotion_eligible": promotion_eligible,
        "historical_promotion_status": "eligible" if promotion_eligible else "blocked",
        "blocked_reasons": blocked_reasons,
        "evidence_boundary": (
            "131/120 gates may support current monitoring only. Historical price results remain descriptive until "
            "row-level availability and dated SW classification history are independently verified."
        ),
    }
    return summary, audits, period_rows


def write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def render_report(summary: dict[str, Any], period_rows: Sequence[dict[str, Any]]) -> str:
    current_snapshot = summary["current_snapshot"]
    lines = [
        "# 申万行业历史口径审计",
        "",
        f"- 审计日期：`{summary['as_of_date']}`",
        f"- 当前快照：`{current_snapshot.get('snapshot_date') or '缺失'}`，仅作当前 131 行业身份参照",
        f"- 历史文件：{summary['valid_current_history_file_count']}/{summary['policy']['required_industry_count']} 通过身份与日期完整性校验",
        f"- 新鲜文件：{summary['fresh_history_file_count']}/{summary['policy']['minimum_fresh_industry_count']} 达到独立新鲜度门槛",
        f"- 当前监控门禁：{'通过' if summary['current_monitoring_gate_passed'] else '失败关闭'}",
        f"- 历史晋级门禁：{'可用' if summary['historical_promotion_eligible'] else '阻断'}",
        "",
        "## 口径结论",
        "",
        "131 是当前申万二级价格文件覆盖门槛，120 是逐文件新鲜度门槛；二者不得互相替代。",
        "当前行业快照只能回答当前代码、名称和上级行业，不能证明历史时点的分类身份。现有价格文件没有逐行 `available_date`，也没有带生效区间和源版本的行业分类历史，因此历史结果只能作为描述性复核，不能参与晋级。",
        "`801951` 和 `801952` 在 2017-01-20 后中断，到 2021-12-13 以不同语义复用代码；审计已切成独立 identity episode，任何跨 episode 的滚动或前向收益均不可用。",
        f"截至审计日，超过一年未更新的长尾缺口文件共 {summary['long_tail_gap_history_file_count']} 个：`{';'.join(summary['long_tail_gap_history_codes']) or '无'}`。这些文件仍计入 131 覆盖核对，但不能计入 120 新鲜文件。",
        "",
        "## 历史横截面断点",
        "",
        "| 年度 | 参考交易日 | 当年有记录 | 参考日有记录 | 截至参考日已出现 | 首次出现 | 参考日缺口 |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in period_rows:
        lines.append(
            f"| {row['period']} | {row['reference_trade_date']} | {row['observed_in_period_count']} | "
            f"{row['observed_on_reference_date_count']} | {row['started_by_reference_count']} | "
            f"{row['first_observed_code_count']} | {row['not_observed_on_reference_count']} |"
        )
    lines.extend(
        [
            "",
            "以上“首次出现”和“缺口”只是价格文件的观察事实，不能冒充官方行业进入、退出或分类调整。没有历史分类表时，等权基准跨断点不可直接比较。",
            "",
            "## 阻断原因与恢复条件",
            "",
        ]
    )
    lines.extend(f"- `{reason}`" for reason in summary["blocked_reasons"])
    lines.extend(
        [
            "",
            "恢复历史晋级资格需同时补齐：逐行可验证的 `available_date`；申万二级行业分类的生效区间、可得日期、源版本与修订状态；按历史有效宇宙重建基准。当前快照不得用于回填。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(
    output_dir: Path,
    summary: dict[str, Any],
    audits: Sequence[HistoryFileAudit],
    period_rows: Sequence[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    file_rows = [row.output_dict() for row in audits]
    file_fields = list(file_rows[0]) if file_rows else ["file_name", "industry_code", "issues"]
    write_csv(output_dir / "file_audit.csv", file_rows, file_fields)
    period_fields = list(period_rows[0]) if period_rows else ["period", "reference_trade_date"]
    write_csv(output_dir / "universe_period_audit.csv", list(period_rows), period_fields)
    (output_dir / "report.md").write_text(render_report(summary, period_rows), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit SW second-level industry history methodology.")
    parser.add_argument("--as-of-date", type=parse_required_date, default=date.today().isoformat())
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--classification-history", type=Path, default=DEFAULT_CLASSIFICATION_HISTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--required-industry-count", type=int, default=REQUIRED_INDUSTRY_COUNT)
    parser.add_argument("--minimum-fresh-industry-count", type=int, default=MINIMUM_FRESH_INDUSTRY_COUNT)
    parser.add_argument("--max-stale-calendar-days", type=int, default=MAX_STALE_CALENDAR_DAYS)
    parser.add_argument(
        "--require-promotion-ready",
        action="store_true",
        help="Exit non-zero unless the historical-promotion gate is proven ready.",
    )
    return parser


def parse_required_date(value: str) -> date:
    parsed = parse_iso_date(value)
    if parsed is None or parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD")
    return parsed


def main() -> int:
    args = build_parser().parse_args()
    summary, audits, period_rows = run_audit(
        history_dir=args.history_dir,
        snapshot_dir=args.snapshot_dir,
        classification_history_path=args.classification_history,
        as_of=args.as_of_date,
        required_industry_count=args.required_industry_count,
        minimum_fresh_industry_count=args.minimum_fresh_industry_count,
        max_stale_days=args.max_stale_calendar_days,
    )
    write_outputs(args.output, summary, audits, period_rows)
    print(
        "industry_history_audit="
        f"coverage={summary['valid_current_history_file_count']}/{args.required_industry_count};"
        f"fresh={summary['fresh_history_file_count']}/{args.minimum_fresh_industry_count};"
        f"current_gate={'pass' if summary['current_monitoring_gate_passed'] else 'fail'};"
        f"historical_promotion={summary['historical_promotion_status']}"
    )
    if args.require_promotion_ready and not summary["historical_promotion_eligible"]:
        return 1
    return 0 if summary["current_monitoring_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
