<!--
archive_record_type: read_only_migration
source_path: README.md
source_commit: 36cc42926a72d488116417e48e6107b544754d93
source_lines: 289-323
original_text_sha256: f5881603e2da4497eb10f80483699fcf06ec83fc01519841b8dcf39875a51cc0
hash_basis: UTF-8, LF line endings, terminal LF included
ignored_output_link_normalizations: 0
-->

# 仓库布局与历史研究边界

> 本页为 README 历史正文的只读迁移件。正文事实、版本和数值按原文保留；仅将被忽略的 `outputs/` Markdown 链接改成行内路径，防止历史文档产生失效本地链接。迁移记录见 `docs/research_history_migration_manifest.json`。

<!-- BEGIN MIGRATED README LINES 289-323 -->
## 文件放置规则

根目录普通文件只保留 `README.md`。后续研究新增文件按类型归档：

- `configs/`：策略、阈值、评价体系配置
- `scripts/`：可运行脚本
- `strategy_lab/`：agent 任务 brief、策略研究代码、dashboard
- `data_catalog/`：schema、数据字典、manifest
- `factor_library/`：因子定义和注册表
- `logs/`：研究日志、阅读队列、纠错记录
- `replication_reports/`：复现实验报告
- `portfolio_lab/`：组合构建和人工复核模板
- `outputs/`：所有运行输出

## 输出结构

每个研究输出目录顶层只保留：

```text
report.md
top_candidates.csv
run_summary.json
debug/
```

- `report.md`：中文研究报告，优先打开。
- `top_candidates.csv`：核心候选或主规则摘要。
- `run_summary.json`：机器可读摘要。
- `debug/`：复现、审计、中间数据和评价明细。

## 研究边界

当前的“有效反弹窗口”只表示：在现有历史样本、当前 V3.4 评价体系和现金基准口径下，这个市场窗口识别规则通过了收益、样本、独立行情簇、路径风险、年度稳定性、bootstrap 和审计门槛。

它不等于行业选择 alpha，不等于个股或 ETF 推荐，也不等于实盘交易系统。下一步如果继续推进，应优先做最新行业数据刷新、参数脆弱性修复、真实载体执行回放和新增样本前推观察，而不是继续加复杂参数。

<!-- END MIGRATED README LINES 289-323 -->
