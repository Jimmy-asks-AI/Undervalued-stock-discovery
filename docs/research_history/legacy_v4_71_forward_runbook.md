<!--
archive_record_type: read_only_migration
source_path: README.md
source_commit: 36cc42926a72d488116417e48e6107b544754d93
source_lines: 189-239
original_text_sha256: 88f6a01e833e840919d4a5f0ab64f4a2945c35d67fdf47a2c1650697cd6df0f3
hash_basis: UTF-8, LF line endings, terminal LF included
ignored_output_link_normalizations: 0
-->

# V4.71 前推样本旧操作手册

> 本页为 README 历史正文的只读迁移件。正文事实、版本和数值按原文保留；仅将被忽略的 `outputs/` Markdown 链接改成行内路径，防止历史文档产生失效本地链接。迁移记录见 `docs/research_history_migration_manifest.json`。

<!-- BEGIN MIGRATED README LINES 189-239 -->
## 如何运行

```powershell
python .\scripts\run_v4_71_live_refresh.py
```

这个命令会顺序刷新行业数据、重跑 V3.7/V4.70/V4.71/V4.72，并执行输出结构和 task brief 审计。V4.72 会同步更新反弹窗口内强行业候选。若只想使用本地缓存做快速复核：

```powershell
python .\scripts\run_v4_71_live_refresh.py --skip-history-refresh
python .\scripts\run_v4_71_live_refresh.py --trade-date 2026-06-23 --skip-history-refresh
```

刷新清单会写入 `debug/live_refresh_manifest.json`，warning 汇总会写入 `debug/live_refresh_warnings.csv`，用于检查每一步命令、耗时和数据异常。`run_v4_71_live_refresh.py --trade-date <日期>` 会按这个日期审计前推账本，并回写 `debug/forward_sample_ledger_audit.csv`；例如 2026-06-23 会把仍未处理的 4 条 planned 标成 `pending_entry_due`。

入场日前，如果人工复核后决定跟踪某个载体，先预登记，避免退出后事后挑最好载体：

```powershell
python .\scripts\append_v4_71_forward_sample.py --decision planned --carrier-code 510300 --carrier-name 沪深300ETF华泰柏瑞 --notes "入场前预登记"
```

入场日如果确实开始跟踪或真实交易，先记录实际入场价。脚本会读取 `debug/manual_carrier_review_sheet.csv` 的 `max_reference_entry_price`；如果入场价超过不追价上限，会拒绝记录。只有明确人工覆盖时才加 `--allow-price-drift`，且必须用 `--notes` 写明原因；覆盖后会在账本里记录 `price_drift_override=True`，并进入 `forward_sample_ledger_clean` 审计：

```powershell
python .\scripts\append_v4_71_forward_sample.py --decision entered --carrier-code 510300 --carrier-name 沪深300ETF华泰柏瑞 --entry-price 4.984 --notes "入场日记录"
```

退出日后再用真实退出价补写收益：

```powershell
python .\scripts\append_v4_71_forward_sample.py --decision entered --carrier-code 510300 --carrier-name 沪深300ETF华泰柏瑞 --exit-price 5.100 --notes "退出日补写真实收益" --replace
```

如果入场日人工复核后决定跳过，也要关闭这条预登记：

```powershell
python .\scripts\append_v4_71_forward_sample.py --decision skipped --carrier-code 510300 --carrier-name 沪深300ETF华泰柏瑞 --notes "入场日跳过"
```

`planned` 预登记只写计划日期，不填写实际成交日期；`planned/skipped/observe` 不接受成交日期或价格参数。`entered` 和 `skipped` 记录默认要求同一个 tracker/载体已经有 `planned` 预登记。如果是补录历史遗漏，必须显式加 `--allow-unplanned`，且必须用 `--notes` 写明原因。`entered` 可以先只写入场价，退出价留空；退出日用 `--replace` 补写退出价后才计算收益，脚本会从已有 `entered` 行读取入场价。`entered` 记录会校验实际入场/退出日期为 `YYYY-MM-DD`，且退出日期不得早于入场日期。

检查前推样本账本：

```powershell
python .\scripts\append_v4_71_forward_sample.py --migrate-schema
python .\scripts\append_v4_71_forward_sample.py --audit
python .\scripts\append_v4_71_forward_sample.py --audit --as-of-date 2026-06-23 --audit-output .\outputs\industry_rebound_window_v4_71_robustness_live_audit\debug\forward_sample_ledger_audit.csv
```

账本位置：`logs/v4_71_forward_sample_ledger.csv`。`--migrate-schema` 只补齐新增表头，不改变已有记录含义。`--as-of-date` 用于按指定交易日判断 planned/entered 是否到期，`--audit-output` 用于把审计结果写回 CSV。V4.71 报告和 `debug/forward_sample_ledger_audit.csv` 也会展示同一套审计状态；同一载体不能同时写入 `entered` 和 `skipped`。如果到了计划入场日还只有 `planned`，`pending_entry_due` 会提示需要处理；如果到了计划退出日仍有未补退出价的 `entered`，`exit_review_due` 会提示补写收益。


<!-- END MIGRATED README LINES 189-239 -->
