import { useEffect, useMemo, useRef, useState } from 'react'
import { Pause, Play } from 'lucide-react'
import Plotly from 'plotly.js-dist-min'
import { SectionHeading } from '../components/SectionHeading'
import { useTheme } from '../hooks/useTheme'
import { fetchJson } from '../utils/api'
import {
  type Book,
  LiveBook, stripClosedDays, tradingBreaks, tradingTicks, spreadTheme,
  FONT_FAMILY, SPREAD_PLOTLY_CONFIG,
} from '../components/OrderBookViz'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/spb'

// Section order on the page; tickers carry their group from the backend.
const GROUP_ORDER = ['US Market', 'Crypto']

// Poll cadence for the warm backend cache.  The backend keeps books via the
// Finam gRPC stream (tick-by-tick deltas), so the cache changes continuously —
// 1 s polling gives a terminal-style feed without flooding the API.
const POLL_MS = 1000

// Spread-on-volume history — completed 15-min buckets, collected by the backend
// during trading hours.  A new point appears at most every 15 min and the 7-day
// payload is ~1.3 MB, so reloading every couple of minutes is prompt enough.
const SPREAD_REFRESH_MS = 120 * 1000

const SPREAD_1M_COLOR  = '#3b82f6'   // SPB (1 млн ₽/сторона)
const MOEX_COLOR       = '#ef4444'   // MOEX crypto-futures overlay

// The API returns each series COLUMNAR — parallel arrays rather than a list of
// point objects — because that is what Plotly consumes and repeating four field
// names per 15-min bucket was most of the payload.
export interface SpreadColumns {
  buckets:    string[]            // ISO timestamps (15-min buckets)
  spread_usd: (number | null)[]   // absolute spread = P_aver_ask − P_aver_bid ($)
  spread_pct: (number | null)[]   // absolute / top-of-book mid × 100
}
export interface SpreadSeries extends SpreadColumns {
  ticker: string
  name:   string
  group:  string
  moex:   SpreadColumns | null    // present for the 5 crypto tickers
}

// Two units for the same metric.  The depth target (1 млн ₽ per side) is the
// same in both charts — only the spread's unit differs: absolute $ (raw VWAP
// price gap) vs basis points.  The API stores/returns percent; ×100 → б.п.
type SpreadMetric = 'usd' | 'pct'
const toBps = (pct: number | null) => (pct == null ? null : pct * 100)
const SPREAD_METRICS: Record<SpreadMetric, {
  title:  string
  yaxis:  Partial<Plotly.LayoutAxis>
  hover1: string
}> = {
  usd: {
    title: 'Спред, $',
    // exponentformat 'none' kills Plotly's SI prefixes (e.g. "$200µ" for TRX,
    // whose spread is ~0.0003 $) → show plain decimals like "$0.0002".
    yaxis: { tickprefix: '$', hoverformat: ',.5f', exponentformat: 'none' },
    hover1:  '$%{y:.5f}<extra></extra>',
  },
  pct: {
    title: 'Спред, б.п.',
    yaxis: { ticksuffix: ' б.п.', hoverformat: ',.2f' },
    hover1:  '%{y:.2f} б.п.<extra></extra>',
  },
}

const EMPTY: (number | null)[] = []
const valuesOf = (s: SpreadColumns | null | undefined, metric: SpreadMetric) =>
  !s ? EMPTY : metric === 'usd' ? s.spread_usd : s.spread_pct.map(toBps)

// Spread-on-volume history: SPB line (1 млн ₽ depth per side), 15-min buckets,
// plus an optional MOEX crypto-futures line (same methodology) for the 5 crypto
// cards.  Gaps (no trading at night / illiquid) are bridged (`connectgaps`).
export function SpreadChart({ series, moex, metric }: {
  series?: SpreadColumns; moex?: SpreadColumns | null; metric: SpreadMetric
}) {
  const divRef = useRef<HTMLDivElement>(null)
  const theme  = useTheme()
  const cfg    = SPREAD_METRICS[metric]

  const spb  = stripClosedDays(series?.buckets ?? [], valuesOf(series, metric))
  const mx   = stripClosedDays(moex?.buckets ?? [], valuesOf(moex, metric))
  const hasSpb  = spb.y.some(v => v != null)
  const hasMoex = mx.y.some(v => v != null)
  const hasAny  = hasSpb || hasMoex

  useEffect(() => {
    if (!divRef.current || !hasAny) return
    const t = spreadTheme(theme)

    const traces: Plotly.Data[] = [
      {
        type: 'scatter', mode: 'lines+markers', name: hasMoex ? 'SPB' : '1 млн ₽',
        x: spb.x, y: spb.y, connectgaps: true,
        line: { color: SPREAD_1M_COLOR, width: 1.6 }, marker: { size: 3 },
        hovertemplate: `SPB ${cfg.hover1}`,
      },
    ]
    if (hasMoex && moex) {
      traces.push({
        type: 'scatter', mode: 'lines+markers', name: 'MOEX',
        x: mx.x, y: mx.y, connectgaps: true,
        line: { color: MOEX_COLOR, width: 1.6 }, marker: { size: 3 },
        hovertemplate: `MOEX ${cfg.hover1}`,
      })
    }

    const xsAll = [...(series?.buckets ?? []), ...(moex?.buckets ?? [])]
    const xBreaks = tradingBreaks(xsAll)
    const xTicks  = tradingTicks(xsAll)

    const layout: Partial<Plotly.Layout> = {
      paper_bgcolor: t.paper, plot_bgcolor: t.bg,
      margin: { l: 54, r: 10, t: 28, b: 34 },
      showlegend: hasMoex,
      legend: { orientation: 'h', x: 0, y: 1.14, font: { color: t.text, size: 10, family: FONT_FAMILY } },
      xaxis: {
        type: 'date', tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid, showgrid: false,
        rangebreaks: xBreaks,
        ...(xTicks
          ? { tickmode: 'array' as const, tickvals: xTicks.tickvals, ticktext: xTicks.ticktext }
          : { nticks: 5 }),
      },
      yaxis: {
        title: { text: cfg.title, font: { color: t.text, size: 10, family: FONT_FAMILY }, standoff: 10 },
        automargin: true, tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid, rangemode: 'tozero', ...cfg.yaxis,
      },
      hoverlabel: { bgcolor: t.hover, bordercolor: t.hoverBorder, font: { color: t.hoverText, size: 11, family: FONT_FAMILY } },
      hovermode: 'x unified',
    }

    void Plotly.react(divRef.current, traces, layout, SPREAD_PLOTLY_CONFIG)
  }, [series, moex, theme, hasAny, metric])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  if (!hasAny) {
    return (
      <div style={{ height: 340, display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 12, color: 'var(--muted)', textAlign: 'center', padding: 12 }}>
        нет данных о спреде<br />(копится / нет ликвидности на V)
      </div>
    )
  }
  return <div ref={divRef} style={{ width: '100%', height: 340 }} />
}

// Small uppercase column label above each order book / chart pair.
const COL_HEAD: React.CSSProperties = {
  fontSize: 10, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.06em',
  color: 'var(--muted)', padding: '5px 8px', borderBottom: '1px solid var(--border)',
}

export function OrderBookCard({ book, spreadHistory, moexHistory, moexBook }: {
  book: Book; spreadHistory?: SpreadColumns; moexHistory?: SpreadColumns | null; moexBook?: Book
}) {
  // Crypto cards get a 4th column (MOEX book): [SPB book | chart ₽ | chart % | MOEX book].
  const cols = moexBook
    ? 'minmax(190px, 250px) 1fr minmax(190px, 250px)'
    : 'minmax(220px, 290px) 1fr'

  return (
    <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
      <div style={{
        padding: '10px 12px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'baseline',
      }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--fg)' }}>{book.name}</span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: cols, alignItems: 'stretch' }}>
        {/* Live SPB order book */}
        <div style={{ borderRight: '1px solid var(--border)' }}>
          <div style={{ ...COL_HEAD, color: SPREAD_1M_COLOR }}>SPB</div>
          <LiveBook book={book} />
        </div>

        {/* Spread-on-volume history (15-min buckets), ₽ and % */}
        <div style={{ padding: '6px 6px 4px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <SpreadChart series={spreadHistory} moex={moexHistory} metric="usd" />
          <SpreadChart series={spreadHistory} moex={moexHistory} metric="pct" />
        </div>

        {/* Live MOEX order book (crypto cards only) */}
        {moexBook && (
          <div style={{ borderLeft: '1px solid var(--border)' }}>
            <div style={{ ...COL_HEAD, color: MOEX_COLOR }}>MOEX</div>
            <LiveBook book={moexBook} />
          </div>
        )}
      </div>
    </div>
  )
}

export function SPBOrderBook() {
  const [books,      setBooks]      = useState<Book[]>([])
  const [moexBooks,  setMoexBooks]  = useState<Map<string, Book>>(new Map())
  const [spread,     setSpread]     = useState<Map<string, SpreadSeries>>(new Map())
  const [loading,  setLoading]  = useState(true)
  const [live,     setLive]     = useState(true)
  const [lastSync, setLastSync] = useState<Date | null>(null)
  const inFlight = useRef(false)

  const load = async (initial = false) => {
    if (inFlight.current) return            // skip overlap if a poll is slow
    inFlight.current = true
    if (initial) setLoading(true)
    try {
      const [sp, mo] = await Promise.allSettled([
        fetchJson<Book[]>(`${API}/orderbook`),
        fetchJson<Book[]>(`${API}/moex-orderbook`),
      ])
      if (sp.status === 'fulfilled') { setBooks(sp.value); setLastSync(new Date()) }
      else console.error('SPBOrderBook: failed to load order books', sp.reason)
      if (mo.status === 'fulfilled') setMoexBooks(new Map(mo.value.map(b => [b.ticker, b])))
    } finally {
      inFlight.current = false
      if (initial) setLoading(false)
    }
  }

  useEffect(() => { load(true) }, [])

  // Continuous refresh — poll the warm backend cache while `live` is on.
  useEffect(() => {
    if (!live) return
    const id = setInterval(() => load(false), POLL_MS)
    return () => clearInterval(id)
  }, [live])

  // Spread-on-volume history — changes only every 15 min, so reload slowly.
  useEffect(() => {
    const loadSpread = async () => {
      try {
        const data = await fetchJson<SpreadSeries[]>(`${API}/spread-history?days=7`)
        setSpread(new Map(data.map(s => [s.ticker, s])))
      } catch (e) {
        console.error('SPBOrderBook: failed to load spread history', e)
      }
    }
    loadSpread()
    const id = setInterval(loadSpread, SPREAD_REFRESH_MS)
    return () => clearInterval(id)
  }, [])

  const byGroup = useMemo(() => {
    const m = new Map<string, Book[]>()
    for (const b of books) {
      const arr = m.get(b.group)
      if (arr) arr.push(b); else m.set(b.group, [b])
    }
    for (const arr of m.values()) arr.sort((a, b) => a.name.localeCompare(b.name))
    return m
  }, [books])

  return (
    <div>
      <div className="page-toolbar">
        <h1>Order Book</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          СПБ Биржа perpetual futures · live · price in USD · size in contracts
          {lastSync && ` · updated ${lastSync.toLocaleTimeString()}`}
        </div>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--muted)' }}>
          <span style={{
            width: 8, height: 8, borderRadius: '50%',
            background: live ? '#22c55e' : 'var(--muted)',
            boxShadow: live ? '0 0 0 3px rgba(34,197,94,.18)' : 'none',
          }} />
          {live ? 'Live' : 'Paused'}
        </span>
        <button
          className="btn-secondary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={() => setLive(v => !v)}
        >
          {live ? <Pause size={13} /> : <Play size={13} />}
          {live ? 'Pause' : 'Resume'}
        </button>
      </div>

      <div className="card" style={{
        marginBottom: 16, padding: '12px 16px', fontSize: 12.5,
        color: 'var(--text)', lineHeight: 1.6,
      }}>
        Спред считается на объём <b>1 млн ₽ по каждой стороне</b> (1 млн ₽ на bid и
        1 млн ₽ на ask), с проходом по стакану. Два графика на карточку:
        <div style={{ marginTop: 6 }}>
          • <b>Спред, $</b> — абсолютный спред в долларах (разница средних цен
          исполнения P_ср(ask) − P_ср(bid));
          &nbsp;• <b>Спред, б.п.</b> — тот же спред, делённый на середину лучших котировок,
          в базисных пунктах (1 б.п. = 0.01%).
        </div>
        <div style={{ marginTop: 6 }}>
          <span style={{ color: SPREAD_1M_COLOR, fontWeight: 600 }}>■ SPB</span> — СПБ Биржа;{' '}
          <span style={{ color: MOEX_COLOR, fontWeight: 600 }}>■ MOEX</span> — фьючерс на криптоиндекс MOEX
          (только у крипто-инструментов).
        </div>
      </div>

      {loading ? (
        <p className="empty">Loading order books…</p>
      ) : books.length === 0 ? (
        <p className="empty">No data (Finam token not configured?)</p>
      ) : (
        <>
          {GROUP_ORDER.map(group => {
            const groupBooks = byGroup.get(group)
            if (!groupBooks?.length) return null
            return (
              <div key={group}>
                <SectionHeading label={group} />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {groupBooks.map(b => (
                    <OrderBookCard key={b.ticker} book={b}
                      spreadHistory={spread.get(b.ticker)}
                      moexHistory={spread.get(b.ticker)?.moex}
                      moexBook={moexBooks.get(b.ticker)} />
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
