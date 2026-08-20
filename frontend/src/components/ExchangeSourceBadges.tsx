import { EXCHANGE_COLORS } from '../types'
import type { Exchange } from '../types'

const EXCHANGE_LABEL: Record<Exchange, string> = {
  binance:     'Binance',
  okx:         'OKX',
  bybit:       'Bybit',
  mexc:        'MEXC',
  hyperliquid: 'Hyperliquid',
  bitget:      'Bitget',
  moex:        'MOEX FORTS',
}

interface Props {
  exchanges: Exchange[]
  label?:    string
}

/** Colored badge strip showing which exchanges contribute data to the page. */
export function ExchangeSourceBadges({ exchanges, label = 'Data from' }: Props) {
  return (
    <div className="card" style={{ marginBottom: 16, padding: '10px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
        <span style={{
          fontSize: 11, fontWeight: 600,
          textTransform: 'uppercase', letterSpacing: '.08em',
          color: 'var(--muted)',
        }}>
          {label}
        </span>
        {exchanges.map(ex => (
          <span key={ex} style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            fontSize: 13, fontWeight: 500,
          }}>
            <span style={{
              width: 10, height: 10, borderRadius: 3, flexShrink: 0,
              background: EXCHANGE_COLORS[ex],
              display: 'inline-block',
            }} />
            {EXCHANGE_LABEL[ex]}
          </span>
        ))}
      </div>
    </div>
  )
}
