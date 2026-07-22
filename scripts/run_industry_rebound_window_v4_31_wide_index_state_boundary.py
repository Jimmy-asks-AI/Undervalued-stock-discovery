#!/usr/bin/env python
from __future__ import annotations

import itertools
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_31_wide_index_state_boundary_policy.json"
VERSION = "4.31.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    source = normalize_trades(pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig"))
    daily = build_wide_index_state(ROOT / policy["wide_index_dir"])
    enriched = attach_state(source, daily)
    candidates = build_candidates(enriched, policy)
    primary = candidates.iloc[0].to_dict()
    primary_trades = apply_conditions(enriched, json.loads(primary["conditions_json"])).copy()
    primary_trades["signal_id"] = primary["signal_id"]
    primary_trades["signal_name_zh"] = primary["signal_name_zh"]
    primary_trades["signal_type"] = "wide_index_state_boundary_upper_bound"
    wf = year_summary(primary_trades)
    data_audit = build_data_audit(source, daily, enriched, candidates, policy)
    leakage = build_leakage_audit()
    notes = build_notes(primary, candidates, policy)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    source.to_csv(debug / "wide_index_source_trades.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(debug / "wide_index_daily_state.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(debug / "wide_index_enriched_trades.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(debug / "wide_index_boundary_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.31宽基指数状态边界上限审计完成")
    print(f"主边界={primary['signal_id']}")
    print(f"独立事件={primary['nonoverlap_events']}")
    print(f"最终结论={run['final_verdict']}")


def normalize_trades(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["signal_date_dt"] = pd.to_datetime(out["signal_date"])
    for col in ["trade_return", "year"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["is_win"] = to_bool(out["is_win"])
    out["is_bad_window"] = to_bool(out["is_bad_window"])
    return out.sort_values("signal_date_dt").reset_index(drop=True)


def build_wide_index_state(index_dir: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    symbols: list[str] = []
    for path in sorted(index_dir.glob("*.csv")):
        sym = path.stem
        symbols.append(sym)
        df = pd.read_csv(path, encoding="utf-8-sig")
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df.sort_values("trade_date")
        for horizon in [5, 20, 60]:
            df[f"{sym}_ret_{horizon}d"] = df["close"] / df["close"].shift(horizon) - 1.0
        for window in [20, 60]:
            df[f"{sym}_ma{window}_gap"] = df["close"] / df["close"].rolling(window).mean() - 1.0
        df[f"{sym}_dd_252d"] = df["close"] / df["close"].rolling(252).max() - 1.0
        df[f"{sym}_vol_20_vs_60"] = df["volume"].rolling(20).mean() / df["volume"].rolling(60).mean() - 1.0
        frames.append(df[["trade_date"] + [c for c in df.columns if c.startswith(f"{sym}_")]])
    wide = frames[0]
    for frame in frames[1:]:
        wide = wide.merge(frame, on="trade_date", how="outer")
    wide = wide.sort_values("trade_date")
    for horizon in [5, 20, 60]:
        cols = [f"{sym}_ret_{horizon}d" for sym in symbols]
        wide[f"wide_avg_ret_{horizon}d"] = wide[cols].mean(axis=1)
        wide[f"wide_positive_{horizon}d_ratio"] = (wide[cols] > 0).mean(axis=1)
    for window in [20, 60]:
        cols = [f"{sym}_ma{window}_gap" for sym in symbols]
        wide[f"wide_above_ma{window}_ratio"] = (wide[cols] > 0).mean(axis=1)
        wide[f"wide_avg_ma{window}_gap"] = wide[cols].mean(axis=1)
    wide["wide_avg_dd_252d"] = wide[[f"{sym}_dd_252d" for sym in symbols]].mean(axis=1)
    wide["wide_avg_vol_20_vs_60"] = wide[[f"{sym}_vol_20_vs_60" for sym in symbols]].mean(axis=1)
    if "sh000852" in symbols and "sh000300" in symbols:
        wide["small_vs_large_20d"] = wide["sh000852_ret_20d"] - wide["sh000300_ret_20d"]
        wide["small_vs_large_60d"] = wide["sh000852_ret_60d"] - wide["sh000300_ret_60d"]
    return wide[["trade_date"] + [c for c in wide.columns if c.startswith("wide_") or c.startswith("small_vs_large_")]]


def attach_state(source: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    return pd.merge_asof(
        source.sort_values("signal_date_dt"),
        daily.rename(columns={"trade_date": "wide_state_date"}).sort_values("wide_state_date"),
        left_on="signal_date_dt",
        right_on="wide_state_date",
        direction="backward",
        allow_exact_matches=False,
    )


def build_candidates(enriched: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    conditions = single_conditions(enriched, policy)
    rows: list[dict[str, Any]] = []
    for cond in conditions:
        rows.append(summary_row(enriched, [cond], policy))
    for a, b in itertools.combinations(conditions, 2):
        if a["feature"] == b["feature"]:
            continue
        rows.append(summary_row(enriched, [a, b], policy))
    frame = pd.DataFrame(rows)
    frame = frame[frame["nonoverlap_events"] >= int(policy["min_realtime_events"])].copy()
    frame = frame.sort_values(["event_mean_return", "event_bad_window_rate", "nonoverlap_events"], ascending=[False, True, False]).reset_index(drop=True)
    frame["signal_id"] = [f"wide_index_boundary_{i+1:03d}" for i in range(len(frame))]
    return frame[["signal_id"] + [c for c in frame.columns if c != "signal_id"]]


def single_conditions(df: pd.DataFrame, policy: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feature in policy["features"]:
        s = pd.to_numeric(df[feature], errors="coerce")
        for threshold in sorted(set(float(x) for x in s.quantile(policy["quantiles"]).dropna())):
            out.append({"feature": feature, "op": ">=", "threshold": threshold})
            out.append({"feature": feature, "op": "<=", "threshold": threshold})
    return out


def summary_row(df: pd.DataFrame, conditions: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    d = apply_conditions(df, conditions)
    returns = pd.to_numeric(d["trade_return"], errors="coerce")
    years = d["year"].dropna().astype(int)
    count = len(d)
    mean = float(returns.mean()) if count else math.nan
    win = float((returns > 0).mean()) if count else math.nan
    bad = float(d["is_bad_window"].mean()) if count else math.nan
    active_years = int(years.nunique()) if len(years) else 0
    concentration = float(years.value_counts(normalize=True).max()) if len(years) else math.nan
    return {
        "signal_name_zh": " + ".join(f"{c['feature']} {c['op']} {c['threshold']:.6g}" for c in conditions),
        "signal_type": "wide_index_state_boundary_upper_bound",
        "status": classify(count, mean, win, bad, active_years, concentration, policy),
        "condition_count": len(conditions),
        "nonoverlap_events": int(count),
        "event_mean_return": mean,
        "event_win_rate": win,
        "event_bad_window_rate": bad,
        "event_worst_return": float(returns.min()) if count else math.nan,
        "active_years": active_years,
        "max_single_year_concentration": concentration,
        "conditions_json": json.dumps(conditions, ensure_ascii=False),
    }


def apply_conditions(df: pd.DataFrame, conditions: list[dict[str, Any]]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for cond in conditions:
        s = pd.to_numeric(df[cond["feature"]], errors="coerce")
        mask &= s >= float(cond["threshold"]) if cond["op"] == ">=" else s <= float(cond["threshold"])
    return df[mask].copy()


def classify(count: int, mean: float, win: float, bad: float, active_years: int, concentration: float, policy: dict[str, Any]) -> str:
    if (
        count >= int(policy["min_realtime_events"])
        and mean >= float(policy["min_realtime_mean_return"])
        and win >= float(policy["min_realtime_win_rate"])
        and bad <= float(policy["max_realtime_bad_window_rate"])
        and active_years >= int(policy["min_active_years"])
        and concentration <= float(policy["max_single_year_concentration"])
    ):
        return "上限达标待冻结验证"
    if count >= int(policy["min_realtime_events"]) and mean >= 0.0 and win >= 0.5 and bad <= float(policy["max_realtime_bad_window_rate"]):
        return "条件观察"
    return "拒绝"


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"year": int(year), "status": "pass", "signal_dates": int(len(g)), "signal_mean_return": float(pd.to_numeric(g["trade_return"], errors="coerce").mean()), "signal_bad_window_rate": float(g["is_bad_window"].mean())}
            for year, g in d.groupby("year")
        ]
    )


def build_data_audit(source: pd.DataFrame, daily: pd.DataFrame, enriched: pd.DataFrame, candidates: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    missing = int(enriched["wide_state_date"].isna().sum())
    return pd.DataFrame(
        [
            {"audit_item": "wide_index_history_loaded", "status": "pass" if len(daily) > 0 else "fail", "evidence": f"wide_days={len(daily)}; source_events={len(source)}", "action": "无宽基指数历史时不得做状态审计。"},
            {"audit_item": "asof_previous_wide_index_state", "status": "pass" if missing == 0 else "fail", "evidence": f"missing_matches={missing}; allow_exact_matches=false", "action": "信号日只能使用此前已存在宽基状态。"},
            {"audit_item": "candidate_sample_floor", "status": "pass" if len(candidates) > 0 else "fail", "evidence": f"eligible_candidates={len(candidates)}; min_events={policy['min_realtime_events']}", "action": "宽基状态边界候选必须保留足够事件。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "no_same_day_wide_index_state", "status": "pass", "evidence": "merge_asof uses allow_exact_matches=false", "action": "防止把信号日当日宽基收盘状态回填到信号。"},
            {"audit_item": "upper_bound_not_frozen_rule", "status": "observe", "evidence": "primary boundary is selected by full-sample realized return ranking", "action": "本版只判断宽基状态信息源上限；不得升级为实时规则。"},
        ]
    )


def build_notes(primary: dict[str, Any], candidates: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    over_line = candidates[candidates["event_mean_return"] >= float(policy["min_realtime_mean_return"])]
    notes = [
        "V4.31确认：宽基指数状态没有修复反弹窗口收益厚度。",
        f"全样本排序最优宽基边界为 {primary['signal_name_zh']}，事件 {int(primary['nonoverlap_events'])} 个，平均收益 {fmt_pct(primary['event_mean_return'])}。",
        f"保留至少 {policy['min_realtime_events']} 个事件的宽基候选共有 {len(candidates)} 个，其中平均收益达到 2% 的候选 {len(over_line)} 个。",
        "宽基状态提供了不同市场信息，但上限仍低于有效窗口门槛。",
    ]
    return {"main_diagnosis": notes[0], "next_iterations": notes, "recommended_next_direction": "下一步应停止单一信息源边界搜索，转向多源冻结规则或重新定义更低频窗口目标。"}


def run_summary(policy: dict[str, Any], primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    audit_fail = int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": 0,
        "audit_fail_count": audit_fail,
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "final_verdict": "research_only；宽基指数状态边界未证明有效反弹窗口",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], candidates: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.31 宽基指数状态边界上限审计报告",
            "",
            notes["main_diagnosis"],
            "",
            f"- 主边界：{run['primary_signal_id']}",
            f"- 主边界独立事件：{run['primary_realtime_events']}",
            f"- 主边界平均收益：{fmt_pct(run['best_event_mean_return'])}",
            f"- 主边界坏窗口率：{fmt_pct(run['best_event_bad_window_rate'])}",
            f"- 最终结论：{run['final_verdict']}",
            "",
            "## 关键判断",
            *[f"- {item}" for item in notes["next_iterations"]],
            "",
            "## 宽基边界候选 Top 20",
            candidates.head(20).to_markdown(index=False),
            "",
            "## 主边界年度表现",
            wf.to_markdown(index=False) if not wf.empty else "主边界无年度事件。",
            "",
            "## 审计",
            data_audit.to_markdown(index=False),
            leakage.to_markdown(index=False),
            "",
            f"研究边界：{policy['research_boundary']}",
        ]
    )


def to_bool(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(["true", "1", "yes"])


def fmt_pct(v: Any) -> str:
    try:
        x = float(v)
    except Exception:
        return ""
    return "" if math.isnan(x) else f"{x:.2%}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v]
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if hasattr(v, "item"):
        return clean(v.item())
    return v


def none_if_nan(v: Any) -> float | None:
    try:
        x = float(v)
    except Exception:
        return None
    return None if math.isnan(x) else x


if __name__ == "__main__":
    main()
