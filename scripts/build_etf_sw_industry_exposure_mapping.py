#!/usr/bin/env python
from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import urllib3


ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "data_catalog" / "etf_pit_master.csv"
MAPPING = ROOT / "data_catalog" / "etf_sw_industry_exposure_mapping.csv"
INDUSTRY_PANEL = ROOT / "outputs" / "industry_index_research_validation" / "debug" / "raw_industry_panel.csv"
CACHE = ROOT / "data_catalog" / "cache" / "etf_sw_industry_mapping"
OUTPUT = ROOT / "outputs" / "audit" / "etf_sw_industry_exposure_mapping"
SWS_STOCK = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
SWS_COMPARE = "https://www.swsresearch.com/swindex/pdf/SwClass2021/2014to2021.xlsx"
CSI_WEIGHT = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/closeweight/{code}closeweight.xls"
HEADERS = {"User-Agent": "Mozilla/5.0"}
MIN_CLASSIFIED_WEIGHT = 0.80
MIN_DOMINANT_EXPOSURE = 0.60


def main() -> None:
    parser = argparse.ArgumentParser(description="按官方指数成分权重构建 ETF 到申万二级行业映射。")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return

    as_of = date.fromisoformat(args.as_of_date)
    cache_dir = CACHE / as_of.isoformat()
    cache_dir.mkdir(parents=True, exist_ok=True)
    stock_map, sw_audit = build_stock_industry_map(cache_dir, as_of, args.refresh)
    etfs = current_target_etfs()
    index_codes = sorted(set(etfs["tracked_index_code"]) - {""})
    fetch_weight_files(index_codes, cache_dir, args.refresh)
    index_exposures, download_audit = build_index_exposures(index_codes, cache_dir, stock_map)
    mappings = etfs.merge(index_exposures, on="tracked_index_code", how="left").fillna("")
    industry_names = pd.read_csv(INDUSTRY_PANEL, dtype=str, keep_default_na=False).drop_duplicates("industry_code").set_index("industry_code")["industry_name"].to_dict()
    mappings["industry_name"] = mappings["industry_code"].map(industry_names).fillna("")
    index_exposures["industry_name"] = index_exposures["industry_code"].map(industry_names).fillna("")
    mappings["mapping_status"] = mappings.apply(mapping_status, axis=1)
    mappings["mapping_confidence"] = mappings["mapping_status"].map({"high_confidence_component_exposure": "high"}).fillna("none")
    mappings["mapping_source"] = "official_csi_closeweight+official_sws_stock_classification"
    write_outputs(mappings, index_exposures, download_audit, sw_audit, as_of)
    passed = int(mappings["mapping_status"].eq("high_confidence_component_exposure").sum())
    print(f"etf_count={len(mappings)}")
    print(f"high_confidence_mapping_count={passed}")
    print(f"mapped_industry_count={mappings.loc[mappings['mapping_status'].eq('high_confidence_component_exposure'), 'industry_code'].nunique()}")


def build_stock_industry_map(cache_dir: Path, as_of: date, refresh: bool) -> tuple[dict[str, str], dict[str, Any]]:
    stock_path = download(SWS_STOCK, cache_dir / "StockClassifyUse_stock.xls", refresh)
    compare_path = download(SWS_COMPARE, cache_dir / "2014to2021.xlsx", refresh)
    stock = pd.read_excel(stock_path, dtype=str)
    stock.columns = ["stock_code", "entry_date", "industry_internal_code", "update_time"]
    stock["entry_date"] = pd.to_datetime(stock["entry_date"], errors="coerce")
    stock = stock[stock["entry_date"].dt.date <= as_of].sort_values(["stock_code", "entry_date"]).drop_duplicates("stock_code", keep="last")
    stock["second_internal_code"] = stock["industry_internal_code"].str[:4] + "00"

    comparison = pd.read_excel(compare_path, sheet_name=1, header=1, dtype=str)
    second = comparison.iloc[:, [5, 7]].dropna()
    second.columns = ["industry_name", "second_internal_code"]
    second = second[second["second_internal_code"].str.fullmatch(r"\d{4}00", na=False)]
    industry = pd.read_csv(INDUSTRY_PANEL, dtype=str, keep_default_na=False)[["industry_code", "industry_name"]].drop_duplicates()
    name_to_code = industry.set_index("industry_name")["industry_code"].to_dict()
    internal_to_sw = {
        row.second_internal_code: name_to_code[row.industry_name]
        for row in second.itertuples() if row.industry_name in name_to_code
    }
    stock["industry_code"] = stock["second_internal_code"].map(internal_to_sw).fillna("")
    mapped = stock[stock["industry_code"].ne("")]
    return mapped.set_index("stock_code")["industry_code"].to_dict(), {
        "stock_classification_count": len(stock),
        "mapped_stock_count": len(mapped),
        "mapped_stock_coverage": len(mapped) / len(stock) if len(stock) else 0.0,
        "mapped_second_industry_count": mapped["industry_code"].nunique(),
        "official_second_internal_mapping_count": len(internal_to_sw),
    }


def current_target_etfs() -> pd.DataFrame:
    master = pd.read_csv(MASTER, dtype=str, keep_default_na=False)
    latest = master[master["snapshot_date"] == master["snapshot_date"].max()]
    columns = ["etf_code", "exchange", "fund_name", "tracked_index_code", "tracked_index_name", "scale_cny_100m"]
    return latest[latest["eligible_stock_etf"].str.lower().eq("true") & latest["mapping_status"].eq("exact_index_code")][columns].drop_duplicates("etf_code")


def fetch_weight_files(codes: list[str], cache_dir: Path, refresh: bool) -> None:
    weight_dir = cache_dir / "csi_closeweight"
    weight_dir.mkdir(parents=True, exist_ok=True)

    def fetch(code: str) -> None:
        path = weight_dir / f"{code}.xls"
        missing = weight_dir / f"{code}.missing"
        if (path.exists() or missing.exists()) and not refresh:
            return
        try:
            response = requests.get(CSI_WEIGHT.format(code=code), headers=HEADERS, timeout=60)
            response.raise_for_status()
            pd.read_excel(io.BytesIO(response.content), nrows=1)
            path.write_bytes(response.content)
            missing.unlink(missing_ok=True)
        except Exception:
            path.unlink(missing_ok=True)
            missing.write_text("official_weight_file_missing", encoding="ascii")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fetch, codes))


def build_index_exposures(codes: list[str], cache_dir: Path, stock_map: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, audit = [], []
    weight_dir = cache_dir / "csi_closeweight"
    for code in codes:
        path = weight_dir / f"{code}.xls"
        if not path.exists():
            audit.append({"tracked_index_code": code, "status": "official_weight_file_missing", "component_count": 0})
            continue
        try:
            frame = pd.read_excel(path, dtype=str)
            if frame.shape[1] < 10:
                raise ValueError("unexpected_weight_schema")
            frame = frame.iloc[:, [4, 9]].copy()
            frame.columns = ["stock_code", "weight"]
            frame["stock_code"] = frame["stock_code"].astype(str).str.extract(r"(\d{6})", expand=False).fillna("")
            frame["weight"] = pd.to_numeric(frame["weight"], errors="coerce").fillna(0.0)
            frame["industry_code"] = frame["stock_code"].map(stock_map).fillna("")
            total = frame["weight"].sum()
            classified = frame.loc[frame["industry_code"].ne(""), "weight"].sum()
            exposure = frame[frame["industry_code"].ne("")].groupby("industry_code")["weight"].sum().sort_values(ascending=False)
            dominant_code = exposure.index[0] if len(exposure) else ""
            dominant_weight = float(exposure.iloc[0] / total) if len(exposure) and total > 0 else 0.0
            rows.append({
                "tracked_index_code": code,
                "industry_code": dominant_code,
                "dominant_industry_weight": dominant_weight,
                "classified_weight_coverage": float(classified / total) if total > 0 else 0.0,
                "component_count": len(frame),
                "weight_date": str(pd.read_excel(path, nrows=1).iloc[0, 0]),
            })
            audit.append({"tracked_index_code": code, "status": "parsed", "component_count": len(frame)})
        except Exception as exc:
            audit.append({"tracked_index_code": code, "status": f"parse_error:{type(exc).__name__}", "component_count": 0})
    return pd.DataFrame(rows), pd.DataFrame(audit)


def mapping_status(row: pd.Series) -> str:
    try:
        coverage = float(row.get("classified_weight_coverage") or 0)
        exposure = float(row.get("dominant_industry_weight") or 0)
    except (TypeError, ValueError):
        return "component_evidence_missing"
    if coverage >= MIN_CLASSIFIED_WEIGHT and exposure >= MIN_DOMINANT_EXPOSURE and row.get("industry_code"):
        return "high_confidence_component_exposure"
    return "insufficient_component_exposure" if row.get("tracked_index_code") else "component_evidence_missing"


def write_outputs(mappings: pd.DataFrame, exposures: pd.DataFrame, download_audit: pd.DataFrame,
                  sw_audit: dict[str, Any], as_of: date) -> None:
    debug = OUTPUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    MAPPING.parent.mkdir(parents=True, exist_ok=True)
    mappings.to_csv(MAPPING, index=False, encoding="utf-8-sig")
    passed = mappings[mappings["mapping_status"].eq("high_confidence_component_exposure")].copy()
    passed.to_csv(OUTPUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    exposures.to_csv(debug / "index_industry_exposures.csv", index=False, encoding="utf-8-sig")
    download_audit.to_csv(debug / "index_weight_download_audit.csv", index=False, encoding="utf-8-sig")
    summary = {
        "version": "etf-sw-industry-exposure-mapping-1.0",
        "as_of_date": as_of.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target_etf_count": len(mappings),
        "official_weight_index_count": int((download_audit["status"] == "parsed").sum()),
        "high_confidence_mapping_count": len(passed),
        "mapped_industry_count": passed["industry_code"].nunique(),
        "mapping_ready": bool(len(passed)),
        "minimum_classified_weight": MIN_CLASSIFIED_WEIGHT,
        "minimum_dominant_exposure": MIN_DOMINANT_EXPOSURE,
        **sw_audit,
        "production_ready": False,
    }
    (OUTPUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = f"""# ETF 到申万二级行业成分暴露映射审计

结论：高置信度映射 **{len(passed)}** 只 ETF，覆盖 **{summary['mapped_industry_count']}** 个申万二级行业。

- 当前目标 ETF：{summary['target_etf_count']}
- 可取得中证官方权重的指数：{summary['official_weight_index_count']}
- 申万股票分类覆盖率：{summary['mapped_stock_coverage']:.2%}
- 分类权重门槛：{MIN_CLASSIFIED_WEIGHT:.0%}
- 单一行业暴露门槛：{MIN_DOMINANT_EXPOSURE:.0%}
- 映射就绪：`{str(summary['mapping_ready']).lower()}`

边界：只接受官方跟踪指数代码、官方指数权重和申万官方股票分类的成分交集；宽基或跨行业指数不会因为名称相似被映射。
"""
    (OUTPUT / "report.md").write_text(report, encoding="utf-8")


def download(url: str, path: Path, refresh: bool) -> Path:
    if path.exists() and not refresh:
        return path
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = requests.get(url, headers=HEADERS, verify=False, timeout=60)
    response.raise_for_status()
    path.write_bytes(response.content)
    return path


def self_check() -> None:
    assert mapping_status(pd.Series({"classified_weight_coverage": 0.90, "dominant_industry_weight": 0.70, "industry_code": "801081", "tracked_index_code": "931865"})) == "high_confidence_component_exposure"
    assert mapping_status(pd.Series({"classified_weight_coverage": 0.95, "dominant_industry_weight": 0.20, "industry_code": "801081", "tracked_index_code": "000300"})) == "insufficient_component_exposure"
    print("self_check=pass")


if __name__ == "__main__":
    main()
