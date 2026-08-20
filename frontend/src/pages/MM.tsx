import { useEffect, useMemo, useRef, useState } from 'react'
import { Pause, Play } from 'lucide-react'
import Plotly from 'plotly.js-dist-min'
import { fetchJson } from '../utils/api'
import { useTheme } from '../hooks/useTheme'
import {
  type Book,
  LiveBook, stripClosedDays, tradingBreaks, tradingTicks, spreadTheme,
  FONT_FAMILY, SPREAD_PLOTLY_CONFIG,
} from '../components/OrderBookViz'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/mm'

const POLL_MS          = 1000         // live order-book cache
// Completed 15-min buckets: a new point can only appear every 15 min, and the
// full 7-day payload is ~1.9 MB for the 65-instrument shares tab — polling it
// every 30 s moved megabytes a minute to show nothing new.
const SPREAD_HIST_MS   = 120 * 1000

const SPB_COLOR = '#3b82f6'          // the (single) MM series colour

// FORTS futures are quoted in the instrument's own currency; the absolute
// spread carries that unit (₽ / $ / points), while bps is unit-free.
type MMBook = Book & { currency?: string }
// Columnar payload (parallel arrays, one per metric) — see the endpoint's
// docstring; the shares tab alone is ~65 instruments × 7 days of 15-min points.
interface MMSeries {
  ticker:     string
  name:       string
  currency:   string
  buckets:    string[]
  spread_abs: (number | null)[]
  spread_pct: (number | null)[]
}

const toBps = (pct: number | null) => (pct == null ? null : pct * 100)

// Absolute-spread axis formatting for a given quote symbol: "$" reads as a
// prefix, everything else (₽, пт, ¥ …) as a trailing unit.
function absAxis(currency: string): { title: string; yaxis: Partial<Plotly.LayoutAxis>; hover: string } {
  if (currency === '$') return {
    title: 'Спред, $',
    yaxis: { tickprefix: '$', hoverformat: ',.5f', exponentformat: 'none' },
    hover: '$%{y:.5f}<extra></extra>',
  }
  return {
    title: `Спред, ${currency}`,
    yaxis: { ticksuffix: ` ${currency}`, hoverformat: ',.4f', exponentformat: 'none' },
    hover: `%{y:.4f} ${currency}<extra></extra>`,
  }
}

type SpreadMetric = 'abs' | 'pct'

// One spread-on-volume chart (1 млн ₽ per side).  `metric` picks the unit:
// absolute in the instrument's quote currency, or basis points.
function SpreadChart({ series, currency, metric }: {
  series?: MMSeries; currency: string; metric: SpreadMetric
}) {
  const divRef = useRef<HTMLDivElement>(null)
  const theme  = useTheme()

  const abs = absAxis(currency)
  const raw = !series ? [] : metric === 'abs' ? series.spread_abs : series.spread_pct.map(toBps)
  const buckets = series?.buckets ?? []
  const { x: xMsk, y: ys } = stripClosedDays(buckets, raw)
  const hasAny = ys.some(v => v != null)

  useEffect(() => {
    if (!divRef.current || !hasAny) return
    const t = spreadTheme(theme)

    const traces: Plotly.Data[] = [{
      type: 'scatter', mode: 'lines+markers', name: '1 млн ₽',
      x: xMsk, y: ys, connectgaps: true,
      line: { color: SPB_COLOR, width: 1.6 }, marker: { size: 3 },
      hovertemplate: metric === 'abs' ? abs.hover : '%{y:.2f} б.п.<extra></extra>',
    }]

    const xBreaks = tradingBreaks(buckets)
    const xTicks  = tradingTicks(buckets)

    const layout: Partial<Plotly.Layout> = {
      paper_bgcolor: t.paper, plot_bgcolor: t.bg,
      margin: { l: 56, r: 10, t: 22, b: 34 },
      showlegend: false,
      xaxis: {
        type: 'date', tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid, showgrid: false,
        rangebreaks: xBreaks,
        ...(xTicks
          ? { tickmode: 'array' as const, tickvals: xTicks.tickvals, ticktext: xTicks.ticktext }
          : { nticks: 5 }),
      },
      yaxis: {
        title: { text: metric === 'abs' ? abs.title : 'Спред, б.п.', font: { color: t.text, size: 10, family: FONT_FAMILY }, standoff: 10 },
        automargin: true, tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid, rangemode: 'tozero',
        ...(metric === 'abs' ? abs.yaxis : { ticksuffix: ' б.п.', hoverformat: ',.2f' }),
      },
      hoverlabel: { bgcolor: t.hover, bordercolor: t.hoverBorder, font: { color: t.hoverText, size: 11, family: FONT_FAMILY } },
      hovermode: 'x unified',
    }

    void Plotly.react(divRef.current, traces, layout, SPREAD_PLOTLY_CONFIG)
  }, [series, currency, theme, hasAny, metric])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  if (!hasAny) {
    return (
      <div style={{ height: 300, display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 12, color: 'var(--muted)', textAlign: 'center', padding: 12 }}>
        нет данных о спреде<br />(копится / нет ликвидности на V)
      </div>
    )
  }
  return <div ref={divRef} style={{ width: '100%', height: 300 }} />
}

const COL_HEAD: React.CSSProperties = {
  fontSize: 10, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.06em',
  color: 'var(--muted)', padding: '5px 8px', borderBottom: '1px solid var(--border)',
}

function OrderBookCard({ book, series }: { book: MMBook; series?: MMSeries }) {
  const currency = book.currency ?? '₽'
  return (
    <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
      <div style={{ padding: '10px 12px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--fg)' }}>{book.name}</span>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{book.ticker} · котировка в {currency}</span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(220px, 290px) 1fr', alignItems: 'stretch' }}>
        <div style={{ borderRight: '1px solid var(--border)' }}>
          <div style={{ ...COL_HEAD, color: SPB_COLOR }}>MOEX FORTS</div>
          <LiveBook book={book} />
        </div>
        <div style={{ padding: '6px 6px 4px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <SpreadChart series={series} currency={currency} metric="abs" />
          <SpreadChart series={series} currency={currency} metric="pct" />
        </div>
      </div>
    </div>
  )
}

export function MM({ group, label }: { group: string; label: string }) {
  const [books,   setBooks]   = useState<MMBook[]>([])
  const [hist,    setHist]    = useState<Map<string, MMSeries>>(new Map())
  const [playing, setPlaying] = useState(true)
  const [loading, setLoading] = useState(true)
  const [lastSync, setLastSync] = useState<Date | null>(null)
  const inFlight = useRef(false)

  // Reset accumulated state when switching tabs (group changes).
  useEffect(() => {
    setBooks([]); setHist(new Map()); setLoading(true)
  }, [group])

  // Live order-book cache.
  const loadBooks = async (initial = false) => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const data = await fetchJson<MMBook[]>(`${API}/orderbook?group=${group}`)
      setBooks(data); setLastSync(new Date())
    } catch (e) {
      console.error('MM: failed to load order books', e)
    } finally {
      inFlight.current = false
      if (initial) setLoading(false)
    }
  }
  useEffect(() => { loadBooks(true) }, [group])
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => loadBooks(false), POLL_MS)
    return () => clearInterval(id)
  }, [playing, group])

  // Completed 15-min buckets (the spread chart is 15-min points — same
  // methodology as the SPB Order Book page; the order book above stays live).
  useEffect(() => {
    const load = async () => {
      try {
        const data = await fetchJson<MMSeries[]>(`${API}/spread-history?group=${group}&days=7`)
        setHist(new Map(data.map(s => [s.ticker, s])))
      } catch (e) {
        console.error('MM: failed to load spread history', e)
      }
    }
    load()
    const id = setInterval(load, SPREAD_HIST_MS)
    return () => clearInterval(id)
  }, [group])

  const sorted = useMemo(
    () => [...books].sort((a, b) => a.name.localeCompare(b.name)),
    [books],
  )

  return (
    <div>
      <div className="page-toolbar">
        <h1>{label}</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          MOEX FORTS · ближайший срок · live · спред на 1 млн ₽/сторону
          {lastSync && ` · updated ${lastSync.toLocaleTimeString()}`}
        </div>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--muted)' }}>
          <span style={{ width: 8, height: 8, borderRadius: '50%',
            background: playing ? '#22c55e' : 'var(--muted)',
            boxShadow: playing ? '0 0 0 3px rgba(34,197,94,.18)' : 'none' }} />
          {playing ? 'Live' : 'Paused'}
        </span>
        <button className="btn-secondary" style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={() => setPlaying(v => !v)}>
          {playing ? <Pause size={13} /> : <Play size={13} />}
          {playing ? 'Pause' : 'Resume'}
        </button>
      </div>

      {loading ? (
        <p className="empty">Loading…</p>
      ) : books.length === 0 ? (
        <p className="empty">Нет ликвидных инструментов в этой группе (или Finam-токен не настроен).</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {sorted.map(b => (
            <OrderBookCard key={b.ticker} book={b} series={hist.get(b.ticker)} />
          ))}
        </div>
      )}
    </div>
  )
}
