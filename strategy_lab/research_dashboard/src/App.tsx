import { useCallback, useEffect, useRef, useState } from "react";
import {
  Button,
  InlineNotification,
  Select,
  SelectItem,
  SkeletonText,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tag,
} from "@carbon/react";
import { Checkmark, Close, DataBase, Renew, Security, Time, Wallet, WarningAlt } from "@carbon/icons-react";
import type { AnyRecord, DashboardDetails } from "./types";
import { parseDashboardData, parseDashboardDetails, sha256Hex } from "./dashboardDataContract";
import { ShanghaiCandlestickChart } from "./ShanghaiCandlestickChart";

const DATA_URL = `${import.meta.env.BASE_URL}data/dashboard_data.json`;

const REQUIRED_NOTICES = [
  "不构成投资建议",
  "历史回放不代表未来",
  "数据可能延迟",
  "人工辅助交易未就绪",
  "自动执行关闭",
];

type LoadState = "loading" | "ready" | "reloading" | "error";
type DetailState = "idle" | "loading" | "ready" | "empty" | "stale" | "error";

type DashboardViewData = {
  schema_version: string;
  generated_at: string;
  decision_as_of_date: string;
  trust_summary: AnyRecord;
  source_freshness: AnyRecord[];
  data_quality_warnings: Array<string | AnyRecord>;
  current_recommendation?: AnyRecord;
  gate_results: AnyRecord[];
  valuation_snapshot: AnyRecord;
  market_index_states?: AnyRecord[];
  detail_manifest: AnyRecord;
  refresh_semantics: AnyRecord;
  fixed_notices: Array<string | AnyRecord>;
  evidence_catalog?: AnyRecord[];
  candidate_state?: string;
  top_candidates?: Record<string, AnyRecord[]>;
  summaries?: Record<string, AnyRecord>;
};

const blockerText: Record<string, string> = {
  timing_robustness: "反弹窗口尚未通过稳健性验证",
  industry_selection: "强反弹行业规则尚未验证",
  account_state: "未配置真实账户与持仓",
  portfolio_risk: "无法计算真实账户风险",
  projected_portfolio_risk: "建议仓位加入后将突破组合风险约束",
  goal_evidence: "历史证据未达到建议级别",
  agent_veto_chain: "至少一个研究门禁未通过",
  data_freshness: "关键数据已过期",
  etf_pit_master: "ETF 可交易性数据不完整",
  pit_universe_methodology: "PIT 估值与行业历史口径门禁未通过",
};

const sourceStatusText: Record<string, string> = {
  fresh: "新鲜",
  current: "当前",
  historical_archive: "历史档案",
  blocked: "已阻断",
  missing_optional: "可选源缺失",
  stale_optional: "可选源陈旧",
  stale: "陈旧",
  superseded: "已被替代",
  degraded: "降级",
};

function record(value: unknown): AnyRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as AnyRecord : {};
}

function records(value: unknown): AnyRecord[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === "object" && item !== null && !Array.isArray(item)) as AnyRecord[] : [];
}

function text(value: unknown, fallback = "-"): string {
  return typeof value === "string" && value.trim() ? value.trim() : value === null || value === undefined ? fallback : String(value);
}

function percent(value: unknown, digits = 1): string {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "string" && value.includes("%")) return value;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "-";
  const normalized = Math.abs(parsed) > 1.5 ? parsed : parsed * 100;
  return `${normalized.toFixed(digits)}%`;
}

function number(value: unknown, digits = 2): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "-";
}

function formatTimestamp(value: unknown): string {
  const raw = text(value);
  const parsed = Date.parse(raw);
  if (raw === "-" || Number.isNaN(parsed)) return raw;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(parsed));
}

function booleanState(value: unknown): boolean {
  return value === true || value === "pass" || value === "consistent";
}

function statusIsBlocking(status: unknown, required: unknown): boolean {
  const normalized = text(status, "unknown").toLowerCase();
  if (normalized.includes("stale") || normalized === "blocked" || normalized === "superseded" || normalized === "degraded") return true;
  return required === true && normalized.includes("missing");
}

function statusTone(status: unknown): "ok" | "warn" | "danger" | "neutral" {
  const normalized = text(status, "unknown").toLowerCase();
  if (["fresh", "current", "pass"].includes(normalized)) return "ok";
  if (normalized === "blocked" || normalized === "superseded" || normalized === "error") return "danger";
  if (normalized.includes("stale") || normalized.includes("missing") || normalized === "degraded") return "warn";
  return "neutral";
}

function warningText(value: string | AnyRecord): string {
  return typeof value === "string" ? value : text(value.message ?? value.detail ?? value.code, "未命名数据警告");
}

function warningSeverity(value: string | AnyRecord): string {
  return typeof value === "string" ? "warning" : text(value.severity, "warning").toLowerCase();
}

function normalizeNotices(value: Array<string | AnyRecord> | undefined): string[] {
  const supplied = Array.isArray(value)
    ? value.map((item) => typeof item === "string" ? item : text(item.text ?? item.message, "")).filter(Boolean)
    : [];
  return supplied.length ? supplied : REQUIRED_NOTICES;
}

function portableDataUrl(relativePath: string): string {
  if (!relativePath || /^[a-zA-Z]:[\\/]/.test(relativePath) || /^[a-zA-Z][a-zA-Z\d+.-]*:/.test(relativePath)) {
    throw new Error("历史明细路径不是可移植的同源相对路径");
  }
  const base = new URL(import.meta.env.BASE_URL, window.location.href);
  const resolved = new URL(relativePath, base);
  if (resolved.origin !== window.location.origin) throw new Error("历史明细路径越过当前站点边界");
  return resolved.href;
}

export function App() {
  const [data, setData] = useState<DashboardViewData | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const summaryAbortRef = useRef<AbortController | null>(null);

  const loadSummary = useCallback(async (manual = false) => {
    summaryAbortRef.current?.abort();
    const controller = new AbortController();
    summaryAbortRef.current = controller;
    setLoadState(manual ? "reloading" : "loading");
    setError("");
    try {
      const separator = DATA_URL.includes("?") ? "&" : "?";
      const response = await fetch(`${DATA_URL}${separator}read=${Date.now()}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`本地结果返回 HTTP ${response.status}`);
      const raw = await response.text();
      const parsed = parseDashboardData(JSON.parse(raw)) as unknown as DashboardViewData;
      setData(parsed);
      setLoadState("ready");
    } catch (reason) {
      if (controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : String(reason));
      setLoadState("error");
    }
  }, []);

  useEffect(() => {
    void loadSummary(false);
    return () => summaryAbortRef.current?.abort();
  }, [loadSummary]);

  const notices = normalizeNotices(data?.fixed_notices);
  return (
    <div className="app-shell">
      {data ? (
        <DecisionDashboard
          data={data}
          loadError={error}
          loadState={loadState}
          onReload={() => void loadSummary(true)}
        />
      ) : loadState === "error" ? (
        <StateScreen
          title="数据契约或文件读取失败"
          detail={error}
          error
          action={<Button data-testid="reload-local-data" kind="primary" renderIcon={Renew} onClick={() => void loadSummary(true)}>重新读取本地结果</Button>}
        />
      ) : (
        <StateScreen title="正在读取本地研究结果" detail="只读取已生成的摘要文件，不会联网刷新研究数据。" />
      )}
      <FixedNotices notices={notices} />
    </div>
  );
}

function DecisionDashboard({ data, loadError, loadState, onReload }: { data: DashboardViewData; loadError: string; loadState: LoadState; onReload: () => void }) {
  const [details, setDetails] = useState<DashboardDetails | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  const [detailError, setDetailError] = useState("");
  const [historyLimit, setHistoryLimit] = useState("10");
  const detailsAbortRef = useRef<AbortController | null>(null);

  const trust = record(data.trust_summary);
  const current = record(data.current_recommendation);
  const manifest = record(data.detail_manifest);
  const refresh = record(data.refresh_semantics);
  const sources = records(data.source_freshness);
  const gates = records(data.gate_results);
  const valuation = record(data.valuation_snapshot);
  const legacyValuation = record(data.summaries?.v2_4);
  const valuationDate = text(valuation.snapshot_date ?? valuation.date ?? valuation.valuation_snapshot_date ?? legacyValuation.valuation_snapshot_date, "未知");
  const valuationCandidates = data.candidate_state === "empty"
    ? []
    : records(valuation.candidates ?? valuation.rows ?? data.top_candidates?.v2_4).slice(0, 8);
  const indexStates = records(data.market_index_states);
  const warnings = Array.isArray(data.data_quality_warnings) ? data.data_quality_warnings : [];
  const blockers = Array.isArray(current.risk_vetoes) ? current.risk_vetoes.map(String) : [];
  const actionCode = text(current.action ?? trust.current_action ?? trust.action, "BLOCKED_DATA");
  const cohortConsistent = booleanState(trust.cohort_consistent ?? trust.cohort_consistency);
  const statusValid = trust.status_valid !== false;
  const stateConsistent = trust.state_consistent !== false;
  const sourceBlocked = sources.some((source) => statusIsBlocking(source.status, source.required));
  const blockingWarning = warnings.some((warning) => ["warning", "error"].includes(warningSeverity(warning)));
  const safeCandidateRows = records(current.candidates).filter((row) => row.action === "BUY_CANDIDATE");
  const allGatesPass = gates.length > 0 && gates.every((gate) => gate.passed === true && gate.status === "pass" && gate.veto === false);
  const canShowActionCandidate = actionCode === "BUY_CANDIDATE"
    && cohortConsistent
    && statusValid
    && stateConsistent
    && !sourceBlocked
    && !blockingWarning
    && allGatesPass
    && trust.manual_support_ready === true
    && trust.production_ready === true
    && safeCandidateRows.length > 0;
  const blockedGateCount = gates.filter((gate) => gate.passed !== true || gate.status === "blocked" || gate.veto === true).length;

  useEffect(() => {
    detailsAbortRef.current?.abort();
    setDetails(null);
    setDetailState("idle");
    setDetailError("");
  }, [data.generated_at]);

  useEffect(() => () => detailsAbortRef.current?.abort(), []);

  const loadDetails = async () => {
    detailsAbortRef.current?.abort();
    const controller = new AbortController();
    detailsAbortRef.current = controller;
    setDetailState("loading");
    setDetailError("");
    try {
      const expectedSchema = text(manifest.schema_version, "dashboard-details-v1");
      const expectedHash = text(manifest.sha256, "").toLowerCase();
      const expectedBytes = Number(manifest.bytes);
      if (!/^[a-f\d]{64}$/.test(expectedHash)) throw new Error("历史明细 manifest 缺少有效 SHA-256");
      const response = await fetch(portableDataUrl(text(manifest.url, "")), { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(`历史明细返回 HTTP ${response.status}`);
      const raw = await response.text();
      const actualHash = await sha256Hex(raw);
      const actualBytes = new TextEncoder().encode(raw).byteLength;
      if (actualHash !== expectedHash) throw new Error("历史明细 SHA-256 与摘要 manifest 不一致");
      if (Number.isFinite(expectedBytes) && expectedBytes >= 0 && actualBytes !== expectedBytes) {
        throw new Error(`历史明细字节数不一致：期望 ${expectedBytes}，实际 ${actualBytes}`);
      }
      const parsed = parseDashboardDetails(JSON.parse(raw));
      if (parsed.schema_version !== expectedSchema || parsed.schema_version !== "dashboard-details-v1") {
        throw new Error(`历史明细 schema 不受支持：${text(parsed.schema_version)}`);
      }
      if (!parsed.generated_at || Number.isNaN(Date.parse(parsed.generated_at))) throw new Error("历史明细 generated_at 不是有效时间");
      if (!parsed.decision_as_of_date) throw new Error("历史明细缺少 decision_as_of_date");
      if (parsed.decision_as_of_date !== data.decision_as_of_date) {
        setDetailState("stale");
        setDetailError(`历史明细决策日期 ${parsed.decision_as_of_date} 与摘要 ${data.decision_as_of_date} 不一致，已停止展示。`);
        return;
      }
      const rows = records(parsed.historical_etf_opportunities);
      const candles = records(parsed.shanghai_index_candles);
      const markers = records(parsed.shanghai_index_trade_markers);
      const expectedCounts = record(manifest.counts);
      for (const [key, actual] of [
        ["historical_etf_opportunities", rows.length],
        ["shanghai_index_candles", candles.length],
        ["shanghai_index_trade_markers", markers.length],
      ] as const) {
        const expected = Number(expectedCounts[key]);
        if (Number.isFinite(expected) && expected !== actual) throw new Error(`历史明细 ${key} 计数与 manifest 不一致`);
      }
      setDetails(parsed);
      setDetailState(rows.length || candles.length || markers.length ? "ready" : "empty");
    } catch (reason) {
      if (controller.signal.aborted) return;
      setDetailError(reason instanceof Error ? reason.message : String(reason));
      setDetailState("error");
    }
  };

  const action = actionPresentation(actionCode, canShowActionCandidate, sourceBlocked || !cohortConsistent || !statusValid || !stateConsistent);
  const localCommand = text(refresh.rebuild_command ?? refresh.local_rebuild_command, "python .\\scripts\\build_dashboard_dataset.py");
  const onlineCommand = text(refresh.network_refresh_command ?? refresh.online_refresh_command ?? refresh.full_refresh_command, "python .\\scripts\\run_etf_assisted_trading_current.py --refresh-inputs");
  const historyRows = records(details?.historical_etf_opportunities);
  const opportunityRows = historyLimit === "all" ? historyRows : historyRows.slice(0, Number(historyLimit));
  const historicalSummary = record(details?.historical_opportunity_summary);
  const candles = records(details?.shanghai_index_candles);
  const markers = records(details?.shanghai_index_trade_markers);

  return (
    <div className="decision-app">
      <header className="app-header">
        <div>
          <span className="product-kicker">A股行业与指数 ETF · 本地研究工作台</span>
          <h1>低估资产研究状态</h1>
        </div>
        <div className="header-meta"><DataBase size={16} aria-hidden="true" /> 摘要 schema {data.schema_version}</div>
      </header>

      <main>
        <section className="trust-panel" data-testid="trust-summary" aria-labelledby="trust-title">
          <div className="trust-hero">
            <div>
              <span className="eyebrow">先看数据与门禁，再看市场结果</span>
              <h2 id="trust-title">研究可信度摘要</h2>
            </div>
            <div className="trust-state-group">
              <Tag size="md" type="cool-gray">{text(trust.research_state ?? trust.overall_research_status, "unknown")}</Tag>
              <Tag size="md" type={warnings.some((warning) => warningSeverity(warning) === "error") ? "red" : warnings.length ? "warm-gray" : "green"}>{warnings.length} 条质量提示</Tag>
              <div className="current-action-code"><span>当前动作</span><strong>{actionCode}</strong></div>
            </div>
          </div>

          <div className="trust-meta-grid">
            <TrustMetric label="摘要生成时间" value={formatTimestamp(data.generated_at)} detail="页面读取的文件生成时间" />
            <TrustMetric label="决策 as-of" value={text(data.decision_as_of_date ?? trust.decision_as_of_date)} detail="本次门禁判断日期" />
            <TrustMetric label="总体研究状态" value={text(trust.research_state ?? trust.overall_research_status)} detail={text(trust.policy_status, "仅供研究")} />
            <TrustMetric label="关键门禁" value={blockedGateCount ? `${blockedGateCount} 项阻断` : "未见阻断"} detail={`${gates.length} 项门禁已载入`} tone={blockedGateCount ? "danger" : "ok"} />
            <TrustMetric label="Cohort 一致性" value={cohortConsistent ? "一致" : "不一致"} detail={text(trust.active_cohort_id, "未提供 cohort")} tone={cohortConsistent ? "ok" : "danger"} />
            <TrustMetric label="状态交叉校验" value={statusValid && stateConsistent ? "通过" : "未通过"} detail="CURRENT_STATUS / state / runner" tone={statusValid && stateConsistent ? "ok" : "danger"} />
            <TrustMetric label="人工辅助交易" value={trust.manual_support_ready === true ? "已就绪" : "未就绪"} detail="需要真实账户与人工确认" tone={trust.manual_support_ready === true ? "ok" : "warn"} />
            <TrustMetric label="自动执行" value={trust.auto_execution_allowed === false ? "关闭" : "异常"} detail="页面不提供下单入口" tone={trust.auto_execution_allowed === false ? "neutral" : "danger"} />
          </div>

          <div className="cohort-evidence wrap-anywhere">
            <span>活动 cohort：<code>{text(trust.active_cohort_id)}</code></span>
            <span>状态 cohort：<code>{text(trust.current_status_cohort_id)}</code></span>
            {!cohortConsistent && <strong>cohort ID 或 manifest hash 不一致，当前动作已强制阻断。</strong>}
          </div>

          <div className="trust-subheading"><div><span>逐源时点</span><h3>数据截止与滞后</h3></div><small>所有摘要源均列出；陈旧、缺失、降级和 superseded 不会静默。</small></div>
          <div className="source-grid" data-testid="source-freshness" aria-label="数据源新鲜度">
            {sources.map((source, index) => {
              const status = text(source.status, "unknown");
              const tone = statusTone(status);
              return (
                <article className={`source-card ${tone}`} key={`${text(source.source_id ?? source.source, "source")}-${index}`}>
                  <div className="source-card-head"><strong>{text(source.source ?? source.source_id)}</strong><span>{sourceStatusText[status] ?? status}</span></div>
                  <dl>
                    <div><dt>cutoff</dt><dd>{text(source.cutoff_date ?? source.data_cutoff, "缺失")}</dd></div>
                    <div><dt>滞后</dt><dd>{source.lag_days === null || source.lag_days === undefined ? "未知" : `${number(source.lag_days, 0)} 天`}</dd></div>
                  </dl>
                  <p className="wrap-anywhere">{text(source.detail, source.required === true ? "必需数据源" : "可选数据源")}</p>
                  <small className="evidence-id wrap-anywhere">{text(source.evidence_id, "无 evidence_id")}</small>
                </article>
              );
            })}
            {!sources.length && <div className="empty-state danger-copy">未提供任何源级 cutoff；当前结果不能解释为数据已就绪。</div>}
          </div>

          <div className="warning-area" aria-live="polite">
            <div className="warning-title"><WarningAlt size={18} aria-hidden="true" /><strong>数据质量提示</strong><span>{warnings.length} 条</span></div>
            {warnings.length ? (
              <ul className="warning-list">
                {warnings.map((warning, index) => (
                  <li className={`warning-item severity-${warningSeverity(warning)}`} key={`${typeof warning === "string" ? "warning" : text(warning.code, "warning")}-${index}`}>
                    <span>{warningSeverity(warning)}</span>
                    <p className="wrap-anywhere">{warningText(warning)}</p>
                    {typeof warning !== "string" && warning.evidence_id ? <code className="wrap-anywhere">{text(warning.evidence_id)}</code> : null}
                  </li>
                ))}
              </ul>
            ) : <p className="no-warning">摘要没有报告数据质量警告；仍应结合逐源 cutoff 与门禁判断。</p>}
          </div>
        </section>

        <section className="refresh-panel" aria-labelledby="refresh-title">
          <div className="refresh-copy">
            <span className="eyebrow">三个动作，三种边界</span>
            <h2 id="refresh-title">重新读取、重建摘要与联网刷新彼此独立</h2>
            <p>{text(refresh.local_reload_note, "网页按钮只会重新请求当前目录中已生成的摘要 JSON，不会运行 Python、不会访问行情源，也不会改变研究结论。")}</p>
            <Button data-testid="reload-local-data" kind="primary" size="md" renderIcon={Renew} disabled={loadState === "reloading"} onClick={onReload}>
              重新读取本地结果
            </Button>
            <span className="reload-state" aria-live="polite">{loadState === "reloading" ? "正在重新读取本地摘要……" : loadError ? `重新读取失败：${loadError}` : "当前摘要已读取"}</span>
          </div>
          <div className="command-grid">
            <div><strong>本地重建摘要</strong><p>复用已有研究产物，只生成 Dashboard 摘要与明细。</p><code>{localCommand}</code></div>
            <div><strong>联网刷新后重建</strong><p>{text(refresh.network_refresh_note, "先刷新受控输入链，再重新计算研究结果；需在终端显式执行。")}</p><code>{onlineCommand}</code></div>
            <div className="port-note"><strong>服务端口</strong><p><code>dev={text(refresh.dev_port, "5173")}</code><code>preview={text(refresh.preview_port, "4175")}</code></p></div>
          </div>
        </section>

        <section className="decision-grid" aria-label="当前动作与门禁">
          <article className={`decision-panel action-panel ${action.tone}`}>
            <div className="panel-label"><Wallet size={20} aria-hidden="true" /> 当前动作</div>
            <div className="action-status"><Security size={32} aria-hidden="true" /><span>{action.title}</span></div>
            <p>{action.detail}</p>
            <code className="action-raw">{actionCode}</code>
            {canShowActionCandidate ? (
              <div className="review-list">
                {safeCandidateRows.map((row, index) => <ReviewRow key={`${text(row.etf_code)}-${index}`} row={row} />)}
              </div>
            ) : (
              <InlineNotification className="no-action" kind={sourceBlocked || !cohortConsistent ? "warning" : "info"} lowContrast hideCloseButton title="当前无可执行动作" subtitle="保持观望；只有数据、研究门禁、账户和人工确认全部满足后，才进入人工复核。" />
            )}
            <small>人工辅助交易未就绪；自动执行关闭。</small>
          </article>

          <section className="gate-panel" aria-labelledby="gate-title">
            <div className="section-heading"><div><span>逐项失败关闭</span><h2 id="gate-title">关键门禁</h2></div><Time size={24} aria-hidden="true" /></div>
            <div className="gate-list">
              {gates.map((gate, index) => {
                const passed = gate.passed === true && gate.status !== "blocked" && gate.veto !== true;
                return (
                  <article className={`gate-item ${passed ? "pass" : "fail"}`} key={`${text(gate.gate_id, "gate")}-${index}`}>
                    <span className="gate-icon" aria-hidden="true">{passed ? <Checkmark size={16} /> : <Close size={16} />}</span>
                    <div><strong>{text(gate.label ?? gate.gate_id)}</strong><small>{passed ? "通过" : "阻断"} · {text(gate.reason, passed ? "证据满足" : "证据不足")}</small></div>
                  </article>
                );
              })}
              {!gates.length && <div className="empty-state danger-copy">关键门禁为空；当前动作已按阻断处理。</div>}
            </div>
            {blockers.length ? <div className="blocker-list"><strong>当前 veto</strong>{blockers.map((item) => <span className="wrap-anywhere" key={item}>{blockerText[item] ?? item}</span>)}</div> : null}
          </section>
        </section>

        <section className="section-block index-section" aria-label="A股主要指数状态">
          <div className="section-heading"><div><span>点位与动量</span><h2>A股主要指数状态</h2></div><span className="method-note">3年点位分位 + 14日 RSI</span></div>
          <div className="index-grid">{indexStates.map((row, index) => <IndexState key={`${text(row.symbol)}-${index}`} row={row} />)}</div>
          {!indexStates.length && <div className="empty-state">当前没有可展示的指数状态。</div>}
          <p className="section-footnote">点位分位是历史价格位置代理，不等同于 PE/PB 基本面估值，也不构成当前交易信号。</p>
        </section>

        <section className="section-block valuation-section" aria-labelledby="valuation-title">
          <div className="section-heading"><div><span>只用于观察</span><h2 id="valuation-title">当前相对低估行业</h2></div><span className="snapshot-date">估值日期 {valuationDate}</span></div>
          <div className="table-wrap desktop-valuation">
            <Table className="value-table" size="md" useZebraStyles={false} aria-label={`估值日期 ${valuationDate} 的相对低估行业`}>
              <TableHead><TableRow><TableHeader>行业</TableHeader><TableHeader>判断</TableHeader><TableHeader>估值分</TableHeader><TableHeader>超跌分</TableHeader><TableHeader>PE</TableHeader><TableHeader>PB</TableHeader><TableHeader>股息率</TableHeader></TableRow></TableHead>
              <TableBody>{valuationCandidates.map((row, index) => <ValueRow row={row} key={`${text(row.industry_code ?? row["行业代码"])}-${index}`} />)}</TableBody>
            </Table>
          </div>
          <div className="valuation-mobile" data-testid="valuation-mobile">
            <div className="valuation-mobile-meta"><strong>估值日期 {valuationDate}</strong><span>字段：PE / PB / 股息率</span></div>
            {valuationCandidates.map((row, index) => <MobileValueCard row={row} key={`${text(row.industry_code ?? row["行业代码"])}-${index}`} />)}
            {!valuationCandidates.length && <div className="empty-state">暂无估值候选；PE、PB 与股息率均无可展示值。</div>}
          </div>
          {!valuationCandidates.length && <div className="empty-state desktop-empty">暂无估值候选。空候选不会升级为当前动作。</div>}
        </section>

        <section className="section-block history-section" data-testid="history-section" aria-labelledby="history-title">
          <div className="section-heading history-heading">
            <div><span>摘要首载不含大体量历史序列</span><h2 id="history-title">历史 K 线与交易明细</h2></div>
            {detailState === "ready" ? (
              <Select id="history-limit" labelText="显示历史机会数量" hideLabel size="sm" value={historyLimit} onChange={(event) => setHistoryLimit(event.target.value)}>
                <SelectItem value="10" text="最近10次" /><SelectItem value="20" text="最近20次" /><SelectItem value="all" text="全部" />
              </Select>
            ) : null}
          </div>

          {detailState === "idle" && (
            <div className="history-idle">
              <div><strong>历史明细尚未加载</strong><p>点击后读取同源相对路径，先核对字节数与 SHA-256，再解析 schema 和决策日期。</p><small>{formatManifestCounts(record(manifest.counts))}</small></div>
              <Button data-testid="history-load" kind="secondary" renderIcon={DataBase} onClick={() => void loadDetails()}>加载并校验历史明细</Button>
            </div>
          )}
          {detailState === "loading" && <div className="history-state" role="status" aria-live="polite"><SkeletonText heading width="14rem" /><p>正在下载并校验历史明细，当前动作区不受影响。</p></div>}
          {(detailState === "error" || detailState === "stale") && <div className="history-state" role="alert"><WarningAlt size={24} aria-hidden="true" /><strong>{detailState === "stale" ? "历史明细已陈旧" : "历史明细读取失败"}</strong><p className="wrap-anywhere">{detailError}</p><Button kind="tertiary" onClick={() => void loadDetails()}>重试校验</Button></div>}
          {detailState === "empty" && <div className="history-state"><strong>历史明细为空</strong><p>manifest 校验通过，但没有 K 线、交易标记或历史成交记录。</p></div>}
          {detailState === "ready" && details ? (
            <div className="history-loaded">
              <div className="details-verified"><Checkmark size={16} aria-hidden="true" /> SHA-256、schema、字节数、计数和决策日期均已校验</div>
              {candles.length ? <ShanghaiCandlestickChart candles={candles} markers={markers} /> : <div className="empty-state">历史明细没有 K 线数据。</div>}
              <div className="history-summary"><span>历史回放 {historyRows.length} 条</span><span>60日冷却后 {text(historicalSummary.independent_cluster_count_60d)} 个独立簇</span><span>包含重叠信号，不等于独立机会次数</span></div>
              <div className="table-wrap history-table-wrap">
                <Table className="history-table" size="md" useZebraStyles={false} aria-label="历史 ETF 回放记录">
                  <TableHead><TableRow><TableHeader>ETF</TableHeader><TableHeader>买入时点</TableHeader><TableHeader>卖出时点</TableHeader><TableHeader>成本后收益</TableHeader><TableHeader>持有</TableHeader><TableHeader>退出方式</TableHeader></TableRow></TableHead>
                  <TableBody>{opportunityRows.map((row, index) => <OpportunityRow row={row} key={`${text(row.entry_date)}-${index}`} />)}</TableBody>
                </Table>
              </div>
              {!historyRows.length && <div className="empty-state">当前没有历史 ETF 回放记录。</div>}
            </div>
          ) : null}
        </section>
      </main>
    </div>
  );
}

function actionPresentation(code: string, canShowCandidate: boolean, trustBlocked: boolean): { title: string; detail: string; tone: string } {
  if (canShowCandidate) return { title: "人工复核候选", detail: "数据与研究门禁通过后，仍需核对真实账户、价差、IOPV 和仓位。", tone: "review" };
  if (trustBlocked || code === "BLOCKED_DATA") return { title: "可信度门禁阻断 / 保持观望", detail: "数据时点、cohort 或状态证据不满足，页面不会展示可执行候选。", tone: "danger" };
  if (code === "WATCH" || code === "WATCH_NO_TRADEABLE_ETF" || code === "REVIEW_REQUIRED") return { title: "继续观察 / 无可执行动作", detail: "研究环境接近，但实施条件或人工复核证据还不完整。", tone: "watch" };
  return { title: "无可执行动作 / 保持观望", detail: "当前没有通过全部门禁的研究窗口。", tone: "neutral" };
}

function TrustMetric({ label, value, detail, tone = "neutral" }: { label: string; value: string; detail: string; tone?: string }) {
  return <div className={`trust-metric ${tone}`}><span>{label}</span><strong className="wrap-anywhere">{value}</strong><small className="wrap-anywhere">{detail}</small></div>;
}

function ValueRow({ row }: { row: AnyRecord }) {
  const status = text(row.status ?? row["状态"], "观察");
  return <TableRow>
    <TableCell><strong>{text(row.industry_name ?? row["行业"])}</strong><small>{text(row.parent_industry ?? row.parent_industry_name ?? row["上级行业"], "")}</small></TableCell>
    <TableCell><Tag size="sm" type="warm-gray">{status}</Tag></TableCell>
    <TableCell>{percent(row.valuation_score ?? row["估值分"], 0)}</TableCell><TableCell>{percent(row.oversold_score ?? row["超跌分"], 0)}</TableCell>
    <TableCell>{number(row.pe_ttm ?? row.PE_TTM)}</TableCell><TableCell>{number(row.pb ?? row.PB)}</TableCell><TableCell>{percent(row.dividend_yield ?? row["股息率"], 1)}</TableCell>
  </TableRow>;
}

function MobileValueCard({ row }: { row: AnyRecord }) {
  return <article className="mobile-value-card">
    <div><strong className="wrap-anywhere">{text(row.industry_name ?? row["行业"])}</strong><span className="wrap-anywhere">{text(row.status ?? row["状态"], "观察")}</span></div>
    <dl><div><dt>PE</dt><dd>{number(row.pe_ttm ?? row.PE_TTM)}</dd></div><div><dt>PB</dt><dd>{number(row.pb ?? row.PB)}</dd></div><div><dt>股息率</dt><dd>{percent(row.dividend_yield ?? row["股息率"], 1)}</dd></div></dl>
  </article>;
}

function ReviewRow({ row }: { row: AnyRecord }) {
  return <div className="review-row"><div><strong>{text(row.etf_name ?? row.etf_code)}</strong><small>{text(row.industry_name, "人工复核")}</small></div><span>{percent(row.target_model_weight)}</span></div>;
}

function IndexState({ row }: { row: AnyRecord }) {
  const pointStatus = text(row.point_status, "中性区");
  const momentumStatus = text(row.momentum_status, "中性");
  const tagType = (status: string) => status === "低估区" || status === "超卖" ? "green" : status === "高估区" || status === "超买" ? "red" : "cool-gray";
  return <article className="index-item">
    <div className="index-top"><div><strong>{text(row.name)}</strong><small>{text(row.symbol, "")}</small></div><span>{number(row.close, 2)}</span></div>
    <div className="index-tags"><Tag size="sm" type={tagType(pointStatus)}>{pointStatus}</Tag><Tag size="sm" type={tagType(momentumStatus)}>{momentumStatus}</Tag></div>
    <dl><div><dt>3年分位</dt><dd>{percent(row.price_percentile_3y, 0)}</dd></div><div><dt>RSI14</dt><dd>{number(row.rsi_14, 1)}</dd></div><div><dt>20日</dt><dd className={Number(row.return_20d) >= 0 ? "positive" : "negative"}>{percent(row.return_20d, 1)}</dd></div></dl>
    <small>截至 {text(row.trade_date)}</small>
  </article>;
}

function OpportunityRow({ row }: { row: AnyRecord }) {
  const positive = Number(row.net_return) >= 0;
  return <TableRow>
    <TableCell><strong>{text(row.etf_code)}</strong></TableCell>
    <TableCell><strong>{text(row.entry_date)}</strong><small>{number(row.entry_price, 3)} 元</small></TableCell>
    <TableCell><strong>{text(row.exit_date)}</strong><small>{number(row.exit_price, 3)} 元</small></TableCell>
    <TableCell><Tag size="sm" type={positive ? "green" : "red"}>{percent(row.net_return, 1)}</Tag></TableCell>
    <TableCell>{text(row.holding_days)} 天</TableCell><TableCell>{text(row.exit_reason)}</TableCell>
  </TableRow>;
}

function formatManifestCounts(counts: AnyRecord): string {
  const rows = number(counts.historical_etf_opportunities, 0);
  const candles = number(counts.shanghai_index_candles, 0);
  const markers = number(counts.shanghai_index_trade_markers, 0);
  return `manifest：历史回放 ${rows} 条，K 线 ${candles} 根，交易标记 ${markers} 个`;
}

function FixedNotices({ notices }: { notices: string[] }) {
  return <aside className="fixed-notices" data-testid="fixed-notices" aria-label="固定研究边界声明"><strong>研究边界</strong><div>{notices.map((notice, index) => <span className="wrap-anywhere" key={`${notice}-${index}`}>{notice}</span>)}</div></aside>;
}

function StateScreen({ title, detail, error = false, action }: { title: string; detail: string; error?: boolean; action?: React.ReactNode }) {
  return <main className="state-screen" role={error ? "alert" : "status"} aria-live="polite">{error ? <WarningAlt size={24} aria-hidden="true" /> : <SkeletonText heading width="12rem" />}<h1>{title}</h1><p className="wrap-anywhere">{detail}</p>{action}</main>;
}
