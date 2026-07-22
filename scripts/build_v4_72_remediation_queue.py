#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V471_SUMMARY = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "run_summary.json"
SCORECARD = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_evaluation_scorecard" / "top_candidates.csv"
PRE_TRADE = ROOT / "outputs" / "audit" / "v4_72_pre_trade_review_packet" / "top_candidates.csv"
ENTRY_SUMMARY = ROOT / "outputs" / "audit" / "v4_72_entry_readiness" / "run_summary.json"
ENTRY_DETAIL = ROOT / "outputs" / "audit" / "v4_72_entry_readiness" / "debug" / "entry_readiness.csv"
OPERATOR_SUMMARY = ROOT / "outputs" / "audit" / "v4_72_pre_entry_operator_checklist" / "run_summary.json"
OPERATOR_DETAIL = ROOT / "outputs" / "audit" / "v4_72_pre_entry_operator_checklist" / "debug" / "operator_checklist.csv"
TRADEABLE_LEADER = ROOT / "outputs" / "audit" / "v4_72_tradeable_research_blocked_leader" / "run_summary.json"
LEDGER = ROOT / "logs" / "v4_72_rebound_leader_forward_ledger.csv"
OUT = ROOT / "outputs" / "audit" / "v4_72_remediation_queue"
DEBUG = OUT / "debug"

FIELDS = [
    "priority",
    "gap_type",
    "related_metric",
    "industry_code",
    "industry_name",
    "current_status",
    "practical_decision",
    "required_evidence",
    "next_action",
    "action_owner_agent",
    "due_rule",
    "auto_execution_impact",
    "evidence_path",
]

DEDICATED_SCORECARD_METRICS = {
    "tradeable_leader_evidence_pass_count",
    "tradeable_leader_forward_settlement",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a V4.72 remediation queue from failed gates and manual review rows.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    rows = build_queue(read_rows(SCORECARD), read_rows(PRE_TRADE), read_rows(LEDGER), read_json(ENTRY_SUMMARY), read_json(V471_SUMMARY), read_rows(ENTRY_DETAIL), read_json(TRADEABLE_LEADER), read_json(OPERATOR_SUMMARY), read_rows(OPERATOR_DETAIL))
    write_outputs(rows)
    print(f"output_dir={OUT}")
    print(f"queue_rows={len(rows)}")
    print("production_ready=False")


def build_queue(scorecard_rows: list[dict[str, str]], pre_trade_rows: list[dict[str, str]], ledger_rows: list[dict[str, str]], entry_summary: dict[str, Any] | None = None, v471_summary: dict[str, Any] | None = None, entry_rows: list[dict[str, str]] | None = None, tradeable_leader: dict[str, Any] | None = None, operator_summary: dict[str, Any] | None = None, operator_rows: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    entry_by_code = {row.get("industry_code", "").zfill(6): row for row in entry_rows or []}
    exit_dates = sorted({row.get("planned_exit_date", "") for row in ledger_rows if row.get("planned_exit_date")})
    next_exit = exit_dates[0] if exit_dates else "前推退出日后"
    rows.extend(v471_blocker_rows(v471_summary or {}))
    rows.extend(tradeable_leader_rows(tradeable_leader or {}))
    rows.extend(operator_blocker_rows(operator_summary or {}))
    rows.extend(operator_detail_rows(operator_rows or []))
    for item in scorecard_rows:
        if item.get("status") == "pass" or item.get("metric") in DEDICATED_SCORECARD_METRICS:
            continue
        rows.append({
            "priority": "P0_证据闸门",
            "gap_type": item.get("dimension", ""),
            "related_metric": item.get("metric", ""),
            "industry_code": "",
            "industry_name": "",
            "current_status": scorecard_current_status(item),
            "required_evidence": item.get("required", ""),
            "next_action": gate_action(item.get("metric", "")),
            "action_owner_agent": gate_owner(item.get("metric", "")),
            "due_rule": next_exit if item.get("metric") == "settled_forward_rows" else "每次 live refresh 后复核",
            "auto_execution_impact": "保持 system_position_cap_pct=0；不得自动执行",
            "evidence_path": item.get("evidence_path", ""),
        })
    rows.extend(entry_gate_rows(entry_summary or {}))
    for item in pre_trade_rows:
        gate = item.get("manual_gate_status", "")
        if gate == "research_observation_only":
            continue
        entry = entry_by_code.get(item.get("industry_code", "").zfill(6), {})
        rows.append({
            "priority": "P1_盘前补证" if gate.startswith("blocked") else "P2_观察风险",
            "gap_type": "盘前人工门禁",
            "related_metric": gate,
            "industry_code": item.get("industry_code", ""),
            "industry_name": item.get("industry_name", ""),
            "current_status": current_status(item, entry),
            "required_evidence": required_for_gate(gate),
            "next_action": item_action(item, entry),
            "action_owner_agent": owner_for_gate(gate),
            "due_rule": "入场日前人工复核；不能补齐则跳过",
            "auto_execution_impact": "保持 system_position_cap_pct=0；不得自动执行",
            "evidence_path": "outputs/audit/v4_72_pre_trade_review_packet/top_candidates.csv",
        })
    return [with_practical_decision(row) for row in sorted(rows, key=lambda row: (priority_rank(row["priority"]), row["industry_code"], row["related_metric"]))]


def operator_blocker_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    specs = [
        ("p0_pending_count", "盘前P0未决", "重跑计划入场日前 live refresh；未决项清零前不进入实盘复核。"),
        ("p0_hard_stop_count", "盘前P0硬停止", "先解除硬停止；不能用人工判断覆盖 P0 阻断。"),
        ("p0_skip_count", "盘前P0跳过", "被标记跳过的行业只能跳过；补齐证据后再进入下一轮复核。"),
        ("state_guardrail_blocked_count", "状态护栏阻断", "失败状态桶内强行业排序只观察，不得提高入场信心。"),
    ]
    rows = []
    for metric, gap_type, action in specs:
        count = int_value(summary.get(metric))
        if count <= 0:
            continue
        rows.append({
            "priority": "P0_证据闸门",
            "gap_type": gap_type,
            "related_metric": metric,
            "industry_code": "",
            "industry_name": "",
            "current_status": f"count={count}; operator_ready={summary.get('pre_entry_operator_ready', '')}; entry_permitted={summary.get('entry_permitted', '')}",
            "required_evidence": "0",
            "next_action": action,
            "action_owner_agent": "pre_entry_operator_auditor",
            "due_rule": "入场日前",
            "auto_execution_impact": "保持 system_position_cap_pct=0；不得自动执行",
            "evidence_path": "outputs/audit/v4_72_pre_entry_operator_checklist/run_summary.json",
        })
    return rows


def operator_detail_rows(items: list[dict[str, str]]) -> list[dict[str, str]]:
    stages = {"入场日前", "稳健性护栏", "稳健性硬护栏", "强行业选择"}
    statuses = {"blocked", "pending"}
    rows = []
    for item in items:
        if item.get("priority") != "P0" or item.get("stage") not in stages or item.get("status") not in statuses:
            continue
        rows.append({
            "priority": "P0_证据闸门",
            "gap_type": item.get("stage", ""),
            "related_metric": item.get("check_item", ""),
            "industry_code": "",
            "industry_name": "",
            "current_status": "; ".join(part for part in [item.get("status", ""), item.get("evidence", "")] if part),
            "required_evidence": "pass",
            "next_action": item.get("required_action", ""),
            "action_owner_agent": "pre_entry_operator_auditor",
            "due_rule": "入场日前",
            "auto_execution_impact": "保持 system_position_cap_pct=0；不得自动执行",
            "evidence_path": item.get("evidence_path", "outputs/audit/v4_72_pre_entry_operator_checklist/debug/operator_checklist.csv"),
        })
    return rows


def scorecard_current_status(item: dict[str, str]) -> str:
    current = item.get("current", "")
    status = item.get("status", "")
    return f"{status}; current={current}" if current else status


def tradeable_leader_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    if int_value(summary.get("evidence_fail_count")):
        rows.append({
            "priority": "P0_证据闸门",
            "gap_type": "结构通过池强反弹",
            "related_metric": "tradeable_leader_evidence_pass_count",
            "industry_code": "",
            "industry_name": str(summary.get("blocked_industries", "")),
            "current_status": f"pass={summary.get('evidence_pass_count', '')}/{summary.get('target_count', '')}; best={summary.get('best_context_industry', '')}; mean_relative={summary.get('best_context_mean_relative_return', '')}; top_quintile={summary.get('best_context_top_quintile_hit_rate', '')}",
            "required_evidence": "结构通过池至少有行业通过强反弹证据门槛，且不能依赖单次样本。",
            "next_action": "继续前推结构通过池；保险Ⅱ样本太少、游戏Ⅱ接近但Top分位命中不足、旅游及景区证据缺失时都不能放行。",
            "action_owner_agent": "factor_validation_auditor",
            "due_rule": "每次 live refresh 后复核；退出日后用真实前推刷新",
            "auto_execution_impact": "保持 system_position_cap_pct=0；不得自动执行",
            "evidence_path": "outputs/audit/v4_72_tradeable_research_blocked_leader/run_summary.json",
        })
    if int_value(summary.get("forward_observation_count")) and int_value(summary.get("forward_settled_count")) < int_value(summary.get("forward_observation_count")):
        rows.append({
            "priority": "P0_证据闸门",
            "gap_type": "结构通过池前推",
            "related_metric": "tradeable_leader_forward_settlement",
            "industry_code": "",
            "industry_name": str(summary.get("blocked_industries", "")),
            "current_status": f"observations={summary.get('forward_observation_count', '')}; settled={summary.get('forward_settled_count', '')}; status={summary.get('forward_observation_status', '')}",
            "required_evidence": "到退出日后结算结构通过池行业相对收益和未来收益前20%命中。",
            "next_action": "等计划退出日后运行结构通过池审计，结算 realized_relative_return 与 future_top_quintile。",
            "action_owner_agent": "forward_settlement_auditor",
            "due_rule": str(summary.get("forward_planned_exit_date", "退出日后")),
            "auto_execution_impact": "保持 system_position_cap_pct=0；不得自动执行",
            "evidence_path": "outputs/audit/v4_72_tradeable_research_blocked_leader/debug/forward_tradeable_leader_checklist.csv",
        })
    return rows


def v471_blocker_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for item in summary.get("blocking_issues", []):
        if not isinstance(item, dict) or item.get("status") != "fail":
            continue
        metric = str(item.get("check", ""))
        rows.append({
            "priority": "P0_证据闸门",
            "gap_type": "V4.71稳健性阻断",
            "related_metric": metric,
            "industry_code": "",
            "industry_name": "",
            "current_status": "; ".join(x for x in [str(item.get("evidence", "")), v471_blocker_detail(metric, summary)] if x),
            "required_evidence": "pass",
            "next_action": v471_blocker_action(metric, summary),
            "action_owner_agent": v471_blocker_owner(metric),
            "due_rule": "每次 live refresh 后复核",
            "auto_execution_impact": "保持 system_position_cap_pct=0；不得自动执行",
            "evidence_path": "outputs/industry_rebound_window_v4_71_robustness_live_audit/run_summary.json",
        })
    return rows


def v471_blocker_detail(metric: str, summary: dict[str, Any]) -> str:
    details = {
        "parameter_perturbation_pass_rate": str(summary.get("parameter_failed_variants", "")),
        "cooldown_60_clusters": str(summary.get("cooldown_failing_gaps", "")),
        "year_state_sparse_cells": str(summary.get("year_state_sparse_buckets", "")),
    }
    return details.get(metric, "")


def v471_blocker_action(metric: str, summary: dict[str, Any] | None = None) -> str:
    summary = summary or {}
    actions = {
        "parameter_perturbation_pass_rate": "继续前推新增样本；不得临场改入场延迟、止损或波动阈值来迎合历史。",
        "cooldown_60_clusters": "继续累计独立行情簇；同一轮行情重复触发不能当作独立证据。",
        "year_state_sparse_cells": "继续累计分年 x 分状态样本；稀疏格子只能作描述，不能作生产门槛依据。",
    }
    base = actions.get(metric, "复核 V4.71 稳健性阻断项。")
    if metric == "parameter_perturbation_pass_rate" and summary.get("parameter_failure_actions"):
        return f"{base} 当前禁止动作：{summary['parameter_failure_actions']}"
    return base


def v471_blocker_owner(metric: str) -> str:
    if metric == "cooldown_60_clusters":
        return "event_independence_auditor"
    if metric == "year_state_sparse_cells":
        return "market_state_validation_auditor"
    return "robustness_auditor"


def entry_gate_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    specs = [
        ("date_gate_pending_count", "日期门禁", "等计划入场日重跑 live refresh，不提前入场。", "entry_readiness_auditor", "入场日前"),
        ("carrier_gate_fail_count", "载体门禁", "补可复核载体；没有合格载体的行业直接跳过。", "carrier_mapping_auditor", "入场日前人工复核"),
        ("fund_flow_gate_fail_count", "资金流门禁", "补精确资金流映射；代理资金流不解除门禁。", "fund_flow_mapping_auditor", "入场日前人工复核"),
        ("tracking_gate_fail_count", "跟踪门禁", "补载体跟踪证据；弱跟踪载体不放行。", "carrier_mapping_auditor", "入场日前人工复核"),
        ("history_gate_fail_count", "历史失败门禁", "历史失败或重复最差事件只观察，等待新增前推证据。", "factor_validation_auditor", "每次 forward settlement 后复核"),
        ("research_gate_fail_count", "研究验证门禁", "强反弹行业选择未验证前，所有候选保持 research_only。", "factor_validation_auditor", "每次 live refresh 后复核"),
    ]
    rows = []
    for metric, gap_type, action, owner, due_rule in specs:
        count = int_value(summary.get(metric))
        if count <= 0:
            continue
        detail = entry_gate_detail(metric, summary)
        rows.append({
            "priority": "P0_证据闸门" if metric in {"research_gate_fail_count", "date_gate_pending_count"} else "P1_盘前补证",
            "gap_type": gap_type,
            "related_metric": metric,
            "industry_code": "",
            "industry_name": "",
            "current_status": "; ".join(x for x in [f"fail_count={count}", detail] if x),
            "required_evidence": "0",
            "next_action": entry_gate_action(metric, action, summary),
            "action_owner_agent": owner,
            "due_rule": due_rule,
            "auto_execution_impact": "保持 system_position_cap_pct=0；不得自动执行",
            "evidence_path": "outputs/audit/v4_72_entry_readiness/run_summary.json",
        })
    return rows


def entry_gate_detail(metric: str, summary: dict[str, Any]) -> str:
    details = {
        "date_gate_pending_count": "等到计划入场日",
        "fund_flow_gate_fail_count": summary.get("entry_skip_rule_industries", ""),
        "tracking_gate_fail_count": summary.get("entry_skip_rule_industries", ""),
        "history_gate_fail_count": summary.get("entry_observe_only_rule_industries", ""),
        "research_gate_fail_count": summary.get("entry_manual_review_rule_industries", ""),
    }
    value = str(details.get(metric, ""))
    parts = [f"affected={value}" if value else ""]
    if metric == "research_gate_fail_count":
        parts.append(rebound_leader_guardrail_status())
    return "; ".join(part for part in parts if part)


def rebound_leader_guardrail_status() -> str:
    return "guardrails=locked_for_research_only; allowed=manual_review_only; forbidden=4"


def entry_gate_action(metric: str, action: str, summary: dict[str, Any]) -> str:
    extras = {
        "fund_flow_gate_fail_count": summary.get("entry_skip_clearance_steps", ""),
        "tracking_gate_fail_count": summary.get("entry_skip_clearance_steps", ""),
        "history_gate_fail_count": summary.get("entry_observe_only_clearance_steps", ""),
        "research_gate_fail_count": summary.get("entry_manual_review_clearance_steps", ""),
    }
    extra = str(extras.get(metric, ""))
    return f"{action} 具体清理：{extra}" if extra else action


def gate_action(metric: str) -> str:
    actions = {
        "top_quintile_hit_rate": "继续前推验证 Top10 是否进入全行业未来反弹前 20%；不要降低命中率门槛。",
        "top_quintile_wilson_lower_bound": "继续前推强反弹命中样本，直到 Top 分位命中率置信下界超过随机前 20%；不要只看点估计。",
        "relative_win_wilson_lower_bound": "继续前推跑赢样本，直到相对胜率置信下界超过 50%；不要用少量高收益事件替代胜率证据。",
        "positive_year_rate": "按年度追加前推结果，确认弱年份是否改善；不要用均值掩盖分年失败。",
        "worst_weak_year_relative_return": "复盘最差年份的行业组合；若仍明显跑输，不得把均值收益当作强行业选择能力。",
        "repeated_worst_event_industry_count": "把反复出现在最差事件中的行业列为风险提示；未证明修复前，不得把它们作为强反弹核心证据。",
        "latest_repeated_worst_industry_count": "盘前候选若踩中历史反复最差行业，只能保留观察；需要替换为未反复失败且仍满足评分的候选。",
        "reviewable_carrier_industry_count": "补齐无载体行业，或明确标记不可交易并从实盘候选中剔除。",
        "fund_flow_overlay_coverage": "补齐缺失行业的当前资金流映射；不能用宽泛代理硬凑。",
        "allowed_entry_count": "查看入场就绪明细；只有候选同时满足到期、载体、资金流、跟踪和历史失败门禁，才允许从观察升级为可入场。",
        "projected_entry_blocked_count": "入场日前刷新价格、资金流、载体映射和跟踪证据；若预计阻断仍大于 0，入场日继续跳过。",
        "production_ready": "等待硬闸门全部通过且前推样本结算后再重新评估。",
        "settled_forward_rows": "到计划退出日后运行结算脚本，填入真实 forward return。",
        "asof_failure_filter_passes_gate": "保留历史失败过滤为风险提示，不把它升级为剔除规则；继续观察 Top 分位命中率和分年稳定性是否改善。",
        "factor_discovery_passes_gate": "不要临场选择单个表现较好的因子；只有单因子在全样本、OOS、Top 分位和分年稳定性同时过门槛后才可升级。",
        "structure_factor_passes_gate": "继续前推结构变化因子；未过门槛前不能用相对强度、成交变化或估值变化替代强行业选择证据。",
    }
    return actions.get(metric, "复核该闸门的证据文件并更新状态。")


def gate_owner(metric: str) -> str:
    if metric in {"reviewable_carrier_industry_count"}:
        return "carrier_mapping_auditor"
    if metric in {"fund_flow_overlay_coverage"}:
        return "fund_flow_mapping_auditor"
    if metric in {"settled_forward_rows"}:
        return "forward_settlement_auditor"
    if metric in {"allowed_entry_count", "projected_entry_blocked_count"}:
        return "entry_readiness_auditor"
    return "factor_validation_auditor"


def current_status(item: dict[str, str], entry: dict[str, str]) -> str:
    reason = entry.get("tracking_failure_reason", "")
    if reason and reason != "pass":
        return f"{item.get('manual_gate_status', '')}; tracking={reason}"
    return item.get("manual_gate_status", "")


def with_practical_decision(row: dict[str, str]) -> dict[str, str]:
    row["practical_decision"] = practical_decision(row)
    return row


def practical_decision(row: dict[str, str]) -> str:
    priority = row.get("priority", "")
    metric = row.get("related_metric", "")
    status = row.get("current_status", "")
    if metric == "settled_forward_rows":
        return "等待未来样本结算，不提前认定有效。"
    if priority.startswith("P0"):
        return "系统闸门未过，保持 research_only。"
    if priority.startswith("P2"):
        return "只观察，不进入实盘候选。"
    if "blocked" in status or "fail" in status:
        return "盘前补证；未补齐则跳过。"
    return "人工复核，不自动执行。"


def required_for_gate(gate: str) -> str:
    if gate == "blocked_no_tradeable_carrier":
        return "存在行业相关、非宽基、可复核、流动性和折溢价合格的载体。"
    if gate == "blocked_missing_fund_flow":
        return "存在明确 THS 行业映射和当日资金流缓存。"
    if gate == "blocked_proxy_fund_flow_only":
        return "存在精确 THS 行业映射；代理观察不能解除阻断。"
    if gate == "blocked_tracking_not_ready":
        return "完成载体跟踪观察，证明载体与行业暴露匹配。"
    if gate == "blocked_tracking_weak":
        return "载体-行业相关性和收益差达到可观察标准，或更换更贴近行业的载体。"
    if gate == "observe_only_low_liquidity":
        return "流动性达标或维持只观察。"
    if gate == "observe_only_historical_failure":
        return "历史失败标记解除，或新证据证明该行业不再属于重复失败样本。"
    return "人工复核通过。"


def item_action(item: dict[str, str], entry: dict[str, str] | None = None) -> str:
    entry = entry or {}
    gate = item.get("manual_gate_status", "")
    if gate == "blocked_no_tradeable_carrier":
        evidence = item.get("carrier_mapping_evidence", "")
        suffix = f" 当前映射证据只有：{evidence}。" if evidence else ""
        return f"不要用宽基或泛行业 ETF 替代；先做载体白名单人工核验。{suffix}"
    if gate == "blocked_missing_fund_flow":
        return "补 THS 映射和资金流缓存；若只能代理映射，必须标记为 proxy_observation。"
    if gate == "blocked_proxy_fund_flow_only":
        return with_tracking_context("保留代理观察但不放行；继续寻找精确行业资金流或标记不可精确覆盖。", entry)
    if gate == "blocked_tracking_not_ready":
        return tracking_action(entry, "做载体-行业跟踪误差和成分暴露复核。")
    if gate == "blocked_tracking_weak":
        return tracking_action(entry, "不要用该载体放行；优先寻找更贴近行业的载体，或继续累计跟踪证据。")
    if gate == "observe_only_low_liquidity":
        return with_tracking_context("只观察；低流动性载体不得按常规仓位处理。", entry)
    return item.get("manual_gate_action", "") or "继续观察，不进入自动执行。"


def with_tracking_context(action: str, entry: dict[str, str]) -> str:
    reason = entry.get("tracking_failure_reason", "")
    if not reason or reason == "pass":
        return action
    note = entry.get("carrier_tracking_evidence_note", "")
    suffix = note or reason
    return f"{action} 同时存在跟踪问题：{suffix}"


def tracking_action(entry: dict[str, str], fallback: str) -> str:
    reason = entry.get("tracking_failure_reason", "")
    note = entry.get("carrier_tracking_evidence_note", "")
    if not reason:
        return fallback
    actions = []
    if "low_liquidity" in reason:
        actions.append("先找成交额达标载体，低流动性载体只观察")
    if "tracking_not_audited" in reason:
        actions.append("补足载体历史跟踪审计")
    if "low_daily_return_corr" in reason:
        actions.append("优先寻找日收益相关>=0.70的载体")
    if "large_cumulative_return_gap" in reason:
        actions.append("要求累计收益差压到20%以内")
    detail = "；".join(actions) or fallback
    return f"{detail}。当前证据：{note or reason}"


def owner_for_gate(gate: str) -> str:
    if "carrier" in gate or "tracking" in gate or "liquidity" in gate:
        return "carrier_mapping_auditor"
    if "fund_flow" in gate:
        return "fund_flow_mapping_auditor"
    if "historical_failure" in gate:
        return "factor_validation_auditor"
    return "manual_review_agent"


def write_outputs(rows: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "remediation_queue.csv", rows)
    summary = {
        "version": "v4_72_remediation_queue_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "queue_rows": len(rows),
        "p0_count": sum(row["priority"].startswith("P0") for row in rows),
        "p1_count": sum(row["priority"].startswith("P1") for row in rows),
        "p2_count": sum(row["priority"].startswith("P2") for row in rows),
        "operator_blocker_count": sum(row["action_owner_agent"] == "pre_entry_operator_auditor" for row in rows),
        "auto_execution_allowed": False,
        "production_ready": False,
        "final_verdict": "该清单只列出补证动作；未完成前系统仓位上限保持 0。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.72 补证动作清单",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 动作数：{summary['queue_rows']}",
        f"- P0 证据闸门：{summary['p0_count']}",
        f"- P1 盘前补证：{summary['p1_count']}",
        f"- P2 观察风险：{summary['p2_count']}",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        f"- 生产就绪：`{str(summary['production_ready']).lower()}`",
        "",
        to_markdown(rows),
        "",
        "边界：该清单用于推进研究和人工复核，不生成买入/卖出指令。",
    ])


def to_markdown(rows: list[dict[str, str]]) -> str:
    cols = ["priority", "gap_type", "related_metric", "industry_name", "current_status", "practical_decision", "next_action", "due_rule"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")).replace("\n", " ") for col in cols) + " |")
    return "\n".join(lines)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def priority_rank(priority: str) -> int:
    return {"P0_证据闸门": 0, "P1_盘前补证": 1, "P2_观察风险": 2}.get(priority, 9)


def int_value(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def self_check() -> None:
    with tempfile.TemporaryDirectory():
        rows = build_queue(
            [{"dimension": "未来新增样本前推", "metric": "settled_forward_rows", "status": "pending", "required": ">= 10"}],
            [{"industry_code": "801203", "industry_name": "一般零售", "manual_gate_status": "blocked_no_tradeable_carrier"}],
            [{"planned_exit_date": "2026-07-21"}],
            {"research_gate_fail_count": 1, "tracking_gate_fail_count": 2},
            {"blocking_issues": [{"check": "year_state_sparse_cells", "status": "fail", "evidence": "sparse_lt3=13"}], "year_state_sparse_buckets": "2020/volatility_guard/非高波动区=1"},
            tradeable_leader={"target_count": 3, "evidence_pass_count": 0, "evidence_fail_count": 3, "blocked_industries": "保险Ⅱ,游戏Ⅱ,旅游及景区", "forward_observation_count": 3, "forward_settled_count": 0, "forward_observation_status": "pending_until_exit_date", "forward_planned_exit_date": "2026-07-21"},
            operator_summary={"row_count": 16, "p0_pending_count": 1, "p0_hard_stop_count": 2, "p0_skip_count": 4, "state_guardrail_blocked_count": 1, "pre_entry_operator_ready": False, "entry_permitted": False},
            operator_rows=[
                {"priority": "P0", "stage": "稳健性护栏", "check_item": "禁止临场改失败参数", "status": "blocked", "required_action": "不得提前入场或放宽止损。", "evidence": "forbidden=6", "evidence_path": "outputs/audit/v4_71_live_guardrail_playbook/run_summary.json"},
                {"priority": "P0", "stage": "稳健性硬护栏", "check_item": "参数扰动稳健性", "status": "blocked", "required_action": "禁止临场替换失败参数。", "evidence": "forbidden_runtime_override_count=6", "evidence_path": "outputs/audit/v4_71_live_guardrail_playbook/run_summary.json"},
                {"priority": "P0", "stage": "强行业选择", "check_item": "强行业状态护栏", "status": "blocked", "required_action": "失败状态桶内强行业排序只观察。", "evidence": "triggered=True", "evidence_path": "outputs/audit/v4_72_rebound_leader_state_guardrail/debug/state_guardrail.csv"},
            ],
        )
        assert rows[0]["priority"] == "P0_证据闸门"
        assert any(row["industry_name"] == "一般零售" and "宽基" in row["next_action"] for row in rows)
        assert any(row["due_rule"] == "2026-07-21" for row in rows)
        assert any(row["related_metric"] == "research_gate_fail_count" and row["action_owner_agent"] == "factor_validation_auditor" for row in rows)
        assert any(row["related_metric"] == "research_gate_fail_count" and "guardrails=locked_for_research_only; allowed=manual_review_only; forbidden=4" in row["current_status"] for row in rows)
        assert any(row["related_metric"] == "tracking_gate_fail_count" and row["priority"] == "P1_盘前补证" for row in rows)
        assert any(row["related_metric"] == "year_state_sparse_cells" and row["action_owner_agent"] == "market_state_validation_auditor" for row in rows)
        assert any(row["related_metric"] == "year_state_sparse_cells" and "2020/volatility_guard" in row["current_status"] for row in rows)
        assert any(row["related_metric"] == "tradeable_leader_forward_settlement" and row["due_rule"] == "2026-07-21" for row in rows)
        assert any(row["related_metric"] == "tradeable_leader_evidence_pass_count" and "pass=0/3" in row["current_status"] for row in rows)
        assert any(row["related_metric"] == "state_guardrail_blocked_count" and row["action_owner_agent"] == "pre_entry_operator_auditor" for row in rows)
        assert any(row["related_metric"] == "p0_hard_stop_count" and "count=2" in row["current_status"] for row in rows)
        assert any(row["related_metric"] == "禁止临场改失败参数" and row["action_owner_agent"] == "pre_entry_operator_auditor" for row in rows)
        assert any(row["related_metric"] == "参数扰动稳健性" and row["action_owner_agent"] == "pre_entry_operator_auditor" for row in rows)
        assert any(row["related_metric"] == "强行业状态护栏" and "triggered=True" in row["current_status"] for row in rows)
        assert any(row["industry_name"] == "一般零售" and row["practical_decision"] == "盘前补证；未补齐则跳过。" for row in rows)
        rows = build_queue(
            [{"dimension": "入场就绪", "metric": "allowed_entry_count", "status": "fail", "required": "> 0"}],
            [],
            [],
            {"fund_flow_gate_fail_count": 1, "entry_skip_rule_industries": "饲料", "entry_skip_clearance_steps": "补精确资金流映射"},
            {"blocking_issues": [{"check": "parameter_perturbation_pass_rate", "status": "fail", "evidence": "33.33% variants effective"}], "parameter_failed_variants": "entry_lag_1=worst_cluster_net_return", "parameter_failure_actions": "禁止把入场延迟从2日提前到1日"},
        )
        assert rows[0]["action_owner_agent"] == "entry_readiness_auditor"
        assert "可入场" in rows[0]["next_action"]
        assert any(row["related_metric"] == "parameter_perturbation_pass_rate" for row in rows)
        assert any(row["related_metric"] == "parameter_perturbation_pass_rate" and "entry_lag_1" in row["current_status"] for row in rows)
        assert any(row["related_metric"] == "fund_flow_gate_fail_count" and "affected=饲料" in row["current_status"] for row in rows)
        assert any(row["related_metric"] == "fund_flow_gate_fail_count" and "具体清理：补精确资金流映射" in row["next_action"] for row in rows)
        rows = build_queue(
            [
                {"metric": "tradeable_leader_evidence_pass_count", "status": "fail"},
                {"metric": "tradeable_leader_forward_settlement", "status": "pending"},
            ],
            [],
            [],
            tradeable_leader={
                "target_count": 3,
                "evidence_pass_count": 0,
                "evidence_fail_count": 3,
                "forward_observation_count": 3,
                "forward_settled_count": 0,
            },
        )
        assert sum(row["related_metric"] == "tradeable_leader_evidence_pass_count" for row in rows) == 1
        assert sum(row["related_metric"] == "tradeable_leader_forward_settlement" for row in rows) == 1
    print("self_check=pass")


if __name__ == "__main__":
    main()
