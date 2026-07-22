#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_market_state_v4_81 as v481
import run_industry_rebound_leader_robust_grid_v4_80 as v480


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
PARENT_PANEL = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "raw_industry_panel.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_parent_neutral_v4_85"
DEBUG = OUT / "debug"

STATE_VARIANTS = ["deep_or_high_vol", "deep_highvol_liq_repair"]
FEATURES = ["oversold_liquidity_score", "oversold_score", "value_oversold_turn_score"]
TOP_NS = [10, 15, 20]
SELECTION_MODES = {
    "global_rank": "全市场直接排序。",
    "parent_rank": "先在父行业内分位排序，再全市场排序。",
    "global_rank_parent_cap1": "全市场排序，但每个父行业最多选 1 个二级行业。",
    "global_rank_parent_cap2": "全市场排序，但每个父行业最多选 2 个二级行业。",
    "parent_rank_parent_cap1": "父行业内分位排序，并且每个父行业最多选 1 个二级行业。",
    "parent_rank_parent_cap2": "父行业内分位排序，并且每个父行业最多选 2 个二级行业。",
}
GATE_TEXT = "same as V4.80: point gate + bootstrap robust gate + leave-one-year gate"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.85 parent-neutral audit for rebound-leader industries.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    parent_map = load_parent_map()
    frame = attach_parent(v481.attach_full_state(opportunity, trades), parent_map)
    frame = add_parent_rank_features(frame)
    parent_audit = build_parent_mapping_audit(frame, parent_map)
    event_panel = build_event_panel(frame)
    results = summarize(event_panel)
    best = results.iloc[0] if len(results) else pd.Series(dtype=object)
    gate = gate_audit(best)
    top_rules = top_rule_table(results)
    summary = build_summary(results, best, gate, parent_audit)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    top_rules.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, top_rules, parent_audit, gate), encoding="utf-8")
    event_panel.to_csv(DEBUG / "parent_neutral_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "parent_neutral_grid_results.csv", index=False, encoding="utf-8-sig")
    parent_audit.to_csv(DEBUG / "parent_mapping_audit.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def load_parent_map() -> pd.DataFrame:
    panel = pd.read_csv(PARENT_PANEL, encoding="utf-8-sig", dtype={"industry_code": str})
    return panel[["industry_code", "parent_industry"]].drop_duplicates().assign(
        industry_code=lambda df: df["industry_code"].astype(str).str.zfill(6)
    )


def attach_parent(frame: pd.DataFrame, parent_map: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["industry_code"] = out["industry_code"].astype(str).str.zfill(6)
    return out.merge(parent_map, on="industry_code", how="left")


def add_parent_rank_features(frame: pd.DataFrame) -> pd.DataFrame:
    pieces = []
    for _, event in frame.groupby(["signal_date", "entry_date", "exit_date"], sort=False):
        event = event.copy()
        for feature in FEATURES:
            event[f"{feature}_parent_rank"] = event.groupby("parent_industry")[feature].rank(pct=True, ascending=True)
        pieces.append(event)
    return pd.concat(pieces, ignore_index=True)


def build_event_panel(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for state in STATE_VARIANTS:
        state_frame = frame[v481.state_mask(frame, state)].copy()
        for mode in SELECTION_MODES:
            for feature in FEATURES:
                for top_n in TOP_NS:
                    rows.extend(evaluate_rule(state_frame, state, mode, feature, top_n))
    return pd.DataFrame(rows)


def evaluate_rule(frame: pd.DataFrame, state: str, mode: str, feature: str, top_n: int) -> list[dict[str, object]]:
    rows = []
    score_col = f"{feature}_parent_rank" if mode.startswith("parent_rank") else feature
    cap = 1 if mode.endswith("cap1") else 2 if mode.endswith("cap2") else None
    for (signal_date, entry_date, exit_date), event in frame.groupby(["signal_date", "entry_date", "exit_date"]):
        event = event.dropna(subset=[score_col, "future_return", "parent_industry"])
        if len(event) < top_n:
            continue
        ranked = event.sort_values(score_col, ascending=False)
        selected = select_with_parent_cap(ranked, top_n, cap)
        if len(selected) < top_n:
            continue
        benchmark = float(event["future_return"].mean())
        selected_return = float(selected["future_return"].mean())
        relative = selected_return - benchmark - 0.001
        top_cut = event["future_return"].quantile(0.8)
        rank_ic = float(event[[score_col, "future_return"]].corr(method="spearman").iloc[0, 1])
        parent_counts = selected["parent_industry"].value_counts()
        rows.append({
            "state_gate_variant": state,
            "selection_mode": mode,
            "factor": feature,
            "score_column": score_col,
            "top_n": top_n,
            "signal_date": signal_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "year": int(pd.to_datetime(signal_date).year),
            "selected_return": selected_return,
            "benchmark_return": benchmark,
            "relative_return": relative,
            "relative_win": relative > 0,
            "rank_ic": rank_ic,
            "rank_ic_positive": rank_ic > 0,
            "top_quintile_hit_rate": float((selected["future_return"] >= top_cut).mean()),
            "selected_parent_count": int(parent_counts.size),
            "max_parent_weight": float(parent_counts.max() / len(selected)),
            "selected_industry_codes": "|".join(selected["industry_code"].astype(str).str.zfill(6)),
            "selected_industries": "|".join(selected["industry_name"].astype(str)),
            "selected_parents": "|".join(selected["parent_industry"].astype(str)),
        })
    return rows


def select_with_parent_cap(ranked: pd.DataFrame, top_n: int, cap: int | None) -> pd.DataFrame:
    if cap is None:
        return ranked.head(top_n)
    counts: defaultdict[str, int] = defaultdict(int)
    rows = []
    for _, row in ranked.iterrows():
        parent = str(row["parent_industry"])
        if counts[parent] >= cap:
            continue
        counts[parent] += 1
        rows.append(row)
        if len(rows) >= top_n:
            break
    return pd.DataFrame(rows)


def summarize(panel: pd.DataFrame) -> pd.DataFrame:
    if panel.empty:
        return pd.DataFrame()
    rows = []
    for (state, mode, feature, top_n), group in panel.groupby(["state_gate_variant", "selection_mode", "factor", "top_n"]):
        row = point_metrics(group, state, mode, feature, int(top_n))
        robust = v480.robustness_metrics(group, int(top_n)) if v480.point_gate_passed(row) else {}
        row.update(robust)
        row["point_gate_passed"] = v480.point_gate_passed(row)
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_v4_85_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_gate_groups"] = failed_gate_groups(row)
        rows.append(row)
    out = pd.DataFrame(rows)
    for column in ["bootstrap_top_quintile_hit_p05", "bootstrap_positive_year_p05", "leave_one_year_min_hit_rate", "leave_one_year_min_mean_relative_return"]:
        if column not in out.columns:
            out[column] = 0.0
        out[column] = out[column].fillna(0.0)
    return out.sort_values(
        [
            "passes_v4_85_gate",
            "robust_gate_passed",
            "point_gate_passed",
            "bootstrap_top_quintile_hit_p05",
            "top_quintile_hit_rate",
            "mean_relative_return",
        ],
        ascending=[False, False, False, False, False, False],
    ).reset_index(drop=True)


def point_metrics(group: pd.DataFrame, state: str, mode: str, feature: str, top_n: int) -> dict[str, object]:
    yearly = group.groupby("year")["relative_return"].mean()
    oos = group[group["year"] >= 2022]
    return {
        "state_gate_variant": state,
        "selection_mode": mode,
        "feature": feature,
        "top_n": top_n,
        "event_count": int(len(group)),
        "year_count": int(group["year"].nunique()),
        "mean_relative_return": float(group["relative_return"].mean()),
        "median_relative_return": float(group["relative_return"].median()),
        "relative_win_rate": float(group["relative_win"].mean()),
        "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
        "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
        "oos_event_count": int(len(oos)),
        "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
        "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
        "mean_selected_parent_count": float(group["selected_parent_count"].mean()),
        "mean_max_parent_weight": float(group["max_parent_weight"].mean()),
    }


def build_parent_mapping_audit(frame: pd.DataFrame, parent_map: pd.DataFrame) -> pd.DataFrame:
    unique_industries = frame[["industry_code", "parent_industry"]].drop_duplicates()
    missing = unique_industries["parent_industry"].isna().sum()
    return pd.DataFrame([
        {
            "audit_item": "parent_mapping_coverage",
            "current": float(1 - missing / len(unique_industries)) if len(unique_industries) else 0.0,
            "required": 1.0,
            "operator": "==",
            "status": "pass" if missing == 0 and len(unique_industries) else "fail",
            "evidence": f"mapped={len(unique_industries) - missing}; total={len(unique_industries)}; map_rows={len(parent_map)}",
        },
        {
            "audit_item": "parent_industry_count",
            "current": int(unique_industries["parent_industry"].nunique()),
            "required": 20,
            "operator": ">=",
            "status": "pass" if unique_industries["parent_industry"].nunique() >= 20 else "fail",
            "evidence": "父行业数量用于判断分散约束是否有意义。",
        },
    ])


def gate_audit(best: pd.Series) -> pd.DataFrame:
    if best.empty:
        return pd.DataFrame()
    checks = [
        ("point_gate_passed", True, "=="),
        ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="),
        ("event_count", 30, ">="),
        ("year_count", 5, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="),
        ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0.0, ">"),
    ]
    return pd.DataFrame([
        {
            "state_gate_variant": best.get("state_gate_variant", ""),
            "selection_mode": best.get("selection_mode", ""),
            "feature": best.get("feature", ""),
            "top_n": best.get("top_n", ""),
            "metric": metric,
            "current": best.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "pass" if compare(best.get(metric, ""), required, op) else "fail",
        }
        for metric, required, op in checks
    ])


def top_rule_table(results: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "state_gate_variant",
        "selection_mode",
        "feature",
        "top_n",
        "passes_v4_85_gate",
        "point_gate_passed",
        "robust_gate_passed",
        "leave_one_year_gate_passed",
        "event_count",
        "year_count",
        "mean_relative_return",
        "top_quintile_hit_rate",
        "positive_year_rate",
        "mean_selected_parent_count",
        "mean_max_parent_weight",
        "bootstrap_top_quintile_hit_p05",
        "bootstrap_positive_year_p05",
        "leave_one_year_min_hit_rate",
        "failed_gate_groups",
    ]
    return results[[column for column in columns if column in results.columns]].head(20).copy()


def build_summary(results: pd.DataFrame, best: pd.Series, gate: pd.DataFrame, parent_audit: pd.DataFrame) -> dict[str, object]:
    passing = results[results["passes_v4_85_gate"].eq(True)] if len(results) else pd.DataFrame()
    point = results[results["point_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    robust = results[results["robust_gate_passed"].eq(True)] if len(results) else pd.DataFrame()
    parent_ready = bool(parent_audit["status"].eq("pass").all()) if len(parent_audit) else False
    passed = bool(len(passing) and parent_ready)
    return {
        "version": "4.85.0",
        "policy_id": "industry_rebound_leader_parent_neutral_v4_85",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_rule_count": int(len(results)),
        "parent_mapping_ready": parent_ready,
        "point_gate_pass_count": int(len(point)),
        "robust_gate_pass_count": int(len(robust)),
        "passing_rule_count": int(len(passing)),
        "best_state_gate_variant": best.get("state_gate_variant", ""),
        "best_selection_mode": best.get("selection_mode", ""),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_mean_selected_parent_count": float(best.get("mean_selected_parent_count", 0.0) or 0.0),
        "best_mean_max_parent_weight": float(best.get("mean_max_parent_weight", 0.0) or 0.0),
        "best_bootstrap_top_quintile_hit_p05": float(best.get("bootstrap_top_quintile_hit_p05", 0.0) or 0.0),
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_robust_parent_neutral_leader_gate" if passed else "research_only_no_robust_parent_neutral_rule",
        "production_ready": False,
        "auto_execution_allowed": False,
        "evaluation_gate": GATE_TEXT,
        "final_verdict": (
            "V4.85 找到通过完整稳健门槛的父行业中性强行业规则；仍需实盘前推。"
            if passed else
            "V4.85 未找到通过完整稳健门槛的父行业中性规则；行业分散和父行业中性化没有证明能稳定选出强反弹行业。"
        ),
    }


def failed_gate_groups(row: dict[str, object]) -> str:
    failed = []
    if not bool(row.get("point_gate_passed", False)):
        failed.append("point")
    if not bool(row.get("robust_gate_passed", False)):
        failed.append("robust")
    if not bool(row.get("leave_one_year_gate_passed", False)):
        failed.append("leave_one_year")
    return ";".join(failed)


def compare(value: object, required: object, op: str) -> bool:
    if op == "==":
        return value == required
    current = float(value or 0)
    target = float(required)
    return current >= target if op == ">=" else current > target


def render_report(summary: dict[str, object], top_rules: pd.DataFrame, parent_audit: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.85 父行业中性强反弹行业审计",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 使用申万二级到父行业映射，检查强行业选择是否被单一父行业集中拖累。",
        "- 测试全市场排序、父行业内排序、每个父行业最多选 1 个或 2 个二级行业的分散约束。",
        "- 只使用信号日已可见的行业特征，不使用未来收益反选。",
        "- 评价门槛沿用 V4.80：点估计、bootstrap 5% 下界、留一年验证全部通过才算找到强反弹行业规则。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 最接近通过的规则",
        "",
        table(top_rules),
        "",
        "## 父行业映射审计",
        "",
        table(parent_audit),
        "",
        "## 最优规则门槛审计",
        "",
        table(gate),
        "",
        "## 研究边界",
        "",
        "V4.85 检查的是反弹窗口内强行业选择的分散和中性化问题。如果没有通过，说明当前失败不是简单的父行业集中度问题；继续扩大同类价格/估值规则的收益有限。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    ranked = pd.DataFrame({
        "parent_industry": ["A", "A", "B", "B", "C"],
        "score": [5, 4, 3, 2, 1],
    }).sort_values("score", ascending=False)
    assert len(select_with_parent_cap(ranked, 3, None)) == 3
    capped = select_with_parent_cap(ranked, 3, 1)
    assert capped["parent_industry"].tolist() == ["A", "B", "C"]
    assert compare(0.3, 0.3, ">=")
    assert not compare(0.29, 0.3, ">=")
    print("self_check=pass")


if __name__ == "__main__":
    main()
