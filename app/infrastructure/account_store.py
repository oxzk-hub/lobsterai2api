"""账号凭证与池状态的 Redis 持久化。"""

from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis

from app.domain.auth import Auth, parse_auth

log = logging.getLogger("lobsterai2api.store")


MODELS_CACHE_TTL = 3600
"""上游模型列表缓存有效期, 单位秒。"""


class RedisAccountStore:
    """使用 Redis 保存 `auths`、`data` 与模型列表缓存。"""

    def __init__(self, redis_url: str, prefix: str) -> None:
        """
        创建 Redis 存储。

        Args:
            redis_url: Redis 连接地址。
            prefix: 键前缀, 实际键为 `{prefix}:auths` 与 `{prefix}:data`。
        """
        if not redis_url.strip():
            raise RuntimeError("LB2A_REDIS_URL is empty")
        self._prefix = prefix.strip() or "lb2a"
        self._redis = Redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
        )

    @property
    def auths_key(self) -> str:
        """账号凭证 Hash 键。"""
        return f"{self._prefix}:auths"

    @property
    def data_key(self) -> str:
        """账号池状态键。"""
        return f"{self._prefix}:data"

    @property
    def models_key(self) -> str:
        """上游模型列表缓存键。"""
        return f"{self._prefix}:models"

    async def load_models(self) -> list[str]:
        """读取缓存的上游模型列表, 未缓存或已过期返回空列表。"""
        payload = await self._redis.get(self.models_key)
        if not payload:
            return []
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("ignore invalid models cache")
            return []
        if not isinstance(data, list):
            return []
        return [str(item) for item in data if item]

    async def save_models(self, models: list[str], ttl_seconds: int) -> None:
        """写入上游模型列表缓存, 到期自动删除。"""
        await self._redis.set(
            self.models_key,
            json.dumps([str(item) for item in models if item], ensure_ascii=False),
            ex=ttl_seconds,
        )

    async def ping(self) -> None:
        """探测 Redis 是否可用。"""
        if not await self._redis.ping():
            raise RuntimeError("Redis ping failed")

    async def load_auths(self) -> list[Auth]:
        """读取全部账号凭证。"""
        raw = await self._redis.hgetall(self.auths_key)
        auths: list[Auth] = []
        for uid, payload in raw.items():
            try:
                document = json.loads(payload)
            except json.JSONDecodeError:
                log.warning("skip invalid auth document %s", uid)
                continue
            if not isinstance(document, dict):
                log.warning("skip invalid auth document %s", uid)
                continue
            auth = parse_auth(document)
            if not auth:
                continue
            if not auth.uid:
                auth.uid = str(uid)
            auths.append(auth)
        return auths

    async def save_auth(self, auth: Auth) -> None:
        """写入单个账号凭证。"""
        if not auth.uid:
            raise ValueError("auth uid is empty")
        await self._redis.hset(
            self.auths_key,
            mapping={auth.uid: json.dumps(auth.to_document(), ensure_ascii=False)},
        )

    async def load_state(self) -> dict[str, Any]:
        """读取账号池运行状态。"""
        payload = await self._redis.get(self.data_key)
        if not payload:
            return {}
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("ignore invalid pool state document")
            return {}
        return data if isinstance(data, dict) else {}

    async def save_state(self, data: dict[str, Any]) -> None:
        """写入账号池运行状态。"""
        await self._redis.set(self.data_key, json.dumps(data, ensure_ascii=False))

    def login_key(self, state: str) -> str:
        """登录尝试键。"""
        return f"{self._prefix}:login:{state}"

    async def save_login_attempt(self, attempt: dict[str, Any], ttl_seconds: int) -> None:
        """写入一次性登录尝试, 到期自动删除。"""
        state = str(attempt.get("state", "")).strip()
        if not state:
            raise ValueError("login state is empty")
        await self._redis.set(
            self.login_key(state),
            json.dumps(attempt, ensure_ascii=False),
            ex=ttl_seconds,
        )

    async def pop_login_attempt(self, state: str) -> dict[str, Any] | None:
        """读取并删除一次登录尝试。"""
        key = self.login_key(state)
        pipe = self._redis.pipeline()
        pipe.get(key)
        pipe.delete(key)
        payload, _deleted = await pipe.execute()
        if not payload:
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    async def close(self) -> None:
        """关闭 Redis 连接。"""
        await self._redis.aclose()
