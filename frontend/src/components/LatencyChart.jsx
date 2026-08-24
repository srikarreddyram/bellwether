import { useMemo, useRef, useState } from 'react'

import { formatMs, formatTime } from '../lib/format'

/**
 * Request latency over time, one line per cohort, against the abort threshold.
 *
 * This charts the *proxy's own request stream*. The previous dashboard plotted
 * one point per MLflow run under the label "Canary Latency" — a build history,
 * not a latency trace — so a 20-build record looked like a 20-second window.
 *
 * Written as plain SVG rather than a charting library: two series, one
 * reference line and a crosshair do not justify the dependency, and it lets the
 * marks follow spec exactly (2px strokes, ≥8px hit targets, a 2px surface ring
 * where the lines overlap).
 */

const PADDING = { top: 16, right: 16, bottom: 26, left: 46 }
const COHORTS = [
  { key: 'stable', label: 'Stable', color: 'var(--series-stable)' },
  { key: 'canary', label: 'Canary', color: 'var(--series-canary)' },
]

export default function LatencyChart({ telemetry, height = 220 }) {
  const [hover, setHover] = useState(null)
  const svgRef = useRef(null)

  const samples = useMemo(() => telemetry?.samples ?? [], [telemetry])
  const threshold = telemetry?.thresholds?.latencyP95Ms ?? null

  const series = useMemo(() => {
    const grouped = { stable: [], canary: [] }
    for (const sample of samples) {
      if (grouped[sample.cohort]) grouped[sample.cohort].push(sample)
    }
    return grouped
  }, [samples])

  if (!samples.length) {
    return (
      <EmptyState
        height={height}
        message="No traffic recorded yet"
        hint="The proxy starts measuring once a rollout reaches its first traffic shift."
      />
    )
  }

  const width = 720
  const plotWidth = width - PADDING.left - PADDING.right
  const plotHeight = height - PADDING.top - PADDING.bottom

  const times = samples.map((s) => s.timestamp)
  const minTime = Math.min(...times)
  const maxTime = Math.max(...times)
  const timeSpan = Math.max(maxTime - minTime, 1)

  const latencies = samples.map((s) => s.latencyMs)
  // Keep the threshold on screen when it is close to the data, but never let a
  // 500ms threshold flatten a 20ms trace into a line along the axis.
  const dataMax = Math.max(...latencies)
  const yMax = threshold && threshold <= dataMax * 3 ? Math.max(dataMax, threshold) * 1.15 : dataMax * 1.15
  const yScale = (value) => PADDING.top + plotHeight - (value / yMax) * plotHeight
  const xScale = (time) => PADDING.left + ((time - minTime) / timeSpan) * plotWidth

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => yMax * fraction)
  // Only advertise the threshold in the legend when it is actually drawn --
  // a 500ms limit is off-scale against a 4ms trace.
  const thresholdVisible = threshold != null && threshold <= yMax

  const handleMove = (event) => {
    const svg = svgRef.current
    if (!svg) return
    const bounds = svg.getBoundingClientRect()
    const x = ((event.clientX - bounds.left) / bounds.width) * width
    const time = minTime + ((x - PADDING.left) / plotWidth) * timeSpan

    let nearest = null
    let best = Infinity
    for (const sample of samples) {
      const distance = Math.abs(sample.timestamp - time)
      if (distance < best) {
        best = distance
        nearest = sample
      }
    }
    if (nearest) setHover({ sample: nearest, x: xScale(nearest.timestamp) })
  }

  return (
    <figure className="m-0">
      <div className="relative">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${width} ${height}`}
          className="w-full"
          style={{ height }}
          role="img"
          aria-label={`Request latency over time for stable and canary, against a ${threshold} millisecond threshold`}
          onMouseMove={handleMove}
          onMouseLeave={() => setHover(null)}
        >
          {/* Gridlines — recessive hairlines, never competing with the data. */}
          {ticks.map((tick) => (
            <g key={tick}>
              <line
                x1={PADDING.left}
                x2={width - PADDING.right}
                y1={yScale(tick)}
                y2={yScale(tick)}
                stroke="var(--grid)"
                strokeWidth="1"
              />
              <text
                x={PADDING.left - 8}
                y={yScale(tick) + 3}
                textAnchor="end"
                className="fill-[var(--ink-3)] text-[10px] [font-variant-numeric:tabular-nums]"
              >
                {Math.round(tick)}
              </text>
            </g>
          ))}

          {/* Abort threshold — status critical, dashed so it never reads as a series. */}
          {thresholdVisible && (
            <g>
              <line
                x1={PADDING.left}
                x2={width - PADDING.right}
                y1={yScale(threshold)}
                y2={yScale(threshold)}
                stroke="var(--status-critical)"
                strokeWidth="1.5"
                strokeDasharray="5 4"
              />
              <text
                x={width - PADDING.right}
                y={yScale(threshold) - 5}
                textAnchor="end"
                className="fill-[var(--status-critical)] text-[10px] font-medium"
              >
                abort above {Math.round(threshold)} ms
              </text>
            </g>
          )}

          {COHORTS.map(({ key, color }) => {
            const points = series[key]
            if (points.length < 1) return null
            const path = points
              .map((s, i) => `${i === 0 ? 'M' : 'L'} ${xScale(s.timestamp)} ${yScale(s.latencyMs)}`)
              .join(' ')
            return (
              <g key={key}>
                {/* A 2px surface ring keeps the lines legible where they cross. */}
                <path d={path} fill="none" stroke="var(--surface-1)" strokeWidth="5"
                      strokeLinejoin="round" strokeLinecap="round" opacity="0.85" />
                <path d={path} fill="none" stroke={color} strokeWidth="2"
                      strokeLinejoin="round" strokeLinecap="round" />
                {points.length === 1 && (
                  <circle cx={xScale(points[0].timestamp)} cy={yScale(points[0].latencyMs)}
                          r="4" fill={color} />
                )}
              </g>
            )
          })}

          {hover && (
            <g>
              <line x1={hover.x} x2={hover.x} y1={PADDING.top} y2={PADDING.top + plotHeight}
                    stroke="var(--axis)" strokeWidth="1" />
              <circle
                cx={hover.x}
                cy={yScale(hover.sample.latencyMs)}
                r="5"
                fill={hover.sample.cohort === 'canary' ? 'var(--series-canary)' : 'var(--series-stable)'}
                stroke="var(--surface-1)"
                strokeWidth="2"
              />
            </g>
          )}

          <line x1={PADDING.left} x2={width - PADDING.right}
                y1={PADDING.top + plotHeight} y2={PADDING.top + plotHeight}
                stroke="var(--axis)" strokeWidth="1" />
        </svg>

        {hover && (
          <div
            className="pointer-events-none absolute z-10 rounded-md border border-line bg-surface px-2.5 py-1.5 text-xs shadow-lg"
            style={{
              left: `${Math.min((hover.x / width) * 100, 78)}%`,
              top: 8,
            }}
          >
            <div className="font-medium text-ink">{formatMs(hover.sample.latencyMs)}</div>
            <div className="mt-0.5 flex items-center gap-1.5 text-ink-soft">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{
                  background:
                    hover.sample.cohort === 'canary'
                      ? 'var(--series-canary)'
                      : 'var(--series-stable)',
                }}
              />
              {hover.sample.cohort} · HTTP {hover.sample.statusCode}
            </div>
            <div className="text-ink-muted">{formatTime(hover.sample.timestamp)}</div>
          </div>
        )}
      </div>

      {/* Legend is always present for two series, so identity is never colour alone. */}
      <figcaption className="mt-2 flex flex-wrap items-center gap-4 text-xs text-ink-soft">
        {COHORTS.map(({ key, label, color }) => (
          <span key={key} className="inline-flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-4 rounded-full" style={{ background: color }} />
            {label}
            <span className="text-ink-muted">({series[key].length})</span>
          </span>
        ))}
        {thresholdVisible ? (
          <span className="inline-flex items-center gap-1.5 text-ink-muted">
            <span className="inline-block h-0 w-4 border-t-2 border-dashed"
                  style={{ borderColor: 'var(--status-critical)' }} />
            abort threshold
          </span>
        ) : (
          threshold != null && (
            <span className="text-ink-muted">
              abort threshold {Math.round(threshold)} ms — above this range
            </span>
          )
        )}
      </figcaption>
    </figure>
  )
}

function EmptyState({ height, message, hint }) {
  return (
    <div
      className="flex flex-col items-center justify-center gap-1 rounded-md border border-dashed border-line text-center"
      style={{ height }}
    >
      <p className="text-sm text-ink-soft">{message}</p>
      <p className="max-w-xs text-xs text-ink-muted">{hint}</p>
    </div>
  )
}
