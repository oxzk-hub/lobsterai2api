from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Auth:
    """单个 LobsterAI 账号凭证。"""

    access_token: str
    """访问令牌。"""
    refresh_token: str = ""
    """刷新令牌。"""
    expires_at: int = 0
    """访问令牌过期时间戳。"""
    uid: str = ""
    """账号唯一标识。"""
    user_id: str = ""
    """上游用户 ID。"""
    nickname: str = ""
    """账号昵称。"""
    uuid: str = ""
    """客户端 UUID。"""
    first_keyfrom: str = ""
    """首次 keyfrom。"""
    latest_keyfrom: str = ""
    """最近一次 keyfrom。"""

    @property
    def needs_refresh(self) -> bool:
        """访问令牌是否已进入刷新窗口。"""
        return bool(self.refresh_token) and self.expires_at <= int(time.time()) + 600

    def keyfrom(self) -> dict[str, Any]:
        """构造上游刷新请求所需的客户端标识。"""
        result: dict[str, Any] = {
            "firstKeyfrom": self.first_keyfrom,
            "latestKeyfrom": self.latest_keyfrom,
            "version": "0.1.0",
        }
        if self.uuid:
            result["uuid"] = self.uuid
        if self.user_id:
            result["userId"] = self.user_id
        return result

    def to_document(self) -> dict[str, Any]:
        """序列化为 Redis 中保存的账号文档。"""
        return {
            "auth": {
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "expiresAt": self.expires_at,
                "uuid": self.uuid,
                "firstKeyfrom": self.first_keyfrom,
                "latestKeyfrom": self.latest_keyfrom,
            },
            "account": {
                "uid": self.uid,
                "userId": self.user_id,
                "nickname": self.nickname,
            },
        }


def parse_auth(raw: dict[str, Any]) -> Auth | None:
    """解析账号文档; 兼容嵌套 `{auth, account}` 与扁平格式。"""
    data = raw.get("auth", {})
    account = raw.get("account", {})
    if not data:
        data, account = raw, raw
    token = str(data.get("accessToken", "")).strip()
    if not token:
        return None
    return Auth(
        token,
        str(data.get("refreshToken", "")),
        int(data.get("expiresAt", 0) or 0),
        str(account.get("uid", "")),
        str(account.get("userId", "")),
        str(account.get("nickname", "")),
        str(data.get("uuid", "")),
        str(data.get("firstKeyfrom", "")),
        str(data.get("latestKeyfrom", "")),
    )
