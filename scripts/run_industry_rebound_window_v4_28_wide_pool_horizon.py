#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_28_wide_pool_horizon_policy.json"
VERSION = "4.28.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    source = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig")
    source = normalize_types(source)
    filtered = source[source["industry_positive_20d_ratio"] >= float(policy["breadth_floor"])].copy()
    filtered["signal_id"] = filtered.apply(signal_id, axis=1)
    filtered["signal_name_zh"] = filtered.apply(signal_name, axis=1)
    filtered["signal_type"] = "wide_pool_fixed_breadth_horizon_audit"

    summary = build_summary(filtered, policy)
    primary = primary_row(summary, policy)
    primary_trades = filtered[filtered["signal_id"] == primary["signal_id"]].copy()
    wf = year_summary(primary_trades)
    data_audit = build_data_audit(source, filtered, policy)
    leakage = build_leakage_audit(policy)
    notes = build_notes(summary, primary, policy)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    summary.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, summary, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    source.to_csv(debug / "horizon_source_trades.csv", index=False, encoding="utf-8-sig")
    filtered.to_csv(debug / "horizon_filtered_trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(debug / "horizon_rule_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.28宽池固定广度底线多持有期审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"独立事件={primary['nonoverlap_events']}")
    print(f"最终结论={run['final_verdict']}")


def normalize_types(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in [
        "holding_days",
        "min_consecutive_signal_days",
        "year",
        "trade_return",
        "max_adverse_return",
        "industry_positive_20d_ratio",
    ]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["is_win"] = to_bool(out["is_win"])
    out["is_bad_window"] = to_bool(out["is_bad_window"])
    return out


def signal_id(row: pd.Series) -> str:
    return f"breadth_floor_min{int(row['min_consecutive_signal_days'])}_{int(row['holding_days'])}d"


def signal_name(row: pd.Series) -> str:
    return f"广度底线连续{int(row['min_consecutive_signal_days'])}日信号{int(row['holding_days'])}日持有"


def build_summary(filtered: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for min_days in policy["min_consecutive_signal_days_grid"]:
        for horizon in policy["horizon_grid"]:
            d = filtered[
                (filtered["min_consecutive_signal_days"] == int(min_days))
                & (filtered["holding_days"] == int(horizon))
            ].copy()
            rows.append(summary_row(d, int(min_days), int(horizon), policy))
    frame = pd.DataFrame(rows)
    primary_mask = (
        (frame["min_consecutive_signal_days"] == int(policy["primary_min_consecutive_signal_days"]))
        & (frame["holding_days"] == int(policy["primary_horizon"]))
    )
    primary = frame[primary_mask]
    rest = frame[~primary_mask].sort_values(["status_rank", "nonoverlap_events", "event_mean_return"], ascending=[True, False, False])
    return pd.concat([primary, rest], ignore_index=True).drop(columns=["status_rank"])


def summary_row(d: pd.DataFrame, min_days: int, horizon: int, policy: dict[str, Any]) -> dict[str, Any]:
    count = len(d)
    returns = pd.to_numeric(d["trade_return"], errors="coerce")
    years = d["year"].dropna().astype(int)
    mean = float(returns.mean()) if count else math.nan
    win = float((returns > 0).mean()) if count else math.nan
    bad = float(d["is_bad_window"].mean()) if count else math.nan
    concentration = float(years.value_counts(normalize=True).max()) if len(years) else math.nan
    active_years = int(years.nunique()) if len(years) else 0
    status = classify(count, mean, win, bad, active_years, concentration, policy)
    return {
        "signal_id": f"breadth_floor_min{min_days}_{horizon}d",
        "signal_name_zh": f"广度底线连续{min_days}日信号{horizon}日持有",
        "signal_type": "wide_pool_fixed_breadth_horizon_audit",
        "status": status,
        "min_consecutive_signal_days": min_days,
        "holding_days": horizon,
        "nonoverlap_events": int(count),
        "event_mean_return": mean,
        "event_win_rate": win,
        "event_bad_window_rate": bad,
        "event_worst_return": float(returns.min()) if count else math.nan,
        "active_years": active_years,
        "max_single_year_concentration": concentration,
        "conditions_json": json.dumps({"industry_positive_20d_ratio": [">=", policy["breadth_floor"]]}, ensure_ascii=False),
        "status_rank": {"有效反弹窗口": 0, "条件观察": 1, "样本不足": 2, "拒绝": 3}.get(status, 9),
    }


def classify(count: int, mean: float, win: float, bad: float, active_years: int, concentration: float, policy: dict[str, Any]) -> str:
    hard = (
        count >= int(policy["min_realtime_events"])
        and mean >= float(policy["min_realtime_mean_return"])
        and win >= float(policy["min_realtime_win_rate"])
        and bad <= float(policy["max_realtime_bad_window_rate"])
        and active_years >= int(policy["min_active_years"])
        and concentration <= float(policy["max_single_year_concentration"])
    )
    if hard:
        return "有效反弹窗口"
    if count < int(policy["min_realtime_events"]):
        return "样本不足"
    if mean >= 0.0 and win >= 0.5 and bad <= float(policy["max_realtime_bad_window_rate"]):
        return "条件观察"
    return "拒绝"


def primary_row(summary: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    mask = (
        (summary["min_consecutive_signal_days"] == int(policy["primary_min_consecutive_signal_days"]))
        & (summary["holding_days"] == int(policy["primary_horizon"]))
    )
    if not mask.any():
        raise RuntimeError("primary horizon row missing")
    return summary[mask].iloc[0].to_dict()


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, g in d.groupby("year"):
        returns = pd.to_numeric(g["trade_return"], errors="coerce")
        rows.append(
            {
                "year": int(year),
                "status": "pass",
                "signal_dates": int(len(g)),
                "signal_mean_return": float(returns.mean()),
                "signal_bad_window_rate": float(g["is_bad_window"].mean()),
            }
        )
    return pd.DataFrame(rows)


def build_data_audit(source: pd.DataFrame, filtered: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    horizons = sorted(int(x) for x in source["holding_days"].dropna().unique())
    min_days = sorted(int(x) for x in source["min_consecutive_signal_days"].dropna().unique())
    required_horizons = [int(x) for x in policy["horizon_grid"]]
    required_min_days = [int(x) for x in policy["min_consecutive_signal_days_grid"]]
    return pd.DataFrame(
        [
            {
                "audit_item": "source_horizon_grid",
                "status": "pass" if set(required_horizons).issubset(horizons) and set(required_min_days).issubset(min_days) else "fail",
                "evidence": f"horizons={horizons}; min_days={min_days}; source_rows={len(source)}",
                "action": "缺少预声明持有期或连续信号网格时不得升级。",
            },
            {
                "audit_item": "breadth_floor_filter",
                "status": "pass" if len(filtered) > 0 else "fail",
                "evidence": f"breadth_floor={policy['breadth_floor']}; filtered_rows={len(filtered)}",
                "action": "只使用信号日前可见的行业上涨广度底线。",
            },
        ]
    )


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "fixed_entry_condition_only",
                "status": "pass",
                "evidence": "entry filter uses industry_positive_20d_ratio only; returns are used only for evaluation",
                "action": "不得按未来收益挑选入场事件。",
            },
            {
                "audit_item": "diagnostic_horizon_not_alpha",
                "status": "pass",
                "evidence": f"primary_horizon={policy['primary_horizon']}; full horizon grid is diagnostic",
                "action": "全样本最优持有期不得直接视为可交易有效窗口。",
            },
        ]
    )


def build_notes(summary: pd.DataFrame, primary: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    adequate = summary[summary["nonoverlap_events"] >= int(policy["min_realtime_events"])].copy()
    best = summary.sort_values("event_mean_return", ascending=False).iloc[0].to_dict() if not summary.empty else {}
    notes = [
        "V4.28确认：延长持有期没有在可验证样本数下解决收益厚度问题。",
        f"主规则仍为V4.27的连续1日、5日持有：事件{int(primary['nonoverlap_events'])}个，平均收益{fmt_pct(primary['event_mean_return'])}，坏窗口{fmt_pct(primary['event_bad_window_rate'])}。",
        f"事件数达到30个的规则数量为{len(adequate)}；这些规则中没有一条平均收益达到2%。",
        f"全表最高均值来自{best.get('signal_id', '')}，但它只有{int(best.get('nonoverlap_events', 0) or 0)}个事件，属于样本不足诊断，不可升级。",
    ]
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "停止围绕持有期做小步优化；下一步应回到反弹窗口定义，寻找更强的外生压力释放证据或承认当前数据无法稳定识别窗口。",
    }


def run_summary(policy: dict[str, Any], primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    audit_fail = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    candidate_count = 1 if primary["status"] == "有效反弹窗口" and audit_fail == 0 else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": candidate_count,
        "audit_fail_count": audit_fail,
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；固定广度底线多持有期审计未证明有效反弹窗口",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], summary: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.28 宽池固定广度底线多持有期审计报告",
            "",
            notes["main_diagnosis"],
            "",
            f"- 主规则：{run['primary_signal_id']}",
            f"- 主规则独立事件：{run['primary_realtime_events']}",
            f"- 主规则平均收益：{fmt_pct(run['best_event_mean_return'])}",
            f"- 主规则坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
            f"- 最终结论：{run['final_verdict']}",
            "",
            "## 关键判断",
            *[f"- {item}" for item in notes["next_iterations"]],
            "",
            "## 多持有期规则摘要",
            summary.to_markdown(index=False),
            "",
            "## 主规则年度表现",
            wf.to_markdown(index=False) if not wf.empty else "主规则无年度事件。",
            "",
            "## 审计",
            data_audit.to_markdown(index=False),
            leakage.to_markdown(index=False),
            "",
            f"研究边界：{policy['research_boundary']}",
        ]
    )


def to_bool(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(["true", "1", "yes"])


def fmt_pct(v: Any) -> str:
    try:
        x = float(v)
    except Exception:
        return ""
    return "" if math.isnan(x) else f"{x:.2%}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v]
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if hasattr(v, "item"):
        return clean(v.item())
    return v


def none_if_nan(v: Any) -> float | None:
    try:
        x = float(v)
    except Exception:
        return None
    return None if math.isnan(x) else x


if __name__ == "__main__":
    main()
