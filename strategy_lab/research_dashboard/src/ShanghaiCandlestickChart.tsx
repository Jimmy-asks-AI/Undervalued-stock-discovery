import { useEffect, useRef } from "react";
import { CandlestickSeries, ColorType, CrosshairMode, createChart, createSeriesMarkers, type BusinessDay, type Time } from "lightweight-charts";
import type { AnyRecord } from "./types";

type Props = { candles: AnyRecord[]; markers: AnyRecord[] };

function timeKey(time: Time | undefined): string {
  if (!time) return "";
  if (typeof time === "string") return time;
  if (typeof time === "number") return new Date(time * 1000).toISOString().slice(0, 10);
  const day = time as BusinessDay;
  return `${day.year}-${String(day.month).padStart(2, "0")}-${String(day.day).padStart(2, "0")}`;
}

export function ShanghaiCandlestickChart({ candles, markers }: Props) {
  const chartRef = useRef<HTMLDivElement>(null);
  const dateRef = useRef<HTMLSpanElement>(null);
  const pointRef = useRef<HTMLElement>(null);
  const percentileRef = useRef<HTMLSpanElement>(null);
  const pointStatusRef = useRef<HTMLElement>(null);
  const rsiRef = useRef<HTMLSpanElement>(null);
  const momentumRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const container = chartRef.current;
    if (!container || !candles.length) return;
    const byDate = new Map(candles.map((row) => [String(row.time), row]));
    const latest = candles[candles.length - 1];
    const show = (row: AnyRecord) => {
      if (dateRef.current) dateRef.current.textContent = String(row.time ?? "-");
      if (pointRef.current) pointRef.current.textContent = Number(row.close).toFixed(2);
      if (percentileRef.current) percentileRef.current.textContent = `${(Number(row.price_percentile_3y) * 100).toFixed(0)}%`;
      if (pointStatusRef.current) {
        pointStatusRef.current.textContent = String(row.point_status ?? "-");
        pointStatusRef.current.dataset.status = String(row.point_status ?? "");
      }
      if (rsiRef.current) rsiRef.current.textContent = Number(row.rsi_14).toFixed(1);
      if (momentumRef.current) {
        momentumRef.current.textContent = String(row.momentum_status ?? "-");
        momentumRef.current.dataset.status = String(row.momentum_status ?? "");
      }
    };
    show(latest);

    const chart = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight,
      layout: { background: { type: ColorType.Solid, color: "#ffffff" }, textColor: "#5f6964", fontFamily: '"IBM Plex Sans", sans-serif' },
      grid: { vertLines: { color: "#edf0ee" }, horzLines: { color: "#edf0ee" } },
      crosshair: { mode: CrosshairMode.MagnetOHLC },
      rightPriceScale: { borderColor: "#cfd6d1" },
      timeScale: { borderColor: "#cfd6d1", timeVisible: false, minBarSpacing: 0.8, rightOffset: 8 },
      handleScale: true,
      handleScroll: true,
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#c23b32", downColor: "#138a5b", borderUpColor: "#c23b32", borderDownColor: "#138a5b",
      wickUpColor: "#c23b32", wickDownColor: "#138a5b", priceLineVisible: true,
    });
    series.setData(candles.map((row) => ({
      time: String(row.time) as Time, open: Number(row.open), high: Number(row.high), low: Number(row.low), close: Number(row.close),
    })));
    createSeriesMarkers(series, markers.map((row) => ({
      time: String(row.time) as Time,
      position: row.action === "buy" ? "belowBar" as const : "aboveBar" as const,
      color: row.action === "buy" ? "#087f5b" : "#b42318",
      shape: row.action === "buy" ? "arrowUp" as const : "arrowDown" as const,
      text: `${row.action === "buy" ? "买" : "卖"}${Number(row.count) > 1 ? row.count : ""}`,
    })));
    chart.subscribeCrosshairMove((param) => show(byDate.get(timeKey(param.time)) ?? latest));
    // Keep 420 bars visible so recent trade markers remain readable without a range selector.
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, candles.length - 420), to: candles.length + 5 });
    const observer = new ResizeObserver(([entry]) => chart.applyOptions({ width: Math.floor(entry.contentRect.width), height: Math.floor(entry.contentRect.height) }));
    observer.observe(container);
    return () => { observer.disconnect(); chart.remove(); };
  }, [candles, markers]);

  return <div className="kline-panel">
    <div className="kline-heading">
      <div><span>上证综指 · 日线</span><h2>K线与历史 ETF 买卖时点</h2></div>
      <div className="kline-legend"><span className="legend-buy">买</span><span className="legend-sell">卖</span></div>
    </div>
    <div className="kline-canvas" ref={chartRef} aria-label="上证综指日K线及历史ETF买卖标记" />
    <div className="kline-indicator" aria-live="polite">
      <div><span>日期</span><strong ref={dateRef}>-</strong></div>
      <div><span>指数点位</span><strong ref={pointRef}>-</strong></div>
      <div><span>3年分位</span><strong ref={percentileRef}>-</strong></div>
      <div><span>点位状态</span><strong className="state-value" ref={pointStatusRef}>-</strong></div>
      <div><span>RSI14</span><strong ref={rsiRef}>-</strong></div>
      <div><span>动量状态</span><strong className="state-value" ref={momentumRef}>-</strong></div>
    </div>
    <div className="kline-footer"><span>拖动查看历史，滚轮缩放。状态只使用当日及此前数据。</span><a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">图表由 TradingView Lightweight Charts 提供</a></div>
  </div>;
}
