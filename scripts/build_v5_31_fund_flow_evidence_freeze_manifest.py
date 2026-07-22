#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from fund_flow_forward_evidence import record_ledger_checkpoint, verify_ledger_checkpoint
from research_integrity import (
    DuplicateRecordError,
    HashChainLedger,
    atomic_write_csv,
    atomic_write_json,
    atomic_write_text,
    csv_fingerprint,
    file_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
LEGACY_BASELINE = ROOT / "logs" / "v5_31_fund_flow_evidence_freeze_manifest.csv"
BASELINE_DIR = ROOT / "logs" / "v5_31_fund_flow_evidence_freeze"
HISTORY = ROOT / "logs" / "v5_31_fund_flow_evidence_freeze_history.jsonl"
ACTIVE = ROOT / "logs" / "v5_31_fund_flow_evidence_freeze_active.json"
OUT = ROOT / "outputs" / "audit" / "fund_flow_evidence_freeze_manifest_v5_31"
DEBUG = OUT / "debug"

SCRIPT_PATHS = [
    "scripts/research_integrity.py",
    "scripts/fund_flow_forward_evidence.py",
    "scripts/build_v5_10_rebound_leader_goal_completion_audit.py",
    "scripts/build_v5_25_fund_flow_forward_observer.py",
    "scripts/build_v5_26_fund_flow_forward_entry_gate.py",
    "scripts/settle_v5_27_fund_flow_forward_samples.py",
    "scripts/build_v5_28_fund_flow_promotion_evaluator.py",
    "scripts/build_v5_29_fund_flow_evidence_calendar.py",
    "scripts/audit_v5_30_fund_flow_forward_ledger_integrity.py",
    "scripts/build_v5_31_fund_flow_evidence_freeze_manifest.py",
    "scripts/build_v5_32_fund_flow_holding_observation.py",
    "scripts/build_v5_33_fund_flow_entry_price_freeze.py",
    "scripts/build_v5_34_fund_flow_benchmark_entry_freeze.py",
    "scripts/build_v5_35_fund_flow_waiting_room.py",
    "scripts/run_v4_71_live_refresh.py",
]
STATIC_ARTIFACTS = [
    "configs/fund_flow_forward_chain_policy.json",
    "configs/fund_flow_forward_ledger_schema.json",
    "data_catalog/cache/trading_calendar/a_share_trade_calendar.csv",
]
DIRECTORY_ARTIFACTS: list[str] = []


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.31 immutable, cohort-scoped evidence freeze manifest.")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--create-cohort", action="store_true")
    parser.add_argument("--update-baseline", action="store_true", help="Compatibility alias: create a new cohort; existing baselines are never overwritten.")
    parser.add_argument("--cohort-id", default="")
    parser.add_argument("--operator", default="")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    create = args.create_cohort or args.update_baseline
    active_before = read_json(ACTIVE)
    cohort_id = str(args.cohort_id or active_before.get("cohort_id", ""))
    if create:
        if not cohort_id or not args.operator.strip() or not args.reason.strip():
            parser.error("creating a cohort requires --cohort-id, --operator and --reason")
        if cohort_id == str(active_before.get("cohort_id", "")) and baseline_path(cohort_id).exists():
            parser.error("existing cohort baselines are immutable; choose a new --cohort-id")
    elif not cohort_id:
        cohort_id = "missing_active_cohort"

    current = current_manifest()
    manifest_hash = manifest_fingerprint(current)
    baseline_file = baseline_path(cohort_id)
    created = False
    previous = dict(active_before)
    if create:
        if HISTORY.is_file() and HISTORY.stat().st_size > 0:
            verify_ledger_checkpoint(HISTORY)
        if baseline_file.exists():
            parser.error(f"cohort baseline already exists and cannot be overwritten: {baseline_file}")
        atomic_write_csv(baseline_file, current, fieldnames=["artifact_id", "artifact_type", "fingerprint"])
        history_record = append_history(
            cohort_id=cohort_id,
            manifest_hash=manifest_hash,
            manifest_path=str(baseline_file.relative_to(ROOT)).replace("\\", "/"),
            operator=args.operator.strip(),
            reason=args.reason.strip(),
            previous=previous,
        )
        created = True
        atomic_write_json(ACTIVE, {
            "cohort_id": cohort_id,
            "manifest_hash": manifest_hash,
            "manifest_path": str(baseline_file.relative_to(ROOT)).replace("\\", "/"),
            "freeze_passed": False,
            "verification_required": True,
            "created_at_utc": history_record["created_at_utc"],
            "verified_at_utc": "",
            "operator": args.operator.strip(),
            "reason": args.reason.strip(),
            "previous_cohort_id": history_record["previous_cohort_id"],
            "previous_manifest_hash": history_record["previous_manifest_hash"],
            "history_record_hash": history_record["record_hash"],
        })

    baseline_exists = baseline_file.exists()
    baseline = read_manifest(baseline_file) if baseline_exists else []
    comparison = compare(current, baseline)
    summary = build_summary(
        current,
        comparison,
        baseline_exists=baseline_exists,
        created=created,
        cohort_id=cohort_id,
        manifest_hash=manifest_hash,
        baseline_file=baseline_file,
        previous=previous,
    )
    if summary["freeze_passed"]:
        active = read_json(ACTIVE)
        if str(active.get("cohort_id", "")) == cohort_id and str(active.get("manifest_hash", "")) == manifest_hash:
            active = verified_active_pointer(active, utc_now_text())
            atomic_write_json(ACTIVE, active)
            summary["verified_at_utc"] = active["verified_at_utc"]
    else:
        active = read_json(ACTIVE)
        if str(active.get("cohort_id", "")) == cohort_id:
            active.update({
                "freeze_passed": False,
                "verification_required": True,
                "invalidated_at_utc": utc_now_text(),
                "invalidation_reason": "current artifact manifest does not match the immutable cohort baseline",
            })
            atomic_write_json(ACTIVE, active)
    write_outputs(summary, current, baseline, comparison)
    print(f"output_dir={OUT}")
    print(f"cohort_id={cohort_id}")
    print(f"freeze_passed={summary['freeze_passed']}")
    print(f"changed_count={summary['changed_count']}")
    if not summary["freeze_passed"]:
        raise SystemExit(2)


def baseline_path(cohort_id: str) -> Path:
    safe = "".join(char for char in cohort_id if char.isalnum() or char in {"-", "_", "."})
    if not safe or safe != cohort_id:
        raise ValueError("cohort_id may contain only letters, digits, dash, underscore and dot")
    return BASELINE_DIR / safe / "manifest.csv"


def verified_active_pointer(active: Mapping[str, Any], verified_at_utc: str) -> dict[str, Any]:
    """Return a verified pointer with no stale invalidation metadata."""

    result = dict(active)
    result.pop("invalidated_at_utc", None)
    result.pop("invalidation_reason", None)
    result.update({
        "freeze_passed": True,
        "verification_required": False,
        "verified_at_utc": verified_at_utc,
    })
    return result


def current_manifest() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for relative in SCRIPT_PATHS:
        rows.append(artifact_row(relative, "script_sha256"))
    for relative in STATIC_ARTIFACTS:
        rows.append(artifact_row(relative, "evidence_sha256"))
    for relative in DIRECTORY_ARTIFACTS:
        rows.append({
            "artifact_id": relative,
            "artifact_type": "directory_manifest_sha256",
            "fingerprint": directory_fingerprint(ROOT / relative),
        })
    return sorted(rows, key=lambda row: row["artifact_id"])


def artifact_row(relative: str, artifact_type: str) -> dict[str, str]:
    path = ROOT / relative
    return {
        "artifact_id": relative,
        "artifact_type": artifact_type,
        "fingerprint": file_sha256(path) if path.is_file() else "MISSING",
    }


def directory_fingerprint(path: Path) -> str:
    if not path.is_dir():
        return "MISSING"
    rows = [
        {"path": str(item.relative_to(path)).replace("\\", "/"), "sha256": file_sha256(item)}
        for item in sorted(path.rglob("*"))
        if item.is_file() and not item.name.endswith(".lock")
    ]
    return csv_fingerprint(rows, fieldnames=["path", "sha256"], sort_rows_by=["path"])


def manifest_fingerprint(rows: Iterable[Mapping[str, Any]]) -> str:
    return csv_fingerprint(rows, fieldnames=["artifact_id", "artifact_type", "fingerprint"], sort_rows_by=["artifact_id"])


def append_history(
    *,
    cohort_id: str,
    manifest_hash: str,
    manifest_path: str,
    operator: str,
    reason: str,
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "event_type": "cohort_created",
        "event_id": f"cohort:{cohort_id}",
        "created_at_utc": utc_now_text(),
        "cohort_id": cohort_id,
        "manifest_hash": manifest_hash,
        "manifest_path": manifest_path,
        "operator": operator,
        "reason": reason,
        "previous_cohort_id": str(previous.get("cohort_id", "")),
        "previous_manifest_hash": str(previous.get("manifest_hash", "")),
        "previous_history_record_hash": str(previous.get("history_record_hash", "")),
        "legacy_baseline_preserved": LEGACY_BASELINE.exists(),
    }
    try:
        if HISTORY.is_file() and HISTORY.stat().st_size > 0:
            verify_ledger_checkpoint(HISTORY)
        record = HashChainLedger(HISTORY).append(payload, unique_fields=["event_id"])
        record_ledger_checkpoint(HISTORY)
        return record
    except DuplicateRecordError as exc:
        raise ValueError(f"cohort history already contains {cohort_id}") from exc


def compare(current: list[dict[str, str]], baseline: list[dict[str, str]]) -> pd.DataFrame:
    now = {row["artifact_id"]: row for row in current}
    old = {row["artifact_id"]: row for row in baseline}
    rows = []
    for artifact_id in sorted(set(now) | set(old)):
        current_row = now.get(artifact_id, {})
        baseline_row = old.get(artifact_id, {})
        current_fingerprint = str(current_row.get("fingerprint", ""))
        baseline_fingerprint = str(baseline_row.get("fingerprint", ""))
        if not baseline_row:
            status = "fail_added_after_baseline"
        elif not current_row:
            status = "fail_removed_after_baseline"
        elif current_fingerprint != baseline_fingerprint:
            status = "fail_changed"
        else:
            status = "pass"
        rows.append({
            "artifact_id": artifact_id,
            "artifact_type": str(current_row.get("artifact_type") or baseline_row.get("artifact_type", "")),
            "fingerprint": current_fingerprint,
            "baseline_fingerprint": baseline_fingerprint,
            "status": status,
        })
    return pd.DataFrame(rows, columns=["artifact_id", "artifact_type", "fingerprint", "baseline_fingerprint", "status"])


def build_summary(
    current: list[dict[str, str]],
    comparison: pd.DataFrame,
    *,
    baseline_exists: bool,
    created: bool,
    cohort_id: str,
    manifest_hash: str,
    baseline_file: Path,
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    changed = int(comparison["status"].ne("pass").sum()) if len(comparison) else len(current)
    missing = sum(row["fingerprint"] == "MISSING" for row in current)
    freeze_passed = baseline_exists and not created and changed == 0 and missing == 0
    return {
        "version": "5.31.2",
        "policy_id": "fund_flow_evidence_freeze_manifest_v5_31",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cohort_id": cohort_id,
        "manifest_hash": manifest_hash,
        "artifact_count": len(current),
        "baseline_exists": baseline_exists,
        "baseline_created": created,
        "baseline_initialized": created,
        "baseline_updated": False,
        "changed_count": changed,
        "missing_artifact_count": missing,
        "freeze_passed": freeze_passed,
        "verification_required": created or not freeze_passed,
        "baseline_path": str(baseline_file.relative_to(ROOT)).replace("\\", "/"),
        "legacy_baseline_path": str(LEGACY_BASELINE.relative_to(ROOT)).replace("\\", "/"),
        "legacy_baseline_preserved": LEGACY_BASELINE.exists(),
        "previous_cohort_id": str(previous.get("cohort_id", "")),
        "previous_manifest_hash": str(previous.get("manifest_hash", "")),
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_freeze_manifest_passed" if freeze_passed else ("research_only_new_cohort_requires_second_verification" if created else "research_only_freeze_manifest_failed"),
        "final_verdict": "V5.31 当前 cohort 的不可变基线经独立第二次运行验证通过；这不代表已找到强反弹行业。" if freeze_passed else ("V5.31 已创建新的不可变 cohort 基线，但本次不得立即显示通过；必须再次独立运行验证。" if created else "V5.31 缺少不可变基线或发现证据指纹变化，完整性门禁失败。"),
    }


def read_manifest(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    import json

    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


def validated_active_cohort() -> dict[str, Any]:
    """Recompute the active cohort gate instead of trusting its mutable pointer."""

    active = read_json(ACTIVE)
    result = dict(active)
    result["freeze_passed"] = False
    result["validation_reason"] = "active cohort is missing or unverified"
    cohort_id = str(active.get("cohort_id", ""))
    manifest_hash = str(active.get("manifest_hash", ""))
    manifest_path = ROOT / str(active.get("manifest_path", ""))
    if not cohort_id or not manifest_hash or active.get("freeze_passed") is not True or not manifest_path.is_file():
        return result
    current = current_manifest()
    baseline = read_manifest(manifest_path)
    comparison = compare(current, baseline)
    if not current or any(row["fingerprint"] == "MISSING" for row in current):
        result["validation_reason"] = "current manifest has missing artifacts"
        return result
    if manifest_fingerprint(current) != manifest_hash or comparison["status"].ne("pass").any():
        result["validation_reason"] = "current manifest drifted from active cohort baseline"
        return result
    try:
        verify_ledger_checkpoint(HISTORY)
        history = HashChainLedger(HISTORY).verify()
    except Exception as exc:
        result["validation_reason"] = f"cohort history hash chain invalid: {exc}"
        return result
    matching = [
        row for row in history.records
        if str(row.get("cohort_id", "")) == cohort_id
        and str(row.get("manifest_hash", "")) == manifest_hash
        and str(row.get("record_hash", "")) == str(active.get("history_record_hash", ""))
    ]
    if not matching or not history.records or str(history.records[-1].get("record_hash", "")) != str(active.get("history_record_hash", "")):
        result["validation_reason"] = "active cohort is not anchored in the immutable history chain"
        return result
    history_record = matching[0]
    canonical_created_at = str(history_record.get("created_at_utc", ""))
    # Downstream PIT ownership must use the timestamp anchored in the append-only
    # history, never the mutable convenience pointer.
    result["created_at_utc"] = canonical_created_at
    if str(history_record.get("event_type", "")) != "cohort_created":
        result["validation_reason"] = "active cohort history record has the wrong event type"
        return result
    if str(active.get("manifest_path", "")) != str(history_record.get("manifest_path", "")):
        result["validation_reason"] = "active cohort manifest path differs from immutable history"
        return result
    if str(active.get("created_at_utc", "")) != canonical_created_at:
        result["validation_reason"] = "active cohort creation timestamp differs from immutable history"
        return result
    try:
        parsed_created_at = datetime.fromisoformat(canonical_created_at.replace("Z", "+00:00"))
    except ValueError:
        parsed_created_at = None
    if parsed_created_at is None or parsed_created_at.tzinfo is None:
        result["validation_reason"] = "active cohort immutable creation timestamp is invalid"
        return result
    result["freeze_passed"] = True
    result["validation_reason"] = "active cohort baseline and history chain verified"
    return result


def write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    if path.exists():
        raise FileExistsError(f"immutable baseline already exists: {path}")
    atomic_write_csv(path, rows, fieldnames=["artifact_id", "artifact_type", "fingerprint"])


def write_outputs(summary: dict[str, Any], current: list[dict[str, str]], baseline: list[dict[str, str]], comparison: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    atomic_write_csv(OUT / "top_candidates.csv", comparison.fillna("").to_dict("records"), fieldnames=list(comparison.columns))
    atomic_write_json(OUT / "run_summary.json", summary)
    atomic_write_text(OUT / "report.md", render_report(summary, comparison))
    atomic_write_csv(DEBUG / "freeze_comparison.csv", comparison.fillna("").to_dict("records"), fieldnames=list(comparison.columns))
    atomic_write_csv(DEBUG / "current_manifest.csv", current, fieldnames=["artifact_id", "artifact_type", "fingerprint"])
    atomic_write_csv(DEBUG / "baseline_manifest.csv", baseline, fieldnames=["artifact_id", "artifact_type", "fingerprint"])


def render_report(summary: dict[str, Any], comparison: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.31 资金流前推证据不可变 cohort 基线",
        "",
        summary["final_verdict"],
        "",
        f"- cohort：`{summary['cohort_id']}`",
        f"- manifest hash：`{summary['manifest_hash']}`",
        f"- 基线路径：`{summary['baseline_path']}`",
        f"- 本次是否创建新基线：`{str(summary['baseline_created']).lower()}`",
        f"- 旧基线是否保留：`{str(summary['legacy_baseline_preserved']).lower()}`",
        f"- 缺失证据 / 变化数量：{summary['missing_artifact_count']} / {summary['changed_count']}",
        f"- 冻结指纹通过：`{str(summary['freeze_passed']).lower()}`",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        comparison.to_markdown(index=False),
    ])


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def self_check() -> None:
    sample = [
        {"artifact_id": "a", "artifact_type": "script_sha256", "fingerprint": "1"},
        {"artifact_id": "b", "artifact_type": "logical_ledger_sha256", "fingerprint": "2"},
    ]
    same = compare(sample, sample)
    assert same["status"].eq("pass").all()
    changed = compare(sample, [{**sample[0], "fingerprint": "x"}, sample[1]])
    assert int(changed["status"].ne("pass").sum()) == 1
    missing = compare(sample, [])
    missing_summary = build_summary(sample, missing, baseline_exists=False, created=False, cohort_id="c", manifest_hash="m", baseline_file=BASELINE_DIR / "c" / "manifest.csv", previous={})
    assert missing_summary["freeze_passed"] is False
    created_summary = build_summary(sample, same, baseline_exists=True, created=True, cohort_id="c", manifest_hash="m", baseline_file=BASELINE_DIR / "c" / "manifest.csv", previous={})
    assert created_summary["freeze_passed"] is False
    verified_summary = build_summary(sample, same, baseline_exists=True, created=False, cohort_id="c", manifest_hash="m", baseline_file=BASELINE_DIR / "c" / "manifest.csv", previous={})
    assert verified_summary["freeze_passed"] is True
    verified_pointer = verified_active_pointer({"freeze_passed": False, "invalidated_at_utc": "old", "invalidation_reason": "old"}, "now")
    assert verified_pointer["freeze_passed"] is True
    assert "invalidated_at_utc" not in verified_pointer and "invalidation_reason" not in verified_pointer
    print("self_check=pass")


if __name__ == "__main__":
    main()
