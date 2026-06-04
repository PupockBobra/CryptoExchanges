import * as XLSX from 'xlsx'

interface WeeklyRow {
  week_start:   string
  symbol:       string
  exchange:     string
  adtv:         number
}

interface DailyRow {
  date:     string
  symbol:   string
  exchange: string
  volume_rub: number
}

function sanitizeSheetName(name: string): string {
  // Excel sheet names: max 31 chars, no / \ ? * [ ]
  return name.replace(/[\/\\?*\[\]]/g, '-').slice(0, 31)
}

function buildPivotSheet<T extends { exchange: string }>(
  rows: T[],
  getDate:  (r: T) => string,
  getLabel: (r: T) => string,
  getValue: (r: T) => number,
  valueHeader: string,
): XLSX.WorkSheet {
  const exchanges = Array.from(new Set(rows.map(r => r.exchange))).sort()
  const dates     = Array.from(new Set(rows.map(r => getDate(r)))).sort()

  // Build lookup: date → exchange → value
  const lookup = new Map<string, Map<string, number>>()
  for (const r of rows) {
    const d = getDate(r)
    if (!lookup.has(d)) lookup.set(d, new Map())
    lookup.get(d)!.set(r.exchange, getValue(r))
  }

  // Label lookup: date → human label
  const labels = new Map<string, string>()
  for (const r of rows) labels.set(getDate(r), getLabel(r))

  const header = ['Date', ...exchanges, `Total ${valueHeader}`]
  const data: (string | number)[][] = [header]

  for (const d of dates) {
    const byEx = lookup.get(d)!
    const vals = exchanges.map(ex => {
      const v = byEx.get(ex) ?? 0
      return v > 0 ? Math.round(v / 1e9 * 10) / 10 : 0  // → ₽B, 1 decimal
    })
    const total = Math.round(vals.reduce((a, b) => a + b, 0) * 10) / 10
    data.push([labels.get(d) ?? d, ...vals, total])
  }

  const ws = XLSX.utils.aoa_to_sheet(data)

  // Column widths
  ws['!cols'] = [{ wch: 16 }, ...exchanges.map(() => ({ wch: 12 })), { wch: 14 }]

  return ws
}

export function exportWeeklyAdtv(
  allRows:  WeeklyRow[],
  symbols:  string[],
  filename: string,
): void {
  const wb = XLSX.utils.book_new()

  for (const sym of symbols) {
    const rows = allRows.filter(r => r.symbol === sym)
    if (!rows.length) continue
    const ws = buildPivotSheet(
      rows,
      r => r.week_start,
      r => {
        // Format as "Jan 06 – Jan 12"
        const d = new Date(r.week_start + 'T00:00:00')
        const end = new Date(d); end.setDate(d.getDate() + 6)
        const fmt = (dt: Date) => dt.toLocaleDateString('en-US', { month: 'short', day: '2-digit' })
        return `${fmt(d)} – ${fmt(end)}`
      },
      r => r.adtv,
      '₽B',
    )
    XLSX.utils.book_append_sheet(wb, ws, sanitizeSheetName(sym))
  }

  // Summary sheet: all symbols, last week ADTV
  const lastWeek = allRows.reduce((max, r) => r.week_start > max ? r.week_start : max, '')
  const summaryRows = allRows.filter(r => r.week_start === lastWeek)
  if (summaryRows.length) {
    const exchanges = Array.from(new Set(allRows.map(r => r.exchange))).sort()
    const syms = Array.from(new Set(summaryRows.map(r => r.symbol))).sort()
    const header = ['Symbol', ...exchanges, 'Total ₽B']
    const data: (string | number)[][] = [header]
    for (const s of syms) {
      const byEx = new Map<string, number>()
      summaryRows.filter(r => r.symbol === s).forEach(r => byEx.set(r.exchange, r.adtv))
      const vals = exchanges.map(ex => {
        const v = byEx.get(ex) ?? 0
        return v > 0 ? Math.round(v / 1e9 * 10) / 10 : 0
      })
      const total = Math.round(vals.reduce((a, b) => a + b, 0) * 10) / 10
      data.push([s, ...vals, total])
    }
    const ws = XLSX.utils.aoa_to_sheet(data)
    ws['!cols'] = [{ wch: 20 }, ...exchanges.map(() => ({ wch: 12 })), { wch: 14 }]
    XLSX.utils.book_append_sheet(wb, ws, 'Summary (last week)')
  }

  XLSX.writeFile(wb, filename)
}

export function exportDailyVolume(
  allRows:  DailyRow[],
  symbols:  string[],
  filename: string,
): void {
  const wb = XLSX.utils.book_new()

  for (const sym of symbols) {
    const rows = allRows.filter(r => r.symbol === sym)
    if (!rows.length) continue
    const ws = buildPivotSheet(
      rows,
      r => r.date,
      r => {
        const d = new Date(r.date + 'T00:00:00')
        return d.toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: 'numeric' })
      },
      r => r.volume_rub,
      '₽B',
    )
    XLSX.utils.book_append_sheet(wb, ws, sanitizeSheetName(sym))
  }

  XLSX.writeFile(wb, filename)
}
