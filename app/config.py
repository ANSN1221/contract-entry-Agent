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
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_retry_base_delay: float = 0.5

    # Embedding：硅基流动，OpenAI 兼容协议
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"

    # 抽取与校验
    extract_confidence_threshold: float = 0.7  # 低于此值进确认卡标红
    verify_with_llm: bool = True  # 规则层没比对出来时，是否再让模型判一次

    # 后台执行器
    runner_workers: int = 2  # 0 表示不启动 worker（测试用）
    runner_timeout_seconds: float = 120.0  # 单任务执行上限
    checkpoint_path: str = "data/checkpoints.db"  # LangGraph 断点存储

    # 鉴权：三项都不为空才算开启。dev 留空放行，prod 留空直接拒绝（fail closed）
    api_token: str = ""  # 长期静态 token，开发/内网用
    api_token_secret: str = ""  # 签名密钥，配了才能签发短期 token
    api_token_ttl_seconds: int = 3600  # 短期 token 默认有效期
    rate_limit_per_minute: int = 120

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_token or self.api_token_secret)

    @property
    def rate_limit_enabled(self) -> bool:
        return self.rate_limit_per_minute > 0 and self.env != "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试要换配置时用 get_settings.cache_clear() 或直接构造 Settings。"""
    return Settings()
