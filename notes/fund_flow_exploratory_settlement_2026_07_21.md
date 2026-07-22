# 四条探索性资金流记录终局说明（2026-07-21）

## 终局结论

本次只处置 2026-06-22 形成的四条 `exploratory_fund_flow_only` 旧观察。正式结果为 settled / terminal blocked / pending / qualified settled = **0 / 4 / 0 / 0**，当前动作保持 **`NO_ACTION`**。

| 行业 | 独立终局 |
|---|---|
| 801194 保险Ⅱ | `blocked_terminal_late_freeze_excluded` |
| 801125 白酒Ⅱ | `blocked_terminal_late_freeze_excluded` |
| 801764 游戏Ⅱ | `blocked_terminal_late_freeze_excluded` |
| 801203 一般零售 | `blocked_terminal_late_freeze_excluded` |

四条记录的 `actual_entry_date`、`actual_exit_date`、`realized_return`、`benchmark_return`、`realized_relative_return`、`future_return_rank_pct`、`future_top_quintile` 全部为空。没有收益结论，没有样本晋级。

原账本未改写，四行 `settlement_status` 仍为 `not_due`。该字段记录旧账本状态；本次终局以独立产物中的 `disposition_status` 为准。二者并列保留，不能用终局处置反写历史账本。

## 结算专用行情口径

结算专用缓存共有 131 个行业文件：130 个正常刷新，`801156` 固定隔离。精确日期覆盖为：

- 入场日 `2026-06-23`：123 个行业；
- 退出日 `2026-07-21`：123 个行业；
- 两日同一行业交集：123；
- 四个目标行业：4/4。

123 只说明两个结算日期的事后行情可用。四条记录在入场时没有形成按时冻结的基准宇宙，历史 `benchmark_universe_count=0`；事后补齐行情不能修复这项证据缺口，也不能生成收益。

`801156` 的隔离原因码为 `provider_history_incompatible_with_append_only_cache`。上游响应含非标准 `NaN`，严格解析后，原始序列与既有追加式历史仍存在连续性和口径冲突，因此保留原文件并排除出精确日期覆盖。该文件在主线缓存与专用缓存中均为字节一致副本：

`f84fea1c417b3487fe7b5c7bf1c8e90fd8c6257733f1b20ce8575a5fb7a3f23d`

## V5.29 计数说明

V5.29 的 `pending_count=4` 指四个晋级指标尚无可用数据：`mean_relative_return`、`median_relative_return`、`positive_batch_rate`、`top_quintile_hit_rate`。它不表示四条探索记录仍待处置；探索记录的 `exploratory_pending_count=0`，四条均已终局排除。

## 共享主线缓存修正披露

早期刷新尝试曾写入共享主线缓存。随后已恢复到主线 `industry_history=2026-07-15` 的语义边界。恢复过程发生 CSV 重序列化，主线聚合哈希由原 `9ebc…` 变为：

`ae35da0c892fecdd0afba1ed71aeef2c8cd8e74749f987e30db65907c0b7799b`

因此，这次恢复不主张字节级还原。历史数值等价由此前的 append-only 连续性校验支持；最终正式刷新又核验 408,744 条既有有效行全部未变。bootstrap 与 refresh 在同一个双缓存锁域内连续完成，最终刷新全程只写结算专用缓存，主线聚合在刷新前后均为 `ae35…`，保持不变。

结算专用缓存最终聚合为：

`bedff91421395fcaa05185082dac3edc245a75869b745b51e2f6cc1845151a46`

## 正式证据与复核

active cohort 为 `ff_integrity_v8_20260721`，manifest 为 `facaa1a541c8160ca1039bc0649eb658ad4de48ee4f85b4de9a8dbb1aa68d360`。

| 证据 | SHA-256 |
|---|---|
| 行情刷新摘要 | `61b21b53a36b2761b4c4224e872833644069462a89568007eb1d67e6f495a5ff` |
| 正式 `run_summary.json` | `3a5ac038543ad736eeb3630ab4e1ab4a4bce8cdb1a3ee21d47ac3cb9c2943c00` |
| 正式 `report.md` | `a2e6e5e44adb3fdbf3c2e73d45a1762e4e8bd6b1d54d1430e1225d50c19b8564` |
| `settlement_dispositions.csv` / `top_candidates.csv` | `c292cd5edf079cd38200840b869b1c71ba4c7e2ff67e8b3d202412f695d27e14` |
| `command_results.json` | `4a15d8bb0d3754b726cf1d2eef4334ce019af2635c64ed1f31b1e52fdbb30ddc` |
| `formal_commit.json` | `81c623fc383ad82a5264d3f858c64a8a931d8cb7b27599dba2fa60117f072232` |
| `sha256_manifest.csv` | `cf7fc99682d6adc238be4e4daeeeafe15174c2e1ebb06de9ec0c7c1b4ec9cab1` |

清单复算为 **284/284**，其中主线行情 131 项、结算专用行情 131 项；无缺失、无哈希或字节数不一致。正式编排记录 **14/14** 个命令均符合预期退出码并通过语义校验；V5.30 的退出码 2 属于预期的失败关闭结果，不代表命令链失败。Python 全量回归为 **479 passed、1 deselected**，资金流结算定向回归为 **193 passed**。

## 纠错记录

- 类型：`method_fix / code_fix`
- 原理解：结算行情刷新可以直接复用共享主线缓存。
- 问题：早期尝试把退出日数据写入主线缓存，混淆了 2026-07-18 决策快照与 2026-07-21 结算证据；恢复时又因 CSV 重序列化改变了字节聚合。
- 修正：主线保持 2026-07-15 语义边界；四条旧观察改用独立的结算专用缓存，固定隔离 `801156`，终局只写独立 disposition。
- 影响：主线 byte hash 从 `9ebc…` 变为 `ae35…`，历史数值连续性校验通过；正式刷新期间主线哈希不再变化。四条记录仍为 0 settled、4 terminal blocked、0 pending、0 qualified settled，结论保持 `research_only / NO_ACTION`。

本项目不构成投资建议，也不授权自动交易。
