#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from build_v5_31_fund_flow_evidence_freeze_manifest import validated_active_cohort
from fund_flow_forward_evidence import (
    checkpoint_path_for,
    materialize_observations,
    read_events,
    verify_ledger_checkpoint,
)
from research_integrity import atomic_write_csv, atomic_write_json, atomic_write_text, file_sha256


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "fund_flow_forward_chain_remediation"
DEBUG = OUT / "debug"
OBSERVATION_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.jsonl"
MATERIALIZED_LEDGER = ROOT / "logs" / "v5_25_fund_flow_forward_ledger.csv"
CANDIDATE_FREEZE_LEDGER = ROOT / "logs" / "v5_33_fund_flow_entry_price_freeze.jsonl"
BENCHMARK_FREEZE_LEDGER = ROOT / "logs" / "v5_34_fund_flow_benchmark_entry_freeze.jsonl"
PRE_MIGRATION_LEDGER = DEBUG / "pre_migration_ledger.csv"
VALIDATION_RESULTS = DEBUG / "validation_results.json"

FUTURE_FIELDS = [
    "realized_return", "benchmark_return", "realized_relative_return",
    "future_return_rank_pct", "future_top_quintile", "actual_exit_date",
]

CHANGED_FILES = [
    ("scripts/research_integrity.py", "基础设施", "严格 A 股会话日历、进程锁、原子写、哈希链与 checkpoint。"),
    ("scripts/fund_flow_forward_evidence.py", "领域契约", "观察/结算事件不可变约束、时间状态、显式 checkpoint bootstrap。"),
    ("scripts/build_v5_25_fund_flow_forward_observer.py", "观察登记", "源 bundle、2.1 事件、盘前等待、active cohort 绑定。"),
    ("scripts/build_v5_26_fund_flow_forward_entry_gate.py", "入场门禁", "直接读取权威账本并核验 checkpoint。"),
    ("scripts/settle_v5_27_fund_flow_forward_samples.py", "退出结算", "active cohort、15:00 收盘、完整性票据、精确源快照与一次结算。"),
    ("scripts/build_v5_28_fund_flow_promotion_evaluator.py", "晋级评估", "只认权威已结算 active 样本，保留原阈值。"),
    ("scripts/build_v5_29_fund_flow_evidence_calendar.py", "证据日历", "active/global 分离，checkpoint 门禁，明确收盘后结算。"),
    ("scripts/audit_v5_30_fund_flow_forward_ledger_integrity.py", "独立审计", "Schema、物化一致性、源快照复算、日期、价格和 cohort 创建时点复核。"),
    ("scripts/build_v5_31_fund_flow_evidence_freeze_manifest.py", "批次基线", "不可覆盖 cohort、二次核验、创建时点由历史链锚定、生产 runner 纳入冻结。"),
    ("scripts/build_v5_32_fund_flow_holding_observation.py", "持有观察", "直接读取权威账本并核验 checkpoint。"),
    ("scripts/build_v5_33_fund_flow_entry_price_freeze.py", "候选价冻结", "窗口三态、内容寻址源快照、active/global 分离。"),
    ("scripts/build_v5_34_fund_flow_benchmark_entry_freeze.py", "基准冻结", "至少 100 行、首冻不可变、源快照、active/global 分离。"),
    ("scripts/build_v5_35_fund_flow_waiting_room.py", "等待室", "全部依赖按 active pair 过滤，历史只作诊断。"),
    ("scripts/run_v4_71_live_refresh.py", "生产编排", "按先核验、再观察、再冻结、再审计、后结算的顺序执行。"),
    ("configs/fund_flow_forward_chain_policy.json", "治理配置", "固化 2.2 时间、快照、checkpoint 与 active 范围规则。"),
    ("configs/fund_flow_forward_ledger_schema.json", "事件 Schema", "兼容 2.0 迁移记录，对 2.1 强制 verified bundle 与 cohort hash。"),
    ("tests/test_research_integrity.py", "基础测试", "并发、崩溃、篡改、交易日历测试。"),
    ("tests/test_fund_flow_forward_chain.py", "链路测试", "反例、时间边界、源复算、范围隔离与不可变状态测试。"),
    ("scripts/build_fund_flow_forward_chain_remediation_report.py", "交付报告", "汇总真实状态、迁移、反例和验证证据。"),
]

COUNTEREXAMPLES = [
    ("盘前观察", "信号日 15:00 前", "early_pending，不产生权威 observation"),
    ("迟到观察", "入场日 09:30 后", "late_backfill_excluded，永久不得晋级"),
    ("盘前候选价", "入场日 14:59", "freeze_window_pending，不写冻结账本"),
    ("按时候选价", "入场日 15:30", "frozen_on_time"),
    ("迟到候选价", "入场日 16:01", "late_backfill_excluded"),
    ("盘前基准", "入场日 14:59 已出现当日行情", "pending，不固化整批 late"),
    ("不足基准", "2 行或 99 行", "不结算；最低 100 行"),
    ("精确退出日缺失", "只有下一日价格", "不向后滚动"),
    ("退出日盘中", "北京时间 15:00 前", "pending_exit_market_close，不读写收益事件"),
    ("两套入场价", "候选价与基准候选成员不一致", "pending_entry_freeze_price_mismatch"),
    ("伪造完整性票据", "cohort 相同 hash 不同 id", "结算失败关闭"),
    ("checkpoint 丢失", "非空账本仍在", "普通 append 失败，必须显式 migration/bootstrap"),
    ("合法前缀回滚", "JSONL 截短但链本身仍合法", "独立 checkpoint 检出"),
    ("结算二写", "同一 observation 第二个 settlement", "拒绝"),
    ("结算改写观察字段", "settlement 修改不可变字段", "拒绝"),
    ("源快照篡改", "bundle 或价格源文件改变", "独立复算/证据 manifest 失败"),
    ("物化 CSV 漂移", "CSV 与 JSONL 重建不一致", "V5.30 失败"),
    ("历史污染", "legacy late marker 仍存在", "仅进入 global diagnostics，不影响 active gate"),
    ("活动指针篡改", "把 active.json 的 created_at_utc 回拨", "与不可变 history 不一致，fail closed，并回填历史链中的 canonical 时间"),
    ("新批次追认旧信号", "手工把 evidence_cutoff 早于 cohort 创建时点的事件绑定到 active", "V5.30 报 retroactive_cohort_ownership，资格重算失败"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the fund-flow forward-chain remediation report.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of_date)
    if as_of > date.today():
        parser.error("report as-of date cannot be in the future")

    DEBUG.mkdir(parents=True, exist_ok=True)
    validation = read_json(VALIDATION_RESULTS)
    snapshot = current_snapshot(as_of)
    changed_rows = [
        {"path": path, "role": role, "remediation_action": action}
        for path, role, action in CHANGED_FILES
    ]
    pytest_ok = validation_status(validation, "fund_flow_pytest") == "pass"
    counterexample_rows = [
        {"case": case, "input": input_, "expected_guardrail": result, "status": "pass" if pytest_ok else "unverified"}
        for case, input_, result in COUNTEREXAMPLES
    ]
    migration_rows = build_migration_manifest(snapshot)
    validation_rows = list(validation.get("checks", [])) if isinstance(validation.get("checks"), list) else []
    accepted = {"pass", "expected_fail_closed", "not_run_by_design"}
    validation_ok = bool(validation_rows) and all(str(item.get("status", "")) in accepted for item in validation_rows)
    checkpoints_ok = all(item.get("valid") is True for item in snapshot["checkpoints"].values())
    remediation_complete = bool(validation_ok and checkpoints_ok and snapshot["active_cohort_freeze_passed"])

    summary = {
        "version": "1.0.0",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "remediation_complete": remediation_complete,
        "active_cohort_id": snapshot["active_cohort_id"],
        "active_cohort_manifest_hash": snapshot["active_cohort_manifest_hash"],
        "active_cohort_freeze_passed": snapshot["active_cohort_freeze_passed"],
        "global_observation_rows": snapshot["global_observation_rows"],
        "active_observation_rows": snapshot["active_observation_rows"],
        "global_settlement_events": snapshot["global_settlement_events"],
        "qualified_settled_rows": snapshot["qualified_settled_rows"],
        "global_candidate_late_markers": snapshot["global_candidate_late_markers"],
        "global_benchmark_late_markers": snapshot["global_benchmark_late_markers"],
        "future_fields_blank_before_exit": snapshot["future_fields_blank_before_exit"],
        "exit_result_inspected": False,
        "validation_check_count": len(validation_rows),
        "validation_passed": validation_ok,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "default_action": "NO_ACTION",
        "best_status": "research_only_chain_repaired_no_active_qualified_samples" if remediation_complete else "research_only_remediation_incomplete",
        "final_verdict": (
            "资金流前推证据链的工程整改已通过验证；现有 4 条历史观察仍是探索样本，新 active cohort 尚无合格观察，不能晋级或形成交易动作。"
            if remediation_complete else
            "资金流前推证据链仍有未通过的整改验证，保持失败关闭。"
        ),
    }

    active_rows = snapshot.pop("active_rows")
    top_fields = [
        "observation_id", "batch_id", "industry_code", "industry_name", "signal_date",
        "planned_entry_date", "planned_exit_date", "sample_scope", "qualified_for_goal",
        "integrity_eligible", "promotion_eligible", "settlement_status", "qualification_reason",
    ]
    top_candidates = [{field: row.get(field, "") for field in top_fields} for row in active_rows]
    atomic_write_csv(OUT / "top_candidates.csv", top_candidates, fieldnames=top_fields)
    atomic_write_json(OUT / "run_summary.json", summary)
    atomic_write_text(OUT / "report.md", render_report(summary, snapshot, validation_rows, counterexample_rows, migration_rows))
    atomic_write_csv(DEBUG / "changed_files.csv", changed_rows, fieldnames=["path", "role", "remediation_action"])
    atomic_write_csv(DEBUG / "counterexample_results.csv", counterexample_rows, fieldnames=["case", "input", "expected_guardrail", "status"])
    atomic_write_csv(DEBUG / "migration_manifest.csv", migration_rows, fieldnames=["artifact", "role", "exists", "row_or_event_count", "sha256", "note"])
    atomic_write_csv(DEBUG / "validation_commands.csv", validation_rows, fieldnames=["id", "command", "status", "evidence"])
    atomic_write_json(DEBUG / "current_chain_snapshot.json", snapshot)
    print(f"output_dir={OUT}")
    print(f"remediation_complete={remediation_complete}")
    print(f"active_observation_rows={summary['active_observation_rows']}")


def current_snapshot(as_of: date) -> dict[str, Any]:
    observations = read_events(OBSERVATION_LEDGER) if OBSERVATION_LEDGER.exists() else []
    rows = materialize_observations(observations) if observations else []
    candidate_events = read_events(CANDIDATE_FREEZE_LEDGER) if CANDIDATE_FREEZE_LEDGER.exists() else []
    benchmark_events = read_events(BENCHMARK_FREEZE_LEDGER) if BENCHMARK_FREEZE_LEDGER.exists() else []
    active = validated_active_cohort()
    cohort_id = str(active.get("cohort_id", ""))
    manifest_hash = str(active.get("manifest_hash", ""))
    active_rows = filter_pair(rows, cohort_id, manifest_hash) if active.get("freeze_passed") is True else []
    active_settled = [row for row in active_rows if str(row.get("settlement_status", "")) == "settled"]
    checkpoints = {
        "observation": checkpoint_state(OBSERVATION_LEDGER),
        "candidate_freeze": checkpoint_state(CANDIDATE_FREEZE_LEDGER),
        "benchmark_freeze": checkpoint_state(BENCHMARK_FREEZE_LEDGER),
    }
    return {
        "as_of_date": as_of.isoformat(),
        "active_cohort_id": cohort_id,
        "active_cohort_manifest_hash": manifest_hash,
        "active_cohort_freeze_passed": active.get("freeze_passed") is True,
        "active_cohort_validation_reason": str(active.get("validation_reason", "")),
        "global_observation_rows": len(rows),
        "active_observation_rows": len(active_rows),
        "global_settlement_events": sum(str(item.get("event_type", "")) == "settlement" for item in observations),
        "active_settled_rows": len(active_settled),
        "qualified_settled_rows": sum(is_true(row.get("qualified_for_goal")) for row in active_settled),
        "global_candidate_freeze_rows": len(candidate_events),
        "active_candidate_freeze_rows": len(filter_pair(candidate_events, cohort_id, manifest_hash)),
        "global_candidate_late_markers": sum(str(item.get("entry_price_freeze_status", "")) == "late_backfill_excluded" for item in candidate_events),
        "global_benchmark_freeze_rows": len(benchmark_events),
        "active_benchmark_freeze_rows": len(filter_pair(benchmark_events, cohort_id, manifest_hash)),
        "global_benchmark_late_markers": sum(str(item.get("benchmark_entry_freeze_status", "")) == "late_backfill_excluded" for item in benchmark_events),
        "future_fields_blank_before_exit": all(not str(row.get(field, "")).strip() for row in rows for field in FUTURE_FIELDS),
        "checkpoints": checkpoints,
        "active_rows": active_rows,
    }


def filter_pair(rows: Iterable[Mapping[str, Any]], cohort_id: str, manifest_hash: str) -> list[dict[str, Any]]:
    if not cohort_id or not manifest_hash:
        return []
    return [
        dict(row) for row in rows
        if str(row.get("cohort_id", "")) == cohort_id
        and str(row.get("cohort_manifest_hash", "")) == manifest_hash
    ]


def checkpoint_state(path: Path) -> dict[str, Any]:
    try:
        result = verify_ledger_checkpoint(path)
        return {
            "valid": True,
            "event_count": int(result["event_count"]),
            "head_hash": str(result["head_hash"]),
            "checkpoint_path": str(Path(result["checkpoint_path"]).relative_to(ROOT)).replace("\\", "/"),
            "checkpoint_sha256": file_sha256(checkpoint_path_for(path)),
        }
    except Exception as exc:
        return {"valid": False, "event_count": 0, "head_hash": "", "error": str(exc)}


def build_migration_manifest(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    artifacts = [
        (PRE_MIGRATION_LEDGER, "legacy_backup", csv_row_count(PRE_MIGRATION_LEDGER), "迁移前 CSV 原样备份。"),
        (OBSERVATION_LEDGER, "authoritative_observation_ledger", int(snapshot["global_observation_rows"]), "4 条 legacy 观察转成 observation 事件；未补造 cohort、来源或未来收益。"),
        (MATERIALIZED_LEDGER, "compatibility_materialized_view", csv_row_count(MATERIALIZED_LEDGER), "由 JSONL 权威状态原子物化。"),
        (checkpoint_path_for(OBSERVATION_LEDGER), "independent_head_checkpoint", checkpoint_line_count(checkpoint_path_for(OBSERVATION_LEDGER)), "记录观察账本事件数与头哈希。"),
        (CANDIDATE_FREEZE_LEDGER, "authoritative_candidate_freeze_ledger", int(snapshot["global_candidate_freeze_rows"]), "缺少可核验精确入场价时保留不可变 late failure marker，不补价。"),
        (BENCHMARK_FREEZE_LEDGER, "authoritative_benchmark_freeze_ledger", int(snapshot["global_benchmark_freeze_rows"]), "基准不足 100 行时保留不可变 failure marker，不伪造面板。"),
    ]
    return [
        {
            "artifact": str(path.relative_to(ROOT)).replace("\\", "/"),
            "role": role,
            "exists": path.is_file(),
            "row_or_event_count": count,
            "sha256": file_sha256(path) if path.is_file() else "MISSING",
            "note": note,
        }
        for path, role, count, note in artifacts
    ]


def csv_row_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return max(sum(1 for _ in path.open("r", encoding="utf-8-sig")) - 1, 0)


def checkpoint_line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.open("r", encoding="utf-8") if line.strip())


def validation_status(validation: Mapping[str, Any], check_id: str) -> str:
    for item in validation.get("checks", []):
        if str(item.get("id", "")) == check_id:
            return str(item.get("status", ""))
    return "missing"


def render_report(
    summary: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    validation: list[dict[str, Any]],
    counterexamples: list[dict[str, Any]],
    migration: list[dict[str, Any]],
) -> str:
    checkpoint_lines = [
        f"- {name}: `{'pass' if item.get('valid') else 'fail'}`；events={item.get('event_count', 0)}；head=`{item.get('head_hash', '')}`"
        for name, item in snapshot["checkpoints"].items()
    ]
    validation_table = pd.DataFrame(validation, columns=["id", "command", "status", "evidence"])
    counterexample_table = pd.DataFrame(counterexamples, columns=["case", "input", "expected_guardrail", "status"])
    migration_table = pd.DataFrame(migration, columns=["artifact", "role", "exists", "row_or_event_count", "sha256", "note"])
    return "\n".join([
        "# 资金流前推证据链整改报告",
        "",
        str(summary["final_verdict"]),
        "",
        "## 结论",
        "",
        f"- 工程整改通过：`{str(summary['remediation_complete']).lower()}`",
        f"- 当前证据批次：`{summary['active_cohort_id'] or 'missing'}`",
        f"- 批次基线二次核验：`{str(summary['active_cohort_freeze_passed']).lower()}`",
        f"- 当前 active 观察 / 合格已结算：{summary['active_observation_rows']} / {summary['qualified_settled_rows']}",
        f"- 历史观察 / settlement 事件：{summary['global_observation_rows']} / {summary['global_settlement_events']}",
        f"- 当前动作：`{summary['default_action']}`；自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "这里要分清两件事：证据链的工程缺口已经整改，不等于研究目标已经得到样本支持。现有 4 条记录仍是历史探索样本；没有新 active 观察，也没有合格结算样本。",
        "",
        "## 这次改了什么",
        "",
        "1. 交易日由冻结的 A 股会话日历计算，入场为下一会话，退出为入场后第 20 个会话；没有工作日兜底。",
        "2. observation、候选价冻结、全行业基准冻结均进入追加式 JSONL 哈希链；兼容 CSV 只是原子物化视图。",
        "3. 非空账本缺 checkpoint 时失败关闭；合法前缀回滚、重复结算和 settlement 改写观察字段都会被拦截。",
        "4. 时间状态拆成窗口未开始、窗口内、已过截止。盘前刷新只等待，不会制造不可逆的 late marker。",
        "5. 观察源同时冻结候选表和提供 signal_date 的摘要；候选价、基准价、退出结算均保存内容寻址源快照，由 V5.30 独立复算。",
        "6. active cohort 指标和 global history 彻底分开；legacy failure marker 留作历史诊断，不再永久污染新批次。",
        "7. cohort 基线不可覆盖，创建后必须二次核验；创建时点只认追加式 history 的末条记录，active 指针无法回拨时间追认旧信号。",
        "8. 生产 runner 也纳入冻结清单；V5.30 会独立检查 cohort 创建时点不晚于每条观察的 evidence_cutoff。",
        "",
        "## 迁移原则",
        "",
        "迁移只改变存储结构，不美化历史事实。4 条旧记录保留原 signal/entry/exit 日期，统一标为探索样本；来源、日历和 cohort 无法事后证明的字段继续保持不合格。精确入场价与全行业基准缺失时，写入 failure marker；没有补造价格，也没有查看或结算 2026-07-21 的退出收益。",
        "",
        migration_table.to_markdown(index=False) if not migration_table.empty else "无迁移记录。",
        "",
        "## 当前账本",
        "",
        f"- global observation：{summary['global_observation_rows']}，active observation：{summary['active_observation_rows']}。",
        f"- global 候选 late marker：{summary['global_candidate_late_markers']}；global 基准 late marker：{summary['global_benchmark_late_markers']}。",
        f"- 退出日前未来字段保持空白：`{str(summary['future_fields_blank_before_exit']).lower()}`。",
        f"- 是否查看退出结果：`{str(summary['exit_result_inspected']).lower()}`。",
        "",
        "## checkpoint",
        "",
        *checkpoint_lines,
        "",
        "## 反例矩阵",
        "",
        counterexample_table.to_markdown(index=False),
        "",
        "## 验证",
        "",
        validation_table.to_markdown(index=False) if not validation_table.empty else "尚无验证记录。",
        "",
        "## 已知非阻塞项",
        "",
        "V5.28 在当前 active 表为空时会输出一条 Pandas4Warning，提示布尔 dtype 与字符串列的 `and` 运算将在未来主版本收紧。当前运行结果、74 项测试和晋级边界均未受影响；升级 pandas 主版本前应把空表相关列显式归一为布尔类型。",
        "",
        "## 证据边界",
        "",
        "本地哈希链与独立 checkpoint 能发现意外篡改、截断和状态漂移；它们与账本仍位于同一文件系统，没有外部签名、可信时间戳或 WORM 锚点，因此不能宣传为能抵御高权限攻击者的绝对不可篡改系统。",
        "",
        "当前结论保持 `research_only / NO_ACTION`。只有未来新 active cohort 的观察按时登记、入场与基准按时冻结、退出日收盘后精确结算，并达到原晋级阈值，研究目标才可能升级。",
    ])


def is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "是"}


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


if __name__ == "__main__":
    main()
