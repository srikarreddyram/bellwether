import { useEffect, useRef, useState } from 'react'

import { Card } from './primitives'

/** Streaming orchestrator output, with opt-out auto-scroll. */
export default function Console({ lines }) {
  const [follow, setFollow] = useState(true)
  const endRef = useRef(null)
  const boxRef = useRef(null)

  useEffect(() => {
    if (follow) endRef.current?.scrollIntoView({ block: 'end' })
  }, [lines, follow])

  // Scrolling up to read history should not fight the incoming stream.
  const onScroll = () => {
    const box = boxRef.current
    if (!box) return
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 32
    setFollow(atBottom)
  }

  return (
    <Card
      title="Orchestrator log"
      action={
        <label className="flex items-center gap-1.5 text-xs text-ink-muted">
          <input
            type="checkbox"
            checked={follow}
            onChange={(event) => setFollow(event.target.checked)}
            className="accent-[var(--series-stable)]"
          />
          Follow
        </label>
      }
    >
      <div
        ref={boxRef}
        onScroll={onScroll}
        className="h-64 overflow-auto rounded-md border border-line bg-plane p-3 font-mono text-xs leading-relaxed"
        role="log"
        aria-live="polite"
        aria-label="Orchestrator output"
      >
        {lines.length === 0 ? (
          <p className="text-ink-muted">Waiting for a rollout…</p>
        ) : (
          lines.map((line, index) => (
            <div key={index} className="whitespace-pre-wrap break-words" style={{ color: toneFor(line) }}>
              {line}
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
    </Card>
  )
}

function toneFor(line) {
  if (/\b(FAILED|ABORT|ERROR|refused)\b/.test(line)) return 'var(--status-critical)'
  if (/\b(WARNING|rolling back|abort requested)\b/i.test(line)) return 'var(--status-warning)'
  if (/\b(healthy|promoted|SUCCEEDED|passed)\b/i.test(line)) return 'var(--status-good)'
  return 'var(--ink-2)'
}
