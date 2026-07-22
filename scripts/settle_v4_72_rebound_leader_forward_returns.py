#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "logs" / "v4_72_rebound_leader_forward_ledger.csv"
HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
OUT = ROOT / "outputs" / "audit" / "v4_72_forward_return_settlement"
DEBUG = OUT / "debug"

EXTRA_FIELDS = ["realized_return", "benchmark_return", "realized_relative_return", "settlement_status", "settlement_notes"]
SCHEDULE_FIELDS = ["event_date", "event_type", "row_count", "status", "action", "command"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Settle due V4.72 forward observations without looking past as-of date.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if error := as_of_date_error(args.as_of_date, date.today()):
        parser.error(error)
    as_of = date.fromisoformat(args.as_of_date)
    rows, settled = settle_rows(read_rows(LEDGER), as_of, HISTORY)
    write_rows(LEDGER, rows)
    write_outputs(as_of, rows, settled)
    print(f"ledger={LEDGER}")
    print(f"settled_rows={len(settled)}")
    print("production_ready=False")


def settle_rows(rows: list[dict[str, str]], as_of: date, history_dir: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    benchmark_cache: dict[tuple[str, str], float] = {}
    settled = []
    out = []
    for row in rows:
        row = with_extra_fields(row)
        if row.get("outcome_status") != "pending_forward_observation" or row.get("realized_relative_return"):
            out.append(row)
            continue
        if not due(row.get("planned_exit_date", ""), as_of):
            row["settlement_status"] = "not_due"
            out.append(row)
            continue
        result = compute_return(history_dir / f"{row['industry_code']}.csv", row["planned_entry_date"], row["planned_exit_date"], as_of)
        if not result:
            row["settlement_status"] = "missing_price"
            row["settlement_notes"] = "行业历史价格不足，无法结算。"
            out.append(row)
            continue
        key = (row["planned_entry_date"], row["planned_exit_date"])
        if key not in benchmark_cache:
            benchmark_cache[key] = benchmark_return(history_dir, key[0], key[1], as_of)
        relative = result["return"] - benchmark_cache[key]
        row.update({
            "actual_entry_date": result["entry_date"],
            "actual_exit_date": result["exit_date"],
            "realized_return": f"{result['return']:.8f}",
            "benchmark_return": f"{benchmark_cache[key]:.8f}",
            "realized_relative_return": f"{relative:.8f}",
            "outcome_status": "settled_forward_observation",
            "settlement_status": "settled",
            "settlement_notes": "按申万二级行业指数收盘价结算；基准为同区间全行业等权平均收益。",
        })
        settled.append(row)
        out.append(row)
    return out, settled


def due(planned_exit: str, as_of: date) -> bool:
    return bool(planned_exit) and date.fromisoformat(planned_exit) <= as_of


def as_of_date_error(value: str, today: date) -> str:
    # ponytail: future guard only; weekend backfill settlement is allowed.
    if date.fromisoformat(value) > today:
        return f"--as-of-date {value} is in the future; settle forward returns on or after that date."
    return ""


def compute_return(path: Path, entry_target: str, exit_target: str, as_of: date) -> dict[str, str | float] | None:
    prices = read_price_series(path)
    entry = first_on_or_after(prices, date.fromisoformat(entry_target), as_of)
    exit_ = first_on_or_after(prices, date.fromisoformat(exit_target), as_of)
    if not entry or not exit_:
        return None
    return {
        "entry_date": entry[0].isoformat(),
        "exit_date": exit_[0].isoformat(),
        "return": exit_[1] / entry[1] - 1.0,
    }


def benchmark_return(history_dir: Path, entry_target: str, exit_target: str, as_of: date) -> float:
    returns = []
    for path in history_dir.glob("*.csv"):
        result = compute_return(path, entry_target, exit_target, as_of)
        if result:
            returns.append(float(result["return"]))
    return sum(returns) / len(returns) if returns else 0.0


def read_price_series(path: Path) -> list[tuple[date, float]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            try:
                rows.append((date.fromisoformat(row["日期"]), float(row["收盘"])))
            except (KeyError, ValueError):
                continue
        return rows


def first_on_or_after(prices: list[tuple[date, float]], target: date, as_of: date) -> tuple[date, float] | None:
    for item in prices:
        if target <= item[0] <= as_of:
            return item
    return None


def with_extra_fields(row: dict[str, str]) -> dict[str, str]:
    for field in EXTRA_FIELDS:
        row.setdefault(field, "")
    return row


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str] | None = None, append_extra: bool = True) -> None:
    fields = fields or (list(rows[0]) if rows else [])
    if not fields:
        return
    if append_extra:
        for field in EXTRA_FIELDS:
            if field not in fields:
                fields.append(field)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(as_of: date, rows: list[dict[str, str]], settled: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    pending = [row for row in rows if row.get("outcome_status") == "pending_forward_observation"]
    schedule = settlement_schedule(rows, as_of)
    next_action = next_schedule_action(schedule)
    summary = {
        "version": "v4_72_forward_return_settlement_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "ledger_rows": len(rows),
        "settled_rows": len(settled),
        "pending_rows": len(pending),
        "missing_price_rows": sum(row.get("settlement_status") == "missing_price" for row in rows),
        "settlement_schedule_rows": len(schedule),
        "next_action_date": next_action.get("event_date", ""),
        "next_action": next_action.get("action", ""),
        "production_ready": False,
        "final_verdict": "只结算已经到期的前推观察；未到退出日不填未来收益。",
    }
    ledger_fields = list(rows[0]) if rows else []
    write_rows(OUT / "top_candidates.csv", settled or pending[:10], ledger_fields)
    write_rows(DEBUG / "settled_forward_rows.csv", settled, ledger_fields)
    write_rows(DEBUG / "settlement_audit.csv", [summary], list(summary), append_extra=False)
    write_rows(DEBUG / "settlement_schedule.csv", schedule, SCHEDULE_FIELDS, append_extra=False)
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary), encoding="utf-8")


def settlement_schedule(rows: list[dict[str, str]], as_of: date) -> list[dict[str, str]]:
    pending = [row for row in rows if row.get("outcome_status") == "pending_forward_observation"]
    return schedule_rows(pending, "planned_entry_date", "pre_entry_refresh", as_of) + schedule_rows(pending, "planned_exit_date", "forward_settlement", as_of)


def schedule_rows(rows: list[dict[str, str]], date_field: str, event_type: str, as_of: date) -> list[dict[str, str]]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(date_field, "")
        if value:
            counts[value] = counts.get(value, 0) + 1
    out = []
    for event_date, count in sorted(counts.items()):
        status = "due_now" if date.fromisoformat(event_date) <= as_of else "pending"
        action = schedule_action(event_type, status)
        out.append({
            "event_date": event_date,
            "event_type": event_type,
            "row_count": str(count),
            "status": status,
            "action": action,
            "command": schedule_command(event_type, event_date),
        })
    return out


def schedule_action(event_type: str, status: str) -> str:
    if event_type == "pre_entry_refresh":
        return "入场日前重跑 live refresh；仍不自动入场。" if status == "pending" else "今天应重跑 live refresh；仍不自动入场。"
    return "退出日后结算真实 forward return。" if status == "pending" else "今天应结算真实 forward return。"


def schedule_command(event_type: str, event_date: str) -> str:
    if event_type == "pre_entry_refresh":
        return f"python .\\scripts\\run_v4_71_live_refresh.py --trade-date {event_date}"
    return f"python .\\scripts\\settle_v4_72_rebound_leader_forward_returns.py --as-of-date {event_date}"


def next_schedule_action(rows: list[dict[str, str]]) -> dict[str, str]:
    return sorted(rows, key=lambda row: (row.get("status") != "due_now", row.get("event_date", ""), row.get("event_type", "")))[0] if rows else {}


def render_report(summary: dict[str, object]) -> str:
    return "\n".join([
        "# V4.72 前推收益结算",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 截止日期：{summary['as_of_date']}",
        f"- 账本行数：{summary['ledger_rows']}",
        f"- 已结算：{summary['settled_rows']}",
        f"- 待观察：{summary['pending_rows']}",
        f"- 缺价格：{summary['missing_price_rows']}",
        f"- 前推日程行数：{summary['settlement_schedule_rows']}",
        f"- 下一动作日期：{summary['next_action_date']}",
        f"- 下一动作：{summary['next_action']}",
        f"- 生产可用：`{str(summary['production_ready']).lower()}`",
        "",
        "边界：这是前推样本结算工具，不改变候选生成和交易规则。",
    ])


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert as_of_date_error("2026-06-20", date(2026, 6, 20)) == ""
        assert "future" in as_of_date_error("2026-07-21", date(2026, 6, 20))
        history = Path(tmp) / "history"
        history.mkdir()
        for code, exit_price in [("801001", 110.0), ("801002", 120.0)]:
            with (history / f"{code}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["日期", "收盘"])
                writer.writeheader()
                writer.writerow({"日期": "2026-01-02", "收盘": "100"})
                writer.writerow({"日期": "2026-01-03", "收盘": str(exit_price)})
        rows = [{
            "outcome_status": "pending_forward_observation",
            "industry_code": "801001",
            "planned_entry_date": "2026-01-01",
            "planned_exit_date": "2026-01-03",
            "realized_relative_return": "",
        }]
        updated, settled = settle_rows(rows, date(2026, 1, 3), history)
        assert len(settled) == 1
        assert updated[0]["actual_entry_date"] == "2026-01-02"
        assert updated[0]["actual_exit_date"] == "2026-01-03"
        assert round(float(updated[0]["realized_return"]), 6) == 0.1
        assert round(float(updated[0]["benchmark_return"]), 6) == 0.15
        assert round(float(updated[0]["realized_relative_return"]), 6) == -0.05
        not_due, settled = settle_rows([{
            "outcome_status": "pending_forward_observation",
            "industry_code": "801001",
            "planned_entry_date": "2026-01-01",
            "planned_exit_date": "2026-01-04",
            "realized_relative_return": "",
        }], date(2026, 1, 3), history)
        assert not settled
        assert not_due[0]["settlement_status"] == "not_due"
        schedule = settlement_schedule(not_due, date(2026, 1, 3))
        assert any(row["event_type"] == "pre_entry_refresh" and row["status"] == "due_now" for row in schedule)
        assert any(row["event_type"] == "forward_settlement" and row["status"] == "pending" for row in schedule)
        assert next_schedule_action(schedule)["event_date"] == "2026-01-01"
    print("self_check=pass")


if __name__ == "__main__":
    main()
