from __future__ import annotations

import csv
import hashlib
import math
from datetime import date
from pathlib import Path

from scripts import audit_industry_history_methodology as audit


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_snapshot(path: Path, codes: list[str]) -> None:
    write_csv(
        path,
        [
            {"行业代码": code, "行业名称": f"行业{code}", "上级行业": "测试一级"}
            for code in codes
        ],
        ["行业代码", "行业名称", "上级行业"],
    )


def build_history(
    path: Path,
    code: str,
    dates: list[str],
    *,
    with_available_date: bool = False,
    pit_artifact: tuple[str, str] | None = None,
) -> None:
    rows = []
    for index, trade_date in enumerate(dates, start=1):
        row: dict[str, object] = {"代码": code, "日期": trade_date, "收盘": 100 + index}
        if pit_artifact:
            artifact_path, artifact_hash = pit_artifact
            row.update(
                {
                    "published_at": f"{trade_date}T14:00:00+08:00",
                    "available_date": trade_date,
                    "fetched_at": f"{trade_date}T14:05:00+08:00",
                    "source": "fixture_official_source",
                    "source_version": f"sha256:{artifact_hash}",
                    "source_hash": artifact_hash,
                    "source_hash_basis": audit.SOURCE_HASH_BASIS,
                    "source_artifact_path": artifact_path,
                    "revision_status": "original",
                    "availability_basis": "source_publication_timestamp",
                    "data_status": audit.VERIFIED_DATA_STATUS,
                }
            )
        elif with_available_date:
            row["available_date"] = trade_date
        rows.append(row)
    fields = ["代码", "日期", "收盘"]
    if pit_artifact:
        fields.extend(audit.HISTORY_PIT_REQUIRED_COLUMNS)
    elif with_available_date:
        fields.append("available_date")
    write_csv(path, rows, fields)


def build_source_artifact(root: Path, name: str) -> tuple[str, str]:
    path = root / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"immutable fixture: {name}\n".encode("utf-8"))
    return path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()


def build_classification_history(
    path: Path,
    codes: list[str],
    *,
    pit_artifact: tuple[str, str] | None = None,
) -> None:
    artifact_path, artifact_hash = pit_artifact or ("", "")
    write_csv(
        path,
        [
            {
                "industry_code": code,
                "industry_name": f"行业{code}",
                "industry_level": "second",
                "parent_industry": "测试一级",
                "effective_from": "2020-01-02",
                "effective_to": "",
                "published_at": "2020-01-02T14:00:00+08:00" if pit_artifact else "",
                "available_date": "2020-01-02",
                "fetched_at": "2020-01-02T14:05:00+08:00" if pit_artifact else "",
                "source": "fixture_official_source" if pit_artifact else "",
                "source_version": f"sha256:{artifact_hash}" if pit_artifact else "fixture-v1",
                "source_hash": artifact_hash,
                "source_hash_basis": audit.SOURCE_HASH_BASIS if pit_artifact else "",
                "source_artifact_path": artifact_path,
                "revision_status": "original",
                "availability_basis": "source_publication_timestamp" if pit_artifact else "",
                "data_status": audit.VERIFIED_DATA_STATUS if pit_artifact else "",
            }
            for code in codes
        ],
        list(audit.CLASSIFICATION_HISTORY_REQUIRED_COLUMNS),
    )


def test_coverage_131_semantics_are_independent_from_freshness_120_semantics(tmp_path: Path) -> None:
    codes = ["801001", "801002", "801003"]
    history_dir = tmp_path / "history"
    snapshot_dir = tmp_path / "snapshots"
    build_snapshot(snapshot_dir / "2026-07-18.csv", codes)
    build_history(history_dir / "801001.csv", "801001", ["2026-07-17"])
    build_history(history_dir / "801002.csv", "801002", ["2026-07-16"])
    build_history(history_dir / "801003.csv", "801003", ["2026-06-01"])

    summary, rows, _ = audit.run_audit(
        history_dir=history_dir,
        snapshot_dir=snapshot_dir,
        classification_history_path=tmp_path / "missing.csv",
        as_of=date(2026, 7, 18),
        required_industry_count=3,
        minimum_fresh_industry_count=2,
        max_stale_days=4,
    )

    assert len(rows) == 3
    assert summary["coverage_gate_passed"] is True
    assert summary["freshness_gate_passed"] is True
    assert summary["fresh_history_file_count"] == 2
    assert summary["policy"]["thresholds_are_independent"] is True
    assert summary["historical_promotion_eligible"] is False
    assert "historical_classification_and_effective_dates_unavailable" in summary["blocked_reasons"]


def test_filename_code_mismatch_and_future_date_fail_current_gate(tmp_path: Path) -> None:
    history_dir = tmp_path / "history"
    snapshot_dir = tmp_path / "snapshots"
    build_snapshot(snapshot_dir / "2026-07-18.csv", ["801001"])
    build_history(history_dir / "801001.csv", "801002", ["2026-07-19"])

    summary, rows, _ = audit.run_audit(
        history_dir=history_dir,
        snapshot_dir=snapshot_dir,
        classification_history_path=tmp_path / "missing.csv",
        as_of=date(2026, 7, 18),
        required_industry_count=1,
        minimum_fresh_industry_count=1,
    )

    assert summary["current_monitoring_gate_passed"] is False
    assert rows[0].code_column_matches_file is False
    assert rows[0].future_trade_date_count == 1
    assert rows[0].current_monitoring_file_valid is False


def test_current_labels_without_historical_provenance_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "801001.csv"
    write_csv(
        path,
        [
            {
                "代码": "801001",
                "日期": "2026-07-17",
                "收盘": 100,
                "available_date": "2026-07-17",
                "行业名称": "今天的行业名",
            }
        ],
        ["代码", "日期", "收盘", "available_date", "行业名称"],
    )

    row = audit.audit_history_file(
        path,
        expected_codes={"801001"},
        as_of=date(2026, 7, 18),
        max_stale_days=4,
    )

    assert row.current_monitoring_file_valid is True
    assert row.historical_promotion_file_eligible is False
    assert row.unverified_label_columns == "行业名称"
    assert "current_or_unversioned_labels_in_history" in row.issues


def test_verified_classification_and_row_availability_can_clear_promotion_gate(tmp_path: Path) -> None:
    codes = ["801001", "801002"]
    history_dir = tmp_path / "history"
    snapshot_dir = tmp_path / "snapshots"
    classification = tmp_path / "classification.csv"
    history_artifact = build_source_artifact(tmp_path, "history.raw")
    classification_artifact = build_source_artifact(tmp_path, "classification.raw")
    build_snapshot(snapshot_dir / "2026-07-18.csv", codes)
    build_history(
        history_dir / "801001.csv",
        "801001",
        ["2020-01-02", "2026-07-17"],
        pit_artifact=history_artifact,
    )
    build_history(
        history_dir / "801002.csv",
        "801002",
        ["2021-01-04", "2026-07-17"],
        pit_artifact=history_artifact,
    )
    build_classification_history(classification, codes, pit_artifact=classification_artifact)

    summary, _, periods = audit.run_audit(
        history_dir=history_dir,
        snapshot_dir=snapshot_dir,
        classification_history_path=classification,
        as_of=date(2026, 7, 18),
        required_industry_count=2,
        minimum_fresh_industry_count=2,
        artifact_root=tmp_path,
    )

    assert summary["classification_history"]["verified"] is True
    assert summary["historical_promotion_eligible"] is True
    assert next(row for row in periods if row["period"] == "2020")["started_by_reference_count"] == 1
    assert next(row for row in periods if row["period"] == "2021")["first_observed_codes"] == "801002"


def test_available_date_alone_and_partial_classification_never_unlock_promotion(tmp_path: Path) -> None:
    codes = ["801001", "801002"]
    history_dir = tmp_path / "history"
    snapshot_dir = tmp_path / "snapshots"
    classification = tmp_path / "classification.csv"
    classification_artifact = build_source_artifact(tmp_path, "classification.raw")
    build_snapshot(snapshot_dir / "2026-07-18.csv", codes)
    for code in codes:
        build_history(history_dir / f"{code}.csv", code, ["2026-07-17"], with_available_date=True)
    build_classification_history(
        classification,
        ["801001"],
        pit_artifact=classification_artifact,
    )

    summary, _, _ = audit.run_audit(
        history_dir=history_dir,
        snapshot_dir=snapshot_dir,
        classification_history_path=classification,
        as_of=date(2026, 7, 18),
        required_industry_count=2,
        minimum_fresh_industry_count=2,
        artifact_root=tmp_path,
    )

    assert summary["historical_promotion_eligible"] is False
    assert summary["history_files_with_complete_pit_chain"] == 0
    assert summary["classification_history"]["governed_code_coverage_verified"] is False
    assert summary["classification_history"]["missing_governed_codes"] == ["801002"]
    assert "historical_classification_governed_code_coverage_unproven" in summary["blocked_reasons"]


def test_overlapping_classification_intervals_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "classification.csv"
    rows = [
        {
            "industry_code": "801001",
            "industry_name": "旧行业",
            "industry_level": "second",
            "parent_industry": "一级",
            "effective_from": "2020-01-01",
            "effective_to": "2021-12-31",
            "available_date": "2020-01-01",
            "source_version": "v1",
            "revision_status": "original",
        },
        {
            "industry_code": "801001",
            "industry_name": "新行业",
            "industry_level": "second",
            "parent_industry": "一级",
            "effective_from": "2021-12-31",
            "effective_to": "",
            "available_date": "2021-12-31",
            "source_version": "v2",
            "revision_status": "revised",
        },
    ]
    write_csv(path, rows, list(audit.CLASSIFICATION_HISTORY_REQUIRED_COLUMNS))

    result = audit.audit_classification_history(path)

    assert result["verified"] is False
    assert "801001:overlapping_effective_intervals" in result["issues"]


def test_reused_code_is_split_into_identity_episodes_and_return_cannot_cross_boundary() -> None:
    dates = [date(2017, 1, 20), date(2021, 12, 13), date(2021, 12, 14)]
    values = [100.0, 200.0, 220.0]

    assert audit.identity_episode_for_date("801951", dates[0]) == "801951:legacy_imp_computer"
    assert audit.identity_episode_for_date("801951", dates[1]) == "801951:sw2021_coal_mining"
    assert audit.episode_safe_return("801951", dates, values, 0, 1) is None
    assert math.isclose(audit.episode_safe_return("801951", dates, values, 1, 2) or 0.0, 0.1)
    assert audit.episode_safe_return(
        "801951",
        [date(2021, 12, 14), date(2021, 12, 13)],
        [220.0, 200.0],
        0,
        1,
    ) is None


def test_reused_code_file_records_episode_boundary_without_breaking_current_freshness(tmp_path: Path) -> None:
    path = tmp_path / "801952.csv"
    build_history(
        path,
        "801952",
        ["2017-01-20", "2021-12-13", "2026-07-17"],
        with_available_date=True,
    )

    row = audit.audit_history_file(
        path,
        expected_codes={"801952"},
        as_of=date(2026, 7, 18),
        max_stale_days=4,
    )

    assert row.current_monitoring_file_valid is True
    assert row.fresh_at_as_of is True
    assert row.identity_reuse_guard_status == "segmented"
    assert row.identity_episode_count == 2
    assert row.cross_episode_boundary_count == 1
