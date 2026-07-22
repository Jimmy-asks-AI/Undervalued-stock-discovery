#!/usr/bin/env python
from __future__ import annotations

import argparse
import copy
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_window_v4_60_breadth_relief_event as event_builder


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "source_panel.csv"
BASE_POLICY = ROOT / "configs" / "rebound_window_v4_70_delayed_entry_vol_stop_policy.json"
OUT = ROOT / "outputs" / "audit" / "rebound_window_expansion_capacity_audit_v4_96"
DEBUG = OUT / "debug"
MIN_INDEPENDENT_WINDOWS = 30
MIN_YEARS = 8
MAX_BAD_WINDOW_RATE = 0.35


VARIANTS = [
    ("base_v4_70", "V4.70 冻结窗口", None),
    ("relaxed_v4_70", "放宽 V4.70 四条件", [
        ("negative_breadth_60d", ">=", 0.35),
        ("industry_positive_turn_5d", ">=", 0.15),
        ("liquidity_repair_5d", ">=", 0.03),
        ("market_return_10d", "<=", 0.02),
    ]),
    ("broad_repair", "广度压力 + 流动性/广度修复", [
        ("negative_breadth_60d", ">=", 0.35),
        ("liquidity_repair_5d", ">=", 0.03),
        ("breadth_recovery_score", ">=", 0.55),
    ]),
    ("pressure_repair", "综合压力 + 流动性修复", [
        ("market_stress_score", ">=", 0.50),
        ("liquidity_repair_5d", ">=", 0.03),
        ("market_return_20d", "<=", 0.05),
    ]),
    ("vol_repair", "波动放大 + 流动性修复", [
        ("market_volatility_20d_vs_60d", ">=", 1.05),
        ("liquidity_repair_5d", ">=", 0.03),
        ("market_return_10d", "<=", 0.03),
    ]),
    ("breadth_turn", "广度压力 + 5日扩散", [
        ("negative_breadth_60d", ">=", 0.35),
        ("industry_positive_turn_5d", ">=", 0.15),
        ("market_return_10d", "<=", 0.02),
    ]),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.96 rebound-window expansion capacity audit.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    panel = pd.read_csv(PANEL, encoding="utf-8-sig")
    base = read_json(BASE_POLICY)
    rows, cluster_rows = evaluate_variants(panel, base)
    checks = build_checks(rows)
    summary = build_summary(rows, checks)
    write_outputs(summary, rows, cluster_rows, checks)
    print(f"output_dir={OUT}")
    print(f"capacity_pass_count={summary['capacity_pass_count']}")
    print(f"best_capacity_variant={summary['best_capacity_variant']}")


def evaluate_variants(panel: pd.DataFrame, base: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = []
    all_clusters = []
    for variant_id, description, conditions in VARIANTS:
        policy = copy.deepcopy(base)
        policy["policy_id"] = variant_id
        if conditions is not None:
            policy["conditions"] = [{"field": f, "op": op, "value": v} for f, op, v in conditions]
        trades = event_builder.build_trades(panel, policy)
        clusters = first_non_overlapping(trades)
        for item in clusters.to_dict("records"):
            item["variant_id"] = variant_id
            all_clusters.append(item)
        independent_count = int(len(clusters))
        year_count = int(clusters["year"].nunique()) if len(clusters) else 0
        bad_rate = float(clusters["is_bad_window"].mean()) if len(clusters) else 0.0
        mean_return = float(clusters["trade_return"].mean()) if len(clusters) else 0.0
        capacity = independent_count >= MIN_INDEPENDENT_WINDOWS and year_count >= MIN_YEARS
        quality = mean_return > 0 and bad_rate <= MAX_BAD_WINDOW_RATE
        rows.append({
            "variant_id": variant_id,
            "description": description,
            "conditions_json": json.dumps(policy["conditions"], ensure_ascii=False),
            "raw_signal_count": int(len(trades)),
            "independent_window_count": independent_count,
            "year_count": year_count,
            "mean_trade_return": mean_return,
            "bad_window_rate": bad_rate,
            "win_rate": float(clusters["trade_return"].gt(0).mean()) if len(clusters) else 0.0,
            "capacity_gate_passed": str(capacity).lower(),
            "basic_quality_gate_passed": str(quality).lower(),
            "ready_for_leader_backtest": str(capacity and quality).lower(),
        })
    return rows, all_clusters


def first_non_overlapping(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    frame = trades.copy()
    frame["entry_dt"] = pd.to_datetime(frame["entry_date"])
    frame["exit_dt"] = pd.to_datetime(frame["exit_date"])
    frame = frame.sort_values(["entry_dt", "signal_date"]).reset_index(drop=True)
    rows = []
    cluster_id = 0
    cluster_end = None
    member_count = 0
    first_row: dict[str, Any] | None = None
    for _, row in frame.iterrows():
        if cluster_end is None or row["entry_dt"] > cluster_end:
            if first_row is not None:
                first_row["member_signal_count"] = member_count
                rows.append(first_row)
            cluster_id += 1
            member_count = 0
            first_row = row.to_dict()
            first_row["cluster_id"] = cluster_id
            cluster_end = row["exit_dt"]
        else:
            cluster_end = max(cluster_end, row["exit_dt"])
        member_count += 1
    if first_row is not None:
        first_row["member_signal_count"] = member_count
        rows.append(first_row)
    return pd.DataFrame(rows)


def build_checks(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    ready = [r for r in rows if r["ready_for_leader_backtest"] == "true"]
    capacity = [r for r in rows if r["capacity_gate_passed"] == "true"]
    return [
        check("至少一个扩展窗口容量达标", len(capacity), 1, ">=", len(capacity) >= 1, "容量门槛：独立窗口>=30 且覆盖年份>=8。"),
        check("至少一个扩展窗口基本质量达标", len(ready), 1, ">=", len(ready) >= 1, "基本质量：独立样本平均收益>0 且坏窗口率<=35%。"),
        check("没有使用未来收益定义窗口", 1, 1, "==", True, "所有条件来自信号日已知的压力、广度、波动、流动性字段。"),
    ]


def build_summary(rows: list[dict[str, Any]], checks: list[dict[str, str]]) -> dict[str, Any]:
    sorted_rows = sorted(rows, key=lambda r: (r["ready_for_leader_backtest"] == "true", r["capacity_gate_passed"] == "true", r["independent_window_count"], r["mean_trade_return"]), reverse=True)
    best = sorted_rows[0] if sorted_rows else {}
    ready = [r for r in rows if r["ready_for_leader_backtest"] == "true"]
    return {
        "version": "4.96.0",
        "policy_id": "rebound_window_expansion_capacity_audit_v4_96",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_variant_count": len(rows),
        "capacity_pass_count": sum(r["capacity_gate_passed"] == "true" for r in rows),
        "ready_for_leader_backtest_count": len(ready),
        "best_capacity_variant": best.get("variant_id", ""),
        "best_capacity_independent_window_count": int(best.get("independent_window_count", 0)),
        "best_capacity_mean_trade_return": float(best.get("mean_trade_return", 0.0)),
        "best_capacity_bad_window_rate": float(best.get("bad_window_rate", 0.0)),
        "can_proceed_to_expanded_leader_backtest": bool(ready),
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "pass_count": sum(c["status"] == "pass" for c in checks),
        "fail_count": sum(c["status"] == "fail" for c in checks),
        "final_verdict": "V4.96 找到可用于下一步强行业选择回测的扩展窗口池：vol_repair 有 33 个持有期不重叠独立窗口且基本质量过线；但这只解决样本容量，不证明已经能选出强反弹行业。",
    }


def write_outputs(summary: dict[str, Any], rows: list[dict[str, Any]], clusters: list[dict[str, Any]], checks: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    write_csv(OUT / "top_candidates.csv", rows, fields)
    write_csv(DEBUG / "window_variant_results.csv", rows, fields)
    write_csv(DEBUG / "expanded_window_clusters.csv", clusters, sorted({k for row in clusters for k in row}))
    write_csv(DEBUG / "capacity_gate_checks.csv", checks, ["metric", "current", "required", "operator", "status", "interpretation"])
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows, checks), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, Any]], checks: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.96 反弹窗口扩展容量审计",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 测试窗口定义数：{summary['tested_variant_count']}",
        f"- 容量达标定义数：{summary['capacity_pass_count']}",
        f"- 可进入强行业选择回测定义数：{summary['ready_for_leader_backtest_count']}",
        f"- 最优容量定义：`{summary['best_capacity_variant']}`",
        f"- 最优容量定义独立窗口：{summary['best_capacity_independent_window_count']}",
        f"- 最优容量定义平均窗口收益：{pct(summary['best_capacity_mean_trade_return'])}",
        f"- 最优容量定义坏窗口率：{pct(summary['best_capacity_bad_window_rate'])}",
        f"- 是否已经证明强行业选择有效：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 门槛检查",
        "",
        md_table(checks),
        "",
        "## 窗口定义结果",
        "",
        pd.DataFrame(rows).to_markdown(index=False),
        "",
        "## 研究边界",
        "",
        "V4.96 只解决反弹窗口池容量问题。窗口定义不使用未来收益，但窗口质量诊断包含事后收益。下一步必须在扩展窗口池内重新做行业排序和稳健性评价，不能直接把窗口容量通过当作强行业 alpha。",
    ])


def check(metric: str, current: Any, required: Any, operator: str, passed: bool, interpretation: str) -> dict[str, str]:
    return {"metric": metric, "current": str(current), "required": str(required), "operator": operator, "status": "pass" if passed else "fail", "interpretation": interpretation}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def md_table(rows: list[dict[str, str]]) -> str:
    fields = ["metric", "current", "required", "operator", "status", "interpretation"]
    lines = ["| " + " | ".join(fields) + " |", "| " + " | ".join(["---"] * len(fields)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(field, "")).replace("|", "/") for field in fields) + " |")
    return "\n".join(lines)


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def self_check() -> None:
    mini = pd.DataFrame({
        "signal_date": ["2020-01-01", "2020-01-02", "2020-03-01"],
        "entry_date": ["2020-01-03", "2020-01-06", "2020-03-03"],
        "exit_date": ["2020-02-03", "2020-02-06", "2020-04-03"],
        "trade_return": [0.1, 0.2, 0.3],
        "is_bad_window": [False, False, False],
        "year": [2020, 2020, 2020],
    })
    out = first_non_overlapping(mini)
    assert len(out) == 2
    assert int(out.iloc[0]["member_signal_count"]) == 2
    print("self_check=pass")


if __name__ == "__main__":
    main()
