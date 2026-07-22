#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "v4_72_pre_entry_operator_checklist"
DEBUG = OUT / "debug"

ENTRY = ROOT / "outputs" / "audit" / "v4_72_entry_readiness"
GUARDRAILS = ROOT / "outputs" / "audit" / "v4_71_live_guardrail_playbook"
QUARANTINE = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_quarantine_overlay"
SETTLEMENT = ROOT / "outputs" / "audit" / "v4_72_forward_return_settlement"
PRETRADE = ROOT / "outputs" / "audit" / "v4_72_pre_trade_review_packet"
STATE_GUARDRAIL = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_state_guardrail"
V471 = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit"

FIELDS = [
    "priority",
    "stage",
    "check_item",
    "status",
    "required_action",
    "evidence",
    "evidence_path",
    "auto_execution_allowed",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a single pre-entry operator checklist.")
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
    entry_summary = read_json(ENTRY / "run_summary.json")
    guard_summary = read_json(GUARDRAILS / "run_summary.json")
    quarantine = read_json(QUARANTINE / "run_summary.json")
    pretrade = read_json(PRETRADE / "run_summary.json")
    entry_rows = read_rows(ENTRY / "debug" / "pre_entry_action_checklist.csv")
    schedule_rows = read_rows(SETTLEMENT / "debug" / "settlement_schedule.csv")
    state_rows = read_rows(STATE_GUARDRAIL / "debug" / "state_guardrail.csv")
    source_rows = read_rows(V471 / "debug" / "source_panel.csv")
    latest_rows = read_rows(V471 / "debug" / "latest_signal_status.csv")
    rows: list[dict[str, str]] = []

    refresh = next((item for item in schedule_rows if item.get("event_type") == "pre_entry_refresh"), {})
    rows.append(row(
        "P0",
        "入场日前",
        "重跑 live refresh",
        refresh.get("status", "pending"),
        refresh.get("command", "python .\\scripts\\run_v4_71_live_refresh.py --trade-date <planned_entry_date>"),
        refresh.get("action", "入场日前重跑，仍不自动入场。"),
        "outputs/audit/v4_72_forward_return_settlement/debug/settlement_schedule.csv",
    ))
    rows.append(row(
        "P0",
        "全局硬停止",
        "自动执行关闭",
        "pass" if not truthy(entry_summary.get("auto_execution_allowed")) and not truthy(pretrade.get("auto_execution_allowed")) else "fail",
        "保持 auto_execution_allowed=false；系统仓位上限=0。",
        f"entry={entry_summary.get('auto_execution_allowed')}; pretrade={pretrade.get('auto_execution_allowed')}; cap_all_zero={pretrade.get('system_position_cap_all_zero')}",
        "outputs/audit/v4_72_entry_readiness/run_summary.json; outputs/audit/v4_72_pre_trade_review_packet/run_summary.json",
    ))
    rows.append(row(
        "P0",
        "稳健性护栏",
        "禁止临场改失败参数",
        "blocked",
        "不得提前入场、放宽/取消止损、上调高波动阈值或重复计数反弹簇。",
        f"forbidden={guard_summary.get('forbidden_runtime_override_count', '')}; sparse={guard_summary.get('sparse_state_not_production_evidence_count', '')}; cooldown_insufficient={guard_summary.get('insufficient_independent_clusters_count', '')}",
        "outputs/audit/v4_71_live_guardrail_playbook/run_summary.json",
    ))
    rows.extend(robustness_hard_stop_rows(guard_summary))
    rows.append(current_state_guardrail_row(state_rows, source_rows, latest_rows))
    rows.append(row(
        "P1",
        "强行业替补观察",
        "历史失败隔离后替补池",
        "observe_only",
        "只用于人工复核，不作为入场许可。",
        f"count={quarantine.get('replacement_observation_count', '')}; top={quarantine.get('top_replacement_industries', '')}; asof_pass={quarantine.get('asof_failure_filter_passes_gate', '')}",
        "outputs/audit/v4_72_rebound_leader_quarantine_overlay/run_summary.json",
    ))

    for item in entry_rows:
        rows.append(row(
            priority_for_action(item.get("action_bucket", "")),
            "行业入场动作",
            item.get("industry_name", ""),
            item.get("action_bucket", ""),
            item.get("entry_day_action", ""),
            f"allowed={item.get('entry_allowed', '')}; carrier={item.get('candidate_carrier', '')}; fund_flow={item.get('fund_flow_gate_status', '')}; tracking={item.get('tracking_gate_status', '')}; history={item.get('history_gate_status', '')}; research={item.get('research_gate_status', '')}; clearance={item.get('evidence_clearance_step', '')}",
            "outputs/audit/v4_72_entry_readiness/debug/pre_entry_action_checklist.csv",
        ))

    settlement = next((item for item in schedule_rows if item.get("event_type") == "forward_settlement"), {})
    rows.append(row(
        "P2",
        "退出后",
        "结算真实 forward return",
        settlement.get("status", "pending"),
        settlement.get("command", "python .\\scripts\\settle_v4_72_rebound_leader_forward_returns.py --as-of-date <exit_date>"),
        settlement.get("action", "退出日后结算真实 forward return。"),
        "outputs/audit/v4_72_forward_return_settlement/debug/settlement_schedule.csv",
    ))
    return rows


def row(priority: str, stage: str, item: str, status: str, action: str, evidence: str, path: str) -> dict[str, str]:
    return {
        "priority": priority,
        "stage": stage,
        "check_item": item,
        "status": status,
        "required_action": action,
        "evidence": evidence,
        "evidence_path": path,
        "auto_execution_allowed": "否",
    }


def robustness_hard_stop_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    specs = [
        (
            "参数扰动稳健性",
            int_value(summary.get("forbidden_runtime_override_count")),
            "禁止提前入场、放宽/取消止损、上调高波动阈值或临场替换失败参数。",
            "forbidden_runtime_override_count",
        ),
        (
            "冷却期独立样本",
            int_value(summary.get("insufficient_independent_clusters_count")),
            "冷却期独立簇不足时，不得降低冷却期门槛或重复计算同一反弹簇。",
            "insufficient_independent_clusters_count",
        ),
        (
            "分年/分状态稀疏",
            int_value(summary.get("sparse_state_not_production_evidence_count")),
            "分年/分状态样本不足时，只能作为风险标签，不能用全样本均值提高入场信心。",
            "sparse_state_not_production_evidence_count",
        ),
    ]
    return [
        row(
            "P0" if count else "P1",
            "稳健性硬护栏",
            name,
            "blocked" if count else "pass",
            action,
            f"{field}={count}",
            "outputs/audit/v4_71_live_guardrail_playbook/run_summary.json",
        )
        for name, count, action, field in specs
    ]


def priority_for_action(action_bucket: str) -> str:
    if action_bucket == "跳过":
        return "P0"
    if action_bucket == "仅人工复核":
        return "P1"
    return "P2"


def write_outputs(rows: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "operator_checklist.csv", rows)
    p0_pending = count_priority_status(rows, "P0", {"pending"})
    p0_hard_stop = count_priority_status(rows, "P0", {"blocked", "fail"})
    p0_skip = count_priority_status(rows, "P0", {"跳过"})
    state_blocked = count_check_status(rows, "强行业状态护栏", {"blocked", "fail"})
    summary = {
        "version": "v4_72_pre_entry_operator_checklist_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "row_count": len(rows),
        "p0_count": sum(item["priority"] == "P0" for item in rows),
        "p1_count": sum(item["priority"] == "P1" for item in rows),
        "p2_count": sum(item["priority"] == "P2" for item in rows),
        "p0_pending_count": p0_pending,
        "p0_hard_stop_count": p0_hard_stop,
        "p0_skip_count": p0_skip,
        "state_guardrail_blocked_count": state_blocked,
        "skip_count": sum(item["status"] == "跳过" for item in rows),
        "manual_review_only_count": sum(item["status"] == "仅人工复核" for item in rows),
        "observe_only_count": sum(item["status"] == "只观察" for item in rows),
        "entry_permitted": False,
        "pre_entry_operator_ready": p0_pending == 0 and p0_hard_stop == 0 and p0_skip == 0,
        "auto_execution_allowed": False,
        "production_ready": False,
        "final_verdict": "盘前操作总清单只合并已有审计动作；未解除研究门禁前不允许入场。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# V4.72 盘前操作总清单",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 清单行数：{summary['row_count']}",
        f"- P0：{summary['p0_count']}",
        f"- P1：{summary['p1_count']}",
        f"- P2：{summary['p2_count']}",
        f"- P0待处理：{summary['p0_pending_count']}",
        f"- P0硬停止：{summary['p0_hard_stop_count']}",
        f"- P0跳过行业：{summary['p0_skip_count']}",
        f"- 状态护栏阻断：{summary['state_guardrail_blocked_count']}",
        f"- 允许入场：`{str(summary['entry_permitted']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "| priority | stage | check_item | status | required_action |",
        "|:---|:---|:---|:---|:---|",
    ]
    for item in rows:
        lines.append(f"| {item['priority']} | {item['stage']} | {item['check_item']} | {item['status']} | {item['required_action']} |")
    return "\n".join(lines)


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


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"true", "1", "yes", "是"}


def count_priority_status(rows: list[dict[str, str]], priority: str, statuses: set[str]) -> int:
    return sum(item["priority"] == priority and item["status"] in statuses for item in rows)


def count_check_status(rows: list[dict[str, str]], check_item: str, statuses: set[str]) -> int:
    return sum(item["check_item"] == check_item and item["status"] in statuses for item in rows)


def current_state_guardrail_row(state_rows: list[dict[str, str]], source_rows: list[dict[str, str]], latest_rows: list[dict[str, str]]) -> dict[str, str]:
    latest = latest_rows[0] if latest_rows else {}
    latest_date = latest.get("latest_panel_date", "")
    source = next((item for item in source_rows if item.get("trade_date") == latest_date or item.get("trade_date_text") == latest_date), source_rows[-1] if source_rows else {})
    buckets = {
        "volatility_guard": volatility_guard(source),
        "stress_level": stress_level(source),
        "negative_breadth": negative_breadth(source),
    }
    statuses = []
    for dimension, bucket in buckets.items():
        match = next((item for item in state_rows if item.get("dimension") == dimension and item.get("bucket") == bucket), {})
        statuses.append(f"{dimension}:{bucket}={match.get('status', 'missing')}")
    blocked = any(part.endswith("=fail") or part.endswith("=missing") for part in statuses)
    triggered = truthy(latest.get("v4_70_triggered_on_latest_date"))
    return row(
        "P0" if triggered and blocked else "P1",
        "强行业选择",
        "强行业状态护栏",
        "blocked" if triggered and blocked else "observe_only",
        "失败状态桶内强行业排序只观察，不得提高入场信心。" if triggered and blocked else "未触发或状态桶未阻断；仍不自动入场。",
        f"triggered={triggered}; latest={latest_date}; {'; '.join(statuses)}",
        "outputs/audit/v4_72_rebound_leader_state_guardrail/debug/state_guardrail.csv; outputs/industry_rebound_window_v4_71_robustness_live_audit/debug/source_panel.csv",
    )


def volatility_guard(row: dict[str, str]) -> str:
    return "高波动保护区" if float_value(row.get("market_volatility_20d_vs_60d")) >= 1.30 else "非高波动区"


def stress_level(row: dict[str, str]) -> str:
    score = float_value(row.get("market_stress_score"))
    if score <= 0.55:
        return "低/中压力"
    if score <= 0.70:
        return "中高压力"
    return "高压力"


def negative_breadth(row: dict[str, str]) -> str:
    return "深负广度" if float_value(row.get("negative_breadth_60d")) >= 0.75 else "普通负广度"


def float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def int_value(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def self_check() -> None:
    assert priority_for_action("跳过") == "P0"
    assert priority_for_action("仅人工复核") == "P1"
    assert priority_for_action("只观察") == "P2"
    assert truthy(True)
    assert truthy("true")
    assert not truthy("否")
    rows = [
        row("P0", "x", "a", "pending", "do", "e", "p"),
        row("P0", "x", "b", "blocked", "do", "e", "p"),
        row("P0", "x", "c", "跳过", "do", "e", "p"),
        row("P1", "x", "d", "pending", "do", "e", "p"),
    ]
    robust = robustness_hard_stop_rows({
        "forbidden_runtime_override_count": 1,
        "insufficient_independent_clusters_count": 2,
        "sparse_state_not_production_evidence_count": 3,
    })
    assert len(robust) == 3
    assert all(item["priority"] == "P0" and item["status"] == "blocked" for item in robust)
    assert count_priority_status(rows, "P0", {"pending"}) == 1
    assert count_priority_status(rows, "P0", {"blocked", "fail"}) == 1
    assert count_priority_status(rows, "P0", {"跳过"}) == 1
    state_row = current_state_guardrail_row(
        [{"dimension": "negative_breadth", "bucket": "普通负广度", "status": "fail"}],
        [{"trade_date": "2026-06-18", "market_volatility_20d_vs_60d": "1.0", "market_stress_score": "0.6", "negative_breadth_60d": "0.5"}],
        [{"latest_panel_date": "2026-06-18", "v4_70_triggered_on_latest_date": "True"}],
    )
    assert state_row["status"] == "blocked"
    assert count_check_status([state_row], "强行业状态护栏", {"blocked"}) == 1
    print("self_check=pass")


if __name__ == "__main__":
    main()
