from fastapi import APIRouter, Request

from app.api.dependencies import get_container
from app.api.responses import success

router = APIRouter()


@router.get("/status")
async def status(request: Request) -> dict:
    container = get_container(request)
    return success({"accounts": container.pool.status()})
