<!--
record_type: retrospective_inventory
recorded_on: 2026-07-18
historical_timestamp_claimed: false
source_commit: 36cc42926a72d488116417e48e6107b544754d93
-->

# V5.32—V5.35 追溯库存

这份文档是在 2026-07-18 补建的 `retrospective_inventory`，不是这些版本运行前形成的 task brief 或实验预注册，也不把补录日期伪装成历史研究时间。它只登记本地现存运行产物所表达的目标、状态和证据边界。

## 证据状态

四个 `run_summary.json` 都位于被 `.gitignore` 排除的 `outputs/`，不属于 `source_commit`。下表保留首次归档读取时的旧运行快照，并以 SHA256 固定来源；它们是当时的运行库存，不是仓库提交内的永久事实，也不是 2026-07-18 状态重建后的当前摘要。

首次归档时，V5.32 与 V5.35 的摘要仍绑定 `ff_integrity_v2_20260718` 和清单哈希 `08c54873a134d999555c07abab0a1dc14f4e52c5b7c338dc9bee2d342275a2a6`，而活动指针当时已经进入 `ff_integrity_v4_20260718`。这组不一致正是它们只能作为历史运行库存、不能作为当前状态来源的原因。

归档完成后，状态重建又建立并双次验证了 `ff_integrity_v5_20260718`，manifest hash 为 `531cf927cd18cc3c774777098ba20794b7f78c1f8dbfe67a49397d8a6f17954c`。V5.32—V5.35 已按 `as_of_date=2026-07-18` 重建并显式绑定该 pair；当前状态应读取根目录 `CURRENT_STATUS.md` 和本轮机器摘要，不能回读下表中的 v2 旧快照。

## 版本库存

| 版本 | 目标 | 现存运行快照 | 结论边界 |
|---|---|---|---|
| V5.32 / `5.32.1` | 确认资金流前推样本是否进入持有观察期 | `observation_rows=0`，持有中 0，到期待结算 0，`fail_count=1`，`pending_count=2`，`best_status=research_only_no_active_holding_observation` | 没有已结算 forward return；`goal_ready=false`，不能声称找到强反弹行业 |
| V5.33 / `5.33.2` | 冻结候选行业在计划入场日的精确指数点位 | 冻结行数 0，已冻结入场价 0；全局历史 4 行均为 late backfill excluded；`fail_count=3`，`pending_count=1` | 入场价格冻结不完整，补录样本永久不得晋级；冻结入场价也不等于 Alpha 验证 |
| V5.34 / `5.34.2` | 冻结同批次全行业等权基准的入场点 | 批次数 0，基准冻结行数 0；全局历史 4 行均为 late backfill excluded；`fail_count=4`，`pending_count=1` | 基准入场点缺失；基准冻结本身也不等于强行业能力验证 |
| V5.35 / `5.35.1` | 汇总等待期观察、冻结覆盖和下一动作 | 观察样本 0，持有中 0，到期待结算 0；readiness 为 6 ready、2 blocked、2 pending；`current_tradeable=false` | 等待室不结算未来收益，不证明强行业 Alpha；本行保留的是绑定 v2 的旧库存快照 |

四个快照共同保持：`policy_status=research_only`、`goal_ready=false`、`can_claim_strong_rebound_industries=false`、`production_ready=false`、`auto_execution_allowed=false`。

## 来源指纹

| 版本/指针 | 来源路径 | SHA256 |
|---|---|---|
| V5.32 | `outputs/audit/fund_flow_holding_observation_v5_32/run_summary.json` | `c83756795c0db87a71073b593395d780db134e501f3a16200c48b0a07ffcc799` |
| V5.33 | `outputs/audit/fund_flow_entry_price_freeze_v5_33/run_summary.json` | `ae421d1d825d00ec96160055aef6d3ada8dfc0b019c3d94a83ff7dff74dd2a39` |
| V5.34 | `outputs/audit/fund_flow_benchmark_entry_freeze_v5_34/run_summary.json` | `cdc9d4b2d09d845ec7569ff8d229a80e7ea1768ea02dcbdb6f0171330556035a` |
| V5.35 | `outputs/audit/fund_flow_waiting_room_v5_35/run_summary.json` | `43f2c1c8e6a29a5a4bbc98967d3162fe6f29060a04b82fda2a97a2ce1eba2dcb` |
| 活动 cohort 指针 | `logs/v5_31_fund_flow_evidence_freeze_active.json` | `7c15b66966fa4600432df64db8aa6489dff5f3355e09d68dfe11c50513ae3c34` |

## 治理判断

- 记录类型：追溯库存，`post_hoc=true`；不能机器误判为 preregistration。
- 当前效力：表内四个旧摘要因 cohort 不一致而陈旧；状态重建后的四个摘要已绑定活动 v5 cohort，但仍须通过当前状态一致性审计后，才可进入 `CURRENT_STATUS.md`。
- 研究边界：0 个活动观察、0 个已结算前推样本；稳定强行业 Alpha 未验证。
- 交易边界：人工辅助交易尚未就绪，自动交易禁止。
