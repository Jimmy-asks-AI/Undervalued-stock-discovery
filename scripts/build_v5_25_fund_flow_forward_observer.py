#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from fund_flow_forward_evidence import (
    LEDGER_FIELDS,
    append_events,
    entry_cutoff_utc,
    event_chain_head,
    evidence_cutoff_utc,
    freeze_deadline_utc,
    is_true,
    iso_utc,
    materialize_observations,
    migrate_legacy_csv,
    observation_detected_on_time,
    observation_timing_status,
    parse_timestamp,
    read_events,
    relative_posix,
    snapshot_source,
    stable_observation_id,
    utc_now,
    verify_ledger_checkpoint,
    with_schema_fields,
    write_materialized_csv,
)
from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from research_integrity import AShareTradingCalendar, DuplicateRecordError, atomic_write_json, file_sha256, json_fingerprint, load_a_share_trading_calendar


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs" / "industry_rebound_leader_fund_flow_readiness_v4_76" / "top_candidates.csv"
SUMMARY = ROOT / "outputs" / "industry_rebound_leader_fund_flow_readiness_v4_76" / "run_summary.json"
LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv"
EVENT_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
SOURCE_SNAPSHOTS = ROOT / "logs" / "v5_25_fund_flow_forward_sources"
CALENDAR_CACHE = ROOT / "data_catalog" / "cache" / "trading_calendar" / "a_share_trade_calendar.csv"
OUT = ROOT / "outputs" / "audit" / "fund_flow_forward_observer_v5_25"
DEBUG = OUT / "debug"
MIGRATION_BACKUP = ROOT / "outputs" / "audit" / "fund_flow_forward_chain_remediation" / "debug" / "pre_migration_ledger.csv"

FIELDS = LEDGER_FIELDS

QUALIFICATION_FIELDS = [
    "window_signal_pass", "valuation_gate_pass", "stabilization_gate_pass",
    "window_id", "frozen_selection_rule_id",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.25 forward observer for fund-flow dual-positive industry candidates.")
    parser.add_argument("--apply", action="store_true", help="Append current observations to the persistent ledger.")
    parser.add_argument("--migrate-ledger", action="store_true", help="One-time conversion of the legacy CSV into the append-only hash chain.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    if args.migrate_ledger:
        events = migrate_legacy_csv(LEDGER, EVENT_LEDGER, backup_path=MIGRATION_BACKUP)
        write_materialized_csv(LEDGER, materialize_observations(events))
        if not args.apply:
            print(f"event_ledger={EVENT_LEDGER}")
            print(f"migrated_observation_count={len(materialize_observations(events))}")
            print(f"ledger_head_hash={event_chain_head(EVENT_LEDGER)}")
            return

    if args.apply and not EVENT_LEDGER.exists():
        parser.error("append-only event ledger is missing; run --migrate-ledger before --apply")

    source = pd.read_csv(SOURCE, encoding="utf-8-sig")
    summary = read_json(SUMMARY)
    signal_date = str(summary.get("latest_cache_date", ""))
    required_through = date.fromisoformat(signal_date) + timedelta(days=45)
    calendar = load_a_share_trading_calendar(CALENDAR_CACHE, required_through=required_through)
    source_artifact = relative_posix(SOURCE, ROOT)
    source_fingerprint = file_sha256(SOURCE)
    source_status = "preview_not_snapshotted"
    if args.apply:
        source_artifact, source_fingerprint = create_source_bundle(signal_date)
        source_status = "verified_bundle"
    calendar_fingerprint = file_sha256(CALENDAR_CACHE)
    calendar_artifact = calendar.source
    if args.apply:
        calendar_snapshot, calendar_fingerprint = snapshot_source(CALENDAR_CACHE, SOURCE_SNAPSHOTS / "calendars")
        calendar_artifact = relative_posix(calendar_snapshot, ROOT)
    active_cohort = validated_active_cohort()
    rows = build_observations(
        source,
        signal_date,
        calendar=calendar,
        detected_at=utc_now(),
        source_artifact=source_artifact,
        source_fingerprint=source_fingerprint,
        source_fingerprint_status=source_status,
        calendar_fingerprint=calendar_fingerprint,
        calendar_artifact=calendar_artifact,
        cohort=active_cohort,
    )
    ledger_before = read_ledger()
    ledger_after = ledger_before
    if args.apply:
        existing_events = read_events(EVENT_LEDGER)
        rows = first_write_wins_observations(rows, existing_events)
        append_events(EVENT_LEDGER, rows.fillna("").to_dict("records"))
        events = read_events(EVENT_LEDGER)
        ledger_after = pd.DataFrame(materialize_observations(events), columns=FIELDS)
        write_materialized_csv(LEDGER, ledger_after.to_dict("records"))
    checks = build_checks(rows, ledger_before, ledger_after, args.apply)
    run_summary = build_summary(rows, ledger_before, ledger_after, checks, args.apply, active_cohort=active_cohort)
    write_outputs(run_summary, rows, ledger_after, checks)
    print(f"output_dir={OUT}")
    print(f"apply={args.apply}")
    print(f"appendable_observation_count={len(rows)}")


def build_observations(
    source: pd.DataFrame,
    signal_date: str,
    *,
    calendar: AShareTradingCalendar,
    detected_at: datetime,
    source_artifact: str,
    source_fingerprint: str,
    source_fingerprint_status: str,
    calendar_fingerprint: str,
    calendar_artifact: str = "",
    cohort: dict[str, Any] | None = None,
) -> pd.DataFrame:
    dual = source[source["dual_positive_flow"].astype(str).str.lower().eq("true")].copy()
    if dual.empty:
        return pd.DataFrame(columns=FIELDS)
    signal_day = date.fromisoformat(signal_date)
    entry_day = calendar.next_trading_day(signal_day)
    exit_day = calendar.holding_exit(entry_day, 20)
    entry = entry_day.isoformat()
    exit_ = exit_day.isoformat()
    now = iso_utc(detected_at)
    cohort = cohort or {}
    cohort_id = str(cohort.get("cohort_id", "") or f"unfrozen_exploratory_{signal_date.replace('-', '')}")
    cohort_hash = str(cohort.get("manifest_hash", ""))
    cohort_verified = bool(cohort_hash and cohort.get("freeze_passed") is True)
    cohort_created_at = parse_timestamp(cohort.get("created_at_utc", ""))
    source_verified = source_fingerprint_status in {"verified_snapshot", "verified_bundle"} and len(source_fingerprint) == 64
    code_version = file_sha256(Path(__file__))
    rows = []
    for _, row in dual.iterrows():
        base_qualified, base_reason = qualification_status(row)
        cutoffs = {
            "detected_at_utc": now,
            "evidence_cutoff": iso_utc(evidence_cutoff_utc(signal_day)),
            "entry_cutoff": iso_utc(entry_cutoff_utc(entry_day)),
        }
        timing_status = observation_timing_status(cutoffs)
        if timing_status == "early_pending":
            continue
        if timing_status == "invalid":
            raise ValueError("invalid observation timing window")
        evidence_available_at = parse_timestamp(cutoffs["evidence_cutoff"])
        if cohort_created_at and evidence_available_at and cohort_created_at > evidence_available_at:
            # A newly frozen methodology cohort cannot retroactively own an older signal.
            continue
        detected_on_time = observation_detected_on_time(cutoffs)
        integrity_eligible = detected_on_time and source_verified and cohort_verified
        qualified = base_qualified and integrity_eligible
        reasons = [] if base_qualified else [base_reason]
        if not detected_on_time:
            reasons.append("late_backfill_excluded")
        if not source_verified:
            reasons.append("source_snapshot_not_verified")
        if not cohort_verified:
            reasons.append("cohort_baseline_not_verified")
        reason = "all_required_gates_and_integrity_passed" if qualified else "|".join(reasons)
        scope = "goal_qualified" if qualified else "exploratory_fund_flow_only"
        item = {
            "recorded_at": now,
            "batch_id": (f"v5_25_goal_qualified_{signal_date}_{cohort_id}_{row.get('window_id', '')}".rstrip("_") if qualified else f"v5_25_fund_flow_dual_positive_{signal_date}_{cohort_id}"),
            "policy_version": "5.25.2",
            "policy_id": "fund_flow_forward_observer_v5_25",
            "policy_status": "research_only",
            "decision": "planned_observation",
            "outcome_status": "pending_goal_observation" if qualified else "exploratory_forward_observation",
            "signal_date": signal_date,
            "planned_entry_date": entry,
            "planned_exit_date": exit_,
            "industry_code": str(row.get("industry_code", "")).zfill(6),
            "industry_name": row.get("industry_name", ""),
            "selection_score": row.get("selection_score", ""),
            "fund_flow_research_status": row.get("fund_flow_research_status", ""),
            "fund_flow_overlay_status": row.get("fund_flow_overlay_status", ""),
            "ths_industry_name": row.get("ths_industry_name", ""),
            "ths_today_net_flow": row.get("ths_today_net_flow", ""),
            "ths_5d_net_flow": row.get("ths_5d_net_flow", ""),
            "today_flow_positive": row.get("today_flow_positive", ""),
            "five_day_flow_positive": row.get("five_day_flow_positive", ""),
            "dual_positive_flow": row.get("dual_positive_flow", ""),
            "historical_failure_flag": row.get("historical_failure_flag", ""),
            "settlement_status": "not_due",
            "settlement_notes": "合格目标样本；未到退出日不填未来收益。" if qualified else "探索性资金流样本；不计入原目标晋级。",
            "sample_scope": scope,
            "window_signal_pass": row.get("window_signal_pass", ""),
            "valuation_gate_pass": row.get("valuation_gate_pass", ""),
            "stabilization_gate_pass": row.get("stabilization_gate_pass", ""),
            "window_id": row.get("window_id", ""),
            "frozen_selection_rule_id": row.get("frozen_selection_rule_id", ""),
            "qualified_for_goal": qualified,
            "qualification_reason": reason,
            "record_schema_version": "2.1",
            "event_type": "observation",
            "parent_event_id": "",
            "event_recorded_at_utc": now,
            "detected_at_utc": now,
            "evidence_cutoff": cutoffs["evidence_cutoff"],
            "entry_cutoff": cutoffs["entry_cutoff"],
            "freeze_deadline_utc": iso_utc(freeze_deadline_utc(entry_day)),
            "source_artifact": source_artifact,
            "source_fingerprint": source_fingerprint,
            "source_fingerprint_status": source_fingerprint_status,
            "calendar_source": calendar_artifact or calendar.source,
            "calendar_fingerprint": calendar_fingerprint,
            "experiment_id": "fund_flow_forward_observer_v5_25",
            "cohort_id": cohort_id,
            "cohort_manifest_hash": cohort_hash,
            "rule_id": str(row.get("frozen_selection_rule_id", "") or "fund_flow_dual_positive_observer_v5_25"),
            "code_version": code_version,
            "late_backfill_excluded": not detected_on_time,
            "integrity_eligible": integrity_eligible,
            "promotion_eligible": qualified,
            "migration_version": "",
        }
        observation_id = stable_observation_id(item)
        item["observation_id"] = observation_id
        item["event_id"] = f"{observation_id}:observation"
        rows.append(with_schema_fields(item))
    return pd.DataFrame(rows, columns=FIELDS)


def qualification_status(row: pd.Series | dict[str, Any]) -> tuple[bool, str]:
    missing = []
    for field in QUALIFICATION_FIELDS:
        value = row.get(field, "")
        if field.endswith("_pass"):
            if not is_true(value):
                missing.append(field)
        elif not str(value).strip():
            missing.append(field)
    return not missing, "all_required_gates_passed" if not missing else "missing_or_failed:" + "|".join(missing)


def read_ledger() -> pd.DataFrame:
    if EVENT_LEDGER.exists():
        verify_ledger_checkpoint(EVENT_LEDGER)
        ledger = pd.DataFrame(materialize_observations(read_events(EVENT_LEDGER)))
    else:
        ledger = pd.read_csv(LEDGER, encoding="utf-8-sig") if LEDGER.exists() else pd.DataFrame(columns=FIELDS)
    return with_ledger_fields(ledger)


def write_ledger(ledger: pd.DataFrame) -> None:
    raise RuntimeError("direct ledger overwrite is forbidden; append to the JSONL hash chain")


def with_ledger_fields(ledger: pd.DataFrame) -> pd.DataFrame:
    if ledger.empty:
        return pd.DataFrame(columns=FIELDS)
    return pd.DataFrame([with_schema_fields(row) for row in ledger.fillna("").to_dict("records")], columns=FIELDS)


def append_unique(ledger: pd.DataFrame, rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return ledger
    combined = pd.concat([ledger, rows], ignore_index=True)
    return combined.drop_duplicates(["batch_id", "industry_code"], keep="first")


REPLAY_DYNAMIC_FIELDS = frozenset({
    "recorded_at", "detected_at_utc", "event_recorded_at_utc", "ledger_sequence",
    "previous_hash", "record_hash",
})


def first_write_wins_observations(rows: pd.DataFrame, existing_events: list[dict[str, Any]]) -> pd.DataFrame:
    """Make a crash retry idempotent while rejecting changed evidence under the same ID."""
    if rows.empty:
        return rows.copy()
    existing = {
        str(event.get("observation_id", "")): event
        for event in existing_events
        if str(event.get("event_type", "")) == "observation"
    }
    appendable: list[dict[str, Any]] = []
    for row in rows.fillna("").to_dict("records"):
        observation_id = str(row.get("observation_id", ""))
        previous = existing.get(observation_id)
        if previous is None:
            appendable.append(row)
            continue
        changed = [
            field for field in FIELDS
            if field not in REPLAY_DYNAMIC_FIELDS
            and str(previous.get(field, "")) != str(row.get(field, ""))
        ]
        if changed:
            raise DuplicateRecordError(f"observation replay changed immutable evidence {changed}: {observation_id}")
    return pd.DataFrame(appendable, columns=FIELDS)


def build_checks(rows: pd.DataFrame, before: pd.DataFrame, after: pd.DataFrame, applied: bool) -> pd.DataFrame:
    qualified_rows = qualified_frame(rows)
    qualified_after = qualified_frame(after)
    late_rows = int(rows.get("late_backfill_excluded", pd.Series(dtype=object)).map(is_true).sum()) if not rows.empty else 0
    chain_ok = EVENT_LEDGER.exists() and bool(event_chain_head(EVENT_LEDGER))
    checks = pd.DataFrame([
        check("dual_positive_observations_exist", "pass" if len(rows) else "pending", f"appendable={len(rows)}", "只跟踪资金流双正候选。"),
        check("ledger_append_applied", "pass" if applied else "pending", f"applied={applied}", "未 apply 时只生成预览。"),
        check("ledger_hash_chain", "pass" if chain_ok else "pending", f"event_ledger_exists={EVENT_LEDGER.exists()}; head={event_chain_head(EVENT_LEDGER) if EVENT_LEDGER.exists() else ''}", "权威账本必须是可校验的追加式哈希链。"),
        check("late_backfill_excluded", "pass" if late_rows == 0 else "blocked", f"late_rows={late_rows}", "入场截止后检测到的记录只能保留为永久排除样本。"),
        check("ledger_row_count", "pass", f"before={len(before)}; after={len(after)}", "CSV 只是哈希链的物化视图；观察事件不允许覆盖。"),
        check("goal_qualified_observations", "pass" if len(qualified_after) else "pending", f"appendable_qualified={len(qualified_rows)}; ledger_qualified={len(qualified_after)}", "只有低估、窗口、企稳和冻结规则全部通过的样本可用于目标晋级。"),
        check("can_claim_goal", "fail", "forward observations are not settled", "前推观察未结算前不能证明强行业 alpha。"),
    ])
    has_forward_observations = len(rows) > 0 or len(after) > 0
    checks.loc[checks["check"].eq("dual_positive_observations_exist"), ["status", "evidence", "meaning"]] = [
        "pass" if has_forward_observations else "pending",
        f"appendable={len(rows)}; ledger_after={len(after)}",
        "只跟踪资金流双正候选；已有账本行时不因当天无新增样本而回到 pending。",
    ]
    checks.loc[checks["check"].eq("can_claim_goal"), ["status", "meaning"]] = [
        "pending",
        "前推观察未结算前不能证明强行业 alpha。",
    ]
    return checks


def qualified_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "qualified_for_goal" not in frame.columns:
        return frame.iloc[0:0]
    return frame[frame["qualified_for_goal"].map(is_true)].copy()


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_summary(
    rows: pd.DataFrame,
    before: pd.DataFrame,
    after: pd.DataFrame,
    checks: pd.DataFrame,
    applied: bool,
    *,
    active_cohort: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    qualified_rows = qualified_frame(rows)
    qualified_after = qualified_frame(after)
    active = dict(active_cohort or {})
    return {
        "version": "5.25.2",
        "policy_id": "fund_flow_forward_observer_v5_25",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "applied": applied,
        "appendable_observation_count": int(len(rows)),
        "ledger_rows_before": int(len(before)),
        "ledger_rows_after": int(len(after)),
        "qualified_observation_count": int(len(qualified_rows)),
        "qualified_ledger_rows_after": int(len(qualified_after)),
        "exploratory_observation_count": int(len(rows) - len(qualified_rows)),
        "active_cohort_id": str(active.get("cohort_id", "")),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", "")),
        "active_cohort_freeze_passed": active.get("freeze_passed") is True,
        "late_backfill_excluded_count": int(rows.get("late_backfill_excluded", pd.Series(dtype=object)).map(is_true).sum()) if not rows.empty else 0,
        "event_ledger_path": str(EVENT_LEDGER.relative_to(ROOT)),
        "event_ledger_head_hash": event_chain_head(EVENT_LEDGER) if EVENT_LEDGER.exists() else "",
        "planned_entry_date": "" if rows.empty else str(rows["planned_entry_date"].iloc[0]),
        "planned_exit_date": "" if rows.empty else str(rows["planned_exit_date"].iloc[0]),
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "pass_count": int(checks["status"].eq("pass").sum()),
        "fail_count": int(checks["status"].eq("fail").sum()),
        "pending_count": int(checks["status"].eq("pending").sum()),
        "best_status": "research_only_goal_qualified_observations_recorded" if len(qualified_after) else ("research_only_exploratory_observations_only" if len(after) else "research_only_forward_observations_pending"),
        "final_verdict": "V5.25 只允许低估、窗口、企稳和冻结规则均通过的样本进入目标晋级；其他资金流双正记录保留为探索样本。",
    }


def write_outputs(summary: dict[str, Any], rows: pd.DataFrame, ledger: pd.DataFrame, checks: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    rows.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, rows, checks), encoding="utf-8")
    rows.to_csv(DEBUG / "appendable_observations.csv", index=False, encoding="utf-8-sig")
    ledger.to_csv(DEBUG / "forward_ledger_snapshot.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(DEBUG / "forward_observer_checks.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], rows: pd.DataFrame, checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.25 资金流双正前推观察",
        "",
        summary["final_verdict"],
        "",
        f"- 是否写入账本：`{str(summary['applied']).lower()}`",
        f"- 可追加观察数：{summary['appendable_observation_count']}",
        f"- 账本行数：{summary['ledger_rows_after']}",
        f"- 合格目标样本：{summary['qualified_ledger_rows_after']}",
        f"- 本次探索样本：{summary['exploratory_observation_count']}",
        f"- 计划入场日：{summary['planned_entry_date']}",
        f"- 计划退出日：{summary['planned_exit_date']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 观察候选",
        "",
        rows.to_markdown(index=False) if not rows.empty else "当前没有资金流双正候选。",
        "",
        "## 检查",
        "",
        checks.to_markdown(index=False),
        "",
        "边界：V5.25 只冻结未来样本，不计算未来收益，不改变交易状态。",
    ])


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def create_source_bundle(signal_date: str) -> tuple[str, str]:
    """Freeze both the candidate table and the summary that supplies signal_date."""
    candidate_snapshot, candidate_hash = snapshot_source(SOURCE, SOURCE_SNAPSHOTS / "candidates")
    summary_snapshot, summary_hash = snapshot_source(SUMMARY, SOURCE_SNAPSHOTS / "summaries")
    summary_payload = read_json(SUMMARY)
    if str(summary_payload.get("latest_cache_date", "")) != signal_date:
        raise ValueError("run_summary latest_cache_date does not match the observation signal_date")
    source_rows = pd.read_csv(SOURCE, encoding="utf-8-sig")
    manifest = {
        "bundle_version": "fund_flow_observation_source_bundle_v1",
        "signal_date": signal_date,
        "candidate_artifact": relative_posix(candidate_snapshot, ROOT),
        "candidate_fingerprint": candidate_hash,
        "candidate_row_count": int(len(source_rows)),
        "summary_artifact": relative_posix(summary_snapshot, ROOT),
        "summary_fingerprint": summary_hash,
    }
    bundle_id = json_fingerprint(manifest)
    bundle_path = SOURCE_SNAPSHOTS / "manifests" / f"{bundle_id}.json"
    if bundle_path.exists() and read_json(bundle_path) != manifest:
        raise ValueError(f"source bundle collision: {bundle_path}")
    if not bundle_path.exists():
        atomic_write_json(bundle_path, manifest)
    return relative_posix(bundle_path, ROOT), file_sha256(bundle_path)


def self_check() -> None:
    sessions = [
        "2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26",
        "2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03",
        "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10",
        "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17",
        "2026-07-20", "2026-07-21",
    ]
    calendar = AShareTradingCalendar(sessions, source="self_check_fixture")
    common = {
        "calendar": calendar,
        "detected_at": datetime.fromisoformat("2026-06-22T12:22:43+00:00"),
        "source_artifact": "fixture.csv",
        "source_fingerprint": "a" * 64,
        "source_fingerprint_status": "verified_snapshot",
        "calendar_fingerprint": "b" * 64,
        "calendar_artifact": "self_check_fixture",
        "cohort": {"cohort_id": "c1", "manifest_hash": "c" * 64, "freeze_passed": True},
    }
    source = pd.DataFrame([{"dual_positive_flow": True, "industry_code": 1, "industry_name": "A"}])
    rows = build_observations(source, "2026-06-22", **common)
    assert len(rows) == 1
    assert rows["sample_scope"].iloc[0] == "exploratory_fund_flow_only"
    assert not is_true(rows["qualified_for_goal"].iloc[0])
    assert rows["planned_entry_date"].iloc[0] == "2026-06-23"
    assert rows["planned_exit_date"].iloc[0] == "2026-07-21"
    twice = append_unique(rows, rows)
    assert len(twice) == 1
    settled = rows.copy()
    settled.loc[0, "settlement_status"] = "settled"
    settled.loc[0, "realized_return"] = "0.1"
    kept = append_unique(settled, rows)
    assert kept["settlement_status"].iloc[0] == "settled"
    assert float(kept["realized_return"].iloc[0]) == 0.1
    old = rows.drop(columns=["benchmark_entry_freeze_status"])
    assert "benchmark_entry_freeze_status" in with_ledger_fields(old).columns
    legacy = pd.DataFrame([{"batch_id": "old", "industry_code": "1"}], dtype=str)
    migrated = with_ledger_fields(legacy)
    assert migrated["qualified_for_goal"].iloc[0] == "False"
    assert migrated["sample_scope"].iloc[0] == "exploratory_fund_flow_only"
    no_new_checks = build_checks(rows.iloc[0:0], rows, rows, True)
    indexed = no_new_checks.set_index("check")
    assert indexed.loc["dual_positive_observations_exist", "status"] == "pass"
    assert indexed.loc["can_claim_goal", "status"] == "pending"
    summary = build_summary(rows.iloc[0:0], rows, rows, no_new_checks, True)
    assert summary["fail_count"] == 0
    assert summary["best_status"] == "research_only_exploratory_observations_only"
    qualified_source = pd.DataFrame([{
        "dual_positive_flow": True, "industry_code": 2, "industry_name": "B",
        "window_signal_pass": True, "valuation_gate_pass": True, "stabilization_gate_pass": True,
        "window_id": "w1", "frozen_selection_rule_id": "r1",
    }])
    qualified = build_observations(qualified_source, "2026-06-22", **common)
    assert qualified["sample_scope"].iloc[0] == "goal_qualified"
    assert is_true(qualified["qualified_for_goal"].iloc[0])
    late = build_observations(qualified_source, "2026-06-22", **{**common, "detected_at": datetime.fromisoformat("2026-06-23T02:00:00+00:00")})
    assert is_true(late["late_backfill_excluded"].iloc[0])
    assert not is_true(late["qualified_for_goal"].iloc[0])
    early = build_observations(qualified_source, "2026-06-22", **{**common, "detected_at": datetime.fromisoformat("2026-06-22T06:59:59+00:00")})
    assert early.empty
    for field in ["realized_return", "benchmark_return", "future_return_rank_pct", "entry_price_freeze_status", "benchmark_entry_freeze_status"]:
        assert field in rows.columns
    print("self_check=pass")


if __name__ == "__main__":
    main()
