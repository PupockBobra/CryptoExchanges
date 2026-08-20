import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCw, Upload, Download, Flame } from 'lucide-react'
import { fetchJson } from '../utils/api'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/spb'

// Section order; tickers carry their group from the backend.
const GROUP_ORDER = ['US Market', 'Crypto']

interface FundingRow {
  date:       string   // 'YYYY-MM-DD'
  ticker:     string
  name:       string
  group:      string
  pct_year:   number | null
  pct_day:    number | null
  fund_curr:  number | null
  mean_price: number | null
  mean_index: number | null
}

type MetricKey = 'pct_year' | 'pct_day' | 'fund_curr' | 'mean_price' | 'mean_index'

interface Metric {
  key:       MetricKey
  label:     string
  diverging: boolean   // true → red/green around 0; false → per-row sequential
}

const METRICS: Metric[] = [
  { key: 'pct_year',   label: '% year',      diverging: true  },
  { key: 'pct_day',    label: '% day',       diverging: true  },
  { key: 'fund_curr',  label: 'Funding USD', diverging: true  },
  { key: 'mean_price', label: 'MeanPrice',   diverging: false },
  { key: 'mean_index', label: 'MeanIndex',   diverging: false },
]

// ── formatting ────────────────────────────────────────────────────────────────

function fmt(metric: MetricKey, v: number | null): string {
  if (v == null) return ''
  if (metric === 'pct_year') return v.toFixed(2)
  if (metric === 'pct_day')  return v.toFixed(4)
  if (metric === 'fund_curr') return v.toLocaleString('en-US', { maximumSignificantDigits: 4 })
  // price / index — magnitude-adaptive
  const a = Math.abs(v)
  const dp = a >= 100 ? 2 : a >= 1 ? 3 : 5
  return v.toFixed(dp)
}

function dateLabel(iso: string): string {
  const [, m, d] = iso.split('-')
  return `${d}.${m}`
}

// ── heatmap colour ────────────────────────────────────────────────────────────

/** 90th-percentile of |values| → clip scale, so a few outliers don't wash out
 *  the rest of the diverging heatmap. */
function clipScale(vals: number[]): number {
  const abs = vals.filter(v => Number.isFinite(v)).map(Math.abs).sort((a, b) => a - b)
  if (!abs.length) return 1
  const p = abs[Math.min(abs.length - 1, Math.floor(abs.length * 0.9))]
  return p > 0 ? p : (abs[abs.length - 1] || 1)
}

function divergingBg(v: number | null, scale: number): string {
  if (v == null) return 'transparent'
  const t = Math.max(-1, Math.min(1, v / scale))
  const a = (Math.abs(t) * 0.5).toFixed(3)
  return t >= 0 ? `rgba(16,185,129,${a})` : `rgba(239,68,68,${a})`
}

function seqBg(v: number | null, min: number, max: number): string {
  if (v == null || max <= min) return 'transparent'
  const t = (v - min) / (max - min)
  return `rgba(0,163,196,${(t * 0.55).toFixed(3)})`
}

// ── upload ────────────────────────────────────────────────────────────────────

interface UploadResult {
  saved:    number
  files:    number
  accepted: number
  results:  { name: string; ok: boolean; rows: number; error?: string; date?: string }[]
}

// ── page ──────────────────────────────────────────────────────────────────────

export function SPBFunding() {
  const [rows, setRows]       = useState<FundingRow[]>([])
  const [loading, setLoading] = useState(true)
  const [metric, setMetric]   = useState<MetricKey>('pct_year')
  const [heatmap, setHeatmap] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [report, setReport]   = useState<UploadResult | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const load = async () => {
    setLoading(true)
    try {
      setRows(await fetchJson<FundingRow[]>(`${API}/funding`))
    } catch (e) {
      console.error('SPBFunding: failed to load funding', e)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [])

  const upload = async (files: FileList | File[]) => {
    const list = Array.from(files).filter(f => f.name.toLowerCase().endsWith('.csv'))
    if (!list.length) return
    setUploading(true)
    setReport(null)
    try {
      const payload = {
        files: await Promise.all(list.map(async f => ({ name: f.name, text: await f.text() }))),
      }
      const res = await fetchJson<UploadResult>(`${API}/funding/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      setReport(res)
      await load()
    } catch (e) {
      console.error('SPBFunding: upload failed', e)
      setReport({ saved: 0, files: list.length, accepted: 0,
        results: [{ name: 'upload', ok: false, rows: 0, error: String(e) }] })
    } finally {
      setUploading(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  // ── pivot ──────────────────────────────────────────────────────────────────
  const dates = useMemo(
    () => Array.from(new Set(rows.map(r => r.date))).sort(),
    [rows],
  )

  const tickers = useMemo(() => {
    const seen = new Map<string, { ticker: string; name: string; group: string }>()
    for (const r of rows) if (!seen.has(r.ticker)) seen.set(r.ticker, r)
    return Array.from(seen.values()).sort((a, b) => a.name.localeCompare(b.name))
  }, [rows])

  // (ticker,date) → value for the active metric
  const cell = useMemo(() => {
    const m = new Map<string, number | null>()
    for (const r of rows) m.set(`${r.ticker}|${r.date}`, r[metric])
    return m
  }, [rows, metric])

  const activeMetric = METRICS.find(x => x.key === metric)!

  // colour scales: diverging → one global clip; sequential → per-ticker min/max
  const globalScale = useMemo(() => {
    if (!activeMetric.diverging) return 1
    const vals: number[] = []
    for (const v of cell.values()) if (v != null) vals.push(v)
    return clipScale(vals)
  }, [cell, activeMetric])

  const rowRange = useMemo(() => {
    const m = new Map<string, { min: number; max: number }>()
    if (activeMetric.diverging) return m
    for (const t of tickers) {
      let mn = Infinity, mx = -Infinity
      for (const d of dates) {
        const v = cell.get(`${t.ticker}|${d}`)
        if (v != null) { mn = Math.min(mn, v); mx = Math.max(mx, v) }
      }
      if (mn !== Infinity) m.set(t.ticker, { min: mn, max: mx })
    }
    return m
  }, [cell, tickers, dates, activeMetric])

  const bgFor = (ticker: string, v: number | null): string => {
    if (!heatmap) return 'transparent'
    if (activeMetric.diverging) return divergingBg(v, globalScale)
    const r = rowRange.get(ticker)
    return r ? seqBg(v, r.min, r.max) : 'transparent'
  }

  const exportCsv = () => {
    const head = ['ticker', 'name', 'group', ...dates.map(dateLabel)]
    const lines = [head.join(',')]
    for (const g of GROUP_ORDER) {
      for (const t of tickers.filter(x => x.group === g)) {
        const vals = dates.map(d => {
          const v = cell.get(`${t.ticker}|${d}`)
          return v == null ? '' : String(v)
        })
        lines.push([t.ticker, `"${t.name}"`, g, ...vals].join(','))
      }
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `spb-funding-${metric}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const hasData = tickers.length > 0 && dates.length > 0

  return (
    <div>
      <div className="page-toolbar">
        <h1>SPB Funding</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          СПБ Биржа perpetual futures · daily funding · {dates.length} дней
        </div>
        <button className="btn-secondary" style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={load} disabled={loading}>
          <RefreshCw size={13} className={loading ? 'spin' : ''} />
          Refresh
        </button>
      </div>

      {/* controls */}
      <div className="card" style={{ marginBottom: 16, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 16, padding: '12px 16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.08em', color: 'var(--muted)' }}>
            Метрика
          </span>
          <div style={{ display: 'flex', gap: 4 }}>
            {METRICS.map(m => (
              <button key={m.key} onClick={() => setMetric(m.key)}
                className="btn-secondary"
                style={{
                  padding: '5px 10px', fontSize: 12,
                  background: metric === m.key ? '#00a3c4' : undefined,
                  color: metric === m.key ? '#fff' : undefined,
                  borderColor: metric === m.key ? '#00a3c4' : undefined,
                }}>
                {m.label}
              </button>
            ))}
          </div>
        </div>

        <button className="btn-secondary"
          onClick={() => setHeatmap(h => !h)}
          style={{
            display: 'flex', alignItems: 'center', gap: 6, padding: '5px 10px', fontSize: 12,
            background: heatmap ? '#00a3c4' : undefined,
            color: heatmap ? '#fff' : undefined,
            borderColor: heatmap ? '#00a3c4' : undefined,
          }}>
          <Flame size={13} /> Heatmap
        </button>

        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
          <button className="btn-secondary" onClick={exportCsv} disabled={!hasData}
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 10px', fontSize: 12 }}>
            <Download size={13} /> Export CSV
          </button>
          <button className="btn-secondary" onClick={() => fileInput.current?.click()} disabled={uploading}
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '5px 10px', fontSize: 12 }}>
            <Upload size={13} className={uploading ? 'spin' : ''} /> Загрузить CSV
          </button>
          <input ref={fileInput} type="file" accept=".csv" multiple style={{ display: 'none' }}
            onChange={e => e.target.files && upload(e.target.files)} />
        </div>
      </div>

      {/* drop zone + report */}
      <div
        className={`funding-drop${dragOver ? ' drag' : ''}`}
        style={{ marginBottom: 16, padding: '10px 16px', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}
        onDragOver={e => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={e => { e.preventDefault(); setDragOver(false); if (e.dataTransfer.files) upload(e.dataTransfer.files) }}
      >
        <Upload size={14} style={{ color: 'var(--muted)' }} />
        <span style={{ fontSize: 12, color: 'var(--muted)' }}>
          Перетащите сюда файлы «Итоговый фандинг DD-MM-YYYY.csv» (можно несколько) или нажмите «Загрузить CSV».
          Повторная загрузка того же дня перезапишет данные.
        </span>
        {report && (
          <span style={{ fontSize: 12, marginLeft: 'auto',
            color: report.accepted === report.files ? '#10b981' : '#f59e0b' }}>
            Принято {report.accepted}/{report.files} файлов · {report.saved} строк
            {report.results.some(r => !r.ok) && (
              <> · пропущены: {report.results.filter(r => !r.ok).map(r => `${r.name} (${r.error})`).join('; ')}</>
            )}
          </span>
        )}
      </div>

      {loading ? (
        <p className="empty">Loading funding data…</p>
      ) : !hasData ? (
        <p className="empty">Данных пока нет — загрузите CSV-файлы фандинга.</p>
      ) : (
        <div className="funding-wrap">
          <table className="funding-table">
            <thead>
              <tr>
                <th className="tk">Инструмент</th>
                {dates.map(d => <th key={d}>{dateLabel(d)}</th>)}
              </tr>
            </thead>
            <tbody>
              {GROUP_ORDER.map(group => {
                const groupTickers = tickers.filter(t => t.group === group)
                if (!groupTickers.length) return null
                return (
                  <Fragment key={group}>
                    <tr className="funding-group">
                      <td className="tk">{group}</td>
                      <td colSpan={dates.length} />
                    </tr>
                    {groupTickers.map(t => (
                      <tr key={t.ticker}>
                        <td className="tk">
                          {t.ticker.replace(/perpA$/, '')}
                          <span className="tk-sub"> · {t.name}</span>
                        </td>
                        {dates.map(d => {
                          const v = cell.get(`${t.ticker}|${d}`) ?? null
                          return (
                            <td key={d}
                              className={v == null ? 'empty' : undefined}
                              style={{ background: bgFor(t.ticker, v) }}>
                              {v == null ? '·' : fmt(metric, v)}
                            </td>
                          )
                        })}
                      </tr>
                    ))}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
