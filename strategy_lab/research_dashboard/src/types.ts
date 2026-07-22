export type AnyRecord = Record<string, unknown>;

export type DashboardAction =
  | "BLOCKED_DATA"
  | "NO_ACTION"
  | "WATCH"
  | "WATCH_NO_TRADEABLE_ETF"
  | "REVIEW_REQUIRED"
  | "BUY_CANDIDATE"
  | "HOLD"
  | "REDUCE"
  | "EXIT";

export type SourceFreshnessStatus =
  | "fresh"
  | "historical_archive"
  | "blocked"
  | "missing_optional"
  | "stale_optional"
  | "degraded"
  | "superseded";

export interface TrustSummary {
  research_state: string;
  policy_status: string;
  current_action: DashboardAction;
  status_valid: boolean;
  state_consistent: boolean;
  manual_support_ready: boolean;
  production_ready: boolean;
  auto_execution_allowed: false;
  decision_as_of_date: string;
  current_status_generated_at: string;
  current_state_generated_at: string;
  runner_generated_at: string;
  active_cohort_id: string;
  active_cohort_manifest_hash: string;
  current_status_cohort_id: string;
  current_status_manifest_hash: string;
  cohort_consistent: boolean;
}

export interface SourceFreshness {
  source: string;
  source_id: string;
  cutoff_date: string | null;
  lag_days: number | null;
  required: boolean;
  status: SourceFreshnessStatus;
  detail: string;
  evidence_id: string;
}

export interface DataQualityWarning {
  code: string;
  severity: "info" | "warning" | "error";
  source: string;
  message: string;
  evidence_id: string;
}

export interface GateResult {
  gate_id: string;
  label: string;
  passed: boolean;
  status: "pass" | "blocked" | "fail" | "warning";
  veto: boolean;
  reason: string;
  evidence_id: string;
}

export interface RecommendationCandidate extends AnyRecord {
  action?: DashboardAction;
  etf_code?: string;
  etf_name?: string;
  industry_name?: string;
  target_model_weight?: number;
}

export interface CurrentRecommendation {
  recommendation_id?: string;
  as_of_datetime?: string;
  action: DashboardAction;
  action_reason_codes?: string[];
  risk_vetoes: string[];
  candidates: RecommendationCandidate[];
  human_confirmation_required: true;
  auto_execution_allowed: false;
  [key: string]: unknown;
}

export interface ValuationCandidate {
  rank: number | null;
  industry_code: string;
  industry_name: string;
  parent_industry: string;
  status: string;
  score: number | null;
  valuation_score: number | null;
  oversold_score: number | null;
  pe_ttm: number | null;
  pb: number | null;
  dividend_yield: number | null;
  pit_status: string;
  note: string;
}

export interface ValuationSnapshot {
  version: string;
  generated_at: string | null;
  snapshot_date: string | null;
  status: string;
  available_count: number;
  candidate_count: number;
  candidates: ValuationCandidate[];
}

export interface MarketIndexState extends AnyRecord {
  symbol?: string;
  name?: string;
  close?: number;
  trade_date?: string;
  price_percentile_3y?: number;
  point_status?: string;
  rsi_14?: number;
  momentum_status?: string;
  return_20d?: number;
}

export interface DetailManifest {
  url: string;
  schema_version: "dashboard-details-v1";
  sha256: string;
  bytes: number;
  counts: Record<string, number>;
}

export interface RefreshSemantics {
  local_reload_label: string;
  local_reload_note: string;
  rebuild_command: string;
  online_refresh_command: string;
  network_refresh_note: string;
  dev_port: 5173;
  preview_port: 4175;
}

export interface FixedNotice {
  code: string;
  text: string;
}

export interface EvidenceEntry {
  evidence_id: string;
  path: string;
  local_generated: boolean;
  linkable: boolean;
}

export interface DashboardData {
  schema_version: "dashboard-data-v2";
  generated_at: string;
  decision_as_of_date: string;
  trust_summary: TrustSummary;
  source_freshness: SourceFreshness[];
  data_quality_warnings: DataQualityWarning[];
  current_recommendation: CurrentRecommendation;
  gate_results: GateResult[];
  valuation_snapshot: ValuationSnapshot;
  market_index_states: MarketIndexState[];
  detail_manifest: DetailManifest;
  refresh_semantics: RefreshSemantics;
  fixed_notices: FixedNotice[];
  evidence_catalog: EvidenceEntry[];
}

export interface HistoricalOpportunity extends AnyRecord {
  etf_code?: string;
  entry_date?: string;
  exit_date?: string;
  entry_price?: number;
  exit_price?: number;
  net_return?: number;
  holding_days?: number;
  exit_reason?: string;
}

export interface DashboardDetails {
  schema_version: "dashboard-details-v1";
  generated_at: string;
  decision_as_of_date: string;
  counts: Record<string, number>;
  historical_etf_opportunities: HistoricalOpportunity[];
  historical_opportunity_summary: AnyRecord;
  shanghai_index_candles: AnyRecord[];
  shanghai_index_trade_markers: AnyRecord[];
}
