#!/usr/bin/env python
"""Domain contract for the V5.25-V5.35 fund-flow forward evidence chain.

The JSONL file is the authoritative append-only ledger.  The historical CSV is
kept as an atomic, materialized compatibility view so older reporting scripts
can continue to read it without becoming the source of truth.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from research_integrity import (
    GENESIS_HASH,
    DuplicateRecordError,
    HashChainError,
    InterProcessFileLock,
    atomic_write_bytes,
    atomic_write_csv,
    canonical_json_bytes,
    file_sha256,
    hash_chain_record,
    json_fingerprint,
    lock_path_for,
    verify_hash_chain,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
SCHEMA_VERSION = "2.1"
LEGACY_MIGRATION_VERSION = "fund_flow_forward_ledger_v2_20260718"

LEGACY_FIELDS = [
    "recorded_at", "batch_id", "policy_version", "policy_id", "policy_status",
    "decision", "outcome_status", "signal_date", "planned_entry_date",
    "planned_exit_date", "industry_code", "industry_name", "selection_score",
    "fund_flow_research_status", "fund_flow_overlay_status", "ths_industry_name",
    "ths_today_net_flow", "ths_5d_net_flow", "today_flow_positive",
    "five_day_flow_positive", "dual_positive_flow", "historical_failure_flag",
    "settlement_status", "settlement_notes", "actual_entry_date", "actual_exit_date",
    "realized_return", "benchmark_return", "realized_relative_return",
    "future_return_rank_pct", "future_top_quintile", "entry_price_freeze_status",
    "benchmark_entry_freeze_status", "entry_date_exact", "exit_date_exact",
    "benchmark_universe_count_used", "settlement_source_artifact",
    "settlement_source_fingerprint", "settlement_source_row_count",
    "settlement_calculation_version", "sample_scope", "window_signal_pass",
    "valuation_gate_pass", "stabilization_gate_pass", "window_id",
    "frozen_selection_rule_id", "qualified_for_goal", "qualification_reason",
]

INTEGRITY_FIELDS = [
    "record_schema_version", "event_type", "event_id", "observation_id",
    "parent_event_id", "ledger_sequence", "event_recorded_at_utc",
    "detected_at_utc", "evidence_cutoff", "entry_cutoff", "freeze_deadline_utc",
    "source_artifact", "source_fingerprint", "source_fingerprint_status",
    "calendar_source", "calendar_fingerprint", "experiment_id", "cohort_id",
    "cohort_manifest_hash", "rule_id", "code_version", "late_backfill_excluded",
    "integrity_eligible", "promotion_eligible", "migration_version",
    "previous_hash", "record_hash",
]

LEDGER_FIELDS = LEGACY_FIELDS + INTEGRITY_FIELDS

SETTLEMENT_MUTABLE_FIELDS = frozenset({
    "outcome_status", "settlement_status", "settlement_notes", "actual_entry_date",
    "actual_exit_date", "realized_return", "benchmark_return",
    "realized_relative_return", "future_return_rank_pct", "future_top_quintile",
    "entry_price_freeze_status", "benchmark_entry_freeze_status",
    "entry_date_exact", "exit_date_exact", "benchmark_universe_count_used",
    "settlement_source_artifact", "settlement_source_fingerprint",
    "settlement_source_row_count", "settlement_calculation_version",
})
EVENT_METADATA_FIELDS = frozenset({
    "record_schema_version", "event_type", "event_id", "parent_event_id",
    "ledger_sequence", "event_recorded_at_utc", "previous_hash", "record_hash",
})
SETTLEMENT_REQUIRED_NONEMPTY_FIELDS = frozenset({
    "actual_entry_date", "actual_exit_date", "realized_return", "benchmark_return",
    "realized_relative_return", "future_return_rank_pct", "future_top_quintile",
    "entry_price_freeze_status", "benchmark_entry_freeze_status",
    "entry_date_exact", "exit_date_exact", "benchmark_universe_count_used",
    "settlement_source_artifact", "settlement_source_fingerprint",
    "settlement_source_row_count", "settlement_calculation_version",
})


def is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "是"}


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("UTC timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI).astimezone(UTC)
    return parsed.astimezone(UTC)


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def market_timestamp(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=SHANGHAI).astimezone(UTC)


def evidence_cutoff_utc(signal_day: date) -> datetime:
    return market_timestamp(signal_day, time(15, 0))


def entry_cutoff_utc(entry_day: date) -> datetime:
    return market_timestamp(entry_day, time(9, 30))


def freeze_window_start_utc(entry_day: date) -> datetime:
    return market_timestamp(entry_day, time(15, 0))


def freeze_deadline_utc(entry_day: date) -> datetime:
    return market_timestamp(entry_day, time(16, 0))


def observation_detected_on_time(row: Mapping[str, Any]) -> bool:
    return observation_timing_status(row) == "on_time"


def observation_timing_status(row: Mapping[str, Any]) -> str:
    """Classify observation timing without treating a pre-window run as late."""
    detected = parse_timestamp(row.get("detected_at_utc"))
    evidence = parse_timestamp(row.get("evidence_cutoff"))
    entry = parse_timestamp(row.get("entry_cutoff"))
    if not detected or not evidence or not entry or evidence >= entry:
        return "invalid"
    if detected < evidence:
        return "early_pending"
    if detected >= entry:
        return "late_excluded"
    return "on_time"


def freeze_recorded_on_time(row: Mapping[str, Any]) -> bool:
    if freeze_timing_status(row) != "on_time":
        return False
    planned = parse_date(row.get("planned_entry_date"))
    actual = parse_date(row.get("actual_entry_date"))
    as_of = parse_date(row.get("as_of_date"))
    return bool(planned and actual == planned and as_of == planned)


def freeze_timing_status(row: Mapping[str, Any]) -> str:
    """Classify an entry-freeze attempt as pending, on-time, late, or invalid."""
    planned = parse_date(row.get("planned_entry_date"))
    as_of = parse_date(row.get("as_of_date"))
    frozen_at = parse_timestamp(row.get("freeze_at_utc"))
    if not planned or not as_of or not frozen_at:
        return "invalid"
    start = freeze_window_start_utc(planned)
    deadline = freeze_deadline_utc(planned)
    if frozen_at < start or as_of < planned:
        return "early_pending"
    if frozen_at > deadline or as_of > planned:
        return "late_excluded"
    return "on_time"


def logical_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("batch_id", "")), str(row.get("industry_code", "")).zfill(6)


def logical_key_text(row: Mapping[str, Any]) -> str:
    return "|".join(logical_key(row))


def stable_observation_id(row: Mapping[str, Any]) -> str:
    payload = {
        "batch_id": str(row.get("batch_id", "")),
        "industry_code": str(row.get("industry_code", "")).zfill(6),
        "signal_date": str(row.get("signal_date", "")),
        "planned_entry_date": str(row.get("planned_entry_date", "")),
        "planned_exit_date": str(row.get("planned_exit_date", "")),
    }
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return f"ffobs-{digest[:24]}"


def with_schema_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    out = {field: row.get(field, "") for field in LEDGER_FIELDS}
    out["industry_code"] = str(out.get("industry_code", "")).zfill(6)
    out["record_schema_version"] = str(out.get("record_schema_version") or SCHEMA_VERSION)
    if not str(out.get("sample_scope", "")).strip():
        out["sample_scope"] = "exploratory_fund_flow_only"
    if not str(out.get("qualified_for_goal", "")).strip():
        out["qualified_for_goal"] = "False"
    if not str(out.get("qualification_reason", "")).strip():
        out["qualification_reason"] = "legacy_row_missing_goal_qualification"
    return out


def _legacy_detected_at(row: Mapping[str, Any]) -> datetime | None:
    return parse_timestamp(row.get("recorded_at"))


def legacy_observation_event(row: Mapping[str, Any], sequence: int) -> dict[str, Any]:
    out = with_schema_fields(row)
    observation_id = stable_observation_id(out)
    signal = parse_date(out.get("signal_date"))
    entry = parse_date(out.get("planned_entry_date"))
    detected = _legacy_detected_at(out)
    out.update({
        "record_schema_version": "2.0",
        "event_type": "observation",
        "event_id": f"{observation_id}:observation",
        "observation_id": observation_id,
        "parent_event_id": "",
        "ledger_sequence": str(sequence),
        "event_recorded_at_utc": iso_utc(detected) if detected else "",
        "detected_at_utc": iso_utc(detected) if detected else "",
        "evidence_cutoff": iso_utc(evidence_cutoff_utc(signal)) if signal else "",
        "entry_cutoff": iso_utc(entry_cutoff_utc(entry)) if entry else "",
        "freeze_deadline_utc": iso_utc(freeze_deadline_utc(entry)) if entry else "",
        "source_artifact": "unavailable_legacy_source_snapshot",
        "source_fingerprint": "UNVERIFIED_LEGACY_SOURCE",
        "source_fingerprint_status": "unverified_legacy",
        "calendar_source": "legacy_dates_unverified",
        "calendar_fingerprint": "UNVERIFIED_LEGACY_CALENDAR",
        "experiment_id": "fund_flow_forward_exploratory_v5_25",
        "cohort_id": "legacy_exploratory_20260622",
        "cohort_manifest_hash": "UNVERIFIED_LEGACY_COHORT",
        "rule_id": str(out.get("frozen_selection_rule_id") or "legacy_unfrozen_fund_flow_dual_positive"),
        "code_version": "UNVERIFIED_LEGACY_CODE",
        "late_backfill_excluded": "False",
        "integrity_eligible": "False",
        "promotion_eligible": "False",
        "migration_version": LEGACY_MIGRATION_VERSION,
        "qualified_for_goal": "False",
        "sample_scope": "exploratory_fund_flow_only",
        "qualification_reason": str(out.get("qualification_reason") or "legacy_row_missing_goal_qualification"),
    })
    return out


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _chain_records(payloads: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    previous = GENESIS_HASH
    records: list[dict[str, Any]] = []
    for sequence, payload in enumerate(payloads, start=1):
        record = dict(payload)
        record["ledger_sequence"] = str(sequence)
        record["previous_hash"] = previous
        record["record_hash"] = hash_chain_record(record)
        previous = str(record["record_hash"])
        records.append(record)
    return records


def _jsonl_bytes(records: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(record) + b"\n" for record in records)


def migrate_legacy_csv(
    csv_path: Path,
    event_ledger_path: Path,
    *,
    backup_path: Path,
) -> list[dict[str, Any]]:
    """One-time, fail-closed conversion of the legacy CSV into a hash chain."""

    rows = read_csv_rows(csv_path)
    if not rows:
        raise ValueError(f"legacy ledger is missing or empty: {csv_path}")
    with InterProcessFileLock(lock_path_for(event_ledger_path)):
        if event_ledger_path.exists():
            verified = verify_hash_chain(event_ledger_path)
            record_ledger_checkpoint(event_ledger_path)
            return [dict(row) for row in verified.records]
        original = csv_path.read_bytes()
        if backup_path.exists() and backup_path.read_bytes() != original:
            raise HashChainError(f"migration backup already exists with different bytes: {backup_path}")
        if not backup_path.exists():
            atomic_write_bytes(backup_path, original)
        payloads = [legacy_observation_event(row, index) for index, row in enumerate(rows, start=1)]
        records = _chain_records(payloads)
        atomic_write_bytes(event_ledger_path, _jsonl_bytes(records))
        verified = verify_hash_chain(event_ledger_path)
        if verified.record_count != len(rows):
            raise HashChainError("legacy migration record count mismatch")
        write_materialized_csv(csv_path, materialize_observations(verified.records))
        record_ledger_checkpoint(event_ledger_path)
        return [dict(row) for row in verified.records]


def read_events(event_ledger_path: Path) -> list[dict[str, Any]]:
    return [dict(row) for row in verify_hash_chain(event_ledger_path).records]


def materialize_observations(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    settlement_seen: set[str] = set()
    for event in events:
        event_type = str(event.get("event_type", ""))
        observation_id = str(event.get("observation_id", ""))
        if not observation_id:
            continue
        if event_type == "observation":
            if observation_id in states:
                raise DuplicateRecordError(f"duplicate observation event: {observation_id}")
            states[observation_id] = dict(event)
            order.append(observation_id)
        elif event_type == "settlement":
            if observation_id not in states:
                raise HashChainError(f"settlement precedes observation: {observation_id}")
            if observation_id in settlement_seen:
                raise DuplicateRecordError(f"duplicate settlement event: {observation_id}")
            state = states[observation_id]
            if str(event.get("parent_event_id", "")) != str(state.get("event_id", "")):
                raise HashChainError(f"settlement parent mismatch: {observation_id}")
            if str(event.get("settlement_status", "")) != "settled" or str(event.get("outcome_status", "")) != "settled_forward_observation":
                raise HashChainError(f"settlement event is not terminal: {observation_id}")
            missing = sorted(field for field in SETTLEMENT_REQUIRED_NONEMPTY_FIELDS if not str(event.get(field, "")).strip())
            if missing:
                raise HashChainError(f"settlement event missing required fields {missing}: {observation_id}")
            if not is_true(event.get("entry_date_exact")) or not is_true(event.get("exit_date_exact")):
                raise HashChainError(f"settlement event dates are not exact: {observation_id}")
            for key in LEDGER_FIELDS:
                if key in SETTLEMENT_MUTABLE_FIELDS or key in EVENT_METADATA_FIELDS:
                    continue
                if str(event.get(key, "")) != str(state.get(key, "")):
                    raise HashChainError(f"settlement mutates immutable field {key}: {observation_id}")
            merged = dict(states[observation_id])
            for key in SETTLEMENT_MUTABLE_FIELDS:
                value = event.get(key, "")
                if str(value) != "":
                    merged[key] = value
            merged["latest_event_type"] = "settlement"
            merged["latest_event_id"] = event.get("event_id", "")
            merged["latest_event_hash"] = event.get("record_hash", "")
            states[observation_id] = merged
            settlement_seen.add(observation_id)
        else:
            raise HashChainError(f"unknown ledger event type: {event_type}")
    return [with_schema_fields(states[item]) for item in order]


def write_materialized_csv(csv_path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    materialized = [with_schema_fields(row) for row in rows]
    atomic_write_csv(csv_path, materialized, fieldnames=LEDGER_FIELDS, utf8_bom=True)


def append_events(
    event_ledger_path: Path,
    payloads: Sequence[Mapping[str, Any]],
    *,
    unique_event_ids: bool = True,
    maintain_checkpoint: bool = True,
) -> list[dict[str, Any]]:
    """Atomically append a batch while preserving the exact verified prefix.

    A non-empty ledger is appendable only when its independent head checkpoint
    already exists and verifies.  Missing checkpoint state is ambiguous: the
    ledger may be a legitimate pre-checkpoint migration input, or it may have
    been rolled back together with its checkpoint.  Ordinary append therefore
    fails closed instead of silently accepting the current head as trustworthy.
    Initial checkpoint creation is reserved for a brand-new/empty ledger in
    this transaction, or an explicit ``record_ledger_checkpoint`` migration or
    bootstrap call.
    """

    if not payloads:
        return []
    event_ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with InterProcessFileLock(lock_path_for(event_ledger_path)):
        verified = verify_hash_chain(event_ledger_path)
        if maintain_checkpoint and verified.record_count > 0:
            verify_ledger_checkpoint(event_ledger_path)
        existing_ids = {str(row.get("event_id", "")): row for row in verified.records}
        new_payloads: list[dict[str, Any]] = []
        seen_new: set[str] = set()
        for payload in payloads:
            item = dict(payload)
            event_id = str(item.get("event_id", ""))
            if not event_id:
                raise ValueError("event_id is required")
            if event_id in seen_new:
                raise DuplicateRecordError(f"duplicate event_id in append batch: {event_id}")
            seen_new.add(event_id)
            if event_id in existing_ids:
                if unique_event_ids:
                    existing_payload = {k: v for k, v in existing_ids[event_id].items() if k not in {"previous_hash", "record_hash", "ledger_sequence"}}
                    proposed_payload = {k: v for k, v in item.items() if k not in {"previous_hash", "record_hash", "ledger_sequence"}}
                    if canonical_json_bytes(existing_payload) != canonical_json_bytes(proposed_payload):
                        raise DuplicateRecordError(f"conflicting duplicate event_id: {event_id}")
                    continue
            new_payloads.append(item)
        if not new_payloads:
            return []
        previous = verified.head_hash
        sequence = verified.record_count
        appended: list[dict[str, Any]] = []
        for payload in new_payloads:
            sequence += 1
            record = dict(payload)
            record["ledger_sequence"] = str(sequence)
            record["previous_hash"] = previous
            record["record_hash"] = hash_chain_record(record)
            previous = str(record["record_hash"])
            appended.append(record)
        existing_bytes = event_ledger_path.read_bytes() if event_ledger_path.exists() else b""
        separator = b"" if not existing_bytes or existing_bytes.endswith(b"\n") else b"\n"
        atomic_write_bytes(event_ledger_path, existing_bytes + separator + _jsonl_bytes(appended))
        verify_hash_chain(event_ledger_path, expected_head=previous)
        if maintain_checkpoint:
            record_ledger_checkpoint(event_ledger_path)
        return appended


def checkpoint_path_for(event_ledger_path: Path) -> Path:
    return event_ledger_path.with_name(f"{event_ledger_path.stem}_head_checkpoints.jsonl")


def record_ledger_checkpoint(event_ledger_path: Path) -> dict[str, Any]:
    """Explicitly record or bootstrap the independently chained ledger head.

    Callers must use this entry point deliberately for migration/bootstrap.
    Normal writes go through :func:`append_events`, which refuses to recreate a
    missing checkpoint for an already non-empty ledger.
    """

    verified = verify_hash_chain(event_ledger_path)
    checkpoint_path = checkpoint_path_for(event_ledger_path)
    target_file_hash = file_sha256(event_ledger_path)
    if checkpoint_path.exists():
        checkpoint_chain = verify_hash_chain(checkpoint_path)
        if checkpoint_chain.records:
            latest = checkpoint_chain.records[-1]
            if (
                int(str(latest.get("target_event_count", "-1"))) == verified.record_count
                and str(latest.get("target_head_hash", "")) == verified.head_hash
                and str(latest.get("target_file_sha256", "")) == target_file_hash
            ):
                return dict(latest)
    payload = {
        "event_type": "ledger_head_checkpoint",
        "event_id": f"checkpoint:{event_ledger_path.stem}:{verified.record_count}:{verified.head_hash}",
        "event_recorded_at_utc": iso_utc(utc_now()),
        "ledger_kind": event_ledger_path.stem,
        "target_event_count": str(verified.record_count),
        "target_head_hash": verified.head_hash,
        "target_file_sha256": target_file_hash,
        "ledger_sequence": "",
        "previous_hash": "",
        "record_hash": "",
    }
    appended = append_events(checkpoint_path, [payload], maintain_checkpoint=False)
    return appended[0] if appended else dict(verify_hash_chain(checkpoint_path).records[-1])


def verify_ledger_checkpoint(event_ledger_path: Path) -> dict[str, Any]:
    verified = verify_hash_chain(event_ledger_path)
    checkpoint_path = checkpoint_path_for(event_ledger_path)
    if not checkpoint_path.exists():
        raise HashChainError(f"ledger head checkpoint is missing: {checkpoint_path}")
    checkpoint_chain = verify_hash_chain(checkpoint_path)
    if not checkpoint_chain.records:
        raise HashChainError(f"ledger head checkpoint is empty: {checkpoint_path}")
    previous_count = -1
    for item in checkpoint_chain.records:
        try:
            current_count = int(str(item.get("target_event_count", "")))
        except ValueError as exc:
            raise HashChainError("checkpoint target_event_count is invalid") from exc
        if current_count <= previous_count:
            raise HashChainError("checkpoint event counts are not strictly increasing")
        previous_count = current_count
    latest = dict(checkpoint_chain.records[-1])
    if int(str(latest.get("target_event_count", "-1"))) != verified.record_count:
        raise HashChainError("ledger event count rolled back or checkpoint is stale")
    if str(latest.get("target_head_hash", "")) != verified.head_hash:
        raise HashChainError("ledger head hash does not match latest checkpoint")
    target_file_hash = file_sha256(event_ledger_path)
    if str(latest.get("target_file_sha256", "")) != target_file_hash:
        raise HashChainError("ledger file fingerprint does not match latest checkpoint")
    return {
        "event_count": verified.record_count,
        "head_hash": verified.head_hash,
        "file_sha256": target_file_hash,
        "checkpoint_head_hash": checkpoint_chain.head_hash,
        "checkpoint_count": checkpoint_chain.record_count,
        "checkpoint_path": str(checkpoint_path),
    }


def persist_immutable_freezes(
    event_ledger_path: Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    freeze_kind: str,
    key_fields: Sequence[str],
    status_field: str,
    persistable_statuses: frozenset[str] = frozenset({"frozen_on_time", "late_backfill_excluded"}),
) -> tuple[list[dict[str, Any]], int]:
    """Append only the first observed freeze for each logical key.

    A later run may recompute the same logical key with a different timestamp or
    status.  The original event remains authoritative and the recomputation is
    ignored, which prevents an on-time freeze from becoming a late backfill.
    """

    if not freeze_kind.strip():
        raise ValueError("freeze_kind is required")
    if not key_fields:
        raise ValueError("at least one freeze key field is required")
    existing = read_events(event_ledger_path) if event_ledger_path.exists() else []
    if existing:
        verify_ledger_checkpoint(event_ledger_path)
    existing_ids = {str(item.get("event_id", "")) for item in existing}
    payloads: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        if str(row.get(status_field, "")) not in persistable_statuses:
            continue
        key_payload = {field: str(row.get(field, "")) for field in key_fields}
        if any(not value for value in key_payload.values()):
            continue
        event_id = f"{freeze_kind}:{json_fingerprint(key_payload)}"
        if event_id in existing_ids:
            continue
        row.update({
            "freeze_event_schema_version": "1.0",
            "event_type": freeze_kind,
            "event_id": event_id,
            "event_recorded_at_utc": str(row.get("freeze_at_utc", "")),
            "ledger_sequence": "",
            "previous_hash": "",
            "record_hash": "",
        })
        payloads.append(row)
        existing_ids.add(event_id)
    try:
        appended = append_events(event_ledger_path, payloads) if payloads else []
    except DuplicateRecordError:
        current = read_events(event_ledger_path) if event_ledger_path.exists() else []
        current_ids = {str(item.get("event_id", "")) for item in current}
        if not all(str(item.get("event_id", "")) in current_ids for item in payloads):
            raise
        appended = []
    return (read_events(event_ledger_path) if event_ledger_path.exists() else []), len(appended)


def snapshot_source(source: Path, snapshot_dir: Path) -> tuple[Path, str]:
    fingerprint = file_sha256(source)
    suffix = source.suffix.lower() or ".bin"
    target = snapshot_dir / f"{fingerprint}{suffix}"
    data = source.read_bytes()
    if target.exists() and target.read_bytes() != data:
        raise HashChainError(f"source snapshot collision: {target}")
    if not target.exists():
        atomic_write_bytes(target, data)
    return target, fingerprint


def relative_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def active_cohort_metadata(root: Path) -> dict[str, Any]:
    pointer = root / "logs" / "v5_31_fund_flow_evidence_freeze_active.json"
    if not pointer.exists():
        return {}
    value = json.loads(pointer.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


def event_chain_head(event_ledger_path: Path) -> str:
    return verify_hash_chain(event_ledger_path).head_hash


__all__ = [
    "EVENT_METADATA_FIELDS", "INTEGRITY_FIELDS", "LEDGER_FIELDS", "LEGACY_FIELDS",
    "SCHEMA_VERSION", "SETTLEMENT_MUTABLE_FIELDS", "SETTLEMENT_REQUIRED_NONEMPTY_FIELDS",
    "active_cohort_metadata", "append_events", "checkpoint_path_for", "entry_cutoff_utc",
    "event_chain_head", "evidence_cutoff_utc", "freeze_deadline_utc",
    "freeze_recorded_on_time", "freeze_timing_status", "freeze_window_start_utc", "is_true", "iso_utc",
    "legacy_observation_event", "logical_key", "logical_key_text",
    "materialize_observations", "migrate_legacy_csv", "observation_detected_on_time", "observation_timing_status",
    "parse_date", "parse_timestamp", "persist_immutable_freezes", "read_csv_rows", "read_events",
    "record_ledger_checkpoint", "relative_posix", "snapshot_source", "stable_observation_id",
    "utc_now", "verify_ledger_checkpoint", "with_schema_fields",
    "write_materialized_csv",
]
