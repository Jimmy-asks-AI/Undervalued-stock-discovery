#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "test" / "current_mainline_self_check"
CHECKS = [
    ("目标完成门禁", "scripts/build_v5_10_rebound_leader_goal_completion_audit.py"),
    ("ETF PIT主表", "scripts/build_etf_pit_master.py"),
    ("ETF生命周期", "scripts/audit_official_etf_lifecycle_sources.py"),
    ("ETF申万成分暴露", "scripts/build_etf_sw_industry_exposure_mapping.py"),
    ("ETF真实成交回放", "scripts/run_etf_realistic_execution_replay.py"),
    ("当前六角色确定性否决链", "scripts/run_etf_assisted_trading_current.py"),
    ("行业候选数据新鲜度", "scripts/run_industry_index_research_validation.py"),
    ("前推样本不可变边界", "scripts/build_v5_08_rebound_leader_forward_signal_detector.py"),
    ("前推样本严格结算", "scripts/settle_v5_06_rebound_leader_forward_samples.py"),
    ("前推择时与行业晋级", "scripts/build_v5_07_rebound_leader_promotion_evaluator.py"),
    ("纸面人工决策日志", "scripts/record_etf_paper_decision.py"),
    ("辅助交易完成度", "scripts/audit_etf_assisted_trading_completion.py"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 ETF 辅助交易当前主线自检回归。")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        assert status_for(0) == "pass" and status_for(1) == "fail"
        assert all((ROOT / path).exists() for _, path in CHECKS)
        print("self_check=pass")
        return

    rows = []
    for name, relative_path in CHECKS:
        completed = subprocess.run(
            [sys.executable, str(ROOT / relative_path), "--self-check"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        rows.append({"check": name, "script": relative_path, "status": status_for(completed.returncode),
                     "return_code": completed.returncode, "output": (completed.stdout + completed.stderr).strip()[:2000]})
    write_outputs(rows)
    failed = sum(row["status"] == "fail" for row in rows)
    print(f"self_checks={len(rows)} failed={failed}")
    raise SystemExit(1 if failed else 0)


def status_for(return_code: int) -> str:
    return "pass" if return_code == 0 else "fail"


def write_outputs(rows: list[dict[str, object]]) -> None:
    debug = OUTPUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    with (debug / "test_results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader(); writer.writerows(rows)
    with (OUTPUT / "top_candidates.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["check", "script", "status"])
        writer.writeheader(); writer.writerows({key: row[key] for key in writer.fieldnames} for row in rows)
    passed = sum(row["status"] == "pass" for row in rows)
    summary = {
        "version": "current-mainline-self-check-regression-2.0",
        "suite_type": "self_check_regression",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "self_check_count": len(rows),
        "self_check_pass_count": passed,
        "self_check_fail_count": len(rows) - passed,
        "self_check_regression_passed": passed == len(rows),
    }
    (OUTPUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# ETF辅助交易当前主线自检回归", "", f"- 自检项：{len(rows)}", f"- 通过：{passed}",
             f"- 失败：{len(rows) - passed}", f"- 总状态：`{'pass' if passed == len(rows) else 'fail'}`", "",
             "本入口只编排当前主线已有 `--self-check`，用于检查脚本基本合同与可调用性。",
             "它不等同于独立行为测试，行为测试通过数在完成度审计中另行统计。"]
    (OUTPUT / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
