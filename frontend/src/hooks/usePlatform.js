import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { io } from 'socket.io-client'

import { api } from '../lib/api'

/**
 * Single source of truth for dashboard state.
 *
 * The backend pushes over a websocket and also serves a full snapshot on
 * connect, so a dashboard opened mid-rollout is populated immediately rather
 * than blank until the next event happens to fire. Telemetry is polled on a
 * slow interval because it is a read model over a file the proxy writes
 * continuously — pushing every proxied request would be far noisier than it is
 * useful.
 */
export function usePlatform() {
  const [connected, setConnected] = useState(false)
  const [config, setConfig] = useState(null)
  const [status, setStatus] = useState({ stages: [], building: false, trafficPct: 0, run: null })
  const [consoleLines, setConsoleLines] = useState([])
  const [history, setHistory] = useState({ runs: [], stats: {} })
  const [telemetry, setTelemetry] = useState(null)
  const [risk, setRisk] = useState(null)
  const [chaos, setChaos] = useState({ enabled: false, available: false })
  const [error, setError] = useState(null)

  const socketRef = useRef(null)

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await api.history())
    } catch (cause) {
      setError(cause.message)
    }
  }, [])

  const refreshTelemetry = useCallback(async () => {
    try {
      setTelemetry(await api.telemetry())
    } catch {
      /* Telemetry is best-effort; a gap must not blank the whole dashboard. */
    }
  }, [])

  useEffect(() => {
    const socket = io(api.baseUrl, { transports: ['websocket', 'polling'] })
    socketRef.current = socket

    socket.on('connect', () => {
      setConnected(true)
      setError(null)
      refreshHistory()
      api.chaos().then(setChaos).catch(() => {})
    })
    socket.on('disconnect', () => setConnected(false))
    socket.on('connect_error', () => setConnected(false))

    // Full state on connect, so a late-joining dashboard is never empty.
    socket.on('snapshot', (payload) => {
      if (payload.status) {
        setStatus(payload.status)
        // A dashboard opened after a run finished still gets its verdict.
        if (payload.status.risk) setRisk(payload.status.risk)
      }
      if (payload.console) setConsoleLines(payload.console)
      if (payload.config) setConfig(payload.config)
    })

    socket.on('log', (payload) => {
      setConsoleLines((lines) => [...lines, payload.line].slice(-500))
    })

    socket.on('stage', (stage) => {
      setStatus((current) => ({
        ...current,
        stages: current.stages.map((item) => (item.key === stage.key ? stage : item)),
      }))
    })

    socket.on('run', (run) => {
      setStatus((current) => ({ ...current, run }))
      refreshHistory()
    })

    socket.on('traffic', ({ weight }) => {
      setStatus((current) => ({ ...current, trafficPct: weight }))
    })

    socket.on('risk', setRisk)
    socket.on('chaos', ({ enabled }) => setChaos((c) => ({ ...c, enabled })))

    return () => {
      socket.removeAllListeners()
      socket.disconnect()
    }
  }, [refreshHistory])

  // Bootstrap over REST too, so the dashboard renders even if the websocket
  // handshake is blocked by a proxy the operator is behind.
  useEffect(() => {
    let cancelled = false
    Promise.all([api.config(), api.status(), api.console()])
      .then(([configPayload, statusPayload, consolePayload]) => {
        if (cancelled) return
        setConfig(configPayload)
        setStatus(statusPayload)
        if (statusPayload.risk) setRisk(statusPayload.risk)
        setConsoleLines(consolePayload.lines ?? [])
      })
      .catch((cause) => !cancelled && setError(cause.message))
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    // Deferred rather than called inline: a synchronous setState inside an
    // effect body triggers a cascading render on every mount.
    const initial = setTimeout(refreshTelemetry, 0)
    const interval = setInterval(refreshTelemetry, 2000)
    return () => {
      clearTimeout(initial)
      clearInterval(interval)
    }
  }, [refreshTelemetry])

  const deploy = useCallback(async (repoUrl, launch) => {
    setError(null)
    setRisk(null)
    setConsoleLines([])
    try {
      await api.deploy(repoUrl, launch)
    } catch (cause) {
      setError(cause.message)
      throw cause
    }
  }, [])

  const rollback = useCallback(async (reason) => {
    setError(null)
    try {
      await api.rollback(reason)
    } catch (cause) {
      setError(cause.message)
      throw cause
    }
  }, [])

  const toggleChaos = useCallback(async (enabled) => {
    try {
      setChaos(await api.setChaos(enabled).then((r) => ({ ...chaos, ...r })))
    } catch (cause) {
      setError(cause.message)
    }
  }, [chaos])

  const stages = useMemo(() => {
    if (status.stages?.length) return status.stages
    // Before the first run the backend has not sent live stages yet, so render
    // the declared catalogue as pending. The stage list always comes from the
    // backend -- never a hardcoded copy that can drift from what it runs.
    return (config?.stages ?? []).map((stage) => ({
      key: stage.key,
      title: stage.title,
      description: stage.description,
      trafficPct: stage.traffic,
      status: 'PENDING',
    }))
  }, [status.stages, config])

  return {
    connected,
    config,
    status,
    stages,
    consoleLines,
    history,
    telemetry,
    risk,
    chaos,
    error,
    clearError: () => setError(null),
    deploy,
    rollback,
    toggleChaos,
  }
}

function readStoredTheme() {
  try {
    return localStorage.getItem('bellwether-theme') ?? 'system'
  } catch {
    return 'system'
  }
}

/** Persisted light/dark preference, defaulting to the operating system. */
export function useTheme() {
  const [theme, setTheme] = useState(readStoredTheme)

  useEffect(() => {
    const root = document.documentElement
    if (theme === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', theme)
    try {
      localStorage.setItem('bellwether-theme', theme)
    } catch {
      /* Storage can be unavailable (private mode, blocked site data). The
         theme still applies for this session. */
    }
  }, [theme])

  return [theme, setTheme]
}
