<!--
archive_record_type: read_only_migration
source_path: README.md
source_commit: 36cc42926a72d488116417e48e6107b544754d93
source_lines: 324-366
original_text_sha256: a1ac7ca9e62dbb6a8ce528ce870ad27653cf0741cbaa007512f41d4fde7e7b1b
hash_basis: UTF-8, LF line endings, terminal LF included
ignored_output_link_normalizations: 11
-->

# V5.21—V5.31 资金流证据链归档

> 本页为 README 历史正文的只读迁移件。正文事实、版本和数值按原文保留；仅将被忽略的 `outputs/` Markdown 链接改成行内路径，防止历史文档产生失效本地链接。迁移记录见 `docs/research_history_migration_manifest.json`。

<!-- BEGIN MIGRATED README LINES 324-366 -->
## V5.21 新 PIT 数据源发现审计

V5.21 不再继续微调已经失败的价格、估值、成交额字段，而是盘点还能补足强反弹行业选择证据的新数据源。当前审计 9 类候选源，可直接进入强行业历史回测的新源数量为 0：申万二级估值和价格成交额已经验证失败；同花顺资金流只有 1 个缓存交易日且映射覆盖不足；东方财富历史资金流仍需重审接口稳定性；两融和北向属于市场状态源，不能证明行业选择 alpha；盈利预测/成分股聚合必须先建立 as-of 快照，不能用当前数据回填历史。结论继续是 `research_only_new_pit_source_not_ready`，下一步应优先累计资金流快照、重审东方财富历史资金流、建立盈利预测快照。输出目录：`outputs/audit/rebound_leader_new_pit_source_discovery_v5_21/report.md`。

## V5.22 东方财富行业历史资金流探针

V5.22 对 V5.21 的 P0 源“东方财富行业历史资金流”做了实际联网探针，测试行业资金流排名和汽车服务、半导体、银行三个历史资金流样本。当前全部失败，错误为 `ProxyError`，`successful_hist_probe_count=0`，`historical_source_ready=false`，因此该源仍不能进入申万二级映射审计，更不能进入强反弹行业回测。结论继续是 `research_only_eastmoney_fund_flow_not_ready`。输出目录：`outputs/audit/rebound_leader_eastmoney_fund_flow_probe_v5_22/report.md`。

## V5.23 行业资金流 PIT 面板

V5.23 把已缓存的同花顺行业资金流快照整理成统一 PIT 面板，当前覆盖 `2026-06-19` 和 `2026-06-22` 两个交易日，共 180 行。这个面板解决的是“后续每天新增快照如何进入同一评价体系”的问题，不解决样本不足问题：当前 `snapshot_date_count=2`，低于 60 日观察门槛和 252 日 alpha 验证门槛，高置信申万二级映射覆盖约 74.44%，仍低于 80% 门槛，因此仍然不能声称已经找到反弹窗口下更强行业。输出目录：`outputs/audit/fund_flow_pit_panel_v5_23/report.md`。

## V5.24 资金流映射保守修复

V5.24 只对白名单内 7 个明显同义或词序差异的同花顺行业到申万二级映射做保守提升，例如“公路铁路运输→铁路公路”“港口航运→航运港口”“塑料制品→塑料”。修复后资金流面板高置信申万二级映射覆盖从约 74.44% 提升到约 82.22%，通过 80% 映射门槛；但 `snapshot_date_count=2` 仍远低于 60/252 日门槛，所以这只是数据管道进展，不是强行业 alpha 证据。输出目录：`outputs/audit/fund_flow_mapping_remediation_v5_24/report.md`。

## V5.25 资金流双正前推观察

V5.25 把 `2026-06-22` 的 4 个资金流双正候选固化为前推观察账本，分别是保险Ⅱ、白酒Ⅱ、游戏Ⅱ、一般零售；计划入场日为 `2026-06-23`，计划退出日为 `2026-07-21`。账本路径为 `logs/v5_25_fund_flow_forward_ledger.csv`，同一批次和行业幂等去重。该版本只冻结未来样本，不计算未来收益、不生成交易指令；退出日后结算前，不能声称已经找到强反弹行业。输出目录：`outputs/audit/fund_flow_forward_observer_v5_25/report.md`。

## V5.26 资金流前推入场门禁

V5.26 为 V5.25 的 4 条资金流前推观察增加入场日前置门禁。以 `2026-06-22` 复核时，4 条观察全部仍是 `entry_not_due`，`entry_allowed_count=0`；计划入场日仍为 `2026-06-23`，入场日必须重新刷新资金流快照并人工复核，未通过前不得把观察样本计入已入场或已验证。该版本仍是 `research_only`，不生成交易指令。输出目录：`outputs/audit/fund_flow_forward_entry_gate_v5_26/report.md`。

## V5.27 资金流前推样本结算

V5.27 固化 V5.25 资金流前推观察的退出后结算口径：到计划退出日后，按申万二级行业指数收盘价计算行业收益、全行业等权基准、相对收益和未来收益 Top20% 命中。以 `2026-06-22` 复核时，4 条观察尚未到 `2026-07-21` 计划退出日，因此 `settled_rows=0`、`pending_rows=4`。该版本只定义结算规则，不回填未来收益，不改变筛选规则。输出目录：`outputs/audit/fund_flow_forward_settlement_v5_27/report.md`。

## V5.28 资金流前推晋级评价

V5.28 固化资金流前推样本的晋级门槛：至少 30 个已结算批次、至少 30 个已结算行业观察、平均和中位相对收益为正、正超额批次比例不低于 55%、未来收益 Top20% 命中率不低于 30%。以 `2026-06-22` 复核时，已结算批次为 0，因此 `promotion_ready=false`、`can_claim_strong_rebound_industries=false`。该版本只评价已结算前推样本，不读取未到期未来收益，不改变资金流筛选规则。输出目录：`outputs/audit/fund_flow_promotion_evaluator_v5_28/report.md`。

## V5.29 资金流前推证据日历

V5.29 汇总 V5.25-V5.28 的下一步证据动作和晋级缺口。以 `2026-06-22` 复核时，下一动作日期为 `2026-06-23`，动作为入场日前刷新和门禁复核；下一命令为 `python .\scripts\run_v4_71_live_refresh.py --trade-date 2026-06-23`。当前缺口仍包括已结算批次 0/30、已结算行业观察 0/30、4 条观察尚未到退出日，因此仍不能声称找到强反弹行业。输出目录：`outputs/audit/fund_flow_evidence_calendar_v5_29/report.md`。

## V5.30 资金流前推账本完整性审计

V5.30 检查 V5.25 资金流前推账本是否适合继续前推验证：字段完整、`batch_id+industry_code` 不重复、日期链满足 `signal_date <= planned_entry_date < planned_exit_date`、全部保持 `research_only`、资金流双正标记有效、未到退出日不得填写未来收益字段。以 `2026-06-22` 复核时，账本 4 行、违规 0 行，`integrity_passed=true`。这只证明账本干净，不证明已经找到强反弹行业。输出目录：`outputs/audit/fund_flow_forward_ledger_integrity_v5_30/report.md`。

## V5.31 资金流证据冻结指纹

V5.31 为资金流前推链建立 SHA256 指纹基线，覆盖 V5.25-V5.30 的关键脚本，以及 V5.25 前推账本的稳定观察字段逻辑指纹。基线写入 `logs/v5_31_fund_flow_evidence_freeze_manifest.csv`；后续重跑会检查是否有脚本或稳定观察字段在结算前后发生变化。以 `2026-06-22` 复核时，指纹数量 7 个、变化数量 0，`freeze_passed=true`。这只证明证据链未被改动，不证明已经找到强反弹行业。输出目录：`outputs/audit/fund_flow_evidence_freeze_manifest_v5_31/report.md`。

<!-- END MIGRATED README LINES 324-366 -->
