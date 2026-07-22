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
CONFIG = ROOT / "configs" / "rebound_window_v4_59_walk_forward_horizon_select_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(ROOT / config["source_panel"], encoding="utf-8-sig")
    log, trades = walk_forward(source, config)
    trades["signal_id"] = "v4_59_walk_forward_horizon_select"
    trades["signal_name_zh"] = "V4.59年前滚持有期选择"
    trades["signal_type"] = "walk_forward_horizon_select"
    row = stop_exit.summarize("v4_59_walk_forward_horizon_select", trades, float(config["stop_loss"]), config)
    row.update({"signal_name_zh": "V4.59年前滚持有期选择", "signal_type": "walk_forward_horizon_select", "status": "research_only"})
    write_outputs(out, debug, config, source, log, trades, row)
    print(f"output_dir={out}")
    print(f"events={int(row['nonoverlap_events'])}")
    print(f"clusters={int(row['independent_event_clusters'])}")
    print(f"net={row['net_mean_return']:.2%}")
    print(f"relative={row['relative_mean_return']:.2%}")


def walk_forward(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = source.copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce")
    frame = frame.dropna(subset=["signal_date"])
    frame["year"] = frame["signal_date"].dt.year
    years = [int(y) for y in sorted(frame["year"].dropna().unique()) if int(y) >= int(config["min_test_year"])]
    logs: list[dict[str, Any]] = []
    parts: list[pd.DataFrame] = []
    for year in years:
        candidates = [score_horizon(frame[(frame["year"] < year) & (pd.to_numeric(frame["holding_days"], errors="coerce") == h)].copy(), h, config) for h in config["holding_days_grid"]]
        valid = [r for r in candidates if r["train_events"] >= int(config["min_train_events"])]
        selected = max(valid or candidates, key=lambda r: (r["selection_score"], r["train_net_mean_return"], r["train_relative_mean_return"], r["train_events"]))
        selected["test_year"] = year
        logs.append(selected)
        test = frame[(frame["year"] == year) & (pd.to_numeric(frame["holding_days"], errors="coerce") == int(selected["holding_days"]))].copy()
        test = prep(test)
        test = stop_exit.apply_stop(test, float(config["stop_loss"]), config)
        test["selected_holding_days"] = int(selected["holding_days"])
        if not test.empty:
            parts.append(test)
    return pd.DataFrame(logs), pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=frame.columns)


def score_horizon(frame: pd.DataFrame, holding_days: int, config: dict[str, Any]) -> dict[str, Any]:
    trades = stop_exit.apply_stop(prep(frame), float(config["stop_loss"]), config)
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
        "holding_days": int(holding_days),
        "train_events": events,
        "train_net_mean_return": net,
        "train_relative_mean_return": rel,
        "train_win_rate": win,
        "train_bad_window_rate": bad_rate,
        "selection_score": float(score),
    }


def prep(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["signal_date"] = pd.to_datetime(out["signal_date"], errors="coerce")
    out = out.dropna(subset=["signal_date"]).sort_values("signal_date").drop_duplicates("signal_date")
    out["benchmark_return_horizon"] = pd.to_numeric(out["benchmark_return_horizon"], errors="coerce")
    out["market_return_5d"] = out["benchmark_return_horizon"]
    out["relative_return_horizon"] = pd.to_numeric(out["trade_return"], errors="coerce") - out["benchmark_return_horizon"]
    return out


def write_outputs(out: Path, debug: Path, config: dict[str, Any], source: pd.DataFrame, log: pd.DataFrame, trades: pd.DataFrame, row: dict[str, Any]) -> None:
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
        "final_verdict": "research_only；年前滚持有期选择未证明有效反弹窗口。",
        "main_diagnosis": "V4.59 每年只用过去年份在 5/10/15/20 日持有期中选择执行方式。",
        "research_boundary": config["research_boundary"],
    }
    pd.DataFrame([row]).to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, row, log), encoding="utf-8")
    source.to_csv(debug / "horizon_select_source_panel.csv", index=False, encoding="utf-8-sig")
    log.to_csv(debug / "horizon_selection_log.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([row]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "year_forward_horizon_selection", "status": "pass", "evidence": "每个测试年份只使用此前年份选择持有期。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "ponytail: 固定4个已有持有期，不加新退出网格；先看年前滚是否能泛化。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any], log: pd.DataFrame) -> str:
    counts = log["holding_days"].value_counts().sort_index().to_dict() if not log.empty else {}
    return "\n".join([
        "# V4.59 年前滚持有期选择",
        "",
        "## 结论",
        "",
        f"- 持有期选择次数：{counts}。",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对市场收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "本版本把 V4.53 的全样本持有期审计改为年前滚选择。若仍不过 V3.2，说明仅调整持有期不能解决反弹窗口收益厚度。",
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
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return ""


if __name__ == "__main__":
    main()
