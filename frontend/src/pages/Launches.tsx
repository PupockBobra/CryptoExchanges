import { useEffect, useState } from 'react'
import { RefreshCw, Rocket } from 'lucide-react'
import { EXCHANGE_COLORS } from '../types'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/launches'

interface LaunchRow {
  symbol:    string
  base:      string
  exchange:  string
  listed_at: string | null  // 'YYYY-MM-DD' or null
}

interface GroupedInstrument {
  base:         string
  newest_date:  string | null
  exchanges:    { exchange: string; symbol: string; listed_at: string | null }[]
}

const EXCHANGE_LABEL: Record<string, string> = {
  binance:     'Binance',
  okx:         'OKX',
  mexc:        'MEXC',
  hyperliquid: 'Hyperliquid',
}

function daysAgo(dateStr: string | null): string {
  if (!dateStr) return '—'
  const diff = Math.floor((Date.now() - new Date(dateStr).getTime()) / 86400000)
  if (diff === 0) return 'today'
  if (diff === 1) return '1 day ago'
  if (diff < 30) return `${diff} days ago`
  if (diff < 365) return `${Math.floor(diff / 30)}mo ago`
  return `${Math.floor(diff / 365)}y ago`
}

function isNew(dateStr: string | null): boolean {
  if (!dateStr) return false
  return (Date.now() - new Date(dateStr).getTime()) < 30 * 86400000
}

function groupRows(rows: LaunchRow[]): GroupedInstrument[] {
  const map = new Map<string, GroupedInstrument>()
  for (const r of rows) {
    if (!map.has(r.base)) {
      map.set(r.base, { base: r.base, newest_date: null, exchanges: [] })
    }
    const g = map.get(r.base)!
    g.exchanges.push({ exchange: r.exchange, symbol: r.symbol, listed_at: r.listed_at })
    if (r.listed_at && (g.newest_date === null || r.listed_at > g.newest_date)) {
      g.newest_date = r.listed_at
    }
  }
  return Array.from(map.values()).sort((a, b) => {
    if (a.newest_date === b.newest_date) return a.base.localeCompare(b.base)
    if (a.newest_date === null) return 1
    if (b.newest_date === null) return -1
    return b.newest_date.localeCompare(a.newest_date)
  })
}

export function Launches() {
  const [rows,    setRows]    = useState<LaunchRow[]>([])
  const [loading, setLoading] = useState(false)
  const [loaded,  setLoaded]  = useState(false)
  const [error,   setError]   = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const data: LaunchRow[] = await fetch(API).then((r) => {
        if (!r.ok) throw new Error(r.statusText)
        return r.json()
      })
      setRows(data)
      setLoaded(true)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const groups = groupRows(rows)

  return (
    <div>
      {/* Toolbar */}
      <div className="page-toolbar">
        <h1>Futures Launches</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Non-crypto perpetual swaps · {loaded ? `${groups.length} instruments across ${new Set(rows.map(r => r.exchange)).size} exchanges` : ''}
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

      {/* States */}
      {loading && <p className="empty">Scanning exchanges…</p>}
      {error && <p className="empty" style={{ color: 'var(--red)' }}>{error}</p>}

      {/* Table */}
      {!loading && loaded && (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                {['Instrument', 'Exchange', 'Symbol (exchange-specific)', 'Listed', 'Age'].map(h => (
                  <th key={h} style={{
                    padding: '10px 14px', textAlign: 'left', fontWeight: 600,
                    fontSize: 11, textTransform: 'uppercase', letterSpacing: '.05em',
                    color: 'var(--muted)', whiteSpace: 'nowrap',
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {groups.map((g) =>
                g.exchanges.map((ex, i) => (
                  <tr
                    key={`${g.base}-${ex.exchange}`}
                    style={{
                      borderBottom: '1px solid var(--border)',
                      background: i % 2 === 0 ? 'transparent' : 'var(--bg-alt, rgba(255,255,255,0.01))',
                    }}
                  >
                    {/* Instrument name — only on first row of group */}
                    <td style={{ padding: '9px 14px', fontWeight: 600, color: 'var(--title)' }}>
                      {i === 0 ? (
                        <span style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                          {g.base}
                          {isNew(g.newest_date) && (
                            <span style={{
                              background: '#10b981', color: '#fff',
                              fontSize: 10, fontWeight: 700, borderRadius: 4,
                              padding: '1px 5px', letterSpacing: '.04em',
                              display: 'flex', alignItems: 'center', gap: 3,
                            }}>
                              <Rocket size={9} /> NEW
                            </span>
                          )}
                        </span>
                      ) : null}
                    </td>

                    {/* Exchange badge */}
                    <td style={{ padding: '9px 14px' }}>
                      <span style={{
                        display: 'inline-flex', alignItems: 'center', gap: 5,
                        fontSize: 12, fontWeight: 500,
                      }}>
                        <span style={{
                          width: 8, height: 8, borderRadius: 2,
                          background: (EXCHANGE_COLORS as Record<string, string>)[ex.exchange] ?? '#888',
                          display: 'inline-block', flexShrink: 0,
                        }} />
                        {EXCHANGE_LABEL[ex.exchange] ?? ex.exchange}
                      </span>
                    </td>

                    {/* Exchange-specific symbol */}
                    <td style={{ padding: '9px 14px', color: 'var(--muted)', fontFamily: 'monospace', fontSize: 12 }}>
                      {ex.symbol}
                    </td>

                    {/* Date */}
                    <td style={{ padding: '9px 14px', whiteSpace: 'nowrap' }}>
                      {ex.listed_at ?? <span style={{ color: 'var(--muted)' }}>—</span>}
                    </td>

                    {/* Age */}
                    <td style={{ padding: '9px 14px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                      {daysAgo(ex.listed_at)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
