from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import append_v5_05_rebound_leader_forward_sample as v505
import audit_official_etf_lifecycle_sources as lifecycle
import build_etf_pit_master as pit
import record_etf_paper_decision as paper
import run_etf_assisted_trading_current as current
import run_etf_realistic_execution_replay as replay
import run_v4_71_live_refresh as live_refresh
import settle_v5_06_rebound_leader_forward_samples as v506


def pit_row(
    snapshot_date: str,
    *,
    code: str = "510300",
    available_date: str | None = None,
    mapping_status: str = "exact_index_code",
    eligible: bool = True,
) -> dict[str, str]:
    row = {field: "" for field in pit.FIELDS}
    row.update(
        {
            "snapshot_date": snapshot_date,
            "available_date": available_date or snapshot_date,
            "etf_code": code,
            "exchange": "SSE",
            "fund_name": f"测试ETF-{code}",
            "fund_type": "F111",
            "investment_type": "境内股票ETF",
            "tracked_index_code": "000300",
            "tracked_index_name": "沪深300",
            "list_date": "2012-05-28",
            "source": "offline_fixture",
            "source_url": "https://example.invalid/fixture",
            "mapping_status": mapping_status,
            "mapping_source": "offline_fixture",
            "eligible_stock_etf": str(eligible),
            "record_hash": "f" * 64,
        }
    )
    return row


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def account_fixture(as_of: str = "2026-07-18") -> dict[str, object]:
    return {
        "configured": True,
        "as_of_date": as_of,
        "cash": 100_000,
        "total_equity": 100_000,
        "peak_equity": 100_000,
        "max_acceptable_drawdown": 0.10,
        "positions": [],
    }


def test_contract_live_summary_never_enables_auto_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_read_json(path: Path) -> dict[str, object]:
        if path == live_refresh.V471 / "run_summary.json":
            return {"latest_signal_triggered": True, "production_ready": True}
        if path == live_refresh.V472 / "run_summary.json":
            return {"best_status": "validated", "auto_execution_allowed": True}
        return {}

    monkeypatch.setattr(live_refresh, "read_json", fake_read_json)

    summary = live_refresh.build_daily_decision_summary()

    assert summary["decision_state"] == "manual_review_required"
    assert summary["auto_execution_allowed"] is False


def test_contract_paper_recorder_rejects_auto_execution_enabled() -> None:
    recommendation = {
        "recommendation_id": "r1",
        "data_cutoff_date": "2026-07-18",
        "policy_hash": "a" * 64,
        "action": "NO_ACTION",
        "risk_vetoes": [],
        "human_confirmation_required": True,
        "auto_execution_allowed": True,
    }
    values = {"decision": "ACCEPT", "operator": "tester", "note": "", "executed_action": "NO_ACTION"}

    with pytest.raises(ValueError, match="禁止自动执行"):
        paper.build_record(recommendation, values)


def test_data_quality_degraded_same_day_refresh_preserves_last_good_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = tmp_path / "etf_pit_master.csv"
    monkeypatch.setattr(pit, "MASTER", master)
    good = pit_row("2026-07-18")
    write_csv(master, [good])
    before = master.read_bytes()
    degraded = pit_row("2026-07-18", mapping_status="index_name_only")

    merged, effective, accepted, reason = pit.append_snapshot([degraded])

    assert accepted is False
    assert reason == "degraded_refresh_same_date_retained"
    assert effective[0]["mapping_status"] == "exact_index_code"
    assert len(merged) == 1
    assert master.read_bytes() == before


def test_data_quality_network_failure_preserves_last_good_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = tmp_path / "etf_pit_master.csv"
    monkeypatch.setattr(pit, "MASTER", master)
    write_csv(master, [pit_row("2026-07-18")])
    before = master.read_bytes()

    def fail_fetch(_snapshot_date: date) -> tuple[list[dict[str, object]], list[Path]]:
        raise requests.ConnectionError("offline fixture")

    summary, rows = pit.refresh_snapshot(date(2026, 7, 19), fetcher=fail_fetch)

    assert summary["network_fetch_succeeded"] is False
    assert summary["refresh_accepted"] is False
    assert summary["refresh_reason"] == "network_failure_last_good_retained"
    assert rows[0]["snapshot_date"] == "2026-07-18"
    assert master.read_bytes() == before


def test_data_quality_out_of_order_or_future_dates_fail_freshness(tmp_path: Path) -> None:
    path = tmp_path / "history.csv"
    write_csv(
        path,
        [
            {"trade_date": "2026-07-17", "value": 1},
            {"trade_date": "2026-07-20", "value": 2},
            {"trade_date": "2026-07-18", "value": 3},
        ],
    )

    latest = current.last_csv_date(path)
    freshness = current.freshness_row("fixture", latest, date(2026, 7, 18), 4, True, True, str(path))

    assert latest == date(2026, 7, 20)
    assert freshness["status"] == "fail"


def test_unit_latest_etf_lookup_respects_snapshot_and_available_date(tmp_path: Path) -> None:
    path = tmp_path / "pit.csv"
    write_csv(
        path,
        [
            {"snapshot_date": "2026-07-16", "available_date": "2026-07-16", "etf_code": "510300", "fund_name": "known"},
            {"snapshot_date": "2026-07-17", "available_date": "2026-07-19", "etf_code": "510500", "fund_name": "future"},
        ],
    )

    lookup = current.latest_etf_lookup(path, date(2026, 7, 18))

    assert set(lookup) == {"510300"}
    assert lookup["510300"]["fund_name"] == "known"


def test_data_quality_lifecycle_inventory_uses_only_as_of_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "ROOT", tmp_path)
    write_csv(
        tmp_path / "data_catalog" / "etf_pit_master.csv",
        [
            {"snapshot_date": "2026-07-18", "exchange": "SSE", "etf_code": "510300", "list_date": "2012-05-28", "eligible_stock_etf": "True"},
            {"snapshot_date": "2026-07-20", "exchange": "SSE", "etf_code": "510500", "list_date": "2013-03-15", "eligible_stock_etf": "True"},
        ],
    )
    announcements = pd.DataFrame(
        [
            {"exchange": "SSE", "etf_code": "510300", "event_type": "listing", "announcement_date": "2012-05-25", "code_available": True, "etf_announcement": True},
            {"exchange": "SSE", "etf_code": "510500", "event_type": "listing", "announcement_date": "2026-07-17", "code_available": True, "etf_announcement": True},
        ]
    )

    inventory = lifecycle.build_lifecycle_inventory(announcements, {}, date(2026, 7, 18)).set_index("etf_code")

    assert inventory.at["510300", "lifecycle_status"] == "current_active"
    assert inventory.at["510500", "lifecycle_status"] != "current_active"


@pytest.mark.parametrize("route", ["direct", "exposure"])
def test_data_quality_delisted_etf_is_filtered_from_every_mapping_route(route: str) -> None:
    industry_code = "000300" if route == "direct" else "801010"
    etf = {
        "etf_code": "510300",
        "fund_name": "退市ETF",
        "eligible_stock_etf": "True",
        "mapping_status": "exact_index_code",
        "tracked_index_code": "000300" if route == "direct" else "000905",
        "list_date": "2012-05-28",
        "delist_date": "2026-07-18",
        "scale_cny_100m": "100",
    }
    exposure = (
        []
        if route == "direct"
        else [{"industry_code": industry_code, "etf_code": "510300", "mapping_status": "high_confidence_component_exposure", "dominant_industry_weight": "0.8"}]
    )

    rows = current.build_buy_candidates(
        [{"industry_code": industry_code, "industry_name": "测试行业", "trade_date": "2026-07-17"}],
        {"510300": etf},
        exposure,
        date(2026, 7, 18),
        True,
        [],
        {"max_single_etf_weight": 0.20, "max_strategy_weight": 0.50, "minimum_cash_weight": 0.10},
        {},
    )

    assert rows[0]["action"] == "WATCH_NO_TRADEABLE_ETF"
    assert rows[0]["etf_code"] == ""


def test_research_evidence_duplicate_forward_sample_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "forward.csv"
    row = {field: "" for field in v505.FIELDS}
    row.update(
        {
            "frozen_rule": "quality_score_ge2",
            "signal_date": "2026-06-18",
            "entry_date": "2026-06-23",
            "exit_date": "2026-07-21",
            "settlement_status": "pending",
        }
    )
    v505.append_row(path, row)

    with pytest.raises(SystemExit, match="duplicate forward sample"):
        v505.append_row(path, row)


@pytest.mark.parametrize(
    ("signal_date", "entry_date", "exit_date"),
    [
        ("2026-06-24", "2026-06-23", "2026-07-21"),
        ("2026-06-18", "2026-07-21", "2026-06-23"),
        ("2026-06-18", "2026-06-18", "2026-07-21"),
    ],
)
def test_research_evidence_forward_dates_must_be_strictly_ordered(
    signal_date: str, entry_date: str, exit_date: str
) -> None:
    args = argparse.Namespace(
        frozen_rule="quality_score_ge2",
        signal_date=signal_date,
        entry_date=entry_date,
        exit_date=exit_date,
        selected_industries="",
        benchmark_return="",
        selected_net_return="",
        relative_return="",
        top_quintile_hit_rate="",
        settlement_status="pending",
        notes="",
    )

    with pytest.raises(SystemExit, match="signal_date < entry_date < exit_date"):
        v505.build_row(args, {"quality_score_ge2"})


def benchmark_history(count: int) -> pd.DataFrame:
    rows = []
    for index in range(count):
        code = f"{index:06d}"
        name = f"行业{index}"
        rows.extend(
            [
                {"trade_date": date(2026, 6, 23), "industry_code": code, "industry_name": name, "close_index": 100.0},
                {"trade_date": date(2026, 7, 21), "industry_code": code, "industry_name": name, "close_index": 101.0},
            ]
        )
    return pd.DataFrame(rows)


@pytest.mark.parametrize(("coverage", "expected"), [(119, "pending_incomplete_benchmark"), (120, "settled")])
def test_research_evidence_benchmark_coverage_gate_is_behavioral(coverage: int, expected: str) -> None:
    row = {
        "selected_industries": "000000",
        "entry_date": "2026-06-23",
        "exit_date": "2026-07-21",
        "notes": "",
    }

    result = v506.settle_one(row, benchmark_history(coverage))

    assert result["settlement_status"] == expected


def test_unit_execution_gate_rejects_suspension_and_one_price_limit_up() -> None:
    suspended = pd.Series({"amount": 0, "open": 1.0, "low": 1.0, "prev_close": 1.0})
    limit_up = pd.Series({"amount": 1_000, "open": 1.10, "low": 1.10, "prev_close": 1.0})

    assert replay.buyable(suspended, replay.POLICY) is False
    assert replay.buyable(limit_up, replay.POLICY) is False


def test_unit_execution_replay_enforces_t_plus_one_when_exit_is_same_day() -> None:
    prices = replay.prepare_prices(
        pd.DataFrame(
            [
                {"date": "2026-01-02", "open": 1.00, "high": 1.01, "low": 0.99, "close": 1.00, "amount": 1_000},
                {"date": "2026-01-05", "open": 1.01, "high": 1.02, "low": 1.00, "close": 1.01, "amount": 1_000},
            ]
        )
    )
    signal = pd.Series({"signal_date": "2026-01-01", "entry_date": "2026-01-02", "exit_date": "2026-01-02", "stop_loss_level": 0.06})

    result = replay.replay_one(signal, prices, replay.POLICY)

    assert result["status"] == "filled"
    assert result["actual_entry_date"] == "2026-01-02"
    assert result["actual_exit_date"] == "2026-01-05"
    assert result["t_plus_one_respected"] is True


def test_unit_execution_replay_delays_one_price_limit_down_exit() -> None:
    prices = replay.prepare_prices(
        pd.DataFrame(
            [
                {"date": "2026-01-02", "open": 1.00, "high": 1.01, "low": 0.99, "close": 1.00, "amount": 1_000},
                {"date": "2026-01-05", "open": 0.90, "high": 0.90, "low": 0.90, "close": 0.90, "amount": 1_000},
                {"date": "2026-01-06", "open": 0.91, "high": 0.93, "low": 0.90, "close": 0.92, "amount": 1_000},
            ]
        )
    )
    signal = pd.Series({"signal_date": "2026-01-01", "entry_date": "2026-01-02", "exit_date": "2026-01-05", "stop_loss_level": 0.20})

    result = replay.replay_one(signal, prices, replay.POLICY)

    assert result["actual_exit_date"] == "2026-01-06"
    assert result["exit_reason"].endswith("delayed_untradeable")


def test_unit_execution_replay_missing_exit_price_returns_stable_failure() -> None:
    prices = replay.prepare_prices(
        pd.DataFrame(
            [{"date": "2026-01-02", "open": 1.00, "high": 1.01, "low": 0.99, "close": 1.00, "amount": 1_000}]
        )
    )
    signal = pd.Series({"signal_date": "2026-01-01", "entry_date": "2026-01-02", "exit_date": "2026-01-05", "stop_loss_level": 0.06})

    result = replay.replay_one(signal, prices, replay.POLICY)

    assert result["status"] == "failed"
    assert result["failure_reason"] == "no_exit_price"


def test_unit_account_state_rejects_stale_account_and_future_position() -> None:
    account = account_fixture("2026-07-17")
    account["positions"] = [
        {
            "etf_code": "510300",
            "shares": 100,
            "sellable_shares": 0,
            "cost_price": 5.0,
            "market_price": 5.0,
            "entry_date": "2026-07-19",
        }
    ]

    errors = current.validate_account_state(account, date(2026, 7, 18))

    assert "stale_as_of_date" in errors
    assert "future_entry_date" in errors


def test_unit_same_etf_positions_are_aggregated_before_risk_gate() -> None:
    account = account_fixture()
    account["positions"] = [
        {"etf_code": "510300", "shares": 1_000, "market_price": 6.0},
        {"etf_code": "510300", "shares": 1_000, "market_price": 6.0},
    ]
    limits = {"max_single_etf_weight": 0.08, "max_strategy_weight": 0.20, "minimum_cash_weight": 0.10}

    weights = current.position_weights(account)
    risk = current.portfolio_risk(account, limits)

    assert weights["510300"] == pytest.approx(0.12)
    assert "single_etf_weight" in risk["breaches"]


def test_unit_projected_portfolio_risk_recomputes_all_limits() -> None:
    account = account_fixture()
    limits = {"max_single_etf_weight": 0.20, "max_strategy_weight": 0.25, "minimum_cash_weight": 0.10}

    risk = current.projected_portfolio_risk(
        account,
        [{"etf_code": "510300", "target_model_weight": 0.30}],
        limits,
    )

    assert risk["risk_gate_passed"] is False
    assert set(risk["breaches"]) >= {"single_etf_weight", "strategy_weight"}


def test_integration_projected_risk_breach_vetoes_candidate_and_auto_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(current, "ROOT", tmp_path)
    account = account_fixture()
    payloads = {
        "timing.json": {"production_ready": True, "blocking_issue_count": 0},
        "selection.json": {"passing_rule_count": 1, "best_status": "validated"},
        "goal.json": {"goal_ready": True, "blocking_nonpass_count": 0},
        "promotion.json": {"forward_timing_gate_passed": True, "passing_rule_count": 1, "best_rule": "rule_a"},
        "detector.json": {"appendable_signal_count": 1, "latest_signal_date": "2026-07-17", "as_of_date": "2026-07-18"},
        "ledger.json": {"integrity_passed": True, "experiment_count": 1, "ledger_head_hash": "a" * 64},
        "lifecycle.json": {"observed_tradability_universe_ready": True},
        "account.json": account,
    }

    def fake_read_json(path: Path) -> dict[str, object]:
        text = str(path).replace("\\", "/")
        if "etf_pit_master/run_summary.json" in text:
            return {"current_mapping_ready": True, "historical_pit_ready": True, "exact_index_code_coverage": 1.0}
        if "etf_realistic_execution_replay/run_summary.json" in text:
            return {"cross_check_passed": True, "external_event_engine_cross_check": "pass"}
        return payloads.get(Path(path).name, {})

    monkeypatch.setattr(current, "read_json", fake_read_json)
    monkeypatch.setattr(
        current,
        "build_source_manifest",
        lambda *_args, **_kwargs: [{"source": "fixture", "latest_date": "2026-07-18", "status": "pass", "required": True}],
    )
    monkeypatch.setattr(
        current,
        "read_csv_rows",
        lambda path: [{"frozen_rule": "rule_a", "industry_code": "000300", "industry_name": "沪深300", "trade_date": "2026-07-17"}]
        if Path(path).name == "candidates.csv"
        else [],
    )
    monkeypatch.setattr(
        current,
        "latest_etf_lookup",
        lambda *_args: {
            "510300": {
                "etf_code": "510300",
                "fund_name": "沪深300ETF",
                "eligible_stock_etf": "True",
                "mapping_status": "exact_index_code",
                "tracked_index_code": "000300",
                "list_date": "2012-05-28",
                "delist_date": "",
            }
        },
    )
    monkeypatch.setattr(
        current,
        "build_direct_mapping_audit",
        lambda *_args: [{"industry_code": "000300", "mapping_status": "exact_index_code", "matched_etf_count": 1, "matched_etf_codes": "510300"}],
    )

    def overweight_candidate(
        _industries: list[dict[str, str]],
        _lookup: dict[str, dict[str, str]],
        _exposure: list[dict[str, str]],
        _as_of: date,
        _window_active: bool,
        blockers: list[str],
        _limits: dict[str, object],
        _account: dict[str, object],
    ) -> list[dict[str, object]]:
        return [
            {
                "recommendation_type": "buy_candidate",
                "industry_code": "000300",
                "industry_name": "沪深300",
                "etf_code": "510300",
                "action": "WATCH" if blockers else "BUY_CANDIDATE",
                "action_reason_codes": [f"gate:{name}" for name in blockers],
                "target_model_weight": 0.30,
                "human_confirmation_required": True,
            }
        ]

    monkeypatch.setattr(current, "build_buy_candidates", overweight_candidate)
    monkeypatch.setattr(current, "build_position_recommendations", lambda *_args, **_kwargs: [])
    config = {
        "policy_id": "fixture",
        "version": "1.0",
        "policy_status": "research_only",
        "output_dir": "outputs/fixture",
        "max_stale_calendar_days": 4,
        "required_industry_count": 1,
        "minimum_fresh_industry_count": 1,
        "minimum_valuation_history_years": 1,
        "portfolio_limits": {"max_single_etf_weight": 0.20, "max_strategy_weight": 0.25, "minimum_cash_weight": 0.10},
        "position_rules": {},
        "sources": {
            "timing_summary": "timing.json",
            "industry_selection_summary": "selection.json",
            "goal_summary": "goal.json",
            "forward_promotion_summary": "promotion.json",
            "forward_detector_summary": "detector.json",
            "industry_candidate_file": "candidates.csv",
            "experiment_ledger_summary": "ledger.json",
            "etf_lifecycle_summary": "lifecycle.json",
            "account_state": "account.json",
            "etf_pit_master": "pit.csv",
            "etf_sw_industry_mapping": "exposure.csv",
            "industry_history_dir": "industry_history",
            "etf_history_dir": "etf_history",
        },
    }

    result = current.run_pipeline(config, date(2026, 7, 18))

    assert "projected_portfolio_risk" in result["summary"]["blocking_gates"]
    assert result["summary"]["action"] != "BUY_CANDIDATE"
    assert result["buy_candidates"][0]["action"] != "BUY_CANDIDATE"
    assert result["summary"]["projected_portfolio_risk_gate_passed"] is False
    assert result["summary"]["auto_execution_allowed"] is False
    assert result["recommendation"]["auto_execution_allowed"] is False
    assert result["recommendation"]["human_confirmation_required"] is True
