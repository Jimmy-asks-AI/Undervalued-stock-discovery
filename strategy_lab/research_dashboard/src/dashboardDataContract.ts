import type {
  AnyRecord,
  DashboardAction,
  DashboardData,
  DashboardDetails,
  SourceFreshnessStatus,
} from "./types";

const ACTIONS = new Set<DashboardAction>([
  "BLOCKED_DATA",
  "NO_ACTION",
  "WATCH",
  "WATCH_NO_TRADEABLE_ETF",
  "REVIEW_REQUIRED",
  "BUY_CANDIDATE",
  "HOLD",
  "REDUCE",
  "EXIT",
]);

const SOURCE_STATUSES = new Set<SourceFreshnessStatus>([
  "fresh",
  "historical_archive",
  "blocked",
  "missing_optional",
  "stale_optional",
  "degraded",
  "superseded",
]);

const GATE_STATUSES = new Set(["pass", "blocked", "fail", "warning"]);
const WARNING_SEVERITIES = new Set(["info", "warning", "error"]);
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const TIMEZONE_SUFFIX = /(Z|[+-]\d{2}:\d{2})$/;
const SHA256 = /^[a-f0-9]{64}$/;
const WINDOWS_ABSOLUTE_PATH = /(?:^|[\s=("'])\\\\|(?:^|[\s=("'])[A-Za-z]:[\\/]/;
const FILE_URI = /file:\/\//i;

function record(value: unknown, path: string): AnyRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${path} must be an object`);
  }
  return value as AnyRecord;
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${path} must be an array`);
  return value;
}

function string(value: unknown, path: string, allowEmpty = false): string {
  if (typeof value !== "string" || (!allowEmpty && value.length === 0)) {
    throw new Error(`${path} must be ${allowEmpty ? "a string" : "a non-empty string"}`);
  }
  return value;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${path} must be a boolean`);
  return value;
}

function finiteNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${path} must be a finite number`);
  }
  return value;
}

function nullableFiniteNumber(value: unknown, path: string): number | null {
  if (value === null) return null;
  return finiteNumber(value, path);
}

function nullableString(value: unknown, path: string): string | null {
  if (value === null) return null;
  return string(value, path, true);
}

function isoDate(value: unknown, path: string): string {
  const parsed = string(value, path);
  if (!ISO_DATE.test(parsed) || Number.isNaN(Date.parse(`${parsed}T00:00:00Z`))) {
    throw new Error(`${path} must be an ISO calendar date`);
  }
  return parsed;
}

function isoTimestamp(value: unknown, path: string, nullable = false): string | null {
  if (nullable && value === null) return null;
  const parsed = string(value, path);
  if (!TIMEZONE_SUFFIX.test(parsed) || Number.isNaN(Date.parse(parsed))) {
    throw new Error(`${path} must be an ISO timestamp with an explicit timezone`);
  }
  return parsed;
}

function enumString<T extends string>(value: unknown, path: string, allowed: Set<T>): T {
  const parsed = string(value, path) as T;
  if (!allowed.has(parsed)) throw new Error(`${path} has unsupported value: ${parsed}`);
  return parsed;
}

function assertNoAbsoluteLocalPaths(value: unknown, path = "root"): void {
  if (typeof value === "string") {
    if (WINDOWS_ABSOLUTE_PATH.test(value) || FILE_URI.test(value)) {
      throw new Error(`${path} must not expose an absolute local path`);
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoAbsoluteLocalPaths(item, `${path}[${index}]`));
    return;
  }
  if (typeof value !== "object" || value === null) return;
  for (const [key, item] of Object.entries(value)) {
    assertNoAbsoluteLocalPaths(item, `${path}.${key}`);
  }
}

function assertAutoExecutionDisabled(value: unknown, path = "root"): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertAutoExecutionDisabled(item, `${path}[${index}]`));
    return;
  }
  if (typeof value !== "object" || value === null) return;
  for (const [key, item] of Object.entries(value)) {
    if (key === "auto_execution_allowed" && item !== false) {
      throw new Error(`${path}.${key} must always be false`);
    }
    assertAutoExecutionDisabled(item, `${path}.${key}`);
  }
}

function parseTrustSummary(value: unknown, decisionAsOf: string): AnyRecord {
  const trust = record(value, "root.trust_summary");
  string(trust.research_state, "root.trust_summary.research_state");
  string(trust.policy_status, "root.trust_summary.policy_status");
  enumString(trust.current_action, "root.trust_summary.current_action", ACTIONS);
  for (const key of [
    "status_valid",
    "state_consistent",
    "manual_support_ready",
    "production_ready",
    "auto_execution_allowed",
    "cohort_consistent",
  ]) boolean(trust[key], `root.trust_summary.${key}`);
  if (trust.auto_execution_allowed !== false) {
    throw new Error("root.trust_summary.auto_execution_allowed must always be false");
  }
  if (isoDate(trust.decision_as_of_date, "root.trust_summary.decision_as_of_date") !== decisionAsOf) {
    throw new Error("root.trust_summary.decision_as_of_date must match root.decision_as_of_date");
  }
  for (const key of ["current_status_generated_at", "current_state_generated_at", "runner_generated_at"]) {
    isoTimestamp(trust[key], `root.trust_summary.${key}`);
  }
  for (const key of [
    "active_cohort_id",
    "active_cohort_manifest_hash",
    "current_status_cohort_id",
    "current_status_manifest_hash",
  ]) string(trust[key], `root.trust_summary.${key}`);
  for (const key of ["active_cohort_manifest_hash", "current_status_manifest_hash"]) {
    if (!SHA256.test(String(trust[key]))) throw new Error(`root.trust_summary.${key} must be a lowercase SHA-256 digest`);
  }
  if (trust.cohort_consistent === true && (
    trust.active_cohort_id !== trust.current_status_cohort_id
    || trust.active_cohort_manifest_hash !== trust.current_status_manifest_hash
  )) {
    throw new Error("root.trust_summary cohort_consistent=true requires matching cohort IDs and hashes");
  }
  return trust;
}

function parseSourceFreshness(value: unknown): AnyRecord[] {
  const rows = array(value, "root.source_freshness").map((item, index) => {
    const path = `root.source_freshness[${index}]`;
    const row = record(item, path);
    string(row.source, `${path}.source`);
    string(row.source_id, `${path}.source_id`);
    const cutoff = nullableString(row.cutoff_date, `${path}.cutoff_date`);
    if (cutoff !== null && cutoff !== "") isoDate(cutoff, `${path}.cutoff_date`);
    const lagDays = nullableFiniteNumber(row.lag_days, `${path}.lag_days`);
    if (lagDays !== null && (!Number.isInteger(lagDays) || lagDays < 0)) {
      throw new Error(`${path}.lag_days must be a non-negative integer or null`);
    }
    boolean(row.required, `${path}.required`);
    enumString(row.status, `${path}.status`, SOURCE_STATUSES);
    string(row.detail, `${path}.detail`, true);
    string(row.evidence_id, `${path}.evidence_id`);
    return row;
  });
  if (rows.length === 0) throw new Error("root.source_freshness must not be empty");
  const ids = rows.map((row) => row.source_id);
  if (new Set(ids).size !== ids.length) throw new Error("root.source_freshness source_id values must be unique");
  return rows;
}

function parseWarnings(value: unknown): AnyRecord[] {
  return array(value, "root.data_quality_warnings").map((item, index) => {
    const path = `root.data_quality_warnings[${index}]`;
    const warning = record(item, path);
    string(warning.code, `${path}.code`);
    enumString(warning.severity, `${path}.severity`, WARNING_SEVERITIES);
    string(warning.source, `${path}.source`);
    string(warning.message, `${path}.message`);
    string(warning.evidence_id, `${path}.evidence_id`);
    return warning;
  });
}

function parseRecommendation(value: unknown): AnyRecord {
  const recommendation = record(value, "root.current_recommendation");
  enumString(recommendation.action, "root.current_recommendation.action", ACTIONS);
  const blockers = array(recommendation.risk_vetoes, "root.current_recommendation.risk_vetoes");
  if (blockers.some((item) => typeof item !== "string")) {
    throw new Error("root.current_recommendation.risk_vetoes must contain strings");
  }
  array(recommendation.candidates, "root.current_recommendation.candidates").forEach((item, index) => {
    const candidate = record(item, `root.current_recommendation.candidates[${index}]`);
    if (candidate.action !== undefined) {
      enumString(candidate.action, `root.current_recommendation.candidates[${index}].action`, ACTIONS);
    }
  });
  if (recommendation.human_confirmation_required !== true) {
    throw new Error("root.current_recommendation.human_confirmation_required must be true");
  }
  if (recommendation.auto_execution_allowed !== false) {
    throw new Error("root.current_recommendation.auto_execution_allowed must be false");
  }
  return recommendation;
}

function parseGateResults(value: unknown): AnyRecord[] {
  const rows = array(value, "root.gate_results").map((item, index) => {
    const path = `root.gate_results[${index}]`;
    const gate = record(item, path);
    string(gate.gate_id, `${path}.gate_id`);
    string(gate.label, `${path}.label`);
    boolean(gate.passed, `${path}.passed`);
    enumString(gate.status, `${path}.status`, GATE_STATUSES);
    boolean(gate.veto, `${path}.veto`);
    string(gate.reason, `${path}.reason`, true);
    string(gate.evidence_id, `${path}.evidence_id`);
    if (gate.passed === true && gate.status !== "pass") {
      throw new Error(`${path}.passed and status disagree`);
    }
    if (gate.veto === true && gate.passed === true) {
      throw new Error(`${path}.veto cannot be true for a passing gate`);
    }
    return gate;
  });
  if (rows.length === 0) throw new Error("root.gate_results must not be empty");
  return rows;
}

function parseValuationSnapshot(value: unknown): AnyRecord {
  const snapshot = record(value, "root.valuation_snapshot");
  string(snapshot.version, "root.valuation_snapshot.version");
  isoTimestamp(snapshot.generated_at, "root.valuation_snapshot.generated_at", true);
  const snapshotDate = nullableString(snapshot.snapshot_date, "root.valuation_snapshot.snapshot_date");
  if (snapshotDate !== null && snapshotDate !== "") isoDate(snapshotDate, "root.valuation_snapshot.snapshot_date");
  string(snapshot.status, "root.valuation_snapshot.status");
  for (const key of ["available_count", "candidate_count"]) finiteNumber(snapshot[key], `root.valuation_snapshot.${key}`);
  const candidates = array(snapshot.candidates, "root.valuation_snapshot.candidates");
  candidates.forEach((item, index) => {
    const path = `root.valuation_snapshot.candidates[${index}]`;
    const row = record(item, path);
    nullableFiniteNumber(row.rank, `${path}.rank`);
    for (const key of ["industry_code", "industry_name", "parent_industry", "status", "pit_status", "note"]) {
      string(row[key], `${path}.${key}`, key === "note" || key === "parent_industry");
    }
    for (const key of ["score", "valuation_score", "oversold_score", "pe_ttm", "pb", "dividend_yield"]) {
      nullableFiniteNumber(row[key], `${path}.${key}`);
    }
  });
  if (snapshot.candidate_count !== candidates.length) {
    throw new Error("root.valuation_snapshot.candidate_count must match candidates.length");
  }
  return snapshot;
}

function parseDetailManifest(value: unknown): AnyRecord {
  const manifest = record(value, "root.detail_manifest");
  const url = string(manifest.url, "root.detail_manifest.url");
  if (/^(?:[a-z]+:|\/|\\)/i.test(url) || url.includes("..")) {
    throw new Error("root.detail_manifest.url must be a safe relative URL");
  }
  if (manifest.schema_version !== "dashboard-details-v1") {
    throw new Error("root.detail_manifest.schema_version must equal dashboard-details-v1");
  }
  if (!SHA256.test(string(manifest.sha256, "root.detail_manifest.sha256"))) {
    throw new Error("root.detail_manifest.sha256 must be a lowercase SHA-256 digest");
  }
  if (finiteNumber(manifest.bytes, "root.detail_manifest.bytes") <= 0) {
    throw new Error("root.detail_manifest.bytes must be positive");
  }
  const counts = record(manifest.counts, "root.detail_manifest.counts");
  for (const [key, count] of Object.entries(counts)) {
    if (!key || finiteNumber(count, `root.detail_manifest.counts.${key}`) < 0) {
      throw new Error(`root.detail_manifest.counts.${key} must be non-negative`);
    }
  }
  return manifest;
}

function parseRefreshSemantics(value: unknown): void {
  const refresh = record(value, "root.refresh_semantics");
  for (const key of [
    "local_reload_label",
    "local_reload_note",
    "rebuild_command",
    "online_refresh_command",
    "network_refresh_note",
  ]) string(refresh[key], `root.refresh_semantics.${key}`);
  if (refresh.local_reload_label !== "重新读取本地结果") {
    throw new Error("root.refresh_semantics.local_reload_label must state local reload semantics");
  }
  if (refresh.dev_port !== 5173 || refresh.preview_port !== 4175) {
    throw new Error("root.refresh_semantics must declare dev=5173 and preview=4175");
  }
}

function parseNotices(value: unknown): void {
  const rows = array(value, "root.fixed_notices");
  if (rows.length < 5) throw new Error("root.fixed_notices must contain all five fixed notices");
  const codes = new Set<string>();
  rows.forEach((item, index) => {
    const path = `root.fixed_notices[${index}]`;
    const row = record(item, path);
    codes.add(string(row.code, `${path}.code`));
    string(row.text, `${path}.text`);
  });
  for (const code of ["not_investment_advice", "history_not_future", "data_may_lag", "manual_support_not_ready", "auto_execution_disabled"]) {
    if (!codes.has(code)) throw new Error(`root.fixed_notices is missing ${code}`);
  }
}

function parseEvidenceCatalog(value: unknown): Set<string> {
  const rows = array(value, "root.evidence_catalog");
  const ids = new Set<string>();
  rows.forEach((item, index) => {
    const path = `root.evidence_catalog[${index}]`;
    const row = record(item, path);
    const id = string(row.evidence_id, `${path}.evidence_id`);
    if (ids.has(id)) throw new Error(`duplicate evidence_id: ${id}`);
    ids.add(id);
    const evidencePath = string(row.path, `${path}.path`);
    if (/^(?:[a-z]+:|\/|\\)/i.test(evidencePath) || evidencePath.includes("..")) {
      throw new Error(`${path}.path must be repository-relative`);
    }
    const generated = boolean(row.local_generated, `${path}.local_generated`);
    const linkable = boolean(row.linkable, `${path}.linkable`);
    if (generated && linkable) throw new Error(`${path} local generated evidence must not be linkable`);
  });
  return ids;
}

function assertEvidenceReferences(
  catalogIds: Set<string>,
  freshness: AnyRecord[],
  warnings: AnyRecord[],
  gates: AnyRecord[],
): void {
  for (const [collection, rows] of [["source_freshness", freshness], ["data_quality_warnings", warnings], ["gate_results", gates]] as const) {
    rows.forEach((row, index) => {
      const evidenceId = String(row.evidence_id ?? "");
      if (!catalogIds.has(evidenceId)) {
        throw new Error(`root.${collection}[${index}].evidence_id is absent from evidence_catalog: ${evidenceId}`);
      }
    });
  }
}

function buyCandidates(recommendation: AnyRecord): AnyRecord[] {
  return (recommendation.candidates as unknown[])
    .map((item, index) => record(item, `root.current_recommendation.candidates[${index}]`))
    .filter((item) => item.action === "BUY_CANDIDATE");
}

function assertDecisionSafety(
  trust: AnyRecord,
  freshness: AnyRecord[],
  warnings: AnyRecord[],
  recommendation: AnyRecord,
  gates: AnyRecord[],
): void {
  if (recommendation.action !== trust.current_action) {
    throw new Error("current recommendation action must match trust summary current_action");
  }
  const candidates = buyCandidates(recommendation);
  if (recommendation.action !== "BUY_CANDIDATE" && candidates.length > 0) {
    throw new Error("non-BUY action must not contain BUY_CANDIDATE rows");
  }
  if (recommendation.action !== "BUY_CANDIDATE") return;

  const unsafeSources = freshness.filter((row) => !["fresh", "historical_archive"].includes(String(row.status)));
  if (unsafeSources.length > 0) throw new Error("BUY_CANDIDATE is forbidden when any source is stale, missing, blocked, or superseded");
  if (warnings.some((warning) => warning.severity !== "info")) {
    throw new Error("BUY_CANDIDATE is forbidden while warning- or error-level data warnings exist");
  }
  if (gates.some((gate) => gate.passed !== true || gate.status !== "pass" || gate.veto !== false)) {
    throw new Error("BUY_CANDIDATE is forbidden while any decision gate is not passing");
  }
  if (trust.cohort_consistent !== true || trust.active_cohort_id !== trust.current_status_cohort_id || trust.active_cohort_manifest_hash !== trust.current_status_manifest_hash) {
    throw new Error("BUY_CANDIDATE is forbidden when cohort identity is inconsistent");
  }
  if (trust.status_valid !== true || trust.state_consistent !== true || trust.manual_support_ready !== true || trust.production_ready !== true) {
    throw new Error("BUY_CANDIDATE is forbidden until status, state, manual support, and production readiness all pass");
  }
  if (candidates.length === 0) throw new Error("BUY_CANDIDATE requires at least one BUY_CANDIDATE row");
}

export function canRenderBuyRecommendation(data: DashboardData): boolean {
  if (data.current_recommendation.action !== "BUY_CANDIDATE") return false;
  if (!data.trust_summary.status_valid || !data.trust_summary.state_consistent || !data.trust_summary.cohort_consistent) return false;
  if (!data.trust_summary.manual_support_ready || !data.trust_summary.production_ready) return false;
  if (data.source_freshness.some((source) => !["fresh", "historical_archive"].includes(source.status))) return false;
  if (data.data_quality_warnings.some((warning) => warning.severity !== "info")) return false;
  if (data.gate_results.some((gate) => gate.passed !== true || gate.status !== "pass" || gate.veto !== false)) return false;
  return data.current_recommendation.candidates.some((candidate) => candidate.action === "BUY_CANDIDATE");
}

export function parseDashboardData(value: unknown): DashboardData {
  const root = record(value, "root");
  if (root.schema_version !== "dashboard-data-v2") {
    throw new Error("root.schema_version must equal dashboard-data-v2");
  }
  isoTimestamp(root.generated_at, "root.generated_at");
  const decisionAsOf = isoDate(root.decision_as_of_date, "root.decision_as_of_date");
  const trust = parseTrustSummary(root.trust_summary, decisionAsOf);
  const freshness = parseSourceFreshness(root.source_freshness);
  const warnings = parseWarnings(root.data_quality_warnings);
  const recommendation = parseRecommendation(root.current_recommendation);
  const gates = parseGateResults(root.gate_results);
  parseValuationSnapshot(root.valuation_snapshot);
  array(root.market_index_states, "root.market_index_states").forEach((row, index) => record(row, `root.market_index_states[${index}]`));
  parseDetailManifest(root.detail_manifest);
  parseRefreshSemantics(root.refresh_semantics);
  parseNotices(root.fixed_notices);
  const evidenceIds = parseEvidenceCatalog(root.evidence_catalog);
  assertEvidenceReferences(evidenceIds, freshness, warnings, gates);
  assertDecisionSafety(trust, freshness, warnings, recommendation, gates);
  assertAutoExecutionDisabled(root);
  assertNoAbsoluteLocalPaths(root);
  return root as unknown as DashboardData;
}

export function parseDashboardDetails(value: unknown): DashboardDetails {
  const root = record(value, "root");
  if (root.schema_version !== "dashboard-details-v1") {
    throw new Error("root.schema_version must equal dashboard-details-v1");
  }
  isoTimestamp(root.generated_at, "root.generated_at");
  isoDate(root.decision_as_of_date, "root.decision_as_of_date");
  const counts = record(root.counts, "root.counts");
  for (const [key, count] of Object.entries(counts)) finiteNumber(count, `root.counts.${key}`);
  for (const key of ["historical_etf_opportunities", "shanghai_index_candles", "shanghai_index_trade_markers"]) {
    array(root[key], `root.${key}`).forEach((row, index) => record(row, `root.${key}[${index}]`));
  }
  record(root.historical_opportunity_summary, "root.historical_opportunity_summary");
  assertAutoExecutionDisabled(root);
  assertNoAbsoluteLocalPaths(root);
  return root as unknown as DashboardDetails;
}

export async function sha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
