from __future__ import annotations

import logging
from typing import Any

from app.application.container import ApplicationContainer

log = logging.getLogger("lobsterai2api.credits")


class CreditService:
    """Query upstream credits and update the local account pool."""

    def __init__(self, container: ApplicationContainer):
        self.container = container

    async def run(self) -> dict[str, Any]:
        if not self.container.upstream:
            raise RuntimeError("upstream base URL is not configured")

        accounts: list[dict[str, Any]] = []
        total = 0
        for entry in sorted(self.container.pool.entries.values(), key=lambda item: item.auth.uid):
            result: dict[str, Any] = {
                "uid": entry.auth.uid,
                "nickname": entry.auth.nickname,
                "credits": None,
                "disabled": entry.disabled,
            }
            if not entry.auth.access_token:
                result["status"] = "missing_token"
                accounts.append(result)
                continue

            try:
                credits = await self.container.upstream.quota(entry.auth)
                result["credits"] = credits
                result["status"] = "ok" if credits is not None else "unavailable"
                if credits is not None:
                    entry.credits = credits
                    total += credits
                    await self.container.pool.save()
            except Exception as exc:
                result["status"] = "error"
                result["error"] = str(exc)
                log.warning("credit query %s: %s", entry.auth.uid, exc)
            accounts.append(result)

        return {"total_credits": total, "accounts": accounts}
