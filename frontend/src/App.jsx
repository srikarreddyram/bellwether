import Console from './components/Console'
import DeployPanel from './components/DeployPanel'
import Header from './components/Header'
import HistoryTable from './components/HistoryTable'
import LatencyChart from './components/LatencyChart'
import PipelineRail from './components/PipelineRail'
import RiskPanel from './components/RiskPanel'
import TrafficSplit from './components/TrafficSplit'
import { Card, Stat, StatusPill } from './components/primitives'
import { usePlatform, useTheme } from './hooks/usePlatform'
import { formatMs, formatPercent, repoLabel, STATUS_TONE } from './lib/format'

export default function App() {
  const [theme, setTheme] = useTheme()
  const {
    connected, config, status, stages, consoleLines, history,
    telemetry, risk, chaos, error, clearError, deploy, rollback, toggleChaos,
  } = usePlatform()

  const run = status.run
  const canary = telemetry?.cohorts?.canary
  const thresholds = telemetry?.thresholds

  return (
    <div className="mx-auto max-w-6xl px-4 py-5 sm:px-6">
      <Header connected={connected} config={config} theme={theme} setTheme={setTheme} />

      {error && (
        <div
          role="alert"
          className="mb-4 flex items-start justify-between gap-3 rounded-md border px-3 py-2.5 text-sm"
          style={{
            borderColor: 'color-mix(in srgb, var(--status-critical) 35%, transparent)',
            background: 'color-mix(in srgb, var(--status-critical) 8%, transparent)',
            color: 'var(--status-critical)',
          }}
        >
          <span className="flex gap-2">
            <span aria-hidden="true">⚠</span>
            {error}
          </span>
          <button type="button" onClick={clearError} className="shrink-0 underline underline-offset-2">
            Dismiss
          </button>
        </div>
      )}

      <div className="grid gap-4">
        <DeployPanel
          building={status.building}
          allowedHosts={config?.allowedRepoHosts}
          onDeploy={deploy}
          onRollback={rollback}
        />

        <Card
          title={run ? `Run ${run.number} · ${repoLabel(run.repoUrl)}` : 'Rollout'}
          action={
            run ? (
              <StatusPill tone={STATUS_TONE[run.status] ?? 'muted'} icon={run.status === 'RUNNING' ? '●' : '·'}>
                {run.status.replace('_', ' ')}
              </StatusPill>
            ) : (
              <StatusPill tone="muted" icon="○">Idle</StatusPill>
            )
          }
        >
          <PipelineRail stages={stages} />
        </Card>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
          <div className="grid gap-4">
            <Card>
              <TrafficSplit trafficPct={status.trafficPct} proxyHealthy={status.proxyHealthy} />
            </Card>

            <Card title="Live canary health">
              <div className="grid grid-cols-2 gap-4">
                <Stat
                  label="Latency P95"
                  value={formatMs(canary?.latencyP95Ms)}
                  hint={`abort above ${formatMs(thresholds?.latencyP95Ms)}`}
                  tone={
                    canary?.latencyP95Ms > (thresholds?.latencyP95Ms ?? Infinity) ? 'critical' : undefined
                  }
                />
                <Stat
                  label="Error rate"
                  value={formatPercent(canary?.errorRate)}
                  hint={`abort above ${formatPercent(thresholds?.errorRate)}`}
                  tone={canary?.errorRate > (thresholds?.errorRate ?? Infinity) ? 'critical' : undefined}
                />
                <Stat label="Canary requests" value={canary?.count ?? 0} />
                <Stat
                  label="Telemetry age"
                  value={telemetry?.ageS != null ? `${Math.round(telemetry.ageS)}s` : '—'}
                  hint={telemetry?.sampleCount ? `${telemetry.sampleCount} in window` : 'no samples'}
                />
              </div>

              {config?.chaosAvailable && (
                <label className="mt-4 flex items-center gap-2 border-t border-line pt-3 text-xs text-ink-soft">
                  <input
                    type="checkbox"
                    checked={chaos.enabled}
                    onChange={(event) => toggleChaos(event.target.checked)}
                    className="accent-[var(--status-critical)]"
                  />
                  Inject faults into the canary (rollback drill)
                </label>
              )}
            </Card>
          </div>

          <Card title="Request latency by cohort">
            <LatencyChart telemetry={telemetry} />
          </Card>
        </div>

        <RiskPanel risk={risk} telemetry={telemetry} />

        <div className="grid gap-4 lg:grid-cols-2">
          <Console lines={consoleLines} />
          <HistoryTable history={history} />
        </div>
      </div>

      <footer className="mt-6 border-t border-line pt-4 text-xs text-ink-muted">
        Bellwether {config?.version ?? ''} · risk policy on missing data:{' '}
        <span className="font-medium text-ink-soft">{config?.risk?.insufficientDataPolicy ?? '—'}</span>
        {config?.proxy?.stickySessions && ' · sticky cohort sessions enabled'}
      </footer>
    </div>
  )
}
