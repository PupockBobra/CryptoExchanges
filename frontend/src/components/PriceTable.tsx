import { useState, useCallback, useEffect } from 'react'
import { useWebSocket } from '../hooks/useWebSocket'
import { symbolChannel, formatSymbol } from '../types'
import type { PriceTick } from '../types'

interface Props {
  symbol: string
}

/** Format a USDT volume number to a compact human-readable string. */
function fmtVol(v?: number): string {
  if (!v) return '—'
  if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `$${(v / 1e3).toFixed(0)}K`
  return `$${v.toFixed(0)}`
}

export function PriceTable({ symbol }: Props) {
  const [ticks, setTicks] = useState<Record<string, PriceTick>>({})
  const channel = symbolChannel(symbol)

  // Clear stale prices immediately when the symbol changes so the table
  // never briefly shows BTC/USDT prices under the ETH/USDT heading.
  useEffect(() => {
    setTicks({})
  }, [symbol])

  // `symbol` is intentionally in deps — the callback must capture the current
  // symbol so it can reject buffered messages from the previous channel that
  // arrive while the old WebSocket is still in CLOSING state.
  const onMessage = useCallback((data: unknown) => {
    const tick = data as PriceTick
    if (tick.symbol !== symbol) return          // drop stale cross-symbol frames
    setTicks((prev) => ({ ...prev, [tick.exchange]: tick }))
  }, [symbol])

  useWebSocket(channel, { onMessage })

  const rows = Object.values(ticks).sort((a, b) => a.exchange.localeCompare(b.exchange))

  return (
    <div className="card">
      <h2 className="card-title">Live Prices — {formatSymbol(symbol)}</h2>
      <table className="price-table">
        <thead>
          <tr>
            <th>Exchange</th>
            <th>Bid</th>
            <th>Ask</th>
            <th>Last</th>
            <th>24h Vol</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={6} className="empty">Waiting for data…</td>
            </tr>
          )}
          {rows.map((t) => (
            <tr key={t.exchange}>
              <td className="exchange-badge">{t.exchange}</td>
              <td>{t.bid.toFixed(2)}</td>
              <td>{t.ask.toFixed(2)}</td>
              <td className="last">{t.last.toFixed(2)}</td>
              <td className="vol">{fmtVol(t.volume)}</td>
              <td className="ts">{new Date(t.ts).toLocaleTimeString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
