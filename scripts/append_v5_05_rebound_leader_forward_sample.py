#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "outputs" / "audit" / "rebound_leader_evidence_freeze_v5_04" / "top_candidates.csv"
LEDGER = ROOT / "logs" / "v5_05_rebound_leader_forward_ledger.csv"

FIELDS = [
    "recorded_at", "frozen_rule", "signal_date", "entry_date", "exit_date",
    "selected_industries", "benchmark_return", "selected_net_return",
    "relative_return", "top_quintile_hit_rate", "settlement_status",
    "created_by_version", "sample_source", "rule_mutation_allowed", "notes",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Append one V5.05 frozen-rule forward sample.")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--frozen-rule")
    parser.add_argument("--signal-date")
    parser.add_argument("--entry-date")
    parser.add_argument("--exit-date")
    parser.add_argument("--selected-industries", default="")
    parser.add_argument("--benchmark-return", default="")
    parser.add_argument("--selected-net-return", default="")
    parser.add_argument("--relative-return", default="")
    parser.add_argument("--top-quintile-hit-rate", default="")
    parser.add_argument("--settlement-status", choices=["pending", "settled"], default="pending")
    parser.add_argument("--notes", default="")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    row = build_row(args, allowed_rules())
    append_row(LEDGER, row, replace=args.replace)
    print(f"ledger={LEDGER}")
    print(f"frozen_rule={row['frozen_rule']}")
    print(f"settlement_status={row['settlement_status']}")


def allowed_rules() -> set[str]:
    return set(pd.read_csv(FROZEN, encoding="utf-8-sig")["frozen_rule"].astype(str))


def build_row(args: argparse.Namespace, allowed: set[str]) -> dict[str, str]:
    required = ["frozen_rule", "signal_date", "entry_date", "exit_date"]
    missing = [name for name in required if not getattr(args, name)]
    if missing:
        raise SystemExit(f"missing required args: {','.join('--' + item.replace('_', '-') for item in missing)}")
    if args.frozen_rule not in allowed:
        raise SystemExit(f"unknown frozen rule: {args.frozen_rule}")
    signal_date, entry_date, exit_date = (
        datetime.fromisoformat(value) for value in [args.signal_date, args.entry_date, args.exit_date]
    )
    if not signal_date < entry_date < exit_date:
        raise SystemExit("date order must satisfy signal_date < entry_date < exit_date")
    if args.settlement_status == "settled":
        for value in [args.benchmark_return, args.selected_net_return, args.relative_return, args.top_quintile_hit_rate]:
            float(value)
    return {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "frozen_rule": args.frozen_rule,
        "signal_date": args.signal_date,
        "entry_date": args.entry_date,
        "exit_date": args.exit_date,
        "selected_industries": args.selected_industries,
        "benchmark_return": args.benchmark_return,
        "selected_net_return": args.selected_net_return,
        "relative_return": args.relative_return,
        "top_quintile_hit_rate": args.top_quintile_hit_rate,
        "settlement_status": args.settlement_status,
        "created_by_version": "5.05.0",
        "sample_source": "future_only",
        "rule_mutation_allowed": "False",
        "notes": args.notes,
    }


def append_row(path: Path, row: dict[str, str], replace: bool = False) -> None:
    old = read_rows(path)
    key = sample_key(row)
    if any(sample_key(item) == key for item in old) and not replace:
        raise SystemExit("duplicate forward sample; rerun with --replace to update")
    kept = [item for item in old if sample_key(item) != key]
    write_rows(path, kept + [row])


def sample_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (row.get("frozen_rule", ""), row.get("signal_date", ""), row.get("entry_date", ""), row.get("exit_date", ""))


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.csv"
        args = argparse.Namespace(
            frozen_rule="quality_score_ge2", signal_date="2026-06-18", entry_date="2026-06-23",
            exit_date="2026-07-21", selected_industries="", benchmark_return="", selected_net_return="",
            relative_return="", top_quintile_hit_rate="", settlement_status="pending", notes="",
        )
        row = build_row(args, {"quality_score_ge2"})
        append_row(path, row)
        try:
            append_row(path, row)
        except SystemExit:
            pass
        else:
            raise AssertionError("duplicate should fail")
        append_row(path, {**row, "notes": "replace"}, replace=True)
        assert read_rows(path)[0]["notes"] == "replace"
    print("self_check=pass")


if __name__ == "__main__":
    main()
