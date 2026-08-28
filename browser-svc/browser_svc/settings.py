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


@functools.cache
def load_settings() -> BrowserSettings:
    return BrowserSettings.model_validate(dict(os.environ))
