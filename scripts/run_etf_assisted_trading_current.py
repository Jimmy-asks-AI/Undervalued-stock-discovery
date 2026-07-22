#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from strategy_lab.etf_assisted_trading_agents import run_veto_chain
from research_evidence_routes import (
    verified_forward_evidence_ready,
    verified_forward_industry_ready,
    verified_forward_timing_ready,
)
from valuation_pit_contract import methodology_route_ready
DEFAULT_CONFIG = ROOT / "configs" / "etf_assisted_trading_current_policy.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the single current ETF-assisted trading research pipeline.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--refresh-inputs", action="store_true", help="Refresh the minimum daily input chain before generating advice.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    as_of = date.fromisoformat(args.as_of_date)
    config = read_json(Path(args.config))
    if args.refresh_inputs:
        account_errors = validate_account_state(read_json(ROOT / config["sources"]["account_state"]), as_of)
        if account_errors:
            raise SystemExit(f"account state preflight failed: {','.join(account_errors)}")
        run_commands(refresh_input_commands(as_of))
    result = run_pipeline(config, as_of)
    write_outputs(config, result)
    if args.refresh_inputs:
        run_commands([["scripts/build_dashboard_dataset.py"]])
    print(f"output_dir={result['output_dir']}")
    print(f"action={result['summary']['action']}")
    print(f"blocking_gate_count={result['summary']['blocking_gate_count']}")


def refresh_input_commands(as_of: date) -> list[list[str]]:
    value = as_of.isoformat()
    return [
        ["scripts/build_etf_pit_master.py", "--snapshot-date", value],
        ["scripts/run_industry_index_research_validation.py", "--trade-date", value, "--industry-level", "second", "--cache-dir", "data_catalog/cache/industry_index", "--output", "outputs/industry_index_research_validation", "--refresh-history"],
        ["scripts/audit_pit_universe_methodology.py"],
        ["scripts/run_industry_rebound_window_v3_4_realtime_model.py", "--refresh-market-index-only"],
        ["scripts/run_industry_rebound_window_v3_7_industry_breadth.py"],
        ["scripts/run_industry_rebound_window_v4_61_relaxed_breadth_relief.py", "--config", "configs/rebound_window_v4_70_delayed_entry_vol_stop_policy.json"],
        ["scripts/evaluate_rebound_window_effectiveness.py", "--output-dir", "outputs/industry_rebound_window_v4_70_delayed_entry_vol_stop"],
        ["scripts/run_industry_rebound_window_v4_71_robustness_live_audit.py", "--as-of-date", value],
        ["scripts/build_v5_08_rebound_leader_forward_signal_detector.py", "--as-of-date", value, "--apply"],
        ["scripts/settle_v5_06_rebound_leader_forward_samples.py", "--as-of-date", value],
        ["scripts/build_v5_07_rebound_leader_promotion_evaluator.py"],
        ["scripts/build_v5_21_rebound_leader_new_pit_source_discovery.py"],
        ["scripts/build_v5_11_rebound_leader_pit_valuation_audit.py"],
        ["scripts/build_v5_12_rebound_leader_pit_valuation_percentile_audit.py"],
        ["scripts/build_v5_20_rebound_leader_evidence_boundary_audit.py"],
        ["scripts/build_v5_10_rebound_leader_goal_completion_audit.py"],
    ]


def run_commands(commands: list[list[str]]) -> None:
    for command in commands:
        subprocess.run([sys.executable, str(ROOT / command[0]), *command[1:]], cwd=ROOT, check=True)


def run_pipeline(config: dict[str, Any], as_of: date) -> dict[str, Any]:
    sources = config["sources"]
    timing = read_json(ROOT / sources["timing_summary"])
    selection = read_json(ROOT / sources["industry_selection_summary"])
    goal = read_json(ROOT / sources["goal_summary"])
    promotion = read_json(ROOT / sources["forward_promotion_summary"])
    detector = read_json(ROOT / sources["forward_detector_summary"])
    methodology_source = sources.get("pit_universe_methodology_summary")
    pit_methodology = read_json(ROOT / methodology_source) if methodology_source else {}
    timing, selection, goal, forward_evidence_ready = resolve_forward_evidence(timing, selection, goal, promotion, detector)
    industry_candidate_rows = active_forward_candidates(read_csv_rows(ROOT / sources["industry_candidate_file"]), promotion)
    experiment_ledger = read_json(ROOT / sources["experiment_ledger_summary"])
    lifecycle = read_json(ROOT / sources["etf_lifecycle_summary"])
    manifest = build_source_manifest(
        sources,
        as_of,
        int(config["max_stale_calendar_days"]),
        int(config["required_industry_count"]),
        int(config.get("minimum_fresh_industry_count", config["required_industry_count"])),
        int(config.get("minimum_valuation_history_years", 8)),
        timing,
        selection,
        pit_methodology,
        promotion,
    )
    account = read_json(ROOT / sources["account_state"])
    account_errors = validate_account_state(account, as_of)
    risk = portfolio_risk(account, config.get("portfolio_limits", {})) if not account_errors else {}

    data_ok = all(
        row["status"] == "pass"
        for row in manifest
        if row["required"] and row["source"] != "pit_valuation_methodology"
    )
    pit_methodology_route_ready = methodology_route_ready(pit_methodology, promotion)
    etf_pit = read_json(ROOT / "outputs" / "audit" / "etf_pit_master" / "run_summary.json")
    replay = read_json(ROOT / "outputs" / "audit" / "etf_realistic_execution_replay" / "run_summary.json")
    etf_lookup = latest_etf_lookup(ROOT / sources["etf_pit_master"], as_of)
    exposure_mapping = read_csv_rows(ROOT / sources["etf_sw_industry_mapping"])
    direct_mapping_audit = build_direct_mapping_audit(
        [path.stem for path in (ROOT / sources["industry_history_dir"]).glob("*.csv")], etf_lookup, exposure_mapping
    )
    direct_mapping_count = sum(row["mapping_status"] in {"exact_index_code", "high_confidence_component_exposure"} for row in direct_mapping_audit)
    historical_universe_ready = bool(etf_pit.get("historical_pit_ready") or lifecycle.get("observed_tradability_universe_ready"))
    etf_pit_ok = bool(etf_pit.get("current_mapping_ready") and historical_universe_ready)
    effective_etf_pit = {
        **etf_pit,
        "pit_master_ready": etf_pit_ok,
        "historical_universe_ready": historical_universe_ready,
        "historical_reconstruction_mode": "daily_pit_snapshots" if etf_pit.get("historical_pit_ready") else "observed_trade_intervals",
    }
    latest_signal = bool(timing.get("latest_signal_triggered"))
    gates = [
        gate("data_freshness", data_ok, source_evidence(manifest, exclude={"pit_valuation_methodology"}), True),
        gate("pit_universe_methodology", pit_methodology_route_ready, f"audit_passed={pit_methodology.get('audit_passed')}; historical_promotion_gate={pit_methodology.get('promotion_gate_passed')}; true_forward_route={forward_evidence_ready}; eligible_valuation_rows={pit_methodology.get('promotion_eligible_valuation_row_count')}; valuation_cutoff={pit_methodology.get('valuation_direct_source_max_trade_date')}; classification={pit_methodology.get('classification_history_status')}", True),
        gate("timing_robustness", bool(timing.get("production_ready")), f"production_ready={timing.get('production_ready')}; blocking={timing.get('blocking_issue_count')}", True),
        gate("industry_selection", int(selection.get("passing_rule_count", 0) or 0) > 0, f"passing_rule_count={selection.get('passing_rule_count')}; status={selection.get('best_status')}", True),
        gate("etf_pit_master", etf_pit_ok, f"exists={(ROOT / sources['etf_pit_master']).exists()}; exact_index_code_coverage={etf_pit.get('exact_index_code_coverage')}; historical_mode={effective_etf_pit['historical_reconstruction_mode']}; ready={etf_pit_ok}", True),
        gate("account_state", not account_errors, f"path={sources['account_state']}; errors={','.join(account_errors) or 'none'}", True),
        gate("portfolio_risk", bool(risk.get("risk_gate_passed")) if not account_errors else False, f"breaches={','.join(risk.get('breaches', [])) or 'none'}", True),
        gate("goal_evidence", bool(goal.get("goal_ready")), f"goal_ready={goal.get('goal_ready')}; blocking_nonpass={goal.get('blocking_nonpass_count')}", True),
        gate("current_industry_candidates", not latest_signal or not forward_evidence_ready or bool(industry_candidate_rows), f"latest_signal={latest_signal}; forward_evidence_ready={forward_evidence_ready}; candidates={len(industry_candidate_rows)}", True),
    ]
    agent_results = run_veto_chain({"source_manifest": manifest, "etf_pit": effective_etf_pit, "timing": timing,
                                    "selection": selection, "goal": goal, "replay": replay,
                                    "account_errors": account_errors, "portfolio_risk": risk,
                                    "direct_industry_etf_mapping_count": direct_mapping_count,
                                    "experiment_ledger": experiment_ledger})
    gates.append(gate("agent_veto_chain", all(row["status"] == "pass" for row in agent_results),
                      ";".join(f"{row['agent']}={row['status']}" for row in agent_results), True))
    has_position = bool(account.get("positions")) if not account_errors else False
    blockers = [row["gate"] for row in gates if row["veto"] and row["status"] != "pass"]
    policy_hash = sha256_json(config)
    buy_candidates = build_buy_candidates(industry_candidate_rows, etf_lookup, exposure_mapping, as_of, latest_signal, blockers,
                                          config.get("portfolio_limits", {}), account if not account_errors else {})
    position_recommendations = build_position_recommendations(
        account,
        as_of,
        latest_signal,
        blockers,
        config.get("position_rules", {}),
        ROOT / sources["etf_history_dir"],
        ROOT / sources["etf_pit_master"],
    ) if not account_errors else []
    projected_risk = projected_portfolio_risk(account, buy_candidates + position_recommendations,
                                              config.get("portfolio_limits", {})) if not account_errors else {}
    projected_risk_passed = bool(projected_risk.get("risk_gate_passed")) if not account_errors else False
    gates.append(gate(
        "projected_portfolio_risk",
        projected_risk_passed,
        f"breaches={','.join(projected_risk.get('breaches', [])) or 'none'}; "
        f"strategy_weight={projected_risk.get('strategy_weight', '')}; cash_weight={projected_risk.get('cash_weight', '')}",
        True,
    ))
    final_blockers = [row["gate"] for row in gates if row["veto"] and row["status"] != "pass"]
    if final_blockers != blockers:
        blockers = final_blockers
        buy_candidates = build_buy_candidates(
            industry_candidate_rows, etf_lookup, exposure_mapping, as_of, latest_signal, blockers,
            config.get("portfolio_limits", {}), account if not account_errors else {},
        )
        position_recommendations = build_position_recommendations(
            account,
            as_of,
            latest_signal,
            blockers,
            config.get("position_rules", {}),
            ROOT / sources["etf_history_dir"],
            ROOT / sources["etf_pit_master"],
        ) if not account_errors else []
    action = choose_action(gates, has_position=has_position, window_active=latest_signal)
    recommendation_id = hashlib.sha256(
        f"{config['policy_id']}|{config['version']}|{as_of.isoformat()}|{action}|{policy_hash}".encode()
    ).hexdigest()[:20]
    recommendation = {
        "recommendation_id": recommendation_id,
        "as_of_datetime": datetime.now().isoformat(timespec="seconds"),
        "data_cutoff_date": as_of.isoformat(),
        "data_cutoff_by_source": {row["source"]: row["latest_date"] for row in manifest},
        "policy_id": config["policy_id"],
        "policy_version": config["version"],
        "policy_hash": policy_hash,
        "experiment_ledger_head_hash": experiment_ledger.get("ledger_head_hash", ""),
        "evidence_status": "forward_validated" if forward_evidence_ready else "research_only",
        "action": action,
        "action_reason_codes": blockers,
        "risk_vetoes": blockers,
        "signal_date": timing.get("latest_panel_date", ""),
        "candidates": buy_candidates + position_recommendations,
        "portfolio_risk": risk,
        "projected_portfolio_risk": projected_risk,
        "human_confirmation_required": True,
        "auto_execution_allowed": False,
    }
    summary = {
        "version": config["version"],
        "policy_id": config["policy_id"],
        "policy_status": config["policy_status"],
        "generated_at": recommendation["as_of_datetime"],
        "as_of_date": as_of.isoformat(),
        "action": action,
        "candidate_count": sum(row["action"] == "BUY_CANDIDATE" for row in buy_candidates),
        "industry_candidate_count": len(industry_candidate_rows),
        "buy_candidate_review_count": len(buy_candidates),
        "direct_industry_etf_mapping_count": direct_mapping_count,
        "position_recommendation_count": len(position_recommendations),
        "blocking_gate_count": len(blockers),
        "blocking_gates": blockers,
        "data_gate_passed": data_ok,
        "pit_universe_methodology_gate_passed": pit_methodology_route_ready,
        "historical_valuation_pit_gate_passed": bool(pit_methodology.get("historical_valuation_pit_gate_passed")),
        "historical_classification_gate_passed": bool(pit_methodology.get("historical_classification_gate_passed")),
        "promotion_eligible_valuation_row_count": int(pit_methodology.get("promotion_eligible_valuation_row_count", 0) or 0),
        "valuation_direct_source_max_trade_date": pit_methodology.get("valuation_direct_source_max_trade_date", ""),
        "valuation_availability_status": pit_methodology.get("valuation_availability_status", "unknown"),
        "historical_review_set_label": pit_methodology.get("historical_review_set_label", "unknown"),
        "timing_gate_passed": gate_passed(gates, "timing_robustness"),
        "industry_selection_gate_passed": gate_passed(gates, "industry_selection"),
        "etf_pit_gate_passed": gate_passed(gates, "etf_pit_master"),
        "etf_historical_reconstruction_mode": effective_etf_pit["historical_reconstruction_mode"],
        "account_state_gate_passed": gate_passed(gates, "account_state"),
        "portfolio_risk_gate_passed": gate_passed(gates, "portfolio_risk"),
        "projected_portfolio_risk_gate_passed": bool(projected_risk.get("risk_gate_passed")) if projected_risk else False,
        "goal_evidence_gate_passed": gate_passed(gates, "goal_evidence"),
        "forward_timing_gate_passed": verified_forward_timing_ready(promotion),
        "forward_industry_gate_passed": verified_forward_industry_ready(promotion),
        "evidence_route": "forward_validated" if forward_evidence_ready else "historical_research_only",
        "can_generate_buy_recommendation": action == "BUY_CANDIDATE",
        "can_generate_sell_recommendation": action in {"REDUCE", "EXIT"},
        "manual_decision_support_ready": not blockers,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "当前主线被硬门禁阻断，不生成ETF买卖建议。" if blockers else "全部研究门禁通过，仍须人工确认。",
    }
    return {"output_dir": str(ROOT / config["output_dir"]), "summary": summary, "manifest": manifest, "gates": gates,
            "recommendation": recommendation, "agent_results": agent_results,
            "position_recommendations": position_recommendations, "buy_candidates": buy_candidates,
            "direct_mapping_audit": direct_mapping_audit}


def resolve_forward_evidence(timing: dict[str, Any], selection: dict[str, Any], goal: dict[str, Any],
                             promotion: dict[str, Any], detector: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]:
    timing_ready = verified_forward_timing_ready(promotion)
    industry_ready = verified_forward_industry_ready(promotion)
    forward_ready = verified_forward_evidence_ready(promotion)
    effective_timing = {**timing, "latest_signal_triggered": int(detector.get("appendable_signal_count", 0) or 0) > 0}
    if detector.get("as_of_date"):
        effective_timing["as_of_date"] = detector["as_of_date"]
    if detector.get("latest_signal_date"):
        effective_timing["latest_panel_date"] = detector["latest_signal_date"]
    if timing_ready:
        effective_timing.update({"production_ready": True, "blocking_issue_count": 0, "evidence_route": "forward_validated"})
    effective_selection = promotion if industry_ready else selection
    effective_goal = {**goal, "verified_forward_evidence_ready": forward_ready}
    return effective_timing, effective_selection, effective_goal, forward_ready


def active_forward_candidates(rows: list[dict[str, str]], promotion: dict[str, Any]) -> list[dict[str, str]]:
    if not verified_forward_industry_ready(promotion):
        return []
    best_rule = str(promotion.get("best_rule", ""))
    return [row for row in rows if row.get("frozen_rule") == best_rule]


def build_buy_candidates(industry_rows: list[dict[str, str]], etf_lookup: dict[str, dict[str, str]],
                         exposure_mapping: list[dict[str, str]], as_of: date, window_active: bool, blockers: list[str], limits: dict[str, Any],
                         account: dict[str, Any]) -> list[dict[str, Any]]:
    by_index: dict[str, list[dict[str, str]]] = {}
    for etf in etf_lookup.values():
        if not etf_is_eligible_as_of(etf, as_of) or etf.get("mapping_status") != "exact_index_code":
            continue
        by_index.setdefault(etf.get("tracked_index_code", ""), []).append(etf)
    by_industry: dict[str, list[dict[str, str]]] = {}
    for row in exposure_mapping:
        if row.get("mapping_status") == "high_confidence_component_exposure":
            etf = etf_lookup.get(str(row.get("etf_code", "")), {})
            if etf and etf_is_eligible_as_of(etf, as_of):
                by_industry.setdefault(str(row.get("industry_code", "")).zfill(6), []).append({**etf, **row})
    prepared = []
    for industry in industry_rows:
        code = str(industry.get("industry_code", "")).zfill(6)
        matches = by_index.get(code, []) or by_industry.get(code, [])
        matches = sorted(matches, key=lambda row: (safe_float(row.get("dominant_industry_weight")), safe_float(row.get("scale_cny_100m"))), reverse=True)
        etf = matches[0] if matches else {}
        prepared.append((industry, code, etf))
    current_weights = position_weights(account)
    current_strategy_weight = sum(current_weights.values())
    mapped_count = sum(bool(etf) for _, _, etf in prepared)
    equity = safe_float(account.get("total_equity"))
    cash_capacity = safe_float(account.get("cash")) / equity - float(limits.get("minimum_cash_weight", 0.0)) if equity > 0 else 1.0
    total_capacity = max(min(float(limits.get("max_strategy_weight", 1.0)) - current_strategy_weight, cash_capacity), 0.0)
    capacity_each = total_capacity / max(mapped_count, 1)
    max_single = float(limits.get("max_single_etf_weight", 1.0))
    rows = []
    for industry, code, etf in prepared:
        reasons = list(f"gate:{name}" for name in blockers)
        current_weight = current_weights.get(etf.get("etf_code", ""), 0.0) if etf else 0.0
        target_weight = min(current_weight + capacity_each, max_single) if etf else None
        if not etf:
            action = "WATCH_NO_TRADEABLE_ETF"
            reasons.append("no_exact_official_index_code_match")
        elif target_weight is not None and target_weight <= current_weight:
            action = "REVIEW_REQUIRED"
            reasons.append("portfolio_capacity_exhausted")
        elif blockers or not window_active:
            action = "WATCH"
            if not window_active:
                reasons.append("rebound_window_inactive")
        else:
            action = "BUY_CANDIDATE"
            reasons.append("all_research_and_mapping_gates_passed")
        rows.append({
            "recommendation_type": "buy_candidate",
            "industry_code": code,
            "industry_name": industry.get("industry_name", ""),
            "signal_date": industry.get("trade_date", ""),
            "etf_code": etf.get("etf_code", ""),
            "etf_name": etf.get("fund_name", ""),
            "tracked_index_code": etf.get("tracked_index_code", ""),
            "mapping_status": etf.get("mapping_status", "missing_index_identity"),
            "action": action,
            "action_reason_codes": reasons,
            "current_weight": current_weight,
            "target_model_weight": target_weight,
            "suggested_weight_change": target_weight - current_weight if target_weight is not None else None,
            "human_confirmation_required": True,
        })
    return rows


def etf_is_eligible_as_of(etf: dict[str, str], as_of: date) -> bool:
    if etf.get("eligible_stock_etf", "").lower() != "true":
        return False
    list_date = parse_date(etf.get("list_date", ""))
    delist_date = parse_date(etf.get("delist_date", ""))
    return not (list_date and list_date > as_of or delist_date and delist_date <= as_of)


def build_direct_mapping_audit(industry_codes: list[str], etf_lookup: dict[str, dict[str, str]],
                               exposure_mapping: list[dict[str, str]]) -> list[dict[str, Any]]:
    exact = {}
    for etf in etf_lookup.values():
        if etf.get("eligible_stock_etf", "").lower() == "true" and etf.get("mapping_status") == "exact_index_code":
            exact.setdefault(etf.get("tracked_index_code", ""), []).append(etf.get("etf_code", ""))
    exposure: dict[str, list[str]] = {}
    for row in exposure_mapping:
        if row.get("mapping_status") == "high_confidence_component_exposure":
            exposure.setdefault(str(row.get("industry_code", "")).zfill(6), []).append(str(row.get("etf_code", "")))
    rows = []
    for code in sorted(set(industry_codes)):
        codes = exact.get(code, []) or sorted(set(exposure.get(code, [])))
        status = "exact_index_code" if code in exact else ("high_confidence_component_exposure" if codes else "no_direct_official_match")
        rows.append({"industry_code": code, "mapping_status": status, "matched_etf_count": len(codes), "matched_etf_codes": ";".join(codes)})
    return rows


def build_position_recommendations(account: dict[str, Any], as_of: date, window_active: bool,
                                   blockers: list[str], rules: dict[str, Any], history_dir: Path,
                                   etf_master_path: Path) -> list[dict[str, Any]]:
    lookup = latest_etf_lookup(etf_master_path, as_of)
    max_days = int(rules.get("max_holding_trading_days", 20))
    rows = []
    for position in account.get("positions", []):
        code = str(position["etf_code"])
        shares = int(position["shares"])
        price = float(position["market_price"])
        current_weight = shares * price / float(account["total_equity"])
        held_days = trading_days_held(history_dir / f"{code}.csv", position["entry_date"], as_of)
        stop = float(position["protective_stop_price"]) if position.get("protective_stop_price") is not None else None
        reasons = []
        hard_exit = stop is not None and price <= stop
        expired = held_days is not None and held_days >= max_days
        execution = position_execution_checks(position, rules)
        execution_quality_clear = not execution["missing_fields"] and all(
            execution[name] == "pass" for name in ("liquidity_status", "premium_discount_status", "spread_status")
        )
        if blockers:
            action = "REVIEW_REQUIRED"
            reasons.extend(f"gate:{name}" for name in blockers)
            if hard_exit:
                reasons.append("protective_stop_breached_manual_exit_review")
            if expired:
                reasons.append("max_holding_days_reached_manual_exit_review")
        elif hard_exit or expired:
            action = "EXIT"
            reasons.append("protective_stop_breached" if hard_exit else "max_holding_days_reached")
        elif not execution_quality_clear:
            action = "REVIEW_REQUIRED"
            reasons.extend(execution["reason_codes"])
        elif execution["retention_status"] != "pass":
            action = "REDUCE"
            reasons.append("industry_rank_outside_retention_zone")
        elif not window_active:
            action = "REDUCE"
            reasons.append("rebound_window_inactive")
        else:
            action = "HOLD"
            reasons.append("window_active_and_exit_rules_clear")
        identity = lookup.get(code, {})
        rows.append({
            "recommendation_type": "position_review",
            "etf_code": code,
            "etf_name": identity.get("fund_name", ""),
            "tracked_index_code": identity.get("tracked_index_code", ""),
            "mapping_status": identity.get("mapping_status", "missing"),
            "action": action,
            "action_reason_codes": reasons,
            "shares": shares,
            "sellable_shares": int(position["sellable_shares"]),
            "entry_date": position["entry_date"],
            "holding_trading_days": held_days,
            "reference_price": price,
            "protective_stop_price": stop,
            "current_weight": current_weight,
            "target_model_weight": 0.0 if action == "EXIT" else current_weight if action in {"HOLD", "REVIEW_REQUIRED"} else None,
            "suggested_weight_change": -current_weight if action == "EXIT" else 0.0 if action in {"HOLD", "REVIEW_REQUIRED"} else None,
            "max_holding_days": max_days,
            "exit_rules": ["protective_stop_price", "max_holding_trading_days", "rebound_window_inactive", "industry_rank_retention", "execution_quality"],
            **execution,
            "tracking_status": "official_mapping_only" if identity.get("mapping_status") == "exact_index_code" else "not_checked",
            "human_confirmation_required": True,
        })
    return rows


def position_execution_checks(position: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    required = ("bid_price", "ask_price", "iopv", "average_daily_amount_20d", "current_industry_rank")
    missing = [name for name in required if position.get(name) is None]
    if missing:
        return {"missing_fields": missing, "reason_codes": [f"missing_execution_field:{name}" for name in missing],
                "spread_bps": None, "iopv_premium": None, "liquidity_status": "not_checked",
                "premium_discount_status": "not_checked", "spread_status": "not_checked", "retention_status": "not_checked"}
    bid, ask, iopv = float(position["bid_price"]), float(position["ask_price"]), float(position["iopv"])
    amount, rank = float(position["average_daily_amount_20d"]), int(position["current_industry_rank"])
    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 10000
    premium = mid / iopv - 1
    statuses = {
        "liquidity_status": "pass" if amount >= float(rules.get("minimum_average_daily_amount_20d", 10_000_000)) else "fail",
        "premium_discount_status": "pass" if abs(premium) <= float(rules.get("max_abs_iopv_premium", 0.02)) else "fail",
        "spread_status": "pass" if 0 <= spread_bps <= float(rules.get("max_spread_bps", 30)) else "fail",
        "retention_status": "pass" if rank <= int(rules.get("retention_rank_max", 10)) else "fail",
    }
    return {"missing_fields": [], "reason_codes": [name for name, status in statuses.items() if status == "fail"],
            "spread_bps": spread_bps, "iopv_premium": premium, **statuses}


def latest_etf_lookup(path: Path, as_of: date) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            snapshot_date = parse_date(row.get("snapshot_date", ""))
            available_date = parse_date(row.get("available_date", "")) or snapshot_date
            if snapshot_date and available_date and snapshot_date <= as_of and available_date <= as_of:
                rows.append(row)
    if not rows:
        return {}
    latest = max(parse_date(row["snapshot_date"]) for row in rows)
    return {row["etf_code"]: row for row in rows if parse_date(row["snapshot_date"]) == latest}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def position_weights(account: dict[str, Any]) -> dict[str, float]:
    equity = safe_float(account.get("total_equity"))
    if equity <= 0:
        return {}
    weights: dict[str, float] = {}
    for row in account.get("positions", []):
        code = str(row.get("etf_code", ""))
        weights[code] = weights.get(code, 0.0) + safe_float(row.get("shares")) * safe_float(row.get("market_price")) / equity
    return weights


def trading_days_held(path: Path, entry_date: str, as_of: date) -> int | None:
    entry = parse_date(entry_date)
    if not path.exists() or not entry:
        return None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    dates = []
    for row in rows:
        value = next((row.get(key) for key in ("日期", "date", "trade_date") if row.get(key)), "")
        parsed = parse_date(value)
        if parsed and entry < parsed <= as_of:
            dates.append(parsed)
    return len(set(dates))


def sha256_json(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_source_manifest(sources: dict[str, str], as_of: date, max_stale_days: int, required_industries: int,
                          minimum_fresh_industries: int,
                          minimum_valuation_history_years: int, timing: dict[str, Any],
                          selection: dict[str, Any], pit_methodology: dict[str, Any] | None = None,
                          forward_evidence_summary: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    pit_methodology = pit_methodology or {}
    industry_files = sorted((ROOT / sources["industry_history_dir"]).glob("*.csv"))
    industry_dates = [last_csv_date(path) for path in industry_files]
    industry_dates = [value for value in industry_dates if value]
    majority_date = Counter(industry_dates).most_common(1)[0][0] if industry_dates else None
    fresh_industry_count = count_fresh_dates(industry_dates, as_of, max_stale_days)
    candidate_required = bool(timing.get("latest_signal_triggered")) and int(selection.get("passing_rule_count", 0) or 0) > 0
    valuation_path = ROOT / sources["valuation_history"]
    valuation_start, valuation_end, valuation_industries = valuation_archive_stats(valuation_path)
    rows = [freshness_row("industry_history", majority_date, as_of, max_stale_days, fresh_industry_count >= minimum_fresh_industries, True, f"files={len(industry_files)}; fresh_files={fresh_industry_count}; minimum_required={minimum_fresh_industries}; stale_files={len(industry_dates) - fresh_industry_count}"),
            archive_coverage_row("valuation_history", valuation_start, valuation_end, valuation_industries, as_of,
                                 minimum_valuation_history_years, required_industries, sources["valuation_history"]),
            methodology_source_row(pit_methodology, sources.get("pit_universe_methodology_summary", ""), forward_evidence_summary or {}),
            freshness_row("valuation_snapshot", latest_named_date(ROOT / sources["valuation_snapshot_dir"], "*.csv"), as_of, max_stale_days, True, True, sources["valuation_snapshot_dir"]),
            freshness_row("market_index", latest_file_date(ROOT / sources["market_index_dir"]), as_of, max_stale_days, True, True, sources["market_index_dir"]),
            freshness_row("etf_history", majority_file_date(ROOT / sources["etf_history_dir"], numeric_only=True), as_of, max_stale_days, True, True, sources["etf_history_dir"]),
            freshness_row("etf_pit_master", last_csv_date(ROOT / sources["etf_pit_master"]), as_of, max_stale_days, True, True, sources["etf_pit_master"]),
            freshness_row("timing_evidence", summary_evidence_date(timing), as_of, max_stale_days, True, True, sources["forward_detector_summary"]),
            freshness_row("industry_candidate_evidence", last_csv_date(ROOT / sources["industry_candidate_file"]), as_of, max_stale_days, True, candidate_required, sources["industry_candidate_file"]),
            freshness_row("fund_flow", latest_named_date(ROOT / sources["fund_flow_dir"], "*"), as_of, max_stale_days, True, False, sources["fund_flow_dir"])]
    return rows


def valuation_archive_stats(path: Path) -> tuple[date | None, date | None, int]:
    if not path.exists():
        return None, None, 0
    first = last = None
    industries = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("source") == "recovered_from_v2_5_quality_components":
                continue
            current = parse_date(row.get("trade_date", ""))
            if current:
                first = current if first is None or current < first else first
                last = current if last is None or current > last else last
            if row.get("industry_code"):
                industries.add(str(row["industry_code"]).zfill(6))
    return first, last, len(industries)


def methodology_source_row(summary: dict[str, Any], detail: str, forward_evidence_summary: dict[str, Any]) -> dict[str, Any]:
    historical_ready = bool(summary.get("promotion_gate_passed"))
    control_ready = bool(summary.get("audit_passed")) and bool(summary.get("methodology_remediation_complete"))
    forward_evidence_ready = verified_forward_evidence_ready(forward_evidence_summary)
    route_ready = methodology_route_ready(summary, forward_evidence_summary)
    latest = parse_date(str(summary.get("valuation_direct_source_max_trade_date", "")))
    return {
        "source": "pit_valuation_methodology",
        "latest_date": latest.isoformat() if latest else "",
        "age_calendar_days": "",
        "coverage_ok": control_ready,
        "required": True,
        "status": "pass" if route_ready else "fail",
        "detail": (
            f"{detail}; audit_passed={summary.get('audit_passed')}; "
            f"historical_promotion_gate={historical_ready}; true_forward_route={forward_evidence_ready}; "
            f"eligible_valuation_rows={summary.get('promotion_eligible_valuation_row_count')}; "
            f"availability={summary.get('valuation_availability_status')}; direct_source_cutoff={summary.get('valuation_direct_source_max_trade_date')}"
        ),
    }


def archive_coverage_row(source: str, start: date | None, end: date | None, industry_count: int,
                         as_of: date, minimum_years: int, required_industries: int, detail: str) -> dict[str, Any]:
    span_days = (end - start).days if start and end and start <= end <= as_of else 0
    coverage_ok = span_days >= minimum_years * 365 and industry_count >= required_industries
    return {"source": source, "latest_date": end.isoformat() if end else "",
            "age_calendar_days": (as_of - end).days if end and end <= as_of else "",
            "coverage_ok": coverage_ok, "required": True, "status": "pass" if coverage_ok else "fail",
            "detail": f"{detail}; start={start}; years={span_days / 365:.1f}; industries={industry_count}/{required_industries}; current snapshot audited separately"}


def freshness_row(source: str, latest: date | None, as_of: date, max_stale_days: int, coverage_ok: bool, required: bool, detail: str) -> dict[str, Any]:
    age = (as_of - latest).days if latest and latest <= as_of else None
    ok = coverage_ok and age is not None and age <= max_stale_days
    return {"source": source, "latest_date": latest.isoformat() if latest else "", "age_calendar_days": age if age is not None else "", "coverage_ok": coverage_ok, "required": required, "status": "pass" if ok else "fail", "detail": detail}


def count_fresh_dates(values: list[date], as_of: date, max_stale_days: int) -> int:
    return sum(0 <= (as_of - value).days <= max_stale_days for value in values)


def last_csv_date(path: Path) -> date | None:
    if not path.exists():
        return None
    values = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            parsed = next(
                (parse_date(row[key]) for key in ("trade_date", "snapshot_date", "日期", "date") if row.get(key)),
                None,
            )
            if parsed:
                values.append(parsed)
    return max(values, default=None)


def latest_file_date(directory: Path, numeric_only: bool = False) -> date | None:
    values = []
    for path in directory.glob("*.csv") if directory.exists() else []:
        if numeric_only and not path.stem.isdigit():
            continue
        value = last_csv_date(path)
        if value:
            values.append(value)
    return max(values, default=None)


def majority_file_date(directory: Path, numeric_only: bool = False) -> date | None:
    values = []
    for path in directory.glob("*.csv") if directory.exists() else []:
        if numeric_only and not path.stem.isdigit():
            continue
        value = last_csv_date(path)
        if value:
            values.append(value)
    return Counter(values).most_common(1)[0][0] if values else None


def summary_evidence_date(summary: dict[str, Any]) -> date | None:
    for key in ("data_cutoff_date", "latest_panel_date", "as_of_date"):
        value = parse_date(str(summary.get(key, "")))
        if value:
            return value
    return None


def latest_named_date(directory: Path, pattern: str) -> date | None:
    values = [parse_date(path.stem) for path in directory.glob(pattern)] if directory.exists() else []
    return max((value for value in values if value), default=None)


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def gate(name: str, passed: bool, evidence: str, veto: bool) -> dict[str, Any]:
    return {"gate": name, "status": "pass" if passed else "fail", "veto": veto, "evidence": evidence}


def gate_passed(gates: list[dict[str, Any]], name: str) -> bool:
    return any(row["gate"] == name and row["status"] == "pass" for row in gates)


def choose_action(gates: list[dict[str, Any]], *, has_position: bool = False, window_active: bool = False,
                  retention_pass: bool = True, hard_exit: bool = False) -> str:
    failed = {row["gate"] for row in gates if row["veto"] and row["status"] != "pass"}
    if "data_freshness" in failed:
        return "BLOCKED_DATA"
    if "pit_universe_methodology" in failed:
        return "NO_ACTION"
    if "timing_robustness" in failed:
        return "NO_ACTION"
    if "industry_selection" in failed or "goal_evidence" in failed:
        return "WATCH"
    if "current_industry_candidates" in failed:
        return "WATCH"
    if "etf_pit_master" in failed:
        return "WATCH_NO_TRADEABLE_ETF"
    if "account_state" in failed:
        return "REVIEW_REQUIRED"
    if "portfolio_risk" in failed or "projected_portfolio_risk" in failed:
        return "REDUCE" if has_position else "REVIEW_REQUIRED"
    if "agent_veto_chain" in failed:
        return "REVIEW_REQUIRED"
    if has_position and hard_exit:
        return "EXIT"
    if has_position and not window_active:
        return "REDUCE"
    if has_position:
        return "HOLD" if retention_pass else "REDUCE"
    return "BUY_CANDIDATE" if window_active else "NO_ACTION"


def validate_account_state(account: dict[str, Any], as_of: date) -> list[str]:
    if not account:
        return ["missing"]
    errors = []
    if account.get("configured") is not True:
        errors.append("not_configured")
    account_date = parse_date(str(account.get("as_of_date", "")))
    if account_date != as_of:
        errors.append("stale_as_of_date")
    try:
        equity = float(account.get("total_equity", 0))
        cash = float(account.get("cash", -1))
        if equity <= 0 or cash < 0 or cash > equity:
            errors.append("invalid_cash_or_equity")
    except (TypeError, ValueError):
        errors.append("invalid_cash_or_equity")
    try:
        peak = float(account.get("peak_equity", 0))
        max_drawdown = float(account.get("max_acceptable_drawdown", 0))
        if peak < equity or not 0 < max_drawdown <= 1:
            errors.append("invalid_peak_or_drawdown_limit")
    except (TypeError, ValueError, UnboundLocalError):
        errors.append("invalid_peak_or_drawdown_limit")
    for position in account.get("positions", []):
        if not str(position.get("etf_code", "")).isdigit() or len(str(position.get("etf_code", ""))) != 6:
            errors.append("invalid_position_code")
        if int(position.get("sellable_shares", 0) or 0) > int(position.get("shares", 0) or 0):
            errors.append("sellable_exceeds_shares")
        if not parse_date(str(position.get("entry_date", ""))):
            errors.append("missing_or_invalid_entry_date")
        elif parse_date(str(position["entry_date"])) > as_of:
            errors.append("future_entry_date")
        try:
            if float(position.get("cost_price", 0)) <= 0 or float(position.get("market_price", 0)) <= 0:
                errors.append("invalid_position_price")
            if position.get("protective_stop_price") is not None and float(position["protective_stop_price"]) <= 0:
                errors.append("invalid_protective_stop_price")
        except (TypeError, ValueError):
            errors.append("invalid_position_price")
        execution_values = [position.get(name) for name in ("bid_price", "ask_price", "iopv", "average_daily_amount_20d", "current_industry_rank")]
        if any(value is not None for value in execution_values):
            if any(value is None for value in execution_values):
                errors.append("incomplete_execution_snapshot")
            else:
                try:
                    if min(float(position["bid_price"]), float(position["ask_price"]), float(position["iopv"])) <= 0 or float(position["bid_price"]) > float(position["ask_price"]):
                        errors.append("invalid_execution_prices")
                    if float(position["average_daily_amount_20d"]) < 0 or int(position["current_industry_rank"]) < 1:
                        errors.append("invalid_execution_metrics")
                except (TypeError, ValueError):
                    errors.append("invalid_execution_metrics")
    return sorted(set(errors))


def portfolio_risk(account: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    equity = float(account["total_equity"])
    result = risk_from_weights(position_weights(account), float(account["cash"]) / equity, limits)
    return add_drawdown_risk(result, account)


def projected_portfolio_risk(account: dict[str, Any], recommendations: list[dict[str, Any]],
                             limits: dict[str, Any]) -> dict[str, Any]:
    weights = position_weights(account)
    current_total = sum(weights.values())
    for row in recommendations:
        code, target = str(row.get("etf_code", "")), row.get("target_model_weight")
        if code and target is not None:
            weights[code] = float(target)
    equity = safe_float(account.get("total_equity"))
    current_cash = safe_float(account.get("cash")) / equity if equity > 0 else 0.0
    projected_cash = current_cash - (sum(weights.values()) - current_total)
    result = risk_from_weights(weights, projected_cash, limits)
    result["projected_weight_change"] = sum(weights.values()) - current_total
    return add_drawdown_risk(result, account)


def add_drawdown_risk(result: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    equity, peak = safe_float(account.get("total_equity")), safe_float(account.get("peak_equity"))
    drawdown = equity / peak - 1.0 if peak > 0 else 0.0
    limit = safe_float(account.get("max_acceptable_drawdown"))
    result["account_drawdown"] = drawdown
    result["max_acceptable_drawdown"] = limit
    if limit > 0 and drawdown < -limit:
        result["breaches"].append("account_drawdown")
        result["risk_gate_passed"] = False
    return result


def risk_from_weights(weights_by_code: dict[str, float], cash_weight: float, limits: dict[str, Any]) -> dict[str, Any]:
    weights = list(weights_by_code.values())
    strategy_weight = sum(weights)
    max_weight = max(weights, default=0.0)
    breaches = []
    if max_weight > float(limits.get("max_single_etf_weight", 1.0)):
        breaches.append("single_etf_weight")
    if strategy_weight > float(limits.get("max_strategy_weight", 1.0)):
        breaches.append("strategy_weight")
    if cash_weight < float(limits.get("minimum_cash_weight", 0.0)):
        breaches.append("minimum_cash_weight")
    return {"strategy_weight": strategy_weight, "max_single_etf_weight": max_weight, "cash_weight": cash_weight,
            "weights_by_etf": weights_by_code,
            "breaches": breaches, "risk_gate_passed": not breaches}


def source_evidence(rows: list[dict[str, Any]], exclude: set[str] | None = None) -> str:
    excluded = exclude or set()
    return "; ".join(
        f"{row['source']}={row['status']}:{row['latest_date']}"
        for row in rows
        if row["required"] and row["source"] not in excluded
    )


def write_outputs(config: dict[str, Any], result: dict[str, Any]) -> None:
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    write_json(out / "run_summary.json", result["summary"])
    write_json(debug / "recommendation.json", result["recommendation"])
    write_json(debug / "agent_results.json", {"agents": result["agent_results"]})
    write_csv(debug / "gate_results.csv", result["gates"])
    write_csv(debug / "source_manifest.csv", result["manifest"])
    write_csv(debug / "industry_etf_direct_mapping_audit.csv", result["direct_mapping_audit"])
    items = result["buy_candidates"] + result["position_recommendations"]
    rows = [{
        "类型": row.get("recommendation_type", ""), "行业代码": row.get("industry_code", ""), "行业": row.get("industry_name", ""),
        "ETF代码": row.get("etf_code", ""), "ETF名称": row.get("etf_name", ""), "跟踪指数代码": row.get("tracked_index_code", ""),
        "动作": row.get("action", ""), "当前权重": row.get("current_weight"), "模型目标权重": row.get("target_model_weight"),
        "持有交易日": row.get("holding_trading_days"), "保护价": row.get("protective_stop_price"),
        "原因": ";".join(row["action_reason_codes"]),
    } for row in items]
    write_csv(out / "top_candidates.csv", rows, ["类型", "行业代码", "行业", "ETF代码", "ETF名称", "跟踪指数代码", "动作", "当前权重", "模型目标权重", "持有交易日", "保护价", "原因"])
    (out / "report.md").write_text(render_report(result), encoding="utf-8")


def render_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "# ETF量化辅助交易当前主线",
        "",
        summary["final_verdict"],
        "",
        f"- as-of 日期：{summary['as_of_date']}",
        f"- 当前动作：`{summary['action']}`",
        f"- 阻断门禁：{summary['blocking_gate_count']}",
        f"- 候选数：{summary['candidate_count']}",
        f"- 当前行业候选输入：{summary['industry_candidate_count']}",
        f"- 申万二级代码直接匹配 ETF：{summary['direct_industry_etf_mapping_count']}",
        f"- 持仓建议数：{summary['position_recommendation_count']}",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 门禁",
        "",
        "| 门禁 | 状态 | 是否否决 | 证据 |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(f"| {row['gate']} | {row['status']} | {str(row['veto']).lower()} | {row['evidence']} |" for row in result["gates"])
    lines += ["", "边界：当前主线只生成研究状态和人工复核建议，不自动下单。"]
    return "\n".join(lines)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    fieldnames = fields or (list(rows[0]) if rows else [])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def self_check() -> None:
    refresh = [" ".join(command) for command in refresh_input_commands(date(2026, 7, 13))]
    assert len(refresh) == 16
    assert "scripts/run_industry_rebound_window_v3_4_realtime_model.py --refresh-market-index-only" in refresh
    assert refresh.index("scripts/audit_pit_universe_methodology.py") < refresh.index("scripts/build_v5_11_rebound_leader_pit_valuation_audit.py") < refresh.index("scripts/build_v5_12_rebound_leader_pit_valuation_percentile_audit.py") < refresh.index("scripts/build_v5_20_rebound_leader_evidence_boundary_audit.py") < refresh.index("scripts/build_v5_10_rebound_leader_goal_completion_audit.py")
    assert refresh.index("scripts/build_v5_08_rebound_leader_forward_signal_detector.py --as-of-date 2026-07-13 --apply") < refresh.index("scripts/settle_v5_06_rebound_leader_forward_samples.py --as-of-date 2026-07-13") < refresh.index("scripts/build_v5_07_rebound_leader_promotion_evaluator.py") < refresh.index("scripts/build_v5_21_rebound_leader_new_pit_source_discovery.py") < refresh.index("scripts/build_v5_10_rebound_leader_goal_completion_audit.py")
    assert refresh[-1] == "scripts/build_v5_10_rebound_leader_goal_completion_audit.py"
    assert not any("fund_flow" in command or "v4_72" in command for command in refresh)
    assert count_fresh_dates([date(2026, 7, 14), date(2026, 7, 10), date(2026, 7, 15)], date(2026, 7, 14), 4) == 2
    valid_methodology = {
        "audit_passed": True,
        "methodology_remediation_complete": True,
        "legacy_oos_label_corrected": True,
        "historical_review_set_label": "historical_review_used_in_iteration",
        "valuation_required_fields": ["trade_date", "published_at", "available_date", "fetched_at", "source_version", "revision_status"],
        "policy_status": "research_only",
        "production_ready": False,
        "auto_execution_allowed": False,
        "promotion_gate_passed": False,
        "historical_valuation_pit_gate_passed": False,
        "historical_classification_gate_passed": False,
        "promotion_eligible_valuation_row_count": 0,
        "valuation_availability_status": "unavailable_for_promotion",
        "classification_history_status": "unavailable",
        "valuation_direct_source_max_trade_date": "2025-12-31",
    }
    assert not methodology_route_ready(valid_methodology, {"forward_evidence_integrity_passed": True})
    assert not methodology_route_ready({**valid_methodology, "audit_passed": False}, {"forward_evidence_integrity_passed": True})
    assert methodology_source_row({**valid_methodology, "audit_passed": False}, "fixture", {})["status"] == "fail"
    failed_data = [gate("data_freshness", False, "x", True)]
    assert choose_action(failed_data) == "BLOCKED_DATA"
    assert choose_action([gate("pit_universe_methodology", False, "x", True)]) == "NO_ACTION"
    ready = [gate(name, True, "ok", True) for name in ["data_freshness", "timing_robustness", "industry_selection", "etf_pit_master", "account_state", "portfolio_risk", "goal_evidence"]]
    assert choose_action(ready) == "NO_ACTION"
    assert choose_action(ready, window_active=True) == "BUY_CANDIDATE"
    assert choose_action(ready, has_position=True, window_active=True) == "HOLD"
    assert choose_action(ready, has_position=True, window_active=False) == "REDUCE"
    assert choose_action(ready + [gate("current_industry_candidates", False, "x", True)], window_active=True) == "WATCH"
    verified_promotion = {
        "version": "5.07.0",
        "policy_id": "rebound_leader_promotion_evaluator_v5_07",
        "policy_status": "research_only",
        "auto_execution_allowed": False,
        "forward_evidence_integrity_model": "append_only_hash_chain_v1",
        "forward_evidence_integrity_passed": True,
        "forward_evidence_start": "2026-07-12",
        "historical_backfill_eligible_count": 0,
        "rule_mutation_detected": False,
        "forward_ledger_head_hash": "a" * 64,
        "forward_timing_gate_passed": True,
        "forward_timing_event_count": 20,
        "forward_timing_mean_return": 0.01,
        "forward_timing_median_return": 0.01,
        "forward_timing_win_rate": 0.60,
        "passing_rule_count": 1,
        "can_claim_strong_rebound_industries": True,
        "best_status": "pass_rebound_leader_promotion_gate",
        "best_event_count": 30,
        "best_forward_event_count": 12,
        "forward_settled_event_count": 20,
        "best_mean_relative_return": 0.02,
        "best_top_quintile_hit_rate": 0.35,
        "best_rule": "quality_score_ge3",
    }
    effective = resolve_forward_evidence(
        {"production_ready": False, "blocking_issue_count": 3}, {"passing_rule_count": 0},
        {"goal_ready": False, "blocking_nonpass_count": 4},
        verified_promotion,
        {"appendable_signal_count": 1, "latest_signal_date": "2026-07-11"},
    )
    assert not effective[3]
    assert effective[0]["production_ready"] is False
    assert effective[1]["passing_rule_count"] == 0
    assert effective[2]["goal_ready"] is False and effective[2]["blocking_nonpass_count"] == 4
    effective_goal_ready = resolve_forward_evidence(
        {"production_ready": False, "blocking_issue_count": 3}, {"passing_rule_count": 0},
        {"goal_ready": True, "blocking_nonpass_count": 0}, verified_promotion,
        {"appendable_signal_count": 1, "latest_signal_date": "2026-07-11"},
    )
    assert len(active_forward_candidates([{"frozen_rule": "quality_score_ge3"}], effective[1])) == 0
    agents = run_veto_chain({
        "source_manifest": [], "etf_pit": {"pit_master_ready": True, "current_mapping_ready": True},
        "timing": {"production_ready": True, "blocking_issue_count": 0, "latest_signal_triggered": True},
        "selection": {"passing_rule_count": 1},
        "goal": {"goal_ready": True, "blocking_nonpass_count": 0},
        "replay": {"cross_check_passed": True, "external_event_engine_cross_check": "pass"},
        "account_errors": [], "portfolio_risk": {"risk_gate_passed": True, "breaches": []},
        "direct_industry_etf_mapping_count": 1, "experiment_ledger": {"integrity_passed": True, "experiment_count": 2},
    })
    assert all(item["status"] == "pass" for item in agents)
    account = {"configured": True, "as_of_date": "2026-07-11", "cash": 80000, "total_equity": 100000,
               "peak_equity": 105000, "max_acceptable_drawdown": 0.10,
                "positions": [{"etf_code": "510300", "shares": 1000, "sellable_shares": 1000, "cost_price": 5.2,
                               "market_price": 5.0, "entry_date": "2026-07-01", "protective_stop_price": 4.8,
                               "bid_price": 4.999, "ask_price": 5.001, "iopv": 5.0,
                               "average_daily_amount_20d": 100000000, "current_industry_rank": 5}]}
    assert not validate_account_state(account, date(2026, 7, 11))
    assert portfolio_risk(account, {"max_single_etf_weight": 0.08, "max_strategy_weight": 0.20, "minimum_cash_weight": 0.10})["risk_gate_passed"]
    assert parse_date("2026-07-11") == date(2026, 7, 11)
    assert summary_evidence_date({"latest_panel_date": "2026-07-10"}) == date(2026, 7, 10)
    assert summary_evidence_date({"generated_at": "2026-07-11T20:00:00"}) is None
    archive = archive_coverage_row("valuation_history", date(2015, 1, 1), date(2026, 6, 12), 131,
                                   date(2026, 7, 12), 8, 131, "x")
    assert archive["status"] == "pass"
    review = build_position_recommendations(account, date(2026, 7, 11), True, ["data_freshness"],
                                            {"max_holding_trading_days": 20}, Path("missing"), Path("missing"))
    assert review[0]["action"] == "REVIEW_REQUIRED"
    exit_row = build_position_recommendations(account, date(2026, 7, 11), True, [],
                                              {"max_holding_trading_days": 20}, Path("missing"), Path("missing"))
    assert exit_row[0]["action"] == "HOLD"
    weak_rank = json.loads(json.dumps(account)); weak_rank["positions"][0]["current_industry_rank"] = 11
    assert build_position_recommendations(weak_rank, date(2026, 7, 11), True, [],
                                          {"retention_rank_max": 10}, Path("missing"), Path("missing"))[0]["action"] == "REDUCE"
    stopped = json.loads(json.dumps(account))
    stopped["positions"][0]["market_price"] = 4.7
    assert build_position_recommendations(stopped, date(2026, 7, 11), True, [], {}, Path("missing"), Path("missing"))[0]["action"] == "EXIT"
    mapped = {"510300": {"etf_code": "510300", "fund_name": "沪深300ETF", "eligible_stock_etf": "True",
                         "mapping_status": "exact_index_code", "tracked_index_code": "000300", "list_date": "2012-05-28",
                         "delist_date": "", "scale_cny_100m": "100"}}
    limits = {"max_single_etf_weight": 0.08, "max_strategy_weight": 0.20, "minimum_cash_weight": 0.10}
    buys = build_buy_candidates([{"industry_code": "000300", "industry_name": "沪深300", "trade_date": "2026-07-10"}], mapped,
                                [], date(2026, 7, 11), True, [], limits, {})
    assert buys[0]["action"] == "BUY_CANDIDATE" and buys[0]["etf_code"] == "510300"
    assert buys[0]["target_model_weight"] == 0.08
    missing = build_buy_candidates([{"industry_code": "801010", "industry_name": "农业"}], mapped, [], date(2026, 7, 11), True, [], limits, {})
    assert missing[0]["action"] == "WATCH_NO_TRADEABLE_ETF"
    projected = projected_portfolio_risk(account, [{"etf_code": "510300", "target_model_weight": 0.08}], limits)
    assert projected["risk_gate_passed"] and projected["strategy_weight"] == 0.08
    drawn = json.loads(json.dumps(account)); drawn["total_equity"] = 80000; drawn["cash"] = 75000
    assert "account_drawdown" in portfolio_risk(drawn, limits)["breaches"]
    print("self_check=pass")


if __name__ == "__main__":
    main()
