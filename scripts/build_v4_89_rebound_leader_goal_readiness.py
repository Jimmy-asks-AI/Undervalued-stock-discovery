#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "rebound_leader_goal_readiness_v4_89"
DEBUG = OUT / "debug"

V485_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85" / "run_summary.json"
V486_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_forward_v4_86" / "run_summary.json"
V487_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_evidence_scorecard_v4_87" / "run_summary.json"
V488_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_pre_entry_audit_v4_88" / "run_summary.json"
V490_SUMMARY = ROOT / "outputs" / "audit" / "rebound_leader_entry_batch_gate_v4_90" / "run_summary.json"
V491_SUMMARY = ROOT / "outputs" / "audit" / "rebound_leader_promotion_math_v4_91" / "run_summary.json"
V492_SUMMARY = ROOT / "outputs" / "audit" / "rebound_leader_metric_grain_v4_92" / "run_summary.json"
SETTLEMENT_SUMMARY = ROOT / "outputs" / "audit" / "v4_85_parent_neutral_forward_settlement" / "run_summary.json"
FORWARD_LEDGER = ROOT / "logs" / "v4_85_parent_neutral_forward_ledger.csv"

CHECK_FIELDS = ["dimension", "check", "current", "required", "status", "interpretation", "evidence_path"]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.89 goal-readiness audit for rebound-leader selection.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    sources = load_sources()
    checks = build_checks(sources)
    summary = build_summary(checks, sources)
    protocol = build_forward_protocol(summary)
    write_outputs(checks, summary, protocol)
    print(f"output_dir={OUT}")
    print(f"goal_ready={summary['goal_ready']}")
    print(f"can_claim_strong_rebound_industries={summary['can_claim_strong_rebound_industries']}")


def load_sources() -> dict[str, Any]:
    return {
        "v485": read_json(V485_SUMMARY),
        "v486": read_json(V486_SUMMARY),
        "v487": read_json(V487_SUMMARY),
        "v488": read_json(V488_SUMMARY),
        "v490": read_json(V490_SUMMARY),
        "v491": read_json(V491_SUMMARY),
        "v492": read_json(V492_SUMMARY),
        "settlement": read_json(SETTLEMENT_SUMMARY),
        "ledger": read_rows(FORWARD_LEDGER),
    }


def build_checks(src: dict[str, Any]) -> list[dict[str, str]]:
    v485 = src["v485"]
    v486 = src["v486"]
    v487 = src["v487"]
    v488 = src["v488"]
    v490 = src["v490"]
    v491 = src["v491"]
    v492 = src["v492"]
    settlement = src["settlement"]
    ledger = src["ledger"]

    framework_ready = bool(v487) and int_value(v487.get("scorecard_rows")) >= 10
    frozen_ready = int_value(v486.get("ledger_rows")) > 0 and int_value(v486.get("duplicate_key_count")) == 0
    pre_entry_pass = v488.get("pre_entry_status") == "pre_entry_consistent" and int_value(v488.get("fail_count")) == 0
    settled_batches = int_value(settlement.get("settled_tracker_count"))
    required_batches = int_value(settlement.get("required_settled_tracker_count")) or 30
    historical_fail_count = int_value(v487.get("fail_count"))
    pending_count = int_value(v487.get("pending_count"))

    return [
        check(
            "评价体系",
            "强反弹行业评价体系已固化",
            str(framework_ready),
            "true",
            "pass" if framework_ready else "fail",
            "V4.87 已固定历史稳健性、前推样本和前推表现三类门槛。",
            rel(V487_SUMMARY),
        ),
        check(
            "候选规则",
            "最接近规则已冻结且无重复账本",
            f"ledger_rows={v486.get('ledger_rows', '')}; duplicate_key_count={v486.get('duplicate_key_count', '')}",
            "ledger_rows > 0 and duplicate_key_count == 0",
            "pass" if frozen_ready else "fail",
            "V4.85 父行业 cap1 规则已进入前推账本，但仍是 research_only。",
            f"{rel(V486_SUMMARY)}; {rel(FORWARD_LEDGER)}",
        ),
        check(
            "历史证据",
            "历史点估计能选出更强行业",
            f"mean_relative={fmt_float(v485.get('best_mean_relative_return'))}; top20_hit={fmt_float(v485.get('best_top_quintile_hit_rate'))}",
            "mean_relative > 0 and top20_hit >= 0.30",
            "pass" if float_value(v485.get("best_mean_relative_return")) > 0 and float_value(v485.get("best_top_quintile_hit_rate")) >= 0.30 else "fail",
            "点估计支持“可能有强反弹行业排序信息”，但不能单独证明稳健有效。",
            rel(V485_SUMMARY),
        ),
        check(
            "历史稳健性",
            "bootstrap 下界通过",
            f"top20_p05={fmt_float(v485.get('best_bootstrap_top_quintile_hit_p05'))}; evidence_fail_count={historical_fail_count}",
            "top20_p05 >= 0.30 and all historical robust gates pass",
            "pass" if float_value(v485.get("best_bootstrap_top_quintile_hit_p05")) >= 0.30 and historical_fail_count == 0 else "fail",
            "当前主要失败项仍是强反弹命中率和年度稳定性的稳健下界不足。",
            f"{rel(V485_SUMMARY)}; {rel(V487_SUMMARY)}",
        ),
        check(
            "入场前审计",
            "冻结候选入场前一致",
            f"pre_entry_status={v488.get('pre_entry_status', '')}; fail_count={v488.get('fail_count', '')}",
            "pre_entry_consistent and fail_count == 0",
            "pass" if pre_entry_pass else "fail",
            "当前批次在入场日前一致，但仍需入场日刷新。",
            rel(V488_SUMMARY),
        ),
        check(
            "入场门控",
            "当前批次进入有效前推样本",
            f"entry_gate_status={v490.get('entry_gate_status', '')}; apply_allowed={v490.get('apply_allowed', '')}",
            "entered_research_observation before settlement; not_due before entry date",
            entry_gate_check_status(str(v490.get("entry_gate_status", ""))),
            "只有入场门控通过的批次才允许进入后续强反弹行业前推评价。",
            rel(V490_SUMMARY),
        ),
        check(
            "晋级口径",
            "前推晋级数学门槛已固定",
            f"min_batches={v491.get('min_forward_batches', '')}; positive_at_30={v491.get('required_positive_batches_at_30', '')}; top20_rows_at_30={v491.get('required_top_quintile_hit_rows_at_30', '')}",
            "30 batches; positive >= 17; top20 hit rows >= 90",
            "pass" if int_value(v491.get("min_forward_batches")) == 30 and int_value(v491.get("required_positive_batches_at_30")) == 17 and int_value(v491.get("required_top_quintile_hit_rows_at_30")) == 90 else "pending",
            "未来样本出来后不能临时修改正超额和 Top20% 命中门槛。",
            rel(V491_SUMMARY),
        ),
        check(
            "指标粒度",
            "Top20% 命中口径粒度一致",
            f"metric_grain_status={v492.get('metric_grain_status', '')}; fail_count={v492.get('fail_count', '')}",
            "metric_grain_status == pass and fail_count == 0",
            "pass" if v492.get("metric_grain_status") == "pass" and int_value(v492.get("fail_count")) == 0 else "pending",
            "V4.87、V4.91、结算账本必须都使用候选行级 Top20% 命中口径。",
            rel(V492_SUMMARY),
        ),
        check(
            "前推样本",
            "独立前推批次足够",
            f"settled={settled_batches}; required={required_batches}",
            "settled >= 30",
            "pass" if settled_batches >= required_batches else "pending",
            "未到足够真实前推样本前，不能把历史候选升级为已验证强行业规则。",
            rel(SETTLEMENT_SUMMARY),
        ),
        check(
            "前推表现",
            "真实前推表现达标",
            f"pending_scorecard_items={pending_count}",
            "mean_relative > 0; positive_batch_rate >= 55%; top20_hit >= 30%",
            "pending" if settled_batches < required_batches else ("pass" if pending_count == 0 and historical_fail_count == 0 else "fail"),
            "前推收益、正超额批次比例和 Top20% 命中率必须按冻结口径结算。",
            rel(V487_SUMMARY),
        ),
        check(
            "生产边界",
            "不自动交易",
            f"auto_execution_allowed={v486.get('auto_execution_allowed', '')}; ledger_rows={len(ledger)}",
            "false",
            "pass" if not bool(v486.get("auto_execution_allowed", True)) else "fail",
            "当前只能作为研究观察和人工复核输入，不能自动生成交易指令。",
            rel(V486_SUMMARY),
        ),
    ]


def build_summary(checks: list[dict[str, str]], src: dict[str, Any]) -> dict[str, Any]:
    pass_count = sum(item["status"] == "pass" for item in checks)
    fail_count = sum(item["status"] == "fail" for item in checks)
    pending_count = sum(item["status"] == "pending" for item in checks)
    v485, v486, v488, settlement = src["v485"], src["v486"], src["v488"], src["settlement"]
    can_claim = fail_count == 0 and pending_count == 0
    planned_entry = str(v488.get("planned_entry_date") or "")
    planned_exit = str(v488.get("planned_exit_date") or "")
    return {
        "version": "4.89.0",
        "policy_id": "industry_rebound_leader_goal_readiness_v4_89",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_rule": "deep_highvol_liq_repair + global_rank_parent_cap1 + oversold_liquidity_score Top10",
        "has_strong_rebound_evaluation_framework": any(item["check"] == "强反弹行业评价体系已固化" and item["status"] == "pass" for item in checks),
        "can_claim_strong_rebound_industries": can_claim,
        "goal_ready": can_claim,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "pending_count": pending_count,
        "current_candidate_count": int_value(v486.get("ledger_rows")),
        "historical_mean_relative_return": float_value(v485.get("best_mean_relative_return")),
        "historical_top_quintile_hit_rate": float_value(v485.get("best_top_quintile_hit_rate")),
        "historical_bootstrap_top_quintile_hit_p05": float_value(v485.get("best_bootstrap_top_quintile_hit_p05")),
        "settled_forward_tracker_count": int_value(settlement.get("settled_tracker_count")),
        "required_settled_forward_tracker_count": int_value(settlement.get("required_settled_tracker_count")) or 30,
        "planned_entry_date": planned_entry,
        "planned_exit_date": planned_exit,
        "next_required_action": next_required_action(planned_entry, planned_exit, date.today()),
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "已有强反弹行业评价体系，也已有一条可前推观察的候选规则；但稳健历史证据和真实前推样本仍不足，不能声称已经找到反弹窗口下的强反弹行业。",
    }


def build_forward_protocol(summary: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "step": "1",
            "stage": "entry_day_refresh",
            "date": str(summary.get("planned_entry_date", "")),
            "required_action": "入场日重新运行 live refresh 和 V4.88 盘前一致性审计。",
            "pass_condition": "候选集合稳定、窗口仍有效、审计无失败。",
        },
        {
            "step": "2",
            "stage": "exit_day_settlement",
            "date": str(summary.get("planned_exit_date", "")),
            "required_action": "退出日后结算真实相对收益和 Top20% 命中。",
            "pass_condition": "只使用退出日已发生价格，不回填未来信息。",
        },
        {
            "step": "3",
            "stage": "promotion_gate",
            "date": "累计满 30 个独立前推批次后",
            "required_action": "重新运行 V4.87/V4.89 总评价。",
            "pass_condition": "前推平均超额 > 0、正超额批次比例 >= 55%、Top20% 命中率 >= 30%，且无未解释硬失败。",
        },
    ]


def write_outputs(checks: list[dict[str, str]], summary: dict[str, Any], protocol: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "top_candidates.csv", checks, CHECK_FIELDS)
    write_csv(DEBUG / "readiness_checks.csv", checks, CHECK_FIELDS)
    write_csv(DEBUG / "forward_protocol.csv", protocol, ["step", "stage", "date", "required_action", "pass_condition"])
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, checks, protocol), encoding="utf-8")


def render_report(summary: dict[str, Any], checks: list[dict[str, str]], protocol: list[dict[str, str]]) -> str:
    lines = [
        "# V4.89 强反弹行业目标就绪度审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 当前回答",
        "",
        f"- 是否已有评价体系：`{str(summary['has_strong_rebound_evaluation_framework']).lower()}`",
        f"- 是否已经证明找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        f"- 当前候选数量：{summary['current_candidate_count']}",
        f"- 历史平均相对收益：{fmt_pct(summary['historical_mean_relative_return'])}",
        f"- 历史 Top20% 命中率：{fmt_pct(summary['historical_top_quintile_hit_rate'])}",
        f"- bootstrap Top20% 命中率 5% 下界：{fmt_pct(summary['historical_bootstrap_top_quintile_hit_p05'])}",
        f"- 已结算前推批次：{summary['settled_forward_tracker_count']}/{summary['required_settled_forward_tracker_count']}",
        f"- 计划入场/退出：{summary['planned_entry_date']} / {summary['planned_exit_date']}",
        f"- 下一步动作：{summary['next_required_action']}",
        f"- 生产就绪：`{str(summary['production_ready']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 就绪度检查",
        "",
        markdown_table(checks, ["dimension", "check", "current", "required", "status", "interpretation"]),
        "",
        "## 前推协议",
        "",
        markdown_table(protocol, ["step", "stage", "date", "required_action", "pass_condition"]),
        "",
        "## 边界",
        "",
        "V4.89 只汇总现有证据，不新增参数、不重新选择规则。任何未来批次都必须按冻结口径结算，不能因为结果好坏临时调整门槛。",
    ]
    return "\n".join(lines)


def check(dimension: str, name: str, current: str, required: str, status: str, interpretation: str, evidence: str) -> dict[str, str]:
    return {
        "dimension": dimension,
        "check": name,
        "current": current,
        "required": required,
        "status": status,
        "interpretation": interpretation,
        "evidence_path": evidence,
    }


def entry_gate_check_status(status: str) -> str:
    if status == "entered_research_observation":
        return "pass"
    if status == "not_due":
        return "pending"
    return "fail" if status else "pending"


def next_required_action(entry: str, exit_: str, today: date) -> str:
    try:
        entry_date = date.fromisoformat(entry)
        exit_date = date.fromisoformat(exit_)
    except ValueError:
        return "缺少计划入场或退出日期，先刷新 V4.86/V4.88。"
    if today <= entry_date:
        return f"{entry} 入场日前重新运行 live refresh；若候选漂移或审计失败，则跳过本批次。"
    if today <= exit_date:
        return f"持有观察中；{exit_} 退出日后再结算真实收益。"
    return f"运行 V4.85 前推结算脚本并刷新 V4.87/V4.89。"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def int_value(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def float_value(value: Any) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def fmt_float(value: Any) -> str:
    return f"{float_value(value):.4f}"


def fmt_pct(value: Any) -> str:
    return f"{float_value(value) * 100:.2f}%"


def markdown_table(rows: list[dict[str, str]], cols: list[str]) -> str:
    if not rows:
        return "无数据"
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(escape_md(str(row.get(col, ""))) for col in cols) + " |")
    return "\n".join(lines)


def escape_md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        checks = build_checks({
            "v485": {
                "best_mean_relative_return": 0.02,
                "best_top_quintile_hit_rate": 0.32,
                "best_bootstrap_top_quintile_hit_p05": 0.28,
            },
            "v486": {"ledger_rows": 10, "duplicate_key_count": 0, "auto_execution_allowed": False},
            "v487": {"scorecard_rows": 11, "fail_count": 2, "pending_count": 5},
            "v488": {"pre_entry_status": "pre_entry_consistent", "fail_count": 0, "planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21"},
            "v490": {"entry_gate_status": "not_due", "apply_allowed": False},
            "v491": {"min_forward_batches": 30, "required_positive_batches_at_30": 17, "required_top_quintile_hit_rows_at_30": 90},
            "v492": {"metric_grain_status": "pass", "fail_count": 0},
            "settlement": {"settled_tracker_count": 0, "required_settled_tracker_count": 30},
            "ledger": [{"tracker_id": "x"}],
        })
        summary = build_summary(checks, {
            "v485": {
                "best_mean_relative_return": 0.02,
                "best_top_quintile_hit_rate": 0.32,
                "best_bootstrap_top_quintile_hit_p05": 0.28,
            },
            "v486": {"ledger_rows": 10},
            "v488": {"planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21"},
            "settlement": {"settled_tracker_count": 0, "required_settled_tracker_count": 30},
        })
        assert any(row["status"] == "fail" for row in checks)
        assert any(row["status"] == "pending" for row in checks)
        assert entry_gate_check_status("entered_research_observation") == "pass"
        assert summary["has_strong_rebound_evaluation_framework"] is True
        assert summary["can_claim_strong_rebound_industries"] is False
        assert markdown_table(checks, ["dimension", "status"])
        assert out.exists()
    print("self_check=pass")


if __name__ == "__main__":
    main()
