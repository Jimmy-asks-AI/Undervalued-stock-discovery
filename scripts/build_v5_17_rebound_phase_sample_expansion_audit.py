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
HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
NAME_SOURCE = ROOT / "outputs" / "audit" / "rebound_leader_confirmation_filter_audit_v5_14" / "debug" / "confirmation_filter_opportunity_set.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_phase_sample_expansion_audit_v5_17"
DEBUG = OUT / "debug"
TOP_N = 5
CONFIRM_DAYS = 5
HOLD_DAYS = 20
COOLDOWN_DAYS = 20
COST = 0.001


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.17 expanded rebound-phase sample audit.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    close = load_close_panel()
    market = build_market_panel(close)
    events = build_events(close, market)
    opportunity = build_opportunity(close, events)
    event_panel = evaluate(opportunity)
    results = summarize(event_panel)
    summary = build_summary(results)
    write_outputs(summary, opportunity, event_panel, results, events)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def load_close_panel() -> pd.DataFrame:
    pieces = []
    for path in sorted(HISTORY_DIR.glob("*.csv")):
        code = path.stem.zfill(6)
        frame = pd.read_csv(path, encoding="utf-8-sig", usecols=["日期", "收盘"])
        frame["日期"] = pd.to_datetime(frame["日期"])
        frame[code] = pd.to_numeric(frame["收盘"], errors="coerce")
        pieces.append(frame[["日期", code]].set_index("日期"))
    close = pd.concat(pieces, axis=1, sort=False).sort_index()
    return close.loc["2015-01-01":].dropna(axis=1, thresh=252)


def build_market_panel(close: pd.DataFrame) -> pd.DataFrame:
    returns = close.pct_change()
    market_close = close.mean(axis=1)
    out = pd.DataFrame(index=close.index)
    out["market_ret_5"] = market_close / market_close.shift(5) - 1.0
    out["market_ret_10"] = market_close / market_close.shift(10) - 1.0
    out["market_ret_60"] = market_close / market_close.shift(60) - 1.0
    out["negative_breadth_60"] = (close / close.shift(60) - 1.0).lt(0).mean(axis=1)
    out["drawdown_120"] = market_close / market_close.rolling(120).max() - 1.0
    out["vol_ratio_20_60"] = returns.mean(axis=1).rolling(20).std() / returns.mean(axis=1).rolling(60).std()
    return out


def build_events(close: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    variants = {
        "broad_pressure_recovery": (market["market_ret_60"] <= -0.05) & (market["market_ret_5"] > 0) & (market["negative_breadth_60"] >= 0.55),
        "drawdown_repair_recovery": (market["drawdown_120"] <= -0.08) & (market["market_ret_10"] > 0),
        "mild_pressure_recovery": (market["market_ret_60"] <= -0.03) & (market["market_ret_5"] > 0) & (market["vol_ratio_20_60"] <= 1.5),
    }
    rows = []
    dates = list(close.index)
    date_pos = {date: i for i, date in enumerate(dates)}
    for variant, mask in variants.items():
        last_pos = -10_000
        for signal_date in market.index[mask.fillna(False)]:
            pos = date_pos.get(signal_date)
            if pos is None or pos - last_pos < COOLDOWN_DAYS:
                continue
            entry_pos = pos + 1
            confirm_pos = entry_pos + CONFIRM_DAYS
            exit_pos = confirm_pos + HOLD_DAYS
            if exit_pos >= len(dates) or pos < 130:
                continue
            rows.append({
                "phase_variant": variant,
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "entry_date": dates[entry_pos].strftime("%Y-%m-%d"),
                "confirm_date": dates[confirm_pos].strftime("%Y-%m-%d"),
                "exit_date": dates[exit_pos].strftime("%Y-%m-%d"),
                "market_ret_5": float(market.at[signal_date, "market_ret_5"]),
                "market_ret_60": float(market.at[signal_date, "market_ret_60"]),
                "negative_breadth_60": float(market.at[signal_date, "negative_breadth_60"]),
                "drawdown_120": float(market.at[signal_date, "drawdown_120"]),
                "vol_ratio_20_60": float(market.at[signal_date, "vol_ratio_20_60"]),
            })
            last_pos = pos
    return pd.DataFrame(rows)


def build_opportunity(close: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    names = load_names()
    returns = close.pct_change()
    rows = []
    for event in events.to_dict("records"):
        signal_date = pd.Timestamp(event["signal_date"])
        entry_date = pd.Timestamp(event["entry_date"])
        confirm_date = pd.Timestamp(event["confirm_date"])
        exit_date = pd.Timestamp(event["exit_date"])
        hist = returns.loc[:signal_date].tail(120)
        hist = hist.loc[:, hist.count() >= 80]
        market_ret = hist.mean(axis=1)
        market_var = market_ret.var()
        if not market_var or pd.isna(market_var):
            continue
        beta = hist.apply(lambda s: s.cov(market_ret) / market_var)
        entry = close.loc[entry_date]
        confirm = close.loc[confirm_date]
        exit_ = close.loc[exit_date]
        early = confirm / entry - 1.0
        future = exit_ / confirm - 1.0
        frame = pd.DataFrame({
            "industry_code": close.columns,
            "beta_120": beta.reindex(close.columns).values,
            "early_return": early.reindex(close.columns).values,
            "future_return_after_confirm": future.reindex(close.columns).values,
        }).dropna()
        frame["industry_name"] = frame["industry_code"].map(names).fillna(frame["industry_code"])
        frame["beta_120_rank"] = frame["beta_120"].rank(pct=True)
        frame["early_strength_rank"] = (frame["early_return"] - frame["early_return"].mean()).rank(pct=True)
        frame["early_beta_score"] = 0.60 * frame["early_strength_rank"] + 0.40 * frame["beta_120_rank"]
        frame["future_benchmark_return_after_confirm"] = frame["future_return_after_confirm"].mean()
        frame["relative_return_after_confirm"] = frame["future_return_after_confirm"] - COST - frame["future_benchmark_return_after_confirm"]
        for key, value in event.items():
            frame[key] = value
        rows.append(frame)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def load_names() -> dict[str, str]:
    if not NAME_SOURCE.exists():
        return {}
    frame = pd.read_csv(NAME_SOURCE, encoding="utf-8-sig", dtype={"industry_code": str})
    return dict(zip(frame["industry_code"].str.zfill(6), frame["industry_name"].astype(str)))


def evaluate(opportunity: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, event in opportunity.groupby(["phase_variant", "signal_date", "entry_date", "confirm_date", "exit_date"], sort=False):
        phase_variant, signal_date, entry_date, confirm_date, exit_date = keys
        top_cut = event["future_return_after_confirm"].quantile(0.8)
        selected = event.sort_values("early_beta_score", ascending=False).head(TOP_N)
        relative = float(selected["relative_return_after_confirm"].mean())
        rows.append({
            "phase_variant": phase_variant,
            "feature": "early_beta_score",
            "top_n": TOP_N,
            "confirm_days": CONFIRM_DAYS,
            "signal_date": signal_date,
            "entry_date": entry_date,
            "confirm_date": confirm_date,
            "exit_date": exit_date,
            "year": int(pd.to_datetime(signal_date).year),
            "relative_return": relative,
            "relative_win": relative > 0,
            "top_quintile_hit_rate": float((selected["future_return_after_confirm"] >= top_cut).mean()),
        })
    return pd.DataFrame(rows)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for variant, group in events.groupby("phase_variant"):
        oos = group[group["year"].ge(2022)]
        yearly = group.groupby("year")["relative_return"].mean()
        row = {
            "phase_variant": variant,
            "feature": "early_beta_score",
            "top_n": TOP_N,
            "confirm_days": CONFIRM_DAYS,
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
        row.update(v480.robustness_metrics(group, TOP_N) if row["point_gate_passed"] else {})
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["passes_gate", "point_gate_passed", "mean_relative_return"], ascending=[False, False, False])


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
    failed = []
    for metric, required, op in checks:
        if op == "==":
            ok = row.get(metric) == required
        else:
            value = float(row.get(metric, 0) or 0)
            ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    return failed


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
        "version": "5.17.0",
        "policy_id": "rebound_phase_sample_expansion_audit_v5_17",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_phase_count": int(len(results)),
        "best_phase_variant": best.get("phase_variant", ""),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "passing_rule_count": int(results["passes_gate"].sum()) if len(results) else 0,
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "pass_rebound_phase_expansion_gate" if passed else "research_only_no_rebound_phase_expansion_alpha",
        "final_verdict": "V5.17 扩展压力恢复阶段样本未通过完整强行业门槛，不能声称目标完成。" if not passed else "V5.17 扩展压力恢复阶段样本通过强行业门槛，但仍需前推验证。",
    }


def write_outputs(summary: dict[str, Any], opportunity: pd.DataFrame, event_panel: pd.DataFrame, results: pd.DataFrame, events: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, results), encoding="utf-8")
    events.to_csv(DEBUG / "expanded_phase_events.csv", index=False, encoding="utf-8-sig")
    opportunity.to_csv(DEBUG / "expanded_phase_opportunity_set.csv", index=False, encoding="utf-8-sig")
    event_panel.to_csv(DEBUG / "expanded_phase_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "expanded_phase_results.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.17 压力恢复阶段样本扩展审计",
        "",
        summary["final_verdict"],
        "",
        f"- 测试阶段数：{summary['tested_phase_count']}",
        f"- 最优阶段：`{summary['best_phase_variant']}`",
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
        "边界：V5.17 从申万二级历史价格生成更宽的压力恢复阶段样本，特征只使用信号日和确认日前可见数据。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    dates = pd.date_range("2020-01-01", periods=140, freq="B")
    close = pd.DataFrame({"000001": range(100, 240), "000002": range(90, 230)}, index=dates, dtype=float)
    market = build_market_panel(close)
    assert {"market_ret_5", "negative_breadth_60", "drawdown_120"}.issubset(market.columns)
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
