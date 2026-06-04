interface WeeklyRow { week_start: string; symbol: string; exchange: string; adtv: number }
interface DailyRow  { date: string;       symbol: string; exchange: string; volume_rub: number }

function dateLabel(d: string) {
  return new Date(d + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: 'numeric' })
}

function buildCsv(dates: string[], columns: string[], lookup: Map<string, Map<string, number>>, title: string): string {
  const header = ['Date', ...columns, 'Total (RUB)'].join(',')
  const rows = dates.map(d => {
    const byCol = lookup.get(d) ?? new Map<string, number>()
    const vals  = columns.map(c => byCol.get(c) ?? 0)
    const total = vals.reduce((a, b) => a + b, 0)
    return [dateLabel(d), ...vals.map(v => Math.round(v)), Math.round(total)].join(',')
  })
  return `# ${title}\n${[header, ...rows].join('\n')}`
}

export function exportByExchange(rows: DailyRow[], filename: string) {
  const exchanges = Array.from(new Set(rows.map(r => r.exchange))).sort()
  const dates     = Array.from(new Set(rows.map(r => r.date))).sort()
  const lookup    = new Map<string, Map<string, number>>()
  for (const r of rows) {
    if (!lookup.has(r.date)) lookup.set(r.date, new Map())
    const m = lookup.get(r.date)!
    m.set(r.exchange, (m.get(r.exchange) ?? 0) + r.volume_rub)
  }
  download(buildCsv(dates, exchanges, lookup, 'Volume by Exchange (RUB)'), filename)
}

export function exportByBase(rows: DailyRow[], filename: string) {
  const bases  = Array.from(new Set(rows.map(r => r.symbol.split('/')[0]))).sort()
  const dates  = Array.from(new Set(rows.map(r => r.date))).sort()
  const lookup = new Map<string, Map<string, number>>()
  for (const r of rows) {
    const base = r.symbol.split('/')[0]
    if (!lookup.has(r.date)) lookup.set(r.date, new Map())
    const m = lookup.get(r.date)!
    m.set(base, (m.get(base) ?? 0) + r.volume_rub)
  }
  download(buildCsv(dates, bases, lookup, 'Volume by Instrument (RUB)'), filename)
}

export function exportByGroup(rows: DailyRow[], getGroup: (sym: string) => string, filename: string) {
  const groups = ['Commodities', 'US Market', 'Cryptocurrencies']
  const dates  = Array.from(new Set(rows.map(r => r.date))).sort()
  const lookup = new Map<string, Map<string, number>>()
  for (const r of rows) {
    const group = getGroup(r.symbol)
    if (!lookup.has(r.date)) lookup.set(r.date, new Map())
    const m = lookup.get(r.date)!
    m.set(group, (m.get(group) ?? 0) + r.volume_rub)
  }
  download(buildCsv(dates, groups, lookup, 'Volume by Asset Group (RUB)'), filename)
}

function download(content: string, filename: string) {
  const blob = new Blob(['﻿' + content], { type: 'text/csv;charset=utf-8;' })
  const url  = URL.createObjectURL(blob)
  const a    = document.createElement('a')
  a.href = url; a.download = filename; a.click()
  URL.revokeObjectURL(url)
}

function pivot<T extends { exchange: string }>(
  rows:     T[],
  getDate:  (r: T) => string,
  getLabel: (r: T) => string,
  getValue: (r: T) => number,
): string {
  const exchanges = Array.from(new Set(rows.map(r => r.exchange))).sort()
  const dates     = Array.from(new Set(rows.map(r => getDate(r)))).sort()
  const labels    = new Map<string, string>(rows.map(r => [getDate(r), getLabel(r)]))

  const lookup = new Map<string, Map<string, number>>()
  for (const r of rows) {
    if (!lookup.has(getDate(r))) lookup.set(getDate(r), new Map())
    lookup.get(getDate(r))!.set(r.exchange, getValue(r))
  }

  const header = ['Date', ...exchanges, 'Total (RUB)'].join(',')
  const dataRows = dates.map(d => {
    const byEx = lookup.get(d)!
    const vals = exchanges.map(ex => byEx.get(ex) ?? 0)
    const total = vals.reduce((a, b) => a + b, 0)
    return [labels.get(d) ?? d, ...vals.map(v => Math.round(v)), Math.round(total)].join(',')
  })

  return [header, ...dataRows].join('\n')
}

export function exportWeeklyCsv(rows: WeeklyRow[], symbol: string, filename: string) {
  const csv = pivot(
    rows,
    r => r.week_start,
    r => {
      const d = new Date(r.week_start + 'T00:00:00')
      const e = new Date(d); e.setDate(d.getDate() + 6)
      const f = (dt: Date) => dt.toLocaleDateString('en-US', { month: 'short', day: '2-digit' })
      return `${f(d)} - ${f(e)}`
    },
    r => r.adtv,
  )
  download(`# Weekly ADTV (RUB) — ${symbol}\n${csv}`, filename)
}

export function exportDailyCsv(rows: DailyRow[], symbol: string, filename: string) {
  const csv = pivot(
    rows,
    r => r.date,
    r => {
      const d = new Date(r.date + 'T00:00:00')
      return d.toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: 'numeric' })
    },
    r => r.volume_rub,
  )
  download(`# Daily Volume (RUB) — ${symbol}\n${csv}`, filename)
}
