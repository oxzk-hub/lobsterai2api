from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.api.dependencies import get_login_service
from app.api.responses import success

router = APIRouter()


@router.get("/auth/login")
async def login(request: Request) -> dict[str, Any]:
    """开始浏览器登录, state 写入 Redis。"""
    return success(await get_login_service(request).start())


@router.get("/auth/callback")
async def login_callback(request: Request, code: str | None = None, state: str | None = None) -> dict[str, Any]:
    if not code:
        raise HTTPException(status_code=400, detail="missing login code")
    if not state:
        raise HTTPException(status_code=400, detail="missing login state")
    try:
        account = await get_login_service(request).complete(state, code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return success(account, "account added")
