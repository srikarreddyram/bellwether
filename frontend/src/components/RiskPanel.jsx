import { formatMs, formatPercent } from '../lib/format'
import { Card, StatusPill } from './primitives'

/**
 * The risk gate's decision and the evidence behind it.
 *
 * `dataSource` is shown prominently and on purpose. The previous engine fell
 * back to `random.uniform()` whenever telemetry was missing — which, because
 * the proxy was never started, was always — and presented the result
 * indistinguishably from a real measurement. A simulated verdict is now
 * labelled as one.
 */

const SOURCE_NOTE = {
  telemetry: { tone: 'good', label: 'Measured', text: 'Decided from real proxied requests.' },
  simulated: {
    tone: 'warning',
    label: 'Simulated',
    text: 'Metrics were synthesised — this verdict is not based on real traffic.',
  },
  insufficient: {
    tone: 'serious',
    label: 'No evidence',
    text: 'Not enough canary traffic to judge the build.',
  },
}

export default function RiskPanel({ risk, telemetry }) {
  const canary = risk?.canary ?? telemetry?.cohorts?.canary
  const stable = risk?.stable ?? telemetry?.cohorts?.stable
  const thresholds = risk?.thresholds ?? telemetry?.thresholds
  const source = risk ? SOURCE_NOTE[risk.dataSource] : null

  return (
    <Card
      title="Risk gate"
      action={
        risk ? (
          <StatusPill
            tone={risk.decision === 'PROMOTE' ? 'good' : 'critical'}
            icon={risk.decision === 'PROMOTE' ? '✓' : '✕'}
          >
            {risk.decision}
          </StatusPill>
        ) : (
          <StatusPill tone="muted" icon="○">Not scored</StatusPill>
        )
      }
    >
      {source && (
        <div className="mb-3 flex items-start gap-2 rounded-md border border-line bg-plane px-3 py-2">
          <StatusPill tone={source.tone} icon={source.tone === 'good' ? '◎' : '⚠'}>
            {source.label}
          </StatusPill>
          <p className="text-xs leading-relaxed text-ink-soft">{source.text}</p>
        </div>
      )}

      <table className="w-full text-sm">
        <caption className="sr-only">Canary and stable cohort health against abort thresholds</caption>
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-[0.04em] text-ink-muted">
            <th scope="col" className="pb-2 font-semibold">Metric</th>
            <th scope="col" className="pb-2 text-right font-semibold">
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full" style={{ background: 'var(--series-stable)' }} />
                Stable
              </span>
            </th>
            <th scope="col" className="pb-2 text-right font-semibold">
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full" style={{ background: 'var(--series-canary)' }} />
                Canary
              </span>
            </th>
            <th scope="col" className="pb-2 text-right font-semibold">Abort above</th>
          </tr>
        </thead>
        <tbody className="[font-variant-numeric:tabular-nums]">
          <Row
            label="Latency P95"
            stable={formatMs(stable?.latencyP95Ms)}
            canary={formatMs(canary?.latencyP95Ms)}
            limit={formatMs(thresholds?.latencyP95Ms)}
            breached={
              canary?.latencyP95Ms != null &&
              thresholds?.latencyP95Ms != null &&
              canary.latencyP95Ms > thresholds.latencyP95Ms
            }
          />
          <Row
            label="Error rate"
            stable={formatPercent(stable?.errorRate)}
            canary={formatPercent(canary?.errorRate)}
            limit={formatPercent(thresholds?.errorRate)}
            breached={
              canary?.errorRate != null &&
              thresholds?.errorRate != null &&
              canary.errorRate > thresholds.errorRate
            }
          />
          <Row
            label="Requests observed"
            stable={stable?.count ?? 0}
            canary={canary?.count ?? 0}
            limit="—"
          />
        </tbody>
      </table>

      {risk?.reasons?.length > 0 && (
        <ul className="mt-3 space-y-1 border-t border-line pt-3 text-xs text-ink-soft">
          {risk.reasons.map((reason, index) => (
            <li key={index} className="flex gap-2">
              <span aria-hidden="true" className="text-ink-muted">·</span>
              <span>{reason}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

function Row({ label, stable, canary, limit, breached }) {
  return (
    <tr className="border-t border-line">
      <th scope="row" className="py-2 text-left font-normal text-ink-soft">
        {label}
      </th>
      <td className="py-2 text-right text-ink">{stable}</td>
      <td
        className="py-2 text-right font-medium"
        style={{ color: breached ? 'var(--status-critical)' : 'var(--ink-1)' }}
      >
        {breached && <span aria-hidden="true" className="mr-1">⚠</span>}
        {canary}
        {breached && <span className="sr-only"> (threshold breached)</span>}
      </td>
      <td className="py-2 text-right text-ink-muted">{limit}</td>
    </tr>
  )
}
