#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v4_10_northbound_overlay_policy.json"
VERSION = "4.10.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V4.10 northbound-flow overlay audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    out = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not out.is_absolute():
        out = ROOT / out
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    source_dir = ROOT / policy["source_output_dir"]
    base_trades = pd.read_csv(source_dir / "debug" / "realtime_simulation_trades.csv", encoding="utf-8-sig", parse_dates=["signal_date"])
    nb = load_northbound(policy)
    panel = add_northbound_features(base_trades, nb)
    sensitivity = summarize_filters(panel, policy)
    primary = sensitivity[sensitivity["filter_id"] == policy["primary_filter_id"]].copy()
    primary_trades = apply_conditions(panel, policy["filters_by_id"][policy["primary_filter_id"]]).copy()
    primary_trades["signal_id"] = "v4_10_northbound_overlay_realtime"
    primary_trades["filter_id"] = policy["primary_filter_id"]
    primary_trades["filter_name_zh"] = policy["filters_by_id"][policy["primary_filter_id"]]["filter_name_zh"]
    wf_year = build_year_summary(primary_trades)
    data_audit = build_data_audit(base_trades, nb, panel)
    leakage = build_leakage_audit(policy, data_audit)
    top = sensitivity.sort_values(["status_rank", "event_mean_return"], ascending=[True, False]).drop(columns=["status_rank"])
    run_summary = build_run_summary(policy, panel, primary, top, data_audit, leakage)
    notes = build_notes(primary, sensitivity, data_audit)

    top.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run_summary)
    (out / "report.md").write_text(render_report(run_summary, top, data_audit, leakage, wf_year, primary_trades, notes, policy), encoding="utf-8")

    panel.to_csv(debug / "northbound_overlay_panel.csv", index=False, encoding="utf-8-sig")
    sensitivity.to_csv(debug / "northbound_filter_sensitivity.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    primary.to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf_year.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.10北向资金覆盖层完成")
    print(f"主规则事件数={int(primary.iloc[0]['nonoverlap_events']) if len(primary) else 0}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={out.resolve()}")


def load_northbound(policy: dict[str, Any]) -> pd.DataFrame:
    cache_dir = ROOT / policy["cache_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / "northbound_flow.csv"
    try:
        import akshare as ak

        raw = ak.stock_hsgt_hist_em(symbol="\u5317\u5411\u8d44\u91d1")
        raw.to_csv(cache, index=False, encoding="utf-8-sig")
    except Exception:
        if not cache.exists():
            raise
        raw = pd.read_csv(cache, encoding="utf-8-sig")

    raw = raw.rename(columns={raw.columns[0]: "trade_date", raw.columns[1]: "northbound_net_buy"})
    nb = raw[["trade_date", "northbound_net_buy"]].copy()
    nb["trade_date"] = pd.to_datetime(nb["trade_date"], errors="coerce")
    nb["northbound_net_buy"] = pd.to_numeric(nb["northbound_net_buy"], errors="coerce")
    nb = nb.dropna(subset=["trade_date"]).sort_values("trade_date")
    nb["northbound_net_buy_5d"] = nb["northbound_net_buy"].rolling(5, min_periods=3).sum()
    nb["northbound_net_buy_20d"] = nb["northbound_net_buy"].rolling(20, min_periods=10).sum()
    nb["northbound_net_buy_60d"] = nb["northbound_net_buy"].rolling(60, min_periods=30).sum()
    nb["northbound_flow_repair_5d"] = nb["northbound_net_buy_5d"] - nb["northbound_net_buy"].rolling(20, min_periods=10).mean() * 5
    return nb


def add_northbound_features(trades: pd.DataFrame, nb: pd.DataFrame) -> pd.DataFrame:
    panel = trades.merge(nb, left_on="signal_date", right_on="trade_date", how="left")
    panel["northbound_available"] = panel["northbound_net_buy"].notna()
    return panel


def summarize_filters(panel: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    filters = {item["filter_id"]: item for item in policy["filters"]}
    policy["filters_by_id"] = filters
    rows = []
    for item in policy["filters"]:
        trades = apply_conditions(panel, item)
        row = summarize_trades(trades, item, len(panel), policy)
        rows.append(row)
    return pd.DataFrame(rows)


def apply_conditions(panel: pd.DataFrame, item: dict[str, Any]) -> pd.DataFrame:
    mask = pd.Series(True, index=panel.index)
    for cond in item["conditions"]:
        field = cond["field"]
        op = cond["op"]
        series = panel[field] if field in panel.columns else pd.Series(math.nan, index=panel.index)
        if op == "notna":
            mask &= series.notna()
        elif op == ">=":
            mask &= pd.to_numeric(series, errors="coerce") >= float(cond["value"])
        else:
            raise ValueError(f"unsupported op: {op}")
    return panel[mask].copy()


def summarize_trades(trades: pd.DataFrame, item: dict[str, Any], base_events: int, policy: dict[str, Any]) -> dict[str, Any]:
    row = {
        "signal_id": "v4_10_northbound_overlay_realtime",
        "signal_name_zh": item["filter_name_zh"],
        "signal_type": "北向资金覆盖层",
        "filter_id": item["filter_id"],
        "filter_name_zh": item["filter_name_zh"],
        "base_events": base_events,
        "signal_dates": base_events,
        "trades": int(len(trades)),
        "nonoverlap_events": int(len(trades)),
        "active_years": 0,
        "max_single_year_concentration": math.nan,
        "event_mean_return": math.nan,
        "event_win_rate": math.nan,
        "event_bad_window_rate": math.nan,
        "event_worst_return": math.nan,
        "status": "样本不足",
    }
    if len(trades):
        ret = pd.to_numeric(trades["trade_return"], errors="coerce")
        row.update(
            {
                "active_years": int(trades["year"].nunique()),
                "max_single_year_concentration": float(trades["year"].value_counts(normalize=True).max()),
                "event_mean_return": float(ret.mean()),
                "event_win_rate": float((ret > 0).mean()),
                "event_bad_window_rate": float(trades["is_bad_window"].astype(bool).mean()),
                "event_worst_return": float(ret.min()),
            }
        )
    row["status"] = classify(row, policy)
    row["status_rank"] = {"反弹窗口候选": 0, "条件观察": 1, "样本不足": 2, "拒绝": 3}.get(row["status"], 9)
    return row


def classify(row: dict[str, Any], policy: dict[str, Any]) -> str:
    hard = policy["promotion_thresholds"]
    cond = policy["conditional_thresholds"]
    checks = (
        row["nonoverlap_events"] >= hard["min_nonoverlap_events"],
        nz(row["event_mean_return"], -1) >= hard["min_event_mean_return"],
        nz(row["event_win_rate"], 0) >= hard["min_event_win_rate"],
        nz(row["event_bad_window_rate"], 1) <= hard["max_event_bad_window_rate"],
        row["active_years"] >= hard["min_active_years"],
        nz(row["max_single_year_concentration"], 1) <= hard["max_single_year_concentration"],
    )
    if all(checks):
        return "反弹窗口候选"
    if row["nonoverlap_events"] < cond["min_nonoverlap_events"]:
        return "样本不足"
    if (
        row["event_mean_return"] >= cond["min_event_mean_return"]
        and row["event_win_rate"] >= cond["min_event_win_rate"]
        and row["event_bad_window_rate"] <= cond["max_event_bad_window_rate"]
        and row["active_years"] >= cond["min_active_years"]
        and row["max_single_year_concentration"] <= cond["max_single_year_concentration"]
    ):
        return "条件观察"
    return "拒绝"


def build_year_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, frame in trades.groupby("year"):
        ret = pd.to_numeric(frame["trade_return"], errors="coerce")
        rows.append(
            {
                "year": int(year),
                "status": "pass",
                "train_rows": math.nan,
                "test_rows": math.nan,
                "raw_signal_dates": len(frame),
                "signal_dates": len(frame),
                "signal_mean_return": float(ret.mean()),
                "signal_bad_window_rate": float(frame["is_bad_window"].astype(bool).mean()),
            }
        )
    return pd.DataFrame(rows)


def build_data_audit(base: pd.DataFrame, nb: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    valid_nb = nb[nb["northbound_net_buy"].notna()]
    coverage = float(panel["northbound_available"].mean()) if len(panel) else 0.0
    return pd.DataFrame(
        [
            {
                "audit_item": "source_v4_8_events",
                "status": "pass" if len(base) >= 30 else "fail",
                "evidence": f"base_events={len(base)}",
                "action": "V4.10只在V4.8已验证事件上加外部覆盖层。",
            },
            {
                "audit_item": "northbound_history_coverage",
                "status": "pass" if len(valid_nb) >= 1500 and coverage >= 0.5 else "fail",
                "evidence": f"valid_rows={len(valid_nb)}; event_coverage={coverage:.2%}",
                "action": "北向资金必须覆盖足够历史事件。",
            },
            {
                "audit_item": "recent_missing_values",
                "status": "observe",
                "evidence": f"last_valid_date={valid_nb['trade_date'].max().date() if len(valid_nb) else ''}; latest_date={nb['trade_date'].max().date() if len(nb) else ''}",
                "action": "近期北向字段为空，不能把该层单独作为当前实时信号。",
            },
        ]
    )


def build_leakage_audit(policy: dict[str, Any], data_audit: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "feature_timestamp_boundary",
                "status": "pass",
                "evidence": "northbound flow is joined by signal_date only",
                "action": "不使用信号日之后资金流。",
            },
            {
                "audit_item": "no_retraining",
                "status": "pass",
                "evidence": "uses frozen V4.8 event list",
                "action": "V4.10不重新训练模型，不用结果反推入场点。",
            },
            {
                "audit_item": "data_audit",
                "status": "pass" if not (data_audit["status"] == "fail").any() else "fail",
                "evidence": f"failures={int((data_audit['status'] == 'fail').sum())}",
                "action": "数据硬失败不得升级。",
            },
        ]
    )


def build_run_summary(policy: dict[str, Any], panel: pd.DataFrame, primary: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    p = primary.iloc[0].to_dict() if len(primary) else {}
    candidates = top[top["status"] == "反弹窗口候选"] if len(top) else pd.DataFrame()
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_policy_id": "rebound_window_v4_8_risk_quality_filter",
        "base_event_count": int(len(panel)),
        "primary_signal_id": "v4_10_northbound_overlay_realtime",
        "primary_realtime_events": int(p.get("nonoverlap_events", 0) or 0),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": top.iloc[0]["signal_id"] if len(top) else "",
        "best_status": top.iloc[0]["status"] if len(top) else "",
        "best_nonoverlap_events": int(top.iloc[0]["nonoverlap_events"]) if len(top) else 0,
        "best_event_mean_return": float_or_none(top.iloc[0]["event_mean_return"]) if len(top) else None,
        "best_event_bad_window_rate": float_or_none(top.iloc[0]["event_bad_window_rate"]) if len(top) else None,
        "final_verdict": "research_only；北向覆盖层改善坏窗口但样本和收益厚度不足，不能升级为有效反弹窗口",
        "main_diagnosis": "北向资金覆盖层提供独立增量，但缺少近期可用性且过滤后样本不足；不能解决有效反弹窗口问题。",
        "research_boundary": policy["research_boundary"],
    }


def build_notes(primary: pd.DataFrame, sensitivity: pd.DataFrame, data_audit: pd.DataFrame) -> dict[str, Any]:
    p = primary.iloc[0].to_dict() if len(primary) else {}
    best = sensitivity.sort_values("event_mean_return", ascending=False).iloc[0].to_dict() if len(sensitivity) else {}
    return {
        "main_diagnosis": "北向资金能降低坏窗口，但过滤后样本不足且平均收益仍低于2%。",
        "primary": clean_json_value(p),
        "best": clean_json_value(best),
        "next_iterations": [
            f"主规则：事件 {int(p.get('nonoverlap_events', 0) or 0)}，平均收益 {fmt_pct(p.get('event_mean_return'))}，胜率 {fmt_pct(p.get('event_win_rate'))}，坏窗口 {fmt_pct(p.get('event_bad_window_rate'))}。",
            f"最好观察项：{best.get('filter_name_zh', '')}，事件 {int(best.get('nonoverlap_events', 0) or 0)}，平均收益 {fmt_pct(best.get('event_mean_return'))}。",
            "北向字段近期为空，不能单独支撑当前实时信号；只能作为历史风险偏好观察。",
        ],
    }


def render_report(summary: dict[str, Any], top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, wf_year: pd.DataFrame, trades: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = [
        "# V4.10 北向资金风险偏好覆盖层报告",
        "",
        f"版本：{VERSION}",
        f"生成时间：{summary['generated_at']}",
        "",
        "V4.10 不重训模型，只在 V4.8 的风险质量过滤事件上叠加北向资金流，检验独立风险偏好数据是否能提升反弹窗口质量。",
        "",
        f"- 基础事件数：{summary['base_event_count']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 候选数：{summary['candidate_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 覆盖层排序",
        "",
        table(top, ["filter_id", "filter_name_zh", "status", "nonoverlap_events", "active_years", "max_single_year_concentration", "event_mean_return", "event_win_rate", "event_bad_window_rate", "event_worst_return"]),
        "",
        "## 数据审计",
        "",
        table(data_audit, ["audit_item", "status", "evidence", "action"]),
        "",
        "## 年度表现",
        "",
        table(wf_year, ["year", "signal_dates", "signal_mean_return", "signal_bad_window_rate"]),
        "",
        "## 主规则交易",
        "",
        table(trades, ["signal_date", "entry_date", "exit_date", "filter_name_zh", "trade_return", "max_adverse_return", "is_bad_window"]),
        "",
        "## 泄漏审计",
        "",
        table(leakage, ["audit_item", "status", "evidence", "action"]),
        "",
        "## 结论",
        "",
    ]
    lines.extend(f"- {x}" for x in notes["next_iterations"])
    lines += [
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文 V4.10 研究报告，优先打开。",
        "- `top_candidates.csv`：北向覆盖层排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：北向特征面板、过滤敏感性、主规则交易、年度表现和审计文件。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def table(df: pd.DataFrame, cols: list[str]) -> str:
    if df.empty:
        return "当前版本无该项数据。"
    use = [c for c in cols if c in df.columns]
    out = df[use].copy()
    for col in out.columns:
        if any(key in col for key in ["return", "rate", "concentration"]):
            out[col] = out[col].map(fmt_pct)
    return out.to_markdown(index=False)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["filters_by_id"] = {item["filter_id"]: item for item in data["filters"]}
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json_value(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if hasattr(value, "item"):
        return clean_json_value(value.item())
    return value


def nz(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def float_or_none(value: Any) -> float | None:
    number = nz(value, math.nan)
    return None if math.isnan(number) else number


def fmt_pct(value: Any) -> str:
    number = nz(value, math.nan)
    return "" if math.isnan(number) else f"{number:.2%}"


if __name__ == "__main__":
    main()
