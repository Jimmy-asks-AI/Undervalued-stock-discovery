<!--
archive_record_type: read_only_migration
source_path: README.md
source_commit: 36cc42926a72d488116417e48e6107b544754d93
source_lines: 240-288
original_text_sha256: 3b211da3c12371fec3f057105ff34aa66c663eb315727f767550b037e1f286b0
hash_basis: UTF-8, LF line endings, terminal LF included
ignored_output_link_normalizations: 0
-->

# V4.70—V4.88 旧命令矩阵

> 本页为 README 历史正文的只读迁移件。正文事实、版本和数值按原文保留；仅将被忽略的 `outputs/` Markdown 链接改成行内路径，防止历史文档产生失效本地链接。迁移记录见 `docs/research_history_migration_manifest.json`。

<!-- BEGIN MIGRATED README LINES 240-288 -->
拆开运行如下：

```powershell
python .\scripts\run_industry_rebound_window_v4_61_relaxed_breadth_relief.py --config .\configs\rebound_window_v4_70_delayed_entry_vol_stop_policy.json
python .\scripts\evaluate_rebound_window_effectiveness.py --output-dir .\outputs\industry_rebound_window_v4_70_delayed_entry_vol_stop
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_window_v4_70_delayed_entry_vol_stop --required-debug-files delayed_entry_vol_stop_source_panel.csv realtime_simulation_trades.csv realtime_simulation_summary.csv walk_forward_year_summary.csv data_availability_audit.csv leakage_audit.csv evaluation_scorecard.csv evaluation_summary.json optimization_notes.json frozen_policy.json
python .\scripts\run_industry_rebound_window_v4_71_robustness_live_audit.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_window_v4_71_robustness_live_audit --required-debug-files source_panel.csv base_v4_70_trades.csv parameter_perturbation.csv parameter_failure_diagnosis.csv cooldown_sensitivity.csv annual_breakdown.csv market_state_breakdown.csv latest_signal_status.csv tradable_carrier_mapping.csv carrier_execution_replay.csv manual_carrier_review_sheet.csv pre_entry_manual_review.md carrier_mapping_audit.csv robustness_checks.csv data_availability_audit.csv leakage_audit.csv live_decision_packet.json forward_sample_tracker.json forward_sample_checklist.csv forward_sample_ledger_audit.csv pre_entry_gate.csv production_readiness_debt.csv live_refresh_manifest.json live_refresh_warnings.csv optimization_notes.json frozen_policy.json
python .\scripts\run_industry_rebound_leader_selection_v4_72.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_selection_v4_72 --required-debug-files industry_event_panel.csv strategy_results.csv annual_breakdown.csv latest_rebound_leader_candidates.csv leakage_audit.csv evaluation_gate_audit.csv failure_diagnosis.csv industry_candidate_carrier_mapping.csv carrier_mapping_audit.csv carrier_exposure_audit.csv carrier_tracking_audit.csv pre_trade_manual_review_sheet.csv
python .\scripts\run_industry_rebound_leader_state_gated_v4_73.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_state_gated_v4_73 --required-debug-files state_annotated_event_panel.csv state_gated_event_panel.csv state_gated_strategy_results.csv evaluation_gate_audit.csv latest_state_gated_candidates.csv
python .\scripts\run_industry_rebound_leader_oos_factor_v4_74.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_oos_factor_v4_74 --required-debug-files state_annotated_opportunity_set.csv factor_event_panel.csv factor_oos_results.csv selected_factor_oos_audit.csv evaluation_gate_audit.csv latest_oos_factor_candidates.csv
python .\scripts\run_industry_rebound_leader_walk_forward_v4_75.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_walk_forward_v4_75 --required-debug-files factor_event_panel.csv walk_forward_year_decisions.csv walk_forward_executed_events.csv walk_forward_annual_breakdown.csv evaluation_gate_audit.csv latest_walk_forward_candidates.csv
python .\scripts\run_industry_rebound_leader_fund_flow_readiness_v4_76.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_fund_flow_readiness_v4_76 --required-debug-files fund_flow_readiness_audit.csv candidate_fund_flow_inputs.csv current_fund_flow_observation_candidates.csv
python .\scripts\run_industry_rebound_leader_feature_separability_v4_77.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_feature_separability_v4_77 --required-debug-files feature_event_separability.csv feature_separability_results.csv evaluation_gate_audit.csv
python .\scripts\run_industry_rebound_leader_separable_portfolio_v4_78.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_separable_portfolio_v4_78 --required-debug-files separable_portfolio_event_panel.csv separable_portfolio_results.csv evaluation_gate_audit.csv latest_separable_portfolio_candidates.csv
python .\scripts\run_industry_rebound_leader_pressure_state_v4_79.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_pressure_state_v4_79 --required-debug-files pressure_state_event_panel.csv pressure_state_strategy_results.csv evaluation_gate_audit.csv robustness_audit.csv yearly_diagnostics.csv latest_pressure_state_candidates.csv
python .\scripts\run_industry_rebound_leader_robust_grid_v4_80.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_robust_grid_v4_80 --required-debug-files robust_grid_event_panel.csv robust_grid_results.csv robustness_detail.csv evaluation_gate_audit.csv
python .\scripts\run_industry_rebound_leader_market_state_v4_81.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_market_state_v4_81 --required-debug-files market_state_event_panel.csv market_state_grid_results.csv state_definition_audit.csv evaluation_gate_audit.csv
python .\scripts\audit_industry_fund_flow_source.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\audit\industry_fund_flow_source_audit --required-debug-files source_attempts.csv
python .\scripts\run_industry_rebound_leader_new_pit_source_v4_82.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_new_pit_source_v4_82 --required-debug-files source_readiness_audit.csv fund_flow_cache_audit.csv mapping_readiness_audit.csv pit_collection_plan.csv
python .\scripts\run_industry_rebound_leader_trap_guardrail_v4_83.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_trap_guardrail_v4_83 --required-debug-files guardrail_event_panel.csv guardrail_grid_results.csv guardrail_definition_audit.csv evaluation_gate_audit.csv
python .\scripts\run_industry_rebound_leader_structure_features_v4_84.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_structure_features_v4_84 --required-debug-files structure_event_panel.csv structure_grid_results.csv feature_coverage_audit.csv evaluation_gate_audit.csv
python .\scripts\run_industry_rebound_leader_parent_neutral_v4_85.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_parent_neutral_v4_85 --required-debug-files parent_neutral_event_panel.csv parent_neutral_grid_results.csv parent_mapping_audit.csv evaluation_gate_audit.csv
python .\scripts\run_industry_rebound_leader_parent_neutral_forward_v4_86.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_parent_neutral_forward_v4_86 --required-debug-files archived_forward_rows.csv forward_ledger_audit.csv frozen_parent_neutral_rule.csv forward_observation_plan.csv current_opportunity_set.csv
python .\scripts\settle_v4_85_parent_neutral_forward_returns.py --as-of-date 2026-06-20
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\audit\v4_85_parent_neutral_forward_settlement --required-debug-files settled_forward_rows.csv settlement_audit.csv pending_forward_rows.csv
python .\scripts\build_v4_85_parent_neutral_evidence_scorecard.py
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_evidence_scorecard_v4_87 --required-debug-files evidence_scorecard.csv forward_tracker_status.csv promotion_protocol.csv ledger_snapshot.csv
python .\scripts\build_v4_85_parent_neutral_pre_entry_audit.py --as-of-date 2026-06-20
python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_rebound_leader_pre_entry_audit_v4_88 --required-debug-files pre_entry_checks.csv candidate_consistency.csv pre_entry_action_plan.csv regenerated_current_candidates.csv
python .\scripts\audit_task_briefs.py
```


<!-- END MIGRATED README LINES 240-288 -->
