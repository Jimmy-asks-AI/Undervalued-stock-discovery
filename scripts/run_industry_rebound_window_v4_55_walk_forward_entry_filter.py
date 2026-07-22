#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_window_v4_48_stop_loss_exit as stop_exit


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_55_walk_forward_entry_filter_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = load_source(config)
    rule_log, trades = walk_forward(source, config)
    trades["signal_id"] = "v4_55_walk_forward_entry_filter"
    trades["signal_name_zh"] = "V4.55年前滚单特征入场过滤"
    trades["signal_type"] = "walk_forward_entry_filter"
    row = stop_exit.summarize(trades["signal_id"].iloc[0] if len(trades) else "v4_55_walk_forward_entry_filter", trades, float(config["stop_loss"]), config)
    row.update({"signal_name_zh": "V4.55年前滚单特征入场过滤", "signal_type": "walk_forward_entry_filter", "status": "research_only"})
    write_outputs(out, debug, config, source, rule_log, trades, row)
    print(f"output_dir={out}")
    print(f"events={int(row['nonoverlap_events'])}")
    print(f"clusters={int(row['independent_event_clusters'])}")
    print(f"net={row['net_mean_return']:.2%}")
    print(f"relative={row['relative_mean_return']:.2%}")


def load_source(config: dict[str, Any]) -> pd.DataFrame:
    frame = pd.read_csv(ROOT / config["source_panel"], encoding="utf-8-sig")
    frame = frame[pd.to_numeric(frame["holding_days"], errors="coerce") == int(config["holding_days"])].copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce")
    frame = frame.dropna(subset=["signal_date"]).sort_values("signal_date").drop_duplicates("signal_date")
    frame["year"] = frame["signal_date"].dt.year
    frame["benchmark_return_horizon"] = pd.to_numeric(frame["benchmark_return_horizon"], errors="coerce")
    frame["relative_return_horizon"] = pd.to_numeric(frame["trade_return"], errors="coerce") - frame["benchmark_return_horizon"]
    return frame


def walk_forward(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rule_rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    years = [int(y) for y in sorted(source["year"].dropna().unique()) if int(y) >= int(config["min_test_year"])]
    for year in years:
        train = source[source["year"] < year].copy()
        test = source[source["year"] == year].copy()
        if test.empty:
            continue
        candidates = candidate_rules(train, config)
        valid = [r for r in candidates if int(r["train_events"]) >= int(config["min_train_events"])]
        selected = max(valid or candidates, key=lambda r: (r["selection_score"], r["train_net_mean_return"], r["train_relative_mean_return"], r["train_events"]))
        selected["test_year"] = year
        selected["valid_train_rule_count"] = len(valid)
        test_trades = apply_rule(test, selected)
        test_trades = stop_exit.apply_stop(test_trades, float(config["stop_loss"]), config)
        test_trades["selected_rule_id"] = selected["rule_id"]
        test_trades["selected_feature"] = selected["feature"]
        test_trades["selected_op"] = selected["op"]
        test_trades["selected_threshold"] = selected["threshold"]
        rule_rows.append(selected)
        if not test_trades.empty:
            trade_frames.append(test_trades)
    return pd.DataFrame(rule_rows), pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame(columns=source.columns)


def candidate_rules(train: pd.DataFrame, config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [score_rule(train, {"rule_id": "no_filter", "feature": "", "op": "", "threshold": math.nan}, config)]
    for feature in config["feature_columns"]:
        values = pd.to_numeric(train.get(feature, pd.Series(dtype=float)), errors="coerce").dropna()
        if values.empty:
            continue
        for q in config["quantiles"]:
            threshold = float(values.quantile(float(q)))
            for op in [">=", "<="]:
                rows.append(score_rule(train, {"rule_id": f"{feature}_{op}_q{int(float(q)*100)}", "feature": feature, "op": op, "threshold": threshold}, config))
    return rows


def score_rule(train: pd.DataFrame, rule: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    trades = stop_exit.apply_stop(apply_rule(train, rule), float(config["stop_loss"]), config)
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
        **rule,
        "train_events": events,
        "train_net_mean_return": net,
        "train_relative_mean_return": rel,
        "train_win_rate": win,
        "train_bad_window_rate": bad_rate,
        "selection_score": float(score),
    }


def apply_rule(frame: pd.DataFrame, rule: dict[str, Any]) -> pd.DataFrame:
    if not rule.get("feature"):
        return frame.copy()
    values = pd.to_numeric(frame[rule["feature"]], errors="coerce")
    if rule["op"] == ">=":
        return frame[values >= float(rule["threshold"])].copy()
    return frame[values <= float(rule["threshold"])].copy()


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
        "final_verdict": "research_only；年前滚单特征过滤未证明有效反弹窗口。",
        "main_diagnosis": "V4.55 只用过去年份选择单特征入场过滤，再应用到未来年份。",
        "research_boundary": config["research_boundary"],
    }
    pd.DataFrame([row]).to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, row, rule_log), encoding="utf-8")
    source.to_csv(debug / "walk_forward_entry_source_panel.csv", index=False, encoding="utf-8-sig")
    rule_log.to_csv(debug / "walk_forward_entry_rule_selection.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([row]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "year_forward_rule_selection", "status": "pass", "evidence": "每个测试年份只使用此前年份选择过滤规则。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "ponytail: 单特征分位网格足够检验 V4.51 的全样本过滤是否能年前滚泛化；多特征模型等单特征有效后再加。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any], rule_log: pd.DataFrame) -> str:
    years = int(rule_log["test_year"].nunique()) if not rule_log.empty else 0
    return "\n".join([
        "# V4.55 年前滚单特征入场过滤",
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
        "本版本把 V4.51 的单特征过滤从全样本上限改成年前滚选择。每一年只用该年以前的数据挑一个单条件，再应用到当年。",
        "",
        "如果结果不能通过 V3.2，说明单特征入场过滤在实时口径下没有把宽事件池的约 +1% 收益厚度显著抬高。",
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
