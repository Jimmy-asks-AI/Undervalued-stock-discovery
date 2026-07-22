#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRE_TRADE = ROOT / "outputs" / "audit" / "v4_72_pre_trade_review_packet" / "top_candidates.csv"
OUT = ROOT / "outputs" / "audit" / "v4_72_entry_readiness"
DEBUG = OUT / "debug"

FIELDS = [
    "as_of_date",
    "planned_entry_date",
    "days_until_entry",
    "industry_code",
    "industry_name",
    "practical_review_bucket",
    "candidate_carrier_code",
    "candidate_carrier_name",
    "best_alternative_carrier_code",
    "best_alternative_carrier_name",
    "best_alternative_tracking_status",
    "best_alternative_action_note",
    "tradeable_filter_status",
    "structural_blockers",
    "manual_gate_status",
    "date_gate_status",
    "carrier_gate_status",
    "fund_flow_gate_status",
    "tracking_gate_status",
    "tracking_failure_reason",
    "carrier_tracking_evidence_note",
    "history_gate_status",
    "research_gate_status",
    "safety_cap_status",
    "system_position_cap_pct",
    "entry_readiness_status",
    "projected_entry_status",
    "projected_entry_action",
    "entry_day_decision_rule",
    "next_clearance_step",
    "evidence_clearance_step",
    "would_entry_be_allowed",
    "entry_action",
    "blocking_reasons",
]

ACTION_FIELDS = [
    "planned_entry_date",
    "days_until_entry",
    "industry_code",
    "industry_name",
    "action_bucket",
    "entry_allowed",
    "projected_entry_status",
    "entry_day_decision_rule",
    "candidate_carrier",
    "fund_flow_gate_status",
    "tracking_gate_status",
    "history_gate_status",
    "research_gate_status",
    "entry_day_action",
    "evidence_clearance_step",
    "auto_execution_allowed",
    "system_position_cap_pct",
]

CORE_TRADEABLE_GATE_FIELDS = [
    "carrier_gate_status",
    "fund_flow_gate_status",
    "tracking_gate_status",
    "history_gate_status",
    "safety_cap_status",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit V4.72 entry readiness without creating trade instructions.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if error := as_of_date_error(args.as_of_date, date.today()):
        parser.error(error)
    rows = build_entry_readiness(read_rows(PRE_TRADE), date.fromisoformat(args.as_of_date))
    write_outputs(rows, args.as_of_date)
    print(f"output_dir={OUT}")
    print(f"rows={len(rows)}")
    print("auto_execution_allowed=False")


def as_of_date_error(value: str, today: date) -> str:
    # ponytail: future guard only; historical as-of replay remains allowed.
    if date.fromisoformat(value) > today:
        return f"--as-of-date {value} is in the future; run entry readiness on or after that date."
    return ""


def build_entry_readiness(rows: list[dict[str, str]], as_of: date) -> list[dict[str, str]]:
    out = []
    for row in rows:
        planned = date.fromisoformat(row.get("planned_entry_date", "9999-12-31"))
        blockers = blockers_for(row)
        not_due = as_of < planned
        gates = gate_statuses(row, not_due)
        status = "entry_not_due" if not_due else ("entry_blocked" if blockers else "entry_review_required")
        decision_rule = entry_day_decision_rule(row)
        projected_status = projected_entry_status(decision_rule)
        out.append({
            "as_of_date": as_of.isoformat(),
            "planned_entry_date": planned.isoformat(),
            "days_until_entry": str((planned - as_of).days),
            "industry_code": row.get("industry_code", ""),
            "industry_name": row.get("industry_name", ""),
            "practical_review_bucket": row.get("practical_review_bucket", ""),
            "candidate_carrier_code": row.get("candidate_carrier_code", ""),
            "candidate_carrier_name": row.get("candidate_carrier_name", ""),
            "best_alternative_carrier_code": row.get("best_alternative_carrier_code", ""),
            "best_alternative_carrier_name": row.get("best_alternative_carrier_name", ""),
            "best_alternative_tracking_status": row.get("best_alternative_tracking_status", ""),
            "best_alternative_action_note": row.get("best_alternative_action_note", ""),
            "tradeable_filter_status": row.get("tradeable_filter_status", ""),
            "structural_blockers": row.get("structural_blockers", ""),
            "manual_gate_status": row.get("manual_gate_status", ""),
            **gates,
            "tracking_failure_reason": tracking_failure_reason(row, gates),
            "carrier_tracking_evidence_note": row.get("carrier_tracking_evidence_note", ""),
            "system_position_cap_pct": row.get("system_position_cap_pct", "0"),
            "entry_readiness_status": status,
            "projected_entry_status": projected_status,
            "projected_entry_action": projected_entry_action(projected_status),
            "entry_day_decision_rule": decision_rule,
            "next_clearance_step": next_clearance_step(gates),
            "evidence_clearance_step": next_clearance_step(gates, include_date=False),
            "would_entry_be_allowed": "否",
            "entry_action": "未到计划入场日；只更新观察。" if not_due else ("跳过；入场门禁未通过。" if blockers else "仅人工复核；系统不自动执行。"),
            "blocking_reasons": "；".join(blockers) or "系统仍需人工复核，且 auto_execution_allowed=false",
        })
    return sorted(out, key=lambda item: (int(item["days_until_entry"]), item["industry_code"]))


def blockers_for(row: dict[str, str]) -> list[str]:
    blockers = []
    if row.get("system_position_cap_pct", "0") != "0":
        blockers.append("system_position_cap_pct 非零需独立风控复核")
    else:
        blockers.append("system_position_cap_pct=0")
    gate = row.get("manual_gate_status", "")
    if gate != "research_observation_only":
        blockers.append(f"manual_gate_status={gate}")
    else:
        blockers.append("强行业选择未验证")
    if row.get("manual_override_required") == "是":
        blockers.append("manual_override_required=是")
    if row.get("tradeable_filter_status"):
        blockers.append(f"tradeable_filter_status={row.get('tradeable_filter_status')}")
    if row.get("structural_blockers"):
        blockers.append(f"structural_blockers={row.get('structural_blockers')}")
    alternative = f"{row.get('best_alternative_carrier_code', '')} {row.get('best_alternative_carrier_name', '')}".strip()
    if alternative:
        blockers.append(f"best_alternative={alternative}/{row.get('best_alternative_tracking_status', '')}；{row.get('best_alternative_action_note', '')}")
    return blockers


def gate_statuses(row: dict[str, str], not_due: bool) -> dict[str, str]:
    structural = row.get("structural_blockers", "")
    tracking = row.get("best_alternative_tracking_status") or row.get("tracking_audit_status", "")
    historical = row.get("historical_failure_flag") == "True" or "repeated_worst_event_industry" in structural
    return {
        "date_gate_status": "pending_until_entry_date" if not_due else "pass",
        "carrier_gate_status": "fail_no_tradeable_carrier" if "no_tradeable_carrier" in structural or not row.get("candidate_carrier_code") else "pass",
        "fund_flow_gate_status": fund_flow_gate_status(row.get("fund_flow_overlay_status", "")),
        "tracking_gate_status": "pass" if tracking == "tracking_observed_review_required" and "weak_carrier_tracking" not in structural and "tracking_not_ready" not in structural else "fail_tracking_not_ready",
        "history_gate_status": "fail_historical_or_repeated_worst_event" if historical else "pass",
        "research_gate_status": "pass" if row.get("research_gate_status") == "validated" else "fail_research_only",
        "safety_cap_status": "pass_zero_cap" if row.get("system_position_cap_pct", "0") == "0" else "fail_nonzero_cap",
    }


def fund_flow_gate_status(status: str) -> str:
    if status == "available_current_only":
        return "pass"
    if status == "proxy_current_only":
        return "fail_proxy_only"
    return "fail_missing"


def tracking_failure_reason(row: dict[str, str], gates: dict[str, str]) -> str:
    if gates.get("tracking_gate_status", "").startswith("pass"):
        return "pass"
    structural = row.get("structural_blockers", "")
    reasons = []
    if "low_turnover_carrier" in structural:
        reasons.append("low_liquidity")
    if "tracking_not_ready" in structural:
        reasons.append("tracking_not_audited")
    corr = safe_float(row.get("carrier_daily_return_corr", ""))
    gap = safe_float(row.get("carrier_return_gap", ""))
    if corr is not None and corr < 0.70:
        reasons.append("low_daily_return_corr")
    if gap is not None and abs(gap) > 0.20:
        reasons.append("large_cumulative_return_gap")
    if "weak_carrier_tracking" in structural and not reasons:
        reasons.append("weak_carrier_tracking")
    return "|".join(reasons) or "tracking_not_ready"


def safe_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def next_clearance_step(gates: dict[str, str], include_date: bool = True) -> str:
    order = [
        ("date_gate_status", "等到计划入场日并重跑 live refresh。"),
        ("carrier_gate_status", "补可复核交易载体或确认无载体则跳过。"),
        ("fund_flow_gate_status", "补精确资金流映射；代理资金流不能解除门禁。"),
        ("tracking_gate_status", "补载体跟踪证据，弱跟踪不进场。"),
        ("history_gate_status", "历史失败或重复最差事件未解除，只能观察。"),
        ("research_gate_status", "强反弹行业选择未验证，保持研究观察。"),
        ("safety_cap_status", "系统仓位上限异常，先恢复为 0。"),
    ]
    for key, action in order:
        if key == "date_gate_status" and not include_date:
            continue
        if not str(gates.get(key, "")).startswith("pass"):
            return action
    return "仅允许人工复核；系统不自动执行。"


def entry_day_decision_rule(row: dict[str, str]) -> str:
    status = row.get("tradeable_filter_status", "")
    if status == "structural_blocked":
        return "skip_unless_structural_blockers_cleared"
    if status == "structural_observe_only":
        return "observe_only_no_entry"
    if status == "structural_reviewable_research_gate_blocked":
        return "manual_review_only_until_strong_rebound_validated"
    return "manual_review_required_no_auto_execution"


def projected_entry_status(rule: str) -> str:
    if rule == "manual_review_only_until_strong_rebound_validated":
        return "projected_entry_manual_review_only"
    if rule == "manual_review_required_no_auto_execution":
        return "projected_entry_review_required"
    return "projected_entry_blocked"


def projected_entry_action(status: str) -> str:
    if status == "projected_entry_manual_review_only":
        return "按当前证据，到计划入场日也只能人工复核；强反弹行业选择未验证前不入场。"
    if status == "projected_entry_review_required":
        return "按当前证据，到计划入场日也只能人工复核。"
    return "按当前证据，到计划入场日仍应跳过。"


def write_outputs(rows: list[dict[str, str]], as_of_date: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "entry_readiness.csv", rows)
    action_rows = build_pre_entry_action_checklist(rows)
    write_action_rows(DEBUG / "pre_entry_action_checklist.csv", action_rows)
    allowed_entry_count = 0
    projected_blocked_count = sum(row["projected_entry_status"] == "projected_entry_blocked" for row in rows)
    tracking_reasons = tracking_reason_counts(rows)
    live_decision = "no_entry_currently" if allowed_entry_count == 0 else "manual_review_only"
    summary = {
        "version": "v4_72_entry_readiness_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of_date,
        "row_count": len(rows),
        "entry_not_due_count": sum(row["entry_readiness_status"] == "entry_not_due" for row in rows),
        "entry_blocked_count": sum(row["entry_readiness_status"] == "entry_blocked" for row in rows),
        "entry_review_required_count": sum(row["entry_readiness_status"] == "entry_review_required" for row in rows),
        "projected_entry_blocked_count": projected_blocked_count,
        "projected_entry_manual_review_only_count": sum(row["projected_entry_status"] == "projected_entry_manual_review_only" for row in rows),
        "projected_entry_review_required_count": sum(row["projected_entry_status"] == "projected_entry_review_required" for row in rows),
        "pre_entry_action_checklist_rows": len(action_rows),
        "pre_entry_action_skip_count": sum(row["action_bucket"] == "跳过" for row in action_rows),
        "pre_entry_action_observe_count": sum(row["action_bucket"] == "只观察" for row in action_rows),
        "pre_entry_action_manual_review_only_count": sum(row["action_bucket"] == "仅人工复核" for row in action_rows),
        "entry_skip_rule_count": sum(row["entry_day_decision_rule"] == "skip_unless_structural_blockers_cleared" for row in rows),
        "entry_observe_only_rule_count": sum(row["entry_day_decision_rule"] == "observe_only_no_entry" for row in rows),
        "entry_manual_review_rule_count": sum(row["entry_day_decision_rule"] == "manual_review_only_until_strong_rebound_validated" for row in rows),
        "entry_skip_rule_industries": names_by_rule(rows, "skip_unless_structural_blockers_cleared"),
        "entry_observe_only_rule_industries": names_by_rule(rows, "observe_only_no_entry"),
        "entry_manual_review_rule_industries": names_by_rule(rows, "manual_review_only_until_strong_rebound_validated"),
        "entry_skip_clearance_steps": clearance_steps_by_rule(rows, "skip_unless_structural_blockers_cleared"),
        "entry_observe_only_clearance_steps": clearance_steps_by_rule(rows, "observe_only_no_entry"),
        "entry_manual_review_clearance_steps": clearance_steps_by_rule(rows, "manual_review_only_until_strong_rebound_validated"),
        "core_manual_review_bucket_count": sum(row["practical_review_bucket"] == "人工优先复核_研究门禁未过" for row in rows),
        "core_tradeable_gate_pass_count": core_tradeable_gate_pass_count(rows),
        "core_research_gate_fail_count": sum(row["practical_review_bucket"] == "人工优先复核_研究门禁未过" and not row["research_gate_status"].startswith("pass") for row in rows),
        "tradeable_research_blocked_count": len(tradeable_research_blocked_rows(rows)),
        "tradeable_research_blocked_industries": names_by_tradeable_research_blocked(rows),
        "tradeable_research_blocked_boundary": "交易侧门禁通过，但强反弹行业选择未验证；只能人工复核，不能自动入场。",
        "skip_if_unresolved_bucket_count": sum(row["practical_review_bucket"] == "补证失败则跳过" for row in rows),
        "observe_only_bucket_count": sum(row["practical_review_bucket"] == "只观察" for row in rows),
        "date_gate_pending_count": sum(row["date_gate_status"] == "pending_until_entry_date" for row in rows),
        "carrier_gate_fail_count": sum(not row["carrier_gate_status"].startswith("pass") for row in rows),
        "fund_flow_gate_fail_count": sum(not row["fund_flow_gate_status"].startswith("pass") for row in rows),
        "tracking_gate_fail_count": sum(not row["tracking_gate_status"].startswith("pass") for row in rows),
        "tracking_failure_reason_counts": tracking_reasons,
        "top_tracking_failure_reason": next(iter(tracking_reasons), ""),
        "alternative_tracking_pass_count": alternative_tracking_pass_count(rows),
        "alternative_tracking_fail_count": alternative_tracking_fail_count(rows),
        "alternative_tracking_missing_count": alternative_tracking_missing_count(rows),
        "alternative_tracking_unresolved_industries": alternative_tracking_unresolved_industries(rows),
        "alternative_tracking_resolved_industries": alternative_tracking_resolved_industries(rows),
        "history_gate_fail_count": sum(not row["history_gate_status"].startswith("pass") for row in rows),
        "research_gate_fail_count": sum(not row["research_gate_status"].startswith("pass") for row in rows),
        "allowed_entry_count": allowed_entry_count,
        "live_entry_decision": live_decision,
        "live_entry_action": "当前无可入场候选；计划入场日前继续刷新，门禁不变则跳过。" if live_decision == "no_entry_currently" else "仅允许人工复核；系统不自动执行。",
        "auto_execution_allowed": False,
        "production_ready": False,
        "final_verdict": "V4.72 只做入场就绪审计；当前不生成入场指令，系统仓位上限保持 0。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, object], rows: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.72 入场就绪审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 截止日期：{summary['as_of_date']}",
        f"- 行数：{summary['row_count']}",
        f"- 未到入场日：{summary['entry_not_due_count']}",
        f"- 入场阻断：{summary['entry_blocked_count']}",
        f"- 人工复核：{summary['entry_review_required_count']}",
        f"- 到入场日仍预计阻断：{summary['projected_entry_blocked_count']}",
        f"- 到入场日预计只能人工复核：{summary['projected_entry_manual_review_only_count']}",
        f"- 到入场日预计人工复核：{summary['projected_entry_review_required_count']}",
        f"- 入场日跳过规则：{summary['entry_skip_rule_count']}",
        f"- 入场日只观察规则：{summary['entry_observe_only_rule_count']}",
        f"- 入场日人工复核规则：{summary['entry_manual_review_rule_count']}",
        f"- 入场日跳过名单：{summary['entry_skip_rule_industries']}",
        f"- 入场日只观察名单：{summary['entry_observe_only_rule_industries']}",
        f"- 入场日人工复核名单：{summary['entry_manual_review_rule_industries']}",
        f"- 跳过清理动作：{summary['entry_skip_clearance_steps']}",
        f"- 只观察清理动作：{summary['entry_observe_only_clearance_steps']}",
        f"- 人工复核清理动作：{summary['entry_manual_review_clearance_steps']}",
        f"- 人工优先复核池：{summary['core_manual_review_bucket_count']}",
        f"- 核心池交易侧门禁通过：{summary['core_tradeable_gate_pass_count']}",
        f"- 核心池研究门禁失败：{summary['core_research_gate_fail_count']}",
        f"- 结构通过但研究阻断池：{summary['tradeable_research_blocked_count']}；{summary['tradeable_research_blocked_industries']}",
        f"- 结构通过池边界：{summary['tradeable_research_blocked_boundary']}",
        f"- 补证失败则跳过池：{summary['skip_if_unresolved_bucket_count']}",
        f"- 只观察池：{summary['observe_only_bucket_count']}",
        f"- 日期门禁待入场日：{summary['date_gate_pending_count']}",
        f"- 载体门禁失败：{summary['carrier_gate_fail_count']}",
        f"- 资金流门禁失败：{summary['fund_flow_gate_fail_count']}",
        f"- 跟踪门禁失败：{summary['tracking_gate_fail_count']}",
        f"- 跟踪失败原因：{summary['tracking_failure_reason_counts']}",
        f"- 替代载体跟踪可解阻：{summary['alternative_tracking_pass_count']}",
        f"- 替代载体跟踪不可解阻：{summary['alternative_tracking_fail_count']}",
        f"- 替代载体缺失：{summary['alternative_tracking_missing_count']}",
        f"- 替代载体不可解阻名单：{summary['alternative_tracking_unresolved_industries']}",
        f"- 替代载体可解阻名单：{summary['alternative_tracking_resolved_industries']}",
        f"- 历史失败门禁失败：{summary['history_gate_fail_count']}",
        f"- 研究验证门禁失败：{summary['research_gate_fail_count']}",
        f"- 可入场：{summary['allowed_entry_count']}",
        f"- 当前入场总决策：`{summary['live_entry_decision']}`",
        f"- 当前动作：{summary['live_entry_action']}",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 盘前动作清单",
        "",
        action_checklist_markdown(build_pre_entry_action_checklist(rows)),
        "",
        "## 详细门禁",
        "",
        to_markdown(rows[:10]),
        "",
        "边界：该审计只回答“是否满足入场前置条件”，不是买入指令。",
    ])


def tracking_reason_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        if row.get("tracking_gate_status", "").startswith("pass"):
            continue
        for reason in row.get("tracking_failure_reason", "").split("|"):
            if reason and reason != "pass":
                counter[reason] += 1
    return dict(counter.most_common())


def alternative_tracking_pass_count(rows: list[dict[str, str]]) -> int:
    return sum(row.get("best_alternative_tracking_status") == "tracking_observed_review_required" for row in rows)


def alternative_tracking_fail_count(rows: list[dict[str, str]]) -> int:
    return sum(
        bool(row.get("best_alternative_carrier_code"))
        and row.get("best_alternative_tracking_status") != "tracking_observed_review_required"
        for row in rows
    )


def alternative_tracking_missing_count(rows: list[dict[str, str]]) -> int:
    return sum(not row.get("best_alternative_carrier_code") for row in rows)


def alternative_tracking_unresolved_industries(rows: list[dict[str, str]]) -> str:
    return ",".join(
        row.get("industry_name", "")
        for row in rows
        if bool(row.get("best_alternative_carrier_code"))
        and row.get("best_alternative_tracking_status") != "tracking_observed_review_required"
    )


def alternative_tracking_resolved_industries(rows: list[dict[str, str]]) -> str:
    return ",".join(
        row.get("industry_name", "")
        for row in rows
        if row.get("best_alternative_tracking_status") == "tracking_observed_review_required"
    )


def names_by_rule(rows: list[dict[str, str]], rule: str) -> str:
    return ",".join(row.get("industry_name", "") for row in rows if row.get("entry_day_decision_rule") == rule)


def clearance_steps_by_rule(rows: list[dict[str, str]], rule: str) -> str:
    seen = []
    for row in rows:
        if row.get("entry_day_decision_rule") != rule:
            continue
        step = row.get("evidence_clearance_step", "")
        if step and step not in seen:
            seen.append(step)
    return "；".join(seen)


def core_tradeable_gate_pass_count(rows: list[dict[str, str]]) -> int:
    return len(tradeable_research_blocked_rows(rows))


def tradeable_research_blocked_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        row for row in rows
        if row.get("practical_review_bucket") == "人工优先复核_研究门禁未过"
        and all(row.get(field, "").startswith("pass") for field in CORE_TRADEABLE_GATE_FIELDS)
        and not row.get("research_gate_status", "").startswith("pass")
    ]


def names_by_tradeable_research_blocked(rows: list[dict[str, str]]) -> str:
    return ",".join(row.get("industry_name", "") for row in tradeable_research_blocked_rows(rows))


def build_pre_entry_action_checklist(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "planned_entry_date": row.get("planned_entry_date", ""),
            "days_until_entry": row.get("days_until_entry", ""),
            "industry_code": row.get("industry_code", ""),
            "industry_name": row.get("industry_name", ""),
            "action_bucket": action_bucket(row.get("entry_day_decision_rule", "")),
            "entry_allowed": "否",
            "projected_entry_status": row.get("projected_entry_status", ""),
            "entry_day_decision_rule": row.get("entry_day_decision_rule", ""),
            "candidate_carrier": f"{row.get('candidate_carrier_code', '')} {row.get('candidate_carrier_name', '')}".strip(),
            "fund_flow_gate_status": row.get("fund_flow_gate_status", ""),
            "tracking_gate_status": row.get("tracking_gate_status", ""),
            "history_gate_status": row.get("history_gate_status", ""),
            "research_gate_status": row.get("research_gate_status", ""),
            "entry_day_action": entry_day_action(row.get("entry_day_decision_rule", "")),
            "evidence_clearance_step": row.get("evidence_clearance_step", ""),
            "auto_execution_allowed": "否",
            "system_position_cap_pct": row.get("system_position_cap_pct", "0"),
        }
        for row in rows
    ]


def action_bucket(rule: str) -> str:
    if rule == "skip_unless_structural_blockers_cleared":
        return "跳过"
    if rule == "observe_only_no_entry":
        return "只观察"
    if rule == "manual_review_only_until_strong_rebound_validated":
        return "仅人工复核"
    return "人工复核"


def entry_day_action(rule: str) -> str:
    if rule == "skip_unless_structural_blockers_cleared":
        return "入场日仍未补齐结构证据则跳过"
    if rule == "observe_only_no_entry":
        return "只观察，不入场"
    if rule == "manual_review_only_until_strong_rebound_validated":
        return "仅人工复核，不入场"
    return "人工复核，不自动执行"


def action_checklist_markdown(rows: list[dict[str, str]]) -> str:
    cols = [
        "industry_name",
        "action_bucket",
        "entry_allowed",
        "candidate_carrier",
        "fund_flow_gate_status",
        "tracking_gate_status",
        "history_gate_status",
        "research_gate_status",
        "entry_day_action",
        "evidence_clearance_step",
    ]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")).replace("\n", " ") for col in cols) + " |")
    return "\n".join(lines)


def to_markdown(rows: list[dict[str, str]]) -> str:
    cols = ["planned_entry_date", "days_until_entry", "industry_name", "practical_review_bucket", "date_gate_status", "carrier_gate_status", "fund_flow_gate_status", "tracking_gate_status", "tracking_failure_reason", "history_gate_status", "research_gate_status", "next_clearance_step", "evidence_clearance_step"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")).replace("\n", " ") for col in cols) + " |")
    return "\n".join(lines)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_action_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def self_check() -> None:
    with tempfile.TemporaryDirectory():
        assert as_of_date_error("2026-06-20", date(2026, 6, 20)) == ""
        assert "future" in as_of_date_error("2026-06-23", date(2026, 6, 20))
        rows = build_entry_readiness([{
            "planned_entry_date": "2026-06-23",
            "industry_code": "801001",
            "industry_name": "样本",
            "practical_review_bucket": "补证失败则跳过",
            "manual_gate_status": "blocked_no_tradeable_carrier",
            "system_position_cap_pct": "0",
            "manual_override_required": "是",
            "best_alternative_carrier_code": "159001",
            "best_alternative_carrier_name": "替代ETF",
            "best_alternative_tracking_status": "tracking_weak_review_required",
            "best_alternative_action_note": "跟踪偏弱，不解除盘前阻断。",
            "tradeable_filter_status": "structural_blocked",
            "structural_blockers": "no_tradeable_carrier",
            "carrier_daily_return_corr": "0.5",
            "carrier_return_gap": "0.3",
            "carrier_tracking_evidence_note": "253日重叠；日收益相关=0.50；累计收益差=30.00%",
        }], date(2026, 6, 19))
        assert rows[0]["entry_readiness_status"] == "entry_not_due"
        assert rows[0]["projected_entry_status"] == "projected_entry_blocked"
        assert rows[0]["practical_review_bucket"] == "补证失败则跳过"
        assert rows[0]["would_entry_be_allowed"] == "否"
        assert "best_alternative=159001 替代ETF" in rows[0]["blocking_reasons"]
        assert rows[0]["entry_day_decision_rule"] == "skip_unless_structural_blockers_cleared"
        assert "structural_blockers=no_tradeable_carrier" in rows[0]["blocking_reasons"]
        assert rows[0]["date_gate_status"] == "pending_until_entry_date"
        assert rows[0]["carrier_gate_status"] == "fail_no_tradeable_carrier"
        assert rows[0]["research_gate_status"] == "fail_research_only"
        assert rows[0]["tracking_failure_reason"] == "low_daily_return_corr|large_cumulative_return_gap"
        assert alternative_tracking_fail_count(rows) == 1
        assert alternative_tracking_pass_count(rows) == 0
        assert alternative_tracking_missing_count(rows) == 0
        assert alternative_tracking_unresolved_industries(rows) == "样本"
        assert alternative_tracking_resolved_industries(rows) == ""
        assert "累计收益差" in rows[0]["carrier_tracking_evidence_note"]
        assert tracking_reason_counts(rows)["low_daily_return_corr"] == 1
        assert core_tradeable_gate_pass_count(rows) == 0
        assert names_by_rule(rows, "skip_unless_structural_blockers_cleared") == "样本"
        assert clearance_steps_by_rule(rows, "skip_unless_structural_blockers_cleared") == "补可复核交易载体或确认无载体则跳过。"
        assert rows[0]["next_clearance_step"] == "等到计划入场日并重跑 live refresh。"
        assert rows[0]["evidence_clearance_step"] == "补可复核交易载体或确认无载体则跳过。"
        checklist = build_pre_entry_action_checklist(rows)
        assert checklist[0]["action_bucket"] == "跳过"
        assert checklist[0]["entry_allowed"] == "否"
        assert checklist[0]["entry_day_action"] == "入场日仍未补齐结构证据则跳过"
        assert checklist[0]["auto_execution_allowed"] == "否"
        rows = build_entry_readiness([{**rows[0], "planned_entry_date": "2026-06-19"}], date(2026, 6, 19))
        assert rows[0]["entry_readiness_status"] == "entry_blocked"
        assert rows[0]["date_gate_status"] == "pass"
        assert rows[0]["next_clearance_step"] == "补可复核交易载体或确认无载体则跳过。"
        assert "system_position_cap_pct=0" in rows[0]["blocking_reasons"]
        reviewable = build_entry_readiness([{
            "planned_entry_date": "2026-06-23",
            "industry_code": "801002",
            "industry_name": "可复核样本",
            "practical_review_bucket": "人工优先复核_研究门禁未过",
            "candidate_carrier_code": "159002",
            "fund_flow_overlay_status": "available_current_only",
            "best_alternative_tracking_status": "tracking_observed_review_required",
            "tradeable_filter_status": "structural_reviewable_research_gate_blocked",
            "manual_gate_status": "research_observation_only",
            "system_position_cap_pct": "0",
        }], date(2026, 6, 19))
        assert core_tradeable_gate_pass_count(reviewable) == 1
        assert names_by_tradeable_research_blocked(reviewable) == "可复核样本"
        assert reviewable[0]["projected_entry_status"] == "projected_entry_manual_review_only"
        assert "强反弹行业选择未验证前不入场" in reviewable[0]["projected_entry_action"]
        assert build_pre_entry_action_checklist(reviewable)[0]["action_bucket"] == "仅人工复核"
        assert build_pre_entry_action_checklist(reviewable)[0]["entry_day_action"] == "仅人工复核，不入场"
        report = render_report({"final_verdict": "test", "as_of_date": "2026-06-19", "row_count": 1, "entry_not_due_count": 1, "entry_blocked_count": 0, "entry_review_required_count": 0, "projected_entry_blocked_count": 0, "projected_entry_manual_review_only_count": 1, "projected_entry_review_required_count": 0, "entry_skip_rule_count": 0, "entry_observe_only_rule_count": 0, "entry_manual_review_rule_count": 1, "entry_skip_rule_industries": "", "entry_observe_only_rule_industries": "", "entry_manual_review_rule_industries": "可复核样本", "entry_skip_clearance_steps": "", "entry_observe_only_clearance_steps": "", "entry_manual_review_clearance_steps": "强反弹行业选择未验证，保持研究观察。", "core_manual_review_bucket_count": 1, "core_tradeable_gate_pass_count": 1, "core_research_gate_fail_count": 1, "tradeable_research_blocked_count": 1, "tradeable_research_blocked_industries": "可复核样本", "tradeable_research_blocked_boundary": "只人工复核", "skip_if_unresolved_bucket_count": 0, "observe_only_bucket_count": 0, "date_gate_pending_count": 1, "carrier_gate_fail_count": 0, "fund_flow_gate_fail_count": 0, "tracking_gate_fail_count": 0, "tracking_failure_reason_counts": {}, "alternative_tracking_pass_count": 1, "alternative_tracking_fail_count": 0, "alternative_tracking_missing_count": 0, "alternative_tracking_unresolved_industries": "", "alternative_tracking_resolved_industries": "可复核样本", "history_gate_fail_count": 0, "research_gate_fail_count": 1, "allowed_entry_count": 0, "live_entry_decision": "no_entry_currently", "live_entry_action": "只观察", "auto_execution_allowed": False}, reviewable)
        assert "## 盘前动作清单" in report
        assert "仅人工复核" in report
    print("self_check=pass")


if __name__ == "__main__":
    main()
