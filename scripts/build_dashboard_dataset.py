"""Build a portable trust summary and lazy-loaded detail data for the dashboard.

Research outputs are read-only inputs.  The CLI writes a small
``dashboard-data-v2`` summary plus one ``dashboard-details-v1`` history chunk;
both documents are scrubbed of machine-local absolute paths before publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SHANGHAI_TZ = timezone(timedelta(hours=8))
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")

CURRENT_RUNNER_SUMMARY = Path("etf_assisted_trading_current/run_summary.json")
CURRENT_RECOMMENDATION = Path("etf_assisted_trading_current/debug/recommendation.json")
SOURCE_MANIFEST = Path("etf_assisted_trading_current/debug/source_manifest.csv")
GATE_RESULTS = Path("etf_assisted_trading_current/debug/gate_results.csv")
CURRENT_STATUS_SUMMARY = Path("audit/current_status/run_summary.json")
CURRENT_STATUS_SNAPSHOT = Path("audit/current_status/debug/status_snapshot.json")
CURRENT_STATE_SUMMARY = Path("audit/current_state_consistency/run_summary.json")
V24_DIR = Path("industry_fundamental_pressure_v2_4")
EXECUTION_REPLAY_DIR = Path("audit/etf_realistic_execution_replay")
TIMING_ROBUSTNESS_DIR = Path("industry_rebound_window_v4_71_robustness_live_audit")

SOURCE_EVIDENCE_PATHS = {
    "industry_history": "data_catalog/cache/industry_index/history/second",
    "valuation_history": "data_catalog/cache/industry_index/valuation_history/second/sws_second_industry_daily_valuation_2015_present.csv",
    "pit_valuation_methodology": "outputs/audit/pit_universe_methodology_remediation/run_summary.json",
    "valuation_snapshot": "data_catalog/cache/industry_index/valuation_snapshots/second",
    "market_index": "data_catalog/cache/market_index/wide",
    "etf_history": "data_catalog/cache/tradable_carrier_etf_history",
    "etf_pit_master": "data_catalog/etf_pit_master.csv",
    "timing_evidence": "outputs/audit/rebound_leader_forward_signal_detector_v5_08/run_summary.json",
    "industry_candidate_evidence": "outputs/audit/rebound_leader_forward_signal_detector_v5_08/debug/selected_industry_candidates.csv",
    "fund_flow": "data_catalog/cache/industry_fund_flow/ths",
    "account_state": "portfolio_lab/current_account_state.json",
}

GATE_LABELS = {
    "data_freshness": "数据完整性与时点",
    "pit_universe_methodology": "PIT估值与行业历史方法门",
    "timing_robustness": "V4.71 择时稳健性",
    "industry_selection": "强行业选择",
    "etf_pit_master": "ETF PIT 主表",
    "account_state": "账户状态",
    "portfolio_risk": "现有组合风险",
    "goal_evidence": "V5.10 目标证据",
    "current_industry_candidates": "当前行业候选",
    "agent_veto_chain": "六角色确定性否决链",
    "projected_portfolio_risk": "建议后组合风险",
    "forward_timing_evidence": "择时前推证据",
    "forward_industry_evidence": "强行业前推证据",
}

RUNS = [
    {
        "id": "current",
        "label": "当前 ETF 辅助交易主线",
        "dir": "etf_assisted_trading_current",
        "focus": "不可绕过的数据、择时、行业、ETF、账户、风险和证据门禁。",
    },
    {
        "id": "v2_4",
        "label": "V2.4 当前估值压力",
        "dir": "industry_fundamental_pressure_v2_4",
        "focus": "当前 PE/PB/股息率重新接入解释层，仍禁止历史回填。",
    },
    {
        "id": "v2_6",
        "label": "V2.6 估值 PIT 验证",
        "dir": "industry_valuation_pit_validation_v2_6",
        "focus": "使用保守 available_date 验证历史估值候选因子。",
    },
    {
        "id": "v2_7",
        "label": "V2.7 估值质量验证",
        "dir": "industry_valuation_quality_v2_7",
        "focus": "检验估值稳定性、股息连续性和质量代理。",
    },
    {
        "id": "v2_8",
        "label": "V2.8 RankIC 桥接",
        "dir": "industry_rankic_portfolio_bridge_v2_8",
        "focus": "检验 RankIC 信息能否转译为组合收益。",
    },
    {
        "id": "v2_9",
        "label": "V2.9 实时仿真",
        "dir": "industry_realtime_simulation_v2_9",
        "focus": "冻结低估超跌非陷阱 Top10，做 as-of 实时回放。",
    },
    {
        "id": "v2_10",
        "label": "V2.10 压力敏感性",
        "dir": "industry_realtime_pressure_sensitivity_v2_10",
        "focus": "固定检验压力门槛、陷阱阈值和权重扰动。",
    },
]


def clean_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    if pd.isna(value):
        return None
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    return value


def classify_index_state(price_percentile: float, rsi_14: float) -> tuple[str, str]:
    point_status = "低估区" if price_percentile <= 0.20 else "高估区" if price_percentile >= 0.80 else "中性区"
    momentum_status = "超卖" if rsi_14 <= 30 else "超买" if rsi_14 >= 70 else "中性"
    return point_status, momentum_status


def build_market_index_states(index_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(index_dir.glob("*.csv")):
        frame = pd.read_csv(path)
        required = {"trade_date", "close", "name_zh", "symbol"}
        if not required.issubset(frame.columns):
            continue
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
        if len(frame) < 252:
            continue
        close = frame["close"]
        latest_close = float(close.iloc[-1])
        lookback = close.tail(756)
        percentile = float((lookback <= latest_close).mean())
        delta = close.diff()
        average_gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
        average_loss = -delta.clip(upper=0).rolling(14).mean().iloc[-1]
        rsi = 100.0 if average_loss == 0 else float(100 - 100 / (1 + average_gain / average_loss))
        point_status, momentum_status = classify_index_state(percentile, rsi)
        rows.append({
            "symbol": str(frame["symbol"].iloc[-1]), "name": str(frame["name_zh"].iloc[-1]),
            "trade_date": frame["trade_date"].iloc[-1].date().isoformat(), "close": latest_close,
            "return_20d": float(latest_close / close.iloc[-21] - 1) if len(close) > 20 else None,
            "return_60d": float(latest_close / close.iloc[-61] - 1) if len(close) > 60 else None,
            "price_percentile_3y": percentile, "rsi_14": rsi,
            "point_status": point_status, "momentum_status": momentum_status,
            "status_basis": "过去756个交易日点位分位与14日RSI，不等同于PE/PB基本面估值",
        })
    return rows


def build_historical_etf_opportunities(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    frame = pd.read_csv(path)
    if frame.empty or "status" not in frame.columns:
        return []
    frame = frame[frame["status"] == "filled"].copy()
    frame["actual_entry_date"] = pd.to_datetime(frame["actual_entry_date"], errors="coerce")
    frame = frame.dropna(subset=["actual_entry_date"]).sort_values("actual_entry_date", ascending=False)
    rows = []
    for row in frame.itertuples(index=False):
        net_return = float(row.net_return)
        rows.append({
            "etf_code": str(row.etf_code), "entry_date": row.actual_entry_date.date().isoformat(),
            "entry_price": float(row.entry_price), "exit_date": str(row.actual_exit_date),
            "exit_price": float(row.exit_price), "holding_days": int(row.calendar_holding_days),
            "net_return": net_return, "result": "盈利" if net_return > 0 else "亏损" if net_return < 0 else "持平",
            "exit_reason": "止损退出" if str(row.exit_reason).startswith("stop") else "持有期结束",
            "evidence_status": "historical_replay_research_only",
        })
    return rows


def build_shanghai_candles(path: Path, opportunities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not path.exists():
        return [], []
    frame = pd.read_csv(path)
    required = {"trade_date", "open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        return [], []
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=list(required)).sort_values("trade_date")
    close = frame["close"]
    frame["price_percentile_3y"] = close.rolling(756, min_periods=252).apply(
        lambda values: float((values <= values[-1]).mean()), raw=True
    )
    delta = close.diff()
    average_gain = delta.clip(lower=0).rolling(14).mean()
    average_loss = -delta.clip(upper=0).rolling(14).mean()
    frame["rsi_14"] = 100 - 100 / (1 + average_gain / average_loss.replace(0, float("nan")))
    frame.loc[(average_loss == 0) & average_gain.notna(), "rsi_14"] = 100.0
    frame = frame[frame["trade_date"] >= pd.Timestamp("2015-01-01")]
    candles = []
    for row in frame.itertuples(index=False):
        percentile = float(row.price_percentile_3y)
        rsi = float(row.rsi_14)
        point_status, momentum_status = classify_index_state(percentile, rsi)
        candles.append({
            "time": row.trade_date.date().isoformat(), "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close), "price_percentile_3y": percentile,
            "rsi_14": rsi, "point_status": point_status, "momentum_status": momentum_status,
        })
    grouped: dict[tuple[str, str], int] = {}
    for row in opportunities:
        for action, field in (("buy", "entry_date"), ("sell", "exit_date")):
            key = (str(row[field]), action)
            grouped[key] = grouped.get(key, 0) + 1
    markers = [{"time": day, "action": action, "count": count} for (day, action), count in sorted(grouped.items())]
    return candles, markers


def records_from_csv(path: Path, *, limit: int | None = None) -> tuple[list[dict[str, Any]], str | None]:
    if not path.exists():
        return [], f"缺少文件：{path.relative_to(ROOT)}"
    try:
        frame = pd.read_csv(path)
    except Exception as exc:  # pragma: no cover - user-facing diagnostic
        return [], f"读取失败：{path.relative_to(ROOT)}；{exc}"
    if limit is not None:
        frame = frame.head(limit)
    rows = []
    for record in frame.to_dict(orient="records"):
        rows.append({str(key): clean_value(value) for key, value in record.items()})
    return rows, None


def read_json(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, f"缺少文件：{path.relative_to(ROOT)}"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:  # pragma: no cover - user-facing diagnostic
        return {}, f"读取失败：{path.relative_to(ROOT)}；{exc}"
    return {str(key): clean_value(value) for key, value in data.items()}, None


def read_text_head(path: Path, *, max_chars: int = 3200) -> tuple[str, str | None]:
    if not path.exists():
        return "", f"缺少文件：{path.relative_to(ROOT)}"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # pragma: no cover - user-facing diagnostic
        return "", f"读取失败：{path.relative_to(ROOT)}；{exc}"
    return text[:max_chars], None


def read_filtered_csv(
    path: Path,
    *,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    if not path.exists():
        return [], f"缺少文件：{path.relative_to(ROOT)}"
    try:
        frame = pd.read_csv(path)
    except Exception as exc:  # pragma: no cover - user-facing diagnostic
        return [], f"读取失败：{path.relative_to(ROOT)}；{exc}"
    if filters:
        for column, expected in filters.items():
            if column not in frame.columns:
                return [], f"缺少列：{path.relative_to(ROOT)}::{column}"
            frame = frame[frame[column] == expected]
    if limit is not None:
        frame = frame.head(limit)
    rows = []
    for record in frame.to_dict(orient="records"):
        rows.append({str(key): clean_value(value) for key, value in record.items()})
    return rows, None


def read_nav_samples(path: Path, *, every_n: int = 10) -> tuple[list[dict[str, Any]], str | None]:
    if not path.exists():
        return [], f"缺少文件：{path.relative_to(ROOT)}"
    try:
        frame = pd.read_csv(path)
    except Exception as exc:  # pragma: no cover - user-facing diagnostic
        return [], f"读取失败：{path.relative_to(ROOT)}；{exc}"
    required_columns = [
        "trade_date",
        "parameter_id",
        "active_strategy_nav",
        "active_benchmark_nav",
        "active_relative_nav",
        "strategy_nav",
        "benchmark_nav",
        "relative_nav",
        "is_invested",
        "selected_count",
    ]
    available_columns = [column for column in required_columns if column in frame.columns]
    if "parameter_id" not in available_columns or "trade_date" not in available_columns:
        return [], f"缺少列：{path.relative_to(ROOT)}::parameter_id/trade_date"

    rows: list[dict[str, Any]] = []
    for _, group in frame[available_columns].sort_values(["parameter_id", "trade_date"]).groupby("parameter_id"):
        sample = group.iloc[::every_n].copy()
        if not sample.empty and sample.iloc[-1]["trade_date"] != group.iloc[-1]["trade_date"]:
            sample = pd.concat([sample, group.tail(1)], ignore_index=True)
        for record in sample.to_dict(orient="records"):
            rows.append({str(key): clean_value(value) for key, value in record.items()})
    return rows, None


def audit_package(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {**summary, "summary": summary, "rows": rows}


def collect_audits(debug_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    audit_specs = [
        ("timestamp", "时间戳审计", "timestamp_audit.csv"),
        ("leakage", "泄漏审计", "leakage_audit.csv"),
        ("source", "数据源审计", "source_audit.csv"),
        ("asof_replay", "as-of 回放一致性", "asof_replay_consistency.csv"),
    ]
    audits: list[dict[str, Any]] = []
    for group, label, filename in audit_specs:
        rows, warning = records_from_csv(debug_dir / filename, limit=80)
        if warning:
            warnings.append(warning)
            continue
        for row in rows:
            audits.append({"group": group, "group_zh": label, **row})
    return audits, warnings


def build_legacy_payload(outputs_dir: Path) -> dict[str, Any]:
    """Retained as a read-only migration aid; the CLI no longer writes this v1 shape."""
    warnings: list[str] = []
    min_safe_sample = 30
    versions: list[dict[str, Any]] = []
    summaries: dict[str, dict[str, Any]] = {}
    top_candidates: dict[str, list[dict[str, Any]]] = {}
    report_previews: dict[str, str] = {}

    for run in RUNS:
        run_dir = outputs_dir / run["dir"]
        summary, warning = read_json(run_dir / "run_summary.json")
        if warning:
            warnings.append(warning)
        report, warning = read_text_head(run_dir / "report.md")
        if warning:
            warnings.append(warning)
        candidates, warning = records_from_csv(run_dir / "top_candidates.csv", limit=120)
        if warning:
            warnings.append(warning)

        status = summary.get("policy_status") or summary.get("status") or "research_only"
        verdict = summary.get("final_verdict") or summary.get("research_boundary") or "未提供结论"
        version_number = summary.get("version") or run["label"].split()[0]
        audit_fail_count = summary.get("audit_fail_count")
        candidate_count = (
            summary.get("candidate_requires_source_audit_count")
            if summary.get("candidate_requires_source_audit_count") is not None
            else summary.get("candidate_count")
        )

        versions.append(
            {
                "id": run["id"],
                "label": run["label"],
                "version": version_number,
                "output_dir": f"outputs/{run['dir']}",
                "focus": run["focus"],
                "status": status,
                "final_verdict": verdict,
                "candidate_count": candidate_count,
                "conditional_count": summary.get("conditional_observation_count"),
                "rejected_count": summary.get("rejected_count"),
                "audit_fail_count": audit_fail_count,
                "generated_at": summary.get("generated_at"),
                "best_parameter_id": summary.get("best_parameter_id"),
                "primary_relative_return": summary.get("best_60d_mean_relative_return")
                or summary.get("primary_60d_mean_relative_return"),
                "active_relative_final_nav": summary.get("best_active_relative_final_nav")
                or summary.get("active_relative_final_nav"),
            }
        )
        summaries[run["id"]] = summary
        top_candidates[run["id"]] = candidates
        report_previews[run["id"]] = report

    v210_dir = outputs_dir / "industry_realtime_pressure_sensitivity_v2_10"
    debug_dir = v210_dir / "debug"
    v210_summary = summaries.get("v2_10", {})
    best_parameter = v210_summary.get("best_parameter_id") or "no_pressure_gate__trap1__quality_plus"

    parameter_summary, warning = records_from_csv(debug_dir / "parameter_summary.csv", limit=300)
    if warning:
        warnings.append(warning)
    pressure_gate_effect, warning = records_from_csv(debug_dir / "pressure_gate_effect.csv")
    if warning:
        warnings.append(warning)
    trap_effect, warning = records_from_csv(debug_dir / "momentum_trap_effect.csv")
    if warning:
        warnings.append(warning)
    weight_sensitivity, warning = records_from_csv(debug_dir / "weight_sensitivity.csv")
    if warning:
        warnings.append(warning)
    cash_sensitivity, warning = records_from_csv(debug_dir / "cash_sensitivity.csv")
    if warning:
        warnings.append(warning)
    nav_series, warning = read_filtered_csv(
        debug_dir / "parameter_daily_nav.csv",
        filters={"parameter_id": best_parameter},
    )
    if warning:
        warnings.append(warning)
    event_returns, warning = read_filtered_csv(
        debug_dir / "parameter_event_returns.csv",
        filters={"parameter_id": best_parameter, "horizon": 60},
        limit=200,
    )
    if warning:
        warnings.append(warning)
    parameter_nav_samples, warning = read_nav_samples(debug_dir / "parameter_daily_nav.csv", every_n=10)
    if warning:
        warnings.append(warning)
    parameter_event_returns_full, warning = records_from_csv(debug_dir / "parameter_event_returns.csv")
    if warning:
        warnings.append(warning)
    audits, audit_warnings = collect_audits(debug_dir)
    warnings.extend(audit_warnings)
    live_manifest, warning = read_json(outputs_dir / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "live_refresh_manifest.json")
    if warning:
        warnings.append(warning)
    pre_trade_review_packet, warning = records_from_csv(outputs_dir / "audit" / "v4_72_pre_trade_review_packet" / "top_candidates.csv", limit=30)
    if warning:
        warnings.append(warning)
    pre_trade_review_summary, warning = read_json(outputs_dir / "audit" / "v4_72_pre_trade_review_packet" / "run_summary.json")
    if warning:
        warnings.append(warning)
    entry_readiness_rows, warning = records_from_csv(outputs_dir / "audit" / "v4_72_entry_readiness" / "top_candidates.csv", limit=30)
    if warning:
        warnings.append(warning)
    pre_entry_action_checklist, warning = records_from_csv(outputs_dir / "audit" / "v4_72_entry_readiness" / "debug" / "pre_entry_action_checklist.csv", limit=30)
    if warning:
        warnings.append(warning)
    entry_readiness_summary, warning = read_json(outputs_dir / "audit" / "v4_72_entry_readiness" / "run_summary.json")
    if warning:
        warnings.append(warning)
    operator_checklist, warning = records_from_csv(outputs_dir / "audit" / "v4_72_pre_entry_operator_checklist" / "debug" / "operator_checklist.csv", limit=30)
    if warning:
        warnings.append(warning)
    operator_checklist_summary, warning = read_json(outputs_dir / "audit" / "v4_72_pre_entry_operator_checklist" / "run_summary.json")
    if warning:
        warnings.append(warning)
    forward_settlement_schedule, warning = records_from_csv(outputs_dir / "audit" / "v4_72_forward_return_settlement" / "debug" / "settlement_schedule.csv", limit=30)
    if warning:
        warnings.append(warning)
    scorecard_rows, warning = records_from_csv(outputs_dir / "audit" / "v4_72_rebound_leader_evaluation_scorecard" / "top_candidates.csv", limit=30)
    if warning:
        warnings.append(warning)
    scorecard_summary, warning = read_json(outputs_dir / "audit" / "v4_72_rebound_leader_evaluation_scorecard" / "run_summary.json")
    if warning:
        warnings.append(warning)
    remediation_rows, warning = records_from_csv(outputs_dir / "audit" / "v4_72_remediation_queue" / "top_candidates.csv", limit=30)
    if warning:
        warnings.append(warning)
    remediation_summary, warning = read_json(outputs_dir / "audit" / "v4_72_remediation_queue" / "run_summary.json")
    if warning:
        warnings.append(warning)
    goal_readiness_rows, warning = records_from_csv(outputs_dir / "audit" / "goal_readiness_check" / "top_candidates.csv", limit=30)
    if warning:
        warnings.append(warning)
    goal_readiness_summary, warning = read_json(outputs_dir / "audit" / "goal_readiness_check" / "run_summary.json")
    if warning:
        warnings.append(warning)

    required_v210 = [
        ("version", v210_summary.get("version")),
        ("policy_status", v210_summary.get("policy_status")),
        ("parameter_count", v210_summary.get("parameter_count")),
        ("best_parameter_id", v210_summary.get("best_parameter_id")),
        ("best_60d_mean_relative_return", v210_summary.get("best_60d_mean_relative_return")),
        ("audit_fail_count", v210_summary.get("audit_fail_count")),
    ]
    for field, value in required_v210:
        if value is None:
            warnings.append(f"V2.10 核心字段缺失：{field}")

    primary_parameter_rows = [row for row in parameter_summary if row.get("horizon") == 60]
    positive_primary_relative_count = sum(
        1
        for row in primary_parameter_rows
        if isinstance(row.get("mean_relative_return"), (int, float)) and row.get("mean_relative_return") > 0
    )
    below_safe_sample_count = sum(
        1
        for row in primary_parameter_rows
        if isinstance(row.get("samples"), (int, float)) and row.get("samples") < min_safe_sample
    )
    passed_candidate_count = sum(
        1
        for row in primary_parameter_rows
        if "候选" in str(row.get("signal_status", "")) and "条件" not in str(row.get("signal_status", ""))
    )

    current_recommendation, warning = read_json(outputs_dir / "etf_assisted_trading_current" / "debug" / "recommendation.json")
    if warning:
        warnings.append(warning)
    current_agents, warning = read_json(outputs_dir / "etf_assisted_trading_current" / "debug" / "agent_results.json")
    if warning:
        warnings.append(warning)
    etf_pit_summary, warning = read_json(outputs_dir / "audit" / "etf_pit_master" / "run_summary.json")
    if warning:
        warnings.append(warning)
    execution_replay_summary, warning = read_json(outputs_dir / "audit" / "etf_realistic_execution_replay" / "run_summary.json")
    if warning:
        warnings.append(warning)
    market_index_states = build_market_index_states(ROOT / "data_catalog" / "cache" / "market_index" / "wide")
    historical_etf_opportunities = build_historical_etf_opportunities(
        outputs_dir / "audit" / "etf_realistic_execution_replay" / "debug" / "trade_ledger.csv"
    )
    shanghai_index_candles, shanghai_index_trade_markers = build_shanghai_candles(
        ROOT / "data_catalog" / "cache" / "market_index" / "wide" / "sh000001.csv",
        historical_etf_opportunities,
    )
    timing_robustness_summary, warning = read_json(outputs_dir / "industry_rebound_window_v4_71_robustness_live_audit" / "run_summary.json")
    if warning:
        warnings.append(warning)
    if not market_index_states:
        warnings.append("主要A股指数状态数据为空")
    if not historical_etf_opportunities:
        warnings.append("历史ETF机会数据为空")
    if not shanghai_index_candles:
        warnings.append("上证综指K线数据为空")

    payload = {
        "schema_version": "dashboard-data-v1",
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "active_version": "current",
        "data_quality_warnings": warnings,
        "versions": versions,
        "summaries": summaries,
        "top_candidates": top_candidates,
        "report_previews": report_previews,
        "nav_series": {"v2_10": nav_series},
        "parameter_nav_samples": parameter_nav_samples,
        "parameter_summary": parameter_summary,
        "pressure_gate_effect": pressure_gate_effect,
        "trap_effect": trap_effect,
        "weight_sensitivity": weight_sensitivity,
        "cash_sensitivity": cash_sensitivity,
        "event_returns": {"v2_10": event_returns},
        "parameter_event_returns": parameter_event_returns_full,
        "audits": audits,
        "daily_decision_summary": live_manifest.get("daily_decision_summary", {}),
        "current_recommendation": current_recommendation,
        "current_agents": current_agents.get("agents", []),
        "etf_pit_summary": etf_pit_summary,
        "execution_replay_summary": execution_replay_summary,
        "market_index_states": market_index_states,
        "historical_etf_opportunities": historical_etf_opportunities,
        "historical_opportunity_summary": {
            "replay_record_count": len(historical_etf_opportunities),
            "independent_cluster_count_60d": timing_robustness_summary.get("cooldown_60_clusters"),
            "independence_rule": "入场信号按60个交易日冷却合并",
        },
        "shanghai_index_candles": shanghai_index_candles,
        "shanghai_index_trade_markers": shanghai_index_trade_markers,
        "pre_trade_review_packet": {
            "summary": pre_trade_review_summary,
            "rows": pre_trade_review_packet,
        },
        "entry_readiness": audit_package(entry_readiness_summary, entry_readiness_rows),
        "operator_checklist": {
            "summary": operator_checklist_summary,
            "rows": operator_checklist,
        },
        "pre_entry_action_checklist": pre_entry_action_checklist,
        "forward_settlement_schedule": forward_settlement_schedule,
        "goal_readiness": audit_package(goal_readiness_summary, goal_readiness_rows),
        "rebound_leader_scorecard": {
            "summary": scorecard_summary,
            "rows": scorecard_rows,
        },
        "remediation_queue": {
            "summary": remediation_summary,
            "rows": remediation_rows,
        },
        "parameter_guardrails": {
            "min_safe_sample": min_safe_sample,
            "primary_horizon": 60,
            "primary_parameter_count": len(primary_parameter_rows),
            "positive_primary_relative_count": positive_primary_relative_count,
            "below_safe_sample_count": below_safe_sample_count,
            "passed_candidate_count": passed_candidate_count,
            "headline": "V2.10 无参数组合通过候选门槛；参数实验台只用于敏感性理解，不用于寻找交易参数。",
            "low_sample_warning": "样本数低于 30 时，结果容易被少数历史窗口支配，不能作为稳健证据。",
            "mixed_evidence_warning": "样本外、非重叠和全样本方向不一致时，应按证据不足处理。",
        },
        "ui_notes": {
            "research_boundary": "本面板展示申万行业、指数和境内股票 ETF 实施研究；只生成需人工确认的辅助状态，不自动下单。",
            "alpha_boundary": "research_only 表示研究观察；当前 ETF 辅助交易主线未通过全部门禁，不能解释为有效买卖信号。",
        },
    }
    return payload


def as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def generated_at_shanghai() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def timestamp_in_shanghai(value: Any) -> str:
    """Attach the documented local zone to naive source timestamps."""
    if not value:
        return ""
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    else:
        parsed = parsed.astimezone(SHANGHAI_TZ)
    return parsed.isoformat(timespec="seconds")


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "pass"}


def int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def number_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        is_percent = text.endswith("%")
        if is_percent:
            text = text[:-1]
        try:
            number = float(text)
        except ValueError:
            return None
        return number / 100.0 if is_percent else number
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def portable_string(value: str) -> str:
    """Normalize repo-local paths and reject any remaining Windows drive path."""
    normalized = value.replace(str(ROOT), ".").replace(ROOT.as_posix(), ".")
    normalized = normalized.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if WINDOWS_ABSOLUTE_PATH.search(normalized):
        raise ValueError(f"dashboard document contains a non-portable absolute path: {normalized}")
    return normalized


def portable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): portable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [portable_value(item) for item in value]
    if isinstance(value, Path):
        return portable_string(str(value))
    if isinstance(value, str):
        return portable_string(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return clean_value(value)


def json_document_bytes(document: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    portable = portable_value(dict(document))
    encoded = (json.dumps(portable, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    rendered = encoded.decode("utf-8")
    if WINDOWS_ABSOLUTE_PATH.search(rendered):
        raise ValueError("dashboard document contains a serialized Windows absolute path")
    return portable, encoded


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def structured_warning(
    code: str,
    severity: str,
    message: str,
    *,
    source: str | None = None,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    if not evidence_id:
        raise ValueError(f"structured warning {code!r} requires a stable evidence_id")
    return {
        "code": code,
        "severity": severity,
        "source": source or evidence_id,
        "message": message,
        "evidence_id": evidence_id,
    }


def load_json_source(
    path: Path,
    *,
    evidence_id: str,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    payload, warning = read_json(path)
    if warning:
        warnings.append(
            structured_warning(
                "authoritative_source_unavailable",
                "error",
                warning,
                source=evidence_id,
                evidence_id=evidence_id,
            )
        )
    return payload


def load_csv_source(
    path: Path,
    *,
    evidence_id: str,
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows, warning = records_from_csv(path)
    if warning:
        warnings.append(
            structured_warning(
                "authoritative_source_unavailable",
                "error",
                warning,
                source=evidence_id,
                evidence_id=evidence_id,
            )
        )
    return rows


def source_status(row: Mapping[str, Any]) -> str:
    source = str(row.get("source", ""))
    raw_status = str(row.get("status", "")).strip().lower()
    detail = str(row.get("detail", "")).lower()
    required = bool_value(row.get("required"))
    cutoff = str(row.get("latest_date") or row.get("cutoff_date") or "")
    if raw_status == "superseded" or "superseded" in detail:
        return "superseded"
    if raw_status == "degraded" or "degraded" in detail:
        return "degraded"
    if not cutoff:
        return "blocked" if required else "missing_optional"
    if raw_status not in {"pass", "fresh", "ok"}:
        return "blocked" if required else "stale_optional"
    if source == "valuation_history":
        return "historical_archive"
    return "fresh"


def build_source_freshness(
    manifest_rows: list[dict[str, Any]],
    *,
    decision_as_of_date: str,
    account_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows = list(manifest_rows)
    if not any(str(row.get("source", "")) == "account_state" for row in rows):
        rows.append(
            {
                "source": "account_state",
                "latest_date": account_state.get("as_of_date"),
                "required": True,
                "status": "pass" if account_state.get("gate_passed") is True else "fail",
                "detail": (
                    f"configured={account_state.get('configured')}; "
                    f"gate_passed={account_state.get('gate_passed')}; "
                    f"position_count={account_state.get('position_count')}"
                ),
            }
        )

    decision_date = iso_date(decision_as_of_date)
    freshness: list[dict[str, Any]] = []
    for row in rows:
        source = str(row.get("source", ""))
        if not source:
            continue
        cutoff = str(row.get("latest_date") or row.get("cutoff_date") or "") or None
        lag = int_value(row.get("age_calendar_days"))
        cutoff_date = iso_date(cutoff)
        if lag is None and decision_date is not None and cutoff_date is not None:
            lag = (decision_date - cutoff_date).days
        freshness.append(
            {
                "source": source,
                "source_id": source,
                "cutoff_date": cutoff,
                "lag_days": lag,
                "required": bool_value(row.get("required")),
                "status": source_status(row),
                "detail": str(row.get("detail") or ""),
                "evidence_id": f"source.{source}",
            }
        )
    return freshness


def source_quality_warnings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    labels = {
        "historical_archive": ("info", "该源是历史档案口径，不代表已更新到决策日。"),
        "blocked": ("error", "必需数据源或方法门处于阻断状态。"),
        "missing_optional": ("warning", "可选数据源缺失，本轮未用它解除任何门禁。"),
        "stale_optional": ("warning", "可选数据源陈旧，本轮只作降级参考。"),
        "degraded": ("warning", "该数据源处于降级状态，不得静默当作完整证据。"),
        "superseded": ("warning", "该数据源已被后续版本替代，不得作为当前证据。"),
    }
    for row in rows:
        status = str(row.get("status", ""))
        if status == "fresh":
            continue
        severity, text = labels.get(status, ("warning", "数据源状态需要复核。"))
        source = str(row.get("source", ""))
        cutoff = row.get("cutoff_date") or "无可用日期"
        result.append(
            structured_warning(
                f"source_{status}",
                severity,
                f"{source}: {text} 截止={cutoff}；滞后天数={row.get('lag_days')}。",
                source=source,
                evidence_id=str(row.get("evidence_id", "")),
            )
        )
    return result


def build_gate_results(
    raw_rows: list[dict[str, Any]],
    hard_gates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_by_id = {str(row.get("gate", "")): row for row in raw_rows if row.get("gate")}
    hard_by_id = {str(row.get("gate_id", "")): row for row in hard_gates if row.get("gate_id")}
    ordered_ids = list(hard_by_id)
    ordered_ids.extend(gate_id for gate_id in raw_by_id if gate_id not in hard_by_id)
    result: list[dict[str, Any]] = []
    for gate_id in ordered_ids:
        hard = hard_by_id.get(gate_id, {})
        raw = raw_by_id.get(gate_id, {})
        raw_status = str(hard.get("status") or raw.get("status") or "blocked").lower()
        passed = raw_status in {"pass", "passed", "ok"}
        result.append(
            {
                "gate_id": gate_id,
                "label": str(hard.get("label") or GATE_LABELS.get(gate_id, gate_id)),
                "passed": passed,
                "status": "pass" if passed else "blocked",
                "veto": not passed,
                "veto_capable": bool_value(raw.get("veto", True)),
                "reason": str(hard.get("evidence") or raw.get("evidence") or ""),
                "evidence_id": "runner.gate_results" if gate_id in raw_by_id else "current_status.snapshot",
            }
        )
    return result


def normalized_valuation_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rank": int_value(row.get("排名")),
        "industry_code": str(row.get("行业代码") or ""),
        "industry_name": str(row.get("行业") or ""),
        "parent_industry": str(row.get("上级行业") or ""),
        "status": str(row.get("状态") or ""),
        "score": number_value(row.get("V2.4综合分")),
        "valuation_score": number_value(row.get("估值分")),
        "oversold_score": number_value(row.get("超跌分")),
        "price_quality_score": number_value(row.get("价格质量分")),
        "trap_risk": number_value(row.get("陷阱风险")),
        "pe_ttm": number_value(row.get("PE_TTM")),
        "pb": number_value(row.get("PB")),
        "dividend_yield": number_value(row.get("股息率")),
        "return_60d": number_value(row.get("60日收益")),
        "return_120d": number_value(row.get("120日收益")),
        "drawdown_252d": number_value(row.get("252日回撤")),
        "pressure_score": number_value(row.get("当前压力分")),
        "pressure_status": str(row.get("压力状态") or ""),
        "pit_status": str(row.get("PIT状态") or ""),
        "note": str(row.get("说明") or ""),
    }


def build_evidence_catalog(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}

    def add(evidence_id: str, path: str) -> None:
        catalog[evidence_id] = {
            "evidence_id": evidence_id,
            "path": path,
            "local_generated": True,
            "linkable": False,
        }

    add("current_status.summary", "outputs/audit/current_status/run_summary.json")
    add("current_status.snapshot", "outputs/audit/current_status/debug/status_snapshot.json")
    add("current_state.summary", "outputs/audit/current_state_consistency/run_summary.json")
    add("runner.summary", "outputs/etf_assisted_trading_current/run_summary.json")
    add("runner.recommendation", "outputs/etf_assisted_trading_current/debug/recommendation.json")
    add("runner.source_manifest", "outputs/etf_assisted_trading_current/debug/source_manifest.csv")
    add("runner.gate_results", "outputs/etf_assisted_trading_current/debug/gate_results.csv")
    add("valuation.v2_4.summary", "outputs/industry_fundamental_pressure_v2_4/run_summary.json")
    add("valuation.v2_4.candidates", "outputs/industry_fundamental_pressure_v2_4/top_candidates.csv")
    add("market.index.history", "data_catalog/cache/market_index/wide")
    add("execution.replay.ledger", "outputs/audit/etf_realistic_execution_replay/debug/trade_ledger.csv")
    add("timing.robustness.summary", "outputs/industry_rebound_window_v4_71_robustness_live_audit/run_summary.json")
    add("dashboard.details", "strategy_lab/research_dashboard/public/data/dashboard_details.json")
    for row in source_rows:
        source = str(row.get("source", ""))
        if source:
            add(str(row.get("evidence_id")), SOURCE_EVIDENCE_PATHS.get(source, "outputs/etf_assisted_trading_current/debug/source_manifest.csv"))
    return list(catalog.values())


def build_documents(outputs_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    load_warnings: list[dict[str, Any]] = []
    current_runner = load_json_source(outputs_dir / CURRENT_RUNNER_SUMMARY, evidence_id="runner.summary", warnings=load_warnings)
    recommendation = load_json_source(outputs_dir / CURRENT_RECOMMENDATION, evidence_id="runner.recommendation", warnings=load_warnings)
    current_status_summary = load_json_source(outputs_dir / CURRENT_STATUS_SUMMARY, evidence_id="current_status.summary", warnings=load_warnings)
    current_status_snapshot = load_json_source(outputs_dir / CURRENT_STATUS_SNAPSHOT, evidence_id="current_status.snapshot", warnings=load_warnings)
    current_state = load_json_source(outputs_dir / CURRENT_STATE_SUMMARY, evidence_id="current_state.summary", warnings=load_warnings)
    source_manifest = load_csv_source(outputs_dir / SOURCE_MANIFEST, evidence_id="runner.source_manifest", warnings=load_warnings)
    raw_gate_results = load_csv_source(outputs_dir / GATE_RESULTS, evidence_id="runner.gate_results", warnings=load_warnings)
    v24_summary = load_json_source(outputs_dir / V24_DIR / "run_summary.json", evidence_id="valuation.v2_4.summary", warnings=load_warnings)
    v24_candidates = load_csv_source(outputs_dir / V24_DIR / "top_candidates.csv", evidence_id="valuation.v2_4.candidates", warnings=load_warnings)
    timing_robustness = load_json_source(outputs_dir / TIMING_ROBUSTNESS_DIR / "run_summary.json", evidence_id="timing.robustness.summary", warnings=load_warnings)

    status = as_mapping(current_status_snapshot.get("status"))
    decision_as_of_date = str(
        current_status_summary.get("decision_as_of")
        or status.get("decision_as_of")
        or current_state.get("current_as_of_date")
        or current_runner.get("as_of_date")
        or recommendation.get("data_cutoff_date")
        or ""
    )
    current_action = str(
        current_status_summary.get("current_action")
        or status.get("action")
        or current_state.get("current_action")
        or current_runner.get("action")
        or recommendation.get("action")
        or "BLOCKED_DATA"
    )

    current_status_cohort = as_mapping(as_mapping(status.get("freeze_layers")).get("fund_flow_evidence_cohort"))
    active_cohort_id = str(current_state.get("active_cohort_id") or "")
    active_manifest_hash = str(current_state.get("active_cohort_manifest_hash") or "")
    status_cohort_id = str(current_status_cohort.get("cohort_id") or "")
    status_manifest_hash = str(current_status_cohort.get("manifest_hash") or "")
    state_consistent = current_state.get("state_consistent") is True and status.get("state_consistent") is True
    cohort_consistent = bool(
        state_consistent
        and current_state.get("active_cohort_validated") is True
        and current_status_cohort.get("freeze_passed") is True
        and active_cohort_id
        and active_manifest_hash
        and (active_cohort_id, active_manifest_hash) == (status_cohort_id, status_manifest_hash)
    )

    trust_summary = {
        "research_state": str(current_status_summary.get("policy_status") or current_runner.get("policy_status") or "blocked_data"),
        "policy_status": str(current_status_summary.get("policy_status") or current_runner.get("policy_status") or "blocked_data"),
        "current_action": current_action,
        "status_valid": current_status_summary.get("status_valid") is True,
        "state_consistent": state_consistent,
        "manual_support_ready": current_status_summary.get("manual_decision_support_ready") is True,
        "production_ready": current_status_summary.get("production_ready") is True,
        "auto_execution_allowed": current_status_summary.get("auto_execution_allowed") is True,
        "decision_as_of_date": decision_as_of_date,
        "current_status_generated_at": timestamp_in_shanghai(current_status_summary.get("generated_at") or status.get("source_snapshot_generated_at")),
        "current_state_generated_at": timestamp_in_shanghai(current_state.get("generated_at")),
        "runner_generated_at": timestamp_in_shanghai(current_runner.get("generated_at")),
        "active_cohort_id": active_cohort_id,
        "active_cohort_manifest_hash": active_manifest_hash,
        "current_status_cohort_id": status_cohort_id,
        "current_status_manifest_hash": status_manifest_hash,
        "cohort_consistent": cohort_consistent,
    }

    source_freshness = build_source_freshness(
        source_manifest,
        decision_as_of_date=decision_as_of_date,
        account_state=as_mapping(status.get("account_state")),
    )
    gate_results = build_gate_results(raw_gate_results, as_rows(status.get("hard_gates")))
    warnings = [*load_warnings, *source_quality_warnings(source_freshness)]

    if trust_summary["status_valid"] is not True:
        warnings.append(structured_warning("current_status_invalid", "error", "CURRENT_STATUS 生成审计未通过。", evidence_id="current_status.summary"))
    if not state_consistent:
        warnings.append(structured_warning("current_state_inconsistent", "error", "当前状态源未通过一致性校验。", evidence_id="current_state.summary"))
    if not cohort_consistent:
        warnings.append(structured_warning("cohort_mismatch", "error", "active cohort 与 CURRENT_STATUS cohort pair 不一致或未复验。", evidence_id="current_state.summary"))

    action_values = {
        str(value)
        for value in [current_action, current_runner.get("action"), recommendation.get("action"), current_state.get("current_action")]
        if value
    }
    if len(action_values) > 1:
        warnings.append(structured_warning("current_action_mismatch", "error", f"权威来源动作不一致：{sorted(action_values)}。", evidence_id="current_status.summary"))
    as_of_values = {
        str(value)
        for value in [decision_as_of_date, current_runner.get("as_of_date"), recommendation.get("data_cutoff_date"), current_state.get("current_as_of_date")]
        if value
    }
    if len(as_of_values) > 1:
        warnings.append(structured_warning("decision_as_of_mismatch", "error", f"权威来源 decision as-of 不一致：{sorted(as_of_values)}。", evidence_id="current_status.summary"))

    candidates = as_rows(recommendation.get("candidates"))
    buy_candidates = [row for row in candidates if str(row.get("action", "")) == "BUY_CANDIDATE"]
    if current_action != "BUY_CANDIDATE" and buy_candidates:
        warnings.append(structured_warning("no_action_contains_buy_candidate", "error", "非 BUY_CANDIDATE 总动作中混入买入候选，展示必须失败关闭。", evidence_id="runner.recommendation"))
    if current_action == "BUY_CANDIDATE" and not buy_candidates:
        warnings.append(structured_warning("buy_action_without_candidate", "error", "BUY_CANDIDATE 总动作没有对应候选，展示必须失败关闭。", evidence_id="runner.recommendation"))

    normalized_candidates = [normalized_valuation_candidate(row) for row in v24_candidates]
    snapshot_date = str(v24_summary.get("valuation_snapshot_date") or "")
    latest_snapshot_cutoff = next(
        (str(row.get("cutoff_date") or "") for row in source_freshness if row.get("source") == "valuation_snapshot"),
        "",
    )
    snapshot_status = "missing" if not snapshot_date else "stale" if latest_snapshot_cutoff and snapshot_date < latest_snapshot_cutoff else "current"
    valuation_snapshot = {
        "version": str(v24_summary.get("version") or "2.4.0"),
        "generated_at": timestamp_in_shanghai(v24_summary.get("generated_at")),
        "snapshot_date": snapshot_date or None,
        "status": snapshot_status,
        "pit_status": str(v24_summary.get("pit_valuation_status") or "unavailable"),
        "available_count": int_value(v24_summary.get("valuation_covered_rows")) or 0,
        "snapshot_count": int_value(v24_summary.get("valuation_snapshot_count")) or 0,
        "candidate_count": len(normalized_candidates),
        "candidates": normalized_candidates,
    }
    if snapshot_status == "stale":
        warnings.append(
            structured_warning(
                "valuation_candidate_snapshot_stale",
                "warning",
                f"V2.4 候选估值日 {snapshot_date} 早于当前估值源截止 {latest_snapshot_cutoff}。",
                source="valuation_snapshot",
                evidence_id="valuation.v2_4.summary",
            )
        )

    market_index_states = build_market_index_states(ROOT / "data_catalog" / "cache" / "market_index" / "wide")
    historical_etf_opportunities = build_historical_etf_opportunities(
        outputs_dir / EXECUTION_REPLAY_DIR / "debug" / "trade_ledger.csv"
    )
    shanghai_index_candles, shanghai_index_trade_markers = build_shanghai_candles(
        ROOT / "data_catalog" / "cache" / "market_index" / "wide" / "sh000001.csv",
        historical_etf_opportunities,
    )
    generated_at = generated_at_shanghai()
    historical_opportunity_summary = {
        "replay_record_count": len(historical_etf_opportunities),
        "independent_cluster_count_60d": timing_robustness.get("cooldown_60_clusters"),
        "independence_rule": "入场信号按60个交易日冷却合并",
        "evidence_status": "historical_replay_research_only",
    }
    detail_counts = {
        "historical_etf_opportunities": len(historical_etf_opportunities),
        "shanghai_index_candles": len(shanghai_index_candles),
        "shanghai_index_trade_markers": len(shanghai_index_trade_markers),
    }
    details = {
        "schema_version": "dashboard-details-v1",
        "generated_at": generated_at,
        "decision_as_of_date": decision_as_of_date,
        "counts": detail_counts,
        "historical_etf_opportunities": historical_etf_opportunities,
        "historical_opportunity_summary": historical_opportunity_summary,
        "shanghai_index_candles": shanghai_index_candles,
        "shanghai_index_trade_markers": shanghai_index_trade_markers,
    }

    summary = {
        "schema_version": "dashboard-data-v2",
        "generated_at": generated_at,
        "decision_as_of_date": decision_as_of_date,
        "trust_summary": trust_summary,
        "source_freshness": source_freshness,
        "data_quality_warnings": warnings,
        "current_recommendation": recommendation,
        "gate_results": gate_results,
        "valuation_snapshot": valuation_snapshot,
        "market_index_states": market_index_states,
        "detail_manifest": {},
        "refresh_semantics": {
            "local_reload_label": "重新读取本地结果",
            "local_reload_note": "只重新读取已经生成的本地 JSON，不联网更新研究数据或账户状态。",
            "rebuild_command": "python ./scripts/build_dashboard_dataset.py",
            "online_refresh_command": f"python ./scripts/run_etf_assisted_trading_current.py --as-of-date {decision_as_of_date} --refresh-inputs",
            "network_refresh_note": "真正刷新研究数据须在本地受控环境运行命令并通过账户预检；网页按钮不会执行该命令。",
            "dev_port": 5173,
            "preview_port": 4175,
        },
        "fixed_notices": [
            {"code": "not_investment_advice", "text": "本仪表盘不构成投资建议。"},
            {"code": "history_not_future", "text": "历史回放不代表未来表现。"},
            {"code": "data_may_lag", "text": "数据可能延迟，请以各源截止时间和质量状态为准。"},
            {"code": "manual_support_not_ready", "text": "人工辅助交易未就绪。"},
            {"code": "auto_execution_disabled", "text": "自动执行关闭。"},
        ],
        "evidence_catalog": build_evidence_catalog(source_freshness),
    }
    return summary, details


def main() -> None:
    parser = argparse.ArgumentParser(description="Build portable summary and lazy-loaded detail data for the research dashboard.")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument(
        "--outputs-dir",
        default=str(ROOT / "outputs"),
        help="Directory containing versioned research outputs.",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "strategy_lab" / "research_dashboard" / "public" / "data" / "dashboard_data.json"),
        help="Frontend summary JSON output path.",
    )
    parser.add_argument(
        "--details-output",
        default="",
        help="Frontend detail JSON output path; defaults beside --output as dashboard_details.json.",
    )
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    output_path = Path(args.output)
    details_path = Path(args.details_output) if args.details_output else output_path.with_name("dashboard_details.json")
    summary, details = build_documents(Path(args.outputs_dir))

    details, details_bytes = json_document_bytes(details)
    summary["detail_manifest"] = {
        "url": f"data/{details_path.name}",
        "schema_version": str(details["schema_version"]),
        "sha256": hashlib.sha256(details_bytes).hexdigest(),
        "bytes": len(details_bytes),
        "counts": details["counts"],
    }
    summary, summary_bytes = json_document_bytes(summary)

    atomic_write_bytes(details_path, details_bytes)
    atomic_write_bytes(output_path, summary_bytes)
    print(f"dashboard_data.json generated: {output_path}")
    print(f"dashboard_details.json generated: {details_path}")
    print(
        f"summary_schema={summary['schema_version']}; summary_bytes={len(summary_bytes)}; "
        f"details_schema={details['schema_version']}; details_bytes={len(details_bytes)}; "
        f"warnings={len(summary['data_quality_warnings'])}"
    )


def self_check() -> None:
    package = audit_package({"goal_ready": False, "live_entry_decision": "no_entry_currently"}, [{"status": "fail"}])
    assert package["goal_ready"] is False
    assert package["live_entry_decision"] == "no_entry_currently"
    assert package["summary"]["goal_ready"] is False
    assert package["rows"][0]["status"] == "fail"
    assert classify_index_state(0.10, 50) == ("低估区", "中性")
    assert classify_index_state(0.90, 75) == ("高估区", "超买")
    assert classify_index_state(0.50, 25) == ("中性区", "超卖")
    assert source_status({"source": "valuation_history", "latest_date": "2025-12-31", "required": True, "status": "pass"}) == "historical_archive"
    assert source_status({"source": "required", "latest_date": "", "required": True, "status": "fail"}) == "blocked"
    assert source_status({"source": "optional", "latest_date": "", "required": False, "status": "fail"}) == "missing_optional"
    assert source_status({"source": "optional", "latest_date": "2026-01-01", "required": False, "status": "fail"}) == "stale_optional"
    assert source_status({"source": "optional", "latest_date": "2026-01-01", "required": False, "status": "degraded"}) == "degraded"
    warning = structured_warning("self_check", "warning", "self-check", evidence_id="current_status.summary")
    assert warning["source"] == "current_status.summary"
    assert portable_string(str(ROOT / "outputs" / "example.json")) == "outputs/example.json"
    try:
        portable_string(r"C:\outside\private.json")
    except ValueError:
        pass
    else:  # pragma: no cover - deterministic safety assertion
        raise AssertionError("external Windows absolute paths must be rejected")
    timestamp = generated_at_shanghai()
    assert timestamp.endswith("+08:00")
    assert timestamp_in_shanghai("2026-07-18T20:46:48").endswith("+08:00")
    candidate = normalized_valuation_candidate({"PE_TTM": "12.82", "PB": "1.01", "股息率": "4.52%"})
    assert candidate["pe_ttm"] == 12.82 and candidate["pb"] == 1.01 and candidate["dividend_yield"] == 0.0452
    print("self_check=pass")


if __name__ == "__main__":
    main()
