import { Fragment, useState, useEffect, useCallback, useMemo } from 'react'
import { RefreshCw, Download, Flame } from 'lucide-react'
import { EXCHANGE_COLORS, SYMBOL_SECTIONS, classifySymbol, formatSymbol } from '../types'
import type { SymbolSection } from '../types'

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

// ── Funding heatmap (instruments × dates) ─────────────────────────────────────
//
// Same layout as the SPB funding page: one row per instrument, one column per
// day, colour by value.  Crypto funding settles several times a day on several
// venues, so a cell is either one venue's day or the average across venues.

interface HeatRow {
  date:     string
  symbol:   string
  exchange: string
  pct_day:  number
  pct_year: number | null
}

type HeatMetric = 'pct_day' | 'pct_year'

/** 90th percentile of |values| — a couple of outliers must not wash out the scale. */
function clipScale(vals: number[]): number {
  const abs = vals.filter(v => Number.isFinite(v)).map(Math.abs).sort((a, b) => a - b)
  if (!abs.length) return 1
  const p = abs[Math.min(abs.length - 1, Math.floor(abs.length * 0.9))]
  return p > 0 ? p : (abs[abs.length - 1] || 1)
}

/** Green = longs get paid (negative funding), red = longs pay. */
function heatBg(v: number | null, scale: number): string {
  if (v == null) return 'transparent'
  const t = Math.max(-1, Math.min(1, v / scale))
  const a = (Math.abs(t) * 0.5).toFixed(3)
  return t >= 0 ? `rgba(239,68,68,${a})` : `rgba(16,185,129,${a})`
}

function heatDateLabel(iso: string): string {
  const [, m, d] = iso.split('-')
  return `${d}.${m}`
}

function FundingHeatmap() {
  const [rows,     setRows]     = useState<HeatRow[]>([])
  const [loading,  setLoading]  = useState(true)
  const [days,     setDays]     = useState(30)
  const [metric,   setMetric]   = useState<HeatMetric>('pct_day')
  const [exchange, setExchange] = useState('')      // '' = average across venues
  const [heatmap,  setHeatmap]  = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetch(`${API}/heatmap?days=${days}`)
      .then(r => r.json())
      .then(d => { if (!cancelled) setRows(d.rows ?? []) })
      .catch(() => { /* keep the previous grid on a transient error */ })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [days])

  const exchanges = useMemo(
    () => [...new Set(rows.map(r => r.exchange))].sort(),
    [rows],
  )
  const dates = useMemo(
    () => [...new Set(rows.map(r => r.date))].sort(),
    [rows],
  )

  // (symbol,date) → value: one venue, or the mean across the venues that settled.
  const cell = useMemo(() => {
    const acc = new Map<string, { sum: number; n: number }>()
    for (const r of rows) {
      if (exchange && r.exchange !== exchange) continue
      const v = metric === 'pct_day' ? r.pct_day : r.pct_year
      if (v == null) continue
      const key = `${r.symbol}|${r.date}`
      const cur = acc.get(key)
      if (cur) { cur.sum += v; cur.n += 1 } else { acc.set(key, { sum: v, n: 1 }) }
    }
    const out = new Map<string, number>()
    for (const [k, { sum, n }] of acc) out.set(k, sum / n)
    return out
  }, [rows, metric, exchange])

  const symbols = useMemo(() => {
    const seen = new Set<string>()
    for (const r of rows) if (!exchange || r.exchange === exchange) seen.add(r.symbol)
    return [...seen].sort()
  }, [rows, exchange])

  const scale = useMemo(() => clipScale([...cell.values()]), [cell])

  const fmtCell = (v: number) => metric === 'pct_day' ? v.toFixed(4) : v.toFixed(1)

  const exportCsv = () => {
    const head = ['symbol', 'section', ...dates.map(heatDateLabel)]
    const lines = [head.join(',')]
    for (const sym of symbols) {
      const vals = dates.map(d => { const v = cell.get(`${sym}|${d}`); return v == null ? '' : String(v) })
      lines.push([sym, classifySymbol(sym), ...vals].join(','))
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `funding-heatmap-${metric}-${days}d.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const hasData = symbols.length > 0 && dates.length > 0

  return (
    <div>
      <div className="card" style={{ marginBottom: 16, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 16, padding: '12px 16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)' }}>
            Метрика
          </span>
          <div style={{ display: 'flex', gap: 4 }}>
            {([['pct_day', '% day'], ['pct_year', '% year']] as [HeatMetric, string][]).map(([key, label]) => (
              <button key={key} onClick={() => setMetric(key)} className="btn-secondary"
                style={{
                  padding: '5px 10px', fontSize: 12,
                  background: metric === key ? '#00a3c4' : undefined,
                  color: metric === key ? '#fff' : undefined,
                  borderColor: metric === key ? '#00a3c4' : undefined,
                }}>
                {label}
              </button>
            ))}
          </div>
        </div>

        <select value={exchange} onChange={e => setExchange(e.target.value)}
          style={{ padding: '5px 8px', fontSize: 12 }}>
          <option value="">Все биржи (среднее)</option>
          {exchanges.map(ex => <option key={ex} value={ex}>{ex}</option>)}
        </select>

        <select value={days} onChange={e => setDays(Number(e.target.value))}
          style={{ padding: '5px 8px', fontSize: 12 }}>
          {[14, 30, 60, 90].map(d => <option key={d} value={d}>{d} дней</option>)}
        </select>

        <button className="btn-secondary" onClick={() => setHeatmap(h => !h)}
          style={{
            display: 'flex', alignItems: 'center', gap: 6, padding: '5px 10px', fontSize: 12,
            background: heatmap ? '#00a3c4' : undefined,
            color: heatmap ? '#fff' : undefined,
            borderColor: heatmap ? '#00a3c4' : undefined,
          }}>
          <Flame size={13} /> Heatmap
        </button>

        <div style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--muted)' }}>
          Красный — лонги платят, зелёный — лонгам платят
        </div>

        <button className="btn-secondary" onClick={exportCsv} disabled={!hasData}
          style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 10px', fontSize: 12 }}>
          <Download size={13} /> Export CSV
        </button>
      </div>

      {loading ? (
        <p className="empty">Loading funding heatmap…</p>
      ) : !hasData ? (
        <p className="empty">Нет данных фандинга за выбранный период</p>
      ) : (
        <div className="funding-wrap">
          <table className="funding-table">
            <thead>
              <tr>
                <th className="tk">Инструмент</th>
                {dates.map(d => <th key={d}>{heatDateLabel(d)}</th>)}
              </tr>
            </thead>
            <tbody>
              {SYMBOL_SECTIONS.map(({ label }) => {
                const sectionSyms = symbols.filter(s => classifySymbol(s) === (label as SymbolSection))
                if (!sectionSyms.length) return null
                return (
                  <Fragment key={label}>
                    <tr className="funding-group">
                      <td className="tk">{label}</td>
                      <td colSpan={dates.length} />
                    </tr>
                    {sectionSyms.map(sym => (
                      <tr key={sym}>
                        <td className="tk">{formatSymbol(sym)}</td>
                        {dates.map(d => {
                          const v = cell.get(`${sym}|${d}`) ?? null
                          return (
                            <td key={d}
                              className={v == null ? 'empty' : undefined}
                              style={{ background: heatmap ? heatBg(v, scale) : 'transparent' }}>
                              {v == null ? '·' : fmtCell(v)}
                            </td>
                          )
                        })}
                      </tr>
                    ))}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
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

type Tab = 'heatmap' | 'matrix' | 'history'

export function Funding() {
  const [tab,      setTab]     = useState<Tab>('heatmap')
  const [rates,    setRates]   = useState<RateEntry[]>([])
  const [loading,  setLoading] = useState(true)
  const [lastSync, setLastSync] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const rRes = await fetch(`${API}/current`).then(r => r.json())
      setRates(rRes.rates ?? [])
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

  // Extremes drive the KPI strip now that entry opportunities are gone.
  const extremes = useMemo(() => {
    let max = rates[0], min = rates[0]
    for (const r of rates) {
      if (r.annualized_pct > max.annualized_pct) max = r
      if (r.annualized_pct < min.annualized_pct) min = r
    }
    return { max, min }
  }, [rates])

  const nextTs = useMemo(
    () => rates.map(r => r.next_funding_time).filter(Boolean).sort()[0] ?? null,
    [rates],
  )

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
      {!loading && rates.length > 0 && (
        <div className="fr-kpi-strip">
          <div className="fr-kpi">
            <span className="fr-kpi-label">Highest funding</span>
            <span className="fr-kpi-value fr-yield-hi">{fmtAnn(extremes.max.annualized_pct)}</span>
            <span className="fr-kpi-sub">
              {extremes.max.symbol.replace('/USDT:USDT', '')} — {extremes.max.exchange}
            </span>
          </div>
          <div className="fr-kpi">
            <span className="fr-kpi-label">Lowest funding</span>
            <span className="fr-kpi-value">{fmtAnn(extremes.min.annualized_pct)}</span>
            <span className="fr-kpi-sub">
              {extremes.min.symbol.replace('/USDT:USDT', '')} — {extremes.min.exchange}
            </span>
          </div>
          <div className="fr-kpi">
            <span className="fr-kpi-label">Symbols tracked</span>
            <span className="fr-kpi-value">{[...new Set(rates.map(r => r.symbol))].length}</span>
            <span className="fr-kpi-sub">
              {[...new Set(rates.map(r => r.exchange))].length} бирж
            </span>
          </div>
          <div className="fr-kpi">
            <span className="fr-kpi-label">Next funding in</span>
            <span className="fr-kpi-value">{nextFundingIn(nextTs)}</span>
          </div>
        </div>
      )}

      {/* ── Tabs ── */}
      <div className="fr-tabs">
        {(['heatmap', 'matrix', 'history'] as Tab[]).map(t => (
          <button key={t}
            className={`fr-tab ${tab === t ? 'fr-tab--active' : ''}`}
            onClick={() => setTab(t)}>
            {t === 'heatmap' ? 'Heatmap' : t === 'matrix' ? 'All Rates' : 'History'}
          </button>
        ))}
      </div>

      {/* ── Content ── */}
      {loading ? (
        <p className="empty">Loading funding rates…</p>
      ) : (
        <>
          {tab === 'heatmap'  && <FundingHeatmap />}
          {tab === 'matrix'   && <RatesMatrix rates={rates} />}
          {tab === 'history'  && <HistorySection />}
        </>
      )}
    </div>
  )
}
