#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_parent_neutral_v4_85 as v485
import run_industry_rebound_leader_selection_v4_72 as v472


ROOT = Path(__file__).resolve().parents[1]
V471_SUMMARY = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "run_summary.json"
V485_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85" / "run_summary.json"
LEDGER = ROOT / "logs" / "v4_85_parent_neutral_forward_ledger.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_forward_v4_86"
DEBUG = OUT / "debug"

FROZEN_RULE = {
    "source_version": "4.85.0",
    "state_gate_variant": "deep_highvol_liq_repair",
    "selection_mode": "global_rank_parent_cap1",
    "feature": "oversold_liquidity_score",
    "top_n": 10,
    "parent_cap": 1,
}
LEDGER_FIELDS = [
    "recorded_at",
    "tracker_id",
    "policy_version",
    "policy_id",
    "policy_status",
    "decision",
    "outcome_status",
    "signal_date",
    "feature_date",
    "price_date",
    "planned_entry_date",
    "planned_exit_date",
    "industry_code",
    "industry_name",
    "parent_industry",
    "selection_mode",
    "selection_feature",
    "selection_score",
    "parent_cap",
    "rank",
    "valuation_score",
    "oversold_score",
    "turn_score",
    "liquidity_score",
    "actual_entry_date",
    "actual_exit_date",
    "realized_return",
    "benchmark_return",
    "realized_relative_return",
    "top_quintile_hit",
    "settlement_status",
    "settlement_notes",
]
CANDIDATE_FIELDS = [
    "candidate_status",
    "rank",
    "trade_date",
    "feature_date",
    "price_date",
    "industry_code",
    "industry_name",
    "parent_industry",
    "selection_mode",
    "selection_feature",
    "selection_score",
    "parent_cap",
    "valuation_score",
    "oversold_score",
    "turn_score",
    "liquidity_score",
    "return_20d",
    "return_60d",
    "return_120d",
    "drawdown_252d",
    "pe",
    "pb",
    "dividend_yield",
    "auto_execution_allowed",
    "manual_review_reason",
]
def main() -> None:
    parser = argparse.ArgumentParser(description="V4.86 frozen V4.85 parent-neutral forward observation packet.")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.audit:
        write_outputs(pd.DataFrame(), audit_ledger(read_rows(LEDGER)), read_rows(LEDGER), read_v485_summary(), read_v471_summary())
        return

    v471 = read_v471_summary()
    v485_summary = read_v485_summary()
    opportunity = build_current_opportunity(v471)
    candidates = select_current_candidates(opportunity)
    rows = build_ledger_rows(candidates, v471, v485_summary)
    append_ledger(rows)
    write_outputs(candidates, audit_ledger(read_rows(LEDGER)), rows, v485_summary, v471)
    print(f"output_dir={OUT}")
    print(f"current_candidate_count={len(candidates)}")
    print("production_ready=False")


def read_v471_summary() -> dict[str, object]:
    return json.loads(V471_SUMMARY.read_text(encoding="utf-8"))


def read_v485_summary() -> dict[str, object]:
    return json.loads(V485_SUMMARY.read_text(encoding="utf-8"))


def build_current_opportunity(v471: dict[str, object]) -> pd.DataFrame:
    features = v472.current_snapshot_features(v471)
    if features.empty:
        return features
    parent_map = v485.load_parent_map()
    out = features.copy()
    out["industry_code"] = out["industry_code"].astype(str).str.zfill(6)
    out = out.merge(parent_map, on="industry_code", how="left")
    out["parent_mapping_status"] = out["parent_industry"].notna().map({True: "pass", False: "missing_parent"})
    return out


def select_current_candidates(opportunity: pd.DataFrame) -> pd.DataFrame:
    if opportunity.empty or not bool(read_v471_summary().get("latest_signal_triggered", False)):
        return pd.DataFrame(columns=CANDIDATE_FIELDS)
    frame = opportunity.dropna(subset=[FROZEN_RULE["feature"], "parent_industry"]).sort_values(FROZEN_RULE["feature"], ascending=False)
    selected = v485.select_with_parent_cap(frame, int(FROZEN_RULE["top_n"]), int(FROZEN_RULE["parent_cap"]))
    if selected.empty:
        return pd.DataFrame(columns=CANDIDATE_FIELDS)
    selected = selected.copy().reset_index(drop=True)
    selected["rank"] = selected.index + 1
    selected["candidate_status"] = "research_only_parent_neutral_forward_observation"
    selected["selection_mode"] = FROZEN_RULE["selection_mode"]
    selected["selection_feature"] = FROZEN_RULE["feature"]
    selected["selection_score"] = pd.to_numeric(selected[FROZEN_RULE["feature"]], errors="coerce")
    selected["parent_cap"] = FROZEN_RULE["parent_cap"]
    selected["auto_execution_allowed"] = False
    selected["manual_review_reason"] = "V4.85 点估计和留一年通过，但 bootstrap 稳健性未通过；只能前推观察。"
    return selected[[column for column in CANDIDATE_FIELDS if column in selected.columns]]


def build_ledger_rows(candidates: pd.DataFrame, v471: dict[str, object], v485_summary: dict[str, object]) -> list[dict[str, str]]:
    if candidates.empty:
        return []
    now = datetime.now().isoformat(timespec="seconds")
    signal_date = str(v471.get("latest_panel_date") or v471.get("signal_date") or "")
    tracker_id = f"v4_85_parent_neutral_forward_{signal_date}"
    rows = []
    for item in candidates.to_dict("records"):
        rows.append({
            "recorded_at": now,
            "tracker_id": tracker_id,
            "policy_version": str(v485_summary.get("version", "")),
            "policy_id": "industry_rebound_leader_parent_neutral_v4_85_frozen_rule",
            "policy_status": str(v485_summary.get("best_status", "")),
            "decision": "planned_observation",
            "outcome_status": "pending_forward_observation",
            "signal_date": signal_date,
            "feature_date": str(item.get("feature_date", "")),
            "price_date": str(item.get("price_date", "")),
            "planned_entry_date": str(v471.get("planned_entry_date", "")),
            "planned_exit_date": str(v471.get("planned_exit_date", "")),
            "industry_code": str(item.get("industry_code", "")).zfill(6),
            "industry_name": str(item.get("industry_name", "")),
            "parent_industry": str(item.get("parent_industry", "")),
            "selection_mode": str(item.get("selection_mode", "")),
            "selection_feature": str(item.get("selection_feature", "")),
            "selection_score": fmt(item.get("selection_score", "")),
            "parent_cap": str(item.get("parent_cap", "")),
            "rank": str(item.get("rank", "")),
            "valuation_score": fmt(item.get("valuation_score", "")),
            "oversold_score": fmt(item.get("oversold_score", "")),
            "turn_score": fmt(item.get("turn_score", "")),
            "liquidity_score": fmt(item.get("liquidity_score", "")),
            "actual_entry_date": "",
            "actual_exit_date": "",
            "realized_return": "",
            "benchmark_return": "",
            "realized_relative_return": "",
            "top_quintile_hit": "",
            "settlement_status": "not_due",
            "settlement_notes": "冻结 V4.85 最优候选规则前推观察；未到退出日不填未来收益。",
        })
    return rows


def append_ledger(new_rows: list[dict[str, str]]) -> None:
    old = read_rows(LEDGER)
    keys = {(row["tracker_id"], row["industry_code"], row["selection_mode"], row["selection_feature"]) for row in new_rows}
    kept = [
        row for row in old
        if (row.get("tracker_id", ""), row.get("industry_code", ""), row.get("selection_mode", ""), row.get("selection_feature", "")) not in keys
    ]
    write_rows(LEDGER, kept + new_rows, LEDGER_FIELDS)


def audit_ledger(rows: list[dict[str, str]]) -> dict[str, object]:
    pending = [row for row in rows if row.get("outcome_status") == "pending_forward_observation"]
    settled = [row for row in rows if row.get("outcome_status") == "settled_forward_observation"]
    keys = [(row.get("tracker_id", ""), row.get("industry_code", ""), row.get("selection_mode", ""), row.get("selection_feature", "")) for row in rows]
    duplicate_count = len(keys) - len(set(keys))
    return {
        "version": "4.86.0",
        "policy_id": "industry_rebound_leader_parent_neutral_forward_v4_86",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ledger_rows": len(rows),
        "unique_tracker_count": len({row.get("tracker_id", "") for row in rows}),
        "pending_forward_observation_count": len(pending),
        "settled_forward_observation_count": len(settled),
        "duplicate_key_count": duplicate_count,
        "forward_sample_gate_required_settled_events": 30,
        "forward_sample_gate_current_settled_events": len({row.get("tracker_id", "") for row in settled}),
        "forward_sample_gate_status": "pass" if len({row.get("tracker_id", "") for row in settled}) >= 30 else "pending",
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "V4.85 父行业 cap1 规则已冻结并进入前推观察；当前仍不能声称已经找到稳定强反弹行业。",
    }


def write_outputs(
    candidates: pd.DataFrame,
    audit: dict[str, object],
    archived_rows: list[dict[str, str]],
    v485_summary: dict[str, object],
    v471: dict[str, object],
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    top_candidates_frame(candidates, archived_rows).to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", audit)
    (OUT / "report.md").write_text(render_report(audit, v485_summary, v471), encoding="utf-8")
    write_rows(DEBUG / "archived_forward_rows.csv", archived_rows, LEDGER_FIELDS)
    write_rows(DEBUG / "forward_ledger_audit.csv", [stringify(audit)], list(audit.keys()))
    pd.DataFrame([FROZEN_RULE]).to_csv(DEBUG / "frozen_parent_neutral_rule.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([forward_plan_row(audit, v471)]).to_csv(DEBUG / "forward_observation_plan.csv", index=False, encoding="utf-8-sig")
    opportunity = build_current_opportunity(v471)
    opportunity.to_csv(DEBUG / "current_opportunity_set.csv", index=False, encoding="utf-8-sig")


def top_candidates_frame(candidates: pd.DataFrame, archived_rows: list[dict[str, str]]) -> pd.DataFrame:
    if len(candidates.columns):
        return candidates
    if archived_rows:
        return pd.DataFrame(archived_rows[-10:])
    return pd.DataFrame(columns=CANDIDATE_FIELDS)


def forward_plan_row(audit: dict[str, object], v471: dict[str, object]) -> dict[str, object]:
    return {
        "plan_item": "settle_v4_85_parent_neutral_forward_samples",
        "current_status": audit.get("forward_sample_gate_status", ""),
        "planned_entry_date": v471.get("planned_entry_date", ""),
        "planned_exit_date": v471.get("planned_exit_date", ""),
        "required_settled_events": audit.get("forward_sample_gate_required_settled_events", ""),
        "current_settled_events": audit.get("forward_sample_gate_current_settled_events", ""),
        "next_action": "退出日后结算真实行业指数 forward return；未结算前不升级生产状态。",
    }


def render_report(audit: dict[str, object], v485_summary: dict[str, object], v471: dict[str, object]) -> str:
    return "\n".join([
        "# V4.86 父行业 cap1 强反弹规则前推观察包",
        "",
        str(audit["final_verdict"]),
        "",
        "## 冻结规则",
        "",
        "- 来源：V4.85 最接近成功规则。",
        "- 状态门控：`deep_highvol_liq_repair`。",
        "- 排序：`oversold_liquidity_score` 全市场排序。",
        "- 分散约束：每个父行业最多 1 个二级行业。",
        "- TopN：10。",
        "",
        "## 当前证据边界",
        "",
        f"- V4.85 平均超额：{float(v485_summary.get('best_mean_relative_return', 0.0)):.2%}",
        f"- V4.85 Top20% 命中率：{float(v485_summary.get('best_top_quintile_hit_rate', 0.0)):.2%}",
        f"- V4.85 bootstrap Top20% 命中率 5% 下界：{float(v485_summary.get('best_bootstrap_top_quintile_hit_p05', 0.0)):.2%}",
        f"- V4.85 状态：`{v485_summary.get('best_status', '')}`",
        f"- V4.71 最新反弹窗口触发：`{str(v471.get('latest_signal_triggered', False)).lower()}`",
        f"- 计划入场日：{v471.get('planned_entry_date', '')}",
        f"- 计划退出日：{v471.get('planned_exit_date', '')}",
        "",
        "## 前推账本",
        "",
        f"- 账本行数：{audit['ledger_rows']}",
        f"- 追踪批次数：{audit['unique_tracker_count']}",
        f"- 待结算观察：{audit['pending_forward_observation_count']}",
        f"- 已结算观察批次：{audit['forward_sample_gate_current_settled_events']}",
        f"- 目标结算批次：{audit['forward_sample_gate_required_settled_events']}",
        f"- 前推样本门槛：`{audit['forward_sample_gate_status']}`",
        f"- 自动执行：`{str(audit['auto_execution_allowed']).lower()}`",
        "",
        "## 研究边界",
        "",
        "V4.86 只是把最接近成功的 V4.85 规则冻结并开始前推观察。它不会把未结算的未来收益写入结果，也不会把 research_only 候选升级为交易信号。",
    ])


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def stringify(row: dict[str, object]) -> dict[str, str]:
    return {key: str(value) for key, value in row.items()}


def fmt(value: object) -> str:
    try:
        return f"{float(value):.8f}"
    except (TypeError, ValueError):
        return str(value)


def self_check() -> None:
    sample = pd.DataFrame([
        {"industry_code": "1", "industry_name": "A1", "parent_industry": "A", "oversold_liquidity_score": 0.9},
        {"industry_code": "2", "industry_name": "A2", "parent_industry": "A", "oversold_liquidity_score": 0.8},
        {"industry_code": "3", "industry_name": "B1", "parent_industry": "B", "oversold_liquidity_score": 0.7},
    ])
    selected = v485.select_with_parent_cap(sample.sort_values("oversold_liquidity_score", ascending=False), 2, 1)
    assert selected["industry_code"].tolist() == ["1", "3"]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.csv"
        global LEDGER
        old_ledger = LEDGER
        LEDGER = path
        rows = [{"tracker_id": "t", "industry_code": "1", "selection_mode": "m", "selection_feature": "f", **{field: "" for field in LEDGER_FIELDS}}]
        rows[0].update({"tracker_id": "t", "industry_code": "1", "selection_mode": "m", "selection_feature": "f"})
        append_ledger(rows)
        append_ledger([{**rows[0], "rank": "2"}])
        loaded = read_rows(path)
        assert len(loaded) == 1
        assert loaded[0]["rank"] == "2"
        audit = audit_ledger(loaded)
        assert audit["duplicate_key_count"] == 0
        LEDGER = old_ledger
    empty = select_current_candidates(pd.DataFrame())
    assert empty.empty
    assert list(empty.columns) == CANDIDATE_FIELDS
    assert list(top_candidates_frame(pd.DataFrame(), []).columns) == CANDIDATE_FIELDS
    print("self_check=pass")


if __name__ == "__main__":
    main()
