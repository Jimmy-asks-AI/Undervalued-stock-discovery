#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
V485_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85" / "run_summary.json"
V485_GATE = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85" / "debug" / "evaluation_gate_audit.csv"
V486_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_forward_v4_86" / "run_summary.json"
SETTLEMENT_SUMMARY = ROOT / "outputs" / "audit" / "v4_85_parent_neutral_forward_settlement" / "run_summary.json"
LEDGER = ROOT / "logs" / "v4_85_parent_neutral_forward_ledger.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_evidence_scorecard_v4_87"
DEBUG = OUT / "debug"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.87 evidence scorecard for V4.85 parent-neutral rebound leaders.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    v485 = read_json(V485_SUMMARY)
    v486 = read_json(V486_SUMMARY)
    settlement = read_json(SETTLEMENT_SUMMARY)
    gate = pd.read_csv(V485_GATE, encoding="utf-8-sig")
    ledger = read_rows(LEDGER)
    tracker = build_tracker_status(ledger)
    scorecard = build_scorecard(v485, v486, settlement, gate, tracker, ledger)
    protocol = build_promotion_protocol()
    summary = build_summary(scorecard, tracker, v485, settlement)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    scorecard.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, scorecard, protocol), encoding="utf-8")
    scorecard.to_csv(DEBUG / "evidence_scorecard.csv", index=False, encoding="utf-8-sig")
    tracker.to_csv(DEBUG / "forward_tracker_status.csv", index=False, encoding="utf-8-sig")
    protocol.to_csv(DEBUG / "promotion_protocol.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(ledger).to_csv(DEBUG / "ledger_snapshot.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"goal_ready={summary['goal_ready']}")
    print(f"fail_count={summary['fail_count']}")
    print(f"pending_count={summary['pending_count']}")


def build_tracker_status(rows: list[dict[str, str]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=[
            "tracker_id",
            "row_count",
            "outcome_status",
            "mean_realized_relative_return",
            "relative_win_rate",
            "top_quintile_hit_rate",
        ])
    frame = pd.DataFrame(rows)
    out = []
    for tracker_id, group in frame.groupby("tracker_id", dropna=False):
        rel = pd.to_numeric(group.get("realized_relative_return", pd.Series(dtype=float)), errors="coerce")
        hit = pd.to_numeric(group.get("top_quintile_hit", pd.Series(dtype=float)), errors="coerce")
        settled = group["outcome_status"].eq("settled_forward_observation")
        out.append({
            "tracker_id": tracker_id,
            "row_count": int(len(group)),
            "outcome_status": "settled" if bool(settled.all()) else "pending",
            "mean_realized_relative_return": float(rel.mean()) if rel.notna().any() else "",
            "relative_win_rate": float(rel.gt(0).mean()) if rel.notna().any() else "",
            "top_quintile_hit_rate": float(hit.mean()) if hit.notna().any() else "",
            "planned_entry_date": first_non_empty(group, "planned_entry_date"),
            "planned_exit_date": first_non_empty(group, "planned_exit_date"),
        })
    return pd.DataFrame(out)


def build_scorecard(
    v485: dict[str, object],
    v486: dict[str, object],
    settlement: dict[str, object],
    gate: pd.DataFrame,
    tracker: pd.DataFrame,
    ledger: list[dict[str, str]],
) -> pd.DataFrame:
    settled = tracker[tracker["outcome_status"].eq("settled")] if len(tracker) else pd.DataFrame()
    pending_rows = int(v486.get("pending_forward_observation_count", 0) or 0)
    duplicate_keys = int(v486.get("duplicate_key_count", 0) or 0)
    current_forward_batches = int(settlement.get("settled_tracker_count", 0) or 0)
    rows = [
        row("历史点估计", "point_gate_passed", gate_value(gate, "point_gate_passed"), "== true", gate_status(gate, "point_gate_passed"), "V4.85 点估计门槛。"),
        row("历史留一年", "leave_one_year_gate_passed", gate_value(gate, "leave_one_year_gate_passed"), "== true", gate_status(gate, "leave_one_year_gate_passed"), "V4.85 留一年验证。"),
        row("历史稳健性", "bootstrap_top_quintile_hit_p05", v485.get("best_bootstrap_top_quintile_hit_p05", ""), ">= 0.30", "pass" if float(v485.get("best_bootstrap_top_quintile_hit_p05", 0) or 0) >= 0.30 else "fail", "强行业命中率的 bootstrap 5% 下界。"),
        row("历史稳健性", "bootstrap_positive_year_p05", gate_value(gate, "bootstrap_positive_year_p05"), ">= 0.60", gate_status(gate, "bootstrap_positive_year_p05"), "年度稳定性的 bootstrap 5% 下界。"),
        row("前推样本", "settled_forward_tracker_count", current_forward_batches, ">= 30", "pass" if current_forward_batches >= 30 else "pending", "真实前推批次数，不用历史回填。"),
        row("前推样本", "pending_forward_observation_rows", pending_rows, "== 0 after exit", "pending" if pending_rows else "pass", "未到退出日前保持 pending。"),
        row("前推账本", "duplicate_key_count", duplicate_keys, "== 0", "pass" if duplicate_keys == 0 else "fail", "防止同一批次重复计入。"),
        row("生产边界", "auto_execution_allowed", v486.get("auto_execution_allowed", ""), "== false until all gates pass", "pass" if not bool(v486.get("auto_execution_allowed", True)) else "fail", "所有候选仍是 research_only。"),
    ]
    if len(settled):
        rows.extend([
            row("前推表现", "forward_mean_relative_return", float(settled["mean_realized_relative_return"].mean()), "> 0", "pass" if float(settled["mean_realized_relative_return"].mean()) > 0 else "fail", "已结算批次平均超额。"),
            row("前推表现", "forward_positive_batch_rate", float(settled["mean_realized_relative_return"].gt(0).mean()), ">= 0.55", "pass" if float(settled["mean_realized_relative_return"].gt(0).mean()) >= 0.55 else "fail", "已结算批次正超额比例。"),
            row("前推表现", "forward_top_quintile_hit_rate", float(settled["top_quintile_hit_rate"].mean()), ">= 0.30", "pass" if float(settled["top_quintile_hit_rate"].mean()) >= 0.30 else "fail", "已结算批次 Top20% 命中率。"),
        ])
    else:
        rows.extend([
            row("前推表现", "forward_mean_relative_return", "", "> 0", "pending", "尚无已结算批次。"),
            row("前推表现", "forward_positive_batch_rate", "", ">= 0.55", "pending", "尚无已结算批次。"),
            row("前推表现", "forward_top_quintile_hit_rate", "", ">= 0.30", "pending", "尚无已结算批次。"),
        ])
    if not ledger:
        rows.append(row("前推账本", "ledger_exists", 0, "> 0", "fail", "缺少前推账本。"))
    return pd.DataFrame(rows)


def build_promotion_protocol() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "stage": "historical_gate",
            "requirement": "V4.85 点估计和留一年验证通过，bootstrap 失败项必须通过或由前推证据补足。",
            "metric": "point_gate_passed; leave_one_year_gate_passed; bootstrap_top_quintile_hit_p05; bootstrap_positive_year_p05",
            "decision_rule": "任一硬失败项未补足时保持 research_only。",
        },
        {
            "stage": "forward_gate",
            "requirement": "至少 30 个独立前推批次结算，且平均超额>0、正超额比例>=55%、Top20% 命中率>=30%。",
            "metric": "settled_forward_tracker_count; forward_mean_relative_return; forward_positive_batch_rate; forward_top_quintile_hit_rate",
            "decision_rule": "未满 30 个批次时只能观察；满足后再复算综合评价。",
        },
        {
            "stage": "production_boundary",
            "requirement": "即使前推门槛通过，也只证明行业选择研究有效，不自动生成交易指令。",
            "metric": "auto_execution_allowed",
            "decision_rule": "自动执行必须保持 false，除非另有交易系统和人工授权。",
        },
    ])


def build_summary(scorecard: pd.DataFrame, tracker: pd.DataFrame, v485: dict[str, object], settlement: dict[str, object]) -> dict[str, object]:
    fail_count = int(scorecard["status"].eq("fail").sum())
    pending_count = int(scorecard["status"].eq("pending").sum())
    pass_count = int(scorecard["status"].eq("pass").sum())
    goal_ready = fail_count == 0 and pending_count == 0
    return {
        "version": "4.87.0",
        "policy_id": "industry_rebound_leader_evidence_scorecard_v4_87",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_rule_version": v485.get("version", ""),
        "settled_tracker_count": settlement.get("settled_tracker_count", 0),
        "required_settled_tracker_count": settlement.get("required_settled_tracker_count", 30),
        "scorecard_rows": int(len(scorecard)),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "pending_count": pending_count,
        "goal_ready": goal_ready,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "尚未证明找到稳定强反弹行业；V4.85 规则已冻结，等待前推样本结算并补足稳健性证据。",
    }


def render_report(summary: dict[str, object], scorecard: pd.DataFrame, protocol: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.87 强反弹行业证据计分卡",
        "",
        str(summary["final_verdict"]),
        "",
        "## 当前状态",
        "",
        f"- 已结算前推批次：{summary['settled_tracker_count']}/{summary['required_settled_tracker_count']}",
        f"- 通过项：{summary['pass_count']}",
        f"- 失败项：{summary['fail_count']}",
        f"- 待观察项：{summary['pending_count']}",
        f"- 目标完成：`{str(summary['goal_ready']).lower()}`",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 证据计分卡",
        "",
        table(scorecard),
        "",
        "## 晋级协议",
        "",
        table(protocol),
        "",
        "## 研究边界",
        "",
        "V4.87 不新增策略参数，只固定判断口径。未来结算样本必须按本表评价，不能因为结果好坏临时调整门槛。",
    ])


def row(dimension: str, metric: str, current: object, required: str, status: str, interpretation: str) -> dict[str, object]:
    return {
        "dimension": dimension,
        "metric": metric,
        "current": current,
        "required": required,
        "status": status,
        "interpretation": interpretation,
    }


def gate_value(gate: pd.DataFrame, metric: str) -> object:
    item = gate[gate["metric"].eq(metric)]
    return item["current"].iloc[0] if len(item) else ""


def gate_status(gate: pd.DataFrame, metric: str) -> str:
    item = gate[gate["metric"].eq(metric)]
    return str(item["status"].iloc[0]) if len(item) else "missing"


def first_non_empty(frame: pd.DataFrame, column: str) -> str:
    if column not in frame.columns:
        return ""
    values = frame[column].dropna().astype(str)
    values = values[values.ne("")]
    return values.iloc[0] if len(values) else ""


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def self_check() -> None:
    tracker = build_tracker_status([
        {"tracker_id": "t1", "outcome_status": "settled_forward_observation", "realized_relative_return": "0.1", "top_quintile_hit": "1"},
        {"tracker_id": "t1", "outcome_status": "settled_forward_observation", "realized_relative_return": "-0.1", "top_quintile_hit": "0"},
    ])
    assert tracker["mean_realized_relative_return"].iloc[0] == 0
    assert tracker["top_quintile_hit_rate"].iloc[0] == 0.5
    gate = pd.DataFrame([
        {"metric": "point_gate_passed", "current": True, "status": "pass"},
        {"metric": "leave_one_year_gate_passed", "current": True, "status": "pass"},
        {"metric": "bootstrap_positive_year_p05", "current": 0.5, "status": "fail"},
    ])
    scorecard = build_scorecard(
        {"best_bootstrap_top_quintile_hit_p05": 0.28},
        {"pending_forward_observation_count": 0, "duplicate_key_count": 0, "auto_execution_allowed": False},
        {"settled_tracker_count": 1},
        gate,
        tracker,
        [{"tracker_id": "t1"}],
    )
    assert "fail" in set(scorecard["status"])
    assert "pending" in set(scorecard["status"])
    print("self_check=pass")


if __name__ == "__main__":
    main()
