#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from research_evidence_routes import verified_forward_evidence_ready
from valuation_pit_contract import methodology_route_ready


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "rebound_leader_goal_completion_audit_v5_10"
DEBUG = OUT / "debug"

SOURCES = {
    "family_multiple_testing": ROOT / "outputs" / "audit" / "rebound_leader_historical_backtest_verdict_v4_93" / "run_summary.json",
    "experiment_ledger": ROOT / "outputs" / "audit" / "research_experiment_ledger" / "run_summary.json",
    "window_capacity": ROOT / "outputs" / "audit" / "rebound_window_expansion_capacity_audit_v4_96" / "run_summary.json",
    "window_quality": ROOT / "outputs" / "industry_rebound_leader_window_quality_v5_03" / "run_summary.json",
    "freeze": ROOT / "outputs" / "audit" / "rebound_leader_evidence_freeze_v5_04" / "run_summary.json",
    "tracker": ROOT / "outputs" / "audit" / "rebound_leader_forward_tracker_v5_05" / "run_summary.json",
    "settlement": ROOT / "outputs" / "audit" / "rebound_leader_forward_settlement_v5_06" / "run_summary.json",
    "promotion": ROOT / "outputs" / "audit" / "rebound_leader_promotion_evaluator_v5_07" / "run_summary.json",
    "detector": ROOT / "outputs" / "audit" / "rebound_leader_forward_signal_detector_v5_08" / "run_summary.json",
    "pseudo_forward": ROOT / "outputs" / "audit" / "rebound_leader_pseudo_forward_audit_v5_09" / "run_summary.json",
    "pit_universe_methodology": ROOT / "outputs" / "audit" / "pit_universe_methodology_remediation" / "run_summary.json",
    "pit_valuation_percentile": ROOT / "outputs" / "audit" / "rebound_leader_pit_valuation_percentile_audit_v5_12" / "run_summary.json",
    "early_confirmation": ROOT / "outputs" / "audit" / "rebound_leader_early_confirmation_audit_v5_13" / "run_summary.json",
    "confirmation_filter": ROOT / "outputs" / "audit" / "rebound_leader_confirmation_filter_audit_v5_14" / "run_summary.json",
    "failure_diagnosis": ROOT / "outputs" / "audit" / "rebound_leader_failure_diagnosis_v5_15" / "run_summary.json",
    "window_quality_proxy": ROOT / "outputs" / "audit" / "rebound_window_quality_proxy_audit_v5_16" / "run_summary.json",
    "phase_sample_expansion": ROOT / "outputs" / "audit" / "rebound_phase_sample_expansion_audit_v5_17" / "run_summary.json",
    "rolling_quarantine": ROOT / "outputs" / "audit" / "rebound_leader_rolling_quarantine_audit_v5_18" / "run_summary.json",
    "volume_confirmation": ROOT / "outputs" / "audit" / "rebound_leader_volume_confirmation_audit_v5_19" / "run_summary.json",
    "evidence_boundary": ROOT / "outputs" / "audit" / "rebound_leader_evidence_boundary_audit_v5_20" / "run_summary.json",
    "new_pit_source_discovery": ROOT / "outputs" / "audit" / "rebound_leader_new_pit_source_discovery_v5_21" / "run_summary.json",
    "eastmoney_fund_flow_probe": ROOT / "outputs" / "audit" / "rebound_leader_eastmoney_fund_flow_probe_v5_22" / "run_summary.json",
    "fund_flow_pit_panel": ROOT / "outputs" / "audit" / "fund_flow_pit_panel_v5_23" / "run_summary.json",
    "fund_flow_mapping_remediation": ROOT / "outputs" / "audit" / "fund_flow_mapping_remediation_v5_24" / "run_summary.json",
    "fund_flow_forward_observer": ROOT / "outputs" / "audit" / "fund_flow_forward_observer_v5_25" / "run_summary.json",
    "fund_flow_forward_entry_gate": ROOT / "outputs" / "audit" / "fund_flow_forward_entry_gate_v5_26" / "run_summary.json",
    "fund_flow_forward_settlement": ROOT / "outputs" / "audit" / "fund_flow_forward_settlement_v5_27" / "run_summary.json",
    "fund_flow_promotion_evaluator": ROOT / "outputs" / "audit" / "fund_flow_promotion_evaluator_v5_28" / "run_summary.json",
    "fund_flow_evidence_calendar": ROOT / "outputs" / "audit" / "fund_flow_evidence_calendar_v5_29" / "run_summary.json",
    "fund_flow_ledger_integrity": ROOT / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30" / "run_summary.json",
    "fund_flow_freeze_manifest": ROOT / "outputs" / "audit" / "fund_flow_evidence_freeze_manifest_v5_31" / "run_summary.json",
    "fund_flow_holding_observation": ROOT / "outputs" / "audit" / "fund_flow_holding_observation_v5_32" / "run_summary.json",
    "fund_flow_entry_price_freeze": ROOT / "outputs" / "audit" / "fund_flow_entry_price_freeze_v5_33" / "run_summary.json",
    "fund_flow_benchmark_entry_freeze": ROOT / "outputs" / "audit" / "fund_flow_benchmark_entry_freeze_v5_34" / "run_summary.json",
    "fund_flow_waiting_room": ROOT / "outputs" / "audit" / "fund_flow_waiting_room_v5_35" / "run_summary.json",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.10 completion audit for rebound-leader goal.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    source = {name: read_json(path) for name, path in SOURCES.items()}
    checks = build_checks(source)
    next_actions = build_next_actions(source)
    evidence = build_evidence_sources()
    summary = build_summary(
        checks,
        source["pit_universe_methodology"],
        promotion=source["promotion"],
    )
    write_outputs(summary, checks, next_actions, evidence)
    print(f"output_dir={OUT}")
    print(f"goal_ready={summary['goal_ready']}")
    print(f"fail_count={summary['fail_count']}")


def build_checks(source: dict[str, dict[str, Any]]) -> pd.DataFrame:
    forward_ready = true_forward_evidence_ready(source)
    methodology_ready = methodology_route_ready(source["pit_universe_methodology"], source.get("promotion", {}))
    return pd.DataFrame([
        row("研究家族多重检验", "pass" if int(source['family_multiple_testing'].get('familywise_pass_count', 0) or 0) > 0 else "rejected", f"V4.93 family_rules={source['family_multiple_testing'].get('family_rule_count')}; familywise_pass={source['family_multiple_testing'].get('familywise_pass_count')}; min_raw_p={source['family_multiple_testing'].get('minimum_raw_sign_flip_pvalue')}; min_bonferroni_p={source['family_multiple_testing'].get('minimum_bonferroni_pvalue')}; registration={source['family_multiple_testing'].get('experiment_registration_status')}", "outputs/audit/rebound_leader_historical_backtest_verdict_v4_93/report.md"),
        row("前推实验哈希账本", "pass" if source['experiment_ledger'].get('integrity_passed') and int(source['experiment_ledger'].get('experiment_count', 0) or 0) > 0 else "fail", f"experiments={source['experiment_ledger'].get('experiment_count')}; integrity={source['experiment_ledger'].get('integrity_passed')}; head={source['experiment_ledger'].get('ledger_head_hash')}", "outputs/audit/research_experiment_ledger/report.md"),
        row("反弹窗口样本池", "pass", "V4.96 找到 vol_repair 33 个独立窗口；这只证明窗口池可用。", "outputs/audit/rebound_window_expansion_capacity_audit_v4_96/report.md"),
        row("强反弹行业历史规则", "rejected", f"V5.03 passing_rule_count={source['window_quality'].get('passing_rule_count')}; can_claim={source['window_quality'].get('can_claim_strong_rebound_industries')}", "outputs/industry_rebound_leader_window_quality_v5_03/report.md"),
        row("小样本规则冻结", "pass", f"V5.04 frozen_rule_count={source['freeze'].get('frozen_rule_count')}; status={source['freeze'].get('best_status')}", "outputs/audit/rebound_leader_evidence_freeze_v5_04/report.md"),
        row("前推账本", "pass", f"V5.05 tracked_rule_count={source['tracker'].get('tracked_rule_count')}; ledger={source['tracker'].get('ledger_path')}", "outputs/audit/rebound_leader_forward_tracker_v5_05/report.md"),
        row("已结算前推样本", "rejected", f"V5.06 settled_rows={source['settlement'].get('settled_rows')}; ledger_rows={source['settlement'].get('ledger_rows')}", "outputs/audit/rebound_leader_forward_settlement_v5_06/report.md"),
        row("晋级评价", "rejected", f"V5.07 passing_rule_count={source['promotion'].get('passing_rule_count')}; forward_settled={source['promotion'].get('forward_settled_event_count')}", "outputs/audit/rebound_leader_promotion_evaluator_v5_07/report.md"),
        row("冻结后新信号", "rejected", f"V5.08 appendable_signal_count={source['detector'].get('appendable_signal_count')}; latest_signal_date={source['detector'].get('latest_signal_date')}", "outputs/audit/rebound_leader_forward_signal_detector_v5_08/report.md"),
        row("历史伪前推", "rejected", f"V5.09 passing_split_count={source['pseudo_forward'].get('passing_split_count')}; best_post_event_count={source['pseudo_forward'].get('best_post_event_count')}", "outputs/audit/rebound_leader_pseudo_forward_audit_v5_09/report.md"),
        row("历史回测证据边界", "pass", "历史回测只能淘汰或提出候选规则，不能替代冻结规则后的真实前推结算；目标完成必须依赖 V5.27/V5.28/V5.29 前推链路。", "outputs/audit/rebound_leader_goal_completion_audit_v5_10/report.md"),
        row(
            "PIT 估值与行业历史方法门",
            "pass" if methodology_ready else "fail",
            f"audit_passed={source['pit_universe_methodology'].get('audit_passed')}; remediation_complete={source['pit_universe_methodology'].get('methodology_remediation_complete')}; historical_promotion_gate={source['pit_universe_methodology'].get('promotion_gate_passed')}; true_forward_route={forward_ready}; eligible_valuation_rows={source['pit_universe_methodology'].get('promotion_eligible_valuation_row_count')}; valuation={source['pit_universe_methodology'].get('valuation_availability_status')}; classification={source['pit_universe_methodology'].get('classification_history_status')}; review_label={source['pit_universe_methodology'].get('historical_review_set_label')}",
            "outputs/audit/pit_universe_methodology_remediation/report.md",
        ),
        row("PIT 估值历史分位", "rejected", f"V5.12 passing_rule_count={source['pit_valuation_percentile'].get('passing_rule_count')}; best_mean_relative_return={source['pit_valuation_percentile'].get('best_mean_relative_return')}; best_top_quintile_hit_rate={source['pit_valuation_percentile'].get('best_top_quintile_hit_rate')}", "outputs/audit/rebound_leader_pit_valuation_percentile_audit_v5_12/report.md"),
        row("早期相对强弱确认", "rejected", f"V5.13 passing_rule_count={source['early_confirmation'].get('passing_rule_count')}; best_mean_relative_return={source['early_confirmation'].get('best_mean_relative_return')}; best_top_quintile_hit_rate={source['early_confirmation'].get('best_top_quintile_hit_rate')}", "outputs/audit/rebound_leader_early_confirmation_audit_v5_13/report.md"),
        row("确认期过滤", "rejected", f"V5.14 passing_rule_count={source['confirmation_filter'].get('passing_rule_count')}; best_mean_relative_return={source['confirmation_filter'].get('best_mean_relative_return')}; best_top_quintile_hit_rate={source['confirmation_filter'].get('best_top_quintile_hit_rate')}", "outputs/audit/rebound_leader_confirmation_filter_audit_v5_14/report.md"),
        row("强行业失败归因", "rejected", f"V5.15 failure_event_count={source['failure_diagnosis'].get('failure_event_count')}; dominant_failure_bucket={source['failure_diagnosis'].get('dominant_failure_bucket')}; verdict={source['failure_diagnosis'].get('best_status')}", "outputs/audit/rebound_leader_failure_diagnosis_v5_15/report.md"),
        row("窗口质量代理", "rejected", f"V5.16 passing_rule_count={source['window_quality_proxy'].get('passing_rule_count')}; best_mean_relative_return={source['window_quality_proxy'].get('best_mean_relative_return')}; best_top_quintile_hit_rate={source['window_quality_proxy'].get('best_top_quintile_hit_rate')}", "outputs/audit/rebound_window_quality_proxy_audit_v5_16/report.md"),
        row("压力恢复阶段扩样", "rejected", f"V5.17 passing_rule_count={source['phase_sample_expansion'].get('passing_rule_count')}; best_mean_relative_return={source['phase_sample_expansion'].get('best_mean_relative_return')}; best_top_quintile_hit_rate={source['phase_sample_expansion'].get('best_top_quintile_hit_rate')}", "outputs/audit/rebound_phase_sample_expansion_audit_v5_17/report.md"),
        row("滚动失败隔离", "rejected", f"V5.18 passing_rule_count={source['rolling_quarantine'].get('passing_rule_count')}; best_rule={source['rolling_quarantine'].get('best_rule')}; best_mean_relative_return={source['rolling_quarantine'].get('best_mean_relative_return')}", "outputs/audit/rebound_leader_rolling_quarantine_audit_v5_18/report.md"),
        row("量能确认", "rejected", f"V5.19 passing_rule_count={source['volume_confirmation'].get('passing_rule_count')}; best_feature={source['volume_confirmation'].get('best_feature')}; best_mean_relative_return={source['volume_confirmation'].get('best_mean_relative_return')}", "outputs/audit/rebound_leader_volume_confirmation_audit_v5_19/report.md"),
        row("证据边界", "rejected", f"V5.20 boundary={source['evidence_boundary'].get('evidence_boundary')}; passing_total={source['evidence_boundary'].get('passing_rule_count_total')}; verdict={source['evidence_boundary'].get('best_status')}", "outputs/audit/rebound_leader_evidence_boundary_audit_v5_20/report.md"),
        row("新 PIT 数据源", "rejected", f"V5.21 ready_source_count={source['new_pit_source_discovery'].get('historical_backtest_ready_source_count')}; boundary={source['new_pit_source_discovery'].get('evidence_boundary')}; verdict={source['new_pit_source_discovery'].get('best_status')}", "outputs/audit/rebound_leader_new_pit_source_discovery_v5_21/report.md"),
        row("东方财富历史资金流", "rejected", f"V5.22 successful_hist_probe_count={source['eastmoney_fund_flow_probe'].get('successful_hist_probe_count')}; historical_source_ready={source['eastmoney_fund_flow_probe'].get('historical_source_ready')}; verdict={source['eastmoney_fund_flow_probe'].get('best_status')}", "outputs/audit/rebound_leader_eastmoney_fund_flow_probe_v5_22/report.md"),
        row("资金流 PIT 面板", "rejected", f"V5.23 snapshot_date_count={source['fund_flow_pit_panel'].get('snapshot_date_count')}; alpha_ready={source['fund_flow_pit_panel'].get('alpha_ready')}; mapped_coverage={source['fund_flow_pit_panel'].get('mapped_coverage')}", "outputs/audit/fund_flow_pit_panel_v5_23/report.md"),
        row("资金流映射修复", "pass" if source['fund_flow_mapping_remediation'].get('mapping_gate_passed') else "fail", f"V5.24 high_confidence_after={source['fund_flow_mapping_remediation'].get('high_confidence_mapping_coverage_after')}; mapping_gate_passed={source['fund_flow_mapping_remediation'].get('mapping_gate_passed')}", "outputs/audit/fund_flow_mapping_remediation_v5_24/report.md"),
        row("资金流前推观察", "pass" if int(source['fund_flow_forward_observer'].get('qualified_ledger_rows_after', 0) or 0) > 0 else "pending", f"V5.25 qualified={source['fund_flow_forward_observer'].get('qualified_ledger_rows_after')}; exploratory={source['fund_flow_forward_observer'].get('exploratory_observation_count')}; ledger_rows_after={source['fund_flow_forward_observer'].get('ledger_rows_after')}", "outputs/audit/fund_flow_forward_observer_v5_25/report.md"),
        entry_gate_check(source["fund_flow_forward_entry_gate"]),
        row("资金流前推结算", "pass" if int(source['fund_flow_forward_settlement'].get('qualified_settled_rows', 0) or 0) > 0 and int(source['fund_flow_forward_settlement'].get('qualified_pending_rows', 0) or 0) == 0 else "pending", f"V5.27 qualified_settled={source['fund_flow_forward_settlement'].get('qualified_settled_rows')}; qualified_pending={source['fund_flow_forward_settlement'].get('qualified_pending_rows')}; exploratory_settled={source['fund_flow_forward_settlement'].get('exploratory_settled_rows')}", "outputs/audit/fund_flow_forward_settlement_v5_27/report.md"),
        row("资金流前推晋级评价", "pass" if source['fund_flow_promotion_evaluator'].get('promotion_ready') else "pending", f"V5.28 promotion_ready={source['fund_flow_promotion_evaluator'].get('promotion_ready')}; settled_batch_count={source['fund_flow_promotion_evaluator'].get('settled_batch_count')}; can_claim={source['fund_flow_promotion_evaluator'].get('can_claim_strong_rebound_industries')}", "outputs/audit/fund_flow_promotion_evaluator_v5_28/report.md"),
        row("资金流证据日历", "pass" if source['fund_flow_evidence_calendar'].get('goal_ready') else "pending", f"V5.29 next_action_date={source['fund_flow_evidence_calendar'].get('next_action_date')}; fail_count={source['fund_flow_evidence_calendar'].get('fail_count')}; pending_count={source['fund_flow_evidence_calendar'].get('pending_count')}", "outputs/audit/fund_flow_evidence_calendar_v5_29/report.md"),
        row("资金流账本完整性", "pass" if source['fund_flow_ledger_integrity'].get('integrity_passed') else "fail", f"V5.30 integrity_passed={source['fund_flow_ledger_integrity'].get('integrity_passed')}; violation_count={source['fund_flow_ledger_integrity'].get('violation_count')}", "outputs/audit/fund_flow_forward_ledger_integrity_v5_30/report.md"),
        row("资金流证据冻结指纹", "pass" if source['fund_flow_freeze_manifest'].get('freeze_passed') else "fail", f"V5.31 freeze_passed={source['fund_flow_freeze_manifest'].get('freeze_passed')}; changed_count={source['fund_flow_freeze_manifest'].get('changed_count')}", "outputs/audit/fund_flow_evidence_freeze_manifest_v5_31/report.md"),
        row("资金流持有观察", "pass" if int(source['fund_flow_holding_observation'].get('holding_observation_count', 0) or 0) or int(source['fund_flow_holding_observation'].get('exit_settlement_due_count', 0) or 0) else "pending", f"V5.32 holding={source['fund_flow_holding_observation'].get('holding_observation_count')}; exit_due={source['fund_flow_holding_observation'].get('exit_settlement_due_count')}", "outputs/audit/fund_flow_holding_observation_v5_32/report.md"),
        row("资金流候选入场价冻结", "pass" if int(source['fund_flow_entry_price_freeze'].get('frozen_entry_count', 0) or 0) > 0 else "pending", f"V5.33 frozen_entry_count={source['fund_flow_entry_price_freeze'].get('frozen_entry_count')}; freeze_rows={source['fund_flow_entry_price_freeze'].get('freeze_rows')}", "outputs/audit/fund_flow_entry_price_freeze_v5_33/report.md"),
        row("资金流基准入场价冻结", "pass" if int(source['fund_flow_benchmark_entry_freeze'].get('benchmark_frozen_rows', 0) or 0) >= 100 else "pending", f"V5.34 benchmark_frozen_rows={source['fund_flow_benchmark_entry_freeze'].get('benchmark_frozen_rows')}; batch_count={source['fund_flow_benchmark_entry_freeze'].get('benchmark_batch_count')}", "outputs/audit/fund_flow_benchmark_entry_freeze_v5_34/report.md"),
        row("资金流等待期实盘辅助层", "pass" if int(source['fund_flow_waiting_room'].get('observation_rows', 0) or 0) > 0 and not source['fund_flow_waiting_room'].get('can_claim_strong_rebound_industries') else "pending", f"V5.35 observation_rows={source['fund_flow_waiting_room'].get('observation_rows')}; next_action_date={source['fund_flow_waiting_room'].get('next_action_date')}; can_claim={source['fund_flow_waiting_room'].get('can_claim_strong_rebound_industries')}", "outputs/audit/fund_flow_waiting_room_v5_35/report.md"),
    ])


def row(requirement: str, status: str, evidence: str, source_path: str) -> dict[str, str]:
    return {"requirement": requirement, "status": status, "evidence": evidence, "source_path": source_path}


def true_forward_evidence_ready(source: dict[str, dict[str, Any]]) -> bool:
    return verified_forward_evidence_ready(source.get("promotion", {}))


def entry_gate_check(gate: dict[str, Any]) -> dict[str, str]:
    reviewed = int(gate.get("entry_review_required_count", 0) or 0) + int(gate.get("entry_allowed_count", 0) or 0)
    missing = int(gate.get("entry_missing_snapshot_count", 1) or 0)
    status = "pass" if reviewed > 0 and missing == 0 else "pending"
    evidence = (
        f"V5.26 review_or_allowed={reviewed}; "
        f"entry_review_required={gate.get('entry_review_required_count')}; "
        f"entry_allowed={gate.get('entry_allowed_count')}; "
        f"missing_snapshot={gate.get('entry_missing_snapshot_count')}; "
        f"as_of={gate.get('as_of_date')}"
    )
    return row("资金流入场门禁", status, evidence, "outputs/audit/fund_flow_forward_entry_gate_v5_26/report.md")


def build_next_actions(source: dict[str, dict[str, Any]]) -> pd.DataFrame:
    next_date = source.get("fund_flow_evidence_calendar", {}).get("next_action_date") or "<YYYY-MM-DD>"
    return pd.DataFrame([
        {"priority": "P0", "action": "补齐历史估值可得时间与同期分类", "why": f"当前方法门={source['pit_universe_methodology'].get('promotion_gate_passed')}；可晋级估值行={source['pit_universe_methodology'].get('promotion_eligible_valuation_row_count')}", "command": "python .\\scripts\\audit_pit_universe_methodology.py --check"},
        {"priority": "P1", "action": "结算资金流前推观察", "why": f"当前 V5.27 settled_rows={source['fund_flow_forward_settlement'].get('settled_rows')}", "command": f"python .\\scripts\\settle_v5_27_fund_flow_forward_samples.py --as-of-date {next_date}"},
        {"priority": "P1", "action": "评价资金流前推晋级", "why": f"当前 V5.28 promotion_ready={source['fund_flow_promotion_evaluator'].get('promotion_ready')}", "command": "python .\\scripts\\build_v5_28_fund_flow_promotion_evaluator.py"},
        {"priority": "P1", "action": "查看资金流证据日历", "why": f"下一动作日期={source['fund_flow_evidence_calendar'].get('next_action_date')}", "command": f"python .\\scripts\\build_v5_29_fund_flow_evidence_calendar.py --as-of-date {next_date}"},
    ])


def build_evidence_sources() -> pd.DataFrame:
    return pd.DataFrame([
        {"source_id": name, "path": str(path.relative_to(ROOT)), "exists": path.exists()}
        for name, path in SOURCES.items()
    ])


def build_summary(
    checks: pd.DataFrame,
    methodology: dict[str, Any] | None = None,
    *,
    promotion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    methodology = methodology or {}
    forward_evidence_ready = verified_forward_evidence_ready(promotion or {})
    fail_count = int(checks["status"].eq("fail").sum())
    pending_count = int(checks["status"].eq("pending").sum())
    rejected_count = int(checks["status"].eq("rejected").sum())
    blocking_nonpass_count = int(checks["status"].ne("pass").sum())
    goal_ready = bool(len(checks)) and blocking_nonpass_count == 0
    return {
        "version": "5.10.0",
        "policy_id": "rebound_leader_goal_completion_audit_v5_10",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pass_count": int(checks["status"].eq("pass").sum()),
        "fail_count": fail_count,
        "pending_count": pending_count,
        "rejected_count": rejected_count,
        "blocking_nonpass_count": blocking_nonpass_count,
        "goal_ready": goal_ready,
        "can_claim_strong_rebound_industries": goal_ready,
        "production_ready": False,
        "auto_execution_allowed": False,
        "pit_methodology_audit_passed": bool(methodology.get("audit_passed", False)),
        "pit_universe_methodology_gate_passed": methodology_route_ready(methodology, promotion or {}),
        "historical_pit_universe_promotion_gate_passed": bool(methodology.get("promotion_gate_passed", False)),
        "true_forward_route_ready": forward_evidence_ready,
        "promotion_eligible_valuation_row_count": int(methodology.get("promotion_eligible_valuation_row_count", 0) or 0),
        "valuation_availability_status": methodology.get("valuation_availability_status", "unknown"),
        "historical_review_set_label": methodology.get("historical_review_set_label", "unknown"),
        "best_status": "research_only_goal_ready" if goal_ready else "research_only_goal_not_complete",
        "final_verdict": "V5.10 完成度审计显示：全部门槛已通过，可以声称研究上找到强反弹行业。" if goal_ready else "V5.10 完成度审计显示：历史估值与行业分类方法门失败关闭，旧回测不能晋级；真实前推证据也尚未完成，目标未完成。",
    }


def write_outputs(summary: dict[str, Any], checks: pd.DataFrame, next_actions: pd.DataFrame, evidence: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    checks.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, checks, next_actions), encoding="utf-8")
    checks.to_csv(DEBUG / "goal_completion_checks.csv", index=False, encoding="utf-8-sig")
    evidence.to_csv(DEBUG / "evidence_sources.csv", index=False, encoding="utf-8-sig")
    write_json(DEBUG / "source_summaries.json", {"sources": evidence.to_dict(orient="records")})
    next_actions.to_csv(DEBUG / "next_actions.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], checks: pd.DataFrame, next_actions: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.10 目标完成度审计",
        "",
        summary["final_verdict"],
        "",
        f"- 通过项：{summary['pass_count']}",
        f"- 失败项：{summary['fail_count']}",
        f"- 待观察项：{summary['pending_count']}",
        f"- 已拒绝项：{summary['rejected_count']}",
        f"- 目标是否完成：`{str(summary['goal_ready']).lower()}`",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        f"- PIT/行业历史方法门：`{str(summary['pit_universe_methodology_gate_passed']).lower()}`（可晋级估值行={summary['promotion_eligible_valuation_row_count']}）",
        "",
        "## 检查项",
        "",
        checks.to_markdown(index=False),
        "",
        "## 下一步",
        "",
        next_actions.to_markdown(index=False),
    ])


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    checks = pd.DataFrame([row("x", "pass", "ok", "p"), row("y", "fail", "bad", "p"), row("z", "rejected", "old", "p")])
    methodology = {
        "audit_passed": True,
        "methodology_remediation_complete": True,
        "legacy_oos_label_corrected": True,
        "historical_review_set_label": "historical_review_used_in_iteration",
        "valuation_required_fields": ["trade_date", "published_at", "available_date", "fetched_at", "source_version", "revision_status"],
        "policy_status": "research_only",
        "production_ready": False,
        "auto_execution_allowed": False,
        "promotion_gate_passed": False,
        "historical_valuation_pit_gate_passed": False,
        "historical_classification_gate_passed": False,
        "promotion_eligible_valuation_row_count": 0,
        "valuation_availability_status": "unavailable_for_promotion",
        "classification_history_status": "unavailable",
    }
    summary = build_summary(checks, methodology)
    assert summary["goal_ready"] is False
    assert summary["fail_count"] == 1
    assert summary["rejected_count"] == 1
    assert summary["can_claim_strong_rebound_industries"] is False
    assert summary["pit_universe_methodology_gate_passed"] is False
    assert build_summary(pd.DataFrame([row("x", "pass", "ok", "p")]), methodology, promotion={"forward_evidence_integrity_passed": True})["pit_universe_methodology_gate_passed"] is False
    assert build_summary(pd.DataFrame([row("x", "pass", "ok", "p")]), {**methodology, "audit_passed": False}, promotion={"forward_evidence_integrity_passed": True})["pit_universe_methodology_gate_passed"] is False
    rejected_only = build_summary(pd.DataFrame([row("core", "rejected", "old", "p")]))
    assert rejected_only["goal_ready"] is False
    assert rejected_only["blocking_nonpass_count"] == 1
    ready = build_summary(pd.DataFrame([row("x", "pass", "ok", "p")]))
    assert ready["goal_ready"] is True
    assert ready["can_claim_strong_rebound_industries"] is True
    assert entry_gate_check({"entry_review_required_count": 4, "entry_allowed_count": 0, "entry_missing_snapshot_count": 0})["status"] == "pass"
    assert entry_gate_check({"entry_review_required_count": 0, "entry_allowed_count": 0, "entry_missing_snapshot_count": 0})["status"] == "pending"
    source = {name: {} for name in SOURCES}
    source["pit_universe_methodology"] = methodology
    source["fund_flow_forward_observer"] = {"ledger_rows_after": 4, "qualified_ledger_rows_after": 4}
    observer_status = build_checks(source).set_index("requirement").loc["资金流前推观察", "status"]
    assert observer_status == "pass"
    assert "历史回测证据边界" in build_checks(source).set_index("requirement").index
    source["fund_flow_forward_settlement"] = {"settled_rows": 4, "pending_rows": 0, "qualified_settled_rows": 4, "qualified_pending_rows": 0}
    source["fund_flow_promotion_evaluator"] = {"promotion_ready": True}
    source["fund_flow_evidence_calendar"] = {"goal_ready": True, "next_action_date": "2026-07-21"}
    statuses = build_checks(source).set_index("requirement")["status"].to_dict()
    assert statuses["资金流前推结算"] == "pass"
    assert statuses["资金流前推晋级评价"] == "pass"
    assert statuses["资金流证据日历"] == "pass"
    source["promotion"] = {
        "version": "5.07.0",
        "policy_id": "rebound_leader_promotion_evaluator_v5_07",
        "policy_status": "research_only",
        "auto_execution_allowed": False,
        "forward_evidence_integrity_model": "append_only_hash_chain_v1",
        "forward_evidence_integrity_passed": True,
        "forward_evidence_start": "2026-07-12",
        "historical_backfill_eligible_count": 0,
        "rule_mutation_detected": False,
        "forward_ledger_head_hash": "a" * 64,
        "forward_timing_gate_passed": True,
        "forward_timing_event_count": 20,
        "forward_timing_mean_return": 0.01,
        "forward_timing_median_return": 0.01,
        "forward_timing_win_rate": 0.60,
        "passing_rule_count": 1,
        "can_claim_strong_rebound_industries": True,
        "best_status": "pass_rebound_leader_promotion_gate",
        "best_event_count": 30,
        "best_forward_event_count": 12,
        "forward_settled_event_count": 20,
        "best_mean_relative_return": 0.02,
        "best_top_quintile_hit_rate": 0.35,
    }
    assert build_checks(source).set_index("requirement").loc["PIT 估值与行业历史方法门", "status"] == "fail"
    source["pit_universe_methodology"] = {**methodology, "audit_passed": False}
    assert build_checks(source).set_index("requirement").loc["PIT 估值与行业历史方法门", "status"] == "fail"
    actions = build_next_actions(source)
    assert len(actions) == 4
    assert not actions["command"].str.contains("v5_06|v5_07|v5_21|v5_22|v5_23", case=False, regex=True).any()
    assert not actions["command"].str.contains("<YYYY-MM-DD>", regex=False).any()
    assert actions["command"].str.contains("2026-07-21", regex=False).sum() == 2
    assert "pit_valuation_percentile" in SOURCES
    assert "pit_universe_methodology" in SOURCES
    assert "early_confirmation" in SOURCES
    assert "confirmation_filter" in SOURCES
    assert "failure_diagnosis" in SOURCES
    assert "window_quality_proxy" in SOURCES
    assert "phase_sample_expansion" in SOURCES
    assert "rolling_quarantine" in SOURCES
    assert "volume_confirmation" in SOURCES
    assert "evidence_boundary" in SOURCES
    assert "new_pit_source_discovery" in SOURCES
    assert "eastmoney_fund_flow_probe" in SOURCES
    assert "fund_flow_pit_panel" in SOURCES
    assert "fund_flow_mapping_remediation" in SOURCES
    assert "fund_flow_forward_observer" in SOURCES
    assert "fund_flow_forward_entry_gate" in SOURCES
    assert "fund_flow_forward_settlement" in SOURCES
    assert "fund_flow_promotion_evaluator" in SOURCES
    assert "fund_flow_evidence_calendar" in SOURCES
    assert "fund_flow_ledger_integrity" in SOURCES
    assert "fund_flow_freeze_manifest" in SOURCES
    assert "fund_flow_holding_observation" in SOURCES
    assert "fund_flow_entry_price_freeze" in SOURCES
    assert "fund_flow_benchmark_entry_freeze" in SOURCES
    assert "fund_flow_waiting_room" in SOURCES
    print("self_check=pass")


if __name__ == "__main__":
    main()
