#!/usr/bin/env python
"""Run and record the reproducible Dashboard trust acceptance command matrix."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "strategy_lab" / "research_dashboard"
DEFAULT_OUTPUT = ROOT / "outputs" / "audit" / "dashboard_trust_remediation" / "debug"
SHANGHAI_TZ = timezone(timedelta(hours=8))


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def command_record(label: str, argv: list[str], cwd: Path) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(  # noqa: S603 - fixed repository-owned command matrix
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
    )
    duration = round(time.perf_counter() - started, 3)
    output = "\n".join(part.rstrip() for part in [completed.stdout, completed.stderr] if part.strip())
    return {
        "label": label,
        "argv": argv,
        "cwd": cwd.relative_to(ROOT).as_posix() or ".",
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "passed": completed.returncode == 0,
        "output": output,
    }


def current_state_alignment() -> dict[str, Any]:
    dashboard = json.loads((DASHBOARD / "public" / "data" / "dashboard_data.json").read_text(encoding="utf-8"))
    status_snapshot = json.loads((ROOT / "outputs" / "audit" / "current_status" / "debug" / "status_snapshot.json").read_text(encoding="utf-8"))
    current_state = json.loads((ROOT / "outputs" / "audit" / "current_state_consistency" / "run_summary.json").read_text(encoding="utf-8"))
    trust = dashboard["trust_summary"]
    status = status_snapshot["status"]
    pairs = {
        "decision_as_of_date": [dashboard["decision_as_of_date"], status["decision_as_of"], current_state["current_as_of_date"]],
        "current_action": [trust["current_action"], status["action"], current_state["current_action"]],
        "policy_status": [trust["policy_status"], status["policy_status"], current_state["policy_status"]],
        "manual_support_ready": [trust["manual_support_ready"], status["manual_decision_support_ready"], current_state["manual_decision_support_ready"]],
        "production_ready": [trust["production_ready"], status["production_ready"], current_state["production_ready"]],
        "auto_execution_allowed": [trust["auto_execution_allowed"], status["auto_execution_allowed"], current_state["auto_execution_allowed"]],
        "active_cohort_id": [trust["active_cohort_id"], status["freeze_layers"]["fund_flow_evidence_cohort"]["cohort_id"], current_state["active_cohort_id"]],
        "active_cohort_manifest_hash": [trust["active_cohort_manifest_hash"], status["freeze_layers"]["fund_flow_evidence_cohort"]["manifest_hash"], current_state["active_cohort_manifest_hash"]],
    }
    checks = [
        {"field": field, "values": values, "passed": len({json.dumps(value, sort_keys=True) for value in values}) == 1}
        for field, values in pairs.items()
    ]
    checks.extend(
        [
            {"field": "status_valid", "values": [trust["status_valid"]], "passed": trust["status_valid"] is True},
            {"field": "state_consistent", "values": [trust["state_consistent"], status["state_consistent"], current_state["state_consistent"]], "passed": all([trust["state_consistent"], status["state_consistent"], current_state["state_consistent"]])},
            {"field": "cohort_consistent", "values": [trust["cohort_consistent"]], "passed": trust["cohort_consistent"] is True},
        ]
    )
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Dashboard 可信度命令验收",
        "",
        f"- 状态：`{payload['status']}`",
        f"- 生成时间：`{payload['generated_at']}`",
        f"- Python：`{payload['environment']['python']}`",
        f"- Node：`{payload['environment']['node']}`",
        f"- npm：`{payload['environment']['npm']}`",
        "",
        "| 命令 | 用时 | 退出码 | 结果 |",
        "|---|---:|---:|---|",
    ]
    for item in payload["commands"]:
        lines.append(
            f"| `{item['label']}` | {item['duration_seconds']:.3f}s | {item['exit_code']} | "
            f"{'PASS' if item['passed'] else 'FAIL'} |"
        )
    lines.extend(["", "## CURRENT_STATUS / current state / Dashboard 对齐", "", "| 字段 | 值 | 结果 |", "|---|---|---|"])
    for item in payload["alignment"]["checks"]:
        values = " = ".join(f"`{value}`" for value in item["values"])
        lines.append(f"| `{item['field']}` | {values} | {'PASS' if item['passed'] else 'FAIL'} |")
    lines.extend(["", "## 原始输出", ""])
    for item in payload["commands"]:
        lines.extend(
            [
                f"### {item['label']}",
                "",
                f"工作目录：`{item['cwd']}`",
                "",
                "```text",
                item["output"] or "(no output)",
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--skip-install", action="store_true", help="Skip npm ci for a quick local rerun.")
    args = parser.parse_args()

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is not available on PATH")
    python = sys.executable
    matrix: list[tuple[str, list[str], Path]] = [
        ("builder self-check", [python, "scripts/build_dashboard_dataset.py", "--self-check"], ROOT),
        ("builder", [python, "scripts/build_dashboard_dataset.py"], ROOT),
        ("uv lock check", [python, "-m", "uv", "lock", "--check"], ROOT),
        ("UI QA self-check", [python, "scripts/audit_dashboard_trust_ui.py", "--self-check"], ROOT),
    ]
    npm_matrix = [
        ("npm ci", [npm, "ci", "--no-audit", "--no-fund"], DASHBOARD),
        ("npm run validate:data", [npm, "run", "validate:data"], DASHBOARD),
        ("npm test", [npm, "test"], DASHBOARD),
        ("npm run check", [npm, "run", "check"], DASHBOARD),
        ("npm run build", [npm, "run", "build"], DASHBOARD),
    ]
    if args.skip_install:
        npm_matrix = npm_matrix[1:]
    matrix[2:2] = npm_matrix

    commands = [command_record(label, argv, cwd) for label, argv, cwd in matrix]
    node_version = subprocess.run([shutil.which("node") or "node", "--version"], capture_output=True, text=True, check=False).stdout.strip()
    npm_version = subprocess.run([npm, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    alignment = current_state_alignment()
    payload = {
        "schema_version": "dashboard-trust-command-acceptance-v1",
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "status": "pass" if all(item["passed"] for item in commands) and alignment["passed"] else "fail",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "node": node_version,
            "npm": npm_version,
        },
        "commands": commands,
        "alignment": alignment,
    }
    output_dir = Path(args.output_dir)
    atomic_write(output_dir / "acceptance_commands.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    atomic_write(output_dir / "acceptance_commands.md", render_markdown(payload))
    print(f"status={payload['status']}")
    print(f"commands={sum(item['passed'] for item in commands)}/{len(commands)}")
    print(f"json={(output_dir / 'acceptance_commands.json').relative_to(ROOT).as_posix()}")
    print(f"report={(output_dir / 'acceptance_commands.md').relative_to(ROOT).as_posix()}")
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
