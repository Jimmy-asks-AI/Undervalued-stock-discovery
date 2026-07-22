#!/usr/bin/env python
from __future__ import annotations

import argparse
import copy
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

import evaluate_rebound_window_effectiveness as ev
import run_industry_rebound_window_v4_60_breadth_relief_event as event_builder


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_71_robustness_live_audit_policy.json"
FORWARD_SAMPLE_LEDGER = ROOT / "logs" / "v4_71_forward_sample_ledger.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.71 robustness and live-readiness audit.")
    parser.add_argument("--as-of-date", type=iso_date, default=None, help="Audit date, YYYY-MM-DD. Defaults to latest panel date.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    error = as_of_date_error(args.as_of_date, date.today()) if args.as_of_date else None
    if error:
        parser.error(error)

    cfg = read_json(CONFIG)
    source_policy = read_json(ROOT / cfg["source_policy"])
    eval_cfg = read_json(ROOT / cfg["evaluation_config"])
    out = ROOT / cfg["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    panel = pd.read_csv(ROOT / source_policy["source_panel"], encoding="utf-8-sig")
    base_trades = event_builder.build_trades(panel, source_policy)
    parameter = parameter_perturbation(cfg, source_policy, panel, eval_cfg)
    parameter_diagnosis = parameter_failure_diagnosis(parameter)
    cooldown = cooldown_sensitivity(base_trades, eval_cfg)
    annual = annual_breakdown(base_trades, eval_cfg)
    state = market_state_breakdown(base_trades, eval_cfg)
    year_state = year_state_breakdown(base_trades, eval_cfg)
    latest = latest_signal_status(panel, source_policy, cfg)
    carriers, carrier_audit = tradable_carrier_mapping(cfg)
    carrier_replay, execution_audit = carrier_execution_replay(cfg, base_trades, carriers)
    carrier_audit = pd.concat([carrier_audit, execution_audit], ignore_index=True)
    manual_review = manual_carrier_review_sheet(cfg, carriers, carrier_replay)
    summary = build_summary(cfg, parameter, cooldown, annual, state, year_state, latest, carriers, carrier_audit, carrier_replay, manual_review, args.as_of_date or None)

    top = pd.DataFrame([summary["top_candidate"]])
    top.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary["run_summary"])
    (out / "report.md").write_text(render_report(summary, parameter, parameter_diagnosis, cooldown, annual, state, year_state, latest, carriers, carrier_replay, carrier_audit), encoding="utf-8")
    (debug / "pre_entry_manual_review.md").write_text(render_pre_entry_manual_review(summary, latest, manual_review), encoding="utf-8")

    panel.to_csv(debug / "source_panel.csv", index=False, encoding="utf-8-sig")
    base_trades.to_csv(debug / "base_v4_70_trades.csv", index=False, encoding="utf-8-sig")
    parameter.to_csv(debug / "parameter_perturbation.csv", index=False, encoding="utf-8-sig")
    parameter_diagnosis.to_csv(debug / "parameter_failure_diagnosis.csv", index=False, encoding="utf-8-sig")
    cooldown.to_csv(debug / "cooldown_sensitivity.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(debug / "annual_breakdown.csv", index=False, encoding="utf-8-sig")
    state.to_csv(debug / "market_state_breakdown.csv", index=False, encoding="utf-8-sig")
    year_state.to_csv(debug / "year_state_breakdown.csv", index=False, encoding="utf-8-sig")
    latest.to_csv(debug / "latest_signal_status.csv", index=False, encoding="utf-8-sig")
    carriers.to_csv(debug / "tradable_carrier_mapping.csv", index=False, encoding="utf-8-sig")
    carrier_replay.to_csv(debug / "carrier_execution_replay.csv", index=False, encoding="utf-8-sig")
    manual_review.to_csv(debug / "manual_carrier_review_sheet.csv", index=False, encoding="utf-8-sig")
    carrier_audit.to_csv(debug / "carrier_mapping_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(summary["robustness_checks"]).to_csv(debug / "robustness_checks.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_panel", "status": "pass", "evidence": source_policy["source_panel"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "no_rule_reoptimization", "status": "pass", "evidence": "only frozen V4.70 perturbation and live-readiness audit"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "live_decision_packet.json", summary["live_decision_packet"])
    write_json(debug / "forward_sample_tracker.json", summary["forward_sample_tracker"])
    pd.DataFrame(summary["forward_sample_tracker"]["checklist"]).to_csv(debug / "forward_sample_checklist.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(summary["forward_sample_ledger_audit"]).to_csv(debug / "forward_sample_ledger_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(summary["pre_entry_gate"]).to_csv(debug / "pre_entry_gate.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(summary["production_readiness_debt"]).to_csv(debug / "production_readiness_debt.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "ponytail: V4.71 不优化新规则，只审计 V4.70 的扰动、冷却期、状态拆解、最新样本和载体可得性。"})
    write_json(debug / "frozen_policy.json", cfg)

    print(f"output_dir={out}")
    print(f"production_ready={summary['run_summary']['production_ready']}")
    print(f"blocking_issues={summary['run_summary']['blocking_issue_count']}")


def parameter_perturbation(cfg: dict[str, Any], base_policy: dict[str, Any], panel: pd.DataFrame, eval_cfg: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for item in cfg["parameter_perturbations"]:
        policy = copy.deepcopy(base_policy)
        policy["policy_id"] = item["variant_id"]
        policy["entry_lag_days"] = int(item.get("entry_lag_days", policy.get("entry_lag_days", 2)))
        if item.get("disable_stop"):
            policy.pop("conditional_stop_loss", None)
        elif "conditional_stop_loss" in policy:
            policy["conditional_stop_loss"]["level"] = float(item.get("stop_loss_level", policy["conditional_stop_loss"]["level"]))
            if item.get("stop_condition_all"):
                policy["conditional_stop_loss"]["conditions"] = []
            elif "vol_threshold" in item:
                policy["conditional_stop_loss"]["conditions"][0]["value"] = float(item["vol_threshold"])
        trades = event_builder.build_trades(panel, policy)
        metrics = score_trades(item["variant_id"], trades, eval_cfg)
        rows.append({
            "variant_id": item["variant_id"],
            "description": item["description"],
            "entry_lag_days": policy.get("entry_lag_days"),
            "stop_loss_level": (policy.get("conditional_stop_loss") or {}).get("level"),
            "vol_threshold": ((policy.get("conditional_stop_loss") or {}).get("conditions") or [{}])[0].get("value"),
            **metrics,
        })
    return pd.DataFrame(rows)


def score_trades(signal_id: str, trades: pd.DataFrame, eval_cfg: dict[str, Any]) -> dict[str, Any]:
    cost = float(eval_cfg["hard_gates"]["round_trip_cost_bps"]) / 10000.0
    if trades.empty:
        return {"events": 0, "score": 0.0, "effective": False}
    returns = pd.to_numeric(trades["trade_return"], errors="coerce")
    years = pd.to_datetime(trades["signal_date"], errors="coerce").dt.year
    row = {
        "signal_id": signal_id,
        "nonoverlap_events": len(trades),
        "trades": len(trades),
        "independent_event_clusters": ev.event_cluster_stats(trades, int(eval_cfg["hard_gates"]["independence_cluster_gap_calendar_days"]))["clusters"],
        "event_mean_return": float(returns.mean()),
        "mean_return": float(returns.mean()),
        "net_mean_return": float(returns.mean() - cost),
        "event_relative_mean_return": float(returns.mean()),
        "relative_mean_return": float(returns.mean()),
        "event_win_rate": float((returns > 0).mean()),
        "event_bad_window_rate": float(pd.Series(trades["is_bad_window"]).astype(bool).mean()),
        "event_worst_return": float(returns.min()),
        "active_years": int(years.nunique()),
        "max_single_year_concentration": float(years.value_counts(normalize=True).max()),
    }
    yearly = year_summary(trades)
    metrics = ev.build_metrics({"audit_fail_count": 0, "policy_id": signal_id}, pass_audit(), pass_audit(), row, trades, yearly, eval_cfg, None)
    metrics["policy_freeze_pass"] = True
    metrics["policy_freeze_evidence"] = "robustness audit variant"
    scorecard = ev.build_scorecard(metrics, eval_cfg)
    raw = sum(float(x["points"]) for x in scorecard)
    score, caps = ev.apply_score_caps(raw, scorecard, metrics, eval_cfg)
    status, _, failures = ev.classify(metrics, scorecard, eval_cfg)
    failed_score_metrics = [x["metric_id"] for x in scorecard if not bool(x["passed"])]
    return {
        "events": metrics["realtime_events"],
        "clusters": metrics["independent_event_clusters"],
        "score": score,
        "effective": bool(status == eval_cfg["status_labels"]["effective"]),
        "failed_metrics": ",".join(x["metric_id"] for x in failures),
        "failed_score_metrics": ",".join(failed_score_metrics),
        "net_mean_return": metrics["realtime_net_mean_return"],
        "relative_mean_return": metrics["realtime_relative_mean_return"],
        "win_rate": metrics["realtime_win_rate"],
        "bad_window_rate": metrics["realtime_bad_window_rate"],
        "worst_return": metrics["realtime_worst_return"],
        "worst_cluster_net_return": metrics["worst_cluster_net_return"],
        "path_worst_max_adverse_return": metrics["path_worst_max_adverse_return"],
        "annual_positive_rate": metrics["annual_positive_rate"],
        "score_caps": ",".join(x["cap_id"] for x in caps),
    }


def parameter_failure_diagnosis(parameter: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for row in parameter.to_dict("records"):
        failed = ",".join(x for x in [str(row.get("failed_metrics", "")), str(row.get("failed_score_metrics", ""))] if x)
        variant = str(row["variant_id"])
        effective = bool(row.get("effective"))
        if variant == "base_v4_70":
            family = "冻结基准"
        elif "entry_lag" in variant:
            family = "入场节奏"
        elif "stop_loss" in variant or "stop" in variant:
            family = "止损设置"
        elif "vol_threshold" in variant:
            family = "高波动触发阈值"
        else:
            family = "其他"
        if effective:
            diagnosis = "通过统一评价"
            action = "可作为冻结规则的稳健性支持"
        elif "path_drawdown_control" in failed:
            diagnosis = "收益更高但持有路径回撤失控"
            action = "不能为追求更高收益取消或放松保护止损"
        elif "tail_loss_control" in failed:
            diagnosis = "尾部亏损超出控制线"
            action = "不能放宽止损幅度"
        elif "worst_cluster_net_return" in failed:
            diagnosis = "最差独立行情簇亏损过大"
            action = "不能提前入场或一刀切止损"
        elif float(row.get("bad_window_rate", 0.0)) > 0.20:
            diagnosis = "坏窗口率超过 20% 上限"
            action = "该扰动只能作为反例，不进入实盘规则"
        else:
            diagnosis = "未达有效窗口认证分"
            action = "保持 research_only"
        rows.append({
            "variant_id": variant,
            "family": family,
            "score": row.get("score"),
            "effective": effective,
            "failed_metrics": row.get("failed_metrics", ""),
            "failed_score_metrics": row.get("failed_score_metrics", ""),
            "diagnosis": diagnosis,
            "action": action,
        })
    return pd.DataFrame(rows)


def cooldown_sensitivity(trades: pd.DataFrame, eval_cfg: dict[str, Any]) -> pd.DataFrame:
    cost = float(eval_cfg["hard_gates"]["round_trip_cost_bps"]) / 10000.0
    rows = []
    for gap in [30, 45, 60, 90]:
        clustered = ev.event_cluster_frame(trades, gap)
        if clustered.empty:
            rows.append({"cooldown_days": gap, "clusters": 0})
            continue
        clustered["_net"] = pd.to_numeric(clustered["trade_return"], errors="coerce") - cost
        means = clustered.groupby("_cluster_id")["_net"].mean()
        rows.append({
            "cooldown_days": gap,
            "clusters": int(means.size),
            "cluster_net_mean_return": float(means.mean()),
            "worst_cluster_net_return": float(means.min()),
            "cluster_positive_rate": float((means > 0).mean()),
            "max_cluster_concentration": float(clustered["_cluster_id"].value_counts().max() / len(clustered)),
            "passes_min_clusters_20": bool(means.size >= 20),
        })
    return pd.DataFrame(rows)


def annual_breakdown(trades: pd.DataFrame, eval_cfg: dict[str, Any]) -> pd.DataFrame:
    cost = float(eval_cfg["hard_gates"]["round_trip_cost_bps"]) / 10000.0
    rows = []
    for year, group in trades.groupby(pd.to_datetime(trades["signal_date"], errors="coerce").dt.year):
        ret = pd.to_numeric(group["trade_return"], errors="coerce")
        rows.append({
            "year": int(year),
            "events": int(len(group)),
            "net_mean_return": float(ret.mean() - cost),
            "win_rate": float((ret > 0).mean()),
            "bad_window_rate": float(pd.Series(group["is_bad_window"]).astype(bool).mean()),
            "worst_return": float(ret.min()),
            "path_worst_max_adverse_return": float(pd.to_numeric(group["max_adverse_return"], errors="coerce").min()),
        })
    return pd.DataFrame(rows)


def market_state_breakdown(trades: pd.DataFrame, eval_cfg: dict[str, Any]) -> pd.DataFrame:
    cost = float(eval_cfg["hard_gates"]["round_trip_cost_bps"]) / 10000.0
    frame = trades.copy()
    rows = []
    for dimension, labels in market_state_labels(frame).items():
        frame["_bucket"] = labels.astype(str)
        for bucket, group in frame.groupby("_bucket"):
            ret = pd.to_numeric(group["trade_return"], errors="coerce")
            rows.append({
                "dimension": dimension,
                "bucket": bucket,
                "events": int(len(group)),
                "net_mean_return": float(ret.mean() - cost),
                "win_rate": float((ret > 0).mean()),
                "bad_window_rate": float(pd.Series(group["is_bad_window"]).astype(bool).mean()),
                "worst_return": float(ret.min()),
            })
    return pd.DataFrame(rows)


def year_state_breakdown(trades: pd.DataFrame, eval_cfg: dict[str, Any]) -> pd.DataFrame:
    cost = float(eval_cfg["hard_gates"]["round_trip_cost_bps"]) / 10000.0
    frame = trades.copy()
    frame["_year"] = pd.to_datetime(frame["signal_date"], errors="coerce").dt.year
    rows = []
    for dimension, labels in market_state_labels(frame).items():
        frame["_bucket"] = labels.astype(str)
        for (year, bucket), group in frame.dropna(subset=["_year"]).groupby(["_year", "_bucket"]):
            ret = pd.to_numeric(group["trade_return"], errors="coerce")
            rows.append({
                "year": int(year),
                "dimension": dimension,
                "bucket": bucket,
                "events": int(len(group)),
                "net_mean_return": float(ret.mean() - cost),
                "win_rate": float((ret > 0).mean()),
                "bad_window_rate": float(pd.Series(group["is_bad_window"]).astype(bool).mean()),
                "worst_return": float(ret.min()),
            })
    return pd.DataFrame(rows)


def market_state_labels(frame: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "volatility_guard": pd.to_numeric(frame["market_volatility_20d_vs_60d"], errors="coerce").ge(1.30).map({True: "高波动保护区", False: "非高波动区"}),
        "stress_level": pd.cut(pd.to_numeric(frame["market_stress_score"], errors="coerce"), [-math.inf, 0.55, 0.70, math.inf], labels=["低/中压力", "中高压力", "高压力"]),
        "negative_breadth": pd.to_numeric(frame["negative_breadth_60d"], errors="coerce").ge(0.75).map({True: "深负广度", False: "普通负广度"}),
    }


def latest_signal_status(panel: pd.DataFrame, policy: dict[str, Any], cfg: dict[str, Any]) -> pd.DataFrame:
    frame = panel.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    latest = frame.iloc[-1]
    checks = []
    triggered = True
    for cond in policy["conditions"]:
        value = float(latest[cond["field"]])
        ok = compare(value, cond["op"], float(cond["value"]))
        triggered = triggered and ok
        checks.append(f"{cond['field']}={value:.4f} {cond['op']} {cond['value']} {'PASS' if ok else 'FAIL'}")
    stale_days = (date.today() - latest["trade_date"].date()).days
    entry_lag = int(policy.get("entry_lag_days", 2))
    holding_days = int(policy.get("holding_days", 20))
    planned_entry, planned_exit, calendar_source = planned_trade_dates(latest["trade_date"], entry_lag, holding_days) if triggered else ("未触发", "未触发", "not_triggered")
    return pd.DataFrame([{
        "latest_panel_date": latest["trade_date"].strftime("%Y-%m-%d"),
        "calendar_stale_days": int(stale_days),
        "freshness_status": "pass" if stale_days <= int(cfg["latest_data_max_stale_calendar_days"]) else "fail",
        "v4_70_triggered_on_latest_date": bool(triggered),
        "condition_checks": "; ".join(checks),
        "planned_entry_date_if_triggered": planned_entry,
        "planned_holding_days_if_triggered": holding_days if triggered else 0,
        "planned_exit_date_if_triggered": planned_exit,
        "trade_calendar_source": calendar_source,
        "latest_market_nav": float(latest["market_nav"]),
    }])


def planned_trade_dates(signal_date: pd.Timestamp, entry_lag: int, holding_days: int) -> tuple[str, str, str]:
    try:
        import akshare as ak
        dates = pd.to_datetime(ak.tool_trade_date_hist_sina()["trade_date"], errors="coerce").dropna().sort_values().reset_index(drop=True)
        source = "akshare.tool_trade_date_hist_sina"
    except Exception:
        # ponytail: weekday fallback only; replace with exchange calendar if AkShare calendar becomes unavailable.
        dates = pd.Series(pd.bdate_range(signal_date, periods=entry_lag + holding_days + 10))
        source = "weekday_fallback"
    start = dates.searchsorted(pd.to_datetime(signal_date), side="right")
    entry_i = start + entry_lag - 1
    exit_i = entry_i + holding_days
    if exit_i >= len(dates):
        return "交易日历不足", "交易日历不足", source
    return dates.iloc[entry_i].strftime("%Y-%m-%d"), dates.iloc[exit_i].strftime("%Y-%m-%d"), source


def tradable_carrier_mapping(cfg: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rules = cfg["tradable_carrier"]
    try:
        import akshare as ak
        raw = ak.fund_etf_spot_em()
    except Exception as exc:
        return pd.DataFrame(), pd.DataFrame([{"item": "akshare_fund_etf_spot_em", "status": "fail", "evidence": f"{type(exc).__name__}: {exc}"}])
    name = raw["名称"].astype(str)
    include = name.apply(lambda x: any(k in x for k in rules["include_keywords"]))
    exclude = name.apply(lambda x: any(k in x for k in rules["exclude_keywords"]))
    data = raw[include & ~exclude].copy()
    data["成交额"] = pd.to_numeric(data["成交额"], errors="coerce")
    data = data[data["成交额"] >= float(rules["min_turnover_amount"])].copy()
    data["carrier_role"] = data["名称"].map(lambda x: match_keyword(str(x), rules["include_keywords"]))
    data["mapping_confidence"] = data["成交额"].map(lambda x: "high" if x >= 100000000 else "medium")
    data["review_status"] = "人工复核候选，不是交易指令"
    data = data.sort_values("成交额", ascending=False).head(int(rules["top_n"]))
    keep = ["代码", "名称", "最新价", "基金折价率", "成交额", "流通市值", "总市值", "数据日期", "更新时间", "carrier_role", "mapping_confidence", "review_status"]
    audit = pd.DataFrame([
        {"item": "akshare_fund_etf_spot_em", "status": "pass", "evidence": f"raw_rows={len(raw)}"},
        {"item": "broad_market_carrier_candidates", "status": "pass" if len(data) else "fail", "evidence": f"candidates={len(data)}; min_turnover={rules['min_turnover_amount']}"},
    ])
    return data[[c for c in keep if c in data.columns]], audit


def carrier_execution_replay(cfg: dict[str, Any], trades: pd.DataFrame, carriers: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rules = cfg["tradable_carrier"]
    if carriers.empty:
        return pd.DataFrame(), pd.DataFrame([{"item": "carrier_execution_replay", "status": "fail", "evidence": "no carrier candidates"}])
    rows = []
    audit = []
    top_n = int(rules.get("execution_replay_top_n", 5))
    cost = float(rules.get("round_trip_cost_bps", 10)) / 10000.0
    cache_dir = ROOT / rules.get("history_cache_dir", "data_catalog/cache/tradable_carrier_etf_history")
    cache_dir.mkdir(parents=True, exist_ok=True)
    start = pd.to_datetime(trades["entry_date"], errors="coerce").min().strftime("%Y%m%d")
    end = pd.Timestamp.today().strftime("%Y%m%d")
    for carrier in carriers.head(top_n).to_dict("records"):
        code = str(carrier.get("代码", "")).zfill(6)
        hist, status, evidence = load_etf_history(code, start, end, cache_dir)
        audit.append({"item": f"carrier_history_{code}", "status": status, "evidence": evidence})
        if hist.empty:
            continue
        replay = replay_one_carrier(code, str(carrier.get("名称", "")), hist, trades, cost)
        rows.extend(replay.to_dict("records"))
    result = pd.DataFrame(rows)
    min_events = int(rules.get("min_replay_events", 30))
    if result.empty:
        audit.append({"item": "carrier_execution_replay", "status": "fail", "evidence": "no ETF history replay rows"})
    else:
        summary = result.groupby(["carrier_code", "carrier_name"]).agg(
            events=("signal_date", "count"),
            net_mean_return=("carrier_net_return", "mean"),
            win_rate=("carrier_net_return", lambda x: float((x > 0).mean())),
            mean_tracking_gap=("tracking_gap_vs_market_window", "mean"),
        ).reset_index()
        summary.to_csv(cache_dir / "latest_execution_replay_summary.csv", index=False, encoding="utf-8-sig")
        min_carriers = int(rules.get("min_replay_carriers", 3))
        qualified = summary[summary["events"] >= min_events]
        ok = qualified.shape[0] >= min_carriers
        audit.append({"item": "carrier_execution_replay", "status": "pass" if ok else "fail", "evidence": f"qualified_carriers={qualified.shape[0]}; min_carriers={min_carriers}; total_carriers={summary.shape[0]}; max_events={int(summary['events'].max())}; min_events={min_events}"})
    return result, pd.DataFrame(audit)


def load_etf_history(code: str, start: str, end: str, cache_dir: Path) -> tuple[pd.DataFrame, str, str]:
    cache = cache_dir / f"{code}.csv"
    cached = pd.DataFrame()
    if cache.exists():
        cached = normalize_etf_history(pd.read_csv(cache, encoding="utf-8-sig"))
        if etf_cache_is_recent(cached, end):
            return cached, "pass", f"cache={cache}; latest={cached['trade_date'].max():%Y-%m-%d}"
    errors = []
    try:
        import akshare as ak
        raw = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq")
    except Exception as exc:
        errors.append(f"fund_etf_hist_em {type(exc).__name__}: {str(exc)[:120]}")
        try:
            market_code = ("sh" if code.startswith(("5", "6")) else "sz") + code
            raw = ak.stock_zh_a_hist_tx(symbol=market_code, start_date=start, end_date=end, adjust="qfq")
        except Exception as fallback_exc:
            errors.append(f"stock_zh_a_hist_tx {type(fallback_exc).__name__}: {str(fallback_exc)[:120]}")
            if not cached.empty:
                return cached, "fail", f"stale_cache_retained; latest={cached['trade_date'].max():%Y-%m-%d}; " + " | ".join(errors)
            return pd.DataFrame(), "fail", " | ".join(errors)
    if raw.empty:
        if not cached.empty:
            return cached, "fail", f"stale_cache_retained; latest={cached['trade_date'].max():%Y-%m-%d}; empty history"
        return pd.DataFrame(), "fail", "empty history"
    temporary = cache.with_suffix(cache.suffix + ".tmp")
    raw.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(cache)
    source = "stock_zh_a_hist_tx" if "date" in raw.columns else "fund_etf_hist_em"
    return normalize_etf_history(raw), "pass", f"{source}; fetched_rows={len(raw)}"


def etf_cache_is_recent(frame: pd.DataFrame, requested_end: str, max_age_days: int = 5) -> bool:
    if frame.empty:
        return False
    latest = pd.to_datetime(frame["trade_date"], errors="coerce").max()
    return pd.notna(latest) and latest >= pd.to_datetime(requested_end) - pd.Timedelta(days=max_age_days)


def normalize_etf_history(raw: pd.DataFrame) -> pd.DataFrame:
    date_col = "日期" if "日期" in raw.columns else "date" if "date" in raw.columns else raw.columns[0]
    close_col = "收盘" if "收盘" in raw.columns else "close"
    if close_col not in raw.columns:
        return pd.DataFrame()
    out = raw[[date_col, close_col]].copy()
    out.columns = ["trade_date", "close"]
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.dropna().sort_values("trade_date").reset_index(drop=True)


def replay_one_carrier(code: str, name: str, hist: pd.DataFrame, trades: pd.DataFrame, cost: float) -> pd.DataFrame:
    rows = []
    dates = hist["trade_date"]
    for trade in trades.to_dict("records"):
        entry_i = dates.searchsorted(pd.to_datetime(trade["entry_date"]), side="left")
        exit_i = dates.searchsorted(pd.to_datetime(trade["exit_date"]), side="left")
        if entry_i >= len(hist) or exit_i >= len(hist) or exit_i <= entry_i:
            continue
        entry = hist.iloc[int(entry_i)]
        exit_ = hist.iloc[int(exit_i)]
        gross = float(exit_["close"] / entry["close"] - 1.0)
        net = gross - cost
        market_return = float(trade.get("trade_return", 0.0))
        rows.append({
            "carrier_code": code,
            "carrier_name": name,
            "signal_date": trade["signal_date"],
            "entry_date": entry["trade_date"].strftime("%Y-%m-%d"),
            "exit_date": exit_["trade_date"].strftime("%Y-%m-%d"),
            "carrier_gross_return": gross,
            "carrier_net_return": net,
            "market_window_return": market_return,
            "tracking_gap_vs_market_window": net - market_return,
        })
    return pd.DataFrame(rows)


def build_summary(
    cfg: dict[str, Any],
    parameter: pd.DataFrame,
    cooldown: pd.DataFrame,
    annual: pd.DataFrame,
    state: pd.DataFrame,
    year_state: pd.DataFrame,
    latest: pd.DataFrame,
    carriers: pd.DataFrame,
    carrier_audit: pd.DataFrame,
    carrier_replay: pd.DataFrame,
    manual_review: pd.DataFrame,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    base = parameter[parameter["variant_id"] == "base_v4_70"].iloc[0].to_dict()
    perturb_pass_rate = float(parameter["effective"].mean()) if len(parameter) else 0.0
    cooldown_60 = cooldown[cooldown["cooldown_days"] == 60].iloc[0].to_dict()
    latest_row = latest.iloc[0].to_dict()
    audit_as_of_date = as_of_date or str(latest_row["latest_panel_date"])
    carrier_counts = carrier_count_summary(cfg, carriers, carrier_replay, manual_review)
    year_state_sparse = year_state_sparsity(year_state)
    checks = [
        {"check": "base_v4_70_effective", "status": "pass" if base.get("effective") else "fail", "evidence": f"score={base.get('score')}"},
        {"check": "parameter_perturbation_pass_rate", "status": "pass" if perturb_pass_rate >= 0.6 else "fail", "evidence": f"{perturb_pass_rate:.2%} variants effective"},
        {"check": "cooldown_60_clusters", "status": "pass" if int(cooldown_60.get("clusters", 0)) >= 20 else "fail", "evidence": f"60d clusters={cooldown_60.get('clusters')}"},
        {"check": "year_state_sparse_cells", "status": "pass" if year_state_sparse["year_state_sparse_lt3_count"] == 0 else "fail", "evidence": f"sparse_lt3={year_state_sparse['year_state_sparse_lt3_count']}; min_events={year_state_sparse['year_state_min_events']}; cells={year_state_sparse['year_state_cell_count']}"},
        {"check": "latest_data_freshness", "status": latest_row["freshness_status"], "evidence": f"latest={latest_row['latest_panel_date']}; stale_days={latest_row['calendar_stale_days']}"},
        {"check": "tradable_carrier_current_data", "status": "pass" if len(carriers) else "fail", "evidence": f"carrier_candidates={len(carriers)}"},
        validate_position_sizing(cfg),
        execution_check(carrier_audit, carrier_replay),
    ]
    blocking = [x for x in checks if x["status"] == "fail"]
    production_ready = len(blocking) == 0
    final_verdict = build_final_verdict(production_ready, blocking)
    run_summary = {
        "version": cfg["version"],
        "policy_id": cfg["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_policy": cfg["source_policy"],
        "candidate_count": 0,
        "audit_fail_count": len(blocking),
        "production_ready": production_ready,
        "blocking_issue_count": len(blocking),
        "blocking_issues": blocking,
        "base_v4_70_score": float(base.get("score", 0.0)),
        "base_v4_70_effective": bool(base.get("effective", False)),
        "parameter_perturbation_effective_rate": perturb_pass_rate,
        "parameter_failed_variants": parameter_failed_variants(parameter),
        "parameter_failure_actions": parameter_failure_actions(parameter),
        "cooldown_60_clusters": int(cooldown_60.get("clusters", 0)),
        "cooldown_failing_gaps": cooldown_failing_gaps(cooldown),
        **year_state_sparse,
        "year_state_sparse_buckets": year_state_sparse_buckets(year_state),
        "latest_panel_date": latest_row["latest_panel_date"],
        "as_of_date": audit_as_of_date,
        "latest_signal_triggered": bool(latest_row["v4_70_triggered_on_latest_date"]),
        "planned_entry_date": latest_row["planned_entry_date_if_triggered"],
        "planned_exit_date": latest_row["planned_exit_date_if_triggered"],
        "tradable_carrier_candidates": int(len(carriers)),
        "carrier_execution_replay_rows": int(len(carrier_replay)),
        **carrier_counts,
        "final_verdict": final_verdict,
        "research_boundary": cfg["research_boundary"],
    }
    top_candidate = {
        "policy_id": cfg["policy_id"],
        "status": "production_candidate" if production_ready else "research_only_not_production_ready",
        "base_v4_70_effective": bool(base.get("effective", False)),
        "parameter_perturbation_effective_rate": perturb_pass_rate,
        "cooldown_60_clusters": int(cooldown_60.get("clusters", 0)),
        "latest_signal_triggered": bool(latest_row["v4_70_triggered_on_latest_date"]),
        "tradable_carrier_candidates": int(len(carriers)),
        "carrier_execution_replay_rows": int(len(carrier_replay)),
        **carrier_counts,
        "blocking_issue_count": len(blocking),
    }
    live_packet = build_live_decision_packet(cfg, run_summary, latest_row, carrier_replay)
    tracker = build_forward_sample_tracker(live_packet, run_summary, run_summary["as_of_date"])
    ledger_audit = audit_forward_sample_ledger(run_summary["as_of_date"])
    pre_entry_gate = build_pre_entry_gate(run_summary, latest_row, checks, ledger_audit)
    readiness_debt = build_production_readiness_debt(run_summary, parameter, cooldown)
    run_summary["forward_sample_ledger_status"] = ledger_audit[0]["status"]
    run_summary["forward_sample_skipped"] = ledger_audit[0]["skipped"]
    run_summary["forward_sample_pending_planned"] = ledger_audit[0]["pending_planned"]
    run_summary["forward_sample_pending_entry_due"] = ledger_audit[0]["pending_entry_due"]
    run_summary["forward_sample_open_entered"] = ledger_audit[0]["open_entered"]
    run_summary["forward_sample_exit_review_due"] = ledger_audit[0]["exit_review_due"]
    run_summary["forward_sample_unplanned_entered"] = ledger_audit[0]["unplanned_entered"]
    run_summary["forward_sample_price_drift_overrides"] = ledger_audit[0]["price_drift_overrides"]
    run_summary["forward_sample_conflicting_outcomes"] = ledger_audit[0]["conflicting_outcomes"]
    run_summary.update(next_action_summary(run_summary, tracker, ledger_audit[0]))
    return {
        "run_summary": run_summary,
        "top_candidate": top_candidate,
        "robustness_checks": checks,
        "live_decision_packet": live_packet,
        "forward_sample_tracker": tracker,
        "forward_sample_ledger_audit": ledger_audit,
        "pre_entry_gate": pre_entry_gate,
        "production_readiness_debt": readiness_debt,
    }


def carrier_count_summary(cfg: dict[str, Any], carriers: pd.DataFrame, carrier_replay: pd.DataFrame, manual_review: pd.DataFrame) -> dict[str, int]:
    min_events = int(cfg["tradable_carrier"].get("min_replay_events", 30))
    if carrier_replay.empty:
        replayed_carriers = 0
        qualified_carriers = 0
    else:
        counts = carrier_replay.groupby("carrier_code")["signal_date"].count()
        replayed_carriers = int(len(counts))
        qualified_carriers = int((counts >= min_events).sum())
    return {
        "tradable_carrier_spot_candidates": int(len(carriers)),
        "tradable_carrier_manual_review_rows": int(len(manual_review)),
        "tradable_carrier_replayed_count": replayed_carriers,
        "tradable_carrier_qualified_replay_count": qualified_carriers,
        "tradable_carrier_insufficient_history_count": int(manual_review["history_replay_status"].eq("insufficient_history").sum()) if "history_replay_status" in manual_review.columns else 0,
        "tradable_carrier_priority_review_count": int(manual_review["auto_review_priority"].eq("优先复核").sum()) if "auto_review_priority" in manual_review.columns else 0,
    }


def year_state_sparsity(year_state: pd.DataFrame) -> dict[str, int]:
    if year_state.empty or "events" not in year_state.columns:
        return {"year_state_cell_count": 0, "year_state_sparse_lt3_count": 0, "year_state_sparse_lt5_count": 0, "year_state_min_events": 0}
    events = pd.to_numeric(year_state["events"], errors="coerce").fillna(0)
    return {
        "year_state_cell_count": int(len(year_state)),
        "year_state_sparse_lt3_count": int((events < 3).sum()),
        "year_state_sparse_lt5_count": int((events < 5).sum()),
        "year_state_min_events": int(events.min()),
    }


def parameter_failed_variants(parameter: pd.DataFrame) -> str:
    if parameter.empty:
        return ""
    failed = parameter[~pd.Series(parameter["effective"]).astype(bool)].copy()
    return "；".join(f"{row.variant_id}={row.failed_score_metrics or row.failed_metrics}" for row in failed.itertuples())


def parameter_failure_actions(parameter: pd.DataFrame) -> str:
    text = parameter_failed_variants(parameter)
    actions = []
    if "entry_lag_1" in text:
        actions.append("禁止把入场延迟从2日提前到1日")
    if "stop_loss_7_2pct" in text or "no_stop_overlay" in text:
        actions.append("禁止放宽或取消保护止损")
    if "vol_threshold_1_56" in text:
        actions.append("高波动阈值上调会暴露路径回撤")
    if "all_events_stop" in text:
        actions.append("不能对所有事件一刀切止损")
    return "；".join(actions)


def cooldown_failing_gaps(cooldown: pd.DataFrame) -> str:
    if cooldown.empty:
        return ""
    failed = cooldown[pd.to_numeric(cooldown["clusters"], errors="coerce").fillna(0) < 20]
    return "；".join(f"{int(row.cooldown_days)}d={int(row.clusters)}簇" for row in failed.itertuples())


def year_state_sparse_buckets(year_state: pd.DataFrame) -> str:
    if year_state.empty:
        return ""
    frame = year_state[pd.to_numeric(year_state["events"], errors="coerce").fillna(0) < 3]
    return "；".join(
        f"{int(row.year)}/{row.dimension}/{row.bucket}={int(row.events)}"
        for row in frame.head(8).itertuples()
    )


def build_production_readiness_debt(run_summary: dict[str, Any], parameter: pd.DataFrame, cooldown: pd.DataFrame) -> list[dict[str, Any]]:
    effective_count = int(pd.Series(parameter["effective"]).astype(bool).sum())
    variant_count = int(len(parameter))
    required_effective = math.ceil(0.60 * variant_count)
    cooldown_60 = int(cooldown[cooldown["cooldown_days"] == 60].iloc[0]["clusters"])
    return [
        {
            "blocker": "parameter_perturbation_pass_rate",
            "current": f"{effective_count}/{variant_count}",
            "required": f"{required_effective}/{variant_count}",
            "gap": max(required_effective - effective_count, 0),
            "unblock_condition": "至少 60% 参数扰动版本通过统一评价，且不得靠放松风险门槛实现。",
            "current_action": "保持冻结规则观察，不升级为生产规则。",
            "live_decision_rule": "信号触发时只能进入观察清单；入场节奏、止损和波动阈值不得临场改参。",
            "next_evidence_to_collect": "继续记录未来触发样本，并检查失败扰动是否仍集中在入场过早、止损过松或路径回撤失控。",
            "unsafe_shortcut": "不能把 score 接近 100 但未通过硬门槛的扰动当作生产通过。",
        },
        {
            "blocker": "cooldown_60_clusters",
            "current": cooldown_60,
            "required": 20,
            "gap": max(20 - cooldown_60, 0),
            "unblock_condition": "60 日冷却口径下至少 20 个独立行情簇。",
            "current_action": "继续新增样本前推；不要把高度重叠事件当作独立证据。",
            "live_decision_rule": "同一轮行情内重复触发只算一次观察，不因为连续触发而加仓或提高信心。",
            "next_evidence_to_collect": "前推账本新增独立行情簇；每次触发后按 planned/entered/skipped/settled 留痕。",
            "unsafe_shortcut": "不能把 30 日冷却通过替代 60 日独立样本门槛。",
        },
        {
            "blocker": "year_state_sparse_cells",
            "current": run_summary.get("year_state_sparse_lt3_count", 0),
            "required": 0,
            "gap": int(run_summary.get("year_state_sparse_lt3_count", 0)),
            "unblock_condition": "分年 x 分状态所有格子至少有 3 个事件，或降级为仅观察描述。",
            "current_action": "分状态结论只能作为解释，不得作为生产门槛依据。",
            "live_decision_rule": "不能因为某个状态桶均值为正就提高实盘信心。",
            "next_evidence_to_collect": "继续前推新增年份和状态桶样本，记录稀疏格子是否改善。",
            "unsafe_shortcut": "不能用跨年份合并均值掩盖单年单状态样本过少。",
        },
        {
            "blocker": "production_ready",
            "current": run_summary["production_ready"],
            "required": True,
            "gap": int(not run_summary["production_ready"]),
            "unblock_condition": "所有 robustness_checks 均为 pass。",
            "current_action": "只能作为 watchlist_only_research_signal。",
            "live_decision_rule": "自动执行=否；任何实际交易都必须脱离系统自动建议，由人工独立决定并记录原因。",
            "next_evidence_to_collect": "补齐参数扰动、独立样本、载体执行回放和前推账本证据。",
            "unsafe_shortcut": "不能因为 V4.70 单版本评分 100 就绕过 V4.71 生产审计。",
        },
    ]


def build_pre_entry_gate(run_summary: dict[str, Any], latest_row: dict[str, Any], checks: list[dict[str, str]], ledger_audit: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = [
        {"gate": "latest_signal_triggered", "status": "pass" if run_summary["latest_signal_triggered"] else "fail", "evidence": f"signal_date={run_summary['latest_panel_date']}"},
        {"gate": "planned_entry_known", "status": "pass" if run_summary["planned_entry_date"] not in {"未触发", "交易日历不足"} else "fail", "evidence": f"entry={run_summary['planned_entry_date']}; exit={run_summary['planned_exit_date']}"},
        {"gate": "decision_state_allows_auto_trade", "status": "fail" if not run_summary["production_ready"] else "pass", "evidence": f"production_ready={run_summary['production_ready']}"},
    ]
    for check in checks:
        rows.append({"gate": str(check["check"]), "status": str(check["status"]), "evidence": str(check["evidence"])})
    ledger = ledger_audit[0]
    rows.append({
        "gate": "forward_sample_ledger_clean",
        "status": "fail" if int(ledger["unplanned_entered"]) or int(ledger["price_drift_overrides"]) or int(ledger["conflicting_outcomes"]) or int(ledger["pending_entry_due"]) or int(ledger["exit_review_due"]) else "pass",
        "evidence": f"status={ledger['status']}; skipped={ledger['skipped']}; pending_planned={ledger['pending_planned']}; pending_entry_due={ledger['pending_entry_due']}; open_entered={ledger['open_entered']}; exit_review_due={ledger['exit_review_due']}; unplanned_entered={ledger['unplanned_entered']}; price_drift_overrides={ledger['price_drift_overrides']}; conflicting_outcomes={ledger['conflicting_outcomes']}",
    })
    rows.append({
        "gate": "pre_entry_final_state",
        "status": "watchlist_only" if not run_summary["production_ready"] else "manual_review_required",
        "evidence": f"decision_state=watchlist_only_research_signal; data_freshness={latest_row['freshness_status']}",
    })
    return rows


def next_action_summary(run_summary: dict[str, Any], tracker: dict[str, Any], ledger: dict[str, Any]) -> dict[str, str]:
    if not run_summary["latest_signal_triggered"]:
        return {"next_action_date": run_summary["as_of_date"], "next_action": "无当前触发；继续每日刷新观察。"}
    if int(ledger["pending_entry_due"]):
        return {"next_action_date": run_summary["as_of_date"], "next_action": "计划入场日已到；逐条把 planned 记录为 entered 或 skipped。"}
    if int(ledger["exit_review_due"]):
        return {"next_action_date": run_summary["as_of_date"], "next_action": "计划退出日已到；补写 entered 记录的真实 exit_price。"}
    if tracker["stage"] == "pending_entry":
        return {"next_action_date": run_summary["planned_entry_date"], "next_action": "入场日前重跑 live refresh；只做人工复核，不自动下单。"}
    if tracker["stage"] == "active_holding_window":
        return {"next_action_date": run_summary["planned_exit_date"], "next_action": "处于前推持有观察窗口；退出日后补写真实收益。"}
    return {"next_action_date": run_summary["as_of_date"], "next_action": "复核前推账本和最新信号状态。"}


def validate_position_sizing(cfg: dict[str, Any]) -> dict[str, str]:
    account = position_cfg(cfg, "reference_account_cny")
    single = position_cfg(cfg, "single_carrier_cap_pct")
    total = position_cfg(cfg, "total_strategy_cap_pct")
    stop = position_cfg(cfg, "risk_stop_reference_pct")
    lot = position_cfg(cfg, "etf_lot_size")
    max_order_bps = position_cfg(cfg, "max_order_turnover_bps")
    max_entry_drift = position_cfg(cfg, "max_entry_price_drift_pct")
    problems = []
    if account <= 0:
        problems.append("reference_account_cny<=0")
    if not 0 < single <= 1:
        problems.append("single_carrier_cap_pct not in (0,1]")
    if not 0 < total <= 1:
        problems.append("total_strategy_cap_pct not in (0,1]")
    if total < single:
        problems.append("total_strategy_cap_pct<single_carrier_cap_pct")
    if not 0 < stop < 1:
        problems.append("risk_stop_reference_pct not in (0,1)")
    if lot <= 0 or int(lot) != lot:
        problems.append("etf_lot_size must be positive integer")
    if max_order_bps <= 0:
        problems.append("max_order_turnover_bps<=0")
    if not 0 <= max_entry_drift < 1:
        problems.append("max_entry_price_drift_pct not in [0,1)")
    evidence = f"account={account:g}; single={single:.2%}; total={total:.2%}; stop={stop:.2%}; lot={lot:g}; max_order_bps={max_order_bps:g}; max_entry_drift={max_entry_drift:.2%}"
    if problems:
        evidence += "; " + ",".join(problems)
    return {"check": "position_sizing_config", "status": "fail" if problems else "pass", "evidence": evidence}


def build_live_decision_packet(cfg: dict[str, Any], run_summary: dict[str, Any], latest_row: dict[str, Any], carrier_replay: pd.DataFrame) -> dict[str, Any]:
    triggered = bool(run_summary["latest_signal_triggered"])
    production_ready = bool(run_summary["production_ready"])
    if not triggered:
        state = "no_live_signal"
    elif production_ready:
        state = "production_candidate_manual_review"
    else:
        state = "watchlist_only_research_signal"
    return {
        "decision_state": state,
        "signal_date": run_summary["latest_panel_date"],
        "planned_entry_date": run_summary["planned_entry_date"] if triggered else "未触发",
        "planned_exit_date": run_summary["planned_exit_date"] if triggered else "未触发",
        "planned_holding_days": int(latest_row.get("planned_holding_days_if_triggered", 0) or 0),
        "blocking_issues": run_summary["blocking_issues"],
        "carrier_review_scope": "broad_market_reference_only_not_industry_execution",
        "industry_carrier_review_packet": "outputs/audit/v4_72_pre_trade_review_packet/top_candidates.csv",
        "industry_entry_readiness_packet": "outputs/audit/v4_72_entry_readiness/top_candidates.csv",
        "carrier_review_candidates": carrier_review_candidates(cfg, carrier_replay),
        "position_sizing": {
            "reference_account_cny": position_cfg(cfg, "reference_account_cny"),
            "single_carrier_cap_pct": position_cfg(cfg, "single_carrier_cap_pct"),
            "total_strategy_cap_pct": position_cfg(cfg, "total_strategy_cap_pct"),
            "risk_stop_reference_pct": position_cfg(cfg, "risk_stop_reference_pct"),
            "etf_lot_size": int(position_cfg(cfg, "etf_lot_size")),
            "max_order_turnover_bps": position_cfg(cfg, "max_order_turnover_bps"),
            "max_entry_price_drift_pct": position_cfg(cfg, "max_entry_price_drift_pct"),
        },
        "allowed_use": [
            "作为研究观察和人工复核清单",
            "在计划入场日前重新刷新行业面板、载体行情和阻断项",
            "宽基/全市场载体只作为市场代理观察，不替代 V4.72 行业载体复核包",
            "只在人工确认仓位、风险和流动性后作为参考"
        ],
        "manual_risk_controls": [
            "默认自动执行=否",
            f"优先复核载体仅提供人工仓位上限模板：单载体 {position_cfg(cfg, 'single_carrier_cap_pct'):.0%}，策略合计 {position_cfg(cfg, 'total_strategy_cap_pct'):.0%}",
            f"参考止损风险口径为 {position_cfg(cfg, 'risk_stop_reference_pct'):.0%}；实际仓位用 min(账户资金*单载体上限, 单笔风险预算/{position_cfg(cfg, 'risk_stop_reference_pct'):.2f})",
            f"参考单占当日成交额超过 {position_cfg(cfg, 'max_order_turnover_bps'):g}bp 时，需要人工降仓或跳过",
            f"入场价较参考价上浮超过 {position_cfg(cfg, 'max_entry_price_drift_pct'):.0%} 时，需要人工跳过或降级观察",
            "production_ready=false 时，即使触发信号也只能记录观察或人工跳过"
        ],
        "prohibited_use": [
            "自动下单",
            "把候选载体视为买入建议",
            "用宽基/全市场 ETF 替代行业载体入场",
            "在 production_ready=false 时按生产系统执行"
        ],
    }


def position_cfg(cfg: dict[str, Any], key: str) -> float:
    defaults = {
        "reference_account_cny": 100_000,
        "single_carrier_cap_pct": 0.08,
        "total_strategy_cap_pct": 0.20,
        "risk_stop_reference_pct": 0.06,
        "etf_lot_size": 100,
        "max_order_turnover_bps": 1.0,
        "max_entry_price_drift_pct": 0.02,
    }
    return float(cfg.get("position_sizing", {}).get(key, defaults[key]))


def carrier_review_candidates(cfg: dict[str, Any], carrier_replay: pd.DataFrame) -> list[dict[str, Any]]:
    if carrier_replay.empty:
        return []
    min_events = int(cfg["tradable_carrier"].get("min_replay_events", 30))
    summary = carrier_replay.groupby(["carrier_code", "carrier_name"]).agg(
        events=("signal_date", "count"),
        net_mean_return=("carrier_net_return", "mean"),
        win_rate=("carrier_net_return", lambda x: float((x > 0).mean())),
        tracking_gap=("tracking_gap_vs_market_window", "mean"),
    ).reset_index()
    summary = summary[summary["events"] >= min_events].sort_values(["tracking_gap", "events"], ascending=[False, False])
    return summary.to_dict("records")


def build_forward_sample_tracker(packet: dict[str, Any], run_summary: dict[str, Any], as_of_date: str | None = None) -> dict[str, Any]:
    today = pd.to_datetime(as_of_date).normalize() if as_of_date else pd.Timestamp.today().normalize()
    triggered = bool(run_summary["latest_signal_triggered"])
    if not triggered:
        stage = "no_signal"
    else:
        entry = pd.to_datetime(packet["planned_entry_date"])
        exit_ = pd.to_datetime(packet["planned_exit_date"])
        if today < entry:
            stage = "pending_entry"
        elif today <= exit_:
            stage = "active_holding_window"
        else:
            stage = "ready_for_realized_return_review"
    return {
        "tracker_id": f"v4_71_forward_{packet['signal_date']}",
        "stage": stage,
        "as_of_date": today.strftime("%Y-%m-%d"),
        "signal_date": packet["signal_date"],
        "planned_entry_date": packet["planned_entry_date"],
        "planned_exit_date": packet["planned_exit_date"],
        "production_ready": bool(run_summary["production_ready"]),
        "decision_state": packet["decision_state"],
        "checklist": forward_checklist(stage, packet),
    }


def forward_checklist(stage: str, packet: dict[str, Any]) -> list[dict[str, str]]:
    rows = [
        {"step": "refresh_before_entry", "due_date": packet["planned_entry_date"], "status": "pending" if stage == "pending_entry" else "done_or_due", "action": "入场日前重跑 V4.71，确认 latest_data_freshness、载体回放和阻断项。"},
        {"step": "entry_day_manual_review", "due_date": packet["planned_entry_date"], "status": "pending" if stage in {"pending_entry", "active_holding_window"} else "done_or_due", "action": "只做人工复核，不自动下单；production_ready=false 时保持观察清单。"},
        {"step": "exit_day_realized_review", "due_date": packet["planned_exit_date"], "status": "pending" if stage != "ready_for_realized_return_review" else "due", "action": "退出日后用真实载体价格补写前推样本收益。"},
    ]
    return rows


def audit_forward_sample_ledger(as_of_date: str | None = None) -> list[dict[str, Any]]:
    as_of = pd.to_datetime(as_of_date).normalize() if as_of_date else pd.Timestamp.today().normalize()
    if not FORWARD_SAMPLE_LEDGER.exists():
        return [{"status": "empty", "rows": 0, "planned": 0, "entered": 0, "skipped": 0, "pending_planned": 0, "pending_entry_due": 0, "open_entered": 0, "exit_review_due": 0, "unplanned_entered": 0, "price_drift_overrides": 0, "conflicting_outcomes": 0, "ledger_path": str(FORWARD_SAMPLE_LEDGER)}]
    rows = pd.read_csv(FORWARD_SAMPLE_LEDGER, encoding="utf-8-sig").fillna("")
    planned = set(tuple(x) for x in rows.loc[rows["decision"].eq("planned"), ["tracker_id", "carrier_code"]].to_numpy())
    entered = set(tuple(x) for x in rows.loc[rows["decision"].eq("entered"), ["tracker_id", "carrier_code"]].to_numpy())
    skipped = set(tuple(x) for x in rows.loc[rows["decision"].eq("skipped"), ["tracker_id", "carrier_code"]].to_numpy())
    override = rows["unplanned_override"].astype(str).eq("True") if "unplanned_override" in rows.columns else pd.Series(False, index=rows.index)
    unplanned = rows["decision"].eq("entered") & (override | ~rows[["tracker_id", "carrier_code"]].apply(tuple, axis=1).isin(planned))
    conflicts = entered & skipped
    pending = planned - entered - skipped
    planned_dates = pd.to_datetime(rows.get("planned_entry_date"), errors="coerce")
    pending_due = rows["decision"].eq("planned") & rows[["tracker_id", "carrier_code"]].apply(tuple, axis=1).isin(pending) & planned_dates.le(as_of)
    open_entered = rows["decision"].eq("entered") & rows.get("exit_price", "").astype(str).eq("")
    exit_dates = pd.to_datetime(rows.get("planned_exit_date"), errors="coerce")
    exit_due = open_entered & exit_dates.le(as_of)
    price_drift_overrides = rows.get("price_drift_override", pd.Series("", index=rows.index)).astype(str).eq("True")
    return [{
        "status": "pass" if int(unplanned.sum()) == 0 and len(conflicts) == 0 and int(pending_due.sum()) == 0 and int(exit_due.sum()) == 0 and int(price_drift_overrides.sum()) == 0 else "review",
        "rows": int(len(rows)),
        "planned": int(len(planned)),
        "entered": int(len(entered)),
        "skipped": int(len(skipped)),
        "pending_planned": int(len(pending)),
        "pending_entry_due": int(pending_due.sum()),
        "open_entered": int(open_entered.sum()),
        "exit_review_due": int(exit_due.sum()),
        "unplanned_entered": int(unplanned.sum()),
        "price_drift_overrides": int(price_drift_overrides.sum()),
        "conflicting_outcomes": int(len(conflicts)),
        "ledger_path": str(FORWARD_SAMPLE_LEDGER),
    }]


def execution_check(carrier_audit: pd.DataFrame, carrier_replay: pd.DataFrame) -> dict[str, str]:
    rows = carrier_audit[carrier_audit["item"] == "carrier_execution_replay"]
    if rows.empty:
        return {"check": "execution_backtest_ready", "status": "fail", "evidence": "真实载体执行回放未生成"}
    row = rows.iloc[-1]
    return {"check": "execution_backtest_ready", "status": str(row["status"]), "evidence": str(row["evidence"]) + f"; rows={len(carrier_replay)}"}


def build_final_verdict(production_ready: bool, blocking: list[dict[str, str]]) -> str:
    if production_ready:
        return "research_only；V4.71 实盘前审计全部通过，但仍需人工确认后才能作为生产候选监控。"
    labels = {
        "parameter_perturbation_pass_rate": "参数扰动稳定性不足",
        "cooldown_60_clusters": "60日冷却期独立样本不足",
        "year_state_sparse_cells": "分年分状态样本稀疏",
        "latest_data_freshness": "最新行业数据不够新",
        "tradable_carrier_current_data": "当前可交易载体数据不足",
        "execution_backtest_ready": "真实载体执行回放覆盖不足",
        "base_v4_70_effective": "V4.70 原规则未通过统一评价",
        "position_sizing_config": "人工仓位配置无效",
    }
    reasons = "、".join(labels.get(item["check"], item["check"]) for item in blocking)
    return f"research_only；V4.71 已完成稳健性和实盘辅助复核，但仍未生产就绪。当前阻断项：{reasons}。"


def manual_carrier_review_sheet(cfg: dict[str, Any], carriers: pd.DataFrame, carrier_replay: pd.DataFrame) -> pd.DataFrame:
    if carriers.empty:
        return pd.DataFrame()
    replay = pd.DataFrame()
    if not carrier_replay.empty:
        replay = carrier_replay.groupby(["carrier_code", "carrier_name"]).agg(
            replay_events=("signal_date", "count"),
            replay_net_mean_return=("carrier_net_return", "mean"),
            replay_win_rate=("carrier_net_return", lambda x: float((x > 0).mean())),
            replay_tracking_gap=("tracking_gap_vs_market_window", "mean"),
        ).reset_index()
    out = carriers.copy()
    out = out.rename(columns={"代码": "carrier_code", "名称": "carrier_name", "基金折价率": "discount_rate", "成交额": "turnover_amount", "流通市值": "free_float_market_value", "数据日期": "data_date"})
    if not replay.empty:
        out = out.merge(replay, on=["carrier_code", "carrier_name"], how="left")
    min_events = int(cfg["tradable_carrier"].get("min_replay_events", 30))
    replay_events = pd.to_numeric(out["replay_events"], errors="coerce").fillna(0) if "replay_events" in out.columns else pd.Series([0] * len(out), index=out.index)
    discount_abs = pd.to_numeric(out["discount_rate"], errors="coerce").abs() if "discount_rate" in out.columns else pd.Series([math.nan] * len(out), index=out.index)
    out["history_replay_status"] = replay_events.ge(min_events).map({True: "pass", False: "insufficient_history"})
    out["liquidity_status"] = out["mapping_confidence"].eq("high").map({True: "pass", False: "review"})
    out["discount_status"] = discount_abs.le(0.80).map({True: "pass", False: "review"})
    out["auto_review_priority"] = [
        auto_carrier_priority(history, liquidity, discount)
        for history, liquidity, discount in zip(out["history_replay_status"], out["liquidity_status"], out["discount_status"])
    ]
    out["manual_liquidity_check"] = "人工复核"
    out["manual_tracking_error_check"] = "人工复核"
    out["auto_execution_allowed"] = "否"
    single_cap = position_cfg(cfg, "single_carrier_cap_pct")
    total_cap = position_cfg(cfg, "total_strategy_cap_pct")
    stop_pct = position_cfg(cfg, "risk_stop_reference_pct")
    account_cny = position_cfg(cfg, "reference_account_cny")
    lot_size = int(position_cfg(cfg, "etf_lot_size"))
    max_order_bps = position_cfg(cfg, "max_order_turnover_bps")
    max_entry_drift = position_cfg(cfg, "max_entry_price_drift_pct")
    out["default_single_carrier_cap_pct"] = [single_cap if priority == "优先复核" else 0.0 for priority in out["auto_review_priority"]]
    out["default_total_strategy_cap_pct"] = [total_cap if priority == "优先复核" else 0.0 for priority in out["auto_review_priority"]]
    out["risk_stop_reference_pct"] = stop_pct
    out["reference_account_cny"] = int(account_cny) if account_cny.is_integer() else account_cny
    latest_price = pd.to_numeric(out.get("最新价"), errors="coerce")
    cap = pd.to_numeric(out["default_single_carrier_cap_pct"], errors="coerce").fillna(0.0)
    raw_shares = (out["reference_account_cny"] * cap / latest_price).where(latest_price > 0, 0)
    out["reference_shares"] = (raw_shares.fillna(0) // lot_size * lot_size).astype(int)
    out["reference_notional_cny"] = (out["reference_shares"] * latest_price).round(2)
    out["reference_stop_loss_cny"] = (out["reference_notional_cny"] * out["risk_stop_reference_pct"]).round(2)
    turnover = pd.to_numeric(out.get("turnover_amount"), errors="coerce")
    out["reference_order_turnover_bps"] = (out["reference_notional_cny"] / turnover * 10000).where(turnover > 0, math.nan).round(4)
    out["reference_capacity_status"] = out["reference_order_turnover_bps"].le(max_order_bps).map({True: "pass", False: "review"})
    out["reference_entry_price"] = latest_price
    out["max_reference_entry_price"] = (latest_price * (1 + max_entry_drift)).round(3)
    out["entry_price_drift_rule"] = f"入场价>{max_entry_drift:.0%}参考上限则跳过/降级观察"
    out["manual_position_limit"] = f"人工填写；公式=min(账户资金*单载体上限, 单笔风险预算/{stop_pct:.2f})"
    out["manual_decision"] = "观察/跳过/待复核"
    out["pre_register_command"] = out.apply(lambda row: pre_register_command(row["carrier_code"], row["carrier_name"]), axis=1)
    out["entry_record_command"] = out.apply(lambda row: entry_record_command(row["carrier_code"], row["carrier_name"]), axis=1)
    out["exit_settle_command"] = out.apply(lambda row: exit_settle_command(row["carrier_code"], row["carrier_name"]), axis=1)
    out["skip_command"] = out.apply(lambda row: skip_command(row["carrier_code"], row["carrier_name"]), axis=1)
    out["notes"] = ""
    keep = ["carrier_code", "carrier_name", "carrier_role", "latest_price", "discount_rate", "turnover_amount", "free_float_market_value", "data_date", "mapping_confidence", "liquidity_status", "discount_status", "history_replay_status", "auto_review_priority", "replay_events", "replay_net_mean_return", "replay_win_rate", "replay_tracking_gap", "auto_execution_allowed", "default_single_carrier_cap_pct", "default_total_strategy_cap_pct", "risk_stop_reference_pct", "reference_account_cny", "reference_shares", "reference_notional_cny", "reference_stop_loss_cny", "reference_order_turnover_bps", "reference_capacity_status", "reference_entry_price", "max_reference_entry_price", "entry_price_drift_rule", "manual_liquidity_check", "manual_tracking_error_check", "manual_position_limit", "manual_decision", "pre_register_command", "entry_record_command", "exit_settle_command", "skip_command", "notes"]
    out = out.rename(columns={"最新价": "latest_price"})
    return out[[c for c in keep if c in out.columns]].head(20)


def pre_register_command(code: str, name: str) -> str:
    return f'python .\\scripts\\append_v4_71_forward_sample.py --decision planned --carrier-code {str(code).zfill(6)} --carrier-name "{name}" --notes "入场前预登记"'


def entry_record_command(code: str, name: str) -> str:
    return f'python .\\scripts\\append_v4_71_forward_sample.py --decision entered --carrier-code {str(code).zfill(6)} --carrier-name "{name}" --entry-price 0.000 --notes "入场日记录"'


def exit_settle_command(code: str, name: str) -> str:
    return f'python .\\scripts\\append_v4_71_forward_sample.py --decision entered --carrier-code {str(code).zfill(6)} --carrier-name "{name}" --exit-price 0.000 --notes "退出日补写真实收益" --replace'


def skip_command(code: str, name: str) -> str:
    return f'python .\\scripts\\append_v4_71_forward_sample.py --decision skipped --carrier-code {str(code).zfill(6)} --carrier-name "{name}" --notes "入场日人工跳过"'


def auto_carrier_priority(history: str, liquidity: str, discount: str) -> str:
    if history == "pass" and liquidity == "pass" and discount == "pass":
        return "优先复核"
    if history != "pass":
        return "谨慎：历史不足"
    if discount != "pass":
        return "谨慎：折溢价"
    if liquidity != "pass":
        return "谨慎：流动性"
    return "谨慎复核"


def render_pre_entry_manual_review(summary: dict[str, Any], latest: pd.DataFrame, manual_review: pd.DataFrame) -> str:
    rs = summary["run_summary"]
    packet = summary["live_decision_packet"]
    sizing = packet["position_sizing"]
    gate = pd.DataFrame(summary["pre_entry_gate"])
    rows = [
        "# V4.71 入场前人工复核单",
        "",
        f"- 信号日：{packet['signal_date']}",
        f"- 计划入场日：{packet['planned_entry_date']}",
        f"- 计划退出日：{packet['planned_exit_date']}",
        f"- 决策状态：`{packet['decision_state']}`",
        f"- 生产就绪：`{rs['production_ready']}`",
        f"- 载体口径：现货候选 {rs['tradable_carrier_spot_candidates']} 个；人工复核表展示 {rs['tradable_carrier_manual_review_rows']} 个；历史回放合格 {rs['tradable_carrier_qualified_replay_count']} 个；历史不足 {rs['tradable_carrier_insufficient_history_count']} 个。",
        "",
        "## 当前处理结论",
        "",
        "不自动入场。当前只能作为观察清单和人工复核材料，因为仍存在参数扰动稳定性不足、60 日冷却期独立样本不足两个阻断项。",
        "",
        "## 入场日前必须重跑",
        "",
        f"在 {packet['planned_entry_date']} 开盘前重新运行：",
        "",
        "```powershell",
        f"python .\\scripts\\run_v4_71_live_refresh.py --trade-date {packet['planned_entry_date']}",
        "```",
        "",
        "只有当最新数据、载体行情、执行回放都通过时，才进入人工判断；只要 `production_ready=false`，仍不能当作自动交易信号。",
        "",
        "## 最新触发条件",
        "",
        table(latest, pct_cols=set()),
        "",
        "## Gate",
        "",
        table(gate, pct_cols=set()),
        "",
        "## 载体人工复核优先级",
        "",
        manual_review_priority_table(manual_review),
        "",
        "## 优先复核载体预登记命令",
        "",
        pre_register_command_block(manual_review),
        "",
        "## 人工仓位风险模板",
        "",
        "这不是建议仓位，只是入场日前人工复核用的上限模板：",
        "",
        "- 自动执行：否。",
        f"- 优先复核载体默认单载体上限：{sizing['single_carrier_cap_pct']:.0%}。",
        f"- 策略合计上限：{sizing['total_strategy_cap_pct']:.0%}。",
        f"- 参考止损风险口径：{sizing['risk_stop_reference_pct']:.0%}。",
        f"- `manual_carrier_review_sheet.csv` 按 {sizing['reference_account_cny']:.0f} 元参考账户和 ETF {sizing['etf_lot_size']} 份一手，给出参考份额、参考名义金额和参考止损亏损额。",
        f"- 参考单占当日成交额超过 {sizing['max_order_turnover_bps']:g}bp 时，容量状态会标记为 `review`。",
        f"- 入场日价格较参考价上浮超过 {sizing['max_entry_price_drift_pct']:.0%} 时，跳过或降级观察，不追价。",
        f"- 实际金额公式：`min(账户资金*单载体上限, 单笔风险预算/{sizing['risk_stop_reference_pct']:.2f})`。",
        "- 若 `production_ready=false`，即使信号触发也只能观察或跳过，不能按生产规则自动执行。",
        "",
        "## 优先复核载体入场记录命令",
        "",
        "把 `0.000` 改为实际入场价后再执行；若实际入场价高于 `manual_carrier_review_sheet.csv` 的 `max_reference_entry_price`，账本脚本会拒绝记录。显式加 `--allow-price-drift` 可以人工覆盖，但必须同时用 `--notes` 说明原因。",
        "",
        command_block(manual_review, "entry_record_command", "当前没有可入场记录命令。"),
        "",
        "## 优先复核载体退出结算命令",
        "",
        "退出日把 `0.000` 改为实际退出价后再执行；脚本会读取已有入场价。",
        "",
        command_block(manual_review, "exit_settle_command", "当前没有可退出结算命令。"),
        "",
        "## 优先复核载体跳过命令",
        "",
        skip_command_block(manual_review),
        "",
        "## 人工复核动作",
        "",
        "- 确认计划入场日当天信号仍成立。",
        "- 剔除历史回放不足、成交额突然萎缩、折溢价异常、跟踪误差不可解释的载体。",
        "- 若决定跟踪某个载体，先用 `python .\\scripts\\append_v4_71_forward_sample.py --decision planned --carrier-code <代码> --carrier-name <名称>` 预登记；退出后追加 `entered` 默认要求已有预登记。",
        "- 手工填写 `debug/manual_carrier_review_sheet.csv` 的仓位上限、跟踪误差检查和最终决定。",
        "- 退出日后把真实成交载体收益补回前推样本，不用事后最佳载体替代真实选择。",
    ]
    return "\n".join(rows)


def manual_review_priority_table(manual_review: pd.DataFrame) -> str:
    if manual_review.empty:
        return "当前没有可人工复核载体。"
    frame = manual_review.copy()
    frame["_history_ok"] = frame["history_replay_status"].eq("pass")
    for col in ["turnover_amount", "replay_events", "replay_net_mean_return", "replay_win_rate", "replay_tracking_gap"]:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.sort_values(["_history_ok", "replay_tracking_gap", "turnover_amount"], ascending=[False, False, False]).head(8)
    keep = ["carrier_code", "carrier_name", "carrier_role", "auto_review_priority", "liquidity_status", "discount_status", "history_replay_status", "turnover_amount", "replay_events", "replay_net_mean_return", "replay_win_rate", "replay_tracking_gap", "manual_decision"]
    return table(frame[[c for c in keep if c in frame.columns]], pct_cols={"replay_net_mean_return", "replay_win_rate", "replay_tracking_gap"})


def pre_register_command_block(manual_review: pd.DataFrame) -> str:
    if manual_review.empty or "pre_register_command" not in manual_review.columns:
        return "当前没有可预登记命令。"
    frame = manual_review[manual_review["auto_review_priority"].eq("优先复核")].head(4)
    if frame.empty:
        return "当前没有优先复核载体；如需跟踪谨慎项，请从 `debug/manual_carrier_review_sheet.csv` 手工复制命令。"
    return "```powershell\n" + "\n".join(frame["pre_register_command"].astype(str).tolist()) + "\n```"


def command_block(manual_review: pd.DataFrame, column: str, empty: str) -> str:
    if manual_review.empty or column not in manual_review.columns:
        return empty
    frame = manual_review[manual_review["auto_review_priority"].eq("优先复核")].head(4)
    if frame.empty:
        return "当前没有优先复核载体。"
    return "```powershell\n" + "\n".join(frame[column].astype(str).tolist()) + "\n```"


def skip_command_block(manual_review: pd.DataFrame) -> str:
    if manual_review.empty or "skip_command" not in manual_review.columns:
        return "当前没有可跳过命令。"
    frame = manual_review[manual_review["auto_review_priority"].eq("优先复核")].head(4)
    if frame.empty:
        return "当前没有优先复核载体。"
    return "```powershell\n" + "\n".join(frame["skip_command"].astype(str).tolist()) + "\n```"


def render_report(summary: dict[str, Any], parameter: pd.DataFrame, parameter_diagnosis: pd.DataFrame, cooldown: pd.DataFrame, annual: pd.DataFrame, state: pd.DataFrame, year_state: pd.DataFrame, latest: pd.DataFrame, carriers: pd.DataFrame, carrier_replay: pd.DataFrame, carrier_audit: pd.DataFrame) -> str:
    rs = summary["run_summary"]
    lines = [
        "# V4.71 稳健性与实盘辅助复核",
        "",
        "## 结论",
        "",
        f"- V4.70 原规则仍然有效：`{rs['base_v4_70_effective']}`，V3.4 分数 {rs['base_v4_70_score']:.1f}。",
        f"- 参数扰动有效率：{rs['parameter_perturbation_effective_rate']:.2%}。",
        f"- 参数扰动失败版本：{rs.get('parameter_failed_variants', '')}",
        f"- 参数失败动作：{rs.get('parameter_failure_actions', '')}",
        f"- 60 天冷却期独立簇：{rs['cooldown_60_clusters']}。",
        f"- 冷却期失败档位：{rs.get('cooldown_failing_gaps', '')}",
        f"- 稀疏分年状态桶样例：{rs.get('year_state_sparse_buckets', '')}",
        f"- 最新行业面板日期：{rs['latest_panel_date']}；最新日是否触发 V4.70：{rs['latest_signal_triggered']}。",
        f"- 可交易载体现货候选数：{rs['tradable_carrier_spot_candidates']}；人工复核表展示 {rs['tradable_carrier_manual_review_rows']} 个；历史回放合格 {rs['tradable_carrier_qualified_replay_count']} 个；历史不足 {rs['tradable_carrier_insufficient_history_count']} 个。",
        f"- 真实载体执行回放行数：{rs['carrier_execution_replay_rows']}。",
        f"- 生产就绪：`{rs['production_ready']}`；阻断项 {rs['blocking_issue_count']} 个。",
        "",
        rs["final_verdict"],
        "",
        "## 阻断项",
        "",
    ]
    for item in rs["blocking_issues"]:
        lines.append(f"- `{item['check']}`：{item['evidence']}")
    lines += ["", "## 参数扰动", "", table(parameter[["variant_id", "description", "events", "clusters", "score", "effective", "net_mean_return", "win_rate", "bad_window_rate", "failed_metrics", "failed_score_metrics"]], pct_cols={"net_mean_return", "win_rate", "bad_window_rate"})]
    lines += ["", "## 参数失败诊断", "", table(parameter_diagnosis, pct_cols=set())]
    lines += ["", "## 冷却期敏感性", "", table(cooldown, pct_cols={"cluster_net_mean_return", "worst_cluster_net_return", "cluster_positive_rate", "max_cluster_concentration"})]
    lines += ["", "## 生产就绪证据债务", "", table(pd.DataFrame(summary["production_readiness_debt"]), pct_cols=set())]
    lines += ["", "## 分年拆解", "", table(annual, pct_cols={"net_mean_return", "win_rate", "bad_window_rate", "worst_return", "path_worst_max_adverse_return"})]
    lines += ["", "## 分状态拆解", "", table(state, pct_cols={"net_mean_return", "win_rate", "bad_window_rate", "worst_return"})]
    lines += ["", "## 分年 x 分状态拆解", "", table(year_state, pct_cols={"net_mean_return", "win_rate", "bad_window_rate", "worst_return"})]
    lines += ["", "## 最新信号状态", "", table(latest, pct_cols=set())]
    lines += ["", "## 实盘辅助决策包", "", live_decision_section(summary["live_decision_packet"])]
    lines += ["", "## 前推样本跟踪", "", forward_tracker_section(summary["forward_sample_tracker"])]
    lines += ["", "## 前推样本账本审计", "", table(pd.DataFrame(summary["forward_sample_ledger_audit"]), pct_cols=set())]
    lines += ["", "## 入场前复核 Gate", "", table(pd.DataFrame(summary["pre_entry_gate"]), pct_cols=set())]
    lines += ["", "## 可交易载体映射", "", "以下只是宽基/全市场 ETF 现货池的人工复核材料，不是交易指令；历史回放不足的载体不能当作已验证执行载体；`基金折价率` 原始单位为百分点。", "", table(carriers.head(20), pct_cols=set()) if not carriers.empty else "当前未取得合格候选。"]
    lines += ["", "人工复核表已写入 `debug/manual_carrier_review_sheet.csv`；入场前检查单已写入 `debug/pre_entry_manual_review.md`。"]
    lines += ["", "## 真实载体执行回放", "", carrier_replay_summary(carrier_replay)]
    lines += ["", "## 载体映射审计", "", table(carrier_audit, pct_cols=set())]
    lines += ["", "## 下一步", "", "- 维持申万行业面板每日刷新，并在每次交易日前复核最新触发状态。", "- 对上表可交易载体候选做真实滑点、跟踪误差、折溢价和成交额回放。", "- 重新审视参数扰动和冷却期样本不足问题；在这些审计通过前，不升级为生产候选监控。", "", rs["research_boundary"]]
    return "\n".join(lines)


def forward_tracker_section(tracker: dict[str, Any]) -> str:
    lines = [
        f"- 跟踪 ID：`{tracker['tracker_id']}`",
        f"- 当前阶段：`{tracker['stage']}`",
        f"- as-of 日期：{tracker['as_of_date']}",
        "",
        table(pd.DataFrame(tracker["checklist"]), pct_cols=set()),
    ]
    return "\n".join(lines)


def live_decision_section(packet: dict[str, Any]) -> str:
    candidates = pd.DataFrame(packet.get("carrier_review_candidates", []))
    lines = [
        f"- 决策状态：`{packet['decision_state']}`",
        f"- 信号日：{packet['signal_date']}",
        f"- 计划入场日：{packet['planned_entry_date']}",
        f"- 计划退出日：{packet['planned_exit_date']}",
        f"- 计划持有天数：{packet['planned_holding_days']}",
        f"- 载体候选口径：`{packet.get('carrier_review_scope', '')}`",
        f"- V4.72 行业载体复核包：`{packet.get('industry_carrier_review_packet', '')}`",
        f"- V4.72 入场就绪包：`{packet.get('industry_entry_readiness_packet', '')}`",
        "- 人工风险控制：" + "；".join(packet.get("manual_risk_controls", [])),
        "- 禁止用途：" + "；".join(packet["prohibited_use"]),
        "",
        "宽基/全市场载体观察候选：",
        "",
        table(candidates, pct_cols={"net_mean_return", "win_rate", "tracking_gap"}) if not candidates.empty else "当前无合格载体候选。",
    ]
    return "\n".join(lines)


def carrier_replay_summary(carrier_replay: pd.DataFrame) -> str:
    if carrier_replay.empty:
        return "当前没有可用载体历史回放。"
    summary = carrier_replay.groupby(["carrier_code", "carrier_name"]).agg(
        events=("signal_date", "count"),
        carrier_net_mean_return=("carrier_net_return", "mean"),
        carrier_win_rate=("carrier_net_return", lambda x: float((x > 0).mean())),
        mean_tracking_gap=("tracking_gap_vs_market_window", "mean"),
    ).reset_index()
    return table(summary, pct_cols={"carrier_net_mean_return", "carrier_win_rate", "mean_tracking_gap"})


def year_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, group in trades.groupby(pd.to_datetime(trades["signal_date"], errors="coerce").dt.year):
        ret = pd.to_numeric(group["trade_return"], errors="coerce")
        rows.append({"year": int(year), "status": "pass", "signal_dates": int(len(group)), "signal_mean_return": float(ret.mean()), "signal_win_rate": float((ret > 0).mean())})
    return pd.DataFrame(rows)


def pass_audit() -> pd.DataFrame:
    return pd.DataFrame([{"status": "pass"}])


def compare(left: float, op: str, right: float) -> bool:
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == "<":
        return left < right
    raise ValueError(f"unsupported op: {op}")


def match_keyword(text: str, keywords: list[str]) -> str:
    for key in keywords:
        if key in text:
            return key
    return ""


def table(df: pd.DataFrame, pct_cols: set[str]) -> str:
    if df.empty:
        return "当前版本无该项数据。"
    out = df.copy()
    for col in pct_cols & set(out.columns):
        out[col] = pd.to_numeric(out[col], errors="coerce").map(lambda x: "" if pd.isna(x) else f"{x:.2%}")
    return out.to_markdown(index=False)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD") from exc


def as_of_date_error(value: str, today: date) -> str | None:
    as_of = date.fromisoformat(value)
    if as_of > today:
        return f"--as-of-date cannot be in the future: {value} > {today.isoformat()}"
    return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean(payload), ensure_ascii=True, indent=2, allow_nan=False), encoding="utf-8")


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, tuple):
        return [clean(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value


def self_check() -> None:
    assert as_of_date_error("2026-06-19", date(2026, 6, 20)) is None
    assert "future" in str(as_of_date_error("2026-06-23", date(2026, 6, 20)))
    trades = pd.DataFrame([
        {"signal_date": "2020-01-01", "trade_return": 0.10, "is_bad_window": False, "market_volatility_20d_vs_60d": 1.40, "market_stress_score": 0.80, "negative_breadth_60d": 0.80},
        {"signal_date": "2020-02-01", "trade_return": -0.05, "is_bad_window": True, "market_volatility_20d_vs_60d": 1.00, "market_stress_score": 0.50, "negative_breadth_60d": 0.60},
        {"signal_date": "2021-01-01", "trade_return": 0.02, "is_bad_window": False, "market_volatility_20d_vs_60d": 1.10, "market_stress_score": 0.60, "negative_breadth_60d": 0.80},
    ])
    out = year_state_breakdown(trades, {"hard_gates": {"round_trip_cost_bps": 10}})
    assert {"year", "dimension", "bucket", "events", "net_mean_return"}.issubset(out.columns)
    assert int(out[out["dimension"].eq("volatility_guard")]["events"].sum()) == len(trades)
    assert set(out["year"]) == {2020, 2021}
    sparse = year_state_sparsity(out)
    assert sparse["year_state_cell_count"] == len(out)
    assert sparse["year_state_sparse_lt3_count"] > 0
    assert sparse["year_state_min_events"] == 1
    parameter = pd.DataFrame([
        {"variant_id": "base_v4_70", "effective": True, "failed_metrics": "", "failed_score_metrics": ""},
        {"variant_id": "entry_lag_1", "effective": False, "failed_metrics": "", "failed_score_metrics": "worst_cluster_net_return"},
    ])
    assert "entry_lag_1=worst_cluster_net_return" in parameter_failed_variants(parameter)
    assert "禁止把入场延迟从2日提前到1日" in parameter_failure_actions(parameter)
    cooldown = pd.DataFrame([{"cooldown_days": 60, "clusters": 12}])
    assert cooldown_failing_gaps(cooldown) == "60d=12簇"
    assert "2020/volatility_guard" in year_state_sparse_buckets(out)
    packet = build_live_decision_packet(
        {"tradable_carrier": {"min_replay_events": 1}},
        {"latest_signal_triggered": True, "production_ready": False, "latest_panel_date": "2026-06-18", "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21", "blocking_issues": []},
        {"planned_holding_days_if_triggered": 20},
        pd.DataFrame([{"carrier_code": "510300", "carrier_name": "沪深300ETF", "signal_date": "2026-01-01", "carrier_net_return": 0.01, "tracking_gap_vs_market_window": 0.01}]),
    )
    assert packet["carrier_review_scope"] == "broad_market_reference_only_not_industry_execution"
    assert "outputs/audit/v4_72_pre_trade_review_packet/top_candidates.csv" == packet["industry_carrier_review_packet"]
    assert "用宽基/全市场 ETF 替代行业载体入场" in packet["prohibited_use"]
    recent = pd.DataFrame({"trade_date": pd.to_datetime(["2026-07-10"]), "close": [1.0]})
    assert etf_cache_is_recent(recent, "20260711")
    assert not etf_cache_is_recent(recent, "20260720")
    section = live_decision_section(packet)
    assert "宽基/全市场载体观察候选" in section
    assert "V4.72 行业载体复核包" in section
    print("self_check=pass")


if __name__ == "__main__":
    main()
