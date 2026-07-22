#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_leader_robust_grid_v4_80 as v480
try:
    from valuation_pit_contract import ValuationPITContractError, attach_pit_valuation_asof, prepare_pit_valuation_history
except ModuleNotFoundError:  # package-style imports in tests and audits
    from scripts.valuation_pit_contract import ValuationPITContractError, attach_pit_valuation_asof, prepare_pit_valuation_history


ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"
OPPORTUNITY = ROOT / "outputs" / "industry_rebound_leader_market_sensitivity_v4_99" / "debug" / "market_sensitivity_opportunity_set.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_pit_valuation_percentile_audit_v5_12"
DEBUG = OUT / "debug"
FEATURES = ["pb_3y_cheap_rank", "pe_3y_cheap_rank", "dividend_3y_high_rank", "beta_pb_percentile_score"]
TOP_N = 5


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.12 PIT valuation percentile audit.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    try:
        panel = attach_percentile_features()
    except ValuationPITContractError as exc:
        write_blocked_outputs(str(exc))
        print(f"output_dir={OUT}")
        print("best_status=blocked_non_pit_valuation_history")
        print("passing_rule_count=0")
        return
    events = evaluate_features(panel)
    results = summarize(events)
    summary = build_summary(results)
    write_outputs(summary, panel, events, results)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def attach_percentile_features() -> pd.DataFrame:
    opp = pd.read_csv(OPPORTUNITY, encoding="utf-8-sig", dtype={"industry_code": str})
    pct = build_percentiles()
    opp["industry_code"] = opp["industry_code"].astype(str).str.zfill(6)
    merged = attach_pit_valuation_asof(
        opp,
        pct,
        decision_date_column="signal_date",
    )
    pieces = []
    for _, event in merged.groupby(["signal_date", "entry_date", "exit_date"], sort=False):
        event = event.copy()
        event["pb_3y_cheap_rank"] = event["pb_3y_cheap_pct"].rank(pct=True)
        event["pe_3y_cheap_rank"] = event["pe_3y_cheap_pct"].rank(pct=True)
        event["dividend_3y_high_rank"] = event["dividend_3y_high_pct"].rank(pct=True)
        event["beta_pb_percentile_score"] = 0.70 * event["beta_120_rank"] + 0.30 * event["pb_3y_cheap_rank"]
        pieces.append(event)
    return pd.concat(pieces, ignore_index=True)


def build_percentiles() -> pd.DataFrame:
    hist = pd.read_csv(HISTORY, encoding="utf-8-sig", dtype={"industry_code": str})
    hist = prepare_pit_valuation_history(hist, source=str(HISTORY))
    hist = hist.sort_values(["industry_code", "valuation_available_date", "valuation_trade_date"])
    pieces = []
    for _, group in hist.groupby("industry_code", sort=False):
        group = group.copy()
        group["pb_pct"] = rolling_percentile(group["pb"])
        group["pe_pct"] = rolling_percentile(group["pe"])
        group["dividend_pct"] = rolling_percentile(group["dividend_yield"])
        group["pb_3y_cheap_pct"] = 1.0 - group["pb_pct"]
        group["pe_3y_cheap_pct"] = 1.0 - group["pe_pct"]
        group["dividend_3y_high_pct"] = group["dividend_pct"]
        pieces.append(group)
    return pd.concat(pieces, ignore_index=True)


def rolling_percentile(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    return values.rolling(756, min_periods=252).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)


def evaluate_features(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in FEATURES:
        for (signal_date, entry_date, exit_date), event in panel.groupby(["signal_date", "entry_date", "exit_date"]):
            event = event.dropna(subset=[feature, "future_return"])
            if event.empty:
                continue
            benchmark = float(event["future_return"].mean())
            top_cut = event["future_return"].quantile(0.8)
            selected = event.sort_values(feature, ascending=False).head(TOP_N)
            relative = float(selected["future_return"].mean()) - 0.001 - benchmark
            rows.append({
                "feature": feature,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "year": int(pd.to_datetime(signal_date).year),
                "relative_return": relative,
                "relative_win": relative > 0,
                "top_quintile_hit_rate": float((selected["future_return"] >= top_cut).mean()),
            })
    return pd.DataFrame(rows)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature, group in events.groupby("feature"):
        oos = group[group["year"].ge(2022)]
        yearly = group.groupby("year")["relative_return"].mean()
        row = {
            "feature": feature,
            "top_n": TOP_N,
            "event_count": int(len(group)),
            "year_count": int(group["year"].nunique()),
            "mean_relative_return": float(group["relative_return"].mean()),
            "median_relative_return": float(group["relative_return"].median()),
            "relative_win_rate": float(group["relative_win"].mean()),
            "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
        }
        row["point_gate_passed"] = passes_point_gate(row)
        robust = v480.robustness_metrics(group, TOP_N) if row["point_gate_passed"] else {}
        row.update(robust)
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["passes_gate", "mean_relative_return"], ascending=[False, False])


def failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("event_count", 30, ">="), ("year_count", 8, ">="),
        ("mean_relative_return", 0, ">"), ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="), ("top_quintile_hit_rate", 0.30, ">="),
        ("oos_event_count", 8, ">="), ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="), ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="), ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="), ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0, ">"),
    ]
    out = []
    for metric, required, op in checks:
        if op == "==":
            ok = row.get(metric) == required
        else:
            value = float(row.get(metric, 0) or 0)
            ok = value >= required if op == ">=" else value > required
        if not ok:
            out.append(metric)
    return out


def passes_point_gate(row: dict[str, Any]) -> bool:
    point = {
        "event_count", "year_count", "mean_relative_return", "median_relative_return",
        "relative_win_rate", "top_quintile_hit_rate", "oos_event_count",
        "oos_mean_relative_return", "oos_relative_win_rate",
    }
    return not (point & set(failed_metrics(row)))


def build_summary(results: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_gate", False))
    return {
        "version": "5.12.0",
        "policy_id": "rebound_leader_pit_valuation_percentile_audit_v5_12",
        "policy_status": "research_only",
        "valuation_data_status": "pit_verified",
        "pit_eligible": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_feature_count": len(FEATURES),
        "best_feature": best.get("feature", ""),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "passing_rule_count": int(results["passes_gate"].sum()) if len(results) else 0,
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "pass_pit_valuation_percentile_gate" if passed else "research_only_no_pit_valuation_percentile_alpha",
        "final_verdict": "V5.12 PIT 估值历史分位特征未通过完整强行业门槛，不能声称目标完成。" if not passed else "V5.12 PIT 估值历史分位特征通过强行业门槛，但仍需前推验证。",
    }


def write_outputs(summary: dict[str, Any], panel: pd.DataFrame, events: pd.DataFrame, results: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, results), encoding="utf-8")
    panel.to_csv(DEBUG / "pit_valuation_percentile_opportunity_set.csv", index=False, encoding="utf-8-sig")
    events.to_csv(DEBUG / "pit_valuation_percentile_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "pit_valuation_percentile_results.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.12 PIT 估值历史分位审计",
        "",
        summary["final_verdict"],
        "",
        f"- 测试特征数：{summary['tested_feature_count']}",
        f"- 最优特征：`{summary['best_feature']}`",
        f"- 最优事件数：{summary['best_event_count']}",
        f"- 最优平均相对收益：{pct(summary['best_mean_relative_return'])}",
        f"- 最优 Top20% 命中率：{pct(summary['best_top_quintile_hit_rate'])}",
        f"- 通过规则数：{summary['passing_rule_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 结果",
        "",
        results.to_markdown(index=False) if len(results) else "无数据",
        "",
        "边界：V5.12 的滚动分位按已验证 available_date 排序并向后 as-of 关联；缺少真实发布时间、抓取时间、版本/哈希或修订证据时整轮失败关闭。",
    ])


def write_blocked_outputs(reason: str) -> None:
    summary = {
        "version": "5.12.0",
        "policy_id": "rebound_leader_pit_valuation_percentile_audit_v5_12",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "valuation_data_status": "blocked_non_pit_valuation_history",
        "pit_eligible": False,
        "block_reason": reason,
        "tested_feature_count": 0,
        "best_feature": "",
        "best_event_count": 0,
        "best_mean_relative_return": 0.0,
        "best_top_quintile_hit_rate": 0.0,
        "passing_rule_count": 0,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "blocked_non_pit_valuation_history",
        "final_verdict": "V5.12 输入缺少可验证的真实可用时间或版本链，未运行估值分位回测。",
    }
    write_outputs(summary, pd.DataFrame(), pd.DataFrame(), pd.DataFrame())


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    sample = pd.Series(range(300), dtype=float)
    value = rolling_percentile(sample).dropna().iloc[-1]
    assert 0.99 <= value <= 1.0
    row = {
        "event_count": 30, "year_count": 8, "mean_relative_return": 0.01,
        "median_relative_return": 0.01, "relative_win_rate": 0.56,
        "top_quintile_hit_rate": 0.31, "oos_event_count": 8,
        "oos_mean_relative_return": 0.01, "oos_relative_win_rate": 0.50,
        "robust_gate_passed": True, "leave_one_year_gate_passed": True,
        "bootstrap_top_quintile_hit_p05": 0.31, "bootstrap_positive_year_p05": 0.60,
        "leave_one_year_min_hit_rate": 0.25, "leave_one_year_min_mean_relative_return": 0.01,
    }
    assert passes_point_gate(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
