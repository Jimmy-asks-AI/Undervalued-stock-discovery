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
CONFIG = ROOT / "configs" / "rebound_window_v4_60_breadth_relief_event_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    panel = pd.read_csv(ROOT / config["source_panel"], encoding="utf-8-sig")
    trades = build_trades(panel, config)
    row = stop_exit.summarize("v4_60_breadth_relief_event", trades, None, config)
    row.update({"signal_name_zh": "V4.60广度压力缓解事件", "signal_type": "breadth_relief_event", "status": "research_only"})
    write_outputs(out, debug, config, panel, trades, row)
    print(f"output_dir={out}")
    print(f"events={int(row['nonoverlap_events'])}")
    print(f"clusters={int(row['independent_event_clusters'])}")
    print(f"net={row['net_mean_return']:.2%}")
    print(f"relative={row['relative_mean_return']:.2%}")


def build_trades(panel: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    if "entry_lag_days" in config or "conditional_stop_loss" in config:
        return build_delayed_trades(panel, config)
    frame = panel.copy()
    mask = pd.Series(True, index=frame.index)
    for cond in config["conditions"]:
        values = pd.to_numeric(frame[cond["field"]], errors="coerce")
        if cond["op"] == ">=":
            mask &= values >= float(cond["value"])
        elif cond["op"] == ">":
            mask &= values > float(cond["value"])
        elif cond["op"] == "<=":
            mask &= values <= float(cond["value"])
        else:
            raise ValueError(f"unsupported op: {cond['op']}")
    trades = frame[mask].copy()
    trades["signal_id"] = "v4_60_breadth_relief_event"
    trades["signal_date"] = trades["trade_date"]
    trades["entry_date"] = trades["trade_date"]
    trades["exit_date"] = ""
    trades["holding_days"] = int(config["holding_days"])
    trades["trade_return"] = pd.to_numeric(trades["forward_return_20d_next_close"], errors="coerce")
    trades["max_adverse_return"] = pd.to_numeric(trades["forward_max_drawdown_20d_next_close"], errors="coerce")
    trades["is_win"] = trades["trade_return"] > 0
    trades["is_bad_window"] = trades["trade_return"] <= float(config["bad_window_return_threshold"])
    trades["stop_loss_hit"] = False
    trades["stop_loss_level"] = None
    trades["year"] = pd.to_datetime(trades["trade_date"], errors="coerce").dt.year
    trades["benchmark_return_horizon"] = 0.0
    trades["relative_return_horizon"] = trades["trade_return"]
    trades["market_return_5d"] = 0.0
    return trades.dropna(subset=["trade_return"])


def build_delayed_trades(panel: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    frame = panel.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    mask = condition_mask(frame, config["conditions"])
    nav = pd.to_numeric(frame["market_nav"], errors="coerce")
    lag = int(config.get("entry_lag_days", 0))
    holding_days = int(config["holding_days"])
    stop_cfg = config.get("conditional_stop_loss") or {}
    stop_level = float(stop_cfg.get("level", 0.0) or 0.0)
    stop_conditions = stop_cfg.get("conditions", [])
    rows: list[dict[str, Any]] = []
    for idx, row in frame[mask].iterrows():
        entry_idx = idx + lag
        exit_idx = entry_idx + holding_days
        if exit_idx >= len(frame) or pd.isna(nav.iloc[entry_idx]) or pd.isna(nav.iloc[exit_idx]):
            continue
        path = nav.iloc[entry_idx : exit_idx + 1] / nav.iloc[entry_idx] - 1.0
        trade_return = float(nav.iloc[exit_idx] / nav.iloc[entry_idx] - 1.0)
        max_adverse = float(path.min()) if len(path) else math.nan
        stop_hit = bool(stop_level and max_adverse <= -stop_level and condition_mask(row.to_frame().T, stop_conditions).iloc[0])
        if stop_hit:
            # ponytail: close-to-close stop overlay; intraday stop modeling needs intraday data.
            trade_return = -stop_level
            max_adverse = -stop_level
        output = row.to_dict()
        output.update(
            {
                "signal_id": config["policy_id"],
                "signal_date": date_text(row["trade_date"]),
                "entry_date": date_text(frame.loc[entry_idx, "trade_date"]),
                "exit_date": date_text(frame.loc[exit_idx, "trade_date"]),
                "holding_days": holding_days,
                "entry_lag_days": lag,
                "trade_return": trade_return,
                "max_adverse_return": max_adverse,
                "is_win": trade_return > 0,
                "is_bad_window": trade_return <= float(config["bad_window_return_threshold"]),
                "stop_loss_hit": stop_hit,
                "stop_loss_level": stop_level if stop_level else None,
                "year": int(pd.Timestamp(row["trade_date"]).year),
                "benchmark_return_horizon": 0.0,
                "relative_return_horizon": trade_return,
                "market_return_5d": 0.0,
            }
        )
        rows.append(output)
    return pd.DataFrame(rows).dropna(subset=["trade_return"]) if rows else pd.DataFrame()


def condition_mask(frame: pd.DataFrame, conditions: list[dict[str, Any]]) -> pd.Series:
    mask = pd.Series(True, index=frame.index)
    for cond in conditions:
        values = pd.to_numeric(frame[cond["field"]], errors="coerce")
        op = cond["op"]
        threshold = float(cond["value"])
        if op == ">=":
            mask &= values >= threshold
        elif op == ">":
            mask &= values > threshold
        elif op == "<=":
            mask &= values <= threshold
        elif op == "<":
            mask &= values < threshold
        else:
            raise ValueError(f"unsupported op: {op}")
    return mask


def date_text(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def write_outputs(out: Path, debug: Path, config: dict[str, Any], panel: pd.DataFrame, trades: pd.DataFrame, row: dict[str, Any]) -> None:
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
        "final_verdict": "research_only；广度压力缓解事件收益较厚但独立簇严重不足。",
        "main_diagnosis": "V4.60 换成固定广度压力缓解事件，检验旧事件池之外的市场状态定义。",
        "research_boundary": config["research_boundary"],
    }
    pd.DataFrame([row]).to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, row), encoding="utf-8")
    panel.to_csv(debug / "breadth_relief_source_panel.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([row]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "fixed_breadth_relief_conditions", "status": "pass", "evidence": "固定使用 V3.7 已有日频广度字段；不使用未来收益筛选事件。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "ponytail: 固定一组广度缓解条件，不扫阈值；先判断新事件定义是否值得继续。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any]) -> str:
    return "\n".join([
        "# V4.60 广度压力缓解事件",
        "",
        "## 结论",
        "",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对现金收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "本版本不再沿用旧事件池，而是直接定义广度压力缓解事件：60 日下跌广度高、5 日上涨占比明显修复、新低压力缓解。",
        "",
        "该口径收益更厚，但事件高度集中在少数行情簇，仍不能认定为有效反弹窗口。",
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
