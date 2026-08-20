import { useState, useEffect } from 'react'
import { Wifi, WifiOff, RefreshCw } from 'lucide-react'
import { EXCHANGES, EXCHANGE_COLORS, fmtBytes } from '../types'
import type { ExchangeStats, Exchange } from '../types'
import { timeAgo } from '../utils/format'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/exchanges/'

function uptime(ts: string | null): string {
  if (!ts) return '—'
  const diff = Math.floor((Date.now() - new Date(ts).getTime()) / 1000)
  const h = Math.floor(diff / 3600)
  const m = Math.floor((diff % 3600) / 60)
  const s = diff % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

function fmtRate(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

interface ExCardProps {
  ex:    Exchange
  stats: ExchangeStats | undefined
}

function ExchangeCard({ ex, stats }: ExCardProps) {
  const color  = EXCHANGE_COLORS[ex]
  const status = stats?.status ?? 'unknown'

  return (
    <div className="ex-card" style={{ borderColor: status === 'connected' ? color + '40' : undefined }}>
      {/* Header */}
      <div className="ex-card-header">
        <span className={`ex-dot ${status}`} />
        <span className="ex-name" style={{ color }}>{ex}</span>
        <span className={`badge badge--${status === 'connected' ? 'conn' : status === 'disconnected' ? 'disc' : 'unk'}`}>
          {status === 'connected' ? <><Wifi size={10} /> live</> : status === 'connecting' ? 'connecting…' : status === 'disconnected' ? <><WifiOff size={10} /> offline</> : 'unknown'}
        </span>
      </div>

      {/* Key metrics */}
      <div className="ex-stats-grid">
        <div className="ex-stat">
          <span className="ex-stat-label">Ticks / min</span>
          <span className={`ex-stat-value ${status === 'connected' ? 'green' : ''}`}>
            {fmtRate(stats?.ticks_1m ?? 0)}
          </span>
        </div>
        <div className="ex-stat">
          <span className="ex-stat-label">Total ticks</span>
          <span className="ex-stat-value">{(stats?.ticks_total ?? 0).toLocaleString()}</span>
        </div>
        <div className="ex-stat">
          <span className="ex-stat-label">Data received</span>
          <span className="ex-stat-value" style={{ fontSize: 15 }}>{fmtBytes(stats?.bytes_in ?? 0)}</span>
        </div>
        <div className="ex-stat">
          <span className="ex-stat-label">Reconnects</span>
          <span className="ex-stat-value" style={{ fontSize: 15, color: (stats?.reconnects ?? 0) > 0 ? 'var(--yellow)' : undefined }}>
            {stats?.reconnects ?? 0}
          </span>
        </div>
      </div>

      {/* Active symbols */}
      {(stats?.symbols_active?.length ?? 0) > 0 && (
        <div>
          <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '.04em', marginBottom: 6 }}>
            Active symbols
          </div>
          <div className="ex-symbols">
            {stats!.symbols_active.map((s) => (
              <span key={s} className="ex-sym-chip">{s}</span>
            ))}
          </div>
        </div>
      )}

      {/* Footer timestamps */}
      <div className="ex-footer">
        <div className="ex-footer-row">
          <span>Last tick</span>
          <span>{timeAgo(stats?.last_tick_ts ?? null)}</span>
        </div>
        <div className="ex-footer-row">
          <span>Uptime</span>
          <span>{uptime(stats?.started_at ?? null)}</span>
        </div>
        <div className="ex-footer-row">
          <span>Stats updated</span>
          <span>{timeAgo(stats?.updated_at ?? null)}</span>
        </div>
      </div>
    </div>
  )
}

export function Exchanges() {
  const [stats,    setStats]    = useState<Record<string, ExchangeStats>>({})
  const [loading,  setLoading]  = useState(true)
  const [lastFetch, setLastFetch] = useState<Date | null>(null)

  const load = async () => {
    try {
      const r = await fetch(API)
      if (r.ok) setStats(await r.json())
      setLastFetch(new Date())
    } catch (e) {
      console.error('Exchanges: failed to load stats', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [])

  const totalTicks  = Object.values(stats).reduce((s, e) => s + (e.ticks_total ?? 0), 0)
  const totalBytes  = Object.values(stats).reduce((s, e) => s + (e.bytes_in   ?? 0), 0)
  const connCount   = Object.values(stats).filter((e) => e.status === 'connected').length

  return (
    <div>
      <div className="page-toolbar">
        <h1>Exchanges</h1>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginLeft: 'auto' }}>
          {lastFetch && (
            <span style={{ fontSize: 12, color: 'var(--muted)' }}>
              Updated {timeAgo(lastFetch.toISOString())}
            </span>
          )}
          <button
            className="btn-secondary"
            style={{ display: 'flex', alignItems: 'center', gap: 6 }}
            onClick={load}
          >
            <RefreshCw size={13} /> Refresh
          </button>
        </div>
      </div>

      {/* Summary bar */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap' }}>
          <div className="ex-stat">
            <span className="ex-stat-label">Connected</span>
            <span className="ex-stat-value green">{connCount} / {EXCHANGES.length}</span>
          </div>
          <div className="ex-stat">
            <span className="ex-stat-label">Total ticks (session)</span>
            <span className="ex-stat-value">{totalTicks.toLocaleString()}</span>
          </div>
          <div className="ex-stat">
            <span className="ex-stat-label">Total data received</span>
            <span className="ex-stat-value" style={{ fontSize: 15 }}>{fmtBytes(totalBytes)}</span>
          </div>
          <div className="ex-stat">
            <span className="ex-stat-label">Ticks / min (combined)</span>
            <span className="ex-stat-value green">
              {Object.values(stats).reduce((s, e) => s + (e.ticks_1m ?? 0), 0)}
            </span>
          </div>
        </div>
      </div>

      {loading ? (
        <p className="empty">Loading exchange stats…</p>
      ) : (
        <div className="exchanges-grid">
          {EXCHANGES.map((ex) => (
            <ExchangeCard key={ex} ex={ex} stats={stats[ex]} />
          ))}
        </div>
      )}
    </div>
  )
}
