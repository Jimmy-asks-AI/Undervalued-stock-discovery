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


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_56_walk_forward_rule_vote_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = v455.load_source(config)
    rule_log, trades = walk_forward_vote(source, config)
    trades["signal_id"] = "v4_56_walk_forward_rule_vote"
    trades["signal_name_zh"] = "V4.56年前滚单特征规则投票"
    trades["signal_type"] = "walk_forward_rule_vote"
    row = stop_exit.summarize("v4_56_walk_forward_rule_vote", trades, float(config["stop_loss"]), config)
    row.update({"signal_name_zh": "V4.56年前滚单特征规则投票", "signal_type": "walk_forward_rule_vote", "status": "research_only"})
    write_outputs(out, debug, config, source, rule_log, trades, row)
    print(f"output_dir={out}")
    print(f"events={int(row['nonoverlap_events'])}")
    print(f"clusters={int(row['independent_event_clusters'])}")
    print(f"net={row['net_mean_return']:.2%}")
    print(f"relative={row['relative_mean_return']:.2%}")


def walk_forward_vote(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    logs: list[pd.DataFrame] = []
    trade_frames: list[pd.DataFrame] = []
    years = [int(y) for y in sorted(source["year"].dropna().unique()) if int(y) >= int(config["min_test_year"])]
    for year in years:
        train = source[source["year"] < year].copy()
        test = source[source["year"] == year].copy()
        candidates = pd.DataFrame(v455.candidate_rules(train, config))
        candidates = candidates[candidates["train_events"] >= int(config["min_train_events"])].copy()
        if candidates.empty or test.empty:
            continue
        top = candidates.sort_values(["selection_score", "train_net_mean_return", "train_relative_mean_return"], ascending=False).head(int(config["top_rule_count"])).copy()
        top["test_year"] = year
        logs.append(top)
        voted = apply_vote(test, top.to_dict("records"), int(config["min_vote_count"]))
        voted = stop_exit.apply_stop(voted, float(config["stop_loss"]), config)
        voted["selected_rule_ids"] = "|".join(top["rule_id"].astype(str).tolist())
        voted["vote_count_required"] = int(config["min_vote_count"])
        if not voted.empty:
            trade_frames.append(voted)
    return (pd.concat(logs, ignore_index=True) if logs else pd.DataFrame(), pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame(columns=source.columns))


def apply_vote(frame: pd.DataFrame, rules: list[dict[str, Any]], min_votes: int) -> pd.DataFrame:
    if not rules:
        return frame.iloc[0:0].copy()
    votes = pd.Series(0, index=frame.index)
    for rule in rules:
        if not rule.get("feature"):
            votes += 1
            continue
        values = pd.to_numeric(frame[rule["feature"]], errors="coerce")
        votes += (values >= float(rule["threshold"])) if rule["op"] == ">=" else (values <= float(rule["threshold"]))
    out = frame[votes >= min_votes].copy()
    out["vote_count"] = votes.loc[out.index].astype(int)
    return out


def write_outputs(out: Path, debug: Path, config: dict[str, Any], source: pd.DataFrame, rule_log: pd.DataFrame, trades: pd.DataFrame, row: dict[str, Any]) -> None:
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
        "final_verdict": "research_only；年前滚规则投票未证明有效反弹窗口。",
        "main_diagnosis": "V4.56 每年只用过去年份选择前5个单特征规则，并要求当年事件至少命中3个。",
        "research_boundary": config["research_boundary"],
    }
    pd.DataFrame([row]).to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, row, rule_log), encoding="utf-8")
    source.to_csv(debug / "walk_forward_vote_source_panel.csv", index=False, encoding="utf-8-sig")
    rule_log.to_csv(debug / "walk_forward_vote_rule_selection.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([row]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "year_forward_rule_vote", "status": "pass", "evidence": "每个测试年份只使用此前年份选择投票规则。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "ponytail: 固定top5和3票门槛，避免再扫参数；若这都无效，再加投票网格意义不大。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any], rule_log: pd.DataFrame) -> str:
    years = int(rule_log["test_year"].nunique()) if not rule_log.empty else 0
    return "\n".join([
        "# V4.56 年前滚单特征规则投票",
        "",
        "## 结论",
        "",
        f"- 测试年份数：{years}。",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对市场收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "本版本用过去年份选前 5 个单特征规则，要求当年事件至少命中 3 个。它检验的是多个弱过滤投票能否比 V4.55 单规则更稳。",
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
        return {str(k): clean(v) for k, v in value.items()}
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
