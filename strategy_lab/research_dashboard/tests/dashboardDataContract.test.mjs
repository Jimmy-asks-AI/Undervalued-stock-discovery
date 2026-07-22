import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  canRenderBuyRecommendation,
  parseDashboardData,
  parseDashboardDetails,
  sha256Hex,
} from "../src/dashboardDataContract.ts";

const fixtureUrl = new URL("./fixtures/dashboard_data.valid.json", import.meta.url);

async function fixture() {
  return JSON.parse(await readFile(fixtureUrl, "utf8"));
}

function makeBuyReady(value) {
  value.trust_summary.current_action = "BUY_CANDIDATE";
  value.current_recommendation.action = "BUY_CANDIDATE";
  value.trust_summary.policy_status = "decision_support_ready";
  value.trust_summary.research_state = "manual_review_candidate";
  value.trust_summary.manual_support_ready = true;
  value.trust_summary.production_ready = true;
  value.current_recommendation.risk_vetoes = [];
  value.gate_results = [{
    gate_id: "all_decision_gates",
    label: "所有决策门禁",
    passed: true,
    status: "pass",
    veto: false,
    reason: "固定合同正例",
    evidence_id: "gate_results",
  }];
  value.current_recommendation.candidates = [{
    action: "BUY_CANDIDATE",
    etf_code: "510300",
    etf_name: "沪深300ETF",
    target_model_weight: 0.1,
  }];
  return value;
}

test("accepts the fixed offline v2 summary and keeps NO_ACTION fail-closed", async () => {
  const parsed = parseDashboardData(await fixture());
  assert.equal(parsed.schema_version, "dashboard-data-v2");
  assert.equal(parsed.current_recommendation.auto_execution_allowed, false);
  assert.equal(canRenderBuyRecommendation(parsed), false);
});

test("accepts a fully consistent manual-review BUY candidate", async () => {
  const parsed = parseDashboardData(makeBuyReady(await fixture()));
  assert.equal(canRenderBuyRecommendation(parsed), true);
});

test("rejects an unknown schema before rendering", async () => {
  const value = await fixture();
  value.schema_version = "unknown";
  assert.throws(() => parseDashboardData(value), /schema_version/);
});

test("rejects a generated_at timestamp without an explicit timezone", async () => {
  const value = await fixture();
  value.generated_at = "2026-07-18T21:00:00";
  assert.throws(() => parseDashboardData(value), /explicit timezone/);
});

test("rejects auto execution flags anywhere in the payload", async () => {
  const value = await fixture();
  value.trust_summary.auto_execution_allowed = true;
  assert.throws(() => parseDashboardData(value), /auto_execution_allowed/);
});

test("rejects absolute local paths", async () => {
  const value = await fixture();
  value.evidence_catalog[0].path = "E:\\private\\source_manifest.csv";
  assert.throws(() => parseDashboardData(value), /repository-relative|absolute local path/);
});

test("rejects stale or missing source state with BUY semantics", async () => {
  const value = makeBuyReady(await fixture());
  value.source_freshness[0].status = "stale_optional";
  value.source_freshness[0].required = false;
  assert.throws(() => parseDashboardData(value), /stale, missing, blocked, or superseded/);
});

test("accepts stale NO_ACTION only as a fail-closed non-BUY state", async () => {
  const value = await fixture();
  value.source_freshness[0].status = "stale_optional";
  value.source_freshness[0].required = false;
  value.data_quality_warnings = [{
    code: "stale_market_index",
    severity: "warning",
    source: "market_index",
    message: "固定反例：源已陈旧。",
    evidence_id: "source_manifest",
  }];
  const parsed = parseDashboardData(value);
  assert.equal(canRenderBuyRecommendation(parsed), false);
});

test("accepts an explicit degraded NO_ACTION state but never renders BUY", async () => {
  const value = await fixture();
  value.source_freshness[0].status = "degraded";
  value.data_quality_warnings = [{
    code: "degraded_market_index",
    severity: "warning",
    source: "market_index",
    message: "固定反例：源处于降级状态。",
    evidence_id: "source_manifest",
  }];
  const parsed = parseDashboardData(value);
  assert.equal(canRenderBuyRecommendation(parsed), false);
});

test("rejects cohort mismatch with BUY semantics", async () => {
  const value = makeBuyReady(await fixture());
  value.trust_summary.current_status_cohort_id = "other-cohort";
  value.trust_summary.cohort_consistent = false;
  assert.throws(() => parseDashboardData(value), /cohort identity/);
});

test("accepts cohort mismatch only while the action remains NO_ACTION", async () => {
  const value = await fixture();
  value.trust_summary.current_status_cohort_id = "other-cohort";
  value.trust_summary.cohort_consistent = false;
  value.data_quality_warnings = [{
    code: "cohort_mismatch",
    severity: "error",
    source: "current_state.summary",
    message: "固定反例：cohort 不一致。",
    evidence_id: "source_manifest",
  }];
  const parsed = parseDashboardData(value);
  assert.equal(canRenderBuyRecommendation(parsed), false);
});

test("rejects NO_ACTION carrying a BUY row", async () => {
  const value = await fixture();
  value.current_recommendation.candidates = [{ action: "BUY_CANDIDATE", etf_code: "510300" }];
  assert.throws(() => parseDashboardData(value), /non-BUY action/);
});

test("rejects BUY action with an empty candidate array", async () => {
  const value = makeBuyReady(await fixture());
  value.current_recommendation.candidates = [];
  assert.throws(() => parseDashboardData(value), /at least one/);
});

test("rejects warning-level data quality findings with BUY semantics", async () => {
  const value = makeBuyReady(await fixture());
  value.data_quality_warnings = [{
    code: "source_contract_error",
    severity: "warning",
    source: "market_index",
    message: "源合同错误",
    evidence_id: "source_manifest",
  }];
  assert.throws(() => parseDashboardData(value), /warning- or error-level data warnings/);
});

test("rejects BUY semantics while any decision gate is blocked", async () => {
  const value = makeBuyReady(await fixture());
  value.gate_results[0].passed = false;
  value.gate_results[0].status = "blocked";
  value.gate_results[0].veto = true;
  assert.throws(() => parseDashboardData(value), /decision gate is not passing/);
});

test("rejects a disagreement between trust and recommendation actions", async () => {
  const value = await fixture();
  value.trust_summary.current_action = "WATCH";
  assert.throws(() => parseDashboardData(value), /must match/);
});

test("accepts dashboard-details-v1 and computes a stable SHA-256 digest", async () => {
  const details = {
    schema_version: "dashboard-details-v1",
    generated_at: "2026-07-18T21:00:00+08:00",
    decision_as_of_date: "2026-07-18",
    counts: { historical_etf_opportunities: 0 },
    historical_etf_opportunities: [],
    historical_opportunity_summary: {},
    shanghai_index_candles: [],
    shanghai_index_trade_markers: [],
  };
  assert.equal(parseDashboardDetails(details).schema_version, "dashboard-details-v1");
  assert.equal(await sha256Hex("dashboard"), "66cd9688a2ae068244ea01e70f0e230f5623b7fa4cdecb65070a09ec06452262");
});
