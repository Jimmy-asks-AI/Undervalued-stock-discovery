#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from append_v5_05_rebound_leader_forward_sample import FIELDS, LEDGER, read_rows, write_rows


ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
SNAPSHOT_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_snapshots" / "second"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_forward_settlement_v5_06"
DEBUG = OUT / "debug"
MIN_INDUSTRY_COVERAGE = 120


def main() -> None:
    parser = argparse.ArgumentParser(description="Settle due V5.05 frozen-rule forward samples.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    as_of = datetime.fromisoformat(args.as_of_date).date()
    before = read_rows(LEDGER)
    required_dates = {
        value for row in before if row.get("settlement_status") != "settled"
        and (parse_date(row.get("exit_date", "")) or date.max) <= as_of
        for value in [parse_date(row.get("entry_date", "")), parse_date(row.get("exit_date", ""))] if value
    }
    hist = load_history(HISTORY_DIR, required_dates)
    after, settled = settle_rows(before, hist, as_of)
    if after != before:
        write_rows(LEDGER, after)
    summary = build_summary(before, after, settled, as_of)
    write_outputs(summary, after, settled)
    print(f"output_dir={OUT}")
    print(f"settled_rows={summary['settled_rows']}")
    print(f"ledger={LEDGER}")


def load_history(path: Path, required_dates: set[date]) -> pd.DataFrame:
    snapshots = list(SNAPSHOT_DIR.glob("*.csv"))
    if not required_dates or not snapshots:
        return pd.DataFrame(columns=["trade_date", "industry_code", "industry_name", "close_index"])
    snapshot = pd.read_csv(max(snapshots), encoding="utf-8-sig", dtype={"行业代码": str})
    names = dict(zip(snapshot["行业代码"].str.zfill(6), snapshot["行业名称"]))
    pieces = []
    for file in path.glob("*.csv"):
        frame = pd.read_csv(file, encoding="utf-8-sig", usecols=["日期", "收盘"])
        frame["trade_date"] = pd.to_datetime(frame["日期"]).dt.date
        frame = frame[frame["trade_date"].isin(required_dates)].copy()
        if frame.empty:
            continue
        frame["industry_code"] = file.stem.zfill(6)
        frame["industry_name"] = names.get(frame["industry_code"].iloc[0], "")
        frame["close_index"] = pd.to_numeric(frame["收盘"], errors="coerce")
        pieces.append(frame[["trade_date", "industry_code", "industry_name", "close_index"]])
    return pd.concat(pieces, ignore_index=True).dropna(subset=["close_index"]) if pieces else pd.DataFrame()


def settle_rows(rows: list[dict[str, str]], hist: pd.DataFrame, as_of: date) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    out = []
    settled = []
    for row in rows:
        item = dict(row)
        if row.get("settlement_status") == "settled":
            out.append(item)
            continue
        exit_date = parse_date(row.get("exit_date", ""))
        if not exit_date or exit_date > as_of:
            out.append(item)
            continue
        result = settle_one(row, hist)
        item.update(result)
        item["recorded_at"] = datetime.now().isoformat(timespec="seconds")
        out.append(item)
        if item.get("settlement_status") == "settled":
            settled.append(item)
    return out, settled


def settle_one(row: dict[str, str], hist: pd.DataFrame) -> dict[str, str]:
    names = [part.strip() for part in row.get("selected_industries", "").split("|") if part.strip()]
    if not names:
        return {"settlement_status": "pending_missing_selected_industries", "notes": append_note(row, "缺少 selected_industries，无法自动结算")}
    entry = parse_date(row.get("entry_date", ""))
    exit_ = parse_date(row.get("exit_date", ""))
    if not entry or not exit_:
        return {"settlement_status": "pending_bad_dates", "notes": append_note(row, "日期格式错误")}
    returns = industry_returns(hist, names, entry, exit_)
    if len(returns) < len(set(names)):
        return {"settlement_status": "pending_missing_price", "notes": append_note(row, "缺少入场或退出价格")}
    benchmark = benchmark_return(hist, entry, exit_)
    if len(benchmark) < MIN_INDUSTRY_COVERAGE:
        return {"settlement_status": "pending_incomplete_benchmark", "notes": append_note(row, "全行业基准同日覆盖不足")}
    selected_net = float(returns["return"].mean()) - 0.001
    top_cut = benchmark["return"].quantile(0.8)
    hit_rate = float((returns["return"] >= top_cut).mean())
    return {
        "benchmark_return": f"{float(benchmark['return'].mean()):.10f}",
        "selected_net_return": f"{selected_net:.10f}",
        "relative_return": f"{selected_net - float(benchmark['return'].mean()):.10f}",
        "top_quintile_hit_rate": f"{hit_rate:.10f}",
        "settlement_status": "settled",
        "notes": append_note(row, "自动结算"),
    }


def industry_returns(hist: pd.DataFrame, names: list[str], entry: date, exit_: date) -> pd.DataFrame:
    universe = hist[hist["industry_name"].isin(names) | hist["industry_code"].isin([name.zfill(6) for name in names])]
    return returns_between(universe, entry, exit_)


def benchmark_return(hist: pd.DataFrame, entry: date, exit_: date) -> pd.DataFrame:
    return returns_between(hist, entry, exit_)


def returns_between(hist: pd.DataFrame, entry: date, exit_: date) -> pd.DataFrame:
    entry_rows = hist[hist["trade_date"].eq(entry)].drop_duplicates("industry_code", keep="last")
    exit_rows = hist[hist["trade_date"].eq(exit_)].drop_duplicates("industry_code", keep="last")
    merged = entry_rows[["industry_code", "industry_name", "close_index"]].merge(
        exit_rows[["industry_code", "close_index"]], on="industry_code", suffixes=("_entry", "_exit")
    )
    merged["return"] = merged["close_index_exit"] / merged["close_index_entry"] - 1.0
    return merged


def parse_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def append_note(row: dict[str, str], note: str) -> str:
    old = row.get("notes", "")
    return f"{old}; {note}".strip("; ")


def build_summary(before: list[dict[str, str]], after: list[dict[str, str]], settled: list[dict[str, str]], as_of: date) -> dict[str, Any]:
    return {
        "version": "5.06.0",
        "policy_id": "rebound_leader_forward_settlement_v5_06",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "ledger_rows": len(after),
        "settled_rows": len(settled),
        "pending_rows": sum(row.get("settlement_status", "").startswith("pending") for row in after),
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_forward_settlement",
        "final_verdict": "V5.06 只结算到期前推样本；结算样本不足前，不能声称目标完成。",
    }


def write_outputs(summary: dict[str, Any], ledger: list[dict[str, str]], settled: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    top = pd.DataFrame(settled if settled else ledger)
    top.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary), encoding="utf-8")
    write_debug_csv(DEBUG / "settled_forward_rows.csv", settled)
    write_debug_csv(DEBUG / "forward_ledger_snapshot.csv", ledger)
    write_debug_csv(DEBUG / "settlement_audit.csv", [stringify(summary)])


def write_debug_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = list(rows[0]) if rows else FIELDS
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def render_report(summary: dict[str, Any]) -> str:
    return "\n".join([
        "# V5.06 前推样本结算",
        "",
        summary["final_verdict"],
        "",
        f"- as-of 日期：{summary['as_of_date']}",
        f"- 账本行数：{summary['ledger_rows']}",
        f"- 本次结算行数：{summary['settled_rows']}",
        f"- 待结算行数：{summary['pending_rows']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "边界：只结算 V5.04 冻结规则产生的未来样本，不新增历史回填样本，不改变规则阈值。",
    ])


def stringify(payload: dict[str, Any]) -> dict[str, str]:
    return {key: str(value) for key, value in payload.items()}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    rows = []
    for index in range(MIN_INDUSTRY_COVERAGE):
        code, name = f"{index:06d}", "A" if index == 0 else f"I{index}"
        rows += [
            {"trade_date": date(2026, 1, 1), "industry_code": code, "industry_name": name, "close_index": 100.0},
            {"trade_date": date(2026, 1, 2), "industry_code": code, "industry_name": name, "close_index": 110.0 if index == 0 else 105.0},
        ]
    hist = pd.DataFrame(rows)
    row = {
        "frozen_rule": "quality_score_ge2", "entry_date": "2026-01-01", "exit_date": "2026-01-02",
        "selected_industries": "A", "settlement_status": "pending", "notes": "",
    }
    result = settle_one(row, hist)
    assert result["settlement_status"] == "settled"
    assert float(result["relative_return"]) > 0
    assert returns_between(hist, date(2026, 1, 1), date(2026, 1, 3)).empty
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.csv"
        write_rows(path, [{field: row.get(field, "") for field in FIELDS}])
        assert read_rows(path)[0]["frozen_rule"] == "quality_score_ge2"
    print("self_check=pass")


if __name__ == "__main__":
    main()
