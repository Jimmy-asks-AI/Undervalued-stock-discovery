#!/usr/bin/env python
from __future__ import annotations

import itertools
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from valuation_pit_contract import audit_pit_valuation_history, official_valuation_history


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_30_valuation_regime_boundary_policy.json"
VERSION = "4.30.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    source = normalize_trades(pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig"))
    valuation_raw = official_valuation_history(pd.read_csv(ROOT / policy["valuation_history_path"], encoding="utf-8-sig"))
    pit_audit = audit_pit_valuation_history(valuation_raw)
    if not pit_audit.eligible:
        write_blocked_outputs(policy, out, debug, source, pit_audit)
        print("V4.30历史估值方法门阻断：缺少可验证的逐行可得日期。")
        return
    daily = build_daily_market_state(valuation_raw)
    enriched = attach_valuation_state(source, daily)
    candidates = build_candidates(enriched, policy)
    primary = candidates.iloc[0].to_dict()
    primary_trades = apply_conditions(enriched, json.loads(primary["conditions_json"])).copy()
    primary_trades["signal_id"] = primary["signal_id"]
    primary_trades["signal_name_zh"] = primary["signal_name_zh"]
    primary_trades["signal_type"] = "valuation_regime_boundary_upper_bound"
    wf = year_summary(primary_trades)
    data_audit = build_data_audit(source, daily, enriched, candidates, policy)
    leakage = build_leakage_audit()
    notes = build_notes(primary, candidates, policy)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    source.to_csv(debug / "valuation_source_trades.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(debug / "valuation_daily_market_state.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(debug / "valuation_enriched_trades.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(debug / "valuation_boundary_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.30市场估值状态边界上限审计完成")
    print(f"主边界={primary['signal_id']}")
    print(f"独立事件={primary['nonoverlap_events']}")
    print(f"最终结论={run['final_verdict']}")


def write_blocked_outputs(policy: dict[str, Any], out: Path, debug: Path, source: pd.DataFrame, pit_audit: Any) -> None:
    empty_candidates = pd.DataFrame(columns=["signal_id", "signal_name_zh", "signal_type", "status"])
    audit = pd.DataFrame([
        {
            "audit_item": "valuation_pit_contract",
            "status": "fail",
            "evidence": f"status={pit_audit.status}; rows={pit_audit.row_count}; errors={' | '.join(pit_audit.errors)}",
            "action": "补齐 source-backed published_at/available_date/fetched_at/source_version/revision_status 后才可重跑。",
        }
    ])
    summary = {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": 0,
        "passing_rule_count": 0,
        "valuation_pit_gate_passed": False,
        "valuation_pit_status": pit_audit.status,
        "promotion_eligible": False,
        "historical_evidence_label": "historical_review_used_in_iteration",
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "历史估值缺少可验证的逐行发布时间与可得日期；V4.30 旧结果降级为历史研究记录，本轮失败关闭。",
    }
    empty = pd.DataFrame()
    empty_candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text("# V4.30 市场估值状态边界\n\n" + summary["final_verdict"] + "\n", encoding="utf-8")
    source.to_csv(debug / "valuation_source_trades.csv", index=False, encoding="utf-8-sig")
    empty.to_csv(debug / "valuation_daily_market_state.csv", index=False, encoding="utf-8-sig")
    empty.to_csv(debug / "valuation_enriched_trades.csv", index=False, encoding="utf-8-sig")
    empty_candidates.to_csv(debug / "valuation_boundary_summary.csv", index=False, encoding="utf-8-sig")
    empty.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    empty.to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    empty.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    audit.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"status": pit_audit.status, "errors": list(pit_audit.errors)})
    write_json(debug / "frozen_policy.json", policy)


def normalize_trades(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["signal_date_dt"] = pd.to_datetime(out["signal_date"])
    for col in ["trade_return", "year"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["is_win"] = to_bool(out["is_win"])
    out["is_bad_window"] = to_bool(out["is_bad_window"])
    return out.sort_values("signal_date_dt").reset_index(drop=True)


def build_daily_market_state(v: pd.DataFrame) -> pd.DataFrame:
    pit_audit = audit_pit_valuation_history(v)
    pit_audit.require(source="V4.30 valuation history")
    frame = v.copy()
    frame["industry_code"] = frame["industry_code"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
    frame["valuation_trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise")
    frame["valuation_available_date"] = pd.to_datetime(frame["available_date"], errors="raise")
    frame["_published_order"] = pd.to_datetime(frame["published_at"], errors="raise", utc=True)
    frame["pe"] = pd.to_numeric(frame["pe"], errors="coerce")
    frame["pe_clip"] = frame["pe"].where((frame["pe"] > 0) & (frame["pe"] < 200))
    frame["pb"] = pd.to_numeric(frame["pb"], errors="coerce")
    frame["dividend_yield"] = pd.to_numeric(frame["dividend_yield"], errors="coerce")
    frame = frame.sort_values(
        ["valuation_available_date", "industry_code", "valuation_trade_date", "_published_order", "source_version"],
        kind="stable",
    )

    # Materialise the cross-section that was actually available on each
    # eligible decision date.  A later publication may update one industry,
    # but it must never make that row visible on its earlier trade_date.
    latest_by_industry: dict[str, tuple[tuple[pd.Timestamp, pd.Timestamp, str], dict[str, Any]]] = {}
    snapshots: list[dict[str, Any]] = []
    for available_date, released in frame.groupby("valuation_available_date", sort=True):
        for _, row in released.iterrows():
            code = str(row["industry_code"])
            order_key = (
                pd.Timestamp(row["valuation_trade_date"]),
                pd.Timestamp(row["_published_order"]),
                str(row["source_version"]),
            )
            current = latest_by_industry.get(code)
            if current is None or order_key >= current[0]:
                latest_by_industry[code] = (order_key, row.to_dict())
        snapshot = pd.DataFrame([payload for _, payload in latest_by_industry.values()])
        snapshots.append(
            {
                "valuation_available_date": pd.Timestamp(available_date),
                "valuation_source_trade_date": snapshot["valuation_trade_date"].max(),
                "median_pe": float(snapshot["pe_clip"].median()),
                "median_pb": float(snapshot["pb"].median()),
                "median_dividend": float(snapshot["dividend_yield"].median()),
                "valuation_coverage": int(snapshot["industry_code"].nunique()),
                "available_batch_rows": int(len(released)),
            }
        )
    daily = pd.DataFrame(snapshots).sort_values("valuation_available_date").reset_index(drop=True)
    daily["median_pe_low_score"] = expanding_low_score(daily["median_pe"])
    daily["median_pb_low_score"] = expanding_low_score(daily["median_pb"])
    daily["median_dividend_high_score"] = expanding_high_score(daily["median_dividend"])
    daily["market_valuation_cheap_score"] = daily[["median_pe_low_score", "median_pb_low_score", "median_dividend_high_score"]].mean(axis=1)
    return daily


def expanding_low_score(s: pd.Series) -> list[float]:
    seen: list[float] = []
    out: list[float] = []
    for value in pd.to_numeric(s, errors="coerce"):
        seen.append(value)
        valid = pd.Series(seen).dropna()
        out.append(float((valid >= value).mean()) if len(valid) else math.nan)
    return out


def expanding_high_score(s: pd.Series) -> list[float]:
    seen: list[float] = []
    out: list[float] = []
    for value in pd.to_numeric(s, errors="coerce"):
        seen.append(value)
        valid = pd.Series(seen).dropna()
        out.append(float((valid <= value).mean()) if len(valid) else math.nan)
    return out


def attach_valuation_state(source: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    left = source.copy()
    left["signal_date_dt"] = pd.to_datetime(left["signal_date_dt"], errors="raise")
    left["_source_order"] = range(len(left))
    right = daily.copy()
    if "valuation_available_date" not in right.columns:
        raise ValueError("daily valuation state must expose valuation_available_date")
    right["valuation_available_date"] = pd.to_datetime(right["valuation_available_date"], errors="raise")
    merged = pd.merge_asof(
        left.sort_values("signal_date_dt"),
        right.sort_values("valuation_available_date"),
        left_on="signal_date_dt",
        right_on="valuation_available_date",
        direction="backward",
        # available_date already applies the publication-time/market-close
        # rule, so a row eligible on the signal date may be used that day.
        allow_exact_matches=True,
    )
    matched = merged["valuation_available_date"].notna()
    if matched.any() and (
        merged.loc[matched, "valuation_available_date"] > merged.loc[matched, "signal_date_dt"]
    ).any():
        raise ValueError("valuation state was attached before its available_date")
    merged["valuation_date"] = merged["valuation_available_date"]
    return merged.sort_values("_source_order").drop(columns="_source_order").reset_index(drop=True)


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
    frame["signal_id"] = [f"valuation_boundary_{i+1:03d}" for i in range(len(frame))]
    return frame[["signal_id"] + [col for col in frame.columns if col != "signal_id"]]


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
        "signal_type": "valuation_regime_boundary_upper_bound",
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
    rows = []
    for year, g in d.groupby("year"):
        returns = pd.to_numeric(g["trade_return"], errors="coerce")
        rows.append({"year": int(year), "status": "pass", "signal_dates": int(len(g)), "signal_mean_return": float(returns.mean()), "signal_bad_window_rate": float(g["is_bad_window"].mean())})
    return pd.DataFrame(rows)


def build_data_audit(source: pd.DataFrame, daily: pd.DataFrame, enriched: pd.DataFrame, candidates: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    missing = int(enriched["valuation_available_date"].isna().sum())
    matched = enriched["valuation_available_date"].notna()
    future_matches = int(
        (
            enriched.loc[matched, "valuation_available_date"]
            > enriched.loc[matched, "signal_date_dt"]
        ).sum()
    )
    return pd.DataFrame(
        [
            {"audit_item": "valuation_history_loaded", "status": "pass" if len(daily) > 0 else "fail", "evidence": f"valuation_days={len(daily)}; source_events={len(source)}", "action": "无历史估值时不得做估值状态审计。"},
            {"audit_item": "asof_available_valuation", "status": "pass" if missing == 0 and future_matches == 0 else "fail", "evidence": f"missing_matches={missing}; future_matches={future_matches}; key=available_date; allow_exact_matches=true", "action": "信号日只能使用 available_date 不晚于该日的估值截面。"},
            {"audit_item": "candidate_sample_floor", "status": "pass" if len(candidates) > 0 else "fail", "evidence": f"eligible_candidates={len(candidates)}; min_events={policy['min_realtime_events']}", "action": "估值边界候选必须保留足够事件。"},
        ]
    )


def build_leakage_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"audit_item": "valuation_available_date_asof_only", "status": "pass", "evidence": "strict PIT contract + backward merge_asof on valuation_available_date", "action": "trade_date 不得充当可得日；available_date 已按发布时间和收盘边界确定。"},
            {"audit_item": "upper_bound_not_frozen_rule", "status": "observe", "evidence": "primary boundary is selected by full-sample realized return ranking", "action": "本版只判断估值信息源上限；不得升级为实时规则。"},
        ]
    )


def build_notes(primary: dict[str, Any], candidates: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    over_line = candidates[candidates["event_mean_return"] >= float(policy["min_realtime_mean_return"])]
    notes = [
        "V4.30确认：市场估值状态没有修复反弹窗口收益厚度。",
        f"全样本排序最优估值边界为 {primary['signal_name_zh']}，事件 {int(primary['nonoverlap_events'])} 个，平均收益 {fmt_pct(primary['event_mean_return'])}。",
        f"保留至少 {policy['min_realtime_events']} 个事件的估值候选共有 {len(candidates)} 个，其中平均收益达到 2% 的候选 {len(over_line)} 个。",
        "估值状态是不同信息源，但对5日反弹窗口的边际解释不足；继续只加估值门槛不值得。",
    ]
    return {"main_diagnosis": notes[0], "next_iterations": notes, "recommended_next_direction": "下一步不要继续做单点过滤；应把目标改成更低频的风险暴露管理，或引入真正的交易行为数据。"}


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
        "final_verdict": "research_only；市场估值状态边界未证明有效反弹窗口",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], candidates: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.30 市场估值状态边界上限审计报告",
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
            "## 估值边界候选 Top 20",
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
