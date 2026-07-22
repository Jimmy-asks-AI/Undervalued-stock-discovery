# A 股低估资产与 ETF 辅助决策研究

## 项目定位

本项目研究申万行业的低估、超跌企稳、反弹窗口和强行业选择，并以境内上市股票型宽基、行业或主题指数 ETF 作为人工复核的交易载体。系统采用日频、长仓、T+1 和硬门禁治理，只提供研究与辅助判断；不做个股筛选，不自动下单。

当前唯一权威状态见 [CURRENT_STATUS](./CURRENT_STATUS.md)。任何历史评分、候选清单、观察记录或局部审计结果，都不能绕过该页列出的证据边界。

## 公开仓库边界

仓库保留研究代码、配置、数据合同、测试、治理日志和可公开复核的数据快照。真实账户状态、持仓与操作上下文、密钥、授权数据、行情缓存、生成输出、本机恢复文件和绝对路径均不进入公开版本；需要运行时请按 schema 在本地自行补齐。仓库内容只用于研究与工程复现，不构成投资建议。

## 当前状态摘要

截至运行日 `2026-07-21`，主线决策快照仍为 `2026-07-18`，当前结论是 **`research_only / NO_ACTION`**：强行业 Alpha 尚未验证，人工辅助交易未就绪，自动交易禁止。

| 项目 | 当前状态 |
|---|---|
| 当前 runner | `CURRENT_MAINLINE / 1.0.0` |
| 策略版本 | `V4.70`，只代表历史市场反弹窗口规则 |
| 稳健性审计 | `V4.71`，`production_ready=false` |
| 目标审计 | `V5.10`，`goal_ready=false` |
| 数据治理链 | `V5.25—V5.35` |
| active forward cohort | `ff_integrity_v8_20260721` |
| 当前动作 | `NO_ACTION` |
| 主线硬门禁 | 10 个阻断：PIT 估值与行业历史方法、择时、行业、账户、现有组合风险、目标证据、六角色确定性否决链、建议后组合风险、择时前推证据、强行业前推证据 |
| 强行业合格前推样本 | 0 |
| 探索性资金流观察 | 4；settled 0 / terminal blocked 4 / pending 0 / qualified settled 0 |
| 工程合同检查 | 29/29；只证明实现合同存在 |
| 建议就绪门禁 | 6/13；未通过 |
| 独立行为测试 | 23/23 |
| 主线自检回归 | 12/12 |

主线数据截止并不整齐：行业行情到 `2026-07-15`，官方直接来源估值历史到 `2025-12-31`；原表 `2026-06-12` 的 131 行回收快照已隔离，不能充当官方历史。估值快照、市场指数和 ETF 数据到 `2026-07-16`，资金流到 `2026-06-26`，账户状态到 `2026-07-13`。四条探索记录的独立终局审计另行核验了 `2026-06-23` 与 `2026-07-21` 的 123 个同一行业精确行情，四个目标 4/4；这批结算专用数据不回写 `2026-07-18` 主线决策，也不产生收益结论。完整口径、冻结规则、样本分栏、恢复条件和禁止事项均由 [CURRENT_STATUS](./CURRENT_STATUS.md) 给出。

## 快速开始

项目锁定 Python `3.14.3`、uv `0.11.29`。先在仓库根目录恢复环境：

```powershell
uv sync --frozen --python 3.14.3 --group test --group verify
. .\.venv\Scripts\Activate.ps1
python -m pip check
```

只读现有本地缓存生成当前建议：

```powershell
python .\scripts\run_etf_assisted_trading_current.py --as-of-date YYYY-MM-DD
```

需要刷新输入时，先把 `portfolio_lab/current_account_state.json` 更新为真实账户快照，再显式运行十六项输入刷新链：

```powershell
python .\scripts\run_etf_assisted_trading_current.py --as-of-date YYYY-MM-DD --refresh-inputs
```

默认测试与分层核验：

```powershell
python -m pytest
python .\scripts\run_current_mainline_behavior_tests.py
python .\scripts\run_etf_assisted_trading_regression.py
python .\scripts\audit_etf_assisted_trading_completion.py
python .\scripts\audit_current_state_consistency.py
python .\scripts\build_research_version_inventory.py --check
python .\scripts\audit_research_governance_coverage.py
python .\scripts\build_current_status.py
```

构建并预览 Dashboard：

```powershell
python .\scripts\build_dashboard_dataset.py
Push-Location .\strategy_lab\research_dashboard
npm ci --no-audit --no-fund
npm run validate:data
npm run check
npm run build
npm run preview
Pop-Location
```

开发服务固定使用 `http://127.0.0.1:5173/`，预览服务固定使用 `http://127.0.0.1:4175/`。两者端口被占用时会直接报错，不会静默切换地址。

Dashboard 的三种“刷新”不是一回事：

1. 页面内的“重新读取本地结果”只重新读取 `public/data/dashboard_data.json`，不会访问外部数据源。
2. `python .\scripts\build_dashboard_dataset.py` 从仓库内已有权威产物重建本地 Dashboard 摘要和历史明细；首屏摘要与按需加载的历史明细分开保存。
3. `python .\scripts\run_etf_assisted_trading_current.py --as-of-date YYYY-MM-DD --refresh-inputs` 才会显式刷新上游输入；完成后还要重建 Dashboard 数据。账户快照必须先由操作者按真实情况更新。

Dashboard 摘要从 `CURRENT_STATUS`、当前状态一致性审计和当前 runner 读取 `research_only / NO_ACTION`、逐源截止日与 active cohort。ignored 的本地生成产物只通过稳定 `evidence_id` 和仓库相对路径登记，不当作公开链接。

完整验收可从仓库根目录运行：

```powershell
python .\scripts\audit_dashboard_trust_acceptance.py

Push-Location .\strategy_lab\research_dashboard
npm run preview
# 另开一个终端，在同一目录运行浏览器矩阵
npm run audit:ui
Pop-Location
```

命令验收覆盖数据构建自检、数据合同、22 个前端测试、类型检查、生产构建和锁文件复验；浏览器矩阵覆盖 1440px 与 390px 的正常态和异常态。UI QA 依赖 verify 组锁定的 `playwright==1.60.0`，调用系统 Chrome，不需要另行下载 Playwright 浏览器。

## 证据边界

- `V4.70` 的历史框架分数不是生产就绪评分；`V4.71 production_ready=false` 与当前硬门禁优先。
- V4.72—V5.35 尚未证明稳定的强行业 Alpha。历史候选、伪前推、探索性资金流观察和迟到回填均不得计入合格前推样本。
- `ff_integrity_v8_20260721` 只证明活动证据 cohort 的清单与历史链通过复验，不证明策略有效。
- PIT 估值与行业历史方法控制已接入主链；历史发布时点和官方分类成员表仍缺失，因此历史晋级门保持阻断。
- V5.07 仍缺可重放的追加式证据账本；摘要中自报完整性不能解锁前推路线。
- 29/29 工程合同检查、23/23 行为测试和 12/12 自检回归属于不同验证层；它们都不能替代研究证据、真实账户和可成交性门禁。
- 六角色确定性否决链中的任一角色否决，当前 runner 都必须保持 `NO_ACTION`。
- 本项目不构成投资建议。人工辅助交易资格恢复后，自动交易仍须独立授权与治理；当前 `auto_execution_allowed=false`。

## 文档索引

- [当前项目状态](./CURRENT_STATUS.md)：唯一当前结论、真实数据截止、门禁、样本和恢复条件。
- [研究状态与治理整改报告](./notes/research_governance_remediation_report.md)：目标 4A—4D 的整改范围、证据和验证矩阵。
- [PIT 估值与行业历史口径整改报告](./notes/pit_universe_methodology_report.md)：目标 5 的字段合同、宇宙断点、失败关闭和恢复条件。
- [四条探索性资金流记录终局处置说明](./notes/fund_flow_exploratory_settlement_2026_07_21.md)：精确日期覆盖、逐条终局、隔离修正和证据边界。
- [Dashboard 可信度整改报告](./notes/dashboard_trust_remediation_report.md)：目标 6 的数据合同、移动端字段、异常态截图和验收记录。
- [当前状态一致性整改报告](./notes/current_state_consistency_report.md)：cohort 权威源、依赖顺序、冲突与恢复条件。
- [项目全量审查报告](./notes/项目全量审查报告_2026-07-17.md)：整改前的完整问题盘点与风险分级。
- [可复现性与测试报告](./notes/reproducibility_and_test_report.md)：环境锁定、测试分层与恢复验证。
- [研究历史索引](./docs/research_history/README.md)：原 README 历史正文的无损迁移入口。
- [仓库布局与历史研究边界](./docs/repository_layout.md)：文件职责和标准输出四件套。
- [版本变更日志](./logs/version_changelog.md)、[研究日志](./logs/research_log.md)、[纠错日志](./logs/review_correction_log.md)：只追加的治理记录。
- [研究版本库存（JSON）](./logs/research_version_inventory.json) 与 [研究版本库存（CSV）](./logs/research_version_inventory.csv)：V4.72—V5.35 和当前主线的机器可读库存。
- [研究治理可恢复基线](./notes/handoffs/RESEARCH_GOVERNANCE_BASELINE_2026-07-18.md)：最终标签、外部 bundle、恢复步骤与数据边界。
