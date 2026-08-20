import { useEffect, useMemo, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { EXCHANGES, EXCHANGE_COLORS, SYMBOL_SECTIONS, classifySymbol, formatSymbol } from '../types'
import type { Exchange, SymbolSection } from '../types'
import { SectionHeading } from '../components/SectionHeading'
import { ExchangeSourceBadges } from '../components/ExchangeSourceBadges'
import { fetchJson } from '../utils/api'
import { pickUnit, maxStackedTotal } from '../utils/scale'
import { sortSymbolsByValue } from '../utils/rank'
import { mskHourLabel, hourTick } from '../utils/hourly'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/history'

type View = 'series' | 'profile'

/**
 * Both endpoints answer in the columnar shape the spread-history endpoints use:
 * one shared x-axis plus a value array per symbol × exchange.  `null` means the
 * instrument had no bar in that hour — not zero volume.
 */
interface Series { symbol: string; exchange: string; values: (number | null)[] }
interface HourlyResponse<A> { days: number; axis: A[]; series: Series[] }

const SERIES_DAYS  = 7
const PROFILE_DAYS = 30

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

const PLOTLY_CONFIG: Partial<Plotly.Config> = {
  displayModeBar: true,
  modeBarButtonsToRemove: [
    'select2d', 'lasso2d', 'autoScale2d', 'hoverClosestCartesian',
    'hoverCompareCartesian', 'toggleSpikelines',
  ] as Plotly.ModeBarDefaultButtons[],
  displaylogo: false,
  responsive: true,
}

interface ChartProps { symbol: string; series: Series[]; categories: string[]; view: View }

function HourlyChart({ symbol, series, categories, view }: ChartProps) {
  const divRef = useRef<HTMLDivElement>(null)
  const theme  = useTheme()

  useEffect(() => {
    if (!divRef.current || !series.length) return
    const t = themeTokens(theme)

    // Unit follows this instrument's own scale — see utils/scale.ts
    const { scale, suffix } = pickUnit(
      maxStackedTotal(
        series.flatMap(s => s.values.map((v, i) => ({ key: categories[i], value: v }))),
      ),
    )

    const traces: Plotly.Data[] = EXCHANGES.map((ex: Exchange) => {
      const row = series.find(s => s.exchange === ex)
      const y   = categories.map((_, i) => {
        const v = row?.values[i]
        return v != null ? v / scale : null
      })
      const hasAny = y.some(v => v !== null && v > 0)
      return {
        type: 'bar', name: ex, x: categories, y,
        marker: { color: EXCHANGE_COLORS[ex], opacity: 0.85 },
        visible: hasAny ? true : 'legendonly',
        hovertemplate: `<b>${ex}</b>: ₽%{y:.1f}${suffix}<extra></extra>`,
      } satisfies Plotly.Data
    })

    const layout: Partial<Plotly.Layout> = {
      title: { text: formatSymbol(symbol), font: { color: t.title, size: 14, family: FONT_FAMILY }, x: 0.02, xanchor: 'left' },
      barmode: 'stack',
      bargap: 0.15,
      paper_bgcolor: t.paper,
      plot_bgcolor:  t.bg,
      margin: { l: 70, r: 16, t: 44, b: view === 'series' ? 110 : 80 },
      legend: {
        orientation: 'h', x: 0, y: view === 'series' ? -0.42 : -0.28,
        font: { color: t.text, size: 11, family: FONT_FAMILY },
        bgcolor: 'transparent',
      },
      xaxis: {
        type: 'category',
        // The series axis carries ~168 hourly categories — let Plotly thin the
        // ticks instead of printing every hour on top of itself.
        tickangle: -40,
        nticks: view === 'series' ? 14 : 24,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid, showgrid: false,
        title: view === 'profile'
          ? { text: 'Час МСК', font: { color: t.text, size: 11, family: FONT_FAMILY } }
          : undefined,
      },
      yaxis: {
        title: {
          text: view === 'profile' ? `Средний объём (₽${suffix})` : `Volume (₽${suffix})`,
          font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14,
        },
        automargin: true,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        tickprefix: '₽', tickformat: ',.1f', ticksuffix: suffix, hoverformat: ',.1f',
      },
      hoverlabel: {
        bgcolor: t.hover, bordercolor: t.hoverBorder,
        font: { color: t.hoverText, size: 12, family: FONT_FAMILY },
      },
      hovermode: 'x unified',
    }

    Plotly.react(divRef.current, traces, layout, PLOTLY_CONFIG)
  }, [symbol, series, categories, view, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return (
    <div className="card analytics-card">
      <div ref={divRef} style={{ width: '100%', height: 360 }} />
      {!series.length && (
        <p className="empty" style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          No data
        </p>
      )}
    </div>
  )
}

export function HourlyVolume() {
  const [view, setView]             = useState<View>('profile')
  const [seriesBySymbol, setSeriesBySymbol] = useState<Map<string, Series[]>>(new Map())
  const [categories, setCategories] = useState<string[]>([])
  const [usStocks, setUsStocks]     = useState<Set<string>>(new Set())
  const [loading, setLoading]       = useState(true)
  const [lastSync, setLastSync]     = useState<Date | null>(null)

  const load = async (v: View) => {
    setLoading(true)
    try {
      const [data, stocks] = await Promise.all([
        v === 'series'
          ? fetchJson<HourlyResponse<string>>(`${API}/hourly-volume?days=${SERIES_DAYS}`)
          : fetchJson<HourlyResponse<number>>(`${API}/hourly-profile?days=${PROFILE_DAYS}`),
        fetchJson<{ tickers: string[] }>(`${API}/us-stock-tickers`),
      ])

      // The axis arrives ordered (UTC timestamps / hours 0–23); only the labels
      // differ between the two views.
      const cats = v === 'series'
        ? (data.axis as string[]).map(mskHourLabel)
        : (data.axis as number[]).map(hourTick)

      const bySymbol = new Map<string, Series[]>()
      for (const s of data.series) {
        const arr = bySymbol.get(s.symbol)
        if (arr) arr.push(s); else bySymbol.set(s.symbol, [s])
      }

      setSeriesBySymbol(bySymbol)
      setCategories(cats)
      setUsStocks(new Set(stocks.tickers))
      setLastSync(new Date())
    } catch (e) {
      console.error('HourlyVolume: failed to load hourly volume', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load(view) }, [view])

  // Ranked by total turnover in the window, biggest first — not alphabetically.
  const symbols = useMemo(() => {
    const totals: { symbol: string; value: number }[] = []
    for (const [sym, arr] of seriesBySymbol) {
      for (const s of arr) {
        for (const v of s.values) if (v != null) totals.push({ symbol: sym, value: v })
      }
    }
    return sortSymbolsByValue(totals, r => r.symbol, r => r.value)
  }, [seriesBySymbol])

  return (
    <div>
      <div className="page-toolbar">
        <h1>Hourly Volume</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          {view === 'series'
            ? `Часовой оборот · МСК · последние ${SERIES_DAYS} дней`
            : `Средний оборот по часам суток · МСК · за ${PROFILE_DAYS} дней`}
          {lastSync && ` · loaded ${lastSync.toLocaleTimeString()}`}
        </div>
        <div className="type-filter">
          <button
            className={`filter-btn ${view === 'profile' ? 'filter-btn--active' : ''}`}
            onClick={() => setView('profile')}
            disabled={loading}
          >
            Профиль
          </button>
          <button
            className={`filter-btn ${view === 'series' ? 'filter-btn--active' : ''}`}
            onClick={() => setView('series')}
            disabled={loading}
          >
            Ряд
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
        exchanges={['binance', 'okx', 'bybit', 'mexc', 'hyperliquid', 'bitget']}
      />

      <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 0 }}>
        <div style={{ padding: '12px 20px 12px 16px', borderRight: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Страница показывает
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            {view === 'profile'
              ? `Средний оборот в каждый час суток за ${PROFILE_DAYS} дней — в какие часы реально идёт ликвидность. Дневной шум усреднён.`
              : `Оборот час за часом за последние ${SERIES_DAYS} дней — видны конкретные всплески и события.`}
            {' '}Только криптобиржи: MOEX не публикует рублёвый оборот во внутридневных свечах.
          </div>
        </div>
        <div style={{ padding: '12px 16px 12px 20px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Единица измерения
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Рубли по курсу USDRUBF, время московское. Единица подбирается под каждый инструмент: ₽K / ₽M / ₽B / ₽T — по самому объёмному часу на графике.
          </div>
        </div>
      </div>

      {loading ? (
        <p className="empty">Loading hourly volume data…</p>
      ) : symbols.length === 0 ? (
        <p className="empty">Пока нет данных — часовые свечи собираются с момента первого запуска ETL.</p>
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
                    <HourlyChart
                      key={sym}
                      symbol={sym}
                      series={seriesBySymbol.get(sym) ?? []}
                      categories={categories}
                      view={view}
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
