import { useState, useEffect, useCallback } from 'react'
import { RefreshCw, TrendingUp, TrendingDown, Download } from 'lucide-react'
import { EXCHANGE_COLORS } from '../types'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/funding'

// ── Types ─────────────────────────────────────────────────────────────────────

interface RateEntry {
  symbol:                   string
  exchange:                 string
  rate:                     number
  predicted_rate:           number | null
  annualized_pct:           number
  predicted_annualized_pct: number | null
  next_funding_time:        string | null
  interval_hours:           number
  updated_at:               string
}

interface SpreadEntry {
  symbol:            string
  long_exchange:     string
  short_exchange:    string
  long_rate:         number
  long_rate_ann:     number
  short_rate:        number
  short_rate_ann:    number
  spread:            number
  annualized_pct:    number
  interval_hours:    number
  next_funding_time: string | null
  all_rates:         { exchange: string; rate: number; annualized_pct: number }[]
}

interface HistoryPoint {
  time:           string
  exchange:       string
  rate:           number
  annualized_pct: number
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtRate(r: number): string {
  return (r * 100).toFixed(4) + '%'
}
function fmtAnn(r: number): string {
  const sign = r >= 0 ? '+' : ''
  return sign + r.toFixed(2) + '% p.a.'
}
function nextFundingIn(iso: string | null): string {
  if (!iso) return '—'
  const diff = new Date(iso).getTime() - Date.now()
  if (diff < 0) return 'now'
  const h = Math.floor(diff / 3_600_000)
  const m = Math.floor((diff % 3_600_000) / 60_000)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}
function rateClass(r: number): string {
  if (r > 0.0002) return 'fr-pos-high'
  if (r > 0)      return 'fr-pos'
  if (r < -0.0002) return 'fr-neg-high'
  return 'fr-neg'
}

// ── Mini SVG line chart ────────────────────────────────────────────────────────

function SparkLine({ data, color }: { data: number[]; color: string }) {
  if (data.length < 2) return null
  const W = 200, H = 40, PAD = 2
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 0.00001
  const points = data.map((v, i) => {
    const x = PAD + (i / (data.length - 1)) * (W - PAD * 2)
    const y = PAD + (1 - (v - min) / range) * (H - PAD * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const zeroY = PAD + (1 - (0 - min) / range) * (H - PAD * 2)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: H, display: 'block' }}>
      {min < 0 && max >= 0 && (
        <line x1={PAD} y1={zeroY} x2={W - PAD} y2={zeroY}
          stroke="var(--border)" strokeWidth="0.8" strokeDasharray="3,3" />
      )}
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.5"
        strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

// ── History chart (full size) ──────────────────────────────────────────────────

function HistoryChart({ data }: { data: HistoryPoint[] }) {
  if (data.length < 2) return <p className="empty">No history data</p>

  // Group by exchange
  const byEx: Record<string, HistoryPoint[]> = {}
  data.forEach(d => { (byEx[d.exchange] = byEx[d.exchange] || []).push(d) })

  const allRates = data.map(d => d.rate)
  const minR = Math.min(...allRates)
  const maxR = Math.max(...allRates)
  const range = maxR - minR || 0.00001
  const allTs  = data.map(d => new Date(d.time).getTime())
  const minTs  = Math.min(...allTs)
  const maxTs  = Math.max(...allTs)
  const tsRange = maxTs - minTs || 1

  const W = 900, H = 200, PX = 8, PY = 8

  const toXY = (point: HistoryPoint) => {
    const x = PX + ((new Date(point.time).getTime() - minTs) / tsRange) * (W - PX * 2)
    const y = PY + (1 - (point.rate - minR) / range) * (H - PY * 2)
    return { x, y }
  }

  const zeroY = PY + (1 - (0 - minR) / range) * (H - PY * 2)

  return (
    <div className="fr-chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: H }}>
        {/* Zero line */}
        {minR < 0 && maxR >= 0 && (
          <line x1={PX} y1={zeroY} x2={W - PX} y2={zeroY}
            stroke="var(--border)" strokeWidth="1" strokeDasharray="4,4" />
        )}
        {Object.entries(byEx).map(([ex, pts]) => {
          const pathData = pts.map((p, i) => {
            const { x, y } = toXY(p)
            return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
          }).join(' ')
          return (
            <path key={ex} d={pathData} fill="none"
              stroke={(EXCHANGE_COLORS as Record<string, string>)[ex] ?? 'var(--accent)'}
              strokeWidth="1.5" strokeLinejoin="round" />
          )
        })}
      </svg>
      <div className="fr-chart-legend">
        {Object.keys(byEx).map(ex => (
          <span key={ex} className="fr-legend-item">
            <span className="fr-legend-dot"
              style={{ background: (EXCHANGE_COLORS as Record<string, string>)[ex] ?? 'var(--accent)' }} />
            {ex}
          </span>
        ))}
      </div>
    </div>
  )
}

// ── Rate matrix (all symbols × exchanges) ─────────────────────────────────────

function RatesMatrix({ rates }: { rates: RateEntry[] }) {
  const symbols   = [...new Set(rates.map(r => r.symbol))].sort()
  const exchanges = [...new Set(rates.map(r => r.exchange))].sort()
  const lookup: Record<string, RateEntry> = {}
  rates.forEach(r => { lookup[`${r.symbol}::${r.exchange}`] = r })

  return (
    <div className="fr-table-wrap">
      <table className="fr-table">
        <thead>
          <tr>
            <th>Symbol</th>
            {exchanges.map(ex => (
              <th key={ex}>
                <span className="fr-ex-dot" style={{ background: (EXCHANGE_COLORS as Record<string, string>)[ex] ?? 'var(--accent)' }} />
                {ex}
              </th>
            ))}
            <th>Next funding</th>
          </tr>
        </thead>
        <tbody>
          {symbols.map(sym => {
            const rowRates = exchanges.map(ex => lookup[`${sym}::${ex}`] ?? null)
            const nextTs   = rowRates.find(r => r?.next_funding_time)?.next_funding_time ?? null
            return (
              <tr key={sym}>
                <td className="fr-sym">{sym.replace('/USDT:USDT', '').replace('/USDT', '')}</td>
                {rowRates.map((r, i) => (
                  <td key={exchanges[i]} className={r ? rateClass(r.rate) : 'fr-na'}>
                    {r ? (
                      <>
                        <span className="fr-rate-main">{fmtRate(r.rate)}</span>
                        <span className="fr-rate-ann">{fmtAnn(r.annualized_pct)}</span>
                      </>
                    ) : '—'}
                  </td>
                ))}
                <td className="fr-next">{nextFundingIn(nextTs)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ── Spreads table ──────────────────────────────────────────────────────────────

function SpreadsTable({ spreads }: { spreads: SpreadEntry[] }) {
  if (!spreads.length) return <p className="empty">No spread opportunities found yet</p>
  return (
    <div className="fr-table-wrap">
      <table className="fr-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Symbol</th>
            <th>Long on</th>
            <th>Short on</th>
            <th>Long rate</th>
            <th>Short rate</th>
            <th>Spread</th>
            <th>Annual yield</th>
            <th>Next funding</th>
          </tr>
        </thead>
        <tbody>
          {spreads.map((s, i) => (
            <tr key={`${s.symbol}-${i}`}>
              <td className="fr-rank">{i + 1}</td>
              <td className="fr-sym">{s.symbol.replace('/USDT:USDT', '')}</td>
              <td>
                <span className="fr-ex-badge fr-ex-long">
                  <TrendingUp size={11} /> {s.long_exchange}
                </span>
              </td>
              <td>
                <span className="fr-ex-badge fr-ex-short">
                  <TrendingDown size={11} /> {s.short_exchange}
                </span>
              </td>
              <td className={rateClass(s.long_rate)}>
                <span className="fr-rate-main">{fmtRate(s.long_rate)}</span>
                <span className="fr-rate-ann">{fmtAnn(s.long_rate_ann)}</span>
              </td>
              <td className={rateClass(s.short_rate)}>
                <span className="fr-rate-main">{fmtRate(s.short_rate)}</span>
                <span className="fr-rate-ann">{fmtAnn(s.short_rate_ann)}</span>
              </td>
              <td className="fr-spread">
                <span className="fr-rate-main">{fmtRate(s.spread)}</span>
              </td>
              <td className="fr-ann-yield">
                <span className="fr-yield-badge">
                  {s.annualized_pct > 0 ? '+' : ''}{s.annualized_pct.toFixed(2)}%
                </span>
              </td>
              <td className="fr-next">{nextFundingIn(s.next_funding_time)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── History section ────────────────────────────────────────────────────────────

function HistorySection() {
  const [symbols,  setSymbols]  = useState<string[]>([])
  const [symbol,   setSymbol]   = useState('')
  const [exchange, setExchange] = useState('')
  const [days,     setDays]     = useState(90)
  const [data,     setData]     = useState<HistoryPoint[]>([])
  const [loading,  setLoading]  = useState(false)

  useEffect(() => {
    fetch(`${API}/symbols`)
      .then(r => r.json())
      .then(d => {
        const syms = d.symbols ?? []
        setSymbols(syms)
        if (syms.length) setSymbol(syms[0])
      })
      .catch(() => {})
  }, [])

  const load = useCallback(async () => {
    if (!symbol) return
    setLoading(true)
    try {
      const params = new URLSearchParams({ symbol, days: String(days) })
      if (exchange) params.set('exchange', exchange)
      const d = await fetch(`${API}/history?${params}`).then(r => r.json())
      setData(d.rates ?? [])
    } catch { /* keep existing */ }
    finally { setLoading(false) }
  }, [symbol, exchange, days])

  useEffect(() => { load() }, [load])

  const downloadCsv = () => {
    const header = 'time,exchange,rate,annualized_pct'
    const rows   = data.map(d =>
      `${d.time},${d.exchange},${d.rate},${d.annualized_pct}`
    )
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv' })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href = url
    a.download = `funding_${symbol.replace('/', '_')}_${days}d.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const exchanges = [...new Set(data.map(d => d.exchange))].sort()

  return (
    <div>
      <div className="fr-history-controls">
        <select value={symbol} onChange={e => setSymbol(e.target.value)}>
          {symbols.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={exchange} onChange={e => setExchange(e.target.value)}>
          <option value="">All exchanges</option>
          {exchanges.map(ex => <option key={ex} value={ex}>{ex}</option>)}
        </select>
        <select value={days} onChange={e => setDays(Number(e.target.value))}>
          {[7, 30, 90, 180, 365, 730].map(d => (
            <option key={d} value={d}>{d} days</option>
          ))}
        </select>
        <button className="btn-secondary" onClick={downloadCsv} disabled={!data.length}
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <Download size={13} /> CSV
        </button>
      </div>

      {loading
        ? <p className="empty">Loading history…</p>
        : (
          <>
            <HistoryChart data={data} />
            <div style={{ marginTop: 12, fontSize: 12, color: 'var(--muted)' }}>
              {data.length} records · {symbol} · {days}d lookback
            </div>
          </>
        )
      }
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

type Tab = 'spreads' | 'matrix' | 'history'

export function Funding() {
  const [tab,      setTab]     = useState<Tab>('spreads')
  const [rates,    setRates]   = useState<RateEntry[]>([])
  const [spreads,  setSpreads] = useState<SpreadEntry[]>([])
  const [loading,  setLoading] = useState(true)
  const [lastSync, setLastSync] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [rRes, sRes] = await Promise.all([
        fetch(`${API}/current`).then(r => r.json()),
        fetch(`${API}/spreads`).then(r => r.json()),
      ])
      setRates(rRes.rates ?? [])
      setSpreads(sRes.spreads ?? [])
      setLastSync(new Date())
    } catch { /* keep existing on transient error */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  // Auto-refresh every 5 minutes
  useEffect(() => {
    const id = setInterval(() => load(), 5 * 60_000)
    return () => clearInterval(id)
  }, [load])

  const topSpread = spreads[0]

  return (
    <div>
      {/* ── Toolbar ── */}
      <div className="page-toolbar">
        <h1>Funding Rates</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          {lastSync && `Updated ${lastSync.toLocaleTimeString()}`}
        </div>
        <button className="btn-secondary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={() => load()} disabled={loading}>
          <RefreshCw size={13} className={loading ? 'spin' : ''} />
          Refresh
        </button>
      </div>

      {/* ── KPI strip ── */}
      {!loading && topSpread && (
        <div className="fr-kpi-strip">
          <div className="fr-kpi">
            <span className="fr-kpi-label">Best spread</span>
            <span className="fr-kpi-value fr-yield-hi">
              +{topSpread.annualized_pct.toFixed(2)}% p.a.
            </span>
            <span className="fr-kpi-sub">
              {topSpread.symbol.replace('/USDT:USDT', '')} — long {topSpread.long_exchange} / short {topSpread.short_exchange}
            </span>
          </div>
          <div className="fr-kpi">
            <span className="fr-kpi-label">Symbols tracked</span>
            <span className="fr-kpi-value">{[...new Set(rates.map(r => r.symbol))].length}</span>
          </div>
          <div className="fr-kpi">
            <span className="fr-kpi-label">Active opportunities</span>
            <span className="fr-kpi-value">
              {spreads.filter(s => s.annualized_pct >= 10).length}
            </span>
            <span className="fr-kpi-sub">≥ 10% p.a.</span>
          </div>
          <div className="fr-kpi">
            <span className="fr-kpi-label">Next funding in</span>
            <span className="fr-kpi-value">
              {nextFundingIn(topSpread.next_funding_time)}
            </span>
          </div>
        </div>
      )}

      {/* ── Tabs ── */}
      <div className="fr-tabs">
        {(['spreads', 'matrix', 'history'] as Tab[]).map(t => (
          <button key={t}
            className={`fr-tab ${tab === t ? 'fr-tab--active' : ''}`}
            onClick={() => setTab(t)}>
            {t === 'spreads' ? 'Opportunities' : t === 'matrix' ? 'All Rates' : 'History'}
          </button>
        ))}
      </div>

      {/* ── Content ── */}
      {loading ? (
        <p className="empty">Loading funding rates…</p>
      ) : (
        <>
          {tab === 'spreads'  && <SpreadsTable spreads={spreads} />}
          {tab === 'matrix'   && <RatesMatrix rates={rates} />}
          {tab === 'history'  && <HistorySection />}
        </>
      )}
    </div>
  )
}
