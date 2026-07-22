#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "configs" / "industry_fund_flow_ths_sw2_mapping.csv"
OUT = ROOT / "outputs" / "audit" / "fund_flow_mapping_remediation_v5_24"
DEBUG = OUT / "debug"

PROMOTIONS = {
    "公路铁路运输": ("801179", "铁路公路"),
    "港口航运": ("801992", "航运港口"),
    "塑料制品": ("801036", "塑料"),
    "橡胶制品": ("801037", "橡胶"),
    "油气开采及服务": ("801961", "油气开采Ⅱ"),
    "汽车服务及其他": ("801092", "汽车服务"),
    "煤炭开采加工": ("801951", "煤炭开采"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="V5.24 conservative remediation for THS-to-SW2 fund-flow mapping.")
    parser.add_argument("--apply", action="store_true", help="Apply the conservative mapping promotions to the config CSV.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    mapping = pd.read_csv(MAPPING, encoding="utf-8-sig")
    before = coverage(mapping)
    review = build_review(mapping)
    if args.apply:
        mapping = apply_promotions(mapping, review)
        mapping.to_csv(MAPPING, index=False, encoding="utf-8-sig")
    after = coverage(mapping)
    checks = build_checks(before, after, review)
    summary = build_summary(before, after, review, args.apply)
    write_outputs(summary, review, checks)
    print(f"output_dir={OUT}")
    print(f"applied={args.apply}")
    print(f"high_confidence_after={after['high_confidence_mapping_coverage']:.2%}")


def build_review(mapping: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for ths_name, (code, name) in PROMOTIONS.items():
        matched = mapping[mapping["ths_industry_name"].eq(ths_name)]
        if matched.empty:
            rows.append(row(ths_name, code, name, "missing_source_row", False, "映射表缺少同花顺行业。"))
            continue
        current = matched.iloc[0]
        code_ok = str(current.get("mapped_sw2_code", "")).zfill(6) == code
        name_ok = str(current.get("mapped_sw2_name", "")) == name
        confidence = float(current.get("mapping_confidence", 0) or 0)
        eligible = bool(code_ok and name_ok and confidence < 0.95)
        rows.append(row(
            ths_name,
            code,
            name,
            "eligible" if eligible else "skip",
            eligible,
            "词序/后缀差异，保守提升为高置信映射。" if eligible else f"code_ok={code_ok}; name_ok={name_ok}; confidence={confidence}",
        ))
    return pd.DataFrame(rows)


def row(ths_name: str, code: str, name: str, status: str, eligible: bool, note: str) -> dict[str, Any]:
    return {
        "ths_industry_name": ths_name,
        "target_sw2_code": code,
        "target_sw2_name": name,
        "review_status": status,
        "eligible_for_promotion": eligible,
        "note": note,
    }


def apply_promotions(mapping: pd.DataFrame, review: pd.DataFrame) -> pd.DataFrame:
    out = mapping.copy()
    for _, item in review[review["eligible_for_promotion"].eq(True)].iterrows():
        mask = out["ths_industry_name"].eq(item["ths_industry_name"])
        out.loc[mask, "mapping_method"] = "manual_semantic_equivalent"
        out.loc[mask, "mapping_confidence"] = 0.95
        out.loc[mask, "review_status"] = "manual_semantic_reviewed"
        out.loc[mask, "notes"] = "V5.24保守修复：词序或后缀差异；仍不允许自动交易，只用于PIT面板覆盖。"
    return out


def coverage(mapping: pd.DataFrame) -> dict[str, float]:
    confidence = pd.to_numeric(mapping["mapping_confidence"], errors="coerce")
    return {
        "row_count": float(len(mapping)),
        "high_confidence_mapping_coverage": float(confidence.ge(0.95).mean()),
        "exact_mapping_coverage": float(mapping["mapping_method"].isin(["exact", "normalized_exact", "manual_semantic_equivalent"]).mean()),
    }


def build_checks(before: dict[str, float], after: dict[str, float], review: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([
        check("promotion_whitelist_nonempty", "pass" if int(review["eligible_for_promotion"].sum()) else "fail", f"eligible={int(review['eligible_for_promotion'].sum())}", "只允许白名单保守提升。"),
        check("high_confidence_coverage_gate", "pass" if after["high_confidence_mapping_coverage"] >= 0.8 else "fail", f"before={before['high_confidence_mapping_coverage']:.2%}; after={after['high_confidence_mapping_coverage']:.2%}; required=80.00%", "资金流面板进入观察评估前需要 >=80% 高置信映射。"),
        check("auto_trading_still_disabled", "pass", "production_allowed unchanged", "映射提升不允许自动交易。"),
    ])


def check(name: str, status: str, evidence: str, meaning: str) -> dict[str, str]:
    return {"check": name, "status": status, "evidence": evidence, "meaning": meaning}


def build_summary(before: dict[str, float], after: dict[str, float], review: pd.DataFrame, applied: bool) -> dict[str, Any]:
    return {
        "version": "5.24.0",
        "policy_id": "fund_flow_mapping_remediation_v5_24",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "applied": applied,
        "eligible_promotion_count": int(review["eligible_for_promotion"].sum()),
        "high_confidence_mapping_coverage_before": before["high_confidence_mapping_coverage"],
        "high_confidence_mapping_coverage_after": after["high_confidence_mapping_coverage"],
        "mapping_gate_passed": after["high_confidence_mapping_coverage"] >= 0.8,
        "goal_ready": False,
        "can_claim_strong_rebound_industries": False,
        "production_ready": False,
        "auto_execution_allowed": False,
        "best_status": "research_only_mapping_gate_improved",
        "final_verdict": "V5.24 只修复保守同义映射，提升资金流面板的申万二级高置信覆盖；这仍不是强行业 alpha 证据。",
    }


def write_outputs(summary: dict[str, Any], review: pd.DataFrame, checks: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    review.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    write_json(OUT / "run_summary.json", summary)
    (OUT / "report.md").write_text(render_report(summary, review, checks), encoding="utf-8")
    review.to_csv(DEBUG / "mapping_promotions.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(DEBUG / "mapping_remediation_checks.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, Any], review: pd.DataFrame, checks: pd.DataFrame) -> str:
    return "\n".join([
        "# V5.24 资金流映射保守修复",
        "",
        summary["final_verdict"],
        "",
        f"- 是否已应用：`{str(summary['applied']).lower()}`",
        f"- 可提升映射数：{summary['eligible_promotion_count']}",
        f"- 高置信覆盖修复前：{summary['high_confidence_mapping_coverage_before']:.2%}",
        f"- 高置信覆盖修复后：{summary['high_confidence_mapping_coverage_after']:.2%}",
        f"- 映射门槛是否通过：`{str(summary['mapping_gate_passed']).lower()}`",
        "",
        "## 提升清单",
        "",
        review.to_markdown(index=False),
        "",
        "## 检查",
        "",
        checks.to_markdown(index=False),
        "",
        "边界：V5.24 只解决资金流行业名映射覆盖，不提供新的历史样本，也不证明强反弹行业选择有效。",
    ])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def self_check() -> None:
    sample = pd.DataFrame([{
        "ths_industry_name": "塑料制品",
        "mapped_sw2_code": "801036",
        "mapped_sw2_name": "塑料",
        "mapping_method": "difflib_suggestion",
        "mapping_confidence": 0.6667,
        "review_status": "manual_review_required",
        "production_allowed": "否",
        "notes": "",
    }])
    review = build_review(sample)
    assert int(review["eligible_for_promotion"].sum()) == 1
    fixed = apply_promotions(sample, review)
    assert float(fixed.loc[0, "mapping_confidence"]) == 0.95
    assert fixed.loc[0, "mapping_method"] == "manual_semantic_equivalent"
    print("self_check=pass")


if __name__ == "__main__":
    main()
