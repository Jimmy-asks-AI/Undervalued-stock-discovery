from __future__ import annotations

from typing import Any, Callable


Agent = tuple[str, Callable[[dict[str, Any]], tuple[bool, list[str], dict[str, Any]]]]


def run_veto_chain(context: dict[str, Any]) -> list[dict[str, Any]]:
    agents: list[Agent] = [
        ("data_pit_steward", data_pit_steward),
        ("market_regime_agent", market_regime_agent),
        ("industry_rank_agent", industry_rank_agent),
        ("etf_implementation_agent", etf_implementation_agent),
        ("portfolio_risk_agent", portfolio_risk_agent),
        ("independent_validation_auditor", independent_validation_auditor),
    ]
    results = []
    for name, function in agents:
        passed, reasons, evidence = function(context)
        status = "pass" if passed else "fail"
        results.append({"agent": name, "status": status, "veto": not passed,
                        "reason_codes": reasons, "evidence": evidence})
    return results


def data_pit_steward(context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    stale = [row["source"] for row in context["source_manifest"] if row["required"] and row["status"] != "pass"]
    etf_pit = context["etf_pit"]
    reasons = [f"stale:{name}" for name in stale]
    if not etf_pit.get("pit_master_ready"):
        reasons.append("etf_historical_pit_not_ready")
    return not reasons, reasons, {"stale_required_sources": stale, "etf_pit_ready": etf_pit.get("pit_master_ready"),
                                  "exact_index_code_coverage": etf_pit.get("exact_index_code_coverage")}


def market_regime_agent(context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    timing = context["timing"]
    reasons = []
    if not timing.get("production_ready"):
        reasons.append("timing_robustness_not_ready")
    if not timing.get("latest_signal_triggered"):
        reasons.append("no_current_rebound_window")
    return not reasons, reasons, {"production_ready": timing.get("production_ready"),
                                  "latest_signal_triggered": timing.get("latest_signal_triggered")}


def industry_rank_agent(context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    selection, goal = context["selection"], context["goal"]
    reasons = []
    if int(selection.get("passing_rule_count", 0) or 0) <= 0:
        reasons.append("no_robust_industry_rule")
    if not goal.get("goal_ready"):
        reasons.append("industry_alpha_goal_not_ready")
    return not reasons, reasons, {"passing_rule_count": selection.get("passing_rule_count"), "goal_ready": goal.get("goal_ready")}


def etf_implementation_agent(context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    pit, replay = context["etf_pit"], context["replay"]
    reasons = []
    if not pit.get("current_mapping_ready"):
        reasons.append("etf_mapping_coverage_insufficient")
    direct_mapping_count = int(context.get("direct_industry_etf_mapping_count", 0) or 0)
    if direct_mapping_count <= 0:
        reasons.append("no_direct_industry_etf_mapping")
    if not replay.get("cross_check_passed"):
        reasons.append("execution_arithmetic_cross_check_failed")
    if replay.get("external_event_engine_cross_check") != "pass":
        reasons.append("external_event_engine_pending")
    return not reasons, reasons, {"mapping_coverage": pit.get("exact_index_code_coverage"),
                                  "direct_industry_etf_mapping_count": direct_mapping_count,
                                  "execution_mean_net_return": replay.get("mean_net_return"),
                                  "external_cross_check": replay.get("external_event_engine_cross_check")}


def portfolio_risk_agent(context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    errors, risk = context["account_errors"], context["portfolio_risk"]
    reasons = list(errors) + list(risk.get("breaches", []))
    return not reasons, reasons, risk


def independent_validation_auditor(context: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    timing, goal, replay = context["timing"], context["goal"], context["replay"]
    ledger = context.get("experiment_ledger", {})
    reasons = []
    if int(timing.get("blocking_issue_count", 0) or 0):
        reasons.append("timing_blocking_issues")
    if int(goal.get("blocking_nonpass_count", 0) or 0):
        reasons.append("goal_nonpass_items")
    if replay.get("external_event_engine_cross_check") != "pass":
        reasons.append("independent_engine_not_verified")
    if not ledger.get("integrity_passed") or int(ledger.get("experiment_count", 0) or 0) <= 0:
        reasons.append("experiment_ledger_not_ready")
    return not reasons, reasons, {"timing_blocking_issue_count": timing.get("blocking_issue_count"),
                                  "goal_blocking_nonpass_count": goal.get("blocking_nonpass_count"),
                                  "replay_cross_check": replay.get("external_event_engine_cross_check"),
                                  "experiment_ledger_integrity": ledger.get("integrity_passed"),
                                  "experiment_count": ledger.get("experiment_count")}


def self_check() -> None:
    context = {"source_manifest": [{"source": "x", "required": True, "status": "fail"}],
               "etf_pit": {}, "timing": {}, "selection": {}, "goal": {}, "replay": {},
               "account_errors": ["missing"], "portfolio_risk": {}, "direct_industry_etf_mapping_count": 0,
               "experiment_ledger": {}}
    results = run_veto_chain(context)
    assert results[0]["status"] == "fail"
    assert len(results) == 6 and all(row["status"] == "fail" for row in results)
    print("self_check=pass")


if __name__ == "__main__":
    self_check()
