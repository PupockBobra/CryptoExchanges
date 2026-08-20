export interface PriceTick {
  exchange: string
  symbol:   string
  bid:      number
  ask:      number
  last:     number
  volume?:  number   // 24 h trading volume in USDT (0 / absent when exchange doesn't provide)
  ts:       string
}

export interface ArbitrageAlert {
  symbol:        string
  buy_exchange:  string
  sell_exchange: string
  buy_price:     number
  sell_price:    number
  spread_pct:    number
  ts:            string
}

export interface OhlcvBar {
  bucket:   string
  exchange: string
  symbol:   string
  open:     number
  high:     number
  low:      number
  close:    number
  ticks:    number
}

export interface Instrument {
  id:          number
  canonical:   string
  type:        'spot' | 'perp'
  base_asset:  string
  quote_asset: string
  description: string
  enabled:     boolean
  aliases:     Record<string, string | null>   // { exchange_id: symbol | null }
  created_at:  string
  updated_at:  string
}

export interface ExchangeStats {
  exchange:       string
  status:         'connected' | 'connecting' | 'disconnected' | 'unknown'
  ticks_total:    number
  ticks_1m:       number
  bytes_in:       number
  reconnects:     number
  symbols_active: string[]
  last_tick_ts:   string | null
  started_at:     string | null
  updated_at:     string | null
}

export type Exchange = 'binance' | 'okx' | 'bybit' | 'mexc' | 'hyperliquid' | 'moex'

// Crypto exchanges only — used by all pages that work with real-time prices,
// OHLCV history, or exchange connection stats. MOEX is a data-only source
// (we ingest FORTS turnover but never connect a WebSocket or fetch prices).
export const EXCHANGES: Exchange[] = ['binance', 'okx', 'bybit', 'mexc', 'hyperliquid']

// Includes MOEX — used by analytics pages that visualise stacked turnover.
export const VOLUME_EXCHANGES: Exchange[] = [...EXCHANGES, 'moex']

export const EXCHANGE_COLORS: Record<Exchange, string> = {
  binance:     '#f0b90b',
  okx:         '#0052ff',
  bybit:       '#ff6b35',
  mexc:        '#0ecb81',
  hyperliquid: '#00e5ff',
  moex:        '#d52b1e',
}

// ── Symbol sections ───────────────────────────────────────────────────────────

export const SYMBOL_SECTIONS = [
  { label: 'Commodities',    bases: ['BRN', 'WTI', 'USOIL', 'NATGAS', 'NGAS', 'UKOIL', 'BRENT', 'COPPER', 'ALUMINIUM', 'WHEAT', 'CORN', 'URANIUM', 'TTF'] },
  { label: 'Precious Metals', bases: ['XAU', 'XAG', 'XPT', 'XPD'] },
  { label: 'US Market',      bases: ['NVDA', 'QQQ', 'SPY', 'AAPL', 'TSLA', 'AMZN', 'MSFT', 'GOOGL', 'META', 'SPCX'] },
  { label: 'Korean Market',  bases: ['SKHYNIX', 'SAMSUNG', 'HYUNDAI'] },
  { label: 'Crypto Perps',   bases: [] as string[] },   // catch-all
] as const

export type SymbolSection = 'Commodities' | 'Precious Metals' | 'US Market' | 'Korean Market' | 'Crypto Perps'

/** Classify a canonical symbol (e.g. 'BRN/USDT:USDT') into a display section. */
export function classifySymbol(sym: string): SymbolSection {
  const base = sym.split('/')[0]
  for (const section of SYMBOL_SECTIONS) {
    if (section.bases.length && (section.bases as readonly string[]).includes(base)) {
      return section.label as SymbolSection
    }
  }
  return 'Crypto Perps'
}

export const INSTRUMENT_COLORS: Record<string, string> = {
  // Energy
  BRN:    '#c47a35',
  WTI:    '#8b4513',
  USOIL:  '#8b4513',
  UKOIL:  '#c47a35',
  BRENT:  '#c47a35',
  NATGAS: '#4682b4',
  NGAS:   '#4682b4',
  COPPER:    '#b87333',
  ALUMINIUM: '#8ca0b3',
  WHEAT:     '#d9a441',
  CORN:      '#e1c340',
  URANIUM:   '#3fae5a',
  TTF:       '#5b9bd5',
  // Precious metals
  XAU:    '#ffd700',
  XAG:    '#adb5bd',
  XPT:    '#a8d8ea',
  XPD:    '#9b7bb5',
  // US Market
  NVDA:   '#76b900',
  QQQ:    '#3f51b5',
  SPY:    '#003f87',
  AAPL:   '#888888',
  TSLA:   '#cc0000',
  AMZN:   '#ff9900',
  MSFT:   '#00a4ef',
  GOOGL:  '#4285f4',
  META:   '#0668e1',
  SPCX:   '#005288',
  // Korean stocks
  SKHYNIX: '#1a73e8',
  SAMSUNG: '#1428a0',
  HYUNDAI: '#002c5f',
}

/** BTC/USDT → BTC/USDT   XAU/USDT:USDT → XAU/USDT PERP */
export function formatSymbol(sym: string): string {
  if (!sym.includes(':')) return sym
  const [base] = sym.split('/')
  return `${base}/USDT PERP`
}

/** Canonical Redis/WebSocket channel for a symbol */
export function symbolChannel(sym: string): string {
  return `prices:${sym.replace('/', '_').replace(':', '_')}`
}

export { fmtVolume } from '../utils/format'

// ── Historical OHLCV types ───────────────────────────────────────────────────

export interface DailyCandle {
  ts:           string
  symbol:       string
  exchange:     string
  open:         number
  high:         number
  low:          number
  close:        number
  base_volume:  number
  quote_volume: number
}

export interface HistoryMetrics {
  symbol:         string
  adtv_ytd:       number | null
  ytd_days:       number
  adtv_week:      number | null
  week_days:      number
  adtv_last_week: number | null
  wow_pct:        number | null
  last_updated:   string | null
}

export interface HistoryMetricsByExchange extends HistoryMetrics {
  exchange: string
}

/** Format bytes to human-readable string */
export function fmtBytes(b: number): string {
  if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB`
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`
  if (b >= 1e3) return `${(b / 1e3).toFixed(0)} KB`
  return `${b} B`
}
