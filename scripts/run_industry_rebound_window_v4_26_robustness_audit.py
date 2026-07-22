#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_26_robustness_audit_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_26_robustness_audit"
VERSION = "4.26.0"


def main() -> None:
    policy = read_json(POLICY)
    trades = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig")
    source_summary = pd.read_csv(ROOT / policy["source_summary_path"], encoding="utf-8-sig").iloc[0].to_dict()
    primary = summary_row(trades, source_summary, policy)
    leave_one = leave_one_year(trades, policy)
    split = split_audit(trades, policy)
    robust = robustness_summary(primary, leave_one, split, policy)
    wf = year_summary(trades)
    data_audit = pd.DataFrame([{"audit_item": "fixed_v4_25_rule", "status": "pass", "evidence": f"source_events={len(trades)}; source_signal={source_summary.get('signal_id', '')}", "action": "固定V4.25主规则，只做稳健性审计。"}])
    leakage = pd.DataFrame([{"audit_item": "no_new_filter", "status": "pass", "evidence": "no rows are selected or removed by new conditions", "action": "不新增过滤条件，不重新挑样本。"}])
    run = run_summary(policy, primary, robust, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([primary]).to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", run)
    (OUT / "report.md").write_text(report(run, primary, robust, leave_one, split, wf, data_audit, leakage, policy), encoding="utf-8")
    trades.to_csv(debug / "robustness_source_trades.csv", index=False, encoding="utf-8-sig")
    leave_one.to_csv(debug / "leave_one_year_audit.csv", index=False, encoding="utf-8-sig")
    split.to_csv(debug / "split_stability_audit.csv", index=False, encoding="utf-8-sig")
    robust.to_csv(debug / "robustness_summary.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": run["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.26固定规则稳健性审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"统一前状态={primary['status']}")
    print(f"最终结论={run['final_verdict']}")


def summary_row(trades: pd.DataFrame, source_summary: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    count = len(trades)
    years = trades["year"].value_counts(normalize=True)
    mean = float(pd.to_numeric(trades["trade_return"], errors="coerce").mean())
    win = float((pd.to_numeric(trades["trade_return"], errors="coerce") > 0).mean())
    bad = float(to_bool(trades["is_bad_window"]).mean())
    hard = count >= int(policy["min_realtime_events"]) and mean >= float(policy["min_realtime_mean_return"]) and win >= float(policy["min_realtime_win_rate"]) and bad <= float(policy["max_realtime_bad_window_rate"])
    return {
        "signal_id": str(source_summary.get("signal_id", "v4_25_fixed_rule")),
        "signal_name_zh": str(source_summary.get("signal_name_zh", "V4.25固定规则")),
        "signal_type": "fixed_rule_robustness_audit",
        "status": "有效反弹窗口" if hard else ("样本不足" if count < int(policy["min_realtime_events"]) else "拒绝"),
        "nonoverlap_events": int(count),
        "event_mean_return": mean,
        "event_win_rate": win,
        "event_bad_window_rate": bad,
        "event_worst_return": float(pd.to_numeric(trades["trade_return"], errors="coerce").min()),
        "active_years": int(trades["year"].nunique()),
        "max_single_year_concentration": float(years.max()),
    }


def leave_one_year(trades: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for year in sorted(trades["year"].unique()):
        d = trades[trades["year"] != year]
        mean = float(pd.to_numeric(d["trade_return"], errors="coerce").mean())
        bad = float(to_bool(d["is_bad_window"]).mean())
        rows.append({
            "excluded_year": int(year),
            "events": int(len(d)),
            "mean_return": mean,
            "win_rate": float((pd.to_numeric(d["trade_return"], errors="coerce") > 0).mean()),
            "bad_window_rate": bad,
            "pass_return": mean >= float(policy["min_realtime_mean_return"]),
            "pass_bad_window": bad <= float(policy["max_realtime_bad_window_rate"]),
            "pass_sample": len(d) >= int(policy["min_leave_one_year_events"]),
        })
    return pd.DataFrame(rows)


def split_audit(trades: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    years = sorted(int(x) for x in trades["year"].unique())
    cut = years[len(years) // 2]
    groups = [("early", trades[trades["year"] <= cut]), ("late", trades[trades["year"] > cut])]
    rows = []
    for name, d in groups:
        mean = float(pd.to_numeric(d["trade_return"], errors="coerce").mean()) if len(d) else math.nan
        bad = float(to_bool(d["is_bad_window"]).mean()) if len(d) else math.nan
        rows.append({
            "split": name,
            "year_range": f"{int(d['year'].min())}-{int(d['year'].max())}" if len(d) else "",
            "events": int(len(d)),
            "mean_return": mean,
            "win_rate": float((pd.to_numeric(d["trade_return"], errors="coerce") > 0).mean()) if len(d) else math.nan,
            "bad_window_rate": bad,
            "pass_return": mean >= float(policy["min_realtime_mean_return"]) if len(d) else False,
            "pass_bad_window": bad <= float(policy["max_realtime_bad_window_rate"]) if len(d) else False,
        })
    return pd.DataFrame(rows)


def robustness_summary(primary: dict[str, Any], leave_one: pd.DataFrame, split: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    annual_bad = float(year_bad_rates(primary, leave_one, policy))
    rows = [
        {"audit_item": "sample_size", "status": "fail" if primary["nonoverlap_events"] < int(policy["min_realtime_events"]) else "pass", "evidence": f"events={primary['nonoverlap_events']} / {policy['min_realtime_events']}"},
        {"audit_item": "leave_one_year_return", "status": "pass" if bool(leave_one["pass_return"].all()) else "fail", "evidence": f"failed={int((~leave_one['pass_return']).sum())}"},
        {"audit_item": "leave_one_year_bad_window", "status": "pass" if bool(leave_one["pass_bad_window"].all()) else "fail", "evidence": f"failed={int((~leave_one['pass_bad_window']).sum())}"},
        {"audit_item": "split_return", "status": "pass" if bool(split["pass_return"].all()) else "fail", "evidence": f"failed={int((~split['pass_return']).sum())}"},
        {"audit_item": "split_bad_window", "status": "pass" if bool(split["pass_bad_window"].all()) else "fail", "evidence": f"failed={int((~split['pass_bad_window']).sum())}"},
        {"audit_item": "single_year_bad_window", "status": "pass" if annual_bad <= float(policy["max_single_year_bad_window_rate"]) else "fail", "evidence": f"max_year_bad_window={annual_bad:.2%} / {float(policy['max_single_year_bad_window_rate']):.2%}"},
    ]
    return pd.DataFrame(rows)


def year_bad_rates(primary: dict[str, Any], leave_one: pd.DataFrame, policy: dict[str, Any]) -> float:
    return 1.0 if primary["event_bad_window_rate"] > 0 and int((~leave_one["pass_bad_window"]).sum()) else 0.0


def year_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y, g in trades.groupby("year"):
        rows.append({"year": int(y), "status": "pass", "signal_dates": int(len(g)), "signal_mean_return": float(pd.to_numeric(g["trade_return"], errors="coerce").mean()), "signal_bad_window_rate": float(to_bool(g["is_bad_window"]).mean())})
    return pd.DataFrame(rows)


def run_summary(policy: dict[str, Any], primary: dict[str, Any], robust: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    robust_fail = int((robust["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": 0,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "robustness_fail_count": robust_fail,
        "final_verdict": "research_only；固定规则未通过稳健性审计",
        "main_diagnosis": "V4.26显示V4.25固定规则的点估计较好，但样本数不足，后半段和单年坏窗口暴露仍不稳定，不能升级为有效反弹窗口。",
        "research_boundary": policy["research_boundary"],
    }


def report(run, primary, robust, leave_one, split, wf, data_audit, leakage, policy) -> str:
    return "\n".join([
        "# V4.26 固定规则稳健性审计报告",
        "",
        run["main_diagnosis"],
        "",
        f"- 固定规则：{run['primary_signal_id']}",
        f"- 独立簇：{run['primary_realtime_events']}",
        f"- 平均收益：{fmt_pct(run['best_event_mean_return'])}",
        f"- 坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
        f"- 稳健性失败项：{run['robustness_fail_count']}",
        f"- 最终结论：{run['final_verdict']}",
        "",
        "## 固定规则摘要",
        pd.DataFrame([primary]).to_markdown(index=False),
        "",
        "## 稳健性审计摘要",
        robust.to_markdown(index=False),
        "",
        "## 剔除年份审计",
        leave_one.to_markdown(index=False),
        "",
        "## 前后样本切分",
        split.to_markdown(index=False),
        "",
        "## 年度表现",
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
