/**
 * Chart unit picker.
 *
 * Volume/OI values span many orders of magnitude — a BTC perp trades in
 * hundreds of ₽B while a Korean stock perp barely reaches ₽M.  A fixed unit
 * either flattens the small instruments to "0.0" or blows the large ones up to
 * "500,000M", so each chart picks the unit that fits its own data.
 */

export interface Unit {
  scale:  number   // divide raw values by this
  suffix: string   // 'K' | 'M' | 'B' | 'T' ('' below a thousand)
}

const UNITS: Unit[] = [
  { scale: 1e12, suffix: 'T' },
  { scale: 1e9,  suffix: 'B' },
  { scale: 1e6,  suffix: 'M' },
  { scale: 1e3,  suffix: 'K' },
  { scale: 1,    suffix: ''  },
]

/**
 * Unit for a chart whose largest bar is `max`.  The threshold is 1 unit, so a
 * max of 2.4e9 renders as "2.4B" and 9.9e8 as "990.0M" — never "0.0" and never
 * a six-digit tick label.
 */
export function pickUnit(max: number): Unit {
  const m = Math.abs(max)
  if (!isFinite(m) || m === 0) return { scale: 1e6, suffix: 'M' }
  return UNITS.find(u => m >= u.scale) ?? UNITS[UNITS.length - 1]
}

/**
 * Largest stacked-bar total: values are summed per X category first, since it's
 * the total height — not any single segment — that sets the axis range.
 */
export function maxStackedTotal(values: Iterable<{ key: string; value: number | null | undefined }>): number {
  const totals = new Map<string, number>()
  for (const { key, value } of values) {
    if (value == null || !isFinite(value)) continue
    totals.set(key, (totals.get(key) ?? 0) + value)
  }
  let max = 0
  for (const v of totals.values()) if (v > max) max = v
  return max
}
