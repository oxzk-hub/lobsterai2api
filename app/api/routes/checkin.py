from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.dependencies import get_container
from app.api.responses import success
from app.application.checkin import CheckinService

router = APIRouter()


@router.get("/checkin")
@router.post("/checkin")
@router.get("/admin/checkin")
@router.post("/admin/checkin")
async def checkin(request: Request) -> Any:
    """手动或 Vercel Cron 触发全账号签到。"""
    container = get_container(request)
    return success(await CheckinService(container).run())
