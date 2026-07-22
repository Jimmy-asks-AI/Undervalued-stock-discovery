# Research Log

## 2026-06-12 - Fundamental Value Research OS V0.1

- Completed reading and compressing the prior Codex thread into `notes/handoffs/FUNDAMENTAL_VALUE_RESEARCH_OS_HANDOFF.md`.
- Initialized the quant research workspace structure.
- Created the first multi-agent contract in `configs/fundamental_value_agents.json`.
- Created the first data contract in `data_catalog/fundamental_value_data_contract.md`.
- Created the first factor registry in `factor_library/fundamental_value_factor_registry.csv`.
- Created system blueprint in `notes/Fundamental_Value_Research_OS_Blueprint.md`.
- Added agent playbook in `agents/README.md`.
- Added runnable V0 orchestrator in `strategy_lab/fundamental_value_os/`.
- Added smoke test script `scripts/run_fundamental_value_smoke.py`.
- Added candidate report template in `portfolio_lab/candidate_report_template.md`.
- Key boundary: fundamental undervaluation scoring is separate from timing, ETF rotation, and T-trading overlays.

## 2026-06-12 - Fundamental Value Research OS V0.2

- Added `data_catalog/input_asset_panel_schema.csv` as the machine-readable asset-panel schema for latest-view and PIT validation modes.
- Added `data_catalog/fundamental_value_source_inventory.csv` to make the data acquisition queue explicit.
- Added `data_catalog/input_asset_panel_example.csv` as a schema-compliant example panel that can drive the V0 scorer.
- Added `scripts/validate_asset_panel_schema.py` to validate required fields, date ordering, enums, numeric fields, duplicate keys, and PIT-mode status.
- Validation run: `python .\scripts\validate_asset_panel_schema.py --input .\data_catalog\input_asset_panel_example.csv --mode latest` passed with 6 rows and 0 issues.
- Boundary: V0.2 still does not acquire real vendor data and does not validate alpha.

## 2026-06-12 - Fundamental Value Research OS V0.3

- Added `scripts/audit_factor_registry.py` to audit the factor registry against the input schema.
- The audit checks required registry columns, duplicate factor IDs, required field coverage, PIT rules, status ladder, failure modes, and label leakage.
- Validation run: `python .\scripts\audit_factor_registry.py` passed with 18 factors, 0 errors, and 0 warnings.
- Validation run: `python .\scripts\run_fundamental_value_smoke.py --input .\data_catalog\input_asset_panel_example.csv --output .\outputs\fundamental_value_smoke_v0_2_example` passed and preserved the V0.1 sample ranking behavior.
- Boundary: V0.3 audits factor definitions but does not run IC, RankIC, grouped returns, neutralization, OOS, costs, turnover, or capacity tests.

## 2026-06-12 - Fundamental Value Research OS V0.4

- Added `configs/fundamental_value_task_brief_schema.json` as the machine-readable task brief schema.
- Added task briefs under `strategy_lab/agents/task_briefs/` for the V0.1 smoke scorer, V0.2 asset-panel schema validation, and V0.3 factor registry audit.
- Added `scripts/audit_task_briefs.py` to check required task keys, owner agents, task status, allowed inputs, forbidden inputs, required outputs, and acceptance checks.
- Validation run: `python .\scripts\audit_task_briefs.py` passed with 3 task briefs, 0 errors, and 0 warnings.
- Boundary: V0.4 governs task packaging and does not execute multi-agent scheduling or promote any model.

## 2026-06-12 - Fundamental Value Research OS V0.5

- Added `scripts/run_current_a_share_value_snapshot.py` to run a current A-share `research_only` snapshot.
- Data sources: Eastmoney spot quote API direct request, AkShare `stock_yjbb_em` Eastmoney earnings report, and AkShare `stock_fhps_em` Eastmoney dividend/distribution data.
- Added `strategy_lab/agents/task_briefs/v0_5_current_a_share_value_snapshot.json`.
- Current run: 5,862 spot rows, 5,822 earnings rows, 3,858 dividend rows, 4,339 schema-valid research panel rows, and 30 current-snapshot obvious-value candidates.
- Validation run: current panel latest-mode schema check passed with 4,339 rows and 0 issues.
- Boundary: V0.5 uses current snapshot and proxy fields only; outputs remain `research_only`, not PIT validation, not alpha evidence, and not investment advice.

## 2026-06-12 - Fundamental Value Research OS V0.6

- Added `relative_cheapness_confirmation_auditor` to the core scorer after user review noted that industry-relative cheapness can indicate hidden company-level problems.
- The new auditor penalizes industry-relative cheapness when it is not confirmed by profitability quality, growth stability, cash-flow safety, shareholder-return support, or enough industry peers.
- The current A-share snapshot runner now blocks `relative_value_trap_flag=True` rows from the `obvious_value_flag` list.
- Current rerun: 4,339 schema-valid research panel rows, 226 industry-relative cheapness risk flags, and 30 obvious-value candidates with 0 such flags.
- Boundary: this is still a current-snapshot research guardrail, not historical proof that the filter improves returns.

## 2026-06-12 - Fundamental Value Research OS V0.7

- Added `scripts/run_quality_value_comparison.py` to filter current obvious-value candidates into stricter undervalued-and-quality candidates.
- Added `strategy_lab/agents/task_briefs/v0_7_quality_value_comparison.json`.
- Corrected V0.6 snapshot profile semantics: `obvious_value_candidates` now records the full obvious-value count, while `top_obvious_value_output_rows` records the report output limit.
- Quality gate requires composite score, profitability, growth, cash-flow safety, shareholder return, annualized ROE proxy, OCF/net income, and low penalty to pass.
- Current comparison: 53 previous obvious-value candidates, 30 previous Top output rows, 12 undervalued-and-quality candidates, 8 inside previous Top output, 4 outside previous Top output, 22 previous Top rows removed by quality gate.
- Split result: 11 financial-sector proxy-quality rows and 1 non-financial current-snapshot quality row.
- Boundary: financial-sector quality remains proxy-only until NPL, capital adequacy, provisioning, leverage, and sector-specific risk metrics are connected.

## 2026-06-12 - Fundamental Value Research OS V0.8

- Added a financial-sector value framework after the review that low valuation in banks, brokers, and insurers may be market risk pricing rather than undervaluation.
- Added `financial_sector_value_auditor` to the multi-agent configuration.
- Added optional bank, securities, and insurance fields to the input schema.
- Added financial-sector data-source requirements for bank asset quality and capital metrics, broker risk-control metrics, and insurer EV/solvency metrics.
- Added 7 financial-sector factors to the factor registry.
- Added `scripts/run_financial_value_iteration.py` to re-rank V0.7 financial candidates using common financial value factors plus bank and securities balance-sheet proxies.
- Current run: 11 V0.7 financial candidates, 11 proxy gate pass, 0 proxy gate fail, 0 confirmed financial undervaluation candidates.
- Top proxy ranks: 北京银行, 渝农商行, 华泰证券, 苏州银行, 南京银行.
- Boundary: all financial names remain `proxy_pass_regulatory_data_required`; no financial candidate can be called confirmed undervalued until sector-critical regulatory metrics are connected.

## 2026-06-12 - Industry ETF Value Research OS V0.9

- User redirected the system from specific stocks to concrete ETFs and industry-level research.
- Created `Industry ETF Value Research OS` as the new primary direction.
- Added multi-agent configuration for industry ETF research: orchestrator, data steward, industry valuation, oversold reversion, cycle quality, ETF mapping/liquidity, value-trap, validation, and report agents.
- Added industry ETF panel schema, source inventory, factor registry, keyword ETF mapping config, and system blueprint.
- Added `scripts/run_current_industry_etf_value_snapshot.py`.
- Current data sources: AkShare `sw_index_first_info`, `index_hist_sw`, and `fund_etf_spot_em`.
- Current run: 31 SW level-1 industry rows, 1,507 ETF spot rows, 3 industry value-oversold candidates, 20 top output rows.
- Current `industry_value_oversold_candidate` rows: 非银金融, 房地产, 石油石化.
- Current non-candidate examples: 银行 is `cheap_but_not_oversold`; 传媒 is watchlist because oversold is strong but valuation support is weaker; some industries are blocked by no tradeable ETF mapping.
- Corrected keyword mapping after first run removed obvious false matches: 香港证券 ETF from 非银金融, 航空航天 ETF from 交通运输, 消费电子 ETF from 食品饮料, and 工程机械 ETF from 建筑装饰.
- Boundary: output is industry-level research plus representative ETFs, not individual-stock selection and not investment advice.

## 2026-06-12 - Industry ETF Value Research OS V1.0

- Added SW second-industry research validation as the next-stage workflow.
- Added `scripts/run_industry_etf_research_validation.py` with local history caching under `data_catalog/cache/industry_etf/`.
- Added `scripts/audit_industry_etf_mapping.py` to check representative ETF mapping quality and excluded ETF types.
- Added V1 task brief with compact output, mapping audit, factor registry audit, and task brief audit acceptance checks.
- Added schema fields for `industry_level`, `parent_industry`, mapping confidence, 60D/120D/252D forward labels, volatility, and average amount.
- Added validation and mapping-confidence factors to the industry ETF factor registry.
- Current V1 run: 131 SW second-industry rows, 1,507 ETF spot rows, 17,026 historical feature rows, 19 current industry value-oversold candidates.
- Historical validation uses price-only PIT features. Current PE/PB/dividend yield is not backfilled into history.
- Price-only RankIC result is weak to negative: 60D mean 0.0022, 120D mean -0.0315, 252D mean -0.0589.
- ETF mapping confidence is now included in ETF implementation score and value-trap penalty.
- Mapping audit passed with 131 rows, 0 errors, 0 warnings.
- Boundary: V1 remains `research_only`; current valuation composite is not validated alpha until PIT valuation history is available.

## 2026-06-13 - Industry ETF Value Research OS V1.5

- Advanced the SW second-industry workflow to V1.5 using real AkShare public data only.
- Added valuation snapshot archiving under `data_catalog/cache/industry_etf/valuation_snapshots/second/`.
- Tightened ETF mapping: parent-industry keyword mappings are no longer treated as pass-quality mappings without review.
- Added `stabilized_oversold_signal` and retained `price_only_oversold_signal` for comparison.
- Added validation verdicts using RankIC, positive ratio, group spread, and Top-N cost-after-return checks.
- Added current portfolio-candidate simulation with ETF turnover and parent-industry constraints.
- Real-data test output: `outputs/test/industry_etf_research_validation_v1_5_real_data/`.
- Test result: 131 SW second-industry rows, 1,507 ETF spot rows, 17,026 historical feature rows, 3 V1.5 candidates, 6 RankIC rows, 3 current portfolio candidates.
- Real-data audit confirms `sample_or_mock_data_used=false`; one AkShare history gap remains for `801156`.
- Validation conclusion remains weak, not promoted: `偏弱：存在方向性证据但未达到可推广门槛`.
- Default output overwrite was blocked because `outputs/industry_etf_research_validation/top_candidates.csv` was open in Excel.

## 2026-06-13 - Industry ETF Value Research OS V1.5 Default Output Completion

- Excel file lock was released and the default output directory was refreshed with V1.5 real-data results.
- Main output: `outputs/industry_etf_research_validation/`.
- Real-data run command used `--refresh-history`; AkShare public data was fetched again and local history cache was refreshed.
- Current result: 131 SW second-industry rows, 1,507 ETF spot rows, 17,026 historical feature rows, 3 V1.5 candidates, 6 RankIC rows, and 3 current portfolio candidates.
- Main output is compact: `report.md`, `top_candidates.csv`, `run_summary.json`, and `debug/`.
- `debug/real_data_audit.json` confirms `real_data_only=true`, `sample_or_mock_data_used=false`, and `refresh_history=true`.
- Governance checks passed: compact output layout, task briefs, factor registry, and ETF mapping audit.
- V1.5 conclusion remains research-only and weak: `偏弱：存在方向性证据但未达到可推广门槛`.

## 2026-06-13 - Industry ETF Value Research OS V1.6

- Advanced the SW second-industry workflow to V1.6 focused on ETF mapping governance.
- Added manual ETF whitelists and code/name blacklists in `configs/industry_etf_mapping_keywords.json`.
- Added mapping evidence levels, review-required flags, review reasons, whitelist code traces, and blacklist-hit traces.
- Added compact debug outputs: `debug/etf_mapping_review_queue.csv` and `debug/etf_mapping_coverage_summary.csv`.
- Real-data run used `--refresh-history`; AkShare public data was fetched again and local history cache was refreshed.
- Current result: 131 SW second-industry rows, 1,507 ETF spot rows, 17,026 historical feature rows, 5 V1.6 candidates, 6 RankIC rows, and 5 current portfolio candidates.
- Current candidates: 保险Ⅱ, 房地产开发, 游戏Ⅱ, 证券Ⅱ, 白酒Ⅱ.
- ETF mapping audit status: 9 pass, 107 requires_review, 15 no_tradeable_etf, 0 fail.
- Manual whitelist mappings: 19 rows. Review queue: 122 rows.
- `debug/real_data_audit.json` confirms `real_data_only=true`, `sample_or_mock_data_used=false`, and `refresh_history=true`.
- Governance checks passed: compact output layout, ETF mapping audit, factor registry audit, and task brief audit.
- V1.6 conclusion remains research-only and weak: `偏弱：存在方向性证据但未达到可推广门槛`.

## 2026-06-13 - Industry ETF Value Research OS V1.6.1 Debug Processing

- Processed the first high-priority ETF mapping debug queue batch from `debug/etf_mapping_review_queue.csv`.
- Added explicit `manual_no_tradeable` handling for industries where the current public ETF universe has no suitable second-industry carrier: 医药商业, 燃气Ⅱ, 多元金融, 饲料, 房地产服务, 农化制品.
- Added or tightened direct/close manual mappings for 养殖业, 中药Ⅱ, and 基础建设.
- Cleaned blacklist-hit audit logging so global blacklisted ETFs are recorded only when they would otherwise match the industry.
- Current result: 131 SW second-industry rows, 1,507 ETF spot rows, 17,026 historical feature rows, 6 V1.6.1 candidates, 6 RankIC rows, and 6 current portfolio candidates.
- Current candidates: 保险Ⅱ, 房地产开发, 游戏Ⅱ, 养殖业, 证券Ⅱ, 白酒Ⅱ.
- ETF mapping audit status: 12 pass, 98 requires_review, 21 no_tradeable_etf, 0 fail.
- Mapping audit warnings fell from 97 to 88; review queue rows fell from 122 to 119.
- `debug/etf_mapping_review_queue.csv` no longer contains the unrelated `513310` global-blacklist noise.
- V1.6.1 remains research-only and weak: `偏弱：存在方向性证据但未达到可推广门槛`.

## 2026-06-13 - Industry Index Research OS V1.7

- Switched the current mainline to pure SW industry and industry-index research.
- Added `configs/industry_index_value_agents.json`, `data_catalog/industry_index_panel_schema.csv`, `data_catalog/industry_index_source_inventory.csv`, `factor_library/industry_index_factor_registry.csv`, and `strategy_lab/industry_index_research_os/`.
- Added `scripts/run_industry_index_research_validation.py`.
- Removed old implementation-carrier scripts, config, schema, registry, active outputs, and old cache from the current workspace mainline.
- Current output: `outputs/industry_index_research_validation/`.
- Test output: `outputs/test/industry_index_research_validation_v1_7/`.
- Current result: 131 SW second-industry rows, 17,026 historical feature rows, 28 current industry candidates, 6 RankIC rows, and 8 current research-basket rows.
- Current research basket: 厨卫电器, 水泥, 特钢Ⅱ, 商用车, 医药商业, 基础建设, 房屋建设Ⅱ, 保险Ⅱ.
- Real-data audit: 1 empty history (`801156`) and 7 short histories remain.
- Validation conclusion remains research-only and weak: `偏弱：存在方向性证据但未达到可推广门槛`.
- Governance checks passed: compact output layout, industry-index factor registry audit, and task brief audit.

## 2026-06-13 - Industry Index Research OS V2.3

- Advanced the pure industry-index workflow to V2.3 focused on pressure-regime price-quality filtering.
- Added `scripts/run_industry_pressure_quality_v2_3.py`.
- V2.3 quality proxy is price-based only: short-term recovery, liquidity, low volatility, relative trend resilience, and drawdown quality.
- Current output: `outputs/industry_pressure_quality_v2_3/`.
- Current result: 17,026 feature rows, 8,955 event rows, 1,752 non-overlapping rows, 6,679 daily NAV rows, 128 pressure episodes.
- Signal counts: 0 candidate signals, 39 conditional observations, 24 rejected standalone signals.
- Strongest V2.3 observation: `V2.3：压力质量流动性` Top5/252, with full-sample relative return 1.66%, OOS relative return 6.02%, non-overlap relative return 4.23%, but bootstrap 5% lower bound -0.04%, relative NAV 0.986, and only 29 samples.
- Research conclusion: quality filtering helps some event-return statistics, but not enough to pass sample-size, non-overlap, OOS, bootstrap, and daily NAV gates.
- Boundary: no alpha promotion; V2.3 remains research-only.

## 2026-06-13 - Industry Index Research OS V2.4

- Advanced the pure industry-index workflow to V2.4 focused on current valuation plus pressure candidate explanation.
- Added `scripts/run_industry_fundamental_pressure_v2_4.py`.
- Current output: `outputs/industry_fundamental_pressure_v2_4/`.
- V2.4 joins the latest industry-index current ranking with the latest archived valuation snapshot under `data_catalog/cache/industry_index/valuation_snapshots/second/`.
- Current valuation snapshot date: 2026-06-12.
- Current valuation coverage: 131 of 131 current second-industry rows.
- PIT valuation snapshot count: 1, below the 60-snapshot readiness threshold.
- Current market pressure state: `普通状态`, pressure score 0.546.
- Current result: 11 current snapshot candidates, 21 valuation watchlist rows, 39 oversold-without-valuation-support rows, and 60 research watchlist rows.
- `debug/pit_readiness_audit.csv` explicitly blocks historical valuation-factor validation because only one current valuation snapshot exists.
- Governance checks passed: compact output layout, industry-index factor registry audit, task brief audit, and V2.4 script compile.
- Boundary: V2.4 candidates are current observations only; current PE, PB, and dividend yield are not backfilled into history and no alpha is promoted.

## 2026-06-13 - Industry Index Research OS V2.4.iterative.3

- Standardized the strategy iteration workflow as three rounds of backtest, report, review, and next-iteration adjustment.
- Added `scripts/run_industry_fundamental_pressure_iterative_v2_4.py`.
- Current output: `outputs/industry_fundamental_pressure_iterative_v2_4/`.
- The runner refreshes the V2.4 current snapshot first, then runs historical tests using only PIT-available industry-index price, pressure, turnover, and price-quality proxy fields.
- Current V2.4 valuation context remains blocked for PIT valuation validation: valuation snapshot count 1, required minimum 60.
- Iteration 1 tested the V2.4 pressure-quality baseline; best Top5/252 had full-sample relative return +2.33%, OOS +1.03%, non-overlap +1.44%, bootstrap lower bound +0.28%, but relative NAV 0.989 and sample strength 10.00%.
- Iteration 2 widened the pressure gate and added liquidity confirmation; best Top10/252 had relative NAV 1.019, but only one effective sample and no valid bootstrap confidence.
- Iteration 3 added defensive quality confirmation while keeping enough events; best Top5/120 had full-sample relative return +4.92%, OOS +3.28%, non-overlap +2.88%, bootstrap lower bound +1.79%, but relative NAV 0.985 and sample strength 5.16%.
- Total result: 192 event rows, 0 candidate signals, 10 conditional observations, 2 rejected parameter combinations.
- Research conclusion: do not promote alpha and do not continue parameter-mining; next step is PIT valuation snapshot accumulation or external historical industry valuation data.
- Boundary: the three-round workflow validates only price/pressure/quality proxies. It does not validate current PE, PB, or dividend yield as historical factors.

## 2026-06-14 - Industry Index Research OS V2.5

- Advanced the pure industry-index workflow to V2.5 focused on historical industry valuation data collection and an industry quality proxy.
- Added `scripts/run_industry_quality_proxy_v2_5.py`.
- Current output: `outputs/industry_quality_proxy_v2_5/`.
- Public SWS Research index analysis daily route succeeded and collected 250,306 second-industry valuation rows from 2015-01-05 to 2026-06-12.
- Current public valuation coverage: 131 second-level industries.
- Licensed vendor route was attempted but remains blocked by missing credentials or missing terminal SDK: Tushare Pro and JQData need credentials; Wind/Choice/iFinD terminal SDKs are not present.
- Stock-level reconstruction route was attempted but remains blocked because current public components are not enough for PIT historical industry membership plus stock-level fundamentals.
- V2.5 quality proxy combines PE/PB sanity, dividend continuity, valuation stability, market depth, V2.4 price-quality proxy, and valuation history coverage.
- Current result: 131 quality rows, 104 generic quality-proxy passes, 12 sector-data-required flags, and 11 V2.5 current observations.
- Boundary: V2.5 remains `research_only`; public valuation history can support the next PIT valuation validation step only after source-mouth, release-lag, and cross-source audits.

## 2026-06-14 - Industry Index Research OS V2.6

- Advanced the pure industry-index workflow to V2.6 focused on PIT candidate validation of historical industry valuation factors.
- Added `scripts/run_industry_valuation_pit_validation_v2_6.py`.
- Current output: `outputs/industry_valuation_pit_validation_v2_6/`.
- V2.6 uses the V2.5 SWS historical valuation cache and applies a conservative `valuation_available_date = valuation_trade_date + 1 calendar day` rule.
- Added minimum cross-section filtering: only feature dates with at least 20 matched industries enter validation.
- Current result: 9,007 signal rows, 2,214 event-backtest rows, 440 non-overlapping event rows, and 7,130 daily NAV rows.
- Signal verdict: 0 candidate signals, 15 conditional observations, and 18 rejected signals.
- Best observation: pure valuation PIT Top20/60 has full-sample relative +1.29%, OOS +1.17%, non-overlap +1.07%, and relative NAV 1.091, but bootstrap lower bound is -0.15% and non-overlap sample count is only 10.
- RankIC evidence improved versus price-only work: valuation quality proxy on 252d horizon has mean RankIC +5.31%, t-stat 4.46, and positive ratio 62.72%.
- Blocking issues: bootstrap lower bound remains negative, sample strength is insufficient, industry universe changes materially after 2021, and public SWS source mouth/release-lag still require audit.
- Boundary: V2.6 remains `research_only`; no valuation factor is promoted to alpha.

## 2026-06-14 - Industry Index Research OS V2.7

- Advanced the pure industry-index workflow to V2.7 focused on valuation-quality validation instead of low-valuation bottom-fishing.
- Added `scripts/run_industry_valuation_quality_v2_7.py`.
- Current output: `outputs/industry_valuation_quality_v2_7/`.
- V2.7 reuses the V2.5 SWS historical valuation cache and V2.6 PIT available-date handling.
- Default portfolio sizes are Top20 and Top30; smaller Top5/Top10 tail baskets are intentionally removed from the default run.
- Added quality-core, quality-value, defensive-quality, post-2022 quality, quality-value-no-trap, and broad-control variants.
- Current result: 9,007 signal rows, 657 event-backtest rows, 129 non-overlapping event rows, and 2,287 daily NAV rows.
- Signal verdict: 0 candidate signals, 10 conditional observations, and 2 rejected signals.
- Best event observation: `V2.7：质量宽口径对照` Top30/60 has full-sample relative +0.48%, non-overlap relative +0.45%, and relative NAV 1.023, but OOS relative -0.03% and bootstrap lower bound -1.09%.
- Strongest RankIC observation: `quality_value_no_trap_score` on 252d horizon has mean RankIC +6.46%, t-stat 4.55, and positive ratio 61.65%.
- Research conclusion: valuation quality contains long-horizon ranking information, but portfolio-level evidence is not strong enough for alpha promotion.
- Boundary: V2.7 remains `research_only`; no trading signal is generated.

## 2026-06-14 - Industry Index Research OS V2.8

- Advanced the pure industry-index workflow to V2.8 focused on RankIC-to-portfolio bridge validation.
- Added `scripts/run_industry_rankic_portfolio_bridge_v2_8.py`.
- Current output: `outputs/industry_rankic_portfolio_bridge_v2_8/`.
- V2.8 reuses V2.7 factors and does not add new ranking formulas.
- Tested Top20/Top30, 252-day horizon, feature-monthly/quarterly/semiannual rebalance, broad universe, sector-excluded universe, post-2022 sector-excluded universe, Top-Bottom return, long-relative return, bootstrap, daily Top-Bottom NAV, quantile monotonicity, sector attribution, and same-date capacity.
- Current result: 9,007 signal rows, 1,314 bridge-event rows, 54 portfolio combinations, 0 candidate signals, 32 conditional observations, and 22 rejected signals.
- Best observation: `V2.6纯估值对照` broad Top20/252 has full-sample Top-Bottom +2.28% and daily Top-Bottom NAV 1.119, but OOS Top-Bottom -7.71%, bootstrap lower bound -5.98%, and quantile Top-Bottom -4.02%.
- Strongest RankIC evidence remains `质量价值非陷阱` sector-excluded 252d RankIC: +8.14%, t-stat 5.56, positive ratio 68.24%.
- Same-date capacity audit explains why old-universe bridge events disappear: 2015-2021 broad sample average same-date eligible industries 29.50 and max 39, below the 40 industries needed for Top20-Bottom20.
- Research conclusion: RankIC information exists, but it currently fails to translate into robust, sample-out, tail-portfolio alpha.
- Boundary: V2.8 remains `research_only`; no trading signal is generated.

## 2026-06-14 - Latest Framework Undervalued-Oversold Bottom Test

- Ran a dedicated bottom-fishing backtest using the latest V2.8/V2.7 signal panel and V2.6 PIT valuation source.
- Added `scripts/run_latest_undervalued_oversold_bottom_backtest.py`.
- Current output: `outputs/industry_latest_undervalued_oversold_bottom_backtest/`.
- State definitions use PIT valuation cheapness, own-history valuation cheapness, stabilized/price-only oversold signals, drawdown, V2.7 quality-value-no-trap score, and momentum-trap filters. Future returns are labels only.
- Current result: 9,007 signal rows, 2,565 event rows, 24 parameter combinations, 0 candidate signals, 7 conditional observations, and 17 rejected signals.
- Best observation: `低估超跌非陷阱` Top10/60 has absolute net return +4.21%, benchmark return +4.66%, relative return -0.45%, OOS relative +1.44%, non-overlap relative +1.75%, and relative NAV 0.991.
- Higher-horizon observation: `低估超跌非陷阱` Top10/252 has absolute net return +12.06%, benchmark return +12.72%, relative return -0.66%, and relative NAV 0.991.
- Research conclusion: latest undervalued-oversold bottom-fishing captures absolute rebound beta but still does not produce stable industry-selection alpha versus equal-weight industry benchmark.
- Boundary: dedicated test remains `research_only`; no trading signal is generated.

## 2026-07-18 - 资金流前推证据链 P0 整改

- 目标：修复 V5.25-V5.35 的前推观察、入场冻结、全行业基准、退出结算、晋级和完整性审计链，不改变既有晋级阈值。
- 新建不可变 cohort `ff_integrity_v2_20260718`；18 项方法与静态证据全部存在，manifest 为 `08c54873a134d999555c07abab0a1dc14f4e52c5b7c338dc9bee2d342275a2a6`，创建后经独立第二次运行核验通过。
- cohort 创建时点只认追加式 history 的末条记录；篡改 `active.json` 的 `created_at_utc` 会失败关闭，V5.25 和 V5.30 均禁止新 cohort 追认旧信号。
- 权威状态改为追加式 JSONL 哈希链加独立 head checkpoint；兼容 CSV 由事件链原子重建。观察、候选冻结、基准冻结、cohort history 四条链均通过核验。
- 时间状态分成 `early_pending`、窗口内和 `late_backfill_excluded`；盘前运行不写不可逆迟到标记，退出日北京时间 15:00 前不读取或写入收益。
- 观察源冻结候选表与 signal-date 摘要 bundle；候选价、全行业基准和未来结算使用内容寻址源快照，由 V5.30 独立复算。
- 并发、首次写入优先、事件已追加但 CSV 未写完的崩溃恢复、重复结算、合法前缀回滚和源快照篡改均有反例测试。
- 旧 CSV 迁移为 4 条 legacy observation 事件，仍是探索样本；4 条候选冻结和 4 条基准冻结保留为历史 late failure marker，没有补造价格或 131 行面板。
- 当前 active observation=0，qualified settled=0；global observation=4，settlement event=0。V5.30 因当前批次没有可审计观察而按设计失败关闭，违规行=0。
- 验收：资金流测试 74/74、链路 self-check 12/12、ETF 回归 12/12、dashboard 生产构建 923 modules、紧凑输出审计 errors=0。
- 交付：`outputs/audit/fund_flow_forward_chain_remediation/`，其中 `report.md` 为主报告，证据文件统一放在 `debug/`。
- 边界：未运行实际 V5.27，未查看或结算 2026-07-21 退出收益；当前仍是 `research_only / NO_ACTION`。本地哈希链与 checkpoint 能发现状态漂移，但没有外部签名、可信时间戳或 WORM 锚点。

## 2026-07-18 - 当前状态与研究治理追溯盘点

- `record_type=retrospective_inventory`
- `recorded_at=2026-07-18`
- `historical_timestamp_claimed=false`
- `post_hoc=true`：本条是 2026-07-18 对既有研究版本所作的追溯登记，不倒填历史记录时间，也不把追溯盘点表述成事前注册。
- 版本库存共 65 条：V4.72-V5.35 共 64 个历史版本，另加 1 条 `CURRENT_MAINLINE`。当前库存中 65/65 个 producer 可定位，65/65 个输出目录具备 `report.md`、`run_summary.json`、`top_candidates.csv` 与 `debug/`；治理是否通过仍由独立覆盖审计判定，日志文字不能替代 task brief 或把缺项自动判为通过。
- registration 边界按机器可判别口径固定：V5.04 是唯一 `preregistered_forward_only` 的精确事前注册；V5.05-V5.10 为 `inherits_registered_rule`；其余历史版本，即 V4.72-V5.03 与 V5.11-V5.35，均为 `explicit_post_hoc`；`CURRENT_MAINLINE` 为 `preregistered_forward_only_inherited`。这一区分只说明登记性质，不改变任何历史研究结论。
- 当前活跃证据批次为 `ff_integrity_v5_20260718`，manifest 为 `531cf927cd18cc3c774777098ba20794b7f78c1f8dbfe67a49397d8a6f17954c`；第二次独立核验已通过，active pointer 不含失效字段。此前 v2、v3、v4 批次及其摘要继续保留为不可改写的历史快照，不再代表 current。
- 当前主线 `as_of_date=2026-07-18`，`policy_status=research_only`，`action=NO_ACTION`。7 个阻断门分别为 `timing_robustness`、`industry_selection`、`account_state`、`portfolio_risk`、`goal_evidence`、`agent_veto_chain`、`projected_portfolio_risk`。
- 证据边界不变：强行业 Alpha 尚未验证，`manual_decision_support_ready=false`，人工辅助交易未就绪；`production_ready=false`，`auto_execution_allowed=false`，自动交易禁止。
- 强行业主线已结算 forward 样本为 0；当前 active fund-flow observation 为 0，qualified settled 为 0。全局历史保留 4 条 `exploratory_fund_flow_only` legacy observation，均 `qualified_for_goal=false`、`integrity_eligible=false`、`promotion_eligible=false`，且尚未结算；其计划退出日为 2026-07-21，在退出日门禁前不得读取或写入未来收益，日后结算也不会自动把探索样本改成合格样本。
- 最新本地状态一致性审计已扩展到 V5.25—V5.35 全部 11 个 cohort-aware 摘要，30/30 项通过。完成度审计记录工程实现检查 28/28、决策就绪检查 6/12、behavior tests 23/23、脚本 self-check 12/12；四类数字属于不同验证层，不相加、不互相替代。先前 P0 条目中的 74/74 只对应当时资金流专项测试快照，不作为本次全项目测试结论。
- 对外统一使用“六角色确定性否决链”。策略版本、审计版本、数据治理版本与 forward cohort 分栏记录，文档版本不再冒充策略版本。

## 2026-07-18 - PIT 估值与行业历史方法审计

- 估值原表 236,682 行，其中直接 SWS 来源 236,551 行，真实截止 2025-12-31；2026-06-12 的 131 行回收快照已隔离。
- 六字段合同只有 `trade_date` 具备数据，`published_at/available_date/fetched_at/source_version/revision_status` 均缺失；可晋级估值行 0。
- 冻结交易日历哈希绑定，盘后、周末和节假日只能落到下一有效交易日；自然日 lag 不再接受。
- 观察宇宙由 2015 年单日中位 68，经过 2021-12-13 的 66→124 扩容，转为 2022 年中位 123，并在 2023-10-09 稳定为 131。
- 131 个行业历史文件完整；123 个满足当前新鲜度，超过 120 门槛。7 个长尾文件止于 2024-06-17，只计档案覆盖，不计新鲜度。
- 三种宇宙视图共 20 行指标全部标记为描述性历史审查；分段结果差异明显，不支持稳定 Alpha 结论。
- V2.6、V5.11、V5.12 实测均为 `blocked_non_pit_valuation_history`，passing=0；V4.84 的未验证估值特征已屏蔽，passing=0。
- 当前主线新增 `pit_universe_methodology` 硬门，动作保持 `NO_ACTION`。恢复历史路线需要真实发布链和带生效区间的官方行业分类成员表。

## 2026-07-18 - PIT 与行业历史整改最终验收

- 活跃证据批次更新为 `ff_integrity_v6_20260718`，manifest `4d785b55ecb2ed56ea2e5e9a15aa0e1ba1c080e97b27b7d4a455d353c507d777`；冻结复验没有发现依赖漂移。
- 当前状态一致性 34/34，工程合同 29/29，建议就绪 6/13；主线新增方法阻断后共有 8 个硬阻断，动作仍为 `NO_ACTION`。
- 版本库存、task brief 与治理覆盖按“输出最终化 → 库存 → brief → 再建库存 → 审计”的顺序重建，65/65 通过。
- 全量离线测试 196 passed、1 deselected；行为测试 23/23；脚本 self-check 12/12；锁文件检查通过。
- 验收通过只证明合同、审计和失败关闭机制可执行；历史估值与分类的外部证据缺口仍在，不能据此宣称研究或交易就绪。

## 2026-07-18 - PIT 终态门禁复核与 v7 基线

- 估值合同补充不可变原始文件路径、现场 SHA-256 复算、修订链末态和默认交易日历固定哈希；任意 64 位字符串、非交易日或晚到 superseded 版本均不能取得 PIT 资格。
- V4.30 改为按 `available_date` 形成市场估值状态；V4.84 只按 `feature_available_date` 向后关联；V4.72 隔离 131 条恢复型快照，并禁止核心估值全空的中性分进入当前候选。三条反例均复现旧风险并验证新路径失败关闭。
- 行业身份口径改为：166 个观察名称段、35 个名称或口径变化代码、2 个已确认语义复用代码。7 个长尾缺口和 `801156` 普通陈旧分栏；历史 beta 未完成身份安全重算，`beta_low_pb_score` 已从只读稳健性视图排除。
- 方法审计 13/13 通过，三种宇宙共 15 行描述性指标；历史晋级门仍为 false，可晋级估值行 0，分类历史状态仍为 unavailable。
- V5.07 没有可重放的追加式证据账本，历史方法摘要也没有独立晋级凭证。消费端已禁止摘要自报或裸布尔值解锁，两条路线保持硬阻断。
- 活跃冻结批次切换到 `ff_integrity_v7_20260718`，manifest `966e40a07d2248d8447692e85faf3d28d4ffaee51b2db8b0a787a861db0bf7e2`，复验 `changed_count=0`。V5.25—V5.35 已全部重绑；active 样本仍为 0，V5.30 因空批次按设计失败关闭且违规数为 0。
- 当前主线仍有 8 个硬阻断，动作 `NO_ACTION`；本轮没有修改因子、阈值、TopN、晋级门槛或策略版本。
- 最终现场回归：Python 215 passed、1 deselected；Dashboard 合同 6/6、构建 924 modules；库存 65/65、治理覆盖 65/65、brief 181 份 0 error/0 warning、状态一致性 34/34。

## 2026-07-18 - PIT 终验反例加固

- 独立暂存区复核发现两条理论误放行路径：空行业代码可被补零后接受，以及行业历史仅凭自声明 `available_date` 和不完整分类表可能打开局部门禁。
- 估值合同现严格要求 `industry_code` 匹配 `801xxx`；行业价格史与分类史现同时校验冻结日历、发布时间、抓取时间、来源版本、修订状态、不可变原始证据现场哈希和受管代码全集覆盖。
- 四类反例——空/非法代码、自声明可得日、部分分类覆盖、伪哈希或非冻结日历——均失败关闭。完整离线回归更新为 222 passed、1 deselected；当前结论仍为 `research_only / NO_ACTION`。

## 2026-07-21 - 四条探索性资金流记录正式终局

- 运行门禁于北京时间 2026-07-21 15:00 后通过；计划入场日为 2026-06-23，计划退出日为 2026-07-21。
- 801194 保险Ⅱ、801125 白酒Ⅱ、801764 游戏Ⅱ、801203 一般零售全部形成 `blocked_terminal_late_freeze_excluded` 独立处置。
- 终局计数为 settled 0、terminal blocked 4、pending 0、qualified settled 0；原账本 `settlement_status=not_due` 未被反写，所有收益字段为空。
- 结算专用缓存 131 个文件：正常刷新 130、固定隔离 801156 共 1、失败 0；两日精确行情交集 123，四个目标 4/4。
- 行情交集只证明事后日期可用性。入场时 `benchmark_universe_count=0` 和两类 `late_backfill_excluded` 冻结不能由 2026-07-21 行情补齐。
- 结算专用缓存聚合 SHA-256 为 `bedff91421395fcaa05185082dac3edc245a75869b745b51e2f6cc1845151a46`；801156 文件为 `f84fea1c417b3487fe7b5c7bf1c8e90fd8c6257733f1b20ce8575a5fb7a3f23d`。
- 早期共享缓存写入已纠正到 2026-07-15 语义边界。恢复发生 CSV 重序列化，主线聚合从原 `9ebc…` 变为 `ae35…`；正式刷新期间主线前后哈希一致。
- 正式编排 14/14 步通过，清单 284/284（主线行情 131、结算专用行情 131），Python 全量回归 479 passed、1 deselected。结论保持 `research_only / NO_ACTION`，不构成投资建议。
