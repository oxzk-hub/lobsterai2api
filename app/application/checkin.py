from __future__ import annotations

import logging
from typing import Any

from app.application.container import ApplicationContainer

log = logging.getLogger("lobsterai2api.checkin")


class CheckinService:
    """Run account check-in, refresh credits, and release eligible cooldowns."""

    def __init__(self, container: ApplicationContainer):
        self.container = container

    async def run(self) -> dict[str, Any]:
        if not self.container.upstream:
            raise RuntimeError("upstream base URL is not configured")

        results: list[dict[str, Any]] = []
        for entry in list(self.container.pool.entries.values()):
            if entry.disabled or not entry.auth.access_token:
                continue
            result: dict[str, Any] = {"uid": entry.auth.uid, "checkin": "skipped"}
            try:
                checkin_result = await self.container.upstream.checkin(entry.auth)
                result["checkin"] = "ok" if checkin_result.get("checked_in") else "skipped"
                result["message"] = checkin_result.get("message")
                if checkin_result.get("credits_gained") is not None:
                    result["credits_gained"] = checkin_result["credits_gained"]
            except Exception as exc:
                result["checkin"] = "error"
                result["error"] = str(exc)
                log.warning("checkin %s: %s", entry.auth.uid, exc)

            try:
                remain = await self.container.upstream.quota(entry.auth)
                if remain is not None:
                    entry.credits = remain
                    if remain > 0 and not entry.disabled and entry.cooldown_kind == "hard_credit":
                        entry.until, entry.reason, entry.errors, entry.cooldown_kind = 0, "", 0, ""
                    await self.container.pool.save()
                    result["credits"] = remain
            except Exception as exc:
                result["quota"] = "error"
                result["quota_error"] = str(exc)
            results.append(result)

        return {"accounts": results}
