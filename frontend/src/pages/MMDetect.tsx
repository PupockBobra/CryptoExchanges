// MM presence estimator (SPB perps) — "how much volume rests in the book, at
// what spread", inferred from captured order-book snapshots.
//
// Everything on this page is recomputed server-side from stored snapshots on
// every threshold change, so the sliders are the analysis, not a display filter:
// what counts as "persistent", "the same size" and "symmetric" is exactly what
// separates an estimate of a maker from an estimate of noise, and the reader is
// meant to see how much the answer moves when those are dragged.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Download, RefreshCw } from 'lucide-react'
import Plotly from 'plotly.js-dist-min'
import { fetchJson } from '../utils/api'
import { useTheme } from '../hooks/useTheme'
import { FONT_FAMILY, SPREAD_PLOTLY_CONFIG, spreadTheme, toMsk } from '../components/OrderBookViz'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/mmdetect'

interface Coverage {
  ticker: string; name: string; group: string; n_snapshots: number
  first_ts: string | null; last_ts: string | null
  expected: number; miss_ratio: number | null
}
interface Stat { median: number | null; p25: number | null; p75: number | null; n: number }
interface Pair {
  volume_bid: number; volume_ask: number; volume_two_sided: number
  size_mismatch: number
  presence_bid: number; presence_ask: number
  dist_bid_steps: number | null; dist_ask_steps: number | null
  spread_bps: Stat; spread_abs: Stat
  match_share: number; tracked: boolean
  alone_bid: number; alone_ask: number
  volume_usd: number | null; volume_rub: number | null
}
// One candidate resting size on one side: how often it stood, and where.
interface ProfileRow {
  side: 'bid' | 'ask'; volume: number; presence: number
  dist_steps: number | null; dist_bps: number | null
}
// `mm0_bps`, `mm1_bps`, … — one column per tracked quoter, hence the index type.
interface SeriesPoint {
  ts: string; spread_abs: number | null; spread_steps: number | null
  spread_bps: number | null
  [key: string]: string | number | null
}
interface CorridorStat {
  bid: Stat; ask: Stat; two_sided: Stat; two_sided_usd: Stat; truncated_share: number
}
interface AnalyzeResult {
  n_snapshots: number; price_step: number; bin_steps: number; enough_data: boolean
  spread_observed: Stat; spread_observed_abs: Stat; spread_mm: Stat; spread_mm_abs: Stat
  mm_volume: { total: number; largest: number; median: number
               p25: number; p75: number; n_pairs: number } | null
  corridors: Record<string, CorridorStat>
  clusters: { side: 'bid' | 'ask'; volume: number; presence: number
              dist_steps: number | null; matched: boolean }[]
  radius_steps: number | null
  pairs: Pair[]; series: SeriesPoint[]; profile: ProfileRow[]
  valid_match_share: number
}
interface Heatmap {
  x: string[]; y: number[]; z: (number | null)[][]
  bin_steps: number; max_bin_shown: number; bins_present: number
}
interface AnalyzeResponse {
  ticker: string; name: string; from: string; to: string; mode: string
  params: { bin_steps: number; bin_steps_auto: boolean; stride_sec: number
            search_radius_pct?: number }
  result: AnalyzeResult; heatmap: Heatmap | null
}
// One detected quoter as the summary table lists it.
interface SummaryQuoter {
  volume_bid: number; volume_ask: number; volume_two_sided: number
  dist_bid_steps: number | null; dist_ask_steps: number | null
  spread_bps: number | null
  presence_bid: number; presence_ask: number
  alone_bid: number; alone_ask: number; match_share: number
  volume_usd: number | null; volume_rub: number | null
}
interface SummaryRow {
  ticker: string; name: string; group: string; n_snapshots: number; enough_data: boolean
  spread_observed_bps: number | null; spread_mm_bps: number | null
  mm_volume: number | null; mm_volume_largest: number | null
  n_pairs: number; valid_match_share: number
  corridors: Record<string, { two_sided: number | null; two_sided_usd: number | null; truncated_share: number }>
  dist_bid_steps: number | null; dist_ask_steps: number | null
  quoters: SummaryQuoter[]
  miss_ratio: number | null; stride_sec: number
}

const WINDOWS = [
  { h: 1, label: '1 ч' }, { h: 3, label: '3 ч' }, { h: 6, label: '6 ч' },
  { h: 12, label: '12 ч' }, { h: 24, label: '24 ч' }, { h: 48, label: '48 ч' },
]
const OBS_COLOR = '#3b82f6'
const MM_COLOR  = '#f97316'

const f = (v: number | null | undefined, nd = 2) =>
  v == null ? '—' : v.toLocaleString('ru-RU', { maximumFractionDigits: nd })
const pct = (v: number | null | undefined, nd = 0) =>
  v == null ? '—' : `${(v * 100).toLocaleString('ru-RU', { maximumFractionDigits: nd })}%`
const usd = (v: number | null | undefined) =>
  v == null ? '—' : `$${Math.round(v).toLocaleString('ru-RU')}`
const rub = (v: number | null | undefined) =>
  v == null ? '—' : `${Math.round(v).toLocaleString('ru-RU')} ₽`

function Slider({ label, value, min, max, step, onChange, format }: {
  label: string; value: number; min: number; max: number; step: number
  onChange: (v: number) => void; format: (v: number) => string
}) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 170 }}>
      <span style={{ fontSize: 11, color: 'var(--muted)', display: 'flex', justifyContent: 'space-between' }}>
        <span>{label}</span>
        <strong style={{ color: 'var(--text)' }}>{format(value)}</strong>
      </span>
      <input type="range" min={min} max={max} step={step} value={value}
             onChange={e => onChange(Number(e.target.value))} style={{ width: '100%' }} />
    </label>
  )
}

// ── heat map: time × distance from the mid, volume as colour ─────────────────
function HeatmapChart({ hm, pairs }: { hm: Heatmap | null; pairs: Pair[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const theme = useTheme()

  useEffect(() => {
    const el = ref.current
    if (!el || !hm || !hm.x.length) return
    const t = spreadTheme(theme)
    // y is a signed bin index (ask above the mid, bid below); label it in price
    // steps, the unit the whole detector works in.
    const ySteps = hm.y.map(b => b * hm.bin_steps)

    const data: Plotly.Data[] = [{
      type: 'heatmap',
      x: hm.x.map(toMsk), y: ySteps, z: hm.z,
      colorscale: theme === 'light' ? 'YlGnBu' : 'Viridis',
      hovertemplate: '%{x|%d.%m %H:%M}<br>%{y} шагов от мида<br>объём %{z:.0f}<extra></extra>',
      colorbar: { title: { text: 'Объём', font: { color: t.text, size: 10 } }, thickness: 10,
                  tickfont: { color: t.text, size: 9 }, len: 0.9 },
    }]

    // Each confirmed pair marks its two sides SEPARATELY: the maker's bid and
    // ask rest at their own distances, and drawing one mirrored band would
    // reassert exactly the assumption this detector dropped.
    const shapes: Partial<Plotly.Shape>[] = []
    for (const p of pairs) {
      const ys = [p.dist_ask_steps, p.dist_bid_steps == null ? null : -p.dist_bid_steps]
      for (const y of ys) {
        if (y == null) continue
        shapes.push({
          type: 'line', xref: 'paper', x0: 0, x1: 1, y0: y, y1: y,
          line: { color: MM_COLOR, width: 1.6, dash: 'dot' }, layer: 'above',
        })
      }
    }

    Plotly.react(el, data, {
      paper_bgcolor: t.paper, plot_bgcolor: t.bg,
      margin: { l: 58, r: 10, t: 10, b: 34 }, shapes,
      xaxis: { type: 'date', tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
               gridcolor: t.grid, linecolor: t.grid, showgrid: false },
      yaxis: { title: { text: 'Удаление от мида, шаги', font: { color: t.text, size: 10, family: FONT_FAMILY } },
               tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
               gridcolor: t.grid, linecolor: t.grid, zeroline: true, zerolinecolor: t.text },
      hoverlabel: { bgcolor: t.hover, bordercolor: t.hoverBorder,
                    font: { color: t.hoverText, size: 11, family: FONT_FAMILY } },
    }, SPREAD_PLOTLY_CONFIG)
    return () => { Plotly.purge(el) }
  }, [hm, pairs, theme])

  if (!hm || !hm.x.length) return <div className="empty">нет снимков в этом окне</div>
  return (
    <>
      <div ref={ref} style={{ width: '100%', height: 320 }} />
      {hm.bins_present > hm.max_bin_shown && (
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>
          Показаны ближние {hm.max_bin_shown + 1} бина каждой стороны из {hm.bins_present + 1} —
          дальние уровни увели бы масштаб и спрятали зону котирования.
        </div>
      )}
    </>
  )
}

// Break the line wherever the record itself has a hole.  Plotly joins whatever
// points it is given, so a stretch when the collector was not running would be
// drawn as a long straight segment — a claim about the spread over minutes that
// were never observed.  A gap of more than 4× the usual spacing becomes a null.
function withGaps(series: SeriesPoint[], keys: string[]):
    { x: string[]; cols: Record<string, (number | null)[]> } {
  const x: string[] = []
  const cols: Record<string, (number | null)[]> = {}
  for (const k of keys) cols[k] = []
  const ts = series.map(s => Date.parse(s.ts))
  const dts = ts.slice(1).map((t, i) => t - ts[i]).sort((a, b) => a - b)
  const typical = dts.length ? dts[Math.floor(dts.length / 2)] : 0
  series.forEach((s, i) => {
    if (i > 0 && typical > 0 && ts[i] - ts[i - 1] > 4 * typical) {
      x.push(toMsk(new Date(ts[i - 1] + typical).toISOString()))
      for (const k of keys) cols[k].push(null)
    }
    x.push(toMsk(s.ts))
    for (const k of keys) cols[k].push((s[k] as number | null) ?? null)
  })
  return { x, cols }
}

// A colour per detected quoter — they are different participants, so they must
// not share a colour with each other or with the observed spread.
const QUOTER_COLORS = ['#f97316', '#a855f7', '#14b8a6', '#eab308']

// ── observed vs MM spread ────────────────────────────────────────────────────
function SpreadChart({ series, pairs }: { series: SeriesPoint[]; pairs: Pair[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const theme = useTheme()

  useEffect(() => {
    const el = ref.current
    if (!el || !series.length) return
    const t = spreadTheme(theme)
    const tracked = pairs.filter(p => p.tracked)
    const keys = ['spread_bps', ...tracked.map((_p, i) => `mm${i}_bps`)]
    const { x, cols } = withGaps(series, keys)
    const traces: Plotly.Data[] = [{
      type: 'scatter', mode: 'lines', name: 'Наблюдаемый (best bid/ask)',
      x, y: cols['spread_bps'], connectgaps: false,
      line: { color: OBS_COLOR, width: 1.5 },
      hovertemplate: '%{y:.2f} б.п.<extra>наблюдаемый</extra>',
    }]
    tracked.forEach((p, i) => {
      const label = `ММ ${f(p.volume_two_sided, 0)} контр.`
      traces.push({
        type: 'scatter', mode: 'lines', name: label,
        x, y: cols[`mm${i}_bps`], connectgaps: false,
        line: { color: QUOTER_COLORS[i % QUOTER_COLORS.length], width: 1.5 },
        hovertemplate: `%{y:.2f} б.п.<extra>${label}</extra>`,
      })
    })
    Plotly.react(el, traces, {
      paper_bgcolor: t.paper, plot_bgcolor: t.bg,
      margin: { l: 52, r: 10, t: 10, b: 34 },
      legend: { orientation: 'h', y: 1.16, x: 0, font: { color: t.text, size: 10, family: FONT_FAMILY } },
      xaxis: { type: 'date', tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
               gridcolor: t.grid, linecolor: t.grid, showgrid: false },
      yaxis: { title: { text: 'Спред, б.п.', font: { color: t.text, size: 10, family: FONT_FAMILY } },
               tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
               gridcolor: t.grid, linecolor: t.grid, rangemode: 'tozero' },
      hoverlabel: { bgcolor: t.hover, bordercolor: t.hoverBorder,
                    font: { color: t.hoverText, size: 11, family: FONT_FAMILY } },
    }, SPREAD_PLOTLY_CONFIG)
    return () => { Plotly.purge(el) }
  }, [series, pairs, theme])

  if (!series.length) return <div className="empty">нет данных</div>
  return <div ref={ref} style={{ width: '100%', height: 240 }} />
}

// ── how often each resting SIZE stood, bid vs ask, against the cut-off ───────
// This is the detector's own working surface: it scores sizes, not places, so a
// bar here is "a level of this size stood somewhere within the search radius in
// N% of snapshots".  Where it stood is a separate answer, shown in the tooltip
// and in the pairs table.
function ProfileChart({ profile, threshold }: {
  profile: ProfileRow[]; threshold: number
}) {
  const ref = useRef<HTMLDivElement>(null)
  const theme = useTheme()

  useEffect(() => {
    const el = ref.current
    if (!el || !profile.length) return
    const t = spreadTheme(theme)
    // Shared category axis so the same size lines up on both sides.
    const sizes = [...new Set(profile.map(p => p.volume))].sort((a, b) => b - a)
    const labels = sizes.map(v => v.toLocaleString('ru-RU', { maximumFractionDigits: 1 }))
    const mk = (side: 'bid' | 'ask') => {
      const by = new Map(profile.filter(p => p.side === side).map(p => [p.volume, p]))
      return {
        y: sizes.map(v => by.get(v)?.presence ?? null),
        custom: sizes.map(v => by.get(v)?.dist_steps ?? null),
      }
    }
    const bid = mk('bid'), ask = mk('ask')
    const tip = (name: string) =>
      `%{x} контр.<br>стоял в %{y:.0%} снимков<br>удаление ~%{customdata:.0f} шагов<extra>${name}</extra>`

    Plotly.react(el, [
      { type: 'bar', name: 'Bid', x: labels, y: bid.y, customdata: bid.custom,
        marker: { color: '#22c55e' }, hovertemplate: tip('bid') },
      { type: 'bar', name: 'Ask', x: labels, y: ask.y, customdata: ask.custom,
        marker: { color: '#ef4444' }, hovertemplate: tip('ask') },
    ] as Plotly.Data[], {
      paper_bgcolor: t.paper, plot_bgcolor: t.bg,
      margin: { l: 46, r: 10, t: 10, b: 42 }, bargap: 0.2, barmode: 'group',
      legend: { orientation: 'h', y: 1.18, x: 0, font: { color: t.text, size: 10, family: FONT_FAMILY } },
      shapes: [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: threshold, y1: threshold,
                 line: { color: MM_COLOR, width: 1.2, dash: 'dot' } }],
      annotations: [{ xref: 'paper', x: 1, y: threshold, yanchor: 'bottom', xanchor: 'right',
                      text: `порог ${(threshold * 100).toFixed(0)}%`, showarrow: false,
                      font: { color: MM_COLOR, size: 10, family: FONT_FAMILY } }],
      xaxis: { type: 'category', title: { text: 'Объём заявки, контрактов',
                        font: { color: t.text, size: 10, family: FONT_FAMILY } },
               tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
               gridcolor: t.grid, linecolor: t.grid },
      yaxis: { tickformat: '.0%', range: [0, 1], tickfont: { color: t.text, size: 9, family: FONT_FAMILY },
               gridcolor: t.grid, linecolor: t.grid },
      hoverlabel: { bgcolor: t.hover, bordercolor: t.hoverBorder,
                    font: { color: t.hoverText, size: 11, family: FONT_FAMILY } },
    }, SPREAD_PLOTLY_CONFIG)
    return () => { Plotly.purge(el) }
  }, [profile, threshold, theme])

  if (!profile.length) return <div className="empty">нет данных</div>
  return <div ref={ref} style={{ width: '100%', height: 240 }} />
}

export function MMDetect() {
  const [instruments, setInstruments] = useState<Coverage[]>([])
  const [ticker, setTicker] = useState<string>('')
  const [hours, setHours] = useState(6)
  const [mode, setMode] = useState('all')
  const [modes, setModes] = useState<Record<string, string>>({})

  const [persistence, setPersistence] = useState(0.7)
  const [volTol, setVolTol] = useState(0.10)
  const [symTol, setSymTol] = useState(0.25)
  const [minVol, setMinVol] = useState(2)              // contracts
  const [radiusPct, setRadiusPct] = useState(0.5)      // % of price

  const [analysis, setAnalysis] = useState<AnalyzeResponse | null>(null)
  const [summary, setSummary] = useState<SummaryRow[]>([])
  const [corridors, setCorridors] = useState<number[]>([])
  const [usdrub, setUsdrub] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const query = useMemo(() => new URLSearchParams({
    hours: String(hours), mode,
    persistence_min: String(persistence), volume_tol: String(volTol),
    symmetry_tol: String(symTol),
    min_cluster_volume: String(minVol),
    search_radius_pct: String(radiusPct / 100),
  }).toString(), [hours, mode, persistence, volTol, symTol, minVol, radiusPct])

  useEffect(() => {
    fetchJson<{ instruments: Coverage[]; modes: Record<string, string> }>(
      `${API}/instruments?hours=${hours}`)
      .then(d => {
        setInstruments(d.instruments)
        setModes(d.modes)
        setTicker(t => t || (d.instruments.find(i => i.n_snapshots > 0)?.ticker
                             ?? d.instruments[0]?.ticker ?? ''))
      })
      .catch(e => setError(String(e)))
  }, [hours])

  const load = useCallback(async () => {
    if (!ticker) return
    setLoading(true)
    try {
      const [a, s] = await Promise.all([
        fetchJson<AnalyzeResponse>(`${API}/analyze?ticker=${ticker}&${query}`),
        fetchJson<{ rows: SummaryRow[]; corridors: number[]; usdrub: number | null }>(
          `${API}/summary?${query}`),
      ])
      setAnalysis(a)
      setSummary(s.rows)
      setCorridors(s.corridors)
      setUsdrub(s.usdrub)
      setError(null)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [ticker, query])

  // Debounced: dragging a slider must not fire a request per pixel — each one
  // re-runs the detector over the whole window server-side.
  useEffect(() => {
    const id = setTimeout(load, 350)
    return () => clearTimeout(id)
  }, [load])

  const res = analysis?.result
  const cov = instruments.find(i => i.ticker === ticker)

  return (
    <div>
      <div className="page-toolbar">
        <h1>Присутствие маркет-мейкеров — СПБ Биржа</h1>
        <select className="btn-secondary" value={ticker} onChange={e => setTicker(e.target.value)}>
          {[...new Set(instruments.map(i => i.group))].map(g => (
            <optgroup key={g} label={g}>
              {instruments.filter(i => i.group === g).map(i => (
                <option key={i.ticker} value={i.ticker}>
                  {i.name} ({i.ticker.replace('perpA', '')}){i.n_snapshots ? '' : ' — нет данных'}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        <div className="type-filter">
          {WINDOWS.map(w => (
            <button key={w.h} className={`filter-btn ${hours === w.h ? 'filter-btn--active' : ''}`}
                    onClick={() => setHours(w.h)}>{w.label}</button>
          ))}
        </div>
        <select className="btn-secondary" value={mode} onChange={e => setMode(e.target.value)}>
          {Object.entries(modes).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
        <button className="btn-icon" onClick={load} title="Пересчитать">
          <RefreshCw size={14} className={loading ? 'spin' : ''} />
        </button>
        <a className="btn-secondary" href={`${API}/export.xlsx?${query}`}
           title="Сводка со всеми котировщиками в Excel">
          <Download size={13} style={{ verticalAlign: '-2px', marginRight: 6 }} />Excel
        </a>
        <a className="btn-secondary" href={`${API}/export.csv?${query}`}>
          <Download size={13} style={{ verticalAlign: '-2px', marginRight: 6 }} />CSV
        </a>
      </div>

      {/* Limits of the method, at the top where they are read — not in a footnote. */}
      <div className="card" style={{ marginBottom: 16, borderLeft: '3px solid ' + MM_COLOR }}>
        <div className="card-title" style={{ color: MM_COLOR }}>Как читать эти цифры</div>
        <div style={{ fontSize: 12.5, lineHeight: 1.65, color: 'var(--muted)' }}>
          Стакан анонимен: маркет-мейкера в нём не видно. Здесь показана <strong style={{ color: 'var(--text)' }}>оценка
          присутствия ММ</strong> по двум признакам: заявка одного и того же <strong style={{ color: 'var(--text)' }}>размера</strong> держится
          в стакане снимок за снимком, и такая же по размеру стоит с противоположной стороны.
          Удаление от мида при этом <strong style={{ color: 'var(--text)' }}>не обязано совпадать</strong> — обязательство ММ связывает
          объёмы котировок, а не расстояния; поэтому удаление показано отдельно по каждой стороне. Отсюда ограничения:
          <ul style={{ margin: '6px 0 0 18px', padding: 0 }}>
            <li>это <strong style={{ color: 'var(--text)' }}>верхняя оценка</strong>: алго-участники,
                не являющиеся ММ, котируют так же и попадают в ту же выборку;</li>
            <li>если котировщиков несколько, они показаны отдельно — но только по РАЗМЕРУ заявки:
                два ММ, стоящие одинаковым объёмом, сольются в одного, а один ММ, выставляющий
                две заявки разного размера, будет посчитан как двое;</li>
            <li>айсберг-заявки видны только надводной частью, поэтому реальный объём может быть больше;</li>
            <li>уровень стакана показывает СУММУ всех заявок по этой цене, поэтому заявка ММ,
                к которой встала такая же чужая, засчитывается по кратности (до 3×) — колонка
                «один на уровне» показывает, как часто она стояла одна;</li>
            <li>ММ обычно котирует с запасом к обязательству, так что оценка ≠ обязательство;</li>
            <li>в лучшие котировки вклинивается клиентский поток — он же двигает мид, из-за чего
                удаления двух котировок ММ расходятся; поэтому спред между котировками ММ показан
                отдельно от наблюдаемого, сравнивать нужно оба.</li>
          </ul>
        </div>
      </div>

      {error && <div className="card" style={{ marginBottom: 16, color: 'var(--red)' }}>{error}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Пороги детектора</div>
        <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
          <Slider label="Персистентность ≥" value={persistence} min={0.3} max={1} step={0.05}
                  onChange={setPersistence} format={v => `${(v * 100).toFixed(0)}%`} />
          <Slider label="Допуск по объёму ±" value={volTol} min={0.02} max={0.5} step={0.01}
                  onChange={setVolTol} format={v => `${(v * 100).toFixed(0)}%`} />
          <Slider label="Допуск по симметрии" value={symTol} min={0.05} max={1} step={0.05}
                  onChange={setSymTol} format={v => `${(v * 100).toFixed(0)}%`} />
          <Slider label="Мин. объём кластера" value={minVol} min={1} max={50} step={1}
                  onChange={setMinVol} format={v => `${v} контр.`} />
          <Slider label="Радиус поиска от мида" value={radiusPct} min={0.05} max={3} step={0.05}
                  onChange={setRadiusPct}
                  format={v => `${v}%${res?.radius_steps ? ` (~${Math.round(res.radius_steps)} ш.)` : ''}`} />
        </div>
      </div>

      {res && !res.enough_data && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="empty">
            В окне {WINDOWS.find(w => w.h === hours)?.label} по {ticker.replace('perpA', '')} всего{' '}
            {res.n_snapshots} снимков — слишком мало для доли. Стакан нельзя бэкфилить: история
            копится с момента запуска сборщика.
          </div>
        </div>
      )}

      {res && res.enough_data && (
        <>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">Оценка · {analysis?.name}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 16 }}>
              <Metric label="Объём ММ, всего (двусторонний)"
                      value={res.mm_volume ? `${f(res.mm_volume.total, 0)} контр.` : 'не подтверждён'}
                      sub={res.mm_volume
                        ? `котировщиков: ${res.mm_volume.n_pairs} · крупнейший ${f(res.mm_volume.largest, 0)}`
                        : 'нет одинаковых по размеру кластеров с двух сторон'} />
              <Metric label="Спред крупнейшего ММ"
                      value={res.spread_mm.median != null ? `${f(res.spread_mm.median)} б.п.` : '—'}
                      sub={res.pairs.length
                        ? `$${f(res.spread_mm_abs.median, 4)} · bid ${f(res.pairs[0].dist_bid_steps, 0)} ш. / ask ${f(res.pairs[0].dist_ask_steps, 0)} ш. от мида`
                        : '—'} />
              <Metric label="Наблюдаемый спред (best bid/ask)"
                      value={`${f(res.spread_observed.median)} б.п.`}
                      sub={`p25–p75 ${f(res.spread_observed.p25)}–${f(res.spread_observed.p75)}`} />
              <Metric label="Снимков в окне" value={f(res.n_snapshots, 0)}
                      sub={`пропуск ${pct(cov?.miss_ratio, 1)} · шаг сетки ${analysis?.params.stride_sec}с`} />
              <Metric label="Доля снимков с валидным матчем" value={pct(res.valid_match_share, 1)}
                      sub={`бин ${res.bin_steps} шагов${analysis?.params.bin_steps_auto ? ' (авто)' : ''}`} />
            </div>
          </div>

          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">Двусторонняя глубина в коридорах от мида</div>
            <table className="price-table">
              <thead>
                <tr>
                  <th>Коридор</th><th>min(bid, ask), контр.</th><th>в деньгах</th>
                  <th>bid</th><th>ask</th><th>книга обрезана</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(res.corridors).map(([c, v]) => (
                  <tr key={c}>
                    <td>±{(Number(c) * 100).toFixed(2).replace(/0$/, '')}%</td>
                    <td>{f(v.two_sided.median, 0)}</td>
                    <td>{usd(v.two_sided_usd.median)}</td>
                    <td>{f(v.bid.median, 0)}</td>
                    <td>{f(v.ask.median, 0)}</td>
                    <td style={{ color: v.truncated_share > 0.05 ? 'var(--red)' : 'var(--muted)' }}>
                      {pct(v.truncated_share, 0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
              «Книга обрезана» — доля снимков, где сохранённая глубина кончилась внутри коридора:
              там цифра является нижней оценкой, а не «дальше пусто».
            </div>
          </div>

          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">Стакан во времени · уровни котировщиков отмечены пунктиром</div>
            <HeatmapChart hm={analysis?.heatmap ?? null} pairs={res.pairs} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))', gap: 16, marginBottom: 16 }}>
            <div className="card">
              <div className="card-title">Спред: наблюдаемый и по каждому котировщику</div>
              <SpreadChart series={res.series} pairs={res.pairs} />
            </div>
            <div className="card">
              <div className="card-title">Как часто стоял каждый объём</div>
              <ProfileChart profile={res.profile} threshold={persistence} />
            </div>
          </div>

          {res.pairs.length > 0 && (
            <div className="card" style={{ marginBottom: 16 }}>
              <div className="card-title">Обнаруженные котировщики · размер совпал с двух сторон, удаление может отличаться</div>
              <table className="price-table">
                <thead>
                  <tr>
                    <th>Котировщик</th><th>Объём bid</th><th>Объём ask</th><th>min(bid, ask)</th>
                    <th>Объём, $</th><th>Объём, ₽</th>
                    <th>Удаление bid, ш.</th><th>Удаление ask, ш.</th>
                    <th>Спред, б.п.</th><th>Стоял bid</th><th>Стоял ask</th>
                    <th>Один на уровне</th><th>Обе стороны</th>
                  </tr>
                </thead>
                <tbody>
                  {res.pairs.map((p, i) => (
                    <tr key={i}>
                      <td>
                        <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 2,
                                       marginRight: 6,
                                       background: p.tracked ? QUOTER_COLORS[i % QUOTER_COLORS.length] : 'var(--border)' }} />
                        №{i + 1}
                      </td>
                      <td>{f(p.volume_bid, 0)}</td>
                      <td>{f(p.volume_ask, 0)}</td>
                      <td>{f(p.volume_two_sided, 0)}</td>
                      <td>{usd(p.volume_usd)}</td>
                      <td>{rub(p.volume_rub)}</td>
                      <td>{f(p.dist_bid_steps, 0)}</td>
                      <td>{f(p.dist_ask_steps, 0)}</td>
                      <td>{f(p.spread_bps.median)}</td>
                      <td>{pct(p.presence_bid, 0)}</td>
                      <td>{pct(p.presence_ask, 0)}</td>
                      <td title="доля снимков, где заявка этого размера стояла на своей цене одна, а не в очереди с такой же">
                        {pct(p.alone_bid, 0)} / {pct(p.alone_ask, 0)}
                      </td>
                      <td>{pct(p.match_share, 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <div className="card">
        <div className="card-title">Сводка · каждый обнаруженный котировщик отдельной строкой</div>
        <div style={{ overflowX: 'auto' }}>
          <table className="price-table">
            <thead>
              <tr>
                <th>Инструмент</th><th>Снимков</th><th>Пропуск</th>
                <th>Спред набл., б.п.</th><th>ММ всего</th>
                {corridors.map(c => <th key={c}>±{(c * 100).toFixed(2).replace(/0$/, '')}%</th>)}
                <th>Котировщик</th><th>Объём</th><th>Объём, $</th><th>Объём, ₽</th>
                <th>Удал. bid/ask, ш.</th>
                <th>Спред, б.п.</th><th>Стоял bid/ask</th><th>Один на уровне</th><th>Обе стороны</th>
              </tr>
            </thead>
            <tbody>
              {!summary.length && (
                <tr><td colSpan={13 + corridors.length} style={{ color: 'var(--muted)' }}>
                  {loading ? 'считаем по всем инструментам…' : 'нет данных'}
                </td></tr>
              )}
              {summary.flatMap((r, ri) => {
                const newGroup = ri === 0 || summary[ri - 1].group !== r.group
                // One row per quoter, with the instrument's own figures merged
                // down the group — a book with three makers is three facts, and
                // one line per instrument could only show their sum.
                const qs: (SummaryQuoter | null)[] = r.quoters.length ? r.quoters : [null]
                const sel = r.ticker === ticker
                const head = newGroup ? [(
                  <tr key={`g-${r.group}`}>
                    <td colSpan={13 + corridors.length}
                        style={{ fontSize: 11, fontWeight: 600, color: 'var(--muted)',
                                 textTransform: 'uppercase', letterSpacing: '.05em',
                                 background: 'var(--surface2)' }}>
                      {r.group}
                    </td>
                  </tr>
                )] : []
                return head.concat(qs.map((q, i) => (
                  <tr key={`${r.ticker}-${i}`}
                      style={{ cursor: 'pointer', fontWeight: sel ? 600 : 400,
                               background: sel ? 'var(--surface2)' : undefined }}
                      onClick={() => setTicker(r.ticker)}>
                    {i === 0 && <>
                      <td rowSpan={qs.length}>{r.name}</td>
                      <td rowSpan={qs.length}>{f(r.n_snapshots, 0)}</td>
                      <td rowSpan={qs.length}
                          style={{ color: (r.miss_ratio ?? 0) > 0.2 ? 'var(--red)' : undefined }}>
                        {pct(r.miss_ratio, 0)}
                      </td>
                      <td rowSpan={qs.length}>{f(r.spread_observed_bps)}</td>
                      <td rowSpan={qs.length}>{r.mm_volume == null ? '—' : f(r.mm_volume, 0)}</td>
                      {corridors.map(c => (
                        <td key={c} rowSpan={qs.length}>{f(r.corridors[String(c)]?.two_sided, 0)}</td>
                      ))}
                    </>}
                    {q == null ? (
                      <td colSpan={8} style={{ color: 'var(--muted)' }}>не подтверждён</td>
                    ) : (
                      <>
                        <td>
                          <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 2,
                                         marginRight: 6,
                                         background: QUOTER_COLORS[i % QUOTER_COLORS.length] }} />
                          №{i + 1}
                        </td>
                        <td>{f(q.volume_two_sided, 0)}</td>
                        <td>{usd(q.volume_usd)}</td>
                        <td>{rub(q.volume_rub)}</td>
                        <td>{f(q.dist_bid_steps, 0)} / {f(q.dist_ask_steps, 0)}</td>
                        <td>{f(q.spread_bps)}</td>
                        <td>{pct(q.presence_bid, 0)} / {pct(q.presence_ask, 0)}</td>
                        <td>{pct(q.alone_bid, 0)} / {pct(q.alone_ask, 0)}</td>
                        <td>{pct(q.match_share, 0)}</td>
                      </>
                    )}
                  </tr>
                )))
              })}
            </tbody>
          </table>
        </div>
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
          Сводка считается по прореженной сетке ({summary[0]?.stride_sec ?? '—'} с) — карточка выше
          использует полную, поэтому числа могут слегка расходиться. Строка кликабельна.
          Деньги: объём × цена собственной котировки ММ; рубли — по курсу USDRUBF
          {usdrub ? ` (${usdrub.toFixed(2)} ₽/$)` : ''}, тому же, что и на остальных страницах.
        </div>
      </div>
    </div>
  )
}

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700 }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>{sub}</div>}
    </div>
  )
}
