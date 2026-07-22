<!--
archive_record_type: read_only_migration
source_path: README.md
source_commit: 36cc42926a72d488116417e48e6107b544754d93
source_lines: 1-85
original_text_sha256: 1c3bc3b0eca31dfde3272401534bb2f59ef91c139fe7703700273b3a02237bba
hash_basis: UTF-8, LF line endings, terminal LF included
ignored_output_link_normalizations: 2
-->

# V4.70—V4.71 研究与实盘前审计归档

> 本页为 README 历史正文的只读迁移件。正文事实、版本和数值按原文保留；仅将被忽略的 `outputs/` Markdown 链接改成行内路径，防止历史文档产生失效本地链接。迁移记录见 `docs/research_history_migration_manifest.json`。

<!-- BEGIN MIGRATED README LINES 1-85 -->
# 行业指数反弹窗口研究系统

## ETF量化辅助交易当前主线

当前系统面向 A 股市场研究，交易载体限定为境内上市股票型宽基、行业或主题指数 ETF，不包含个股。系统是日频、长仓、T+1、人工确认的辅助决策工具，自动下单始终关闭。

```powershell
python -m pip install backtrader==1.9.78.123
python .\scripts\run_etf_realistic_execution_replay.py
python .\scripts\run_etf_assisted_trading_current.py --as-of-date YYYY-MM-DD --refresh-inputs
python .\scripts\audit_etf_assisted_trading_completion.py
```

Backtrader 只用于独立事件引擎逐笔交叉验证；主研究逻辑不依赖它生成信号。当前 57 笔成交已完成双引擎一致性复核。

优先查看：

- `outputs/etf_assisted_trading_current/report.md`：当前动作与硬门禁。
- `outputs/audit/etf_assisted_trading_completion/report.md`：工程完成度与建议就绪度。
- `outputs/audit/etf_pit_master/report.md`：ETF 官方主表、生命周期与跟踪指数映射审计。
- `outputs/audit/official_etf_lifecycle_sources/report.md`：两交易所 ETF 终止上市公告批量源、代码与生效日覆盖审计；只证明数据源可用，不代替历史 PIT 完整性证明。
- `outputs/audit/etf_sw_industry_exposure_mapping/report.md`：用官方指数权重和申万股票分类计算 ETF 的申万二级行业暴露，低覆盖或跨行业 ETF 不映射。
- `outputs/audit/etf_realistic_execution_replay/report.md`：下一交易时点、T+1、滑点和佣金回放。

只有完成度审计中全部 `readiness` 项通过，系统才允许升级为人工买卖建议；任一项失败时保持 `research_only` 或风险阻断。

ETF 成交回放必须使用未复权历史价格。当前 `outputs/audit/etf_realistic_execution_replay/` 已对 57 笔 V4.70 窗口执行下一交易日开盘、100 份整手、T+1、滑点、最低佣金、涨跌停和次日止损回放，并通过 Backtrader 逐笔复核；前一可用单位净值参考覆盖 100%。该参考包含隔夜市场变动，不等同真实折溢价；历史 IOPV 和真实盘口价差仍是实施缺口。

当前持仓状态机读取账户中的保护价、可卖份额和执行快照。执行快照需包含 `bid_price`、`ask_price`、`iopv`、`average_daily_amount_20d`、`current_industry_rank`；缺失时输出 `REVIEW_REQUIRED`，不会猜测折溢价、流动性或行业排名。

每日收盘后先把 `portfolio_lab/current_account_state.json` 的 `as_of_date`、现金、权益和持仓更新到当日，再运行带 `--refresh-inputs` 的统一入口。它严格执行 9 步最小刷新链并重建 Dashboard 数据；不带该参数时只基于现有输入快速生成建议。ETF 官方目录若出现覆盖率或数量劣化，不会覆盖最近合格 PIT 快照；主线仍按 4 个日历日 SLA 检查该快照，超期后自动进入 `BLOCKED_DATA`。行业行情逐文件检查日期，至少 120 个行业在 SLA 内才通过，不能再用多数日期掩盖陈旧文件。

当前辅助交易主线统一回归命令：`python scripts/run_etf_assisted_trading_regression.py`。它复用目标门禁、ETF PIT、生命周期、申万成分暴露、真实成交、六 Agent 主线、行业候选数据新鲜度、前推检测、严格结算、择时/行业晋级、纸面人工决策日志和完成度审计共 12 项已有自检，结果归档到 `outputs/test/etf_assisted_trading_regression/`。

人工确认后可用 `python scripts/record_etf_paper_decision.py --decision ACCEPT --operator Jimmy --executed-action NO_ACTION` 追加纸面决定；拒绝或延后必须同时提供 `--note`。使用 `python scripts/record_etf_paper_decision.py --verify` 校验日志哈希链。该工具只记录用户明确决定，不会自动接受建议或生成成交。

本地 Dashboard 使用 Carbon 设计系统，首行展示上证综指日 K 线、历史 ETF 买卖标记、所选历史时点的指数点位状态和当前 ETF 操作建议；下方保留五道买入门槛、低估行业、阻断原因、6 个主要 A 股指数状态和 510300 历史买卖回放。指数点位状态仅使用当日及此前 756 个交易日的滚动分位，20% 以下为低估区、80% 以上为高估区；超买超卖使用 RSI14 的 70/30 阈值。点位分位不等同 PE/PB 基本面估值。运行地址为 `http://127.0.0.1:4175/`；构建命令为 `npm run build`，目录为 `strategy_lab/research_dashboard/`。

这个工作区用于研究申万行业和行业指数的低估、超跌、压力释放与反弹窗口。当前系统不做个股筛选，不把 ETF 当作研究对象；ETF 只作为人工复核的可交易载体，不生成买入/卖出/下单指令。

当前统一入口是 `python .\scripts\run_etf_assisted_trading_current.py --as-of-date YYYY-MM-DD --refresh-inputs`。它按数据新鲜度、择时稳健性、行业选择、ETF PIT、账户状态和目标证据顺序执行硬门禁；任何上游失败都不会生成 ETF 候选。当前输出目录为 `outputs/etf_assisted_trading_current/`，仍保持 `report.md`、`top_candidates.csv`、`run_summary.json`、`debug/`。

## 当前结论

最新研究规则是 `V4.70 延迟入场与高波动保护止损`；最新实盘前审计版本是 `V4.71 稳健性与可交易载体复核`。

- 输出目录：`outputs/industry_rebound_window_v4_70_delayed_entry_vol_stop/report.md`
- 统一评价体系：`rebound_window_effectiveness_evaluation` V3.4
- 评价结果：`有效反弹窗口`
- `effective=true`
- 原始分：100.0
- 认证分：100.0

V4.70 的规则很窄：沿用 V4.68 的非追涨事件定义，固定延迟 2 个交易日入场；只有信号日 `market_volatility_20d_vs_60d >= 1.30` 时，才启用 6% 保护止损。

核心结果：

- 事件数：57
- 独立行情簇：20
- 10bps 成本后平均收益：3.24%
- 相对现金平均收益：3.34%
- 胜率：63.16%
- 坏窗口率：19.30%
- 最差单笔：-6.00%
- 最差独立簇成本后收益：-3.93%
- 持有路径最大不利波动：-6.26%
- 年度正收益比例：88.89%

V4.71 对 V4.70 做了实盘前复核，结论是 `production_ready=false`。

- 输出目录：`outputs/industry_rebound_window_v4_71_robustness_live_audit/report.md`
- 参数扰动通过率：33.33%，不足以证明规则稳定。
- 60 日冷却期独立行情簇：12 个，低于当前样本下限。
- 最新行业面板日期：2026-06-18，实时性已通过；最新日触发 V4.70 条件。
- 若按冻结规则执行，入场需要等信号后 T+2 交易日；交易日历给出的计划入场日是 2026-06-23，计划持有 20 个交易日，计划退出日是 2026-07-21。
- 可交易载体口径已拆分：现货候选 30 个；人工复核表展示 20 个；当前执行回放覆盖 5 个载体、217 行记录；其中 4 个载体达到至少 30 个事件的审计门槛，16 个展示载体仍是历史不足的谨慎项。
- 人工载体复核表已生成：`debug/manual_carrier_review_sheet.csv`，用于入场日前逐项检查流动性、折溢价、跟踪误差和人工仓位限制；表内会先给出 `优先复核/谨慎` 自动标记，并提供每个载体的预登记命令。默认自动执行为“否”；优先复核载体只给人工仓位上限模板：单载体 8%、策略合计 20%、参考止损风险 6%，并按 10 万元参考账户、ETF 100 份一手生成参考份额、参考名义金额和参考止损亏损额，同时计算参考单占当日成交额的 bp 数和容量状态，并给出入场价上浮 2% 的不追价参考上限。账户规模、仓位上限、风险口径、成交额占比阈值和入场价漂移阈值在 `configs/rebound_window_v4_71_robustness_live_audit_policy.json` 的 `position_sizing` 中调整。
- 入场前人工复核单已生成：`debug/pre_entry_manual_review.md`，用于交易日前确认是否仍只能观察、是否需要跳过；优先复核载体会同时给出预登记、入场记录、退出结算和跳过命令。
- 实盘辅助决策包状态：`watchlist_only_research_signal`，只能作为研究观察和人工复核清单，不能自动下单，也不能把候选载体视为买入建议。
- 前推样本跟踪状态：`pending_entry`；入场日前应重跑 V4.71，退出日后补写真实载体前推收益。
- 入场前复核 Gate 已生成：信号、计划日期、数据新鲜度和载体回放通过；自动交易状态、参数扰动稳定性和 60 日冷却期独立样本仍不通过。
- 参数失败诊断已生成：不能提前到 T+1，不能取消/放宽保护止损，也不能一刀切止损；当前只允许冻结规则作为观察参考。
- 生产就绪证据债务已生成：参数扰动还差 3 个通过版本；60 日冷却口径还差 8 个独立行情簇。

所以当前严谨结论是：V4.70 在历史评价框架内通过，但 V4.71 暴露出实盘前硬问题。系统现在可以作为研究和人工复核工具，不能直接作为自动交易或下单系统。

<!-- END MIGRATED README LINES 1-85 -->
