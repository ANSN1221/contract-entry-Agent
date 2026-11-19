import { useState } from 'react'

interface Props {
  disabled: boolean
  onSubmit: (payload: {
    original_filename: string
    file_sha256: string
    file_path: string
  }) => void
}

// 默认值指向仓库里的合成样例，点一下就能跑通完整链路
const SAMPLE = {
  original_filename: '合同样例-软件服务采购.pdf',
  file_sha256: 'c'.repeat(64),
  file_path: 'data/samples/合同样例-软件服务采购.pdf',
}

export function NewTaskForm({ disabled, onSubmit }: Props) {
  const [form, setForm] = useState(SAMPLE)
  const [open, setOpen] = useState(false)

  return (
    <div className="new-task">
      <div className="new-task-head">
        <strong>新建任务</strong>
        <button className="link" onClick={() => setOpen((value) => !value)}>
          {open ? '收起' : '改参数'}
        </button>
      </div>

      {open && (
        <div className="form-grid">
          <label>
            文件名
            <input
              value={form.original_filename}
              onChange={(event) => setForm({ ...form, original_filename: event.target.value })}
            />
          </label>
          <label>
            文件路径
            <input
              value={form.file_path}
              onChange={(event) => setForm({ ...form, file_path: event.target.value })}
            />
          </label>
          <label>
            SHA-256
            <input
              value={form.file_sha256}
              onChange={(event) => setForm({ ...form, file_sha256: event.target.value })}
            />
          </label>
        </div>
      )}

      <button className="primary" disabled={disabled} onClick={() => onSubmit(form)}>
        {disabled ? '处理中…' : '创建示例任务'}
      </button>
    </div>
  )
}
