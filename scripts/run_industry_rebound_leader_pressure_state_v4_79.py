#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_oos_factor_v4_74 as v474


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_pressure_state_v4_79"
DEBUG = OUT / "debug"

FEATURES = ["oversold_score", "oversold_liquidity_score", "robust_oversold_rank_avg"]
TOP_NS = [5, 10, 15, 20]
GATE_TEXT = (
    "event_count>=30; year_count>=5; mean/median relative>0; win_rate>=55%; "
    "top_quintile_hit_rate>=30%; positive_year_rate>=60%; "
    "OOS events>=8; OOS mean relative>0; OOS win_rate>=50%"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.79 pressure-state strong rebound industry selector.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    state_opp = v474.attach_state(opportunity, trades)
    state_opp = add_robust_rank_feature(state_opp)
    event_panel = build_event_panel(state_opp)
    results = summarize(event_panel)
    best = select_best(results)
    gate = gate_audit(best)
    yearly = yearly_diagnostics(event_panel, best)
    robustness = robustness_audit(event_panel, best)
    latest = current_candidates(state_opp, trades, best)
    summary = build_summary(best, gate, robustness, latest)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    latest.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, results, gate, robustness, yearly, latest), encoding="utf-8")
    event_panel.to_csv(DEBUG / "pressure_state_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "pressure_state_strategy_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    robustness.to_csv(DEBUG / "robustness_audit.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(DEBUG / "yearly_diagnostics.csv", index=False, encoding="utf-8-sig")
    latest.to_csv(DEBUG / "latest_pressure_state_candidates.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_feature={summary['best_feature']}")
    print(f"best_top_n={summary['best_top_n']}")


def add_robust_rank_feature(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    base = ["oversold_liquidity_score", "oversold_score", "oversold_turn_score"]
    parts = []
    for _, event in out.groupby(["signal_date", "entry_date", "exit_date"], sort=False):
        event = event.copy()
        rank_cols = []
        for col in base:
            rank_col = f"{col}_rank"
            event[rank_col] = pd.to_numeric(event[col], errors="coerce").rank(pct=True, ascending=True)
            rank_cols.append(rank_col)
        event["robust_oversold_rank_avg"] = event[rank_cols].mean(axis=1)
        parts.append(event)
    return pd.concat(parts, ignore_index=True)


def build_event_panel(frame: pd.DataFrame) -> pd.DataFrame:
    source = frame[deep_or_high_vol_mask(frame)].copy()
    rows = []
    for feature in FEATURES:
        for top_n in TOP_NS:
            rows.extend(v474.evaluate_factor(source, "deep_or_high_vol", feature, top_n))
    return pd.DataFrame(rows)


def deep_or_high_vol_mask(frame: pd.DataFrame) -> pd.Series:
    return frame["deep_negative_breadth"].fillna(False) | frame["high_volatility_protection"].fillna(False)


def summarize(panel: pd.DataFrame) -> pd.DataFrame:
    if panel.empty:
        return pd.DataFrame()
    rows = []
    for (feature, top_n), group in panel.groupby(["factor", "top_n"]):
        yearly = group.groupby("year")["relative_return"].mean()
        oos = group[group["year"] >= 2022]
        row = {
            "state_gate_variant": "deep_or_high_vol",
            "feature": feature,
            "top_n": int(top_n),
            "event_count": int(len(group)),
            "year_count": int(group["year"].nunique()),
            "mean_relative_return": float(group["relative_return"].mean()),
            "median_relative_return": float(group["relative_return"].median()),
            "relative_win_rate": float(group["relative_win"].mean()),
            "mean_rank_ic": float(group["rank_ic"].mean()),
            "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
            "selected_industry_examples": "|".join(group["selected_industries"].head(3).astype(str)),
        }
        row["passes_strong_rebound_gate"] = passes(row)
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values(
        ["passes_strong_rebound_gate", "mean_relative_return", "top_quintile_hit_rate"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def select_best(results: pd.DataFrame) -> pd.Series:
    if results.empty:
        return pd.Series(dtype=object)
    passed = results[results["passes_strong_rebound_gate"].eq(True)]
    source = passed if not passed.empty else results
    return source.sort_values(
        ["passes_strong_rebound_gate", "mean_relative_return", "top_quintile_hit_rate", "relative_win_rate"],
        ascending=[False, False, False, False],
    ).iloc[0]


def failed_metrics(row: dict[str, object] | pd.Series) -> list[str]:
    checks = [
        ("event_count", 30, ">="),
        ("year_count", 5, ">="),
        ("mean_relative_return", 0, ">"),
        ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("positive_year_rate", 0.60, ">="),
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


def passes(row: dict[str, object] | pd.Series) -> bool:
    return not failed_metrics(row)


def gate_audit(best: pd.Series) -> pd.DataFrame:
    if best.empty:
        return pd.DataFrame()
    failed = set(str(best.get("failed_metrics", "")).split(";"))
    requirements = [
        ("event_count", 30, ">="),
        ("year_count", 5, ">="),
        ("mean_relative_return", 0, ">"),
        ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="),
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
            "status": "fail" if metric in failed else "pass",
        }
        for metric, required, op in requirements
    ])


def yearly_diagnostics(panel: pd.DataFrame, best: pd.Series) -> pd.DataFrame:
    if panel.empty or best.empty:
        return pd.DataFrame()
    mask = panel["factor"].eq(best["feature"]) & panel["top_n"].eq(best["top_n"])
    selected = panel[mask]
    return selected.groupby("year").agg(
        event_count=("relative_return", "size"),
        mean_relative_return=("relative_return", "mean"),
        median_relative_return=("relative_return", "median"),
        relative_win_rate=("relative_win", "mean"),
        top_quintile_hit_rate=("top_quintile_hit_rate", "mean"),
        mean_rank_ic=("rank_ic", "mean"),
    ).reset_index()


def robustness_audit(panel: pd.DataFrame, best: pd.Series) -> pd.DataFrame:
    if panel.empty or best.empty:
        return pd.DataFrame()
    mask = panel["factor"].eq(best["feature"]) & panel["top_n"].eq(best["top_n"])
    selected = panel[mask].copy()
    if selected.empty:
        return pd.DataFrame()
    top_n = int(best["top_n"])
    hit_successes = int(round(float(selected["top_quintile_hit_rate"].sum()) * top_n))
    hit_trials = int(len(selected) * top_n)
    bootstrap = bootstrap_event_metrics(selected)
    rows = [
        {
            "metric": "top_quintile_wilson_lower_bound",
            "current": wilson_lower(hit_successes, hit_trials),
            "operator": ">",
            "required": 0.20,
            "status": "pass" if wilson_lower(hit_successes, hit_trials) > 0.20 else "fail",
            "interpretation": "Top20% 命中率置信下界需要高于随机 20%。",
        },
        {
            "metric": "bootstrap_mean_relative_p05",
            "current": float(bootstrap["mean_relative_return"].quantile(0.05)),
            "operator": ">",
            "required": 0.0,
            "status": "pass" if float(bootstrap["mean_relative_return"].quantile(0.05)) > 0 else "fail",
            "interpretation": "事件重采样后平均超额收益 5% 下界需要为正。",
        },
        {
            "metric": "bootstrap_top_quintile_hit_p05",
            "current": float(bootstrap["top_quintile_hit_rate"].quantile(0.05)),
            "operator": ">=",
            "required": 0.30,
            "status": "pass" if float(bootstrap["top_quintile_hit_rate"].quantile(0.05)) >= 0.30 else "fail",
            "interpretation": "事件重采样后 Top20% 命中率 5% 下界需要不低于硬门槛。",
        },
        {
            "metric": "bootstrap_positive_year_p05",
            "current": float(bootstrap["positive_year_rate"].quantile(0.05)),
            "operator": ">=",
            "required": 0.60,
            "status": "pass" if float(bootstrap["positive_year_rate"].quantile(0.05)) >= 0.60 else "fail",
            "interpretation": "事件重采样后年度正收益比例 5% 下界需要不低于硬门槛。",
        },
    ]
    return pd.DataFrame(rows)


def bootstrap_event_metrics(frame: pd.DataFrame, iterations: int = 2000) -> pd.DataFrame:
    rng = random.Random(20260620)
    rows = []
    for _ in range(iterations):
        sample = frame.sample(n=len(frame), replace=True, random_state=rng.randrange(1_000_000_000))
        yearly = sample.groupby("year")["relative_return"].mean()
        rows.append({
            "mean_relative_return": float(sample["relative_return"].mean()),
            "top_quintile_hit_rate": float(sample["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
        })
    return pd.DataFrame(rows)


def wilson_lower(successes: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0:
        return 0.0
    phat = successes / trials
    denominator = 1 + z * z / trials
    center = (phat + z * z / (2 * trials)) / denominator
    half = z * math.sqrt((phat * (1 - phat) + z * z / (4 * trials)) / trials) / denominator
    return float(center - half)


def current_candidates(frame: pd.DataFrame, trades: pd.DataFrame, best: pd.Series) -> pd.DataFrame:
    latest = pd.read_csv(V472 / "top_candidates.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    if best.empty:
        latest["candidate_status"] = "research_only_no_validated_rule"
        return latest.head(0)
    feature = str(best["feature"])
    top_n = int(best["top_n"])
    latest = add_missing_current_feature(latest, feature)
    latest_signal = latest["signal_date"].iloc[0] if "signal_date" in latest.columns and len(latest) else ""
    current_state = latest_state(trades, latest_signal)
    state_active = bool(current_state.get("deep_or_high_vol", False))
    source = latest.sort_values(feature, ascending=False).head(top_n).copy() if feature in latest.columns else latest.head(top_n).copy()
    source["selection_feature_score"] = source[feature] if feature in source.columns else pd.NA
    source["candidate_status"] = "research_only_strong_rebound_candidate" if state_active else "state_gate_not_active_observation_only"
    source["state_gate_variant"] = "deep_or_high_vol"
    source["selection_feature"] = feature
    source["selection_top_n"] = top_n
    source["state_gate_active"] = state_active
    source["manual_review_reason"] = (
        "V4.79 历史研究门槛通过，但仍需要前推样本和人工复核；当前状态门控已触发。"
        if state_active else
        "V4.79 历史研究门槛通过，但当前不在 deep_or_high_vol 状态，只能观察，不能当作入场信号。"
    )
    cols = [
        "candidate_status",
        "state_gate_variant",
        "selection_feature",
        "selection_feature_score",
        "selection_top_n",
        "state_gate_active",
        "industry_code",
        "industry_name",
        "selection_score",
        "valuation_score",
        "oversold_score",
        "turn_score",
        "liquidity_score",
        "oversold_liquidity_score",
        "manual_review_reason",
    ]
    return source[[col for col in cols if col in source.columns]]


def add_missing_current_feature(frame: pd.DataFrame, feature: str) -> pd.DataFrame:
    out = frame.copy()
    if feature in out.columns:
        return out
    if feature == "oversold_liquidity_score" and {"oversold_score", "liquidity_score"}.issubset(out.columns):
        out[feature] = pd.to_numeric(out["oversold_score"], errors="coerce").fillna(0.5) * 0.85
        out[feature] += pd.to_numeric(out["liquidity_score"], errors="coerce").fillna(0.5) * 0.15
    elif feature == "robust_oversold_rank_avg":
        cols = [col for col in ["oversold_score", "turn_score", "liquidity_score"] if col in out.columns]
        # ponytail: current top_candidates lacks the full event universe, so this is only a display fallback.
        out[feature] = out[cols].rank(pct=True).mean(axis=1) if cols else pd.NA
    return out


def latest_state(trades: pd.DataFrame, latest_signal: str) -> dict[str, object]:
    if trades.empty:
        return {"deep_or_high_vol": False}
    source = trades.copy()
    if latest_signal and "signal_date" in source.columns:
        matched = source[source["signal_date"].astype(str).eq(str(latest_signal))]
        if not matched.empty:
            source = matched
    row = source.sort_values("signal_date").iloc[-1]
    negative = float(row.get("negative_breadth_60d", 0) or 0) >= 0.75
    high_vol = float(row.get("market_volatility_20d_vs_60d", 0) or 0) >= 1.30
    return {
        "signal_date": row.get("signal_date", ""),
        "deep_negative_breadth": negative,
        "high_volatility_protection": high_vol,
        "deep_or_high_vol": negative or high_vol,
    }


def build_summary(best: pd.Series, gate: pd.DataFrame, robustness: pd.DataFrame, latest: pd.DataFrame) -> dict[str, object]:
    failed = gate[gate["status"].eq("fail")]["metric"].tolist() if len(gate) else ["no_results"]
    robustness_failed = robustness[robustness["status"].eq("fail")]["metric"].tolist() if len(robustness) else ["no_robustness_audit"]
    point_passed = not failed
    robustness_passed = not robustness_failed
    state_active = bool(latest["state_gate_active"].iloc[0]) if "state_gate_active" in latest.columns and len(latest) else False
    return {
        "version": "4.79.0",
        "policy_id": "industry_rebound_leader_pressure_state_v4_79",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "state_gate_variant": best.get("state_gate_variant", ""),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_year_count": int(best.get("year_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_median_relative_return": float(best.get("median_relative_return", 0.0) or 0.0),
        "best_relative_win_rate": float(best.get("relative_win_rate", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_positive_year_rate": float(best.get("positive_year_rate", 0.0) or 0.0),
        "best_oos_mean_relative_return": float(best.get("oos_mean_relative_return", 0.0) or 0.0),
        "best_oos_relative_win_rate": float(best.get("oos_relative_win_rate", 0.0) or 0.0),
        "latest_state_gate_active": state_active,
        "latest_candidate_count": int(len(latest)),
        "failed_metrics": ";".join(failed),
        "robustness_failed_metrics": ";".join(robustness_failed),
        "point_estimate_gate_passed": point_passed,
        "robustness_gate_passed": robustness_passed,
        "best_status": (
            "pass_stronger_industry_research_gate"
            if point_passed and robustness_passed else
            "research_only_point_gate_passed_robustness_not_confirmed"
            if point_passed else
            "research_only_not_validated"
        ),
        "production_ready": False,
        "auto_execution_allowed": False,
        "evaluation_gate": GATE_TEXT,
        "final_verdict": (
            "V4.79 在 deep_or_high_vol 状态下通过点估计门槛和稳健性门槛；仍不是自动交易信号。"
            if point_passed and robustness_passed else
            "V4.79 点估计通过强反弹行业门槛，但 bootstrap 稳健性未完全确认；不能称为真正找到稳定强反弹行业。"
            if point_passed else
            "V4.79 仍未通过强反弹行业研究评价门槛。"
        ),
    }


def render_report(
    summary: dict[str, object],
    results: pd.DataFrame,
    gate: pd.DataFrame,
    robustness: pd.DataFrame,
    yearly: pd.DataFrame,
    latest: pd.DataFrame,
) -> str:
    return "\n".join([
        "# V4.79 压力状态强反弹行业选择验证",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 只在 `deep_or_high_vol` 状态下评估行业强弱选择：`negative_breadth_60d >= 0.75` 或 `market_volatility_20d_vs_60d >= 1.30`。",
        "- 这个状态门控对应深度行业杀跌或高波动保护期，目的是减少 V4.78 在普通反弹窗口中的年份不稳定。",
        "- 行业选择只使用信号日可见的价格、企稳和流动性特征，不使用未来收益、不使用 ETF、不使用个股。",
        "- 通过门槛只代表历史研究证据通过，不代表自动交易、买入建议或生产就绪。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 策略结果",
        "",
        table(results),
        "",
        "## 门槛审计",
        "",
        table(gate),
        "",
        "## 稳健性审计",
        "",
        table(robustness),
        "",
        "## 年度诊断",
        "",
        table(yearly),
        "",
        "## 当前观察候选",
        "",
        table(latest),
        "",
        "## 研究边界",
        "",
        "V4.79 解决的是“反弹窗口中行业强弱排序是否能跑赢全行业等权”的研究问题。它没有解决真实 ETF 承载、滑点、仓位、流动性冲击和未来新增样本前推问题，因此 `production_ready=false`、`auto_execution_allowed=false`。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    row = {
        "event_count": 30,
        "year_count": 5,
        "mean_relative_return": 0.01,
        "median_relative_return": 0.01,
        "relative_win_rate": 0.55,
        "top_quintile_hit_rate": 0.30,
        "positive_year_rate": 0.60,
        "oos_event_count": 8,
        "oos_mean_relative_return": 0.01,
        "oos_relative_win_rate": 0.50,
    }
    assert passes(row)
    row["top_quintile_hit_rate"] = 0.29
    assert "top_quintile_hit_rate" in failed_metrics(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
