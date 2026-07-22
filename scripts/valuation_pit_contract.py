#!/usr/bin/env python
"""Strict point-in-time contract for valuation data.

The repository contains useful SWS daily valuation history, but its public API
payload does not expose when each row was published.  A trade date is therefore
not evidence of availability.  This module centralises the fail-closed rule so
callers cannot silently manufacture ``available_date`` from ``trade_date``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd


SHANGHAI = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRADING_CALENDAR = ROOT / "data_catalog" / "cache" / "trading_calendar" / "a_share_trade_calendar.csv"
FROZEN_TRADING_CALENDAR_SHA256 = "f348dd4c8863a5f2a5ff543a427c36198dbe3ca00f01a268f736490b2989d975"
DAILY_DECISION_CUTOFF = time(15, 0)
VERIFIED_STATUS = "pit_verified"
CURRENT_SNAPSHOT_STATUS = "current_snapshot_observed_only"
NON_PIT_HISTORY_STATUS = "non_pit_publication_time_missing"
VERIFIED_AVAILABILITY_BASES = {
    "source_publication_timestamp",
    "vendor_available_timestamp",
}
PIT_REQUIRED_COLUMNS = {
    "trade_date",
    "industry_code",
    "source",
    "published_at",
    "available_date",
    "fetched_at",
    "source_version",
    "source_hash",
    "source_hash_basis",
    "source_artifact_path",
    "revision_status",
    "availability_basis",
    "data_status",
}
PIT_CORE_FIELDS = (
    "trade_date",
    "published_at",
    "available_date",
    "fetched_at",
    "source_version",
    "revision_status",
)
REVISION_STATUSES = {"original", "restated", "superseded"}
ACTIVE_REVISION_STATUSES = {"original", "restated"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SW_INDUSTRY_CODE_RE = re.compile(r"^801\d{3}$")
HISTORICAL_METHODOLOGY_PROMOTION_VERIFIER_AVAILABLE = False
HISTORICAL_METHODOLOGY_ROUTE_BLOCKER = "historical_pit_and_classification_receipt_verifier_missing"


class ValuationPITContractError(ValueError):
    """Raised when valuation rows do not prove historical availability."""


@dataclass(frozen=True)
class ValuationPITAudit:
    eligible: bool
    status: str
    row_count: int
    errors: tuple[str, ...]

    def require(self, *, source: str = "valuation history") -> None:
        if self.eligible:
            return
        detail = "; ".join(self.errors) or self.status
        raise ValuationPITContractError(f"{source} is not PIT eligible: {detail}")


def methodology_control_ready(summary: dict[str, Any]) -> bool:
    return (
        summary.get("audit_passed") is True
        and summary.get("methodology_remediation_complete") is True
        and summary.get("legacy_oos_label_corrected") is True
        and summary.get("historical_review_set_label") == "historical_review_used_in_iteration"
        and tuple(summary.get("valuation_required_fields", ())) == PIT_CORE_FIELDS
        and summary.get("policy_status") == "research_only"
        and summary.get("production_ready") is False
        and summary.get("auto_execution_allowed") is False
    )


def verified_historical_methodology_route_ready(summary: dict[str, Any]) -> bool:
    if not HISTORICAL_METHODOLOGY_PROMOTION_VERIFIER_AVAILABLE:
        return False
    return (
        methodology_control_ready(summary)
        and summary.get("promotion_gate_passed") is True
        and summary.get("historical_valuation_pit_gate_passed") is True
        and summary.get("historical_classification_gate_passed") is True
        and int(summary.get("promotion_eligible_valuation_row_count", 0) or 0) > 0
        and summary.get("valuation_availability_status") == "pit_verified_for_promotion"
        and summary.get("classification_history_status") in {"verified", "verified_for_promotion"}
    )


def methodology_route_ready(
    summary: dict[str, Any],
    forward_evidence_summary: Mapping[str, Any] | None = None,
) -> bool:
    """Require an intact control and one evidence route verified inside this call."""

    from research_evidence_routes import verified_forward_evidence_ready

    forward_ready = (
        isinstance(forward_evidence_summary, Mapping)
        and verified_forward_evidence_ready(forward_evidence_summary)
    )
    return methodology_control_ready(summary) and (
        verified_historical_methodology_route_ready(summary) or forward_ready
    )


def _parse_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None or pd.isna(value):
        return None
    try:
        parsed = pd.Timestamp(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.tz_convert(SHANGHAI)


def _normalize_industry_code(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def revision_chain_valid_mask(frame: pd.DataFrame) -> pd.Series:
    """Mark chains whose latest publication is active and earlier vintages are superseded."""

    required = {"industry_code", "trade_date", "published_at", "revision_status", "source_version"}
    result = pd.Series(False, index=frame.index, dtype=bool)
    if frame.empty or not required.issubset(frame.columns):
        return result
    for _, group in frame.groupby(["industry_code", "trade_date"], dropna=False, sort=False):
        published = group["published_at"].map(_parse_timestamp)
        if published.isna().any() or published.duplicated(keep=False).any():
            continue
        ordered = group.assign(_published_order=published).sort_values(
            ["_published_order", "source_version"], kind="stable"
        )
        statuses = ordered["revision_status"].fillna("").astype(str).tolist()
        if not statuses or statuses[-1] not in ACTIVE_REVISION_STATUSES:
            continue
        if any(status != "superseded" for status in statuses[:-1]):
            continue
        result.loc[group.index] = True
    return result


def source_artifact_valid_mask(frame: pd.DataFrame, *, artifact_root: Path = ROOT) -> pd.Series:
    """Verify each declared source hash against an immutable archived artifact."""

    required = {"source_hash", "source_hash_basis", "source_artifact_path"}
    result = pd.Series(False, index=frame.index, dtype=bool)
    if frame.empty or not required.issubset(frame.columns):
        return result
    root = artifact_root.resolve()
    cache: dict[str, str | None] = {}
    for index, row in frame.iterrows():
        if row.get("source_hash_basis") != "immutable_raw_artifact_sha256":
            continue
        relative = Path(str(row.get("source_artifact_path", "")).strip())
        if not str(relative) or relative.is_absolute():
            continue
        resolved = (root / relative).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            continue
        key = resolved.as_posix()
        if key not in cache:
            cache[key] = hashlib.sha256(resolved.read_bytes()).hexdigest()
        result.at[index] = cache[key] == str(row.get("source_hash", "")).lower()
    return result


def load_frozen_trading_calendar(path: Path = DEFAULT_TRADING_CALENDAR) -> tuple[date, ...]:
    if not path.exists():
        raise ValuationPITContractError(f"frozen A-share trading calendar is missing: {path}")
    if path.resolve() == DEFAULT_TRADING_CALENDAR.resolve():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != FROZEN_TRADING_CALENDAR_SHA256:
            raise ValuationPITContractError(
                "frozen A-share trading calendar hash mismatch: "
                f"expected={FROZEN_TRADING_CALENDAR_SHA256}; actual={digest}"
            )
    calendar = pd.read_csv(path, encoding="utf-8-sig")
    if list(calendar.columns) != ["trade_date"]:
        raise ValuationPITContractError("frozen trading calendar must contain only trade_date")
    parsed = pd.to_datetime(calendar["trade_date"], errors="coerce").dt.date
    if parsed.isna().any():
        raise ValuationPITContractError("frozen trading calendar contains invalid dates")
    dates = tuple(parsed.tolist())
    if not dates or dates != tuple(sorted(set(dates))):
        raise ValuationPITContractError("frozen trading calendar must be non-empty, unique, and sorted")
    return dates


def first_eligible_trade_date(published_at: Any, trading_dates: tuple[date, ...]) -> date:
    """Map a real publication timestamp to the first daily decision date.

    Publications strictly before the 15:00 Shanghai cutoff on a trading day can
    enter that day's close decision.  At/after close, on weekends, or on exchange
    holidays, availability moves to the next date present in the frozen calendar.
    """

    published = _parse_timestamp(published_at)
    if published is None:
        raise ValuationPITContractError("published_at must be a timezone-aware timestamp")
    published_date = published.date()
    trading_set = set(trading_dates)
    if published_date in trading_set and published.time().replace(tzinfo=None) < DAILY_DECISION_CUTOFF:
        return published_date
    for candidate in trading_dates:
        if candidate > published_date:
            return candidate
    raise ValuationPITContractError(f"frozen trading calendar has no session after {published_date}")


def audit_pit_valuation_history(
    frame: pd.DataFrame,
    *,
    calendar_path: Path = DEFAULT_TRADING_CALENDAR,
    artifact_root: Path = ROOT,
) -> ValuationPITAudit:
    """Prove that every valuation row has source-backed availability metadata.

    Availability is derived only from ``published_at`` and the frozen A-share
    trading calendar.  No calendar-day lag is accepted as evidence.
    """

    errors: list[str] = []
    missing = sorted(PIT_REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        errors.append("missing columns: " + ",".join(missing))
        return ValuationPITAudit(False, "blocked_missing_availability_metadata", len(frame), tuple(errors))
    if frame.empty:
        return ValuationPITAudit(False, "blocked_empty_history", 0, ("history has no rows",))

    try:
        trading_dates = load_frozen_trading_calendar(calendar_path)
    except ValuationPITContractError as exc:
        return ValuationPITAudit(False, "blocked_invalid_trading_calendar", len(frame), (str(exc),))

    trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    available_dates = pd.to_datetime(frame["available_date"], errors="coerce").dt.date
    invalid_trade = int(trade_dates.isna().sum())
    invalid_available = int(available_dates.isna().sum())
    trading_set = set(trading_dates)
    if invalid_trade:
        errors.append(f"invalid trade_date rows={invalid_trade}")
    if invalid_available:
        errors.append(f"invalid available_date rows={invalid_available}")
    invalid_trade_session = int((trade_dates.notna() & ~trade_dates.isin(trading_set)).sum())
    invalid_available_session = int((available_dates.notna() & ~available_dates.isin(trading_set)).sum())
    if invalid_trade_session:
        errors.append(f"trade_date is not in the frozen trading calendar; invalid rows={invalid_trade_session}")
    if invalid_available_session:
        errors.append(f"available_date is not in the frozen trading calendar; invalid rows={invalid_available_session}")

    statuses = frame["data_status"].fillna("").astype(str)
    bad_status = int(statuses.ne(VERIFIED_STATUS).sum())
    if bad_status:
        errors.append(f"data_status must be {VERIFIED_STATUS}; invalid rows={bad_status}")

    bases = frame["availability_basis"].fillna("").astype(str)
    bad_basis = int((~bases.isin(VERIFIED_AVAILABILITY_BASES)).sum())
    if bad_basis:
        errors.append(f"availability_basis is not source-backed; invalid rows={bad_basis}")

    sources = frame["source"].fillna("").astype(str).str.strip()
    bad_source = sources.str.lower().isin({"", "unknown", "unverified", "synthetic"})
    recovered_source = sources.eq("recovered_from_v2_5_quality_components")
    if bad_source.any():
        errors.append(f"source identifier is missing or unverified; invalid rows={int(bad_source.sum())}")
    if recovered_source.any():
        errors.append(f"recovered V2.5 current components are not official historical rows; invalid rows={int(recovered_source.sum())}")

    industry_codes = frame["industry_code"].map(_normalize_industry_code)
    bad_industry_code = ~industry_codes.map(lambda value: bool(SW_INDUSTRY_CODE_RE.fullmatch(value)))
    if bad_industry_code.any():
        errors.append(
            "industry_code must be a six-digit SW industry code matching 801xxx; "
            f"invalid rows={int(bad_industry_code.sum())}"
        )

    bad_published = 0
    bad_fetched = 0
    fetched_before_published = 0
    wrong_available_date = 0
    published_before_trade = 0
    for trade_date, available_date, published_at, fetched_at in zip(
        trade_dates,
        available_dates,
        frame["published_at"],
        frame["fetched_at"],
        strict=True,
    ):
        published = _parse_timestamp(published_at)
        fetched = _parse_timestamp(fetched_at)
        if published is None:
            bad_published += 1
            continue
        if fetched is None:
            bad_fetched += 1
            continue
        if fetched < published:
            fetched_before_published += 1
        if not pd.isna(trade_date) and published.date() < trade_date:
            published_before_trade += 1
        if pd.isna(available_date):
            continue
        expected = first_eligible_trade_date(published, trading_dates)
        if available_date != expected:
            wrong_available_date += 1
    if bad_published:
        errors.append(f"published_at must be timezone-aware; invalid rows={bad_published}")
    if bad_fetched:
        errors.append(f"fetched_at must be timezone-aware; invalid rows={bad_fetched}")
    if fetched_before_published:
        errors.append(f"fetched_at precedes published_at; invalid rows={fetched_before_published}")
    if published_before_trade:
        errors.append(f"published_at precedes trade_date; invalid rows={published_before_trade}")
    if wrong_available_date:
        errors.append(f"available_date does not match frozen-calendar rule; invalid rows={wrong_available_date}")

    versions = frame["source_version"].fillna("").astype(str)
    hashes = frame["source_hash"].fillna("").astype(str).str.lower()
    bad_hash = int((~hashes.map(lambda value: bool(SHA256_RE.fullmatch(value)))).sum())
    bad_version = int((versions != ("sha256:" + hashes)).sum())
    if bad_hash:
        errors.append(f"source_hash must be lowercase SHA-256; invalid rows={bad_hash}")
    if bad_version:
        errors.append(f"source_version must equal sha256:<source_hash>; invalid rows={bad_version}")
    artifact_mask = source_artifact_valid_mask(frame, artifact_root=artifact_root)
    if not artifact_mask.all():
        errors.append(f"source_hash must match an immutable archived source artifact; invalid rows={int((~artifact_mask).sum())}")

    revisions = frame["revision_status"].fillna("").astype(str)
    bad_revision = int((~revisions.isin(REVISION_STATUSES)).sum())
    if bad_revision:
        errors.append(f"invalid revision_status rows={bad_revision}")
    duplicate_version = int(frame.duplicated(["industry_code", "trade_date", "source_version"], keep=False).sum())
    if duplicate_version:
        errors.append(f"duplicate industry_code+trade_date+source_version rows={duplicate_version}")
    active = frame[revisions.isin(ACTIVE_REVISION_STATUSES)]
    active_counts = active.groupby(["industry_code", "trade_date"], dropna=False).size()
    all_keys = frame.groupby(["industry_code", "trade_date"], dropna=False).size().index
    invalid_active_keys = sum(int(active_counts.get(key, 0) != 1) for key in all_keys)
    if invalid_active_keys:
        errors.append(f"each industry_code+trade_date needs exactly one active revision; invalid keys={invalid_active_keys}")
    revision_chain_mask = revision_chain_valid_mask(frame)
    invalid_revision_keys = int(
        frame.loc[~revision_chain_mask, ["industry_code", "trade_date"]].drop_duplicates().shape[0]
    )
    if invalid_revision_keys:
        errors.append(f"latest revision must be active and all earlier vintages superseded; invalid keys={invalid_revision_keys}")

    eligible = not errors
    return ValuationPITAudit(
        eligible,
        "pit_verified" if eligible else "blocked_invalid_availability_metadata",
        len(frame),
        tuple(errors),
    )


def prepare_pit_valuation_history(
    frame: pd.DataFrame,
    *,
    source: str = "valuation history",
    calendar_path: Path = DEFAULT_TRADING_CALENDAR,
    artifact_root: Path = ROOT,
) -> pd.DataFrame:
    """Validate and normalise a history for backward as-of joins."""

    audit = audit_pit_valuation_history(frame, calendar_path=calendar_path, artifact_root=artifact_root)
    audit.require(source=source)
    out = frame.copy()
    out["industry_code"] = out["industry_code"].map(_normalize_industry_code)
    out["valuation_trade_date"] = pd.to_datetime(out["trade_date"], errors="raise")
    out["valuation_available_date"] = pd.to_datetime(out["available_date"], errors="raise")
    out["valuation_published_at"] = pd.to_datetime(out["published_at"], errors="raise", utc=True)
    out["valuation_fetched_at"] = pd.to_datetime(out["fetched_at"], errors="raise", utc=True)
    return out.sort_values(
        ["industry_code", "valuation_available_date", "valuation_published_at", "source_version"]
    ).reset_index(drop=True)


def attach_pit_valuation_asof(
    decisions: pd.DataFrame,
    valuation: pd.DataFrame,
    *,
    decision_date_column: str,
    industry_code_column: str = "industry_code",
    calendar_path: Path = DEFAULT_TRADING_CALENDAR,
    artifact_root: Path = ROOT,
) -> pd.DataFrame:
    """Attach only rows whose proved availability is on/before the decision date."""

    prepared = prepare_pit_valuation_history(
        valuation,
        calendar_path=calendar_path,
        artifact_root=artifact_root,
    )
    left = decisions.copy()
    left["_pit_decision_date"] = pd.to_datetime(left[decision_date_column], errors="raise")
    left[industry_code_column] = left[industry_code_column].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
    rows: list[pd.DataFrame] = []
    for code, left_group in left.groupby(industry_code_column, sort=True):
        right = prepared[prepared["industry_code"].eq(code)].copy()
        if right.empty:
            rows.append(left_group)
            continue
        right = right.drop(columns=["industry_code", "trade_date"], errors="ignore")
        right = right.sort_values(
            ["valuation_available_date", "valuation_trade_date", "valuation_published_at", "source_version"]
        )
        right = right.drop_duplicates("valuation_available_date", keep="last")
        merged = pd.merge_asof(
            left_group.sort_values("_pit_decision_date"),
            right,
            left_on="_pit_decision_date",
            right_on="valuation_available_date",
            direction="backward",
            allow_exact_matches=True,
        )
        rows.append(merged)
    if not rows:
        return left.drop(columns=["_pit_decision_date"])
    out = pd.concat(rows, ignore_index=True)
    matched = out["valuation_available_date"].notna() if "valuation_available_date" in out.columns else pd.Series(False, index=out.index)
    if matched.any() and (out.loc[matched, "valuation_available_date"] > out.loc[matched, "_pit_decision_date"]).any():
        raise ValuationPITContractError("as-of join selected valuation after the decision date")
    return out.drop(columns=["_pit_decision_date"])


def mark_trade_date_only_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Make the public SWS limitation explicit without inventing availability."""

    out = frame.copy()
    out["available_date"] = ""
    out["published_at"] = ""
    out["fetched_at"] = ""
    out["source_version"] = ""
    out["source_hash"] = ""
    out["revision_status"] = "unverified"
    out["availability_basis"] = "missing_source_publication_metadata"
    out["data_status"] = NON_PIT_HISTORY_STATUS
    out["pit_eligible"] = False
    return out


def official_valuation_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Exclude rows reconstructed from a current V2.5 component snapshot."""

    if "source" not in frame.columns:
        return frame.copy()
    source = frame["source"].fillna("").astype(str)
    return frame[~source.eq("recovered_from_v2_5_quality_components")].copy()


def official_valuation_cutoff(frame: pd.DataFrame) -> str:
    official = official_valuation_history(frame)
    if official.empty or "trade_date" not in official.columns:
        return ""
    latest = pd.to_datetime(official["trade_date"], errors="coerce").max()
    return "" if pd.isna(latest) else latest.date().isoformat()


def current_snapshot_as_of_error(requested_as_of_date: str, observed_at: datetime) -> str | None:
    """A live current-only endpoint must never be labelled with an earlier date."""

    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    observed_date = observed_at.astimezone(SHANGHAI).date().isoformat()
    if requested_as_of_date != observed_date:
        return (
            f"live valuation snapshot observed on {observed_date} cannot be archived as "
            f"--trade-date {requested_as_of_date}; use an immutable snapshot actually observed by that as-of date"
        )
    return None


def stamp_current_snapshot(
    frame: pd.DataFrame,
    *,
    requested_as_of_date: str,
    observed_at: datetime,
) -> pd.DataFrame:
    """Stamp a current-only snapshot with observation evidence, never a fake trade date."""

    error = current_snapshot_as_of_error(requested_as_of_date, observed_at)
    if error:
        raise ValuationPITContractError(error)
    local = observed_at.astimezone(SHANGHAI)
    out = frame.copy()
    canonical = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    source_hash = hashlib.sha256(canonical).hexdigest()
    out["snapshot_observed_at"] = local.isoformat(timespec="seconds")
    out["snapshot_available_date"] = local.date().isoformat()
    out["requested_as_of_date"] = requested_as_of_date
    out["source_trade_date"] = ""
    out["published_at"] = ""
    out["available_date"] = local.date().isoformat()
    out["fetched_at"] = local.isoformat(timespec="seconds")
    out["source_hash"] = source_hash
    out["source_version"] = f"sha256:{source_hash}"
    out["revision_status"] = "original"
    out["availability_basis"] = "first_observed_current_snapshot"
    out["data_status"] = CURRENT_SNAPSHOT_STATUS
    out["pit_eligible"] = False
    return out


def archive_current_snapshot_immutable(
    frame: pd.DataFrame,
    *,
    requested_as_of_date: str,
    observed_at: datetime,
    snapshot_dir: Path,
) -> dict[str, Any]:
    """Write one immutable first-observed snapshot per observation date."""

    stamped = stamp_current_snapshot(
        frame,
        requested_as_of_date=requested_as_of_date,
        observed_at=observed_at,
    )
    observed_date = observed_at.astimezone(SHANGHAI).date().isoformat()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    output_path = snapshot_dir / f"{observed_date}.csv"
    if output_path.exists():
        existing = pd.read_csv(output_path, encoding="utf-8-sig")
        required = {"snapshot_observed_at", "snapshot_available_date", "data_status", "pit_eligible"}
        if not required.issubset(existing.columns):
            raise ValuationPITContractError(
                f"refusing to overwrite legacy snapshot without observation metadata: {output_path}"
            )
        return {
            "path": str(output_path.resolve()),
            "rows": int(len(existing)),
            "observed_at": str(existing["snapshot_observed_at"].iloc[0]),
            "available_date": str(existing["snapshot_available_date"].iloc[0]),
            "data_status": str(existing["data_status"].iloc[0]),
            "pit_eligible": False,
            "archive_status": "immutable_first_observation_reused",
        }
    temporary = output_path.with_suffix(".csv.tmp")
    stamped.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(output_path)
    return {
        "path": str(output_path.resolve()),
        "rows": int(len(stamped)),
        "observed_at": str(stamped["snapshot_observed_at"].iloc[0]),
        "available_date": str(stamped["snapshot_available_date"].iloc[0]),
        "data_status": CURRENT_SNAPSHOT_STATUS,
        "pit_eligible": False,
        "archive_status": "immutable_first_observation_written",
    }
