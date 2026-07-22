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
LEDGER = ROOT / "logs" / "v4_85_parent_neutral_forward_ledger.csv"
V488_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_pre_entry_audit_v4_88" / "run_summary.json"
V488_CHECKS = ROOT / "outputs" / "industry_rebound_leader_pre_entry_audit_v4_88" / "debug" / "pre_entry_checks.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_entry_batch_gate_v4_90"
DEBUG = OUT / "debug"

CHECK_FIELDS = ["dimension", "check", "current", "required", "status", "interpretation"]
OPERATOR_FIELDS = ["priority", "step", "status", "command", "condition", "operator_note"]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.90 entry gate for frozen V4.85 rebound-leader forward batch.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true", help="Update the V4.85 forward ledger when the entry gate is due.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    as_of = date.fromisoformat(args.as_of_date)
    ledger = read_rows(LEDGER)
    v488 = read_json(V488_SUMMARY)
    v488_checks = read_rows(V488_CHECKS)
    decision = build_decision(as_of, ledger, v488, v488_checks)
    rows_after = apply_decision(ledger, decision) if args.apply else ledger
    if args.apply and decision["apply_allowed"]:
        write_rows(LEDGER, rows_after, list(rows_after[0]) if rows_after else [])
    write_outputs(as_of, decision, ledger, rows_after, v488_checks, args.apply)
    print(f"output_dir={OUT}")
    print(f"entry_gate_status={decision['entry_gate_status']}")
    print(f"apply_allowed={decision['apply_allowed']}")


def build_decision(as_of: date, ledger: list[dict[str, str]], v488: dict[str, Any], checks: list[dict[str, str]]) -> dict[str, Any]:
    planned_entry = parse_date(str(v488.get("planned_entry_date", "")))
    planned_exit = parse_date(str(v488.get("planned_exit_date", "")))
    audit_date = parse_date(str(v488.get("as_of_date", "")))
    fail_count = int_value(v488.get("fail_count"))
    pass_count = int_value(v488.get("pass_count"))
    existing_statuses = sorted({row.get("outcome_status", "") for row in ledger})
    command_date = planned_entry.isoformat() if planned_entry and as_of < planned_entry else as_of.isoformat()
    if not planned_entry:
        status = "blocked_missing_planned_entry"
        action = "先刷新 V4.86/V4.88，补齐计划入场日。"
        apply_allowed = False
    elif as_of < planned_entry:
        status = "not_due"
        action = f"{planned_entry.isoformat()} 入场日重新刷新后再决定 entered/skipped。"
        apply_allowed = False
    elif as_of > planned_entry:
        status = "blocked_entry_gate_overdue"
        action = "已过计划入场日且没有同日门控确认；该批次不得自动计入有效前推样本。"
        apply_allowed = False
    elif audit_date != as_of:
        status = "blocked_same_day_refresh_required"
        action = f"先运行 python .\\scripts\\run_v4_71_live_refresh.py --trade-date {as_of.isoformat()}，再重新执行 V4.90。"
        apply_allowed = False
    elif fail_count != 0 or str(v488.get("pre_entry_status", "")) != "pre_entry_consistent":
        status = "skipped_entry_gate_failed"
        action = "V4.88 入场前审计失败；用 --apply 标记为 skipped，不进入有效前推样本。"
        apply_allowed = True
    elif not ledger:
        status = "blocked_missing_forward_ledger"
        action = "缺少 V4.85 前推账本，不能进入样本。"
        apply_allowed = False
    elif any(row.get("outcome_status") not in {"pending_forward_observation", "skipped_forward_observation", "settled_forward_observation"} for row in ledger):
        status = "blocked_unknown_ledger_status"
        action = "账本中存在未知 outcome_status，先人工检查。"
        apply_allowed = False
    else:
        status = "entered_research_observation"
        action = "用 --apply 将该批次标记为 entry_confirmed；仍保持 research_only，等待退出日结算。"
        apply_allowed = True

    return {
        "entry_gate_status": status,
        "as_of_date": as_of.isoformat(),
        "planned_entry_date": planned_entry.isoformat() if planned_entry else "",
        "planned_exit_date": planned_exit.isoformat() if planned_exit else "",
        "v488_audit_date": audit_date.isoformat() if audit_date else "",
        "v488_pre_entry_status": str(v488.get("pre_entry_status", "")),
        "v488_pass_count": pass_count,
        "v488_fail_count": fail_count,
        "ledger_rows": len(ledger),
        "ledger_outcome_statuses": "|".join(existing_statuses),
        "apply_allowed": apply_allowed,
        "recommended_action": action,
        "entry_refresh_command": f"python .\\scripts\\run_v4_71_live_refresh.py --trade-date {command_date}",
        "entry_gate_command": f"python .\\scripts\\build_v4_90_rebound_leader_entry_batch_gate.py --as-of-date {command_date}",
        "entry_apply_command": f"python .\\scripts\\build_v4_90_rebound_leader_entry_batch_gate.py --as-of-date {command_date} --apply" if apply_allowed else "",
        "checks": build_checks(as_of, ledger, v488, checks, status),
    }


def build_checks(as_of: date, ledger: list[dict[str, str]], v488: dict[str, Any], checks: list[dict[str, str]], status: str) -> list[dict[str, str]]:
    planned_entry = str(v488.get("planned_entry_date", ""))
    audit_date = str(v488.get("as_of_date", ""))
    return [
        check("timing", "as_of_reaches_entry_date", as_of.isoformat(), f">= {planned_entry}", "pass" if parse_date(planned_entry) and as_of >= parse_date(planned_entry) else "pending", "入场日之前不能确认 entered/skipped。"),
        check("timing", "same_day_v488_refresh", audit_date, f"== {as_of.isoformat()}", "pass" if audit_date == as_of.isoformat() else ("pending" if status == "not_due" else "fail"), "入场日必须使用同日刷新后的 V4.88 审计。"),
        check("pre_entry", "v488_pre_entry_consistent", str(v488.get("pre_entry_status", "")), "pre_entry_consistent", "pass" if v488.get("pre_entry_status") == "pre_entry_consistent" else "fail", "冻结候选、重算候选和账本必须一致。"),
        check("pre_entry", "v488_no_failed_checks", str(v488.get("fail_count", "")), "0", "pass" if int_value(v488.get("fail_count")) == 0 else "fail", "任一入场前审计失败都不得进入有效样本。"),
        check("ledger", "forward_ledger_exists", str(len(ledger)), "> 0", "pass" if ledger else "fail", "没有前推账本就不能结算未来强反弹表现。"),
        check("ledger", "only_known_outcome_status", "|".join(sorted({row.get("outcome_status", "") for row in ledger})), "known statuses", "pass" if all(row.get("outcome_status") in {"pending_forward_observation", "skipped_forward_observation", "settled_forward_observation"} for row in ledger) else "fail", "未知账本状态需要人工检查。"),
        check("gate", "entry_gate_status", status, "entered/skipped/not_due/blocked", "pass" if status in {"entered_research_observation", "skipped_entry_gate_failed", "not_due"} else "fail", "只有 entered 的批次才进入后续真实前推评价。"),
    ]


def apply_decision(rows: list[dict[str, str]], decision: dict[str, Any]) -> list[dict[str, str]]:
    status = str(decision["entry_gate_status"])
    if not decision["apply_allowed"]:
        return rows
    out = []
    for row in rows:
        item = dict(row)
        if item.get("outcome_status") != "pending_forward_observation":
            out.append(item)
            continue
        if status == "entered_research_observation":
            item["decision"] = "entered_research_observation"
            item["actual_entry_date"] = str(decision["as_of_date"])
            item["settlement_status"] = "entry_confirmed"
            item["settlement_notes"] = "V4.90 入场日门控通过；仍为 research_only，等待退出日后结算真实收益。"
        elif status == "skipped_entry_gate_failed":
            item["decision"] = "skipped_entry_gate_failed"
            item["outcome_status"] = "skipped_forward_observation"
            item["settlement_status"] = "skipped_entry_gate_failed"
            item["settlement_notes"] = "V4.90 入场日门控失败；该批次不计入强反弹行业前推评价。"
        out.append(item)
    return out


def write_outputs(
    as_of: date,
    decision: dict[str, Any],
    rows_before: list[dict[str, str]],
    rows_after: list[dict[str, str]],
    v488_checks: list[dict[str, str]],
    applied: bool,
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    checks = decision.pop("checks")
    summary = {
        "version": "4.90.0",
        "policy_id": "industry_rebound_leader_entry_batch_gate_v4_90",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **decision,
        "apply_requested": applied,
        "ledger_would_change": rows_before != rows_after,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": final_verdict(str(decision["entry_gate_status"])),
    }
    write_csv(OUT / "top_candidates.csv", checks, CHECK_FIELDS)
    write_csv(DEBUG / "entry_gate_checks.csv", checks, CHECK_FIELDS)
    write_csv(DEBUG / "ledger_before.csv", rows_before, list(rows_before[0]) if rows_before else [])
    write_csv(DEBUG / "ledger_after_preview.csv", rows_after, list(rows_after[0]) if rows_after else [])
    write_csv(DEBUG / "v488_check_snapshot.csv", v488_checks, list(v488_checks[0]) if v488_checks else [])
    write_csv(DEBUG / "entry_operator_checklist.csv", build_operator_checklist(summary), OPERATOR_FIELDS)
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, checks), encoding="utf-8")


def final_verdict(status: str) -> str:
    if status == "not_due":
        return "尚未到 V4.85 前推批次计划入场日；当前不能确认强反弹行业样本已经进入真实前推。"
    if status == "entered_research_observation":
        return "V4.85 前推批次入场日门控通过，可作为 research_only 前推样本等待退出日结算。"
    if status == "skipped_entry_gate_failed":
        return "V4.85 前推批次入场日门控失败，应跳过该批次，不计入强反弹行业评价。"
    return "V4.85 前推批次入场门控被阻断，需要先完成同日刷新或人工检查。"


def build_operator_checklist(summary: dict[str, Any]) -> list[dict[str, str]]:
    status = str(summary.get("entry_gate_status", ""))
    apply_allowed = bool(summary.get("apply_allowed", False))
    return [
        operator_row(
            "P0",
            "same_day_live_refresh",
            "required_on_entry_day" if status != "not_due" else "not_due",
            str(summary.get("entry_refresh_command", "")),
            "Run first on the planned entry date before any ledger apply.",
            "入场日必须先刷新全链路；不是入场日时不执行。",
        ),
        operator_row(
            "P0",
            "review_entry_gate",
            "required_on_entry_day" if status != "not_due" else "not_due",
            str(summary.get("entry_gate_command", "")),
            "Run after live refresh and inspect entry_gate_status.",
            "只读检查，不写账本。",
        ),
        operator_row(
            "P0",
            "apply_entered_or_skipped",
            "allowed" if apply_allowed else "blocked",
            str(summary.get("entry_apply_command", "")),
            "Only run when apply_allowed=true after same-day refresh.",
            "写账本动作；entered 或 skipped 都必须保持 research_only。",
        ),
        operator_row(
            "P0",
            "auto_execution_boundary",
            "blocked",
            "",
            "auto_execution_allowed must remain false.",
            "该系统不自动下单，不生成买卖指令。",
        ),
    ]


def operator_row(priority: str, step: str, status: str, command: str, condition: str, note: str) -> dict[str, str]:
    return {
        "priority": priority,
        "step": step,
        "status": status,
        "command": command,
        "condition": condition,
        "operator_note": note,
    }


def render_report(summary: dict[str, Any], checks: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.90 强反弹行业前推入场门控",
        "",
        str(summary["final_verdict"]),
        "",
        "## 当前状态",
        "",
        f"- 截止日期：{summary['as_of_date']}",
        f"- 计划入场/退出：{summary['planned_entry_date']} / {summary['planned_exit_date']}",
        f"- V4.88 审计日期：{summary['v488_audit_date']}",
        f"- 入场门控状态：`{summary['entry_gate_status']}`",
        f"- 是否允许写入账本：`{str(summary['apply_allowed']).lower()}`",
        f"- 本次是否请求写入：`{str(summary['apply_requested']).lower()}`",
        f"- 账本是否会变化：`{str(summary['ledger_would_change']).lower()}`",
        f"- 推荐动作：{summary['recommended_action']}",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 检查项",
        "",
        markdown_table(checks, CHECK_FIELDS[:-1]),
        "",
        "## 边界",
        "",
        "V4.90 只决定该批次能否进入 research_only 前推样本，不计算收益、不改变候选规则，也不生成交易指令。",
    ])


def check(dimension: str, name: str, current: str, required: str, status: str, interpretation: str) -> dict[str, str]:
    return {
        "dimension": dimension,
        "check": name,
        "current": current,
        "required": required,
        "status": status,
        "interpretation": interpretation,
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    if not fields:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    if not fields:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def int_value(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def markdown_table(rows: list[dict[str, str]], cols: list[str]) -> str:
    if not rows:
        return "无数据"
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")).replace("|", "\\|") for col in cols) + " |")
    return "\n".join(lines)


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rows = [{
            "decision": "planned_observation",
            "outcome_status": "pending_forward_observation",
            "actual_entry_date": "",
            "settlement_status": "not_due",
            "settlement_notes": "",
        }]
        v488 = {"planned_entry_date": "2026-06-23", "planned_exit_date": "2026-07-21", "as_of_date": "2026-06-23", "pre_entry_status": "pre_entry_consistent", "pass_count": 11, "fail_count": 0}
        decision = build_decision(date(2026, 6, 23), rows, v488, [])
        assert decision["entry_gate_status"] == "entered_research_observation"
        assert decision["entry_apply_command"].endswith("--apply")
        checklist = build_operator_checklist({**decision, "apply_requested": False, "ledger_would_change": False})
        assert any(row["step"] == "apply_entered_or_skipped" and row["status"] == "allowed" for row in checklist)
        applied = apply_decision(rows, decision)
        assert applied[0]["decision"] == "entered_research_observation"
        assert applied[0]["outcome_status"] == "pending_forward_observation"
        early = build_decision(date(2026, 6, 20), rows, v488, [])
        assert early["entry_gate_status"] == "not_due"
        stale = build_decision(date(2026, 6, 23), rows, {**v488, "as_of_date": "2026-06-20"}, [])
        assert stale["entry_gate_status"] == "blocked_same_day_refresh_required"
        failed = build_decision(date(2026, 6, 23), rows, {**v488, "pre_entry_status": "pre_entry_blocked", "fail_count": 1}, [])
        assert failed["entry_gate_status"] == "skipped_entry_gate_failed"
        skipped = apply_decision(rows, failed)
        assert skipped[0]["outcome_status"] == "skipped_forward_observation"
        assert Path(tmp).exists()
    print("self_check=pass")


if __name__ == "__main__":
    main()
