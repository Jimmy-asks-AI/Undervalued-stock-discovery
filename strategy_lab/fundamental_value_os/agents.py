from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


Record = Dict[str, Any]
ScoreMap = Dict[str, float]


@dataclass
class AgentResult:
    agent: str
    summary: str
    scores: Dict[str, Any]
    issues: List[str]


REQUIRED_FIELDS = [
    "asset",
    "name",
    "trade_date",
    "available_date",
    "industry",
    "pe_ttm",
    "pb",
    "pcf_ocf_ttm",
    "dividend_yield_ttm",
    "roe_ttm",
    "roic_ttm",
    "revenue_cagr_3y",
    "net_profit_cagr_3y",
    "ocf_to_net_income",
    "fcf_yield_ttm",
    "debt_to_assets",
    "interest_coverage",
    "payout_ratio",
    "st_flag",
    "suspend_flag",
    "avg_amount_20d",
]


def load_csv_records(path: str | Path) -> List[Record]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_outputs(result: Dict[str, Any], output_dir: str | Path) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    with (output_path / "agent_results.json").open("w", encoding="utf-8") as handle:
        json.dump(result["agent_results"], handle, ensure_ascii=False, indent=2)

    ranking = result["candidate_ranking"]
    if ranking:
        with (output_path / "candidate_ranking.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(ranking[0].keys()))
            writer.writeheader()
            writer.writerows(ranking)

    with (output_path / "candidate_report.md").open("w", encoding="utf-8") as handle:
        handle.write(render_candidate_report(result))


def run_fundamental_value_agents(records: Sequence[Record]) -> Dict[str, Any]:
    normalized = [_normalize_record(record) for record in records]
    asset_ids = [record["asset"] for record in normalized]

    data_result = _fundamental_data_steward(normalized)
    valuation_result = _valuation_factor_researcher(normalized)
    industry_result = _industry_relative_value_agent(normalized)
    profitability_result = _profitability_growth_analyst(normalized)
    shareholder_result = _shareholder_return_analyst(normalized)
    accounting_result = _accounting_quality_auditor(normalized)
    relative_confirmation_result = _relative_cheapness_confirmation_auditor(
        normalized,
        valuation_result,
        industry_result,
        profitability_result,
        shareholder_result,
        accounting_result,
    )
    trap_result = _value_trap_risk_agent(normalized, data_result, accounting_result)

    ranking: List[Dict[str, Any]] = []
    for record in normalized:
        asset = record["asset"]
        valuation = valuation_result.scores[asset]
        industry = industry_result.scores[asset]
        profitability = profitability_result.scores["profitability_quality"][asset]
        growth = profitability_result.scores["growth_stability"][asset]
        shareholder = shareholder_result.scores[asset]
        safety = accounting_result.scores["cash_flow_safety"][asset]
        penalty = (
            data_result.scores["penalty"].get(asset, 0.0)
            + accounting_result.scores["penalty"].get(asset, 0.0)
            + relative_confirmation_result.scores["penalty"].get(asset, 0.0)
            + trap_result.scores["penalty"].get(asset, 0.0)
        )

        raw_score = (
            0.35 * valuation
            + 0.20 * industry
            + 0.15 * profitability
            + 0.10 * growth
            + 0.10 * shareholder
            + 0.10 * safety
            - penalty
        )
        composite = _clamp(raw_score)
        hard_block = bool(trap_result.scores["hard_block"].get(asset, False))
        bucket = _classify_bucket(
            composite=composite,
            valuation=valuation,
            industry=industry,
            profitability=profitability,
            safety=safety,
            hard_block=hard_block,
        )

        ranking.append(
            {
                "asset": asset,
                "name": record.get("name", ""),
                "trade_date": record.get("trade_date", ""),
                "available_date": record.get("available_date", ""),
                "industry": record.get("industry", ""),
                "bucket": bucket,
                "composite_score": round(composite, 4),
                "valuation_cheapness": round(valuation, 4),
                "industry_relative_value": round(industry, 4),
                "profitability_quality": round(profitability, 4),
                "growth_stability": round(growth, 4),
                "shareholder_return": round(shareholder, 4),
                "cash_flow_safety": round(safety, 4),
                "relative_cheapness_confirmation_penalty": round(
                    relative_confirmation_result.scores["penalty"].get(asset, 0.0), 4
                ),
                "relative_value_trap_flag": bool(
                    relative_confirmation_result.scores["flag"].get(asset, False)
                ),
                "total_penalty": round(penalty, 4),
                "hard_block": hard_block,
                "data_status": record.get("data_status", "research_only"),
            }
        )

    ranking.sort(key=lambda row: row["composite_score"], reverse=True)

    validation_result = _factor_validation_auditor(normalized, ranking)
    report_result = _research_report_synthesizer(ranking)

    agent_results = [
        data_result,
        valuation_result,
        industry_result,
        profitability_result,
        shareholder_result,
        accounting_result,
        relative_confirmation_result,
        trap_result,
        validation_result,
        report_result,
    ]

    return {
        "run_manifest": {
            "system": "Fundamental Value Research OS",
            "version": "0.1.0",
            "asset_count": len(asset_ids),
            "ranking_count": len(ranking),
            "research_boundary": "research_only until PIT validation and historical labels are connected",
        },
        "agent_results": [asdict(result) for result in agent_results],
        "candidate_ranking": ranking,
    }


def render_candidate_report(result: Dict[str, Any]) -> str:
    lines = [
        "# Fundamental Value Candidate Report",
        "",
        f"System: {result['run_manifest']['system']} {result['run_manifest']['version']}",
        f"Asset count: {result['run_manifest']['asset_count']}",
        "",
        "## Boundary",
        "",
        result["run_manifest"]["research_boundary"],
        "",
        "## Top Ranked Assets",
        "",
        "| Rank | Asset | Name | Bucket | Score | Valuation | Industry | Quality | Safety | Penalty |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]

    for idx, row in enumerate(result["candidate_ranking"], start=1):
        lines.append(
            "| {rank} | {asset} | {name} | {bucket} | {score:.4f} | {valuation:.4f} | {industry:.4f} | {quality:.4f} | {safety:.4f} | {penalty:.4f} |".format(
                rank=idx,
                asset=row["asset"],
                name=row["name"],
                bucket=row["bucket"],
                score=row["composite_score"],
                valuation=row["valuation_cheapness"],
                industry=row["industry_relative_value"],
                quality=row["profitability_quality"],
                safety=row["cash_flow_safety"],
                penalty=row["total_penalty"],
            )
        )

    lines.extend(
        [
            "",
            "## Required Next Validation",
            "",
            "- Replace sample/current snapshot data with point-in-time fundamental data.",
            "- Add adjusted total-return labels before factor performance claims.",
            "- Run IC/RankIC, grouped returns, neutralized tests, OOS splits, costs, turnover, and capacity checks.",
            "- Keep timing and T-trading overlays separate from fundamental undervaluation scoring.",
            "",
        ]
    )
    return "\n".join(lines)


def _fundamental_data_steward(records: Sequence[Record]) -> AgentResult:
    penalty: ScoreMap = {}
    issues: List[str] = []
    seen: set[tuple[str, str]] = set()

    for index, record in enumerate(records, start=1):
        asset = record["asset"]
        row_penalty = 0.0
        missing = [field for field in REQUIRED_FIELDS if _is_missing(record.get(field))]
        if missing:
            issues.append(f"{asset}: missing fields {missing}")
            row_penalty += min(0.20, 0.02 * len(missing))

        key = (asset, str(record.get("trade_date", "")))
        if key in seen:
            issues.append(f"{asset}: duplicated asset + trade_date at row {index}")
            row_penalty += 0.10
        seen.add(key)

        available_date = str(record.get("available_date", ""))
        trade_date = str(record.get("trade_date", ""))
        if available_date and trade_date and available_date > trade_date:
            issues.append(f"{asset}: available_date later than trade_date")
            row_penalty += 0.25

        if str(record.get("data_status", "research_only")) != "pit_verified":
            row_penalty += 0.03

        penalty[asset] = row_penalty

    return AgentResult(
        agent="fundamental_data_steward",
        summary="Validated required fields, duplicate keys, available_date order, and data status.",
        scores={"penalty": penalty},
        issues=issues,
    )


def _valuation_factor_researcher(records: Sequence[Record]) -> AgentResult:
    pe = _percentile_scores(records, "pe_ttm", higher_better=False, invalid_if_nonpositive=True)
    pb = _percentile_scores(records, "pb", higher_better=False, invalid_if_nonpositive=True)
    pcf = _percentile_scores(records, "pcf_ocf_ttm", higher_better=False, invalid_if_nonpositive=True)
    fcf = _percentile_scores(records, "fcf_yield_ttm", higher_better=True)
    dividend = _percentile_scores(records, "dividend_yield_ttm", higher_better=True)

    scores = {}
    for record in records:
        asset = record["asset"]
        scores[asset] = _mean_score([pe[asset], pb[asset], pcf[asset], fcf[asset], dividend[asset]])

    return AgentResult(
        agent="valuation_factor_researcher",
        summary="Scored absolute cheapness from PE, PB, PCF, FCF yield, and dividend yield ranks.",
        scores=scores,
        issues=[],
    )


def _industry_relative_value_agent(records: Sequence[Record]) -> AgentResult:
    pe = _group_percentile_scores(records, "industry", "pe_ttm", higher_better=False, invalid_if_nonpositive=True)
    pb = _group_percentile_scores(records, "industry", "pb", higher_better=False, invalid_if_nonpositive=True)
    dividend = _group_percentile_scores(records, "industry", "dividend_yield_ttm", higher_better=True)

    scores = {}
    issues: List[str] = []
    group_counts: Dict[str, int] = {}
    for record in records:
        group = str(record.get("industry", "UNKNOWN"))
        group_counts[group] = group_counts.get(group, 0) + 1

    for record in records:
        asset = record["asset"]
        group = str(record.get("industry", "UNKNOWN"))
        if group_counts[group] < 3:
            issues.append(f"{asset}: industry peer count below 3; score is low-confidence")
        scores[asset] = _mean_score([pe[asset], pb[asset], dividend[asset]])

    return AgentResult(
        agent="industry_relative_value_agent",
        summary="Scored valuation ranks within industry groups.",
        scores=scores,
        issues=issues,
    )


def _profitability_growth_analyst(records: Sequence[Record]) -> AgentResult:
    roe = _percentile_scores(records, "roe_ttm", higher_better=True)
    roic = _percentile_scores(records, "roic_ttm", higher_better=True)
    revenue = _percentile_scores(records, "revenue_cagr_3y", higher_better=True)
    profit = _percentile_scores(records, "net_profit_cagr_3y", higher_better=True)

    profitability_quality = {}
    growth_stability = {}
    for record in records:
        asset = record["asset"]
        profitability_quality[asset] = _mean_score([roe[asset], roic[asset]])
        growth_stability[asset] = _mean_score([revenue[asset], profit[asset]])

    return AgentResult(
        agent="profitability_growth_analyst",
        summary="Scored profitability quality and three-year growth stability.",
        scores={
            "profitability_quality": profitability_quality,
            "growth_stability": growth_stability,
        },
        issues=[],
    )


def _shareholder_return_analyst(records: Sequence[Record]) -> AgentResult:
    dividend = _percentile_scores(records, "dividend_yield_ttm", higher_better=True)
    fcf = _percentile_scores(records, "fcf_yield_ttm", higher_better=True)
    scores = {}
    issues: List[str] = []

    for record in records:
        asset = record["asset"]
        payout = _to_float(record.get("payout_ratio"))
        payout_score = 0.5
        if payout is not None:
            payout_score = _clamp(1.0 - abs(payout - 0.45) / 0.55)
            if payout > 0.90:
                issues.append(f"{asset}: payout ratio above 90%; dividend sustainability warning")
        scores[asset] = _mean_score([dividend[asset], fcf[asset], payout_score])

    return AgentResult(
        agent="shareholder_return_analyst",
        summary="Scored dividend yield, free cash-flow support, and payout sustainability.",
        scores=scores,
        issues=issues,
    )


def _accounting_quality_auditor(records: Sequence[Record]) -> AgentResult:
    ocf = _percentile_scores(records, "ocf_to_net_income", higher_better=True)
    leverage = _percentile_scores(records, "debt_to_assets", higher_better=False)
    coverage = _percentile_scores(records, "interest_coverage", higher_better=True)
    safety: ScoreMap = {}
    penalty: ScoreMap = {}
    issues: List[str] = []

    for record in records:
        asset = record["asset"]
        safety[asset] = _mean_score([ocf[asset], leverage[asset], coverage[asset]])
        row_penalty = 0.0

        ocf_value = _to_float(record.get("ocf_to_net_income"))
        debt = _to_float(record.get("debt_to_assets"))
        interest = _to_float(record.get("interest_coverage"))

        if ocf_value is not None and ocf_value < 0.60:
            issues.append(f"{asset}: operating cash flow to net income below 0.60")
            row_penalty += 0.08
        if debt is not None and debt > 0.75:
            issues.append(f"{asset}: debt to assets above 0.75")
            row_penalty += 0.08
        if interest is not None and interest < 2.0:
            issues.append(f"{asset}: interest coverage below 2.0")
            row_penalty += 0.08

        penalty[asset] = row_penalty

    return AgentResult(
        agent="accounting_quality_auditor",
        summary="Scored cash-flow safety and flagged accounting risk conditions.",
        scores={"cash_flow_safety": safety, "penalty": penalty},
        issues=issues,
    )


def _relative_cheapness_confirmation_auditor(
    records: Sequence[Record],
    valuation_result: AgentResult,
    industry_result: AgentResult,
    profitability_result: AgentResult,
    shareholder_result: AgentResult,
    accounting_result: AgentResult,
) -> AgentResult:
    penalty: ScoreMap = {}
    flag: Dict[str, bool] = {}
    issues: List[str] = []
    industry_counts: Dict[str, int] = {}

    for record in records:
        industry = str(record.get("industry", "UNKNOWN"))
        industry_counts[industry] = industry_counts.get(industry, 0) + 1

    for record in records:
        asset = record["asset"]
        industry = str(record.get("industry", "UNKNOWN"))
        valuation = float(valuation_result.scores.get(asset, 0.5))
        industry_value = float(industry_result.scores.get(asset, 0.5))
        profitability = float(profitability_result.scores["profitability_quality"].get(asset, 0.5))
        growth = float(profitability_result.scores["growth_stability"].get(asset, 0.5))
        shareholder = float(shareholder_result.scores.get(asset, 0.5))
        safety = float(accounting_result.scores["cash_flow_safety"].get(asset, 0.5))

        row_penalty = 0.0
        row_flag = False
        relative_cheap = industry_value >= 0.80 and valuation >= 0.60

        if relative_cheap and industry_counts.get(industry, 0) < 5:
            row_penalty += 0.05
            row_flag = True
            issues.append(f"{asset}: industry-relative cheapness has fewer than 5 peers")
        if relative_cheap and profitability < 0.45:
            row_penalty += 0.08
            row_flag = True
            issues.append(f"{asset}: industry-relative cheapness not confirmed by profitability quality")
        if relative_cheap and growth < 0.35:
            row_penalty += 0.06
            row_flag = True
            issues.append(f"{asset}: industry-relative cheapness paired with weak growth stability")
        if relative_cheap and safety < 0.45:
            row_penalty += 0.08
            row_flag = True
            issues.append(f"{asset}: industry-relative cheapness not confirmed by cash-flow safety")
        if relative_cheap and shareholder < 0.30:
            row_penalty += 0.04
            row_flag = True
            issues.append(f"{asset}: industry-relative cheapness has weak shareholder-return support")

        ocf_value = _to_float(record.get("ocf_to_net_income"))
        revenue_growth = _to_float(record.get("revenue_cagr_3y"))
        profit_growth = _to_float(record.get("net_profit_cagr_3y"))
        if relative_cheap and ocf_value is not None and ocf_value < 0.60:
            row_penalty += 0.08
            row_flag = True
            issues.append(f"{asset}: industry-relative cheapness with OCF/net income below 0.60")
        if relative_cheap and revenue_growth is not None and revenue_growth < -0.10:
            row_penalty += 0.06
            row_flag = True
            issues.append(f"{asset}: industry-relative cheapness with revenue decline below -10%")
        if relative_cheap and profit_growth is not None and profit_growth < -0.30:
            row_penalty += 0.08
            row_flag = True
            issues.append(f"{asset}: industry-relative cheapness with profit decline below -30%")

        penalty[asset] = min(row_penalty, 0.30)
        flag[asset] = row_flag

    return AgentResult(
        agent="relative_cheapness_confirmation_auditor",
        summary="Penalized industry-relative cheapness when not confirmed by profitability, growth, cash flow, peer count, or shareholder-return support.",
        scores={"penalty": penalty, "flag": flag},
        issues=issues,
    )


def _value_trap_risk_agent(
    records: Sequence[Record],
    data_result: AgentResult,
    accounting_result: AgentResult,
) -> AgentResult:
    penalty: ScoreMap = {}
    hard_block: Dict[str, bool] = {}
    issues: List[str] = []

    for record in records:
        asset = record["asset"]
        row_penalty = 0.0
        blocked = False

        if _to_bool(record.get("st_flag")):
            blocked = True
            row_penalty += 0.45
            issues.append(f"{asset}: ST flag hard block")
        if _to_bool(record.get("suspend_flag")):
            blocked = True
            row_penalty += 0.45
            issues.append(f"{asset}: suspension flag hard block")

        avg_amount = _to_float(record.get("avg_amount_20d"))
        if avg_amount is not None and avg_amount < 20_000_000:
            row_penalty += 0.08
            issues.append(f"{asset}: 20-day average amount below 20m")

        dividend_yield = _to_float(record.get("dividend_yield_ttm"))
        payout = _to_float(record.get("payout_ratio"))
        ocf = _to_float(record.get("ocf_to_net_income"))
        if dividend_yield is not None and dividend_yield > 0.07 and payout is not None and payout > 0.90:
            row_penalty += 0.12
            issues.append(f"{asset}: high dividend yield with high payout ratio")
        if dividend_yield is not None and dividend_yield > 0.07 and ocf is not None and ocf < 0.60:
            row_penalty += 0.12
            issues.append(f"{asset}: high dividend yield with weak cash conversion")

        penalty[asset] = row_penalty
        hard_block[asset] = blocked

    return AgentResult(
        agent="value_trap_risk_agent",
        summary="Applied value-trap, liquidity, ST, and suspension penalties.",
        scores={"penalty": penalty, "hard_block": hard_block},
        issues=issues,
    )


def _factor_validation_auditor(records: Sequence[Record], ranking: Sequence[Dict[str, Any]]) -> AgentResult:
    pit_count = sum(1 for record in records if str(record.get("data_status", "")) == "pit_verified")
    issues = [
        "No historical IC/RankIC validation has been run in V0.1.",
        "No adjusted total-return labels are connected in V0.1.",
        "Scores are candidate research output, not validated alpha.",
    ]
    if pit_count < len(records):
        issues.append("Some rows are not pit_verified; keep output research_only.")

    return AgentResult(
        agent="factor_validation_auditor",
        summary="Marked V0.1 output as candidate-only until PIT labels and validation tests exist.",
        scores={"pit_verified_rows": pit_count, "candidate_rows": len(ranking)},
        issues=issues,
    )


def _research_report_synthesizer(ranking: Sequence[Dict[str, Any]]) -> AgentResult:
    buckets: Dict[str, int] = {}
    for row in ranking:
        buckets[row["bucket"]] = buckets.get(row["bucket"], 0) + 1

    return AgentResult(
        agent="research_report_synthesizer",
        summary="Prepared ranked candidate table and bucket counts.",
        scores={"bucket_counts": buckets},
        issues=[],
    )


def _classify_bucket(
    composite: float,
    valuation: float,
    industry: float,
    profitability: float,
    safety: float,
    hard_block: bool,
) -> str:
    if hard_block:
        return "value_trap_rejected"
    if composite >= 0.68 and profitability >= 0.65 and safety >= 0.60:
        return "quality_value_candidate"
    if composite >= 0.58 and valuation >= 0.70 and safety >= 0.45:
        return "deep_value_candidate"
    if composite >= 0.58 and industry >= 0.65 and safety >= 0.45:
        return "cyclical_value_candidate"
    return "watchlist"


def _normalize_record(record: Record) -> Record:
    normalized = dict(record)
    normalized["asset"] = str(normalized.get("asset", "")).strip()
    for field in REQUIRED_FIELDS:
        normalized.setdefault(field, "")
    normalized.setdefault("data_status", "research_only")
    return normalized


def _percentile_scores(
    records: Sequence[Record],
    field: str,
    *,
    higher_better: bool,
    invalid_if_nonpositive: bool = False,
) -> ScoreMap:
    values: List[tuple[str, float]] = []
    scores: ScoreMap = {}
    for record in records:
        asset = record["asset"]
        value = _to_float(record.get(field))
        if value is None or (invalid_if_nonpositive and value <= 0):
            scores[asset] = 0.25
            continue
        values.append((asset, value))

    if not values:
        return {record["asset"]: scores.get(record["asset"], 0.25) for record in records}

    ranks = _rank_percentiles(values)
    for asset, percentile in ranks.items():
        scores[asset] = percentile if higher_better else 1.0 - percentile
    return {record["asset"]: scores.get(record["asset"], 0.25) for record in records}


def _group_percentile_scores(
    records: Sequence[Record],
    group_field: str,
    value_field: str,
    *,
    higher_better: bool,
    invalid_if_nonpositive: bool = False,
) -> ScoreMap:
    grouped: Dict[str, List[Record]] = {}
    for record in records:
        grouped.setdefault(str(record.get(group_field, "UNKNOWN")), []).append(record)

    scores: ScoreMap = {}
    for group_records in grouped.values():
        group_scores = _percentile_scores(
            group_records,
            value_field,
            higher_better=higher_better,
            invalid_if_nonpositive=invalid_if_nonpositive,
        )
        scores.update(group_scores)
    return {record["asset"]: scores.get(record["asset"], 0.5) for record in records}


def _rank_percentiles(values: Sequence[tuple[str, float]]) -> ScoreMap:
    clean_values = [(asset, value) for asset, value in values if math.isfinite(value)]
    if len(clean_values) == 1:
        return {clean_values[0][0]: 0.5}
    sorted_values = sorted(clean_values, key=lambda item: item[1])
    denominator = max(1, len(sorted_values) - 1)
    return {asset: index / denominator for index, (asset, _value) in enumerate(sorted_values)}


def _mean_score(values: Iterable[float]) -> float:
    usable = [_clamp(value) for value in values if value is not None and math.isfinite(value)]
    if not usable:
        return 0.5
    return sum(usable) / len(usable)


def _to_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "st", "suspended"}


def _is_missing(value: Any) -> bool:
    return value is None or str(value).strip() == "" or str(value).strip().lower() in {"nan", "none", "null"}


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))
