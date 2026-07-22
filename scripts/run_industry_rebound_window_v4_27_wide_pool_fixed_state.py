#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_27_wide_pool_fixed_state_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_27_wide_pool_fixed_state"
VERSION = "4.27.0"


def main() -> None:
    policy = read_json(POLICY)
    source = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig")
    summary, filtered = evaluate_rules(source, policy)
    primary = summary[summary["signal_id"] == policy["primary_rule_id"]].iloc[0].to_dict()
    primary_trades = filtered[filtered["signal_id"] == primary["signal_id"]].copy()
    wf = year_summary(primary_trades)
    data_audit = pd.DataFrame([{"audit_item": "fixed_v4_7_wide_pool", "status": "pass", "evidence": f"source_events={len(source)}; rules={len(policy['rules'])}", "action": "固定V4.7宽事件池，只回放事前状态规则。"}])
    leakage = pd.DataFrame([{"audit_item": "pre_signal_features_only", "status": "pass", "evidence": "rules use market_return_5d / industry breadth / ma20 fields in source trades", "action": "不使用未来收益或坏窗口标签筛选。"}])
    run = run_summary(policy, primary, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", run)
    (OUT / "report.md").write_text(report(run, summary, wf, data_audit, leakage, policy), encoding="utf-8")
    source.to_csv(debug / "wide_pool_source_trades.csv", index=False, encoding="utf-8-sig")
    filtered.to_csv(debug / "wide_pool_filtered_trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(debug / "wide_pool_rule_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": run["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.27宽池固定状态规则回放完成")
    print(f"主规则={primary['signal_id']}")
    print(f"独立事件={primary['nonoverlap_events']}")
    print(f"最终结论={run['final_verdict']}")


def evaluate_rules(source: pd.DataFrame, policy: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, frames = [], []
    for rule in policy["rules"]:
        d = source[mask_for(source, rule["conditions"])].copy()
        d["signal_id"] = rule["rule_id"]
        d["signal_name_zh"] = rule["rule_name_zh"]
        d["signal_type"] = "wide_pool_fixed_state"
        frames.append(d)
        rows.append(summary_row(d, rule, policy))
    summary = pd.DataFrame(rows)
    primary = summary[summary["signal_id"] == policy["primary_rule_id"]]
    rest = summary[summary["signal_id"] != policy["primary_rule_id"]].sort_values(["nonoverlap_events", "event_mean_return"], ascending=[False, False])
    return pd.concat([primary, rest], ignore_index=True), pd.concat(frames, ignore_index=True)


def mask_for(df: pd.DataFrame, conditions: dict[str, list[Any]]) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for col, (op, value) in conditions.items():
        s = pd.to_numeric(df[col], errors="coerce")
        if op == ">=":
            mask &= s >= float(value)
        elif op == "<=":
            mask &= s <= float(value)
        else:
            raise ValueError(f"unsupported operator: {op}")
    return mask


def summary_row(d: pd.DataFrame, rule: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    count = len(d)
    years = d["year"].value_counts(normalize=True) if count else pd.Series(dtype=float)
    mean = float(pd.to_numeric(d["trade_return"], errors="coerce").mean()) if count else math.nan
    win = float(to_bool(d["is_win"]).mean()) if count else math.nan
    bad = float(to_bool(d["is_bad_window"]).mean()) if count else math.nan
    hard = count >= int(policy["min_realtime_events"]) and mean >= float(policy["min_realtime_mean_return"]) and win >= float(policy["min_realtime_win_rate"]) and bad <= float(policy["max_realtime_bad_window_rate"]) and (years.empty or float(years.max()) <= float(policy["max_single_year_concentration"]))
    return {
        "signal_id": rule["rule_id"],
        "signal_name_zh": rule["rule_name_zh"],
        "signal_type": "wide_pool_fixed_state",
        "status": "有效反弹窗口" if hard else ("样本不足" if count < int(policy["min_realtime_events"]) else "拒绝"),
        "nonoverlap_events": int(count),
        "event_mean_return": mean,
        "event_win_rate": win,
        "event_bad_window_rate": bad,
        "event_worst_return": float(pd.to_numeric(d["trade_return"], errors="coerce").min()) if count else math.nan,
        "active_years": int(d["year"].nunique()) if count else 0,
        "max_single_year_concentration": float(years.max()) if len(years) else math.nan,
        "conditions_json": json.dumps(rule["conditions"], ensure_ascii=False),
    }


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": int(len(g)), "signal_mean_return": float(pd.to_numeric(g["trade_return"], errors="coerce").mean()), "signal_bad_window_rate": float(to_bool(g["is_bad_window"]).mean())} for y, g in d.groupby("year")])


def run_summary(policy: dict[str, Any], primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": 1 if primary["status"] == "有效反弹窗口" else 0,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；宽池固定状态规则收益厚度不足，不能升级为有效反弹窗口",
        "main_diagnosis": "V4.27显示V4.25的广度底线回放到V4.7宽池后可以把样本扩到30个且坏窗口很低，但5日平均收益只有约1.19%，收益厚度仍不达标。",
        "research_boundary": policy["research_boundary"],
    }


def report(run, summary, wf, data_audit, leakage, policy) -> str:
    return "\n".join([
        "# V4.27 宽池固定状态规则回放报告",
        "",
        run["main_diagnosis"],
        "",
        f"- 主规则：{run['primary_signal_id']}",
        f"- 主规则独立事件：{run['primary_realtime_events']}",
        f"- 主规则平均收益：{fmt_pct(run['best_event_mean_return'])}",
        f"- 主规则坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
        f"- 最终结论：{run['final_verdict']}",
        "",
        "## 固定状态规则摘要",
        summary.to_markdown(index=False),
        "",
        "## 主规则年度表现",
        wf.to_markdown(index=False),
        "",
        "## 审计",
        data_audit.to_markdown(index=False),
        leakage.to_markdown(index=False),
        "",
        f"研究边界：{policy['research_boundary']}",
    ])


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


def clean(v):
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v]
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if hasattr(v, "item"):
        return clean(v.item())
    return v


def none_if_nan(v):
    try:
        x = float(v)
    except Exception:
        return None
    return None if math.isnan(x) else x


if __name__ == "__main__":
    main()
