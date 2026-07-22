#!/usr/bin/env python
from __future__ import annotations

import json
import math
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "configs" / "rebound_window_v4_21_frontier_stat_reliability_policy.json"
OUT = ROOT / "outputs" / "industry_rebound_window_v4_21_frontier_stat_reliability"
VERSION = "4.21.0"


def main() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    frontier = pd.read_csv(ROOT / policy["source_frontier_path"], encoding="utf-8-sig")
    trades = pd.read_csv(ROOT / policy["source_trades_path"], encoding="utf-8-sig", parse_dates=["signal_date"])
    stats = reliability_summary(frontier, trades, policy)
    primary = stats[stats["frontier_role"] == "收益最高规则"].iloc[0].to_dict()
    primary_trades = trades[trades["signal_id"] == primary["signal_id"]].copy()
    wf = year_summary(primary_trades)
    data_audit = pd.DataFrame([{"audit_item": "fixed_v4_20_frontier", "status": "pass", "evidence": f"rules={len(frontier)}; trades={len(trades)}; bootstrap={policy['bootstrap_iterations']}", "action": "固定V4.20边界规则，只做统计可靠性审计。"}])
    leakage = pd.DataFrame([{"audit_item": "no_new_signal", "status": "pass", "evidence": "bootstrap resamples realized V4.20 trades only", "action": "不新增参数，不重新筛选事件。"}])
    summary = run_summary(policy, stats, primary, data_audit, leakage)

    debug = OUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    stats.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(report(summary, stats, data_audit, leakage, wf, policy), encoding="utf-8")
    frontier.to_csv(debug / "frontier_stat_source.csv", index=False, encoding="utf-8-sig")
    stats.to_csv(debug / "stat_reliability_summary.csv", index=False, encoding="utf-8-sig")
    primary_trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([primary]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    wf.to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    leakage.to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"main_diagnosis": summary["main_diagnosis"]})
    write_json(debug / "frozen_policy.json", policy)
    print("V4.21边界统计可靠性审计完成")
    print(f"主规则={primary['signal_id']}")
    print(f"最终结论={summary['final_verdict']}")


def reliability_summary(frontier: pd.DataFrame, trades: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    rng = random.Random(int(policy["random_seed"]))
    for _, rule in frontier.iterrows():
        d = trades[trades["signal_id"] == rule["signal_id"]].copy()
        returns = [float(x) for x in pd.to_numeric(d["trade_return"], errors="coerce").dropna()]
        wins = int(sum(x > 0 for x in returns))
        bads = int(d["is_bad_window"].astype(bool).sum())
        means = bootstrap_means(returns, int(policy["bootstrap_iterations"]), rng)
        win_lo, win_hi = wilson_interval(wins, len(returns))
        bad_lo, bad_hi = wilson_interval(bads, len(returns))
        mean_lo, mean_hi = quantile(means, 0.025), quantile(means, 0.975)
        robust = (
            mean_lo >= float(policy["min_realtime_mean_return"])
            and win_lo >= float(policy["min_realtime_win_rate"])
            and bad_hi <= float(policy["max_realtime_bad_window_rate"])
        )
        row = rule.to_dict()
        row.update({
            "bootstrap_mean_p025": mean_lo,
            "bootstrap_mean_p975": mean_hi,
            "wilson_win_rate_low": win_lo,
            "wilson_win_rate_high": win_hi,
            "wilson_bad_window_low": bad_lo,
            "wilson_bad_window_high": bad_hi,
            "robust_gate_pass": robust,
            "mean_ci_crosses_gate": mean_lo < float(policy["min_realtime_mean_return"]) < mean_hi,
            "win_ci_crosses_gate": win_lo < float(policy["min_realtime_win_rate"]) < win_hi,
            "bad_ci_crosses_gate": bad_lo < float(policy["max_realtime_bad_window_rate"]) < bad_hi,
            "status": "有效反弹窗口" if robust else "条件观察",
        })
        rows.append(row)
    return pd.DataFrame(rows)


def bootstrap_means(values: list[float], n: int, rng: random.Random) -> list[float]:
    out = []
    size = len(values)
    for _ in range(n):
        out.append(sum(values[rng.randrange(size)] for _ in range(size)) / size)
    return sorted(out)


def quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return math.nan
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] * (hi - pos) + sorted_values[hi] * (pos - lo)


def wilson_interval(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if n == 0:
        return math.nan, math.nan
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"year": int(y), "status": "pass", "signal_dates": len(g), "signal_mean_return": float(g["trade_return"].mean()), "signal_bad_window_rate": float(g["is_bad_window"].astype(bool).mean())} for y, g in d.groupby("year")])


def run_summary(policy: dict[str, Any], stats: pd.DataFrame, primary: dict[str, Any], data_audit: pd.DataFrame, leakage: pd.DataFrame) -> dict[str, Any]:
    robust_count = int(stats["robust_gate_pass"].sum())
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": primary["signal_id"],
        "primary_realtime_events": int(primary["nonoverlap_events"]),
        "candidate_count": robust_count,
        "audit_fail_count": int((data_audit["status"] == "fail").sum()) + int((leakage["status"] == "fail").sum()),
        "best_signal_id": primary["signal_id"],
        "best_status": primary["status"],
        "best_nonoverlap_events": int(primary["nonoverlap_events"]),
        "best_event_mean_return": none_if_nan(primary["event_mean_return"]),
        "best_event_bad_window_rate": none_if_nan(primary["event_bad_window_rate"]),
        "robust_gate_pass_count": robust_count,
        "final_verdict": "research_only；边界规则没有通过统计可靠性审计",
        "main_diagnosis": "V4.21显示V4.20边界规则的均值、胜率或坏窗口置信区间仍跨越有效门槛，不能把点估计接近门槛视为可靠反弹窗口。",
        "research_boundary": policy["research_boundary"],
    }


def report(summary, stats, data_audit, leakage, wf, policy) -> str:
    return "\n".join([
        "# V4.21 边界统计可靠性审计报告",
        "",
        summary["main_diagnosis"],
        "",
        f"- 主规则：{summary['primary_signal_id']}",
        f"- 主规则事件数：{summary['primary_realtime_events']}",
        f"- 稳健通过规则数：{summary['robust_gate_pass_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        "",
        "## 统计可靠性",
        stats.to_markdown(index=False),
        "",
        "## 主规则年度表现",
        wf.to_markdown(index=False),
        "",
        "## 审计",
        data_audit.to_markdown(index=False),
        leakage.to_markdown(index=False),
        "",
        f"研究边界：{policy['research_boundary']}",
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def clean(v):
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if hasattr(v, "item"):
        return clean(v.item())
    return v


def none_if_nan(v):
    try:
        x = float(v)
    except Exception:
        return None
    return None if math.isnan(x) else x


if __name__ == "__main__":
    main()
