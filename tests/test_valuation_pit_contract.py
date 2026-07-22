from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import run_industry_quality_proxy_v2_5 as v25
import run_industry_rebound_leader_selection_v4_72 as v472
import run_industry_valuation_pit_validation_v2_6 as v26
import valuation_pit_contract as pit_contract
from valuation_pit_contract import (
    CURRENT_SNAPSHOT_STATUS,
    DEFAULT_TRADING_CALENDAR,
    NON_PIT_HISTORY_STATUS,
    SHANGHAI,
    ValuationPITContractError,
    archive_current_snapshot_immutable,
    attach_pit_valuation_asof,
    audit_pit_valuation_history,
    current_snapshot_as_of_error,
    first_eligible_trade_date,
    load_frozen_trading_calendar,
    methodology_route_ready,
    official_valuation_cutoff,
    stamp_current_snapshot,
)


SOURCE_HASH = "881fd31376997a0174429888f1b9aeb199742a7213496efaf95260ad910adbbc"


def pit_row(
    *,
    trade_date: str = "2026-07-17",
    published_at: str = "2026-07-17T14:00:00+08:00",
    available_date: str = "2026-07-17",
    fetched_at: str = "2026-07-17T14:30:00+08:00",
    source_hash: str = SOURCE_HASH,
    source_version: str | None = None,
    source_artifact_path: str = "tests/fixtures/valuation_pit_source.txt",
    revision_status: str = "original",
    pe: float = 10.0,
) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "industry_code": "801010",
        "industry_name": "测试行业",
        "pe": pe,
        "pb": 1.0,
        "dividend_yield": 0.02,
        "published_at": published_at,
        "available_date": available_date,
        "fetched_at": fetched_at,
        "source_hash": source_hash,
        "source_version": source_version or f"sha256:{source_hash}",
        "source_hash_basis": "immutable_raw_artifact_sha256",
        "source_artifact_path": source_artifact_path,
        "revision_status": revision_status,
        "availability_basis": "source_publication_timestamp",
        "data_status": "pit_verified",
        "source": "official_fixture",
    }


def test_frozen_calendar_handles_close_weekend_and_holiday() -> None:
    calendar = load_frozen_trading_calendar()
    assert first_eligible_trade_date("2026-07-17T14:59:59+08:00", calendar).isoformat() == "2026-07-17"
    assert first_eligible_trade_date("2026-07-17T15:00:00+08:00", calendar).isoformat() == "2026-07-20"
    assert first_eligible_trade_date("2026-07-18T10:00:00+08:00", calendar).isoformat() == "2026-07-20"
    assert first_eligible_trade_date("2026-10-01T10:00:00+08:00", calendar).isoformat() == "2026-10-08"


def test_default_frozen_calendar_is_bound_to_content_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tampered = tmp_path / "a_share_trade_calendar.csv"
    pd.DataFrame({"trade_date": ["2026-07-17", "2026-07-20"]}).to_csv(
        tampered, index=False, encoding="utf-8-sig"
    )
    monkeypatch.setattr(pit_contract, "DEFAULT_TRADING_CALENDAR", tampered)
    with pytest.raises(ValuationPITContractError, match="calendar hash mismatch"):
        pit_contract.load_frozen_trading_calendar(tampered)


def test_self_declared_historical_or_boolean_forward_route_cannot_unlock_methodology() -> None:
    forged = {
        "audit_passed": True,
        "methodology_remediation_complete": True,
        "legacy_oos_label_corrected": True,
        "historical_review_set_label": "historical_review_used_in_iteration",
        "valuation_required_fields": list(pit_contract.PIT_CORE_FIELDS),
        "policy_status": "research_only",
        "production_ready": False,
        "auto_execution_allowed": False,
        "promotion_gate_passed": True,
        "historical_valuation_pit_gate_passed": True,
        "historical_classification_gate_passed": True,
        "promotion_eligible_valuation_row_count": 100,
        "valuation_availability_status": "pit_verified_for_promotion",
        "classification_history_status": "verified_for_promotion",
    }
    assert not methodology_route_ready(forged, True)
    assert not methodology_route_ready(forged, {"forward_evidence_integrity_passed": True})


def test_calendar_day_lag_is_rejected_after_close() -> None:
    row = pit_row(
        published_at="2026-07-17T16:00:00+08:00",
        available_date="2026-07-18",  # naive trade_date + 1 calendar day
        fetched_at="2026-07-17T16:05:00+08:00",
    )
    audit = audit_pit_valuation_history(pd.DataFrame([row]))
    assert not audit.eligible
    assert any("frozen-calendar rule" in error for error in audit.errors)


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("published_at", "2026-07-17 14:00:00", "timezone-aware"),
        ("fetched_at", "2026-07-17 14:30:00", "timezone-aware"),
        ("source_hash", "bad", "lowercase SHA-256"),
        ("source_hash", "d" * 64, "immutable archived source artifact"),
        ("source_version", "vendor-v1", "sha256:<source_hash>"),
        ("source_hash_basis", "self_declared", "immutable archived source artifact"),
        ("revision_status", "unknown", "revision_status"),
    ],
)
def test_contract_rejects_unproved_provenance(field: str, value: str, fragment: str) -> None:
    row = pit_row()
    row[field] = value
    audit = audit_pit_valuation_history(pd.DataFrame([row]))
    assert not audit.eligible
    assert any(fragment in error for error in audit.errors)


def test_contract_rejects_fetch_before_publish_and_publish_before_trade() -> None:
    early_fetch = pit_row(fetched_at="2026-07-17T13:59:59+08:00")
    audit = audit_pit_valuation_history(pd.DataFrame([early_fetch]))
    assert any("fetched_at precedes" in error for error in audit.errors)

    early_publish = pit_row(
        trade_date="2026-07-17",
        published_at="2026-07-16T14:00:00+08:00",
        available_date="2026-07-16",
        fetched_at="2026-07-16T14:30:00+08:00",
    )
    audit = audit_pit_valuation_history(pd.DataFrame([early_publish]))
    assert any("published_at precedes trade_date" in error for error in audit.errors)


@pytest.mark.parametrize(
    "missing_field",
    ["source", "availability_basis", "source_hash_basis", "source_artifact_path"],
)
def test_contract_requires_source_provenance_fields(missing_field: str) -> None:
    row = pit_row()
    row.pop(missing_field)
    audit = audit_pit_valuation_history(pd.DataFrame([row]))
    assert not audit.eligible
    assert any("missing columns" in error for error in audit.errors)


def test_contract_rejects_non_trading_trade_date() -> None:
    row = pit_row(
        trade_date="2026-07-18",
        published_at="2026-07-18T10:00:00+08:00",
        available_date="2026-07-20",
        fetched_at="2026-07-18T10:05:00+08:00",
    )
    audit = audit_pit_valuation_history(pd.DataFrame([row]))
    assert not audit.eligible
    assert any("trade_date is not in the frozen trading calendar" in error for error in audit.errors)


@pytest.mark.parametrize("industry_code", ["", None, "80101", "not-a-code", "000000"])
def test_contract_rejects_blank_or_invalid_industry_code(industry_code: object) -> None:
    row = pit_row()
    row["industry_code"] = industry_code

    audit = audit_pit_valuation_history(pd.DataFrame([row]))

    assert not audit.eligible
    assert any("industry_code must be a six-digit SW industry code" in error for error in audit.errors)


def test_revision_chain_is_unique_and_asof_join_uses_then_available_vintage() -> None:
    original_hash = "e0c6aef89358bd38bca9caf7a8280d690dcb6d7819a937e8c575e342b0626d71"
    restated_hash = "0c3c03a688dc879d75a9c5405aef9ed3a30dec97035da864f1cd8bd62bcc2672"
    original = pit_row(
        published_at="2026-07-16T16:00:00+08:00",
        trade_date="2026-07-16",
        available_date="2026-07-17",
        fetched_at="2026-07-16T16:05:00+08:00",
        source_hash=original_hash,
        source_artifact_path="tests/fixtures/valuation_pit_original.txt",
        revision_status="superseded",
        pe=10.0,
    )
    restated = pit_row(
        published_at="2026-07-20T10:00:00+08:00",
        trade_date="2026-07-16",
        available_date="2026-07-20",
        fetched_at="2026-07-20T10:05:00+08:00",
        source_hash=restated_hash,
        source_artifact_path="tests/fixtures/valuation_pit_restated.txt",
        revision_status="restated",
        pe=12.0,
    )
    history = pd.DataFrame([original, restated])
    assert audit_pit_valuation_history(history).eligible

    decisions = pd.DataFrame(
        {
            "signal_date": ["2026-07-17", "2026-07-20"],
            "industry_code": ["801010", "801010"],
        }
    )
    joined = attach_pit_valuation_asof(decisions, history, decision_date_column="signal_date").sort_values("signal_date")
    assert joined["pe"].tolist() == [10.0, 12.0]
    assert joined["valuation_available_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-07-17", "2026-07-20"]

    bad = history.copy()
    bad.loc[0, "revision_status"] = "original"
    assert not audit_pit_valuation_history(bad).eligible


def test_recovered_current_components_are_excluded_from_official_cutoff() -> None:
    frame = pd.DataFrame(
        [
            {"trade_date": "2025-12-31", "source": "sws_index_analysis_daily"},
            {"trade_date": "2026-06-12", "source": "recovered_from_v2_5_quality_components"},
        ]
    )
    assert official_valuation_cutoff(frame) == "2025-12-31"


def test_repository_history_is_non_pit_and_official_cutoff_is_2025_12_31() -> None:
    history_path = (
        Path(__file__).resolve().parents[1]
        / "data_catalog/cache/industry_index/valuation_history/second/sws_second_industry_daily_valuation_2015_present.csv"
    )
    if not history_path.is_file():
        pytest.skip(
            "ignored local valuation history is absent from this clean Git restore; "
            "repository-evidence integration check remains unavailable rather than passing"
        )
    frame = pd.read_csv(history_path, encoding="utf-8-sig", dtype={"industry_code": str}, low_memory=False)
    assert official_valuation_cutoff(frame) == "2025-12-31"
    audit = audit_pit_valuation_history(frame)
    assert not audit.eligible
    assert any("missing columns" in error or "recovered V2.5" in error for error in audit.errors)
    with pytest.raises(ValuationPITContractError):
        v26.load_valuation_history(history_path, release_lag_days=1)


def test_sws_normalizer_declares_publication_time_missing() -> None:
    frame = v25.normalize_sws_results(
        [
            {
                "bargaindate": "2025-12-31",
                "swindexcode": "801010",
                "swindexname": "测试行业",
                "pe": "10",
                "pb": "1",
                "dp": "2",
            }
        ]
    )
    assert frame.loc[0, "data_status"] == NON_PIT_HISTORY_STATUS
    assert frame.loc[0, "available_date"] == ""
    assert frame.loc[0, "published_at"] == ""
    assert not bool(frame.loc[0, "pit_eligible"])


def test_v472_masks_unverified_historical_valuation(tmp_path: Path) -> None:
    path = tmp_path / "history.csv"
    pd.DataFrame(
        [
            {
                "trade_date": "2025-12-31",
                "industry_code": "801010",
                "industry_name": "测试行业",
                "close_index": 100.0,
                "amount_share_pct": 1.0,
                "turnover_rate": 2.0,
                "pe": 10.0,
                "pb": 1.0,
                "dividend_yield": 0.02,
                "source": "sws_index_analysis_daily",
            }
        ]
    ).to_csv(path, index=False, encoding="utf-8-sig")
    loaded = v472.load_history(path)
    assert loaded["valuation_data_status"].eq("blocked_non_pit_valuation_history").all()
    assert loaded[["pe", "pb", "dividend_yield"]].isna().all().all()


def test_v472_excludes_recovered_snapshot_rows_from_history(tmp_path: Path) -> None:
    path = tmp_path / "history.csv"
    official = {
        "trade_date": "2025-12-31",
        "industry_code": "801010",
        "industry_name": "官方历史行业",
        "close_index": 100.0,
        "amount_share_pct": 1.0,
        "turnover_rate": 2.0,
        "pe": 10.0,
        "pb": 1.0,
        "dividend_yield": 0.02,
        "source": "sws_index_analysis_daily",
    }
    recovered = [
        {
            **official,
            "trade_date": "2026-06-12",
            "industry_code": str(801000 + index),
            "industry_name": f"回收快照{index}",
            "source": "recovered_from_v2_5_quality_components",
        }
        for index in range(1, 132)
    ]
    pd.DataFrame([official, *recovered]).to_csv(path, index=False, encoding="utf-8-sig")

    loaded = v472.load_history(path)

    assert len(loaded) == 1
    assert loaded["source"].eq("sws_index_analysis_daily").all()
    assert loaded["trade_date"].max() == pd.Timestamp("2025-12-31")


def test_v472_all_missing_core_valuation_features_cannot_be_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    feature_date = pd.Timestamp("2026-06-12")
    features = pd.DataFrame(
        [
            {
                "trade_date": feature_date,
                "industry_code": str(801000 + index),
                "industry_name": f"回收快照{index}",
                "source": "recovered_from_v2_5_quality_components",
                "pe": float("nan"),
                "pb": float("nan"),
                "dividend_yield": float("nan"),
                "valuation_score": float("nan"),
                "oversold_score": float("nan"),
                "turn_score": float("nan"),
                "liquidity_score": float("nan"),
                "value_oversold_turn_score": 0.5,
                "return_20d": float("nan"),
                "return_60d": float("nan"),
                "return_120d": float("nan"),
                "drawdown_252d": float("nan"),
            }
            for index in range(1, 132)
        ]
    )
    results = pd.DataFrame([{"strategy": "value_oversold_turn", "top_n": 10}])
    monkeypatch.setattr(
        v472,
        "read_json",
        lambda _path: {
            "latest_signal_triggered": True,
            "latest_panel_date": "2026-07-18",
            "planned_entry_date": "2026-07-20",
        },
    )
    monkeypatch.setattr(v472, "current_snapshot_features", lambda _summary: pd.DataFrame())

    candidates = v472.latest_candidates(features, results)

    assert candidates.empty
    assert list(candidates.columns) == v472.LATEST_CANDIDATE_COLUMNS


def test_current_snapshot_is_observation_only_and_cannot_be_backdated(tmp_path: Path) -> None:
    observed = datetime(2026, 7, 18, 10, 0, tzinfo=SHANGHAI)
    assert current_snapshot_as_of_error("2026-07-18", observed) is None
    assert "cannot be archived" in str(current_snapshot_as_of_error("2026-07-17", observed))

    raw = pd.DataFrame([{"行业代码": "801010", "行业名称": "测试行业", "市净率": 1.0}])
    stamped = stamp_current_snapshot(raw, requested_as_of_date="2026-07-18", observed_at=observed)
    assert stamped.loc[0, "data_status"] == CURRENT_SNAPSHOT_STATUS
    assert stamped.loc[0, "published_at"] == ""
    assert stamped.loc[0, "fetched_at"] == "2026-07-18T10:00:00+08:00"
    assert stamped.loc[0, "source_version"] == f"sha256:{stamped.loc[0, 'source_hash']}"
    assert not bool(stamped.loc[0, "pit_eligible"])

    first = archive_current_snapshot_immutable(
        raw,
        requested_as_of_date="2026-07-18",
        observed_at=observed,
        snapshot_dir=tmp_path,
    )
    changed = raw.assign(市净率=9.0)
    second = archive_current_snapshot_immutable(
        changed,
        requested_as_of_date="2026-07-18",
        observed_at=datetime(2026, 7, 18, 11, 0, tzinfo=SHANGHAI),
        snapshot_dir=tmp_path,
    )
    assert first["archive_status"] == "immutable_first_observation_written"
    assert second["archive_status"] == "immutable_first_observation_reused"
    archived = pd.read_csv(first["path"], encoding="utf-8-sig")
    assert archived.loc[0, "市净率"] == 1.0


def test_legacy_snapshot_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "2026-07-18.csv"
    pd.DataFrame([{"行业代码": "801010"}]).to_csv(path, index=False, encoding="utf-8-sig")
    with pytest.raises(ValuationPITContractError, match="legacy snapshot"):
        archive_current_snapshot_immutable(
            pd.DataFrame([{"行业代码": "801010"}]),
            requested_as_of_date="2026-07-18",
            observed_at=datetime(2026, 7, 18, 10, 0, tzinfo=SHANGHAI),
            snapshot_dir=tmp_path,
        )


def test_frozen_calendar_is_repository_evidence() -> None:
    assert DEFAULT_TRADING_CALENDAR.exists()
    assert DEFAULT_TRADING_CALENDAR.read_text(encoding="utf-8-sig").splitlines()[0] == "trade_date"
