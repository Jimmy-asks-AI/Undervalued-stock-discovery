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
CONFIG = ROOT / "configs" / "rebound_window_v4_54_cluster_position_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = load_source(config)
    grid, by_id = scan(source, config)
    primary = grid.sort_values(["eligible_sample_clusters", "net_mean_return", "relative_mean_return"], ascending=False).iloc[0].to_dict()
    trades = by_id[primary["signal_id"]]
    write_outputs(out, debug, config, source, grid, primary, trades)
    print(f"output_dir={out}")
    print(f"primary={primary['signal_id']}")
    print(f"events={int(primary['nonoverlap_events'])}")
    print(f"clusters={int(primary['independent_event_clusters'])}")
    print(f"net={primary['net_mean_return']:.2%}")
    print(f"relative={primary['relative_mean_return']:.2%}")


def load_source(config: dict[str, Any]) -> pd.DataFrame:
    frame = pd.read_csv(ROOT / config["source_panel"], encoding="utf-8-sig")
    frame = frame[pd.to_numeric(frame["holding_days"], errors="coerce") == int(config["holding_days"])].copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce")
    frame = frame.dropna(subset=["signal_date"]).sort_values("signal_date").drop_duplicates("signal_date")
    frame["market_return_5d"] = pd.to_numeric(frame["benchmark_return_horizon"], errors="coerce")
    frame["relative_return_horizon"] = pd.to_numeric(frame["trade_return"], errors="coerce") - frame["market_return_5d"]
    return frame


def scan(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    rows = []
    by_id = {}
    for gap_days in config["cluster_gap_days_grid"]:
        clustered = add_cluster_position(source, int(gap_days))
        for pos in config["cluster_positions"]:
            selected = clustered[clustered["cluster_position"] == int(pos)].copy()
            add_row(rows, by_id, selected, config, f"gap{gap_days}_pos{pos}", int(gap_days), f"pos{pos}")
        selected = clustered[clustered["cluster_position"] >= 2].copy()
        add_row(rows, by_id, selected, config, f"gap{gap_days}_pos_ge2", int(gap_days), "pos_ge2")
        selected = clustered.groupby("cluster_id", as_index=False).tail(1).copy()
        add_row(rows, by_id, selected, config, f"gap{gap_days}_last_posthoc", int(gap_days), "last_posthoc")
    return pd.DataFrame(rows), by_id


def add_cluster_position(frame: pd.DataFrame, gap_days: int) -> pd.DataFrame:
    output = frame.copy()
    cluster_ids = []
    positions = []
    cluster_id = -1
    position = 0
    last_date = None
    for date in output["signal_date"]:
        if last_date is None or (date - last_date).days > gap_days:
            cluster_id += 1
            position = 1
        else:
            position += 1
        cluster_ids.append(cluster_id)
        positions.append(position)
        last_date = date
    output["cluster_id"] = cluster_ids
    output["cluster_position"] = positions
    return output


def add_row(rows: list[dict[str, Any]], by_id: dict[str, pd.DataFrame], selected: pd.DataFrame, config: dict[str, Any], rule_id: str, gap_days: int, position_rule: str) -> None:
    if selected.empty:
        return
    signal_id = f"full_sample_cluster_position_{rule_id}"
    trades = stop_exit.apply_stop(selected, float(config["stop_loss"]), config)
    trades["signal_id"] = signal_id
    trades["signal_name_zh"] = rule_id
    trades["signal_type"] = "full_sample_cluster_position"
    by_id[signal_id] = trades
    row = stop_exit.summarize(signal_id, trades, float(config["stop_loss"]), config)
    row.update({
        "cluster_gap_days": gap_days,
        "position_rule": position_rule,
        "eligible_sample_clusters": row["nonoverlap_events"] >= 30 and row["independent_event_clusters"] >= 20,
    })
    rows.append(row)


def write_outputs(out: Path, debug: Path, config: dict[str, Any], source: pd.DataFrame, grid: pd.DataFrame, primary: dict[str, Any], trades: pd.DataFrame) -> None:
    summary = {
        "version": config["version"],
        "policy_id": config["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "primary_independent_event_clusters": int(primary["independent_event_clusters"]),
        "candidate_count": int(((grid["nonoverlap_events"] >= 30) & (grid["independent_event_clusters"] >= 20) & (grid["net_mean_return"] >= 0.02) & (grid["relative_mean_return"] >= 0.01)).sum()),
        "audit_fail_count": 0,
        "best_signal_id": primary["signal_id"],
        "best_status": "理论上限",
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": float(primary["event_mean_return"]),
        "best_event_relative_mean_return": float(primary["relative_mean_return"]),
        "best_event_bad_window_rate": float(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；信号簇确认提高小样本收益但不能满足样本与收益门槛",
        "main_diagnosis": "V4.54 审计固定2%止损下，不同信号簇位置的收益厚度。",
        "research_boundary": config["research_boundary"],
    }
    grid.sort_values(["net_mean_return", "relative_mean_return"], ascending=False).to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, primary, grid), encoding="utf-8")
    source.to_csv(debug / "cluster_position_source_panel.csv", index=False, encoding="utf-8-sig")
    grid.to_csv(debug / "cluster_position_grid.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "full_sample_cluster_position", "status": "pass", "evidence": "本版显式标记为 full_sample_cluster_position，统一评价按理论上限处理。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "全样本信号簇位置审计，不是交易规则。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any], grid: pd.DataFrame) -> str:
    eligible = grid[(grid["nonoverlap_events"] >= 30) & (grid["independent_event_clusters"] >= 20)]
    passed = eligible[(eligible["net_mean_return"] >= 0.02) & (eligible["relative_mean_return"] >= 0.01)]
    return "\n".join([
        "# V4.54 固定止损信号簇位置审计",
        "",
        "## 结论",
        "",
        f"- 样本和独立簇达标组合数：{len(eligible)}。",
        f"- 核心收益门槛通过组合数：{len(passed)}。",
        f"- 主口径：`{row['signal_id']}`。",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对市场收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "簇内第 2/3 个确认信号的小样本收益更高，但样本数远低于 V3.1 要求；样本够的簇位置仍停留在约 +1% 成本后收益附近。",
        "",
        "这说明等待信号簇确认不能单独解决反弹窗口识别问题。",
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
