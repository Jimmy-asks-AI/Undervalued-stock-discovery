from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence


Record = Dict[str, Any]
ScoreMap = Dict[str, float]
SYSTEM_VERSION = "1.8.0"


@dataclass
class AgentResult:
    agent: str
    summary: str
    scores: Dict[str, Any]
    issues: List[str]


REQUIRED_FIELDS = [
    "industry_code",
    "industry_name",
    "industry_level",
    "parent_industry",
    "trade_date",
    "source",
    "data_status",
    "constituent_count",
    "pe_ttm",
    "pb",
    "dividend_yield",
    "industry_close",
    "return_20d",
    "return_60d",
    "return_120d",
    "return_252d",
    "drawdown_252d",
    "volatility_60d",
    "avg_amount_60d",
    "history_days",
    "history_latest_date",
    "history_age_calendar_days",
    "history_fresh",
]


STATUS_ZH = {
    "industry_value_oversold_candidate": "低估超跌候选",
    "watchlist": "观察名单",
    "cheap_but_not_oversold": "估值便宜但未明显超跌",
    "oversold_without_valuation_support": "超跌但估值支撑不足",
    "data_rejected": "数据质量不通过",
}


CANDIDATE_OUTPUT_COLUMNS_ZH = [
    ("rank", "排名"),
    ("industry_code", "行业代码"),
    ("industry_name", "行业名称"),
    ("parent_industry", "上级行业"),
    ("candidate_status", "候选状态"),
    ("industry_value_score", "综合分"),
    ("industry_valuation_score", "估值分"),
    ("industry_oversold_score", "超跌分"),
    ("cycle_quality_score", "周期质量分"),
    ("data_quality_score", "数据质量分"),
    ("total_penalty", "总惩罚分"),
    ("pe_ttm", "PE_TTM"),
    ("pb", "PB"),
    ("dividend_yield", "股息率"),
    ("return_20d", "20日收益"),
    ("return_60d", "60日收益"),
    ("return_120d", "120日收益"),
    ("return_252d", "252日收益"),
    ("drawdown_252d", "252日回撤"),
    ("volatility_60d", "60日波动率"),
    ("avg_amount_60d", "60日平均成交额"),
    ("history_days", "历史天数"),
    ("history_latest_date", "历史最新日期"),
    ("history_age_calendar_days", "历史滞后天数"),
    ("history_fresh", "历史是否新鲜"),
    ("data_status", "数据状态"),
    ("research_weight", "研究篮子权重"),
    ("research_notes", "研究备注"),
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


def run_industry_index_research_agents(records: Sequence[Record]) -> Dict[str, Any]:
    normalized = [_normalize_record(record) for record in records]

    data_result = _industry_data_steward(normalized)
    valuation_result = _industry_fundamental_value_agent(normalized)
    oversold_result = _industry_oversold_reversion_agent(normalized)
    cycle_result = _industry_cycle_quality_agent(normalized, valuation_result, oversold_result)
    trap_result = _industry_value_trap_agent(normalized, data_result, valuation_result, oversold_result)

    ranking: List[Dict[str, Any]] = []
    for record in normalized:
        industry_code = record["industry_code"]
        valuation = valuation_result.scores[industry_code]
        oversold = oversold_result.scores[industry_code]
        cycle_quality = cycle_result.scores[industry_code]
        data_quality = data_result.scores["data_quality"].get(industry_code, 0.0)
        penalty = (
            data_result.scores["penalty"].get(industry_code, 0.0)
            + trap_result.scores["penalty"].get(industry_code, 0.0)
        )
        hard_block = bool(trap_result.scores["hard_block"].get(industry_code, False))
        raw_score = 0.40 * valuation + 0.35 * oversold + 0.15 * cycle_quality + 0.10 * data_quality - penalty
        composite = _clamp(raw_score)
        status = _classify_candidate_status(
            composite=composite,
            valuation=valuation,
            oversold=oversold,
            hard_block=hard_block,
        )

        ranking.append(
            {
                "industry_code": industry_code,
                "industry_name": record.get("industry_name", ""),
                "industry_level": record.get("industry_level", ""),
                "parent_industry": record.get("parent_industry", ""),
                "trade_date": record.get("trade_date", ""),
                "candidate_status": status,
                "industry_value_score": round(composite, 4),
                "industry_valuation_score": round(valuation, 4),
                "industry_oversold_score": round(oversold, 4),
                "cycle_quality_score": round(cycle_quality, 4),
                "data_quality_score": round(data_quality, 4),
                "total_penalty": round(penalty, 4),
                "hard_block": hard_block,
                "pe_ttm": _round_or_blank(record.get("pe_ttm"), 2),
                "pb": _round_or_blank(record.get("pb"), 2),
                "dividend_yield": _round_or_blank(record.get("dividend_yield"), 6),
                "return_20d": _round_or_blank(record.get("return_20d"), 6),
                "return_60d": _round_or_blank(record.get("return_60d"), 6),
                "return_120d": _round_or_blank(record.get("return_120d"), 6),
                "return_252d": _round_or_blank(record.get("return_252d"), 6),
                "drawdown_252d": _round_or_blank(record.get("drawdown_252d"), 6),
                "volatility_60d": _round_or_blank(record.get("volatility_60d"), 6),
                "avg_amount_60d": _round_or_blank(record.get("avg_amount_60d"), 2),
                "history_days": int(_to_float(record.get("history_days")) or 0),
                "history_latest_date": record.get("history_latest_date", ""),
                "history_age_calendar_days": record.get("history_age_calendar_days", ""),
                "history_fresh": _to_bool(record.get("history_fresh")),
                "data_status": record.get("data_status", "research_only"),
                "research_notes": _build_research_notes(record, valuation, oversold, data_quality),
            }
        )

    ranking.sort(key=lambda row: row["industry_value_score"], reverse=True)

    validation_result = _industry_factor_validation_auditor(normalized, ranking)
    report_result = _industry_report_synthesizer(ranking)
    agent_results = [
        data_result,
        valuation_result,
        oversold_result,
        cycle_result,
        trap_result,
        validation_result,
        report_result,
    ]

    return {
        "run_manifest": {
            "system": "行业指数价值研究系统",
            "version": SYSTEM_VERSION,
            "industry_count": len(normalized),
            "ranking_count": len(ranking),
            "research_boundary": "仅研究申万行业指数；不做个股筛选，不生成交易指令，不把当前估值回填到历史验证。",
        },
        "agent_results": [asdict(result) for result in agent_results],
        "candidate_ranking": ranking,
    }


def render_candidate_report(result: Dict[str, Any], top: int | None = None) -> str:
    ranking = result["candidate_ranking"] if top is None else result["candidate_ranking"][:top]
    lines = [
        "# 行业指数价值候选报告",
        "",
        f"系统：{result['run_manifest']['system']} {result['run_manifest']['version']}",
        f"行业数量：{result['run_manifest']['industry_count']}",
        "",
        "## 研究边界",
        "",
        result["run_manifest"]["research_boundary"],
        "",
        "## 行业排名",
        "",
        "| 排名 | 行业 | 上级行业 | 状态 | 综合分 | 估值分 | 超跌分 | 周期质量 | 数据质量 | PE | PB | 股息率 | 60日收益 | 120日收益 | 252日回撤 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for idx, row in enumerate(ranking, start=1):
        lines.append(
            "| {rank} | {industry} | {parent} | {status} | {score:.4f} | {value:.4f} | {oversold:.4f} | {cycle:.4f} | {quality:.4f} | {pe} | {pb} | {div} | {r60} | {r120} | {dd} |".format(
                rank=idx,
                industry=row["industry_name"],
                parent=row.get("parent_industry", ""),
                status=translate_candidate_status(row["candidate_status"]),
                score=float(row["industry_value_score"]),
                value=float(row["industry_valuation_score"]),
                oversold=float(row["industry_oversold_score"]),
                cycle=float(row["cycle_quality_score"]),
                quality=float(row["data_quality_score"]),
                pe=_fmt(row.get("pe_ttm")),
                pb=_fmt(row.get("pb")),
                div=_fmt_pct(row.get("dividend_yield")),
                r60=_fmt_pct(row.get("return_60d")),
                r120=_fmt_pct(row.get("return_120d")),
                dd=_fmt_pct(row.get("drawdown_252d")),
            )
        )
    return "\n".join(lines)


def candidate_rows_for_chinese_output(rows: Sequence[Record]) -> List[Record]:
    output: List[Record] = []
    for row in rows:
        translated: Record = {}
        for source_key, label in CANDIDATE_OUTPUT_COLUMNS_ZH:
            if source_key not in row:
                continue
            value = row[source_key]
            if source_key == "candidate_status":
                value = translate_candidate_status(str(value))
            translated[label] = value
        output.append(translated)
    return output


def translate_candidate_status(status: str) -> str:
    return STATUS_ZH.get(status, status)


def _industry_data_steward(records: Sequence[Record]) -> AgentResult:
    penalty: ScoreMap = {}
    data_quality: ScoreMap = {}
    issues: List[str] = []
    for record in records:
        industry_code = record["industry_code"]
        history_days = int(_to_float(record.get("history_days")) or 0)
        missing_required = [field for field in REQUIRED_FIELDS if _is_missing(record.get(field))]
        row_penalty = 0.0
        quality = 1.0
        if missing_required:
            row_penalty += min(0.20, 0.02 * len(missing_required))
            issues.append(f"{industry_code}: missing fields {','.join(missing_required)}")
        if history_days < 252:
            row_penalty += 0.25
            quality = min(quality, 0.30)
            issues.append(f"{industry_code}: history shorter than 252 trading days")
        elif history_days < 756:
            row_penalty += 0.05
            quality = min(quality, 0.70)
        if not _to_bool(record.get("history_fresh")):
            row_penalty += 0.30
            quality = 0.0
            issues.append(f"{industry_code}: current history is stale")
        penalty[industry_code] = min(row_penalty, 0.50)
        data_quality[industry_code] = _clamp(quality)
    return AgentResult(
        agent="industry_data_steward",
        summary="校验申万行业指数覆盖、历史长度和字段完整性。",
        scores={"penalty": penalty, "data_quality": data_quality},
        issues=issues,
    )


def _industry_fundamental_value_agent(records: Sequence[Record]) -> AgentResult:
    pe_scores = _inverse_rank(records, "pe_ttm")
    pb_scores = _inverse_rank(records, "pb")
    dy_scores = _rank(records, "dividend_yield")
    parent_relative = _parent_relative_value(records)
    scores: ScoreMap = {}
    issues: List[str] = []
    for record in records:
        industry_code = record["industry_code"]
        parts = [
            pe_scores.get(industry_code, 0.5),
            pb_scores.get(industry_code, 0.5),
            dy_scores.get(industry_code, 0.5),
            parent_relative.get(industry_code, 0.5),
        ]
        scores[industry_code] = _clamp(0.30 * parts[0] + 0.30 * parts[1] + 0.20 * parts[2] + 0.20 * parts[3])
        if _is_missing(record.get("pe_ttm")) or _is_missing(record.get("pb")):
            issues.append(f"{industry_code}: valuation fields are incomplete")
    return AgentResult(
        agent="industry_fundamental_value_agent",
        summary="基于当前可见 PE、PB、股息率和父行业内相对位置计算当前横截面估值分。",
        scores=scores,
        issues=issues,
    )


def _industry_oversold_reversion_agent(records: Sequence[Record]) -> AgentResult:
    r20 = _inverse_rank(records, "return_20d")
    r60 = _inverse_rank(records, "return_60d")
    r120 = _inverse_rank(records, "return_120d")
    dd252 = _inverse_rank(records, "drawdown_252d")
    vol60 = _inverse_rank(records, "volatility_60d")
    scores: ScoreMap = {}
    issues: List[str] = []
    for record in records:
        industry_code = record["industry_code"]
        scores[industry_code] = _clamp(
            0.10 * r20.get(industry_code, 0.5)
            + 0.30 * r60.get(industry_code, 0.5)
            + 0.25 * r120.get(industry_code, 0.5)
            + 0.25 * dd252.get(industry_code, 0.5)
            + 0.10 * vol60.get(industry_code, 0.5)
        )
        if _is_missing(record.get("return_60d")) or _is_missing(record.get("drawdown_252d")):
            issues.append(f"{industry_code}: oversold features are incomplete")
    return AgentResult(
        agent="industry_oversold_reversion_agent",
        summary="使用行业指数收益、回撤和波动率计算超跌均值回归候选分。",
        scores=scores,
        issues=issues,
    )


def _industry_cycle_quality_agent(
    records: Sequence[Record],
    valuation_result: AgentResult,
    oversold_result: AgentResult,
) -> AgentResult:
    scores: ScoreMap = {}
    issues: List[str] = []
    for record in records:
        industry_code = record["industry_code"]
        valuation = float(valuation_result.scores.get(industry_code, 0.5))
        oversold = float(oversold_result.scores.get(industry_code, 0.5))
        r20_value = _to_float(record.get("return_20d"))
        stabilization = 0.60 if r20_value is not None and r20_value > 0 else 0.40
        scores[industry_code] = _clamp(0.45 * valuation + 0.40 * oversold + 0.15 * stabilization)
        if oversold >= 0.65 and valuation < 0.45:
            issues.append(f"{industry_code}: oversold without valuation confirmation")
    return AgentResult(
        agent="industry_cycle_quality_agent",
        summary="合并估值便宜、价格超跌和短期企稳信息，避免纯跌幅筛选。",
        scores=scores,
        issues=issues,
    )


def _industry_value_trap_agent(
    records: Sequence[Record],
    data_result: AgentResult,
    valuation_result: AgentResult,
    oversold_result: AgentResult,
) -> AgentResult:
    penalty: ScoreMap = {}
    hard_block: Dict[str, bool] = {}
    issues: List[str] = []
    for record in records:
        industry_code = record["industry_code"]
        row_penalty = 0.0
        blocked = False
        valuation = float(valuation_result.scores.get(industry_code, 0.5))
        oversold = float(oversold_result.scores.get(industry_code, 0.5))
        history_days = int(_to_float(record.get("history_days")) or 0)
        vol60 = _to_float(record.get("volatility_60d"))
        if history_days < 120 or not _to_bool(record.get("history_fresh")):
            blocked = True
            row_penalty += 0.30
            issues.append(f"{industry_code}: hard block because history is short or stale")
        if valuation < 0.35 and oversold > 0.65:
            row_penalty += 0.15
            issues.append(f"{industry_code}: oversold without valuation support")
        if vol60 is not None and vol60 > 0.55:
            row_penalty += 0.08
            issues.append(f"{industry_code}: high recent volatility")
        row_penalty += float(data_result.scores["penalty"].get(industry_code, 0.0))
        penalty[industry_code] = min(row_penalty, 0.50)
        hard_block[industry_code] = blocked
    return AgentResult(
        agent="industry_value_trap_agent",
        summary="应用行业数据缺口、估值陷阱和异常波动惩罚。",
        scores={"penalty": penalty, "hard_block": hard_block},
        issues=issues,
    )


def _industry_factor_validation_auditor(records: Sequence[Record], ranking: Sequence[Dict[str, Any]]) -> AgentResult:
    pit_count = sum(1 for record in records if str(record.get("data_status", "")) == "pit_verified")
    issues = [
        "当前估值快照只能解释当前横截面，不能回填历史验证。",
        "历史验证目前只使用行业指数价格衍生特征和未来收益标签。",
        "当前输出是行业指数研究候选，不是买卖信号。",
    ]
    if pit_count < len(records):
        issues.append("部分行不是 pit_verified；当前结论保持 research_only。")
    return AgentResult(
        agent="industry_factor_validation_auditor",
        summary="保持研究边界：价格衍生信号可回测，当前估值因子只做当前解释。",
        scores={"pit_verified_rows": pit_count, "candidate_rows": len(ranking)},
        issues=issues,
    )


def _industry_report_synthesizer(ranking: Sequence[Dict[str, Any]]) -> AgentResult:
    statuses: Dict[str, int] = {}
    for row in ranking:
        statuses[row["candidate_status"]] = statuses.get(row["candidate_status"], 0) + 1
    return AgentResult(
        agent="industry_report_synthesizer",
        summary="生成行业指数候选排名、状态计数和研究报告输入。",
        scores={"status_counts": statuses},
        issues=[],
    )


def _classify_candidate_status(*, composite: float, valuation: float, oversold: float, hard_block: bool) -> str:
    if hard_block:
        return "data_rejected"
    if composite >= 0.62 and valuation >= 0.55 and oversold >= 0.50:
        return "industry_value_oversold_candidate"
    if valuation >= 0.65 and oversold < 0.45:
        return "cheap_but_not_oversold"
    if oversold >= 0.65 and valuation < 0.45:
        return "oversold_without_valuation_support"
    return "watchlist"


def _build_research_notes(record: Record, valuation: float, oversold: float, data_quality: float) -> str:
    parts = [
        f"value={valuation:.4f}",
        f"oversold={oversold:.4f}",
        f"data_quality={data_quality:.4f}",
        f"PE={_fmt(record.get('pe_ttm'))}",
        f"PB={_fmt(record.get('pb'))}",
        f"60d={_fmt_pct(record.get('return_60d'))}",
        f"dd252={_fmt_pct(record.get('drawdown_252d'))}",
    ]
    return "; ".join(parts)


def _normalize_record(record: Record) -> Record:
    normalized = dict(record)
    normalized["industry_code"] = str(normalized.get("industry_code", "")).strip()
    for field in REQUIRED_FIELDS:
        normalized.setdefault(field, "")
    normalized.setdefault("data_status", "research_only")
    return normalized


def _parent_relative_value(records: Sequence[Record]) -> ScoreMap:
    grouped: Dict[str, List[Record]] = {}
    for record in records:
        grouped.setdefault(str(record.get("parent_industry", "")), []).append(record)
    output: ScoreMap = {}
    for group_records in grouped.values():
        pe = _inverse_rank(group_records, "pe_ttm")
        pb = _inverse_rank(group_records, "pb")
        dy = _rank(group_records, "dividend_yield")
        for record in group_records:
            industry_code = record["industry_code"]
            output[industry_code] = _clamp(
                0.40 * pe.get(industry_code, 0.5)
                + 0.40 * pb.get(industry_code, 0.5)
                + 0.20 * dy.get(industry_code, 0.5)
            )
    return output


def _rank(records: Sequence[Record], field: str) -> ScoreMap:
    values: List[tuple[str, float]] = []
    for record in records:
        value = _to_float(record.get(field))
        if value is not None and math.isfinite(value):
            values.append((record["industry_code"], value))
    if not values:
        return {record["industry_code"]: 0.5 for record in records}
    values.sort(key=lambda item: item[1])
    if len(values) == 1:
        return {values[0][0]: 0.5}
    scores = {code: idx / (len(values) - 1) for idx, (code, _) in enumerate(values)}
    return {record["industry_code"]: scores.get(record["industry_code"], 0.5) for record in records}


def _inverse_rank(records: Sequence[Record], field: str) -> ScoreMap:
    ranked = _rank(records, field)
    return {key: _clamp(1.0 - value) for key, value in ranked.items()}


def _to_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _to_bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _round_or_blank(value: Any, digits: int) -> float | str:
    number = _to_float(value)
    if number is None:
        return ""
    return round(number, digits)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _fmt(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return ""
    return f"{number:.2f}"


def _fmt_pct(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return ""
    return f"{number * 100:.2f}%"
