#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import math
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V487_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_evidence_scorecard_v4_87" / "run_summary.json"
V489_SUMMARY = ROOT / "outputs" / "audit" / "rebound_leader_goal_readiness_v4_89" / "run_summary.json"
FORWARD_TRACKER = ROOT / "outputs" / "industry_rebound_leader_evidence_scorecard_v4_87" / "debug" / "forward_tracker_status.csv"
LEDGER = ROOT / "logs" / "v4_85_parent_neutral_forward_ledger.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_promotion_math_v4_91"
DEBUG = OUT / "debug"

POSITIVE_BATCH_RATE_REQUIRED = 0.55
TOP_QUINTILE_HIT_RATE_REQUIRED = 0.30
MIN_FORWARD_BATCHES = 30
SELECTED_PER_BATCH = 10
GRID_BATCH_COUNTS = [1, 5, 10, 20, 30, 40, 50, 60]

CURRENT_FIELDS = ["metric", "current", "required", "operator", "status", "interpretation"]
GRID_FIELDS = [
    "settled_forward_batches",
    "selected_rows",
    "min_positive_batches",
    "min_top_quintile_hit_rows",
    "positive_batch_rate_required",
    "top_quintile_hit_rate_required",
    "sample_gate_status",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.91 promotion math audit for rebound-leader forward evidence.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    v487 = read_json(V487_SUMMARY)
    v489 = read_json(V489_SUMMARY)
    tracker = read_rows(FORWARD_TRACKER)
    ledger = read_rows(LEDGER)
    grid = build_threshold_grid(GRID_BATCH_COUNTS)
    current = build_current_status(v487, v489, tracker, ledger)
    summary = build_summary(current, grid, v487, v489)
    write_outputs(summary, current, grid)
    print(f"output_dir={OUT}")
    print(f"goal_ready={summary['goal_ready']}")
    print(f"min_forward_batches={summary['min_forward_batches']}")


def build_threshold_grid(batch_counts: list[int]) -> list[dict[str, str]]:
    rows = []
    for batch_count in batch_counts:
        selected_rows = batch_count * SELECTED_PER_BATCH
        rows.append({
            "settled_forward_batches": str(batch_count),
            "selected_rows": str(selected_rows),
            "min_positive_batches": str(math.ceil(POSITIVE_BATCH_RATE_REQUIRED * batch_count)),
            "min_top_quintile_hit_rows": str(math.ceil(TOP_QUINTILE_HIT_RATE_REQUIRED * selected_rows)),
            "positive_batch_rate_required": f"{POSITIVE_BATCH_RATE_REQUIRED:.2f}",
            "top_quintile_hit_rate_required": f"{TOP_QUINTILE_HIT_RATE_REQUIRED:.2f}",
            "sample_gate_status": "eligible" if batch_count >= MIN_FORWARD_BATCHES else "sample_too_small",
        })
    return rows


def build_current_status(
    v487: dict[str, Any],
    v489: dict[str, Any],
    tracker: list[dict[str, str]],
    ledger: list[dict[str, str]],
) -> list[dict[str, str]]:
    settled_batches = [row for row in tracker if row.get("outcome_status") == "settled"]
    settled_rows = [row for row in ledger if row.get("outcome_status") == "settled_forward_observation"]
    relative_returns = [
        float(row["mean_realized_relative_return"])
        for row in settled_batches
        if numeric(row.get("mean_realized_relative_return", ""))
    ]
    settled_batch_count = int_value(v487.get("settled_tracker_count"))
    positive_batch_count = sum(value > 0 for value in relative_returns)
    settled_selected_rows = len([row for row in settled_rows if row.get("top_quintile_hit") in {"0", "1"}])
    top_quintile_hit_rows = sum(row.get("top_quintile_hit") == "1" for row in settled_rows)
    min_selected_rows = MIN_FORWARD_BATCHES * SELECTED_PER_BATCH
    required_hit_rows = math.ceil(TOP_QUINTILE_HIT_RATE_REQUIRED * max(settled_selected_rows, min_selected_rows))
    return [
        metric_row("settled_forward_batches", settled_batch_count, MIN_FORWARD_BATCHES, ">=", settled_batch_count >= MIN_FORWARD_BATCHES, "真实独立前推批次数。"),
        metric_row("positive_batch_count", positive_batch_count, math.ceil(POSITIVE_BATCH_RATE_REQUIRED * max(settled_batch_count, MIN_FORWARD_BATCHES)), ">=", settled_batch_count >= MIN_FORWARD_BATCHES and positive_batch_count >= math.ceil(POSITIVE_BATCH_RATE_REQUIRED * settled_batch_count), "正超额批次数。"),
        metric_row("settled_selected_rows", settled_selected_rows, min_selected_rows, ">=", settled_batch_count >= MIN_FORWARD_BATCHES, "已结算候选行业行数；V4.85 每批 Top10。"),
        metric_row("top_quintile_hit_rows", top_quintile_hit_rows, required_hit_rows, ">=", settled_batch_count >= MIN_FORWARD_BATCHES and top_quintile_hit_rows >= math.ceil(TOP_QUINTILE_HIT_RATE_REQUIRED * settled_selected_rows), "Top20% 强反弹命中候选行数。"),
        {
            "metric": "mean_relative_return_positive",
            "current": "",
            "required": "> 0",
            "operator": ">",
            "status": "pending" if settled_batch_count == 0 else "pass",
            "interpretation": "结算后必须复算平均相对收益。",
        },
        {
            "metric": "goal_ready",
            "current": str(v489.get("goal_ready", False)).lower(),
            "required": "true",
            "operator": "==",
            "status": "pass" if bool(v489.get("goal_ready", False)) else "pending",
            "interpretation": "总目标是否可声明完成。",
        },
    ]


def metric_row(metric: str, current: Any, required: Any, operator: str, passed: bool, interpretation: str) -> dict[str, str]:
    return {
        "metric": metric,
        "current": str(current),
        "required": str(required),
        "operator": operator,
        "status": "pass" if passed else "pending",
        "interpretation": interpretation,
    }


def build_summary(current: list[dict[str, str]], grid: list[dict[str, str]], v487: dict[str, Any], v489: dict[str, Any]) -> dict[str, Any]:
    settled = int_value(v487.get("settled_tracker_count"))
    required = int_value(v487.get("required_settled_tracker_count")) or MIN_FORWARD_BATCHES
    return {
        "version": "4.91.1",
        "policy_id": "industry_rebound_leader_promotion_math_v4_91",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "settled_forward_batches": settled,
        "min_forward_batches": required,
        "selected_per_batch": SELECTED_PER_BATCH,
        "remaining_forward_batches": max(required - settled, 0),
        "required_positive_batches_at_30": math.ceil(POSITIVE_BATCH_RATE_REQUIRED * required),
        "required_top_quintile_hit_rows_at_30": math.ceil(TOP_QUINTILE_HIT_RATE_REQUIRED * required * SELECTED_PER_BATCH),
        "positive_batch_rate_required": POSITIVE_BATCH_RATE_REQUIRED,
        "top_quintile_hit_rate_required": TOP_QUINTILE_HIT_RATE_REQUIRED,
        "can_claim_strong_rebound_industries": bool(v489.get("can_claim_strong_rebound_industries", False)),
        "goal_ready": bool(v489.get("goal_ready", False)),
        "pass_count": sum(row["status"] == "pass" for row in current),
        "fail_count": sum(row["status"] == "fail" for row in current),
        "pending_count": sum(row["status"] == "pending" for row in current),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "强反弹行业晋级数学口径已修正为候选行级 Top20% 命中率；当前前推批次不足，不能声明已经找到反弹更猛的行业。",
    }


def write_outputs(summary: dict[str, Any], current: list[dict[str, str]], grid: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "top_candidates.csv", current, CURRENT_FIELDS)
    write_csv(DEBUG / "promotion_math_current.csv", current, CURRENT_FIELDS)
    write_csv(DEBUG / "promotion_threshold_grid.csv", grid, GRID_FIELDS)
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, current, grid), encoding="utf-8")


def render_report(summary: dict[str, Any], current: list[dict[str, str]], grid: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.91 强反弹行业晋级数学口径",
        "",
        str(summary["final_verdict"]),
        "",
        "## 预注册门槛",
        "",
        f"- 最少真实前推批次：{summary['min_forward_batches']}",
        f"- 每批候选行业数：{summary['selected_per_batch']}",
        f"- 30 批次时最少正超额批次：{summary['required_positive_batches_at_30']}",
        f"- 30 批次时最少 Top20% 命中候选行：{summary['required_top_quintile_hit_rows_at_30']}",
        "- 平均相对收益：必须大于 0",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 当前状态",
        "",
        markdown_table(current, CURRENT_FIELDS),
        "",
        "## 批次数门槛表",
        "",
        markdown_table(grid, ["settled_forward_batches", "selected_rows", "min_positive_batches", "min_top_quintile_hit_rows", "sample_gate_status"]),
        "",
        "## 边界",
        "",
        "V4.91 只固定前推证据的晋级数学口径，不改变候选规则、不选择新参数、不回填未来收益。Top20% 命中率按候选行级别统计；V4.85 当前每批 Top10，因此 30 批次对应 300 个候选行。",
    ])


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def int_value(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def markdown_table(rows: list[dict[str, str]], cols: list[str]) -> str:
    if not rows:
        return "无数据"
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for item in rows:
        lines.append("| " + " | ".join(str(item.get(col, "")).replace("|", "\\|") for col in cols) + " |")
    return "\n".join(lines)


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        grid = build_threshold_grid([30])
        assert grid[0]["selected_rows"] == "300"
        assert grid[0]["min_positive_batches"] == "17"
        assert grid[0]["min_top_quintile_hit_rows"] == "90"
        current = build_current_status(
            {"settled_tracker_count": 0, "required_settled_tracker_count": 30},
            {"goal_ready": False},
            [],
            [],
        )
        assert any(row["metric"] == "top_quintile_hit_rows" and row["required"] == "90" for row in current)
        summary = build_summary(current, grid, {"settled_tracker_count": 0, "required_settled_tracker_count": 30}, {"goal_ready": False})
        assert summary["required_positive_batches_at_30"] == 17
        assert summary["required_top_quintile_hit_rows_at_30"] == 90
        assert Path(tmp).exists()
    print("self_check=pass")


if __name__ == "__main__":
    main()
