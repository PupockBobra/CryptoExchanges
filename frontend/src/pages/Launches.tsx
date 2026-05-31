import { useEffect, useState } from 'react'
import { RefreshCw, Rocket } from 'lucide-react'
import { EXCHANGE_COLORS } from '../types'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/launches'

interface LaunchRow {
  symbol:    string
  base:      string
  exchange:  string
  listed_at: string | null
}

interface GroupedInstrument {
  base:        string
  newest_date: string | null
  exchanges:   { exchange: string; symbol: string; listed_at: string | null }[]
}

// ── Full names ────────────────────────────────────────────────────────────────

const BASE_NAMES: Record<string, string> = {
  // Energy
  BRN: 'Brent Crude Oil', BZ: 'Brent Crude Oil', BRENT: 'Brent Crude Oil',
  UKOIL: 'Brent Crude Oil (UK)', USOIL: 'WTI Crude Oil',
  WTI: 'WTI Crude Oil', OIL: 'Crude Oil',
  NG: 'Natural Gas', NGAS: 'Natural Gas', NATGAS: 'Natural Gas',
  // Metals
  GOLD: 'Gold', XAU: 'Gold', XAUT: 'Gold (Tether)',
  SILVER: 'Silver', XAG: 'Silver',
  PLATINUM: 'Platinum', XPT: 'Platinum',
  PALLADIUM: 'Palladium', XPD: 'Palladium',
  COPPER: 'Copper', HG: 'Copper',
  // Agricultural
  WHEAT: 'Wheat', CORN: 'Corn', SOYBEAN: 'Soybean',
  COTTON: 'Cotton', COFFEE: 'Coffee', COCOA: 'Cocoa', SUGAR: 'Sugar',
  // Indices / ETFs
  QQQ: 'Nasdaq-100 ETF', SPY: 'S&P 500 ETF', SPX: 'S&P 500',
  SPX500: 'S&P 500', NAS100: 'Nasdaq-100', NASDAQ: 'Nasdaq Composite',
  NDX: 'Nasdaq-100', DOW: 'Dow Jones', DJI: 'Dow Jones', NIKKEI: 'Nikkei 225',
  DAX: 'DAX 40', FTSE: 'FTSE 100', CAC: 'CAC 40',
  ES: 'S&P 500 Futures', NQ: 'Nasdaq Futures',
  RUT: 'Russell 2000', VIX: 'CBOE Volatility Index',
  // US Stocks
  AAPL: 'Apple Inc.', AMZN: 'Amazon.com', GOOGL: 'Alphabet (Google)', GOOG: 'Alphabet (Google)',
  META: 'Meta Platforms', MSFT: 'Microsoft Corp.', NVDA: 'NVIDIA Corp.',
  TSLA: 'Tesla Inc.', NFLX: 'Netflix Inc.',
  AMD: 'Advanced Micro Devices', INTC: 'Intel Corp.', QCOM: 'Qualcomm',
  MU: 'Micron Technology', TXN: 'Texas Instruments', AVGO: 'Broadcom Inc.',
  CRM: 'Salesforce', ORCL: 'Oracle Corp.', IBM: 'IBM Corp.',
  CSCO: 'Cisco Systems', DELL: 'Dell Technologies', HPQ: 'HP Inc.',
  PYPL: 'PayPal Holdings', COIN: 'Coinbase Global', HOOD: 'Robinhood Markets',
  PLTR: 'Palantir Technologies', ABNB: 'Airbnb Inc.', UBER: 'Uber Technologies',
  LYFT: 'Lyft Inc.', SNAP: 'Snap Inc.', PINS: 'Pinterest Inc.',
  RBLX: 'Roblox Corp.', DIS: 'Walt Disney Co.', V: 'Visa Inc.',
  MA: 'Mastercard', JPM: 'JPMorgan Chase', BAC: 'Bank of America',
  GS: 'Goldman Sachs', MS: 'Morgan Stanley', WMT: 'Walmart',
  TGT: 'Target Corp.', COST: 'Costco Wholesale', HD: 'Home Depot',
  PFE: 'Pfizer Inc.', MRNA: 'Moderna Inc.', JNJ: 'Johnson & Johnson',
  UNH: 'UnitedHealth Group', CVX: 'Chevron Corp.', XOM: 'ExxonMobil',
  BA: 'Boeing Co.', GE: 'GE Aerospace', F: 'Ford Motor Co.',
  GM: 'General Motors', NIO: 'NIO Inc.', BABA: 'Alibaba Group',
  JD: 'JD.com', PDD: 'PDD Holdings (Temu)', SHOP: 'Shopify',
  SQ: 'Block Inc. (Square)', ROKU: 'Roku Inc.', ZM: 'Zoom Video',
  CRWD: 'CrowdStrike', DDOG: 'Datadog', SNOW: 'Snowflake',
  AFRM: 'Affirm Holdings', SOFI: 'SoFi Technologies',
  RIVN: 'Rivian Automotive', LCID: 'Lucid Group',
  SBUX: 'Starbucks', MCD: "McDonald's", KO: 'Coca-Cola',
  PEP: 'PepsiCo', PG: 'Procter & Gamble',
  LLY: 'Eli Lilly', ABBV: 'AbbVie Inc.', MRK: 'Merck & Co.', BMY: 'Bristol-Myers Squibb',
  WTI2: 'WTI Crude',
}

function getName(base: string): string {
  return BASE_NAMES[base.toUpperCase()] ?? ''
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const EXCHANGE_LABEL: Record<string, string> = {
  binance: 'Binance', okx: 'OKX', mexc: 'MEXC', hyperliquid: 'Hyperliquid',
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
  return (Date.now() - new Date(dateStr).getTime()) < 7 * 86400000
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

  return Array.from(map.values())
    .map((g) => ({
      ...g,
      // Sort exchanges within group: dated ones first (newest → oldest), then undated
      exchanges: [...g.exchanges].sort((a, b) => {
        if (a.listed_at === b.listed_at) return a.exchange.localeCompare(b.exchange)
        if (!a.listed_at) return 1
        if (!b.listed_at) return -1
        return b.listed_at.localeCompare(a.listed_at)
      }),
    }))
    .sort((a, b) => {
      // Groups with no dates go to the bottom
      if (!a.newest_date && !b.newest_date) return a.base.localeCompare(b.base)
      if (!a.newest_date) return 1
      if (!b.newest_date) return -1
      return b.newest_date.localeCompare(a.newest_date)
    })
}

// ── Page ─────────────────────────────────────────────────────────────────────

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

  const TH = (label: string) => (
    <th key={label} style={{
      padding: '10px 14px', textAlign: 'left', fontWeight: 600,
      fontSize: 11, textTransform: 'uppercase', letterSpacing: '.05em',
      color: 'var(--muted)', whiteSpace: 'nowrap',
    }}>{label}</th>
  )

  return (
    <div>
      {/* Toolbar */}
      <div className="page-toolbar">
        <h1>Futures Launches</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Non-crypto perpetual swaps · sorted by launch date
          {loaded && ` · ${groups.length} instruments, ${new Set(rows.map(r => r.exchange)).size} exchanges`}
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

      {loading && <p className="empty">Scanning exchanges…</p>}
      {error   && <p className="empty" style={{ color: 'var(--red)' }}>{error}</p>}

      {!loading && loaded && (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                {TH('Ticker')}
                {TH('Base Asset')}
                {TH('Exchange')}
                {TH('Symbol (exchange)')}
                {TH('Listed')}
                {TH('Age')}
              </tr>
            </thead>
            <tbody>
              {groups.map((g) =>
                g.exchanges.map((ex, i) => (
                  <tr
                    key={`${g.base}-${ex.exchange}`}
                    style={{ borderBottom: '1px solid var(--border)' }}
                  >
                    {/* Ticker — only on first row of group */}
                    <td style={{ padding: '9px 14px', fontWeight: 700, color: 'var(--title)', whiteSpace: 'nowrap' }}>
                      {i === 0 && (
                        <span style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                          {g.base}
                          {isNew(g.newest_date) && (
                            <span style={{
                              background: '#10b981', color: '#fff',
                              fontSize: 10, fontWeight: 700, borderRadius: 4,
                              padding: '1px 5px', letterSpacing: '.04em',
                              display: 'inline-flex', alignItems: 'center', gap: 3,
                            }}>
                              <Rocket size={9} /> NEW
                            </span>
                          )}
                        </span>
                      )}
                    </td>

                    {/* Base Asset full name — only on first row */}
                    <td style={{ padding: '9px 14px', color: 'var(--muted)' }}>
                      {i === 0 ? getName(g.base) : ''}
                    </td>

                    {/* Exchange badge */}
                    <td style={{ padding: '9px 14px' }}>
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12 }}>
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
                    <td style={{ padding: '9px 14px', whiteSpace: 'nowrap', fontWeight: ex.listed_at ? 500 : 400 }}>
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
