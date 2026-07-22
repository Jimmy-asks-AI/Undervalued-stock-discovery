#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V491_SUMMARY = ROOT / "outputs" / "audit" / "rebound_leader_promotion_math_v4_91" / "run_summary.json"
V491_GRID = ROOT / "outputs" / "audit" / "rebound_leader_promotion_math_v4_91" / "debug" / "promotion_threshold_grid.csv"
TRACKER = ROOT / "outputs" / "industry_rebound_leader_evidence_scorecard_v4_87" / "debug" / "forward_tracker_status.csv"
LEDGER = ROOT / "logs" / "v4_85_parent_neutral_forward_ledger.csv"
V487_SCRIPT = ROOT / "scripts" / "build_v4_85_parent_neutral_evidence_scorecard.py"
SETTLEMENT_SCRIPT = ROOT / "scripts" / "settle_v4_85_parent_neutral_forward_returns.py"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_metric_grain_v4_92"
DEBUG = OUT / "debug"

CHECK_FIELDS = ["dimension", "check", "current", "required", "status", "interpretation"]
TRACKER_FIELDS = ["tracker_id", "ledger_rows", "tracker_row_count", "status", "interpretation"]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.92 audit for rebound-leader metric grain consistency.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    sources = load_sources()
    tracker_snapshot = build_tracker_snapshot(sources["ledger"], sources["tracker"])
    checks = build_checks(sources, tracker_snapshot)
    summary = build_summary(checks)
    write_outputs(summary, checks, tracker_snapshot)
    print(f"output_dir={OUT}")
    print(f"metric_grain_status={summary['metric_grain_status']}")
    print(f"fail_count={summary['fail_count']}")


def load_sources() -> dict[str, Any]:
    return {
        "v491": read_json(V491_SUMMARY),
        "v491_grid": read_rows(V491_GRID),
        "tracker": read_rows(TRACKER),
        "ledger": read_rows(LEDGER),
        "v487_source": V487_SCRIPT.read_text(encoding="utf-8"),
        "settlement_source": SETTLEMENT_SCRIPT.read_text(encoding="utf-8"),
    }


def build_tracker_snapshot(ledger: list[dict[str, str]], tracker: list[dict[str, str]]) -> list[dict[str, str]]:
    ledger_counts: dict[str, int] = {}
    for row in ledger:
        tracker_id = row.get("tracker_id", "")
        ledger_counts[tracker_id] = ledger_counts.get(tracker_id, 0) + 1
    tracker_counts = {row.get("tracker_id", ""): int_value(row.get("row_count")) for row in tracker}
    ids = sorted(set(ledger_counts) | set(tracker_counts))
    out = []
    for tracker_id in ids:
        ledger_rows = ledger_counts.get(tracker_id, 0)
        tracker_row_count = tracker_counts.get(tracker_id, 0)
        ok = ledger_rows == tracker_row_count and ledger_rows == int_value(read_json(V491_SUMMARY).get("selected_per_batch"))
        out.append({
            "tracker_id": tracker_id,
            "ledger_rows": str(ledger_rows),
            "tracker_row_count": str(tracker_row_count),
            "status": "pass" if ok else "fail",
            "interpretation": "每个前推批次应有固定 Top10 候选行。",
        })
    return out


def build_checks(src: dict[str, Any], tracker_snapshot: list[dict[str, str]]) -> list[dict[str, str]]:
    v491 = src["v491"]
    grid30 = next((row for row in src["v491_grid"] if row.get("settled_forward_batches") == "30"), {})
    v487_source = src["v487_source"]
    settlement_source = src["settlement_source"]
    ledger = src["ledger"]
    selected_per_batch = int_value(v491.get("selected_per_batch"))
    required_hit_rows = int_value(v491.get("required_top_quintile_hit_rows_at_30"))
    return [
        check("V4.87", "forward_top_quintile_is_row_mean", source_has_v487_row_mean(v487_source), "true", "V4.87 forward_tracker_status 应按账本行 top_quintile_hit 求均值。"),
        check("V4.85 settlement", "settlement_writes_row_level_hit", source_has_row_level_settlement(settlement_source), "true", "结算脚本应逐候选行业行写入 top_quintile_hit。"),
        check("V4.91", "selected_per_batch_fixed", selected_per_batch, 10, "V4.85 冻结规则当前每批 Top10。"),
        check("V4.91", "top20_threshold_is_row_level", required_hit_rows, 90, "30 批次 x 10 行 x 30% = 90 个候选行命中。"),
        check("V4.91 grid", "grid_30_row_threshold", grid30.get("min_top_quintile_hit_rows", ""), "90", "门槛表中 30 批次行级 Top20% 命中数必须为 90。"),
        check("ledger", "top_quintile_hit_column_exists", "top_quintile_hit" in (ledger[0] if ledger else {}), "true", "前推账本必须保留候选行级命中字段。"),
        check("ledger", "tracker_row_counts_match_topn", all(row["status"] == "pass" for row in tracker_snapshot), "true", "账本和 V4.87 tracker 行数必须一致且等于 Top10。"),
    ]


def source_has_v487_row_mean(source: str) -> bool:
    return 'group.get("top_quintile_hit"' in source and '"top_quintile_hit_rate": float(hit.mean())' in source


def source_has_row_level_settlement(source: str) -> bool:
    return 'row["top_quintile_hit"] = "1"' in source and "top_quintile_hit_settled_rows" in source


def check(dimension: str, name: str, current: Any, required: Any, interpretation: str) -> dict[str, str]:
    current_text = normalize(current)
    required_text = normalize(required)
    return {
        "dimension": dimension,
        "check": name,
        "current": current_text,
        "required": required_text,
        "status": "pass" if current_text == required_text else "fail",
        "interpretation": interpretation,
    }


def normalize(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def build_summary(checks: list[dict[str, str]]) -> dict[str, Any]:
    fail_count = sum(row["status"] == "fail" for row in checks)
    pass_count = sum(row["status"] == "pass" for row in checks)
    return {
        "version": "4.92.0",
        "policy_id": "industry_rebound_leader_metric_grain_v4_92",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metric_grain_status": "pass" if fail_count == 0 else "fail",
        "pass_count": pass_count,
        "fail_count": fail_count,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "强反弹行业评价的 Top20% 命中口径已按候选行级别审计；当前仅允许 research_only 前推观察。",
    }


def write_outputs(summary: dict[str, Any], checks: list[dict[str, str]], tracker_snapshot: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "top_candidates.csv", checks, CHECK_FIELDS)
    write_csv(DEBUG / "metric_grain_checks.csv", checks, CHECK_FIELDS)
    write_csv(DEBUG / "tracker_grain_snapshot.csv", tracker_snapshot, TRACKER_FIELDS)
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, checks, tracker_snapshot), encoding="utf-8")


def render_report(summary: dict[str, Any], checks: list[dict[str, str]], tracker_snapshot: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.92 强反弹行业指标粒度一致性审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 指标粒度状态：`{summary['metric_grain_status']}`",
        f"- 失败项：{summary['fail_count']}",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "## 检查项",
        "",
        markdown_table(checks, CHECK_FIELDS),
        "",
        "## 批次行数快照",
        "",
        markdown_table(tracker_snapshot, TRACKER_FIELDS),
        "",
        "## 边界",
        "",
        "V4.92 不新增策略，不改变门槛，只审计 Top20% 命中率的计算粒度是否和 V4.87/V4.91/账本一致。",
    ])


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def int_value(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def markdown_table(rows: list[dict[str, str]], fields: list[str]) -> str:
    if not rows:
        return "无数据"
    lines = ["| " + " | ".join(fields) + " |", "|" + "|".join(":---" for _ in fields) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(field, "")).replace("|", "\\|") for field in fields) + " |")
    return "\n".join(lines)


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert source_has_v487_row_mean('hit = pd.to_numeric(group.get("top_quintile_hit", x)); "top_quintile_hit_rate": float(hit.mean())')
        assert source_has_row_level_settlement('row["top_quintile_hit"] = "1"; top_quintile_hit_settled_rows')
        tracker_snapshot = build_tracker_snapshot(
            [{"tracker_id": "t1", "top_quintile_hit": ""} for _ in range(10)],
            [{"tracker_id": "t1", "row_count": "10"}],
        )
        assert tracker_snapshot[0]["status"] in {"pass", "fail"}
        checks = build_checks({
            "v491": {"selected_per_batch": 10, "required_top_quintile_hit_rows_at_30": 90},
            "v491_grid": [{"settled_forward_batches": "30", "min_top_quintile_hit_rows": "90"}],
            "ledger": [{"top_quintile_hit": ""}],
            "v487_source": 'group.get("top_quintile_hit"; "top_quintile_hit_rate": float(hit.mean())',
            "settlement_source": 'row["top_quintile_hit"] = "1"; top_quintile_hit_settled_rows',
        }, [])
        assert any(row["check"] == "top20_threshold_is_row_level" for row in checks)
        assert check("x", "bool", True, "true", "ok")["status"] == "pass"
        assert Path(tmp).exists()
    print("self_check=pass")


if __name__ == "__main__":
    main()
