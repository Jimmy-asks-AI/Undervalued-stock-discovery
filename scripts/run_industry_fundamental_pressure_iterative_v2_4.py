#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEATURES = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "historical_feature_panel.csv"
DEFAULT_RANKING = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "all_ranked_industries.csv"
DEFAULT_HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
DEFAULT_CURRENT_V24_OUTPUT = ROOT / "outputs" / "industry_fundamental_pressure_v2_4"
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_fundamental_pressure_iterative_v2_4"
V23_SCRIPT = ROOT / "scripts" / "run_industry_pressure_quality_v2_3.py"
V24_CURRENT_SCRIPT = ROOT / "scripts" / "run_industry_fundamental_pressure_v2_4.py"
VERSION = "2.4.iterative.3"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run three V2.4 iterative PIT-proxy backtest and review loops.")
    parser.add_argument("--features", default=str(DEFAULT_FEATURES), help="PIT historical feature panel.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current industry ranking for names.")
    parser.add_argument("--history-dir", default=str(DEFAULT_HISTORY_DIR), help="Cached industry daily histories.")
    parser.add_argument("--current-v24-output", default=str(DEFAULT_CURRENT_V24_OUTPUT), help="V2.4 current snapshot output directory.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--horizons", default="60,120,252", help="Forward holding horizons.")
    parser.add_argument("--top-ns", default="5,10,20", help="Top N baskets.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="One-way rebalance cost in bps.")
    parser.add_argument("--bootstrap-rounds", type=int, default=500, help="Block bootstrap rounds.")
    parser.add_argument("--oos-split-ratio", type=float, default=0.70, help="Walk-forward split ratio.")
    parser.add_argument("--rebalance-step-days", type=int, default=20, help="Feature-panel rebalance step approximation.")
    parser.add_argument("--skip-current-refresh", action="store_true", help="Do not rerun the V2.4 current snapshot runner.")
    args = parser.parse_args()

    output_dir = Path(args.output)
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_current_refresh:
        subprocess.run([sys.executable, str(V24_CURRENT_SCRIPT)], cwd=str(ROOT), check=True)

    v23 = load_v23_module()
    horizons = parse_int_list(args.horizons)
    top_ns = parse_int_list(args.top_ns)
    current_summary = load_json(Path(args.current_v24_output) / "run_summary.json")

    features = v23.attach_names(v23.load_features(Path(args.features)), v23.load_names(Path(args.ranking)))
    close_matrix = v23.load_close_matrix(Path(args.history_dir), features["industry_code"].dropna().unique().tolist())

    reviews: list[dict[str, Any]] = []
    all_panels: list[pd.DataFrame] = []
    all_events: list[pd.DataFrame] = []
    all_nonoverlap: list[pd.DataFrame] = []
    all_oos: list[pd.DataFrame] = []
    all_bootstrap: list[pd.DataFrame] = []
    all_nav: list[pd.DataFrame] = []
    all_nav_metrics: list[pd.DataFrame] = []
    all_sensitivity: list[pd.DataFrame] = []
    all_rejections: list[pd.DataFrame] = []

    config = initial_iteration_config()
    for iteration in range(1, 4):
        config["iteration"] = iteration
        panel, strategy = build_iteration_panel(v23, features, config)
        result = run_backtest_iteration(
            v23=v23,
            signal_panel=panel,
            strategy=strategy,
            horizons=horizons,
            top_ns=top_ns,
            cost_bps=args.cost_bps,
            bootstrap_rounds=args.bootstrap_rounds,
            oos_split_ratio=args.oos_split_ratio,
            rebalance_step_days=args.rebalance_step_days,
            close_matrix=close_matrix,
        )
        review = review_iteration(config, result["parameter_sensitivity"], result["rejection_log"])
        reviews.append(review)

        all_panels.append(add_iteration_metadata(panel, config))
        for key, target in [
            ("event_backtest", all_events),
            ("nonoverlap", all_nonoverlap),
            ("walk_forward", all_oos),
            ("bootstrap", all_bootstrap),
            ("daily_nav", all_nav),
            ("nav_metrics", all_nav_metrics),
            ("parameter_sensitivity", all_sensitivity),
            ("rejection_log", all_rejections),
        ]:
            frame = result[key]
            if not frame.empty:
                target.append(add_iteration_metadata(frame, config))

        if iteration < 3:
            config = next_iteration_config(config, review)

    signal_panel_out = concat_frames(all_panels)
    event_out = concat_frames(all_events)
    nonoverlap_out = concat_frames(all_nonoverlap)
    oos_out = concat_frames(all_oos)
    bootstrap_out = concat_frames(all_bootstrap)
    nav_out = concat_frames(all_nav)
    nav_metrics_out = concat_frames(all_nav_metrics)
    sensitivity_out = concat_frames(all_sensitivity)
    rejection_out = concat_frames(all_rejections)
    reviews_out = pd.DataFrame(reviews)
    top_candidates = build_top_candidates(sensitivity_out, rejection_out, reviews_out)
    configs = build_config_output(reviews)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    signal_panel_out.to_csv(debug_dir / "iteration_signal_panel.csv", index=False, encoding="utf-8-sig")
    event_out.to_csv(debug_dir / "event_backtest.csv", index=False, encoding="utf-8-sig")
    nonoverlap_out.to_csv(debug_dir / "nonoverlap_backtest.csv", index=False, encoding="utf-8-sig")
    oos_out.to_csv(debug_dir / "walk_forward_oos.csv", index=False, encoding="utf-8-sig")
    bootstrap_out.to_csv(debug_dir / "bootstrap_confidence.csv", index=False, encoding="utf-8-sig")
    nav_out.to_csv(debug_dir / "daily_portfolio_nav.csv", index=False, encoding="utf-8-sig")
    nav_metrics_out.to_csv(debug_dir / "portfolio_nav_metrics.csv", index=False, encoding="utf-8-sig")
    sensitivity_out.to_csv(debug_dir / "parameter_sensitivity.csv", index=False, encoding="utf-8-sig")
    rejection_out.to_csv(debug_dir / "signal_rejection_log.csv", index=False, encoding="utf-8-sig")
    reviews_out.to_csv(debug_dir / "iteration_reviews.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "iteration_configs.json", configs)

    summary = build_summary(
        current_summary=current_summary,
        features=features,
        event_backtest=event_out,
        reviews=reviews_out,
        top_candidates=top_candidates,
        args=args,
        horizons=horizons,
        top_ns=top_ns,
    )
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            current_summary=current_summary,
            reviews=reviews_out,
            top_candidates=top_candidates,
            sensitivity=sensitivity_out,
            rejections=rejection_out,
        ),
        encoding="utf-8",
    )

    print(f"V{VERSION} 三轮迭代回测完成")
    print(f"样本区间={summary['date_start']} 至 {summary['date_end']}")
    print(f"迭代轮数={summary['iteration_count']}")
    print(f"事件行数={summary['event_rows']}")
    print(f"候选信号={summary['candidate_signal_count']}")
    print(f"条件观察={summary['conditional_signal_count']}")
    print(f"最终结论={summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v23_module() -> Any:
    spec = importlib.util.spec_from_file_location("industry_pressure_quality_v2_3", V23_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load V2.3 module: {V23_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def initial_iteration_config() -> dict[str, Any]:
    return {
        "iteration_zh": "第1轮：V2.4 压力质量基线",
        "strategy_id": "v24_iter1_pressure_quality_baseline",
        "strategy_zh": "V2.4迭代1：压力质量基线",
        "description": "使用 V2.3 压力质量信号作为 V2.4 历史代理基线；当前估值只用于解释，不进入历史特征。",
        "review_input_reason": "从 V2.4 报告出发，先验证现有 PIT 价格压力质量代理是否能独立形成历史证据。",
        "score_variant": "baseline_pressure_quality",
        "stress_threshold": 0.65,
        "quality_min": 0.50,
        "liquidity_min": 0.50,
        "recovery_min": 0.00,
        "low_vol_min": 0.00,
        "drawdown_quality_min": 0.00,
        "oversold_min": 0.45,
        "oversold_max": 0.95,
        "max_trap_score": 1,
    }


def next_iteration_config(previous: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    iteration = int(previous["iteration"]) + 1
    weak_nav = safe_number(review.get("best_relative_final_nav")) <= 1.0
    weak_bootstrap = safe_number(review.get("best_bootstrap_ci_5")) <= 0.0
    thin_sample = safe_number(review.get("best_sample_strength")) < 0.50

    if iteration == 2:
        return {
            "iteration_zh": "第2轮：扩样 + 流动性确认",
            "strategy_id": "v24_iter2_liquid_expanded_pressure",
            "strategy_zh": "V2.4迭代2：扩样流动性",
            "description": "根据第1轮样本不足和净值不稳的问题，放宽压力阈值，同时提高流动性要求。",
            "review_input_reason": build_next_reason(review, "第1轮", "降低压力阈值到 0.60，并把流动性确认作为硬门槛。"),
            "score_variant": "liquidity_expanded",
            "stress_threshold": 0.60 if thin_sample else 0.63,
            "quality_min": 0.45,
            "liquidity_min": 0.55,
            "recovery_min": 0.00,
            "low_vol_min": 0.00,
            "drawdown_quality_min": 0.00,
            "oversold_min": 0.45,
            "oversold_max": 0.95,
            "max_trap_score": 1,
        }

    return {
        "iteration_zh": "第3轮：防守质量确认",
        "strategy_id": "v24_iter3_defensive_confirmed_pressure",
        "strategy_zh": "V2.4迭代3：防守质量确认",
        "description": "根据第2轮审查，加入低波、回撤伤害和短期修复确认，但保留足够事件样本，避免第三轮不可检验。",
        "review_input_reason": build_next_reason(
            review,
            "第2轮",
            "适度放宽压力阈值到 0.55，同时保留低波、回撤质量和短期修复门槛；若仍不通过，则停止继续加参数。",
        ),
        "score_variant": "defensive_confirmed",
        "stress_threshold": 0.55,
        "quality_min": 0.45,
        "liquidity_min": 0.50,
        "recovery_min": 0.30 if (weak_nav or weak_bootstrap) else 0.25,
        "low_vol_min": 0.35,
        "drawdown_quality_min": 0.30,
        "oversold_min": 0.45,
        "oversold_max": 0.92,
        "max_trap_score": 1,
    }


def build_iteration_panel(v23: Any, features: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, Any]:
    base = v23.build_pressure_quality_signal_panel(
        features,
        stress_threshold=float(config["stress_threshold"]),
        extreme_stress_threshold=max(0.80, float(config["stress_threshold"]) + 0.15),
    )
    frame = base.copy()
    score_col = f"v24_iter{config['iteration']}_score"
    gate_col = f"v24_iter{config['iteration']}_gate"
    raw_col = f"{score_col}_raw"

    if config["score_variant"] == "baseline_pressure_quality":
        frame[raw_col] = frame["pressure_quality_score"].fillna(0.0)
    elif config["score_variant"] == "liquidity_expanded":
        frame[raw_col] = (
            0.44 * frame["pressure_quality_score"].fillna(0.0)
            + 0.28 * frame["liquidity_quality_score"].fillna(0.0)
            + 0.16 * frame["recovery_quality_score"].fillna(0.0)
            + 0.12 * frame["market_stress_score"].fillna(0.0)
            - 0.10 * frame["momentum_trap_score"].clip(upper=3)
        )
    else:
        frame[raw_col] = (
            0.38 * frame["pressure_quality_score"].fillna(0.0)
            + 0.20 * frame["liquidity_quality_score"].fillna(0.0)
            + 0.18 * frame["low_volatility_quality_score"].fillna(0.0)
            + 0.12 * frame["drawdown_quality_score"].fillna(0.0)
            + 0.12 * frame["recovery_quality_score"].fillna(0.0)
            - 0.12 * frame["momentum_trap_score"].clip(upper=3)
            - 0.05 * frame["deep_breakdown_trap"].astype(float)
        )
    frame[score_col] = frame.groupby("trade_date")[raw_col].rank(pct=True, method="average")
    frame[gate_col] = (
        (frame["market_stress_score"] >= float(config["stress_threshold"]))
        & (frame["momentum_trap_score"] <= int(config["max_trap_score"]))
        & (frame["price_quality_composite"] >= float(config["quality_min"]))
        & (frame["liquidity_quality_score"] >= float(config["liquidity_min"]))
        & (frame["recovery_quality_score"] >= float(config["recovery_min"]))
        & (frame["low_volatility_quality_score"] >= float(config["low_vol_min"]))
        & (frame["drawdown_quality_score"] >= float(config["drawdown_quality_min"]))
        & (frame["stabilized_oversold_signal"] >= float(config["oversold_min"]))
        & (frame["stabilized_oversold_signal"] <= float(config["oversold_max"]))
    )
    frame["iteration"] = int(config["iteration"])
    frame["iteration_zh"] = str(config["iteration_zh"])
    frame["iteration_strategy_id"] = str(config["strategy_id"])
    frame["iteration_score_variant"] = str(config["score_variant"])
    frame["iteration_gate_col"] = gate_col
    frame["iteration_score_col"] = score_col

    strategy = v23.StrategySpec(
        str(config["strategy_id"]),
        str(config["strategy_zh"]),
        score_col,
        gate_col,
        "conditional_only_signal",
        str(config["description"]),
    )
    return frame.drop(columns=[raw_col]), strategy


def run_backtest_iteration(
    *,
    v23: Any,
    signal_panel: pd.DataFrame,
    strategy: Any,
    horizons: list[int],
    top_ns: list[int],
    cost_bps: float,
    bootstrap_rounds: int,
    oos_split_ratio: float,
    rebalance_step_days: int,
    close_matrix: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    previous_strategies = v23.STRATEGIES
    v23.STRATEGIES = [strategy]
    try:
        event_backtest = v23.compute_event_backtest(signal_panel, horizons, top_ns, cost_bps)
        nonoverlap = v23.compute_nonoverlap_backtest(event_backtest, rebalance_step_days)
        walk_forward = v23.compute_walk_forward_oos(event_backtest, oos_split_ratio)
        bootstrap = v23.compute_bootstrap_confidence(event_backtest, bootstrap_rounds)
        daily_nav = v23.compute_daily_portfolio_nav(signal_panel, close_matrix, top_ns, cost_bps)
        nav_metrics = v23.compute_nav_metrics(daily_nav)
        parameter_sensitivity = v23.compute_parameter_sensitivity(event_backtest, nonoverlap, walk_forward, bootstrap, nav_metrics)
        rejection_log = v23.build_signal_rejection_log(parameter_sensitivity)
    finally:
        v23.STRATEGIES = previous_strategies
    return {
        "event_backtest": event_backtest,
        "nonoverlap": nonoverlap,
        "walk_forward": walk_forward,
        "bootstrap": bootstrap,
        "daily_nav": daily_nav,
        "nav_metrics": nav_metrics,
        "parameter_sensitivity": parameter_sensitivity,
        "rejection_log": rejection_log,
    }


def review_iteration(config: dict[str, Any], sensitivity: pd.DataFrame, rejection_log: pd.DataFrame) -> dict[str, Any]:
    if sensitivity.empty:
        return {
            **config_review_fields(config),
            "review_status": "无有效样本",
            "best_strategy": "",
            "best_top_n": 0,
            "best_horizon": 0,
            "best_mean_relative_return": math.nan,
            "best_oos_mean_relative_return": math.nan,
            "best_nonoverlap_mean_relative_return": math.nan,
            "best_bootstrap_ci_5": math.nan,
            "best_bootstrap_probability_positive": math.nan,
            "best_relative_final_nav": math.nan,
            "best_sample_strength": 0.0,
            "candidate_signal_count": 0,
            "conditional_signal_count": 0,
            "rejected_signal_count": 0,
            "main_issues": "没有形成满足 TopN 的有效事件样本",
            "next_action": "停止扩大参数，先检查数据覆盖和门槛设置",
        }
    best = sensitivity.sort_values("robust_score", ascending=False).iloc[0].to_dict()
    status_counts = rejection_log["signal_status"].value_counts().to_dict() if not rejection_log.empty else {}
    issues = build_issues(best)
    candidate_count = int(status_counts.get("candidate_signal", 0))
    conditional_count = int(status_counts.get("conditional_only_signal", 0))
    rejected_count = int(status_counts.get("rejected_standalone_signal", 0))
    if candidate_count > 0:
        review_status = "候选待人工复核"
    elif conditional_count > 0:
        review_status = "条件观察，禁止升级"
    else:
        review_status = "拒绝升级"
    if int(config["iteration"]) >= 3:
        next_action = "三轮均未证明可升级；停止继续加参，转向估值PIT归档、扩展历史估值源和跨样本验证。"
    elif safe_number(best.get("sample_strength")) < 0.50:
        next_action = "下一轮优先扩样，但必须保留流动性或质量确认，避免只放宽到纯反转。"
    elif safe_number(best.get("relative_final_nav")) <= 1.0:
        next_action = "下一轮加入防守质量、回撤伤害和短期修复约束，先解决逐日净值不过关。"
    elif safe_number(best.get("bootstrap_ci_5")) <= 0.0:
        next_action = "下一轮降低尾部噪声，要求更稳定的质量确认和更宽样本。"
    else:
        next_action = "下一轮保持当前方向，并检查更严格成本和年度稳定性。"
    return {
        **config_review_fields(config),
        "review_status": review_status,
        "best_strategy": best.get("strategy_zh", ""),
        "best_top_n": int(best.get("top_n", 0)),
        "best_horizon": int(best.get("horizon", 0)),
        "best_mean_relative_return": best.get("mean_relative_return", math.nan),
        "best_oos_mean_relative_return": best.get("oos_mean_relative_return", math.nan),
        "best_nonoverlap_mean_relative_return": best.get("nonoverlap_mean_relative_return", math.nan),
        "best_bootstrap_ci_5": best.get("bootstrap_ci_5", math.nan),
        "best_bootstrap_probability_positive": best.get("bootstrap_probability_positive", math.nan),
        "best_relative_final_nav": best.get("relative_final_nav", math.nan),
        "best_annualized_relative_return": best.get("annualized_relative_return", math.nan),
        "best_sample_strength": best.get("sample_strength", 0.0),
        "best_samples": int(best.get("samples", 0)),
        "best_nonoverlap_samples": int(best.get("nonoverlap_samples", 0)),
        "best_oos_samples": int(best.get("oos_samples", 0)),
        "candidate_signal_count": candidate_count,
        "conditional_signal_count": conditional_count,
        "rejected_signal_count": rejected_count,
        "main_issues": "；".join(issues) if issues else "主要硬门槛通过",
        "next_action": next_action,
    }


def build_issues(row: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if safe_number(row.get("mean_relative_return")) <= 0:
        issues.append("全样本相对收益不为正")
    if safe_number(row.get("oos_mean_relative_return")) <= 0:
        issues.append("样本外相对收益不为正")
    if safe_number(row.get("nonoverlap_mean_relative_return")) <= 0:
        issues.append("非重叠相对收益不为正")
    if safe_number(row.get("bootstrap_ci_5")) <= 0:
        issues.append("bootstrap下沿不为正")
    if safe_number(row.get("bootstrap_probability_positive")) <= 0.60:
        issues.append("bootstrap正收益概率不足60%")
    if safe_number(row.get("relative_final_nav")) <= 1.0:
        issues.append("逐日相对净值不大于1")
    if safe_number(row.get("sample_strength")) < 0.50:
        issues.append("样本强度不足")
    return issues


def build_top_candidates(sensitivity: pd.DataFrame, rejection_log: pd.DataFrame, reviews: pd.DataFrame) -> pd.DataFrame:
    if sensitivity.empty:
        return pd.DataFrame()
    merged = sensitivity.merge(
        rejection_log[["iteration", "strategy_id", "top_n", "horizon", "signal_status", "rejection_reasons"]],
        on=["iteration", "strategy_id", "top_n", "horizon"],
        how="left",
    )
    review_cols = ["iteration", "review_status", "main_issues", "next_action"]
    merged = merged.merge(reviews[review_cols], on="iteration", how="left")
    rows: list[dict[str, Any]] = []
    for row in merged.sort_values("robust_score", ascending=False).head(30).to_dict("records"):
        rows.append(
            {
                "迭代": int(row["iteration"]),
                "策略": row["strategy_zh"],
                "TopN": int(row["top_n"]),
                "持有期": int(row["horizon"]),
                "状态": translate_status(row.get("signal_status", "")),
                "全样本相对收益": fmt_pct(row.get("mean_relative_return")),
                "样本外相对收益": fmt_pct(row.get("oos_mean_relative_return")),
                "非重叠相对收益": fmt_pct(row.get("nonoverlap_mean_relative_return")),
                "Bootstrap下沿": fmt_pct(row.get("bootstrap_ci_5")),
                "Bootstrap为正概率": fmt_pct(row.get("bootstrap_probability_positive")),
                "逐日相对净值": fmt_float(row.get("relative_final_nav"), 3),
                "年化相对收益": fmt_pct(row.get("annualized_relative_return")),
                "样本强度": fmt_pct(row.get("sample_strength")),
                "样本数": int(row.get("samples", 0)),
                "非重叠样本数": int(row.get("nonoverlap_samples", 0)),
                "审查结论": row.get("review_status", ""),
                "主要问题": row.get("main_issues", ""),
                "下一步": row.get("next_action", ""),
            }
        )
    return pd.DataFrame(rows)


def build_summary(
    *,
    current_summary: dict[str, Any],
    features: pd.DataFrame,
    event_backtest: pd.DataFrame,
    reviews: pd.DataFrame,
    top_candidates: pd.DataFrame,
    args: argparse.Namespace,
    horizons: list[int],
    top_ns: list[int],
) -> dict[str, Any]:
    candidate_count = int(reviews["candidate_signal_count"].sum()) if not reviews.empty else 0
    conditional_count = int(reviews["conditional_signal_count"].sum()) if not reviews.empty else 0
    rejected_count = int(reviews["rejected_signal_count"].sum()) if not reviews.empty else 0
    final_verdict = "research_only_no_alpha_promotion"
    if candidate_count > 0:
        final_verdict = "candidate_requires_manual_review"
    elif conditional_count > 0:
        final_verdict = "conditional_observation_only"
    return {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_boundary": "三轮迭代只验证 PIT 可得的行业指数价格、压力、质量代理；当前 PE/PB/股息率只用于 V2.4 当前解释，不进入历史回测。",
        "current_v24_snapshot_date": current_summary.get("valuation_snapshot_date", ""),
        "current_v24_pit_valuation_status": current_summary.get("pit_valuation_status", ""),
        "current_v24_candidate_count": current_summary.get("current_observation_candidates", 0),
        "date_start": date_to_str(features["trade_date"].min()) if not features.empty else "",
        "date_end": date_to_str(features["trade_date"].max()) if not features.empty else "",
        "feature_rows": int(len(features)),
        "event_rows": int(len(event_backtest)),
        "iteration_count": int(len(reviews)),
        "candidate_signal_count": candidate_count,
        "conditional_signal_count": conditional_count,
        "rejected_signal_count": rejected_count,
        "top_output_rows": int(len(top_candidates)),
        "horizons": horizons,
        "top_ns": top_ns,
        "cost_bps": args.cost_bps,
        "bootstrap_rounds": args.bootstrap_rounds,
        "final_verdict": final_verdict,
    }


def render_report(
    *,
    summary: dict[str, Any],
    current_summary: dict[str, Any],
    reviews: pd.DataFrame,
    top_candidates: pd.DataFrame,
    sensitivity: pd.DataFrame,
    rejections: pd.DataFrame,
) -> str:
    lines: list[str] = [
        "# V2.4 三轮迭代回测与审查报告",
        "",
        f"版本：{summary['version']}",
        "",
        "## 研究边界",
        "",
        "本报告把“跑回测 -> 审查报告 -> 根据审查结果推进下一轮”固化为标准流程，并连续执行三轮。",
        "",
        "关键限制：当前 PE、PB、股息率只有当前快照，不能回填到历史。三轮回测只使用 PIT 可得的行业指数价格、压力、成交额和价格质量代理。V2.4 当前估值只用于解释当前候选，不构成历史 alpha 证据。",
        "",
        "## V2.4 当前快照上下文",
        "",
        f"- 当前估值快照日期：{current_summary.get('valuation_snapshot_date', '')}",
        f"- 当前估值覆盖：{current_summary.get('valuation_covered_rows', 0)} / {current_summary.get('current_industry_rows', 0)}",
        f"- 估值快照数量：{current_summary.get('valuation_snapshot_count', 0)}",
        f"- PIT估值状态：{translate_pit_status(current_summary.get('pit_valuation_status', ''))}",
        f"- 当前快照候选数：{current_summary.get('current_observation_candidates', 0)}",
        f"- 当前市场压力：{current_summary.get('current_pressure_tier', '')}（{fmt_float(current_summary.get('current_market_stress_score'), 3)}）",
        "",
        "## 三轮审查结论",
        "",
    ]
    lines.extend(render_review_table(reviews))
    lines.extend(
        [
            "",
            "## 组合排序结果",
            "",
        ]
    )
    lines.extend(render_markdown_table(top_candidates.head(15)))
    lines.extend(["", "## 分轮审查", ""])
    for row in reviews.to_dict("records"):
        lines.extend(
            [
                f"### {row.get('iteration_zh', '')}",
                "",
                f"- 输入理由：{row.get('review_input_reason', '')}",
                f"- 最优组合：{row.get('best_strategy', '')}，Top{int(row.get('best_top_n', 0))}，持有 {int(row.get('best_horizon', 0))} 日。",
                f"- 全样本相对收益：{fmt_pct(row.get('best_mean_relative_return'))}",
                f"- 样本外相对收益：{fmt_pct(row.get('best_oos_mean_relative_return'))}",
                f"- 非重叠相对收益：{fmt_pct(row.get('best_nonoverlap_mean_relative_return'))}",
                f"- Bootstrap 5% 下沿：{fmt_pct(row.get('best_bootstrap_ci_5'))}",
                f"- 逐日相对净值：{fmt_float(row.get('best_relative_final_nav'), 3)}",
                f"- 样本强度：{fmt_pct(row.get('best_sample_strength'))}",
                f"- 审查结论：{row.get('review_status', '')}",
                f"- 主要问题：{row.get('main_issues', '')}",
                f"- 下一步动作：{row.get('next_action', '')}",
                "",
            ]
        )
    lines.extend(
        [
            "## 最终判断",
            "",
            "三轮迭代没有把 V2.4 历史代理信号升级为 alpha。若某些组合全样本、样本外或非重叠收益为正，也仍受样本强度、bootstrap 下沿、逐日相对净值或 OOS 稳定性约束，不能作为独立策略。",
            "",
            "下一阶段不应继续靠加参数挤出结果。更合理的方向是积累每日行业估值快照，或接入可靠的历史行业估值数据源，然后再做真正的“估值 + 压力 + 质量”PIT 验证。",
            "",
            "## 复现文件",
            "",
            "- `debug/iteration_signal_panel.csv`",
            "- `debug/event_backtest.csv`",
            "- `debug/nonoverlap_backtest.csv`",
            "- `debug/walk_forward_oos.csv`",
            "- `debug/bootstrap_confidence.csv`",
            "- `debug/daily_portfolio_nav.csv`",
            "- `debug/portfolio_nav_metrics.csv`",
            "- `debug/parameter_sensitivity.csv`",
            "- `debug/signal_rejection_log.csv`",
            "- `debug/iteration_reviews.csv`",
            "- `debug/iteration_configs.json`",
        ]
    )
    return "\n".join(lines)


def render_review_table(reviews: pd.DataFrame) -> list[str]:
    if reviews.empty:
        return ["无审查结果。"]
    rows = [
        "| 迭代 | 审查结论 | 最优组合 | 全样本相对 | 样本外相对 | 非重叠相对 | Bootstrap下沿 | 逐日相对净值 | 样本强度 | 主要问题 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in reviews.to_dict("records"):
        combo = f"{row.get('best_strategy', '')} Top{int(row.get('best_top_n', 0))}/{int(row.get('best_horizon', 0))}日"
        rows.append(
            "| "
            + " | ".join(
                [
                    str(int(row.get("iteration", 0))),
                    str(row.get("review_status", "")),
                    combo,
                    fmt_pct(row.get("best_mean_relative_return")),
                    fmt_pct(row.get("best_oos_mean_relative_return")),
                    fmt_pct(row.get("best_nonoverlap_mean_relative_return")),
                    fmt_pct(row.get("best_bootstrap_ci_5")),
                    fmt_float(row.get("best_relative_final_nav"), 3),
                    fmt_pct(row.get("best_sample_strength")),
                    str(row.get("main_issues", "")),
                ]
            )
            + " |"
        )
    return rows


def build_config_output(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "iteration_count": len(reviews),
        "research_boundary": "Iteration configs are based on PIT price/pressure/quality proxies only; current valuation is excluded from historical features.",
        "iterations": [
            {
                key: review.get(key)
                for key in [
                    "iteration",
                    "iteration_zh",
                    "strategy_id",
                    "score_variant",
                    "stress_threshold",
                    "quality_min",
                    "liquidity_min",
                    "recovery_min",
                    "low_vol_min",
                    "drawdown_quality_min",
                    "oversold_min",
                    "oversold_max",
                    "max_trap_score",
                    "review_status",
                    "main_issues",
                    "next_action",
                ]
            }
            for review in reviews
        ],
    }


def config_review_fields(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "iteration": int(config["iteration"]),
        "iteration_zh": config["iteration_zh"],
        "strategy_id": config["strategy_id"],
        "strategy_zh": config["strategy_zh"],
        "score_variant": config["score_variant"],
        "review_input_reason": config["review_input_reason"],
        "stress_threshold": config["stress_threshold"],
        "quality_min": config["quality_min"],
        "liquidity_min": config["liquidity_min"],
        "recovery_min": config["recovery_min"],
        "low_vol_min": config["low_vol_min"],
        "drawdown_quality_min": config["drawdown_quality_min"],
        "oversold_min": config["oversold_min"],
        "oversold_max": config["oversold_max"],
        "max_trap_score": config["max_trap_score"],
    }


def add_iteration_metadata(frame: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    out = frame.copy()
    out["iteration"] = int(config["iteration"])
    out["iteration_zh"] = str(config["iteration_zh"])
    out["iteration_strategy_id"] = str(config["strategy_id"])
    out["iteration_score_variant"] = str(config["score_variant"])
    return out


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def build_next_reason(review: dict[str, Any], source_round: str, action: str) -> str:
    return f"{source_round}审查结论为“{review.get('review_status', '')}”，主要问题：{review.get('main_issues', '')}。{action}"


def parse_int_list(value: str) -> list[int]:
    return [int(item.strip()) for item in str(value).split(",") if item.strip()]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    def default(obj: Any) -> Any:
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, pd.Timestamp):
            return obj.strftime("%Y-%m-%d")
        return str(obj)

    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=default)


def render_markdown_table(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    columns = list(frame.columns)
    rows = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for record in frame.to_dict("records"):
        rows.append("| " + " | ".join(str(record.get(column, "")) for column in columns) + " |")
    return rows


def translate_status(status: Any) -> str:
    return {
        "candidate_signal": "候选待复核",
        "conditional_only_signal": "条件观察",
        "rejected_standalone_signal": "拒绝升级",
    }.get(str(status), str(status))


def translate_pit_status(status: Any) -> str:
    return {
        "pit_ready": "PIT估值样本就绪",
        "current_snapshot_only_not_pit": "仅当前快照，未PIT验证",
    }.get(str(status), str(status))


def date_to_str(value: Any) -> str:
    if pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def safe_number(value: Any) -> float:
    try:
        if value is None or str(value).strip() == "":
            return 0.0
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) else number


def fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(number):
        return ""
    return f"{number * 100:.2f}%"


def fmt_float(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(number):
        return ""
    return f"{number:.{digits}f}"


if __name__ == "__main__":
    main()
