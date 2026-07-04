/** 主机地址：留空 = 本机后端（开发用 vite 代理 /api；打包客户端用本机 sidecar）；
 *  设值（如 http://192.168.1.5:8000）= 作为客人连到房主后端。 */
export function getServerUrl(): string {
  return localStorage.getItem('trpg_server_url') || ''
}

export function setServerUrl(url: string) {
  const clean = url.trim().replace(/\/+$/, '')
  if (clean) localStorage.setItem('trpg_server_url', clean)
  else localStorage.removeItem('trpg_server_url')
}

/** 当前 API 前缀：本机走同源 /api（vite 代理）；连主机时走绝对地址 <host>/api。 */
export function getApiBase(): string {
  const s = getServerUrl()
  return s ? `${s}/api` : '/api'
}

/** 轻量玩家身份：localStorage 生成并持久化 UUID，作为 X-Player-Token 带上。 */
export function getPlayerToken(): string {
  let t = localStorage.getItem('trpg_player_token')
  if (!t) {
    t = (crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`)
    localStorage.setItem('trpg_player_token', t)
  }
  return t
}

function authHeaders(extra?: HeadersInit): HeadersInit {
  return { 'X-Player-Token': getPlayerToken(), ...(extra || {}) }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${getApiBase()}${path}`, {
    ...init,
    headers: authHeaders({ 'Content-Type': 'application/json', ...(init?.headers || {}) }),
  })
  if (!res.ok) {
    const body = await res.text()
    let msg = body
    try {
      const json = JSON.parse(body)
      msg = json.detail || json.message || body
    } catch { /* use raw text */ }
    throw new Error(msg)
  }
  return res.json()
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body ? JSON.stringify(body) : undefined }),
  delete: <T = void>(path: string) => request<T>(path, { method: 'DELETE' }),
}

async function* parseSSEStream(res: Response) {
  if (!res.body) return

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const data = JSON.parse(line.slice(6))
      yield data
    }
  }
}

export async function* streamSSE(path: string, body?: unknown) {
  const res = await fetch(`${getApiBase()}${path}`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok || !res.body) throw new Error(`SSE error: ${res.status}`)
  yield* parseSSEStream(res)
}

export async function* connectSSE(path: string, signal?: AbortSignal) {
  const res = await fetch(`${getApiBase()}${path}`, { signal, headers: authHeaders() })
  if (res.status === 204 || !res.body) return
  if (!res.ok) throw new Error(`SSE error: ${res.status}`)
  yield* parseSSEStream(res)
}
