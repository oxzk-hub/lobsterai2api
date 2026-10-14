from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.application.checkin import CheckinService
from app.application.container import ApplicationContainer
from app.infrastructure.upstream import UpstreamError

log = logging.getLogger("lobsterai2api.scheduler")


class Scheduler:
    """Trigger application use cases on configured local-time schedules."""

    def __init__(self, container: ApplicationContainer):
        self.container = container
        self.checkin = CheckinService(container)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        last_checkin: tuple[int, int] | None = None
        last_keepalive: tuple[int, int] | None = None
        while True:
            try:
                now = datetime.now()
                marker = (now.toordinal(), now.hour)
                if now.hour in self.container.settings.checkin_hours and marker != last_checkin:
                    last_checkin = marker
                    await self._run_checkin()
                if now.hour in self.container.settings.keepalive_hours and marker != last_keepalive:
                    last_keepalive = marker
                    await self._refresh_tokens()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler iteration failed")
            await asyncio.sleep(30)

    async def _run_checkin(self) -> None:
        try:
            await self.checkin.run()
        except Exception as exc:
            log.warning("scheduled checkin failed: %s", exc)

    async def _refresh_tokens(self) -> None:
        if not self.container.upstream:
            return
        for entry in list(self.container.pool.entries.values()):
            if entry.disabled or not entry.auth.refresh_token:
                continue
            try:
                await self.container.upstream.refresh(entry.auth)
            except UpstreamError as exc:
                if exc.kind == "session_dead":
                    entry.disabled, entry.reason = True, "refresh session dead"
                    await self.container.pool.save()
                else:
                    log.warning("token refresh failed for %s: %s", entry.auth.uid, exc)
            except Exception:
                log.exception("token refresh crashed for %s", entry.auth.uid)
