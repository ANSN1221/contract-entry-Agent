// 接口层：所有请求集中在这里，组件不直接拼 URL。
// token 存 localStorage：这是内部工具，没有 XSS 面；真要上线应换 httpOnly cookie。

import type { RunOutcome, TaskDetail, TaskList } from './types'

const TOKEN_KEY = 'contract-agent-token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token.trim())
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message)
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const resp = await fetch(path, { ...init, headers })
  if (!resp.ok) {
    let code: string | undefined
    let message = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      code = body?.error?.code
      message = body?.error?.message ?? message
    } catch {
      /* 非 JSON 错误体，保留状态码 */
    }
    throw new ApiError(message, resp.status, code)
  }
  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

export const api = {
  listTasks(params: { offset?: number; limit?: number; status?: string } = {}) {
    const query = new URLSearchParams()
    if (params.offset) query.set('offset', String(params.offset))
    if (params.limit) query.set('limit', String(params.limit))
    if (params.status) query.set('status', params.status)
    const suffix = query.size ? `?${query.toString()}` : ''
    return request<TaskList>(`/api/v1/tasks${suffix}`)
  },

  getTask(taskId: string) {
    return request<TaskDetail>(`/api/v1/tasks/${taskId}`)
  },

  createTask(body: { original_filename: string; file_sha256: string; file_path: string }) {
    return request('/api/v1/tasks', { method: 'POST', body: JSON.stringify(body) })
  },

  runTask(taskId: string) {
    return request<RunOutcome>(`/api/v1/tasks/${taskId}/run?wait=true`, { method: 'POST' })
  },

  confirm(
    taskId: string,
    body: { confirm_token: string; fields: { field_key: string; value: string | null }[] },
  ) {
    return request(`/api/v1/tasks/${taskId}/confirm`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
}

/**
 * 订阅任务事件。EventSource 不能带请求头，所以 token 走查询串
 * （后端只对 /events 开了这个口子）。
 */
export function subscribeEvents(
  taskId: string,
  onEvent: (event: { id: number; type: string; payload: Record<string, unknown> }) => void,
  onEnd?: () => void,
): () => void {
  const token = getToken()
  const url = `/api/v1/tasks/${taskId}/events${token ? `?token=${encodeURIComponent(token)}` : ''}`
  const source = new EventSource(url)

  const known = [
    'task.queued',
    'task.running',
    'retrieval.done',
    'extraction.done',
    'verification.done',
    'extraction.retry',
    'task.await_confirm',
    'task.confirmed',
    'task.done',
    'task.failed',
  ]
  for (const type of known) {
    source.addEventListener(type, (raw) => {
      const message = raw as MessageEvent<string>
      try {
        const parsed = JSON.parse(message.data)
        onEvent({ id: Number(message.lastEventId), type, payload: parsed.payload ?? {} })
      } catch {
        /* 坏帧忽略，不要因为一条事件把流打断 */
      }
    })
  }
  source.onerror = () => {
    source.close()
    onEnd?.()
  }
  return () => source.close()
}
