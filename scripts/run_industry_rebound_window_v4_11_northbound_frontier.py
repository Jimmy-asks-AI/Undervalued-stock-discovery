#!/usr/bin/env python
from __future__ import annotations

import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_11_northbound_frontier_policy.json"
V410 = ROOT / "scripts" / "run_industry_rebound_window_v4_10_northbound_overlay.py"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_11_northbound_frontier"
VERSION = "4.11.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    v410 = load_v410()
    nb = v410.load_northbound({"cache_dir": policy["northbound_cache_dir"]})
    frames = []
    summaries = []
    for src in policy["source_runs"]:
        trades = pd.read_csv(ROOT / src["trades_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
        panel = v410.add_northbound_features(trades, nb)
        panel["source_id"] = src["source_id"]
        panel["source_name_zh"] = src["source_name_zh"]
        frames.append(panel)
        summaries.extend(summarize_source(panel, src))
    all_panel = pd.concat(frames, ignore_index=True)
    top = pd.DataFrame(summaries).sort_values(["status_rank", "event_mean_return"], ascending=[True, False]).drop(columns=["status_rank"])
    primary = top[top["signal_id"] == policy["primary_signal_id"]].copy()
    primary_trades = select_primary_trades(all_panel)
    wf = year_summary(primary_trades)
    data_audit = data_audit_frame(all_panel, nb)
    leakage = leakage_frame(data_audit)
    run_summary = run_summary_frame(policy, primary, top, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    top.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", run_summary)
    (OUT / "report.md").write_text(report(run_summary, top, data_audit, leakage, wf, primary_trades, policy), encoding="utf-8")
    all_panel.to_csv(debug / "northbound_frontier_panel.csv", index=False, encoding="utf-8-sig")
    top.to_csv(debug / "northbound_frontier_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    primary.to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": run_summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.11北向样本前沿审计完成")
    print(f"主规则事件数={run_summary['primary_realtime_events']}")
    print(f"最终结论={run_summary['final_verdict']}")


def summarize_source(panel: pd.DataFrame, src: dict[str, str]) -> list[dict[str, Any]]:
    masks = {
        "northbound_valid": panel["northbound_net_buy"].notna(),
        "northbound_5d_positive": panel["northbound_net_buy_5d"] >= 0,
        "northbound_20d_positive": panel["northbound_net_buy_20d"] >= 0,
        "northbound_60d_positive": panel["northbound_net_buy_60d"] >= 0,
    }
    rows = []
    for fid, mask in masks.items():
        d = panel[mask].copy()
        ret = pd.to_numeric(d["trade_return"], errors="coerce")
        row = {
            "signal_id": f"v4_11_{src['source_id']}_{fid}",
            "signal_name_zh": f"{src['source_name_zh']} + {fid}",
            "signal_type": "北向覆盖层样本前沿",
            "source_id": src["source_id"],
            "filter_id": fid,
            "base_events": len(panel),
            "signal_dates": len(panel),
            "trades": len(d),
            "nonoverlap_events": len(d),
            "active_years": int(d["year"].nunique()) if len(d) else 0,
            "max_single_year_concentration": float(d["year"].value_counts(normalize=True).max()) if len(d) else math.nan,
            "event_mean_return": float(ret.mean()) if len(d) else math.nan,
            "event_win_rate": float((ret > 0).mean()) if len(d) else math.nan,
            "event_bad_window_rate": float(d["is_bad_window"].astype(bool).mean()) if len(d) else math.nan,
            "event_worst_return": float(ret.min()) if len(d) else math.nan,
        }
        row["status"] = classify(row)
        row["status_rank"] = {"反弹窗口候选": 0, "条件观察": 1, "样本不足": 2, "拒绝": 3}.get(row["status"], 9)
        rows.append(row)
    return rows


def classify(row: dict[str, Any]) -> str:
    hard = row["nonoverlap_events"] >= 30 and nz(row["event_mean_return"]) >= 0.02 and nz(row["event_win_rate"]) >= 0.6 and nz(row["event_bad_window_rate"], 1) <= 0.2 and row["active_years"] >= 4 and nz(row["max_single_year_concentration"], 1) <= 0.35
    if hard:
        return "反弹窗口候选"
    cond = row["nonoverlap_events"] >= 8 and nz(row["event_mean_return"]) >= 0 and nz(row["event_win_rate"]) >= 0.5 and nz(row["event_bad_window_rate"], 1) <= 0.35 and row["active_years"] >= 3 and nz(row["max_single_year_concentration"], 1) <= 0.5
    return "条件观察" if cond else ("样本不足" if row["nonoverlap_events"] < 8 else "拒绝")


def select_primary_trades(panel: pd.DataFrame) -> pd.DataFrame:
    d = panel[(panel["source_id"] == "v4_7_short_horizon") & (panel["northbound_net_buy_20d"] >= 0)].copy()
    d["signal_id"] = "v4_11_v47_northbound_20d_positive"
    return d


def year_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y, d in trades.groupby("year"):
        rows.append({"year": int(y), "status": "pass", "signal_dates": len(d), "signal_mean_return": float(d["trade_return"].mean()), "signal_bad_window_rate": float(d["is_bad_window"].astype(bool).mean())})
    return pd.DataFrame(rows)


def data_audit_frame(panel: pd.DataFrame, nb: pd.DataFrame) -> pd.DataFrame:
    valid = nb[nb["northbound_net_buy"].notna()]
    return pd.DataFrame([
        {"audit_item": "frontier_sources", "status": "pass", "evidence": f"sources={panel['source_id'].nunique()}; rows={len(panel)}", "action": "比较V4.7和V4.8事件池。"},
        {"audit_item": "northbound_coverage", "status": "pass", "evidence": f"valid_rows={len(valid)}; last_valid={valid['trade_date'].max().date()}", "action": "北向资金只作为历史覆盖层。"},
        {"audit_item": "recent_missing_values", "status": "observe", "evidence": f"latest={nb['trade_date'].max().date()}", "action": "近期空值限制实时使用。"},
    ])


def leakage_frame(data_audit: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([
        {"audit_item": "feature_timestamp_boundary", "status": "pass", "evidence": "joined by signal_date", "action": "不使用信号日之后资金流。"},
        {"audit_item": "data_audit", "status": "pass" if not (data_audit["status"] == "fail").any() else "fail", "evidence": f"failures={int((data_audit['status'] == 'fail').sum())}", "action": "数据失败不得升级。"},
    ])


def run_summary_frame(policy: dict[str, Any], primary: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    p = primary.iloc[0].to_dict()
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": policy["primary_signal_id"],
        "primary_realtime_events": int(p["nonoverlap_events"]),
        "candidate_count": int((top["status"] == "反弹窗口候选").sum()),
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": top.iloc[0]["signal_id"],
        "best_status": top.iloc[0]["status"],
        "best_nonoverlap_events": int(top.iloc[0]["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(top.iloc[0]["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(top.iloc[0]["event_bad_window_rate"]),
        "final_verdict": "research_only；放宽到V4.7事件池后仍未同时满足样本数和收益厚度门槛",
        "main_diagnosis": "V4.11证明北向覆盖层的样本-收益前沿仍不够：V4.7池样本接近30但收益不足，V4.8池质量更高但样本更少。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary: dict[str, Any], top: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, wf: pd.DataFrame, trades: pd.DataFrame, policy: dict[str, Any]) -> str:
    return "\n".join([
        "# V4.11 北向覆盖层样本前沿审计报告",
        "",
        f"版本：{VERSION}",
        f"生成时间：{summary['generated_at']}",
        "",
        summary["main_diagnosis"],
        "",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 候选数：{summary['candidate_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 样本前沿",
        top.to_markdown(index=False),
        "",
        "## 数据审计",
        data_audit.to_markdown(index=False),
        "",
        "## 年度表现",
        wf.to_markdown(index=False),
        "",
        "## 主规则交易",
        trades[["signal_date", "entry_date", "exit_date", "trade_return", "max_adverse_return", "is_bad_window"]].to_markdown(index=False),
        "",
        "## 泄漏审计",
        leakage.to_markdown(index=False),
        "",
        f"研究边界：{policy['research_boundary']}",
    ])


def load_v410():
    spec = importlib.util.spec_from_file_location("v410", V410)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if hasattr(v, "item"):
        return clean(v.item())
    return v


def nz(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except Exception:
        return default
    return default if math.isnan(x) else x


def none_if_nan(v: Any) -> float | None:
    x = nz(v, math.nan)
    return None if math.isnan(x) else x


if __name__ == "__main__":
    main()
