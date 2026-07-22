#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "rebound_leader_eastmoney_fund_flow_probe_v5_22"
DEBUG = OUT / "debug"
DEFAULT_SYMBOLS = ["汽车服务", "半导体", "银行"]


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.22 live probe for Eastmoney historical industry fund-flow source.")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--skip-live-probe", action="store_true", help="Write a deterministic blocked audit without network calls.")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    probes = build_blocked_probe_rows() if args.skip_live_probe else run_live_probes()
    columns = build_column_summary(probes)
    checks = build_readiness_checks(probes)
    summary = build_summary(probes, checks)
    write_outputs(summary, probes, columns, checks)
    print(f"output_dir={OUT}")
    print(f"historical_source_ready={summary['historical_source_ready']}")
    print(f"successful_hist_probe_count={summary['successful_hist_probe_count']}")


def run_live_probes() -> pd.DataFrame:
    import akshare as ak

    rows: list[dict[str, Any]] = []
    rank_result = capture_probe(
        "eastmoney_sector_rank_today",
        "东方财富行业资金流排名",
        "rank_current_or_rolling",
        lambda: ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流"),
    )
    rows.append(rank_result)
    symbols = rank_symbols(rank_result)[:3] or DEFAULT_SYMBOLS
    for symbol in symbols:
        rows.append(capture_probe(
            f"eastmoney_sector_hist_{symbol}",
            f"东方财富行业历史资金流-{symbol}",
            "candidate_historical",
            lambda symbol=symbol: ak.stock_sector_fund_flow_hist(symbol=symbol),
        ))
    return pd.DataFrame(rows)


def capture_probe(source_id: str, source_name: str, pit_boundary: str, fn: Callable[[], pd.DataFrame]) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        frame = fn()
        error = ""
        status = "pass" if isinstance(frame, pd.DataFrame) and not frame.empty else "fail"
    except Exception as exc:
        frame = pd.DataFrame()
        error = f"{type(exc).__name__}: {exc}"
        status = "fail"
    elapsed = round(time.perf_counter() - start, 3)
    cols = list(frame.columns) if isinstance(frame, pd.DataFrame) else []
    date_col = first_matching(cols, ["日期", "date", "时间"])
    name_col = first_matching(cols, ["名称", "行业", "板块", "name"])
    date_count = date_unique_count(frame, date_col)
    return {
        "source_id": source_id,
        "source_name": source_name,
        "pit_boundary": pit_boundary,
        "probe_status": status,
        "row_count": int(len(frame)) if isinstance(frame, pd.DataFrame) else 0,
        "column_count": int(len(cols)),
        "has_date_column": bool(date_col),
        "date_column": date_col,
        "date_count": date_count,
        "has_name_column": bool(name_col),
        "name_column": name_col,
        "columns": "|".join(map(str, cols[:30])),
        "sample_names": sample_values(frame, name_col),
        "date_min": date_minmax(frame, date_col, "min"),
        "date_max": date_minmax(frame, date_col, "max"),
        "duration_seconds": elapsed,
        "error": error[:600],
        "can_enter_sw2_mapping_audit": bool(status == "pass" and date_count >= 60),
    }


def rank_symbols(rank_result: dict[str, Any]) -> list[str]:
    names = str(rank_result.get("sample_names", "")).split("|")
    return [name for name in names if name]


def build_blocked_probe_rows() -> pd.DataFrame:
    return pd.DataFrame([{
        "source_id": "eastmoney_sector_hist_skipped",
        "source_name": "东方财富行业历史资金流",
        "pit_boundary": "candidate_historical",
        "probe_status": "blocked",
        "row_count": 0,
        "column_count": 0,
        "has_date_column": False,
        "date_column": "",
        "date_count": 0,
        "has_name_column": False,
        "name_column": "",
        "columns": "",
        "sample_names": "",
        "date_min": "",
        "date_max": "",
        "duration_seconds": 0,
        "error": "skip_live_probe=true",
        "can_enter_sw2_mapping_audit": False,
    }])


def build_column_summary(probes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in probes.iterrows():
        for column in str(row.get("columns", "")).split("|"):
            if column:
                rows.append({"source_id": row["source_id"], "column": column})
    return pd.DataFrame(rows or [{"source_id": "", "column": ""}])


def build_readiness_checks(probes: pd.DataFrame) -> pd.DataFrame:
    hist = probes[probes["pit_boundary"].eq("candidate_historical")]
    successful = hist[hist["probe_status"].eq("pass")]
    mappable = hist[hist["can_enter_sw2_mapping_audit"].eq(True)]
    return pd.DataFrame([
        check("hist_probe_success", "pass" if len(successful) else "fail", f"successful_hist_probe_count={len(successful)}", "历史资金流接口必须能成功返回。"),
        check("hist_date_depth", "pass" if int(hist["date_count"].max() if len(hist) else 0) >= 60 else "fail", f"max_date_count={int(hist['date_count'].max() if len(hist) else 0)}; required=60", "至少要有足够历史日期，才值得做映射审计。"),
        check("sw2_mapping_audit_ready", "pass" if len(mappable) else "fail", f"mappable_hist_probe_count={len(mappable)}", "只有成功历史源才能进入东方财富行业到申万二级映射。"),
        check("strong_industry_backtest_ready", "pending" if len(mappable) else "fail", f"ready_after_mapping=false; mappable_hist_probe_count={len(mappable)}", "即使源可用，也要先通过映射和覆盖审计，不能直接声明可回测。"),
    ])


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_summary(probes: pd.DataFrame, checks: pd.DataFrame) -> dict[str, Any]:
    hist = probes[probes["pit_boundary"].eq("candidate_historical")]
    successful = int(hist["probe_status"].eq("pass").sum())
    mappable = int(hist["can_enter_sw2_mapping_audit"].eq(True).sum())
    ready = mappable > 0 and not checks["status"].eq("fail").any()
    return {
        "version": "5.22.0",
        "policy_id": "rebound_leader_eastmoney_fund_flow_probe_v5_22",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_source_count": int(len(probes)),
        "successful_hist_probe_count": successful,
        "mappable_hist_probe_count": mappable,
        "historical_source_ready": ready,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "pass_count": int(checks["status"].eq("pass").sum()),
        "fail_count": int(checks["status"].eq("fail").sum()),
        "pending_count": int(checks["status"].eq("pending").sum()),
        "best_status": "ready_for_mapping_audit" if mappable else "research_only_eastmoney_fund_flow_not_ready",
        "evidence_boundary": "source_probe_only",
        "final_verdict": final_verdict(successful, mappable),
    }


def final_verdict(successful: int, mappable: int) -> str:
    if mappable:
        return "V5.22 东方财富行业历史资金流探针显示：接口已返回带历史日期的数据，可进入下一步行业映射和覆盖审计；但尚未完成申万二级映射和强行业回测，不能声称目标完成。"
    if successful:
        return "V5.22 东方财富行业历史资金流探针显示：接口有成功返回，但历史日期深度或字段条件不足，暂不能进入强行业回测。"
    return "V5.22 东方财富行业历史资金流探针显示：当前接口未能提供可用历史资金流样本，不能作为强反弹行业选择的新 PIT 数据源。"


def write_outputs(summary: dict[str, Any], probes: pd.DataFrame, columns: pd.DataFrame, checks: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    probes.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, probes, checks), encoding="utf-8")
    probes.to_csv(DEBUG / "source_probe_results.csv", index=False, encoding="utf-8-sig")
    columns.to_csv(DEBUG / "sample_columns.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(DEBUG / "source_readiness_checks.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], probes: pd.DataFrame, checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.22 东方财富行业历史资金流探针",
        "",
        summary["final_verdict"],
        "",
        f"- 历史接口成功样本数：{summary['successful_hist_probe_count']}",
        f"- 可进入映射审计的历史样本数：{summary['mappable_hist_probe_count']}",
        f"- 是否可进入强行业回测：`{str(summary['historical_source_ready']).lower()}`",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 探针结果",
        "",
        probes.to_markdown(index=False),
        "",
        "## 就绪度检查",
        "",
        checks.to_markdown(index=False),
        "",
        "边界：V5.22 只验证东方财富行业资金流源是否可用；即使源成功，也必须先做申万二级映射、覆盖率和泄漏审计，之后才能回测强行业选择。",
    ])


def first_matching(columns: list[Any], keys: list[str]) -> str:
    for column in map(str, columns):
        lowered = column.lower()
        if any(key.lower() in lowered for key in keys):
            return column
    return ""


def date_unique_count(frame: pd.DataFrame, column: str) -> int:
    if not column or column not in frame:
        return 0
    return int(pd.to_datetime(frame[column], errors="coerce").dropna().nunique())


def date_minmax(frame: pd.DataFrame, column: str, method: str) -> str:
    if not column or column not in frame:
        return ""
    dates = pd.to_datetime(frame[column], errors="coerce").dropna()
    if dates.empty:
        return ""
    return str(getattr(dates, method)().date())


def sample_values(frame: pd.DataFrame, column: str) -> str:
    if not column or column not in frame:
        return ""
    return "|".join(map(str, frame[column].dropna().astype(str).head(5).tolist()))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    probes = pd.DataFrame([
        {"pit_boundary": "candidate_historical", "probe_status": "pass", "date_count": 80, "can_enter_sw2_mapping_audit": True},
    ])
    checks = build_readiness_checks(probes)
    summary = build_summary(probes, checks)
    assert summary["mappable_hist_probe_count"] == 1
    assert summary["can_claim_strong_rebound_industries"] is False
    assert "source_probe_only" == summary["evidence_boundary"]
    print("self_check=pass")


if __name__ == "__main__":
    main()
