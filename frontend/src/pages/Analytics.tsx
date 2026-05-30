/**
 * Analytics — Weekly ADTV dynamics YTD
 *
 * One stacked-bar chart per instrument.  Each bar represents one ISO week;
 * bar segments are coloured by exchange so the total height is the combined
 * ADTV across all exchanges for that week.
 *
 * Rendered with Plotly.js — same chart engine as Python's plotly library,
 * same visual output as plotly.express.bar(..., barmode='stack').
 */

import { useEffect, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { EXCHANGES, EXCHANGE_COLORS, SYMBOL_SECTIONS, classifySymbol, formatSymbol } from '../types'
import type { Exchange, SymbolSection } from '../types'

// Sections where MOEX has no data — exclude from traces entirely
const MOEX_SECTIONS: SymbolSection[] = ['US Market', 'Spot Crypto']

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/history'

// ── Types ─────────────────────────────────────────────────────────────────────

interface WeeklyRow {
  week_start:   string   // 'YYYY-MM-DD'
  week_label:   string   // 'Jan 06'
  symbol:       string
  exchange:     string
  days_in_week: number
  adtv:         number   // RUB
}

// ── Layout helpers ────────────────────────────────────────────────────────────

const FONT_FAMILY = 'Inter, system-ui, sans-serif'

// week_start is Monday (YYYY-MM-DD); returns e.g. "May 18 – May 24"
function weekRangeLabel(weekStart: string): string {
  const start = new Date(weekStart + 'T00:00:00')
  const end   = new Date(start)
  end.setDate(end.getDate() + 6)
  const fmt = (d: Date) => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  return `${fmt(start)} – ${fmt(end)}`
}

function themeTokens(theme: 'dark' | 'light') {
  if (theme === 'light') return {
    bg:      '#ffffff',
    paper:   '#f8fafc',
    grid:    '#e2e8f0',
    text:    '#64748b',
    title:   '#1e293b',
    hover:   '#ffffff',
    hoverBorder: '#e2e8f0',
    hoverText:   '#1e293b',
  }
  return {
    bg:      '#0f1117',
    paper:   '#1a1d27',
    grid:    '#1f2937',
    text:    '#9ca3af',
    title:   '#e2e8f0',
    hover:   '#21263a',
    hoverBorder: '#2d3148',
    hoverText:   '#e2e8f0',
  }
}

function buildLayout(title: string, theme: 'dark' | 'light'): Partial<Plotly.Layout> {
  const t = themeTokens(theme)
  return {
    title: {
      text: title,
      font: { color: t.title, size: 14, family: FONT_FAMILY },
      x: 0.02,
      xanchor: 'left',
    },
    barmode:       'stack',
    paper_bgcolor: t.paper,
    plot_bgcolor:  t.bg,
    margin:        { l: 70, r: 16, t: 44, b: 120 },
    legend: {
      orientation: 'h',
      x: 0, y: -0.35,
      font: { color: t.text, size: 11, family: FONT_FAMILY },
      bgcolor: 'transparent',
    },
    xaxis: {
      tickangle: -40,
      tickfont:  { color: t.text, size: 10, family: FONT_FAMILY },
      gridcolor: t.grid,
      linecolor: t.grid,
      showgrid:  false,
    },
    yaxis: {
      title: { text: 'ADTV (₽B)', font: { color: t.text, size: 11, family: FONT_FAMILY } },
      tickfont:   { color: t.text, size: 10, family: FONT_FAMILY },
      gridcolor:  t.grid,
      linecolor:  t.grid,
      tickprefix: '₽',
      tickformat: ',.1f',
      ticksuffix: 'B',
      hoverformat:',.1f',
    },
    hoverlabel: {
      bgcolor:     t.hover,
      bordercolor: t.hoverBorder,
      font: { color: t.hoverText, size: 12, family: FONT_FAMILY },
    },
    hovermode: 'x unified',
  }
}

const PLOTLY_CONFIG: Partial<Plotly.Config> = {
  displayModeBar:  true,
  modeBarButtonsToRemove: [
    'select2d', 'lasso2d', 'autoScale2d', 'hoverClosestCartesian',
    'hoverCompareCartesian', 'toggleSpikelines',
  ] as Plotly.ModeBarDefaultButtons[],
  displaylogo: false,
  responsive:  true,
}

// ── Single instrument chart ───────────────────────────────────────────────────

interface ChartProps {
  symbol: string
  rows:   WeeklyRow[]
}

function WeeklyAdtvChart({ symbol, rows }: ChartProps) {
  const divRef = useRef<HTMLDivElement>(null)
  const theme  = useTheme()

  useEffect(() => {
    if (!divRef.current || !rows.length) return

    const section = classifySymbol(symbol)

    // Collect ordered list of week starts (X axis), label as date range
    const weekStarts = Array.from(new Set(rows.map((r) => r.week_start))).sort()
    const labels = weekStarts.map(weekRangeLabel)

    // Auto-scale: if peak weekly ADTV across all exchanges < 30B → use millions
    const maxAdtv = Math.max(...rows.map((r) => r.adtv))
    const useMillions = maxAdtv < 30e9
    const scale  = useMillions ? 1e6 : 1e9
    const suffix = useMillions ? 'M' : 'B'

    // Exchanges to include: omit moex for sections that have no MOEX data
    const visibleExchanges = MOEX_SECTIONS.includes(section)
      ? EXCHANGES.filter((ex) => ex !== 'moex')
      : EXCHANGES

    // Build one trace per exchange; Y values pre-scaled for readable ticks
    const traces: Plotly.Data[] = visibleExchanges.map((ex: Exchange) => {
      const byWeek = new Map<string, number>()
      rows.filter((r) => r.exchange === ex).forEach((r) => byWeek.set(r.week_start, r.adtv))

      const y = weekStarts.map((w) => {
        const v = byWeek.get(w)
        return v != null ? v / scale : null
      })
      const hasAny = y.some((v) => v !== null && v > 0)

      return {
        type:        'bar',
        name:        ex,
        x:           labels,
        y,
        marker:      { color: EXCHANGE_COLORS[ex], opacity: 0.85 },
        visible:     hasAny ? true : 'legendonly',
        hovertemplate: `<b>${ex}</b>: ₽%{y:.1f}${suffix}<extra></extra>`,
      } satisfies Plotly.Data
    })

    const layout = buildLayout(formatSymbol(symbol), theme)
    layout.yaxis = {
      ...layout.yaxis,
      title: { text: `ADTV (₽${suffix})`, font: { color: themeTokens(theme).text, size: 11, family: FONT_FAMILY } },
      ticksuffix: suffix,
    }

    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [symbol, rows, theme])

  // Clean up on unmount
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

// ── Page ──────────────────────────────────────────────────────────────────────

export function Analytics() {
  const [allRows,   setAllRows]   = useState<WeeklyRow[]>([])
  const [loading,   setLoading]   = useState(true)
  const [lastSync,  setLastSync]  = useState<Date | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const data: WeeklyRow[] = await fetch(`${API}/weekly-adtv`).then((r) => r.json())
      setAllRows(data)
      setLastSync(new Date())
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  // Distinct ordered symbols
  const symbols = Array.from(new Set(allRows.map((r) => r.symbol))).sort()

  // Summary: last full week's combined ADTV across all symbols
  const lastWeeks = new Map<string, number>()
  allRows.forEach((r) => {
    const cur = lastWeeks.get(r.week_start) ?? 0
    lastWeeks.set(r.week_start, cur + r.adtv)
  })
  const sortedWeeks = Array.from(lastWeeks.entries()).sort((a, b) => a[0].localeCompare(b[0]))
  // pick last COMPLETE week (second from last — last may be partial current week)
  const lastCompleteWeek = sortedWeeks.length >= 2 ? sortedWeeks[sortedWeeks.length - 2] : null
  const currentWeek      = sortedWeeks.length >= 1 ? sortedWeeks[sortedWeeks.length - 1] : null

  function fmtRub(v: number) {
    if (v >= 1e9) return `₽${(v / 1e9).toFixed(2)}B`
    if (v >= 1e6) return `₽${(v / 1e6).toFixed(1)}M`
    return `₽${v.toFixed(0)}`
  }

  return (
    <div>
      {/* ── Toolbar ── */}
      <div className="page-toolbar">
        <h1>Weekly ADTV Analytics</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Average Daily Trading Volume per ISO week · all exchanges + MOEX FORTS stacked · RUB
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

      {/* ── Summary strip ── */}
      {!loading && sortedWeeks.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap', alignItems: 'center' }}>
            {lastCompleteWeek && (
              <div>
                <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '.05em' }}>
                  Last complete week ADTV
                  <span style={{ marginLeft: 6, color: 'var(--border)', fontWeight: 400 }}>
                    (w/c {lastCompleteWeek[0]})
                  </span>
                </div>
                <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2 }}>
                  {fmtRub(lastCompleteWeek[1])}
                </div>
              </div>
            )}
            {currentWeek && (
              <div>
                <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '.05em' }}>
                  Current week ADTV (partial)
                </div>
                <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2 }}>
                  {fmtRub(currentWeek[1])}
                </div>
              </div>
            )}
            {lastCompleteWeek && currentWeek && (() => {
              const wow = (currentWeek[1] / lastCompleteWeek[1] - 1) * 100
              const up = wow >= 0
              return (
                <div>
                  <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '.05em' }}>WoW trend</div>
                  <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2, color: up ? 'var(--green)' : 'var(--red)' }}>
                    {up ? '+' : ''}{wow.toFixed(1)}%
                  </div>
                </div>
              )
            })()}
            <div style={{ marginLeft: 'auto' }}>
              <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                {EXCHANGES.map((ex) => (
                  <span key={ex} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, color: 'var(--muted)' }}>
                    <span style={{ width: 10, height: 10, borderRadius: 2, background: EXCHANGE_COLORS[ex], display: 'inline-block' }} />
                    {ex}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Charts grouped by section ── */}
      {loading ? (
        <p className="empty">Loading weekly ADTV data…</p>
      ) : symbols.length === 0 ? (
        <p className="empty">No data yet — run a backfill from the Historical page</p>
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
                <div className="analytics-grid">
                  {sectionSyms.map((sym) => (
                    <WeeklyAdtvChart
                      key={sym}
                      symbol={sym}
                      rows={allRows.filter((r) => r.symbol === sym)}
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
