#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from fund_flow_forward_evidence import materialize_observations, read_events, verify_ledger_checkpoint
from settle_v5_27_fund_flow_forward_samples import integrity_inputs_current

ROOT = Path(__file__).resolve().parents[1]
SETTLEMENT = ROOT / "outputs" / "audit" / "fund_flow_forward_settlement_v5_27" / "top_candidates.csv"
EVENT_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
INTEGRITY = ROOT / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30" / "run_summary.json"
FREEZE_MANIFEST = ROOT / "outputs" / "audit" / "fund_flow_evidence_freeze_manifest_v5_31" / "run_summary.json"
OUT = ROOT / "outputs" / "audit" / "fund_flow_promotion_evaluator_v5_28"
DEBUG = OUT / "debug"

GATES = {
    "settled_batch_count": (30, ">="),
    "settled_industry_count": (30, ">="),
    "mean_relative_return": (0.0, ">"),
    "median_relative_return": (0.0, ">"),
    "positive_batch_rate": (0.55, ">="),
    "top_quintile_hit_rate": (0.30, ">="),
    "candidate_entry_freeze_rate": (1.0, "=="),
    "benchmark_entry_freeze_rate": (1.0, "=="),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.28 promotion evaluator for settled fund-flow forward observations.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    active_cohort = validated_active_cohort()
    global_frame = load_authoritative_rows(EVENT_LEDGER)
    active_frame = filter_active_cohort(global_frame, active_cohort)
    settled = normalize_settled_frame(active_frame)
    global_settled = normalize_settled_frame(global_frame)
    integrity = read_json(INTEGRITY)
    freeze_manifest = read_json(FREEZE_MANIFEST)
    integrity_current, integrity_current_reason = integrity_inputs_current(integrity)
    batches = batch_metrics(settled)
    metric_checks = promotion_checks(settled, batches)
    dependency_checks = integrity_dependency_checks(
        settled,
        integrity,
        freeze_manifest,
        integrity_snapshot_current=integrity_current,
        integrity_snapshot_reason=integrity_current_reason,
        active_cohort=active_cohort,
    )
    checks = pd.concat([dependency_checks, metric_checks], ignore_index=True)
    summary = build_summary(
        settled, batches, checks, integrity=integrity, freeze_manifest=freeze_manifest,
        active_cohort=active_cohort, global_settled=global_settled,
    )
    write_outputs(summary, checks, settled, batches, global_settled=global_settled)
    print(f"output_dir={OUT}")
    print(f"promotion_ready={summary['promotion_ready']}")
    print(f"settled_batch_count={summary['settled_batch_count']}")


def load_settled(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    settled = filter_qualified_settled(frame)
    for col in ["realized_relative_return", "future_return_rank_pct"]:
        if col in settled:
            settled[col] = pd.to_numeric(settled[col], errors="coerce")
    if "future_top_quintile" in settled:
        settled["future_top_quintile"] = settled["future_top_quintile"].astype(str).eq("True")
    return settled


def load_authoritative_rows(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    verify_ledger_checkpoint(path)
    rows = materialize_observations(read_events(path))
    return pd.DataFrame(rows)


def load_authoritative_settled(path: Path, active_cohort: dict[str, Any] | None = None) -> pd.DataFrame:
    active = validated_active_cohort() if active_cohort is None else active_cohort
    return normalize_settled_frame(filter_active_cohort(load_authoritative_rows(path), active))


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


def normalize_settled_frame(frame: pd.DataFrame) -> pd.DataFrame:
    settled = filter_qualified_settled(frame)
    for col in ["realized_relative_return", "future_return_rank_pct"]:
        if col in settled:
            settled[col] = pd.to_numeric(settled[col], errors="coerce")
    if "future_top_quintile" in settled:
        settled["future_top_quintile"] = settled["future_top_quintile"].astype(str).eq("True")
    return settled


def is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "是"}


def filter_qualified_settled(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "settlement_status", "qualified_for_goal", "promotion_eligible", "integrity_eligible",
        "late_backfill_excluded", "entry_date_exact", "exit_date_exact",
        "benchmark_universe_count_used", "cohort_id", "cohort_manifest_hash",
    }
    if not required.issubset(frame.columns):
        return frame.iloc[0:0]
    benchmark_count = pd.to_numeric(frame["benchmark_universe_count_used"], errors="coerce").fillna(0)
    return frame[
        frame["settlement_status"].eq("settled")
        & frame["qualified_for_goal"].map(is_true)
        & frame["promotion_eligible"].map(is_true)
        & frame["integrity_eligible"].map(is_true)
        & ~frame["late_backfill_excluded"].map(is_true)
        & frame["entry_date_exact"].map(is_true)
        & frame["exit_date_exact"].map(is_true)
        & benchmark_count.ge(100)
    ].copy()


def integrity_dependency_checks(
    settled: pd.DataFrame,
    integrity: dict[str, Any],
    freeze_manifest: dict[str, Any],
    *,
    integrity_snapshot_current: bool = True,
    integrity_snapshot_reason: str = "not_checked",
    active_cohort: dict[str, Any] | None = None,
) -> pd.DataFrame:
    settled_hashes = sorted(set(settled.get("cohort_manifest_hash", pd.Series(dtype=str)).dropna().astype(str))) if not settled.empty else []
    settled_cohorts = sorted(set(settled.get("cohort_id", pd.Series(dtype=str)).dropna().astype(str))) if not settled.empty else []
    eligible_hashes = sorted(str(value) for value in integrity.get("eligible_cohort_hashes", []) if str(value))
    freeze_hash = str(freeze_manifest.get("manifest_hash", ""))
    freeze_cohort = str(freeze_manifest.get("cohort_id", ""))
    active = validated_active_cohort() if active_cohort is None else active_cohort
    active_match = (
        active.get("freeze_passed") is True
        and str(active.get("cohort_id", "")) == freeze_cohort
        and str(active.get("manifest_hash", "")) == freeze_hash
    )
    hash_match = bool(settled_hashes) and settled_hashes == eligible_hashes == [freeze_hash]
    cohort_match = bool(settled_cohorts) and settled_cohorts == [freeze_cohort]
    late_count = int(integrity.get("late_backfill_count", 0) or 0)
    rows = [
        {"metric": "current_integrity_passed", "current": bool(integrity.get("integrity_passed")), "required": True, "operator": "==", "status": "pass" if integrity.get("integrity_passed") else "fail"},
        {"metric": "integrity_snapshot_current", "current": integrity_snapshot_reason, "required": True, "operator": "==", "status": "pass" if integrity_snapshot_current else "fail"},
        {"metric": "cohort_baseline_passed", "current": bool(freeze_manifest.get("freeze_passed")), "required": True, "operator": "==", "status": "pass" if freeze_manifest.get("freeze_passed") else "fail"},
        {"metric": "active_cohort_revalidated", "current": active_match, "required": True, "operator": "==", "status": "pass" if active_match else "fail"},
        {"metric": "cohort_hash_match", "current": str(settled_hashes), "required": freeze_hash, "operator": "==", "status": "pass" if hash_match else "fail"},
        {"metric": "cohort_id_match", "current": str(settled_cohorts), "required": freeze_cohort, "operator": "==", "status": "pass" if cohort_match else "fail"},
        {"metric": "late_backfill_count", "current": late_count, "required": 0, "operator": "==", "status": "pass" if late_count == 0 else "fail"},
    ]
    return pd.DataFrame(rows)


def batch_metrics(settled: pd.DataFrame) -> pd.DataFrame:
    if settled.empty:
        return pd.DataFrame(columns=["batch_id", "industry_count", "mean_relative_return", "top_quintile_hit_rate"])
    return settled.groupby("batch_id").agg(
        industry_count=("industry_code", "count"),
        mean_relative_return=("realized_relative_return", "mean"),
        top_quintile_hit_rate=("future_top_quintile", "mean"),
    ).reset_index()


def promotion_checks(settled: pd.DataFrame, batches: pd.DataFrame) -> pd.DataFrame:
    metrics = {
        "settled_batch_count": len(batches),
        "settled_industry_count": len(settled),
        "mean_relative_return": float(settled["realized_relative_return"].mean()) if len(settled) else None,
        "median_relative_return": float(settled["realized_relative_return"].median()) if len(settled) else None,
        "positive_batch_rate": float(batches["mean_relative_return"].gt(0).mean()) if len(batches) else None,
        "top_quintile_hit_rate": float(settled["future_top_quintile"].mean()) if len(settled) else None,
        "candidate_entry_freeze_rate": freeze_rate(settled, "entry_price_freeze_status", "frozen_entry_price_used"),
        "benchmark_entry_freeze_rate": freeze_prefix_rate(settled, "benchmark_entry_freeze_status", "frozen_benchmark_entry_used"),
    }
    rows = []
    for metric, (required, op) in GATES.items():
        current = metrics[metric]
        rows.append({
            "metric": metric,
            "current": "" if current is None else current,
            "required": required,
            "operator": op,
            "status": status(current, required, op),
        })
    return pd.DataFrame(rows)


def freeze_rate(frame: pd.DataFrame, column: str, value: str) -> float | None:
    return None if frame.empty or column not in frame else float(frame[column].astype(str).eq(value).mean())


def freeze_prefix_rate(frame: pd.DataFrame, column: str, prefix: str) -> float | None:
    return None if frame.empty or column not in frame else float(frame[column].astype(str).str.startswith(prefix).mean())


def status(current: float | int | None, required: float | int, op: str) -> str:
    if current is None:
        return "pending"
    if op == "==":
        ok = current == required
    elif op == ">=":
        ok = current >= required
    else:
        ok = current > required
    return "pass" if ok else "fail"


def build_summary(
    settled: pd.DataFrame,
    batches: pd.DataFrame,
    checks: pd.DataFrame,
    *,
    integrity: dict[str, Any] | None = None,
    freeze_manifest: dict[str, Any] | None = None,
    active_cohort: dict[str, Any] | None = None,
    global_settled: pd.DataFrame | None = None,
) -> dict[str, Any]:
    integrity = integrity or {}
    freeze_manifest = freeze_manifest or {}
    active = active_cohort or {}
    promotion_ready = bool(len(checks) and checks["status"].eq("pass").all())
    return {
        "version": "5.28.2",
        "policy_id": "fund_flow_promotion_evaluator_v5_28",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "settled_batch_count": int(len(batches)),
        "settled_industry_count": int(len(settled)),
        "active_cohort_id": str(active.get("cohort_id", freeze_manifest.get("cohort_id", ""))),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", freeze_manifest.get("manifest_hash", ""))),
        "active_cohort_freeze_passed": active.get("freeze_passed") is True if active_cohort is not None else bool(freeze_manifest.get("freeze_passed", False)),
        "global_history_qualified_settled_rows": int(len(global_settled)) if global_settled is not None else int(len(settled)),
        "mean_relative_return": none_or_float(settled["realized_relative_return"].mean()) if len(settled) else None,
        "median_relative_return": none_or_float(settled["realized_relative_return"].median()) if len(settled) else None,
        "positive_batch_rate": none_or_float(batches["mean_relative_return"].gt(0).mean()) if len(batches) else None,
        "top_quintile_hit_rate": none_or_float(settled["future_top_quintile"].mean()) if len(settled) else None,
        "candidate_entry_freeze_rate": freeze_rate(settled, "entry_price_freeze_status", "frozen_entry_price_used"),
        "benchmark_entry_freeze_rate": freeze_prefix_rate(settled, "benchmark_entry_freeze_status", "frozen_benchmark_entry_used"),
        "integrity_passed": bool(integrity.get("integrity_passed", False)),
        "integrity_result_hash": str(integrity.get("integrity_result_hash", "")),
        "cohort_id": str(freeze_manifest.get("cohort_id", "")),
        "cohort_manifest_hash": str(freeze_manifest.get("manifest_hash", "")),
        "cohort_freeze_passed": bool(freeze_manifest.get("freeze_passed", False)),
        "late_backfill_count": int(integrity.get("late_backfill_count", 0) or 0),
        "promotion_ready": promotion_ready,
        "can_claim_strong_rebound_industries": promotion_ready,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_fund_flow_promotion_pending" if not promotion_ready else "fund_flow_forward_promotion_gate_passed",
        "final_verdict": "V5.28 资金流前推样本尚未通过晋级门槛，不能声称找到强反弹行业。" if not promotion_ready else "V5.28 资金流前推样本通过晋级门槛，但仍不代表自动交易许可。",
    }


def none_or_float(value: Any) -> float | None:
    return None if pd.isna(value) else float(value)


def write_outputs(
    summary: dict[str, Any],
    checks: pd.DataFrame,
    settled: pd.DataFrame,
    batches: pd.DataFrame,
    *,
    global_settled: pd.DataFrame | None = None,
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    checks.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, checks), encoding="utf-8")
    checks.to_csv(DEBUG / "promotion_checks.csv", index=False, encoding="utf-8-sig")
    settled.to_csv(DEBUG / "settled_observations.csv", index=False, encoding="utf-8-sig")
    batches.to_csv(DEBUG / "batch_metrics.csv", index=False, encoding="utf-8-sig")
    history = global_settled if global_settled is not None else settled
    history.to_csv(DEBUG / "global_qualified_settled_history.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.28 资金流前推晋级评价",
        "",
        summary["final_verdict"],
        "",
        f"- 已结算批次：{summary['settled_batch_count']}",
        f"- 已结算行业观察：{summary['settled_industry_count']}",
        f"- 平均相对收益：{fmt_pct(summary['mean_relative_return'])}",
        f"- 中位相对收益：{fmt_pct(summary['median_relative_return'])}",
        f"- 正超额批次比例：{fmt_pct(summary['positive_batch_rate'])}",
        f"- Top20% 命中率：{fmt_pct(summary['top_quintile_hit_rate'])}",
        f"- 候选入场价冻结使用率：{fmt_pct(summary['candidate_entry_freeze_rate'])}",
        f"- 基准入场点冻结使用率：{fmt_pct(summary['benchmark_entry_freeze_rate'])}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 晋级门槛",
        "",
        checks.to_markdown(index=False),
        "",
        "边界：V5.28 只评价 V5.27 已结算资金流前推样本，不读取未到期未来收益，不改变筛选规则。",
    ])


def fmt_pct(value: float | None) -> str:
    return "未结算" if value is None else f"{value:.2%}"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def self_check() -> None:
    integrity_fields = {
        "promotion_eligible": [True, True, True],
        "integrity_eligible": [True, True, True],
        "late_backfill_excluded": [False, False, False],
        "entry_date_exact": [True, True, True],
        "exit_date_exact": [True, True, True],
        "benchmark_universe_count_used": [131, 131, 131],
        "cohort_id": ["c1", "c1", "c1"],
        "cohort_manifest_hash": ["h1", "h1", "h1"],
    }
    settled = pd.DataFrame({
        "batch_id": ["b1", "b1", "b2"],
        "industry_code": ["1", "2", "3"],
        "realized_relative_return": [0.02, -0.01, 0.03],
        "future_top_quintile": [True, False, True],
        "entry_price_freeze_status": ["frozen_entry_price_used", "frozen_entry_price_used", "frozen_entry_price_used"],
        "benchmark_entry_freeze_status": ["frozen_benchmark_entry_used:3", "frozen_benchmark_entry_used:3", "frozen_benchmark_entry_used:3"],
        "settlement_status": ["settled", "settled", "settled"],
        "qualified_for_goal": [True, True, True],
        **integrity_fields,
    })
    batches = batch_metrics(settled)
    checks = promotion_checks(settled, batches)
    indexed = checks.set_index("metric")
    assert indexed.loc["settled_batch_count", "status"] == "fail"
    assert round(float(indexed.loc["top_quintile_hit_rate", "current"]), 3) == 0.667
    assert indexed.loc["candidate_entry_freeze_rate", "status"] == "pass"
    assert indexed.loc["benchmark_entry_freeze_rate", "status"] == "pass"
    bad = settled.copy()
    bad.loc[0, "entry_price_freeze_status"] = "not_available"
    bad_checks = promotion_checks(bad, batch_metrics(bad)).set_index("metric")
    assert bad_checks.loc["candidate_entry_freeze_rate", "status"] == "fail"
    assert build_summary(settled, batches, checks)["promotion_ready"] is False
    passing = pd.DataFrame({
        "batch_id": [f"b{i}" for i in range(30)],
        "industry_code": [str(i) for i in range(30)],
        "realized_relative_return": [0.01] * 30,
        "future_top_quintile": [True] * 10 + [False] * 20,
        "entry_price_freeze_status": ["frozen_entry_price_used"] * 30,
        "benchmark_entry_freeze_status": ["frozen_benchmark_entry_used:131"] * 30,
        "settlement_status": ["settled"] * 30,
        "qualified_for_goal": [True] * 30,
        "promotion_eligible": [True] * 30,
        "integrity_eligible": [True] * 30,
        "late_backfill_excluded": [False] * 30,
        "entry_date_exact": [True] * 30,
        "exit_date_exact": [True] * 30,
        "benchmark_universe_count_used": [131] * 30,
        "cohort_id": ["c1"] * 30,
        "cohort_manifest_hash": ["h1"] * 30,
    })
    integrity = {"integrity_passed": True, "eligible_cohort_hashes": ["h1"], "late_backfill_count": 0, "integrity_result_hash": "i1"}
    freeze_manifest = {"freeze_passed": True, "cohort_id": "c1", "manifest_hash": "h1"}
    active_fixture = {"freeze_passed": True, "cohort_id": "c1", "manifest_hash": "h1"}
    passing_checks = pd.concat([integrity_dependency_checks(passing, integrity, freeze_manifest, active_cohort=active_fixture), promotion_checks(passing, batch_metrics(passing))], ignore_index=True)
    passing_summary = build_summary(passing, batch_metrics(passing), passing_checks, integrity=integrity, freeze_manifest=freeze_manifest)
    assert passing_checks["status"].eq("pass").all()
    assert passing_summary["promotion_ready"] is True
    assert passing_summary["can_claim_strong_rebound_industries"] is True
    exploratory = passing.assign(qualified_for_goal=False)
    assert filter_qualified_settled(exploratory).empty
    failed_integrity = {**integrity, "integrity_passed": False, "late_backfill_count": 1, "eligible_cohort_hashes": []}
    blocked_checks = pd.concat([integrity_dependency_checks(passing, failed_integrity, freeze_manifest, active_cohort=active_fixture), promotion_checks(passing, batch_metrics(passing))], ignore_index=True)
    blocked_summary = build_summary(passing, batch_metrics(passing), blocked_checks, integrity=failed_integrity, freeze_manifest=freeze_manifest)
    assert blocked_summary["promotion_ready"] is False
    assert blocked_summary["can_claim_strong_rebound_industries"] is False
    print("self_check=pass")


if __name__ == "__main__":
    main()
