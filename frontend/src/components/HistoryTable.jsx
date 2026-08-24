import { formatDateTime, formatDuration, repoLabel, STATUS_TONE } from '../lib/format'
import { Card, StatusPill } from './primitives'

const GLYPH = {
  SUCCEEDED: '✓',
  FAILED: '✕',
  ROLLED_BACK: '↩',
  RUNNING: '●',
  QUEUED: '○',
}

export default function HistoryTable({ history }) {
  const runs = history?.runs ?? []
  const stats = history?.stats ?? {}

  return (
    <Card
      title="Deployment history"
      action={
        stats.total > 0 && (
          <span className="text-xs text-ink-muted [font-variant-numeric:tabular-nums]">
            {stats.total} runs
            {stats.successRate != null && ` · ${Math.round(stats.successRate * 100)}% promoted`}
            {stats.avgDurationS != null && ` · ${formatDuration(stats.avgDurationS)} avg`}
          </span>
        )
      }
    >
      {runs.length === 0 ? (
        <p className="py-6 text-center text-sm text-ink-muted">No deployments recorded yet.</p>
      ) : (
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[440px] text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-[0.04em] text-ink-muted">
                <th scope="col" className="px-1 pb-2 font-semibold">#</th>
                <th scope="col" className="px-1 pb-2 font-semibold">Repository</th>
                <th scope="col" className="px-1 pb-2 font-semibold">Status</th>
                <th scope="col" className="px-1 pb-2 text-right font-semibold">Duration</th>
                <th scope="col" className="px-1 pb-2 text-right font-semibold">Started</th>
              </tr>
            </thead>
            <tbody>
              {runs.slice(0, 8).map((run) => (
                <tr key={run.id} className="border-t border-line">
                  <td className="px-1 py-2 font-mono text-xs text-ink-muted [font-variant-numeric:tabular-nums]">
                    {run.number}
                  </td>
                  <td className="max-w-[220px] truncate px-1 py-2 text-ink" title={run.repoUrl}>
                    {repoLabel(run.repoUrl)}
                  </td>
                  <td className="px-1 py-2">
                    <StatusPill
                      tone={STATUS_TONE[run.status] ?? 'muted'}
                      icon={GLYPH[run.status]}
                    >
                      {run.status.replace('_', ' ')}
                    </StatusPill>
                  </td>
                  <td className="px-1 py-2 text-right text-ink-soft [font-variant-numeric:tabular-nums]">
                    {formatDuration(run.durationS)}
                  </td>
                  <td className="px-1 py-2 text-right text-xs text-ink-muted">
                    {formatDateTime(run.createdAt)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
