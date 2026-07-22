#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "rebound_leader_new_pit_source_discovery_v5_21"
DEBUG = OUT / "debug"
CACHE = ROOT / "data_catalog" / "cache"


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.21 audit for new PIT sources usable in rebound-leader validation.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    sources = build_candidate_sources()
    checks = build_readiness_checks(sources)
    actions = build_next_actions(sources)
    summary = build_summary(sources, checks)
    write_outputs(summary, sources, checks, actions)
    print(f"output_dir={OUT}")
    print(f"historical_backtest_ready_source_count={summary['historical_backtest_ready_source_count']}")
    print(f"new_pit_alpha_ready={summary['new_pit_alpha_ready']}")


def build_candidate_sources() -> pd.DataFrame:
    fund_flow_summary = read_json(ROOT / "outputs" / "industry_rebound_leader_new_pit_source_v4_82" / "run_summary.json")
    fund_flow_cache = read_json(ROOT / "outputs" / "audit" / "industry_fund_flow_cache_snapshot" / "run_summary.json")
    eastmoney_probe = read_json(ROOT / "outputs" / "audit" / "rebound_leader_eastmoney_fund_flow_probe_v5_22" / "run_summary.json")
    rows = [
        source_row(
            "sw2_valuation_history",
            "申万二级估值历史",
            "valuation",
            "historical_pit",
            "ready_but_already_failed",
            "申万二级原生",
            valuation_history_exists(),
            "V5.11/V5.12 已验证未通过强行业门槛，不再作为新信息源。",
            "hold",
            "不继续调估值权重，除非新增更长或更高质量 PIT 估值字段。",
        ),
        source_row(
            "sw2_price_amount_history",
            "申万二级价格和成交额历史",
            "price_volume",
            "historical_pit",
            "ready_but_already_failed",
            "申万二级原生",
            sw2_history_file_count() >= 100,
            "价格、超跌、企稳、成交额已在 V5.13-V5.19 多轮失败。",
            "hold",
            "不再只靠价格/成交额做微调。",
        ),
        source_row(
            "ths_industry_fund_flow_snapshots",
            "同花顺行业资金流快照",
            "fund_flow",
            "current_or_rolling_snapshot",
            "not_enough_history",
            "需映射到申万二级",
            fund_flow_trade_date_count() >= 60 and float(fund_flow_cache.get("mapping_coverage", 0) or 0) >= 0.8,
            f"cached_trade_dates={fund_flow_trade_date_count()}; mapping_coverage={fund_flow_cache.get('mapping_coverage', '')}",
            "collect_forward",
            "继续每日缓存；满 60 个交易日后只做观察，满 252 个交易日再考虑 alpha 验证。",
        ),
        source_row(
            "eastmoney_sector_fund_flow_hist",
            "东方财富行业历史资金流",
            "fund_flow",
            "candidate_historical",
            "source_not_stable",
            "东方财富行业体系，需映射到申万二级",
            False,
            f"V5.22 successful_hist_probe_count={eastmoney_probe.get('successful_hist_probe_count', '')}; V4.82 blocked={fund_flow_summary.get('source_blocked_ids', '')}",
            "probe_later",
            "网络或接口稳定后先重跑源审计，不直接接入回测。",
        ),
        source_row(
            "cninfo_industry_pe_ratio",
            "巨潮行业市盈率",
            "valuation",
            "date_query_candidate",
            "taxonomy_mismatch",
            "证监会/国证行业，非申万二级",
            False,
            f"akshare_function_exists={ak_func_exists('stock_industry_pe_ratio_cninfo')}",
            "map_or_skip",
            "只有能稳定按日期批量取数且映射到申万二级后才可测试。",
        ),
        source_row(
            "market_margin_financing",
            "两融余额市场级历史",
            "market_state",
            "historical_pit",
            "market_state_only",
            "非行业级",
            False,
            f"cache_exists={cache_file_exists('external_risk/v3_5/margin_sh.csv') and cache_file_exists('external_risk/v3_5/margin_sz.csv')}",
            "window_filter_only",
            "只能辅助反弹窗口状态，不能证明选出更强行业。",
        ),
        source_row(
            "northbound_flow_market",
            "北向资金市场级历史",
            "market_state",
            "historical_pit",
            "market_state_only",
            "非行业级",
            False,
            f"cache_exists={cache_file_exists('external_risk/v4_10/northbound_flow.csv')}",
            "window_filter_only",
            "只能辅助市场状态，不能作为行业强弱排序证据。",
        ),
        source_row(
            "sw_component_and_profit_forecast",
            "申万成分股与盈利预测聚合",
            "fundamental_revision",
            "candidate_if_snapshotted",
            "no_historical_snapshot",
            "可聚合到申万二级，但需要 as-of 快照",
            False,
            f"component_func={ak_func_exists('index_component_sw')}; forecast_func={ak_func_exists('stock_profit_forecast_ths')}",
            "snapshot_first",
            "先设计每日快照，不允许把当前盈利预测回填历史。",
        ),
        source_row(
            "sw_industry_classification_history",
            "申万行业分类变动历史",
            "metadata",
            "historical_reference",
            "not_alpha_feature",
            "申万体系",
            False,
            f"akshare_function_exists={ak_func_exists('stock_industry_clf_hist_sw')}",
            "use_as_mapping_audit",
            "用于成分和分类口径审计，不单独作为强反弹因子。",
        ),
    ]
    return pd.DataFrame(rows).sort_values(["priority_rank", "source_id"]).drop(columns=["priority_rank"])


def source_row(
    source_id: str,
    source_name: str,
    feature_family: str,
    pit_boundary: str,
    readiness_status: str,
    sw2_mapping_status: str,
    can_backtest: bool,
    evidence: str,
    decision: str,
    next_action: str,
) -> dict[str, Any]:
    ready = bool(can_backtest and readiness_status == "ready_for_historical_backtest")
    return {
        "source_id": source_id,
        "source_name": source_name,
        "feature_family": feature_family,
        "pit_boundary": pit_boundary,
        "readiness_status": readiness_status,
        "sw2_mapping_status": sw2_mapping_status,
        "can_backtest_stronger_industry": ready,
        "priority": "P0" if decision in {"collect_forward", "probe_later", "snapshot_first"} else "P1",
        "priority_rank": 0 if decision in {"collect_forward", "probe_later", "snapshot_first"} else 1,
        "decision": decision,
        "evidence": evidence,
        "next_action": next_action,
    }


def build_readiness_checks(sources: pd.DataFrame) -> pd.DataFrame:
    ready = int(sources["can_backtest_stronger_industry"].sum())
    new_candidates = sources[~sources["readiness_status"].isin(["ready_but_already_failed", "not_alpha_feature"])]
    return pd.DataFrame([
        check("new_pit_source_ready_for_backtest", "fail" if ready == 0 else "pass", f"ready_source_count={ready}", "必须有可历史/PIT、可映射申万二级、日期覆盖足够的新源。"),
        check("fund_flow_cache_depth", "fail" if fund_flow_trade_date_count() < 60 else "pending", f"cached_trade_dates={fund_flow_trade_date_count()}; required_observation=60; required_alpha=252", "资金流当前只能继续累计，不能回填。"),
        check("historical_candidate_source_exists", "pending" if len(new_candidates) else "fail", f"candidate_count={len(new_candidates)}", "存在候选源不等于可回测，仍需源稳定性和映射审计。"),
        check("market_state_sources_not_enough", "fail", "margin/northbound are market-level only", "市场级源最多改善窗口，不证明窗口内选行业 alpha。"),
    ])


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_next_actions(sources: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([
        {"priority": "P0", "action": "继续每日缓存同花顺行业资金流", "why": f"当前只有 {fund_flow_trade_date_count()} 个交易日，不能回填历史。", "acceptance": ">=60 个交易日后做观察审计；>=252 个交易日后做 alpha 审计。"},
        {"priority": "P0", "action": "重审东方财富行业历史资金流接口", "why": "这是最可能直接提供历史行业资金流的免费源，但 V4.82/V5.22 接口失败。", "acceptance": "连续多次取数成功，字段含历史日期，且能映射到申万二级。"},
        {"priority": "P0", "action": "建立盈利预测/成分股每日快照方案", "why": "盈利修正可能比价格/估值更接近强反弹行业选择，但不能用当前预测回填历史。", "acceptance": "快照带 available_date，至少覆盖申万二级主要成分。"},
        {"priority": "P1", "action": "把两融和北向只保留为窗口状态变量", "why": "它们不是行业级源，不能单独完成强行业选择目标。", "acceptance": "只进入窗口过滤审计，不计入 strong-industry source ready。"},
    ])


def build_summary(sources: pd.DataFrame, checks: pd.DataFrame) -> dict[str, Any]:
    ready = int(sources["can_backtest_stronger_industry"].sum())
    return {
        "version": "5.21.0",
        "policy_id": "rebound_leader_new_pit_source_discovery_v5_21",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_source_count": int(len(sources)),
        "historical_backtest_ready_source_count": ready,
        "new_pit_alpha_ready": ready > 0,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "pass_count": int(checks["status"].eq("pass").sum()),
        "fail_count": int(checks["status"].eq("fail").sum()),
        "pending_count": int(checks["status"].eq("pending").sum()),
        "best_status": "research_only_new_pit_source_not_ready",
        "evidence_boundary": "new_pit_source_not_ready",
        "final_verdict": "V5.21 新 PIT 数据源发现审计显示：当前没有新的、可直接历史回测并映射到申万二级的 PIT 行业级数据源；下一步应优先累计资金流快照、重审东方财富历史资金流、建立盈利预测快照，而不是继续微调已失败的本地价格/估值/成交额字段。",
    }


def write_outputs(summary: dict[str, Any], sources: pd.DataFrame, checks: pd.DataFrame, actions: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    sources.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, sources, checks, actions), encoding="utf-8")
    sources.to_csv(DEBUG / "candidate_pit_sources.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(DEBUG / "source_readiness_checks.csv", index=False, encoding="utf-8-sig")
    actions.to_csv(DEBUG / "next_source_actions.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], sources: pd.DataFrame, checks: pd.DataFrame, actions: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.21 新 PIT 数据源发现审计",
        "",
        summary["final_verdict"],
        "",
        f"- 候选源数量：{summary['candidate_source_count']}",
        f"- 可直接进入强行业历史回测的新源数量：{summary['historical_backtest_ready_source_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        f"- 证据边界：`{summary['evidence_boundary']}`",
        "",
        "## 候选 PIT 源",
        "",
        sources.to_markdown(index=False),
        "",
        "## 就绪度检查",
        "",
        checks.to_markdown(index=False),
        "",
        "## 下一步",
        "",
        actions.to_markdown(index=False),
        "",
        "边界：V5.21 只做数据源发现审计，不新增交易规则；没有可回测新源前，不应继续把已失败字段调参成新版本。",
    ])


def valuation_history_exists() -> bool:
    return (CACHE / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv").exists()


def sw2_history_file_count() -> int:
    path = CACHE / "industry_index" / "history" / "second"
    return len(list(path.glob("*.csv"))) if path.exists() else 0


def fund_flow_trade_date_count() -> int:
    path = CACHE / "industry_fund_flow" / "ths"
    if not path.exists():
        return 0
    return sum(1 for child in path.iterdir() if child.is_dir())


def cache_file_exists(relative: str) -> bool:
    return (CACHE / relative).exists()


def ak_func_exists(name: str) -> bool:
    try:
        akshare = importlib.import_module("akshare")
    except Exception:
        return False
    return hasattr(akshare, name)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    sources = pd.DataFrame([
        {"source_id": "x", "readiness_status": "not_enough_history", "can_backtest_stronger_industry": False},
        {"source_id": "y", "readiness_status": "market_state_only", "can_backtest_stronger_industry": False},
    ])
    checks = build_readiness_checks(sources)
    summary = build_summary(sources, checks)
    assert summary["new_pit_alpha_ready"] is False
    assert summary["goal_ready"] is False
    assert "new_pit_source_not_ready" == summary["evidence_boundary"]
    print("self_check=pass")


if __name__ == "__main__":
    main()
