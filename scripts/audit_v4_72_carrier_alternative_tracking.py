#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "industry_candidate_carrier_mapping.csv"
HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
OUT = ROOT / "outputs" / "audit" / "v4_72_carrier_alternative_tracking"
DEBUG = OUT / "debug"

FIELDS = [
    "industry_code",
    "industry_name",
    "candidate_carrier_code",
    "candidate_carrier_name",
    "liquidity_status",
    "discount_status",
    "overlap_days",
    "daily_return_corr",
    "mean_abs_daily_return_gap",
    "carrier_return",
    "industry_return",
    "return_gap",
    "alternative_tracking_status",
    "alternative_rank",
    "action_note",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit V4.72 alternate carrier tracking.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    rows = build_audit(read_rows(MAPPING))
    write_outputs(rows)
    print(f"output_dir={OUT}")
    print(f"rows={len(rows)}")
    print(f"usable_alternative_count={len(usable_alternatives(rows))}")


def build_audit(mapping_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in mapping_rows:
        code = str(item.get("industry_code", "")).zfill(6)
        carrier = str(item.get("candidate_carrier_code", "")).zfill(6)
        if not carrier or carrier == "000000":
            rows.append(empty_row(item, "no_candidate_carrier"))
            continue
        rows.append(track(item, HISTORY_DIR / f"{code}.csv", carrier))
    return rank_by_industry(rows)


def track(item: dict[str, str], industry_path: Path, carrier: str) -> dict[str, Any]:
    if not industry_path.exists():
        return empty_row(item, "industry_history_missing")
    etf = fetch_etf_history(carrier)
    if etf.empty:
        return empty_row(item, "carrier_history_fetch_failed")
    industry = pd.read_csv(industry_path, encoding="utf-8-sig")
    industry["date"] = pd.to_datetime(industry["日期"])
    industry["industry_close"] = pd.to_numeric(industry["收盘"], errors="coerce")
    etf["date"] = pd.to_datetime(etf["date"])
    etf["etf_close"] = pd.to_numeric(etf["close"], errors="coerce")
    merged = industry[["date", "industry_close"]].merge(etf[["date", "etf_close"]], on="date", how="inner").dropna().tail(253)
    if len(merged) < 60:
        return empty_row(item, "insufficient_overlap", overlap_days=len(merged))
    returns = merged[["industry_close", "etf_close"]].pct_change().dropna()
    corr = float(returns["industry_close"].corr(returns["etf_close"]))
    gap = returns["etf_close"] - returns["industry_close"]
    mean_abs_gap = float(gap.abs().mean())
    industry_return = float(merged["industry_close"].iloc[-1] / merged["industry_close"].iloc[0] - 1)
    carrier_return = float(merged["etf_close"].iloc[-1] / merged["etf_close"].iloc[0] - 1)
    return_gap = carrier_return - industry_return
    status = tracking_status(corr, mean_abs_gap, return_gap)
    return {
        **base_row(item),
        "overlap_days": len(merged),
        "daily_return_corr": corr,
        "mean_abs_daily_return_gap": mean_abs_gap,
        "carrier_return": carrier_return,
        "industry_return": industry_return,
        "return_gap": return_gap,
        "alternative_tracking_status": status,
        "alternative_rank": "",
        "action_note": action_note(status, item),
    }


def fetch_etf_history(code: str) -> pd.DataFrame:
    module_path = ROOT / "scripts" / "run_industry_rebound_leader_selection_v4_72.py"
    spec = importlib.util.spec_from_file_location("v472", module_path)
    if spec is None or spec.loader is None:
        return pd.DataFrame()
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fetch_etf_history(code)


def tracking_status(corr: float, mean_abs_gap: float, return_gap: float) -> str:
    if corr >= 0.70 and mean_abs_gap <= 0.03 and abs(return_gap) <= 0.20:
        return "tracking_observed_review_required"
    return "tracking_weak_review_required"


def rank_by_industry(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for _, group in pd.DataFrame(rows).groupby("industry_code", dropna=False):
        group = group.copy()
        if "discount_status" not in group.columns:
            group["discount_status"] = ""
        if "liquidity_status" not in group.columns:
            group["liquidity_status"] = ""
        group["_pass"] = group["alternative_tracking_status"].eq("tracking_observed_review_required").astype(int)
        group["_corr"] = pd.to_numeric(group["daily_return_corr"], errors="coerce").fillna(-1)
        group["_gap"] = pd.to_numeric(group["return_gap"], errors="coerce").abs().fillna(999)
        group["_tradable_pass"] = (group["liquidity_status"].eq("pass") & group["discount_status"].eq("pass")).astype(int)
        group["_usable"] = (group["_pass"].eq(1) & group["_tradable_pass"].eq(1)).astype(int)
        group = group.sort_values(["_usable", "_tradable_pass", "_pass", "_corr", "_gap"], ascending=[False, False, False, False, True])
        for rank, item in enumerate(group.drop(columns=["_pass", "_corr", "_gap", "_tradable_pass", "_usable"]).to_dict("records"), start=1):
            item["alternative_rank"] = rank
            out.append(item)
    return out


def action_note(status: str, item: dict[str, str]) -> str:
    if status == "tracking_observed_review_required" and item.get("liquidity_status") == "pass" and item.get("discount_status") == "pass":
        return "可进入人工复核候选；仍不是自动交易放行。"
    if item.get("liquidity_status") != "pass":
        return "跟踪即使可观察，也因流动性不足只能观察。"
    return "跟踪偏弱，不解除盘前阻断。"


def base_row(item: dict[str, str]) -> dict[str, Any]:
    return {
        "industry_code": str(item.get("industry_code", "")).zfill(6),
        "industry_name": item.get("industry_name", ""),
        "candidate_carrier_code": str(item.get("candidate_carrier_code", "")).zfill(6) if item.get("candidate_carrier_code") else "",
        "candidate_carrier_name": item.get("candidate_carrier_name", ""),
        "liquidity_status": item.get("liquidity_status", ""),
        "discount_status": item.get("discount_status", ""),
    }


def empty_row(item: dict[str, str], status: str, **extra: Any) -> dict[str, Any]:
    return {
        **base_row(item),
        "overlap_days": extra.get("overlap_days", ""),
        "daily_return_corr": "",
        "mean_abs_daily_return_gap": "",
        "carrier_return": "",
        "industry_return": "",
        "return_gap": "",
        "alternative_tracking_status": status,
        "alternative_rank": "",
        "action_note": "无可用跟踪证据，不解除盘前阻断。",
    }


def write_outputs(rows: list[dict[str, Any]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "carrier_alternative_tracking.csv", rows)
    usable = usable_alternatives(rows)
    summary = {
        "version": "v4_72_carrier_alternative_tracking_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "row_count": len(rows),
        "industry_count": len({r["industry_code"] for r in rows}),
        "usable_alternative_count": len(usable),
        "usable_alternative_industry_count": len({r["industry_code"] for r in usable}),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "替代载体审计只用于人工复核；不能单独解除研究系统的生产门禁。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    return "\n".join([
        "# V4.72 替代载体跟踪审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 审计载体数：{summary['row_count']}",
        f"- 覆盖行业数：{summary['industry_count']}",
        f"- 可进入人工复核的替代载体数：{summary['usable_alternative_count']}",
        f"- 可进入人工复核的行业数：{summary['usable_alternative_industry_count']}",
        f"- 生产就绪：`{str(summary['production_ready']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        to_markdown([r for r in rows if int(r.get("alternative_rank") or 99) == 1]),
        "",
        "边界：该审计只检查 ETF/基金载体与申万二级行业指数的历史贴近度，不生成买入/卖出指令。",
    ])


def to_markdown(rows: list[dict[str, Any]]) -> str:
    cols = ["industry_name", "candidate_carrier_code", "candidate_carrier_name", "liquidity_status", "daily_return_corr", "return_gap", "alternative_tracking_status", "action_note"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(format_value(row.get(col, "")) for col in cols) + " |")
    return "\n".join(lines)


def format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value).replace("\n", " ")


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def self_check() -> None:
    with tempfile.TemporaryDirectory():
        rows = rank_by_industry([
            {"industry_code": "1", "alternative_tracking_status": "tracking_weak_review_required", "daily_return_corr": 0.9, "return_gap": 0.1, "liquidity_status": "pass"},
            {"industry_code": "1", "alternative_tracking_status": "tracking_observed_review_required", "daily_return_corr": 0.8, "return_gap": 0.1, "liquidity_status": "pass"},
        ])
        assert rows[0]["alternative_tracking_status"] == "tracking_observed_review_required"
        assert rank_by_industry([]) == []
        rows = rank_by_industry([
            {"industry_code": "1", "alternative_tracking_status": "tracking_observed_review_required", "daily_return_corr": 0.9, "return_gap": 0.1, "liquidity_status": "low_turnover", "discount_status": "pass"},
            {"industry_code": "1", "alternative_tracking_status": "tracking_weak_review_required", "daily_return_corr": 0.7, "return_gap": 0.1, "liquidity_status": "pass", "discount_status": "pass"},
        ])
        assert rows[0]["liquidity_status"] == "pass"
        assert tracking_status(0.7, 0.03, 0.2) == "tracking_observed_review_required"
        assert tracking_status(0.69, 0.03, 0.2) == "tracking_weak_review_required"
        assert len({r["industry_code"] for r in usable_alternatives([{
            "industry_code": "1",
            "alternative_tracking_status": "tracking_observed_review_required",
            "liquidity_status": "pass",
            "discount_status": "pass",
        }, {
            "industry_code": "1",
            "alternative_tracking_status": "tracking_observed_review_required",
            "liquidity_status": "pass",
            "discount_status": "pass",
        }])}) == 1
    print("self_check=pass")


def usable_alternatives(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if row["alternative_tracking_status"] == "tracking_observed_review_required"
        and row["liquidity_status"] == "pass"
        and row["discount_status"] == "pass"
    ]


if __name__ == "__main__":
    main()
