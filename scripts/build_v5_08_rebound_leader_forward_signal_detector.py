#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from append_v5_05_rebound_leader_forward_sample import LEDGER, append_row, build_row, read_rows, sample_key


ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "outputs" / "audit" / "rebound_leader_evidence_freeze_v5_04" / "top_candidates.csv"
EXPERIMENT_LEDGER = ROOT / "logs" / "research_experiment_ledger.jsonl"
SOURCE_PANEL = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "source_panel.csv"
HISTORY_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "history" / "second"
VALUATION_SNAPSHOT_DIR = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_snapshots" / "second"
OUT = ROOT / "outputs" / "audit" / "rebound_leader_forward_signal_detector_v5_08"
DEBUG = OUT / "debug"
TOP_N = 5
MIN_INDUSTRY_COVERAGE = 120
ENTRY_LAG_DAYS = 2
HOLDING_DAYS = 20
VOL_REPAIR_CONDITIONS = [
    ("market_volatility_20d_vs_60d", ">=", 1.05),
    ("liquidity_repair_5d", ">=", 0.03),
    ("market_return_10d", "<=", 0.03),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.08 detect post-freeze forward samples for frozen rebound-leader rules.")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true", help="Append eligible, non-duplicate samples to the frozen forward ledger.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    as_of = date.fromisoformat(args.as_of_date)
    freeze_date = forward_evidence_start(read_json_lines(EXPERIMENT_LEDGER))
    panel = pd.read_csv(SOURCE_PANEL, encoding="utf-8-sig")
    latest = build_live_events(panel, freeze_date, as_of, load_trade_calendar()).tail(1).copy()
    latest = add_quality_score(latest)
    checks = build_checks(latest, freeze_date, as_of)
    commands = build_append_commands(checks)
    candidates = build_selected_candidates(checks)
    appended = append_allowed_samples(checks) if args.apply else 0
    summary = build_summary(latest, checks, commands, freeze_date, as_of, appended, len(candidates))
    write_outputs(summary, latest, checks, commands, candidates)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"appendable_signal_count={summary['appendable_signal_count']}")


def add_quality_score(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    out["q_low_liquidity_repair"] = out["liquidity_repair_5d"].lt(0.08)
    out["q_low_positive_10d"] = out["industry_positive_10d_ratio"].lt(0.30)
    out["q_high_downside_concentration"] = out["industry_downside_concentration_20d"].ge(0.50)
    out["q_high_breadth_pressure"] = out["negative_breadth_60d"].ge(0.50)
    out["q_high_stress"] = out["market_stress_score"].ge(0.60)
    out["q_high_vol_ratio"] = out["market_volatility_20d_vs_60d"].ge(1.20)
    flags = [col for col in out.columns if col.startswith("q_")]
    out["window_quality_score"] = out[flags].sum(axis=1)
    return out


def build_live_events(panel: pd.DataFrame, freeze_date: date, as_of: date, calendar: list[date]) -> pd.DataFrame:
    frame = panel.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    mask = frame["trade_date"].gt(freeze_date) & frame["trade_date"].le(as_of)
    for field, operator, threshold in VOL_REPAIR_CONDITIONS:
        values = pd.to_numeric(frame[field], errors="coerce")
        mask &= values.ge(threshold) if operator == ">=" else values.le(threshold)
    candidates = frame[mask].sort_values("trade_date")
    calendar_index = {value: index for index, value in enumerate(calendar)}
    rows, cluster_end = [], None
    for _, candidate in candidates.iterrows():
        signal = candidate["trade_date"]
        index = calendar_index.get(signal)
        if index is None or index + ENTRY_LAG_DAYS + HOLDING_DAYS >= len(calendar):
            continue
        entry = calendar[index + ENTRY_LAG_DAYS]
        exit_ = calendar[index + ENTRY_LAG_DAYS + HOLDING_DAYS]
        if cluster_end is not None and entry <= cluster_end:
            cluster_end = max(cluster_end, exit_)
            continue
        row = candidate.to_dict()
        row.update({"signal_date": signal.isoformat(), "entry_date": entry.isoformat(), "exit_date": exit_.isoformat()})
        rows.append(row)
        cluster_end = exit_
    return pd.DataFrame(rows)


def load_trade_calendar() -> list[date]:
    import akshare as ak

    frame = ak.tool_trade_date_hist_sina()
    dates = sorted(set(pd.to_datetime(frame["trade_date"]).dt.date))
    if not dates:
        raise ValueError("empty A-share trade calendar")
    return dates


def build_checks(latest: pd.DataFrame, freeze_date: date, as_of: date) -> pd.DataFrame:
    frozen = pd.read_csv(FROZEN, encoding="utf-8-sig")
    rows = []
    if latest.empty:
        return pd.DataFrame([{
            "frozen_rule": rule["frozen_rule"], "signal_date": "", "entry_date": "", "exit_date": "",
            "freeze_date": freeze_date.isoformat(), "window_quality_score": 0,
            "required_quality_score": 3 if str(rule["frozen_rule"]).endswith("ge3") else 2,
            "rule_triggered": False, "post_freeze_signal": False, "detected_before_entry": False,
            "selection_ready": False, "append_allowed": False, "selected_industries": "",
            "block_reason": "no_live_signal_after_freeze",
        } for _, rule in frozen.iterrows()])
    signal_date = pd.to_datetime(latest.iloc[0]["signal_date"]).date()
    entry_date = pd.to_datetime(latest.iloc[0]["entry_date"]).date()
    selected = latest_beta_top5(signal_date)
    selection_ready = len(selected) == TOP_N
    for _, rule in frozen.iterrows():
        threshold = 3 if str(rule["frozen_rule"]).endswith("ge3") else 2
        triggered = int(latest.iloc[0]["window_quality_score"]) >= threshold
        post_freeze = signal_date > freeze_date
        observed_before_entry = as_of <= entry_date
        rows.append({
            "frozen_rule": rule["frozen_rule"],
            "signal_date": latest.iloc[0]["signal_date"],
            "entry_date": latest.iloc[0]["entry_date"],
            "exit_date": latest.iloc[0]["exit_date"],
            "freeze_date": freeze_date.isoformat(),
            "window_quality_score": int(latest.iloc[0]["window_quality_score"]),
            "required_quality_score": threshold,
            "rule_triggered": triggered,
            "post_freeze_signal": post_freeze,
            "detected_before_entry": observed_before_entry,
            "selection_ready": selection_ready,
            "append_allowed": triggered and post_freeze and observed_before_entry and selection_ready,
            "selected_industries": "|".join(selected),
            "block_reason": block_reason(triggered, post_freeze, observed_before_entry, selection_ready),
        })
    return pd.DataFrame(rows)


def latest_beta_top5(signal_date: date) -> list[str]:
    pieces = []
    for path in HISTORY_DIR.glob("*.csv"):
        frame = pd.read_csv(path, encoding="utf-8-sig", usecols=["代码", "日期", "收盘"], dtype={"代码": str})
        frame["trade_date"] = pd.to_datetime(frame["日期"])
        frame = frame[frame["trade_date"].le(pd.Timestamp(signal_date))].tail(121).copy()
        if frame.empty or frame["trade_date"].max() != pd.Timestamp(signal_date):
            continue
        frame["industry_code"] = frame["代码"].str.zfill(6)
        frame["ret"] = (pd.to_numeric(frame["收盘"], errors="coerce").pct_change() * 100).round(2) / 100
        pieces.append(frame[["trade_date", "industry_code", "ret"]])
    if not pieces:
        return []
    hist = pd.concat(pieces, ignore_index=True)
    hist = hist.merge(hist.groupby("trade_date")["ret"].mean().rename("market_ret"), on="trade_date", how="left")
    rows = []
    for industry_code, group in hist.groupby("industry_code"):
        group = group.dropna(subset=["ret", "market_ret"]).tail(120)
        variance = group["market_ret"].var()
        if len(group) >= 40 and variance:
            rows.append({"industry_code": industry_code, "beta_120": group["ret"].cov(group["market_ret"]) / variance})
    sample = pd.DataFrame(rows).dropna(subset=["beta_120"])
    if len(sample) < MIN_INDUSTRY_COVERAGE:
        return []
    snapshots = list(VALUATION_SNAPSHOT_DIR.glob("*.csv"))
    if not snapshots:
        return []
    snapshot = pd.read_csv(max(snapshots), encoding="utf-8-sig", dtype={"行业代码": str})
    names = snapshot[["行业代码", "行业名称"]].rename(columns={"行业代码": "industry_code", "行业名称": "industry_name"})
    names["industry_code"] = names["industry_code"].str.zfill(6)
    sample = sample.merge(names, on="industry_code", how="left").dropna(subset=["industry_name"])
    sample["beta_120_rank"] = sample["beta_120"].rank(pct=True)
    return sample.sort_values("beta_120_rank", ascending=False).head(TOP_N)["industry_name"].astype(str).tolist()


def block_reason(triggered: bool, post_freeze: bool, observed_before_entry: bool, selection_ready: bool = True) -> str:
    if not post_freeze:
        return "signal_not_after_freeze_date"
    if not triggered:
        return "frozen_rule_not_triggered"
    if not observed_before_entry:
        return "detected_after_entry_date"
    if not selection_ready:
        return "industry_data_not_available_as_of_signal"
    return ""


def forward_evidence_start(rows: list[dict[str, Any]]) -> date:
    dates = [date.fromisoformat(row["evidence_start_date"]) for row in rows if row.get("registration_status") == "preregistered_forward_only"]
    if not dates:
        raise ValueError("no preregistered forward-only experiment")
    return max(dates)


def append_allowed_samples(checks: pd.DataFrame) -> int:
    existing = {sample_key(row) for row in read_rows(LEDGER)}
    allowed = set(checks["frozen_rule"].astype(str))
    appended = 0
    for _, item in checks[checks["append_allowed"]].iterrows():
        args = argparse.Namespace(
            frozen_rule=str(item["frozen_rule"]), signal_date=str(item["signal_date"]),
            entry_date=str(item["entry_date"]), exit_date=str(item["exit_date"]),
            selected_industries=str(item["selected_industries"]), benchmark_return="", selected_net_return="",
            relative_return="", top_quintile_hit_rate="", settlement_status="pending",
            notes="auto-appended by immutable forward boundary",
        )
        row = build_row(args, allowed)
        key = sample_key(row)
        if key in existing:
            continue
        append_row(LEDGER, row)
        existing.add(key)
        appended += 1
    return appended


def build_append_commands(checks: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in checks[checks["append_allowed"]].iterrows():
        rows.append({
            "frozen_rule": row["frozen_rule"],
            "command": (
                f"python .\\scripts\\append_v5_05_rebound_leader_forward_sample.py "
                f"--frozen-rule {row['frozen_rule']} --signal-date {row['signal_date']} "
                f"--entry-date {row['entry_date']} --exit-date {row['exit_date']} "
                f"--selected-industries \"{row['selected_industries']}\""
            ),
        })
    return pd.DataFrame(rows, columns=["frozen_rule", "command"])


def build_selected_candidates(checks: pd.DataFrame) -> pd.DataFrame:
    columns = ["trade_date", "industry_code", "industry_name", "frozen_rule"]
    selected = checks[checks["append_allowed"]]
    if selected.empty:
        return pd.DataFrame(columns=columns)
    snapshot = pd.read_csv(max(VALUATION_SNAPSHOT_DIR.glob("*.csv")), encoding="utf-8-sig", dtype={"行业代码": str})
    name_to_code = dict(zip(snapshot["行业名称"], snapshot["行业代码"].str.zfill(6)))
    rows = []
    for item in selected.to_dict("records"):
        for name in str(item["selected_industries"]).split("|"):
            if name in name_to_code:
                rows.append({"trade_date": item["signal_date"], "industry_code": name_to_code[name],
                             "industry_name": name, "frozen_rule": item["frozen_rule"]})
    return pd.DataFrame(rows, columns=columns).drop_duplicates(columns).reset_index(drop=True)


def build_summary(latest: pd.DataFrame, checks: pd.DataFrame, commands: pd.DataFrame, freeze_date: date,
                  as_of: date, appended: int, selected_industry_count: int) -> dict[str, Any]:
    return {
        "version": "5.08.0",
        "policy_id": "rebound_leader_forward_signal_detector_v5_08",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "freeze_date": freeze_date.isoformat(),
        "latest_signal_date": str(latest.iloc[0]["signal_date"]) if len(latest) else "",
        "latest_window_quality_score": int(latest.iloc[0]["window_quality_score"]) if len(latest) else 0,
        "triggered_rule_count": int(checks["rule_triggered"].sum()) if len(checks) else 0,
        "appendable_signal_count": int(checks["append_allowed"].sum()) if len(checks) else 0,
        "append_command_count": int(len(commands)),
        "appended_signal_count": appended,
        "selected_industry_count": selected_industry_count,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_no_appendable_forward_signal" if commands.empty else "research_only_appendable_forward_signal",
        "final_verdict": "V5.08 未发现冻结日之后可追加的前推样本；不能声称目标完成。" if commands.empty else "V5.08 发现可追加前推样本，但仍需结算后再评价。",
    }


def write_outputs(summary: dict[str, Any], latest: pd.DataFrame, checks: pd.DataFrame, commands: pd.DataFrame,
                  candidates: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    checks.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, checks, commands), encoding="utf-8")
    latest.to_csv(DEBUG / "latest_window_state.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(DEBUG / "frozen_rule_trigger_check.csv", index=False, encoding="utf-8-sig")
    commands.to_csv(DEBUG / "candidate_append_commands.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(DEBUG / "selected_industry_candidates.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], checks: pd.DataFrame, commands: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.08 冻结规则前推信号检测器",
        "",
        summary["final_verdict"],
        "",
        f"- 冻结日期：{summary['freeze_date']}",
        f"- 检测日期：{summary['as_of_date']}",
        f"- 最新信号日期：{summary['latest_signal_date']}",
        f"- 最新窗口质量分：{summary['latest_window_quality_score']}",
        f"- 触发冻结规则数：{summary['triggered_rule_count']}",
        f"- 可追加前推样本数：{summary['appendable_signal_count']}",
        f"- 本次实际追加数：{summary['appended_signal_count']}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 触发检查",
        "",
        checks.to_markdown(index=False),
        "",
        "## 追加命令",
        "",
        commands.to_markdown(index=False) if len(commands) else "无可追加命令",
        "",
        "边界：检测器只读取截至检测日的实时源面板和信号日可见的历史行情；信号必须晚于冻结日且在入场前被检测，才允许进入前推账本。",
    ])


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    sample = pd.DataFrame({
        "liquidity_repair_5d": [0.07], "industry_positive_10d_ratio": [0.2],
        "industry_downside_concentration_20d": [0.6], "negative_breadth_60d": [0.6],
        "market_stress_score": [0.7], "market_volatility_20d_vs_60d": [1.3],
    })
    scored = add_quality_score(sample)
    assert int(scored["window_quality_score"].iloc[0]) == 6
    panel = pd.DataFrame({
        "trade_date": ["2026-07-13", "2026-07-14", "2026-08-03"],
        "market_volatility_20d_vs_60d": [1.10, 1.10, 1.10],
        "liquidity_repair_5d": [0.04, 0.04, 0.04], "market_return_10d": [0.01, 0.01, 0.01],
    })
    calendar = [date(2026, 7, day) for day in range(13, 32)] + [date(2026, 8, day) for day in range(1, 32)]
    events = build_live_events(panel, date(2026, 7, 12), date(2026, 8, 6), calendar)
    assert events.iloc[0]["entry_date"] == "2026-07-15" and events.iloc[0]["exit_date"] == "2026-08-04"
    assert len(events) == 1
    assert block_reason(True, False, True) == "signal_not_after_freeze_date"
    assert block_reason(True, True, False) == "detected_after_entry_date"
    assert block_reason(True, True, True, False) == "industry_data_not_available_as_of_signal"
    assert forward_evidence_start([{"registration_status": "preregistered_forward_only", "evidence_start_date": "2026-07-12"}]) == date(2026, 7, 12)
    print("self_check=pass")


if __name__ == "__main__":
    main()
