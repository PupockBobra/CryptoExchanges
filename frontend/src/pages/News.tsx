import { useState, useEffect, useCallback } from 'react'
import { RefreshCw, ExternalLink, Clock } from 'lucide-react'
import { timeAgo } from '../utils/format'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/news'

// ── Types ─────────────────────────────────────────────────────────────────────

interface Article {
  id:           string
  title:        string
  url:          string
  description:  string
  published_at: string
  author:       string
  categories:   string[]
  image_url:    string
  source:       string
}

// ── Article card ──────────────────────────────────────────────────────────────

function ArticleCard({ a }: { a: Article }) {
  return (
    <a
      href={a.url}
      target="_blank"
      rel="noopener noreferrer"
      className="news-card"
    >
      {a.image_url && (
        <div className="news-card-img">
          <img
            src={a.image_url}
            alt=""
            loading="lazy"
            onError={(e) => {
              const el = e.currentTarget
              el.parentElement!.style.display = 'none'
            }}
          />
        </div>
      )}

      <div className="news-card-body">
        {a.categories.length > 0 && (
          <div className="news-categories">
            {a.categories.slice(0, 3).map((c) => (
              <span key={c} className="news-tag">{c}</span>
            ))}
          </div>
        )}

        <h3 className="news-title">{a.title}</h3>

        {a.description && (
          <p className="news-desc">{a.description}</p>
        )}

        <div className="news-meta">
          {a.author && (
            <span className="news-author">{a.author}</span>
          )}
          <span className="news-time">
            <Clock size={11} />
            {timeAgo(a.published_at)}
          </span>
          <span className="news-source-badge">{a.source}</span>
          <ExternalLink size={11} style={{ marginLeft: 'auto', opacity: 0.4, flexShrink: 0 }} />
        </div>
      </div>
    </a>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export function News() {
  const [articles,       setArticles]       = useState<Article[]>([])
  const [loading,        setLoading]        = useState(true)
  const [lastSync,       setLastSync]       = useState<Date | null>(null)
  const [activeCategory, setActiveCategory] = useState('All')

  const load = useCallback(async (forceRefresh = false) => {
    setLoading(true)
    try {
      if (forceRefresh) {
        await fetch(`${API}/refresh`, { method: 'POST' })
      }
      const data = await fetch(`${API}?limit=100`).then((r) => r.json())
      setArticles(data.articles ?? [])
      setLastSync(new Date())
    } catch {
      // keep existing articles on transient error
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial load
  useEffect(() => { load() }, [load])

  // Auto-refresh every 15 minutes
  useEffect(() => {
    const id = setInterval(() => load(), 15 * 60 * 1000)
    return () => clearInterval(id)
  }, [load])

  // Collect all distinct categories from fetched articles
  const allCategories = [
    'All',
    ...Array.from(new Set(articles.flatMap((a) => a.categories))).sort(),
  ]

  const filtered =
    activeCategory === 'All'
      ? articles
      : articles.filter((a) => a.categories.includes(activeCategory))

  return (
    <div>
      {/* ── Toolbar ── */}
      <div className="page-toolbar">
        <h1>Crypto News</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Source: CoinDesk · refreshes every 15 min
          {lastSync && ` · loaded ${lastSync.toLocaleTimeString()}`}
        </div>
        <button
          className="btn-secondary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={() => load(true)}
          disabled={loading}
        >
          <RefreshCw size={13} className={loading ? 'spin' : ''} />
          Refresh
        </button>
      </div>

      {/* ── Category filter bar ── */}
      {!loading && allCategories.length > 1 && (
        <div className="news-category-bar">
          {allCategories.map((cat) => (
            <button
              key={cat}
              className={`news-cat-btn ${activeCategory === cat ? 'news-cat-btn--active' : ''}`}
              onClick={() => setActiveCategory(cat)}
            >
              {cat}
            </button>
          ))}
        </div>
      )}

      {/* ── Articles grid ── */}
      {loading ? (
        <p className="empty">Loading news…</p>
      ) : filtered.length === 0 ? (
        <p className="empty">No articles found</p>
      ) : (
        <div className="news-grid">
          {filtered.map((a) => (
            <ArticleCard key={a.id} a={a} />
          ))}
        </div>
      )}
    </div>
  )
}
