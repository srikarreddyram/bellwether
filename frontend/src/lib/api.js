/**
 * REST client for the Bellwether control plane.
 *
 * Every call goes through `request`, so error handling is uniform: the backend
 * returns `{ error: { code, message } }` on every failure path, and we surface
 * that message rather than an opaque "failed to fetch". The previous dashboard
 * called `fetch(...)` inline with no `.catch` on mutations, so a rejected
 * deployment looked identical to a successful one.
 */

const BASE = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:5001'
const TOKEN = import.meta.env.VITE_API_TOKEN ?? ''

export class ApiError extends Error {
  constructor(message, { status, code } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function request(path, { method = 'GET', body, signal } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`

  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (cause) {
    if (cause.name === 'AbortError') throw cause
    throw new ApiError('Cannot reach the Bellwether API. Is `bellwether api` running?')
  }

  const text = await response.text()
  const payload = text ? safeParse(text) : null

  if (!response.ok) {
    const detail = payload?.error
    throw new ApiError(detail?.message ?? `Request failed with status ${response.status}`, {
      status: response.status,
      code: detail?.code,
    })
  }
  return payload
}

function safeParse(text) {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

export const api = {
  baseUrl: BASE,
  config: () => request('/api/config'),
  status: () => request('/api/status'),
  history: (limit = 25) => request(`/api/history?limit=${limit}`),
  telemetry: () => request('/api/telemetry'),
  metrics: () => request('/api/metrics'),
  console: () => request('/api/console'),
  chaos: () => request('/api/chaos'),
  deploy: (repoUrl, launch) =>
    request('/api/deploy', { method: 'POST', body: { repoUrl, launch } }),
  rollback: (reason) => request('/api/rollback', { method: 'POST', body: { reason } }),
  setChaos: (enabled) => request('/api/chaos', { method: 'POST', body: { enabled } }),
}
