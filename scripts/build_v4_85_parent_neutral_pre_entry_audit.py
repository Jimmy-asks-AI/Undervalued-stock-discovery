#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_parent_neutral_forward_v4_86 as v486


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "logs" / "v4_85_parent_neutral_forward_ledger.csv"
V486_CANDIDATES = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_forward_v4_86" / "top_candidates.csv"
V486_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_forward_v4_86" / "run_summary.json"
OUT = ROOT / "outputs" / "industry_rebound_leader_pre_entry_audit_v4_88"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.88 pre-entry consistency audit for frozen V4.85 parent-neutral rule.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    as_of = date.fromisoformat(args.as_of_date)
    ledger = read_csv(LEDGER)
    frozen_candidates = read_csv(V486_CANDIDATES)
    v471 = v486.read_v471_summary()
    regenerated = v486.select_current_candidates(v486.build_current_opportunity(v471))
    checks = build_checks(as_of, v471, ledger, frozen_candidates, regenerated)
    candidate_compare = compare_candidates(frozen_candidates, regenerated, ledger)
    action_plan = build_action_plan(as_of, v471, checks)
    summary = build_summary(checks, candidate_compare, action_plan, v471, as_of)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    checks.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, checks, action_plan), encoding="utf-8")
    checks.to_csv(DEBUG / "pre_entry_checks.csv", index=False, encoding="utf-8-sig")
    candidate_compare.to_csv(DEBUG / "candidate_consistency.csv", index=False, encoding="utf-8-sig")
    action_plan.to_csv(DEBUG / "pre_entry_action_plan.csv", index=False, encoding="utf-8-sig")
    regenerated.to_csv(DEBUG / "regenerated_current_candidates.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"pre_entry_status={summary['pre_entry_status']}")
    print(f"fail_count={summary['fail_count']}")


def build_checks(
    as_of: date,
    v471: dict[str, object],
    ledger: pd.DataFrame,
    frozen: pd.DataFrame,
    regenerated: pd.DataFrame,
) -> pd.DataFrame:
    planned_entry = str(v471.get("planned_entry_date", ""))
    planned_exit = str(v471.get("planned_exit_date", ""))
    signal_date = str(v471.get("latest_panel_date", ""))
    planned_entry_date = parse_iso_date(planned_entry)
    planned_exit_date = parse_iso_date(planned_exit)
    checks = [
        check("window", "latest_signal_triggered", bool(v471.get("latest_signal_triggered", False)), "== true", bool(v471.get("latest_signal_triggered", False)), "反弹窗口仍需在冻结包中有明确触发记录。"),
        check("timing", "as_of_before_or_on_entry", as_of.isoformat(), f"<= {planned_entry}", bool(planned_entry_date and as_of <= planned_entry_date), "当前审计必须发生在计划入场日前或当天。"),
        check("timing", "planned_exit_after_entry", planned_exit, f"> {planned_entry}", bool(planned_entry_date and planned_exit_date and planned_exit_date > planned_entry_date), "退出日必须晚于入场日。"),
        check("candidate", "candidate_count", len(frozen), "== 10", len(frozen) == 10, "冻结候选数量必须等于规则 Top10。"),
        check("candidate", "regenerated_candidate_count", len(regenerated), "== 10", len(regenerated) == 10, "用当前输入重算仍应得到 10 个候选。"),
        check("candidate", "candidate_set_stable", candidate_codes(frozen), "== regenerated", candidate_codes(frozen) == candidate_codes(regenerated), "冻结候选和当前重算候选必须一致。"),
        check("candidate", "ledger_matches_candidates", candidate_codes(ledger), "== frozen", candidate_codes(ledger) == candidate_codes(frozen), "前推账本必须和冻结候选一致。"),
        check("parent_cap", "parent_cap1_satisfied", max_parent_count(frozen), "<= 1", max_parent_count(frozen) <= 1, "每个父行业最多一个二级行业。"),
        check("research_boundary", "auto_execution_disabled", auto_execution_values(frozen), "all false", auto_execution_disabled(frozen), "冻结候选不能自动执行。"),
        check("asof", "feature_date_not_after_signal", max_date(frozen, "feature_date"), f"<= {signal_date}", max_date_ok(frozen, "feature_date", signal_date), "特征日期不能晚于信号日。"),
        check("asof", "price_date_not_after_signal", max_date(frozen, "price_date"), f"<= {signal_date}", max_date_ok(frozen, "price_date", signal_date), "价格日期不能晚于信号日。"),
    ]
    return pd.DataFrame(checks)


def compare_candidates(frozen: pd.DataFrame, regenerated: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for source_name, frame in [("frozen", frozen), ("regenerated", regenerated), ("ledger", ledger)]:
        for row in frame.to_dict("records"):
            rows.append({
                "source": source_name,
                "rank": row.get("rank", ""),
                "industry_code": str(row.get("industry_code", "")).zfill(6),
                "industry_name": row.get("industry_name", ""),
                "parent_industry": row.get("parent_industry", ""),
                "selection_score": row.get("selection_score", ""),
            })
    return pd.DataFrame(rows)


def build_action_plan(as_of: date, v471: dict[str, object], checks: pd.DataFrame) -> pd.DataFrame:
    planned_entry = str(v471.get("planned_entry_date", ""))
    planned_entry_date = parse_iso_date(planned_entry)
    entry_due = bool(planned_entry_date and as_of >= planned_entry_date)
    fail_count = int(checks["status"].eq("fail").sum())
    return pd.DataFrame([
        {
            "priority": "P0",
            "action": "entry_day_refresh_required",
            "due_date": planned_entry,
            "status": "due_now" if entry_due else "pending",
            "command": f"python .\\scripts\\run_v4_71_live_refresh.py --trade-date {planned_entry}",
            "decision_rule": "入场日前/当天必须重新刷新；若候选漂移、窗口失效或审计失败，前推样本标记为 skipped，不得视为有效进入样本。",
        },
        {
            "priority": "P0",
            "action": "manual_research_only_boundary",
            "due_date": as_of.isoformat(),
            "status": "pass" if fail_count == 0 else "blocked",
            "command": "",
            "decision_rule": "无论审计是否通过，auto_execution_allowed 必须保持 false。",
        },
    ])


def build_summary(
    checks: pd.DataFrame,
    candidate_compare: pd.DataFrame,
    action_plan: pd.DataFrame,
    v471: dict[str, object],
    as_of: date,
) -> dict[str, object]:
    fail_count = int(checks["status"].eq("fail").sum())
    pass_count = int(checks["status"].eq("pass").sum())
    entry_status = "pre_entry_consistent" if fail_count == 0 else "pre_entry_blocked"
    return {
        "version": "4.88.0",
        "policy_id": "industry_rebound_leader_pre_entry_audit_v4_88",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "planned_entry_date": v471.get("planned_entry_date", ""),
        "planned_exit_date": v471.get("planned_exit_date", ""),
        "check_count": int(len(checks)),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "candidate_compare_rows": int(len(candidate_compare)),
        "pre_entry_status": entry_status,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "V4.85 冻结候选入场前一致性审计通过；仍需入场日刷新，且只允许 research_only 前推观察。" if fail_count == 0 else "V4.85 冻结候选入场前一致性审计存在失败项；不得进入有效前推样本。",
    }


def render_report(summary: dict[str, object], checks: pd.DataFrame, action_plan: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.88 父行业 cap1 入场前一致性审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 当前状态",
        "",
        f"- 审计日期：{summary['as_of_date']}",
        f"- 计划入场日：{summary['planned_entry_date']}",
        f"- 计划退出日：{summary['planned_exit_date']}",
        f"- 通过项：{summary['pass_count']}",
        f"- 失败项：{summary['fail_count']}",
        f"- 入场前状态：`{summary['pre_entry_status']}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 审计项",
        "",
        table(checks),
        "",
        "## 入场前动作",
        "",
        table(action_plan),
        "",
        "## 研究边界",
        "",
        "V4.88 只确认冻结候选、账本和当前重算结果是否一致；它不结算未来收益，也不把候选升级为交易信号。",
    ])


def check(dimension: str, item: str, current: object, required: str, ok: bool, note: str) -> dict[str, object]:
    return {
        "dimension": dimension,
        "item": item,
        "current": current,
        "required": required,
        "status": "pass" if ok else "fail",
        "note": note,
    }


def candidate_codes(frame: pd.DataFrame) -> str:
    if frame.empty or "industry_code" not in frame.columns:
        return ""
    return "|".join(frame.sort_values("rank" if "rank" in frame.columns else "industry_code")["industry_code"].astype(str).str.zfill(6).tolist())


def max_parent_count(frame: pd.DataFrame) -> int:
    if frame.empty or "parent_industry" not in frame.columns:
        return 0
    return int(frame["parent_industry"].value_counts().max())


def auto_execution_values(frame: pd.DataFrame) -> str:
    if "auto_execution_allowed" not in frame.columns:
        return ""
    return "|".join(frame["auto_execution_allowed"].astype(str).unique().tolist())


def auto_execution_disabled(frame: pd.DataFrame) -> bool:
    if "auto_execution_allowed" not in frame.columns:
        return False
    return not frame["auto_execution_allowed"].astype(str).str.lower().isin(["true", "1", "yes"]).any()


def max_date(frame: pd.DataFrame, column: str) -> str:
    if frame.empty or column not in frame.columns:
        return ""
    values = pd.to_datetime(frame[column], errors="coerce").dropna()
    return values.max().date().isoformat() if len(values) else ""


def max_date_ok(frame: pd.DataFrame, column: str, signal_date: str) -> bool:
    value_date = parse_iso_date(max_date(frame, column))
    signal = parse_iso_date(signal_date)
    return bool(value_date and signal and value_date <= signal)


def parse_iso_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"industry_code": str})


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def self_check() -> None:
    frozen = pd.DataFrame([
        {"rank": i + 1, "industry_code": str(i + 1), "parent_industry": f"P{i + 1}", "feature_date": "2026-01-01", "price_date": "2026-01-01", "auto_execution_allowed": False}
        for i in range(10)
    ])
    regenerated = frozen.copy()
    ledger = frozen.copy()
    checks = build_checks(
        date(2026, 1, 2),
        {"planned_entry_date": "2026-01-03", "planned_exit_date": "2026-02-01", "latest_panel_date": "2026-01-01", "latest_signal_triggered": True},
        ledger,
        frozen,
        regenerated,
    )
    assert checks["status"].eq("fail").sum() == 0
    bad = frozen.copy()
    bad.loc[1, "parent_industry"] = "P1"
    bad_checks = build_checks(
        date(2026, 1, 2),
        {"planned_entry_date": "2026-01-03", "planned_exit_date": "2026-02-01", "latest_panel_date": "2026-01-01", "latest_signal_triggered": True},
        ledger,
        bad,
        regenerated,
    )
    assert "parent_cap1_satisfied" in set(bad_checks[bad_checks["status"].eq("fail")]["item"])
    print("self_check=pass")


if __name__ == "__main__":
    main()
