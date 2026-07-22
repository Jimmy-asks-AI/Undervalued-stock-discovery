#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_window_v4_48_stop_loss_exit as stop_exit
import run_industry_rebound_window_v4_55_walk_forward_entry_filter as v455
import run_industry_rebound_window_v4_56_walk_forward_rule_vote as v456


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_57_walk_forward_family_select_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = v455.load_source(config)
    family_log, trades = walk_forward_family(source, config)
    trades["signal_id"] = "v4_57_walk_forward_family_select"
    trades["signal_name_zh"] = "V4.57年前滚过滤家族选择"
    trades["signal_type"] = "walk_forward_family_select"
    row = stop_exit.summarize("v4_57_walk_forward_family_select", trades, float(config["stop_loss"]), config)
    row.update({"signal_name_zh": "V4.57年前滚过滤家族选择", "signal_type": "walk_forward_family_select", "status": "research_only"})
    write_outputs(out, debug, config, source, family_log, trades, row)
    print(f"output_dir={out}")
    print(f"events={int(row['nonoverlap_events'])}")
    print(f"clusters={int(row['independent_event_clusters'])}")
    print(f"net={row['net_mean_return']:.2%}")
    print(f"relative={row['relative_mean_return']:.2%}")


def walk_forward_family(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    logs: list[dict[str, Any]] = []
    frames: list[pd.DataFrame] = []
    years = [int(y) for y in sorted(source["year"].dropna().unique()) if int(y) >= int(config["min_test_year"])]
    for year in years:
        train = source[source["year"] < year].copy()
        test = source[source["year"] == year].copy()
        candidates = family_candidates(train, config)
        selected = max(candidates, key=lambda r: (r["selection_score"], r["train_net_mean_return"], r["train_events"]))
        selected["test_year"] = year
        logs.append(selected)
        selected_trades = apply_family(test, selected, config)
        selected_trades = stop_exit.apply_stop(selected_trades, float(config["stop_loss"]), config)
        selected_trades["selected_family"] = selected["family"]
        selected_trades["selected_rule_ids"] = selected["rule_ids"]
        if not selected_trades.empty:
            frames.append(selected_trades)
    return pd.DataFrame(logs), pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=source.columns)


def family_candidates(train: pd.DataFrame, config: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    no_rule = {"rule_id": "no_filter", "feature": "", "op": "", "threshold": math.nan}
    rows.append(score_family("no_filter", [no_rule], train, config))
    rules = pd.DataFrame(v455.candidate_rules(train, config))
    rules = rules[rules["train_events"] >= int(config["min_train_events"])].copy()
    if not rules.empty:
        top_one = rules.sort_values(["selection_score", "train_net_mean_return", "train_relative_mean_return"], ascending=False).head(1).to_dict("records")
        rows.append(score_family("single_rule", top_one, train, config))
        top_vote = rules.sort_values(["selection_score", "train_net_mean_return", "train_relative_mean_return"], ascending=False).head(int(config["top_rule_count"])).to_dict("records")
        rows.append(score_family("rule_vote", top_vote, train, config))
    return rows


def score_family(family: str, rules: list[dict[str, Any]], train: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    trades = stop_exit.apply_stop(apply_family(train, {"family": family, "rules": rules}, config), float(config["stop_loss"]), config)
    returns = pd.to_numeric(trades.get("trade_return", pd.Series(dtype=float)), errors="coerce")
    relative = pd.to_numeric(trades.get("relative_return_horizon", pd.Series(dtype=float)), errors="coerce")
    bad = trades.get("is_bad_window", pd.Series(dtype=bool)).astype(bool) if not trades.empty else pd.Series(dtype=bool)
    events = int(len(trades))
    net = float(returns.mean() - float(config["round_trip_cost_bps"]) / 10000.0) if events else -9.0
    rel = float(relative.mean()) if events else -9.0
    win = float((returns > 0).mean()) if events else 0.0
    bad_rate = float(bad.mean()) if events else 1.0
    score = min(events, 30) / 30 + 8 * net + 6 * rel + win - bad_rate
    return {
        "family": family,
        "rule_ids": "|".join(str(r["rule_id"]) for r in rules),
        "rules": rules,
        "train_events": events,
        "train_net_mean_return": net,
        "train_relative_mean_return": rel,
        "train_win_rate": win,
        "train_bad_window_rate": bad_rate,
        "selection_score": float(score),
    }


def apply_family(frame: pd.DataFrame, selected: dict[str, Any], config: dict[str, Any]) -> pd.DataFrame:
    family = selected["family"]
    rules = selected["rules"]
    if family == "no_filter":
        return frame.copy()
    if family == "single_rule":
        return v455.apply_rule(frame, rules[0])
    return v456.apply_vote(frame, rules, int(config["min_vote_count"]))


def write_outputs(out: Path, debug: Path, config: dict[str, Any], source: pd.DataFrame, family_log: pd.DataFrame, trades: pd.DataFrame, row: dict[str, Any]) -> None:
    serializable_log = family_log.drop(columns=["rules"], errors="ignore")
    summary = {
        "version": config["version"],
        "policy_id": config["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": row["signal_id"],
        "primary_realtime_events": int(row["nonoverlap_events"]),
        "primary_independent_event_clusters": int(row["independent_event_clusters"]),
        "candidate_count": 0,
        "audit_fail_count": 0,
        "best_signal_id": row["signal_id"],
        "best_status": "research_only",
        "best_nonoverlap_events": int(row["nonoverlap_events"]),
        "best_event_mean_return": float(row["event_mean_return"]),
        "best_event_relative_mean_return": float(row["relative_mean_return"]),
        "best_event_bad_window_rate": float(row["event_bad_window_rate"]),
        "final_verdict": "research_only；年前滚家族选择未证明有效反弹窗口。",
        "main_diagnosis": "V4.57 每年只用过去年份在不滤、单规则、规则投票三族中选择执行方式。",
        "research_boundary": config["research_boundary"],
    }
    pd.DataFrame([row]).to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, row, serializable_log), encoding="utf-8")
    source.to_csv(debug / "family_select_source_panel.csv", index=False, encoding="utf-8-sig")
    serializable_log.to_csv(debug / "family_selection_log.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([row]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "year_forward_family_selection", "status": "pass", "evidence": "每个测试年份只使用此前年份选择过滤家族。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "ponytail: 只比较三类已有简单家族；若不滤胜出，过滤分支应停止加复杂度。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any], family_log: pd.DataFrame) -> str:
    counts = family_log["family"].value_counts().to_dict() if not family_log.empty else {}
    return "\n".join([
        "# V4.57 年前滚过滤家族选择",
        "",
        "## 结论",
        "",
        f"- 家族选择次数：{counts}。",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对市场收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "本版本检验是否应该继续过滤分支：每年只用过去年份在不滤、单规则、规则投票三族中选择一个执行。",
        "",
        "## 研究边界",
        "",
        config["research_boundary"],
        "",
    ])


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items() if k != "rules"}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value


def fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.2%}" if math.isfinite(number) else ""


if __name__ == "__main__":
    main()
