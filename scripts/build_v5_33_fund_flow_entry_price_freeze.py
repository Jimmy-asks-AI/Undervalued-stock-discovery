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
    parse_timestamp,
    persist_immutable_freezes,
    relative_posix,
    snapshot_source,
    materialize_observations,
    read_events,
    utc_now,
    verify_ledger_checkpoint,
)
from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from research_integrity import atomic_write_csv, atomic_write_json, atomic_write_text, file_sha256, json_fingerprint


ROOT = Path(__file__).resolve().parents[1]
HOLDING = ROOT / "outputs" / "audit" / "fund_flow_holding_observation_v5_32" / "top_candidates.csv"
OBSERVATION_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
HISTORY = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"
CURRENT_SNAPSHOT = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "raw_industry_panel.csv"
FREEZE_LEDGER = ROOT / "logs" / "v5_33_fund_flow_entry_price_freeze.jsonl"
SOURCE_SNAPSHOTS = ROOT / "logs" / "v5_33_fund_flow_entry_price_sources"
OUT = ROOT / "outputs" / "audit" / "fund_flow_entry_price_freeze_v5_33"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.33 freeze entry prices for fund-flow forward observations.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    as_of = date.fromisoformat(args.as_of_date)
    if as_of > date.today():
        parser.error(f"--as-of-date {args.as_of_date} is in the future; entry price freeze must use current or past dates.")

    if OBSERVATION_LEDGER.exists():
        verify_ledger_checkpoint(OBSERVATION_LEDGER)
        holding = pd.DataFrame(materialize_observations(read_events(OBSERVATION_LEDGER)))
    else:
        holding = pd.DataFrame()
    hist = load_history(HISTORY)
    current = load_current_snapshot(CURRENT_SNAPSHOT)
    candidate_freezes = build_freeze(holding, hist, current, as_of, freeze_at=utc_now())
    persistable = seal_source_snapshots(terminal_freezes(candidate_freezes, "entry_price_freeze_status"))
    events, appended_count = persist_immutable_freezes(
        FREEZE_LEDGER,
        persistable.fillna("").to_dict("records"),
        freeze_kind="candidate_entry_freeze",
        key_fields=["cohort_id", "cohort_manifest_hash", "batch_id", "observation_id", "industry_code", "planned_entry_date"],
        status_field="entry_price_freeze_status",
    )
    global_freeze = pd.DataFrame(events)
    if global_freeze.empty:
        global_freeze = candidate_freezes.iloc[0:0].copy()
    active = validated_active_cohort()
    freeze = active_cohort_frame(global_freeze, active)
    checks = build_checks(freeze)
    summary = build_summary(
        freeze,
        checks,
        as_of,
        appended_count=appended_count,
        freeze_ledger_head_hash=event_chain_head(FREEZE_LEDGER) if FREEZE_LEDGER.exists() else "",
        global_freeze=global_freeze,
        pending_attempt_count=int(len(candidate_freezes) - len(persistable)),
        active_cohort=active,
    )
    write_outputs(summary, freeze, checks, global_freeze=global_freeze, pending_attempts=candidate_freezes[~candidate_freezes.index.isin(persistable.index)])
    print(f"output_dir={OUT}")
    print(f"frozen_entry_count={summary['frozen_entry_count']}")
    print(f"goal_ready={summary['goal_ready']}")


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str}) if path.exists() else pd.DataFrame()


def load_history(path: Path) -> pd.DataFrame:
    hist = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    hist["trade_date"] = pd.to_datetime(hist["trade_date"]).dt.date
    hist["industry_code"] = hist["industry_code"].str.zfill(6)
    hist["close_index"] = pd.to_numeric(hist["close_index"], errors="coerce")
    return hist.dropna(subset=["close_index"])


def load_current_snapshot(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["industry_code", "industry_name", "trade_date", "close_index"])
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    frame["industry_code"] = frame["industry_code"].str.zfill(6)
    frame["close_index"] = pd.to_numeric(frame["industry_close"], errors="coerce")
    return frame.dropna(subset=["close_index"])


def build_freeze(
    holding: pd.DataFrame,
    hist: pd.DataFrame,
    current: pd.DataFrame,
    as_of: date,
    *,
    freeze_at: datetime | None = None,
) -> pd.DataFrame:
    columns = [
        "as_of_date", "batch_id", "industry_code", "industry_name", "signal_date",
        "planned_entry_date", "planned_exit_date", "actual_entry_date", "entry_close_index",
        "entry_price_freeze_status", "entry_date_exact", "late_backfill_excluded",
        "freeze_at_utc", "freeze_deadline_utc", "source_fingerprint", "observation_id",
        "cohort_id", "cohort_manifest_hash", "holding_observation_status", "freeze_source",
        "research_boundary",
    ]
    if holding.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    freeze_at = freeze_at or utc_now()
    freeze_at_text = iso_utc(freeze_at)
    for item in holding.fillna("").to_dict("records"):
        code = str(item.get("industry_code", "")).zfill(6)
        planned = parse_date(item.get("planned_entry_date", ""))
        frame = hist[hist["industry_code"].eq(code)]
        if planned:
            frame = frame[frame["trade_date"].eq(planned)].sort_values("trade_date")
        source = HISTORY
        if planned and frame.empty:
            frame = current[current["industry_code"].eq(code) & current["trade_date"].eq(planned)].sort_values("trade_date")
            source = CURRENT_SNAPSHOT
        if planned and not frame.empty:
            first = frame.iloc[0]
            actual_entry = first["trade_date"].isoformat()
            close_index = f"{float(first['close_index']):.10f}"
            probe = {
                "planned_entry_date": planned.isoformat(),
                "actual_entry_date": actual_entry,
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
        else:
            on_time = False
            timing_status = freeze_timing_status({
                "planned_entry_date": planned.isoformat() if planned else "",
                "as_of_date": as_of.isoformat(),
                "freeze_at_utc": freeze_at_text,
            })
            status = "late_backfill_excluded" if timing_status == "late_excluded" else "missing_exact_entry_price"
            actual_entry = ""
            close_index = ""
        source_payload = {
            "industry_code": code,
            "actual_entry_date": actual_entry,
            "entry_close_index": close_index,
            "freeze_source": str(source.relative_to(ROOT)),
        }
        rows.append({
            "as_of_date": as_of.isoformat(),
            "batch_id": item.get("batch_id", ""),
            "industry_code": code,
            "industry_name": item.get("industry_name", ""),
            "signal_date": item.get("signal_date", ""),
            "planned_entry_date": item.get("planned_entry_date", ""),
            "planned_exit_date": item.get("planned_exit_date", ""),
            "actual_entry_date": actual_entry,
            "entry_close_index": close_index,
            "entry_price_freeze_status": status,
            "entry_date_exact": bool(planned and actual_entry == planned.isoformat()),
            "late_backfill_excluded": status == "late_backfill_excluded",
            "freeze_at_utc": freeze_at_text,
            "freeze_deadline_utc": iso_utc(freeze_deadline_utc(planned)) if planned else "",
            "source_fingerprint": json_fingerprint(source_payload),
            "observation_id": item.get("observation_id", ""),
            "cohort_id": item.get("cohort_id", ""),
            "cohort_manifest_hash": item.get("cohort_manifest_hash", ""),
            "holding_observation_status": item.get("holding_observation_status", ""),
            "freeze_source": str(source.relative_to(ROOT)),
            "research_boundary": "冻结入场价只固定前推收益起点；不计算未来收益，不生成交易指令，不证明强行业 alpha。",
        })
    return pd.DataFrame(rows, columns=columns)


TERMINAL_FREEZE_STATUSES = frozenset({"frozen_on_time", "late_backfill_excluded"})


def terminal_freezes(frame: pd.DataFrame, status_field: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    return frame[frame[status_field].astype(str).isin(TERMINAL_FREEZE_STATUSES)].copy()


def seal_source_snapshots(frame: pd.DataFrame) -> pd.DataFrame:
    """Replace mutable price-source paths with content-addressed source snapshots."""
    if frame.empty:
        return frame.copy()
    sealed = frame.copy()
    for source_text in sealed["freeze_source"].astype(str).unique():
        source = ROOT / source_text
        if not source.is_file():
            raise FileNotFoundError(f"entry freeze source is missing: {source}")
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


def parse_date(value: object) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def build_checks(freeze: pd.DataFrame) -> pd.DataFrame:
    frozen = int(freeze["entry_price_freeze_status"].eq("frozen_on_time").sum()) if not freeze.empty else 0
    late = int(freeze["entry_price_freeze_status"].eq("late_backfill_excluded").sum()) if not freeze.empty else 0
    exact = int(freeze.get("entry_date_exact", pd.Series(dtype=bool)).astype(str).str.lower().eq("true").sum()) if not freeze.empty else 0
    return pd.DataFrame([
        check("freeze_rows_exist", "pass" if len(freeze) else "pending", f"rows={len(freeze)}", "必须有持有观察样本。"),
        check("all_entry_dates_exact", "pass" if len(freeze) and exact == len(freeze) else "fail", f"exact={exact}; rows={len(freeze)}", "只接受计划入场日的精确价格，禁止向后寻找。"),
        check("all_entry_prices_frozen_on_time", "pass" if len(freeze) and frozen == len(freeze) else "fail", f"frozen_on_time={frozen}; rows={len(freeze)}", "候选价必须在计划入场日收盘后的冻结窗口内生成。"),
        check("late_backfill_excluded", "blocked" if late else "pass", f"late={late}", "入场日后补录的候选价永久排除。"),
        check("goal_claim", "fail", "entry prices only", "冻结入场价不等于已验证强反弹行业。"),
    ])


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_summary(
    freeze: pd.DataFrame,
    checks: pd.DataFrame,
    as_of: date,
    *,
    appended_count: int = 0,
    freeze_ledger_head_hash: str = "",
    global_freeze: pd.DataFrame | None = None,
    pending_attempt_count: int = 0,
    active_cohort: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    frozen = int(freeze["entry_price_freeze_status"].eq("frozen_on_time").sum()) if not freeze.empty else 0
    late = int(freeze["entry_price_freeze_status"].eq("late_backfill_excluded").sum()) if not freeze.empty else 0
    active = dict(active_cohort or {})
    return {
        "version": "5.33.3",
        "policy_id": "fund_flow_entry_price_freeze_v5_33",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "freeze_rows": int(len(freeze)),
        "frozen_entry_count": frozen,
        "late_backfill_excluded_count": late,
        "active_freeze_rows": int(len(freeze)),
        "active_cohort_id": str(active.get("cohort_id", "")),
        "active_cohort_manifest_hash": str(active.get("manifest_hash", "")),
        "active_cohort_freeze_passed": active.get("freeze_passed") is True,
        "global_history_rows": int(len(global_freeze)) if global_freeze is not None else int(len(freeze)),
        "global_late_backfill_excluded_count": int(global_freeze["entry_price_freeze_status"].eq("late_backfill_excluded").sum()) if global_freeze is not None and not global_freeze.empty else late,
        "pending_attempt_count": pending_attempt_count,
        "freeze_events_appended": appended_count,
        "freeze_ledger_head_hash": freeze_ledger_head_hash,
        "fail_count": int(checks["status"].eq("fail").sum()),
        "pending_count": int(checks["status"].eq("pending").sum()),
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_entry_prices_frozen" if len(freeze) and frozen == len(freeze) else "research_only_entry_price_freeze_incomplete",
        "final_verdict": "V5.33 只冻结资金流前推样本的入场日行业指数点位；未到退出结算前不能声称找到强反弹行业。",
    }


def write_outputs(
    summary: dict[str, Any],
    freeze: pd.DataFrame,
    checks: pd.DataFrame,
    *,
    global_freeze: pd.DataFrame | None = None,
    pending_attempts: pd.DataFrame | None = None,
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    atomic_write_csv(OUT / "top_candidates.csv", freeze.fillna("").to_dict("records"), fieldnames=list(freeze.columns))
    atomic_write_json(OUT / "run_summary.json", summary)
    atomic_write_text(OUT / "report.md", render_report(summary, freeze, checks))
    atomic_write_csv(DEBUG / "entry_price_freeze.csv", freeze.fillna("").to_dict("records"), fieldnames=list(freeze.columns))
    atomic_write_csv(DEBUG / "entry_price_freeze_checks.csv", checks.fillna("").to_dict("records"), fieldnames=list(checks.columns))
    history = global_freeze if global_freeze is not None else freeze
    pending = pending_attempts if pending_attempts is not None else freeze.iloc[0:0]
    atomic_write_csv(DEBUG / "global_entry_price_freeze_history.csv", history.fillna("").to_dict("records"), fieldnames=list(history.columns))
    atomic_write_csv(DEBUG / "pending_entry_price_freeze_attempts.csv", pending.fillna("").to_dict("records"), fieldnames=list(pending.columns))


def render_report(summary: dict[str, Any], freeze: pd.DataFrame, checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.33 资金流前推入场价格冻结",
        "",
        summary["final_verdict"],
        "",
        f"- as-of 日期：{summary['as_of_date']}",
        f"- 冻结行数：{summary['freeze_rows']}",
        f"- 已冻结入场价：{summary['frozen_entry_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 入场价格冻结表",
        "",
        freeze.to_markdown(index=False) if len(freeze) else "无冻结样本。",
        "",
        "## 检查",
        "",
        checks.to_markdown(index=False),
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    holding = pd.DataFrame([{"batch_id": "b", "industry_code": "1", "industry_name": "A", "planned_entry_date": "2026-01-02", "planned_exit_date": "2026-01-10"}])
    hist = pd.DataFrame({"trade_date": [date(2026, 1, 1)], "industry_code": ["000001"], "industry_name": ["A"], "close_index": [99.0]})
    current = pd.DataFrame({"trade_date": [date(2026, 1, 2)], "industry_code": ["000001"], "industry_name": ["A"], "close_index": [100.0]})
    freeze = build_freeze(holding, hist, current, date(2026, 1, 2), freeze_at=datetime.fromisoformat("2026-01-02T07:30:00+00:00"))
    assert freeze["entry_price_freeze_status"].iloc[0] == "frozen_on_time"
    assert freeze["actual_entry_date"].iloc[0] == "2026-01-02"
    assert float(freeze["entry_close_index"].iloc[0]) == 100.0
    checks = build_checks(freeze)
    assert checks[checks["check"].eq("all_entry_prices_frozen_on_time")]["status"].iloc[0] == "pass"
    late = build_freeze(holding, hist, current, date(2026, 1, 5), freeze_at=datetime.fromisoformat("2026-01-05T07:30:00+00:00"))
    assert late["entry_price_freeze_status"].iloc[0] == "late_backfill_excluded"
    missing_late = build_freeze(holding, hist, current.iloc[0:0], date(2026, 1, 5), freeze_at=datetime.fromisoformat("2026-01-05T07:30:00+00:00"))
    assert missing_late["entry_price_freeze_status"].iloc[0] == "late_backfill_excluded"
    assert missing_late["entry_close_index"].iloc[0] == ""
    early = build_freeze(holding, hist, current, date(2026, 1, 2), freeze_at=datetime.fromisoformat("2026-01-02T06:00:00+00:00"))
    assert early["entry_price_freeze_status"].iloc[0] == "freeze_window_pending"
    assert terminal_freezes(early, "entry_price_freeze_status").empty
    print("self_check=pass")


if __name__ == "__main__":
    main()


