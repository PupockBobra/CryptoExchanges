/**
 * Shared formatting helpers used across pages.
 * Single source of truth — prevents drift between Launches / Exchanges / News.
 */

/** "1 day ago", "3 days ago", "2mo ago", "1y ago", or "—" for null. */
export function daysAgo(dateStr: string | null): string {
  if (!dateStr) return '—'
  const diff = Math.floor((Date.now() - new Date(dateStr).getTime()) / 86400000)
  if (diff <= 0)   return 'today'
  if (diff === 1)  return '1 day ago'
  if (diff < 30)   return `${diff} days ago`
  if (diff < 365)  return `${Math.floor(diff / 30)}mo ago`
  return `${Math.floor(diff / 365)}y ago`
}

/** "just now" / "Ns ago" / "Nm ago" / "Nh ago" / "Nd ago". */
export function timeAgo(ts: string | null): string {
  if (!ts) return '—'
  const diff = Math.floor((Date.now() - new Date(ts).getTime()) / 1000)
  if (diff < 5)    return 'just now'
  if (diff < 60)   return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

/** Format USD/USDT volume to compact human-readable string. */
export function fmtVolume(v?: number | null): string {
  if (!v) return '—'
  if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `$${(v / 1e3).toFixed(0)}K`
  return `$${v.toFixed(0)}`
}
