from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
V472_LATEST = ROOT / "outputs" / "industry_rebound_leader_selection_v4_72" / "debug" / "latest_rebound_leader_candidates.csv"
MAPPING = ROOT / "configs" / "industry_fund_flow_ths_sw2_mapping.csv"
CACHE_ROOT = ROOT / "data_catalog" / "cache" / "industry_fund_flow" / "ths"
OUT = ROOT / "outputs" / "audit" / "v4_72_candidate_fund_flow_overlay"
DEBUG = OUT / "debug"
USABLE_REVIEW_STATUS = {"auto_exact_match", "auto_normalized_match", "manual_current_observation"}
PROXY_CURRENT_OBSERVATION = {
    "801014": ("养殖业", 0.40, "饲料缺 THS 精确行业；养殖业仅作产业链热度代理观察。"),
    "801952": ("煤炭开采加工", 0.55, "焦炭缺 THS 精确行业；煤炭开采加工仅作上游链条代理观察。"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Overlay cached THS fund-flow observations on V4.72 candidates.")
    parser.add_argument("--trade-date", default="2026-06-19")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if error := trade_date_error(args.trade_date, date.today()):
        parser.error(error)
    overlay = build_overlay(args.trade_date)
    write_outputs(args.trade_date, overlay)
    print(f"output_dir={OUT}")
    print(f"overlay_rows={len(overlay)}")
    print(f"available={int(overlay['fund_flow_overlay_status'].eq('available_current_only').sum())}")
    print(f"proxy={int(overlay['fund_flow_overlay_status'].eq('proxy_current_only').sum())}")
    print("production_ready=False")


def build_overlay(trade_date: str) -> pd.DataFrame:
    candidates = pd.read_csv(V472_LATEST, encoding="utf-8-sig", dtype={"industry_code": str})
    mapping = pd.read_csv(MAPPING, encoding="utf-8-sig", dtype={"mapped_sw2_code": str})
    now = pd.read_csv(CACHE_ROOT / trade_date / "ths_industry_fund_flow_now.csv", encoding="utf-8-sig")
    rolling = pd.read_csv(CACHE_ROOT / trade_date / "ths_industry_fund_flow_5d.csv", encoding="utf-8-sig")
    candidates["industry_code"] = candidates["industry_code"].astype(str).str.zfill(6)
    mapping["mapped_sw2_code"] = mapping["mapped_sw2_code"].astype(str).str.zfill(6)
    usable_mapping = mapping[mapping["review_status"].isin(USABLE_REVIEW_STATUS)].copy()
    usable_mapping = usable_mapping.sort_values(["mapped_sw2_code", "mapping_confidence"], ascending=[True, False])
    usable_mapping = usable_mapping.drop_duplicates("mapped_sw2_code")
    now_keep = now[["行业", "行业-涨跌幅", "流入资金", "流出资金", "净额", "领涨股", "领涨股-涨跌幅"]].copy()
    now_keep.rename(columns={
        "行业": "ths_industry_name",
        "行业-涨跌幅": "ths_today_return_pct",
        "流入资金": "ths_today_inflow",
        "流出资金": "ths_today_outflow",
        "净额": "ths_today_net_flow",
        "领涨股": "ths_leading_stock",
        "领涨股-涨跌幅": "ths_leading_stock_return_pct",
    }, inplace=True)
    rolling_keep = rolling[["行业", "阶段涨跌幅", "净额"]].copy()
    rolling_keep.rename(columns={
        "行业": "ths_industry_name",
        "阶段涨跌幅": "ths_5d_return_pct",
        "净额": "ths_5d_net_flow",
    }, inplace=True)
    out = candidates.merge(
        usable_mapping,
        left_on="industry_code",
        right_on="mapped_sw2_code",
        how="left",
    ).merge(now_keep, on="ths_industry_name", how="left").merge(rolling_keep, on="ths_industry_name", how="left")
    out["fund_flow_overlay_status"] = out["ths_today_net_flow"].map(lambda x: "available_current_only" if pd.notna(x) else "missing_mapping_or_flow")
    out = apply_proxy_observations(out, now_keep.merge(rolling_keep, on="ths_industry_name", how="left"))
    out["production_allowed"] = "否"
    out["fund_flow_usage_boundary"] = "当前观察字段；不参与历史回测、不改变V4.72候选排序、不生成交易指令"
    cols = [
        "industry_code", "industry_name", "selection_strategy", "selection_score", "planned_entry_date",
        "ths_industry_name", "review_status", "mapping_confidence", "fund_flow_overlay_status",
        "ths_today_return_pct", "ths_today_net_flow", "ths_5d_return_pct", "ths_5d_net_flow",
        "ths_leading_stock", "ths_leading_stock_return_pct", "historical_failure_flag",
        "production_allowed", "fund_flow_usage_boundary", "proxy_observation_note",
    ]
    return out[[c for c in cols if c in out.columns]]


def trade_date_error(value: str, today: date) -> str:
    # ponytail: future guard only; cache existence still verifies actual data availability.
    if date.fromisoformat(value) > today:
        return f"--trade-date {value} is in the future; run fund-flow overlay on or after that date."
    return ""


def apply_proxy_observations(out: pd.DataFrame, flow: pd.DataFrame) -> pd.DataFrame:
    flow_by_name = flow.set_index("ths_industry_name").to_dict("index")
    for code, (ths_name, confidence, note) in PROXY_CURRENT_OBSERVATION.items():
        mask = out["industry_code"].astype(str).str.zfill(6).eq(code) & out["fund_flow_overlay_status"].eq("missing_mapping_or_flow")
        source = flow_by_name.get(ths_name)
        if not mask.any() or not source:
            continue
        out.loc[mask, "ths_industry_name"] = ths_name
        out.loc[mask, "review_status"] = "proxy_current_observation"
        out.loc[mask, "mapping_confidence"] = confidence
        out.loc[mask, "fund_flow_overlay_status"] = "proxy_current_only"
        out.loc[mask, "proxy_observation_note"] = note
        for col, value in source.items():
            if col in out.columns and col != "ths_industry_name":
                out.loc[mask, col] = value
    return out


def write_outputs(trade_date: str, overlay: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    overlay.to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    overlay.to_csv(DEBUG / "candidate_fund_flow_overlay.csv", index=False, encoding="utf-8-sig")
    missing = overlay[overlay["fund_flow_overlay_status"].eq("missing_mapping_or_flow")]
    proxy_count = int(overlay["fund_flow_overlay_status"].eq("proxy_current_only").sum())
    gate_fail_count = int(len(missing) + proxy_count)
    missing.to_csv(DEBUG / "missing_candidate_fund_flow_mapping.csv", index=False, encoding="utf-8-sig")
    summary = {
        "version": "v4_72_candidate_fund_flow_overlay_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "candidate_count": int(len(overlay)),
        "available_overlay_count": int(overlay["fund_flow_overlay_status"].eq("available_current_only").sum()),
        "proxy_overlay_count": proxy_count,
        "overlay_gate_fail_count": gate_fail_count,
        "missing_overlay_count": int(len(missing)),
        "missing_candidate_industries": "、".join(missing["industry_name"].astype(str).tolist()),
        "proxy_candidate_industries": names_for_status(overlay, "proxy_current_only"),
        "proxy_observation_notes": proxy_observation_notes(overlay),
        "production_ready": False,
        "final_verdict": "资金流只作为当前观察叠加；由于映射和PIT历史不足，不接入V4.72强行业选择规则。",
    }
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary, overlay), encoding="utf-8")


def render_report(summary: dict[str, object], overlay: pd.DataFrame) -> str:
    report_frame = overlay.fillna("")
    return "\n".join([
        "# V4.72 候选行业资金流只读叠加",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 交易日：{summary['trade_date']}",
        f"- 候选行业：{summary['candidate_count']}",
        f"- 有资金流观察：{summary['available_overlay_count']}",
        f"- 代理资金流观察：{summary['proxy_overlay_count']}",
        f"- 资金流门禁未通过：{summary['overlay_gate_fail_count']}",
        f"- 缺失映射或资金流：{summary['missing_overlay_count']}",
        f"- 优先补映射：{summary['missing_candidate_industries']}",
        f"- 代理观察行业：{summary['proxy_candidate_industries']}",
        f"- 代理观察原因：{summary['proxy_observation_notes']}",
        f"- 生产可用：`{str(summary['production_ready']).lower()}`",
        "",
        report_frame.to_markdown(index=False) if len(report_frame) else "无数据。",
        "",
        "边界：该表只用于人工复核当前候选热度；不能用于历史收益归因、不能作为买卖建议。",
    ])


def names_for_status(overlay: pd.DataFrame, status: str) -> str:
    rows = overlay[overlay["fund_flow_overlay_status"].eq(status)]
    return "、".join(rows["industry_name"].astype(str).tolist())


def proxy_observation_notes(overlay: pd.DataFrame) -> str:
    rows = overlay[overlay["fund_flow_overlay_status"].eq("proxy_current_only")].fillna("")
    return "；".join(f"{row.industry_name}={row.proxy_observation_note}" for row in rows.itertuples())


def self_check() -> None:
    assert trade_date_error("2026-06-20", date(2026, 6, 20)) == ""
    assert "future" in trade_date_error("2026-06-23", date(2026, 6, 20))
    frame = pd.DataFrame({"fund_flow_overlay_status": ["available_current_only", "missing_mapping_or_flow"]})
    assert int(frame["fund_flow_overlay_status"].eq("available_current_only").sum()) == 1
    assert "manual_current_observation" in USABLE_REVIEW_STATUS
    out = pd.DataFrame({"industry_code": ["801014"], "fund_flow_overlay_status": ["missing_mapping_or_flow"]})
    flow = pd.DataFrame({"ths_industry_name": ["养殖业"], "ths_today_net_flow": [1.0]})
    proxied = apply_proxy_observations(out, flow)
    assert proxied.loc[0, "fund_flow_overlay_status"] == "proxy_current_only"
    assert proxied.loc[0, "review_status"] == "proxy_current_observation"
    assert int(proxied["fund_flow_overlay_status"].eq("missing_mapping_or_flow").sum()) == 0
    proxied["industry_name"] = ["饲料"]
    assert names_for_status(proxied, "proxy_current_only") == "饲料"
    assert "饲料缺 THS 精确行业" in proxy_observation_notes(proxied)
    print("self_check=pass")


if __name__ == "__main__":
    main()
