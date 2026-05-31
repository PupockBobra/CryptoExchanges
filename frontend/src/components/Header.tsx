import { BarChart2, BarChart, Clock, Activity, Globe, List, Sun, Moon, Newspaper, Percent, Rocket } from 'lucide-react'
import { formatSymbol } from '../types'

export type Page = 'dashboard' | 'instruments' | 'exchanges' | 'history' | 'analytics' | 'daily-volume' | 'news' | 'funding' | 'launches'
export type Theme = 'dark' | 'light'

interface Props {
  page:           Page
  onPageChange:   (p: Page) => void
  selectedSymbol: string
  symbols:        string[]
  onSymbolChange: (s: string) => void
  theme:          Theme
  onThemeToggle:  () => void
}

const NAV: { id: Page; label: string; Icon: React.ElementType }[] = [
  { id: 'analytics',     label: 'Weekly Performance',      Icon: BarChart2  },
  { id: 'daily-volume', label: 'Daily Volume',             Icon: BarChart   },
  { id: 'launches',     label: 'Futures Launches',         Icon: Rocket     },
  { id: 'history',     label: 'Historical Prices & Vols',  Icon: Clock      },
  { id: 'dashboard',   label: 'Realtime Prices',           Icon: Activity   },
  { id: 'news',        label: 'News',                      Icon: Newspaper  },
  { id: 'funding',     label: 'Funding Rates',             Icon: Percent    },
  { id: 'exchanges',   label: 'Exchanges',                 Icon: Globe      },
  { id: 'instruments', label: 'Instruments',               Icon: List       },
]

export function Header({
  page, onPageChange,
  selectedSymbol, symbols, onSymbolChange,
  theme, onThemeToggle,
}: Props) {
  return (
    <aside className="sidebar">
      {/* Brand */}
      <div className="sidebar-brand">
        <Activity size={18} />
        <span>Crypto Tracker</span>
      </div>

      {/* Main nav */}
      <nav className="sidebar-nav">
        {NAV.map(({ id, label, Icon }) => (
          <button
            key={id}
            className={`nav-btn ${page === id ? 'nav-btn--active' : ''}`}
            onClick={() => onPageChange(id)}
            title={label}
          >
            <Icon size={15} />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      {/* Symbol selector — only on Realtime Prices page */}
      {page === 'dashboard' && symbols.length > 0 && (
        <div className="sidebar-symbol-section">
          <div className="sidebar-symbol-label">Symbol</div>
          <div className="sidebar-symbol-tabs">
            {symbols.map((s) => (
              <button
                key={s}
                className={`sidebar-sym-btn ${s === selectedSymbol ? 'sidebar-sym-btn--active' : ''}`}
                onClick={() => onSymbolChange(s)}
                title={formatSymbol(s)}
              >
                {formatSymbol(s)}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="sidebar-spacer" />

      {/* Theme toggle */}
      <div className="sidebar-bottom">
        <button className="theme-toggle" onClick={onThemeToggle} title="Toggle theme">
          {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
          <span>{theme === 'dark' ? 'Light mode' : 'Dark mode'}</span>
        </button>
      </div>
    </aside>
  )
}
