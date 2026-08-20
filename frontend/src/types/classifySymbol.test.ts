import { describe, it, expect } from 'vitest'
import { classifySymbol, formatSymbol, SYMBOL_SECTIONS } from './index'

describe('classifySymbol', () => {
  it('puts the index perps in their own section, not US Market', () => {
    expect(classifySymbol('QQQ/USDT:USDT')).toBe('Indexes')
    expect(classifySymbol('SPY/USDT:USDT')).toBe('Indexes')
  })

  it('keeps the curated instrument sections intact', () => {
    expect(classifySymbol('BRN/USDT:USDT')).toBe('Commodities')
    expect(classifySymbol('XAU/USDT:USDT')).toBe('Precious Metals')
    expect(classifySymbol('AAPL/USDT:USDT')).toBe('US Market')
    expect(classifySymbol('SAMSUNG/USDT:USDT')).toBe('Korean Market')
    expect(classifySymbol('BTC/USDT')).toBe('Crypto Perps')
  })

  it('classifies a backend-supplied stock ticker as US Market', () => {
    const top = new Set(['AVGO', 'PLTR'])
    expect(classifySymbol('AVGO/USDT:USDT', top)).toBe('US Market')
    expect(classifySymbol('PLTR/USDT:USDT', top)).toBe('US Market')
  })

  it('does not swallow crypto when a ticker set is supplied', () => {
    const top = new Set(['AVGO'])
    expect(classifySymbol('SOL/USDT', top)).toBe('Crypto Perps')
    expect(classifySymbol('XAU/USDT:USDT', top)).toBe('Precious Metals')
  })

  it('leaves an unknown ticker in the catch-all when no set is supplied', () => {
    expect(classifySymbol('AVGO/USDT:USDT')).toBe('Crypto Perps')
  })

  it('keeps the SK Hynix ADR contract with the Korean cards', () => {
    expect(classifySymbol('SKHY/USDT:USDT')).toBe('Korean Market')
    // even when the ranking hands it over as a stock ticker
    expect(classifySymbol('SKHY/USDT:USDT', new Set(['SKHY']))).toBe('Korean Market')
  })

  it('renders Indexes above US Market in the section order', () => {
    const labels = SYMBOL_SECTIONS.map(s => s.label)
    expect(labels.indexOf('Indexes')).toBeLessThan(labels.indexOf('US Market'))
    expect(labels[labels.length - 1]).toBe('Crypto Perps')   // catch-all stays last
  })
})

describe('formatSymbol', () => {
  it('marks the ADR contract so it is not read as a second listing', () => {
    expect(formatSymbol('SKHY/USDT:USDT')).toBe('SKHY/USDT PERP · ADR')
  })

  it('leaves every other symbol untouched', () => {
    expect(formatSymbol('XAU/USDT:USDT')).toBe('XAU/USDT PERP')
    expect(formatSymbol('BTC/USDT')).toBe('BTC/USDT')
  })
})
