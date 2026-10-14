from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings
from app.domain.account_pool import AccountPool
from app.infrastructure.account_store import RedisAccountStore
from app.infrastructure.upstream import Upstream


@dataclass
class ApplicationContainer:
    """启动时组装的应用级依赖。"""

    settings: Settings
    """运行时配置。"""
    pool: AccountPool
    """账号池。"""
    store: RedisAccountStore
    """Redis 账号存储。"""
    upstream: Upstream | None
    """上游 HTTP 客户端。"""

    @classmethod
    async def create(cls) -> ApplicationContainer:
        """连接 Redis, 加载账号, 并组装上游客户端。"""
        settings = Settings()
        store = RedisAccountStore(settings.redis_url, settings.redis_prefix)
        await store.ping()
        pool = AccountPool(store)
        await pool.reload()
        upstream = Upstream(settings, store) if settings.upstream_base else None
        return cls(settings=settings, pool=pool, store=store, upstream=upstream)

    async def reload(self) -> None:
        """从 Redis 重新加载账号凭证与池状态。"""
        await self.pool.reload()

    async def close(self) -> None:
        """关闭上游客户端与 Redis 连接。"""
        if self.upstream:
            await self.upstream.close()
        await self.store.close()
