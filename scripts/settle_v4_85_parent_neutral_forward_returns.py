#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import settle_v4_72_rebound_leader_forward_returns as base_settle


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "logs" / "v4_85_parent_neutral_forward_ledger.csv"
HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
OUT = ROOT / "outputs" / "audit" / "v4_85_parent_neutral_forward_settlement"
DEBUG = OUT / "debug"
ENTRY_CONFIRMED_DECISION = "entered_research_observation"


def main() -> None:
    parser = argparse.ArgumentParser(description="Settle due V4.85 parent-neutral forward observations.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if error := base_settle.as_of_date_error(args.as_of_date, date.today()):
        parser.error(error)
    as_of = date.fromisoformat(args.as_of_date)
    rows, settled = settle_rows(read_rows(LEDGER), as_of, HISTORY)
    write_rows(LEDGER, rows)
    write_outputs(as_of, rows, settled)
    print(f"ledger={LEDGER}")
    print(f"settled_rows={len(settled)}")
    print("production_ready=False")


def settle_rows(rows: list[dict[str, str]], as_of: date, history_dir: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    guarded = enforce_entry_gate(rows, as_of)
    updated, settled = base_settle.settle_rows(guarded, as_of, history_dir)
    thresholds: dict[tuple[str, str], float | None] = {}
    for row in updated:
        if row.get("outcome_status") != "settled_forward_observation":
            continue
        if row.get("top_quintile_hit") in {"0", "1"}:
            continue
        key = (row.get("planned_entry_date", ""), row.get("planned_exit_date", ""))
        if key not in thresholds:
            thresholds[key] = top_quintile_threshold(history_dir, key[0], key[1], as_of)
        threshold = thresholds[key]
        if threshold is None:
            row["top_quintile_hit"] = ""
            row["settlement_notes"] = (row.get("settlement_notes", "") + " Top20% 命中缺少全行业收益阈值。").strip()
            continue
        try:
            row["top_quintile_hit"] = "1" if float(row.get("realized_return", "")) >= threshold else "0"
        except ValueError:
            row["top_quintile_hit"] = ""
    settled_keys = {(row.get("tracker_id", ""), row.get("industry_code", "")) for row in settled}
    settled = [row for row in updated if (row.get("tracker_id", ""), row.get("industry_code", "")) in settled_keys]
    return updated, settled


def enforce_entry_gate(rows: list[dict[str, str]], as_of: date) -> list[dict[str, str]]:
    out = []
    for row in rows:
        item = dict(row)
        if item.get("outcome_status") != "pending_forward_observation":
            out.append(item)
            continue
        planned_exit = item.get("planned_exit_date", "")
        if not planned_exit or date.fromisoformat(planned_exit) > as_of:
            out.append(item)
            continue
        if item.get("decision") == ENTRY_CONFIRMED_DECISION:
            out.append(item)
            continue
        item["decision"] = "skipped_entry_not_confirmed"
        item["outcome_status"] = "skipped_forward_observation"
        item["settlement_status"] = "entry_not_confirmed"
        item["settlement_notes"] = "V4.90 入场门控未确认 entered；该批次不计入强反弹行业前推评价。"
        out.append(item)
    return out


def top_quintile_threshold(history_dir: Path, entry_target: str, exit_target: str, as_of: date) -> float | None:
    returns = []
    for path in history_dir.glob("*.csv"):
        result = base_settle.compute_return(path, entry_target, exit_target, as_of)
        if result:
            returns.append(float(result["return"]))
    if not returns:
        return None
    return float(pd.Series(returns).quantile(0.8))


def write_outputs(as_of: date, rows: list[dict[str, str]], settled: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    pending = [row for row in rows if row.get("outcome_status") == "pending_forward_observation"]
    skipped = [row for row in rows if row.get("outcome_status") == "skipped_forward_observation"]
    settled_trackers = {row.get("tracker_id", "") for row in rows if row.get("outcome_status") == "settled_forward_observation"}
    summary = {
        "version": "v4_85_parent_neutral_forward_settlement_1.2",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "ledger_rows": len(rows),
        "settled_rows": len(settled),
        "pending_rows": len(pending),
        "skipped_rows": len(skipped),
        "entry_not_confirmed_rows": sum(row.get("settlement_status") == "entry_not_confirmed" for row in rows),
        "settled_tracker_count": len(settled_trackers),
        "required_settled_tracker_count": 30,
        "forward_sample_gate_status": "pass" if len(settled_trackers) >= 30 else "pending",
        "missing_price_rows": sum(row.get("settlement_status") == "missing_price" for row in rows),
        "top_quintile_hit_settled_rows": sum(row.get("top_quintile_hit") in {"0", "1"} for row in rows),
        "top_quintile_hit_missing_rows": sum(row.get("outcome_status") == "settled_forward_observation" and row.get("top_quintile_hit") not in {"0", "1"} for row in rows),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "只结算已经到期的 V4.85 父行业 cap1 前推观察；未到退出日不填未来收益。",
    }
    fields = list(rows[0]) if rows else []
    write_rows(OUT / "top_candidates.csv", settled or pending[:10] or skipped[:10], fields)
    write_rows(DEBUG / "settled_forward_rows.csv", settled, fields)
    write_rows(DEBUG / "settlement_audit.csv", [stringify(summary)], list(summary.keys()))
    write_rows(DEBUG / "pending_forward_rows.csv", pending, fields)
    write_rows(DEBUG / "skipped_forward_rows.csv", skipped, fields)
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary), encoding="utf-8")


def render_report(summary: dict[str, object]) -> str:
    return "\n".join([
        "# V4.85 父行业 cap1 前推收益结算",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 截止日期：{summary['as_of_date']}",
        f"- 账本行数：{summary['ledger_rows']}",
        f"- 本次结算行数：{summary['settled_rows']}",
        f"- 待结算行数：{summary['pending_rows']}",
        f"- 已结算批次：{summary['settled_tracker_count']}",
        f"- 需要结算批次：{summary['required_settled_tracker_count']}",
        f"- Top20% 命中已结算行：{summary['top_quintile_hit_settled_rows']}",
        f"- Top20% 命中缺失行：{summary['top_quintile_hit_missing_rows']}",
        f"- 前推样本门槛：`{summary['forward_sample_gate_status']}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "边界：该结算只使用 as-of-date 已经发生的价格，不会提前写入未来收益。",
    ])


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str] | None = None) -> None:
    if not rows and not fields:
        return
    fields = fields or list(rows[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def stringify(row: dict[str, object]) -> dict[str, str]:
    return {key: str(value) for key, value in row.items()}


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        history = Path(tmp) / "history"
        history.mkdir()
        date_col, close_col = [item for item in base_settle.read_price_series.__code__.co_consts if isinstance(item, str)][-2:]
        for code, exit_price in [("801001", "110"), ("801002", "100"), ("801003", "200")]:
            with (history / f"{code}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[date_col, close_col])
                writer.writeheader()
                writer.writerow({date_col: "2026-01-02", close_col: "100"})
                writer.writerow({date_col: "2026-01-03", close_col: exit_price})
        rows = [{
            "tracker_id": "t1",
            "decision": ENTRY_CONFIRMED_DECISION,
            "outcome_status": "pending_forward_observation",
            "industry_code": "801001",
            "planned_entry_date": "2026-01-02",
            "planned_exit_date": "2026-01-03",
            "realized_relative_return": "",
            "top_quintile_hit": "",
        }]
        updated, settled = settle_rows(rows, date(2026, 1, 3), history)
        assert len(settled) == 1
        assert updated[0]["settlement_status"] == "settled"
        assert updated[0]["top_quintile_hit"] == "0"
        blocked, blocked_settled = settle_rows([{
            "tracker_id": "t2",
            "decision": "planned_observation",
            "outcome_status": "pending_forward_observation",
            "industry_code": "801001",
            "planned_entry_date": "2026-01-02",
            "planned_exit_date": "2026-01-03",
            "realized_relative_return": "",
            "top_quintile_hit": "",
        }], date(2026, 1, 3), history)
        assert not blocked_settled
        assert blocked[0]["outcome_status"] == "skipped_forward_observation"
        assert blocked[0]["settlement_status"] == "entry_not_confirmed"
        assert enforce_entry_gate([{
            "decision": "planned_observation",
            "outcome_status": "pending_forward_observation",
            "planned_exit_date": "2026-01-03",
        }], date(2026, 1, 2))[0]["outcome_status"] == "pending_forward_observation"
        threshold = top_quintile_threshold(history, "2026-01-02", "2026-01-03", date(2026, 1, 3))
        assert threshold is not None and threshold > 0.1
    print("self_check=pass")


if __name__ == "__main__":
    main()
