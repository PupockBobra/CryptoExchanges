import { useEffect, useMemo, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw, Download } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { SectionHeading } from '../components/SectionHeading'
import { exportByBase } from '../utils/exportCsv'
import { fetchJson } from '../utils/api'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/spb'
const FONT_FAMILY = 'Inter, system-ui, sans-serif'

// Group colours (matches the SPB Volume / Weekly grouping: US Market vs Crypto).
const GROUP_ORDER = ['US Market', 'Crypto'] as const
const GROUP_COLORS: Record<string, string> = {
  'US Market': '#3f51b5',
  Crypto:      '#00e5ff',
}

// Raw API row shapes.
export interface VolumeRow { date: string; date_label: string; ticker: string; name: string; group: string; turnover_rub: number }
export interface OiRow     { date: string; date_label: string; ticker: string; name: string; group: string; oi_contracts: number; oi_rub: number }

// Generic row consumed by the charts — one RUB value per (date, instrument).
export interface ShareRow { date: string; ticker: string; name: string; group: string; value: number }

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

function baseLayout(title: string, theme: 'dark' | 'light'): Partial<Plotly.Layout> {
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
    hoverlabel: {
      bgcolor: t.hover, bordercolor: t.hoverBorder,
      font: { color: t.hoverText, size: 9, family: FONT_FAMILY },
      namelength: -1,   // never truncate instrument names in the unified tooltip
    },
    hovermode: 'x unified',
  }
}

// Horizontal labels showing each bar's stacked total, centred above the column.
// The ₽ and B units are dropped (the y-axis already reads the metric + "₽B") so
// the labels are short enough not to overlap.  Rendered as layout annotations.
function totalsAnnotations(labels: string[], totalsB: number[], theme: 'dark' | 'light'): Partial<Plotly.Annotations>[] {
  const color = themeTokens(theme).title
  const out: Partial<Plotly.Annotations>[] = []
  labels.forEach((lab, i) => {
    const v = totalsB[i]
    if (v > 0) out.push({
      x: lab, y: v, xref: 'x', yref: 'y',
      text: v.toFixed(1),
      showarrow: false,
      xanchor: 'center', yanchor: 'bottom',
      yshift: 3,
      font: { color, size: 9, family: FONT_FAMILY },
    })
  })
  return out
}

// Top-of-axis headroom so the vertical total labels aren't clipped (e.g. the
// tallest bar's label). Returns the absolute-chart yaxis with a padded range.
function absYAxis(theme: 'dark' | 'light', maxB: number, metric: string): Partial<Plotly.LayoutAxis> {
  const t = themeTokens(theme)
  return {
    title: { text: `${metric} (₽B)`, font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
    automargin: true,
    tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
    gridcolor: t.grid, linecolor: t.grid,
    tickprefix: '₽', tickformat: ',.1f', ticksuffix: 'B',
    range: maxB > 0 ? [0, maxB * 1.25] : undefined,
  }
}

function pctYAxis(theme: 'dark' | 'light'): Partial<Plotly.LayoutAxis> {
  const t = themeTokens(theme)
  return {
    title: { text: 'Share (%)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
    automargin: true,
    tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
    gridcolor: t.grid, linecolor: t.grid,
    ticksuffix: '%', range: [0, 100],
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

function dateLabels(dates: string[]): string[] {
  return dates.map(d => new Date(d + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }))
}

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

// ── By instrument (absolute ₽B / percent) ─────────────────────────────────────

function ByInstrument({ rows, theme, percent, metric, exportName }: { rows: ShareRow[]; theme: 'dark' | 'light'; percent: boolean; metric: string; exportName: string }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates  = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dateLabels(dates)
    // Tickers ordered by display name; one stacked series each.
    const tickers = Array.from(new Map(rows.map(r => [r.ticker, r.name])).entries())
      .sort((a, b) => a[1].localeCompare(b[1]))

    const totals = new Map<string, number>()
    dates.forEach(d => totals.set(d, rows.filter(r => r.date === d).reduce((s, r) => s + r.value, 0)))

    const traces: Plotly.Data[] = tickers.map(([ticker, name]) => {
      const byDate = new Map<string, number>()
      rows.filter(r => r.ticker === ticker).forEach(r => byDate.set(r.date, r.value))
      const y = dates.map(d => {
        const v = byDate.get(d) ?? 0
        if (percent) { const tot = totals.get(d) ?? 0; return tot > 0 ? Math.round((v / tot) * 1000) / 10 : null }
        return v > 0 ? v / 1e9 : null
      })
      const hasAny = y.some(v => v !== null && (v as number) > 0)
      return {
        type: 'bar', name, x: labels, y,
        opacity: 0.85,
        visible: hasAny ? true : 'legendonly',
        hovertemplate: percent ? `<b>${name}</b>: %{y:.1f}%<extra></extra>` : `<b>${name}</b>: ₽%{y:.2f}B<extra></extra>`,
      } satisfies Plotly.Data
    })

    const totalsB = dates.map(d => (totals.get(d) ?? 0) / 1e9)

    const layout: Partial<Plotly.Layout> = {
      ...baseLayout(percent ? 'Instrument Share (%)' : `Instrument ${metric} (₽B)`, theme),
      yaxis: percent ? pctYAxis(theme) : absYAxis(theme, Math.max(0, ...totalsB), metric),
      annotations: percent ? [] : totalsAnnotations(labels, totalsB, theme),
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme, percent, metric])

  useEffect(() => { const el = divRef.current; return () => { if (el) Plotly.purge(el) } }, [])

  return (
    <>
      <div ref={divRef} style={{ width: '100%', height: 480 }} />
      {!percent && (
        <ExportButton onClick={() => exportByBase(
          rows.map(r => ({ date: r.date, symbol: r.ticker, exchange: 'spb', volume_rub: r.value })),
          exportName,
        )} />
      )}
    </>
  )
}

// ── By group (absolute ₽B / percent) ──────────────────────────────────────────

function ByGroup({ rows, theme, percent, metric }: { rows: ShareRow[]; theme: 'dark' | 'light'; percent: boolean; metric: string }) {
  const divRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!divRef.current) return
    const dates  = Array.from(new Set(rows.map(r => r.date))).sort()
    const labels = dateLabels(dates)
    const totals = new Map<string, number>()
    dates.forEach(d => totals.set(d, rows.filter(r => r.date === d).reduce((s, r) => s + r.value, 0)))

    const traces: Plotly.Data[] = GROUP_ORDER.map(group => {
      const byDate = new Map<string, number>()
      rows.filter(r => r.group === group).forEach(r => byDate.set(r.date, (byDate.get(r.date) ?? 0) + r.value))
      const y = dates.map(d => {
        const v = byDate.get(d) ?? 0
        if (percent) { const tot = totals.get(d) ?? 0; return tot > 0 ? Math.round((v / tot) * 1000) / 10 : null }
        return v > 0 ? v / 1e9 : null
      })
      const hasAny = y.some(v => v !== null && (v as number) > 0)
      return {
        type: 'bar', name: group, x: labels, y,
        marker: { color: GROUP_COLORS[group], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: percent ? `<b>${group}</b>: %{y:.1f}%<extra></extra>` : `<b>${group}</b>: ₽%{y:.2f}B<extra></extra>`,
      } satisfies Plotly.Data
    })

    const totalsB = dates.map(d => (totals.get(d) ?? 0) / 1e9)

    const layout: Partial<Plotly.Layout> = {
      ...baseLayout(percent ? 'Group Share (%)' : `Group ${metric} (₽B)`, theme),
      yaxis: percent ? pctYAxis(theme) : absYAxis(theme, Math.max(0, ...totalsB), metric),
      annotations: percent ? [] : totalsAnnotations(labels, totalsB, theme),
    }
    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [rows, theme, percent, metric])

  useEffect(() => { const el = divRef.current; return () => { if (el) Plotly.purge(el) } }, [])

  return <div ref={divRef} style={{ width: '100%', height: 380 }} />
}

// A metric section = the 2×2 grid (instrument abs/%, group abs/%).
// Exported so the SPB Screenshot page can reuse the exact same grid.
export function MetricSection({ rows, theme, metric, slug }: { rows: ShareRow[]; theme: 'dark' | 'light'; metric: string; slug: string }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 8 }}>
      <div className="card analytics-card"><ByInstrument rows={rows} theme={theme} percent={false} metric={metric} exportName={`spb-${slug}-market-share-by-instrument.csv`} /></div>
      <div className="card analytics-card"><ByInstrument rows={rows} theme={theme} percent={true}  metric={metric} exportName={`spb-${slug}-market-share-by-instrument.csv`} /></div>
      <div className="card analytics-card"><ByGroup rows={rows} theme={theme} percent={false} metric={metric} /></div>
      <div className="card analytics-card"><ByGroup rows={rows} theme={theme} percent={true}  metric={metric} /></div>
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export function SPBMarketShare() {
  const [volRows, setVolRows] = useState<ShareRow[]>([])
  const [oiRows, setOiRows]   = useState<ShareRow[]>([])
  const [loading, setLoading] = useState(true)
  const [lastSync, setLastSync] = useState<Date | null>(null)
  const theme = useTheme()

  const load = async () => {
    setLoading(true)
    // allSettled: one failing feed must not blank the other (Volume and OI come
    // from independent sources — Finam vs the exchange's own API).
    const [vol, oi] = await Promise.allSettled([
      fetchJson<VolumeRow[]>(`${API}/daily-volume`),
      fetchJson<OiRow[]>(`${API}/open-interest`),
    ])
    if (vol.status === 'fulfilled') {
      setVolRows(vol.value.map(r => ({ date: r.date, ticker: r.ticker, name: r.name, group: r.group, value: r.turnover_rub })))
    } else {
      console.error('SPBMarketShare: failed to load volume', vol.reason)
    }
    if (oi.status === 'fulfilled') {
      setOiRows(oi.value.map(r => ({ date: r.date, ticker: r.ticker, name: r.name, group: r.group, value: r.oi_rub })))
    } else {
      console.error('SPBMarketShare: failed to load open interest', oi.reason)
    }
    if (vol.status === 'fulfilled' || oi.status === 'fulfilled') setLastSync(new Date())
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const empty = useMemo(() => volRows.length === 0 && oiRows.length === 0, [volRows, oiRows])

  return (
    <div>
      <div className="page-toolbar">
        <h1>SPB Market Share</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Доля инструментов в обороте и открытом интересе СПБ Биржи · RUB · last 30 days
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

      <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 0 }}>
        <div style={{ padding: '12px 20px 12px 16px', borderRight: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Страница показывает
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Долю каждого инструмента и группы (
            <span style={{ color: GROUP_COLORS['US Market'], fontWeight: 600 }}>US Market</span> /{' '}
            <span style={{ color: GROUP_COLORS['Crypto'], fontWeight: 600 }}>Crypto</span>
            ) в дневном обороте и открытом интересе СПБ Биржи за последние 30 дней — в абсолюте и в процентах.
          </div>
        </div>
        <div style={{ padding: '12px 16px 12px 20px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Единица измерения
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Абсолют — ₽B (миллиарды рублей), доля — % от суммарного дневного значения.
          </div>
        </div>
      </div>

      {loading ? (
        <p className="empty">Loading…</p>
      ) : empty ? (
        <p className="empty">No data yet</p>
      ) : (
        <>
          <SectionHeading label="Volume" />
          <MetricSection rows={volRows} theme={theme} metric="Volume" slug="volume" />
          <SectionHeading label="Open Interest" />
          <div style={{ fontSize: 12, color: 'var(--muted)', margin: '-4px 0 4px' }}>
            Открытый интерес учитывается с двух сторон (long + short).
          </div>
          <MetricSection rows={oiRows} theme={theme} metric="OI" slug="oi" />
        </>
      )}
    </div>
  )
}
