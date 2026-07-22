#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "goal_readiness_check"
DEBUG = OUT / "debug"

FIELDS = [
    "requirement",
    "status",
    "current_evidence",
    "required_to_complete",
    "evidence_path",
    "next_action",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an objective-level readiness checklist.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    sources = load_sources()
    rows = build_rows(sources)
    write_outputs(rows, sources)
    print(f"output_dir={OUT}")
    print(f"rows={len(rows)}")
    print(f"goal_ready={goal_ready(rows)}")


def load_sources() -> dict[str, Any]:
    return {
        "v471": read_json(ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "run_summary.json"),
        "v471_frozen": read_json(ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "frozen_policy.json"),
        "v471_guardrails": read_json(ROOT / "outputs" / "audit" / "v4_71_live_guardrail_playbook" / "run_summary.json"),
        "entry": read_json(ROOT / "outputs" / "audit" / "v4_72_entry_readiness" / "run_summary.json"),
        "operator": read_json(ROOT / "outputs" / "audit" / "v4_72_pre_entry_operator_checklist" / "run_summary.json"),
        "scorecard": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_evaluation_scorecard" / "run_summary.json"),
        "pretrade": read_json(ROOT / "outputs" / "audit" / "v4_72_pre_trade_review_packet" / "run_summary.json"),
        "settlement": read_json(ROOT / "outputs" / "audit" / "v4_72_forward_return_settlement" / "run_summary.json"),
        "settlement_schedule": read_rows(ROOT / "outputs" / "audit" / "v4_72_forward_return_settlement" / "debug" / "settlement_schedule.csv"),
        "fund_flow": read_json(ROOT / "outputs" / "audit" / "v4_72_candidate_fund_flow_overlay" / "run_summary.json"),
        "alternatives": read_json(ROOT / "outputs" / "audit" / "v4_72_carrier_alternative_tracking" / "run_summary.json"),
        "tradeable_leader": read_json(ROOT / "outputs" / "audit" / "v4_72_tradeable_research_blocked_leader" / "run_summary.json"),
        "quarantine": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_quarantine_overlay" / "run_summary.json"),
        "random_baseline": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_random_baseline_audit" / "run_summary.json"),
        "bootstrap": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_bootstrap_audit" / "run_summary.json"),
        "state_guardrail": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_state_guardrail" / "run_summary.json"),
    }


def build_rows(src: dict[str, Any]) -> list[dict[str, str]]:
    v471 = src["v471"]
    v471_frozen = src.get("v471_frozen", {})
    v471_guardrails = src.get("v471_guardrails", {})
    entry = src["entry"]
    operator = src.get("operator", {})
    scorecard = src["scorecard"]
    pretrade = src["pretrade"]
    settlement = src["settlement"]
    fund_flow = src["fund_flow"]
    alternatives = src["alternatives"]
    tradeable_leader = src.get("tradeable_leader", {})
    quarantine = src.get("quarantine", {})
    random_baseline = src.get("random_baseline", {})
    bootstrap = src.get("bootstrap", {})
    state_guardrail = src.get("state_guardrail", {})
    parameter_issue = issue_evidence(v471, "parameter_perturbation_pass_rate")
    cooldown_issue = issue_evidence(v471, "cooldown_60_clusters")
    year_state_issue = issue_evidence(v471, "year_state_sparse_cells")
    return [
        row("参数扰动稳健性", "fail" if parameter_issue else "pass", f"{parameter_issue or 'pass'}; guardrails={parameter_guardrail_evidence(v471_frozen)}; forbidden_playbook={v471_guardrails.get('forbidden_runtime_override_count', '')}; audit_only={v471_guardrails.get('audit_support_only_count', '')}; failed_variants={v471.get('parameter_failed_variants', '')}; actions={v471.get('parameter_failure_actions', '')}", "多数参数扰动仍有效，且 V4.71 对应闸门通过。", "outputs/industry_rebound_window_v4_71_robustness_live_audit/run_summary.json; outputs/industry_rebound_window_v4_71_robustness_live_audit/debug/frozen_policy.json; outputs/audit/v4_71_live_guardrail_playbook/run_summary.json", "继续随新增样本复核；冻结实盘参数护栏，不用单一冻结参数掩盖脆弱性。"),
        row("冷却期敏感性", "fail" if cooldown_issue else "pass", f"{cooldown_issue or 'pass'}; guardrails={cooldown_guardrail_evidence(v471_frozen)}; insufficient_clusters={v471_guardrails.get('insufficient_independent_clusters_count', '')}; failing_gaps={v471.get('cooldown_failing_gaps', '')}", "60日冷却独立样本与稳健性门槛通过。", "outputs/industry_rebound_window_v4_71_robustness_live_audit/run_summary.json; outputs/industry_rebound_window_v4_71_robustness_live_audit/debug/frozen_policy.json; outputs/audit/v4_71_live_guardrail_playbook/run_summary.json", "继续前推；冻结冷却期独立样本护栏，不降低冷却期门槛。"),
        row("分年/分状态拆解", "fail" if year_state_issue or int_value(scorecard.get("weak_year_count")) else "pass", f"weak_year_count={scorecard.get('weak_year_count', '')}; weak_years={scorecard.get('weak_years', '')}; year_state_sparse={year_state_issue or 'pass'}; guardrails={year_state_guardrail_evidence(v471_frozen)}; sparse_playbook={v471_guardrails.get('sparse_state_not_production_evidence_count', '')}; sparse_buckets={v471.get('year_state_sparse_buckets', '')}", "分年稳定性和分状态样本厚度通过；不把载体、入场或生产门禁混入该项。", "outputs/audit/v4_72_rebound_leader_evaluation_scorecard/run_summary.json; outputs/industry_rebound_window_v4_71_robustness_live_audit/run_summary.json; outputs/industry_rebound_window_v4_71_robustness_live_audit/debug/frozen_policy.json; outputs/audit/v4_71_live_guardrail_playbook/run_summary.json", "继续记录弱年份和稀疏状态桶；冻结分年/分状态护栏，不用均值掩盖失败。"),
        row("未来新增样本前推", "pending" if int_value(settlement.get("pending_rows")) or int_value(tradeable_leader.get("forward_observation_count")) else "pass", f"settled={settlement.get('settled_rows', '')}; pending={settlement.get('pending_rows', '')}; 下一前推动作={settlement.get('next_action_date', '')}:{settlement.get('next_action', '')}; 结构通过池前推={tradeable_leader.get('forward_observation_count', '')}; 结构通过池退出日={tradeable_leader.get('forward_planned_exit_date', '')}; 结构通过池状态={tradeable_leader.get('forward_observation_status', '')}", "计划退出日后结算真实 forward return。", "outputs/audit/v4_72_forward_return_settlement/run_summary.json; outputs/audit/v4_72_tradeable_research_blocked_leader/debug/forward_tradeable_leader_checklist.csv", "2026-06-23 入场日前重跑 live refresh；2026-07-21 后结算真实 forward return。"),
        row("真实可交易载体映射", "pass" if tradeable_mapping_ready(entry, pretrade) else "fail", f"结构阻断={pretrade.get('structural_blocked_count', '')}; 核心可交易复核池={pretrade.get('core_manual_review_count', '')}; 核心交易侧门禁通过={entry.get('core_tradeable_gate_pass_count', '')}; 替代载体可复核行业={alternatives.get('usable_alternative_industry_count', alternatives.get('usable_alternative_count', ''))}{tracking_reason_evidence(entry)}", "至少存在可复核、流动性与跟踪合格的核心人工复核池；不可交易行业必须被跳过或只观察。", "outputs/audit/v4_72_pre_trade_review_packet/run_summary.json", "保留不可交易行业的跳过规则；继续解决教育低流动性、一般零售/乘用车/焦炭跟踪弱等载体问题。"),
        row("当前资金流覆盖", "pass" if core_fund_flow_ready(pretrade) else "fail", f"available={fund_flow.get('available_overlay_count', '')}/{fund_flow.get('candidate_count', '')}; proxy={fund_flow.get('proxy_overlay_count', '')}; proxy_industries={fund_flow.get('proxy_candidate_industries', '')}; missing={fund_flow.get('missing_overlay_count', '')}; 核心资金流覆盖={core_fund_flow_count(pretrade)}/{pretrade.get('core_manual_review_count', '')}; 核心资金流={pretrade.get('core_manual_review_fund_flow', '')}; proxy_notes={fund_flow.get('proxy_observation_notes', '')}", "核心人工复核池必须有逐项当前资金流；代理口径行业必须被跳过或只观察。", "outputs/audit/v4_72_candidate_fund_flow_overlay/run_summary.json", "保留代理行业跳过规则；核心池资金流缺失时再补精确映射。"),
        row("入场日前实盘规则", "pass" if entry_rules_ready(entry) and operator_checklist_clear(operator) else "fail", f"{entry_gate_evidence(entry)}; operator_rows={operator.get('row_count', '')}; operator_p0={operator.get('p0_count', '')}; operator_p1={operator.get('p1_count', '')}; operator_p2={operator.get('p2_count', '')}; operator_p0_pending={operator.get('p0_pending_count', '')}; operator_p0_hard_stop={operator.get('p0_hard_stop_count', '')}; operator_p0_skip={operator.get('p0_skip_count', '')}; operator_state_guardrail_blocked={operator.get('state_guardrail_blocked_count', '')}; operator_entry_permitted={operator.get('entry_permitted', '')}; operator_ready={operator.get('pre_entry_operator_ready', '')}; operator_skip={operator.get('skip_count', '')}; operator_manual_review_only={operator.get('manual_review_only_count', '')}; operator_observe={operator.get('observe_only_count', '')}; {pretrade_bucket_evidence(pretrade)}", "能稳定输出跳过、只观察、仅人工复核三类盘前动作，覆盖全部候选；P0 未决、硬停止、跳过和状态护栏阻断必须清零，且自动执行关闭。", "outputs/audit/v4_72_entry_readiness/run_summary.json; outputs/audit/v4_72_pre_entry_operator_checklist/run_summary.json", "规则已落地；实际入场仍受日期、强行业验证、状态护栏和生产门禁约束；盘前只按操作总清单执行复核。"),
        row("强反弹行业评价体系", "fail" if int_value(scorecard.get("alpha_hard_gate_fail_count")) or int_value(scorecard.get("hard_gate_pending_count")) or int_value(tradeable_leader.get("evidence_fail_count")) else "pass", f"alpha_fail={scorecard.get('alpha_hard_gate_fail_count', '')}; alpha_fail_dimensions={scorecard.get('alpha_hard_gate_fail_dimensions', '')}; hard_pending={scorecard.get('hard_gate_pending_count', '')}; alpha_fail_metrics={scorecard.get('alpha_hard_gate_fail_metrics', '')}; alpha_actions={scorecard.get('alpha_failure_action_summary', '')}; guardrails={rebound_leader_guardrail_evidence(scorecard)}; 随机基准缺口=Top20+{random_baseline.get('top_quintile_success_gap', '')}/正年份+{random_baseline.get('positive_year_gap', '')}/相对胜率+{random_baseline.get('relative_win_success_gap', '')}; 逐事件随机={random_baseline.get('empirical_random_top_quintile_current', '')}; 分年随机失败={random_baseline.get('year_random_fail_years', '')}; 状态护栏失败={state_guardrail.get('failed_buckets', '')}; bootstrap失败={bootstrap.get('failed_metrics', '')}; bootstrap结论={bootstrap.get('final_verdict', '')}; 随机基准结论={random_baseline.get('final_verdict', '')}; non_alpha_hard_fail={scorecard.get('hard_gate_fail_count', '')}; fail_metrics={scorecard.get('hard_gate_fail_metrics', '')}; 结构通过池强反弹通过={tradeable_leader.get('evidence_pass_count', '')}/{tradeable_leader.get('target_count', '')}; 结构通过池最好={tradeable_leader.get('best_context_industry', '')}; 结构通过池阻断={tradeable_leader.get('blocked_industries', '')}; 隔离替补池={quarantine.get('replacement_observation_count', scorecard.get('quarantine_replacement_observation_count', ''))}; 替补观察={quarantine.get('top_replacement_industries', scorecard.get('quarantine_top_replacement_industries', ''))}", "能证明窗口内选出相对更强行业，而不只是全行业 beta；运营门禁单独审计。", "outputs/audit/v4_72_rebound_leader_evaluation_scorecard/run_summary.json; outputs/audit/v4_72_tradeable_research_blocked_leader/run_summary.json; outputs/audit/v4_72_rebound_leader_quarantine_overlay/run_summary.json; outputs/audit/v4_72_rebound_leader_random_baseline_audit/run_summary.json; outputs/audit/v4_72_rebound_leader_bootstrap_audit/run_summary.json; outputs/audit/v4_72_rebound_leader_state_guardrail/run_summary.json", "继续前推验证 Top 分位命中率和分年稳定性；失败状态桶内强行业排序只观察；结构通过池未过证据门槛前只能人工观察。"),
        row("实盘生产就绪", "fail" if not all(bool(x.get("production_ready")) for x in [v471, entry, scorecard, pretrade]) else "pass", f"v471={v471.get('production_ready')}; entry={entry.get('production_ready')}; scorecard={scorecard.get('production_ready')}; pretrade={pretrade.get('production_ready')}", "所有生产门禁通过，且 auto_execution_allowed 仍按人工规则控制。", "multiple", "当前保持 research_only，不升级生产。"),
    ]


def row(requirement: str, status: str, current: str, required: str, evidence: str, action: str) -> dict[str, str]:
    return {
        "requirement": requirement,
        "status": status,
        "current_evidence": current,
        "required_to_complete": required,
        "evidence_path": evidence,
        "next_action": action,
    }


def write_outputs(rows: list[dict[str, str]], sources: dict[str, Any] | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "goal_readiness_check.csv", rows)
    source_dates = source_date_summary(sources or {})
    summary = {
        "version": "goal_readiness_check_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **source_dates,
        "row_count": len(rows),
        "pass_count": sum(r["status"] == "pass" for r in rows),
        "fail_count": sum(r["status"] == "fail" for r in rows),
        "pending_count": sum(r["status"] == "pending" for r in rows),
        "failed_requirements": requirement_names(rows, "fail"),
        "pending_requirements": requirement_names(rows, "pending"),
        "next_action_summary": next_action_summary(rows),
        "hard_stop_reasons": hard_stop_reasons(rows),
        "goal_ready": goal_ready(rows),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "目标尚未完成；前推样本未结算，历史稳健性、强行业选择与生产门禁仍未通过。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    return "\n".join([
        "# 目标完成度总审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 源数据日期：as_of={summary.get('source_as_of_date', '')}；latest_panel={summary.get('source_latest_panel_date', '')}",
        f"- 快照新鲜度：{summary.get('snapshot_freshness_status', '')}；滞后日历日={summary.get('snapshot_stale_calendar_days', '')}",
        f"- 计划入场/退出：{summary.get('planned_entry_date', '')} / {summary.get('planned_exit_date', '')}",
        f"- 下一前推动作：{summary.get('next_forward_action_date', '')}；状态={summary.get('next_forward_action_due_status', '')}；剩余日历日={summary.get('next_forward_action_days_until', '')}",
        f"- 下一动作命令：`{summary.get('next_forward_action_command', '')}`",
        f"- 前推日程：{summary.get('forward_action_schedule', '')}",
        f"- 检查项：{summary['row_count']}",
        f"- 通过：{summary['pass_count']}",
        f"- 失败：{summary['fail_count']}",
        f"- 待结算：{summary['pending_count']}",
        f"- 失败要求：{summary['failed_requirements']}",
        f"- 待结算要求：{summary['pending_requirements']}",
        f"- 下一步动作：{summary['next_action_summary']}",
        f"- 硬停止原因：{summary['hard_stop_reasons']}",
        f"- 目标完成：`{str(summary['goal_ready']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        to_markdown(rows),
    ])


def source_date_summary(sources: dict[str, Any]) -> dict[str, str]:
    v471 = sources.get("v471", {}) if isinstance(sources, dict) else {}
    settlement = sources.get("settlement", {}) if isinstance(sources, dict) else {}
    schedule = sources.get("settlement_schedule", []) if isinstance(sources, dict) else []
    as_of = str(v471.get("as_of_date", ""))
    stale_days = snapshot_stale_days(as_of, date.today())
    next_action_date = str(settlement.get("next_action_date", ""))
    next_action_row = schedule_row_for_date(schedule, next_action_date)
    days_until_next_action = calendar_delta_days(next_action_date, date.today())
    return {
        "source_as_of_date": as_of,
        "source_latest_panel_date": str(v471.get("latest_panel_date", "")),
        "snapshot_stale_calendar_days": str(stale_days) if stale_days is not None else "",
        "snapshot_freshness_status": snapshot_freshness_status(stale_days),
        "planned_entry_date": str(v471.get("planned_entry_date", "")),
        "planned_exit_date": str(v471.get("planned_exit_date", "")),
        "next_forward_action_date": next_action_date,
        "next_forward_action_days_until": str(days_until_next_action) if days_until_next_action is not None else "",
        "next_forward_action_due_status": due_status(days_until_next_action),
        "next_forward_action": str(settlement.get("next_action", "")),
        "next_forward_action_command": str(next_action_row.get("command", "")),
        "forward_action_schedule": format_action_schedule(schedule),
    }


def schedule_row_for_date(rows: Any, action_date: str) -> dict[str, str]:
    if not isinstance(rows, list):
        return {}
    return next((row for row in rows if isinstance(row, dict) and row.get("event_date") == action_date), {})


def format_action_schedule(rows: Any) -> str:
    if not isinstance(rows, list):
        return ""
    parts = []
    for row in rows:
        if isinstance(row, dict) and row.get("event_date") and row.get("command"):
            parts.append(f"{row.get('event_date')} {row.get('event_type', '')}: {row.get('command')}")
    return " | ".join(parts)


def snapshot_stale_days(source_as_of_date: str, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(source_as_of_date)).days
    except ValueError:
        return None


def calendar_delta_days(target_date: str, today: date) -> int | None:
    try:
        return (date.fromisoformat(target_date) - today).days
    except ValueError:
        return None


def due_status(delta_days: int | None) -> str:
    if delta_days is None:
        return "unknown"
    if delta_days < 0:
        return "overdue"
    if delta_days == 0:
        return "due_today"
    return "pending"


def snapshot_freshness_status(stale_days: int | None) -> str:
    if stale_days is None:
        return "unknown"
    if stale_days < 0:
        return "source_date_after_today"
    if stale_days == 0:
        return "current_calendar_day"
    if stale_days == 1:
        return "stale_1_calendar_day"
    return "stale_gt1_calendar_days"


def to_markdown(rows: list[dict[str, str]]) -> str:
    cols = ["requirement", "status", "current_evidence", "required_to_complete", "next_action"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for item in rows:
        lines.append("| " + " | ".join(str(item.get(col, "")).replace("\n", " ") for col in cols) + " |")
    return "\n".join(lines)


def goal_ready(rows: list[dict[str, str]]) -> bool:
    return bool(rows) and all(r["status"] == "pass" for r in rows)


def requirement_names(rows: list[dict[str, str]], status: str) -> str:
    return "、".join(row["requirement"] for row in rows if row["status"] == status)


def next_action_summary(rows: list[dict[str, str]]) -> str:
    return "；".join(f"{row['requirement']}={row['next_action']}" for row in rows if row["status"] != "pass")


def hard_stop_reasons(rows: list[dict[str, str]]) -> str:
    stops = [row["requirement"] for row in rows if row["status"] in {"fail", "pending"}]
    return "、".join(stops) or "无"


def issue_evidence(summary: dict[str, Any], check: str) -> str:
    for item in summary.get("blocking_issues", []):
        if isinstance(item, dict) and item.get("check") == check and item.get("status") == "fail":
            return str(item.get("evidence", "fail"))
    return ""


def parameter_guardrail_evidence(frozen_policy: dict[str, Any]) -> str:
    guardrails = frozen_policy.get("live_parameter_guardrails") or {}
    forbidden = guardrails.get("forbidden_runtime_overrides") or []
    return f"{guardrails.get('status', 'missing')}; forbidden={len(forbidden)}; release={guardrails.get('release_condition', '')}"


def cooldown_guardrail_evidence(frozen_policy: dict[str, Any]) -> str:
    guardrails = frozen_policy.get("live_cooldown_guardrails") or {}
    forbidden = guardrails.get("forbidden_runtime_overrides") or []
    return f"{guardrails.get('status', 'missing')}; min_clusters={guardrails.get('minimum_independent_cluster_count', '')}; forbidden={len(forbidden)}; release={guardrails.get('release_condition', '')}"


def year_state_guardrail_evidence(frozen_policy: dict[str, Any]) -> str:
    guardrails = frozen_policy.get("live_year_state_guardrails") or {}
    forbidden = guardrails.get("forbidden_runtime_overrides") or []
    return f"{guardrails.get('status', 'missing')}; min_cell_events={guardrails.get('minimum_events_per_year_state_cell', '')}; forbidden={len(forbidden)}; release={guardrails.get('release_condition', '')}"


def rebound_leader_guardrail_evidence(scorecard: dict[str, Any]) -> str:
    guardrails = scorecard.get("live_rebound_leader_guardrails") or {}
    forbidden = guardrails.get("forbidden_runtime_overrides") or []
    return f"{guardrails.get('status', 'missing')}; allowed={guardrails.get('allowed_use', '')}; forbidden={len(forbidden)}; release={guardrails.get('release_condition', '')}"


def entry_gate_evidence(entry: dict[str, Any]) -> str:
    return (
        f"可入场={entry.get('allowed_entry_count', '')}; "
        f"日期待确认={entry.get('date_gate_pending_count', '')}; "
        f"载体失败={entry.get('carrier_gate_fail_count', '')}; "
        f"资金流失败={entry.get('fund_flow_gate_fail_count', '')}; "
        f"跟踪失败={entry.get('tracking_gate_fail_count', '')}; "
        f"历史失败={entry.get('history_gate_fail_count', '')}; "
        f"研究验证失败={entry.get('research_gate_fail_count', '')}; "
        f"核心交易门禁通过={entry.get('core_tradeable_gate_pass_count', '')}; "
        f"核心研究门禁失败={entry.get('core_research_gate_fail_count', '')}; "
        f"入场跳过名单={entry.get('entry_skip_rule_industries', '')}; "
        f"入场只观察名单={entry.get('entry_observe_only_rule_industries', '')}; "
        f"入场人工复核名单={entry.get('entry_manual_review_rule_industries', '')}; "
        f"跳过清理={entry.get('entry_skip_clearance_steps', '')}; "
        f"只观察清理={entry.get('entry_observe_only_clearance_steps', '')}; "
        f"人工复核清理={entry.get('entry_manual_review_clearance_steps', '')}"
    )


def pretrade_bucket_evidence(pretrade: dict[str, Any]) -> str:
    return (
        f"人工优先复核={pretrade.get('core_manual_review_count', '')}; "
        f"核心名单={pretrade.get('core_manual_review_industries', '')}; "
        f"核心载体={pretrade.get('core_manual_review_carriers', '')}; "
        f"核心跟踪={pretrade.get('core_manual_review_tracking', '')}; "
        f"核心资金流={pretrade.get('core_manual_review_fund_flow', '')}; "
        f"资金流确认=今日正向{pretrade.get('core_today_flow_positive_count', '')}/5日正向{pretrade.get('core_5d_flow_positive_count', '')}/双正向{pretrade.get('core_dual_flow_positive_count', '')}; "
        f"资金流状态={pretrade.get('core_flow_confirmation_status', '')}; "
        f"补证跳过={pretrade.get('skip_if_unresolved_count', '')}; "
        f"只观察={pretrade.get('observe_only_bucket_count', '')}"
    )


def tracking_reason_evidence(entry: dict[str, Any]) -> str:
    reasons = entry.get("tracking_failure_reason_counts", {})
    if not isinstance(reasons, dict) or not reasons:
        return ""
    top = entry.get("top_tracking_failure_reason", "")
    details = ",".join(f"{key}={value}" for key, value in reasons.items())
    return f"; tracking主因={top}; tracking原因={details}"


def tradeable_mapping_ready(entry: dict[str, Any], pretrade: dict[str, Any]) -> bool:
    core_count = int_value(pretrade.get("core_manual_review_count"))
    return core_count > 0 and int_value(entry.get("core_tradeable_gate_pass_count")) >= core_count


def core_fund_flow_ready(pretrade: dict[str, Any]) -> bool:
    core_count = int_value(pretrade.get("core_manual_review_count"))
    return core_count > 0 and core_fund_flow_count(pretrade) >= core_count


def core_fund_flow_count(pretrade: dict[str, Any]) -> int:
    text = str(pretrade.get("core_manual_review_fund_flow", ""))
    return sum(1 for part in text.split(",") if "=" in part)


def entry_rules_ready(entry: dict[str, Any]) -> bool:
    row_count = int_value(entry.get("row_count"))
    classified = (
        int_value(entry.get("entry_skip_rule_count"))
        + int_value(entry.get("entry_observe_only_rule_count"))
        + int_value(entry.get("entry_manual_review_rule_count"))
    )
    projected = (
        int_value(entry.get("projected_entry_blocked_count"))
        + int_value(entry.get("projected_entry_manual_review_only_count"))
        + int_value(entry.get("projected_entry_review_required_count"))
    )
    return row_count > 0 and classified == row_count and projected == row_count and entry.get("auto_execution_allowed") is False


def operator_checklist_clear(operator: dict[str, Any]) -> bool:
    return (
        int_value(operator.get("row_count")) > 0
        and int_value(operator.get("p0_pending_count")) == 0
        and int_value(operator.get("p0_hard_stop_count")) == 0
        and int_value(operator.get("p0_skip_count")) == 0
        and int_value(operator.get("state_guardrail_blocked_count")) == 0
        and operator.get("pre_entry_operator_ready") is True
        and operator.get("auto_execution_allowed") is False
    )


def pct(value: Any) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return ""


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
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def int_value(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def self_check() -> None:
    with tempfile.TemporaryDirectory():
        assert snapshot_stale_days("2026-06-19", date(2026, 6, 20)) == 1
        assert snapshot_freshness_status(1) == "stale_1_calendar_day"
        assert calendar_delta_days("2026-06-23", date(2026, 6, 20)) == 3
        assert due_status(3) == "pending"
        assert due_status(0) == "due_today"
        sample_summary = source_date_summary({"v471": {"as_of_date": "2026-06-19", "latest_panel_date": "2026-06-18", "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21"}, "settlement": {"next_action_date": "2026-06-23", "next_action": "入场日前重跑 live refresh"}, "settlement_schedule": [{"event_date": "2026-06-23", "event_type": "pre_entry_refresh", "command": "python .\\scripts\\run_v4_71_live_refresh.py --trade-date 2026-06-23"}, {"event_date": "2026-07-21", "event_type": "forward_settlement", "command": "python .\\scripts\\settle_v4_72_rebound_leader_forward_returns.py --as-of-date 2026-07-21"}]})
        assert sample_summary["source_latest_panel_date"] == "2026-06-18"
        assert sample_summary["next_forward_action_command"].endswith("--trade-date 2026-06-23")
        assert "2026-07-21 forward_settlement" in sample_summary["forward_action_schedule"]
        rows = build_rows({
            "v471": {"blocking_issues": [
                {"check": "parameter_perturbation_pass_rate", "status": "fail", "evidence": "33.33% variants effective"},
                {"check": "cooldown_60_clusters", "status": "fail", "evidence": "60d clusters=12"},
                {"check": "year_state_sparse_cells", "status": "fail", "evidence": "sparse_lt3=13"},
            ], "production_ready": False, "parameter_failed_variants": "entry_lag_1=worst_cluster_net_return", "parameter_failure_actions": "禁止把入场延迟从2日提前到1日", "cooldown_failing_gaps": "60d=12簇", "year_state_sparse_buckets": "2020/volatility_guard/非高波动区=1"},
            "v471_frozen": {
                "live_parameter_guardrails": {"status": "locked_for_research_only", "forbidden_runtime_overrides": ["entry_lag_days_lt_2"], "release_condition": "parameter_perturbation_pass_rate_gate_passes_with_forward_samples"},
                "live_cooldown_guardrails": {"status": "locked_for_research_only", "minimum_independent_cluster_count": 20, "forbidden_runtime_overrides": ["lower_cooldown_gap_to_create_sample_size"], "release_condition": "cooldown_60_clusters_gate_passes_with_forward_samples"},
                "live_year_state_guardrails": {"status": "locked_for_research_only", "minimum_events_per_year_state_cell": 3, "forbidden_runtime_overrides": ["hide_weak_year_with_full_sample_average"], "release_condition": "year_state_sparse_cells_gate_passes_with_forward_samples"},
            },
            "entry": {"allowed_entry_count": 0, "date_gate_pending_count": 1, "carrier_gate_fail_count": 1, "fund_flow_gate_fail_count": 0, "tracking_gate_fail_count": 1, "tracking_failure_reason_counts": {"large_cumulative_return_gap": 1}, "top_tracking_failure_reason": "large_cumulative_return_gap", "history_gate_fail_count": 0, "research_gate_fail_count": 1, "core_tradeable_gate_pass_count": 3, "core_research_gate_fail_count": 3, "entry_skip_rule_industries": "乘用车,饲料", "entry_observe_only_rule_industries": "教育", "entry_manual_review_rule_industries": "保险Ⅱ", "entry_skip_clearance_steps": "补载体跟踪证据，弱跟踪不进场。", "entry_observe_only_clearance_steps": "历史失败或重复最差事件未解除，只能观察。", "entry_manual_review_clearance_steps": "强反弹行业选择未验证，保持研究观察。", "production_ready": False},
            "operator": {"row_count": 16, "p0_count": 8, "p1_count": 4, "p2_count": 4, "p0_pending_count": 1, "p0_hard_stop_count": 2, "p0_skip_count": 4, "state_guardrail_blocked_count": 1, "entry_permitted": False, "pre_entry_operator_ready": False, "skip_count": 4, "manual_review_only_count": 4, "observe_only_count": 4},
            "scorecard": {"hard_gate_fail_count": 1, "hard_gate_fail_metrics": "top_quintile_hit_rate", "alpha_hard_gate_fail_count": 1, "alpha_hard_gate_fail_dimensions": "历史强行业选择", "alpha_hard_gate_fail_metrics": "top_quintile_hit_rate", "alpha_failure_action_summary": "历史强行业选择=提高Top分位命中率和正年份率", "live_rebound_leader_guardrails": {"status": "locked_for_research_only", "allowed_use": "manual_review_only", "forbidden_runtime_overrides": ["promote_best_current_industry_without_alpha_gate"], "release_condition": "alpha_hard_gates_and_tradeable_forward_settlement_pass"}, "hard_gate_pending_count": 0, "production_ready": False},
            "pretrade": {"structural_blocked_count": 1, "core_manual_review_count": 3, "core_manual_review_industries": "保险Ⅱ,游戏Ⅱ,旅游及景区", "core_manual_review_carriers": "保险Ⅱ=证券保险ETF易方达,游戏Ⅱ=游戏ETF华夏,旅游及景区=旅游ETF富国", "core_manual_review_tracking": "保险Ⅱ=253日重叠；日收益相关=0.86；累计收益差=7.06%", "core_manual_review_fund_flow": "保险Ⅱ=今日-45.21/5日-8.93/龙头中国人保,游戏Ⅱ=今日7.02/5日-7.28/龙头昆仑万维,旅游及景区=今日-1.69/5日-5.14/龙头三特索道", "core_today_flow_positive_count": 1, "core_5d_flow_positive_count": 0, "core_dual_flow_positive_count": 0, "core_flow_confirmation_status": "weak_today_only_positive_flow", "skip_if_unresolved_count": 4, "observe_only_bucket_count": 3, "production_ready": False},
            "settlement": {"settled_rows": 0, "pending_rows": 1, "next_action_date": "2026-06-23", "next_action": "入场日前重跑 live refresh；仍不自动入场。"},
            "fund_flow": {"available_overlay_count": 8, "candidate_count": 10, "proxy_overlay_count": 2, "proxy_candidate_industries": "饲料、焦炭Ⅱ", "missing_overlay_count": 0, "overlay_gate_fail_count": 2, "proxy_observation_notes": "饲料=饲料缺 THS 精确行业"},
            "alternatives": {"usable_alternative_count": 1},
            "random_baseline": {"top_quintile_success_gap": 2, "positive_year_gap": 2, "relative_win_success_gap": 0, "empirical_random_top_quintile_current": "observed=160; expected=117.21; z=4.64; p_one_sided=0.0000", "year_random_fail_years": "2016,2018", "final_verdict": "强行业选择相对随机有方向性，但仍未过硬门槛。"},
            "bootstrap": {"failed_metrics": "top_quintile_hit_rate,positive_year_rate", "final_verdict": "事件级 bootstrap 未通过全部强反弹行业稳定性门槛。"},
            "state_guardrail": {"failed_buckets": "stress_level:低/中压力", "final_verdict": "存在失败或样本不足状态桶。"},
        })
        assert goal_ready(rows) is False
        assert "参数扰动稳健性" in requirement_names(rows, "fail")
        assert "未来新增样本前推" in requirement_names(rows, "pending")
        assert "参数扰动稳健性=继续随新增样本复核" in next_action_summary(rows)
        assert "实盘生产就绪" in hard_stop_reasons(rows)
        assert any(r["requirement"] == "参数扰动稳健性" and r["status"] == "fail" for r in rows)
        assert any(r["requirement"] == "参数扰动稳健性" and "failed_variants=entry_lag_1=worst_cluster_net_return" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "参数扰动稳健性" and "guardrails=locked_for_research_only; forbidden=1" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "参数扰动稳健性" and "actions=禁止把入场延迟从2日提前到1日" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "冷却期敏感性" and "failing_gaps=60d=12簇" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "冷却期敏感性" and "guardrails=locked_for_research_only; min_clusters=20; forbidden=1" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "分年/分状态拆解" and "sparse_buckets=2020/volatility_guard/非高波动区=1" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "分年/分状态拆解" and "guardrails=locked_for_research_only; min_cell_events=3; forbidden=1" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "未来新增样本前推" and r["status"] == "pending" for r in rows)
        assert any(r["requirement"] == "未来新增样本前推" and "下一前推动作=2026-06-23" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "真实可交易载体映射" and r["status"] == "pass" for r in rows)
        assert any(r["requirement"] == "真实可交易载体映射" and "核心交易侧门禁通过=3" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "真实可交易载体映射" and "tracking主因=large_cumulative_return_gap" in r["current_evidence"] for r in rows)
        base_rows = rows
        assert entry_rules_ready({"row_count": 3, "entry_skip_rule_count": 1, "entry_observe_only_rule_count": 1, "entry_manual_review_rule_count": 1, "projected_entry_blocked_count": 2, "projected_entry_manual_review_only_count": 1, "projected_entry_review_required_count": 0, "auto_execution_allowed": False})
        assert not entry_rules_ready({"row_count": 3, "entry_skip_rule_count": 1, "entry_observe_only_rule_count": 0, "entry_manual_review_rule_count": 1, "projected_entry_blocked_count": 2, "projected_entry_manual_review_only_count": 1, "projected_entry_review_required_count": 0, "auto_execution_allowed": False})
        assert not operator_checklist_clear({"row_count": 16, "p0_pending_count": 1, "p0_hard_stop_count": 2, "p0_skip_count": 4, "state_guardrail_blocked_count": 1, "pre_entry_operator_ready": False, "auto_execution_allowed": False})
        assert operator_checklist_clear({"row_count": 16, "p0_pending_count": 0, "p0_hard_stop_count": 0, "p0_skip_count": 0, "state_guardrail_blocked_count": 0, "pre_entry_operator_ready": True, "auto_execution_allowed": False})
        rows = build_rows({
            "v471": {"blocking_issues": [], "production_ready": False},
            "entry": {"allowed_entry_count": 0, "core_tradeable_gate_pass_count": 0, "production_ready": False},
            "scorecard": {"hard_gate_fail_count": 0, "hard_gate_pending_count": 0, "production_ready": False},
            "pretrade": {"structural_blocked_count": 0, "core_manual_review_count": 0, "production_ready": False},
            "settlement": {"settled_rows": 1, "pending_rows": 0},
            "fund_flow": {"missing_overlay_count": 0},
            "alternatives": {},
        })
        assert any(r["requirement"] == "真实可交易载体映射" and r["status"] == "fail" for r in rows)
        rows = base_rows
        assert any(r["requirement"] == "入场日前实盘规则" and "研究验证失败=1" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "核心交易门禁通过=3" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "核心研究门禁失败=3" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "人工优先复核=3" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "核心名单=保险Ⅱ,游戏Ⅱ,旅游及景区" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "核心载体=保险Ⅱ=证券保险ETF易方达" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "核心跟踪=保险Ⅱ=253日重叠" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "核心资金流=保险Ⅱ=今日-45.21" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "资金流确认=今日正向1/5日正向0/双正向0" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "资金流状态=weak_today_only_positive_flow" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "入场跳过名单=乘用车,饲料" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "入场只观察名单=教育" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "入场人工复核名单=保险Ⅱ" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "跳过清理=补载体跟踪证据，弱跟踪不进场。" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "入场日前实盘规则" and "operator_state_guardrail_blocked=1" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "当前资金流覆盖" and r["status"] == "pass" for r in rows)
        assert any(r["requirement"] == "当前资金流覆盖" and "核心资金流覆盖=3/3" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "当前资金流覆盖" and "proxy_industries=饲料、焦炭Ⅱ" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "当前资金流覆盖" and "proxy_notes=饲料=饲料缺 THS 精确行业" in r["current_evidence"] for r in rows)
        rows = build_rows({
            "v471": {"blocking_issues": [], "production_ready": False},
            "entry": {"allowed_entry_count": 0, "production_ready": False},
            "scorecard": {"hard_gate_fail_count": 0, "hard_gate_pending_count": 0, "production_ready": False},
            "pretrade": {"structural_blocked_count": 0, "core_manual_review_count": 2, "core_manual_review_fund_flow": "保险Ⅱ=今日-1/5日-1/龙头样本", "production_ready": False},
            "settlement": {"settled_rows": 1, "pending_rows": 0},
            "fund_flow": {"missing_overlay_count": 0},
            "alternatives": {},
        })
        assert any(r["requirement"] == "当前资金流覆盖" and r["status"] == "fail" for r in rows)
        rows = base_rows
        assert any(r["requirement"] == "强反弹行业评价体系" and "alpha_fail=1" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "强反弹行业评价体系" and "alpha_fail_dimensions=历史强行业选择" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "强反弹行业评价体系" and "alpha_fail_metrics=top_quintile_hit_rate" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "强反弹行业评价体系" and "alpha_actions=历史强行业选择=提高Top分位命中率和正年份率" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "强反弹行业评价体系" and "guardrails=locked_for_research_only; allowed=manual_review_only; forbidden=1" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "强反弹行业评价体系" and "随机基准缺口=Top20+2/正年份+2/相对胜率+0" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "强反弹行业评价体系" and "逐事件随机=observed=160" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "强反弹行业评价体系" and "分年随机失败=2016,2018" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "强反弹行业评价体系" and "状态护栏失败=stress_level:低/中压力" in r["current_evidence"] for r in rows)
        assert any(r["requirement"] == "强反弹行业评价体系" and "bootstrap失败=top_quintile_hit_rate,positive_year_rate" in r["current_evidence"] for r in rows)
        rows = build_rows({
            "v471": {"blocking_issues": [{"check": "parameter_perturbation_pass_rate", "status": "fail", "evidence": "33.33% variants effective"}], "production_ready": False},
            "entry": {"allowed_entry_count": 0, "production_ready": False},
            "scorecard": {"hard_gate_fail_count": 0, "hard_gate_pending_count": 0, "production_ready": False},
            "pretrade": {"structural_blocked_count": 0, "production_ready": False},
            "settlement": {"settled_rows": 1, "pending_rows": 0},
            "fund_flow": {"missing_overlay_count": 0},
            "alternatives": {},
        })
        assert any(r["requirement"] == "参数扰动稳健性" and r["status"] == "fail" for r in rows)
        rows = build_rows({
            "v471": {"blocking_issues": [{"check": "year_state_sparse_cells", "status": "fail", "evidence": "sparse_lt3=13"}], "production_ready": False},
            "entry": {"allowed_entry_count": 1, "production_ready": False},
            "scorecard": {"hard_gate_fail_count": 0, "hard_gate_pending_count": 0, "production_ready": False},
            "pretrade": {"structural_blocked_count": 0, "production_ready": False},
            "settlement": {"settled_rows": 1, "pending_rows": 0},
            "fund_flow": {"missing_overlay_count": 0},
            "alternatives": {},
        })
        assert any(r["requirement"] == "分年/分状态拆解" and r["status"] == "fail" and "sparse_lt3=13" in r["current_evidence"] for r in rows)
    print("self_check=pass")


if __name__ == "__main__":
    main()
