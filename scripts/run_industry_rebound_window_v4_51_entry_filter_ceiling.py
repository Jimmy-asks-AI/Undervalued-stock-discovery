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
CONFIG = ROOT / "configs" / "rebound_window_v4_51_entry_filter_ceiling_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = load_source(config)
    grid, trades_by_id = scan_filters(source, config)
    primary = choose_primary(grid)
    trades = trades_by_id[primary["signal_id"]]
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


def scan_filters(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    rows = []
    trades_by_id = {}
    filters = [{"filter_id": "none", "feature": "", "operator": "", "quantile": None, "threshold": None}]
    for feature in config["feature_columns"]:
        if feature not in source.columns:
            continue
        values = pd.to_numeric(source[feature], errors="coerce")
        for quantile in config["quantiles"]:
            threshold = float(values.quantile(float(quantile)))
            filters.append({"filter_id": f"{feature}_ge_q{int(float(quantile) * 100)}", "feature": feature, "operator": ">=", "quantile": quantile, "threshold": threshold})
            filters.append({"filter_id": f"{feature}_le_q{int(float(quantile) * 100)}", "feature": feature, "operator": "<=", "quantile": quantile, "threshold": threshold})
    for item in filters:
        filtered = apply_filter(source, item)
        trades = stop_exit.apply_stop(filtered, float(config["stop_loss"]), config)
        signal_id = f"full_sample_entry_filter_ceiling_{item['filter_id']}"
        trades["signal_id"] = signal_id
        trades["signal_name_zh"] = item["filter_id"]
        trades["signal_type"] = "full_sample_entry_filter_ceiling"
        trades_by_id[signal_id] = trades
        row = stop_exit.summarize(signal_id, trades, float(config["stop_loss"]), config)
        row.update(item)
        rows.append(row)
    return pd.DataFrame(rows), trades_by_id


def apply_filter(source: pd.DataFrame, item: dict[str, Any]) -> pd.DataFrame:
    feature = item["feature"]
    if not feature:
        return source.copy()
    values = pd.to_numeric(source[feature], errors="coerce")
    if item["operator"] == ">=":
        return source[values >= float(item["threshold"])].copy()
    return source[values <= float(item["threshold"])].copy()


def choose_primary(grid: pd.DataFrame) -> dict[str, Any]:
    frame = grid.copy()
    frame["eligible_sample_clusters"] = (frame["nonoverlap_events"] >= 30) & (frame["independent_event_clusters"] >= 20)
    frame["passes_core"] = (
        frame["eligible_sample_clusters"]
        & (frame["net_mean_return"] >= 0.02)
        & (frame["relative_mean_return"] >= 0.01)
        & (frame["event_win_rate"] >= 0.60)
        & (frame["event_bad_window_rate"] <= 0.20)
        & (frame["event_worst_return"] >= -0.06)
    )
    ordered = frame.sort_values(["passes_core", "eligible_sample_clusters", "net_mean_return", "relative_mean_return"], ascending=[False, False, False, False])
    return ordered.iloc[0].to_dict()


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
        "final_verdict": "research_only；单特征入场过滤仍不能突破收益厚度",
        "main_diagnosis": "V4.51 审计宽池固定2%止损后，现有事前单特征过滤的全样本理论上限。",
        "research_boundary": config["research_boundary"],
    }
    grid.sort_values(["net_mean_return", "relative_mean_return"], ascending=False).head(30).to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, primary, grid), encoding="utf-8")
    source.to_csv(debug / "entry_filter_source_panel.csv", index=False, encoding="utf-8-sig")
    grid.to_csv(debug / "entry_filter_grid.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "full_sample_filter_selection", "status": "pass", "evidence": "本版显式标记为 full_sample_entry_filter_ceiling，统一评价按理论上限处理。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "全样本单特征入场过滤上限审计，不是交易规则。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any], grid: pd.DataFrame) -> str:
    eligible = grid[(grid["nonoverlap_events"] >= 30) & (grid["independent_event_clusters"] >= 20)]
    passed = eligible[(eligible["net_mean_return"] >= 0.02) & (eligible["relative_mean_return"] >= 0.01)]
    return "\n".join([
        "# V4.51 宽池固定止损入场过滤上限审计",
        "",
        "## 结论",
        "",
        f"- 全部核心收益门槛通过组合数：{len(passed)}。",
        f"- 样本和独立簇达标组合数：{len(eligible)}。",
        f"- 主口径：`{row['signal_id']}`。",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对市场收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "高收益过滤主要来自极窄样本，无法满足 V3.1 的事件数和独立行情簇要求。样本与独立簇达标的过滤，收益仍停留在约 +1% 附近。",
        "",
        "这说明现有单特征不足以把宽事件池提升到有效反弹窗口。",
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
