from fastapi import APIRouter

from app.api.responses import success

router = APIRouter()


@router.get("/")
async def root() -> dict:
    """服务根路由。"""
    return success({"name": "lobsterai2api", "version": "1.0.0"})


@router.get("/healthz")
async def healthz() -> dict:
    """健康检查。"""
    return success({"status": "ok"})
