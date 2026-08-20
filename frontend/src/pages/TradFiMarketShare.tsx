import { useEffect, useMemo, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw, Download } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { EXCHANGES, EXCHANGE_COLORS, INSTRUMENT_COLORS, classifySymbol } from '../types'
import type { Exchange } from '../types'
import { ExchangeSourceBadges } from '../components/ExchangeSourceBadges'
import { exportByExchange, exportByBase, exportByGroup } from '../utils/exportCsv'
import { fetchJson } from '../utils/api'

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
const STOCK_API = (import.meta.env.VITE_API_URL ?? '') + '/api/stocks'
const FONT_FAMILY = 'Inter, system-ui, sans-serif'

// Equity-perp turnover (all company stocks on crypto exchanges), served by the
// stock ETL. One flat row per bucket × series (exchange or ticker).
interface StockRow {
  bucket:       string   // 'YYYY-MM-DD' (day, or Monday of the ISO week)
  bucket_label: string
  series:       string   // exchange id, or ticker
  volume_rub:   number
}
interface StockVolume {
  period:        'daily' | 'weekly'
  by_exchange:   StockRow[]
  by_instrument: StockRow[]
}

// 20-colour palette for the per-instrument stock chart (top-20 + «Прочее»).
const STOCK_PALETTE = [
  '#4e79a7', '#f28e2b', '#59a14f', '#e15759', '#76b7b2', '#edc948', '#b07aa1',
  '#ff9da7', '#9c755f', '#bab0ac', '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728',
  '#9467bd', '#8c564b', '#e377c2', '#17becf', '#bcbd22', '#7f7f7f',
]
const STOCK_OTHER_COLOR = '#aab0bb'

// X-axis label for a bucket start date. Daily → "May 18"; weekly → the full
// Mon–Sun range "May 18 – May 24" (weekly rows carry the Monday week_start).
function bucketLabel(date: string, weekly: boolean): string {
  const start = new Date(date + 'T00:00:00')
  const fmt = (d: Date) => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  if (!weekly) return fmt(start)
  const end = new Date(start)
  end.setDate(end.getDate() + 6)
  return `${fmt(start)} – ${fmt(end)}`
}

interface TradFiRow {
  date:       string
  date_label: string
  symbol:     string
  exchange:   string
  volume_rub: number
}

// Company stocks are enumerated far more completely by the stock ETL (~520 perps)
// than by the curated tradfi allow-list (a handful of US names).  To show ALL
// TradFi instruments in charts 1–4 without double-counting, keep only commodities,
// precious metals and the two index perps (QQQ/SPY — absent from the stock ETL,
// which excludes indices/ETFs) from the curated tradfi rows, and take every
// company stock from the stock ETL instead.
const TRADFI_INDEX_BASES = new Set(['QQQ', 'SPY'])

function nonStockTradfiRows(rows: TradFiRow[]): TradFiRow[] {
  return rows.filter(r => {
    const section = classifySymbol(r.symbol)
    if (section === 'Commodities' || section === 'Precious Metals') return true
    return TRADFI_INDEX_BASES.has(r.symbol.split('/')[0])
  })
}

// Charts 1–2 group by exchange (symbol irrelevant); charts 3–4 group by
// instrument (exchange irrelevant).  The stock ETL is pre-aggregated on each
// axis separately, so we build one merged TradFiRow[] per axis.
function mergedByExchange(tradfiRows: TradFiRow[], stock: StockVolume | null): TradFiRow[] {
  const stockRows: TradFiRow[] = (stock?.by_exchange ?? []).map(r => ({
    date: r.bucket, date_label: r.bucket_label, symbol: '', exchange: r.series, volume_rub: r.volume_rub,
  }))
  return [...nonStockTradfiRows(tradfiRows), ...stockRows]
}

function mergedByInstrument(tradfiRows: TradFiRow[], stock: StockVolume | null): TradFiRow[] {
  const stockRows: TradFiRow[] = (stock?.by_instrument ?? []).map(r => ({
    date: r.bucket, date_label: r.bucket_label, symbol: `${r.series}/USDT:USDT`, exchange: '', volume_rub: r.volume_rub,
  }))
  return [...nonStockTradfiRows(tradfiRows), ...stockRows]
}

// Charts 5–6 (asset-group) apply the same "all TradFi" methodology: from the
// all-asset rows keep crypto + commodities/metals + the index perps (QQQ/SPY),
// drop the curated company stocks (they'd double-count), and fold the FULL
// equity-perp universe from the stock ETL into the US Market group as one carrier
// series (charts sum by group, so per-ticker identity is irrelevant here).
const US_MARKET_CARRIER = 'AAPL/USDT:USDT'   // any base classifySymbol maps to 'US Market'

function assetGroupRows(allRows: TradFiRow[], stock: StockVolume | null): TradFiRow[] {
  const kept = allRows.filter(r => {
    if (getAssetGroup(r.symbol) === 'US Market')
      return TRADFI_INDEX_BASES.has(r.symbol.split('/')[0])   // keep QQQ/SPY only
    return true                                               // crypto + commodities/metals
  })
  const byDate = new Map<string, { label: string; v: number }>()
  ;(stock?.by_exchange ?? []).forEach(r => {
    const cur = byDate.get(r.bucket)
    byDate.set(r.bucket, { label: r.bucket_label, v: (cur?.v ?? 0) + r.volume_rub })
  })
  const stockRows: TradFiRow[] = [...byDate.entries()].map(([date, { label, v }]) => ({
    date, date_label: label, symbol: US_MARKET_CARRIER, exchange: '', volume_rub: v,
  }))
  return [...kept, ...stockRows]
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

// Horizontal labels showing each bar's stacked total (₽B), centred above the
// column. Rendered as layout annotations; the y-axis range is padded so the
// tallest bar's label isn't clipped.
function totalsAnnotations(labels: string[], totalsB: number[], theme: 'dark' | 'light'): Partial<Plotly.Annotations>[] {
  const color = themeTokens(theme).title
  const out: Partial<Plotly.Annotations>[] = []
  labels.forEach((lab, i) => {
    const v = totalsB[i]
    if (v > 0) out.push({
      x: lab, y: v, xref: 'x', yref: 'y',
      text: v.toFixed(0),
      showarrow: false,
      xanchor: 'center', yanchor: 'bottom',
      yshift: 3,
      font: { color, size: 9, family: FONT_FAMILY },
    })
  })
  return out
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

function ByExchangeAbsolute({ rows, theme, weekly }: { rows: TradFiRow[]; theme: 'dark' | 'light'; weekly: boolean }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => bucketLabel(d, weekly))
    const scale = 1e9
    const traces: Plotly.Data[] = EXCHANGES.map((ex: Exchange) => {
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
    const totalsB = dates.map(d => rows.filter(r => r.date === d).reduce((s, r) => s + r.volume_rub, 0) / scale)
    const layout = {
      ...baseLayout('Exchange Volume (₽B)', theme, 'Volume (₽B)'),
      yaxis: {
        ...baseLayout('', theme, '').yaxis,
        tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'B',
        title: { text: 'Volume (₽B)', font: { color: themeTokens(theme).text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: themeTokens(theme).text, size: 10, family: FONT_FAMILY },
        gridcolor: themeTokens(theme).grid, linecolor: themeTokens(theme).grid,
        range: [0, Math.max(0, ...totalsB) * 1.25],
      },
      annotations: totalsAnnotations(labels, totalsB, theme),
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme, weekly])

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

function ByExchangePercent({ rows, theme, weekly }: { rows: TradFiRow[]; theme: 'dark' | 'light'; weekly: boolean }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => bucketLabel(d, weekly))
    const totals = new Map<string, number>()
    dates.forEach(d => {
      totals.set(d, rows.filter(r => r.date === d).reduce((s, r) => s + r.volume_rub, 0))
    })
    const traces: Plotly.Data[] = EXCHANGES.map((ex: Exchange) => {
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
  }, [rows, theme, weekly])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return <div ref={divRef} style={{ width: '100%', height: 380 }} />
}

// ── Chart 3: volume by instrument (absolute) ──────────────────────────────────

function ByInstrumentAbsolute({ rows, theme, weekly }: { rows: TradFiRow[]; theme: 'dark' | 'light'; weekly: boolean }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => bucketLabel(d, weekly))
    const scale = 1e9

    // top-20 bases by total turnover; the rest collapse into «Прочее»
    const totalByBase = new Map<string, number>()
    rows.forEach(r => {
      const base = r.symbol.split('/')[0]
      totalByBase.set(base, (totalByBase.get(base) ?? 0) + r.volume_rub)
    })
    const top = [...totalByBase.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20).map(e => e[0])
    const topset = new Set(top)

    const traces: Plotly.Data[] = top.map((base, i) => {
      const byDate = new Map<string, number>()
      rows.filter(r => r.symbol.split('/')[0] === base).forEach(r => {
        byDate.set(r.date, (byDate.get(r.date) ?? 0) + r.volume_rub)
      })
      const y = dates.map(d => { const v = byDate.get(d); return v != null ? v / scale : null })
      const hasAny = y.some(v => v !== null && v > 0)
      return {
        type: 'bar', name: base, x: labels, y,
        marker: { color: INSTRUMENT_COLORS[base] ?? STOCK_PALETTE[i % STOCK_PALETTE.length], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${base}</b>: ₽%{y:.2f}B<extra></extra>`,
      } satisfies Plotly.Data
    })
    const otherY = dates.map(d =>
      rows.filter(r => r.date === d && !topset.has(r.symbol.split('/')[0])).reduce((s, r) => s + r.volume_rub, 0) / scale)
    if (otherY.some(v => v > 0)) traces.push({
      type: 'bar', name: 'Прочее', x: labels, y: otherY.map(v => (v > 0 ? v : null)),
      marker: { color: STOCK_OTHER_COLOR, opacity: 0.85 },
      hovertemplate: `<b>Прочее</b>: ₽%{y:.2f}B<extra></extra>`,
    } satisfies Plotly.Data)

    const totalsB = dates.map(d => rows.filter(r => r.date === d).reduce((s, r) => s + r.volume_rub, 0) / scale)
    const t = themeTokens(theme)
    const layout: Partial<Plotly.Layout> = {
      ...baseLayout('Instrument Volume (₽B)', theme, 'Volume (₽B)'),
      yaxis: {
        title: { text: 'Volume (₽B)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'B',
        range: [0, Math.max(0, ...totalsB) * 1.25],
      },
      annotations: totalsAnnotations(labels, totalsB, theme),
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme, weekly])

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

function ByInstrumentPercent({ rows, theme, weekly }: { rows: TradFiRow[]; theme: 'dark' | 'light'; weekly: boolean }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => bucketLabel(d, weekly))
    const totals = new Map<string, number>()
    dates.forEach(d => {
      totals.set(d, rows.filter(r => r.date === d).reduce((s, r) => s + r.volume_rub, 0))
    })

    // top-20 bases by total turnover; the rest collapse into «Прочее»
    const totalByBase = new Map<string, number>()
    rows.forEach(r => {
      const base = r.symbol.split('/')[0]
      totalByBase.set(base, (totalByBase.get(base) ?? 0) + r.volume_rub)
    })
    const top = [...totalByBase.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20).map(e => e[0])
    const topset = new Set(top)

    const traces: Plotly.Data[] = top.map((base, i) => {
      const byDate = new Map<string, number>()
      rows.filter(r => r.symbol.split('/')[0] === base).forEach(r => {
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
        marker: { color: INSTRUMENT_COLORS[base] ?? STOCK_PALETTE[i % STOCK_PALETTE.length], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${base}</b>: %{y:.1f}%<extra></extra>`,
      } satisfies Plotly.Data
    })
    const otherY = dates.map(d => {
      const v = rows.filter(r => r.date === d && !topset.has(r.symbol.split('/')[0])).reduce((s, r) => s + r.volume_rub, 0)
      const total = totals.get(d) ?? 0
      return total > 0 ? Math.round((v / total) * 1000) / 10 : null
    })
    if (otherY.some(v => v !== null && (v as number) > 0)) traces.push({
      type: 'bar', name: 'Прочее', x: labels, y: otherY,
      marker: { color: STOCK_OTHER_COLOR, opacity: 0.85 },
      hovertemplate: `<b>Прочее</b>: %{y:.1f}%<extra></extra>`,
    } satisfies Plotly.Data)

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
  }, [rows, theme, weekly])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return <div ref={divRef} style={{ width: '100%', height: 380 }} />
}

// ── Chart 5: volume by asset group (absolute) ─────────────────────────────────

function ByAssetGroupAbsolute({ rows, theme, weekly }: { rows: TradFiRow[]; theme: 'dark' | 'light'; weekly: boolean }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => bucketLabel(d, weekly))
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
    const totalsB = dates.map(d => rows.filter(r => r.date === d).reduce((s, r) => s + r.volume_rub, 0) / scale)
    const t = themeTokens(theme)
    const layout: Partial<Plotly.Layout> = {
      ...baseLayout('Asset Group Volume (₽B)', theme, 'Volume (₽B)'),
      yaxis: {
        title: { text: 'Volume (₽B)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'B',
        range: [0, Math.max(0, ...totalsB) * 1.25],
      },
      annotations: totalsAnnotations(labels, totalsB, theme),
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme, weekly])

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

function ByAssetGroupPercent({ rows, theme, weekly }: { rows: TradFiRow[]; theme: 'dark' | 'light'; weekly: boolean }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dates.map(d => bucketLabel(d, weekly))
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
  }, [rows, theme, weekly])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return <div ref={divRef} style={{ width: '100%', height: 380 }} />
}

// ── Stocks: all company-stock perps across crypto exchanges ───────────────────

function stockAbsYAxis(theme: 'dark' | 'light', maxB: number): Partial<Plotly.LayoutAxis> {
  const t = themeTokens(theme)
  return {
    title: { text: 'Volume (₽B)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
    automargin: true,
    tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
    gridcolor: t.grid, linecolor: t.grid,
    tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'B',
    range: maxB > 0 ? [0, maxB * 1.25] : undefined,
  }
}

function StockByExchange({ rows, theme, weekly }: { rows: StockRow[]; theme: 'dark' | 'light'; weekly: boolean }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const buckets = Array.from(new Set(rows.map(r => r.bucket))).sort()
    const labels  = buckets.map(b => bucketLabel(b, weekly))
    const scale = 1e9
    const traces: Plotly.Data[] = EXCHANGES.map((ex: Exchange) => {
      const byB = new Map<string, number>()
      rows.filter(r => r.series === ex).forEach(r => byB.set(r.bucket, r.volume_rub))
      const y = buckets.map(b => { const v = byB.get(b); return v != null ? v / scale : null })
      const hasAny = y.some(v => v !== null && v > 0)
      return {
        type: 'bar', name: ex, x: labels, y,
        marker: { color: EXCHANGE_COLORS[ex], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${ex}</b>: ₽%{y:.1f}B<extra></extra>`,
      } satisfies Plotly.Data
    })
    const totalsB = buckets.map(b => rows.filter(r => r.bucket === b).reduce((s, r) => s + r.volume_rub, 0) / scale)
    const layout: Partial<Plotly.Layout> = {
      ...baseLayout('Акции — объём по биржам (₽B)', theme, 'Volume (₽B)'),
      yaxis: stockAbsYAxis(theme, Math.max(0, ...totalsB)),
      annotations: totalsAnnotations(labels, totalsB, theme),
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme, weekly])

  useEffect(() => { const el = divRef.current; return () => { if (el) Plotly.purge(el) } }, [])

  return <div ref={divRef} style={{ width: '100%', height: 420 }} />
}

function StockByInstrument({ rows, theme, weekly }: { rows: StockRow[]; theme: 'dark' | 'light'; weekly: boolean }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const buckets = Array.from(new Set(rows.map(r => r.bucket))).sort()
    const labels  = buckets.map(b => bucketLabel(b, weekly))
    const scale = 1e9

    // top-20 tickers by total turnover; the rest collapse into «Прочее»
    const totals = new Map<string, number>()
    rows.forEach(r => totals.set(r.series, (totals.get(r.series) ?? 0) + r.volume_rub))
    const top = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20).map(e => e[0])
    const topset = new Set(top)

    const traces: Plotly.Data[] = top.map((tk, i) => {
      const byB = new Map<string, number>()
      rows.filter(r => r.series === tk).forEach(r => byB.set(r.bucket, (byB.get(r.bucket) ?? 0) + r.volume_rub))
      const y = buckets.map(b => { const v = byB.get(b); return v != null ? v / scale : null })
      const hasAny = y.some(v => v !== null && v > 0)
      return {
        type: 'bar', name: tk, x: labels, y,
        marker: { color: STOCK_PALETTE[i % STOCK_PALETTE.length], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${tk}</b>: ₽%{y:.2f}B<extra></extra>`,
      } satisfies Plotly.Data
    })
    const otherY = buckets.map(b =>
      rows.filter(r => r.bucket === b && !topset.has(r.series)).reduce((s, r) => s + r.volume_rub, 0) / scale)
    traces.push({
      type: 'bar', name: 'Прочее', x: labels, y: otherY.map(v => (v > 0 ? v : null)),
      marker: { color: STOCK_OTHER_COLOR, opacity: 0.85 },
      hovertemplate: `<b>Прочее</b>: ₽%{y:.2f}B<extra></extra>`,
    } satisfies Plotly.Data)

    const totalsB = buckets.map(b => rows.filter(r => r.bucket === b).reduce((s, r) => s + r.volume_rub, 0) / scale)
    const layout: Partial<Plotly.Layout> = {
      ...baseLayout('Акции — объём по инструментам (топ-20 + прочее, ₽B)', theme, 'Volume (₽B)'),
      yaxis: stockAbsYAxis(theme, Math.max(0, ...totalsB)),
      annotations: totalsAnnotations(labels, totalsB, theme),
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme, weekly])

  useEffect(() => { const el = divRef.current; return () => { if (el) Plotly.purge(el) } }, [])

  return <div ref={divRef} style={{ width: '100%', height: 420 }} />
}

// ── Page ──────────────────────────────────────────────────────────────────────

type View = 'daily' | 'weekly'

export function TradFiMarketShare() {
  const [view, setView]             = useState<View>('daily')
  const [tradfiRows, setTradfiRows] = useState<TradFiRow[]>([])
  const [allRows, setAllRows]       = useState<TradFiRow[]>([])
  const [stockVol, setStockVol]     = useState<StockVolume | null>(null)
  const [loading, setLoading]       = useState(true)
  const [lastSync, setLastSync]     = useState<Date | null>(null)
  const theme = useTheme()

  const load = async (v: View) => {
    setLoading(true)
    try {
      const [tradfiPath, allPath] = v === 'weekly'
        ? ['tradfi-weekly-volume', 'weekly-volume']
        : ['tradfi-volume', 'daily-volume']
      const [tradfi, all, stocks] = await Promise.all([
        fetchJson<TradFiRow[]>(`${API}/${tradfiPath}`),
        fetchJson<TradFiRow[]>(`${API}/${allPath}`),
        fetchJson<StockVolume>(`${STOCK_API}/volume?period=${v}`).catch(() => null),
      ])
      // crypto exchanges only (no moex) across all charts on this page
      setTradfiRows(tradfi.filter(r => r.exchange !== 'moex'))
      setAllRows(all.filter(r => r.exchange !== 'moex'))
      setStockVol(stocks)
      setLastSync(new Date())
    } catch (e) {
      console.error('TradFiMarketShare: failed to load volumes', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load(view) }, [view])

  const weekly = view === 'weekly'

  // charts 1–4 span ALL TradFi instruments: curated commodities/metals/indices
  // plus the full equity-perp universe from the stock ETL (no double-counting —
  // curated company stocks are dropped in favour of the fuller stock source).
  const exchangeRows   = useMemo(() => mergedByExchange(tradfiRows, stockVol),   [tradfiRows, stockVol])
  const instrumentRows = useMemo(() => mergedByInstrument(tradfiRows, stockVol), [tradfiRows, stockVol])
  const groupRows      = useMemo(() => assetGroupRows(allRows, stockVol),        [allRows, stockVol])

  return (
    <div>
      <div className="page-toolbar">
        <h1>TradFi Market Share</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          {weekly
            ? 'Weekly total volume of traditional asset perps on crypto exchanges · RUB · YTD'
            : 'Daily volume of traditional asset perps on crypto exchanges · RUB · last 30 days'}
          {lastSync && ` · loaded ${lastSync.toLocaleTimeString()}`}
        </div>
        <div className="type-filter">
          <button
            className={`filter-btn ${view === 'daily' ? 'filter-btn--active' : ''}`}
            onClick={() => setView('daily')}
            disabled={loading}
          >
            Daily
          </button>
          <button
            className={`filter-btn ${view === 'weekly' ? 'filter-btn--active' : ''}`}
            onClick={() => setView('weekly')}
            disabled={loading}
          >
            Weekly
          </button>
        </div>
        <button
          className="btn-secondary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={() => load(view)}
          disabled={loading}
        >
          <RefreshCw size={13} className={loading ? 'spin' : ''} />
          Refresh
        </button>
      </div>

      <ExchangeSourceBadges
        exchanges={['binance', 'okx', 'bybit', 'mexc', 'hyperliquid']}
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
        <>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 8 }}>
          <div className="card analytics-card">
            <ByExchangeAbsolute rows={exchangeRows} theme={theme} weekly={weekly} />
          </div>
          <div className="card analytics-card">
            <ByExchangePercent rows={exchangeRows} theme={theme} weekly={weekly} />
          </div>
          <div className="card analytics-card">
            <ByInstrumentAbsolute rows={instrumentRows} theme={theme} weekly={weekly} />
          </div>
          <div className="card analytics-card">
            <ByInstrumentPercent rows={instrumentRows} theme={theme} weekly={weekly} />
          </div>
          <div className="card analytics-card">
            <ByAssetGroupAbsolute rows={groupRows} theme={theme} weekly={weekly} />
          </div>
          <div className="card analytics-card">
            <ByAssetGroupPercent rows={groupRows} theme={theme} weekly={weekly} />
          </div>
        </div>

        {stockVol && stockVol.by_exchange.length > 0 && (
          <div style={{ marginTop: 24 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text)', margin: '8px 0 4px' }}>
              Акции — объём торгов вечными фьючерсами (все криптобиржи, все компании)
            </div>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12 }}>
              {weekly
                ? 'Недельный объём · все компании-акции на Binance / OKX / Bybit / MEXC / Hyperliquid · RUB · YTD'
                : 'Дневной объём · все компании-акции на Binance / OKX / Bybit / MEXC / Hyperliquid · RUB · last 30 days'}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 16 }}>
              <div className="card analytics-card">
                <StockByExchange rows={stockVol.by_exchange} theme={theme} weekly={weekly} />
              </div>
              <div className="card analytics-card">
                <StockByInstrument rows={stockVol.by_instrument} theme={theme} weekly={weekly} />
              </div>
            </div>
          </div>
        )}
        </>
      )}
    </div>
  )
}
