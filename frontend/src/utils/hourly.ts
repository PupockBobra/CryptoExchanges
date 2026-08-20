/**
 * Hourly Volume x-axis helpers.
 *
 * The backend returns hourly bars stamped in UTC (same convention as the spread
 * charts), but the page reads in Moscow time — so the shift happens here, on the
 * label, rather than in SQL.  The one exception is the intraday profile, whose
 * hour bucket IS the group key and therefore has to be built server-side.
 */

const MSK_OFFSET_MS = 3 * 3600 * 1000

const pad = (n: number) => String(n).padStart(2, '0')

/**
 * Label for one hourly bar: `'31.07 14:00'` in Moscow time.
 *
 * Shifting via UTC getters (rather than the host's local timezone) keeps the
 * output identical on a laptop in any timezone — a chart that renamed its hours
 * depending on who opened it would be worse than useless here.
 */
export function mskHourLabel(iso: string): string {
  const d = new Date(Date.parse(iso) + MSK_OFFSET_MS)
  return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)} ${pad(d.getUTCHours())}:00`
}

/** Tick label for the intraday profile: `14` → `'14:00'`. */
export const hourTick = (h: number) => `${pad(h)}:00`
