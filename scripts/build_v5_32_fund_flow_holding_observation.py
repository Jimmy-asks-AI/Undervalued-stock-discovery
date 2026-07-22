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
ENTRY_ROWS = ROOT / "outputs" / "audit" / "fund_flow_forward_entry_gate_v5_26" / "debug" / "entry_gate_rows.csv"
LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv"
EVENT_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
OUT = ROOT / "outputs" / "audit" / "fund_flow_holding_observation_v5_32"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.32 holding observation status for fund-flow forward samples.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    as_of = date.fromisoformat(args.as_of_date)
    if as_of > date.today():
        parser.error(f"--as-of-date {args.as_of_date} is in the future; holding observation must use current or past dates.")

    active_cohort = validated_active_cohort()
    global_entry = read_csv(ENTRY_ROWS)
    global_ledger = read_ledger()
    entry = filter_active_cohort(global_entry, active_cohort)
    ledger = filter_active_cohort(global_ledger, active_cohort)
    rows = build_holding_rows(entry, as_of)
    checks = build_checks(rows, ledger)
    summary = build_summary(rows, checks, as_of, active_cohort=active_cohort, global_ledger=global_ledger)
    write_outputs(summary, rows, checks, ledger, global_ledger=global_ledger, global_entry=global_entry)
    print(f"output_dir={OUT}")
    print(f"holding_observation_count={summary['holding_observation_count']}")
    print(f"goal_ready={summary['goal_ready']}")


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}) if path.exists() else pd.DataFrame()


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


def build_holding_rows(entry: pd.DataFrame, as_of: date) -> pd.DataFrame:
    columns = [
        "as_of_date", "batch_id", "industry_code", "industry_name", "signal_date",
        "planned_entry_date", "planned_exit_date", "entry_gate_status",
        "holding_observation_status", "entry_cache_exists", "entry_allowed",
        "fund_flow_research_status", "historical_failure_flag", "observation_id",
        "cohort_id", "cohort_manifest_hash", "sample_scope", "qualified_for_goal",
        "integrity_eligible", "late_backfill_excluded", "research_boundary",
    ]
    if entry.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for item in entry.fillna("").to_dict("records"):
        planned_entry = parse_date(item.get("planned_entry_date", ""))
        planned_exit = parse_date(item.get("planned_exit_date", ""))
        reviewed = item.get("entry_gate_status") in {"entry_review_required_research_only", "entry_reviewed_research_only"} and str(item.get("entry_cache_exists", "")).lower() == "true"
        excluded = is_true(item.get("late_backfill_excluded")) or not is_true(item.get("integrity_eligible"))
        if excluded:
            status = "holding_observation_exploratory_excluded"
        elif reviewed and planned_entry and planned_entry <= as_of and (not planned_exit or as_of < planned_exit):
            status = "holding_observation_research_only"
        elif reviewed and planned_exit and as_of >= planned_exit:
            status = "exit_settlement_due"
        elif planned_entry and as_of < planned_entry:
            status = "entry_not_due"
        else:
            status = "entry_not_reviewed"
        rows.append({
            "as_of_date": as_of.isoformat(),
            "batch_id": item.get("batch_id", ""),
            "industry_code": str(item.get("industry_code", "")).zfill(6),
            "industry_name": item.get("industry_name", ""),
            "signal_date": item.get("signal_date", ""),
            "planned_entry_date": item.get("planned_entry_date", ""),
            "planned_exit_date": item.get("planned_exit_date", ""),
            "entry_gate_status": item.get("entry_gate_status", ""),
            "holding_observation_status": status,
            "entry_cache_exists": item.get("entry_cache_exists", ""),
            "entry_allowed": item.get("entry_allowed", ""),
            "fund_flow_research_status": item.get("fund_flow_research_status", ""),
            "historical_failure_flag": item.get("historical_failure_flag", ""),
            "observation_id": item.get("observation_id", ""),
            "cohort_id": item.get("cohort_id", ""),
            "cohort_manifest_hash": item.get("cohort_manifest_hash", ""),
            "sample_scope": item.get("sample_scope", ""),
            "qualified_for_goal": item.get("qualified_for_goal", ""),
            "integrity_eligible": item.get("integrity_eligible", ""),
            "late_backfill_excluded": item.get("late_backfill_excluded", ""),
            "research_boundary": "持有观察只记录前推状态；不代表自动交易，不计算未来收益，不证明强行业 alpha。",
        })
    return pd.DataFrame(rows, columns=columns)


def parse_date(value: object) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def build_checks(rows: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    holding = int(rows["holding_observation_status"].eq("holding_observation_research_only").sum()) if not rows.empty else 0
    due = int(rows["holding_observation_status"].eq("exit_settlement_due").sum()) if not rows.empty else 0
    row_keys = set(zip(rows.get("batch_id", pd.Series(dtype=str)).astype(str), rows.get("industry_code", pd.Series(dtype=str)).astype(str).str.zfill(6)))
    ledger_keys = set(zip(ledger.get("batch_id", pd.Series(dtype=str)).astype(str), ledger.get("industry_code", pd.Series(dtype=str)).astype(str).str.zfill(6)))
    excluded = int(rows["holding_observation_status"].eq("holding_observation_exploratory_excluded").sum()) if not rows.empty else 0
    return pd.DataFrame([
        check("entry_rows_exist", "pass" if len(rows) else "pending", f"rows={len(rows)}", "必须先有 V5.26 入场门禁明细。"),
        check("ledger_keys_match", "pass" if row_keys == ledger_keys else "fail", f"entry_keys={len(row_keys)}; ledger_keys={len(ledger_keys)}", "持有观察必须与前推账本按 batch+industry 精确对应。"),
        check("integrity_excluded_rows", "blocked" if excluded else "pass", f"excluded={excluded}", "完整性不足的样本继续观察，但永久不得晋级。"),
        check("holding_or_exit_due", "pass" if holding or due else "pending", f"holding={holding}; exit_due={due}", "入场复核后样本应进入持有观察或退出结算状态。"),
        check("goal_claim", "fail", "no settled forward return", "未到退出结算前不能声称找到强反弹行业。"),
    ])


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_summary(
    rows: pd.DataFrame,
    checks: pd.DataFrame,
    as_of: date,
    *,
    active_cohort: dict[str, Any] | None = None,
    global_ledger: pd.DataFrame | None = None,
) -> dict[str, Any]:
    statuses = rows["holding_observation_status"].value_counts().to_dict() if not rows.empty else {}
    active = active_cohort or {}
    return {
        "version": "5.32.1",
        "policy_id": "fund_flow_holding_observation_v5_32",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "observation_rows": int(len(rows)),
        "active_cohort_id": str(active.get("cohort_id", "")),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", "")),
        "active_cohort_freeze_passed": active.get("freeze_passed") is True,
        "global_history_observation_rows": int(len(global_ledger)) if global_ledger is not None else int(len(rows)),
        "holding_observation_count": int(statuses.get("holding_observation_research_only", 0)),
        "exit_settlement_due_count": int(statuses.get("exit_settlement_due", 0)),
        "integrity_excluded_count": int(statuses.get("holding_observation_exploratory_excluded", 0)),
        "fail_count": int(checks["status"].eq("fail").sum()),
        "pending_count": int(checks["status"].eq("pending").sum()),
        "next_action": "等待计划退出日后运行 V5.27 结算真实 forward return。",
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_holding_observation" if statuses.get("holding_observation_research_only", 0) else "research_only_no_active_holding_observation",
        "final_verdict": "V5.32 只确认资金流前推样本已进入持有观察期；未结算真实收益前不能声称找到强反弹行业。",
    }


def write_outputs(
    summary: dict[str, Any],
    rows: pd.DataFrame,
    checks: pd.DataFrame,
    ledger: pd.DataFrame,
    *,
    global_ledger: pd.DataFrame | None = None,
    global_entry: pd.DataFrame | None = None,
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    rows.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, rows, checks), encoding="utf-8")
    rows.to_csv(DEBUG / "holding_observation_rows.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(DEBUG / "holding_observation_checks.csv", index=False, encoding="utf-8-sig")
    ledger.to_csv(DEBUG / "forward_ledger_snapshot.csv", index=False, encoding="utf-8-sig")
    history = global_ledger if global_ledger is not None else ledger
    entry_history = global_entry if global_entry is not None else rows.iloc[0:0]
    history.to_csv(DEBUG / "global_forward_ledger_history.csv", index=False, encoding="utf-8-sig")
    entry_history.to_csv(DEBUG / "global_entry_gate_history.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], rows: pd.DataFrame, checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.32 资金流前推持有观察状态",
        "",
        summary["final_verdict"],
        "",
        f"- as-of 日期：{summary['as_of_date']}",
        f"- 观察行数：{summary['observation_rows']}",
        f"- 持有观察中：{summary['holding_observation_count']}",
        f"- 到期待结算：{summary['exit_settlement_due_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 持有观察明细",
        "",
        rows.to_markdown(index=False) if len(rows) else "无持有观察。",
        "",
        "## 检查",
        "",
        checks.to_markdown(index=False),
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    entry = pd.DataFrame([{
        "batch_id": "b", "industry_code": "1", "industry_name": "A", "signal_date": "2026-01-01",
        "planned_entry_date": "2026-01-02", "planned_exit_date": "2026-01-10",
        "entry_gate_status": "entry_review_required_research_only", "entry_cache_exists": True,
        "entry_allowed": False, "fund_flow_research_status": "current_dual_positive_observation_only", "historical_failure_flag": False,
        "integrity_eligible": True, "late_backfill_excluded": False,
    }])
    rows = build_holding_rows(entry, date(2026, 1, 3))
    assert rows["holding_observation_status"].iloc[0] == "holding_observation_research_only"
    entry.loc[0, "entry_gate_status"] = "entry_reviewed_research_only"
    rows = build_holding_rows(entry, date(2026, 1, 3))
    assert rows["holding_observation_status"].iloc[0] == "holding_observation_research_only"
    rows = build_holding_rows(entry, date(2026, 1, 10))
    assert rows["holding_observation_status"].iloc[0] == "exit_settlement_due"
    checks = build_checks(rows, entry)
    assert checks[checks["check"].eq("goal_claim")]["status"].iloc[0] == "fail"
    print("self_check=pass")


if __name__ == "__main__":
    main()


