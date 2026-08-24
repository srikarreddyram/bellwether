import { formatDuration } from '../lib/format'

/**
 * The stage rail.
 *
 * Stages come from the backend's own catalogue over `/api/config`. The previous
 * dashboard hardcoded a parallel `STAGE_MAP` whose documentation warned that
 * the strings "must match Orchestrator stage names exactly, including case and
 * spacing" — a comment describing a bug waiting to happen. There is now one
 * definition, on the side that actually runs the stages.
 */

const VISUALS = {
  SUCCEEDED: { ring: 'var(--status-good)', text: 'var(--status-good)', glyph: '✓' },
  RUNNING: { ring: 'var(--status-warning)', text: 'var(--status-warning)', glyph: '●' },
  FAILED: { ring: 'var(--status-critical)', text: 'var(--status-critical)', glyph: '✕' },
  ROLLED_BACK: { ring: 'var(--status-serious)', text: 'var(--status-serious)', glyph: '↩' },
  SKIPPED: { ring: 'var(--axis)', text: 'var(--ink-3)', glyph: '–' },
  PENDING: { ring: 'var(--axis)', text: 'var(--ink-3)', glyph: '' },
}

export default function PipelineRail({ stages }) {
  return (
    <ol className="flex list-none gap-1 overflow-x-auto p-0" aria-label="Rollout stages">
      {stages.map((stage, index) => {
        const visual = VISUALS[stage.status] ?? VISUALS.PENDING
        const isLast = index === stages.length - 1
        return (
          <li key={stage.key} className="relative flex min-w-[92px] flex-1 flex-col items-center">
            <div className="flex w-full items-center">
              <span className="h-px flex-1" style={{ background: index === 0 ? 'transparent' : 'var(--grid)' }} />
              <span
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-2 text-[11px] font-bold"
                style={{
                  borderColor: visual.ring,
                  color: visual.text,
                  background: 'var(--surface-1)',
                }}
                title={stage.description}
              >
                {stage.status === 'RUNNING' ? (
                  <span className="h-2 w-2 animate-pulse rounded-full" style={{ background: visual.ring }} />
                ) : (
                  visual.glyph
                )}
              </span>
              <span className="h-px flex-1" style={{ background: isLast ? 'transparent' : 'var(--grid)' }} />
            </div>

            <div className="mt-2 px-1 text-center">
              <div className="text-[11px] font-medium leading-tight" style={{ color: visual.text }}>
                {stage.title}
              </div>
              <div className="mt-0.5 text-[10px] text-ink-muted [font-variant-numeric:tabular-nums]">
                {stage.durationS != null
                  ? formatDuration(stage.durationS)
                  : stage.trafficPct > 0
                    ? `${stage.trafficPct}%`
                    : ''}
              </div>
            </div>
            <span className="sr-only">{stage.status}</span>
          </li>
        )
      })}
    </ol>
  )
}
