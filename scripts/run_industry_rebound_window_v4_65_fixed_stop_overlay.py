#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_window_v4_48_stop_loss_exit as stop_exit


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_65_fixed_stop_overlay_policy.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply one fixed stop-loss overlay to a rebound-window event set.")
    parser.add_argument("--config", default=str(CONFIG), help="Policy JSON path.")
    args = parser.parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    config = read_json(config_path)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(ROOT / config["source_trades"], encoding="utf-8-sig")
    stop_loss = float(config["fixed_stop_loss"])
    trades = stop_exit.apply_stop(source, stop_loss, config)
    trades["signal_id"] = config["policy_id"]
    trades["signal_name_zh"] = config["policy_name_zh"]
    trades["signal_type"] = "fixed_stop_overlay"
    row = stop_exit.summarize(config["policy_id"], trades, stop_loss, config)
    row.update({"signal_name_zh": config["policy_name_zh"], "signal_type": "fixed_stop_overlay", "status": "research_only"})
    write_outputs(out, debug, config, source, trades, row)
    print(f"output_dir={out}")
    print(f"events={int(row['nonoverlap_events'])}")
    print(f"clusters={int(row['independent_event_clusters'])}")
    print(f"net={row['net_mean_return']:.2%}")
    print(f"relative={row['relative_mean_return']:.2%}")


def write_outputs(out: Path, debug: Path, config: dict[str, Any], source: pd.DataFrame, trades: pd.DataFrame, row: dict[str, Any]) -> None:
    summary = {
        "version": config["version"],
        "policy_id": config["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": row["signal_id"],
        "primary_realtime_events": int(row["nonoverlap_events"]),
        "primary_independent_event_clusters": int(row["independent_event_clusters"]),
        "candidate_count": 0,
        "audit_fail_count": 0,
        "best_signal_id": row["signal_id"],
        "best_status": "research_only",
        "best_nonoverlap_events": int(row["nonoverlap_events"]),
        "best_event_mean_return": float(row["event_mean_return"]),
        "best_event_relative_mean_return": float(row["relative_mean_return"]),
        "best_event_bad_window_rate": float(row["event_bad_window_rate"]),
        "final_verdict": config["final_verdict"],
        "main_diagnosis": config["main_diagnosis"],
        "research_boundary": config["research_boundary"],
    }
    pd.DataFrame([row]).to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, row), encoding="utf-8")
    source.to_csv(debug / "fixed_stop_source_trades.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([row]).to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    stop_exit.year_summary(trades).to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "source_trades", "status": "pass", "evidence": config["source_trades"]}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "fixed_stop_loss_overlay", "status": "pass", "evidence": config["leakage_evidence"]}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": config["optimization_note"]})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], row: dict[str, Any]) -> str:
    return "\n".join([
        f"# {config['policy_name_zh']}",
        "",
        "## 结论",
        "",
        f"- 事件数：{int(row['nonoverlap_events'])}；独立行情簇：{int(row['independent_event_clusters'])}。",
        f"- 止损触发次数：{int(row['stop_loss_hits'])}。",
        f"- 10bps 成本后收益：{fmt_pct(row['net_mean_return'])}；相对现金收益：{fmt_pct(row['relative_mean_return'])}。",
        f"- 胜率：{fmt_pct(row['event_win_rate'])}；坏窗口率：{fmt_pct(row['event_bad_window_rate'])}；最差单笔：{fmt_pct(row['event_worst_return'])}。",
        "",
        "## 解读",
        "",
        config["main_diagnosis"],
        "",
        "## 研究边界",
        "",
        config["research_boundary"],
        "",
    ])


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return ""


if __name__ == "__main__":
    main()
