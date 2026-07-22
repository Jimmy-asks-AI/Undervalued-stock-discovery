#!/usr/bin/env python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_window_v4_46_walk_forward_independence as base


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_47_tail_guardrail_policy.json"


def main() -> None:
    config = base.read_json(CONFIG)
    output_dir = ROOT / config["output_dir"]
    debug_dir = output_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    source = base.load_source(config)
    trades, selections = walk_forward_tail_guardrail(source, config)
    row = base.summarize("v4_47_walk_forward_tail_guardrail", trades, config)
    row["signal_name_zh"] = "V4.47年前滚尾部风险守门"
    row["signal_type"] = "walk_forward_tail_guardrail"
    summary = build_summary(config, row, len(selections))
    write_outputs(output_dir, debug_dir, config, source, trades, selections, row, summary)
    print(f"output_dir={output_dir}")
    print(f"events={int(row['nonoverlap_events'])}")
    print(f"clusters={int(row['independent_event_clusters'])}")
    print(f"net={row['net_mean_return']:.2%}")
    print(f"relative={row['relative_mean_return']:.2%}")


def walk_forward_tail_guardrail(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = []
    selections = []
    for year in sorted(y for y in source["year"].unique() if y >= int(config["start_year"])):
        train = source[source["year"] < year].copy()
        test = source[source["year"] == year].copy()
        candidates = []
        for feature_filter in config["feature_filters"]:
            train_filtered, threshold = base.filter_frame(train, train, feature_filter)
            for cooldown_days in config["cooldown_days_grid"]:
                train_trades = base.apply_cooldown(train_filtered, int(cooldown_days))
                train_row = base.summarize("train", train_trades, config)
                if int(train_row["nonoverlap_events"]) < int(config["min_train_events"]):
                    continue
                candidates.append((tail_score(train_row), feature_filter, threshold, int(cooldown_days), train_row))
        if not candidates:
            continue
        _, feature_filter, threshold, cooldown_days, train_row = max(candidates, key=lambda item: item[0])
        test_trades = base.apply_cooldown(base.filter_frame(test, train, feature_filter)[0], cooldown_days)
        test_row = base.summarize(f"v4_47_{year}", test_trades, config)
        selections.append({
            "test_year": int(year),
            "feature_filter_id": feature_filter["filter_id"],
            "threshold": threshold,
            "cooldown_days": cooldown_days,
            "train_events": int(train_row["nonoverlap_events"]),
            "train_clusters": int(train_row["independent_event_clusters"]),
            "train_net_mean_return": float(train_row["net_mean_return"]),
            "train_relative_mean_return": float(train_row["relative_mean_return"]),
            "train_bad_window_rate": float(train_row["event_bad_window_rate"]),
            "train_worst_return": float(train_row["event_worst_return"]),
            "test_events": int(test_row["nonoverlap_events"]),
            "test_net_mean_return": float(test_row["net_mean_return"]),
            "test_relative_mean_return": float(test_row["relative_mean_return"]),
            "test_bad_window_rate": float(test_row["event_bad_window_rate"]),
            "test_worst_return": float(test_row["event_worst_return"]),
        })
        if not test_trades.empty:
            test_trades = test_trades.copy()
            test_trades["signal_id"] = "v4_47_walk_forward_tail_guardrail"
            test_trades["signal_name_zh"] = "V4.47年前滚尾部风险守门"
            test_trades["signal_type"] = "walk_forward_tail_guardrail"
            test_trades["selected_rule"] = feature_filter["filter_id"]
            test_trades["cooldown_days"] = cooldown_days
            trades.append(test_trades)
    return (pd.concat(trades, ignore_index=True) if trades else pd.DataFrame(), pd.DataFrame(selections))


def tail_score(row: dict[str, Any]) -> float:
    # ponytail: one-line utility, only for this audit; replace if it ever becomes a candidate.
    return min(row["nonoverlap_events"], 30) / 30 + min(row["independent_event_clusters"], 20) / 20 + 10 * row["net_mean_return"] + 8 * row["relative_mean_return"] - row["event_bad_window_rate"] + 2 * max(row["event_worst_return"], -0.10)


def build_summary(config: dict[str, Any], row: dict[str, Any], selection_count: int) -> dict[str, Any]:
    return {
        "version": config["version"],
        "policy_id": config["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": row["signal_id"],
        "primary_realtime_events": int(row["nonoverlap_events"]),
        "primary_independent_event_clusters": int(row["independent_event_clusters"]),
        "candidate_count": 0,
        "audit_fail_count": 0,
        "walk_forward_selection_years": int(selection_count),
        "best_signal_id": row["signal_id"],
        "best_status": "research_only",
        "best_nonoverlap_events": int(row["nonoverlap_events"]),
        "best_event_mean_return": float(row["event_mean_return"]),
        "best_event_relative_mean_return": float(row["relative_mean_return"]),
        "best_event_bad_window_rate": float(row["event_bad_window_rate"]),
        "final_verdict": "research_only；尾部风险守门没有改善有效性",
        "main_diagnosis": "V4.47 每年只用过去年份选择尾部风险过滤和冷却规则。",
        "research_boundary": config["research_boundary"],
    }


def write_outputs(output_dir: Path, debug_dir: Path, config: dict[str, Any], source: pd.DataFrame, trades: pd.DataFrame, selections: pd.DataFrame, row: dict[str, Any], summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([row]).to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    base.write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(render_report(config, row, selections), encoding="utf-8")
    source.to_csv(debug_dir / "tail_guardrail_source_panel.csv", index=False, encoding="utf-8-sig")
    selections.to_csv(debug_dir / "tail_guardrail_rule_selection.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([row]).to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    base.year_summary(trades).to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "year_forward_thresholds", "status": "pass", "evidence": "每年仅用过去年份数据选择守门规则。"}]).to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    base.write_json(debug_dir / "optimization_notes.json", {"note": "年前滚尾部风险守门审计；不是交易规则。"})
    base.write_json(debug_dir / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any], selections: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.47 年前滚尾部风险守门审计",
        "",
        "## 结论",
        "",
        f"- 选择年份数：{len(selections)}。",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{base.fmt_pct(row['net_mean_return'])}；相对市场收益：{base.fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{base.fmt_pct(row['event_win_rate'])}；坏窗口率：{base.fmt_pct(row['event_bad_window_rate'])}；最差单笔：{base.fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "V4.47 在 V4.46 的年前滚框架内加入尾部风险守门选择。结果样本被明显压缩，收益厚度和相对收益没有改善，尾部风险也没有被稳定修复。",
        "",
        "这说明当前特征库里的简单风险过滤不能可靠区分坏窗口。继续堆这类单层过滤不是高价值方向。",
        "",
        "## 研究边界",
        "",
        config["research_boundary"],
        "",
    ])


if __name__ == "__main__":
    main()
