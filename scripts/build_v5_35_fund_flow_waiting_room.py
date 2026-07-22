#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from fund_flow_forward_evidence import freeze_recorded_on_time, is_true, materialize_observations, read_events, verify_ledger_checkpoint


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv"
EVENT_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
SETTLEMENT = ROOT / "outputs" / "audit" / "fund_flow_forward_settlement_v5_27" / "run_summary.json"
CALENDAR = ROOT / "outputs" / "audit" / "fund_flow_evidence_calendar_v5_29" / "run_summary.json"
HOLDING = ROOT / "outputs" / "audit" / "fund_flow_holding_observation_v5_32" / "debug" / "holding_observation_rows.csv"
ENTRY_FREEZE = ROOT / "outputs" / "audit" / "fund_flow_entry_price_freeze_v5_33" / "top_candidates.csv"
BENCHMARK_FREEZE = ROOT / "outputs" / "audit" / "fund_flow_benchmark_entry_freeze_v5_34" / "top_candidates.csv"
ENTRY_FREEZE_LEDGER = ROOT / "logs" / "v5_33_fund_flow_entry_price_freeze.jsonl"
BENCHMARK_FREEZE_LEDGER = ROOT / "logs" / "v5_34_fund_flow_benchmark_entry_freeze.jsonl"
MAPPING = ROOT / "outputs" / "audit" / "fund_flow_mapping_remediation_v5_24" / "run_summary.json"
ENTRY_GATE = ROOT / "outputs" / "audit" / "fund_flow_forward_entry_gate_v5_26" / "run_summary.json"
OBSERVER = ROOT / "outputs" / "audit" / "fund_flow_forward_observer_v5_25" / "run_summary.json"
INTEGRITY = ROOT / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30" / "run_summary.json"
FREEZE_MANIFEST = ROOT / "outputs" / "audit" / "fund_flow_evidence_freeze_manifest_v5_31" / "run_summary.json"
PROMOTION = ROOT / "outputs" / "audit" / "fund_flow_promotion_evaluator_v5_28" / "run_summary.json"
OUT = ROOT / "outputs" / "audit" / "fund_flow_waiting_room_v5_35"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.35 waiting-room monitor for fund-flow forward observations.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    as_of = date.fromisoformat(args.as_of_date)
    if as_of > date.today():
        parser.error(f"--as-of-date {args.as_of_date} is in the future; waiting-room monitor must use current or past dates.")

    global_ledger = read_ledger()
    active_cohort = validated_active_cohort()
    ledger = filter_frame_to_active_cohort(global_ledger, active_cohort)
    holding = filter_frame_to_active_cohort(read_csv(HOLDING), active_cohort)
    if ENTRY_FREEZE_LEDGER.exists():
        verify_ledger_checkpoint(ENTRY_FREEZE_LEDGER)
    if BENCHMARK_FREEZE_LEDGER.exists():
        verify_ledger_checkpoint(BENCHMARK_FREEZE_LEDGER)
    global_entry_freeze = pd.DataFrame(read_events(ENTRY_FREEZE_LEDGER)) if ENTRY_FREEZE_LEDGER.exists() else pd.DataFrame()
    global_benchmark_freeze = pd.DataFrame(read_events(BENCHMARK_FREEZE_LEDGER)) if BENCHMARK_FREEZE_LEDGER.exists() else pd.DataFrame()
    entry_freeze = filter_frame_to_active_cohort(global_entry_freeze, active_cohort)
    benchmark_freeze = filter_frame_to_active_cohort(global_benchmark_freeze, active_cohort)
    raw_sources = {
        "settlement": read_json(SETTLEMENT),
        "calendar": read_json(CALENDAR),
        "mapping": read_json(MAPPING),
        "entry_gate": read_json(ENTRY_GATE),
        "observer": read_json(OBSERVER),
        "integrity": read_json(INTEGRITY),
        "freeze_manifest": read_json(FREEZE_MANIFEST),
        "promotion": read_json(PROMOTION),
    }
    sources, source_scope = scope_sources_to_active_cohort(raw_sources, active_cohort, global_source_names={"mapping"})
    sources["active_cohort"] = normalized_active_cohort(active_cohort)
    sources["source_scope"] = source_scope
    sources["global_history_diagnostics"] = global_history_diagnostics(global_ledger, raw_sources)
    rows = build_waiting_rows(
        ledger,
        holding,
        entry_freeze,
        benchmark_freeze,
        sources["settlement"],
        as_of,
        active_cohort=active_cohort,
    )
    checks = build_checks(rows, sources)
    summary = build_summary(
        rows,
        checks,
        sources,
        as_of,
        active_cohort=active_cohort,
        global_ledger=global_ledger,
    )
    write_outputs(summary, rows, checks, sources)
    print(f"output_dir={OUT}")
    print(f"observation_rows={summary['observation_rows']}")
    print(f"can_claim_strong_rebound_industries={summary['can_claim_strong_rebound_industries']}")


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}) if path.exists() else pd.DataFrame()


def read_ledger() -> pd.DataFrame:
    if EVENT_LEDGER.exists():
        verify_ledger_checkpoint(EVENT_LEDGER)
        return pd.DataFrame(materialize_observations(read_events(EVENT_LEDGER)))
    if LEDGER.exists():
        raise RuntimeError("authoritative V5.25 JSONL ledger is missing; compatibility CSV cannot be used as evidence")
    return pd.DataFrame()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


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


def filter_frame_to_active_cohort(frame: pd.DataFrame, active_cohort: dict[str, Any] | None) -> pd.DataFrame:
    pair = validated_cohort_pair(active_cohort)
    if frame.empty:
        return frame.copy()
    if pair is None or not {"cohort_id", "cohort_manifest_hash"}.issubset(frame.columns):
        return frame.iloc[0:0].copy()
    cohort_id, manifest_hash = pair
    mask = frame["cohort_id"].astype(str).eq(cohort_id) & frame["cohort_manifest_hash"].astype(str).eq(manifest_hash)
    return frame.loc[mask].copy()


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
    *,
    global_source_names: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, bool]]:
    active_pair = validated_cohort_pair(active_cohort)
    global_names = global_source_names or set()
    scoped: dict[str, dict[str, Any]] = {}
    matches: dict[str, bool] = {}
    for name, payload in sources.items():
        match = name in global_names or (active_pair is not None and summary_cohort_pair(payload) == active_pair)
        matches[name] = match
        scoped[name] = dict(payload) if match else {}
    return scoped, matches


def global_history_diagnostics(frame: pd.DataFrame, sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    records = frame.fillna("").to_dict("records") if not frame.empty else []
    cohort_pairs = {
        (str(row.get("cohort_id", "")), str(row.get("cohort_manifest_hash", "")))
        for row in records
        if str(row.get("cohort_id", "")) or str(row.get("cohort_manifest_hash", ""))
    }
    return {
        "ledger_rows": len(records),
        "settled_rows": sum(str(row.get("settlement_status", "")) == "settled" for row in records),
        "cohort_count": len(cohort_pairs),
        "source_pairs": {
            name: list(pair) if (pair := summary_cohort_pair(payload)) else []
            for name, payload in sources.items()
        },
        "research_boundary": "全局历史只作独立诊断，不参与当前 cohort 等待室门禁或计数。",
    }


def build_waiting_rows(
    ledger: pd.DataFrame,
    holding: pd.DataFrame,
    entry_freeze: pd.DataFrame,
    benchmark_freeze: pd.DataFrame,
    settlement: dict[str, Any],
    as_of: date,
    *,
    active_cohort: dict[str, Any] | None = None,
) -> pd.DataFrame:
    columns = [
        "batch_id", "industry_code", "industry_name", "signal_date", "planned_entry_date",
        "planned_exit_date", "holding_status", "days_since_entry", "days_to_exit",
        "entry_price_frozen", "benchmark_frozen", "late_backfill_excluded",
        "integrity_eligible", "cohort_id", "cohort_manifest_hash", "settlement_status",
        "research_boundary",
    ]
    active = validated_active_cohort() if active_cohort is None else active_cohort
    ledger = filter_frame_to_active_cohort(ledger, active)
    holding = filter_frame_to_active_cohort(holding, active)
    entry_freeze = filter_frame_to_active_cohort(entry_freeze, active)
    benchmark_freeze = filter_frame_to_active_cohort(benchmark_freeze, active)
    if ledger.empty:
        return pd.DataFrame(columns=columns)
    holding_by_key = by_key(holding)
    entry_frozen = {
        row_key(row)
        for row in entry_freeze.fillna("").to_dict("records")
        if row.get("entry_price_freeze_status") == "frozen_on_time" and freeze_recorded_on_time(row)
    } if not entry_freeze.empty else set()
    benchmark_members = valid_benchmark_members(benchmark_freeze)
    rows = []
    for item in ledger.fillna("").to_dict("records"):
        key = row_key(item)
        hold = holding_by_key.get(key, {})
        entry_day = parse_date(item.get("planned_entry_date", ""))
        exit_day = parse_date(item.get("planned_exit_date", ""))
        rows.append({
            "batch_id": item.get("batch_id", ""),
            "industry_code": str(item.get("industry_code", "")).zfill(6),
            "industry_name": item.get("industry_name", ""),
            "signal_date": item.get("signal_date", ""),
            "planned_entry_date": item.get("planned_entry_date", ""),
            "planned_exit_date": item.get("planned_exit_date", ""),
            "holding_status": holding_status(hold, exit_day, as_of),
            "days_since_entry": "" if not entry_day else (as_of - entry_day).days,
            "days_to_exit": "" if not exit_day else (exit_day - as_of).days,
            "entry_price_frozen": key in entry_frozen,
            "benchmark_frozen": str(item.get("industry_code", "")).zfill(6) in benchmark_members.get(batch_key(item), set()),
            "late_backfill_excluded": item.get("late_backfill_excluded", ""),
            "integrity_eligible": item.get("integrity_eligible", ""),
            "cohort_id": item.get("cohort_id", ""),
            "cohort_manifest_hash": item.get("cohort_manifest_hash", ""),
            "settlement_status": settlement_status(item, settlement, exit_day, as_of),
            "research_boundary": "等待期只做前推观察和复核提示；不计算未来收益，不生成交易信号，不证明强行业 alpha。",
        })
    return pd.DataFrame(rows, columns=columns)


def by_key(frame: pd.DataFrame) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    if frame.empty:
        return {}
    return {row_key(row): row for row in frame.fillna("").to_dict("records")}


def row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("cohort_id", "")),
        str(row.get("cohort_manifest_hash", "")),
        str(row.get("batch_id", "")),
        str(row.get("industry_code", "")).zfill(6),
    )


def batch_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("cohort_id", "")),
        str(row.get("cohort_manifest_hash", "")),
        str(row.get("batch_id", "")),
        str(row.get("planned_entry_date", "")),
    )


def valid_benchmark_members(frame: pd.DataFrame) -> dict[tuple[str, str, str, str], set[str]]:
    valid: dict[tuple[str, str, str, str], set[str]] = {}
    if frame.empty:
        return valid
    rows = frame.fillna("").to_dict("records")
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(batch_key(row), []).append(row)
    for key, group in grouped.items():
        eligible = {
            str(row.get("industry_code", "")).zfill(6)
            for row in group
            if row.get("benchmark_entry_freeze_status") == "frozen_on_time" and freeze_recorded_on_time(row)
        }
        if len(eligible) >= 100:
            valid[key] = eligible
    return valid


def parse_date(value: object) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def holding_status(hold: dict[str, Any], exit_day: date | None, as_of: date) -> str:
    if exit_day and as_of >= exit_day:
        return "exit_settlement_due"
    return str(hold.get("holding_observation_status") or "holding_observation")


def settlement_status(row: dict[str, Any], settlement: dict[str, Any], exit_day: date | None, as_of: date) -> str:
    if is_true(row.get("late_backfill_excluded")) or not is_true(row.get("integrity_eligible")):
        return "integrity_excluded_no_settlement"
    if str(row.get("settlement_status", "")) == "settled":
        return "settled_by_v5_27"
    if exit_day and as_of >= exit_day:
        return "run_v5_27_required"
    if int(settlement.get("pending_rows", 0) or 0):
        return "pending_forward_settlement"
    return "not_settled"


def build_checks(rows: pd.DataFrame, sources: dict[str, dict[str, Any]]) -> pd.DataFrame:
    waiting = int(rows["settlement_status"].eq("pending_forward_settlement").sum()) if not rows.empty else 0
    due = int(rows["settlement_status"].eq("run_v5_27_required").sum()) if not rows.empty else 0
    active_validated = sources.get("active_cohort", {}).get("freeze_passed") is True
    return pd.DataFrame([
        check("active_cohort_revalidated", "pass" if active_validated else "blocked", f"freeze_passed={active_validated}", "等待室只能展示经 V5.31 重新校验的当前 cohort。"),
        check("current_observations", "pass" if len(rows) else "pending", f"rows={len(rows)}", "必须能列出当前前推观察样本。"),
        check("entry_price_freeze", "pass" if not rows.empty and rows["entry_price_frozen"].all() else "fail", f"frozen={int(rows['entry_price_frozen'].sum()) if not rows.empty else 0}", "候选行业入场点必须已冻结。"),
        check("benchmark_freeze", "pass" if not rows.empty and rows["benchmark_frozen"].all() else "fail", f"frozen={int(rows['benchmark_frozen'].sum()) if not rows.empty else 0}", "全行业基准入场点必须已冻结。"),
        check("current_integrity", "pass" if sources.get("integrity", {}).get("integrity_passed") else "blocked", f"integrity_passed={sources.get('integrity', {}).get('integrity_passed')}", "等待室不得绕过 V5.30 当前完整性结果。"),
        check("cohort_freeze", "pass" if sources.get("freeze_manifest", {}).get("freeze_passed") else "blocked", f"freeze_passed={sources.get('freeze_manifest', {}).get('freeze_passed')}", "等待室必须绑定 V5.31 当前 cohort 基线。"),
        check("promotion_block", "pass" if not sources.get("promotion", {}).get("promotion_ready") else "blocked", f"promotion_ready={sources.get('promotion', {}).get('promotion_ready')}", "完整性违规或样本不足时不得晋级。"),
        check("fund_flow_mapping_ready", "pass" if sources["mapping"].get("mapping_gate_passed") else "blocked", f"mapping_gate_passed={sources['mapping'].get('mapping_gate_passed')}", "下一批样本捕捉需要资金流行业映射可用。"),
        check("entry_gate_ready", "pass" if int(sources["entry_gate"].get("entry_missing_snapshot_count", 1) or 0) == 0 else "blocked", f"missing_snapshot={sources['entry_gate'].get('entry_missing_snapshot_count')}", "下一批样本捕捉需要入场门禁快照可用。"),
        check("next_signal_capture", "waiting_for_next_signal", f"appendable={sources['observer'].get('appendable_observation_count')}", "不新增交易信号，只等待下一批可观察样本。"),
        check("settlement_wait", "pending" if waiting or due else "pass", f"waiting={waiting}; due={due}", "未到退出日时等待；到期后运行 V5.27。"),
        check("goal_claim", "blocked", "no settled forward return", "V5.35 通过也不代表可以声称找到强反弹行业。"),
    ])


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_summary(
    rows: pd.DataFrame,
    checks: pd.DataFrame,
    sources: dict[str, dict[str, Any]],
    as_of: date,
    *,
    active_cohort: dict[str, Any] | None = None,
    global_ledger: pd.DataFrame | None = None,
) -> dict[str, Any]:
    active = validated_active_cohort() if active_cohort is None else active_cohort
    active_pair = validated_cohort_pair(active)
    scoped_rows = filter_frame_to_active_cohort(rows, active)
    history = global_ledger if global_ledger is not None else rows
    due = int(scoped_rows["settlement_status"].eq("run_v5_27_required").sum()) if not scoped_rows.empty else 0
    ready_checks = int(checks["status"].eq("pass").sum())
    blocked_checks = int(checks["status"].eq("blocked").sum())
    pending_checks = int(checks["status"].isin(["pending", "waiting_for_next_signal"]).sum())
    return {
        "version": "5.35.1",
        "policy_id": "fund_flow_waiting_room_v5_35",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "active_cohort_id": active_pair[0] if active_pair else "",
        "active_cohort_manifest_hash": active_pair[1] if active_pair else "",
        "active_cohort_validated": active_pair is not None,
        "observation_rows": int(len(scoped_rows)),
        "global_history_observation_rows": int(len(history)),
        "global_history_settled_rows": int(history["settlement_status"].astype(str).eq("settled").sum()) if not history.empty and "settlement_status" in history.columns else 0,
        "holding_observation_count": int(scoped_rows["holding_status"].eq("holding_observation_research_only").sum()) if not scoped_rows.empty else 0,
        "exit_settlement_due_count": due,
        "integrity_excluded_count": int(scoped_rows["settlement_status"].eq("integrity_excluded_no_settlement").sum()) if not scoped_rows.empty else 0,
        "next_action_date": sources["calendar"].get("next_action_date", ""),
        "next_command": sources["calendar"].get("next_command", ""),
        "readiness_ready_count": ready_checks,
        "readiness_blocked_count": blocked_checks,
        "readiness_pending_count": pending_checks,
        "current_tradeable": False,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": (
            "research_only_waiting_room_no_valid_active_cohort"
            if active_pair is None
            else ("research_only_waiting_room_exit_due" if due else "research_only_waiting_room_active")
        ),
        "final_verdict": "V5.35 是前推等待期实盘辅助层；它只跟踪观察样本、冻结覆盖和下一步动作，不结算未来收益，不证明强行业 alpha。",
    }


def write_outputs(summary: dict[str, Any], rows: pd.DataFrame, checks: pd.DataFrame, sources: dict[str, dict[str, Any]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    rows.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, rows, checks), encoding="utf-8")
    checks.to_csv(DEBUG / "waiting_room_checks.csv", index=False, encoding="utf-8-sig")
    rows.to_csv(DEBUG / "waiting_room_rows.csv", index=False, encoding="utf-8-sig")
    write_json(DEBUG / "source_snapshot.json", sources)


def render_report(summary: dict[str, Any], rows: pd.DataFrame, checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.35 资金流前推等待期实盘辅助层",
        "",
        summary["final_verdict"],
        "",
        f"- as-of 日期：{summary['as_of_date']}",
        f"- 当前是否可交易：否",
        f"- 当前是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        f"- 观察样本数：{summary['observation_rows']}",
        f"- 持有观察中：{summary['holding_observation_count']}",
        f"- 到期待结算：{summary['exit_settlement_due_count']}",
        f"- 下一动作日期：{summary['next_action_date']}",
        f"- 下一命令：`{summary['next_command']}`",
        "",
        "当前能做：观察持有样本、等待退出结算、准备下一批样本捕捉。",
        "",
        "当前不能做：不能证明 alpha，不能自动交易，不能调参追结果。",
        "",
        "## 观察样本",
        "",
        rows.to_markdown(index=False) if len(rows) else "无观察样本。",
        "",
        "## 准备度检查",
        "",
        checks.to_markdown(index=False),
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    active_fixture = {"freeze_passed": True, "cohort_id": "c1", "manifest_hash": "h1"}
    ledger = pd.DataFrame([{
        "batch_id": "b", "industry_code": "1", "industry_name": "A", "signal_date": "2026-01-01",
        "planned_entry_date": "2026-01-02", "planned_exit_date": "2026-01-10", "settlement_status": "not_due",
        "integrity_eligible": True, "late_backfill_excluded": False, "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }])
    holding = pd.DataFrame([{
        "batch_id": "b", "industry_code": "1", "holding_observation_status": "holding_observation_research_only",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }])
    entry = pd.DataFrame([{
        "batch_id": "b", "industry_code": "1", "planned_entry_date": "2026-01-02",
        "actual_entry_date": "2026-01-02", "as_of_date": "2026-01-02",
        "freeze_at_utc": "2026-01-02T07:30:00Z", "entry_price_freeze_status": "frozen_on_time",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }])
    benchmark = pd.DataFrame([{
        "batch_id": "b", "planned_entry_date": "2026-01-02", "actual_entry_date": "2026-01-02",
        "as_of_date": "2026-01-02", "freeze_at_utc": "2026-01-02T07:30:00Z",
        "benchmark_entry_freeze_status": "frozen_on_time", "industry_code": "1" if index == 0 else f"{index + 100000:06d}",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    } for index in range(100)])
    rows = build_waiting_rows(
        ledger, holding, entry, benchmark, {"pending_rows": 1}, date(2026, 1, 5),
        active_cohort=active_fixture,
    )
    assert rows["holding_status"].iloc[0] == "holding_observation_research_only"
    assert rows["days_since_entry"].iloc[0] == 3
    assert rows["days_to_exit"].iloc[0] == 5
    assert bool(rows["entry_price_frozen"].iloc[0]) is True
    assert bool(rows["benchmark_frozen"].iloc[0]) is True
    assert rows["settlement_status"].iloc[0] == "pending_forward_settlement"
    due = build_waiting_rows(
        ledger, holding, entry, benchmark, {"pending_rows": 1}, date(2026, 1, 10),
        active_cohort=active_fixture,
    )
    assert due["holding_status"].iloc[0] == "exit_settlement_due"
    assert due["settlement_status"].iloc[0] == "run_v5_27_required"
    checks = build_checks(rows, {
        "active_cohort": active_fixture,
        "mapping": {"mapping_gate_passed": True},
        "entry_gate": {"entry_missing_snapshot_count": 0},
        "observer": {"appendable_observation_count": 0},
        "integrity": {"integrity_passed": True},
        "freeze_manifest": {"freeze_passed": True},
        "promotion": {"promotion_ready": False},
    })
    assert checks.set_index("check").loc["fund_flow_mapping_ready", "status"] == "pass"
    assert checks.set_index("check").loc["goal_claim", "status"] == "blocked"
    summary = build_summary(
        rows,
        checks,
        {"calendar": {"next_action_date": "2026-01-10", "next_command": "x"}},
        date(2026, 1, 5),
        active_cohort=active_fixture,
    )
    assert summary["production_ready"] is False
    assert summary["auto_execution_allowed"] is False
    assert summary["can_claim_strong_rebound_industries"] is False
    print("self_check=pass")


if __name__ == "__main__":
    main()
