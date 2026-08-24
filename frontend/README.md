# Bellwether dashboard

The control-plane UI: a React + Vite single-page app that talks to
`bellwether api` over REST and a WebSocket.

```bash
npm install
npm run dev      # http://localhost:5173
```

The API must be running (`bellwether api`) and must list this origin in
`BELLWETHER_CORS_ORIGINS`. The default allowlist already includes
`http://localhost:5173`; if you serve the dashboard from anywhere else, add that
origin or the browser will block every request.

| Script | Purpose |
|---|---|
| `npm run dev` | Dev server with hot reload |
| `npm run build` | Production bundle into `dist/` |
| `npm run preview` | Serve the built bundle |
| `npm run lint` | ESLint |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VITE_API_URL` | `http://127.0.0.1:5001` | Control-plane address |
| `VITE_API_TOKEN` | unset | Sent as `Authorization: Bearer` when the API requires a token |

## Notes for contributors

- **Stages come from the backend.** `/api/config` serves the stage catalogue and
  the UI renders whatever it declares. Do not hardcode a parallel stage list —
  that drift is exactly what the previous dashboard suffered from.
- **Colours are tokens, never literals.** Every colour resolves through a CSS
  custom property in `src/index.css`, so light and dark are defined in one
  place. The two cohort colours are a validated categorical pair; the status
  palette is separate and never reused for a series.
- **State is never colour alone.** Every status pill carries a glyph and a text
  label.
- **The latency chart is plain SVG.** Two series, one reference line and a
  crosshair do not justify a charting dependency.
