# 当前主线可复现环境与行为测试报告

日期：2026-07-18

## 结论

“锁定环境并建立真实行为测试”已完成工程整改和独立环境验收。

- Python 与 Node 均有精确版本声明、可复现锁文件和干净安装证据。
- 带完整本地治理输出的工作树，默认离线测试为 `166 passed, 1 deselected`；其中独立行为测试 `23 / 23`，不再与 12 项脚本自检混算。
- 只从 Git bundle 恢复的干净 clone 不含 ignored research outputs；默认测试为 `162 passed, 4 skipped, 1 deselected`。4 项跳过均为输出证据集成检查，不能记作通过；显式治理审计仍对缺失证据失败关闭。
- 当前主线自检回归为 `12 / 12`。
- Dashboard 数据契约测试为 `6 / 6`，真实生成数据校验、TypeScript 检查与生产构建均通过。
- 自动执行保持关闭；当前动作仍为 `NO_ACTION`。本次没有改变策略阈值，也没有改变研究结论。
- 完成度审计仍为建议就绪 `6 / 12`，人工决策支持状态为 `false`。缺少授权历史实施数据、真实账户和独立前推证据等问题没有被测试通过所掩盖。

## 运行环境与锁定边界

### Python

| 项目 | 锁定值 | 验收结果 |
| --- | --- | --- |
| CPython | `3.14.3` | 干净环境实测 |
| 兼容范围 | `>=3.14,<3.15` | 只声明已完成行为验证的版本 |
| 锁工具 | `uv 0.11.29` | `uv lock --check` 通过 |
| 锁文件 | `uv.lock` | 解析 47 个包；干净环境安装 44 个适用包 |
| 安装一致性 | `pip 25.3` | `python -m pip check` 返回 `No broken requirements found.` |

默认依赖只覆盖当前 ETF 辅助交易主线、十一项输入刷新链、自检、离线行为测试和报告生成。`tushare`、`jqdatasdk`、`WindPy`、`EmQuantAPI`、`iFinDPy` 被明确列为外部数据商可选 SDK，不进入默认锁环境；仓库与锁文件均不保存凭证。

### Node

| 项目 | 锁定值 | 验收结果 |
| --- | --- | --- |
| Node.js | `24.13.0` | `.node-version`、`.nvmrc` 与干净环境一致 |
| npm | `11.6.2` | `packageManager` 与干净环境一致 |
| 依赖锁 | `package-lock.json`，lockfile v3 | `npm ci --no-audit --no-fund` 从零安装 136 个包 |
| 直接依赖边界 | `@carbon/icons-react@11.83.0` | 不再依赖偶然提升 |
| 锁文件稳定性 | SHA-256 `556351E341C68E7B356CD4F31B53A160615B36631FB1A381DD63A24D5EFC5205` | `npm ci` 前后与主工作区完全一致 |

## 独立行为测试

标准四件套位于 `outputs/test/current_mainline_behavior/`：

- `report.md`
- `run_summary.json`
- `top_candidates.csv`
- `debug/`

行为测试共 23 项，分层如下：

| 证据层 | 通过 | 覆盖重点 |
| --- | ---: | --- |
| contract | 2 / 2 | 研究编排器自动执行恒为关闭；纸面记录器拒绝自动执行 |
| unit | 8 / 8 | as-of 查询、停牌/涨跌停、T+1、无退出价、账户时点、持仓汇总、建议后风险 |
| integration | 1 / 1 | 建议后风险超限重新进入硬门禁并撤销候选 |
| data-quality | 6 / 6 | PIT、陈旧/乱序、网络失败保留快照、生命周期与两条 ETF 映射路径 |
| research-evidence | 6 / 6 | 重复前推、严格时间顺序、119/120 与 120/120 基准覆盖边界 |

这些测试使用固定 fixture、临时目录和 monkeypatch，不访问实时行情。完成度审计中的源码字符串检查只记作 `contract`，不计入 `behavior_test_pass_count`。

12 项脚本 `--self-check` 另存于 `outputs/test/current_mainline_self_check/`，摘要字段统一为 `self_check_*`；它只证明脚本基本合同与可调用性，不替代行为测试。

## 干净环境验收

验收在从基线提交 `0587f48` 建立的独立 detached worktree 中进行，再应用本次整改快照。该目录开始时没有 `.venv`、`node_modules` 或现成生成数据。

| 验收项 | 观察结果 |
| --- | --- |
| `uv sync --frozen --python C:\Python314\python.exe --group test --group verify` | 成功新建 `.venv` 并安装锁定依赖 |
| `uv lock --check` | 通过 |
| 默认 pytest | `166 passed, 1 deselected` |
| bundle 干净恢复 pytest | `162 passed, 4 skipped, 1 deselected`；4 项均明确标注为 ignored-output 集成检查 |
| `python -m pip check` | 通过 |
| 独立行为测试 runner | `23 / 23` |
| 当前主线自检回归 | `12 / 12` |
| 显式 live smoke | 单独运行 `1 passed`；不计入离线核心测试 |
| V5.31 冻结 cohort | `ff_integrity_v5_20260718` / `531cf927cd18cc3c774777098ba20794b7f78c1f8dbfe67a49397d8a6f17954c`，独立复验 `freeze_passed=true` |
| `npm ci` | 成功，从零安装 136 个包 |
| `npm ls --depth=0` | 无 missing 或 extraneous |
| 干净数据生成 | 成功；在没有历史 outputs 时产生 63 条缺失证据警告和明确空建议，不伪造 `NO_ACTION` |
| `npm run validate:data` | fixture 数据与真实生成的空状态数据均通过 |
| `npm test` | `6 / 6` |
| `npm run typecheck` | 两份 TypeScript 配置通过 |
| `npm run build` | Vite 6.4.3，924 个模块构建完成 |

显式联网冒烟入口为：

```powershell
python -m pytest -q -p no:cacheprovider -m live tests/test_live_network_smoke.py
```

默认 pytest 通过 `-m 'not live'` 排除该用例，实时网络失败不会改写离线测试结论。

Git 恢复基线只保存受版本控制的源码、配置、brief 和治理记录。`tests/test_research_version_inventory.py` 与 `tests/test_retrospective_task_briefs.py` 中 4 项输出证据集成检查仅在对应 ignored outputs 存在时执行；恢复后的显式库存、brief 与治理覆盖审计不会因此降级，证据缺失时仍返回失败。

## Dashboard 运行态复核

- 真实数据经过同一 `parseDashboardData` 运行时解析器后正常渲染。
- 1440 像素桌面视口和 390 像素移动视口均无页面级横向溢出，无控制台错误或未处理异常。
- 移动端宽表保留内部横向滚动；页面本身不被撑宽。
- 注入未知 `schema_version` 后，页面进入明确“数据读取失败”状态，没有静默回退成投资建议。
- 截图保存在 `outputs/test/current_mainline_behavior/debug/dashboard_desktop_1440.png`、`dashboard_mobile_390.png` 和 `dashboard_contract_error.png`。

## 研究与执行边界

本次整改只修复工程行为和证据表达：

- 网络刷新失败或同日劣化时保留最近合格 PIT 快照；
- as-of 查询同时约束 `snapshot_date` 与 `available_date`；
- ETF 生命周期同时约束直接映射与行业暴露映射；
- 重复持仓先汇总再计算组合风险；
- 建议后风险重新进入硬门禁；
- 前推日期必须满足 `signal_date < entry_date < exit_date`；
- 成交回放在无退出行情时返回稳定失败状态；
- 研究编排器的 `auto_execution_allowed` 恒为 `false`。

仍未满足的 6 项建议就绪门禁保持原样：授权历史实施数据、择时稳健性、强行业选择证据、研究家族多重检验、真实账户状态、六角色确定性否决链全通过。因此，本报告证明的是“环境可恢复、代码行为可回归”，不证明策略有效，也不构成买卖建议。
