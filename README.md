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
├── adapters/
│   └── rag.py         # contract-qa 适配层（双路召回 + 引用校验的边界）
└── api/
    └── v1/
        ├── __init__.py  # v1 路由汇总
        └── health.py    # /healthz /readyz
docs/                  # 文件名中英对照等说明文档
tests/                 # 状态机单测 / API 契约 / 并发幂等（随排期补）
```

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
| 11-19 ~ 11-20 | 数据模型 + 迁移 + 状态机 + 单测 | 待做 |
| 11-23 ~ 11-24 | 仓储层 + 任务 CRUD API + 集成测试 | 待做 |
| 后续 | 见项目计划书第八章 | — |

已完成：`uv` 工程装配、路径依赖接入、应用工厂、统一错误体、request_id 中间件、限流、结构化日志、`/healthz` `/readyz`、测试。

## 设计约定

- **状态机**：`pending → running → await_confirm → done`，`running` 异常转 `blocked` / `failed`，`await_confirm` 超时转 `blocked`；终态是吸收态，再操作返回 409。迁移走白名单表 `ALLOWED_TRANSITIONS`，每次迁移落一条 `TaskTransition` 审计记录。
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
