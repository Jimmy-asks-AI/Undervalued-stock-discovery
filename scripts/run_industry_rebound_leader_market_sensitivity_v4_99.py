#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_leader_robust_grid_v4_80 as v480


ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"
OPPORTUNITY = ROOT / "outputs" / "industry_rebound_leader_expanded_window_v4_97" / "debug" / "industry_event_opportunity_set.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_market_sensitivity_v4_99"
DEBUG = OUT / "debug"

FEATURES = [
    "beta_120_rank",
    "beta_60_rank",
    "corr_120_rank",
    "low_down_capture_120_rank",
    "residual_vol_120_rank",
    "beta_corr_score",
    "beta_low_down_capture_score",
]
TOP_NS = [5, 10, 20]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.99 market-sensitivity feature audit for rebound-leader industries.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(OPPORTUNITY, encoding="utf-8-sig", dtype={"industry_code": str})
    sensitivity = build_market_sensitivity_features()
    frame = attach_features(opportunity, sensitivity)
    sep = feature_separability(frame)
    sep_results = summarize_separability(sep)
    event_panel = evaluate_strategies(frame)
    strategy_results = summarize_strategies(event_panel)
    gate = gate_audit(strategy_results)
    summary = build_summary(strategy_results, sep_results, gate)
    write_outputs(summary, frame, sep, sep_results, event_panel, strategy_results, gate)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_feature={summary['best_feature']}")


def build_market_sensitivity_features() -> pd.DataFrame:
    hist = pd.read_csv(HISTORY, encoding="utf-8-sig", dtype={"industry_code": str})
    hist["industry_code"] = hist["industry_code"].str.zfill(6)
    hist["trade_date"] = pd.to_datetime(hist["trade_date"])
    hist["ret"] = pd.to_numeric(hist["return_pct"], errors="coerce") / 100.0
    market = hist.groupby("trade_date")["ret"].mean().rename("market_ret")
    hist = hist.merge(market, on="trade_date", how="left").sort_values(["industry_code", "trade_date"])
    pieces = []
    for _, group in hist.groupby("industry_code", sort=False):
        group = group.copy()
        for window in [60, 120]:
            cov = group["ret"].rolling(window, min_periods=40).cov(group["market_ret"])
            var = group["market_ret"].rolling(window, min_periods=40).var()
            group[f"beta_{window}"] = cov / var
        group["corr_120"] = group["ret"].rolling(120, min_periods=40).corr(group["market_ret"])
        down_ind = group["ret"].where(group["market_ret"] < 0)
        down_mkt = group["market_ret"].where(group["market_ret"] < 0)
        group["down_capture_120"] = down_ind.rolling(120, min_periods=20).mean() / down_mkt.rolling(120, min_periods=20).mean()
        group["residual_vol_120"] = (group["ret"] - group["beta_120"] * group["market_ret"]).rolling(120, min_periods=40).std()
        pieces.append(group)
    out = pd.concat(pieces, ignore_index=True)
    cols = ["trade_date", "industry_code", "beta_60", "beta_120", "corr_120", "down_capture_120", "residual_vol_120"]
    return out[cols]


def attach_features(opportunity: pd.DataFrame, sensitivity: pd.DataFrame) -> pd.DataFrame:
    frame = opportunity.copy()
    frame["industry_code"] = frame["industry_code"].astype(str).str.zfill(6)
    frame["trade_date"] = pd.to_datetime(frame["signal_date"])
    merged = frame.merge(sensitivity, on=["trade_date", "industry_code"], how="left")
    pieces = []
    for _, event in merged.groupby(["signal_date", "entry_date", "exit_date"], sort=False):
        event = event.copy()
        event["beta_120_rank"] = event["beta_120"].rank(pct=True)
        event["beta_60_rank"] = event["beta_60"].rank(pct=True)
        event["corr_120_rank"] = event["corr_120"].rank(pct=True)
        event["low_down_capture_120_rank"] = event["down_capture_120"].rank(pct=True, ascending=False)
        event["residual_vol_120_rank"] = event["residual_vol_120"].rank(pct=True)
        event["beta_corr_score"] = 0.60 * event["beta_120_rank"] + 0.40 * event["corr_120_rank"]
        event["beta_low_down_capture_score"] = 0.60 * event["beta_120_rank"] + 0.40 * event["low_down_capture_120_rank"]
        pieces.append(event)
    return pd.concat(pieces, ignore_index=True)


def feature_separability(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in FEATURES:
        for (signal_date, entry_date, exit_date), event in frame.groupby(["signal_date", "entry_date", "exit_date"]):
            event = event.dropna(subset=[feature, "future_return", "future_return_top_quintile"])
            if event.empty or event["future_return_top_quintile"].nunique() < 2:
                continue
            top = event[event["future_return_top_quintile"].astype(bool)]
            rest = event[~event["future_return_top_quintile"].astype(bool)]
            gap = float(top[feature].mean() - rest[feature].mean())
            std = float(event[feature].std()) or 1.0
            rows.append({
                "feature": feature,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "year": int(pd.to_datetime(signal_date).year),
                "rank_ic": float(event[[feature, "future_return"]].corr(method="spearman").iloc[0, 1]),
                "rank_ic_positive": float(event[[feature, "future_return"]].corr(method="spearman").iloc[0, 1]) > 0,
                "standardized_gap": gap / std,
                "gap_positive": gap > 0,
            })
    return pd.DataFrame(rows)


def summarize_separability(sep: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature, group in sep.groupby("feature"):
        oos = group[group["year"].ge(2022)]
        yearly = group.groupby("year")["rank_ic"].mean()
        rows.append({
            "feature": feature,
            "event_count": int(len(group)),
            "year_count": int(group["year"].nunique()),
            "mean_rank_ic": float(group["rank_ic"].mean()),
            "positive_rank_ic_rate": float(group["rank_ic_positive"].mean()),
            "mean_standardized_gap": float(group["standardized_gap"].mean()),
            "positive_gap_rate": float(group["gap_positive"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "oos_event_count": int(len(oos)),
            "oos_mean_rank_ic": float(oos["rank_ic"].mean()) if len(oos) else 0.0,
        })
    return pd.DataFrame(rows).sort_values(["mean_rank_ic", "mean_standardized_gap"], ascending=[False, False]).reset_index(drop=True)


def evaluate_strategies(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in FEATURES:
        for (signal_date, entry_date, exit_date), event in frame.groupby(["signal_date", "entry_date", "exit_date"]):
            event = event.dropna(subset=[feature, "future_return"])
            if event.empty:
                continue
            benchmark = float(event["future_return"].mean())
            top_cut = event["future_return"].quantile(0.8)
            ranked = event.sort_values(feature, ascending=False)
            for top_n in TOP_NS:
                selected = ranked.head(top_n)
                rel = float(selected["future_return"].mean()) - 0.001 - benchmark
                rows.append({
                    "feature": feature,
                    "top_n": top_n,
                    "signal_date": signal_date,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "year": int(pd.to_datetime(signal_date).year),
                    "relative_return": rel,
                    "relative_win": rel > 0,
                    "rank_ic": float(event[[feature, "future_return"]].corr(method="spearman").iloc[0, 1]),
                    "rank_ic_positive": float(event[[feature, "future_return"]].corr(method="spearman").iloc[0, 1]) > 0,
                    "top_quintile_hit_rate": float((selected["future_return"] >= top_cut).mean()),
                })
    return pd.DataFrame(rows)


def summarize_strategies(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (feature, top_n), group in panel.groupby(["feature", "top_n"]):
        oos = group[group["year"].ge(2022)]
        yearly = group.groupby("year")["relative_return"].mean()
        row = {
            "feature": feature,
            "top_n": int(top_n),
            "event_count": int(len(group)),
            "year_count": int(group["year"].nunique()),
            "mean_relative_return": float(group["relative_return"].mean()),
            "median_relative_return": float(group["relative_return"].median()),
            "relative_win_rate": float(group["relative_win"].mean()),
            "mean_rank_ic": float(group["rank_ic"].mean()),
            "positive_rank_ic_rate": float(group["rank_ic_positive"].mean()),
            "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
        }
        row["point_gate_passed"] = passes_point_gate(row)
        robust = v480.robustness_metrics(group, int(top_n)) if row["point_gate_passed"] else {}
        row.update(robust)
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_metrics"] = ";".join(strategy_failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        ["passes_gate", "robust_gate_passed", "point_gate_passed", "mean_relative_return"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def strategy_failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("point_gate_passed", True, "=="),
        ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="),
        ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="),
        ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0, ">"),
    ]
    failed = []
    for metric, required, op in checks:
        if op == "==":
            ok = row.get(metric) == required
        else:
            value = float(row.get(metric, 0) or 0)
            ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    failed.extend(point_failed_metrics(row))
    return failed


def point_failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("event_count", 30, ">="),
        ("year_count", 8, ">="),
        ("mean_relative_return", 0, ">"),
        ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="),
        ("mean_rank_ic", 0, ">"),
        ("positive_rank_ic_rate", 0.55, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="),
    ]
    failed = []
    for metric, required, op in checks:
        value = float(row.get(metric, 0) or 0)
        ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    return failed


def passes_point_gate(row: dict[str, Any]) -> bool:
    return not point_failed_metrics(row)


def passes_strategy(row: dict[str, Any]) -> bool:
    return not strategy_failed_metrics(row)


def gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0].to_dict()
    failed = set(str(best.get("failed_metrics", "")).split(";"))
    requirements = [
        ("point_gate_passed", True, "=="),
        ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="),
        ("event_count", 30, ">="),
        ("year_count", 8, ">="),
        ("mean_relative_return", 0, ">"),
        ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="),
        ("mean_rank_ic", 0, ">"),
        ("positive_rank_ic_rate", 0.55, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="),
        ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="),
        ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0, ">"),
    ]
    return pd.DataFrame([
        {
            "feature": best.get("feature", ""),
            "top_n": best.get("top_n", ""),
            "metric": metric,
            "current": best.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "fail" if metric in failed else "pass",
        }
        for metric, required, op in requirements
    ])


def build_summary(strategy: pd.DataFrame, sep: pd.DataFrame, gate: pd.DataFrame) -> dict[str, Any]:
    best = strategy.iloc[0].to_dict() if len(strategy) else {}
    best_sep = sep.iloc[0].to_dict() if len(sep) else {}
    passed = bool(best.get("passes_gate", False))
    return {
        "version": "4.99.0",
        "policy_id": "industry_rebound_leader_market_sensitivity_v4_99",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_variant": "vol_repair",
        "tested_feature_count": len(FEATURES),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_oos_mean_relative_return": float(best.get("oos_mean_relative_return", 0.0) or 0.0),
        "best_bootstrap_top_quintile_hit_p05": num(best.get("bootstrap_top_quintile_hit_p05")),
        "best_bootstrap_positive_year_p05": num(best.get("bootstrap_positive_year_p05")),
        "best_leave_one_year_min_hit_rate": num(best.get("leave_one_year_min_hit_rate")),
        "best_separability_feature": best_sep.get("feature", ""),
        "best_separability_rank_ic": float(best_sep.get("mean_rank_ic", 0.0) or 0.0),
        "passing_rule_count": int(strategy["passes_gate"].sum()) if len(strategy) else 0,
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_market_sensitivity_leader_gate" if passed else "research_only_no_market_sensitivity_alpha",
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": (
            "V4.99 的市场敏感度特征通过完整强行业门槛；仍需前推观察。"
            if passed else
            "V4.99 测试 beta、相关性、下跌捕获和残差波动等市场敏感度特征，点估计有改善，但未通过完整稳健门槛。"
        ),
    }


def write_outputs(summary: dict[str, Any], frame: pd.DataFrame, sep: pd.DataFrame, sep_results: pd.DataFrame, event_panel: pd.DataFrame, strategy: pd.DataFrame, gate: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    strategy.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, strategy, sep_results, gate), encoding="utf-8")
    frame.to_csv(DEBUG / "market_sensitivity_opportunity_set.csv", index=False, encoding="utf-8-sig")
    sep.to_csv(DEBUG / "feature_event_separability.csv", index=False, encoding="utf-8-sig")
    sep_results.to_csv(DEBUG / "feature_separability_results.csv", index=False, encoding="utf-8-sig")
    event_panel.to_csv(DEBUG / "strategy_event_panel.csv", index=False, encoding="utf-8-sig")
    strategy.to_csv(DEBUG / "strategy_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], strategy: pd.DataFrame, sep: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.99 市场敏感度强行业回测",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 窗口定义：`{summary['window_variant']}`",
        f"- 测试特征数：{summary['tested_feature_count']}",
        f"- 最优组合规则：`{summary['best_feature']}` Top{summary['best_top_n']}",
        f"- 最优平均相对收益：{pct(summary['best_mean_relative_return'])}",
        f"- 最优 Top20% 命中率：{pct(summary['best_top_quintile_hit_rate'])}",
        f"- 最优样本外相对收益：{pct(summary['best_oos_mean_relative_return'])}",
        f"- bootstrap Top20% 命中率 5% 下界：{pct(summary['best_bootstrap_top_quintile_hit_p05'])}",
        f"- 留一年最小 Top20% 命中率：{pct(summary['best_leave_one_year_min_hit_rate'])}",
        f"- 最优分离度特征：`{summary['best_separability_feature']}`，RankIC={summary['best_separability_rank_ic']:.4f}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 最优规则门槛",
        "",
        gate.to_markdown(index=False) if len(gate) else "无数据",
        "",
        "## 组合规则结果",
        "",
        strategy.to_markdown(index=False) if len(strategy) else "无数据",
        "",
        "## 分离度结果",
        "",
        sep.to_markdown(index=False) if len(sep) else "无数据",
        "",
        "## 研究边界",
        "",
        "V4.99 使用信号日前 60/120 日行业指数收益计算市场敏感度特征；不使用未来收益构造特征，不生成交易指令。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def num(value: Any) -> float:
    return float(value) if pd.notna(value) else 0.0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    sample = pd.DataFrame({
        "feature": ["x"],
        "event_count": [30],
        "year_count": [8],
        "mean_relative_return": [0.01],
        "median_relative_return": [0.01],
        "relative_win_rate": [0.6],
        "mean_rank_ic": [0.1],
        "positive_rank_ic_rate": [0.6],
        "top_quintile_hit_rate": [0.31],
        "oos_event_count": [8],
        "oos_mean_relative_return": [0.01],
        "oos_relative_win_rate": [0.5],
        "point_gate_passed": [True],
        "robust_gate_passed": [True],
        "leave_one_year_gate_passed": [True],
        "bootstrap_top_quintile_hit_p05": [0.31],
        "bootstrap_positive_year_p05": [0.6],
        "leave_one_year_min_hit_rate": [0.25],
        "leave_one_year_min_mean_relative_return": [0.01],
    }).iloc[0].to_dict()
    assert passes_strategy(sample)
    sample["top_quintile_hit_rate"] = 0.2
    assert not passes_strategy(sample)
    print("self_check=pass")


if __name__ == "__main__":
    main()
