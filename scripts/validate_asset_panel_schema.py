#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "data_catalog" / "input_asset_panel_schema.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "audit" / "schema_validation"


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a Fundamental Value asset panel against the V0.2 schema.")
    parser.add_argument("--input", required=True, help="CSV asset panel to validate.")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA), help="Schema CSV path.")
    parser.add_argument("--mode", choices=["latest", "pit"], default="latest", help="Validation strictness.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output directory.")
    args = parser.parse_args()

    schema_rows = _read_csv(Path(args.schema))
    panel_rows = _read_csv(Path(args.input))
    issues = validate_panel(panel_rows, schema_rows, args.mode)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "version": "0.2.0",
        "input": str(Path(args.input).resolve()),
        "schema": str(Path(args.schema).resolve()),
        "mode": args.mode,
        "row_count": len(panel_rows),
        "issue_count": len(issues),
        "status": "pass" if not issues else "fail",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(output_dir / "schema_validation_report.json", report)
    _write_csv(output_dir / "schema_validation_issues.csv", issues, ["row", "asset", "field", "severity", "message"])

    print(f"rows={report['row_count']}")
    print(f"issues={report['issue_count']}")
    print(f"status={report['status']}")
    print(f"output={output_dir.resolve()}")
    if issues:
        raise SystemExit(1)


def validate_panel(panel_rows: list[dict[str, str]], schema_rows: list[dict[str, str]], mode: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    schema_by_field = {row["field"]: row for row in schema_rows}
    required_column = "required_for_pit_backtest" if mode == "pit" else "required_for_latest"
    required_fields = [row["field"] for row in schema_rows if row.get(required_column, "").lower() == "yes"]
    fieldnames = set(panel_rows[0].keys()) if panel_rows else set()

    for field in required_fields:
        if field not in fieldnames:
            issues.append(_issue(0, "", field, "error", f"missing required column for {mode} mode"))

    seen_keys: set[tuple[str, str, str]] = set()
    for row_index, row in enumerate(panel_rows, start=1):
        asset = row.get("asset", "")
        key = (asset, row.get("trade_date", ""), row.get("report_period", ""))
        if key in seen_keys:
            issues.append(_issue(row_index, asset, "asset", "error", "duplicate asset + trade_date + report_period"))
        seen_keys.add(key)

        for field in required_fields:
            if field in row and _is_missing(row.get(field)):
                issues.append(_issue(row_index, asset, field, "error", f"blank required field for {mode} mode"))

        for field, value in row.items():
            if field not in schema_by_field or _is_missing(value):
                continue
            issues.extend(_validate_field(row_index, asset, field, value, row, schema_by_field[field]))

        data_status = row.get("data_status", "")
        if mode == "pit" and data_status != "pit_verified":
            issues.append(_issue(row_index, asset, "data_status", "error", "pit mode requires data_status=pit_verified"))
        if data_status == "pit_verified" and _is_missing(row.get("available_date")):
            issues.append(_issue(row_index, asset, "available_date", "error", "pit_verified row requires available_date"))

    return issues


def _validate_field(
    row_index: int,
    asset: str,
    field: str,
    value: str,
    row: dict[str, str],
    schema: dict[str, str],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    field_type = schema.get("type", "")
    allowed = [item for item in schema.get("allowed_values", "").split("|") if item]
    rule = schema.get("validation_rule", "")

    if field_type == "date" and not _is_date(value):
        issues.append(_issue(row_index, asset, field, "error", "invalid YYYY-MM-DD date"))
    elif field_type == "float" and _to_float(value) is None:
        issues.append(_issue(row_index, asset, field, "error", "invalid numeric value"))
    elif field_type == "bool" and value.strip().lower() not in {"0", "1", "true", "false", "yes", "no", "y", "n"}:
        issues.append(_issue(row_index, asset, field, "error", "invalid boolean value"))
    elif field_type == "enum" and allowed and value not in allowed:
        issues.append(_issue(row_index, asset, field, "error", f"value not in allowed set {allowed}"))

    if field == "asset" and value and not value[0].isdigit():
        issues.append(_issue(row_index, asset, field, "warning", "asset does not start with a digit"))

    if rule == "available_date_lte_trade_date":
        trade_date = row.get("trade_date", "")
        if _is_date(value) and _is_date(trade_date) and value > trade_date:
            issues.append(_issue(row_index, asset, field, "error", "date is later than trade_date"))

    if rule in {"non_negative", "positive_or_blank_for_rank"}:
        number = _to_float(value)
        if number is not None:
            if rule == "non_negative" and number < 0:
                issues.append(_issue(row_index, asset, field, "error", "value must be non-negative"))
            if rule == "positive_or_blank_for_rank" and number <= 0:
                issues.append(_issue(row_index, asset, field, "warning", "non-positive value will be low-confidence for ranking"))

    if rule == "zero_to_one_if_present":
        number = _to_float(value)
        if number is not None and not (0 <= number <= 1):
            issues.append(_issue(row_index, asset, field, "warning", "value should usually be between 0 and 1"))

    if rule == "forbidden_as_feature":
        issues.append(_issue(row_index, asset, field, "warning", "forward label must not be used as a feature"))

    return issues


def _issue(row: int, asset: str, field: str, severity: str, message: str) -> dict[str, Any]:
    return {"row": row, "asset": asset, "field": field, "severity": severity, "message": message}


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


def _to_float(value: str) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _is_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return True


def _is_missing(value: str | None) -> bool:
    return value is None or str(value).strip() == "" or str(value).strip().lower() in {"nan", "null"}


if __name__ == "__main__":
    main()
