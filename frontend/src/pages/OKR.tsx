import { useEffect, useMemo, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { fetchJson } from '../utils/api'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/okr'

const DAYS = 30
const LINE_COLOR = '#7c3aed'
const FONT_FAMILY = 'Inter, system-ui, sans-serif'

interface Point {
  date:       string        // 'YYYY-MM-DD'
  date_label: string        // 'Aug 19'
  moex_rub:   number
  crypto_rub: number
  ratio_pct:  number | null
}

interface RatioResponse {
  days:    number
  points:  Point[]
  latest:  Point | null
  avg_pct: number | null
  baskets: { commodity: string[]; foreign: string[] }
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

/** '229.8 млрд ₽' — the two sides differ by three orders of magnitude. */
function fmtRub(v: number): string {
  if (v >= 1e12) return `${(v / 1e12).toFixed(2)} трлн ₽`
  if (v >= 1e9)  return `${(v / 1e9).toFixed(1)} млрд ₽`
  return `${(v / 1e6).toFixed(1)} млн ₽`
}

function RatioChart({ points }: { points: Point[] }) {
  const divRef = useRef<HTMLDivElement>(null)
  const theme  = useTheme()

  useEffect(() => {
    if (!divRef.current || !points.length) return
    const t = themeTokens(theme)

    const trace: Plotly.Data = {
      type: 'scatter',
      mode: 'lines+markers',
      name: 'MOEX / крипто',
      x: points.map(p => p.date_label),
      y: points.map(p => p.ratio_pct),
      line:   { color: LINE_COLOR, width: 2 },
      marker: { color: LINE_COLOR, size: 5 },
      hovertemplate: '%{y:.2f}%<extra></extra>',
    }

    const layout: Partial<Plotly.Layout> = {
      paper_bgcolor: t.paper,
      plot_bgcolor:  t.bg,
      margin: { l: 64, r: 16, t: 20, b: 64 },
      showlegend: false,
      xaxis: {
        tickangle: -40,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid, showgrid: false,
      },
      yaxis: {
        title: { text: 'MOEX / крипто (%)', font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        rangemode: 'tozero',
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        ticksuffix: '%', tickformat: ',.1f',
      },
      hoverlabel: {
        bgcolor: t.hover, bordercolor: t.hoverBorder,
        font: { color: t.hoverText, size: 12, family: FONT_FAMILY },
      },
      hovermode: 'x unified',
    }

    Plotly.react(divRef.current, [trace], layout, PLOTLY_CONFIG)
  }, [points, theme])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  return <div ref={divRef} style={{ width: '100%', height: 420 }} />
}

export function OKR() {
  const [data, setData]       = useState<RatioResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [lastSync, setSync]   = useState<Date | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      setData(await fetchJson<RatioResponse>(`${API}/ratio?days=${DAYS}`))
      setSync(new Date())
    } catch (e) {
      console.error('OKR: failed to load ratio', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const latest = data?.latest ?? null
  const avg    = data?.avg_pct ?? null
  // The KPI is the last COMPLETE day; the delta says where that day sits versus
  // the mean of the daily ratios drawn on the chart.
  const delta  = latest?.ratio_pct != null && avg != null ? latest.ratio_pct - avg : null

  const baskets = useMemo(() => data?.baskets ?? { commodity: [], foreign: [] }, [data])

  return (
    <div>
      <div className="page-toolbar">
        <h1>OKR</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Зеркальные контракты MOEX к TradFi криптобирж · ₽ · последние {DAYS} дней
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
            Числитель
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Оборот MOEX FORTS: зеркальные товарные контракты ({baskets.commodity.length} шт)
            + фьючерсы на иностранные ценные бумаги ({baskets.foreign.length} шт).
            Крипто-индексы MOEX и российские инструменты не входят.
          </div>
        </div>
        <div style={{ padding: '12px 16px 12px 20px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Знаменатель
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            TradFi шести криптобирж (Binance, OKX, Bybit, MEXC, Bitget, Hyperliquid):
            вся вселенная перпов на акции + сырьё, металлы и индексные ETF.
            Доллары переводятся в рубли по USDRUBF.
          </div>
        </div>
      </div>

      {loading ? (
        <p className="empty">Loading OKR data…</p>
      ) : !latest ? (
        <p className="empty">Нет данных: сбор MOEX-оборотов ещё не прошёл</p>
      ) : (
        <>
          <div className="card" style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 40, padding: '20px 24px', flexWrap: 'wrap' }}>
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 4 }}>
                за {latest.date_label}
              </div>
              <div style={{ fontSize: 44, fontWeight: 700, lineHeight: 1, color: LINE_COLOR }}>
                {latest.ratio_pct?.toFixed(2) ?? '—'} %
              </div>
              {delta != null && (
                <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 6 }}>
                  {delta >= 0 ? '▲ +' : '▼ '}{delta.toFixed(2)} п.п. к среднему за {DAYS} дней
                  {avg != null && ` (${avg.toFixed(2)} %)`}
                </div>
              )}
            </div>
            <div style={{ display: 'flex', gap: 32 }}>
              <div>
                <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 4 }}>
                  MOEX
                </div>
                <div style={{ fontSize: 20, fontWeight: 600, color: 'var(--text)' }}>{fmtRub(latest.moex_rub)}</div>
              </div>
              <div>
                <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 4 }}>
                  Криптобиржи
                </div>
                <div style={{ fontSize: 20, fontWeight: 600, color: 'var(--text)' }}>{fmtRub(latest.crypto_rub)}</div>
              </div>
            </div>
          </div>

          <div className="card">
            <RatioChart points={data!.points} />
          </div>
        </>
      )}
    </div>
  )
}
