#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "current_phase_readiness_check"
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
    parser = argparse.ArgumentParser(description="Audit current-stage practical research-assistant readiness.")
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
    print(f"current_phase_ready={all_pass(rows)}")


def load_sources() -> dict[str, Any]:
    return {
        "v471": read_json(ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "run_summary.json"),
        "v471_packet": read_json(ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "live_decision_packet.json"),
        "v471_guardrails": read_json(ROOT / "outputs" / "audit" / "v4_71_live_guardrail_playbook" / "run_summary.json"),
        "v471_leakage": read_rows(ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "leakage_audit.csv"),
        "v472": read_json(ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "run_summary.json"),
        "v472_leakage": read_rows(ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "leakage_audit.csv"),
        "pretrade": read_json(ROOT / "outputs" / "audit" / "v4_72_pre_trade_review_packet" / "run_summary.json"),
        "entry": read_json(ROOT / "outputs" / "audit" / "v4_72_entry_readiness" / "run_summary.json"),
        "operator": read_json(ROOT / "outputs" / "audit" / "v4_72_pre_entry_operator_checklist" / "run_summary.json"),
        "remediation": read_json(ROOT / "outputs" / "audit" / "v4_72_remediation_queue" / "run_summary.json"),
        "scorecard": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_evaluation_scorecard" / "run_summary.json"),
        "scorecard_rows": read_rows(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_evaluation_scorecard" / "debug" / "evaluation_scorecard.csv"),
        "random_baseline": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_random_baseline_audit" / "run_summary.json"),
        "bootstrap": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_bootstrap_audit" / "run_summary.json"),
        "state_guardrail": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_state_guardrail" / "run_summary.json"),
        "settlement": read_json(ROOT / "outputs" / "audit" / "v4_72_forward_return_settlement" / "run_summary.json"),
        "settlement_schedule": read_rows(ROOT / "outputs" / "audit" / "v4_72_forward_return_settlement" / "debug" / "settlement_schedule.csv"),
        "tradeable_leader": read_json(ROOT / "outputs" / "audit" / "v4_72_tradeable_research_blocked_leader" / "run_summary.json"),
        "fund_flow": read_json(ROOT / "outputs" / "audit" / "v4_72_candidate_fund_flow_overlay" / "run_summary.json"),
        "alternatives": read_json(ROOT / "outputs" / "audit" / "v4_72_carrier_alternative_tracking" / "run_summary.json"),
    }


def build_rows(src: dict[str, Any]) -> list[dict[str, str]]:
    return [
        robustness_framework_row(src),
        no_leakage_row(src),
        carrier_mapping_row(src),
        pre_entry_workflow_row(src),
        rebound_leader_scorecard_row(src),
        research_only_row(src),
        forward_sample_row(src),
        user_decision_clarity_row(src),
    ]


def robustness_framework_row(src: dict[str, Any]) -> dict[str, str]:
    v471 = src["v471"]
    guardrails = src["v471_guardrails"]
    files = [
        "parameter_perturbation.csv",
        "cooldown_sensitivity.csv",
        "annual_breakdown.csv",
        "market_state_breakdown.csv",
        "year_state_breakdown.csv",
        "parameter_failure_diagnosis.csv",
        "frozen_policy.json",
    ]
    missing = missing_debug_files(ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug", files)
    ok = not missing and int_value(guardrails.get("row_count")) > 0
    evidence = (
        f"missing={','.join(missing) or 'none'}; "
        f"guardrail_rows={guardrails.get('row_count', '')}; "
        f"forbidden_runtime_override_count={guardrails.get('forbidden_runtime_override_count', '')}; "
        f"insufficient_independent_clusters_count={guardrails.get('insufficient_independent_clusters_count', '')}; "
        f"sparse_state_not_production_evidence_count={guardrails.get('sparse_state_not_production_evidence_count', '')}; "
        f"blocking_issues={v471.get('blocking_issue_count', '')}; "
        f"parameter_failed_variants={v471.get('parameter_failed_variants', '')}"
    )
    return row(
        "反弹窗口稳健性复核框架",
        pass_fail(ok),
        evidence,
        "必须存在参数扰动、冷却期、分年、分状态、失败归因和禁止临场改参数护栏。",
        "outputs/industry_rebound_window_v4_71_robustness_live_audit/debug; outputs/audit/v4_71_live_guardrail_playbook",
        "框架已存在；失败项继续作为风险标签和后续前推验证对象。",
    )


def no_leakage_row(src: dict[str, Any]) -> dict[str, str]:
    v471_pass = all_status_pass(src["v471_leakage"])
    v472_pass = all_status_pass(src["v472_leakage"])
    return row(
        "不偷看未来与特征时点审计",
        pass_fail(v471_pass and v472_pass),
        f"v471_leakage_pass={v471_pass}; v472_leakage_pass={v472_pass}; v472_rows={len(src['v472_leakage'])}",
        "V4.71 和 V4.72 的 leakage/as-of 审计必须全通过。",
        "outputs/industry_rebound_window_v4_71_robustness_live_audit/debug/leakage_audit.csv; outputs/industry_rebound_leader_selection_v4_72/debug/leakage_audit.csv",
        "若任一泄漏审计失败，停止使用相关报告做盘前参考。",
    )


def carrier_mapping_row(src: dict[str, Any]) -> dict[str, str]:
    packet = src["v471_packet"]
    pretrade = src["pretrade"]
    entry = src["entry"]
    alternatives = src["alternatives"]
    ok = (
        packet.get("carrier_review_scope") == "broad_market_reference_only_not_industry_execution"
        and "宽基/全市场 ETF" in "；".join(packet.get("prohibited_use", []))
        and int_value(pretrade.get("review_rows")) > 0
        and int_value(entry.get("core_tradeable_gate_pass_count")) > 0
        and int_value(alternatives.get("usable_alternative_industry_count", alternatives.get("usable_alternative_count"))) > 0
    )
    evidence = (
        f"scope={packet.get('carrier_review_scope', '')}; "
        f"industry_packet={packet.get('industry_carrier_review_packet', '')}; "
        f"entry_packet={packet.get('industry_entry_readiness_packet', '')}; "
        f"review_rows={pretrade.get('review_rows', '')}; "
        f"structural_blocked={pretrade.get('structural_blocked_count', '')}; "
        f"core_tradeable_gate_pass={entry.get('core_tradeable_gate_pass_count', '')}; "
        f"alternative_industries={alternatives.get('usable_alternative_industry_count', alternatives.get('usable_alternative_count', ''))}"
    )
    return row(
        "真实可交易载体映射体系",
        pass_fail(ok),
        evidence,
        "必须区分行业指数、行业 ETF、宽基 ETF 和代理载体，且宽基/弱映射不能替代行业入场。",
        "outputs/audit/v4_72_pre_trade_review_packet; outputs/audit/v4_72_entry_readiness; outputs/industry_rebound_window_v4_71_robustness_live_audit/debug/live_decision_packet.json",
        "继续补弱跟踪和代理资金流行业，但当前已能阻止宽基或弱映射被误用。",
    )


def pre_entry_workflow_row(src: dict[str, Any]) -> dict[str, str]:
    entry = src["entry"]
    operator = src["operator"]
    remediation = src["remediation"]
    action_count = int_value(entry.get("pre_entry_action_checklist_rows"))
    classified = int_value(entry.get("pre_entry_action_skip_count")) + int_value(entry.get("pre_entry_action_observe_count")) + int_value(entry.get("pre_entry_action_manual_review_only_count"))
    ok = (
        action_count > 0
        and classified == action_count
        and int_value(operator.get("row_count")) > 0
        and int_value(remediation.get("queue_rows")) > 0
        and operator.get("auto_execution_allowed") is False
        and remediation.get("auto_execution_allowed") is False
    )
    evidence = (
        f"action_rows={action_count}; classified={classified}; "
        f"skip={entry.get('pre_entry_action_skip_count', '')}; observe={entry.get('pre_entry_action_observe_count', '')}; manual_review_only={entry.get('pre_entry_action_manual_review_only_count', '')}; "
        f"operator_rows={operator.get('row_count', '')}; operator_p0={operator.get('p0_count', '')}; "
        f"remediation_rows={remediation.get('queue_rows', '')}; remediation_p0={remediation.get('p0_count', '')}; "
        f"entry_allowed={entry.get('allowed_entry_count', '')}; auto={entry.get('auto_execution_allowed', '')}"
    )
    return row(
        "盘前实盘辅助流程",
        pass_fail(ok),
        evidence,
        "必须覆盖候选、载体、资金流、跟踪、流动性、历史失败、动作分类和 P0/P1/P2 清单。",
        "outputs/audit/v4_72_entry_readiness; outputs/audit/v4_72_pre_entry_operator_checklist; outputs/audit/v4_72_remediation_queue",
        "当前流程可用于盘前人工复核；P0 项只表示阻断和补证，不表示入场。",
    )


def rebound_leader_scorecard_row(src: dict[str, Any]) -> dict[str, str]:
    metrics = {item.get("metric", "") for item in src["scorecard_rows"]}
    required_metrics = {
        "top_quintile_hit_rate",
        "positive_year_rate",
        "top_quintile_wilson_lower_bound",
        "state_guardrail",
        "event_bootstrap_gate",
        "random_baseline_gap",
        "tradeable_leader_evidence_pass_count",
    }
    v472 = src["v472"]
    ok = (
        required_metrics.issubset(metrics)
        and int_value(src["scorecard"].get("scorecard_rows")) > 0
        and "rank_ic" in " ".join(src["scorecard"].keys()).lower() + " " + " ".join(v472.keys()).lower()
        and int_value(src["random_baseline"].get("row_count")) > 0
        and int_value(src["bootstrap"].get("row_count")) > 0
        and int_value(src["state_guardrail"].get("row_count")) > 0
        and int_value(src["tradeable_leader"].get("forward_observation_count")) >= 0
    )
    missing = sorted(required_metrics - metrics)
    evidence = (
        f"missing_metrics={','.join(missing) or 'none'}; "
        f"scorecard_rows={src['scorecard'].get('scorecard_rows', '')}; "
        f"best_mean_relative_return={v472.get('best_mean_relative_return', '')}; "
        f"best_relative_win_rate={v472.get('best_relative_win_rate', '')}; "
        f"best_mean_rank_ic={v472.get('best_mean_rank_ic', '')}; "
        f"random_rows={src['random_baseline'].get('row_count', '')}; bootstrap_rows={src['bootstrap'].get('row_count', '')}; "
        f"state_rows={src['state_guardrail'].get('row_count', '')}; forward_observations={src['tradeable_leader'].get('forward_observation_count', '')}"
    )
    return row(
        "强反弹行业选择评价体系",
        pass_fail(ok),
        evidence,
        "必须包含相对收益、Top 分位、胜率、RankIC、分年/状态稳定性、随机基准、bootstrap 和前推入口。",
        "outputs/audit/v4_72_rebound_leader_evaluation_scorecard; outputs/audit/v4_72_rebound_leader_random_baseline_audit; outputs/audit/v4_72_rebound_leader_bootstrap_audit; outputs/audit/v4_72_tradeable_research_blocked_leader",
        "评价体系已建成；当前结果仍可失败，但失败不会被包装成 alpha。",
    )


def research_only_row(src: dict[str, Any]) -> dict[str, str]:
    summaries = [src["v471"], src["v472"], src["pretrade"], src["entry"], src["operator"], src["scorecard"], src["remediation"]]
    auto_off = all(item.get("auto_execution_allowed") in {False, None, ""} for item in summaries)
    production_off = all(item.get("production_ready") in {False, None, ""} for item in summaries)
    entry_zero = int_value(src["entry"].get("allowed_entry_count")) == 0
    ok = auto_off and production_off and entry_zero and src["v472"].get("policy_status") == "research_only"
    evidence = (
        f"auto_off={auto_off}; production_off={production_off}; allowed_entry_count={src['entry'].get('allowed_entry_count', '')}; "
        f"v472_policy_status={src['v472'].get('policy_status', '')}; live_decision={src['entry'].get('live_entry_decision', '')}"
    )
    return row(
        "research_only 与禁止自动交易",
        pass_fail(ok),
        evidence,
        "所有结论必须保持 research_only，不生成自动买入、卖出或仓位执行信号。",
        "outputs/audit/v4_72_entry_readiness/run_summary.json; outputs/industry_rebound_leader_selection_v4_72/run_summary.json",
        "继续保持系统仓位上限为 0；任何人工复核也不升级为自动指令。",
    )


def forward_sample_row(src: dict[str, Any]) -> dict[str, str]:
    schedule = src["settlement_schedule"]
    event_types = {row.get("event_type", "") for row in schedule}
    ok = {"pre_entry_refresh", "forward_settlement"}.issubset(event_types) and int_value(src["settlement"].get("pending_rows")) >= 0
    evidence = (
        f"schedule_events={','.join(sorted(event_types))}; "
        f"pending_rows={src['settlement'].get('pending_rows', '')}; settled_rows={src['settlement'].get('settled_rows', '')}; "
        f"next_action={src['settlement'].get('next_action_date', '')}:{src['settlement'].get('next_action', '')}"
    )
    return row(
        "未来真实样本前推入口",
        pass_fail(ok),
        evidence,
        "必须保留未来刷新和 forward return 结算入口，但当前阶段不因未到期而失败。",
        "outputs/audit/v4_72_forward_return_settlement/debug/settlement_schedule.csv",
        "到 2026-06-23 和 2026-07-21 后按日程更新证据等级。",
    )


def user_decision_clarity_row(src: dict[str, Any]) -> dict[str, str]:
    entry = src["entry"]
    remediation = src["remediation"]
    ok = (
        int_value(entry.get("row_count")) > 0
        and entry.get("live_entry_decision") == "no_entry_currently"
        and bool(entry.get("live_entry_action"))
        and int_value(remediation.get("queue_rows")) > 0
    )
    evidence = (
        f"live_entry_decision={entry.get('live_entry_decision', '')}; "
        f"live_entry_action={entry.get('live_entry_action', '')}; "
        f"failed_or_blocked_queue={remediation.get('queue_rows', '')}; "
        f"skip_industries={entry.get('entry_skip_rule_industries', '')}; manual_review_industries={entry.get('entry_manual_review_rule_industries', '')}"
    )
    return row(
        "用户决策解释能力",
        pass_fail(ok),
        evidence,
        "系统必须清楚说明当前能不能入场、为什么不能入场、缺什么证据、下一步怎么补证。",
        "outputs/audit/v4_72_entry_readiness/report.md; outputs/audit/v4_72_remediation_queue/report.md",
        "继续让报告优先展示可执行的跳过、只观察、仅人工复核和补证动作。",
    )


def row(requirement: str, status: str, evidence: str, required: str, path: str, action: str) -> dict[str, str]:
    return {
        "requirement": requirement,
        "status": status,
        "current_evidence": evidence,
        "required_to_complete": required,
        "evidence_path": path,
        "next_action": action,
    }


def write_outputs(rows: list[dict[str, str]], sources: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "current_phase_readiness_check.csv", rows)
    summary = {
        "version": "current_phase_readiness_check_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_as_of_date": sources.get("v471", {}).get("as_of_date", ""),
        "planned_entry_date": sources.get("v471", {}).get("planned_entry_date", ""),
        "planned_exit_date": sources.get("v471", {}).get("planned_exit_date", ""),
        "row_count": len(rows),
        "pass_count": sum(item["status"] == "pass" for item in rows),
        "fail_count": sum(item["status"] == "fail" for item in rows),
        "failed_requirements": "、".join(item["requirement"] for item in rows if item["status"] == "fail"),
        "future_forward_validation_deferred": True,
        "current_phase_ready": all_pass(rows),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "当前阶段能力建设已完成；系统保持 research_only，未来真实样本只用于后续证据等级更新。" if all_pass(rows) else "当前阶段能力建设仍有缺口；不得作为实盘辅助完成版。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# 当前阶段完成度审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- source_as_of_date：{summary['source_as_of_date']}",
        f"- 计划入场/退出：{summary['planned_entry_date']} / {summary['planned_exit_date']}",
        f"- 检查项：{summary['row_count']}",
        f"- 通过：{summary['pass_count']}",
        f"- 失败：{summary['fail_count']}",
        f"- 失败要求：{summary['failed_requirements']}",
        f"- 未来真实样本延后验证：`{str(summary['future_forward_validation_deferred']).lower()}`",
        f"- 当前阶段完成：`{str(summary['current_phase_ready']).lower()}`",
        f"- 生产可用：`{str(summary['production_ready']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        to_markdown(rows),
    ]
    return "\n".join(lines)


def to_markdown(rows: list[dict[str, str]]) -> str:
    cols = ["requirement", "status", "current_evidence", "required_to_complete", "next_action"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for item in rows:
        lines.append("| " + " | ".join(str(item.get(col, "")).replace("\n", " ") for col in cols) + " |")
    return "\n".join(lines)


def missing_debug_files(folder: Path, names: list[str]) -> list[str]:
    return [name for name in names if not (folder / name).exists()]


def all_status_pass(rows: list[dict[str, str]]) -> bool:
    return bool(rows) and all(row.get("status") == "pass" for row in rows)


def all_pass(rows: list[dict[str, str]]) -> bool:
    return all(item["status"] == "pass" for item in rows)


def pass_fail(ok: bool) -> str:
    return "pass" if ok else "fail"


def int_value(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


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


def self_check() -> None:
    rows = build_rows({
        "v471": {"as_of_date": "2026-06-19", "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21", "blocking_issue_count": 3, "parameter_failed_variants": "entry_lag_1"},
        "v471_packet": {"carrier_review_scope": "broad_market_reference_only_not_industry_execution", "industry_carrier_review_packet": "outputs/audit/v4_72_pre_trade_review_packet/top_candidates.csv", "industry_entry_readiness_packet": "outputs/audit/v4_72_entry_readiness/top_candidates.csv", "prohibited_use": ["用宽基/全市场 ETF 替代行业载体入场"]},
        "v471_guardrails": {"row_count": 1, "forbidden_runtime_override_count": 1, "insufficient_independent_clusters_count": 1, "sparse_state_not_production_evidence_count": 1},
        "v471_leakage": [{"status": "pass"}],
        "v472": {"policy_status": "research_only", "best_mean_relative_return": 0.01, "best_relative_win_rate": 0.6, "best_mean_rank_ic": 0.1, "production_ready": False, "auto_execution_allowed": False},
        "v472_leakage": [{"status": "pass"}],
        "pretrade": {"review_rows": 10, "core_manual_review_count": 3, "structural_blocked_count": 4, "production_ready": False, "auto_execution_allowed": False},
        "entry": {"row_count": 10, "core_tradeable_gate_pass_count": 3, "pre_entry_action_checklist_rows": 10, "pre_entry_action_skip_count": 4, "pre_entry_action_observe_count": 3, "pre_entry_action_manual_review_only_count": 3, "allowed_entry_count": 0, "live_entry_decision": "no_entry_currently", "live_entry_action": "只观察", "entry_skip_rule_industries": "饲料", "entry_manual_review_rule_industries": "保险Ⅱ", "production_ready": False, "auto_execution_allowed": False},
        "operator": {"row_count": 19, "p0_count": 11, "auto_execution_allowed": False, "production_ready": False},
        "remediation": {"queue_rows": 45, "p0_count": 35, "auto_execution_allowed": False, "production_ready": False},
        "scorecard": {"scorecard_rows": 35, "production_ready": False, "auto_execution_allowed": False},
        "scorecard_rows": [{"metric": name} for name in ["top_quintile_hit_rate", "positive_year_rate", "top_quintile_wilson_lower_bound", "state_guardrail", "event_bootstrap_gate", "random_baseline_gap", "tradeable_leader_evidence_pass_count"]],
        "random_baseline": {"row_count": 5},
        "bootstrap": {"row_count": 4},
        "state_guardrail": {"row_count": 7},
        "settlement": {"pending_rows": 10, "settled_rows": 0, "next_action_date": "2026-06-23", "next_action": "刷新"},
        "settlement_schedule": [{"event_type": "pre_entry_refresh"}, {"event_type": "forward_settlement"}],
        "tradeable_leader": {"forward_observation_count": 3},
        "fund_flow": {},
        "alternatives": {"usable_alternative_industry_count": 5},
    })
    assert all_pass(rows)
    assert any(item["requirement"] == "未来真实样本前推入口" and item["status"] == "pass" for item in rows)
    broken = build_rows({**{k: {} for k in load_sources()}, "v471_leakage": [{"status": "fail"}], "v472_leakage": [{"status": "pass"}]})
    assert any(item["requirement"] == "不偷看未来与特征时点审计" and item["status"] == "fail" for item in broken)
    print("self_check=pass")


if __name__ == "__main__":
    main()
