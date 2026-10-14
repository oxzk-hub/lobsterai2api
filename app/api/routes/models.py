from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.dependencies import get_container
from app.api.responses import success
from app.application.models import ModelService

router = APIRouter()


@router.get("/v1/models")
async def models(request: Request) -> Any:
    container = get_container(request)
    data = await ModelService(container.pool, container.upstream, container.store).list_models()
    return success(data)
