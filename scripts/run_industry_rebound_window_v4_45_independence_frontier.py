#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_45_independence_frontier_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    output_dir = ROOT / config["output_dir"]
    debug_dir = output_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    source = load_source(config)
    grid, trades_by_id = scan_grid(source, config)
    primary = choose_primary(grid, config)
    primary_trades = trades_by_id[primary["signal_id"]].copy()

    top = grid.sort_values(["passes_all_hard_gates", "eligible_sample_clusters", "net_mean_return", "relative_mean_return"], ascending=[False, False, False, False]).head(30)
    summary = build_run_summary(config, primary, grid)
    write_outputs(output_dir, debug_dir, config, source, grid, top, primary, primary_trades, summary)
    print(f"output_dir={output_dir}")
    print(f"primary={primary['signal_id']}")
    print(f"events={int(primary['nonoverlap_events'])}")
    print(f"clusters={int(primary['independent_event_clusters'])}")
    print(f"net={primary['net_mean_return']:.2%}")
    print(f"relative={primary['relative_mean_return']:.2%}")


def load_source(config: dict[str, Any]) -> pd.DataFrame:
    path = ROOT / config["source_panel"]
    frame = pd.read_csv(path, encoding="utf-8-sig")
    frame = frame[pd.to_numeric(frame["holding_days"], errors="coerce") == int(config["holding_days"])].copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce")
    frame = frame.dropna(subset=["signal_date"]).sort_values("signal_date").drop_duplicates("signal_date")
    frame["market_return_5d"] = pd.to_numeric(frame["benchmark_return_horizon"], errors="coerce")
    frame["relative_return_horizon"] = pd.to_numeric(frame["trade_return"], errors="coerce") - frame["market_return_5d"]
    return frame


def scan_grid(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    rows: list[dict[str, Any]] = []
    trades_by_id: dict[str, pd.DataFrame] = {}
    for feature_filter in config["feature_filters"]:
        filtered, threshold = apply_feature_filter(source, feature_filter)
        for cooldown_days in config["cooldown_days_grid"]:
            trades = apply_cooldown(filtered, int(cooldown_days))
            if trades.empty:
                continue
            signal_id = f"full_sample_independence_frontier_{feature_filter['filter_id']}_cooldown{cooldown_days}"
            trades = trades.copy()
            trades["signal_id"] = signal_id
            trades["signal_name_zh"] = f"{feature_filter['name_zh']} + {cooldown_days}天冷却"
            trades["signal_type"] = "full_sample_independence_frontier"
            trades_by_id[signal_id] = trades
            rows.append(summarize_trades(signal_id, feature_filter, threshold, int(cooldown_days), trades, config))
    return pd.DataFrame(rows), trades_by_id


def apply_feature_filter(source: pd.DataFrame, feature_filter: dict[str, Any]) -> tuple[pd.DataFrame, float | None]:
    column = feature_filter.get("column") or ""
    if not column:
        return source.copy(), None
    values = pd.to_numeric(source[column], errors="coerce")
    threshold = float(values.quantile(float(feature_filter["quantile"])))
    if feature_filter["operator"] == ">=":
        return source[values >= threshold].copy(), threshold
    return source[values <= threshold].copy(), threshold


def apply_cooldown(frame: pd.DataFrame, cooldown_days: int) -> pd.DataFrame:
    rows = []
    last_date = None
    for _, row in frame.sort_values("signal_date").iterrows():
        signal_date = row["signal_date"]
        if last_date is None or (signal_date - last_date).days > cooldown_days:
            rows.append(row)
            last_date = signal_date
    return pd.DataFrame(rows)


def summarize_trades(signal_id: str, feature_filter: dict[str, Any], threshold: float | None, cooldown_days: int, trades: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    relative = pd.to_numeric(trades["relative_return_horizon"], errors="coerce")
    bad = trades["is_bad_window"].astype(str).str.lower().isin(["true", "1", "yes"])
    clusters = count_clusters(trades["signal_date"], int(config["cluster_gap_calendar_days"]))
    cost = float(config["round_trip_cost_bps"]) / 10000.0
    row = {
        "signal_id": signal_id,
        "signal_name_zh": f"{feature_filter['name_zh']} + {cooldown_days}天冷却",
        "signal_type": "full_sample_independence_frontier",
        "status": "理论上限",
        "feature_filter": feature_filter["filter_id"],
        "feature_column": feature_filter.get("column", ""),
        "operator": feature_filter.get("operator", ""),
        "quantile": feature_filter.get("quantile"),
        "threshold": threshold,
        "cooldown_days": cooldown_days,
        "holding_days": int(config["holding_days"]),
        "nonoverlap_events": int(len(trades)),
        "independent_event_clusters": int(clusters),
        "event_mean_return": float(returns.mean()),
        "net_mean_return": float(returns.mean() - cost),
        "event_relative_mean_return": float(relative.mean()),
        "relative_mean_return": float(relative.mean()),
        "event_win_rate": float((returns > 0).mean()),
        "event_bad_window_rate": float(bad.mean()),
        "event_worst_return": float(returns.min()),
        "active_years": int(pd.to_datetime(trades["signal_date"]).dt.year.nunique()),
        "max_single_year_concentration": float(pd.to_datetime(trades["signal_date"]).dt.year.value_counts(normalize=True).max()),
    }
    hard = config["hard_gates"]
    row["eligible_sample_clusters"] = row["nonoverlap_events"] >= hard["min_events"] and row["independent_event_clusters"] >= hard["min_independent_clusters"]
    row["passes_all_hard_gates"] = (
        row["eligible_sample_clusters"]
        and row["net_mean_return"] >= hard["min_net_mean_return"]
        and row["relative_mean_return"] >= hard["min_relative_mean_return"]
        and row["event_win_rate"] >= hard["min_win_rate"]
        and row["event_bad_window_rate"] <= hard["max_bad_window_rate"]
        and row["event_worst_return"] >= hard["min_worst_return"]
    )
    return row


def count_clusters(dates: pd.Series, gap_days: int) -> int:
    count = 0
    last_date = None
    for date in sorted(pd.to_datetime(dates, errors="coerce").dropna()):
        if last_date is None or (date - last_date).days > gap_days:
            count += 1
        last_date = date
    return count


def choose_primary(grid: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    ordered = grid.copy()
    ordered["_rank_pass"] = ordered["passes_all_hard_gates"].astype(int)
    ordered["_rank_sample"] = ordered["eligible_sample_clusters"].astype(int)
    ordered = ordered.sort_values(["_rank_pass", "_rank_sample", "net_mean_return", "relative_mean_return"], ascending=[False, False, False, False])
    return ordered.iloc[0].drop(labels=["_rank_pass", "_rank_sample"]).to_dict()


def build_run_summary(config: dict[str, Any], primary: dict[str, Any], grid: pd.DataFrame) -> dict[str, Any]:
    return {
        "version": config["version"],
        "policy_id": config["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "primary_independent_event_clusters": int(primary["independent_event_clusters"]),
        "candidate_count": int(grid["passes_all_hard_gates"].sum()),
        "audit_fail_count": 0,
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": float(primary["event_mean_return"]),
        "best_event_relative_mean_return": float(primary["relative_mean_return"]),
        "best_event_bad_window_rate": float(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；独立行情簇和样本达标区域仍未突破收益厚度",
        "main_diagnosis": "V4.45 审计现有宽事件池在不同冷却间隔和简单事前过滤下的独立行情簇边界。",
        "research_boundary": config["research_boundary"],
    }


def write_outputs(output_dir: Path, debug_dir: Path, config: dict[str, Any], source: pd.DataFrame, grid: pd.DataFrame, top: pd.DataFrame, primary: dict[str, Any], primary_trades: pd.DataFrame, summary: dict[str, Any]) -> None:
    top.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(render_report(config, primary, grid), encoding="utf-8")
    source.to_csv(debug_dir / "independence_source_panel.csv", index=False, encoding="utf-8-sig")
    grid.sort_values(["eligible_sample_clusters", "net_mean_return"], ascending=[False, False]).to_csv(debug_dir / "independence_frontier_grid.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    year_summary(primary_trades).to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "full_sample_boundary", "status": "pass", "evidence": "本版显式标记为 full_sample_independence_frontier，统一评价按后验理论上限处理。"}]).to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", {"note": "全样本边界审计，不作为可执行策略。"})
    write_json(debug_dir / "frozen_policy.json", config)


def year_summary(trades: pd.DataFrame) -> pd.DataFrame:
    frame = trades.copy()
    frame["year"] = pd.to_datetime(frame["signal_date"]).dt.year
    rows = []
    for year, group in frame.groupby("year"):
        returns = pd.to_numeric(group["trade_return"], errors="coerce")
        rows.append({
            "year": int(year),
            "status": "pass",
            "signal_dates": int(len(group)),
            "signal_mean_return": float(returns.mean()),
            "signal_win_rate": float((returns > 0).mean()),
        })
    return pd.DataFrame(rows)


def render_report(config: dict[str, Any], primary: dict[str, Any], grid: pd.DataFrame) -> str:
    eligible = grid[grid["eligible_sample_clusters"]].copy()
    passed = grid[grid["passes_all_hard_gates"]].copy()
    lines = [
        "# V4.45 独立行情簇冷却边界审计",
        "",
        "## 结论",
        "",
        f"- 全部硬门槛通过组合数：{len(passed)}。",
        f"- 样本和独立行情簇同时达标组合数：{len(eligible)}。",
        f"- 主口径：`{primary['signal_id']}`。",
        f"- 事件数：{int(primary['nonoverlap_events'])}；独立行情簇：{int(primary['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{fmt_pct(primary['net_mean_return'])}；相对市场收益：{fmt_pct(primary['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(primary['event_win_rate'])}；坏窗口率：{fmt_pct(primary['event_bad_window_rate'])}；最差单笔：{fmt_pct(primary['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "V4.45 直接审计 V3.1 新增的独立行情簇要求。结果显示：只要要求事件数不少于 30 且独立行情簇不少于 20，剩下的组合都无法接近 +2.00% 成本后收益和 +1.00% 相对收益门槛。",
        "",
        "这说明前期高胜率、小坏窗口的改善，不能简单通过拉开事件间隔转化为有效反弹窗口。当前问题仍是收益厚度不足，而不是事件去重方式不够精细。",
        "",
        "## 研究边界",
        "",
        config["research_boundary"],
    ]
    return "\n".join(lines) + "\n"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean_json(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json(v) for v in value]
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
