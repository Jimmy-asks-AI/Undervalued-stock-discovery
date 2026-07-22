from __future__ import annotations

import argparse
import csv
import json
import math
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from settle_v4_72_rebound_leader_forward_returns import benchmark_return, compute_return


ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
OUT = ROOT / "outputs" / "audit" / "v4_72_tradeable_research_blocked_leader"
DEBUG = OUT / "debug"
LEDGER = ROOT / "logs" / "v4_72_tradeable_research_blocked_forward_ledger.csv"
ENTRY = ROOT / "outputs" / "audit" / "v4_72_entry_readiness" / "top_candidates.csv"
LATEST = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "latest_rebound_leader_candidates.csv"
EVENTS = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "industry_event_panel.csv"
FAILURE = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "failure_diagnosis.csv"
V471_SUMMARY = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "run_summary.json"

FIELDS = [
    "industry_code",
    "industry_name",
    "current_selection_score",
    "historical_failure_flag",
    "event_count",
    "selected_event_count",
    "selected_event_rate",
    "context_mean_relative_return",
    "context_relative_win_rate",
    "context_top_quintile_hit_rate",
    "context_positive_year_rate",
    "context_worst_year",
    "context_worst_year_relative_return",
    "repeated_worst_event_flag",
    "evidence_status",
    "failed_checks",
    "decision_boundary",
]

FORWARD_FIELDS = [
    "industry_code",
    "industry_name",
    "planned_entry_date",
    "planned_exit_date",
    "settlement_status",
    "actual_entry_date",
    "actual_exit_date",
    "realized_return",
    "benchmark_return",
    "realized_relative_return",
    "future_return_rank_pct",
    "future_top_quintile",
    "required_label",
    "required_evidence",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit strong-rebound evidence for tradeable but research-blocked industries.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if error := as_of_date_error(args.as_of_date, date.today()):
        parser.error(error)
    window = read_json(V471_SUMMARY)
    rows = build_rows(read_rows(ENTRY), read_rows(LATEST), read_rows(EVENTS), read_rows(FAILURE))
    current_forward = build_forward_checklist(rows, read_rows(ENTRY), window, date.fromisoformat(args.as_of_date), HISTORY)
    as_of = date.fromisoformat(args.as_of_date)
    forward_rows = settle_forward_ledger(update_forward_ledger(read_rows(LEDGER), current_forward), as_of, HISTORY)
    write_rows(LEDGER, forward_rows, FORWARD_FIELDS)
    write_outputs(rows, forward_rows, as_of)
    print(f"output_dir={OUT}")
    print(f"rows={len(rows)}")
    print("production_ready=False")


def as_of_date_error(value: str, today: date) -> str:
    # ponytail: future guard only; historical as-of replay remains allowed.
    if date.fromisoformat(value) > today:
        return f"--as-of-date {value} is in the future; run tradeable leader audit on or after that date."
    return ""


def build_rows(entry_rows: list[dict[str, str]], latest_rows: list[dict[str, str]], event_rows: list[dict[str, str]], failure_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    latest = {row.get("industry_code", ""): row for row in latest_rows}
    repeated = {row.get("item", "") for row in failure_rows if row.get("category") == "repeated_worst_event_industry"}
    events = [row for row in event_rows if row.get("strategy") == "oversold_liquidity" and row.get("top_n") == "10"]
    targets = [row for row in entry_rows if row.get("tradeable_filter_status") == "structural_reviewable_research_gate_blocked"]
    return [audit_industry(row, latest.get(row.get("industry_code", ""), {}), events, repeated) for row in targets]


def audit_industry(entry: dict[str, str], latest: dict[str, str], events: list[dict[str, str]], repeated: set[str]) -> dict[str, str]:
    code = entry.get("industry_code", "")
    name = entry.get("industry_name", "")
    selected = [row for row in events if code in row.get("selected_industry_codes", "").split("|")]
    year_returns: dict[str, list[float]] = defaultdict(list)
    for row in selected:
        year_returns[row.get("year", "")].append(float_value(row.get("relative_return")))
    yearly = {year: mean(vals) for year, vals in year_returns.items() if vals}
    worst_year, worst_year_return = worst_year_value(yearly)
    checks = failed_checks(selected, yearly, name in repeated)
    status = "candidate_evidence_pass" if not checks else "research_blocked_insufficient_strong_rebound_evidence"
    return {
        "industry_code": code,
        "industry_name": name,
        "current_selection_score": latest.get("selection_score", ""),
        "historical_failure_flag": latest.get("historical_failure_flag", ""),
        "event_count": str(len(events)),
        "selected_event_count": str(len(selected)),
        "selected_event_rate": fmt_ratio(len(selected), len(events)),
        "context_mean_relative_return": fmt_float(mean(float_value(row.get("relative_return")) for row in selected)),
        "context_relative_win_rate": fmt_ratio(sum(row.get("relative_win") == "True" for row in selected), len(selected)),
        "context_top_quintile_hit_rate": fmt_float(mean(float_value(row.get("top_quintile_hit_rate")) for row in selected)),
        "context_positive_year_rate": fmt_ratio(sum(value > 0 for value in yearly.values()), len(yearly)),
        "context_worst_year": worst_year,
        "context_worst_year_relative_return": fmt_float(worst_year_return),
        "repeated_worst_event_flag": str(name in repeated).lower(),
        "evidence_status": status,
        "failed_checks": "；".join(checks),
        "decision_boundary": "交易侧可人工复核；强反弹证据未过则不入场。",
    }


def failed_checks(selected: list[dict[str, str]], yearly: dict[str, float], repeated: bool) -> list[str]:
    checks = []
    if len(selected) < 8:
        checks.append("历史入选事件少于8次")
    if mean(float_value(row.get("relative_return")) for row in selected) <= 0:
        checks.append("入选事件平均相对收益不为正")
    if ratio(sum(row.get("relative_win") == "True" for row in selected), len(selected)) < 0.55:
        checks.append("入选事件跑赢率低于55%")
    if mean(float_value(row.get("top_quintile_hit_rate")) for row in selected) < 0.30:
        checks.append("Top分位命中率低于30%")
    if ratio(sum(value > 0 for value in yearly.values()), len(yearly)) < 0.60:
        checks.append("正收益年份率低于60%")
    if yearly and min(yearly.values()) < -0.02:
        checks.append("最差年份相对收益低于-2%")
    if repeated:
        checks.append("反复出现在最差事件行业")
    return checks


def build_forward_checklist(rows: list[dict[str, str]], entry_rows: list[dict[str, str]], window: dict[str, object], as_of: date, history_dir: Path) -> list[dict[str, str]]:
    entry_by_code = {row.get("industry_code", ""): row for row in entry_rows}
    planned_exit = str(window.get("planned_exit_date", ""))
    first_entry = next((row.get("planned_entry_date", "") for row in entry_rows if row.get("planned_entry_date")), "")
    ranks = future_return_ranks(history_dir, first_entry, planned_exit, as_of)
    out = []
    for row in rows:
        entry = entry_by_code.get(row["industry_code"], {})
        settlement = settle_forward_row(history_dir, row["industry_code"], entry.get("planned_entry_date", ""), planned_exit, as_of, ranks.get(row["industry_code"], {}))
        out.append({
            "industry_code": row["industry_code"],
            "industry_name": row["industry_name"],
            "planned_entry_date": entry.get("planned_entry_date", ""),
            "planned_exit_date": planned_exit,
            **settlement,
            "required_label": "forward_relative_return_and_top_quintile_rank",
            "required_evidence": "到退出日后结算行业forward收益、全行业等权收益、相对收益、是否进入全行业未来收益前20%。",
        })
    return out


def update_forward_ledger(existing: list[dict[str, str]], current: list[dict[str, str]]) -> list[dict[str, str]]:
    # ponytail: CSV ledger is enough here; move to SQLite only if duplicate audits or concurrent writers become real.
    by_key = {forward_key(row): normalize_forward_row(row) for row in existing if forward_key(row)}
    for row in current:
        key = forward_key(row)
        if not key:
            continue
        old = by_key.get(key, {})
        merged = {**old, **normalize_forward_row(row)}
        if old.get("settlement_status") == "settled" and row.get("settlement_status") != "settled":
            merged = {**normalize_forward_row(row), **old}
        by_key[key] = merged
    return [by_key[key] for key in sorted(by_key)]


def settle_forward_ledger(rows: list[dict[str, str]], as_of: date, history_dir: Path) -> list[dict[str, str]]:
    rank_cache: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
    out = []
    for row in rows:
        row = normalize_forward_row(row)
        if row.get("settlement_status") == "settled":
            out.append(row)
            continue
        entry = row.get("planned_entry_date", "")
        exit_ = row.get("planned_exit_date", "")
        if not entry or not exit_ or as_of < date.fromisoformat(exit_):
            row["settlement_status"] = "pending_until_exit_date"
            out.append(row)
            continue
        key = (entry, exit_)
        if key not in rank_cache:
            rank_cache[key] = future_return_ranks(history_dir, entry, exit_, as_of)
        settlement = settle_forward_row(history_dir, row["industry_code"], entry, exit_, as_of, rank_cache[key].get(row["industry_code"], {}))
        row.update(settlement)
        out.append(row)
    return out


def forward_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (row.get("industry_code", ""), row.get("planned_entry_date", ""), row.get("planned_exit_date", ""))


def normalize_forward_row(row: dict[str, str]) -> dict[str, str]:
    return {field: row.get(field, "") for field in FORWARD_FIELDS}


def settle_forward_row(history_dir: Path, code: str, entry_date: str, exit_date: str, as_of: date, rank: dict[str, str]) -> dict[str, str]:
    if not exit_date or as_of < date.fromisoformat(exit_date):
        return empty_settlement("pending_until_exit_date")
    result = compute_return(history_dir / f"{code}.csv", entry_date, exit_date, as_of)
    if not result:
        return empty_settlement("missing_price")
    bench = benchmark_return(history_dir, entry_date, exit_date, as_of)
    realized = float(result["return"])
    return {
        "settlement_status": "settled",
        "actual_entry_date": str(result["entry_date"]),
        "actual_exit_date": str(result["exit_date"]),
        "realized_return": fmt_float(realized),
        "benchmark_return": fmt_float(bench),
        "realized_relative_return": fmt_float(realized - bench),
        "future_return_rank_pct": rank.get("future_return_rank_pct", ""),
        "future_top_quintile": rank.get("future_top_quintile", ""),
    }


def empty_settlement(status: str) -> dict[str, str]:
    return {
        "settlement_status": status,
        "actual_entry_date": "",
        "actual_exit_date": "",
        "realized_return": "",
        "benchmark_return": "",
        "realized_relative_return": "",
        "future_return_rank_pct": "",
        "future_top_quintile": "",
    }


def future_return_ranks(history_dir: Path, entry_date: str, exit_date: str, as_of: date) -> dict[str, dict[str, str]]:
    if not entry_date or not exit_date or as_of < date.fromisoformat(exit_date):
        return {}
    universe = []
    for path in history_dir.glob("*.csv"):
        result = compute_return(path, entry_date, exit_date, as_of)
        if result:
            universe.append((path.stem, float(result["return"])))
    universe.sort(key=lambda item: item[1], reverse=True)
    n = len(universe)
    return {
        code: {
            "future_return_rank_pct": fmt_float((rank + 1) / n),
            "future_top_quintile": str((rank + 1) / n <= 0.20).lower(),
        }
        for rank, (code, _) in enumerate(universe)
        if n
    }


def write_outputs(rows: list[dict[str, str]], forward_rows: list[dict[str, str]], as_of: date) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "tradeable_research_blocked_leader_audit.csv", rows)
    write_rows(DEBUG / "forward_tradeable_leader_checklist.csv", forward_rows, FORWARD_FIELDS)
    passing = [row for row in rows if row["evidence_status"] == "candidate_evidence_pass"]
    best = max(rows, key=lambda row: float_value(row["context_mean_relative_return"]), default={})
    summary = {
        "version": "v4_72_tradeable_research_blocked_leader_audit_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "target_count": len(rows),
        "evidence_pass_count": len(passing),
        "evidence_fail_count": len(rows) - len(passing),
        "best_context_industry": best.get("industry_name", ""),
        "best_context_mean_relative_return": best.get("context_mean_relative_return", ""),
        "best_context_top_quintile_hit_rate": best.get("context_top_quintile_hit_rate", ""),
        "blocked_industries": ",".join(row["industry_name"] for row in rows if row["evidence_status"] != "candidate_evidence_pass"),
        "forward_observation_count": len(forward_rows),
        "forward_observation_status": forward_status(forward_rows),
        "forward_planned_exit_date": forward_rows[0]["planned_exit_date"] if forward_rows else "",
        "forward_settled_count": sum(row["settlement_status"] == "settled" for row in forward_rows),
        "forward_top_quintile_count": sum(row["future_top_quintile"] == "true" for row in forward_rows),
        "forward_ledger_path": str(LEDGER.relative_to(ROOT)),
        "production_ready": False,
        "final_verdict": "结构通过池仍未证明能选出更强反弹行业；只能人工复核，不能自动入场。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, object], rows: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.72 结构通过池强反弹审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 审计行业数：{summary['target_count']}",
        f"- 证据通过：{summary['evidence_pass_count']}",
        f"- 证据失败：{summary['evidence_fail_count']}",
        f"- 历史上下文最好行业：{summary['best_context_industry']}；平均相对收益={summary['best_context_mean_relative_return']}；Top分位命中={summary['best_context_top_quintile_hit_rate']}",
        f"- 阻断行业：{summary['blocked_industries']}",
        f"- 前推观察：{summary['forward_observation_count']}；状态={summary['forward_observation_status']}；退出日={summary['forward_planned_exit_date']}",
        f"- 前推已结算：{summary['forward_settled_count']}；进入前20%：{summary['forward_top_quintile_count']}",
        f"- 前推账本：`{summary['forward_ledger_path']}`",
        "",
        to_markdown(rows),
        "",
        "边界：这里用的是入选事件上下文证据，不是行业单独收益归因；未过门槛时不得升级为买入依据。",
    ])


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str] = FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def forward_status(rows: list[dict[str, str]]) -> str:
    statuses = {row["settlement_status"] for row in rows}
    if not rows:
        return "empty"
    if statuses == {"settled"}:
        return "settled"
    if "missing_price" in statuses:
        return "missing_price"
    return "pending_until_exit_date"


def to_markdown(rows: list[dict[str, str]]) -> str:
    cols = ["industry_name", "selected_event_count", "context_mean_relative_return", "context_relative_win_rate", "context_top_quintile_hit_rate", "context_positive_year_rate", "context_worst_year", "context_worst_year_relative_return", "evidence_status", "failed_checks"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")).replace("|", "/") for col in cols) + " |")
    return "\n".join(lines)


def float_value(value: object) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else 0.0
    except (TypeError, ValueError):
        return 0.0


def mean(values) -> float:
    nums = [float_value(value) for value in values]
    return sum(nums) / len(nums) if nums else 0.0


def ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def fmt_ratio(num: int, den: int) -> str:
    return fmt_float(ratio(num, den))


def fmt_float(value: float) -> str:
    return f"{float_value(value):.6f}"


def worst_year_value(yearly: dict[str, float]) -> tuple[str, float]:
    if not yearly:
        return "", 0.0
    year = min(yearly, key=lambda item: yearly[item])
    return year, yearly[year]


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert as_of_date_error("2026-06-20", date(2026, 6, 20)) == ""
        assert "future" in as_of_date_error("2026-06-23", date(2026, 6, 20))
        rows = build_rows(
            [{"industry_code": "1", "industry_name": "样本", "tradeable_filter_status": "structural_reviewable_research_gate_blocked"}],
            [{"industry_code": "1", "selection_score": "0.9", "historical_failure_flag": "False"}],
            [
                {"strategy": "oversold_liquidity", "top_n": "10", "year": "2020", "relative_return": "0.03", "relative_win": "True", "top_quintile_hit_rate": "0.4", "selected_industry_codes": "1|2"},
                {"strategy": "oversold_liquidity", "top_n": "10", "year": "2021", "relative_return": "-0.01", "relative_win": "False", "top_quintile_hit_rate": "0.2", "selected_industry_codes": "1|3"},
            ],
            [],
        )
        assert rows[0]["selected_event_count"] == "2"
        assert rows[0]["context_mean_relative_return"] == "0.010000"
        assert rows[0]["evidence_status"] == "research_blocked_insufficient_strong_rebound_evidence"
        assert "历史入选事件少于8次" in rows[0]["failed_checks"]
        forward = build_forward_checklist(rows, [{"industry_code": "1", "planned_entry_date": "2026-06-23"}], {"planned_exit_date": "2026-07-21"}, date(2026, 6, 19), Path(tmp))
        assert forward[0]["planned_exit_date"] == "2026-07-21"
        assert forward[0]["settlement_status"] == "pending_until_exit_date"
        history = Path(tmp) / "history"
        history.mkdir()
        for code, exit_price in [("1", 110), ("2", 120), ("3", 90)]:
            with (history / f"{code}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["日期", "收盘"])
                writer.writeheader()
                writer.writerow({"日期": "2026-06-23", "收盘": "100"})
                writer.writerow({"日期": "2026-07-21", "收盘": str(exit_price)})
        settled = build_forward_checklist(rows, [{"industry_code": "1", "planned_entry_date": "2026-06-23"}], {"planned_exit_date": "2026-07-21"}, date(2026, 7, 21), history)
        assert settled[0]["settlement_status"] == "settled"
        assert settled[0]["actual_exit_date"] == "2026-07-21"
        assert settled[0]["future_return_rank_pct"] == "0.666667"
        ledger = update_forward_ledger(forward, settled)
        assert len(ledger) == 1
        assert ledger[0]["settlement_status"] == "settled"
        unchanged = update_forward_ledger(ledger, forward)
        assert unchanged[0]["settlement_status"] == "settled"
        due_existing = settle_forward_ledger([{
            "industry_code": "1",
            "industry_name": "样本",
            "planned_entry_date": "2026-06-23",
            "planned_exit_date": "2026-07-21",
            "settlement_status": "pending_until_exit_date",
        }], date(2026, 7, 21), history)
        assert due_existing[0]["settlement_status"] == "settled"
        assert due_existing[0]["future_return_rank_pct"] == "0.666667"
    print("self_check=pass")


if __name__ == "__main__":
    main()
