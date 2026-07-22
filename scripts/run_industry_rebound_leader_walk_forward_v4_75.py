#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_oos_factor_v4_74 as v474


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
V471_SOURCE = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "source_panel.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_walk_forward_v4_75"
DEBUG = OUT / "debug"

MIN_TRAIN_EVENTS = 8
TRAIN_GATE = "train_events>=8; train_mean_relative>0; train_win_rate>=55%; train_top_quintile>=30%; train_positive_year>=50%"
FINAL_GATE = "executed_event_count>=30; mean/median relative>0; win_rate>=55%; top_quintile_hit_rate>=30%; positive_year_rate>=60%"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.75 walk-forward rebound-leader rule selection.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    latest = pd.read_csv(V472 / "top_candidates.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    state_opp = v474.attach_state(opportunity, trades)
    factor_events = v474.build_event_panel(state_opp)
    year_decisions, executed = walk_forward(factor_events)
    annual = annual_breakdown(executed, year_decisions)
    gate = gate_audit(executed, annual)
    current_state = v474.latest_state_row()
    latest_candidates = current_candidates(latest, year_decisions, current_state)
    summary = build_summary(executed, annual, gate, year_decisions, latest_candidates, current_state)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    latest_candidates.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, year_decisions, gate, latest_candidates, annual), encoding="utf-8")
    factor_events.to_csv(DEBUG / "factor_event_panel.csv", index=False, encoding="utf-8-sig")
    year_decisions.to_csv(DEBUG / "walk_forward_year_decisions.csv", index=False, encoding="utf-8-sig")
    executed.to_csv(DEBUG / "walk_forward_executed_events.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(DEBUG / "walk_forward_annual_breakdown.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    latest_candidates.to_csv(DEBUG / "latest_walk_forward_candidates.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"executed_year_count={summary['executed_year_count']}")


def walk_forward(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    years = sorted(int(x) for x in events["year"].dropna().unique())
    decisions = []
    executed_rows = []
    for year in years:
        train = events[events["year"] < year]
        test = events[events["year"] == year]
        selected = select_rule(train)
        if selected is None:
            decisions.append({
                "test_year": year,
                "decision": "skip_no_train_rule",
                "selected_state_gate_variant": "",
                "selected_factor": "",
                "selected_top_n": "",
                "train_event_count": int(len(train)),
                "train_mean_relative_return": "",
                "train_relative_win_rate": "",
                "train_top_quintile_hit_rate": "",
                "test_event_count": 0,
                "test_mean_relative_return": "",
                "test_top_quintile_hit_rate": "",
            })
            continue
        variant, factor, top_n, stats = selected
        test_rule = test[
            test["state_gate_variant"].eq(variant)
            & test["factor"].eq(factor)
            & test["top_n"].eq(top_n)
        ].copy()
        if not test_rule.empty:
            test_rule["walk_forward_selected_by_year"] = year
            test_rule["walk_forward_train_event_count"] = stats["event_count"]
            executed_rows.append(test_rule)
        decisions.append({
            "test_year": year,
            "decision": "execute_rule",
            "selected_state_gate_variant": variant,
            "selected_factor": factor,
            "selected_top_n": top_n,
            "train_event_count": stats["event_count"],
            "train_mean_relative_return": stats["mean_relative_return"],
            "train_relative_win_rate": stats["relative_win_rate"],
            "train_top_quintile_hit_rate": stats["top_quintile_hit_rate"],
            "test_event_count": int(len(test_rule)),
            "test_mean_relative_return": float(test_rule["relative_return"].mean()) if len(test_rule) else "",
            "test_top_quintile_hit_rate": float(test_rule["top_quintile_hit_rate"].mean()) if len(test_rule) else "",
        })
    executed = pd.concat(executed_rows, ignore_index=True) if executed_rows else pd.DataFrame()
    return pd.DataFrame(decisions), executed


def select_rule(train: pd.DataFrame) -> tuple[str, str, int, dict[str, float]] | None:
    if train.empty:
        return None
    rows = []
    for (variant, factor, top_n), g in train.groupby(["state_gate_variant", "factor", "top_n"]):
        stats = slice_stats(g)
        if train_rule_passes(stats):
            rows.append((variant, factor, int(top_n), stats))
    if not rows:
        return None
    rows.sort(key=lambda item: (
        item[3]["top_quintile_hit_rate"],
        item[3]["mean_relative_return"],
        item[3]["relative_win_rate"],
        item[3]["event_count"],
    ), reverse=True)
    return rows[0]


def slice_stats(frame: pd.DataFrame) -> dict[str, float]:
    yearly = frame.groupby("year")["relative_return"].mean()
    return {
        "event_count": float(len(frame)),
        "mean_relative_return": float(frame["relative_return"].mean()) if len(frame) else 0.0,
        "median_relative_return": float(frame["relative_return"].median()) if len(frame) else 0.0,
        "relative_win_rate": float(frame["relative_win"].mean()) if len(frame) else 0.0,
        "top_quintile_hit_rate": float(frame["top_quintile_hit_rate"].mean()) if len(frame) else 0.0,
        "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
    }


def train_rule_passes(stats: dict[str, float]) -> bool:
    return (
        stats["event_count"] >= MIN_TRAIN_EVENTS
        and stats["mean_relative_return"] > 0
        and stats["relative_win_rate"] >= 0.55
        and stats["top_quintile_hit_rate"] >= 0.30
        and stats["positive_year_rate"] >= 0.50
    )


def annual_breakdown(executed: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    rows = []
    executed_by_year = {int(year): frame for year, frame in executed.groupby("year")} if not executed.empty else {}
    for item in decisions.to_dict("records"):
        year = int(item["test_year"])
        frame = executed_by_year.get(year, pd.DataFrame())
        rows.append({
            "year": year,
            "decision": item["decision"],
            "event_count": int(len(frame)),
            "mean_relative_return": float(frame["relative_return"].mean()) if len(frame) else 0.0,
            "relative_win_rate": float(frame["relative_win"].mean()) if len(frame) else 0.0,
            "top_quintile_hit_rate": float(frame["top_quintile_hit_rate"].mean()) if len(frame) else 0.0,
        })
    return pd.DataFrame(rows)


def gate_audit(executed: pd.DataFrame, annual: pd.DataFrame) -> pd.DataFrame:
    stats = final_stats(executed, annual)
    checks = [
        ("executed_event_count", stats["executed_event_count"], 30, ">="),
        ("mean_relative_return", stats["mean_relative_return"], 0, ">"),
        ("median_relative_return", stats["median_relative_return"], 0, ">"),
        ("relative_win_rate", stats["relative_win_rate"], 0.55, ">="),
        ("top_quintile_hit_rate", stats["top_quintile_hit_rate"], 0.30, ">="),
        ("positive_year_rate", stats["positive_year_rate"], 0.60, ">="),
    ]
    rows = []
    for metric, current, required, op in checks:
        ok = current >= required if op == ">=" else current > required
        rows.append({
            "metric": metric,
            "current": current,
            "operator": op,
            "required": required,
            "status": "pass" if ok else "fail",
        })
    return pd.DataFrame(rows)


def final_stats(executed: pd.DataFrame, annual: pd.DataFrame) -> dict[str, float]:
    if executed.empty:
        return {
            "executed_event_count": 0.0,
            "executed_year_count": 0.0,
            "mean_relative_return": 0.0,
            "median_relative_return": 0.0,
            "relative_win_rate": 0.0,
            "top_quintile_hit_rate": 0.0,
            "positive_year_rate": 0.0,
        }
    used_years = annual[annual["event_count"].gt(0)]
    return {
        "executed_event_count": float(len(executed)),
        "executed_year_count": float(executed["year"].nunique()),
        "mean_relative_return": float(executed["relative_return"].mean()),
        "median_relative_return": float(executed["relative_return"].median()),
        "relative_win_rate": float(executed["relative_win"].mean()),
        "top_quintile_hit_rate": float(executed["top_quintile_hit_rate"].mean()),
        "positive_year_rate": float((used_years["mean_relative_return"] > 0).mean()) if len(used_years) else 0.0,
    }


def current_candidates(latest: pd.DataFrame, decisions: pd.DataFrame, state: dict[str, object]) -> pd.DataFrame:
    out = latest.copy()
    last_exec = decisions[decisions["decision"].eq("execute_rule")].tail(1)
    latest_rule = last_exec.iloc[0].to_dict() if len(last_exec) else {}
    state_ok = bool(state.get("any_passed_state_bucket"))
    out["candidate_status"] = "research_only_walk_forward_blocked"
    out["walk_forward_selected_factor"] = latest_rule.get("selected_factor", "")
    out["walk_forward_selected_variant"] = latest_rule.get("selected_state_gate_variant", "")
    out["walk_forward_selected_top_n"] = latest_rule.get("selected_top_n", "")
    out["state_gate_feature_date"] = str(state.get("trade_date", ""))
    out["latest_any_passed_state_bucket"] = state_ok
    out["manual_review_reason"] = "逐年前推规则未通过总评价体系或当前状态桶未通过，不能作为强反弹行业。"
    cols = [
        "candidate_status",
        "walk_forward_selected_factor",
        "walk_forward_selected_variant",
        "walk_forward_selected_top_n",
        "selection_strategy",
        "planned_entry_date",
        "feature_date",
        "state_gate_feature_date",
        "industry_code",
        "industry_name",
        "selection_score",
        "valuation_score",
        "oversold_score",
        "turn_score",
        "liquidity_score",
        "latest_any_passed_state_bucket",
        "manual_review_reason",
    ]
    return out[[col for col in cols if col in out.columns]]


def build_summary(executed: pd.DataFrame, annual: pd.DataFrame, gate: pd.DataFrame, decisions: pd.DataFrame, latest: pd.DataFrame, state: dict[str, object]) -> dict[str, object]:
    stats = final_stats(executed, annual)
    failed = gate[gate["status"].eq("fail")]["metric"].tolist()
    passed = not failed
    return {
        "version": "4.75.0",
        "policy_id": "industry_rebound_leader_walk_forward_v4_75",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_policy": "industry_rebound_leader_oos_factor_v4_74",
        "year_count": int(decisions["test_year"].nunique()) if len(decisions) else 0,
        "execute_year_count": int(decisions["decision"].eq("execute_rule").sum()) if len(decisions) else 0,
        "skip_year_count": int(decisions["decision"].eq("skip_no_train_rule").sum()) if len(decisions) else 0,
        "executed_event_count": int(stats["executed_event_count"]),
        "executed_year_count": int(stats["executed_year_count"]),
        "mean_relative_return": stats["mean_relative_return"],
        "median_relative_return": stats["median_relative_return"],
        "relative_win_rate": stats["relative_win_rate"],
        "top_quintile_hit_rate": stats["top_quintile_hit_rate"],
        "positive_year_rate": stats["positive_year_rate"],
        "failed_metrics": ";".join(failed),
        "best_status": "pass_stronger_industry_gate" if passed else "research_only_not_validated",
        "latest_candidate_count": int(len(latest)),
        "latest_state_date": str(state.get("trade_date", "")),
        "latest_any_passed_state_bucket": bool(state.get("any_passed_state_bucket")),
        "train_gate": TRAIN_GATE,
        "evaluation_gate": FINAL_GATE,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": (
            "逐年前推规则通过强反弹行业评价，但仍保持 research_only 等待真实前推。"
            if passed
            else "逐年前推规则未证明能稳定选出强反弹行业。"
        ),
    }


def render_report(summary: dict[str, object], decisions: pd.DataFrame, gate: pd.DataFrame, latest: pd.DataFrame, annual: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.75 逐年前推强反弹行业选择审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 每个测试年只使用之前年份已完成的反弹窗口样本训练规则。",
        "- 如果训练期没有规则通过初筛，当年强行业选择跳过，不用当年未来收益反选。",
        "- 规则空间沿用 V4.74：状态桶、可见因子、TopN。",
        "- 最终仍用强反弹行业评价体系判断，而不是看单一年份表现。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 年度前推决策",
        "",
        table(decisions),
        "",
        "## 年度结果",
        "",
        table(annual),
        "",
        "## 门槛审计",
        "",
        table(gate),
        "",
        "## 当前候选状态",
        "",
        table(latest),
        "",
        "## 研究边界",
        "",
        "本版本检验的是反弹窗口内行业选择规则能否逐年前推，不是交易指令。未通过前，所有候选只用于研究观察。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    stats = {"event_count": 8, "mean_relative_return": 0.01, "relative_win_rate": 0.6, "top_quintile_hit_rate": 0.31, "positive_year_rate": 0.5}
    assert train_rule_passes(stats)
    stats["top_quintile_hit_rate"] = 0.29
    assert not train_rule_passes(stats)
    frame = pd.DataFrame({
        "year": [2020] * 4 + [2021] * 4,
        "state_gate_variant": ["v"] * 8,
        "factor": ["f"] * 8,
        "top_n": [5] * 8,
        "relative_return": [0.1, 0.2, 0.1, -0.1, 0.1, 0.2, 0.1, -0.1],
        "relative_win": [True, True, True, False, True, True, True, False],
        "top_quintile_hit_rate": [0.4, 0.4, 0.2, 0.2, 0.4, 0.4, 0.2, 0.2],
    })
    assert select_rule(frame) is not None
    print("self_check=pass")


if __name__ == "__main__":
    main()
