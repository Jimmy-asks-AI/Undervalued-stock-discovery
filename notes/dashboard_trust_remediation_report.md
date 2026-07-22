# Dashboard 可信度整改报告

日期：2026-07-18

范围：目标 6——只整改展示、数据合同、移动端信息完整性、刷新语义与发布便携性；不改策略、阈值、门禁或研究结论。

## 结论

本轮整改通过验收。

Dashboard 当前机器状态与 `CURRENT_STATUS`、当前状态一致性审计、当前 runner 完全对齐：`research_only / NO_ACTION`。状态有效、一致性通过、active cohort 一致；人工辅助交易未就绪，生产未就绪，自动执行关闭。

这不是策略有效性升级。页面现在能更准确地说明“数据到哪一天、哪些源有缺口、为什么不能行动”，也能在合同异常时失败关闭；它没有解除任何研究或交易门禁。

| 项目 | 当前值 |
|---|---|
| Dashboard 生成时间 | `2026-07-18T22:37:42+08:00`（最终命令验收所用产物） |
| 决策 as-of | `2026-07-18` |
| 当前动作 | `NO_ACTION` |
| 研究状态 | `research_only` |
| 状态有效 / 状态一致 / cohort 一致 | `true / true / true` |
| active cohort | `ff_integrity_v7_20260718` |
| cohort manifest | `966e40a07d2248d8447692e85faf3d28d4ffaee51b2db8b0a787a861db0bf7e2` |
| 人工辅助 / 生产 / 自动执行 | `false / false / false` |
| 数据源 / 质量提示 / 门禁 | `11 / 6 / 13` |
| V2.4 候选估值日 | `2026-07-11`，早于当前估值源截止 `2026-07-16`，页面明确标记为 stale |

## 整改结果

| 要求 | 落地结果 |
|---|---|
| 首屏可信度 | 首屏展示带时区的 `generated_at`、决策 as-of、总体状态、当前动作、cohort pair、一致性、11 个源的 cutoff/lag/status、6 条结构化质量提示和门禁概况。1440×1000 首屏可完整看到这些信息。 |
| 刷新语义 | 页面按钮改为“重新读取本地结果”，只做 `cache: no-store` 的摘要 fetch；页面同时分别展示本地重建命令、联网刷新命令及 dev/preview 端口。 |
| 390px 信息完整性 | 移动端使用估值卡保留估值日期、PE、PB、股息率；当前动作、关键门禁、warnings 和逐源 cutoff/lag 均未隐藏。 |
| 固定研究边界 | 五条声明进入文档流页脚：不构成投资建议、历史回放不代表未来、数据可能延迟、人工辅助交易未就绪、自动执行关闭。声明不再覆盖正文。 |
| 可移植证据 | `evidence_catalog` 只保存稳定 `evidence_id` 和仓库相对 POSIX 路径；本地生成证据统一 `linkable=false`。摘要与明细中的 Windows 绝对路径扫描均为 0。 |
| Vite 与端口 | `base="./"`；dev 固定 `5173`、preview 固定 `4175`，二者均 `strictPort=true`；README 与页面文案一致。 |
| 首载拆分 | 原 11,223,448 bytes 单文件改为 38,344 bytes 摘要和 841,907 bytes 历史明细；首载体积下降约 99.7%。K 线、历史标记和成交表按需加载。 |
| 明细完整性 | 加载前校验同源相对 URL、SHA-256、字节数、schema、决策日期和三项计数；任一不符即停止展示并进入 error/stale 状态。 |
| 合同与反例 | 运行时合同覆盖未知 schema、显式时区、绝对路径、evidence 引用、stale/missing/degraded/superseded、warning、cohort mismatch、NO_ACTION 混入 BUY、空 BUY、blocked gate、自动执行等失败关闭条件。 |
| 状态与视觉 QA | 覆盖 loading、empty、error、stale、warning、NO_ACTION、contract error、长文本、键盘焦点、历史明细已加载、1440px 与 390px。 |

## BUY 失败关闭边界

当前页面和合同共用以下硬条件；缺一项都不能展示当前 BUY 候选：

1. 总动作必须是 `BUY_CANDIDATE`，且至少有一条对应候选。
2. recommendation 动作必须与 trust summary 完全一致。
3. 所有决策门禁必须 `passed=true / status=pass / veto=false`。
4. 状态必须有效、一致，cohort ID 与 manifest hash 必须一致。
5. 人工辅助和 production readiness 必须同时为 true。
6. 所有源只能是 `fresh` 或受控的 `historical_archive`；stale、missing、degraded、blocked、superseded 均阻断。
7. 不得存在 warning/error 级数据质量提示。
8. `auto_execution_allowed` 在任意层级都只能为 false。

当前真实数据不满足上述条件，因此页面只显示“可信度门禁阻断 / 保持观望”，不显示当前买入建议。

## 数据合同与产物

### 摘要 `dashboard-data-v2`

摘要包含：

- `generated_at`、`decision_as_of_date`
- `trust_summary`
- `source_freshness`
- 结构化 `data_quality_warnings`
- current recommendation、13 项 gate results
- V2.4 估值快照与候选、主要指数状态
- detail manifest、三种刷新语义、五条固定声明
- evidence catalog

### 明细 `dashboard-details-v1`

明细包含：57 条历史 ETF 回放、2,802 根上证 K 线、114 个买卖标记及历史汇总。最终 manifest 声明的 SHA-256、字节数和现场文件一致；精确值以每次重建后的 `dashboard_data.json` 为准。

`dashboard_data.json` 与 `dashboard_details.json` 都是本地生成文件，已纳入 `.gitignore`。它们不被当作公开 URL 或仓库永久证据。

## 验收记录

### 命令矩阵

最终命令验收状态为 `pass`，9/9 通过：

| 命令 | 结果 |
|---|---|
| `python scripts/build_dashboard_dataset.py --self-check` | PASS |
| `python scripts/build_dashboard_dataset.py` | PASS |
| `npm ci --no-audit --no-fund` | PASS |
| `npm run validate:data` | PASS |
| `npm test` | PASS：17 个数据合同测试 + 5 个 UI 静态合同测试 |
| `npm run check` | PASS |
| `npm run build` | PASS：924 modules transformed |
| `python -m uv lock --check` | PASS |
| `python scripts/audit_dashboard_trust_ui.py --self-check` | PASS |

原始命令、退出码、用时和完整输出保存在：

- [命令验收 Markdown](../outputs/audit/dashboard_trust_remediation/debug/acceptance_commands.md)
- [命令验收 JSON](../outputs/audit/dashboard_trust_remediation/debug/acceptance_commands.json)

命令验收另做了 11 项机器对齐检查：decision as-of、动作、policy、人工辅助、production、自动执行、cohort ID、cohort hash、状态有效性、状态一致性和 cohort 一致性全部通过。

### 浏览器矩阵

最终 UI QA：`16/16 PASS`。

| 场景 | 1440px | 390px |
|---|---|---|
| normal | PASS | PASS |
| history loaded | PASS | PASS |
| stale | PASS | PASS |
| warning | PASS | PASS |
| contract error | PASS | PASS |
| empty candidates | PASS | PASS |
| NO_ACTION | PASS | PASS |
| long text + focus | PASS | PASS |

所有场景均满足：无页面级横向溢出、无 console/page error、无 failed request、无当前 BUY 文案。390px 正常/降级状态保留估值日期、PE、PB、股息率、当前动作和关键门禁。历史明细加载态保留 K 线和成交表。固定声明的 computed position 为 `static`，`overlaysContent=false`。

证据路径：

- [UI QA 报告](../outputs/audit/dashboard_trust_remediation/debug/ui_qa_report.md)
- [UI QA JSON](../outputs/audit/dashboard_trust_remediation/debug/ui_qa_results.json)
- [截图目录](../outputs/audit/dashboard_trust_remediation/debug/)

截图文件按 `{scenario}__{viewport}.png` 命名，例如：

- `normal__desktop_1440.png`、`normal__mobile_390.png`
- `history_loaded__desktop_1440.png`、`history_loaded__mobile_390.png`
- `stale__desktop_1440.png`、`stale__mobile_390.png`
- `warning__desktop_1440.png`、`warning__mobile_390.png`
- `contract_error__desktop_1440.png`、`contract_error__mobile_390.png`
- `empty_candidates__desktop_1440.png`、`empty_candidates__mobile_390.png`
- `no_action__desktop_1440.png`、`no_action__mobile_390.png`
- `long_text_focus__desktop_1440.png`、`long_text_focus__mobile_390.png`

## 复现方式

```powershell
python -m uv sync --frozen --group test --group verify
python .\scripts\audit_dashboard_trust_acceptance.py

Push-Location .\strategy_lab\research_dashboard
npm run preview
# 另开一个终端：npm run audit:ui
Pop-Location
```

UI QA 使用 verify 组锁定的 `playwright==1.60.0` 和系统 Chrome，不需要下载 Playwright 自带浏览器。preview 必须先在 `127.0.0.1:4175` 启动。

## 仍然存在的研究边界

- V2.4 候选估值日仍是 `2026-07-11`，比当前估值源截止 `2026-07-16` 旧；整改只把差异显式展示，没有替换研究数据。
- `pit_valuation_methodology` 与账户状态仍为 blocked；fund flow 为可选陈旧源；行业候选证据为空。
- 当前硬门禁仍有 10 项阻断；人工辅助交易与 production readiness 均为 false。
- 历史回放不代表未来表现，K 线与成交标记只用于研究回看。
- 页面按钮不会联网刷新研究数据。真正刷新必须由操作者在终端显式运行受控命令，并在完成后重建 Dashboard。

因此，整改后的正确结论仍是：研究工作台可以更可信地展示“为何不能行动”，但不能据此给出或执行买卖指令。
