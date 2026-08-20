import { useEffect, useMemo, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw, Download } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { SectionHeading } from '../components/SectionHeading'
import { exportWeeklyCsv } from '../utils/exportCsv'
import { fetchJson } from '../utils/api'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/spb'

// Section order on the page; tickers carry their group from the backend.
const GROUP_ORDER = ['US Market', 'Crypto']

// СПБ Биржа brand cyan — single-source page, so one colour for every bar.
const SPB_COLOR = '#00a3c4'

interface WeeklyRow {
  week_start:   string   // 'YYYY-MM-DD' (Monday)
  week_label:   string   // 'May 18'
  ticker:       string   // 'AMZNperpA'
  name:         string   // 'Amazon.com'
  group:        string   // 'US Market' | 'Crypto'
  days_in_week: number
  adtv:         number   // RUB
}

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

const PLOTLY_CONFIG: Partial<Plotly.Config> = {
  displayModeBar: true,
  modeBarButtonsToRemove: [
    'select2d', 'lasso2d', 'autoScale2d', 'hoverClosestCartesian',
    'hoverCompareCartesian', 'toggleSpikelines',
  ] as Plotly.ModeBarDefaultButtons[],
  displaylogo: false,
  responsive: true,
}

interface ChartProps { ticker: string; name: string; rows: WeeklyRow[] }

function WeeklyAdtvChart({ ticker, name, rows }: ChartProps) {
  const divRef = useRef<HTMLDivElement>(null)
  const theme  = useTheme()

  useEffect(() => {
    if (!divRef.current || !rows.length) return
    const t = themeTokens(theme)

    const weekStarts = Array.from(new Set(rows.map(r => r.week_start))).sort()
    const labels = weekStarts.map(weekRangeLabel)

    // Weekly ADTV is same magnitude as daily turnover → show in ₽M.
    const byWeek = new Map<string, number>()
    rows.forEach(r => byWeek.set(r.week_start, r.adtv))
    const y = weekStarts.map(w => { const v = byWeek.get(w); return v != null ? v / 1e6 : null })

    const trace: Plotly.Data = {
      type: 'bar', name, x: labels, y,
      marker: { color: SPB_COLOR, opacity: 0.85 },
      hovertemplate: '₽%{y:.1f}M<extra></extra>',
    }

    const layout: Partial<Plotly.Layout> = {
      title: { text: name, font: { color: t.title, size: 14, family: FONT_FAMILY }, x: 0.02, xanchor: 'left' },
      paper_bgcolor: t.paper,
      plot_bgcolor:  t.bg,
      margin: { l: 70, r: 16, t: 44, b: 90 },
      showlegend: false,
      xaxis: {
        tickangle: -40,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid, showgrid: false,
      },
      yaxis: {
        title: { text: 'ADTV (₽M)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
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

    Plotly.react(divRef.current, [trace], layout, PLOTLY_CONFIG)
  }, [ticker, name, rows, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return (
    <div className="card analytics-card">
      <div ref={divRef} style={{ width: '100%', height: 360 }} />
      {rows.length > 0 && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', paddingTop: 4 }}>
          <button
            onClick={() => exportWeeklyCsv(
              rows.map(r => ({ week_start: r.week_start, symbol: name, exchange: 'spb', adtv: r.adtv })),
              name,
              `spb-weekly-adtv-${ticker}.csv`,
            )}
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

export function SPBWeekly() {
  const [allRows, setAllRows] = useState<WeeklyRow[]>([])
  const [loading, setLoading] = useState(true)
  const [lastSync, setLastSync] = useState<Date | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const data = await fetchJson<WeeklyRow[]>(`${API}/weekly-adtv`)
      setAllRows(data)
      setLastSync(new Date())
    } catch (e) {
      console.error('SPBWeekly: failed to load weekly ADTV', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const tickers = useMemo(() => {
    const seen = new Map<string, { ticker: string; name: string; group: string }>()
    for (const r of allRows) {
      if (!seen.has(r.ticker)) seen.set(r.ticker, { ticker: r.ticker, name: r.name, group: r.group })
    }
    return Array.from(seen.values()).sort((a, b) => a.name.localeCompare(b.name))
  }, [allRows])

  const rowsByTicker = useMemo(() => {
    const map = new Map<string, WeeklyRow[]>()
    for (const r of allRows) {
      const arr = map.get(r.ticker)
      if (arr) arr.push(r); else map.set(r.ticker, [r])
    }
    return map
  }, [allRows])

  return (
    <div>
      <div className="page-toolbar">
        <h1>SPB Weekly Performance</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          СПБ Биржа perpetual futures · weekly ADTV · RUB
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
            Средний дневной оборот (ADTV) вечных фьючерсов СПБ Биржи по неделям, по каждому инструменту.
          </div>
        </div>
        <div style={{ padding: '12px 16px 12px 20px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Единица измерения
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            ₽M (миллионы рублей).
</div>
        </div>
      </div>

      {loading ? (
        <p className="empty">Loading SPB weekly ADTV data…</p>
      ) : tickers.length === 0 ? (
        <p className="empty">No data yet</p>
      ) : (
        <>
          {GROUP_ORDER.map(group => {
            const groupTickers = tickers.filter(t => t.group === group)
            if (!groupTickers.length) return null
            return (
              <div key={group}>
                <SectionHeading label={group} />
                <div className="analytics-grid">
                  {groupTickers.map(({ ticker, name }) => (
                    <WeeklyAdtvChart
                      key={ticker}
                      ticker={ticker}
                      name={name}
                      rows={rowsByTicker.get(ticker) ?? []}
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
