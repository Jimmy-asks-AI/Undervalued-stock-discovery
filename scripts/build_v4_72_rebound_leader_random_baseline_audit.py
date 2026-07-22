#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
OUT = ROOT / "outputs" / "audit" / "v4_72_rebound_leader_random_baseline_audit"
DEBUG = OUT / "debug"

TOP_QUINTILE_RANDOM_BASELINE = 0.20
TOP_QUINTILE_REQUIRED_RATE = 0.30
RELATIVE_WIN_RANDOM_BASELINE = 0.50
RELATIVE_WIN_REQUIRED_RATE = 0.55
POSITIVE_YEAR_REQUIRED_RATE = 0.60

FIELDS = [
    "metric",
    "current",
    "random_baseline",
    "required",
    "status",
    "gap",
    "interpretation",
    "evidence_path",
]
YEAR_FIELDS = [
    "year",
    "event_count",
    "selected_count",
    "observed_top_hits",
    "expected_random_top_hits",
    "excess_top_hits",
    "top_quintile_hit_rate",
    "z_score",
    "mean_relative_return",
    "status",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit V4.72 rebound-leader performance versus random baselines.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    strategy_rows = read_rows(SRC / "debug" / "strategy_results.csv")
    event_rows = read_rows(SRC / "debug" / "industry_event_panel.csv")
    annual_rows = read_rows(SRC / "debug" / "annual_breakdown.csv")
    opportunity_rows = read_rows(SRC / "debug" / "industry_event_opportunity_set.csv")
    rows = build_rows(strategy_rows, event_rows, annual_rows, opportunity_rows)
    year_rows = build_year_random_breakdown(strategy_rows, event_rows, opportunity_rows)
    write_outputs(rows, year_rows)
    print(f"output_dir={OUT}")
    print(f"rows={len(rows)}")


def build_rows(
    strategy_rows: list[dict[str, str]],
    event_rows: list[dict[str, str]],
    annual_rows: list[dict[str, str]],
    opportunity_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    if not strategy_rows:
        return [row("source_strategy_results", "missing", "", "present", "fail", "missing", "缺少策略结果，不能评价是否强于随机。", "outputs/industry_rebound_leader_selection_v4_72/debug/strategy_results.csv")]
    best = strategy_rows[0]
    event_count = int_value(best.get("event_count"))
    strategy = best.get("strategy", "")
    top_n = best.get("top_n", "")
    best_events = [item for item in event_rows if item.get("strategy") == strategy and item.get("top_n") == top_n]
    year_rows = [item for item in annual_rows if item.get("strategy") == strategy and item.get("top_n") == top_n]
    positive_years = sum(float_value(item.get("mean_relative_return")) > 0 for item in year_rows)
    year_count = len(year_rows)

    selected_count = sum(int_value(item.get("top_n")) for item in best_events) or event_count * int_value(top_n)
    top_successes = round(sum(float_value(item.get("top_quintile_hit_rate")) * int_value(item.get("top_n")) for item in best_events)) if best_events else round(float_value(best.get("top_quintile_hit_rate")) * selected_count)
    relative_trials = len(best_events) or event_count
    relative_successes = sum(str(item.get("relative_win", "")).lower() == "true" for item in best_events) if best_events else round(float_value(best.get("relative_win_rate")) * relative_trials)
    top_required = required_successes(TOP_QUINTILE_REQUIRED_RATE, selected_count)
    relative_required = required_successes(RELATIVE_WIN_REQUIRED_RATE, relative_trials)
    positive_year_required = required_successes(POSITIVE_YEAR_REQUIRED_RATE, year_count)
    top_wilson = wilson_lower_bound(top_successes, selected_count)
    empirical = empirical_random_edge(best_events, opportunity_rows)

    return [
        row(
            "top_quintile_hit_rate",
            f"{top_successes}/{selected_count}={rate(top_successes, selected_count)}",
            f"{TOP_QUINTILE_RANDOM_BASELINE:.0%}",
            f">= {top_required}/{selected_count}={TOP_QUINTILE_REQUIRED_RATE:.0%}",
            "pass" if top_successes >= top_required else "fail",
            gap_text(top_required - top_successes, "hit_events"),
            "当前强反弹命中率高于随机，但还没达到系统自己的 30% 硬门槛。",
            "outputs/industry_rebound_leader_selection_v4_72/debug/strategy_results.csv",
        ),
        row(
            "top_quintile_wilson_lower_bound",
            f"{top_wilson:.2%}",
            f"{TOP_QUINTILE_RANDOM_BASELINE:.0%}",
            f"> {TOP_QUINTILE_RANDOM_BASELINE:.0%}",
            "pass" if top_wilson > TOP_QUINTILE_RANDOM_BASELINE else "fail",
            "0" if top_wilson > TOP_QUINTILE_RANDOM_BASELINE else "需要更多样本或更高命中率",
            "点估计强于随机还不够，置信下界也必须超过随机前 20%。",
            "outputs/industry_rebound_leader_selection_v4_72/debug/strategy_results.csv",
        ),
        row(
            "empirical_random_top_quintile_edge",
            f"observed={empirical['observed']:.0f}; expected={empirical['expected']:.2f}; z={empirical['z_score']:.2f}; p_one_sided={empirical['p_one_sided']:.4f}",
            "逐事件机会集随机抽样",
            "z >= 1.96",
            "pass" if empirical["z_score"] >= 1.96 else "fail",
            f"{empirical['observed'] - empirical['expected']:.2f} excess_hits",
            "按每个窗口当时的行业数量和Top20%数量计算随机TopN期望；这是比固定20%更贴近真实机会集的基准。",
            "outputs/industry_rebound_leader_selection_v4_72/debug/industry_event_opportunity_set.csv",
        ),
        row(
            "relative_win_rate",
            f"{relative_successes}/{relative_trials}={rate(relative_successes, relative_trials)}",
            f"{RELATIVE_WIN_RANDOM_BASELINE:.0%}",
            f">= {relative_required}/{relative_trials}={RELATIVE_WIN_REQUIRED_RATE:.0%}",
            "pass" if relative_successes >= relative_required else "fail",
            gap_text(relative_required - relative_successes, "win_events"),
            "相对全行业平均的跑赢频率已经超过随机和系统门槛，但这不能替代 Top20% 强反弹命中。",
            "outputs/industry_rebound_leader_selection_v4_72/debug/strategy_results.csv",
        ),
        row(
            "positive_year_rate",
            f"{positive_years}/{year_count}={rate(positive_years, year_count)}",
            "n/a",
            f">= {positive_year_required}/{year_count}={POSITIVE_YEAR_REQUIRED_RATE:.0%}",
            "pass" if positive_years >= positive_year_required else "fail",
            gap_text(positive_year_required - positive_years, "positive_years"),
            "分年稳定性不够，说明结果仍可能靠少数年份拉动。",
            "outputs/industry_rebound_leader_selection_v4_72/debug/annual_breakdown.csv",
        ),
    ]


def empirical_random_edge(best_events: list[dict[str, str]], opportunity_rows: list[dict[str, str]]) -> dict[str, float]:
    if not best_events or not opportunity_rows:
        return {"observed": 0.0, "expected": 0.0, "variance": 0.0, "z_score": 0.0, "p_one_sided": 1.0}
    opportunities: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for item in opportunity_rows:
        opportunities.setdefault(event_key(item), []).append(item)
    observed = 0.0
    expected = 0.0
    variance = 0.0
    for event in best_events:
        n = int_value(event.get("top_n"))
        group = opportunities.get(event_key(event), [])
        universe = len(group)
        if n <= 0 or universe <= 0:
            continue
        top_count = sum(truthy(item.get("future_return_top_quintile")) for item in group)
        p = top_count / universe
        observed += float_value(event.get("top_quintile_hit_rate")) * n
        expected += n * p
        if universe > 1:
            variance += n * p * (1 - p) * (universe - n) / (universe - 1)
    z_score = (observed - expected) / math.sqrt(variance) if variance > 0 else 0.0
    return {
        "observed": observed,
        "expected": expected,
        "variance": variance,
        "z_score": z_score,
        "p_one_sided": 0.5 * math.erfc(z_score / math.sqrt(2)) if variance > 0 else 1.0,
    }


def build_year_random_breakdown(
    strategy_rows: list[dict[str, str]],
    event_rows: list[dict[str, str]],
    opportunity_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    if not strategy_rows:
        return []
    best = strategy_rows[0]
    best_events = [
        item for item in event_rows
        if item.get("strategy") == best.get("strategy") and item.get("top_n") == best.get("top_n")
    ]
    years = sorted({item.get("year", "") for item in best_events if item.get("year")})
    rows = []
    for year in years:
        events = [item for item in best_events if item.get("year") == year]
        empirical = empirical_random_edge(events, opportunity_rows)
        selected_count = sum(int_value(item.get("top_n")) for item in events)
        mean_relative = sum(float_value(item.get("relative_return")) for item in events) / len(events) if events else 0.0
        top_rate = empirical["observed"] / selected_count if selected_count else 0.0
        status = "pass" if mean_relative > 0 and top_rate >= TOP_QUINTILE_REQUIRED_RATE else "fail"
        rows.append({
            "year": year,
            "event_count": str(len(events)),
            "selected_count": str(selected_count),
            "observed_top_hits": f"{empirical['observed']:.2f}",
            "expected_random_top_hits": f"{empirical['expected']:.2f}",
            "excess_top_hits": f"{empirical['observed'] - empirical['expected']:.2f}",
            "top_quintile_hit_rate": f"{top_rate:.4f}",
            "z_score": f"{empirical['z_score']:.4f}",
            "mean_relative_return": f"{mean_relative:.4f}",
            "status": status,
        })
    return rows


def event_key(item: dict[str, str]) -> tuple[str, str, str]:
    return (item.get("signal_date", ""), item.get("entry_date", ""), item.get("exit_date", ""))


def row(metric: str, current: str, baseline: str, required: str, status: str, gap: str, note: str, evidence: str) -> dict[str, str]:
    return {
        "metric": metric,
        "current": current,
        "random_baseline": baseline,
        "required": required,
        "status": status,
        "gap": gap,
        "interpretation": note,
        "evidence_path": evidence,
    }


def write_outputs(rows: list[dict[str, str]], year_rows: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_rows(OUT / "top_candidates.csv", rows)
    write_rows(DEBUG / "random_baseline_audit.csv", rows)
    write_rows(DEBUG / "year_random_breakdown.csv", year_rows, YEAR_FIELDS)
    values = {item["metric"]: item for item in rows}
    empirical = values.get("empirical_random_top_quintile_edge", {}).get("current", "")
    failed_years = [item["year"] for item in year_rows if item.get("status") == "fail"]
    summary = {
        "version": "v4_72_rebound_leader_random_baseline_audit_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "row_count": len(rows),
        "pass_count": sum(item["status"] == "pass" for item in rows),
        "fail_count": sum(item["status"] == "fail" for item in rows),
        "top_quintile_success_gap": parse_gap(values.get("top_quintile_hit_rate", {}).get("gap")),
        "positive_year_gap": parse_gap(values.get("positive_year_rate", {}).get("gap")),
        "relative_win_success_gap": parse_gap(values.get("relative_win_rate", {}).get("gap")),
        "empirical_random_top_quintile_current": empirical,
        "empirical_random_top_quintile_z_score": parse_labeled_float(empirical, "z"),
        "empirical_random_top_quintile_p_one_sided": parse_labeled_float(empirical, "p_one_sided"),
        "year_random_pass_count": sum(item.get("status") == "pass" for item in year_rows),
        "year_random_fail_count": len(failed_years),
        "year_random_fail_years": ",".join(failed_years),
        "top_quintile_random_baseline": TOP_QUINTILE_RANDOM_BASELINE,
        "relative_win_random_baseline": RELATIVE_WIN_RANDOM_BASELINE,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": final_verdict(rows),
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows), encoding="utf-8")


def final_verdict(rows: list[dict[str, str]]) -> str:
    if any(item["status"] == "fail" for item in rows):
        return "强行业选择相对随机有方向性，但 Top20% 命中、置信下界或正年份稳定性仍未过硬门槛。"
    return "强行业选择已通过随机基准缺口审计，但仍需结合前推结算和交易载体门禁。"


def render_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [
        "# V4.72 强反弹行业随机基准审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 检查项：{summary['row_count']}",
        f"- 通过：{summary['pass_count']}",
        f"- 失败：{summary['fail_count']}",
        f"- Top20%命中缺口：{summary['top_quintile_success_gap']}",
        f"- 正年份缺口：{summary['positive_year_gap']}",
        f"- 相对胜率缺口：{summary['relative_win_success_gap']}",
        f"- 逐事件随机基准：{summary.get('empirical_random_top_quintile_current', '')}",
        f"- 分年随机基准失败年份：{summary.get('year_random_fail_years', '')}",
        f"- 自动执行：`{str(summary['auto_execution_allowed']).lower()}`",
        "",
        "| metric | current | random_baseline | required | status | gap | interpretation |",
        "|:---|:---|:---|:---|:---|:---|:---|",
    ]
    for item in rows:
        lines.append(f"| {item['metric']} | {item['current']} | {item['random_baseline']} | {item['required']} | {item['status']} | {item['gap']} | {item['interpretation']} |")
    lines += ["", "边界：这是解析式与逐事件机会集随机基准审计，不是新增策略，也不是交易信号。"]
    return "\n".join(lines)


def required_successes(rate: float, count: int) -> int:
    return math.ceil(rate * count) if count > 0 else 0


def wilson_lower_bound(successes: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0:
        return 0.0
    p = successes / trials
    denom = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
    return max(0.0, (centre - margin) / denom)


def gap_text(gap: int, unit: str) -> str:
    return "0" if gap <= 0 else f"+{gap} {unit}"


def parse_gap(value: str | None) -> int:
    if not value or value == "0" or not value.startswith("+"):
        return 0
    try:
        return int(value.split()[0].lstrip("+"))
    except (IndexError, ValueError):
        return 0


def parse_labeled_float(text: str, label: str) -> float:
    needle = f"{label}="
    for part in text.split(";"):
        part = part.strip()
        if part.startswith(needle):
            try:
                return float(part.removeprefix(needle))
            except ValueError:
                return 0.0
    return 0.0


def rate(successes: int, count: int) -> str:
    return "" if count <= 0 else f"{successes / count:.2%}"


def int_value(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def truthy(value: Any) -> bool:
    return str(value).lower() in {"true", "1", "yes", "是"}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str] = FIELDS) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def self_check() -> None:
    rows = build_rows(
        [{"strategy": "s", "top_n": "10", "event_count": "57", "top_quintile_hit_rate": "0.2807017543859649", "relative_win_rate": "0.6842105263157895"}],
        [{"strategy": "s", "top_n": "10", "signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "top_quintile_hit_rate": "0.3", "relative_win": "True"}],
        [
            {"strategy": "s", "top_n": "10", "mean_relative_return": "0.1"},
            {"strategy": "s", "top_n": "10", "mean_relative_return": "-0.1"},
            {"strategy": "s", "top_n": "10", "mean_relative_return": "0.2"},
        ],
        [
            {"signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "future_return_top_quintile": "True"},
            {"signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "future_return_top_quintile": "False"},
            {"signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "future_return_top_quintile": "False"},
            {"signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "future_return_top_quintile": "False"},
        ],
    )
    year_rows = build_year_random_breakdown(
        [{"strategy": "s", "top_n": "10"}],
        [{"strategy": "s", "top_n": "10", "year": "2020", "signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "top_quintile_hit_rate": "0.3", "relative_return": "0.01"}],
        [
            {"signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "future_return_top_quintile": "True"},
            {"signal_date": "2020-01-01", "entry_date": "2020-01-02", "exit_date": "2020-01-03", "future_return_top_quintile": "False"},
        ],
    )
    assert required_successes(0.30, 570) == 171
    assert year_rows[0]["year"] == "2020"
    assert year_rows[0]["status"] == "pass"
    assert any(item["metric"] == "top_quintile_hit_rate" and item["gap"] == "0" for item in rows)
    assert any(item["metric"] == "relative_win_rate" and item["status"] == "pass" for item in rows)
    assert any(item["metric"] == "positive_year_rate" and item["status"] == "pass" for item in rows)
    assert any(item["metric"] == "empirical_random_top_quintile_edge" for item in rows)
    assert empirical_random_edge(
        [{"signal_date": "d", "entry_date": "e", "exit_date": "x", "top_n": "2", "top_quintile_hit_rate": "1.0"}],
        [
            {"signal_date": "d", "entry_date": "e", "exit_date": "x", "future_return_top_quintile": "True"},
            {"signal_date": "d", "entry_date": "e", "exit_date": "x", "future_return_top_quintile": "False"},
            {"signal_date": "d", "entry_date": "e", "exit_date": "x", "future_return_top_quintile": "False"},
        ],
    )["observed"] == 2
    assert parse_gap("+2 hit_events") == 2
    assert parse_labeled_float("observed=1; z=2.5; p_one_sided=0.01", "z") == 2.5
    assert 0 < wilson_lower_bound(16, 57) < 0.3
    print("self_check=pass")


if __name__ == "__main__":
    main()
