import { useState, useEffect, lazy, Suspense } from 'react'
import { Header }      from './components/Header'

// Pages are code-split: most of them pull in Plotly (~4 MB of the bundle), and
// eagerly importing every page meant the first visit downloaded all of it before
// anything rendered — including the charting library for pages that show tables.
// Vite emits one chunk per page plus a shared Plotly chunk, fetched on demand.
const Dashboard         = lazy(() => import('./pages/Dashboard').then(m => ({ default: m.Dashboard })))
const Instruments       = lazy(() => import('./pages/Instruments').then(m => ({ default: m.Instruments })))
const Exchanges         = lazy(() => import('./pages/Exchanges').then(m => ({ default: m.Exchanges })))
const History           = lazy(() => import('./pages/History').then(m => ({ default: m.History })))
const Analytics         = lazy(() => import('./pages/Analytics').then(m => ({ default: m.Analytics })))
const DailyVolume       = lazy(() => import('./pages/DailyVolume').then(m => ({ default: m.DailyVolume })))
const HourlyVolume      = lazy(() => import('./pages/HourlyVolume').then(m => ({ default: m.HourlyVolume })))
const SPBVolume         = lazy(() => import('./pages/SPBVolume').then(m => ({ default: m.SPBVolume })))
const SPBWeekly         = lazy(() => import('./pages/SPBWeekly').then(m => ({ default: m.SPBWeekly })))
const SPBMarketShare    = lazy(() => import('./pages/SPBMarketShare').then(m => ({ default: m.SPBMarketShare })))
const SPBOpenInterest   = lazy(() => import('./pages/SPBOpenInterest').then(m => ({ default: m.SPBOpenInterest })))
const SPBOrderBook      = lazy(() => import('./pages/SPBOrderBook').then(m => ({ default: m.SPBOrderBook })))
const SPBFunding        = lazy(() => import('./pages/SPBFunding').then(m => ({ default: m.SPBFunding })))
const SPBScreenshot     = lazy(() => import('./pages/SPBScreenshot').then(m => ({ default: m.SPBScreenshot })))
const TradFiMarketShare = lazy(() => import('./pages/TradFiMarketShare').then(m => ({ default: m.TradFiMarketShare })))
const Launches          = lazy(() => import('./pages/Launches').then(m => ({ default: m.Launches })))
const News              = lazy(() => import('./pages/News').then(m => ({ default: m.News })))
const Funding           = lazy(() => import('./pages/Funding').then(m => ({ default: m.Funding })))
const OpenInterest      = lazy(() => import('./pages/OpenInterest').then(m => ({ default: m.OpenInterest })))
const CustomReport      = lazy(() => import('./pages/CustomReport').then(m => ({ default: m.CustomReport })))
const CryptoIndex       = lazy(() => import('./pages/CryptoIndex').then(m => ({ default: m.CryptoIndex })))
const MM                = lazy(() => import('./pages/MM').then(m => ({ default: m.MM })))
const MMDetect          = lazy(() => import('./pages/MMDetect').then(m => ({ default: m.MMDetect })))
const OKR               = lazy(() => import('./pages/OKR').then(m => ({ default: m.OKR })))
import type { Page, Theme }  from './components/Header'
import { MM_TABS } from './components/Header'
import { fetchJson } from './utils/api'

const DEFAULT_SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XAU/USDT:USDT', 'XAG/USDT:USDT']

export default function App() {
  const [page,           setPage]    = useState<Page>('analytics')
  const [symbols,        setSymbols] = useState<string[]>(DEFAULT_SYMBOLS)
  const [selectedSymbol, setSymbol]  = useState(DEFAULT_SYMBOLS[0])
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem('theme') as Theme) ?? 'light'
  )

  // Apply theme to root element and persist
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('theme', theme)
  }, [theme])

  const toggleTheme = () => setTheme(t => t === 'dark' ? 'light' : 'dark')

  // Load active symbols from backend
  const loadSymbols = () => {
    const apiBase = import.meta.env.VITE_API_URL ?? ''
    fetchJson<{ symbols?: string[] }>(`${apiBase}/api/prices/symbols`)
      .then((data) => {
        const syms = data.symbols
        if (syms?.length) {
          setSymbols(syms)
          setSymbol((prev) => syms.includes(prev) ? prev : syms[0])
        }
      })
      .catch(() => { /* keep defaults */ })
  }

  useEffect(() => { loadSymbols() }, [])

  const handlePageChange = (p: Page) => {
    setPage(p)
    if (p === 'dashboard') loadSymbols()
  }

  return (
    <div className="app">
      <Header
        page={page}
        onPageChange={handlePageChange}
        selectedSymbol={selectedSymbol}
        symbols={symbols}
        onSymbolChange={setSymbol}
        theme={theme}
        onThemeToggle={toggleTheme}
      />
      <div className="main-content">
        <div className="page-body">
          <Suspense fallback={<p className="empty">Loading…</p>}>
          {page === 'dashboard'   && <Dashboard symbol={selectedSymbol} />}
          {page === 'instruments' && <Instruments />}
          {page === 'exchanges'   && <Exchanges />}
          {page === 'history'     && <History />}
          {page === 'okr'                 && <OKR />}
          {page === 'analytics'           && <Analytics />}
          {page === 'daily-volume'        && <DailyVolume />}
          {page === 'hourly-volume'       && <HourlyVolume />}
          {page === 'spb-volume'          && <SPBVolume />}
          {page === 'spb-weekly'          && <SPBWeekly />}
          {page === 'spb-market-share'    && <SPBMarketShare />}
          {page === 'spb-open-interest'   && <SPBOpenInterest />}
          {page === 'spb-order-book'      && <SPBOrderBook />}
          {page === 'spb-funding'         && <SPBFunding />}
          {page === 'spb-screenshot'      && <SPBScreenshot />}
          {page === 'tradfi-market-share' && <TradFiMarketShare />}
          {page === 'launches'            && <Launches />}
          {page === 'news'        && <News />}
          {page === 'funding'        && <Funding />}
          {page === 'open-interest'  && <OpenInterest />}
          {page === 'crypto-index'   && <CryptoIndex />}
          {page === 'custom-report'  && <CustomReport />}
          {page === 'spb-mm-presence' && <MMDetect />}
          {(() => {
            const tab = MM_TABS.find(t => t.id === page)
            return tab ? <MM group={tab.group} label={tab.label} /> : null
          })()}
          </Suspense>
        </div>
      </div>
    </div>
  )
}
