from __future__ import annotations

import csv
import json
import sys
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_research_governance_coverage as coverage
import audit_task_briefs as briefs


def fixture(tmp_path: Path) -> tuple[dict[str, object], list[Path], Path, Path]:
    payload, paths = briefs._build_self_check_fixture(tmp_path)
    return payload, paths, tmp_path / "logs" / "version_changelog.md", tmp_path / "logs" / "active.json"


def run_governance(
    tmp_path: Path,
    payload: dict[str, object],
    paths: list[Path],
    changelog: Path,
    active: Path,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    return briefs.audit_inventory_governance(
        payload,
        paths,
        root=tmp_path,
        changelog_path=changelog,
        active_cohort_path=active,
    )


def test_complete_fixture_covers_exact_expected_set(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)

    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)

    assert len(rows) == 65
    assert len({row["version"] for row in rows}) == 65
    assert all(row["governance_status"] == "pass" for row in rows)
    assert not [issue for issue in issues if issue["severity"] == "error"]


def test_expected_set_rejects_same_length_fake_version_replacement(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    changed = deepcopy(payload)
    changed["versions"][0]["version"] = "V9.99"

    rows, issues = run_governance(tmp_path, changed, paths, changelog, active)

    mismatch = next(issue for issue in issues if issue["field"] == "versions.expected_order")
    assert "missing=V4.72" in mismatch["message"]
    assert "unexpected=V9.99" in mismatch["message"]
    result = coverage.CoverageResult(
        root=tmp_path,
        inventory_path=tmp_path / "logs" / "research_version_inventory.json",
        task_paths=tuple(paths),
        rows=tuple(rows),
        issues=tuple(issues),
    )
    summary = coverage.build_summary(result)
    assert summary["version_order_matches"] is False
    assert summary["missing_expected_versions"] == ["V4.72"]
    assert summary["unexpected_versions"] == ["V9.99"]
    report = coverage.report_text(summary, result)
    assert "missing_expected_versions" not in report
    assert "缺失版本：`V4.72`" in report
    assert "意外版本：`V9.99`" in report


def test_expected_set_rejects_correct_versions_in_wrong_order(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    changed = deepcopy(payload)
    changed["versions"][0], changed["versions"][1] = (
        changed["versions"][1],
        changed["versions"][0],
    )

    _, issues = run_governance(tmp_path, changed, paths, changelog, active)

    mismatch = next(issue for issue in issues if issue["field"] == "versions.expected_order")
    assert "first_difference=0" in mismatch["message"]
    assert "missing=none" in mismatch["message"]
    assert "unexpected=none" in mismatch["message"]


def test_recursive_discovery_includes_nested_briefs(tmp_path: Path) -> None:
    nested = tmp_path / "task_briefs" / "retrospective" / "nested.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}", encoding="utf-8")

    assert briefs.discover_task_briefs(tmp_path / "task_briefs") == [nested]


def test_missing_and_duplicate_expected_versions_fail_closed(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    historical = next(path for path in paths if path.name == "v4_72.json")

    rows, missing_issues = run_governance(
        tmp_path, payload, [path for path in paths if path != historical], changelog, active
    )
    assert rows[0]["governance_status"] == "fail"
    assert any("missing task brief for V4.72" in issue["message"] for issue in missing_issues)

    duplicate = historical.with_name("v4_72_duplicate.json")
    duplicate.write_bytes(historical.read_bytes())
    rows, duplicate_issues = run_governance(tmp_path, payload, paths + [duplicate], changelog, active)
    assert rows[0]["governance_status"] == "fail"
    assert any("duplicate version briefs for V4.72" in issue["message"] for issue in duplicate_issues)


def test_retrospective_brief_cannot_claim_historical_timestamp(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    historical = next(path for path in paths if path.name == "v4_72.json")
    brief = briefs.read_json_object(historical)
    brief["record_type"] = "preregistered_experiment"
    brief["recorded_at"] = "2026-06-01"
    brief["created_at"] = "2026-06-01T09:00:00"
    brief["historical_timestamp_claimed"] = True
    briefs.write_json(historical, brief)

    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)

    assert rows[0]["governance_status"] == "fail"
    assert {issue["field"] for issue in issues} >= {
        "record_type",
        "recorded_at",
        "created_at",
        "historical_timestamp_claimed",
    }


def test_registration_later_than_evidence_start_fails(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    ledger = tmp_path / "logs" / "ledger.jsonl"
    row = json.loads(ledger.read_text(encoding="utf-8"))
    row["registered_at"] = "2026-07-19T00:00:00"
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")

    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)

    current = next(row for row in rows if row["version"] == "CURRENT_MAINLINE")
    assert current["registration_status"] == "fail"
    assert any("registration later than evidence start" in issue["message"] for issue in issues)


def test_unrecoverable_task_brief_keeps_governance_failed_closed(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    changed = deepcopy(payload)
    record = changed["versions"][0]
    record["governance_status"] = "fail"
    record["task_brief_git_recoverable"] = False
    record["missing_requirements"] = ["task_brief_git_recoverability"]

    rows, issues = run_governance(tmp_path, changed, paths, changelog, active)

    assert rows[0]["recoverability_status"] == "fail"
    assert rows[0]["governance_status"] == "fail"
    assert any(issue["field"] == "git_recoverability" for issue in issues)


def test_explicit_governance_fields_are_mandatory(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    historical = next(path for path in paths if path.name == "v4_72.json")
    brief = briefs.read_json_object(historical)
    del brief["objective_source"]
    del brief["evidence_paths"]
    del brief["post_hoc_status"]
    briefs.write_json(historical, brief)

    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)

    assert rows[0]["governance_status"] == "fail"
    fields = {issue["field"] for issue in issues}
    assert {"objective_source", "evidence_paths", "post_hoc_status"} <= fields


def test_explicit_governance_fields_must_match_canonical_semantics(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    historical = next(path for path in paths if path.name == "v4_72.json")
    brief = briefs.read_json_object(historical)
    brief["objective_source"] = "logs/research_version_inventory.json#V4.73"
    brief["evidence_paths"].remove(brief["source_paths"][0])
    brief["post_hoc_status"] = "preregistered_forward_only"
    briefs.write_json(historical, brief)

    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)

    assert rows[0]["governance_status"] == "fail"
    fields = {issue["field"] for issue in issues}
    assert {"objective_source", "evidence_paths", "post_hoc_status"} <= fields


def test_non_experiment_status_requires_matching_class_and_notes(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    changed = deepcopy(payload)
    record = next(row for row in changed["versions"] if row["version"] == "V5.21")
    record["post_hoc_status"] = "retrospective_governance"
    record["version_class"] = "data_governance_version"
    record["notes"] = ["retrospective data-governance repair; not a strategy experiment"]
    brief_path = next(path for path in paths if path.name == "v5_21.json")
    brief = briefs.read_json_object(brief_path)
    brief["post_hoc_status"] = "retrospective_governance"
    briefs.write_json(brief_path, brief)

    rows, issues = run_governance(tmp_path, changed, paths, changelog, active)

    row = next(row for row in rows if row["version"] == "V5.21")
    assert row["governance_status"] == "fail"
    fields = {issue["field"] for issue in issues}
    assert {"version_class", "notes"} <= fields

    brief["version_class"] = record["version_class"]
    brief["notes"] = record["notes"]
    briefs.write_json(brief_path, brief)
    record["task_brief"]["sha256"] = briefs.inventory_builder.sha256_file(brief_path)
    rows, issues = run_governance(tmp_path, changed, paths, changelog, active)
    row = next(row for row in rows if row["version"] == "V5.21")
    assert row["governance_status"] == "pass"
    assert not [issue for issue in issues if issue["task_id"] == "V5.21"]


def test_run_summary_content_change_invalidates_live_sha(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    output = tmp_path / payload["versions"][0]["output"]["directory"]
    # Same byte length isolates the content SHA from the path/size structure SHA.
    (output / "run_summary.json").write_text("[]\n", encoding="utf-8")

    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)

    assert rows[0]["standard_output_status"] == "fail"
    fields = {issue["field"] for issue in issues}
    assert "standard_manifest.run_summary_sha256" in fields
    assert "standard_manifest.structure_manifest_sha256" not in fields


def test_output_file_set_change_invalidates_structure_sha(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    output = tmp_path / payload["versions"][0]["output"]["directory"]
    (output / "debug" / "unexpected.txt").write_text("new evidence\n", encoding="utf-8")

    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)

    assert rows[0]["standard_output_status"] == "fail"
    fields = {issue["field"] for issue in issues}
    assert "standard_manifest.structure_manifest_sha256" in fields
    assert "standard_manifest.file_count" in fields


def test_missing_changelog_manifest_and_cohort_mismatch_each_fail(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    changed = deepcopy(payload)
    changed["versions"][0]["cohort"] = {
        "applicable": True,
        "status": "stale_or_mismatched_active_pair",
        "declared_cohort_id": "old",
        "declared_manifest_hash": "b" * 64,
        "active_cohort_id": "c1",
        "active_manifest_hash": "a" * 64,
    }
    changelog.write_text(changelog.read_text(encoding="utf-8").replace("- V4.72\n", ""), encoding="utf-8")
    output_dir = tmp_path / changed["versions"][0]["output"]["directory"]
    (output_dir / "report.md").unlink()

    rows, issues = run_governance(tmp_path, changed, paths, changelog, active)

    first = rows[0]
    assert first["changelog_status"] == "fail"
    assert first["standard_output_status"] == "fail"
    assert first["cohort_status"] == "fail"
    messages = "\n".join(issue["message"] for issue in issues)
    assert "exact retrospective inventory section is missing version token V4.72" in messages
    assert "standard output manifest incomplete" in messages
    assert "cohort mismatch" in messages


def test_old_history_token_cannot_replace_exact_anchor_token(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    exact_section = changelog.read_text(encoding="utf-8")
    changelog.write_text(
        "# Old historical V4.72 entry\n\n" + exact_section.replace("- V4.72\n", ""),
        encoding="utf-8",
    )

    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)

    assert rows[0]["changelog_status"] == "fail"
    assert any(
        issue["field"] == "changelog_anchor.version_token"
        and "missing version token V4.72" in issue["message"]
        for issue in issues
    )


def test_changelog_section_tamper_invalidates_section_hash(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    text = changelog.read_text(encoding="utf-8")
    changelog.write_text(text.replace("- V4.73\n", "- V4.73 changed\n"), encoding="utf-8")

    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)

    assert rows[0]["changelog_status"] == "fail"
    assert any(issue["field"] == "changelog_anchor.section_sha256" for issue in issues)


def test_standard_four_piece_labels_top_candidates_as_governance_only(tmp_path: Path) -> None:
    payload, paths, changelog, active = fixture(tmp_path)
    rows, issues = run_governance(tmp_path, payload, paths, changelog, active)
    result = coverage.CoverageResult(
        root=tmp_path,
        inventory_path=tmp_path / "logs" / "research_version_inventory.json",
        task_paths=tuple(paths),
        rows=tuple(rows),
        issues=tuple(issues),
    )
    output = tmp_path / "outputs" / "audit" / "research_governance_coverage"

    summary = coverage.write_outputs(result, output)

    assert summary["audit_passed"] is True
    assert summary["version_order_matches"] is True
    assert summary["expected_version_order"] == briefs.expected_governance_versions()
    assert summary["actual_version_order"] == briefs.expected_governance_versions()
    assert (output / "report.md").is_file()
    assert (output / "run_summary.json").is_file()
    assert (output / "top_candidates.csv").is_file()
    assert (output / "debug").is_dir()
    with (output / "top_candidates.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        written = list(csv.DictReader(handle))
    assert len(written) == 65
    assert {row["artifact_role"] for row in written} == {"governance_detail_not_investment_candidate"}
    report = (output / "report.md").read_text(encoding="utf-8")
    assert "不是证券、ETF 或任何投资候选清单" in report
    assert "expected / actual 版本序列" in report
    assert "显式 `objective_source`" in report
    assert "research_only" in report
    assert "NO_ACTION" in report


def test_built_in_self_checks_cover_adversarial_case() -> None:
    briefs.self_check()
    coverage.self_check()
