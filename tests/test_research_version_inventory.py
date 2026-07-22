from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_research_version_inventory as inventory


SCOPE_PATH = ROOT / "configs" / "research_governance_scope.json"
JSON_PATH = ROOT / "logs" / "research_version_inventory.json"
CSV_PATH = ROOT / "logs" / "research_version_inventory.csv"
AUDIT_PATH = ROOT / "outputs" / "audit" / "research_version_inventory"


def local_inventory_outputs_available() -> bool:
    """Return true only when the ignored integration evidence is present locally."""
    if not (AUDIT_PATH / "run_summary.json").is_file():
        return False
    payload = inventory.read_json(JSON_PATH)
    return all(
        (ROOT / row["output"]["run_summary_path"]).is_file()
        for row in payload["versions"]
    )


REQUIRES_LOCAL_INVENTORY_OUTPUTS = pytest.mark.skipif(
    not local_inventory_outputs_available(),
    reason=(
        "ignored version run outputs are intentionally absent from a clean Git restore; "
        "run the explicit governance audit after restoring or regenerating evidence"
    ),
)


def load_scope() -> dict[str, object]:
    return inventory.read_json(SCOPE_PATH)


def load_artifact() -> dict[str, object]:
    return inventory.read_json(JSON_PATH)


def by_version(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    return {row["version"]: row for row in payload["versions"]}


def test_scope_expands_to_exact_two_digit_version_range() -> None:
    versions = inventory.expected_versions(load_scope())

    assert len(versions) == 64
    assert versions[:3] == ["V4.72", "V4.73", "V4.74"]
    assert versions[27:31] == ["V4.99", "V5.00", "V5.01", "V5.02"]
    assert versions[-1] == "V5.35"
    assert len(set(versions)) == len(versions)


def test_inventory_has_64_historical_rows_and_one_current_mainline() -> None:
    payload = load_artifact()
    records = payload["versions"]
    expected = inventory.expected_versions(load_scope()) + ["CURRENT_MAINLINE"]

    assert [row["version"] for row in records] == expected
    assert payload["summary"]["record_count"] == 65
    assert payload["summary"]["historical_version_count"] == 64
    assert payload["summary"]["current_mainline_count"] == 1
    assert sum(bool(row["in_current_mainline"]) for row in records) == 1
    assert [row["sequence_ordinal"] for row in records] == list(range(1, 66))
    assert [row["version_id"] for row in records] == expected
    assert all(row["schema_version"] == payload["schema_version"] for row in records)
    assert all(row["implementation_version"] == row["run_summary_version"] for row in records)


def test_producers_are_matched_by_output_binding_and_run_summary_version() -> None:
    rows = by_version(load_artifact())

    assert all(row["producer"]["status"] == "matched" for row in rows.values())
    assert all(
        row["producer"]["match_method"] == "static_OUT_OUTPUT_path_plus_run_summary_version"
        for version, row in rows.items()
        if version != "CURRENT_MAINLINE"
    )
    assert rows["V4.72"]["producer"]["source"] == "scripts/run_industry_rebound_leader_selection_v4_72.py"
    assert rows["V4.87"]["producer"]["source"] == "scripts/build_v4_85_parent_neutral_evidence_scorecard.py"
    assert rows["V4.88"]["producer"]["source"] == "scripts/build_v4_85_parent_neutral_pre_entry_audit.py"
    assert rows["V5.27"]["producer"]["source"] == "scripts/settle_v5_27_fund_flow_forward_samples.py"
    assert rows["V5.30"]["producer"]["source"] == "scripts/audit_v5_30_fund_flow_forward_ledger_integrity.py"


def test_post_hoc_and_preregistration_are_machine_distinguishable() -> None:
    rows = by_version(load_artifact())

    assert rows["V4.72"]["post_hoc"] is True
    assert rows["V4.72"]["registration"]["kind"] == "explicit_post_hoc"
    assert rows["V5.03"]["post_hoc"] is True
    assert rows["V5.04"]["post_hoc"] is False
    assert rows["V5.04"]["registration"]["kind"] == "preregistered_forward_only"
    assert rows["V5.04"]["registration"]["status"] == "valid"
    assert rows["V5.05"]["post_hoc"] is False
    assert rows["V5.05"]["registration"]["kind"] == "inherits_registered_rule"
    assert rows["V5.10"]["registration"]["kind"] == "inherits_registered_rule"
    assert rows["V5.11"]["post_hoc"] is True
    assert rows["V5.20"]["post_hoc"] is True
    assert rows["V5.21"]["post_hoc"] is True
    assert rows["V5.35"]["registration"]["kind"] == "explicit_post_hoc"
    assert rows["CURRENT_MAINLINE"]["registration"]["kind"] == "preregistered_forward_only_inherited"
    assert rows["V4.72"]["post_hoc_status"] == "post_hoc_historical_inventory"
    assert rows["V5.04"]["post_hoc_status"] == "preregistered_forward_only"
    assert rows["V5.05"]["post_hoc_status"] == "inherits_registered_rule"
    assert rows["V5.21"]["post_hoc_status"] == "retrospective_governance"
    assert rows["V5.30"]["post_hoc_status"] == "not_an_experiment"
    assert rows["CURRENT_MAINLINE"]["post_hoc_status"] == "preregistered_forward_only_inherited"
    assert rows["V5.04"]["registration"]["timing_status"] == "valid"
    assert all(check["status"] == "pass" for check in rows["V5.04"]["registration"]["timing_checks"])


def test_formal_fields_do_not_invent_configuration_or_recoverability() -> None:
    rows = by_version(load_artifact())

    assert rows["V4.72"]["configuration_mode"] == "embedded_or_inherited"
    assert rows["V4.72"]["config_paths"] == []
    assert rows["V5.23"]["configuration_mode"] == "explicit_file_refs"
    assert rows["V5.23"]["config_paths"] == ["configs/industry_fund_flow_ths_sw2_mapping.csv"]
    assert rows["V4.72"]["source_paths"] == ["scripts/run_industry_rebound_leader_selection_v4_72.py"]
    assert len(rows["V4.72"]["source_path_sha256"][rows["V4.72"]["source_paths"][0]]) == 64
    assert rows["V4.72"]["artifact_git_state"] == "ignored"
    assert rows["V4.72"]["artifact_git_recoverable"] is False
    assert rows["V4.72"]["source_git_recoverability"] == "recoverable"
    assert rows["V4.72"]["retrospective_inventory"] is True
    assert rows["CURRENT_MAINLINE"]["retrospective_inventory"] is False
    assert all(row["inventory_recorded_at"] == "2026-07-18" for row in rows.values())


def test_mainline_role_is_machine_distinguishable() -> None:
    rows = by_version(load_artifact())
    roles = {row["mainline_role"] for row in rows.values()}

    assert inventory.SUPPORTED_MAINLINE_ROLES == {
        "direct_runtime_source",
        "transitive_gate_evidence",
        "full_refresh_only",
        "archive_only",
        "current_orchestrator",
    }
    assert roles <= inventory.SUPPORTED_MAINLINE_ROLES
    assert rows["CURRENT_MAINLINE"]["mainline_role"] == "current_orchestrator"
    assert rows["V4.85"]["mainline_role"] == "direct_runtime_source"
    assert rows["V4.72"]["mainline_role"] == "transitive_gate_evidence"
    assert rows["V4.86"]["mainline_role"] == "full_refresh_only"


def test_exact_changelog_anchor_is_parsed_and_hashed() -> None:
    rows = by_version(load_artifact())
    hashes = {row["changelog_anchor"]["section_sha256"] for row in rows.values()}

    assert len(hashes) == 1
    assert len(next(iter(hashes))) == 64
    for version, row in rows.items():
        anchor = row["changelog_anchor"]
        assert anchor["status"] == "present"
        assert anchor["path"] == "logs/version_changelog.md"
        assert anchor["heading"] == inventory.CHANGELOG_HEADING
        assert anchor["record_type"] == "retrospective_inventory"
        assert anchor["recorded_at"] == "2026-07-18"
        assert anchor["historical_timestamp_claimed"] is False
        assert anchor["version_token"] == version


def test_standard_output_and_missing_governance_are_explicit() -> None:
    payload = load_artifact()

    assert payload["summary"]["standard_output_counts"] == {"complete": 65}
    assert all(row["output"]["standard_manifest"]["status"] == "complete" for row in payload["versions"])
    assert all(isinstance(row["missing_requirements"], list) for row in payload["versions"])
    assert all(row["governance_status"] in {"pass", "fail"} for row in payload["versions"])


def test_cohort_comparison_distinguishes_current_stale_and_undeclared() -> None:
    active = {"cohort_id": "c3", "manifest_hash": "h3"}

    current = inventory.assess_cohort(
        {"active_cohort_id": "c3", "active_cohort_manifest_hash": "h3"}, active, True
    )
    stale = inventory.assess_cohort(
        {"cohort_id": "c2", "manifest_hash": "h2"}, active, True
    )
    undeclared = inventory.assess_cohort({}, active, True)

    assert current["status"] == "matches_active_pair"
    assert stale["status"] == "stale_or_mismatched_active_pair"
    assert undeclared["status"] == "not_declared_in_run_summary"


def test_missing_run_summary_fails_closed(tmp_path: Path) -> None:
    output = tmp_path / "outputs" / "only_v4_72"
    output.mkdir(parents=True)
    (output / "run_summary.json").write_text('{"version":"4.72.0"}', encoding="utf-8")

    with pytest.raises(ValueError, match="missing run_summary versions: V4.73"):
        inventory.discover_version_outputs(tmp_path, ["V4.72", "V4.73"])


def test_duplicate_run_summary_version_fails_closed(tmp_path: Path) -> None:
    for name in ["first", "second"]:
        output = tmp_path / "outputs" / name
        output.mkdir(parents=True)
        (output / "run_summary.json").write_text('{"version":"4.72.0"}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate run_summary versions: V4.72"):
        inventory.discover_version_outputs(tmp_path, ["V4.72"])


def test_wrong_standard_output_manifest_fails_closed(tmp_path: Path) -> None:
    output = tmp_path / "outputs" / "fixture"
    (output / "debug").mkdir(parents=True)
    for name in ["report.md", "run_summary.json", "top_candidates.csv", "unexpected.txt"]:
        (output / name).write_text("{}" if name.endswith(".json") else "fixture", encoding="utf-8")

    manifest = inventory.standard_output_manifest(
        tmp_path, "outputs/fixture", ["report.md", "run_summary.json", "top_candidates.csv", "debug"]
    )

    assert manifest["status"] == "invalid"
    assert manifest["unexpected_top_level"] == ["unexpected.txt"]


def test_false_preregistration_timing_fails_closed(tmp_path: Path) -> None:
    ledger = tmp_path / "logs" / "fixture.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "experiment_id": "late_registration",
                "registered_at": "2026-07-13T09:00:00",
                "evidence_start_date": "2026-07-12",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rule = {
        "registration_kind": "preregistered_forward_only",
        "registration_paths": ["logs/fixture.jsonl"],
        "experiment_ids": ["late_registration"],
    }

    evidence = inventory.registration_evidence(tmp_path, rule)

    assert evidence["status"] == "invalid"
    assert evidence["timing_status"] == "invalid"
    assert evidence["timing_checks"][0]["status"] == "fail"


def test_csv_is_one_row_per_version_with_required_columns() -> None:
    rows = list(csv.DictReader(io.StringIO(CSV_PATH.read_text(encoding="utf-8"))))

    assert len(rows) == 65
    assert list(rows[0]) == inventory.CSV_FIELDS
    assert [row["version"] for row in rows][28:31] == ["V5.00", "V5.01", "V5.02"]
    assert rows[-1]["version"] == "CURRENT_MAINLINE"
    assert rows[0]["version_id"] == "V4.72"
    assert rows[0]["sequence_ordinal"] == "1"
    assert rows[28]["version_id"] == "V5.00"
    assert rows[0]["configuration_mode"] == "embedded_or_inherited"
    assert len(rows[0]["changelog_section_sha256"]) == 64


@REQUIRES_LOCAL_INVENTORY_OUTPUTS
def test_standard_four_piece_is_exact_and_not_an_investment_candidate_list() -> None:
    assert {path.name for path in AUDIT_PATH.iterdir()} == {"report.md", "run_summary.json", "top_candidates.csv", "debug"}
    assert {path.name for path in (AUDIT_PATH / "debug").iterdir()} == {
        "governance_gaps.csv",
        "input_manifest.json",
        "structure_manifest.json",
    }
    summary = inventory.read_json(AUDIT_PATH / "run_summary.json")
    top_rows = list(csv.DictReader((AUDIT_PATH / "top_candidates.csv").read_text(encoding="utf-8").splitlines()))
    assert summary["record_count"] == 65
    assert summary["investment_candidate_count"] == 0
    assert summary["top_candidates_semantics"] == "governance_inventory_detail_not_investment_candidate"
    assert len(top_rows) == 65
    assert [row["version_id"] for row in top_rows] == inventory.expected_versions(load_scope()) + ["CURRENT_MAINLINE"]
    assert {row["investment_candidate"] for row in top_rows} == {"false"}
    assert {row["record_semantics"] for row in top_rows} == {"governance_inventory_detail_not_investment_candidate"}
    assert "不是股票、行业、ETF 或任何投资候选清单" in (AUDIT_PATH / "report.md").read_text(encoding="utf-8")


@REQUIRES_LOCAL_INVENTORY_OUTPUTS
def test_debug_input_and_structure_manifests_are_deterministic() -> None:
    input_manifest = inventory.read_json(AUDIT_PATH / "debug" / "input_manifest.json")
    structure = inventory.read_json(AUDIT_PATH / "debug" / "structure_manifest.json")

    assert input_manifest["missing_input_count"] == 0
    assert len(input_manifest["manifest_sha256"]) == 64
    assert structure["top_level_exact"] == ["report.md", "run_summary.json", "top_candidates.csv", "debug"]
    assert structure["debug_exact"] == ["governance_gaps.csv", "input_manifest.json", "structure_manifest.json"]


@REQUIRES_LOCAL_INVENTORY_OUTPUTS
def test_checked_in_artifacts_match_a_fresh_deterministic_build() -> None:
    scope = load_scope()
    first = inventory.build_inventory(ROOT, scope)
    second = inventory.build_inventory(ROOT, scope)

    assert first == second
    assert inventory.json_text(first).encode("utf-8") == JSON_PATH.read_bytes()
    assert inventory.csv_text(first).encode("utf-8") == CSV_PATH.read_bytes()
    assert first["records_sha256"] == second["records_sha256"]
    first_audit = inventory.build_audit_artifacts(ROOT, first)
    second_audit = inventory.build_audit_artifacts(ROOT, second)
    assert first_audit == second_audit
    for relative, content in first_audit.items():
        assert (AUDIT_PATH / relative).read_bytes() == content
