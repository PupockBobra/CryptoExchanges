import { describe, it, expect } from 'vitest'
import { mskHourLabel, hourTick } from './hourly'

describe('mskHourLabel', () => {
  it('shifts UTC to Moscow time', () => {
    expect(mskHourLabel('2026-07-31T11:00:00+00:00')).toBe('31.07 14:00')
  })

  it('rolls over to the next day past 21:00 UTC', () => {
    expect(mskHourLabel('2026-07-31T22:00:00+00:00')).toBe('01.08 01:00')
  })

  it('rolls over month and year boundaries', () => {
    expect(mskHourLabel('2026-12-31T23:00:00+00:00')).toBe('01.01 02:00')
  })

  it('pads single-digit day, month and hour', () => {
    expect(mskHourLabel('2026-01-05T04:00:00+00:00')).toBe('05.01 07:00')
  })
})

describe('profile axis', () => {
  it('formats hour ticks', () => {
    expect(hourTick(0)).toBe('00:00')
    expect(hourTick(9)).toBe('09:00')
    expect(hourTick(23)).toBe('23:00')
  })
})
