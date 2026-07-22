# Fundamental Value Research OS Version Changelog

## V0.1 - Scaffold And Runnable Prototype

Date: 2026-06-12

Added:

- Workspace scaffold for a fundamental-factor undervalued asset research system.
- Multi-agent role graph in `configs/fundamental_value_agents.json`.
- PIT data contract in `data_catalog/fundamental_value_data_contract.md`.
- Initial factor registry in `factor_library/fundamental_value_factor_registry.csv`.
- Runnable sample scorer in `strategy_lab/fundamental_value_os/agents.py`.
- Smoke runner in `scripts/run_fundamental_value_smoke.py`.

Validated:

- Python compilation passed for the scorer and smoke script.
- Smoke test generated ranked sample candidates.

Not Done:

- No real PIT data source.
- No historical IC, RankIC, group returns, OOS, costs, turnover, or capacity validation.
- No model promotion beyond `research_only`.

## V0.2 - Input Schema And Source Inventory

Date: 2026-06-12

Added:

- `data_catalog/input_asset_panel_schema.csv`
- `data_catalog/fundamental_value_source_inventory.csv`
- `data_catalog/input_asset_panel_example.csv`
- `scripts/validate_asset_panel_schema.py`

Validated:

- `python .\scripts\validate_asset_panel_schema.py --input .\data_catalog\input_asset_panel_example.csv --mode latest`
- Result: 6 rows, 0 issues, status pass.

Not Done:

- No external data acquisition.
- No PIT-mode pass, because the example panel remains `research_only`.
- No alpha or backtest claim.

## V0.3 - Factor Registry Audit Gate

Date: 2026-06-12

Added:

- `scripts/audit_factor_registry.py`
- Audit outputs under `outputs/factor_registry_audit/`

Validated:

- `python .\scripts\audit_factor_registry.py`
- Result: 18 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\run_fundamental_value_smoke.py --input .\data_catalog\input_asset_panel_example.csv --output .\outputs\fundamental_value_smoke_v0_2_example`
- Result: scoring pipeline accepts the V0.2 example panel and preserves the V0.1 sample ranking behavior.

Not Done:

- No factor performance evidence yet.
- No neutralization, RankIC, grouped returns, walk-forward, costs, turnover, or capacity engine yet.
- No candidate can be promoted beyond `candidate` or `research_only`.

Completed Next:

- V0.4 added machine-readable task briefs so each current gate has declared inputs, forbidden inputs, outputs, and acceptance checks.

## V0.4 - Task Brief Governance Gate

Date: 2026-06-12

Added:

- `configs/fundamental_value_task_brief_schema.json`
- `strategy_lab/agents/task_briefs/v0_1_sample_scorer_smoke.json`
- `strategy_lab/agents/task_briefs/v0_2_asset_panel_schema_validation.json`
- `strategy_lab/agents/task_briefs/v0_3_factor_registry_audit.json`
- `scripts/audit_task_briefs.py`

Validated:

- `python .\scripts\audit_task_briefs.py`
- Result: 3 task briefs, 0 errors, 0 warnings, status pass.

Not Done:

- No automated multi-agent scheduler yet.
- No run manifest with file hashes yet.
- No factor validation or model promotion.

Completed Next:

- V0.5 added a current A-share research-only snapshot adapter and candidate output. Reproducible file-hash manifests remain future work.

## V0.5 - Current A-Share Research-Only Snapshot

Date: 2026-06-12

Added:

- `scripts/run_current_a_share_value_snapshot.py`
- `strategy_lab/agents/task_briefs/v0_5_current_a_share_value_snapshot.json`
- Output directory `outputs/current_a_share_value_snapshot/`

Validated:

- `python .\scripts\run_current_a_share_value_snapshot.py --report-date 20260331 --trade-date 2026-06-12 --top 30`
- Result: 5,862 spot rows, 4,339 current research panel rows, 30 current-snapshot obvious-value candidates.
- `python .\scripts\validate_asset_panel_schema.py --input .\outputs\current_a_share_value_snapshot\asset_panel_current_research_only.csv --mode latest --output .\outputs\current_a_share_value_snapshot\schema_validation`
- Result: 4,339 rows, 0 issues, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 4 task briefs, 0 errors, 0 warnings, status pass.

Not Done:

- No PIT financial panel.
- No historical labels.
- No RankIC, group returns, neutralization, OOS, costs, turnover, or capacity tests.
- Several unavailable fundamentals are represented by explicit proxy or neutral placeholder fields.

Next:

- V0.6 should replace neutral proxy fields with richer current financial fields for balance-sheet leverage, interest coverage, payout ratio, and true FCF.

## V0.6 - Industry-Relative Cheapness Confirmation

Date: 2026-06-12

Added:

- `relative_cheapness_confirmation_auditor` inside `strategy_lab/fundamental_value_os/agents.py`.
- `relative_cheapness_confirmation_penalty` and `relative_value_trap_flag` fields in candidate ranking outputs.
- Current snapshot obvious-value filter now excludes rows with `relative_value_trap_flag=True`.

Validated:

- `python .\scripts\run_current_a_share_value_snapshot.py --report-date 20260331 --trade-date 2026-06-12 --top 30`
- Result: 4,339 current research panel rows, 226 industry-relative cheapness risk flags, 30 obvious-value candidates, 0 flags inside the top obvious-value list.
- `python .\scripts\validate_asset_panel_schema.py --input .\outputs\current_a_share_value_snapshot\asset_panel_current_research_only.csv --mode latest --output .\outputs\current_a_share_value_snapshot\schema_validation`
- Result: 4,339 rows, 0 issues, status pass.

Not Done:

- The guardrail is still based on current snapshot and proxy fields.
- It does not replace real PIT accounting-quality, governance, pledge, audit-opinion, related-party, and litigation checks.

## V0.7 - Undervalued-And-Quality Comparison

Date: 2026-06-12

Added:

- `scripts/run_quality_value_comparison.py`
- `strategy_lab/agents/task_briefs/v0_7_quality_value_comparison.json`
- `outputs/current_a_share_quality_value_snapshot/`

Validated:

- `python .\scripts\run_quality_value_comparison.py --top 30`
- Result: 53 full obvious-value candidates from V0.6, 12 stricter undervalued-and-quality candidates, 8 inside the previous Top 30 output, 4 outside the previous Top 30 output, and 22 previous Top 30 rows removed by the quality gate.
- Split: 11 financial-sector proxy-quality candidates and 1 non-financial current-snapshot quality candidate.

Not Done:

- Financial-sector quality is not fully validated because NPL ratio, capital adequacy, provisioning, broker risk exposures, and insurer embedded-value metrics are not connected.
- No PIT validation, historical labels, RankIC, group returns, OOS, costs, turnover, or capacity tests.

## V0.8 - Financial-Sector Value And Risk Proxy Iteration

Date: 2026-06-12

Added:

- `financial_sector_value_auditor` in `configs/fundamental_value_agents.json`.
- `factor_library/financial_sector_value_factor_framework.md`.
- Optional financial-sector fields in `data_catalog/input_asset_panel_schema.csv`.
- Financial-sector rows in `data_catalog/fundamental_value_source_inventory.csv`.
- 7 financial-sector factors in `factor_library/fundamental_value_factor_registry.csv`.
- `scripts/run_financial_value_iteration.py`.
- `strategy_lab/agents/task_briefs/v0_8_financial_value_iteration.json`.
- Output directory `outputs/current_a_share_financial_value_iteration/`.

Validated:

- `python .\scripts\run_financial_value_iteration.py --trade-date 2026-06-12 --top 20`
- Result: 11 V0.7 financial candidates, 11 proxy gate pass, 0 proxy gate fail, and 0 confirmed financial undervaluation candidates because sector-critical regulatory metrics are not connected.
- `python .\scripts\validate_asset_panel_schema.py --input .\data_catalog\input_asset_panel_example.csv --mode latest`
- Result: 6 rows, 0 issues, status pass.
- `python .\scripts\audit_factor_registry.py`
- Result: 25 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 7 task briefs, 0 errors, 0 warnings, status pass.

Not Done:

- Bank NPL ratio, provision coverage, capital adequacy, and NIM are not connected.
- Broker net capital, risk coverage, capital leverage, liquidity coverage, and net stable funding ratios are not connected.
- Insurer embedded value, new business value, solvency, and combined-ratio metrics are not connected.
- No PIT validation, historical labels, RankIC, group returns, OOS, costs, turnover, or capacity tests.

## V0.9 - Industry ETF Value Research OS

Date: 2026-06-12

Added:

- `configs/industry_etf_value_agents.json`.
- `configs/industry_etf_mapping_keywords.json`.
- `data_catalog/industry_etf_panel_schema.csv`.
- `data_catalog/industry_etf_source_inventory.csv`.
- `factor_library/industry_etf_factor_registry.csv`.
- `notes/Industry_ETF_Value_Research_OS_Blueprint.md`.
- `strategy_lab/industry_etf_value_os/`.
- `scripts/run_current_industry_etf_value_snapshot.py`.
- `strategy_lab/agents/task_briefs/v0_9_industry_etf_value_os.json`.
- Output directory `outputs/current_industry_etf_value_snapshot/`.

Changed:

- Primary research target switched from individual stocks to undervalued and oversold industries implemented with listed ETFs.
- Individual-stock artifacts remain available as historical components, but new candidate output is industry plus representative ETFs.
- ETF mapping is handled by a dedicated implementation agent and is explicitly marked as keyword-based V0.1 research proxy.

Validated:

- `python .\scripts\run_current_industry_etf_value_snapshot.py --trade-date 2026-06-12 --top 20`
- Result: 31 industry rows, 1,507 ETF spot rows, 3 current industry value-oversold candidates, 20 top output rows.
- Current candidates: 非银金融, 房地产, 石油石化.
- `python .\scripts\audit_factor_registry.py --registry .\factor_library\industry_etf_factor_registry.csv --schema .\data_catalog\industry_etf_panel_schema.csv --output .\outputs\industry_etf_factor_registry_audit`
- Result: 10 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 8 task briefs, 0 errors, 0 warnings, status pass.

Not Done:

- ETF mapping is keyword-based and can still require manual review.
- No PIT industry valuation history.
- No PIT ETF tracking-index mapping or ETF lifecycle history.
- No industry forward-return labels, RankIC, group returns, walk-forward, costs, slippage, turnover, capacity, or portfolio risk tests.

## V1.0 - SW Second-Industry Research Validation

Date: 2026-06-12

Added:

- `scripts/run_industry_etf_research_validation.py`.
- `scripts/audit_industry_etf_mapping.py`.
- `strategy_lab/agents/task_briefs/v1_0_industry_etf_research_validation.json`.
- SW second-industry fields and label fields in `data_catalog/industry_etf_panel_schema.csv`.
- Additional validation and mapping-confidence factors in `factor_library/industry_etf_factor_registry.csv`.
- Local cache under `data_catalog/cache/industry_etf/`.
- Output directory `outputs/industry_etf_research_validation/`.

Changed:

- V1 validation uses SW second industries while retaining SW first industry as `parent_industry`.
- Current PE, PB, and dividend yield are used only for current-snapshot explanation.
- Historical validation uses PIT price-derived features and forward return labels, not backfilled current valuation.
- ETF mapping now includes manual overrides, derived keywords, mapping confidence, and a dedicated audit.
- ETF mapping confidence now affects implementation score and value-trap penalty.

Validated:

- `python .\scripts\run_industry_etf_research_validation.py --industry-level second --horizons 60,120,252 --trade-date 2026-06-12 --top 30`
- Result: 131 SW second-industry rows, 17,026 historical feature rows, 19 current industry value-oversold candidates, and 3 RankIC horizon rows.
- Price-only historical RankIC: 60D mean 0.0022, 120D mean -0.0315, 252D mean -0.0589. This is not alpha validation for the current valuation composite.
- `python .\scripts\audit_industry_etf_mapping.py --input .\outputs\industry_etf_research_validation\debug\raw_industry_panel.csv --output .\outputs\industry_etf_research_validation\debug\etf_mapping_audit_check.json`
- Result: 131 rows, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_etf_research_validation --required-debug-files all_ranked_industries.csv raw_industry_panel.csv agent_results.json historical_feature_panel.csv rankic_report.csv group_return_report.csv topn_backtest.csv etf_mapping_audit.csv`
- Result: 0 errors, status pass.
- `python .\scripts\audit_factor_registry.py --registry .\factor_library\industry_etf_factor_registry.csv --schema .\data_catalog\industry_etf_panel_schema.csv --output .\outputs\industry_etf_factor_registry_audit`
- Result: 16 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 9 task briefs, 0 errors, 0 warnings, status pass.

Not Done:

- One SW second-industry history fetch currently fails from the free AkShare endpoint and is treated as a data gap.
- Historical valuation PIT data is still missing, so current PE/PB/dividend factors are not validated historically.
- ETF tracking-index PIT mapping and ETF lifecycle history remain missing.
- No promotion beyond `research_only`.

## V1.5 - Real-Data Industry ETF Validation and Portfolio Layer

Date: 2026-06-13

Added:

- V1.5 mode in `scripts/run_industry_etf_research_validation.py`.
- Real AkShare valuation snapshot archive under `data_catalog/cache/industry_etf/valuation_snapshots/`.
- `stabilized_oversold_signal` alongside the original `price_only_oversold_signal`.
- Validation verdict output: `debug/validation_decisions.csv`.
- Current portfolio-candidate simulation: `debug/current_portfolio.csv` and `debug/portfolio_summary.csv`.
- Real-data audit: `debug/real_data_audit.json`.
- V1.5 task brief: `strategy_lab/agents/task_briefs/v1_5_industry_etf_research_validation.json`.

Changed:

- Parent-industry keyword ETF mappings are now marked as review mappings and receive lower confidence.
- V1.5 candidate promotion requires ETF mapping quality and ETF turnover checks.
- `top_candidates.csv`, `report.md`, and `run_summary.json` remain compact and Chinese by default.

Validated:

- `python .\scripts\run_industry_etf_research_validation.py --industry-level second --horizons 60,120,252 --trade-date 2026-06-12 --top 30 --portfolio-size 8 --min-etf-turnover 50000000 --output .\outputs\test\industry_etf_research_validation_v1_5_real_data`
- Result: 131 SW second-industry rows, 1,507 ETF spot rows, 17,026 historical feature rows, 3 current V1.5 industry value-oversold candidates, 6 RankIC rows, and 3 current portfolio candidates.
- Real-data audit: `sample_or_mock_data_used=false`, AkShare valuation snapshot rows 131, ETF spot rows 1,507, one empty industry history (`801156`).
- V1.5 validation conclusion: `偏弱：存在方向性证据但未达到可推广门槛`.
- Compact output layout passed for the V1.5 real-data test output.

Not Done:

- The default output `outputs/industry_etf_research_validation/top_candidates.csv` was locked by an open Excel process during the run, so the verified V1.5 run was written to `outputs/test/industry_etf_research_validation_v1_5_real_data/`.
- Historical PE/PB/dividend-yield factor validation still requires accumulated PIT valuation snapshots.
- ETF tracking-index PIT mapping is still not connected.

## V1.5 - Default Output Refresh Completed

Date: 2026-06-13

Changed:

- Refreshed the default V1.5 output directory after the Excel file lock was released.
- Restored the V1.5 task brief required outputs from the temporary test path to `outputs/industry_etf_research_validation/`.
- Changed the default ETF mapping audit output to `outputs/audit/industry_etf_mapping_audit`.
- Removed the obsolete non-refresh V1.5 test output directory and stale debug audit-check file.
- Refreshed `data_catalog/manifests/research_manifest.csv` and `logs/reading_queue.csv`.

Validated:

- `python .\scripts\run_industry_etf_research_validation.py --industry-level second --horizons 60,120,252 --trade-date 2026-06-12 --top 30 --portfolio-size 8 --min-etf-turnover 50000000 --refresh-history`
- Result: 131 SW second-industry rows, 1,507 ETF spot rows, 17,026 historical feature rows, 3 current V1.5 candidates, 6 RankIC rows, and 3 current portfolio candidates.
- `debug/real_data_audit.json`: `real_data_only=true`, `sample_or_mock_data_used=false`, `refresh_history=true`.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_etf_research_validation --required-debug-files all_ranked_industries.csv raw_industry_panel.csv agent_results.json historical_feature_panel.csv rankic_report.csv group_return_report.csv topn_backtest.csv etf_mapping_audit.csv validation_decisions.csv current_portfolio.csv portfolio_summary.csv real_data_audit.json`
- Result: 0 errors, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 4 active task briefs, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_factor_registry.py --registry .\factor_library\industry_etf_factor_registry.csv --schema .\data_catalog\industry_etf_panel_schema.csv --output .\outputs\audit\industry_etf_factor_registry_audit`
- Result: 21 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_industry_etf_mapping.py`
- Result: 131 rows, 0 errors, 109 review warnings, status pass.

Status:

- V1.5 implementation and real-data test are complete.
- V1.5 remains research-only; validation conclusion is `偏弱：存在方向性证据但未达到可推广门槛`.

## V1.6 - ETF Mapping Governance

Date: 2026-06-13

Added:

- Manual ETF whitelist and blacklist governance in `configs/industry_etf_mapping_keywords.json`.
- V1.6 mapping fields: `mapping_evidence_level`, `mapping_review_required`, `mapping_review_reason`, `mapping_whitelist_codes`, and `mapping_blacklist_hits`.
- `debug/etf_mapping_review_queue.csv` for non-pass mappings requiring human tracking-index or holdings review.
- `debug/etf_mapping_coverage_summary.csv` for mapping status, source, and evidence-level counts.
- V1.6 task brief: `strategy_lab/agents/task_briefs/v1_6_industry_etf_mapping_governance.json`.

Changed:

- `scripts/run_industry_etf_research_validation.py` version updated to 1.6.0.
- V1.6 candidate gate now uses mapping audit status, manual review flags, mapping confidence, and ETF turnover.
- `scripts/audit_industry_etf_mapping.py` now audits V1.6 mapping governance fields and excluded cross-border tokens.
- V1.5 task brief moved to `strategy_lab/agents/task_briefs/archive/`.
- Old V1.5 test output directory was removed; current test output is `outputs/test/industry_etf_research_validation_v1_6_mapping_governance/`.

Validated:

- `python .\scripts\run_industry_etf_research_validation.py --industry-level second --horizons 60,120,252 --trade-date 2026-06-12 --top 30 --portfolio-size 8 --min-etf-turnover 50000000 --refresh-history`
- Result: 131 SW second-industry rows, 1,507 ETF spot rows, 17,026 historical feature rows, 5 current V1.6 candidates, 6 RankIC rows, and 5 current portfolio candidates.
- V1.6 candidates: 保险Ⅱ, 房地产开发, 游戏Ⅱ, 证券Ⅱ, 白酒Ⅱ.
- Mapping audit: 9 pass, 107 requires_review, 15 no_tradeable_etf, 0 fail.
- `debug/real_data_audit.json`: `real_data_only=true`, `sample_or_mock_data_used=false`, `refresh_history=true`.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_etf_research_validation --required-debug-files all_ranked_industries.csv raw_industry_panel.csv agent_results.json historical_feature_panel.csv rankic_report.csv group_return_report.csv topn_backtest.csv etf_mapping_audit.csv etf_mapping_review_queue.csv etf_mapping_coverage_summary.csv validation_decisions.csv current_portfolio.csv portfolio_summary.csv real_data_audit.json`
- Result: 0 errors, status pass.
- `python .\scripts\audit_industry_etf_mapping.py --input .\outputs\industry_etf_research_validation\debug\raw_industry_panel.csv --output .\outputs\audit\industry_etf_mapping_audit`
- Result: 131 rows, 0 errors, 97 review warnings, status pass.
- `python .\scripts\audit_factor_registry.py --registry .\factor_library\industry_etf_factor_registry.csv --schema .\data_catalog\industry_etf_panel_schema.csv --output .\outputs\audit\industry_etf_factor_registry_audit`
- Result: 23 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 4 active task briefs, 0 errors, 0 warnings, status pass.

Status:

- V1.6 implementation and real-data test are complete.
- V1.6 remains research-only; validation conclusion is `偏弱：存在方向性证据但未达到可推广门槛`.

## V1.6.1 - Debug Review Queue Processing

Date: 2026-06-13

Changed:

- Added `manual_no_tradeable_industries` to `configs/industry_etf_mapping_keywords.json`.
- Added direct/close manual ETF mappings for 养殖业, 中药Ⅱ, and 基础建设.
- Marked 医药商业, 燃气Ⅱ, 多元金融, 饲料, 房地产服务, and 农化制品 as currently having no suitable second-industry ETF carrier.
- Changed blacklist-hit logging to record only ETFs that would otherwise match the industry.
- Added `etf_manual_no_tradeable_mapping` to `factor_library/industry_etf_factor_registry.csv`.
- Updated README V1.6.1 notes.

Validated:

- `python .\scripts\run_industry_etf_research_validation.py --industry-level second --horizons 60,120,252 --trade-date 2026-06-12 --top 30 --portfolio-size 8 --min-etf-turnover 50000000 --refresh-history`
- Result: 131 SW second-industry rows, 1,507 ETF spot rows, 17,026 historical feature rows, 6 current V1.6.1 candidates, 6 RankIC rows, and 6 current portfolio candidates.
- V1.6.1 candidates: 保险Ⅱ, 房地产开发, 游戏Ⅱ, 养殖业, 证券Ⅱ, 白酒Ⅱ.
- Mapping audit: 12 pass, 98 requires_review, 21 no_tradeable_etf, 0 fail.
- Mapping audit warnings: 88.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_etf_research_validation --required-debug-files all_ranked_industries.csv raw_industry_panel.csv agent_results.json historical_feature_panel.csv rankic_report.csv group_return_report.csv topn_backtest.csv etf_mapping_audit.csv etf_mapping_review_queue.csv etf_mapping_coverage_summary.csv validation_decisions.csv current_portfolio.csv portfolio_summary.csv real_data_audit.json`
- Result: 0 errors, status pass.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\test\industry_etf_research_validation_v1_6_mapping_governance --required-debug-files all_ranked_industries.csv raw_industry_panel.csv agent_results.json historical_feature_panel.csv rankic_report.csv group_return_report.csv topn_backtest.csv etf_mapping_audit.csv etf_mapping_review_queue.csv etf_mapping_coverage_summary.csv validation_decisions.csv current_portfolio.csv portfolio_summary.csv real_data_audit.json`
- Result: 0 errors, status pass.
- `python .\scripts\audit_industry_etf_mapping.py --input .\outputs\industry_etf_research_validation\debug\raw_industry_panel.csv --output .\outputs\audit\industry_etf_mapping_audit`
- Result: 131 rows, 0 errors, 88 review warnings, status pass.
- `python .\scripts\audit_factor_registry.py --registry .\factor_library\industry_etf_factor_registry.csv --schema .\data_catalog\industry_etf_panel_schema.csv --output .\outputs\audit\industry_etf_factor_registry_audit`
- Result: 24 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 4 active task briefs, 0 errors, 0 warnings, status pass.

Status:

- V1.6.1 debug processing is complete.
- V1.6.1 remains research-only; validation conclusion is `偏弱：存在方向性证据但未达到可推广门槛`.

## V1.7 - Industry Index Research Mainline

Date: 2026-06-13

Added:

- `configs/industry_index_value_agents.json`.
- `data_catalog/industry_index_panel_schema.csv`.
- `data_catalog/industry_index_source_inventory.csv`.
- `factor_library/industry_index_factor_registry.csv`.
- `strategy_lab/industry_index_research_os/`.
- `scripts/run_industry_index_research_validation.py`.
- Output directory `outputs/industry_index_research_validation/`.

Changed:

- Current mainline now researches only SW industries and industry indexes.
- Current scoring uses valuation, oversold, cycle-quality, data-quality, and value-trap controls.
- Current report and candidate table no longer include implementation-carrier fields.
- Current validation uses price-derived PIT features and forward returns only.
- README was rewritten around the V1.7 industry-index workflow.
- Old implementation-carrier scripts, config, schema, registry, active outputs, and old cache were removed from the current workspace mainline.
- Old V0.9 and V1.6 active task briefs were moved to archive.

Validated:

- `python .\scripts\run_industry_index_research_validation.py --industry-level second --horizons 60,120,252 --trade-date 2026-06-12 --top 30 --candidate-count 8 --refresh-history`
- Result: 131 SW second-industry rows, 17,026 historical feature rows, 28 current V1.7 candidates, 6 RankIC rows, and 8 current research-basket rows.
- Research basket: 厨卫电器, 水泥, 特钢Ⅱ, 商用车, 医药商业, 基础建设, 房屋建设Ⅱ, 保险Ⅱ.
- `debug/real_data_audit.json`: `real_data_only=true`, `sample_or_mock_data_used=false`, `refresh_history=true`, `empty_history_count=1`, `short_history_count=7`.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_index_research_validation --required-debug-files all_ranked_industries.csv raw_industry_panel.csv agent_results.json historical_feature_panel.csv rankic_report.csv group_return_report.csv topn_backtest.csv validation_decisions.csv current_research_basket.csv research_basket_summary.csv real_data_audit.json`
- Result: 0 errors, status pass.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\test\industry_index_research_validation_v1_7 --required-debug-files all_ranked_industries.csv raw_industry_panel.csv agent_results.json historical_feature_panel.csv rankic_report.csv group_return_report.csv topn_backtest.csv validation_decisions.csv current_research_basket.csv research_basket_summary.csv real_data_audit.json`
- Result: 0 errors, status pass.
- `python .\scripts\audit_factor_registry.py --registry .\factor_library\industry_index_factor_registry.csv --schema .\data_catalog\industry_index_panel_schema.csv --output .\outputs\audit\industry_index_factor_registry_audit`
- Result: 17 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 3 active task briefs, 0 errors, 0 warnings, status pass.

Status:

- V1.7 implementation and real-data test are complete.
- V1.7 remains research-only; validation conclusion is `偏弱：存在方向性证据但未达到可推广门槛`.

## V2.3 - Pressure Quality Reversal Validation

Date: 2026-06-13

Added:

- `scripts/run_industry_pressure_quality_v2_3.py`.
- `outputs/industry_pressure_quality_v2_3/`.
- V2.3 price-quality proxy fields in `data_catalog/industry_index_panel_schema.csv`.
- V2.3 observation factors in `factor_library/industry_index_factor_registry.csv`.
- `strategy_lab/agents/task_briefs/v2_3_pressure_quality_validation.json`.

Changed:

- Current mainline moved from V2.2 pressure reversal to V2.3 pressure quality reversal.
- V2.2 active task brief was moved to archive.
- README now points to the V2.3 runner and output directory.

Validated:

- `python .\scripts\run_industry_pressure_quality_v2_3.py`
- Result: 17,026 feature rows, 8,955 event rows, 1,752 non-overlapping rows, 6,679 daily NAV rows, 128 pressure episodes, 0 candidate signals, 39 conditional observations, and 24 rejected standalone signals.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_pressure_quality_v2_3 --required-debug-files pressure_quality_signal_panel.csv event_backtest.csv nonoverlap_backtest.csv walk_forward_oos.csv bootstrap_confidence.csv stress_episode_report.csv pressure_episode_summary.csv daily_portfolio_nav.csv portfolio_nav_metrics.csv parameter_sensitivity.csv quality_filter_impact.csv momentum_trap_cases.csv signal_rejection_log.csv`
- Result: 0 errors, status pass.
- `python .\scripts\audit_factor_registry.py --registry .\factor_library\industry_index_factor_registry.csv --schema .\data_catalog\industry_index_panel_schema.csv --output .\outputs\audit\industry_index_factor_registry_audit`
- Result: 31 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 3 active task briefs, 0 errors, 0 warnings, status pass.

Status:

- V2.3 remains research-only.
- Price-quality filtering improved some full-sample and bootstrap statistics, but did not pass sample-size, non-overlap, out-of-sample, and daily NAV promotion gates.
- No V2.3 signal is promoted to alpha.

## V2.4 - Current Fundamental Pressure Candidate Layer

Date: 2026-06-13

Added:

- `scripts/run_industry_fundamental_pressure_v2_4.py`.
- `outputs/industry_fundamental_pressure_v2_4/`.
- V2.4 current valuation snapshot, current price-quality, current pressure, value-trap proxy, and PIT readiness fields in `data_catalog/industry_index_panel_schema.csv`.
- V2.4 observation factors in `factor_library/industry_index_factor_registry.csv`.
- `strategy_lab/agents/task_briefs/v2_4_fundamental_pressure_validation.json`.

Changed:

- Current mainline moved from V2.3 price-quality pressure validation to V2.4 current fundamental-pressure candidate explanation.
- V2.3 active task brief was moved to archive.
- README now points to the V2.4 runner and output directory.

Validated:

- `python .\scripts\run_industry_fundamental_pressure_v2_4.py`
- Result: 131 current industry rows, 131 current valuation-covered rows, 1 archived valuation snapshot, 11 current snapshot candidates, 21 valuation watchlist rows, and 39 oversold-without-valuation-support rows.
- Current valuation snapshot date: 2026-06-12.
- Current market pressure: `普通状态`, pressure score 0.546.
- PIT valuation status: `current_snapshot_only_not_pit`; minimum required valuation snapshots remain 60.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_fundamental_pressure_v2_4 --required-debug-files current_fundamental_pressure_panel.csv valuation_snapshot_coverage.csv historical_signal_evidence.csv pit_readiness_audit.csv candidate_decision_log.csv current_market_pressure_context.json`
- Result: 0 errors, status pass.
- `python .\scripts\audit_factor_registry.py --registry .\factor_library\industry_index_factor_registry.csv --schema .\data_catalog\industry_index_panel_schema.csv --output .\outputs\audit\industry_index_factor_registry_audit`
- Result: 35 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 3 active task briefs, 0 errors, 0 warnings, status pass.

Status:

- V2.4 remains research-only.
- Current PE, PB, and dividend yield are used only for current candidate explanation.
- No current valuation field is backfilled into historical validation.
- PIT valuation validation is blocked until enough daily valuation snapshots have accumulated or a reliable historical valuation source is connected.

## V2.4.iterative.3 - Three-Round Backtest Review Workflow

Date: 2026-06-13

Added:

- `scripts/run_industry_fundamental_pressure_iterative_v2_4.py`.
- `outputs/industry_fundamental_pressure_iterative_v2_4/`.
- V2.4 iterative signal, gate, and review fields in `data_catalog/industry_index_panel_schema.csv`.
- V2.4 iterative validation factors in `factor_library/industry_index_factor_registry.csv`.
- `strategy_lab/agents/task_briefs/v2_4_iterative_backtest_review.json`.

Changed:

- Current standard workflow is now three rounds of `backtest -> report -> review -> next iteration`.
- The workflow refreshes the V2.4 current snapshot first, but historical testing still excludes current PE, PB, and dividend yield.
- `strategy_lab/agents/task_briefs/v2_4_fundamental_pressure_validation.json` was moved to archive.
- README now documents the V2.4 iterative runner and compact output audit command.

Validated:

- `python .\scripts\run_industry_fundamental_pressure_iterative_v2_4.py`
- Result: 17,026 feature rows, 192 event rows, 3 iterations, 0 candidate signals, 10 conditional observations, 2 rejected parameter combinations.
- Iteration 1 best: Top5/252, full-sample relative +2.33%, OOS +1.03%, non-overlap +1.44%, bootstrap 5% lower bound +0.28%, relative NAV 0.989, sample strength 10.00%.
- Iteration 2 best: Top10/252, full-sample relative +3.49%, OOS +3.49%, non-overlap +3.49%, relative NAV 1.019, but only one effective sample and no valid bootstrap confidence.
- Iteration 3 best: Top5/120, full-sample relative +4.92%, OOS +3.28%, non-overlap +2.88%, bootstrap 5% lower bound +1.79%, relative NAV 0.985, sample strength 5.16%.

Status:

- V2.4.iterative.3 remains research-only.
- No signal is promoted to alpha.
- Positive event returns are not enough because sample strength and daily relative NAV remain weak.
- The next research direction is PIT valuation snapshot accumulation or reliable historical industry valuation data, not further parameter additions.

## V2.5 - Industry Quality Proxy and Historical Valuation Route Audit

Date: 2026-06-14

Added:

- `scripts/run_industry_quality_proxy_v2_5.py`.
- `outputs/industry_quality_proxy_v2_5/`.
- Public SWS second-industry daily valuation cache under `data_catalog/cache/industry_index/valuation_history/second/`.
- V2.5 quality proxy and valuation coverage fields in `data_catalog/industry_index_panel_schema.csv`.
- V2.5 quality proxy factors in `factor_library/industry_index_factor_registry.csv`.
- `strategy_lab/agents/task_briefs/v2_5_industry_quality_proxy_data_collection.json`.

Changed:

- Current mainline moved from V2.4 iterative price-pressure proxy review to V2.5 historical valuation data collection and quality proxy.
- `strategy_lab/agents/task_briefs/v2_4_iterative_backtest_review.json` was moved to archive.
- README now documents V2.5 runner, public valuation cache, route status, and compact output audit command.

Validated:

- `python .\scripts\run_industry_quality_proxy_v2_5.py`
- Result: 250,306 public historical valuation rows, valuation range 2015-01-05 to 2026-06-12, 131 covered second-level industries, 131 current quality rows, 104 generic quality-proxy passes, 12 sector-data-required flags, and 11 V2.5 current observations.
- Public route status: `collected_public_sws_daily_analysis`.
- Vendor route status: `not_collected_credentials_missing`.
- Stock reconstruction route status: `not_collected_pit_membership_and_stock_fundamental_history_missing`.
- `python .\scripts\audit_compact_output_layout.py --output-dir .\outputs\industry_quality_proxy_v2_5 --required-debug-files industry_quality_proxy_panel.csv quality_proxy_components.csv valuation_data_route_audit.csv vendor_connector_audit.csv stock_reconstruction_route_audit.csv public_source_collection_audit.csv pit_valuation_coverage.csv data_collection_log.json`
- Result: 0 errors, status pass.
- `python .\scripts\audit_factor_registry.py --registry .\factor_library\industry_index_factor_registry.csv --schema .\data_catalog\industry_index_panel_schema.csv --output .\outputs\audit\industry_index_factor_registry_audit`
- Result: 46 factors, 0 errors, 0 warnings, status pass.
- `python .\scripts\audit_task_briefs.py`
- Result: 3 active task briefs, 0 errors, 0 warnings, status pass.

Status:

- V2.5 remains research-only.
- Public SWS history is usable as a candidate PIT valuation source for the next validation stage, pending source-mouth audit, release-lag audit, and cross-source consistency checks.
- V2.5 quality proxy is not complete fundamental quality and does not promote any signal to alpha.

## V2.6 - Historical Valuation PIT Candidate Validation

Date: 2026-06-14

Added:

- `scripts/run_industry_valuation_pit_validation_v2_6.py`.
- `outputs/industry_valuation_pit_validation_v2_6/`.
- V2.6 PIT valuation fields in `data_catalog/industry_index_panel_schema.csv`.
- V2.6 valuation PIT factors in `factor_library/industry_index_factor_registry.csv`.
- `strategy_lab/agents/task_briefs/v2_6_valuation_pit_validation.json`.

Changed:

- Current mainline moved from V2.5 historical valuation collection and quality proxy to V2.6 historical valuation PIT candidate validation.
- `strategy_lab/agents/task_briefs/v2_5_industry_quality_proxy_data_collection.json` was moved to archive.
- README now documents the V2.6 runner, output directory, compact output audit command, and current research conclusion.

Validated:

- `python .\scripts\run_industry_valuation_pit_validation_v2_6.py`
- Result: 250,306 valuation rows, 9,007 signal-panel rows after minimum cross-section filtering, 2,214 event-backtest rows, 440 non-overlapping rows, and 7,130 daily NAV rows.
- Validation setup: `valuation_available_date = valuation_trade_date + 1 calendar day`, minimum cross-section count 20, horizons 60/120/252, TopN 5/10/20, one-way cost 10 bps.
- Result status: 0 `candidate_requires_source_audit`, 15 `conditional_observation`, 18 `rejected_signal`.
- Best observation: `V2.6：纯估值PIT` Top20/60, full-sample relative +1.29%, OOS relative +1.17%, non-overlap relative +1.07%, relative NAV 1.091, but bootstrap 5% lower bound -0.15% and non-overlap sample count only 10.
- Strongest RankIC evidence: valuation quality proxy on 252d horizon, mean RankIC +5.31%, t-stat 4.46, positive ratio 62.72%.
- Blocking evidence: no signal passed bootstrap lower-bound, sample-strength, source-audit, and strict promotion gates together.

Status:

- V2.6 remains research-only.
- Historical valuation data is useful and directionally informative, especially on 252-day valuation-quality RankIC, but not enough for alpha promotion.
- Next iteration should improve data alignment and source validation rather than adding more signal parameters.

## V2.7 - Valuation Quality Validation

Date: 2026-06-14

Added:

- `scripts/run_industry_valuation_quality_v2_7.py`.
- `outputs/industry_valuation_quality_v2_7/`.
- V2.7 valuation-quality fields in `data_catalog/industry_index_panel_schema.csv`.
- V2.7 valuation-quality factors in `factor_library/industry_index_factor_registry.csv`.
- `strategy_lab/agents/task_briefs/v2_7_valuation_quality_validation.json`.

Changed:

- Current mainline moved from V2.6 low-valuation PIT candidate validation to V2.7 valuation-quality validation.
- `strategy_lab/agents/task_briefs/v2_6_valuation_pit_validation.json` was moved to archive.
- Default TopN moved to 20/30 to avoid V2.x tail-basket overfitting.
- V2.7 explicitly separates the broad control from sector-exclusion variants for banks, non-bank financials, real estate, and construction.

Validated:

- `python .\scripts\run_industry_valuation_quality_v2_7.py`
- Result: 9,007 signal-panel rows, 657 event-backtest rows, 129 non-overlapping rows, and 2,287 daily NAV rows.
- Result status: 0 `candidate_requires_source_audit`, 10 `conditional_observation`, 2 `rejected_signal`.
- Best observation: `V2.7：质量宽口径对照` Top30/60, full-sample relative +0.48%, OOS relative -0.03%, non-overlap relative +0.45%, bootstrap 5% lower bound -1.09%, relative NAV 1.023.
- Strongest RankIC evidence: `quality_value_no_trap_score` on 252d horizon, mean RankIC +6.46%, t-stat 4.55, positive ratio 61.65%.

Status:

- V2.7 remains research-only.
- Long-horizon valuation-quality ranking information is stronger than the event portfolio evidence.
- No signal is promoted to alpha because OOS, bootstrap lower bound, source audit, and strict promotion gates do not pass together.

## V2.8 - RankIC to Portfolio Bridge Validation

Date: 2026-06-14

Added:

- `scripts/run_industry_rankic_portfolio_bridge_v2_8.py`.
- `outputs/industry_rankic_portfolio_bridge_v2_8/`.
- V2.8 bridge validation fields in `data_catalog/industry_index_panel_schema.csv`.
- V2.8 bridge validation factors in `factor_library/industry_index_factor_registry.csv`.
- `strategy_lab/agents/task_briefs/v2_8_rankic_portfolio_bridge.json`.

Changed:

- Current mainline moved from V2.7 valuation-quality validation to V2.8 RankIC-to-portfolio bridge validation.
- `strategy_lab/agents/task_briefs/v2_7_valuation_quality_validation.json` was moved to archive.
- V2.8 does not add new ranking formulas; it reuses V2.7 factors and tests Top-Bottom, long-relative, low-frequency rebalance, sector-exclusion attribution, quantile monotonicity, and same-date capacity.

Validated:

- `python .\scripts\run_industry_rankic_portfolio_bridge_v2_8.py`
- Result: 9,007 signal-panel rows, 1,314 bridge-event rows, 54 portfolio combinations, 0 candidate signals, 32 conditional observations, and 22 rejected signals.
- Best observation: `V2.6纯估值对照` broad Top20/252, full-sample Top-Bottom +2.28%, OOS Top-Bottom -7.71%, daily Top-Bottom NAV 1.119, bootstrap 5% lower bound -5.98%, quantile Top-Bottom -4.02%.
- Strongest RankIC bridge evidence: `质量价值非陷阱` sector-excluded 252d RankIC +8.14%, t-stat 5.56, positive ratio 68.24%.
- Same-date capacity audit: legacy 2015-2021 broad sample average eligible industries 29.50, max 39, so Top20-Bottom20 cannot be constructed in the old universe; bridge portfolio events are effectively 2022+.

Status:

- V2.8 remains research-only.
- RankIC information exists, but current evidence does not translate into robust portfolio alpha.
- The main blockers are OOS decay, negative bootstrap lower bounds, weak quantile tail spread, and same-date capacity limits in the legacy universe.

## 2026-07-18 - Retrospective Research Inventory And Current Governance Baseline

record_type: `retrospective_inventory`

recorded_at: `2026-07-18`

historical_timestamp_claimed: `false`

post_hoc: `true`

This entry was written during the 2026-07-18 governance remediation. It inventories existing artifacts; it does not claim that the text below existed when the historical experiments ran, and it does not convert any historical result into a preregistered experiment.

Historical bridge:

- V2.9 through V4.71 are retained in the repository and the archived README history. Their original artifacts and task briefs remain evidence, but this bridge entry is retrospective and does not reconstruct unknown historical timestamps.
- V4.70 is the frozen timing-strategy candidate. V4.71 is its robustness audit and remains `production_ready=false`.

Machine inventory coverage:

- V4.72, V4.73, V4.74, V4.75, V4.76, V4.77, V4.78, V4.79
- V4.80, V4.81, V4.82, V4.83, V4.84, V4.85, V4.86, V4.87
- V4.88, V4.89, V4.90, V4.91, V4.92, V4.93, V4.94, V4.95
- V4.96, V4.97, V4.98, V4.99
- V5.00, V5.01, V5.02, V5.03, V5.04, V5.05, V5.06, V5.07
- V5.08, V5.09, V5.10, V5.11, V5.12, V5.13, V5.14, V5.15
- V5.16, V5.17, V5.18, V5.19, V5.20, V5.21, V5.22, V5.23
- V5.24, V5.25, V5.26, V5.27, V5.28, V5.29, V5.30, V5.31
- V5.32, V5.33, V5.34, V5.35
- CURRENT_MAINLINE

Registration boundary:

- V4.72 through V5.03 and V5.11 through V5.35 are recorded conservatively as explicit retrospective or post-hoc inventory; no historical preregistration is claimed.
- V5.04 is the only covered research version with two hash-ledger `preregistered_forward_only` rules, both registered on 2026-07-12.
- V5.05 through V5.10 inherit those two frozen rules only for forward tracking, settlement, promotion, signal detection and goal auditing. Inheritance does not preregister new historical findings.
- `CURRENT_MAINLINE` inherits the same forward-only boundary and remains a current operating orchestrator, not a new strategy version.

Added:

- Deterministic `research_version_inventory.csv/json` for 64 historical versions plus `CURRENT_MAINLINE`.
- Retrospective task-brief records that carry the actual remediation date and explicitly deny historical timestamp claims.
- Fail-closed current-state, research-governance and Markdown-link audits with compact standard outputs.
- `CURRENT_STATUS.md` as the generated single-page state entry.
- A lossless README history archive with source-line and SHA-256 migration evidence.

Changed:

- The daily runner now refreshes eleven governed inputs, rebuilds V5.21 after V5.07, and then runs the single final V5.10 target audit.
- The full refresh generates V5.21 before its single final V5.10 run, so goal evidence cannot silently read the previous generation.
- External terminology is standardized as “六角色确定性否决链”.
- Verified active pointers no longer retain mutually exclusive invalidation metadata.

Validated:

- The active fund-flow integrity cohort is `ff_integrity_v5_20260718`; its immutable manifest is independently reverified after creation.
- All covered cohort-aware summaries bind to the same active `(cohort_id, manifest_hash)` pair.
- The current runner remains `research_only / NO_ACTION`; strong-industry Alpha, manual decision support and production readiness remain unvalidated or false, and automatic execution remains prohibited.

Status:

- This governance record changes documentation, traceability and failure semantics only.
- It does not change strategy thresholds, TopN, promotion criteria or any investment conclusion.

## 2026-07-18 - PIT 估值与行业历史口径整改

Added:

- 严格估值 PIT 合同：六个核心可见性字段、来源哈希/版本、修订链和冻结 A 股交易日历。
- `pit_universe_methodology_remediation` 标准审计，覆盖估值来源隔离、行业宇宙断点、代码身份分段、131/120 双门槛和三种只读稳健性视图。
- 行业历史方法审计及 30 个专项正反例测试。

Changed:

- SWS 日频历史由“PIT 候选”降级为 `not_pit_publication_time_missing`；禁止以 `trade_date+lag` 伪造 `available_date`。
- 2026-06-12 的 131 行 V2.5 回收快照从官方历史中隔离，直接来源截止恢复为 2025-12-31。
- V2.6、V5.11、V5.12、V4.30 对缺证据历史失败关闭；V4.72/V4.84 屏蔽未验证估值字段。
- V5.20、V5.10、当前 ETF 辅助 runner、完成度审计和 CURRENT_STATUS 接入同一方法门。
- 当前最小刷新链由 11 项扩为 16 项，方法审计必须先于 V5.11、V5.12、V5.20 和最终 V5.10。

Result:

- 方法控制审计 12/12 通过，但 `promotion_gate_passed=false`、可晋级估值行 0、历史分类状态 `unavailable`。
- 2015、2022、2023 的行业横截面断点已显式分栏；35 个复用代码分为 166 个观察身份段。
- 当前维持 `research_only / NO_ACTION`；未新增因子、阈值、TopN 或策略版本。

Final validation:

- 冻结队列切换到 `ff_integrity_v6_20260718`，manifest 为 `4d785b55ecb2ed56ea2e5e9a15aa0e1ba1c080e97b27b7d4a455d353c507d777`，复验 `changed_count=0`。
- 当前主线为 8 个硬阻断；工程合同 29/29、就绪门禁 6/13、状态一致性 34/34、行为测试 23/23、自检 12/12。
- Python 默认离线测试 196 passed、1 deselected；研究治理覆盖 65/65；方法标准输出结构审计 errors=0。

## 2026-07-18 - PIT 终态门禁复核与 v7 冻结基线

Added:

- 不可变原始估值文件路径与现场 SHA-256 复算；默认 A 股交易日历绑定固定内容哈希。
- V4.30、V4.84 的 `available_date` 消费反例，以及 V4.72 恢复型快照与全空估值候选反例。
- 历史方法晋级凭证与 V5.07 追加式账本复验器缺失时的显式硬阻断。

Changed:

- V4.30 估值市场状态按可得日形成，V4.84 结构特征按可得日向后关联，V4.72 当前候选仅接受有可用估值输入的官方历史或合格前向快照。
- 行业身份统计改为 166 个观察名称段、35 个名称或口径变化代码、2 个已确认语义复用代码；`801156` 普通陈旧与 7 个长尾缺口分栏。
- `beta_low_pb_score` 在按身份 episode 重算完成前退出三种宇宙只读稳健性指标；指标行由 20 调整为 15。
- V5.10、当前 runner、状态审计与 CURRENT_STATUS 不再接受调用方裸布尔值或摘要自报完整性解锁。

Result:

- 方法控制审计 13/13 通过；`promotion_gate_passed=false`、可晋级估值行 0、历史分类状态 `unavailable`。
- 冻结批次更新为 `ff_integrity_v7_20260718`，manifest `966e40a07d2248d8447692e85faf3d28d4ffaee51b2db8b0a787a861db0bf7e2`，二次复验 `changed_count=0`。
- V5.25—V5.35 全部重绑 v7；当前 active fund-flow 样本 0，V5.30 对空批次按设计保持失败关闭。
- 未修改因子、阈值、TopN、晋级标准或策略版本；当前结论仍为 `research_only / NO_ACTION`。

Final validation:

- Python 默认离线测试 215 passed、1 deselected；定向 PIT/消费端/状态反例 57/57。
- Dashboard 数据合同 6/6，TypeScript 与 Vite 生产构建 924 modules；`pip check` 与 `uv lock --check` 通过。
- 版本库存 65/65、研究治理覆盖 65/65、task brief 181 份 0 error/0 warning、状态一致性 34/34、方法控制 13/13。

## 2026-07-18 - PIT 终验反例加固

Added:

- 空、短码、非数字和非 `801xxx` 行业代码的失败关闭反例。
- 行业历史仅有自声明 `available_date`、分类史覆盖不全、原始证据哈希不匹配和交易日历未冻结的失败关闭反例。

Changed:

- 估值准备阶段不再用补零修补非法行业身份。
- 行业价格史与分类史晋级门改为验证完整 PIT 来源链、现场原始证据 SHA-256、冻结交易日历及受管代码全集精确覆盖。

Final validation:

- Python 默认离线测试 222 passed、1 deselected；定向 PIT 与行业历史合同 40/40。
- 当前主线维持 8 个硬阻断，动作仍为 `NO_ACTION`；未修改因子、阈值、TopN、晋级标准或策略版本。

## 2026-07-21 - 四条探索性资金流记录终局处置

Added:

- 2026-07-21 15:00 后的正式终局编排、独立 disposition 产物与 Git 跟踪终局说明。
- 结算专用行业行情缓存、主线缓存前后哈希证明、801156 固定隔离及现场字节哈希绑定。
- SWS 原始响应的严格日期/数值合同、默认 TLS 校验，以及刷新器、解析器、价格合同三份生成代码的现场哈希封存。

Changed:

- 四条 legacy 记录从结算前 pending 状态转为独立终局：settled 0、terminal blocked 4、pending 0、qualified settled 0。
- CURRENT_STATUS 增加 2026-06-23 / 2026-07-21 结算专用行情边界；主线决策仍为 2026-07-18，主线行业历史仍截止 2026-07-15。
- 主线硬门禁文档口径由 8 项修正为 10 项；V5.30 报告明确区分“失败关闭摘要门通过”与 `integrity=false`。

Result:

- 131 个结算文件中 130 个正常刷新、801156 隔离 1 个、失败 0；两日同一行业交集 123，四个目标 4/4。
- 四条均为 `blocked_terminal_late_freeze_excluded`，候选与基准冻结均保持 `late_backfill_excluded`，所有收益字段为空。
- 正式编排 14/14 步语义通过，证据清单 284/284（主线行情 131、结算专用行情 131）；active cohort 为 `ff_integrity_v8_20260721`。

Final validation:

- Python 全量回归 479 passed、1 deselected；资金流结算定向回归 193 passed。
- 版本库存 65/65、研究治理覆盖 65/65、task brief 181 份 0 error/0 warning；PIT 方法与 CURRENT_STATUS 自检通过。
- 当前动作仍为 `research_only / NO_ACTION`；未修改因子、阈值、TopN、晋级标准，也未生成投资建议。
