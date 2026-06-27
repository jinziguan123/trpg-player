const BASE = '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
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
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok || !res.body) throw new Error(`SSE error: ${res.status}`)
  yield* parseSSEStream(res)
}

export async function* connectSSE(path: string) {
  const res = await fetch(`${BASE}${path}`)
  if (res.status === 204 || !res.body) return
  if (!res.ok) throw new Error(`SSE error: ${res.status}`)
  yield* parseSSEStream(res)
}
