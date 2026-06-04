import { useEffect, useMemo, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { VOLUME_EXCHANGES, EXCHANGE_COLORS, SYMBOL_SECTIONS, classifySymbol, formatSymbol } from '../types'
import type { Exchange, SymbolSection } from '../types'
import { SectionHeading } from '../components/SectionHeading'
import { ExchangeSourceBadges } from '../components/ExchangeSourceBadges'

const MOEX_SECTIONS: SymbolSection[] = ['US Market', 'Spot Crypto']
const API = (import.meta.env.VITE_API_URL ?? '') + '/api/history'

interface DailyRow {
  date:       string   // 'YYYY-MM-DD'
  date_label: string   // 'May 31'
  symbol:     string
  exchange:   string
  volume_rub: number   // RUB
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
      title: { text: 'Volume (₽B)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
      automargin: true,
      tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
      gridcolor: t.grid, linecolor: t.grid,
      tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'B', hoverformat: ',.1f',
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

interface ChartProps { symbol: string; rows: DailyRow[] }

function DailyVolumeChart({ symbol, rows }: ChartProps) {
  const divRef = useRef<HTMLDivElement>(null)
  const theme  = useTheme()
  const section = classifySymbol(symbol)

  useEffect(() => {
    if (!divRef.current || !rows.length) return

    const dates  = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => {
      const dt = new Date(d + 'T00:00:00')
      return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    })

    const scale  = 1e9
    const suffix = 'B'

    const visibleExchanges = MOEX_SECTIONS.includes(section)
      ? VOLUME_EXCHANGES.filter(ex => ex !== 'moex')
      : VOLUME_EXCHANGES

    const traces: Plotly.Data[] = visibleExchanges.map((ex: Exchange) => {
      const byDate = new Map<string, number>()
      rows.filter(r => r.exchange === ex).forEach(r => byDate.set(r.date, r.volume_rub))
      const y = dates.map(d => { const v = byDate.get(d); return v != null ? v / scale : null })
      const hasAny = y.some(v => v !== null && v > 0)
      return {
        type: 'bar', name: ex, x: labels, y,
        marker: { color: EXCHANGE_COLORS[ex], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${ex}</b>: ₽%{y:.1f}${suffix}<extra></extra>`,
      } satisfies Plotly.Data
    })

    const layout = buildLayout(formatSymbol(symbol), theme)
    layout.yaxis = {
      ...layout.yaxis,
      title: { text: `Volume (₽${suffix})`, font: { color: themeTokens(theme).text, size: 11, family: FONT_FAMILY }, standoff: 14 },
      ticksuffix: suffix,
    }

    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [symbol, rows, theme, section])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return (
    <div className="card analytics-card">
      <div ref={divRef} style={{ width: '100%', height: 360 }} />
      {!rows.length && (
        <p className="empty" style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          No data
        </p>
      )}
    </div>
  )
}

export function DailyVolume() {
  const [allRows, setAllRows] = useState<DailyRow[]>([])
  const [loading, setLoading] = useState(true)
  const [lastSync, setLastSync] = useState<Date | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const data: DailyRow[] = await fetch(`${API}/daily-volume`).then(r => r.json())
      setAllRows(data)
      setLastSync(new Date())
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const symbols = useMemo(
    () => Array.from(new Set(allRows.map(r => r.symbol))).sort(),
    [allRows],
  )
  const rowsBySymbol = useMemo(() => {
    const map = new Map<string, DailyRow[]>()
    for (const r of allRows) {
      const arr = map.get(r.symbol)
      if (arr) arr.push(r); else map.set(r.symbol, [r])
    }
    return map
  }, [allRows])

  return (
    <div>
      <div className="page-toolbar">
        <h1>Daily Volume</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Daily trading volume per instrument · all exchanges stacked · RUB · last 30 days
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
        exchanges={['binance', 'okx', 'bybit', 'mexc', 'hyperliquid', 'moex']}
      />

      {loading ? (
        <p className="empty">Loading daily volume data…</p>
      ) : symbols.length === 0 ? (
        <p className="empty">No data yet</p>
      ) : (
        <>
          {SYMBOL_SECTIONS.map(({ label }) => {
            const sectionSyms = symbols.filter(s => classifySymbol(s) === (label as SymbolSection))
            if (!sectionSyms.length) return null
            return (
              <div key={label}>
                <SectionHeading label={label} />
                <div className="analytics-grid">
                  {sectionSyms.map(sym => (
                    <DailyVolumeChart
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
