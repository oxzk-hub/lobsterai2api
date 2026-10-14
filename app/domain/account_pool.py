from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.auth import Auth
from app.domain.persistence import AccountPersistence


@dataclass
class Entry:
    """账号池中的单个账号运行状态。"""

    auth: Auth
    """账号凭证。"""
    credits: int = 0
    """剩余积分。"""
    disabled: bool = False
    """是否已停用。"""
    reason: str = ""
    """停用或冷却原因。"""
    until: float = 0
    """冷却结束时间戳。"""
    errors: int = 0
    """连续错误次数。"""
    cooldown_kind: str = ""
    """当前冷却类型。"""

    def healthy(self) -> bool:
        """账号是否可用于发起上游请求。"""
        return not self.disabled and self.until <= time.time()


class AccountPool:
    """按积分选择健康账号, 并把运行状态写入 Redis。"""

    def __init__(self, store: AccountPersistence) -> None:
        """
        创建空的内存账号池。

        Args:
            store: 账号凭证与池状态的持久化实现。
        """
        self._store = store
        self.entries: dict[str, Entry] = {}

    async def reload(self) -> None:
        """从 Redis 加载状态, 再与账号凭证对齐。"""
        self.entries = {}
        await self._load_state()
        self.sync(await self._store.load_auths())

    async def _load_state(self) -> None:
        """把 Redis 中的运行状态灌入内存条目。"""
        data = await self._store.load_state()
        for uid, value in data.get("accounts", {}).items():
            if not isinstance(value, dict):
                continue
            self.entries[uid] = Entry(
                auth=Auth("", uid=uid),
                credits=int(value.get("credits", 0)),
                disabled=bool(value.get("disabled", False)),
                reason=str(value.get("reason", "")),
                until=self._parse_time(value.get("until")),
                cooldown_kind=str(value.get("cooldown_kind", "")),
            )

    @staticmethod
    def _parse_time(value: Any) -> float:
        """解析 ISO 时间; 非法值视为未冷却。"""
        if not value:
            return 0
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0

    def _dump_state(self) -> dict[str, Any]:
        """序列化当前内存状态。"""
        return {
            "accounts": {
                uid: {
                    "credits": entry.credits,
                    "disabled": entry.disabled,
                    "reason": entry.reason,
                    "until": datetime.fromtimestamp(entry.until).isoformat() if entry.until else None,
                    "cooldown_kind": entry.cooldown_kind,
                }
                for uid, entry in self.entries.items()
            }
        }

    async def save(self) -> None:
        """把账号池运行状态写入 Redis `data`。"""
        await self._store.save_state(self._dump_state())

    def sync(self, auths: list[Auth]) -> None:
        """以凭证列表为准同步内存条目, 去掉已删除账号。"""
        found = {auth.uid: auth for auth in auths if auth.uid}
        for uid, auth in found.items():
            if uid in self.entries:
                self.entries[uid].auth = auth
            else:
                self.entries[uid] = Entry(auth)
        for uid in list(self.entries):
            if uid not in found:
                del self.entries[uid]

    def pick(self, tried: set[str]) -> Entry | None:
        """选择积分最高且健康的未尝试账号。"""
        candidates = [
            entry
            for uid, entry in self.entries.items()
            if uid not in tried and entry.healthy() and entry.auth.access_token
        ]
        return max(candidates, key=lambda entry: entry.credits, default=None)

    def status(self) -> list[dict[str, Any]]:
        """导出账号状态快照。"""
        return [
            {
                "uid": uid,
                "nickname": entry.auth.nickname,
                "credits": entry.credits,
                "cooling": entry.until > time.time(),
                "until": datetime.fromtimestamp(entry.until).isoformat() if entry.until else None,
                "reason": entry.reason,
                "disabled": entry.disabled,
                "err_count": entry.errors,
            }
            for uid, entry in sorted(self.entries.items())
        ]

    async def cooldown(self, entry: Entry, seconds: float, reason: str, kind: str) -> None:
        """对账号施加冷却并立即持久化。"""
        entry.until, entry.reason, entry.errors = time.time() + seconds, reason, 0
        entry.cooldown_kind = kind
        await self.save()
