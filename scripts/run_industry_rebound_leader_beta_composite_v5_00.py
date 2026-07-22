#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import run_industry_rebound_leader_market_sensitivity_v4_99 as v499


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs" / "industry_rebound_leader_market_sensitivity_v4_99" / "debug" / "market_sensitivity_opportunity_set.csv"
OUT = ROOT / "outputs" / "industry_rebound_leader_beta_composite_v5_00"
DEBUG = OUT / "debug"

COMPOSITES = {
    "beta_120_rank": None,
    "beta_oversold_score": ("beta_120_rank", 0.60, "oversold_score", 0.40),
    "beta_oversold_liquidity": ("beta_120_rank", 0.60, "oversold_liquidity_score", 0.40),
    "beta_turn_score": ("beta_120_rank", 0.60, "turn_score", 0.40),
    "beta_liquidity_score": ("beta_120_rank", 0.60, "liquidity_score", 0.40),
    "beta_value_oversold_turn": ("beta_120_rank", 0.50, "value_oversold_turn_score", 0.50),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.00 beta composite rebound-leader audit.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    frame = build_frame()
    old_features = v499.FEATURES
    v499.FEATURES = list(COMPOSITES)
    try:
        event_panel = v499.evaluate_strategies(frame)
        results = v499.summarize_strategies(event_panel)
    finally:
        v499.FEATURES = old_features
    gate = v499.gate_audit(results)
    summary = build_summary(results, gate)
    write_outputs(summary, frame, event_panel, results, gate)
    print(f"output_dir={OUT}")
    print(f"best_status={summary['best_status']}")
    print(f"best_feature={summary['best_feature']}")


def build_frame() -> pd.DataFrame:
    frame = pd.read_csv(SOURCE, encoding="utf-8-sig", dtype={"industry_code": str})
    for name, spec in COMPOSITES.items():
        if spec is None:
            continue
        left, lw, right, rw = spec
        frame[name] = lw * frame[left] + rw * frame[right]
    return frame


def build_summary(results: pd.DataFrame, gate: pd.DataFrame) -> dict[str, Any]:
    best = results.iloc[0].to_dict() if len(results) else {}
    passed = bool(best.get("passes_gate", False))
    return {
        "version": "5.00.0",
        "policy_id": "industry_rebound_leader_beta_composite_v5_00",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_variant": "vol_repair",
        "tested_feature_count": len(COMPOSITES),
        "best_feature": best.get("feature", ""),
        "best_top_n": int(best.get("top_n", 0) or 0),
        "best_mean_relative_return": num(best.get("mean_relative_return")),
        "best_top_quintile_hit_rate": num(best.get("top_quintile_hit_rate")),
        "best_bootstrap_top_quintile_hit_p05": num(best.get("bootstrap_top_quintile_hit_p05")),
        "best_bootstrap_positive_year_p05": num(best.get("bootstrap_positive_year_p05")),
        "passing_rule_count": int(results["passes_gate"].sum()) if len(results) else 0,
        "failed_metrics": ";".join(gate[gate["status"].eq("fail")]["metric"].tolist()) if len(gate) else "no_results",
        "best_status": "pass_beta_composite_leader_gate" if passed else "research_only_no_beta_composite_alpha",
        "can_claim_strong_rebound_industries": passed,
        "production_ready": False,
        "auto_execution_allowed": False,
        "final_verdict": "V5.00 测试 beta 与超跌/流动性/企稳组合，未优于单独 beta_120_rank，也未通过完整稳健门槛。",
    }


def write_outputs(summary: dict[str, Any], frame: pd.DataFrame, event_panel: pd.DataFrame, results: pd.DataFrame, gate: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "run_summary.json", summary)
    results.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.md").write_text(render_report(summary, results, gate), encoding="utf-8")
    frame.to_csv(DEBUG / "beta_composite_opportunity_set.csv", index=False, encoding="utf-8-sig")
    event_panel.to_csv(DEBUG / "strategy_event_panel.csv", index=False, encoding="utf-8-sig")
    results.to_csv(DEBUG / "strategy_results.csv", index=False, encoding="utf-8-sig")
    gate.to_csv(DEBUG / "evaluation_gate_audit.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], results: pd.DataFrame, gate: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.00 Beta 组合强行业回测",
        "",
        summary["final_verdict"],
        "",
        "## 核心结论",
        "",
        f"- 窗口定义：`{summary['window_variant']}`",
        f"- 测试特征数：{summary['tested_feature_count']}",
        f"- 最接近规则：`{summary['best_feature']}` Top{summary['best_top_n']}",
        f"- 平均相对收益：{pct(summary['best_mean_relative_return'])}",
        f"- Top20% 命中率：{pct(summary['best_top_quintile_hit_rate'])}",
        f"- bootstrap Top20% 命中率 5% 下界：{pct(summary['best_bootstrap_top_quintile_hit_p05'])}",
        f"- 是否可声称找到强反弹行业：`{str(summary['can_claim_strong_rebound_industries']).lower()}`",
        "",
        "## 最优规则门槛",
        "",
        gate.to_markdown(index=False) if len(gate) else "无数据",
        "",
        "## 策略结果",
        "",
        results.to_markdown(index=False) if len(results) else "无数据",
        "",
        "## 研究边界",
        "",
        "V5.00 只测试少量事前可解释组合，不做大网格调参，不生成交易指令。",
    ])


def pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def num(value: Any) -> float:
    return float(value) if pd.notna(value) else 0.0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    sample = pd.DataFrame({"beta_120_rank": [0.5], "oversold_score": [0.25]})
    spec = COMPOSITES["beta_oversold_score"]
    assert spec is not None
    left, lw, right, rw = spec
    sample["beta_oversold_score"] = lw * sample[left] + rw * sample[right]
    assert abs(float(sample["beta_oversold_score"].iloc[0]) - 0.4) < 1e-12
    print("self_check=pass")


if __name__ == "__main__":
    main()
