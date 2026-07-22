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
OUT = ROOT / "outputs" / "industry_rebound_leader_oos_factor_v4_74"
DEBUG = OUT / "debug"

FACTOR_COLUMNS = [
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
TOP_NS = [5, 10, 20]
TRAIN_END_YEAR = 2021
GATE_TEXT = "train selects factor; full event_count>=30; full/oos mean relative>0; win_rate>=55%; top_quintile_hit_rate>=30%; positive_year_rate>=60%"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.74 OOS factor selection for rebound-leader industries.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    latest = pd.read_csv(V472 / "top_candidates.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    state_opp = attach_state(opportunity, trades)
    event_panel = build_event_panel(state_opp)
    summary_table = summarize_candidates(event_panel)
    selected = select_by_train(summary_table)
    gate = build_gate_audit(selected)
    current_state = latest_state_row()
    current_candidates = build_current_candidates(latest, selected, current_state)
    summary = build_summary(selected, current_candidates, current_state)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    current_candidates.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, selected, gate, current_candidates), encoding="utf-8")
    state_opp.to_csv(DEBUG / "state_annotated_opportunity_set.csv", index=False, encoding="utf-8-sig")
    event_panel.to_csv(DEBUG / "factor_event_panel.csv", index=False, encoding="utf-8-sig")
    summary_table.to_csv(DEBUG / "factor_oos_results.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(DEBUG / "selected_factor_oos_audit.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    current_candidates.to_csv(DEBUG / "latest_oos_factor_candidates.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_factor={summary['best_factor']}")


def attach_state(opportunity: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    cols = ["signal_date", "entry_date", "exit_date", "market_stress_score", "negative_breadth_60d", "market_volatility_20d_vs_60d"]
    out = opportunity.merge(trades[cols], on=["signal_date", "entry_date", "exit_date"], how="left")
    stress = pd.to_numeric(out["market_stress_score"], errors="coerce")
    out["deep_negative_breadth"] = pd.to_numeric(out["negative_breadth_60d"], errors="coerce").ge(0.75)
    out["mid_high_stress"] = stress.gt(0.55) & stress.le(0.70)
    out["high_volatility_protection"] = pd.to_numeric(out["market_volatility_20d_vs_60d"], errors="coerce").ge(1.30)
    out["any_passed_state_bucket"] = out["deep_negative_breadth"] | out["mid_high_stress"] | out["high_volatility_protection"]
    out["year"] = pd.to_datetime(out["signal_date"]).dt.year
    return out


def build_event_panel(opportunity: pd.DataFrame) -> pd.DataFrame:
    variants = {
        "deep_negative_breadth_only": opportunity["deep_negative_breadth"],
        "mid_high_stress_only": opportunity["mid_high_stress"],
        "high_volatility_only": opportunity["high_volatility_protection"],
        "any_passed_state_bucket": opportunity["any_passed_state_bucket"],
    }
    rows = []
    for variant, mask in variants.items():
        frame = opportunity[mask].copy()
        for factor in FACTOR_COLUMNS:
            if factor not in frame.columns:
                continue
            for top_n in TOP_NS:
                rows.extend(evaluate_factor(frame, variant, factor, top_n))
    return pd.DataFrame(rows)


def evaluate_factor(frame: pd.DataFrame, variant: str, factor: str, top_n: int) -> list[dict[str, object]]:
    rows = []
    for (signal_date, entry_date, exit_date), event in frame.groupby(["signal_date", "entry_date", "exit_date"]):
        event = event.dropna(subset=[factor, "future_return"])
        if event.empty:
            continue
        ranked = event.sort_values(factor, ascending=False)
        selected = ranked.head(top_n)
        benchmark = float(event["future_return"].mean())
        selected_return = float(selected["future_return"].mean())
        relative = selected_return - benchmark - 0.001
        top_cut = event["future_return"].quantile(0.8)
        rank_ic = float(event[[factor, "future_return"]].corr(method="spearman").iloc[0, 1])
        rows.append({
            "state_gate_variant": variant,
            "factor": factor,
            "top_n": top_n,
            "signal_date": signal_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "year": int(pd.to_datetime(signal_date).year),
            "selected_return": selected_return,
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


def summarize_candidates(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if panel.empty:
        return pd.DataFrame()
    for (variant, factor, top_n), g in panel.groupby(["state_gate_variant", "factor", "top_n"]):
        train = g[g["year"] <= TRAIN_END_YEAR]
        oos = g[g["year"] > TRAIN_END_YEAR]
        full = summarize_slice(g, "full")
        train_row = summarize_slice(train, "train")
        oos_row = summarize_slice(oos, "oos")
        row = {"state_gate_variant": variant, "factor": factor, "top_n": int(top_n)}
        row.update(full)
        row.update(train_row)
        row.update(oos_row)
        row["train_passes_selection_gate"] = train_passes(row)
        row["passes_strong_rebound_gate"] = final_passes(row)
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values(["train_passes_selection_gate", "oos_mean_relative_return", "full_mean_relative_return"], ascending=[False, False, False]).reset_index(drop=True)


def summarize_slice(frame: pd.DataFrame, prefix: str) -> dict[str, object]:
    if frame.empty:
        return {
            f"{prefix}_event_count": 0,
            f"{prefix}_year_count": 0,
            f"{prefix}_mean_relative_return": 0.0,
            f"{prefix}_median_relative_return": 0.0,
            f"{prefix}_relative_win_rate": 0.0,
            f"{prefix}_mean_rank_ic": 0.0,
            f"{prefix}_positive_rank_ic_rate": 0.0,
            f"{prefix}_top_quintile_hit_rate": 0.0,
            f"{prefix}_positive_year_rate": 0.0,
        }
    yearly = frame.groupby("year")["relative_return"].mean()
    return {
        f"{prefix}_event_count": int(len(frame)),
        f"{prefix}_year_count": int(frame["year"].nunique()),
        f"{prefix}_mean_relative_return": float(frame["relative_return"].mean()),
        f"{prefix}_median_relative_return": float(frame["relative_return"].median()),
        f"{prefix}_relative_win_rate": float(frame["relative_win"].mean()),
        f"{prefix}_mean_rank_ic": float(frame["rank_ic"].mean()),
        f"{prefix}_positive_rank_ic_rate": float(frame["rank_ic_positive"].mean()),
        f"{prefix}_top_quintile_hit_rate": float(frame["top_quintile_hit_rate"].mean()),
        f"{prefix}_positive_year_rate": float((yearly > 0).mean()),
    }


def train_passes(row: dict[str, object]) -> bool:
    return (
        float(row.get("train_event_count", 0)) >= 8
        and float(row.get("train_mean_relative_return", 0)) > 0
        and float(row.get("train_relative_win_rate", 0)) >= 0.55
        and float(row.get("train_top_quintile_hit_rate", 0)) >= 0.30
    )


def final_passes(row: dict[str, object]) -> bool:
    return not failed_metrics(row)


def failed_metrics(row: dict[str, object]) -> list[str]:
    checks = [
        ("full_event_count", 30, ">="),
        ("full_mean_relative_return", 0, ">"),
        ("full_median_relative_return", 0, ">"),
        ("full_relative_win_rate", 0.55, ">="),
        ("full_mean_rank_ic", 0, ">"),
        ("full_positive_rank_ic_rate", 0.55, ">="),
        ("full_top_quintile_hit_rate", 0.30, ">="),
        ("full_positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="),
        ("oos_top_quintile_hit_rate", 0.30, ">="),
        ("oos_positive_year_rate", 0.60, ">="),
    ]
    failed = []
    for metric, required, op in checks:
        value = float(row.get(metric, 0) or 0)
        ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    return failed


def select_by_train(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    train_passed = results[results["train_passes_selection_gate"].eq(True)]
    source = train_passed if not train_passed.empty else results
    # ponytail: if no rule passes the train gate, still pick the least-bad train rule
    # only for diagnosis; never let OOS performance choose the rule.
    selected = source.sort_values(
        ["train_passes_selection_gate", "train_top_quintile_hit_rate", "train_mean_relative_return", "train_relative_win_rate"],
        ascending=[False, False, False, False],
    ).head(1)
    return selected.reset_index(drop=True)


def build_gate_audit(selected: pd.DataFrame) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()
    row = selected.iloc[0].to_dict()
    requirements = [
        ("full_event_count", 30, ">="),
        ("full_mean_relative_return", 0, ">"),
        ("full_median_relative_return", 0, ">"),
        ("full_relative_win_rate", 0.55, ">="),
        ("full_mean_rank_ic", 0, ">"),
        ("full_positive_rank_ic_rate", 0.55, ">="),
        ("full_top_quintile_hit_rate", 0.30, ">="),
        ("full_positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="),
        ("oos_top_quintile_hit_rate", 0.30, ">="),
        ("oos_positive_year_rate", 0.60, ">="),
    ]
    failed = set(str(row.get("failed_metrics", "")).split(";"))
    return pd.DataFrame([
        {
            "state_gate_variant": row.get("state_gate_variant", ""),
            "factor": row.get("factor", ""),
            "top_n": row.get("top_n", ""),
            "metric": metric,
            "current": row.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "fail" if metric in failed else "pass",
        }
        for metric, required, op in requirements
    ])


def latest_state_row() -> dict[str, object]:
    source = pd.read_csv(V471_SOURCE, encoding="utf-8-sig")
    row = source.sort_values("trade_date").iloc[-1].to_dict()
    stress = float(row.get("market_stress_score", 0.0))
    row["deep_negative_breadth"] = float(row.get("negative_breadth_60d", 0.0)) >= 0.75
    row["mid_high_stress"] = stress > 0.55 and stress <= 0.70
    row["high_volatility_protection"] = float(row.get("market_volatility_20d_vs_60d", 0.0)) >= 1.30
    row["any_passed_state_bucket"] = bool(row["deep_negative_breadth"] or row["mid_high_stress"] or row["high_volatility_protection"])
    return row


def build_current_candidates(latest: pd.DataFrame, selected: pd.DataFrame, state: dict[str, object]) -> pd.DataFrame:
    out = latest.copy()
    selected_row = selected.iloc[0].to_dict() if len(selected) else {}
    state_ok = bool(state.get("any_passed_state_bucket"))
    leader_ok = bool(selected_row.get("passes_strong_rebound_gate", False))
    out["candidate_status"] = "research_only_oos_factor_candidate" if state_ok and leader_ok else "research_only_oos_factor_blocked"
    out["oos_selected_factor"] = selected_row.get("factor", "")
    out["oos_selected_variant"] = selected_row.get("state_gate_variant", "")
    out["state_gate_feature_date"] = str(state.get("trade_date", ""))
    out["latest_any_passed_state_bucket"] = state_ok
    out["manual_review_reason"] = (
        "训练期选择的因子样本外通过且当前状态桶通过，仍只允许人工复核。"
        if state_ok and leader_ok
        else "训练期选择因子或当前状态桶未通过，不能作为强反弹行业。"
    )
    cols = [
        "candidate_status",
        "oos_selected_factor",
        "oos_selected_variant",
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
        "latest_any_passed_state_bucket",
        "manual_review_reason",
    ]
    return out[[c for c in cols if c in out.columns]]


def build_summary(selected: pd.DataFrame, latest: pd.DataFrame, state: dict[str, object]) -> dict[str, object]:
    row = selected.iloc[0].to_dict() if len(selected) else {}
    passed = bool(row.get("passes_strong_rebound_gate", False))
    return {
        "version": "4.74.0",
        "policy_id": "industry_rebound_leader_oos_factor_v4_74",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_policy": "industry_rebound_leader_selection_v4_72",
        "train_end_year": TRAIN_END_YEAR,
        "best_state_gate_variant": row.get("state_gate_variant", ""),
        "best_factor": row.get("factor", ""),
        "best_top_n": int(row.get("top_n", 0) or 0),
        "full_event_count": int(row.get("full_event_count", 0) or 0),
        "full_mean_relative_return": float(row.get("full_mean_relative_return", 0.0) or 0.0),
        "full_relative_win_rate": float(row.get("full_relative_win_rate", 0.0) or 0.0),
        "full_top_quintile_hit_rate": float(row.get("full_top_quintile_hit_rate", 0.0) or 0.0),
        "full_positive_year_rate": float(row.get("full_positive_year_rate", 0.0) or 0.0),
        "oos_event_count": int(row.get("oos_event_count", 0) or 0),
        "oos_mean_relative_return": float(row.get("oos_mean_relative_return", 0.0) or 0.0),
        "oos_relative_win_rate": float(row.get("oos_relative_win_rate", 0.0) or 0.0),
        "oos_top_quintile_hit_rate": float(row.get("oos_top_quintile_hit_rate", 0.0) or 0.0),
        "oos_positive_year_rate": float(row.get("oos_positive_year_rate", 0.0) or 0.0),
        "failed_metrics": row.get("failed_metrics", ""),
        "best_status": "pass_stronger_industry_gate" if passed else "research_only_not_validated",
        "latest_candidate_count": int(len(latest)),
        "latest_state_date": str(state.get("trade_date", "")),
        "latest_any_passed_state_bucket": bool(state.get("any_passed_state_bucket")),
        "evaluation_gate": GATE_TEXT,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": (
            "训练期选择因子在样本外和全样本通过强行业评价，但仍保持 research_only 等待前推。"
            if passed
            else "训练期选择因子未能在样本外和全样本同时证明可稳定选出强反弹行业。"
        ),
    }


def render_report(summary: dict[str, object], selected: pd.DataFrame, gate: pd.DataFrame, latest: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.74 样本外因子选择强反弹行业审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        f"- 训练期：{TRAIN_END_YEAR} 年及以前；样本外：{TRAIN_END_YEAR + 1} 年以后。",
        "- 只使用 signal_date 当天可见的行业特征和状态桶。",
        "- 先在训练期选择状态桶、因子和 TopN，再检查样本外与全样本是否过门槛。",
        "- 不使用未来收益调当前候选；未来收益只作为回测标签。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 训练期选出的规则",
        "",
        table(selected),
        "",
        "## 门槛审计",
        "",
        table(gate),
        "",
        "## 当前候选状态",
        "",
        table(latest),
        "",
        "## 研究边界",
        "",
        "本版本检验的是反弹窗口内行业选择 alpha，不是反弹窗口本身，也不是交易指令。未通过前，所有候选只能作为研究观察。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    row = {
        "full_event_count": 30,
        "full_mean_relative_return": 0.01,
        "full_median_relative_return": 0.01,
        "full_relative_win_rate": 0.6,
        "full_mean_rank_ic": 0.1,
        "full_positive_rank_ic_rate": 0.6,
        "full_top_quintile_hit_rate": 0.31,
        "full_positive_year_rate": 0.7,
        "oos_event_count": 8,
        "oos_mean_relative_return": 0.01,
        "oos_relative_win_rate": 0.6,
        "oos_top_quintile_hit_rate": 0.31,
        "oos_positive_year_rate": 0.7,
    }
    assert final_passes(row)
    row["oos_top_quintile_hit_rate"] = 0.29
    assert "oos_top_quintile_hit_rate" in failed_metrics(row)
    frame = pd.DataFrame({
        "signal_date": ["2020-01-01", "2020-01-01"],
        "entry_date": ["2020-01-02", "2020-01-02"],
        "exit_date": ["2020-01-03", "2020-01-03"],
        "industry_code": ["1", "2"],
        "industry_name": ["A", "B"],
        "future_return": [0.1, 0.0],
        "factor": [1.0, 0.0],
    })
    rows = evaluate_factor(frame, "x", "factor", 1)
    assert rows[0]["selected_industry_codes"] == "000001"
    print("self_check=pass")


if __name__ == "__main__":
    main()
