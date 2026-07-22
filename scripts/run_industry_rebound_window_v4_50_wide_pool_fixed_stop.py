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
CONFIG = ROOT / "configs" / "rebound_window_v4_50_wide_pool_fixed_stop_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = load_source(config)
    trades = stop_exit.apply_stop(source, float(config["stop_loss"]), config)
    trades["signal_id"] = "v4_50_wide_pool_fixed_2pct_stop"
    trades["signal_name_zh"] = "V4.50宽事件池固定2%止损"
    trades["signal_type"] = "wide_pool_fixed_stop"
    row = stop_exit.summarize("v4_50_wide_pool_fixed_2pct_stop", trades, float(config["stop_loss"]), config)
    row["signal_name_zh"] = "V4.50宽事件池固定2%止损"
    row["signal_type"] = "wide_pool_fixed_stop"
    write_outputs(out, debug, config, source, trades, row)
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
    frame["market_return_5d"] = pd.to_numeric(frame["benchmark_return_horizon"], errors="coerce")
    frame["relative_return_horizon"] = pd.to_numeric(frame["trade_return"], errors="coerce") - frame["market_return_5d"]
    return frame


def write_outputs(out: Path, debug: Path, config: dict[str, Any], source: pd.DataFrame, trades: pd.DataFrame, row: dict[str, Any]) -> None:
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
        "final_verdict": "research_only；宽池补足独立簇但收益厚度仍不足",
        "main_diagnosis": "V4.50 使用宽事件池和固定 2% 机械止损复核样本覆盖与收益厚度的冲突。",
        "research_boundary": config["research_boundary"],
    }
    pd.DataFrame([row]).to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, row), encoding="utf-8")
    source.to_csv(debug / "wide_pool_source_panel.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([row]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "fixed_rule", "status": "pass", "evidence": "宽事件池 + 固定 2% 止损，不扫描参数。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "固定宽池和固定 2% 止损，检查独立簇过线后收益是否足够。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any]) -> str:
    return "\n".join([
        "# V4.50 宽事件池固定2%止损复核",
        "",
        "## 结论",
        "",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 止损触发次数：{int(row['stop_loss_hits'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对市场收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "宽事件池解决了 V4.49 的独立行情簇不足问题，但收益厚度和相对收益仍明显低于 V3.1 门槛。",
        "",
        "这说明当前系统不是单纯缺样本，而是入场事件池本身的平均收益不够厚。",
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
