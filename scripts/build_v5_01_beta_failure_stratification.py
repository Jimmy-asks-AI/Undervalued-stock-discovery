#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OPPORTUNITY = ROOT / "outputs" / "industry_rebound_leader_market_sensitivity_v4_99" / "debug" / "market_sensitivity_opportunity_set.csv"
WINDOWS = ROOT / "outputs" / "industry_rebound_leader_expanded_window_v4_97" / "debug" / "expanded_window_trades.csv"
PARENT_PANEL = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "raw_industry_panel.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_beta_failure_stratification_v5_01"
DEBUG = OUT / "debug"

FEATURE = "beta_120_rank"
TOP_N = 5


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.01 beta Top5 failure stratification.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = add_parent(pd.read_csv(OPPORTUNITY, encoding="utf-8-sig", dtype={"industry_code": str}))
    windows = pd.read_csv(WINDOWS, encoding="utf-8-sig")
    events, selected = build_beta_events(opportunity, windows)
    state = state_stratification(events)
    parents = parent_stratification(selected)
    summary = build_summary(events, state, parents)
    write_outputs(summary, events, selected, state, parents)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"failure_event_count={summary['failure_event_count']}")


def add_parent(frame: pd.DataFrame) -> pd.DataFrame:
    parent = pd.read_csv(PARENT_PANEL, encoding="utf-8-sig", dtype={"industry_code": str})
    parent = parent[["industry_code", "parent_industry"]].drop_duplicates()
    parent["industry_code"] = parent["industry_code"].str.zfill(6)
    out = frame.copy()
    out["industry_code"] = out["industry_code"].astype(str).str.zfill(6)
    return out.merge(parent, on="industry_code", how="left")


def build_beta_events(opportunity: pd.DataFrame, windows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    event_rows: list[dict[str, Any]] = []
    selected_rows: list[pd.DataFrame] = []
    keys = ["signal_date", "entry_date", "exit_date"]
    for (signal_date, entry_date, exit_date), group in opportunity.groupby(keys):
        group = group.dropna(subset=[FEATURE, "future_return"]).copy()
        if group.empty:
            continue
        benchmark = float(group["future_return"].mean())
        selected = group.sort_values(FEATURE, ascending=False).head(TOP_N).copy()
        selected_return = float(selected["future_return"].mean()) - 0.001
        relative = selected_return - benchmark
        top_cut = group["future_return"].quantile(0.8)
        parent_counts = selected["parent_industry"].fillna("未知").value_counts()
        row = {
            "signal_date": signal_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "year": int(pd.to_datetime(signal_date).year),
            "benchmark_return": benchmark,
            "selected_net_return": selected_return,
            "relative_return": relative,
            "relative_win": relative > 0,
            "top_quintile_hit_rate": float((selected["future_return"] >= top_cut).mean()),
            "selected_parent_count": int(parent_counts.size),
            "max_parent_weight": float(parent_counts.max() / TOP_N) if len(parent_counts) else 0.0,
            "selected_parents": "|".join(parent_counts.index.astype(str)),
            "selected_industries": "|".join(selected["industry_name"].astype(str)),
        }
        event_rows.append(row)
        selected["event_relative_return"] = relative
        selected["event_relative_win"] = relative > 0
        selected["event_top_quintile_hit_rate"] = row["top_quintile_hit_rate"]
        selected_rows.append(selected)
    events = pd.DataFrame(event_rows)
    if not events.empty:
        events = events.merge(windows, on=["signal_date", "entry_date", "exit_date", "year"], how="left", suffixes=("", "_window"))
        events["failure_type"] = events.apply(failure_type, axis=1)
    selected = pd.concat(selected_rows, ignore_index=True) if selected_rows else pd.DataFrame()
    return events, selected


def failure_type(row: pd.Series) -> str:
    if float(row["relative_return"]) <= 0:
        return "relative_loss"
    if float(row["top_quintile_hit_rate"]) < 0.30:
        return "low_top20_hit"
    return "pass_event"


def state_stratification(events: pd.DataFrame) -> pd.DataFrame:
    specs = [
        ("high_stress", events["market_stress_score"].ge(0.60)),
        ("low_stress", events["market_stress_score"].lt(0.60)),
        ("high_breadth_pressure", events["negative_breadth_60d"].ge(0.50)),
        ("low_breadth_pressure", events["negative_breadth_60d"].lt(0.50)),
        ("high_liquidity_repair", events["liquidity_repair_5d"].ge(0.08)),
        ("low_liquidity_repair", events["liquidity_repair_5d"].lt(0.08)),
        ("high_downside_concentration", events["industry_downside_concentration_20d"].ge(0.50)),
        ("low_downside_concentration", events["industry_downside_concentration_20d"].lt(0.50)),
        ("high_positive_10d", events["industry_positive_10d_ratio"].ge(0.30)),
        ("low_positive_10d", events["industry_positive_10d_ratio"].lt(0.30)),
    ]
    return pd.DataFrame([summary_row(name, events[mask].copy()) for name, mask in specs]).sort_values(
        ["event_count", "mean_relative_return"], ascending=[False, False]
    )


def parent_stratification(selected: pd.DataFrame) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    rows = []
    selected = selected.copy()
    selected["parent_industry"] = selected["parent_industry"].fillna("未知")
    for parent, group in selected.groupby("parent_industry"):
        rows.append({
            "parent_industry": parent,
            "selected_rows": int(len(group)),
            "selected_event_count": int(group[["signal_date", "entry_date", "exit_date"]].drop_duplicates().shape[0]),
            "mean_future_return": float(group["future_return"].mean()),
            "mean_event_relative_return": float(group["event_relative_return"].mean()),
            "relative_win_rate": float(group["event_relative_win"].mean()),
            "avg_event_top_quintile_hit_rate": float(group["event_top_quintile_hit_rate"].mean()),
        })
    return pd.DataFrame(rows).sort_values(["selected_rows", "mean_event_relative_return"], ascending=[False, True])


def summary_row(label: str, sample: pd.DataFrame) -> dict[str, Any]:
    if sample.empty:
        return {
            "bucket": label,
            "event_count": 0,
            "mean_relative_return": 0.0,
            "relative_win_rate": 0.0,
            "top_quintile_hit_rate": 0.0,
            "failure_rate": 0.0,
        }
    return {
        "bucket": label,
        "event_count": int(len(sample)),
        "mean_relative_return": float(sample["relative_return"].mean()),
        "relative_win_rate": float(sample["relative_win"].mean()),
        "top_quintile_hit_rate": float(sample["top_quintile_hit_rate"].mean()),
        "failure_rate": float(sample["failure_type"].ne("pass_event").mean()),
    }


def build_summary(events: pd.DataFrame, state: pd.DataFrame, parents: pd.DataFrame) -> dict[str, Any]:
    failures = events[events["failure_type"].ne("pass_event")]
    worst_state = state[state["event_count"].ge(5)].sort_values("mean_relative_return").head(1)
    worst_parent = parents.sort_values("mean_event_relative_return").head(1) if len(parents) else pd.DataFrame()
    return {
        "version": "5.01.0",
        "policy_id": "rebound_leader_beta_failure_stratification_v5_01",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_rule": "vol_repair + beta_120_rank Top5",
        "event_count": int(len(events)),
        "failure_event_count": int(len(failures)),
        "mean_relative_return": float(events["relative_return"].mean()) if len(events) else 0.0,
        "top_quintile_hit_rate": float(events["top_quintile_hit_rate"].mean()) if len(events) else 0.0,
        "worst_state_bucket": str(worst_state.iloc[0]["bucket"]) if len(worst_state) else "",
        "worst_state_mean_relative_return": float(worst_state.iloc[0]["mean_relative_return"]) if len(worst_state) else 0.0,
        "worst_parent_industry": str(worst_parent.iloc[0]["parent_industry"]) if len(worst_parent) else "",
        "worst_parent_mean_event_relative_return": float(worst_parent.iloc[0]["mean_event_relative_return"]) if len(worst_parent) else 0.0,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_beta_failure_diagnosed",
        "final_verdict": "V5.01 发现 beta_120 Top5 的失败主要需要通过事前窗口状态和父行业暴露继续过滤；该版本只做诊断，不证明目标完成。",
    }


def write_outputs(summary: dict[str, Any], events: pd.DataFrame, selected: pd.DataFrame, state: pd.DataFrame, parents: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    state.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, state, parents), encoding="utf-8")
    events.to_csv(DEBUG / "beta_event_diagnostics.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(DEBUG / "selected_industry_rows.csv", index=False, encoding="utf-8-sig")
    state.to_csv(DEBUG / "state_failure_buckets.csv", index=False, encoding="utf-8-sig")
    parents.to_csv(DEBUG / "parent_failure_buckets.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], state: pd.DataFrame, parents: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.01 Beta 失败分层审计",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 来源规则：`{summary['source_rule']}`",
        f"- 事件数：{summary['event_count']}",
        f"- 失败事件数：{summary['failure_event_count']}",
        f"- 平均相对收益：{pct(summary['mean_relative_return'])}",
        f"- Top20% 命中率：{pct(summary['top_quintile_hit_rate'])}",
        f"- 最差状态桶：`{summary['worst_state_bucket']}`，平均相对收益 {pct(summary['worst_state_mean_relative_return'])}",
        f"- 最差父行业暴露：`{summary['worst_parent_industry']}`，平均事件相对收益 {pct(summary['worst_parent_mean_event_relative_return'])}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 状态分层",
        "",
        state.to_markdown(index=False) if len(state) else "无数据",
        "",
        "## 父行业分层",
        "",
        parents.head(20).to_markdown(index=False) if len(parents) else "无数据",
        "",
        "## 研究边界",
        "",
        "V5.01 只诊断失败集中位置，不新增可交易规则。任何过滤条件都必须在下一版本重新通过完整门槛。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    assert failure_type(pd.Series({"relative_return": -0.1, "top_quintile_hit_rate": 0.4})) == "relative_loss"
    assert failure_type(pd.Series({"relative_return": 0.1, "top_quintile_hit_rate": 0.2})) == "low_top20_hit"
    assert failure_type(pd.Series({"relative_return": 0.1, "top_quintile_hit_rate": 0.4})) == "pass_event"
    print("self_check=pass")


if __name__ == "__main__":
    main()
