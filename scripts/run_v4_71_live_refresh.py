#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "live_refresh_manifest.json"
WARNINGS = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "live_refresh_warnings.csv"
V471 = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit"
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V473 = ROOT / "outputs" / "industry_rebound_leader_state_gated_v4_73"


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the V4.71 live-review chain.")
    parser.add_argument("--trade-date", type=iso_date, default=date.today().isoformat(), help="Decision date, YYYY-MM-DD.")
    parser.add_argument("--skip-history-refresh", action="store_true", help="Use cached industry histories.")
    parser.add_argument("--skip-task-brief-audit", action="store_true", help="Skip the task brief audit.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if error := trade_date_error(args.trade_date, date.today()):
        parser.error(error)
    manifest: list[dict[str, object]] = []

    for command in refresh_commands(args):
        run(command, manifest)
    write_manifest(args, manifest, "pre_audit")
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_window_v4_71_robustness_live_audit", "--required-debug-files", "source_panel.csv", "base_v4_70_trades.csv", "parameter_perturbation.csv", "parameter_failure_diagnosis.csv", "cooldown_sensitivity.csv", "annual_breakdown.csv", "market_state_breakdown.csv", "year_state_breakdown.csv", "latest_signal_status.csv", "tradable_carrier_mapping.csv", "carrier_execution_replay.csv", "manual_carrier_review_sheet.csv", "pre_entry_manual_review.md", "carrier_mapping_audit.csv", "robustness_checks.csv", "data_availability_audit.csv", "leakage_audit.csv", "live_decision_packet.json", "forward_sample_tracker.json", "forward_sample_checklist.csv", "forward_sample_ledger_audit.csv", "pre_entry_gate.csv", "production_readiness_debt.csv", "live_refresh_manifest.json", "live_refresh_warnings.csv", "optimization_notes.json", "frozen_policy.json"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_selection_v4_72", "--required-debug-files", "industry_event_panel.csv", "industry_event_opportunity_set.csv", "strategy_results.csv", "factor_discovery_events.csv", "factor_discovery_results.csv", "annual_breakdown.csv", "latest_rebound_leader_candidates.csv", "leakage_audit.csv", "evaluation_gate_audit.csv", "industry_leader_evidence_debt.csv", "asof_failure_filter_events.csv", "asof_failure_filter_sensitivity.csv", "failure_diagnosis.csv", "industry_candidate_carrier_mapping.csv", "carrier_mapping_audit.csv", "carrier_exposure_audit.csv", "carrier_tracking_audit.csv", "pre_trade_manual_review_sheet.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_state_gated_v4_73", "--required-debug-files", "state_annotated_event_panel.csv", "state_gated_event_panel.csv", "state_gated_strategy_results.csv", "evaluation_gate_audit.csv", "latest_state_gated_candidates.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_oos_factor_v4_74", "--required-debug-files", "state_annotated_opportunity_set.csv", "factor_event_panel.csv", "factor_oos_results.csv", "selected_factor_oos_audit.csv", "evaluation_gate_audit.csv", "latest_oos_factor_candidates.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_walk_forward_v4_75", "--required-debug-files", "factor_event_panel.csv", "walk_forward_year_decisions.csv", "walk_forward_executed_events.csv", "walk_forward_annual_breakdown.csv", "evaluation_gate_audit.csv", "latest_walk_forward_candidates.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_fund_flow_readiness_v4_76", "--required-debug-files", "fund_flow_readiness_audit.csv", "candidate_fund_flow_inputs.csv", "current_fund_flow_observation_candidates.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_feature_separability_v4_77", "--required-debug-files", "feature_event_separability.csv", "feature_separability_results.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_separable_portfolio_v4_78", "--required-debug-files", "separable_portfolio_event_panel.csv", "separable_portfolio_results.csv", "evaluation_gate_audit.csv", "latest_separable_portfolio_candidates.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_pressure_state_v4_79", "--required-debug-files", "pressure_state_event_panel.csv", "pressure_state_strategy_results.csv", "evaluation_gate_audit.csv", "robustness_audit.csv", "yearly_diagnostics.csv", "latest_pressure_state_candidates.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_robust_grid_v4_80", "--required-debug-files", "robust_grid_event_panel.csv", "robust_grid_results.csv", "robustness_detail.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_market_state_v4_81", "--required-debug-files", "market_state_event_panel.csv", "market_state_grid_results.csv", "state_definition_audit.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/industry_fund_flow_source_audit", "--required-debug-files", "source_attempts.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_new_pit_source_v4_82", "--required-debug-files", "source_readiness_audit.csv", "fund_flow_cache_audit.csv", "mapping_readiness_audit.csv", "pit_collection_plan.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_trap_guardrail_v4_83", "--required-debug-files", "guardrail_event_panel.csv", "guardrail_grid_results.csv", "guardrail_definition_audit.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_structure_features_v4_84", "--required-debug-files", "structure_event_panel.csv", "structure_grid_results.csv", "feature_coverage_audit.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_parent_neutral_v4_85", "--required-debug-files", "parent_neutral_event_panel.csv", "parent_neutral_grid_results.csv", "parent_mapping_audit.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_parent_neutral_forward_v4_86", "--required-debug-files", "archived_forward_rows.csv", "forward_ledger_audit.csv", "frozen_parent_neutral_rule.csv", "forward_observation_plan.csv", "current_opportunity_set.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_evidence_scorecard_v4_87", "--required-debug-files", "evidence_scorecard.csv", "forward_tracker_status.csv", "promotion_protocol.csv", "ledger_snapshot.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_pre_entry_audit_v4_88", "--required-debug-files", "pre_entry_checks.csv", "candidate_consistency.csv", "pre_entry_action_plan.csv", "regenerated_current_candidates.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_entry_batch_gate_v4_90", "--required-debug-files", "entry_gate_checks.csv", "ledger_before.csv", "ledger_after_preview.csv", "v488_check_snapshot.csv", "entry_operator_checklist.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_promotion_math_v4_91", "--required-debug-files", "promotion_math_current.csv", "promotion_threshold_grid.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_metric_grain_v4_92", "--required-debug-files", "metric_grain_checks.csv", "tracker_grain_snapshot.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_goal_readiness_v4_89", "--required-debug-files", "readiness_checks.csv", "forward_protocol.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_historical_backtest_verdict_v4_93", "--required-debug-files", "source_version_summary.csv", "historical_gate_checks.csv", "market_quality_filter_probe.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_independent_event_audit_v4_94", "--required-debug-files", "independent_event_rows.csv", "cluster_members.csv", "independent_gate_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_window_sample_capacity_audit_v4_95", "--required-debug-files", "all_window_clusters.csv", "year_distribution.csv", "sample_capacity_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_window_expansion_capacity_audit_v4_96", "--required-debug-files", "window_variant_results.csv", "expanded_window_clusters.csv", "capacity_gate_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_expanded_window_v4_97", "--required-debug-files", "expanded_window_trades.csv", "industry_event_panel.csv", "industry_event_opportunity_set.csv", "strategy_results.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_expanded_feature_separability_v4_98", "--required-debug-files", "feature_event_separability.csv", "feature_separability_results.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_market_sensitivity_v4_99", "--required-debug-files", "market_sensitivity_opportunity_set.csv", "feature_event_separability.csv", "feature_separability_results.csv", "strategy_event_panel.csv", "strategy_results.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_beta_composite_v5_00", "--required-debug-files", "beta_composite_opportunity_set.csv", "strategy_event_panel.csv", "strategy_results.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_beta_failure_stratification_v5_01", "--required-debug-files", "beta_event_diagnostics.csv", "selected_industry_rows.csv", "state_failure_buckets.csv", "parent_failure_buckets.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_beta_guardrail_v5_02", "--required-debug-files", "beta_guardrail_event_panel.csv", "beta_guardrail_results.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/industry_rebound_leader_window_quality_v5_03", "--required-debug-files", "window_quality_labeled_events.csv", "window_quality_event_panel.csv", "window_quality_results.csv", "evaluation_gate_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_evidence_freeze_v5_04", "--required-debug-files", "frozen_rule_spec.csv", "promotion_checklist.csv", "forward_validation_template.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_forward_tracker_v5_05", "--required-debug-files", "forward_validation_ledger.csv", "promotion_progress.csv", "forward_boundary_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_forward_settlement_v5_06", "--required-debug-files", "settled_forward_rows.csv", "forward_ledger_snapshot.csv", "settlement_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_promotion_evaluator_v5_07", "--required-debug-files", "promotion_results.csv", "promotion_gate_audit.csv", "historical_frozen_events.csv", "forward_settled_events.csv", "combined_evaluation_events.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_forward_signal_detector_v5_08", "--required-debug-files", "latest_window_state.csv", "frozen_rule_trigger_check.csv", "candidate_append_commands.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_pseudo_forward_audit_v5_09", "--required-debug-files", "pseudo_forward_splits.csv", "passing_pseudo_forward_splits.csv", "failed_pseudo_forward_splits.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_goal_completion_audit_v5_10", "--required-debug-files", "goal_completion_checks.csv", "evidence_sources.csv", "next_actions.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_pit_valuation_audit_v5_11", "--required-debug-files", "pit_valuation_opportunity_set.csv", "pit_valuation_event_panel.csv", "pit_valuation_results.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_pit_valuation_percentile_audit_v5_12", "--required-debug-files", "pit_valuation_percentile_opportunity_set.csv", "pit_valuation_percentile_event_panel.csv", "pit_valuation_percentile_results.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/pit_universe_methodology_remediation", "--required-debug-files", "valuation_field_contract.csv", "valuation_source_provenance.csv", "universe_period_audit.csv", "universe_membership_changes.csv", "identity_episodes.csv", "industry_history_file_audit.csv", "universe_robustness_metrics.csv", "evidence_set_labels.csv", "methodology_checks.csv", "input_manifest.json", "structure_manifest.json"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_early_confirmation_audit_v5_13", "--required-debug-files", "early_confirmation_opportunity_set.csv", "early_confirmation_event_panel.csv", "early_confirmation_results.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_confirmation_filter_audit_v5_14", "--required-debug-files", "confirmation_filter_opportunity_set.csv", "confirmation_filter_event_panel.csv", "confirmation_filter_results.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_failure_diagnosis_v5_15", "--required-debug-files", "selected_industry_rows.csv", "event_diagnostics.csv", "industry_failure_exposure.csv", "failure_bucket_diagnostics.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_window_quality_proxy_audit_v5_16", "--required-debug-files", "window_quality_proxy_source_panel.csv", "window_quality_proxy_event_panel.csv", "window_quality_proxy_results.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_phase_sample_expansion_audit_v5_17", "--required-debug-files", "expanded_phase_events.csv", "expanded_phase_opportunity_set.csv", "expanded_phase_event_panel.csv", "expanded_phase_results.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_rolling_quarantine_audit_v5_18", "--required-debug-files", "rolling_quarantine_source_panel.csv", "rolling_quarantine_event_panel.csv", "rolling_quarantine_selected_rows.csv", "rolling_quarantine_results.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_volume_confirmation_audit_v5_19", "--required-debug-files", "volume_confirmation_opportunity_set.csv", "volume_confirmation_event_panel.csv", "volume_confirmation_results.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_evidence_boundary_audit_v5_20", "--required-debug-files", "evidence_version_summary.csv", "evidence_boundary_checks.csv", "next_evidence_actions.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_new_pit_source_discovery_v5_21", "--required-debug-files", "candidate_pit_sources.csv", "source_readiness_checks.csv", "next_source_actions.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/rebound_leader_eastmoney_fund_flow_probe_v5_22", "--required-debug-files", "source_probe_results.csv", "sample_columns.csv", "source_readiness_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_pit_panel_v5_23", "--required-debug-files", "fund_flow_pit_panel.csv", "latest_mapped_snapshot.csv", "readiness_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_mapping_remediation_v5_24", "--required-debug-files", "mapping_promotions.csv", "mapping_remediation_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_forward_observer_v5_25", "--required-debug-files", "appendable_observations.csv", "forward_ledger_snapshot.csv", "forward_observer_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_forward_entry_gate_v5_26", "--required-debug-files", "entry_gate_rows.csv", "forward_ledger_snapshot.csv", "entry_gate_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_holding_observation_v5_32", "--required-debug-files", "holding_observation_rows.csv", "holding_observation_checks.csv", "forward_ledger_snapshot.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_entry_price_freeze_v5_33", "--required-debug-files", "entry_price_freeze.csv", "entry_price_freeze_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_benchmark_entry_freeze_v5_34", "--required-debug-files", "benchmark_entry_panel.csv", "benchmark_entry_checks.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_forward_settlement_v5_27", "--required-debug-files", "forward_ledger_snapshot.csv", "settled_forward_rows.csv", "settlement_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_promotion_evaluator_v5_28", "--required-debug-files", "promotion_checks.csv", "settled_observations.csv", "batch_metrics.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_evidence_calendar_v5_29", "--required-debug-files", "evidence_calendar.csv", "evidence_gaps.csv", "source_snapshot.json"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_forward_ledger_integrity_v5_30", "--required-debug-files", "ledger_integrity_checks.csv", "ledger_snapshot.csv", "violation_rows.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_evidence_freeze_manifest_v5_31", "--required-debug-files", "freeze_comparison.csv", "current_manifest.csv", "baseline_manifest.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/fund_flow_waiting_room_v5_35", "--required-debug-files", "waiting_room_checks.csv", "waiting_room_rows.csv", "source_snapshot.json"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/industry_fund_flow_cache_snapshot", "--required-debug-files", "ths_sw2_name_mapping_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/industry_fund_flow_mapping_audit", "--required-debug-files", "mapping_draft.csv", "manual_review_required.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_candidate_fund_flow_overlay", "--required-debug-files", "candidate_fund_flow_overlay.csv", "missing_candidate_fund_flow_mapping.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_rebound_leader_forward_archive", "--required-debug-files", "archived_forward_rows.csv", "forward_ledger_audit.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_forward_return_settlement", "--required-debug-files", "settled_forward_rows.csv", "settlement_audit.csv", "settlement_schedule.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_85_parent_neutral_forward_settlement", "--required-debug-files", "settled_forward_rows.csv", "settlement_audit.csv", "pending_forward_rows.csv", "skipped_forward_rows.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_71_live_guardrail_playbook", "--required-debug-files", "live_guardrail_playbook.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_carrier_alternative_tracking", "--required-debug-files", "carrier_alternative_tracking.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_pre_trade_review_packet", "--required-debug-files", "pre_trade_review_packet.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_entry_readiness", "--required-debug-files", "entry_readiness.csv", "pre_entry_action_checklist.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_pre_entry_operator_checklist", "--required-debug-files", "operator_checklist.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_rebound_leader_quarantine_overlay", "--required-debug-files", "full_ranked_quarantine_overlay.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_rebound_leader_random_baseline_audit", "--required-debug-files", "random_baseline_audit.csv", "year_random_breakdown.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_rebound_leader_bootstrap_audit", "--required-debug-files", "bootstrap_audit.csv", "bootstrap_samples.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_rebound_leader_state_guardrail", "--required-debug-files", "state_guardrail.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_rebound_leader_evaluation_scorecard", "--required-debug-files", "evaluation_scorecard.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_tradeable_research_blocked_leader", "--required-debug-files", "tradeable_research_blocked_leader_audit.csv", "forward_tradeable_leader_checklist.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/v4_72_remediation_queue", "--required-debug-files", "remediation_queue.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/goal_readiness_check", "--required-debug-files", "goal_readiness_check.csv"], manifest)
    run(["scripts/audit_compact_output_layout.py", "--output-dir", "outputs/audit/current_phase_readiness_check", "--required-debug-files", "current_phase_readiness_check.csv"], manifest)
    if not args.skip_task_brief_audit:
        run(["scripts/audit_task_briefs.py"], manifest)
    run(["scripts/append_v4_71_forward_sample.py", "--audit", "--as-of-date", args.trade_date, "--audit-output", "outputs/industry_rebound_window_v4_71_robustness_live_audit/debug/forward_sample_ledger_audit.csv"], manifest)
    write_manifest(args, manifest, "pre_dashboard")
    write_daily_decision_report_section()
    write_manifest(args, manifest, "pass")
    run(["scripts/build_dashboard_dataset.py"], manifest)
    write_manifest(args, manifest, "pass")
    print("V4.71 live refresh complete.")


def refresh_commands(args: argparse.Namespace) -> list[list[str]]:
    commands = [
        ["scripts/run_industry_index_research_validation.py", "--trade-date", args.trade_date, "--industry-level", "second", "--cache-dir", "data_catalog/cache/industry_index", "--output", "outputs/industry_index_research_validation"] + ([] if args.skip_history_refresh else ["--refresh-history"]),
        ["scripts/run_industry_rebound_window_v3_7_industry_breadth.py"],
        ["scripts/run_industry_rebound_window_v4_61_relaxed_breadth_relief.py", "--config", "configs/rebound_window_v4_70_delayed_entry_vol_stop_policy.json"],
        ["scripts/evaluate_rebound_window_effectiveness.py", "--output-dir", "outputs/industry_rebound_window_v4_70_delayed_entry_vol_stop"],
        ["scripts/run_industry_rebound_window_v4_71_robustness_live_audit.py", "--as-of-date", args.trade_date],
        ["scripts/run_industry_rebound_leader_selection_v4_72.py"],
        ["scripts/run_industry_rebound_leader_state_gated_v4_73.py"],
        ["scripts/run_industry_rebound_leader_oos_factor_v4_74.py"],
        ["scripts/run_industry_rebound_leader_walk_forward_v4_75.py"],
        ["scripts/cache_industry_fund_flow_snapshot.py", "--trade-date", args.trade_date],
        ["scripts/build_industry_fund_flow_mapping.py", "--ths-snapshot", f"data_catalog/cache/industry_fund_flow/ths/{args.trade_date}/ths_industry_fund_flow_now.csv"],
        ["scripts/build_v5_24_fund_flow_mapping_remediation.py", "--apply"],
        ["scripts/audit_v4_72_candidate_fund_flow_overlay.py", "--trade-date", args.trade_date],
        ["scripts/audit_industry_fund_flow_source.py"],
        ["scripts/run_industry_rebound_leader_fund_flow_readiness_v4_76.py"],
        ["scripts/run_industry_rebound_leader_feature_separability_v4_77.py"],
        ["scripts/run_industry_rebound_leader_separable_portfolio_v4_78.py"],
        ["scripts/run_industry_rebound_leader_pressure_state_v4_79.py"],
        ["scripts/run_industry_rebound_leader_robust_grid_v4_80.py"],
        ["scripts/run_industry_rebound_leader_market_state_v4_81.py"],
        ["scripts/run_industry_rebound_leader_new_pit_source_v4_82.py"],
        ["scripts/run_industry_rebound_leader_trap_guardrail_v4_83.py"],
        ["scripts/run_industry_rebound_leader_structure_features_v4_84.py"],
        ["scripts/run_industry_rebound_leader_parent_neutral_v4_85.py"],
        ["scripts/run_industry_rebound_leader_parent_neutral_forward_v4_86.py"],
        ["scripts/append_v4_72_rebound_leader_forward_archive.py"],
        ["scripts/settle_v4_72_rebound_leader_forward_returns.py", "--as-of-date", args.trade_date],
        ["scripts/settle_v4_85_parent_neutral_forward_returns.py", "--as-of-date", args.trade_date],
        ["scripts/build_v4_85_parent_neutral_evidence_scorecard.py"],
        ["scripts/build_v4_85_parent_neutral_pre_entry_audit.py", "--as-of-date", args.trade_date],
        ["scripts/build_v4_90_rebound_leader_entry_batch_gate.py", "--as-of-date", args.trade_date],
        ["scripts/build_v4_91_rebound_leader_promotion_math.py"],
        ["scripts/build_v4_92_rebound_leader_metric_grain_audit.py"],
        ["scripts/build_v4_89_rebound_leader_goal_readiness.py"],
        ["scripts/build_v4_93_rebound_leader_historical_backtest_verdict.py"],
        ["scripts/build_v4_94_rebound_leader_independent_event_audit.py"],
        ["scripts/build_v4_95_rebound_window_sample_capacity_audit.py"],
        ["scripts/build_v4_96_rebound_window_expansion_capacity_audit.py"],
        ["scripts/run_industry_rebound_leader_expanded_window_v4_97.py"],
        ["scripts/build_v4_98_expanded_window_feature_separability.py"],
        ["scripts/run_industry_rebound_leader_market_sensitivity_v4_99.py"],
        ["scripts/run_industry_rebound_leader_beta_composite_v5_00.py"],
        ["scripts/build_v5_01_beta_failure_stratification.py"],
        ["scripts/run_industry_rebound_leader_beta_guardrail_v5_02.py"],
        ["scripts/run_industry_rebound_leader_window_quality_v5_03.py"],
        ["scripts/build_v5_04_rebound_leader_evidence_freeze.py"],
        ["scripts/build_v5_05_rebound_leader_forward_tracker.py"],
        ["scripts/settle_v5_06_rebound_leader_forward_samples.py", "--as-of-date", args.trade_date],
        ["scripts/build_v5_07_rebound_leader_promotion_evaluator.py"],
        ["scripts/build_v5_08_rebound_leader_forward_signal_detector.py", "--as-of-date", args.trade_date, "--apply"],
        ["scripts/build_v5_05_rebound_leader_forward_tracker.py"],
        ["scripts/build_v5_07_rebound_leader_promotion_evaluator.py"],
        ["scripts/build_v5_09_rebound_leader_pseudo_forward_audit.py"],
        ["scripts/audit_pit_universe_methodology.py"],
        ["scripts/build_v5_11_rebound_leader_pit_valuation_audit.py"],
        ["scripts/build_v5_12_rebound_leader_pit_valuation_percentile_audit.py"],
        ["scripts/build_v5_13_rebound_leader_early_confirmation_audit.py"],
        ["scripts/build_v5_14_rebound_leader_confirmation_filter_audit.py"],
        ["scripts/build_v5_15_rebound_leader_failure_diagnosis.py"],
        ["scripts/build_v5_16_rebound_window_quality_proxy_audit.py"],
        ["scripts/build_v5_17_rebound_phase_sample_expansion_audit.py"],
        ["scripts/build_v5_18_rebound_leader_rolling_quarantine_audit.py"],
        ["scripts/build_v5_19_rebound_leader_volume_confirmation_audit.py"],
        ["scripts/build_v5_20_rebound_leader_evidence_boundary_audit.py"],
        ["scripts/build_v5_22_rebound_leader_eastmoney_fund_flow_probe.py"],
        ["scripts/build_v5_23_fund_flow_pit_panel.py"],
        ["scripts/build_v5_31_fund_flow_evidence_freeze_manifest.py"],
        ["scripts/build_v5_25_fund_flow_forward_observer.py", "--apply"],
        ["scripts/build_v5_26_fund_flow_forward_entry_gate.py", "--as-of-date", args.trade_date],
        ["scripts/build_v5_32_fund_flow_holding_observation.py", "--as-of-date", args.trade_date],
        ["scripts/build_v5_33_fund_flow_entry_price_freeze.py", "--as-of-date", args.trade_date],
        ["scripts/build_v5_34_fund_flow_benchmark_entry_freeze.py", "--as-of-date", args.trade_date],
        ["scripts/audit_v5_30_fund_flow_forward_ledger_integrity.py", "--as-of-date", args.trade_date],
        ["scripts/settle_v5_27_fund_flow_forward_samples.py", "--as-of-date", args.trade_date],
        ["scripts/audit_v5_30_fund_flow_forward_ledger_integrity.py", "--as-of-date", args.trade_date],
        ["scripts/build_v5_28_fund_flow_promotion_evaluator.py"],
        ["scripts/build_v5_29_fund_flow_evidence_calendar.py", "--as-of-date", args.trade_date],
        ["scripts/build_v5_35_fund_flow_waiting_room.py", "--as-of-date", args.trade_date],
        ["scripts/build_v5_21_rebound_leader_new_pit_source_discovery.py"],
        ["scripts/build_v5_10_rebound_leader_goal_completion_audit.py"],
        ["scripts/build_v4_71_live_guardrail_playbook.py"],
        ["scripts/audit_v4_72_carrier_alternative_tracking.py"],
        ["scripts/build_v4_72_pre_trade_review_packet.py"],
        ["scripts/audit_v4_72_entry_readiness.py", "--as-of-date", args.trade_date],
        ["scripts/build_v4_72_pre_entry_operator_checklist.py"],
        ["scripts/audit_v4_72_tradeable_research_blocked_leader.py", "--as-of-date", args.trade_date],
        ["scripts/build_v4_72_rebound_leader_quarantine_overlay.py"],
        ["scripts/build_v4_72_rebound_leader_random_baseline_audit.py"],
        ["scripts/build_v4_72_rebound_leader_bootstrap_audit.py"],
        ["scripts/build_v4_72_rebound_leader_state_guardrail.py"],
        ["scripts/build_v4_72_rebound_leader_evaluation_scorecard.py"],
        ["scripts/build_v4_72_remediation_queue.py"],
        ["scripts/build_goal_readiness_check.py"],
        ["scripts/build_current_phase_readiness_check.py"],
    ]
    return commands


def self_check() -> None:
    args = argparse.Namespace(trade_date="2026-06-19", skip_history_refresh=True, skip_task_brief_audit=True)
    commands = [" ".join(command) for command in refresh_commands(args)]
    required_order = [
        "scripts/run_industry_rebound_leader_state_gated_v4_73.py",
        "scripts/run_industry_rebound_leader_oos_factor_v4_74.py",
        "scripts/run_industry_rebound_leader_walk_forward_v4_75.py",
        "scripts/audit_industry_fund_flow_source.py",
        "scripts/run_industry_rebound_leader_fund_flow_readiness_v4_76.py",
        "scripts/run_industry_rebound_leader_feature_separability_v4_77.py",
        "scripts/run_industry_rebound_leader_separable_portfolio_v4_78.py",
        "scripts/run_industry_rebound_leader_pressure_state_v4_79.py",
        "scripts/run_industry_rebound_leader_robust_grid_v4_80.py",
        "scripts/run_industry_rebound_leader_market_state_v4_81.py",
        "scripts/run_industry_rebound_leader_new_pit_source_v4_82.py",
        "scripts/run_industry_rebound_leader_trap_guardrail_v4_83.py",
        "scripts/run_industry_rebound_leader_structure_features_v4_84.py",
        "scripts/run_industry_rebound_leader_parent_neutral_v4_85.py",
        "scripts/run_industry_rebound_leader_parent_neutral_forward_v4_86.py",
        "scripts/build_v4_85_parent_neutral_evidence_scorecard.py",
        "scripts/build_v4_85_parent_neutral_pre_entry_audit.py --as-of-date 2026-06-19",
        "scripts/build_v4_90_rebound_leader_entry_batch_gate.py --as-of-date 2026-06-19",
        "scripts/build_v4_91_rebound_leader_promotion_math.py",
        "scripts/build_v4_92_rebound_leader_metric_grain_audit.py",
        "scripts/build_v4_89_rebound_leader_goal_readiness.py",
        "scripts/audit_v4_72_entry_readiness.py --as-of-date 2026-06-19",
        "scripts/build_v4_72_pre_entry_operator_checklist.py",
        "scripts/audit_v4_72_tradeable_research_blocked_leader.py --as-of-date 2026-06-19",
        "scripts/build_v4_72_rebound_leader_quarantine_overlay.py",
        "scripts/build_v4_72_rebound_leader_random_baseline_audit.py",
        "scripts/build_v4_72_rebound_leader_bootstrap_audit.py",
        "scripts/build_v4_72_rebound_leader_state_guardrail.py",
        "scripts/build_v4_72_rebound_leader_evaluation_scorecard.py",
        "scripts/build_v4_72_remediation_queue.py",
        "scripts/build_goal_readiness_check.py",
        "scripts/build_current_phase_readiness_check.py",
    ]
    indexes = [commands.index(item) for item in required_order]
    assert indexes == sorted(indexes), indexes
    fund_flow_chain = [
        "scripts/build_v5_31_fund_flow_evidence_freeze_manifest.py",
        "scripts/build_v5_25_fund_flow_forward_observer.py --apply",
        "scripts/build_v5_26_fund_flow_forward_entry_gate.py --as-of-date 2026-06-19",
        "scripts/build_v5_32_fund_flow_holding_observation.py --as-of-date 2026-06-19",
        "scripts/build_v5_33_fund_flow_entry_price_freeze.py --as-of-date 2026-06-19",
        "scripts/build_v5_34_fund_flow_benchmark_entry_freeze.py --as-of-date 2026-06-19",
        "scripts/audit_v5_30_fund_flow_forward_ledger_integrity.py --as-of-date 2026-06-19",
        "scripts/settle_v5_27_fund_flow_forward_samples.py --as-of-date 2026-06-19",
        "scripts/audit_v5_30_fund_flow_forward_ledger_integrity.py --as-of-date 2026-06-19",
        "scripts/build_v5_28_fund_flow_promotion_evaluator.py",
        "scripts/build_v5_29_fund_flow_evidence_calendar.py --as-of-date 2026-06-19",
        "scripts/build_v5_35_fund_flow_waiting_room.py --as-of-date 2026-06-19",
    ]
    chain_indexes = []
    search_from = 0
    for command in fund_flow_chain:
        index = commands.index(command, search_from)
        chain_indexes.append(index)
        search_from = index + 1
    assert chain_indexes == sorted(chain_indexes), chain_indexes
    assert commands[0].endswith("--output outputs/industry_index_research_validation")
    assert "--refresh-history" not in commands[0]
    assert "scripts/build_dashboard_dataset.py" not in commands
    detector = commands.index("scripts/build_v5_08_rebound_leader_forward_signal_detector.py --as-of-date 2026-06-19 --apply")
    assert commands[detector + 1] == "scripts/build_v5_05_rebound_leader_forward_tracker.py"
    assert commands[detector + 2] == "scripts/build_v5_07_rebound_leader_promotion_evaluator.py"
    pit_discovery = commands.index("scripts/build_v5_21_rebound_leader_new_pit_source_discovery.py")
    goal_audit = commands.index("scripts/build_v5_10_rebound_leader_goal_completion_audit.py")
    assert pit_discovery < goal_audit
    pit_methodology = commands.index("scripts/audit_pit_universe_methodology.py")
    pit_v511 = commands.index("scripts/build_v5_11_rebound_leader_pit_valuation_audit.py")
    pit_v512 = commands.index("scripts/build_v5_12_rebound_leader_pit_valuation_percentile_audit.py")
    boundary_v520 = commands.index("scripts/build_v5_20_rebound_leader_evidence_boundary_audit.py")
    assert pit_methodology < pit_v511 < pit_v512 < boundary_v520 < goal_audit
    assert trade_date_error("2026-06-19", date(2026, 6, 20)) == ""
    assert "weekend" in trade_date_error("2026-06-20", date(2026, 6, 20))
    assert "future" in trade_date_error("2026-06-23", date(2026, 6, 20))
    source = Path(__file__).read_text(encoding="utf-8")
    assert "industry_event_opportunity_set.csv" in source
    assert "settlement_schedule.csv" in source
    assert "live_guardrail_playbook.csv" in source
    assert "pre_entry_action_checklist.csv" in source
    assert "operator_checklist.csv" in source
    assert "full_ranked_quarantine_overlay.csv" in source
    assert "random_baseline_audit.csv" in source
    assert "year_random_breakdown.csv" in source
    assert "bootstrap_audit.csv" in source
    assert "state_guardrail.csv" in source
    assert "latest_oos_factor_candidates.csv" in source
    assert "latest_walk_forward_candidates.csv" in source
    assert "current_fund_flow_observation_candidates.csv" in source
    assert "feature_separability_results.csv" in source
    assert "latest_separable_portfolio_candidates.csv" in source
    assert "latest_pressure_state_candidates.csv" in source
    assert "robust_grid_results.csv" in source
    assert "market_state_grid_results.csv" in source
    assert "source_readiness_audit.csv" in source
    assert "guardrail_grid_results.csv" in source
    assert "structure_grid_results.csv" in source
    assert "parent_neutral_grid_results.csv" in source
    assert "frozen_parent_neutral_rule.csv" in source
    assert "v4_85_parent_neutral_forward_settlement" in source
    assert "evidence_scorecard.csv" in source
    assert "pre_entry_checks.csv" in source
    assert "rebound_leader_entry_batch_gate_v4_90" in source
    assert "rebound_leader_promotion_math_v4_91" in source
    assert "rebound_leader_metric_grain_v4_92" in source
    assert "rebound_leader_goal_readiness_v4_89" in source
    assert "rebound_leader_historical_backtest_verdict_v4_93" in source
    assert "rebound_leader_independent_event_audit_v4_94" in source
    assert "rebound_window_sample_capacity_audit_v4_95" in source
    assert "rebound_window_expansion_capacity_audit_v4_96" in source
    assert "industry_rebound_leader_expanded_window_v4_97" in source
    assert "rebound_leader_expanded_feature_separability_v4_98" in source
    assert "industry_rebound_leader_market_sensitivity_v4_99" in source
    assert "industry_rebound_leader_beta_composite_v5_00" in source
    assert "rebound_leader_beta_failure_stratification_v5_01" in source
    assert "industry_rebound_leader_beta_guardrail_v5_02" in source
    assert "industry_rebound_leader_window_quality_v5_03" in source
    assert "rebound_leader_evidence_freeze_v5_04" in source
    assert "rebound_leader_forward_tracker_v5_05" in source
    assert "rebound_leader_forward_settlement_v5_06" in source
    assert "rebound_leader_promotion_evaluator_v5_07" in source
    assert "rebound_leader_forward_signal_detector_v5_08" in source
    assert "rebound_leader_pseudo_forward_audit_v5_09" in source
    assert "rebound_leader_goal_completion_audit_v5_10" in source
    assert "rebound_leader_pit_valuation_audit_v5_11" in source
    assert "rebound_leader_pit_valuation_percentile_audit_v5_12" in source
    assert "rebound_leader_early_confirmation_audit_v5_13" in source
    assert "rebound_leader_confirmation_filter_audit_v5_14" in source
    assert "rebound_leader_failure_diagnosis_v5_15" in source
    assert "rebound_window_quality_proxy_audit_v5_16" in source
    assert "rebound_phase_sample_expansion_audit_v5_17" in source
    assert "rebound_leader_rolling_quarantine_audit_v5_18" in source
    assert "rebound_leader_volume_confirmation_audit_v5_19" in source
    assert "rebound_leader_evidence_boundary_audit_v5_20" in source
    assert "rebound_leader_new_pit_source_discovery_v5_21" in source
    assert "rebound_leader_eastmoney_fund_flow_probe_v5_22" in source
    assert "fund_flow_pit_panel_v5_23" in source
    assert "fund_flow_mapping_remediation_v5_24" in source
    assert "fund_flow_forward_observer_v5_25" in source
    assert "fund_flow_forward_entry_gate_v5_26" in source
    assert "fund_flow_holding_observation_v5_32" in source
    assert "fund_flow_entry_price_freeze_v5_33" in source
    assert "fund_flow_benchmark_entry_freeze_v5_34" in source
    assert "fund_flow_forward_settlement_v5_27" in source
    assert "fund_flow_promotion_evaluator_v5_28" in source
    assert "fund_flow_evidence_calendar_v5_29" in source
    assert "fund_flow_forward_ledger_integrity_v5_30" in source
    assert "fund_flow_evidence_freeze_manifest_v5_31" in source
    assert "fund_flow_waiting_room_v5_35" in source
    assert "current_phase_readiness_check.csv" in source
    assert "退出日结算命令：`python .\\\\scripts\\\\settle_v4_72_rebound_leader_forward_returns.py --as-of-date" in source
    print("self_check=pass")


def run(args: list[str], manifest: list[dict[str, object]]) -> None:
    cmd = [sys.executable, *args]
    print(">", " ".join(args), flush=True)
    started = datetime.now()
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    lines = (result.stdout + "\n" + result.stderr).splitlines()
    manifest.append({
        "command": " ".join(args),
        "exit_code": result.returncode,
        "started_at": started.isoformat(timespec="seconds"),
        "duration_seconds": round((datetime.now() - started).total_seconds(), 3),
        "warning_lines": [line for line in lines if is_warning_line(line)],
    })
    if result.returncode:
        write_manifest(argparse.Namespace(trade_date="", skip_history_refresh=False, skip_task_brief_audit=False), manifest, "fail")
        raise subprocess.CalledProcessError(result.returncode, cmd)


def iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD") from exc


def trade_date_error(value: str, today: date) -> str:
    # ponytail: future/weekend guard only; V4.71 inner audit still owns exchange-holiday calendar checks.
    trade_day = date.fromisoformat(value)
    if trade_day > today:
        return f"--trade-date {value} is in the future; run live refresh on or after that date."
    if trade_day.weekday() >= 5:
        return f"--trade-date {value} is a weekend; use the next A-share trading day for live refresh."
    return ""


def write_manifest(args: argparse.Namespace, runs: list[dict[str, object]], status: str) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps({
        "status": status,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": args.trade_date,
        "skip_history_refresh": args.skip_history_refresh,
        "skip_task_brief_audit": args.skip_task_brief_audit,
        "daily_decision_summary": build_daily_decision_summary(),
        "runs": runs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    write_warnings(runs)


def write_warnings(runs: list[dict[str, object]]) -> None:
    WARNINGS.parent.mkdir(parents=True, exist_ok=True)
    with WARNINGS.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["command", "warning_line"])
        writer.writeheader()
        for run in runs:
            for warning in run.get("warning_lines", []):
                writer.writerow({"command": run["command"], "warning_line": warning})


def is_warning_line(line: str) -> bool:
    text = line.lower().strip()
    if text in {"warnings=0", "warning=0"}:
        return False
    return "failed to fetch" in text or text.startswith("warning:")


def build_daily_decision_summary() -> dict[str, object]:
    window = read_json(V471 / "run_summary.json")
    leader = read_json(V472 / "run_summary.json")
    signal = bool(window.get("latest_signal_triggered"))
    production_ready = bool(window.get("production_ready"))
    leader_validated = str(leader.get("best_status")) == "validated"
    # Hard safety invariant: this research orchestrator never authorizes orders.
    auto_allowed = False
    if not signal:
        decision_state = "no_live_signal"
        action = "无反弹窗口触发；继续每日刷新。"
    elif not production_ready:
        decision_state = "watchlist_only_research_signal"
        action = "窗口触发但生产审计未通过；只能观察或人工跳过。"
    elif not leader_validated:
        decision_state = "window_manual_review_industry_research_only"
        action = "窗口可人工复核；行业强弱排序仅作观察，不作为自动行业选择。"
    else:
        decision_state = "manual_review_required"
        action = "进入人工复核；仍不自动下单。"
    return {
        "decision_state": decision_state,
        "action": action,
        "auto_execution_allowed": auto_allowed,
        "window": {
            "production_ready": production_ready,
            "latest_signal_triggered": signal,
            "planned_entry_date": window.get("planned_entry_date"),
            "planned_exit_date": window.get("planned_exit_date"),
            "next_action_date": window.get("next_action_date"),
            "next_action": window.get("next_action"),
            "blocking_issues": window.get("blocking_issues", []),
        },
        "industry_leader_selection": {
            "best_status": leader.get("best_status"),
            "best_strategy": leader.get("best_strategy"),
            "best_top_n": leader.get("best_top_n"),
            "mean_relative_return": leader.get("best_mean_relative_return"),
            "win_rate": leader.get("best_relative_win_rate"),
            "top_quintile_hit_rate": leader.get("best_top_quintile_hit_rate"),
            "asof_failure_filter_best_variant": leader.get("asof_failure_filter_best_variant"),
            "asof_failure_filter_best_mean_relative_return": leader.get("asof_failure_filter_best_mean_relative_return"),
            "asof_failure_filter_best_top_quintile_hit_rate": leader.get("asof_failure_filter_best_top_quintile_hit_rate"),
            "asof_failure_filter_best_positive_year_rate": leader.get("asof_failure_filter_best_positive_year_rate"),
            "asof_failure_filter_passes_gate": leader.get("asof_failure_filter_passes_gate"),
            "factor_discovery_best_factor": leader.get("factor_discovery_best_factor"),
            "factor_discovery_best_factor_label": leader.get("factor_discovery_best_factor_label"),
            "factor_discovery_best_top_n": leader.get("factor_discovery_best_top_n"),
            "factor_discovery_best_mean_relative_return": leader.get("factor_discovery_best_mean_relative_return"),
            "factor_discovery_best_top_quintile_hit_rate": leader.get("factor_discovery_best_top_quintile_hit_rate"),
            "factor_discovery_best_positive_year_rate": leader.get("factor_discovery_best_positive_year_rate"),
            "factor_discovery_passes_gate": leader.get("factor_discovery_passes_gate"),
            "structure_factor_best_factor": leader.get("structure_factor_best_factor"),
            "structure_factor_best_factor_label": leader.get("structure_factor_best_factor_label"),
            "structure_factor_best_top_n": leader.get("structure_factor_best_top_n"),
            "structure_factor_best_mean_relative_return": leader.get("structure_factor_best_mean_relative_return"),
            "structure_factor_best_top_quintile_hit_rate": leader.get("structure_factor_best_top_quintile_hit_rate"),
            "structure_factor_passes_gate": leader.get("structure_factor_passes_gate"),
            "latest_candidate_count": leader.get("latest_candidate_count"),
            "forward_archive": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_forward_archive" / "run_summary.json"),
            "forward_settlement": read_json(ROOT / "outputs" / "audit" / "v4_72_forward_return_settlement" / "run_summary.json"),
            "live_guardrail_playbook": read_json(ROOT / "outputs" / "audit" / "v4_71_live_guardrail_playbook" / "run_summary.json"),
            "fund_flow_overlay": read_json(ROOT / "outputs" / "audit" / "v4_72_candidate_fund_flow_overlay" / "run_summary.json"),
            "carrier_alternative_tracking": read_json(ROOT / "outputs" / "audit" / "v4_72_carrier_alternative_tracking" / "run_summary.json"),
            "pre_trade_review_packet": read_json(ROOT / "outputs" / "audit" / "v4_72_pre_trade_review_packet" / "run_summary.json"),
            "entry_readiness": read_json(ROOT / "outputs" / "audit" / "v4_72_entry_readiness" / "run_summary.json"),
            "operator_checklist": read_json(ROOT / "outputs" / "audit" / "v4_72_pre_entry_operator_checklist" / "run_summary.json"),
            "random_baseline_audit": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_random_baseline_audit" / "run_summary.json"),
            "bootstrap_audit": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_bootstrap_audit" / "run_summary.json"),
            "state_guardrail": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_state_guardrail" / "run_summary.json"),
            "evaluation_scorecard": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_evaluation_scorecard" / "run_summary.json"),
            "quarantine_overlay": read_json(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_quarantine_overlay" / "run_summary.json"),
            "tradeable_leader_audit": read_json(ROOT / "outputs" / "audit" / "v4_72_tradeable_research_blocked_leader" / "run_summary.json"),
            "remediation_queue": read_json(ROOT / "outputs" / "audit" / "v4_72_remediation_queue" / "run_summary.json"),
            "goal_readiness": read_json(ROOT / "outputs" / "audit" / "goal_readiness_check" / "run_summary.json"),
            "current_phase_readiness": read_json(ROOT / "outputs" / "audit" / "current_phase_readiness_check" / "run_summary.json"),
            "evidence_debt": read_csv_rows(V472 / "debug" / "industry_leader_evidence_debt.csv", 5),
            "scorecard_rows": read_csv_rows(ROOT / "outputs" / "audit" / "v4_72_rebound_leader_evaluation_scorecard" / "top_candidates.csv", 6),
            "remediation_rows": read_csv_rows(ROOT / "outputs" / "audit" / "v4_72_remediation_queue" / "top_candidates.csv", 12),
            "top_review_rows": read_top_review_rows(ROOT / "outputs" / "audit" / "v4_72_pre_trade_review_packet" / "top_candidates.csv", 5),
        },
    }


def read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_top_review_rows(path: Path, limit: int) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            rows.append({
                "review_priority": row.get("review_priority", ""),
                "manual_gate_status": row.get("manual_gate_status", ""),
                "manual_gate_action": row.get("manual_gate_action", ""),
                "industry_name": row.get("industry_name", ""),
                "candidate_carrier_code": row.get("candidate_carrier_code", ""),
                "candidate_carrier_name": row.get("candidate_carrier_name", ""),
                "tracking_audit_status": row.get("tracking_audit_status", ""),
                "carrier_fallback_status": row.get("carrier_fallback_status", ""),
                "fund_flow_overlay_status": row.get("fund_flow_overlay_status", ""),
                "system_position_cap_pct": row.get("system_position_cap_pct", ""),
                "manual_override_required": row.get("manual_override_required", ""),
                "manual_action": row.get("manual_action", ""),
                "blocking_notes": row.get("blocking_notes", ""),
            })
            if len(rows) >= limit:
                break
        return rows


def read_csv_rows(path: Path, limit: int) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            rows.append(dict(row))
            if len(rows) >= limit:
                break
        return rows


def write_daily_decision_report_section() -> None:
    report = V471 / "report.md"
    if not report.exists():
        return
    section = render_daily_decision_section(build_daily_decision_summary())
    text = report.read_text(encoding="utf-8")
    start = "<!-- daily_decision_summary:start -->"
    end = "<!-- daily_decision_summary:end -->"
    block = f"{start}\n{section}\n{end}\n"
    if start in text and end in text:
        before, rest = text.split(start, 1)
        _, after = rest.split(end, 1)
        report.write_text(before + block + after.lstrip("\n"), encoding="utf-8")
        return
    lines = text.splitlines()
    insert_at = 1 if lines and lines[0].startswith("# ") else 0
    lines[insert_at:insert_at] = ["", block.rstrip(), ""]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_daily_decision_section(summary: dict[str, object]) -> str:
    window = summary.get("window", {}) if isinstance(summary.get("window"), dict) else {}
    leader = summary.get("industry_leader_selection", {}) if isinstance(summary.get("industry_leader_selection"), dict) else {}
    rows = leader.get("top_review_rows", []) if isinstance(leader.get("top_review_rows"), list) else []
    debts = leader.get("evidence_debt", []) if isinstance(leader.get("evidence_debt"), list) else []
    forward = leader.get("forward_archive", {}) if isinstance(leader.get("forward_archive"), dict) else {}
    settlement = leader.get("forward_settlement", {}) if isinstance(leader.get("forward_settlement"), dict) else {}
    live_guardrails = leader.get("live_guardrail_playbook", {}) if isinstance(leader.get("live_guardrail_playbook"), dict) else {}
    fund_flow = leader.get("fund_flow_overlay", {}) if isinstance(leader.get("fund_flow_overlay"), dict) else {}
    carrier_alternatives = leader.get("carrier_alternative_tracking", {}) if isinstance(leader.get("carrier_alternative_tracking"), dict) else {}
    review_packet = leader.get("pre_trade_review_packet", {}) if isinstance(leader.get("pre_trade_review_packet"), dict) else {}
    entry_readiness = leader.get("entry_readiness", {}) if isinstance(leader.get("entry_readiness"), dict) else {}
    operator = leader.get("operator_checklist", {}) if isinstance(leader.get("operator_checklist"), dict) else {}
    scorecard = leader.get("evaluation_scorecard", {}) if isinstance(leader.get("evaluation_scorecard"), dict) else {}
    random_baseline = leader.get("random_baseline_audit", {}) if isinstance(leader.get("random_baseline_audit"), dict) else {}
    bootstrap = leader.get("bootstrap_audit", {}) if isinstance(leader.get("bootstrap_audit"), dict) else {}
    state_guardrail = leader.get("state_guardrail", {}) if isinstance(leader.get("state_guardrail"), dict) else {}
    quarantine = leader.get("quarantine_overlay", {}) if isinstance(leader.get("quarantine_overlay"), dict) else {}
    tradeable_leader = leader.get("tradeable_leader_audit", {}) if isinstance(leader.get("tradeable_leader_audit"), dict) else {}
    remediation = leader.get("remediation_queue", {}) if isinstance(leader.get("remediation_queue"), dict) else {}
    goal = leader.get("goal_readiness", {}) if isinstance(leader.get("goal_readiness"), dict) else {}
    current_phase = leader.get("current_phase_readiness", {}) if isinstance(leader.get("current_phase_readiness"), dict) else {}
    scorecard_rows = leader.get("scorecard_rows", []) if isinstance(leader.get("scorecard_rows"), list) else []
    remediation_rows = leader.get("remediation_rows", []) if isinstance(leader.get("remediation_rows"), list) else []
    lines = [
        "## 每日实盘辅助摘要",
        "",
        f"- 决策状态：`{summary.get('decision_state', '')}`",
        f"- 当前动作：{summary.get('action', '')}",
        f"- 自动执行：`{str(summary.get('auto_execution_allowed', False)).lower()}`",
        f"- 反弹窗口：触发={window.get('latest_signal_triggered')}；生产就绪={window.get('production_ready')}；计划入场={window.get('planned_entry_date')}；计划退出={window.get('planned_exit_date')}",
        f"- 下一次检查：{window.get('next_action_date', '')}；{window.get('next_action', '')}",
        f"- 入场日前刷新命令：`python .\\scripts\\run_v4_71_live_refresh.py --trade-date {window.get('planned_entry_date', '')}`",
        f"- 退出日结算命令：`python .\\scripts\\settle_v4_72_rebound_leader_forward_returns.py --as-of-date {tradeable_leader.get('forward_planned_exit_date', window.get('planned_exit_date', ''))}`",
        f"- 强行业选择：状态=`{leader.get('best_status', '')}`；策略={leader.get('best_strategy', '')} Top{leader.get('best_top_n', '')}；平均相对收益={fmt_pct(leader.get('mean_relative_return'))}；胜率={fmt_pct(leader.get('win_rate'))}；Top分位命中率={fmt_pct(leader.get('top_quintile_hit_rate'))}",
        f"- As-of历史失败过滤：最佳变体={leader.get('asof_failure_filter_best_variant', '')}；是否过门槛=`{str(leader.get('asof_failure_filter_passes_gate', False)).lower()}`；平均相对收益={fmt_pct(leader.get('asof_failure_filter_best_mean_relative_return'))}；Top分位命中率={fmt_pct(leader.get('asof_failure_filter_best_top_quintile_hit_rate'))}；正收益年份={fmt_pct(leader.get('asof_failure_filter_best_positive_year_rate'))}",
        f"- 单因子发现：最佳因子={leader.get('factor_discovery_best_factor_label', '')} Top{leader.get('factor_discovery_best_top_n', '')}；是否过门槛=`{str(leader.get('factor_discovery_passes_gate', False)).lower()}`；平均相对收益={fmt_pct(leader.get('factor_discovery_best_mean_relative_return'))}；Top分位命中率={fmt_pct(leader.get('factor_discovery_best_top_quintile_hit_rate'))}；正收益年份={fmt_pct(leader.get('factor_discovery_best_positive_year_rate'))}",
        f"- 结构变化因子：最佳因子={leader.get('structure_factor_best_factor_label', '')} Top{leader.get('structure_factor_best_top_n', '')}；是否过门槛=`{str(leader.get('structure_factor_passes_gate', False)).lower()}`；平均相对收益={fmt_pct(leader.get('structure_factor_best_mean_relative_return'))}；Top分位命中率={fmt_pct(leader.get('structure_factor_best_top_quintile_hit_rate'))}",
        f"- V4.72前推归档：账本行数={forward.get('ledger_rows', '')}；待观察={forward.get('pending_forward_observations', '')}；重复键={forward.get('duplicate_keys', '')}",
        f"- V4.72前推结算：已结算={settlement.get('settled_rows', '')}；待观察={settlement.get('pending_rows', '')}；缺价格={settlement.get('missing_price_rows', '')}",
        f"- V4.71稳健性护栏：禁用扰动={live_guardrails.get('forbidden_runtime_override_count', '')}；审计支持={live_guardrails.get('audit_support_only_count', '')}；冷却期不足={live_guardrails.get('insufficient_independent_clusters_count', '')}；稀疏状态={live_guardrails.get('sparse_state_not_production_evidence_count', '')}",
        f"- 候选资金流观察：候选={fund_flow.get('candidate_count', '')}；精确覆盖={fund_flow.get('available_overlay_count', '')}；代理={fund_flow.get('proxy_overlay_count', '')}；真实缺失={fund_flow.get('missing_overlay_count', '')}；门禁失败={fund_flow.get('overlay_gate_fail_count', fund_flow.get('missing_overlay_count', ''))}",
        f"- 替代载体跟踪：审计载体={carrier_alternatives.get('row_count', '')}；可人工复核载体={carrier_alternatives.get('usable_alternative_count', '')}；可人工复核行业={carrier_alternatives.get('usable_alternative_industry_count', carrier_alternatives.get('usable_alternative_count', ''))}；自动执行=false",
        f"- 盘前复核包：P1={review_packet.get('p1_count', '')}；P2={review_packet.get('p2_count', '')}；P3={review_packet.get('p3_count', '')}",
        f"- 盘前结构筛选：结构可复核={review_packet.get('structural_reviewable_count', '')}；结构阻断={review_packet.get('structural_blocked_count', '')}；只观察={review_packet.get('structural_observe_only_count', '')}；历史最差重复={review_packet.get('repeated_worst_event_candidate_count', '')}",
        f"- V4.72入场就绪：未到期={entry_readiness.get('entry_not_due_count', '')}；阻断={entry_readiness.get('entry_blocked_count', '')}；预计入场日仍阻断={entry_readiness.get('projected_entry_blocked_count', '')}；可入场={entry_readiness.get('allowed_entry_count', '')}；自动执行={str(entry_readiness.get('auto_execution_allowed', '')).lower()}",
        f"- 当前入场总决策：`{entry_readiness.get('live_entry_decision', '')}`；{entry_readiness.get('live_entry_action', '')}",
        f"- 入场日规则：跳过={entry_readiness.get('entry_skip_rule_count', '')}；只观察={entry_readiness.get('entry_observe_only_rule_count', '')}；人工复核={entry_readiness.get('entry_manual_review_rule_count', '')}",
        f"- 盘前操作总清单：P0={operator.get('p0_count', '')}；P1={operator.get('p1_count', '')}；P2={operator.get('p2_count', '')}；自动执行={str(operator.get('auto_execution_allowed', '')).lower()}",
        f"- 结构通过但研究阻断池：{entry_readiness.get('tradeable_research_blocked_count', '')}；{entry_readiness.get('tradeable_research_blocked_industries', '')}",
        f"- 结构通过池强反弹审计：通过={tradeable_leader.get('evidence_pass_count', '')}/{tradeable_leader.get('target_count', '')}；最好上下文={tradeable_leader.get('best_context_industry', '')}；平均相对收益={fmt_pct(tradeable_leader.get('best_context_mean_relative_return'))}；Top分位命中={fmt_pct(tradeable_leader.get('best_context_top_quintile_hit_rate'))}",
        f"- 结构通过池前推观察：{tradeable_leader.get('forward_observation_count', '')}；状态={tradeable_leader.get('forward_observation_status', '')}；退出日={tradeable_leader.get('forward_planned_exit_date', '')}",
        f"- 风险隔离替补池：数量={quarantine.get('replacement_observation_count', '')}；Top={quarantine.get('top_replacement_industries', '')}；自动执行={str(quarantine.get('auto_execution_allowed', '')).lower()}",
        f"- 强行业随机基准：Top20缺口={random_baseline.get('top_quintile_success_gap', '')}；正年份缺口={random_baseline.get('positive_year_gap', '')}；相对胜率缺口={random_baseline.get('relative_win_success_gap', '')}",
        f"- 强行业Bootstrap：失败项={bootstrap.get('failed_metrics', '')}；通过={str(bootstrap.get('bootstrap_passes_gate', '')).lower()}",
        f"- 强行业状态护栏：失败桶={state_guardrail.get('failed_buckets', '')}",
        f"- 强反弹行业评价：失败={scorecard.get('fail_count', '')}；待结算={scorecard.get('pending_count', '')}；硬闸门失败={scorecard.get('hard_gate_fail_count', '')}；生产就绪={str(scorecard.get('production_ready', '')).lower()}",
        f"- 补证动作清单：P0={remediation.get('p0_count', '')}；P1={remediation.get('p1_count', '')}；P2={remediation.get('p2_count', '')}",
        f"- 总目标完成度：通过={goal.get('pass_count', '')}；失败={goal.get('fail_count', '')}；待结算={goal.get('pending_count', '')}；目标完成={str(goal.get('goal_ready', '')).lower()}",
        f"- 当前阶段完成度：通过={current_phase.get('pass_count', '')}；失败={current_phase.get('fail_count', '')}；当前阶段完成={str(current_phase.get('current_phase_ready', '')).lower()}；未来样本延后验证={str(current_phase.get('future_forward_validation_deferred', '')).lower()}",
        f"- 目标失败要求：{goal.get('failed_requirements', '')}",
        f"- 目标待结算要求：{goal.get('pending_requirements', '')}",
        f"- 目标硬停止：{goal.get('hard_stop_reasons', '')}",
        f"- 目标下一步：{goal.get('next_action_summary', '')}",
    ]
    blockers = window.get("blocking_issues", [])
    if blockers:
        lines += ["", "阻断项："]
        for item in blockers:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('check', '')}`：{item.get('evidence', '')}")
    if debts:
        lines += ["", "强行业选择证据债务："]
        for item in debts:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('blocker', '')}`：当前={item.get('current', '')}，要求={item.get('required', '')}；{item.get('live_decision_rule', '')}")
    if rows:
        lines += [
            "",
            "盘前优先复核行：",
            "",
            "| 优先级 | 门禁 | 系统仓位上限 | 行业 | 载体 | 备选状态 | 资金流 | 动作 |",
            "|:---|:---|:---|:---|:---|:---|:---|:---|",
        ]
        for row in rows:
            if isinstance(row, dict):
                carrier = f"{row.get('candidate_carrier_code', '')} {row.get('candidate_carrier_name', '')}".strip() or "无"
                lines.append(f"| {row.get('review_priority', '')} | {row.get('manual_gate_status', '')} | {row.get('system_position_cap_pct', '')}% | {row.get('industry_name', '')} | {carrier} | {row.get('carrier_fallback_status', '')} | {row.get('fund_flow_overlay_status', '')} | {row.get('manual_gate_action', '')} |")
    if scorecard_rows:
        lines += [
            "",
            "强反弹行业评价闸门：",
            "",
            "| 维度 | 指标 | 当前值 | 要求 | 状态 | 解释 |",
            "|:---|:---|:---|:---|:---|:---|",
        ]
        for row in scorecard_rows:
            if isinstance(row, dict):
                lines.append(f"| {row.get('dimension', '')} | {row.get('metric', '')} | {row.get('current', '')} | {row.get('required', '')} | {row.get('status', '')} | {row.get('interpretation', '')} |")
    if remediation_rows:
        lines += [
            "",
            "下一步补证动作：",
            "",
            "| 优先级 | 缺口 | 指标/门禁 | 行业 | 动作 | 到期规则 |",
            "|:---|:---|:---|:---|:---|:---|",
        ]
        for row in remediation_rows:
            if isinstance(row, dict):
                lines.append(f"| {row.get('priority', '')} | {row.get('gap_type', '')} | {row.get('related_metric', '')} | {row.get('industry_name', '')} | {row.get('next_action', '')} | {row.get('due_rule', '')} |")
    lines += ["", "边界：该摘要只做盘前研究辅助；`auto_execution_allowed=false` 时不得当作自动交易指令。"]
    return "\n".join(lines)


def fmt_pct(value: object) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return ""


if __name__ == "__main__":
    main()
