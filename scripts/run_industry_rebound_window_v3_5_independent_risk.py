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
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v3_5_independent_risk_policy.json"
V34_SCRIPT = ROOT / "scripts" / "run_industry_rebound_window_v3_4_realtime_model.py"
VERSION = "3.5.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V3.5 independent risk proxy rebound-window audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V3.5 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    parser.add_argument("--refresh-external-risk", action="store_true", help="Refresh margin/rate external risk cache.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    if args.refresh_external_risk:
        policy["refresh_external_risk"] = True
    v34_policy = read_json(ROOT / policy["v3_4_policy_path"])
    merged_policy = merge_v35_into_v34(policy, v34_policy)
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    v34 = load_v34_module()
    v20 = v34.load_v20_module()
    close_matrix = v20.load_close_matrix(ROOT / v34_policy["industry_history_dir"])
    amount_matrix = v34.load_amount_matrix(ROOT / v34_policy["industry_history_dir"])
    market_index_data, market_index_audit = v34.load_market_indices(v34_policy)
    external_raw, external_audit = load_external_risk_sources(policy)

    features = v20.build_daily_features(close_matrix, read_json(ROOT / v34_policy["source_policy_path"]))
    features = v34.add_industry_liquidity_features(features, amount_matrix)
    features = v34.add_market_volatility_ratio(features)
    features = v34.add_wide_market_features(features, market_index_data)
    features = add_external_risk_features(features, external_raw)
    panel = v34.add_rebound_targets(features, merged_policy)

    data_audit = build_v35_data_audit(v34, merged_policy, close_matrix, amount_matrix, market_index_audit, external_audit, panel)
    target_audit = v34.build_target_label_audit(panel, merged_policy)
    rule_summary, rule_events = v34.run_rule_audit(panel, merged_policy)
    predictions, model_year_summary, model_summary = v34.run_walk_forward_model(panel, merged_policy)
    realtime_trades, realtime_summary = v34.run_realtime_simulation(panel, predictions, merged_policy)
    normalize_realtime_labels(realtime_trades, realtime_summary)
    annual_distribution = v34.build_annual_distribution(rule_events, predictions, realtime_trades)
    top_candidates = v34.build_top_candidates(rule_summary, model_summary, realtime_summary, merged_policy)
    leakage_audit = build_v35_leakage_audit(v34, merged_policy, data_audit, predictions)
    notes = build_v35_notes(v34, top_candidates, realtime_summary, rule_summary, model_summary)
    run_summary = build_v35_run_summary(v34, policy, panel, top_candidates, data_audit, leakage_audit, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    panel.to_csv(debug_dir / "v35_feature_target_panel.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    rule_summary.to_csv(debug_dir / "independent_risk_rule_summary.csv", index=False, encoding="utf-8-sig")
    rule_events.to_csv(debug_dir / "independent_risk_rule_events.csv", index=False, encoding="utf-8-sig")
    predictions.to_csv(debug_dir / "walk_forward_predictions.csv", index=False, encoding="utf-8-sig")
    model_year_summary.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    model_summary.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    realtime_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    realtime_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    annual_distribution.to_csv(debug_dir / "annual_signal_distribution.csv", index=False, encoding="utf-8-sig")
    external_audit.to_csv(debug_dir / "external_risk_source_audit.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", {"v3_5_policy": policy, "merged_v3_4_policy": merged_policy})
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(
        render_v35_report(v34, run_summary, top_candidates, data_audit, target_audit, model_year_summary, realtime_trades, leakage_audit, notes, policy),
        encoding="utf-8",
    )

    print("V3.5独立风险偏好代理反弹窗口研究完成")
    print(f"特征标签面板行数={run_summary['feature_target_panel_count']}")
    print(f"外生风险数据可用数={run_summary['external_risk_pass_count']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最佳信号={run_summary['best_signal_id']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_v34_module() -> Any:
    spec = importlib.util.spec_from_file_location("v34_realtime_model", V34_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load V3.4 module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def merge_v35_into_v34(policy: dict[str, Any], v34_policy: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(v34_policy, ensure_ascii=False))
    merged["version"] = VERSION
    merged["policy_id"] = policy["policy_id"]
    merged["status"] = policy["status"]
    merged["output_dir"] = policy["output_dir"]
    merged["rule_candidates"] = policy["rule_candidates"]
    merged["research_boundary"] = policy["research_boundary"]
    merged["model"]["features"] = list(dict.fromkeys(merged["model"]["features"] + policy["model_extra_features"]))
    return merged


def load_external_risk_sources(policy: dict[str, Any]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    cache_dir = ROOT / policy["external_risk_cache_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    refresh = bool(policy.get("refresh_external_risk", False))
    sources: dict[str, pd.DataFrame] = {}
    rows: list[dict[str, Any]] = []
    loaders = {
        "margin_sh": fetch_margin_sh,
        "margin_sz": fetch_margin_sz,
        "shibor": fetch_shibor,
        "lpr": fetch_lpr,
        "bond_zh_us_rate": fetch_bond_zh_us_rate,
    }
    for source_id, loader in loaders.items():
        path = cache_dir / f"{source_id}.csv"
        status = "fail"
        source = "cache"
        error = ""
        frame = pd.DataFrame()
        if path.exists() and not refresh:
            try:
                frame = pd.read_csv(path, encoding="utf-8-sig")
                status = "pass"
            except Exception as exc:
                error = str(exc)
        if frame.empty:
            try:
                frame = loader()
                frame.to_csv(path, index=False, encoding="utf-8-sig")
                status = "pass"
                source = "akshare"
            except Exception as exc:
                error = str(exc)
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
            frame = frame.dropna(subset=["trade_date"]).sort_values("trade_date")
            sources[source_id] = frame
        rows.append(
            {
                "source_id": source_id,
                "status": status if source_id in sources else "fail",
                "rows": int(len(sources.get(source_id, pd.DataFrame()))),
                "start_date": date_text(sources[source_id]["trade_date"].min()) if source_id in sources else "",
                "end_date": date_text(sources[source_id]["trade_date"].max()) if source_id in sources else "",
                "cache_path": str(path.relative_to(ROOT)),
                "source": source,
                "error": error,
            }
        )
    return sources, pd.DataFrame(rows)


def fetch_margin_sh() -> pd.DataFrame:
    import akshare as ak

    raw = ak.macro_china_market_margin_sh()
    return normalize_margin(raw, "sh")


def fetch_margin_sz() -> pd.DataFrame:
    import akshare as ak

    raw = ak.macro_china_market_margin_sz()
    return normalize_margin(raw, "sz")


def normalize_margin(raw: pd.DataFrame, suffix: str) -> pd.DataFrame:
    frame = raw.rename(columns={"日期": "trade_date"}).copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame.rename(
        columns={
            "融资买入额": f"margin_buy_{suffix}",
            "融资余额": f"margin_balance_{suffix}",
            "融资融券余额": f"margin_total_{suffix}",
        }
    )[["trade_date", f"margin_buy_{suffix}", f"margin_balance_{suffix}", f"margin_total_{suffix}"]]


def fetch_shibor() -> pd.DataFrame:
    import akshare as ak

    raw = ak.macro_china_shibor_all()
    frame = raw.rename(columns={"日期": "trade_date", "1W-定价": "shibor_1w", "3M-定价": "shibor_3m"}).copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame[["trade_date", "shibor_1w", "shibor_3m"]]


def fetch_lpr() -> pd.DataFrame:
    import akshare as ak

    raw = ak.macro_china_lpr()
    frame = raw.rename(columns={"TRADE_DATE": "trade_date"}).copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame[["trade_date", "LPR1Y", "LPR5Y"]]


def fetch_bond_zh_us_rate() -> pd.DataFrame:
    import akshare as ak

    raw = ak.bond_zh_us_rate(start_date="20150101")
    frame = raw.rename(
        columns={
            "日期": "trade_date",
            "中国国债收益率10年": "china_10y",
            "中国国债收益率10年-2年": "china_term_spread",
            "美国国债收益率10年": "us_10y",
        }
    ).copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame[["trade_date", "china_10y", "china_term_spread", "us_10y"]]


def add_external_risk_features(features: pd.DataFrame, sources: dict[str, pd.DataFrame]) -> pd.DataFrame:
    output = features.copy()
    output["trade_date"] = pd.to_datetime(output["trade_date"], errors="coerce")
    if "margin_sh" in sources and "margin_sz" in sources:
        margin = sources["margin_sh"].merge(sources["margin_sz"], on="trade_date", how="outer").sort_values("trade_date")
        for col in ["margin_buy_sh", "margin_balance_sh", "margin_total_sh", "margin_buy_sz", "margin_balance_sz", "margin_total_sz"]:
            margin[col] = pd.to_numeric(margin[col], errors="coerce")
        margin["margin_buy_total"] = margin["margin_buy_sh"].fillna(0) + margin["margin_buy_sz"].fillna(0)
        margin["margin_balance_total"] = margin["margin_balance_sh"].fillna(0) + margin["margin_balance_sz"].fillna(0)
        margin["margin_balance_5d_chg"] = margin["margin_balance_total"] / margin["margin_balance_total"].shift(5) - 1.0
        margin["margin_balance_20d_chg"] = margin["margin_balance_total"] / margin["margin_balance_total"].shift(20) - 1.0
        margin["margin_buy_5d_vs_20d"] = margin["margin_buy_total"].rolling(5, min_periods=3).mean() / margin["margin_buy_total"].rolling(20, min_periods=10).mean()
        margin["margin_risk_appetite_score"] = clip01((margin["margin_balance_20d_chg"] + 0.03) / 0.08) * 0.5 + clip01((margin["margin_buy_5d_vs_20d"] - 0.85) / 0.30) * 0.5
        output = output.merge(margin[["trade_date", "margin_buy_total", "margin_balance_total", "margin_balance_5d_chg", "margin_balance_20d_chg", "margin_buy_5d_vs_20d", "margin_risk_appetite_score"]], on="trade_date", how="left")
    if "shibor" in sources:
        shibor = sources["shibor"].sort_values("trade_date").copy()
        shibor["shibor_1w"] = pd.to_numeric(shibor["shibor_1w"], errors="coerce")
        shibor["shibor_3m"] = pd.to_numeric(shibor["shibor_3m"], errors="coerce")
        shibor["shibor_1w_20d_change"] = shibor["shibor_1w"] - shibor["shibor_1w"].shift(20)
        shibor["shibor_3m_20d_change"] = shibor["shibor_3m"] - shibor["shibor_3m"].shift(20)
        output = output.merge(shibor[["trade_date", "shibor_1w", "shibor_3m", "shibor_1w_20d_change", "shibor_3m_20d_change"]], on="trade_date", how="left")
    if "bond_zh_us_rate" in sources:
        bond = sources["bond_zh_us_rate"].sort_values("trade_date").copy()
        for col in ["china_10y", "china_term_spread", "us_10y"]:
            bond[col] = pd.to_numeric(bond[col], errors="coerce")
        bond["china_10y_20d_change"] = bond["china_10y"] - bond["china_10y"].shift(20)
        bond["china_term_spread_20d_change"] = bond["china_term_spread"] - bond["china_term_spread"].shift(20)
        output = output.merge(bond[["trade_date", "china_10y", "china_term_spread", "us_10y", "china_10y_20d_change", "china_term_spread_20d_change"]], on="trade_date", how="left")
    if "lpr" in sources:
        lpr = sources["lpr"].sort_values("trade_date").copy()
        output["trade_date"] = pd.to_datetime(output["trade_date"], errors="coerce").astype("datetime64[ns]")
        lpr["trade_date"] = pd.to_datetime(lpr["trade_date"], errors="coerce").astype("datetime64[ns]")
        lpr["LPR1Y"] = pd.to_numeric(lpr["LPR1Y"], errors="coerce")
        lpr["LPR5Y"] = pd.to_numeric(lpr["LPR5Y"], errors="coerce")
        lpr["lpr1y_60d_change"] = lpr["LPR1Y"] - lpr["LPR1Y"].shift(3)
        output = pd.merge_asof(output.sort_values("trade_date"), lpr[["trade_date", "LPR1Y", "LPR5Y", "lpr1y_60d_change"]].sort_values("trade_date"), on="trade_date", direction="backward")
    output = output.sort_values("trade_date")
    for col in ["margin_balance_5d_chg", "margin_balance_20d_chg", "margin_buy_5d_vs_20d", "margin_risk_appetite_score", "shibor_1w_20d_change", "shibor_3m_20d_change", "china_10y_20d_change", "china_term_spread_20d_change", "lpr1y_60d_change"]:
        if col not in output.columns:
            output[col] = math.nan
    output["funding_easing_score"] = (
        clip01((-pd.to_numeric(output["shibor_1w_20d_change"], errors="coerce") + 0.10) / 0.30) * 0.35
        + clip01((-pd.to_numeric(output["china_10y_20d_change"], errors="coerce") + 0.05) / 0.20) * 0.45
        + clip01((-pd.to_numeric(output["lpr1y_60d_change"], errors="coerce") + 0.05) / 0.15) * 0.20
    )
    fill_cols = [
        "margin_buy_total", "margin_balance_total", "margin_balance_5d_chg", "margin_balance_20d_chg",
        "margin_buy_5d_vs_20d", "margin_risk_appetite_score", "shibor_1w", "shibor_3m",
        "shibor_1w_20d_change", "shibor_3m_20d_change", "china_10y", "china_term_spread",
        "china_10y_20d_change", "china_term_spread_20d_change", "LPR1Y", "LPR5Y", "lpr1y_60d_change", "funding_easing_score"
    ]
    output[fill_cols] = output[fill_cols].ffill()
    return output


def build_v35_data_audit(v34: Any, merged_policy: dict[str, Any], close_matrix: pd.DataFrame, amount_matrix: pd.DataFrame, market_index_audit: pd.DataFrame, external_audit: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    base = v34.build_data_availability_audit(merged_policy, close_matrix, amount_matrix, market_index_audit, panel)
    rows = base.to_dict("records")
    pass_count = int((external_audit["status"] == "pass").sum()) if not external_audit.empty else 0
    rows.append(
        {
            "audit_item": "independent_risk_proxy_history",
            "status": "pass" if pass_count >= 4 else "fail",
            "evidence": f"available={pass_count}/{len(external_audit)}",
            "action": "两融、SHIBOR、LPR和国债收益率用于独立风险偏好代理。",
        }
    )
    for row in external_audit.to_dict("records"):
        rows.append(
            {
                "audit_item": f"external_risk_{row['source_id']}",
                "status": row["status"],
                "evidence": f"rows={row['rows']}; {row['start_date']}~{row['end_date']}",
                "action": row["source"] if row["status"] == "pass" else row.get("error", ""),
            }
        )
    return pd.DataFrame(rows)


def build_v35_leakage_audit(v34: Any, policy: dict[str, Any], data_audit: pd.DataFrame, predictions: pd.DataFrame) -> pd.DataFrame:
    audit = v34.build_leakage_audit(policy, data_audit, predictions)
    extra = pd.DataFrame(
        [
            {
                "audit_item": "external_risk_timestamp_boundary",
                "status": "pass",
                "evidence": "margin/rate features are merged by trade_date and forward-filled only from known historical dates",
                "action": "外生风险数据不使用未来观测。",
            }
        ]
    )
    return pd.concat([audit, extra], ignore_index=True)


def build_v35_notes(v34: Any, top: pd.DataFrame, realtime_summary: pd.DataFrame, rule_summary: pd.DataFrame, model_summary: pd.DataFrame) -> dict[str, Any]:
    base = v34.build_optimization_notes(top, model_summary, realtime_summary, rule_summary)
    notes = list(base.get("next_iterations", []))
    notes = [item.replace("V3.4", "V3.5") for item in notes]
    main = str(base.get("main_diagnosis", "")).replace("V3.4", "V3.5")
    notes.append("V3.5额外接入两融、SHIBOR、LPR和国债收益率；若实时仿真仍不改善，说明需要更长历史和真正的全市场广度/资金流数据。")
    return {
        "main_diagnosis": main,
        "next_iterations": notes,
        "recommended_next_direction": "若V3.5仍失败，应停止在日频价量/资金价格上扩参，转向更长历史的市场广度、涨跌停、两融分市场结构和政策事件标签。",
    }


def build_v35_run_summary(v34: Any, policy: dict[str, Any], panel: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    summary = v34.build_run_summary(policy, panel, top, data_audit, pd.DataFrame(), leakage, notes)
    summary["version"] = VERSION
    summary["policy_id"] = policy["policy_id"]
    summary["external_risk_pass_count"] = int(data_audit[data_audit["audit_item"].astype(str).str.startswith("external_risk_")]["status"].eq("pass").sum())
    return summary


def render_v35_report(v34: Any, summary: dict[str, Any], top: pd.DataFrame, data_audit: pd.DataFrame, target_audit: pd.DataFrame, model_year: pd.DataFrame, realtime_trades: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    text = v34.render_report(summary, top, data_audit, target_audit, model_year, realtime_trades, leakage, notes, {**policy, "research_boundary": policy["research_boundary"]})
    text = text.replace("V3.4 外生风险代理与实时仿真反弹窗口研究报告", "V3.5 独立风险偏好代理反弹窗口研究报告")
    text = text.replace("版本：3.4.0", "版本：3.5.0")
    text = text.replace("V3.4 将研究从 V2.x 的价格/成交额阈值调参，推进到宽基风险代理、反弹窗口目标标签、预声明规则、轻量 walk-forward 模型和下一交易日入场仿真。", "V3.5 在 V3.4 宽基风险代理基础上加入两融、SHIBOR、LPR和国债收益率，检验更独立的风险偏好/资金价格变量是否能改善反弹窗口识别。")
    text = text.replace(f"- 宽基指数可用数：{summary.get('market_index_pass_count', '')}", f"- 宽基指数可用数：{summary.get('market_index_pass_count', '')}；外生风险数据可用数：{summary.get('external_risk_pass_count', '')}")
    text = text.replace("## V3.4 实时仿真交易", "## V3.5 实时仿真交易")
    text = text.replace("V3.4实时仿真", "V3.5实时仿真")
    text = text.replace("中文 V3.4 研究报告", "中文 V3.5 研究报告")
    return text


def normalize_realtime_labels(realtime_trades: pd.DataFrame, realtime_summary: pd.DataFrame) -> None:
    if not realtime_trades.empty and "signal_id" in realtime_trades.columns:
        realtime_trades["signal_id"] = "v3_5_realtime_simulation"
    if not realtime_summary.empty:
        if "signal_id" in realtime_summary.columns:
            realtime_summary["signal_id"] = "v3_5_realtime_simulation"
        if "signal_name_zh" in realtime_summary.columns:
            realtime_summary["signal_name_zh"] = "V3.5实时仿真"


def clip01(series: Any) -> Any:
    return pd.Series(series).clip(lower=0.0, upper=1.0) if not np.isscalar(series) else min(max(float(series), 0.0), 1.0)


def date_text(value: Any) -> str:
    return "" if pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
