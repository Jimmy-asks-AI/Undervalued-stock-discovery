#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs" / "rebound_window_effectiveness_evaluation.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate rebound-window version effectiveness with a shared scorecard.")
    parser.add_argument("--output-dir", required=True, help="Research run output directory.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Evaluation framework JSON.")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    config = read_json(Path(args.config))
    result = evaluate_run(output_dir, config)
    debug_dir = output_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(result["scorecard"]).to_csv(debug_dir / "evaluation_scorecard.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "evaluation_summary.json", result["summary"])
    print(f"output_dir={output_dir.resolve()}")
    print(f"framework={config['framework_id']} {config['version']}")
    print(f"raw_score={result['summary']['raw_score']:.1f}")
    print(f"score={result['summary']['score']:.1f}")
    print(f"status={result['summary']['evaluation_status']}")
    print(f"effective={result['summary']['is_effective']}")


def evaluate_run(output_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    run_summary = read_json(output_dir / "run_summary.json")
    top = read_csv(output_dir / "top_candidates.csv")
    debug_dir = output_dir / "debug"
    leakage = read_csv(debug_dir / "leakage_audit.csv")
    data_audit = read_csv(debug_dir / "data_availability_audit.csv")
    realtime_summary = read_csv(debug_dir / "realtime_simulation_summary.csv")
    realtime_trades = read_csv(debug_dir / "realtime_simulation_trades.csv")
    wf_year = read_csv(debug_dir / "walk_forward_year_summary.csv")

    realtime = select_realtime_row(top, realtime_summary)
    metrics = build_metrics(run_summary, leakage, data_audit, realtime, realtime_trades, wf_year, config, output_dir)
    scorecard = build_scorecard(metrics, config)
    raw_score = sum(float(row["points"]) for row in scorecard)
    score, score_caps_applied = apply_score_caps(raw_score, scorecard, metrics, config)
    status, reasons, blocking_failures = classify(metrics, scorecard, config)
    summary = {
        "framework_id": config["framework_id"],
        "framework_version": config["version"],
        "evaluated_output_dir": str(output_dir.relative_to(ROOT) if output_dir.is_relative_to(ROOT) else output_dir),
        "run_version": run_summary.get("version", ""),
        "policy_id": run_summary.get("policy_id", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "raw_score": raw_score,
        "score": score,
        "score_caps_applied": score_caps_applied,
        "evaluation_status": status,
        "is_effective": status == config["status_labels"]["effective"],
        "main_reasons": reasons,
        "blocking_failures": blocking_failures,
        "key_metrics": metrics,
        "research_boundary": config["research_boundary"],
    }
    return {"scorecard": scorecard, "summary": summary}


def build_metrics(
    run_summary: dict[str, Any],
    leakage: pd.DataFrame,
    data_audit: pd.DataFrame,
    realtime: dict[str, Any],
    realtime_trades: pd.DataFrame,
    wf_year: pd.DataFrame,
    config: dict[str, Any] | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    config = config or {}
    hard = config.get("hard_gates", {})
    cost = float(hard.get("round_trip_cost_bps", 0.0)) / 10000.0
    leakage_fail = int((leakage.get("status", pd.Series(dtype=str)).astype(str) == "fail").sum()) if not leakage.empty else 0
    data_fail = int((data_audit.get("status", pd.Series(dtype=str)).astype(str) == "fail").sum()) if not data_audit.empty else 0
    audit_fail_count = int(run_summary.get("audit_fail_count", 0) or 0) + leakage_fail + data_fail
    active_years = int_or_zero(realtime.get("active_years"))
    max_concentration = float_or_nan(realtime.get("max_single_year_concentration"), 1.0)
    event_count = int_or_zero(realtime.get("nonoverlap_events", realtime.get("trades", 0)))
    mean_return = float_or_nan(realtime.get("event_mean_return"))
    win_rate = float_or_nan(realtime.get("event_win_rate"))
    bad_rate = float_or_nan(realtime.get("event_bad_window_rate"), 1.0)
    worst_return = float_or_nan(realtime.get("event_worst_return"))
    returns = pd.Series(dtype=float)
    net_returns = pd.Series(dtype=float)
    relative_returns = pd.Series(dtype=float)
    recent_mean = math.nan
    recent_bad_rate = math.nan
    independent_clusters = 0
    max_cluster_concentration = math.nan
    cluster_net_mean_return = math.nan
    cluster_relative_mean_return = math.nan
    worst_cluster_net_return = math.nan
    realtime_net_median_return = math.nan
    win_loss_payoff_ratio = math.nan
    cluster_positive_rate = math.nan
    positive_cluster_return_concentration = math.nan
    bad_window_mean_return = math.nan
    path_worst_max_adverse_return = math.nan
    annual_positive_rate = math.nan
    relative_return_basis = "missing"

    if not realtime_trades.empty:
        returns_raw = pd.to_numeric(realtime_trades["trade_return"], errors="coerce")
        returns = returns_raw.dropna()
        net_returns = returns - cost
        realtime_net_median_return = float_or_nan(net_returns.median())
        win_loss_payoff_ratio = payoff_ratio(returns)
        path_worst_max_adverse_return = path_worst_adverse_return(realtime_trades, returns_raw)
        if "is_bad_window" in realtime_trades.columns:
            bad_mask = to_bool(realtime_trades["is_bad_window"])
            bad_window_mean_return = 0.0 if not bad_mask.any() else float_or_nan(returns_raw[bad_mask].mean())
        if "relative_return_horizon" in realtime_trades.columns:
            relative_returns = pd.to_numeric(realtime_trades["relative_return_horizon"], errors="coerce").dropna()
            relative_return_basis = infer_relative_return_basis(realtime_trades)
        elif "market_return_5d" in realtime_trades.columns:
            market = pd.to_numeric(realtime_trades["market_return_5d"], errors="coerce")
            relative_returns = (returns_raw - market).dropna()
            relative_return_basis = "market_return_5d"
        cluster_stats = event_cluster_stats(realtime_trades, int(hard.get("independence_cluster_gap_calendar_days", 30)))
        independent_clusters = cluster_stats["clusters"]
        max_cluster_concentration = cluster_stats["max_cluster_concentration"]
        clustered = event_cluster_frame(realtime_trades, int(hard.get("independence_cluster_gap_calendar_days", 30)))
        if not clustered.empty:
            clustered["_net_return"] = pd.to_numeric(clustered["trade_return"], errors="coerce") - cost
            cluster_net_mean_return = float_or_nan(clustered.groupby("_cluster_id")["_net_return"].mean().mean())
            if "relative_return_horizon" in clustered.columns:
                clustered["_relative_return"] = pd.to_numeric(clustered["relative_return_horizon"], errors="coerce")
                cluster_relative_mean_return = float_or_nan(clustered.groupby("_cluster_id")["_relative_return"].mean().mean())
            elif "market_return_5d" in clustered.columns:
                clustered["_relative_return"] = pd.to_numeric(clustered["trade_return"], errors="coerce") - pd.to_numeric(clustered["market_return_5d"], errors="coerce")
                cluster_relative_mean_return = float_or_nan(clustered.groupby("_cluster_id")["_relative_return"].mean().mean())
            cluster_means = clustered.groupby("_cluster_id")["_net_return"].mean()
            worst_cluster_net_return = float_or_nan(cluster_means.min())
            cluster_positive_rate = float_or_nan((cluster_means > 0).mean())
            positive_means = cluster_means.clip(lower=0)
            total_positive = float(positive_means.sum())
            positive_cluster_return_concentration = float_or_nan(positive_means.max() / total_positive) if total_positive > 0 else math.nan
        if math.isnan(mean_return) or event_count == 0:
            event_count = len(realtime_trades)
            mean_return = float_or_nan(returns.mean())
            win_rate = float_or_nan((returns > 0).mean())
            bad_rate = float_or_nan(to_bool(realtime_trades["is_bad_window"]).mean(), 1.0)
            worst_return = float_or_nan(returns.min())
            active_years = int(realtime_trades["year"].nunique()) if "year" in realtime_trades.columns else active_years
            max_concentration = float(realtime_trades["year"].value_counts(normalize=True).max()) if "year" in realtime_trades.columns and len(realtime_trades) else max_concentration
        annual_positive_rate = annual_positive_return_rate(realtime_trades, cost)
        ordered = realtime_trades.copy()
        date_col = "signal_date" if "signal_date" in ordered.columns else ("entry_date" if "entry_date" in ordered.columns else "")
        if date_col:
            ordered["_sort_date"] = pd.to_datetime(ordered[date_col], errors="coerce")
            ordered = ordered.sort_values("_sort_date")
        recent_n = max(1, math.ceil(len(ordered) * float(hard.get("recent_fraction", 0.33)))) if len(ordered) else 0
        recent = ordered.tail(recent_n).copy() if recent_n else pd.DataFrame()
        if not recent.empty:
            recent_mean = float_or_nan(pd.to_numeric(recent["trade_return"], errors="coerce").mean())
            recent_bad_rate = float_or_nan(to_bool(recent["is_bad_window"]).mean(), 1.0)

    wf_valid = wf_year[wf_year.get("status", pd.Series(dtype=str)).astype(str) == "pass"].copy() if not wf_year.empty else pd.DataFrame()
    wf_signal_years = int((pd.to_numeric(wf_valid.get("signal_dates", pd.Series(dtype=float)), errors="coerce") > 0).sum()) if not wf_valid.empty else 0
    wf_positive_years = int((pd.to_numeric(wf_valid.get("signal_mean_return", pd.Series(dtype=float)), errors="coerce") > 0).sum()) if not wf_valid.empty else 0
    wf_positive_rate = wf_positive_years / wf_signal_years if wf_signal_years else math.nan
    boot = bootstrap_stats(net_returns, int(hard.get("bootstrap_samples", 1000)), int(hard.get("bootstrap_seed", 42)))
    rel_boot = bootstrap_stats(relative_returns, int(hard.get("bootstrap_samples", 1000)), int(hard.get("bootstrap_seed", 42)))
    post_hoc = is_post_hoc_selected(run_summary, realtime, hard.get("post_hoc_policy_patterns", []))
    policy_freeze = policy_freeze_audit(output_dir, run_summary, hard)
    return {
        "audit_fail_count": audit_fail_count,
        "realtime_events": event_count,
        "independent_event_clusters": independent_clusters,
        "max_single_cluster_concentration": max_cluster_concentration,
        "realtime_mean_return": mean_return,
        "realtime_net_mean_return": float_or_nan(net_returns.mean()),
        "realtime_relative_mean_return": float_or_nan(relative_returns.mean()),
        "relative_return_basis": relative_return_basis,
        "cluster_net_mean_return": cluster_net_mean_return,
        "cluster_relative_mean_return": cluster_relative_mean_return,
        "worst_cluster_net_return": worst_cluster_net_return,
        "realtime_net_median_return": realtime_net_median_return,
        "win_loss_payoff_ratio": win_loss_payoff_ratio,
        "cluster_positive_rate": cluster_positive_rate,
        "positive_cluster_return_concentration": positive_cluster_return_concentration,
        "bad_window_mean_return": bad_window_mean_return,
        "path_worst_max_adverse_return": path_worst_max_adverse_return,
        "realtime_win_rate": win_rate,
        "realtime_bad_window_rate": bad_rate,
        "realtime_worst_return": worst_return,
        "bootstrap_positive_prob": boot["positive_prob"],
        "bootstrap_p05_mean_return": boot["p05_mean_return"],
        "relative_bootstrap_positive_prob": rel_boot["positive_prob"],
        "relative_bootstrap_p05_mean_return": rel_boot["p05_mean_return"],
        "recent_mean_return": recent_mean,
        "recent_bad_window_rate": recent_bad_rate,
        "active_years": active_years,
        "max_single_year_concentration": max_concentration,
        "annual_positive_rate": annual_positive_rate,
        "walk_forward_signal_years": wf_signal_years,
        "walk_forward_positive_year_rate": wf_positive_rate,
        "is_post_hoc_selected": post_hoc,
        "policy_freeze_pass": policy_freeze["passed"],
        "policy_freeze_evidence": policy_freeze["evidence"],
    }


def build_scorecard(metrics: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    w = config["score_weights"]
    hard = config["hard_gates"]
    return [
        score_row("audit", "审计通过", metrics["audit_fail_count"] == 0, w["audit"], f"audit_fail_count={metrics['audit_fail_count']}"),
        score_row("sample_size", "实时非重叠样本", metrics["realtime_events"] >= hard["min_realtime_events"], w["sample_size"], f"events={metrics['realtime_events']} / {hard['min_realtime_events']}"),
        score_row(
            "independent_clusters",
            "独立行情簇",
            metrics["independent_event_clusters"] >= hard["min_independent_event_clusters"]
            and metrics["max_single_cluster_concentration"] <= hard["max_single_cluster_concentration"],
            w["independent_clusters"],
            f"clusters={metrics['independent_event_clusters']} / {hard['min_independent_event_clusters']}; cluster_concentration={fmt_pct(metrics['max_single_cluster_concentration'])} / {fmt_pct(hard['max_single_cluster_concentration'])}",
        ),
        score_row("net_return", "成本后实时收益", metrics["realtime_net_mean_return"] >= hard["min_realtime_net_mean_return"], w["net_return"], f"net_mean_return={fmt_pct(metrics['realtime_net_mean_return'])} / {fmt_pct(hard['min_realtime_net_mean_return'])}; cost={hard.get('round_trip_cost_bps', 0)}bps"),
        score_row("relative_return", "相对/现金口径收益", metrics["realtime_relative_mean_return"] >= hard["min_realtime_relative_mean_return"], w["relative_return"], f"relative_mean_return={fmt_pct(metrics['realtime_relative_mean_return'])} / {fmt_pct(hard['min_realtime_relative_mean_return'])}; basis={metrics['relative_return_basis']}"),
        score_row("cluster_net_return", "独立簇等权成本后收益", metrics["cluster_net_mean_return"] >= hard["min_cluster_net_mean_return"], w["cluster_net_return"], f"cluster_net_mean={fmt_pct(metrics['cluster_net_mean_return'])} / {fmt_pct(hard['min_cluster_net_mean_return'])}"),
        score_row("cluster_relative_return", "独立簇等权相对收益", metrics["cluster_relative_mean_return"] >= hard["min_cluster_relative_mean_return"], w["cluster_relative_return"], f"cluster_relative_mean={fmt_pct(metrics['cluster_relative_mean_return'])} / {fmt_pct(hard['min_cluster_relative_mean_return'])}"),
        score_row("worst_cluster_net_return", "最差独立簇成本后收益", metrics["worst_cluster_net_return"] >= hard["min_worst_cluster_net_return"], w["worst_cluster_net_return"], f"worst_cluster_net={fmt_pct(metrics['worst_cluster_net_return'])} / {fmt_pct(hard['min_worst_cluster_net_return'])}"),
        score_row("median_net_return", "成本后中位数收益", metrics["realtime_net_median_return"] >= hard["min_realtime_net_median_return"], w["median_net_return"], f"net_median_return={fmt_pct(metrics['realtime_net_median_return'])} / {fmt_pct(hard['min_realtime_net_median_return'])}"),
        score_row("payoff_ratio", "盈亏比", metrics["win_loss_payoff_ratio"] >= hard["min_win_loss_payoff_ratio"], w["payoff_ratio"], f"win_loss_payoff_ratio={fmt_decimal(metrics['win_loss_payoff_ratio'])} / {fmt_decimal(hard['min_win_loss_payoff_ratio'])}"),
        score_row("cluster_positive_rate", "独立簇正收益比例", metrics["cluster_positive_rate"] >= hard["min_cluster_positive_rate"], w["cluster_positive_rate"], f"cluster_positive_rate={fmt_pct(metrics['cluster_positive_rate'])} / {fmt_pct(hard['min_cluster_positive_rate'])}"),
        score_row("cluster_return_concentration", "正收益簇集中度", metrics["positive_cluster_return_concentration"] <= hard["max_positive_cluster_return_concentration"], w["cluster_return_concentration"], f"positive_cluster_concentration={fmt_pct(metrics['positive_cluster_return_concentration'])} / {fmt_pct(hard['max_positive_cluster_return_concentration'])}"),
        score_row("bad_window_severity", "坏窗口严重度", metrics["bad_window_mean_return"] >= hard["min_bad_window_mean_return"], w["bad_window_severity"], f"bad_window_mean={fmt_pct(metrics['bad_window_mean_return'])} / {fmt_pct(hard['min_bad_window_mean_return'])}"),
        score_row("path_drawdown_control", "持有路径最大不利波动", metrics["path_worst_max_adverse_return"] >= hard["min_path_worst_max_adverse_return"], w["path_drawdown_control"], f"path_worst_adverse={fmt_pct(metrics['path_worst_max_adverse_return'])} / {fmt_pct(hard['min_path_worst_max_adverse_return'])}"),
        score_row(
            "bootstrap_confidence",
            "Bootstrap置信",
            metrics["bootstrap_positive_prob"] >= hard["min_bootstrap_positive_prob"] and metrics["bootstrap_p05_mean_return"] >= hard["min_bootstrap_p05_mean_return"],
            w["bootstrap_confidence"],
            f"positive_prob={fmt_pct(metrics['bootstrap_positive_prob'])}; p05_mean={fmt_pct(metrics['bootstrap_p05_mean_return'])}",
        ),
        score_row(
            "relative_bootstrap_confidence",
            "相对收益Bootstrap置信",
            metrics["relative_bootstrap_positive_prob"] >= hard["min_relative_bootstrap_positive_prob"] and metrics["relative_bootstrap_p05_mean_return"] >= hard["min_relative_bootstrap_p05_mean_return"],
            w["relative_bootstrap_confidence"],
            f"relative_positive_prob={fmt_pct(metrics['relative_bootstrap_positive_prob'])}; relative_p05_mean={fmt_pct(metrics['relative_bootstrap_p05_mean_return'])}",
        ),
        score_row("realtime_win_rate", "实时仿真胜率", metrics["realtime_win_rate"] >= hard["min_realtime_win_rate"], w["realtime_win_rate"], f"win_rate={fmt_pct(metrics['realtime_win_rate'])} / {fmt_pct(hard['min_realtime_win_rate'])}"),
        score_row("bad_window_control", "坏窗口控制", metrics["realtime_bad_window_rate"] <= hard["max_realtime_bad_window_rate"], w["bad_window_control"], f"bad_window={fmt_pct(metrics['realtime_bad_window_rate'])} / {fmt_pct(hard['max_realtime_bad_window_rate'])}"),
        score_row("tail_loss_control", "尾部亏损控制", metrics["realtime_worst_return"] >= hard["min_worst_realtime_return"], w["tail_loss_control"], f"worst_return={fmt_pct(metrics['realtime_worst_return'])} / {fmt_pct(hard['min_worst_realtime_return'])}"),
        score_row("year_stability", "年份稳定性", metrics["active_years"] >= hard["min_active_years"] and metrics["max_single_year_concentration"] <= hard["max_single_year_concentration"], w["year_stability"], f"years={metrics['active_years']}; concentration={fmt_pct(metrics['max_single_year_concentration'])}"),
        score_row("annual_positive_rate", "年度正收益比例", metrics["annual_positive_rate"] >= hard["min_annual_positive_rate"], w["annual_positive_rate"], f"annual_positive_rate={fmt_pct(metrics['annual_positive_rate'])} / {fmt_pct(hard['min_annual_positive_rate'])}"),
        score_row("recent_stability", "近期样本稳定性", metrics["recent_mean_return"] >= hard["min_recent_mean_return"] and metrics["recent_bad_window_rate"] <= hard["max_recent_bad_window_rate"], w["recent_stability"], f"recent_mean={fmt_pct(metrics['recent_mean_return'])}; recent_bad={fmt_pct(metrics['recent_bad_window_rate'])}"),
        score_row("walk_forward_stability", "Walk-forward稳定性", metrics["walk_forward_signal_years"] >= hard["min_walk_forward_signal_years"] and metrics["walk_forward_positive_year_rate"] >= hard["min_walk_forward_positive_year_rate"], w["walk_forward_stability"], f"positive_year_rate={fmt_pct(metrics['walk_forward_positive_year_rate'])}; signal_years={metrics['walk_forward_signal_years']}"),
        score_row("selection_bias_control", "后验选择控制", not metrics["is_post_hoc_selected"], w["selection_bias_control"], f"is_post_hoc_selected={metrics['is_post_hoc_selected']}"),
        score_row("policy_freeze_audit", "政策冻结审计", bool(metrics["policy_freeze_pass"]), w["policy_freeze_audit"], metrics["policy_freeze_evidence"]),
    ]


def apply_score_caps(raw_score: float, scorecard: list[dict[str, Any]], metrics: dict[str, Any], config: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    caps = config.get("score_caps", {})
    triggered: list[dict[str, Any]] = []

    def cap_if(condition: bool, cap_id: str, reason: str) -> None:
        if condition and cap_id in caps:
            triggered.append({"cap_id": cap_id, "cap": float(caps[cap_id]), "reason": reason})

    failed = {row["metric_id"] for row in scorecard if not bool(row["passed"])}
    cap_if(metrics["audit_fail_count"] > config["hard_gates"]["audit_fail_count_max"], "audit_failed", "审计失败时不能给研究有效分")
    cap_if(bool(metrics["is_post_hoc_selected"]), "post_hoc_selected", "后验/理论上限/未来收益选择只能作为容量观察")
    cap_if("sample_size" in failed, "sample_size_failed", "样本数不足，不能认证稳定窗口")
    cap_if("independent_clusters" in failed, "independent_clusters_failed", "独立行情簇不足，非重叠事件可能仍来自同一轮行情")
    cap_if("net_return" in failed and "relative_return" in failed, "core_return_failed", "绝对收益和相对收益同时不足，不能用风险控制分数掩盖收益厚度缺口")
    cap_if("net_return" in failed, "net_return_failed", "成本后收益厚度不足")
    cap_if("relative_return" in failed, "relative_return_failed", "相对市场收益不足")
    cap_if("cluster_net_return" in failed, "cluster_net_return_failed", "独立簇等权成本后收益不足")
    cap_if("cluster_relative_return" in failed, "cluster_relative_return_failed", "独立簇等权相对收益不足")
    cap_if("worst_cluster_net_return" in failed, "worst_cluster_net_return_failed", "最差独立行情簇亏损过深")
    cap_if("median_net_return" in failed, "median_net_return_failed", "成本后中位数收益不足")
    cap_if("payoff_ratio" in failed, "payoff_ratio_failed", "盈亏比不足")
    cap_if("cluster_positive_rate" in failed, "cluster_positive_rate_failed", "独立行情簇正收益比例不足")
    cap_if("cluster_return_concentration" in failed, "cluster_return_concentration_failed", "正收益过度集中在少数行情簇")
    cap_if("bad_window_severity" in failed, "bad_window_severity_failed", "坏窗口平均亏损过深")
    cap_if("path_drawdown_control" in failed, "path_drawdown_failed", "持有路径最大不利波动过深")
    cap_if("bootstrap_confidence" in failed, "bootstrap_failed", "Bootstrap 下沿或正收益概率不足")
    cap_if("relative_bootstrap_confidence" in failed, "relative_bootstrap_failed", "相对收益 Bootstrap 下沿或正收益概率不足")
    cap_if("tail_loss_control" in failed, "tail_loss_failed", "单笔尾部亏损过深")
    cap_if("year_stability" in failed, "year_stability_failed", "年份覆盖或集中度不达标")
    cap_if("annual_positive_rate" in failed, "annual_positive_rate_failed", "年度正收益比例不足")
    cap_if("recent_stability" in failed, "recent_stability_failed", "近期样本稳定性不达标")
    cap_if("policy_freeze_audit" in failed, "policy_freeze_failed", "缺少可复核的事前冻结政策记录")

    score = raw_score
    for item in triggered:
        score = min(score, item["cap"])
    return float(score), triggered


def classify(metrics: dict[str, Any], scorecard: list[dict[str, Any]], config: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]]]:
    labels = config["status_labels"]
    hard = config["hard_gates"]
    cond = config["conditional_gates"]
    blocking_ids = set(config.get("blocking_metric_ids", []))
    failed_rows = [row for row in scorecard if not bool(row["passed"])]
    blocking_failures = [row for row in failed_rows if row["metric_id"] in blocking_ids]

    if metrics["audit_fail_count"] > hard["audit_fail_count_max"]:
        return labels["audit_failed"], ["存在数据或泄漏审计失败。"], blocking_failures
    if not failed_rows:
        return labels["effective"], ["全部硬门槛通过。"], blocking_failures

    reasons = [row["evidence"] for row in failed_rows]
    if metrics["is_post_hoc_selected"]:
        return labels["theoretical"], reasons, blocking_failures

    sample_and_risk_ok = (
        metrics["realtime_events"] >= hard["min_realtime_events"]
        and metrics["independent_event_clusters"] >= hard["min_independent_event_clusters"]
        and metrics["realtime_win_rate"] >= hard["min_realtime_win_rate"]
        and metrics["realtime_bad_window_rate"] <= hard["max_realtime_bad_window_rate"]
        and metrics["bad_window_mean_return"] >= hard["min_bad_window_mean_return"]
        and metrics["realtime_worst_return"] >= hard["min_worst_realtime_return"]
        and metrics["worst_cluster_net_return"] >= hard["min_worst_cluster_net_return"]
        and metrics["path_worst_max_adverse_return"] >= hard["min_path_worst_max_adverse_return"]
        and metrics["cluster_positive_rate"] >= hard["min_cluster_positive_rate"]
        and metrics["positive_cluster_return_concentration"] <= hard["max_positive_cluster_return_concentration"]
        and metrics["active_years"] >= hard["min_active_years"]
        and metrics["max_single_year_concentration"] <= hard["max_single_year_concentration"]
        and metrics["annual_positive_rate"] >= hard["min_annual_positive_rate"]
        and metrics["policy_freeze_pass"]
    )
    core_return_failed = (
        metrics["realtime_net_mean_return"] < hard["min_realtime_net_mean_return"]
        or metrics["realtime_relative_mean_return"] < hard["min_realtime_relative_mean_return"]
        or metrics["cluster_net_mean_return"] < hard["min_cluster_net_mean_return"]
        or metrics["cluster_relative_mean_return"] < hard["min_cluster_relative_mean_return"]
        or metrics["worst_cluster_net_return"] < hard["min_worst_cluster_net_return"]
        or metrics["realtime_net_median_return"] < hard["min_realtime_net_median_return"]
        or metrics["win_loss_payoff_ratio"] < hard["min_win_loss_payoff_ratio"]
    )
    if sample_and_risk_ok and core_return_failed:
        return labels["economic_insufficient"], reasons, blocking_failures

    conditional = (
        metrics["realtime_events"] >= cond["min_realtime_events"]
        and metrics["realtime_net_mean_return"] >= cond["min_realtime_net_mean_return"]
        and metrics["realtime_relative_mean_return"] >= cond["min_realtime_relative_mean_return"]
        and metrics["realtime_win_rate"] >= cond["min_realtime_win_rate"]
        and metrics["realtime_bad_window_rate"] <= cond["max_realtime_bad_window_rate"]
        and metrics["active_years"] >= cond["min_active_years"]
        and metrics["max_single_year_concentration"] <= cond["max_single_year_concentration"]
        and metrics["policy_freeze_pass"]
    )
    if conditional:
        return labels["conditional"], reasons, blocking_failures
    if metrics["realtime_events"] < cond["min_realtime_events"]:
        return labels["insufficient"], reasons, blocking_failures
    return labels["rejected"], reasons, blocking_failures


def select_realtime_row(top: pd.DataFrame, realtime_summary: pd.DataFrame) -> dict[str, Any]:
    if not realtime_summary.empty:
        return realtime_summary.iloc[0].to_dict()
    if not top.empty:
        mask = top["signal_id"].astype(str).str.contains("realtime", case=False, na=False)
        if mask.any():
            return top[mask].iloc[0].to_dict()
    return {}


def score_row(metric_id: str, metric_name_zh: str, passed: bool, max_points: float, evidence: str) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "metric_name_zh": metric_name_zh,
        "passed": bool(passed),
        "points": float(max_points if passed else 0.0),
        "max_points": float(max_points),
        "evidence": evidence,
    }


def event_cluster_stats(trades: pd.DataFrame, gap_days: int) -> dict[str, Any]:
    clustered = event_cluster_frame(trades, gap_days)
    if clustered.empty:
        return {"clusters": 0, "max_cluster_concentration": math.nan}
    sizes = clustered["_cluster_id"].value_counts()
    max_concentration = sizes.max() / len(clustered) if len(clustered) else math.nan
    return {"clusters": int(sizes.size), "max_cluster_concentration": float(max_concentration)}


def event_cluster_frame(trades: pd.DataFrame, gap_days: int) -> pd.DataFrame:
    date_col = "signal_date" if "signal_date" in trades.columns else ("entry_date" if "entry_date" in trades.columns else "")
    if not date_col or trades.empty:
        return pd.DataFrame()
    ordered = trades.copy()
    ordered["_cluster_date"] = pd.to_datetime(ordered[date_col], errors="coerce")
    ordered = ordered.dropna(subset=["_cluster_date"]).sort_values("_cluster_date")
    if ordered.empty:
        return pd.DataFrame()
    cluster_ids: list[int] = []
    cluster_id = -1
    last_date = None
    for date in ordered["_cluster_date"]:
        if last_date is None or (date - last_date).days > gap_days:
            cluster_id += 1
        cluster_ids.append(cluster_id)
        last_date = date
    ordered["_cluster_id"] = cluster_ids
    return ordered


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [clean_json_value(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value


def to_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "yes"])


def bootstrap_stats(returns: pd.Series, samples: int, seed: int) -> dict[str, float]:
    values = [float(x) for x in returns.dropna().tolist()]
    if not values:
        return {"positive_prob": math.nan, "p05_mean_return": math.nan}
    rng = random.Random(seed)
    means: list[float] = []
    n = len(values)
    for _ in range(max(samples, 1)):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    p05 = means[min(len(means) - 1, max(0, int(len(means) * 0.05)))]
    positive_prob = sum(1 for x in means if x > 0.0) / len(means)
    return {"positive_prob": float(positive_prob), "p05_mean_return": float(p05)}


def payoff_ratio(returns: pd.Series) -> float:
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    if wins.empty or losses.empty:
        return math.nan
    return float(wins.mean() / abs(losses.mean()))


def infer_relative_return_basis(trades: pd.DataFrame) -> str:
    if "benchmark_return_horizon" not in trades.columns:
        return "relative_return_horizon"
    benchmark = pd.to_numeric(trades["benchmark_return_horizon"], errors="coerce").fillna(0.0)
    if float(benchmark.abs().sum()) < 1e-12:
        return "cash_or_zero_benchmark"
    return "benchmark_return_horizon"


def is_post_hoc_selected(run_summary: dict[str, Any], realtime: dict[str, Any], patterns: list[Any]) -> bool:
    haystack = " ".join(
        str(x).lower()
        for x in [
            run_summary.get("policy_id", ""),
            run_summary.get("best_signal_id", ""),
            run_summary.get("main_diagnosis", ""),
            realtime.get("signal_id", ""),
            realtime.get("signal_type", ""),
            realtime.get("signal_name_zh", ""),
        ]
    )
    return any(str(pattern).lower() in haystack for pattern in patterns)


def path_worst_adverse_return(trades: pd.DataFrame, returns: pd.Series) -> float:
    if "max_adverse_return" in trades.columns:
        adverse = pd.to_numeric(trades["max_adverse_return"], errors="coerce").dropna()
        if not adverse.empty:
            return float_or_nan(adverse.min())
    # ponytail: 旧输出没有路径回撤时，用最终收益兜底；升级方向是所有 runner 都写 max_adverse_return。
    return float_or_nan(returns.dropna().min())


def annual_positive_return_rate(trades: pd.DataFrame, cost: float) -> float:
    if trades.empty or "year" not in trades.columns or "trade_return" not in trades.columns:
        return math.nan
    frame = trades[["year", "trade_return"]].copy()
    frame["_net_return"] = pd.to_numeric(frame["trade_return"], errors="coerce") - cost
    yearly = frame.dropna(subset=["_net_return"]).groupby("year")["_net_return"].mean()
    if yearly.empty:
        return math.nan
    return float((yearly > 0).mean())


def policy_freeze_audit(output_dir: Path | None, run_summary: dict[str, Any], hard: dict[str, Any]) -> dict[str, Any]:
    require_policy = bool(hard.get("require_frozen_policy_file", False))
    require_note = bool(hard.get("require_optimization_note", False))
    if output_dir is None:
        return {"passed": not (require_policy or require_note), "evidence": "output_dir missing"}
    debug_dir = output_dir / "debug"
    frozen_path = debug_dir / "frozen_policy.json"
    note_path = debug_dir / "optimization_notes.json"
    missing: list[str] = []
    if require_policy and not frozen_path.exists():
        missing.append("frozen_policy.json")
    if require_note and not note_path.exists():
        missing.append("optimization_notes.json")
    if missing:
        return {"passed": False, "evidence": "missing " + ",".join(missing)}
    if frozen_path.exists():
        try:
            frozen = read_json(frozen_path)
        except json.JSONDecodeError:
            return {"passed": False, "evidence": "frozen_policy.json invalid_json"}
        frozen_policy_id = str(frozen.get("policy_id", ""))
        run_policy_id = str(run_summary.get("policy_id", ""))
        if run_policy_id and frozen_policy_id and frozen_policy_id != run_policy_id:
            return {"passed": False, "evidence": f"policy_id mismatch: frozen={frozen_policy_id}; run={run_policy_id}"}
    return {"passed": True, "evidence": "frozen_policy.json and optimization_notes.json present"}


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def float_or_nan(value: Any, default: float = math.nan) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def fmt_pct(value: Any) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number:.2%}"


def fmt_decimal(value: Any) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number:.2f}"


if __name__ == "__main__":
    main()
