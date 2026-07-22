#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RANKING = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "all_ranked_industries.csv"
DEFAULT_VALUATION_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_snapshots" / "second"
DEFAULT_PRESSURE_PANEL = ROOT / "outputs" / "industry_pressure_quality_v2_3" / "debug" / "pressure_quality_signal_panel.csv"
DEFAULT_V23_SENSITIVITY = ROOT / "outputs" / "industry_pressure_quality_v2_3" / "debug" / "parameter_sensitivity.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_fundamental_pressure_v2_4"
VERSION = "2.4.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V2.4 current valuation and pressure candidate research.")
    parser.add_argument("--ranking", default=str(DEFAULT_RANKING), help="Current industry ranking panel.")
    parser.add_argument("--valuation-dir", default=str(DEFAULT_VALUATION_DIR), help="Industry valuation snapshot directory.")
    parser.add_argument("--valuation-date", default="", help="Optional valuation snapshot date YYYY-MM-DD.")
    parser.add_argument("--pressure-panel", default=str(DEFAULT_PRESSURE_PANEL), help="Historical pressure panel from V2.3.")
    parser.add_argument("--v23-sensitivity", default=str(DEFAULT_V23_SENSITIVITY), help="V2.3 parameter sensitivity table.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--candidate-count", type=int, default=20, help="Top current candidates to expose.")
    parser.add_argument("--min-pit-snapshots", type=int, default=60, help="Minimum valuation snapshots before PIT validation.")
    args = parser.parse_args()

    output_dir = Path(args.output)
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    valuation_inventory = build_valuation_inventory(Path(args.valuation_dir))
    valuation_snapshot = load_valuation_snapshot(Path(args.valuation_dir), args.valuation_date)
    ranking = load_current_ranking(Path(args.ranking))
    pressure_panel = load_pressure_panel(Path(args.pressure_panel))

    current_context = compute_current_market_pressure_context(ranking, pressure_panel)
    current_panel = build_current_fundamental_pressure_panel(
        ranking=ranking,
        valuation_snapshot=valuation_snapshot,
        valuation_inventory=valuation_inventory,
        current_context=current_context,
        min_pit_snapshots=args.min_pit_snapshots,
    )
    historical_evidence = build_historical_signal_evidence(Path(args.v23_sensitivity))
    pit_audit = build_pit_readiness_audit(
        valuation_inventory=valuation_inventory,
        valuation_snapshot=valuation_snapshot,
        current_panel=current_panel,
        min_pit_snapshots=args.min_pit_snapshots,
    )
    coverage = build_valuation_snapshot_coverage(valuation_snapshot, current_panel, valuation_inventory)
    decision_log = build_candidate_decision_log(current_panel)
    top_candidates = build_top_candidates(current_panel, args.candidate_count)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    current_panel.to_csv(debug_dir / "current_fundamental_pressure_panel.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(debug_dir / "valuation_snapshot_coverage.csv", index=False, encoding="utf-8-sig")
    historical_evidence.to_csv(debug_dir / "historical_signal_evidence.csv", index=False, encoding="utf-8-sig")
    pit_audit.to_csv(debug_dir / "pit_readiness_audit.csv", index=False, encoding="utf-8-sig")
    decision_log.to_csv(debug_dir / "candidate_decision_log.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "current_market_pressure_context.json", current_context)

    summary = {
        "version": VERSION,
        "language": "zh-CN",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "research_boundary": "V2.4 只把当前估值快照用于当前行业候选解释；不把当前 PE/PB/股息率回填到历史，也不生成交易指令。",
        "ranking_path": str(Path(args.ranking).resolve()),
        "valuation_dir": str(Path(args.valuation_dir).resolve()),
        "valuation_snapshot_date": valuation_snapshot.attrs.get("snapshot_date", ""),
        "valuation_snapshot_count": int(len(valuation_inventory)),
        "min_pit_snapshots": int(args.min_pit_snapshots),
        "pit_valuation_status": "pit_ready" if len(valuation_inventory) >= args.min_pit_snapshots else "current_snapshot_only_not_pit",
        "current_industry_rows": int(len(current_panel)),
        "valuation_covered_rows": int(current_panel["has_current_valuation"].sum()) if not current_panel.empty else 0,
        "current_observation_candidates": int((current_panel["candidate_status"] == "current_snapshot_candidate_not_pit_validated").sum())
        if not current_panel.empty
        else 0,
        "valuation_watchlist_count": int((current_panel["candidate_status"] == "valuation_watchlist_not_oversold").sum())
        if not current_panel.empty
        else 0,
        "oversold_without_valuation_support_count": int((current_panel["candidate_status"] == "oversold_without_valuation_support").sum())
        if not current_panel.empty
        else 0,
        "current_market_stress_score": current_context.get("current_market_stress_score"),
        "current_pressure_tier": current_context.get("current_pressure_tier"),
        "historical_evidence_rows": int(len(historical_evidence)),
    }
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_report(
            summary=summary,
            top_candidates=top_candidates,
            pit_audit=pit_audit,
            coverage=coverage,
            historical_evidence=historical_evidence,
            current_context=current_context,
            decision_log=decision_log,
        ),
        encoding="utf-8",
    )

    print(f"V{VERSION} 当前估值压力候选研究完成")
    print(f"估值快照日期={summary['valuation_snapshot_date']}")
    print(f"估值快照数量={summary['valuation_snapshot_count']}")
    print(f"PIT估值状态={summary['pit_valuation_status']}")
    print(f"当前候选数={summary['current_observation_candidates']}")
    print(f"当前压力状态={summary['current_pressure_tier']}({summary['current_market_stress_score']:.3f})")
    print(f"输出目录={output_dir.resolve()}")


def load_current_ranking(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    frame["industry_code"] = frame["industry_code"].map(lambda value: str(value).zfill(6))
    for col in [
        "industry_valuation_score",
        "industry_oversold_score",
        "industry_value_score",
        "cycle_quality_score",
        "data_quality_score",
        "pe_ttm",
        "pb",
        "dividend_yield",
        "return_20d",
        "return_60d",
        "return_120d",
        "return_252d",
        "drawdown_252d",
        "volatility_60d",
        "avg_amount_60d",
        "history_days",
    ]:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if "trade_date" in frame.columns:
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def build_valuation_inventory(valuation_dir: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for path in sorted(valuation_dir.glob("*.csv")):
        date = path.stem
        try:
            raw = pd.read_csv(path, encoding="utf-8-sig", dtype={"行业代码": str})
            rows.append(
                {
                    "snapshot_date": date,
                    "path": str(path.resolve()),
                    "rows": int(len(raw)),
                    "non_null_pe_ttm": int(pd.to_numeric(raw.get("TTM(滚动)市盈率"), errors="coerce").notna().sum())
                    if "TTM(滚动)市盈率" in raw.columns
                    else 0,
                    "non_null_pb": int(pd.to_numeric(raw.get("市净率"), errors="coerce").notna().sum()) if "市净率" in raw.columns else 0,
                    "non_null_dividend": int(pd.to_numeric(raw.get("静态股息率"), errors="coerce").notna().sum())
                    if "静态股息率" in raw.columns
                    else 0,
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append({"snapshot_date": date, "path": str(path.resolve()), "rows": 0, "error": str(exc)})
    return pd.DataFrame(rows)


def load_valuation_snapshot(valuation_dir: Path, requested_date: str) -> pd.DataFrame:
    paths = sorted(valuation_dir.glob("*.csv"))
    if requested_date:
        path = valuation_dir / f"{requested_date}.csv"
        if not path.exists():
            raise FileNotFoundError(f"valuation snapshot not found: {path}")
    elif paths:
        path = paths[-1]
    else:
        return pd.DataFrame()

    raw = pd.read_csv(path, encoding="utf-8-sig", dtype={"行业代码": str})
    frame = pd.DataFrame(
        {
            "industry_code": raw["行业代码"].map(lambda value: str(value).zfill(6)),
            "snapshot_industry_name": raw.get("行业名称", ""),
            "snapshot_parent_industry": raw.get("上级行业", ""),
            "constituent_count_snapshot": pd.to_numeric(raw.get("成份个数"), errors="coerce"),
            "pe_static_snapshot": pd.to_numeric(raw.get("静态市盈率"), errors="coerce"),
            "pe_ttm_snapshot": pd.to_numeric(raw.get("TTM(滚动)市盈率"), errors="coerce"),
            "pb_snapshot": pd.to_numeric(raw.get("市净率"), errors="coerce"),
            "dividend_yield_snapshot": pd.to_numeric(raw.get("静态股息率"), errors="coerce") / 100.0,
        }
    )
    frame.attrs["snapshot_date"] = path.stem
    frame.attrs["snapshot_path"] = str(path.resolve())
    return frame


def load_pressure_panel(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame.dropna(subset=["trade_date"])


def compute_current_market_pressure_context(ranking: pd.DataFrame, pressure_panel: pd.DataFrame) -> dict[str, Any]:
    current_return_120d = float(ranking["return_120d"].mean())
    current_volatility_60d = float(ranking["volatility_60d"].median())
    current_drawdown_252d = float(ranking["drawdown_252d"].mean())
    current_negative_breadth_60d = float((ranking["return_60d"] < 0).mean())

    if pressure_panel.empty:
        return_pressure = clamp01(-current_return_120d / 0.25)
        volatility_pressure = clamp01(current_volatility_60d / 0.45)
        drawdown_pressure = clamp01(-current_drawdown_252d / 0.35)
        breadth_pressure = current_negative_breadth_60d
        history_rows = 0
    else:
        unique = pressure_panel.drop_duplicates("trade_date").copy()
        market_return_col = "market_return_120d_y" if "market_return_120d_y" in unique.columns else "market_return_120d_x"
        market_vol_col = "market_volatility_60d_y" if "market_volatility_60d_y" in unique.columns else "market_volatility_60d_x"
        return_pressure = percentile_rank((-unique[market_return_col]).dropna(), -current_return_120d)
        volatility_pressure = percentile_rank(unique[market_vol_col].dropna(), current_volatility_60d)
        drawdown_pressure = percentile_rank((-unique["market_drawdown_252d"]).dropna(), -current_drawdown_252d)
        breadth_pressure = percentile_rank(unique["negative_breadth_60d"].dropna(), current_negative_breadth_60d)
        history_rows = int(len(unique))

    current_market_stress_score = (
        0.35 * return_pressure + 0.25 * volatility_pressure + 0.25 * drawdown_pressure + 0.15 * breadth_pressure
    )
    tier = "极端压力" if current_market_stress_score >= 0.80 else ("压力区" if current_market_stress_score >= 0.65 else "普通状态")
    return {
        "current_market_return_120d": current_return_120d,
        "current_market_volatility_60d": current_volatility_60d,
        "current_market_drawdown_252d": current_drawdown_252d,
        "current_negative_breadth_60d": current_negative_breadth_60d,
        "return_pressure": return_pressure,
        "volatility_pressure": volatility_pressure,
        "drawdown_pressure": drawdown_pressure,
        "breadth_pressure": breadth_pressure,
        "current_market_stress_score": current_market_stress_score,
        "current_pressure_tier": tier,
        "historical_pressure_dates": history_rows,
    }


def build_current_fundamental_pressure_panel(
    *,
    ranking: pd.DataFrame,
    valuation_snapshot: pd.DataFrame,
    valuation_inventory: pd.DataFrame,
    current_context: dict[str, Any],
    min_pit_snapshots: int,
) -> pd.DataFrame:
    frame = ranking.copy()
    snapshot_date = valuation_snapshot.attrs.get("snapshot_date", "")
    if not valuation_snapshot.empty:
        frame = frame.merge(valuation_snapshot, on="industry_code", how="left")
    else:
        for col in [
            "snapshot_industry_name",
            "snapshot_parent_industry",
            "constituent_count_snapshot",
            "pe_static_snapshot",
            "pe_ttm_snapshot",
            "pb_snapshot",
            "dividend_yield_snapshot",
        ]:
            frame[col] = np.nan

    frame["has_current_valuation"] = frame["pe_ttm_snapshot"].notna() | frame["pb_snapshot"].notna() | frame["dividend_yield_snapshot"].notna()
    frame["valuation_snapshot_date"] = snapshot_date
    frame["valuation_snapshot_count"] = int(len(valuation_inventory))
    frame["current_valuation_pit_status"] = (
        "pit_ready" if len(valuation_inventory) >= min_pit_snapshots else "current_snapshot_only_not_pit"
    )

    pe_source = frame["pe_ttm_snapshot"].where(frame["pe_ttm_snapshot"].notna(), frame.get("pe_ttm"))
    pb_source = frame["pb_snapshot"].where(frame["pb_snapshot"].notna(), frame.get("pb"))
    dy_source = frame["dividend_yield_snapshot"].where(frame["dividend_yield_snapshot"].notna(), frame.get("dividend_yield"))
    frame["pe_ttm_current"] = pe_source
    frame["pb_current"] = pb_source
    frame["dividend_yield_current"] = dy_source

    frame["pe_cheapness_score"] = inverse_rank_positive(frame["pe_ttm_current"])
    frame["pb_cheapness_score"] = inverse_rank_positive(frame["pb_current"])
    frame["dividend_support_score"] = pct_rank(frame["dividend_yield_current"], ascending=True)
    frame["valuation_snapshot_score"] = (
        0.45 * frame["pe_cheapness_score"].fillna(0.0)
        + 0.35 * frame["pb_cheapness_score"].fillna(0.0)
        + 0.20 * frame["dividend_support_score"].fillna(0.0)
    )
    if "industry_valuation_score" in frame.columns:
        frame["valuation_score_blended"] = 0.65 * frame["valuation_snapshot_score"].fillna(0.0) + 0.35 * frame[
            "industry_valuation_score"
        ].fillna(0.0)
    else:
        frame["valuation_score_blended"] = frame["valuation_snapshot_score"]

    for window in [20, 60, 120, 252]:
        col = f"return_{window}d"
        frame[f"relative_return_{window}d_current"] = frame[col] - frame[col].mean()

    frame["recovery_quality_score_current"] = pct_rank(
        0.60 * frame["return_20d"].fillna(0.0) + 0.40 * frame["relative_return_20d_current"].fillna(0.0),
        ascending=True,
    )
    frame["liquidity_quality_score_current"] = pct_rank(np.log1p(frame["avg_amount_60d"].clip(lower=0).fillna(0.0)), ascending=True)
    frame["low_volatility_quality_score_current"] = pct_rank(frame["volatility_60d"], ascending=False)
    frame["drawdown_quality_score_current"] = pct_rank(frame["drawdown_252d"], ascending=True)
    frame["relative_trend_quality_score_current"] = pct_rank(
        0.50 * frame["relative_return_120d_current"].fillna(0.0)
        + 0.30 * frame["relative_return_60d_current"].fillna(0.0)
        + 0.20 * frame["relative_return_252d_current"].fillna(0.0),
        ascending=True,
    )
    frame["price_quality_composite_current"] = (
        0.30 * frame["recovery_quality_score_current"].fillna(0.0)
        + 0.25 * frame["liquidity_quality_score_current"].fillna(0.0)
        + 0.20 * frame["low_volatility_quality_score_current"].fillna(0.0)
        + 0.15 * frame["relative_trend_quality_score_current"].fillna(0.0)
        + 0.10 * frame["drawdown_quality_score_current"].fillna(0.0)
    )
    oversold = frame.get("industry_oversold_score")
    if oversold is None:
        oversold = 0.55 * inverse_rank(frame["return_60d"]) + 0.45 * inverse_rank(frame["drawdown_252d"])
    frame["oversold_score_current"] = oversold

    persistent_weakness = (
        (frame["relative_return_20d_current"] < 0)
        & (frame["relative_return_60d_current"] < 0)
        & (frame["relative_return_120d_current"] < 0)
    )
    no_recovery = (frame["return_20d"] < 0) & (frame["relative_return_20d_current"] < 0)
    high_volatility = pct_rank(frame["volatility_60d"], ascending=True) >= 0.80
    bad_valuation = (frame["pe_ttm_current"] <= 0) | (frame["pe_ttm_current"] > 100) | (frame["pb_current"] > 6)
    no_dividend = frame["dividend_yield_current"].fillna(0.0) <= 0
    thin_constituents = frame["constituent_count_snapshot"].fillna(10) <= 5
    deep_drawdown_no_recovery = (frame["drawdown_252d"] <= -0.35) & no_recovery
    frame["value_trap_proxy_score"] = (
        0.24 * bad_valuation.astype(float)
        + 0.20 * persistent_weakness.astype(float)
        + 0.16 * no_recovery.astype(float)
        + 0.16 * high_volatility.astype(float)
        + 0.12 * no_dividend.astype(float)
        + 0.07 * thin_constituents.astype(float)
        + 0.05 * deep_drawdown_no_recovery.astype(float)
    ).clip(upper=1.0)
    frame["current_market_stress_score"] = current_context["current_market_stress_score"]
    frame["current_pressure_tier"] = current_context["current_pressure_tier"]
    frame["fundamental_pressure_score_raw"] = (
        0.32 * frame["valuation_score_blended"].fillna(0.0)
        + 0.22 * frame["oversold_score_current"].fillna(0.0)
        + 0.18 * frame["price_quality_composite_current"].fillna(0.0)
        + 0.10 * frame["dividend_support_score"].fillna(0.0)
        + 0.08 * frame["liquidity_quality_score_current"].fillna(0.0)
        + 0.10 * frame["current_market_stress_score"].fillna(0.0)
        - 0.18 * frame["value_trap_proxy_score"].fillna(0.0)
    )
    frame["current_fundamental_pressure_score"] = frame["fundamental_pressure_score_raw"].clip(lower=0.0, upper=1.0)
    frame["current_fundamental_pressure_rank"] = frame["current_fundamental_pressure_score"].rank(ascending=False, method="first").astype(int)

    statuses: list[str] = []
    reasons: list[str] = []
    for row in frame.to_dict("records"):
        status, reason = classify_current_candidate(row, min_pit_snapshots)
        statuses.append(status)
        reasons.append(reason)
    frame["candidate_status"] = statuses
    frame["decision_reason"] = reasons
    return frame.sort_values("current_fundamental_pressure_score", ascending=False).reset_index(drop=True)


def classify_current_candidate(row: dict[str, Any], min_pit_snapshots: int) -> tuple[str, str]:
    reasons: list[str] = []
    has_valuation = bool(row.get("has_current_valuation"))
    if not has_valuation:
        return "blocked_no_current_valuation", "缺少当前估值快照"

    valuation_ok = safe_number(row.get("valuation_score_blended")) >= 0.62
    oversold_ok = safe_number(row.get("oversold_score_current")) >= 0.55
    quality_ok = safe_number(row.get("price_quality_composite_current")) >= 0.45
    trap_ok = safe_number(row.get("value_trap_proxy_score")) <= 0.45
    pressure_ok = safe_number(row.get("current_market_stress_score")) >= 0.55

    if valuation_ok:
        reasons.append("估值分较高")
    else:
        reasons.append("估值分不足")
    if oversold_ok:
        reasons.append("价格处于超跌区")
    else:
        reasons.append("超跌证据不足")
    if quality_ok:
        reasons.append("价格质量代理可接受")
    else:
        reasons.append("价格质量代理偏弱")
    if not trap_ok:
        reasons.append("价值陷阱代理偏高")
    if not pressure_ok:
        reasons.append("当前市场压力未达到强压力区")
    if int(row.get("valuation_snapshot_count") or 0) < min_pit_snapshots:
        reasons.append("估值快照不足以做PIT历史验证")

    if valuation_ok and oversold_ok and quality_ok and trap_ok:
        status = "current_snapshot_candidate_not_pit_validated"
    elif valuation_ok and not oversold_ok:
        status = "valuation_watchlist_not_oversold"
    elif oversold_ok and not valuation_ok:
        status = "oversold_without_valuation_support"
    else:
        status = "research_watchlist"
    return status, "；".join(reasons)


def build_historical_signal_evidence(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, encoding="utf-8-sig")
    keep = [
        "strategy_id",
        "strategy_zh",
        "top_n",
        "horizon",
        "mean_relative_return",
        "oos_mean_relative_return",
        "nonoverlap_mean_relative_return",
        "bootstrap_ci_5",
        "bootstrap_probability_positive",
        "relative_final_nav",
        "nav_daily_rows",
        "sample_strength",
        "samples",
        "nonoverlap_samples",
        "oos_samples",
        "robust_score",
    ]
    frame = frame[[col for col in keep if col in frame.columns]].copy()
    for col in frame.columns:
        if col not in {"strategy_id", "strategy_zh"}:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["evidence_status"] = frame.apply(classify_historical_evidence, axis=1)
    return frame.sort_values("robust_score", ascending=False).head(30)


def classify_historical_evidence(row: pd.Series) -> str:
    if safe_number(row.get("sample_strength")) < 0.50:
        return "conditional_thin_sample"
    checks = [
        safe_number(row.get("mean_relative_return")) > 0,
        safe_number(row.get("oos_mean_relative_return")) > 0,
        safe_number(row.get("nonoverlap_mean_relative_return")) > 0,
        safe_number(row.get("bootstrap_ci_5")) > 0,
        safe_number(row.get("relative_final_nav")) > 1.0,
    ]
    return "historically_supportive" if all(checks) else "mixed_or_rejected"


def build_pit_readiness_audit(
    *,
    valuation_inventory: pd.DataFrame,
    valuation_snapshot: pd.DataFrame,
    current_panel: pd.DataFrame,
    min_pit_snapshots: int,
) -> pd.DataFrame:
    snapshot_count = len(valuation_inventory)
    covered = int(current_panel["has_current_valuation"].sum()) if not current_panel.empty else 0
    total = int(len(current_panel))
    return pd.DataFrame(
        [
            {
                "check_id": "valuation_snapshot_count",
                "status": "pass" if snapshot_count >= min_pit_snapshots else "blocked",
                "observed": snapshot_count,
                "required": min_pit_snapshots,
                "note": "估值快照数量不足时不能验证估值因子的历史有效性",
            },
            {
                "check_id": "current_valuation_coverage",
                "status": "pass" if total and covered / total >= 0.95 else "warning",
                "observed": covered,
                "required": total,
                "note": "当前快照覆盖当前行业池",
            },
            {
                "check_id": "no_current_valuation_backfill",
                "status": "pass",
                "observed": valuation_snapshot.attrs.get("snapshot_date", ""),
                "required": "no_history_backfill",
                "note": "当前估值只用于当前解释，不回填到历史特征",
            },
            {
                "check_id": "historical_price_evidence_boundary",
                "status": "pass",
                "observed": "V2.3 price-only validation",
                "required": "separate_from_current_valuation",
                "note": "历史验证仍然只使用PIT价格和成交额字段",
            },
        ]
    )


def build_valuation_snapshot_coverage(
    valuation_snapshot: pd.DataFrame, current_panel: pd.DataFrame, valuation_inventory: pd.DataFrame
) -> pd.DataFrame:
    snapshot_date = valuation_snapshot.attrs.get("snapshot_date", "")
    total = int(len(current_panel))
    covered = int(current_panel["has_current_valuation"].sum()) if not current_panel.empty else 0
    rows = [
        {
            "snapshot_date": snapshot_date,
            "snapshot_count": int(len(valuation_inventory)),
            "current_universe_rows": total,
            "covered_rows": covered,
            "coverage_ratio": covered / total if total else math.nan,
            "pe_ttm_non_null": int(current_panel["pe_ttm_current"].notna().sum()) if not current_panel.empty else 0,
            "pb_non_null": int(current_panel["pb_current"].notna().sum()) if not current_panel.empty else 0,
            "dividend_non_null": int(current_panel["dividend_yield_current"].notna().sum()) if not current_panel.empty else 0,
        }
    ]
    return pd.DataFrame(rows)


def build_candidate_decision_log(current_panel: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "industry_code",
        "industry_name",
        "parent_industry",
        "candidate_status",
        "decision_reason",
        "current_fundamental_pressure_score",
        "valuation_score_blended",
        "oversold_score_current",
        "price_quality_composite_current",
        "value_trap_proxy_score",
        "current_market_stress_score",
        "current_pressure_tier",
        "pe_ttm_current",
        "pb_current",
        "dividend_yield_current",
        "return_60d",
        "drawdown_252d",
        "current_valuation_pit_status",
    ]
    return current_panel[[col for col in cols if col in current_panel.columns]].copy()


def build_top_candidates(current_panel: pd.DataFrame, candidate_count: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(current_panel.head(candidate_count).to_dict("records"), start=1):
        rows.append(
            {
                "排名": index,
                "行业代码": row["industry_code"],
                "行业": row.get("industry_name", ""),
                "上级行业": row.get("parent_industry", ""),
                "状态": translate_candidate_status(row.get("candidate_status", "")),
                "V2.4综合分": fmt_float(row.get("current_fundamental_pressure_score"), 3),
                "估值分": fmt_pct(row.get("valuation_score_blended")),
                "超跌分": fmt_pct(row.get("oversold_score_current")),
                "价格质量分": fmt_pct(row.get("price_quality_composite_current")),
                "陷阱风险": fmt_pct(row.get("value_trap_proxy_score")),
                "PE_TTM": fmt_float(row.get("pe_ttm_current"), 2),
                "PB": fmt_float(row.get("pb_current"), 2),
                "股息率": fmt_pct(row.get("dividend_yield_current")),
                "60日收益": fmt_pct(row.get("return_60d")),
                "120日收益": fmt_pct(row.get("return_120d")),
                "252日回撤": fmt_pct(row.get("drawdown_252d")),
                "当前压力分": fmt_float(row.get("current_market_stress_score"), 3),
                "压力状态": row.get("current_pressure_tier", ""),
                "PIT状态": translate_pit_status(row.get("current_valuation_pit_status", "")),
                "说明": row.get("decision_reason", ""),
            }
        )
    return pd.DataFrame(rows)


def render_report(
    *,
    summary: dict[str, Any],
    top_candidates: pd.DataFrame,
    pit_audit: pd.DataFrame,
    coverage: pd.DataFrame,
    historical_evidence: pd.DataFrame,
    current_context: dict[str, Any],
    decision_log: pd.DataFrame,
) -> str:
    lines = [
        "# V2.4 行业估值压力候选研究报告",
        "",
        f"版本：{VERSION}",
        "",
        "## 研究结论",
        "",
        summary["research_boundary"],
        "",
        f"- 估值快照日期：{summary['valuation_snapshot_date']}",
        f"- 估值快照数量：{summary['valuation_snapshot_count']}",
        f"- PIT估值状态：{translate_pit_status(summary['pit_valuation_status'])}",
        f"- 当前行业行数：{summary['current_industry_rows']}",
        f"- 当前估值覆盖行数：{summary['valuation_covered_rows']}",
        f"- 当前观察候选数：{summary['current_observation_candidates']}",
        f"- 当前市场压力：{summary['current_pressure_tier']}（{summary['current_market_stress_score']:.3f}）",
        "",
        "V2.4 不是历史估值回测版本。当前估值快照只有当前可见日期，不能回填到 2001-2025 的历史特征里。报告中的候选只能用于当前研究观察和后续每日归档，不代表 alpha 已验证。",
        "",
        "## 当前市场压力",
        "",
        render_context_table(current_context),
        "",
        "## 当前估值压力候选",
        "",
    ]
    lines.extend(render_markdown_table(top_candidates.head(15)))

    lines.extend(["", "## PIT 准备度审计", ""])
    lines.extend(render_markdown_table(pit_audit))

    lines.extend(["", "## 估值快照覆盖", ""])
    display_coverage = coverage.copy()
    if "coverage_ratio" in display_coverage.columns:
        display_coverage["coverage_ratio"] = display_coverage["coverage_ratio"].map(fmt_pct)
    lines.extend(render_markdown_table(display_coverage))

    lines.extend(["", "## 历史价格证据边界", ""])
    if historical_evidence.empty:
        lines.append("未读取到 V2.3 历史价格验证结果。")
    else:
        display = historical_evidence.head(10).copy()
        display = display[
            [
                "strategy_zh",
                "top_n",
                "horizon",
                "mean_relative_return",
                "oos_mean_relative_return",
                "nonoverlap_mean_relative_return",
                "bootstrap_ci_5",
                "relative_final_nav",
                "sample_strength",
                "evidence_status",
            ]
        ].rename(
            columns={
                "strategy_zh": "策略",
                "top_n": "TopN",
                "horizon": "持有期",
                "mean_relative_return": "全样本相对收益",
                "oos_mean_relative_return": "样本外相对收益",
                "nonoverlap_mean_relative_return": "非重叠相对收益",
                "bootstrap_ci_5": "Bootstrap下沿",
                "relative_final_nav": "逐日相对净值",
                "sample_strength": "样本强度",
                "evidence_status": "证据状态",
            }
        )
        for col in ["全样本相对收益", "样本外相对收益", "非重叠相对收益", "Bootstrap下沿", "样本强度"]:
            display[col] = display[col].map(fmt_pct)
        display["逐日相对净值"] = display["逐日相对净值"].map(lambda value: fmt_float(value, 3))
        lines.extend(render_markdown_table(display))

    lines.extend(["", "## 决策状态分布", ""])
    if decision_log.empty:
        lines.append("无决策日志。")
    else:
        status_counts = (
            decision_log.groupby("candidate_status", dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
        )
        status_counts["状态"] = status_counts["candidate_status"].map(translate_candidate_status)
        lines.extend(render_markdown_table(status_counts[["状态", "count"]].rename(columns={"count": "数量"})))

    lines.extend(
        [
            "",
            "## 复现文件",
            "",
            "- `debug/current_fundamental_pressure_panel.csv`",
            "- `debug/valuation_snapshot_coverage.csv`",
            "- `debug/historical_signal_evidence.csv`",
            "- `debug/pit_readiness_audit.csv`",
            "- `debug/candidate_decision_log.csv`",
            "- `debug/current_market_pressure_context.json`",
            "",
        ]
    )
    return "\n".join(lines)


def render_context_table(context: dict[str, Any]) -> str:
    rows = [
        ["120日市场收益", fmt_pct(context.get("current_market_return_120d"))],
        ["60日市场波动", fmt_pct(context.get("current_market_volatility_60d"))],
        ["252日市场回撤", fmt_pct(context.get("current_market_drawdown_252d"))],
        ["60日负收益行业占比", fmt_pct(context.get("current_negative_breadth_60d"))],
        ["市场压力分", fmt_float(context.get("current_market_stress_score"), 3)],
        ["压力状态", str(context.get("current_pressure_tier", ""))],
    ]
    return "\n".join(["| 指标 | 数值 |", "| --- | --- |"] + [f"| {key} | {value} |" for key, value in rows])


def render_markdown_table(frame: pd.DataFrame) -> list[str]:
    if frame.empty:
        return ["无结果。"]
    columns = list(frame.columns)
    rows = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for record in frame.to_dict("records"):
        rows.append("| " + " | ".join(str(record.get(column, "")) for column in columns) + " |")
    return rows


def percentile_rank(series: pd.Series, value: float) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty or math.isnan(value):
        return 0.0
    return float((clean <= value).mean())


def pct_rank(series: pd.Series, ascending: bool) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.rank(pct=True, method="average", ascending=ascending)


def inverse_rank_positive(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    valid = numeric.where(numeric > 0)
    return valid.rank(pct=True, method="average", ascending=False)


def inverse_rank(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").rank(pct=True, method="average", ascending=False)


def clamp01(value: float) -> float:
    if math.isnan(value):
        return 0.0
    return min(max(float(value), 0.0), 1.0)


def safe_number(value: Any) -> float:
    try:
        if value is None or str(value).strip() == "":
            return 0.0
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) else number


def fmt_pct(value: Any) -> str:
    number = safe_number(value)
    return f"{number * 100:.2f}%"


def fmt_float(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(number):
        return ""
    return f"{number:.{digits}f}"


def translate_candidate_status(status: str) -> str:
    return {
        "current_snapshot_candidate_not_pit_validated": "当前快照候选，未PIT验证",
        "valuation_watchlist_not_oversold": "估值观察，未超跌",
        "oversold_without_valuation_support": "超跌但估值支持不足",
        "research_watchlist": "研究观察",
        "blocked_no_current_valuation": "缺少当前估值",
    }.get(str(status), str(status))


def translate_pit_status(status: str) -> str:
    return {
        "pit_ready": "PIT估值样本就绪",
        "current_snapshot_only_not_pit": "仅当前快照，未PIT验证",
    }.get(str(status), str(status))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    def default(obj: Any) -> Any:
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, pd.Timestamp):
            return obj.strftime("%Y-%m-%d")
        return str(obj)

    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=default)


if __name__ == "__main__":
    main()
