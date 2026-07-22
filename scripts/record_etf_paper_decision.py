#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECOMMENDATION = ROOT / "outputs" / "etf_assisted_trading_current" / "debug" / "recommendation.json"
DEFAULT_LEDGER = ROOT / "logs" / "etf_assisted_trading_paper_decisions.jsonl"
DECISIONS = {"ACCEPT", "REJECT", "DEFER"}
EXECUTED_ACTIONS = {"NO_ACTION", "BUY", "HOLD", "REDUCE", "EXIT"}
SYSTEM_TO_EXECUTED_ACTION = {"BUY_CANDIDATE": "BUY", "HOLD": "HOLD", "REDUCE": "REDUCE", "EXIT": "EXIT"}
REQUIRED_RECOMMENDATION_FIELDS = {
    "recommendation_id", "data_cutoff_date", "policy_hash", "action",
    "risk_vetoes", "human_confirmation_required", "auto_execution_allowed",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="追加记录 ETF 辅助交易的人工纸面决定。")
    parser.add_argument("--recommendation", default=str(DEFAULT_RECOMMENDATION))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--decision", choices=sorted(DECISIONS))
    parser.add_argument("--operator")
    parser.add_argument("--note", default="")
    parser.add_argument("--executed-action", choices=sorted(EXECUTED_ACTIONS))
    parser.add_argument("--etf-code")
    parser.add_argument("--expected-price", type=float)
    parser.add_argument("--filled-price", type=float)
    parser.add_argument("--filled-shares", type=int)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.verify:
        rows = verify_ledger(Path(args.ledger))
        print(f"ledger_records={len(rows)} verified=true")
        return
    if not args.decision or not args.operator:
        parser.error("记录决定时必须提供 --decision 和 --operator")

    recommendation = read_json(Path(args.recommendation))
    record = build_record(recommendation, vars(args))
    append_record(Path(args.ledger), record)
    print(f"recommendation_id={record['recommendation_id']}")
    print(f"decision={record['decision']}")
    print(f"record_hash={record['record_hash']}")


def build_record(recommendation: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(REQUIRED_RECOMMENDATION_FIELDS - recommendation.keys())
    if missing:
        raise ValueError(f"recommendation 缺少字段: {','.join(missing)}")
    if recommendation["human_confirmation_required"] is not True or recommendation["auto_execution_allowed"] is not False:
        raise ValueError("只允许记录必须人工确认且禁止自动执行的建议")

    decision = str(values["decision"])
    operator = str(values.get("operator") or "").strip()
    if not operator:
        raise ValueError("operator 不能为空")
    note = str(values.get("note") or "").strip()
    if decision in {"REJECT", "DEFER"} and not note:
        raise ValueError("拒绝或延后必须填写 --note")
    executed_action = values.get("executed_action")
    execution_values = [values.get("etf_code"), values.get("expected_price"), values.get("filled_price"), values.get("filled_shares")]
    if executed_action and decision != "ACCEPT":
        raise ValueError("只有 ACCEPT 可以记录纸面执行")
    if any(value is not None for value in execution_values) and not executed_action:
        raise ValueError("填写成交信息时必须提供 --executed-action")
    if executed_action == "NO_ACTION" and any(value is not None for value in execution_values):
        raise ValueError("NO_ACTION 不应包含成交信息")
    if executed_action and executed_action != "NO_ACTION":
        if not values.get("etf_code") or values.get("filled_price") is None or values.get("filled_shares") is None:
            raise ValueError("交易动作必须提供 ETF 代码、成交价和成交份额")
        if not str(values["etf_code"]).isdigit() or len(str(values["etf_code"])) != 6:
            raise ValueError("ETF 代码必须是 6 位数字")
        if values["filled_price"] <= 0 or values["filled_shares"] <= 0 or values["filled_shares"] % 100:
            raise ValueError("成交价必须为正，成交份额必须为正的 100 整数倍")
    if values.get("expected_price") is not None and values["expected_price"] <= 0:
        raise ValueError("预期价格必须为正")

    deviation = None
    if values.get("expected_price") is not None and values.get("filled_price") is not None:
        deviation = round((values["filled_price"] / values["expected_price"] - 1) * 10000, 4)
    return {
        "schema_version": "1.0",
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "recommendation_id": recommendation["recommendation_id"],
        "recommendation_action": recommendation["action"],
        "data_cutoff_date": recommendation["data_cutoff_date"],
        "policy_hash": recommendation["policy_hash"],
        "risk_vetoes": recommendation["risk_vetoes"],
        "decision": decision,
        "operator": operator,
        "note": note,
        "executed_action": executed_action,
        "etf_code": values.get("etf_code"),
        "expected_price": values.get("expected_price"),
        "filled_price": values.get("filled_price"),
        "filled_shares": values.get("filled_shares"),
        "execution_deviation_bps": deviation,
        "policy_deviation": bool(executed_action and executed_action != SYSTEM_TO_EXECUTED_ACTION.get(recommendation["action"], "NO_ACTION")),
    }


def append_record(path: Path, record: dict[str, Any]) -> None:
    existing = verify_ledger(path)
    if any(row["recommendation_id"] == record["recommendation_id"] for row in existing):
        raise ValueError("该 recommendation_id 已有人工记录，日志只允许追加一次")
    previous_hash = existing[-1]["record_hash"] if existing else "GENESIS"
    record["previous_hash"] = previous_hash
    record["record_hash"] = record_hash(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: local single-operator append; add an OS file lock only if concurrent writers appear.
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def verify_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows, previous_hash = [], "GENESIS"
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("previous_hash") != previous_hash or row.get("record_hash") != record_hash(row):
            raise ValueError(f"日志哈希链校验失败: line={line_number}")
        rows.append(row)
        previous_hash = row["record_hash"]
    return rows


def record_hash(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "record_hash"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def self_check() -> None:
    recommendation = {
        "recommendation_id": "demo-1", "data_cutoff_date": "2026-07-12", "policy_hash": "abc",
        "action": "NO_ACTION", "risk_vetoes": ["timing"],
        "human_confirmation_required": True, "auto_execution_allowed": False,
    }
    values = {"decision": "ACCEPT", "operator": "self-check", "note": "", "executed_action": "NO_ACTION",
              "etf_code": None, "expected_price": None, "filled_price": None, "filled_shares": None}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ledger.jsonl"
        append_record(path, build_record(recommendation, values))
        rows = verify_ledger(path)
        assert len(rows) == 1 and rows[0]["previous_hash"] == "GENESIS"
        try:
            append_record(path, build_record(recommendation, values))
        except ValueError as error:
            assert "已有人工记录" in str(error)
        else:
            raise AssertionError("duplicate recommendation must fail")
    print("self_check=pass")


if __name__ == "__main__":
    main()
