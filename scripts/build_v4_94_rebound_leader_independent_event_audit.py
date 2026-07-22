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
SOURCE = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85" / "debug" / "parent_neutral_event_panel.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_independent_event_audit_v4_94"
DEBUG = OUT / "debug"

RULE = {
    "state_gate_variant": "deep_highvol_liq_repair",
    "selection_mode": "global_rank_parent_cap1",
    "factor": "oversold_liquidity_score",
    "top_n": 10,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.94 independent-event audit for V4.85 rebound-leader rule.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    rule_events = load_rule_events()
    clusters = assign_overlap_clusters(rule_events)
    independent = first_signal_per_cluster(clusters)
    checks = build_checks(rule_events, independent)
    summary = build_summary(rule_events, independent, checks)
    write_outputs(summary, independent, clusters, checks)
    print(f"output_dir={OUT}")
    print(f"independent_event_count={summary['independent_event_count']}")
    print(f"independent_sample_gate_passed={summary['independent_sample_gate_passed']}")


def load_rule_events() -> pd.DataFrame:
    df = pd.read_csv(SOURCE, encoding="utf-8-sig")
    mask = pd.Series(True, index=df.index)
    for column, value in RULE.items():
        mask &= df[column].eq(value)
    out = df[mask].copy()
    out["entry_dt"] = pd.to_datetime(out["entry_date"])
    out["exit_dt"] = pd.to_datetime(out["exit_date"])
    out["signal_dt"] = pd.to_datetime(out["signal_date"])
    return out.sort_values(["entry_dt", "signal_dt"]).reset_index(drop=True)


def assign_overlap_clusters(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    cluster_id = 0
    cluster_end = None
    for _, row in events.iterrows():
        if cluster_end is None or row["entry_dt"] > cluster_end:
            cluster_id += 1
            cluster_end = row["exit_dt"]
        else:
            cluster_end = max(cluster_end, row["exit_dt"])
        item = row.to_dict()
        item["independent_cluster_id"] = cluster_id
        item["cluster_end_date"] = str(cluster_end.date())
        rows.append(item)
    return pd.DataFrame(rows)


def first_signal_per_cluster(clusters: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, group in clusters.groupby("independent_cluster_id", sort=True):
        first = group.sort_values(["entry_dt", "signal_dt"]).iloc[0].to_dict()
        first["cluster_event_count"] = int(len(group))
        first["cluster_entry_start"] = str(group["entry_dt"].min().date())
        first["cluster_entry_end"] = str(group["entry_dt"].max().date())
        rows.append(first)
    return pd.DataFrame(rows)


def build_checks(all_events: pd.DataFrame, independent: pd.DataFrame) -> list[dict[str, str]]:
    return [
        check("原始日信号数", len(all_events), 30, ">=", len(all_events) >= 30, "V4.85 点估计的事件数。"),
        check("独立事件数", len(independent), 30, ">=", len(independent) >= 30, "按持有期重叠合并后，每个簇只保留第一条可交易信号。"),
        check("覆盖年份数", int(independent["year"].nunique()) if len(independent) else 0, 5, ">=", int(independent["year"].nunique()) >= 5 if len(independent) else False, "独立样本需要至少 5 个年份。"),
        check("独立样本平均相对收益", mean(independent, "relative_return"), 0.0, ">", mean(independent, "relative_return") > 0, "候选组合相对全行业等权。"),
        check("独立样本 Top20% 命中率", mean(independent, "top_quintile_hit_rate"), 0.30, ">=", mean(independent, "top_quintile_hit_rate") >= 0.30, "候选行业行级 Top20% 命中均值。"),
    ]


def build_summary(all_events: pd.DataFrame, independent: pd.DataFrame, checks: list[dict[str, str]]) -> dict[str, Any]:
    sample_pass = len(independent) >= 30 and independent["year"].nunique() >= 5 if len(independent) else False
    performance_pass = mean(independent, "relative_return") > 0 and mean(independent, "top_quintile_hit_rate") >= 0.30
    return {
        "version": "4.94.0",
        "policy_id": "rebound_leader_independent_event_audit_v4_94",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_rule": "deep_highvol_liq_repair + global_rank_parent_cap1 + oversold_liquidity_score Top10",
        "raw_event_count": int(len(all_events)),
        "independent_event_count": int(len(independent)),
        "independent_year_count": int(independent["year"].nunique()) if len(independent) else 0,
        "independent_mean_relative_return": mean(independent, "relative_return"),
        "independent_top_quintile_hit_rate": mean(independent, "top_quintile_hit_rate"),
        "independent_positive_year_rate": positive_year_rate(independent),
        "independent_sample_gate_passed": sample_pass,
        "independent_performance_gate_passed": performance_pass,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "V4.85 的 32 个日信号按持有期不重叠口径只剩 8 个独立反弹事件；点估计仍为正，但独立样本数远低于 30，不能证明已经稳定找到强反弹行业。",
        "pass_count": sum(row["status"] == "pass" for row in checks),
        "fail_count": sum(row["status"] == "fail" for row in checks),
    }


def write_outputs(summary: dict[str, Any], independent: pd.DataFrame, clusters: pd.DataFrame, checks: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    cols = [
        "independent_cluster_id",
        "signal_date",
        "entry_date",
        "exit_date",
        "year",
        "relative_return",
        "top_quintile_hit_rate",
        "cluster_event_count",
        "selected_industries",
        "selected_parents",
    ]
    independent[[c for c in cols if c in independent.columns]].to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    independent.to_csv(DEBUG / "independent_event_rows.csv", index=False, encoding="utf-8-sig")
    clusters.to_csv(DEBUG / "cluster_members.csv", index=False, encoding="utf-8-sig")
    write_csv(DEBUG / "independent_gate_checks.csv", checks, ["metric", "current", "required", "operator", "status", "interpretation"])
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, checks, independent), encoding="utf-8")


def render_report(summary: dict[str, Any], checks: list[dict[str, str]], independent: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.94 强反弹行业独立事件审计",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 原始日信号数：{summary['raw_event_count']}",
        f"- 持有期不重叠独立事件数：{summary['independent_event_count']}",
        f"- 独立事件覆盖年份：{summary['independent_year_count']}",
        f"- 独立事件平均相对收益：{pct(summary['independent_mean_relative_return'])}",
        f"- 独立事件 Top20% 命中率：{pct(summary['independent_top_quintile_hit_rate'])}",
        f"- 独立样本门槛是否通过：`{str(summary['independent_sample_gate_passed']).lower()}`",
        "",
        "## 门槛检查",
        "",
        md_table(checks),
        "",
        "## 独立事件",
        "",
        independent[["independent_cluster_id", "signal_date", "entry_date", "exit_date", "year", "relative_return", "top_quintile_hit_rate", "cluster_event_count"]].to_markdown(index=False),
        "",
        "## 研究边界",
        "",
        "V4.94 不改变 V4.85 规则，只改变证据粒度：同一持有期内的连续信号不能当作多个独立证明。该版本用于防止样本重叠夸大历史有效性。",
    ])


def check(metric: str, current: Any, required: Any, operator: str, passed: bool, interpretation: str) -> dict[str, str]:
    return {
        "metric": metric,
        "current": f"{current:.6f}" if isinstance(current, float) else str(current),
        "required": str(required),
        "operator": operator,
        "status": "pass" if passed else "fail",
        "interpretation": interpretation,
    }


def mean(df: pd.DataFrame, column: str) -> float:
    return float(df[column].mean()) if len(df) else 0.0


def positive_year_rate(df: pd.DataFrame) -> float:
    return float((df.groupby("year")["relative_return"].mean() > 0).mean()) if len(df) else 0.0


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


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
    })
    df["entry_dt"] = pd.to_datetime(df["entry_date"])
    df["exit_dt"] = pd.to_datetime(df["exit_date"])
    df["signal_dt"] = pd.to_datetime(df["signal_date"])
    clustered = assign_overlap_clusters(df)
    assert clustered["independent_cluster_id"].tolist() == [1, 1, 2]
    assert len(first_signal_per_cluster(clustered)) == 2
    print("self_check=pass")


if __name__ == "__main__":
    main()
