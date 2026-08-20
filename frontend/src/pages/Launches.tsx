import { useEffect, useMemo, useState } from 'react'
import { RefreshCw, Rocket, Archive } from 'lucide-react'
import { EXCHANGE_COLORS } from '../types'
import { daysAgo } from '../utils/format'
import { SectionHeading } from '../components/SectionHeading'
import { ExchangeSourceBadges } from '../components/ExchangeSourceBadges'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/launches'

interface LaunchRow {
  symbol:      string
  base:        string
  exchange:    string
  listed_at:   string | null
  known_since: string | null  // earliest date in our ohlcv_daily for this (symbol, exchange)
}

interface ExchangeRow {
  exchange:    string
  symbol:      string
  listed_at:   string | null
  known_since: string | null
  is_recent:   boolean   // listed_at within NEW_DAYS — true regardless of group state
}

interface GroupedInstrument {
  base:             string
  newest_date:      string | null   // most recent listed_at across exchanges
  is_new_product:   boolean         // first listing across ALL exchanges within NEW_DAYS
  exchanges:        ExchangeRow[]
}

type Category = 'New' | 'Commodities' | 'Stocks' | 'Indexes' | 'Other'

// ── Lookups ───────────────────────────────────────────────────────────────────

const BASE_NAMES: Record<string, string> = {
  BRN: 'Brent Crude Oil', BZ: 'Brent Crude Oil', BRENT: 'Brent Crude Oil',
  UKOIL: 'Brent Crude Oil (UK)', USOIL: 'WTI Crude Oil', WTI: 'WTI Crude Oil', OIL: 'Crude Oil',
  NG: 'Natural Gas', NGAS: 'Natural Gas', NATGAS: 'Natural Gas',
  GOLD: 'Gold', XAU: 'Gold', XAUT: 'Gold (Tether)',
  SILVER: 'Silver', XAG: 'Silver',
  PLATINUM: 'Platinum', XPT: 'Platinum',
  PALLADIUM: 'Palladium', XPD: 'Palladium',
  COPPER: 'Copper',
  WHEAT: 'Wheat', CORN: 'Corn', SOYBEAN: 'Soybean',
  COTTON: 'Cotton', COFFEE: 'Coffee', COCOA: 'Cocoa', SUGAR: 'Sugar',
  QQQ: 'Nasdaq-100 ETF', SPY: 'S&P 500 ETF', SPX500: 'S&P 500',
  NAS100: 'Nasdaq-100', NASDAQ: 'Nasdaq Composite', NDX: 'Nasdaq-100',
  DOW: 'Dow Jones', DJI: 'Dow Jones Industrial Avg.',
  NIKKEI: 'Nikkei 225', DAX: 'DAX 40', FTSE: 'FTSE 100', CAC: 'CAC 40',
  ES: 'S&P 500 Futures', NQ: 'Nasdaq Futures',
  RUT: 'Russell 2000', VIX: 'CBOE Volatility Index',
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
  UNH: 'UnitedHealth Group', XOM: 'ExxonMobil',
  BA: 'Boeing Co.', GE: 'GE Aerospace',
  GM: 'General Motors', NIO: 'NIO Inc.', BABA: 'Alibaba Group',
  JD: 'JD.com', PDD: 'PDD Holdings', SHOP: 'Shopify',
  SQ: 'Block Inc. (Square)', ROKU: 'Roku Inc.', ZM: 'Zoom Video',
  CRWD: 'CrowdStrike', DDOG: 'Datadog', SNOW: 'Snowflake',
  AFRM: 'Affirm Holdings', SOFI: 'SoFi Technologies',
  RIVN: 'Rivian Automotive', LCID: 'Lucid Group',
  SBUX: 'Starbucks', MCD: "McDonald's", KO: 'Coca-Cola',
  PEP: 'PepsiCo', PG: 'Procter & Gamble',
  LLY: 'Eli Lilly', ABBV: 'AbbVie Inc.', MRK: 'Merck & Co.', BMY: 'Bristol-Myers Squibb',
  SPCX: 'SpaceX (pre-IPO)',
  // Korean stocks
  SKHYNIX: 'SK Hynix Inc.', SAMSUNG: 'Samsung Electronics', HYUNDAI: 'Hyundai Motor Co.',
}

// Commodities = energy + metals + agricultural (all physical assets)
const COMMODITIES = new Set([
  // Energy
  'BRN','BZ','BRENT','UKOIL','USOIL','OIL','WTI','NG','NGAS','NATGAS',
  // Metals
  'GOLD','XAU','XAUT','SILVER','XAG','PLATINUM','XPT','PALLADIUM','XPD','COPPER',
  // Agricultural
  'WHEAT','CORN','SOYBEAN','COTTON','COFFEE','COCOA','SUGAR',
])
// Indexes = indices, ETFs, volatility
const INDEXES = new Set([
  'QQQ','SPY','SPX500','NAS100','NASDAQ','NDX','DOW','DJI',
  'NIKKEI','DAX','FTSE','CAC','ES','NQ','RUT','VIX',
])

function getCategory(base: string): Exclude<Category, 'New'> {
  const b = base.toUpperCase()
  if (COMMODITIES.has(b)) return 'Commodities'
  if (INDEXES.has(b))     return 'Indexes'
  if (BASE_NAMES[b])      return 'Stocks'
  return 'Other'
}

const NEW_DAYS = 7

// ── Helpers ───────────────────────────────────────────────────────────────────

const EXCHANGE_LABEL: Record<string, string> = {
  binance: 'Binance', okx: 'OKX', mexc: 'MEXC', bybit: 'Bybit',
  hyperliquid: 'Hyperliquid', bitget: 'Bitget',
}

function isRecent(dateStr: string | null): boolean {
  if (!dateStr) return false
  return (Date.now() - new Date(dateStr).getTime()) < NEW_DAYS * 86400000
}

function groupRows(rows: LaunchRow[]): GroupedInstrument[] {
  const map = new Map<string, GroupedInstrument>()
  for (const r of rows) {
    if (!map.has(r.base))
      map.set(r.base, {
        base: r.base, newest_date: null, is_new_product: false, exchanges: [],
      })
    const g = map.get(r.base)!
    g.exchanges.push({
      exchange:    r.exchange,
      symbol:      r.symbol,
      listed_at:   r.listed_at,
      known_since: r.known_since,
      is_recent:   isRecent(r.listed_at),
    })
    if (r.listed_at && (g.newest_date === null || r.listed_at > g.newest_date))
      g.newest_date = r.listed_at
  }

  // is_new_product: every (known) listed_at is within NEW_DAYS AND we have no
  // older ohlcv data. Brand-new product on the market.
  for (const g of map.values()) {
    const dates = g.exchanges.map(e => e.listed_at).filter(Boolean) as string[]
    if (dates.length === 0) { g.is_new_product = false; continue }

    const minDate = dates.reduce((a, b) => a < b ? a : b)
    if (!isRecent(minDate)) { g.is_new_product = false; continue }

    const knownSince = g.exchanges.map(e => e.known_since).filter(Boolean).sort()[0] ?? null
    g.is_new_product = !knownSince || knownSince >= minDate
  }

  return Array.from(map.values()).map((g) => ({
    ...g,
    exchanges: [...g.exchanges].sort((a, b) => {
      if (!a.listed_at && !b.listed_at) return a.exchange.localeCompare(b.exchange)
      if (!a.listed_at) return 1
      if (!b.listed_at) return -1
      return b.listed_at.localeCompare(a.listed_at)
    }),
  })).sort((a, b) => {
    if (!a.newest_date && !b.newest_date) return a.base.localeCompare(b.base)
    if (!a.newest_date) return 1
    if (!b.newest_date) return -1
    return b.newest_date.localeCompare(a.newest_date)
  })
}

/** Rows for "New on Exchange" — flattened, one per (base, recent listing). */
interface RecentListing {
  base:         string
  base_name:    string
  exchange:     string
  symbol:       string
  listed_at:    string
}

function collectRecentListings(groups: GroupedInstrument[]): RecentListing[] {
  const out: RecentListing[] = []
  for (const g of groups) {
    if (g.is_new_product) continue   // already in the "New Products" section
    for (const ex of g.exchanges) {
      if (ex.is_recent && ex.listed_at) {
        out.push({
          base:      g.base,
          base_name: BASE_NAMES[g.base.toUpperCase()] ?? '',
          exchange:  ex.exchange,
          symbol:    ex.symbol,
          listed_at: ex.listed_at,
        })
      }
    }
  }
  // newest first
  return out.sort((a, b) => b.listed_at.localeCompare(a.listed_at))
}

// ── Sub-components ────────────────────────────────────────────────────────────

const SECTION_ORDER: Category[] = ['New', 'Commodities', 'Stocks', 'Indexes', 'Other']

function InstrumentTable({ groups }: { groups: GroupedInstrument[] }) {
  return (
    <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            {['Ticker', 'Base Asset', 'Exchange', 'Symbol (exchange)', 'Listed', 'Age'].map(h => (
              <th key={h} style={{
                padding: '9px 14px', textAlign: 'left', fontWeight: 600,
                fontSize: 11, textTransform: 'uppercase', letterSpacing: '.05em',
                color: 'var(--muted)', whiteSpace: 'nowrap',
              }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {groups.map((g) =>
            g.exchanges.map((ex, i) => (
              <tr key={`${g.base}-${ex.exchange}`} style={{ borderBottom: '1px solid var(--border)' }}>

                <td style={{ padding: '8px 14px', fontWeight: 700, color: 'var(--title)', whiteSpace: 'nowrap' }}>
                  {i === 0 && (
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      {g.base}
                      {g.is_new_product && (
                        <span style={{
                          background: '#10b981', color: '#fff', fontSize: 10,
                          fontWeight: 700, borderRadius: 4, padding: '1px 5px',
                          letterSpacing: '.04em', display: 'inline-flex', alignItems: 'center', gap: 3,
                        }}>
                          <Rocket size={9} /> NEW
                        </span>
                      )}
                    </span>
                  )}
                </td>

                <td style={{ padding: '8px 14px', color: 'var(--muted)' }}>
                  {i === 0 ? (BASE_NAMES[g.base.toUpperCase()] ?? '') : ''}
                </td>

                <td style={{ padding: '8px 14px' }}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12 }}>
                    <span style={{
                      width: 8, height: 8, borderRadius: 2, flexShrink: 0,
                      background: (EXCHANGE_COLORS as Record<string, string>)[ex.exchange] ?? '#888',
                      display: 'inline-block',
                    }} />
                    {EXCHANGE_LABEL[ex.exchange] ?? ex.exchange}
                  </span>
                </td>

                <td style={{ padding: '8px 14px', color: 'var(--muted)', fontFamily: 'monospace', fontSize: 12 }}>
                  {ex.symbol}
                </td>

                <td style={{ padding: '8px 14px', whiteSpace: 'nowrap', fontWeight: ex.listed_at ? 500 : 400 }}>
                  {ex.listed_at ?? <span style={{ color: 'var(--muted)' }}>—</span>}
                </td>

                <td style={{ padding: '8px 14px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                  {daysAgo(ex.listed_at)}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}

function RecentListingsTable({ rows }: { rows: RecentListing[] }) {
  return (
    <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            {['Ticker', 'Base Asset', 'Exchange', 'Symbol (exchange)', 'Listed', 'Age'].map(h => (
              <th key={h} style={{
                padding: '9px 14px', textAlign: 'left', fontWeight: 600,
                fontSize: 11, textTransform: 'uppercase', letterSpacing: '.05em',
                color: 'var(--muted)', whiteSpace: 'nowrap',
              }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.base}-${r.exchange}-${i}`} style={{ borderBottom: '1px solid var(--border)' }}>
              <td style={{ padding: '8px 14px', fontWeight: 700, color: 'var(--title)', whiteSpace: 'nowrap' }}>
                {r.base}
              </td>
              <td style={{ padding: '8px 14px', color: 'var(--muted)' }}>
                {r.base_name}
              </td>
              <td style={{ padding: '8px 14px' }}>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12 }}>
                  <span style={{
                    width: 8, height: 8, borderRadius: 2, flexShrink: 0,
                    background: (EXCHANGE_COLORS as Record<string, string>)[r.exchange] ?? '#888',
                    display: 'inline-block',
                  }} />
                  {EXCHANGE_LABEL[r.exchange] ?? r.exchange}
                </span>
              </td>
              <td style={{ padding: '8px 14px', color: 'var(--muted)', fontFamily: 'monospace', fontSize: 12 }}>
                {r.symbol}
              </td>
              <td style={{ padding: '8px 14px', whiteSpace: 'nowrap', fontWeight: 500 }}>
                {r.listed_at}
              </td>
              <td style={{ padding: '8px 14px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                {daysAgo(r.listed_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────

export function Launches() {
  const [rows,    setRows]    = useState<LaunchRow[]>([])
  const [loading, setLoading] = useState(false)
  const [loaded,  setLoaded]  = useState(false)
  const [error,   setError]   = useState<string | null>(null)

  const [updatedAt, setUpdatedAt] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const fetchData = async (url: string, method = 'GET') => {
    const res = await fetch(url, { method })
    if (!res.ok) throw new Error(res.statusText)
    const json = await res.json()
    setRows(json.data)
    setUpdatedAt(json.updated_at)
    setLoaded(true)
  }

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      await fetchData(API)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }

  const refresh = async () => {
    setRefreshing(true)
    setError(null)
    try {
      await fetchData(API + '/refresh', 'POST')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to refresh')
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => { load() }, [])

  // groupRows is O(rows). Memoize so re-renders triggered by state changes
  // (Refresh button, updatedAt) don't re-sort on every render.
  const allGroups = useMemo(() => groupRows(rows), [rows])

  const { newGroups, newOnExchange, byCategory } = useMemo(() => {
    const byCat = new Map<Exclude<Category, 'New'>, GroupedInstrument[]>()
    for (const g of allGroups) {
      const cat = getCategory(g.base)
      if (!byCat.has(cat)) byCat.set(cat, [])
      byCat.get(cat)!.push(g)
    }
    return {
      newGroups:     allGroups.filter(g => g.is_new_product),
      newOnExchange: collectRecentListings(allGroups),
      byCategory:    byCat,
    }
  }, [allGroups])

  const totalInstruments = allGroups.length
  const totalExchanges   = useMemo(
    () => new Set(rows.map(r => r.exchange)).size,
    [rows],
  )

  return (
    <div>
      {/* Toolbar */}
      <div className="page-toolbar">
        <h1>Futures Launches</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Non-crypto perpetual swaps · sorted by launch date
          {loaded && ` · ${totalInstruments} instruments, ${totalExchanges} exchanges`}
          {updatedAt && (
            <span style={{ marginLeft: 10, opacity: 0.6 }}>
              · updated {new Date(updatedAt).toLocaleTimeString()}
            </span>
          )}
        </div>
        <button
          className="btn-secondary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={refresh}
          disabled={loading || refreshing}
        >
          <RefreshCw size={13} className={refreshing ? 'spin' : ''} />
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      <ExchangeSourceBadges exchanges={['bitget', 'hyperliquid', 'binance', 'okx', 'bybit', 'mexc']} />

      <div className="card" style={{ marginBottom: 16, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 0 }}>
        <div style={{ padding: '12px 20px 12px 16px', borderRight: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Страница показывает
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>
            Перп-контракты на традиционные активы на крупнейших криптобиржах: Bitget, Hyperliquid, Binance, OKX, Bybit, MEXC. Охватывает товары (нефть, газ, металлы), акции США и мировые индексы.
          </div>
        </div>
        <div style={{ padding: '12px 16px 12px 20px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)', marginBottom: 6 }}>
            Разделы
          </div>
          <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.6 }}>
            <b>New products</b> — инструмент появился на рынке впервые: самая ранняя дата листинга среди всех бирж не старше 7 дней и в нашей базе нет более ранних данных по нему.<br />
            <b>New on exchange</b> — инструмент давно существует, но конкретная биржа добавила его в течение последних 7 дней.<br />
            <b>History of launches</b> — полный список всех отслеживаемых перпов, сгруппированный по категориям активов.
          </div>
        </div>
      </div>

      {loading && <p className="empty">Scanning exchanges…</p>}
      {error   && <p className="empty" style={{ color: 'var(--red)' }}>{error}</p>}

      {!loading && loaded && (
        <>
          {/* Brand-new products (first listing anywhere within NEW_DAYS) */}
          {newGroups.length > 0 && (
            <>
              <SectionHeading
                label={`New Products — last ${NEW_DAYS} days`}
                count={newGroups.length}
                accent
                icon={<Rocket size={12} />}
              />
              <InstrumentTable groups={newGroups} />
            </>
          )}

          {/* Existing products that picked up a fresh listing on some exchange */}
          {newOnExchange.length > 0 && (
            <>
              <SectionHeading
                label={`New on Exchange — last ${NEW_DAYS} days`}
                count={newOnExchange.length}
                accent
                icon={<Rocket size={12} />}
              />
              <RecentListingsTable rows={newOnExchange} />
            </>
          )}

          {/* Historical launches — all instruments by category */}
          <SectionHeading label="History of Launches" count={allGroups.length} accent icon={<Archive size={12} />} />

          {SECTION_ORDER.filter(s => s !== 'New').map(cat => {
            const groups = byCategory.get(cat as Exclude<Category, 'New'>) ?? []
            if (!groups.length) return null
            return (
              <div key={cat}>
                <SectionHeading label={cat} count={groups.length} />
                <InstrumentTable groups={groups} />
              </div>
            )
          })}
        </>
      )}
    </div>
  )
}
