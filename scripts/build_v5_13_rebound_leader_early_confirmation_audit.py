#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_leader_robust_grid_v4_80 as v480


ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
OPPORTUNITY = ROOT / "outputs" / "industry_rebound_leader_market_sensitivity_v4_99" / "debug" / "market_sensitivity_opportunity_set.csv"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_early_confirmation_audit_v5_13"
DEBUG = OUT / "debug"
CONFIRM_DAYS = [3, 5, 10]
FEATURES = ["early_strength_rank", "early_beta_score"]
TOP_NS = [5, 10, 20]
COST = 0.001


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.13 early confirmation audit.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    panel = build_panel()
    events = evaluate(panel)
    results = summarize(events)
    summary = build_summary(results)
    write_outputs(summary, panel, events, results)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"passing_rule_count={summary['passing_rule_count']}")


def build_panel() -> pd.DataFrame:
    opp = pd.read_csv(OPPORTUNITY, encoding="utf-8-sig", dtype={"industry_code": str})
    histories = load_histories(opp["industry_code"].unique())
    rows: list[dict[str, Any]] = []
    for (signal_date, entry_date, exit_date), event in opp.groupby(["signal_date", "entry_date", "exit_date"], sort=False):
        for confirm_days in CONFIRM_DAYS:
            event_rows = []
            for row in event.to_dict("records"):
                code = str(row["industry_code"]).zfill(6)
                hist = histories.get(code)
                if hist is None:
                    continue
                ret = delayed_returns(hist, entry_date, exit_date, confirm_days)
                if ret is None:
                    continue
                event_rows.append({**row, **ret, "confirm_days": confirm_days})
            if not event_rows:
                continue
            frame = pd.DataFrame(event_rows)
            frame["early_benchmark_return"] = frame["early_return"].mean()
            frame["future_benchmark_return_after_confirm"] = frame["future_return_after_confirm"].mean()
            frame["early_relative_return"] = frame["early_return"] - frame["early_benchmark_return"]
            frame["relative_return_after_confirm"] = frame["future_return_after_confirm"] - COST - frame["future_benchmark_return_after_confirm"]
            frame["early_strength_rank"] = frame["early_relative_return"].rank(pct=True)
            frame["early_beta_score"] = 0.60 * frame["early_strength_rank"] + 0.40 * frame["beta_120_rank"]
            rows.extend(frame.to_dict("records"))
    return pd.DataFrame(rows)


def load_histories(codes: list[str] | pd.Series) -> dict[str, pd.DataFrame]:
    histories = {}
    for code in sorted({str(code).zfill(6) for code in codes}):
        path = HISTORY_DIR / f"{code}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, encoding="utf-8-sig")
        frame["日期"] = pd.to_datetime(frame["日期"]).dt.strftime("%Y-%m-%d")
        frame["收盘"] = pd.to_numeric(frame["收盘"], errors="coerce")
        histories[code] = frame.dropna(subset=["收盘"]).reset_index(drop=True)
    return histories


def delayed_returns(history: pd.DataFrame, entry_date: str, exit_date: str, confirm_days: int) -> dict[str, Any] | None:
    entry_matches = history.index[history["日期"].eq(entry_date)].tolist()
    exit_matches = history.index[history["日期"].eq(exit_date)].tolist()
    if not entry_matches or not exit_matches:
        return None
    entry_idx = entry_matches[0]
    confirm_idx = entry_idx + confirm_days
    exit_idx = exit_matches[0]
    if confirm_idx >= exit_idx or confirm_idx >= len(history):
        return None
    entry_close = float(history.at[entry_idx, "收盘"])
    confirm_close = float(history.at[confirm_idx, "收盘"])
    exit_close = float(history.at[exit_idx, "收盘"])
    if min(entry_close, confirm_close, exit_close) <= 0:
        return None
    return {
        "confirm_date": history.at[confirm_idx, "日期"],
        "entry_close": entry_close,
        "confirm_close": confirm_close,
        "exit_close": exit_close,
        "early_return": confirm_close / entry_close - 1.0,
        "future_return_after_confirm": exit_close / confirm_close - 1.0,
    }


def evaluate(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in FEATURES:
        for top_n in TOP_NS:
            for confirm_days, subset in panel.groupby("confirm_days"):
                for (signal_date, entry_date, exit_date, confirm_date), event in subset.groupby(["signal_date", "entry_date", "exit_date", "confirm_date"]):
                    event = event.dropna(subset=[feature, "future_return_after_confirm"])
                    if event.empty:
                        continue
                    top_cut = event["future_return_after_confirm"].quantile(0.8)
                    selected = event.sort_values(feature, ascending=False).head(top_n)
                    relative = float(selected["relative_return_after_confirm"].mean())
                    rows.append({
                        "feature": feature,
                        "top_n": top_n,
                        "confirm_days": int(confirm_days),
                        "signal_date": signal_date,
                        "entry_date": entry_date,
                        "confirm_date": confirm_date,
                        "exit_date": exit_date,
                        "year": int(pd.to_datetime(signal_date).year),
                        "relative_return": relative,
                        "relative_win": relative > 0,
                        "top_quintile_hit_rate": float((selected["future_return_after_confirm"] >= top_cut).mean()),
                    })
    return pd.DataFrame(rows)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (feature, top_n, confirm_days), group in events.groupby(["feature", "top_n", "confirm_days"]):
        oos = group[group["year"].ge(2022)]
        yearly = group.groupby("year")["relative_return"].mean()
        row = {
            "feature": feature,
            "top_n": int(top_n),
            "confirm_days": int(confirm_days),
            "event_count": int(len(group)),
            "year_count": int(group["year"].nunique()),
            "mean_relative_return": float(group["relative_return"].mean()),
            "median_relative_return": float(group["relative_return"].median()),
            "relative_win_rate": float(group["relative_win"].mean()),
            "top_quintile_hit_rate": float(group["top_quintile_hit_rate"].mean()),
            "positive_year_rate": float((yearly > 0).mean()),
            "oos_event_count": int(len(oos)),
            "oos_mean_relative_return": float(oos["relative_return"].mean()) if len(oos) else 0.0,
            "oos_relative_win_rate": float(oos["relative_win"].mean()) if len(oos) else 0.0,
        }
        row["point_gate_passed"] = passes_point_gate(row)
        row.update(v480.robustness_metrics(group, int(top_n)) if row["point_gate_passed"] else {})
        row["robust_gate_passed"] = bool(row.get("robust_gate_passed", False))
        row["leave_one_year_gate_passed"] = bool(row.get("leave_one_year_gate_passed", False))
        row["passes_gate"] = row["point_gate_passed"] and row["robust_gate_passed"] and row["leave_one_year_gate_passed"]
        row["failed_metrics"] = ";".join(failed_metrics(row))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["passes_gate", "mean_relative_return"], ascending=[False, False])


def failed_metrics(row: dict[str, Any]) -> list[str]:
    checks = [
        ("event_count", 30, ">="), ("year_count", 8, ">="),
        ("mean_relative_return", 0, ">"), ("median_relative_return", 0, ">"),
        ("relative_win_rate", 0.55, ">="), ("top_quintile_hit_rate", 0.30, ">="),
        ("oos_event_count", 8, ">="), ("oos_mean_relative_return", 0, ">"),
        ("oos_relative_win_rate", 0.50, ">="), ("robust_gate_passed", True, "=="),
        ("leave_one_year_gate_passed", True, "=="), ("bootstrap_top_quintile_hit_p05", 0.30, ">="),
        ("bootstrap_positive_year_p05", 0.60, ">="), ("leave_one_year_min_hit_rate", 0.25, ">="),
        ("leave_one_year_min_mean_relative_return", 0, ">"),
    ]
    failed = []
    for metric, required, op in checks:
        if op == "==":
            ok = row.get(metric) == required
        else:
            value = float(row.get(metric, 0) or 0)
            ok = value >= required if op == ">=" else value > required
        if not ok:
            failed.append(metric)
    return failed


def passes_point_gate(row: dict[str, Any]) -> bool:
    point = {
        "event_count", "year_count", "mean_relative_return", "median_relative_return",
        "relative_win_rate", "top_quintile_hit_rate", "oos_event_count",
        "oos_mean_relative_return", "oos_relative_win_rate",
    }
    return not (point & set(failed_metrics(row)))


def build_summary(results: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_gate", False))
    return {
        "version": "5.13.0",
        "policy_id": "rebound_leader_early_confirmation_audit_v5_13",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tested_rule_count": int(len(results)),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_confirm_days": int(best.get("confirm_days", 0) or 0),
        "best_event_count": int(best.get("event_count", 0) or 0),
        "best_mean_relative_return": float(best.get("mean_relative_return", 0.0) or 0.0),
        "best_top_quintile_hit_rate": float(best.get("top_quintile_hit_rate", 0.0) or 0.0),
        "passing_rule_count": int(results["passes_gate"].sum()) if len(results) else 0,
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "pass_early_confirmation_gate" if passed else "research_only_no_early_confirmation_alpha",
        "final_verdict": "V5.13 早期相对强弱确认未通过完整强行业门槛，不能声称目标完成。" if not passed else "V5.13 早期相对强弱确认通过强行业门槛，但仍需前推验证。",
    }


def write_outputs(summary: dict[str, Any], panel: pd.DataFrame, events: pd.DataFrame, results: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, results), encoding="utf-8")
    panel.to_csv(DEBUG / "early_confirmation_opportunity_set.csv", index=False, encoding="utf-8-sig")
    events.to_csv(DEBUG / "early_confirmation_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "early_confirmation_results.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.13 早期相对强弱确认审计",
        "",
        summary["final_verdict"],
        "",
        f"- 测试规则数：{summary['tested_rule_count']}",
        f"- 最优特征：`{summary['best_feature']}`",
        f"- 最优 TopN：{summary['best_top_n']}",
        f"- 最优确认等待：{summary['best_confirm_days']} 个交易日",
        f"- 最优事件数：{summary['best_event_count']}",
        f"- 最优平均相对收益：{pct(summary['best_mean_relative_return'])}",
        f"- 最优 Top20% 命中率：{pct(summary['best_top_quintile_hit_rate'])}",
        f"- 通过规则数：{summary['passing_rule_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 结果",
        "",
        results.to_markdown(index=False) if len(results) else "无数据",
        "",
        "边界：V5.13 等待信号后 3/5/10 个交易日，用这段已经发生的相对强弱排序，再从确认日持有到原退出日；它没有使用确认日之后的未来收益构造特征。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    frame = pd.DataFrame({"日期": ["2020-01-01", "2020-01-02", "2020-01-03"], "收盘": [100, 110, 121]})
    got = delayed_returns(frame, "2020-01-01", "2020-01-03", 1)
    assert got is not None
    assert round(got["early_return"], 4) == 0.1
    assert round(got["future_return_after_confirm"], 4) == 0.1
    row = {
        "event_count": 30, "year_count": 8, "mean_relative_return": 0.01,
        "median_relative_return": 0.01, "relative_win_rate": 0.56,
        "top_quintile_hit_rate": 0.31, "oos_event_count": 8,
        "oos_mean_relative_return": 0.01, "oos_relative_win_rate": 0.50,
        "robust_gate_passed": True, "leave_one_year_gate_passed": True,
        "bootstrap_top_quintile_hit_p05": 0.31, "bootstrap_positive_year_p05": 0.60,
        "leave_one_year_min_hit_rate": 0.25, "leave_one_year_min_mean_relative_return": 0.01,
    }
    assert passes_point_gate(row)
    print("self_check=pass")


if __name__ == "__main__":
    main()
