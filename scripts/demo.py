"""本机演示启动器：一条命令把环境查完、库迁好、token 发好、服务起来。

用法::

    uv run python scripts/demo.py              # 默认 8000 端口
    uv run python scripts/demo.py --port 8080  # 换端口
    uv run python scripts/demo.py --no-auth    # 演示时不想填 token

它做的事按「坏了先喊、能修就修」排：

| 检查 | 缺失时 |
| --- | --- |
| `.env` 里的两个 key | 警告，仍启动（抽取会失败，界面能看） |
| 数据库表 | **自动**跑 `alembic upgrade head` |
| 项目一索引 | 警告并给出建索引命令（检索会报 RAG_UNAVAILABLE） |
| 前端产物 | 警告并给出构建命令（只提供 API） |
| 访问 token | 自动签发一个 12 小时有效的
"""

from __future__ import annotations

import argparse
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.auth import SECRET_MIN_LENGTH, issue_token  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.store import get_database  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST = ROOT / "frontend" / "dist"
RAG_INDEX = ROOT.parent / "Contract-qa-RAG" / "data" / "processed" / "bm25.json.gz"


def check(label: str, ok: bool, hint: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}" + ("" if ok else f"  → {hint}"))
    return ok


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def ensure_schema() -> bool:
    """库没表就跑迁移。只读检查一次，避免每次启动都白跑 alembic。"""
    try:
        with get_database().session() as session:
            session.exec(text("SELECT 1 FROM task LIMIT 1"))
        return True
    except Exception:
        print("  数据库未迁移，执行 alembic upgrade head …")
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        return True


def reset_database(database_url: str) -> None:
    """删掉本地 SQLite 文件重来。只对 sqlite 生效，别的库碰都不碰。"""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        print(f"  非 SQLite 库，跳过重置：{database_url}")
        return
    path = Path(database_url.removeprefix(prefix))
    if not path.is_absolute():
        path = ROOT / path
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)
    print(f"  已清空演示库：{path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="本机演示启动器")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-auth", action="store_true", help="关闭鉴权（仅演示用）")
    parser.add_argument("--ttl-hours", type=int, default=12, help="token 有效期")
    parser.add_argument("--fresh", action="store_true", help="清空本地演示库后重新开始")
    args = parser.parse_args()

    settings = get_settings()
    if args.no_auth:
        # 环境变量优先于 .env，这里直接改进程内配置
        object.__setattr__(settings, "api_token_secret", "")
        object.__setattr__(settings, "api_token", "")

    print("\n合同录入执行 Agent · 本机演示\n" + "─" * 46)
    print("环境自检：")

    if args.fresh:
        reset_database(settings.database_url)

    check("DEEPSEEK_API_KEY 已配置", bool(settings.deepseek_api_key), "在 .env 里填 LLM 密钥")
    check(
        "EMBEDDING 密钥已配置",
        bool(settings.siliconflow_api_key),
        "在 .env 里填 EMBEDDING 密钥",
    )
    ensure_schema()
    check("数据库表就绪", True)
    check(
        "项目一索引已构建",
        RAG_INDEX.exists(),
        "cd ../Contract-qa-RAG && uv run contract-qa index",
    )
    has_ui = FRONTEND_DIST.exists()
    check("前端产物已构建", has_ui, "cd frontend && npm install && npm run build")

    if not port_free(args.port):
        print(f"\n端口 {args.port} 已被占用，换一个：--port {args.port + 1}")
        return 1

    token = None
    if settings.auth_enabled:
        if len(settings.api_token_secret) < SECRET_MIN_LENGTH:
            print(f"\nAPI_TOKEN_SECRET 至少 {SECRET_MIN_LENGTH} 位，否则无法签发 token")
            return 1
        token = issue_token(
            "demo", secret=settings.api_token_secret, ttl_seconds=args.ttl_hours * 3600
        )

    print("\n" + "─" * 46)
    print(f"地址：http://127.0.0.1:{args.port}/" + ("" if has_ui else "  （未构建前端，只有 API）"))
    print(f"文档：http://127.0.0.1:{args.port}/docs")
    print(f"指标：http://127.0.0.1:{args.port}/metrics")
    if token:
        expires = (datetime.now() + timedelta(hours=args.ttl_hours)).strftime("%m-%d %H:%M")
        print(f"\n访问凭证（{expires} 过期，粘到页面右上角）：\n{token}")
    else:
        print("\n鉴权已关闭（--no-auth），页面直接可用")
    print("\n按 Ctrl+C 停止\n" + "─" * 46 + "\n")

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=args.port,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
