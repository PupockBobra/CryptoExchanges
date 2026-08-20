import { useEffect, useState } from 'react'
import { useTheme } from '../hooks/useTheme'
import { SectionHeading } from '../components/SectionHeading'
import { fetchJson } from '../utils/api'
import {
  MetricSection,
  type ShareRow, type VolumeRow, type OiRow,
} from './SPBMarketShare'
import { SpreadChart, type SpreadSeries } from './SPBOrderBook'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/spb'

// One-shot morning report: the two Market Share sections (Volume + Open
// Interest) followed by the BTC & ETH spread-on-volume charts, stacked so the
// whole thing can be captured in a single full-page screenshot.  Tickers are
// the SPB crypto-index perps.
const SCREENSHOT_TICKERS = ['BTCUSDperpA', 'ETHUSDperpA']

// Spread-history reload cadence — completed 15-min buckets change slowly.
const SPREAD_REFRESH_MS = 60 * 1000

export function SPBScreenshot() {
  const theme = useTheme()

  // ── Market Share data (Volume + OI) ──────────────────────────────────────
  const [volRows, setVolRows] = useState<ShareRow[]>([])
  const [oiRows, setOiRows]   = useState<ShareRow[]>([])

  useEffect(() => {
    ;(async () => {
      const [vol, oi] = await Promise.allSettled([
        fetchJson<VolumeRow[]>(`${API}/daily-volume`),
        fetchJson<OiRow[]>(`${API}/open-interest`),
      ])
      if (vol.status === 'fulfilled')
        setVolRows(vol.value.map(r => ({ date: r.date, ticker: r.ticker, name: r.name, group: r.group, value: r.turnover_rub })))
      if (oi.status === 'fulfilled')
        setOiRows(oi.value.map(r => ({ date: r.date, ticker: r.ticker, name: r.name, group: r.group, value: r.oi_rub })))
    })()
  }, [])

  // ── Spread-on-volume history (BTC + ETH) ─────────────────────────────────
  const [spread, setSpread] = useState<Map<string, SpreadSeries>>(new Map())

  useEffect(() => {
    const loadSpread = async () => {
      try {
        const data = await fetchJson<SpreadSeries[]>(`${API}/spread-history?days=7`)
        setSpread(new Map(data.map(s => [s.ticker, s])))
      } catch (e) {
        console.error('SPBScreenshot: failed to load spread history', e)
      }
    }
    loadSpread()
    const id = setInterval(loadSpread, SPREAD_REFRESH_MS)
    return () => clearInterval(id)
  }, [])

  return (
    <div>
      <div className="page-toolbar">
        <h1>Screenshot</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Утренний отчёт одним снимком · Market Share (Volume + OI) + спред BTC/ETH
        </div>
      </div>

      <SectionHeading label="Market Share · Volume" />
      <MetricSection rows={volRows} theme={theme} metric="Volume" slug="volume" />

      <SectionHeading label="Market Share · Open Interest" />
      <div style={{ fontSize: 12, color: 'var(--muted)', margin: '-4px 0 4px' }}>
        Открытый интерес учитывается с двух сторон (long + short).
      </div>
      <MetricSection rows={oiRows} theme={theme} metric="OI" slug="oi" />

      <SectionHeading label="Спред на объём (1 млн руб) · BTC / ETH" />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {SCREENSHOT_TICKERS.map(ticker => (
          <div key={ticker} className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ padding: '10px 12px', borderBottom: '1px solid var(--border)' }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--fg)' }}>
                {spread.get(ticker)?.name ?? ticker}
              </span>
            </div>
            <div style={{ padding: '6px 6px 4px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <SpreadChart series={spread.get(ticker)} moex={spread.get(ticker)?.moex} metric="usd" />
              <SpreadChart series={spread.get(ticker)} moex={spread.get(ticker)?.moex} metric="pct" />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
