<!--
record_type: current_state_governance_report
recorded_at: 2026-07-18
historical_timestamp_claimed: false
remediation_base_commit: 36cc42926a72d488116417e48e6107b544754d93
-->

# 当前状态一致性整改报告

## 结论

截至决策日 `2026-07-18`，当前状态源已经对齐到同一活动证据批次：`ff_integrity_v5_20260718`，manifest hash 为 `531cf927cd18cc3c774777098ba20794b7f78c1f8dbfe67a49397d8a6f17954c`。状态一致性审计实跑 30/30 项通过；当前结论仍是 `research_only / NO_ACTION`，强行业 Alpha 未验证，人工辅助交易未就绪，自动交易禁止。

这次整改只处理状态来源、依赖顺序和治理语义，没有修改策略阈值、TopN、晋级门槛或研究结论。

## 权威来源顺序

当前状态按以下优先级取证：

1. 追加式 cohort history 与独立 checkpoint；
2. `validated_active_cohort()` 对活动指针、manifest 和历史链的现场复验；
3. 显式绑定同一 `(cohort_id, manifest_hash)`、且生成时间不早于 cohort 建立时间的运行摘要；
4. [CURRENT_STATUS](../CURRENT_STATUS.md) 等叙述性文档。

叙述文档不能覆盖机器状态。旧摘要即使文件仍在，也只能保留为历史快照。

## 发现的冲突与修复

| 冲突 | 原风险 | 本次处理 |
|---|---|---|
| v2、v3、v4 摘要与活动指针并存 | 多个批次都可能被误称为 current | 旧批次和历史链完整保留；当前唯一活动 pair 升级并双次验证为 v5 |
| active pointer 曾同时保留 verified 与 invalidated 语义 | 下游无法判断证据是否可用 | 验证成功时清除 `invalidated_at_utc`、`invalidation_reason` 和待验证状态；失败时禁止声明 verified |
| V5.25、V5.33、V5.34 空摘要不声明活动 pair | “零样本”输出无法证明属于哪个 cohort | 空结果也强制写入活动 cohort id、manifest hash 和复验状态 |
| 完整刷新中 V5.10 可能早于最终 V5.21 | 目标审计读取上一轮新数据源状态 | [完整刷新入口](../scripts/run_v4_71_live_refresh.py)固定为 V5.21 先生成、唯一最终 V5.10 后生成 |
| 当前短刷新曾只在 V5.07 后重建 V5.10 | `--refresh-inputs` 仍可能读取旧 V5.21 | [当前 runner](../scripts/run_etf_assisted_trading_current.py)执行十一项输入刷新，其中 V5.21 在本轮重建，随后才运行最终 V5.10 |
| 运行日期、决策日和数据截止日混写 | 旧数据可能被包装成 2026-07-18 数据 | 当前快照分栏记录 generated_at、decision as-of 和每个真实数据源 cutoff |

## 当前机器快照

- 决策 as-of：`2026-07-18`
- 当前动作：`NO_ACTION`
- runner 主阻断门：7 个
- 活动 cohort：`ff_integrity_v5_20260718`
- 强行业冻结规则合格前推样本：0
- active fund-flow observation：0
- 历史探索性 fund-flow observation：4
- 迟到回填排除观察：4
- 计划退出日：`2026-07-21`；本次没有提前读取或结算未来收益

V5.25—V5.35 共 11 个 cohort-aware 摘要均纳入审计。每个摘要同时检查 pair 一致性和生成时间；缺文件、旧 pair、错误 hash 或早于活动 cohort 的摘要都会失败关闭。

## 离线反例覆盖

新增或保留的反例包括：

- v2/v3 或任意 superseded pair 冒充 current；
- 同 pair 但摘要早于活动 cohort，属于 stale；
- active pointer 同时含 verified 与 invalidated；
- 缺失 checkpoint 导致活动 cohort 无法复验；
- 摘要 manifest hash 错误；
- V5.10 早于 V5.21，或当前短刷新不重建 V5.21；
- 任一异常试图把 `auto_execution_allowed` 改为 true。

上述异常均不能产生 BUY、人工辅助就绪或自动执行结论；审计摘要固定保持 `production_ready=false`、`auto_execution_allowed=false`。

## 产物与指纹

标准四件套位于 `outputs/audit/current_state_consistency/`，该目录属于可重建的 ignored output，不作为仓库内可点击链接。重建命令：

```powershell
python scripts/audit_current_state_consistency.py
```

本次报告固定的关键文件 SHA256：

| 文件 | SHA256 |
|---|---|
| `scripts/audit_current_state_consistency.py` | `956b08f8ef3f6c09b375325818777f4bcc69b214418b0750c2416edb751e3ec5` |
| `scripts/run_etf_assisted_trading_current.py` | `386771460057e81ea552ef0e297ca84165e30f1f01c7df70f61cdf03df751cf0` |
| `scripts/run_v4_71_live_refresh.py` | `512ca335d90b0b41d79b935e6537e3be1e5354fd9f08f5fd30b9b39a6fc9c5cf` |
| `logs/v5_31_fund_flow_evidence_freeze_active.json` | `d55dc5b2c82fa4b90698c070dc0b15b866300315ec375ac35a1fd144d58e7e76` |
| `logs/v5_31_fund_flow_evidence_freeze_history.jsonl` | `52dc067a3ec3f4bfc3adabbac4b392e958ec8d590d40a7b9dc2a18732ae4a20b` |
| `logs/v5_31_fund_flow_evidence_freeze_history_head_checkpoints.jsonl` | `6d82cd4a047e863d24029f29c260a4d0cafd7a80904b9ca49d5ce7fa3efe870f` |

## 仍然陈旧的内容与恢复条件

v2—v4 cohort、旧 run summary 以及历史 README 迁移件继续保留，用于解释状态如何演进；它们不参与当前就绪判断。若活动 history、checkpoint、manifest 或任一 current 摘要再次不一致，系统必须立即回到失败关闭状态。

恢复人工辅助资格仍需同时满足：真实数据时点与覆盖率通过、V4.71 或受治理的后续稳健性门禁通过、冻结规则取得足够的新前推样本并完成 V5.07/V5.10 晋级、账户与组合风险通过、六角色确定性否决链全部通过。单独修复状态一致性不解除任何研究或交易门禁。
