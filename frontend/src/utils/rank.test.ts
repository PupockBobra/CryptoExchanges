import { describe, it, expect } from 'vitest'
import { sortSymbolsByValue } from './rank'

const sym = (r: { s: string }) => r.s
const val = (r: { v: number | null }) => r.v

describe('sortSymbolsByValue', () => {
  it('ranks by summed value, biggest first', () => {
    const rows = [
      { s: 'AAA', v: 10 },
      { s: 'BBB', v: 50 },
      { s: 'AAA', v: 100 },   // 110 total → first
      { s: 'CCC', v: 70 },
    ]
    expect(sortSymbolsByValue(rows, sym, val)).toEqual(['AAA', 'CCC', 'BBB'])
  })

  it('falls back to alphabetical on ties so the layout is stable', () => {
    const rows = [{ s: 'ZZZ', v: 5 }, { s: 'AAA', v: 5 }]
    expect(sortSymbolsByValue(rows, sym, val)).toEqual(['AAA', 'ZZZ'])
  })

  it('keeps symbols whose value is missing, ordered last', () => {
    const rows = [{ s: 'AAA', v: null }, { s: 'BBB', v: 1 }]
    expect(sortSymbolsByValue(rows, sym, val)).toEqual(['BBB', 'AAA'])
  })

  it('returns nothing for no rows', () => {
    const empty: { s: string; v: number | null }[] = []
    expect(sortSymbolsByValue(empty, sym, val)).toEqual([])
  })
})
