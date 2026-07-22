#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import akshare as ak

try:
    import backtrader as bt
except ImportError:
    bt = None


ROOT = Path(__file__).resolve().parents[1]
SIGNALS = ROOT / "outputs" / "industry_rebound_window_v4_71_robustness_live_audit" / "debug" / "base_v4_70_trades.csv"
PRICE = ROOT / "data_catalog" / "cache" / "tradable_carrier_etf_history" / "510300_unadjusted.csv"
NAV_CACHE = ROOT / "data_catalog" / "cache" / "tradable_carrier_etf_history" / "510300_nav.csv"
OUTPUT = ROOT / "outputs" / "audit" / "etf_realistic_execution_replay"
POLICY = {
    "etf_code": "510300",
    "lot_size": 100,
    "model_capital": 100000.0,
    "target_weight": 0.20,
    "commission_rate": 0.00025,
    "minimum_commission": 5.0,
    "slippage_bps_each_side": 5.0,
    "limit_ratio": 0.10,
    "minimum_amount": 1.0,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="ETF 日频真实交易约束回放。")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    signals = pd.read_csv(SIGNALS)
    prices = load_unadjusted_prices(POLICY["etf_code"])
    trades = replay(signals, prices, POLICY)
    nav = load_required_nav(trades, POLICY["etf_code"])
    trades = attach_prior_nav_reference(trades, nav)
    engine_check = backtrader_cross_check(signals, prices, trades, POLICY)
    summary = summarize(trades, engine_check)
    write_outputs(trades, engine_check, summary)
    print(f"trade_count={summary['trade_count']}")
    print(f"filled_count={summary['filled_count']}")
    print(f"mean_net_return={summary['mean_net_return']:.6f}")
    print(f"cross_check_passed={str(summary['cross_check_passed']).lower()}")


def prepare_prices(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    out["prev_close"] = out["close"].shift(1)
    return out


def load_unadjusted_prices(fund_code: str) -> pd.DataFrame:
    if PRICE.exists():
        return prepare_prices(pd.read_csv(PRICE))
    raw = ak.fund_etf_hist_sina(symbol=f"sh{fund_code}")
    required = {"date", "open", "high", "low", "close", "amount"}
    if not required.issubset(raw.columns):
        raise RuntimeError(f"unadjusted price source missing columns: {sorted(required - set(raw.columns))}")
    clean = raw[list(required)].copy()
    clean = clean[["date", "open", "high", "low", "close", "amount"]]
    PRICE.parent.mkdir(parents=True, exist_ok=True)
    clean.to_csv(PRICE, index=False, encoding="utf-8-sig")
    return prepare_prices(clean)


def load_required_nav(trades: pd.DataFrame, fund_code: str) -> pd.DataFrame:
    cached = pd.read_csv(NAV_CACHE) if NAV_CACHE.exists() else pd.DataFrame(columns=["date", "unit_nav"])
    if not cached.empty:
        cached["date"] = pd.to_datetime(cached["date"])
    required = pd.to_datetime(
        pd.concat([trades["actual_entry_date"], trades["actual_exit_date"]], ignore_index=True), errors="coerce"
    ).dropna().drop_duplicates()
    missing_targets = [day for day in required if cached.empty or not (
        (cached["date"] < day) & (cached["date"] >= day - pd.Timedelta(days=20))
    ).any()]
    # ponytail: one short request per uncovered decision date; the cache removes repeat network work.
    rows = []
    for target in missing_targets:
        rows.extend(fetch_nav_window(fund_code, target - pd.Timedelta(days=20), target - pd.Timedelta(days=1)))
    if rows:
        cached = pd.concat([cached, pd.DataFrame(rows)], ignore_index=True)
        cached["date"] = pd.to_datetime(cached["date"])
        cached["unit_nav"] = pd.to_numeric(cached["unit_nav"], errors="coerce")
        cached = cached.dropna().drop_duplicates("date").sort_values("date")
        NAV_CACHE.parent.mkdir(parents=True, exist_ok=True)
        cached.assign(date=cached["date"].dt.date).to_csv(NAV_CACHE, index=False, encoding="utf-8-sig")
    return cached.sort_values("date").reset_index(drop=True)


def fetch_nav_window(fund_code: str, start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, Any]]:
    response = requests.get(
        "https://api.fund.eastmoney.com/f10/lsjz",
        params={"fundCode": fund_code, "pageIndex": 1, "pageSize": 20,
                "startDate": start.date().isoformat(), "endDate": end.date().isoformat()},
        headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://fundf10.eastmoney.com/jjjz_{fund_code}.html"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("ErrCode") not in (None, 0):
        raise RuntimeError(f"NAV source error: {payload.get('ErrMsg')}")
    return [{"date": item["FSRQ"], "unit_nav": item["DWJZ"]}
            for item in (payload.get("Data") or {}).get("LSJZList", []) if item.get("FSRQ") and item.get("DWJZ")]


def attach_prior_nav_reference(trades: pd.DataFrame, nav: pd.DataFrame) -> pd.DataFrame:
    out = trades.copy()
    if nav.empty:
        for column in ("entry_nav_date", "entry_unit_nav", "entry_prior_nav_deviation", "exit_nav_date",
                       "exit_unit_nav", "exit_prior_nav_deviation", "prior_nav_deviation_change"):
            out[column] = None
        return out
    nav = nav.sort_values("date")
    for side in ("entry", "exit"):
        decision = pd.DataFrame({"row_id": out.index, "decision_date": pd.to_datetime(out[f"actual_{side}_date"], errors="coerce")})
        valid = decision.dropna().sort_values("decision_date")
        matched = pd.merge_asof(valid, nav, left_on="decision_date", right_on="date", direction="backward", allow_exact_matches=False)
        out.loc[matched["row_id"], f"{side}_nav_date"] = matched["date"].dt.date.astype(str).values
        out.loc[matched["row_id"], f"{side}_unit_nav"] = matched["unit_nav"].values
        out[f"{side}_prior_nav_deviation"] = pd.to_numeric(out[f"{side}_price"], errors="coerce") / pd.to_numeric(out[f"{side}_unit_nav"], errors="coerce") - 1
    out["prior_nav_deviation_change"] = out["exit_prior_nav_deviation"] - out["entry_prior_nav_deviation"]
    return out


def replay(signals: pd.DataFrame, prices: pd.DataFrame, policy: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for _, signal in signals.iterrows():
        rows.append(replay_one(signal, prices, policy))
    return pd.DataFrame(rows)


def replay_one(signal: pd.Series, prices: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
    planned_entry = pd.Timestamp(signal["entry_date"])
    planned_exit = pd.Timestamp(signal["exit_date"])
    entry_candidates = prices[prices["date"] >= planned_entry]
    if entry_candidates.empty:
        return failed_row(signal, "no_entry_price")
    entry_idx = int(entry_candidates.index[0])
    entry = prices.loc[entry_idx]
    if not buyable(entry, policy):
        return failed_row(signal, "entry_not_tradeable", entry["date"])

    slip = float(policy["slippage_bps_each_side"]) / 10000.0
    entry_price = min(float(entry["open"]) * (1 + slip), float(entry["high"]))
    budget = float(policy["model_capital"]) * float(policy["target_weight"])
    shares = int(budget / entry_price / int(policy["lot_size"])) * int(policy["lot_size"])
    if shares <= 0:
        return failed_row(signal, "insufficient_model_capital", entry["date"])
    entry_notional = shares * entry_price
    buy_commission = max(entry_notional * float(policy["commission_rate"]), float(policy["minimum_commission"]))

    stop_level = float(signal.get("stop_loss_level", 0.06) or 0.06)
    earliest_sell_idx = entry_idx + 1
    exit_reason = "max_holding_date"
    exit_idx = next(
        (int(i) for i in prices.index[earliest_sell_idx:] if prices.at[i, "date"] >= planned_exit),
        None,
    )
    if exit_idx is None:
        return failed_row(signal, "no_exit_price", entry["date"])
    # 计划退出开盘后已经离场，之后的收盘价不得反向改写本次交易。
    for idx in prices.index[earliest_sell_idx:]:
        if prices.at[idx, "date"] >= prices.at[exit_idx, "date"]:
            break
        if float(prices.at[idx, "close"]) <= entry_price * (1 - stop_level):
            exit_idx = idx + 1 if idx + 1 < len(prices) else None
            exit_reason = "stop_confirmed_next_open"
            break
    if exit_idx is None or exit_idx >= len(prices):
        return failed_row(signal, "no_exit_price", entry["date"])

    while exit_idx < len(prices) and not sellable(prices.loc[exit_idx], policy):
        exit_idx += 1
        exit_reason += "_delayed_untradeable"
    if exit_idx >= len(prices):
        return failed_row(signal, "exit_not_tradeable", entry["date"])
    exit_row = prices.loc[exit_idx]
    exit_price = max(float(exit_row["open"]) * (1 - slip), float(exit_row["low"]))
    exit_notional = shares * exit_price
    sell_commission = max(exit_notional * float(policy["commission_rate"]), float(policy["minimum_commission"]))
    pnl = exit_notional - sell_commission - entry_notional - buy_commission
    net_return = pnl / (entry_notional + buy_commission)
    cross_check = reference_return(entry_price, exit_price, shares, buy_commission, sell_commission)
    return {
        "signal_date": signal["signal_date"],
        "planned_entry_date": signal["entry_date"],
        "actual_entry_date": entry["date"].date().isoformat(),
        "planned_exit_date": signal["exit_date"],
        "actual_exit_date": exit_row["date"].date().isoformat(),
        "etf_code": policy["etf_code"],
        "status": "filled",
        "failure_reason": "",
        "exit_reason": exit_reason,
        "shares": shares,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "buy_commission": buy_commission,
        "sell_commission": sell_commission,
        "gross_return": exit_price / entry_price - 1,
        "net_return": net_return,
        "cross_check_return": cross_check,
        "cross_check_abs_diff": abs(net_return - cross_check),
        "calendar_holding_days": (exit_row["date"] - entry["date"]).days,
        "t_plus_one_respected": bool(exit_idx > entry_idx),
    }


def buyable(row: pd.Series, policy: dict[str, Any]) -> bool:
    if float(row["amount"]) < float(policy["minimum_amount"]) or float(row["open"]) <= 0:
        return False
    if pd.notna(row["prev_close"]):
        limit = float(row["prev_close"]) * (1 + float(policy["limit_ratio"]))
        if float(row["open"]) >= limit * 0.999 and float(row["low"]) >= limit * 0.999:
            return False
    return True


def sellable(row: pd.Series, policy: dict[str, Any]) -> bool:
    if float(row["amount"]) < float(policy["minimum_amount"]) or float(row["open"]) <= 0:
        return False
    if pd.notna(row["prev_close"]):
        limit = float(row["prev_close"]) * (1 - float(policy["limit_ratio"]))
        if float(row["open"]) <= limit * 1.001 and float(row["high"]) <= limit * 1.001:
            return False
    return True


def reference_return(entry_price: float, exit_price: float, shares: int, buy_fee: float, sell_fee: float) -> float:
    cash_out = -(entry_price * shares + buy_fee)
    cash_in = exit_price * shares - sell_fee
    return (cash_in + cash_out) / -cash_out


def failed_row(signal: pd.Series, reason: str, entry_date: Any = "") -> dict[str, Any]:
    return {
        "signal_date": signal["signal_date"], "planned_entry_date": signal["entry_date"],
        "actual_entry_date": str(entry_date)[:10], "planned_exit_date": signal["exit_date"],
        "actual_exit_date": "", "etf_code": POLICY["etf_code"], "status": "failed",
        "failure_reason": reason, "exit_reason": "", "shares": 0, "entry_price": None,
        "exit_price": None, "buy_commission": None, "sell_commission": None,
        "gross_return": None, "net_return": None, "cross_check_return": None,
        "cross_check_abs_diff": None, "calendar_holding_days": None, "t_plus_one_respected": False,
    }


def backtrader_cross_check(signals: pd.DataFrame, prices: pd.DataFrame, trades: pd.DataFrame,
                           policy: dict[str, Any]) -> pd.DataFrame:
    if bt is None:
        return pd.DataFrame([{"status": "dependency_missing", "reason": "backtrader_not_installed"}])

    class EtfFeed(bt.feeds.PandasData):
        lines = ("amount", "prevclose")
        params = (("datetime", "date"), ("open", "open"), ("high", "high"), ("low", "low"),
                  ("close", "close"), ("volume", -1), ("openinterest", -1),
                  ("amount", "amount"), ("prevclose", "prev_close"))

    class MinCommission(bt.CommInfoBase):
        params = (("stocklike", True), ("commtype", bt.CommInfoBase.COMM_PERC),
                  ("percabs", True), ("min_fee", 5.0))

        def _getcommission(self, size: float, price: float, pseudoexec: bool) -> float:
            return max(abs(size) * price * self.p.commission, self.p.min_fee)

    class ReplayStrategy(bt.Strategy):
        params = (("planned_entry", None), ("planned_exit", None), ("stop_level", 0.06),
                  ("policy", None))

        def __init__(self) -> None:
            self.order = None
            self.entry_price = None
            self.entry_date = None
            self.exit_date = None
            self.entry_commission = None
            self.exit_commission = None
            self.entry_size = 0
            self.stop_due = False
            self.exit_reason = ""

        def next_open(self) -> None:
            if self.order:
                return
            current = self.data.datetime.date(0)
            if not self.position and self.entry_date is None and current >= self.p.planned_entry:
                if not self._buyable():
                    return
                slip = float(self.p.policy["slippage_bps_each_side"]) / 10000.0
                expected_price = min(float(self.data.open[0]) * (1 + slip), float(self.data.high[0]))
                budget = float(self.p.policy["model_capital"]) * float(self.p.policy["target_weight"])
                lot = int(self.p.policy["lot_size"])
                size = int(budget / expected_price / lot) * lot
                if size > 0:
                    self.order = self.buy(size=size)
            elif self.position and (self.stop_due or current >= self.p.planned_exit):
                if self._sellable():
                    self.exit_reason = "stop_confirmed_next_open" if self.stop_due else "max_holding_date"
                    self.order = self.sell(size=self.position.size)

        def prenext_open(self) -> None:
            self.next_open()

        def next(self) -> None:
            current = self.data.datetime.date(0)
            if self.position and self.entry_price and current < self.p.planned_exit:
                if float(self.data.close[0]) <= self.entry_price * (1 - float(self.p.stop_level)):
                    self.stop_due = True

        def notify_order(self, order: Any) -> None:
            if order.status not in (order.Completed, order.Canceled, order.Margin, order.Rejected):
                return
            if order.status == order.Completed:
                if order.isbuy():
                    self.entry_date = bt.num2date(order.executed.dt).date()
                    self.entry_price = float(order.executed.price)
                    self.entry_commission = float(order.executed.comm)
                    self.entry_size = int(order.executed.size)
                else:
                    self.exit_date = bt.num2date(order.executed.dt).date()
                    self.exit_price = float(order.executed.price)
                    self.exit_commission = float(order.executed.comm)
            self.order = None

        def _buyable(self) -> bool:
            if float(self.data.amount[0]) < float(self.p.policy["minimum_amount"]) or float(self.data.open[0]) <= 0:
                return False
            prev = float(self.data.prevclose[0])
            if prev == prev:
                limit = prev * (1 + float(self.p.policy["limit_ratio"]))
                if float(self.data.open[0]) >= limit * 0.999 and float(self.data.low[0]) >= limit * 0.999:
                    return False
            return True

        def _sellable(self) -> bool:
            if float(self.data.amount[0]) < float(self.p.policy["minimum_amount"]) or float(self.data.open[0]) <= 0:
                return False
            prev = float(self.data.prevclose[0])
            if prev == prev:
                limit = prev * (1 - float(self.p.policy["limit_ratio"]))
                if float(self.data.open[0]) <= limit * 1.001 and float(self.data.high[0]) <= limit * 1.001:
                    return False
            return True

    rows = []
    for (_, signal), (_, expected) in zip(signals.iterrows(), trades.iterrows()):
        entry_pos = prices.index[prices["date"] >= pd.Timestamp(signal["entry_date"])]
        exit_pos = prices.index[prices["date"] >= pd.Timestamp(signal["exit_date"])]
        if entry_pos.empty or exit_pos.empty:
            rows.append({"signal_date": signal["signal_date"], "status": "not_comparable", "reason": "missing_price_window"})
            continue
        start = max(0, int(entry_pos[0]) - 1)
        end = min(len(prices), int(exit_pos[0]) + 10)
        feed_frame = prices.iloc[start:end].copy()
        cerebro = bt.Cerebro(cheat_on_open=True, stdstats=False)
        cerebro.adddata(EtfFeed(dataname=feed_frame))
        cerebro.addstrategy(ReplayStrategy, planned_entry=pd.Timestamp(signal["entry_date"]).date(),
                            planned_exit=pd.Timestamp(signal["exit_date"]).date(),
                            stop_level=float(signal.get("stop_loss_level", 0.06) or 0.06), policy=policy)
        cerebro.broker.setcash(float(policy["model_capital"]))
        cerebro.broker.set_slippage_perc(float(policy["slippage_bps_each_side"]) / 10000.0,
                                        slip_open=True, slip_match=True, slip_out=False)
        cerebro.broker.addcommissioninfo(MinCommission(commission=float(policy["commission_rate"]),
                                                       min_fee=float(policy["minimum_commission"])))
        strategy = cerebro.run()[0]
        if strategy.entry_date is None or strategy.exit_date is None:
            rows.append({"signal_date": signal["signal_date"], "status": "mismatch", "reason": "engine_order_not_completed"})
            continue
        engine_return = reference_return(strategy.entry_price, strategy.exit_price, strategy.entry_size,
                                         strategy.entry_commission, strategy.exit_commission)
        differences = {
            "entry_date": strategy.entry_date.isoformat() != str(expected["actual_entry_date"]),
            "exit_date": strategy.exit_date.isoformat() != str(expected["actual_exit_date"]),
            "shares": strategy.entry_size != int(expected["shares"]),
            "entry_price": abs(strategy.entry_price - float(expected["entry_price"])) > 1e-10,
            "exit_price": abs(strategy.exit_price - float(expected["exit_price"])) > 1e-10,
            "buy_commission": abs(strategy.entry_commission - float(expected["buy_commission"])) > 1e-10,
            "sell_commission": abs(strategy.exit_commission - float(expected["sell_commission"])) > 1e-10,
            "net_return": abs(engine_return - float(expected["net_return"])) > 1e-10,
        }
        failed = [name for name, different in differences.items() if different]
        rows.append({"signal_date": signal["signal_date"], "status": "pass" if not failed else "mismatch",
                     "reason": ",".join(failed), "engine_entry_date": strategy.entry_date.isoformat(),
                     "engine_exit_date": strategy.exit_date.isoformat(), "engine_shares": strategy.entry_size,
                     "engine_net_return": engine_return, "reference_net_return": expected["net_return"],
                     "absolute_return_diff": abs(engine_return - float(expected["net_return"]))})
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame, engine_check: pd.DataFrame) -> dict[str, Any]:
    filled = trades[trades["status"] == "filled"]
    cross_ok = bool(len(filled)) and bool((filled["cross_check_abs_diff"] < 1e-12).all())
    return {
        "version": "etf-realistic-execution-replay-1.0",
        "policy_status": "research_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "etf_code": POLICY["etf_code"],
        "price_source": "Sina fund_etf_hist_sina",
        "price_adjustment": "none",
        "nav_source": "Eastmoney historical unit NAV",
        "trade_count": len(trades),
        "filled_count": len(filled),
        "failed_count": int((trades["status"] == "failed").sum()),
        "mean_gross_return": float(filled["gross_return"].mean()) if len(filled) else None,
        "mean_net_return": float(filled["net_return"].mean()) if len(filled) else None,
        "median_net_return": float(filled["net_return"].median()) if len(filled) else None,
        "win_rate": float((filled["net_return"] > 0).mean()) if len(filled) else None,
        "stop_exit_count": int(filled["exit_reason"].str.startswith("stop").sum()) if len(filled) else 0,
        "t_plus_one_violation_count": int((~filled["t_plus_one_respected"]).sum()) if len(filled) else 0,
        "prior_nav_reference_coverage": float(filled["entry_prior_nav_deviation"].notna().mean()) if len(filled) else 0.0,
        "mean_entry_prior_nav_deviation": float(filled["entry_prior_nav_deviation"].mean()) if filled["entry_prior_nav_deviation"].notna().any() else None,
        "mean_exit_prior_nav_deviation": float(filled["exit_prior_nav_deviation"].mean()) if filled["exit_prior_nav_deviation"].notna().any() else None,
        "mean_prior_nav_deviation_change": float(filled["prior_nav_deviation_change"].mean()) if filled["prior_nav_deviation_change"].notna().any() else None,
        "historical_iopv_available": False,
        "cross_check_passed": cross_ok,
        "external_event_engine_cross_check": "pass" if len(engine_check) == len(trades) and bool((engine_check["status"] == "pass").all()) else "fail",
        "external_event_engine": f"Backtrader {getattr(bt, '__version__', 'unknown')}" if bt else "unavailable",
        "external_event_engine_pass_count": int((engine_check["status"] == "pass").sum()) if "status" in engine_check else 0,
        "production_ready": False,
    }


def write_outputs(trades: pd.DataFrame, engine_check: pd.DataFrame, summary: dict[str, Any]) -> None:
    debug = OUTPUT / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    trades.to_csv(debug / "trade_ledger.csv", index=False, encoding="utf-8-sig")
    engine_check.to_csv(debug / "backtrader_crosscheck.csv", index=False, encoding="utf-8-sig")
    filled = trades[trades["status"] == "filled"].sort_values("net_return", ascending=False)
    filled.head(20).to_csv(OUTPUT / "top_candidates.csv", index=False, encoding="utf-8-sig")
    (OUTPUT / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = f"""# ETF 真实交易约束回放

本回放把 V4.70 的历史窗口落到 510300，使用未复权历史价格、下一计划交易日开盘价、双边滑点、最低佣金、100份整数手、T+1、停牌/一字板失败和次日止损执行。

- 信号记录：{summary['trade_count']}
- 成交记录：{summary['filled_count']}
- 失败记录：{summary['failed_count']}
- 成本后平均收益：{summary['mean_net_return']:.2%}
- 成本后中位收益：{summary['median_net_return']:.2%}
- 胜率：{summary['win_rate']:.2%}
- 止损退出：{summary['stop_exit_count']}
- T+1 违规：{summary['t_plus_one_violation_count']}
- 前一可用净值参考覆盖：{summary['prior_nav_reference_coverage']:.2%}
- 平均入场前净值偏离：{summary['mean_entry_prior_nav_deviation']:.2%}
- 平均退出前净值偏离：{summary['mean_exit_prior_nav_deviation']:.2%}
- 平均前净值偏离变化：{summary['mean_prior_nav_deviation_change']:.2%}
- 双路径算术复核：`{str(summary['cross_check_passed']).lower()}`
- 外部事件引擎复核：`{summary['external_event_engine_cross_check']}`

边界：成交价格使用新浪未复权日线；前净值偏离使用交易日前一可用单位净值，不使用当日收盘后才发布的净值。它包含隔夜标的指数变动，不能解释为真实折溢价。免费历史 IOPV 与真实盘口价差仍缺失；这只是宽基 ETF 实施回放，不证明反弹窗口稳健，也不证明强行业选择 alpha。
"""
    (OUTPUT / "report.md").write_text(report, encoding="utf-8")


def self_check() -> None:
    prices = prepare_prices(pd.DataFrame([
        {"date": "2025-12-31", "open": 1.00, "high": 1.01, "low": 0.99, "close": 1.00, "amount": 1000},
        {"date": "2026-01-02", "open": 1.00, "high": 1.02, "low": 0.99, "close": 1.01, "amount": 1000},
        {"date": "2026-01-05", "open": 1.01, "high": 1.02, "low": 1.00, "close": 1.01, "amount": 1000},
        {"date": "2026-01-06", "open": 1.02, "high": 1.03, "low": 1.01, "close": 1.02, "amount": 1000},
        {"date": "2026-01-07", "open": 0.90, "high": 0.91, "low": 0.89, "close": 0.90, "amount": 1000},
    ]))
    signal = pd.Series({"signal_date": "2026-01-01", "entry_date": "2026-01-02", "exit_date": "2026-01-06", "stop_loss_level": 0.06})
    result = replay_one(signal, prices, POLICY)
    assert result["status"] == "filled" and result["t_plus_one_respected"]
    assert result["shares"] % POLICY["lot_size"] == 0
    assert result["cross_check_abs_diff"] < 1e-12
    assert result["actual_exit_date"] == "2026-01-06" and result["exit_reason"] == "max_holding_date"
    engine = backtrader_cross_check(pd.DataFrame([signal]), prices, pd.DataFrame([result]), POLICY)
    assert len(engine) == 1 and engine.iloc[0]["status"] == "pass"
    nav = pd.DataFrame([{"date": pd.Timestamp("2025-12-31"), "unit_nav": 0.99},
                        {"date": pd.Timestamp("2026-01-05"), "unit_nav": 1.00}])
    enriched = attach_prior_nav_reference(pd.DataFrame([result]), nav).iloc[0]
    assert enriched["entry_nav_date"] == "2025-12-31" and enriched["exit_nav_date"] == "2026-01-05"
    print("self_check=pass")


if __name__ == "__main__":
    main()
