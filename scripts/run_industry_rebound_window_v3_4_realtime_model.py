#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v3_4_realtime_model_policy.json"
V20_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_online_state_machine_v2_20.py"
VERSION = "3.4.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V3.4 exogenous proxy + walk-forward realtime rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V3.4 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    parser.add_argument("--refresh-market-index", action="store_true", help="Refresh wide market index cache.")
    parser.add_argument("--refresh-market-index-only", action="store_true", help="Refresh wide market index cache and exit.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    if args.refresh_market_index or args.refresh_market_index_only:
        policy["refresh_market_index"] = True
    if args.refresh_market_index_only:
        market_index_data, market_index_audit = load_market_indices(policy)
        expected = len(policy["market_indices"])
        if len(market_index_data) != expected or int(market_index_audit["status"].eq("pass").sum()) != expected:
            raise SystemExit("wide market index refresh incomplete")
        print(f"market_index_refreshed={len(market_index_data)}/{expected}")
        return
    source_policy = read_json(ROOT / policy["source_policy_path"])
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v20 = load_v20_module()
    close_matrix = v20.load_close_matrix(ROOT / policy["industry_history_dir"])
    amount_matrix = load_amount_matrix(ROOT / policy["industry_history_dir"])
    market_index_data, market_index_audit = load_market_indices(policy)

    features = v20.build_daily_features(close_matrix, source_policy)
    features = add_industry_liquidity_features(features, amount_matrix)
    features = add_market_volatility_ratio(features)
    features = add_wide_market_features(features, market_index_data)
    panel = add_rebound_targets(features, policy)

    data_audit = build_data_availability_audit(policy, close_matrix, amount_matrix, market_index_audit, panel)
    target_audit = build_target_label_audit(panel, policy)
    rule_summary, rule_events = run_rule_audit(panel, policy)
    predictions, model_year_summary, model_summary = run_walk_forward_model(panel, policy)
    realtime_trades, realtime_summary = run_realtime_simulation(panel, predictions, policy)
    annual_distribution = build_annual_distribution(rule_events, predictions, realtime_trades)
    top_candidates = build_top_candidates(rule_summary, model_summary, realtime_summary, policy)
    leakage_audit = build_leakage_audit(policy, data_audit, predictions)
    notes = build_optimization_notes(top_candidates, model_summary, realtime_summary, rule_summary)
    run_summary = build_run_summary(policy, panel, top_candidates, data_audit, target_audit, leakage_audit, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v3_feature_target_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    rule_summary.to_csv(debug_dir / "exogenous_rule_summary.csv", index=False, encoding="utf-8-sig")
    rule_events.to_csv(debug_dir / "exogenous_rule_events.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    model_year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    realtime_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    realtime_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", {"v3_4_policy": policy, "source_v2_20_policy": source_policy})
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_report(run_summary, top_candidates, data_audit, target_audit, model_year_summary, realtime_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V3.4外生风险代理与实时仿真反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"市场指数可用数={run_summary['market_index_pass_count']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最佳信号={run_summary['best_signal_id']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v20_module() -> Any:
    spec = importlib.util.spec_from_file_location("v20_state_machine", V20_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V2.20 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_amount_matrix(history_dir: Path) -> pd.DataFrame:
    frames: list[pd.Series] = []
    for path in sorted(history_dir.glob("*.csv")):
        raw = pd.read_csv(path, encoding="utf-8-sig")
        if "日期" not in raw.columns or "成交额" not in raw.columns:
            continue
        dates = pd.to_datetime(raw["日期"], errors="coerce")
        amount = pd.to_numeric(raw["成交额"], errors="coerce")
        series = pd.Series(amount.values, index=dates, name=path.stem.zfill(6)).dropna()
        series = series[~series.index.duplicated(keep="last")]
        if not series.empty:
            frames.append(series)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1, sort=True).sort_index()


def load_market_indices(policy: dict[str, Any]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    cache_dir = ROOT / policy["market_index_cache_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    index_data: dict[str, pd.DataFrame] = {}
    audit_rows: list[dict[str, Any]] = []
    refresh = bool(policy.get("refresh_market_index", False))
    for item in policy["market_indices"]:
        symbol = item["symbol"]
        cache_path = cache_dir / f"{symbol}.csv"
        status = "fail"
        source = "cache"
        error = ""
        frame = pd.DataFrame()
        if cache_path.exists() and not refresh:
            try:
                frame = pd.read_csv(cache_path, encoding="utf-8-sig")
                status = "pass"
            except Exception as exc:  # pragma: no cover - defensive cache read
                error = str(exc)
        if frame.empty:
            try:
                import akshare as ak

                raw = ak.stock_zh_index_daily(symbol=symbol)
                frame = raw.rename(columns={"date": "trade_date"}).copy()
                frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
                frame["symbol"] = symbol
                frame["name_zh"] = item["name_zh"]
                frame.to_csv(cache_path, index=False, encoding="utf-8-sig")
                status = "pass"
                source = "akshare.stock_zh_index_daily"
            except Exception as exc:
                error = str(exc)
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
            frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
            frame["volume"] = pd.to_numeric(frame.get("volume", np.nan), errors="coerce")
            frame = frame.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
            frame = frame[frame["trade_date"] >= pd.Timestamp(policy["feature_start_date"])]
            if not frame.empty:
                index_data[symbol] = frame
        audit_rows.append(
            {
                "symbol": symbol,
                "name_zh": item["name_zh"],
                "status": status if symbol in index_data else "fail",
                "source": source,
                "rows": int(len(index_data.get(symbol, pd.DataFrame()))),
                "start_date": date_text(index_data[symbol]["trade_date"].min()) if symbol in index_data else "",
                "end_date": date_text(index_data[symbol]["trade_date"].max()) if symbol in index_data else "",
                "cache_path": str(cache_path.relative_to(ROOT)),
                "error": error,
            }
        )
    return index_data, pd.DataFrame(audit_rows)


def add_industry_liquidity_features(features: pd.DataFrame, amount_matrix: pd.DataFrame) -> pd.DataFrame:
    output = features.copy()
    output["trade_date"] = pd.to_datetime(output["trade_date"], errors="coerce")
    if amount_matrix.empty:
        for col in ["market_amount_total", "market_amount_5d_vs_20d", "market_amount_20d_vs_120d", "liquidity_repair_5d"]:
            output[col] = math.nan
        output["amount_industry_count"] = 0
        return output
    amount = amount_matrix.sort_index().copy()
    total_amount = amount.sum(axis=1, skipna=True)
    count = amount.notna().sum(axis=1)
    liq = pd.DataFrame({"trade_date": total_amount.index, "market_amount_total": total_amount.values, "amount_industry_count": count.values})
    liq["amount_avg_5d"] = liq["market_amount_total"].rolling(5, min_periods=3).mean()
    liq["amount_avg_20d"] = liq["market_amount_total"].rolling(20, min_periods=10).mean()
    liq["amount_avg_120d"] = liq["market_amount_total"].rolling(120, min_periods=60).mean()
    liq["market_amount_5d_vs_20d"] = liq["amount_avg_5d"] / liq["amount_avg_20d"]
    liq["market_amount_20d_vs_120d"] = liq["amount_avg_20d"] / liq["amount_avg_120d"]
    liq["liquidity_repair_5d"] = liq["market_amount_5d_vs_20d"] - liq["market_amount_5d_vs_20d"].shift(5)
    keep = ["trade_date", "market_amount_total", "amount_industry_count", "market_amount_5d_vs_20d", "market_amount_20d_vs_120d", "liquidity_repair_5d"]
    return output.merge(liq[keep], on="trade_date", how="left")


def add_market_volatility_ratio(features: pd.DataFrame) -> pd.DataFrame:
    output = features.copy()
    daily = pd.to_numeric(output["market_daily_return"], errors="coerce")
    vol20 = daily.rolling(20, min_periods=12).std() * math.sqrt(252)
    vol60 = daily.rolling(60, min_periods=40).std() * math.sqrt(252)
    output["market_volatility_20d"] = vol20
    output["market_volatility_20d_vs_60d"] = vol20 / vol60
    return output


def add_wide_market_features(features: pd.DataFrame, market_index_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    output = features.copy()
    output["trade_date"] = pd.to_datetime(output["trade_date"], errors="coerce")
    if not market_index_data:
        return output
    feature_frames: list[pd.DataFrame] = []
    for symbol, frame in market_index_data.items():
        local = frame[["trade_date", "close", "volume"]].copy().sort_values("trade_date")
        close = pd.to_numeric(local["close"], errors="coerce")
        volume = pd.to_numeric(local["volume"], errors="coerce")
        local[f"{symbol}_return_5d"] = close / close.shift(5) - 1.0
        local[f"{symbol}_return_20d"] = close / close.shift(20) - 1.0
        local[f"{symbol}_return_60d"] = close / close.shift(60) - 1.0
        local[f"{symbol}_drawdown_252d"] = close / close.rolling(252, min_periods=120).max() - 1.0
        local[f"{symbol}_volume_5d_vs_20d"] = volume.rolling(5, min_periods=3).mean() / volume.rolling(20, min_periods=10).mean()
        feature_frames.append(local.drop(columns=["close", "volume"]))
    wide = feature_frames[0]
    for frame in feature_frames[1:]:
        wide = wide.merge(frame, on="trade_date", how="outer")
    return_cols_5 = [col for col in wide.columns if col.endswith("_return_5d")]
    return_cols_20 = [col for col in wide.columns if col.endswith("_return_20d")]
    return_cols_60 = [col for col in wide.columns if col.endswith("_return_60d")]
    drawdown_cols = [col for col in wide.columns if col.endswith("_drawdown_252d")]
    volume_cols = [col for col in wide.columns if col.endswith("_volume_5d_vs_20d")]
    wide["wide_avg_return_5d"] = wide[return_cols_5].mean(axis=1, skipna=True)
    wide["wide_avg_return_20d"] = wide[return_cols_20].mean(axis=1, skipna=True)
    wide["wide_avg_return_60d"] = wide[return_cols_60].mean(axis=1, skipna=True)
    wide["wide_positive_5d_ratio"] = (wide[return_cols_5] > 0).mean(axis=1, skipna=True)
    wide["wide_positive_20d_ratio"] = (wide[return_cols_20] > 0).mean(axis=1, skipna=True)
    wide["wide_avg_drawdown_252d"] = wide[drawdown_cols].mean(axis=1, skipna=True)
    wide["wide_volume_5d_vs_20d"] = wide[volume_cols].mean(axis=1, skipna=True)
    if "sh000852_return_20d" in wide.columns and "sh000300_return_20d" in wide.columns:
        wide["smallcap_vs_large_20d"] = wide["sh000852_return_20d"] - wide["sh000300_return_20d"]
    else:
        wide["smallcap_vs_large_20d"] = math.nan
    return output.merge(wide, on="trade_date", how="left")


def add_rebound_targets(features: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    panel = features.copy().sort_values("trade_date").reset_index(drop=True)
    horizon = int(policy["target_horizon"])
    nav = pd.to_numeric(panel["market_nav"], errors="coerce").reset_index(drop=True)
    entry = nav.shift(-1)
    exit_nav = nav.shift(-(horizon + 1))
    panel[f"forward_return_{horizon}d_next_close"] = exit_nav / entry - 1.0
    max_dd: list[float] = []
    for idx in range(len(panel)):
        entry_value = entry.iloc[idx]
        if pd.isna(entry_value) or idx + horizon + 1 >= len(nav):
            max_dd.append(math.nan)
            continue
        path = nav.iloc[idx + 1 : idx + horizon + 2] / entry_value - 1.0
        max_dd.append(float(path.min()) if len(path) else math.nan)
    panel[f"forward_max_drawdown_{horizon}d_next_close"] = max_dd
    ret = pd.to_numeric(panel[f"forward_return_{horizon}d_next_close"], errors="coerce")
    dd = pd.to_numeric(panel[f"forward_max_drawdown_{horizon}d_next_close"], errors="coerce")
    panel["target_rebound_window"] = ((ret >= float(policy["target_return_threshold"])) & (dd >= float(policy["target_max_drawdown_floor"]))).astype(int)
    panel["is_bad_window"] = (ret <= float(policy["bad_window_threshold"])).astype(int)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    panel["year"] = panel["trade_date"].dt.year
    return panel.dropna(subset=["trade_date"]).reset_index(drop=True)


def build_data_availability_audit(policy: dict[str, Any], close_matrix: pd.DataFrame, amount_matrix: pd.DataFrame, market_index_audit: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    feature_cols = list(policy["model"]["features"])
    rows = [
        {
            "audit_item": "industry_price_history",
            "status": "pass" if close_matrix.shape[1] >= 100 and len(close_matrix) >= 1200 else "fail",
            "evidence": f"industries={close_matrix.shape[1]}; rows={len(close_matrix)}",
            "action": "申万二级行业价格用于构建市场状态和目标收益。",
        },
        {
            "audit_item": "industry_amount_history",
            "status": "pass" if amount_matrix.shape[1] >= 100 and len(amount_matrix) >= 1200 else "fail",
            "evidence": f"industries={amount_matrix.shape[1]}; rows={len(amount_matrix)}",
            "action": "行业成交额用于本地流动性代理。",
        },
        {
            "audit_item": "wide_market_index_history",
            "status": "pass" if int((market_index_audit["status"] == "pass").sum()) >= 4 else "fail",
            "evidence": f"available={int((market_index_audit['status'] == 'pass').sum())}/{len(market_index_audit)}",
            "action": "宽基指数用于外生风险偏好代理。",
        },
        {
            "audit_item": "model_feature_coverage",
            "status": "pass" if panel[feature_cols].notna().mean().min() >= 0.65 else "fail",
            "evidence": f"min_feature_coverage={panel[feature_cols].notna().mean().min():.2%}",
            "action": "特征覆盖不足时不得升级为有效模型。",
        },
    ]
    for row in market_index_audit.to_dict("records"):
        rows.append(
            {
                "audit_item": f"market_index_{row['symbol']}",
                "status": row["status"],
                "evidence": f"{row['name_zh']}; rows={row['rows']}; {row['start_date']}~{row['end_date']}",
                "action": row["source"] if row["status"] == "pass" else row.get("error", ""),
            }
        )
    return pd.DataFrame(rows)


def build_target_label_audit(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    horizon = int(policy["target_horizon"])
    return_col = f"forward_return_{horizon}d_next_close"
    dd_col = f"forward_max_drawdown_{horizon}d_next_close"
    valid = panel.dropna(subset=[return_col, dd_col]).copy()
    pressure_mask = conditions_mask(valid, policy["baseline_pressure_conditions"], logic="all")
    oos_mask = valid["trade_date"] >= pd.Timestamp(policy["oos_start"])
    rows = [
        {
            "audit_item": "target_definition",
            "status": "pass",
            "evidence": f"{horizon}日下一收盘入场收益>={policy['target_return_threshold']:.2%}; 路径最大回撤>={policy['target_max_drawdown_floor']:.2%}",
            "action": "目标同时要求上涨和路径可承受，不把牛市普通上涨直接等同抄底窗口。",
        },
        metric_row("all_dates", valid),
        metric_row("pressure_dates", valid[pressure_mask]),
        metric_row("oos_dates", valid[oos_mask]),
        metric_row("oos_pressure_dates", valid[oos_mask & pressure_mask]),
    ]
    return pd.DataFrame(rows)


def metric_row(name: str, frame: pd.DataFrame) -> dict[str, Any]:
    target = pd.to_numeric(frame.get("target_rebound_window", pd.Series(dtype=float)), errors="coerce")
    ret_col = next((col for col in frame.columns if col.startswith("forward_return_") and col.endswith("next_close")), "")
    ret = pd.to_numeric(frame[ret_col], errors="coerce") if ret_col else pd.Series(dtype=float)
    return {
        "audit_item": name,
        "status": "observe",
        "evidence": f"rows={len(frame)}; target_rate={(target.mean() if len(target) else math.nan):.2%}; mean_return={(ret.mean() if len(ret) else math.nan):.2%}",
        "action": "目标分布观察项。",
    }


def run_rule_audit(panel: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    event_frames: list[pd.DataFrame] = []
    valid = valid_panel(panel, policy)
    pressure_mask = conditions_mask(valid, policy["baseline_pressure_conditions"], logic="all")
    for rule in policy["rule_candidates"]:
        mask = conditions_mask(valid, rule["conditions"], logic="all")
        summary = summarize_signal(valid, mask, pressure_mask, rule["signal_id"], rule["signal_name_zh"], "预声明规则", policy)
        events = build_nonoverlap_events(valid, mask, rule["signal_id"], rule["signal_name_zh"], "预声明规则", policy)
        summary.update(summarize_event_frame(events))
        summary["status"] = classify_summary(summary, policy)
        summary_rows.append(summary)
        event_frames.append(events)
    return pd.DataFrame(summary_rows), concat_frames(event_frames)


def run_walk_forward_model(panel: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    model_policy = policy["model"]
    features = list(model_policy["features"])
    valid = valid_panel(panel, policy).copy()
    rows: list[pd.DataFrame] = []
    year_rows: list[dict[str, Any]] = []
    for year in range(int(model_policy["test_start_year"]), int(model_policy["test_end_year"]) + 1):
        test_start = pd.Timestamp(f"{year}-01-01")
        test_end = pd.Timestamp(f"{year}-12-31")
        train_end = test_start - pd.Timedelta(days=int(model_policy["purge_days"]))
        train = valid[valid["trade_date"] < train_end].dropna(subset=features + ["target_rebound_window"]).copy()
        test = valid[(valid["trade_date"] >= test_start) & (valid["trade_date"] <= test_end)].dropna(subset=features + ["target_rebound_window"]).copy()
        if len(train) < int(model_policy["train_min_rows"]) or test.empty:
            year_rows.append({"year": year, "status": "skip", "train_rows": len(train), "test_rows": len(test), "signal_dates": 0})
            continue
        x_train = train[features].astype(float).to_numpy()
        y_train = train["target_rebound_window"].astype(float).to_numpy()
        x_test = test[features].astype(float).to_numpy()
        mean = np.nanmean(x_train, axis=0)
        std = np.nanstd(x_train, axis=0)
        std = np.where(std < 1e-9, 1.0, std)
        x_train_z = np.nan_to_num((x_train - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)
        x_test_z = np.nan_to_num((x_test - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)
        weights = fit_logistic(x_train_z, y_train, model_policy)
        train_prob = predict_logistic(x_train_z, weights)
        test_prob = predict_logistic(x_test_z, weights)
        pressure_train = conditions_mask(train, policy["baseline_pressure_conditions"], logic="all").to_numpy(dtype=bool)
        threshold_source = train_prob[pressure_train] if pressure_train.any() else train_prob
        threshold = max(float(model_policy["minimum_probability_threshold"]), float(np.nanquantile(threshold_source, float(model_policy["probability_quantile"]))))
        test_out = test[["trade_date", "year", "market_nav", "target_rebound_window", f"forward_return_{policy['target_horizon']}d_next_close", f"forward_max_drawdown_{policy['target_horizon']}d_next_close"]].copy()
        test_out["model_probability"] = test_prob
        test_out["model_threshold"] = threshold
        test_out["model_signal"] = test_out["model_probability"] >= threshold
        test_out["signal_id"] = "walk_forward_probability_model"
        rows.append(test_out)
        signal = test_out[test_out["model_signal"]]
        year_rows.append(
            {
                "year": year,
                "status": "pass",
                "train_rows": len(train),
                "train_target_rate": float(y_train.mean()) if len(y_train) else math.nan,
                "test_rows": len(test),
                "threshold": threshold,
                "signal_dates": int(len(signal)),
                "signal_target_rate": float(signal["target_rebound_window"].mean()) if len(signal) else math.nan,
                "signal_mean_return": float(signal[f"forward_return_{policy['target_horizon']}d_next_close"].mean()) if len(signal) else math.nan,
            }
        )
    predictions = concat_frames(rows)
    year_summary = pd.DataFrame(year_rows)
    if predictions.empty:
        model_summary = pd.DataFrame([empty_model_summary(policy)])
        return predictions, year_summary, model_summary
    valid_pred = predictions.dropna(subset=[f"forward_return_{policy['target_horizon']}d_next_close"]).copy()
    mask = valid_pred["model_signal"].astype(bool)
    pressure_mask = pd.Series(False, index=valid_pred.index)
    base = valid_panel(panel, policy)[["trade_date", "market_stress_score", "negative_breadth_60d"]].copy()
    merged = valid_pred.merge(base, on="trade_date", how="left")
    pressure_mask = conditions_mask(merged, policy["baseline_pressure_conditions"], logic="all")
    summary = summarize_signal(merged, mask, pressure_mask, "walk_forward_probability_model", "Walk-forward概率模型", "轻量模型", policy)
    events = build_nonoverlap_events(merged, mask, "walk_forward_probability_model", "Walk-forward概率模型", "轻量模型", policy)
    summary.update(summarize_event_frame(events))
    summary["status"] = classify_summary(summary, policy)
    return predictions, year_summary, pd.DataFrame([summary])


def fit_logistic(x: np.ndarray, y: np.ndarray, model_policy: dict[str, Any]) -> np.ndarray:
    x_aug = np.column_stack([np.ones(len(x)), x])
    weights = np.zeros(x_aug.shape[1])
    lr = float(model_policy["learning_rate"])
    l2 = float(model_policy["l2"])
    pos_weight = float(model_policy["positive_weight"])
    sample_weight = np.where(y > 0.5, pos_weight, 1.0)
    for _ in range(int(model_policy["max_iter"])):
        pred = sigmoid(x_aug @ weights)
        grad = (x_aug.T @ ((pred - y) * sample_weight)) / max(float(sample_weight.sum()), 1.0)
        penalty = np.r_[0.0, weights[1:]] * l2 / len(x)
        weights -= lr * (grad + penalty)
    return weights


def predict_logistic(x: np.ndarray, weights: np.ndarray) -> np.ndarray:
    x_aug = np.column_stack([np.ones(len(x)), x])
    return sigmoid(x_aug @ weights)


def sigmoid(value: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -30, 30)))


def empty_model_summary(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_id": "walk_forward_probability_model",
        "signal_name_zh": "Walk-forward概率模型",
        "signal_type": "轻量模型",
        "signal_dates": 0,
        "status": "样本不足",
    }


def run_realtime_simulation(panel: pd.DataFrame, predictions: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if predictions.empty:
        return pd.DataFrame(), pd.DataFrame([{"signal_id": "v3_4_realtime_simulation", "status": "样本不足", "trades": 0}])
    horizon = int(policy["target_horizon"])
    full = panel.sort_values("trade_date").reset_index(drop=True).copy()
    signal_dates = set(pd.to_datetime(predictions.loc[predictions["model_signal"].astype(bool), "trade_date"]).dt.strftime("%Y-%m-%d"))
    rows: list[dict[str, Any]] = []
    last_exit = -1
    nav = pd.to_numeric(full["market_nav"], errors="coerce")
    for idx, row in full.iterrows():
        date_key = pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d")
        if date_key not in signal_dates or idx <= last_exit:
            continue
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
        rows.append(
            {
                "signal_id": "v3_4_realtime_simulation",
                "signal_date": date_key,
                "entry_date": pd.Timestamp(full.loc[entry_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "exit_date": pd.Timestamp(full.loc[exit_idx, "trade_date"]).strftime("%Y-%m-%d"),
                "holding_days": horizon,
                "trade_return": trade_return,
                "max_adverse_return": float(path.min()),
                "is_win": bool(trade_return > 0),
                "is_bad_window": bool(trade_return <= float(policy["bad_window_threshold"])),
                "year": int(pd.Timestamp(row["trade_date"]).year),
            }
        )
        last_exit = exit_idx
    trades = pd.DataFrame(rows)
    if trades.empty:
        summary = pd.DataFrame([{"signal_id": "v3_4_realtime_simulation", "signal_name_zh": "V3.4实时仿真", "signal_type": "实时仿真", "status": "样本不足", "trades": 0}])
        return trades, summary
    annual = trades["year"].value_counts(normalize=True)
    summary = {
        "signal_id": "v3_4_realtime_simulation",
        "signal_name_zh": "V3.4实时仿真",
        "signal_type": "实时仿真",
        "signal_dates": int(len(predictions.loc[predictions["model_signal"].astype(bool)])),
        "nonoverlap_events": int(len(trades)),
        "event_mean_return": float(trades["trade_return"].mean()),
        "event_win_rate": float(trades["is_win"].mean()),
        "event_bad_window_rate": float(trades["is_bad_window"].mean()),
        "event_worst_return": float(trades["trade_return"].min()),
        "max_single_year_concentration": float(annual.max()),
        "active_years": int(trades["year"].nunique()),
        "status": "反弹窗口候选",
    }
    summary["status"] = classify_summary(summary, policy)
    return trades, pd.DataFrame([summary])


def valid_panel(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    horizon = int(policy["target_horizon"])
    required = [f"forward_return_{horizon}d_next_close", f"forward_max_drawdown_{horizon}d_next_close", "target_rebound_window"]
    return panel.dropna(subset=required).copy()


def summarize_signal(frame: pd.DataFrame, mask: pd.Series, pressure_mask: pd.Series, signal_id: str, signal_name: str, signal_type: str, policy: dict[str, Any]) -> dict[str, Any]:
    horizon = int(policy["target_horizon"])
    return_col = f"forward_return_{horizon}d_next_close"
    dd_col = f"forward_max_drawdown_{horizon}d_next_close"
    selected = frame[mask.reindex(frame.index, fill_value=False)].copy()
    pressure = frame[pressure_mask.reindex(frame.index, fill_value=False)].copy()
    selected_ret = pd.to_numeric(selected[return_col], errors="coerce")
    pressure_ret = pd.to_numeric(pressure[return_col], errors="coerce")
    annual = selected["year"].value_counts(normalize=True) if not selected.empty else pd.Series(dtype=float)
    return {
        "signal_id": signal_id,
        "signal_name_zh": signal_name,
        "signal_type": signal_type,
        "signal_dates": int(len(selected)),
        "target_capture_rate": float(selected["target_rebound_window"].mean()) if len(selected) else math.nan,
        "mean_return": float(selected_ret.mean()) if len(selected_ret) else math.nan,
        "mean_drawdown": float(pd.to_numeric(selected[dd_col], errors="coerce").mean()) if len(selected) else math.nan,
        "pressure_mean_return": float(pressure_ret.mean()) if len(pressure_ret) else math.nan,
        "mean_edge_vs_pressure": safe_sub(selected_ret.mean() if len(selected_ret) else math.nan, pressure_ret.mean() if len(pressure_ret) else math.nan),
        "bad_window_rate": float((selected_ret <= float(policy["bad_window_threshold"])).mean()) if len(selected_ret) else math.nan,
        "active_years": int(selected["year"].nunique()) if len(selected) else 0,
        "max_single_year_concentration": float(annual.max()) if len(annual) else math.nan,
    }


def build_nonoverlap_events(frame: pd.DataFrame, mask: pd.Series, signal_id: str, signal_name: str, signal_type: str, policy: dict[str, Any]) -> pd.DataFrame:
    horizon = int(policy["target_horizon"])
    return_col = f"forward_return_{horizon}d_next_close"
    dd_col = f"forward_max_drawdown_{horizon}d_next_close"
    selected = frame[mask.reindex(frame.index, fill_value=False)].sort_values("trade_date").copy()
    rows: list[dict[str, Any]] = []
    last_idx = -10_000_000
    for idx, row in selected.iterrows():
        pos = int(idx)
        if pos <= last_idx + horizon:
            continue
        ret = float(row[return_col])
        rows.append(
            {
                "signal_id": signal_id,
                "signal_name_zh": signal_name,
                "signal_type": signal_type,
                "trade_date": pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d"),
                "year": int(row["year"]),
                "event_return": ret,
                "event_max_drawdown": row.get(dd_col, math.nan),
                "target_rebound_window": int(row.get("target_rebound_window", 0)),
                "is_win": bool(ret > 0),
                "is_bad_window": bool(ret <= float(policy["bad_window_threshold"])),
                "market_stress_score": row.get("market_stress_score", math.nan),
                "wide_avg_return_5d": row.get("wide_avg_return_5d", math.nan),
                "wide_positive_5d_ratio": row.get("wide_positive_5d_ratio", math.nan),
                "market_amount_5d_vs_20d": row.get("market_amount_5d_vs_20d", math.nan),
            }
        )
        last_idx = pos
    return pd.DataFrame(rows)


def summarize_event_frame(events: pd.DataFrame) -> dict[str, Any]:
    if events.empty:
        return {
            "nonoverlap_events": 0,
            "event_mean_return": math.nan,
            "event_win_rate": math.nan,
            "event_bad_window_rate": math.nan,
            "event_worst_return": math.nan,
        }
    return {
        "nonoverlap_events": int(len(events)),
        "event_mean_return": float(pd.to_numeric(events["event_return"], errors="coerce").mean()),
        "event_win_rate": float(events["is_win"].mean()),
        "event_bad_window_rate": float(events["is_bad_window"].mean()),
        "event_worst_return": float(pd.to_numeric(events["event_return"], errors="coerce").min()),
    }


def classify_summary(row: dict[str, Any], policy: dict[str, Any]) -> str:
    th = policy["promotion_thresholds"]
    checks = {
        "signal_dates": nz(row.get("signal_dates")) >= float(th["min_signal_dates"]),
        "events": nz(row.get("nonoverlap_events")) >= float(th["min_nonoverlap_events"]),
        "active_years": nz(row.get("active_years")) >= float(th["min_active_years"]),
        "concentration": nz(row.get("max_single_year_concentration"), 1.0) <= float(th["max_single_year_concentration"]),
        "edge": nz(row.get("mean_edge_vs_pressure")) >= float(th["min_mean_edge_vs_pressure"]) if "mean_edge_vs_pressure" in row else True,
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


def build_annual_distribution(rule_events: pd.DataFrame, predictions: pd.DataFrame, realtime_trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not rule_events.empty:
        for (signal_id, year), group in rule_events.groupby(["signal_id", "year"]):
            rows.append({"source": "rule_nonoverlap", "signal_id": signal_id, "year": int(year), "count": int(len(group))})
    if not predictions.empty:
        signals = predictions[predictions["model_signal"].astype(bool)].copy()
        for year, group in signals.groupby("year"):
            rows.append({"source": "model_signal_dates", "signal_id": "walk_forward_probability_model", "year": int(year), "count": int(len(group))})
    if not realtime_trades.empty:
        for year, group in realtime_trades.groupby("year"):
            rows.append({"source": "realtime_trades", "signal_id": "v3_4_realtime_simulation", "year": int(year), "count": int(len(group))})
    return pd.DataFrame(rows)


def build_top_candidates(rule_summary: pd.DataFrame, model_summary: pd.DataFrame, realtime_summary: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    combined = concat_frames([rule_summary, model_summary, realtime_summary])
    if combined.empty:
        return pd.DataFrame()
    priority = {"反弹窗口候选": 0, "状态观察": 1, "样本不足": 2, "拒绝": 3}
    combined["_priority"] = combined["status"].map(priority).fillna(9)
    for col in ["event_mean_return", "mean_edge_vs_pressure", "event_win_rate"]:
        if col not in combined.columns:
            combined[col] = math.nan
    combined["_score"] = (
        2.0 * combined["event_mean_return"].map(nz)
        + 1.5 * combined["mean_edge_vs_pressure"].map(nz)
        + combined["event_win_rate"].map(nz)
        - combined.get("event_bad_window_rate", pd.Series(0, index=combined.index)).map(nz)
        - 0.4 * combined.get("max_single_year_concentration", pd.Series(1, index=combined.index)).map(lambda value: nz(value, 1.0))
    )
    combined = combined.sort_values(["_priority", "_score"], ascending=[True, False]).drop(columns=["_priority", "_score"])
    columns = [
        "signal_id",
        "signal_name_zh",
        "signal_type",
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


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "features use same-day close/volume; V3.4 simulation enters next trading day close",
                "action": "不使用未来收益、未来成交额或同日收盘执行。",
            },
            {
                "audit_item": "target_used_only_as_outcome",
                "status": "pass",
                "evidence": "target_rebound_window and forward_return fields are generated after features and only used for evaluation/training labels",
                "action": "目标标签不作为规则触发特征。",
            },
            {
                "audit_item": "purged_walk_forward",
                "status": "pass" if not predictions.empty else "fail",
                "evidence": f"purge_days={policy['model']['purge_days']}; prediction_rows={len(predictions)}",
                "action": "每个测试年份只用之前样本训练，并剔除测试前重叠标签窗口。",
            },
            {
                "audit_item": "external_proxy_availability",
                "status": "pass" if not data_audit[data_audit["audit_item"] == "wide_market_index_history"].empty and data_audit.loc[data_audit["audit_item"] == "wide_market_index_history", "status"].iloc[0] == "pass" else "fail",
                "evidence": data_audit.loc[data_audit["audit_item"] == "wide_market_index_history", "evidence"].iloc[0] if not data_audit[data_audit["audit_item"] == "wide_market_index_history"].empty else "",
                "action": "宽基风险代理进入V3.0数据层。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["research_boundary"],
                "action": "不生成买卖指令；通过也只是研究候选。",
            },
        ]
    )


def build_optimization_notes(top: pd.DataFrame, model_summary: pd.DataFrame, realtime_summary: pd.DataFrame, rule_summary: pd.DataFrame) -> dict[str, Any]:
    if top.empty:
        return {"main_diagnosis": "V3.4没有可排序结果。", "next_iterations": ["检查数据可得性。"]}
    best = top.iloc[0].to_dict()
    candidates = top[top["status"] == "反弹窗口候选"]
    notes = []
    if candidates.empty:
        notes.append("V3.4没有发现可升级的反弹窗口候选。")
    else:
        notes.append("V3.4发现反弹窗口候选，但仍必须保持research_only并等待未来样本。")
    notes.append(
        f"最佳项 {best.get('signal_id', '')}：状态 {best.get('status', '')}，"
        f"非重叠事件 {best.get('nonoverlap_events', 0)}，事件收益 {fmt_pct(best.get('event_mean_return'))}，"
        f"坏窗口 {fmt_pct(best.get('event_bad_window_rate'))}。"
    )
    if not realtime_summary.empty:
        rt = realtime_summary.iloc[0].to_dict()
        notes.append(
            f"实时仿真：交易 {int(nz(rt.get('nonoverlap_events', rt.get('trades', 0))))} 次，"
            f"平均收益 {fmt_pct(rt.get('event_mean_return'))}，坏窗口 {fmt_pct(rt.get('event_bad_window_rate'))}。"
        )
    notes.append("若仍无候选，下一步不应继续扩大参数网格，应优先获取更长历史的全市场广度、两融、利率/信用和政策周期数据。")
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "停止在现有样本上调参；只有接入更长、更独立的风险偏好数据后才值得继续。",
    }


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, target_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    candidates = top[top["status"] == "反弹窗口候选"] if not top.empty else pd.DataFrame()
    best = top.iloc[0].to_dict() if not top.empty else {}
    audit_fail_count = int((leakage["status"] == "fail").sum()) + int((data_audit["status"] == "fail").sum())
    market_pass = int(data_audit[data_audit["audit_item"].astype(str).str.startswith("market_index_")]["status"].eq("pass").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_target_panel_count": int(len(panel)),
        "market_index_pass_count": market_pass,
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
        return "research_only；仍未证明能有效找到反弹窗口"
    return "research_only；存在候选但仍需未来样本验证"


def render_report(summary: dict[str, Any], top: pd.DataFrame, data_audit: pd.DataFrame, target_audit: pd.DataFrame, model_year: pd.DataFrame, realtime_trades: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V3.4 外生风险代理与实时仿真反弹窗口研究报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines.extend(
        [
            "V3.4 将研究从 V2.x 的价格/成交额阈值调参，推进到宽基风险代理、反弹窗口目标标签、预声明规则、轻量 walk-forward 模型和下一交易日入场仿真。",
            "",
            f"- 特征标签面板行数：{summary['feature_target_panel_count']}",
            f"- 宽基指数可用数：{summary['market_index_pass_count']}",
            f"- 反弹窗口候选数：{summary['candidate_count']}",
            f"- 审计失败数：{summary['audit_fail_count']}",
            f"- 最终结论：{summary['final_verdict']}",
            f"- 主要诊断：{summary['main_diagnosis']}",
            "",
            "## 候选排序",
            "",
        ]
    )
    lines.extend(table_or_empty(top, {
        "signal_id": "信号ID",
        "signal_name_zh": "名称",
        "signal_type": "类型",
        "status": "状态",
        "signal_dates": "信号日",
        "nonoverlap_events": "非重叠事件",
        "active_years": "活跃年份",
        "max_single_year_concentration": "单年集中度",
        "target_capture_rate": "目标命中率",
        "mean_edge_vs_pressure": "相对压力日",
        "event_mean_return": "事件收益",
        "event_win_rate": "事件胜率",
        "event_bad_window_rate": "坏窗口",
        "event_worst_return": "最差事件",
    }, {
        "max_single_year_concentration", "target_capture_rate", "mean_edge_vs_pressure", "event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"
    }))
    lines += ["", "## V3.0 数据可得性", ""]
    lines.extend(table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## V3.1 目标标签", ""]
    lines.extend(table_or_empty(target_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "说明"}, set()))
    lines += ["", "## V3.3 Walk-forward 年度模型", ""]
    lines.extend(table_or_empty(model_year, {"year": "年份", "status": "状态", "train_rows": "训练样本", "train_target_rate": "训练目标率", "test_rows": "测试样本", "threshold": "阈值", "signal_dates": "信号日", "signal_target_rate": "信号目标率", "signal_mean_return": "信号收益"}, {"train_target_rate", "threshold", "signal_target_rate", "signal_mean_return"}))
    lines += ["", "## V3.4 实时仿真交易", ""]
    lines.extend(table_or_empty(realtime_trades.head(20), {"signal_date": "信号日", "entry_date": "入场日", "exit_date": "退出日", "trade_return": "收益", "max_adverse_return": "最大不利", "is_bad_window": "坏窗口"}, {"trade_return", "max_adverse_return"}))
    lines += ["", "## 审计", ""]
    lines.extend(table_or_empty(leakage, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += ["", "## 结论与下一步", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines.append(f"- 建议方向：{notes.get('recommended_next_direction', '')}")
    lines += [
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文 V3.4 研究报告，优先打开。",
        "- `top_candidates.csv`：规则、模型和实时仿真排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：V3.0 数据审计、V3.1 标签审计、V3.2 规则、V3.3 walk-forward、V3.4 实时仿真、年度分布、泄漏审计和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def conditions_mask(frame: pd.DataFrame, conditions: list[dict[str, Any]], logic: str = "all") -> pd.Series:
    result = pd.Series(True if logic == "all" else False, index=frame.index)
    for condition in conditions:
        mask = condition_mask(frame, condition)
        if logic == "all":
            result &= mask
        else:
            result |= mask
    return result.fillna(False)


def condition_mask(frame: pd.DataFrame, condition: dict[str, Any]) -> pd.Series:
    field = str(condition["field"])
    if field not in frame.columns:
        return pd.Series(False, index=frame.index)
    series = pd.to_numeric(frame[field], errors="coerce")
    value = float(condition["value"])
    op = str(condition["op"])
    if op == ">=":
        return (series >= value).fillna(False)
    if op == ">":
        return (series > value).fillna(False)
    if op == "<=":
        return (series <= value).fillna(False)
    if op == "<":
        return (series < value).fillna(False)
    raise ValueError(f"Unsupported op: {op}")


def table_or_empty(frame: pd.DataFrame, rename: dict[str, str], pct_cols: set[str]) -> list[str]:
    if frame.empty:
        return ["无数据。"]
    source = frame.loc[:, ~frame.columns.duplicated()].copy()
    display = source[[col for col in rename if col in source.columns]].copy()
    for col in display.columns:
        if col in pct_cols:
            display[col] = display[col].map(fmt_pct)
        elif pd.api.types.is_float_dtype(display[col]):
            display[col] = display[col].map(lambda value: fmt_float(value, 3))
    display = display.rename(columns=rename)
    cols = list(display.columns)
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in display.iterrows():
        values = [row.iloc[idx] for idx in range(len(cols))]
        lines.append("| " + " | ".join(str(value) if pd.notna(value) else "" for value in values) + " |")
    return lines


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


def safe_sub(left: Any, right: Any) -> float:
    left_number = float_or_nan(left)
    right_number = float_or_nan(right)
    if math.isnan(left_number) or math.isnan(right_number):
        return math.nan
    return float(left_number - right_number)


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


def fmt_float(value: Any, digits: int = 3) -> str:
    number = float_or_nan(value)
    return "" if math.isnan(number) else f"{number:.{digits}f}"


def date_text(value: Any) -> str:
    return "" if pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
