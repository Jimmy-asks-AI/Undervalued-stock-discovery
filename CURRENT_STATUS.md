# CURRENT_STATUS｜当前项目状态

> 本页由 `scripts/build_current_status.py` 从当前运行摘要、状态一致性审计、冻结账本和研究治理审计生成。生成成功不等于研究或交易就绪。

**当前唯一结论：`research_only / NO_ACTION`。强行业 Alpha 未验证；人工辅助交易未就绪；自动交易禁止。**

## 一眼看清

| 项目 | 当前值 |
|---|---|
| 状态生成时间 | `2026-07-21T23:51:58` |
| 当前日期 | `2026-07-21` |
| 决策 as-of | `2026-07-18` |
| 当前动作 | `NO_ACTION` |
| 证据口径 | `research_only` |
| 强行业 Alpha | `未验证`（合格前推样本 0） |
| PIT估值/行业历史方法门 | `阻断`（可晋级估值行 0） |
| 人工辅助交易 | `未就绪` |
| 自动交易 | `禁止` |
| 状态源一致性 | `pass` |
| 研究治理覆盖 | `pass` |
| 本页有效性 | `valid` |

`V4.70` 的历史框架分数不能覆盖 `V4.71 production_ready=false`、`V5.10 goal_ready=false` 和当前硬门禁；它不是当前结论。

## 日期与真实数据截止日

runner 合同中的 `data_cutoff_date=2026-07-18` 只是本次请求的决策边界，表示不得读取此日之后的数据；它**不是**各数据源都更新到该日的声明。实际截止日如下。

| 数据源 | 真实截止日 |
|---|---|
| `industry_history` | `2026-07-15` |
| `valuation_history` | `2025-12-31` |
| `pit_valuation_methodology` | `2025-12-31` |
| `valuation_snapshot` | `2026-07-16` |
| `market_index` | `2026-07-16` |
| `etf_history` | `2026-07-16` |
| `etf_pit_master` | `2026-07-16` |
| `timing_evidence` | `2026-07-15` |
| `industry_candidate_evidence` | `无可用日期` |
| `fund_flow` | `2026-06-26` |
| `account_state` | `2026-07-13`（门禁失败） |

空日期表示该源在本轮没有可用于决策的证据；不能用运行日补齐。

探索性资金流终局另行使用结算专用行情：精确入场日 `2026-06-23` 与退出日 `2026-07-21` 的同一行业交集为 123，目标 4/4。这批数据只用于旧观察的终局核验，不改写决策 as-of `2026-07-18`，不改变上表主线 `industry_history=2026-07-15` 的截止口径，也不计算或补录收益。

## PIT估值与行业历史口径

- 方法控制审计：`pass`；历史晋级门：`blocked`。
- 官方直接来源估值截止：`2025-12-31`；原表最大日期 `2026-06-12` 中含回收快照 131 行，已隔离。
- 估值可得性：`unavailable_for_promotion`；可晋级估值行：0。
- 行业历史文件：131；当前新鲜文件：123；长尾缺口：7；普通陈旧：1（801156）。
- 观察名称分段：166；名称或口径发生变化的代码：35；已确认语义复用代码：2。
- 历史 beta 身份安全：`blocked`；已排除指标：beta_low_pb_score。
- 独立前推证据复验：`blocked`；缺口：`append_only_forward_ledger_verifier_missing`。
- 旧回测口径：`historical_review_used_in_iteration`；真正前推证据最早日期：`2026-07-12`。
- 当前缺口：valuation_publication_timestamp_missing、valuation_available_date_unproved、valuation_source_version_missing、valuation_revision_status_missing、historical_industry_classification_membership_unavailable、historical_beta_identity_episode_recomputation_unverified、historical_pit_and_classification_receipt_verifier_missing。

## 版本坐标

| 类型 | 名称 | 产物版本 | 证据边界 |
|---|---|---|---|
| 策略版本 | `V4.70` | `4.70.0` | 冻结市场反弹窗口；不等于强行业选择 Alpha。 |
| 稳健性审计版本 | `V4.71` | `4.71.0` | V4.70 的参数扰动、独立样本与实盘辅助审计。 |
| 强行业研究版本 | `V4.85` | `4.85.0` | 父行业中性候选规则；当前无稳健通过规则。 |
| 前推评价版本 | `V5.07` | `5.07.0` | 只评价冻结规则与已结算前推样本。 |
| 前推检测版本 | `V5.08` | `5.08.0` | 只检测冻结日后的自然触发，不回填历史。 |
| 研究审计版本 | `V5.10` | `5.10.0` | 目标完成度审计；不是策略版本。 |
| 数据治理版本 | `V5.31 / V5.35` | `5.31.2 / 5.35.1` | V5.31 固定不可变证据 cohort，V5.35 只管理等待室；二者都不证明 Alpha。 |
| 当前 runner 版本 | `CURRENT_MAINLINE` | `1.0.0` | ETF 辅助人工决策聚合层；硬门禁未清零时只允许 NO_ACTION。 |
| 前推 cohort（数据批次） | `ff_integrity_v8_20260721` | `facaa1a541c8…` | cohort 是证据冻结批次，不是软件版本、策略版本或 Alpha 结论。 |

策略版本、研究审计版本、数据治理版本和前推 cohort 是四类不同对象，禁止相互替代。

## 两层冻结规则

### 第一层：强行业规则前推冻结

`logs/research_experiment_ledger.jsonl` 只预注册冻结日之后的新前推样本，不追认历史结果。

| 冻结规则 | 证据起点 | 每条规则最低新样本 | 允许动作 | 禁止动作 |
|---|---|---:|---|---|
| `vol_repair window + beta_120_rank Top5 + window_quality_score >= 2` | `2026-07-12` | 12 | `append_new_forward_samples_only` | `do_not_change_thresholds_from_historical_results` |
| `vol_repair window + beta_120_rank Top5 + window_quality_score >= 3` | `2026-07-12` | 12 | `append_new_forward_samples_only` | `do_not_change_thresholds_from_historical_results` |

### 第二层：资金流证据 cohort 冻结

- active cohort：`ff_integrity_v8_20260721`
- manifest：`facaa1a541c8160ca1039bc0649eb658ad4de48ee4f85b4de9a8dbb1aa68d360`
- 复验：`pass`；`active cohort baseline and history chain verified`
- 只有 cohort_id 与 manifest_hash 同时匹配、且在冻结时限内取得的证据才可能进入 active 样本；legacy、错配 hash 和迟到回填一律隔离。
- cohort 冻结只证明证据未漂移，不证明强行业 Alpha。

## 样本账本

| 样本口径 | 数量 | 能否用于强行业结论 |
|---|---:|---|
| 强行业冻结规则合格前推样本 | 0 | 否；每条冻结规则最低要求 12 |
| active fund-flow 观察样本 | 0 | 仅进入证据链，不自动合格 |
| active fund-flow 完整性合格样本 | 0 | 否；仍须目标资格、结算和晋级 |
| active fund-flow 已结算且目标合格样本 | 0 | 只能进入后续批次评价 |
| 探索性 fund-flow 样本总数 | 4 | 否，永久与合格前推样本分栏 |
| 探索性 fund-flow 已结算 | 0 | 否，不计入目标样本 |
| 探索性 fund-flow terminal blocked | 4 | 否，永久排除且不得补价转正 |
| 探索性 fund-flow pending | 0 | 否，只等待合法终局处置 |
| 探索性 fund-flow qualified settled | 0 | 必须保持 0 |
| 迟到回填排除观察 | 4 | 否，不得事后转正 |

四条探索观察的独立终局处置已经通过摘要、逐行记录和 active pair 复核：settled 0 / terminal blocked 4 / pending 0 / qualified settled 0。终局不含收益，不证明或否定强行业 Alpha。

V4.71 与 V4.85 的旧计划观察不在上述“强行业合格前推样本”内；历史候选、探索观察和真实前推证据不能混算。

## 当前硬门禁

| 门禁 | 状态 | 当前证据 |
|---|---|---|
| 数据完整性与时点 | `通过` | industry_history=pass:2026-07-15; valuation_history=pass:2025-12-31; valuation_snapshot=pass:2026-07-16; market_index=pass:2026-07-16; etf_history=pass:2026-07-16; etf_pit_master=pass:2026-07-16; timing_evidence=pass:2026-07-15 |
| PIT估值与行业历史方法门 | `阻断` | audit_passed=True; historical_promotion_gate=False; true_forward_route=False; eligible_valuation_rows=0; valuation_cutoff=2025-12-31; classification=unavailable |
| V4.71 择时稳健性 | `阻断` | production_ready=False; blocking=3 |
| 强行业选择 | `阻断` | passing_rule_count=0; status=research_only_no_robust_parent_neutral_rule |
| ETF PIT 主表 | `通过` | exists=True; exact_index_code_coverage=0.9657097288676236; historical_mode=observed_trade_intervals; ready=True |
| 账户状态 | `阻断` | path=portfolio_lab/current_account_state.json; errors=stale_as_of_date |
| 现有组合风险 | `阻断` | breaches=none |
| V5.10 目标证据 | `阻断` | goal_ready=False; blocking_nonpass=29 |
| 六角色确定性否决链 | `阻断` | data_pit_steward=fail;market_regime_agent=fail;industry_rank_agent=fail;etf_implementation_agent=pass;portfolio_risk_agent=fail;independent_validation_auditor=fail |
| 建议后组合风险 | `阻断` | breaches=none; strategy_weight=; cash_weight= |
| 择时前推证据 | `阻断` | forward_timing_gate_passed=False |
| 强行业前推证据 | `阻断` | forward_industry_gate_passed=False |

“六角色确定性否决链”是当前统一外部术语；任何一个角色否决，都不能输出买卖建议。

## 下一步允许做什么

- 把账户快照和必需数据源更新到真实可得日期，再以 `2026-07-18` 重新运行当前 runner 与状态一致性审计；更新日期不能替代实际源截止日。
- 先把 V5.07 的可变 CSV 证据改为可重放的 append-only 哈希账本，并在消费端现场复验；在此之前新增样本也不能解除前推门。
- 只在 V5.04 已冻结规则自然触发后，按 `append_new_forward_samples_only` 追加新前推样本；不得从历史窗口回填。
- 只在计划退出日到达、入场价与基准价均按时冻结且 PIT 来源完整时结算收益；不满足则保持 pending 或永久排除。
- 研究治理覆盖已通过；新增或改动研究版本时必须同步维护 task brief、登记或 post-hoc、变更记录和标准输出，并重跑覆盖审计。
- 继续观察和审计；在所有硬门禁清零前维持 `NO_ACTION`。

## 明确禁止

- 禁止生成或执行 ETF 买卖指令，禁止把 `WATCH`、候选清单或框架分数写成买入建议。
- 禁止宣称强行业 Alpha 已验证，禁止把历史候选、探索样本、迟到回填或 cohort 冻结通过混入合格前推样本。
- 禁止根据已经看到的历史结果修改 V5.04 冻结阈值；新规则必须另立实验并重新登记。
- 禁止把工程测试通过、数据门禁局部通过或 `V4.70=100` 写成人工辅助交易已就绪。
- 禁止自动交易；当前 `auto_execution_allowed=false` 是硬边界，不随单个研究门禁通过而解除。
- 禁止在状态一致性或研究治理覆盖失败时把任一旧摘要当作当前权威状态。

## 恢复人工辅助资格的条件

- 当前状态一致性审计和研究治理覆盖审计同时通过，所有当前摘要绑定同一 active cohort pair。
- 必需数据源在决策 as-of 下通过真实时点、覆盖率和 PIT 检查；不得用运行日冒充源截止日。
- V4.71 或其受治理的后续审计通过择时稳健性，且择时前推门禁通过。
- V5.07 前推证据由可重放的 append-only 哈希账本承载，并由消费端现场复验账本头、顺序、规则冻结时间和零历史回填。
- 强行业规则取得预登记要求的新前推样本并通过 V5.07/V5.10；当前最低门槛为每条冻结规则 12 个新事件。
- 账户快照与决策 as-of 一致，现有组合风险及建议后组合风险均通过。
- 六角色确定性否决链在同一决策快照内全部通过，runner 不再产生任何 blocking gate。

这些条件必须在同一决策快照中同时成立。即便人工辅助资格恢复，自动交易仍然禁止，除非另有经过审查的独立授权与执行治理。

## 研究治理状态

- 版本库存：`pass`；65 / 65 条；治理失败 0 条。
- 覆盖审计：`pass`；fail_count=0；check_count=65。
- 工程完成度快照：29/29；就绪门禁：6/13；行为测试：23/23。工程通过不能代替研究证据通过。

治理覆盖缺失或失败时，本页只能作为失败关闭的状态快照，不能作为发布、实盘或研究完成的验收凭证。

## 可复核入口

- [项目全量审查报告](notes/项目全量审查报告_2026-07-17.md)
- [可复现性与测试报告](notes/reproducibility_and_test_report.md)
- 当前 runner 摘要：`outputs/etf_assisted_trading_current/run_summary.json`
- 当前状态一致性审计：`outputs/audit/current_state_consistency/run_summary.json`
- 当前状态生成审计：`outputs/audit/current_status/run_summary.json`
- 研究治理覆盖审计：`outputs/audit/research_governance_coverage/run_summary.json`
