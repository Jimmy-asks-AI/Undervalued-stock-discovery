#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_market_state_v4_81 as v481
import run_industry_rebound_leader_robust_grid_v4_80 as v480


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_trap_guardrail_v4_83"
DEBUG = OUT / "debug"

STATE_VARIANTS = ["deep_or_high_vol", "deep_highvol_liq_repair"]
FEATURES = ["oversold_liquidity_score", "oversold_score"]
TOP_NS = [5, 10, 15, 20]
GUARDRAILS = {
    "no_extra_guardrail": "不加行业级尾部过滤，作为基准。",
    "turn_floor_25": "剔除企稳分位低于 25% 的行业。",
    "liquidity_floor_25": "剔除流动性分位低于 25% 的行业。",
    "valuation_floor_20": "剔除估值支持分位低于 20% 的行业。",
    "turn_liquidity_floor_25": "同时要求企稳和流动性分位不低于 25%。",
    "poor_repair_trap_filter": "剔除超跌分位高于 75% 但企稳分位低于 25% 的行业。",
    "low_liquidity_trap_filter": "剔除超跌分位高于 75% 但流动性分位低于 25% 的行业。",
    "balanced_floor_20": "估值、企稳、流动性分位都不低于 20%。",
}
GATE_TEXT = "same as V4.80: point gate + bootstrap robust gate + leave-one-year gate"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.83 trap guardrail audit for rebound-leader industries.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    frame = add_rank_features(v481.attach_full_state(opportunity, trades))
    definitions = guardrail_definition_audit(frame)
    event_panel = build_event_panel(frame)
    results = summarize(event_panel)
    best = results.iloc[0] if len(results) else pd.Series(dtype=object)
    gate = gate_audit(best)
    top_rules = top_rule_table(results)
    summary = build_summary(results, best, gate)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    top_rules.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, top_rules, definitions, gate), encoding="utf-8")
    event_panel.to_csv(DEBUG / "guardrail_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "guardrail_grid_results.csv", index=False, encoding="utf-8-sig")
    definitions.to_csv(DEBUG / "guardrail_definition_audit.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def add_rank_features(frame: pd.DataFrame) -> pd.DataFrame:
    rank_columns = ["valuation_score", "oversold_score", "turn_score", "liquidity_score"]
    pieces = []
    for _, event in frame.groupby(["signal_date", "entry_date", "exit_date"], sort=False):
        event = event.copy()
        for column in rank_columns:
            event[f"{column}_rank"] = pd.to_numeric(event[column], errors="coerce").rank(pct=True, ascending=True)
        pieces.append(event)
    return pd.concat(pieces, ignore_index=True)


def build_event_panel(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for state in STATE_VARIANTS:
        state_frame = frame[v481.state_mask(frame, state)].copy()
        for guardrail in GUARDRAILS:
            guarded = apply_guardrail(state_frame, guardrail)
            for feature in FEATURES:
                for top_n in TOP_NS:
                    rows.extend(evaluate_rule(guarded, state, guardrail, feature, top_n))
    return pd.DataFrame(rows)


def apply_guardrail(frame: pd.DataFrame, guardrail: str) -> pd.DataFrame:
    if guardrail == "no_extra_guardrail":
        return frame.copy()
    turn = frame["turn_score_rank"]
    liquidity = frame["liquidity_score_rank"]
    valuation = frame["valuation_score_rank"]
    oversold = frame["oversold_score_rank"]
    if guardrail == "turn_floor_25":
        mask = turn.ge(0.25)
    elif guardrail == "liquidity_floor_25":
        mask = liquidity.ge(0.25)
    elif guardrail == "valuation_floor_20":
        mask = valuation.ge(0.20)
    elif guardrail == "turn_liquidity_floor_25":
        mask = turn.ge(0.25) & liquidity.ge(0.25)
    elif guardrail == "poor_repair_trap_filter":
        mask = ~(oversold.ge(0.75) & turn.lt(0.25))
    elif guardrail == "low_liquidity_trap_filter":
        mask = ~(oversold.ge(0.75) & liquidity.lt(0.25))
    elif guardrail == "balanced_floor_20":
        mask = valuation.ge(0.20) & turn.ge(0.20) & liquidity.ge(0.20)
    else:
        raise ValueError(f"unknown guardrail: {guardrail}")
    return frame[mask].copy()


def evaluate_rule(frame: pd.DataFrame, state: str, guardrail: str, feature: str, top_n: int) -> list[dict[str, object]]:
    rows = []
    for (signal_date, entry_date, exit_date), event in frame.groupby(["signal_date", "entry_date", "exit_date"]):
        event = event.dropna(subset=[feature, "future_return"])
        if len(event) < top_n:
            continue
        ranked = event.sort_values(feature, ascending=False)
        selected = ranked.head(top_n)
        benchmark = float(event["future_return"].mean())
        relative = float(selected["future_return"].mean()) - benchmark - 0.001
        top_cut = event["future_return"].quantile(0.8)
        rank_ic = float(event[[feature, "future_return"]].corr(method="spearman").iloc[0, 1])
        rows.append({
            "state_gate_variant": state,
            "guardrail": guardrail,
            "factor": feature,
            "top_n": top_n,
            "signal_date": signal_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "year": int(pd.to_datetime(signal_date).year),
            "candidate_pool_size": int(len(event)),
            "selected_return": float(selected["future_return"].mean()),
            "benchmark_return": benchmark,
            "relative_return": relative,
            "relative_win": relative > 0,
            "rank_ic": rank_ic,
            "rank_ic_positive": rank_ic > 0,
            "top_quintile_hit_rate": float((selected["future_return"] >= top_cut).mean()),
            "selected_industry_codes": "|".join(selected["industry_code"].astype(str).str.zfill(6)),
            "selected_industries": "|".join(selected["industry_name"].astype(str)),
        })
    return rows


def summarize(panel: pd.DataFrame) -> pd.DataFrame:
    if panel.empty:
        return pd.DataFrame()
    rows = []
    for (state, guardrail, feature, top_n), group in panel.groupby(["state_gate_variant", "guardrail", "factor", "top_n"]):
        row = point_metrics(group, state, guardrail, feature, int(top_n))
        robust = v480.robustness_metrics(group, int(top_n)) if v480.point_gate_passed(row) else {}
        row.update(robust)
        row["point_gate_passed"] = v480.point_gate_passed(row)
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_v4_83_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_gate_groups"] = failed_gate_groups(row)
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        [
            "passes_v4_83_gate",
            "robust_gate_passed",
            "point_gate_passed",
            "bootstrap_top_quintile_hit_p05",
            "top_quintile_hit_rate",
            "mean_relative_return",
        ],
        ascending=[False, False, False, False, False, False],
    ).reset_index(drop=True)


def point_metrics(group: pd.DataFrame, state: str, guardrail: str, feature: str, top_n: int) -> dict[str, object]:
    yearly = group.groupby("year")["relative_return"].mean()
    oos = group[group["year"] >= 2022]
    return {
        "state_gate_variant": state,
        "guardrail": guardrail,
        "feature": feature,
        "top_n": top_n,
        "event_count": int(len(group)),
        "year_count": int(group["year"].nunique()),
        "mean_candidate_pool_size": float(group["candidate_pool_size"].mean()),
        "mean_relative_return": float(group["relative_return"].mean()),
        "median_relative_return": float(group["relative_return"].median()),
        "relative_win_rate": float(group["relative_win"].mean()),
        "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
        "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
        "oos_event_count": int(len(oos)),
        "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
        "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
    }


def guardrail_definition_audit(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    base_events = frame[["signal_date", "entry_date", "exit_date"]].drop_duplicates()
    for state in STATE_VARIANTS:
        state_frame = frame[v481.state_mask(frame, state)].copy()
        state_events = state_frame[["signal_date", "entry_date", "exit_date"]].drop_duplicates()
        for guardrail, definition in GUARDRAILS.items():
            guarded = apply_guardrail(state_frame, guardrail)
            guarded_events = guarded[["signal_date", "entry_date", "exit_date"]].drop_duplicates()
            rows.append({
                "state_gate_variant": state,
                "guardrail": guardrail,
                "definition": definition,
                "base_event_count": int(len(base_events)),
                "state_event_count": int(len(state_events)),
                "guarded_event_count": int(len(guarded_events)),
                "mean_guarded_pool_size": float(guarded.groupby(["signal_date", "entry_date", "exit_date"]).size().mean()) if len(guarded) else 0.0,
                "status": "pass_sample_floor" if len(guarded_events) >= 30 else "low_sample",
            })
    return pd.DataFrame(rows)


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
        ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0.0, ">"),
    ]
    return pd.DataFrame([
        {
            "state_gate_variant": best.get("state_gate_variant", ""),
            "guardrail": best.get("guardrail", ""),
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


def top_rule_table(results: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "state_gate_variant",
        "guardrail",
        "feature",
        "top_n",
        "passes_v4_83_gate",
        "point_gate_passed",
        "robust_gate_passed",
        "leave_one_year_gate_passed",
        "event_count",
        "year_count",
        "mean_candidate_pool_size",
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
    passing = results[results["passes_v4_83_gate"].eq(True)] if len(results) else pd.DataFrame()
    point = results[results["point_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    robust = results[results["robust_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    return {
        "version": "4.83.0",
        "policy_id": "industry_rebound_leader_trap_guardrail_v4_83",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_rule_count": int(len(results)),
        "point_gate_pass_count": int(len(point)),
        "robust_gate_pass_count": int(len(robust)),
        "passing_rule_count": int(len(passing)),
        "best_state_gate_variant": best.get("state_gate_variant", ""),
        "best_guardrail": best.get("guardrail", ""),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_bootstrap_top_quintile_hit_p05": float(best.get("bootstrap_top_quintile_hit_p05", 0.0) or 0.0),
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_robust_trap_guardrail_leader_gate" if len(passing) else "research_only_no_robust_trap_guardrail_rule",
        "production_ready": False,
        "auto_execution_allowed": False,
        "evaluation_gate": GATE_TEXT,
        "final_verdict": (
            "V4.83 找到通过完整稳健门槛的失败尾部护栏强行业规则；仍需实盘前推。"
            if len(passing) else
            "V4.83 未找到通过完整稳健门槛的失败尾部护栏规则；现有事前护栏仍不能证明能稳定选出强反弹行业。"
        ),
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


def compare(value: object, required: object, op: str) -> bool:
    if op == "==":
        return value == required
    current = float(value or 0)
    target = float(required)
    return current >= target if op == ">=" else current > target


def render_report(summary: dict[str, object], top_rules: pd.DataFrame, definitions: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.83 失败尾部护栏强行业审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 只测试 V4.80/V4.81 最接近成功的两个状态门控：`deep_or_high_vol` 和 `deep_highvol_liq_repair`。",
        "- 只使用反弹窗口信号日已可见的估值、超跌、企稳、流动性分位。",
        "- 护栏目标是剔除“超跌但未企稳”“超跌但流动性差”“估值支持不足”等事前可见尾部，不使用未来收益反选。",
        "- 评价门槛沿用 V4.80：点估计、bootstrap 5% 下界、留一年验证全部通过才算找到强反弹行业规则。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 最接近通过的规则",
        "",
        table(top_rules),
        "",
        "## 护栏定义审计",
        "",
        table(definitions),
        "",
        "## 最优规则门槛审计",
        "",
        table(gate),
        "",
        "## 研究边界",
        "",
        "如果 V4.83 仍未通过，说明失败不是靠少量简单事前尾部护栏就能解决；下一步应优先等待新 PIT 信息源和真实前推样本，而不是继续扩大参数网格。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    mini = pd.DataFrame({
        "signal_date": ["2020-01-01"] * 5,
        "entry_date": ["2020-01-02"] * 5,
        "exit_date": ["2020-01-03"] * 5,
        "valuation_score": [0.1, 0.2, 0.3, 0.4, 0.5],
        "oversold_score": [0.9, 0.8, 0.2, 0.1, 0.0],
        "turn_score": [0.1, 0.5, 0.7, 0.9, 1.0],
        "liquidity_score": [0.1, 0.5, 0.7, 0.9, 1.0],
    })
    ranked = add_rank_features(mini)
    assert len(apply_guardrail(ranked, "poor_repair_trap_filter")) == 4
    assert len(apply_guardrail(ranked, "turn_liquidity_floor_25")) == 4
    assert compare(0.3, 0.3, ">=")
    assert not compare(0.29, 0.3, ">=")
    print("self_check=pass")


if __name__ == "__main__":
    main()
