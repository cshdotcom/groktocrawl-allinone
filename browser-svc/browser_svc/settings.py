"""Centralized settings for browser-svc."""

import functools
import os

from pydantic import BaseModel, Field


class BrowserSettings(BaseModel):
    """All env-var-driven configuration for browser-svc."""

    # 默认 127.0.0.1(单容器内嵌 valkey); 旧多镜像默认 "valkey" 主机名在
    # 单容器内不可解析 → cookie 持久化静默禁用。完整 URL 场景用 VALKEY_URL。
    valkey_host: str = Field(default="127.0.0.1", alias="VALKEY_HOST")
    valkey_port: int = Field(default=6379, alias="VALKEY_PORT")
    # 浏览器动作(navigate/click/wait...)默认超时(毫秒)。旧默认 10000 对慢站
    # (TTFB 高、阻塞脚本指向被墙域)过于激进; 30000 与 Playwright 自身默认一致。
    # 慢站场景可在 .env 调大, 例如 BROWSER_NAVIGATE_TIMEOUT_MS=60000。
    navigate_timeout_ms: int = Field(
        default=30000, alias="BROWSER_NAVIGATE_TIMEOUT_MS", ge=1000
    )


@functools.cache
def load_settings() -> BrowserSettings:
    return BrowserSettings.model_validate(dict(os.environ))
