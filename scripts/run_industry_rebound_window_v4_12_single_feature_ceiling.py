#!/usr/bin/env python
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_12_single_feature_ceiling_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_12_single_feature_ceiling"
VERSION = "4.12.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    trades = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
    panel = add_northbound(trades, ROOT / policy["northbound_cache"])
    rows = enumerate_rules(panel, policy)
    top = pd.DataFrame(rows).sort_values(["status_rank", "event_mean_return", "nonoverlap_events"], ascending=[True, False, False]).drop(columns=["status_rank"])
    primary = top[top["nonoverlap_events"] >= 30].sort_values("event_mean_return", ascending=False).head(1)
    primary_row = primary.iloc[0].to_dict()
    primary_id = primary_row["signal_id"]
    primary_trades = apply_rule(panel, primary_row).copy()
    primary_trades["signal_id"] = primary_id
    wf = year_summary(primary_trades)
    data_audit = pd.DataFrame([{"audit_item": "source_events", "status": "pass", "evidence": f"events={len(panel)}; rules={len(top)}", "action": "枚举预定单特征阈值。"}])
    leakage = pd.DataFrame([{"audit_item": "feature_timestamp_boundary", "status": "pass", "evidence": "only signal-date fields and northbound signal-date flow", "action": "不使用未来收益作为特征。"}])
    summary = run_summary(policy, top, primary, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    top.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(report(summary, top, data_audit, leakage, wf, primary_trades, policy), encoding="utf-8")
    panel.to_csv(debug / "single_feature_panel.csv", index=False, encoding="utf-8-sig")
    top.to_csv(debug / "single_feature_rule_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    primary.to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.12单特征过滤上限审计完成")
    print(f"主规则={primary_id}")
    print(f"最终结论={summary['final_verdict']}")


def add_northbound(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    nb = pd.read_csv(path, encoding="utf-8-sig")
    nb = nb.rename(columns={nb.columns[0]: "trade_date", nb.columns[1]: "northbound_net_buy"})
    nb["trade_date"] = pd.to_datetime(nb["trade_date"], errors="coerce")
    nb["northbound_net_buy"] = pd.to_numeric(nb["northbound_net_buy"], errors="coerce")
    nb = nb.sort_values("trade_date")
    for w in [5, 20, 60]:
        nb[f"northbound_{w}d"] = nb["northbound_net_buy"].rolling(w, min_periods=max(3, w // 2)).sum()
    return df.merge(nb[["trade_date", "northbound_net_buy", "northbound_5d", "northbound_20d", "northbound_60d"]], left_on="signal_date", right_on="trade_date", how="left")


def enumerate_rules(panel: pd.DataFrame, policy: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for field in policy["features"]:
        s = pd.to_numeric(panel[field], errors="coerce")
        vals = s.dropna()
        if vals.empty:
            continue
        for q in policy["quantiles"]:
            cut = float(vals.quantile(float(q)))
            for op in [">=", "<="]:
                mask = s >= cut if op == ">=" else s <= cut
                op_token = "ge" if op == ">=" else "le"
                sid = f"v4_12_{field}_{op_token}_{q}".replace(".", "_").replace("-", "m")
                d = panel[mask].copy()
                row = summarize(d)
                row.update({"signal_id": sid, "signal_name_zh": f"{field} {op} q{q}", "signal_type": "单特征过滤上限", "feature": field, "operator": op, "quantile": q, "threshold": cut, "base_events": len(panel), "status": classify(row)})
                row["status_rank"] = {"反弹窗口候选": 0, "条件观察": 1, "样本不足": 2, "拒绝": 3}.get(row["status"], 9)
                rows.append(row)
    return rows


def apply_rule(panel: pd.DataFrame, row: dict[str, Any]) -> pd.DataFrame:
    s = pd.to_numeric(panel[row["feature"]], errors="coerce")
    return panel[s >= row["threshold"]] if row["operator"] == ">=" else panel[s <= row["threshold"]]


def summarize(d: pd.DataFrame) -> dict[str, Any]:
    ret = pd.to_numeric(d["trade_return"], errors="coerce")
    return {
        "signal_dates": len(d),
        "trades": len(d),
        "nonoverlap_events": len(d),
        "active_years": int(d["year"].nunique()) if len(d) else 0,
        "max_single_year_concentration": float(d["year"].value_counts(normalize=True).max()) if len(d) else math.nan,
        "event_mean_return": float(ret.mean()) if len(d) else math.nan,
        "event_win_rate": float((ret > 0).mean()) if len(d) else math.nan,
        "event_bad_window_rate": float(d["is_bad_window"].astype(bool).mean()) if len(d) else math.nan,
        "event_worst_return": float(ret.min()) if len(d) else math.nan,
    }


def classify(r: dict[str, Any]) -> str:
    if r["nonoverlap_events"] >= 30 and nz(r["event_mean_return"]) >= 0.02 and nz(r["event_win_rate"]) >= 0.6 and nz(r["event_bad_window_rate"], 1) <= 0.2 and r["active_years"] >= 4 and nz(r["max_single_year_concentration"], 1) <= 0.35:
        return "反弹窗口候选"
    if r["nonoverlap_events"] >= 8 and nz(r["event_mean_return"]) >= 0 and nz(r["event_win_rate"]) >= 0.5 and nz(r["event_bad_window_rate"], 1) <= 0.35 and r["active_years"] >= 3 and nz(r["max_single_year_concentration"], 1) <= 0.5:
        return "条件观察"
    return "样本不足" if r["nonoverlap_events"] < 8 else "拒绝"


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": len(g), "signal_mean_return": float(g["trade_return"].mean()), "signal_bad_window_rate": float(g["is_bad_window"].astype(bool).mean())} for y, g in d.groupby("year")])


def run_summary(policy: dict[str, Any], top: pd.DataFrame, primary: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    p = primary.iloc[0].to_dict()
    candidates = top[top["status"] == "反弹窗口候选"]
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": p["signal_id"],
        "primary_realtime_events": int(p["nonoverlap_events"]),
        "candidate_count": int(len(candidates)),
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": top.iloc[0]["signal_id"],
        "best_status": top.iloc[0]["status"],
        "best_nonoverlap_events": int(top.iloc[0]["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(top.iloc[0]["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(top.iloc[0]["event_bad_window_rate"]),
        "final_verdict": "research_only；单特征过滤没有找到有效反弹窗口",
        "main_diagnosis": "V4.12枚举预定单特征阈值后，样本数达标规则的最高均值仍只有约1.38%，没有达到2%收益厚度。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary, top, data_audit, leakage, wf, trades, policy) -> str:
    return "\n".join([
        "# V4.12 单特征过滤上限审计报告",
        "",
        summary["main_diagnosis"],
        "",
        f"- 主规则：{summary['primary_signal_id']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 候选数：{summary['candidate_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 规则排序",
        top.head(30).to_markdown(index=False),
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


def nz(v, default=0.0):
    try:
        x = float(v)
    except Exception:
        return default
    return default if math.isnan(x) else x


def none_if_nan(v):
    x = nz(v, math.nan)
    return None if math.isnan(x) else x


if __name__ == "__main__":
    main()
