# 研究历史索引

这里保存根 `README.md` 在项目状态重建前承载的历史研究正文。归档只用于追溯，不是当前策略结论、当前运行状态或生产就绪声明；当前状态应以根目录 `CURRENT_STATUS.md` 和当次机器输出为准。

## README 原文迁移

| 原 README 行段 | 归档文件 | 内容 |
|---|---|---|
| 1—85 | [V4.70—V4.71 研究与实盘前审计](./v4_70_v4_71_research_and_live_audit.md) | 旧主线摘要、V4.70 历史评价和 V4.71 实盘前复核 |
| 86—188 | [V4.72—V5.20 强行业研究](./v4_72_v5_20_strong_industry_research.md) | 强行业选择研究、失败审计、前推规则与证据边界 |
| 189—239 | [V4.71 前推样本旧操作手册](./legacy_v4_71_forward_runbook.md) | 预登记、入场、退出、跳过和账本审计命令 |
| 240—288 | [V4.70—V4.88 旧命令矩阵](./legacy_v4_70_v4_88_command_matrix.md) | 逐版本运行与输出结构审计命令 |
| 289—323 | [仓库布局与历史研究边界](../repository_layout.md) | 文件放置、标准输出四件套和旧研究边界 |
| 324—366 | [V5.21—V5.31 资金流证据链](./v5_21_v5_31_fund_flow_evidence_chain.md) | 新 PIT 数据源、前推观察、完整性与冻结指纹 |

六个迁移区间首尾相接，完整覆盖源 README 的 366 行。原文哈希、目标文件和 62 次 ignored-output 链接规范化记录在 [JSON 迁移清单](../research_history_migration_manifest.json)；便于表格审阅的摘要见 [CSV 迁移清单](../research_history_migration_manifest.csv)。

## 补录库存

[V5.32—V5.35 追溯库存](./v5_32_v5_35_retrospective_inventory.md)不是 README 原文迁移，而是在 2026-07-18 根据本地运行产物补建的 `retrospective_inventory`。它明确保留旧摘要与活动 cohort 的不一致，不把旧摘要冒充当前状态，也不构成预注册。

## 阅读边界

- `V4.70` 的 100 分是特定历史评价框架内的结果，不是当前生产就绪评分。
- `V4.71 production_ready=false` 是实盘前审计结论，不应与 V4.70 的历史评分并列成两个“当前结论”。
- V4.72 以后仍未验证稳定强行业 Alpha；归档中的候选、观察和前推规则均不构成交易指令。
- 全部归档默认 `research_only`；人工辅助交易尚未就绪，自动交易禁止。
