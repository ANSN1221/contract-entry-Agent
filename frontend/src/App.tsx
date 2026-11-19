import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError, getToken, setToken, subscribeEvents } from './api'
import type { RunOutcome, Task, TaskDetail } from './types'
import { STATUS_LABELS } from './types'
import { TaskDetailPanel } from './components/TaskDetailPanel'
import { NewTaskForm } from './components/NewTaskForm'

interface EventLine {
  id: number
  type: string
  payload: Record<string, unknown>
  at: string
}

const STATUS_FILTERS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待处理' },
  { value: 'running', label: '执行中' },
  { value: 'await_confirm', label: '待确认' },
  { value: 'done', label: '已完成' },
  { value: 'failed', label: '已失败' },
]

export default function App() {
  const [token, setTokenInput] = useState(getToken())
  const [tasks, setTasks] = useState<Task[]>([])
  const [statusFilter, setStatusFilter] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [events, setEvents] = useState<EventLine[]>([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'info' | 'error'; text: string } | null>(null)

  const reload = useCallback(async () => {
    try {
      const list = await api.listTasks({ limit: 50, status: statusFilter || undefined })
      setTasks(list.items)
      setSelectedId((current) => current ?? list.items[0]?.id ?? null)
    } catch (error) {
      setNotice({ kind: 'error', text: describe(error) })
    }
  }, [statusFilter])

  const loadDetail = useCallback(async (taskId: string) => {
    try {
      setDetail(await api.getTask(taskId))
    } catch (error) {
      setNotice({ kind: 'error', text: describe(error) })
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    void loadDetail(selectedId)
    setEvents([])
    const stop = subscribeEvents(
      selectedId,
      (event) => {
        setEvents((prev) =>
          prev.some((item) => item.id === event.id)
            ? prev
            : [
                ...prev,
                {
                  id: event.id,
                  type: event.type,
                  payload: event.payload,
                  at: new Date().toLocaleTimeString('zh-CN'),
                },
              ],
        )
        if (['task.await_confirm', 'task.done', 'task.failed', 'task.confirmed'].includes(event.type)) {
          void loadDetail(selectedId)
          void reload()
        }
      },
      () => undefined,
    )
    return stop
  }, [selectedId, loadDetail, reload])

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === selectedId) ?? null,
    [tasks, selectedId],
  )

  async function handleSaveToken() {
    setToken(token)
    setNotice({ kind: 'info', text: '凭证已保存到本地' })
    await reload()
  }

  async function handleCreate(payload: {
    original_filename: string
    file_sha256: string
    file_path: string
  }) {
    setBusy(true)
    try {
      const created = (await api.createTask(payload)) as Task
      await reload()
      setSelectedId(created.id)
      setNotice({ kind: 'info', text: '任务已创建，点「开始抽取」运行' })
    } catch (error) {
      setNotice({ kind: 'error', text: describe(error) })
    } finally {
      setBusy(false)
    }
  }

  async function handleRun(taskId: string) {
    setBusy(true)
    setNotice({ kind: 'info', text: '抽取中，模型通常需要 5~15 秒…' })
    try {
      const outcome: RunOutcome = await api.runTask(taskId)
      setNotice({
        kind: 'info',
        text: `执行完成：${outcome.status}，轮次 ${outcome.rounds}，token ${
          Object.values(outcome.tokens).reduce((a, b) => a + b, 0)
        }`,
      })
      await loadDetail(taskId)
      await reload()
    } catch (error) {
      setNotice({ kind: 'error', text: describe(error) })
    } finally {
      setBusy(false)
    }
  }

  async function handleConfirm(
    taskId: string,
    token: string,
    fields: Array<{ field_key: string; value: string | null }>,
  ) {
    setBusy(true)
    try {
      await api.confirm(taskId, { confirm_token: token, fields })
      setNotice({ kind: 'info', text: '已确认并写库' })
      await loadDetail(taskId)
      await reload()
    } catch (error) {
      setNotice({ kind: 'error', text: describe(error) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="layout">
      <header className="topbar">
        <div>
          <h1>合同录入执行 Agent</h1>
          <p className="muted">上传 → 抽取 → 证据校验 → 人工确认 → 写库</p>
        </div>
        <div className="token-box">
          <input
            aria-label="访问凭证"
            placeholder="Bearer token（留空表示开发环境免鉴权）"
            value={token}
            onChange={(event) => setTokenInput(event.target.value)}
          />
          <button onClick={handleSaveToken}>保存凭证</button>
        </div>
      </header>

      {notice && (
        <div className={`notice ${notice.kind}`} role="status">
          {notice.text}
          <button className="link" onClick={() => setNotice(null)}>
            关闭
          </button>
        </div>
      )}

      <main className="columns">
        <aside className="panel list-panel">
          <NewTaskForm disabled={busy} onSubmit={handleCreate} />

          <div className="filters">
            {STATUS_FILTERS.map((item) => (
              <button
                key={item.value || 'all'}
                className={statusFilter === item.value ? 'chip active' : 'chip'}
                onClick={() => setStatusFilter(item.value)}
              >
                {item.label}
              </button>
            ))}
          </div>

          <ul className="task-list">
            {tasks.map((task) => (
              <li
                key={task.id}
                className={task.id === selectedId ? 'task-row selected' : 'task-row'}
                onClick={() => setSelectedId(task.id)}
              >
                <div className="task-title">{task.original_filename}</div>
                <div className="task-meta">
                  <span className={`badge status-${task.status}`}>
                    {STATUS_LABELS[task.status] ?? task.status}
                  </span>
                  <span className="muted">{shortId(task.id)}</span>
                  <span className="muted">{task.attempt > 0 ? `第 ${task.attempt} 次` : ''}</span>
                </div>
              </li>
            ))}
            {tasks.length === 0 && <li className="empty">还没有任务，用上面的表单建一个</li>}
          </ul>
        </aside>

        <section className="panel detail-panel">
          {detail && selectedTask ? (
            <TaskDetailPanel
              task={detail}
              events={events}
              busy={busy}
              onRun={() => handleRun(detail.id)}
              onConfirm={(tokenValue, fields) => handleConfirm(detail.id, tokenValue, fields)}
            />
          ) : (
            <p className="empty">左侧选择一个任务查看详情</p>
          )}
        </section>
      </main>
    </div>
  )
}

function shortId(id: string): string {
  return id.slice(0, 8)
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.code ?? error.status}：${error.message}`
  }
  return error instanceof Error ? error.message : String(error)
}
