#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "run_summary.json"
CANDIDATES = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "latest_rebound_leader_candidates.csv"
FUND_FLOW = ROOT / "outputs" / "audit" / "v4_72_candidate_fund_flow_overlay" / "debug" / "candidate_fund_flow_overlay.csv"
V471_TRACKER = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "forward_sample_tracker.json"
LEDGER = ROOT / "logs" / "v4_72_rebound_leader_forward_ledger.csv"
OUT = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_forward_archive"
DEBUG = OUT / "debug"

FIELDS = [
    "recorded_at",
    "tracker_id",
    "policy_version",
    "policy_status",
    "decision",
    "outcome_status",
    "signal_date",
    "feature_date",
    "planned_entry_date",
    "planned_exit_date",
    "industry_code",
    "industry_name",
    "selection_strategy",
    "selection_score",
    "valuation_score",
    "oversold_score",
    "turn_score",
    "liquidity_score",
    "historical_failure_flag",
    "fund_flow_overlay_status",
    "ths_industry_name",
    "mapping_review_status",
    "mapping_confidence",
    "ths_today_net_flow",
    "ths_5d_net_flow",
    "production_allowed",
    "actual_entry_date",
    "actual_exit_date",
    "realized_return",
    "benchmark_return",
    "realized_relative_return",
    "settlement_status",
    "settlement_notes",
    "notes",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive V4.72 rebound-leader candidates for forward validation.")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.audit:
        write_outputs(audit_ledger(read_rows(LEDGER)), [])
        return

    rows = build_rows()
    write_ledger(LEDGER, rows)
    write_outputs(audit_ledger(read_rows(LEDGER)), rows)
    print(f"ledger={LEDGER}")
    print(f"archived_rows={len(rows)}")
    print("production_ready=False")


def build_rows() -> list[dict[str, str]]:
    summary = read_json(SUMMARY)
    candidates = read_rows(CANDIDATES)
    overlays = {row["industry_code"].zfill(6): row for row in read_rows(FUND_FLOW)}
    v471 = read_json(V471_TRACKER) if V471_TRACKER.exists() else {}
    tracker_id = f"v4_72_forward_{first(candidates, 'signal_date') or first(candidates, 'feature_date')}"
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for candidate in candidates:
        code = candidate["industry_code"].zfill(6)
        overlay = overlays.get(code, {})
        rows.append({
            "recorded_at": now,
            "tracker_id": tracker_id,
            "policy_version": str(summary.get("version", "")),
            "policy_status": str(summary.get("best_status") or summary.get("policy_status", "")),
            "decision": "planned",
            "outcome_status": "pending_forward_observation",
            "signal_date": candidate.get("signal_date", ""),
            "feature_date": candidate.get("feature_date", ""),
            "planned_entry_date": candidate.get("planned_entry_date", ""),
            "planned_exit_date": str(v471.get("planned_exit_date", "")),
            "industry_code": code,
            "industry_name": candidate.get("industry_name", ""),
            "selection_strategy": candidate.get("selection_strategy", ""),
            "selection_score": candidate.get("selection_score", ""),
            "valuation_score": candidate.get("valuation_score", ""),
            "oversold_score": candidate.get("oversold_score", ""),
            "turn_score": candidate.get("turn_score", ""),
            "liquidity_score": candidate.get("liquidity_score", ""),
            "historical_failure_flag": candidate.get("historical_failure_flag", ""),
            "fund_flow_overlay_status": overlay.get("fund_flow_overlay_status", "missing_mapping_or_flow"),
            "ths_industry_name": overlay.get("ths_industry_name", ""),
            "mapping_review_status": overlay.get("review_status", ""),
            "mapping_confidence": overlay.get("mapping_confidence", ""),
            "ths_today_net_flow": overlay.get("ths_today_net_flow", ""),
            "ths_5d_net_flow": overlay.get("ths_5d_net_flow", ""),
            "production_allowed": overlay.get("production_allowed", "否"),
            "actual_entry_date": "",
            "actual_exit_date": "",
            "realized_return": "",
            "benchmark_return": "",
            "realized_relative_return": "",
            "settlement_status": "not_due",
            "settlement_notes": "",
            "notes": "前推观察归档；不得作为已验证交易信号。",
        })
    return rows


def write_ledger(path: Path, new_rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = read_rows(path)
    keys = {(row["tracker_id"], row["industry_code"], row["decision"]) for row in new_rows}
    kept = [row for row in old if (row.get("tracker_id", ""), row.get("industry_code", ""), row.get("decision", "")) not in keys]
    write_rows(path, kept + new_rows)


def audit_ledger(rows: list[dict[str, str]]) -> dict[str, str | int]:
    keys = [(row.get("tracker_id", ""), row.get("industry_code", ""), row.get("decision", "")) for row in rows]
    duplicate_keys = sum(count - 1 for count in Counter(keys).values() if count > 1)
    pending = [row for row in rows if row.get("outcome_status") == "pending_forward_observation"]
    return {
        "status": "pass" if rows and duplicate_keys == 0 else "review",
        "ledger_rows": len(rows),
        "unique_trackers": len({row.get("tracker_id", "") for row in rows}),
        "pending_forward_observations": len(pending),
        "missing_fund_flow_overlay": sum(row.get("fund_flow_overlay_status") != "available_current_only" for row in pending),
        "historical_failure_flagged": sum(row.get("historical_failure_flag") == "True" for row in pending),
        "production_allowed_rows": sum(row.get("production_allowed") == "是" for row in pending),
        "duplicate_keys": duplicate_keys,
        "ledger_path": str(LEDGER.relative_to(ROOT)),
        "production_ready": "false",
    }


def write_outputs(summary: dict[str, str | int], archived_rows: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", archived_rows or read_rows(LEDGER)[-10:])
    write_rows(DEBUG / "archived_forward_rows.csv", archived_rows)
    write_rows(DEBUG / "forward_ledger_audit.csv", [summary])
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary), encoding="utf-8")


def render_report(summary: dict[str, str | int]) -> str:
    return "\n".join([
        "# V4.72 强行业候选前推归档",
        "",
        "把每日 V4.72 候选行业写入前推账本；当前只记录计划观察，不提前填写未来收益。",
        "",
        f"- 账本行数：{summary['ledger_rows']}",
        f"- 追踪批次：{summary['unique_trackers']}",
        f"- 待前推观察：{summary['pending_forward_observations']}",
        f"- 缺资金流观察：{summary['missing_fund_flow_overlay']}",
        f"- 历史失败标记：{summary['historical_failure_flagged']}",
        f"- 生产允许行：{summary['production_allowed_rows']}",
        f"- 重复键：{summary['duplicate_keys']}",
        f"- 账本：`{summary['ledger_path']}`",
        f"- 生产可用：`{summary['production_ready']}`",
        "",
        "边界：这是新增样本前推证据链，不是回测收益，也不是交易指令。",
    ])


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS if path.suffix == ".csv" and path.name != "forward_ledger_audit.csv" else list(rows[0]) if rows else FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def first(rows: list[dict[str, str]], key: str) -> str:
    return rows[0].get(key, "") if rows else ""


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.csv"
        sample = {field: "" for field in FIELDS}
        sample.update({"tracker_id": "t1", "industry_code": "801001", "decision": "planned", "outcome_status": "pending_forward_observation", "fund_flow_overlay_status": "missing_mapping_or_flow", "historical_failure_flag": "True", "production_allowed": "否"})
        write_ledger(path, [sample])
        write_ledger(path, [{**sample, "notes": "replace"}])
        rows = read_rows(path)
        assert len(rows) == 1
        assert rows[0]["notes"] == "replace"
        summary = audit_ledger(rows)
        assert summary["status"] == "pass"
        assert summary["missing_fund_flow_overlay"] == 1
        assert summary["historical_failure_flagged"] == 1
    print("self_check=pass")


if __name__ == "__main__":
    main()
