#!/usr/bin/env python
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_leader_selection_v4_72 as v472
import run_industry_rebound_window_v4_60_breadth_relief_event as event_builder


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "source_panel.csv"
BASE_POLICY = ROOT / "configs" / "rebound_window_v4_70_delayed_entry_vol_stop_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_leader_expanded_window_v4_97"
DEBUG = OUT / "debug"

VOL_REPAIR_CONDITIONS = [
    {"field": "market_volatility_20d_vs_60d", "op": ">=", "value": 1.05},
    {"field": "liquidity_repair_5d", "op": ">=", "value": 0.03},
    {"field": "market_return_10d", "op": "<=", "value": 0.03},
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.97 strong-industry backtest inside expanded vol_repair rebound windows.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    trades = build_expanded_trades()
    features = v472.build_features(v472.load_history(v472.VALUATION_HISTORY))
    event_panel = v472.evaluate_events(features, trades, [5, 10, 20], 10.0)
    opportunity = v472.build_event_opportunity_set(features, trades)
    results = v472.summarize_strategies(event_panel)
    gate = gate_audit(results)
    summary = build_summary(trades, results, gate)
    write_outputs(summary, trades, event_panel, opportunity, results, gate)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_strategy={summary['best_strategy']}")


def build_expanded_trades() -> pd.DataFrame:
    panel = pd.read_csv(PANEL, encoding="utf-8-sig")
    policy = copy.deepcopy(read_json(BASE_POLICY))
    policy["policy_id"] = "vol_repair_expanded_window_v4_97"
    policy["conditions"] = VOL_REPAIR_CONDITIONS
    raw = event_builder.build_trades(panel, policy)
    return first_non_overlapping(raw)


def first_non_overlapping(trades: pd.DataFrame) -> pd.DataFrame:
    frame = trades.copy()
    frame["entry_dt"] = pd.to_datetime(frame["entry_date"])
    frame["exit_dt"] = pd.to_datetime(frame["exit_date"])
    frame = frame.sort_values(["entry_dt", "signal_date"]).reset_index(drop=True)
    rows = []
    end = None
    for _, row in frame.iterrows():
        if end is None or row["entry_dt"] > end:
            rows.append(row.to_dict())
            end = row["exit_dt"]
        else:
            end = max(end, row["exit_dt"])
    return pd.DataFrame(rows)


def gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0]
    checks = [
        ("event_count", best["event_count"], 30, ">=", best["event_count"] >= 30),
        ("mean_relative_return", best["mean_relative_return"], 0.0, ">", best["mean_relative_return"] > 0),
        ("median_relative_return", best["median_relative_return"], 0.0, ">", best["median_relative_return"] > 0),
        ("relative_win_rate", best["relative_win_rate"], 0.55, ">=", best["relative_win_rate"] >= 0.55),
        ("mean_rank_ic", best["mean_rank_ic"], 0.0, ">", best["mean_rank_ic"] > 0),
        ("positive_rank_ic_rate", best["positive_rank_ic_rate"], 0.55, ">=", best["positive_rank_ic_rate"] >= 0.55),
        ("top_quintile_hit_rate", best["top_quintile_hit_rate"], 0.30, ">=", best["top_quintile_hit_rate"] >= 0.30),
        ("oos_mean_relative_return", best["oos_mean_relative_return"], 0.0, ">", best["oos_mean_relative_return"] > 0),
    ]
    return pd.DataFrame([
        {
            "metric": metric,
            "current": current,
            "required": required,
            "operator": op,
            "status": "pass" if ok else "fail",
        }
        for metric, current, required, op, ok in checks
    ])


def build_summary(trades: pd.DataFrame, results: pd.DataFrame, gate: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_strong_rebound_gate", False))
    return {
        "version": "4.97.0",
        "policy_id": "industry_rebound_leader_expanded_window_v4_97",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_variant": "vol_repair",
        "window_conditions": VOL_REPAIR_CONDITIONS,
        "independent_window_count": int(len(trades)),
        "best_strategy": best.get("strategy", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_relative_win_rate": float(best.get("relative_win_rate", 0.0) or 0.0),
        "best_oos_mean_relative_return": float(best.get("oos_mean_relative_return", 0.0) or 0.0),
        "passing_rule_count": int(results["passes_strong_rebound_gate"].sum()) if len(results) else 0,
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_expanded_window_strong_industry_gate" if passed else "research_only_expanded_window_no_strong_industry_alpha",
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "V4.97 在样本容量达标的 vol_repair 扩展窗口内重跑强行业选择，仍未找到能稳定跑赢全行业等权的行业排序规则；扩展窗口解决了样本数，但没有解决行业选择 alpha。",
    }


def write_outputs(summary: dict[str, Any], trades: pd.DataFrame, event_panel: pd.DataFrame, opportunity: pd.DataFrame, results: pd.DataFrame, gate: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    summary_table = results.copy()
    summary_table.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, results, gate), encoding="utf-8")
    trades.to_csv(DEBUG / "expanded_window_trades.csv", index=False, encoding="utf-8-sig")
    event_panel.to_csv(DEBUG / "industry_event_panel.csv", index=False, encoding="utf-8-sig")
    opportunity.to_csv(DEBUG / "industry_event_opportunity_set.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "strategy_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.97 扩展反弹窗口强行业选择回测",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 窗口定义：`{summary['window_variant']}`",
        f"- 独立窗口数：{summary['independent_window_count']}",
        f"- 最优策略：`{summary['best_strategy']}` Top{summary['best_top_n']}",
        f"- 最优平均相对收益：{pct(summary['best_mean_relative_return'])}",
        f"- 最优 Top20% 命中率：{pct(summary['best_top_quintile_hit_rate'])}",
        f"- 最优胜率：{pct(summary['best_relative_win_rate'])}",
        f"- 样本外平均相对收益：{pct(summary['best_oos_mean_relative_return'])}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 最优规则门槛",
        "",
        gate.to_markdown(index=False) if len(gate) else "无数据",
        "",
        "## 策略结果",
        "",
        results.to_markdown(index=False) if len(results) else "无数据",
        "",
        "## 研究边界",
        "",
        "V4.97 使用 V4.96 预先定义的 vol_repair 扩展窗口，行业排序仍只使用信号日可见的行业价格、估值、企稳和流动性特征。该版本不使用未来收益挑窗口或挑行业，不生成交易指令。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def self_check() -> None:
    mini = pd.DataFrame({
        "signal_date": ["2020-01-01", "2020-01-02", "2020-03-01"],
        "entry_date": ["2020-01-03", "2020-01-06", "2020-03-03"],
        "exit_date": ["2020-02-03", "2020-02-06", "2020-04-03"],
    })
    assert len(first_non_overlapping(mini)) == 2
    print("self_check=pass")


if __name__ == "__main__":
    main()
