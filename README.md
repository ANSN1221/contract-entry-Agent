# 合同录入执行 Agent

上传合同 PDF → 自动抽取字段 → 低置信字段标红 → 人工确认 → 写库。

重点不在「模型能不能抽字段」，而在**把 Agent 编排放进一套工程化的后端底座**：任务调度、状态机、幂等、可观测、完整 API 契约。

- 仓库：<https://github.com/ANSN1221/contract-entry-Agent>
- 文件名看不懂：[docs/文件名对照.md](docs/文件名对照.md)
- 目标岗位：大模型应用开发（偏后端）
- 执行窗口：2026-11-18 — 2026-12-09，共 16 个工作日

---

## 快速开始

```bash
# 1. 同级目录先放项目一（检索与引用校验能力）
git clone https://github.com/ANSN1221/Contract-qa-RAG.git ../Contract-qa-RAG

# 2. 装依赖（含检索能力）
uv sync --extra dev --extra rag

# 3. 配环境变量
cp .env.example .env   # 填 DEEPSEEK_API_KEY / SILICONFLOW_API_KEY

# 4. 起服务
uv run uvicorn app.main:app --reload --port 8000

# 5. 验收
curl http://127.0.0.1:8000/healthz
# {"status":"ok","service":"contract-entry-agent","version":"0.1.0","env":"dev"}
```

文档：<http://127.0.0.1:8000/docs> · OpenAPI：<http://127.0.0.1:8000/openapi.json>

只想跑骨架、不接检索（无 `contract-qa` 时）：

```bash
uv sync --extra dev
```

## 常用命令

| 用途 | 命令 |
| --- | --- |
| 启动 | `uv run uvicorn app.main:app --reload` |
| 建库 / 升级到最新 | `uv run alembic upgrade head` |
| 改完模型生成迁移 | `uv run alembic revision --autogenerate -m "描述"` |
| 回退一步 | `uv run alembic downgrade -1` |
| 查看当前版本 | `uv run alembic current` |
| 测试 | `uv run pytest` |
| 静态检查 | `uv run ruff check .` |
| 格式化 | `uv run ruff format .` |

## 目录结构

```
app/
├── main.py            # 应用工厂：装配顺序 日志 → 错误体 → 中间件 → 路由
├── config.py          # env 聚合（pydantic-settings），业务代码不读 os.environ
├── errors.py          # 统一错误体 + 异常处理器
├── middleware.py      # request_id 贯穿 / 访问日志 / 滑窗限流
├── log.py             # structlog 配置
├── models.py          # Task / TaskTransition / TaskField，12 字段清单
├── store.py           # 引擎、会话、SQLite PRAGMA + get_session 请求级会话依赖
├── repository.py      # 任务仓储层：接口层不直接写 SQL，业务失败翻译成 AppError
├── schemas.py         # 接口契约：请求/响应 pydantic 模型，与数据库模型分开演化
├── state_machine.py   # 白名单迁移 + 乐观锁 + 审计
├── adapters/
│   └── rag.py         # contract-qa 适配层（双路召回 + 引用校验的边界）
└── api/
    └── v1/
        ├── __init__.py  # v1 路由汇总
        ├── health.py    # /healthz /readyz
        └── tasks.py     # 任务 CRUD + 状态迁移 + 审计时间线
migrations/            # Alembic 迁移脚本（versions/ 下按时间累积）
alembic.ini            # 迁移工具配置（连接串由 env.py 从 config 注入）
docs/                  # 文件名中英对照等说明文档
tests/                 # 状态机单测 / 模型约束 / API 契约（随排期补）
```

## 状态机

```
pending ──→ running ──→ await_confirm ──→ done
              │  │            │
              │  └────────────┴──→ blocked
              └──→ failed
```

| 迁移 | 触发场景 |
| --- | --- |
| `pending → running` | 调度器领取任务 |
| `running → await_confirm` | 抽取完成，存在低置信字段 |
| `running → done` | 无可疑字段，直接写库 |
| `running → blocked` | 缺关键信息，转人工 |
| `running → failed` | 重试用尽或不可恢复错误 |
| `await_confirm → done` | 人工确认后写库 |
| `await_confirm → running` | 用户要求重新抽取 |
| `await_confirm → blocked` | 确认超时 |

`done` / `blocked` / `failed` 是吸收态，没有出边；针对终态的任何操作返回 409。
每次迁移写一条 `TaskTransition`（谁、从哪到哪、原因、时间、request_id），形成审计链。
并发用 `Task.version` 做 CAS，抢不到的一方抛 `ConcurrentModification`，不覆盖别人的写入。

## 统一错误体

所有异常出口只有一种形状：

```json
{"error": {"code": "NOT_FOUND", "message": "资源不存在", "detail": null, "request_id": "a1b2c3d4e5f6"}}
```

- HTTP 语义化：400 / 401 / 403 / 404 / 409 / 429 / 500
- 校验失败统一 400 `VALIDATION_ERROR`（不用 FastAPI 默认 422，契约里只有一个参数不合法出口）
- `request_id` 同时回写在响应头 `X-Request-ID`，客户端可凭它反查整条日志链；请求自带该头且长度合法时原样透传

## 当前进度

| 日期 | 交付物 | 状态 |
| --- | --- | --- |
| 11-18 | 仓库骨架 + uv 装配 + 空 FastAPI + 统一错误体 | 已完成 |
| 11-19 ~ 11-20 | 数据模型 + 迁移 + 状态机 + 单测 | 已完成（提前） |
| 11-23 ~ 11-24 | 仓储层 + 任务 CRUD API + 集成测试 | 已完成（提前，11-19） |
| 11-25 ~ 11-27 | 工具与主链路（条款检索 / 证据校验 / 抽取提示词 / 编排） | 待做 |
| 后续 | 见项目计划书第八章 | — |

已完成：`uv` 工程装配、路径依赖接入、应用工厂、统一错误体、request_id 中间件、限流、结构化日志、`/healthz` `/readyz`、SQLModel 三张表、Alembic 初始迁移、状态机白名单 + 乐观锁 + 审计链、任务仓储层、任务 CRUD API（幂等创建 / 分页 / 状态筛选 / 状态迁移）、83 条测试。

## 任务 API

| 端点 | 说明 |
| --- | --- |
| `POST /api/v1/tasks` | 创建任务（JSON 元数据）；带 `Idempotency-Key` 头重复提交返回同一任务 |
| `GET /api/v1/tasks` | 分页列表：`offset`/`limit`（默认 0/20，上限 100）+ `status` 筛选，信封 `{items, total, offset, limit}` |
| `GET /api/v1/tasks/{id}` | 详情 + 状态迁移审计时间线 |
| `POST /api/v1/tasks/{id}/transitions` | 状态迁移：`{to_status, version, reason?, actor?}`；版本不符 409 `CONCURRENT_MODIFICATION`，非法迁移 409 `INVALID_TRANSITION` |

## 设计约定

- **状态机**：白名单表 `ALLOWED_TRANSITIONS` 是唯一判定源，非法迁移抛 `InvalidTransition`；迁移与审计写在同一事务，中途失败不留半条记录。
- **状态存字符串不存枚举**：跨 SQLite / PostgreSQL 更省事，合法性由状态机保证，数据库只负责存。
- **乐观锁**：`Task.version` 做 CAS（`WHERE version = ?`），并发改同一条任务时后到者失败而非覆盖。
- **幂等**：上传带 `Idempotency-Key`，重复返回同一 task；写库靠唯一约束；确认用一次性 `confirm_token`，二次提交 409。
- **状态与连接解耦**：后台 runner 持有任务状态，浏览器关闭不影响任务推进。
- **依赖隔离**：`contract_qa` 的内部数据结构不进入业务层，一律经 `app/adapters/` 转换成本项目类型。

## 与项目一的关系

[Contract-qa-RAG](https://github.com/ANSN1221/Contract-qa-RAG) 提供条款级检索（BM25 + 向量，RRF 融合）与引用校验，本项目通过 path 依赖接入：

```toml
[tool.uv.sources]
contract-qa-agent = { path = "../Contract-qa-RAG", editable = true }
```

发行名 `contract-qa-agent`，导入名 `contract_qa`。缺包时 `app/adapters/rag.py` 抛出带安装指引的 `RagUnavailable`，而不是 `ImportError`。

## 明确不做

OCR 全流程 · 多租户权限 · 移动端 · 分布式队列（Celery / Kafka）· 法律意见生成。
