#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "outputs" / "audit" / "rebound_leader_confirmation_filter_audit_v5_14" / "debug" / "confirmation_filter_opportunity_set.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_failure_diagnosis_v5_15"
DEBUG = OUT / "debug"
FILTER_NAME = "no_severe_early_selloff"
FEATURE = "early_beta_score"
TOP_N = 5
CONFIRM_DAYS = 5


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.15 failure diagnosis for the closest rebound-leader rule.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    selected, events = build_selected_rows()
    year_rows = year_diagnostics(events)
    industry_rows = industry_failure_exposure(selected)
    bucket_rows = bucket_diagnostics(events)
    summary = build_summary(events, industry_rows, bucket_rows)
    write_outputs(summary, year_rows, selected, events, industry_rows, bucket_rows)
    print(f"output_dir={OUT}")
    print(f"failure_event_count={summary['failure_event_count']}")
    print(f"dominant_failure_bucket={summary['dominant_failure_bucket']}")


def build_selected_rows() -> tuple[pd.DataFrame, pd.DataFrame]:
    panel = pd.read_csv(PANEL, encoding="utf-8-sig", dtype={"industry_code": str})
    subset = panel[
        (panel["confirm_days"].eq(CONFIRM_DAYS))
        & (panel[f"filter_{FILTER_NAME}"].astype(bool))
    ].copy()
    selected_parts = []
    event_rows = []
    for keys, event in subset.groupby(["signal_date", "entry_date", "confirm_date", "exit_date"], sort=False):
        signal_date, entry_date, confirm_date, exit_date = keys
        selected = event.sort_values(FEATURE, ascending=False).head(TOP_N).copy()
        relative_return = float(selected["relative_return_after_confirm"].mean())
        top_cut = float(event["future_return_after_confirm"].quantile(0.8))
        selected["selected_rank"] = range(1, len(selected) + 1)
        selected["selected_relative_return"] = selected["relative_return_after_confirm"]
        selected["event_relative_return"] = relative_return
        selected["event_top_quintile_hit_rate"] = (selected["future_return_after_confirm"] >= top_cut).mean()
        selected_parts.append(selected)
        event_rows.append({
            "signal_date": signal_date,
            "entry_date": entry_date,
            "confirm_date": confirm_date,
            "exit_date": exit_date,
            "year": int(pd.to_datetime(signal_date).year),
            "relative_return": relative_return,
            "relative_win": relative_return > 0,
            "top_quintile_hit_rate": float((selected["future_return_after_confirm"] >= top_cut).mean()),
            "early_benchmark_return": float(event["early_benchmark_return"].iloc[0]),
            "future_benchmark_return_after_confirm": float(event["future_benchmark_return_after_confirm"].iloc[0]),
            "early_relative_dispersion": float(event["early_relative_return"].std()),
            "selected_industries": "、".join(selected["industry_name"].astype(str).tolist()),
            "failure_type": classify_failure(relative_return, float((selected["future_return_after_confirm"] >= top_cut).mean()), float(event["future_benchmark_return_after_confirm"].iloc[0])),
        })
    return pd.concat(selected_parts, ignore_index=True), pd.DataFrame(event_rows)


def classify_failure(relative_return: float, hit_rate: float, future_benchmark: float) -> str:
    if relative_return > 0 and hit_rate >= 0.30:
        return "pass_event"
    if future_benchmark < 0:
        return "post_confirm_market_down"
    if relative_return <= 0:
        return "selection_underperformed"
    return "weak_top_quintile_hit"


def year_diagnostics(events: pd.DataFrame) -> pd.DataFrame:
    return events.groupby("year").agg(
        event_count=("relative_return", "size"),
        mean_relative_return=("relative_return", "mean"),
        relative_win_rate=("relative_win", "mean"),
        top_quintile_hit_rate=("top_quintile_hit_rate", "mean"),
        failure_count=("failure_type", lambda s: int((s != "pass_event").sum())),
    ).reset_index().sort_values(["mean_relative_return", "year"])


def industry_failure_exposure(selected: pd.DataFrame) -> pd.DataFrame:
    selected = selected.copy()
    selected["is_failure_event"] = selected["event_relative_return"] <= 0
    rows = selected.groupby(["industry_code", "industry_name"]).agg(
        selected_count=("industry_code", "size"),
        failure_selected_count=("is_failure_event", "sum"),
        avg_selected_relative_return=("selected_relative_return", "mean"),
        avg_event_relative_return=("event_relative_return", "mean"),
    ).reset_index()
    rows["failure_selected_rate"] = rows["failure_selected_count"] / rows["selected_count"]
    return rows.sort_values(["failure_selected_count", "avg_event_relative_return"], ascending=[False, True])


def bucket_diagnostics(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for column in ["failure_type", "early_benchmark_bucket", "dispersion_bucket"]:
        frame = events.copy()
        if column == "early_benchmark_bucket":
            frame[column] = pd.cut(frame["early_benchmark_return"], [-1, -0.02, 0, 0.02, 1], labels=["early_down_gt_2pct", "early_down_0_2pct", "early_up_0_2pct", "early_up_gt_2pct"])
        if column == "dispersion_bucket":
            frame[column] = pd.cut(frame["early_relative_dispersion"], [0, 0.02, 0.03, 1], labels=["low_dispersion", "mid_dispersion", "high_dispersion"])
        for bucket, group in frame.groupby(column, observed=False):
            rows.append({
                "bucket_type": column,
                "bucket": str(bucket),
                "event_count": int(len(group)),
                "mean_relative_return": float(group["relative_return"].mean()),
                "relative_win_rate": float(group["relative_win"].mean()),
                "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
            })
    return pd.DataFrame(rows).sort_values(["bucket_type", "mean_relative_return"])


def build_summary(events: pd.DataFrame, industry_rows: pd.DataFrame, bucket_rows: pd.DataFrame) -> dict[str, Any]:
    failures = events[events["failure_type"].ne("pass_event")]
    failure_buckets = bucket_rows[
        bucket_rows["bucket_type"].eq("failure_type") & bucket_rows["bucket"].ne("pass_event")
    ]
    dominant = failure_buckets.sort_values("event_count", ascending=False).iloc[0]
    top_industries = industry_rows.head(5)["industry_name"].astype(str).tolist()
    return {
        "version": "5.15.0",
        "policy_id": "rebound_leader_failure_diagnosis_v5_15",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "diagnosed_rule": f"{FILTER_NAME}+{FEATURE}+Top{TOP_N}+confirm{CONFIRM_DAYS}",
        "event_count": int(len(events)),
        "failure_event_count": int(len(failures)),
        "mean_relative_return": float(events["relative_return"].mean()),
        "dominant_failure_bucket": str(dominant["bucket"]),
        "dominant_failure_bucket_events": int(dominant["event_count"]),
        "top_failure_exposure_industries": "、".join(top_industries),
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_failure_diagnosed_not_solved",
        "final_verdict": "V5.15 只完成失败归因，没有产生通过门槛的新强行业规则。",
    }


def write_outputs(summary: dict[str, Any], year_rows: pd.DataFrame, selected: pd.DataFrame, events: pd.DataFrame, industry_rows: pd.DataFrame, bucket_rows: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    year_rows.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, year_rows, bucket_rows, industry_rows), encoding="utf-8")
    selected.to_csv(DEBUG / "selected_industry_rows.csv", index=False, encoding="utf-8-sig")
    events.to_csv(DEBUG / "event_diagnostics.csv", index=False, encoding="utf-8-sig")
    industry_rows.to_csv(DEBUG / "industry_failure_exposure.csv", index=False, encoding="utf-8-sig")
    bucket_rows.to_csv(DEBUG / "failure_bucket_diagnostics.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], year_rows: pd.DataFrame, bucket_rows: pd.DataFrame, industry_rows: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.15 强反弹行业失败归因审计",
        "",
        summary["final_verdict"],
        "",
        f"- 诊断规则：`{summary['diagnosed_rule']}`",
        f"- 事件数：{summary['event_count']}",
        f"- 失败事件数：{summary['failure_event_count']}",
        f"- 平均相对收益：{pct(summary['mean_relative_return'])}",
        f"- 最大失败桶：`{summary['dominant_failure_bucket']}`，事件数 {summary['dominant_failure_bucket_events']}",
        f"- 高频失败暴露行业：{summary['top_failure_exposure_industries']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 年度诊断",
        "",
        year_rows.to_markdown(index=False),
        "",
        "## 失败桶诊断",
        "",
        bucket_rows.to_markdown(index=False),
        "",
        "## 高频失败暴露行业",
        "",
        industry_rows.head(12).to_markdown(index=False),
        "",
        "边界：V5.15 是失败归因，不是新交易规则；`post_confirm_market_down` 属于事后诊断，不能直接用于入场前过滤。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    assert classify_failure(0.01, 0.4, -0.1) == "pass_event"
    assert classify_failure(-0.01, 0.4, -0.1) == "post_confirm_market_down"
    assert classify_failure(-0.01, 0.4, 0.1) == "selection_underperformed"
    assert classify_failure(0.01, 0.2, 0.1) == "weak_top_quintile_hit"
    print("self_check=pass")


if __name__ == "__main__":
    main()
