#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_oos_factor_v4_74 as v474
import run_industry_rebound_leader_robust_grid_v4_80 as v480


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_market_state_v4_81"
DEBUG = OUT / "debug"

FEATURES = ["oversold_liquidity_score", "oversold_score", "liquidity_score", "oversold_turn_score"]
TOP_NS = [5, 8, 10, 12, 15, 20]
STATE_DEFINITIONS = {
    "deep_or_high_vol": "negative_breadth_60d>=0.75 OR market_volatility_20d_vs_60d>=1.30",
    "deep_highvol_liq_repair": "deep_or_high_vol AND liquidity_repair_5d>0",
    "deep_highvol_breadth_recovery": "deep_or_high_vol AND breadth_recovery_score>=0.60",
    "deep_highvol_panic_exhaustion": "deep_or_high_vol AND panic_exhaustion_score>=0.60",
    "deep_highvol_amount_expansion": "deep_or_high_vol AND market_amount_5d_vs_20d>=1.0",
    "deep_highvol_low_downside_conc": "deep_or_high_vol AND industry_downside_concentration_20d<=0.35",
    "recovery_and_liquidity": "breadth_recovery_score>=0.60 AND liquidity_repair_5d>0",
    "panic_plus_recovery": "panic_exhaustion_score>=0.60 AND breadth_recovery_score>=0.60",
}
GATE_TEXT = "same as V4.80: point gate + bootstrap robust gate + leave-one-year gate"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.81 market-state audit for stronger rebound industry selection.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    frame = attach_full_state(opportunity, trades)
    state_audit = state_definition_audit(frame)
    event_panel = build_event_panel(frame)
    results = summarize(event_panel)
    best = results.iloc[0] if len(results) else pd.Series(dtype=object)
    gate = gate_audit(best)
    summary = build_summary(results, best, gate)
    latest = latest_rules(results)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    latest.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, latest, state_audit, gate), encoding="utf-8")
    event_panel.to_csv(DEBUG / "market_state_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "market_state_grid_results.csv", index=False, encoding="utf-8-sig")
    state_audit.to_csv(DEBUG / "state_definition_audit.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def attach_full_state(opportunity: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    merge_cols = [column for column in trades.columns if column not in opportunity.columns or column in {"signal_date", "entry_date", "exit_date"}]
    frame = opportunity.merge(trades[merge_cols], on=["signal_date", "entry_date", "exit_date"], how="left")
    frame["year"] = pd.to_datetime(frame["signal_date"]).dt.year
    return frame


def state_mask(frame: pd.DataFrame, state: str) -> pd.Series:
    deep_or_high = frame["negative_breadth_60d"].ge(0.75) | frame["market_volatility_20d_vs_60d"].ge(1.30)
    if state == "deep_or_high_vol":
        return deep_or_high
    if state == "deep_highvol_liq_repair":
        return deep_or_high & frame["liquidity_repair_5d"].gt(0)
    if state == "deep_highvol_breadth_recovery":
        return deep_or_high & frame["breadth_recovery_score"].ge(0.60)
    if state == "deep_highvol_panic_exhaustion":
        return deep_or_high & frame["panic_exhaustion_score"].ge(0.60)
    if state == "deep_highvol_amount_expansion":
        return deep_or_high & frame["market_amount_5d_vs_20d"].ge(1.0)
    if state == "deep_highvol_low_downside_conc":
        return deep_or_high & frame["industry_downside_concentration_20d"].le(0.35)
    if state == "recovery_and_liquidity":
        return frame["breadth_recovery_score"].ge(0.60) & frame["liquidity_repair_5d"].gt(0)
    if state == "panic_plus_recovery":
        return frame["panic_exhaustion_score"].ge(0.60) & frame["breadth_recovery_score"].ge(0.60)
    return pd.Series(False, index=frame.index)


def state_definition_audit(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for state, definition in STATE_DEFINITIONS.items():
        selected = frame[state_mask(frame, state)]
        events = selected[["signal_date", "entry_date", "exit_date"]].drop_duplicates()
        rows.append({
            "state_gate_variant": state,
            "definition": definition,
            "event_count": int(len(events)),
            "year_count": int(pd.to_datetime(events["signal_date"]).dt.year.nunique()) if len(events) else 0,
            "status": "pass_sample_floor" if len(events) >= 30 and pd.to_datetime(events["signal_date"]).dt.year.nunique() >= 5 else "low_sample",
        })
    return pd.DataFrame(rows)


def build_event_panel(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for state in STATE_DEFINITIONS:
        source = frame[state_mask(frame, state)].copy()
        for feature in FEATURES:
            for top_n in TOP_NS:
                rows.extend(v474.evaluate_factor(source, state, feature, top_n))
    return pd.DataFrame(rows)


def summarize(panel: pd.DataFrame) -> pd.DataFrame:
    if panel.empty:
        return pd.DataFrame()
    rows = []
    for (state, feature, top_n), group in panel.groupby(["state_gate_variant", "factor", "top_n"]):
        point = point_metrics(group, state, feature, int(top_n))
        robust = v480.robustness_metrics(group, int(top_n)) if v480.point_gate_passed(point) else {}
        row = {**point, **robust}
        row["point_gate_passed"] = v480.point_gate_passed(row)
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_v4_81_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_gate_groups"] = failed_gate_groups(row)
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        [
            "passes_v4_81_gate",
            "robust_gate_passed",
            "point_gate_passed",
            "bootstrap_top_quintile_hit_p05",
            "top_quintile_hit_rate",
            "mean_relative_return",
        ],
        ascending=[False, False, False, False, False, False],
    ).reset_index(drop=True)


def point_metrics(group: pd.DataFrame, state: str, feature: str, top_n: int) -> dict[str, object]:
    yearly = group.groupby("year")["relative_return"].mean()
    oos = group[group["year"] >= 2022]
    return {
        "state_gate_variant": state,
        "feature": feature,
        "top_n": top_n,
        "event_count": int(len(group)),
        "year_count": int(group["year"].nunique()),
        "mean_relative_return": float(group["relative_return"].mean()),
        "median_relative_return": float(group["relative_return"].median()),
        "relative_win_rate": float(group["relative_win"].mean()),
        "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
        "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
        "oos_event_count": int(len(oos)),
        "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
        "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
    }


def failed_gate_groups(row: dict[str, object]) -> str:
    failed = []
    if not bool(row.get("point_gate_passed", False)):
        failed.append("point")
    if not bool(row.get("robust_gate_passed", False)):
        failed.append("robust")
    if not bool(row.get("leave_one_year_gate_passed", False)):
        failed.append("leave_one_year")
    return ";".join(failed)


def gate_audit(best: pd.Series) -> pd.DataFrame:
    if best.empty:
        return pd.DataFrame()
    checks = [
        ("point_gate_passed", True, "=="),
        ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="),
        ("event_count", 30, ">="),
        ("year_count", 5, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="),
    ]
    return pd.DataFrame([
        {
            "state_gate_variant": best.get("state_gate_variant", ""),
            "feature": best.get("feature", ""),
            "top_n": best.get("top_n", ""),
            "metric": metric,
            "current": best.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "pass" if compare(best.get(metric, ""), required, op) else "fail",
        }
        for metric, required, op in checks
    ])


def compare(value: object, required: object, op: str) -> bool:
    if op == "==":
        return value == required
    return float(value or 0) >= float(required) if op == ">=" else float(value or 0) > float(required)


def latest_rules(results: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "state_gate_variant",
        "feature",
        "top_n",
        "passes_v4_81_gate",
        "point_gate_passed",
        "robust_gate_passed",
        "leave_one_year_gate_passed",
        "event_count",
        "year_count",
        "mean_relative_return",
        "top_quintile_hit_rate",
        "positive_year_rate",
        "bootstrap_top_quintile_hit_p05",
        "bootstrap_positive_year_p05",
        "leave_one_year_min_hit_rate",
        "leave_one_year_min_mean_relative_return",
        "failed_gate_groups",
    ]
    return results[[column for column in columns if column in results.columns]].head(20).copy()


def build_summary(results: pd.DataFrame, best: pd.Series, gate: pd.DataFrame) -> dict[str, object]:
    passing = results[results["passes_v4_81_gate"].eq(True)] if len(results) else pd.DataFrame()
    point = results[results["point_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    robust = results[results["robust_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    return {
        "version": "4.81.0",
        "policy_id": "industry_rebound_leader_market_state_v4_81",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_rule_count": int(len(results)),
        "point_gate_pass_count": int(len(point)),
        "robust_gate_pass_count": int(len(robust)),
        "passing_rule_count": int(len(passing)),
        "best_state_gate_variant": best.get("state_gate_variant", ""),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_bootstrap_top_quintile_hit_p05": float(best.get("bootstrap_top_quintile_hit_p05", 0.0) or 0.0),
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_robust_market_state_leader_gate" if len(passing) else "research_only_no_robust_market_state_rule",
        "production_ready": False,
        "auto_execution_allowed": False,
        "evaluation_gate": GATE_TEXT,
        "final_verdict": (
            "V4.81 找到通过完整门槛的市场状态强行业规则；仍需前推验证。"
            if len(passing) else
            "V4.81 未找到能让强行业选择通过完整稳健门槛的市场状态过滤。"
        ),
    }


def render_report(summary: dict[str, object], latest: pd.DataFrame, state_audit: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.81 市场状态扩展强行业审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 只使用 V4.70 信号日已经可见的市场状态字段。",
        "- 测试广度修复、流动性修复、恐慌衰竭、成交额扩张、下跌集中度等窗口质量条件。",
        "- 行业排序仍只使用现有价格/估值/企稳/流动性特征，不使用 ETF、个股或未来收益反选。",
        "- 完整门槛沿用 V4.80：点估计、bootstrap 5% 下界和留一年验证都要通过。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 最接近通过的规则",
        "",
        table(latest),
        "",
        "## 状态定义审计",
        "",
        table(state_audit),
        "",
        "## 最优规则门槛审计",
        "",
        table(gate),
        "",
        "## 研究边界",
        "",
        "市场状态过滤可以改善部分点估计，但当前没有解决 bootstrap 下界和样本稀疏问题；不能升级为已找到稳定强反弹行业。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    mini = pd.DataFrame({
        "negative_breadth_60d": [0.8, 0.1],
        "market_volatility_20d_vs_60d": [1.0, 1.4],
        "liquidity_repair_5d": [0.1, -0.1],
        "breadth_recovery_score": [0.7, 0.2],
        "panic_exhaustion_score": [0.7, 0.1],
        "market_amount_5d_vs_20d": [1.1, 0.9],
        "industry_downside_concentration_20d": [0.2, 0.4],
    })
    assert state_mask(mini, "deep_or_high_vol").tolist() == [True, True]
    assert state_mask(mini, "deep_highvol_liq_repair").tolist() == [True, False]
    assert state_mask(mini, "panic_plus_recovery").tolist() == [True, False]
    print("self_check=pass")


if __name__ == "__main__":
    main()
