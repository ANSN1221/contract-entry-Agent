"""环境配置聚合：所有外部依赖的开关都在这里，业务代码不直接读 os.environ。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "contract-entry-agent"
    version: str = "0.1.0"
    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    # 数据层：SQLite 起步，D14 换 PostgreSQL 只改连接串
    database_url: str = "sqlite:///./data/app.db"

    # LLM：DeepSeek 负责抽取与生成
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # Embedding：硅基流动，OpenAI 兼容协议
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"

    # 鉴权与限流：api_token 为空表示 dev 不校验；上线必须填
    api_token: str = ""
    rate_limit_per_minute: int = 120

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_token)

    @property
    def rate_limit_enabled(self) -> bool:
        return self.rate_limit_per_minute > 0 and self.env != "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试要换配置时用 get_settings.cache_clear() 或直接构造 Settings。"""
    return Settings()
