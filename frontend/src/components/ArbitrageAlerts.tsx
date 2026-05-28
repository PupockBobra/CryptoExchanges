import { TrendingUp } from 'lucide-react'
import { useArbitrage } from '../hooks/useArbitrage'
import type { ArbitrageAlert } from '../types'

function AlertRow({ alert }: { alert: ArbitrageAlert }) {
  return (
    <div className="alert-row">
      <div className="alert-spread">
        <TrendingUp size={14} />
        <span>{alert.spread_pct.toFixed(3)}%</span>
      </div>
      <div className="alert-detail">
        <span className="alert-symbol">{alert.symbol}</span>
        <span className="alert-route">
          Buy <strong>{alert.buy_exchange}</strong> @ {alert.buy_price.toFixed(2)}
          {' → '}
          Sell <strong>{alert.sell_exchange}</strong> @ {alert.sell_price.toFixed(2)}
        </span>
      </div>
      <div className="alert-ts">{new Date(alert.ts).toLocaleTimeString()}</div>
    </div>
  )
}

export function ArbitrageAlerts() {
  const alerts = useArbitrage()

  return (
    <div className="card alerts-card">
      <h2 className="card-title">Arbitrage Alerts</h2>
      {alerts.length === 0 ? (
        <p className="empty">No alerts yet — threshold {import.meta.env.VITE_ARBI_THRESHOLD ?? '0.3'}%</p>
      ) : (
        <div className="alert-list">
          {alerts.map((a) => (
            <AlertRow key={`${a.ts}-${a.symbol}-${a.buy_exchange}-${a.sell_exchange}`} alert={a} />
          ))}
        </div>
      )}
    </div>
  )
}
