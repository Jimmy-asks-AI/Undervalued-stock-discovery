#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V471 = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit"
OUT = ROOT / "outputs" / "audit" / "v4_71_live_guardrail_playbook"
DEBUG = OUT / "debug"

FIELDS = [
    "guardrail_type",
    "item",
    "status",
    "current_evidence",
    "live_rule",
    "forbidden_override",
    "manual_review_action",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build V4.71 live guardrail playbook from failed robustness checks.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    rows = build_rows()
    write_outputs(rows)
    print(f"output_dir={OUT}")
    print(f"rows={len(rows)}")


def build_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    policy = read_json(V471 / "debug" / "frozen_policy.json")
    rows.extend(parameter_rows(read_rows(V471 / "debug" / "parameter_failure_diagnosis.csv"), policy))
    rows.extend(cooldown_rows(read_rows(V471 / "debug" / "cooldown_sensitivity.csv"), policy))
    rows.extend(year_state_rows(read_rows(V471 / "debug" / "year_state_breakdown.csv"), policy))
    return rows


def parameter_rows(items: list[dict[str, str]], policy: dict[str, Any]) -> list[dict[str, str]]:
    forbidden = policy.get("live_parameter_guardrails", {}).get("forbidden_runtime_overrides", [])
    out = []
    for item in items:
        variant = item.get("variant_id", "")
        effective = item.get("effective") == "True"
        if variant == "base_v4_70":
            status = "frozen_rule"
            rule = "仅使用冻结规则做盘前复核。"
            override = ""
        elif effective:
            status = "audit_support_only"
            rule = "只能说明邻域不全坏，不能临场替换冻结参数。"
            override = "promote_parameter_variant_without_full_gate"
        else:
            status = "forbidden_runtime_override"
            rule = "不得在盘前把失败扰动升级为实盘参数。"
            override = ";".join(forbidden)
        out.append(row(
            "parameter_perturbation",
            variant,
            status,
            f"score={item.get('score', '')}; effective={item.get('effective', '')}; failed={item.get('failed_score_metrics', '')}; action={item.get('action', '')}",
            rule,
            override,
            item.get("action", ""),
        ))
    return out


def cooldown_rows(items: list[dict[str, str]], policy: dict[str, Any]) -> list[dict[str, str]]:
    guard = policy.get("live_cooldown_guardrails", {})
    minimum = int_value(guard.get("minimum_independent_cluster_count"))
    forbidden = ";".join(guard.get("forbidden_runtime_overrides", []))
    out = []
    for item in items:
        clusters = int_value(item.get("clusters"))
        days = item.get("cooldown_days", "")
        passes = clusters >= minimum
        status = "audit_support_only" if passes else "insufficient_independent_clusters"
        out.append(row(
            "cooldown_sensitivity",
            f"{days}d",
            status,
            f"clusters={clusters}; min={minimum}; mean={item.get('cluster_net_mean_return', '')}; worst={item.get('worst_cluster_net_return', '')}; concentration={item.get('max_cluster_concentration', '')}",
            "冷却期只能用于独立性审计；不得降低冷却期来制造样本数。",
            forbidden if not passes else "lower_cooldown_gap_to_create_sample_size",
            "继续前推真实新样本；不要把同一反弹簇重复计数。",
        ))
    return out


def year_state_rows(items: list[dict[str, str]], policy: dict[str, Any]) -> list[dict[str, str]]:
    guard = policy.get("live_year_state_guardrails", {})
    minimum = int_value(guard.get("minimum_events_per_year_state_cell"))
    forbidden = ";".join(guard.get("forbidden_runtime_overrides", []))
    out = []
    for item in items:
        events = int_value(item.get("events"))
        if events >= minimum:
            continue
        label = f"{item.get('year', '')}/{item.get('dimension', '')}/{item.get('bucket', '')}"
        out.append(row(
            "year_state_breakdown",
            label,
            "sparse_state_not_production_evidence",
            f"events={events}; min={minimum}; mean={item.get('net_mean_return', '')}; bad_window_rate={item.get('bad_window_rate', '')}; worst={item.get('worst_return', '')}",
            "样本少于最低格数时只能做风险标签，不能做生产证据。",
            forbidden,
            "等待前推样本补足；不要用全样本均值掩盖稀疏状态。",
        ))
    return out


def row(kind: str, item: str, status: str, evidence: str, rule: str, forbidden: str, action: str) -> dict[str, str]:
    return {
        "guardrail_type": kind,
        "item": item,
        "status": status,
        "current_evidence": evidence,
        "live_rule": rule,
        "forbidden_override": forbidden,
        "manual_review_action": action,
    }


def write_outputs(rows: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "live_guardrail_playbook.csv", rows)
    summary = {
        "version": "v4_71_live_guardrail_playbook_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "row_count": len(rows),
        "forbidden_runtime_override_count": count_status(rows, "forbidden_runtime_override"),
        "audit_support_only_count": count_status(rows, "audit_support_only"),
        "insufficient_independent_clusters_count": count_status(rows, "insufficient_independent_clusters"),
        "sparse_state_not_production_evidence_count": count_status(rows, "sparse_state_not_production_evidence"),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "V4.71 稳健性失败项已转成盘前禁用/观察护栏；不能升级为生产规则。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# V4.71 盘前稳健性护栏清单",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 护栏行数：{summary['row_count']}",
        f"- 禁止临场参数覆盖：{summary['forbidden_runtime_override_count']}",
        f"- 只作审计支持：{summary['audit_support_only_count']}",
        f"- 冷却期独立样本不足：{summary['insufficient_independent_clusters_count']}",
        f"- 分年/状态稀疏：{summary['sparse_state_not_production_evidence_count']}",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "| type | item | status | live_rule | manual_review_action |",
        "|:---|:---|:---|:---|:---|",
    ]
    for item in rows:
        lines.append(f"| {item['guardrail_type']} | {item['item']} | {item['status']} | {item['live_rule']} | {item['manual_review_action']} |")
    return "\n".join(lines)


def count_status(rows: list[dict[str, str]], status: str) -> int:
    return sum(item["status"] == status for item in rows)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def int_value(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def self_check() -> None:
    policy = {
        "live_parameter_guardrails": {"forbidden_runtime_overrides": ["no"]},
        "live_cooldown_guardrails": {"minimum_independent_cluster_count": 20, "forbidden_runtime_overrides": ["x"]},
        "live_year_state_guardrails": {"minimum_events_per_year_state_cell": 3, "forbidden_runtime_overrides": ["y"]},
    }
    params = parameter_rows([
        {"variant_id": "base_v4_70", "effective": "True"},
        {"variant_id": "bad", "effective": "False", "score": "70"},
    ], policy)
    assert params[0]["status"] == "frozen_rule"
    assert params[1]["status"] == "forbidden_runtime_override"
    cooldown = cooldown_rows([{"cooldown_days": "60", "clusters": "12"}], policy)
    assert cooldown[0]["status"] == "insufficient_independent_clusters"
    sparse = year_state_rows([{"year": "2020", "dimension": "x", "bucket": "y", "events": "2"}], policy)
    assert sparse[0]["status"] == "sparse_state_not_production_evidence"
    assert int_value("") == 0
    print("self_check=pass")


if __name__ == "__main__":
    main()
