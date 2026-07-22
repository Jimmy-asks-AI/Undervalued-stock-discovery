#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import evaluate_rebound_window_effectiveness as ev
import run_industry_rebound_leader_robust_grid_v4_80 as v480


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit" / "rebound_leader_historical_backtest_verdict_v4_93"
DEBUG = OUT / "debug"

SOURCE_RUNS = [
    ("4.72", "初始强行业选择", ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "run_summary.json"),
    ("4.73", "状态门控", ROOT / "outputs" / "industry_rebound_leader_state_gated_v4_73" / "run_summary.json"),
    ("4.74", "训练期选因子/OOS", ROOT / "outputs" / "industry_rebound_leader_oos_factor_v4_74" / "run_summary.json"),
    ("4.75", "逐年前推", ROOT / "outputs" / "industry_rebound_leader_walk_forward_v4_75" / "run_summary.json"),
    ("4.77", "特征分离度", ROOT / "outputs" / "industry_rebound_leader_feature_separability_v4_77" / "run_summary.json"),
    ("4.78", "分离度组合", ROOT / "outputs" / "industry_rebound_leader_separable_portfolio_v4_78" / "run_summary.json"),
    ("4.79", "压力状态", ROOT / "outputs" / "industry_rebound_leader_pressure_state_v4_79" / "run_summary.json"),
    ("4.80", "稳健网格", ROOT / "outputs" / "industry_rebound_leader_robust_grid_v4_80" / "run_summary.json"),
    ("4.81", "市场状态扩展", ROOT / "outputs" / "industry_rebound_leader_market_state_v4_81" / "run_summary.json"),
    ("4.83", "尾部护栏", ROOT / "outputs" / "industry_rebound_leader_trap_guardrail_v4_83" / "run_summary.json"),
    ("4.84", "结构特征", ROOT / "outputs" / "industry_rebound_leader_structure_features_v4_84" / "run_summary.json"),
    ("4.85", "父行业中性", ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85" / "run_summary.json"),
]

V485_EVENTS = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85" / "debug" / "parent_neutral_event_panel.csv"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
RULE_FAMILIES = [
    ("4.80", ROOT / "outputs" / "industry_rebound_leader_robust_grid_v4_80" / "debug" / "robust_grid_event_panel.csv", ["state_gate_variant", "factor", "top_n"]),
    ("4.81", ROOT / "outputs" / "industry_rebound_leader_market_state_v4_81" / "debug" / "market_state_event_panel.csv", ["state_gate_variant", "factor", "top_n"]),
    ("4.83", ROOT / "outputs" / "industry_rebound_leader_trap_guardrail_v4_83" / "debug" / "guardrail_event_panel.csv", ["state_gate_variant", "guardrail", "factor", "top_n"]),
    ("4.84", ROOT / "outputs" / "industry_rebound_leader_structure_features_v4_84" / "debug" / "structure_event_panel.csv", ["state_gate_variant", "factor", "top_n"]),
    ("4.85", ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85" / "debug" / "parent_neutral_event_panel.csv", ["state_gate_variant", "selection_mode", "factor", "top_n"]),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.93 historical verdict audit for rebound-leader selection.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    rows = [source_row(version, stage, path) for version, stage, path in SOURCE_RUNS]
    probes = market_quality_filter_probe()
    family_ledger = build_family_ledger()
    checks = gate_checks(rows, probes, family_ledger)
    summary = build_summary(rows, probes, checks, family_ledger)
    write_outputs(summary, rows, probes, checks, family_ledger)
    print(f"output_dir={OUT}")
    print(f"historical_goal_ready={summary['historical_goal_ready']}")
    print(f"can_claim_strong_rebound_industries_from_backtest={summary['can_claim_strong_rebound_industries_from_backtest']}")


def source_row(version: str, stage: str, path: Path) -> dict[str, Any]:
    data = read_json(path)
    top = read_first_csv(path.parent / "top_candidates.csv")
    best_rule = " + ".join(str(x) for x in [
        data.get("best_state_gate_variant") or "",
        data.get("best_selection_mode") or "",
        data.get("best_feature") or data.get("best_strategy") or data.get("best_factor") or "",
        f"Top{data.get('best_top_n')}" if data.get("best_top_n") else "",
    ] if x)
    passing = int_value(data.get("passing_rule_count"))
    robust_count = int_value(data.get("robust_gate_pass_count"))
    point_count = int_value(data.get("point_gate_pass_count"))
    return {
        "version": version,
        "stage": stage,
        "policy_id": data.get("policy_id", ""),
        "best_rule": best_rule,
        "tested_rule_count": int_value(data.get("tested_rule_count")),
        "event_count": first_int(data, top, ["best_event_count", "full_event_count", "executed_event_count", "event_rows", "event_count"]),
        "year_count": first_int(data, top, ["best_year_count", "full_year_count", "year_count"]),
        "mean_relative_return": first_float(data, top, ["best_mean_relative_return", "full_mean_relative_return", "mean_relative_return"]),
        "top_quintile_hit_rate": first_float(data, top, ["best_top_quintile_hit_rate", "full_top_quintile_hit_rate", "top_quintile_hit_rate"]),
        "positive_year_rate": first_float(data, top, ["best_positive_year_rate", "full_positive_year_rate", "positive_year_rate"]),
        "oos_mean_relative_return": first_float(data, ["best_oos_mean_relative_return", "oos_mean_relative_return"]),
        "bootstrap_top_quintile_hit_p05": first_float(data, top, ["best_bootstrap_top_quintile_hit_p05", "bootstrap_top_quintile_hit_p05"]),
        "point_gate_passed": str(bool(data.get("point_estimate_gate_passed", False) or point_count > 0)).lower(),
        "robust_gate_passed": str(bool(data.get("robustness_gate_passed", False) or robust_count > 0)).lower(),
        "passing_rule_count": passing,
        "best_status": data.get("best_status", ""),
        "final_verdict": data.get("final_verdict", ""),
        "source_path": rel(path),
    }


def market_quality_filter_probe() -> list[dict[str, Any]]:
    if not V485_EVENTS.exists() or not V470_TRADES.exists():
        return []
    events = pd.read_csv(V485_EVENTS, encoding="utf-8-sig")
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    base = events[
        events["state_gate_variant"].eq("deep_highvol_liq_repair")
        & events["selection_mode"].eq("global_rank_parent_cap1")
        & events["factor"].eq("oversold_liquidity_score")
        & events["top_n"].eq(10)
    ].merge(trades, on=["signal_date", "entry_date", "exit_date"], how="left")
    filters = [
        ("无新增过滤", lambda df: pd.Series(True, index=df.index)),
        ("成交额5日/20日 >= 0.90", lambda df: df["market_amount_5d_vs_20d"].ge(0.90)),
        ("成交额5日/20日 >= 1.00", lambda df: df["market_amount_5d_vs_20d"].ge(1.00)),
        ("下跌集中度 <= 0.60", lambda df: df["industry_downside_concentration_20d"].le(0.60)),
        ("成交额>=0.90 且 下跌集中度<=0.60", lambda df: df["market_amount_5d_vs_20d"].ge(0.90) & df["industry_downside_concentration_20d"].le(0.60)),
    ]
    rows = []
    for name, fn in filters:
        sample = base[fn(base)].copy()
        sample["year"] = sample["year_x"]
        row = {
            "filter_name": name,
            "event_count": int(len(sample)),
            "year_count": int(sample["year"].nunique()) if len(sample) else 0,
            "sample_floor_status": "pass" if len(sample) >= 30 and sample["year"].nunique() >= 5 else "fail",
            "mean_relative_return": float(sample["relative_return"].mean()) if len(sample) else 0.0,
            "top_quintile_hit_rate": float(sample["top_quintile_hit_rate"].mean()) if len(sample) else 0.0,
            "positive_year_rate": float((sample.groupby("year")["relative_return"].mean() > 0).mean()) if len(sample) else 0.0,
            "point_gate_passed": "false",
            "robust_gate_passed": "false",
        }
        if row["sample_floor_status"] == "pass":
            point = {
                "event_count": row["event_count"],
                "year_count": row["year_count"],
                "mean_relative_return": row["mean_relative_return"],
                "median_relative_return": float(sample["relative_return"].median()),
                "relative_win_rate": float(sample["relative_win"].mean()),
                "top_quintile_hit_rate": row["top_quintile_hit_rate"],
                "positive_year_rate": row["positive_year_rate"],
                "oos_event_count": int(len(sample[sample["year"].ge(2022)])),
                "oos_mean_relative_return": float(sample[sample["year"].ge(2022)]["relative_return"].mean()),
                "oos_relative_win_rate": float(sample[sample["year"].ge(2022)]["relative_win"].mean()),
            }
            row["point_gate_passed"] = str(v480.point_gate_passed(point)).lower()
            robust = v480.robustness_metrics(sample, 10) if v480.point_gate_passed(point) else {}
            row["bootstrap_top_quintile_hit_p05"] = float(robust.get("bootstrap_top_quintile_hit_p05", 0.0) or 0.0)
            row["bootstrap_positive_year_p05"] = float(robust.get("bootstrap_positive_year_p05", 0.0) or 0.0)
            row["robust_gate_passed"] = str(bool(robust.get("robust_gate_passed", False))).lower()
        rows.append(row)
    return rows


def build_family_ledger() -> list[dict[str, Any]]:
    rows = []
    for version, path, keys in RULE_FAMILIES:
        if not path.exists():
            continue
        frame = pd.read_csv(path, encoding="utf-8-sig")
        for values, group in frame.groupby(keys, dropna=False):
            values = values if isinstance(values, tuple) else (values,)
            clustered = ev.event_cluster_frame(group, 60)
            returns = pd.to_numeric(clustered["relative_return"], errors="coerce").groupby(clustered["_cluster_id"]).mean().dropna().tolist()
            raw_p = exact_sign_flip_pvalue(returns)
            rows.append({
                "version": version,
                "rule_id": "|".join(f"{key}={value}" for key, value in zip(keys, values)),
                "registration_status": "post_hoc_historical_inventory",
                "event_count": int(len(group)),
                "independent_cluster_count_60d": len(returns),
                "cluster_mean_relative_return": float(pd.Series(returns).mean()) if returns else 0.0,
                "raw_sign_flip_pvalue": raw_p,
                "source_path": rel(path),
            })
    adjust_multiple_testing(rows)
    return rows


def exact_sign_flip_pvalue(values: list[float]) -> float:
    values = [float(value) for value in values]
    if not values or sum(values) <= 0:
        return 1.0
    observed_sum = sum(values)
    exceed = 0
    for mask in range(1 << len(values)):
        signed_sum = sum(value if mask & (1 << index) else -value for index, value in enumerate(values))
        exceed += signed_sum >= observed_sum - 1e-15
    return exceed / float(1 << len(values))


def adjust_multiple_testing(rows: list[dict[str, Any]]) -> None:
    count = len(rows)
    ordered = sorted(enumerate(rows), key=lambda item: item[1]["raw_sign_flip_pvalue"])
    running = 1.0
    for reverse_rank, (index, row) in enumerate(reversed(ordered), start=1):
        rank = count - reverse_rank + 1
        running = min(running, float(row["raw_sign_flip_pvalue"]) * count / rank)
        rows[index]["bh_fdr_qvalue"] = min(running, 1.0)
    for row in rows:
        row["bonferroni_pvalue"] = min(float(row["raw_sign_flip_pvalue"]) * count, 1.0)
        row["familywise_pass"] = bool(row["bonferroni_pvalue"] <= 0.05 and row["independent_cluster_count_60d"] >= 10)


def gate_checks(rows: list[dict[str, Any]], probes: list[dict[str, Any]], family_ledger: list[dict[str, Any]]) -> list[dict[str, str]]:
    best = best_historical_row(rows)
    any_robust = any(int_value(row.get("passing_rule_count")) > 0 or row.get("robust_gate_passed") == "true" for row in rows)
    any_probe_sample = any(row.get("filter_name") != "无新增过滤" and row.get("sample_floor_status") == "pass" for row in probes)
    return [
        check("历史点估计", "最优规则相对收益为正且 Top20% 命中率 >= 30%", best.get("version", ""), "pass" if best.get("mean_relative_return", 0) > 0 and best.get("top_quintile_hit_rate", 0) >= 0.30 else "fail", rel(SOURCE_RUNS[-1][2])),
        check("稳健性", "存在完整稳健门槛通过的强行业规则", str(any_robust).lower(), "pass" if any_robust else "fail", "outputs/industry_rebound_leader_*"),
        check("样本外", "训练期选因子后样本外仍为正且命中率达标", row_by_version(rows, "4.74").get("best_status", ""), "pass" if "pass" in row_by_version(rows, "4.74").get("best_status", "") else "fail", row_by_version(rows, "4.74").get("source_path", "")),
        check("逐年前推", "逐年前推收益和 Top20% 命中率达标", row_by_version(rows, "4.75").get("best_status", ""), "pass" if "pass" in row_by_version(rows, "4.75").get("best_status", "") else "fail", row_by_version(rows, "4.75").get("source_path", "")),
        check("新增事前过滤", "宽松市场质量过滤仍保留 >=30 事件和 >=5 年样本", str(any_probe_sample).lower(), "pass" if any_probe_sample else "fail", rel(DEBUG / "market_quality_filter_probe.csv")),
        check("家族级多重检验", "796条规则中至少一条通过60日独立簇符号翻转和Bonferroni 5%门槛", str(sum(bool(row.get("familywise_pass")) for row in family_ledger)), "pass" if any(row.get("familywise_pass") for row in family_ledger) else "fail", rel(DEBUG / "experiment_family_ledger.csv")),
    ]


def build_summary(rows: list[dict[str, Any]], probes: list[dict[str, Any]], checks: list[dict[str, str]], family_ledger: list[dict[str, Any]]) -> dict[str, Any]:
    best = best_historical_row(rows)
    fail_count = sum(row["status"] == "fail" for row in checks)
    pass_count = sum(row["status"] == "pass" for row in checks)
    tested_total = sum(int_value(row.get("tested_rule_count")) for row in rows)
    return {
        "version": "4.93.0",
        "policy_id": "rebound_leader_historical_backtest_verdict_v4_93",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_version_count": len(rows),
        "recorded_tested_rule_count": tested_total,
        "best_historical_version": best.get("version", ""),
        "best_historical_rule": best.get("best_rule", ""),
        "best_historical_mean_relative_return": best.get("mean_relative_return", 0.0),
        "best_historical_top_quintile_hit_rate": best.get("top_quintile_hit_rate", 0.0),
        "best_historical_bootstrap_top_quintile_hit_p05": best.get("bootstrap_top_quintile_hit_p05", 0.0),
        "historical_goal_ready": False,
        "can_claim_strong_rebound_industries_from_backtest": fail_count == 0,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "market_quality_probe_count": len(probes),
        "family_rule_count": len(family_ledger),
        "familywise_pass_count": sum(bool(row.get("familywise_pass")) for row in family_ledger),
        "minimum_raw_sign_flip_pvalue": min((float(row["raw_sign_flip_pvalue"]) for row in family_ledger), default=1.0),
        "minimum_bonferroni_pvalue": min((float(row["bonferroni_pvalue"]) for row in family_ledger), default=1.0),
        "experiment_registration_status": "post_hoc_historical_inventory",
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "历史回测已经找到最接近的父行业中性候选规则，但没有找到通过完整稳健门槛的强反弹行业规则；仅靠当前历史回测不能完成目标。",
    }


def write_outputs(summary: dict[str, Any], rows: list[dict[str, Any]], probes: list[dict[str, Any]], checks: list[dict[str, str]], family_ledger: list[dict[str, Any]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "top_candidates.csv", rows, list(rows[0]))
    write_csv(DEBUG / "source_version_summary.csv", rows, list(rows[0]))
    write_csv(DEBUG / "historical_gate_checks.csv", checks, ["dimension", "check", "current", "status", "evidence_path"])
    write_csv(DEBUG / "market_quality_filter_probe.csv", probes, sorted({key for row in probes for key in row}))
    write_csv(DEBUG / "experiment_family_ledger.csv", family_ledger, list(family_ledger[0]) if family_ledger else [])
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, rows, probes, checks), encoding="utf-8")


def render_report(summary: dict[str, Any], rows: list[dict[str, Any]], probes: list[dict[str, Any]], checks: list[dict[str, str]]) -> str:
    return "\n".join([
        "# V4.93 强反弹行业历史回测总审计",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 汇总版本数：{summary['source_version_count']}",
        f"- 已记录测试规则数：{summary['recorded_tested_rule_count']}",
        f"- 最接近规则：V{summary['best_historical_version']} `{summary['best_historical_rule']}`",
        f"- 最接近规则平均相对收益：{pct(summary['best_historical_mean_relative_return'])}",
        f"- 最接近规则 Top20% 命中率：{pct(summary['best_historical_top_quintile_hit_rate'])}",
        f"- 最接近规则 bootstrap Top20% 命中率 5% 下界：{pct(summary['best_historical_bootstrap_top_quintile_hit_p05'])}",
        f"- 是否可仅凭历史回测声称已找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries_from_backtest']).lower()}`",
        f"- 家族规则数：{summary['family_rule_count']}；Bonferroni 通过数：{summary['familywise_pass_count']}",
        f"- 最小原始符号翻转 p 值：{summary['minimum_raw_sign_flip_pvalue']:.6f}；最小 Bonferroni p 值：{summary['minimum_bonferroni_pvalue']:.6f}",
        "- 历史实验登记状态：`post_hoc_historical_inventory`，不能追溯性声称事前预注册。",
        "",
        "## 门槛检查",
        "",
        md_table(checks, ["dimension", "check", "current", "status", "evidence_path"]),
        "",
        "## 版本证据汇总",
        "",
        md_table(rows, ["version", "stage", "best_rule", "mean_relative_return", "top_quintile_hit_rate", "positive_year_rate", "bootstrap_top_quintile_hit_p05", "best_status"]),
        "",
        "## 事前过滤探针",
        "",
        md_table(probes, ["filter_name", "event_count", "year_count", "sample_floor_status", "mean_relative_return", "top_quintile_hit_rate", "robust_gate_passed"]),
        "",
        "## 研究边界",
        "",
        "V4.93 只汇总已有历史回测并做少量事前市场质量过滤探针，不使用未来收益反选行业，不改动 V4.85 冻结规则，也不生成交易信号。",
    ])


def best_historical_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=lambda row: (float_value(row.get("bootstrap_top_quintile_hit_p05")), float_value(row.get("top_quintile_hit_rate")), float_value(row.get("mean_relative_return"))))


def row_by_version(rows: list[dict[str, Any]], version: str) -> dict[str, Any]:
    return next((row for row in rows if row["version"] == version), {})


def check(dimension: str, text: str, current: str, status: str, evidence_path: str) -> dict[str, str]:
    return {"dimension": dimension, "check": text, "current": current, "status": status, "evidence_path": evidence_path}


def first_float(data: dict[str, Any], top: dict[str, str] | list[str], keys: list[str] | None = None) -> float:
    if keys is None:
        keys = top  # type: ignore[assignment]
        top = {}
    for key in keys:
        if key in data:
            return float_value(data.get(key))
        if isinstance(top, dict) and key in top:
            return float_value(top.get(key))
    return 0.0


def first_int(data: dict[str, Any], top: dict[str, str] | list[str], keys: list[str] | None = None) -> int:
    if keys is None:
        keys = top  # type: ignore[assignment]
        top = {}
    for key in keys:
        if key in data:
            return int_value(data.get(key))
        if isinstance(top, dict) and key in top:
            return int_value(top.get(key))
    return 0


def float_value(value: Any) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def int_value(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def pct(value: Any) -> str:
    return f"{float_value(value) * 100:.2f}%"


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/") if path.is_absolute() else str(path).replace("\\", "/")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_first_csv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.DictReader(handle), {})


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def md_table(rows: list[dict[str, Any]], fields: list[str]) -> str:
    if not rows:
        return "无数据"
    lines = ["| " + " | ".join(fields) + " |", "| " + " | ".join(["---"] * len(fields)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(format_cell(row.get(field, "")) for field in fields) + " |")
    return "\n".join(lines)


def format_cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value).replace("|", "/")


def self_check() -> None:
    rows = [
        {"version": "x", "bootstrap_top_quintile_hit_p05": 0.2, "top_quintile_hit_rate": 0.4, "mean_relative_return": 0.1},
        {"version": "y", "bootstrap_top_quintile_hit_p05": 0.3, "top_quintile_hit_rate": 0.31, "mean_relative_return": 0.01},
    ]
    assert best_historical_row(rows)["version"] == "y"
    assert pct(0.1234) == "12.34%"
    assert exact_sign_flip_pvalue([1.0, 1.0]) == 0.25
    adjusted = [{"raw_sign_flip_pvalue": 0.01, "independent_cluster_count_60d": 12},
                {"raw_sign_flip_pvalue": 0.20, "independent_cluster_count_60d": 12}]
    adjust_multiple_testing(adjusted)
    assert adjusted[0]["bonferroni_pvalue"] == 0.02 and adjusted[0]["familywise_pass"]
    assert adjusted[0]["bh_fdr_qvalue"] <= adjusted[1]["bh_fdr_qvalue"]
    print("self_check=pass")


if __name__ == "__main__":
    main()
