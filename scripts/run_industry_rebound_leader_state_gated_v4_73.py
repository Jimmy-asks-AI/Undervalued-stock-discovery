#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
V471_SOURCE = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "source_panel.csv"
VALUATION_HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_state_gated_v4_73"
DEBUG = OUT / "debug"

GATE_TEXT = (
    "event_count>=30; mean/median relative>0; win_rate>=55%; mean_rank_ic>0; "
    "positive_rank_ic_rate>=55%; top_quintile_hit_rate>=30%; positive_year_rate>=60%; "
    "OOS events>=8; OOS mean relative>0; OOS win_rate>=50%; OOS mean_rank_ic>0"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.73 state-gated rebound-leader selection audit.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    panel = pd.read_csv(V472 / "debug" / "industry_event_panel.csv", encoding="utf-8-sig")
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    latest = pd.read_csv(V472 / "top_candidates.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    names = industry_name_map()

    enriched = attach_state(panel, trades)
    variants = build_variant_panel(enriched)
    results = summarize(variants)
    gate_audit = build_gate_audit(results)
    current_state = latest_state_row()
    current_candidates = build_current_candidates(latest, names, current_state, results)
    summary = build_summary(results, current_candidates, current_state)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    current_candidates.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, results, gate_audit, current_candidates), encoding="utf-8")
    enriched.to_csv(DEBUG / "state_annotated_event_panel.csv", index=False, encoding="utf-8-sig")
    variants.to_csv(DEBUG / "state_gated_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "state_gated_strategy_results.csv", index=False, encoding="utf-8-sig")
    gate_audit.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    current_candidates.to_csv(DEBUG / "latest_state_gated_candidates.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_variant={summary['best_state_gate_variant']}")


def attach_state(panel: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    cols = ["signal_date", "entry_date", "exit_date", "market_stress_score", "negative_breadth_60d", "market_volatility_20d_vs_60d"]
    trade_state = trades[cols].copy()
    out = panel.merge(trade_state, on=["signal_date", "entry_date", "exit_date"], how="left")
    out["deep_negative_breadth"] = pd.to_numeric(out["negative_breadth_60d"], errors="coerce").ge(0.75)
    stress = pd.to_numeric(out["market_stress_score"], errors="coerce")
    out["mid_high_stress"] = stress.gt(0.55) & stress.le(0.70)
    out["high_volatility_protection"] = pd.to_numeric(out["market_volatility_20d_vs_60d"], errors="coerce").ge(1.30)
    out["any_passed_state_bucket"] = out["deep_negative_breadth"] | out["mid_high_stress"] | out["high_volatility_protection"]
    return out


def build_variant_panel(enriched: pd.DataFrame) -> pd.DataFrame:
    specs = {
        "deep_negative_breadth_only": enriched["deep_negative_breadth"],
        "mid_high_stress_only": enriched["mid_high_stress"],
        "high_volatility_only": enriched["high_volatility_protection"],
        "any_passed_state_bucket": enriched["any_passed_state_bucket"],
        "all_three_state_buckets": enriched["deep_negative_breadth"] & enriched["mid_high_stress"] & enriched["high_volatility_protection"],
    }
    rows = []
    for variant, mask in specs.items():
        frame = enriched[mask].copy()
        if frame.empty:
            continue
        frame["state_gate_variant"] = variant
        rows.append(frame)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def summarize(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if panel.empty:
        return pd.DataFrame()
    for (variant, strategy, top_n), g in panel.groupby(["state_gate_variant", "strategy", "top_n"]):
        yearly = g.groupby("year")["relative_return"].mean()
        oos = g[g["year"] >= 2022]
        row = {
            "state_gate_variant": variant,
            "strategy": strategy,
            "top_n": int(top_n),
            "event_count": int(len(g)),
            "year_count": int(g["year"].nunique()),
            "mean_selected_net_return": float(g["selected_net_return"].mean()),
            "mean_benchmark_return": float(g["benchmark_return"].mean()),
            "mean_relative_return": float(g["relative_return"].mean()),
            "median_relative_return": float(g["relative_return"].median()),
            "relative_win_rate": float(g["relative_win"].mean()),
            "mean_rank_ic": float(g["rank_ic"].mean()),
            "positive_rank_ic_rate": float(g["rank_ic_positive"].mean()),
            "top_quintile_hit_rate": float(g["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
            "oos_mean_rank_ic": float(oos["rank_ic"].mean()) if len(oos) else 0.0,
        }
        row["passes_strong_rebound_gate"] = passes_gate(row)
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values(["passes_strong_rebound_gate", "mean_relative_return"], ascending=[False, False]).reset_index(drop=True)


def passes_gate(row: dict[str, float | int | str | bool]) -> bool:
    return not failed_metrics(row)


def failed_metrics(row: dict[str, float | int | str | bool]) -> list[str]:
    checks = [
        ("event_count", row.get("event_count", 0), 30, ">="),
        ("mean_relative_return", row.get("mean_relative_return", 0), 0, ">"),
        ("median_relative_return", row.get("median_relative_return", 0), 0, ">"),
        ("relative_win_rate", row.get("relative_win_rate", 0), 0.55, ">="),
        ("mean_rank_ic", row.get("mean_rank_ic", 0), 0, ">"),
        ("positive_rank_ic_rate", row.get("positive_rank_ic_rate", 0), 0.55, ">="),
        ("top_quintile_hit_rate", row.get("top_quintile_hit_rate", 0), 0.30, ">="),
        ("positive_year_rate", row.get("positive_year_rate", 0), 0.60, ">="),
        ("oos_event_count", row.get("oos_event_count", 0), 8, ">="),
        ("oos_mean_relative_return", row.get("oos_mean_relative_return", 0), 0, ">"),
        ("oos_relative_win_rate", row.get("oos_relative_win_rate", 0), 0.50, ">="),
        ("oos_mean_rank_ic", row.get("oos_mean_rank_ic", 0), 0, ">"),
    ]
    failed = []
    for metric, current, required, op in checks:
        value = float(current)
        ok = value >= float(required) if op == ">=" else value > float(required)
        if not ok:
            failed.append(metric)
    return failed


def build_gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0].to_dict()
    rows = []
    requirements = {
        "event_count": (30, ">="),
        "mean_relative_return": (0, ">"),
        "median_relative_return": (0, ">"),
        "relative_win_rate": (0.55, ">="),
        "mean_rank_ic": (0, ">"),
        "positive_rank_ic_rate": (0.55, ">="),
        "top_quintile_hit_rate": (0.30, ">="),
        "positive_year_rate": (0.60, ">="),
        "oos_event_count": (8, ">="),
        "oos_mean_relative_return": (0, ">"),
        "oos_relative_win_rate": (0.50, ">="),
        "oos_mean_rank_ic": (0, ">"),
    }
    failed = set(str(best.get("failed_metrics", "")).split(";"))
    for metric, (required, op) in requirements.items():
        rows.append({
            "state_gate_variant": best.get("state_gate_variant", ""),
            "strategy": best.get("strategy", ""),
            "top_n": best.get("top_n", ""),
            "metric": metric,
            "current": best.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "fail" if metric in failed else "pass",
        })
    return pd.DataFrame(rows)


def latest_state_row() -> dict[str, object]:
    source = pd.read_csv(V471_SOURCE, encoding="utf-8-sig")
    row = source.sort_values("trade_date").iloc[-1].to_dict()
    deep = float(row.get("negative_breadth_60d", 0.0)) >= 0.75
    stress = float(row.get("market_stress_score", 0.0))
    mid_high = stress > 0.55 and stress <= 0.70
    high_vol = float(row.get("market_volatility_20d_vs_60d", 0.0)) >= 1.30
    row.update({
        "deep_negative_breadth": deep,
        "mid_high_stress": mid_high,
        "high_volatility_protection": high_vol,
        "any_passed_state_bucket": deep or mid_high or high_vol,
    })
    return row


def build_current_candidates(latest: pd.DataFrame, names: dict[str, str], current_state: dict[str, object], results: pd.DataFrame) -> pd.DataFrame:
    out = latest.copy()
    out["industry_code"] = out["industry_code"].astype(str).str.zfill(6)
    out["industry_name"] = out["industry_code"].map(names).fillna(out.get("industry_name", ""))
    state_ok = bool(current_state.get("any_passed_state_bucket"))
    gate_ok = bool(len(results) and results.iloc[0].get("passes_strong_rebound_gate", False))
    out["state_gate_feature_date"] = str(current_state.get("trade_date", ""))
    out["deep_negative_breadth"] = bool(current_state.get("deep_negative_breadth"))
    out["mid_high_stress"] = bool(current_state.get("mid_high_stress"))
    out["high_volatility_protection"] = bool(current_state.get("high_volatility_protection"))
    out["state_gate_status"] = "pass_state_bucket" if state_ok else "blocked_current_state_not_in_passed_bucket"
    out["candidate_status"] = "research_only_state_gated_candidate" if state_ok and gate_ok else "research_only_state_gate_blocked"
    out["manual_review_reason"] = (
        "当前状态桶通过且强行业评价通过，仍只允许人工复核。"
        if state_ok and gate_ok
        else "当前状态桶或强行业评价未通过，不能把候选当作强反弹行业。"
    )
    cols = [
        "candidate_status",
        "state_gate_status",
        "selection_strategy",
        "planned_entry_date",
        "feature_date",
        "state_gate_feature_date",
        "industry_code",
        "industry_name",
        "selection_score",
        "valuation_score",
        "oversold_score",
        "turn_score",
        "liquidity_score",
        "deep_negative_breadth",
        "mid_high_stress",
        "high_volatility_protection",
        "manual_review_reason",
    ]
    return out[[c for c in cols if c in out.columns]]


def industry_name_map() -> dict[str, str]:
    df = pd.read_csv(VALUATION_HISTORY, encoding="utf-8-sig", dtype={"industry_code": str}, usecols=["industry_code", "industry_name", "trade_date"])
    df["industry_code"] = df["industry_code"].str.zfill(6)
    latest = df.sort_values("trade_date").drop_duplicates("industry_code", keep="last")
    return latest.set_index("industry_code")["industry_name"].to_dict()


def build_summary(results: pd.DataFrame, latest: pd.DataFrame, current_state: dict[str, object]) -> dict[str, object]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_strong_rebound_gate", False))
    return {
        "version": "4.73.0",
        "policy_id": "industry_rebound_leader_state_gated_v4_73",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_policy": "industry_rebound_leader_selection_v4_72",
        "best_state_gate_variant": best.get("state_gate_variant", ""),
        "best_strategy": best.get("strategy", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_year_count": int(best.get("year_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_relative_win_rate": float(best.get("relative_win_rate", 0.0) or 0.0),
        "best_mean_rank_ic": float(best.get("mean_rank_ic", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_positive_year_rate": float(best.get("positive_year_rate", 0.0) or 0.0),
        "best_oos_event_count": int(best.get("oos_event_count", 0) or 0),
        "best_oos_mean_relative_return": float(best.get("oos_mean_relative_return", 0.0) or 0.0),
        "best_failed_metrics": best.get("failed_metrics", ""),
        "best_status": "pass_stronger_industry_gate" if passed else "research_only_not_validated",
        "latest_candidate_count": int(len(latest)),
        "latest_state_date": str(current_state.get("trade_date", "")),
        "latest_deep_negative_breadth": bool(current_state.get("deep_negative_breadth")),
        "latest_mid_high_stress": bool(current_state.get("mid_high_stress")),
        "latest_high_volatility_protection": bool(current_state.get("high_volatility_protection")),
        "latest_any_passed_state_bucket": bool(current_state.get("any_passed_state_bucket")),
        "evaluation_gate": GATE_TEXT,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": (
            "状态门控后已通过强反弹行业评价，但仍保持 research_only，需前推验证。"
            if passed
            else "状态门控改善了部分指标，但尚未按原评价体系证明能稳定选出强反弹行业。"
        ),
    }


def render_report(summary: dict[str, object], results: pd.DataFrame, gate: pd.DataFrame, latest: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.73 状态门控强反弹行业选择复检",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 复用 V4.70 已识别的反弹窗口，不重新优化窗口。",
        "- 复用 V4.72 的行业排序结果和原强行业评价门槛。",
        "- 只加入 signal_date 当天可见的状态门控：深负广度、中高压力、高波动保护区。",
        "- 不降低事件数、Top20% 命中率、分年稳定性或样本外门槛。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 状态门控策略结果",
        "",
        table(results.head(20)),
        "",
        "## 最优组合门槛审计",
        "",
        table(gate),
        "",
        "## 当前候选状态",
        "",
        table(latest),
        "",
        "## 研究边界",
        "",
        "本版本只回答：状态门控是否能让反弹窗口内的行业选择更稳定地跑赢全行业等权。它不是交易指令，也不允许自动执行。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    panel = pd.DataFrame({
        "signal_date": ["2020-01-01", "2020-01-02"],
        "entry_date": ["2020-01-03", "2020-01-04"],
        "exit_date": ["2020-01-10", "2020-01-11"],
        "year": [2020, 2020],
        "strategy": ["s", "s"],
        "top_n": [5, 5],
        "selected_net_return": [0.03, 0.01],
        "benchmark_return": [0.01, 0.02],
        "relative_return": [0.02, -0.01],
        "relative_win": [True, False],
        "rank_ic": [0.2, -0.1],
        "rank_ic_positive": [True, False],
        "top_quintile_hit_rate": [0.4, 0.2],
    })
    trades = pd.DataFrame({
        "signal_date": ["2020-01-01", "2020-01-02"],
        "entry_date": ["2020-01-03", "2020-01-04"],
        "exit_date": ["2020-01-10", "2020-01-11"],
        "market_stress_score": [0.6, 0.4],
        "negative_breadth_60d": [0.8, 0.2],
        "market_volatility_20d_vs_60d": [1.1, 1.0],
    })
    enriched = attach_state(panel, trades)
    assert enriched["deep_negative_breadth"].tolist() == [True, False]
    assert enriched["mid_high_stress"].tolist() == [True, False]
    variants = build_variant_panel(enriched)
    assert set(variants["state_gate_variant"]) == {"deep_negative_breadth_only", "mid_high_stress_only", "any_passed_state_bucket"}
    row = {
        "event_count": 30,
        "mean_relative_return": 0.01,
        "median_relative_return": 0.01,
        "relative_win_rate": 0.6,
        "mean_rank_ic": 0.1,
        "positive_rank_ic_rate": 0.6,
        "top_quintile_hit_rate": 0.31,
        "positive_year_rate": 0.7,
        "oos_event_count": 8,
        "oos_mean_relative_return": 0.01,
        "oos_relative_win_rate": 0.5,
        "oos_mean_rank_ic": 0.1,
    }
    assert passes_gate(row)
    row["top_quintile_hit_rate"] = 0.29
    assert "top_quintile_hit_rate" in failed_metrics(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
