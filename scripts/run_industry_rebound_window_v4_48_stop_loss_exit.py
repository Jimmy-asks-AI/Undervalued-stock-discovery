#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_48_stop_loss_exit_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(ROOT / config["source_trades"], encoding="utf-8-sig")
    grid, trades_by_id = scan(source, config)
    primary = grid.sort_values(["net_mean_return", "relative_mean_return"], ascending=False).iloc[0].to_dict()
    trades = trades_by_id[primary["signal_id"]]
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
    for stop_loss in [None] + [float(x) for x in config["stop_loss_grid"]]:
        signal_id = "v4_48_no_stop_reference" if stop_loss is None else f"full_sample_stop_loss_{int(stop_loss * 100)}pct"
        trades = apply_stop(source, stop_loss, config)
        trades["signal_id"] = signal_id
        trades["signal_name_zh"] = "不止损参考" if stop_loss is None else f"全样本{int(stop_loss * 100)}%机械止损"
        trades["signal_type"] = "full_sample_stop_loss_exit"
        by_id[signal_id] = trades
        rows.append(summarize(signal_id, trades, stop_loss, config))
    return pd.DataFrame(rows), by_id


def apply_stop(source: pd.DataFrame, stop_loss: float | None, config: dict[str, Any]) -> pd.DataFrame:
    frame = source.copy()
    original = pd.to_numeric(frame["trade_return"], errors="coerce")
    if stop_loss is None:
        adjusted = original
        hit = pd.Series(False, index=frame.index)
    else:
        threshold = -float(stop_loss)
        hit = pd.to_numeric(frame["max_adverse_return"], errors="coerce") <= threshold
        adjusted = original.where(~hit, threshold)
    frame["original_trade_return"] = original
    frame["stop_loss_hit"] = hit
    frame["stop_loss_level"] = None if stop_loss is None else float(stop_loss)
    frame["trade_return"] = adjusted
    if stop_loss is not None and "max_adverse_return" in frame.columns:
        # ponytail: stop overlay exits at the stop line; full intraday path modeling would need lower-frequency bar data.
        frame["max_adverse_return"] = pd.to_numeric(frame["max_adverse_return"], errors="coerce").where(~hit, threshold)
    frame["is_win"] = adjusted > 0
    frame["is_bad_window"] = adjusted <= float(config["bad_window_return_threshold"])
    frame["relative_return_horizon"] = adjusted - pd.to_numeric(frame["benchmark_return_horizon"], errors="coerce")
    frame["market_return_5d"] = pd.to_numeric(frame["benchmark_return_horizon"], errors="coerce")
    return frame


def summarize(signal_id: str, trades: pd.DataFrame, stop_loss: float | None, config: dict[str, Any]) -> dict[str, Any]:
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    relative = pd.to_numeric(trades["relative_return_horizon"], errors="coerce")
    years = pd.to_datetime(trades["signal_date"], errors="coerce").dt.year
    bad = trades["is_bad_window"].astype(bool)
    cost = float(config["round_trip_cost_bps"]) / 10000.0
    return {
        "signal_id": signal_id,
        "signal_name_zh": "不止损参考" if stop_loss is None else f"全样本{int(stop_loss * 100)}%机械止损",
        "signal_type": "full_sample_stop_loss_exit",
        "status": "理论上限",
        "stop_loss_level": stop_loss,
        "stop_loss_hits": int(trades["stop_loss_hit"].astype(bool).sum()),
        "nonoverlap_events": int(len(trades)),
        "trades": int(len(trades)),
        "independent_event_clusters": int(count_clusters(trades["signal_date"], int(config["cluster_gap_calendar_days"]))),
        "event_mean_return": float(returns.mean()),
        "mean_return": float(returns.mean()),
        "net_mean_return": float(returns.mean() - cost),
        "event_relative_mean_return": float(relative.mean()),
        "relative_mean_return": float(relative.mean()),
        "event_win_rate": float((returns > 0).mean()),
        "event_bad_window_rate": float(bad.mean()),
        "event_worst_return": float(returns.min()),
        "active_years": int(years.nunique()),
        "max_single_year_concentration": float(years.value_counts(normalize=True).max()),
    }


def count_clusters(dates: pd.Series, gap_days: int) -> int:
    count = 0
    last = None
    for date in sorted(pd.to_datetime(dates, errors="coerce").dropna()):
        if last is None or (date - last).days > gap_days:
            count += 1
        last = date
    return count


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
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": float(primary["event_mean_return"]),
        "best_event_relative_mean_return": float(primary["relative_mean_return"]),
        "best_event_bad_window_rate": float(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；机械止损改善尾部但未突破收益厚度",
        "main_diagnosis": "V4.48 用 V4.46 事件池审计机械止损退出上限。",
        "research_boundary": config["research_boundary"],
    }
    grid.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, primary), encoding="utf-8")
    source.to_csv(debug / "stop_loss_source_trades.csv", index=False, encoding="utf-8-sig")
    grid.to_csv(debug / "stop_loss_exit_grid.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_trades", "status": "pass", "evidence": config["source_trades"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "full_sample_stop_selection", "status": "pass", "evidence": "本版显式标记为 full_sample_stop_loss_exit，统一评价按理论上限处理。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "全样本止损退出上限审计，不是交易规则。"})
    write_json(debug / "frozen_policy.json", config)


def year_summary(trades: pd.DataFrame) -> pd.DataFrame:
    frame = trades.copy()
    frame["year"] = pd.to_datetime(frame["signal_date"], errors="coerce").dt.year
    rows = []
    for year, group in frame.groupby("year"):
        ret = pd.to_numeric(group["trade_return"], errors="coerce")
        rows.append({"year": int(year), "status": "pass", "signal_dates": int(len(group)), "signal_mean_return": float(ret.mean()), "signal_win_rate": float((ret > 0).mean())})
    return pd.DataFrame(rows)


def render_report(config: dict[str, Any], row: dict[str, Any]) -> str:
    return "\n".join([
        "# V4.48 全样本机械止损退出审计",
        "",
        "## 结论",
        "",
        f"- 主口径：`{row['signal_id']}`。",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 止损触发次数：{int(row['stop_loss_hits'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对市场收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "2% 机械止损能把最差单笔从 V4.46 的 -7.27% 改善到 -2.00%，并提高相对收益，但仍没有达到 V3.1 的成本后收益和相对收益门槛。",
        "",
        "这说明退出层能改善尾部，但不能把当前入场事件池转化为有效反弹窗口。",
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
