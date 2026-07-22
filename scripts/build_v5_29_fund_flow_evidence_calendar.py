#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from fund_flow_forward_evidence import materialize_observations, read_events, verify_ledger_checkpoint


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv"
EVENT_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
ENTRY_GATE = ROOT / "outputs" / "audit" / "fund_flow_forward_entry_gate_v5_26" / "run_summary.json"
SETTLEMENT = ROOT / "outputs" / "audit" / "fund_flow_forward_settlement_v5_27" / "run_summary.json"
PROMOTION = ROOT / "outputs" / "audit" / "fund_flow_promotion_evaluator_v5_28" / "run_summary.json"
LEDGER_INTEGRITY = ROOT / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30" / "run_summary.json"
LEDGER_INTEGRITY_CHECKS = ROOT / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30" / "debug" / "ledger_integrity_checks.csv"
ENTRY_FREEZE = ROOT / "outputs" / "audit" / "fund_flow_entry_price_freeze_v5_33" / "run_summary.json"
BENCHMARK_ENTRY_FREEZE = ROOT / "outputs" / "audit" / "fund_flow_benchmark_entry_freeze_v5_34" / "run_summary.json"
FREEZE_MANIFEST = ROOT / "outputs" / "audit" / "fund_flow_evidence_freeze_manifest_v5_31" / "run_summary.json"
OUT = ROOT / "outputs" / "audit" / "fund_flow_evidence_calendar_v5_29"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.29 evidence calendar for fund-flow forward validation.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    as_of = datetime.fromisoformat(args.as_of_date).date()
    if as_of > date.today():
        parser.error(f"--as-of-date {args.as_of_date} is in the future; evidence calendar must use current or past dates.")

    global_ledger = read_ledger()
    active_cohort = validated_active_cohort()
    ledger = filter_rows_to_active_cohort(global_ledger, active_cohort)
    raw_sources = {name: read_json(path) for name, path in {
        "entry_gate": ENTRY_GATE, "settlement": SETTLEMENT, "promotion": PROMOTION, "ledger_integrity": LEDGER_INTEGRITY,
        "entry_freeze": ENTRY_FREEZE, "benchmark_entry_freeze": BENCHMARK_ENTRY_FREEZE,
        "freeze_manifest": FREEZE_MANIFEST,
    }.items()}
    sources, source_scope = scope_sources_to_active_cohort(raw_sources, active_cohort)
    sources["ledger_integrity_checks"] = {
        "rows": read_csv_rows(LEDGER_INTEGRITY_CHECKS) if source_scope.get("ledger_integrity") else []
    }
    sources["active_cohort"] = normalized_active_cohort(active_cohort)
    sources["source_scope"] = source_scope
    sources["global_history_diagnostics"] = global_history_diagnostics(global_ledger, raw_sources)
    calendar = build_calendar(ledger, as_of, sources)
    gaps = build_gaps(sources)
    summary = build_summary(
        as_of,
        ledger,
        calendar,
        gaps,
        sources,
        active_cohort=active_cohort,
        global_ledger=global_ledger,
    )
    write_outputs(summary, calendar, gaps, sources)
    print(f"output_dir={OUT}")
    print(f"next_action_date={summary['next_action_date']}")
    print(f"goal_ready={summary['goal_ready']}")


def validated_cohort_pair(active_cohort: dict[str, Any] | None) -> tuple[str, str] | None:
    active = active_cohort or {}
    cohort_id = str(active.get("cohort_id", ""))
    manifest_hash = str(active.get("manifest_hash", ""))
    if active.get("freeze_passed") is not True or not cohort_id or not manifest_hash:
        return None
    return cohort_id, manifest_hash


def normalized_active_cohort(active_cohort: dict[str, Any] | None) -> dict[str, Any]:
    pair = validated_cohort_pair(active_cohort)
    return {
        "cohort_id": pair[0] if pair else "",
        "manifest_hash": pair[1] if pair else "",
        "freeze_passed": pair is not None,
    }


def filter_rows_to_active_cohort(
    rows: list[dict[str, str]],
    active_cohort: dict[str, Any] | None,
) -> list[dict[str, str]]:
    pair = validated_cohort_pair(active_cohort)
    if pair is None:
        return []
    cohort_id, manifest_hash = pair
    return [
        row
        for row in rows
        if str(row.get("cohort_id", "")) == cohort_id
        and str(row.get("cohort_manifest_hash", "")) == manifest_hash
    ]


def summary_cohort_pair(summary: dict[str, Any]) -> tuple[str, str] | None:
    for cohort_field, hash_field in [
        ("active_cohort_id", "active_cohort_manifest_hash"),
        ("cohort_id", "cohort_manifest_hash"),
        ("cohort_id", "manifest_hash"),
    ]:
        cohort_id = str(summary.get(cohort_field, ""))
        manifest_hash = str(summary.get(hash_field, ""))
        if cohort_id and manifest_hash:
            return cohort_id, manifest_hash
    return None


def scope_sources_to_active_cohort(
    sources: dict[str, dict[str, Any]],
    active_cohort: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, bool]]:
    active_pair = validated_cohort_pair(active_cohort)
    scoped: dict[str, dict[str, Any]] = {}
    matches: dict[str, bool] = {}
    for name, payload in sources.items():
        match = active_pair is not None and summary_cohort_pair(payload) == active_pair
        matches[name] = match
        scoped[name] = dict(payload) if match else {}
    return scoped, matches


def global_history_diagnostics(
    rows: list[dict[str, str]],
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    cohort_pairs = {
        (str(row.get("cohort_id", "")), str(row.get("cohort_manifest_hash", "")))
        for row in rows
        if str(row.get("cohort_id", "")) or str(row.get("cohort_manifest_hash", ""))
    }
    return {
        "ledger_rows": len(rows),
        "settled_rows": sum(str(row.get("settlement_status", "")) == "settled" for row in rows),
        "cohort_count": len(cohort_pairs),
        "source_pairs": {
            name: list(pair) if (pair := summary_cohort_pair(payload)) else []
            for name, payload in sources.items()
        },
        "research_boundary": "全局历史只作独立诊断，不参与当前 cohort 日历、缺口或晋级门禁。",
    }


def build_calendar(rows: list[dict[str, str]], as_of: date, sources: dict[str, dict[str, Any]] | None = None) -> pd.DataFrame:
    items = []
    for event_type, field, command in [
        ("entry_refresh", "planned_entry_date", "python .\\scripts\\run_v4_71_live_refresh.py --trade-date {date}"),
        ("forward_settlement", "planned_exit_date", "python .\\scripts\\settle_v5_27_fund_flow_forward_samples.py --as-of-date {date}  # 仅限北京时间15:00收盘后运行"),
        ("promotion_evaluation", "planned_exit_date", "python .\\scripts\\build_v5_28_fund_flow_promotion_evaluator.py"),
    ]:
        counts: dict[str, int] = {}
        for row in rows:
            value = row.get(field, "")
            if value:
                counts[value] = counts.get(value, 0) + 1
        for event_date, count in sorted(counts.items()):
            status = event_status(event_type, event_date, count, as_of, sources or {})
            items.append({
                "event_date": event_date,
                "event_type": event_type,
                "row_count": count,
                "status": status,
                "command": command.format(date=event_date),
                "action": action_text(event_type, status),
            })
    return pd.DataFrame(items).sort_values(["status", "event_date", "event_type"]) if items else pd.DataFrame()


def event_status(event_type: str, event_date: str, count: int, as_of: date, sources: dict[str, dict[str, Any]]) -> str:
    event_day = datetime.fromisoformat(event_date).date()
    if event_type == "entry_refresh":
        gate = sources.get("entry_gate", {})
        gate_day = parse_date(gate.get("as_of_date", ""))
        reviewed = int(gate.get("entry_review_required_count", 0) or 0) + int(gate.get("entry_allowed_count", 0) or 0)
        missing_snapshots = int(gate.get("entry_missing_snapshot_count", 1))
        if gate_day and gate_day >= event_day and missing_snapshots == 0 and reviewed >= count:
            return "completed_review_only"
    if event_type == "forward_settlement":
        settlement = sources.get("settlement", {})
        if int(settlement.get("settled_rows", 0) or 0) >= count and int(settlement.get("pending_rows", 0) or 0) == 0:
            return "completed_settlement"
    if event_type == "promotion_evaluation" and sources.get("promotion", {}).get("promotion_ready"):
        return "completed_promotion"
    return "due_now" if event_day <= as_of else "pending"


def parse_date(value: object) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def action_text(event_type: str, status: str) -> str:
    if status == "completed_review_only":
        return "入场日刷新和门禁复核已完成；研究系统仍不自动交易。"
    if status == "completed_settlement":
        return "退日北京时间 15:00 收盘后的真实收益结算已完成。"
    if status == "completed_promotion":
        return "晋级评价已完成；仍不代表自动交易许可。"
    prefix = "今天应执行" if status == "due_now" else "等待执行"
    return {
        "entry_refresh": f"{prefix}入场日前刷新和门禁复核，不自动交易。",
        "forward_settlement": f"{prefix}计划退出日北京时间 15:00 收盘后的真实收益结算。",
        "promotion_evaluation": f"{prefix}晋级评价，只有已结算样本可参与。",
    }[event_type]


def build_gaps(sources: dict[str, dict[str, Any]]) -> pd.DataFrame:
    promo = sources["promotion"]
    settle = sources["settlement"]
    gate = sources["entry_gate"]
    integrity = sources.get("ledger_integrity", {})
    entry_freeze = sources.get("entry_freeze", {})
    benchmark_freeze = sources.get("benchmark_entry_freeze", {})
    freeze_manifest = sources.get("freeze_manifest", {})
    integrity_checks = {row.get("check", ""): row.get("status", "") for row in sources.get("ledger_integrity_checks", {}).get("rows", [])}
    reviewed = int(gate.get("entry_review_required_count", 0) or 0) + int(gate.get("entry_allowed_count", 0) or 0)
    rows = [
        gap("entry_review_or_allowed_count", reviewed, 1, ">=", "入场日前门禁必须形成研究复核样本；研究系统不要求自动入场。"),
        gap("settled_batch_count", promo.get("settled_batch_count", 0), 30, ">=", "至少需要 30 个已结算批次。"),
        gap("settled_industry_count", promo.get("settled_industry_count", 0), 30, ">=", "至少需要 30 个已结算行业观察。"),
        gap("mean_relative_return", promo.get("mean_relative_return"), 0, ">", "已结算样本平均相对收益需要为正。"),
        gap("median_relative_return", promo.get("median_relative_return"), 0, ">", "已结算样本中位相对收益需要为正。"),
        gap("positive_batch_rate", promo.get("positive_batch_rate"), 0.55, ">=", "正超额批次比例需要不低于 55%。"),
        gap("top_quintile_hit_rate", promo.get("top_quintile_hit_rate"), 0.30, ">=", "未来收益 Top20% 命中率需要不低于 30%。"),
        gap("pending_rows", settle.get("pending_rows", 0), 0, "==", "所有当前前推观察都要到期结算。"),
        gap("ledger_integrity_passed", 1 if integrity.get("integrity_passed") else 0, 1, "==", "账本完整性和冻结覆盖审计必须通过。"),
        gap("late_backfill_count", integrity.get("late_backfill_count", 0), 0, "==", "任何迟到观察或冻结都会阻断晋级。"),
        gap("cohort_freeze_passed", 1 if freeze_manifest.get("freeze_passed") else 0, 1, "==", "当前 cohort 的不可变基线必须独立复核通过。"),
        gap("cohort_hash_match", 1 if integrity.get("eligible_cohort_hashes") == [freeze_manifest.get("manifest_hash")] and freeze_manifest.get("manifest_hash") else 0, 1, "==", "V5.30 与 V5.31 必须指向同一 cohort manifest hash。"),
        gap("candidate_entry_freeze_count", entry_freeze.get("frozen_entry_count"), reviewed, ">=", "V5.33 必须冻结已复核候选行业的入场点。"),
        gap("benchmark_entry_freeze_rows", benchmark_freeze.get("benchmark_frozen_rows"), 100, ">=", "V5.34 必须冻结全行业基准入场点。"),
        gap("candidate_entry_freeze_coverage", 1 if integrity_checks.get("candidate_entry_freeze_coverage") == "pass" else 0, 1, "==", "已到入场日的待结算样本必须冻结候选行业入场价。"),
        gap("benchmark_entry_freeze_coverage", 1 if integrity_checks.get("benchmark_entry_freeze_coverage") == "pass" else 0, 1, "==", "已到入场日的待结算样本必须冻结全行业基准入场点。"),
    ]
    return pd.DataFrame(rows)


def gap(metric: str, current: Any, required: float, op: str, reason: str) -> dict[str, Any]:
    return {
        "metric": metric,
        "current": "" if current is None else current,
        "required": required,
        "operator": op,
        "status": status(current, required, op),
        "reason": reason,
    }


def status(current: Any, required: float, op: str) -> str:
    if current is None:
        return "pending"
    value = float(current)
    ok = value == required if op == "==" else (value >= required if op == ">=" else value > required)
    return "pass" if ok else "fail"


def build_summary(
    as_of: date,
    ledger: list[dict[str, str]],
    calendar: pd.DataFrame,
    gaps: pd.DataFrame,
    sources: dict[str, dict[str, Any]],
    *,
    active_cohort: dict[str, Any] | None = None,
    global_ledger: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    active = validated_active_cohort() if active_cohort is None else active_cohort
    active_pair = validated_cohort_pair(active)
    scoped_ledger = filter_rows_to_active_cohort(ledger, active)
    history = list(global_ledger) if global_ledger is not None else list(ledger)
    next_row = next_action(calendar)
    fail_count = int(gaps["status"].eq("fail").sum()) if len(gaps) else 0
    pending_count = int(gaps["status"].eq("pending").sum()) if len(gaps) else 0
    promotion_ready = bool(sources["promotion"].get("promotion_ready", False))
    goal_ready = (
        active_pair is not None
        and promotion_ready
        and bool(sources.get("ledger_integrity", {}).get("integrity_passed"))
        and bool(sources.get("freeze_manifest", {}).get("freeze_passed"))
        and fail_count == 0
        and pending_count == 0
    )
    return {
        "version": "5.29.1",
        "policy_id": "fund_flow_evidence_calendar_v5_29",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "active_cohort_id": active_pair[0] if active_pair else "",
        "active_cohort_manifest_hash": active_pair[1] if active_pair else "",
        "active_cohort_validated": active_pair is not None,
        "ledger_rows": len(scoped_ledger),
        "global_history_ledger_rows": len(history),
        "global_history_settled_rows": sum(str(row.get("settlement_status", "")) == "settled" for row in history),
        "calendar_rows": int(len(calendar)),
        "fail_count": fail_count,
        "pending_count": pending_count,
        "next_action_date": next_row.get("event_date", ""),
        "next_action": next_row.get("action", ""),
        "next_command": next_row.get("command", ""),
        "promotion_ready": promotion_ready,
        "goal_ready": goal_ready,
        "can_claim_strong_rebound_industries": goal_ready,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_evidence_ready" if goal_ready else "research_only_evidence_collection_pending",
        "final_verdict": "V5.29 显示资金流前推证据缺口已清零，且 V5.28 已通过晋级评价。" if goal_ready else "V5.29 只列出资金流前推证据日历和晋级缺口；缺口未清零前不能声称找到强反弹行业。",
    }


def next_action(calendar: pd.DataFrame) -> dict[str, Any]:
    if calendar.empty:
        return {}
    active = calendar[~calendar["status"].astype(str).str.startswith("completed")]
    if active.empty:
        return {}
    due = active[active["status"].eq("due_now")]
    row = (due if len(due) else active).sort_values(["event_date", "event_type"]).iloc[0]
    return row.to_dict()


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_ledger() -> list[dict[str, str]]:
    if EVENT_LEDGER.exists():
        verify_ledger_checkpoint(EVENT_LEDGER)
        return materialize_observations(read_events(EVENT_LEDGER))
    if LEDGER.exists():
        raise RuntimeError("authoritative V5.25 JSONL ledger is missing; compatibility CSV cannot be used as evidence")
    return []


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_outputs(summary: dict[str, Any], calendar: pd.DataFrame, gaps: pd.DataFrame, sources: dict[str, dict[str, Any]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    calendar.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, calendar, gaps), encoding="utf-8")
    calendar.to_csv(DEBUG / "evidence_calendar.csv", index=False, encoding="utf-8-sig")
    gaps.to_csv(DEBUG / "evidence_gaps.csv", index=False, encoding="utf-8-sig")
    write_json(DEBUG / "source_snapshot.json", sources)


def render_report(summary: dict[str, Any], calendar: pd.DataFrame, gaps: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.29 资金流前推证据日历",
        "",
        summary["final_verdict"],
        "",
        f"- as-of 日期：{summary['as_of_date']}",
        f"- 前推账本行数：{summary['ledger_rows']}",
        f"- 证据日历行数：{summary['calendar_rows']}",
        f"- 失败缺口：{summary['fail_count']}",
        f"- 待观察缺口：{summary['pending_count']}",
        f"- 下一动作日期：{summary['next_action_date']}",
        f"- 下一动作：{summary['next_action']}",
        f"- 下一命令：`{summary['next_command']}`",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 证据日历",
        "",
        calendar.to_markdown(index=False) if len(calendar) else "无前推日程",
        "",
        "## 晋级缺口",
        "",
        gaps.to_markdown(index=False) if len(gaps) else "无缺口",
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    active_fixture = {"freeze_passed": True, "cohort_id": "c1", "manifest_hash": "h1"}
    rows = [{
        "planned_entry_date": "2026-01-02", "planned_exit_date": "2026-01-10",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }]
    calendar = build_calendar(rows, date(2026, 1, 1))
    assert calendar.iloc[0]["event_date"] == "2026-01-02"
    reviewed_calendar = build_calendar(rows, date(2026, 1, 2), {"entry_gate": {"as_of_date": "2026-01-02", "entry_missing_snapshot_count": 0, "entry_review_required_count": 1}})
    assert reviewed_calendar[reviewed_calendar["event_type"].eq("entry_refresh")]["status"].iloc[0] == "completed_review_only"
    assert next_action(reviewed_calendar)["event_date"] == "2026-01-10"
    gaps = build_gaps({
        "entry_gate": {"entry_allowed_count": 0, "entry_review_required_count": 1},
        "settlement": {"pending_rows": 1},
        "promotion": {"settled_batch_count": 0, "settled_industry_count": 0},
        "ledger_integrity": {"integrity_passed": True},
        "freeze_manifest": {"freeze_passed": True, "manifest_hash": "h1"},
        "entry_freeze": {"frozen_entry_count": 1},
        "benchmark_entry_freeze": {"benchmark_frozen_rows": 131},
        "ledger_integrity_checks": {"rows": [
            {"check": "candidate_entry_freeze_coverage", "status": "pass"},
            {"check": "benchmark_entry_freeze_coverage", "status": "pass"},
        ]},
    })
    assert gaps.set_index("metric").loc["settled_batch_count", "status"] == "fail"
    assert gaps.set_index("metric").loc["entry_review_or_allowed_count", "status"] == "pass"
    assert gaps.set_index("metric").loc["ledger_integrity_passed", "status"] == "pass"
    assert gaps.set_index("metric").loc["candidate_entry_freeze_count", "status"] == "pass"
    assert gaps.set_index("metric").loc["benchmark_entry_freeze_rows", "status"] == "pass"
    assert gaps.set_index("metric").loc["candidate_entry_freeze_coverage", "status"] == "pass"
    assert gaps.set_index("metric").loc["benchmark_entry_freeze_coverage", "status"] == "pass"
    assert build_summary(
        date(2026, 1, 1), rows, calendar, gaps, {"promotion": {}}, active_cohort=active_fixture,
    )["goal_ready"] is False
    completed_calendar = build_calendar(rows, date(2026, 1, 10), {
        "entry_gate": {"as_of_date": "2026-01-02", "entry_missing_snapshot_count": 0, "entry_review_required_count": 1},
        "settlement": {"settled_rows": 1, "pending_rows": 0},
        "promotion": {"promotion_ready": True},
    })
    completed_status = completed_calendar.set_index("event_type")["status"].to_dict()
    assert completed_status["entry_refresh"] == "completed_review_only"
    assert completed_status["forward_settlement"] == "completed_settlement"
    assert completed_status["promotion_evaluation"] == "completed_promotion"
    assert next_action(completed_calendar) == {}
    passing_gaps = pd.DataFrame([gap("x", 1, 1, "==", "ok")])
    passing_summary = build_summary(date(2026, 1, 10), rows, reviewed_calendar.iloc[0:0], passing_gaps, {
        "promotion": {"promotion_ready": True},
        "ledger_integrity": {"integrity_passed": True},
        "freeze_manifest": {"freeze_passed": True},
    }, active_cohort=active_fixture)
    assert passing_summary["goal_ready"] is True
    assert passing_summary["can_claim_strong_rebound_industries"] is True
    blocked_summary = build_summary(date(2026, 1, 10), rows, reviewed_calendar.iloc[0:0], passing_gaps, {
        "promotion": {"promotion_ready": False},
        "ledger_integrity": {"integrity_passed": True},
        "freeze_manifest": {"freeze_passed": True},
    }, active_cohort=active_fixture)
    assert blocked_summary["goal_ready"] is False
    print("self_check=pass")


if __name__ == "__main__":
    main()











