"""集中配置。

原项目在 ai_service.py 里用 `os.getenv("sk-aaaa")` 这种方式读配置，把
"值"当成了"变量名"，导致三个配置全部取到 None。这里统一改成 pydantic-settings，
所有敏感项都只能从环境变量 / .env 读取，源码里不出现任何真实 Key。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_name: str = "跨境商品管理 API"
    app_version: str = "2.0.0"
    debug: bool = False
    log_level: str = "INFO"

    # ---------- 数据库 ----------
    # 同步连接串保留给原有 CRUD / 批量任务使用，不动老代码
    database_url: str = "mysql+pymysql://root:root123@localhost:3306/product_db"
    # 异步连接串（新代码使用）。留空则自动从 database_url 推导
    async_database_url: str = ""

    # ---------- Redis ----------
    redis_url: str = "redis://localhost:6379/0"
    redis_enabled: bool = True
    redis_timeout: float = 1.0
    cache_ttl: int = 300

    # ---------- AI / LLM ----------
    ai_api_key: str = Field(default="", description="LLM API Key，禁止写死在代码里")
    ai_base_url: str = "https://api.deepseek.com/v1"
    ai_model: str = "deepseek-chat"
    ai_timeout: float = 30.0
    ai_max_retries: int = 3
    ai_temperature: float = 0.3
    ai_max_tokens: int = 800

    # auto   = 先尝试原生 tools，接口不支持时自动降级到 prompt 模式
    # native = 只走 OpenAI 兼容的 tools / tool_calls
    # prompt = 只走提示词约定 JSON 的兜底模式（任何 chat 模型都能用）
    ai_tool_mode: Literal["auto", "native", "prompt"] = "auto"
    ai_max_tool_rounds: int = 5

    # ---------- 会话 ----------
    chat_history_limit: int = 20

    @field_validator("ai_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def async_url(self) -> str:
        """把同步连接串转成异步连接串。"""
        if self.async_database_url:
            return self.async_database_url
        url = self.database_url
        if url.startswith("mysql+pymysql"):
            return url.replace("mysql+pymysql", "mysql+asyncmy", 1)
        if url.startswith("mysql://"):
            return url.replace("mysql://", "mysql+asyncmy://", 1)
        if url.startswith("sqlite://") and "+aiosqlite" not in url:
            return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return url

    @property
    def chat_completions_url(self) -> str:
        base = self.ai_base_url
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
