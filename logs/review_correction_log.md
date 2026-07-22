# Review And Correction Log

## 2026-06-12 - Initial Fundamental Value OS Setup

original understanding:
Use the old high-dividend low-PE plus T-trading framework as the main system.

problem:
The user later clarified that the broader goal is a fundamental-factor undervalued asset discovery system with multi-agent research. The old framework is useful context but too strategy-specific.

corrected understanding:
Build a general fundamental value research OS first. High dividend, ETF timing, and T-trading remain optional overlays or applications.

impact:
The initial artifacts separate fundamental undervaluation scoring from timing/execution overlays and enforce point-in-time data rules.

correction_type:
`concept_fix`

## 2026-06-12 - V0.3 Audit Missing-Value Semantics

original understanding:
Treat the string `none` as a missing value in local audit utilities.

problem:
The factor registry uses `none` as an explicit valid policy value, such as no neutralization for a hard veto factor. Treating it as missing created a false warning.

corrected understanding:
Audit utilities should treat blank, `nan`, and `null` as missing. The literal string `none` can be a valid declared policy when the schema or registry allows it.

impact:
`scripts/validate_asset_panel_schema.py` and `scripts/audit_factor_registry.py` were corrected, and the factor registry audit now passes with 0 warnings.

correction_type:
`code_fix`

## 2026-06-12 - Research Target Switched From Stocks To Industry ETFs

original understanding:
The system should find specific undervalued stocks and then improve stock-level quality and sector-specific risk checks.

problem:
The user clarified that the goal is not to find specific stocks. The desired output is concrete ETFs backed by industry-level research: find undervalued and oversold industries, then map those exposures to ETFs.

corrected understanding:
The primary research object should be the industry or theme index. ETF selection is an implementation layer, and individual stocks should not appear as candidates. The system needs industry valuation factors, oversold factors, ETF mapping and liquidity checks, and multi-agent governance.

impact:
V0.9 added the `Industry ETF Value Research OS`, including new agent configuration, schema, source inventory, factor registry, keyword mapping config, scoring package, current snapshot runner, task brief, and current outputs under `outputs/current_industry_etf_value_snapshot/`.

correction_type:
`scope_fix`

## 2026-06-12 - Financial-Sector Cheapness Can Be Risk Pricing

original understanding:
The V0.7 quality gate could treat financial-sector candidates as undervalued-and-quality if they passed generic valuation, profitability, growth, cash-flow, and shareholder-return thresholds.

problem:
Banks, brokers, and insurers naturally trade on lower PE/PB ranges, and the low valuation can reflect market pricing of credit risk, capital pressure, liquidity risk, trading-book exposure, or insurance liability and solvency risk. Generic quality factors do not answer whether the discount is justified.

corrected understanding:
Financial stocks need a dedicated sector audit. Banks require asset quality, provisioning, capital adequacy, NIM, and funding checks. Securities firms require net capital, risk coverage, capital leverage, liquidity coverage, net stable funding, and trading-exposure checks. Insurers require EV/NBV, solvency, underwriting, and investment-risk checks.

impact:
V0.8 added `financial_sector_value_auditor`, a financial-sector factor framework, optional schema fields, source inventory rows, registry factors, and `scripts/run_financial_value_iteration.py`. Current financial outputs are downgraded to `proxy_pass_regulatory_data_required` rather than confirmed undervaluation.

correction_type:
`method_fix`

## 2026-06-12 - Industry-Relative Cheapness Can Be A Trap

original understanding:
Industry-relative cheapness can be treated as a direct positive component in the composite value score.

problem:
A company can be cheap inside its own industry because the market is pricing company-specific risks, such as weak cash conversion, deteriorating growth, accounting quality issues, governance risk, or poor liquidity.

corrected understanding:
Industry-relative cheapness should require confirmation from profitability, growth, cash-flow safety, shareholder-return support, peer-count adequacy, and explicit value-trap checks. Otherwise it should be penalized or removed from obvious-value candidates.

impact:
Added `relative_cheapness_confirmation_auditor` and blocked `relative_value_trap_flag=True` rows from the obvious-value current-snapshot list.

correction_type:
`method_fix`

## 2026-06-12 - Obvious-Value Count Versus Top Output Count

original understanding:
The V0.6 snapshot profile field `obvious_value_candidates` represented the count of all obvious-value candidates.

problem:
The script wrote only the Top N obvious-value rows and used that Top N length as the candidate count. The full obvious-value set was larger than the report output.

corrected understanding:
Separate full candidate count from report output count: `obvious_value_candidates` is the full set, and `top_obvious_value_output_rows` is the limited report table size.

impact:
V0.7 corrected the profile semantics before comparing obvious-value and undervalued-and-quality sets.

correction_type:
`code_fix`

## 2026-07-18 - 盘前等待不能记成迟到失败

original understanding:
只要本次刷新没有拿到可冻结的当日价格，就可以统一写成迟到或缺失。

problem:
信号日收盘前、入场日收盘前和真正错过截止是三种不同状态。盘前运行若写入不可变的 late marker，会把正常等待永久污染成证据失败。

corrected understanding:
时间门禁必须显式区分 `early_pending`、窗口内和 `late_backfill_excluded`。窗口开始前只返回等待状态，不写权威 observation 或冻结事件；超过截止后才写不可变失败标记。

impact:
V5.25、V5.33 和 V5.34 使用三态时钟，边界反例覆盖 14:59、15:30、16:01 等场景；盘前刷新不再制造不可逆失败。

correction_type:
`method_fix`

## 2026-07-18 - 当前批次不能被历史污染，也不能追认旧信号

original understanding:
只要账本哈希链有效，历史样本和当前样本可以放在同一汇总里；active 指针中的创建时间可直接供下游使用。

problem:
legacy late marker 会永久拖累新方法批次。更严重的是，`active.json` 是可变文件，回拨其中的 `created_at_utc` 可以让新 cohort 追认创建前的旧信号。

corrected understanding:
当前门禁只认经二次核验的 `(cohort_id, manifest_hash)`；global history 只作诊断。cohort 创建时点必须从追加式 history 的末条记录回填并与 active 指针比对，且创建时点不得晚于观察的 `evidence_cutoff`。

impact:
V5.25-V5.35 的当前指标全部按 active pair 隔离；V5.30 新增 `retroactive_cohort_ownership` 独立违规项。旧 4 行仍在全局历史，但新 cohort 当前观察保持 0。

correction_type:
`code_fix`

## 2026-07-18 - 哈希链需要独立头锚和可复算源证据

original understanding:
JSONL 行内前后哈希足以证明账本可靠，派生 CSV 与结算结果可由同一脚本直接信任。

problem:
合法前缀截断仍能形成一条内部有效的短链；崩溃可能发生在事件追加与 CSV 物化之间；只校验结果文件哈希不能证明候选、基准和收益计算可重复。

corrected understanding:
非空账本必须同时具备独立 head checkpoint；普通追加不得静默重建缺失锚点。CSV 只是 JSONL 的确定性物化视图，可在重试时修复。观察 bundle、价格冻结和结算都保存内容寻址源快照，并由 V5.30 独立重算。

impact:
观察、候选冻结、基准冻结及 cohort history 的 checkpoint 全部通过；合法前缀回滚、重复结算、崩溃恢复、源篡改和物化漂移均有自动化反例。完整性声明同时注明本地同文件系统边界，不宣称能抵御高权限攻击者。

correction_type:
`method_fix`

## 2026-07-18 - 旧 cohort 摘要不得冒充当前状态

record metadata:
`record_type=retrospective_inventory`; `recorded_at=2026-07-18`; `historical_timestamp_claimed=false`; `post_hoc=true`。本组纠错记录形成于整改日，只纠正当前解释和治理规则，不补造历史时点。

original understanding:
一次整改报告中的 active cohort 和测试数字，可以在后续批次建立后继续充当“当前状态”。

problem:
资金流证据批次已经由 v2 依次推进到 v3、v4、v5。旧 `run_summary.json` 是生成当时的不可变快照；若继续把 v2 摘要称为 current，会同时制造多个“当前批次”，破坏恢复基线。

corrected understanding:
当前状态的取证优先级固定为：追加式 history 与独立 checkpoint、经实时核验的 active pointer、同一 `(cohort_id, manifest_hash)` 的最新生成摘要，最后才是叙述性文档。旧摘要只作历史证据，不被回写，也不覆盖当前指针。

impact:
当前唯一有效批次为 `ff_integrity_v5_20260718`，manifest 为 `531cf927cd18cc3c774777098ba20794b7f78c1f8dbfe67a49397d8a6f17954c`；v2-v4 均保留，但不得再用于 current 判定。

correction_type:
`state_source_fix`

## 2026-07-18 - verified 与 invalidated 必须互斥

original understanding:
cohort 重新核验通过后，可以保留先前的 `invalidated_at_utc` 和 `invalidation_reason` 作为说明，同时再写 `verified_at_utc`。

problem:
同一 active pointer 同时声称“已验证”和“已失效”，下游无法确定该批次是否可用；这种矛盾状态会让失败关闭语义失效。

corrected understanding:
失效状态与有效状态必须互斥。独立第二次核验成功时，指针写入 `verified_at_utc`、清除 `verification_required`，并移除 `invalidated_at_utc` 与 `invalidation_reason`；核验失败时则不得声明 verified。

impact:
当前 v5 active pointer 已通过第二次核验且不含失效字段；一致性审计据此验证 active pair，而不是从叙述文字猜测状态。

correction_type:
`state_machine_fix`

## 2026-07-18 - V5.21 必须先于最终 V5.10，刷新链为十一项

original understanding:
可以把完成度审计 V5.10 放在 PIT 新源发现 V5.21 之前运行，或继续把当前刷新过程称为十步链。

problem:
V5.10 汇总 V5.21 及后续证据。提前生成 V5.10 会让最终完成度读取旧输入；“十步”也与当前实际刷新任务数不符。

corrected understanding:
完整刷新链按十一项输入刷新记录；V5.21 必须先生成，V5.10 只在所有依赖与最终 V5.07 结果完成后重建一次，作为链尾完成度审计。

impact:
当前 runner 与完成度审计使用同一十一项依赖契约，状态一致性检查显式验证 V5.21 早于 V5.10。

correction_type:
`dependency_order_fix`

## 2026-07-18 - task brief 审计必须覆盖 expected set

original understanding:
扫描当前目录中已经存在的 task brief，并确认这些文件格式正确，就足以证明研究治理覆盖完整。

problem:
只审计“已经存在的文件”会产生空集通过：缺失的版本不会进入扫描，自然也不会报错。格式正确不等于 V4.72-V5.35 与当前主线都受治理。

corrected understanding:
审计的 expected set 由 65 条版本库存给出，并与递归发现的 task brief 做集合对账。每个影响结论的版本都必须有 brief、事前注册或明确 post-hoc 声明、changelog 记录和标准输出 manifest；缺失、重复、伪造事前注册、晚登记或 cohort 不一致均失败关闭。

impact:
治理结论只能来自 expected-versus-observed 覆盖审计；单个 task brief 的格式通过不再被解释为全版本覆盖通过。

correction_type:
`governance_coverage_fix`

## 2026-07-18 - behavior、self-check 与 contract 不得混称

original understanding:
behavior tests、脚本 self-check 和 contract checks 都可以统称为“测试通过”，数字也可以直接相加。

problem:
三者验证对象不同。behavior tests 是可执行行为用例，self-check 是脚本内置的确定性小样本检查，contract 是完成度审计中的接口与产物约束；混称会重复计数，并夸大验证覆盖。

corrected understanding:
三层必须分别报告。当前本地产物中的 23/23 behavior tests、12/12 self-check 与 contract 分类各自保留名称、分母和来源，不相加，不用其中一层替代另一层，也不把 readiness 门禁混入工程测试。

impact:
完成度摘要分别保存 behavior、self-check、implementation 与 readiness 字段；报告引用时必须带验证层名称。

correction_type:
`evidence_label_fix`

## 2026-07-18 - ignored output 不作为可点击文档链接

original understanding:
只要本机 `outputs/` 下存在报告，就可以在 README 或历史文档中用 Markdown 链接直接指向它。

problem:
`outputs/` 属于忽略目录。链接在当前机器上可能可点，但在干净 clone、bundle 恢复或他人环境中没有目标文件，形成不可移植的死链接。

corrected understanding:
可点击文档链接只指向受版本控制的文件。忽略输出使用行内代码路径，并同时给出重建命令或其生产脚本；链接审计应把 missing、绝对本地路径和未跟踪目标视为失败。

impact:
README 与研究历史可以在干净环境中导航；本地生成证据仍保留明确路径，但不伪装成仓库交付物。

correction_type:
`documentation_portability_fix`

## 2026-07-18 - 对外统一为六角色确定性否决链

original understanding:
可以把历史文档中的 Agent 数、内部任务数、验证层数和当前决策角色数混用，沿用“九 Agent”或其他口径。

problem:
这些数字描述的对象不同。混用会让读者误以为决策链角色发生了变化，也会把工程编排数量误写成投资治理机制。

corrected understanding:
当前对外术语统一为“六角色确定性否决链”。策略版本、审计版本、数据治理版本和 forward cohort 分别命名；内部任务数与测试数只在其各自语境中出现。

impact:
当前 runner、完成度审计和面向用户的状态文档采用同一角色口径；历史原文若保留，必须明确其历史语境，不再用于 current 描述。

correction_type:
`terminology_fix`

## 2026-07-18 - 交易日期与保守 lag 不能证明估值可得

original understanding:
历史估值表有 `trade_date`，再顺延一个自然日作为 `available_date`，可以视作保守 PIT 处理；2022 年以来的结果可以继续称 OOS。

problem:
交易日期不是发布时间。自然日顺延无法证明数据何时被投资者看见，也不能处理盘后发布、周末、节假日、版本修订和抓取时间。现有历史还混入 131 行由当前 V2.5 组件回收的 2026-06-12 快照；同时缺少带生效区间的历史行业分类，2015、2022、2023 横截面不能直接视为同一宇宙。

corrected understanding:
历史估值必须提供 `published_at/available_date/fetched_at/source_version/revision_status`，并由冻结 A 股交易日历验证。缺任一证据即 `unavailable_for_promotion`。回收快照永久隔离；2022 年以来旧结果改标为 `historical_review_used_in_iteration`。只有 2026-07-12 起的事前登记真实前推或此前未使用的新数据可以申请晋级。

impact:
V2.6、V5.11、V5.12 和两条旧估值旁路均已失败关闭或屏蔽估值字段；V5.20、V5.10 与当前 runner 共用方法门。当前动作仍为 `NO_ACTION`。

correction_type:
`pit_availability_and_universe_fix`

## 2026-07-18 - 方法审计通过不等于历史证据晋级通过

original understanding:
PIT 与行业历史方法审计本身通过后，可以把旧历史结果恢复为可晋级证据。

problem:
方法审计通过只说明缺失证据会被识别并失败关闭。当前 `published_at/available_date/source_version/revision_status` 和官方历史分类成员表仍缺失，可晋级估值行仍为 0。

corrected understanding:
必须并列报告 `audit_passed=true` 与 `promotion_gate_passed=false`。前者是控制有效，后者才是证据资格；二者不能互相替代。

impact:
CURRENT_STATUS、V5.10 和当前 runner 统一保留 `research_only / NO_ACTION`，直到外部证据按恢复条件补齐并重新通过同一门禁。

correction_type:
`method_gate_evidence_boundary_fix`

## 2026-07-18 - 名称变化不等于代码语义复用

original understanding:
166 个观察身份段可以概括成 35 个“复用代码”，并据此默认所有滚动指标已经完成身份隔离。

problem:
35 个代码只证明名称或观察口径发生变化。现有证据能够确认语义复用的只有 `801951`、`801952`；把其余 33 个代码一并称为复用会扩大结论。历史 beta 也没有按可信身份 episode 重算，继续展示会把旧名称序列串接风险隐藏在指标中。

corrected understanding:
分开报告 166 个观察名称段、35 个名称或口径变化代码，以及 2 个已确认语义复用代码。`beta_low_pb_score` 在身份安全重算完成前从三种宇宙只读指标中排除；`801156` 的普通陈旧也与 7 个长尾缺口分栏。

impact:
方法审计现输出 15 行估值稳健性指标，不再展示旧 beta 组合结果；CURRENT_STATUS 和专项报告采用同一术语。

correction_type:
`industry_identity_scope_fix`

## 2026-07-18 - 摘要自报完整性不能替代现场复验

original understanding:
只要 V5.07 或方法审计摘要自报 `integrity=true`、给出 64 位哈希和通过布尔值，消费端即可把它当成已验证路线。

problem:
V5.07 当前仍从可变 CSV 物化，没有可重放的追加式账本；历史方法摘要也没有独立、不可变的晋级凭证。手改 JSON 可以成套伪造这些字段，摘要内部自洽不等于证据真实。

corrected understanding:
消费端只接受自身能够现场复验的原始证据。当前前推账本复验器和历史 PIT/分类晋级凭证复验器均未落地，两条路线一律硬阻断；调用方直接传入 `true` 也不能解锁。

impact:
V5.10、当前 runner、状态一致性审计和 CURRENT_STATUS 共用同一失败关闭入口。当前 `promotion_gate_passed=false`、前推路线 `false`、动作 `NO_ACTION`。

correction_type:
`evidence_receipt_verification_fix`

## 2026-07-21 - 探索记录结算行情必须与主线缓存隔离

original understanding:
`801156` 的历史缺口可以概括为上游临时缺数，四条旧观察的退出日行情也可以直接刷新进共享行业历史缓存。

problem:
上游响应实际含有非标准 `NaN`；严格解析后，`801156` 返回序列仍与既有追加式历史存在日期连续性和成交量口径冲突。早期刷新还把 2026-07-21 行情写入了主线缓存，破坏了 `industry_history=2026-07-15` 的决策快照边界。随后虽恢复主线日期边界，CSV 重序列化使聚合哈希从 `9ebc…` 变为 `ae35…`，不能声称字节级还原。

corrected understanding:
正式结算使用独立的 `settlement_2026_07_21` 缓存；bootstrap 与 refresh 共用一个连续的双缓存锁域。`801156` 固定隔离，其 bootstrap、刷新提交、主线现场文件和结算现场文件 SHA-256 必须一致。原始响应回退只接受严格 ISO 日期、有限数值与默认 TLS 校验。主线按 2026-07-15 语义边界保留，最终刷新前后聚合均为 `ae35…`，不再写入主线。

impact:
结算专用缓存 131 个文件中，130 个正常刷新、1 个隔离；2026-06-23 与 2026-07-21 的同一行业交集为 123，四个目标 4/4。正式清单逐项封存主线 131 个文件与结算缓存 131 个文件，runner 将两者同时纳入不可变范围。四条记录仍全部 `blocked_terminal_late_freeze_excluded`，收益字段为空，结论保持 `research_only / NO_ACTION`。

correction_type:
`method_fix / code_fix`
