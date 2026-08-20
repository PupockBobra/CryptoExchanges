import { useEffect, useMemo, useRef, useState } from 'react'
import Plotly from 'plotly.js-dist-min'
import { RefreshCw, Download, Search, X } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { formatSymbol } from '../types'
import { fetchJson } from '../utils/api'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/reports'

type Metric   = 'volume' | 'open_interest' | 'price' | 'funding'
type Agg      = 'daily' | 'weekly' | 'monthly'
type Currency = 'rub' | 'usd'
type View     = 'chart' | 'stacked' | 'table' | 'pie'

const VIEWS: { id: View; label: string }[] = [
  { id: 'chart',   label: 'Chart' },
  { id: 'stacked', label: 'Stacked' },
  { id: 'table',   label: 'Table' },
  { id: 'pie',     label: 'Pie' },
]

/** Filled button when selected, outlined otherwise — makes the active choice obvious. */
const selCls = (active: boolean) => (active ? 'btn-primary' : 'btn-secondary')

const METRICS: { id: Metric; label: string; money: boolean }[] = [
  { id: 'volume',        label: 'Volume',        money: true  },
  { id: 'open_interest', label: 'Open Interest', money: true  },
  { id: 'price',         label: 'Close Price',   money: false },
  { id: 'funding',       label: 'Funding Rate',  money: false },
]

interface Options {
  metric:      string
  instruments: string[]
  exchanges:   string[]
  date_min:    string | null
  date_max:    string | null
}

interface Leaf { symbol: string; exchange: string; label: string }
interface ClassNode { id: string; label: string; instruments: Leaf[] }
interface ExchangeNode { id: string; label: string; children: ClassNode[] }
interface TreeGroup { id: string; label: string; children: (ExchangeNode | ClassNode)[] }

const isExchangeNode = (n: ExchangeNode | ClassNode): n is ExchangeNode =>
  (n as ExchangeNode).children !== undefined

const pairKey = (l: Leaf) => `${l.exchange}~${l.symbol}`

interface Row {
  bucket:       string
  bucket_label: string
  symbol:       string
  exchange:     string
  value:        number
}

const FONT_FAMILY = 'Inter, system-ui, sans-serif'

// Distinct series palette (kept local so this page needs no shared refactor).
const PALETTE = [
  '#f0b90b', '#0052ff', '#ff6b35', '#0ecb81', '#00e5ff', '#d52b1e',
  '#a855f7', '#ec4899', '#14b8a6', '#f97316', '#84cc16', '#6366f1',
  '#eab308', '#06b6d4', '#ef4444', '#22c55e', '#8b5cf6', '#f43f5e',
]

function themeTokens(theme: 'dark' | 'light') {
  if (theme === 'light') return {
    bg: '#ffffff', paper: '#f8fafc', grid: '#e2e8f0',
    text: '#64748b', title: '#1e293b',
    hover: '#ffffff', hoverBorder: '#e2e8f0', hoverText: '#1e293b',
  }
  return {
    bg: '#0f1117', paper: '#1a1d27', grid: '#1f2937',
    text: '#9ca3af', title: '#e2e8f0',
    hover: '#21263a', hoverBorder: '#2d3148', hoverText: '#e2e8f0',
  }
}

const PLOTLY_CONFIG: Partial<Plotly.Config> = {
  displayModeBar: true,
  modeBarButtonsToRemove: [
    'select2d', 'lasso2d', 'autoScale2d', 'hoverClosestCartesian',
    'hoverCompareCartesian', 'toggleSpikelines',
  ] as Plotly.ModeBarDefaultButtons[],
  displaylogo: false,
  responsive: true,
}

function seriesKey(r: Row) { return `${r.symbol}|${r.exchange}` }

/** Compact money scaling: pick B or M by the largest value in the set. */
function moneyScale(max: number): { div: number; suffix: string } {
  return max >= 1e9 ? { div: 1e9, suffix: 'B' } : { div: 1e6, suffix: 'M' }
}

function todayISO() { return new Date().toISOString().slice(0, 10) }
function daysAgoISO(n: number) {
  const d = new Date(); d.setDate(d.getDate() - n)
  return d.toISOString().slice(0, 10)
}

export function CustomReport() {
  const theme = useTheme()

  const [metric,   setMetric]   = useState<Metric>('volume')
  const [options,  setOptions]  = useState<Options | null>(null)
  const [tree,     setTree]     = useState<TreeGroup[]>([])
  const [selPairs, setSelPairs] = useState<Set<string>>(new Set())
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [from,     setFrom]     = useState(daysAgoISO(30))
  const [to,       setTo]       = useState(todayISO())
  const [agg,      setAgg]      = useState<Agg>('daily')
  const [currency, setCurrency] = useState<Currency>('rub')
  const [view,     setView]     = useState<View>('chart')
  const [instrFilter, setInstrFilter] = useState('')

  const [rows,    setRows]    = useState<Row[]>([])
  // Currency the current `rows` were actually fetched in — toggling the
  // Currency buttons after a build must not relabel old data as ₽/$.
  const [builtCurrency, setBuiltCurrency] = useState<Currency>('rub')
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState<string | null>(null)

  const divRef = useRef<HTMLDivElement>(null)
  const isMoney = metric === 'volume' || metric === 'open_interest'

  // Load the instrument tree + date bounds whenever the metric changes.
  useEffect(() => {
    let cancelled = false
    Promise.all([
      fetchJson<TreeGroup[]>(`${API}/tree?metric=${metric}`),
      fetchJson<Options>(`${API}/options?metric=${metric}`),
    ])
      .then(([tr, opt]) => {
        if (cancelled) return
        setTree(tr)
        setOptions(opt)
        setSelPairs(new Set())
        setExpanded(new Set())
        setInstrFilter('')
        setRows([])
        setError(null)
        if (opt.date_max) setTo(opt.date_max)
        if (opt.date_min) {
          const wanted = daysAgoISO(30)
          setFrom(wanted < opt.date_min ? opt.date_min : wanted)
        }
      })
      .catch(e => { if (!cancelled) setError(String(e)) })
    return () => { cancelled = true }
  }, [metric])

  const build = async () => {
    if (!selPairs.size) { setError('Select at least one instrument'); return }
    setLoading(true); setError(null)
    try {
      const params = new URLSearchParams({
        metric,
        pairs: Array.from(selPairs).join(','),
        from, to, agg,
      })
      if (isMoney) params.set('currency', currency)
      const data = await fetchJson<Row[]>(`${API}/data?${params.toString()}`)
      setRows(data)
      setBuiltCurrency(currency)
      if (!data.length) setError('No data for this selection')
    } catch (e) {
      setError(String(e))
      setRows([])
    } finally {
      setLoading(false)
    }
  }

  // Shared pivot used by every view (chart / stacked / table / pie).
  const summary = useMemo(() => {
    if (!rows.length) return null
    const buckets = Array.from(new Set(rows.map(r => r.bucket))).sort()
    const labelByBucket = new Map(rows.map(r => [r.bucket, r.bucket_label]))
    const x = buckets.map(b => labelByBucket.get(b) ?? b)
    const keys = Array.from(new Set(rows.map(seriesKey)))
    const symbols   = new Set(rows.map(r => r.symbol))
    const exchanges = new Set(rows.map(r => r.exchange))
    const nameOf = (r: Row) => {
      const sym = formatSymbol(r.symbol)
      if (symbols.size === 1) return r.exchange
      if (exchanges.size === 1) return sym
      return `${sym} · ${r.exchange}`
    }
    const valueAt = new Map(rows.map(r => [`${seriesKey(r)}@${r.bucket}`, r.value]))
    const series = keys.map((k, i) => {
      const rs = rows.filter(r => seriesKey(r) === k)
      const sum = rs.reduce((a, r) => a + r.value, 0)
      return {
        key: k,
        name: nameOf(rs[0]),
        exchange: rs[0].exchange,
        color: PALETTE[i % PALETTE.length],
        // Pie needs one number per series: total for money, average for price/funding.
        agg: isMoney ? sum : sum / rs.length,
      }
    })
    return { buckets, x, keys, valueAt, series, maxVal: Math.max(0, ...rows.map(r => r.value)) }
  }, [rows, isMoney])

  const cur = builtCurrency === 'rub' ? '₽' : '$'

  const fmtCell = (v: number | undefined) => {
    if (v == null) return '—'
    if (metric === 'funding') return `${(v * 100).toFixed(4)}%`
    if (metric === 'price')   return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
    return `${cur}${Math.round(v).toLocaleString('en-US')}`
  }

  // Render the Plotly view (chart / stacked / pie); table is plain HTML below.
  // The div stays mounted at all times (hidden in table view) so the ref is
  // stable, and we purge before every render — switching between a pie
  // (non-cartesian) and a bar/scatter chart on the same node otherwise throws.
  useEffect(() => {
    const el = divRef.current
    if (!el) return
    Plotly.purge(el)
    if (!summary || view === 'table') return

    const t = themeTokens(theme)

    if (view === 'pie') {
      const trace: Plotly.Data = {
        type: 'pie',
        labels: summary.series.map(s => s.name),
        values: summary.series.map(s => Math.abs(s.agg)),
        marker: { colors: summary.series.map(s => s.color) },
        textinfo: 'label+percent',
        hovertemplate: `<b>%{label}</b>: %{value:,.2f} (%{percent})<extra></extra>`,
        sort: true,
      }
      Plotly.react(el, [trace], {
        paper_bgcolor: t.paper,
        margin: { l: 8, r: 8, t: 8, b: 8 },
        showlegend: true,
        legend: { font: { color: t.text, size: 11, family: FONT_FAMILY } },
        hoverlabel: { bgcolor: t.hover, bordercolor: t.hoverBorder, font: { color: t.hoverText, size: 12, family: FONT_FAMILY } },
      }, PLOTLY_CONFIG)
      return
    }

    // chart | stacked
    const { div, suffix } = isMoney ? moneyScale(summary.maxVal) : { div: 1, suffix: '' }
    const traces: Plotly.Data[] = summary.series.map(s => {
      const y = summary.buckets.map(b => {
        const v = summary.valueAt.get(`${s.key}@${b}`)
        return v == null ? null : v / div
      })
      if (isMoney) {
        return {
          type: 'bar', name: s.name, x: summary.x, y,
          marker: { color: s.color, opacity: 0.85 },
          hovertemplate: `<b>${s.name}</b>: ${cur}%{y:.2f}${suffix}<extra></extra>`,
        } satisfies Plotly.Data
      }
      const fmt = metric === 'funding'
        ? `<b>${s.name}</b>: %{customdata:.4f}%<extra></extra>`
        : `<b>${s.name}</b>: %{y:.4f}<extra></extra>`
      return {
        type: 'scatter', mode: 'lines+markers', name: s.name, x: summary.x,
        y: metric === 'funding' ? y.map(v => v == null ? null : v * 100) : y,
        customdata: metric === 'funding' ? y.map(v => v == null ? null : v * 100) : undefined,
        line: { color: s.color, width: 2 }, marker: { color: s.color, size: 5 },
        hovertemplate: fmt,
      } satisfies Plotly.Data
    })

    const yTitle = isMoney
      ? `${METRICS.find(m => m.id === metric)!.label} (${cur}${suffix})`
      : metric === 'funding' ? 'Funding rate (%)' : 'Close price'

    const layout: Partial<Plotly.Layout> = {
      barmode: view === 'stacked' ? 'stack' : 'group',
      paper_bgcolor: t.paper,
      plot_bgcolor:  t.bg,
      margin: { l: 72, r: 16, t: 16, b: 110 },
      legend: {
        orientation: 'h', x: 0, y: -0.28,
        font: { color: t.text, size: 11, family: FONT_FAMILY },
        bgcolor: 'transparent',
      },
      xaxis: {
        tickangle: -40,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid, showgrid: false,
      },
      yaxis: {
        title: { text: yTitle, font: { color: t.text, size: 11, family: FONT_FAMILY }, standoff: 14 },
        automargin: true,
        tickfont: { color: t.text, size: 10, family: FONT_FAMILY },
        gridcolor: t.grid, linecolor: t.grid,
        tickprefix: isMoney ? cur : '',
        ticksuffix: isMoney ? suffix : (metric === 'funding' ? '%' : ''),
      },
      hoverlabel: {
        bgcolor: t.hover, bordercolor: t.hoverBorder,
        font: { color: t.hoverText, size: 12, family: FONT_FAMILY },
      },
      hovermode: 'x unified',
    }

    Plotly.react(el, traces, layout, PLOTLY_CONFIG)
  }, [summary, theme, metric, isMoney, view, cur])

  useEffect(() => {
    const el = divRef.current
    return () => { if (el) Plotly.purge(el) }
  }, [])

  const exportCsv = () => {
    if (!rows.length) return
    const buckets = Array.from(new Set(rows.map(r => r.bucket))).sort()
    const keys = Array.from(new Set(rows.map(seriesKey)))
    const byKeyBucket = new Map(rows.map(r => [`${seriesKey(r)}@${r.bucket}`, r.value]))
    const header = ['Date', ...keys].join(',')
    const lines = buckets.map(b =>
      [b, ...keys.map(k => byKeyBucket.get(`${k}@${b}`) ?? '')].join(',')
    )
    const content = `# ${metric} report (${agg}${isMoney ? ', ' + builtCurrency : ''})\n${[header, ...lines].join('\n')}`
    const blob = new Blob(['﻿' + content], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `report-${metric}-${agg}.csv`; a.click()
    URL.revokeObjectURL(url)
  }

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, v: string) => {
    const next = new Set(set)
    next.has(v) ? next.delete(v) : next.add(v)
    setter(next)
  }

  // ── Tree picker helpers ──────────────────────────────────────────────────
  const toggleExpanded = (id: string) => toggle(expanded, setExpanded, id)
  const filtering = instrFilter.trim() !== ''
  const filterOk = (l: Leaf) => {
    const f = instrFilter.trim().toUpperCase()
    return !f || l.label.toUpperCase().includes(f) || l.symbol.toUpperCase().includes(f)
  }
  const setPairs = (keys: string[], on: boolean) => {
    const next = new Set(selPairs)
    keys.forEach(k => (on ? next.add(k) : next.delete(k)))
    setSelPairs(next)
  }

  const caret = (open: boolean) => (
    <span style={{ display: 'inline-block', width: 12, color: 'var(--muted)', fontSize: 10 }}>{open ? '▾' : '▸'}</span>
  )

  const renderClass = (cls: ClassNode, indent: number) => {
    const leaves = cls.instruments.filter(filterOk)
    if (!leaves.length) return null
    const keys = leaves.map(pairKey)
    const allOn = keys.every(k => selPairs.has(k))
    const someOn = !allOn && keys.some(k => selPairs.has(k))
    const open = expanded.has(cls.id) || filtering
    return (
      <div key={cls.id} style={{ marginLeft: indent }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}>
          <span style={{ cursor: 'pointer' }} onClick={() => toggleExpanded(cls.id)}>{caret(open)}</span>
          <input type="checkbox" checked={allOn}
            ref={el => { if (el) el.indeterminate = someOn }}
            onChange={() => setPairs(keys, !allOn)} />
          <span style={{ fontSize: 12, fontWeight: 600, cursor: 'pointer' }} onClick={() => toggleExpanded(cls.id)}>
            {cls.label} <span style={{ color: 'var(--muted)', fontWeight: 400 }}>({leaves.length})</span>
          </span>
        </div>
        {open && (
          <div style={{ marginLeft: 24 }}>
            {leaves.map(l => {
              const k = pairKey(l)
              return (
                <label key={k} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 4px', fontSize: 12, cursor: 'pointer' }}>
                  <input type="checkbox" checked={selPairs.has(k)} onChange={() => setPairs([k], !selPairs.has(k))} />
                  {l.label}
                </label>
              )
            })}
          </div>
        )}
      </div>
    )
  }

  const renderExchange = (ex: ExchangeNode) => {
    const classes = ex.children.map(c => renderClass(c, 24)).filter(Boolean)
    if (!classes.length) return null
    const open = expanded.has(ex.id) || filtering
    return (
      <div key={ex.id} style={{ marginLeft: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0', cursor: 'pointer', fontSize: 12, fontWeight: 600 }}
          onClick={() => toggleExpanded(ex.id)}>
          {caret(open)} {ex.label}
        </div>
        {open && <div>{classes}</div>}
      </div>
    )
  }

  const renderGroup = (g: TreeGroup) => {
    const kids = g.children.map(c => (isExchangeNode(c) ? renderExchange(c) : renderClass(c, 12))).filter(Boolean)
    if (!kids.length) return null
    const open = expanded.has(g.id) || filtering
    return (
      <div key={g.id} style={{ marginBottom: 2 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0', cursor: 'pointer', fontSize: 12, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.04em' }}
          onClick={() => toggleExpanded(g.id)}>
          {caret(open)} {g.label}
        </div>
        {open && <div>{kids}</div>}
      </div>
    )
  }

  return (
    <div>
      <div className="page-toolbar">
        <h1>Custom Report</h1>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 'auto' }}>
          Pick a metric, drill instruments by source → exchange → asset class, set a range · money metrics in RUB by default
        </div>
      </div>

      <div className="card" style={{ padding: 16, marginBottom: 16, display: 'grid', gap: 16 }}>
        {/* Metric */}
        <div>
          <div className="report-label">Metric</div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {METRICS.map(m => (
              <button
                key={m.id}
                className={selCls(metric === m.id)}
                onClick={() => setMetric(m.id)}
              >
                {m.label}
              </button>
            ))}
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 16 }}>
          {/* Instrument tree */}
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div className="report-label" style={{ marginBottom: 0 }}>
                Instruments {selPairs.size > 0 && `(${selPairs.size} selected)`}
              </div>
              {selPairs.size > 0 && (
                <button
                  onClick={() => setSelPairs(new Set())}
                  style={{
                    background: 'transparent', border: 'none', cursor: 'pointer',
                    color: 'var(--muted)', fontSize: 11, padding: 0,
                    display: 'flex', alignItems: 'center', gap: 3,
                  }}
                  onMouseEnter={e => (e.currentTarget.style.color = 'var(--fg)')}
                  onMouseLeave={e => (e.currentTarget.style.color = 'var(--muted)')}
                >
                  <X size={11} /> clear selection
                </button>
              )}
            </div>
            <div style={{ position: 'relative', margin: '6px 0' }}>
              <Search size={13} style={{ position: 'absolute', left: 8, top: 9, color: 'var(--muted)' }} />
              <input
                value={instrFilter}
                onChange={e => setInstrFilter(e.target.value)}
                placeholder="Filter instruments…"
                style={{
                  width: '100%', padding: '6px 26px 6px 28px', fontSize: 13,
                  background: 'var(--bg)', border: '1px solid var(--border)',
                  borderRadius: 6, color: 'var(--text)',
                }}
              />
              {instrFilter && (
                <button
                  onClick={() => setInstrFilter('')}
                  title="Clear filter"
                  style={{
                    position: 'absolute', right: 6, top: 6, background: 'transparent',
                    border: 'none', cursor: 'pointer', color: 'var(--muted)', padding: 2,
                    display: 'flex', alignItems: 'center',
                  }}
                  onMouseEnter={e => (e.currentTarget.style.color = 'var(--fg)')}
                  onMouseLeave={e => (e.currentTarget.style.color = 'var(--muted)')}
                >
                  <X size={13} />
                </button>
              )}
            </div>
            <div style={{
              maxHeight: 260, overflowY: 'auto', border: '1px solid var(--border)',
              borderRadius: 6, padding: 8,
            }}>
              {!tree.length
                ? <div className="empty" style={{ padding: 8 }}>Loading…</div>
                : (() => {
                    const rendered = tree.map(renderGroup).filter(Boolean)
                    return rendered.length ? rendered : <div className="empty" style={{ padding: 8 }}>No matches</div>
                  })()}
            </div>
          </div>

          {/* Controls */}
          <div style={{ display: 'grid', gap: 14, alignContent: 'start' }}>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              <div>
                <div className="report-label">From</div>
                <input type="date" value={from} min={options?.date_min ?? undefined} max={to}
                  onChange={e => setFrom(e.target.value)} className="report-date" />
              </div>
              <div>
                <div className="report-label">To</div>
                <input type="date" value={to} min={from} max={options?.date_max ?? undefined}
                  onChange={e => setTo(e.target.value)} className="report-date" />
              </div>
            </div>

            <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap' }}>
              <div>
                <div className="report-label">Aggregation</div>
                <div style={{ display: 'flex', gap: 6 }}>
                  {(['daily', 'weekly', 'monthly'] as Agg[]).map(a => (
                    <button key={a} className={selCls(agg === a)}
                      style={{ fontSize: 12, padding: '3px 10px' }} onClick={() => setAgg(a)}>
                      {a}
                    </button>
                  ))}
                </div>
              </div>
              {isMoney && (
                <div>
                  <div className="report-label">Currency</div>
                  <div style={{ display: 'flex', gap: 6 }}>
                    {(['rub', 'usd'] as Currency[]).map(c => (
                      <button key={c} className={selCls(currency === c)}
                        style={{ fontSize: 12, padding: '3px 10px' }} onClick={() => setCurrency(c)}>
                        {c.toUpperCase()}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div>
              <div className="report-label">Display form</div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {VIEWS.map(v => (
                  <button key={v.id} className={selCls(view === v.id)}
                    style={{ fontSize: 12, padding: '3px 10px' }} onClick={() => setView(v.id)}>
                    {v.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button className="btn-primary" onClick={build} disabled={loading || !selPairs.size}
            style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <RefreshCw size={13} className={loading ? 'spin' : ''} />
            Build report
          </button>
          {error && <span style={{ fontSize: 12, color: '#ef4444' }}>{error}</span>}
        </div>
      </div>

      {rows.length > 0 && summary && (
        <div className="card" style={{ padding: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 6 }}>
            <button onClick={exportCsv} style={{
              background: 'transparent', border: 'none', cursor: 'pointer',
              color: 'var(--muted)', padding: '3px 6px', borderRadius: 4,
              display: 'flex', alignItems: 'center', gap: 4, fontSize: 11,
            }}>
              <Download size={12} /> export to csv
            </button>
          </div>

          {/* Plot node stays mounted (hidden in table view) for a stable ref. */}
          <div ref={divRef} style={{ width: '100%', height: view === 'table' ? 0 : 440, display: view === 'table' ? 'none' : 'block' }} />
          {view === 'table' && (
            <div style={{ overflowX: 'auto' }}>
              <table className="report-table">
                <thead>
                  <tr>
                    <th>Date</th>
                    {summary.series.map(s => <th key={s.key} style={{ textAlign: 'right' }}>{s.name}</th>)}
                    {isMoney && summary.series.length > 1 && <th style={{ textAlign: 'right' }}>Total</th>}
                  </tr>
                </thead>
                <tbody>
                  {summary.buckets.map((b, i) => {
                    const vals = summary.series.map(s => summary.valueAt.get(`${s.key}@${b}`))
                    const total = vals.reduce<number>((a, v) => a + (v ?? 0), 0)
                    return (
                      <tr key={b}>
                        <td>{summary.x[i]}</td>
                        {vals.map((v, j) => <td key={j} style={{ textAlign: 'right' }}>{fmtCell(v)}</td>)}
                        {isMoney && summary.series.length > 1 && <td style={{ textAlign: 'right', fontWeight: 600 }}>{fmtCell(total)}</td>}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
