#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "pre_trade_manual_review_sheet.csv"
FUND_FLOW = ROOT / "outputs" / "audit" / "v4_72_candidate_fund_flow_overlay" / "debug" / "candidate_fund_flow_overlay.csv"
CARRIER_MAPPING = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "industry_candidate_carrier_mapping.csv"
CARRIER_TRACKING = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "carrier_tracking_audit.csv"
ALTERNATIVE_TRACKING = ROOT / "outputs" / "audit" / "v4_72_carrier_alternative_tracking" / "debug" / "carrier_alternative_tracking.csv"
FAILURE_DIAGNOSIS = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "failure_diagnosis.csv"
OUT = ROOT / "outputs" / "audit" / "v4_72_pre_trade_review_packet"
DEBUG = OUT / "debug"

FIELDS = [
    "review_priority",
    "practical_review_bucket",
    "industry_code",
    "industry_name",
    "selection_score",
    "planned_entry_date",
    "research_gate_status",
    "historical_failure_flag",
    "candidate_carrier_code",
    "candidate_carrier_name",
    "carrier_mapping_status",
    "carrier_mapping_confidence",
    "carrier_mapping_evidence",
    "tracking_audit_status",
    "carrier_fallback_status",
    "carrier_turnover_amount",
    "carrier_daily_return_corr",
    "carrier_return_gap",
    "carrier_tracking_evidence_note",
    "best_alternative_carrier_code",
    "best_alternative_carrier_name",
    "best_alternative_tracking_status",
    "best_alternative_action_note",
    "fund_flow_overlay_status",
    "ths_industry_name",
    "ths_today_net_flow",
    "ths_5d_net_flow",
    "ths_leading_stock",
    "repeated_worst_event_count",
    "repeated_worst_event_status",
    "tradeable_filter_status",
    "structural_blockers",
    "auto_execution_allowed",
    "system_position_cap_pct",
    "manual_override_required",
    "manual_override_reason",
    "manual_gate_status",
    "manual_gate_action",
    "manual_action",
    "blocking_notes",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a V4.72 pre-trade manual review packet.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    rows = build_packet(read_rows(MANUAL), read_rows(FUND_FLOW), read_rows(CARRIER_MAPPING), read_rows(CARRIER_TRACKING), read_rows(ALTERNATIVE_TRACKING), read_rows(FAILURE_DIAGNOSIS))
    write_outputs(rows)
    print(f"output_dir={OUT}")
    print(f"review_rows={len(rows)}")
    print("production_ready=False")


def build_packet(manual_rows: list[dict[str, str]], flow_rows: list[dict[str, str]], carrier_rows: list[dict[str, str]], tracking_rows: list[dict[str, str]] | None = None, alternative_rows: list[dict[str, str]] | None = None, failure_rows: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    flow_by_code = {row.get("industry_code", "").zfill(6): row for row in flow_rows}
    fallback_by_code = low_turnover_fallbacks(carrier_rows)
    mapping_by_key = carrier_mapping_lookup(carrier_rows)
    alternative_by_code = best_alternative_lookup(alternative_rows or [])
    repeated_failures = repeated_worst_event_lookup(failure_rows or [])
    tracking_by_key = {
        (row.get("industry_code", "").zfill(6), row.get("candidate_carrier_code", "")): row
        for row in tracking_rows or []
    }
    rows = []
    for row in manual_rows:
        code = row.get("industry_code", "").zfill(6)
        flow = flow_by_code.get(code, {})
        fallback = fallback_by_code.get(code, {}) if not row.get("candidate_carrier_code") else {}
        carrier_code = row.get("candidate_carrier_code", "") or fallback.get("candidate_carrier_code", "")
        tracking = tracking_by_key.get((code, carrier_code), {})
        mapping = mapping_by_key.get((code, carrier_code), mapping_by_key.get((code, ""), {}))
        alternative = alternative_by_code.get(code, {})
        merged = {
            "industry_code": code,
            "industry_name": row.get("industry_name", ""),
            "selection_score": row.get("selection_score", ""),
            "planned_entry_date": row.get("planned_entry_date", ""),
            "research_gate_status": row.get("research_gate_status", ""),
            "historical_failure_flag": row.get("historical_failure_flag", ""),
            "candidate_carrier_code": carrier_code,
            "candidate_carrier_name": row.get("candidate_carrier_name", "") or fallback.get("candidate_carrier_name", ""),
            "carrier_mapping_status": mapping.get("carrier_mapping_status", ""),
            "carrier_mapping_confidence": mapping.get("mapping_confidence", ""),
            "carrier_mapping_evidence": mapping.get("mapping_evidence", ""),
            "tracking_audit_status": row.get("tracking_audit_status", "") if not fallback else "low_turnover_fallback_not_tracking_audited",
            "carrier_fallback_status": "low_turnover_fallback" if fallback else "",
            "carrier_turnover_amount": fallback.get("turnover_amount", ""),
            "carrier_daily_return_corr": tracking.get("daily_return_corr", ""),
            "carrier_return_gap": tracking.get("return_gap", ""),
            "carrier_tracking_evidence_note": tracking_note(tracking),
            "best_alternative_carrier_code": alternative.get("candidate_carrier_code", ""),
            "best_alternative_carrier_name": alternative.get("candidate_carrier_name", ""),
            "best_alternative_tracking_status": alternative.get("alternative_tracking_status", ""),
            "best_alternative_action_note": alternative.get("action_note", ""),
            "fund_flow_overlay_status": flow.get("fund_flow_overlay_status", "missing_mapping_or_flow"),
            "ths_industry_name": flow.get("ths_industry_name", ""),
            "ths_today_net_flow": flow.get("ths_today_net_flow", ""),
            "ths_5d_net_flow": flow.get("ths_5d_net_flow", ""),
            "ths_leading_stock": flow.get("ths_leading_stock", ""),
            "repeated_worst_event_count": repeated_failures.get(row.get("industry_name", ""), ""),
            "repeated_worst_event_status": "repeated_worst_event_industry" if repeated_failures.get(row.get("industry_name", ""), 0) >= 3 else "",
            "auto_execution_allowed": "否",
            "manual_action": "只读观察/人工复核",
        }
        merged["blocking_notes"] = blocking_notes(merged)
        merged["review_priority"] = priority(merged)
        merged["manual_gate_status"], merged["manual_gate_action"] = manual_gate(merged)
        merged["practical_review_bucket"] = practical_review_bucket(merged)
        merged["tradeable_filter_status"] = tradeable_filter_status(merged)
        merged["structural_blockers"] = structural_blockers(merged)
        # ponytail: all caps stay zero until forward evidence proves production readiness.
        merged["system_position_cap_pct"] = "0"
        merged["manual_override_required"] = "是"
        merged["manual_override_reason"] = "production_ready=false；强行业选择未验证，只能做人工研究观察。"
        rows.append(merged)
    return sorted(rows, key=lambda x: (priority_rank(x["review_priority"]), -safe_float(x["selection_score"])))


def tradeable_filter_status(row: dict[str, str]) -> str:
    gate = row.get("manual_gate_status", "")
    if gate == "research_observation_only":
        return "structural_reviewable_research_gate_blocked"
    if gate.startswith("observe_only"):
        return "structural_observe_only"
    return "structural_blocked"


def structural_blockers(row: dict[str, str]) -> str:
    gate = row.get("manual_gate_status", "")
    blockers = []
    if not row.get("candidate_carrier_code"):
        blockers.append("no_tradeable_carrier")
    if row.get("fund_flow_overlay_status") == "proxy_current_only":
        blockers.append("proxy_fund_flow_only")
    elif row.get("fund_flow_overlay_status") != "available_current_only":
        blockers.append("missing_fund_flow")
    if row.get("carrier_fallback_status") == "low_turnover_fallback":
        blockers.append("low_turnover_carrier")
    if row.get("tracking_audit_status") == "tracking_weak_review_required":
        blockers.append("weak_carrier_tracking")
    elif row.get("tracking_audit_status") != "tracking_observed_review_required":
        blockers.append("tracking_not_ready")
    if row.get("historical_failure_flag") == "True":
        blockers.append("historical_failure_flag")
    if row.get("repeated_worst_event_status"):
        blockers.append("repeated_worst_event_industry")
    if gate == "research_observation_only":
        blockers.append("strong_industry_selection_not_validated")
    return "|".join(blockers) or "none"


def low_turnover_fallbacks(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    candidates = [
        row for row in rows
        if row.get("candidate_carrier_code")
        and row.get("liquidity_status") == "low_turnover"
        and row.get("discount_status") == "pass"
    ]
    candidates.sort(key=lambda row: safe_float(row.get("turnover_amount", "")), reverse=True)
    out: dict[str, dict[str, str]] = {}
    for row in candidates:
        out.setdefault(row.get("industry_code", "").zfill(6), row)
    return out


def best_alternative_lookup(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if str(row.get("alternative_rank", "")) == "1":
            out[row.get("industry_code", "").zfill(6)] = row
    return out


def carrier_mapping_lookup(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    out: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        code = row.get("industry_code", "").zfill(6)
        carrier = row.get("candidate_carrier_code", "")
        out.setdefault((code, carrier), row)
        if not carrier:
            out.setdefault((code, ""), row)
    return out


def blocking_notes(row: dict[str, str]) -> str:
    notes = []
    if row["research_gate_status"] != "validated":
        notes.append("强行业选择未验证")
    if row["historical_failure_flag"] == "True":
        notes.append("历史失败标记")
    if row.get("repeated_worst_event_status"):
        notes.append(f"最差事件重复出现={row.get('repeated_worst_event_count')}")
    if row["fund_flow_overlay_status"] == "proxy_current_only":
        notes.append("仅代理资金流观察")
    elif row["fund_flow_overlay_status"] != "available_current_only":
        notes.append("缺资金流观察")
    if row["carrier_fallback_status"] == "low_turnover_fallback":
        notes.append("仅有低流动性备选载体")
    if row["carrier_mapping_status"] == "no_keyword_match":
        notes.append("无可复核载体映射")
    if row["tracking_audit_status"] == "tracking_weak_review_required":
        notes.append("载体跟踪偏弱")
    elif row["tracking_audit_status"] not in {"tracking_observed_review_required"}:
        notes.append("载体跟踪未充分观察")
    return "；".join(notes) or "仍需人工确认仓位、价格漂移和流动性"


def priority(row: dict[str, str]) -> str:
    if (
        row["fund_flow_overlay_status"] != "available_current_only"
        or not row["candidate_carrier_code"]
        or row["carrier_fallback_status"] == "low_turnover_fallback"
        or row["tracking_audit_status"] != "tracking_observed_review_required"
    ):
        return "P1_补数据后再看"
    if row["historical_failure_flag"] == "True":
        return "P2_高风险观察"
    return "P3_常规观察"


def priority_rank(value: str) -> int:
    return {"P1_补数据后再看": 0, "P2_高风险观察": 1, "P3_常规观察": 2}.get(value, 9)


def manual_gate(row: dict[str, str]) -> tuple[str, str]:
    if not row["candidate_carrier_code"]:
        evidence = row.get("carrier_mapping_evidence", "")
        suffix = f"；当前映射证据={evidence}" if evidence else ""
        return "blocked_no_tradeable_carrier", f"跳过；先补可复核载体{suffix}。"
    if row["fund_flow_overlay_status"] == "proxy_current_only":
        return "blocked_proxy_fund_flow_only", "跳过；代理资金流只可观察，需补精确行业映射。"
    if row["fund_flow_overlay_status"] != "available_current_only":
        return "blocked_missing_fund_flow", "跳过；先补资金流映射或缓存。"
    if row["carrier_fallback_status"] == "low_turnover_fallback":
        return "observe_only_low_liquidity", "只观察；低流动性载体不得按常规仓位处理。"
    if row["tracking_audit_status"] == "tracking_weak_review_required":
        return "blocked_tracking_weak", f"跳过；载体跟踪偏弱，{row.get('carrier_tracking_evidence_note', '需补跟踪证据')}。"
    if row["tracking_audit_status"] != "tracking_observed_review_required":
        return "blocked_tracking_not_ready", "跳过或只观察；载体跟踪未充分审计。"
    if row["historical_failure_flag"] == "True":
        return "observe_only_historical_failure", "只观察；历史失败标记未解除。"
    if row.get("repeated_worst_event_status"):
        return "observe_only_repeated_worst_event", "只观察；该行业重复出现在历史最差反弹选择事件中。"
    return "research_observation_only", "只读观察；强行业选择未验证，不能自动执行。"


def practical_review_bucket(row: dict[str, str]) -> str:
    gate = row.get("manual_gate_status", "")
    if gate == "research_observation_only":
        return "人工优先复核_研究门禁未过"
    if gate.startswith("blocked"):
        return "补证失败则跳过"
    return "只观察"


def write_outputs(rows: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "pre_trade_review_packet.csv", rows)
    summary = {
        "version": "v4_72_pre_trade_review_packet_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "review_rows": len(rows),
        "p1_count": sum(row["review_priority"] == "P1_补数据后再看" for row in rows),
        "p2_count": sum(row["review_priority"] == "P2_高风险观察" for row in rows),
        "p3_count": sum(row["review_priority"] == "P3_常规观察" for row in rows),
        "core_manual_review_count": sum(row["practical_review_bucket"] == "人工优先复核_研究门禁未过" for row in rows),
        "core_manual_review_industries": bucket_names(rows, "人工优先复核_研究门禁未过"),
        "core_manual_review_carriers": bucket_carriers(rows, "人工优先复核_研究门禁未过"),
        "core_manual_review_tracking": bucket_tracking(rows, "人工优先复核_研究门禁未过"),
        "core_manual_review_fund_flow": bucket_fund_flow(rows, "人工优先复核_研究门禁未过"),
        "core_today_flow_positive_count": bucket_flow_positive_count(rows, "人工优先复核_研究门禁未过", "ths_today_net_flow"),
        "core_5d_flow_positive_count": bucket_flow_positive_count(rows, "人工优先复核_研究门禁未过", "ths_5d_net_flow"),
        "core_dual_flow_positive_count": bucket_dual_flow_positive_count(rows, "人工优先复核_研究门禁未过"),
        "core_flow_confirmation_status": core_flow_confirmation_status(rows),
        "skip_if_unresolved_count": sum(row["practical_review_bucket"] == "补证失败则跳过" for row in rows),
        "skip_if_unresolved_industries": bucket_names(rows, "补证失败则跳过"),
        "observe_only_bucket_count": sum(row["practical_review_bucket"] == "只观察" for row in rows),
        "observe_only_industries": bucket_names(rows, "只观察"),
        "blocked_count": sum(str(row["manual_gate_status"]).startswith("blocked_") for row in rows),
        "observe_only_count": sum("observe_only" in row["manual_gate_status"] or row["manual_gate_status"] == "research_observation_only" for row in rows),
        "structural_reviewable_count": sum(row["tradeable_filter_status"] == "structural_reviewable_research_gate_blocked" for row in rows),
        "structural_blocked_count": sum(row["tradeable_filter_status"] == "structural_blocked" for row in rows),
        "structural_observe_only_count": sum(row["tradeable_filter_status"] == "structural_observe_only" for row in rows),
        "repeated_worst_event_candidate_count": sum(row["repeated_worst_event_status"] == "repeated_worst_event_industry" for row in rows),
        "system_position_cap_all_zero": all(row["system_position_cap_pct"] == "0" for row in rows),
        "auto_execution_allowed": False,
        "production_ready": False,
        "final_verdict": "盘前复核包只合并候选、载体和资金流观察；不得作为自动交易指令。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def render_report(summary: dict[str, object], rows: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.72 盘前人工复核包",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 复核行数：{summary['review_rows']}",
        f"- P1 补数据后再看：{summary['p1_count']}",
        f"- P2 高风险观察：{summary['p2_count']}",
        f"- P3 常规观察：{summary['p3_count']}",
        f"- 人工优先复核池：{summary['core_manual_review_count']}",
        f"- 人工优先复核名单：{summary['core_manual_review_industries']}",
        f"- 人工优先复核载体：{summary['core_manual_review_carriers']}",
        f"- 人工优先复核跟踪：{summary['core_manual_review_tracking']}",
        f"- 人工优先复核资金流：{summary['core_manual_review_fund_flow']}",
        f"- 核心池资金流确认：今日正向 {summary['core_today_flow_positive_count']}；5日正向 {summary['core_5d_flow_positive_count']}；双正向 {summary['core_dual_flow_positive_count']}",
        f"- 核心池资金流状态：{summary['core_flow_confirmation_status']}",
        f"- 补证失败则跳过：{summary['skip_if_unresolved_count']}",
        f"- 只观察池：{summary['observe_only_bucket_count']}",
        f"- 人工门禁阻断：{summary['blocked_count']}",
        f"- 只读观察：{summary['observe_only_count']}",
        f"- 结构可复核但研究门禁未过：{summary['structural_reviewable_count']}",
        f"- 结构阻断：{summary['structural_blocked_count']}",
        f"- 命中历史最差事件重复行业：{summary['repeated_worst_event_candidate_count']}",
        f"- 系统仓位上限全为 0：`{str(summary['system_position_cap_all_zero']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        f"- 生产可用：`{str(summary['production_ready']).lower()}`",
        "",
        to_markdown(rows[:10]) if rows else "无数据。",
        "",
        "边界：该表用于盘前人工复核，不改变 V4.72 排名，不生成买入/卖出指令。",
    ])


def to_markdown(rows: list[dict[str, str]]) -> str:
    cols = ["review_priority", "practical_review_bucket", "tradeable_filter_status", "manual_gate_status", "system_position_cap_pct", "industry_name", "candidate_carrier_name", "best_alternative_carrier_name", "fund_flow_overlay_status", "repeated_worst_event_count", "structural_blockers"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(":---" for _ in cols) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")).replace("\n", " ") for col in cols) + " |")
    return "\n".join(lines)


def bucket_names(rows: list[dict[str, str]], bucket: str) -> str:
    return ",".join(row.get("industry_name", "") for row in rows if row.get("practical_review_bucket") == bucket)


def bucket_carriers(rows: list[dict[str, str]], bucket: str) -> str:
    parts = []
    for row in rows:
        if row.get("practical_review_bucket") != bucket:
            continue
        carrier = row.get("best_alternative_carrier_name") or row.get("candidate_carrier_name", "")
        parts.append(f"{row.get('industry_name', '')}={carrier}")
    return ",".join(parts)


def bucket_tracking(rows: list[dict[str, str]], bucket: str) -> str:
    parts = []
    for row in rows:
        if row.get("practical_review_bucket") != bucket:
            continue
        parts.append(f"{row.get('industry_name', '')}={row.get('carrier_tracking_evidence_note', '')}")
    return ",".join(parts)


def bucket_fund_flow(rows: list[dict[str, str]], bucket: str) -> str:
    parts = []
    for row in rows:
        if row.get("practical_review_bucket") != bucket:
            continue
        parts.append(f"{row.get('industry_name', '')}=今日{row.get('ths_today_net_flow', '')}/5日{row.get('ths_5d_net_flow', '')}/龙头{row.get('ths_leading_stock', '')}")
    return ",".join(parts)


def bucket_flow_positive_count(rows: list[dict[str, str]], bucket: str, field: str) -> int:
    return sum(1 for row in rows if row.get("practical_review_bucket") == bucket and safe_float(row.get(field, "")) > 0)


def bucket_dual_flow_positive_count(rows: list[dict[str, str]], bucket: str) -> int:
    return sum(
        1
        for row in rows
        if row.get("practical_review_bucket") == bucket
        and safe_float(row.get("ths_today_net_flow", "")) > 0
        and safe_float(row.get("ths_5d_net_flow", "")) > 0
    )


def core_flow_confirmation_status(rows: list[dict[str, str]]) -> str:
    bucket = "人工优先复核_研究门禁未过"
    if bucket_dual_flow_positive_count(rows, bucket) > 0:
        return "confirmed_dual_positive_flow"
    if bucket_flow_positive_count(rows, bucket, "ths_today_net_flow") > 0:
        return "weak_today_only_positive_flow"
    return "weak_no_positive_flow"


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def safe_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def tracking_note(row: dict[str, str]) -> str:
    if not row:
        return ""
    corr = row.get("daily_return_corr", "")
    gap = row.get("return_gap", "")
    days = row.get("overlap_days", "")
    if corr or gap:
        return f"{days}日重叠；日收益相关={safe_float(corr):.2f}；累计收益差={safe_float(gap):.2%}"
    return ""


def repeated_worst_event_lookup(rows: list[dict[str, str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        if row.get("category") != "repeated_worst_event_industry":
            continue
        try:
            out[row.get("item", "")] = int(float(row.get("value", "0")))
        except ValueError:
            out[row.get("item", "")] = 0
    return out


def self_check() -> None:
    with tempfile.TemporaryDirectory():
        manual = [{"industry_code": "801001", "industry_name": "样本", "research_gate_status": "research_only", "historical_failure_flag": "True", "candidate_carrier_code": "159001", "tracking_audit_status": "tracking_weak_review_required", "selection_score": "0.8"}]
        flow = [{"industry_code": "801001", "fund_flow_overlay_status": "available_current_only"}]
        carriers = [{"industry_code": "801001", "candidate_carrier_code": "159001", "carrier_mapping_status": "keyword_match_review_required", "mapping_confidence": "low", "mapping_evidence": "样本"}]
        alternatives = [{"industry_code": "801001", "alternative_rank": "1", "candidate_carrier_code": "159002", "candidate_carrier_name": "替代ETF", "alternative_tracking_status": "tracking_observed_review_required", "action_note": "可进入人工复核候选"}]
        rows = build_packet(manual, flow, carriers, [{"industry_code": "801001", "candidate_carrier_code": "159001", "daily_return_corr": "0.5", "return_gap": "0.9", "overlap_days": "253"}], alternatives, [{"category": "repeated_worst_event_industry", "item": "样本", "value": "3"}])
        assert rows[0]["review_priority"] == "P1_补数据后再看"
        assert rows[0]["manual_gate_status"] == "blocked_tracking_weak"
        assert rows[0]["practical_review_bucket"] == "补证失败则跳过"
        assert rows[0]["tradeable_filter_status"] == "structural_blocked"
        assert "weak_carrier_tracking" in rows[0]["structural_blockers"]
        assert "repeated_worst_event_industry" in rows[0]["structural_blockers"]
        assert rows[0]["repeated_worst_event_count"] == 3
        assert "日收益相关=0.50" in rows[0]["carrier_tracking_evidence_note"]
        assert rows[0]["carrier_mapping_status"] == "keyword_match_review_required"
        assert rows[0]["best_alternative_carrier_code"] == "159002"
        assert rows[0]["system_position_cap_pct"] == "0"
        assert rows[0]["manual_override_required"] == "是"
        assert "强行业选择未验证" in rows[0]["blocking_notes"]
        rows = build_packet(manual, [], [])
        assert rows[0]["review_priority"] == "P1_补数据后再看"
        assert rows[0]["manual_gate_status"] == "blocked_missing_fund_flow"
        no_carrier = [{"industry_code": "801003", "candidate_carrier_code": "", "carrier_mapping_status": "no_keyword_match", "mapping_confidence": "none", "mapping_evidence": "零售"}]
        rows = build_packet([{**manual[0], "industry_code": "801003", "candidate_carrier_code": "", "historical_failure_flag": "False"}], [{"industry_code": "801003", "fund_flow_overlay_status": "available_current_only"}], no_carrier)
        assert rows[0]["manual_gate_status"] == "blocked_no_tradeable_carrier"
        assert "no_tradeable_carrier" in rows[0]["structural_blockers"]
        assert rows[0]["carrier_mapping_status"] == "no_keyword_match"
        assert "零售" in rows[0]["manual_gate_action"]
        fallback = [{"industry_code": "801002", "candidate_carrier_code": "513360", "candidate_carrier_name": "教育ETF", "liquidity_status": "low_turnover", "discount_status": "pass", "turnover_amount": "100"}]
        rows = build_packet([{**manual[0], "industry_code": "801002", "candidate_carrier_code": "", "historical_failure_flag": "False"}], [{"industry_code": "801002", "fund_flow_overlay_status": "available_current_only"}], fallback)
        assert rows[0]["candidate_carrier_code"] == "513360"
        assert rows[0]["carrier_fallback_status"] == "low_turnover_fallback"
        assert rows[0]["review_priority"] == "P1_补数据后再看"
        assert rows[0]["manual_gate_status"] == "observe_only_low_liquidity"
        rows = build_packet([{**manual[0], "research_gate_status": "validated", "tracking_audit_status": "tracking_observed_review_required", "historical_failure_flag": "False"}], flow, carriers, [], [], [{"category": "repeated_worst_event_industry", "item": "样本", "value": "3"}])
        assert rows[0]["manual_gate_status"] == "observe_only_repeated_worst_event"
        rows = build_packet([{**manual[0], "tracking_audit_status": "tracking_observed_review_required", "historical_failure_flag": "False"}], flow, carriers)
        assert rows[0]["practical_review_bucket"] == "人工优先复核_研究门禁未过"
        assert bucket_names(rows, "人工优先复核_研究门禁未过") == "样本"
        assert bucket_carriers(rows, "人工优先复核_研究门禁未过") == "样本="
        assert bucket_tracking(rows, "人工优先复核_研究门禁未过") == "样本="
        assert bucket_fund_flow(rows, "人工优先复核_研究门禁未过") == "样本=今日/5日/龙头"
        assert bucket_flow_positive_count(rows, "人工优先复核_研究门禁未过", "ths_today_net_flow") == 0
        assert core_flow_confirmation_status(rows) == "weak_no_positive_flow"
    print("self_check=pass")


if __name__ == "__main__":
    main()
