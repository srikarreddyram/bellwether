/**
 * The live traffic split, as a single stacked bar.
 *
 * The number is the headline, so it is a stat, not a chart; the bar exists to
 * show the *relationship* between the two cohorts at a glance. Segments carry a
 * 2px surface gap and both are directly labelled, so the split is readable
 * without consulting a legend and without relying on colour.
 */
export default function TrafficSplit({ trafficPct, proxyHealthy }) {
  const canary = Math.max(0, Math.min(100, trafficPct ?? 0))
  const stable = 100 - canary

  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <div className="card-title">Canary traffic</div>
          <div
            className="mt-1 text-4xl font-semibold leading-none"
            style={{ color: canary > 0 ? 'var(--series-canary)' : 'var(--ink-3)' }}
          >
            {canary}
            <span className="ml-0.5 text-xl text-ink-muted">%</span>
          </div>
        </div>
        <span className="text-xs text-ink-muted">
          {proxyHealthy ? 'proxy routing' : 'proxy offline'}
        </span>
      </div>

      <div
        className="mt-4 flex h-3 w-full overflow-hidden rounded-full"
        style={{ background: 'var(--surface-2)' }}
        role="img"
        aria-label={`${stable}% of traffic to stable, ${canary}% to canary`}
      >
        {stable > 0 && (
          <div
            className="h-full transition-[width] duration-500"
            style={{
              width: `${stable}%`,
              background: 'var(--series-stable)',
              // 2px surface gap between adjacent segments.
              marginRight: canary > 0 ? 2 : 0,
            }}
          />
        )}
        {canary > 0 && (
          <div
            className="h-full flex-1 transition-[width] duration-500"
            style={{ background: 'var(--series-canary)' }}
          />
        )}
      </div>

      <div className="mt-2 flex justify-between text-xs">
        <span className="inline-flex items-center gap-1.5 text-ink-soft">
          <span className="h-2 w-2 rounded-full" style={{ background: 'var(--series-stable)' }} />
          Stable {stable}%
        </span>
        <span className="inline-flex items-center gap-1.5 text-ink-soft">
          <span className="h-2 w-2 rounded-full" style={{ background: 'var(--series-canary)' }} />
          Canary {canary}%
        </span>
      </div>
    </div>
  )
}
