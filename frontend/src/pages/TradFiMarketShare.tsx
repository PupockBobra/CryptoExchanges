import { useEffect, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw, Download } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { VOLUME_EXCHANGES, EXCHANGE_COLORS, INSTRUMENT_COLORS, classifySymbol } from '../types'
import type { Exchange } from '../types'
import { ExchangeSourceBadges } from '../components/ExchangeSourceBadges'
import { exportByExchange, exportByBase, exportByGroup } from '../utils/exportCsv'

type AssetGroup = 'Commodities' | 'US Market' | 'Cryptocurrencies'

const ASSET_GROUP_COLORS: Record<AssetGroup, string> = {
  Commodities:    '#c47a35',
  'US Market':    '#3f51b5',
  Cryptocurrencies: '#00e5ff',
}

function getAssetGroup(symbol: string): AssetGroup {
  const section = classifySymbol(symbol)
  if (section === 'Commodities' || section === 'Precious Metals') return 'Commodities'
  if (section === 'US Market') return 'US Market'
  return 'Cryptocurrencies'
}

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/history'
const FONT_FAMILY = 'Inter, system-ui, sans-serif'

interface TradFiRow {
  date:       string
  date_label: string
  symbol:     string
  exchange:   string
  volume_rub: number
}

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

function baseLayout(title: string, theme: 'dark' | 'light', yTitle: string): Partial<Plotly.Layout> {
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
      title: { text: yTitle, font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
      automargin: true,
      tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
      gridcolor: t.grid, linecolor: t.grid,
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

// ── Chart 1: volume by exchange (absolute) ────────────────────────────────────

function ExportButton({ onClick }: { onClick: () => void }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end', paddingTop: 4 }}>
      <button
        onClick={onClick}
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
  )
}

function ByExchangeAbsolute({ rows, theme }: { rows: TradFiRow[]; theme: 'dark' | 'light'; }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => {
      const dt = new Date(d + 'T00:00:00')
      return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    })
    const scale = 1e9
    const traces: Plotly.Data[] = VOLUME_EXCHANGES.map((ex: Exchange) => {
      const byDate = new Map<string, number>()
      rows.filter(r => r.exchange === ex).forEach(r => {
        byDate.set(r.date, (byDate.get(r.date) ?? 0) + r.volume_rub)
      })
      const y = dates.map(d => { const v = byDate.get(d); return v != null ? v / scale : null })
      const hasAny = y.some(v => v !== null && v > 0)
      return {
        type: 'bar', name: ex, x: labels, y,
        marker: { color: EXCHANGE_COLORS[ex], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${ex}</b>: ₽%{y:.1f}B<extra></extra>`,
      } satisfies Plotly.Data
    })
    const layout = {
      ...baseLayout('Exchange Volume (₽B)', theme, 'Volume (₽B)'),
      yaxis: {
        ...baseLayout('', theme, '').yaxis,
        tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'B',
        title: { text: 'Volume (₽B)', font: { color: themeTokens(theme).text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: themeTokens(theme).text, size: 10, family: FONT_FAMILY },
        gridcolor: themeTokens(theme).grid, linecolor: themeTokens(theme).grid,
      },
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return (
    <>
      <div ref={divRef} style={{ width: '100%', height: 380 }} />
      <ExportButton onClick={() => exportByExchange(rows, 'tradfi-by-exchange.csv')} />
    </>
  )
}

// ── Chart 2: volume by exchange (percent) ─────────────────────────────────────

function ByExchangePercent({ rows, theme }: { rows: TradFiRow[]; theme: 'dark' | 'light' }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => {
      const dt = new Date(d + 'T00:00:00')
      return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    })
    const totals = new Map<string, number>()
    dates.forEach(d => {
      totals.set(d, rows.filter(r => r.date === d).reduce((s, r) => s + r.volume_rub, 0))
    })
    const traces: Plotly.Data[] = VOLUME_EXCHANGES.map((ex: Exchange) => {
      const byDate = new Map<string, number>()
      rows.filter(r => r.exchange === ex).forEach(r => {
        byDate.set(r.date, (byDate.get(r.date) ?? 0) + r.volume_rub)
      })
      const y = dates.map(d => {
        const v = byDate.get(d) ?? 0
        const total = totals.get(d) ?? 0
        return total > 0 ? Math.round((v / total) * 1000) / 10 : null
      })
      const hasAny = y.some(v => v !== null && (v as number) > 0)
      return {
        type: 'bar', name: ex, x: labels, y,
        marker: { color: EXCHANGE_COLORS[ex], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${ex}</b>: %{y:.1f}%<extra></extra>`,
      } satisfies Plotly.Data
    })
    const t = themeTokens(theme)
    const layout: Partial<Plotly.Layout> = {
      ...baseLayout('Exchange Share (%)', theme, 'Share (%)'),
      barmode: 'stack',
      yaxis: {
        title: { text: 'Share (%)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        ticksuffix: '%', range: [0, 100],
      },
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return <div ref={divRef} style={{ width: '100%', height: 380 }} />
}

// ── Chart 3: volume by instrument (absolute) ──────────────────────────────────

function ByInstrumentAbsolute({ rows, theme }: { rows: TradFiRow[]; theme: 'dark' | 'light' }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => {
      const dt = new Date(d + 'T00:00:00')
      return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    })
    const symbols = Array.from(new Set(rows.map(r => r.symbol))).sort()
    const scale = 1e9
    const traces: Plotly.Data[] = symbols.map(sym => {
      const base = sym.split('/')[0]
      const byDate = new Map<string, number>()
      rows.filter(r => r.symbol === sym).forEach(r => {
        byDate.set(r.date, (byDate.get(r.date) ?? 0) + r.volume_rub)
      })
      const y = dates.map(d => { const v = byDate.get(d); return v != null ? v / scale : null })
      const hasAny = y.some(v => v !== null && v > 0)
      return {
        type: 'bar', name: base, x: labels, y,
        marker: { color: INSTRUMENT_COLORS[base] ?? '#888888', opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${base}</b>: ₽%{y:.1f}B<extra></extra>`,
      } satisfies Plotly.Data
    })
    const t = themeTokens(theme)
    const layout: Partial<Plotly.Layout> = {
      ...baseLayout('Instrument Volume (₽B)', theme, 'Volume (₽B)'),
      yaxis: {
        title: { text: 'Volume (₽B)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'B',
      },
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return (
    <>
      <div ref={divRef} style={{ width: '100%', height: 380 }} />
      <ExportButton onClick={() => exportByBase(rows, 'tradfi-by-instrument.csv')} />
    </>
  )
}

// ── Chart 4: volume by instrument (percent) ───────────────────────────────────

function ByInstrumentPercent({ rows, theme }: { rows: TradFiRow[]; theme: 'dark' | 'light' }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => {
      const dt = new Date(d + 'T00:00:00')
      return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    })
    const symbols = Array.from(new Set(rows.map(r => r.symbol))).sort()
    const totals = new Map<string, number>()
    dates.forEach(d => {
      totals.set(d, rows.filter(r => r.date === d).reduce((s, r) => s + r.volume_rub, 0))
    })
    const traces: Plotly.Data[] = symbols.map(sym => {
      const base = sym.split('/')[0]
      const byDate = new Map<string, number>()
      rows.filter(r => r.symbol === sym).forEach(r => {
        byDate.set(r.date, (byDate.get(r.date) ?? 0) + r.volume_rub)
      })
      const y = dates.map(d => {
        const v = byDate.get(d) ?? 0
        const total = totals.get(d) ?? 0
        return total > 0 ? Math.round((v / total) * 1000) / 10 : null
      })
      const hasAny = y.some(v => v !== null && (v as number) > 0)
      return {
        type: 'bar', name: base, x: labels, y,
        marker: { color: INSTRUMENT_COLORS[base] ?? '#888888', opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${base}</b>: %{y:.1f}%<extra></extra>`,
      } satisfies Plotly.Data
    })
    const t = themeTokens(theme)
    const layout: Partial<Plotly.Layout> = {
      ...baseLayout('Instrument Share (%)', theme, 'Share (%)'),
      barmode: 'stack',
      yaxis: {
        title: { text: 'Share (%)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        ticksuffix: '%', range: [0, 100],
      },
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return <div ref={divRef} style={{ width: '100%', height: 380 }} />
}

// ── Chart 5: volume by asset group (absolute) ─────────────────────────────────

function ByAssetGroupAbsolute({ rows, theme }: { rows: TradFiRow[]; theme: 'dark' | 'light' }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => {
      const dt = new Date(d + 'T00:00:00')
      return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    })
    const scale = 1e9
    const groups: AssetGroup[] = ['Cryptocurrencies', 'Commodities', 'US Market']
    const traces: Plotly.Data[] = groups.map(group => {
      const byDate = new Map<string, number>()
      rows.filter(r => getAssetGroup(r.symbol) === group).forEach(r => {
        byDate.set(r.date, (byDate.get(r.date) ?? 0) + r.volume_rub)
      })
      const y = dates.map(d => { const v = byDate.get(d); return v != null ? v / scale : null })
      const hasAny = y.some(v => v !== null && v > 0)
      return {
        type: 'bar', name: group, x: labels, y,
        marker: { color: ASSET_GROUP_COLORS[group], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${group}</b>: ₽%{y:.1f}B<extra></extra>`,
      } satisfies Plotly.Data
    })
    const t = themeTokens(theme)
    const layout: Partial<Plotly.Layout> = {
      ...baseLayout('Asset Group Volume (₽B)', theme, 'Volume (₽B)'),
      yaxis: {
        title: { text: 'Volume (₽B)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'B',
      },
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return (
    <>
      <div ref={divRef} style={{ width: '100%', height: 380 }} />
      <ExportButton onClick={() => exportByGroup(rows, getAssetGroup, 'tradfi-by-asset-group.csv')} />
    </>
  )
}

// ── Chart 6: volume by asset group (percent) ──────────────────────────────────

function ByAssetGroupPercent({ rows, theme }: { rows: TradFiRow[]; theme: 'dark' | 'light' }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => {
      const dt = new Date(d + 'T00:00:00')
      return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    })
    const totals = new Map<string, number>()
    dates.forEach(d => {
      totals.set(d, rows.filter(r => r.date === d).reduce((s, r) => s + r.volume_rub, 0))
    })
    const groups: AssetGroup[] = ['Cryptocurrencies', 'Commodities', 'US Market']
    const traces: Plotly.Data[] = groups.map(group => {
      const byDate = new Map<string, number>()
      rows.filter(r => getAssetGroup(r.symbol) === group).forEach(r => {
        byDate.set(r.date, (byDate.get(r.date) ?? 0) + r.volume_rub)
      })
      const y = dates.map(d => {
        const v = byDate.get(d) ?? 0
        const total = totals.get(d) ?? 0
        return total > 0 ? Math.round((v / total) * 1000) / 10 : null
      })
      const hasAny = y.some(v => v !== null && (v as number) > 0)
      return {
        type: 'bar', name: group, x: labels, y,
        marker: { color: ASSET_GROUP_COLORS[group], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${group}</b>: %{y:.1f}%<extra></extra>`,
      } satisfies Plotly.Data
    })
    const t = themeTokens(theme)
    const layout: Partial<Plotly.Layout> = {
      ...baseLayout('Asset Group Share (%)', theme, 'Share (%)'),
      barmode: 'stack',
      yaxis: {
        title: { text: 'Share (%)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        ticksuffix: '%', range: [0, 100],
      },
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return <div ref={divRef} style={{ width: '100%', height: 380 }} />
}

// ── Page ──────────────────────────────────────────────────────────────────────

export function TradFiMarketShare() {
  const [tradfiRows, setTradfiRows] = useState<TradFiRow[]>([])
  const [allRows, setAllRows]       = useState<TradFiRow[]>([])
  const [loading, setLoading]       = useState(true)
  const [lastSync, setLastSync]     = useState<Date | null>(null)
  const theme = useTheme()

  const load = async () => {
    setLoading(true)
    try {
      const [tradfi, all] = await Promise.all([
        fetch(`${API}/tradfi-volume`).then(r => r.json()) as Promise<TradFiRow[]>,
        fetch(`${API}/daily-volume`).then(r => r.json())  as Promise<TradFiRow[]>,
      ])
      setTradfiRows(tradfi)
      // asset group charts: crypto exchanges only (no moex)
      setAllRows(all.filter(r => r.exchange !== 'moex'))
      setLastSync(new Date())
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  return (
    <div>
      <div className="page-toolbar">
        <h1>TradFi Market Share</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Daily volume of traditional asset perps on crypto exchanges · RUB · last 30 days
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

      <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 0 }}>
        <div style={{ padding: '12px 20px 12px 16px', borderRight: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Страница показывает
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Дневной объём торгов перп-контрактами на традиционные активы, а также объём торгов криптовалютой. Диаграммы 1–4 — только TradFi инструменты; диаграммы 5–6 — все классы активов.
          </div>
        </div>
        <div style={{ padding: '12px 20px 12px 20px', borderRight: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Единица измерения
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Российские рубли (₽B = миллиарды ₽). Криптообъёмы переведены из USDT по курсу вечного фьючерса USDRUBF (MOEX). Курс на выходные и праздники — последнее известное значение.
          </div>
        </div>
        <div style={{ padding: '12px 16px 12px 20px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Группировка активов
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            <span style={{ color: ASSET_GROUP_COLORS['Commodities'], fontWeight: 600 }}>Commodities</span> — энергоносители (Brent, WTI, газ) и металлы (золото, серебро, платина, палладий).{' '}
            <span style={{ color: ASSET_GROUP_COLORS['US Market'], fontWeight: 600 }}>US Market</span> — акции (AAPL, TSLA, NVDA…) и индексы (SPY, QQQ).{' '}
            <span style={{ color: ASSET_GROUP_COLORS['Cryptocurrencies'], fontWeight: 600 }}>Cryptocurrencies</span> — BTC, ETH, SOL и другие цифровые активы.
          </div>
        </div>
      </div>

      {loading ? (
        <p className="empty">Loading…</p>
      ) : tradfiRows.length === 0 ? (
        <p className="empty">No data yet</p>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 8 }}>
          <div className="card analytics-card">
            <ByExchangeAbsolute rows={tradfiRows} theme={theme} />
          </div>
          <div className="card analytics-card">
            <ByExchangePercent rows={tradfiRows} theme={theme} />
          </div>
          <div className="card analytics-card">
            <ByInstrumentAbsolute rows={tradfiRows} theme={theme} />
          </div>
          <div className="card analytics-card">
            <ByInstrumentPercent rows={tradfiRows} theme={theme} />
          </div>
          <div className="card analytics-card">
            <ByAssetGroupAbsolute rows={allRows} theme={theme} />
          </div>
          <div className="card analytics-card">
            <ByAssetGroupPercent rows={allRows} theme={theme} />
          </div>
        </div>
      )}
    </div>
  )
}
