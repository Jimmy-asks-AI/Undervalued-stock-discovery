#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import run_industry_rebound_leader_oos_factor_v4_74 as v474


ROOT = Path(__file__).resolve().parents[1]
V472 = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72"
V470_TRADES = ROOT / "outputs" / "industry_rebound_window_v4_70_delayed_entry_vol_stop" / "debug" / "realtime_simulation_trades.csv"
V477_SUMMARY = ROOT / "outputs" / "industry_rebound_leader_feature_separability_v4_77" / "run_summary.json"
OUT = ROOT / "outputs" / "industry_rebound_leader_separable_portfolio_v4_78"
DEBUG = OUT / "debug"

TOP_NS = [5, 10, 20]
GATE_TEXT = "event_count>=30; mean/median relative>0; win_rate>=55%; top_quintile_hit_rate>=30%; positive_year_rate>=60%; OOS mean relative>0"


def main() -> None:
    parser = argparse.ArgumentParser(description="V4.78 portfolio test for V4.77 separable rebound-leader feature.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    sep = read_json(V477_SUMMARY)
    feature = str(sep.get("best_feature", ""))
    variant = str(sep.get("best_state_gate_variant", ""))
    opportunity = pd.read_csv(V472 / "debug" / "industry_event_opportunity_set.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    trades = pd.read_csv(V470_TRADES, encoding="utf-8-sig")
    state_opp = v474.attach_state(opportunity, trades)
    event_panel = build_event_panel(state_opp, feature, variant)
    results = summarize(event_panel, feature, variant)
    gate = gate_audit(results)
    summary = build_summary(results, gate, sep)
    latest = current_candidates(feature, variant, results)

    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    latest.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, results, gate, latest), encoding="utf-8")
    event_panel.to_csv(DEBUG / "separable_portfolio_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "separable_portfolio_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")
    latest.to_csv(DEBUG / "latest_separable_portfolio_candidates.csv", index=False, encoding="utf-8-sig")
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_top_n={summary['best_top_n']}")


def build_event_panel(frame: pd.DataFrame, feature: str, variant: str) -> pd.DataFrame:
    if not feature or feature not in frame.columns:
        return pd.DataFrame()
    mask = variant_mask(frame, variant)
    source = frame[mask].copy()
    rows = []
    for top_n in TOP_NS:
        rows.extend(v474.evaluate_factor(source, variant, feature, top_n))
    return pd.DataFrame(rows)


def variant_mask(frame: pd.DataFrame, variant: str) -> pd.Series:
    if variant == "deep_negative_breadth_only":
        return frame["deep_negative_breadth"]
    if variant == "mid_high_stress_only":
        return frame["mid_high_stress"]
    if variant == "high_volatility_only":
        return frame["high_volatility_protection"]
    if variant == "any_passed_state_bucket":
        return frame["any_passed_state_bucket"]
    return pd.Series(True, index=frame.index)


def summarize(panel: pd.DataFrame, feature: str, variant: str) -> pd.DataFrame:
    rows = []
    if panel.empty:
        return pd.DataFrame()
    for top_n, g in panel.groupby("top_n"):
        yearly = g.groupby("year")["relative_return"].mean()
        oos = g[g["year"] >= 2022]
        row = {
            "state_gate_variant": variant,
            "feature": feature,
            "top_n": int(top_n),
            "event_count": int(len(g)),
            "year_count": int(g["year"].nunique()),
            "mean_relative_return": float(g["relative_return"].mean()),
            "median_relative_return": float(g["relative_return"].median()),
            "relative_win_rate": float(g["relative_win"].mean()),
            "top_quintile_hit_rate": float(g["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()) if len(yearly) else 0.0,
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
            "selected_industry_examples": "|".join(g["selected_industries"].head(3).astype(str)),
        }
        row["passes_strong_rebound_gate"] = passes(row)
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values(["passes_strong_rebound_gate", "mean_relative_return"], ascending=[False, False]).reset_index(drop=True)


def failed_metrics(row: dict[str, object]) -> list[str]:
    checks = [
        ("event_count", 30, ">="),
        ("mean_relative_return", 0, ">"),
        ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="),
    ]
    failed = []
    for metric, required, op in checks:
        value = float(row.get(metric, 0) or 0)
        ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    return failed


def passes(row: dict[str, object]) -> bool:
    return not failed_metrics(row)


def gate_audit(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    best = results.iloc[0].to_dict()
    failed = set(str(best.get("failed_metrics", "")).split(";"))
    reqs = [
        ("event_count", 30, ">="),
        ("mean_relative_return", 0, ">"),
        ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="),
        ("top_quintile_hit_rate", 0.30, ">="),
        ("positive_year_rate", 0.60, ">="),
        ("oos_event_count", 8, ">="),
        ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="),
    ]
    return pd.DataFrame([
        {
            "feature": best.get("feature", ""),
            "state_gate_variant": best.get("state_gate_variant", ""),
            "top_n": best.get("top_n", ""),
            "metric": metric,
            "current": best.get(metric, ""),
            "operator": op,
            "required": required,
            "status": "fail" if metric in failed else "pass",
        }
        for metric, required, op in reqs
    ])


def build_summary(results: pd.DataFrame, gate: pd.DataFrame, sep: dict[str, object]) -> dict[str, object]:
    best = results.iloc[0].to_dict() if len(results) else {}
    failed = gate[gate["status"].eq("fail")]["metric"].tolist() if len(gate) else ["no_results"]
    passed = not failed
    return {
        "version": "4.78.0",
        "policy_id": "industry_rebound_leader_separable_portfolio_v4_78",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_feature_separability_status": sep.get("best_status", ""),
        "best_state_gate_variant": best.get("state_gate_variant", ""),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_relative_win_rate": float(best.get("relative_win_rate", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "best_positive_year_rate": float(best.get("positive_year_rate", 0.0) or 0.0),
        "best_oos_mean_relative_return": float(best.get("oos_mean_relative_return", 0.0) or 0.0),
        "failed_metrics": ";".join(failed),
        "best_status": "pass_stronger_industry_gate" if passed else "research_only_not_validated",
        "production_ready": False,
        "auto_execution_allowed": False,
        "evaluation_gate": GATE_TEXT,
        "final_verdict": "分离度通过特征转成组合后通过强行业收益门槛。" if passed else "分离度通过特征转成组合后仍未通过强行业收益门槛。",
    }


def current_candidates(feature: str, variant: str, results: pd.DataFrame) -> pd.DataFrame:
    latest = pd.read_csv(V472 / "top_candidates.csv", encoding="utf-8-sig", dtype={"industry_code": str})
    best_top_n = int(results.iloc[0]["top_n"]) if len(results) else 0
    out = latest.head(best_top_n or len(latest)).copy()
    out["candidate_status"] = "research_only_separable_portfolio_candidate"
    out["separable_feature"] = feature
    out["state_gate_variant"] = variant
    out["manual_review_reason"] = "V4.77 分离度通过，但 V4.78 收益门槛未必通过；未通过前只作为研究观察。"
    cols = [
        "candidate_status",
        "separable_feature",
        "state_gate_variant",
        "industry_code",
        "industry_name",
        "selection_score",
        "valuation_score",
        "oversold_score",
        "turn_score",
        "liquidity_score",
        "manual_review_reason",
    ]
    return out[[col for col in cols if col in out.columns]]


def render_report(summary: dict[str, object], results: pd.DataFrame, gate: pd.DataFrame, latest: pd.DataFrame) -> str:
    return "\n".join([
        "# V4.78 分离度通过特征组合验证",
        "",
        str(summary["final_verdict"]),
        "",
        "## 方法",
        "",
        "- 只使用 V4.77 已通过分离度审计的状态桶和特征。",
        "- 不继续搜索其他因子或状态桶。",
        "- 检查 Top5/10/20 组合是否能按收益评价体系跑赢全行业等权。",
        "",
        "## 核心结论",
        "",
        table(pd.DataFrame([summary])),
        "",
        "## 组合结果",
        "",
        table(results),
        "",
        "## 门槛审计",
        "",
        table(gate),
        "",
        "## 当前候选",
        "",
        table(latest),
        "",
        "## 研究边界",
        "",
        "分离度通过只说明特征有排序信息；组合收益门槛通过前，仍不能称为已找到强反弹行业。",
    ])


def table(df: pd.DataFrame) -> str:
    return "无数据。" if df.empty else df.to_markdown(index=False)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    row = {
        "event_count": 30,
        "mean_relative_return": 0.01,
        "median_relative_return": 0.01,
        "relative_win_rate": 0.6,
        "top_quintile_hit_rate": 0.3,
        "positive_year_rate": 0.6,
        "oos_event_count": 8,
        "oos_mean_relative_return": 0.01,
        "oos_relative_win_rate": 0.5,
    }
    assert passes(row)
    row["positive_year_rate"] = 0.4
    assert "positive_year_rate" in failed_metrics(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
