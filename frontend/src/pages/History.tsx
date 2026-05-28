import { useState, useEffect, useRef, useCallback } from 'react'
import { RefreshCw, TrendingUp, TrendingDown, Minus } from 'lucide-react'
import {
  createChart,
  IChartApi,
  ISeriesApi,
  CandlestickData,
  HistogramData,
  Time,
} from 'lightweight-charts'
import { useTheme, chartColors } from '../hooks/useTheme'
import { EXCHANGES, EXCHANGE_COLORS, SYMBOL_SECTIONS, classifySymbol, formatSymbol, fmtVolume } from '../types'
import type { DailyCandle, HistoryMetrics, HistoryMetricsByExchange, Exchange, SymbolSection } from '../types'

function readThemeFromDom() {
  return (document.documentElement.getAttribute('data-theme') ?? 'light') as 'dark' | 'light'
}

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/history'

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtAdtv(v: number | null): string {
  if (v == null || v === 0) return '—'
  return fmtVolume(v)
}

function WoWBadge({ pct }: { pct: number | null }) {
  if (pct == null) return <span className="wow-badge wow-neutral">—</span>
  const abs = Math.abs(pct).toFixed(1)
  if (pct > 0)  return <span className="wow-badge wow-up"><TrendingUp size={11} />+{abs}%</span>
  if (pct < 0)  return <span className="wow-badge wow-down"><TrendingDown size={11} />−{abs}%</span>
  return <span className="wow-badge wow-neutral"><Minus size={11} />0%</span>
}

// ── Per-instrument candlestick chart ─────────────────────────────────────────

interface CandleChartProps {
  symbol:   string
  exchange: string
}

function CandleChart({ symbol, exchange }: CandleChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef     = useRef<IChartApi | null>(null)
  const candleRef    = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volRef       = useRef<ISeriesApi<'Histogram'> | null>(null)
  const [status, setStatus] = useState<'loading' | 'empty' | 'ok'>('loading')
  const theme = useTheme()

  // ── Create chart once ────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return
    const c = chartColors(readThemeFromDom())
    const chart = createChart(containerRef.current, {
      layout:     { background: { color: c.bg }, textColor: c.text },
      grid:       { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      timeScale:  { timeVisible: false, borderColor: c.border },
      rightPriceScale: { borderColor: c.border, autoScale: true },
      leftPriceScale: {
        visible: true,
        borderVisible: false,
        scaleMargins: { top: 0.80, bottom: 0 },
      },
      crosshair: { mode: 1 },
      width:  containerRef.current.clientWidth,
      height: 240,
    })

    const candle = chart.addCandlestickSeries({
      upColor:        '#22c55e',
      downColor:      '#ef4444',
      borderUpColor:  '#22c55e',
      borderDownColor:'#ef4444',
      wickUpColor:    '#22c55e',
      wickDownColor:  '#ef4444',
      priceScaleId:   'right',
    })

    const vol = chart.addHistogramSeries({
      color:           c.volBar,
      priceFormat:     { type: 'volume' },
      priceScaleId:    'left',
      lastValueVisible: false,
      priceLineVisible: false,
    })

    candleRef.current = candle
    volRef.current    = vol
    chartRef.current  = chart

    const ro = new ResizeObserver(() => {
      if (containerRef.current)
        chart.applyOptions({ width: containerRef.current.clientWidth })
    })
    ro.observe(containerRef.current)

    return () => {
      ro.disconnect()
      candleRef.current = null
      volRef.current    = null
      chart.remove()
      chartRef.current  = null
    }
  }, [])

  // ── Re-apply colors when theme changes ───────────────────────────────────
  useEffect(() => {
    if (!chartRef.current) return
    const c = chartColors(theme)
    chartRef.current.applyOptions({
      layout: { background: { color: c.bg }, textColor: c.text },
      grid:   { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    })
  }, [theme])

  // ── Fetch data when symbol or exchange changes ───────────────────────────
  // NOTE: we do NOT clear chart data before the fetch completes — this keeps
  // the previous exchange's candles visible while loading, preventing the
  // chart from appearing to "disappear" during the transition.
  const load = useCallback(async () => {
    if (!candleRef.current || !volRef.current) return
    setStatus('loading')

    try {
      const url = `${API}/ohlcv?symbol=${encodeURIComponent(symbol)}&exchange=${exchange}&limit=365`
      const rows: DailyCandle[] = await fetch(url).then((r) => r.ok ? r.json() : [])

      if (!rows.length) {
        candleRef.current?.setData([])
        volRef.current?.setData([])
        setStatus('empty')
        return
      }

      const candles: CandlestickData[] = []
      const volumes: HistogramData[]   = []

      rows.forEach((r) => {
        const t = r.ts.slice(0, 10) as Time
        candles.push({ time: t, open: r.open, high: r.high, low: r.low, close: r.close })
        volumes.push({
          time:  t,
          value: r.quote_volume,
          color: r.close >= r.open ? '#16a34a30' : '#ef444430',
        })
      })

      chartRef.current?.priceScale('right').applyOptions({ autoScale: true })
      candleRef.current?.setData(candles)
      volRef.current?.setData(volumes)
      chartRef.current?.timeScale().fitContent()
      setStatus('ok')
    } catch {
      candleRef.current?.setData([])
      volRef.current?.setData([])
      setStatus('empty')
    }
  }, [symbol, exchange])

  useEffect(() => { load() }, [load])

  return (
    <div style={{ position: 'relative' }}>
      {status === 'loading' && <div className="chart-overlay">Loading…</div>}
      {status === 'empty'   && <div className="chart-overlay">No data for this exchange</div>}
      <div ref={containerRef} />
    </div>
  )
}

// ── Section heading ───────────────────────────────────────────────────────────

function SectionHeading({ label }: { label: string }) {
  return (
    <div style={{ margin: '28px 0 14px' }}>
      <h2 style={{
        margin: 0,
        fontSize: 11,
        fontWeight: 600,
        textTransform: 'uppercase',
        letterSpacing: '.1em',
        color: 'var(--muted)',
      }}>
        {label}
      </h2>
      <div style={{ height: 1, background: 'var(--border)', marginTop: 8 }} />
    </div>
  )
}

// ── Instrument history card ───────────────────────────────────────────────────

interface CardProps {
  metrics:           HistoryMetrics | undefined
  exchangeMetrics:   HistoryMetricsByExchange[]
  symbol:            string
}

function InstrumentHistoryCard({ metrics, exchangeMetrics, symbol }: CardProps) {
  const [selectedEx, setSelectedEx] = useState<Exchange>('binance')

  // Pick the first exchange that has data as default
  useEffect(() => {
    if (exchangeMetrics.length) {
      const first = exchangeMetrics.find((e) =>
        EXCHANGES.includes(e.exchange as Exchange)
      )
      if (first) setSelectedEx(first.exchange as Exchange)
    }
  }, [exchangeMetrics])

  const exColor = EXCHANGE_COLORS[selectedEx] ?? '#6366f1'

  return (
    <div className="card hist-card">
      {/* ── Card header ── */}
      <div className="hist-card-header">
        <div className="hist-card-title">
          <span className="hist-symbol">{formatSymbol(symbol)}</span>
          <span className={`badge badge--${symbol.includes(':') ? 'perp' : 'spot'}`}>
            {symbol.includes(':') ? 'PERP' : 'SPOT'}
          </span>
        </div>
        {metrics && (
          <div className="hist-metrics-row">
            <div className="hist-metric">
              <span className="hist-metric-label">ADTV YtD</span>
              <span className="hist-metric-value">{fmtAdtv(metrics.adtv_ytd)}</span>
            </div>
            <div className="hist-metric-sep" />
            <div className="hist-metric">
              <span className="hist-metric-label">ADTV this week</span>
              <span className="hist-metric-value">{fmtAdtv(metrics.adtv_week)}</span>
            </div>
            <div className="hist-metric-sep" />
            <div className="hist-metric">
              <span className="hist-metric-label">WoW</span>
              <WoWBadge pct={metrics.wow_pct} />
            </div>
            <div className="hist-metric-sep" />
            <div className="hist-metric">
              <span className="hist-metric-label">Days</span>
              <span className="hist-metric-value">{metrics.ytd_days}</span>
            </div>
          </div>
        )}
      </div>

      {/* ── Exchange selector ── */}
      <div className="hist-ex-tabs">
        {EXCHANGES.map((ex) => {
          const exm = exchangeMetrics.find((e) => e.exchange === ex)
          const hasData = !!exm?.ytd_days
          return (
            <button
              key={ex}
              className={`hist-ex-tab ${selectedEx === ex ? 'hist-ex-tab--active' : ''} ${!hasData ? 'hist-ex-tab--nodata' : ''}`}
              style={selectedEx === ex ? { borderColor: EXCHANGE_COLORS[ex], color: EXCHANGE_COLORS[ex] } : {}}
              onClick={() => hasData && setSelectedEx(ex)}
              title={hasData ? `${ex}: ${exm?.ytd_days ?? 0} days` : `${ex}: no data`}
            >
              {ex}
              {hasData && (
                <span className="hist-ex-tab-days">{exm!.ytd_days}d</span>
              )}
            </button>
          )
        })}
      </div>

      {/* ── Per-exchange metrics bar ── */}
      {(() => {
        const exm = exchangeMetrics.find((e) => e.exchange === selectedEx)
        if (!exm) return null
        return (
          <div className="hist-ex-metrics" style={{ borderColor: exColor + '40' }}>
            <span style={{ color: exColor, fontWeight: 600, fontSize: 12 }}>{selectedEx}</span>
            <span className="hist-ex-metric">
              ADTV YtD <strong>{fmtAdtv(exm.adtv_ytd)}</strong>
            </span>
            <span className="hist-ex-metric">
              this week <strong>{fmtAdtv(exm.adtv_week)}</strong>
            </span>
            <span className="hist-ex-metric">
              WoW <WoWBadge pct={exm.wow_pct} />
            </span>
          </div>
        )
      })()}

      {/* ── Candlestick chart ── */}
      <CandleChart symbol={symbol} exchange={selectedEx} />
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────

export function History() {
  const [metrics,    setMetrics]    = useState<HistoryMetrics[]>([])
  const [exMetrics,  setExMetrics]  = useState<HistoryMetricsByExchange[]>([])
  const [loading,    setLoading]    = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [lastSync,   setLastSync]   = useState<Date | null>(null)

  const loadMetrics = async () => {
    try {
      const [m, ex] = await Promise.all([
        fetch(`${API}/metrics`).then((r) => r.json()),
        fetch(`${API}/metrics/exchanges`).then((r) => r.json()),
      ])
      setMetrics(m)
      setExMetrics(ex)
      setLastSync(new Date())
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadMetrics() }, [])

  const triggerRefresh = async () => {
    setRefreshing(true)
    await fetch(`${API}/refresh`, { method: 'POST' })
    // Give the backfill a moment then reload metrics
    setTimeout(async () => {
      await loadMetrics()
      setRefreshing(false)
    }, 3000)
  }

  // Summary aggregated across all instruments
  const totalAdtvYtd  = metrics.reduce((s, m) => s + (m.adtv_ytd  ?? 0), 0)
  const totalAdtvWeek = metrics.reduce((s, m) => s + (m.adtv_week ?? 0), 0)

  // All symbols that have data
  const symbols = metrics.map((m) => m.symbol)

  return (
    <div>
      {/* ── Toolbar ── */}
      <div className="page-toolbar">
        <h1>Historical Data</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Daily candles since 2026-01-01 · refreshed every 6 h
          {lastSync && ` · last sync ${lastSync.toLocaleTimeString()}`}
        </div>
        <button
          className="btn-secondary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={triggerRefresh}
          disabled={refreshing}
        >
          <RefreshCw size={13} className={refreshing ? 'spin' : ''} />
          {refreshing ? 'Fetching…' : 'Refresh now'}
        </button>
      </div>

      {/* ── Summary bar ── */}
      {!loading && metrics.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap', alignItems: 'center' }}>
            <div>
              <div className="hist-metric-label">Total market ADTV YtD</div>
              <div className="hist-metric-value" style={{ fontSize: 20 }}>{fmtAdtv(totalAdtvYtd)}</div>
            </div>
            <div>
              <div className="hist-metric-label">Total market ADTV this week</div>
              <div className="hist-metric-value" style={{ fontSize: 20 }}>{fmtAdtv(totalAdtvWeek)}</div>
            </div>
            <div>
              <div className="hist-metric-label">Instruments tracked</div>
              <div className="hist-metric-value" style={{ fontSize: 20 }}>{metrics.length}</div>
            </div>
            <div style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)', lineHeight: 1.6 }}>
              ADTV = Average Daily Trading Volume (USDT-equivalent)<br />
              WoW = week-over-week change vs prior Mon–Sun week<br />
              Volumes summed across all exchanges before averaging
            </div>
          </div>
        </div>
      )}

      {/* ── Instrument cards grouped by section ── */}
      {loading ? (
        <p className="empty">Loading historical data…</p>
      ) : symbols.length === 0 ? (
        <p className="empty">No historical data yet — backfill running in the background</p>
      ) : (
        <>
          {SYMBOL_SECTIONS.map(({ label }) => {
            const sectionSyms = symbols.filter(
              (s) => classifySymbol(s) === (label as SymbolSection),
            )
            if (!sectionSyms.length) return null
            return (
              <div key={label}>
                <SectionHeading label={label} />
                <div className="hist-grid">
                  {sectionSyms.map((sym) => (
                    <InstrumentHistoryCard
                      key={sym}
                      symbol={sym}
                      metrics={metrics.find((m) => m.symbol === sym)}
                      exchangeMetrics={exMetrics.filter((e) => e.symbol === sym)}
                    />
                  ))}
                </div>
              </div>
            )
          })}
        </>
      )}
    </div>
  )
}
