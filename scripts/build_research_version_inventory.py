#!/usr/bin/env python
"""Build the deterministic research-governance version inventory.

Historical producers are not inferred from filenames.  A producer is accepted
only when a statically declared OUT/OUTPUT/DEFAULT_OUTPUT path matches an output
directory whose run_summary.json declares the covered semantic version.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import re
import subprocess
from collections import Counter
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCOPE = ROOT / "configs" / "research_governance_scope.json"
DEFAULT_JSON_OUTPUT = ROOT / "logs" / "research_version_inventory.json"
DEFAULT_CSV_OUTPUT = ROOT / "logs" / "research_version_inventory.csv"
DEFAULT_AUDIT_OUTPUT = ROOT / "outputs" / "audit" / "research_version_inventory"
OUTPUT_BINDING_NAMES = {"OUT", "OUTPUT", "DEFAULT_OUTPUT"}
SUPPORTED_MAINLINE_ROLES = {
    "direct_runtime_source",
    "transitive_gate_evidence",
    "full_refresh_only",
    "archive_only",
    "current_orchestrator",
}
_GIT_PATH_STATUS_CACHE: dict[tuple[str, str], str] = {}
VERSION_RE = re.compile(r"^V?(\d+)\.(\d{1,2})(?:\.(\d+))?$", re.IGNORECASE)
CHANGELOG_HEADING = "2026-07-18 - Retrospective Research Inventory And Current Governance Baseline"

CSV_FIELDS = [
    "schema_version",
    "version_id",
    "sequence_ordinal",
    "implementation_version",
    "version_class",
    "objective",
    "source_paths",
    "source_path_sha256",
    "configuration_mode",
    "experiment_registration_status",
    "post_hoc_status",
    "inventory_recorded_at",
    "retrospective_inventory",
    "changelog_anchor",
    "changelog_anchor_id",
    "changelog_section_sha256",
    "output_generated_at",
    "artifact_git_state",
    "artifact_git_recoverable",
    "source_git_recoverability",
    "research_status",
    "mainline_role",
    "consistency_status",
    "notes",
    "version",
    "run_summary_version",
    "goal",
    "producer_source",
    "producer_match_status",
    "config_paths",
    "config_status",
    "task_brief_path",
    "task_brief_status",
    "registration_kind",
    "registration_status",
    "registration_paths",
    "experiment_ids",
    "post_hoc",
    "inventory_recorded_retrospectively",
    "changelog_status",
    "output_dir",
    "standard_output_status",
    "standard_output_missing",
    "output_structure_manifest_sha256",
    "source_git_status",
    "config_git_status",
    "task_brief_git_status",
    "output_git_status",
    "in_current_mainline",
    "mainline_relation",
    "cohort_status",
    "declared_cohort_id",
    "active_cohort_id",
    "evidence_boundary_status",
    "state_consistency_status",
    "lifecycle_status",
    "governance_status",
    "missing_requirements",
]


def normalize_rel(path: str | Path) -> str:
    value = str(path).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return PurePosixPath(value).as_posix()


def canonical_version(value: object) -> str | None:
    if str(value).upper() == "CURRENT_MAINLINE":
        return "CURRENT_MAINLINE"
    match = VERSION_RE.fullmatch(str(value).strip())
    if not match:
        return None
    major, minor = int(match.group(1)), int(match.group(2))
    return f"V{major}.{minor:02d}"


def version_key(value: str) -> tuple[int, int]:
    canonical = canonical_version(value)
    if not canonical or canonical == "CURRENT_MAINLINE":
        raise ValueError(f"not a numbered research version: {value}")
    major, minor = canonical[1:].split(".")
    return int(major), int(minor)


def version_in_range(version: str, start: str, end: str) -> bool:
    return version_key(start) <= version_key(version) <= version_key(end)


def expected_versions(scope: dict[str, Any]) -> list[str]:
    covered = scope["covered_version_range"]
    start = version_key(covered["start"])
    end = version_key(covered["end"])
    versions: list[str] = []
    for major in range(start[0], end[0] + 1):
        first = start[1] if major == start[0] else 0
        last = end[1] if major == end[0] else 99
        versions.extend(f"V{major}.{minor:02d}" for minor in range(first, last + 1))
    expected_count = int(covered["expected_count"])
    if len(versions) != expected_count:
        raise ValueError(f"scope expands to {len(versions)} versions, expected {expected_count}")
    return versions


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError(f"JSON object required: {path}:{line_number}")
            rows.append(value)
    return rows


def _static_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and node.id == "ROOT":
        return "."
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _static_path(node.left)
        right = _static_path(node.right)
        if left is not None and right is not None:
            return normalize_rel(PurePosixPath(left) / PurePosixPath(right))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path" and len(node.args) == 1:
        return _static_path(node.args[0])
    return None


def inspect_source(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    bindings: list[dict[str, str]] = []
    configs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            resolved = _static_path(value) if value is not None else None
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if resolved:
                normalized = normalize_rel(resolved)
                if normalized.startswith("configs/"):
                    configs.add(normalized)
                for name in names:
                    if name in OUTPUT_BINDING_NAMES and normalized.startswith("outputs/"):
                        bindings.append({"binding": name, "output_dir": normalized})
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            normalized = normalize_rel(node.value)
            if normalized.startswith("configs/") and Path(normalized).suffix.lower() in {".json", ".yaml", ".yml"}:
                configs.add(normalized)
    unique_bindings = {(row["binding"], row["output_dir"]): row for row in bindings}
    return [unique_bindings[key] for key in sorted(unique_bindings)], sorted(configs)


def discover_producers(root: Path) -> dict[str, list[dict[str, Any]]]:
    producers: dict[str, list[dict[str, Any]]] = {}
    for source in sorted((root / "scripts").glob("*.py")):
        bindings, configs = inspect_source(source)
        source_rel = normalize_rel(source.relative_to(root))
        for binding in bindings:
            row: dict[str, Any] = {
                "source": source_rel,
                "binding": binding["binding"],
                "config_paths": configs,
            }
            producers.setdefault(binding["output_dir"], []).append(row)
    return producers


def report_goal(output_dir: Path, summary: dict[str, Any]) -> str:
    report = output_dir / "report.md"
    if report.exists():
        for raw in report.read_text(encoding="utf-8-sig").splitlines():
            if raw.startswith("# "):
                return raw[2:].strip()
    return str(summary.get("final_verdict") or summary.get("policy_id") or "未声明研究目标")


def discover_version_outputs(root: Path, versions: Iterable[str]) -> dict[str, dict[str, Any]]:
    allowed = set(versions)
    found: dict[str, list[dict[str, Any]]] = {version: [] for version in allowed}
    for summary_path in sorted((root / "outputs").rglob("run_summary.json")):
        try:
            summary = read_json(summary_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        version = canonical_version(summary.get("version"))
        if version not in allowed:
            continue
        output_dir = summary_path.parent
        found[version].append(
            {
                "output_dir": normalize_rel(output_dir.relative_to(root)),
                "run_summary_path": normalize_rel(summary_path.relative_to(root)),
                "run_summary_version": str(summary.get("version", "")),
                "summary": summary,
                "goal": report_goal(output_dir, summary),
            }
        )
    missing = sorted((version for version, rows in found.items() if not rows), key=version_key)
    duplicate = {version: rows for version, rows in found.items() if len(rows) > 1}
    if missing or duplicate:
        messages = []
        if missing:
            messages.append(f"missing run_summary versions: {','.join(missing)}")
        if duplicate:
            messages.append(
                "duplicate run_summary versions: "
                + ";".join(f"{version}={','.join(row['output_dir'] for row in rows)}" for version, rows in duplicate.items())
            )
        raise ValueError("; ".join(messages))
    return {version: found[version][0] for version in allowed}


def discover_task_briefs(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    brief_root = root / "strategy_lab" / "agents" / "task_briefs"
    for path in sorted(brief_root.rglob("*.json")):
        try:
            brief = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        version = canonical_version(brief.get("version"))
        if version:
            found.setdefault(version, []).append(normalize_rel(path.relative_to(root)))
    return found


def scope_rule(version: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [rule for rule in rules if version_in_range(version, rule["start"], rule["end"])]
    if len(matches) != 1:
        raise ValueError(f"{version} must match exactly one scope rule; matched={len(matches)}")
    return matches[0]


def changelog_has_version(text: str, version: str) -> bool:
    if version == "CURRENT_MAINLINE":
        return bool(re.search(r"(?<![A-Za-z0-9_])CURRENT_MAINLINE(?![A-Za-z0-9_])", text))
    major, minor = version_key(version)
    minor_forms = {f"{minor:02d}", str(minor)}
    alternatives = "|".join(re.escape(value) for value in sorted(minor_forms, key=len, reverse=True))
    return bool(re.search(rf"(?<![A-Za-z0-9.])V{major}\.(?:{alternatives})(?![0-9.])", text, re.IGNORECASE))


def parse_changelog_inventory_section(text: str) -> dict[str, Any]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    heading_line = f"## {CHANGELOG_HEADING}"
    lines = normalized.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == heading_line]
    if len(starts) != 1:
        return {"status": "missing_or_ambiguous", "heading": CHANGELOG_HEADING, "text": "", "sha256": ""}
    start = starts[0]
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    section_text = "\n".join(line.rstrip() for line in lines[start:end]).strip() + "\n"
    required_metadata = {
        "record_type": "record_type: `retrospective_inventory`",
        "recorded_at": "recorded_at: `2026-07-18`",
        "historical_timestamp_claimed": "historical_timestamp_claimed: `false`",
    }
    missing_metadata = [key for key, marker in required_metadata.items() if marker not in section_text]
    return {
        "status": "valid" if not missing_metadata else "invalid_metadata",
        "heading": CHANGELOG_HEADING,
        "text": section_text,
        "sha256": hashlib.sha256(section_text.encode("utf-8")).hexdigest(),
        "missing_metadata": missing_metadata,
    }


def changelog_anchor_for_version(section: dict[str, Any], version: str) -> dict[str, Any]:
    token_present = section.get("status") == "valid" and changelog_has_version(str(section.get("text", "")), version)
    return {
        "path": "logs/version_changelog.md",
        "heading": CHANGELOG_HEADING,
        "anchor_id": "retrospective-research-inventory-and-current-governance-baseline",
        "record_type": "retrospective_inventory",
        "recorded_at": "2026-07-18",
        "historical_timestamp_claimed": False,
        "version_token": version,
        "section_sha256": str(section.get("sha256", "")),
        "status": "present" if token_present else "missing",
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def standard_output_manifest(root: Path, output_dir_rel: str, required: list[str]) -> dict[str, Any]:
    output_dir = root / output_dir_rel
    missing: list[str] = []
    wrong_type: list[str] = []
    for name in required:
        path = output_dir / name
        if name == "debug":
            if not path.is_dir():
                (wrong_type if path.exists() else missing).append(name)
        elif not path.is_file():
            (wrong_type if path.exists() else missing).append(name)
    observed_top_level = sorted(path.name for path in output_dir.iterdir()) if output_dir.is_dir() else []
    unexpected_top_level = sorted(set(observed_top_level) - set(required))
    files = sorted((path for path in output_dir.rglob("*") if path.is_file()), key=lambda path: normalize_rel(path.relative_to(output_dir))) if output_dir.exists() else []
    structure_rows = [f"{normalize_rel(path.relative_to(output_dir))}\t{path.stat().st_size}" for path in files]
    structure_hash = hashlib.sha256("\n".join(structure_rows).encode("utf-8")).hexdigest()
    summary_path = output_dir / "run_summary.json"
    return {
        "status": "complete" if not missing and not wrong_type and not unexpected_top_level else "invalid",
        "required_artifacts": required,
        "missing_artifacts": missing,
        "wrong_type_artifacts": wrong_type,
        "observed_top_level": observed_top_level,
        "unexpected_top_level": unexpected_top_level,
        "file_count": len(files),
        "debug_file_count": sum(1 for path in files if normalize_rel(path.relative_to(output_dir)).startswith("debug/")),
        "structure_manifest_sha256": structure_hash,
        "run_summary_sha256": sha256_file(summary_path) if summary_path.is_file() else "",
    }


def git_tracked_paths(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    )
    return {normalize_rel(value.decode("utf-8")) for value in result.stdout.split(b"\0") if value}


def git_path_status(root: Path, relative: str, tracked: set[str]) -> str:
    relative = normalize_rel(relative)
    if relative in tracked or any(item.startswith(relative.rstrip("/") + "/") for item in tracked):
        return "tracked"
    cache_key = (str(root.resolve()), relative)
    if cache_key in _GIT_PATH_STATUS_CACHE:
        return _GIT_PATH_STATUS_CACHE[cache_key]
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", relative], cwd=root, check=False
    ).returncode == 0
    status = "ignored" if ignored else "untracked"
    _GIT_PATH_STATUS_CACHE[cache_key] = status
    return status


def git_path_statuses(root: Path, relatives: Iterable[str], tracked: set[str]) -> dict[str, str]:
    normalized = sorted({normalize_rel(relative) for relative in relatives})
    result: dict[str, str] = {}
    unresolved: list[str] = []
    for relative in normalized:
        if relative in tracked or any(item.startswith(relative.rstrip("/") + "/") for item in tracked):
            result[relative] = "tracked"
        else:
            cache_key = (str(root.resolve()), relative)
            if cache_key in _GIT_PATH_STATUS_CACHE:
                result[relative] = _GIT_PATH_STATUS_CACHE[cache_key]
            else:
                unresolved.append(relative)
    if unresolved:
        payload = b"\0".join(relative.encode("utf-8") for relative in unresolved) + b"\0"
        checked = subprocess.run(
            ["git", "check-ignore", "-z", "--stdin"],
            cwd=root,
            check=False,
            input=payload,
            capture_output=True,
        )
        if checked.returncode not in {0, 1}:
            raise RuntimeError(checked.stderr.decode("utf-8", errors="replace"))
        ignored = {
            value.decode("utf-8") for value in checked.stdout.split(b"\0") if value
        }
        for relative in unresolved:
            status = "ignored" if relative in ignored else "untracked"
            result[relative] = status
            _GIT_PATH_STATUS_CACHE[(str(root.resolve()), relative)] = status
    return result


def registration_evidence(root: Path, rule: dict[str, Any]) -> dict[str, Any]:
    kind = str(rule["registration_kind"])
    paths = [normalize_rel(path) for path in rule.get("registration_paths", [])]
    expected_ids = [str(value) for value in rule.get("experiment_ids", [])]
    if kind == "explicit_post_hoc":
        return {
            "kind": kind,
            "status": "explicit_post_hoc",
            "paths": [],
            "path_sha256": {},
            "experiment_ids": [],
            "declaration_source": "configs/research_governance_scope.json",
            "boundary": "Retrospective inventory only; no preregistration is claimed.",
            "timing_status": "not_applicable",
            "timing_checks": [],
        }
    observed_ids: set[str] = set()
    observed_rows: dict[str, dict[str, Any]] = {}
    path_errors: list[str] = []
    for relative in paths:
        path = root / relative
        if not path.is_file():
            path_errors.append(relative)
            continue
        try:
            for row in read_jsonl(path):
                if row.get("experiment_id"):
                    experiment_id = str(row["experiment_id"])
                    observed_ids.add(experiment_id)
                    observed_rows[experiment_id] = row
        except (OSError, ValueError, json.JSONDecodeError):
            path_errors.append(relative)
    missing_ids = sorted(set(expected_ids) - observed_ids)
    timing_checks: list[dict[str, Any]] = []
    for experiment_id in expected_ids:
        row = observed_rows.get(experiment_id, {})
        registered_at = str(row.get("registered_at", ""))
        evidence_start_date = str(row.get("evidence_start_date", ""))
        timing_status = "unknown"
        try:
            registered_date = datetime.fromisoformat(registered_at.replace("Z", "+00:00")).date()
            start_date = date.fromisoformat(evidence_start_date)
            timing_status = "pass" if registered_date <= start_date else "fail"
        except ValueError:
            pass
        timing_checks.append(
            {
                "experiment_id": experiment_id,
                "registered_at": registered_at or None,
                "evidence_start_date": evidence_start_date or None,
                "comparison_granularity": "date",
                "status": timing_status,
            }
        )
    timing_status = "valid" if timing_checks and all(row["status"] == "pass" for row in timing_checks) else "invalid"
    return {
        "kind": kind,
        "status": "valid" if not path_errors and not missing_ids and timing_status == "valid" else "invalid",
        "paths": paths,
        "path_sha256": {
            relative: sha256_file(root / relative) for relative in paths if (root / relative).is_file()
        },
        "experiment_ids": expected_ids,
        "missing_paths": path_errors,
        "missing_experiment_ids": missing_ids,
        "declaration_source": "configs/research_governance_scope.json",
        "boundary": str(rule.get("boundary", "Inherited registration applies to forward evidence only.")),
        "timing_status": timing_status,
        "timing_checks": timing_checks,
    }


def post_hoc_status(version: str, registration: dict[str, Any]) -> str:
    kind = str(registration.get("kind", ""))
    if version == "CURRENT_MAINLINE":
        return "preregistered_forward_only_inherited" if kind == "preregistered_forward_only_inherited" else "unknown_requires_review"
    if kind == "preregistered_forward_only":
        return "preregistered_forward_only"
    if kind == "inherits_registered_rule":
        return "inherits_registered_rule"
    if kind != "explicit_post_hoc":
        return "unknown_requires_review"
    if version_in_range(version, "V5.21", "V5.29"):
        return "retrospective_governance"
    if version_in_range(version, "V5.30", "V5.35"):
        return "not_an_experiment"
    return "post_hoc_historical_inventory"


def version_class(version: str) -> str:
    if version == "CURRENT_MAINLINE":
        return "current_operating_mainline"
    if version_in_range(version, "V4.72", "V5.03"):
        return "research_strategy_version"
    if version_in_range(version, "V5.04", "V5.10"):
        return "forward_experiment_governance"
    if version_in_range(version, "V5.11", "V5.20"):
        return "research_audit_version"
    if version_in_range(version, "V5.21", "V5.35"):
        return "data_governance_version"
    return "unknown_requires_review"


def active_cohort(root: Path, scope: dict[str, Any]) -> dict[str, Any]:
    relative = normalize_rel(scope["cohort_scope"]["active_pointer"])
    path = root / relative
    value = read_json(path)
    return {
        "pointer_path": relative,
        "cohort_id": str(value.get("cohort_id") or value.get("active_cohort_id") or ""),
        "manifest_hash": str(value.get("manifest_hash") or value.get("active_cohort_manifest_hash") or ""),
        "freeze_passed": value.get("freeze_passed"),
    }


def assess_cohort(summary: dict[str, Any], active: dict[str, Any], applicable: bool) -> dict[str, Any]:
    if not applicable:
        return {
            "applicable": False,
            "status": "not_applicable",
            "declared_cohort_id": "",
            "declared_manifest_hash": "",
            "active_cohort_id": active["cohort_id"],
            "active_manifest_hash": active["manifest_hash"],
        }
    declared_id = str(summary.get("active_cohort_id") or summary.get("cohort_id") or "")
    declared_hash = str(
        summary.get("active_cohort_manifest_hash")
        or summary.get("cohort_manifest_hash")
        or (summary.get("manifest_hash") if declared_id else "")
        or ""
    )
    if not declared_id or not declared_hash:
        status = "not_declared_in_run_summary"
    elif declared_id == active["cohort_id"] and declared_hash == active["manifest_hash"]:
        status = "matches_active_pair"
    else:
        status = "stale_or_mismatched_active_pair"
    return {
        "applicable": True,
        "status": status,
        "declared_cohort_id": declared_id,
        "declared_manifest_hash": declared_hash,
        "active_cohort_id": active["cohort_id"],
        "active_manifest_hash": active["manifest_hash"],
    }


def assess_boundary(summary: dict[str, Any], expected: dict[str, Any], *, current: bool) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    fields = ["policy_status", "production_ready", "auto_execution_allowed"]
    if current:
        fields.extend(["manual_decision_support_ready", "action"])
    if "can_claim_strong_rebound_industries" in summary:
        fields.append("can_claim_strong_rebound_industries")
    expected_by_field = {
        "policy_status": expected["policy_status"],
        "production_ready": expected["production_ready"],
        "auto_execution_allowed": expected["auto_execution_allowed"],
        "manual_decision_support_ready": expected["manual_decision_support_ready"],
        "action": expected["current_action"],
        "can_claim_strong_rebound_industries": expected["strong_industry_alpha_validated"],
    }
    for field in fields:
        declared = field in summary
        actual = summary.get(field)
        wanted = expected_by_field[field]
        checks.append(
            {
                "field": field,
                "declared": declared,
                "actual": actual if declared else None,
                "expected": wanted,
                "status": "pass" if declared and actual == wanted else ("not_declared" if not declared else "fail"),
            }
        )
    violations = [row["field"] for row in checks if row["status"] == "fail"]
    return {
        "status": "consistent" if not violations else "inconsistent",
        "violations": violations,
        "checks": checks,
    }


def lifecycle_status(summary: dict[str, Any]) -> str:
    for key in ("best_status", "final_status", "task_status", "policy_status"):
        value = summary.get(key)
        if value not in (None, ""):
            return str(value)
    return "not_declared"


def _brief_info(root: Path, version: str, briefs: dict[str, list[str]]) -> dict[str, Any]:
    paths = briefs.get(version, [])
    path = paths[0] if len(paths) == 1 else ""
    return {
        "status": "present" if len(paths) == 1 else ("missing" if not paths else "ambiguous"),
        "path": path,
        "sha256": sha256_file(root / path) if path and (root / path).is_file() else "",
        "candidates": paths,
    }


def _config_info(root: Path, paths: list[str]) -> dict[str, Any]:
    missing = [path for path in paths if not (root / path).is_file()]
    return {
        "status": "not_declared" if not paths else ("present" if not missing else "missing"),
        "paths": paths,
        "missing_paths": missing,
    }


def _all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_strings(child)


def detailed_mainline_roles(
    root: Path,
    scope: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    producers: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    current_config = read_json(root / scope["current_mainline"]["config_paths"][0])
    direct_output_dirs: set[str] = set()
    for value in _all_strings(current_config.get("sources", {})):
        relative = normalize_rel(value)
        if relative.startswith("outputs/"):
            direct_output_dirs.add(normalize_rel(PurePosixPath(relative).parent))

    direct_versions = {
        version for version, output in outputs.items() if output["output_dir"] in direct_output_dirs
    }
    transitive_versions: set[str] = set()
    frontier = set(direct_versions)
    while frontier:
        referenced: set[str] = set()
        for version in frontier:
            output_dir = outputs[version]["output_dir"]
            matches = producers.get(output_dir, [])
            if len(matches) != 1:
                continue
            source_text = (root / matches[0]["source"]).read_text(encoding="utf-8-sig").replace("\\", "/")
            for candidate, candidate_output in outputs.items():
                if candidate in direct_versions or candidate in transitive_versions:
                    continue
                directory_name = PurePosixPath(candidate_output["output_dir"]).name
                if candidate_output["output_dir"] in source_text or directory_name in source_text:
                    referenced.add(candidate)
        transitive_versions.update(referenced)
        frontier = referenced

    refresh_path = root / "scripts" / "run_v4_71_live_refresh.py"
    refresh_text = refresh_path.read_text(encoding="utf-8-sig").replace("\\", "/")
    roles: dict[str, dict[str, Any]] = {}
    for version, output in outputs.items():
        matches = producers.get(output["output_dir"], [])
        source = matches[0]["source"] if len(matches) == 1 else ""
        if version in direct_versions:
            role = "direct_runtime_source"
            evidence = [scope["current_mainline"]["config_paths"][0], output["output_dir"]]
        elif version in transitive_versions:
            role = "transitive_gate_evidence"
            evidence = [source, "recursive reference from a direct or transitive runtime source"]
        elif source and PurePosixPath(source).name in refresh_text:
            role = "full_refresh_only"
            evidence = ["scripts/run_v4_71_live_refresh.py", source]
        else:
            role = "archive_only"
            evidence = [source] if source else []
        roles[version] = {"role": role, "evidence": evidence}
    roles["CURRENT_MAINLINE"] = {
        "role": "current_orchestrator",
        "evidence": [scope["current_mainline"]["producer_source"], scope["current_mainline"]["config_paths"][0]],
    }
    return roles


def enrich_record(
    root: Path,
    scope: dict[str, Any],
    record: dict[str, Any],
    sequence_ordinal: int,
    role: dict[str, Any],
) -> dict[str, Any]:
    source_paths = [record["producer"]["source"]] if record["producer"]["source"] else []
    source_hashes = {
        path: sha256_file(root / path) for path in source_paths if (root / path).is_file()
    }
    config_paths = list(record["config"]["paths"])
    config_hashes = {
        path: sha256_file(root / path) for path in config_paths if (root / path).is_file()
    }
    if record["config"]["status"] == "present":
        configuration_mode = "explicit_file_refs"
    elif record["config"]["status"] == "not_declared":
        configuration_mode = "embedded_or_inherited"
    else:
        configuration_mode = "unknown_requires_review"

    output_summary = read_json(root / record["output"]["run_summary_path"])
    output_generated_at = output_summary.get("generated_at")
    if output_generated_at in (None, ""):
        output_generated_at = None
    source_git_recoverability = (
        "recoverable" if record["git"]["producer_source"] == "tracked" else "not_recoverable"
    )
    artifact_git_state = record["git"]["output_directory"]
    artifact_git_recoverable = artifact_git_state == "tracked"
    task_brief_git_recoverable = record["git"]["task_brief"] == "tracked"
    notes = [
        "retrospective inventory recorded on 2026-07-18; no contemporaneous task brief is claimed"
        if record["version"] != "CURRENT_MAINLINE"
        else "current operating mainline record; not a strategy-version promotion",
    ]
    if configuration_mode == "embedded_or_inherited":
        notes.append("no standalone configuration file was found; configuration is embedded or inherited")
    if not artifact_git_recoverable:
        notes.append(f"local output exists but artifact_git_state={artifact_git_state}; Git recovery is not claimed")
    if output_generated_at is None:
        notes.append("output generated_at is unknown; no timestamp was inferred")

    missing = list(record["missing_requirements"])
    if record["task_brief"]["status"] == "present" and not task_brief_git_recoverable:
        missing.append("task_brief_git_recoverability")
    if source_paths and source_git_recoverability != "recoverable":
        missing.append("producer_source_git_recoverability")
    if config_paths and record["git"]["config_paths"] != "tracked":
        missing.append("config_git_recoverability")
    missing = list(dict.fromkeys(missing))
    registration = record["registration"]
    consistency = {
        "overall_status": record["state_consistency_status"],
        "producer_output_version_status": record["producer"]["status"],
        "evidence_boundary_status": record["evidence_boundary"]["status"],
        "cohort_status": record["cohort"]["status"],
        "registration_timing_status": registration.get("timing_status", "unknown"),
    }
    record.update(
        {
            "schema_version": str(scope["schema_version"]),
            "version_id": record["version"],
            "sequence_ordinal": sequence_ordinal,
            "implementation_version": record["run_summary_version"],
            "version_class": version_class(record["version"]),
            "objective": record["goal"],
            "source_paths": source_paths,
            "source_path_sha256": source_hashes,
            "config_paths": config_paths,
            "config_path_sha256": config_hashes,
            "configuration_mode": configuration_mode,
            "experiment_registration": registration,
            "post_hoc_status": post_hoc_status(record["version"], registration),
            "inventory_recorded_at": str(scope["inventory_as_of"]),
            "retrospective_inventory": record["version"] != "CURRENT_MAINLINE",
            "changelog_anchor": record["changelog_anchor"],
            "output_dir": record["output"]["directory"],
            "output_manifest_hash": record["output"]["standard_manifest"]["structure_manifest_sha256"],
            "output_generated_at": output_generated_at,
            "artifact_git_state": artifact_git_state,
            "artifact_git_recoverable": artifact_git_recoverable,
            "source_git_recoverability": source_git_recoverability,
            "task_brief_git_recoverable": task_brief_git_recoverable,
            "research_status": record["lifecycle_status"],
            "mainline_role": role["role"],
            "mainline_role_evidence": list(role["evidence"]),
            "consistency": consistency,
            "missing_requirements": missing,
            "notes": notes,
            "governance_status": "pass" if not missing else "fail",
        }
    )
    return record


def build_inventory(root: Path, scope: dict[str, Any]) -> dict[str, Any]:
    versions = expected_versions(scope)
    outputs = discover_version_outputs(root, versions)
    producers = discover_producers(root)
    briefs = discover_task_briefs(root)
    tracked = git_tracked_paths(root)
    changelog_path = root / "logs" / "version_changelog.md"
    changelog_text = changelog_path.read_text(encoding="utf-8-sig")
    changelog_section = parse_changelog_inventory_section(changelog_text)
    active = active_cohort(root, scope)
    required_outputs = [str(value) for value in scope["standard_output_artifacts"]]
    roles = detailed_mainline_roles(root, scope, outputs, producers)
    records: list[dict[str, Any]] = []

    for sequence_ordinal, version in enumerate(versions, start=1):
        output = outputs[version]
        matches = producers.get(output["output_dir"], [])
        producer_status = "matched" if len(matches) == 1 else ("missing" if not matches else "ambiguous")
        producer = matches[0] if len(matches) == 1 else {"source": "", "binding": "", "config_paths": []}
        configs = _config_info(root, producer["config_paths"])
        brief = _brief_info(root, version, briefs)
        rule = scope_rule(version, scope["registration_rules"])
        registration = registration_evidence(root, rule)
        manifest = standard_output_manifest(root, output["output_dir"], required_outputs)
        cohort_applicable = version_in_range(version, scope["cohort_scope"]["start"], scope["cohort_scope"]["end"])
        cohort = assess_cohort(output["summary"], active, cohort_applicable)
        boundary = assess_boundary(output["summary"], scope["evidence_boundary"], current=False)
        relation = scope_rule(version, scope["mainline_relations"])["relation"]
        changelog_anchor = changelog_anchor_for_version(changelog_section, version)
        changelog_status = changelog_anchor["status"]
        source_git_status = git_path_status(root, producer["source"], tracked) if producer["source"] else "missing"
        config_git_statuses = [git_path_status(root, path, tracked) for path in configs["paths"]]
        config_git_status = "not_declared" if not config_git_statuses else ("tracked" if set(config_git_statuses) == {"tracked"} else "mixed_or_untracked")
        brief_git_status = git_path_status(root, brief["path"], tracked) if brief["path"] else "missing"
        output_git_status = git_path_status(root, output["output_dir"], tracked)
        missing: list[str] = []
        if producer_status != "matched":
            missing.append("producer_source")
        if brief["status"] != "present":
            missing.append("task_brief")
        if registration["status"] not in {"valid", "explicit_post_hoc"}:
            missing.append("preregistration_or_explicit_post_hoc")
        if changelog_status != "present":
            missing.append("changelog_entry")
        if manifest["status"] != "complete":
            missing.append("standard_output_manifest")
        if boundary["status"] != "consistent":
            missing.append("evidence_boundary_consistency")
        if cohort["status"] == "stale_or_mismatched_active_pair":
            missing.append("active_cohort_pair_consistency")
        elif cohort["status"] == "not_declared_in_run_summary":
            missing.append("active_cohort_reference")
        state_status = (
            "inconsistent"
            if boundary["status"] == "inconsistent" or cohort["status"] == "stale_or_mismatched_active_pair"
            else ("undetermined" if cohort["status"] == "not_declared_in_run_summary" else "consistent")
        )
        records.append(
            enrich_record(
                root,
                scope,
                {
                "version": version,
                "run_summary_version": output["run_summary_version"],
                "goal": output["goal"],
                "producer": {
                    "source": producer["source"],
                    "output_binding": producer["binding"],
                    "match_method": "static_OUT_OUTPUT_path_plus_run_summary_version",
                    "status": producer_status,
                    "candidates": matches,
                },
                "config": configs,
                "task_brief": brief,
                "registration": registration,
                "post_hoc": bool(rule["post_hoc"]),
                "inventory_recorded_retrospectively": True,
                "historical_timestamp_claimed": False,
                "changelog": {"path": "logs/version_changelog.md", "status": changelog_status},
                "changelog_anchor": changelog_anchor,
                "output": {
                    "directory": output["output_dir"],
                    "run_summary_path": output["run_summary_path"],
                    "standard_manifest": manifest,
                },
                "git": {
                    "producer_source": source_git_status,
                    "config_paths": config_git_status,
                    "task_brief": brief_git_status,
                    "output_directory": output_git_status,
                },
                "in_current_mainline": False,
                "mainline_relation": relation,
                "cohort": cohort,
                "evidence_boundary": boundary,
                "state_consistency_status": state_status,
                "lifecycle_status": lifecycle_status(output["summary"]),
                "governance_status": "pass" if not missing else "fail",
                "missing_requirements": missing,
                },
                sequence_ordinal,
                roles[version],
            )
        )

    current_record = build_current_mainline_record(
        root, scope, briefs, tracked, changelog_section, active, required_outputs
    )
    records.append(enrich_record(root, scope, current_record, len(versions) + 1, roles["CURRENT_MAINLINE"]))
    summary = summarize(records, len(versions))
    fingerprint = hashlib.sha256(
        json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": scope["schema_version"],
        "inventory_as_of": scope["inventory_as_of"],
        "record_type": scope["record_type"],
        "historical_timestamp_claimed": bool(scope["historical_timestamp_claimed"]),
        "scope_path": "configs/research_governance_scope.json",
        "producer_match_method": "static OUT/OUTPUT/DEFAULT_OUTPUT path matched to output directory, then run_summary.version",
        "evidence_boundary": scope["evidence_boundary"],
        "active_cohort": active,
        "summary": summary,
        "records_sha256": fingerprint,
        "versions": records,
    }


def build_current_mainline_record(
    root: Path,
    scope: dict[str, Any],
    briefs: dict[str, list[str]],
    tracked: set[str],
    changelog_section: dict[str, Any],
    active: dict[str, Any],
    required_outputs: list[str],
) -> dict[str, Any]:
    seed = scope["current_mainline"]
    version = "CURRENT_MAINLINE"
    source = normalize_rel(seed["producer_source"])
    output_dir = normalize_rel(seed["output_dir"])
    summary = read_json(root / output_dir / "run_summary.json")
    configs = _config_info(root, [normalize_rel(value) for value in seed.get("config_paths", [])])
    brief = _brief_info(root, version, briefs)
    registration = registration_evidence(root, seed)
    manifest = standard_output_manifest(root, output_dir, required_outputs)
    boundary = assess_boundary(summary, scope["evidence_boundary"], current=True)
    changelog_anchor = changelog_anchor_for_version(changelog_section, version)
    changelog_status = changelog_anchor["status"]
    missing: list[str] = []
    if not (root / source).is_file():
        missing.append("producer_source")
    if brief["status"] != "present":
        missing.append("task_brief")
    if registration["status"] not in {"valid", "explicit_post_hoc"}:
        missing.append("preregistration_or_explicit_post_hoc")
    if changelog_status != "present":
        missing.append("changelog_entry")
    if manifest["status"] != "complete":
        missing.append("standard_output_manifest")
    if boundary["status"] != "consistent":
        missing.append("evidence_boundary_consistency")
    config_git_statuses = [git_path_status(root, path, tracked) for path in configs["paths"]]
    config_git_status = "not_declared" if not config_git_statuses else ("tracked" if set(config_git_statuses) == {"tracked"} else "mixed_or_untracked")
    return {
        "version": version,
        "run_summary_version": str(summary.get("version", "")),
        "goal": str(seed["goal"]),
        "producer": {
            "source": source,
            "output_binding": "config.output_dir",
            "match_method": "explicit_current_mainline_seed_plus_run_summary",
            "status": "matched" if (root / source).is_file() else "missing",
            "candidates": [{"source": source, "binding": "config.output_dir", "config_paths": configs["paths"]}],
        },
        "config": configs,
        "task_brief": brief,
        "registration": registration,
        "post_hoc": bool(seed["post_hoc"]),
        "inventory_recorded_retrospectively": False,
        "historical_timestamp_claimed": False,
        "changelog": {"path": "logs/version_changelog.md", "status": changelog_status},
        "changelog_anchor": changelog_anchor,
        "output": {
            "directory": output_dir,
            "run_summary_path": f"{output_dir}/run_summary.json",
            "standard_manifest": manifest,
        },
        "git": {
            "producer_source": git_path_status(root, source, tracked),
            "config_paths": config_git_status,
            "task_brief": git_path_status(root, brief["path"], tracked) if brief["path"] else "missing",
            "output_directory": git_path_status(root, output_dir, tracked),
        },
        "in_current_mainline": True,
        "mainline_relation": str(seed["relation"]),
        "cohort": {
            "applicable": False,
            "status": "referenced_indirectly_by_forward_evidence_only",
            "declared_cohort_id": "",
            "declared_manifest_hash": "",
            "active_cohort_id": active["cohort_id"],
            "active_manifest_hash": active["manifest_hash"],
        },
        "evidence_boundary": boundary,
        "state_consistency_status": "consistent" if boundary["status"] == "consistent" else "inconsistent",
        "lifecycle_status": lifecycle_status(summary),
        "governance_status": "pass" if not missing else "fail",
        "missing_requirements": missing,
    }


def summarize(records: list[dict[str, Any]], historical_count: int) -> dict[str, Any]:
    def count(path: tuple[str, ...]) -> dict[str, int]:
        values: list[str] = []
        for record in records:
            value: Any = record
            for part in path:
                value = value[part]
            values.append(str(value))
        return dict(sorted(Counter(values).items()))

    return {
        "expected_record_count": historical_count + 1,
        "record_count": len(records),
        "historical_version_count": sum(record["version"] != "CURRENT_MAINLINE" for record in records),
        "current_mainline_count": sum(record["version"] == "CURRENT_MAINLINE" for record in records),
        "producer_match_counts": count(("producer", "status")),
        "task_brief_counts": count(("task_brief", "status")),
        "registration_kind_counts": count(("registration", "kind")),
        "post_hoc_counts": count(("post_hoc",)),
        "changelog_counts": count(("changelog", "status")),
        "standard_output_counts": count(("output", "standard_manifest", "status")),
        "cohort_status_counts": count(("cohort", "status")),
        "state_consistency_counts": count(("state_consistency_status",)),
        "governance_status_counts": count(("governance_status",)),
    }


def csv_text(inventory: dict[str, Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for record in inventory["versions"]:
        producer = record["producer"]
        config = record["config"]
        brief = record["task_brief"]
        registration = record["registration"]
        manifest = record["output"]["standard_manifest"]
        cohort = record["cohort"]
        writer.writerow(
            {
                "schema_version": record["schema_version"],
                "version_id": record["version_id"],
                "sequence_ordinal": record["sequence_ordinal"],
                "implementation_version": record["implementation_version"],
                "version_class": record["version_class"],
                "objective": record["objective"],
                "source_paths": ";".join(record["source_paths"]),
                "source_path_sha256": ";".join(
                    f"{path}={record['source_path_sha256'][path]}" for path in record["source_paths"]
                ),
                "configuration_mode": record["configuration_mode"],
                "experiment_registration_status": record["experiment_registration"]["status"],
                "post_hoc_status": record["post_hoc_status"],
                "inventory_recorded_at": record["inventory_recorded_at"],
                "retrospective_inventory": str(record["retrospective_inventory"]).lower(),
                "changelog_anchor": (
                    f"{record['changelog_anchor']['anchor_id']}@{record['changelog_anchor']['section_sha256']}"
                    if record["changelog_anchor"]["status"] == "present"
                    else ""
                ),
                "changelog_anchor_id": record["changelog_anchor"]["anchor_id"],
                "changelog_section_sha256": record["changelog_anchor"]["section_sha256"],
                "output_generated_at": record["output_generated_at"] or "",
                "artifact_git_state": record["artifact_git_state"],
                "artifact_git_recoverable": str(record["artifact_git_recoverable"]).lower(),
                "source_git_recoverability": record["source_git_recoverability"],
                "research_status": record["research_status"],
                "mainline_role": record["mainline_role"],
                "consistency_status": record["consistency"]["overall_status"],
                "notes": " | ".join(record["notes"]),
                "version": record["version"],
                "run_summary_version": record["run_summary_version"],
                "goal": record["goal"],
                "producer_source": producer["source"],
                "producer_match_status": producer["status"],
                "config_paths": ";".join(config["paths"]),
                "config_status": config["status"],
                "task_brief_path": brief["path"],
                "task_brief_status": brief["status"],
                "registration_kind": registration["kind"],
                "registration_status": registration["status"],
                "registration_paths": ";".join(registration["paths"]),
                "experiment_ids": ";".join(registration["experiment_ids"]),
                "post_hoc": str(record["post_hoc"]).lower(),
                "inventory_recorded_retrospectively": str(record["inventory_recorded_retrospectively"]).lower(),
                "changelog_status": record["changelog"]["status"],
                "output_dir": record["output"]["directory"],
                "standard_output_status": manifest["status"],
                "standard_output_missing": ";".join(manifest["missing_artifacts"]),
                "output_structure_manifest_sha256": manifest["structure_manifest_sha256"],
                "source_git_status": record["git"]["producer_source"],
                "config_git_status": record["git"]["config_paths"],
                "task_brief_git_status": record["git"]["task_brief"],
                "output_git_status": record["git"]["output_directory"],
                "in_current_mainline": str(record["in_current_mainline"]).lower(),
                "mainline_relation": record["mainline_relation"],
                "cohort_status": cohort["status"],
                "declared_cohort_id": cohort["declared_cohort_id"],
                "active_cohort_id": cohort["active_cohort_id"],
                "evidence_boundary_status": record["evidence_boundary"]["status"],
                "state_consistency_status": record["state_consistency_status"],
                "lifecycle_status": record["lifecycle_status"],
                "governance_status": record["governance_status"],
                "missing_requirements": ";".join(record["missing_requirements"]),
            }
        )
    return buffer.getvalue()


def json_text(inventory: dict[str, Any]) -> str:
    return json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _csv_bytes(rows: list[dict[str, Any]], fields: list[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _directory_structure_hash(path: Path) -> str:
    rows = [
        f"{normalize_rel(item.relative_to(path))}\t{item.stat().st_size}"
        for item in sorted(path.rglob("*"), key=lambda item: normalize_rel(item.relative_to(path)))
        if item.is_file()
    ]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def build_inventory_input_manifest(root: Path, inventory: dict[str, Any]) -> dict[str, Any]:
    roles_by_path: dict[str, set[str]] = {}

    def add(path: str, role: str) -> None:
        if path:
            roles_by_path.setdefault(normalize_rel(path), set()).add(role)

    add(str(inventory["scope_path"]), "governance_scope")
    add("logs/version_changelog.md", "changelog")
    add(str(inventory["active_cohort"]["pointer_path"]), "active_cohort_pointer")
    for record in inventory["versions"]:
        for path in record["source_paths"]:
            add(path, "producer_source")
        for path in record["config_paths"]:
            add(path, "configuration")
        if record["task_brief"]["path"]:
            add(record["task_brief"]["path"], "task_brief")
        for path in record["experiment_registration"]["paths"]:
            add(path, "experiment_registration")
        for path in [record["output"]["run_summary_path"], *[
            f"{record['output_dir'].rstrip('/')}/{name}"
            for name in record["output"]["standard_manifest"]["required_artifacts"]
            if name != "run_summary.json"
        ]]:
            add(path, "research_output")

    tracked = git_tracked_paths(root)
    git_states = git_path_statuses(root, roles_by_path, tracked)
    rows: list[dict[str, Any]] = []
    for relative in sorted(roles_by_path):
        path = root / relative
        if path.is_file():
            kind = "file"
            digest = sha256_file(path)
        elif path.is_dir():
            kind = "directory_structure"
            digest = _directory_structure_hash(path)
        else:
            kind = "missing"
            digest = ""
        rows.append(
            {
                "path": relative,
                "roles": sorted(roles_by_path[relative]),
                "kind": kind,
                "sha256": digest,
                "git_state": git_states[relative],
            }
        )
    payload = {
        "schema_version": "1.0.0",
        "manifest_type": "research_version_inventory_inputs",
        "inventory_recorded_at": inventory["inventory_as_of"],
        "input_count": len(rows),
        "missing_input_count": sum(row["kind"] == "missing" for row in rows),
        "inputs": rows,
    }
    payload["manifest_sha256"] = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def governance_gap_rows(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in inventory["versions"]:
        for requirement in record["missing_requirements"]:
            rows.append(
                {
                    "sequence_ordinal": record["sequence_ordinal"],
                    "version_id": record["version_id"],
                    "governance_status": record["governance_status"],
                    "missing_requirement": requirement,
                    "research_status": record["research_status"],
                    "notes": " | ".join(record["notes"]),
                }
            )
    return rows


def inventory_top_candidates_text(inventory: dict[str, Any]) -> str:
    fields = [
        "sequence_ordinal",
        "record_semantics",
        "investment_candidate",
        "version_id",
        "implementation_version",
        "version_class",
        "objective",
        "post_hoc_status",
        "research_status",
        "mainline_role",
        "governance_status",
        "missing_requirements",
    ]
    rows = [
        {
            "sequence_ordinal": record["sequence_ordinal"],
            "record_semantics": "governance_inventory_detail_not_investment_candidate",
            "investment_candidate": "false",
            "version_id": record["version_id"],
            "implementation_version": record["implementation_version"],
            "version_class": record["version_class"],
            "objective": record["objective"],
            "post_hoc_status": record["post_hoc_status"],
            "research_status": record["research_status"],
            "mainline_role": record["mainline_role"],
            "governance_status": record["governance_status"],
            "missing_requirements": ";".join(record["missing_requirements"]),
        }
        for record in inventory["versions"]
    ]
    return _csv_bytes(rows, fields).decode("utf-8")


def inventory_report_text(inventory: dict[str, Any]) -> str:
    summary = inventory["summary"]
    failures = summary["governance_status_counts"].get("fail", 0)
    status = "FAIL" if failures else "PASS"
    gap_count = sum(len(record["missing_requirements"]) for record in inventory["versions"])
    lines = [
        "# 研究版本治理库存审计",
        "",
        f"- 审计状态：`{status}`",
        f"- 库存记录：`{summary['record_count']}`（历史版本 {summary['historical_version_count']}，当前主线 {summary['current_mainline_count']}）",
        f"- 缺口明细：`{gap_count}`",
        f"- 记录哈希：`{inventory['records_sha256']}`",
        "",
        "## 证据边界",
        "",
        "本产物只审计研究版本治理和可恢复性。`top_candidates.csv` 是 65 条治理库存明细，不是股票、行业、ETF 或任何投资候选清单。",
        "",
        "项目当前仍为 `research_only / NO_ACTION`；强行业 Alpha 未验证，人工辅助交易与生产条件未就绪，自动交易被禁止。",
        "",
        "## 覆盖",
        "",
        "| 项目 | 结果 |",
        "| --- | ---: |",
        f"| expected set 精确覆盖 | {summary['record_count'] == summary['expected_record_count']} |",
        f"| producer 唯一匹配 | {summary['producer_match_counts'].get('matched', 0)}/65 |",
        f"| 标准输出完整 | {summary['standard_output_counts'].get('complete', 0)}/65 |",
        f"| task brief 存在 | {summary['task_brief_counts'].get('present', 0)}/65 |",
        f"| 治理通过 | {summary['governance_status_counts'].get('pass', 0)}/65 |",
        "",
        "## 审计文件",
        "",
        "- `debug/input_manifest.json`：本次库存生成输入及其哈希。",
        "- `debug/structure_manifest.json`：标准四件套结构和语义合同。",
        "- `debug/governance_gaps.csv`：按版本、按要求拆开的缺口。",
        "",
        "允许治理总状态为 FAIL；FAIL 表示缺口仍在，不得被解释为策略失败或投资结论。",
        "",
    ]
    return "\n".join(lines)


def build_audit_artifacts(root: Path, inventory: dict[str, Any]) -> dict[str, bytes]:
    input_manifest = build_inventory_input_manifest(root, inventory)
    gaps = governance_gap_rows(inventory)
    top_candidates = inventory_top_candidates_text(inventory)
    report = inventory_report_text(inventory)
    audit_status = "fail" if inventory["summary"]["governance_status_counts"].get("fail", 0) else "pass"
    run_summary = {
        "version": "1.0.0",
        "policy_id": "research_version_inventory",
        "policy_status": "research_only",
        "inventory_recorded_at": inventory["inventory_as_of"],
        "status": audit_status,
        "audit_passed": audit_status == "pass",
        "record_count": inventory["summary"]["record_count"],
        "historical_version_count": inventory["summary"]["historical_version_count"],
        "current_mainline_count": inventory["summary"]["current_mainline_count"],
        "records_sha256": inventory["records_sha256"],
        "input_manifest_sha256": input_manifest["manifest_sha256"],
        "governance_gap_count": len(gaps),
        "governance_status_counts": inventory["summary"]["governance_status_counts"],
        "top_candidates_semantics": "governance_inventory_detail_not_investment_candidate",
        "investment_candidate_count": 0,
        "current_action": "NO_ACTION",
        "strong_industry_alpha_validated": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "治理库存可重复生成；任何未补齐要求继续保持 FAIL，不改变研究或投资结论。",
    }
    structure_manifest = {
        "schema_version": "1.0.0",
        "manifest_type": "research_version_inventory_standard_output_structure",
        "top_level_exact": ["report.md", "run_summary.json", "top_candidates.csv", "debug"],
        "debug_exact": ["governance_gaps.csv", "input_manifest.json", "structure_manifest.json"],
        "contracts": {
            "report.md": "human-readable governance audit",
            "run_summary.json": "machine-readable audit status; research_only and NO_ACTION boundary",
            "top_candidates.csv": "65 governance inventory details; never investment candidates",
            "debug/governance_gaps.csv": "one row per version requirement gap",
            "debug/input_manifest.json": "deterministic input paths and hashes",
            "debug/structure_manifest.json": "this non-circular path and semantics contract",
        },
    }
    gap_fields = [
        "sequence_ordinal",
        "version_id",
        "governance_status",
        "missing_requirement",
        "research_status",
        "notes",
    ]
    return {
        "report.md": report.encode("utf-8"),
        "run_summary.json": (json.dumps(run_summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        "top_candidates.csv": top_candidates.encode("utf-8"),
        "debug/governance_gaps.csv": _csv_bytes(gaps, gap_fields),
        "debug/input_manifest.json": (json.dumps(input_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        "debug/structure_manifest.json": (json.dumps(structure_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    }


def materialize_audit_artifacts(output_dir: Path, artifacts: dict[str, bytes], check: bool) -> None:
    expected_top = {"report.md", "run_summary.json", "top_candidates.csv", "debug"}
    expected_debug = {PurePosixPath(path).name for path in artifacts if path.startswith("debug/")}
    if output_dir.exists():
        observed_top = {path.name for path in output_dir.iterdir()}
        if observed_top - expected_top:
            raise SystemExit(f"unexpected inventory audit top-level entries: {sorted(observed_top - expected_top)}")
        debug_dir = output_dir / "debug"
        observed_debug = {path.name for path in debug_dir.iterdir()} if debug_dir.is_dir() else set()
        if observed_debug - expected_debug:
            raise SystemExit(f"unexpected inventory audit debug entries: {sorted(observed_debug - expected_debug)}")
    for relative, content in artifacts.items():
        path = output_dir / relative
        if check:
            if not path.is_file() or path.read_bytes() != content:
                raise SystemExit(f"inventory audit artifact is missing or stale: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    if check:
        observed_top = {path.name for path in output_dir.iterdir()} if output_dir.is_dir() else set()
        observed_debug = {path.name for path in (output_dir / "debug").iterdir()} if (output_dir / "debug").is_dir() else set()
        if observed_top != expected_top or observed_debug != expected_debug:
            raise SystemExit("inventory audit standard output structure is incomplete")


def write_or_check(path: Path, content: str, check: bool) -> None:
    encoded = content.encode("utf-8")
    if check:
        if not path.is_file() or path.read_bytes() != encoded:
            raise SystemExit(f"inventory artifact is stale: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the V4.72-V5.35 and current-mainline governance inventory.")
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV_OUTPUT)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Compare both logs and the standard four-piece audit output without writing.")
    parser.add_argument("--strict", action="store_true", help="Fail when any inventory record still has a governance gap.")
    args = parser.parse_args()

    scope = read_json(args.scope)
    inventory = build_inventory(ROOT, scope)
    write_or_check(args.json_output, json_text(inventory), args.check)
    write_or_check(args.csv_output, csv_text(inventory), args.check)
    materialize_audit_artifacts(args.audit_output, build_audit_artifacts(ROOT, inventory), args.check)
    summary = inventory["summary"]
    print(f"records={summary['record_count']}")
    print(f"historical_versions={summary['historical_version_count']}")
    print(f"current_mainline={summary['current_mainline_count']}")
    print(f"governance={summary['governance_status_counts']}")
    if args.strict and summary["governance_status_counts"].get("fail", 0):
        raise SystemExit("research governance inventory contains unresolved requirements")


if __name__ == "__main__":
    main()
