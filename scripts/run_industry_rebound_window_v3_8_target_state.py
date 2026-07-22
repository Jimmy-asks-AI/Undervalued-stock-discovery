#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v3_8_target_state_policy.json"
V37_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v3_7_industry_breadth.py"
VERSION = "3.8.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V3.8 target-state rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V3.8 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v37 = load_v37_module()
    v34 = v37.load_v34_module()
    v20 = v34.load_v20_module()
    source_policy = read_json(ROOT / policy["source_policy_path"])
    close_matrix = v20.load_close_matrix(ROOT / policy["industry_history_dir"])
    amount_matrix = v34.load_amount_matrix(ROOT / policy["industry_history_dir"])

    features = v20.build_daily_features(close_matrix, {**source_policy, **policy})
    features = v34.add_industry_liquidity_features(features, amount_matrix)
    features = v34.add_market_volatility_ratio(features)
    features = v37.add_industry_breadth_features(features, close_matrix)
    panel = add_target_profiles(features, policy)

    data_audit = v37.build_data_availability_audit(policy, close_matrix, amount_matrix, panel)
    target_audit = build_target_state_audit(panel, policy, v34)
    target_profile_audit = build_target_profile_audit(policy, target_audit)
    breadth_audit = v37.build_breadth_feature_audit(policy, panel)

    rule_summaries: list[pd.DataFrame] = []
    rule_events: list[pd.DataFrame] = []
    model_predictions: list[pd.DataFrame] = []
    model_years: list[pd.DataFrame] = []
    model_summaries: list[pd.DataFrame] = []
    profile_realtime_trades: list[pd.DataFrame] = []
    profile_realtime_summaries: list[pd.DataFrame] = []

    for profile in policy["target_profiles"]:
        profile_policy = make_profile_policy(policy, profile)
        profile_panel = make_profile_panel(panel, profile)
        rs, re = v34.run_rule_audit(profile_panel, profile_policy)
        pred, year_summary, model_summary = v34.run_walk_forward_model(profile_panel, profile_policy)
        rt, rts = v34.run_realtime_simulation(profile_panel, pred, profile_policy)

        rule_summaries.append(annotate_summary(rs, profile, prefix_signal=True))
        rule_events.append(annotate_events(re, profile, prefix_signal=True))
        model_predictions.append(annotate_predictions(pred, profile))
        model_years.append(annotate_year_summary(year_summary, profile))
        model_summaries.append(annotate_summary(model_summary, profile, prefix_signal=True))
        profile_realtime_trades.append(annotate_trades(rt, profile, signal_id=f"{profile['profile_id']}__realtime_simulation"))
        profile_realtime_summaries.append(annotate_summary(rts, profile, prefix_signal=True, replacement_signal_id=f"{profile['profile_id']}__realtime_simulation", replacement_name=f"{profile['profile_name_zh']}实时仿真"))

    rule_summary = concat_frames(rule_summaries)
    rule_event = concat_frames(rule_events)
    predictions = concat_frames(model_predictions)
    profile_year_summary = concat_frames(model_years)
    model_summary = concat_frames(model_summaries)
    profile_realtime_trade = concat_frames(profile_realtime_trades)
    profile_realtime_summary = concat_frames(profile_realtime_summaries)
    combined_trades, combined_summary, combined_year_summary, selected_signals = run_combined_realtime_simulation(panel, predictions, policy)
    top_candidates = build_top_candidates(v34, rule_summary, model_summary, profile_realtime_summary, combined_summary, policy)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions, selected_signals)
    notes = build_notes(top_candidates, combined_summary, profile_realtime_summary, model_summary)
    run_summary = build_run_summary(policy, panel, close_matrix, top_candidates, data_audit, leakage_audit, combined_summary, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v38_target_state_feature_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    target_profile_audit.to_csv(debug_dir / "target_profile_audit.csv", index=False, encoding="utf-8-sig")
    breadth_audit.to_csv(debug_dir / "breadth_feature_audit.csv", index=False, encoding="utf-8-sig")
    rule_summary.to_csv(debug_dir / "target_state_rule_summary.csv", index=False, encoding="utf-8-sig")
    rule_event.to_csv(debug_dir / "target_state_rule_events.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    profile_year_summary.to_csv(debug_dir / "profile_walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    combined_year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    profile_realtime_trade.to_csv(debug_dir / "profile_realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    profile_realtime_summary.to_csv(debug_dir / "profile_realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    selected_signals.to_csv(debug_dir / "combined_selected_signals.csv", index=False, encoding="utf-8-sig")
    combined_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    combined_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    build_annual_distribution(rule_event, predictions, combined_trades).to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(v34, run_summary, top_candidates, data_audit, target_audit, profile_year_summary, combined_trades, combined_summary, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V3.8多目标状态分型反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"目标分型数={run_summary['target_profile_count']}")
    print(f"组合实时交易数={run_summary['combined_realtime_events']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v37_module() -> Any:
    spec = importlib.util.spec_from_file_location("v37_industry_breadth", V37_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V3.7 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_target_profiles(features: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    panel = features.copy().sort_values("trade_date").reset_index(drop=True)
    nav = pd.to_numeric(panel["market_nav"], errors="coerce").reset_index(drop=True)
    entry = nav.shift(-1)
    for profile in policy["target_profiles"]:
        profile_id = profile["profile_id"]
        horizon = int(profile["target_horizon"])
        exit_nav = nav.shift(-(horizon + 1))
        ret_col = f"forward_return_{profile_id}"
        dd_col = f"forward_max_drawdown_{profile_id}"
        target_col = f"target_rebound_window_{profile_id}"
        bad_col = f"is_bad_window_{profile_id}"
        panel[ret_col] = exit_nav / entry - 1.0
        max_dd: list[float] = []
        for idx in range(len(panel)):
            entry_value = entry.iloc[idx]
            if pd.isna(entry_value) or idx + horizon + 1 >= len(nav):
                max_dd.append(math.nan)
                continue
            path = nav.iloc[idx + 1 : idx + horizon + 2] / entry_value - 1.0
            max_dd.append(float(path.min()) if len(path) else math.nan)
        panel[dd_col] = max_dd
        ret = pd.to_numeric(panel[ret_col], errors="coerce")
        dd = pd.to_numeric(panel[dd_col], errors="coerce")
        panel[target_col] = ((ret >= float(profile["target_return_threshold"])) & (dd >= float(profile["target_max_drawdown_floor"]))).astype(int)
        panel[bad_col] = (ret <= float(profile["bad_window_threshold"])).astype(int)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    panel["year"] = panel["trade_date"].dt.year
    return panel.dropna(subset=["trade_date"]).reset_index(drop=True)


def make_profile_policy(policy: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    local = json.loads(json.dumps(policy, ensure_ascii=False))
    local["policy_id"] = f"{policy['policy_id']}__{profile['profile_id']}"
    local["target_horizon"] = int(profile["target_horizon"])
    local["target_return_threshold"] = float(profile["target_return_threshold"])
    local["target_max_drawdown_floor"] = float(profile["target_max_drawdown_floor"])
    local["bad_window_threshold"] = float(profile["bad_window_threshold"])
    local["research_boundary"] = policy["research_boundary"]
    return local


def make_profile_panel(panel: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    local = panel.copy()
    profile_id = profile["profile_id"]
    horizon = int(profile["target_horizon"])
    local[f"forward_return_{horizon}d_next_close"] = local[f"forward_return_{profile_id}"]
    local[f"forward_max_drawdown_{horizon}d_next_close"] = local[f"forward_max_drawdown_{profile_id}"]
    local["target_rebound_window"] = local[f"target_rebound_window_{profile_id}"]
    local["is_bad_window"] = local[f"is_bad_window_{profile_id}"]
    return local


def annotate_summary(frame: pd.DataFrame, profile: dict[str, Any], prefix_signal: bool, replacement_signal_id: str | None = None, replacement_name: str | None = None) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    output["target_profile_id"] = profile["profile_id"]
    output["target_profile_name_zh"] = profile["profile_name_zh"]
    output["target_horizon"] = int(profile["target_horizon"])
    if "signal_id" in output.columns:
        if replacement_signal_id:
            output["signal_id"] = replacement_signal_id
        elif prefix_signal:
            output["signal_id"] = profile["profile_id"] + "__" + output["signal_id"].astype(str)
    if replacement_name and "signal_name_zh" in output.columns:
        output["signal_name_zh"] = replacement_name
    return output


def annotate_events(frame: pd.DataFrame, profile: dict[str, Any], prefix_signal: bool) -> pd.DataFrame:
    output = annotate_summary(frame, profile, prefix_signal)
    if not output.empty:
        output["target_profile_priority"] = int(profile["priority"])
    return output


def annotate_predictions(frame: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    horizon = int(profile["target_horizon"])
    output["target_profile_id"] = profile["profile_id"]
    output["target_profile_name_zh"] = profile["profile_name_zh"]
    output["target_profile_priority"] = int(profile["priority"])
    output["target_horizon"] = horizon
    output["target_return_threshold"] = float(profile["target_return_threshold"])
    output["target_max_drawdown_floor"] = float(profile["target_max_drawdown_floor"])
    output["profile_bad_window_threshold"] = float(profile["bad_window_threshold"])
    output["probability_margin"] = pd.to_numeric(output.get("model_probability", math.nan), errors="coerce") - pd.to_numeric(output.get("model_threshold", math.nan), errors="coerce")
    return_col = f"forward_return_{horizon}d_next_close"
    dd_col = f"forward_max_drawdown_{horizon}d_next_close"
    if return_col in output.columns:
        output["profile_forward_return"] = output[return_col]
    if dd_col in output.columns:
        output["profile_forward_max_drawdown"] = output[dd_col]
    output["signal_id"] = profile["profile_id"] + "__walk_forward_probability_model"
    return output


def annotate_year_summary(frame: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    output["target_profile_id"] = profile["profile_id"]
    output["target_profile_name_zh"] = profile["profile_name_zh"]
    output["target_horizon"] = int(profile["target_horizon"])
    return output


def annotate_trades(frame: pd.DataFrame, profile: dict[str, Any], signal_id: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    output["signal_id"] = signal_id
    output["target_profile_id"] = profile["profile_id"]
    output["target_profile_name_zh"] = profile["profile_name_zh"]
    output["target_horizon"] = int(profile["target_horizon"])
    return output


def run_combined_realtime_simulation(panel: pd.DataFrame, predictions: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    columns = ["signal_id", "signal_date", "entry_date", "exit_date", "holding_days", "trade_return", "max_adverse_return", "is_win", "is_bad_window", "year", "target_profile_id", "target_profile_name_zh"]
    if predictions.empty:
        empty_summary = pd.DataFrame([{"signal_id": "v3_8_combined_realtime_simulation", "signal_name_zh": "V3.8组合实时仿真", "signal_type": "组合实时仿真", "status": "样本不足", "trades": 0}])
        return pd.DataFrame(columns=columns), empty_summary, empty_year_summary(policy), pd.DataFrame()
    signals = predictions[predictions["model_signal"].astype(bool)].copy()
    if signals.empty:
        empty_summary = pd.DataFrame([{"signal_id": "v3_8_combined_realtime_simulation", "signal_name_zh": "V3.8组合实时仿真", "signal_type": "组合实时仿真", "status": "样本不足", "trades": 0}])
        return pd.DataFrame(columns=columns), empty_summary, empty_year_summary(policy), pd.DataFrame()
    signals["trade_date"] = pd.to_datetime(signals["trade_date"], errors="coerce")
    signals = signals.dropna(subset=["trade_date"]).copy()
    signals["_date_key"] = signals["trade_date"].dt.strftime("%Y-%m-%d")
    signals = signals.sort_values(["trade_date", "probability_margin", "target_profile_priority"], ascending=[True, False, True])
    selected = signals.groupby("_date_key", as_index=False, sort=False).head(1).sort_values("trade_date").reset_index(drop=True)

    full = panel.sort_values("trade_date").reset_index(drop=True).copy()
    full["trade_date"] = pd.to_datetime(full["trade_date"], errors="coerce")
    date_to_idx = {pd.Timestamp(value).strftime("%Y-%m-%d"): int(idx) for idx, value in full["trade_date"].items()}
    nav = pd.to_numeric(full["market_nav"], errors="coerce")
    rows: list[dict[str, Any]] = []
    last_exit = -1
    for _, signal in selected.iterrows():
        date_key = signal["_date_key"]
        idx = date_to_idx.get(date_key)
        if idx is None or idx <= last_exit:
            continue
        horizon = int(signal["target_horizon"])
        entry_idx = idx + 1
        exit_idx = idx + 1 + horizon
        if exit_idx >= len(full):
            continue
        entry_nav = nav.iloc[entry_idx]
        exit_nav = nav.iloc[exit_idx]
        if pd.isna(entry_nav) or pd.isna(exit_nav):
            continue
        path = nav.iloc[entry_idx : exit_idx + 1] / entry_nav - 1.0
        trade_return = float(exit_nav / entry_nav - 1.0)
        max_adverse = float(path.min())
        target_hit = bool(trade_return >= float(signal["target_return_threshold"]) and max_adverse >= float(signal["target_max_drawdown_floor"]))
        rows.append(
            {
                "signal_id": "v3_8_combined_realtime_simulation",
                "signal_date": date_key,
                "entry_date": pd.Timestamp(full.loc[entry_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "exit_date": pd.Timestamp(full.loc[exit_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "holding_days": horizon,
                "trade_return": trade_return,
                "max_adverse_return": max_adverse,
                "is_win": bool(trade_return > 0),
                "is_bad_window": bool(trade_return <= float(signal["profile_bad_window_threshold"])),
                "target_hit": target_hit,
                "year": int(pd.Timestamp(signal["trade_date"]).year),
                "target_profile_id": signal["target_profile_id"],
                "target_profile_name_zh": signal["target_profile_name_zh"],
                "model_probability": float(signal["model_probability"]),
                "model_threshold": float(signal["model_threshold"]),
                "probability_margin": float(signal["probability_margin"]),
            }
        )
        last_exit = exit_idx
    trades = pd.DataFrame(rows, columns=columns + ["target_hit", "model_probability", "model_threshold", "probability_margin"])
    if trades.empty:
        empty_summary = pd.DataFrame([{"signal_id": "v3_8_combined_realtime_simulation", "signal_name_zh": "V3.8组合实时仿真", "signal_type": "组合实时仿真", "status": "样本不足", "trades": 0}])
        return trades, empty_summary, empty_year_summary(policy), selected
    annual = trades["year"].value_counts(normalize=True)
    summary = {
        "signal_id": "v3_8_combined_realtime_simulation",
        "signal_name_zh": "V3.8组合实时仿真",
        "signal_type": "组合实时仿真",
        "signal_dates": int(len(selected)),
        "nonoverlap_events": int(len(trades)),
        "event_mean_return": float(trades["trade_return"].mean()),
        "event_win_rate": float(trades["is_win"].mean()),
        "event_bad_window_rate": float(trades["is_bad_window"].mean()),
        "event_worst_return": float(trades["trade_return"].min()),
        "target_capture_rate": float(trades["target_hit"].mean()),
        "max_single_year_concentration": float(annual.max()),
        "active_years": int(trades["year"].nunique()),
        "status": "反弹窗口候选",
    }
    summary["status"] = classify_summary(summary, policy)
    summary_frame = pd.DataFrame([summary])
    year_summary = build_combined_year_summary(policy, selected, trades)
    return trades, summary_frame, year_summary, selected


def build_combined_year_summary(policy: dict[str, Any], selected: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    selected = selected.copy()
    if not selected.empty:
        selected["year"] = pd.to_datetime(selected["trade_date"], errors="coerce").dt.year
    for year in range(int(policy["model"]["test_start_year"]), int(policy["model"]["test_end_year"]) + 1):
        selected_year = selected[selected["year"] == year] if not selected.empty and "year" in selected.columns else pd.DataFrame()
        trades_year = trades[trades["year"] == year] if not trades.empty else pd.DataFrame()
        rows.append(
            {
                "year": year,
                "status": "pass",
                "train_rows": math.nan,
                "test_rows": math.nan,
                "signal_dates": int(len(selected_year)),
                "signal_target_rate": float(trades_year["target_hit"].mean()) if len(trades_year) else math.nan,
                "signal_mean_return": float(trades_year["trade_return"].mean()) if len(trades_year) else math.nan,
            }
        )
    return pd.DataFrame(rows)


def empty_year_summary(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"year": year, "status": "pass", "train_rows": math.nan, "test_rows": math.nan, "signal_dates": 0, "signal_target_rate": math.nan, "signal_mean_return": math.nan}
            for year in range(int(policy["model"]["test_start_year"]), int(policy["model"]["test_end_year"]) + 1)
        ]
    )


def build_target_state_audit(panel: pd.DataFrame, policy: dict[str, Any], v34: Any) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for profile in policy["target_profiles"]:
        profile_id = profile["profile_id"]
        ret_col = f"forward_return_{profile_id}"
        dd_col = f"forward_max_drawdown_{profile_id}"
        target_col = f"target_rebound_window_{profile_id}"
        valid = panel.dropna(subset=[ret_col, dd_col]).copy()
        pressure_mask = v34.conditions_mask(valid, policy["baseline_pressure_conditions"], logic="all") if not valid.empty else pd.Series(dtype=bool)
        oos_mask = valid["trade_date"] >= pd.Timestamp(policy["oos_start"]) if not valid.empty else pd.Series(dtype=bool)
        for sample_name, frame in [
            ("all_dates", valid),
            ("pressure_dates", valid[pressure_mask] if len(valid) else valid),
            ("oos_dates", valid[oos_mask] if len(valid) else valid),
            ("oos_pressure_dates", valid[oos_mask & pressure_mask] if len(valid) else valid),
        ]:
            target = pd.to_numeric(frame.get(target_col, pd.Series(dtype=float)), errors="coerce")
            ret = pd.to_numeric(frame.get(ret_col, pd.Series(dtype=float)), errors="coerce")
            rows.append(
                {
                    "target_profile_id": profile_id,
                    "target_profile_name_zh": profile["profile_name_zh"],
                    "sample": sample_name,
                    "horizon": int(profile["target_horizon"]),
                    "status": "observe",
                    "rows": int(len(frame)),
                    "target_rate": float(target.mean()) if len(target) else math.nan,
                    "mean_return": float(ret.mean()) if len(ret) else math.nan,
                    "definition": f"{profile['target_horizon']}日收益>={profile['target_return_threshold']:.2%}; 最大不利>={profile['target_max_drawdown_floor']:.2%}",
                }
            )
    return pd.DataFrame(rows)


def build_target_profile_audit(policy: dict[str, Any], target_audit: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for profile in policy["target_profiles"]:
        profile_id = profile["profile_id"]
        frame = target_audit[target_audit["target_profile_id"] == profile_id] if not target_audit.empty else pd.DataFrame()

        def sample_value(sample: str, field: str) -> Any:
            sample_frame = frame[frame["sample"] == sample] if not frame.empty else pd.DataFrame()
            if sample_frame.empty or field not in sample_frame.columns:
                return math.nan
            return sample_frame.iloc[0][field]

        all_rows = int(nz(sample_value("all_dates", "rows")))
        oos_rows = int(nz(sample_value("oos_dates", "rows")))
        status = "pass" if all_rows > 0 and oos_rows > 0 else "fail"
        rows.append(
            {
                "target_profile_id": profile_id,
                "target_profile_name_zh": profile["profile_name_zh"],
                "priority": int(profile["priority"]),
                "target_horizon": int(profile["target_horizon"]),
                "target_return_threshold": float(profile["target_return_threshold"]),
                "target_max_drawdown_floor": float(profile["target_max_drawdown_floor"]),
                "bad_window_threshold": float(profile["bad_window_threshold"]),
                "all_rows": all_rows,
                "all_target_rate": float_or_none(sample_value("all_dates", "target_rate")),
                "oos_rows": oos_rows,
                "oos_target_rate": float_or_none(sample_value("oos_dates", "target_rate")),
                "oos_pressure_rows": int(nz(sample_value("oos_pressure_dates", "rows"))),
                "oos_pressure_target_rate": float_or_none(sample_value("oos_pressure_dates", "target_rate")),
                "status": status,
                "audit_note": "目标分型只作为标签和评价口径，不作为当日入场特征。",
                "description": profile.get("description", ""),
            }
        )
    return pd.DataFrame(rows)


def build_top_candidates(v34: Any, rule_summary: pd.DataFrame, model_summary: pd.DataFrame, profile_realtime_summary: pd.DataFrame, combined_summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    combined = v34.concat_frames([combined_summary, rule_summary, model_summary, profile_realtime_summary])
    if combined.empty:
        return combined
    priority = {"反弹窗口候选": 0, "状态观察": 1, "样本不足": 2, "拒绝": 3}
    combined["_priority"] = combined["status"].map(priority).fillna(9)
    for col in ["event_mean_return", "mean_edge_vs_pressure", "event_win_rate", "event_bad_window_rate", "max_single_year_concentration"]:
        if col not in combined.columns:
            combined[col] = math.nan
    combined["_score"] = (
        2.0 * combined["event_mean_return"].map(nz)
        + 1.5 * combined["mean_edge_vs_pressure"].map(nz)
        + combined["event_win_rate"].map(nz)
        - combined["event_bad_window_rate"].map(nz)
        - 0.4 * combined["max_single_year_concentration"].map(lambda value: nz(value, 1.0))
    )
    combined = combined.sort_values(["_priority", "_score"], ascending=[True, False]).drop(columns=["_priority", "_score"])
    columns = [
        "signal_id",
        "signal_name_zh",
        "signal_type",
        "target_profile_id",
        "target_profile_name_zh",
        "target_horizon",
        "status",
        "signal_dates",
        "nonoverlap_events",
        "active_years",
        "max_single_year_concentration",
        "target_capture_rate",
        "mean_return",
        "pressure_mean_return",
        "mean_edge_vs_pressure",
        "bad_window_rate",
        "event_mean_return",
        "event_win_rate",
        "event_bad_window_rate",
        "event_worst_return",
    ]
    return combined[[col for col in columns if col in combined.columns]]


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame, selected_signals: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "industry breadth features use trade-date close/amount only; simulation enters next trading day close",
                "action": "不使用未来行业广度，不做同日收盘执行。",
            },
            {
                "audit_item": "target_profiles_used_only_as_outcomes",
                "status": "pass",
                "evidence": "target profile returns/drawdowns are generated after features and only used for labels/evaluation",
                "action": "多目标标签不作为规则触发特征。",
            },
            {
                "audit_item": "purged_walk_forward",
                "status": "pass" if not predictions.empty else "fail",
                "evidence": f"purge_days={policy['model']['purge_days']}; prediction_rows={len(predictions)}",
                "action": "每个测试年份只用之前样本训练，并剔除最长目标窗口前的重叠标签。",
            },
            {
                "audit_item": "profile_selection_boundary",
                "status": "pass",
                "evidence": f"selected_signal_dates={len(selected_signals)}; method={policy['profile_selection']['method']}",
                "action": "目标分型选择只使用当日模型概率和预声明优先级。",
            },
            {
                "audit_item": "data_availability",
                "status": "pass" if int((data_audit["status"] == "fail").sum()) == 0 else "fail",
                "evidence": f"data_audit_failures={int((data_audit['status'] == 'fail').sum())}",
                "action": "数据可得性失败时不得升级。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["research_boundary"],
                "action": "不生成交易指令；通过也只是研究候选。",
            },
        ]
    )


def build_notes(top: pd.DataFrame, combined_summary: pd.DataFrame, profile_realtime_summary: pd.DataFrame, model_summary: pd.DataFrame) -> dict[str, Any]:
    notes: list[str] = []
    combined = combined_summary.iloc[0].to_dict() if not combined_summary.empty else {}
    if str(combined.get("status", "")) == "反弹窗口候选":
        notes.append("V3.8组合实时仿真达到研究候选状态，但仍必须保持research_only并等待更严格复核。")
    else:
        notes.append("V3.8多目标状态分型仍未证明能有效找到反弹窗口。")
    notes.append(
        f"组合实时仿真：非重叠事件 {int(nz(combined.get('nonoverlap_events', combined.get('trades', 0))))}，"
        f"平均收益 {fmt_pct(combined.get('event_mean_return'))}，胜率 {fmt_pct(combined.get('event_win_rate'))}，"
        f"坏窗口 {fmt_pct(combined.get('event_bad_window_rate'))}。"
    )
    if not profile_realtime_summary.empty:
        best_profile = profile_realtime_summary.sort_values("event_mean_return", ascending=False).head(1).iloc[0].to_dict()
        notes.append(
            f"单分型实时仿真中收益最高的是 {best_profile.get('target_profile_name_zh', '')}，"
            f"事件收益 {fmt_pct(best_profile.get('event_mean_return'))}，状态 {best_profile.get('status', '')}。"
        )
    if not model_summary.empty:
        best_model = model_summary.sort_values("event_mean_return", ascending=False).head(1).iloc[0].to_dict()
        notes.append(
            f"分型模型中最好的是 {best_model.get('target_profile_name_zh', '')}，"
            f"非重叠事件 {int(nz(best_model.get('nonoverlap_events', 0)))}，事件收益 {fmt_pct(best_model.get('event_mean_return'))}。"
        )
    notes.append("若 V3.8 仍失败，下一步应加入风险预算式出场，而不是继续扩展入场标签。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "在目标分型基础上测试持有期内动态退出和失败窗口快速撤退，重点提升胜率和平均收益。",
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, close_matrix: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, combined_summary: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = top[top["status"] == "反弹窗口候选"] if not top.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    combined = combined_summary.iloc[0].to_dict() if not combined_summary.empty else {}
    audit_fail_count = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_target_panel_count": int(len(panel)),
        "industry_count": int(close_matrix.shape[1]),
        "target_profile_count": int(len(policy["target_profiles"])),
        "combined_realtime_events": int(nz(combined.get("nonoverlap_events", combined.get("trades", 0)))),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_signal_id": best.get("signal_id", ""),
        "best_status": best.get("status", ""),
        "best_nonoverlap_events": int(nz(best.get("nonoverlap_events", 0))) if best else 0,
        "best_event_mean_return": float_or_none(best.get("event_mean_return")) if best else None,
        "best_event_bad_window_rate": float_or_none(best.get("event_bad_window_rate")) if best else None,
        "final_verdict": final_verdict(candidates, audit_fail_count),
        "main_diagnosis": notes.get("main_diagnosis", ""),
        "research_boundary": policy["research_boundary"],
    }


def final_verdict(candidates: pd.DataFrame, audit_fail_count: int) -> str:
    if audit_fail_count:
        return "research_only；存在数据或泄漏审计失败"
    if candidates.empty:
        return "research_only；多目标状态分型尚未证明能有效找到反弹窗口"
    return "research_only；存在多目标状态候选但仍需未来样本验证"


def render_report(v34: Any, summary: dict[str, Any], top: pd.DataFrame, data_audit: pd.DataFrame, target_audit: pd.DataFrame, profile_year: pd.DataFrame, combined_trades: pd.DataFrame, combined_summary: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V3.8 多目标状态分型反弹窗口研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V3.8 在 V3.7 行业广度特征基础上，将反弹窗口目标拆成10日技术反抽、20日修复波段和60日趋势反转，并用预声明规则合成组合实时仿真。",
        "",
        f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
        f"- 行业数：{summary['industry_count']}",
        f"- 目标分型数：{summary['target_profile_count']}",
        f"- 组合实时交易数：{summary['combined_realtime_events']}",
        f"- 反弹窗口候选数：{summary['candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 候选排序",
        "",
    ]
    lines.extend(v34.table_or_empty(top, {
        "signal_id": "信号ID",
        "signal_name_zh": "名称",
        "signal_type": "类型",
        "target_profile_name_zh": "目标分型",
        "target_horizon": "持有日",
        "status": "状态",
        "signal_dates": "信号日",
        "nonoverlap_events": "非重叠事件",
        "active_years": "活跃年份",
        "target_capture_rate": "目标命中率",
        "event_mean_return": "事件收益",
        "event_win_rate": "事件胜率",
        "event_bad_window_rate": "坏窗口",
        "event_worst_return": "最差事件",
    }, {"target_capture_rate", "event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"}))
    lines += ["", "## 数据可得性", ""]
    lines.extend(v34.table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## 目标分型标签", ""]
    lines.extend(v34.table_or_empty(target_audit.head(40), {"target_profile_name_zh": "目标分型", "sample": "样本", "horizon": "持有日", "rows": "行数", "target_rate": "目标率", "mean_return": "均值收益", "definition": "定义"}, {"target_rate", "mean_return"}))
    lines += ["", "## Walk-forward 年度分型", ""]
    lines.extend(v34.table_or_empty(profile_year.head(60), {"year": "年份", "target_profile_name_zh": "目标分型", "status": "状态", "train_rows": "训练样本", "test_rows": "测试样本", "signal_dates": "信号日", "signal_target_rate": "目标率", "signal_mean_return": "信号收益"}, {"signal_target_rate", "signal_mean_return"}))
    lines += ["", "## 组合实时仿真", ""]
    lines.extend(v34.table_or_empty(combined_summary, {"signal_id": "信号ID", "status": "状态", "signal_dates": "信号日", "nonoverlap_events": "非重叠交易", "event_mean_return": "平均收益", "event_win_rate": "胜率", "event_bad_window_rate": "坏窗口", "event_worst_return": "最差事件", "active_years": "活跃年份", "max_single_year_concentration": "单年集中"}, {"event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return", "max_single_year_concentration"}))
    lines += ["", "## 组合交易明细", ""]
    lines.extend(v34.table_or_empty(combined_trades.head(40), {"signal_date": "信号日", "entry_date": "入场日", "exit_date": "退出日", "target_profile_name_zh": "目标分型", "holding_days": "持有日", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
    lines += ["", "## 审计", ""]
    lines.extend(v34.table_or_empty(leakage, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += ["", "## 结论与下一步", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议方向：{notes.get('recommended_next_direction', '')}")
    lines += [
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文 V3.8 研究报告，优先打开。",
        "- `top_candidates.csv`：目标分型、规则、模型和组合实时仿真排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：目标分型特征面板、标签审计、分型规则、walk-forward、组合实时仿真、年度分布、泄漏审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def build_annual_distribution(rule_events: pd.DataFrame, predictions: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not rule_events.empty:
        for (signal_id, year), group in rule_events.groupby(["signal_id", "year"]):
            rows.append({"source": "rule_nonoverlap", "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    if not predictions.empty:
        signals = predictions[predictions["model_signal"].astype(bool)].copy()
        for (signal_id, year), group in signals.groupby(["signal_id", "year"]):
            rows.append({"source": "model_signal_dates", "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    if not trades.empty:
        for year, group in trades.groupby("year"):
            rows.append({"source": "combined_realtime_trades", "signal_id": "v3_8_combined_realtime_simulation", "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def classify_summary(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    checks = {
        "signal_dates": nz(row.get("signal_dates")) >= float(th["min_signal_dates"]),
        "events": nz(row.get("nonoverlap_events")) >= float(th["min_nonoverlap_events"]),
        "active_years": nz(row.get("active_years")) >= float(th["min_active_years"]),
        "concentration": nz(row.get("max_single_year_concentration"), 1.0) <= float(th["max_single_year_concentration"]),
        "event_return": nz(row.get("event_mean_return")) >= float(th["min_event_mean_return"]),
        "event_win": nz(row.get("event_win_rate")) >= float(th["min_event_win_rate"]),
        "event_bad": nz(row.get("event_bad_window_rate"), 1.0) <= float(th["max_event_bad_window_rate"]),
    }
    if all(checks.values()):
        return "反弹窗口候选"
    if not checks["signal_dates"] or not checks["events"] or not checks["active_years"]:
        return "样本不足"
    if checks["event_return"] and checks["event_bad"] and checks["event_win"]:
        return "状态观察"
    return "拒绝"


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


def nz(value: Any, default: float = 0.0) -> float:
    number = float_or_nan(value)
    return default if math.isnan(number) else number


def float_or_nan(value: Any, default: float = math.nan) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def float_or_none(value: Any) -> float | None:
    number = float_or_nan(value)
    return None if math.isnan(number) else number


def fmt_pct(value: Any) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number:.2%}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [clean_json_value(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value


if __name__ == "__main__":
    main()
