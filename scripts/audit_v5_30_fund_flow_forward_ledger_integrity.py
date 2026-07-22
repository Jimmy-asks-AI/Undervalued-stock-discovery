#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from fund_flow_forward_evidence import (
    LEDGER_FIELDS,
    checkpoint_path_for,
    freeze_recorded_on_time,
    is_true,
    materialize_observations,
    observation_detected_on_time,
    parse_timestamp,
    read_events,
    stable_observation_id,
    verify_ledger_checkpoint,
    with_schema_fields,
)
from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from research_integrity import (
    AShareTradingCalendar,
    DuplicateRecordError,
    GENESIS_HASH,
    HashChainError,
    atomic_write_csv,
    atomic_write_json,
    atomic_write_text,
    csv_fingerprint,
    file_sha256,
    hash_chain_record,
    json_fingerprint,
    verify_hash_chain,
)


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv"
EVENT_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
ENTRY_FREEZE_LEDGER = ROOT / "logs" / "v5_33_fund_flow_entry_price_freeze.jsonl"
BENCHMARK_FREEZE_LEDGER = ROOT / "logs" / "v5_34_fund_flow_benchmark_entry_freeze.jsonl"
ENTRY_FREEZE = ROOT / "outputs" / "audit" / "fund_flow_entry_price_freeze_v5_33" / "debug" / "entry_price_freeze.csv"
BENCHMARK_ENTRY_FREEZE = ROOT / "outputs" / "audit" / "fund_flow_benchmark_entry_freeze_v5_34" / "debug" / "benchmark_entry_panel.csv"
OUT = ROOT / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30"
DEBUG = OUT / "debug"
LEDGER_SCHEMA = ROOT / "configs" / "fund_flow_forward_ledger_schema.json"

REQUIRED = [
    "record_schema_version", "event_type", "event_id", "observation_id", "record_hash",
    "batch_id", "policy_status", "outcome_status", "signal_date", "planned_entry_date",
    "planned_exit_date", "industry_code", "industry_name", "dual_positive_flow",
    "settlement_status", "realized_return", "benchmark_return", "realized_relative_return",
    "future_return_rank_pct", "future_top_quintile", "actual_entry_date", "actual_exit_date",
    "entry_price_freeze_status", "benchmark_entry_freeze_status", "detected_at_utc",
    "event_recorded_at_utc", "evidence_cutoff", "entry_cutoff", "source_artifact",
    "source_fingerprint", "source_fingerprint_status", "calendar_source",
    "calendar_fingerprint", "experiment_id", "cohort_id", "cohort_manifest_hash",
    "rule_id", "code_version", "late_backfill_excluded", "integrity_eligible",
    "promotion_eligible", "sample_scope", "qualified_for_goal",
]
FUTURE_FIELDS = ["realized_return", "benchmark_return", "realized_relative_return", "future_return_rank_pct", "future_top_quintile"]
SETTLED_REQUIRED_FIELDS = FUTURE_FIELDS + [
    "actual_entry_date", "actual_exit_date", "entry_price_freeze_status",
    "benchmark_entry_freeze_status", "entry_date_exact", "exit_date_exact",
    "benchmark_universe_count_used",
    "settlement_source_artifact", "settlement_source_fingerprint",
    "settlement_source_row_count", "settlement_calculation_version",
]


def authoritative_ledger_checks(
    rows: list[dict[str, Any]],
    as_of: date,
    *,
    observation_events: list[dict[str, Any]],
    entry_freeze_events: list[dict[str, Any]],
    benchmark_freeze_events: list[dict[str, Any]],
    errors: list[str],
    schema_events: list[dict[str, Any]] | None = None,
    materialized_match: bool = False,
    materialized_detail: str = "",
) -> list[dict[str, str]]:
    due = any((parse_date(row.get("planned_entry_date", "")) or date.max) <= as_of for row in rows)
    observation_shape_ok = observation_event_structure_valid(observation_events)
    candidate_shape_ok = freeze_event_structure_valid(
        entry_freeze_events,
        freeze_kind="candidate_entry_freeze",
        key_fields=["cohort_id", "cohort_manifest_hash", "batch_id", "observation_id", "industry_code", "planned_entry_date"],
    )
    benchmark_shape_ok = freeze_event_structure_valid(
        benchmark_freeze_events,
        freeze_kind="benchmark_entry_freeze",
        key_fields=["cohort_id", "cohort_manifest_hash", "batch_id", "planned_entry_date", "industry_code"],
    )
    schema_ok, schema_detail = event_schema_valid(schema_events if schema_events is not None else observation_events)
    return [
        checkpoint_check("observation_ledger_checkpoint", EVENT_LEDGER, required=bool(observation_events)),
        check("observation_event_structure", observation_shape_ok and not errors, f"events={len(observation_events)}; errors={errors}", "观察及结算事件必须具有确定性 ID、合法父事件和不可变状态转移。"),
        check("observation_event_schema", schema_ok, schema_detail, "2.1 新事件及结算事件必须执行冻结的 JSON Schema 契约。"),
        check("materialized_csv_matches_event_ledger", materialized_match, materialized_detail, "兼容 CSV 必须与权威 JSONL 的实时物化结果逐字段一致。"),
        checkpoint_check("candidate_freeze_ledger_checkpoint", ENTRY_FREEZE_LEDGER, required=due),
        check("candidate_freeze_event_structure", (not due and not entry_freeze_events) or candidate_shape_ok, f"due={due}; events={len(entry_freeze_events)}", "候选入场冻结必须来自追加式、确定性逻辑键事件。"),
        checkpoint_check("benchmark_freeze_ledger_checkpoint", BENCHMARK_FREEZE_LEDGER, required=due),
        check("benchmark_freeze_event_structure", (not due and not benchmark_freeze_events) or benchmark_shape_ok, f"due={due}; events={len(benchmark_freeze_events)}", "全行业基准冻结必须来自追加式、确定性逻辑键事件。"),
    ]


def checkpoint_check(name: str, path: Path, *, required: bool) -> dict[str, str]:
    if not required and not path.exists():
        return check(name, True, "not_due_and_absent", "尚未到冻结时点时允许冻结账本不存在。")
    try:
        result = verify_ledger_checkpoint(path)
        return check(name, True, f"events={result['event_count']}; head={result['head_hash']}; checkpoint_head={result['checkpoint_head_hash']}", "权威账本头必须与独立追加式 checkpoint 一致。")
    except (HashChainError, OSError) as exc:
        return check(name, False, str(exc), "权威账本必须有可验证的独立头 checkpoint，合法前缀回滚也应被识别。")


def observation_event_structure_valid(events: list[dict[str, Any]]) -> bool:
    observations: dict[str, dict[str, Any]] = {}
    settlements: set[str] = set()
    for event in events:
        event_type = str(event.get("event_type", ""))
        observation_id = str(event.get("observation_id", ""))
        if event_type == "observation":
            if not observation_id or observation_id in observations:
                return False
            if observation_id != stable_observation_id(event) or str(event.get("event_id", "")) != f"{observation_id}:observation":
                return False
            observations[observation_id] = event
        elif event_type == "settlement":
            parent = observations.get(observation_id)
            if parent is None or observation_id in settlements:
                return False
            expected_id = f"{observation_id}:settlement:{parent.get('planned_exit_date', '')}"
            if str(event.get("event_id", "")) != expected_id or str(event.get("parent_event_id", "")) != str(parent.get("event_id", "")):
                return False
            parent_time = parse_timestamp(parent.get("event_recorded_at_utc"))
            event_time = parse_timestamp(event.get("event_recorded_at_utc"))
            exit_day = parse_date(parent.get("planned_exit_date", ""))
            exit_close = datetime.combine(exit_day, time(15, 0), tzinfo=_shanghai_zone()).astimezone(_utc_zone()) if exit_day else None
            if not parent_time or not event_time or event_time < parent_time or not exit_close or event_time < exit_close:
                return False
            settlements.add(observation_id)
        else:
            return False
    try:
        materialize_observations(events)
    except (HashChainError, DuplicateRecordError):
        return False
    return bool(observations)


def freeze_event_structure_valid(events: list[dict[str, Any]], *, freeze_kind: str, key_fields: list[str]) -> bool:
    if not events:
        return False
    seen: set[str] = set()
    for event in events:
        key_payload = {field: str(event.get(field, "")) for field in key_fields}
        expected = f"{freeze_kind}:{json_fingerprint(key_payload)}"
        event_id = str(event.get("event_id", ""))
        if (
            any(not value for value in key_payload.values())
            or str(event.get("event_type", "")) != freeze_kind
            or event_id != expected
            or event_id in seen
            or str(event.get("event_recorded_at_utc", "")) != str(event.get("freeze_at_utc", ""))
        ):
            return False
        seen.add(event_id)
    return True


def event_schema_valid(events: list[dict[str, Any]]) -> tuple[bool, str]:
    if not LEDGER_SCHEMA.is_file():
        return False, f"schema_missing={LEDGER_SCHEMA}"
    try:
        schema = json.loads(LEDGER_SCHEMA.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"schema_unreadable={exc}"
    failures: list[str] = []
    for index, event in enumerate(events, start=1):
        errors = validate_schema_subset(event, schema)
        if errors:
            failures.append(f"event={index}:{'|'.join(errors)}")
    return not failures, f"schema={schema.get('$id', '')}; events={len(events)}; failures={failures}"


def validate_schema_subset(event: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    """Execute the frozen schema features used by this ledger without a third-party dependency."""
    errors: list[str] = []
    for field in schema.get("required", []):
        if field not in event:
            errors.append(f"missing:{field}")
    for field, rule in schema.get("properties", {}).items():
        if field not in event:
            continue
        value = event.get(field)
        allowed_types = rule.get("type")
        if allowed_types:
            choices = allowed_types if isinstance(allowed_types, list) else [allowed_types]
            if not any(schema_type_matches(value, choice) for choice in choices):
                errors.append(f"type:{field}")
                continue
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"enum:{field}")
        if isinstance(value, str) and len(value) < int(rule.get("minLength", 0)):
            errors.append(f"minLength:{field}")
        if isinstance(value, str) and rule.get("pattern") and re.fullmatch(str(rule["pattern"]), value) is None:
            errors.append(f"pattern:{field}")
        if rule.get("format") == "date-time" and parse_timestamp(value) is None:
            errors.append(f"date-time:{field}")
    for clause in schema.get("allOf", []):
        condition = clause.get("if", {}).get("properties", {})
        matches = all(event.get(field) == rule.get("const") for field, rule in condition.items())
        if matches:
            for field in clause.get("then", {}).get("required", []):
                if field not in event or str(event.get(field, "")).strip() == "":
                    errors.append(f"conditional-required:{field}")
            for field, rule in clause.get("then", {}).get("properties", {}).items():
                value = event.get(field)
                if "const" in rule and value != rule["const"]:
                    errors.append(f"conditional-const:{field}")
                if isinstance(value, str) and len(value) < int(rule.get("minLength", 0)):
                    errors.append(f"conditional-minLength:{field}")
                if isinstance(value, str) and rule.get("pattern") and re.fullmatch(str(rule["pattern"]), value) is None:
                    errors.append(f"conditional-pattern:{field}")
    return errors


def schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def materialized_csv_consistency(rows: list[dict[str, Any]], path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"materialized_csv_missing={path}"
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            actual = list(reader)
            columns = list(reader.fieldnames or [])
        expected = [with_schema_fields(row) for row in rows]
        expected_hash = csv_fingerprint(expected, fieldnames=LEDGER_FIELDS)
        actual_hash = csv_fingerprint(actual, fieldnames=LEDGER_FIELDS) if columns == LEDGER_FIELDS else "column_mismatch"
        matched = columns == LEDGER_FIELDS and expected_hash == actual_hash
        return matched, f"rows_expected={len(expected)}; rows_actual={len(actual)}; expected={expected_hash}; actual={actual_hash}"
    except (OSError, TypeError, ValueError, csv.Error) as exc:
        return False, f"materialized_csv_error={exc}"


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.30 integrity audit for the append-only fund-flow forward ledger.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    as_of = datetime.fromisoformat(args.as_of_date).date()
    if as_of > date.today():
        parser.error(f"--as-of-date {args.as_of_date} is in the future; integrity audit must use current or past dates.")

    materialization_errors: list[str] = []
    try:
        events = read_events(EVENT_LEDGER) if EVENT_LEDGER.exists() else []
        rows = materialize_observations(events) if events else []
    except (HashChainError, DuplicateRecordError) as exc:
        events, rows = [], []
        materialization_errors.append(f"observation ledger: {exc}")
    try:
        entry_freeze_events = read_events(ENTRY_FREEZE_LEDGER) if ENTRY_FREEZE_LEDGER.exists() else []
    except HashChainError as exc:
        entry_freeze_events = []
        materialization_errors.append(f"candidate freeze ledger: {exc}")
    try:
        benchmark_freeze_events = read_events(BENCHMARK_FREEZE_LEDGER) if BENCHMARK_FREEZE_LEDGER.exists() else []
    except HashChainError as exc:
        benchmark_freeze_events = []
        materialization_errors.append(f"benchmark freeze ledger: {exc}")
    entry_freeze = pd.DataFrame(entry_freeze_events)
    benchmark_freeze = pd.DataFrame(benchmark_freeze_events)
    active = validated_active_cohort()
    active_id = str(active.get("cohort_id", ""))
    active_hash = str(active.get("manifest_hash", ""))
    active_rows = [row for row in rows if belongs_to_cohort(row, active_id, active_hash)]
    active_events = [row for row in events if belongs_to_cohort(row, active_id, active_hash)]
    active_entry = filter_frame_by_cohort(entry_freeze, active_id, active_hash)
    active_benchmark = filter_frame_by_cohort(benchmark_freeze, active_id, active_hash)
    materialized_match, materialized_detail = materialized_csv_consistency(rows, LEDGER)
    evidence_manifest_hash, evidence_artifact_count = evidence_artifact_manifest(active_rows, active_entry, active_benchmark, ROOT)

    checks, violations = audit_rows(
        active_rows,
        as_of,
        active_entry,
        active_benchmark,
        events=events,
        root=ROOT,
        cohort_created_at=active.get("created_at_utc", ""),
    )
    active_freeze_ok = bool(active_id and valid_sha256(active_hash) and active.get("freeze_passed") is True)
    checks = pd.concat([
        checks,
        pd.DataFrame([
            check(
            "active_cohort_freeze_verified",
            active_freeze_ok,
            f"cohort_id={active_id or 'missing'}; manifest_hash={active_hash or 'missing'}; freeze_passed={active.get('freeze_passed', False)}",
            "只有已二次核验且不可变的当前 cohort 才能进入完整性与晋级门禁。",
            ),
            *authoritative_ledger_checks(
                active_rows,
                as_of,
                observation_events=events,
                entry_freeze_events=entry_freeze_events,
                benchmark_freeze_events=benchmark_freeze_events,
                errors=materialization_errors,
                schema_events=active_events,
                materialized_match=materialized_match,
                materialized_detail=materialized_detail,
            ),
        ]),
    ], ignore_index=True)

    global_checks, global_violations = audit_rows(rows, as_of, entry_freeze, benchmark_freeze, events=events, root=ROOT)
    global_checks = pd.concat([
        global_checks,
        pd.DataFrame(authoritative_ledger_checks(
            rows,
            as_of,
            observation_events=events,
            entry_freeze_events=entry_freeze_events,
            benchmark_freeze_events=benchmark_freeze_events,
            errors=materialization_errors,
            schema_events=events,
            materialized_match=materialized_match,
            materialized_detail=materialized_detail,
        )),
    ], ignore_index=True)
    for message in materialization_errors:
        violations.append(v(0, "ledger", "authoritative_ledger_materialization_failed", message))
        global_violations.append(v(0, "ledger", "authoritative_ledger_materialization_failed", message))
    summary = build_summary(
        active_rows,
        checks,
        violations,
        as_of,
        events=events,
        active_cohort=active,
        global_rows=rows,
        global_checks=global_checks,
        global_violations=global_violations,
        input_fingerprints={
            "ledger_event_file_sha256": fingerprint_or_missing(EVENT_LEDGER),
            "materialized_ledger_file_sha256": fingerprint_or_missing(LEDGER),
            "entry_freeze_file_sha256": fingerprint_or_missing(ENTRY_FREEZE),
            "benchmark_freeze_file_sha256": fingerprint_or_missing(BENCHMARK_ENTRY_FREEZE),
            "candidate_freeze_event_file_sha256": fingerprint_or_missing(ENTRY_FREEZE_LEDGER),
            "benchmark_freeze_event_file_sha256": fingerprint_or_missing(BENCHMARK_FREEZE_LEDGER),
            "observation_checkpoint_file_sha256": fingerprint_or_missing(checkpoint_path_for(EVENT_LEDGER)),
            "candidate_freeze_checkpoint_file_sha256": fingerprint_or_missing(checkpoint_path_for(ENTRY_FREEZE_LEDGER)),
            "benchmark_freeze_checkpoint_file_sha256": fingerprint_or_missing(checkpoint_path_for(BENCHMARK_FREEZE_LEDGER)),
            "evidence_source_manifest_sha256": evidence_manifest_hash,
            "evidence_source_artifact_count": evidence_artifact_count,
        },
    )
    write_outputs(
        summary,
        checks,
        active_rows,
        violations,
        global_checks=global_checks,
        global_rows=rows,
        global_violations=global_violations,
    )
    print(f"output_dir={OUT}")
    print(f"integrity_passed={summary['integrity_passed']}")
    print(f"violation_count={summary['violation_count']}")
    if not summary["integrity_passed"]:
        raise SystemExit(2)


def audit_rows(
    rows: list[dict[str, Any]],
    as_of: date,
    entry_freeze: pd.DataFrame | None = None,
    benchmark_freeze: pd.DataFrame | None = None,
    *,
    events: Iterable[Mapping[str, Any]] | None = None,
    root: Path | None = None,
    allow_fixture_fingerprints: bool = False,
    cohort_created_at: Any = None,
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    violations: list[dict[str, str]] = []
    columns = set(rows[0]) if rows else set()
    duplicate_keys = duplicates([(str(row.get("batch_id", "")), str(row.get("industry_code", "")).zfill(6)) for row in rows])
    entry_frame = (entry_freeze if entry_freeze is not None else pd.DataFrame()).fillna("")
    benchmark_frame = (benchmark_freeze if benchmark_freeze is not None else pd.DataFrame()).fillna("")
    entry_by_key = {freeze_key(row): row for row in entry_frame.to_dict("records")}
    benchmark_groups = grouped_benchmark_freeze(benchmark_frame)

    event_rows = [dict(item) for item in (events or [])]
    chain_ok = False
    chain_head = ""
    try:
        if not event_rows:
            raise HashChainError("authoritative event ledger is missing")
        verified = verify_hash_chain(event_rows)
        chain_ok = True
        chain_head = verified.head_hash
    except HashChainError as exc:
        violations.append(v(0, "ledger", "ledger_hash_chain_invalid", str(exc)))

    checks = [
        check("ledger_exists", bool(rows), f"observations={len(rows)}; events={len(event_rows)}", "前推账本及权威事件链必须存在。"),
        check("required_columns", set(REQUIRED).issubset(columns), f"missing={sorted(set(REQUIRED) - columns)}", "账本字段必须足以复核时间、来源、cohort 与未来收益。"),
        check("unique_batch_industry", not duplicate_keys, f"duplicates={duplicate_keys}", "同一观察批次同一行业只能有一个 observation 事件。"),
        check("ledger_hash_chain", chain_ok, f"events={len(event_rows)}; head={chain_head}", "事件账本必须从创世哈希到当前头完整可验。"),
    ]

    due_entry_rows = 0
    candidate_freeze_hits = 0
    benchmark_freeze_hits = 0
    source_hits = 0
    qualification_hits = 0
    calendar_schedule_hits = 0
    cohort_time_hits = 0
    time_hits = 0
    late_count = 0
    as_of_cutoff = datetime.combine(as_of, time(23, 59, 59), tzinfo=_shanghai_zone()).astimezone(_utc_zone())

    for idx, row in enumerate(rows, start=2):
        key = f"{row.get('batch_id', '')}|{str(row.get('industry_code', '')).zfill(6)}"
        if str(row.get("observation_id", "")) != stable_observation_id(row):
            violations.append(v(idx, key, "unstable_observation_id", "observation_id 必须由批次、行业、信号日、入场日和退出日确定性生成"))
        signal = parse_date(row.get("signal_date", ""))
        entry = parse_date(row.get("planned_entry_date", ""))
        exit_ = parse_date(row.get("planned_exit_date", ""))
        if not signal or not entry or not exit_ or not (signal < entry < exit_):
            violations.append(v(idx, key, "bad_date_chain", "要求 signal_date < planned_entry_date < planned_exit_date"))
        if row.get("policy_status") != "research_only":
            violations.append(v(idx, key, "policy_status_not_research_only", "资金流前推仍必须保持 research_only"))
        if not is_true(row.get("dual_positive_flow")):
            violations.append(v(idx, key, "not_dual_positive", "V5.25 账本只允许资金流双正观察"))

        detected = parse_timestamp(row.get("detected_at_utc"))
        recorded = parse_timestamp(row.get("event_recorded_at_utc"))
        entry_cutoff = parse_timestamp(row.get("entry_cutoff"))
        time_ok = (
            observation_detected_on_time(row)
            and detected is not None
            and recorded is not None
            and entry_cutoff is not None
            and recorded >= detected
            and recorded < entry_cutoff
            and recorded <= as_of_cutoff
        )
        if time_ok:
            time_hits += 1
        else:
            violations.append(v(idx, key, "observation_timestamp_inversion", "要求 evidence_cutoff <= detected_at_utc < entry_cutoff，且 event_recorded_at_utc 不早于检测、不晚于 as-of"))
        if is_true(row.get("late_backfill_excluded")) or not observation_detected_on_time(row):
            late_count += 1
            violations.append(v(idx, key, "late_backfill_observation", "入场截止后登记的观察永久排除，不得通过完整性门禁"))

        cohort_time_ok = cohort_creation_precedes_evidence(row, cohort_created_at)
        if cohort_time_ok:
            cohort_time_hits += 1
        else:
            violations.append(v(idx, key, "retroactive_cohort_ownership", "不可变 cohort 创建时点必须不晚于该信号的 evidence_cutoff"))

        source_ok = source_fingerprint_valid(row, root, allow_fixture_fingerprints=allow_fixture_fingerprints)
        calendar_ok = calendar_fingerprint_valid(row, root, allow_fixture_fingerprints=allow_fixture_fingerprints)
        if source_ok and calendar_ok:
            source_hits += 1
        else:
            violations.append(v(idx, key, "source_fingerprint_unverifiable", f"source_ok={source_ok}; calendar_ok={calendar_ok}"))
        qualification_ok, qualification_reason = qualification_flags_consistent(
            row,
            source_ok=source_ok,
            calendar_ok=calendar_ok,
            cohort_created_at=cohort_created_at,
        )
        if qualification_ok:
            qualification_hits += 1
        else:
            violations.append(v(idx, key, "qualification_flags_inconsistent", qualification_reason))
        schedule_ok = calendar_schedule_valid(row, root, allow_fixture_fingerprints=allow_fixture_fingerprints)
        if schedule_ok:
            calendar_schedule_hits += 1
        else:
            violations.append(v(idx, key, "calendar_schedule_mismatch", "计划入场日必须是信号日后一交易日，退出日必须是入场后第 20 个 A 股交易会话"))

        if is_true(row.get("qualified_for_goal")):
            if not all(is_true(row.get(field)) for field in ["integrity_eligible", "promotion_eligible"]):
                violations.append(v(idx, key, "qualified_without_integrity_eligibility", "qualified_for_goal 必须同时满足 integrity_eligible 与 promotion_eligible"))
            if not valid_sha256(row.get("cohort_manifest_hash")) or not str(row.get("cohort_id", "")).strip():
                violations.append(v(idx, key, "qualified_missing_cohort_hash", "合格样本必须绑定不可变 cohort 及 manifest hash"))

        if exit_ and exit_ > as_of and any(str(row.get(field, "")).strip() for field in FUTURE_FIELDS):
            violations.append(v(idx, key, "future_fields_filled_before_exit", "未到退出日不得填写未来收益字段"))
        if row.get("settlement_status") == "settled":
            if any(not str(row.get(field, "")).strip() for field in SETTLED_REQUIRED_FIELDS):
                violations.append(v(idx, key, "settled_missing_future_fields", "已结算行必须填齐收益、精确日期、冻结状态和基准数量"))
            if str(row.get("actual_entry_date", "")) != str(row.get("planned_entry_date", "")) or str(row.get("actual_exit_date", "")) != str(row.get("planned_exit_date", "")):
                violations.append(v(idx, key, "settled_date_not_exact", "结算实际入退场日期必须与计划日期精确相等"))
            try:
                used = int(float(str(row.get("benchmark_universe_count_used", "0"))))
            except ValueError:
                used = 0
            if used < 100:
                violations.append(v(idx, key, "settled_benchmark_below_100", f"benchmark_universe_count_used={used}"))
            values_ok, values_reason = settlement_values_valid(
                row,
                root,
                candidate_freeze=entry_by_key.get(freeze_key(row)),
                benchmark_group=benchmark_groups.get(benchmark_key(row), []),
            )
            if not values_ok:
                violations.append(v(idx, key, "settlement_values_not_reproducible", values_reason))

        if entry and entry <= as_of:
            due_entry_rows += 1
            candidate = entry_by_key.get(freeze_key(row))
            candidate_ok, candidate_reason = candidate_freeze_valid(
                row, candidate, as_of_cutoff, root, allow_fixture_fingerprints=allow_fixture_fingerprints
            )
            if candidate_ok:
                candidate_freeze_hits += 1
            else:
                if candidate and candidate.get("entry_price_freeze_status") == "late_backfill_excluded":
                    late_count += 1
                    violation = "late_candidate_entry_freeze"
                else:
                    violation = "missing_or_invalid_candidate_entry_freeze"
                violations.append(v(idx, key, violation, candidate_reason))

            group = benchmark_groups.get(benchmark_key(row), [])
            benchmark_ok, benchmark_reason, benchmark_count = benchmark_freeze_valid(
                row, group, as_of_cutoff, root, allow_fixture_fingerprints=allow_fixture_fingerprints
            )
            if benchmark_ok:
                benchmark_freeze_hits += 1
            else:
                if any(item.get("benchmark_entry_freeze_status") == "late_backfill_excluded" for item in group):
                    late_count += 1
                    violation = "late_benchmark_entry_freeze"
                else:
                    violation = "missing_or_invalid_benchmark_entry_freeze"
                violations.append(v(idx, key, violation, f"{benchmark_reason}; valid_count={benchmark_count}"))
            if candidate_ok and benchmark_ok and not candidate_benchmark_entry_consistent(row, candidate, group):
                candidate_freeze_hits -= 1
                benchmark_freeze_hits -= 1
                violations.append(v(idx, key, "candidate_benchmark_entry_price_mismatch", "候选冻结价必须与同批基准面板中的候选入场价一致"))

    checks.extend([
        check("observation_timestamp_integrity", len(rows) == time_hits, f"rows={len(rows)}; valid={time_hits}", "检测、证据截止、入场截止和 as-of 时间必须单调。"),
        check("cohort_creation_precedes_evidence", len(rows) == cohort_time_hits, f"rows={len(rows)}; valid={cohort_time_hits}", "当前 cohort 必须在信号证据截止前已经写入不可变历史，禁止追认旧信号。"),
        check("source_fingerprint_integrity", len(rows) == source_hits, f"rows={len(rows)}; valid={source_hits}", "观察源与交易日历快照指纹必须可复核。"),
        check("qualification_flags_consistent", len(rows) == qualification_hits, f"rows={len(rows)}; valid={qualification_hits}", "资格标志必须由冻结源门禁、时点、来源和 cohort 独立重算。"),
        check("calendar_schedule_integrity", len(rows) == calendar_schedule_hits, f"rows={len(rows)}; valid={calendar_schedule_hits}", "必须用冻结的 A 股交易日历重算下一交易日与 20 会话退出日。"),
        check("candidate_entry_freeze_coverage", due_entry_rows == candidate_freeze_hits, f"due={due_entry_rows}; valid={candidate_freeze_hits}", "到达入场日的观察必须有按时且精确的候选价冻结。"),
        check("benchmark_entry_freeze_coverage", due_entry_rows == benchmark_freeze_hits, f"due={due_entry_rows}; valid={benchmark_freeze_hits}", "每批基准必须按时、精确、至少 100 行并包含候选。"),
        check("late_backfill_absent", late_count == 0, f"late_backfill_count={late_count}", "任何观察或冻结回填都会阻断完整性。"),
        check("row_level_violations", not violations, f"violations={len(violations)}", "行级日期、来源、cohort、未来收益和冻结覆盖检查。"),
    ])
    return pd.DataFrame(checks), violations


def source_fingerprint_valid(row: Mapping[str, Any], root: Path | None, *, allow_fixture_fingerprints: bool = False) -> bool:
    status = str(row.get("source_fingerprint_status", ""))
    fingerprint = str(row.get("source_fingerprint", ""))
    if status == "verified_fixture":
        return allow_fixture_fingerprints and valid_sha256(fingerprint)
    if status == "verified_bundle":
        return bool(root and observation_source_bundle_valid(row, root))
    if status != "verified_snapshot" or str(row.get("record_schema_version", "")) != "2.0" or not root or not valid_sha256(fingerprint):
        return False
    path = workspace_artifact(root, row.get("source_artifact", ""))
    if path is None:
        return False
    return path.is_file() and file_sha256(path) == fingerprint


def qualification_flags_consistent(
    row: Mapping[str, Any],
    *,
    source_ok: bool,
    calendar_ok: bool,
    cohort_created_at: Any = None,
) -> tuple[bool, str]:
    base_qualified = (
        all(is_true(row.get(field)) for field in ["window_signal_pass", "valuation_gate_pass", "stabilization_gate_pass"])
        and bool(normalized_scalar(row.get("window_id")))
        and bool(normalized_scalar(row.get("frozen_selection_rule_id")))
    )
    cohort_verified = bool(str(row.get("cohort_id", "")).strip() and valid_sha256(row.get("cohort_manifest_hash")))
    cohort_time_ok = cohort_creation_precedes_evidence(row, cohort_created_at)
    expected_integrity = bool(observation_detected_on_time(row) and source_ok and calendar_ok and cohort_verified and cohort_time_ok)
    expected_qualified = bool(base_qualified and expected_integrity)
    actual = {
        "integrity_eligible": is_true(row.get("integrity_eligible")),
        "qualified_for_goal": is_true(row.get("qualified_for_goal")),
        "promotion_eligible": is_true(row.get("promotion_eligible")),
        "goal_scope": str(row.get("sample_scope", "")) == "goal_qualified",
    }
    expected = {
        "integrity_eligible": expected_integrity,
        "qualified_for_goal": expected_qualified,
        "promotion_eligible": expected_qualified,
        "goal_scope": expected_qualified,
    }
    return actual == expected, f"expected={expected}; actual={actual}; base_qualified={base_qualified}; cohort_time_ok={cohort_time_ok}"


def cohort_creation_precedes_evidence(row: Mapping[str, Any], cohort_created_at: Any = None) -> bool:
    """Require immutable cohort creation no later than the signal evidence cutoff.

    ``None`` means the caller is auditing global historical rows whose cohort
    history is intentionally out of scope. Production active-cohort audits pass
    the canonical timestamp returned by ``validated_active_cohort``; an empty or
    malformed timestamp then fails closed.
    """

    if cohort_created_at is None:
        return True
    created_at = parse_timestamp(cohort_created_at)
    evidence_cutoff = parse_timestamp(row.get("evidence_cutoff", ""))
    return bool(created_at and evidence_cutoff and created_at <= evidence_cutoff)


def observation_source_bundle_valid(row: Mapping[str, Any], root: Path) -> bool:
    manifest_path = workspace_artifact(root, row.get("source_artifact", ""))
    if manifest_path is None or not manifest_path.is_file() or file_sha256(manifest_path) != str(row.get("source_fingerprint", "")):
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if manifest.get("bundle_version") != "fund_flow_observation_source_bundle_v1":
            return False
        signal_date = str(row.get("signal_date", ""))
        if str(manifest.get("signal_date", "")) != signal_date:
            return False
        candidate_path = workspace_artifact(root, manifest.get("candidate_artifact", ""))
        summary_path = workspace_artifact(root, manifest.get("summary_artifact", ""))
        if candidate_path is None or summary_path is None or not candidate_path.is_file() or not summary_path.is_file():
            return False
        if file_sha256(candidate_path) != str(manifest.get("candidate_fingerprint", "")):
            return False
        if file_sha256(summary_path) != str(manifest.get("summary_fingerprint", "")):
            return False
        summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
        if str(summary.get("latest_cache_date", "")) != signal_date:
            return False
        candidates = pd.read_csv(candidate_path, encoding="utf-8-sig", dtype={"industry_code": str}).fillna("")
        if len(candidates) != int(manifest.get("candidate_row_count", -1)) or "industry_code" not in candidates.columns:
            return False
        candidates["industry_code"] = candidates["industry_code"].astype(str).str.zfill(6)
        selected = candidates[candidates["industry_code"].eq(str(row.get("industry_code", "")).zfill(6))]
        if len(selected) != 1:
            return False
        source_row = selected.iloc[0].to_dict()
        boolean_fields = [
            "dual_positive_flow", "today_flow_positive", "five_day_flow_positive",
            "window_signal_pass", "valuation_gate_pass", "stabilization_gate_pass",
        ]
        scalar_fields = [
            "industry_name", "selection_score", "fund_flow_research_status",
            "fund_flow_overlay_status", "ths_industry_name", "ths_today_net_flow",
            "ths_5d_net_flow", "historical_failure_flag", "window_id",
            "frozen_selection_rule_id",
        ]
        if any(is_true(source_row.get(field)) != is_true(row.get(field)) for field in boolean_fields):
            return False
        if any(normalized_scalar(source_row.get(field)) != normalized_scalar(row.get(field)) for field in scalar_fields):
            return False
        source_base_qualified = (
            all(is_true(source_row.get(field)) for field in ["window_signal_pass", "valuation_gate_pass", "stabilization_gate_pass"])
            and bool(normalized_scalar(source_row.get("window_id")))
            and bool(normalized_scalar(source_row.get("frozen_selection_rule_id")))
        )
        if source_base_qualified != all(
            [
                is_true(row.get("window_signal_pass")),
                is_true(row.get("valuation_gate_pass")),
                is_true(row.get("stabilization_gate_pass")),
                bool(normalized_scalar(row.get("window_id"))),
                bool(normalized_scalar(row.get("frozen_selection_rule_id"))),
            ]
        ):
            return False
        return is_true(source_row.get("dual_positive_flow"))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, pd.errors.ParserError):
        return False


def normalized_scalar(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return "" if text.lower() in {"nan", "none"} else text


def calendar_fingerprint_valid(row: Mapping[str, Any], root: Path | None, *, allow_fixture_fingerprints: bool = False) -> bool:
    fingerprint = str(row.get("calendar_fingerprint", ""))
    source = str(row.get("calendar_source", ""))
    if source == "self_check_fixture":
        return allow_fixture_fingerprints and valid_sha256(fingerprint)
    if not root or not valid_sha256(fingerprint):
        return False
    path = workspace_artifact(root, source)
    if path is None:
        return False
    return path.is_file() and file_sha256(path) == fingerprint


def calendar_schedule_valid(row: Mapping[str, Any], root: Path | None, *, allow_fixture_fingerprints: bool = False) -> bool:
    source = str(row.get("calendar_source", ""))
    if source == "self_check_fixture":
        return allow_fixture_fingerprints
    if not root:
        return False
    signal = parse_date(row.get("signal_date", ""))
    entry = parse_date(row.get("planned_entry_date", ""))
    exit_ = parse_date(row.get("planned_exit_date", ""))
    path = workspace_artifact(root, source)
    if not signal or not entry or not exit_ or path is None or not path.is_file():
        return False
    try:
        calendar = AShareTradingCalendar.from_csv(path)
        return calendar.is_trading_day(signal) and calendar.next_trading_day(signal) == entry and calendar.holding_exit(entry, 20) == exit_
    except (ValueError, OSError):
        return False


def candidate_freeze_valid(
    row: Mapping[str, Any],
    freeze: Mapping[str, Any] | None,
    as_of_cutoff: datetime,
    root: Path | None = None,
    *,
    allow_fixture_fingerprints: bool = False,
) -> tuple[bool, str]:
    if not freeze:
        return False, "candidate freeze row missing"
    if freeze.get("entry_price_freeze_status") != "frozen_on_time" or not freeze_recorded_on_time(freeze):
        return False, f"status={freeze.get('entry_price_freeze_status')}"
    if str(freeze.get("actual_entry_date", "")) != str(row.get("planned_entry_date", "")):
        return False, "candidate actual_entry_date is not exact"
    frozen_at = parse_timestamp(freeze.get("freeze_at_utc"))
    if not frozen_at or frozen_at > as_of_cutoff:
        return False, "candidate freeze_at_utc is after audit as-of"
    if not cohort_matches(row, freeze):
        return False, "candidate freeze cohort/hash mismatch"
    if allow_fixture_fingerprints and str(freeze.get("freeze_source", "")) == "fixture":
        payload = {
            "industry_code": str(freeze.get("industry_code", "")).zfill(6),
            "actual_entry_date": str(freeze.get("actual_entry_date", "")),
            "entry_close_index": str(freeze.get("entry_close_index", "")),
            "freeze_source": str(freeze.get("freeze_source", "")),
        }
        return (json_fingerprint(payload) == str(freeze.get("source_fingerprint", "")), "fixture_source_reproduced")
    if root is None or not price_source_contains_freeze(freeze, root):
        return False, "candidate immutable source snapshot does not reproduce the frozen price"
    return True, "frozen_on_time_exact_and_source_reproduced"


def benchmark_freeze_valid(
    row: Mapping[str, Any],
    group: list[dict[str, Any]],
    as_of_cutoff: datetime,
    root: Path | None = None,
    *,
    allow_fixture_fingerprints: bool = False,
) -> tuple[bool, str, int]:
    if not group:
        return False, "benchmark freeze group missing", 0
    valid: list[dict[str, Any]] = []
    for item in group:
        frozen_at = parse_timestamp(item.get("freeze_at_utc"))
        if (
            item.get("benchmark_entry_freeze_status") == "frozen_on_time"
            and freeze_recorded_on_time(item)
            and str(item.get("actual_entry_date", "")) == str(row.get("planned_entry_date", ""))
            and frozen_at is not None
            and frozen_at <= as_of_cutoff
            and cohort_matches(row, item)
        ):
            valid.append(item)
    unique = {str(item.get("industry_code", "")).zfill(6): item for item in valid}
    if len(unique) < 100:
        return False, "benchmark has fewer than 100 valid industries", len(unique)
    selected = str(row.get("industry_code", "")).zfill(6)
    if selected not in unique:
        return False, "benchmark does not contain selected industry", len(unique)
    payload_rows = [
        {
            "industry_code": code,
            "actual_entry_date": str(item.get("actual_entry_date", "")),
            "entry_close_index": str(item.get("entry_close_index", "")),
        }
        for code, item in sorted(unique.items())
    ]
    computed = csv_fingerprint(payload_rows, fieldnames=["industry_code", "actual_entry_date", "entry_close_index"], sort_rows_by=["industry_code"])
    fingerprints = {str(item.get("benchmark_universe_fingerprint", "")) for item in valid}
    if fingerprints != {computed}:
        return False, "benchmark universe fingerprint mismatch", len(unique)
    if allow_fixture_fingerprints and not any(str(item.get("freeze_source", "")) for item in valid):
        return True, "fixture_exact_count_and_fingerprint", len(unique)
    if root is None or not benchmark_source_reproduces_group(valid, root):
        return False, "benchmark immutable source snapshot does not reproduce the frozen universe", len(unique)
    return True, "frozen_on_time_exact_count_and_source_reproduced", len(unique)


def read_price_source(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    close_field = "close_index" if "close_index" in frame.columns else "industry_close" if "industry_close" in frame.columns else ""
    if not close_field or not {"trade_date", "industry_code"}.issubset(frame.columns):
        raise ValueError("price source is missing trade_date, industry_code, or close column")
    out = pd.DataFrame({
        "trade_date": pd.to_datetime(frame["trade_date"], errors="raise").dt.date,
        "industry_code": frame["industry_code"].astype(str).str.zfill(6),
        "entry_close_index": pd.to_numeric(frame[close_field], errors="raise"),
    })
    return out.drop_duplicates(["trade_date", "industry_code"], keep="first")


def price_source_contains_freeze(freeze: Mapping[str, Any], root: Path, tolerance: float = 5e-9) -> bool:
    artifact = workspace_artifact(root, freeze.get("freeze_source", ""))
    fingerprint = str(freeze.get("source_fingerprint", ""))
    if artifact is None or not artifact.is_file() or not valid_sha256(fingerprint) or file_sha256(artifact) != fingerprint:
        return False
    try:
        planned = parse_date(freeze.get("planned_entry_date", ""))
        source = read_price_source(artifact)
        rows = source[
            source["trade_date"].eq(planned)
            & source["industry_code"].eq(str(freeze.get("industry_code", "")).zfill(6))
        ]
        return len(rows) == 1 and abs(float(rows.iloc[0]["entry_close_index"]) - float(freeze.get("entry_close_index", "nan"))) <= tolerance
    except (OSError, ValueError, TypeError, pd.errors.ParserError):
        return False


def benchmark_source_reproduces_group(group: list[dict[str, Any]], root: Path, tolerance: float = 5e-9) -> bool:
    references = {(str(item.get("freeze_source", "")), str(item.get("source_fingerprint", ""))) for item in group}
    if len(references) != 1:
        return False
    source_text, fingerprint = next(iter(references))
    artifact = workspace_artifact(root, source_text)
    if artifact is None or not artifact.is_file() or not valid_sha256(fingerprint) or file_sha256(artifact) != fingerprint:
        return False
    try:
        planned_values = {str(item.get("planned_entry_date", "")) for item in group}
        if len(planned_values) != 1:
            return False
        planned = parse_date(next(iter(planned_values)))
        source = read_price_source(artifact)
        source = source[source["trade_date"].eq(planned)].sort_values("industry_code").drop_duplicates("industry_code", keep="first")
        authoritative = {
            str(item.get("industry_code", "")).zfill(6): float(item.get("entry_close_index", "nan"))
            for item in group
        }
        reproduced = dict(zip(source["industry_code"], source["entry_close_index"]))
        return set(authoritative) == set(reproduced) and all(abs(authoritative[code] - float(reproduced[code])) <= tolerance for code in authoritative)
    except (OSError, ValueError, TypeError, pd.errors.ParserError):
        return False


def cohort_matches(row: Mapping[str, Any], freeze: Mapping[str, Any]) -> bool:
    return str(row.get("cohort_id", "")) == str(freeze.get("cohort_id", "")) and str(row.get("cohort_manifest_hash", "")) == str(freeze.get("cohort_manifest_hash", ""))


def candidate_benchmark_entry_consistent(
    row: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
    group: list[dict[str, Any]],
    tolerance: float = 5e-9,
) -> bool:
    if not candidate:
        return False
    code = str(row.get("industry_code", "")).zfill(6)
    selected = [item for item in group if str(item.get("industry_code", "")).zfill(6) == code]
    if len(selected) != 1:
        return False
    try:
        return abs(float(candidate.get("entry_close_index", "nan")) - float(selected[0].get("entry_close_index", "nan"))) <= tolerance
    except (TypeError, ValueError):
        return False


def settlement_values_valid(
    row: Mapping[str, Any],
    root: Path | None,
    *,
    candidate_freeze: Mapping[str, Any] | None = None,
    benchmark_group: list[dict[str, Any]] | None = None,
    tolerance: float = 5e-9,
) -> tuple[bool, str]:
    if not root:
        return False, "workspace root is unavailable"
    artifact_text = str(row.get("settlement_source_artifact", ""))
    artifact = workspace_artifact(root, artifact_text)
    if artifact is None:
        return False, "settlement source snapshot is outside the workspace"
    if not artifact.is_file():
        return False, "settlement source snapshot is missing"
    if file_sha256(artifact) != str(row.get("settlement_source_fingerprint", "")):
        return False, "settlement source fingerprint mismatch"
    if str(row.get("settlement_calculation_version", "")) != "fund_flow_forward_settlement_exact_v2":
        return False, "unknown settlement calculation version"
    try:
        frame = pd.read_csv(artifact, encoding="utf-8-sig", dtype={"industry_code": str}).fillna("")
        for field in ["benchmark_entry_close_index", "exit_close_index"]:
            frame[field] = pd.to_numeric(frame[field], errors="raise")
        if len(frame) != int(str(row.get("settlement_source_row_count", "0"))):
            return False, "settlement source row count mismatch"
        frame["industry_code"] = frame["industry_code"].astype(str).str.zfill(6)
        unique_count = frame["industry_code"].nunique()
        if len(frame) < 100 or unique_count < 100:
            return False, "settlement source has fewer than 100 unique industries"
        if len(frame) != unique_count:
            return False, "settlement source contains duplicate industry rows"
        if not frame["planned_entry_date"].astype(str).eq(str(row.get("planned_entry_date", ""))).all():
            return False, "settlement source entry dates are not exact"
        if not frame["planned_exit_date"].astype(str).eq(str(row.get("planned_exit_date", ""))).all():
            return False, "settlement source exit dates are not exact"
        code = str(row.get("industry_code", "")).zfill(6)
        selected = frame[frame["industry_code"].eq(code)]
        if len(selected) != 1:
            return False, "settlement source does not contain exactly one candidate row"
        if "selected_candidate" not in frame.columns:
            return False, "settlement source is missing selected_candidate"
        selected_flags = frame["selected_candidate"].map(is_true)
        if int(selected_flags.sum()) != 1 or str(frame.loc[selected_flags, "industry_code"].iloc[0]) != code:
            return False, "settlement source selected_candidate flag is not unique or does not match the candidate"
        candidate_entry = float(selected.iloc[0]["candidate_entry_close_index"])
        if candidate_freeze is not None and abs(candidate_entry - float(candidate_freeze.get("entry_close_index", "nan"))) > tolerance:
            return False, "candidate entry price differs from authoritative freeze ledger"
        if benchmark_group is not None:
            authoritative = {
                str(item.get("industry_code", "")).zfill(6): float(item.get("entry_close_index", "nan"))
                for item in benchmark_group
                if item.get("benchmark_entry_freeze_status") == "frozen_on_time"
                and cohort_matches(row, item)
            }
            snapshot_entries = dict(zip(frame["industry_code"], frame["benchmark_entry_close_index"]))
            if len(frame) != len(authoritative):
                return False, "settlement source row count differs from authoritative benchmark universe"
            if set(snapshot_entries) != set(authoritative):
                return False, "settlement source universe differs from authoritative benchmark freeze ledger"
            if any(abs(float(snapshot_entries[item]) - authoritative[item]) > tolerance for item in authoritative):
                return False, "benchmark entry prices differ from authoritative freeze ledger"
        exit_close = float(selected.iloc[0]["exit_close_index"])
        realized = exit_close / candidate_entry - 1.0
        benchmark_returns = frame["exit_close_index"] / frame["benchmark_entry_close_index"] - 1.0
        benchmark = float(benchmark_returns.mean())
        selected_index = selected.index[0]
        rank_pct = float(benchmark_returns.rank(pct=True).loc[selected_index])
        expected = {
            "realized_return": realized,
            "benchmark_return": benchmark,
            "realized_relative_return": realized - benchmark,
            "future_return_rank_pct": rank_pct,
        }
        for field, value in expected.items():
            if abs(float(str(row.get(field, "nan"))) - value) > tolerance:
                return False, f"{field} cannot be reproduced"
        if is_true(row.get("future_top_quintile")) != (rank_pct >= 0.8):
            return False, "future_top_quintile cannot be reproduced"
        if int(str(row.get("benchmark_universe_count_used", "0"))) != len(frame):
            return False, "benchmark_universe_count_used differs from settlement source"
    except (KeyError, TypeError, ValueError, pd.errors.ParserError) as exc:
        return False, f"settlement source parse/recompute failed: {exc}"
    return True, "exact source snapshot reproduces all settlement metrics"


def belongs_to_cohort(row: Mapping[str, Any], cohort_id: str, manifest_hash: str) -> bool:
    return bool(
        cohort_id
        and valid_sha256(manifest_hash)
        and str(row.get("cohort_id", "")) == cohort_id
        and str(row.get("cohort_manifest_hash", "")) == manifest_hash
    )


def filter_frame_by_cohort(frame: pd.DataFrame, cohort_id: str, manifest_hash: str) -> pd.DataFrame:
    if frame.empty or not cohort_id or not valid_sha256(manifest_hash):
        return frame.iloc[0:0].copy()
    if not {"cohort_id", "cohort_manifest_hash"}.issubset(frame.columns):
        return frame.iloc[0:0].copy()
    mask = frame["cohort_id"].astype(str).eq(cohort_id) & frame["cohort_manifest_hash"].astype(str).eq(manifest_hash)
    return frame.loc[mask].copy()


def grouped_benchmark_freeze(frame: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    if frame.empty:
        return groups
    for row in frame.to_dict("records"):
        groups.setdefault(benchmark_key(row), []).append(row)
    return groups


def frozen_entry_keys(frame: pd.DataFrame) -> set[str]:
    if frame.empty:
        return set()
    return {freeze_key(row) for row in frame.fillna("").to_dict("records") if row.get("entry_price_freeze_status") == "frozen_on_time" and freeze_recorded_on_time(row)}


def frozen_benchmark_counts(frame: pd.DataFrame) -> dict[str, int]:
    return {key: len({str(row.get("industry_code", "")).zfill(6) for row in rows if row.get("benchmark_entry_freeze_status") == "frozen_on_time" and freeze_recorded_on_time(row)}) for key, rows in grouped_benchmark_freeze(frame.fillna("") if not frame.empty else frame).items()}


def frozen_benchmark_member_keys(frame: pd.DataFrame) -> set[str]:
    if frame.empty:
        return set()
    return {benchmark_member_key(row) for row in frame.fillna("").to_dict("records") if row.get("benchmark_entry_freeze_status") == "frozen_on_time" and freeze_recorded_on_time(row)}


def freeze_key(row: Mapping[str, Any] | pd.Series) -> str:
    return "|".join([
        str(row.get("cohort_id", "")),
        str(row.get("cohort_manifest_hash", "")),
        str(row.get("batch_id", "")),
        str(row.get("industry_code", "")).zfill(6),
    ])


def benchmark_key(row: Mapping[str, Any] | pd.Series) -> str:
    return "|".join([
        str(row.get("cohort_id", "")),
        str(row.get("cohort_manifest_hash", "")),
        str(row.get("batch_id", "")),
        str(row.get("planned_entry_date", "")),
    ])


def benchmark_member_key(row: Mapping[str, Any] | pd.Series) -> str:
    return f"{benchmark_key(row)}|{str(row.get('industry_code', '')).zfill(6)}"


def duplicates(keys: list[tuple[str, str]]) -> list[str]:
    seen: set[tuple[str, str]] = set()
    duplicate: list[str] = []
    for key in keys:
        if key in seen:
            duplicate.append("|".join(key))
        seen.add(key)
    return duplicate


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def valid_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text.lower())


def evidence_artifact_manifest(
    rows: Iterable[Mapping[str, Any]],
    entry_freeze: pd.DataFrame,
    benchmark_freeze: pd.DataFrame,
    root: Path,
) -> tuple[str, int]:
    """Fingerprint every immutable source file referenced by the active cohort."""
    references: set[str] = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            references.add(text)

    observation_rows = [dict(item) for item in rows]
    for row in observation_rows:
        add(row.get("source_artifact"))
        add(row.get("calendar_source"))
        if str(row.get("source_fingerprint_status", "")) == "verified_bundle":
            manifest_path = workspace_artifact(root, row.get("source_artifact", ""))
            if manifest_path is not None and manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    add(manifest.get("candidate_artifact"))
                    add(manifest.get("summary_artifact"))
                except (OSError, json.JSONDecodeError):
                    pass
    for frame in [entry_freeze, benchmark_freeze]:
        if not frame.empty and "freeze_source" in frame.columns:
            for value in frame["freeze_source"].fillna("").astype(str):
                add(value)
    manifest_rows: list[dict[str, str]] = []
    for reference in sorted(references):
        artifact = workspace_artifact(root, reference)
        if artifact is None:
            fingerprint = "OUTSIDE_WORKSPACE"
        else:
            fingerprint = file_sha256(artifact) if artifact.is_file() else "MISSING"
        manifest_rows.append({"artifact": reference, "sha256": fingerprint})
    return csv_fingerprint(manifest_rows, fieldnames=["artifact", "sha256"], sort_rows_by=["artifact"]), len(manifest_rows)


def fingerprint_or_missing(path: Path) -> str:
    return file_sha256(path) if path.is_file() else "MISSING"


def workspace_artifact(root: Path, value: Any) -> Path | None:
    candidate = Path(str(value or ""))
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def check(name: str, ok: bool, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": "pass" if ok else "fail", "evidence": evidence, "meaning": meaning}


def v(row_number: int, key: str, violation: str, detail: str) -> dict[str, str]:
    return {"row_number": str(row_number), "ledger_key": key, "violation": violation, "detail": detail}


def build_summary(
    rows: list[dict[str, Any]],
    checks: pd.DataFrame,
    violations: list[dict[str, str]],
    as_of: date,
    *,
    events: Iterable[Mapping[str, Any]] | None = None,
    active_cohort: Mapping[str, Any] | None = None,
    global_rows: list[dict[str, Any]] | None = None,
    global_checks: pd.DataFrame | None = None,
    global_violations: list[dict[str, str]] | None = None,
    input_fingerprints: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    fail_count = int(checks["status"].eq("fail").sum())
    event_rows = [dict(item) for item in (events or [])]
    head = ""
    try:
        head = verify_hash_chain(event_rows).head_hash if event_rows else ""
    except HashChainError:
        head = ""
    late = sum(item["violation"].startswith("late_") for item in violations)
    cohort_ids = sorted({str(row.get("cohort_id", "")) for row in rows if str(row.get("cohort_id", ""))})
    cohort_hashes = sorted({str(row.get("cohort_manifest_hash", "")) for row in rows if valid_sha256(row.get("cohort_manifest_hash"))})
    integrity_passed = fail_count == 0 and not violations
    active = dict(active_cohort or {})
    all_rows = rows if global_rows is None else global_rows
    all_checks = checks if global_checks is None else global_checks
    all_violations = violations if global_violations is None else global_violations
    global_fail_count = int(all_checks["status"].eq("fail").sum()) if not all_checks.empty else 0
    global_integrity_passed = global_fail_count == 0 and not all_violations
    global_late = sum(item["violation"].startswith("late_") for item in all_violations)
    summary: dict[str, Any] = {
        "version": "5.30.4",
        "policy_id": "fund_flow_forward_ledger_integrity_v5_30",
        "policy_status": "research_only",
        "generated_at": datetime.now(_shanghai_zone()).isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "ledger_rows": len(rows),
        "ledger_event_count": len(event_rows),
        "ledger_head_hash": head,
        "active_cohort_id": str(active.get("cohort_id", "")),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", "")),
        "active_cohort_freeze_passed": bool(active.get("freeze_passed", False)),
        "cohort_ids": cohort_ids,
        "cohort_manifest_hashes": cohort_hashes,
        "check_count": int(len(checks)),
        "fail_count": fail_count,
        "violation_count": len(violations),
        "late_backfill_count": late,
        "integrity_passed": integrity_passed,
        "eligible_cohort_hashes": cohort_hashes if integrity_passed else [],
        "eligible_cohorts": ([{
            "cohort_id": str(active.get("cohort_id", "")),
            "manifest_hash": str(active.get("manifest_hash", "")),
        }] if integrity_passed and active.get("cohort_id") and active.get("manifest_hash") else []),
        "global_ledger_rows": len(all_rows),
        "global_fail_count": global_fail_count,
        "global_violation_count": len(all_violations),
        "global_late_backfill_count": global_late,
        "global_ledger_integrity_passed": global_integrity_passed,
        **dict(input_fingerprints or {}),
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_ledger_integrity_passed" if integrity_passed else "research_only_ledger_integrity_failed",
        "final_verdict": "V5.30 已校验事件哈希链、观测时点、来源指纹、cohort、精确日期及至少 100 行基准；完整性通过也不代表已找到强反弹行业。" if integrity_passed else "V5.30 检出资金流前推证据完整性违规；相关样本不得结算或晋级。",
    }
    summary["integrity_result_hash"] = json_fingerprint({key: value for key, value in summary.items() if key not in {"generated_at", "integrity_result_hash"}})
    return summary


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_frame(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}).fillna("") if path.exists() else pd.DataFrame()


def write_outputs(
    summary: dict[str, Any],
    checks: pd.DataFrame,
    rows: list[dict[str, Any]],
    violations: list[dict[str, str]],
    *,
    global_checks: pd.DataFrame | None = None,
    global_rows: list[dict[str, Any]] | None = None,
    global_violations: list[dict[str, str]] | None = None,
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    atomic_write_csv(OUT / "top_candidates.csv", checks.fillna("").to_dict("records"), fieldnames=list(checks.columns))
    atomic_write_json(OUT / "run_summary.json", summary)
    atomic_write_text(OUT / "report.md", render_report(summary, checks))
    atomic_write_csv(DEBUG / "ledger_integrity_checks.csv", checks.fillna("").to_dict("records"), fieldnames=list(checks.columns))
    row_frame = pd.DataFrame(rows)
    violation_frame = pd.DataFrame(violations, columns=["row_number", "ledger_key", "violation", "detail"])
    atomic_write_csv(DEBUG / "ledger_snapshot.csv", row_frame.fillna("").to_dict("records"), fieldnames=list(row_frame.columns))
    atomic_write_csv(DEBUG / "violation_rows.csv", violation_frame.fillna("").to_dict("records"), fieldnames=list(violation_frame.columns))
    if global_checks is not None:
        atomic_write_csv(DEBUG / "global_ledger_integrity_checks.csv", global_checks.fillna("").to_dict("records"), fieldnames=list(global_checks.columns))
    if global_rows is not None:
        global_row_frame = pd.DataFrame(global_rows)
        atomic_write_csv(DEBUG / "global_ledger_snapshot.csv", global_row_frame.fillna("").to_dict("records"), fieldnames=list(global_row_frame.columns))
    if global_violations is not None:
        global_violation_frame = pd.DataFrame(global_violations, columns=["row_number", "ledger_key", "violation", "detail"])
        atomic_write_csv(DEBUG / "global_violation_rows.csv", global_violation_frame.fillna("").to_dict("records"), fieldnames=list(global_violation_frame.columns))


def render_report(summary: dict[str, Any], checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.30 资金流前推账本完整性审计",
        "",
        summary["final_verdict"],
        "",
        f"- as-of 日期：{summary['as_of_date']}",
        f"- 观察行数 / 事件数：{summary['ledger_rows']} / {summary['ledger_event_count']}",
        f"- 当前 cohort：`{summary['active_cohort_id'] or 'missing'}`；冻结核验：`{str(summary['active_cohort_freeze_passed']).lower()}`",
        f"- 账本头哈希：`{summary['ledger_head_hash']}`",
        f"- 失败项 / 违规行：{summary['fail_count']} / {summary['violation_count']}",
        f"- 迟到回填：{summary['late_backfill_count']}",
        f"- 账本完整性通过：`{str(summary['integrity_passed']).lower()}`",
        f"- 全局历史账本：行数={summary['global_ledger_rows']}；违规={summary['global_violation_count']}；通过=`{str(summary['global_ledger_integrity_passed']).lower()}`",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        checks.to_markdown(index=False),
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _shanghai_zone():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Shanghai")


def _utc_zone():
    from datetime import timezone

    return timezone.utc


def _self_check_fixture() -> tuple[dict[str, Any], list[dict[str, Any]], pd.DataFrame, pd.DataFrame]:
    row: dict[str, Any] = {
        "record_schema_version": "2.0", "event_type": "observation", "event_id": "",
        "observation_id": "", "record_hash": "", "batch_id": "b1",
        "policy_status": "research_only", "outcome_status": "pending_forward_observation",
        "signal_date": "2026-01-01", "planned_entry_date": "2026-01-02", "planned_exit_date": "2026-01-10",
        "industry_code": "801001", "industry_name": "A", "dual_positive_flow": "True",
        "settlement_status": "not_due", "realized_return": "", "benchmark_return": "",
        "realized_relative_return": "", "future_return_rank_pct": "", "future_top_quintile": "",
        "actual_entry_date": "", "actual_exit_date": "", "entry_price_freeze_status": "",
        "benchmark_entry_freeze_status": "", "detected_at_utc": "2026-01-01T12:00:00Z",
        "event_recorded_at_utc": "2026-01-01T12:01:00Z", "evidence_cutoff": "2026-01-01T07:00:00Z",
        "entry_cutoff": "2026-01-02T01:30:00Z", "source_artifact": "fixture.csv",
        "source_fingerprint": "a" * 64, "source_fingerprint_status": "verified_fixture",
        "calendar_source": "self_check_fixture", "calendar_fingerprint": "b" * 64,
        "experiment_id": "exp1", "cohort_id": "c1", "cohort_manifest_hash": "c" * 64,
        "rule_id": "r1", "code_version": "d" * 64, "late_backfill_excluded": "False",
        "integrity_eligible": "True", "promotion_eligible": "True", "sample_scope": "goal_qualified",
        "qualified_for_goal": "True", "entry_date_exact": "", "exit_date_exact": "",
        "window_signal_pass": "True", "valuation_gate_pass": "True", "stabilization_gate_pass": "True",
        "window_id": "w1", "frozen_selection_rule_id": "r1",
        "benchmark_universe_count_used": "",
    }
    row["observation_id"] = stable_observation_id(row)
    row["event_id"] = f"{row['observation_id']}:observation"
    event = dict(row)
    event["previous_hash"] = GENESIS_HASH
    event["record_hash"] = hash_chain_record(event)
    row["record_hash"] = event["record_hash"]
    entry_payload = {"industry_code": "801001", "actual_entry_date": "2026-01-02", "entry_close_index": "100.0000000000", "freeze_source": "fixture"}
    entry = pd.DataFrame([{**row, "as_of_date": "2026-01-02", "actual_entry_date": "2026-01-02", "entry_close_index": "100.0000000000", "entry_price_freeze_status": "frozen_on_time", "freeze_at_utc": "2026-01-02T07:30:00Z", "freeze_source": "fixture", "source_fingerprint": json_fingerprint(entry_payload)}])
    benchmark_payload = [
        {"industry_code": "801001" if index == 0 else f"{802000 + index:06d}", "actual_entry_date": "2026-01-02", "entry_close_index": "100.0000000000"}
        for index in range(101)
    ]
    universe_hash = csv_fingerprint(benchmark_payload, fieldnames=["industry_code", "actual_entry_date", "entry_close_index"], sort_rows_by=["industry_code"])
    benchmark = pd.DataFrame([{**item, "batch_id": "b1", "planned_entry_date": "2026-01-02", "as_of_date": "2026-01-02", "benchmark_entry_freeze_status": "frozen_on_time", "freeze_at_utc": "2026-01-02T07:30:00Z", "cohort_id": "c1", "cohort_manifest_hash": "c" * 64, "benchmark_universe_fingerprint": universe_hash} for item in benchmark_payload])
    return row, [event], entry, benchmark


def self_check() -> None:
    row, events, entry, benchmark = _self_check_fixture()
    checks, violations = audit_rows([row], date(2026, 1, 2), entry, benchmark, events=events, allow_fixture_fingerprints=True)
    assert checks["status"].eq("pass").all()
    assert not violations
    late_row = dict(row, detected_at_utc="2026-01-02T02:00:00Z", event_recorded_at_utc="2026-01-02T02:01:00Z")
    late_event = dict(late_row, previous_hash=GENESIS_HASH)
    late_event["record_hash"] = hash_chain_record(late_event)
    late_row["record_hash"] = late_event["record_hash"]
    _, violations = audit_rows([late_row], date(2026, 1, 2), entry, benchmark, events=[late_event], allow_fixture_fingerprints=True)
    assert any(item["violation"] == "observation_timestamp_inversion" for item in violations)
    late_entry = entry.copy()
    late_entry.loc[:, "as_of_date"] = "2026-01-03"
    late_entry.loc[:, "freeze_at_utc"] = "2026-01-03T07:30:00Z"
    late_entry.loc[:, "entry_price_freeze_status"] = "late_backfill_excluded"
    _, violations = audit_rows([row], date(2026, 1, 3), late_entry, benchmark, events=events, allow_fixture_fingerprints=True)
    assert any(item["violation"] == "late_candidate_entry_freeze" for item in violations)
    short_benchmark = benchmark.iloc[:99].copy()
    _, violations = audit_rows([row], date(2026, 1, 2), entry, short_benchmark, events=events, allow_fixture_fingerprints=True)
    assert any(item["violation"] == "missing_or_invalid_benchmark_entry_freeze" for item in violations)
    bad_future = dict(row, realized_return="0.1")
    bad_event = dict(bad_future, previous_hash=GENESIS_HASH)
    bad_event["record_hash"] = hash_chain_record(bad_event)
    bad_future["record_hash"] = bad_event["record_hash"]
    _, violations = audit_rows([bad_future], date(2026, 1, 2), entry, benchmark, events=[bad_event], allow_fixture_fingerprints=True)
    assert any(item["violation"] == "future_fields_filled_before_exit" for item in violations)
    summary = build_summary([row], checks, [], date(2026, 1, 2), events=events)
    assert summary["integrity_passed"] is True
    assert summary["can_claim_strong_rebound_industries"] is False
    print("self_check=pass")


if __name__ == "__main__":
    main()
