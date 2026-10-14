from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import uuid as uuid_module
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from app.application.container import ApplicationContainer
from app.domain.auth import Auth


LOGIN_TTL_SECONDS = 600


@dataclass
class LoginAttempt:
    """一次浏览器登录尝试。"""

    state: str
    """OAuth state。"""
    uuid: str
    """本次登录使用的客户端 UUID。"""
    first_keyfrom: str
    """首次 keyfrom。"""
    redirect_uri: str
    """回调地址。"""
    expires_at: float
    """尝试过期时间戳。"""


class LoginService:
    """管理浏览器登录并写入 Redis 账号凭证。"""

    def __init__(self, container: ApplicationContainer):
        """
        绑定应用容器。

        Args:
            container: 应用依赖容器。
        """
        self.container = container

    async def start(self) -> dict[str, Any]:
        """生成登录 URL 与一次性 state, 并写入 Redis。"""
        portal = self.container.settings.login_portal.rstrip("/")
        if not portal:
            raise RuntimeError("LB2A_LOGIN_PORTAL is empty")

        state = secrets.token_urlsafe(24)
        port = secrets.randbelow(64512) + 1024
        redirect_uri = f"http://127.0.0.1:{port}/auth/callback"
        attempt = LoginAttempt(
            state=state,
            uuid=str(uuid_module.uuid4()),
            first_keyfrom=str(int(time.time() * 1000)),
            redirect_uri=redirect_uri,
            expires_at=time.time() + LOGIN_TTL_SECONDS,
        )
        await self.container.store.save_login_attempt(
            {
                "state": attempt.state,
                "uuid": attempt.uuid,
                "first_keyfrom": attempt.first_keyfrom,
                "redirect_uri": attempt.redirect_uri,
                "expires_at": attempt.expires_at,
            },
            LOGIN_TTL_SECONDS,
        )
        query = urlencode({
            "source": "electron",
            "redirect_uri": redirect_uri,
            "state": state,
        })
        return {
            "login_url": f"{portal}/portal#/login?{query}",
            "state": state,
            "redirect_uri": redirect_uri,
            "expires_in": LOGIN_TTL_SECONDS,
        }

    async def complete(self, state: str, code: str) -> dict[str, Any]:
        """用登录 code 换取凭证并写入 Redis。"""
        raw = await self.container.store.pop_login_attempt(state)
        if not raw:
            raise ValueError("invalid or expired login state")
        attempt = LoginAttempt(
            state=str(raw.get("state", "")),
            uuid=str(raw.get("uuid", "")),
            first_keyfrom=str(raw.get("first_keyfrom", "")),
            redirect_uri=str(raw.get("redirect_uri", "")),
            expires_at=float(raw.get("expires_at", 0) or 0),
        )
        if attempt.expires_at <= time.time():
            raise ValueError("invalid or expired login state")
        if not code:
            raise ValueError("missing login code")
        if not self.container.upstream:
            raise RuntimeError("upstream base URL is not configured")

        data = await self.container.upstream.exchange(code, attempt.uuid, attempt.first_keyfrom)
        access_token = str(data["accessToken"])
        refresh_token = str(data.get("refreshToken", ""))
        user = data.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        uid = self._account_uid(user, access_token)
        expires_in = int(data.get("expiresIn", 0) or 0)
        expires_at = int(time.time()) + expires_in if expires_in > 0 else self._jwt_expiry(access_token)
        auth = Auth(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            uid=uid,
            user_id=str(user.get("userId", "")),
            nickname=str(user.get("nickname", "")),
            uuid=attempt.uuid,
            first_keyfrom=attempt.first_keyfrom,
            latest_keyfrom=str(int(time.time() * 1000)),
        )
        await self.container.store.save_auth(auth)
        await self.container.reload()
        return {
            "uid": uid,
            "user_id": auth.user_id,
            "nickname": auth.nickname,
        }

    @staticmethod
    def _jwt_expiry(token: str) -> int:
        """读取 JWT exp, 不校验签名; 令牌由上游签发。"""
        parts = token.split(".")
        if len(parts) != 3:
            return 0
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
            expires_at = int(claims.get("exp", 0) or 0)
            return expires_at if expires_at > 0 else 0
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return 0

    @staticmethod
    def _account_uid(user: dict[str, Any], access_token: str) -> str:
        """从用户信息提取可写入 Redis Hash field 的 uid。"""
        raw_uid = str(user.get("id") or user.get("userId") or user.get("yid") or "").strip()
        if not raw_uid:
            raw_uid = hashlib.sha256(access_token.encode()).hexdigest()[:16]
        return re.sub(r"[^A-Za-z0-9_.-]", "_", raw_uid)
