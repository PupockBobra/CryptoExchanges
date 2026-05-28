import { useState, useEffect } from 'react'
import type { Theme } from '../components/Header'

function readTheme(): Theme {
  return (document.documentElement.getAttribute('data-theme') as Theme) ?? 'light'
}

/**
 * Subscribes to data-theme changes on <html> via MutationObserver.
 * All chart components use this to re-apply colors when the user toggles theme.
 */
export function useTheme(): Theme {
  const [theme, setTheme] = useState<Theme>(readTheme)

  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(readTheme()))
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    return () => observer.disconnect()
  }, [])

  return theme
}

/** Resolved chart color tokens for a given theme. */
export function chartColors(theme: Theme) {
  if (theme === 'light') {
    return {
      bg:         '#ffffff',
      paperBg:    '#f8fafc',
      grid:       '#e2e8f0',
      border:     '#cbd5e1',
      text:       '#64748b',
      volBar:     '#cbd5e1',
      volBarLive: '#94a3b8',
    }
  }
  return {
    bg:         '#0f1117',
    paperBg:    '#1a1d27',
    grid:       '#1f2937',
    border:     '#2d3148',
    text:       '#9ca3af',
    volBar:     '#334155',
    volBarLive: '#475569',
  }
}
