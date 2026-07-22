<!--
record_type: research_governance_remediation_report
recorded_at: 2026-07-18
historical_timestamp_claimed: false
strategy_change: false
-->

# 研究状态与治理整改报告

## 执行结论

目标 4A—4D 及目标 5 已完成工程整改和现场复核。项目现在只有一个当前状态入口：[CURRENT_STATUS](../CURRENT_STATUS.md)。截至决策日 `2026-07-18`，唯一可发布结论仍是：

> `research_only / NO_ACTION`；强行业 Alpha 未验证；人工辅助交易未就绪；自动交易禁止。

本次整改修复了状态来源、刷新依赖、版本库存、task brief 覆盖、文档入口、恢复边界，以及 PIT 估值与行业历史口径；没有修改策略阈值、TopN、晋级门槛或任何投资结论。

## 整改范围与结果

| 目标 | 原问题 | 已完成处理 | 当前结果 |
|---|---|---|---|
| 4A 权威状态与刷新链 | cohort 摘要跨代、active 元数据互斥、V5.10 可能读取旧 V5.21 | 建立 v7 活动 cohort；覆盖 V5.25—V5.35 全部摘要；完整链和当前链均先重建 V5.21，再运行唯一最终 V5.10 | 状态一致性 34/34，通过 |
| 4B 研究版本库存 | V4.72—V5.35 缺少统一机器库存，配置、登记、产物和 Git 状态无法逐项核对 | 建立 64 个历史版本加 `CURRENT_MAINLINE` 的有序库存；加入源码/配置 SHA、精确 changelog anchor、输出 manifest、角色关系和可恢复性 | expected=actual=65，治理缺口 0 |
| 4C task brief 与治理覆盖 | 旧审计只检查已有 brief，缺版本可空集通过 | 补建 64 份 retrospective brief 和 1 份 current brief；覆盖审计按精确 expected set 失败关闭，并现场重算输出 SHA | 181 份 brief 0 error/0 warning；治理 65/65 |
| 4D 当前入口与日志 | README 混放数十个历史版本；三份日志停更；本地 ignored output 被当作可移植链接 | 生成 CURRENT_STATUS；README 收束为五节；旧正文迁入历史档案；三份日志只追加；建立 tracked Markdown 链接审计 | 当前入口有效，历史迁移可反向核验 |

## 当前状态证据

- active cohort：`ff_integrity_v7_20260718`
- manifest hash：`966e40a07d2248d8447692e85faf3d28d4ffaee51b2db8b0a787a861db0bf7e2`
- 当前 runner：`CURRENT_MAINLINE / 1.0.0`
- 当前动作：`NO_ACTION`
- 主线阻断门：8 个
- 强行业冻结规则合格前推样本：0
- active fund-flow 样本：0
- 探索性 fund-flow 观察：4
- 迟到回填排除：4
- 探索观察计划退出日：`2026-07-21`；本次未提前结算

权威源、冲突和恢复条件详见 [当前状态一致性整改报告](./current_state_consistency_report.md)。数据源的真实截止日与决策 as-of 已在 CURRENT_STATUS 分栏展示，运行日期不再冒充数据日期。

## 版本库存与登记边界

机器库存固定为：

- `V4.72—V4.99`：28 条；
- `V5.00—V5.35`：36 条；
- `CURRENT_MAINLINE`：1 条。

每条记录显式保存 sequence、implementation version、version class、objective、源码与配置指纹、configuration mode、brief、实验登记、非二值 post-hoc 状态、精确 changelog 锚点、输出 manifest、Git 可恢复性、mainline role、cohort 和缺失要求。

登记边界保持保守：V5.04 的两条规则是唯一精确的 `preregistered_forward_only`；V5.05—V5.10 仅继承这两条冻结规则；其他历史项按 `post_hoc_historical_inventory`、`retrospective_governance` 或 `not_an_experiment` 分开记录。2026-07-18 补建的 brief 统一声明真实补录日期和 `historical_timestamp_claimed=false`，不追认历史预注册。

库存标准四件套位于 `outputs/audit/research_version_inventory/`，治理覆盖标准四件套位于 `outputs/audit/research_governance_coverage/`。两者均为可重建的 ignored output；仓库内永久交付的是 [JSON 库存](../logs/research_version_inventory.json)、[CSV 库存](../logs/research_version_inventory.csv)、生成器、brief 和测试。

## 文档迁移与日志

原 README 的 366 行历史正文按 6 个连续区段迁入 [研究历史索引](../docs/research_history/README.md)。迁移清单保存源 commit、原始行段、原文 SHA256、目标文件 SHA256 和 62 处 ignored-output 链接规范化；反向替换核验能够还原原文。

[版本变更日志](../logs/version_changelog.md)、[研究日志](../logs/research_log.md)和[纠错日志](../logs/review_correction_log.md)均采用追加方式更新。历史补录带有 `record_type=retrospective_inventory`、实际记录日和 post-hoc 边界；旧日志没有被回写或截断。

## 验证矩阵

| 验证层 | 现场结果 | 能证明什么 |
|---|---:|---|
| Python 默认离线测试 | 222 passed，1 deselected | 当前代码行为、生成器与反例门禁未回退 |
| bundle 干净恢复测试 | 215 passed，7 skipped，1 deselected | 受版本控制内容可恢复；7 项依赖 ignored 本地证据或输出的集成检查显式跳过，不冒充通过 |
| 独立行为测试 | 23/23 | 当前主线关键失败路径与边界可执行 |
| 脚本 self-check 回归 | 12/12 | 12 个既有脚本的内置确定性检查通过 |
| 工程合同检查 | 29/29 | 主线接口、产物与治理合同存在 |
| 建议就绪门禁 | 6/13 | 仍未就绪；不能生成有效买卖建议 |
| 状态一致性 | 34/34 | 11 个 cohort-aware 摘要、方法门、依赖顺序和边界一致 |
| Inventory | 65/65 | 精确版本库存完整、可恢复性门禁通过 |
| Governance coverage | 65/65 | brief、登记/post-hoc、changelog、manifest 与 cohort 齐备 |
| Task brief | 181，0 error/0 warning | 全部现存 brief schema 与治理字段通过 |
| Node 数据合同 | 6/6 | Dashboard 数据合同拒绝未知 schema 和自动执行标记 |
| Dashboard 构建 | 924 modules | TypeScript 与 Vite 生产构建成功 |
| Preview | HTTP 200，端口 4175 | 本地生产预览可访问 |
| Python 依赖 | `pip check` 通过 | 当前环境无已知破损依赖 |
| 锁文件 | `uv lock --check` 通过 | 锁文件与项目声明一致 |

这些数字属于不同验证层，不能相加。工程、测试和治理通过均不证明策略具有 Alpha，也不解除账户、风险或研究门禁。

## 标准审计入口

以下目录均严格保留 `report.md`、`run_summary.json`、`top_candidates.csv`、`debug/` 四项顶层结构：

- `outputs/audit/current_state_consistency/`
- `outputs/audit/research_version_inventory/`
- `outputs/audit/research_governance_coverage/`
- `outputs/audit/current_status/`
- `outputs/audit/pit_universe_methodology_remediation/`
- `outputs/audit/markdown_link_audit/`

这些目录被 Git 忽略，必须由版本化命令重建；它们不属于 Git bundle 的数据恢复承诺。

## 仍未完成的研究问题

以下事项没有被本轮治理整改“顺便完成”：

- V4.71 择时稳健性仍未通过；
- 稳定强行业 Alpha 仍未验证；
- 历史估值与行业宇宙的方法控制已经完成专项整改，但真实 `published_at/available_date` 和官方历史分类成员表仍缺失，晋级门继续失败关闭；
- 真实账户和建议后组合风险门禁仍未通过；
- Dashboard 的时点表达与移动端信息完整性仍属于后续目标；
- 4 条探索观察只能在 `2026-07-21` 到期且数据完整后按既定边界结算，结果也不得自动转成合格前推证据。

因此，本报告的完成含义是“状态与治理可以审计、可以失败关闭、可以恢复”，不代表研究目标完成，更不构成投资建议。

## 2026-07-18 补充：PIT 与行业历史方法门

目标 5 已把原先列为后续事项的方法控制接入主链。详细证据见 [PIT 估值与行业历史口径整改报告](./pit_universe_methodology_report.md)。

- 估值合同改为真实发布时间、抓取时间、内容哈希固定的交易日历、不可变原始文件现场哈希和修订链；自然日 lag 被废止。
- 2026-06-12 的 131 行回收快照不再充当官方历史，直接来源截止为 2025-12-31。
- 2015、2022、2023 宇宙断点、166 个观察名称段、35 个名称或口径变化代码、2 个已确认语义复用代码和长尾缺口已进入机器审计；131 文件覆盖与 120 文件新鲜度分开判断，`801156` 的普通陈旧另列。
- V5.20、V5.10 和当前 runner 已接入方法门。方法控制审计通过，历史证据晋级门不通过，可晋级估值行 0。
- 历史 beta 指标在身份 episode 重算完成前已排除；V5.07 尚无可现场复算的追加式账本，任何摘要自报的 `integrity=true` 都不能解除前推门。
- 最小当前刷新链扩为 16 项，执行顺序固定为方法审计早于 V5.11、V5.12、V5.20 和最终 V5.10。

本项“完成”指失败关闭机制和审计解释已落地。外部数据缺口没有被补造；当前结论仍是 `research_only / NO_ACTION`。
