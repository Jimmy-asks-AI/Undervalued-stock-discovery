#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
OUT = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_bootstrap_audit"
DEBUG = OUT / "debug"

SEED = 472
ITERATIONS = 2000

FIELDS = [
    "metric",
    "current",
    "bootstrap_p05",
    "bootstrap_p50",
    "bootstrap_p95",
    "required",
    "status",
    "interpretation",
]
SAMPLE_FIELDS = [
    "sample_id",
    "mean_relative_return",
    "relative_win_rate",
    "top_quintile_hit_rate",
    "positive_year_rate",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap V4.72 rebound-leader evidence by resampling events.")
    parser.add_argument("--iterations", type=int, default=ITERATIONS)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    strategy_rows = read_rows(SRC / "debug" / "strategy_results.csv")
    event_rows = read_rows(SRC / "debug" / "industry_event_panel.csv")
    audit_rows, sample_rows = build_audit(strategy_rows, event_rows, args.iterations)
    write_outputs(audit_rows, sample_rows)
    print(f"output_dir={OUT}")
    print(f"rows={len(audit_rows)}")


def build_audit(strategy_rows: list[dict[str, str]], event_rows: list[dict[str, str]], iterations: int = ITERATIONS) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not strategy_rows:
        return ([audit_row("source_strategy_results", "missing", 0, 0, 0, "present", "fail", "缺少策略结果。")], [])
    best = strategy_rows[0]
    best_events = [
        item for item in event_rows
        if item.get("strategy") == best.get("strategy") and item.get("top_n") == best.get("top_n")
    ]
    if not best_events:
        return ([audit_row("source_event_panel", "missing", 0, 0, 0, "present", "fail", "缺少最优策略事件。")], [])
    samples = bootstrap_samples(best_events, iterations)
    metrics = {
        "mean_relative_return": (float_value(best.get("mean_relative_return")), 0.0, "平均相对收益 5% 下界必须为正。"),
        "relative_win_rate": (float_value(best.get("relative_win_rate")), 0.55, "跑赢全行业平均的事件胜率 5% 下界必须不低于 55%。"),
        "top_quintile_hit_rate": (float_value(best.get("top_quintile_hit_rate")), 0.30, "Top20% 强反弹命中率 5% 下界必须不低于 30%。"),
        "positive_year_rate": (float_value(best.get("positive_year_rate")), 0.60, "正年份率 5% 下界必须不低于 60%。"),
    }
    rows = []
    for metric, (current, required, note) in metrics.items():
        values = sorted(float_value(item[metric]) for item in samples)
        p05 = quantile(values, 0.05)
        p50 = quantile(values, 0.50)
        p95 = quantile(values, 0.95)
        rows.append(audit_row(
            metric,
            f"{current:.4f}",
            p05,
            p50,
            p95,
            f">= {required:.2f}" if required else "> 0",
            "pass" if p05 >= required else "fail",
            note,
        ))
    return rows, samples


def bootstrap_samples(events: list[dict[str, str]], iterations: int) -> list[dict[str, str]]:
    rng = random.Random(SEED)
    out = []
    count = len(events)
    for sample_id in range(iterations):
        sample = [events[rng.randrange(count)] for _ in range(count)]
        year_values: dict[str, list[float]] = {}
        for item in sample:
            year_values.setdefault(item.get("year", ""), []).append(float_value(item.get("relative_return")))
        selected_count = sum(int_value(item.get("top_n")) for item in sample)
        top_hits = sum(float_value(item.get("top_quintile_hit_rate")) * int_value(item.get("top_n")) for item in sample)
        rel_values = [float_value(item.get("relative_return")) for item in sample]
        out.append({
            "sample_id": str(sample_id),
            "mean_relative_return": f"{statistics.fmean(rel_values):.8f}",
            "relative_win_rate": f"{sum(value > 0 for value in rel_values) / len(rel_values):.8f}",
            "top_quintile_hit_rate": f"{top_hits / selected_count:.8f}" if selected_count else "0",
            "positive_year_rate": f"{sum(statistics.fmean(values) > 0 for values in year_values.values()) / len(year_values):.8f}" if year_values else "0",
        })
    return out


def audit_row(metric: str, current: str, p05: float, p50: float, p95: float, required: str, status: str, note: str) -> dict[str, str]:
    return {
        "metric": metric,
        "current": current,
        "bootstrap_p05": f"{p05:.4f}",
        "bootstrap_p50": f"{p50:.4f}",
        "bootstrap_p95": f"{p95:.4f}",
        "required": required,
        "status": status,
        "interpretation": note,
    }


def write_outputs(rows: list[dict[str, str]], samples: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows, FIELDS)
    write_rows(DEBUG / "bootstrap_audit.csv", rows, FIELDS)
    write_rows(DEBUG / "bootstrap_samples.csv", samples, SAMPLE_FIELDS)
    summary = {
        "version": "v4_72_rebound_leader_bootstrap_audit_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "iterations": len(samples),
        "row_count": len(rows),
        "pass_count": sum(item["status"] == "pass" for item in rows),
        "fail_count": sum(item["status"] == "fail" for item in rows),
        "failed_metrics": ",".join(item["metric"] for item in rows if item["status"] == "fail"),
        "bootstrap_passes_gate": bool(rows) and all(item["status"] == "pass" for item in rows),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "事件级 bootstrap 未通过全部强反弹行业稳定性门槛。" if any(item["status"] == "fail" for item in rows) else "事件级 bootstrap 通过强反弹行业稳定性门槛，仍需前推和交易载体门禁。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# V4.72 强反弹行业事件级 Bootstrap 审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 重采样次数：{summary['iterations']}",
        f"- 通过：{summary['pass_count']}",
        f"- 失败：{summary['fail_count']}",
        f"- 失败项：{summary['failed_metrics']}",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "| metric | current | bootstrap_p05 | bootstrap_p50 | bootstrap_p95 | required | status | interpretation |",
        "|:---|:---|:---|:---|:---|:---|:---|:---|",
    ]
    for item in rows:
        lines.append(f"| {item['metric']} | {item['current']} | {item['bootstrap_p05']} | {item['bootstrap_p50']} | {item['bootstrap_p95']} | {item['required']} | {item['status']} | {item['interpretation']} |")
    lines += ["", "边界：该审计只评价历史事件重采样稳定性，不构成买入、卖出或自动执行指令。"]
    return "\n".join(lines)


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    frac = pos - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
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
    rows, samples = build_audit(
        [{"strategy": "s", "top_n": "2", "mean_relative_return": "0.02", "relative_win_rate": "0.75", "top_quintile_hit_rate": "0.5", "positive_year_rate": "1.0"}],
        [
            {"strategy": "s", "top_n": "2", "year": "2020", "relative_return": "0.02", "relative_win": "True", "top_quintile_hit_rate": "0.5"},
            {"strategy": "s", "top_n": "2", "year": "2021", "relative_return": "0.01", "relative_win": "True", "top_quintile_hit_rate": "0.5"},
        ],
        20,
    )
    assert len(rows) == 4
    assert len(samples) == 20
    assert quantile([1, 2, 3], 0.5) == 2
    assert any(item["metric"] == "mean_relative_return" and item["status"] == "pass" for item in rows)
    print("self_check=pass")


if __name__ == "__main__":
    main()
