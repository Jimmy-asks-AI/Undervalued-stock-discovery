#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
V494 = ROOT / "outputs" / "audit" / "rebound_leader_independent_event_audit_v4_94" / "run_summary.json"
OUT = ROOT / "outputs" / "audit" / "rebound_window_sample_capacity_audit_v4_95"
DEBUG = OUT / "debug"
MIN_INDEPENDENT_WINDOWS = 30


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.95 sample capacity audit for rebound-window leader backtests.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    trades = load_trades()
    clusters = first_non_overlapping_windows(trades)
    year_dist = year_distribution(clusters)
    checks = build_checks(trades, clusters)
    summary = build_summary(trades, clusters, checks)
    write_outputs(summary, clusters, year_dist, checks)
    print(f"output_dir={OUT}")
    print(f"independent_window_count={summary['independent_window_count']}")
    print(f"sample_capacity_status={summary['sample_capacity_status']}")


def load_trades() -> pd.DataFrame:
    df = pd.read_csv(TRADES, encoding="utf-8-sig")
    df["entry_dt"] = pd.to_datetime(df["entry_date"])
    df["exit_dt"] = pd.to_datetime(df["exit_date"])
    df["signal_dt"] = pd.to_datetime(df["signal_date"])
    return df.sort_values(["entry_dt", "signal_dt"]).reset_index(drop=True)


def first_non_overlapping_windows(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    cluster_id = 0
    cluster_end = None
    current_rows: list[dict[str, Any]] = []
    for _, row in trades.iterrows():
        if cluster_end is None or row["entry_dt"] > cluster_end:
            if current_rows:
                rows.append(cluster_row(cluster_id, current_rows, cluster_end))
            cluster_id += 1
            current_rows = []
            cluster_end = row["exit_dt"]
        else:
            cluster_end = max(cluster_end, row["exit_dt"])
        current_rows.append(row.to_dict())
    if current_rows:
        rows.append(cluster_row(cluster_id, current_rows, cluster_end))
    return pd.DataFrame(rows)


def cluster_row(cluster_id: int, rows: list[dict[str, Any]], cluster_end: Any) -> dict[str, Any]:
    first = rows[0]
    return {
        "cluster_id": cluster_id,
        "first_signal_date": first["signal_date"],
        "entry_date": first["entry_date"],
        "exit_date": first["exit_date"],
        "cluster_end_date": str(cluster_end.date()),
        "year": int(first["year"]),
        "member_signal_count": len(rows),
        "trade_return": float(first["trade_return"]),
        "relative_return_horizon": float(first.get("relative_return_horizon", 0.0)),
        "is_bad_window": bool(first.get("is_bad_window", False)),
        "market_stress_score": float(first.get("market_stress_score", 0.0)),
        "negative_breadth_60d": float(first.get("negative_breadth_60d", 0.0)),
        "market_volatility_20d_vs_60d": float(first.get("market_volatility_20d_vs_60d", 0.0)),
    }


def year_distribution(clusters: pd.DataFrame) -> pd.DataFrame:
    return clusters.groupby("year", as_index=False).agg(
        independent_window_count=("cluster_id", "size"),
        mean_trade_return=("trade_return", "mean"),
        bad_window_rate=("is_bad_window", "mean"),
    )


def build_checks(trades: pd.DataFrame, clusters: pd.DataFrame) -> list[dict[str, str]]:
    v494 = read_json(V494)
    return [
        check("原始反弹窗口日信号", len(trades), 30, ">=", len(trades) >= 30, "V4.70 输出的日级触发次数。"),
        check("非重叠独立窗口", len(clusters), MIN_INDEPENDENT_WINDOWS, ">=", len(clusters) >= MIN_INDEPENDENT_WINDOWS, "同一持有期内连续信号只算一个可交易窗口。"),
        check("覆盖年份", int(clusters["year"].nunique()) if len(clusters) else 0, 8, ">=", int(clusters["year"].nunique()) >= 8 if len(clusters) else False, "反弹窗口需要跨多个市场年份。"),
        check("V4.85规则独立事件", int(v494.get("independent_event_count", 0)), MIN_INDEPENDENT_WINDOWS, ">=", int(v494.get("independent_event_count", 0)) >= MIN_INDEPENDENT_WINDOWS, "最接近强行业规则进入选择评价的独立事件数。"),
    ]


def build_summary(trades: pd.DataFrame, clusters: pd.DataFrame, checks: list[dict[str, str]]) -> dict[str, Any]:
    pass_count = sum(row["status"] == "pass" for row in checks)
    fail_count = sum(row["status"] == "fail" for row in checks)
    status = "pass" if len(clusters) >= MIN_INDEPENDENT_WINDOWS else "fail_insufficient_independent_windows"
    return {
        "version": "4.95.0",
        "policy_id": "rebound_window_sample_capacity_audit_v4_95",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "raw_window_signal_count": int(len(trades)),
        "independent_window_count": int(len(clusters)),
        "independent_year_count": int(clusters["year"].nunique()) if len(clusters) else 0,
        "required_independent_window_count": MIN_INDEPENDENT_WINDOWS,
        "v485_rule_independent_event_count": int(read_json(V494).get("independent_event_count", 0)),
        "sample_capacity_status": status,
        "can_complete_strong_industry_goal_with_current_window_pool": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "final_verdict": "当前 V4.70 反弹窗口池只有 19 个持有期不重叠独立窗口，低于 30 个历史评价门槛；在不扩展反弹窗口样本池前，无法仅靠当前历史回测证明能稳定选出强反弹行业。",
    }


def write_outputs(summary: dict[str, Any], clusters: pd.DataFrame, year_dist: pd.DataFrame, checks: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    clusters.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    clusters.to_csv(DEBUG / "all_window_clusters.csv", index=False, encoding="utf-8-sig")
    year_dist.to_csv(DEBUG / "year_distribution.csv", index=False, encoding="utf-8-sig")
    write_csv(DEBUG / "sample_capacity_checks.csv", checks, ["metric", "current", "required", "operator", "status", "interpretation"])
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, checks, year_dist), encoding="utf-8")


def render_report(summary: dict[str, Any], checks: list[dict[str, str]], year_dist: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.95 反弹窗口样本容量审计",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 原始反弹窗口日信号：{summary['raw_window_signal_count']}",
        f"- 持有期不重叠独立窗口：{summary['independent_window_count']}",
        f"- 覆盖年份：{summary['independent_year_count']}",
        f"- 强行业历史评价最低独立窗口门槛：{summary['required_independent_window_count']}",
        f"- V4.85 最接近规则独立事件：{summary['v485_rule_independent_event_count']}",
        f"- 当前窗口池能否完成强行业历史证明：`{str(summary['can_complete_strong_industry_goal_with_current_window_pool']).lower()}`",
        "",
        "## 门槛检查",
        "",
        md_table(checks),
        "",
        "## 年度分布",
        "",
        year_dist.to_markdown(index=False),
        "",
        "## 研究边界",
        "",
        "V4.95 只审计样本容量，不选择行业、不调整阈值、不使用未来收益。结论是当前窗口池容量不足；下一步若继续推进，应先扩展或重定义反弹窗口样本池，再重新做强行业选择评价。",
    ])


def check(metric: str, current: Any, required: Any, operator: str, passed: bool, interpretation: str) -> dict[str, str]:
    return {
        "metric": metric,
        "current": str(current),
        "required": str(required),
        "operator": operator,
        "status": "pass" if passed else "fail",
        "interpretation": interpretation,
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
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


def self_check() -> None:
    df = pd.DataFrame({
        "signal_date": ["2020-01-01", "2020-01-02", "2020-03-01"],
        "entry_date": ["2020-01-03", "2020-01-06", "2020-03-03"],
        "exit_date": ["2020-02-03", "2020-02-06", "2020-04-03"],
        "year": [2020, 2020, 2020],
        "trade_return": [0.1, 0.2, 0.3],
        "relative_return_horizon": [0, 0, 0],
        "is_bad_window": [False, False, False],
    })
    df["entry_dt"] = pd.to_datetime(df["entry_date"])
    df["exit_dt"] = pd.to_datetime(df["exit_date"])
    df["signal_dt"] = pd.to_datetime(df["signal_date"])
    assert len(first_non_overlapping_windows(df)) == 2
    print("self_check=pass")


if __name__ == "__main__":
    main()
