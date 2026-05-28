import { useState, useEffect } from 'react'
import { Header }      from './components/Header'
import { Dashboard }   from './pages/Dashboard'
import { Instruments } from './pages/Instruments'
import { Exchanges }   from './pages/Exchanges'
import { History }     from './pages/History'
import { Analytics }   from './pages/Analytics'
import { News }        from './pages/News'
import { Funding }     from './pages/Funding'
import type { Page, Theme } from './components/Header'

const DEFAULT_SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XAU/USDT:USDT', 'XAG/USDT:USDT']

export default function App() {
  const [page,           setPage]    = useState<Page>('dashboard')
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
    fetch(`${apiBase}/api/prices/symbols`)
      .then((r) => r.json())
      .then((data) => {
        if (data.symbols?.length) {
          setSymbols(data.symbols)
          setSymbol((prev) => data.symbols.includes(prev) ? prev : data.symbols[0])
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
          {page === 'analytics'   && <Analytics />}
          {page === 'news'        && <News />}
          {page === 'funding'     && <Funding />}
        </div>
      </div>
    </div>
  )
}
