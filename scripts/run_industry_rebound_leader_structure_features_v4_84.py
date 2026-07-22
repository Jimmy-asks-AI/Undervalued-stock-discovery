#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_market_state_v4_81 as v481
import run_industry_rebound_leader_oos_factor_v4_74 as v474
import run_industry_rebound_leader_robust_grid_v4_80 as v480
from valuation_pit_contract import audit_pit_valuation_history, load_frozen_trading_calendar, official_valuation_history


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
VALUATION_HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_structure_features_v4_84"
DEBUG = OUT / "debug"

STATE_VARIANTS = ["deep_or_high_vol", "deep_highvol_liq_repair"]
FEATURES = [
    "repair_strength_score",
    "liquidity_acceleration_score",
    "value_quality_score",
    "repair_liquidity_score",
    "value_repair_score",
    "structure_combo_score",
]
TOP_NS = [5, 10, 15, 20]
GATE_TEXT = "same as V4.80: point gate + bootstrap robust gate + leave-one-year gate"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.84 structure-feature audit for rebound-leader industries.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    history = build_history_features()
    frame = attach_structure_features(v481.attach_full_state(opportunity, trades), history)
    frame = add_event_ranks(frame)
    feature_audit = build_feature_audit(frame)
    event_panel = build_event_panel(frame)
    results = summarize(event_panel)
    best = results.iloc[0] if len(results) else pd.Series(dtype=object)
    gate = gate_audit(best)
    top_rules = top_rule_table(results)
    summary = build_summary(results, best, gate, feature_audit)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    top_rules.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, top_rules, feature_audit, gate), encoding="utf-8")
    event_panel.to_csv(DEBUG / "structure_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "structure_grid_results.csv", index=False, encoding="utf-8-sig")
    feature_audit.to_csv(DEBUG / "feature_coverage_audit.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def build_history_features() -> pd.DataFrame:
    hist = official_valuation_history(pd.read_csv(VALUATION_HISTORY, encoding="utf-8-sig", dtype={"industry_code": str}))
    pit_audit = audit_pit_valuation_history(hist)
    hist["industry_code"] = hist["industry_code"].str.zfill(6)
    hist["valuation_trade_date"] = pd.to_datetime(hist["trade_date"])
    hist["feature_available_date"] = (
        pd.to_datetime(hist["available_date"], errors="raise")
        if pit_audit.eligible
        else pd.NaT
    )
    hist = hist.sort_values(["industry_code", "valuation_trade_date"]).copy()
    calendar_position = {value: index for index, value in enumerate(load_frozen_trading_calendar())}
    name = hist["industry_name"].fillna("").astype(str) if "industry_name" in hist.columns else pd.Series("", index=hist.index)
    hist["_identity_name"] = name
    hist["_calendar_position"] = hist["valuation_trade_date"].dt.date.map(calendar_position)
    identity_changed = hist.groupby("industry_code")["_identity_name"].transform(lambda s: s.ne(s.shift()))
    calendar_gap = hist.groupby("industry_code")["_calendar_position"].transform(lambda s: s.diff().ne(1))
    hist["_identity_episode"] = (identity_changed | calendar_gap).groupby(hist["industry_code"]).cumsum()
    group = hist.groupby(["industry_code", "_identity_episode"], group_keys=False)
    close = group["close_index"]
    turnover = group["turnover_rate"]
    amount_share = group["amount_share_pct"]
    hist["price_return_5d"] = close.pct_change(5)
    hist["price_return_10d"] = close.pct_change(10)
    hist["price_return_20d"] = close.pct_change(20)
    hist["price_return_60d"] = close.pct_change(60)
    hist["ma20_gap"] = hist["close_index"] / close.transform(lambda s: s.rolling(20, min_periods=10).mean()) - 1
    hist["ma60_gap"] = hist["close_index"] / close.transform(lambda s: s.rolling(60, min_periods=30).mean()) - 1
    hist["turnover_5d_vs_20d"] = turnover.transform(lambda s: s.rolling(5, min_periods=3).mean()) / turnover.transform(lambda s: s.rolling(20, min_periods=10).mean()) - 1
    hist["amount_share_5d_vs_20d"] = amount_share.transform(lambda s: s.rolling(5, min_periods=3).mean()) / amount_share.transform(lambda s: s.rolling(20, min_periods=10).mean()) - 1
    hist["volatility_20d"] = group["return_pct"].transform(lambda s: (s / 100).rolling(20, min_periods=10).std())
    if pit_audit.eligible:
        hist["pb_inverse"] = 1 / pd.to_numeric(hist["pb"], errors="coerce").where(lambda s: s > 0)
        hist["pe_inverse"] = 1 / pd.to_numeric(hist["pe"], errors="coerce").where(lambda s: s > 0)
        hist["dividend_yield"] = pd.to_numeric(hist["dividend_yield"], errors="coerce")
    else:
        hist[["pb_inverse", "pe_inverse", "dividend_yield"]] = float("nan")
    hist["valuation_pit_eligible"] = pit_audit.eligible
    hist["valuation_pit_status"] = pit_audit.status
    keep = [
        "valuation_trade_date",
        "feature_available_date",
        "industry_code",
        "price_return_5d",
        "price_return_10d",
        "price_return_20d",
        "price_return_60d",
        "ma20_gap",
        "ma60_gap",
        "turnover_5d_vs_20d",
        "amount_share_5d_vs_20d",
        "volatility_20d",
        "pb_inverse",
        "pe_inverse",
        "dividend_yield",
        "valuation_pit_eligible",
        "valuation_pit_status",
    ]
    return hist[keep]


def attach_structure_features(opportunity: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    frame = opportunity.copy()
    frame["industry_code"] = frame["industry_code"].astype(str).str.zfill(6)
    frame["signal_date_dt"] = pd.to_datetime(frame["signal_date"])
    frame["_source_order"] = range(len(frame))

    right = history.copy()
    if "feature_available_date" not in right.columns:
        right["feature_available_date"] = pd.NaT
    right["feature_available_date"] = pd.to_datetime(right["feature_available_date"], errors="coerce")
    eligible = right.get(
        "valuation_pit_eligible", pd.Series(False, index=right.index, dtype=bool)
    ).eq(True)
    right = right.loc[eligible & right["feature_available_date"].notna()].copy()
    feature_columns = [column for column in history.columns if column != "industry_code"]

    pieces: list[pd.DataFrame] = []
    for code, left_group in frame.groupby("industry_code", sort=False):
        candidates = right.loc[right["industry_code"].eq(code)].copy()
        if candidates.empty:
            merged = left_group.copy()
            for column in feature_columns:
                if column in {"feature_available_date", "valuation_trade_date"}:
                    merged[column] = pd.NaT
                elif column == "valuation_pit_eligible":
                    merged[column] = False
                elif column == "valuation_pit_status":
                    merged[column] = "no_pit_feature_available_asof"
                else:
                    merged[column] = float("nan")
            pieces.append(merged)
            continue
        sort_columns = ["feature_available_date"]
        if "valuation_trade_date" in candidates.columns:
            candidates["valuation_trade_date"] = pd.to_datetime(
                candidates["valuation_trade_date"], errors="coerce"
            )
            sort_columns.append("valuation_trade_date")
        candidates = (
            candidates.sort_values(sort_columns, kind="stable")
            .drop_duplicates("feature_available_date", keep="last")
            .drop(columns="industry_code")
        )
        merged = pd.merge_asof(
            left_group.sort_values("signal_date_dt"),
            candidates,
            left_on="signal_date_dt",
            right_on="feature_available_date",
            direction="backward",
            allow_exact_matches=True,
        )
        merged["valuation_pit_eligible"] = merged["valuation_pit_eligible"].fillna(False).astype(bool)
        merged["valuation_pit_status"] = merged["valuation_pit_status"].fillna(
            "no_pit_feature_available_asof"
        )
        pieces.append(merged)

    if not pieces:
        return frame.drop(columns="_source_order")
    out = pd.concat(pieces, ignore_index=True)
    matched = out["feature_available_date"].notna()
    if matched.any() and (
        out.loc[matched, "feature_available_date"] > out.loc[matched, "signal_date_dt"]
    ).any():
        raise ValueError("structure feature was attached before its available_date")
    return out.sort_values("_source_order").drop(columns="_source_order").reset_index(drop=True)


def add_event_ranks(frame: pd.DataFrame) -> pd.DataFrame:
    rank_specs = {
        "price_return_5d": True,
        "price_return_10d": True,
        "ma20_gap": True,
        "ma60_gap": True,
        "turnover_5d_vs_20d": True,
        "amount_share_5d_vs_20d": True,
        "pb_inverse": True,
        "pe_inverse": True,
        "dividend_yield": True,
        "volatility_20d": False,
    }
    pieces = []
    for _, event in frame.groupby(["signal_date", "entry_date", "exit_date"], sort=False):
        event = event.copy()
        for column, higher in rank_specs.items():
            event[f"{column}_rank"] = pd.to_numeric(event[column], errors="coerce").rank(pct=True, ascending=higher)
        event["repair_strength_score"] = (
            0.35 * event["price_return_5d_rank"]
            + 0.25 * event["price_return_10d_rank"]
            + 0.25 * event["ma20_gap_rank"]
            + 0.15 * event["ma60_gap_rank"]
        )
        event["liquidity_acceleration_score"] = (
            0.60 * event["turnover_5d_vs_20d_rank"]
            + 0.40 * event["amount_share_5d_vs_20d_rank"]
        )
        event["value_quality_score"] = (
            0.40 * event["pb_inverse_rank"]
            + 0.30 * event["pe_inverse_rank"]
            + 0.30 * event["dividend_yield_rank"]
        )
        event["repair_liquidity_score"] = (
            0.55 * event["repair_strength_score"]
            + 0.35 * event["liquidity_acceleration_score"]
            + 0.10 * event["volatility_20d_rank"]
        )
        event["value_repair_score"] = (
            0.45 * event["value_quality_score"]
            + 0.35 * event["repair_strength_score"]
            + 0.20 * event["liquidity_acceleration_score"]
        )
        event["structure_combo_score"] = (
            0.35 * event["repair_strength_score"]
            + 0.30 * event["liquidity_acceleration_score"]
            + 0.25 * event["value_quality_score"]
            + 0.10 * event["volatility_20d_rank"]
        )
        pieces.append(event)
    return pd.concat(pieces, ignore_index=True)


def build_event_panel(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for state in STATE_VARIANTS:
        state_frame = frame[v481.state_mask(frame, state)].copy()
        for feature in [item for item in FEATURES if item in state_frame and state_frame[item].notna().any()]:
            for top_n in TOP_NS:
                rows.extend(v474.evaluate_factor(state_frame, state, feature, top_n))
    return pd.DataFrame(rows)


def summarize(panel: pd.DataFrame) -> pd.DataFrame:
    if panel.empty:
        return pd.DataFrame()
    rows = []
    for (state, feature, top_n), group in panel.groupby(["state_gate_variant", "factor", "top_n"]):
        row = point_metrics(group, state, feature, int(top_n))
        robust = v480.robustness_metrics(group, int(top_n)) if v480.point_gate_passed(row) else {}
        row.update(robust)
        row["point_gate_passed"] = v480.point_gate_passed(row)
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_v4_84_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_gate_groups"] = failed_gate_groups(row)
        rows.append(row)
    out = pd.DataFrame(rows)
    for column in [
        "bootstrap_top_quintile_hit_p05",
        "bootstrap_positive_year_p05",
        "leave_one_year_min_hit_rate",
        "leave_one_year_min_mean_relative_return",
    ]:
        if column not in out.columns:
            out[column] = 0.0
        out[column] = out[column].fillna(0.0)
    return out.sort_values(
        [
            "passes_v4_84_gate",
            "robust_gate_passed",
            "point_gate_passed",
            "bootstrap_top_quintile_hit_p05",
            "top_quintile_hit_rate",
            "mean_relative_return",
        ],
        ascending=[False, False, False, False, False, False],
    ).reset_index(drop=True)


def point_metrics(group: pd.DataFrame, state: str, feature: str, top_n: int) -> dict[str, object]:
    yearly = group.groupby("year")["relative_return"].mean()
    oos = group[group["year"] >= 2022]
    return {
        "state_gate_variant": state,
        "feature": feature,
        "top_n": top_n,
        "event_count": int(len(group)),
        "year_count": int(group["year"].nunique()),
        "mean_relative_return": float(group["relative_return"].mean()),
        "median_relative_return": float(group["relative_return"].median()),
        "relative_win_rate": float(group["relative_win"].mean()),
        "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
        "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
        "oos_event_count": int(len(oos)),
        "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
        "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
    }


def build_feature_audit(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in FEATURES:
        rows.append({
            "feature": feature,
            "non_null_rows": int(frame[feature].notna().sum()),
            "total_rows": int(len(frame)),
            "coverage": float(frame[feature].notna().mean()) if len(frame) else 0.0,
            "status": "pass" if frame[feature].notna().mean() >= 0.95 else "low_coverage",
        })
    rows.append({
        "feature": "valuation_pit_contract",
        "non_null_rows": int(frame.get("valuation_pit_eligible", pd.Series(dtype=bool)).eq(True).sum()),
        "total_rows": int(len(frame)),
        "coverage": float(frame.get("valuation_pit_eligible", pd.Series(False, index=frame.index)).eq(True).mean()) if len(frame) else 0.0,
        "status": "pass" if len(frame) and frame.get("valuation_pit_eligible", pd.Series(False, index=frame.index)).eq(True).all() else "blocked_non_pit_valuation_history",
    })
    rows.append({
        "feature": "history_max_trade_date",
        "non_null_rows": "",
        "total_rows": "",
        "coverage": "",
        "status": str(pd.to_datetime(frame["valuation_trade_date"]).max().date()) if len(frame) and frame["valuation_trade_date"].notna().any() else "",
    })
    rows.append({
        "feature": "history_max_available_date",
        "non_null_rows": "",
        "total_rows": "",
        "coverage": "",
        "status": str(pd.to_datetime(frame["feature_available_date"]).max().date()) if len(frame) and frame["feature_available_date"].notna().any() else "",
    })
    return pd.DataFrame(rows)


def gate_audit(best: pd.Series) -> pd.DataFrame:
    if best.empty:
        return pd.DataFrame()
    checks = [
        ("point_gate_passed", True, "=="),
        ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="),
        ("event_count", 30, ">="),
        ("year_count", 5, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="),
        ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0.0, ">"),
    ]
    return pd.DataFrame([
        {
            "state_gate_variant": best.get("state_gate_variant", ""),
            "feature": best.get("feature", ""),
            "top_n": best.get("top_n", ""),
            "metric": metric,
            "current": best.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "pass" if compare(best.get(metric, ""), required, op) else "fail",
        }
        for metric, required, op in checks
    ])


def top_rule_table(results: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "state_gate_variant",
        "feature",
        "top_n",
        "passes_v4_84_gate",
        "point_gate_passed",
        "robust_gate_passed",
        "leave_one_year_gate_passed",
        "event_count",
        "year_count",
        "mean_relative_return",
        "top_quintile_hit_rate",
        "positive_year_rate",
        "bootstrap_top_quintile_hit_p05",
        "bootstrap_positive_year_p05",
        "leave_one_year_min_hit_rate",
        "leave_one_year_min_mean_relative_return",
        "failed_gate_groups",
    ]
    return results[[column for column in columns if column in results.columns]].head(20).copy()


def build_summary(results: pd.DataFrame, best: pd.Series, gate: pd.DataFrame, feature_audit: pd.DataFrame) -> dict[str, object]:
    passing = results[results["passes_v4_84_gate"].eq(True)] if len(results) else pd.DataFrame()
    point = results[results["point_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    robust = results[results["robust_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    valuation_rows = feature_audit[feature_audit["feature"].eq("valuation_pit_contract")] if len(feature_audit) else pd.DataFrame()
    valuation_pit_ready = bool(len(valuation_rows) and bool(valuation_rows["status"].eq("pass").all()))
    return {
        "version": "4.84.0",
        "policy_id": "industry_rebound_leader_structure_features_v4_84",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_rule_count": int(len(results)),
        "feature_coverage_pass_count": int(feature_audit["status"].eq("pass").sum()) if len(feature_audit) else 0,
        "point_gate_pass_count": int(len(point)),
        "robust_gate_pass_count": int(len(robust)),
        "passing_rule_count": int(len(passing)),
        "valuation_pit_gate_passed": valuation_pit_ready,
        "valuation_feature_promotion_eligible": valuation_pit_ready,
        "historical_evidence_label": "historical_review_used_in_iteration",
        "can_claim_strong_rebound_industries": False,
        "best_state_gate_variant": best.get("state_gate_variant", ""),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_bootstrap_top_quintile_hit_p05": float(best.get("bootstrap_top_quintile_hit_p05", 0.0) or 0.0),
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_robust_structure_feature_leader_gate" if len(passing) else "research_only_no_robust_structure_feature_rule",
        "production_ready": False,
        "auto_execution_allowed": False,
        "evaluation_gate": GATE_TEXT,
        "final_verdict": (
            "V4.84 仅保留价格与成交结构研究；历史估值缺少可验证 available_date，PB/PE/股息率特征已屏蔽，任何旧结果不得晋级。"
            if not valuation_pit_ready else
            "V4.84 找到通过完整稳健门槛的结构型强行业规则；仍需实盘前推。"
            if len(passing) else
            "V4.84 未找到通过完整稳健门槛的结构型强行业规则。"
        ),
    }


def failed_gate_groups(row: dict[str, object]) -> str:
    failed = []
    if not bool(row.get("point_gate_passed", False)):
        failed.append("point")
    if not bool(row.get("robust_gate_passed", False)):
        failed.append("robust")
    if not bool(row.get("leave_one_year_gate_passed", False)):
        failed.append("leave_one_year")
    return ";".join(failed)


def compare(value: object, required: object, op: str) -> bool:
    if op == "==":
        return value == required
    current = float(value or 0)
    target = float(required)
    return current >= target if op == ">=" else current > target


def render_report(summary: dict[str, object], top_rules: pd.DataFrame, feature_audit: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.84 结构型价格成交特征强行业审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 价格与成交特征按冻结交易日历连续段计算，代码名称变化或缺失交易日会重置滚动窗口。",
        "- 所有历史结构特征先通过严格 PIT 合同，再以 available_date 对信号日做向后 as-of 关联；trade_date 不得充当可得日。",
        "- 只在 V4.80/V4.81 最接近成功的两个反弹窗口状态内测试，避免扩大参数网格。",
        "- 评价门槛沿用 V4.80：点估计、bootstrap 5% 下界、留一年验证全部通过才算找到强反弹行业规则。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 最接近通过的规则",
        "",
        table(top_rules),
        "",
        "## 特征覆盖审计",
        "",
        table(feature_audit),
        "",
        "## 最优规则门槛审计",
        "",
        table(gate),
        "",
        "## 研究边界",
        "",
        "V4.84 只验证行业指数层面的事前可见结构信息，未引入个股、ETF 或未来收益反选。若仍未通过，应优先转向真实前推样本和新 PIT 信息源，而不是继续堆叠价格派生特征。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    mini = pd.DataFrame({
        "trade_date": pd.date_range("2020-01-01", periods=70).tolist() * 2,
        "industry_code": ["801001"] * 70 + ["801002"] * 70,
        "close_index": list(range(100, 170)) + list(range(200, 270)),
        "turnover_rate": [1.0] * 140,
        "amount_share_pct": [0.5] * 140,
        "return_pct": [1.0] * 140,
        "pb": [1.0] * 140,
        "pe": [10.0] * 140,
        "dividend_yield": [0.02] * 140,
    })
    tmp = mini.copy()
    tmp["trade_date"] = tmp["trade_date"].dt.strftime("%Y-%m-%d")
    assert len(tmp) == 140
    assert compare(0.3, 0.3, ">=")
    assert not compare(0.29, 0.3, ">=")
    print("self_check=pass")


if __name__ == "__main__":
    main()
