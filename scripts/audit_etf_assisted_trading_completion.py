#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "audit" / "etf_assisted_trading_completion"
SELF_CHECK_SUMMARY = ROOT / "outputs" / "test" / "current_mainline_self_check" / "run_summary.json"
BEHAVIOR_SUMMARY = ROOT / "outputs" / "test" / "current_mainline_behavior" / "run_summary.json"
VERIFICATION_LAYERS = ("contract", "unit", "integration", "data-quality", "research-evidence")

SOURCE_CONTRACT_CHECKS = frozenset({
    "ETF PIT劣化刷新保护",
    "账户与组合风险合同",
    "纸面人工决策审计日志",
    "逐持仓动作接口",
    "前推样本实时不可变边界",
    "前推样本严格结算边界",
    "前推证据接入当前主线",
    "单一每日刷新入口",
    "PIT方法控制接入",
    "Dashboard 当前视图",
})

VERIFICATION_LAYER_BY_CHECK = {
    "单一当前 runner": "contract",
    "统一动作状态合同": "contract",
    "ETF PIT劣化刷新保护": "contract",
    "账户与组合风险合同": "contract",
    "建议审计合同": "contract",
    "纸面人工决策审计日志": "contract",
    "逐持仓动作接口": "contract",
    "买入候选严格映射接口": "contract",
    "建议前后组合风险复算": "contract",
    "前推样本实时不可变边界": "contract",
    "前推样本严格结算边界": "contract",
    "前推证据接入当前主线": "contract",
    "单一每日刷新入口": "contract",
    "PIT方法控制接入": "contract",
    "Dashboard 当前视图": "contract",
    "当前主线自检回归": "contract",
    "工作区根目录精简": "contract",
    "Git提交边界": "contract",
    "ETF 官方主表": "data-quality",
    "ETF 官方生命周期源审计": "data-quality",
    "ETF申万成分暴露映射": "data-quality",
    "未复权成交价与前净值参考": "data-quality",
    "数据新鲜度": "data-quality",
    "ETF 当前精确映射覆盖": "data-quality",
    "申万行业 ETF 实施映射": "data-quality",
    "ETF 历史可交易宇宙": "data-quality",
    "授权历史实施数据": "data-quality",
    "历史估值与行业方法门": "data-quality",
    "真实账户状态": "data-quality",
    "前推实验哈希账本": "research-evidence",
    "择时稳健性": "research-evidence",
    "强行业选择证据": "research-evidence",
    "研究家族多重检验": "research-evidence",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="审计 ETF 辅助交易系统工程与建议就绪度。")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    current = read_json(ROOT / "outputs" / "etf_assisted_trading_current" / "run_summary.json")
    recommendation = read_json(ROOT / "outputs" / "etf_assisted_trading_current" / "debug" / "recommendation.json")
    pit = read_json(ROOT / "outputs" / "audit" / "etf_pit_master" / "run_summary.json")
    replay = read_json(ROOT / "outputs" / "audit" / "etf_realistic_execution_replay" / "run_summary.json")
    agents = read_json(ROOT / "outputs" / "etf_assisted_trading_current" / "debug" / "agent_results.json").get("agents", [])
    goal = read_json(ROOT / "outputs" / "audit" / "rebound_leader_goal_completion_audit_v5_10" / "run_summary.json")
    family = read_json(ROOT / "outputs" / "audit" / "rebound_leader_historical_backtest_verdict_v4_93" / "run_summary.json")
    ledger = read_json(ROOT / "outputs" / "audit" / "research_experiment_ledger" / "run_summary.json")
    lifecycle = read_json(ROOT / "outputs" / "audit" / "official_etf_lifecycle_sources" / "run_summary.json")
    exposure = read_json(ROOT / "outputs" / "audit" / "etf_sw_industry_exposure_mapping" / "run_summary.json")
    methodology = read_json(ROOT / "outputs" / "audit" / "pit_universe_methodology_remediation" / "run_summary.json")
    self_check_summary = read_json(SELF_CHECK_SUMMARY)
    behavior_summary = read_json(BEHAVIOR_SUMMARY)
    checks = build_checks(
        current, pit, replay, agents, goal, recommendation, family, ledger, lifecycle, exposure,
        self_check_summary, behavior_summary, methodology,
    )
    write_outputs(checks, current, self_check_summary, behavior_summary)
    summary = read_json(OUTPUT / "run_summary.json")
    print(f"implementation_pass_count={summary['implementation_pass_count']}")
    print(f"readiness_pass_count={summary['readiness_pass_count']}")
    print(f"self_check_pass_count={summary['self_check_pass_count']}")
    print(f"behavior_test_pass_count={summary['behavior_test_pass_count']}")
    print(f"manual_decision_support_ready={str(summary['manual_decision_support_ready']).lower()}")


def build_checks(current: dict[str, Any], pit: dict[str, Any], replay: dict[str, Any],
                 agents: list[dict[str, Any]], goal: dict[str, Any], recommendation: dict[str, Any],
                 family: dict[str, Any], ledger: dict[str, Any], lifecycle: dict[str, Any], exposure: dict[str, Any],
                 self_check_summary: dict[str, Any] | None = None,
                 behavior_summary: dict[str, Any] | None = None,
                 methodology: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    self_check_summary = self_check_summary or {}
    behavior_summary = behavior_summary or {}
    methodology = methodology or {}
    allowed_root_project_files = [".python-version", "CURRENT_STATUS.md", "README.md", "pyproject.toml", "uv.lock"]
    root_files = sorted(
        path.name for path in ROOT.iterdir()
        if path.is_file() and path.name not in {".gitattributes", ".gitignore"}
    )
    account_schema = (ROOT / "portfolio_lab" / "account_state_schema.json").read_text(encoding="utf-8")
    current_runner = (ROOT / "scripts" / "run_etf_assisted_trading_current.py").read_text(encoding="utf-8")
    refresh_start = current_runner.find("def refresh_input_commands")
    refresh_end = current_runner.find("def run_commands", refresh_start)
    current_refresh_block = current_runner[refresh_start:refresh_end] if refresh_start >= 0 and refresh_end > refresh_start else ""
    v507 = "build_v5_07_rebound_leader_promotion_evaluator.py"
    v521 = "build_v5_21_rebound_leader_new_pit_source_discovery.py"
    v510 = "build_v5_10_rebound_leader_goal_completion_audit.py"
    pit_method = "audit_pit_universe_methodology.py"
    v511 = "build_v5_11_rebound_leader_pit_valuation_audit.py"
    v512 = "build_v5_12_rebound_leader_pit_valuation_percentile_audit.py"
    v520 = "build_v5_20_rebound_leader_evidence_boundary_audit.py"
    current_refresh_order_valid = (
        current_refresh_block.count(v507) == 1
        and current_refresh_block.count(v521) == 1
        and current_refresh_block.count(v510) == 1
        and all(current_refresh_block.count(name) == 1 for name in [pit_method, v511, v512, v520])
        and current_refresh_block.find(pit_method) < current_refresh_block.find(v511) < current_refresh_block.find(v512) < current_refresh_block.find(v520) < current_refresh_block.find(v510)
        and current_refresh_block.find(v507) < current_refresh_block.find(v521) < current_refresh_block.find(v510)
    )
    dashboard_app = (ROOT / "strategy_lab" / "research_dashboard" / "src" / "App.tsx").read_text(encoding="utf-8")
    dashboard_chart = (ROOT / "strategy_lab" / "research_dashboard" / "src" / "ShanghaiCandlestickChart.tsx").read_text(encoding="utf-8")
    paper_recorder = (ROOT / "scripts" / "record_etf_paper_decision.py").read_text(encoding="utf-8")
    forward_detector = (ROOT / "scripts" / "build_v5_08_rebound_leader_forward_signal_detector.py").read_text(encoding="utf-8")
    forward_settlement = (ROOT / "scripts" / "settle_v5_06_rebound_leader_forward_samples.py").read_text(encoding="utf-8")
    forward_promotion = (ROOT / "scripts" / "build_v5_07_rebound_leader_promotion_evaluator.py").read_text(encoding="utf-8")
    etf_pit_builder = (ROOT / "scripts" / "build_etf_pit_master.py").read_text(encoding="utf-8")
    industry_runner = (ROOT / "scripts" / "run_industry_index_research_validation.py").read_text(encoding="utf-8")
    industry_agents = (ROOT / "strategy_lab" / "industry_index_research_os" / "agents.py").read_text(encoding="utf-8")
    market_index_runner = (ROOT / "scripts" / "run_industry_rebound_window_v3_4_realtime_model.py").read_text(encoding="utf-8")
    live_refresh = (ROOT / "scripts" / "run_v4_71_live_refresh.py").read_text(encoding="utf-8")
    current_policy = read_json(ROOT / "configs" / "etf_assisted_trading_current_policy.json")
    dashboard_data = read_json(ROOT / "strategy_lab" / "research_dashboard" / "public" / "data" / "dashboard_data.json")
    allowed_actions = {"BLOCKED_DATA", "NO_ACTION", "WATCH", "WATCH_NO_TRADEABLE_ETF", "REVIEW_REQUIRED", "BUY_CANDIDATE", "HOLD", "REDUCE", "EXIT"}
    checks = [
        row("implementation", "单一当前 runner", current.get("policy_id") == "etf_assisted_trading_current", current.get("policy_id"), "必须存在唯一当前编排入口"),
        row("implementation", "统一动作状态合同", current.get("action") in allowed_actions, current.get("action"), "动作必须来自冻结枚举"),
        row("implementation", "ETF 官方主表", int(pit.get("snapshot_row_count", 0) or 0) > 0, pit.get("snapshot_row_count"), "交易所官方快照非空"),
        row("implementation", "ETF PIT劣化刷新保护", all(name in etf_pit_builder for name in ["select_effective_snapshot", "snapshot_metrics", "degraded_refresh_last_good_retained", ".tmp", "refresh_accepted"]), f"accepted={pit.get('refresh_accepted')}; reason={pit.get('refresh_reason')}; effective={pit.get('snapshot_date')}", "不完整上游响应不得覆盖最近合格快照，且写入必须原子替换"),
        row("implementation", "ETF 官方生命周期源审计", lifecycle.get("official_bulk_sources_found") is True, lifecycle.get("announcement_count"), "两交易所官方批量公告源可访问并保留证据边界"),
        row("implementation", "ETF申万成分暴露映射", exposure.get("mapping_ready") is True and (ROOT / "data_catalog" / "etf_sw_industry_exposure_mapping.csv").exists(), f"etfs={exposure.get('high_confidence_mapping_count')}; industries={exposure.get('mapped_industry_count')}", "必须由官方指数权重和申万股票分类生成"),
        row("implementation", "真实成交约束回放", int(replay.get("filled_count", 0) or 0) > 0 and replay.get("cross_check_passed") is True, replay.get("filled_count"), "逐笔账本和内部双路径复核通过"),
        row("implementation", "未复权成交价与前净值参考", replay.get("price_adjustment") == "none" and replay.get("prior_nav_reference_coverage") == 1.0, f"adjustment={replay.get('price_adjustment')}; coverage={replay.get('prior_nav_reference_coverage')}", "真实下单价必须未复权，且每笔使用交易日前已知净值参考"),
        row("implementation", "账户与组合风险合同", (ROOT / "portfolio_lab" / "account_state_schema.json").exists() and account_contract_passed(current), f"account={current.get('account_state_gate_passed')}; risk={current.get('portfolio_risk_gate_passed')}", "账户存在时两项门禁通过，缺失时两项必须同时硬阻断"),
        row("implementation", "建议审计合同", all(key in recommendation for key in ["recommendation_id", "data_cutoff_by_source", "policy_hash", "risk_vetoes", "human_confirmation_required"]), recommendation.get("recommendation_id"), "建议必须可唯一追踪并包含策略哈希和数据截止"),
        row("implementation", "纸面人工决策审计日志", all(name in paper_recorder for name in ["recommendation_id", "policy_hash", "execution_deviation_bps", "previous_hash", "record_hash", "human_confirmation_required"]), "append-only hash chain", "人工决定与纸面成交偏差必须绑定建议并可校验，禁止自动代替用户决定"),
        row("implementation", "逐持仓动作接口", "position_recommendation_count" in current and all(name in account_schema for name in ["protective_stop_price", "bid_price", "ask_price", "iopv", "average_daily_amount_20d", "current_industry_rank"]) and "position_execution_checks" in current_runner, current.get("position_recommendation_count"), "支持持有、减仓、退出、排名保留、执行质量检查和人工复核"),
        row("implementation", "买入候选严格映射接口", "direct_industry_etf_mapping_count" in current and "buy_candidate_review_count" in current, current.get("direct_industry_etf_mapping_count"), "只接受官方跟踪指数代码精确匹配，无匹配时保持观察"),
        row("implementation", "建议前后组合风险复算", "projected_portfolio_risk" in recommendation and "projected_portfolio_risk_gate_passed" in current, current.get("projected_portfolio_risk_gate_passed"), "模型权重必须同时满足单ETF、策略总仓位和最低现金约束"),
        row("implementation", "前推实验哈希账本", ledger.get("integrity_passed") is True and int(ledger.get("experiment_count", 0) or 0) > 0 and recommendation.get("experiment_ledger_head_hash") == ledger.get("ledger_head_hash"), ledger.get("ledger_head_hash"), "建议必须绑定完整的前推实验账本头哈希"),
        row("implementation", "前推样本实时不可变边界", all(name in forward_detector for name in ["EXPERIMENT_LEDGER", "SOURCE_PANEL", "build_live_events", "load_trade_calendar", "latest_beta_top5", "MIN_INDUSTRY_COVERAGE", "forward_evidence_start", "detected_before_entry", "selection_ready", "append_allowed_samples", "--apply"]) and "expanded_window_trades.csv" not in forward_detector and all(name in live_refresh for name in ["build_v5_08_rebound_leader_forward_signal_detector.py", "--as-of-date", "--apply"]), "live as-of panel+PIT beta rank+coverage gate+ledger evidence_start+pre-entry detection+safe apply", "必须从实时面板识别新信号，行业同日覆盖达标，冻结边界来自哈希账本，且只允许入场前检测到的新信号自动追加"),
        row("implementation", "前推样本严格结算边界", all(name in forward_settlement for name in ["HISTORY_DIR", "required_dates", "MIN_INDUSTRY_COVERAGE", "pending_incomplete_benchmark", ".eq(entry)", ".eq(exit_)"]) and "sws_second_industry_daily_valuation" not in forward_settlement, "exact entry/exit dates+>=120 industry benchmark", "结算必须使用申万行业真实价格、精确入退场日期和完整横截面，不得跳到未来可用日期"),
        row("implementation", "前推证据接入当前主线", all(name in current_runner for name in ["forward_promotion_summary", "forward_detector_summary", "resolve_forward_evidence", "active_forward_candidates", "current_industry_candidates", "forward_timing_gate_passed"]) and all(name in forward_promotion for name in ["MIN_FORWARD_TIMING_EVENTS", "evaluate_forward_timing", "forward_timing_gate_passed"]) and current_policy["sources"].get("industry_candidate_file", "").endswith("selected_industry_candidates.csv"), "forward timing+industry promotion+current candidate gate", "未来前推证据达标后必须能解除历史研究阻断，并且只使用当前已触发且已晋级规则的行业候选"),
        row("implementation", "PIT方法控制接入", methodology.get("audit_passed") is True and methodology.get("promotion_gate_passed") is False and int(methodology.get("promotion_eligible_valuation_row_count", -1) or 0) == 0 and current_policy["sources"].get("pit_universe_methodology_summary", "").endswith("pit_universe_methodology_remediation/run_summary.json") and "pit_universe_methodology" in current.get("blocking_gates", []), f"audit={methodology.get('audit_passed')}; promotion={methodology.get('promotion_gate_passed')}; eligible={methodology.get('promotion_eligible_valuation_row_count')}; current_gate={current.get('pit_universe_methodology_gate_passed')}", "方法审计通过不等于证据可晋级；缺逐行可得日期或历史分类时必须进入当前主线否决门"),
        row("implementation", "单一每日刷新入口", current_refresh_order_valid and all(name in current_runner for name in ["--refresh-inputs", "refresh_input_commands", "run_commands", "build_etf_pit_master.py", "run_industry_index_research_validation.py", "run_industry_rebound_window_v3_4_realtime_model.py", "--refresh-market-index-only", "build_v5_08_rebound_leader_forward_signal_detector.py", "settle_v5_06_rebound_leader_forward_samples.py", "build_v5_07_rebound_leader_promotion_evaluator.py", "build_v5_21_rebound_leader_new_pit_source_discovery.py", "audit_pit_universe_methodology.py", "build_v5_11_rebound_leader_pit_valuation_audit.py", "build_v5_12_rebound_leader_pit_valuation_percentile_audit.py", "build_v5_20_rebound_leader_evidence_boundary_audit.py", "build_v5_10_rebound_leader_goal_completion_audit.py", "build_dashboard_dataset.py", "count_fresh_dates", "fresh_files=", "minimum_required=", "stale_files="]) and "--refresh-market-index-only" in market_index_runner and current_policy.get("required_industry_count") == 131 and current_policy.get("minimum_fresh_industry_count") == 120 and all(name in industry_runner for name in ["history_latest_date", "history_age_calendar_days", "history_fresh", "MAX_CURRENT_HISTORY_STALE_DAYS"]) and all(name in industry_agents for name in ["current history is stale", "hard block because history is short or stale", "data_rejected"]), "sixteen-input refresh chain+PIT method-before-V5.11/V5.12/V5.20/final-V5.10+131 archive coverage separate from 120 fresh price minimum", "账户日期预检通过后，一条命令刷新 16 项当前主线输入；先执行 PIT 方法审计，再生成 V5.11/V5.12/V5.20 和最终 V5.10。131 文件覆盖与 120 文件新鲜度必须分别判断"),
        row("implementation", "六角色确定性否决链", len(agents) == 6 and agents[0].get("agent") == "data_pit_steward", len(agents), "固定六角色并顺序否决"),
        row("implementation", "Dashboard 当前视图", (ROOT / "strategy_lab" / "research_dashboard" / "dist" / "index.html").exists() and bool(dashboard_data.get("current_recommendation", {}).get("action")) and len(dashboard_data.get("market_index_states", [])) == 6 and len(dashboard_data.get("historical_etf_opportunities", [])) > 0 and len(dashboard_data.get("shanghai_index_candles", [])) > 0 and len(dashboard_data.get("shanghai_index_trade_markers", [])) > 0 and all(name in dashboard_app for name in ["ETF 操作建议", "五道门槛必须同时通过", "当前阻断原因", "A股主要指数状态", "历史 ETF 买卖时点", "buyCandidates", "valueRows"]) and all(name in dashboard_chart for name in ["K线与历史 ETF 买卖时点", "createSeriesMarkers", "subscribeCrosshairMove"]), "dist+current decision+6 indices+Shanghai candles+ETF markers", "首屏展示上证综指K线、历史ETF买卖时点、时点状态和当前ETF建议"),
        row("implementation", "当前主线自检回归", self_check_summary.get("self_check_regression_passed") is True and int(self_check_summary.get("self_check_count", 0) or 0) == 12, f"pass={self_check_summary.get('self_check_pass_count', 0)}/{self_check_summary.get('self_check_count', 0)}", "12 项自检只复用各脚本已有 `--self-check`，不得计作独立行为测试", evidence_type="self_check_regression"),
        row("implementation", "工作区根目录精简", root_files == allowed_root_project_files, ",".join(root_files), "根目录项目文件只保留 README.md、CURRENT_STATUS.md 与可复现环境声明 .python-version、pyproject.toml、uv.lock；版本控制元文件另计"),
        row("readiness", "数据新鲜度", current.get("data_gate_passed") is True, current.get("data_gate_passed"), "全部必需数据达到 SLA"),
        row("readiness", "ETF 当前精确映射覆盖", pit.get("current_mapping_ready") is True, pit.get("exact_index_code_coverage"), "官方精确代码覆盖率至少95%"),
        row("readiness", "申万行业 ETF 实施映射", int(current.get("direct_industry_etf_mapping_count", 0) or 0) > 0, current.get("direct_industry_etf_mapping_count"), "至少一个研究行业存在官方跟踪指数代码直接匹配的ETF"),
        row("readiness", "ETF 历史可交易宇宙", pit.get("historical_pit_ready") is True or lifecycle.get("observed_tradability_universe_ready") is True, f"daily_pit={pit.get('historical_pit_ready')}; observed_intervals={lifecycle.get('observed_tradability_universe_ready')}", "至少60个日快照，或完整当前上市日加历史退市ETF首末交易区间"),
        row("readiness", "外部事件引擎复核", replay.get("external_event_engine_cross_check") == "pass", replay.get("external_event_engine_cross_check"), "独立事件引擎逐笔一致"),
        row("readiness", "授权历史实施数据", replay.get("historical_iopv_available") is True, f"historical_iopv={replay.get('historical_iopv_available')}", "历史Level-1盘口、IOPV和NAV必须来自可审计授权源"),
        row("readiness", "历史估值与行业方法门", current.get("pit_universe_methodology_gate_passed") is True and (methodology.get("promotion_gate_passed") is True or current.get("evidence_route") == "forward_validated"), f"current_gate={current.get('pit_universe_methodology_gate_passed')}; historical_gate={methodology.get('promotion_gate_passed')}; route={current.get('evidence_route')}; eligible={methodology.get('promotion_eligible_valuation_row_count')}", "历史估值必须逐行证明 available_date 与同期分类，或由事前登记的真实前推路线替代；否则硬阻断"),
        row("readiness", "择时稳健性", current.get("timing_gate_passed") is True, current.get("timing_gate_passed"), "参数扰动、冷却期和分状态均通过"),
        row("readiness", "强行业选择证据", current.get("industry_selection_gate_passed") is True and goal.get("goal_ready") is True and current.get("pit_universe_methodology_gate_passed") is True, f"goal={goal.get('goal_ready')}; methodology={current.get('pit_universe_methodology_gate_passed')}", "OOS/真实前推、独立事件、家族门禁和 PIT 方法门均通过"),
        row("readiness", "研究家族多重检验", int(family.get("familywise_pass_count", 0) or 0) > 0 and family.get("experiment_registration_status") != "post_hoc_historical_inventory", f"rules={family.get('family_rule_count')}; pass={family.get('familywise_pass_count')}; registration={family.get('experiment_registration_status')}", "至少一条事前登记规则通过独立簇检验和家族级校正"),
        row("readiness", "真实账户状态", current.get("account_state_gate_passed") is True, current.get("account_state_gate_passed"), "当日现金持仓和可卖数量有效"),
        row("readiness", "Git提交边界", (ROOT / ".git" / "HEAD").exists(), (ROOT / ".git" / "HEAD").exists(), "当前runner、配置、schema和回归入口必须有明确提交边界"),
        row("readiness", "六角色确定性否决链全通过", len(agents) == 6 and all(item.get("status") == "pass" for item in agents), ";".join(str(item.get("status")) for item in agents), "任何角色非 pass 均阻断"),
    ]
    checks.extend(behavior_layer_checks(behavior_summary))
    return checks


def behavior_layer_checks(summary: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for verification_layer in VERIFICATION_LAYERS:
        stats = behavior_layer_stats(summary, verification_layer)
        evidence = (
            f"pass={stats['pass_count']}/{stats['test_count']}; "
            f"fail={stats['fail_count']}; error={stats['error_count']}"
            if stats["available"]
            else "behavior summary missing" if not summary else "layer summary missing"
        )
        checks.append(row(
            "implementation",
            f"独立行为测试：{verification_layer}",
            stats["passed"],
            evidence,
            "该验证层必须存在至少一个离线行为测试，且全部通过",
            verification_layer=verification_layer,
            evidence_type="behavior_test",
        ))
    return checks


def behavior_layer_stats(summary: dict[str, Any], verification_layer: str) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    aliases = (verification_layer, verification_layer.replace("-", "_"))
    for container_name in ("verification_layers", "layer_summaries", "layers"):
        container = summary.get(container_name)
        if isinstance(container, dict):
            for alias in aliases:
                candidate = container.get(alias)
                if isinstance(candidate, dict):
                    raw = candidate
                    break
        if raw:
            break

    count = first_int(raw, "behavior_test_count", "test_count", "count")
    pass_count = first_int(raw, "behavior_test_pass_count", "pass_count", "passed_count")
    fail_count = first_int(raw, "behavior_test_fail_count", "fail_count", "failed_count")
    error_count = first_int(raw, "behavior_test_error_count", "error_count")
    available = bool(raw) and count > 0
    declared_status = str(raw.get("status", "")).lower()
    declared_passed = raw.get("passed", raw.get("behavior_tests_passed"))
    passed = bool(
        available
        and pass_count == count
        and fail_count == 0
        and error_count == 0
        and declared_status not in {"fail", "failed", "error", "pending"}
        and declared_passed is not False
    )
    return {
        "available": available,
        "test_count": count,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "error_count": error_count,
        "passed": passed,
    }


def behavior_summary_counts(summary: dict[str, Any]) -> dict[str, Any]:
    layer_stats = [behavior_layer_stats(summary, layer) for layer in VERIFICATION_LAYERS]
    count = first_int(summary, "behavior_test_count", "test_count")
    pass_count = first_int(summary, "behavior_test_pass_count", "pass_count")
    fail_count = first_int(summary, "behavior_test_fail_count", "fail_count")
    error_count = first_int(summary, "behavior_test_error_count", "error_count")
    if count == 0 and any(item["available"] for item in layer_stats):
        count = sum(item["test_count"] for item in layer_stats)
        pass_count = sum(item["pass_count"] for item in layer_stats)
        fail_count = sum(item["fail_count"] for item in layer_stats)
        error_count = sum(item["error_count"] for item in layer_stats)
    declared_passed = summary.get("behavior_tests_passed", summary.get("behavior_test_passed"))
    layers_passed = all(item["passed"] for item in layer_stats)
    passed = bool(
        summary
        and count > 0
        and pass_count == count
        and fail_count == 0
        and error_count == 0
        and layers_passed
        and declared_passed is not False
    )
    return {
        "behavior_summary_available": bool(summary),
        "behavior_test_count": count,
        "behavior_test_pass_count": pass_count,
        "behavior_test_fail_count": fail_count,
        "behavior_test_error_count": error_count,
        "behavior_tests_passed": passed,
    }


def first_int(mapping: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = mapping.get(key)
        try:
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def row(layer: str, check: str, passed: bool, evidence: Any, requirement: str,
        *, verification_layer: str | None = None, evidence_type: str | None = None) -> dict[str, Any]:
    resolved_layer = verification_layer or VERIFICATION_LAYER_BY_CHECK.get(check, "integration")
    resolved_evidence_type = evidence_type or ("source_contract" if check in SOURCE_CONTRACT_CHECKS else "runtime_evidence")
    if resolved_evidence_type == "source_contract" and resolved_layer != "contract":
        raise ValueError(f"source contract check must stay in contract layer: {check}")
    return {
        "layer": layer,
        "verification_layer": resolved_layer,
        "evidence_type": resolved_evidence_type,
        "check": check,
        "status": "pass" if passed else "fail",
        "evidence": evidence,
        "requirement": requirement,
    }


def account_contract_passed(current: dict[str, Any]) -> bool:
    return bool(
        current.get("account_state_gate_passed") is True and current.get("portfolio_risk_gate_passed") is True
        or {"account_state", "portfolio_risk"}.issubset(current.get("blocking_gates", []))
    )


def write_outputs(checks: list[dict[str, Any]], current: dict[str, Any],
                  self_check_summary: dict[str, Any], behavior_summary: dict[str, Any]) -> None:
    debug = OUTPUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(checks)
    frame.to_csv(OUTPUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    frame.to_csv(debug / "completion_checks.csv", index=False, encoding="utf-8-sig")
    implementation = frame[frame["layer"] == "implementation"]
    readiness = frame[frame["layer"] == "readiness"]
    behavior_counts = behavior_summary_counts(behavior_summary)
    self_check_count = first_int(self_check_summary, "self_check_count")
    self_check_pass_count = first_int(self_check_summary, "self_check_pass_count")
    self_check_fail_count = first_int(self_check_summary, "self_check_fail_count")
    self_check_passed = bool(
        self_check_summary.get("self_check_regression_passed") is True
        and self_check_count == 12
        and self_check_pass_count == self_check_count
        and self_check_fail_count == 0
    )
    verification_layers: dict[str, dict[str, Any]] = {}
    for verification_layer in VERIFICATION_LAYERS:
        layer_frame = frame[
            (frame["verification_layer"] == verification_layer)
            & (frame["evidence_type"] != "behavior_test")
        ]
        behavior_layer = behavior_layer_stats(behavior_summary, verification_layer)
        verification_layers[verification_layer] = {
            "completion_check_count": int(len(layer_frame)),
            "completion_check_pass_count": int((layer_frame["status"] == "pass").sum()),
            "behavior_test_count": behavior_layer["test_count"],
            "behavior_test_pass_count": behavior_layer["pass_count"],
            "behavior_test_fail_count": behavior_layer["fail_count"],
            "behavior_test_error_count": behavior_layer["error_count"],
            "behavior_tests_passed": behavior_layer["passed"],
        }
    validation_ready = self_check_passed and behavior_counts["behavior_tests_passed"]
    readiness_ready = bool(len(readiness)) and bool((readiness["status"] == "pass").all())
    ready = readiness_ready and validation_ready
    summary = {
        "version": "etf-assisted-trading-completion-2.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "implementation_pass_count": int((implementation["status"] == "pass").sum()),
        "implementation_check_count": len(implementation),
        "readiness_pass_count": int((readiness["status"] == "pass").sum()),
        "readiness_check_count": len(readiness),
        "self_check_count": self_check_count,
        "self_check_pass_count": self_check_pass_count,
        "self_check_fail_count": self_check_fail_count,
        "self_check_regression_passed": self_check_passed,
        **behavior_counts,
        "verification_layers": verification_layers,
        "validation_ready": validation_ready,
        "manual_decision_support_ready": ready,
        "current_action": current.get("action"),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "工程主线已形成，但证据与实时输入门禁未通过，不能给出有效买卖建议。" if not ready else "全部人工辅助门禁通过，仍需人工确认。",
    }
    (OUTPUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = frame[frame["status"] != "pass"]
    lines = ["# ETF量化辅助交易系统完成度审计", "", summary["final_verdict"], "",
             f"- 工程实现：{summary['implementation_pass_count']} / {summary['implementation_check_count']}",
             f"- 建议就绪：{summary['readiness_pass_count']} / {summary['readiness_check_count']}",
             f"- 自检回归：{summary['self_check_pass_count']} / {summary['self_check_count']}",
             f"- 独立行为测试：{summary['behavior_test_pass_count']} / {summary['behavior_test_count']}",
             f"- 当前动作：`{summary['current_action']}`", "- 自动执行：`false`", "",
             "## 验证证据分层", "",
             "| 层级 | 完成度检查 | 独立行为测试 | 行为测试状态 |",
             "| --- | ---: | ---: | --- |"]
    for verification_layer in VERIFICATION_LAYERS:
        item = verification_layers[verification_layer]
        behavior_display = (
            f"{item['behavior_test_pass_count']} / {item['behavior_test_count']}"
            if item["behavior_test_count"] else "待验"
        )
        behavior_status = "pass" if item["behavior_tests_passed"] else "fail"
        lines.append(
            f"| `{verification_layer}` | {item['completion_check_pass_count']} / {item['completion_check_count']} "
            f"| {behavior_display} | `{behavior_status}` |"
        )
    lines += [
        "",
        "源码字符串与静态结构检查只归入 `contract`；它们不作为 `unit`、`integration` 或研究证据。",
        "12 项自检回归与独立行为测试分别统计，缺少行为测试摘要或任一分层时按未通过处理。",
        "",
        "## 未通过项",
        "",
    ]
    lines += [f"- **{item.check}**：{item.evidence}；要求：{item.requirement}" for item in failed.itertuples()]
    (OUTPUT / "report.md").write_text("\n".join(lines), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def self_check() -> None:
    check = row("readiness", "x", False, "e", "r")
    assert check["status"] == "fail" and check["layer"] == "readiness"
    assert row("implementation", "Dashboard 当前视图", True, "e", "r")["verification_layer"] == "contract"
    checks = build_checks({}, {}, {}, [], {}, {}, {}, {}, {}, {}, {}, {})
    assert any(item["check"] == "建议审计合同" and item["status"] == "fail" for item in checks)
    assert any(item["check"] == "当前主线自检回归" and item["status"] == "fail" for item in checks)
    behavior_checks = [item for item in checks if item["evidence_type"] == "behavior_test"]
    assert len(behavior_checks) == len(VERIFICATION_LAYERS)
    assert all(item["status"] == "fail" for item in behavior_checks)
    behavior = {
        "behavior_test_count": 5,
        "behavior_test_pass_count": 5,
        "behavior_test_fail_count": 0,
        "behavior_test_error_count": 0,
        "behavior_tests_passed": True,
        "verification_layers": {
            layer: {"test_count": 1, "pass_count": 1, "fail_count": 0, "error_count": 0, "status": "pass"}
            for layer in VERIFICATION_LAYERS
        },
    }
    assert behavior_summary_counts(behavior)["behavior_tests_passed"] is True
    assert account_contract_passed({"account_state_gate_passed": True, "portfolio_risk_gate_passed": True})
    assert account_contract_passed({"blocking_gates": ["account_state", "portfolio_risk"]})
    assert not account_contract_passed({"blocking_gates": ["account_state"]})
    print("self_check=pass")


if __name__ == "__main__":
    main()
