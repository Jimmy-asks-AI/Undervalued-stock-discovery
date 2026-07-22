#!/usr/bin/env python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "rebound_window_v4_58_filter_branch_review_policy.json"


def main() -> None:
    config = read_json(CONFIG)
    out = ROOT / config["output_dir"]
    debug = out / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    rows = [summary_row(path) for path in config["review_outputs"]]
    comparison = pd.DataFrame(rows)
    primary_dir = ROOT / config["primary_source_output"]
    primary_summary = read_json(primary_dir / "run_summary.json")
    primary_eval = read_json(primary_dir / "debug" / "evaluation_summary.json")
    trades = pd.read_csv(primary_dir / "debug" / "realtime_simulation_trades.csv", encoding="utf-8-sig")
    sim_summary = pd.read_csv(primary_dir / "debug" / "realtime_simulation_summary.csv", encoding="utf-8-sig")
    sim_summary["signal_id"] = "v4_58_filter_branch_review_primary_v4_57"
    trades["signal_id"] = "v4_58_filter_branch_review_primary_v4_57"
    write_outputs(out, debug, config, comparison, primary_summary, primary_eval, trades, sim_summary)
    print(f"output_dir={out}")
    print(f"primary_status={primary_eval['evaluation_status']}")
    print(f"primary_score={primary_eval['score']}")
    print(f"effective={primary_eval['is_effective']}")


def summary_row(output_path: str) -> dict[str, Any]:
    output_dir = ROOT / output_path
    run = read_json(output_dir / "run_summary.json")
    ev = read_json(output_dir / "debug" / "evaluation_summary.json")
    k = ev["key_metrics"]
    return {
        "output_dir": output_path,
        "version": run.get("version"),
        "policy_id": run.get("policy_id"),
        "evaluation_status": ev.get("evaluation_status"),
        "score": ev.get("score"),
        "is_effective": ev.get("is_effective"),
        "events": k.get("realtime_events"),
        "clusters": k.get("independent_event_clusters"),
        "net_mean_return": k.get("realtime_net_mean_return"),
        "relative_mean_return": k.get("realtime_relative_mean_return"),
        "cluster_net_mean_return": k.get("cluster_net_mean_return"),
        "cluster_relative_mean_return": k.get("cluster_relative_mean_return"),
    }


def write_outputs(
    out: Path,
    debug: Path,
    config: dict[str, Any],
    comparison: pd.DataFrame,
    primary_summary: dict[str, Any],
    primary_eval: dict[str, Any],
    trades: pd.DataFrame,
    sim_summary: pd.DataFrame,
) -> None:
    summary = {
        "version": config["version"],
        "policy_id": config["policy_id"],
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "primary_signal_id": "v4_58_filter_branch_review_primary_v4_57",
        "primary_realtime_events": primary_eval["key_metrics"]["realtime_events"],
        "primary_independent_event_clusters": primary_eval["key_metrics"]["independent_event_clusters"],
        "candidate_count": int(len(comparison)),
        "audit_fail_count": 0,
        "best_signal_id": "v4_58_filter_branch_review_primary_v4_57",
        "best_status": "research_only",
        "best_nonoverlap_events": primary_eval["key_metrics"]["realtime_events"],
        "best_event_mean_return": primary_eval["key_metrics"]["realtime_mean_return"],
        "best_event_relative_mean_return": primary_eval["key_metrics"]["realtime_relative_mean_return"],
        "best_event_bad_window_rate": primary_eval["key_metrics"]["realtime_bad_window_rate"],
        "final_verdict": "research_only；过滤分支应停止继续加复杂度。",
        "main_diagnosis": "V4.55/V4.56 过滤压缩样本且收益不足，V4.57 年前滚选择多数年份回到不滤。",
        "research_boundary": config["research_boundary"],
        "source_primary_version": primary_summary.get("version"),
    }
    comparison.to_csv(out / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(out / "run_summary.json", summary)
    (out / "report.md").write_text(render_report(config, comparison, primary_eval), encoding="utf-8")
    comparison.to_csv(debug / "filter_branch_comparison.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(debug / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    sim_summary.to_csv(debug / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    pd.read_csv(ROOT / config["primary_source_output"] / "debug" / "walk_forward_year_summary.csv", encoding="utf-8-sig").to_csv(debug / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "review_outputs", "status": "pass", "evidence": "|".join(config["review_outputs"])}]).to_csv(debug / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"item": "fixed_primary_v4_57", "status": "pass", "evidence": "V4.58 主评价口径固定引用 V4.57，不从对照版本中挑最好结果。"}]).to_csv(debug / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug / "optimization_notes.json", {"note": "ponytail: 这是分支终止审计，不新增交易逻辑；继续加过滤复杂度的边际价值已很低。"})
    write_json(debug / "frozen_policy.json", config)


def render_report(config: dict[str, Any], comparison: pd.DataFrame, primary_eval: dict[str, Any]) -> str:
    rows = [
        f"- {r.version}: 事件 {int(r.events)}，独立簇 {int(r.clusters)}，成本后 {fmt_pct(r.net_mean_return)}，相对 {fmt_pct(r.relative_mean_return)}，状态 {r.evaluation_status}。"
        for r in comparison.itertuples()
    ]
    return "\n".join([
        "# V4.58 过滤分支终止审计",
        "",
        "## 结论",
        "",
        "过滤分支不应继续加复杂度。V4.57 年前滚家族选择已经多数年份回到不滤，说明单特征过滤和投票过滤没有稳定增量。",
        "",
        f"- 主口径状态：{primary_eval['evaluation_status']}；V3.2 分数：{primary_eval['score']}；effective={primary_eval['is_effective']}。",
        "",
        "## 分支对照",
        "",
        *rows,
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
