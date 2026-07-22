#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from valuation_pit_contract import (
        CURRENT_SNAPSHOT_STATUS,
        attach_pit_valuation_asof,
        audit_pit_valuation_history,
        official_valuation_history,
    )
except ModuleNotFoundError:  # package-style imports in tests and audits
    from scripts.valuation_pit_contract import (
        CURRENT_SNAPSHOT_STATUS,
        attach_pit_valuation_asof,
        audit_pit_valuation_history,
        official_valuation_history,
    )


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
DEBUG = OUT / "debug"
VALUATION_HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"
SNAPSHOT_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_snapshots" / "second"
HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
V471_SUMMARY = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "run_summary.json"

STRATEGIES = {
    "value_oversold_turn": {"valuation": 0.35, "oversold": 0.30, "turn": 0.25, "liquidity": 0.10},
    "oversold_turn": {"valuation": 0.00, "oversold": 0.50, "turn": 0.40, "liquidity": 0.10},
    "oversold_liquidity": {"valuation": 0.00, "oversold": 0.85, "turn": 0.00, "liquidity": 0.15},
    "value_only": {"valuation": 0.80, "oversold": 0.00, "turn": 0.00, "liquidity": 0.20},
    "turn_only": {"valuation": 0.00, "oversold": 0.00, "turn": 0.80, "liquidity": 0.20},
}

FACTOR_DISCOVERY = [
    ("valuation_score", "估值综合分", True),
    ("oversold_score", "超跌综合分", True),
    ("turn_score", "企稳反转分", True),
    ("liquidity_score", "流动性分", True),
    ("return_5d", "5日动量", True),
    ("return_20d", "20日动量", True),
    ("return_60d", "60日跌幅", False),
    ("return_120d", "120日跌幅", False),
    ("drawdown_252d", "252日回撤", False),
    ("relative_return_20d", "20日相对强度", True),
    ("relative_return_60d", "60日相对强度", True),
    ("rebound_acceleration_20_60", "20/60日跌幅收敛", True),
    ("rebound_acceleration_20_120", "20/120日跌幅收敛", True),
    ("amount_share_change_20d", "20日成交占比变化", True),
    ("turnover_change_20d", "20日换手变化", True),
    ("pb_change_60d", "60日PB变化", True),
    ("pe_change_60d", "60日PE变化", True),
    ("dividend_yield_change_60d", "60日股息率变化", True),
    ("pe", "低PE", False),
    ("pb", "低PB", False),
    ("dividend_yield", "股息率", True),
]

STRUCTURE_FACTOR_FIELDS = {
    "relative_return_20d",
    "relative_return_60d",
    "rebound_acceleration_20_60",
    "rebound_acceleration_20_120",
    "amount_share_change_20d",
    "turnover_change_20d",
    "pb_change_60d",
    "pe_change_60d",
    "dividend_yield_change_60d",
}
LATEST_CANDIDATE_COLUMNS = [
    "candidate_status",
    "selection_strategy",
    "planned_entry_date",
    "feature_date",
    "signal_date",
    "price_date",
    "price_stale_days",
    "candidate_source",
    "trade_date",
    "industry_code",
    "industry_name",
    "selection_score",
    "valuation_score",
    "oversold_score",
    "turn_score",
    "liquidity_score",
    "return_20d",
    "return_60d",
    "return_120d",
    "drawdown_252d",
    "pe",
    "pb",
    "dividend_yield",
]
CORE_CANDIDATE_VALUATION_FIELDS = ("pe", "pb", "dividend_yield")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate industry selection inside rebound windows.")
    parser.add_argument("--top-n", default="5,10,20")
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    top_ns = [int(x) for x in args.top_n.split(",") if x.strip()]
    features = build_features(load_history(VALUATION_HISTORY))
    valuation_data_status = str(features["valuation_data_status"].iloc[0]) if len(features) else "blocked_empty_history"
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    event_panel = evaluate_events(features, trades, top_ns, args.cost_bps)
    opportunity_set = build_event_opportunity_set(features, trades)
    strategy_results = summarize_strategies(event_panel)
    factor_events = evaluate_factor_discovery(features, trades, top_ns, args.cost_bps)
    factor_results = summarize_factor_discovery(factor_events)
    best_strategy = str(strategy_results.iloc[0]["strategy"]) if len(strategy_results) else "oversold_liquidity"
    best_top_n = int(strategy_results.iloc[0]["top_n"]) if len(strategy_results) else 10
    asof_filter_events = evaluate_asof_failure_filter(features, trades, best_strategy, best_top_n, args.cost_bps)
    asof_filter_summary = summarize_asof_failure_filter(asof_filter_events)
    annual = annual_breakdown(event_panel)
    latest = latest_candidates(features, strategy_results)
    gate_audit = evaluation_gate_audit(strategy_results)
    diagnosis = failure_diagnosis(event_panel, strategy_results, annual, gate_audit, trades)
    evidence_debt = industry_leader_evidence_debt(gate_audit, diagnosis)
    latest = add_candidate_risk_flags(latest, diagnosis)
    carrier_mapping = candidate_carrier_mapping(latest)
    carrier_audit = carrier_mapping_audit(latest, carrier_mapping)
    exposure_audit = carrier_exposure_audit(latest, carrier_mapping)
    tracking_audit = carrier_tracking_audit(exposure_audit)
    pre_trade = pre_trade_review_sheet(latest, strategy_results, carrier_mapping, tracking_audit)
    timing_audit = feature_timing_audit(features, latest, event_panel, opportunity_set)
    summary = build_summary(strategy_results, event_panel, latest, carrier_audit, exposure_audit, tracking_audit, asof_filter_summary, factor_results, valuation_data_status)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    latest.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, strategy_results, annual, latest, gate_audit, evidence_debt, diagnosis, asof_filter_summary, factor_results, carrier_audit, exposure_audit, tracking_audit, pre_trade), encoding="utf-8")
    event_panel.to_csv(DEBUG / "industry_event_panel.csv", index=False, encoding="utf-8-sig")
    opportunity_set.to_csv(DEBUG / "industry_event_opportunity_set.csv", index=False, encoding="utf-8-sig")
    strategy_results.to_csv(DEBUG / "strategy_results.csv", index=False, encoding="utf-8-sig")
    factor_events.to_csv(DEBUG / "factor_discovery_events.csv", index=False, encoding="utf-8-sig")
    factor_results.to_csv(DEBUG / "factor_discovery_results.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(DEBUG / "annual_breakdown.csv", index=False, encoding="utf-8-sig")
    latest.to_csv(DEBUG / "latest_rebound_leader_candidates.csv", index=False, encoding="utf-8-sig")
    gate_audit.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    evidence_debt.to_csv(DEBUG / "industry_leader_evidence_debt.csv", index=False, encoding="utf-8-sig")
    asof_filter_events.to_csv(DEBUG / "asof_failure_filter_events.csv", index=False, encoding="utf-8-sig")
    asof_filter_summary.to_csv(DEBUG / "asof_failure_filter_sensitivity.csv", index=False, encoding="utf-8-sig")
    diagnosis.to_csv(DEBUG / "failure_diagnosis.csv", index=False, encoding="utf-8-sig")
    carrier_mapping.to_csv(DEBUG / "industry_candidate_carrier_mapping.csv", index=False, encoding="utf-8-sig")
    carrier_audit.to_csv(DEBUG / "carrier_mapping_audit.csv", index=False, encoding="utf-8-sig")
    exposure_audit.to_csv(DEBUG / "carrier_exposure_audit.csv", index=False, encoding="utf-8-sig")
    tracking_audit.to_csv(DEBUG / "carrier_tracking_audit.csv", index=False, encoding="utf-8-sig")
    pre_trade.to_csv(DEBUG / "pre_trade_manual_review_sheet.csv", index=False, encoding="utf-8-sig")
    timing_audit.to_csv(DEBUG / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_strategy={summary['best_strategy']}")


def load_history(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    # Recovered V2.5 component rows are current-snapshot reconstructions, not
    # official daily history.  Remove them before choosing any feature date.
    df = official_valuation_history(raw)
    df["industry_code"] = df["industry_code"].str.zfill(6)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    audit = audit_pit_valuation_history(df)
    valuation_columns = ["pe", "pb", "dividend_yield"]
    if not audit.eligible:
        # The same file remains useful as a price/turnover history.  Valuation
        # fields are neutralised so they cannot leak into historical rankings.
        for column in valuation_columns:
            if column in df.columns:
                df[column] = float("nan")
        df["valuation_data_status"] = "blocked_non_pit_valuation_history"
        df["valuation_block_reason"] = "; ".join(audit.errors)
        return df.sort_values(["industry_code", "trade_date"]).reset_index(drop=True)

    left = df.drop(
        columns=valuation_columns
        + [
            "published_at",
            "available_date",
            "fetched_at",
            "source_version",
            "source_hash",
            "revision_status",
            "availability_basis",
            "data_status",
            "pit_eligible",
        ],
        errors="ignore",
    )
    joined = attach_pit_valuation_asof(left, df, decision_date_column="trade_date")
    joined["valuation_data_status"] = "pit_verified_asof"
    joined["valuation_block_reason"] = ""
    return joined.sort_values(["industry_code", "trade_date"]).reset_index(drop=True)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("industry_code", group_keys=False)
    out = df.copy()
    for n in [5, 20, 60, 120]:
        out[f"return_{n}d"] = g["close_index"].pct_change(n)
    out["drawdown_252d"] = out["close_index"] / g["close_index"].rolling(252, min_periods=60).max().reset_index(level=0, drop=True) - 1.0
    out["relative_return_20d"] = out["return_20d"] - out.groupby("trade_date")["return_20d"].transform("mean")
    out["relative_return_60d"] = out["return_60d"] - out.groupby("trade_date")["return_60d"].transform("mean")
    out["rebound_acceleration_20_60"] = out["return_20d"] - out["return_60d"] / 3.0
    out["rebound_acceleration_20_120"] = out["return_20d"] - out["return_120d"] / 6.0
    out["amount_share_change_20d"] = out["amount_share_pct"] - g["amount_share_pct"].shift(20)
    out["turnover_change_20d"] = out["turnover_rate"] - g["turnover_rate"].shift(20)
    out["pb_change_60d"] = out["pb"] - g["pb"].shift(60)
    out["pe_change_60d"] = out["pe"] - g["pe"].shift(60)
    out["dividend_yield_change_60d"] = out["dividend_yield"] - g["dividend_yield"].shift(60)
    out["valuation_score"] = score_by_date(out, [("pe", False), ("pb", False), ("dividend_yield", True)])
    out["oversold_score"] = score_by_date(out, [("return_60d", False), ("return_120d", False), ("drawdown_252d", False)])
    out["turn_score"] = score_by_date(out, [("return_5d", True), ("return_20d", True)])
    out["liquidity_score"] = score_by_date(out, [("amount_share_pct", True)])
    for strategy, weights in STRATEGIES.items():
        out[f"{strategy}_score"] = sum(out[f"{k}_score"].fillna(0.5) * v for k, v in weights.items())
    return out


def score_by_date(df: pd.DataFrame, fields: list[tuple[str, bool]]) -> pd.Series:
    pieces = []
    for field, higher_is_better in fields:
        values = pd.to_numeric(df[field], errors="coerce")
        if field in {"pe", "pb"}:
            values = values.where(values > 0)
        rank = values.groupby(df["trade_date"]).rank(pct=True, ascending=higher_is_better)
        pieces.append(rank)
    return pd.concat(pieces, axis=1).mean(axis=1)


def evaluate_events(features: pd.DataFrame, trades: pd.DataFrame, top_ns: list[int], cost_bps: float) -> pd.DataFrame:
    rows = []
    by_date = {d: x.copy() for d, x in features.groupby("trade_date")}
    close = features.pivot(index="trade_date", columns="industry_code", values="close_index")
    names = features.sort_values("trade_date").drop_duplicates("industry_code", keep="last").set_index("industry_code")["industry_name"].to_dict()
    for _, trade in trades.iterrows():
        signal_date = pd.to_datetime(trade["signal_date"])
        entry_date = pd.to_datetime(trade["entry_date"])
        exit_date = pd.to_datetime(trade["exit_date"])
        if signal_date not in by_date or entry_date not in close.index or exit_date not in close.index:
            continue
        returns = (close.loc[exit_date] / close.loc[entry_date] - 1.0).dropna()
        frame = by_date[signal_date]
        frame = frame[frame["industry_code"].isin(returns.index)].copy()
        if frame.empty:
            continue
        frame["future_return"] = frame["industry_code"].map(returns)
        benchmark = float(frame["future_return"].mean())
        for strategy in STRATEGIES:
            score_col = f"{strategy}_score"
            ranked = frame.sort_values(score_col, ascending=False)
            rank_ic = float(frame[[score_col, "future_return"]].corr(method="spearman").iloc[0, 1])
            for top_n in top_ns:
                selected = ranked.head(top_n)
                gross = float(selected["future_return"].mean())
                net = gross - cost_bps / 10000.0
                rel = net - benchmark
                top_quintile_cut = frame["future_return"].quantile(0.8)
                rows.append({
                    "signal_date": signal_date.strftime("%Y-%m-%d"),
                    "entry_date": entry_date.strftime("%Y-%m-%d"),
                    "exit_date": exit_date.strftime("%Y-%m-%d"),
                    "year": int(signal_date.year),
                    "strategy": strategy,
                    "top_n": top_n,
                    "selected_return": gross,
                    "selected_net_return": net,
                    "benchmark_return": benchmark,
                    "relative_return": rel,
                    "relative_win": rel > 0,
                    "rank_ic": rank_ic,
                    "rank_ic_positive": rank_ic > 0,
                    "top_quintile_hit_rate": float((selected["future_return"] >= top_quintile_cut).mean()),
                    "selected_industry_codes": "|".join(selected["industry_code"].tolist()),
                    "selected_industries": "|".join(selected["industry_code"].map(names).fillna(selected["industry_code"]).tolist()),
                })
    return pd.DataFrame(rows)


def build_event_opportunity_set(features: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    by_date = {d: x.copy() for d, x in features.groupby("trade_date")}
    close = features.pivot(index="trade_date", columns="industry_code", values="close_index")
    cols = [
        "valuation_score",
        "oversold_score",
        "turn_score",
        "liquidity_score",
        *[f"{strategy}_score" for strategy in STRATEGIES],
    ]
    for _, trade in trades.iterrows():
        signal_date = pd.to_datetime(trade["signal_date"])
        entry_date = pd.to_datetime(trade["entry_date"])
        exit_date = pd.to_datetime(trade["exit_date"])
        if signal_date not in by_date or entry_date not in close.index or exit_date not in close.index:
            continue
        returns = (close.loc[exit_date] / close.loc[entry_date] - 1.0).dropna()
        frame = by_date[signal_date]
        frame = frame[frame["industry_code"].isin(returns.index)].copy()
        if frame.empty:
            continue
        frame["future_return"] = frame["industry_code"].map(returns)
        frame["future_return_rank_pct"] = frame["future_return"].rank(pct=True)
        top_cut = frame["future_return"].quantile(0.8)
        benchmark = float(frame["future_return"].mean())
        for item in frame.to_dict("records"):
            out = {
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "industry_code": item.get("industry_code", ""),
                "industry_name": item.get("industry_name", ""),
                "future_return": item.get("future_return", ""),
                "relative_to_event_benchmark": float(item.get("future_return", 0.0) - benchmark),
                "future_return_rank_pct": item.get("future_return_rank_pct", ""),
                "future_return_top_quintile": bool(item.get("future_return", 0.0) >= top_cut),
            }
            out.update({col: item.get(col, "") for col in cols if col in frame.columns})
            rows.append(out)
    return pd.DataFrame(rows)


def summarize_strategies(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (strategy, top_n), g in panel.groupby(["strategy", "top_n"]):
        yearly = g.groupby("year")["relative_return"].mean()
        oos = g[g["year"] >= 2022]
        rows.append({
            "strategy": strategy,
            "top_n": int(top_n),
            "event_count": int(len(g)),
            "mean_selected_net_return": float(g["selected_net_return"].mean()),
            "mean_benchmark_return": float(g["benchmark_return"].mean()),
            "mean_relative_return": float(g["relative_return"].mean()),
            "median_relative_return": float(g["relative_return"].median()),
            "relative_win_rate": float(g["relative_win"].mean()),
            "mean_rank_ic": float(g["rank_ic"].mean()),
            "positive_rank_ic_rate": float(g["rank_ic_positive"].mean()),
            "top_quintile_hit_rate": float(g["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
            "oos_mean_rank_ic": float(oos["rank_ic"].mean()) if len(oos) else 0.0,
        })
    out = pd.DataFrame(rows)
    out["passes_strong_rebound_gate"] = (
        (out["event_count"] >= 30)
        & (out["mean_relative_return"] > 0)
        & (out["median_relative_return"] > 0)
        & (out["relative_win_rate"] >= 0.55)
        & (out["mean_rank_ic"] > 0)
        & (out["positive_rank_ic_rate"] >= 0.55)
        & (out["top_quintile_hit_rate"] >= 0.30)
        & (out["positive_year_rate"] >= 0.60)
        & (out["oos_event_count"] >= 8)
        & (out["oos_mean_relative_return"] > 0)
        & (out["oos_relative_win_rate"] >= 0.50)
        & (out["oos_mean_rank_ic"] > 0)
    )
    return out.sort_values(["passes_strong_rebound_gate", "mean_relative_return"], ascending=[False, False])


def evaluate_asof_failure_filter(features: pd.DataFrame, trades: pd.DataFrame, strategy: str, top_n: int, cost_bps: float) -> pd.DataFrame:
    rows = []
    score_col = f"{strategy}_score"
    if score_col not in features.columns:
        return pd.DataFrame()
    by_date = {d: x.copy() for d, x in features.groupby("trade_date")}
    close = features.pivot(index="trade_date", columns="industry_code", values="close_index")
    names = features.sort_values("trade_date").drop_duplicates("industry_code", keep="last").set_index("industry_code")["industry_name"].to_dict()
    ordered = trades.copy()
    for field in ["signal_date", "entry_date", "exit_date"]:
        ordered[field] = pd.to_datetime(ordered[field])
    ordered = ordered.sort_values("signal_date")
    for threshold in [1, 2, 3]:
        failure_counts: dict[str, int] = {}
        pending: list[tuple[pd.Timestamp, list[str]]] = []
        for _, trade in ordered.iterrows():
            signal_date = trade["signal_date"]
            entry_date = trade["entry_date"]
            exit_date = trade["exit_date"]
            matured = [item for item in pending if item[0] < signal_date]
            pending = [item for item in pending if item[0] >= signal_date]
            for _, codes in matured:
                for code in codes:
                    failure_counts[code] = failure_counts.get(code, 0) + 1
            if signal_date not in by_date or entry_date not in close.index or exit_date not in close.index:
                continue
            returns = (close.loc[exit_date] / close.loc[entry_date] - 1.0).dropna()
            frame = by_date[signal_date]
            frame = frame[frame["industry_code"].isin(returns.index)].copy()
            if frame.empty:
                continue
            frame["future_return"] = frame["industry_code"].map(returns)
            benchmark = float(frame["future_return"].mean())
            banned = {code for code, count in failure_counts.items() if count >= threshold}
            ranked = frame.sort_values(score_col, ascending=False)
            selected = ranked[~ranked["industry_code"].isin(banned)].head(top_n)
            if selected.empty:
                continue
            gross = float(selected["future_return"].mean())
            net = gross - cost_bps / 10000.0
            rel = net - benchmark
            rank_ic = float(frame[[score_col, "future_return"]].corr(method="spearman").iloc[0, 1])
            top_quintile_cut = frame["future_return"].quantile(0.8)
            failed_codes = selected.loc[selected["future_return"].lt(benchmark), "industry_code"].tolist()
            pending.append((exit_date, failed_codes))
            rows.append({
                "variant": f"asof_failure_filter_threshold_{threshold}",
                "failure_threshold": threshold,
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "year": int(signal_date.year),
                "strategy": strategy,
                "top_n": top_n,
                "excluded_industry_count": len(banned),
                "selected_return": gross,
                "selected_net_return": net,
                "benchmark_return": benchmark,
                "relative_return": rel,
                "relative_win": rel > 0,
                "rank_ic": rank_ic,
                "rank_ic_positive": rank_ic > 0,
                "top_quintile_hit_rate": float((selected["future_return"] >= top_quintile_cut).mean()),
                "selected_industry_codes": "|".join(selected["industry_code"].tolist()),
                "selected_industries": "|".join(selected["industry_code"].map(names).fillna(selected["industry_code"]).tolist()),
                "failed_industry_codes_recorded_after_exit": "|".join(failed_codes),
            })
    return pd.DataFrame(rows)


def summarize_asof_failure_filter(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    rows = []
    for (variant, threshold), g in events.groupby(["variant", "failure_threshold"]):
        yearly = g.groupby("year")["relative_return"].mean()
        oos = g[g["year"] >= 2022]
        row = {
            "variant": variant,
            "failure_threshold": int(threshold),
            "event_count": int(len(g)),
            "mean_relative_return": float(g["relative_return"].mean()),
            "median_relative_return": float(g["relative_return"].median()),
            "relative_win_rate": float(g["relative_win"].mean()),
            "mean_rank_ic": float(g["rank_ic"].mean()),
            "positive_rank_ic_rate": float(g["rank_ic_positive"].mean()),
            "top_quintile_hit_rate": float(g["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
            "oos_mean_rank_ic": float(oos["rank_ic"].mean()) if len(oos) else 0.0,
            "mean_excluded_industry_count": float(g["excluded_industry_count"].mean()),
        }
        row["passes_strong_rebound_gate"] = bool(
            row["event_count"] >= 30
            and row["mean_relative_return"] > 0
            and row["median_relative_return"] > 0
            and row["relative_win_rate"] >= 0.55
            and row["mean_rank_ic"] > 0
            and row["positive_rank_ic_rate"] >= 0.55
            and row["top_quintile_hit_rate"] >= 0.30
            and row["positive_year_rate"] >= 0.60
            and row["oos_event_count"] >= 8
            and row["oos_mean_relative_return"] > 0
            and row["oos_relative_win_rate"] >= 0.50
            and row["oos_mean_rank_ic"] > 0
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["passes_strong_rebound_gate", "mean_relative_return"], ascending=[False, False])


def evaluate_factor_discovery(features: pd.DataFrame, trades: pd.DataFrame, top_ns: list[int], cost_bps: float) -> pd.DataFrame:
    rows = []
    by_date = {d: x.copy() for d, x in features.groupby("trade_date")}
    close = features.pivot(index="trade_date", columns="industry_code", values="close_index")
    names = features.sort_values("trade_date").drop_duplicates("industry_code", keep="last").set_index("industry_code")["industry_name"].to_dict()
    for _, trade in trades.iterrows():
        signal_date = pd.to_datetime(trade["signal_date"])
        entry_date = pd.to_datetime(trade["entry_date"])
        exit_date = pd.to_datetime(trade["exit_date"])
        if signal_date not in by_date or entry_date not in close.index or exit_date not in close.index:
            continue
        returns = (close.loc[exit_date] / close.loc[entry_date] - 1.0).dropna()
        frame = by_date[signal_date]
        frame = frame[frame["industry_code"].isin(returns.index)].copy()
        if frame.empty:
            continue
        frame["future_return"] = frame["industry_code"].map(returns)
        benchmark = float(frame["future_return"].mean())
        top_quintile_cut = frame["future_return"].quantile(0.8)
        for field, label, higher_is_better in FACTOR_DISCOVERY:
            if field not in frame.columns:
                continue
            usable = frame.dropna(subset=[field, "future_return"]).copy()
            if usable.empty:
                continue
            usable["_factor_score"] = pd.to_numeric(usable[field], errors="coerce")
            usable = usable.dropna(subset=["_factor_score"])
            if usable.empty:
                continue
            ranked = usable.sort_values("_factor_score", ascending=not higher_is_better)
            rank_ic = float(usable[["_factor_score", "future_return"]].corr(method="spearman").iloc[0, 1])
            if not higher_is_better:
                rank_ic = -rank_ic
            for top_n in top_ns:
                selected = ranked.head(top_n)
                gross = float(selected["future_return"].mean())
                net = gross - cost_bps / 10000.0
                rel = net - benchmark
                rows.append({
                    "factor": field,
                    "factor_label": label,
                    "higher_is_better": higher_is_better,
                    "signal_date": signal_date.strftime("%Y-%m-%d"),
                    "entry_date": entry_date.strftime("%Y-%m-%d"),
                    "exit_date": exit_date.strftime("%Y-%m-%d"),
                    "year": int(signal_date.year),
                    "top_n": top_n,
                    "selected_return": gross,
                    "selected_net_return": net,
                    "benchmark_return": benchmark,
                    "relative_return": rel,
                    "relative_win": rel > 0,
                    "rank_ic": rank_ic,
                    "rank_ic_positive": rank_ic > 0,
                    "top_quintile_hit_rate": float((selected["future_return"] >= top_quintile_cut).mean()),
                    "selected_industries": "|".join(selected["industry_code"].map(names).fillna(selected["industry_code"]).tolist()),
                })
    return pd.DataFrame(rows)


def summarize_factor_discovery(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    rows = []
    for (factor, label, top_n), g in events.groupby(["factor", "factor_label", "top_n"]):
        yearly = g.groupby("year")["relative_return"].mean()
        oos = g[g["year"] >= 2022]
        row = {
            "factor": factor,
            "factor_label": label,
            "top_n": int(top_n),
            "event_count": int(len(g)),
            "mean_relative_return": float(g["relative_return"].mean()),
            "median_relative_return": float(g["relative_return"].median()),
            "relative_win_rate": float(g["relative_win"].mean()),
            "mean_rank_ic": float(g["rank_ic"].mean()),
            "positive_rank_ic_rate": float(g["rank_ic_positive"].mean()),
            "top_quintile_hit_rate": float(g["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
            "oos_mean_rank_ic": float(oos["rank_ic"].mean()) if len(oos) else 0.0,
        }
        row["passes_strong_rebound_gate"] = bool(
            row["event_count"] >= 30
            and row["mean_relative_return"] > 0
            and row["median_relative_return"] > 0
            and row["relative_win_rate"] >= 0.55
            and row["mean_rank_ic"] > 0
            and row["positive_rank_ic_rate"] >= 0.55
            and row["top_quintile_hit_rate"] >= 0.30
            and row["positive_year_rate"] >= 0.60
            and row["oos_event_count"] >= 8
            and row["oos_mean_relative_return"] > 0
            and row["oos_relative_win_rate"] >= 0.50
            and row["oos_mean_rank_ic"] > 0
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["passes_strong_rebound_gate", "mean_relative_return"], ascending=[False, False])


def evaluation_gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0]
    checks = [
        ("event_count", best["event_count"], 30, ">="),
        ("mean_relative_return", best["mean_relative_return"], 0, ">"),
        ("median_relative_return", best["median_relative_return"], 0, ">"),
        ("relative_win_rate", best["relative_win_rate"], 0.55, ">="),
        ("mean_rank_ic", best["mean_rank_ic"], 0, ">"),
        ("positive_rank_ic_rate", best["positive_rank_ic_rate"], 0.55, ">="),
        ("top_quintile_hit_rate", best["top_quintile_hit_rate"], 0.30, ">="),
        ("positive_year_rate", best["positive_year_rate"], 0.60, ">="),
        ("oos_event_count", best["oos_event_count"], 8, ">="),
        ("oos_mean_relative_return", best["oos_mean_relative_return"], 0, ">"),
        ("oos_relative_win_rate", best["oos_relative_win_rate"], 0.50, ">="),
        ("oos_mean_rank_ic", best["oos_mean_rank_ic"], 0, ">"),
    ]
    rows = []
    for metric, value, required, op in checks:
        passed = value >= required if op == ">=" else value > required
        rows.append({
            "strategy": best["strategy"],
            "top_n": int(best["top_n"]),
            "metric": metric,
            "current": float(value),
            "operator": op,
            "required": float(required),
            "status": "pass" if passed else "fail",
        })
    return pd.DataFrame(rows)


def failure_diagnosis(panel: pd.DataFrame, results: pd.DataFrame, annual: pd.DataFrame, gate: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    if panel.empty or results.empty:
        return pd.DataFrame()
    best = results.iloc[0]
    strategy = best["strategy"]
    top_n = int(best["top_n"])
    rows = []
    for item in gate[gate["status"].eq("fail")].to_dict("records"):
        rows.append({
            "category": "gate_failure",
            "item": item["metric"],
            "value": item["current"],
            "threshold": f"{item['operator']} {item['required']}",
            "evidence": f"{strategy} Top{top_n}",
        })
    best_annual = annual[(annual["strategy"].eq(strategy)) & (annual["top_n"].eq(top_n))]
    for item in best_annual[best_annual["mean_relative_return"].le(0)].sort_values("mean_relative_return").to_dict("records"):
        rows.append({
            "category": "weak_year",
            "item": int(item["year"]),
            "value": item["mean_relative_return"],
            "threshold": "> 0",
            "evidence": f"events={int(item['event_count'])}; win_rate={float(item['relative_win_rate']):.2f}",
        })
    best_panel = panel[(panel["strategy"].eq(strategy)) & (panel["top_n"].eq(top_n))]
    worst_events = best_panel.nsmallest(5, "relative_return")
    trade_context = trades.drop_duplicates("signal_date").set_index("signal_date").to_dict("index") if "signal_date" in trades.columns else {}
    for item in worst_events.to_dict("records"):
        context = trade_context.get(item["signal_date"], {})
        market_return = context.get("trade_return", "")
        event_type = "industry_selection_failed_in_positive_window"
        if pd.notna(market_return) and float(market_return) <= 0:
            event_type = "window_failed"
        benchmark = float(item.get("benchmark_return", 0.0) or 0.0)
        selected_net = float(item.get("selected_net_return", 0.0) or 0.0)
        rows.append({
            "category": "worst_event",
            "item": item["signal_date"],
            "value": item["relative_return"],
            "threshold": "> 0",
            "evidence": f"type={event_type}; market_return={market_return}; benchmark={benchmark:.4f}; selected_net={selected_net:.4f}; industries={item['selected_industries']}",
        })
    counts: dict[str, int] = {}
    for names in worst_events["selected_industries"].fillna(""):
        for name in str(names).split("|"):
            counts[name] = counts.get(name, 0) + 1
    for name, count in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:10]:
        rows.append({
            "category": "repeated_worst_event_industry",
            "item": name,
            "value": count,
            "threshold": "< 3",
            "evidence": f"appears_in_worst_5_events={count}",
        })
    return pd.DataFrame(rows)


def industry_leader_evidence_debt(gate: pd.DataFrame, diagnosis: pd.DataFrame) -> pd.DataFrame:
    rows = []
    failed = gate[gate["status"].eq("fail")].copy() if not gate.empty and "status" in gate.columns else pd.DataFrame()
    repeated = diagnosis[diagnosis["category"].eq("repeated_worst_event_industry")]["item"].astype(str).head(8).tolist() if not diagnosis.empty and "category" in diagnosis.columns else []
    weak_years = diagnosis[diagnosis["category"].eq("weak_year")]["item"].astype(str).head(8).tolist() if not diagnosis.empty and "category" in diagnosis.columns else []
    for item in failed.to_dict("records"):
        metric = str(item.get("metric", ""))
        current = float(item.get("current", 0.0) or 0.0)
        required = float(item.get("required", 0.0) or 0.0)
        if metric == "top_quintile_hit_rate":
            meaning = "选出的行业落入未来强反弹前 20% 的比例不足。"
            live_rule = "只把行业排序当作观察清单，不因为排名靠前直接加仓。"
            next_evidence = "继续前推记录未来窗口内 Top10 是否进入强反弹前 20%，并重点复核重复失败行业。"
            unsafe = "不能用平均相对收益为正替代 Top 分位命中证据。"
        elif metric == "positive_year_rate":
            meaning = "跨年度稳定性不足，当前优势集中度过高。"
            live_rule = "年份稳定性未达标前，不把单次窗口的行业排序视为可推广规则。"
            next_evidence = "继续按年度追加前推样本，观察弱年份是否改善；弱年份包括：" + ("、".join(weak_years) if weak_years else "暂无")
            unsafe = "不能用样本外均值为正掩盖年度不稳定。"
        else:
            meaning = "强行业选择评价门槛未通过。"
            live_rule = "保持 research_only，不升级为行业选择 alpha。"
            next_evidence = "继续收集同口径前推证据。"
            unsafe = "不能临场放宽门槛。"
        rows.append({
            "blocker": metric,
            "current": current,
            "required": required,
            "gap": max(required - current, 0.0),
            "meaning": meaning,
            "live_decision_rule": live_rule,
            "next_evidence_to_collect": next_evidence,
            "unsafe_shortcut": unsafe,
            "repeated_worst_event_industries": "、".join(repeated),
        })
    if not rows:
        rows.append({
            "blocker": "none",
            "current": "",
            "required": "",
            "gap": 0.0,
            "meaning": "强行业选择评价门槛当前全部通过。",
            "live_decision_rule": "仍需人工复核载体、流动性和跟踪误差。",
            "next_evidence_to_collect": "继续前推样本，防止新样本破坏稳定性。",
            "unsafe_shortcut": "不能自动下单。",
            "repeated_worst_event_industries": "、".join(repeated),
        })
    return pd.DataFrame(rows)


def annual_breakdown(panel: pd.DataFrame) -> pd.DataFrame:
    return panel.groupby(["strategy", "top_n", "year"]).agg(
        event_count=("relative_return", "count"),
        mean_relative_return=("relative_return", "mean"),
        relative_win_rate=("relative_win", "mean"),
    ).reset_index()


def latest_candidates(features: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    summary = read_json(V471_SUMMARY)
    if not summary.get("latest_signal_triggered"):
        return pd.DataFrame(columns=LATEST_CANDIDATE_COLUMNS)
    strategy = str(results.iloc[0]["strategy"]) if len(results) else "value_oversold_turn"
    top_n = int(results.iloc[0]["top_n"]) if len(results) else 10
    date = pd.to_datetime(summary["latest_panel_date"])
    frame = current_snapshot_features(summary)
    if frame.empty:
        eligible_features = eligible_candidate_features(features)
        if eligible_features.empty or "trade_date" not in eligible_features.columns:
            return pd.DataFrame(columns=LATEST_CANDIDATE_COLUMNS)
        feature_dates = eligible_features.loc[eligible_features["trade_date"].le(date), "trade_date"]
        if feature_dates.empty:
            return pd.DataFrame(columns=LATEST_CANDIDATE_COLUMNS)
        frame = eligible_features[eligible_features["trade_date"].eq(feature_dates.max())].copy()
        frame["feature_date"] = feature_dates.max().strftime("%Y-%m-%d")
        frame["price_date"] = frame["feature_date"]
        frame["price_stale_days"] = 0
        frame["candidate_source"] = "official_valuation_history"
    else:
        frame = eligible_candidate_features(frame)
    if frame.empty:
        return pd.DataFrame(columns=LATEST_CANDIDATE_COLUMNS)
    score_col = f"{strategy}_score"
    if score_col not in frame.columns:
        return pd.DataFrame(columns=LATEST_CANDIDATE_COLUMNS)
    frame = frame.dropna(subset=[score_col])
    if frame.empty:
        return pd.DataFrame(columns=LATEST_CANDIDATE_COLUMNS)
    frame = frame.sort_values(score_col, ascending=False).head(top_n)
    keep = ["trade_date", "industry_code", "industry_name", score_col, "valuation_score", "oversold_score", "turn_score", "liquidity_score", "return_20d", "return_60d", "return_120d", "drawdown_252d", "pe", "pb", "dividend_yield"]
    out = frame[keep].copy()
    out.insert(0, "candidate_status", "research_only_industry_selection_candidate")
    out.insert(1, "selection_strategy", strategy)
    out.insert(2, "planned_entry_date", summary["planned_entry_date"])
    out.insert(3, "feature_date", frame["feature_date"].iloc[0])
    out.insert(4, "signal_date", summary["latest_panel_date"])
    out.insert(5, "price_date", frame["price_date"].iloc[0])
    out.insert(6, "price_stale_days", frame["price_stale_days"].iloc[0])
    out.insert(7, "candidate_source", frame["candidate_source"].iloc[0])
    out.rename(columns={score_col: "selection_score"}, inplace=True)
    return out[LATEST_CANDIDATE_COLUMNS]


def eligible_candidate_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep only source-backed rows with at least one usable valuation input."""

    if frame.empty:
        return frame.copy()
    official = official_valuation_history(frame)
    required = {*CORE_CANDIDATE_VALUATION_FIELDS, "valuation_score"}
    if not required.issubset(official.columns):
        return official.iloc[0:0].copy()
    valuation = official[list(CORE_CANDIDATE_VALUATION_FIELDS)].apply(pd.to_numeric, errors="coerce")
    valuation_score = pd.to_numeric(official["valuation_score"], errors="coerce")
    usable = valuation.notna().any(axis=1) & valuation_score.notna()
    return official.loc[usable].copy()


def feature_timing_audit(features: pd.DataFrame, latest: pd.DataFrame, event_panel: pd.DataFrame, opportunity_set: pd.DataFrame) -> pd.DataFrame:
    label_fields = {
        "future_return",
        "future_return_rank_pct",
        "future_return_top_quintile",
        "selected_return",
        "selected_net_return",
        "benchmark_return",
        "relative_return",
        "relative_win",
        "rank_ic",
        "top_quintile_hit_rate",
    }
    ranking_inputs = {"valuation_score", "oversold_score", "turn_score", "liquidity_score"}
    ranking_inputs.update(f"{strategy}_score" for strategy in STRATEGIES)
    ranking_inputs.update(field for field, _, _ in FACTOR_DISCOVERY)
    leaked_inputs = sorted(ranking_inputs & label_fields)
    rows = [{
        "item": "ranking_inputs_exclude_future_labels",
        "status": "pass" if not leaked_inputs else "fail",
        "evidence": "checked=" + ",".join(sorted(ranking_inputs)) + ("; leaked=" + ",".join(leaked_inputs) if leaked_inputs else ""),
    }]

    if latest.empty:
        rows.append({"item": "latest_candidate_feature_asof", "status": "pass", "evidence": "no latest candidates"})
        rows.append({"item": "latest_candidate_price_asof", "status": "pass", "evidence": "no latest candidates"})
    else:
        signal = pd.to_datetime(latest["signal_date"], errors="coerce")
        feature = pd.to_datetime(latest["feature_date"], errors="coerce")
        price = pd.to_datetime(latest["price_date"], errors="coerce")
        max_price_stale = int(pd.to_numeric(latest.get("price_stale_days", pd.Series([0])), errors="coerce").max())
        rows.append({
            "item": "latest_candidate_feature_asof",
            "status": "pass" if feature.le(signal).all() else "fail",
            "evidence": f"max_feature_date={feature.max().date()}; max_signal_date={signal.max().date()}",
        })
        rows.append({
            "item": "latest_candidate_price_asof",
            "status": "pass" if price.le(signal).all() and max_price_stale <= 7 else "fail",
            "evidence": f"max_price_date={price.max().date()}; max_signal_date={signal.max().date()}; max_price_stale_days={max_price_stale}",
        })

    if event_panel.empty:
        rows.append({"item": "historical_event_date_order", "status": "fail", "evidence": "event_panel missing"})
    else:
        signal = pd.to_datetime(event_panel["signal_date"], errors="coerce")
        entry = pd.to_datetime(event_panel["entry_date"], errors="coerce")
        exit_ = pd.to_datetime(event_panel["exit_date"], errors="coerce")
        ok = signal.le(entry).all() and entry.le(exit_).all()
        rows.append({
            "item": "historical_event_date_order",
            "status": "pass" if ok else "fail",
            "evidence": f"rows={len(event_panel)}; signal<=entry<=exit",
        })

    if opportunity_set.empty:
        rows.append({"item": "opportunity_label_separation", "status": "fail", "evidence": "opportunity_set missing"})
    else:
        label_in_scores = sorted(label_fields & {c for c in opportunity_set.columns if c.endswith("_score")})
        rows.append({
            "item": "opportunity_label_separation",
            "status": "pass" if not label_in_scores else "fail",
            "evidence": "label fields kept only for evaluation; score_label_overlap=" + ",".join(label_in_scores),
        })
    return pd.DataFrame(rows)


def add_candidate_risk_flags(latest: pd.DataFrame, diagnosis: pd.DataFrame) -> pd.DataFrame:
    if latest.empty or diagnosis.empty:
        return latest
    risky = set(diagnosis[(diagnosis["category"].eq("repeated_worst_event_industry")) & (pd.to_numeric(diagnosis["value"], errors="coerce") >= 3)]["item"].astype(str))
    out = latest.copy()
    out["historical_failure_flag"] = out["industry_name"].astype(str).isin(risky)
    out["manual_review_reason"] = out["industry_name"].astype(str).map(lambda name: "recent_or_repeated_worst_event_industry" if name in risky else "")
    return out


def candidate_carrier_mapping(latest: pd.DataFrame) -> pd.DataFrame:
    if latest.empty:
        return pd.DataFrame()
    try:
        import akshare as ak
        spot = ak.fund_etf_spot_em()
    except Exception as exc:
        return pd.DataFrame([{
            "industry_code": item.get("industry_code", ""),
            "industry_name": item.get("industry_name", ""),
            "carrier_mapping_status": "fetch_failed",
            "mapping_evidence": str(exc)[:120],
        } for item in latest.to_dict("records")])
    spot = spot[~spot["名称"].astype(str).str.contains("联接|QDII|债|货币|港|纳指|标普|日经", regex=True, na=False)].copy()
    rows = []
    for item in latest.to_dict("records"):
        industry = str(item.get("industry_name", ""))
        keywords = carrier_keywords(industry)
        matches = spot[spot["名称"].astype(str).apply(lambda name: any(k in name for k in keywords))]
        if matches.empty:
            rows.append({
                "industry_code": item.get("industry_code", ""),
                "industry_name": industry,
                "carrier_mapping_status": "no_keyword_match",
                "mapping_confidence": "none",
                "mapping_evidence": "|".join(keywords),
            })
            continue
        matches = matches.sort_values("成交额", ascending=False).head(3)
        for _, m in matches.iterrows():
            confidence = "medium" if any(k in str(m["名称"]) for k in [industry.replace("Ⅱ", ""), industry[:2]]) else "low"
            turnover = float(m.get("成交额", 0) or 0)
            discount = float(m.get("基金折价率", 99) or 99)
            rows.append({
                "industry_code": item.get("industry_code", ""),
                "industry_name": industry,
                "candidate_carrier_code": str(m["代码"]).zfill(6),
                "candidate_carrier_name": m["名称"],
                "latest_price": m.get("最新价", ""),
                "discount_rate": m.get("基金折价率", ""),
                "turnover_amount": m.get("成交额", ""),
                "free_float_market_value": m.get("流通市值", ""),
                "data_date": m.get("数据日期", ""),
                "carrier_mapping_status": "keyword_match_review_required",
                "mapping_confidence": confidence,
                "liquidity_status": "pass" if turnover >= 50_000_000 else "low_turnover",
                "discount_status": "pass" if abs(discount) <= 1.0 else "large_discount_or_premium",
                "mapping_evidence": "|".join(keywords),
            })
    return pd.DataFrame(rows)


def carrier_keywords(industry: str) -> list[str]:
    mapping = {
        "养殖业": ["养殖", "畜牧", "农业", "农牧"],
        "保险Ⅱ": ["保险"],
        "游戏Ⅱ": ["游戏", "传媒"],
        "教育": ["教育"],
        "乘用车": ["汽车", "智能车", "新能源车"],
        "饲料": ["饲料", "农业", "畜牧", "农牧"],
        "一般零售": ["零售", "消费"],
        "白酒Ⅱ": ["白酒", "酒ETF"],
        "焦炭Ⅱ": ["煤炭", "焦炭"],
        "旅游及景区": ["旅游", "文旅"],
    }
    return mapping.get(industry, [industry.replace("Ⅱ", "")])


def carrier_mapping_audit(latest: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    if latest.empty:
        return pd.DataFrame()
    rows = []
    candidate_count = int(len(latest))
    matched = mapping[mapping["carrier_mapping_status"].eq("keyword_match_review_required")] if not mapping.empty and "carrier_mapping_status" in mapping.columns else pd.DataFrame()
    reviewable = matched[matched["liquidity_status"].eq("pass") & matched["discount_status"].eq("pass")] if not matched.empty and "liquidity_status" in matched.columns else pd.DataFrame()
    rows.append({"item": "candidate_industry_count", "value": candidate_count, "status": "info", "evidence": "V4.72 top candidates"})
    rows.append({"item": "keyword_matched_industry_count", "value": int(matched["industry_code"].nunique()) if not matched.empty else 0, "status": "review_required", "evidence": "keyword match only; SW2 exposure/tracking checked separately"})
    rows.append({"item": "reviewable_carrier_industry_count", "value": int(reviewable["industry_code"].nunique()) if not reviewable.empty else 0, "status": "review_required", "evidence": "liquidity and discount pass; tracking checked separately"})
    rows.append({"item": "no_keyword_match_industry_count", "value": int((mapping["carrier_mapping_status"].eq("no_keyword_match")).sum()) if not mapping.empty and "carrier_mapping_status" in mapping.columns else candidate_count, "status": "review_required", "evidence": "needs manual carrier search"})
    rows.append({"item": "low_confidence_carrier_count", "value": int((matched["mapping_confidence"].eq("low")).sum()) if not matched.empty and "mapping_confidence" in matched.columns else 0, "status": "review_required", "evidence": "broad keyword match"})
    rows.append({"item": "low_turnover_carrier_count", "value": int((matched["liquidity_status"].eq("low_turnover")).sum()) if not matched.empty and "liquidity_status" in matched.columns else 0, "status": "review_required", "evidence": "turnover below 50m CNY"})
    rows.append({"item": "auto_execution_allowed", "value": 0, "status": "blocked", "evidence": "research gate not validated; carrier exposure/tracking not fully validated"})
    return pd.DataFrame(rows)


def carrier_exposure_audit(latest: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    if latest.empty:
        return pd.DataFrame()
    if mapping.empty or "candidate_carrier_code" not in mapping.columns:
        return pd.DataFrame([{
            "industry_code": item.get("industry_code", ""),
            "industry_name": item.get("industry_name", ""),
            "exposure_audit_status": "no_reviewable_carrier",
            "sw_second_tracking_status": "not_validated",
        } for item in latest.to_dict("records")])
    reviewable = mapping[
        mapping["carrier_mapping_status"].eq("keyword_match_review_required")
        & mapping["liquidity_status"].eq("pass")
        & mapping["discount_status"].eq("pass")
    ].copy()
    if reviewable.empty:
        return pd.DataFrame([{
            "industry_code": item.get("industry_code", ""),
            "industry_name": item.get("industry_name", ""),
            "exposure_audit_status": "no_reviewable_carrier",
            "sw_second_tracking_status": "not_validated",
        } for item in latest.to_dict("records")])
    reviewable["turnover_amount"] = pd.to_numeric(reviewable["turnover_amount"], errors="coerce").fillna(0)
    first = reviewable.sort_values("turnover_amount", ascending=False).groupby("industry_code", as_index=False).head(1)
    rows = []
    for item in latest.to_dict("records"):
        code = str(item.get("industry_code", ""))
        industry = str(item.get("industry_name", ""))
        selected = first[first["industry_code"].astype(str).eq(code)]
        if selected.empty:
            rows.append({
                "industry_code": code,
                "industry_name": industry,
                "exposure_audit_status": "no_reviewable_carrier",
                "sw_second_tracking_status": "not_validated",
            })
            continue
        carrier = selected.iloc[0]
        allocation, allocation_year = fetch_carrier_industry_allocation(str(carrier["candidate_carrier_code"]))
        if allocation.empty:
            rows.append({
                "industry_code": code,
                "industry_name": industry,
                "candidate_carrier_code": carrier["candidate_carrier_code"],
                "candidate_carrier_name": carrier["candidate_carrier_name"],
                "exposure_audit_status": "industry_allocation_unavailable",
                "sw_second_tracking_status": "not_validated",
            })
            continue
        expected = expected_broad_industries(industry)
        allocation["占净值比例"] = pd.to_numeric(allocation["占净值比例"], errors="coerce").fillna(0)
        matched = allocation[allocation["行业类别"].astype(str).apply(lambda x: any(e in x for e in expected))]
        expected_pct = float(matched["占净值比例"].sum())
        top = allocation.sort_values("占净值比例", ascending=False).iloc[0]
        rows.append({
            "industry_code": code,
            "industry_name": industry,
            "candidate_carrier_code": carrier["candidate_carrier_code"],
            "candidate_carrier_name": carrier["candidate_carrier_name"],
            "allocation_year": allocation_year,
            "top_broad_industry": top["行业类别"],
            "top_broad_industry_pct": float(top["占净值比例"]),
            "expected_broad_industries": "|".join(expected),
            "expected_broad_exposure_pct": expected_pct,
            "exposure_audit_status": "broad_exposure_observed_not_sw2_validated" if expected_pct >= 50 else "broad_exposure_mismatch_review_required",
            "sw_second_tracking_status": "not_validated",
        })
    return pd.DataFrame(rows)


def fetch_carrier_industry_allocation(code: str) -> tuple[pd.DataFrame, str]:
    try:
        import akshare as ak
        for year in [str(datetime.now().year), str(datetime.now().year - 1)]:
            df = ak.fund_portfolio_industry_allocation_em(symbol=code, date=year)
            if not df.empty:
                return df, year
    except Exception:
        pass
    return pd.DataFrame(), ""


def expected_broad_industries(industry: str) -> list[str]:
    mapping = {
        "养殖业": ["农、林、牧、渔业"],
        "保险Ⅱ": ["金融业"],
        "游戏Ⅱ": ["信息传输、软件和信息技术服务业", "文化、体育和娱乐业"],
        "教育": ["教育"],
        "乘用车": ["制造业"],
        "饲料": ["制造业", "农、林、牧、渔业"],
        "一般零售": ["批发和零售业"],
        "白酒Ⅱ": ["制造业"],
        "焦炭Ⅱ": ["采矿业", "制造业"],
        "旅游及景区": ["文化、体育和娱乐业", "住宿和餐饮业", "租赁和商务服务业"],
    }
    return mapping.get(industry, [])


def carrier_tracking_audit(exposure: pd.DataFrame) -> pd.DataFrame:
    if exposure.empty:
        return pd.DataFrame()
    rows = []
    candidates = exposure[exposure["candidate_carrier_code"].fillna("").astype(str).ne("")]
    for item in candidates.to_dict("records"):
        industry_code = str(item.get("industry_code", "")).zfill(6)
        carrier_code = str(item.get("candidate_carrier_code", "")).zfill(6)
        industry_path = HISTORY_DIR / f"{industry_code}.csv"
        if not industry_path.exists():
            rows.append(tracking_row(item, "industry_history_missing"))
            continue
        etf = fetch_etf_history(carrier_code)
        if etf.empty:
            rows.append(tracking_row(item, "carrier_history_fetch_failed"))
            continue
        industry = pd.read_csv(industry_path, encoding="utf-8-sig")
        industry["date"] = pd.to_datetime(industry["日期"])
        industry["industry_close"] = pd.to_numeric(industry["收盘"], errors="coerce")
        etf["date"] = pd.to_datetime(etf["date"])
        etf["etf_close"] = pd.to_numeric(etf["close"], errors="coerce")
        merged = industry[["date", "industry_close"]].merge(etf[["date", "etf_close"]], on="date", how="inner").dropna().tail(253)
        if len(merged) < 60:
            rows.append(tracking_row(item, "insufficient_overlap", overlap_days=len(merged)))
            continue
        returns = merged[["industry_close", "etf_close"]].pct_change().dropna()
        corr = float(returns["industry_close"].corr(returns["etf_close"]))
        gap = returns["etf_close"] - returns["industry_close"]
        mean_abs_gap = float(gap.abs().mean())
        industry_return = float(merged["industry_close"].iloc[-1] / merged["industry_close"].iloc[0] - 1)
        etf_return = float(merged["etf_close"].iloc[-1] / merged["etf_close"].iloc[0] - 1)
        return_gap = etf_return - industry_return
        status = "tracking_observed_review_required"
        if corr < 0.70 or mean_abs_gap > 0.03 or abs(return_gap) > 0.20:
            status = "tracking_weak_review_required"
        rows.append(tracking_row(
            item,
            status,
            overlap_days=len(merged),
            daily_return_corr=corr,
            mean_abs_daily_return_gap=mean_abs_gap,
            carrier_return=etf_return,
            industry_return=industry_return,
            return_gap=return_gap,
        ))
    return pd.DataFrame(rows)


def tracking_row(item: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    return {
        "industry_code": item.get("industry_code", ""),
        "industry_name": item.get("industry_name", ""),
        "candidate_carrier_code": item.get("candidate_carrier_code", ""),
        "candidate_carrier_name": item.get("candidate_carrier_name", ""),
        "tracking_audit_status": status,
        "sw_second_tracking_status": "not_validated",
        **extra,
    }


def fetch_etf_history(code: str) -> pd.DataFrame:
    try:
        import akshare as ak
        symbol = ("sh" if code.startswith("5") else "sz") + code
        df = ak.fund_etf_hist_sina(symbol=symbol)
        if not df.empty:
            return df.rename(columns={"date": "date", "close": "close"})
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame()


def pre_trade_review_sheet(latest: pd.DataFrame, results: pd.DataFrame, carrier_mapping: pd.DataFrame, tracking_audit: pd.DataFrame) -> pd.DataFrame:
    if latest.empty:
        return pd.DataFrame()
    best_passed = bool(results.iloc[0].get("passes_strong_rebound_gate", False)) if len(results) else False
    status_by_code = carrier_mapping.drop_duplicates("industry_code").set_index("industry_code")["carrier_mapping_status"].to_dict() if not carrier_mapping.empty and "industry_code" in carrier_mapping.columns else {}
    ready_counts = {}
    carrier_by_code = {}
    if not carrier_mapping.empty:
        source = carrier_mapping
        if {"liquidity_status", "discount_status"}.issubset(carrier_mapping.columns):
            passed = carrier_mapping[carrier_mapping["liquidity_status"].eq("pass") & carrier_mapping["discount_status"].eq("pass")]
            ready_counts = passed.groupby("industry_code").size().to_dict()
            source = passed if not passed.empty else carrier_mapping
        carrier_by_code = source.drop_duplicates("industry_code").set_index("industry_code").to_dict("index")
    tracking_by_code = tracking_audit.drop_duplicates("industry_code").set_index("industry_code").to_dict("index") if not tracking_audit.empty and "industry_code" in tracking_audit.columns else {}
    rows = []
    for item in latest.to_dict("records"):
        risky = bool(item.get("historical_failure_flag", False))
        code = str(item.get("industry_code", ""))
        carrier_status = status_by_code.get(code, "not_audited_for_industry_candidate")
        tracking = tracking_by_code.get(code, {})
        carrier = carrier_by_code.get(code, {})
        tracking_status = tracking.get("tracking_audit_status", "not_audited")
        tracking_weak = tracking_status not in {"tracking_observed_review_required"}
        rows.append({
            "industry_code": code,
            "industry_name": item.get("industry_name", ""),
            "selection_strategy": item.get("selection_strategy", ""),
            "selection_score": item.get("selection_score", ""),
            "planned_entry_date": item.get("planned_entry_date", ""),
            "research_gate_status": "pass" if best_passed else "research_only_not_validated",
            "historical_failure_flag": risky,
            "manual_review_reason": item.get("manual_review_reason", ""),
            "carrier_mapping_status": carrier_status,
            "candidate_carrier_code": carrier.get("candidate_carrier_code", ""),
            "candidate_carrier_name": carrier.get("candidate_carrier_name", ""),
            "reviewable_carrier_count": int(ready_counts.get(code, 0)),
            "tracking_audit_status": tracking_status,
            "tracking_daily_return_corr": tracking.get("daily_return_corr", ""),
            "tracking_return_gap": tracking.get("return_gap", ""),
            "auto_execution_allowed": "否",
            "manual_action": "降级观察/待复核" if risky or tracking_weak or not best_passed else "待复核",
            "required_checks": "确认行业载体、流动性、折溢价/跟踪误差、仓位上限、入场价漂移；通过前不得自动执行",
        })
    return pd.DataFrame(rows)


def current_snapshot_features(summary: dict[str, Any]) -> pd.DataFrame:
    signal_date = pd.to_datetime(summary["latest_panel_date"])
    candidates: list[tuple[pd.Timestamp, pd.Timestamp, Path, pd.DataFrame]] = []
    required = {"snapshot_observed_at", "snapshot_available_date", "data_status", "pit_eligible"}
    for path in sorted(SNAPSHOT_DIR.glob("*.csv")):
        try:
            snapshot = pd.read_csv(path, encoding="utf-8-sig")
        except Exception:  # noqa: BLE001 - a malformed cache is simply ineligible
            continue
        if snapshot.empty or not required.issubset(snapshot.columns):
            continue
        status = str(snapshot["data_status"].iloc[0])
        if status != CURRENT_SNAPSHOT_STATUS:
            continue
        available_date = pd.to_datetime(snapshot["snapshot_available_date"].iloc[0], errors="coerce")
        observed_at = pd.to_datetime(snapshot["snapshot_observed_at"].iloc[0], errors="coerce", utc=True)
        if pd.isna(available_date) or pd.isna(observed_at) or available_date > signal_date:
            continue
        candidates.append((available_date, observed_at, path, snapshot))
    if not candidates:
        return pd.DataFrame()
    snapshot_date, _, path, snap = sorted(candidates, key=lambda item: (item[0], item[1]))[-1]
    rows = []
    for item in snap.to_dict("records"):
        code = str(item["行业代码"]).zfill(6)
        hist_path = HISTORY_DIR / f"{code}.csv"
        if not hist_path.exists():
            continue
        hist = pd.read_csv(hist_path, encoding="utf-8-sig")
        hist["日期"] = pd.to_datetime(hist["日期"])
        hist = hist[hist["日期"].le(signal_date)].sort_values("日期")
        if len(hist) < 121:
            continue
        price_date = hist.iloc[-1]["日期"]
        if (signal_date - price_date).days > 7:
            continue
        close = pd.to_numeric(hist["收盘"], errors="coerce")
        row = {
            "trade_date": signal_date,
            "feature_date": snapshot_date.strftime("%Y-%m-%d"),
            "price_date": price_date.strftime("%Y-%m-%d"),
            "price_stale_days": int((signal_date - price_date).days),
            "candidate_source": "forward_observed_current_valuation_snapshot_plus_price_history",
            "valuation_data_status": CURRENT_SNAPSHOT_STATUS,
            "valuation_pit_eligible": False,
            "industry_code": code,
            "industry_name": item["行业名称"],
            "close_index": float(close.iloc[-1]),
            "pe": float(item["TTM(滚动)市盈率"]),
            "pb": float(item["市净率"]),
            "dividend_yield": float(item["静态股息率"]) / 100.0,
            "amount_share_pct": float(pd.to_numeric(hist["成交额"], errors="coerce").iloc[-1]),
        }
        for n in [5, 20, 60, 120]:
            row[f"return_{n}d"] = float(close.iloc[-1] / close.iloc[-1 - n] - 1.0)
        row["drawdown_252d"] = float(close.iloc[-1] / close.tail(252).max() - 1.0)
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["valuation_score"] = score_by_date(out, [("pe", False), ("pb", False), ("dividend_yield", True)])
    out["oversold_score"] = score_by_date(out, [("return_60d", False), ("return_120d", False), ("drawdown_252d", False)])
    out["turn_score"] = score_by_date(out, [("return_5d", True), ("return_20d", True)])
    out["liquidity_score"] = score_by_date(out, [("amount_share_pct", True)])
    for strategy, weights in STRATEGIES.items():
        out[f"{strategy}_score"] = sum(out[f"{k}_score"].fillna(0.5) * v for k, v in weights.items())
    return out


def build_summary(results: pd.DataFrame, panel: pd.DataFrame, latest: pd.DataFrame, carrier_audit: pd.DataFrame, exposure_audit: pd.DataFrame, tracking_audit: pd.DataFrame, asof_filter_summary: pd.DataFrame, factor_results: pd.DataFrame, valuation_data_status: str) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    asof_best = asof_filter_summary.iloc[0].to_dict() if len(asof_filter_summary) else {}
    factor_best = factor_results.iloc[0].to_dict() if len(factor_results) else {}
    structure_rows = factor_results[factor_results["factor"].isin(STRUCTURE_FACTOR_FIELDS)] if len(factor_results) and "factor" in factor_results.columns else pd.DataFrame()
    structure_best = structure_rows.iloc[0].to_dict() if len(structure_rows) else {}
    passed = bool(best.get("passes_strong_rebound_gate", False))
    valuation_pit_eligible = valuation_data_status == "pit_verified_asof"
    return {
        "version": "4.72.0",
        "policy_id": "industry_rebound_leader_selection_v4_72",
        "policy_status": "research_only",
        "valuation_data_status": valuation_data_status,
        "valuation_pit_eligible": valuation_pit_eligible,
        "historical_evidence_label": "historical_review_used_in_iteration",
        "promotion_eligible": False,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "event_rows": int(len(panel)),
        "strategy_count": int(results[["strategy", "top_n"]].drop_duplicates().shape[0]) if len(results) else 0,
        "best_strategy": best.get("strategy", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_relative_win_rate": float(best.get("relative_win_rate", 0.0) or 0.0),
        "best_mean_rank_ic": float(best.get("mean_rank_ic", 0.0) or 0.0),
        "best_positive_rank_ic_rate": float(best.get("positive_rank_ic_rate", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_oos_mean_relative_return": float(best.get("oos_mean_relative_return", 0.0) or 0.0),
        "best_oos_mean_rank_ic": float(best.get("oos_mean_rank_ic", 0.0) or 0.0),
        "best_status": (
            "research_only_historical_review_non_pit_valuation_blocked"
            if not valuation_pit_eligible
            else "pass_stronger_industry_gate" if passed else "research_only_not_validated"
        ),
        "latest_candidate_count": int(len(latest)),
        "latest_candidate_feature_date": str(latest["feature_date"].iloc[0]) if len(latest) and "feature_date" in latest.columns else "",
        "latest_candidate_max_price_stale_days": int(pd.to_numeric(latest["price_stale_days"], errors="coerce").max()) if len(latest) and "price_stale_days" in latest.columns else 0,
        "reviewable_carrier_industry_count": int(audit_value(carrier_audit, "reviewable_carrier_industry_count")),
        "no_keyword_match_industry_count": int(audit_value(carrier_audit, "no_keyword_match_industry_count")),
        "broad_exposure_observed_industry_count": int((exposure_audit["exposure_audit_status"].eq("broad_exposure_observed_not_sw2_validated")).sum()) if not exposure_audit.empty and "exposure_audit_status" in exposure_audit.columns else 0,
        "tracking_observed_carrier_count": int((tracking_audit["tracking_audit_status"].eq("tracking_observed_review_required")).sum()) if not tracking_audit.empty and "tracking_audit_status" in tracking_audit.columns else 0,
        "asof_failure_filter_best_variant": asof_best.get("variant", ""),
        "asof_failure_filter_best_mean_relative_return": float(asof_best.get("mean_relative_return", 0.0) or 0.0),
        "asof_failure_filter_best_top_quintile_hit_rate": float(asof_best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "asof_failure_filter_best_positive_year_rate": float(asof_best.get("positive_year_rate", 0.0) or 0.0),
        "asof_failure_filter_passes_gate": bool(asof_best.get("passes_strong_rebound_gate", False)),
        "factor_discovery_best_factor": factor_best.get("factor", ""),
        "factor_discovery_best_factor_label": factor_best.get("factor_label", ""),
        "factor_discovery_best_top_n": int(factor_best.get("top_n", 0) or 0),
        "factor_discovery_best_mean_relative_return": float(factor_best.get("mean_relative_return", 0.0) or 0.0),
        "factor_discovery_best_top_quintile_hit_rate": float(factor_best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "factor_discovery_best_positive_year_rate": float(factor_best.get("positive_year_rate", 0.0) or 0.0),
        "factor_discovery_passes_gate": bool(factor_best.get("passes_strong_rebound_gate", False)),
        "structure_factor_best_factor": structure_best.get("factor", ""),
        "structure_factor_best_factor_label": structure_best.get("factor_label", ""),
        "structure_factor_best_top_n": int(structure_best.get("top_n", 0) or 0),
        "structure_factor_best_mean_relative_return": float(structure_best.get("mean_relative_return", 0.0) or 0.0),
        "structure_factor_best_top_quintile_hit_rate": float(structure_best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "structure_factor_passes_gate": bool(structure_best.get("passes_strong_rebound_gate", False)),
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "evaluation_gate": "event_count>=30; mean/median relative>0; win_rate>=55%; mean_rank_ic>0; positive_rank_ic_rate>=55%; top_quintile_hit_rate>=30%; positive_year_rate>=60%; OOS events>=8; OOS mean relative>0; OOS win_rate>=50%; OOS mean_rank_ic>0",
        "final_verdict": (
            "历史估值缺少可验证的发布时间与可得日；估值字段已屏蔽，旧结果仅保留为迭代历史审查，不得晋级。"
            if not valuation_pit_eligible
            else "已找到通过评价门槛的强反弹行业选择证据。" if passed else "尚未证明能稳定选出比全行业平均反弹更强的行业。"
        ),
    }


def audit_value(audit: pd.DataFrame, item: str) -> float:
    if audit.empty:
        return 0.0
    rows = audit[audit["item"].eq(item)]
    return float(rows["value"].iloc[0]) if len(rows) else 0.0


def render_report(summary: dict[str, Any], results: pd.DataFrame, annual: pd.DataFrame, latest: pd.DataFrame, gate_audit: pd.DataFrame, evidence_debt: pd.DataFrame, diagnosis: pd.DataFrame, asof_filter_summary: pd.DataFrame, factor_results: pd.DataFrame, carrier_audit: pd.DataFrame, exposure_audit: pd.DataFrame, tracking_audit: pd.DataFrame, pre_trade: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.72 反弹窗口内强行业选择评价",
        "",
        summary["final_verdict"],
        "",
        "## 评价体系",
        "",
        "- 先使用 V4.70 已识别的反弹窗口，不重新优化窗口。",
        (
            "- 估值历史已通过真实发布时间与冻结交易日历校验，并按 available_date 向后关联。"
            if summary.get("valuation_pit_eligible")
            else "- 历史估值缺少真实发布时间/版本证据，估值字段已屏蔽；本轮历史排序不含估值信息。"
        ),
        "- 从 entry_date 到 exit_date 计算行业指数收益。",
        "- 与同一窗口内全部可用申万二级行业等权收益比较。",
        f"- 通过门槛：{summary['evaluation_gate']}。",
        "",
        "## 最优组合",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 策略结果",
        "",
        table(results),
        "",
        "## Gate 审计",
        "",
        table(gate_audit),
        "",
        "## 强行业选择证据债务",
        "",
        table(evidence_debt),
        "",
        "## 失败诊断",
        "",
        table(diagnosis),
        "",
        "## As-of 历史失败行业过滤诊断",
        "",
        "只使用当前 signal_date 之前已经结束并可观察到的失败记录；不使用全样本事后黑名单。",
        "",
        table(asof_filter_summary),
        "",
        "## 窗口内单因子发现诊断",
        "",
        "单独测试现有价格、估值、流动性因子能否在反弹窗口内选出更强行业；该表用于发现方向，不用于临场调参。",
        "",
        table(factor_results.head(20)),
        "",
        "## 结构变化因子诊断",
        "",
        "测试相对强度改善、跌幅收敛、成交占比变化、换手变化和估值变化。该组当前未提供可升级证据。",
        "",
        table(factor_results[factor_results["factor"].isin(STRUCTURE_FACTOR_FIELDS)].head(20)) if len(factor_results) and "factor" in factor_results.columns else "无数据。",
        "",
        "## 当前候选",
        "",
        table(latest.head(20)) if len(latest) else "当前无候选。",
        "",
        "## 载体映射审计",
        "",
        table(carrier_audit),
        "",
        "## 载体宽行业暴露审计",
        "",
        table(exposure_audit),
        "",
        "## 载体历史跟踪审计",
        "",
        table(tracking_audit),
        "",
        "## 入场前人工复核表",
        "",
        table(pre_trade.head(20)) if len(pre_trade) else "当前无复核项。",
        "",
        "## 研究边界",
        "",
        "这是行业选择研究，不是交易指令；ETF 或其他载体仍需另行做流动性、折溢价、跟踪误差和容量复核。",
    ])


def table(df: pd.DataFrame) -> str:
    if df.empty:
        return "无数据。"
    return df.to_markdown(index=False)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2020-01-01", "2020-01-01"]),
        "industry_code": ["1", "2"],
        "industry_name": ["A", "B"],
        "close_index": [1.0, 1.0],
        "pe": [10.0, 20.0],
        "pb": [1.0, 2.0],
        "dividend_yield": [0.03, 0.01],
        "amount_share_pct": [1.0, 2.0],
    })
    score = score_by_date(df, [("pe", False), ("dividend_yield", True)])
    assert score.iloc[0] > score.iloc[1]
    diag = failure_diagnosis(
        pd.DataFrame([{"strategy": "s", "top_n": 1, "year": 2020, "signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "relative_return": -0.1, "selected_industries": "A"}]),
        pd.DataFrame([{"strategy": "s", "top_n": 1}]),
        pd.DataFrame([{"strategy": "s", "top_n": 1, "year": 2020, "event_count": 1, "mean_relative_return": -0.1, "relative_win_rate": 0.0}]),
        pd.DataFrame([{"metric": "x", "current": 0.0, "operator": ">=", "required": 1.0, "status": "fail"}]),
        pd.DataFrame([{"signal_date": "2020-01-01", "trade_return": 0.2}]),
    )
    assert {"gate_failure", "weak_year", "worst_event", "repeated_worst_event_industry"}.issubset(set(diag["category"]))
    assert "industry_selection_failed_in_positive_window" in str(diag["evidence"].iloc[-2])
    debt = industry_leader_evidence_debt(pd.DataFrame([{"metric": "top_quintile_hit_rate", "current": 0.2, "required": 0.3, "status": "fail"}]), diag)
    assert debt["blocker"].iloc[0] == "top_quintile_hit_rate"
    assert debt["gap"].iloc[0] > 0
    flagged = add_candidate_risk_flags(pd.DataFrame([{"industry_name": "A"}]), pd.DataFrame([{"category": "repeated_worst_event_industry", "item": "A", "value": 3}]))
    assert bool(flagged["historical_failure_flag"].iloc[0])
    carrier = pd.DataFrame([{"industry_code": "", "carrier_mapping_status": "keyword_match_review_required"}])
    review = pre_trade_review_sheet(
        flagged,
        pd.DataFrame([{"passes_strong_rebound_gate": False}]),
        carrier,
        pd.DataFrame([{"industry_code": "", "tracking_audit_status": "tracking_observed_review_required", "daily_return_corr": 0.8, "return_gap": 0.0}]),
    )
    assert review["auto_execution_allowed"].iloc[0] == "否"
    assert review["carrier_mapping_status"].iloc[0] == "keyword_match_review_required"
    assert review["tracking_audit_status"].iloc[0] == "tracking_observed_review_required"
    audit = carrier_mapping_audit(flagged, pd.DataFrame([{"industry_code": "", "carrier_mapping_status": "keyword_match_review_required", "liquidity_status": "pass", "discount_status": "pass", "mapping_confidence": "medium"}]))
    assert audit_value(audit, "reviewable_carrier_industry_count") == 1
    exposure = carrier_exposure_audit(flagged, pd.DataFrame())
    assert exposure["sw_second_tracking_status"].iloc[0] == "not_validated"
    tracking = carrier_tracking_audit(pd.DataFrame([{"industry_code": "000000", "industry_name": "X", "candidate_carrier_code": "510300", "candidate_carrier_name": "X"}]))
    assert tracking["tracking_audit_status"].iloc[0] == "industry_history_missing"
    dates = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"] * 2)
    mini = pd.DataFrame({
        "trade_date": dates,
        "industry_code": ["1"] * 4 + ["2"] * 4,
        "industry_name": ["A"] * 4 + ["B"] * 4,
        "close_index": [100, 100, 90, 90, 100, 100, 110, 110],
        "oversold_liquidity_score": [1.0] * 4 + [0.9] * 4,
    })
    trades = pd.DataFrame([
        {"signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03"},
        {"signal_date": "2020-01-04", "entry_date": "2020-01-04", "exit_date": "2020-01-04"},
    ])
    asof = evaluate_asof_failure_filter(mini, trades, "oversold_liquidity", 1, 0.0)
    assert len(asof) == 6
    second = asof[(asof["failure_threshold"].eq(1)) & (asof["signal_date"].eq("2020-01-04"))].iloc[0]
    assert second["excluded_industry_count"] == 1
    assert second["selected_industry_codes"] == "2"
    opportunity = build_event_opportunity_set(mini, trades.iloc[:1])
    assert len(opportunity) == 2
    assert opportunity["future_return_top_quintile"].sum() == 1
    assert "oversold_liquidity_score" in opportunity.columns
    timing = feature_timing_audit(
        mini,
        pd.DataFrame([{"signal_date": "2020-01-03", "feature_date": "2020-01-02", "price_date": "2020-01-02", "price_stale_days": 1}]),
        asof,
        opportunity,
    )
    assert set(timing["status"]) == {"pass"}
    bad_timing = feature_timing_audit(
        mini,
        pd.DataFrame([{"signal_date": "2020-01-03", "feature_date": "2020-01-04", "price_date": "2020-01-02", "price_stale_days": 1}]),
        asof,
        opportunity,
    )
    assert bad_timing[bad_timing["item"].eq("latest_candidate_feature_asof")]["status"].iloc[0] == "fail"
    mini["pb"] = [1.0] * 4 + [2.0] * 4
    factors = evaluate_factor_discovery(mini, trades.iloc[:1], [1], 0.0)
    low_pb = factors[factors["factor"].eq("pb")].iloc[0]
    assert low_pb["selected_industries"] == "A"
    assert low_pb["rank_ic"] < 0
    global read_json
    original_read_json = read_json
    try:
        read_json = lambda _path: {"latest_signal_triggered": False}
        empty_latest = latest_candidates(pd.DataFrame(), pd.DataFrame())
    finally:
        read_json = original_read_json
    assert empty_latest.empty
    assert list(empty_latest.columns) == LATEST_CANDIDATE_COLUMNS
    print("self_check=pass")


if __name__ == "__main__":
    main()
