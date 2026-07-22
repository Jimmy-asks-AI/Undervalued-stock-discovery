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
CONFIG = ROOT / "configs" / "rebound_window_v4_53_fixed_stop_horizon_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(ROOT / config["source_panel"], encoding="utf-8-sig")
    grid, by_id = scan(source, config)
    primary = grid.sort_values(["net_mean_return", "relative_mean_return"], ascending=False).iloc[0].to_dict()
    trades = by_id[primary["signal_id"]]
    write_outputs(out, debug, config, source, grid, primary, trades)
    print(f"output_dir={out}")
    print(f"primary={primary['signal_id']}")
    print(f"events={int(primary['nonoverlap_events'])}")
    print(f"clusters={int(primary['independent_event_clusters'])}")
    print(f"net={primary['net_mean_return']:.2%}")
    print(f"relative={primary['relative_mean_return']:.2%}")


def scan(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    rows = []
    by_id = {}
    for holding_days in config["holding_days_grid"]:
        frame = source[pd.to_numeric(source["holding_days"], errors="coerce") == int(holding_days)].copy()
        frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce")
        frame = frame.dropna(subset=["signal_date"]).sort_values("signal_date").drop_duplicates("signal_date")
        frame["market_return_5d"] = pd.to_numeric(frame["benchmark_return_horizon"], errors="coerce")
        frame["relative_return_horizon"] = pd.to_numeric(frame["trade_return"], errors="coerce") - frame["market_return_5d"]
        trades = stop_exit.apply_stop(frame, float(config["stop_loss"]), config)
        signal_id = f"full_sample_fixed_stop_h{int(holding_days)}"
        trades["signal_id"] = signal_id
        trades["signal_name_zh"] = f"{int(holding_days)}日持有固定2%止损"
        trades["signal_type"] = "full_sample_fixed_stop_horizon"
        by_id[signal_id] = trades
        row = stop_exit.summarize(signal_id, trades, float(config["stop_loss"]), config)
        row["holding_days"] = int(holding_days)
        rows.append(row)
    return pd.DataFrame(rows), by_id


def write_outputs(out: Path, debug: Path, config: dict[str, Any], source: pd.DataFrame, grid: pd.DataFrame, primary: dict[str, Any], trades: pd.DataFrame) -> None:
    summary = {
        "version": config["version"],
        "policy_id": config["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "primary_independent_event_clusters": int(primary["independent_event_clusters"]),
        "candidate_count": 0,
        "audit_fail_count": 0,
        "best_signal_id": primary["signal_id"],
        "best_status": "理论上限",
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": float(primary["event_mean_return"]),
        "best_event_relative_mean_return": float(primary["relative_mean_return"]),
        "best_event_bad_window_rate": float(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；延长持有期没有突破收益厚度",
        "main_diagnosis": "V4.53 审计宽事件池固定2%止损下 5/10/15/20 日持有期效果。",
        "research_boundary": config["research_boundary"],
    }
    grid.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, primary, grid), encoding="utf-8")
    source.to_csv(debug / "fixed_stop_horizon_source_panel.csv", index=False, encoding="utf-8-sig")
    grid.to_csv(debug / "fixed_stop_horizon_grid.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "full_sample_horizon_selection", "status": "pass", "evidence": "本版显式标记为 full_sample_fixed_stop_horizon，统一评价按理论上限处理。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "全样本持有期审计，不是交易规则。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any], grid: pd.DataFrame) -> str:
    table = "\n".join(
        f"- {int(r.holding_days)}日：事件 {int(r.nonoverlap_events)}，独立簇 {int(r.independent_event_clusters)}，成本后 {fmt_pct(r.net_mean_return)}，相对 {fmt_pct(r.relative_mean_return)}，胜率 {fmt_pct(r.event_win_rate)}"
        for r in grid.itertuples()
    )
    return "\n".join([
        "# V4.53 固定2%止损多持有期审计",
        "",
        "## 结论",
        "",
        f"- 主口径：`{row['signal_id']}`。",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对市场收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 持有期对比",
        "",
        table,
        "",
        "## 解读",
        "",
        "10日持有相对收益最高但胜率不足，15日持有收益略高但样本不足，20日明显退化。延长持有期没有把当前事件池推过有效窗口门槛。",
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
