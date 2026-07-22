from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_structure_features_v4_84 as v484
import run_industry_rebound_window_v4_30_valuation_regime_boundary as v430


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ARTIFACT = ROOT / "tests" / "fixtures" / "valuation_pit_source.txt"


def verified_late_publication_row() -> dict[str, object]:
    source_hash = hashlib.sha256(SOURCE_ARTIFACT.read_bytes()).hexdigest()
    return {
        "trade_date": "2026-07-16",
        "industry_code": "801010",
        "pe": 10.0,
        "pb": 1.0,
        "dividend_yield": 0.02,
        "published_at": "2026-07-17T16:00:00+08:00",
        "available_date": "2026-07-20",
        "fetched_at": "2026-07-17T16:05:00+08:00",
        "source": "official_fixture",
        "source_hash": source_hash,
        "source_hash_basis": "immutable_raw_artifact_sha256",
        "source_artifact_path": "tests/fixtures/valuation_pit_source.txt",
        "source_version": f"sha256:{source_hash}",
        "revision_status": "original",
        "availability_basis": "source_publication_timestamp",
        "data_status": "pit_verified",
    }


def test_v430_market_state_uses_available_date_not_trade_date() -> None:
    daily = v430.build_daily_market_state(pd.DataFrame([verified_late_publication_row()]))
    assert daily["valuation_available_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-07-20"]

    signals = pd.DataFrame(
        {"signal_date_dt": pd.to_datetime(["2026-07-17", "2026-07-20"])}
    )
    attached = v430.attach_valuation_state(signals, daily)

    assert pd.isna(attached.loc[0, "market_valuation_cheap_score"])
    assert attached.loc[1, "valuation_available_date"].strftime("%Y-%m-%d") == "2026-07-20"
    assert attached.loc[1, "market_valuation_cheap_score"] == 1.0


def test_v484_structure_feature_requires_pit_eligibility_and_available_date() -> None:
    opportunity = pd.DataFrame(
        {
            "signal_date": ["2026-07-17", "2026-07-20"],
            "entry_date": ["2026-07-20", "2026-07-21"],
            "exit_date": ["2026-07-24", "2026-07-27"],
            "industry_code": ["801010", "801010"],
        }
    )
    history = pd.DataFrame(
        [
            {
                "industry_code": "801010",
                "valuation_trade_date": "2026-07-16",
                "feature_available_date": "2026-07-17",
                "pb_inverse": 999.0,
                "valuation_pit_eligible": False,
                "valuation_pit_status": "blocked_invalid_availability_metadata",
            },
            {
                "industry_code": "801010",
                "valuation_trade_date": "2026-07-16",
                "feature_available_date": "2026-07-20",
                "pb_inverse": 0.5,
                "valuation_pit_eligible": True,
                "valuation_pit_status": "pit_verified",
            },
        ]
    )

    attached = v484.attach_structure_features(opportunity, history)

    assert pd.isna(attached.loc[0, "pb_inverse"])
    assert attached.loc[1, "pb_inverse"] == 0.5
    assert attached.loc[1, "feature_available_date"].strftime("%Y-%m-%d") == "2026-07-20"
