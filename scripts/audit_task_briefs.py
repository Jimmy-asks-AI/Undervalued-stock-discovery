#!/usr/bin/env python
"""Fail-closed task-brief and research-governance audit.

The legacy task briefs remain schema-audited.  The V4.72--V5.35/current
governance set is additionally audited against the checked-in research version
inventory.  This module never creates or repairs a task brief.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import build_research_version_inventory as inventory_builder


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "configs" / "fundamental_value_task_brief_schema.json"
DEFAULT_TASK_DIR = ROOT / "strategy_lab" / "agents" / "task_briefs"
DEFAULT_INVENTORY = ROOT / "logs" / "research_version_inventory.json"
DEFAULT_CHANGELOG = ROOT / "logs" / "version_changelog.md"
DEFAULT_ACTIVE_COHORT = ROOT / "logs" / "v5_31_fund_flow_evidence_freeze_active.json"
DEFAULT_OUTPUT = ROOT / "outputs" / "audit" / "task_brief_audit"
STANDARD_OUTPUT_ARTIFACTS = ("report.md", "run_summary.json", "top_candidates.csv", "debug")
REGISTRATION_KINDS = {
    "explicit_post_hoc",
    "preregistered_forward_only",
    "inherits_registered_rule",
    "preregistered_forward_only_inherited",
}


def expected_governance_versions() -> list[str]:
    """Return the one authoritative ordered governance coverage set."""
    return (
        [f"V4.{minor:02d}" for minor in range(72, 100)]
        + [f"V5.{minor:02d}" for minor in range(0, 36)]
        + ["CURRENT_MAINLINE"]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit task briefs and the complete research-governance expected set."
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root.")
    parser.add_argument("--schema", type=Path, help="Task brief schema JSON path.")
    parser.add_argument("--task-dir", type=Path, help="Task brief directory; searched recursively.")
    parser.add_argument("--inventory", type=Path, help="Research version inventory JSON path.")
    parser.add_argument("--changelog", type=Path, help="Version changelog Markdown path.")
    parser.add_argument("--active-cohort", type=Path, help="Active cohort pointer JSON path.")
    parser.add_argument("--output", type=Path, help="Output directory.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)

    if args.self_check:
        self_check()
        return 0

    root = args.root.resolve()
    schema_path = (args.schema or root / DEFAULT_SCHEMA.relative_to(ROOT)).resolve()
    task_dir = (args.task_dir or root / DEFAULT_TASK_DIR.relative_to(ROOT)).resolve()
    inventory_path = (args.inventory or root / DEFAULT_INVENTORY.relative_to(ROOT)).resolve()
    changelog_path = (args.changelog or root / DEFAULT_CHANGELOG.relative_to(ROOT)).resolve()
    active_path = (args.active_cohort or root / DEFAULT_ACTIVE_COHORT.relative_to(ROOT)).resolve()
    output_dir = (args.output or root / DEFAULT_OUTPUT.relative_to(ROOT)).resolve()

    schema = read_json_object(schema_path)
    task_paths = discover_task_briefs(task_dir)
    issues = audit_task_briefs(task_paths, schema, root=root)
    governance_rows: list[dict[str, Any]] = []
    inventory: dict[str, Any] = {}
    if not inventory_path.is_file():
        issues.append(_issue(inventory_path.name, "", "inventory", "error", "research version inventory is missing"))
    else:
        try:
            inventory = read_json_object(inventory_path)
            governance_rows, governance_issues = audit_inventory_governance(
                inventory,
                task_paths,
                root=root,
                changelog_path=changelog_path,
                active_cohort_path=active_path,
            )
            issues.extend(governance_issues)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(_issue(inventory_path.name, "", "inventory", "error", f"inventory audit failed: {exc}"))

    errors = [issue for issue in issues if issue["severity"] == "error"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]
    report = {
        "schema_version": "1.0.0",
        "policy_status": "research_only",
        "schema": relative_or_absolute(schema_path, root),
        "task_dir": relative_or_absolute(task_dir, root),
        "inventory": relative_or_absolute(inventory_path, root),
        "recursive_discovery": True,
        "task_count": len(task_paths),
        "expected_governance_count": len(inventory.get("versions", [])),
        "governance_row_count": len(governance_rows),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "status": "pass" if not errors else "fail",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "auto_generated_briefs": 0,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "task_brief_audit_report.json", report)
    write_csv(
        output_dir / "task_brief_audit_issues.csv",
        issues,
        ["task_file", "task_id", "field", "severity", "message"],
    )

    print(f"tasks={report['task_count']}")
    print(f"expected_governance={report['expected_governance_count']}")
    print(f"errors={report['error_count']}")
    print(f"warnings={report['warning_count']}")
    print(f"status={report['status']}")
    print(f"output={output_dir}")
    return 1 if errors else 0


def discover_task_briefs(task_dir: Path) -> list[Path]:
    """Discover every brief recursively; archives are not silently omitted."""
    return sorted(path for path in task_dir.rglob("*.json") if path.is_file()) if task_dir.is_dir() else []


def audit_task_briefs(
    task_paths: list[Path],
    schema: dict[str, Any],
    *,
    root: Path = ROOT,
) -> list[dict[str, str]]:
    """Run the legacy schema audit, with archive-safe path semantics.

    Archived briefs are still parsed and structurally checked.  Their historical
    outputs are deliberately not required to remain materialized.  Non-archive
    briefs preserve the original path-existence checks.
    """
    issues: list[dict[str, str]] = []
    if not task_paths:
        return [_issue("", "", "task_dir", "error", "no task brief JSON files found")]

    required_keys = set(schema.get("required_top_level_keys", []))
    allowed_status = set(schema.get("allowed_task_status", []))
    known_agents = set(schema.get("known_agents", []))
    acceptance_required = set(schema.get("acceptance_check_required_keys", []))
    seen_ids: set[str] = set()

    for path in task_paths:
        try:
            task = read_json_object(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(_issue(path.name, "", "json", "error", f"invalid JSON object: {exc}"))
            continue

        task_id = str(task.get("task_id", ""))
        missing = required_keys - set(task.keys())
        for key in sorted(missing):
            issues.append(_issue(path.name, task_id, key, "error", "missing required key"))

        if not task_id:
            issues.append(_issue(path.name, task_id, "task_id", "error", "blank task_id"))
        elif task_id in seen_ids:
            issues.append(_issue(path.name, task_id, "task_id", "error", "duplicate task_id"))
        seen_ids.add(task_id)

        owner = str(task.get("owner_agent", ""))
        if owner and owner not in known_agents and not is_archived_brief(path):
            issues.append(_issue(path.name, task_id, "owner_agent", "error", "owner_agent is not in known agent list"))

        status = str(task.get("task_status", ""))
        if status and status not in allowed_status:
            issues.append(_issue(path.name, task_id, "task_status", "error", "task_status is not allowed"))

        for field in ["allowed_input_paths", "forbidden_input_patterns", "required_output_paths", "acceptance_checks"]:
            value = task.get(field, [])
            if not isinstance(value, list) or not value:
                issues.append(_issue(path.name, task_id, field, "error", "field must be a non-empty list"))

        if not is_archived_brief(path):
            for field in ["allowed_input_paths", "required_output_paths"]:
                value = task.get(field, [])
                if not isinstance(value, list):
                    continue
                for rel_path in value:
                    candidate = root / str(rel_path)
                    if not candidate.exists():
                        issues.append(_issue(path.name, task_id, field, "error", f"path does not exist: {rel_path}"))

        checks = task.get("acceptance_checks", [])
        if not isinstance(checks, list):
            checks = []
        for index, check in enumerate(checks, start=1):
            if not isinstance(check, dict):
                issues.append(_issue(path.name, task_id, "acceptance_checks", "error", f"check {index} is not an object"))
                continue
            missing_check_keys = acceptance_required - set(check.keys())
            for key in sorted(missing_check_keys):
                issues.append(_issue(path.name, task_id, "acceptance_checks", "error", f"check {index} missing key: {key}"))
            if check.get("expected_status") != "exit_0":
                issues.append(_issue(path.name, task_id, "acceptance_checks", "warning", f"check {index} expected_status is not exit_0"))

        forbidden = task.get("forbidden_input_patterns", [])
        if isinstance(forbidden, list) and "validated_alpha" not in " ".join(str(value) for value in forbidden):
            issues.append(_issue(path.name, task_id, "forbidden_input_patterns", "warning", "consider forbidding validated_alpha unless this is a promotion task"))

    return issues


def audit_inventory_governance(
    inventory: Mapping[str, Any],
    task_paths: Sequence[Path],
    *,
    root: Path,
    changelog_path: Path,
    active_cohort_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Audit every expected inventory row against live evidence.

    The function deliberately recomputes the decisive gates.  A stale inventory
    cannot turn a missing brief, changelog entry, manifest, or cohort binding
    into a pass.
    """
    issues: list[dict[str, str]] = []
    records_value = inventory.get("versions", [])
    if not isinstance(records_value, list):
        raise ValueError("inventory.versions must be a list")
    records = [record for record in records_value if isinstance(record, dict)]
    if len(records) != len(records_value):
        issues.append(_issue("research_version_inventory.json", "", "versions", "error", "all inventory rows must be objects"))

    versions = [str(record.get("version", "")) for record in records]
    expected_versions = expected_governance_versions()
    blank_count = sum(not version for version in versions)
    if blank_count:
        issues.append(_issue("research_version_inventory.json", "", "version", "error", f"blank expected versions: {blank_count}"))
    duplicates = sorted({version for version in versions if version and versions.count(version) > 1})
    for version in duplicates:
        issues.append(_issue("research_version_inventory.json", version, "version", "error", "duplicate expected inventory version"))
    if versions != expected_versions:
        missing_versions = [version for version in expected_versions if version not in versions]
        unexpected_versions = [version for version in versions if version not in expected_versions]
        first_difference = next(
            (
                index
                for index, (expected, actual) in enumerate(zip(expected_versions, versions))
                if expected != actual
            ),
            min(len(expected_versions), len(versions)),
        )
        issues.append(
            _issue(
                "research_version_inventory.json",
                "",
                "versions.expected_order",
                "error",
                "ordered expected set mismatch: "
                f"first_difference={first_difference}; "
                f"expected={','.join(expected_versions)}; "
                f"actual={','.join(versions)}; "
                f"missing={','.join(missing_versions) or 'none'}; "
                f"unexpected={','.join(unexpected_versions) or 'none'}",
            )
        )
    expected_count = inventory.get("summary", {}).get("expected_record_count") if isinstance(inventory.get("summary"), dict) else None
    if expected_count is not None and int(expected_count) != len(records):
        issues.append(_issue("research_version_inventory.json", "", "versions", "error", f"expected_record_count={expected_count}, actual={len(records)}"))
    if len(records) != len(expected_versions):
        issues.append(_issue("research_version_inventory.json", "", "versions", "error", f"governance expected set must contain {len(expected_versions)} rows, found {len(records)}"))

    documents, document_issues = read_task_documents(task_paths)
    issues.extend(document_issues)
    aliases = build_version_aliases(records)
    matches: dict[str, list[tuple[Path, dict[str, Any]]]] = {version: [] for version in versions if version}
    for path, brief in documents:
        matched = identify_expected_version(brief, aliases)
        if matched in matches:
            matches[matched].append((path, brief))

    changelog_text = read_text(changelog_path)
    changelog_section = inventory_builder.parse_changelog_inventory_section(changelog_text)
    active = read_json_object(active_cohort_path) if active_cohort_path.is_file() else {}
    inventory_active = inventory.get("active_cohort", {}) if isinstance(inventory.get("active_cohort"), dict) else {}
    active_id = str(active.get("cohort_id") or active.get("active_cohort_id") or "")
    active_hash = str(active.get("manifest_hash") or active.get("active_cohort_manifest_hash") or "")
    if not active_id or not active_hash or active.get("freeze_passed") is not True:
        issues.append(_issue(active_cohort_path.name, "", "cohort", "error", "active cohort pointer is missing or not freeze_passed"))
    if (
        str(inventory_active.get("cohort_id", "")) != active_id
        or str(inventory_active.get("manifest_hash", "")) != active_hash
        or inventory_active.get("freeze_passed") is not True
    ):
        issues.append(_issue("research_version_inventory.json", "", "active_cohort", "error", "inventory active cohort does not match the live verified pointer"))

    rows: list[dict[str, Any]] = []
    inventory_as_of = str(inventory.get("inventory_as_of", ""))
    for record in records:
        version = str(record.get("version", ""))
        if not version:
            continue
        before = len(issues)
        brief_matches = matches.get(version, [])
        brief_status = "present" if len(brief_matches) == 1 else ("missing" if not brief_matches else "duplicate")
        if not brief_matches:
            issues.append(_issue("", version, "governance.task_brief", "error", f"missing task brief for {version}"))
            brief: dict[str, Any] = {}
            brief_path: Path | None = None
        elif len(brief_matches) > 1:
            paths = ",".join(relative_or_absolute(path, root) for path, _ in brief_matches)
            issues.append(_issue(paths, version, "governance.task_brief", "error", f"duplicate version briefs for {version}"))
            brief_path, brief = brief_matches[0]
        else:
            brief_path, brief = brief_matches[0]

        if brief:
            audit_governance_brief_metadata(
                version,
                brief,
                brief_path or Path(),
                record,
                inventory_as_of,
                issues,
            )

        inventory_brief = record.get("task_brief", {}) if isinstance(record.get("task_brief"), dict) else {}
        expected_inventory_brief_status = "present" if brief_status == "present" else ("ambiguous" if brief_status == "duplicate" else "missing")
        if str(inventory_brief.get("status", "")) != expected_inventory_brief_status:
            issues.append(_issue("research_version_inventory.json", version, "task_brief.status", "error", f"stale inventory brief status: declared={inventory_brief.get('status')}, actual={expected_inventory_brief_status}"))
        if brief_status == "present":
            declared_path = normalize_rel(inventory_brief.get("path", ""))
            actual_path = normalize_rel(relative_or_absolute(brief_matches[0][0], root))
            if not declared_path or declared_path != actual_path:
                issues.append(_issue("research_version_inventory.json", version, "task_brief.path", "error", f"inventory brief path mismatch: declared={declared_path}, actual={actual_path}"))
            declared_sha = str(inventory_brief.get("sha256", ""))
            actual_sha = inventory_builder.sha256_file(brief_matches[0][0])
            if not declared_sha or declared_sha != actual_sha:
                issues.append(
                    _issue(
                        "research_version_inventory.json",
                        version,
                        "task_brief.sha256",
                        "error",
                        f"inventory brief SHA mismatch: declared={declared_sha or 'missing'}, recomputed={actual_sha}",
                    )
                )

        producer_status = audit_producer(record, version, root, issues)
        recoverability_status = audit_inventory_recoverability(record, version, issues)
        registration_status = audit_registration(record, version, root, issues)
        changelog_status = audit_changelog(
            record,
            version,
            changelog_section,
            changelog_path,
            issues,
        )
        output_status = audit_standard_manifest(record, version, root, issues)
        cohort_status = audit_cohort(record, version, active_id, active_hash, issues)
        boundary = record.get("evidence_boundary", {}) if isinstance(record.get("evidence_boundary"), dict) else {}
        boundary_status = str(boundary.get("status", "missing"))
        if boundary_status != "consistent" or record.get("state_consistency_status") == "inconsistent":
            issues.append(_issue("research_version_inventory.json", version, "evidence_boundary", "error", f"evidence boundary is not consistent: {boundary_status}"))

        version_issues = issues[before:]
        error_count = sum(issue["severity"] == "error" for issue in version_issues)
        rows.append(
            {
                "version": version,
                "goal": str(record.get("goal", "")),
                "record_type": str(brief.get("record_type", "")) if brief else "",
                "task_brief_path": relative_or_absolute(brief_path, root) if brief_path else "",
                "task_brief_status": brief_status,
                "producer_status": producer_status,
                "recoverability_status": recoverability_status,
                "registration_kind": str((record.get("registration") or {}).get("kind", "")),
                "registration_status": registration_status,
                "post_hoc": str(bool(record.get("post_hoc"))).lower(),
                "changelog_status": changelog_status,
                "standard_output_status": output_status,
                "cohort_status": cohort_status,
                "evidence_boundary_status": boundary_status,
                "in_current_mainline": str(bool(record.get("in_current_mainline"))).lower(),
                "governance_status": "pass" if error_count == 0 else "fail",
                "issue_count": error_count,
                "artifact_role": "governance_detail_not_investment_candidate",
            }
        )

    return rows, issues


def audit_governance_brief_metadata(
    version: str,
    brief: Mapping[str, Any],
    path: Path,
    record: Mapping[str, Any],
    inventory_as_of: str,
    issues: list[dict[str, str]],
) -> None:
    task_id = str(brief.get("task_id", version))
    record_type = str(brief.get("record_type") or brief.get("governance_record_type") or "")
    historical = version != "CURRENT_MAINLINE"
    expected_type = "retrospective_inventory" if historical else "current_operating_brief"
    if record_type != expected_type:
        issues.append(_issue(path.name, task_id, "record_type", "error", f"{version} must declare record_type={expected_type}; found {record_type or 'missing'}"))
    if historical:
        if brief.get("inventory_recorded_retrospectively") is not True:
            issues.append(_issue(path.name, task_id, "inventory_recorded_retrospectively", "error", "historical governance brief must explicitly declare true"))
        if brief.get("historical_timestamp_claimed") is not False:
            issues.append(_issue(path.name, task_id, "historical_timestamp_claimed", "error", "retrospective brief must explicitly declare false"))
        if brief.get("historical_owner_claimed") not in {False, None}:
            issues.append(_issue(path.name, task_id, "historical_owner_claimed", "error", "retrospective inventory cannot claim a historical task owner"))
        recorded_at = str(brief.get("recorded_at", ""))
        if not recorded_at:
            issues.append(_issue(path.name, task_id, "recorded_at", "error", "retrospective brief must declare the actual inventory recording date"))
        elif inventory_as_of and date_part(recorded_at) != date_part(inventory_as_of):
            issues.append(_issue(path.name, task_id, "recorded_at", "error", f"retrospective brief recorded_at must equal inventory_as_of={inventory_as_of}, found {recorded_at}"))
        for timestamp_field in ("created_at", "task_created_at", "preregistered_at", "historical_recorded_at"):
            if str(brief.get(timestamp_field, "")).strip():
                issues.append(_issue(path.name, task_id, timestamp_field, "error", "retrospective inventory cannot present a historical execution timestamp"))
    elif brief.get("inventory_recorded_retrospectively") is not False:
        issues.append(_issue(path.name, task_id, "inventory_recorded_retrospectively", "error", "current operating brief must explicitly declare false"))

    expected_registration = record.get("registration", {}) if isinstance(record.get("registration"), dict) else {}
    brief_registration = brief.get("registration", {}) if isinstance(brief.get("registration"), dict) else {}
    expected_kind = str(expected_registration.get("kind", ""))
    brief_kind = str(brief.get("registration_kind") or brief_registration.get("kind") or "")
    if brief_kind != expected_kind:
        issues.append(_issue(path.name, task_id, "registration_kind", "error", f"registration kind mismatch: expected={expected_kind}, found={brief_kind or 'missing'}"))
    allowed_post_hoc_statuses = {
        "post_hoc_historical_inventory",
        "retrospective_governance",
        "not_an_experiment",
        "preregistered_forward_only",
        "inherits_registered_rule",
        "preregistered_forward_only_inherited",
    }
    expected_post_hoc_status = str(record.get("post_hoc_status", ""))
    if expected_post_hoc_status not in allowed_post_hoc_statuses:
        issues.append(
            _issue(
                "research_version_inventory.json",
                version,
                "post_hoc_status",
                "error",
                f"inventory post_hoc_status is missing or unknown: {expected_post_hoc_status or 'missing'}",
            )
        )
    brief_post_hoc_status = str(brief.get("post_hoc_status", ""))
    if brief_post_hoc_status != expected_post_hoc_status:
        issues.append(
            _issue(
                path.name,
                task_id,
                "post_hoc_status",
                "error",
                "explicit post_hoc_status mismatch: "
                f"expected={expected_post_hoc_status}, found={brief_post_hoc_status or 'missing'}",
            )
        )
    if brief.get("post_hoc") is not bool(record.get("post_hoc")):
        issues.append(_issue(path.name, task_id, "post_hoc", "error", "top-level post_hoc must match inventory"))
    if expected_post_hoc_status in {"retrospective_governance", "not_an_experiment"}:
        expected_version_class = str(record.get("version_class", ""))
        expected_notes = record.get("notes", [])
        if not expected_version_class or not isinstance(expected_notes, list) or not expected_notes:
            issues.append(
                _issue(
                    "research_version_inventory.json",
                    version,
                    "version_class_notes",
                    "error",
                    f"{expected_post_hoc_status} requires an auditable version_class and notes rationale",
                )
            )
        if str(brief.get("version_class", "")) != expected_version_class:
            issues.append(_issue(path.name, task_id, "version_class", "error", "brief version_class must match inventory"))
        if brief.get("notes") != expected_notes:
            issues.append(_issue(path.name, task_id, "notes", "error", "brief notes rationale must match inventory"))
    if brief_registration.get("post_hoc") is not bool(record.get("post_hoc")):
        issues.append(_issue(path.name, task_id, "registration.post_hoc", "error", "nested registration.post_hoc must match inventory"))
    if str(brief_registration.get("status", "")) != str(expected_registration.get("status", "")):
        issues.append(_issue(path.name, task_id, "registration.status", "error", "brief registration status must match inventory"))
    for field in ("paths", "experiment_ids"):
        actual = [normalize_rel(value) for value in brief_registration.get(field, [])]
        expected = [normalize_rel(value) for value in expected_registration.get(field, [])]
        if actual != expected:
            issues.append(_issue(path.name, task_id, f"registration.{field}", "error", f"brief registration {field} must match inventory"))

    objective = str(brief.get("objective", "")).strip()
    if not objective or objective != str(record.get("goal", "")).strip():
        issues.append(_issue(path.name, task_id, "objective", "error", "brief objective must exactly match the inventory goal"))
    if normalize_rel(brief.get("inventory_source", "")) != "logs/research_version_inventory.json":
        issues.append(_issue(path.name, task_id, "inventory_source", "error", "objective provenance must point to logs/research_version_inventory.json"))
    expected_objective_source = f"logs/research_version_inventory.json#{version}"
    if str(brief.get("objective_source", "")) != expected_objective_source:
        issues.append(
            _issue(
                path.name,
                task_id,
                "objective_source",
                "error",
                f"objective_source must equal {expected_objective_source}",
            )
        )

    source_paths = brief.get("source_paths", [])
    config_paths = brief.get("config_paths", [])
    output_manifest = brief.get("output_manifest", {}) if isinstance(brief.get("output_manifest"), dict) else {}
    output_paths = output_manifest.get("required_paths", [])
    if not isinstance(source_paths, list) or not source_paths:
        issues.append(_issue(path.name, task_id, "source_paths", "error", "governance evidence must include at least one producer source"))
    if not isinstance(config_paths, list):
        issues.append(_issue(path.name, task_id, "config_paths", "error", "config_paths must be a list, including an explicit empty list"))
    if not isinstance(output_paths, list) or len(output_paths) < len(STANDARD_OUTPUT_ARTIFACTS):
        issues.append(_issue(path.name, task_id, "output_manifest.required_paths", "error", "governance evidence must include the standard output paths"))
    evidence_paths = brief.get("evidence_paths", [])
    source_values = source_paths if isinstance(source_paths, list) else []
    config_values = config_paths if isinstance(config_paths, list) else []
    output_values = output_paths if isinstance(output_paths, list) else []
    canonical_evidence = list(
        dict.fromkeys(
            [normalize_rel(value) for value in source_values]
            + [normalize_rel(value) for value in config_values]
            + [normalize_rel(value) for value in brief_registration.get("paths", [])]
            + [normalize_rel(value) for value in output_values]
        )
    )
    normalized_evidence = (
        [normalize_rel(value) for value in evidence_paths]
        if isinstance(evidence_paths, list)
        else []
    )
    if not normalized_evidence:
        issues.append(_issue(path.name, task_id, "evidence_paths", "error", "explicit evidence_paths must be a non-empty list"))
    elif len(normalized_evidence) != len(set(normalized_evidence)):
        issues.append(_issue(path.name, task_id, "evidence_paths", "error", "evidence_paths must be stably de-duplicated"))
    else:
        missing_evidence = [value for value in canonical_evidence if value not in normalized_evidence]
        observed_required_order = [value for value in normalized_evidence if value in canonical_evidence]
        if missing_evidence:
            issues.append(_issue(path.name, task_id, "evidence_paths", "error", f"evidence_paths missing canonical evidence: {','.join(missing_evidence)}"))
        elif observed_required_order != canonical_evidence:
            issues.append(_issue(path.name, task_id, "evidence_paths", "error", "canonical evidence appears in a non-deterministic order"))
    expected_source = normalize_rel(nested_value(record, "producer", "source"))
    if expected_source and expected_source not in {normalize_rel(value) for value in source_paths if isinstance(source_paths, list)}:
        issues.append(_issue(path.name, task_id, "source_paths", "error", "producer source evidence does not match inventory"))
    expected_configs = [normalize_rel(value) for value in (record.get("config", {}) or {}).get("paths", [])]
    if isinstance(config_paths, list) and [normalize_rel(value) for value in config_paths] != expected_configs:
        issues.append(_issue(path.name, task_id, "config_paths", "error", "config evidence does not match inventory"))
    expected_directory = normalize_rel(nested_value(record, "output", "directory"))
    if normalize_rel(output_manifest.get("directory", "")) != expected_directory:
        issues.append(_issue(path.name, task_id, "output_manifest.directory", "error", "output manifest directory does not match inventory"))
    if str(output_manifest.get("standard_output_status", "")) != "complete":
        issues.append(_issue(path.name, task_id, "output_manifest.standard_output_status", "error", "brief must declare a complete standard output manifest"))
    expected_output_paths = {normalize_rel(f"{expected_directory}/{name}") for name in STANDARD_OUTPUT_ARTIFACTS}
    actual_output_paths = {normalize_rel(value) for value in output_paths} if isinstance(output_paths, list) else set()
    if actual_output_paths != expected_output_paths:
        issues.append(_issue(path.name, task_id, "output_manifest.required_paths", "error", "brief standard output paths do not exactly match the inventory output directory"))
    if output_manifest.get("missing_paths") not in ([], None):
        issues.append(_issue(path.name, task_id, "output_manifest.missing_paths", "error", "brief output manifest declares missing paths"))
    expected_run_summary = normalize_rel(nested_value(record, "output", "run_summary_path"))
    if expected_run_summary and normalize_rel(output_manifest.get("run_summary_path", "")) != expected_run_summary:
        issues.append(_issue(path.name, task_id, "output_manifest.run_summary_path", "error", "brief run_summary path does not match inventory"))
    expected_run_version = str(record.get("run_summary_version", ""))
    if expected_run_version and str(output_manifest.get("run_summary_version", "")) != expected_run_version:
        issues.append(_issue(path.name, task_id, "output_manifest.run_summary_version", "error", "brief run_summary version does not match inventory"))
    inventory_manifest = nested_value(record, "output", "standard_manifest")
    if isinstance(inventory_manifest, Mapping):
        for hash_field in ("structure_manifest_sha256", "run_summary_sha256"):
            expected_hash = str(inventory_manifest.get(hash_field, ""))
            if expected_hash and str(output_manifest.get(hash_field, "")) != expected_hash:
                issues.append(_issue(path.name, task_id, f"output_manifest.{hash_field}", "error", f"brief {hash_field} does not match inventory"))


def audit_producer(record: Mapping[str, Any], version: str, root: Path, issues: list[dict[str, str]]) -> str:
    producer = record.get("producer", {}) if isinstance(record.get("producer"), dict) else {}
    source = str(producer.get("source", ""))
    passed = producer.get("status") == "matched" and bool(source) and (root / source).is_file()
    if not passed:
        issues.append(_issue("research_version_inventory.json", version, "producer", "error", f"producer is not uniquely matched and present: status={producer.get('status')}, source={source}"))
    config = record.get("config", {}) if isinstance(record.get("config"), dict) else {}
    missing_configs = list(config.get("missing_paths", [])) if isinstance(config.get("missing_paths", []), list) else ["invalid_missing_paths"]
    if missing_configs:
        issues.append(_issue("research_version_inventory.json", version, "config", "error", f"missing config paths: {','.join(str(value) for value in missing_configs)}"))
    return "pass" if passed and not missing_configs else "fail"


def audit_inventory_recoverability(
    record: Mapping[str, Any],
    version: str,
    issues: list[dict[str, str]],
) -> str:
    missing = record.get("missing_requirements", [])
    missing_values = [str(value) for value in missing] if isinstance(missing, list) else ["invalid_missing_requirements"]
    passed = (
        record.get("governance_status") == "pass"
        and not missing_values
        and record.get("source_git_recoverability") == "recoverable"
        and record.get("task_brief_git_recoverable") is True
    )
    if not passed:
        issues.append(
            _issue(
                "research_version_inventory.json",
                version,
                "git_recoverability",
                "error",
                "inventory governance is not Git-recoverable: "
                f"governance_status={record.get('governance_status')}; "
                f"source={record.get('source_git_recoverability')}; "
                f"task_brief={record.get('task_brief_git_recoverable')}; "
                f"missing={','.join(missing_values) or 'none'}",
            )
        )
    return "pass" if passed else "fail"


def audit_registration(record: Mapping[str, Any], version: str, root: Path, issues: list[dict[str, str]]) -> str:
    registration = record.get("registration", {}) if isinstance(record.get("registration"), dict) else {}
    kind = str(registration.get("kind", ""))
    if kind not in REGISTRATION_KINDS:
        issues.append(_issue("research_version_inventory.json", version, "registration.kind", "error", f"unknown registration kind: {kind or 'missing'}"))
        return "fail"
    if kind == "explicit_post_hoc":
        passed = registration.get("status") == "explicit_post_hoc" and record.get("post_hoc") is True
        if not passed:
            issues.append(_issue("research_version_inventory.json", version, "registration", "error", "explicit post-hoc declaration is incomplete"))
        return "pass" if passed else "fail"

    if registration.get("status") != "valid" or record.get("post_hoc") is not False:
        issues.append(_issue("research_version_inventory.json", version, "registration", "error", f"prospective registration is invalid: status={registration.get('status')}, post_hoc={record.get('post_hoc')}"))
        return "fail"
    experiment_ids = [str(value) for value in registration.get("experiment_ids", [])]
    registration_paths = [str(value) for value in registration.get("paths", [])]
    if not experiment_ids or not registration_paths:
        issues.append(_issue("research_version_inventory.json", version, "registration", "error", "prospective registration must reference paths and experiment_ids"))
        return "fail"

    observed: dict[str, Mapping[str, Any]] = {}
    path_failure = False
    for relative in registration_paths:
        path = root / relative
        if not path.is_file():
            path_failure = True
            issues.append(_issue(relative, version, "registration.path", "error", "registration evidence path is missing"))
            continue
        try:
            for row in read_jsonl(path):
                experiment_id = str(row.get("experiment_id", ""))
                if experiment_id:
                    observed[experiment_id] = row
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            path_failure = True
            issues.append(_issue(relative, version, "registration.path", "error", f"registration evidence is unreadable: {exc}"))
    timing_failure = False
    for experiment_id in experiment_ids:
        row = observed.get(experiment_id)
        if row is None:
            timing_failure = True
            issues.append(_issue("research_version_inventory.json", version, "registration.experiment_id", "error", f"missing registered experiment: {experiment_id}"))
            continue
        registered_at = parse_datetime(row.get("registered_at"))
        evidence_start = parse_datetime(row.get("evidence_start_date"))
        if registered_at is None or evidence_start is None:
            timing_failure = True
            issues.append(_issue("research_version_inventory.json", version, "registration.timestamp", "error", f"registration {experiment_id} lacks parseable registered_at/evidence_start_date"))
        elif registered_after_evidence_start(registered_at, evidence_start, row.get("evidence_start_date")):
            timing_failure = True
            issues.append(_issue("research_version_inventory.json", version, "registration.timestamp", "error", f"registration later than evidence start: {experiment_id}; registered_at={row.get('registered_at')}; evidence_start={row.get('evidence_start_date')}"))
    return "fail" if path_failure or timing_failure else "pass"


def audit_changelog(
    record: Mapping[str, Any],
    version: str,
    section: Mapping[str, Any],
    path: Path,
    issues: list[dict[str, str]],
) -> str:
    changelog = record.get("changelog", {}) if isinstance(record.get("changelog"), dict) else {}
    declared_anchor = (
        record.get("changelog_anchor", {})
        if isinstance(record.get("changelog_anchor"), dict)
        else {}
    )
    recomputed_anchor = inventory_builder.changelog_anchor_for_version(dict(section), version)
    failed = False
    if not path.is_file():
        failed = True
        issues.append(_issue(path.name, version, "changelog", "error", "version changelog file is missing"))
    if section.get("status") != "valid":
        failed = True
        issues.append(
            _issue(
                path.name,
                version,
                "changelog_anchor.section",
                "error",
                f"exact retrospective inventory section is invalid: status={section.get('status')}",
            )
        )
    for field in sorted(set(declared_anchor) | set(recomputed_anchor)):
        declared = declared_anchor.get(field)
        actual = recomputed_anchor.get(field)
        if declared != actual:
            failed = True
            issues.append(
                _issue(
                    "research_version_inventory.json",
                    version,
                    f"changelog_anchor.{field}",
                    "error",
                    f"exact changelog anchor mismatch: declared={declared}, recomputed={actual}",
                )
            )
    actual_present = recomputed_anchor.get("status") == "present"
    declared_present = changelog.get("status") == "present"
    if not actual_present:
        failed = True
        issues.append(
            _issue(
                path.name,
                version,
                "changelog_anchor.version_token",
                "error",
                f"exact retrospective inventory section is missing version token {version}",
            )
        )
    if declared_present != actual_present:
        failed = True
        issues.append(
            _issue(
                "research_version_inventory.json",
                version,
                "changelog.status",
                "error",
                "stale changelog status: "
                f"declared={changelog.get('status')}, actual={'present' if actual_present else 'missing'}",
            )
        )
    return "fail" if failed else "pass"


def audit_standard_manifest(record: Mapping[str, Any], version: str, root: Path, issues: list[dict[str, str]]) -> str:
    output = record.get("output", {}) if isinstance(record.get("output"), dict) else {}
    directory = str(output.get("directory", ""))
    manifest = output.get("standard_manifest", {}) if isinstance(output.get("standard_manifest"), dict) else {}
    required = list(STANDARD_OUTPUT_ARTIFACTS)
    # Reuse the inventory builder's canonical implementation.  This prevents
    # the coverage audit and inventory producer from drifting on file ordering,
    # size accounting, or SHA construction.
    recomputed = inventory_builder.standard_output_manifest(root, directory, required)
    failed = False
    comparable_fields = sorted(set(manifest) | set(recomputed))
    for field in comparable_fields:
        declared = manifest.get(field)
        actual = recomputed.get(field)
        if declared != actual:
            failed = True
            issues.append(
                _issue(
                    "research_version_inventory.json",
                    version,
                    f"standard_manifest.{field}",
                    "error",
                    f"live output manifest mismatch: declared={declared}, recomputed={actual}",
                )
            )
    if recomputed["status"] != "complete":
        failed = True
        issues.append(
            _issue(
                "research_version_inventory.json",
                version,
                "standard_manifest",
                "error",
                "standard output manifest incomplete: "
                f"missing={','.join(recomputed['missing_artifacts']) or 'none'}",
            )
        )
    return "fail" if failed else "pass"


def audit_cohort(
    record: Mapping[str, Any],
    version: str,
    active_id: str,
    active_hash: str,
    issues: list[dict[str, str]],
) -> str:
    cohort = record.get("cohort", {}) if isinstance(record.get("cohort"), dict) else {}
    if cohort.get("applicable") is True:
        passed = (
            cohort.get("status") == "matches_active_pair"
            and str(cohort.get("declared_cohort_id", "")) == active_id
            and str(cohort.get("declared_manifest_hash", "")) == active_hash
            and str(cohort.get("active_cohort_id", "")) == active_id
            and str(cohort.get("active_manifest_hash", "")) == active_hash
        )
        if not passed:
            issues.append(_issue("research_version_inventory.json", version, "cohort", "error", f"cohort mismatch: status={cohort.get('status')}, declared=({cohort.get('declared_cohort_id')},{cohort.get('declared_manifest_hash')}), active=({active_id},{active_hash})"))
        return "pass" if passed else "fail"
    allowed = {"not_applicable", "referenced_indirectly_by_forward_evidence_only"}
    passed = cohort.get("status") in allowed and str(cohort.get("active_cohort_id", "")) == active_id and str(cohort.get("active_manifest_hash", "")) == active_hash
    if not passed:
        issues.append(_issue("research_version_inventory.json", version, "cohort", "error", f"non-applicable cohort metadata is stale or invalid: status={cohort.get('status')}"))
    return "pass" if passed else "fail"


def build_version_aliases(records: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for record in records:
        version = str(record.get("version", ""))
        if not version:
            continue
        # CURRENT_MAINLINE's run_summary_version (currently 1.0.0) is not a
        # governance identifier: using it would misclassify the archived V1.0
        # research brief as a second current-mainline brief.
        values = {version}
        if version != "CURRENT_MAINLINE":
            values.add(str(record.get("run_summary_version", "")))
        if version.startswith("V"):
            values.add(version[1:])
            values.add(version.lower().replace(".", "_"))
        if version == "CURRENT_MAINLINE":
            values.update({"current_mainline", "CURRENT_MAINLINE"})
        for value in values:
            normalized = normalize_version_token(value)
            if normalized:
                aliases[normalized] = version
    return aliases


def identify_expected_version(brief: Mapping[str, Any], aliases: Mapping[str, str]) -> str:
    values = [
        brief.get("governance_version"),
        brief.get("research_version"),
        brief.get("version"),
        brief.get("task_id"),
    ]
    for value in values:
        normalized = normalize_version_token(value)
        if normalized in aliases:
            return aliases[normalized]
        text = str(value or "").lower()
        for alias, expected in aliases.items():
            if alias.startswith("v") and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![0-9])", text):
                return expected
    return ""


def normalize_version_token(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"current_mainline", "current mainline"}:
        return "current_mainline"
    semver = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.0)?", text)
    if semver:
        return f"v{int(semver.group(1))}.{int(semver.group(2)):02d}"
    task_version = re.search(r"(?<![a-z0-9])v(\d+)_(\d+)(?!\d)", text)
    if task_version:
        return f"v{int(task_version.group(1))}.{int(task_version.group(2)):02d}"
    return text


def registered_after_evidence_start(registered_at: datetime, evidence_start: datetime, raw_start: Any) -> bool:
    raw = str(raw_start or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return registered_at.date() > evidence_start.date()
    return registered_at > evidence_start


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def date_part(value: str) -> date | None:
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def read_task_documents(task_paths: Sequence[Path]) -> tuple[list[tuple[Path, dict[str, Any]]], list[dict[str, str]]]:
    documents: list[tuple[Path, dict[str, Any]]] = []
    issues: list[dict[str, str]] = []
    for path in task_paths:
        try:
            documents.append((path, read_json_object(path)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(_issue(path.name, "", "json", "error", f"invalid JSON object: {exc}"))
    return documents, issues


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"line {line_number} is not an object")
        rows.append(value)
    return rows


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def nested_value(value: Mapping[str, Any], *parts: str) -> Any:
    current: Any = value
    for part in parts:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def is_archived_brief(path: Path) -> bool:
    return "archive" in {part.lower() for part in path.parts}


def normalize_rel(value: Any) -> str:
    return str(value or "").replace("\\", "/").lstrip("./")


def relative_or_absolute(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return normalize_rel(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _issue(task_file: str, task_id: str, field: str, severity: str, message: str) -> dict[str, str]:
    return {"task_file": task_file, "task_id": task_id, "field": field, "severity": severity, "message": message}


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def self_check() -> None:
    with tempfile.TemporaryDirectory(prefix="task-brief-governance-") as raw:
        root = Path(raw)
        payload, paths = _build_self_check_fixture(root)
        changelog = root / "logs" / "version_changelog.md"
        active = root / "logs" / "active.json"
        rows, issues = audit_inventory_governance(payload, paths, root=root, changelog_path=changelog, active_cohort_path=active)
        assert rows and all(row["governance_status"] == "pass" for row in rows), issues
        assert not [issue for issue in issues if issue["severity"] == "error"], issues

        replaced = json.loads(json.dumps(payload))
        replaced["versions"][0]["version"] = "V9.99"
        _, replaced_issues = audit_inventory_governance(
            replaced, paths, root=root, changelog_path=changelog, active_cohort_path=active
        )
        assert any(issue["field"] == "versions.expected_order" for issue in replaced_issues)

        reordered = json.loads(json.dumps(payload))
        reordered["versions"][0], reordered["versions"][1] = (
            reordered["versions"][1],
            reordered["versions"][0],
        )
        _, reordered_issues = audit_inventory_governance(
            reordered, paths, root=root, changelog_path=changelog, active_cohort_path=active
        )
        assert any(issue["field"] == "versions.expected_order" for issue in reordered_issues)

        historical_path = next(path for path in paths if path.name == "v4_72.json")
        without_historical = [path for path in paths if path != historical_path]
        missing_rows, missing_issues = audit_inventory_governance(payload, without_historical, root=root, changelog_path=changelog, active_cohort_path=active)
        assert any("missing task brief" in issue["message"] for issue in missing_issues)
        assert missing_rows[0]["governance_status"] == "fail"

        duplicate = paths + [historical_path.with_name("duplicate.json")]
        duplicate[-1].write_bytes(historical_path.read_bytes())
        _, duplicate_issues = audit_inventory_governance(payload, duplicate, root=root, changelog_path=changelog, active_cohort_path=active)
        assert any("duplicate version briefs" in issue["message"] for issue in duplicate_issues)

        bad_brief = read_json_object(historical_path)
        bad_brief["record_type"] = "preregistered"
        bad_brief["historical_timestamp_claimed"] = True
        write_json(historical_path, bad_brief)
        _, metadata_issues = audit_inventory_governance(payload, paths, root=root, changelog_path=changelog, active_cohort_path=active)
        assert any(issue["field"] == "record_type" for issue in metadata_issues)
        assert any(issue["field"] == "historical_timestamp_claimed" for issue in metadata_issues)
        write_json(historical_path, _self_check_brief("V4.72", "4.72.0", historical=True, root=root))

        explicit = read_json_object(historical_path)
        del explicit["objective_source"]
        del explicit["evidence_paths"]
        del explicit["post_hoc_status"]
        write_json(historical_path, explicit)
        _, explicit_issues = audit_inventory_governance(
            payload, paths, root=root, changelog_path=changelog, active_cohort_path=active
        )
        explicit_fields = {issue["field"] for issue in explicit_issues}
        assert {"objective_source", "evidence_paths", "post_hoc_status"} <= explicit_fields
        write_json(historical_path, _self_check_brief("V4.72", "4.72.0", historical=True, root=root))

        first_output = root / str(payload["versions"][0]["output"]["directory"])
        summary_path = first_output / "run_summary.json"
        original_summary = summary_path.read_bytes()
        summary_path.write_text("[]\n", encoding="utf-8")
        _, sha_issues = audit_inventory_governance(
            payload, paths, root=root, changelog_path=changelog, active_cohort_path=active
        )
        assert any(issue["field"] == "standard_manifest.run_summary_sha256" for issue in sha_issues)
        summary_path.write_bytes(original_summary)

        unexpected_file = first_output / "debug" / "unexpected.txt"
        unexpected_file.write_text("new\n", encoding="utf-8")
        _, structure_issues = audit_inventory_governance(
            payload, paths, root=root, changelog_path=changelog, active_cohort_path=active
        )
        assert any(issue["field"] == "standard_manifest.structure_manifest_sha256" for issue in structure_issues)
        unexpected_file.unlink()

        ledger_path = root / "logs" / "ledger.jsonl"
        late = json.loads(ledger_path.read_text(encoding="utf-8"))
        late["registered_at"] = "2026-07-19T00:00:00"
        ledger_path.write_text(json.dumps(late) + "\n", encoding="utf-8")
        _, timing_issues = audit_inventory_governance(payload, paths, root=root, changelog_path=changelog, active_cohort_path=active)
        assert any("registration later than evidence start" in issue["message"] for issue in timing_issues)

    print("self_check=pass")


def _build_self_check_fixture(root: Path) -> tuple[dict[str, Any], list[Path]]:
    active = {"cohort_id": "c1", "manifest_hash": "a" * 64, "freeze_passed": True}
    (root / "logs").mkdir(parents=True)
    write_json(root / "logs" / "active.json", active)
    ledger = {
        "experiment_id": "exp1",
        "registered_at": "2026-07-18T09:00:00",
        "evidence_start_date": "2026-07-18",
    }
    (root / "logs" / "ledger.jsonl").write_text(json.dumps(ledger) + "\n", encoding="utf-8")
    brief_dir = root / "strategy_lab" / "agents" / "task_briefs" / "governance"
    brief_dir.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    task_paths: list[Path] = []
    for version in expected_governance_versions():
        historical = version != "CURRENT_MAINLINE"
        if historical:
            major, minor = version[1:].split(".")
            run_version = f"{int(major)}.{int(minor)}.0"
            filename = version.lower().replace(".", "_") + ".json"
        else:
            run_version = "1.0.0"
            filename = "current.json"
        row = _self_check_record(
            root,
            version,
            run_version,
            active,
            explicit_post_hoc=historical,
        )
        task_path = brief_dir / filename
        write_json(
            task_path,
            _self_check_brief(
                version,
                run_version,
                historical=historical,
                root=root,
            ),
        )
        row["task_brief"] = {
            "status": "present",
            "path": relative_or_absolute(task_path, root),
            "sha256": inventory_builder.sha256_file(task_path),
        }
        records.append(row)
        task_paths.append(task_path)
    changelog_path = root / "logs" / "version_changelog.md"
    # The canonical parser requires the remediation section's exact heading
    # and metadata.
    version_lines = "\n".join(f"- {version}" for version in expected_governance_versions())
    changelog_path.write_text(
        f"## {inventory_builder.CHANGELOG_HEADING}\n\n"
        "record_type: `retrospective_inventory`\n\n"
        "recorded_at: `2026-07-18`\n\n"
        "historical_timestamp_claimed: `false`\n\n"
        f"{version_lines}\n",
        encoding="utf-8",
    )
    section = inventory_builder.parse_changelog_inventory_section(
        changelog_path.read_text(encoding="utf-8")
    )
    for row in records:
        row["changelog_anchor"] = inventory_builder.changelog_anchor_for_version(
            section, str(row["version"])
        )
    payload: dict[str, Any] = {
        "inventory_as_of": "2026-07-18",
        "active_cohort": active,
        "summary": {"expected_record_count": len(records)},
        "versions": records,
    }
    return payload, sorted(task_paths)


def _self_check_brief(
    version: str,
    run_version: str,
    *,
    historical: bool,
    root: Path | None = None,
) -> dict[str, Any]:
    safe = version.lower().replace(".", "_")
    registration = (
        {"kind": "explicit_post_hoc", "status": "explicit_post_hoc", "paths": [], "experiment_ids": [], "post_hoc": True}
        if historical
        else {
            "kind": "preregistered_forward_only_inherited",
            "status": "valid",
            "paths": ["logs/ledger.jsonl"],
            "experiment_ids": ["exp1"],
            "post_hoc": False,
        }
    )
    output_directory = f"outputs/{safe}"
    output_paths = [f"{output_directory}/{name}" for name in STANDARD_OUTPUT_ARTIFACTS]
    source_paths = [f"scripts/{safe}.py"]
    evidence_paths = list(dict.fromkeys(source_paths + list(registration["paths"]) + output_paths))
    output_manifest: dict[str, Any] = {
        "directory": output_directory,
        "run_summary_path": f"{output_directory}/run_summary.json",
        "run_summary_version": run_version,
        "standard_output_status": "complete",
        "required_paths": output_paths,
        "missing_paths": [],
    }
    if root is not None:
        live = inventory_builder.standard_output_manifest(
            root, output_directory, list(STANDARD_OUTPUT_ARTIFACTS)
        )
        output_manifest["structure_manifest_sha256"] = live["structure_manifest_sha256"]
        output_manifest["run_summary_sha256"] = live["run_summary_sha256"]
    return {
        "task_id": version.lower().replace(".", "_") + "_governance",
        "version": version if historical else "CURRENT_MAINLINE",
        "record_type": "retrospective_inventory" if historical else "current_operating_brief",
        "recorded_at": "2026-07-18",
        "inventory_recorded_retrospectively": historical,
        "historical_owner_claimed": False,
        "historical_timestamp_claimed": False,
        "objective": "self-check",
        "inventory_source": "logs/research_version_inventory.json",
        "objective_source": f"logs/research_version_inventory.json#{version}",
        "registration": registration,
        "post_hoc": historical,
        "post_hoc_status": "post_hoc_historical_inventory" if historical else "preregistered_forward_only_inherited",
        "source_paths": source_paths,
        "config_paths": [],
        "evidence_paths": evidence_paths,
        "output_manifest": output_manifest,
    }


def _self_check_record(
    root: Path,
    version: str,
    run_version: str,
    active: Mapping[str, Any],
    *,
    explicit_post_hoc: bool,
) -> dict[str, Any]:
    safe = version.lower().replace(".", "_")
    source = root / "scripts" / f"{safe}.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# fixture\n", encoding="utf-8")
    output_dir = root / "outputs" / safe
    (output_dir / "debug").mkdir(parents=True, exist_ok=True)
    (output_dir / "report.md").write_text("# report\n", encoding="utf-8")
    (output_dir / "run_summary.json").write_text("{}\n", encoding="utf-8")
    (output_dir / "top_candidates.csv").write_text("status\n", encoding="utf-8")
    (output_dir / "debug" / "evidence.txt").write_text("fixture\n", encoding="utf-8")
    output_relative = relative_or_absolute(output_dir, root)
    live_manifest = inventory_builder.standard_output_manifest(
        root, output_relative, list(STANDARD_OUTPUT_ARTIFACTS)
    )
    registration = (
        {"kind": "explicit_post_hoc", "status": "explicit_post_hoc", "paths": [], "experiment_ids": []}
        if explicit_post_hoc
        else {
            "kind": "preregistered_forward_only_inherited",
            "status": "valid",
            "paths": ["logs/ledger.jsonl"],
            "experiment_ids": ["exp1"],
        }
    )
    return {
        "version": version,
        "run_summary_version": run_version,
        "goal": "self-check",
        "producer": {"source": relative_or_absolute(source, root), "status": "matched"},
        "config": {"status": "not_declared", "paths": [], "missing_paths": []},
        "task_brief": {},
        "registration": registration,
        "post_hoc": explicit_post_hoc,
        "post_hoc_status": (
            "post_hoc_historical_inventory"
            if explicit_post_hoc
            else "preregistered_forward_only_inherited"
        ),
        "version_class": (
            "research_strategy_version"
            if explicit_post_hoc
            else "current_operating_mainline"
        ),
        "notes": ["self-check governance rationale"],
        "changelog": {"path": "logs/version_changelog.md", "status": "present"},
        "output": {
            "directory": output_relative,
            "run_summary_path": f"{output_relative}/run_summary.json",
            "standard_manifest": live_manifest,
        },
        "cohort": {
            "applicable": False,
            "status": "referenced_indirectly_by_forward_evidence_only" if version == "CURRENT_MAINLINE" else "not_applicable",
            "active_cohort_id": active["cohort_id"],
            "active_manifest_hash": active["manifest_hash"],
        },
        "evidence_boundary": {"status": "consistent"},
        "state_consistency_status": "consistent",
        "in_current_mainline": version == "CURRENT_MAINLINE",
        "governance_status": "pass",
        "missing_requirements": [],
        "source_git_recoverability": "recoverable",
        "task_brief_git_recoverable": True,
    }


if __name__ == "__main__":
    raise SystemExit(main())
