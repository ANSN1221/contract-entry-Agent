// 与后端契约对齐的类型。字段名保持后端原样，避免两套命名来回翻译。

export type TaskStatus =
  | 'pending'
  | 'running'
  | 'await_confirm'
  | 'done'
  | 'blocked'
  | 'failed'

export interface Task {
  id: string
  status: TaskStatus
  version: number
  original_filename: string
  file_sha256: string
  file_path: string
  round_count: number
  attempt: number
  error_code: string | null
  blocked_reason: string | null
  created_at: string
  updated_at: string
}

export interface Transition {
  id: number
  from_status: string
  to_status: string
  reason: string | null
  actor: string
  request_id: string | null
  created_at: string
}

export interface TaskField {
  field_key: string
  value: string | null
  confidence: number
  needs_review: boolean
  source_clause_id: string | null
  evidence: string | null
  verified: boolean | null
  verify_note: string | null
}

export interface TaskDetail extends Task {
  transitions: Transition[]
  fields: TaskField[]
  confirm_token: string | null
}

export interface TaskList {
  items: Task[]
  total: number
  offset: number
  limit: number
}

export interface RunOutcome {
  task_id: string
  status: string
  outcome: string
  rounds: number
  error: string
  tokens: Record<string, number>
  review_keys: string[]
  unsupported: string[]
  confirm_token: string | null
}

export interface TaskEvent {
  id: number
  event: string
  data: {
    task_id: string
    type: string
    payload: Record<string, unknown>
    created_at: string
  }
}

// 12 个字段的中文名。放前端而不是复用后端，是为了前端能独立改文案。
export const FIELD_LABELS: Record<string, string> = {
  contract_no: '合同编号',
  party_a: '甲方名称',
  party_b: '乙方名称',
  total_amount: '合同总价',
  signed_date: '签订日期',
  effective_date: '生效日期',
  performance_period: '履行期限',
  payment_terms: '付款方式',
  breach_liability: '违约责任',
  dispute_resolution: '争议解决方式',
  deposit_ratio: '履约保证金比例',
  warranty_period: '质保期',
}

export const STATUS_LABELS: Record<TaskStatus, string> = {
  pending: '待处理',
  running: '执行中',
  await_confirm: '待确认',
  done: '已完成',
  blocked: '已阻塞',
  failed: '已失败',
}
