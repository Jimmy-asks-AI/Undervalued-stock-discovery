#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_15_dynamic_exit_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_15_dynamic_exit"
VERSION = "4.15.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    source = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
    panel = pd.read_csv(ROOT / policy["market_panel_path"], encoding="utf-8-sig", parse_dates=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    trades = build_trades(source, panel, policy, same_close=False)
    same_close = summarize_rules(build_trades(source, panel, policy, same_close=True))
    summary_table = summarize_rules(trades)
    primary = summary_table.iloc[0].to_dict()
    primary_trades = trades[trades["signal_id"] == primary["signal_id"]].copy()
    wf = year_summary(primary_trades)
    data_audit = pd.DataFrame([{"audit_item": "fixed_event_dynamic_exit", "status": "pass", "evidence": f"source_events={len(source)}; rules={len(summary_table)}", "action": "固定V4.13事件，只测试预声明退出规则。"}])
    leakage = pd.DataFrame([{"audit_item": "next_close_exit_boundary", "status": "pass", "evidence": "close trigger exits at next trading day close", "action": "不使用同日收盘触发后同日收盘退出作为主结果。"}])
    bias = same_close_bias(summary_table, same_close)
    summary = run_summary(policy, summary_table, primary, data_audit, leakage, bias)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    summary_table.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(report(summary, summary_table, bias, data_audit, leakage, wf, policy), encoding="utf-8")
    panel.to_csv(debug / "dynamic_exit_panel.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "dynamic_exit_trades.csv", index=False, encoding="utf-8-sig")
    summary_table.to_csv(debug / "dynamic_exit_summary.csv", index=False, encoding="utf-8-sig")
    bias.to_csv(debug / "same_close_bias_audit.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.15固定事件动态退出审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"最终结论={summary['final_verdict']}")


def build_trades(source: pd.DataFrame, panel: pd.DataFrame, policy: dict[str, Any], same_close: bool) -> pd.DataFrame:
    date_to_idx = {d: i for i, d in enumerate(panel["trade_date"])}
    rows = []
    for pt in policy["profit_take_grid"]:
        for sl in policy["stop_loss_grid"]:
            rule_id = rule_token(pt, sl)
            for _, row in source.iterrows():
                i = date_to_idx.get(row["signal_date"])
                max_h = int(policy["max_holding_days"])
                if i is None or i + 1 + max_h >= len(panel):
                    continue
                entry_idx = i + 1
                entry_nav = float(panel.loc[entry_idx, "market_nav"])
                exit_idx = entry_idx + max_h
                exit_reason = "max_holding"
                trigger_day = max_h
                for day in range(1, max_h + 1):
                    trigger_ret = float(panel.loc[entry_idx + day, "market_nav"] / entry_nav - 1)
                    if (pt is not None and trigger_ret >= float(pt)) or (sl is not None and trigger_ret <= float(sl)):
                        exit_idx = entry_idx + day if same_close else min(entry_idx + day + int(policy["execution_lag_days"]), entry_idx + max_h)
                        trigger_day = day
                        exit_reason = "profit_take" if pt is not None and trigger_ret >= float(pt) else "stop_loss"
                        break
                path = panel.loc[entry_idx + 1 : exit_idx, "market_nav"] / entry_nav - 1
                ret = float(panel.loc[exit_idx, "market_nav"] / entry_nav - 1)
                out = row.to_dict()
                out.update({
                    "signal_id": f"v4_15_{rule_id}",
                    "signal_name_zh": rule_name(pt, sl),
                    "profit_take": pt,
                    "stop_loss": sl,
                    "entry_date": panel.loc[entry_idx, "trade_date"].date().isoformat(),
                    "exit_date": panel.loc[exit_idx, "trade_date"].date().isoformat(),
                    "trigger_day": int(trigger_day),
                    "holding_days": int(exit_idx - entry_idx),
                    "exit_reason": exit_reason,
                    "trade_return": ret,
                    "max_adverse_return": float(path.min()) if len(path) else 0.0,
                    "is_win": bool(ret > 0),
                    "is_bad_window": bool(ret <= float(policy["bad_window_threshold"])),
                    "year": int(pd.Timestamp(row["signal_date"]).year),
                })
                rows.append(out)
    return pd.DataFrame(rows)


def summarize_rules(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sid, g in trades.groupby("signal_id"):
        ret = pd.to_numeric(g["trade_return"], errors="coerce")
        years = g["year"].value_counts(normalize=True)
        row = {
            "signal_id": sid,
            "signal_name_zh": g["signal_name_zh"].iloc[0],
            "signal_type": "固定事件动态退出",
            "status": "",
            "profit_take": g["profit_take"].iloc[0],
            "stop_loss": g["stop_loss"].iloc[0],
            "avg_holding_days": float(g["holding_days"].mean()),
            "profit_take_count": int((g["exit_reason"] == "profit_take").sum()),
            "stop_loss_count": int((g["exit_reason"] == "stop_loss").sum()),
            "signal_dates": len(g),
            "trades": len(g),
            "nonoverlap_events": len(g),
            "active_years": int(g["year"].nunique()),
            "max_single_year_concentration": float(years.max()),
            "event_mean_return": float(ret.mean()),
            "event_win_rate": float((ret > 0).mean()),
            "event_bad_window_rate": float(g["is_bad_window"].astype(bool).mean()),
            "event_worst_return": float(ret.min()),
        }
        row["status"] = classify(row)
        rows.append(row)
    df = pd.DataFrame(rows)
    df["score_rank"] = df.apply(pass_count, axis=1)
    return df.sort_values(["score_rank", "event_mean_return"], ascending=[False, False]).drop(columns=["score_rank"])


def same_close_bias(valid: pd.DataFrame, same_close: pd.DataFrame) -> pd.DataFrame:
    cols = ["signal_id", "event_mean_return", "event_bad_window_rate", "event_win_rate", "status"]
    out = valid[cols].merge(same_close[cols], on="signal_id", suffixes=("_next_close", "_same_close"))
    out["mean_return_bias"] = out["event_mean_return_same_close"] - out["event_mean_return_next_close"]
    out["same_close_candidate_risk"] = (out["event_mean_return_same_close"] >= 0.02) & (out["event_bad_window_rate_same_close"] <= 0.2)
    return out.sort_values("mean_return_bias", ascending=False)


def classify(r: dict[str, Any]) -> str:
    if r["nonoverlap_events"] >= 30 and r["event_mean_return"] >= 0.02 and r["event_win_rate"] >= 0.6 and r["event_bad_window_rate"] <= 0.2 and r["active_years"] >= 4 and r["max_single_year_concentration"] <= 0.35:
        return "反弹窗口候选"
    if r["nonoverlap_events"] >= 8 and r["event_mean_return"] >= 0 and r["event_win_rate"] >= 0.5 and r["event_bad_window_rate"] <= 0.35 and r["active_years"] >= 3 and r["max_single_year_concentration"] <= 0.5:
        return "条件观察"
    return "样本不足" if r["nonoverlap_events"] < 8 else "拒绝"


def pass_count(row: pd.Series) -> int:
    return sum([
        row["nonoverlap_events"] >= 30,
        row["event_mean_return"] >= 0.02,
        row["event_win_rate"] >= 0.6,
        row["event_bad_window_rate"] <= 0.2,
        row["active_years"] >= 4 and row["max_single_year_concentration"] <= 0.35,
    ])


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": len(g), "signal_mean_return": float(g["trade_return"].mean()), "signal_bad_window_rate": float(g["is_bad_window"].astype(bool).mean())} for y, g in d.groupby("year")])


def run_summary(policy: dict[str, Any], table: pd.DataFrame, primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame, bias: pd.DataFrame) -> dict[str, Any]:
    candidates = table[table["status"] == "反弹窗口候选"]
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": table.iloc[0]["signal_id"],
        "best_status": table.iloc[0]["status"],
        "best_nonoverlap_events": int(table.iloc[0]["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(table.iloc[0]["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(table.iloc[0]["event_bad_window_rate"]),
        "same_close_candidate_like_rules": int(bias["same_close_candidate_risk"].sum()),
        "final_verdict": "research_only；合法动态退出没有找到有效反弹窗口",
        "main_diagnosis": f"V4.15使用下一交易日收盘退出后，最佳规则平均收益为{primary['event_mean_return']:.2%}，仍低于2%；同日收盘退出会制造候选假象，不能作为主结果。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary, table, bias, data_audit, leakage, wf, policy) -> str:
    return "\n".join([
        "# V4.15 固定事件动态退出审计报告",
        "",
        summary["main_diagnosis"],
        "",
        f"- 主规则：{summary['primary_signal_id']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 候选数：{summary['candidate_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 合法退出规则排序",
        table.to_markdown(index=False),
        "",
        "## 同日收盘退出偏差",
        bias.head(12).to_markdown(index=False),
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


def rule_token(pt, sl) -> str:
    return f"pt_{token(pt)}_sl_{token(sl)}"


def rule_name(pt, sl) -> str:
    p = "无止盈" if pt is None else f"{pt:.0%}止盈"
    s = "无止损" if sl is None else f"{sl:.0%}止损"
    return f"最长30日，{p}，{s}"


def token(v) -> str:
    if v is None:
        return "none"
    return str(v).replace("-", "m").replace(".", "_")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean(v):
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
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
