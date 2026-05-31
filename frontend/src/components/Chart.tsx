import { useEffect, useRef, useState, useCallback } from 'react'
import {
  createChart,
  IChartApi,
  ISeriesApi,
  LineData,
  HistogramData,
  Time,
} from 'lightweight-charts'
import { useWebSocket } from '../hooks/useWebSocket'
import { useTheme, chartColors } from '../hooks/useTheme'
import { EXCHANGES, EXCHANGE_COLORS, formatSymbol, symbolChannel } from '../types'
import type { PriceTick } from '../types'

interface Props {
  symbol: string
}

function readThemeFromDom() {
  return (document.documentElement.getAttribute('data-theme') ?? 'light') as 'dark' | 'light'
}

/** Round a Unix timestamp (seconds) down to the nearest 1-minute bucket. */
const toMinuteBucket = (secTs: number): Time => (Math.floor(secTs / 60) * 60) as Time

export function Chart({ symbol }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesMapRef = useRef<Map<string, ISeriesApi<'Line'>>>(new Map())
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const rtBucketRef = useRef<Map<number, number>>(new Map())
  const [status, setStatus] = useState<'loading' | 'empty' | 'ok'>('loading')
  const theme = useTheme()

  // ── Create chart once on mount ──────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return
    const c = chartColors(readThemeFromDom())
    const chart = createChart(containerRef.current, {
      layout: { background: { color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: c.border, autoScale: true },
      leftPriceScale: {
        visible: true,
        borderVisible: false,
        scaleMargins: { top: 0.78, bottom: 0 },
        ticksVisible: true,
      },
      crosshair: { mode: 1 },
      width: containerRef.current.clientWidth,
      height: 360,
    })

    const volSeries = chart.addHistogramSeries({
      color: c.volBar,
      priceFormat: { type: 'volume' },
      priceScaleId: 'left',
      lastValueVisible: false,
      priceLineVisible: false,
    })
    volumeSeriesRef.current = volSeries

    EXCHANGES.forEach((ex) => {
      const series = chart.addLineSeries({
        color: EXCHANGE_COLORS[ex],
        lineWidth: 2,
        title: ex,
        priceScaleId: 'right',
        lastValueVisible: true,
        priceLineVisible: false,
      })
      seriesMapRef.current.set(ex, series)
    })

    chartRef.current = chart

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth })
      }
    })
    ro.observe(containerRef.current)

    return () => {
      ro.disconnect()
      seriesMapRef.current.clear()
      volumeSeriesRef.current = null
      chart.remove()
      chartRef.current = null
    }
  }, [])

  // ── Re-apply chart colors when theme changes ───────────────────────────
  useEffect(() => {
    if (!chartRef.current) return
    const c = chartColors(theme)
    chartRef.current.applyOptions({
      layout: { background: { color: c.bg }, textColor: c.text },
      grid:   { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    })
    // Volume bar color is not part of the chart layout — must be re-applied
    // on the series itself, otherwise it stays in whatever color the chart
    // was created with and looks wrong after a theme switch.
    volumeSeriesRef.current?.applyOptions({ color: c.volBar })
  }, [theme])

  // ── Load historical OHLCV for every exchange ────────────────────────────
  useEffect(() => {
    // Re-enable autoScale before clearing so the price axis re-fits to the new
    // symbol's price range instead of staying locked to the previous one.
    chartRef.current?.priceScale('right').applyOptions({ autoScale: true })

    seriesMapRef.current.forEach((s) => s.setData([]))
    volumeSeriesRef.current?.setData([])
    rtBucketRef.current.clear()
    setStatus('loading')

    const apiBase = import.meta.env.VITE_API_URL ?? ''

    Promise.allSettled(
      EXCHANGES.map((exchange) =>
        fetch(
          `${apiBase}/api/prices/ohlcv?symbol=${encodeURIComponent(symbol)}&exchange=${exchange}&interval=1+minute&limit=300`,
        )
          .then((r) => (r.ok ? r.json() : []))
          .then((rows: Array<{ bucket: string; close: number; ticks: number }>) => ({
            exchange,
            rows: Array.isArray(rows) ? rows : [],
          }))
          .catch(() => ({ exchange, rows: [] })),
      ),
    ).then((results) => {
      let hasAny = false
      // Accumulate tick counts per bucket across all exchanges for volume bars
      const volMap = new Map<number, number>()

      results.forEach((res) => {
        if (res.status !== 'fulfilled') return
        const { exchange, rows } = res.value
        if (!rows.length) return
        const series = seriesMapRef.current.get(exchange)
        if (!series) return
        hasAny = true

        const pts: LineData[] = rows
          .map((r) => {
            const t = Math.floor(new Date(r.bucket).getTime() / 1000) as Time
            const bt = t as number
            volMap.set(bt, (volMap.get(bt) ?? 0) + (r.ticks ?? 0))
            return { time: t, value: r.close }
          })
          .sort((a, b) => (a.time as number) - (b.time as number))

        series.setData(pts)
      })

      if (volMap.size > 0) {
        // Use the theme-appropriate volume color so bars match the rest of
        // the chart instead of being locked to the dark-mode color.
        const volColor = chartColors(theme).volBar
        const volData: HistogramData[] = Array.from(volMap.entries())
          .map(([t, v]) => ({ time: t as Time, value: v, color: volColor }))
          .sort((a, b) => (a.time as number) - (b.time as number))
        volumeSeriesRef.current?.setData(volData)
      }

      setStatus(hasAny ? 'ok' : 'empty')
      if (hasAny) chartRef.current?.timeScale().fitContent()
    })
  }, [symbol])

  // ── Real-time updates via WebSocket ─────────────────────────────────────
  const channel = symbolChannel(symbol)

  const onMessage = useCallback(
    (data: unknown) => {
      const tick = data as PriceTick
      if (tick.symbol !== symbol) return   // guard stale frames from previous symbol

      const series = seriesMapRef.current.get(tick.exchange)
      if (!series) return

      // Round to the same 1-min bucket used by historical OHLCV so the
      // live point always updates an existing bar instead of extending right.
      const secTs = Math.floor(new Date(tick.ts).getTime() / 1000)
      const bucket = toMinuteBucket(secTs)

      series.update({ time: bucket, value: tick.last })

      // Increment live tick count for the current bucket → update volume bar
      const bt = bucket as number
      const count = (rtBucketRef.current.get(bt) ?? 0) + 1
      rtBucketRef.current.set(bt, count)
      volumeSeriesRef.current?.update({ time: bucket, value: count, color: chartColors(theme).volBarLive })

      if (status !== 'ok') setStatus('ok')
    },
    [symbol, status],
  )

  useWebSocket(channel, { onMessage })

  return (
    <div className="card chart-card">
      <div className="chart-header">
        <h2 className="card-title">{formatSymbol(symbol)}</h2>
        <div className="chart-legend">
          {EXCHANGES.map((ex) => (
            <span key={ex} className="legend-item">
              <span className="legend-dot" style={{ background: EXCHANGE_COLORS[ex] }} />
              {ex}
            </span>
          ))}
        </div>
      </div>
      {status === 'empty' && (
        <p className="info">No historical data yet — chart will populate as prices arrive</p>
      )}
      <div ref={containerRef} className="chart-container" />
    </div>
  )
}
