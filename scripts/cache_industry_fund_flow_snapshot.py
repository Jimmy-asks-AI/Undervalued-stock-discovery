from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data_catalog" / "cache" / "industry_fund_flow" / "ths"
OUT = ROOT / "outputs" / "audit" / "industry_fund_flow_cache_snapshot"
DEBUG = OUT / "debug"
SW2_VALUATION = ROOT / "data_catalog" / "cache" / "industry_index" / "valuation_history" / "second" / "sws_second_industry_daily_valuation_2015_present.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description="Cache current THS industry fund-flow snapshots for future PIT validation.")
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    error = trade_date_error(args.trade_date, date.today())
    if error:
        parser.error(error)

    now, rolling = fetch_ths()
    cache_dir = CACHE / args.trade_date
    cache_dir.mkdir(parents=True, exist_ok=True)
    now.to_csv(cache_dir / "ths_industry_fund_flow_now.csv", index=False, encoding="utf-8-sig")
    rolling.to_csv(cache_dir / "ths_industry_fund_flow_5d.csv", index=False, encoding="utf-8-sig")
    mapping = mapping_audit(now)
    summary = {
        "version": "industry_fund_flow_cache_snapshot_1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": args.trade_date,
        "cache_dir": str(cache_dir.relative_to(ROOT)),
        "now_rows": int(len(now)),
        "rolling_5d_rows": int(len(rolling)),
        "exact_sw2_name_matches": int(mapping["exact_sw2_match"].sum()) if len(mapping) else 0,
        "mapping_coverage": float(mapping["exact_sw2_match"].mean()) if len(mapping) else 0.0,
        "production_ready": False,
        "final_verdict": "已开始缓存同花顺行业资金流快照，但行业体系不是申万二级；完成稳定缓存和映射前不接入 V4.72。",
    }
    write_outputs(summary, mapping)
    print(f"cache_dir={cache_dir}")
    print(f"mapping_coverage={summary['mapping_coverage']:.2%}")
    print("production_ready=False")


def fetch_ths() -> tuple[pd.DataFrame, pd.DataFrame]:
    import akshare as ak

    now = ak.stock_fund_flow_industry(symbol="即时")
    rolling = ak.stock_fund_flow_industry(symbol="5日排行")
    return normalize(now, "now"), normalize(rolling, "rolling_5d")


def normalize(df: pd.DataFrame, snapshot_type: str) -> pd.DataFrame:
    out = df.copy()
    out.insert(0, "snapshot_type", snapshot_type)
    out.insert(0, "cached_at", datetime.now().isoformat(timespec="seconds"))
    return out


def mapping_audit(now: pd.DataFrame) -> pd.DataFrame:
    sw2 = pd.read_csv(SW2_VALUATION, encoding="utf-8-sig", usecols=["industry_code", "industry_name"])
    sw2["industry_code"] = sw2["industry_code"].astype(str).str.zfill(6)
    names = sw2.drop_duplicates("industry_name").set_index("industry_name")["industry_code"].to_dict()
    rows = []
    for industry in now["行业"].astype(str).tolist():
        rows.append({
            "ths_industry_name": industry,
            "exact_sw2_match": industry in names,
            "matched_sw2_code": names.get(industry, ""),
            "mapping_status": "exact_match" if industry in names else "needs_manual_mapping",
        })
    return pd.DataFrame(rows)


def write_outputs(summary: dict[str, object], mapping: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DEBUG.mkdir(parents=True, exist_ok=True)
    (OUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(summary), encoding="utf-8")
    mapping.to_csv(DEBUG / "ths_sw2_name_mapping_audit.csv", index=False, encoding="utf-8-sig")
    mapping.head(20).to_csv(OUT / "top_candidates.csv", index=False, encoding="utf-8-sig")


def render_report(summary: dict[str, object]) -> str:
    return "\n".join([
        "# 行业资金流 PIT 缓存快照",
        "",
        str(summary["final_verdict"]),
        "",
        f"- 交易日：{summary['trade_date']}",
        f"- 缓存目录：`{summary['cache_dir']}`",
        f"- 即时资金流行数：{summary['now_rows']}",
        f"- 5日资金流行数：{summary['rolling_5d_rows']}",
        f"- 申万二级精确名称映射覆盖：{summary['mapping_coverage']:.2%}",
        f"- 生产可用：`{str(summary['production_ready']).lower()}`",
        "",
        "边界：这只是从今天开始的 PIT 缓存，不回填历史，不作为交易信号。",
    ])


def trade_date_error(value: str, today: date) -> str | None:
    try:
        trade_date = date.fromisoformat(value)
    except ValueError:
        return "--trade-date must be YYYY-MM-DD"
    if trade_date > today:
        return f"--trade-date {value} is in the future; cache fund-flow snapshots on or after that date."
    if trade_date.weekday() >= 5:
        return f"--trade-date {value} is a weekend; use an A-share trading day."
    return None


def self_check() -> None:
    assert trade_date_error("2026-06-19", date(2026, 6, 20)) is None
    assert "future" in str(trade_date_error("2026-06-23", date(2026, 6, 20)))
    assert "weekend" in str(trade_date_error("2026-06-20", date(2026, 6, 20)))
    sample = pd.DataFrame({"行业": ["银行", "不存在行业"]})
    sw2 = pd.DataFrame({"industry_code": ["801780"], "industry_name": ["银行"]})
    tmp = OUT / "debug" / "_self_check_sw2.csv"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    sw2.to_csv(tmp, index=False, encoding="utf-8-sig")
    global SW2_VALUATION
    old = SW2_VALUATION
    SW2_VALUATION = tmp
    try:
        result = mapping_audit(sample)
        assert bool(result["exact_sw2_match"].iloc[0])
        assert not bool(result["exact_sw2_match"].iloc[1])
    finally:
        SW2_VALUATION = old
        tmp.unlink(missing_ok=True)
    print("self_check=pass")


if __name__ == "__main__":
    main()
