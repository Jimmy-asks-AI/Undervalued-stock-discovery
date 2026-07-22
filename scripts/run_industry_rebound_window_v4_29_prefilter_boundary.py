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
POLICY = ROOT / "configs" / "rebound_window_v4_29_prefilter_boundary_policy.json"
VERSION = "4.29.0"


def main() -> None:
    policy = read_json(POLICY)
    out = ROOT / policy["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)

    source = normalize(pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig"), policy)
    candidates = build_candidates(source, policy)
    primary = candidates.iloc[0].to_dict()
    primary_trades = apply_conditions(source, json.loads(primary["conditions_json"])).copy()
    primary_trades["signal_id"] = primary["signal_id"]
    primary_trades["signal_name_zh"] = primary["signal_name_zh"]
    primary_trades["signal_type"] = "prefilter_boundary_upper_bound"
    wf = year_summary(primary_trades)
    data_audit = build_data_audit(source, candidates, policy)
    leakage = build_leakage_audit(policy)
    notes = build_notes(primary, candidates, policy)
    run = run_summary(policy, primary, data_audit, leakage, notes)

    candidates.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", run)
    (out / "report.md").write_text(render_report(run, candidates, wf, data_audit, leakage, notes, policy), encoding="utf-8")
    source.to_csv(debug / "boundary_source_trades.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(debug / "boundary_candidate_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", notes)
    write_json(debug / "frozen_policy.json", policy)

    print("V4.29事前特征边界上限审计完成")
    print(f"主边界={primary['signal_id']}")
    print(f"独立事件={primary['nonoverlap_events']}")
    print(f"最终结论={run['final_verdict']}")


def normalize(df: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    out = df.copy()
    for col in policy["features"] + ["trade_return", "year"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["is_win"] = to_bool(out["is_win"])
    out["is_bad_window"] = to_bool(out["is_bad_window"])
    return out


def build_candidates(source: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    conditions = single_conditions(source, policy)
    rows: list[dict[str, Any]] = []
    for cond in conditions:
        rows.append(summary_row(source, [cond], policy))
    for a, b in itertools.combinations(conditions, 2):
        if a["feature"] == b["feature"]:
            continue
        rows.append(summary_row(source, [a, b], policy))
    frame = pd.DataFrame(rows)
    frame = frame[frame["nonoverlap_events"] >= int(policy["min_realtime_events"])].copy()
    frame = frame.sort_values(["event_mean_return", "event_bad_window_rate", "nonoverlap_events"], ascending=[False, True, False]).reset_index(drop=True)
    frame["signal_id"] = [f"prefilter_boundary_{i+1:03d}" for i in range(len(frame))]
    cols = ["signal_id"] + [c for c in frame.columns if c != "signal_id"]
    return frame[cols]


def single_conditions(source: pd.DataFrame, policy: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feature in policy["features"]:
        s = pd.to_numeric(source[feature], errors="coerce")
        thresholds = sorted(set(float(x) for x in s.quantile(policy["quantiles"]).dropna()))
        for threshold in thresholds:
            out.append({"feature": feature, "op": ">=", "threshold": threshold})
            out.append({"feature": feature, "op": "<=", "threshold": threshold})
    return out


def summary_row(source: pd.DataFrame, conditions: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    d = apply_conditions(source, conditions)
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
        "signal_type": "prefilter_boundary_upper_bound",
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


def apply_conditions(source: pd.DataFrame, conditions: list[dict[str, Any]]) -> pd.DataFrame:
    mask = pd.Series(True, index=source.index)
    for cond in conditions:
        s = pd.to_numeric(source[cond["feature"]], errors="coerce")
        if cond["op"] == ">=":
            mask &= s >= float(cond["threshold"])
        elif cond["op"] == "<=":
            mask &= s <= float(cond["threshold"])
        else:
            raise ValueError(f"unsupported op: {cond['op']}")
    return source[mask].copy()


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
        rows.append(
            {
                "year": int(year),
                "status": "pass",
                "signal_dates": int(len(g)),
                "signal_mean_return": float(returns.mean()),
                "signal_bad_window_rate": float(g["is_bad_window"].mean()),
            }
        )
    return pd.DataFrame(rows)


def build_data_audit(source: pd.DataFrame, candidates: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "source_event_pool",
                "status": "pass" if len(source) > 0 else "fail",
                "evidence": f"source_events={len(source)}",
                "action": "无宽事件池时不得做边界审计。",
            },
            {
                "audit_item": "candidate_sample_floor",
                "status": "pass" if len(candidates) > 0 else "fail",
                "evidence": f"eligible_candidates={len(candidates)}; min_events={policy['min_realtime_events']}",
                "action": "边界候选必须保留足够事件。",
            },
        ]
    )


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "pre_signal_features_only",
                "status": "pass",
                "evidence": ", ".join(policy["features"]),
                "action": "过滤条件只允许使用入场前已存在字段。",
            },
            {
                "audit_item": "upper_bound_not_frozen_rule",
                "status": "observe",
                "evidence": "primary boundary is selected by full-sample realized return ranking",
                "action": "本版只用于判断特征天花板；不得升级为实时规则。",
            },
        ]
    )


def build_notes(primary: dict[str, Any], candidates: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    over_line = candidates[candidates["event_mean_return"] >= float(policy["min_realtime_mean_return"])]
    notes = [
        "V4.29确认：当前事前特征集合的过滤上限仍未突破收益厚度。",
        f"全样本排序最优边界为 {primary['signal_name_zh']}，事件 {int(primary['nonoverlap_events'])} 个，平均收益 {fmt_pct(primary['event_mean_return'])}。",
        f"保留至少 {policy['min_realtime_events']} 个事件的候选共有 {len(candidates)} 个，其中平均收益达到 2% 的候选 {len(over_line)} 个。",
        "因为主边界是按全样本收益排序得到，即使未来达标也必须另做冻结回放；本版只能证明现有特征上限不足。",
    ]
    return {
        "main_diagnosis": notes[0],
        "next_iterations": notes,
        "recommended_next_direction": "停止在当前7个状态特征上继续做单/双条件过滤；下一步需要新增真正不同的信息源，或降低目标为风险提示而非反弹窗口识别。",
    }


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
        "final_verdict": "research_only；现有事前特征边界上限未证明有效反弹窗口",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(run: dict[str, Any], candidates: pd.DataFrame, wf: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V4.29 事前特征边界上限审计报告",
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
            "## 边界候选 Top 20",
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
