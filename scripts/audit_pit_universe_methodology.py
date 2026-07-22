#!/usr/bin/env python
"""Audit PIT valuation availability and historical industry-universe comparability.

This is a methodology remediation, not a new strategy version.  Missing source
publication evidence is expected to block promotion while the audit itself can
still pass by proving that the block is enforced and fully reported.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import audit_industry_history_methodology as industry_history_methodology
from valuation_pit_contract import revision_chain_valid_mask, source_artifact_valid_mask


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "pit_universe_methodology_policy.json"
EXPECTED_TOP_LEVEL = {"report.md", "run_summary.json", "top_candidates.csv", "debug"}
DIRECT_SOURCE_ID = "sws_index_analysis_daily"
RECOVERED_SOURCE_ID = "recovered_from_v2_5_quality_components"
DESCRIPTIVE_FEATURES = (
    "low_pb_rank",
    "low_pe_rank",
    "dividend_yield_rank",
)
IDENTITY_UNSAFE_FEATURES_EXCLUDED = ("beta_low_pb_score",)
CORE_PIT_FIELDS = (
    "trade_date",
    "published_at",
    "available_date",
    "fetched_at",
    "source_version",
    "revision_status",
)
UNIVERSE_MODES = (
    "common_survivor_observed_codes",
    "then_observed_active_codes",
    "classification_break_segments",
)
GOVERNANCE_PIT_FIELDS = (
    "industry_code",
    "source",
    "source_hash",
    "source_hash_basis",
    "source_artifact_path",
    "availability_basis",
    "data_status",
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_DECISION_CUTOFF = time(15, 0)


@dataclass(frozen=True)
class AuditArtifacts:
    summary: dict[str, Any]
    metrics: pd.DataFrame
    checks: pd.DataFrame
    debug_tables: dict[str, pd.DataFrame]
    input_manifest: dict[str, Any]


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit PIT valuation and SW industry-universe methodology.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--check", action="store_true", help="Validate the existing standard output against current inputs.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    policy_path = Path(args.policy)
    policy = read_json(policy_path)
    artifacts = build_audit(policy, policy_path=policy_path)
    output_dir = ROOT / str(policy["output_dir"])
    if args.check:
        validate_existing_outputs(output_dir, artifacts)
        mode = "check"
    else:
        write_outputs(output_dir, artifacts)
        mode = "write"
    print(f"output_dir={output_dir}")
    print(f"audit_passed={str(artifacts.summary['audit_passed']).lower()}")
    print(f"promotion_gate_passed={str(artifacts.summary['promotion_gate_passed']).lower()}")
    print(f"promotion_eligible_valuation_row_count={artifacts.summary['promotion_eligible_valuation_row_count']}")
    print(f"mode={mode}")
    if not artifacts.summary["audit_passed"]:
        raise SystemExit(2)


def build_audit(
    policy: Mapping[str, Any],
    *,
    policy_path: Path = DEFAULT_POLICY,
    generated_at: str | None = None,
    root: Path = ROOT,
) -> AuditArtifacts:
    validate_policy(policy)
    valuation_path = root / str(policy["valuation_history_path"])
    opportunity_path = root / str(policy["opportunity_set_path"])
    calendar_path = root / str(policy["frozen_trading_calendar_path"])
    industry_history_dir = root / str(policy["industry_history_dir"])
    industry_snapshot_dir = root / str(policy["industry_snapshot_dir"])
    classification_history_path = root / str(policy["classification_history_path"])
    audit_as_of = date.fromisoformat(str(policy["audit_as_of_date"]))

    valuation = read_csv(valuation_path, dtype={"industry_code": str})
    opportunity = read_csv(opportunity_path, dtype={"industry_code": str})
    trading_calendar = load_trading_calendar(calendar_path)
    required_fields = tuple(str(item) for item in policy["required_valuation_fields"])

    valuation = normalize_identity_columns(valuation)
    field_contract = build_field_contract_audit(valuation, required_fields)
    recovered_source_ids = tuple(str(item) for item in policy["recovered_source_ids"])
    promotion_rows = promotion_eligible_rows(
        valuation,
        trading_calendar,
        required_fields,
        recovered_source_ids=recovered_source_ids,
        allowed_revision_statuses=tuple(str(item) for item in policy["allowed_revision_statuses"]),
        required_governance_fields=tuple(str(item) for item in policy["required_governance_fields"]),
        artifact_root=root,
    )
    provenance = build_source_provenance_audit(
        valuation,
        promotion_rows=promotion_rows,
        recovered_source_ids=recovered_source_ids,
    )
    direct_history = valuation.loc[valuation.get("source", pd.Series(index=valuation.index, dtype=str)).eq(DIRECT_SOURCE_ID)].copy()
    recovered_history = valuation.loc[
        valuation.get("source", pd.Series(index=valuation.index, dtype=str)).isin(recovered_source_ids)
    ].copy()

    universe_periods = build_universe_period_audit(direct_history, policy)
    membership_changes = build_membership_change_audit(direct_history)
    identity_episodes = build_identity_episode_audit(direct_history)
    calendar_file_audit = audit_industry_history_files(
        industry_history_dir,
        trading_calendar,
        as_of=audit_as_of,
        max_stale_days=int(policy["max_history_stale_calendar_days"]),
        long_gap_days=int(policy["long_gap_calendar_days"]),
    )
    industry_governance, governed_audits, _ = industry_history_methodology.run_audit(
        history_dir=industry_history_dir,
        snapshot_dir=industry_snapshot_dir,
        classification_history_path=classification_history_path,
        as_of=audit_as_of,
        required_industry_count=int(policy["required_industry_file_count"]),
        minimum_fresh_industry_count=int(policy["minimum_fresh_industry_file_count"]),
        max_stale_days=int(policy["max_history_stale_calendar_days"]),
    )
    governed_file_audit = pd.DataFrame([row.output_dict() for row in governed_audits])
    if not governed_file_audit.empty:
        governed_file_audit = governed_file_audit.rename(
            columns={column: f"governed_{column}" for column in governed_file_audit.columns if column != "industry_code"}
        )
        industry_file_audit = calendar_file_audit.merge(governed_file_audit, on="industry_code", how="outer")
    else:
        industry_file_audit = calendar_file_audit.copy()
    evidence_labels = build_evidence_set_labels(opportunity, policy)
    descriptive_panel = build_descriptive_panel(opportunity, direct_history)
    metrics = build_three_universe_metrics(descriptive_panel, policy)

    manifest_paths = {
            "policy": policy_path,
            "valuation_history": valuation_path,
            "opportunity_set": opportunity_path,
            "frozen_trading_calendar": calendar_path,
            "classification_history": classification_history_path,
        }
    snapshot_path, _ = industry_history_methodology.latest_dated_snapshot(industry_snapshot_dir, audit_as_of)
    if snapshot_path is not None:
        manifest_paths["industry_current_snapshot"] = snapshot_path
    manifest_paths.update(
        {f"industry_history:{path.name}": path for path in sorted(industry_history_dir.glob("*.csv"))}
    )
    input_manifest = build_input_manifest(manifest_paths)
    source_fingerprint = sha256_text(
        json.dumps(input_manifest["files"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    direct_max = max_date_text(direct_history.get("trade_date"))
    recovered_max = max_date_text(recovered_history.get("trade_date"))
    field_contract_complete = bool(len(field_contract)) and field_contract["status"].eq("pass").all()
    promotion_count = int(len(promotion_rows))
    calendar_hash = file_sha256(calendar_path)
    identity_reuse = identity_episodes.groupby("industry_code")["identity_episode_id"].nunique() if len(identity_episodes) else pd.Series(dtype=int)
    name_changed_codes = sorted(identity_reuse[identity_reuse.gt(1)].index.astype(str).tolist())
    confirmed_semantic_reuse_codes = ["801951", "801952"]
    required_reuse_detected = set(confirmed_semantic_reuse_codes).issubset(set(name_changed_codes))
    years = set(pd.to_numeric(universe_periods.get("year", pd.Series(dtype=int)), errors="coerce").dropna().astype(int))
    universe_modes = set(metrics.get("universe_mode", pd.Series(dtype=str)).astype(str))
    expected_modes = set(str(item) for item in policy["universe_modes"])
    eligible_sources = set(promotion_rows.get("source", pd.Series(dtype=str)).dropna().astype(str))
    current_snapshot_pollution_excluded = not bool(eligible_sources.intersection(recovered_source_ids))
    required_file_count = int(policy["required_industry_file_count"])
    minimum_fresh_count = int(policy["minimum_fresh_industry_file_count"])
    fresh_file_count = int(industry_governance.get("fresh_history_file_count", 0) or 0)
    long_gap_count = int(industry_governance.get("long_tail_gap_history_file_count", 0) or 0)
    ordinary_stale_codes = sorted(
        calendar_file_audit.loc[calendar_file_audit["tail_gap_status"].eq("stale"), "industry_code"].astype(str).tolist()
    )
    calendar_files_valid = bool(
        len(calendar_file_audit) == required_file_count
        and calendar_file_audit["status"].eq("pass").all()
    )
    governed_universe_valid = bool(
        industry_governance.get("coverage_gate_passed") is True
        and industry_governance.get("freshness_gate_passed") is True
        and industry_governance.get("current_monitoring_gate_passed") is True
    )
    history_reuse = set(
        industry_file_audit.loc[
            industry_file_audit.get("identity_reuse_guard_required", pd.Series(False, index=industry_file_audit.index)),
            "industry_code",
        ].astype(str)
    )

    checks = pd.DataFrame(
        [
            check("required_field_contract_exact", tuple(required_fields) == CORE_PIT_FIELDS, "The six required fields are explicit, ordered, and machine-readable."),
            check("missing_publication_metadata_blocks_promotion", field_contract_complete or promotion_count == 0, f"field_contract_complete={field_contract_complete}; eligible_rows={promotion_count}"),
            check("frozen_trading_calendar_bound", len(trading_calendar) > 0 and calendar_hash == str(policy["frozen_trading_calendar_sha256"]), f"calendar_rows={len(trading_calendar)}; sha256={calendar_hash}"),
            check("recovered_snapshot_rows_quarantined", current_snapshot_pollution_excluded, f"recovered_rows={len(recovered_history)}; recovered_max={recovered_max}; direct_max={direct_max}"),
            check("industry_coverage_and_freshness_separate", calendar_files_valid and governed_universe_valid and fresh_file_count >= minimum_fresh_count, f"files={len(calendar_file_audit)}/{required_file_count}; all_calendar_valid={calendar_files_valid}; snapshot_exact={industry_governance.get('coverage_gate_passed')}; fresh={fresh_file_count}/{minimum_fresh_count}"),
            check("identity_episode_reuse_detected", required_reuse_detected and set(confirmed_semantic_reuse_codes).issubset(history_reuse), f"name_changed_codes={','.join(name_changed_codes)}; confirmed_semantic_reuse={','.join(confirmed_semantic_reuse_codes)}; history_reuse_guard={','.join(sorted(history_reuse))}"),
            check("identity_unsafe_rolling_features_excluded", not set(DESCRIPTIVE_FEATURES).intersection(IDENTITY_UNSAFE_FEATURES_EXCLUDED), f"review_features={','.join(DESCRIPTIVE_FEATURES)}; excluded={','.join(IDENTITY_UNSAFE_FEATURES_EXCLUDED)}"),
            check("long_history_gaps_reported", long_gap_count >= int(policy["minimum_long_gap_file_count"]), f"long_tail_gap_files={long_gap_count}"),
            check("universe_break_years_reported", {2015, 2022, 2023}.issubset(years), f"years={sorted(years)}"),
            check("three_universe_reviews_complete", expected_modes == universe_modes and len(metrics) > 0 and metrics["promotion_eligible"].eq(False).all(), f"modes={sorted(universe_modes)}; rows={len(metrics)}"),
            check("historical_review_label_corrected", len(evidence_labels) > 0 and evidence_labels["promotion_eligible"].eq(False).all() and str(policy["historical_review_set"]["label"]) in set(evidence_labels["evidence_set_label"]), "Historical backtests from 2022 onward never become true-forward evidence."),
            check("classification_history_fails_closed", str(policy.get("classification_history_status")) == "unavailable", "Observed code breaks are reported without claiming verified membership history."),
            check("strategy_parameters_unchanged", policy.get("strategy_parameters_changed") is False, "No factor, threshold, TopN, or strategy version was introduced."),
        ]
    )
    audit_passed = bool(len(checks) and checks["status"].eq("pass").all())
    blocking_reasons = [
        "valuation_publication_timestamp_missing",
        "valuation_available_date_unproved",
        "valuation_source_version_missing",
        "valuation_revision_status_missing",
        "historical_industry_classification_membership_unavailable",
        "historical_beta_identity_episode_recomputation_unverified",
        "historical_pit_and_classification_receipt_verifier_missing",
    ]
    recovery_conditions = [
        "Obtain source-backed published_at, fetched_at, source_version, and revision_status for every historical valuation row.",
        "Validate available_date with the frozen A-share trading calendar and rerun backward as-of joins.",
        "Obtain dated SW membership/classification history and map code reuse into verified identity episodes.",
        "Recompute rolling beta inside verified identity episodes before beta-derived historical metrics can re-enter robustness review.",
        "Bind recovered historical evidence to an immutable manifest and verify that receipt again at every promotion consumer.",
        "Keep 2022+ historical rows in historical_review_used_in_iteration; promotion requires preregistered true-forward evidence from 2026-07-12 onward.",
    ]
    summary = {
        "schema_version": "1.0.0",
        "policy_id": str(policy["policy_id"]),
        "policy_status": "research_only",
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "source_fingerprint": source_fingerprint,
        "audit_check_count": int(len(checks)),
        "audit_pass_count": int(checks["status"].eq("pass").sum()),
        "audit_fail_count": int(checks["status"].ne("pass").sum()),
        "audit_passed": audit_passed,
        "methodology_remediation_complete": audit_passed,
        "valuation_history_row_count": int(len(valuation)),
        "direct_source_row_count": int(len(direct_history)),
        "recovered_snapshot_row_count": int(len(recovered_history)),
        "valuation_raw_max_trade_date": max_date_text(valuation.get("trade_date")),
        "valuation_direct_source_max_trade_date": direct_max,
        "valuation_recovered_snapshot_date": recovered_max,
        "valuation_required_fields": list(required_fields),
        "valuation_missing_required_fields": field_contract.loc[field_contract["status"].ne("pass"), "field"].tolist(),
        "valuation_availability_status": "unavailable_for_promotion" if promotion_count == 0 else "pit_verified_for_promotion",
        "promotion_eligible_valuation_row_count": promotion_count,
        "historical_valuation_pit_gate_passed": promotion_count == len(valuation) and promotion_count > 0,
        "classification_history_status": str(policy.get("classification_history_status", "unavailable")),
        "historical_classification_gate_passed": False,
        "industry_history_file_count": int(len(calendar_file_audit)),
        "industry_history_valid_file_count": int(industry_governance.get("valid_current_history_file_count", 0) or 0),
        "industry_history_fresh_file_count": fresh_file_count,
        "industry_history_long_tail_gap_file_count": long_gap_count,
        "industry_history_ordinary_stale_file_count": int(len(ordinary_stale_codes)),
        "industry_history_ordinary_stale_codes": ordinary_stale_codes,
        "industry_snapshot_code_set_matches": bool(industry_governance.get("coverage_gate_passed")),
        "identity_episode_count": int(len(identity_episodes)),
        "observed_name_episode_count": int(len(identity_episodes)),
        "name_changed_industry_code_count": int(len(name_changed_codes)),
        "name_changed_industry_codes": name_changed_codes,
        "reused_industry_code_count": int(len(confirmed_semantic_reuse_codes)),
        "reused_industry_codes": confirmed_semantic_reuse_codes,
        "historical_beta_identity_safe": False,
        "excluded_identity_unsafe_features": list(IDENTITY_UNSAFE_FEATURES_EXCLUDED),
        "universe_period_count": int(len(universe_periods)),
        "membership_change_count": int(len(membership_changes)),
        "robustness_review_row_count": int(len(metrics)),
        "robustness_universe_modes": sorted(universe_modes),
        "historical_review_set_start": str(policy["historical_review_set"]["start_date"]),
        "historical_review_set_label": str(policy["historical_review_set"]["label"]),
        "true_forward_earliest_evidence_date": str(policy["true_forward_boundary"]["earliest_evidence_date"]),
        "true_forward_required_registration_status": str(policy["true_forward_boundary"]["required_registration_status"]),
        "legacy_oos_label_corrected": True,
        "promotion_gate_passed": False,
        "goal_state": "blocked_external_data",
        "blocking_reasons": blocking_reasons,
        "recovery_conditions": recovery_conditions,
        "can_claim_strong_rebound_industries": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "current_action_required": "NO_ACTION",
        "final_verdict": "方法论整改已完成并失败关闭：历史估值缺少可验证发布时间/版本，历史行业分类成员表缺失，旧结果只能作为描述性历史审查，不能晋级。",
    }
    return AuditArtifacts(
        summary=summary,
        metrics=metrics,
        checks=checks,
        debug_tables={
            "valuation_field_contract.csv": field_contract,
            "valuation_source_provenance.csv": provenance,
            "universe_period_audit.csv": universe_periods,
            "universe_membership_changes.csv": membership_changes,
            "identity_episodes.csv": identity_episodes,
            "industry_history_file_audit.csv": industry_file_audit,
            "evidence_set_labels.csv": evidence_labels,
            "universe_robustness_metrics.csv": metrics,
            "methodology_checks.csv": checks,
        },
        input_manifest=input_manifest,
    )


def build_field_contract_audit(frame: pd.DataFrame, required_fields: Sequence[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for field in required_fields:
        present = field in frame.columns
        non_empty = int(non_empty_mask(frame[field]).sum()) if present else 0
        rows.append(
            {
                "field": field,
                "required_for_promotion": True,
                "present": present,
                "non_empty_row_count": non_empty,
                "total_row_count": int(len(frame)),
                "status": "pass" if present and len(frame) > 0 and non_empty == len(frame) else "blocked",
                "missing_mechanism": "" if present and non_empty == len(frame) else "source_does_not_provide_field_or_value",
            }
        )
    return pd.DataFrame(rows)


def validate_policy(policy: Mapping[str, Any]) -> None:
    required = tuple(str(item) for item in policy.get("required_valuation_fields", ()))
    if required != CORE_PIT_FIELDS:
        raise ValueError(f"required_valuation_fields must equal the ordered six-field contract: {CORE_PIT_FIELDS}")
    governance = tuple(str(item) for item in policy.get("required_governance_fields", ()))
    if governance != GOVERNANCE_PIT_FIELDS:
        raise ValueError(f"required_governance_fields must equal: {GOVERNANCE_PIT_FIELDS}")
    modes = tuple(str(item) for item in policy.get("universe_modes", ()))
    if modes != UNIVERSE_MODES:
        raise ValueError(f"universe_modes must equal the three governed review modes: {UNIVERSE_MODES}")
    digest = str(policy.get("frozen_trading_calendar_sha256", ""))
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("frozen_trading_calendar_sha256 must be a lowercase SHA-256 digest")
    if not policy.get("recovered_source_ids"):
        raise ValueError("recovered_source_ids must not be empty")
    if policy.get("auto_execution_allowed") is not False or policy.get("strategy_parameters_changed") is not False:
        raise ValueError("methodology audit must remain research-only with execution and strategy changes disabled")
    if str(policy.get("historical_review_set", {}).get("label")) != "historical_review_used_in_iteration":
        raise ValueError("legacy OOS evidence must be downgraded to historical_review_used_in_iteration")
    date.fromisoformat(str(policy["audit_as_of_date"]))


def promotion_eligible_rows(
    frame: pd.DataFrame,
    trading_calendar: Sequence[date] | set[date],
    required_fields: Sequence[str],
    *,
    recovered_source_ids: Sequence[str] = (RECOVERED_SOURCE_ID,),
    allowed_revision_statuses: Sequence[str] = ("original", "restated", "superseded"),
    required_governance_fields: Sequence[str] = GOVERNANCE_PIT_FIELDS,
    artifact_root: Path = ROOT,
) -> pd.DataFrame:
    if (
        frame.empty
        or tuple(required_fields) != CORE_PIT_FIELDS
        or any(field not in frame.columns for field in required_fields)
        or any(field not in frame.columns for field in required_governance_fields)
    ):
        return frame.iloc[0:0].copy()
    trading_dates = tuple(sorted(set(trading_calendar)))
    trading_set = set(trading_dates)
    mask = pd.Series(True, index=frame.index)
    for field in required_fields:
        mask &= non_empty_mask(frame[field])
    trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    available_dates = pd.to_datetime(frame["available_date"], errors="coerce").dt.date
    mask &= trade_dates.isin(trading_set)
    mask &= available_dates.isin(trading_set)
    row_time_ok: list[bool] = []
    for trade_date, available_date, published_value, fetched_value in zip(
        trade_dates,
        available_dates,
        frame["published_at"],
        frame["fetched_at"],
        strict=True,
    ):
        published = parse_aware_timestamp(published_value)
        fetched = parse_aware_timestamp(fetched_value)
        if published is None or fetched is None or fetched < published or pd.isna(trade_date) or pd.isna(available_date):
            row_time_ok.append(False)
            continue
        if published.date() < trade_date:
            row_time_ok.append(False)
            continue
        try:
            expected_available = first_eligible_trade_date(published, trading_dates)
        except ValueError:
            row_time_ok.append(False)
            continue
        row_time_ok.append(available_date == expected_available)
    mask &= pd.Series(row_time_ok, index=frame.index)
    revisions = frame["revision_status"].fillna("").astype(str)
    mask &= revisions.isin(set(allowed_revision_statuses))
    source_hash = frame["source_hash"].fillna("").astype(str)
    mask &= source_hash.str.fullmatch(r"[0-9a-f]{64}")
    mask &= frame["source_version"].fillna("").astype(str).eq("sha256:" + source_hash)
    mask &= frame["data_status"].fillna("").astype(str).eq("pit_verified")
    mask &= frame["availability_basis"].fillna("").astype(str).isin(
        {"source_publication_timestamp", "vendor_available_timestamp"}
    )
    mask &= frame["source"].fillna("").astype(str).str.strip().ne("")
    mask &= ~frame["source"].fillna("").astype(str).isin(set(recovered_source_ids))
    duplicates = frame.duplicated(["industry_code", "trade_date", "source_version", "revision_status"], keep=False)
    mask &= ~duplicates
    active_statuses = {"original", "restated"}
    active = frame.loc[revisions.isin(active_statuses)]
    active_counts = active.groupby(["industry_code", "trade_date"], dropna=False).size()
    key_ok = pd.Series(
        [int(active_counts.get((code, trade_date), 0)) == 1 for code, trade_date in zip(frame["industry_code"], frame["trade_date"], strict=True)],
        index=frame.index,
    )
    mask &= key_ok
    mask &= revision_chain_valid_mask(frame)
    mask &= source_artifact_valid_mask(frame, artifact_root=artifact_root)
    return frame.loc[mask].copy()


def parse_aware_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None or pd.isna(value):
        return None
    try:
        parsed = pd.Timestamp(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.tz_convert(SHANGHAI)


def first_eligible_trade_date(published_at: Any, trading_calendar: Sequence[date]) -> date:
    published = published_at if isinstance(published_at, pd.Timestamp) else parse_aware_timestamp(published_at)
    if published is None:
        raise ValueError("published_at must be timezone-aware")
    dates = tuple(trading_calendar)
    if published.date() in set(dates) and published.time().replace(tzinfo=None) < DAILY_DECISION_CUTOFF:
        return published.date()
    for candidate in dates:
        if candidate > published.date():
            return candidate
    raise ValueError(f"frozen trading calendar has no session after {published.date()}")


def build_source_provenance_audit(
    frame: pd.DataFrame,
    *,
    promotion_rows: pd.DataFrame | None = None,
    recovered_source_ids: Sequence[str] = (RECOVERED_SOURCE_ID,),
) -> pd.DataFrame:
    if "source" not in frame.columns or frame.empty:
        return pd.DataFrame(columns=["source", "row_count", "trade_date_min", "trade_date_max", "code_count", "missing_close_count", "promotion_eligible"])
    rows: list[dict[str, Any]] = []
    eligible_index = set(promotion_rows.index) if promotion_rows is not None else set()
    for source, group in frame.groupby("source", dropna=False, sort=True):
        close = pd.to_numeric(group.get("close_index", pd.Series(index=group.index, dtype=float)), errors="coerce")
        rows.append(
            {
                "source": str(source),
                "row_count": int(len(group)),
                "trade_date_min": min_date_text(group.get("trade_date")),
                "trade_date_max": max_date_text(group.get("trade_date")),
                "code_count": int(group["industry_code"].nunique()),
                "missing_close_count": int(close.isna().sum()),
                "promotion_eligible": bool(set(group.index).intersection(eligible_index)),
                "status": "quarantined_recovered_snapshot" if str(source) in set(recovered_source_ids) else ("pit_verified" if set(group.index).intersection(eligible_index) else "descriptive_only_missing_publication_metadata"),
            }
        )
    return pd.DataFrame(rows)


def build_universe_period_audit(frame: pd.DataFrame, policy: Mapping[str, Any]) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["year", "first_date", "last_date", "unique_code_count", "daily_min", "daily_median", "daily_max"])
    work = frame.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
    work = work.dropna(subset=["trade_date"])
    segments = list(policy["classification_break_segments"])
    rows: list[dict[str, Any]] = []
    for year, group in work.groupby(work["trade_date"].dt.year, sort=True):
        daily = group.groupby("trade_date")["industry_code"].nunique()
        first = group["trade_date"].min().date()
        last = group["trade_date"].max().date()
        segment_ids = sorted({segment_for_date(value.date(), segments) for value in group["trade_date"].drop_duplicates()})
        rows.append(
            {
                "year": int(year),
                "first_date": first.isoformat(),
                "last_date": last.isoformat(),
                "unique_code_count": int(group["industry_code"].nunique()),
                "daily_min": int(daily.min()),
                "daily_median": float(daily.median()),
                "daily_max": int(daily.max()),
                "row_count": int(len(group)),
                "segment_ids": ";".join(segment_ids),
                "classification_history_status": "unavailable",
                "classification_change_claim": "observed_code_universe_break_only",
                "missing_mechanism": "membership_change_and_source_missingness_not_separable",
                "benchmark_comparable_to_other_periods": False,
            }
        )
    return pd.DataFrame(rows)


def build_membership_change_audit(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["trade_date", "previous_trade_date", "code_count", "previous_code_count", "entered_count", "exited_count", "entered_codes", "exited_codes", "change_interpretation"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    work = frame.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
    work = work.dropna(subset=["trade_date"])
    grouped = [(stamp.date(), set(group["industry_code"].astype(str))) for stamp, group in work.groupby("trade_date", sort=True)]
    rows: list[dict[str, Any]] = []
    for (previous_date, previous), (current_date, current) in zip(grouped, grouped[1:], strict=False):
        entered = sorted(current - previous)
        exited = sorted(previous - current)
        if not entered and not exited:
            continue
        rows.append(
            {
                "trade_date": current_date.isoformat(),
                "previous_trade_date": previous_date.isoformat(),
                "code_count": len(current),
                "previous_code_count": len(previous),
                "entered_count": len(entered),
                "exited_count": len(exited),
                "entered_codes": ";".join(entered),
                "exited_codes": ";".join(exited),
                "change_interpretation": "observed_change_unverified_classification_or_missingness",
            }
        )
    return pd.DataFrame(rows, columns=columns)


def build_identity_episode_audit(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["industry_code", "identity_episode_id", "industry_name", "first_date", "last_date", "row_count", "cross_episode_rolling_allowed", "identity_status"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    work = frame.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
    work = work.dropna(subset=["trade_date"]).sort_values(["industry_code", "trade_date"])
    rows: list[dict[str, Any]] = []
    for code, group in work.groupby("industry_code", sort=True):
        names = group["industry_name"].fillna("").astype(str)
        episode = names.ne(names.shift()).cumsum()
        for episode_number, episode_group in group.groupby(episode, sort=True):
            name = str(episode_group["industry_name"].iloc[0])
            rows.append(
                {
                    "industry_code": str(code),
                    "identity_episode_id": f"{code}::{int(episode_number)}",
                    "industry_name": name,
                    "first_date": episode_group["trade_date"].min().date().isoformat(),
                    "last_date": episode_group["trade_date"].max().date().isoformat(),
                    "row_count": int(len(episode_group)),
                    "cross_episode_rolling_allowed": False,
                    "identity_status": "observed_name_episode_unverified_membership_history",
                }
            )
    return pd.DataFrame(rows, columns=columns)


def audit_industry_history_files(
    directory: Path,
    trading_calendar: Sequence[date] | set[date],
    *,
    as_of: date = date(2026, 7, 18),
    max_stale_days: int = 4,
    long_gap_days: int = 365,
) -> pd.DataFrame:
    columns = [
        "industry_code", "path", "row_count", "first_date", "latest_date", "latest_age_calendar_days",
        "duplicate_date_count", "non_calendar_date_count", "filename_code_matches", "fresh_as_of_policy_date",
        "tail_gap_status", "max_internal_gap_calendar_days", "long_internal_gap_count",
        "identity_reuse_guard_required", "identity_episode_count", "cross_episode_boundary_count",
        "cross_episode_return_allowed", "status",
    ]
    rows: list[dict[str, Any]] = []
    calendar_set = set(trading_calendar)
    for path in sorted(directory.glob("*.csv")) if directory.exists() else []:
        frame = read_csv(path)
        date_column = next((item for item in ("日期", "trade_date", "date") if item in frame.columns), "")
        code_column = next((item for item in ("代码", "industry_code", "code") if item in frame.columns), "")
        dates = pd.to_datetime(frame[date_column], errors="coerce").dt.date if date_column else pd.Series(dtype=object)
        codes = frame[code_column].astype(str).str.replace(".0", "", regex=False).str.zfill(6) if code_column else pd.Series(dtype=str)
        latest = max((value for value in dates if pd.notna(value)), default=None)
        first = min((value for value in dates if pd.notna(value)), default=None)
        duplicate_count = int(dates.duplicated(keep=False).sum())
        valid_dates = sorted(value for value in dates if pd.notna(value))
        non_calendar = int(sum(value not in calendar_set for value in valid_dates))
        filename_match = bool(len(codes)) and set(codes.dropna()) == {path.stem.zfill(6)}
        age = (as_of - latest).days if latest is not None else None
        fresh = age is not None and 0 <= age <= max_stale_days
        gaps = [(right - left).days for left, right in zip(valid_dates, valid_dates[1:], strict=False)]
        long_internal_gaps = [gap for gap in gaps if gap > long_gap_days]
        reuse_required = path.stem.zfill(6) in {"801951", "801952"}
        episode_ids = [history_identity_episode(path.stem.zfill(6), value) for value in valid_dates]
        assigned_episodes = {value for value in episode_ids if value}
        cross_episode_boundaries = sum(
            left is not None and right is not None and left != right
            for left, right in zip(episode_ids, episode_ids[1:], strict=False)
        )
        status = "pass" if date_column and filename_match and duplicate_count == 0 and non_calendar == 0 else "fail"
        rows.append(
            {
                "industry_code": path.stem.zfill(6),
                "path": normalize_path(path),
                "row_count": int(len(frame)),
                "first_date": first.isoformat() if first else "",
                "latest_date": latest.isoformat() if latest else "",
                "latest_age_calendar_days": age if age is not None else "",
                "duplicate_date_count": duplicate_count,
                "non_calendar_date_count": non_calendar,
                "filename_code_matches": filename_match,
                "fresh_as_of_policy_date": fresh,
                "tail_gap_status": "fresh" if fresh else ("long_tail_gap" if age is not None and age > long_gap_days else "stale"),
                "max_internal_gap_calendar_days": max(gaps, default=0),
                "long_internal_gap_count": len(long_internal_gaps),
                "identity_reuse_guard_required": reuse_required,
                "identity_episode_count": len(assigned_episodes),
                "cross_episode_boundary_count": cross_episode_boundaries,
                "cross_episode_return_allowed": False,
                "status": status,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def history_identity_episode(industry_code: str, trade_date: date) -> str | None:
    code = str(industry_code).zfill(6)
    if code not in {"801951", "801952"}:
        return f"{code}:observed_series"
    if trade_date <= date(2017, 1, 20):
        return f"{code}:legacy_episode"
    if trade_date >= date(2021, 12, 13):
        return f"{code}:sw2021_episode"
    return None


def build_evidence_set_labels(opportunity: pd.DataFrame, policy: Mapping[str, Any]) -> pd.DataFrame:
    dates = pd.to_datetime(opportunity.get("signal_date", pd.Series(dtype=str)), errors="coerce").dropna().drop_duplicates().sort_values()
    review_start = date.fromisoformat(str(policy["historical_review_set"]["start_date"]))
    rows: list[dict[str, Any]] = []
    for stamp in dates:
        signal_date = stamp.date()
        label = str(policy["historical_review_set"]["label"]) if signal_date >= review_start else "historical_development_set"
        matching = opportunity.loc[pd.to_datetime(opportunity.get("signal_date"), errors="coerce").dt.date.eq(signal_date)]
        old_label_column = next((column for column in ("evidence_set_label", "evidence_label", "oos_label") if column in matching.columns), "")
        source_label = ";".join(sorted(set(matching[old_label_column].dropna().astype(str)))) if old_label_column else ""
        rows.append(
            {
                "signal_date": signal_date.isoformat(),
                "source_evidence_label": source_label,
                "evidence_set_label": label,
                "legacy_oos_downgraded": signal_date >= review_start and source_label.lower() in {"oos", "independent_oos", "out_of_sample"},
                "evidence_route": "historical_backtest",
                "promotion_eligible": False,
                "independent_oos": False,
                "required_for_promotion": "preregistered true-forward observation outside historical backtest outputs",
            }
        )
    return pd.DataFrame(rows)


def build_descriptive_panel(opportunity: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    if opportunity.empty or history.empty:
        return pd.DataFrame()
    left = normalize_identity_columns(opportunity)
    right = normalize_identity_columns(history)
    left["signal_date"] = pd.to_datetime(left["signal_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    right["trade_date"] = pd.to_datetime(right["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    keep = [column for column in ("trade_date", "industry_code", "industry_name", "pe", "pb", "dividend_yield", "source") if column in right.columns]
    panel = left.merge(right[keep], left_on=["signal_date", "industry_code"], right_on=["trade_date", "industry_code"], how="left", suffixes=("", "_valuation"))
    pieces: list[pd.DataFrame] = []
    for _, event in panel.groupby(["signal_date", "entry_date", "exit_date"], sort=True):
        event = event.copy()
        event["low_pb_rank"] = pd.to_numeric(event.get("pb"), errors="coerce").rank(pct=True, ascending=False)
        event["low_pe_rank"] = pd.to_numeric(event.get("pe"), errors="coerce").rank(pct=True, ascending=False)
        event["dividend_yield_rank"] = pd.to_numeric(event.get("dividend_yield"), errors="coerce").rank(pct=True)
        event["beta_low_pb_score"] = 0.70 * pd.to_numeric(event.get("beta_120_rank"), errors="coerce") + 0.30 * event["low_pb_rank"]
        pieces.append(event)
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()


def build_three_universe_metrics(panel: pd.DataFrame, policy: Mapping[str, Any]) -> pd.DataFrame:
    columns = [
        "feature", "universe_mode", "segment_id", "event_count", "observation_count", "industry_count_min", "industry_count_max",
        "mean_rank_ic", "rank_ic_ci_low", "rank_ic_ci_high", "top20_hit_rate", "mean_relative_return", "relative_return_ci_low",
        "relative_return_ci_high", "mean_benchmark_return", "mean_full_universe_benchmark", "benchmark_difference", "uncertainty",
        "evidence_status", "promotion_eligible", "record_semantics",
    ]
    if panel.empty:
        return pd.DataFrame(columns=columns)
    valid_events = [group for _, group in panel.groupby(["signal_date", "entry_date", "exit_date"], sort=True)]
    common_codes: set[str] | None = None
    for event in valid_events:
        codes = set(event.loc[event["future_return"].notna(), "industry_code"].astype(str))
        common_codes = codes if common_codes is None else common_codes & codes
    common_codes = common_codes or set()
    rows: list[dict[str, Any]] = []
    for feature in DESCRIPTIVE_FEATURES:
        rows.append(metric_row(panel, panel, feature, "then_observed_active_codes", "all", float(policy["transaction_cost_rate"])))
        common = panel.loc[panel["industry_code"].astype(str).isin(common_codes)].copy()
        rows.append(metric_row(common, panel, feature, "common_survivor_observed_codes", "all", float(policy["transaction_cost_rate"])))
        signal_dates = pd.to_datetime(panel["signal_date"], errors="coerce").dt.date
        for segment in policy["classification_break_segments"]:
            start = date.fromisoformat(str(segment["start_date"]))
            end = date.fromisoformat(str(segment["end_date"])) if segment.get("end_date") else date.max
            selected = panel.loc[(signal_dates >= start) & (signal_dates <= end)].copy()
            rows.append(metric_row(selected, panel, feature, "classification_break_segments", str(segment["segment_id"]), float(policy["transaction_cost_rate"])))
    result = pd.DataFrame(rows, columns=columns)
    result["promotion_eligible"] = False
    result["record_semantics"] = "methodology_robustness_review_not_investment_candidate"
    return result


def metric_row(
    selected_panel: pd.DataFrame,
    full_panel: pd.DataFrame,
    feature: str,
    universe_mode: str,
    segment_id: str,
    transaction_cost: float,
) -> dict[str, Any]:
    event_metrics: list[dict[str, float]] = []
    counts: list[int] = []
    observations = 0
    for keys, event in selected_panel.groupby(["signal_date", "entry_date", "exit_date"], sort=True):
        event = event.dropna(subset=[feature, "future_return"]).copy()
        if len(event) < 2:
            continue
        full_event = full_panel
        for column, value in zip(("signal_date", "entry_date", "exit_date"), keys, strict=True):
            full_event = full_event.loc[full_event[column].eq(value)]
        full_returns = pd.to_numeric(full_event["future_return"], errors="coerce").dropna()
        returns = pd.to_numeric(event["future_return"], errors="coerce")
        features = pd.to_numeric(event[feature], errors="coerce")
        rank_ic = float(features.rank(method="average").corr(returns.rank(method="average")))
        top_count = max(1, math.ceil(len(event) * 0.20))
        selected = event.assign(_feature=features, _return=returns).nlargest(top_count, "_feature")
        return_cut = float(returns.quantile(0.80))
        benchmark = float(returns.mean())
        full_benchmark = float(full_returns.mean()) if len(full_returns) else math.nan
        event_metrics.append(
            {
                "rank_ic": rank_ic,
                "top20_hit": float(selected["_return"].ge(return_cut).mean()),
                "relative_return": float(selected["_return"].mean()) - transaction_cost - benchmark,
                "benchmark": benchmark,
                "full_benchmark": full_benchmark,
            }
        )
        counts.append(int(len(event)))
        observations += int(len(event))
    metrics = pd.DataFrame(event_metrics)
    rank_low, rank_high = mean_ci(metrics.get("rank_ic", pd.Series(dtype=float)))
    relative_low, relative_high = mean_ci(metrics.get("relative_return", pd.Series(dtype=float)))
    event_count = int(len(metrics))
    return {
        "feature": feature,
        "universe_mode": universe_mode,
        "segment_id": segment_id,
        "event_count": event_count,
        "observation_count": observations,
        "industry_count_min": min(counts, default=0),
        "industry_count_max": max(counts, default=0),
        "mean_rank_ic": safe_mean(metrics.get("rank_ic")),
        "rank_ic_ci_low": rank_low,
        "rank_ic_ci_high": rank_high,
        "top20_hit_rate": safe_mean(metrics.get("top20_hit")),
        "mean_relative_return": safe_mean(metrics.get("relative_return")),
        "relative_return_ci_low": relative_low,
        "relative_return_ci_high": relative_high,
        "mean_benchmark_return": safe_mean(metrics.get("benchmark")),
        "mean_full_universe_benchmark": safe_mean(metrics.get("full_benchmark")),
        "benchmark_difference": safe_mean(metrics.get("benchmark")) - safe_mean(metrics.get("full_benchmark")) if event_count else math.nan,
        "uncertainty": "descriptive_only_missing_pit_and_classification_proof",
        "evidence_status": "historical_descriptive_review",
        "promotion_eligible": False,
        "record_semantics": "methodology_robustness_review_not_investment_candidate",
    }


def mean_ci(values: pd.Series | None) -> tuple[float, float]:
    if values is None:
        return math.nan, math.nan
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return math.nan, math.nan
    mean = float(clean.mean())
    if len(clean) < 2:
        return mean, mean
    half = 1.96 * float(clean.std(ddof=1)) / math.sqrt(len(clean))
    return mean - half, mean + half


def safe_mean(values: pd.Series | None) -> float:
    if values is None:
        return math.nan
    clean = pd.to_numeric(values, errors="coerce").dropna()
    return float(clean.mean()) if len(clean) else math.nan


def segment_for_date(value: date, segments: Sequence[Mapping[str, Any]]) -> str:
    for segment in segments:
        start = date.fromisoformat(str(segment["start_date"]))
        end = date.fromisoformat(str(segment["end_date"])) if segment.get("end_date") else date.max
        if start <= value <= end:
            return str(segment["segment_id"])
    return "outside_declared_segments"


def build_input_manifest(paths: Mapping[str, Path]) -> dict[str, Any]:
    files = []
    for source_id, path in sorted(paths.items()):
        files.append(
            {
                "source_id": source_id,
                "path": normalize_path(path),
                "exists": path.is_file(),
                "bytes": path.stat().st_size if path.is_file() else 0,
                "sha256": file_sha256(path) if path.is_file() else "",
            }
        )
    return {"schema_version": "1.0.0", "files": files}


def write_outputs(output_dir: Path, artifacts: AuditArtifacts) -> None:
    debug = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug.mkdir(parents=True, exist_ok=True)
    artifacts.metrics.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(output_dir / "run_summary.json", artifacts.summary)
    (output_dir / "report.md").write_text(render_report(artifacts), encoding="utf-8")
    for name, table in artifacts.debug_tables.items():
        table.to_csv(debug / name, index=False, encoding="utf-8-sig")
    write_json(debug / "input_manifest.json", artifacts.input_manifest)
    debug_files = sorted([*artifacts.debug_tables, "input_manifest.json", "structure_manifest.json"])
    artifact_paths = [
        output_dir / "report.md",
        output_dir / "run_summary.json",
        output_dir / "top_candidates.csv",
        *[debug / name for name in sorted([*artifacts.debug_tables, "input_manifest.json"])],
    ]
    write_json(
        debug / "structure_manifest.json",
        {
            "top_level_exact": sorted(EXPECTED_TOP_LEVEL),
            "debug_files": debug_files,
            "top_candidates_semantics": "methodology_robustness_review_not_investment_candidate",
            "artifact_sha256": {
                path.relative_to(output_dir).as_posix(): file_sha256(path)
                for path in artifact_paths
            },
        },
    )


def validate_existing_outputs(output_dir: Path, expected: AuditArtifacts) -> None:
    if not output_dir.is_dir():
        raise ValueError(f"missing audit output: {output_dir}")
    actual_top = {path.name for path in output_dir.iterdir()}
    if actual_top != EXPECTED_TOP_LEVEL:
        raise ValueError(f"invalid top-level structure: {sorted(actual_top)}")
    debug = output_dir / "debug"
    structure = read_json(debug / "structure_manifest.json")
    expected_debug = sorted([*expected.debug_tables, "input_manifest.json", "structure_manifest.json"])
    actual_debug = sorted(path.name for path in debug.iterdir())
    if actual_debug != expected_debug or structure.get("debug_files") != expected_debug:
        raise ValueError(f"invalid debug structure: {actual_debug}")
    if structure.get("top_level_exact") != sorted(EXPECTED_TOP_LEVEL):
        raise ValueError("structure manifest top-level contract mismatch")
    for relative, digest in structure.get("artifact_sha256", {}).items():
        path = output_dir / str(relative)
        if not path.is_file() or file_sha256(path) != digest:
            raise ValueError(f"artifact hash mismatch: {relative}")
    summary = read_json(output_dir / "run_summary.json")
    stable_summary = dict(summary)
    stable_expected = dict(expected.summary)
    stable_summary.pop("generated_at", None)
    stable_expected.pop("generated_at", None)
    if stable_summary != stable_expected:
        raise ValueError("stale audit summary does not match current inputs")
    if (output_dir / "top_candidates.csv").read_bytes() != dataframe_csv_bytes(expected.metrics):
        raise ValueError("top_candidates.csv content does not match current inputs")
    for name, table in expected.debug_tables.items():
        if (debug / name).read_bytes() != dataframe_csv_bytes(table):
            raise ValueError(f"debug artifact does not match current inputs: {name}")
    if read_json(debug / "input_manifest.json") != expected.input_manifest:
        raise ValueError("input manifest does not match current inputs")
    if (output_dir / "report.md").read_text(encoding="utf-8") != render_report(expected):
        raise ValueError("report does not match current inputs")
    if (
        summary.get("audit_passed") is not True
        or summary.get("promotion_gate_passed") is not False
        or summary.get("production_ready") is not False
        or summary.get("auto_execution_allowed") is not False
    ):
        raise ValueError("audit must pass while promotion remains fail-closed")


def dataframe_csv_bytes(frame: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    frame.to_csv(buffer, index=False, encoding="utf-8-sig")
    return buffer.getvalue()


def render_report(artifacts: AuditArtifacts) -> str:
    summary = artifacts.summary
    metrics = artifacts.metrics
    periods = artifacts.debug_tables["universe_period_audit.csv"]
    provenance = artifacts.debug_tables["valuation_source_provenance.csv"]
    return "\n".join(
        [
            "# PIT 估值与历史行业宇宙方法论整改审计",
            "",
            summary["final_verdict"],
            "",
            "## 估值可见性",
            "",
            f"- 原始估值行：{summary['valuation_history_row_count']}",
            f"- 可直接追溯日报行：{summary['direct_source_row_count']}，截止 `{summary['valuation_direct_source_max_trade_date']}`",
            f"- 恢复型快照行：{summary['recovered_snapshot_row_count']}，日期 `{summary['valuation_recovered_snapshot_date']}`；已隔离",
            f"- 可用于晋级的 PIT 估值行：{summary['promotion_eligible_valuation_row_count']}",
            f"- 估值门禁：`{'pass' if summary['historical_valuation_pit_gate_passed'] else 'blocked'}`",
            "",
            "缺失真实 `published_at`、`available_date`、`fetched_at`、`source_version` 或 `revision_status` 的记录，不得用 trade_date 或固定自然日滞后代替。",
            "",
            "## 行业宇宙断点",
            "",
            periods.to_markdown(index=False) if len(periods) else "无可用行业宇宙记录。",
            "",
            "上述变化只能证明本地源的代码横截面发生断点；没有历史成分与分类成员表，不能把进入/退出全部解释为正式分类调整。",
            "",
            "## 来源隔离",
            "",
            provenance.to_markdown(index=False) if len(provenance) else "无来源记录。",
            "",
            "## 三种宇宙只读复核",
            "",
            metrics.to_markdown(index=False) if len(metrics) else "无可复核指标。",
            "",
            "这些 RankIC、Top20% 命中、相对收益和基准差异只用于说明口径敏感性；估值发布时间与历史分类仍不可证明，因此所有行 `promotion_eligible=false`。",
            "历史 beta 滚动值尚未按可验证身份 episode 重算，本轮已从三种宇宙指标中排除 `beta_low_pb_score`，不得据此晋级。",
            "",
            "## 证据标签纠正",
            "",
            f"- `{summary['historical_review_set_start']}` 起的历史结果统一标记 `{summary['historical_review_set_label']}`，不再称独立 OOS。",
            f"- 晋级只接受 `{summary['true_forward_earliest_evidence_date']}` 起、状态为 `{summary['true_forward_required_registration_status']}` 的真实前推证据。",
            "- 历史回测、伪前推、当前快照和恢复型代理行均不能自动转为晋级证据。",
            "",
            "## 恢复条件",
            "",
            *[f"- {item}" for item in summary["recovery_conditions"]],
            "",
            "当前动作必须保持 `research_only / NO_ACTION`；本审计不新增因子、阈值、TopN 或策略版本。",
        ]
    )


def normalize_identity_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "industry_code" in out.columns:
        out["industry_code"] = out["industry_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
    return out


def non_empty_mask(values: pd.Series) -> pd.Series:
    return values.notna() & values.astype(str).str.strip().ne("") & values.astype(str).str.lower().ne("nan")


def load_trading_calendar(path: Path) -> tuple[date, ...]:
    frame = read_csv(path)
    if list(frame.columns) != ["trade_date"]:
        raise ValueError("frozen trading calendar must contain only trade_date")
    values = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    if values.isna().any():
        raise ValueError("frozen trading calendar contains invalid dates")
    dates = tuple(values.tolist())
    if not dates or dates != tuple(sorted(set(dates))):
        raise ValueError("frozen trading calendar must be non-empty, unique, and sorted")
    return dates


def check(check_id: str, passed: bool, evidence: str) -> dict[str, Any]:
    return {"check_id": check_id, "status": "pass" if passed else "fail", "evidence": evidence}


def read_csv(path: Path, **kwargs: Any) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(path)
    return pd.read_csv(path, encoding="utf-8-sig", **kwargs)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=json_scalar) + "\n",
        encoding="utf-8",
    )


def json_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def min_date_text(values: pd.Series | None) -> str:
    if values is None:
        return ""
    parsed = pd.to_datetime(values, errors="coerce").dropna()
    return parsed.min().date().isoformat() if len(parsed) else ""


def max_date_text(values: pd.Series | None) -> str:
    if values is None:
        return ""
    parsed = pd.to_datetime(values, errors="coerce").dropna()
    return parsed.max().date().isoformat() if len(parsed) else ""


def self_check() -> None:
    required = ["trade_date", "published_at", "available_date", "fetched_at", "source_version", "revision_status"]
    incomplete = pd.DataFrame({"trade_date": ["2026-07-17"], "industry_code": ["801010"]})
    contract = build_field_contract_audit(incomplete, required)
    assert contract.loc[contract["field"].eq("trade_date"), "status"].iloc[0] == "pass"
    assert contract.loc[contract["field"].eq("published_at"), "status"].iloc[0] == "blocked"
    assert promotion_eligible_rows(incomplete, (date(2026, 7, 17),), required).empty
    history = pd.DataFrame(
        {
            "trade_date": ["2021-12-10", "2021-12-13", "2021-12-10", "2021-12-13"],
            "industry_code": ["801951", "801951", "801952", "801952"],
            "industry_name": ["legacy_a", "new_a", "legacy_b", "new_b"],
        }
    )
    episodes = build_identity_episode_audit(history)
    assert episodes.groupby("industry_code")["identity_episode_id"].nunique().eq(2).all()
    labels = build_evidence_set_labels(
        pd.DataFrame({"signal_date": ["2021-12-31", "2022-01-01"]}),
        {"historical_review_set": {"start_date": "2022-01-01", "label": "historical_review_used_in_iteration"}},
    )
    assert labels["promotion_eligible"].eq(False).all()
    assert labels.iloc[-1]["evidence_set_label"] == "historical_review_used_in_iteration"
    print("self_check=pass")


if __name__ == "__main__":
    main()
