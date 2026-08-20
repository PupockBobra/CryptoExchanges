import { useState, useEffect } from 'react'
import { Header }      from './components/Header'
import { Dashboard }   from './pages/Dashboard'
import { Instruments } from './pages/Instruments'
import { Exchanges }   from './pages/Exchanges'
import { History }     from './pages/History'
import { Analytics }          from './pages/Analytics'
import { DailyVolume }        from './pages/DailyVolume'
import { SPBVolume }          from './pages/SPBVolume'
import { SPBWeekly }          from './pages/SPBWeekly'
import { SPBMarketShare }     from './pages/SPBMarketShare'
import { SPBOpenInterest }    from './pages/SPBOpenInterest'
import { SPBOrderBook }       from './pages/SPBOrderBook'
import { TradFiMarketShare }  from './pages/TradFiMarketShare'
import { Launches }           from './pages/Launches'
import { News }        from './pages/News'
import { Funding }     from './pages/Funding'
import { OpenInterest } from './pages/OpenInterest'
import { CustomReport } from './pages/CustomReport'
import type { Page, Theme }  from './components/Header'
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
          {page === 'dashboard'   && <Dashboard symbol={selectedSymbol} />}
          {page === 'instruments' && <Instruments />}
          {page === 'exchanges'   && <Exchanges />}
          {page === 'history'     && <History />}
          {page === 'analytics'           && <Analytics />}
          {page === 'daily-volume'        && <DailyVolume />}
          {page === 'spb-volume'          && <SPBVolume />}
          {page === 'spb-weekly'          && <SPBWeekly />}
          {page === 'spb-market-share'    && <SPBMarketShare />}
          {page === 'spb-open-interest'   && <SPBOpenInterest />}
          {page === 'spb-order-book'      && <SPBOrderBook />}
          {page === 'tradfi-market-share' && <TradFiMarketShare />}
          {page === 'launches'            && <Launches />}
          {page === 'news'        && <News />}
          {page === 'funding'        && <Funding />}
          {page === 'open-interest'  && <OpenInterest />}
          {page === 'custom-report'  && <CustomReport />}
        </div>
      </div>
    </div>
  )
}
