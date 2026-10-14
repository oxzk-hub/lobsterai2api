from __future__ import annotations

import time
from typing import Any

from app.domain.account_pool import AccountPool
from app.domain.persistence import AccountPersistence
from app.infrastructure.upstream import Upstream

MODELS_CACHE_TTL = 3600
"""上游模型列表缓存有效期, 单位秒。"""

# 模型映射：请求里的模型名在发给上游前会先查这里, 例如 "gpt-4o" -> "deepseek-v4-pro"。
MODEL_MAP: dict[str, str] = {}

# 请求的模型不在映射表和上游可用列表里时, 固定使用这个模型发给上游。
FALLBACK_MODEL = "deepseek-flash"


def map_model(model: str, available: set[str] | None = None) -> str:
    """按映射表转换模型名; 未知模型固定回退到 FALLBACK_MODEL。"""
    mapped = MODEL_MAP.get(model, model)
    if available is None or mapped in available:
        return mapped
    return FALLBACK_MODEL


class ModelService:
    """上游模型目录服务, 请求成功后缓存到 Redis, 为空或失败返回空列表。"""

    def __init__(self, pool: AccountPool, upstream: Upstream | None, store: AccountPersistence):
        """
        创建模型服务。

        Args:
            pool: 用于挑选账号请求上游模型列表。
            upstream: 上游 HTTP 客户端, 为空时视为不可用。
            store: 模型列表缓存的持久化实现。
        """
        self.pool = pool
        self.upstream = upstream
        self.store = store

    async def list_models(self) -> dict[str, Any]:
        """获取上游模型列表; 未获取到返回空列表, 获取成功后写入 Redis 缓存。"""
        ids = await self.upstream_ids()
        if not ids:
            return {"object": "list", "data": []}
        await self.store.save_models(ids, MODELS_CACHE_TTL)
        data = [{"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "lobsterai"} for model_id in ids]
        return {"object": "list", "data": data}

    async def upstream_ids(self) -> list[str]:
        """请求上游模型列表; 无可用账号或请求失败返回空列表。"""
        if not self.upstream:
            return []
        entry = self.pool.pick(set())
        if not entry:
            return []
        try:
            return await self.upstream.models(entry.auth)
        except Exception:
            return []

    async def available_models(self) -> set[str]:
        """当前可用的上游模型集合, 用于聊天请求的模型映射校验。"""
        cached = await self.store.load_models()
        if cached:
            return set(cached)
        ids = await self.upstream_ids()
        if ids:
            await self.store.save_models(ids, MODELS_CACHE_TTL)
        return set(ids)
