#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

from audit_v5_30_fund_flow_forward_ledger_integrity import evidence_artifact_manifest
from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from fund_flow_forward_evidence import (
    LEDGER_FIELDS,
    append_events,
    checkpoint_path_for,
    freeze_recorded_on_time,
    is_true,
    iso_utc,
    market_timestamp,
    materialize_observations,
    read_events,
    utc_now,
    verify_ledger_checkpoint,
    with_schema_fields,
    write_materialized_csv,
)
from research_integrity import HashChainError, atomic_write_csv, atomic_write_json, atomic_write_text, canonical_csv_bytes, csv_fingerprint, file_sha256, json_fingerprint


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv"
EVENT_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"
ENTRY_FREEZE = ROOT / "outputs" / "audit" / "fund_flow_entry_price_freeze_v5_33" / "debug" / "entry_price_freeze.csv"
BENCHMARK_ENTRY_FREEZE = ROOT / "outputs" / "audit" / "fund_flow_benchmark_entry_freeze_v5_34" / "debug" / "benchmark_entry_panel.csv"
ENTRY_FREEZE_LEDGER = ROOT / "logs" / "v5_33_fund_flow_entry_price_freeze.jsonl"
BENCHMARK_ENTRY_FREEZE_LEDGER = ROOT / "logs" / "v5_34_fund_flow_benchmark_entry_freeze.jsonl"
INTEGRITY = ROOT / "outputs" / "audit" / "fund_flow_forward_ledger_integrity_v5_30" / "run_summary.json"
SETTLEMENT_SOURCES = ROOT / "logs" / "v5_27_fund_flow_settlement_sources"
OUT = ROOT / "outputs" / "audit" / "fund_flow_forward_settlement_v5_27"
DEBUG = OUT / "debug"

EXTRA_FIELDS = [
    "actual_entry_date",
    "actual_exit_date",
    "realized_return",
    "benchmark_return",
    "realized_relative_return",
    "future_return_rank_pct",
    "future_top_quintile",
    "entry_price_freeze_status",
    "benchmark_entry_freeze_status",
    "entry_date_exact",
    "exit_date_exact",
    "benchmark_universe_count_used",
]


class ReadOnlySettlementProposalError(RuntimeError):
    """Raised when a read-only audit discovers a settlement it would have written."""


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.27 settle due V5.25 fund-flow forward observations.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument(
        "--read-only",
        "--dry-run",
        dest="read_only",
        action="store_true",
        help=(
            "Generate and validate V5.27 audit outputs without appending the event ledger, "
            "rewriting the materialized ledger, or updating any ledger checkpoint."
        ),
    )
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    as_of = datetime.fromisoformat(args.as_of_date).date()
    if as_of > date.today():
        parser.error(f"--as-of-date {args.as_of_date} is in the future; settle only after data exists.")

    try:
        summary = execute_settlement(as_of, read_only=args.read_only)
    except ReadOnlySettlementProposalError as exc:
        print("completion_status=blocked_read_only_settlement_proposal")
        print(f"failure={exc}")
        raise SystemExit(2) from exc
    print(f"output_dir={OUT}")
    print(f"settled_rows={summary['settled_rows']}")
    print(f"pending_rows={summary['pending_rows']}")


def execute_settlement(as_of: date, *, read_only: bool = False) -> dict[str, Any]:
    authoritative_before = authoritative_ledger_snapshot() if read_only else {}
    if read_only:
        _events, global_rows = load_authoritative_state_read_only(EVENT_LEDGER, LEDGER)
    else:
        _events, global_rows = load_authoritative_state(EVENT_LEDGER, LEDGER)
    active_cohort = validated_active_cohort()
    rows = filter_rows_to_active_cohort(global_rows, active_cohort)
    hist = load_history(HISTORY)
    entry_freeze = load_entry_freeze(ENTRY_FREEZE_LEDGER)
    benchmark_entry_freeze = load_benchmark_entry_freeze(BENCHMARK_ENTRY_FREEZE_LEDGER)
    integrity = read_json(INTEGRITY)
    integrity_snapshot_current, integrity_snapshot_reason = integrity_inputs_current(integrity, required_as_of=as_of)
    effective_integrity = dict(integrity)
    if not integrity_snapshot_current:
        effective_integrity.update({"integrity_passed": False, "eligible_cohort_hashes": [], "eligible_cohorts": []})
    after, settled = settle_rows(
        rows,
        hist,
        as_of,
        entry_freeze,
        benchmark_entry_freeze,
        integrity=effective_integrity,
        active_cohort=active_cohort,
    )
    before_settled = {str(row.get("observation_id", "")) for row in rows if row.get("settlement_status") == "settled"}
    new_settled = [row for row in settled if str(row.get("observation_id", "")) not in before_settled]
    if new_settled:
        if read_only:
            assert_authoritative_ledger_unchanged(authoritative_before)
            proposed_ids = sorted(str(row.get("observation_id", "")) for row in new_settled)
            raise ReadOnlySettlementProposalError(
                "read-only V5.27 found settlement events that would require an append; "
                f"proposed_count={len(new_settled)}; observation_ids={proposed_ids}"
            )
        if not EVENT_LEDGER.exists():
            raise RuntimeError("append-only event ledger is missing; migrate V5.25 before settlement")
        for row in new_settled:
            row.update(build_settlement_source_snapshot(row, hist, entry_freeze, benchmark_entry_freeze))
        append_events(EVENT_LEDGER, [settlement_event(row) for row in new_settled])
        events = read_events(EVENT_LEDGER)
        global_rows = materialize_observations(events)
        after = filter_rows_to_active_cohort(global_rows, active_cohort)
        settled = [row for row in after if row.get("settlement_status") == "settled"]
        write_materialized_csv(LEDGER, global_rows)
    summary = build_summary(
        after,
        settled,
        as_of,
        integrity=effective_integrity,
        integrity_snapshot_current=integrity_snapshot_current,
        integrity_snapshot_reason=integrity_snapshot_reason,
        active_cohort=active_cohort,
        global_rows=global_rows,
    )
    if read_only:
        authoritative_after = authoritative_ledger_snapshot()
        assert_authoritative_ledger_unchanged(
            authoritative_before,
            observed=authoritative_after,
        )
        summary.update({
            "execution_mode": "read_only_audit",
            "read_only": True,
            "proposed_settlement_count": 0,
            "event_ledger_write_invoked": False,
            "materialized_ledger_write_invoked": False,
            "checkpoint_write_invoked": False,
            "authoritative_ledger_files_unchanged": True,
            "authoritative_ledger_snapshot_before": authoritative_before,
            "authoritative_ledger_snapshot_after": authoritative_after,
        })
    write_outputs(summary, after, settled)
    if read_only:
        assert_authoritative_ledger_unchanged(authoritative_before)
    return summary


def load_authoritative_state(event_ledger: Path, materialized_csv: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not event_ledger.exists():
        if materialized_csv.exists():
            raise RuntimeError("authoritative V5.25 JSONL ledger is missing; compatibility CSV cannot be used for settlement")
        return [], []
    verify_ledger_checkpoint(event_ledger)
    events = read_events(event_ledger)
    rows = materialize_observations(events) if events else []
    write_materialized_csv(materialized_csv, rows)
    return events, rows


def load_authoritative_state_read_only(
    event_ledger: Path,
    materialized_csv: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify and materialize the event ledger in memory without repairing any file."""

    if not event_ledger.exists():
        if materialized_csv.exists():
            raise RuntimeError(
                "authoritative V5.25 JSONL ledger is missing; compatibility CSV cannot be used for settlement"
            )
        return [], []
    verify_ledger_checkpoint(event_ledger)
    events = read_events(event_ledger)
    rows = materialize_observations(events) if events else []
    if not materialized_csv.is_file():
        raise RuntimeError(
            "materialized V5.25 ledger is missing; read-only mode refuses to create or repair it"
        )
    materialized = [with_schema_fields(row) for row in rows]
    expected_bytes = b"\xef\xbb\xbf" + canonical_csv_bytes(
        materialized,
        fieldnames=LEDGER_FIELDS,
    )
    if materialized_csv.read_bytes() != expected_bytes:
        raise RuntimeError(
            "materialized V5.25 ledger differs from the verified event ledger; "
            "read-only mode refuses to rewrite it"
        )
    return events, rows


def authoritative_ledger_paths() -> tuple[Path, ...]:
    return (
        EVENT_LEDGER,
        LEDGER,
        checkpoint_path_for(EVENT_LEDGER),
        ENTRY_FREEZE_LEDGER,
        checkpoint_path_for(ENTRY_FREEZE_LEDGER),
        BENCHMARK_ENTRY_FREEZE_LEDGER,
        checkpoint_path_for(BENCHMARK_ENTRY_FREEZE_LEDGER),
    )


def authoritative_ledger_snapshot() -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for path in authoritative_ledger_paths():
        key = str(path.resolve())
        if path.is_file():
            snapshot[key] = {
                "exists": True,
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        else:
            snapshot[key] = {"exists": False, "bytes": 0, "sha256": "MISSING"}
    return snapshot


def assert_authoritative_ledger_unchanged(
    expected: dict[str, dict[str, Any]],
    *,
    observed: dict[str, dict[str, Any]] | None = None,
) -> None:
    current = authoritative_ledger_snapshot() if observed is None else observed
    if current != expected:
        changed = sorted(set(expected) | set(current))
        changed = [path for path in changed if expected.get(path) != current.get(path)]
        raise RuntimeError(
            "read-only V5.27 detected an authoritative ledger or checkpoint change; "
            f"changed_paths={changed}"
        )


def load_history(path: Path) -> pd.DataFrame:
    hist = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    hist["trade_date"] = pd.to_datetime(hist["trade_date"]).dt.date
    hist["industry_code"] = hist["industry_code"].str.zfill(6)
    hist["close_index"] = pd.to_numeric(hist["close_index"], errors="coerce")
    return hist.dropna(subset=["close_index"])


def settle_rows(
    rows: list[dict[str, str]],
    hist: pd.DataFrame,
    as_of: date,
    entry_freeze: dict[str, dict[str, str]] | None = None,
    benchmark_entry_freeze: dict[str, dict[str, dict[str, str]]] | None = None,
    *,
    integrity: dict[str, Any] | None = None,
    active_cohort: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    out, settled = [], []
    active = validated_active_cohort() if active_cohort is None else active_cohort
    active_pair = validated_cohort_pair(active)
    scoped_rows = filter_rows_to_active_cohort(rows, active)
    if active_pair is None:
        return out, settled
    observed_now_utc = now_utc or utc_now()
    integrity_data = integrity or {}
    integrity_passed = integrity_data.get("integrity_passed") is True
    eligible_cohorts = {
        (str(item.get("cohort_id", "")), str(item.get("manifest_hash", "")))
        for item in integrity_data.get("eligible_cohorts", [])
        if isinstance(item, dict) and item.get("cohort_id") and item.get("manifest_hash")
    } & {active_pair}
    for row in scoped_rows:
        item = with_extra_fields(dict(row))
        if item.get("settlement_status") == "settled":
            settled.append(item)
            out.append(item)
            continue
        exit_date = parse_date(item.get("planned_exit_date", ""))
        if not exit_date or exit_date > as_of:
            item["settlement_status"] = "not_due"
            item["settlement_notes"] = "未到计划退出日，不填未来收益。"
            out.append(item)
            continue
        result = settle_one(
            item,
            hist,
            as_of,
            entry_freeze or {},
            benchmark_entry_freeze or {},
            integrity_passed=integrity_passed,
            eligible_cohorts=eligible_cohorts,
            now_utc=observed_now_utc,
        )
        item.update(result)
        if item.get("settlement_status") == "settled":
            settled.append(item)
        out.append(item)
    return out, settled


def validated_cohort_pair(active_cohort: dict[str, Any] | None) -> tuple[str, str] | None:
    active = active_cohort or {}
    cohort_id = str(active.get("cohort_id", ""))
    manifest_hash = str(active.get("manifest_hash", ""))
    if active.get("freeze_passed") is not True or not cohort_id or not manifest_hash:
        return None
    return cohort_id, manifest_hash


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


def settle_one(
    row: dict[str, str],
    hist: pd.DataFrame,
    as_of: date,
    entry_freeze: dict[str, dict[str, str]] | None = None,
    benchmark_entry_freeze: dict[str, dict[str, dict[str, str]]] | None = None,
    *,
    integrity_passed: bool = False,
    eligible_cohorts: set[tuple[str, str]] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, str]:
    entry = parse_date(row.get("planned_entry_date", ""))
    exit_ = parse_date(row.get("planned_exit_date", ""))
    if not entry or not exit_:
        return {"settlement_status": "pending_bad_dates", "settlement_notes": "计划入场或退出日期格式错误。"}
    if exit_ > as_of:
        return {"settlement_status": "not_due", "settlement_notes": "未到计划退出日，不填未来收益。"}
    observed_now_utc = now_utc or utc_now()
    if observed_now_utc.tzinfo is None or observed_now_utc < market_timestamp(exit_, time(15, 0)):
        return {"settlement_status": "pending_exit_market_close", "settlement_notes": "计划退出日尚未到北京时间 15:00 收盘，禁止读取或写入退出收益。"}
    if is_true(row.get("late_backfill_excluded")):
        return {"settlement_status": "pending_late_observation_excluded", "settlement_notes": "观察在入场截止后登记，永久不得作为前推样本结算。"}
    cohort_key = (str(row.get("cohort_id", "")), str(row.get("cohort_manifest_hash", "")))
    if not integrity_passed or cohort_key not in (eligible_cohorts or set()):
        return {"settlement_status": "pending_integrity_gate_failed", "settlement_notes": "当前 cohort 未通过 V5.30 完整性审计，禁止直接结算。"}
    if is_true(row.get("qualified_for_goal")) and not all(is_true(row.get(field)) for field in ["integrity_eligible", "promotion_eligible"]):
        return {"settlement_status": "pending_qualification_gate_failed", "settlement_notes": "目标样本缺少完整性或晋级资格，禁止结算。"}
    all_returns = returns_between(hist, entry, exit_, as_of)
    selected = all_returns[all_returns["industry_code"].eq(str(row.get("industry_code", "")).zfill(6))]
    if all_returns.empty or selected.empty:
        return {"settlement_status": "pending_missing_price", "settlement_notes": "缺少入场或退出价格，无法结算。"}
    selected_return = float(selected.iloc[0]["return"])
    actual_entry_date = str(selected.iloc[0]["entry_trade_date"])
    freeze = (entry_freeze or {}).get(freeze_key(row))
    freeze_status = "not_available"
    if freeze and freeze.get("entry_price_freeze_status") == "frozen_on_time" and freeze_recorded_on_time(freeze):
        frozen_close = float(freeze["entry_close_index"])
        selected_return = float(selected.iloc[0]["close_index_exit"]) / frozen_close - 1.0
        actual_entry_date = freeze.get("actual_entry_date", actual_entry_date)
        all_returns.loc[selected.index[0], "return"] = selected_return
        freeze_status = "frozen_entry_price_used"
    if freeze and freeze.get("entry_price_freeze_status") == "late_backfill_excluded":
        return {"settlement_status": "pending_late_entry_freeze_excluded", "settlement_notes": "候选行业入场价为事后回填，永久不得用于结算。"}
    if freeze_status != "frozen_entry_price_used":
        return {"settlement_status": "pending_missing_entry_freeze", "settlement_notes": "缺少按时、精确的候选行业冻结入场价，不结算相对收益。"}
    benchmark_returns, benchmark_freeze_status = apply_benchmark_entry_freeze(all_returns, row, benchmark_entry_freeze or {})
    if benchmark_freeze_status == "late_backfill_excluded":
        return {"settlement_status": "pending_late_benchmark_freeze_excluded", "settlement_notes": "全行业基准为事后回填，永久不得用于结算。"}
    if benchmark_freeze_status == "insufficient_benchmark_universe":
        return {"settlement_status": "pending_insufficient_benchmark_universe", "settlement_notes": "按时冻结且具备精确退出价的基准行业不足 100 个。"}
    if not benchmark_freeze_status.startswith("frozen_benchmark_entry_used"):
        return {"settlement_status": "pending_missing_benchmark_entry_freeze", "settlement_notes": "缺少按时、精确的全行业冻结入场点，不结算相对收益。"}
    if not selected_entry_prices_match(row, freeze or {}, benchmark_entry_freeze or {}):
        return {"settlement_status": "pending_entry_freeze_price_mismatch", "settlement_notes": "候选冻结入场价与同一基准组内该候选入场价不一致，禁止结算。"}
    if selected.index[0] not in benchmark_returns.index:
        return {"settlement_status": "pending_benchmark_missing_selected_industry", "settlement_notes": "冻结基准缺少候选行业本身，不结算相对收益。"}
    benchmark = float(benchmark_returns["return"].mean())
    rank_pct = float(benchmark_returns["return"].rank(pct=True).loc[selected.index[0]]) if selected.index[0] in benchmark_returns.index else float(all_returns["return"].rank(pct=True).loc[selected.index[0]])
    return {
        "actual_entry_date": actual_entry_date,
        "actual_exit_date": str(selected.iloc[0]["exit_trade_date"]),
        "realized_return": f"{selected_return:.10f}",
        "benchmark_return": f"{benchmark:.10f}",
        "realized_relative_return": f"{selected_return - benchmark:.10f}",
        "future_return_rank_pct": f"{rank_pct:.10f}",
        "future_top_quintile": str(rank_pct >= 0.8),
        "outcome_status": "settled_forward_observation",
        "settlement_status": "settled",
        "entry_price_freeze_status": freeze_status,
        "benchmark_entry_freeze_status": benchmark_freeze_status,
        "entry_date_exact": "True",
        "exit_date_exact": "True",
        "benchmark_universe_count_used": benchmark_freeze_status.rsplit(":", 1)[-1],
        "settlement_notes": "按申万二级行业指数收盘价结算；基准为同区间全行业等权平均收益。",
    }


def returns_between(hist: pd.DataFrame, entry: date, exit_: date, as_of: date) -> pd.DataFrame:
    available = hist[hist["trade_date"].le(as_of)]
    entry_rows = available[available["trade_date"].eq(entry)].sort_values("industry_code").drop_duplicates("industry_code", keep="first")
    exit_rows = available[available["trade_date"].eq(exit_)].sort_values("industry_code").drop_duplicates("industry_code", keep="first")
    merged = entry_rows[["industry_code", "industry_name", "trade_date", "close_index"]].merge(
        exit_rows[["industry_code", "trade_date", "close_index"]],
        on="industry_code",
        suffixes=("_entry", "_exit"),
    )
    merged["return"] = merged["close_index_exit"] / merged["close_index_entry"] - 1.0
    merged = merged.rename(columns={"trade_date_entry": "entry_trade_date", "trade_date_exit": "exit_trade_date"})
    return merged


def apply_benchmark_entry_freeze(all_returns: pd.DataFrame, row: dict[str, str], benchmark_entry_freeze: dict[str, dict[str, dict[str, str]]]) -> tuple[pd.DataFrame, str]:
    frozen = benchmark_entry_freeze.get(benchmark_key(row), {})
    if all_returns.empty or not frozen:
        return all_returns, "not_available"
    if any(item.get("benchmark_entry_freeze_status") == "late_backfill_excluded" for item in frozen.values()):
        return all_returns, "late_backfill_excluded"
    planned = str(row.get("planned_entry_date", ""))
    cohort_id = str(row.get("cohort_id", ""))
    cohort_hash = str(row.get("cohort_manifest_hash", ""))
    eligible = {
        code: item
        for code, item in frozen.items()
        if item.get("benchmark_entry_freeze_status") == "frozen_on_time"
        and freeze_recorded_on_time(item)
        and str(item.get("actual_entry_date", "")) == planned
        and (not cohort_id or str(item.get("cohort_id", "")) == cohort_id)
        and (not cohort_hash or str(item.get("cohort_manifest_hash", "")) == cohort_hash)
    }
    selected_code = str(row.get("industry_code", "")).zfill(6)
    if selected_code not in eligible:
        return all_returns, "selected_industry_missing"
    out = all_returns[all_returns["industry_code"].isin(eligible)].copy()
    if len(eligible) < 100 or out["industry_code"].nunique() < 100:
        return out, "insufficient_benchmark_universe"
    out["close_index_entry"] = out["industry_code"].map({code: float(item["entry_close_index"]) for code, item in eligible.items()}).astype(float)
    out["return"] = out["close_index_exit"] / out["close_index_entry"] - 1.0
    return out, f"frozen_benchmark_entry_used:{len(out)}"


def selected_entry_prices_match(
    row: dict[str, str],
    candidate_freeze: dict[str, str],
    benchmark_entry_freeze: dict[str, dict[str, dict[str, str]]],
) -> bool:
    selected_code = str(row.get("industry_code", "")).zfill(6)
    benchmark_item = benchmark_entry_freeze.get(benchmark_key(row), {}).get(selected_code, {})
    try:
        candidate_price = Decimal(str(candidate_freeze.get("entry_close_index", "")))
        benchmark_price = Decimal(str(benchmark_item.get("entry_close_index", "")))
    except InvalidOperation:
        return False
    return candidate_price.is_finite() and benchmark_price.is_finite() and candidate_price == benchmark_price


def build_settlement_source_snapshot(
    row: dict[str, str],
    hist: pd.DataFrame,
    entry_freeze: dict[str, dict[str, str]],
    benchmark_entry_freeze: dict[str, dict[str, dict[str, str]]],
    *,
    output_dir: Path = SETTLEMENT_SOURCES,
) -> dict[str, str]:
    if row.get("settlement_status") != "settled":
        raise ValueError("settlement source snapshots are only created for settled rows")
    planned_entry = str(row.get("planned_entry_date", ""))
    planned_exit = parse_date(row.get("planned_exit_date", ""))
    selected_code = str(row.get("industry_code", "")).zfill(6)
    candidate = entry_freeze.get(freeze_key(row), {})
    frozen = benchmark_entry_freeze.get(benchmark_key(row), {})
    cohort_id = str(row.get("cohort_id", ""))
    cohort_hash = str(row.get("cohort_manifest_hash", ""))
    eligible = {
        code: item for code, item in frozen.items()
        if item.get("benchmark_entry_freeze_status") == "frozen_on_time"
        and freeze_recorded_on_time(item)
        and str(item.get("actual_entry_date", "")) == planned_entry
        and str(item.get("cohort_id", "")) == cohort_id
        and str(item.get("cohort_manifest_hash", "")) == cohort_hash
    }
    if not planned_exit or len(eligible) < 100 or selected_code not in eligible:
        raise ValueError("cannot build settlement snapshot without an exact >=100-member frozen benchmark containing the candidate")
    exit_rows = hist[hist["trade_date"].eq(planned_exit)].sort_values("industry_code").drop_duplicates("industry_code", keep="first")
    exit_by_code = {str(item["industry_code"]).zfill(6): item for item in exit_rows.to_dict("records")}
    snapshot_rows = []
    for code, item in sorted(eligible.items()):
        exit_item = exit_by_code.get(code)
        if not exit_item:
            continue
        snapshot_rows.append({
            "industry_code": code,
            "planned_entry_date": planned_entry,
            "planned_exit_date": planned_exit.isoformat(),
            "benchmark_entry_close_index": f"{float(item['entry_close_index']):.10f}",
            "candidate_entry_close_index": (f"{float(candidate['entry_close_index']):.10f}" if code == selected_code else ""),
            "exit_close_index": f"{float(exit_item['close_index']):.10f}",
            "selected_candidate": str(code == selected_code),
        })
    if len(snapshot_rows) < 100 or not any(item["industry_code"] == selected_code for item in snapshot_rows):
        raise ValueError("settlement snapshot has fewer than 100 exact-exit rows or misses the candidate")
    fields = [
        "industry_code", "planned_entry_date", "planned_exit_date",
        "benchmark_entry_close_index", "candidate_entry_close_index",
        "exit_close_index", "selected_candidate",
    ]
    content_hash = csv_fingerprint(snapshot_rows, fieldnames=fields, sort_rows_by=["industry_code"])
    target = output_dir / f"{content_hash}.csv"
    expected_bytes = b"\xef\xbb\xbf" + canonical_csv_bytes(snapshot_rows, fieldnames=fields, sort_rows_by=["industry_code"])
    if target.exists():
        if target.read_bytes() != expected_bytes:
            raise RuntimeError(f"immutable settlement snapshot collision: {target}")
    else:
        atomic_write_csv(target, snapshot_rows, fieldnames=fields, sort_rows_by=["industry_code"])
    return {
        "settlement_source_artifact": str(target.relative_to(ROOT)).replace("\\", "/") if target.is_relative_to(ROOT) else str(target),
        "settlement_source_fingerprint": file_sha256(target),
        "settlement_source_row_count": str(len(snapshot_rows)),
        "settlement_calculation_version": "fund_flow_forward_settlement_exact_v2",
    }



def freeze_key(row: dict[str, str]) -> str:
    return "|".join([
        str(row.get("cohort_id", "")),
        str(row.get("cohort_manifest_hash", "")),
        str(row.get("batch_id", "")),
        str(row.get("industry_code", "")).zfill(6),
    ])


def benchmark_key(row: dict[str, str] | pd.Series) -> str:
    return "|".join([
        str(row.get("cohort_id", "")),
        str(row.get("cohort_manifest_hash", "")),
        str(row.get("batch_id", "")),
        str(row.get("planned_entry_date", "")),
    ])


def load_entry_freeze(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    if path.suffix.lower() == ".jsonl":
        rows = read_events(path)
    else:
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}).fillna("")
        rows = frame.to_dict("records")
    return {freeze_key(row): {key: str(value) for key, value in row.items()} for row in rows}


def load_benchmark_entry_freeze(path: Path) -> dict[str, dict[str, dict[str, str]]]:
    if not path.exists():
        return {}
    if path.suffix.lower() == ".jsonl":
        rows = read_events(path)
    else:
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}).fillna("")
        rows = frame.to_dict("records")
    out: dict[str, dict[str, dict[str, str]]] = {}
    for row in rows:
        key = benchmark_key(row)
        out.setdefault(key, {})[str(row.get("industry_code", "")).zfill(6)] = {key: str(value) for key, value in row.items()}
    return out

def parse_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def with_extra_fields(row: dict[str, str]) -> dict[str, str]:
    for field in EXTRA_FIELDS:
        row.setdefault(field, "")
    return row


def settlement_event(row: dict[str, str]) -> dict[str, Any]:
    item = with_schema_fields(row)
    observation_id = str(item.get("observation_id", ""))
    if not observation_id:
        raise ValueError("settlement requires observation_id from the append-only ledger")
    parent = str(item.get("event_id", ""))
    item.update({
        "event_type": "settlement",
        "event_id": f"{observation_id}:settlement:{item.get('planned_exit_date', '')}",
        "parent_event_id": parent,
        "event_recorded_at_utc": iso_utc(utc_now()),
        "ledger_sequence": "",
        "previous_hash": "",
        "record_hash": "",
    })
    return item


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    raise RuntimeError("direct ledger overwrite is forbidden; append a settlement event")


def build_summary(
    rows: list[dict[str, str]],
    settled: list[dict[str, str]],
    as_of: date,
    *,
    integrity: dict[str, Any] | None = None,
    integrity_snapshot_current: bool = False,
    integrity_snapshot_reason: str = "not_checked",
    active_cohort: dict[str, Any] | None = None,
    global_rows: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    active = validated_active_cohort() if active_cohort is None else active_cohort
    active_pair = validated_cohort_pair(active)
    scoped_rows = filter_rows_to_active_cohort(rows, active)
    scoped_settled = filter_rows_to_active_cohort(settled, active)
    qualified_settled = [row for row in scoped_settled if is_true(row.get("qualified_for_goal"))]
    qualified_rows = [row for row in scoped_rows if is_true(row.get("qualified_for_goal"))]
    integrity_data = integrity or {}
    eligible_pairs = {
        (str(item.get("cohort_id", "")), str(item.get("manifest_hash", "")))
        for item in integrity_data.get("eligible_cohorts", [])
        if isinstance(item, dict) and item.get("cohort_id") and item.get("manifest_hash")
    }
    active_integrity_passed = bool(
        active_pair
        and integrity_snapshot_current
        and integrity_data.get("integrity_passed") is True
        and active_pair in eligible_pairs
    )
    history = list(global_rows) if global_rows is not None else list(rows)
    history_settled = [row for row in history if row.get("settlement_status") == "settled"]
    global_cohorts = {
        (str(row.get("cohort_id", "")), str(row.get("cohort_manifest_hash", "")))
        for row in history
        if str(row.get("cohort_id", "")) or str(row.get("cohort_manifest_hash", ""))
    }
    return {
        "version": "5.27.3",
        "policy_id": "fund_flow_forward_settlement_v5_27",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "active_cohort_id": active_pair[0] if active_pair else "",
        "active_cohort_manifest_hash": active_pair[1] if active_pair else "",
        "active_cohort_validated": active_pair is not None,
        "ledger_rows": len(scoped_rows),
        "settled_rows": len(scoped_settled),
        "pending_rows": sum(row.get("settlement_status") != "settled" for row in scoped_rows),
        "qualified_ledger_rows": len(qualified_rows),
        "qualified_settled_rows": len(qualified_settled),
        "qualified_pending_rows": sum(row.get("settlement_status") != "settled" for row in qualified_rows),
        "exploratory_settled_rows": len(scoped_settled) - len(qualified_settled),
        "input_integrity_passed": active_integrity_passed,
        "integrity_snapshot_current": integrity_snapshot_current,
        "integrity_snapshot_reason": integrity_snapshot_reason,
        "eligible_cohort_hashes": [active_pair[1]] if active_integrity_passed and active_pair else [],
        "eligible_cohorts": ([{"cohort_id": active_pair[0], "manifest_hash": active_pair[1]}] if active_integrity_passed and active_pair else []),
        "global_history_ledger_rows": len(history),
        "global_history_settled_rows": len(history_settled),
        "global_history_cohort_count": len(global_cohorts),
        "mean_realized_relative_return": mean_float(qualified_settled, "realized_relative_return"),
        "top_quintile_hit_rate": mean_bool(qualified_settled, "future_top_quintile"),
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_forward_settlement_pending" if not qualified_settled else "research_only_qualified_forward_settled_not_promoted",
        "final_verdict": "V5.27 只结算经 V5.31 重新校验的当前 cohort；全局历史仅作独立诊断，不进入当期门禁和晋级统计。",
    }


def mean_float(rows: list[dict[str, str]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field)]
    return sum(values) / len(values) if values else None


def mean_bool(rows: list[dict[str, str]], field: str) -> float | None:
    values = [row.get(field) == "True" for row in rows if row.get(field)]
    return sum(values) / len(values) if values else None


def is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "是"}


def write_outputs(summary: dict[str, Any], rows: list[dict[str, str]], settled: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    top = pd.DataFrame(settled if settled else rows)
    atomic_write_csv(OUT / "top_candidates.csv", top.fillna("").to_dict("records"), fieldnames=list(top.columns))
    atomic_write_json(OUT / "run_summary.json", summary)
    atomic_write_text(OUT / "report.md", render_report(summary))
    row_frame = pd.DataFrame(rows)
    settled_frame = pd.DataFrame(settled)
    atomic_write_csv(DEBUG / "forward_ledger_snapshot.csv", row_frame.fillna("").to_dict("records"), fieldnames=list(row_frame.columns))
    atomic_write_csv(DEBUG / "settled_forward_rows.csv", settled_frame.fillna("").to_dict("records"), fieldnames=list(settled_frame.columns))
    atomic_write_csv(DEBUG / "settlement_audit.csv", [stringify(summary)], fieldnames=list(stringify(summary)))


def render_report(summary: dict[str, Any]) -> str:
    return "\n".join([
        "# V5.27 资金流前推样本结算",
        "",
        summary["final_verdict"],
        "",
        f"- as-of 日期：{summary['as_of_date']}",
        f"- 账本行数：{summary['ledger_rows']}",
        f"- 已结算：{summary['settled_rows']}",
        f"- 合格目标样本已结算：{summary['qualified_settled_rows']}",
        f"- 探索样本已结算：{summary['exploratory_settled_rows']}",
        f"- 待结算：{summary['pending_rows']}",
        f"- 平均相对收益：{fmt_pct(summary['mean_realized_relative_return'])}",
        f"- Top20% 命中率：{fmt_pct(summary['top_quintile_hit_rate'])}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "边界：只结算 V5.25 冻结的真实前推观察，不新增历史回填样本，不改变资金流筛选规则。",
    ])


def fmt_pct(value: float | None) -> str:
    return "未结算" if value is None else f"{value:.2%}"


def stringify(payload: dict[str, Any]) -> dict[str, str]:
    return {key: "" if value is None else str(value) for key, value in payload.items()}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def integrity_inputs_current(integrity: dict[str, Any], *, required_as_of: date | None = None) -> tuple[bool, str]:
    reasons: list[str] = []
    claimed_hash = str(integrity.get("integrity_result_hash", ""))
    payload = {key: value for key, value in integrity.items() if key not in {"generated_at", "integrity_result_hash"}}
    if not claimed_hash or json_fingerprint(payload) != claimed_hash:
        reasons.append("V5.30 integrity_result_hash mismatch")
    if required_as_of is not None and str(integrity.get("as_of_date", "")) != required_as_of.isoformat():
        reasons.append(f"V5.30 as_of_date is not {required_as_of.isoformat()}")
    expected_files = {
        "ledger_event_file_sha256": EVENT_LEDGER,
        "materialized_ledger_file_sha256": LEDGER,
        "candidate_freeze_event_file_sha256": ENTRY_FREEZE_LEDGER,
        "benchmark_freeze_event_file_sha256": BENCHMARK_ENTRY_FREEZE_LEDGER,
        "observation_checkpoint_file_sha256": checkpoint_path_for(EVENT_LEDGER),
        "candidate_freeze_checkpoint_file_sha256": checkpoint_path_for(ENTRY_FREEZE_LEDGER),
        "benchmark_freeze_checkpoint_file_sha256": checkpoint_path_for(BENCHMARK_ENTRY_FREEZE_LEDGER),
    }
    for field, path in expected_files.items():
        actual = file_sha256(path) if path.is_file() else "MISSING"
        if str(integrity.get(field, "")) != actual:
            reasons.append(f"{field} changed")
    for path in [EVENT_LEDGER, ENTRY_FREEZE_LEDGER, BENCHMARK_ENTRY_FREEZE_LEDGER]:
        try:
            verify_ledger_checkpoint(path)
        except Exception as exc:
            reasons.append(f"{path.name} checkpoint invalid: {exc}")
    active = validated_active_cohort()
    active_pair = (str(active.get("cohort_id", "")), str(active.get("manifest_hash", "")))
    eligible_pairs = {
        (str(item.get("cohort_id", "")), str(item.get("manifest_hash", "")))
        for item in integrity.get("eligible_cohorts", [])
        if isinstance(item, dict)
    }
    if active.get("freeze_passed") is not True or active_pair not in eligible_pairs:
        reasons.append("active cohort id/hash is not the V5.30 eligible pair")
    try:
        observation_rows = materialize_observations(read_events(EVENT_LEDGER)) if EVENT_LEDGER.exists() else []
        active_rows = filter_rows_to_active_cohort(observation_rows, active)
        entry_events = pd.DataFrame(read_events(ENTRY_FREEZE_LEDGER)) if ENTRY_FREEZE_LEDGER.exists() else pd.DataFrame()
        benchmark_events = pd.DataFrame(read_events(BENCHMARK_ENTRY_FREEZE_LEDGER)) if BENCHMARK_ENTRY_FREEZE_LEDGER.exists() else pd.DataFrame()
        cohort_id, cohort_hash = active_pair
        active_entry = filter_freeze_frame_to_pair(entry_events, cohort_id, cohort_hash)
        active_benchmark = filter_freeze_frame_to_pair(benchmark_events, cohort_id, cohort_hash)
        source_manifest, source_count = evidence_artifact_manifest(active_rows, active_entry, active_benchmark, ROOT)
        if str(integrity.get("evidence_source_manifest_sha256", "")) != source_manifest:
            reasons.append("evidence_source_manifest_sha256 changed")
        if int(integrity.get("evidence_source_artifact_count", -1)) != source_count:
            reasons.append("evidence_source_artifact_count changed")
    except (OSError, ValueError, TypeError, HashChainError) as exc:
        reasons.append(f"evidence source manifest cannot be recomputed: {exc}")
    if str(integrity.get("ledger_head_hash", "")):
        try:
            if verify_ledger_checkpoint(EVENT_LEDGER)["head_hash"] != str(integrity.get("ledger_head_hash", "")):
                reasons.append("observation ledger head changed")
        except Exception:
            pass
    return not reasons, "current" if not reasons else "; ".join(reasons)


def filter_freeze_frame_to_pair(frame: pd.DataFrame, cohort_id: str, cohort_hash: str) -> pd.DataFrame:
    if frame.empty or not {"cohort_id", "cohort_manifest_hash"}.issubset(frame.columns):
        return frame.iloc[0:0].copy()
    return frame[
        frame["cohort_id"].astype(str).eq(cohort_id)
        & frame["cohort_manifest_hash"].astype(str).eq(cohort_hash)
    ].copy()


def self_check() -> None:
    hist_rows = []
    for index in range(1, 101):
        code = f"{index:06d}"
        hist_rows.extend([
            {"trade_date": date(2026, 1, 1), "industry_code": code, "industry_name": code, "close_index": 100.0},
            {"trade_date": date(2026, 1, 2), "industry_code": code, "industry_name": code, "close_index": 110.0 if index == 1 else 105.0},
        ])
    hist = pd.DataFrame(hist_rows)
    row = {
        "batch_id": "b", "industry_code": "000001", "planned_entry_date": "2026-01-01",
        "planned_exit_date": "2026-01-02", "settlement_status": "not_due",
        "qualified_for_goal": "True", "late_backfill_excluded": "False",
        "integrity_eligible": "True", "promotion_eligible": "True",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }
    gate = {"integrity_passed": True, "eligible_cohort_hashes": ["h1"], "eligible_cohorts": [{"cohort_id": "c1", "manifest_hash": "h1"}]}
    gate_kwargs = {"integrity_passed": True, "eligible_cohorts": {("c1", "h1")}}
    active_fixture = {"freeze_passed": True, "cohort_id": "c1", "manifest_hash": "h1"}
    result = settle_one(row, hist, date(2026, 1, 2), **gate_kwargs)
    assert result["settlement_status"] == "pending_missing_entry_freeze"
    entry_freeze = {"c1|h1|b|000001": {
        "entry_price_freeze_status": "frozen_on_time", "entry_close_index": "100",
        "actual_entry_date": "2026-01-01", "planned_entry_date": "2026-01-01",
        "as_of_date": "2026-01-01", "freeze_at_utc": "2026-01-01T07:30:00Z",
        "cohort_id": "c1", "cohort_manifest_hash": "h1",
    }}
    frozen_benchmark = {"c1|h1|b|2026-01-01": {
        f"{index:06d}": {
            "benchmark_entry_freeze_status": "frozen_on_time", "entry_close_index": "100",
            "actual_entry_date": "2026-01-01", "planned_entry_date": "2026-01-01",
            "as_of_date": "2026-01-01", "freeze_at_utc": "2026-01-01T07:30:00Z",
            "cohort_id": "c1", "cohort_manifest_hash": "h1",
        }
        for index in range(1, 101)
    }}
    no_benchmark = settle_one(row, hist, date(2026, 1, 2), entry_freeze, {}, **gate_kwargs)
    assert no_benchmark["settlement_status"] == "pending_missing_benchmark_entry_freeze"
    insufficient = {"c1|h1|b|2026-01-01": dict(list(frozen_benchmark["c1|h1|b|2026-01-01"].items())[:99])}
    insufficient_result = settle_one(row, hist, date(2026, 1, 2), entry_freeze, insufficient, **gate_kwargs)
    assert insufficient_result["settlement_status"] == "pending_insufficient_benchmark_universe"
    missing_selected = {"c1|h1|b|2026-01-01": {
        f"{index:06d}": {**next(iter(frozen_benchmark["c1|h1|b|2026-01-01"].values())), "entry_close_index": "100"}
        for index in range(101, 201)
    }}
    missing = settle_one(row, hist, date(2026, 1, 2), entry_freeze, missing_selected, **gate_kwargs)
    assert missing["settlement_status"] == "pending_missing_benchmark_entry_freeze"
    frozen = settle_one(row, hist, date(2026, 1, 2), entry_freeze, frozen_benchmark, **gate_kwargs)
    assert frozen["settlement_status"] == "settled"
    assert frozen["benchmark_entry_freeze_status"] == "frozen_benchmark_entry_used:100"
    assert float(frozen["realized_relative_return"]) > 0
    rows, settled = settle_rows([row], hist, date(2026, 1, 1), integrity=gate, active_cohort=active_fixture)
    assert rows[0]["settlement_status"] == "not_due"
    assert not settled
    settled_input = {**row, **frozen}
    rows, settled = settle_rows(
        [settled_input], hist, date(2026, 1, 2), entry_freeze, frozen_benchmark,
        integrity=gate, active_cohort=active_fixture,
    )
    assert rows[0]["settlement_status"] == "settled"
    assert len(settled) == 1
    summary = build_summary(
        rows,
        settled,
        date(2026, 1, 2),
        integrity=gate,
        integrity_snapshot_current=True,
        active_cohort=active_fixture,
    )
    assert summary["settled_rows"] == 1
    assert summary["qualified_settled_rows"] == 1
    print("self_check=pass")


if __name__ == "__main__":
    main()




