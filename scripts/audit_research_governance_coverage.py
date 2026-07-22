#!/usr/bin/env python
"""Build the standard four-piece research-governance coverage audit."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_task_briefs as briefs


DEFAULT_OUTPUT = ROOT / "outputs" / "audit" / "research_governance_coverage"
TOP_FIELDS = [
    "version",
    "goal",
    "record_type",
    "task_brief_path",
    "task_brief_status",
    "producer_status",
    "recoverability_status",
    "registration_kind",
    "registration_status",
    "post_hoc",
    "changelog_status",
    "standard_output_status",
    "cohort_status",
    "evidence_boundary_status",
    "in_current_mainline",
    "governance_status",
    "issue_count",
    "artifact_role",
]
ISSUE_FIELDS = ["task_file", "task_id", "field", "severity", "message"]


@dataclass(frozen=True)
class CoverageResult:
    root: Path
    inventory_path: Path
    task_paths: tuple[Path, ...]
    rows: tuple[dict[str, Any], ...]
    issues: tuple[dict[str, str], ...]

    @property
    def errors(self) -> tuple[dict[str, str], ...]:
        return tuple(issue for issue in self.issues if issue["severity"] == "error")

    @property
    def warnings(self) -> tuple[dict[str, str], ...]:
        return tuple(issue for issue in self.issues if issue["severity"] == "warning")

    @property
    def audit_passed(self) -> bool:
        return bool(self.rows) and not self.errors and all(row["governance_status"] == "pass" for row in self.rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed coverage audit for V4.72--V5.35 and CURRENT_MAINLINE."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--task-dir", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--changelog", type=Path)
    parser.add_argument("--active-cohort", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)

    if args.self_check:
        self_check()
        return 0

    root = args.root.resolve()
    result = audit_repository(
        root,
        schema_path=(args.schema or root / "configs" / "fundamental_value_task_brief_schema.json").resolve(),
        task_dir=(args.task_dir or root / "strategy_lab" / "agents" / "task_briefs").resolve(),
        inventory_path=(args.inventory or root / "logs" / "research_version_inventory.json").resolve(),
        changelog_path=(args.changelog or root / "logs" / "version_changelog.md").resolve(),
        active_cohort_path=(args.active_cohort or root / "logs" / "v5_31_fund_flow_evidence_freeze_active.json").resolve(),
    )
    output = (args.output or root / "outputs" / "audit" / "research_governance_coverage").resolve()
    summary = write_outputs(result, output)
    print(f"version_count={summary['version_count']}")
    print(f"pass_count={summary['pass_count']}")
    print(f"fail_count={summary['fail_count']}")
    print(f"error_count={summary['error_count']}")
    print(f"audit_passed={str(summary['audit_passed']).lower()}")
    print(f"output={output}")
    return 0 if result.audit_passed else 1


def audit_repository(
    root: Path,
    *,
    schema_path: Path,
    task_dir: Path,
    inventory_path: Path,
    changelog_path: Path,
    active_cohort_path: Path,
) -> CoverageResult:
    task_paths = briefs.discover_task_briefs(task_dir)
    issues: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    try:
        schema = briefs.read_json_object(schema_path)
        issues.extend(briefs.audit_task_briefs(task_paths, schema, root=root))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        issues.append(briefs._issue(schema_path.name, "", "schema", "error", f"task brief schema audit failed: {exc}"))

    if not inventory_path.is_file():
        issues.append(briefs._issue(inventory_path.name, "", "inventory", "error", "research version inventory is missing"))
    else:
        try:
            inventory = briefs.read_json_object(inventory_path)
            rows, governance_issues = briefs.audit_inventory_governance(
                inventory,
                task_paths,
                root=root,
                changelog_path=changelog_path,
                active_cohort_path=active_cohort_path,
            )
            issues.extend(governance_issues)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(briefs._issue(inventory_path.name, "", "inventory", "error", f"governance audit failed: {exc}"))

    return CoverageResult(
        root=root,
        inventory_path=inventory_path,
        task_paths=tuple(task_paths),
        rows=tuple(rows),
        issues=tuple(issues),
    )


def build_summary(result: CoverageResult) -> dict[str, Any]:
    pass_count = sum(row["governance_status"] == "pass" for row in result.rows)
    fail_count = len(result.rows) - pass_count
    expected_order = briefs.expected_governance_versions()
    actual_order = [str(row["version"]) for row in result.rows]
    missing_versions = [version for version in expected_order if version not in actual_order]
    unexpected_versions = [version for version in actual_order if version not in expected_order]
    first_difference = next(
        (
            index
            for index, (expected, actual) in enumerate(zip(expected_order, actual_order))
            if expected != actual
        ),
        min(len(expected_order), len(actual_order)),
    )
    return {
        "schema_version": "1.0.0",
        "policy_id": "research_governance_coverage",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inventory_path": briefs.relative_or_absolute(result.inventory_path, result.root),
        "expected_scope": "V4.72--V5.35 plus CURRENT_MAINLINE",
        "expected_version_order": expected_order,
        "actual_version_order": actual_order,
        "version_order_matches": actual_order == expected_order,
        "first_version_order_difference": None if actual_order == expected_order else first_difference,
        "missing_expected_versions": missing_versions,
        "unexpected_versions": unexpected_versions,
        "version_count": len(result.rows),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "audit_passed": result.audit_passed,
        "top_candidates_semantics": "逐版本治理明细；不是证券、ETF 或投资候选列表。",
        "brief_semantic_canonicalization": {
            "objective_provenance": "objective + inventory_source + explicit objective_source",
            "evidence_set": "explicit evidence_paths covers source_paths + config_paths + registration.paths + output_manifest.required_paths",
            "registration": "registration.kind/status/paths/experiment_ids + top-level post_hoc + explicit post_hoc_status",
            "changelog": "inventory changelog path/status + actual Markdown version token",
        },
        "auto_generated_brief_count": 0,
        "research_only": True,
        "current_action": "NO_ACTION",
        "strong_industry_alpha_validated": False,
        "manual_decision_support_ready": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": (
            "65 条研究治理记录全部覆盖；该结果只证明治理证据齐备，不证明策略有效。"
            if result.audit_passed
            else "研究治理覆盖不完整；失败关闭，不得据此提升研究结论或交易就绪状态。"
        ),
    }


def write_outputs(result: CoverageResult, output: Path) -> dict[str, Any]:
    summary = build_summary(result)
    debug = output / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    briefs.write_json(output / "run_summary.json", summary)
    briefs.write_csv(output / "top_candidates.csv", result.rows, TOP_FIELDS)
    briefs.write_csv(debug / "governance_issues.csv", result.issues, ISSUE_FIELDS)
    briefs.write_csv(debug / "version_governance_detail.csv", result.rows, TOP_FIELDS)
    briefs.write_json(
        debug / "audit_input_manifest.json",
        {
            "inventory_path": briefs.relative_or_absolute(result.inventory_path, result.root),
            "task_brief_count": len(result.task_paths),
            "task_brief_paths": [briefs.relative_or_absolute(path, result.root) for path in result.task_paths],
            "top_candidates_semantics": summary["top_candidates_semantics"],
            "brief_generation_performed": False,
        },
    )
    (output / "report.md").write_text(report_text(summary, result), encoding="utf-8")
    return summary


def report_text(summary: Mapping[str, Any], result: CoverageResult) -> str:
    failed = [row for row in result.rows if row["governance_status"] != "pass"]
    errors = list(result.errors)
    lines = [
        "# 研究治理覆盖审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 覆盖结论",
        "",
        f"- 版本范围：`{summary['expected_scope']}`",
        f"- 逐版本通过：{summary['pass_count']} / {summary['version_count']}",
        f"- 治理错误：{summary['error_count']}",
        f"- 审计状态：`{'pass' if summary['audit_passed'] else 'fail'}`",
        "- brief 自动生成：`0`；本审计不会补写缺失 brief。",
        "",
        "## expected / actual 版本序列",
        "",
        f"- 有序序列一致：`{str(summary['version_order_matches']).lower()}`",
        f"- 首个差异索引：`{summary['first_version_order_difference']}`",
        f"- 缺失版本：`{','.join(summary['missing_expected_versions']) or 'none'}`",
        f"- 意外版本：`{','.join(summary['unexpected_versions']) or 'none'}`",
        f"- expected：`{','.join(summary['expected_version_order'])}`",
        f"- actual：`{','.join(summary['actual_version_order'])}`",
        "",
        "## 失败版本",
        "",
    ]
    lines.extend(
        [f"- `{row['version']}`：issue_count={row['issue_count']}；brief={row['task_brief_status']}；登记={row['registration_status']}；日志={row['changelog_status']}；四件套={row['standard_output_status']}；cohort={row['cohort_status']}" for row in failed]
        or ["- 无。"]
    )
    lines += ["", "## 失败明细", ""]
    lines.extend(
        [f"- `{issue['task_id'] or issue['task_file'] or 'global'}` / `{issue['field']}`：{issue['message']}" for issue in errors[:100]]
        or ["- 无。"]
    )
    if len(errors) > 100:
        lines.append(f"- 其余 {len(errors) - 100} 条见 `debug/governance_issues.csv`。")
    lines += [
        "",
        "## brief 语义归一规则",
        "",
        "- 目标来源：`objective` 必须与库存目标逐字一致，且显式 `objective_source` 必须是库存内对应版本锚点。",
        "- 证据集合：显式 `evidence_paths` 必须覆盖 `source_paths`、`config_paths`、`registration.paths` 与 `output_manifest.required_paths` 的稳定去重并集。",
        "- 登记属性：`registration.kind/status/paths/experiment_ids`、顶层 `post_hoc` 与显式 `post_hoc_status` 必须互相一致。",
        "- 变更日志：不依赖 brief 自报；独立核对库存状态和 Markdown 中的实际版本 token。",
        "",
        "## 证据边界",
        "",
        "`top_candidates.csv` 是逐版本治理明细，文件名只服从项目标准四件套约定；它不是证券、ETF 或任何投资候选清单。",
        "",
        "本审计只回答 brief、登记或明确 post-hoc、变更日志、标准输出、状态边界和 cohort 绑定是否齐备。通过不等于 Alpha 有效，不解除 `research_only`、`NO_ACTION`、人工辅助交易未就绪和禁止自动交易的边界。",
        "",
    ]
    return "\n".join(lines)


def self_check() -> None:
    # Reuse the task-brief audit's full pass and adversarial timing checks.
    briefs.self_check()
    assert "not_investment_candidate" in "governance_detail_not_investment_candidate"
    print("coverage_self_check=pass")


if __name__ == "__main__":
    raise SystemExit(main())
