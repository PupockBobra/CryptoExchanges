import { useState, useCallback } from 'react'
import { useWebSocket } from './useWebSocket'
import type { ArbitrageAlert } from '../types'

const MAX_ALERTS = 100

export function useArbitrage() {
  const [alerts, setAlerts] = useState<ArbitrageAlert[]>([])

  const onMessage = useCallback((data: unknown) => {
    const alert = data as ArbitrageAlert
    setAlerts((prev) => {
      // Deduplicate: if the newest alert is the same opportunity (same symbol +
      // exchange pair) and arrived within 1 s, replace it instead of prepending.
      if (prev.length > 0) {
        const top = prev[0]
        const sameOpp =
          top.symbol       === alert.symbol &&
          top.buy_exchange  === alert.buy_exchange &&
          top.sell_exchange === alert.sell_exchange
        const withinWindow =
          Math.abs(new Date(alert.ts).getTime() - new Date(top.ts).getTime()) < 1000
        if (sameOpp && withinWindow) {
          return [alert, ...prev.slice(1)].slice(0, MAX_ALERTS)
        }
      }
      return [alert, ...prev].slice(0, MAX_ALERTS)
    })
  }, [])

  useWebSocket('arbitrage:alerts', { onMessage })

  return alerts
}
