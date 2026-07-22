#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from fund_flow_forward_evidence import (
    event_chain_head,
    freeze_deadline_utc,
    freeze_recorded_on_time,
    freeze_timing_status,
    iso_utc,
    persist_immutable_freezes,
    read_events,
    relative_posix,
    snapshot_source,
    utc_now,
    verify_ledger_checkpoint,
)
from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from research_integrity import atomic_write_csv, atomic_write_json, atomic_write_text, csv_fingerprint


ROOT = Path(__file__).resolve().parents[1]
ENTRY_FREEZE = ROOT / "outputs" / "audit" / "fund_flow_entry_price_freeze_v5_33" / "debug" / "entry_price_freeze.csv"
ENTRY_FREEZE_LEDGER = ROOT / "logs" / "v5_33_fund_flow_entry_price_freeze.jsonl"
HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"
CURRENT_SNAPSHOT = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "raw_industry_panel.csv"
FREEZE_LEDGER = ROOT / "logs" / "v5_34_fund_flow_benchmark_entry_freeze.jsonl"
SOURCE_SNAPSHOTS = ROOT / "logs" / "v5_34_fund_flow_benchmark_entry_sources"
OUT = ROOT / "outputs" / "audit" / "fund_flow_benchmark_entry_freeze_v5_34"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.34 freeze all-industry benchmark entry panel for fund-flow forward observations.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    as_of = date.fromisoformat(args.as_of_date)
    if as_of > date.today():
        parser.error(f"--as-of-date {args.as_of_date} is in the future; benchmark freeze must use current or past data.")

    if ENTRY_FREEZE_LEDGER.exists():
        verify_ledger_checkpoint(ENTRY_FREEZE_LEDGER)
        entry = pd.DataFrame(read_events(ENTRY_FREEZE_LEDGER))
    else:
        entry = pd.DataFrame()
    hist = load_history(HISTORY)
    current = load_current_snapshot(CURRENT_SNAPSHOT)
    candidate_panel = build_freeze(entry, hist, current, as_of, freeze_at=utc_now())
    existing_events = read_events(FREEZE_LEDGER) if FREEZE_LEDGER.exists() else []
    candidate_panel = exclude_existing_frozen_batches(candidate_panel, existing_events)
    persistable = seal_source_snapshots(terminal_freezes(candidate_panel, "benchmark_entry_freeze_status"))
    events, appended_count = persist_immutable_freezes(
        FREEZE_LEDGER,
        persistable.fillna("").to_dict("records"),
        freeze_kind="benchmark_entry_freeze",
        key_fields=["cohort_id", "cohort_manifest_hash", "batch_id", "planned_entry_date", "industry_code"],
        status_field="benchmark_entry_freeze_status",
    )
    global_panel = pd.DataFrame(events)
    if global_panel.empty:
        global_panel = candidate_panel.iloc[0:0].copy()
    active = validated_active_cohort()
    panel = active_cohort_frame(global_panel, active)
    active_entry = active_cohort_frame(entry, active)
    checks = build_checks(panel, active_entry)
    summary = build_summary(
        panel,
        checks,
        as_of,
        appended_count=appended_count,
        freeze_ledger_head_hash=event_chain_head(FREEZE_LEDGER) if FREEZE_LEDGER.exists() else "",
        global_panel=global_panel,
        pending_attempt_count=int(len(candidate_panel) - len(persistable)),
        active_cohort=active,
    )
    write_outputs(
        summary,
        panel,
        checks,
        global_panel=global_panel,
        pending_attempts=candidate_panel[~candidate_panel.index.isin(persistable.index)],
    )
    print(f"output_dir={OUT}")
    print(f"benchmark_frozen_rows={summary['benchmark_frozen_rows']}")
    print(f"goal_ready={summary['goal_ready']}")


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}) if path.exists() else pd.DataFrame()


def freeze_batch_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("cohort_id", "")),
        str(row.get("cohort_manifest_hash", "")),
        str(row.get("batch_id", "")),
        str(row.get("planned_entry_date", "")),
    )


def exclude_existing_frozen_batches(panel: pd.DataFrame, existing_events: list[dict[str, Any]]) -> pd.DataFrame:
    if panel.empty or not existing_events:
        return panel
    frozen_batches = {freeze_batch_key(row) for row in existing_events}
    mask = panel.fillna("").apply(lambda row: freeze_batch_key(row.to_dict()) not in frozen_batches, axis=1)
    return panel.loc[mask].copy()


TERMINAL_FREEZE_STATUSES = frozenset({"frozen_on_time", "late_backfill_excluded"})


def terminal_freezes(frame: pd.DataFrame, status_field: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame[frame[status_field].astype(str).isin(TERMINAL_FREEZE_STATUSES)].copy()


def seal_source_snapshots(frame: pd.DataFrame) -> pd.DataFrame:
    """Replace mutable benchmark price-source paths with immutable snapshots."""
    if frame.empty:
        return frame.copy()
    sealed = frame.copy()
    for source_text in sealed["freeze_source"].astype(str).unique():
        source = ROOT / source_text
        if not source.is_file():
            raise FileNotFoundError(f"benchmark freeze source is missing: {source}")
        snapshot, fingerprint = snapshot_source(source, SOURCE_SNAPSHOTS)
        mask = sealed["freeze_source"].astype(str).eq(source_text)
        sealed.loc[mask, "freeze_source"] = relative_posix(snapshot, ROOT)
        sealed.loc[mask, "source_fingerprint"] = fingerprint
    return sealed


def active_cohort_frame(frame: pd.DataFrame, active: dict[str, Any] | None) -> pd.DataFrame:
    if frame.empty or not active or active.get("freeze_passed") is not True:
        return frame.iloc[0:0].copy()
    cohort_id = str(active.get("cohort_id", ""))
    manifest_hash = str(active.get("manifest_hash", ""))
    if not cohort_id or not manifest_hash or "cohort_id" not in frame.columns or "cohort_manifest_hash" not in frame.columns:
        return frame.iloc[0:0].copy()
    return frame[
        frame["cohort_id"].astype(str).eq(cohort_id)
        & frame["cohort_manifest_hash"].astype(str).eq(manifest_hash)
    ].copy()


def normalize_panel(frame: pd.DataFrame, close_col: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["industry_code", "industry_name", "trade_date", "close_index"])
    out = frame.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    out["industry_code"] = out["industry_code"].astype(str).str.zfill(6)
    out["close_index"] = pd.to_numeric(out[close_col], errors="coerce")
    return out.dropna(subset=["close_index"])


def load_history(path: Path) -> pd.DataFrame:
    return normalize_panel(pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}), "close_index")


def load_current_snapshot(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["industry_code", "industry_name", "trade_date", "close_index"])
    return normalize_panel(pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}), "industry_close")


def build_freeze(
    entry: pd.DataFrame,
    hist: pd.DataFrame,
    current: pd.DataFrame,
    as_of: date,
    *,
    freeze_at: datetime | None = None,
) -> pd.DataFrame:
    columns = [
        "as_of_date", "batch_id", "planned_entry_date", "actual_entry_date", "industry_code", "industry_name",
        "entry_close_index", "benchmark_entry_freeze_status", "entry_date_exact",
        "late_backfill_excluded", "freeze_at_utc", "freeze_deadline_utc",
        "benchmark_universe_count", "benchmark_universe_fingerprint", "cohort_id",
        "cohort_manifest_hash", "freeze_source", "source_fingerprint", "research_boundary",
    ]
    if entry.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    freeze_at = freeze_at or utc_now()
    freeze_at_text = iso_utc(freeze_at)
    batch_fields = ["batch_id", "planned_entry_date", "cohort_id", "cohort_manifest_hash"]
    batch_source = entry.copy()
    for field in batch_fields:
        if field not in batch_source.columns:
            batch_source[field] = ""
    batches = batch_source[batch_fields].drop_duplicates().fillna("").to_dict("records")
    for batch in batches:
        planned = parse_date(batch.get("planned_entry_date", ""))
        if not planned:
            continue
        frame = exact_entry_rows(hist, planned)
        source = HISTORY
        if frame.empty or len(frame) < 100:
            frame = exact_entry_rows(current, planned)
            source = CURRENT_SNAPSHOT
        probe = {
            "planned_entry_date": planned.isoformat(),
            "actual_entry_date": planned.isoformat(),
            "as_of_date": as_of.isoformat(),
            "freeze_at_utc": freeze_at_text,
        }
        timing_status = freeze_timing_status(probe)
        on_time = freeze_recorded_on_time(probe)
        status = (
            "frozen_on_time" if on_time
            else "freeze_window_pending" if timing_status == "early_pending"
            else "late_backfill_excluded"
        )
        payload_rows = [
            {
                "industry_code": str(item.get("industry_code", "")).zfill(6),
                "actual_entry_date": item["trade_date"].isoformat(),
                "entry_close_index": f"{float(item['close_index']):.10f}",
            }
            for item in frame.sort_values("industry_code").to_dict("records")
        ]
        universe_fingerprint = csv_fingerprint(
            payload_rows,
            fieldnames=["industry_code", "actual_entry_date", "entry_close_index"],
            sort_rows_by=["industry_code"],
        )
        cohort_id = str(batch.get("cohort_id", ""))
        cohort_hash = str(batch.get("cohort_manifest_hash", ""))
        if len(frame) < 100:
            late_failure = timing_status == "late_excluded"
            failure_status = "late_backfill_excluded" if late_failure else "insufficient_benchmark_universe_pending"
            selected_rows = entry[
                entry["cohort_id"].astype(str).eq(cohort_id)
                & entry["cohort_manifest_hash"].astype(str).eq(cohort_hash)
                & entry["batch_id"].astype(str).eq(str(batch.get("batch_id", "")))
                & entry["planned_entry_date"].astype(str).eq(planned.isoformat())
            ].fillna("").drop_duplicates("industry_code")
            for selected in selected_rows.to_dict("records"):
                rows.append({
                    "as_of_date": as_of.isoformat(),
                    "batch_id": batch.get("batch_id", ""),
                    "planned_entry_date": planned.isoformat(),
                    "actual_entry_date": "",
                    "industry_code": str(selected.get("industry_code", "")).zfill(6),
                    "industry_name": selected.get("industry_name", ""),
                    "entry_close_index": "",
                    "benchmark_entry_freeze_status": failure_status,
                    "entry_date_exact": False,
                    "late_backfill_excluded": late_failure,
                    "freeze_at_utc": freeze_at_text,
                    "freeze_deadline_utc": iso_utc(freeze_deadline_utc(planned)),
                    "benchmark_universe_count": len(frame),
                    "benchmark_universe_fingerprint": universe_fingerprint,
                    "cohort_id": cohort_id,
                    "cohort_manifest_hash": cohort_hash,
                    "freeze_source": str(source.relative_to(ROOT)),
                    "source_fingerprint": "",
                    "research_boundary": "全行业基准不足 100 行且已错过冻结窗口时永久排除；不允许事后补齐。",
                })
            continue
        for item in frame.sort_values("industry_code").to_dict("records"):
            rows.append({
                "as_of_date": as_of.isoformat(),
                "batch_id": batch.get("batch_id", ""),
                "planned_entry_date": planned.isoformat(),
                "actual_entry_date": item["trade_date"].isoformat(),
                "industry_code": item["industry_code"],
                "industry_name": item.get("industry_name", ""),
                "entry_close_index": f"{float(item['close_index']):.10f}",
                "benchmark_entry_freeze_status": status,
                "entry_date_exact": item["trade_date"] == planned,
                "late_backfill_excluded": status == "late_backfill_excluded",
                "freeze_at_utc": freeze_at_text,
                "freeze_deadline_utc": iso_utc(freeze_deadline_utc(planned)),
                "benchmark_universe_count": len(frame),
                "benchmark_universe_fingerprint": universe_fingerprint,
                "cohort_id": cohort_id,
                "cohort_manifest_hash": cohort_hash,
                "freeze_source": str(source.relative_to(ROOT)),
                "source_fingerprint": "",
                "research_boundary": "冻结全行业基准入场点只用于未来相对收益结算；不计算未来收益，不证明强行业 alpha。",
            })
    return pd.DataFrame(rows, columns=columns)


def exact_entry_rows(frame: pd.DataFrame, planned: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    available = frame[frame["trade_date"].eq(planned)]
    return available.sort_values("industry_code").drop_duplicates("industry_code", keep="first")


def parse_date(value: object) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def build_checks(panel: pd.DataFrame, entry: pd.DataFrame | None = None) -> pd.DataFrame:
    group_fields = ["cohort_id", "cohort_manifest_hash", "batch_id", "planned_entry_date"]
    batches = int(panel[group_fields].drop_duplicates().shape[0]) if not panel.empty else 0
    min_count = int(panel.groupby(group_fields)["industry_code"].nunique().min()) if not panel.empty else 0
    exact = bool(not panel.empty and panel.get("entry_date_exact", pd.Series(dtype=bool)).astype(str).str.lower().eq("true").all())
    on_time = int(panel.get("benchmark_entry_freeze_status", pd.Series(dtype=str)).eq("frozen_on_time").sum()) if not panel.empty else 0
    late = int(panel.get("benchmark_entry_freeze_status", pd.Series(dtype=str)).eq("late_backfill_excluded").sum()) if not panel.empty else 0
    selected_present = True
    if entry is not None and not entry.empty:
        for (cohort_id, cohort_hash, batch_id, planned), group in entry.fillna("").groupby(group_fields):
            selected = set(group["industry_code"].astype(str).str.zfill(6))
            frozen = set(panel[
                panel["cohort_id"].astype(str).eq(str(cohort_id))
                & panel["cohort_manifest_hash"].astype(str).eq(str(cohort_hash))
                & panel["batch_id"].astype(str).eq(str(batch_id))
                & panel["planned_entry_date"].astype(str).eq(str(planned))
            ]["industry_code"].astype(str).str.zfill(6))
            selected_present = selected_present and selected.issubset(frozen)
    return pd.DataFrame([
        check("benchmark_batches_exist", "pass" if batches else "pending", f"batches={batches}", "必须存在待观察批次。"),
        check("benchmark_universe_frozen", "pass" if min_count >= 100 else "fail", f"min_industry_count={min_count}", "每个批次应冻结足够多的申万二级行业作为等权基准。"),
        check("benchmark_entry_date_exact", "pass" if exact else "fail", f"all_exact={exact}", "全行业必须使用精确计划入场日，禁止逐行业向后滚动。"),
        check("benchmark_contains_candidates", "pass" if selected_present else "fail", f"selected_present={selected_present}", "冻结基准必须包含本批次全部候选行业。"),
        check("benchmark_frozen_on_time", "pass" if len(panel) and on_time == len(panel) else "fail", f"on_time={on_time}; rows={len(panel)}", "基准必须在计划入场日冻结窗口内生成。"),
        check("late_backfill_excluded", "blocked" if late else "pass", f"late={late}", "入场日后补录的基准永久排除。"),
        check("goal_claim", "fail", "benchmark entry only", "冻结基准入场点不等于已验证强反弹行业。"),
    ])


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_summary(
    panel: pd.DataFrame,
    checks: pd.DataFrame,
    as_of: date,
    *,
    appended_count: int = 0,
    freeze_ledger_head_hash: str = "",
    global_panel: pd.DataFrame | None = None,
    pending_attempt_count: int = 0,
    active_cohort: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    batch_count = int(panel[["cohort_id", "cohort_manifest_hash", "batch_id", "planned_entry_date"]].drop_duplicates().shape[0]) if not panel.empty else 0
    frozen_rows = int(panel.get("benchmark_entry_freeze_status", pd.Series(dtype=str)).eq("frozen_on_time").sum()) if not panel.empty else 0
    active = dict(active_cohort or {})
    return {
        "version": "5.34.3",
        "policy_id": "fund_flow_benchmark_entry_freeze_v5_34",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "benchmark_batch_count": batch_count,
        "benchmark_frozen_rows": frozen_rows,
        "benchmark_rows_total": int(len(panel)),
        "late_backfill_excluded_count": int(panel.get("benchmark_entry_freeze_status", pd.Series(dtype=str)).eq("late_backfill_excluded").sum()) if not panel.empty else 0,
        "active_benchmark_rows": int(len(panel)),
        "active_cohort_id": str(active.get("cohort_id", "")),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", "")),
        "active_cohort_freeze_passed": active.get("freeze_passed") is True,
        "global_history_rows": int(len(global_panel)) if global_panel is not None else int(len(panel)),
        "global_late_backfill_excluded_count": int(global_panel.get("benchmark_entry_freeze_status", pd.Series(dtype=str)).eq("late_backfill_excluded").sum()) if global_panel is not None and not global_panel.empty else 0,
        "pending_attempt_count": pending_attempt_count,
        "freeze_events_appended": appended_count,
        "freeze_ledger_head_hash": freeze_ledger_head_hash,
        "fail_count": int(checks["status"].eq("fail").sum()),
        "pending_count": int(checks["status"].eq("pending").sum()),
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_benchmark_entry_frozen" if frozen_rows else ("research_only_benchmark_entry_late_excluded" if len(panel) else "research_only_benchmark_entry_missing"),
        "final_verdict": "V5.34 只冻结资金流前推样本的全行业基准入场点；未到退出结算前不能声称找到强反弹行业。",
    }


def write_outputs(
    summary: dict[str, Any],
    panel: pd.DataFrame,
    checks: pd.DataFrame,
    *,
    global_panel: pd.DataFrame | None = None,
    pending_attempts: pd.DataFrame | None = None,
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    atomic_write_csv(OUT / "top_candidates.csv", panel.fillna("").to_dict("records"), fieldnames=list(panel.columns))
    atomic_write_json(OUT / "run_summary.json", summary)
    atomic_write_text(OUT / "report.md", render_report(summary, panel, checks))
    atomic_write_csv(DEBUG / "benchmark_entry_panel.csv", panel.fillna("").to_dict("records"), fieldnames=list(panel.columns))
    atomic_write_csv(DEBUG / "benchmark_entry_checks.csv", checks.fillna("").to_dict("records"), fieldnames=list(checks.columns))
    history = global_panel if global_panel is not None else panel
    pending = pending_attempts if pending_attempts is not None else panel.iloc[0:0]
    atomic_write_csv(DEBUG / "global_benchmark_entry_history.csv", history.fillna("").to_dict("records"), fieldnames=list(history.columns))
    atomic_write_csv(DEBUG / "pending_benchmark_freeze_attempts.csv", pending.fillna("").to_dict("records"), fieldnames=list(pending.columns))


def render_report(summary: dict[str, Any], panel: pd.DataFrame, checks: pd.DataFrame) -> str:
    preview = panel.head(12) if len(panel) else panel
    return "\n".join([
        "# V5.34 资金流前推全行业基准入场点冻结",
        "",
        summary["final_verdict"],
        "",
        f"- as-of 日期：{summary['as_of_date']}",
        f"- 批次数：{summary['benchmark_batch_count']}",
        f"- 已冻结基准行业行数：{summary['benchmark_frozen_rows']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 基准冻结样例",
        "",
        preview.to_markdown(index=False) if len(preview) else "无冻结样本。",
        "",
        "## 检查",
        "",
        checks.to_markdown(index=False),
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    entry = pd.DataFrame([{
        "batch_id": "b", "planned_entry_date": "2026-01-02", "cohort_id": "c1",
        "cohort_manifest_hash": "h1", "industry_code": "000001", "industry_name": "A",
    }])
    hist = pd.DataFrame({"trade_date": [date(2026, 1, 1)], "industry_code": ["000001"], "industry_name": ["A"], "close_index": [99.0]})
    current = pd.DataFrame({
        "trade_date": [date(2026, 1, 2)] * 100,
        "industry_code": [f"{index:06d}" for index in range(1, 101)],
        "industry_name": [f"I{index}" for index in range(1, 101)],
        "close_index": [float(99 + index) for index in range(1, 101)],
    })
    panel = build_freeze(entry, hist, current, date(2026, 1, 2), freeze_at=datetime.fromisoformat("2026-01-02T07:30:00+00:00"))
    assert len(panel) == 100
    assert panel["actual_entry_date"].nunique() == 1
    assert panel.loc[panel["industry_code"].eq("000001"), "entry_close_index"].iloc[0] == "100.0000000000"
    assert panel["benchmark_entry_freeze_status"].eq("frozen_on_time").all()
    assert panel["cohort_id"].eq("c1").all()
    assert panel["cohort_manifest_hash"].eq("h1").all()
    expanded = pd.concat([panel, panel.iloc[[0]].assign(industry_code="999999")], ignore_index=True)
    assert exclude_existing_frozen_batches(expanded, panel.to_dict("records")).empty
    late = build_freeze(entry, hist, current, date(2026, 1, 5), freeze_at=datetime.fromisoformat("2026-01-05T07:30:00+00:00"))
    assert late["benchmark_entry_freeze_status"].eq("late_backfill_excluded").all()
    missing_late = build_freeze(entry, hist, current.iloc[0:0], date(2026, 1, 5), freeze_at=datetime.fromisoformat("2026-01-05T07:30:00+00:00"))
    assert len(missing_late) == 1
    assert missing_late["benchmark_entry_freeze_status"].eq("late_backfill_excluded").all()
    assert int(missing_late["benchmark_universe_count"].iloc[0]) == 0
    early = build_freeze(entry, hist, current, date(2026, 1, 2), freeze_at=datetime.fromisoformat("2026-01-02T06:00:00+00:00"))
    assert early["benchmark_entry_freeze_status"].eq("freeze_window_pending").all()
    assert terminal_freezes(early, "benchmark_entry_freeze_status").empty
    print("self_check=pass")


if __name__ == "__main__":
    main()
