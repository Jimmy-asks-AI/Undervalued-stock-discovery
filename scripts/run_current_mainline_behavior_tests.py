from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = ROOT / "tests" / "test_current_mainline_behavior.py"
DEFAULT_OUTPUT = ROOT / "outputs" / "test" / "current_mainline_behavior"
LAYERS = ("contract", "unit", "integration", "data-quality", "research-evidence")


def classify_layer(test_name: str) -> str:
    prefixes = {
        "test_contract_": "contract",
        "test_unit_": "unit",
        "test_integration_": "integration",
        "test_data_quality_": "data-quality",
        "test_research_evidence_": "research-evidence",
    }
    for prefix, layer in prefixes.items():
        if test_name.startswith(prefix):
            return layer
    raise ValueError(f"行为测试缺少证据层级前缀: {test_name}")


def parse_junit(path: Path) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot()
    rows: list[dict[str, Any]] = []
    for case in root.iter("testcase"):
        name = str(case.attrib.get("name", ""))
        status = "passed"
        detail = ""
        for child_status in ("failure", "error", "skipped"):
            child = case.find(child_status)
            if child is not None:
                status = child_status
                detail = str(child.attrib.get("message") or child.text or "").strip()
                break
        rows.append(
            {
                "verification_layer": classify_layer(name),
                "test_name": name,
                "status": status,
                "duration_seconds": round(float(case.attrib.get("time", 0.0)), 6),
                "detail": detail,
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["verification_layer", "test_name", "status", "duration_seconds", "detail"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_summary(rows: list[dict[str, Any]], command: list[str], returncode: int) -> dict[str, Any]:
    status_counts = Counter(str(row["status"]) for row in rows)
    layer_summaries: dict[str, dict[str, Any]] = {}
    for layer in LAYERS:
        selected = [row for row in rows if row["verification_layer"] == layer]
        passed = sum(row["status"] == "passed" for row in selected)
        failed = sum(row["status"] in {"failure", "error"} for row in selected)
        skipped = sum(row["status"] == "skipped" for row in selected)
        layer_summaries[layer] = {
            "test_count": len(selected),
            "pass_count": passed,
            "fail_count": failed,
            "skip_count": skipped,
            "passed": bool(selected) and failed == 0 and skipped == 0 and passed == len(selected),
        }
    passed_count = int(status_counts["passed"])
    fail_count = int(status_counts["failure"])
    error_count = int(status_counts["error"])
    skip_count = int(status_counts["skipped"])
    suite_passed = (
        returncode == 0
        and bool(rows)
        and passed_count == len(rows)
        and all(item["passed"] for item in layer_summaries.values())
    )
    return {
        "schema_version": "current-mainline-behavior-v1",
        "suite_type": "independent_behavior_tests",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "offline": True,
        "live_network_required": False,
        "test_file": TEST_FILE.relative_to(ROOT).as_posix(),
        "command": command,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "pytest_returncode": returncode,
        "behavior_test_count": len(rows),
        "behavior_test_pass_count": passed_count,
        "behavior_test_fail_count": fail_count,
        "behavior_test_error_count": error_count,
        "behavior_test_skip_count": skip_count,
        "behavior_tests_passed": suite_passed,
        "verification_layers": layer_summaries,
        "evidence_boundary": (
            "本套件使用固定 fixture、临时目录与 monkeypatch 验证代码行为；"
            "不访问实时网络，也不把源码字符串检查计入行为测试。"
        ),
    }


def render_report(summary: dict[str, Any], rows: list[dict[str, Any]], stderr: str) -> str:
    verdict = "通过" if summary["behavior_tests_passed"] else "未通过"
    lines = [
        "# 当前主线独立行为测试报告",
        "",
        f"- 结论：**{verdict}**",
        f"- 行为测试：{summary['behavior_test_pass_count']}/{summary['behavior_test_count']} 通过",
        "- 执行方式：离线；不访问实时行情、交易所接口或数据商接口",
        "- 证据边界：源码字符串检查不计入本报告的行为测试通过数",
        "",
        "## 分层结果",
        "",
        "| 层级 | 通过 | 总数 | 结论 |",
        "|---|---:|---:|---|",
    ]
    for layer in LAYERS:
        item = summary["verification_layers"][layer]
        layer_verdict = "通过" if item["passed"] else "未通过"
        lines.append(f"| {layer} | {item['pass_count']} | {item['test_count']} | {layer_verdict} |")
    lines.extend(["", "## 未通过项", ""])
    failed = [row for row in rows if row["status"] != "passed"]
    if failed:
        for row in failed:
            lines.append(f"- `{row['test_name']}`：{row['status']}；{row['detail']}")
    else:
        lines.append("无。")
    lines.extend(
        [
            "",
            "## 文件说明",
            "",
            "- `run_summary.json`：机器可读总表和独立 `behavior_test_pass_count`。",
            "- `top_candidates.csv`：逐项测试结果，失败项排在前面。",
            "- `debug/test_results.csv`：完整逐项结果。",
            "- `debug/junit.xml`：pytest 原始 JUnit 证据。",
        ]
    )
    if stderr.strip():
        lines.extend(["", "## 运行器标准错误摘要", "", "```text", stderr.strip()[-4000:], "```"])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="运行当前主线独立离线行为测试并生成标准四件套。")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    debug = output / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    junit = debug / "junit.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        TEST_FILE.relative_to(ROOT).as_posix(),
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={junit}",
    ]
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    (debug / "pytest.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (debug / "pytest.stderr.txt").write_text(result.stderr, encoding="utf-8")
    if not junit.exists():
        raise SystemExit(f"pytest 未生成 JUnit 结果，returncode={result.returncode}: {result.stderr}")
    rows = parse_junit(junit)
    summary = build_summary(rows, command, result.returncode)
    ordered = sorted(rows, key=lambda row: (row["status"] == "passed", row["verification_layer"], row["test_name"]))
    write_csv(output / "top_candidates.csv", ordered)
    write_csv(debug / "test_results.csv", rows)
    (output / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "report.md").write_text(render_report(summary, rows, result.stderr), encoding="utf-8")
    print(f"behavior_tests={summary['behavior_test_count']}")
    print(f"behavior_test_pass_count={summary['behavior_test_pass_count']}")
    print(f"behavior_tests_passed={str(summary['behavior_tests_passed']).lower()}")
    if not summary["behavior_tests_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
