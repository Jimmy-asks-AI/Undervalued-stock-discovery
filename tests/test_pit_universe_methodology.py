from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts import audit_pit_universe_methodology as audit


SOURCE_HASH = "881fd31376997a0174429888f1b9aeb199742a7213496efaf95260ad910adbbc"
REPOSITORY_VALUATION_HISTORY = (
    Path(__file__).resolve().parents[1]
    / "data_catalog/cache/industry_index/valuation_history/second/sws_second_industry_daily_valuation_2015_present.csv"
)


def valuation_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "trade_date": "2026-07-17",
        "industry_code": "801010",
        "industry_name": "测试行业",
        "published_at": "2026-07-17T16:00:00+08:00",
        "available_date": "2026-07-20",
        "fetched_at": "2026-07-17T16:05:00+08:00",
        "source_version": "sha256:" + SOURCE_HASH,
        "source_hash": SOURCE_HASH,
        "source_hash_basis": "immutable_raw_artifact_sha256",
        "source_artifact_path": "tests/fixtures/valuation_pit_source.txt",
        "revision_status": "original",
        "data_status": "pit_verified",
        "availability_basis": "source_publication_timestamp",
        "source": "official_fixture",
    }
    row.update(overrides)
    return row


def test_six_field_contract_is_exact_and_missing_field_fails_closed() -> None:
    policy = audit.read_json(audit.DEFAULT_POLICY)
    audit.validate_policy(policy)
    assert tuple(policy["required_valuation_fields"]) == audit.CORE_PIT_FIELDS

    bad_policy = dict(policy)
    bad_policy["required_valuation_fields"] = list(audit.CORE_PIT_FIELDS[:-1])
    with pytest.raises(ValueError, match="six-field contract"):
        audit.validate_policy(bad_policy)

    incomplete = pd.DataFrame([valuation_row()]).drop(columns=["published_at"])
    contract = audit.build_field_contract_audit(incomplete, audit.CORE_PIT_FIELDS)
    assert contract.loc[contract["field"].eq("published_at"), "status"].item() == "blocked"
    assert audit.promotion_eligible_rows(
        incomplete,
        (date(2026, 7, 17), date(2026, 7, 20)),
        audit.CORE_PIT_FIELDS,
    ).empty


def test_frozen_calendar_controls_availability_and_rejects_naive_timestamps(tmp_path: Path) -> None:
    calendar_path = tmp_path / "calendar.csv"
    pd.DataFrame({"trade_date": ["2026-07-17", "2026-07-20"]}).to_csv(
        calendar_path, index=False, encoding="utf-8-sig"
    )
    calendar = audit.load_trading_calendar(calendar_path)
    assert audit.first_eligible_trade_date("2026-07-17T16:00:00+08:00", calendar) == date(2026, 7, 20)
    assert len(audit.promotion_eligible_rows(pd.DataFrame([valuation_row()]), calendar, audit.CORE_PIT_FIELDS)) == 1

    wrong_calendar_day = pd.DataFrame([valuation_row(available_date="2026-07-18")])
    assert audit.promotion_eligible_rows(wrong_calendar_day, calendar, audit.CORE_PIT_FIELDS).empty
    naive = pd.DataFrame([valuation_row(published_at="2026-07-17 16:00:00")])
    assert audit.promotion_eligible_rows(naive, calendar, audit.CORE_PIT_FIELDS).empty

    pd.DataFrame({"trade_date": ["2026-07-20", "2026-07-17"]}).to_csv(
        calendar_path, index=False, encoding="utf-8-sig"
    )
    with pytest.raises(ValueError, match="unique, and sorted"):
        audit.load_trading_calendar(calendar_path)


def test_recovered_snapshot_is_excluded_even_when_six_fields_are_spoofed() -> None:
    recovered = pd.DataFrame(
        [valuation_row(source=audit.RECOVERED_SOURCE_ID, industry_code="801011")]
    )
    eligible = audit.promotion_eligible_rows(
        recovered,
        (date(2026, 7, 17), date(2026, 7, 20)),
        audit.CORE_PIT_FIELDS,
    )
    assert eligible.empty
    provenance = audit.build_source_provenance_audit(recovered, promotion_rows=eligible)
    assert provenance.loc[0, "status"] == "quarantined_recovered_snapshot"
    assert not bool(provenance.loc[0, "promotion_eligible"])


def test_promotion_rejects_revision_chain_with_late_superseded_vintage() -> None:
    first = valuation_row(revision_status="original")
    second_hash = "0c3c03a688dc879d75a9c5405aef9ed3a30dec97035da864f1cd8bd62bcc2672"
    second = valuation_row(
        published_at="2026-07-20T10:00:00+08:00",
        available_date="2026-07-20",
        fetched_at="2026-07-20T10:05:00+08:00",
        source_hash=second_hash,
        source_version="sha256:" + second_hash,
        source_artifact_path="tests/fixtures/valuation_pit_restated.txt",
        revision_status="superseded",
    )
    eligible = audit.promotion_eligible_rows(
        pd.DataFrame([first, second]),
        (date(2026, 7, 17), date(2026, 7, 20)),
        audit.CORE_PIT_FIELDS,
    )
    assert eligible.empty


def test_reused_codes_and_long_gaps_are_explicitly_segmented(tmp_path: Path) -> None:
    history = pd.DataFrame(
        {
            "trade_date": ["2017-01-20", "2021-12-13", "2017-01-20", "2021-12-13"],
            "industry_code": ["801951", "801951", "801952", "801952"],
            "industry_name": ["旧计算机", "煤炭开采", "旧传媒", "焦炭Ⅱ"],
        }
    )
    episodes = audit.build_identity_episode_audit(history)
    assert episodes.groupby("industry_code")["identity_episode_id"].nunique().to_dict() == {
        "801951": 2,
        "801952": 2,
    }
    assert episodes["cross_episode_rolling_allowed"].eq(False).all()

    history_dir = tmp_path / "history"
    history_dir.mkdir()
    pd.DataFrame(
        {
            "代码": ["801951", "801951", "801951"],
            "日期": ["2017-01-20", "2021-12-13", "2026-07-17"],
            "收盘": [100, 200, 220],
        }
    ).to_csv(history_dir / "801951.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        {"代码": ["801999"], "日期": ["2024-06-17"], "收盘": [100]}
    ).to_csv(history_dir / "801999.csv", index=False, encoding="utf-8-sig")
    calendar = (date(2017, 1, 20), date(2021, 12, 13), date(2024, 6, 17), date(2026, 7, 17))
    file_audit = audit.audit_industry_history_files(history_dir, calendar)
    reused = file_audit.loc[file_audit["industry_code"].eq("801951")].iloc[0]
    stale = file_audit.loc[file_audit["industry_code"].eq("801999")].iloc[0]
    assert reused["identity_episode_count"] == 2
    assert reused["cross_episode_boundary_count"] == 1
    assert reused["max_internal_gap_calendar_days"] > 365
    assert not bool(reused["cross_episode_return_allowed"])
    assert stale["tail_gap_status"] == "long_tail_gap"


def test_three_universe_labels_and_legacy_oos_downgrade() -> None:
    policy = audit.read_json(audit.DEFAULT_POLICY)
    rows: list[dict[str, object]] = []
    for signal_date in ("2016-06-01", "2022-06-01", "2024-06-03"):
        for index, code in enumerate(("801010", "801020"), start=1):
            rows.append(
                {
                    "signal_date": signal_date,
                    "entry_date": signal_date,
                    "exit_date": signal_date,
                    "industry_code": code,
                    "future_return": 0.01 * index,
                    "low_pb_rank": index / 2,
                    "low_pe_rank": index / 2,
                    "dividend_yield_rank": index / 2,
                    "beta_low_pb_score": index / 2,
                }
            )
    metrics = audit.build_three_universe_metrics(pd.DataFrame(rows), policy)
    assert set(metrics["universe_mode"]) == set(audit.UNIVERSE_MODES)
    assert "beta_low_pb_score" not in set(metrics["feature"])
    assert metrics["promotion_eligible"].eq(False).all()
    assert metrics["record_semantics"].eq("methodology_robustness_review_not_investment_candidate").all()

    opportunity = pd.DataFrame(
        {
            "signal_date": ["2021-12-31", "2022-01-03", "2024-06-03"],
            "evidence_set_label": ["development", "independent_oos", "oos"],
        }
    )
    labels = audit.build_evidence_set_labels(opportunity, policy)
    review = labels.loc[labels["signal_date"].ge("2022-01-01")]
    assert review["evidence_set_label"].eq("historical_review_used_in_iteration").all()
    assert review["legacy_oos_downgraded"].eq(True).all()
    assert labels["promotion_eligible"].eq(False).all()


@pytest.fixture(scope="module")
def repository_artifacts() -> audit.AuditArtifacts:
    if not REPOSITORY_VALUATION_HISTORY.is_file():
        pytest.skip(
            "ignored local valuation history is absent from this clean Git restore; "
            "repository-evidence integration checks remain unavailable rather than passing"
        )
    policy = audit.read_json(audit.DEFAULT_POLICY)
    return audit.build_audit(policy, policy_path=audit.DEFAULT_POLICY, generated_at="2026-07-18T12:00:00")


def test_repository_audit_is_governance_complete_but_promotion_fails_closed(
    repository_artifacts: audit.AuditArtifacts,
) -> None:
    summary = repository_artifacts.summary
    assert summary["audit_passed"] is True
    assert summary["promotion_gate_passed"] is False
    assert summary["promotion_eligible_valuation_row_count"] == 0
    assert summary["production_ready"] is False
    assert summary["auto_execution_allowed"] is False
    assert summary["current_action_required"] == "NO_ACTION"


def test_output_structure_is_exact_and_content_tampering_is_detected(
    tmp_path: Path,
    repository_artifacts: audit.AuditArtifacts,
) -> None:
    output = tmp_path / "audit"
    audit.write_outputs(output, repository_artifacts)
    audit.validate_existing_outputs(output, repository_artifacts)

    candidates = pd.read_csv(output / "top_candidates.csv", encoding="utf-8-sig")
    candidates.loc[0, "event_count"] = int(candidates.loc[0, "event_count"]) + 1
    candidates.to_csv(output / "top_candidates.csv", index=False, encoding="utf-8-sig")
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        audit.validate_existing_outputs(output, repository_artifacts)

    audit.write_outputs(output, repository_artifacts)
    (output / "debug" / "unexpected.csv").write_text("x\n1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid debug structure"):
        audit.validate_existing_outputs(output, repository_artifacts)
