"""账号凭证与池状态的持久化协议。"""

from __future__ import annotations

from typing import Any, Protocol

from app.domain.auth import Auth


class AccountPersistence(Protocol):
    """账号凭证与运行状态的持久化接口。"""

    async def load_auths(self) -> list[Auth]:
        """读取全部账号凭证。"""
        ...

    async def save_auth(self, auth: Auth) -> None:
        """写入单个账号凭证。"""
        ...

    async def load_state(self) -> dict[str, Any]:
        """读取账号池运行状态。"""
        ...

    async def save_state(self, data: dict[str, Any]) -> None:
        """写入账号池运行状态。"""
        ...

    async def close(self) -> None:
        """关闭底层连接。"""
        ...
