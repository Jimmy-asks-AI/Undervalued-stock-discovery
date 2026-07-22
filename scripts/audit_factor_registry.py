#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "factor_library" / "fundamental_value_factor_registry.csv"
DEFAULT_SCHEMA = ROOT / "data_catalog" / "input_asset_panel_schema.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "audit" / "factor_registry_audit"

REQUIRED_COLUMNS = [
    "factor_id",
    "family",
    "agent",
    "description",
    "formula",
    "expected_direction",
    "required_fields",
    "pit_required",
    "available_date_rule",
    "neutralization",
    "preprocess",
    "failure_modes",
    "status",
]

ALLOWED_DIRECTIONS = {"higher", "lower"}
ALLOWED_PIT = {"yes", "no"}
ALLOWED_STATUS = {
    "candidate",
    "research_validated",
    "paper_trading",
    "production_candidate",
    "observation",
    "rejected",
    "deprecated",
}
KNOWN_AGENTS = {
    "valuation_factor_researcher",
    "industry_relative_value_agent",
    "financial_sector_value_auditor",
    "profitability_growth_analyst",
    "shareholder_return_analyst",
    "accounting_quality_auditor",
    "value_trap_risk_agent",
    "fundamental_data_steward",
    "factor_validation_auditor",
    "industry_fundamental_value_agent",
    "industry_oversold_reversion_agent",
    "industry_cycle_quality_agent",
    "industry_value_trap_agent",
    "industry_factor_validation_auditor",
    "industry_report_synthesizer",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit the Fundamental Value factor registry.")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="Factor registry CSV path.")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA), help="Input asset panel schema CSV path.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output directory.")
    args = parser.parse_args()

    registry_path = Path(args.registry)
    schema_path = Path(args.schema)
    registry_rows = _read_csv(registry_path)
    schema_rows = _read_csv(schema_path)
    schema_fields = {row["field"] for row in schema_rows}
    label_fields = {row["field"] for row in schema_rows if row.get("category") == "label"}

    issues = audit_registry(registry_rows, schema_fields, label_fields)
    errors = [issue for issue in issues if issue["severity"] == "error"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "version": "0.3.0",
        "registry": str(registry_path.resolve()),
        "schema": str(schema_path.resolve()),
        "factor_count": len(registry_rows),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "status": "pass" if not errors else "fail",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(output_dir / "factor_registry_audit_report.json", report)
    _write_csv(output_dir / "factor_registry_audit_issues.csv", issues, ["row", "factor_id", "field", "severity", "message"])

    print(f"factors={report['factor_count']}")
    print(f"errors={report['error_count']}")
    print(f"warnings={report['warning_count']}")
    print(f"status={report['status']}")
    print(f"output={output_dir.resolve()}")
    if errors:
        raise SystemExit(1)


def audit_registry(
    registry_rows: list[dict[str, str]],
    schema_fields: set[str],
    label_fields: set[str],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    if not registry_rows:
        return [_issue(0, "", "factor_id", "error", "registry is empty")]

    fieldnames = set(registry_rows[0].keys())
    for column in REQUIRED_COLUMNS:
        if column not in fieldnames:
            issues.append(_issue(0, "", column, "error", "missing required registry column"))

    seen_factor_ids: set[str] = set()
    for row_index, row in enumerate(registry_rows, start=2):
        factor_id = row.get("factor_id", "").strip()
        if not factor_id:
            issues.append(_issue(row_index, factor_id, "factor_id", "error", "blank factor_id"))
            continue
        if factor_id in seen_factor_ids:
            issues.append(_issue(row_index, factor_id, "factor_id", "error", "duplicate factor_id"))
        seen_factor_ids.add(factor_id)

        for column in REQUIRED_COLUMNS:
            if column in {"neutralization"}:
                continue
            if _is_missing(row.get(column)):
                issues.append(_issue(row_index, factor_id, column, "error", "blank required factor metadata"))

        direction = row.get("expected_direction", "").strip()
        if direction and direction not in ALLOWED_DIRECTIONS:
            issues.append(_issue(row_index, factor_id, "expected_direction", "error", "expected_direction must be higher or lower"))

        pit_required = row.get("pit_required", "").strip()
        if pit_required and pit_required not in ALLOWED_PIT:
            issues.append(_issue(row_index, factor_id, "pit_required", "error", "pit_required must be yes or no"))
        if pit_required == "yes" and _is_missing(row.get("available_date_rule")):
            issues.append(_issue(row_index, factor_id, "available_date_rule", "error", "PIT factor needs an available_date rule"))

        status = row.get("status", "").strip()
        if status and status not in ALLOWED_STATUS:
            issues.append(_issue(row_index, factor_id, "status", "error", "status is not in the allowed status ladder"))
        if status in {"research_validated", "paper_trading", "production_candidate"}:
            issues.append(_issue(row_index, factor_id, "status", "warning", "promoted status requires linked validation evidence"))

        agent = row.get("agent", "").strip()
        if agent and agent not in KNOWN_AGENTS:
            issues.append(_issue(row_index, factor_id, "agent", "warning", "agent is not in the known V0.3 agent set"))

        fields = _split_required_fields(row.get("required_fields", ""))
        if not fields:
            issues.append(_issue(row_index, factor_id, "required_fields", "error", "no required fields declared"))
        for field in fields:
            if field not in schema_fields:
                issues.append(_issue(row_index, factor_id, "required_fields", "error", f"required field is not in input schema: {field}"))
            if field in label_fields:
                issues.append(_issue(row_index, factor_id, "required_fields", "error", f"forward label cannot be a factor input: {field}"))

        if _is_missing(row.get("failure_modes")):
            issues.append(_issue(row_index, factor_id, "failure_modes", "error", "factor needs explicit failure modes"))
        if _is_missing(row.get("preprocess")):
            issues.append(_issue(row_index, factor_id, "preprocess", "warning", "preprocess policy is blank"))
        if _is_missing(row.get("neutralization")):
            issues.append(_issue(row_index, factor_id, "neutralization", "warning", "neutralization policy is blank"))

    return issues


def _split_required_fields(value: str) -> list[str]:
    return [item.strip() for item in str(value).split("|") if item.strip()]


def _issue(row: int, factor_id: str, field: str, severity: str, message: str) -> dict[str, Any]:
    return {"row": row, "factor_id": factor_id, "field": field, "severity": severity, "message": message}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _is_missing(value: str | None) -> bool:
    return value is None or str(value).strip() == "" or str(value).strip().lower() in {"nan", "null"}


if __name__ == "__main__":
    main()
