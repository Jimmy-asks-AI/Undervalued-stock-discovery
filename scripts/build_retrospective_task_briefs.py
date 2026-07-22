#!/usr/bin/env python
"""Explicitly materialize governance briefs from the version inventory.

This builder is deliberately separate from ``audit_task_briefs.py``.  Audits
must fail on missing briefs; they must never manufacture the evidence they are
supposed to inspect.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "logs" / "research_version_inventory.json"
DEFAULT_RETROSPECTIVE_DIR = ROOT / "strategy_lab" / "agents" / "task_briefs" / "retrospective"
DEFAULT_CURRENT_OUTPUT = ROOT / "strategy_lab" / "agents" / "task_briefs" / "current_mainline.json"
RECORDED_AT = "2026-07-18"
CURRENT_MAINLINE = "CURRENT_MAINLINE"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def expected_historical_versions() -> list[str]:
    return [*(f"V4.{minor:02d}" for minor in range(72, 100)), *(f"V5.{minor:02d}" for minor in range(0, 36))]


def version_slug(version: str) -> str:
    if version == CURRENT_MAINLINE:
        return "current_mainline"
    return version.lower().replace(".", "_")


def validate_inventory(inventory: dict[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    rows = inventory.get("versions")
    if not isinstance(rows, list):
        raise ValueError("inventory.versions must be a list")
    by_version: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each inventory version must be an object")
        version = str(row.get("version", ""))
        if not version or version in by_version:
            raise ValueError(f"blank or duplicate inventory version: {version!r}")
        by_version[version] = row

    expected = expected_historical_versions() + [CURRENT_MAINLINE]
    if list(by_version) != expected:
        missing = [version for version in expected if version not in by_version]
        extra = [version for version in by_version if version not in expected]
        raise ValueError(f"inventory version order/scope mismatch; missing={missing}; extra={extra}")

    for version in expected:
        row = by_version[version]
        producer = row.get("producer", {})
        source = str(producer.get("source", ""))
        if producer.get("status") != "matched" or not source or not (root / source).is_file():
            raise ValueError(f"{version}: matched producer source is required")
        config = row.get("config", {})
        paths = config.get("paths", [])
        if not isinstance(paths, list) or any(not (root / str(path)).exists() for path in paths):
            raise ValueError(f"{version}: inventory config paths must be real or explicitly empty")
        output = row.get("output", {})
        manifest = output.get("standard_manifest", {})
        if manifest.get("status") != "complete" or manifest.get("missing_artifacts"):
            raise ValueError(f"{version}: complete standard output manifest is required")
        if version != CURRENT_MAINLINE:
            if row.get("inventory_recorded_retrospectively") is not True:
                raise ValueError(f"{version}: historical inventory marker is required")
            if row.get("historical_timestamp_claimed") is not False:
                raise ValueError(f"{version}: historical timestamps must not be claimed")
    return by_version


def required_output_paths(row: dict[str, Any]) -> list[str]:
    output = row["output"]
    directory = str(output["directory"]).rstrip("/")
    required = output["standard_manifest"]["required_artifacts"]
    return [f"{directory}/{name}" for name in required]


def allowed_input_paths(row: dict[str, Any]) -> list[str]:
    candidates = [
        str(row["producer"]["source"]),
        *[str(path) for path in row["config"]["paths"]],
        *[str(path) for path in row["registration"]["paths"]],
        str(row["output"]["run_summary_path"]),
        f"{str(row['output']['directory']).rstrip('/')}/report.md",
        "logs/research_version_inventory.json",
    ]
    return list(dict.fromkeys(path for path in candidates if path))


def registration_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    registration = row["registration"]
    return {
        "kind": str(registration["kind"]),
        "status": str(registration["status"]),
        "paths": [str(path) for path in registration["paths"]],
        "experiment_ids": [str(value) for value in registration["experiment_ids"]],
        "post_hoc": bool(row["post_hoc"]),
        "declaration_source": str(registration["declaration_source"]),
        "boundary": str(registration["boundary"]),
    }


def brief_post_hoc_status(row: dict[str, Any]) -> str:
    return str(row.get("post_hoc_status") or "unknown_requires_review")


def output_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    output = row["output"]
    manifest = output["standard_manifest"]
    return {
        "directory": str(output["directory"]),
        "run_summary_path": str(output["run_summary_path"]),
        "run_summary_version": str(row["run_summary_version"]),
        "standard_output_status": str(manifest["status"]),
        "required_paths": required_output_paths(row),
        "missing_paths": [str(path) for path in manifest["missing_artifacts"]],
        "structure_manifest_sha256": str(manifest["structure_manifest_sha256"]),
        "run_summary_sha256": str(manifest["run_summary_sha256"]),
    }


def common_brief_fields(row: dict[str, Any]) -> dict[str, Any]:
    version = str(row["version"])
    evidence_paths = list(
        dict.fromkeys(
            [
                *[str(path) for path in row["source_paths"]],
                *[str(path) for path in row["config_paths"]],
                *[str(path) for path in row["registration"]["paths"]],
                *required_output_paths(row),
            ]
        )
    )
    return {
        "version": version,
        "objective": str(row["goal"]),
        "owner_agent": "reproducibility_engineer",
        "allowed_input_paths": allowed_input_paths(row),
        "forbidden_input_patterns": [
            "validated_alpha",
            "production_ready=true",
            "auto_execution_allowed=true",
            "buy_signal",
            "sell_signal",
            "order",
        ],
        "required_output_paths": required_output_paths(row),
        "acceptance_checks": [
            {
                "id": f"check_{version_slug(version)}_governance_brief",
                "description": "核对该治理 brief 与研究版本库存逐字一致。",
                "command": f"python scripts/build_retrospective_task_briefs.py --check --version {version}",
                "expected_status": "exit_0",
            }
        ],
        "objective_source": f"logs/research_version_inventory.json#{version}",
        "source_paths": [str(path) for path in row["source_paths"]],
        "config_paths": [str(path) for path in row["config_paths"]],
        "config_status": str(row["config"]["status"]),
        "evidence_paths": evidence_paths,
        "registration": registration_snapshot(row),
        "post_hoc": bool(row["post_hoc"]),
        "post_hoc_status": brief_post_hoc_status(row),
        "version_class": str(row["version_class"]),
        "notes": [str(note) for note in row["notes"]],
        "output_manifest": output_snapshot(row),
        "inventory_source": "logs/research_version_inventory.json",
        "mainline_relation": str(row["mainline_relation"]),
        "research_result_status": str(row["lifecycle_status"]),
        "evidence_boundary_status": str(row["evidence_boundary"]["status"]),
        "historical_timestamp_claimed": False,
    }


def build_historical_brief(row: dict[str, Any]) -> dict[str, Any]:
    version = str(row["version"])
    if version == CURRENT_MAINLINE:
        raise ValueError("historical builder cannot receive CURRENT_MAINLINE")
    brief = {
        "task_id": f"retrospective_{version_slug(version)}_research_inventory",
        "record_type": "retrospective_inventory",
        "recorded_at": RECORDED_AT,
        "inventory_recorded_retrospectively": True,
        "historical_owner_claimed": False,
        "task_status": "passed",
        "task_status_scope": "retrospective_inventory_record_only",
        **common_brief_fields(row),
        "decision_boundary": (
            f"该文件是 {RECORDED_AT} 补建的 {version} retrospective inventory；"
            "不是该版本执行当时的任务书或预注册记录，不得据此改写、抬升或追认原研究结论。"
        ),
        "research_boundary": (
            "research_only；强行业 Alpha 尚未验证；人工辅助交易尚未就绪；"
            "production_ready=false；auto_execution_allowed=false；禁止自动交易。"
        ),
    }
    return brief


def build_current_brief(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("version") != CURRENT_MAINLINE:
        raise ValueError("current builder requires CURRENT_MAINLINE")
    return {
        "task_id": "current_mainline_operating_brief",
        "record_type": "current_operating_brief",
        "recorded_at": RECORDED_AT,
        "inventory_recorded_retrospectively": False,
        "historical_owner_claimed": False,
        "task_status": "ready",
        "task_status_scope": "current_operating_governance",
        **common_brief_fields(row),
        "decision_boundary": (
            "当前主线只提供 ETF 辅助人工决策研究；任一硬门未通过即保持 NO_ACTION。"
            "不得把历史框架评分、探索样本或单次审计通过解释为交易许可。"
        ),
        "research_boundary": (
            "research_only；current_action=NO_ACTION；强行业 Alpha 尚未验证；"
            "manual_decision_support_ready=false；production_ready=false；"
            "auto_execution_allowed=false；禁止自动交易。"
        ),
    }


def build_briefs(inventory: dict[str, Any], root: Path = ROOT) -> dict[str, dict[str, Any]]:
    by_version = validate_inventory(inventory, root)
    briefs = {
        version: build_historical_brief(by_version[version])
        for version in expected_historical_versions()
    }
    briefs[CURRENT_MAINLINE] = build_current_brief(by_version[CURRENT_MAINLINE])
    return briefs


def brief_text(brief: dict[str, Any]) -> str:
    return json.dumps(brief, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def brief_path(version: str, retrospective_dir: Path, current_output: Path) -> Path:
    if version == CURRENT_MAINLINE:
        return current_output
    return retrospective_dir / f"{version_slug(version)}_research_inventory.json"


def select_versions(requested: str | None) -> list[str]:
    expected = expected_historical_versions() + [CURRENT_MAINLINE]
    if requested is None:
        return expected
    normalized = requested.strip().upper()
    if normalized not in expected:
        raise ValueError(f"version is outside governance scope: {requested}")
    return [normalized]


def unexpected_briefs(retrospective_dir: Path, current_output: Path) -> list[Path]:
    expected = {
        brief_path(version, retrospective_dir, current_output).resolve()
        for version in expected_historical_versions() + [CURRENT_MAINLINE]
    }
    observed = {path.resolve() for path in retrospective_dir.glob("*.json")} if retrospective_dir.exists() else set()
    if current_output.exists():
        observed.add(current_output.resolve())
    return sorted(observed - expected)


def materialize(
    briefs: dict[str, dict[str, Any]],
    versions: list[str],
    retrospective_dir: Path,
    current_output: Path,
    *,
    check: bool,
) -> None:
    if len(versions) == len(briefs):
        extras = unexpected_briefs(retrospective_dir, current_output)
        if extras:
            raise SystemExit("unexpected governance brief files: " + ", ".join(str(path) for path in extras))
    for version in versions:
        path = brief_path(version, retrospective_dir, current_output)
        expected = brief_text(briefs[version]).encode("utf-8")
        if check:
            if not path.is_file() or path.read_bytes() != expected:
                raise SystemExit(f"governance brief is missing or stale: {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicitly build retrospective and current-mainline task briefs.")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--retrospective-dir", type=Path, default=DEFAULT_RETROSPECTIVE_DIR)
    parser.add_argument("--current-output", type=Path, default=DEFAULT_CURRENT_OUTPUT)
    parser.add_argument("--version", help="Build or check one canonical version, for example V5.00 or CURRENT_MAINLINE.")
    parser.add_argument("--check", action="store_true", help="Compare expected bytes without writing any brief.")
    args = parser.parse_args()

    briefs = build_briefs(read_json(args.inventory), ROOT)
    versions = select_versions(args.version)
    materialize(briefs, versions, args.retrospective_dir, args.current_output, check=args.check)
    print(f"mode={'check' if args.check else 'write'}")
    print(f"brief_count={len(versions)}")
    print(f"historical_count={sum(version != CURRENT_MAINLINE for version in versions)}")
    print(f"current_mainline_count={sum(version == CURRENT_MAINLINE for version in versions)}")


if __name__ == "__main__":
    main()
