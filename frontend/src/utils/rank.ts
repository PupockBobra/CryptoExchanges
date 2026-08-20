/**
 * Card ordering inside a section.
 *
 * Charts are ranked by size (turnover, open interest), not alphabetically — the
 * instruments that actually move volume belong at the top of their section.
 */

/**
 * Symbols ordered by summed value, largest first.  Symbols that tie (or carry
 * no value at all) fall back to alphabetical order so the layout is stable
 * between reloads.
 */
export function sortSymbolsByValue<T>(
  rows: T[],
  symbolOf: (row: T) => string,
  valueOf:  (row: T) => number | null | undefined,
): string[] {
  const totals = new Map<string, number>()
  for (const row of rows) {
    const sym = symbolOf(row)
    const v   = valueOf(row)
    const add = v != null && isFinite(v) ? v : 0
    totals.set(sym, (totals.get(sym) ?? 0) + add)
  }
  return [...totals.keys()].sort((a, b) => {
    const diff = (totals.get(b) ?? 0) - (totals.get(a) ?? 0)
    return diff !== 0 ? diff : a.localeCompare(b)
  })
}
