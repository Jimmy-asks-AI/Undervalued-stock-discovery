#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
import time
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRACKER = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "forward_sample_tracker.json"
PACKET = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "live_decision_packet.json"
MANUAL_REVIEW = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "manual_carrier_review_sheet.csv"
LEDGER = ROOT / "logs" / "v4_71_forward_sample_ledger.csv"
FIELDS = [
    "recorded_at",
    "tracker_id",
    "signal_date",
    "planned_entry_date",
    "planned_exit_date",
    "decision_state",
    "production_ready",
    "decision",
    "carrier_code",
    "carrier_name",
    "actual_entry_date",
    "actual_exit_date",
    "entry_price",
    "exit_price",
    "round_trip_cost_bps",
    "gross_return",
    "net_return",
    "reference_entry_price",
    "max_reference_entry_price",
    "entry_price_drift_pct",
    "price_drift_override",
    "unplanned_override",
    "notes",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Append one V4.71 forward-sample record.")
    parser.add_argument("--carrier-code")
    parser.add_argument("--carrier-name", default="")
    parser.add_argument("--decision", choices=["planned", "entered", "skipped", "observe"], default="entered")
    parser.add_argument("--entry-date", default="")
    parser.add_argument("--exit-date", default="")
    parser.add_argument("--entry-price", type=float)
    parser.add_argument("--exit-price", type=float)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--notes", default="")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--allow-unplanned", action="store_true")
    parser.add_argument("--allow-price-drift", action="store_true")
    parser.add_argument("--as-of-date", default="", help="Audit date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--audit-output", default="", help="Optional CSV path for --audit summary.")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--migrate-schema", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    reject_future_dates(args, date.today())
    if args.audit:
        summary = audit_ledger(LEDGER, parse_iso_date(args.as_of_date, "as_of_date") if args.as_of_date else None)
        if args.audit_output:
            write_audit_output(Path(args.audit_output), summary)
        return
    if args.migrate_schema:
        migrate_schema(LEDGER)
        return
    if not args.carrier_code:
        raise SystemExit("--carrier-code is required unless --self-check, --audit or --migrate-schema is used")

    tracker = read_json(TRACKER)
    packet = read_json(PACKET)
    entry_price_supplied = args.entry_price is not None
    fill_existing_entry(args, tracker, LEDGER)
    row = build_row(args, tracker, packet)
    drift = validate_entry_price_drift(row, MANUAL_REVIEW, allow=args.allow_price_drift, check=entry_price_supplied)
    validate_manual_override_reason(row, drift, args.notes)
    row.update(drift)
    existing_entry = getattr(args, "_existing_entry_row", None)
    exit_update = not entry_price_supplied and args.exit_price is not None
    preserve_existing_entry_audit(row, existing_entry, exit_update=exit_update)
    inherited_unplanned = bool(exit_update and existing_entry and existing_entry.get("unplanned_override") == "True")
    append_row(LEDGER, row, replace=args.replace, allow_unplanned=args.allow_unplanned or inherited_unplanned)
    print(f"ledger={LEDGER}")
    print(f"decision={row['decision']}")
    print(f"net_return={row['net_return']}")


def build_row(args: argparse.Namespace, tracker: dict, packet: dict) -> dict[str, str]:
    if args.decision == "entered":
        if args.entry_price is None:
            raise SystemExit("entered decision requires --entry-price")
        if args.entry_price <= 0 or (args.exit_price is not None and args.exit_price <= 0):
            raise SystemExit("prices must be positive")
        if args.exit_date and args.exit_price is None:
            raise SystemExit("--exit-date requires --exit-price")
        actual_entry_date = args.entry_date or str(tracker["planned_entry_date"])
        actual_exit_date = args.exit_date or (str(tracker["planned_exit_date"]) if args.exit_price is not None else "")
        gross = None if args.exit_price is None else args.exit_price / args.entry_price - 1.0
        net = None if gross is None else gross - args.cost_bps / 10000.0
    else:
        if args.entry_price is not None or args.exit_price is not None or args.entry_date or args.exit_date:
            raise SystemExit("only entered decision accepts entry/exit dates or prices")
        gross = None
        net = None
        actual_entry_date = ""
        actual_exit_date = ""
    return {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "tracker_id": str(tracker["tracker_id"]),
        "signal_date": str(tracker["signal_date"]),
        "planned_entry_date": str(tracker["planned_entry_date"]),
        "planned_exit_date": str(tracker["planned_exit_date"]),
        "decision_state": str(packet["decision_state"]),
        "production_ready": str(tracker["production_ready"]),
        "decision": args.decision,
        "carrier_code": str(args.carrier_code).zfill(6),
        "carrier_name": args.carrier_name,
        "actual_entry_date": actual_entry_date,
        "actual_exit_date": actual_exit_date,
        "entry_price": "" if args.entry_price is None else f"{args.entry_price:.6f}",
        "exit_price": "" if args.exit_price is None else f"{args.exit_price:.6f}",
        "round_trip_cost_bps": f"{args.cost_bps:.2f}",
        "gross_return": "" if gross is None else f"{gross:.8f}",
        "net_return": "" if net is None else f"{net:.8f}",
        "unplanned_override": str(bool(args.decision in {"entered", "skipped"} and args.allow_unplanned)),
        "notes": args.notes,
    }


def fill_existing_entry(args: argparse.Namespace, tracker: dict, path: Path) -> None:
    if args.decision != "entered" or args.entry_price is not None or args.exit_price is None:
        return
    key = (str(tracker["tracker_id"]), str(args.carrier_code).zfill(6), "entered")
    for row in read_rows(path):
        if (row["tracker_id"], row["carrier_code"], row["decision"]) == key and row.get("entry_price"):
            args.entry_price = float(row["entry_price"])
            args.entry_date = args.entry_date or row.get("actual_entry_date", "")
            args.carrier_name = args.carrier_name or row.get("carrier_name", "")
            args._existing_entry_row = row
            return
    raise SystemExit("exit-price-only update requires an existing entered record with entry_price")


def validate_entry_price_drift(row: dict[str, str], manual_review_path: Path, *, allow: bool = False, check: bool = True) -> dict[str, str]:
    info = entry_price_drift_info(row, manual_review_path)
    if row["decision"] != "entered" or not row.get("entry_price") or not info["max_reference_entry_price"]:
        return info
    entry_price = float(row["entry_price"])
    max_price = float(info["max_reference_entry_price"])
    if allow:
        info["price_drift_override"] = str(bool(entry_price > max_price))
        return info
    if not check:
        return info
    if entry_price > max_price:
        raise SystemExit(
            f"entry_price {entry_price:.6f} exceeds max_reference_entry_price {max_price:.6f}; "
            "pass --allow-price-drift only for a deliberate manual override"
        )
    return info


def entry_price_drift_info(row: dict[str, str], manual_review_path: Path) -> dict[str, str]:
    info = {"reference_entry_price": "", "max_reference_entry_price": "", "entry_price_drift_pct": "", "price_drift_override": "False"}
    if row["decision"] != "entered" or not row.get("entry_price"):
        return info
    rows = {
        str(r.get("carrier_code", "")).zfill(6): r
        for r in read_rows(manual_review_path)
    }
    review = rows.get(row["carrier_code"], {})
    reference = review.get("reference_entry_price", "")
    limit = review.get("max_reference_entry_price", "")
    info["reference_entry_price"] = reference
    info["max_reference_entry_price"] = limit
    if reference:
        drift = float(row["entry_price"]) / float(reference) - 1.0
        info["entry_price_drift_pct"] = f"{drift:.8f}"
    return info


def validate_manual_override_reason(row: dict[str, str], drift: dict[str, str], notes: str) -> None:
    if row.get("unplanned_override") == "True" and not notes.strip():
        raise SystemExit("--allow-unplanned requires --notes explaining the manual override")
    if drift.get("price_drift_override") == "True" and not notes.strip():
        raise SystemExit("--allow-price-drift requires --notes explaining the manual override")


def preserve_existing_entry_audit(row: dict[str, str], existing: dict[str, str] | None, *, exit_update: bool) -> None:
    if not exit_update or not existing:
        return
    for field in ["reference_entry_price", "max_reference_entry_price", "entry_price_drift_pct", "price_drift_override", "unplanned_override"]:
        if existing.get(field):
            row[field] = existing[field]
    if not row.get("notes") and existing.get("notes"):
        row["notes"] = existing["notes"]


def append_row(path: Path, row: dict[str, str], replace: bool = False, allow_unplanned: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(path)
    try:
        validate_row_dates(row)
        existing = read_rows(path)
        if row["decision"] in {"entered", "skipped"} and not allow_unplanned:
            planned = any(r["tracker_id"] == row["tracker_id"] and r["carrier_code"] == row["carrier_code"] and r["decision"] == "planned" for r in existing)
            if not planned:
                raise SystemExit(f"{row['decision']} record requires existing planned record; pass --allow-unplanned only for old/manual exceptions")
        if row["decision"] in {"entered", "skipped"}:
            opposite = "skipped" if row["decision"] == "entered" else "entered"
            conflict = any(r["tracker_id"] == row["tracker_id"] and r["carrier_code"] == row["carrier_code"] and r["decision"] == opposite for r in existing)
            if conflict:
                raise SystemExit(f"{row['decision']} record conflicts with existing {opposite} record for this tracker/carrier")
        rows = existing
        key = (row["tracker_id"], row["carrier_code"], row["decision"])
        rows = [r for r in rows if (r["tracker_id"], r["carrier_code"], r["decision"]) != key]
        if len(rows) != len(existing) and not replace:
            raise SystemExit("record exists; pass --replace to overwrite that tracker/carrier/decision")
        rows.append(row)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    finally:
        lock.unlink(missing_ok=True)


def acquire_lock(path: Path) -> Path:
    lock = path.with_name(path.name + ".lock")
    # ponytail: coarse process lock; use SQLite if this ledger gets high-frequency writers.
    for _ in range(50):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock
        except FileExistsError:
            time.sleep(0.1)
    raise SystemExit(f"ledger is locked: {lock}")


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def migrate_schema(path: Path) -> None:
    rows = read_rows(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"migrated_schema={path}")
    print(f"rows={len(rows)}")


def write_audit_output(path: Path, summary: dict[str, int | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary))
        writer.writeheader()
        writer.writerow(summary)


def validate_row_dates(row: dict[str, str]) -> None:
    if row["decision"] != "entered":
        return
    entry = parse_iso_date(row.get("actual_entry_date", ""), "actual_entry_date")
    if not row.get("actual_exit_date"):
        return
    exit_ = parse_iso_date(row.get("actual_exit_date", ""), "actual_exit_date")
    if exit_ < entry:
        raise SystemExit("actual_exit_date must be on or after actual_entry_date")


def parse_iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"{field} must be YYYY-MM-DD") from exc


def reject_future_dates(args: argparse.Namespace, today: date) -> None:
    # ponytail: only CLI-supplied dates; ledger historical rows remain auditable.
    for field in ["as_of_date", "entry_date", "exit_date"]:
        value = getattr(args, field, "")
        if value and parse_iso_date(value, field) > today:
            raise SystemExit(f"--{field.replace('_', '-')} {value} is in the future")


def audit_ledger(path: Path, as_of: date | None = None) -> dict[str, int | str]:
    as_of = as_of or date.today()
    rows = read_rows(path)
    if not rows:
        summary = {"status": "empty", "rows": 0, "planned": 0, "entered": 0, "skipped": 0, "pending_planned": 0, "pending_entry_due": 0, "open_entered": 0, "exit_review_due": 0, "unplanned_entered": 0, "price_drift_overrides": 0, "conflicting_outcomes": 0}
    else:
        planned = {(r["tracker_id"], r["carrier_code"]) for r in rows if r["decision"] == "planned"}
        entered = {(r["tracker_id"], r["carrier_code"]) for r in rows if r["decision"] == "entered"}
        skipped = {(r["tracker_id"], r["carrier_code"]) for r in rows if r["decision"] == "skipped"}
        unplanned = [r for r in rows if r["decision"] == "entered" and ((r["tracker_id"], r["carrier_code"]) not in planned or r.get("unplanned_override") == "True")]
        conflicts = entered & skipped
        pending = planned - entered - skipped
        pending_due = [
            r for r in rows
            if r["decision"] == "planned"
            and (r["tracker_id"], r["carrier_code"]) in pending
            and r.get("planned_entry_date")
            and parse_iso_date(r["planned_entry_date"], "planned_entry_date") <= as_of
        ]
        open_entered = [r for r in rows if r["decision"] == "entered" and not r.get("exit_price")]
        exit_due = [
            r for r in open_entered
            if r.get("planned_exit_date") and parse_iso_date(r["planned_exit_date"], "planned_exit_date") <= as_of
        ]
        price_drift_overrides = [r for r in rows if r.get("price_drift_override") == "True"]
        summary = {
            "status": "pass" if not unplanned and not conflicts and not pending_due and not exit_due and not price_drift_overrides else "review",
            "rows": len(rows),
            "planned": len(planned),
            "entered": len(entered),
            "skipped": len(skipped),
            "pending_planned": len(pending),
            "pending_entry_due": len(pending_due),
            "open_entered": len(open_entered),
            "exit_review_due": len(exit_due),
            "unplanned_entered": len(unplanned),
            "price_drift_overrides": len(price_drift_overrides),
            "conflicting_outcomes": len(conflicts),
        }
    for key, value in summary.items():
        print(f"{key}={value}")
    return summary


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.csv"
        reject_future_dates(argparse.Namespace(as_of_date="2026-06-20", entry_date="", exit_date=""), date(2026, 6, 20))
        try:
            reject_future_dates(argparse.Namespace(as_of_date="2026-06-23", entry_date="", exit_date=""), date(2026, 6, 20))
            raise AssertionError("future as-of date should be rejected")
        except SystemExit:
            pass
        try:
            reject_future_dates(argparse.Namespace(as_of_date="", entry_date="2026-06-23", exit_date=""), date(2026, 6, 20))
            raise AssertionError("future entry date should be rejected")
        except SystemExit:
            pass
        args = argparse.Namespace(decision="planned", carrier_code="510300", carrier_name="沪深300ETF华泰柏瑞", entry_date="", exit_date="", entry_price=None, exit_price=None, cost_bps=10.0, allow_unplanned=False, notes="")
        built = build_row(args, {"tracker_id": "t1", "signal_date": "2026-06-18", "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21", "production_ready": False}, {"decision_state": "watchlist_only_research_signal"})
        assert built["actual_entry_date"] == ""
        assert built["actual_exit_date"] == ""
        args.entry_price = 1.0
        try:
            build_row(args, {"tracker_id": "t1", "signal_date": "2026-06-18", "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21", "production_ready": False}, {"decision_state": "watchlist_only_research_signal"})
        except SystemExit:
            pass
        else:
            raise AssertionError("planned record should reject prices")
        row = {"tracker_id": "t1", "carrier_code": "510300", "decision": "planned", **{k: "" for k in FIELDS}}
        row["tracker_id"] = "t1"
        row["carrier_code"] = "510300"
        row["decision"] = "planned"
        append_row(path, row)
        assert len(read_rows(path)) == 1
        future_path = Path(tmp) / "future.csv"
        append_row(future_path, {**row, "planned_entry_date": "2099-01-01"})
        assert audit_ledger(future_path, date(2098, 12, 31))["pending_entry_due"] == 0
        due_summary = audit_ledger(future_path, date(2099, 1, 1))
        assert due_summary["pending_entry_due"] == 1
        audit_path = Path(tmp) / "audit.csv"
        write_audit_output(audit_path, due_summary)
        assert read_rows(audit_path)[0]["pending_entry_due"] == "1"
        append_row(path, {**row, "notes": "replace"}, replace=True)
        assert read_rows(path)[0]["notes"] == "replace"
        append_row(path, {**row, "decision": "entered", "actual_entry_date": "2026-06-23", "actual_exit_date": "2026-07-21", "entry_price": "1.000000", "exit_price": "1.010000", "net_return": "0.00900000"})
        assert len(read_rows(path)) == 2
        args = argparse.Namespace(decision="entered", carrier_code="510300", carrier_name="沪深300ETF华泰柏瑞", entry_date="", exit_date="", entry_price=1.0, exit_price=None, cost_bps=10.0, allow_unplanned=False, notes="")
        built = build_row(args, {"tracker_id": "t1", "signal_date": "2026-06-18", "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21", "production_ready": False}, {"decision_state": "watchlist_only_research_signal"})
        assert built["actual_exit_date"] == ""
        assert built["net_return"] == ""
        manual = Path(tmp) / "manual.csv"
        with manual.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["carrier_code", "max_reference_entry_price"])
            writer.writeheader()
            writer.writerow({"carrier_code": "510300", "max_reference_entry_price": "1.020"})
        drift = validate_entry_price_drift(built, manual)
        assert drift["reference_entry_price"] == ""
        assert drift["max_reference_entry_price"] == "1.020"
        assert drift["price_drift_override"] == "False"
        too_high = {**built, "entry_price": "1.030000"}
        try:
            validate_entry_price_drift(too_high, manual)
        except SystemExit:
            pass
        else:
            raise AssertionError("entry price above drift limit should fail")
        drift = validate_entry_price_drift(too_high, manual, allow=True)
        assert drift["price_drift_override"] == "True"
        try:
            validate_manual_override_reason(built, drift, "")
        except SystemExit:
            pass
        else:
            raise AssertionError("price drift override without notes should fail")
        validate_manual_override_reason(built, drift, "人工确认流动性和价差后覆盖")
        try:
            build_row(argparse.Namespace(**{**vars(args), "exit_date": "2026-07-21"}), {"tracker_id": "t1", "signal_date": "2026-06-18", "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21", "production_ready": False}, {"decision_state": "watchlist_only_research_signal"})
        except SystemExit:
            pass
        else:
            raise AssertionError("exit date without exit price should fail")
        append_row(path, {**row, "carrier_code": "563360", "decision": "planned"})
        append_row(path, {**row, "carrier_code": "563360", "decision": "entered", "actual_entry_date": "2026-06-23", "entry_price": "1.000000"})
        settle_args = argparse.Namespace(decision="entered", carrier_code="563360", carrier_name="", entry_date="", exit_date="", entry_price=None, exit_price=1.01, cost_bps=10.0, allow_unplanned=False, notes="")
        fill_existing_entry(settle_args, {"tracker_id": "t1"}, path)
        assert settle_args.entry_price == 1.0
        assert settle_args.entry_date == "2026-06-23"
        replacement = build_row(settle_args, {"tracker_id": "t1", "signal_date": "2026-06-18", "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21", "production_ready": False}, {"decision_state": "watchlist_only_research_signal"})
        preserve_existing_entry_audit(replacement, {"price_drift_override": "True", "unplanned_override": "True", "entry_price_drift_pct": "0.03000000", "notes": "原入场覆盖"}, exit_update=True)
        assert replacement["price_drift_override"] == "True"
        assert replacement["unplanned_override"] == "True"
        assert replacement["entry_price_drift_pct"] == "0.03000000"
        assert replacement["notes"] == "原入场覆盖"
        append_row(path, {**row, "carrier_code": "588000", "decision": "planned"})
        append_row(path, {**row, "carrier_code": "588000", "decision": "skipped"})
        assert audit_ledger(path)["pending_planned"] == 0
        try:
            append_row(path, {**row, "decision": "skipped"})
        except SystemExit:
            pass
        else:
            raise AssertionError("skipped after entered should fail")
        try:
            append_row(path, {**row, "carrier_code": "512100", "decision": "skipped"})
        except SystemExit:
            pass
        else:
            raise AssertionError("unplanned skipped record should fail")
        try:
            append_row(path, {**row, "carrier_code": "159915", "decision": "entered", "actual_entry_date": "2026-06-23", "actual_exit_date": "2026-07-21"})
        except SystemExit:
            pass
        else:
            raise AssertionError("unplanned entered record should fail")
        try:
            append_row(path, {**row, "decision": "entered", "actual_entry_date": "2026-07-21", "actual_exit_date": "2026-06-23"}, replace=True)
        except SystemExit:
            pass
        else:
            raise AssertionError("exit before entry should fail")
        unplanned_row = {**row, "carrier_code": "159915", "decision": "entered", "actual_entry_date": "2026-06-23", "actual_exit_date": "", "entry_price": "1.000000", "unplanned_override": "True"}
        try:
            validate_manual_override_reason(unplanned_row, {}, "")
        except SystemExit:
            pass
        else:
            raise AssertionError("unplanned override without notes should fail")
        validate_manual_override_reason(unplanned_row, {}, "补录历史遗漏")
        append_row(path, unplanned_row, allow_unplanned=True)
        assert audit_ledger(path)["unplanned_entered"] == 1
        settle_unplanned = argparse.Namespace(decision="entered", carrier_code="159915", carrier_name="", entry_date="", exit_date="", entry_price=None, exit_price=1.01, cost_bps=10.0, allow_unplanned=False, allow_price_drift=False, notes="")
        fill_existing_entry(settle_unplanned, {"tracker_id": "t1"}, path)
        replacement = build_row(settle_unplanned, {"tracker_id": "t1", "signal_date": "2026-06-18", "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21", "production_ready": False}, {"decision_state": "watchlist_only_research_signal"})
        preserve_existing_entry_audit(replacement, getattr(settle_unplanned, "_existing_entry_row", None), exit_update=True)
        assert replacement["unplanned_override"] == "True"
        append_row(path, replacement, replace=True, allow_unplanned=True)
        assert audit_ledger(path)["unplanned_entered"] == 1
        append_row(path, {**row, "carrier_code": "512100", "decision": "planned", "planned_entry_date": "2000-01-01"})
        assert audit_ledger(path)["pending_entry_due"] == 1
        append_row(path, {**row, "carrier_code": "159902", "decision": "planned"})
        append_row(path, {**row, "carrier_code": "159902", "decision": "entered", "actual_entry_date": "2000-01-01", "planned_exit_date": "2000-01-02", "entry_price": "1.000000"})
        assert audit_ledger(path)["exit_review_due"] == 1
        append_row(path, {**row, "carrier_code": "510050", "decision": "planned"})
        append_row(path, {**row, "carrier_code": "510050", "decision": "entered", "actual_entry_date": "2026-06-23", "entry_price": "1.100000", "price_drift_override": "True"})
        assert audit_ledger(path)["price_drift_overrides"] == 1
        dirty = read_rows(path)
        dirty.append({**row, "decision": "skipped"})
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(dirty)
        assert audit_ledger(path)["conflicting_outcomes"] == 1
        old = Path(tmp) / "old.csv"
        with old.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["tracker_id", "carrier_code", "decision"])
            writer.writeheader()
            writer.writerow({"tracker_id": "t2", "carrier_code": "510300", "decision": "planned"})
        migrate_schema(old)
        migrated = read_rows(old)
        assert len(migrated) == 1
        assert "price_drift_override" in migrated[0]
    print("self_check=pass")


if __name__ == "__main__":
    main()
