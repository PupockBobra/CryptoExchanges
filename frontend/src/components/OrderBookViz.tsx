// Shared order-book visualization primitives, used by both the SPB Order Book
// page and the MM FORTS pages: the live stack renderer (LiveBook), the
// trading-hours x-axis helpers (rangebreaks + explicit ticks so the line stays
// continuous and no evening point sits under a next-morning label), the Plotly
// theme, and small formatters.  Kept generic — no page-specific data shapes.
import { useEffect, useRef } from 'react'

export interface Level { price: number; size: number }
export interface Book {
  ticker: string
  name:   string
  group:  string
  bids:   Level[]   // sorted best (highest) first
  asks:   Level[]   // sorted best (lowest) first
  error:  string | null
}

// How many levels to render per side (the backend caps the payload at 12).
export const DISPLAY_DEPTH = 8

export const FONT_FAMILY = 'Inter, system-ui, sans-serif'

// Plotly has no timezone support (renders raw UTC) — shift timestamps to
// Moscow time (UTC+3) before plotting.
export const MSK_OFFSET_MS = 3 * 3600 * 1000
export const toMsk = (iso: string) =>
  new Date(Date.parse(iso) + MSK_OFFSET_MS).toISOString().replace('Z', '')

export const SPREAD_PLOTLY_CONFIG = {
  displayModeBar: false, displaylogo: false, responsive: true,
} as const

// Trading windows (Moscow time): weekdays 07:00–23:45, weekends 10:00–19:00.
// These MUST mirror `_is_trading_now` in `backend/app/spb/spread_etl.py` — the
// backend decides which minutes get sampled, this decides which stretches of the
// x-axis are cut, and a mismatch either hides real points or leaves dead gaps.
const TRADE_WINDOW = (dow: number): [number, number] => (dow === 0 || dow === 6) ? [10, 19] : [7, 23.75]
const DAY_MS = 86_400_000

// Days (Moscow calendar dates) when MOEX did not trade at all, so the collector
// only recorded a frozen book or nothing.  They are cut out of the x-axis
// entirely — on both the SPB Order Book and the MM pages, so the two stay
// comparable.  Extend the list when another non-trading day shows up.
const CLOSED_DAYS = new Set(['2026-08-01', '2026-08-02', '2026-08-15', '2026-08-16'])
const dayKey = (ms: number) => new Date(ms).toISOString().slice(0, 10)

// Bucket timestamps → the MSK-as-UTC frame everything below works in, closed
// days dropped.
const mskStamps = (xsIso: string[]) => xsIso
  .map(s => Date.parse(s) + MSK_OFFSET_MS)
  .filter(n => !Number.isNaN(n) && !CLOSED_DAYS.has(dayKey(n)))

// Drop closed-day points from a series instead of only hiding them behind a
// rangebreak: Plotly still counts hidden points when auto-scaling y, and a
// frozen book reads as a huge spread outlier that would squash the real line.
// Also does the MSK shift, so this replaces `.map(toMsk)` at the call sites.
export function stripClosedDays(bucketsIso: string[], ys: (number | null)[]): {
  x: string[]; y: (number | null)[]
} {
  const x: string[] = [], y: (number | null)[] = []
  bucketsIso.forEach((b, i) => {
    const t = Date.parse(b) + MSK_OFFSET_MS
    if (Number.isNaN(t) || CLOSED_DAYS.has(dayKey(t))) return
    x.push(new Date(t).toISOString().replace('Z', ''))
    y.push(ys[i] ?? null)
  })
  return { x, y }
}

function tradingIntervals(min: number, max: number): [number, number][] {
  const first = new Date(min)
  const day0 = Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), first.getUTCDate()) - DAY_MS
  const intervals: [number, number][] = []
  for (let d = day0; d <= max + DAY_MS; d += DAY_MS) {
    if (CLOSED_DAYS.has(dayKey(d))) continue
    const [oh, ch] = TRADE_WINDOW(new Date(d).getUTCDay())
    intervals.push([d + oh * 3600_000, d + ch * 3600_000])
  }
  return intervals
}

// Rangebreaks that CUT the non-trading gaps out of the x-axis so the line stays
// continuous across sessions.  x is plotted in MSK, so breaks are computed in
// the same MSK-as-UTC frame.
export function tradingBreaks(xsIso: string[]): { values: string[]; dvalue: number }[] {
  const xs = mskStamps(xsIso)
  if (!xs.length) return []
  const intervals = tradingIntervals(Math.min(...xs), Math.max(...xs))
  const breaks: { values: string[]; dvalue: number }[] = []
  for (let i = 1; i < intervals.length; i++) {
    const gs = intervals[i - 1][1] + 60_000, ge = intervals[i][0]
    if (ge > gs) breaks.push({ values: [new Date(gs).toISOString().replace('Z', '')], dvalue: ge - gs })
  }
  return breaks
}

// Explicit x-axis ticks for the rangebreak-cut axis — round wall-clock ticks
// strictly inside each visible session, never on a seam.
const MAX_TICKS = 6
const STEP_HOURS = [1, 2, 3, 4, 6, 12]
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
export function tradingTicks(xsIso: string[]): { tickvals: string[]; ticktext: string[] } | null {
  const xs = mskStamps(xsIso)
  if (!xs.length) return null
  const min = Math.min(...xs), max = Math.max(...xs)
  const rawIntervals = tradingIntervals(min, max)
  const seams = new Set<number>()
  for (const [s, e] of rawIntervals) { seams.add(s); seams.add(e) }
  const spans = rawIntervals
    .map(([s, e]): [number, number] => [Math.max(s, min), Math.min(e, max)])
    .filter(([s, e]) => e > s)
  if (!spans.length) return null
  const total = spans.reduce((acc, [s, e]) => acc + (e - s), 0)
  const stepH = STEP_HOURS.find(h => total / (h * 3600_000) <= MAX_TICKS)
  const tickvals: string[] = []
  const ticktext: string[] = []

  // Multi-day window: hourly ticks would be too dense, so place exactly one
  // tick per trading day anchored to a REAL data point — the first 15-min
  // bucket after the session open (07:15 on weekdays, 10:15 on weekends) — with
  // the date on top and that time underneath.  Days whose open+15 falls outside
  // the loaded data (a partially-included first day) are skipped.  A 24h step
  // would land on midnight — outside every session — and emit nothing, so
  // Plotly auto-ticked every 2 days.
  if (stepH === undefined) {
    for (const [rs, re] of rawIntervals) {
      const t = rs + 15 * 60_000
      if (t < min || t > max || t >= re) continue
      const d = new Date(t)
      const hm = `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
      tickvals.push(d.toISOString().replace('Z', ''))
      ticktext.push(`${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}<br>${hm}`)
    }
    return tickvals.length ? { tickvals, ticktext } : null
  }

  const step = stepH * 3600_000
  let prevDay = ''
  for (const [s, e] of spans) {
    for (let t = Math.ceil(s / step) * step; t <= e; t += step) {
      if (seams.has(t)) continue
      const d = new Date(t)
      const day = `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`
      const hm = `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
      tickvals.push(d.toISOString().replace('Z', ''))
      ticktext.push(day === prevDay ? hm : `${hm}<br>${day}`)
      prevDay = day
    }
  }
  return tickvals.length ? { tickvals, ticktext } : null
}

export function spreadTheme(theme: 'dark' | 'light') {
  if (theme === 'light') return {
    bg: '#ffffff', paper: '#f8fafc', grid: '#e2e8f0', text: '#64748b',
    hover: '#ffffff', hoverBorder: '#e2e8f0', hoverText: '#1e293b',
  }
  return {
    bg: '#0f1117', paper: '#1a1d27', grid: '#1f2937', text: '#9ca3af',
    hover: '#21263a', hoverBorder: '#2d3148', hoverText: '#e2e8f0',
  }
}

export function fmtPrice(p: number): string {
  return p.toLocaleString('en-US', { maximumFractionDigits: p >= 100 ? 2 : 4 })
}
export function fmtSize(s: number): string {
  return s.toLocaleString('en-US', { maximumFractionDigits: 4 })
}

// One live order book (asks on top → mid+spread → bids), terminal-style with a
// flash on changed levels.  Reused for SPB, MOEX and MM books.
export function LiveBook({ book, depth = DISPLAY_DEPTH }: { book: Book; depth?: number }) {
  const asks = book.asks.slice(0, depth)
  const bids = book.bids.slice(0, depth)

  const bestAsk = asks[0]?.price
  const bestBid = bids[0]?.price
  const spread  = bestAsk != null && bestBid != null ? bestAsk - bestBid : null
  const mid     = bestAsk != null && bestBid != null ? (bestAsk + bestBid) / 2 : null
  const spreadPct = spread != null && mid ? (spread / mid) * 100 : null

  const maxSize = Math.max(1, ...asks.map(a => a.size), ...bids.map(b => b.size))

  // Flash a level whose price is new or whose size changed since the last poll.
  const prevRef = useRef<Map<string, number>>(new Map())
  const changed = (side: 'ask' | 'bid', price: number, size: number): boolean => {
    if (prevRef.current.size === 0) return false
    const prev = prevRef.current.get(`${side}:${price}`)
    return prev === undefined || prev !== size
  }
  useEffect(() => {
    const m = new Map<string, number>()
    for (const b of bids) m.set(`bid:${b.price}`, b.size)
    for (const a of asks) m.set(`ask:${a.price}`, a.size)
    prevRef.current = m
  }, [book])

  const rowStyle: React.CSSProperties = {
    position: 'relative', display: 'flex', justifyContent: 'space-between',
    padding: '2px 8px', fontSize: 12, fontVariantNumeric: 'tabular-nums', lineHeight: 1.5,
  }

  const LevelRow = ({ lvl, side, flash }: { lvl: Level; side: 'ask' | 'bid'; flash: boolean }) => {
    const color = side === 'ask' ? '#ef4444' : '#22c55e'
    const bar   = side === 'ask' ? 'rgba(239,68,68,.14)' : 'rgba(34,197,94,.14)'
    return (
      <div style={rowStyle} className={flash ? (side === 'ask' ? 'ob-flash-ask' : 'ob-flash-bid') : undefined}>
        <div style={{ position: 'absolute', top: 0, bottom: 0, right: 0,
          width: `${(lvl.size / maxSize) * 100}%`, background: bar }} />
        <span style={{ position: 'relative', color, fontWeight: 600 }}>{fmtPrice(lvl.price)}</span>
        <span style={{ position: 'relative', color: 'var(--muted)' }}>{fmtSize(lvl.size)}</span>
      </div>
    )
  }

  if (asks.length === 0 && bids.length === 0) {
    return (
      <div style={{ padding: '16px 12px', fontSize: 12, color: 'var(--muted)' }}>
        {book.error
          ? (book.error.includes('429') ? 'превышен лимит запросов Finam, повтор…' : 'нет данных')
          : 'загрузка…'}
      </div>
    )
  }
  return (
    <div style={{ padding: '6px 0' }}>
      {[...asks].reverse().map(lvl => (
        <LevelRow key={`a-${lvl.price}-${lvl.size}`} lvl={lvl} side="ask" flash={changed('ask', lvl.price, lvl.size)} />
      ))}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        padding: '4px 8px', margin: '2px 0',
        borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)',
        fontSize: 11, color: 'var(--muted)' }}>
        <span>{mid != null ? fmtPrice(mid) : '—'}</span>
        <span>{spreadPct != null ? `spread ${spreadPct.toFixed(2)}%` : ''}</span>
      </div>
      {bids.map(lvl => (
        <LevelRow key={`b-${lvl.price}-${lvl.size}`} lvl={lvl} side="bid" flash={changed('bid', lvl.price, lvl.size)} />
      ))}
    </div>
  )
}
