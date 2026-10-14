from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_UPSTREAM_BASE = "https://lobsterai-server.youdao.com"


def duration(value: Any, default: float) -> float:
    """把纯数字或 `60s`/`10m`/`12h`/`1d` 解析为秒。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[value[-1]]
        except (ValueError, KeyError, IndexError):
            pass
    return default


class Settings(BaseSettings):
    """应用配置, 从 `.env` 文件或 `LB2A_*` 环境变量加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LB2A_",
        extra="ignore",
    )

    api_key: str = Field(default="", description="聊天接口的 Bearer API Key, 为空时不启用鉴权")
    login_portal: str = Field(
        default="https://lobsterai.youdao.com",
        description="LobsterAI 登录门户地址",
    )
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        description="Redis 连接地址, auths 与 data 均写入 Redis",
    )
    redis_prefix: str = Field(default="lb2a", description="Redis 键前缀")
    upstream_base: str = Field(default=DEFAULT_UPSTREAM_BASE, description="LobsterAI 上游 API 地址")
    tool_model: str = Field(default="", description="Responses 工具调用使用的上游模型, 为空时沿用普通模型映射")
    timeout_seconds: float = Field(default=180, gt=0, description="上游 API 读取超时时间, 单位为秒")
    max_account_retries: int = Field(default=3, ge=1, description="单次聊天请求最多尝试的账号数")
    hard_credit: float = Field(default=43200, gt=0, description="余额不足后的冷却时间, 支持 12h 等格式")
    soft_rate: float = Field(default=60, gt=0, description="限流或资源不可用后的冷却时间, 支持 60s 等格式")
    err_threshold: int = Field(default=3, ge=1, description="连续错误达到多少次后冷却账号")
    err_cooldown: float = Field(default=600, gt=0, description="连续错误后的冷却时间, 支持 10m 等格式")
    checkin_hours: list[int] = Field(default_factory=lambda: [9, 21], description="自动签到时间, 使用本地时间小时数")
    keepalive_hours: list[int] = Field(default_factory=lambda: [22], description="自动刷新 Token 时间, 使用本地时间小时数")

    @field_validator("hard_credit", "soft_rate", "err_cooldown", mode="before")
    @classmethod
    def parse_duration(cls, value: Any, info: Any) -> float:
        """解析冷却时长配置。"""
        defaults = {
            "hard_credit": 43200,
            "soft_rate": 60,
            "err_cooldown": 600,
        }
        return duration(value, defaults[info.field_name])

    @property
    def timeout(self) -> float:
        """兼容业务层现有的 timeout 属性。"""
        return self.timeout_seconds

    @property
    def hard_cooldown(self) -> float:
        """兼容业务层现有的 hard_cooldown 属性。"""
        return self.hard_credit

    @property
    def soft_cooldown(self) -> float:
        """兼容业务层现有的 soft_cooldown 属性。"""
        return self.soft_rate

    @property
    def error_threshold(self) -> int:
        """兼容业务层现有的 error_threshold 属性。"""
        return self.err_threshold

    @property
    def error_cooldown(self) -> float:
        """兼容业务层现有的 error_cooldown 属性。"""
        return self.err_cooldown
