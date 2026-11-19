"""签发短期访问 token。

用法::

    uv run python scripts/issue_token.py --subject dev --ttl 3600

密钥取自 `.env` 的 `API_TOKEN_SECRET`，不通过命令行传，免得留在 shell 历史里。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import SECRET_MIN_LENGTH, issue_token  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="签发短期 API token")
    parser.add_argument("--subject", default="dev", help="调用方标识，进日志用")
    parser.add_argument("--ttl", type=int, default=None, help="有效期（秒），默认取配置")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.api_token_secret:
        print(f"未配置 API_TOKEN_SECRET，先在 .env 里填一个不少于 {SECRET_MIN_LENGTH} 位的随机串：")
        print('  python -c "import secrets; print(secrets.token_urlsafe(32))"')
        return 1

    ttl = args.ttl if args.ttl is not None else settings.api_token_ttl_seconds
    token = issue_token(args.subject, secret=settings.api_token_secret, ttl_seconds=ttl)
    expires = datetime.fromtimestamp(datetime.now().timestamp() + ttl).strftime("%Y-%m-%d %H:%M:%S")

    print(token, flush=True)
    print(f"\n# subject={args.subject}  expires={expires}  ttl={ttl}s", file=sys.stderr)
    print(
        f'# curl -H "Authorization: Bearer {token}" http://127.0.0.1:8000/api/v1/tasks',
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
