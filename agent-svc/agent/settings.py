"""Centralized settings for agent-svc."""

import functools
import os

from pydantic import BaseModel, Field


class AgentSettings(BaseModel):
    """All env-var-driven configuration for agent-svc."""

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # 单容器(aio)内嵌组件默认绑定 127.0.0.1; 多镜像部署由 compose 显式注入真实地址
    valkey_host: str = Field(default="127.0.0.1", alias="VALKEY_HOST")
    valkey_port: int = Field(default=6379, alias="VALKEY_PORT")
    valkey_db: int = Field(default=0, alias="VALKEY_DB")
    scraper_url: str = Field(default="http://127.0.0.1:8001", alias="SCRAPER_URL")
    searxng_url: str = Field(default="http://127.0.0.1:8888", alias="SEARXNG_URL")
    semantic_url: str = Field(default="http://127.0.0.1:8003", alias="SEMANTIC_URL")
    llm_base_url: str = Field(default="http://127.0.0.1:8011/v1", alias="LLM_BASE_URL")
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_model: str = Field(default="deepseek-v4-flash", alias="LLM_MODEL")
    llm_enable_thinking: bool = Field(default=False, alias="LLM_ENABLE_THINKING")
    llm_llama_cpp_disable_thinking: bool = Field(
        default=False, alias="LLM_LLAMA_CPP_DISABLE_THINKING"
    )
    # Per-operation idle bound (seconds) applied to LLM API calls: an httpx
    # scalar timeout bounds connect/read/write inactivity individually, with
    # no whole-request deadline, so streaming responses may legally run
    # longer than this as long as tokens keep arriving. Raise it (e.g. 300)
    # when pointing LLM_MODEL at reasoning models whose long thinking
    # pauses exceed the default.
    llm_call_timeout: float = Field(default=120.0, alias="LLM_CALL_TIMEOUT", gt=0)
    api_key: str = Field(default="", alias="API_KEY")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")
    max_searches_per_request: int = Field(
        default=5, alias="AGENT_MAX_SEARCHES_PER_REQUEST"
    )
    search_rate_limit: str = Field(default="10/60s", alias="AGENT_SEARCH_RATE_LIMIT")
    # Job-time retry policy for downstream 429 RATE_LIMITED conditions
    # (ADR-0053). Maximum total attempts for the blocked operation;
    # the fallback delay is used when the downstream response carries no
    # retry metadata, and server-provided delays are clamped to
    # ``job_retry_max_wait_seconds``.
    job_retry_max_attempts: int = Field(default=3, alias="JOB_RETRY_MAX_ATTEMPTS", ge=1)
    job_retry_fallback_seconds: float = Field(
        default=1.0, alias="JOB_RETRY_FALLBACK_SECONDS", ge=0
    )
    job_retry_max_wait_seconds: float = Field(
        default=60.0, alias="JOB_RETRY_MAX_WAIT_SECONDS", ge=0
    )
    crawl_max_duration_seconds: int = Field(
        default=1800, alias="CRAWL_MAX_DURATION_SECONDS"
    )
    crawl_idle_timeout_seconds: int = Field(
        default=300, alias="CRAWL_IDLE_TIMEOUT_SECONDS"
    )
    research_memory_ttl: int = Field(default=604800, alias="RESEARCH_MEMORY_TTL")
    research_memory_max_artifact_bytes: int = Field(
        default=5_242_880, alias="RESEARCH_MEMORY_MAX_ARTIFACT_BYTES"
    )
    # Global weighted admission budgets (ADR-0051). Values are weighted
    # units; per-operation weights are fetch=1, llm=4, browser=8.
    admission_light_fetch_limit: int = Field(
        default=64, alias="ADMISSION_LIGHT_FETCH_LIMIT"
    )
    admission_browser_limit: int = Field(default=32, alias="ADMISSION_BROWSER_LIMIT")
    admission_llm_limit: int = Field(default=32, alias="ADMISSION_LLM_LIMIT")
    # 浏览器动作(navigate/click/wait...)默认超时(毫秒)。旧默认 10000 对慢站
    # (TTFB 高、阻塞脚本指向被墙域)过于激进; 30000 与 Playwright 自身默认一致。
    # 慢站场景可在 .env 调大, 例如 BROWSER_NAVIGATE_TIMEOUT_MS=60000。
    browser_navigate_timeout_ms: int = Field(
        default=30000, alias="BROWSER_NAVIGATE_TIMEOUT_MS", ge=1000
    )


@functools.cache
def load_settings() -> AgentSettings:
    return AgentSettings.model_validate(dict(os.environ))
