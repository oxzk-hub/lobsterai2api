from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.dependencies import get_container
from app.api.responses import success
from app.application.credits import CreditService

router = APIRouter()


@router.get("/credits")
@router.get("/credit")
async def credits(request: Request) -> Any:
    container = get_container(request)
    return success(await CreditService(container).run())
