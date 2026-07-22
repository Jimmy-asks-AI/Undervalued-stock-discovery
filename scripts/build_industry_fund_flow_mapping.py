from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
THS_SNAPSHOT = ROOT / "data_catalog" / "cache" / "industry_fund_flow" / "ths" / "2026-06-19" / "ths_industry_fund_flow_now.csv"
SW2_VALUATION = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"
MAPPING = ROOT / "configs" / "industry_fund_flow_ths_sw2_mapping.csv"
OUT = ROOT / "outputs" / "audit" / "industry_fund_flow_mapping_audit"
DEBUG = OUT / "debug"
MANUAL_CURRENT_OBSERVATION = {
    "汽车整车": ("801095", "乘用车", 0.75, "THS汽车整车近似申万乘用车，商用车混入时需复核。"),
    "零售": ("801203", "一般零售", 0.7, "THS零售近似申万一般零售。"),
    "旅游及酒店": ("801993", "旅游及景区", 0.65, "THS含酒店口径。"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build THS industry to SW second-level mapping draft.")
    parser.add_argument("--ths-snapshot", type=Path, default=THS_SNAPSHOT)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    mapping = build_mapping(args.ths_snapshot, SW2_VALUATION)
    write_outputs(mapping)
    exact = int(mapping["review_status"].eq("auto_exact_match").sum())
    print(f"mapping_file={MAPPING}")
    print(f"exact_matches={exact}/{len(mapping)}")
    print("production_ready=False")


def build_mapping(ths_path: Path, sw2_path: Path) -> pd.DataFrame:
    ths = pd.read_csv(ths_path, encoding="utf-8-sig")
    sw2 = pd.read_csv(sw2_path, encoding="utf-8-sig", usecols=["industry_code", "industry_name"])
    sw2["industry_code"] = sw2["industry_code"].astype(str).str.zfill(6)
    sw2 = sw2.drop_duplicates("industry_name").sort_values("industry_name")
    sw_by_name = sw2.set_index("industry_name")["industry_code"].to_dict()
    sw_names = sw2["industry_name"].astype(str).tolist()
    normalized = normalized_unique_map(sw2)
    rows = []
    for name in ths["行业"].astype(str).drop_duplicates().sort_values():
        if name in sw_by_name:
            rows.append(row(name, sw_by_name[name], name, "exact", 1.0, "auto_exact_match"))
            continue
        normalized_name = normalize_name(name)
        if normalized_name in normalized:
            sw_name, sw_code = normalized[normalized_name]
            rows.append(row(name, sw_code, sw_name, "normalized_exact", 0.95, "auto_normalized_match"))
            continue
        if name in MANUAL_CURRENT_OBSERVATION:
            sw_code, sw_name, confidence, note = MANUAL_CURRENT_OBSERVATION[name]
            rows.append(row(
                name,
                sw_code,
                sw_name,
                "manual_current_overlay",
                confidence,
                "manual_current_observation",
                f"人工复核用于当前观察；{note} 不接入因子回测。",
            ))
            continue
        suggestion, confidence = best_match(name, sw_names)
        rows.append(row(
            name,
            sw_by_name.get(suggestion, ""),
            suggestion,
            "difflib_suggestion",
            confidence,
            "manual_review_required",
        ))
    return pd.DataFrame(rows)


def best_match(name: str, candidates: list[str]) -> tuple[str, float]:
    scored = [(SequenceMatcher(None, name, candidate).ratio(), candidate) for candidate in candidates]
    score, candidate = max(scored, default=(0.0, ""))
    return candidate, round(float(score), 4)


def normalized_unique_map(sw2: pd.DataFrame) -> dict[str, tuple[str, str]]:
    seen: dict[str, list[tuple[str, str]]] = {}
    for item in sw2.to_dict("records"):
        key = normalize_name(str(item["industry_name"]))
        seen.setdefault(key, []).append((str(item["industry_name"]), str(item["industry_code"]).zfill(6)))
    return {key: values[0] for key, values in seen.items() if len(values) == 1}


def normalize_name(value: str) -> str:
    return value.replace("Ⅱ", "").replace("II", "").strip()


def row(ths_name: str, sw_code: str, sw_name: str, method: str, confidence: float, status: str, notes: str | None = None) -> dict[str, object]:
    return {
        "ths_industry_name": ths_name,
        "mapped_sw2_code": sw_code,
        "mapped_sw2_name": sw_name,
        "mapping_method": method,
        "mapping_confidence": confidence,
        "review_status": status,
        "production_allowed": "否",
        "notes": notes or "精确匹配也需复核行业口径；manual_review_required 不得接入因子回测。",
    }


def write_outputs(mapping: pd.DataFrame) -> None:
    MAPPING.parent.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    mapping.to_csv(MAPPING, index=False, encoding="utf-8-sig")
    mapping.to_csv(DEBUG / "mapping_draft.csv", index=False, encoding="utf-8-sig")
    pending = mapping[mapping["review_status"].ne("auto_exact_match")].copy()
    pending.to_csv(DEBUG / "manual_review_required.csv", index=False, encoding="utf-8-sig")
    pending.head(20).to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    summary = summary_payload(mapping)
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary), encoding="utf-8")


def summary_payload(mapping: pd.DataFrame) -> dict[str, object]:
    exact = int(mapping["review_status"].eq("auto_exact_match").sum())
    normalized = int(mapping["review_status"].eq("auto_normalized_match").sum())
    manual_current = int(mapping["review_status"].eq("manual_current_observation").sum())
    auto_mapped = exact + normalized
    return {
        "version": "industry_fund_flow_mapping_audit_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mapping_rows": int(len(mapping)),
        "exact_match_count": exact,
        "normalized_match_count": normalized,
        "manual_current_observation_count": manual_current,
        "auto_mapped_count": auto_mapped,
        "manual_review_required_count": int(mapping["review_status"].eq("manual_review_required").sum()),
        "exact_match_coverage": float(exact / len(mapping)) if len(mapping) else 0.0,
        "auto_mapping_coverage": float(auto_mapped / len(mapping)) if len(mapping) else 0.0,
        "mapping_file": str(MAPPING.relative_to(ROOT)),
        "production_ready": False,
        "final_verdict": "已生成同花顺行业到申万二级的映射草案；非精确匹配仍需人工复核，资金流不得接入 V4.72。",
    }


def render_report(summary: dict[str, object]) -> str:
    return "\n".join([
        "# 同花顺行业资金流到申万二级映射审计",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 映射行数：{summary['mapping_rows']}",
        f"- 精确匹配：{summary['exact_match_count']}",
        f"- 标准化匹配：{summary['normalized_match_count']}",
        f"- 人工当前观察映射：{summary['manual_current_observation_count']}",
        f"- 自动映射合计：{summary['auto_mapped_count']}",
        f"- 待人工复核：{summary['manual_review_required_count']}",
        f"- 精确覆盖率：{summary['exact_match_coverage']:.2%}",
        f"- 自动映射覆盖率：{summary['auto_mapping_coverage']:.2%}",
        f"- 映射文件：`{summary['mapping_file']}`",
        f"- 生产可用：`{str(summary['production_ready']).lower()}`",
        "",
        "边界：这是映射草案，不是因子验证结果。人工复核完成和连续 PIT 缓存前，不接入强行业选择。",
    ])


def self_check() -> None:
    tmp = OUT / "debug" / "_self_check"
    tmp.mkdir(parents=True, exist_ok=True)
    ths = tmp / "ths.csv"
    sw = tmp / "sw.csv"
    with ths.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["行业"])
        writer.writeheader()
        writer.writerow({"行业": "半导体"})
        writer.writerow({"行业": "白酒"})
        writer.writerow({"行业": "未知行业"})
    pd.DataFrame([
        {"industry_code": "801081", "industry_name": "半导体"},
        {"industry_code": "801125", "industry_name": "白酒Ⅱ"},
        {"industry_code": "801126", "industry_name": "非白酒"},
        {"industry_code": "801999", "industry_name": "其他行业"},
    ]).to_csv(sw, index=False, encoding="utf-8-sig")
    result = build_mapping(ths, sw)
    assert result.loc[result["ths_industry_name"].eq("半导体"), "review_status"].iloc[0] == "auto_exact_match"
    assert result.loc[result["ths_industry_name"].eq("白酒"), "mapped_sw2_name"].iloc[0] == "白酒Ⅱ"
    assert result.loc[result["ths_industry_name"].eq("未知行业"), "review_status"].iloc[0] == "manual_review_required"
    print("self_check=pass")


if __name__ == "__main__":
    main()
