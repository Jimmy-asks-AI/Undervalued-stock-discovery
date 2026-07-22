#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import math
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
SETTLEMENT = ROOT / "outputs" / "audit" / "v4_72_forward_return_settlement"
FUND_FLOW = ROOT / "outputs" / "audit" / "v4_72_candidate_fund_flow_overlay"
PRE_TRADE = ROOT / "outputs" / "audit" / "v4_72_pre_trade_review_packet"
ENTRY_READINESS = ROOT / "outputs" / "audit" / "v4_72_entry_readiness"
TRADEABLE_LEADER = ROOT / "outputs" / "audit" / "v4_72_tradeable_research_blocked_leader"
QUARANTINE = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_quarantine_overlay"
RANDOM_BASELINE = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_random_baseline_audit"
BOOTSTRAP = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_bootstrap_audit"
STATE_GUARDRAIL = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_state_guardrail"
OUT = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_evaluation_scorecard"
DEBUG = OUT / "debug"
TOP_QUINTILE_RANDOM_BASELINE = 0.20
RELATIVE_WIN_BASELINE = 0.50
MAX_WEAK_YEAR_RELATIVE_LOSS = -0.02
ALPHA_HARD_GATE_DIMENSIONS = {"历史强行业选择", "统计可靠性", "分年风险", "分状态风险", "失败集中度", "当前候选风险"}
ALPHA_FAILURE_ACTIONS = {
    "历史强行业选择": "提高Top分位命中率和正年份率",
    "统计可靠性": "继续前推直到Wilson下界超过随机基准",
    "分年风险": "修复或隔离2025等弱年份",
    "分状态风险": "只在通过的状态桶内人工复核，失败状态桶降级只观察",
    "失败集中度": "降级反复出现在最差事件中的行业",
    "当前候选风险": "当前踩中反复最差事件的候选只观察或替换",
}
ALPHA_HARD_GATE_DIMENSIONS.add("特征时点与泄漏审计")
ALPHA_FAILURE_ACTIONS["特征时点与泄漏审计"] = "修复特征时点或泄漏审计失败项；禁止使用未来收益、入场后数据或结算后字段做排序"

REBOUND_LEADER_GUARDRAILS = {
    "status": "locked_for_research_only",
    "allowed_use": "manual_review_only",
    "release_condition": "alpha_hard_gates_and_tradeable_forward_settlement_pass",
    "forbidden_runtime_overrides": [
        "promote_best_current_industry_without_alpha_gate",
        "use_top_quintile_point_estimate_without_wilson_lower_bound",
        "ignore_weak_year_or_repeated_worst_industry",
        "promote_tradeable_research_blocked_pool_before_forward_settlement",
    ],
}

FIELDS = [
    "dimension",
    "metric",
    "current",
    "required",
    "status",
    "severity",
    "evidence_path",
    "interpretation",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the V4.72 rebound-leader evaluation scorecard.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    rows = build_scorecard(load_sources())
    write_outputs(rows)
    print(f"output_dir={OUT}")
    print(f"rows={len(rows)}")
    print(f"production_ready={production_ready(rows)}")


def load_sources() -> dict[str, Any]:
    return {
        "leader": read_json(V472 / "run_summary.json"),
        "latest_rows": read_rows(V472 / "top_candidates.csv"),
        "leakage_rows": read_rows(V472 / "debug" / "leakage_audit.csv"),
        "gate_rows": read_rows(V472 / "debug" / "evaluation_gate_audit.csv"),
        "strategy_rows": read_rows(V472 / "debug" / "strategy_results.csv"),
        "failure_rows": read_rows(V472 / "debug" / "failure_diagnosis.csv"),
        "settlement": read_json(SETTLEMENT / "run_summary.json"),
        "fund_flow": read_json(FUND_FLOW / "run_summary.json"),
        "pre_trade": read_json(PRE_TRADE / "run_summary.json"),
        "entry_readiness": read_json(ENTRY_READINESS / "run_summary.json"),
        "tradeable_leader": read_json(TRADEABLE_LEADER / "run_summary.json"),
        "quarantine": read_json(QUARANTINE / "run_summary.json"),
        "random_baseline": read_json(RANDOM_BASELINE / "run_summary.json"),
        "bootstrap": read_json(BOOTSTRAP / "run_summary.json"),
        "state_guardrail": read_json(STATE_GUARDRAIL / "run_summary.json"),
    }


def build_scorecard(src: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    leader = src.get("leader", {})
    rows.extend(feature_timing_rows(src.get("leakage_rows", [])))
    for gate_row in src.get("gate_rows", []):
        rows.append({
            "dimension": "历史强行业选择",
            "metric": gate_row.get("metric", ""),
            "current": gate_row.get("current", ""),
            "required": f"{gate_row.get('operator', '')} {gate_row.get('required', '')}".strip(),
            "status": gate_row.get("status", ""),
            "severity": "hard_gate" if gate_row.get("status") == "fail" else "evidence",
            "evidence_path": "outputs/industry_rebound_leader_selection_v4_72/debug/evaluation_gate_audit.csv",
            "interpretation": interpretation_for_gate(gate_row.get("metric", ""), gate_row.get("status", "")),
        })
    rows.extend(statistical_reliability_rows(src.get("strategy_rows", [])))
    rows.extend(weak_year_risk_rows(src.get("failure_rows", [])))
    rows.extend(failure_concentration_rows(src.get("failure_rows", [])))
    rows.extend(latest_candidate_failure_exposure_rows(src.get("failure_rows", []), src.get("latest_rows", [])))
    rows.extend(improvement_check_rows(leader))
    rows.extend(tradeable_leader_rows(src.get("tradeable_leader", {})))
    rows.extend(quarantine_overlay_rows(src.get("quarantine", {})))
    rows.extend(random_baseline_rows(src.get("random_baseline", {})))
    rows.extend(bootstrap_rows(src.get("bootstrap", {})))
    rows.extend(state_guardrail_rows(src.get("state_guardrail", {})))

    settlement = src.get("settlement", {})
    settled = int_value(settlement.get("settled_rows"))
    pending = int_value(settlement.get("pending_rows"))
    rows.append(row(
        "未来新增样本前推",
        "settled_forward_rows",
        str(settled),
        ">= 10",
        "pending" if pending else ("pass" if settled >= 10 else "fail"),
        "hard_gate",
        "outputs/audit/v4_72_forward_return_settlement/run_summary.json",
        "前推样本未到退出日，不能用未来收益提前证明有效。" if pending else "已可纳入前推收益评价。",
    ))
    rows.append(row(
        "未来新增样本前推",
        "missing_price_rows",
        str(int_value(settlement.get("missing_price_rows"))),
        "= 0",
        "pass" if int_value(settlement.get("missing_price_rows")) == 0 else "fail",
        "data_quality",
        "outputs/audit/v4_72_forward_return_settlement/run_summary.json",
        "结算价格缺失会污染真实前推评价。",
    ))

    fund_flow = src.get("fund_flow", {})
    fund_flow_gate_fail = int_value(fund_flow.get("overlay_gate_fail_count", fund_flow.get("missing_overlay_count")))
    pre_trade = src.get("pre_trade", {})
    entry_readiness = src.get("entry_readiness", {})
    latest_count = int_value(leader.get("latest_candidate_count"))
    carrier_blocked = str(pre_trade.get("skip_if_unresolved_industries", ""))
    rows.append(row(
        "真实可交易辅助",
        "reviewable_carrier_industry_count",
        f"reviewable={int_value(leader.get('reviewable_carrier_industry_count'))}/{latest_count}; structural_blocked={int_value(pre_trade.get('structural_blocked_count'))}; blocked={carrier_blocked}",
        f"= {latest_count}",
        "pass" if latest_count and int_value(leader.get("reviewable_carrier_industry_count")) >= latest_count else "fail",
        "hard_gate",
        "outputs/industry_rebound_leader_selection_v4_72/run_summary.json",
        "候选行业必须都有流动性、折溢价和跟踪都可复核的载体；结构阻断名单要逐一清理，不能只看关键词映射数量。",
    ))
    rows.append(row(
        "真实可交易辅助",
        "fund_flow_overlay_coverage",
        f"available={int_value(fund_flow.get('available_overlay_count'))}/{int_value(fund_flow.get('candidate_count'))}; proxy={int_value(fund_flow.get('proxy_overlay_count'))}; missing={int_value(fund_flow.get('missing_overlay_count'))}",
        "10/10",
        "pass" if fund_flow_gate_fail == 0 else "fail",
        "soft_gate",
        "outputs/audit/v4_72_candidate_fund_flow_overlay/run_summary.json",
        "资金流只做当前观察；代理或缺失都会降低人工复核质量。",
    ))
    rows.append(row(
        "盘前安全边界",
        "system_position_cap_all_zero",
        str(bool(pre_trade.get("system_position_cap_all_zero"))).lower(),
        "true while research_only",
        "pass" if pre_trade.get("system_position_cap_all_zero") is True else "fail",
        "safety_gate",
        "outputs/audit/v4_72_pre_trade_review_packet/run_summary.json",
        "当前未验证阶段，系统仓位上限必须为 0。",
    ))
    allowed_entry_count = int_value(entry_readiness.get("allowed_entry_count"))
    rows.append(row(
        "入场就绪",
        "allowed_entry_count",
        str(allowed_entry_count),
        "> 0",
        "pass" if allowed_entry_count > 0 else "fail",
        "hard_gate",
        "outputs/audit/v4_72_entry_readiness/run_summary.json",
        "没有任何候选通过入场门禁时，不能把研究候选升级为可执行入场。",
    ))
    projected_blocked = int_value(entry_readiness.get("projected_entry_blocked_count"))
    rows.append(row(
        "入场就绪",
        "projected_entry_blocked_count",
        str(projected_blocked),
        "= 0",
        "pass" if projected_blocked == 0 else "fail",
        "hard_gate",
        "outputs/audit/v4_72_entry_readiness/run_summary.json",
        "计划入场日若预计仍被阻断，系统必须保持人工观察，不允许自动执行。",
    ))
    rows.append(row(
        "生产就绪",
        "production_ready",
        str(bool(pre_trade.get("production_ready"))).lower(),
        "true",
        "pass" if pre_trade.get("production_ready") is True and not failing_or_pending(rows) else "fail",
        "hard_gate",
        "outputs/audit/v4_72_pre_trade_review_packet/run_summary.json",
        "仍有失败或待结算闸门，不能进入自动执行。",
    ))
    return sorted(rows, key=lambda x: status_rank(x["status"]))


def feature_timing_rows(leakage_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not leakage_rows:
        return [row(
            "特征时点与泄漏审计",
            "feature_timing_audit",
            "missing",
            "all pass",
            "fail",
            "hard_gate",
            "outputs/industry_rebound_leader_selection_v4_72/debug/leakage_audit.csv",
            "缺少强行业排序的时点和泄漏审计，不能证明排序只使用入场前可见信息。",
        )]
    failed = [item for item in leakage_rows if item.get("status") != "pass"]
    current = "; ".join(f"{item.get('item', '')}={item.get('status', '')}" for item in leakage_rows)
    return [row(
        "特征时点与泄漏审计",
        "feature_timing_audit",
        current,
        "all pass",
        "pass" if not failed else "fail",
        "hard_gate",
        "outputs/industry_rebound_leader_selection_v4_72/debug/leakage_audit.csv",
        "强行业排序必须只使用 signal_date 及以前可见特征；future_return、入场后收益、结算字段只能用于评价，不能用于排序。",
    )]


def tradeable_leader_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    if not summary:
        return [
            row(
                "真实可交易强行业验证",
                "tradeable_leader_evidence_pass_count",
                "missing",
                "> 0",
                "fail",
                "hard_gate",
                "outputs/audit/v4_72_tradeable_research_blocked_leader/run_summary.json",
                "缺少结构通过池强反弹审计，不能判断真实可交易候选池是否能选出更强反弹行业。",
            )
        ]
    observations = int_value(summary.get("forward_observation_count"))
    settled = int_value(summary.get("forward_settled_count"))
    passed = int_value(summary.get("evidence_pass_count"))
    return [
        row(
            "真实可交易强行业验证",
            "tradeable_leader_evidence_pass_count",
            f"pass={passed}/{int_value(summary.get('target_count'))}; best={summary.get('best_context_industry', '')}; top_quintile={summary.get('best_context_top_quintile_hit_rate', '')}",
            "> 0",
            "pass" if passed > 0 else "fail",
            "hard_gate",
            "outputs/audit/v4_72_tradeable_research_blocked_leader/run_summary.json",
            "结构通过池更接近真实可交易候选；如果这里没有行业通过证据门槛，就不能说系统已经能挑出反弹更强的行业。",
        ),
        row(
            "真实可交易强行业验证",
            "tradeable_leader_forward_settlement",
            f"settled={settled}/{observations}; status={summary.get('forward_observation_status', '')}; exit={summary.get('forward_planned_exit_date', '')}",
            "all settled and reviewed",
            "pending" if observations and settled < observations else ("pass" if observations else "fail"),
            "hard_gate",
            "outputs/audit/v4_72_tradeable_research_blocked_leader/debug/forward_tradeable_leader_checklist.csv",
            "结构通过池前推样本必须到退出日后结算，不能用未发生的未来收益提前证明有效。",
        ),
    ]


def quarantine_overlay_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    if not summary:
        return [row(
            "当前候选风险",
            "quarantine_replacement_observation_count",
            "missing",
            "> 0",
            "fail",
            "hard_gate",
            "outputs/audit/v4_72_rebound_leader_quarantine_overlay/run_summary.json",
            "缺少历史失败隔离后的替补观察池，当前候选踩中失败行业时没有可复核替代名单。",
        )]
    count = int_value(summary.get("replacement_observation_count"))
    return [row(
        "当前候选风险",
        "quarantine_replacement_observation_count",
        f"{count}; top={summary.get('top_replacement_industries', '')}",
        "> 0",
        "pass" if count > 0 else "fail",
        "hard_gate",
        "outputs/audit/v4_72_rebound_leader_quarantine_overlay/top_candidates.csv",
        "替补池只解决“踩中历史失败行业后看什么”，不证明替补行业未来会更强。",
    )]


def random_baseline_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    if not summary:
        return [row(
            "统计可靠性",
            "random_baseline_gap",
            "missing",
            "Top20%缺口=0；正年份缺口=0；相对胜率缺口=0",
            "fail",
            "hard_gate",
            "outputs/audit/v4_72_rebound_leader_random_baseline_audit/run_summary.json",
            "缺少随机基准缺口审计，不能说明强反弹行业选择相对随机的真实距离。",
        )]
    top_gap = int_value(summary.get("top_quintile_success_gap"))
    year_gap = int_value(summary.get("positive_year_gap"))
    win_gap = int_value(summary.get("relative_win_success_gap"))
    return [row(
        "统计可靠性",
        "random_baseline_gap",
        f"Top20%缺口={top_gap}; 正年份缺口={year_gap}; 相对胜率缺口={win_gap}; 逐事件随机={summary.get('empirical_random_top_quintile_current', '')}; 分年失败={summary.get('year_random_fail_years', '')}; {summary.get('final_verdict', '')}",
        "Top20%缺口=0；正年份缺口=0；相对胜率缺口=0",
        "pass" if top_gap == 0 and year_gap == 0 and win_gap == 0 else "fail",
        "hard_gate",
        "outputs/audit/v4_72_rebound_leader_random_baseline_audit/run_summary.json",
        "相对随机有方向性不等于已过强反弹门槛；必须看还差几个事件和几年。",
    )]


def bootstrap_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    if not summary:
        return [row(
            "统计可靠性",
            "event_bootstrap_gate",
            "missing",
            "bootstrap_passes_gate=true",
            "fail",
            "hard_gate",
            "outputs/audit/v4_72_rebound_leader_bootstrap_audit/run_summary.json",
            "缺少事件级 bootstrap，不能判断历史窗口重采样后强行业选择是否稳定。",
        )]
    return [row(
        "统计可靠性",
        "event_bootstrap_gate",
        f"pass={summary.get('pass_count', '')}; fail={summary.get('fail_count', '')}; failed={summary.get('failed_metrics', '')}; {summary.get('final_verdict', '')}",
        "bootstrap_passes_gate=true",
        "pass" if summary.get("bootstrap_passes_gate") is True else "fail",
        "hard_gate",
        "outputs/audit/v4_72_rebound_leader_bootstrap_audit/run_summary.json",
        "事件级重采样下 Top20% 命中和正年份仍要稳定，否则不能升级为强行业选择证据。",
    )]


def state_guardrail_rows(summary: dict[str, Any]) -> list[dict[str, str]]:
    if not summary:
        return [row(
            "分状态风险",
            "state_guardrail",
            "missing",
            "fail_count=0 and low_data_count=0",
            "fail",
            "hard_gate",
            "outputs/audit/v4_72_rebound_leader_state_guardrail/run_summary.json",
            "缺少状态护栏审计，不能判断哪些市场状态下强行业排序可用于人工复核。",
        )]
    fail_count = int_value(summary.get("fail_count"))
    low_data_count = int_value(summary.get("low_data_count"))
    return [row(
        "分状态风险",
        "state_guardrail",
        f"fail={fail_count}; low_data={low_data_count}; failed={summary.get('failed_buckets', '')}; low_data_buckets={summary.get('low_data_buckets', '')}",
        "fail_count=0 and low_data_count=0",
        "pass" if fail_count == 0 and low_data_count == 0 else "fail",
        "hard_gate",
        "outputs/audit/v4_72_rebound_leader_state_guardrail/run_summary.json",
        "失败状态桶内强行业排序只能只观察；不能用整体均值提高实盘信心。",
    )]


def row(dimension: str, metric: str, current: str, required: str, status: str, severity: str, evidence_path: str, interpretation: str) -> dict[str, str]:
    return {
        "dimension": dimension,
        "metric": metric,
        "current": current,
        "required": required,
        "status": status,
        "severity": severity,
        "evidence_path": evidence_path,
        "interpretation": interpretation,
    }


def interpretation_for_gate(metric: str, status: str) -> str:
    notes = {
        "top_quintile_hit_rate": "没有稳定把候选推到全行业反弹前 20%，说明强反弹识别仍不够。",
        "positive_year_rate": "分年稳定性不足，可能依赖少数年份。",
        "mean_relative_return": "平均相对收益为正只是必要条件，不足以单独证明 alpha。",
        "mean_rank_ic": "RankIC 为正说明排序有一定方向，但仍需命中率和分年稳定性确认。",
    }
    return notes.get(metric, "通过项只能作为辅助证据。" if status == "pass" else "该闸门未通过。")


def improvement_check_rows(leader: dict[str, Any]) -> list[dict[str, str]]:
    specs = [
        (
            "asof_failure_filter_passes_gate",
            "历史失败过滤",
            leader.get("asof_failure_filter_best_variant", ""),
            leader.get("asof_failure_filter_best_mean_relative_return", ""),
            leader.get("asof_failure_filter_best_top_quintile_hit_rate", ""),
            leader.get("asof_failure_filter_best_positive_year_rate", ""),
            leader.get("asof_failure_filter_passes_gate"),
            "as-of 失败行业过滤没有通过强反弹门槛，不能作为已验证改进。",
        ),
        (
            "factor_discovery_passes_gate",
            "单因子发现",
            leader.get("factor_discovery_best_factor_label", ""),
            leader.get("factor_discovery_best_mean_relative_return", ""),
            leader.get("factor_discovery_best_top_quintile_hit_rate", ""),
            leader.get("factor_discovery_best_positive_year_rate", ""),
            leader.get("factor_discovery_passes_gate"),
            "单因子发现没有通过强反弹门槛，不能临场挑一个看起来好的因子。",
        ),
        (
            "structure_factor_passes_gate",
            "结构变化因子",
            leader.get("structure_factor_best_factor_label", ""),
            leader.get("structure_factor_best_mean_relative_return", ""),
            leader.get("structure_factor_best_top_quintile_hit_rate", ""),
            "",
            leader.get("structure_factor_passes_gate"),
            "结构变化因子没有通过强反弹门槛，不能证明能选出更强行业。",
        ),
    ]
    rows = []
    for metric, dimension, name, rel, hit, year, passed, note in specs:
        current = f"{name}; relative={fmt_float(rel)}; top_quintile={fmt_float(hit)}"
        if year != "":
            current += f"; positive_year={fmt_float(year)}"
        rows.append(row(
            "改进方法验证",
            metric,
            current,
            "true",
            "pass" if passed is True else "fail",
            "method_check",
            "outputs/industry_rebound_leader_selection_v4_72/run_summary.json",
            f"{dimension}：{note}",
        ))
    return rows


def statistical_reliability_rows(strategy_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not strategy_rows:
        return [
            reliability_row("top_quintile_wilson_lower_bound", "missing", f"> {TOP_QUINTILE_RANDOM_BASELINE:.2f}", "fail", "缺少策略结果，不能判断强反弹命中率是否显著高于随机前 20%。"),
            reliability_row("relative_win_wilson_lower_bound", "missing", f"> {RELATIVE_WIN_BASELINE:.2f}", "fail", "缺少策略结果，不能判断相对跑赢胜率是否显著高于 50%。"),
        ]
    best = strategy_rows[0]
    event_count = int_value(best.get("event_count"))
    return [
        reliability_check(
            "top_quintile_wilson_lower_bound",
            float_value(best.get("top_quintile_hit_rate")),
            event_count,
            TOP_QUINTILE_RANDOM_BASELINE,
            "Top 分位命中率的 95% 置信下界必须超过随机前 20%，否则不能证明稳定选出强反弹行业。",
        ),
        reliability_check(
            "relative_win_wilson_lower_bound",
            float_value(best.get("relative_win_rate")),
            event_count,
            RELATIVE_WIN_BASELINE,
            "相对跑赢胜率的 95% 置信下界必须超过 50%，否则不能证明窗口内选择结果稳定跑赢全行业平均。",
        ),
    ]


def reliability_check(metric: str, rate: float, event_count: int, baseline: float, note: str) -> dict[str, str]:
    successes = round(rate * event_count)
    lower = wilson_lower_bound(successes, event_count)
    return reliability_row(metric, f"{lower:.4f} ({successes}/{event_count})", f"> {baseline:.2f}", "pass" if lower > baseline else "fail", note)


def reliability_row(metric: str, current: str, required: str, status: str, note: str) -> dict[str, str]:
    return row(
        "统计可靠性",
        metric,
        current,
        required,
        status,
        "hard_gate",
        "outputs/industry_rebound_leader_selection_v4_72/debug/strategy_results.csv",
        note,
    )


def weak_year_risk_rows(failure_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    weak = [row for row in failure_rows if row.get("category") == "weak_year"]
    if not weak:
        return [row(
            "分年风险",
            "worst_weak_year_relative_return",
            "missing",
            f">= {MAX_WEAK_YEAR_RELATIVE_LOSS:.2f}",
            "fail",
            "hard_gate",
            "outputs/industry_rebound_leader_selection_v4_72/debug/failure_diagnosis.csv",
            "缺少弱年份诊断，不能判断强行业选择是否存在年份级反向风险。",
        )]
    worst = min(weak, key=lambda item: float_value(item.get("value")))
    value = float_value(worst.get("value"))
    return [row(
        "分年风险",
        "worst_weak_year_relative_return",
        f"{worst.get('item', '')}: {value:.4f}",
        f">= {MAX_WEAK_YEAR_RELATIVE_LOSS:.2f}",
        "pass" if value >= MAX_WEAK_YEAR_RELATIVE_LOSS else "fail",
        "hard_gate",
        "outputs/industry_rebound_leader_selection_v4_72/debug/failure_diagnosis.csv",
        "最差年份不能明显跑输全行业平均，否则说明强反弹行业选择在部分年份会系统性反向。",
    )]


def failure_concentration_rows(failure_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    repeated = [row for row in failure_rows if row.get("category") == "repeated_worst_event_industry"]
    names = [row.get("item", "") for row in repeated if row.get("item")]
    return [row(
        "失败集中度",
        "repeated_worst_event_industry_count",
        f"{len(names)}: {','.join(names[:8])}",
        "= 0",
        "pass" if not names else "fail",
        "hard_gate",
        "outputs/industry_rebound_leader_selection_v4_72/debug/failure_diagnosis.csv",
        "若同一批行业反复出现在最差事件中，说明当前排序会系统性选中反弹弱项，不能证明能找出更强行业。",
    )]


def latest_candidate_failure_exposure_rows(failure_rows: list[dict[str, str]], latest_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    repeated = {row.get("item", "") for row in failure_rows if row.get("category") == "repeated_worst_event_industry"}
    if not latest_rows:
        return [row(
            "当前候选风险",
            "latest_repeated_worst_industry_count",
            "missing",
            "= 0",
            "fail",
            "hard_gate",
            "outputs/industry_rebound_leader_selection_v4_72/top_candidates.csv",
            "缺少当前候选清单，不能判断盘前候选是否踩中历史反复失败行业。",
        )]
    exposed = [item.get("industry_name", "") for item in latest_rows if item.get("industry_name", "") in repeated]
    return [row(
        "当前候选风险",
        "latest_repeated_worst_industry_count",
        f"{len(exposed)}/{len(latest_rows)}: {','.join(exposed)}",
        "= 0",
        "pass" if not exposed else "fail",
        "hard_gate",
        "outputs/industry_rebound_leader_selection_v4_72/top_candidates.csv",
        "当前候选若包含历史反复最差事件行业，只能作为风险观察，不能作为强反弹核心候选。",
    )]


def wilson_lower_bound(successes: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0:
        return 0.0
    p = successes / trials
    denom = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
    return max(0.0, (centre - margin) / denom)


def fmt_float(value: Any) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return ""


def failing_or_pending(rows: list[dict[str, str]]) -> bool:
    return any(r["status"] in {"fail", "pending"} and r["severity"] == "hard_gate" for r in rows)


def production_ready(rows: list[dict[str, str]]) -> bool:
    return not any(r["status"] in {"fail", "pending"} for r in rows if r["severity"] == "hard_gate")


def write_outputs(rows: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "evaluation_scorecard.csv", rows)
    sources = load_sources()
    failures = failure_summary(sources.get("failure_rows", []))
    quarantine = sources.get("quarantine", {})
    random_baseline = sources.get("random_baseline", {})
    bootstrap = sources.get("bootstrap", {})
    state_guardrail = sources.get("state_guardrail", {})
    summary = {
        "version": "v4_72_rebound_leader_evaluation_scorecard_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scorecard_rows": len(rows),
        "pass_count": sum(r["status"] == "pass" for r in rows),
        "fail_count": sum(r["status"] == "fail" for r in rows),
        "pending_count": sum(r["status"] == "pending" for r in rows),
        "hard_gate_fail_count": sum(r["status"] == "fail" and r["severity"] == "hard_gate" for r in rows),
        "hard_gate_fail_metrics": hard_gate_fail_metrics(rows),
        "alpha_hard_gate_fail_count": len(alpha_hard_gate_fail_rows(rows)),
        "alpha_hard_gate_fail_dimensions": alpha_hard_gate_fail_dimensions(rows),
        "alpha_hard_gate_fail_metrics": alpha_hard_gate_fail_metrics(rows),
        "alpha_failure_action_summary": alpha_failure_action_summary(rows),
        "live_rebound_leader_guardrails": rebound_leader_guardrails(),
        "quarantine_replacement_observation_count": quarantine.get("replacement_observation_count", 0),
        "quarantine_top_replacement_industries": quarantine.get("top_replacement_industries", ""),
        "quarantine_auto_execution_allowed": quarantine.get("auto_execution_allowed", False),
        "random_baseline_top_quintile_success_gap": random_baseline.get("top_quintile_success_gap", ""),
        "random_baseline_positive_year_gap": random_baseline.get("positive_year_gap", ""),
        "random_baseline_relative_win_success_gap": random_baseline.get("relative_win_success_gap", ""),
        "random_baseline_empirical_top_quintile_current": random_baseline.get("empirical_random_top_quintile_current", ""),
        "random_baseline_empirical_top_quintile_z_score": random_baseline.get("empirical_random_top_quintile_z_score", ""),
        "random_baseline_year_fail_count": random_baseline.get("year_random_fail_count", ""),
        "random_baseline_year_fail_years": random_baseline.get("year_random_fail_years", ""),
        "random_baseline_final_verdict": random_baseline.get("final_verdict", ""),
        "bootstrap_failed_metrics": bootstrap.get("failed_metrics", ""),
        "bootstrap_final_verdict": bootstrap.get("final_verdict", ""),
        "state_guardrail_failed_buckets": state_guardrail.get("failed_buckets", ""),
        "state_guardrail_final_verdict": state_guardrail.get("final_verdict", ""),
        "hard_gate_pending_count": sum(r["status"] == "pending" and r["severity"] == "hard_gate" for r in rows),
        **failures,
        "production_ready": production_ready(rows),
        "auto_execution_allowed": False,
        "final_verdict": "尚未证明能稳定找到反弹更强行业；前推样本未结算，生产就绪为 false。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.72 强反弹行业评价记分卡",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 评价项：{summary['scorecard_rows']}",
        f"- 通过：{summary['pass_count']}",
        f"- 失败：{summary['fail_count']}",
        f"- 待结算：{summary['pending_count']}",
        f"- 硬闸门失败：{summary['hard_gate_fail_count']}",
        f"- 硬闸门失败项：{summary.get('hard_gate_fail_metrics', '')}",
        f"- Alpha 硬闸门失败：{summary.get('alpha_hard_gate_fail_count', 0)}",
        f"- Alpha 失败类别：{summary.get('alpha_hard_gate_fail_dimensions', '')}",
        f"- Alpha 失败项：{summary.get('alpha_hard_gate_fail_metrics', '')}",
        f"- Alpha 修复动作：{summary.get('alpha_failure_action_summary', '')}",
        f"- 风险隔离替补池：{summary.get('quarantine_replacement_observation_count', 0)}",
        f"- 替补观察行业：{summary.get('quarantine_top_replacement_industries', '')}",
        f"- 随机基准缺口：Top20%={summary.get('random_baseline_top_quintile_success_gap', '')}；正年份={summary.get('random_baseline_positive_year_gap', '')}；相对胜率={summary.get('random_baseline_relative_win_success_gap', '')}",
        f"- 逐事件随机基准：{summary.get('random_baseline_empirical_top_quintile_current', '')}",
        f"- 分年随机基准失败：{summary.get('random_baseline_year_fail_count', '')}；{summary.get('random_baseline_year_fail_years', '')}",
        f"- 随机基准结论：{summary.get('random_baseline_final_verdict', '')}",
        f"- Bootstrap 失败项：{summary.get('bootstrap_failed_metrics', '')}",
        f"- Bootstrap 结论：{summary.get('bootstrap_final_verdict', '')}",
        f"- 状态护栏失败桶：{summary.get('state_guardrail_failed_buckets', '')}",
        f"- 状态护栏结论：{summary.get('state_guardrail_final_verdict', '')}",
        f"- 硬闸门待结算：{summary['hard_gate_pending_count']}",
        f"- 弱年份数：{summary.get('weak_year_count', 0)}",
        f"- 最差正窗口失败事件数：{summary.get('worst_positive_window_failure_count', 0)}",
        f"- 弱年份：{summary.get('weak_years', '')}",
        f"- 生产就绪：`{str(summary['production_ready']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        to_markdown(rows),
        "",
        "边界：该记分卡评价的是“窗口内能否选出反弹更强行业”，不是买入/卖出指令。",
    ])


def to_markdown(rows: list[dict[str, str]]) -> str:
    cols = ["dimension", "metric", "current", "required", "status", "severity", "interpretation"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for item in rows:
        lines.append("| " + " | ".join(str(item.get(col, "")).replace("\n", " ") for col in cols) + " |")
    return "\n".join(lines)


def hard_gate_fail_metrics(rows: list[dict[str, str]]) -> str:
    return ",".join(row["metric"] for row in rows if row["status"] == "fail" and row["severity"] == "hard_gate")


def alpha_hard_gate_fail_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        row for row in rows
        if row["status"] == "fail"
        and row["severity"] == "hard_gate"
        and row["dimension"] in ALPHA_HARD_GATE_DIMENSIONS
    ]


def alpha_hard_gate_fail_metrics(rows: list[dict[str, str]]) -> str:
    return ",".join(row["metric"] for row in alpha_hard_gate_fail_rows(rows))


def alpha_hard_gate_fail_dimensions(rows: list[dict[str, str]]) -> str:
    out = []
    for row in alpha_hard_gate_fail_rows(rows):
        if row["dimension"] not in out:
            out.append(row["dimension"])
    return ",".join(out)


def alpha_failure_action_summary(rows: list[dict[str, str]]) -> str:
    return "；".join(
        f"{dimension}={ALPHA_FAILURE_ACTIONS.get(dimension, '继续前推验证')}"
        for dimension in alpha_hard_gate_fail_dimensions(rows).split(",")
        if dimension
    )


def rebound_leader_guardrails() -> dict[str, Any]:
    return dict(REBOUND_LEADER_GUARDRAILS)


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


def float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def failure_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    weak_years = [row.get("item", "") for row in rows if row.get("category") == "weak_year"]
    positive_window_failures = [
        row for row in rows
        if row.get("category") == "worst_event" and "industry_selection_failed_in_positive_window" in row.get("evidence", "")
    ]
    return {
        "weak_year_count": len(weak_years),
        "weak_years": ",".join(weak_years),
        "worst_positive_window_failure_count": len(positive_window_failures),
    }


def status_rank(status: str) -> int:
    return {"fail": 0, "pending": 1, "pass": 2}.get(status, 9)


def self_check() -> None:
    with tempfile.TemporaryDirectory():
        rows = build_scorecard({
            "gate_rows": [{"metric": "top_quintile_hit_rate", "current": "0.28", "operator": ">=", "required": "0.3", "status": "fail"}],
            "leakage_rows": [{"item": "ranking_inputs_exclude_future_labels", "status": "pass", "evidence": "checked"}],
            "latest_rows": [{"industry_name": "保险Ⅱ"}],
            "strategy_rows": [{"event_count": "57", "top_quintile_hit_rate": "0.2807017543859649"}],
            "failure_rows": [{"category": "weak_year", "item": "2025"}, {"category": "worst_event", "item": "x", "evidence": "type=industry_selection_failed_in_positive_window"}],
            "settlement": {"settled_rows": 0, "pending_rows": 1, "missing_price_rows": 0},
            "leader": {"latest_candidate_count": 1, "reviewable_carrier_industry_count": 0, "asof_failure_filter_passes_gate": False, "factor_discovery_passes_gate": False, "structure_factor_passes_gate": False},
            "fund_flow": {"candidate_count": 1, "available_overlay_count": 0, "proxy_overlay_count": 1, "missing_overlay_count": 0, "overlay_gate_fail_count": 1},
            "pre_trade": {"system_position_cap_all_zero": True, "production_ready": False, "structural_blocked_count": 1, "skip_if_unresolved_industries": "乘用车"},
            "entry_readiness": {"allowed_entry_count": 0, "projected_entry_blocked_count": 1},
            "random_baseline": {"top_quintile_success_gap": 2, "positive_year_gap": 2, "relative_win_success_gap": 0, "final_verdict": "强行业选择相对随机有方向性，但仍未过硬门槛。"},
            "bootstrap": {"bootstrap_passes_gate": False, "pass_count": 2, "fail_count": 2, "failed_metrics": "top_quintile_hit_rate,positive_year_rate", "final_verdict": "事件级 bootstrap 未通过全部强反弹行业稳定性门槛。"},
            "state_guardrail": {"fail_count": 1, "low_data_count": 0, "failed_buckets": "stress_level:低/中压力", "final_verdict": "存在失败或样本不足状态桶。"},
        })
        assert any(r["metric"] == "top_quintile_hit_rate" and r["status"] == "fail" for r in rows)
        assert any(r["metric"] == "feature_timing_audit" and r["status"] == "pass" for r in rows)
        assert feature_timing_rows([])[0]["status"] == "fail"
        assert any(r["metric"] == "top_quintile_wilson_lower_bound" and r["status"] == "fail" for r in rows)
        assert any(r["metric"] == "worst_weak_year_relative_return" and r["status"] == "pass" for r in rows)
        assert any(r["metric"] == "repeated_worst_event_industry_count" and r["status"] == "pass" for r in rows)
        assert any(r["metric"] == "latest_repeated_worst_industry_count" and r["status"] == "pass" for r in rows)
        assert any(r["metric"] == "asof_failure_filter_passes_gate" and r["status"] == "fail" for r in rows)
        assert any(r["metric"] == "settled_forward_rows" and r["status"] == "pending" for r in rows)
        assert any(r["metric"] == "allowed_entry_count" and r["status"] == "fail" for r in rows)
        assert any(r["metric"] == "reviewable_carrier_industry_count" and "blocked=乘用车" in r["current"] for r in rows)
        assert any(r["metric"] == "random_baseline_gap" and r["status"] == "fail" for r in rows)
        assert any(r["metric"] == "event_bootstrap_gate" and r["status"] == "fail" for r in rows)
        assert any(r["metric"] == "state_guardrail" and r["status"] == "fail" for r in rows)
        assert production_ready(rows) is False
        assert "top_quintile_hit_rate" in hard_gate_fail_metrics(rows)
        assert "top_quintile_hit_rate" in alpha_hard_gate_fail_metrics(rows)
        assert "random_baseline_gap" in alpha_hard_gate_fail_metrics(rows)
        assert "event_bootstrap_gate" in alpha_hard_gate_fail_metrics(rows)
        assert "state_guardrail" in alpha_hard_gate_fail_metrics(rows)
        guardrails = rebound_leader_guardrails()
        assert guardrails["status"] == "locked_for_research_only"
        assert len(guardrails["forbidden_runtime_overrides"]) == 4
        assert "历史强行业选择" in alpha_hard_gate_fail_dimensions(rows)
        assert "历史强行业选择=提高Top分位命中率和正年份率" in alpha_failure_action_summary(rows)
        assert failure_summary([{"category": "weak_year", "item": "2025"}])["weak_years"] == "2025"
        assert weak_year_risk_rows([{"category": "weak_year", "item": "2025", "value": "-0.047"}])[0]["status"] == "fail"
        assert failure_concentration_rows([{"category": "repeated_worst_event_industry", "item": "煤炭开采"}])[0]["status"] == "fail"
        assert latest_candidate_failure_exposure_rows([{"category": "repeated_worst_event_industry", "item": "白酒Ⅱ"}], [{"industry_name": "白酒Ⅱ"}])[0]["status"] == "fail"
    print("self_check=pass")


if __name__ == "__main__":
    main()
