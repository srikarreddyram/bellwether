import { useState } from 'react'

import { Card, Spinner } from './primitives'

/**
 * The deploy control.
 *
 * Two deliberate departures from the previous dashboard:
 *  - Rollback asks for confirmation. It is destructive and it was one click.
 *  - The repository field starts empty. It used to be pre-filled with the
 *    platform's own repository, which is not what anyone wants to deploy and
 *    made the tool look single-purpose.
 */
export default function DeployPanel({ building, allowedHosts, onDeploy, onRollback }) {
  const [repoUrl, setRepoUrl] = useState('')
  const [launchJson, setLaunchJson] = useState('')
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [localError, setLocalError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    setLocalError(null)

    let launch
    if (launchJson.trim()) {
      try {
        launch = JSON.parse(launchJson)
      } catch {
        setLocalError('The launch override is not valid JSON.')
        return
      }
    }

    setBusy(true)
    try {
      await onDeploy(repoUrl.trim(), launch)
    } catch {
      /* surfaced by the shared error banner */
    } finally {
      setBusy(false)
    }
  }

  const rollback = async () => {
    const confirmed = window.confirm(
      'Roll back now?\n\n' +
        'Traffic returns to 0% canary, the canary instance is stopped, and the ' +
        'run is recorded as rolled back in the audit trail.',
    )
    if (!confirmed) return
    setBusy(true)
    try {
      await onRollback('operator requested rollback from the dashboard')
    } catch {
      /* surfaced by the shared error banner */
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Deploy">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="min-w-0 flex-1">
            <label htmlFor="repo-url" className="sr-only">
              Repository URL
            </label>
            <input
              id="repo-url"
              type="text"
              value={repoUrl}
              onChange={(event) => setRepoUrl(event.target.value)}
              placeholder="https://github.com/owner/repository.git"
              className="field font-mono"
              autoComplete="off"
              spellCheck="false"
              disabled={building}
            />
          </div>
          <div className="flex gap-2">
            <button type="submit" className="btn-primary" disabled={building || busy || !repoUrl.trim()}>
              {building || busy ? <Spinner /> : null}
              {building ? 'Rollout in progress' : 'Start rollout'}
            </button>
            <button type="button" onClick={rollback} className="btn-danger" disabled={busy}>
              Roll back
            </button>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-ink-muted">
          <span>
            Allowed hosts: {(allowedHosts ?? []).join(', ') || 'none configured'}
          </span>
          <button
            type="button"
            onClick={() => setShowAdvanced((value) => !value)}
            className="text-ink-soft underline underline-offset-2 hover:text-ink"
            aria-expanded={showAdvanced}
          >
            {showAdvanced ? 'Hide' : 'Show'} launch override
          </button>
        </div>

        {showAdvanced && (
          <div className="rounded-md border border-line bg-plane p-3">
            <label htmlFor="launch-override" className="card-title">
              Launch override (JSON)
            </label>
            <p className="mb-2 mt-1 text-xs text-ink-muted">
              Describe how to build and run a repository whose runtime cannot be detected — without
              modifying that repository. Overrides its own <code>.bellwether.yml</code>, if any.
            </p>
            <textarea
              id="launch-override"
              value={launchJson}
              onChange={(event) => setLaunchJson(event.target.value)}
              rows={5}
              spellCheck="false"
              placeholder={'{\n  "build": ["make build"],\n  "start": "./bin/server --port ${PORT}",\n  "health_path": "/healthz"\n}'}
              className="field font-mono text-xs"
            />
          </div>
        )}

        {localError && (
          <p role="alert" className="text-xs" style={{ color: 'var(--status-critical)' }}>
            {localError}
          </p>
        )}
      </form>
    </Card>
  )
}
