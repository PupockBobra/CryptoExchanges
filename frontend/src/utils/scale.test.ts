import { describe, it, expect } from 'vitest'
import { pickUnit, maxStackedTotal } from './scale'

describe('pickUnit', () => {
  it('picks the unit the value actually reaches', () => {
    expect(pickUnit(2.4e12)).toEqual({ scale: 1e12, suffix: 'T' })
    expect(pickUnit(83e9)).toEqual({ scale: 1e9,  suffix: 'B' })
    expect(pickUnit(9.9e8)).toEqual({ scale: 1e6,  suffix: 'M' })
    expect(pickUnit(4200)).toEqual({ scale: 1e3,  suffix: 'K' })
    expect(pickUnit(12)).toEqual({ scale: 1, suffix: '' })
  })

  it('switches unit exactly at 1 of that unit', () => {
    expect(pickUnit(1e9).suffix).toBe('B')
    expect(pickUnit(1e9 - 1).suffix).toBe('M')
  })

  it('keeps a Korean-scale instrument in millions instead of flattening it to 0.0B', () => {
    const { scale, suffix } = pickUnit(3.7e6)
    expect(suffix).toBe('M')
    expect(3.7e6 / scale).toBeCloseTo(3.7)
  })

  it('keeps a BTC-scale OI in billions instead of 500,000M', () => {
    const { scale, suffix } = pickUnit(5e11)
    expect(suffix).toBe('B')
    expect(5e11 / scale).toBe(500)
  })

  it('falls back to millions on empty or non-finite data', () => {
    expect(pickUnit(0)).toEqual({ scale: 1e6, suffix: 'M' })
    expect(pickUnit(NaN)).toEqual({ scale: 1e6, suffix: 'M' })
  })
})

describe('maxStackedTotal', () => {
  it('sums per category — the stacked total sets the axis, not one segment', () => {
    const rows = [
      { key: 'w1', value: 3 }, { key: 'w1', value: 4 },   // 7
      { key: 'w2', value: 5 },                            // 5
    ]
    expect(maxStackedTotal(rows)).toBe(7)
  })

  it('ignores null and non-finite values', () => {
    const rows = [
      { key: 'd1', value: null }, { key: 'd1', value: 2 },
      { key: 'd2', value: Infinity },
    ]
    expect(maxStackedTotal(rows)).toBe(2)
  })

  it('returns 0 for no data', () => {
    expect(maxStackedTotal([])).toBe(0)
  })
})
