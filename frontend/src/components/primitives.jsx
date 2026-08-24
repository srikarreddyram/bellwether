/** Shared presentational primitives. */

export function Card({ title, action, children, className = '' }) {
  return (
    <section className={`card p-4 ${className}`}>
      {(title || action) && (
        <header className="mb-3 flex items-center justify-between gap-3">
          {title && <h2 className="card-title">{title}</h2>}
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

const TONE_STYLES = {
  good: { color: 'var(--status-good)', bg: 'color-mix(in srgb, var(--status-good) 12%, transparent)' },
  warning: { color: 'var(--status-warning)', bg: 'color-mix(in srgb, var(--status-warning) 16%, transparent)' },
  serious: { color: 'var(--status-serious)', bg: 'color-mix(in srgb, var(--status-serious) 14%, transparent)' },
  critical: { color: 'var(--status-critical)', bg: 'color-mix(in srgb, var(--status-critical) 12%, transparent)' },
  muted: { color: 'var(--ink-3)', bg: 'var(--surface-2)' },
}

/**
 * A status pill. Always pairs the colour with a glyph and a text label, so
 * state is never carried by colour alone.
 */
export function StatusPill({ tone = 'muted', icon, children, className = '' }) {
  const style = TONE_STYLES[tone] ?? TONE_STYLES.muted
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.04em] ${className}`}
      style={{ color: style.color, background: style.bg }}
    >
      {icon && <span aria-hidden="true">{icon}</span>}
      {children}
    </span>
  )
}

/**
 * A stat tile. Per the form heuristic, a single headline number is better as a
 * tile than as a chart -- so traffic share, P95 and error rate are read
 * directly rather than plotted.
 */
export function Stat({ label, value, unit, hint, tone, emphasis = false }) {
  const color = tone ? (TONE_STYLES[tone]?.color ?? 'var(--ink-1)') : 'var(--ink-1)'
  return (
    <div className="min-w-0">
      <div className="card-title">{label}</div>
      <div
        className={`mt-1 truncate font-semibold leading-none ${emphasis ? 'text-3xl' : 'text-xl'}`}
        style={{ color }}
      >
        {value}
        {unit && <span className="ml-1 text-sm font-medium text-ink-muted">{unit}</span>}
      </div>
      {hint && <div className="mt-1 truncate text-xs text-ink-muted">{hint}</div>}
    </div>
  )
}

export function Spinner({ className = '' }) {
  return (
    <span
      className={`inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent ${className}`}
      aria-hidden="true"
    />
  )
}
