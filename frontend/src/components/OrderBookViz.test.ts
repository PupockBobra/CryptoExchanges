import { describe, it, expect } from 'vitest'
import { stripClosedDays, tradingBreaks, tradingTicks } from './OrderBookViz'

// UTC buckets → MSK is +3h
const b = (d: string, hm: string) => `${d}T${hm}:00+00:00`

describe('closed days', () => {
  it('drops Aug 1-2 points (MSK) from the series', () => {
    const buckets = [
      b('2026-07-31', '17:00'),  // 20:00 MSK Fri
      b('2026-08-01', '10:00'),  // 13:00 MSK Sat — closed
      b('2026-08-02', '13:00'),  // 16:00 MSK Sun — closed
      b('2026-08-03', '05:00'),  // 08:00 MSK Mon
    ]
    const { x, y } = stripClosedDays(buckets, [1, 2, 3, 4])
    expect(y).toEqual([1, 4])
    expect(x).toEqual(['2026-07-31T20:00:00.000', '2026-08-03T08:00:00.000'])
  })

  it('a bucket at 21:30 UTC Jul 31 = 00:30 MSK Aug 1 is closed too', () => {
    const { y } = stripClosedDays([b('2026-07-31', '21:30')], [7])
    expect(y).toEqual([])
  })

  it('cuts Jul 31 close → Aug 3 open as one rangebreak', () => {
    const breaks = tradingBreaks([b('2026-07-31', '10:00'), b('2026-08-03', '10:00')])
    const spanning = breaks.filter(br => br.values[0].startsWith('2026-07-31'))
    expect(spanning).toHaveLength(1)
    // 23:45 MSK Fri +1min → 07:00 MSK Mon = 55h14m
    expect(spanning[0].dvalue).toBe(((55 * 60) + 14) * 60_000)
  })

  it('places no tick inside a closed day', () => {
    const ticks = tradingTicks([b('2026-07-29', '10:00'), b('2026-08-03', '10:00')])!
    expect(ticks.tickvals.some(t => t.startsWith('2026-08-01') || t.startsWith('2026-08-02'))).toBe(false)
  })
})
