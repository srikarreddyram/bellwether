/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      // Every colour resolves through a CSS custom property so light and dark
      // swap in exactly one place (src/index.css) rather than being duplicated
      // across every `dark:` variant in the markup.
      colors: {
        plane: 'var(--surface-0)',
        surface: 'var(--surface-1)',
        raised: 'var(--surface-2)',
        ink: {
          DEFAULT: 'var(--ink-1)',
          soft: 'var(--ink-2)',
          muted: 'var(--ink-3)',
        },
        line: 'var(--border)',
        grid: 'var(--grid)',
        stable: 'var(--series-stable)',
        canary: 'var(--series-canary)',
        good: 'var(--status-good)',
        warning: 'var(--status-warning)',
        serious: 'var(--status-serious)',
        critical: 'var(--status-critical)',
      },
      fontFamily: {
        sans: ['system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      borderRadius: { card: '10px' },
    },
  },
  plugins: [],
}
