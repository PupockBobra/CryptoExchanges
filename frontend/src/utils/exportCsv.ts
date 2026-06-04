interface WeeklyRow { week_start: string; symbol: string; exchange: string; adtv: number }
interface DailyRow  { date: string;       symbol: string; exchange: string; volume_rub: number }

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
