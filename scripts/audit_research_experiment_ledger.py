#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "logs" / "research_experiment_ledger.jsonl"
FROZEN_RULES = ROOT / "outputs" / "audit" / "rebound_leader_evidence_freeze_v5_04" / "debug" / "frozen_rule_spec.csv"
PROMOTION = ROOT / "outputs" / "audit" / "rebound_leader_evidence_freeze_v5_04" / "debug" / "promotion_checklist.csv"
OUTPUT = ROOT / "outputs" / "audit" / "research_experiment_ledger"


def main() -> None:
    parser = argparse.ArgumentParser(description="登记并审计前推研究实验哈希链。")
    parser.add_argument("--register-v5-04", action="store_true")
    parser.add_argument("--evidence-start-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.register_v5_04:
        register_v5_04(date.fromisoformat(args.evidence_start_date))
    rows = read_ledger()
    checks = audit(rows)
    write_outputs(rows, checks)
    print(f"ledger_rows={len(rows)}")
    print(f"integrity_passed={str(all(row['status'] == 'pass' for row in checks)).lower()}")


def register_v5_04(evidence_start: date) -> None:
    rows = read_ledger()
    existing = {row["experiment_id"] for row in rows}
    frozen = read_csv(FROZEN_RULES)
    criteria = read_csv(PROMOTION)
    source_hashes = {str(path.relative_to(ROOT)).replace("\\", "/"): sha256(path) for path in (FROZEN_RULES, PROMOTION)}
    previous = rows[-1]["row_hash"] if rows else "GENESIS"
    registered_at = datetime.now().isoformat(timespec="seconds")
    # ponytail: single-process append; add OS file locking if concurrent researchers register.
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as handle:
        for rule in frozen:
            experiment_id = f"v5_04_{rule['frozen_rule']}_forward_only_{evidence_start.isoformat()}"
            if experiment_id in existing:
                continue
            payload = {
                "experiment_id": experiment_id,
                "registered_at": registered_at,
                "evidence_start_date": evidence_start.isoformat(),
                "registration_status": "preregistered_forward_only",
                "frozen_rule": rule["frozen_rule"],
                "rule_definition": rule["rule_definition"],
                "allowed_next_action": rule["allowed_next_action"],
                "forbidden_next_action": rule["forbidden_next_action"],
                "promotion_criteria": [row for row in criteria if row["frozen_rule"] == rule["frozen_rule"]],
                "source_hashes": source_hashes,
                "previous_hash": previous,
            }
            payload["row_hash"] = row_hash(payload)
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            previous = payload["row_hash"]


def audit(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    checks = []
    previous = "GENESIS"
    ids = set()
    for index, row in enumerate(rows, start=1):
        stored = row.get("row_hash", "")
        payload = {key: value for key, value in row.items() if key != "row_hash"}
        valid_hash = stored == row_hash(payload)
        valid_chain = row.get("previous_hash") == previous
        unique = row.get("experiment_id") not in ids
        registered = date.fromisoformat(str(row["registered_at"])[:10])
        evidence_start = date.fromisoformat(row["evidence_start_date"])
        valid_boundary = evidence_start >= registered
        status = "pass" if valid_hash and valid_chain and unique and valid_boundary else "fail"
        checks.append({"row": str(index), "experiment_id": row.get("experiment_id", ""), "status": status,
                       "hash_valid": str(valid_hash).lower(), "chain_valid": str(valid_chain).lower(),
                       "unique_id": str(unique).lower(), "forward_boundary_valid": str(valid_boundary).lower()})
        ids.add(row.get("experiment_id"))
        previous = stored
    return checks or [{"row": "0", "experiment_id": "", "status": "fail", "hash_valid": "false",
                       "chain_valid": "false", "unique_id": "false", "forward_boundary_valid": "false"}]


def write_outputs(rows: list[dict[str, Any]], checks: list[dict[str, str]]) -> None:
    debug = OUTPUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    passed = bool(rows) and all(row["status"] == "pass" for row in checks)
    summary = {"version": "research-experiment-ledger-1.0", "generated_at": datetime.now().isoformat(timespec="seconds"),
               "experiment_count": len(rows), "integrity_passed": passed,
               "ledger_head_hash": rows[-1].get("row_hash", "") if rows else "",
               "preregistered_forward_only_count": sum(row.get("registration_status") == "preregistered_forward_only" for row in rows),
               "historical_results_preregistered": False, "production_ready": False}
    write_json(OUTPUT / "run_summary.json", summary)
    write_csv(OUTPUT / "top_candidates.csv", checks)
    write_csv(debug / "ledger_audit.csv", checks)
    (debug / "ledger_snapshot.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT / "report.md").write_text(
        "# 研究实验账本审计\n\n"
        f"- 实验登记数：{len(rows)}\n- 哈希链完整：`{str(passed).lower()}`\n"
        "- 登记边界：只允许登记日及之后的新样本进入前推证据；历史结果仍是事后库存。\n",
        encoding="utf-8",
    )


def read_ledger() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def row_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def self_check() -> None:
    first = {"experiment_id": "x", "registered_at": "2026-07-12T00:00:00", "evidence_start_date": "2026-07-12",
             "registration_status": "preregistered_forward_only", "previous_hash": "GENESIS"}
    first["row_hash"] = row_hash(first)
    assert audit([first])[0]["status"] == "pass"
    broken = dict(first); broken["evidence_start_date"] = "2026-07-11"
    assert audit([broken])[0]["status"] == "fail"
    print("self_check=pass")


if __name__ == "__main__":
    main()
