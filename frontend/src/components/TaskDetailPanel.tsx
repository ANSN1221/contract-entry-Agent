import { useEffect, useMemo, useState } from 'react'
import type { TaskDetail, TaskField } from '../types'
import { FIELD_LABELS, STATUS_LABELS } from '../types'

interface EventLine {
  id: number
  type: string
  payload: Record<string, unknown>
  at: string
}

interface Props {
  task: TaskDetail
  events: EventLine[]
  busy: boolean
  onRun: () => void
  onConfirm: (
    token: string,
    fields: Array<{ field_key: string; value: string | null }>,
  ) => void
}

const EVENT_LABELS: Record<string, string> = {
  'task.queued': '已入队',
  'task.running': '开始执行',
  'retrieval.done': '条款召回完成',
  'extraction.done': '字段抽取完成',
  'verification.done': '证据校验完成',
  'extraction.retry': '触发重抽',
  'task.await_confirm': '等待人工确认',
  'task.confirmed': '人工已确认',
  'task.done': '已写库完成',
  'task.failed': '执行失败',
}

export function TaskDetailPanel({ task, events, busy, onRun, onConfirm }: Props) {
  const [draft, setDraft] = useState<Record<string, string>>({})

  useEffect(() => {
    // 切任务时重置编辑态，避免把上一个任务的修改带过来
    const next: Record<string, string> = {}
    for (const field of task.fields) {
      next[field.field_key] = field.value ?? ''
    }
    setDraft(next)
  }, [task.id, task.fields])

  const pending = useMemo(
    () => task.fields.filter((field) => field.needs_review || field.verified === false),
    [task.fields],
  )

  const runnable = ['pending', 'await_confirm'].includes(task.status)
  const terminal = ['done', 'failed', 'blocked'].includes(task.status)
  const canConfirm = task.status === 'await_confirm' && Boolean(task.confirm_token)

  function submitConfirm() {
    if (!task.confirm_token) return
    const changes = task.fields
      .filter((field) => (draft[field.field_key] ?? '') !== (field.value ?? ''))
      .map((field) => ({
        field_key: field.field_key,
        value: draft[field.field_key] ? draft[field.field_key] : null,
      }))
    onConfirm(task.confirm_token, changes)
  }

  return (
    <div className="detail">
      <header className="detail-head">
        <div>
          <h2>{task.original_filename}</h2>
          <p className="muted">
            {shortId(task.id)} · 版本 v{task.version} · 轮次 {task.round_count} · 尝试 {task.attempt}
          </p>
        </div>
        <div className="detail-actions">
          <span className={`badge status-${task.status}`}>
            {STATUS_LABELS[task.status] ?? task.status}
          </span>
          <button className="primary" disabled={busy || !runnable} onClick={onRun}>
            {task.status === 'await_confirm' ? '重新抽取' : '开始抽取'}
          </button>
        </div>
      </header>

      {task.error_code && (
        <div className="notice error">
          错误码 {task.error_code}：{task.blocked_reason ?? ''}
        </div>
      )}

      <section>
        <div className="section-head">
          <h3>字段</h3>
          <span className="muted">
            待确认 {pending.length} / 共 {task.fields.length}
          </span>
        </div>
        <table className="fields">
          <thead>
            <tr>
              <th>字段</th>
              <th>值</th>
              <th>置信度</th>
              <th>证据</th>
            </tr>
          </thead>
          <tbody>
            {task.fields.map((field) => (
              <FieldRow
                key={field.field_key}
                field={field}
                value={draft[field.field_key] ?? ''}
                editable={canConfirm && !busy}
                onChange={(value) => setDraft({ ...draft, [field.field_key]: value })}
              />
            ))}
            {task.fields.length === 0 && (
              <tr>
                <td colSpan={4} className="empty">
                  还没有抽取结果{terminal ? '' : '，点右上角开始抽取'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {canConfirm && (
        <div className="confirm-card">
          <div>
            <strong>确认卡</strong>
            <p className="muted">
              标红的字段没有通过证据校验或置信度不足，请核对后确认；确认后立即写库。
            </p>
          </div>
          <button className="primary" disabled={busy} onClick={submitConfirm}>
            确认并写库
          </button>
        </div>
      )}

      <section>
        <div className="section-head">
          <h3>事件时间线</h3>
          <span className="muted">{events.length ? `${events.length} 条` : '等待事件…'}</span>
        </div>
        <ol className="timeline">
          {events.map((event) => (
            <li key={event.id}>
              <span className="dot" />
              <div>
                <div>
                  <strong>{EVENT_LABELS[event.type] ?? event.type}</strong>
                  <span className="muted"> #{event.id} · {event.at}</span>
                </div>
                {Object.keys(event.payload).length > 0 && (
                  <code className="payload">{JSON.stringify(event.payload)}</code>
                )}
              </div>
            </li>
          ))}
          {events.length === 0 && <li className="empty">任务执行后会在这里实时推送</li>}
        </ol>
      </section>
    </div>
  )
}

function FieldRow({
  field,
  value,
  editable,
  onChange,
}: {
  field: TaskField
  value: string
  editable: boolean
  onChange: (value: string) => void
}) {
  const flag = field.needs_review || field.verified === false
  return (
    <tr className={flag ? 'row-flag' : undefined}>
      <td>
        <div>{FIELD_LABELS[field.field_key] ?? field.field_key}</div>
        <code className="muted small">{field.field_key}</code>
      </td>
      <td>
        {editable ? (
          <textarea value={value} rows={2} onChange={(event) => onChange(event.target.value)} />
        ) : (
          <span className={field.value ? '' : 'muted'}>{field.value ?? '—'}</span>
        )}
      </td>
      <td>
        <span className={field.confidence >= 0.7 ? 'conf ok' : 'conf low'}>
          {field.confidence.toFixed(2)}
        </span>
      </td>
      <td>
        <div className="muted small">{field.source_clause_id ?? '无出处'}</div>
        {field.verify_note && <div className="muted small">{field.verify_note}</div>}
        {field.evidence && <div className="evidence">{field.evidence}</div>}
      </td>
    </tr>
  )
}

function shortId(id: string): string {
  return id.slice(0, 8)
}
