# 四条探索性资金流记录结算预检

日期：2026-07-18

## 技术结论

正式结算不能在今天启动。权威目标规定的最早时间是 `2026-07-21 15:00:00+08:00`；本次现场预检生成于 `2026-07-18T23:26:47+08:00`，状态为 `blocked_pre_start`。

四条记录已经逐一对上，当前仍是 pending 4、blocked 0、settled 0。预检没有打开退出行情文件，没有读取或写入收益，也没有改写 observation、候选冻结、基准冻结或 cohort 账本。主线结论继续保持 `research_only / NO_ACTION`。

这不是“结算完成”报告。正式目标仍在等待 7 月 21 日收盘时间门禁和精确日期数据门禁。

## 四条记录均已失去合格前推资格

| 行业 | observation_id | 计划区间 | 当前账本状态 | 候选冻结 | 基准冻结 |
|---|---|---|---|---|---|
| 801194 保险Ⅱ | `ffobs-fef718698a6c9566ae3c49c7` | 2026-06-23 → 2026-07-21 | `not_due` | `late_backfill_excluded` | `late_backfill_excluded`，基准 0 行 |
| 801125 白酒Ⅱ | `ffobs-dfe49086cdac49809130aedd` | 2026-06-23 → 2026-07-21 | `not_due` | `late_backfill_excluded` | `late_backfill_excluded`，基准 0 行 |
| 801764 游戏Ⅱ | `ffobs-6cc0c1c106273b1f038a3abd` | 2026-06-23 → 2026-07-21 | `not_due` | `late_backfill_excluded` | `late_backfill_excluded`，基准 0 行 |
| 801203 一般零售 | `ffobs-4f4f00430698719565ca9676` | 2026-06-23 → 2026-07-21 | `not_due` | `late_backfill_excluded` | `late_backfill_excluded`，基准 0 行 |

四条共同保持以下不可变边界：

- `sample_scope=exploratory_fund_flow_only`
- `qualified_for_goal=false`
- `integrity_eligible=false`
- `promotion_eligible=false`
- `cohort_id=legacy_exploratory_20260622`
- `cohort_manifest_hash=UNVERIFIED_LEGACY_COHORT`
- 实际入场日、退出日和全部收益字段为空

候选价与全行业基准都是入场截止后才补做的冻结审计，因此已经被不可逆地标记为 `late_backfill_excluded`。以后即使行情数据补齐，也只能说明“后来有了历史价格”，不能把事后价格改写成 6 月 23 日按时冻结的前推证据。

## 现有 V5.27 不能替代这次逐条处置

当前 V5.27 只处理与 active cohort 精确匹配的观察。现场 active pair 是：

- cohort：`ff_integrity_v7_20260718`
- manifest：`966e40a07d2248d8447692e85faf3d28d4ffaee51b2db8b0a787a861db0bf7e2`
- 现场复验：通过

四条旧观察属于 legacy cohort，所以 V5.27 当前输出是 active `ledger_rows=0`、global history `ledger_rows=4`。直接运行原脚本只会得到 0 行，不会形成四条逐项处置；若强行放宽 active cohort 或完整性门禁，又会把旧记录伪装成合格前推。

本轮新增的专用审计只读取固定 allowlist 中的四条 global legacy 记录，并把“当前 pending”与“到期后可能形成的 terminal blocked”分开。它不修改 V5.27 的合格样本规则，也不向权威 JSONL 追加伪 settlement event。

## 预检方法与复核结果

本次预检执行：

```powershell
python -B .\scripts\audit_fund_flow_exploratory_settlement_readiness.py --preflight
python -B -m pytest -q .\tests\test_fund_flow_exploratory_settlement_readiness.py
```

验证结果：

| 检查 | 结果 |
|---|---|
| 固定 allowlist 四条记录 | 4/4 命中，无缺失、重复或额外记录 |
| observation / candidate freeze / benchmark freeze / cohort history checkpoint | 4/4 通过 |
| active cohort 现场重算 | 通过 |
| 证据 SHA 清单 | 17 个文件，含四个 checkpoint、active manifest、policy、schema、日历与代码 |
| 时间门禁 | 未到期，正确失败关闭 |
| 行情文件读取 | `false` |
| 收益读取或写入 | `false` |
| 权威账本改写 | `false`；运行前后敏感文件 SHA 完全一致 |
| 专用边界与反例测试 | 20/20 通过 |
| 资金流证据链联合回归 | 76/76 通过 |

测试覆盖北京时间 `14:59:59` 与 `15:00:00` 边界、预检与正式目录隔离、99/100/131 个基准行业、精确日期行必须带正数收盘点位、四条身份和资格不变量、缺失或非法布尔值、重复候选/基准冻结键、晚冻结在以后出现精确日期时仍不能转为 settled、缺记录失败关闭，以及 blocked 四件套结构。

没有生成收益图。当前不存在可合法结算的收益值，图形会把未到期或事后补录数据包装成可比较结果；四行审计表更符合现阶段的证据任务。

## 预检产物

预检标准四件套位于 `outputs/audit/fund_flow_exploratory_settlement_preflight/`：

- `report.md`
- `run_summary.json`
- `top_candidates.csv`，内容类型固定为 `exploratory_settlement_disposition`，不是新候选
- `debug/`

`debug/` 保存逐条处置、SHA 清单、时间与日期覆盖状态、命令状态，以及分别重新采集的结算前后只读快照。当前 `audit_mode=preflight`、`exit_data_read=false`、`all_return_fields_empty=true`；post snapshot 明确记录 `authoritative_hashes_unchanged=true`。

## 正式处置的恢复条件

正式运行必须同时满足：

1. 现场时钟不早于 `2026-07-21 15:00:00+08:00`，不得用伪造 as-of 绕过。
2. 精确 2026-06-23 与 2026-07-21 行情均已落地；两个日期至少各覆盖 100 个同日申万二级行业，四个候选都必须精确命中。
3. observation、冻结账本、checkpoint、active cohort、manifest、代码、policy、schema 和日历哈希复验通过。
4. 缺少任何精确日期或基准覆盖时继续 pending，不向后寻找交易日，不使用当前快照回填。

时间和数据门禁都通过后，专用审计才能生成 `outputs/audit/fund_flow_exploratory_settlement_2026_07_21/`。按当前不可变冻结证据，预期合规终局是 settled 0、terminal blocked 4、qualified settled 0；这四条只回答旧观察为何不能被合法结算，不证明或否定强行业 Alpha。

正式处置完成前，README 和 CURRENT_STATUS 继续保留“4 条未结算”的现状，不提前改成已处置。正式四条 disposition 落地后，再通过状态生成器更新独立的 settled / blocked / pending 计数，并复跑 V5.28、V5.29、V5.30、V5.35、当前状态一致性与主线回归。
