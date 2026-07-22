#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V471 = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit"
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
OUT = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_state_guardrail"
DEBUG = OUT / "debug"

FIELDS = [
    "dimension",
    "bucket",
    "event_count",
    "year_count",
    "mean_relative_return",
    "relative_win_rate",
    "top_quintile_hit_rate",
    "positive_year_rate",
    "status",
    "guardrail_action",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build observable state guardrails for V4.72 rebound-leader selection.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    rows = build_rows(
        read_rows(V472 / "debug" / "strategy_results.csv"),
        read_rows(V472 / "debug" / "industry_event_panel.csv"),
        read_rows(V471 / "debug" / "base_v4_70_trades.csv"),
    )
    write_outputs(rows)
    print(f"output_dir={OUT}")
    print(f"rows={len(rows)}")


def build_rows(strategy_rows: list[dict[str, str]], event_rows: list[dict[str, str]], trade_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not strategy_rows:
        return []
    best = strategy_rows[0]
    best_events = [
        item for item in event_rows
        if item.get("strategy") == best.get("strategy") and item.get("top_n") == best.get("top_n")
    ]
    trade_by_key = {event_key(item): item for item in trade_rows}
    enriched = []
    for event in best_events:
        trade = trade_by_key.get(event_key(event), {})
        if not trade:
            continue
        item = dict(event)
        item.update({
            "volatility_guard": volatility_guard(trade),
            "stress_level": stress_level(trade),
            "negative_breadth": negative_breadth(trade),
        })
        enriched.append(item)
    rows = []
    for dimension in ["volatility_guard", "stress_level", "negative_breadth"]:
        buckets = sorted({item[dimension] for item in enriched if item.get(dimension)})
        for bucket in buckets:
            group = [item for item in enriched if item.get(dimension) == bucket]
            rows.append(state_row(dimension, bucket, group))
    return sorted(rows, key=lambda item: (status_rank(item["status"]), item["dimension"], item["bucket"]))


def state_row(dimension: str, bucket: str, group: list[dict[str, str]]) -> dict[str, str]:
    event_count = len(group)
    years: dict[str, list[float]] = {}
    selected_count = sum(int_value(item.get("top_n")) for item in group)
    top_hits = sum(float_value(item.get("top_quintile_hit_rate")) * int_value(item.get("top_n")) for item in group)
    rels = [float_value(item.get("relative_return")) for item in group]
    for item in group:
        years.setdefault(item.get("year", ""), []).append(float_value(item.get("relative_return")))
    positive_year_rate = sum(mean(values) > 0 for values in years.values()) / len(years) if years else 0.0
    top_rate = top_hits / selected_count if selected_count else 0.0
    mean_relative = mean(rels)
    win_rate = sum(value > 0 for value in rels) / len(rels) if rels else 0.0
    low_data = event_count < 8 or len(years) < 3
    passed = mean_relative > 0 and win_rate >= 0.55 and top_rate >= 0.30 and positive_year_rate >= 0.60
    if low_data:
        status = "low_data"
        action = "样本不足，只能解释，不能作为生产护栏。"
    elif passed:
        status = "pass"
        action = "可作为人工复核的正向状态证据，仍不自动执行。"
    else:
        status = "fail"
        action = "该状态下强行业排序降级为只观察，不得提高入场信心。"
    return {
        "dimension": dimension,
        "bucket": bucket,
        "event_count": str(event_count),
        "year_count": str(len(years)),
        "mean_relative_return": f"{mean_relative:.4f}",
        "relative_win_rate": f"{win_rate:.4f}",
        "top_quintile_hit_rate": f"{top_rate:.4f}",
        "positive_year_rate": f"{positive_year_rate:.4f}",
        "status": status,
        "guardrail_action": action,
    }


def volatility_guard(row: dict[str, str]) -> str:
    return "高波动保护区" if float_value(row.get("market_volatility_20d_vs_60d")) >= 1.30 else "非高波动区"


def stress_level(row: dict[str, str]) -> str:
    score = float_value(row.get("market_stress_score"))
    if score <= 0.55:
        return "低/中压力"
    if score <= 0.70:
        return "中高压力"
    return "高压力"


def negative_breadth(row: dict[str, str]) -> str:
    return "深负广度" if float_value(row.get("negative_breadth_60d")) >= 0.75 else "普通负广度"


def write_outputs(rows: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "state_guardrail.csv", rows)
    failed = [f"{item['dimension']}:{item['bucket']}" for item in rows if item["status"] == "fail"]
    low_data = [f"{item['dimension']}:{item['bucket']}" for item in rows if item["status"] == "low_data"]
    summary = {
        "version": "v4_72_rebound_leader_state_guardrail_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "row_count": len(rows),
        "pass_count": sum(item["status"] == "pass" for item in rows),
        "fail_count": len(failed),
        "low_data_count": len(low_data),
        "failed_buckets": "；".join(failed),
        "low_data_buckets": "；".join(low_data),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "存在失败或样本不足状态桶，强行业排序仍不能作为生产门禁。" if failed or low_data else "状态护栏全部通过，仍需前推和交易载体门禁。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# V4.72 强行业排序状态护栏审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 通过：{summary['pass_count']}",
        f"- 失败：{summary['fail_count']}",
        f"- 样本不足：{summary['low_data_count']}",
        f"- 失败状态桶：{summary['failed_buckets']}",
        f"- 样本不足状态桶：{summary['low_data_buckets']}",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "| dimension | bucket | event_count | year_count | mean_relative_return | relative_win_rate | top_quintile_hit_rate | positive_year_rate | status | guardrail_action |",
        "|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|",
    ]
    for item in rows:
        lines.append(f"| {item['dimension']} | {item['bucket']} | {item['event_count']} | {item['year_count']} | {item['mean_relative_return']} | {item['relative_win_rate']} | {item['top_quintile_hit_rate']} | {item['positive_year_rate']} | {item['status']} | {item['guardrail_action']} |")
    lines += ["", "边界：状态桶来自 signal_date 当时可见市场状态；该审计只用于人工复核护栏，不构成交易指令。"]
    return "\n".join(lines)


def event_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (row.get("signal_date", ""), row.get("entry_date", ""), row.get("exit_date", ""))


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def status_rank(status: str) -> int:
    return {"fail": 0, "low_data": 1, "pass": 2}.get(status, 9)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def int_value(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def self_check() -> None:
    rows = build_rows(
        [{"strategy": "s", "top_n": "2"}],
        [
            {"strategy": "s", "top_n": "2", "signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "year": "2020", "relative_return": "0.1", "relative_win": "True", "top_quintile_hit_rate": "0.5"},
            {"strategy": "s", "top_n": "2", "signal_date": "2021-01-01", "entry_date": "2021-01-02", "exit_date": "2021-01-03", "year": "2021", "relative_return": "-0.1", "relative_win": "False", "top_quintile_hit_rate": "0.0"},
        ],
        [
            {"signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "market_volatility_20d_vs_60d": "1.4", "market_stress_score": "0.8", "negative_breadth_60d": "0.8"},
            {"signal_date": "2021-01-01", "entry_date": "2021-01-02", "exit_date": "2021-01-03", "market_volatility_20d_vs_60d": "1.0", "market_stress_score": "0.5", "negative_breadth_60d": "0.2"},
        ],
    )
    assert rows
    assert volatility_guard({"market_volatility_20d_vs_60d": "1.3"}) == "高波动保护区"
    assert stress_level({"market_stress_score": "0.7"}) == "中高压力"
    assert negative_breadth({"negative_breadth_60d": "0.8"}) == "深负广度"
    assert any(item["status"] == "low_data" for item in rows)
    print("self_check=pass")


if __name__ == "__main__":
    main()
