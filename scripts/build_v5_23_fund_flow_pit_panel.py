#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data_catalog" / "cache" / "industry_fund_flow" / "ths"
MAPPING = ROOT / "configs" / "industry_fund_flow_ths_sw2_mapping.csv"
OUT = ROOT / "outputs" / "audit" / "fund_flow_pit_panel_v5_23"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the accumulated THS industry fund-flow PIT panel.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    panel = build_panel()
    checks = build_checks(panel)
    summary = build_summary(panel, checks)
    latest = latest_snapshot(panel)
    write_outputs(summary, panel, latest, checks)
    print(f"output_dir={OUT}")
    print(f"snapshot_date_count={summary['snapshot_date_count']}")
    print(f"alpha_ready={summary['alpha_ready']}")


def build_panel() -> pd.DataFrame:
    mapping = pd.read_csv(MAPPING, encoding="utf-8-sig") if MAPPING.exists() else pd.DataFrame()
    rows = []
    for day in sorted(child for child in CACHE.iterdir() if child.is_dir()) if CACHE.exists() else []:
        now_path = day / "ths_industry_fund_flow_now.csv"
        rolling_path = day / "ths_industry_fund_flow_5d.csv"
        if not now_path.exists() or not rolling_path.exists():
            continue
        now = normalize_now(pd.read_csv(now_path, encoding="utf-8-sig"), day.name)
        rolling = normalize_rolling(pd.read_csv(rolling_path, encoding="utf-8-sig"), day.name)
        rows.append(now.merge(rolling, on=["trade_date", "ths_industry_name"], how="left"))
    panel = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if not panel.empty and not mapping.empty:
        panel = panel.merge(mapping, on="ths_industry_name", how="left")
    if not panel.empty:
        panel["today_flow_positive"] = panel["today_net_flow"].fillna(0) > 0
        panel["rolling_5d_flow_positive"] = panel["rolling_5d_net_flow"].fillna(0) > 0
        panel["dual_positive_flow"] = panel["today_flow_positive"] & panel["rolling_5d_flow_positive"]
        panel["mapping_status"] = panel["mapped_sw2_code"].map(lambda x: "mapped" if pd.notna(x) else "unmapped")
    return panel


def normalize_now(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    out = pd.DataFrame()
    out["trade_date"] = pd.Series(trade_date, index=frame.index)
    out["cached_at"] = frame.get("cached_at", "")
    out["ths_rank_now"] = number(frame["序号"])
    out["ths_industry_name"] = frame["行业"].astype(str)
    out["industry_index"] = number(frame["行业指数"])
    out["today_return_pct"] = number(frame["行业-涨跌幅"])
    out["today_inflow"] = number(frame["流入资金"])
    out["today_outflow"] = number(frame["流出资金"])
    out["today_net_flow"] = number(frame["净额"])
    out["company_count"] = number(frame["公司家数"])
    out["leader_stock"] = frame["领涨股"].astype(str)
    out["leader_return_pct"] = number(frame["领涨股-涨跌幅"])
    return out


def normalize_rolling(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    out = pd.DataFrame()
    out["trade_date"] = pd.Series(trade_date, index=frame.index)
    out["ths_rank_5d"] = number(frame["序号"])
    out["ths_industry_name"] = frame["行业"].astype(str)
    out["rolling_5d_return_pct"] = number(frame["阶段涨跌幅"])
    out["rolling_5d_inflow"] = number(frame["流入资金"])
    out["rolling_5d_outflow"] = number(frame["流出资金"])
    out["rolling_5d_net_flow"] = number(frame["净额"])
    return out


def number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace("%", "", regex=False), errors="coerce")


def latest_snapshot(panel: pd.DataFrame) -> pd.DataFrame:
    if panel.empty:
        return panel
    latest = panel[panel["trade_date"].eq(panel["trade_date"].max())].copy()
    cols = [
        "trade_date", "ths_industry_name", "mapped_sw2_code", "mapped_sw2_name",
        "mapping_confidence", "mapping_status", "today_return_pct", "today_net_flow",
        "rolling_5d_return_pct", "rolling_5d_net_flow", "dual_positive_flow",
    ]
    for col in cols:
        if col not in latest:
            latest[col] = ""
    return latest[cols].sort_values(["dual_positive_flow", "today_net_flow"], ascending=[False, False])


def build_checks(panel: pd.DataFrame) -> pd.DataFrame:
    dates = snapshot_date_count(panel)
    latest = latest_snapshot(panel)
    mapped_rate = high_confidence_mapping_coverage(panel)
    return pd.DataFrame([
        check("snapshot_observation_depth", "fail" if dates < 60 else "pass", f"snapshot_date_count={dates}; required=60", "少于 60 个交易日只能累计观察，不能做稳定性评估。"),
        check("snapshot_alpha_depth", "fail" if dates < 252 else "pass", f"snapshot_date_count={dates}; required=252", "少于 252 个交易日不能验证强行业 alpha。"),
        check("sw2_high_confidence_mapping_coverage", "fail" if mapped_rate < 0.8 else "pass", f"high_confidence_mapping_coverage={mapped_rate:.2%}; required=80.00%", "资金流行业体系必须高置信映射到申万二级。"),
        check("latest_dual_positive_candidates", "pending" if latest.empty else "pass", f"dual_positive_count={int(latest.get('dual_positive_flow', pd.Series(dtype=bool)).sum())}", "双正资金流只是候选观察，不是交易信号。"),
    ])


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_summary(panel: pd.DataFrame, checks: pd.DataFrame) -> dict[str, Any]:
    dates = snapshot_date_count(panel)
    return {
        "version": "5.23.0",
        "policy_id": "fund_flow_pit_panel_v5_23",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_date_count": dates,
        "latest_snapshot_date": "" if panel.empty else str(panel["trade_date"].max()),
        "panel_row_count": int(len(panel)),
        "mapped_coverage": high_confidence_mapping_coverage(panel),
        "raw_mapped_coverage": raw_mapped_coverage(panel),
        "high_confidence_mapping_coverage": high_confidence_mapping_coverage(panel),
        "exact_mapping_coverage": exact_mapping_coverage(panel),
        "observation_ready": dates >= 60,
        "alpha_ready": dates >= 252 and high_confidence_mapping_coverage(panel) >= 0.8,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "pass_count": int(checks["status"].eq("pass").sum()),
        "fail_count": int(checks["status"].eq("fail").sum()),
        "pending_count": int(checks["status"].eq("pending").sum()),
        "best_status": "research_only_fund_flow_panel_accumulating",
        "final_verdict": "V5.23 已把每日行业资金流快照整理为 PIT 面板；当前样本仍不足以验证反弹窗口内强行业选择能力，只能继续累计。",
    }


def snapshot_date_count(panel: pd.DataFrame) -> int:
    return 0 if panel.empty else int(panel["trade_date"].nunique())


def raw_mapped_coverage(panel: pd.DataFrame) -> float:
    if panel.empty or "mapped_sw2_code" not in panel:
        return 0.0
    return float(panel["mapped_sw2_code"].notna().mean())


def high_confidence_mapping_coverage(panel: pd.DataFrame) -> float:
    if panel.empty or "mapping_confidence" not in panel:
        return 0.0
    return float(pd.to_numeric(panel["mapping_confidence"], errors="coerce").ge(0.95).mean())


def exact_mapping_coverage(panel: pd.DataFrame) -> float:
    if panel.empty or "mapping_method" not in panel:
        return 0.0
    return float(panel["mapping_method"].isin(["exact", "normalized_exact"]).mean())


def write_outputs(summary: dict[str, Any], panel: pd.DataFrame, latest: pd.DataFrame, checks: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    latest.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, latest, checks), encoding="utf-8")
    panel.to_csv(DEBUG / "fund_flow_pit_panel.csv", index=False, encoding="utf-8-sig")
    latest.to_csv(DEBUG / "latest_mapped_snapshot.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(DEBUG / "readiness_checks.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], latest: pd.DataFrame, checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.23 行业资金流 PIT 面板",
        "",
        summary["final_verdict"],
        "",
        f"- 快照交易日数：{summary['snapshot_date_count']}",
        f"- 最新快照日期：{summary['latest_snapshot_date']}",
        f"- 面板行数：{summary['panel_row_count']}",
        f"- 原始映射覆盖：{summary['raw_mapped_coverage']:.2%}",
        f"- 高置信映射覆盖：{summary['high_confidence_mapping_coverage']:.2%}",
        f"- 精确/标准化映射覆盖：{summary['exact_mapping_coverage']:.2%}",
        f"- 是否可做观察评估：`{str(summary['observation_ready']).lower()}`",
        f"- 是否可做 alpha 验证：`{str(summary['alpha_ready']).lower()}`",
        "",
        "## 最新观察",
        "",
        latest.head(20).to_markdown(index=False) if not latest.empty else "当前无可用快照。",
        "",
        "## 就绪度检查",
        "",
        checks.to_markdown(index=False),
        "",
        "边界：资金流面板只用于后续前推验证；少于 60/252 个交易日前，不得声称已经找到强反弹行业。",
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    frame = pd.DataFrame({"trade_date": ["a", "b"], "mapped_sw2_code": ["1", None], "mapping_confidence": [1.0, 0.5], "mapping_method": ["exact", "manual"]})
    checks = build_checks(frame.assign(dual_positive_flow=[True, False], today_net_flow=[1, 0]))
    summary = build_summary(frame, checks)
    assert summary["snapshot_date_count"] == 2
    assert summary["alpha_ready"] is False
    assert round(summary["high_confidence_mapping_coverage"], 2) == 0.5
    print("self_check=pass")


if __name__ == "__main__":
    main()
