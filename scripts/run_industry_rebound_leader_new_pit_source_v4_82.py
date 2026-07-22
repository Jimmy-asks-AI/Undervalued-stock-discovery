#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "industry_rebound_leader_new_pit_source_v4_82"
DEBUG = OUT / "debug"
SOURCE_AUDIT = ROOT / "outputs" / "audit" / "industry_fund_flow_source_audit" / "debug" / "source_attempts.csv"
FUND_FLOW_CACHE = ROOT / "data_catalog" / "cache" / "industry_fund_flow" / "ths"
MAPPING = ROOT / "configs" / "industry_fund_flow_ths_sw2_mapping.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.82 new PIT source readiness audit for rebound leader selection.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    sources = build_source_readiness(read_source_attempts())
    cache = build_cache_audit()
    mapping = build_mapping_audit()
    plan = build_collection_plan(sources, cache, mapping)
    summary = build_summary(sources, cache, mapping)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    plan.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, sources, cache, mapping, plan), encoding="utf-8")
    sources.to_csv(DEBUG / "source_readiness_audit.csv", index=False, encoding="utf-8-sig")
    cache.to_csv(DEBUG / "fund_flow_cache_audit.csv", index=False, encoding="utf-8-sig")
    mapping.to_csv(DEBUG / "mapping_readiness_audit.csv", index=False, encoding="utf-8-sig")
    plan.to_csv(DEBUG / "pit_collection_plan.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"new_pit_alpha_ready={summary['new_pit_alpha_ready']}")
    print(f"historical_backtest_ready_source_count={summary['historical_backtest_ready_source_count']}")


def read_source_attempts() -> pd.DataFrame:
    if not SOURCE_AUDIT.exists():
        return pd.DataFrame(columns=["source_id", "status", "pit_boundary", "row_count", "error"])
    return pd.read_csv(SOURCE_AUDIT, encoding="utf-8-sig")


def build_source_readiness(source_attempts: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, row in source_attempts.iterrows():
        source_id = str(row.get("source_id", ""))
        status = str(row.get("status", ""))
        pit = str(row.get("pit_boundary", ""))
        historical = "historical" in pit
        rolling_only = "rolling" in pit or "current" in pit
        can_backtest = bool(status == "pass" and historical and not rolling_only)
        rows.append({
            "source_id": source_id,
            "source_status": status,
            "pit_boundary": pit,
            "row_count": int(float(row.get("row_count", 0) or 0)),
            "historical_candidate": historical,
            "current_or_rolling_only": rolling_only,
            "can_backtest_stronger_industry": can_backtest,
            "readiness_status": "pass_historical_pit" if can_backtest else "blocked",
            "blocked_reason": "" if can_backtest else blocked_reason(row),
        })
    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame([{
        "source_id": "source_audit_missing",
        "source_status": "missing",
        "pit_boundary": "",
        "row_count": 0,
        "historical_candidate": False,
        "current_or_rolling_only": False,
        "can_backtest_stronger_industry": False,
        "readiness_status": "blocked",
        "blocked_reason": "缺少资金流数据源审计结果。",
    }])


def blocked_reason(row: pd.Series) -> str:
    status = str(row.get("status", ""))
    pit = str(row.get("pit_boundary", ""))
    if status != "pass":
        return "接口当前未成功返回数据，不能作为历史 PIT 验证输入。"
    if "current" in pit or "rolling" in pit:
        return "仅当前或滚动窗口字段，不能回填历史验证强反弹行业。"
    return "未满足历史 PIT 回测边界。"


def build_cache_audit() -> pd.DataFrame:
    dates: list[str] = []
    if FUND_FLOW_CACHE.exists():
        dates = sorted(
            child.name
            for child in FUND_FLOW_CACHE.iterdir()
            if child.is_dir() and (child / "ths_industry_fund_flow_now.csv").exists()
        )
    rows = [
        ("cached_trade_date_count", len(dates), 60, ">=", "研究观察至少需要 60 个 PIT 快照交易日。"),
        ("alpha_cache_trade_date_count", len(dates), 252, ">=", "强行业 alpha 验证至少需要 252 个 PIT 快照交易日。"),
        ("latest_cache_date_present", 1 if dates else 0, 1, ">=", "至少需要一个当前快照作为前推观察起点。"),
    ]
    return pd.DataFrame([
        {
            "audit_item": item,
            "current": current,
            "required": required,
            "operator": op,
            "status": "pass" if compare(current, required, op) else "fail",
            "evidence": evidence + (f" latest={dates[-1]}" if dates else ""),
        }
        for item, current, required, op, evidence in rows
    ])


def build_mapping_audit() -> pd.DataFrame:
    if not MAPPING.exists():
        return pd.DataFrame([{
            "audit_item": "ths_to_sw2_mapping",
            "current": 0,
            "required": 0.8,
            "operator": ">=",
            "status": "fail",
            "evidence": "缺少同花顺行业到申万二级映射表。",
        }])
    mapping = pd.read_csv(MAPPING, encoding="utf-8-sig", dtype=str)
    total = len(mapping)
    reviewed = mapping["review_status"].fillna("").str.contains("exact|normalized|manual", case=False, regex=True).sum() if "review_status" in mapping.columns else 0
    production = mapping["production_allowed"].fillna("").isin(["是", "true", "True", "1", "yes"]).sum() if "production_allowed" in mapping.columns else 0
    confidence = pd.to_numeric(mapping.get("mapping_confidence", pd.Series(dtype=float)), errors="coerce")
    high_confidence = int(confidence.ge(0.95).sum())
    medium_confidence = int(confidence.ge(0.80).sum())
    high_coverage = high_confidence / total if total else 0.0
    medium_coverage = medium_confidence / total if total else 0.0
    production_coverage = production / total if total else 0.0
    return pd.DataFrame([
        mapping_row("high_confidence_mapping_coverage", high_coverage, high_confidence, total, reviewed, 0.8),
        mapping_row("medium_confidence_mapping_coverage", medium_coverage, medium_confidence, total, reviewed, 0.8),
        {
            "audit_item": "production_allowed_mapping_coverage",
            "current": production_coverage,
            "required": 0.8,
            "operator": ">=",
            "status": "pass" if production_coverage >= 0.8 else "fail",
            "evidence": f"production_allowed={production}; total={total}",
        },
    ])


def mapping_row(item: str, coverage: float, count: int, total: int, reviewed: int, required: float) -> dict[str, object]:
    return {
        "audit_item": item,
        "current": coverage,
        "required": required,
        "operator": ">=",
        "status": "pass" if coverage >= required else "fail",
        "evidence": f"matched={count}; total={total}; reviewed={reviewed}",
    }


def build_collection_plan(sources: pd.DataFrame, cache: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "priority": "P0",
            "work_item": "继续每日缓存同花顺行业资金流 now/5d",
            "current_status": cache_status(cache),
            "required_for_goal": "至少 60 个交易日可做观察，252 个交易日才能进入强行业 alpha 验证。",
            "next_action": "在每日 live refresh 中保留 cache_industry_fund_flow_snapshot.py。",
        },
        {
            "priority": "P0",
            "work_item": "修复或替代东方财富行业历史资金流接口",
            "current_status": source_status(sources, "eastmoney_sector_hist"),
            "required_for_goal": "若能稳定取历史行业资金流，可直接进入 V4.x 强行业验证。",
            "next_action": "当前 ProxyError；不把失败接口接入回测。后续换网络或替代源后重审。",
        },
        {
            "priority": "P1",
            "work_item": "提高同花顺行业到申万二级映射覆盖",
            "current_status": mapping_status(mapping),
            "required_for_goal": "映射覆盖不足会导致资金流观察不能对应研究行业。",
            "next_action": "补齐低置信度和缺失映射后再解除资金流门禁。",
        },
        {
            "priority": "P1",
            "work_item": "把资金流只读观察并入前推账本",
            "current_status": "current_only_observation",
            "required_for_goal": "只能用接入日起的真实未来收益评估，不回填历史。",
            "next_action": "保留 planned/entered/skipped 账本，退出日后结算真实 forward return。",
        },
    ])


def build_summary(sources: pd.DataFrame, cache: pd.DataFrame, mapping: pd.DataFrame) -> dict[str, object]:
    historical_ready = int(sources["can_backtest_stronger_industry"].sum()) if len(sources) else 0
    cache_days = int(cache.loc[cache["audit_item"].eq("cached_trade_date_count"), "current"].iloc[0]) if len(cache) else 0
    source_failures = ";".join(sources.loc[sources["readiness_status"].ne("pass_historical_pit"), "source_id"].astype(str).tolist())
    failed_cache = ";".join(cache.loc[cache["status"].eq("fail"), "audit_item"].astype(str).tolist())
    failed_mapping = ";".join(mapping.loc[mapping["status"].eq("fail"), "audit_item"].astype(str).tolist()) if len(mapping) else "mapping_missing"
    ready = historical_ready > 0 or (cache_days >= 252 and not failed_mapping)
    return {
        "version": "4.82.0",
        "policy_id": "industry_rebound_leader_new_pit_source_v4_82",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "historical_backtest_ready_source_count": historical_ready,
        "fund_flow_cache_trade_date_count": cache_days,
        "source_blocked_ids": source_failures,
        "failed_cache_metrics": failed_cache,
        "failed_mapping_metrics": failed_mapping,
        "new_pit_alpha_ready": ready,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "ready_for_new_pit_validation" if ready else "research_only_new_pit_source_not_ready",
        "final_verdict": (
            "已有新 PIT 信息源足以进入强反弹行业验证。"
            if ready else
            "当前新 PIT 信息源仍不足以验证强反弹行业；只能继续缓存和前推观察。"
        ),
    }


def cache_status(cache: pd.DataFrame) -> str:
    if cache.empty:
        return "missing_cache_audit"
    days = int(cache.loc[cache["audit_item"].eq("cached_trade_date_count"), "current"].iloc[0])
    return f"cached_trade_dates={days}"


def source_status(sources: pd.DataFrame, source_id: str) -> str:
    row = sources[sources["source_id"].eq(source_id)]
    if row.empty:
        return "missing_source_attempt"
    item = row.iloc[0]
    return f"{item['source_status']}; {item['blocked_reason']}"


def mapping_status(mapping: pd.DataFrame) -> str:
    if mapping.empty:
        return "missing_mapping"
    return "; ".join(f"{row.audit_item}={row.status}({row.current})" for row in mapping.itertuples())


def compare(current: object, required: object, op: str) -> bool:
    current_f = float(current or 0)
    required_f = float(required)
    return current_f >= required_f if op == ">=" else current_f > required_f


def render_report(
    summary: dict[str, object],
    sources: pd.DataFrame,
    cache: pd.DataFrame,
    mapping: pd.DataFrame,
    plan: pd.DataFrame,
) -> str:
    return "\n".join([
        "# V4.82 新 PIT 信息源强行业验证资格审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 读取行业资金流数据源审计、同花顺资金流本地缓存和同花顺到申万二级映射表。",
        "- 判断是否存在可用于历史强行业选择验证的 PIT 信息源。",
        "- 当前或滚动窗口资金流只允许进入前推观察，不允许回填历史回测。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 数据源资格",
        "",
        table(sources),
        "",
        "## 缓存资格",
        "",
        table(cache),
        "",
        "## 映射资格",
        "",
        table(mapping),
        "",
        "## 下一步计划",
        "",
        table(plan),
        "",
        "## 研究边界",
        "",
        "V4.80/V4.81 已证明现有价格、估值、流动性和市场状态特征不足以稳定选出强反弹行业。V4.82 的作用是确认新 PIT 信息源是否已经可用；在历史资金流或足够长的每日缓存形成前，不能声称已经找到强反弹行业。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    sources = build_source_readiness(pd.DataFrame([
        {"source_id": "hist", "status": "pass", "pit_boundary": "candidate_historical", "row_count": 10, "error": ""},
        {"source_id": "now", "status": "pass", "pit_boundary": "current_only", "row_count": 10, "error": ""},
    ]))
    assert bool(sources.loc[sources["source_id"].eq("hist"), "can_backtest_stronger_industry"].iloc[0])
    assert not bool(sources.loc[sources["source_id"].eq("now"), "can_backtest_stronger_industry"].iloc[0])
    print("self_check=pass")


if __name__ == "__main__":
    main()
