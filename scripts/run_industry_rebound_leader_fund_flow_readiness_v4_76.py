#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data_catalog" / "cache" / "industry_fund_flow" / "ths"
OVERLAY = ROOT / "outputs" / "audit" / "v4_72_candidate_fund_flow_overlay" / "debug" / "candidate_fund_flow_overlay.csv"
MAPPING_SUMMARY = ROOT / "outputs" / "audit" / "industry_fund_flow_mapping_audit" / "run_summary.json"
V475_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_walk_forward_v4_75" / "run_summary.json"
OUT = ROOT / "outputs" / "industry_rebound_leader_fund_flow_readiness_v4_76"
DEBUG = OUT / "debug"

MIN_PIT_SNAPSHOTS_FOR_RESEARCH = 60
MIN_PIT_SNAPSHOTS_FOR_ALPHA = 252
MIN_EXACT_COVERAGE = 0.80


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.76 fund-flow readiness audit for rebound-leader selection.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    overlay = pd.read_csv(OVERLAY, encoding="utf-8-sig", dtype={"industry_code": str})
    cache_dates = cached_dates(CACHE)
    readiness = readiness_rows(overlay, cache_dates, read_json(MAPPING_SUMMARY), read_json(V475_SUMMARY))
    candidates = current_observation_candidates(overlay)
    summary = build_summary(readiness, candidates, cache_dates)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    candidates.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, readiness, candidates), encoding="utf-8")
    readiness.to_csv(DEBUG / "fund_flow_readiness_audit.csv", index=False, encoding="utf-8-sig")
    overlay.to_csv(DEBUG / "candidate_fund_flow_inputs.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(DEBUG / "current_fund_flow_observation_candidates.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"fund_flow_alpha_ready={summary['fund_flow_alpha_ready']}")
    print(f"current_dual_positive_count={summary['current_dual_positive_count']}")


def cached_dates(cache: Path) -> list[str]:
    if not cache.exists():
        return []
    return sorted(path.name for path in cache.iterdir() if path.is_dir())


def readiness_rows(overlay: pd.DataFrame, cache_dates: list[str], mapping: dict, v475: dict) -> pd.DataFrame:
    exact = overlay[overlay["fund_flow_overlay_status"].eq("available_current_only")]
    proxy = overlay[overlay["fund_flow_overlay_status"].eq("proxy_current_only")]
    dual_positive = exact[
        pd.to_numeric(exact["ths_today_net_flow"], errors="coerce").gt(0)
        & pd.to_numeric(exact["ths_5d_net_flow"], errors="coerce").gt(0)
    ]
    rows = [
        row("pit_snapshot_count", len(cache_dates), f">={MIN_PIT_SNAPSHOTS_FOR_RESEARCH}", len(cache_dates) >= MIN_PIT_SNAPSHOTS_FOR_RESEARCH, "资金流历史快照太短，只能当前观察，不能做历史强行业回测。"),
        row("pit_alpha_snapshot_count", len(cache_dates), f">={MIN_PIT_SNAPSHOTS_FOR_ALPHA}", len(cache_dates) >= MIN_PIT_SNAPSHOTS_FOR_ALPHA, "强行业 alpha 至少需要覆盖多个市场状态的 PIT 快照。"),
        row("mapping_exact_coverage", float(mapping.get("exact_match_coverage", 0.0)), f">={MIN_EXACT_COVERAGE}", float(mapping.get("exact_match_coverage", 0.0)) >= MIN_EXACT_COVERAGE, "同花顺行业与申万二级口径仍未充分一致。"),
        row("candidate_exact_overlay", len(exact), f"={len(overlay)}", len(exact) == len(overlay), "候选中仍有代理资金流，不能接入强行业评价。"),
        row("candidate_proxy_overlay", len(proxy), "=0", len(proxy) == 0, "代理行业只能观察，不能解除门禁。"),
        row("current_dual_positive_flow", len(dual_positive), ">=1", len(dual_positive) >= 1, "当前没有同时满足今日和 5 日净流入为正的精确候选。"),
        row("price_factor_walk_forward_gate", str(v475.get("best_status", "")), "pass_stronger_industry_gate", v475.get("best_status") == "pass_stronger_industry_gate", "价格类逐年前推未通过，资金流不能作为锦上添花直接放行。"),
    ]
    return pd.DataFrame(rows)


def row(metric: str, current: object, required: str, ok: bool, interpretation: str) -> dict[str, object]:
    return {
        "metric": metric,
        "current": current,
        "required": required,
        "status": "pass" if ok else "fail",
        "interpretation": interpretation,
    }


def current_observation_candidates(overlay: pd.DataFrame) -> pd.DataFrame:
    out = overlay.copy()
    out["today_flow_positive"] = pd.to_numeric(out["ths_today_net_flow"], errors="coerce").gt(0)
    out["five_day_flow_positive"] = pd.to_numeric(out["ths_5d_net_flow"], errors="coerce").gt(0)
    out["dual_positive_flow"] = out["today_flow_positive"] & out["five_day_flow_positive"]
    out["fund_flow_research_status"] = out.apply(candidate_status, axis=1)
    cols = [
        "fund_flow_research_status",
        "industry_code",
        "industry_name",
        "selection_score",
        "fund_flow_overlay_status",
        "ths_industry_name",
        "ths_today_net_flow",
        "ths_5d_net_flow",
        "today_flow_positive",
        "five_day_flow_positive",
        "dual_positive_flow",
        "historical_failure_flag",
        "proxy_observation_note",
    ]
    defaults = {"historical_failure_flag": False, "proxy_observation_note": ""}
    for col in cols:
        if col not in out.columns:
            out[col] = defaults.get(col, "")
    return out[cols].sort_values(["dual_positive_flow", "today_flow_positive", "ths_today_net_flow"], ascending=[False, False, False])


def candidate_status(row: pd.Series) -> str:
    if row.get("fund_flow_overlay_status") != "available_current_only":
        return "proxy_or_missing_observation_only"
    if bool(row.get("historical_failure_flag")):
        return "current_flow_observation_with_history_risk"
    if pd.to_numeric(row.get("ths_today_net_flow"), errors="coerce") > 0 and pd.to_numeric(row.get("ths_5d_net_flow"), errors="coerce") > 0:
        return "current_dual_positive_observation_only"
    if pd.to_numeric(row.get("ths_today_net_flow"), errors="coerce") > 0:
        return "current_today_positive_observation_only"
    return "current_flow_not_confirmed"


def build_summary(readiness: pd.DataFrame, candidates: pd.DataFrame, cache_dates: list[str]) -> dict[str, object]:
    failed = readiness[readiness["status"].eq("fail")]["metric"].tolist()
    dual = candidates[candidates["dual_positive_flow"].eq(True)]
    today = candidates[candidates["today_flow_positive"].eq(True)]
    return {
        "version": "4.76.0",
        "policy_id": "industry_rebound_leader_fund_flow_readiness_v4_76",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cache_date_count": len(cache_dates),
        "latest_cache_date": cache_dates[-1] if cache_dates else "",
        "candidate_count": int(len(candidates)),
        "current_today_positive_count": int(len(today)),
        "current_dual_positive_count": int(len(dual)),
        "failed_metrics": ";".join(failed),
        "fund_flow_research_ready": "pit_snapshot_count" not in failed and "mapping_exact_coverage" not in failed,
        "fund_flow_alpha_ready": len(failed) == 0,
        "best_status": "research_only_not_validated",
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "资金流是下一步应前推积累的新信息源，但当前 PIT 历史、映射和双正向确认均不足，不能证明强反弹行业选择能力。",
    }


def render_report(summary: dict[str, object], readiness: pd.DataFrame, candidates: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.76 行业资金流强反弹验证资格审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 资格门槛",
        "",
        table(readiness),
        "",
        "## 当前资金流观察候选",
        "",
        table(candidates),
        "",
        "## 研究边界",
        "",
        "资金流当前只能从缓存日起做 PIT 前推，不能回填历史。未达到 PIT 快照数量、映射覆盖和候选确认门槛前，不接入强反弹行业评价体系。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    overlay = pd.DataFrame([
        {"fund_flow_overlay_status": "available_current_only", "ths_today_net_flow": 1.0, "ths_5d_net_flow": 2.0, "historical_failure_flag": False, "industry_code": "1", "industry_name": "A", "selection_score": 1.0, "ths_industry_name": "A", "proxy_observation_note": ""},
        {"fund_flow_overlay_status": "proxy_current_only", "ths_today_net_flow": -1.0, "ths_5d_net_flow": -2.0, "historical_failure_flag": False, "industry_code": "2", "industry_name": "B", "selection_score": 0.9, "ths_industry_name": "B", "proxy_observation_note": "proxy"},
    ])
    candidates = current_observation_candidates(overlay)
    assert int(candidates["dual_positive_flow"].sum()) == 1
    empty_overlay = overlay.iloc[:0].drop(columns=["historical_failure_flag", "proxy_observation_note"])
    empty_candidates = current_observation_candidates(empty_overlay)
    assert empty_candidates.empty
    assert {"historical_failure_flag", "proxy_observation_note"}.issubset(empty_candidates.columns)
    readiness = readiness_rows(overlay, ["2026-01-01"], {"exact_match_coverage": 0.5}, {"best_status": "research_only_not_validated"})
    assert "pit_snapshot_count" in set(readiness[readiness["status"].eq("fail")]["metric"])
    summary = build_summary(readiness, candidates, ["2026-01-01"])
    assert summary["fund_flow_alpha_ready"] is False
    print("self_check=pass")


if __name__ == "__main__":
    main()
