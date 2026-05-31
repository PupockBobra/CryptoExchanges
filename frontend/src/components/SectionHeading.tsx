interface Props {
  label:  string
  count?: number
  accent?: boolean
  icon?:  React.ReactNode
}

/** Section divider used by Analytics, DailyVolume, History, Launches pages. */
export function SectionHeading({ label, count, accent, icon }: Props) {
  return (
    <div style={{ margin: '24px 0 10px' }}>
      <h2 style={{
        margin: 0,
        fontSize: 11,
        fontWeight: 700,
        textTransform: 'uppercase',
        letterSpacing: '.1em',
        color: accent ? '#10b981' : 'var(--muted)',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
      }}>
        {icon}
        {label}
        {count != null && (
          <span style={{ fontWeight: 400, opacity: 0.6 }}>({count})</span>
        )}
      </h2>
      <div style={{
        height: 1,
        background: accent ? '#10b98140' : 'var(--border)',
        marginTop: 6,
      }} />
    </div>
  )
}
