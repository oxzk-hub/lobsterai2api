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
    state_revision: int = 0
    """Redis 中状态的单调版本号。"""

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
        self._session_accounts: dict[str, str] = {}

    async def reload(self) -> None:
        """从 Redis 加载状态, 再与账号凭证对齐。"""
        data = await self._store.load_state()
        stored = data.get("accounts", {})
        if not isinstance(stored, dict):
            stored = {}
        previous = self.entries
        current: dict[str, Entry] = {}
        for auth in await self._store.load_auths():
            if not auth.uid:
                continue
            entry = previous.get(auth.uid) or Entry(auth)
            entry.auth = auth
            value = stored.get(auth.uid)
            if isinstance(value, dict) and int(value.get("revision", 0) or 0) >= entry.state_revision:
                self._apply_state(entry, value)
            current[auth.uid] = entry
        self.entries = current
        self._session_accounts = {
            key: uid for key, uid in self._session_accounts.items() if uid in current
        }

    @classmethod
    def _apply_state(cls, entry: Entry, value: dict[str, Any]) -> None:
        """将持久化状态应用到已有条目, 保留其对象身份供在途请求使用。"""
        entry.credits = int(value.get("credits", 0) or 0)
        entry.disabled = bool(value.get("disabled", False))
        entry.reason = str(value.get("reason", ""))
        entry.until = cls._parse_time(value.get("until"))
        entry.errors = max(0, int(value.get("errors", 0) or 0))
        entry.cooldown_kind = str(value.get("cooldown_kind", ""))
        entry.state_revision = int(value.get("revision", 0) or 0)

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
                    "errors": entry.errors,
                    "revision": entry.state_revision,
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

    def pick(self, tried: set[str], session_key: str | None = None) -> Entry | None:
        """选择账号; 已绑定会话优先复用原账号, 失败后才切换账号。"""
        if session_key:
            bound_uid = self._session_accounts.get(session_key)
            bound = self.entries.get(bound_uid or "")
            if bound and bound.auth.access_token and bound_uid not in tried and bound.healthy():
                return bound
        candidates = [
            entry
            for uid, entry in self.entries.items()
            if uid not in tried and entry.healthy() and entry.auth.access_token
        ]
        return max(candidates, key=lambda entry: entry.credits, default=None)

    def bind_session(self, session_key: str | None, entry: Entry) -> None:
        """把会话绑定到实际成功使用的账号, 供后续请求保持上下文。"""
        if session_key:
            self._session_accounts[session_key] = entry.auth.uid

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
        await self.update_entry(
            entry,
            {
                "until": datetime.fromtimestamp(time.time() + seconds).isoformat(),
                "reason": reason,
                "cooldown_kind": kind,
            },
            reset_errors=True,
        )

    async def update_entry(
        self,
        entry: Entry,
        updates: dict[str, Any],
        *,
        error_delta: int = 0,
        reset_errors: bool = False,
    ) -> None:
        """原子保存条目变更, 并同步 Redis 返回的最新状态。"""
        state = await self._store.update_account_state(
            entry.auth.uid,
            updates,
            error_delta=error_delta,
            reset_errors=reset_errors,
        )
        self._apply_state(entry, state)

    async def record_error(self, entry: Entry) -> None:
        """原子递增账号的连续错误计数。"""
        await self.update_entry(entry, {}, error_delta=1)

    async def clear_errors(self, entry: Entry) -> None:
        """清除账号连续错误计数并持久化。"""
        if entry.errors:
            await self.update_entry(entry, {}, reset_errors=True)

    async def disable(self, entry: Entry, reason: str) -> None:
        """停用账号并持久化原因。"""
        await self.update_entry(entry, {"disabled": True, "reason": reason})

    async def update_credits(self, entry: Entry, credits: int) -> None:
        """更新账号积分, 并在积分恢复时解除余额冷却。"""
        updates: dict[str, Any] = {"credits": credits}
        reset_errors = False
        if credits > 0 and entry.cooldown_kind == "hard_credit":
            updates.update({"until": None, "reason": "", "cooldown_kind": ""})
            reset_errors = True
        await self.update_entry(entry, updates, reset_errors=reset_errors)
