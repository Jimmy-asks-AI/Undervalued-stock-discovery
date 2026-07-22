from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_task_briefs
import build_retrospective_task_briefs as builder


INVENTORY_PATH = ROOT / "logs" / "research_version_inventory.json"
RETROSPECTIVE_DIR = ROOT / "strategy_lab" / "agents" / "task_briefs" / "retrospective"
CURRENT_PATH = ROOT / "strategy_lab" / "agents" / "task_briefs" / "current_mainline.json"
SCHEMA_PATH = ROOT / "configs" / "fundamental_value_task_brief_schema.json"


def local_brief_evidence_available() -> bool:
    """Return true only when every ignored output referenced by the briefs exists."""
    paths = sorted(RETROSPECTIVE_DIR.glob("*.json")) + [CURRENT_PATH]
    for path in paths:
        brief = json.loads(path.read_text(encoding="utf-8"))
        if any(not (ROOT / item).exists() for item in brief["required_output_paths"]):
            return False
    return True


REQUIRES_LOCAL_BRIEF_EVIDENCE = pytest.mark.skipif(
    not local_brief_evidence_available(),
    reason=(
        "ignored research outputs are intentionally absent from a clean Git restore; "
        "the explicit brief audit remains fail-closed until evidence is restored"
    ),
)


def read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def inventory_rows() -> dict[str, dict[str, object]]:
    payload = read(INVENTORY_PATH)
    return {row["version"]: row for row in payload["versions"]}


def brief_paths() -> list[Path]:
    return sorted(RETROSPECTIVE_DIR.glob("*.json")) + [CURRENT_PATH]


def test_exactly_64_retrospective_briefs_and_one_current_brief_exist() -> None:
    historical = sorted(RETROSPECTIVE_DIR.glob("*.json"))
    expected_names = [
        f"{builder.version_slug(version)}_research_inventory.json"
        for version in builder.expected_historical_versions()
    ]

    assert len(historical) == 64
    assert [path.name for path in historical] == sorted(expected_names)
    assert CURRENT_PATH.is_file()
    assert len(brief_paths()) == 65


def test_historical_briefs_make_no_contemporaneous_timestamp_claim() -> None:
    expected_versions = set(builder.expected_historical_versions())
    observed_versions: set[str] = set()

    for path in sorted(RETROSPECTIVE_DIR.glob("*.json")):
        brief = read(path)
        observed_versions.add(brief["version"])
        assert brief["record_type"] == "retrospective_inventory"
        assert brief["recorded_at"] == "2026-07-18"
        assert brief["historical_timestamp_claimed"] is False
        assert brief["inventory_recorded_retrospectively"] is True
        assert brief["historical_owner_claimed"] is False
        assert brief["task_status_scope"] == "retrospective_inventory_record_only"
        assert "不是该版本执行当时的任务书或预注册记录" in brief["decision_boundary"]
        assert "禁止自动交易" in brief["research_boundary"]

    assert observed_versions == expected_versions


def test_objective_source_config_and_output_are_copied_from_inventory() -> None:
    rows = inventory_rows()

    for version in builder.expected_historical_versions() + [builder.CURRENT_MAINLINE]:
        path = builder.brief_path(version, RETROSPECTIVE_DIR, CURRENT_PATH)
        brief = read(path)
        row = rows[version]
        manifest = row["output"]["standard_manifest"]
        expected_outputs = [
            f"{row['output']['directory']}/{name}" for name in manifest["required_artifacts"]
        ]
        assert brief["objective"] == row["goal"]
        assert brief["objective_source"] == f"logs/research_version_inventory.json#{version}"
        assert brief["source_paths"] == row["source_paths"]
        assert brief["config_paths"] == row["config_paths"]
        assert brief["config_status"] == row["config"]["status"]
        assert brief["required_output_paths"] == expected_outputs
        assert brief["output_manifest"]["required_paths"] == expected_outputs
        assert brief["output_manifest"]["structure_manifest_sha256"] == manifest["structure_manifest_sha256"]
        assert brief["output_manifest"]["run_summary_sha256"] == manifest["run_summary_sha256"]
        assert brief["registration"]["kind"] == row["registration"]["kind"]
        assert brief["registration"]["paths"] == row["registration"]["paths"]
        assert brief["post_hoc"] is row["post_hoc"]
        assert brief["post_hoc_status"] == row["post_hoc_status"]
        assert brief["version_class"] == row["version_class"]
        assert brief["notes"] == row["notes"]
        assert brief["evidence_paths"] == list(
            dict.fromkeys(
                [
                    *row["source_paths"],
                    *row["config_paths"],
                    *row["registration"]["paths"],
                    *expected_outputs,
                ]
            )
        )


def test_unknown_configs_remain_explicitly_empty() -> None:
    v472 = read(RETROSPECTIVE_DIR / "v4_72_research_inventory.json")
    v523 = read(RETROSPECTIVE_DIR / "v5_23_research_inventory.json")

    assert v472["config_paths"] == []
    assert v472["config_status"] == "not_declared"
    assert v523["config_paths"] == ["configs/industry_fund_flow_ths_sw2_mapping.csv"]
    assert v523["config_status"] == "present"


def test_current_mainline_is_a_current_operating_brief() -> None:
    brief = read(CURRENT_PATH)

    assert brief["version"] == "CURRENT_MAINLINE"
    assert brief["record_type"] == "current_operating_brief"
    assert brief["inventory_recorded_retrospectively"] is False
    assert brief["task_status"] == "ready"
    assert brief["task_status_scope"] == "current_operating_governance"
    assert "NO_ACTION" in brief["decision_boundary"]
    assert "manual_decision_support_ready=false" in brief["research_boundary"]
    assert "auto_execution_allowed=false" in brief["research_boundary"]
    assert brief["post_hoc_status"] == "preregistered_forward_only_inherited"


def test_post_hoc_status_contract_is_explicit() -> None:
    assert read(RETROSPECTIVE_DIR / "v4_72_research_inventory.json")["post_hoc_status"] == "post_hoc_historical_inventory"
    assert read(RETROSPECTIVE_DIR / "v5_04_research_inventory.json")["post_hoc_status"] == "preregistered_forward_only"
    assert read(RETROSPECTIVE_DIR / "v5_05_research_inventory.json")["post_hoc_status"] == "inherits_registered_rule"
    assert read(RETROSPECTIVE_DIR / "v5_10_research_inventory.json")["post_hoc_status"] == "inherits_registered_rule"
    assert read(RETROSPECTIVE_DIR / "v5_11_research_inventory.json")["post_hoc_status"] == "post_hoc_historical_inventory"
    assert read(RETROSPECTIVE_DIR / "v5_21_research_inventory.json")["post_hoc_status"] == "retrospective_governance"
    assert read(RETROSPECTIVE_DIR / "v5_30_research_inventory.json")["post_hoc_status"] == "not_an_experiment"


@REQUIRES_LOCAL_BRIEF_EVIDENCE
def test_all_generated_briefs_satisfy_the_existing_schema_contract() -> None:
    schema = read(SCHEMA_PATH)
    issues = audit_task_briefs.audit_task_briefs(brief_paths(), schema)

    assert [issue for issue in issues if issue["severity"] == "error"] == []
    assert [issue for issue in issues if issue["severity"] == "warning"] == []


def test_building_in_memory_has_no_write_side_effect(tmp_path: Path) -> None:
    inventory = read(INVENTORY_PATH)

    first = builder.build_briefs(inventory, ROOT)
    second = builder.build_briefs(inventory, ROOT)

    assert first == second
    assert list(tmp_path.iterdir()) == []


def test_checked_in_briefs_match_deterministic_builder_bytes() -> None:
    briefs = builder.build_briefs(read(INVENTORY_PATH), ROOT)

    for version, brief in briefs.items():
        path = builder.brief_path(version, RETROSPECTIVE_DIR, CURRENT_PATH)
        assert path.read_bytes() == builder.brief_text(brief).encode("utf-8")
