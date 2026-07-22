#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "configs" / "rebound_window_v3_6_microstructure_policy.json"
VERSION = "3.6.0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V3.6 market microstructure data availability audit.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="V3.6 policy JSON.")
    parser.add_argument("--output", default="", help="Override compact output directory.")
    parser.add_argument("--refresh-microstructure", action="store_true", help="Refresh microstructure cache.")
    args = parser.parse_args()

    policy = read_json(Path(args.policy))
    if args.refresh_microstructure:
        policy["refresh_microstructure"] = True
    output_dir = Path(args.output) if args.output else ROOT / policy["output_dir"]
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    debug_dir = output_dir / "debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    source_run = ROOT / policy["source_run_dir"]
    source_summary = read_json(source_run / "run_summary.json") if (source_run / "run_summary.json").exists() else {}
    micro_data, source_audit = load_microstructure_sources(policy)
    fund_flow_panel = build_fund_flow_panel(micro_data.get("stock_market_fund_flow", pd.DataFrame()))
    rule_summary = build_microstructure_rule_summary(policy, source_audit, fund_flow_panel)
    top_candidates = build_top_candidates(rule_summary)
    realtime_summary = build_realtime_summary()
    realtime_trades = pd.DataFrame(columns=["signal_id", "signal_date", "entry_date", "exit_date", "trade_return", "max_adverse_return", "is_win", "is_bad_window", "year"])
    wf_year = pd.DataFrame(columns=["year", "status", "train_rows", "test_rows", "signal_dates", "signal_target_rate", "signal_mean_return"])
    wf_model = pd.DataFrame([empty_model_summary()])
    data_audit = build_data_availability_audit(policy, source_audit, fund_flow_panel)
    target_audit = build_target_audit(policy)
    leakage_audit = build_leakage_audit(policy)
    notes = build_notes(rule_summary, source_audit)
    run_summary = build_run_summary(policy, source_summary, source_audit, top_candidates, data_audit, notes)

    top_candidates.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")
    fund_flow_panel.to_csv(debug_dir / "recent_fund_flow_panel.csv", index=False, encoding="utf-8-sig")
    source_audit.to_csv(debug_dir / "microstructure_source_audit.csv", index=False, encoding="utf-8-sig")
    rule_summary.to_csv(debug_dir / "microstructure_rule_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(columns=["signal_id", "signal_name_zh", "trade_date", "event_return"]).to_csv(debug_dir / "microstructure_rule_events.csv", index=False, encoding="utf-8-sig")
    data_audit.to_csv(debug_dir / "data_availability_audit.csv", index=False, encoding="utf-8-sig")
    target_audit.to_csv(debug_dir / "target_label_audit.csv", index=False, encoding="utf-8-sig")
    wf_year.to_csv(debug_dir / "walk_forward_year_summary.csv", index=False, encoding="utf-8-sig")
    wf_model.to_csv(debug_dir / "walk_forward_model_summary.csv", index=False, encoding="utf-8-sig")
    realtime_trades.to_csv(debug_dir / "realtime_simulation_trades.csv", index=False, encoding="utf-8-sig")
    realtime_summary.to_csv(debug_dir / "realtime_simulation_summary.csv", index=False, encoding="utf-8-sig")
    leakage_audit.to_csv(debug_dir / "leakage_audit.csv", index=False, encoding="utf-8-sig")
    write_json(debug_dir / "optimization_notes.json", notes)
    write_json(debug_dir / "frozen_policy.json", policy)
    write_json(output_dir / "run_summary.json", run_summary)
    (output_dir / "report.md").write_text(render_report(run_summary, top_candidates, source_audit, fund_flow_panel, data_audit, leakage_audit, notes, policy), encoding="utf-8")

    print("V3.6市场微观情绪数据可得性审计完成")
    print(f"微观数据源数={run_summary['microstructure_source_count']}")
    print(f"长历史可回测数据源数={run_summary['backtest_ready_source_count']}")
    print(f"候选数={run_summary['candidate_count']}")
    print(f"最终结论={run_summary['final_verdict']}")
    print(f"输出目录={output_dir.resolve()}")


def load_microstructure_sources(policy: dict[str, Any]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    cache_dir = ROOT / policy["microstructure_cache_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    refresh = bool(policy.get("refresh_microstructure", False))
    data: dict[str, pd.DataFrame] = {}
    rows: list[dict[str, Any]] = []

    definitions = [
        ("stock_market_fund_flow", "市场资金流", fetch_stock_market_fund_flow, "recent_short_history"),
        ("stock_market_activity_legu", "市场活跃度", fetch_stock_market_activity, "historical_if_available"),
    ]
    for source_id, name_zh, loader, source_type in definitions:
        frame, status, source, error = load_or_fetch(cache_dir / f"{source_id}.csv", loader, refresh)
        if not frame.empty:
            data[source_id] = frame
        rows.append(source_audit_row(source_id, name_zh, frame, status, source, error, policy, source_type))

    for date_text in policy["test_dates_for_limit_pool"]:
        for pool_id, name_zh, loader in [
            ("stock_zt_pool_em", "涨停池", fetch_zt_pool),
            ("stock_zt_pool_dtgc_em", "跌停池", fetch_dt_pool),
            ("stock_zt_pool_zbgc_em", "炸板池", fetch_zb_pool),
        ]:
            source_id = f"{pool_id}_{date_text.replace('-', '')}"
            frame, status, source, error = load_or_fetch(cache_dir / f"{source_id}.csv", lambda d=date_text, f=loader: f(d), refresh)
            if not frame.empty:
                data[source_id] = frame
            rows.append(source_audit_row(source_id, f"{name_zh}{date_text}", frame, status, source, error, policy, "single_day_or_recent_limit"))
    return data, pd.DataFrame(rows)


def load_or_fetch(path: Path, loader: Any, refresh: bool) -> tuple[pd.DataFrame, str, str, str]:
    frame = pd.DataFrame()
    status = "fail"
    source = "cache"
    error = ""
    if path.exists() and not refresh:
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig")
            status = "pass"
        except Exception as exc:
            error = str(exc)
    if frame.empty:
        try:
            frame = loader()
            frame.to_csv(path, index=False, encoding="utf-8-sig")
            status = "pass"
            source = "akshare"
        except Exception as exc:
            error = str(exc)
    return frame, status if not frame.empty else "fail", source, error


def fetch_stock_market_fund_flow() -> pd.DataFrame:
    import akshare as ak

    raw = ak.stock_market_fund_flow()
    frame = raw.rename(columns={"日期": "trade_date"}).copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def fetch_stock_market_activity() -> pd.DataFrame:
    import akshare as ak

    raw = ak.stock_market_activity_legu()
    frame = raw.rename(columns={"日期": "trade_date", "date": "trade_date"}).copy()
    if "trade_date" in frame.columns:
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def fetch_zt_pool(date_text: str) -> pd.DataFrame:
    import akshare as ak

    return ak.stock_zt_pool_em(date=date_text.replace("-", ""))


def fetch_dt_pool(date_text: str) -> pd.DataFrame:
    import akshare as ak

    return ak.stock_zt_pool_dtgc_em(date=date_text.replace("-", ""))


def fetch_zb_pool(date_text: str) -> pd.DataFrame:
    import akshare as ak

    return ak.stock_zt_pool_zbgc_em(date=date_text.replace("-", ""))


def source_audit_row(source_id: str, name_zh: str, frame: pd.DataFrame, status: str, source: str, error: str, policy: dict[str, Any], source_type: str) -> dict[str, Any]:
    rows = int(len(frame))
    start_date = ""
    end_date = ""
    if "trade_date" in frame.columns:
        dates = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
        if not dates.empty:
            start_date = dates.min().strftime("%Y-%m-%d")
            end_date = dates.max().strftime("%Y-%m-%d")
    backtest_ready = rows >= int(policy["minimum_history_rows_for_backtest"]) and bool(start_date)
    observation_ready = rows >= int(policy["minimum_history_rows_for_observation"])
    if source_type == "single_day_or_recent_limit":
        usability = "近期或单日观察，不可做长历史回测"
    elif backtest_ready:
        usability = "可进入长历史回测"
    elif observation_ready:
        usability = "短样本观察，不可升级"
    else:
        usability = "不可用或样本不足"
    return {
        "source_id": source_id,
        "name_zh": name_zh,
        "status": status,
        "rows": rows,
        "start_date": start_date,
        "end_date": end_date,
        "source": source,
        "source_type": source_type,
        "backtest_ready": bool(backtest_ready),
        "observation_ready": bool(observation_ready),
        "usability": usability,
        "error": error,
    }


def build_fund_flow_panel(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    output = frame.copy().sort_values("trade_date")
    for col in ["主力净流入-净额", "主力净流入-净占比", "超大单净流入-净额", "小单净流入-净额"]:
        if col in output.columns:
            output[col] = pd.to_numeric(output[col], errors="coerce")
    if "主力净流入-净占比" in output.columns:
        output["main_net_inflow_ratio_5d_avg"] = output["主力净流入-净占比"].rolling(5, min_periods=3).mean()
        output["main_net_inflow_repair_5d"] = output["main_net_inflow_ratio_5d_avg"] - output["main_net_inflow_ratio_5d_avg"].shift(5)
        output["main_net_inflow_positive"] = output["主力净流入-净占比"] > 0
    return output


def build_microstructure_rule_summary(policy: dict[str, Any], source_audit: pd.DataFrame, fund_flow: pd.DataFrame) -> pd.DataFrame:
    rows = []
    backtest_ready = int(source_audit["backtest_ready"].sum()) if not source_audit.empty else 0
    rows.append(
        {
            "signal_id": "microstructure_data_availability",
            "signal_name_zh": "微观情绪数据可得性",
            "signal_type": "数据审计",
            "status": "拒绝" if backtest_ready == 0 else "条件观察",
            "signal_dates": int(len(fund_flow)),
            "nonoverlap_events": 0,
            "active_years": int(pd.to_datetime(fund_flow["trade_date"], errors="coerce").dt.year.nunique()) if not fund_flow.empty and "trade_date" in fund_flow.columns else 0,
            "max_single_year_concentration": 1.0,
            "target_capture_rate": math.nan,
            "mean_return": math.nan,
            "pressure_mean_return": math.nan,
            "mean_edge_vs_pressure": math.nan,
            "bad_window_rate": math.nan,
            "event_mean_return": math.nan,
            "event_win_rate": math.nan,
            "event_bad_window_rate": 1.0,
            "event_worst_return": math.nan,
        }
    )
    if not fund_flow.empty and "main_net_inflow_positive" in fund_flow.columns:
        positive_days = int(fund_flow["main_net_inflow_positive"].sum())
        rows.append(
            {
                "signal_id": "recent_main_fund_flow_repair",
                "signal_name_zh": "近期主力资金流修复",
                "signal_type": "短样本观察",
                "status": "样本不足",
                "signal_dates": positive_days,
                "nonoverlap_events": 0,
                "active_years": int(pd.to_datetime(fund_flow["trade_date"], errors="coerce").dt.year.nunique()),
                "max_single_year_concentration": 1.0,
                "target_capture_rate": math.nan,
                "mean_return": math.nan,
                "pressure_mean_return": math.nan,
                "mean_edge_vs_pressure": math.nan,
                "bad_window_rate": math.nan,
                "event_mean_return": math.nan,
                "event_win_rate": math.nan,
                "event_bad_window_rate": 1.0,
                "event_worst_return": math.nan,
            }
        )
    return pd.DataFrame(rows)


def build_top_candidates(rule_summary: pd.DataFrame) -> pd.DataFrame:
    return rule_summary.copy()


def build_realtime_summary() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "signal_id": "v3_6_realtime_simulation",
                "signal_name_zh": "V3.6实时仿真",
                "signal_type": "数据不足仿真",
                "status": "样本不足",
                "signal_dates": 0,
                "nonoverlap_events": 0,
                "active_years": 0,
                "max_single_year_concentration": 1.0,
                "event_mean_return": math.nan,
                "event_win_rate": math.nan,
                "event_bad_window_rate": 1.0,
                "event_worst_return": math.nan,
            }
        ]
    )


def empty_model_summary() -> dict[str, Any]:
    return {
        "signal_id": "walk_forward_probability_model",
        "signal_name_zh": "Walk-forward概率模型",
        "signal_type": "模型未运行",
        "status": "样本不足",
        "signal_dates": 0,
        "nonoverlap_events": 0,
        "active_years": 0,
        "max_single_year_concentration": 1.0,
        "event_mean_return": math.nan,
        "event_win_rate": math.nan,
        "event_bad_window_rate": 1.0,
        "event_worst_return": math.nan,
    }


def build_data_availability_audit(policy: dict[str, Any], source_audit: pd.DataFrame, fund_flow: pd.DataFrame) -> pd.DataFrame:
    backtest_ready = int(source_audit["backtest_ready"].sum()) if not source_audit.empty else 0
    observation_ready = int(source_audit["observation_ready"].sum()) if not source_audit.empty else 0
    return pd.DataFrame(
        [
            {
                "audit_item": "microstructure_backtest_ready",
                "status": "fail" if backtest_ready == 0 else "pass",
                "evidence": f"backtest_ready_sources={backtest_ready}; observation_ready_sources={observation_ready}",
                "action": "没有长历史微观情绪数据时，不允许做有效性回测。",
            },
            {
                "audit_item": "stock_market_fund_flow_history",
                "status": "pass" if len(fund_flow) >= int(policy["minimum_history_rows_for_observation"]) else "fail",
                "evidence": f"rows={len(fund_flow)}",
                "action": "该数据只能作为近期观察，不足以支撑2015年以来验证。",
            },
        ]
    )


def build_target_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "target_not_recomputed",
                "status": "research_only",
                "evidence": "V3.6 does not run target backtest because microstructure history is insufficient",
                "action": "先完成数据可得性审计，避免短样本伪验证。",
            }
        ]
    )


def build_leakage_audit(policy: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "audit_item": "no_short_history_backfill",
                "status": "pass",
                "evidence": "recent fund flow and limit-pool data are not backfilled into 2015-2026 history",
                "action": "短历史或单日数据不得伪装成长历史特征。",
            },
            {
                "audit_item": "promotion_boundary",
                "status": "research_only",
                "evidence": policy["research_boundary"],
                "action": "不生成交易指令。",
            },
        ]
    )


def build_notes(rule_summary: pd.DataFrame, source_audit: pd.DataFrame) -> dict[str, Any]:
    backtest_ready = int(source_audit["backtest_ready"].sum()) if not source_audit.empty else 0
    notes = [
        f"V3.6发现长历史可回测的微观情绪数据源数量为 {backtest_ready}。",
        "涨跌停池接口主要是近期或单日数据，不能回填到历史。",
        "市场资金流只有短历史样本，只能做近期观察，不能进入统一有效性候选。",
        "下一步应优先寻找可批量历史化的全市场上涨家数、涨跌停数量、创新低数量和成交额分布数据源。",
    ]
    return {
        "main_diagnosis": "V3.6没有足够长历史的市场微观情绪数据，不能做有效反弹窗口验证。",
        "next_iterations": notes,
        "recommended_next_direction": "先解决微观情绪数据历史化，再继续模型迭代；否则继续调参会形成短样本幻觉。",
    }


def build_run_summary(policy: dict[str, Any], source_summary: dict[str, Any], source_audit: pd.DataFrame, top: pd.DataFrame, data_audit: pd.DataFrame, notes: dict[str, Any]) -> dict[str, Any]:
    audit_fail_count = int((data_audit["status"] == "fail").sum()) if not data_audit.empty else 0
    candidates = top[top["status"] == "反弹窗口候选"] if not top.empty else pd.DataFrame()
    source_count = int(len(source_audit)) if not source_audit.empty else 0
    backtest_ready_count = int(source_audit["backtest_ready"].sum()) if "backtest_ready" in source_audit.columns else 0
    return {
        "version": VERSION,
        "policy_id": policy["policy_id"],
        "policy_status": policy["status"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_policy_id": source_summary.get("policy_id", ""),
        "microstructure_source_count": source_count,
        "backtest_ready_source_count": backtest_ready_count,
        "candidate_count": int(len(candidates)),
        "audit_fail_count": audit_fail_count,
        "best_signal_id": top.iloc[0]["signal_id"] if not top.empty else "",
        "best_status": top.iloc[0]["status"] if not top.empty else "",
        "final_verdict": "research_only；市场微观情绪数据历史不足，不能证明有效反弹窗口",
        "main_diagnosis": notes["main_diagnosis"],
        "research_boundary": policy["research_boundary"],
    }


def render_report(summary: dict[str, Any], top: pd.DataFrame, source_audit: pd.DataFrame, fund_flow: pd.DataFrame, data_audit: pd.DataFrame, leakage: pd.DataFrame, notes: dict[str, Any], policy: dict[str, Any]) -> str:
    lines = ["# V3.6 市场微观情绪数据可得性与短样本审计报告", "", f"版本：{VERSION}", "", "## 研究结论", ""]
    lines += [
        "V3.6 不是继续调参，而是审计涨跌停、市场活跃度、资金流等微观情绪数据是否具备历史验证条件。",
        "",
        f"- 微观数据源数量：{summary['microstructure_source_count']}",
        f"- 长历史可回测数据源数量：{summary['backtest_ready_source_count']}",
        f"- 反弹窗口候选数：{summary['candidate_count']}",
        f"- 审计失败数：{summary['audit_fail_count']}",
        f"- 最终结论：{summary['final_verdict']}",
        f"- 主要诊断：{summary['main_diagnosis']}",
        "",
        "## 候选排序",
        "",
    ]
    lines.extend(table_or_empty(top, {"signal_id": "信号ID", "signal_name_zh": "名称", "signal_type": "类型", "status": "状态", "signal_dates": "信号日", "nonoverlap_events": "非重叠事件", "event_bad_window_rate": "坏窗口"}, {"event_bad_window_rate"}))
    lines += ["", "## 微观数据源审计", ""]
    lines.extend(table_or_empty(source_audit, {"source_id": "数据源", "name_zh": "名称", "status": "状态", "rows": "行数", "start_date": "开始", "end_date": "结束", "backtest_ready": "可长测", "usability": "用途", "error": "错误"}, set()))
    lines += ["", "## 数据审计", ""]
    lines.extend(table_or_empty(data_audit, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += ["", "## 近期资金流样本", ""]
    lines.extend(table_or_empty(fund_flow.tail(10), {"trade_date": "日期", "主力净流入-净占比": "主力净占比", "main_net_inflow_ratio_5d_avg": "5日均值", "main_net_inflow_repair_5d": "5日修复", "main_net_inflow_positive": "当日为正"}, {"主力净流入-净占比", "main_net_inflow_ratio_5d_avg", "main_net_inflow_repair_5d"}))
    lines += ["", "## 泄漏与边界审计", ""]
    lines.extend(table_or_empty(leakage, {"audit_item": "项目", "status": "状态", "evidence": "证据", "action": "动作"}, set()))
    lines += ["", "## 结论与下一步", ""]
    for item in notes.get("next_iterations", []):
        lines.append(f"- {item}")
    lines += [
        f"- 建议方向：{notes.get('recommended_next_direction', '')}",
        "",
        "## 输出文件说明",
        "",
        "- `report.md`：中文 V3.6 数据可得性审计报告，优先打开。",
        "- `top_candidates.csv`：微观情绪数据可用性排序；不是交易信号。",
        "- `run_summary.json`：机器可读运行摘要。",
        "- `debug/`：微观数据源审计、近期资金流、空仿真表、统一评价结果和冻结策略。",
        "",
        f"研究边界：{policy['research_boundary']}",
    ]
    return "\n".join(lines)


def table_or_empty(frame: pd.DataFrame, rename: dict[str, str], pct_cols: set[str]) -> list[str]:
    if frame.empty:
        return ["无数据。"]
    display = frame[[col for col in rename if col in frame.columns]].copy()
    for col in display.columns:
        if col in pct_cols:
            display[col] = display[col].map(fmt_pct)
    display = display.rename(columns=rename)
    cols = list(display.columns)
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for _, row in display.iterrows():
        lines.append("| " + " | ".join(str(row[col]) if pd.notna(row[col]) else "" for col in cols) + " |")
    return lines


def fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return "" if math.isnan(number) else f"{number:.2%}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
