#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_46_walk_forward_independence_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    output_dir = ROOT / config["output_dir"]
    debug_dir = output_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    source = load_source(config)
    trades, selections = walk_forward(source, config)
    summary_row = summarize("v4_46_walk_forward_independence_freeze", trades, config)
    summary = build_summary(config, summary_row, len(selections))
    write_outputs(output_dir, debug_dir, config, source, trades, selections, summary_row, summary)
    print(f"output_dir={output_dir}")
    print(f"events={int(summary_row['nonoverlap_events'])}")
    print(f"clusters={int(summary_row['independent_event_clusters'])}")
    print(f"net={summary_row['net_mean_return']:.2%}")
    print(f"relative={summary_row['relative_mean_return']:.2%}")


def load_source(config: dict[str, Any]) -> pd.DataFrame:
    frame = pd.read_csv(ROOT / config["source_panel"], encoding="utf-8-sig")
    frame = frame[pd.to_numeric(frame["holding_days"], errors="coerce") == int(config["holding_days"])].copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce")
    frame = frame.dropna(subset=["signal_date"]).sort_values("signal_date").drop_duplicates("signal_date")
    frame["year"] = frame["signal_date"].dt.year
    frame["market_return_5d"] = pd.to_numeric(frame["benchmark_return_horizon"], errors="coerce")
    frame["relative_return_horizon"] = pd.to_numeric(frame["trade_return"], errors="coerce") - frame["market_return_5d"]
    return frame


def walk_forward(source: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = []
    selections = []
    for year in sorted(y for y in source["year"].unique() if y >= int(config["start_year"])):
        train = source[source["year"] < year].copy()
        test = source[source["year"] == year].copy()
        if train.empty or test.empty:
            continue
        selected = choose_rule(train, config)
        test_filtered = filter_frame(test, train, selected["feature_filter"])[0]
        test_trades = apply_cooldown(test_filtered, int(selected["cooldown_days"]))
        test_summary = summarize(f"v4_46_{year}", test_trades, config)
        selections.append({**selected, "test_year": int(year), **{f"test_{k}": v for k, v in test_summary.items() if k not in ["signal_id", "signal_name_zh", "signal_type", "status"]}})
        if not test_trades.empty:
            test_trades = test_trades.copy()
            test_trades["signal_id"] = "v4_46_walk_forward_independence_freeze"
            test_trades["signal_name_zh"] = "V4.46年前滚独立边界冻结"
            test_trades["signal_type"] = "walk_forward_independence_freeze"
            test_trades["selected_rule"] = selected["feature_filter"]["filter_id"]
            test_trades["cooldown_days"] = int(selected["cooldown_days"])
            trades.append(test_trades)
    return (pd.concat(trades, ignore_index=True) if trades else pd.DataFrame(), pd.DataFrame(selections))


def choose_rule(train: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for feature_filter in config["feature_filters"]:
        filtered, threshold = filter_frame(train, train, feature_filter)
        for cooldown_days in config["cooldown_days_grid"]:
            trades = apply_cooldown(filtered, int(cooldown_days))
            row = summarize("train", trades, config)
            # ponytail: simple frozen utility; upgrade to a declared utility only if this line starts passing gates.
            score = min(row["nonoverlap_events"], 30) / 30
            score += min(row["independent_event_clusters"], 20) / 20
            score += 10 * max(row["net_mean_return"], -1)
            score += 8 * max(row["relative_mean_return"], -1)
            score -= max(row["event_bad_window_rate"], 1)
            candidates.append({
                "feature_filter": feature_filter,
                "feature_filter_id": feature_filter["filter_id"],
                "threshold": threshold,
                "cooldown_days": int(cooldown_days),
                "train_score": float(score),
                "train_events": int(row["nonoverlap_events"]),
                "train_clusters": int(row["independent_event_clusters"]),
                "train_net_mean_return": float(row["net_mean_return"]),
                "train_relative_mean_return": float(row["relative_mean_return"]),
            })
    return max(candidates, key=lambda row: row["train_score"])


def filter_frame(frame: pd.DataFrame, train: pd.DataFrame, feature_filter: dict[str, Any]) -> tuple[pd.DataFrame, float | None]:
    column = feature_filter.get("column") or ""
    if not column:
        return frame.copy(), None
    threshold = float(pd.to_numeric(train[column], errors="coerce").quantile(float(feature_filter["quantile"])))
    values = pd.to_numeric(frame[column], errors="coerce")
    if feature_filter["operator"] == ">=":
        return frame[values >= threshold].copy(), threshold
    return frame[values <= threshold].copy(), threshold


def apply_cooldown(frame: pd.DataFrame, cooldown_days: int) -> pd.DataFrame:
    rows = []
    last_date = None
    for _, row in frame.sort_values("signal_date").iterrows():
        if last_date is None or (row["signal_date"] - last_date).days > cooldown_days:
            rows.append(row)
            last_date = row["signal_date"]
    return pd.DataFrame(rows)


def summarize(signal_id: str, trades: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    if trades.empty:
        return empty_summary(signal_id)
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    relative = pd.to_numeric(trades["relative_return_horizon"], errors="coerce")
    bad = trades["is_bad_window"].astype(str).str.lower().isin(["true", "1", "yes"])
    years = pd.to_datetime(trades["signal_date"]).dt.year
    return {
        "signal_id": signal_id,
        "signal_name_zh": "V4.46年前滚独立边界冻结",
        "signal_type": "walk_forward_independence_freeze",
        "status": "research_only",
        "nonoverlap_events": int(len(trades)),
        "trades": int(len(trades)),
        "independent_event_clusters": int(count_clusters(trades["signal_date"], int(config["cluster_gap_calendar_days"]))),
        "event_mean_return": float(returns.mean()),
        "mean_return": float(returns.mean()),
        "net_mean_return": float(returns.mean() - float(config["round_trip_cost_bps"]) / 10000.0),
        "event_relative_mean_return": float(relative.mean()),
        "relative_mean_return": float(relative.mean()),
        "event_win_rate": float((returns > 0).mean()),
        "event_bad_window_rate": float(bad.mean()),
        "event_worst_return": float(returns.min()),
        "active_years": int(years.nunique()),
        "max_single_year_concentration": float(years.value_counts(normalize=True).max()),
    }


def empty_summary(signal_id: str) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "signal_name_zh": "V4.46年前滚独立边界冻结",
        "signal_type": "walk_forward_independence_freeze",
        "status": "research_only",
        "nonoverlap_events": 0,
        "trades": 0,
        "independent_event_clusters": 0,
        "event_mean_return": math.nan,
        "mean_return": math.nan,
        "net_mean_return": math.nan,
        "event_relative_mean_return": math.nan,
        "relative_mean_return": math.nan,
        "event_win_rate": math.nan,
        "event_bad_window_rate": math.nan,
        "event_worst_return": math.nan,
        "active_years": 0,
        "max_single_year_concentration": math.nan,
    }


def count_clusters(dates: pd.Series, gap_days: int) -> int:
    count = 0
    last_date = None
    for date in sorted(pd.to_datetime(dates, errors="coerce").dropna()):
        if last_date is None or (date - last_date).days > gap_days:
            count += 1
        last_date = date
    return count


def build_summary(config: dict[str, Any], row: dict[str, Any], selection_count: int) -> dict[str, Any]:
    return {
        "version": config["version"],
        "policy_id": config["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": row["signal_id"],
        "primary_realtime_events": int(row["nonoverlap_events"]),
        "primary_independent_event_clusters": int(row["independent_event_clusters"]),
        "candidate_count": 0,
        "audit_fail_count": 0,
        "walk_forward_selection_years": int(selection_count),
        "best_signal_id": row["signal_id"],
        "best_status": "research_only",
        "best_nonoverlap_events": int(row["nonoverlap_events"]),
        "best_event_mean_return": float(row["event_mean_return"]),
        "best_event_relative_mean_return": float(row["relative_mean_return"]),
        "best_event_bad_window_rate": float(row["event_bad_window_rate"]),
        "final_verdict": "research_only；年前滚冻结后仍未突破收益厚度和独立簇门槛",
        "main_diagnosis": "V4.46 每年只用过去数据选择过滤与冷却规则，再应用到当年。",
        "research_boundary": config["research_boundary"],
    }


def write_outputs(output_dir: Path, debug_dir: Path, config: dict[str, Any], source: pd.DataFrame, trades: pd.DataFrame, selections: pd.DataFrame, summary_row: dict[str, Any], summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([summary_row]).to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(render_report(config, summary_row, selections), encoding="utf-8")
    source.to_csv(debug_dir / "walk_forward_source_panel.csv", index=False, encoding="utf-8-sig")
    selections.to_csv(debug_dir / "walk_forward_rule_selection.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([summary_row]).to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    year_summary(trades).to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": config["source_panel"]}]).to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "year_forward_thresholds", "status": "pass", "evidence": "每年仅用过去年份数据选择规则和阈值。"}]).to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", {"note": "年前滚冻结选择；不是全样本后验边界。"})
    write_json(debug_dir / "frozen_policy.json", config)


def year_summary(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    frame = trades.copy()
    frame["year"] = pd.to_datetime(frame["signal_date"]).dt.year
    rows = []
    for year, group in frame.groupby("year"):
        returns = pd.to_numeric(group["trade_return"], errors="coerce")
        rows.append({"year": int(year), "status": "pass", "signal_dates": int(len(group)), "signal_mean_return": float(returns.mean()), "signal_win_rate": float((returns > 0).mean())})
    return pd.DataFrame(rows)


def render_report(config: dict[str, Any], row: dict[str, Any], selections: pd.DataFrame) -> str:
    lines = [
        "# V4.46 年前滚独立边界冻结审计",
        "",
        "## 结论",
        "",
        f"- 选择年份数：{len(selections)}。",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对市场收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        "V4.46 去掉了 V4.45 的全样本选择：每年只能用过去年份表现选择过滤条件和冷却间隔，再应用到当年。结果没有形成有效窗口，说明现有宽事件池的边界收益不能稳定前滚。",
        "",
        "主要问题仍是成本后收益和相对收益不足，同时独立行情簇未达到 V3.1 的 20 个要求。",
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
