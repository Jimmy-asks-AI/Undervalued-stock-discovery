#!/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from valuation_pit_contract import SHANGHAI, archive_current_snapshot_immutable, current_snapshot_as_of_error
except ModuleNotFoundError:  # package-style imports in tests and audits
    from scripts.valuation_pit_contract import SHANGHAI, archive_current_snapshot_immutable, current_snapshot_as_of_error


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "data_catalog" / "cache" / "industry_index"
DEFAULT_OUTPUT = ROOT / "outputs" / "industry_index_research_validation"
VERSION = "1.8.1"
VALIDATION_FACTORS = ["price_only_oversold_signal", "stabilized_oversold_signal"]
DEFAULT_ROLLING_RANKIC_WINDOW = 36
DEFAULT_OOS_SPLIT_RATIO = 0.70
MAX_CURRENT_HISTORY_STALE_DAYS = 4

sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

from strategy_lab.industry_index_research_os.agents import (
    candidate_rows_for_chinese_output,
    render_candidate_report,
    run_industry_index_research_agents,
    translate_candidate_status,
)


FACTOR_LABELS_ZH = {
    "price_only_oversold_signal": "纯价格超跌信号",
    "stabilized_oversold_signal": "企稳确认超跌信号",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SW industry index research validation with compact outputs.")
    parser.add_argument("--trade-date", type=iso_date, default=date.today().isoformat(), help="Decision/as-of date, YYYY-MM-DD. Live valuation snapshots cannot be backdated.")
    parser.add_argument("--industry-level", choices=["first", "second"], default="second", help="SW industry level.")
    parser.add_argument("--horizons", default="60,120,252", help="Forward return horizons, comma-separated.")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE), help="Local cache directory for fetched histories.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Compact output directory.")
    parser.add_argument("--top", type=int, default=30, help="Top current rows in top_candidates.csv and report.")
    parser.add_argument("--rebalance-step", type=int, default=20, help="Historical validation sample step in trading days.")
    parser.add_argument("--top-n", type=int, default=5, help="Top N industries for historical validation basket.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="One-way research turnover cost proxy in basis points.")
    parser.add_argument("--candidate-count", type=int, default=8, help="Current research basket candidate count.")
    parser.add_argument("--max-parent-weight", type=float, default=0.35, help="Maximum parent-industry count share.")
    parser.add_argument(
        "--rolling-rankic-window",
        type=int,
        default=DEFAULT_ROLLING_RANKIC_WINDOW,
        help="Rolling daily RankIC window length for stability checks.",
    )
    parser.add_argument(
        "--oos-split-ratio",
        type=float,
        default=DEFAULT_OOS_SPLIT_RATIO,
        help="Chronological in-sample split ratio for out-of-sample validation.",
    )
    parser.add_argument(
        "--refresh-history",
        action="store_true",
        help="Refetch industry histories from AkShare instead of using local cached CSVs.",
    )
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    error = trade_date_error(args.trade_date, date.today())
    if error:
        parser.error(error)
    snapshot_observed_at = datetime.now(SHANGHAI)
    if error := current_snapshot_as_of_error(args.trade_date, snapshot_observed_at):
        parser.error(error)

    horizons = parse_horizons(args.horizons)
    output_dir = Path(args.output)
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    fundamentals = fetch_industry_fundamentals(args.industry_level)
    valuation_snapshot = archive_valuation_snapshot(
        fundamentals=fundamentals,
        industry_level=args.industry_level,
        trade_date=args.trade_date,
        cache_dir=Path(args.cache_dir),
        observed_at=snapshot_observed_at,
    )
    histories = load_industry_histories(
        fundamentals=fundamentals,
        industry_level=args.industry_level,
        cache_dir=Path(args.cache_dir),
        trade_date=args.trade_date,
        refresh=args.refresh_history,
    )

    current_records = build_current_records(
        fundamentals=fundamentals,
        histories=histories,
        trade_date=args.trade_date,
        industry_level=args.industry_level,
    )
    agent_result = run_industry_index_research_agents(current_records)
    ranking = pd.DataFrame(agent_result["candidate_ranking"])
    ranking.insert(0, "rank", range(1, len(ranking) + 1))
    current_basket = build_current_research_basket(
        ranking=ranking,
        candidate_count=args.candidate_count,
        max_parent_weight=args.max_parent_weight,
    )
    basket_summary = summarize_current_research_basket(current_basket, args)
    ranking = attach_research_basket_columns(ranking, current_basket)

    validation_features = build_historical_validation_features(
        histories=histories,
        horizons=horizons,
        rebalance_step=args.rebalance_step,
    )
    rankic = compute_rankic_report(validation_features, horizons, VALIDATION_FACTORS)
    group_returns = compute_group_return_report(validation_features, horizons, VALIDATION_FACTORS)
    topn = compute_topn_backtest(
        validation_features,
        horizons,
        top_n=args.top_n,
        cost_bps=args.cost_bps,
        factor="stabilized_oversold_signal",
    )
    validation_decisions = build_validation_decisions(rankic, group_returns, topn)
    yearly_validation = compute_yearly_validation_report(validation_features, horizons, VALIDATION_FACTORS)
    regime_validation = compute_regime_validation_report(validation_features, horizons, VALIDATION_FACTORS)
    rolling_rankic = compute_rolling_rankic_report(
        validation_features,
        horizons,
        VALIDATION_FACTORS,
        window=args.rolling_rankic_window,
    )
    oos_validation = compute_oos_validation_report(
        validation_features,
        horizons,
        VALIDATION_FACTORS,
        split_ratio=args.oos_split_ratio,
    )
    real_data_audit = build_real_data_audit(
        fundamentals=fundamentals,
        histories=histories,
        valuation_snapshot=valuation_snapshot,
        refresh_history=args.refresh_history,
    )

    candidate_rows = ranking[ranking["candidate_status"] == "industry_value_oversold_candidate"].copy()
    top_candidates = ranking.head(args.top).copy()
    panel = pd.DataFrame(current_records)

    pd.DataFrame(candidate_rows_for_chinese_output(top_candidates.to_dict("records"))).to_csv(
        output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig"
    )
    panel.to_csv(debug_dir / "raw_industry_panel.csv", index=False, encoding="utf-8-sig")
    ranking.to_csv(debug_dir / "all_ranked_industries.csv", index=False, encoding="utf-8-sig")
    validation_features.to_csv(debug_dir / "historical_feature_panel.csv", index=False, encoding="utf-8-sig")
    rankic.to_csv(debug_dir / "rankic_report.csv", index=False, encoding="utf-8-sig")
    group_returns.to_csv(debug_dir / "group_return_report.csv", index=False, encoding="utf-8-sig")
    topn.to_csv(debug_dir / "topn_backtest.csv", index=False, encoding="utf-8-sig")
    validation_decisions.to_csv(debug_dir / "validation_decisions.csv", index=False, encoding="utf-8-sig")
    yearly_validation.to_csv(debug_dir / "yearly_validation_report.csv", index=False, encoding="utf-8-sig")
    regime_validation.to_csv(debug_dir / "regime_validation_report.csv", index=False, encoding="utf-8-sig")
    rolling_rankic.to_csv(debug_dir / "rolling_rankic_report.csv", index=False, encoding="utf-8-sig")
    oos_validation.to_csv(debug_dir / "oos_validation_report.csv", index=False, encoding="utf-8-sig")
    current_basket.to_csv(debug_dir / "current_research_basket.csv", index=False, encoding="utf-8-sig")
    basket_summary.to_csv(debug_dir / "research_basket_summary.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "real_data_audit.json", real_data_audit)
    with (debug_dir / "agent_results.json").open("w", encoding="utf-8") as handle:
        json.dump(agent_result["agent_results"], handle, ensure_ascii=False, indent=2)

    summary = {
        "version": VERSION,
        "language": "zh-CN",
        "data_mode": "真实公开数据：AkShare 申万行业估值快照 + 申万行业指数历史行情 + 本地缓存",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": args.trade_date,
        "industry_level": args.industry_level,
        "horizons": horizons,
        "industry_rows": int(len(panel)),
        "historical_feature_rows": int(len(validation_features)),
        "candidate_count": int(len(candidate_rows)),
        "top_output_rows": int(len(top_candidates)),
        "status_counts": ranking["candidate_status"].value_counts().to_dict(),
        "status_counts_zh": {
            translate_candidate_status(status): int(count)
            for status, count in ranking["candidate_status"].value_counts().to_dict().items()
        },
        "rankic_rows": int(len(rankic)),
        "group_return_rows": int(len(group_returns)),
        "topn_rows": int(len(topn)),
        "validation_decision_rows": int(len(validation_decisions)),
        "yearly_validation_rows": int(len(yearly_validation)),
        "regime_validation_rows": int(len(regime_validation)),
        "rolling_rankic_rows": int(len(rolling_rankic)),
        "oos_validation_rows": int(len(oos_validation)),
        "rolling_rankic_window": int(args.rolling_rankic_window),
        "oos_split_ratio": float(args.oos_split_ratio),
        "current_research_basket_rows": int(len(current_basket)),
        "valuation_snapshot_path": valuation_snapshot["path"],
        "valuation_snapshot_rows": valuation_snapshot["rows"],
        "valuation_snapshot_observed_at": valuation_snapshot["observed_at"],
        "valuation_snapshot_available_date": valuation_snapshot["available_date"],
        "valuation_snapshot_data_status": valuation_snapshot["data_status"],
        "valuation_snapshot_pit_eligible": valuation_snapshot["pit_eligible"],
        "overall_validation_decision": overall_validation_decision(validation_decisions, oos_validation),
        "data_sources": [
            f"akshare.sw_index_{args.industry_level}_info",
            "akshare.index_hist_sw",
        ],
        "cache_dir": str(Path(args.cache_dir).resolve()),
        "research_boundary": "仅研究申万行业指数；当前估值只用于当前截面解释，历史验证只使用 PIT 可得的价格衍生特征和未来收益标签。",
    }
    write_json(output_dir / "run_summary.json", summary)
    (output_dir / "report.md").write_text(
        render_validation_report(
            agent_result=agent_result,
            ranking=ranking,
            rankic=rankic,
            group_returns=group_returns,
            topn=topn,
            validation_decisions=validation_decisions,
            yearly_validation=yearly_validation,
            regime_validation=regime_validation,
            rolling_rankic=rolling_rankic,
            oos_validation=oos_validation,
            current_basket=current_basket,
            basket_summary=basket_summary,
            real_data_audit=real_data_audit,
            summary=summary,
            top=args.top,
        ),
        encoding="utf-8",
    )

    print(f"行业数量={summary['industry_rows']}")
    print(f"历史特征行数={summary['historical_feature_rows']}")
    print(f"候选行业数量={summary['candidate_count']}")
    print(f"RankIC行数={summary['rankic_rows']}")
    print(f"样本外验证行数={summary['oos_validation_rows']}")
    print(f"市场状态验证行数={summary['regime_validation_rows']}")
    print(f"V{VERSION}验证结论={summary['overall_validation_decision']}")
    print(f"研究篮子数量={summary['current_research_basket_rows']}")
    print(f"输出目录={output_dir.resolve()}")
    for row in ranking.head(min(args.top, 10)).to_dict("records"):
        print(
            "{rank}. {industry} 综合分={score:.4f} 状态={status}".format(
                rank=int(row["rank"]),
                industry=row["industry_name"],
                score=float(row["industry_value_score"]),
                status=translate_candidate_status(row["candidate_status"]),
            )
        )


def fetch_industry_fundamentals(industry_level: str) -> pd.DataFrame:
    try:
        import akshare as ak
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"akshare import failed: {type(exc).__name__}: {exc}") from exc
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if industry_level == "second":
            frame = ak.sw_index_second_info()
        else:
            frame = ak.sw_index_first_info()
    frame = frame.copy()
    frame["行业代码"] = frame["行业代码"].map(normalize_industry_code)
    if "上级行业" not in frame.columns:
        frame["上级行业"] = frame["行业名称"]
    return frame


def archive_valuation_snapshot(
    *,
    fundamentals: pd.DataFrame,
    industry_level: str,
    trade_date: str,
    cache_dir: Path,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    snapshot_dir = cache_dir / "valuation_snapshots" / industry_level
    return archive_current_snapshot_immutable(
        fundamentals,
        requested_as_of_date=trade_date,
        observed_at=observed_at or datetime.now(SHANGHAI),
        snapshot_dir=snapshot_dir,
    )


def load_industry_histories(
    *,
    fundamentals: pd.DataFrame,
    industry_level: str,
    cache_dir: Path,
    trade_date: str,
    refresh: bool,
) -> dict[str, pd.DataFrame]:
    history_dir = cache_dir / "history" / industry_level
    history_dir.mkdir(parents=True, exist_ok=True)
    histories: dict[str, pd.DataFrame] = {}
    for code in fundamentals["行业代码"].map(normalize_industry_code).tolist():
        cache_path = history_dir / f"{code}.csv"
        history = pd.DataFrame()
        if cache_path.exists() and not refresh:
            history = pd.read_csv(cache_path, encoding="utf-8-sig")
        else:
            try:
                history = fetch_industry_history(code)
                if not history.empty:
                    history.to_csv(cache_path, index=False, encoding="utf-8-sig")
            except Exception as exc:  # pragma: no cover
                if cache_path.exists():
                    history = pd.read_csv(cache_path, encoding="utf-8-sig")
                else:
                    history = load_history_from_valuation_cache(
                        cache_dir=cache_dir,
                        industry_level=industry_level,
                        industry_code=code,
                    )
                    if history.empty:
                        print(f"warning: failed to fetch {code}: {type(exc).__name__}: {exc}")
                    else:
                        print(f"info: used valuation history fallback for {code} after {type(exc).__name__}")
                        history.to_csv(cache_path, index=False, encoding="utf-8-sig")
        histories[code] = clean_history(history, trade_date)
    return histories


def load_history_from_valuation_cache(*, cache_dir: Path, industry_level: str, industry_code: str) -> pd.DataFrame:
    path = cache_dir / "valuation_history" / industry_level / "sws_second_industry_daily_valuation_2015_present.csv"
    if industry_level != "second" or not path.exists():
        return pd.DataFrame()
    valuation = pd.read_csv(path, encoding="utf-8-sig")
    required = {"trade_date", "industry_code", "close_index"}
    if not required.issubset(valuation.columns):
        return pd.DataFrame()
    rows = valuation[valuation["industry_code"].map(normalize_industry_code) == industry_code].copy()
    if rows.empty:
        return pd.DataFrame()
    if "volume_100m_shares" in rows.columns:
        volume = pd.to_numeric(rows["volume_100m_shares"], errors="coerce") * 100_000_000
    else:
        volume = math.nan
    return pd.DataFrame(
        {
            "代码": industry_code,
            "日期": rows["trade_date"],
            "收盘": rows["close_index"],
            "成交量": volume,
            # ponytail: valuation archive has no traded amount; add only if source later provides it.
            "成交额": math.nan,
        }
    )


def fetch_industry_history(industry_code: str) -> pd.DataFrame:
    try:
        import akshare as ak
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"akshare import failed: {type(exc).__name__}: {exc}") from exc
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            return ak.index_hist_sw(symbol=industry_code, period="day")
        except Exception as exc:
            # Some otherwise valid SWS responses contain a bare JSON NaN in an
            # unused percentage field.  requests/simplejson rejects that token,
            # while the remaining official history is complete.  Fall back only
            # for this parser failure; network and schema failures still surface.
            if not is_json_decode_failure(exc):
                raise
            return fetch_industry_history_from_sws_raw(industry_code)


def is_json_decode_failure(exc: BaseException) -> bool:
    accepted: list[type[BaseException]] = [json.JSONDecodeError]
    try:
        from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError
    except Exception:  # pragma: no cover - requests is an AkShare dependency
        RequestsJSONDecodeError = None
    if RequestsJSONDecodeError is not None:
        accepted.append(RequestsJSONDecodeError)
    return isinstance(exc, tuple(accepted))


def fetch_industry_history_from_sws_raw(industry_code: str) -> pd.DataFrame:
    try:
        import requests
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"requests import failed: {type(exc).__name__}: {exc}") from exc
    url = "https://www.swsresearch.com/institute-sw/api/index_publish/trend/"
    params = {"swindexcode": normalize_industry_code(industry_code), "period": "DAY"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/114.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return parse_sws_history_response(response.text, industry_code)


def parse_sws_history_response(text: str, industry_code: str) -> pd.DataFrame:
    observed_nan = object()

    def parse_constant(token: str) -> object:
        if token == "NaN":
            return observed_nan
        raise ValueError(f"unsupported non-finite JSON token: {token}")

    payload = json.loads(text, parse_constant=parse_constant)
    if not isinstance(payload, dict) or str(payload.get("code", "")) != "200":
        raise ValueError("SWS history response status is not successful")
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("SWS history response data is empty or invalid")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("SWS history response contains a non-object row")
    required = {
        "swindexcode", "bargaindate", "openindex", "maxindex", "minindex",
        "closeindex", "bargainamount", "bargainsum",
    }
    numeric_fields = (
        "openindex", "maxindex", "minindex", "closeindex",
        "bargainamount", "bargainsum",
    )
    price_fields = {"openindex", "maxindex", "minindex", "closeindex"}
    for index, row in enumerate(rows):
        missing = sorted(required - set(row))
        if missing:
            raise ValueError(
                f"SWS history response row {index} is missing fields: {','.join(missing)}"
            )
        for field, value in list(row.items()):
            if value is not observed_nan:
                continue
            if field != "markup":
                raise ValueError(
                    f"non-finite JSON token is allowed only in markup; observed_field={field}"
                )
            row[field] = None
        null_required = sorted(field for field in required if row.get(field) is None)
        if null_required:
            raise ValueError(
                f"SWS history response row {index} has null required fields: {','.join(null_required)}"
            )
        raw_date = row["bargaindate"]
        if not isinstance(raw_date, str) or len(raw_date) != 10:
            raise ValueError(
                f"SWS history response row {index} has invalid ISO date: bargaindate"
            )
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError(
                f"SWS history response row {index} has invalid ISO date: bargaindate"
            ) from exc
        if parsed_date.isoformat() != raw_date:
            raise ValueError(
                f"SWS history response row {index} has invalid ISO date: bargaindate"
            )
        for field in numeric_fields:
            raw_value = row[field]
            if isinstance(raw_value, bool):
                raise ValueError(
                    f"SWS history response row {index} has invalid numeric field: {field}"
                )
            try:
                numeric_value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"SWS history response row {index} has invalid numeric field: {field}"
                ) from exc
            if not math.isfinite(numeric_value):
                raise ValueError(
                    f"SWS history response row {index} has invalid numeric field: {field}"
                )
            if field in price_fields and numeric_value <= 0:
                raise ValueError(
                    f"SWS history response row {index} has non-positive price field: {field}"
                )
            if field not in price_fields and numeric_value < 0:
                raise ValueError(
                    f"SWS history response row {index} has negative volume/amount field: {field}"
                )

    expected_code = normalize_industry_code(industry_code)
    observed_codes = {normalize_industry_code(row.get("swindexcode", "")) for row in rows}
    if observed_codes != {expected_code}:
        raise ValueError(
            f"SWS history response code mismatch: expected={expected_code}; "
            f"observed={sorted(observed_codes)}"
        )

    frame = pd.DataFrame(rows)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"SWS history response is missing fields: {','.join(missing)}")
    frame = frame.rename(columns={
        "swindexcode": "代码",
        "bargaindate": "日期",
        "openindex": "开盘",
        "maxindex": "最高",
        "minindex": "最低",
        "closeindex": "收盘",
        "bargainamount": "成交量",
        "bargainsum": "成交额",
    })
    frame = frame[["代码", "日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]].copy()
    frame["代码"] = frame["代码"].map(normalize_industry_code)
    frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce").dt.date
    if frame["日期"].isna().any():
        raise ValueError("SWS history response contains an invalid date")
    for column in ["开盘", "最高", "最低", "收盘", "成交量", "成交额"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if frame[column].isna().any() or not frame[column].map(lambda value: math.isfinite(float(value))).all():
            raise ValueError(f"SWS history response contains an invalid numeric field: {column}")
    return frame


def clean_history(history: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    frame = history.copy()
    if "日期" not in frame.columns or "收盘" not in frame.columns:
        return pd.DataFrame(columns=["代码", "日期", "收盘", "开盘", "最高", "最低", "成交量", "成交额"])
    frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce")
    frame["收盘"] = pd.to_numeric(frame["收盘"], errors="coerce")
    if "成交额" in frame.columns:
        frame["成交额"] = pd.to_numeric(frame["成交额"], errors="coerce")
    else:
        frame["成交额"] = math.nan
    frame = frame.dropna(subset=["日期", "收盘"]).sort_values("日期")
    frame = frame[frame["日期"] <= pd.to_datetime(trade_date)].copy()
    return frame.reset_index(drop=True)


def build_current_records(
    *,
    fundamentals: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    trade_date: str,
    industry_level: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in fundamentals.to_dict("records"):
        industry_code = normalize_industry_code(row["行业代码"])
        industry_name = str(row["行业名称"])
        parent_industry = str(row.get("上级行业", industry_name))
        history = histories.get(industry_code, pd.DataFrame())
        history_metrics = current_history_metrics(history, trade_date)
        records.append(
            {
                "industry_code": industry_code,
                "industry_name": industry_name,
                "industry_level": industry_level,
                "parent_industry": parent_industry,
                "trade_date": trade_date,
                "available_date": trade_date,
                "source": f"akshare.sw_index_{industry_level}_info|akshare.index_hist_sw",
                "data_status": "research_only",
                "constituent_count": int(to_float(row.get("成份个数")) or 0),
                "pe_ttm": to_float(row.get("TTM(滚动)市盈率")),
                "pb": to_float(row.get("市净率")),
                "dividend_yield": percent_to_decimal(to_float(row.get("静态股息率"))),
                **history_metrics,
            }
        )
    return records


def current_history_metrics(history: pd.DataFrame, trade_date: str) -> dict[str, Any]:
    metrics = {
        "industry_close": math.nan,
        "return_20d": math.nan,
        "return_60d": math.nan,
        "return_120d": math.nan,
        "return_252d": math.nan,
        "drawdown_252d": math.nan,
        "volatility_60d": math.nan,
        "avg_amount_60d": math.nan,
        "history_days": int(len(history)),
        "history_fetch_status": "ok" if len(history) else "empty",
        "history_latest_date": "",
        "history_age_calendar_days": "",
        "history_fresh": False,
    }
    if history.empty:
        return metrics
    closes = history["收盘"].astype(float).tolist()
    latest = pd.Timestamp(history["日期"].max()).date()
    age = (date.fromisoformat(trade_date) - latest).days
    metrics.update({"history_latest_date": latest.isoformat(), "history_age_calendar_days": age,
                    "history_fresh": 0 <= age <= MAX_CURRENT_HISTORY_STALE_DAYS})
    metrics["industry_close"] = closes[-1]
    metrics["return_20d"] = period_return(closes, len(closes) - 1, 20)
    metrics["return_60d"] = period_return(closes, len(closes) - 1, 60)
    metrics["return_120d"] = period_return(closes, len(closes) - 1, 120)
    metrics["return_252d"] = period_return(closes, len(closes) - 1, 252)
    metrics["drawdown_252d"] = drawdown(closes, len(closes) - 1, 252)
    metrics["volatility_60d"] = volatility(closes, len(closes) - 1, 60)
    if "成交额" in history.columns:
        metrics["avg_amount_60d"] = float(history["成交额"].tail(60).mean())
    return metrics


def build_historical_validation_features(
    *,
    histories: dict[str, pd.DataFrame],
    horizons: list[int],
    rebalance_step: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    max_horizon = max(horizons)
    for industry_code, history in histories.items():
        if history.empty or len(history) <= 252 + max_horizon:
            continue
        closes = history["收盘"].astype(float).tolist()
        amounts = history["成交额"].astype(float).tolist() if "成交额" in history.columns else [math.nan] * len(history)
        for index in range(252, len(history) - max_horizon, rebalance_step):
            row: dict[str, Any] = {
                "trade_date": history.loc[index, "日期"].strftime("%Y-%m-%d"),
                "industry_code": industry_code,
                "return_20d": period_return(closes, index, 20),
                "return_60d": period_return(closes, index, 60),
                "return_120d": period_return(closes, index, 120),
                "return_252d": period_return(closes, index, 252),
                "drawdown_252d": drawdown(closes, index, 252),
                "volatility_60d": volatility(closes, index, 60),
                "avg_amount_60d": window_mean(amounts, index, 60),
                "valuation_pit_status": "not_available",
            }
            for horizon in horizons:
                row[f"forward_return_{horizon}d"] = forward_return(closes, index, horizon)
            rows.append(row)
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["price_only_oversold_raw"] = (
        -0.30 * frame["return_60d"].fillna(0.0)
        - 0.25 * frame["return_120d"].fillna(0.0)
        - 0.20 * frame["return_252d"].fillna(0.0)
        + 0.25 * frame["drawdown_252d"].abs().fillna(0.0)
    )
    frame["stabilized_oversold_raw"] = frame["price_only_oversold_raw"] + (
        frame["return_20d"].fillna(0.0).clip(lower=0.0) * 0.50
    )
    frame["price_only_oversold_signal"] = cross_section_rank(frame, "trade_date", "price_only_oversold_raw")
    frame["stabilized_oversold_signal"] = cross_section_rank(frame, "trade_date", "stabilized_oversold_raw")
    frame = add_validation_market_context(frame, horizons)
    return frame.drop(columns=["price_only_oversold_raw", "stabilized_oversold_raw"])


def add_validation_market_context(frame: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    enriched = frame.copy()
    for horizon in horizons:
        label = f"forward_return_{horizon}d"
        benchmark_label = f"benchmark_forward_return_{horizon}d"
        relative_label = f"benchmark_relative_return_{horizon}d"
        enriched[benchmark_label] = enriched.groupby("trade_date")[label].transform("mean")
        enriched[relative_label] = enriched[label] - enriched[benchmark_label]

    enriched["market_return_120d"] = enriched.groupby("trade_date")["return_120d"].transform("mean")
    enriched["market_volatility_60d"] = enriched.groupby("trade_date")["volatility_60d"].transform("median")
    enriched["market_regime"] = enriched["market_return_120d"].map(classify_market_regime)

    volatility_threshold = to_float(enriched["market_volatility_60d"].quantile(0.70))
    if volatility_threshold is None:
        enriched["volatility_regime"] = "未知"
    else:
        enriched["volatility_regime"] = enriched["market_volatility_60d"].map(
            lambda value: "高波动" if pd.notna(value) and float(value) >= volatility_threshold else "常态波动"
        )
    return enriched


def classify_market_regime(value: Any) -> str:
    number = to_float(value)
    if number is None:
        return "未知"
    if number >= 0.08:
        return "上行"
    if number <= -0.08:
        return "下行"
    return "震荡"


def compute_rankic_report(features: pd.DataFrame, horizons: list[int], factors: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if features.empty:
        return pd.DataFrame(rows)
    for factor in factors:
        for horizon in horizons:
            label = f"forward_return_{horizon}d"
            values: list[float] = []
            for _, group in features.groupby("trade_date"):
                sub = group[[factor, label]].dropna()
                if len(sub) < 5:
                    continue
                ic = spearman_corr(sub[factor], sub[label])
                if pd.notna(ic):
                    values.append(float(ic))
            mean_ic = float(pd.Series(values).mean()) if values else math.nan
            std_ic = float(pd.Series(values).std(ddof=1)) if len(values) > 1 else math.nan
            t_stat = mean_ic / (std_ic / math.sqrt(len(values))) if values and std_ic and std_ic > 0 else math.nan
            positive = sum(1 for value in values if value > 0) / len(values) if values else math.nan
            rows.append(
                {
                    "factor": factor,
                    "factor_zh": FACTOR_LABELS_ZH.get(factor, factor),
                    "horizon": horizon,
                    "observations": len(values),
                    "mean_rankic": mean_ic,
                    "std_rankic": std_ic,
                    "rankic_t_stat": t_stat,
                    "positive_ratio": positive,
                }
            )
    return pd.DataFrame(rows)


def compute_group_return_report(features: pd.DataFrame, horizons: list[int], factors: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if features.empty:
        return pd.DataFrame(rows)
    for factor in factors:
        for horizon in horizons:
            label = f"forward_return_{horizon}d"
            group_returns: dict[int, list[float]] = {idx: [] for idx in range(1, 6)}
            for _, group in features.groupby("trade_date"):
                sub = group[[factor, label]].dropna().copy()
                if len(sub) < 10:
                    continue
                sub["quantile"] = pd.qcut(sub[factor].rank(method="first"), q=5, labels=False) + 1
                for quantile, qgroup in sub.groupby("quantile"):
                    group_returns[int(quantile)].append(float(qgroup[label].mean()))
            for quantile, values in group_returns.items():
                rows.append(
                    {
                        "factor": factor,
                        "factor_zh": FACTOR_LABELS_ZH.get(factor, factor),
                        "horizon": horizon,
                        "quantile": quantile,
                        "mean_forward_return": float(pd.Series(values).mean()) if values else math.nan,
                        "observations": len(values),
                    }
                )
    return pd.DataFrame(rows)


def compute_topn_backtest(
    features: pd.DataFrame,
    horizons: list[int],
    top_n: int,
    cost_bps: float,
    factor: str,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if features.empty:
        return pd.DataFrame(rows)
    for horizon in horizons:
        label = f"forward_return_{horizon}d"
        previous: set[str] = set()
        for trade_date, group in features.groupby("trade_date"):
            sub = group[["industry_code", factor, label]].dropna().sort_values(factor, ascending=False).head(top_n)
            current = set(sub["industry_code"].tolist())
            if not current:
                continue
            turnover = len(current.symmetric_difference(previous)) / max(len(current), 1) if previous else 1.0
            gross = float(sub[label].mean())
            cost = turnover * cost_bps / 10000.0
            rows.append(
                {
                    "trade_date": trade_date,
                    "factor": factor,
                    "factor_zh": FACTOR_LABELS_ZH.get(factor, factor),
                    "horizon": horizon,
                    "top_n": int(len(current)),
                    "selected_industries": "|".join(sorted(current)),
                    "gross_forward_return": gross,
                    "turnover": turnover,
                    "cost_assumption_bps": cost_bps,
                    "net_forward_return": gross - cost,
                }
            )
            previous = current
    return pd.DataFrame(rows)


def compute_daily_rankic_series(features: pd.DataFrame, horizons: list[int], factors: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if features.empty:
        return pd.DataFrame(rows)
    for factor in factors:
        for horizon in horizons:
            label = f"forward_return_{horizon}d"
            for trade_date, group in features.groupby("trade_date", sort=True):
                sub = group[[factor, label]].dropna()
                if len(sub) < 5:
                    continue
                ic = spearman_corr(sub[factor], sub[label])
                if pd.notna(ic):
                    rows.append(
                        {
                            "trade_date": str(trade_date),
                            "factor": factor,
                            "factor_zh": FACTOR_LABELS_ZH.get(factor, factor),
                            "horizon": horizon,
                            "rankic": float(ic),
                            "industry_count": int(len(sub)),
                        }
                    )
    return pd.DataFrame(rows)


def summarize_rankic_values(values: pd.Series) -> dict[str, Any]:
    clean = [float(value) for value in values.dropna().tolist()]
    mean_ic = float(pd.Series(clean).mean()) if clean else math.nan
    std_ic = float(pd.Series(clean).std(ddof=1)) if len(clean) > 1 else math.nan
    t_stat = mean_ic / (std_ic / math.sqrt(len(clean))) if clean and std_ic and std_ic > 0 else math.nan
    positive = sum(1 for value in clean if value > 0) / len(clean) if clean else math.nan
    return {
        "observations": len(clean),
        "mean_rankic": mean_ic,
        "std_rankic": std_ic,
        "rankic_t_stat": t_stat,
        "positive_ratio": positive,
    }


def compute_yearly_validation_report(features: pd.DataFrame, horizons: list[int], factors: list[str]) -> pd.DataFrame:
    daily = compute_daily_rankic_series(features, horizons, factors)
    rows: list[dict[str, Any]] = []
    if daily.empty:
        return pd.DataFrame(rows)
    daily["year"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.year
    for (factor, factor_zh, horizon, year), group in daily.dropna(subset=["year"]).groupby(
        ["factor", "factor_zh", "horizon", "year"]
    ):
        stats = summarize_rankic_values(group["rankic"])
        rows.append(
            {
                "factor": factor,
                "factor_zh": factor_zh,
                "horizon": int(horizon),
                "year": int(year),
                **stats,
            }
        )
    return pd.DataFrame(rows)


def compute_regime_validation_report(features: pd.DataFrame, horizons: list[int], factors: list[str]) -> pd.DataFrame:
    daily = compute_daily_rankic_series(features, horizons, factors)
    rows: list[dict[str, Any]] = []
    if daily.empty or features.empty:
        return pd.DataFrame(rows)
    regimes = features[["trade_date", "market_regime", "volatility_regime"]].drop_duplicates("trade_date")
    daily = daily.merge(regimes, on="trade_date", how="left")
    for (factor, factor_zh, horizon, market_regime, volatility_regime), group in daily.groupby(
        ["factor", "factor_zh", "horizon", "market_regime", "volatility_regime"],
        dropna=False,
    ):
        stats = summarize_rankic_values(group["rankic"])
        rows.append(
            {
                "factor": factor,
                "factor_zh": factor_zh,
                "horizon": int(horizon),
                "market_regime": market_regime,
                "volatility_regime": volatility_regime,
                **stats,
            }
        )
    return pd.DataFrame(rows)


def compute_rolling_rankic_report(
    features: pd.DataFrame,
    horizons: list[int],
    factors: list[str],
    window: int,
) -> pd.DataFrame:
    daily = compute_daily_rankic_series(features, horizons, factors)
    rows: list[dict[str, Any]] = []
    if daily.empty or window <= 1:
        return pd.DataFrame(rows)
    for (factor, factor_zh, horizon), group in daily.groupby(["factor", "factor_zh", "horizon"]):
        ordered = group.sort_values("trade_date").reset_index(drop=True)
        if len(ordered) < window:
            continue
        for end_index in range(window - 1, len(ordered)):
            sample = ordered.iloc[end_index - window + 1 : end_index + 1]
            stats = summarize_rankic_values(sample["rankic"])
            rows.append(
                {
                    "factor": factor,
                    "factor_zh": factor_zh,
                    "horizon": int(horizon),
                    "window": int(window),
                    "start_date": sample.iloc[0]["trade_date"],
                    "end_date": sample.iloc[-1]["trade_date"],
                    "rolling_mean_rankic": stats["mean_rankic"],
                    "rolling_rankic_t_stat": stats["rankic_t_stat"],
                    "rolling_positive_ratio": stats["positive_ratio"],
                    "observations": stats["observations"],
                }
            )
    return pd.DataFrame(rows)


def compute_oos_validation_report(
    features: pd.DataFrame,
    horizons: list[int],
    factors: list[str],
    split_ratio: float,
) -> pd.DataFrame:
    daily = compute_daily_rankic_series(features, horizons, factors)
    rows: list[dict[str, Any]] = []
    if daily.empty:
        return pd.DataFrame(rows)
    dates = sorted(daily["trade_date"].dropna().unique().tolist())
    if len(dates) < 4:
        return pd.DataFrame(rows)
    safe_ratio = min(max(split_ratio, 0.10), 0.90)
    split_index = min(max(1, int(len(dates) * safe_ratio)), len(dates) - 1)
    split_date = dates[split_index - 1]
    sample_specs = [
        ("in_sample", "样本内", dates[:split_index]),
        ("out_of_sample", "样本外", dates[split_index:]),
    ]
    for sample, sample_zh, sample_dates in sample_specs:
        sample_set = set(sample_dates)
        sample_daily = daily[daily["trade_date"].isin(sample_set)]
        for (factor, factor_zh, horizon), group in sample_daily.groupby(["factor", "factor_zh", "horizon"]):
            stats = summarize_rankic_values(group["rankic"])
            rows.append(
                {
                    "factor": factor,
                    "factor_zh": factor_zh,
                    "horizon": int(horizon),
                    "sample": sample,
                    "sample_zh": sample_zh,
                    "split_date": split_date,
                    "start_date": min(sample_dates) if sample_dates else "",
                    "end_date": max(sample_dates) if sample_dates else "",
                    **stats,
                }
            )
    return pd.DataFrame(rows)


def build_validation_decisions(rankic: pd.DataFrame, group_returns: pd.DataFrame, topn: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if rankic.empty:
        return pd.DataFrame(rows)
    for row in rankic.to_dict("records"):
        factor = row["factor"]
        horizon = int(row["horizon"])
        group_slice = group_returns[(group_returns["factor"] == factor) & (group_returns["horizon"] == horizon)]
        top_group = group_slice[group_slice["quantile"] == 5]["mean_forward_return"].mean()
        bottom_group = group_slice[group_slice["quantile"] == 1]["mean_forward_return"].mean()
        spread = top_group - bottom_group if pd.notna(top_group) and pd.notna(bottom_group) else math.nan
        topn_slice = topn[(topn["factor"] == factor) & (topn["horizon"] == horizon)]
        topn_net = topn_slice["net_forward_return"].mean() if not topn_slice.empty else math.nan
        mean_ic = to_float(row.get("mean_rankic"))
        t_stat = to_float(row.get("rankic_t_stat"))
        positive = to_float(row.get("positive_ratio"))
        decision = "fail"
        reasons: list[str] = []
        if mean_ic is not None and mean_ic > 0.02:
            reasons.append("RankIC为正")
        if t_stat is not None and t_stat > 1.5:
            reasons.append("T值超过1.5")
        if positive is not None and positive >= 0.52:
            reasons.append("正IC比例超过52%")
        if pd.notna(spread) and spread > 0:
            reasons.append("高分组跑赢低分组")
        if pd.notna(topn_net) and topn_net > 0:
            reasons.append("TopN成本后收益为正")
        if len(reasons) >= 4:
            decision = "pass"
        elif len(reasons) >= 2:
            decision = "weak"
        rows.append(
            {
                "factor": factor,
                "factor_zh": row.get("factor_zh", factor),
                "horizon": horizon,
                "decision": decision,
                "mean_rankic": mean_ic,
                "rankic_t_stat": t_stat,
                "positive_ratio": positive,
                "top_minus_bottom_group_return": spread,
                "topn_net_return": topn_net,
                "decision_reasons": ";".join(reasons) if reasons else "未达到验证门槛",
            }
        )
    return pd.DataFrame(rows)


def build_current_research_basket(
    *,
    ranking: pd.DataFrame,
    candidate_count: int,
    max_parent_weight: float,
) -> pd.DataFrame:
    if ranking.empty or candidate_count <= 0:
        return pd.DataFrame()
    max_parent_count = max(1, int(math.floor(candidate_count * max_parent_weight)))
    selected: list[dict[str, Any]] = []
    parent_counts: dict[str, int] = {}
    eligible = ranking[ranking["candidate_status"] == "industry_value_oversold_candidate"].copy()
    for row in eligible.sort_values("industry_value_score", ascending=False).to_dict("records"):
        parent = str(row.get("parent_industry", ""))
        if parent_counts.get(parent, 0) >= max_parent_count:
            continue
        selected.append(row)
        parent_counts[parent] = parent_counts.get(parent, 0) + 1
        if len(selected) >= candidate_count:
            break
    if not selected:
        return pd.DataFrame()
    weight = 1.0 / len(selected)
    rows: list[dict[str, Any]] = []
    for rank, row in enumerate(selected, start=1):
        rows.append(
            {
                "basket_rank": rank,
                "industry_code": row.get("industry_code", ""),
                "industry_name": row.get("industry_name", ""),
                "parent_industry": row.get("parent_industry", ""),
                "research_weight": weight,
                "industry_value_score": row.get("industry_value_score", math.nan),
                "industry_valuation_score": row.get("industry_valuation_score", math.nan),
                "industry_oversold_score": row.get("industry_oversold_score", math.nan),
                "basket_rule": "等权研究篮子；同一申万一级分组数量受限；仅用于行业指数研究复核",
            }
        )
    return pd.DataFrame(rows)


def summarize_current_research_basket(basket: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if basket.empty:
        return pd.DataFrame(
            [
                {
                    "metric": "selected_count",
                    "value": 0,
                    "note": f"没有行业同时通过V{VERSION}候选门槛和分组约束",
                }
            ]
        )
    parent_counts = basket["parent_industry"].value_counts().to_dict()
    return pd.DataFrame(
        [
            {"metric": "selected_count", "value": int(len(basket)), "note": "当前研究篮子行业数量"},
            {"metric": "target_count", "value": int(args.candidate_count), "note": "目标研究篮子数量"},
            {"metric": "parent_group_count", "value": int(len(parent_counts)), "note": "覆盖申万一级分组数量"},
            {
                "metric": "parent_distribution",
                "value": json.dumps(parent_counts, ensure_ascii=False),
                "note": "研究篮子申万一级分布",
            },
        ]
    )


def attach_research_basket_columns(ranking: pd.DataFrame, basket: pd.DataFrame) -> pd.DataFrame:
    frame = ranking.copy()
    frame["research_weight"] = 0.0
    if basket.empty:
        return frame
    weights = basket.set_index("industry_code")["research_weight"].to_dict()
    frame["research_weight"] = frame["industry_code"].map(weights).fillna(0.0)
    return frame


def build_real_data_audit(
    *,
    fundamentals: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    valuation_snapshot: dict[str, Any],
    refresh_history: bool,
) -> dict[str, Any]:
    empty_histories = [code for code, history in histories.items() if history.empty]
    short_histories = [code for code, history in histories.items() if 0 < len(history) < 252]
    return {
        "real_data_only": True,
        "sample_or_mock_data_used": False,
        "fundamental_rows": int(len(fundamentals)),
        "industry_history_count": int(len(histories)),
        "empty_history_count": int(len(empty_histories)),
        "empty_history_codes": empty_histories,
        "short_history_count": int(len(short_histories)),
        "short_history_codes": short_histories,
        "valuation_snapshot_path": valuation_snapshot["path"],
        "valuation_snapshot_rows": valuation_snapshot["rows"],
        "valuation_snapshot_observed_at": valuation_snapshot.get("observed_at", ""),
        "valuation_snapshot_available_date": valuation_snapshot.get("available_date", ""),
        "valuation_snapshot_data_status": valuation_snapshot.get("data_status", ""),
        "valuation_snapshot_pit_eligible": bool(valuation_snapshot.get("pit_eligible", False)),
        "refresh_history": bool(refresh_history),
    }


def render_validation_report(
    *,
    agent_result: dict[str, Any],
    ranking: pd.DataFrame,
    rankic: pd.DataFrame,
    group_returns: pd.DataFrame,
    topn: pd.DataFrame,
    validation_decisions: pd.DataFrame,
    yearly_validation: pd.DataFrame,
    regime_validation: pd.DataFrame,
    rolling_rankic: pd.DataFrame,
    oos_validation: pd.DataFrame,
    current_basket: pd.DataFrame,
    basket_summary: pd.DataFrame,
    real_data_audit: dict[str, Any],
    summary: dict[str, Any],
    top: int,
) -> str:
    lines = [
        "# 行业指数研究验证报告",
        "",
        f"版本：{VERSION}",
        "",
        "## 研究边界",
        "",
        summary["research_boundary"],
        "",
        "## 运行摘要",
        "",
        f"- 行业层级：{industry_level_label(summary['industry_level'])}",
        f"- 行业数量：{summary['industry_rows']}",
        f"- 历史特征行数：{summary['historical_feature_rows']}",
        f"- 当前候选行业数：{summary['candidate_count']}",
        f"- V{VERSION} 验证结论：{summary['overall_validation_decision']}",
        f"- 研究篮子数量：{summary['current_research_basket_rows']}",
        f"- 验证周期：{', '.join(str(value) for value in summary['horizons'])} 个交易日",
        f"- 数据模式：{summary['data_mode']}",
        f"- 估值快照归档行数：{summary['valuation_snapshot_rows']}",
        "",
        "## 当前候选快照",
        "",
    ]
    lines.append(render_candidate_report(agent_result, top=top))
    lines.extend(["", "## 真实数据审计", ""])
    lines.extend(
        [
            f"- 样本或模拟数据：{'是' if real_data_audit.get('sample_or_mock_data_used') else '否'}",
            f"- 行业历史数量：{real_data_audit.get('industry_history_count')}",
            f"- 空历史行业数：{real_data_audit.get('empty_history_count')}",
            f"- 短历史行业数：{real_data_audit.get('short_history_count')}",
            f"- 估值快照：`{summary['valuation_snapshot_path']}`",
            f"- 快照观察时间：{summary['valuation_snapshot_observed_at']}",
            f"- 快照数据状态：`{summary['valuation_snapshot_data_status']}`；PIT 合格：`{str(summary['valuation_snapshot_pit_eligible']).lower()}`",
            "",
        ]
    )
    lines.extend(["", "## 历史验证", ""])
    if rankic.empty:
        lines.append("未生成 RankIC 结果。")
    else:
        lines.extend(
            [
                "| 因子 | 周期 | 平均RankIC | T值 | 正IC比例 | 样本数 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in rankic.to_dict("records"):
            lines.append(
                "| {factor} | {horizon} | {mean} | {tstat} | {positive} | {obs} |".format(
                    factor=row.get("factor_zh", row.get("factor", "")),
                    horizon=int(row["horizon"]),
                    mean=fmt_float(row["mean_rankic"], 4),
                    tstat=fmt_float(row["rankic_t_stat"], 2),
                    positive=fmt_pct(row["positive_ratio"]),
                    obs=int(row["observations"]),
                )
            )
    lines.extend(["", f"## V{VERSION} 验证判定", ""])
    if validation_decisions.empty:
        lines.append("未生成验证判定。")
    else:
        lines.extend(
            [
                "| 因子 | 周期 | 判定 | RankIC | T值 | Top-Bottom | TopN成本后 | 原因 |",
                "|---|---:|---|---:|---:|---:|---:|---|",
            ]
        )
        for row in validation_decisions.to_dict("records"):
            lines.append(
                "| {factor} | {horizon} | {decision} | {ic} | {tstat} | {spread} | {net} | {reason} |".format(
                    factor=row.get("factor_zh", row.get("factor", "")),
                    horizon=int(row["horizon"]),
                    decision=validation_decision_label(str(row["decision"])),
                    ic=fmt_float(row.get("mean_rankic"), 4),
                    tstat=fmt_float(row.get("rankic_t_stat"), 2),
                    spread=fmt_pct(row.get("top_minus_bottom_group_return")),
                    net=fmt_pct(row.get("topn_net_return")),
                    reason=row.get("decision_reasons", ""),
                )
            )
    lines.extend(["", "## V1.8 样本外验证", ""])
    if oos_validation.empty:
        lines.append("未生成样本外验证结果。")
    else:
        lines.extend(
            [
                "| 因子 | 周期 | 样本 | 平均RankIC | T值 | 正IC比例 | 样本数 | 区间 |",
                "|---|---:|---|---:|---:|---:|---:|---|",
            ]
        )
        for row in oos_validation.to_dict("records"):
            lines.append(
                "| {factor} | {horizon} | {sample} | {mean} | {tstat} | {positive} | {obs} | {start} 至 {end} |".format(
                    factor=row.get("factor_zh", row.get("factor", "")),
                    horizon=int(row["horizon"]),
                    sample=row.get("sample_zh", row.get("sample", "")),
                    mean=fmt_float(row["mean_rankic"], 4),
                    tstat=fmt_float(row["rankic_t_stat"], 2),
                    positive=fmt_pct(row["positive_ratio"]),
                    obs=int(row["observations"]),
                    start=row.get("start_date", ""),
                    end=row.get("end_date", ""),
                )
            )
    lines.extend(["", "## V1.8 市场状态验证", ""])
    if regime_validation.empty:
        lines.append("未生成市场状态验证结果。")
    else:
        main_regime = regime_validation[regime_validation["factor"] == "stabilized_oversold_signal"].copy()
        if main_regime.empty:
            main_regime = regime_validation.copy()
        lines.extend(
            [
                "| 因子 | 周期 | 市场状态 | 波动状态 | 平均RankIC | T值 | 正IC比例 | 样本数 |",
                "|---|---:|---|---|---:|---:|---:|---:|",
            ]
        )
        for row in main_regime.to_dict("records"):
            lines.append(
                "| {factor} | {horizon} | {market} | {vol} | {mean} | {tstat} | {positive} | {obs} |".format(
                    factor=row.get("factor_zh", row.get("factor", "")),
                    horizon=int(row["horizon"]),
                    market=row.get("market_regime", ""),
                    vol=row.get("volatility_regime", ""),
                    mean=fmt_float(row["mean_rankic"], 4),
                    tstat=fmt_float(row["rankic_t_stat"], 2),
                    positive=fmt_pct(row["positive_ratio"]),
                    obs=int(row["observations"]),
                )
            )
    lines.extend(["", "## V1.8 滚动稳定性", ""])
    if rolling_rankic.empty:
        lines.append("未生成滚动 RankIC 结果。")
    else:
        latest_rows = (
            rolling_rankic.sort_values("end_date")
            .groupby(["factor", "factor_zh", "horizon"], as_index=False)
            .tail(1)
            .sort_values(["factor", "horizon"])
        )
        lines.extend(
            [
                "| 因子 | 周期 | 窗口 | 截止日期 | 滚动RankIC | T值 | 正IC比例 |",
                "|---|---:|---:|---|---:|---:|---:|",
            ]
        )
        for row in latest_rows.to_dict("records"):
            lines.append(
                "| {factor} | {horizon} | {window} | {end_date} | {mean} | {tstat} | {positive} |".format(
                    factor=row.get("factor_zh", row.get("factor", "")),
                    horizon=int(row["horizon"]),
                    window=int(row["window"]),
                    end_date=row.get("end_date", ""),
                    mean=fmt_float(row["rolling_mean_rankic"], 4),
                    tstat=fmt_float(row["rolling_rankic_t_stat"], 2),
                    positive=fmt_pct(row["rolling_positive_ratio"]),
                )
            )
    lines.extend(["", "## V1.8 年度验证", ""])
    if yearly_validation.empty:
        lines.append("未生成年度验证结果。")
    else:
        latest_year = int(yearly_validation["year"].max())
        recent_years = sorted(yearly_validation["year"].dropna().unique().tolist())[-3:]
        recent_yearly = yearly_validation[
            (yearly_validation["factor"] == "stabilized_oversold_signal")
            & (yearly_validation["year"].isin(recent_years))
        ].copy()
        if recent_yearly.empty:
            recent_yearly = yearly_validation[yearly_validation["year"].isin(recent_years)].copy()
        lines.append(f"下表展示最近三年可验证年度结果；当前最新年度为 {latest_year}。")
        lines.extend(
            [
                "| 因子 | 周期 | 年度 | 平均RankIC | T值 | 正IC比例 | 样本数 |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in recent_yearly.sort_values(["year", "factor", "horizon"]).to_dict("records"):
            lines.append(
                "| {factor} | {horizon} | {year} | {mean} | {tstat} | {positive} | {obs} |".format(
                    factor=row.get("factor_zh", row.get("factor", "")),
                    horizon=int(row["horizon"]),
                    year=int(row["year"]),
                    mean=fmt_float(row["mean_rankic"], 4),
                    tstat=fmt_float(row["rankic_t_stat"], 2),
                    positive=fmt_pct(row["positive_ratio"]),
                    obs=int(row["observations"]),
                )
            )
    lines.extend(["", "## Top-N 验证摘要", ""])
    if topn.empty:
        lines.append("未生成 Top-N 验证结果。")
    else:
        summary_rows = (
            topn.groupby(["factor", "factor_zh", "horizon"])
            .agg(
                mean_gross_return=("gross_forward_return", "mean"),
                mean_net_return=("net_forward_return", "mean"),
                mean_turnover=("turnover", "mean"),
                samples=("trade_date", "count"),
            )
            .reset_index()
        )
        lines.extend(
            [
                "| 因子 | 周期 | 平均未来收益 | 成本后收益 | 换手率 | 样本数 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in summary_rows.to_dict("records"):
            lines.append(
                "| {factor} | {horizon} | {gross} | {net} | {turnover} | {samples} |".format(
                    factor=row.get("factor_zh", row.get("factor", "")),
                    horizon=int(row["horizon"]),
                    gross=fmt_pct(row["mean_gross_return"]),
                    net=fmt_pct(row["mean_net_return"]),
                    turnover=fmt_pct(row["mean_turnover"]),
                    samples=int(row["samples"]),
                )
            )
    lines.extend(["", "## 当前研究篮子", ""])
    if current_basket.empty:
        lines.append(f"没有行业同时通过 V{VERSION} 候选门槛和分组约束。")
    else:
        lines.extend(
            [
                "| 排名 | 行业 | 上级行业 | 权重 | 综合分 | 估值分 | 超跌分 |",
                "|---:|---|---|---:|---:|---:|---:|",
            ]
        )
        for row in current_basket.to_dict("records"):
            lines.append(
                "| {rank} | {industry} | {parent} | {weight} | {score} | {value} | {oversold} |".format(
                    rank=int(row["basket_rank"]),
                    industry=row["industry_name"],
                    parent=row["parent_industry"],
                    weight=fmt_pct(row["research_weight"]),
                    score=fmt_float(row["industry_value_score"], 4),
                    value=fmt_float(row["industry_valuation_score"], 4),
                    oversold=fmt_float(row["industry_oversold_score"], 4),
                )
            )
    if not basket_summary.empty:
        lines.extend(["", "研究篮子摘要："])
        for row in basket_summary.to_dict("records"):
            lines.append(f"- {row['metric']}: {row['value']} ({row['note']})")
    lines.extend(
        [
            "",
            "## 复现与排查文件",
            "",
            "- `debug/all_ranked_industries.csv`",
            "- `debug/raw_industry_panel.csv`",
            "- `debug/agent_results.json`",
            "- `debug/historical_feature_panel.csv`",
            "- `debug/rankic_report.csv`",
            "- `debug/group_return_report.csv`",
            "- `debug/topn_backtest.csv`",
            "- `debug/validation_decisions.csv`",
            "- `debug/yearly_validation_report.csv`",
            "- `debug/regime_validation_report.csv`",
            "- `debug/rolling_rankic_report.csv`",
            "- `debug/oos_validation_report.csv`",
            "- `debug/current_research_basket.csv`",
            "- `debug/research_basket_summary.csv`",
            "- `debug/real_data_audit.json`",
            "",
        ]
    )
    return "\n".join(lines)


def overall_validation_decision(decisions: pd.DataFrame, oos_validation: pd.DataFrame) -> str:
    if decisions.empty:
        return "未通过：没有可用验证结果"
    counts = decisions["decision"].value_counts().to_dict()
    oos_positive = pd.DataFrame()
    if not oos_validation.empty:
        oos_positive = oos_validation[
            (oos_validation["sample"] == "out_of_sample")
            & (oos_validation["mean_rankic"] > 0)
            & (oos_validation["positive_ratio"] >= 0.50)
        ]
    if counts.get("pass", 0) >= 2 and not oos_positive.empty:
        return "通过：样本内验证和样本外 RankIC 同时达到正向门槛"
    if counts.get("weak", 0) >= 1 or counts.get("pass", 0) >= 1:
        if oos_positive.empty:
            return "偏弱：存在方向性证据，但样本外稳定性不足"
        return "偏弱：存在样本外正向证据，但未达到可推广门槛"
    return f"未通过：V{VERSION} 因子验证未支持稳定正向预测"


def validation_decision_label(value: str) -> str:
    return {"pass": "通过", "weak": "偏弱", "fail": "未通过"}.get(value, value)


def parse_horizons(value: str) -> list[int]:
    horizons = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not horizons:
        raise ValueError("horizons cannot be empty")
    return horizons


def normalize_industry_code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".SI"):
        text = text[:-3]
    return text.zfill(6)


def percent_to_decimal(value: float | None) -> float | None:
    if value is None:
        return None
    if abs(value) > 1:
        return value / 100.0
    return value


def to_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def period_return(values: list[float], index: int, window: int) -> float:
    if index - window < 0 or values[index - window] == 0:
        return math.nan
    return values[index] / values[index - window] - 1.0


def forward_return(values: list[float], index: int, horizon: int) -> float:
    if index + horizon >= len(values) or values[index] == 0:
        return math.nan
    return values[index + horizon] / values[index] - 1.0


def drawdown(values: list[float], index: int, window: int) -> float:
    start = max(0, index - window + 1)
    sample = values[start : index + 1]
    if not sample:
        return math.nan
    peak = max(sample)
    if peak == 0:
        return math.nan
    return values[index] / peak - 1.0


def volatility(values: list[float], index: int, window: int) -> float:
    if index - window < 1:
        return math.nan
    returns = []
    for offset in range(index - window + 1, index + 1):
        previous = values[offset - 1]
        if previous:
            returns.append(values[offset] / previous - 1.0)
    if len(returns) < 2:
        return math.nan
    return float(pd.Series(returns).std(ddof=1) * math.sqrt(252))


def window_mean(values: list[float], index: int, window: int) -> float:
    start = max(0, index - window + 1)
    sample = [value for value in values[start : index + 1] if pd.notna(value)]
    return float(pd.Series(sample).mean()) if sample else math.nan


def cross_section_rank(frame: pd.DataFrame, group_col: str, value_col: str) -> pd.Series:
    return frame.groupby(group_col)[value_col].rank(pct=True, method="average")


def spearman_corr(left: pd.Series, right: pd.Series) -> float:
    ranked = pd.DataFrame({"left": left, "right": right}).dropna()
    if len(ranked) < 2:
        return math.nan
    return float(ranked["left"].rank(method="average").corr(ranked["right"].rank(method="average")))


def fmt_float(value: Any, digits: int) -> str:
    number = to_float(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}"


def fmt_pct(value: Any) -> str:
    number = to_float(value)
    if number is None:
        return ""
    return f"{number * 100:.2f}%"


def industry_level_label(value: str) -> str:
    return {"first": "申万一级", "second": "申万二级"}.get(value, value)


def iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD") from exc


def trade_date_error(value: str, today: date) -> str | None:
    trade_date = date.fromisoformat(value)
    if trade_date > today:
        return f"--trade-date {value} is in the future; run industry validation on or after that date."
    return None


def self_check() -> None:
    assert iso_date("2026-06-19") == "2026-06-19"
    assert trade_date_error("2026-06-19", date(2026, 6, 20)) is None
    assert "future" in str(trade_date_error("2026-06-23", date(2026, 6, 20)))
    observed = datetime(2026, 6, 20, 12, 0, tzinfo=SHANGHAI)
    assert current_snapshot_as_of_error("2026-06-20", observed) is None
    assert "cannot be archived" in str(current_snapshot_as_of_error("2026-06-19", observed))
    sample = pd.DataFrame({"日期": pd.to_datetime(["2026-07-01"]), "收盘": [100.0], "成交额": [1.0]})
    assert current_history_metrics(sample, "2026-07-14")["history_fresh"] is False
    assert current_history_metrics(sample, "2026-07-02")["history_fresh"] is True
    print("self_check=pass")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
