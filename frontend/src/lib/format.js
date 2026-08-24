/** Presentation helpers. Kept pure so they are trivially checkable. */

export function formatMs(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`
  if (value >= 100) return `${Math.round(value)} ms`
  return `${value.toFixed(1)} ms`
}

export function formatPercent(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${Math.round(seconds % 60)}s`
}

export function formatTime(epochSeconds) {
  if (!epochSeconds) return '—'
  return new Date(epochSeconds * 1000).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function formatDateTime(epochSeconds) {
  if (!epochSeconds) return '—'
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function repoLabel(url) {
  if (!url) return '—'
  return url.replace(/^https?:\/\//, '').replace(/\.git$/, '')
}

/** Status → the fixed status palette. Never a categorical series colour. */
export const STATUS_TONE = {
  SUCCEEDED: 'good',
  RUNNING: 'warning',
  QUEUED: 'muted',
  FAILED: 'critical',
  ROLLED_BACK: 'serious',
  PENDING: 'muted',
  SKIPPED: 'muted',
}
