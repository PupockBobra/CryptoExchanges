import { useEffect, useMemo, useRef, useState } from 'react'
import { Pause, Play } from 'lucide-react'
import { SectionHeading } from '../components/SectionHeading'
import { fetchJson } from '../utils/api'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/spb'

// Section order on the page; tickers carry their group from the backend.
const GROUP_ORDER = ['US Market', 'Crypto']

// How many levels to render per side (the backend caps the payload at 12).
const DISPLAY_DEPTH = 8

// Poll cadence for the warm backend cache.  The backend keeps books via the
// Finam gRPC stream (tick-by-tick deltas), so the cache changes continuously —
// 1 s polling gives a terminal-style feed without flooding the API.
const POLL_MS = 1000

interface Level { price: number; size: number }
interface Book {
  ticker: string
  name:   string
  group:  string
  bids:   Level[]   // sorted best (highest) first
  asks:   Level[]   // sorted best (lowest) first
  error:  string | null
}

function fmtPrice(p: number): string {
  return p.toLocaleString('en-US', { maximumFractionDigits: p >= 100 ? 2 : 4 })
}

function fmtSize(s: number): string {
  return s.toLocaleString('en-US', { maximumFractionDigits: 4 })
}

function OrderBookCard({ book }: { book: Book }) {
  const asks = book.asks.slice(0, DISPLAY_DEPTH)
  const bids = book.bids.slice(0, DISPLAY_DEPTH)

  const bestAsk = asks[0]?.price
  const bestBid = bids[0]?.price
  const spread  = bestAsk != null && bestBid != null ? bestAsk - bestBid : null
  const mid     = bestAsk != null && bestBid != null ? (bestAsk + bestBid) / 2 : null
  const spreadPct = spread != null && mid ? (spread / mid) * 100 : null

  // Depth-bar scale — widest bar = largest resting size across both sides.
  const maxSize = Math.max(1, ...asks.map(a => a.size), ...bids.map(b => b.size))

  // Flash a level whose price is new or whose size changed since the last poll
  // (terminal-style): bid updates blink green, ask updates blink red.  Levels
  // are keyed by price+size, so a change remounts the row and replays the CSS
  // animation.  The ref holds the previous snapshot, updated after each commit.
  const prevRef = useRef<Map<string, number>>(new Map())
  const changed = (side: 'ask' | 'bid', price: number, size: number): boolean => {
    if (prevRef.current.size === 0) return false        // first fill — don't flash everything
    const prev = prevRef.current.get(`${side}:${price}`)
    return prev === undefined || prev !== size          // new level, or size changed
  }
  useEffect(() => {
    const m = new Map<string, number>()
    for (const b of bids) m.set(`bid:${b.price}`, b.size)
    for (const a of asks) m.set(`ask:${a.price}`, a.size)
    prevRef.current = m
  }, [book])

  const rowStyle: React.CSSProperties = {
    position: 'relative', display: 'flex', justifyContent: 'space-between',
    padding: '2px 8px', fontSize: 12, fontVariantNumeric: 'tabular-nums',
    lineHeight: 1.5,
  }

  const Level = ({ lvl, side, flash }: { lvl: Level; side: 'ask' | 'bid'; flash: boolean }) => {
    const color = side === 'ask' ? '#ef4444' : '#22c55e'
    const bar   = side === 'ask' ? 'rgba(239,68,68,.14)' : 'rgba(34,197,94,.14)'
    return (
      <div style={rowStyle} className={flash ? (side === 'ask' ? 'ob-flash-ask' : 'ob-flash-bid') : undefined}>
        <div style={{
          position: 'absolute', top: 0, bottom: 0, right: 0,
          width: `${(lvl.size / maxSize) * 100}%`, background: bar,
        }} />
        <span style={{ position: 'relative', color, fontWeight: 600 }}>{fmtPrice(lvl.price)}</span>
        <span style={{ position: 'relative', color: 'var(--muted)' }}>{fmtSize(lvl.size)}</span>
      </div>
    )
  }

  return (
    <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
      <div style={{
        padding: '10px 12px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'baseline', justifyContent: 'space-between',
      }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--fg)' }}>{book.name}</span>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{book.ticker}</span>
      </div>

      {asks.length === 0 && bids.length === 0 ? (
        <div style={{ padding: '16px 12px', fontSize: 12, color: 'var(--muted)' }}>
          {book.error
            ? (book.error.includes('429') ? 'превышен лимит запросов Finam, повтор…' : 'нет данных')
            : 'загрузка…'}
        </div>
      ) : (
        <div style={{ padding: '6px 0' }}>
          {/* asks: highest at top, best ask adjacent to the spread */}
          {[...asks].reverse().map(lvl => (
            <Level key={`a-${lvl.price}-${lvl.size}`} lvl={lvl} side="ask" flash={changed('ask', lvl.price, lvl.size)} />
          ))}

          <div style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            padding: '4px 8px', margin: '2px 0',
            borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)',
            fontSize: 11, color: 'var(--muted)',
          }}>
            <span>{mid != null ? fmtPrice(mid) : '—'}</span>
            <span>{spreadPct != null ? `spread ${spreadPct.toFixed(2)}%` : ''}</span>
          </div>

          {/* bids: best bid adjacent to the spread, then downwards */}
          {bids.map(lvl => (
            <Level key={`b-${lvl.price}-${lvl.size}`} lvl={lvl} side="bid" flash={changed('bid', lvl.price, lvl.size)} />
          ))}
        </div>
      )}
    </div>
  )
}

export function SPBOrderBook() {
  const [books,    setBooks]    = useState<Book[]>([])
  const [loading,  setLoading]  = useState(true)
  const [live,     setLive]     = useState(true)
  const [lastSync, setLastSync] = useState<Date | null>(null)
  const inFlight = useRef(false)

  const load = async (initial = false) => {
    if (inFlight.current) return            // skip overlap if a poll is slow
    inFlight.current = true
    if (initial) setLoading(true)
    try {
      const data = await fetchJson<Book[]>(`${API}/orderbook`)
      setBooks(data)
      setLastSync(new Date())
    } catch (e) {
      console.error('SPBOrderBook: failed to load order books', e)
    } finally {
      inFlight.current = false
      if (initial) setLoading(false)
    }
  }

  useEffect(() => { load(true) }, [])

  // Continuous refresh — poll the warm backend cache while `live` is on.
  useEffect(() => {
    if (!live) return
    const id = setInterval(() => load(false), POLL_MS)
    return () => clearInterval(id)
  }, [live])

  const byGroup = useMemo(() => {
    const m = new Map<string, Book[]>()
    for (const b of books) {
      const arr = m.get(b.group)
      if (arr) arr.push(b); else m.set(b.group, [b])
    }
    for (const arr of m.values()) arr.sort((a, b) => a.name.localeCompare(b.name))
    return m
  }, [books])

  return (
    <div>
      <div className="page-toolbar">
        <h1>Order Book</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          СПБ Биржа perpetual futures · live · price in USD · size in contracts
          {lastSync && ` · updated ${lastSync.toLocaleTimeString()}`}
        </div>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--muted)' }}>
          <span style={{
            width: 8, height: 8, borderRadius: '50%',
            background: live ? '#22c55e' : 'var(--muted)',
            boxShadow: live ? '0 0 0 3px rgba(34,197,94,.18)' : 'none',
          }} />
          {live ? 'Live' : 'Paused'}
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

      <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 0 }}>
        <div style={{ padding: '12px 20px 12px 16px', borderRight: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Страница показывает
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Стакан заявок вечных фьючерсов СПБ Биржи по каждому инструменту, обновляется непрерывно, пока открыта страница.
          </div>
        </div>
        <div style={{ padding: '12px 16px 12px 20px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Единицы
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Цена — в долларах США (валюта котировки), объём — в контрактах. Красное — предложение (ask), зелёное — спрос (bid).
          </div>
        </div>
      </div>

      {loading ? (
        <p className="empty">Loading order books…</p>
      ) : books.length === 0 ? (
        <p className="empty">No data (Finam token not configured?)</p>
      ) : (
        <>
          {GROUP_ORDER.map(group => {
            const groupBooks = byGroup.get(group)
            if (!groupBooks?.length) return null
            return (
              <div key={group}>
                <SectionHeading label={group} />
                <div className="analytics-grid">
                  {groupBooks.map(b => <OrderBookCard key={b.ticker} book={b} />)}
                </div>
              </div>
            )
          })}
        </>
      )}
    </div>
  )
}
