from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "industry_fund_flow_source_audit"
DEBUG = OUT / "debug"


def main() -> None:
    import akshare as ak

    checks: list[tuple[str, str, str, Callable[[], object]]] = [
        ("eastmoney_sector_rank", "东方财富板块资金流排名", "current_only_or_rolling_5_10d", lambda: ak.stock_sector_fund_flow_rank()),
        ("eastmoney_sector_hist", "东方财富行业历史资金流", "candidate_historical", lambda: ak.stock_sector_fund_flow_hist(symbol="汽车服务")),
        ("ths_industry_flow_now", "同花顺行业资金流即时", "current_only", lambda: ak.stock_fund_flow_industry(symbol="即时")),
        ("ths_industry_flow_5d", "同花顺行业资金流5日", "rolling_5d", lambda: ak.stock_fund_flow_industry(symbol="5日排行")),
    ]
    rows = [run_check(item) for item in checks]
    pass_count = sum(1 for row in rows if row["status"] == "pass")
    summary = {
        "version": "fund_flow_source_audit_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_count": len(rows),
        "pass_count": pass_count,
        "production_ready": False,
        "final_verdict": "行业资金流是值得尝试的新维度，但当前 AkShare 免费接口在本机环境未稳定通过，不能接入 V4.72 回测或实盘摘要。",
        "next_action": "先做独立缓存与字段映射；连续多日抓取成功后，再进入因子发现，不直接改交易辅助规则。",
    }
    write_outputs(summary, rows)
    print(f"output_dir={OUT}")
    print(f"pass_count={pass_count}")
    print("production_ready=False")


def run_check(item: tuple[str, str, str, Callable[[], object]]) -> dict[str, object]:
    source_id, name, pit_boundary, call = item
    started = datetime.now()
    try:
        df = call()
        rows = int(getattr(df, "shape", [0, 0])[0])
        cols = list(getattr(df, "columns", []))
        status = "pass" if rows > 0 else "empty"
        error = ""
    except Exception as exc:
        rows = 0
        cols = []
        status = "fail"
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    return {
        "source_id": source_id,
        "source_name": name,
        "pit_boundary": pit_boundary,
        "status": status,
        "row_count": rows,
        "column_count": len(cols),
        "columns": "|".join(map(str, cols[:30])),
        "error": error,
        "duration_seconds": round((datetime.now() - started).total_seconds(), 3),
        "production_decision": "hold_out_until_stable_cache" if status != "pass" else "candidate_for_cache_only",
    }


def write_outputs(summary: dict[str, object], rows: list[dict[str, object]]) -> None:
    DEBUG.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")
    with (DEBUG / "source_attempts.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    # ponytail: empty candidate file keeps compact-output readers boring; delete if audit outputs get their own schema.
    with (OUT / "top_candidates.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write("source_id,status,production_decision\n")
        for row in rows:
            handle.write(f"{row['source_id']},{row['status']},{row['production_decision']}\n")


def render_report(summary: dict[str, object], rows: list[dict[str, object]]) -> str:
    lines = [
        "# 行业资金流数据源审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 通过接口：{summary['pass_count']} / {summary['source_count']}",
        f"- 生产可用：`{str(summary['production_ready']).lower()}`",
        f"- 下一步：{summary['next_action']}",
        "",
        "| source_id | PIT边界 | 状态 | 行数 | 错误 | 生产决策 |",
        "|:---|:---|:---|---:|:---|:---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['source_id']} | {row['pit_boundary']} | {row['status']} | {row['row_count']} | {row['error']} | {row['production_decision']} |"
        )
    lines += [
        "",
        "研究边界：资金流若只有当前或短周期滚动值，只能从接入日起做 PIT 缓存；不能回填历史做强行业选择回测。",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
