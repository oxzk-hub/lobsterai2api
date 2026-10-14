from app.config.settings import Settings
from app.domain.account_pool import AccountPool, Entry
from app.infrastructure.upstream import UpstreamError


class AccountFailurePolicy:
    """按上游错误类型更新账号冷却或停用状态。"""

    def __init__(self, pool: AccountPool, settings: Settings):
        """
        绑定账号池与冷却配置。

        Args:
            pool: 账号池。
            settings: 运行时配置。
        """
        self.pool = pool
        self.settings = settings

    async def record(self, entry: Entry, error: UpstreamError) -> None:
        """根据错误类型停用账号或写入冷却。"""
        if error.kind == "session_dead":
            entry.disabled, entry.reason = True, "session dead"
            await self.pool.save()
        elif error.kind == "hard_credit":
            await self.pool.cooldown(entry, self.settings.hard_cooldown, "余额不足", "hard_credit")
        elif error.kind in ("soft_rate", "not_found"):
            await self.pool.cooldown(entry, self.settings.soft_cooldown, error.kind, "soft_rate")
        else:
            entry.errors += 1
            if entry.errors >= self.settings.error_threshold:
                await self.pool.cooldown(entry, self.settings.error_cooldown, "consecutive errors", "error")
