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
OUT = ROOT / "outputs" / "industry_rebound_leader_feature_separability_v4_77"
DEBUG = OUT / "debug"

FEATURES = [
    "valuation_score",
    "oversold_score",
    "turn_score",
    "liquidity_score",
    "value_oversold_turn_score",
    "oversold_turn_score",
    "oversold_liquidity_score",
    "value_only_score",
    "turn_only_score",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.77 feature separability audit for rebound-leader industries.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    state_opp = v474.attach_state(opportunity, trades)
    event_sep = event_separability(state_opp)
    results = summarize(event_sep)
    gate = gate_audit(results)
    summary = build_summary(results, gate)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    results.head(20).to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, results, gate), encoding="utf-8")
    event_sep.to_csv(DEBUG / "feature_event_separability.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "feature_separability_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_feature={summary['best_feature']}")


def event_separability(frame: pd.DataFrame) -> pd.DataFrame:
    variants = {
        "all_rebound_windows": pd.Series(True, index=frame.index),
        "deep_negative_breadth_only": frame["deep_negative_breadth"],
        "mid_high_stress_only": frame["mid_high_stress"],
        "high_volatility_only": frame["high_volatility_protection"],
        "any_passed_state_bucket": frame["any_passed_state_bucket"],
    }
    rows = []
    for variant, mask in variants.items():
        source = frame[mask].copy()
        for feature in FEATURES:
            if feature not in source.columns:
                continue
            for (signal_date, entry_date, exit_date), event in source.groupby(["signal_date", "entry_date", "exit_date"]):
                event = event.dropna(subset=[feature, "future_return", "future_return_top_quintile"])
                if event.empty or event["future_return_top_quintile"].nunique() < 2:
                    continue
                top = event[event["future_return_top_quintile"].astype(bool)]
                rest = event[~event["future_return_top_quintile"].astype(bool)]
                rank_ic = float(event[[feature, "future_return"]].corr(method="spearman").iloc[0, 1])
                raw_gap = float(top[feature].mean() - rest[feature].mean())
                std = float(event[feature].std()) or 1.0
                rows.append({
                    "state_gate_variant": variant,
                    "feature": feature,
                    "signal_date": signal_date,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "year": int(pd.to_datetime(signal_date).year),
                    "event_industry_count": int(len(event)),
                    "top_quintile_count": int(len(top)),
                    "rank_ic": rank_ic,
                    "rank_ic_positive": rank_ic > 0,
                    "raw_top_vs_rest_gap": raw_gap,
                    "standardized_top_vs_rest_gap": raw_gap / std,
                    "top_feature_mean": float(top[feature].mean()),
                    "rest_feature_mean": float(rest[feature].mean()),
                })
    return pd.DataFrame(rows)


def summarize(event_sep: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if event_sep.empty:
        return pd.DataFrame()
    for (variant, feature), g in event_sep.groupby(["state_gate_variant", "feature"]):
        yearly = g.groupby("year")["rank_ic"].mean()
        oos = g[g["year"] >= 2022]
        row = {
            "state_gate_variant": variant,
            "feature": feature,
            "event_count": int(len(g)),
            "year_count": int(g["year"].nunique()),
            "mean_rank_ic": float(g["rank_ic"].mean()),
            "positive_rank_ic_rate": float(g["rank_ic_positive"].mean()),
            "mean_standardized_gap": float(g["standardized_top_vs_rest_gap"].mean()),
            "positive_gap_rate": float((g["standardized_top_vs_rest_gap"] > 0).mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
            "oos_event_count": int(len(oos)),
            "oos_mean_rank_ic": float(oos["rank_ic"].mean()) if len(oos) else 0.0,
            "oos_positive_gap_rate": float((oos["standardized_top_vs_rest_gap"] > 0).mean()) if len(oos) else 0.0,
        }
        row["passes_feature_separability_gate"] = passes(row)
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values(["passes_feature_separability_gate", "mean_rank_ic", "mean_standardized_gap"], ascending=[False, False, False]).reset_index(drop=True)


def failed_metrics(row: dict[str, object]) -> list[str]:
    checks = [
        ("event_count", 30, ">="),
        ("year_count", 5, ">="),
        ("mean_rank_ic", 0, ">"),
        ("positive_rank_ic_rate", 0.55, ">="),
        ("mean_standardized_gap", 0, ">"),
        ("positive_gap_rate", 0.55, ">="),
        ("positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_rank_ic", 0, ">"),
        ("oos_positive_gap_rate", 0.55, ">="),
    ]
    failed = []
    for metric, required, op in checks:
        value = float(row.get(metric, 0) or 0)
        ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    return failed


def passes(row: dict[str, object]) -> bool:
    return not failed_metrics(row)


def gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0].to_dict()
    failed = set(str(best.get("failed_metrics", "")).split(";"))
    requirements = [
        ("event_count", 30, ">="),
        ("year_count", 5, ">="),
        ("mean_rank_ic", 0, ">"),
        ("positive_rank_ic_rate", 0.55, ">="),
        ("mean_standardized_gap", 0, ">"),
        ("positive_gap_rate", 0.55, ">="),
        ("positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_rank_ic", 0, ">"),
        ("oos_positive_gap_rate", 0.55, ">="),
    ]
    return pd.DataFrame([
        {
            "state_gate_variant": best.get("state_gate_variant", ""),
            "feature": best.get("feature", ""),
            "metric": metric,
            "current": best.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "fail" if metric in failed else "pass",
        }
        for metric, required, op in requirements
    ])


def build_summary(results: pd.DataFrame, gate: pd.DataFrame) -> dict[str, object]:
    best = results.iloc[0].to_dict() if len(results) else {}
    failed = gate[gate["status"].eq("fail")]["metric"].tolist() if len(gate) else ["no_results"]
    passed = not failed
    return {
        "version": "4.77.0",
        "policy_id": "industry_rebound_leader_feature_separability_v4_77",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "best_state_gate_variant": best.get("state_gate_variant", ""),
        "best_feature": best.get("feature", ""),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_rank_ic": float(best.get("mean_rank_ic", 0.0) or 0.0),
        "best_positive_rank_ic_rate": float(best.get("positive_rank_ic_rate", 0.0) or 0.0),
        "best_mean_standardized_gap": float(best.get("mean_standardized_gap", 0.0) or 0.0),
        "best_positive_gap_rate": float(best.get("positive_gap_rate", 0.0) or 0.0),
        "best_oos_mean_rank_ic": float(best.get("oos_mean_rank_ic", 0.0) or 0.0),
        "failed_metrics": ";".join(failed),
        "best_status": "pass_feature_separability_gate" if passed else "research_only_not_validated",
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "现有特征通过强反弹行业分离度审计。" if passed else "现有价格、估值、超跌、流动性特征尚未稳定分离未来强反弹行业。",
    }


def render_report(summary: dict[str, object], results: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.77 强反弹行业特征分离度审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 标签：每个反弹窗口内未来收益前 20% 的行业。",
        "- 特征：估值、超跌、企稳、流动性及已有组合分数。",
        "- 检查：特征是否能稳定把未来 Top20% 行业与其他行业分开。",
        "- 该审计只判断现有特征是否有信号，不生成交易指令。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 分离度排名",
        "",
        table(results.head(20)),
        "",
        "## 最优特征门槛审计",
        "",
        table(gate),
        "",
        "## 研究边界",
        "",
        "若特征分离度不过关，继续在同一批特征里调 TopN 或状态桶很容易过拟合；应优先引入新的 PIT 信息源。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    frame = pd.DataFrame({
        "signal_date": ["2020-01-01"] * 10,
        "entry_date": ["2020-01-02"] * 10,
        "exit_date": ["2020-01-03"] * 10,
        "industry_code": [str(i) for i in range(10)],
        "future_return": list(range(10)),
        "future_return_top_quintile": [False] * 8 + [True, True],
        "valuation_score": list(range(10)),
        "deep_negative_breadth": [True] * 10,
        "mid_high_stress": [False] * 10,
        "high_volatility_protection": [False] * 10,
        "any_passed_state_bucket": [True] * 10,
    })
    sep = event_separability(frame)
    assert not sep.empty
    assert sep.iloc[0]["raw_top_vs_rest_gap"] > 0
    row = {
        "event_count": 30,
        "year_count": 5,
        "mean_rank_ic": 0.1,
        "positive_rank_ic_rate": 0.6,
        "mean_standardized_gap": 0.1,
        "positive_gap_rate": 0.6,
        "positive_year_rate": 0.6,
        "oos_event_count": 8,
        "oos_mean_rank_ic": 0.1,
        "oos_positive_gap_rate": 0.6,
    }
    assert passes(row)
    row["oos_mean_rank_ic"] = -0.1
    assert "oos_mean_rank_ic" in failed_metrics(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
