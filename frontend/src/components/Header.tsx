import { BarChart2, BarChart, Clock, Activity, Globe, List, Sun, Moon, Newspaper, Percent, Rocket, PieChart, TrendingUp, Landmark, CalendarRange, FileText, BookOpen } from 'lucide-react'
import { formatSymbol } from '../types'

export type Page = 'dashboard' | 'instruments' | 'exchanges' | 'history' | 'analytics' | 'daily-volume' | 'news' | 'funding' | 'launches' | 'tradfi-market-share' | 'open-interest' | 'spb-volume' | 'spb-weekly' | 'spb-market-share' | 'spb-open-interest' | 'spb-order-book' | 'custom-report'
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

type NavItem = { id: Page; label: string; Icon: React.ElementType }

const NAV_GROUPS: { group: string; items: NavItem[] }[] = [
  {
    group: 'Cryptoexchanges',
    items: [
      { id: 'analytics',           label: 'Weekly Performance',   Icon: BarChart2  },
      { id: 'daily-volume',        label: 'Daily Volume',         Icon: BarChart   },
      { id: 'open-interest',       label: 'Open Interest',        Icon: TrendingUp },
      { id: 'tradfi-market-share', label: 'TradFi Market Share',  Icon: PieChart   },
      { id: 'launches',            label: 'Futures Launches',     Icon: Rocket     },
      { id: 'history',             label: 'Historical Prices & Vols', Icon: Clock  },
      { id: 'dashboard',           label: 'Realtime Prices',      Icon: Activity   },
      { id: 'news',                label: 'News',                 Icon: Newspaper  },
      { id: 'funding',             label: 'Funding Rates',        Icon: Percent    },
      { id: 'exchanges',           label: 'Exchanges',            Icon: Globe      },
      { id: 'instruments',         label: 'Instruments',          Icon: List       },
    ],
  },
  {
    group: 'SPB',
    items: [
      { id: 'spb-weekly',          label: 'Weekly Performance',   Icon: CalendarRange },
      { id: 'spb-volume',          label: 'SPB Volume',           Icon: Landmark      },
      { id: 'spb-open-interest',   label: 'Open Interest',        Icon: TrendingUp    },
      { id: 'spb-order-book',      label: 'Order Book',           Icon: BookOpen      },
      { id: 'spb-market-share',    label: 'Market Share',         Icon: PieChart      },
    ],
  },
  {
    group: 'Reports',
    items: [
      { id: 'custom-report',       label: 'Custom Report',        Icon: FileText      },
    ],
  },
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

      {/* Main nav — grouped by market */}
      <nav className="sidebar-nav">
        {NAV_GROUPS.map(({ group, items }) => (
          <div key={group}>
            <div className="sidebar-section-label">{group}</div>
            {items.map(({ id, label, Icon }) => (
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
          </div>
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
