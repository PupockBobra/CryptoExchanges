import { useEffect, useMemo, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw, Download } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { VOLUME_EXCHANGES, EXCHANGE_COLORS, SYMBOL_SECTIONS, classifySymbol, formatSymbol } from '../types'
import type { Exchange, SymbolSection } from '../types'
import { SectionHeading } from '../components/SectionHeading'
import { ExchangeSourceBadges } from '../components/ExchangeSourceBadges'
import { pickUnit, maxStackedTotal } from '../utils/scale'
import { sortSymbolsByValue } from '../utils/rank'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/open-interest'
const HISTORY_API = (import.meta.env.VITE_API_URL ?? '') + '/api/history'

interface DailyOIRow {
  date:         string   // 'YYYY-MM-DD'
  date_label:   string   // 'May 31'
  symbol:       string
  exchange:     string
  oi_contracts: number | null
  oi_usdt:      number | null
  oi_rub:       number | null
}

const FONT_FAMILY = 'Inter, system-ui, sans-serif'

function themeTokens(theme: 'dark' | 'light') {
  if (theme === 'light') return {
    bg: '#ffffff', paper: '#f8fafc', grid: '#e2e8f0',
    text: '#64748b', title: '#1e293b',
    hover: '#ffffff', hoverBorder: '#e2e8f0', hoverText: '#1e293b',
  }
  return {
    bg: '#0f1117', paper: '#1a1d27', grid: '#1f2937',
    text: '#9ca3af', title: '#e2e8f0',
    hover: '#21263a', hoverBorder: '#2d3148', hoverText: '#e2e8f0',
  }
}

function buildLayout(title: string, theme: 'dark' | 'light'): Partial<Plotly.Layout> {
  const t = themeTokens(theme)
  return {
    title: { text: title, font: { color: t.title, size: 14, family: FONT_FAMILY }, x: 0.02, xanchor: 'left' },
    barmode: 'stack',
    paper_bgcolor: t.paper,
    plot_bgcolor:  t.bg,
    margin: { l: 70, r: 16, t: 44, b: 120 },
    legend: {
      orientation: 'h', x: 0, y: -0.35,
      font: { color: t.text, size: 11, family: FONT_FAMILY },
      bgcolor: 'transparent',
    },
    xaxis: {
      tickangle: -40,
      tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
      gridcolor: t.grid, linecolor: t.grid, showgrid: false,
    },
    yaxis: {
      title: { text: 'OI (₽M)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
      automargin: true,
      tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
      gridcolor: t.grid, linecolor: t.grid,
      tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'M', hoverformat: ',.1f',
    },
    hoverlabel: {
      bgcolor: t.hover, bordercolor: t.hoverBorder,
      font: { color: t.hoverText, size: 12, family: FONT_FAMILY },
    },
    hovermode: 'x unified',
  }
}

const PLOTLY_CONFIG: Partial<Plotly.Config> = {
  displayModeBar: true,
  modeBarButtonsToRemove: [
    'select2d', 'lasso2d', 'autoScale2d', 'hoverClosestCartesian',
    'hoverCompareCartesian', 'toggleSpikelines',
  ] as Plotly.ModeBarDefaultButtons[],
  displaylogo: false,
  responsive: true,
}

function exportOICsv(rows: DailyOIRow[], symbol: string, filename: string) {
  const exchanges = Array.from(new Set(rows.map(r => r.exchange))).sort()
  const dates     = Array.from(new Set(rows.map(r => r.date))).sort()
  const lookup    = new Map<string, Map<string, number>>()
  for (const r of rows) {
    if (!lookup.has(r.date)) lookup.set(r.date, new Map())
    if (r.oi_rub != null) lookup.get(r.date)!.set(r.exchange, r.oi_rub)
  }
  const header = ['Date', ...exchanges].join(',')
  const dataRows = dates.map(d => {
    const byEx = lookup.get(d) ?? new Map()
    return [d, ...exchanges.map(ex => (byEx.get(ex) ?? '').toString())].join(',')
  })
  const csv = `# Open Interest (RUB) — ${symbol}\n${[header, ...dataRows].join('\n')}`
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const url  = URL.createObjectURL(blob)
  const a    = document.createElement('a')
  a.href = url; a.download = filename; a.click()
  URL.revokeObjectURL(url)
}

// ── Chart component ────────────────────────────────────────────────────────────

interface ChartProps { symbol: string; rows: DailyOIRow[] }

function OIChart({ symbol, rows }: ChartProps) {
  const divRef = useRef<HTMLDivElement>(null)
  const theme  = useTheme()

  useEffect(() => {
    if (!divRef.current || !rows.length) return

    const dates  = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => {
      const dt = new Date(d + 'T00:00:00')
      return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    })

    // Unit follows this instrument's own scale — see utils/scale.ts
    const { scale, suffix } = pickUnit(
      maxStackedTotal(rows.map(r => ({ key: r.date, value: r.oi_rub }))),
    )

    // MOEX FORTS open interest (ISS OPENPOSITION) stacks alongside the crypto
    // venues, but only for the instruments that actually have a FORTS contract.
    const hasMoex = rows.some(r => r.exchange === 'moex')
    const visibleExchanges = hasMoex
      ? VOLUME_EXCHANGES
      : VOLUME_EXCHANGES.filter(ex => ex !== 'moex')

    const traces: Plotly.Data[] = visibleExchanges.map((ex: Exchange) => {
      const byDate = new Map<string, number>()
      rows.filter(r => r.exchange === ex && r.oi_rub != null)
          .forEach(r => byDate.set(r.date, r.oi_rub!))
      const y      = dates.map(d => { const v = byDate.get(d); return v != null ? v / scale : null })
      const hasAny = y.some(v => v !== null && v > 0)
      return {
        type: 'bar', name: ex, x: labels, y,
        marker: { color: EXCHANGE_COLORS[ex], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${ex}</b>: ₽%{y:,.1f}${suffix}<extra></extra>`,
      } satisfies Plotly.Data
    })

    const layout = buildLayout(formatSymbol(symbol), theme)
    layout.yaxis = {
      ...layout.yaxis,
      title: { text: `OI (₽${suffix})`, font: { color: themeTokens(theme).text, size: 11, family: FONT_FAMILY }, standoff: 14 },
      ticksuffix: suffix,
    }

    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [symbol, rows, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  const slug = symbol.replace(/\//g, '-').replace(/:/, '-')

  return (
    <div className="card analytics-card">
      <div ref={divRef} style={{ width: '100%', height: 360 }} />
      {!rows.length && (
        <p className="empty" style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          No data
        </p>
      )}
      {rows.length > 0 && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', paddingTop: 4 }}>
          <button
            onClick={() => exportOICsv(rows, symbol, `open-interest-${slug}.csv`)}
            style={{
              background: 'transparent', border: 'none', cursor: 'pointer',
              color: 'var(--muted)', padding: '3px 6px', borderRadius: 4,
              display: 'flex', alignItems: 'center', gap: 4, fontSize: 11,
            }}
            onMouseEnter={e => (e.currentTarget.style.color = 'var(--fg)')}
            onMouseLeave={e => (e.currentTarget.style.color = 'var(--muted)')}
          >
            <Download size={12} />
            export to csv
          </button>
        </div>
      )}
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export function OpenInterest() {
  const [allRows,  setAllRows]  = useState<DailyOIRow[]>([])
  const [usStocks, setUsStocks] = useState<Set<string>>(new Set())
  const [loading,  setLoading]  = useState(true)
  const [lastSync, setLastSync] = useState<Date | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const r = await fetch(`${API}/daily`)
      if (!r.ok) throw new Error(`OI request failed: ${r.status}`)
      const data = await r.json()
      setAllRows(Array.isArray(data) ? data : [])
      // US Market = top-N equity perps by weekly turnover, named by the backend.
      try {
        const s = await fetch(`${HISTORY_API}/us-stock-tickers`)
        if (s.ok) setUsStocks(new Set(((await s.json()).tickers ?? []) as string[]))
      } catch (e) {
        console.error('Failed to load US stock tickers:', e)
      }
      setLastSync(new Date())
    } catch (e) {
      console.error('Failed to load open interest:', e)
      setAllRows([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  // Only instruments with real OI — hide charts that would be empty (e.g. the
  // untraded CORN/TTF/URANIUM/WHEAT commodity perps whose OI is always zero).
  // Ranked by the most recent OI, biggest first — not alphabetically.  OI is a
  // stock, not a flow, so summing the 30 days would just favour the instruments
  // with the longest history.
  const symbols = useMemo(() => {
    const withOI = new Set<string>()
    for (const r of allRows) if (r.oi_rub != null && r.oi_rub > 0) withOI.add(r.symbol)
    const lastDate = allRows.reduce((m, r) => (r.date > m ? r.date : m), '')
    const ranked = sortSymbolsByValue(
      allRows.filter(r => r.date === lastDate),
      r => r.symbol,
      r => r.oi_rub,
    )
    return ranked.filter(s => withOI.has(s))
  }, [allRows])
  const rowsBySymbol = useMemo(() => {
    const map = new Map<string, DailyOIRow[]>()
    for (const r of allRows) {
      const arr = map.get(r.symbol)
      if (arr) arr.push(r); else map.set(r.symbol, [r])
    }
    return map
  }, [allRows])

  return (
    <div>
      <div className="page-toolbar">
        <h1>Open Interest</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Daily open interest per instrument · all exchanges stacked · RUB · last 30 days
          {lastSync && ` · loaded ${lastSync.toLocaleTimeString()}`}
        </div>
        <button
          className="btn-secondary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={load}
          disabled={loading}
        >
          <RefreshCw size={13} className={loading ? 'spin' : ''} />
          Refresh
        </button>
      </div>

      <ExchangeSourceBadges
        exchanges={['binance', 'okx', 'bybit', 'mexc', 'hyperliquid', 'bitget']}
      />

      <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 0 }}>
        <div style={{ padding: '12px 20px 12px 16px', borderRight: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Страница показывает
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Дневной открытый интерес за последние 30 дней по каждому инструменту в разбивке по биржам, в рублях.
          </div>
        </div>
        <div style={{ padding: '12px 16px 12px 20px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Hyperliquid и MEXC
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Binance, OKX и Bybit предоставляют историю открытого интереса, поэтому доступны за весь период. Hyperliquid и MEXC публикуют только текущее значение OI без истории — поэтому их ряды начинаются с момента запуска сборщика, хотя контракты появились раньше.
          </div>
        </div>
      </div>

      {loading ? (
        <p className="empty">Loading open interest data…</p>
      ) : symbols.length === 0 ? (
        <p className="empty">No data yet — OI collector runs every 30 minutes after startup</p>
      ) : (
        <>
          {SYMBOL_SECTIONS.map(({ label }) => {
            const sectionSyms = symbols.filter(s => classifySymbol(s, usStocks) === (label as SymbolSection))
            if (!sectionSyms.length) return null
            return (
              <div key={label}>
                <SectionHeading label={label} />
                <div className="analytics-grid">
                  {sectionSyms.map(sym => (
                    <OIChart
                      key={sym}
                      symbol={sym}
                      rows={rowsBySymbol.get(sym) ?? []}
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
