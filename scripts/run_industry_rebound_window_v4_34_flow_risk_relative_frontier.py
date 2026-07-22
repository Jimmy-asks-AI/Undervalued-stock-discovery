#!/usr/bin/env python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from run_industry_rebound_window_v3_5_independent_risk import add_external_risk_features, load_external_risk_sources
from run_industry_rebound_window_v4_31_wide_index_state_boundary import attach_state, build_wide_index_state, normalize_trades, read_json, write_json
from run_industry_rebound_window_v4_33_relative_return_frontier import (
    build_candidates,
    build_data_audit,
    build_leakage_audit,
    build_notes,
    fmt_pct,
    render_report,
    run_summary,
    year_summary,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_34_flow_risk_relative_frontier_policy.json"
VERSION = "4.34.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    source = normalize_trades(pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig"))
    daily = build_wide_index_state(ROOT / policy["wide_index_dir"])
    enriched = attach_state(source, daily)
    enriched["trade_date"] = pd.to_datetime(enriched["signal_date_dt"], errors="coerce")
    external_sources, external_audit = load_external_risk_sources({"external_risk_cache_dir": policy["external_risk_cache_dir"]})
    enriched = add_external_risk_features(enriched, external_sources)
    enriched = add_northbound_features(enriched, ROOT / policy["northbound_cache_path"])
    enriched["relative_return_5d"] = pd.to_numeric(enriched["trade_return"], errors="coerce") - pd.to_numeric(enriched["market_return_5d"], errors="coerce")

    candidates = build_candidates(enriched, policy)
    primary = candidates.iloc[0].to_dict()
    primary_trades = apply_conditions(enriched, json.loads(primary["conditions_json"])).copy()
    primary_trades["signal_id"] = primary["signal_id"]
    primary_trades["signal_name_zh"] = primary["signal_name_zh"]
    primary_trades["signal_type"] = "flow_risk_relative_frontier_upper_bound"
    wf = year_summary(primary_trades)
    data_audit = pd.concat([build_data_audit(source, daily, enriched, candidates, policy), external_audit_rows(external_audit, enriched)], ignore_index=True)
    leakage = build_leakage_audit()
    notes = build_notes(primary, candidates, policy)
    notes["main_diagnosis"] = "V4.34 审计资金风险偏好独立特征能否改善相对市场收益上限。"
    notes["next_iterations"].append("本版已加入北向、两融、SHIBOR、LPR和国债收益率代理；仍是上限审计，不得升级为实时规则。")
    run = run_summary(policy, primary, data_audit, leakage, notes)
    run["version"] = VERSION
    run["policy_id"] = policy["policy_id"]
    run["final_verdict"] = "research_only；资金风险偏好相对收益边界仍需冻结验证"

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, notes, policy).replace("V4.33", "V4.34").replace("相对市场收益边界上限", "资金风险偏好相对收益边界上限"), encoding="utf-8")
    source.to_csv(debug / "flow_risk_source_trades.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(debug / "wide_index_daily_state.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(debug / "flow_risk_enriched_trades.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(debug / "flow_risk_frontier_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.34资金风险偏好相对收益边界审计完成")
    print(f"主边界={primary['signal_id']}")
    print(f"事件={primary['nonoverlap_events']}")
    print(f"相对收益={fmt_pct(primary['event_relative_mean_return'])}")


def add_northbound_features(frame: pd.DataFrame, path: Path) -> pd.DataFrame:
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").astype("datetime64[ns]")
    nb = pd.read_csv(path, encoding="utf-8-sig").rename(columns={"日期": "trade_date", "当日成交净买额": "northbound_net_buy"})
    nb["trade_date"] = pd.to_datetime(nb["trade_date"], errors="coerce").astype("datetime64[ns]")
    nb["northbound_net_buy"] = pd.to_numeric(nb["northbound_net_buy"], errors="coerce")
    nb = nb.dropna(subset=["trade_date"]).sort_values("trade_date")
    nb["northbound_net_buy_5d"] = nb["northbound_net_buy"].rolling(5, min_periods=3).sum()
    nb["northbound_net_buy_20d"] = nb["northbound_net_buy"].rolling(20, min_periods=10).sum()
    nb["northbound_flow_repair_5d"] = nb["northbound_net_buy_5d"] - nb["northbound_net_buy"].rolling(20, min_periods=10).mean() * 5
    return pd.merge_asof(
        frame.sort_values("trade_date"),
        nb[["trade_date", "northbound_net_buy_5d", "northbound_net_buy_20d", "northbound_flow_repair_5d"]].sort_values("trade_date"),
        on="trade_date",
        direction="backward",
        allow_exact_matches=False,
    )


def external_audit_rows(external_audit: pd.DataFrame, enriched: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    pass_count = int((external_audit["status"] == "pass").sum()) if not external_audit.empty else 0
    rows.append({"audit_item": "flow_risk_external_sources", "status": "pass" if pass_count >= 4 else "fail", "evidence": f"available={pass_count}/{len(external_audit)}", "action": "资金风险偏好特征至少需要4类外部源。"})
    for col in ["margin_risk_appetite_score", "funding_easing_score", "northbound_net_buy_20d"]:
        coverage = float(enriched[col].notna().mean()) if col in enriched else 0.0
        rows.append({"audit_item": f"feature_coverage_{col}", "status": "pass" if coverage >= 0.8 else "fail", "evidence": f"coverage={coverage:.2%}", "action": "事件级特征覆盖不足时不得升级。"})
    return pd.DataFrame(rows)


def apply_conditions(df: pd.DataFrame, conditions: list[dict[str, Any]]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for cond in conditions:
        s = pd.to_numeric(df[cond["feature"]], errors="coerce")
        mask &= s >= float(cond["threshold"]) if cond["op"] == ">=" else s <= float(cond["threshold"])
    return df[mask].copy()


if __name__ == "__main__":
    main()
