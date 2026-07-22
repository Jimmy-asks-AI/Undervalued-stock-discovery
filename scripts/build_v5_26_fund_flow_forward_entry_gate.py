#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from fund_flow_forward_evidence import is_true, materialize_observations, read_events, verify_ledger_checkpoint


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv"
EVENT_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
CACHE = ROOT / "data_catalog" / "cache" / "industry_fund_flow" / "ths"
OUT = ROOT / "outputs" / "audit" / "fund_flow_forward_entry_gate_v5_26"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.26 entry gate for V5.25 fund-flow forward observations.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if error := future_date_error(args.as_of_date, date.today()):
        parser.error(error)

    global_ledger = read_ledger()
    active_cohort = validated_active_cohort()
    ledger = filter_active_cohort(global_ledger, active_cohort)
    rows = build_entry_gate(ledger, args.as_of_date)
    checks = build_checks(rows, args.as_of_date)
    summary = build_summary(rows, checks, args.as_of_date, active_cohort=active_cohort, global_ledger=global_ledger)
    write_outputs(summary, rows, ledger, checks, global_ledger=global_ledger)
    print(f"output_dir={OUT}")
    print(f"entry_allowed_count={summary['entry_allowed_count']}")
    print(f"entry_not_due_count={summary['entry_not_due_count']}")


def future_date_error(value: str, today: date) -> str:
    if date.fromisoformat(value) > today:
        return f"--as-of-date {value} is in the future; rerun on or after that date."
    return ""


def read_ledger() -> pd.DataFrame:
    if EVENT_LEDGER.exists():
        verify_ledger_checkpoint(EVENT_LEDGER)
        return pd.DataFrame(materialize_observations(read_events(EVENT_LEDGER)))
    if LEDGER.exists():
        raise RuntimeError("authoritative V5.25 JSONL ledger is missing; compatibility CSV cannot be used as evidence")
    return pd.DataFrame()


def filter_active_cohort(frame: pd.DataFrame, active: dict[str, Any] | None) -> pd.DataFrame:
    active = active or {}
    if frame.empty or active.get("freeze_passed") is not True:
        return frame.iloc[0:0].copy()
    if not {"cohort_id", "cohort_manifest_hash"}.issubset(frame.columns):
        return frame.iloc[0:0].copy()
    return frame[
        frame["cohort_id"].astype(str).eq(str(active.get("cohort_id", "")))
        & frame["cohort_manifest_hash"].astype(str).eq(str(active.get("manifest_hash", "")))
    ].copy()


def build_entry_gate(ledger: pd.DataFrame, as_of_text: str) -> pd.DataFrame:
    if ledger.empty:
        return pd.DataFrame(columns=[
            "as_of_date", "batch_id", "industry_code", "industry_name", "planned_entry_date",
            "planned_exit_date", "entry_gate_status", "entry_allowed", "required_action",
        ])
    as_of = date.fromisoformat(as_of_text)
    rows = []
    for _, row in ledger.iterrows():
        planned_entry_text = str(row.get("planned_entry_date", ""))
        planned = date.fromisoformat(planned_entry_text)
        entry_cache_exists = (CACHE / planned_entry_text / "ths_industry_fund_flow_now.csv").exists()
        integrity_eligible = is_true(row.get("integrity_eligible"))
        late_excluded = is_true(row.get("late_backfill_excluded"))
        if late_excluded or not integrity_eligible:
            status = "entry_observation_only_integrity_excluded"
            allowed = False
            action = "仅保留探索观察；时间、来源或 cohort 完整性不足，永久不得进入目标晋级。"
        elif as_of < planned:
            status = "entry_not_due"
            allowed = False
            action = "等到计划入场日，先刷新当日资金流快照。"
        elif as_of == planned and not entry_cache_exists:
            status = "entry_blocked_missing_entry_day_snapshot"
            allowed = False
            action = "先运行 live refresh 或缓存入场日资金流快照。"
        elif as_of == planned:
            status = "entry_review_required_research_only"
            allowed = False
            action = "只允许人工复核；研究系统不生成入场指令。"
        elif entry_cache_exists:
            status = "entry_reviewed_research_only"
            allowed = False
            action = "计划入场日快照已存在；继续研究持有观察，不生成交易指令。"
        else:
            status = "entry_window_missed_or_manual_review_required"
            allowed = False
            action = "计划入场日已过且缺少入场日快照；该批次只保留观察。"
        rows.append({
            "as_of_date": as_of_text,
            "batch_id": row.get("batch_id", ""),
            "industry_code": str(row.get("industry_code", "")).zfill(6),
            "industry_name": row.get("industry_name", ""),
            "signal_date": row.get("signal_date", ""),
            "planned_entry_date": row.get("planned_entry_date", ""),
            "planned_exit_date": row.get("planned_exit_date", ""),
            "entry_gate_status": status,
            "entry_allowed": allowed,
            "entry_cache_exists": entry_cache_exists,
            "fund_flow_research_status": row.get("fund_flow_research_status", ""),
            "historical_failure_flag": row.get("historical_failure_flag", ""),
            "observation_id": row.get("observation_id", ""),
            "cohort_id": row.get("cohort_id", ""),
            "cohort_manifest_hash": row.get("cohort_manifest_hash", ""),
            "sample_scope": row.get("sample_scope", ""),
            "qualified_for_goal": row.get("qualified_for_goal", ""),
            "integrity_eligible": integrity_eligible,
            "late_backfill_excluded": late_excluded,
            "required_action": action,
        })
    return pd.DataFrame(rows)


def build_checks(rows: pd.DataFrame, as_of_date: str) -> pd.DataFrame:
    reviewed = int(rows.get("entry_gate_status", pd.Series(dtype=str)).isin(["entry_review_required_research_only", "entry_reviewed_research_only"]).sum()) if not rows.empty else 0
    cache_exists = bool(rows.get("entry_cache_exists", pd.Series([False])).any())
    excluded = int(rows.get("entry_gate_status", pd.Series(dtype=str)).eq("entry_observation_only_integrity_excluded").sum()) if not rows.empty else 0
    return pd.DataFrame([
        check("ledger_rows_exist", "pass" if len(rows) else "pending", f"rows={len(rows)}", "必须有前推观察账本。"),
        check("entry_day_snapshot_available", "pass" if cache_exists else "pending", f"as_of_date={as_of_date}; cache_exists={cache_exists}", "入场日必须先刷新当日资金流。"),
        check("entry_review_ready", "pass" if reviewed else "pending", f"review_or_allowed_count={reviewed}; entry_allowed_count={int(rows.get('entry_allowed', pd.Series(dtype=bool)).sum())}", "研究系统只要求形成复核观察，不生成自动入场指令。"),
        check("integrity_excluded_rows", "blocked" if excluded else "pass", f"excluded={excluded}", "时间、来源或 cohort 不完整的记录只能保留为探索观察。"),
        check("auto_execution_blocked", "pass", "auto_execution_allowed=False", "研究系统不生成自动交易指令。"),
        check("can_claim_goal", "fail", "forward samples not settled", "未退出结算前不能证明强行业 alpha。"),
    ])


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_summary(
    rows: pd.DataFrame,
    checks: pd.DataFrame,
    as_of_date: str,
    *,
    active_cohort: dict[str, Any] | None = None,
    global_ledger: pd.DataFrame | None = None,
) -> dict[str, Any]:
    counts = rows["entry_gate_status"].value_counts().to_dict() if not rows.empty else {}
    active = active_cohort or {}
    return {
        "version": "5.26.1",
        "policy_id": "fund_flow_forward_entry_gate_v5_26",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of_date,
        "ledger_row_count": int(len(rows)),
        "active_cohort_id": str(active.get("cohort_id", "")),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", "")),
        "active_cohort_freeze_passed": active.get("freeze_passed") is True,
        "global_history_ledger_rows": int(len(global_ledger)) if global_ledger is not None else int(len(rows)),
        "entry_not_due_count": int(counts.get("entry_not_due", 0)),
        "entry_missing_snapshot_count": int(counts.get("entry_blocked_missing_entry_day_snapshot", 0)),
        "entry_review_required_count": int(counts.get("entry_review_required_research_only", 0) + counts.get("entry_reviewed_research_only", 0)),
        "entry_allowed_count": int(rows.get("entry_allowed", pd.Series(dtype=bool)).sum()),
        "integrity_excluded_count": int(counts.get("entry_observation_only_integrity_excluded", 0)),
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "pass_count": int(checks["status"].eq("pass").sum()),
        "fail_count": int(checks["status"].eq("fail").sum()),
        "pending_count": int(checks["status"].eq("pending").sum()),
        "best_status": "research_only_entry_gate_waiting",
        "final_verdict": "V5.26 已建立资金流前推观察的入场日门禁；未刷新入场日并人工确认前，不得把观察样本计入已入场或已验证。",
    }


def write_outputs(
    summary: dict[str, Any],
    rows: pd.DataFrame,
    ledger: pd.DataFrame,
    checks: pd.DataFrame,
    *,
    global_ledger: pd.DataFrame | None = None,
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    rows.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, rows, checks), encoding="utf-8")
    rows.to_csv(DEBUG / "entry_gate_rows.csv", index=False, encoding="utf-8-sig")
    ledger.to_csv(DEBUG / "forward_ledger_snapshot.csv", index=False, encoding="utf-8-sig")
    history = global_ledger if global_ledger is not None else ledger
    history.to_csv(DEBUG / "global_forward_ledger_history.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(DEBUG / "entry_gate_checks.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], rows: pd.DataFrame, checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.26 资金流前推观察入场门禁",
        "",
        summary["final_verdict"],
        "",
        f"- 截止日期：{summary['as_of_date']}",
        f"- 账本行数：{summary['ledger_row_count']}",
        f"- 未到入场日：{summary['entry_not_due_count']}",
        f"- 缺入场日快照：{summary['entry_missing_snapshot_count']}",
        f"- 人工复核观察：{summary['entry_review_required_count']}",
        f"- 系统允许入场数：{summary['entry_allowed_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 门禁明细",
        "",
        rows.to_markdown(index=False) if not rows.empty else "当前没有前推观察账本。",
        "",
        "## 检查",
        "",
        checks.to_markdown(index=False),
        "",
        "边界：V5.26 只做入场日前置门禁，不计算未来收益，不生成交易指令。",
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    ledger = pd.DataFrame([{
        "batch_id": "b",
        "industry_code": "801001",
        "industry_name": "样本",
        "signal_date": "2026-06-22",
        "planned_entry_date": "2026-06-23",
        "planned_exit_date": "2026-07-21",
        "integrity_eligible": True,
        "late_backfill_excluded": False,
    }])
    rows = build_entry_gate(ledger, "2026-06-22")
    assert rows["entry_gate_status"].iloc[0] == "entry_not_due"
    rows = build_entry_gate(ledger, "2026-06-23")
    assert rows["entry_gate_status"].iloc[0] in {"entry_blocked_missing_entry_day_snapshot", "entry_review_required_research_only"}
    rows = build_entry_gate(ledger, "2026-06-25")
    assert rows["entry_gate_status"].iloc[0] in {"entry_window_missed_or_manual_review_required", "entry_reviewed_research_only"}
    checks = build_checks(rows, "2026-06-25")
    assert checks[checks["check"].eq("auto_execution_blocked")]["status"].iloc[0] == "pass"
    assert checks[checks["check"].eq("can_claim_goal")]["status"].iloc[0] == "fail"
    assert future_date_error("2026-06-24", date(2026, 6, 23))
    print("self_check=pass")


if __name__ == "__main__":
    main()


