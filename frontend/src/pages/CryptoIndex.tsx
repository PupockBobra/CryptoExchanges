import { useState, useEffect } from 'react'
import { Pause, Play, Download, Database } from 'lucide-react'
import { EXCHANGE_COLORS } from '../types'
import type { Exchange } from '../types'
import { fetchJson } from '../utils/api'
import { SectionHeading } from '../components/SectionHeading'

// Standalone replicator service (systemd on the host), proxied by nginx under
// /crypto-index/ — NOT part of the tracker backend.  Contract: handoff API.md.
const API = (import.meta.env.VITE_API_URL ?? '') + '/crypto-index'

const POLL_MS = 15_000   // the collector recomputes the index every 15 s

// The four venues of the index — a subset of the tracker's exchanges, and the
// key names used both in the API payload and in EXCHANGE_COLORS.
type IndexExchange = Extract<Exchange, 'binance' | 'bybit' | 'okx' | 'bitget'>
const INDEX_EXCHANGES: IndexExchange[] = ['binance', 'bybit', 'okx', 'bitget']

interface CoinRow {
  coin:        string
  index:       number | null
  n_exchanges: number
  binance:     number | null
  bybit:       number | null
  okx:         number | null
  bitget:      number | null
  min_samples: number
  missing_now: string[]
  flag:        string
}

interface Latest {
  ts:      number | null
  iso:     string | null
  weights: Record<string, number>
  coins:   CoinRow[]
}

interface Stats {
  ticks_rows: number
  index_rows: number
  first_iso:  string | null
  last_ts:    number | null
  last_iso:   string | null
  poll_sec:   number
  window_sec: number
}

/** Prices span 4 orders of magnitude (BTC ~64k, TRX ~0.3) — scale the decimals. */
const fmtPrice = (v: number | null | undefined) =>
  v == null ? '—' : Number(v).toLocaleString('en-US', {
    minimumFractionDigits: v < 5 ? 4 : 2,
    maximumFractionDigits: v < 5 ? 4 : 2,
  })

/** Quality of a tick — see flag legend in the service's API.md. */
function flagBadge(flag: string, missing: string[]) {
  if (flag === 'ok') return <span className="badge badge--on">ok</span>
  const cls = flag === 'PARTIAL_WINDOW' ? 'badge--perp' : 'badge--disc'
  const suffix = missing.length ? `: ${missing.join(',')}` : ''
  return <span className={`badge ${cls}`}>{flag}{suffix}</span>
}

function CoinCard({ row, weights }: { row: CoinRow; weights: Record<string, number> }) {
  return (
    <div className="ex-card">
      <div className="ex-card-header">
        <span className="ex-name">{row.coin}/USDT</span>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>
          {row.n_exchanges}/{INDEX_EXCHANGES.length} бирж
        </span>
        <span style={{ marginLeft: 'auto' }}>{flagBadge(row.flag, row.missing_now)}</span>
      </div>

      <div className="ex-stat">
        <span className="ex-stat-label">Индекс</span>
        <span className="ex-stat-value" style={{ fontSize: 26 }}>{fmtPrice(row.index)}</span>
      </div>

      {/* Per-exchange minute averages that feed the weighted composite */}
      <div>
        {INDEX_EXCHANGES.map((ex) => (
          <div
            key={ex}
            style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
              fontSize: 12.5, padding: '3px 0', fontVariantNumeric: 'tabular-nums',
              opacity: row.missing_now.includes(ex) ? .45 : 1,
            }}
          >
            <span style={{ color: EXCHANGE_COLORS[ex], fontWeight: 600 }}>
              {ex}
              <span style={{ color: 'var(--muted)', fontWeight: 400, marginLeft: 5 }}>
                {((weights[ex] ?? 0) * 100).toFixed(0)}%
              </span>
            </span>
            <span>{fmtPrice(row[ex])}</span>
          </div>
        ))}
      </div>

      <div className="ex-footer">
        <div className="ex-footer-row">
          <span>Сэмплов в окне</span>
          <span>{row.min_samples}/4</span>
        </div>
      </div>
    </div>
  )
}

export function CryptoIndex() {
  const [latest, setLatest] = useState<Latest | null>(null)
  const [stats,  setStats]  = useState<Stats | null>(null)
  const [live,   setLive]   = useState(true)
  const [error,  setError]  = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // CSV export controls
  const [coin, setCoin] = useState('')
  const [from, setFrom] = useState('')
  const [to,   setTo]   = useState('')

  const load = async () => {
    try {
      const [l, s] = await Promise.all([
        fetchJson<Latest>(`${API}/api/latest`, { cache: 'no-store' }),
        fetchJson<Stats>(`${API}/api/stats`,  { cache: 'no-store' }),
      ])
      setLatest(l); setStats(s); setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    if (!live) return
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [live])

  // datetime-local is entered as UTC per the labels → append Z before parsing
  const toUnix = (v: string) => (v ? Math.floor(new Date(v + 'Z').getTime() / 1000) : '')
  const csvUrl = (path: string) => {
    const p = new URLSearchParams()
    if (coin) p.set('coin', coin)
    const f = toUnix(from); if (f) p.set('from', String(f))
    const t = toUnix(to);   if (t) p.set('to',   String(t))
    const q = p.toString()
    return `${API}${path}${q ? '?' + q : ''}`
  }

  // Freshness: the collector writes a tick every 15 s; >45 s behind = stalled.
  const lag = stats?.last_ts ? Math.floor(Date.now() / 1000) - stats.last_ts : null
  const stale = lag != null && lag > 45

  return (
    <div>
      <div className="page-toolbar">
        <h1>Crypto Index</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Реплика методики MOEX · Binance 50% · Bybit 20% · OKX 15% · Bitget 15% ·
          шаг {stats?.poll_sec ?? 15}с · окно {stats?.window_sec ?? 60}с
          {latest?.iso && ` · обновлено ${latest.iso}`}
        </div>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--muted)' }}>
          <span style={{
            width: 8, height: 8, borderRadius: '50%',
            background: live && !stale && !error ? '#22c55e' : 'var(--muted)',
            boxShadow: live && !stale && !error ? '0 0 0 3px rgba(34,197,94,.18)' : 'none',
          }} />
          {error ? 'Ошибка' : stale ? 'Отстаёт' : live ? 'Live' : 'Paused'}
        </span>
        <button
          className="btn-secondary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={() => setLive(v => !v)}
        >
          {live ? <Pause size={13} /> : <Play size={13} />}
          {live ? 'Pause' : 'Resume'}
        </button>
      </div>

      {error && (
        <div className="card" style={{ marginBottom: 16, borderColor: 'var(--red)' }}>
          <div className="card-title" style={{ color: 'var(--red)' }}>Сервис криптоиндекса недоступен</div>
          <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>{error}</div>
        </div>
      )}

      {/* Coverage summary */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap' }}>
          <div className="ex-stat">
            <span className="ex-stat-label">Сырых цен</span>
            <span className="ex-stat-value">{(stats?.ticks_rows ?? 0).toLocaleString()}</span>
          </div>
          <div className="ex-stat">
            <span className="ex-stat-label">Значений индекса</span>
            <span className="ex-stat-value">{(stats?.index_rows ?? 0).toLocaleString()}</span>
          </div>
          <div className="ex-stat">
            <span className="ex-stat-label">История с</span>
            <span className="ex-stat-value" style={{ fontSize: 15 }}>{stats?.first_iso ?? '—'}</span>
          </div>
          <div className="ex-stat">
            <span className="ex-stat-label">Задержка</span>
            <span className={`ex-stat-value ${!stale && lag != null ? 'green' : ''}`} style={{ fontSize: 15 }}>
              {lag == null ? '—' : `${lag}с`}
            </span>
          </div>
        </div>
      </div>

      {loading ? (
        <p className="empty">Загрузка…</p>
      ) : (
        <div className="exchanges-grid">
          {(latest?.coins ?? []).map((c) => (
            <CoinCard key={c.coin} row={c} weights={latest!.weights} />
          ))}
        </div>
      )}

      {/* ── CSV / DB export ─────────────────────────────────────────────── */}
      <SectionHeading label="Выгрузка данных" />
      <div className="card">
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="ex-stat-label">Монета</span>
            <select className="btn-secondary" value={coin} onChange={(e) => setCoin(e.target.value)}>
              <option value="">все</option>
              {(latest?.coins ?? []).map((c) => <option key={c.coin}>{c.coin}</option>)}
            </select>
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="ex-stat-label">С (UTC)</span>
            <input className="btn-secondary" type="datetime-local" value={from}
                   onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="ex-stat-label">По (UTC)</span>
            <input className="btn-secondary" type="datetime-local" value={to}
                   onChange={(e) => setTo(e.target.value)} />
          </label>
          <a className="btn-primary" href={csvUrl('/api/index.csv')}
             style={{ display: 'inline-flex', alignItems: 'center', gap: 6, textDecoration: 'none' }}>
            <Download size={13} /> Индекс + средние (CSV)
          </a>
          <a className="btn-secondary" href={csvUrl('/api/ticks.csv')}
             style={{ display: 'inline-flex', alignItems: 'center', gap: 6, textDecoration: 'none' }}>
            <Download size={13} /> Сырые цены 15с (CSV)
          </a>
          <a className="btn-secondary" href={`${API}/api/db`}
             style={{ display: 'inline-flex', alignItems: 'center', gap: 6, textDecoration: 'none' }}>
            <Database size={13} /> Снимок базы (.db)
          </a>
        </div>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 12 }}>
          Без указания дат: индекс — за сутки, сырые цены — за час. Колонка <code>flag</code> —
          контроль качества такта (<code>ok</code> = все 4 биржи и полное окно;{' '}
          <code>PARTIAL_WINDOW</code> — окно ещё набирается; <code>STALE</code> /{' '}
          <code>MISSING_EXCHANGE</code> — биржа не ответила, веса перенормированы).
          Для строгой сверки с методикой берите строки с <code>flag = ok</code>.
        </div>
      </div>
    </div>
  )
}
