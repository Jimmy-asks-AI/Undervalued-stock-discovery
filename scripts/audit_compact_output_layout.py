#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "output_layout_policy.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit compact research output directory layout.")
    parser.add_argument("--output-dir", required=True, help="Research run output directory to audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="Output layout policy JSON path.")
    parser.add_argument(
        "--required-debug-files",
        nargs="*",
        default=[],
        help="Optional debug files that must exist for this run.",
    )
    args = parser.parse_args()

    policy = read_json(Path(args.policy))["compact_research_output"]
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir

    issues = audit_output_dir(output_dir, policy, args.required_debug_files)
    errors = [issue for issue in issues if issue["severity"] == "error"]

    print(f"output_dir={output_dir.resolve() if output_dir.exists() else output_dir}")
    print(f"errors={len(errors)}")
    print("status=pass" if not errors else "status=fail")
    for issue in issues:
        print(f"{issue['severity']} {issue['path']}: {issue['message']}")

    if errors:
        raise SystemExit(1)


def audit_output_dir(
    output_dir: Path,
    policy: dict[str, Any],
    required_debug_files: list[str],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not output_dir.exists():
        return [issue(str(output_dir), "error", "output directory does not exist")]
    if not output_dir.is_dir():
        return [issue(str(output_dir), "error", "output path is not a directory")]

    required_files = set(policy.get("required_top_level_files", []))
    required_dirs = set(policy.get("required_top_level_dirs", []))
    allowed_files = set(policy.get("allowed_top_level_files", []))
    allowed_dirs = set(policy.get("allowed_top_level_dirs", []))
    forbidden_names = set(policy.get("forbidden_legacy_names", []))

    entries = list(output_dir.iterdir())
    files = {entry.name for entry in entries if entry.is_file()}
    dirs = {entry.name for entry in entries if entry.is_dir()}

    for filename in sorted(required_files - files):
        issues.append(issue(str(output_dir / filename), "error", "required top-level file is missing"))
    for dirname in sorted(required_dirs - dirs):
        issues.append(issue(str(output_dir / dirname), "error", "required top-level directory is missing"))

    for filename in sorted(files - allowed_files):
        issues.append(issue(str(output_dir / filename), "error", "unexpected top-level file; put it under debug/ or rename to the compact contract"))
    for dirname in sorted(dirs - allowed_dirs):
        issues.append(issue(str(output_dir / dirname), "error", "unexpected top-level directory; only debug/ is allowed"))

    for name in sorted((files | dirs) & forbidden_names):
        issues.append(issue(str(output_dir / name), "error", "legacy output name is forbidden by compact output policy"))

    debug_dir = output_dir / "debug"
    if debug_dir.exists() and debug_dir.is_dir():
        for filename in required_debug_files:
            debug_file = debug_dir / filename
            if not debug_file.exists() or not debug_file.is_file():
                issues.append(issue(str(debug_file), "error", "required debug file is missing"))

    return issues


def issue(path: str, severity: str, message: str) -> dict[str, str]:
    return {"path": path, "severity": severity, "message": message}


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


if __name__ == "__main__":
    main()
