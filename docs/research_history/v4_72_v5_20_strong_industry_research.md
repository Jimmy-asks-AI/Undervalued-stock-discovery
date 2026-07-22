<!--
archive_record_type: read_only_migration
source_path: README.md
source_commit: 36cc42926a72d488116417e48e6107b544754d93
source_lines: 86-188
original_text_sha256: 1cb429e528fbe46fbc444f9c519e034783c186d2a794152b553413078fd6ca79
hash_basis: UTF-8, LF line endings, terminal LF included
ignored_output_link_normalizations: 49
-->

# V4.72—V5.20 强行业研究归档

> 本页为 README 历史正文的只读迁移件。正文事实、版本和数值按原文保留；仅将被忽略的 `outputs/` Markdown 链接改成行内路径，防止历史文档产生失效本地链接。迁移记录见 `docs/research_history_migration_manifest.json`。

<!-- BEGIN MIGRATED README LINES 86-188 -->

V4.72 新增“反弹窗口内强行业选择评价”，目标不是再判断有没有反弹窗口，而是在 V4.70 已触发窗口内评估能否选出比全行业等权反弹更强的行业。当前结论仍是 `research_only_not_validated`：最好的 `oversold_liquidity Top10` 平均超额约 1.23%，胜率约 68.42%，平均 RankIC 约 0.158，样本外平均超额约 1.77%，但未来收益前 20% 行业命中率只有 28.07%，年度正收益比例只有 44.44%，未达到“稳定强反弹行业选择”门槛。输出目录：`outputs/industry_rebound_leader_selection_v4_72/report.md`。

V4.73 新增“状态门控强反弹行业选择复检”，把 V4.72 暴露的状态失败桶转为事前可见的状态过滤，只在深负广度、中高压力或高波动保护区等状态下复检强行业排序。当前最优组合为 `mid_high_stress_only + oversold_liquidity Top5`：平均超额约 4.33%，胜率约 81.25%，Top20% 命中率约 40.00%，但事件数只有 16，年度正收益比例只有 50.00%，仍未通过原强行业评价体系，因此结论继续保持 `research_only_not_validated`。当前 2026-06-18 最新状态未落入任何通过状态桶，候选全部降级为状态门控阻断。输出目录：`outputs/industry_rebound_leader_state_gated_v4_73/report.md`。

V4.74 新增“样本外因子选择强反弹行业审计”，把训练期固定为 2021 年及以前，只允许先用训练期选择状态桶、因子和 TopN，再看 2022 年以后的样本外表现。严格修正后，训练期选出的 `mid_high_stress_only + turn_score Top5` 在样本外失败：样本外平均超额约 -2.25%，样本外胜率约 30.77%，样本外 Top20% 命中率约 12.31%，因此继续保持 `research_only_not_validated`。这个结果说明：V4.73 的好结果不能直接当作可前推因子证据；强行业选择仍未找到。输出目录：`outputs/industry_rebound_leader_oos_factor_v4_74/report.md`。

V4.75 新增“逐年前推强反弹行业选择审计”，每个测试年只允许使用之前年份已经完成的样本训练规则；如果历史样本训练不出通过初筛的规则，当年直接跳过，不用当年未来收益反选。当前结果继续失败：5 个可测试年份中只有 1 年能执行规则，执行事件 17 个，平均超额约 -2.35%，胜率约 29.41%，Top20% 命中率约 15.29%，年度正收益比例 0.00%。这说明目前的行业选择规则无法前推，不能证明能找到反弹更猛的行业。输出目录：`outputs/industry_rebound_leader_walk_forward_v4_75/report.md`。

V4.76 新增“行业资金流强反弹验证资格审计”。资金流是下一步更合理的新信息源，但当前只能作为前推观察：PIT 缓存日期只有 1 天，低于研究门槛 60 天和 alpha 门槛 252 天；同花顺到申万二级精确映射覆盖约 64.44%，低于 80% 门槛；当前 10 个候选中 8 个有精确当前资金流、2 个仍是代理观察；今日净流入为正的候选只有 1 个，今日和 5 日同时净流入为正的候选为 0。因此资金流不能接入强反弹行业评价，只能从现在开始累计前推样本。输出目录：`outputs/industry_rebound_leader_fund_flow_readiness_v4_76/report.md`。

V4.77 新增“强反弹行业特征分离度审计”，直接检查现有特征能否把未来收益前 20% 的行业和其他行业分开。`any_passed_state_bucket + oversold_liquidity_score` 通过分离度门槛：事件数 35，平均 RankIC 约 0.207，正 RankIC 比例约 77.14%，标准化分离差约 0.260，样本外平均 RankIC 约 0.259。这说明现有特征不是完全无效，确实有强反弹行业排序信息。输出目录：`outputs/industry_rebound_leader_feature_separability_v4_77/report.md`。

V4.78 新增“分离度通过特征组合验证”，只使用 V4.77 通过的 `any_passed_state_bucket + oversold_liquidity_score`，再测试 Top5/10/20 是否真正跑赢全行业等权。当前最优 `Top5` 平均超额约 1.75%，中位数超额约 1.15%，胜率 60.00%，Top20% 命中率约 30.29%，样本外平均超额约 2.99%，但年度正收益比例只有 40.00%，低于 60.00% 门槛，因此仍是 `research_only_not_validated`。这是一条接近可用但年度稳定性不足的候选规则。输出目录：`outputs/industry_rebound_leader_separable_portfolio_v4_78/report.md`。

V4.79 新增“压力状态强反弹行业选择验证”，把 V4.78 的普通状态门控收窄为事前可见的 `deep_or_high_vol`：`negative_breadth_60d >= 0.75` 或 `market_volatility_20d_vs_60d >= 1.30`。当前最优规则为 `deep_or_high_vol + oversold_liquidity_score Top10`：事件数 32，覆盖 5 个年份，平均超额约 1.93%，中位数超额约 1.22%，胜率 75.00%，Top20% 强行业命中率约 30.94%，年度正收益比例 60.00%，样本外平均超额约 2.53%，样本外胜率约 81.48%，点估计通过强反弹行业研究门槛。但 bootstrap 稳健性未完全确认：Top20% 命中率 5% 下界约 26.56%，年度正收益比例 5% 下界约 50.00%，低于硬门槛。因此状态为 `research_only_point_gate_passed_robustness_not_confirmed`，还不能称为真正找到稳定强反弹行业。当前最新状态门控未触发，因此最新候选仍自动降级为观察，不是入场信号；该版本仍保持 `production_ready=false`、`auto_execution_allowed=false`。输出目录：`outputs/industry_rebound_leader_pressure_state_v4_79/report.md`。

V4.80 新增“稳健强反弹行业规则网格审计”，把评价体系升级为三层：点估计门槛、bootstrap 5% 下界、留一年验证。该版本只测试少量事前可解释的状态门控、现有价格/估值/企稳/流动性特征和 TopN，不引入 ETF、个股或未来收益反选。当前共测试 320 条规则，其中 3 条通过点估计门槛，但 0 条通过完整稳健门槛；最接近的仍是 `deep_or_high_vol + oversold_liquidity_score Top10`，平均超额约 1.93%、Top20% 命中率约 30.94%，但 bootstrap Top20% 命中率 5% 下界约 26.56%，bootstrap 年度正收益比例 5% 下界约 50.00%，留一年最小命中率约 23.33%。因此当前严格结论是 `research_only_no_robust_stronger_industry_rule`：还没有真正找到稳定的强反弹行业选择规则。输出目录：`outputs/industry_rebound_leader_robust_grid_v4_80/report.md`。

V4.81 新增“市场状态扩展强行业审计”，继续测试 V4.70 信号日已可见的窗口质量条件：流动性修复、广度修复、恐慌衰竭、成交额扩张、下跌集中度下降等。该版本共测试 192 条状态过滤规则，其中 7 条通过点估计门槛，但 0 条通过完整稳健门槛；最接近的仍是 `deep_highvol_liq_repair + oversold_liquidity_score Top10`，平均超额约 1.93%、Top20% 命中率约 30.94%，但 bootstrap Top20% 命中率 5% 下界约 26.56%，bootstrap 年度正收益比例 5% 下界约 50.00%，留一年最小命中率约 23.33%。其他看似更强的状态大多样本不足，例如成交额扩张、恐慌衰竭和低下跌集中度只有 14-27 个事件。结论是 `research_only_no_robust_market_state_rule`：市场状态过滤没有解决强行业选择的稳健性问题。输出目录：`outputs/industry_rebound_leader_market_state_v4_81/report.md`。

V4.82 新增“新 PIT 信息源强行业验证资格审计”，目标不是继续调参，而是确认有没有新的、事前可用的信息源可以补足强反弹行业选择。当前结论是 `research_only_new_pit_source_not_ready`：东方财富行业历史资金流接口当前审计失败，不能接入历史回测；同花顺行业资金流 now/5d 可以作为当前/滚动观察，但不能回填历史；本地资金流缓存只有 1 个交易日，低于 60 个观察门槛和 252 个 alpha 验证门槛；同花顺到申万二级映射高置信覆盖约 74.44%、中等置信覆盖约 75.56%，低于 80% 门槛，`production_allowed` 覆盖仍为 0。因此当前还不能用新资金流 PIT 信息证明“找到了强反弹行业”，只能继续缓存和前推观察。输出目录：`outputs/industry_rebound_leader_new_pit_source_v4_82/report.md`。

V4.83 新增“失败尾部护栏强行业审计”，只测试少量事前可见护栏：企稳分位地板、流动性分位地板、估值支持地板，以及“超跌但未企稳/流动性差”的飞刀过滤。该版本共测试 128 条规则，6 条通过点估计门槛，但 0 条通过完整稳健门槛；最优规则仍是“不加额外护栏”的 `deep_highvol_liq_repair + oversold_liquidity_score Top10`，平均超额约 1.93%、Top20% 命中率约 30.94%，但 bootstrap Top20% 命中率 5% 下界仍约 26.56%，bootstrap 年度正收益比例 5% 下界仍约 50.00%，留一年最小命中率仍约 23.33%。结论是 `research_only_no_robust_trap_guardrail_rule`：简单事前尾部护栏不能证明已经找到强反弹行业，也没有优于原始最接近规则。输出目录：`outputs/industry_rebound_leader_trap_guardrail_v4_83/report.md`。

V4.84 新增“结构型价格成交特征强行业审计”，使用申万二级日频估值/成交历史在信号日构造短期修复强度、成交/占比加速、PB/PE/股息率估值质量及组合分。该版本只在 V4.80/V4.81 最接近成功的两个状态内测试 48 条规则，特征覆盖全部通过 95% 门槛，但 0 条规则通过点估计门槛，0 条通过稳健门槛；最优规则为 `deep_highvol_liq_repair + liquidity_acceleration_score Top20`，平均超额约 -0.81%，Top20% 命中率约 21.25%。结论是 `research_only_no_robust_structure_feature_rule`：成交修复、价格修复和估值质量这些结构型行业指数特征并不能稳定选出反弹更猛的行业。输出目录：`outputs/industry_rebound_leader_structure_features_v4_84/report.md`。

V4.85 新增“父行业中性强反弹行业审计”，检查强行业选择是否被单一申万一级/父行业集中度拖累。该版本测试全市场排序、父行业内排序、每个父行业最多选 1 个或 2 个二级行业等 108 条规则；父行业映射覆盖 131/131，审计通过。最优规则为 `deep_highvol_liq_repair + global_rank_parent_cap1 + oversold_liquidity_score Top10`：平均超额约 2.03%，Top20% 命中率约 32.19%，平均覆盖 10 个父行业、单一父行业权重约 10%，并通过点估计和留一年验证；但 bootstrap Top20% 命中率 5% 下界约 27.81%，低于 30%，bootstrap 年度正收益比例 5% 下界约 50.00%，低于 60%。结论是 `research_only_no_robust_parent_neutral_rule`：父行业分散约束是目前最接近成功的改进方向，但仍不能证明已经稳定找到强反弹行业。输出目录：`outputs/industry_rebound_leader_parent_neutral_v4_85/report.md`。

V4.86 新增“父行业 cap1 强反弹规则前推观察包”，把 V4.85 最接近成功的规则冻结为 `deep_highvol_liq_repair + global_rank_parent_cap1 + oversold_liquidity_score Top10`，并用当前 2026-06-18 反弹窗口生成 10 个父行业分散候选，写入前推账本 `logs/v4_85_parent_neutral_forward_ledger.csv`。当前前推批次 1 个、候选 10 个、已结算批次 0、目标结算批次 30；计划入场日 2026-06-23，计划退出日 2026-07-21。该版本同时新增 V4.85 专用结算脚本，当前未到退出日所以结算行数为 0；结算脚本已支持在退出日后写入真实相对收益和 `top_quintile_hit`，用于 V4.87 的前推 Top20% 命中率评价。结论是 `research_only`：V4.85 规则已冻结并开始前推观察，但还不能声称已找到稳定强反弹行业。输出目录：`outputs/industry_rebound_leader_parent_neutral_forward_v4_86/report.md`。

V4.87 新增“强反弹行业证据计分卡”，把 V4.85/V4.86 的历史证据、前推账本和未来晋级门槛固化，避免未来样本结算后临时改口径。当前计分卡 11 项：通过 4 项、失败 2 项、待观察 5 项；失败项仍是 `bootstrap_top_quintile_hit_p05` 和 `bootstrap_positive_year_p05`，待观察项来自前推批次不足和前推收益尚未结算。晋级协议要求至少 30 个独立前推批次结算，且前推平均超额 > 0、正超额批次比例 >= 55%、Top20% 命中率 >= 30%；未满足前继续保持 `research_only` 和 `auto_execution_allowed=false`。输出目录：`outputs/industry_rebound_leader_evidence_scorecard_v4_87/report.md`。

V4.88 新增“父行业 cap1 入场前一致性审计”，检查 V4.85 冻结候选在计划入场日前是否仍满足前推样本条件：反弹窗口触发记录存在、当前日期不晚于计划入场日、计划退出日晚于入场日、冻结候选 Top10 和当前重算 Top10 一致、前推账本和冻结候选一致、父行业 cap1 约束满足、特征和价格日期不晚于信号日、自动执行保持 false。当前 11 项全部通过，状态为 `pre_entry_consistent`；但仍要求 2026-06-23 入场日重新运行 live refresh，若候选漂移、窗口失效或审计失败，则该批次应标记为 skipped，不得计入有效前推样本。输出目录：`outputs/industry_rebound_leader_pre_entry_audit_v4_88/report.md`。

V4.89 新增“强反弹行业目标就绪度审计”，专门回答“是否已有强反弹行业评价体系，以及按该体系是否已经找到强反弹行业”。当前结论是：评价体系已经存在，V4.85 父行业 cap1 规则也已冻结进入前推观察；但历史稳健性仍有 1 项失败、真实前推仍有 2 项待结算，因此 `can_claim_strong_rebound_industries=false`、`goal_ready=false`。当前只能说“有可观察候选规则”，不能说“已经稳定找到反弹窗口下更猛的行业”。输出目录：`outputs/audit/rebound_leader_goal_readiness_v4_89/report.md`。

V4.90 新增“强反弹行业前推入场门控”，把 V4.88 的入场日前一致性检查转成 entered/skipped 决策边界：未到入场日时保持 `not_due`；到 2026-06-23 入场日必须先同日刷新 live refresh 和 V4.88；若候选稳定、窗口有效且审计无失败，才允许将账本标为 `entered_research_observation`；若审计失败，则标为 `skipped_forward_observation`，不得计入强反弹行业前推评价。当前 2026-06-20 状态为 `not_due`、`apply_allowed=false`，不写入账本；debug 中新增 `entry_operator_checklist.csv`，明确下一步应在 2026-06-23 运行 `python .\scripts\run_v4_71_live_refresh.py --trade-date 2026-06-23`，然后只读检查 V4.90，只有 `apply_allowed=true` 才能运行 `--apply` 写账本。V4.85 前推结算脚本也已收紧：退出日到期但没有 V4.90 入场确认的批次会被标为 `entry_not_confirmed` 并跳过，不会进入强反弹命中率和相对收益评价。输出目录：`outputs/audit/rebound_leader_entry_batch_gate_v4_90/report.md`。

V4.91 新增“强反弹行业晋级数学口径审计”，把未来前推样本的通过条件预注册成机器可读门槛，并修正为和 V4.87 实际计算一致的候选行级 Top20% 命中口径：至少 30 个独立前推批次；V4.85 每批 Top10，因此 30 批次对应 300 个候选行业行；30 批次时至少 17 次正超额批次，至少 90 个候选行命中 Top20% 强反弹，且平均相对收益必须大于 0。当前已结算前推批次为 0，因此全部前推表现项仍为 pending；该版本不改变候选规则、不新增参数，只防止未来样本出来后临时修改评价口径。输出目录：`outputs/audit/rebound_leader_promotion_math_v4_91/report.md`。

V4.92 新增“强反弹行业指标粒度一致性审计”，检查 V4.87、V4.91、V4.85 结算脚本和前推账本是否都使用同一个 Top20% 命中粒度。当前审计通过：V4.87 的 `top_quintile_hit_rate` 来自账本候选行 `top_quintile_hit` 的均值；V4.85 结算脚本逐候选行写入 `top_quintile_hit`；V4.91 使用 30 批次 x 每批 Top10 = 300 个候选行，Top20% 命中门槛为 90 行；当前账本和 tracker 都显示本批次 10 行。该版本不新增策略，只防止把候选行级命中率误读为批次级命中。输出目录：`outputs/audit/rebound_leader_metric_grain_v4_92/report.md`。

V4.93 新增“强反弹行业历史回测总审计”，只汇总 V4.72-V4.85 已有历史回测和少量事前市场质量过滤探针，专门回答“能否仅靠历史回测完成找到强反弹行业目标”。当前汇总 12 个版本、已记录 796 条测试规则；最接近规则仍是 V4.85 `deep_highvol_liq_repair + global_rank_parent_cap1 + oversold_liquidity_score Top10`，平均相对收益约 2.03%、Top20% 命中率约 32.19%，但 bootstrap Top20% 命中率 5% 下界只有约 27.81%，低于 30% 门槛；训练期选因子样本外、逐年前推、宽松市场质量过滤样本保留也未通过。因此 `can_claim_strong_rebound_industries_from_backtest=false`：当前历史回测只能给出候选规则，不能证明已经稳定找到反弹窗口下更猛的行业。输出目录：`outputs/audit/rebound_leader_historical_backtest_verdict_v4_93/report.md`。

V4.94 新增“强反弹行业独立事件审计”，把 V4.85 最接近规则的 32 个日信号按持有期重叠合并，只保留每个反弹簇的第一条可交易信号。结果只剩 8 个独立反弹事件，覆盖 5 个年份；独立事件平均相对收益约 1.70%、Top20% 命中率约 35.00%，点估计仍为正，但独立样本数远低于 30 个门槛。因此这个版本进一步确认：V4.85 的历史证据主要来自少数反弹簇内的连续日信号，不能把 32 个日信号当作 32 个独立证明。输出目录：`outputs/audit/rebound_leader_independent_event_audit_v4_94/report.md`。

V4.95 新增“反弹窗口样本容量审计”，检查当前 V4.70 反弹窗口定义本身是否有足够历史容量支撑强行业选择评价。结果显示：原始日信号有 57 个，但按持有期不重叠合并后只有 19 个独立窗口，低于 30 个历史评价门槛；覆盖年份为 9 年，但 V4.85 最接近规则真正进入强行业选择的独立事件只有 8 个。因此在不扩展或重定义反弹窗口样本池前，无法仅靠当前历史回测证明能稳定选出反弹更猛的行业。输出目录：`outputs/audit/rebound_window_sample_capacity_audit_v4_95/report.md`。

V4.96 新增“反弹窗口扩展容量审计”，只测试少量事前可解释的扩展窗口定义，不使用未来收益挑窗口。结果显示 `vol_repair`（20/60 日波动比 >= 1.05、5 日流动性修复 >= 0.03、10 日市场收益 <= 0.03）可把持有期不重叠独立窗口扩到 33 个，覆盖 12 年，平均窗口收益约 0.39%、坏窗口率约 33.33%，达到进入强行业选择回测的最低容量和基本质量门槛。但这只解决窗口池容量，不代表已经找到强行业 alpha。输出目录：`outputs/audit/rebound_window_expansion_capacity_audit_v4_96/report.md`。

V4.97 新增“扩展反弹窗口强行业选择回测”，在 V4.96 预先定义的 `vol_repair` 33 个独立窗口内，复用 V4.72 的行业排序评价体系重跑强行业选择。当前最优为 `oversold_liquidity Top20`，平均相对收益约 0.11%，但中位数相对收益为负、胜率约 48.48%、Top20% 强反弹命中率约 23.18%、样本外平均相对收益约 -0.17%，0 条规则通过强行业门槛。结论是：扩展窗口解决了历史样本数不足，但没有解决“找到反弹更猛行业”的 alpha 问题。输出目录：`outputs/industry_rebound_leader_expanded_window_v4_97/report.md`。

V4.98 新增“扩展窗口特征分离度审计”，在 `vol_repair` 33 个独立窗口内检查现有 9 个价格/估值/超跌/企稳/流动性特征是否能稳定区分未来 Top20% 强反弹行业。当前 0 个特征通过分离度门槛；最优特征为 `oversold_score`，平均 RankIC 约 0.0392、正 RankIC 比例约 45.45%、标准化 Top-vs-Rest gap 为 -0.0342、正 gap 比例约 48.48%。这说明现有特征在扩展窗口内没有稳定分离未来强反弹行业，继续在同一批特征上调 TopN 或权重意义有限。输出目录：`outputs/audit/rebound_leader_expanded_feature_separability_v4_98/report.md`。

V4.99 新增“市场敏感度强行业回测”，在 `vol_repair` 扩展窗口内测试行业 60/120 日 beta、120 日相关性、下跌日捕获、残差波动等信号日前可计算特征。最接近规则为 `beta_120_rank Top5`：平均相对收益约 1.34%、Top20% 命中率约 34.55%、样本外平均相对收益约 1.63%，基础门槛通过；但完整稳健门槛未通过，bootstrap Top20% 命中率 5% 下界约 27.24%，低于 30%，bootstrap 正收益年份 5% 下界约 45.45%，低于 60%。结论是 beta 类特征比旧特征更有方向，但仍不能证明已经稳定找到反弹更猛行业。输出目录：`outputs/industry_rebound_leader_market_sensitivity_v4_99/report.md`。

V5.00 新增“Beta 组合强行业回测”，只测试少量事前可解释组合：`beta_120` 分别叠加超跌、超跌流动性、企稳、流动性和估值超跌企稳分数。结果显示组合没有优于单独 `beta_120_rank Top5`；最接近规则仍是 `beta_120_rank Top5`，平均相对收益约 1.34%、Top20% 命中率约 34.55%，但 bootstrap Top20% 命中率 5% 下界约 27.24%、bootstrap 正收益年份 5% 下界约 45.45%，完整稳健门槛仍未通过。结论是：高 beta 是目前最有方向的线索，但简单叠加旧特征不能把它升级为稳健强行业规则。输出目录：`outputs/industry_rebound_leader_beta_composite_v5_00/report.md`。

V5.01 新增“Beta 失败分层审计”，不新增交易规则，只诊断 `vol_repair + beta_120_rank Top5` 这条最接近线索的失效位置。33 个事件里失败事件 18 个，平均相对收益仍约 1.34%、Top20% 命中率约 34.55%；状态分层显示 `high_liquidity_repair` 桶平均相对收益约 -0.39%，父行业暴露里医药生物平均事件相对收益约 -2.68%。结论是：beta 线索需要继续用事前窗口状态和父行业暴露过滤，但 V5.01 仍不能声称已经找到稳定强反弹行业。输出目录：`outputs/audit/rebound_leader_beta_failure_stratification_v5_01/report.md`。

V5.02 新增“Beta 守门强行业回测”，只测试 V5.01 指出的两个失败源：剔除 `high_liquidity_repair` 状态、剔除医药生物父行业暴露，以及二者组合。结果没有通过完整门槛；最优仍是未过滤的 `baseline_beta_top5`，平均相对收益约 1.34%、Top20% 命中率约 34.55%，但 bootstrap Top20% 命中率 5% 下界约 27.24%、bootstrap 正收益年份 5% 下界约 45.45%。`low_liquidity_repair_beta_top5` 均值升至约 1.99%，但事件数只有 24，样本容量和留一年稳健性失败。结论是：简单守门能改善点估计，但不能证明已经找到稳定强反弹行业。输出目录：`outputs/industry_rebound_leader_beta_guardrail_v5_02/report.md`。

V5.03 新增“窗口质量强行业回测”，不重新调行业因子，只对 `baseline_beta_top5` 事件打窗口质量标签并过滤。结果显示质量过滤能提高点估计，但不能通过完整稳健门槛：`quality_score_ge3` 平均相对收益约 3.48%、Top20% 命中率约 41.82%，但只有 11 个事件；`quality_score_ge2` 平均相对收益约 2.77%、Top20% 命中率约 38.89%，但只有 18 个事件。最优可排序规则仍是未过滤的 `baseline_beta_top5`，且 bootstrap 下界仍失败。结论是：窗口质量确实解释了 beta 线索的强弱，但当前历史样本不足以证明稳定强行业 alpha。输出目录：`outputs/industry_rebound_leader_window_quality_v5_03/report.md`。

V5.04 新增“小样本证据冻结”，把 `quality_score_ge2 + beta_120_rank Top5` 和 `quality_score_ge3 + beta_120_rank Top5` 固定为前推观察规则，不再允许根据历史结果继续调整阈值、TopN 或 beta 定义。冻结时点的历史证据分别为：`quality_score_ge2` 18 个事件、平均相对收益约 2.77%、Top20% 命中率约 38.89%；`quality_score_ge3` 11 个事件、平均相对收益约 3.48%、Top20% 命中率约 41.82%。结论是：这两个规则有观察价值，但历史样本不足，只能通过未来新增样本晋级，不能声称目标已完成。输出目录：`outputs/audit/rebound_leader_evidence_freeze_v5_04/report.md`。

V5.05 新增“冻结规则前推跟踪器”，为 V5.04 的两个冻结规则建立前推账本和晋级进度表。当前已结算前推样本为 0，`quality_score_ge2` 距离新增 12 个前推样本还差 12 个、距离总事件 30 个还差 12 个；`quality_score_ge3` 距离新增 12 个前推样本还差 12 个、距离总事件 30 个还差 19 个。持久账本路径为 `logs/v5_05_rebound_leader_forward_ledger.csv`，后续只能通过 `python .\scripts\append_v5_05_rebound_leader_forward_sample.py --frozen-rule <规则> --signal-date <YYYY-MM-DD> --entry-date <YYYY-MM-DD> --exit-date <YYYY-MM-DD>` 追加新样本。结论是：系统已具备后续追加样本验证的账本，但没有新增已结算样本前，不能声称已找到稳定强反弹行业。输出目录：`outputs/audit/rebound_leader_forward_tracker_v5_05/report.md`。

V5.06“前推样本结算器”现直接读取逐行业申万价格缓存，不再使用停在 2026-06-12 的估值合并表。结算只接受预先冻结的确切入场日和退出日，不会跳到更晚的可用日期；选中行业必须全部有价格，全行业基准同日覆盖至少 120 个行业，否则保持待结算。当前账本行数为 0、本次结算行数为 0。结算命令：`python .\scripts\settle_v5_06_rebound_leader_forward_samples.py --as-of-date <YYYY-MM-DD>`。输出目录：`outputs/audit/rebound_leader_forward_settlement_v5_06/report.md`。

V5.07“强反弹行业晋级评价器”同时评价两个独立问题：至少 20 个去重前推窗口的全行业基准平均/中位收益为正且胜率不低于 55%，才通过前推择时门禁；冻结行业规则仍需满足原有事件数、12 个前推事件、相对收益、Top20% 命中率、bootstrap 和留一年门槛。两项同时通过后，当前 ETF runner 才改走 `forward_validated` 证据路线，并只读取 V5.08 当前触发且已晋级规则的行业候选。当前前推事件为 0，因此两项都未通过，系统仍为 `NO_ACTION`。输出目录：`outputs/audit/rebound_leader_promotion_evaluator_v5_07/report.md`。

V5.08“冻结规则前推信号检测器”现在直接读取 V4.71 截至检测日的实时 `source_panel.csv`，不再把已经具有未来收益的历史完成交易表当作信号源；行业 Top5 只用信号日及以前 120 日 beta 排序，同日行业行情覆盖少于 120 个时拒绝追加，交易日历固定 T+2 入场和 20 个交易日持有。证据起点从不可变研究实验哈希账本读取为 `2026-07-12`，只有冻结后、规则触发、入场前已检测且未重复的样本才允许 `--apply` 自动追加。以 `2026-07-13` 检测时，实时面板尚无冻结后的新信号，故 `appendable_signal_count=0`、账本仍为 0 行；这表示“尚未触发”，不是把旧窗口补入前推证据。日常刷新链会在安全追加后重新生成跟踪与晋级结果。输出目录：`outputs/audit/rebound_leader_forward_signal_detector_v5_08/report.md`。

V5.09 新增“历史伪前推审计”，在不改变冻结规则的前提下，模拟 2018/2020/2022 年后只看后续事件的表现。结果显示方向仍为正：`quality_score_ge2` 在 2018/2020/2022 后的平均相对收益约为 2.62%/3.28%/3.20%，Top20% 命中率约为 34.55%/35.56%/40.00%；`quality_score_ge3` 在 2018/2020/2022 后的平均相对收益约为 3.52%/3.37%/3.18%。但全部 6 个切分都因后验事件数不足而失败，最宽的 `quality_score_ge2` 2018 后也只有 11 个事件，低于 12 个伪前推事件门槛。结论是：伪前推支持“方向值得观察”，但仍不能证明目标完成。输出目录：`outputs/audit/rebound_leader_pseudo_forward_audit_v5_09/report.md`。

V5.10 新增“目标完成度审计”，把目标拆成可验证检查项：反弹窗口样本池、小样本规则冻结、前推账本为通过；强反弹行业历史规则、已结算前推样本、晋级评价、历史伪前推为失败；冻结后新信号为待观察。当前 `pass_count=3`、`fail_count=4`、`pending_count=1`、`goal_ready=false`。结论是：系统已经具备评价闭环，但仍不能声称已经找到稳定强反弹行业。输出目录：`outputs/audit/rebound_leader_goal_completion_audit_v5_10/report.md`。

V5.11 新增“PIT 估值增强审计”，用申万二级历史 PE/PB/股息率在信号日构造固定特征，测试低 PB、低 PE、高股息和 `beta_low_pb_score` 是否能在反弹窗口里选出更强行业。结果没有通过门槛，最优 `beta_low_pb_score` 平均相对收益约 0.60%、Top20% 命中率约 26.06%，弱于此前纯 beta 线索；低 PE、低 PB、高股息单独使用也没有稳定 alpha。结论是：当前可用 PIT 估值数据不能解决强反弹行业选择问题。输出目录：`outputs/audit/rebound_leader_pit_valuation_audit_v5_11/report.md`。

V5.12 新增“PIT 估值历史分位审计”，不再只看绝对低 PE/PB/高股息，而是用行业自身 3 年历史估值分位构造 `pb_3y_cheap_rank`、`pe_3y_cheap_rank`、`dividend_3y_high_rank` 和 `beta_pb_percentile_score`。结果仍未通过强反弹行业门槛：最优 `beta_pb_percentile_score` 平均相对收益约 0.47%、Top20% 命中率约 27.10%，样本外平均相对收益约 -0.19%，通过规则数为 0。结论是：PIT 估值历史分位也不能证明已经找到反弹窗口内更强行业。输出目录：`outputs/audit/rebound_leader_pit_valuation_percentile_audit_v5_12/report.md`。

V5.13 新增“早期相对强弱确认审计”，在反弹窗口触发后等待 3/5/10 个交易日，只用这段已经发生的行业相对强弱做排序，再从确认日持有到原退出日。最优规则为 `early_beta_score` Top5、等待 5 个交易日，平均相对收益约 1.76%、Top20% 命中率约 33.33%、样本外平均相对收益约 2.44%；但 bootstrap 稳健性未通过，通过规则数仍为 0。结论是：早期确认方向值得观察，但仍不能声称已经找到稳定强反弹行业。输出目录：`outputs/audit/rebound_leader_early_confirmation_audit_v5_13/report.md`。

V5.14 新增“确认期过滤审计”，只测试确认日已经可见的固定过滤：确认期全行业平均跌幅不超过 5%，以及确认期行业分化低于 3%。最接近的规则为 `no_severe_early_selloff + early_beta_score Top5 + 5日确认`，事件数 31，平均相对收益约 2.15%，Top20% 命中率约 34.19%，样本外平均相对收益约 2.44%；但 bootstrap Top20% 命中率 5% 下界约 28.39%，正年份比例 5% 下界约 54.55%，仍低于硬门槛，通过规则数为 0。结论是：确认期不过度下跌能改善点估计，但还不能证明稳定强反弹行业选择能力。输出目录：`outputs/audit/rebound_leader_confirmation_filter_audit_v5_14/report.md`。

V5.15 新增“强反弹行业失败归因审计”，只诊断 V5.14 最接近规则 `no_severe_early_selloff + early_beta_score Top5 + 5日确认`，不生成新交易规则。31 个事件中失败事件 16 个，最大失败桶是 `post_confirm_market_down`，共 9 个事件；这说明强行业选择的不稳定性有一部分来自确认日之后市场继续下跌，而这类信息在入场前不可见，不能直接当作过滤条件。高频失败暴露行业包括电机、金属非金属新材料、稀有金属、其他电源设备Ⅱ、厨卫电器。结论是：V5.15 完成失败归因，但没有产生通过门槛的新强行业规则。输出目录：`outputs/audit/rebound_leader_failure_diagnosis_v5_15/report.md`。

V5.16 新增“窗口质量代理审计”，只用确认日已经可见的窗口质量代理复测 V5.14 强行业规则：温和确认期下跌、不过热确认、适中行业分化等。更强代理的点估计更好，例如 `mild_early_pullback` 平均相对收益约 5.53%，但只有 9 个事件；`mid_dispersion_confirmation` 平均相对收益约 3.12%，但只有 15 个事件。唯一样本够的仍是 V5.14 的 `no_severe_early_selloff`，平均相对收益约 2.15%，但 bootstrap 稳健性仍未过。结论是：窗口质量代理方向有效但样本不足，不能声称已经找到稳定强反弹行业。输出目录：`outputs/audit/rebound_window_quality_proxy_audit_v5_16/report.md`。

V5.17 新增“压力恢复阶段样本扩展审计”，不再只依赖旧的 33 个反弹窗口，而是从 131 个申万二级行业日线重新生成更宽的压力后恢复阶段样本，再用 `early_beta_score Top5 + 5日确认` 测试强行业选择。样本数确实上来了：`mild_pressure_recovery` 有 52 个事件，平均相对收益约 1.13%，Top20% 命中率约 31.15%；`drawdown_repair_recovery` 有 54 个事件，平均相对收益约 0.66%，Top20% 命中率约 31.11%。但两者都没有通过点估计和稳健性门槛。结论是：简单扩样会削弱 alpha，V5.14/V5.16 的较好表现依赖更窄、更高质量的窗口，不能用宽样本直接证明目标完成。输出目录：`outputs/audit/rebound_phase_sample_expansion_audit_v5_17/report.md`。

V5.18 新增“滚动失败隔离审计”，测试能否只用当时已经结算的历史失败记录，把过去反弹窗口中反复失败的行业临时排除。结果没有改善：基线 `baseline_no_quarantine` 仍是最优，平均相对收益约 2.15%，Top20% 命中率约 33.55%；`quarantine_after_2_prior_losses` 降到约 1.30%，`quarantine_after_1_prior_loss` 降到约 1.10%。结论是：事前滚动隔离会误伤后续反弹行业，不能解决强行业选择稳定性问题。输出目录：`outputs/audit/rebound_leader_rolling_quarantine_audit_v5_18/report.md`。

V5.19 新增“量能确认审计”，用确认日前已知的申万二级行业成交额构造 `amount_surge_rank` 和 `early_beta_amount_score`，测试成交额放大能否改善 V5.14 的强行业选择。结果仍未通过：最优 `early_beta_amount_score` 平均相对收益约 1.70%，中位数相对收益约 -0.11%，相对胜率约 48.39%，弱于 V5.14 基线；纯 `amount_surge_rank` 更弱。结论是：确认期成交额放大不是当前强反弹行业选择的有效补充信息。输出目录：`outputs/audit/rebound_leader_volume_confirmation_audit_v5_19/report.md`。

V5.20 新增“强反弹行业证据边界审计”，汇总 V5.11-V5.19 的全部新增证据。审计结论是 `local_historical_features_insufficient`：PIT 估值、估值历史分位、早期相对强弱、确认期过滤、窗口质量代理、压力恢复阶段扩样、滚动失败隔离和成交额确认均没有任何规则通过完整强反弹行业门槛。最接近的仍是 V5.14/V5.16 的窄窗口早期确认规则，但仍是 `research_only`。结论是：现有本地历史字段不能证明目标完成；下一步需要冻结规则后的前推样本，或真正新增的 PIT 信息源。输出目录：`outputs/audit/rebound_leader_evidence_boundary_audit_v5_20/report.md`。

V4.72 当前候选表使用 `valuation_snapshot_plus_price_history`：估值来自不晚于信号日的最新快照，价格特征来自申万二级行业历史，并剔除价格历史距离信号日超过 7 天的行业。当前候选的 `feature_date=2026-06-18`、`signal_date=2026-06-18`、`price_date=2026-06-18`、最大价格滞后 0 天，前几名为养殖业、保险Ⅱ、游戏Ⅱ、教育、乘用车；这些仍是 `research_only` 候选，不是交易指令。
候选表里的 `historical_failure_flag` 只作为人工复核提示：当前养殖业、白酒Ⅱ因在最差事件中反复出现而被标记为 `recent_or_repeated_worst_event_industry`，不会被自动剔除。
V4.72 同时生成 `debug/industry_candidate_carrier_mapping.csv`、`debug/carrier_mapping_audit.csv`、`debug/carrier_exposure_audit.csv`、`debug/carrier_tracking_audit.csv` 和 `debug/pre_trade_manual_review_sheet.csv`：用公开 ETF 实时列表做保守关键词候选映射，汇总覆盖率、低置信度、低流动性和无匹配情况，读取 ETF 行业配置做宽行业暴露观察，并用 ETF 与申万二级行业指数最近一年日收益做粗跟踪审计。这些审计仍不是申万二级精确跟踪验证；入场前人工复核清单明确 `auto_execution_allowed=否`。完成行业载体、流动性、折溢价/跟踪误差、仓位上限和入场价漂移复核前，不允许自动执行。


<!-- END MIGRATED README LINES 86-188 -->
