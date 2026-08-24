import { StatusPill } from './primitives'

const THEMES = [
  { key: 'light', label: 'Light', icon: '☀' },
  { key: 'dark', label: 'Dark', icon: '☾' },
  { key: 'system', label: 'System', icon: '◐' },
]

export default function Header({ connected, config, theme, setTheme }) {
  return (
    <header className="mb-5 flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-baseline gap-3">
        <h1 className="text-lg font-semibold tracking-tight text-ink">Bellwether</h1>
        <p className="text-sm text-ink-muted">Risk-gated progressive delivery</p>
      </div>

      <div className="flex items-center gap-2">
        <StatusPill
          tone={connected ? 'good' : 'critical'}
          icon={connected ? '●' : '○'}
        >
          {connected ? 'Connected' : 'Disconnected'}
        </StatusPill>

        {config?.proxy?.url && (
          <span className="hidden font-mono text-xs text-ink-muted sm:inline">
            proxy {config.proxy.url.replace(/^https?:\/\//, '')}
          </span>
        )}

        <div
          className="flex items-center rounded-md border border-line p-0.5"
          role="group"
          aria-label="Colour theme"
        >
          {THEMES.map(({ key, label, icon }) => (
            <button
              key={key}
              type="button"
              onClick={() => setTheme(key)}
              aria-pressed={theme === key}
              title={label}
              className={`rounded px-2 py-1 text-xs transition-colors ${
                theme === key ? 'bg-raised text-ink' : 'text-ink-muted hover:text-ink-soft'
              }`}
            >
              <span aria-hidden="true">{icon}</span>
              <span className="sr-only">{label}</span>
            </button>
          ))}
        </div>
      </div>
    </header>
  )
}
