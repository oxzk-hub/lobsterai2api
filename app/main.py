from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.exceptions import register_exception_handlers
from app.api.middleware import ChatAuthMiddleware
from app.api.routes import routers
from app.application.container import ApplicationContainer
from app.application.login import LoginService
from app.application.scheduler import Scheduler

PUBLIC_DIR = Path(__file__).resolve().parents[1] / "public"
"""仓库根目录下的静态资源目录。"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """组装依赖; Vercel 上由 Cron 触发签到, 不启动进程内定时器。"""
    container = await ApplicationContainer.create()
    app.state.container = container
    app.state.login_service = LoginService(container)
    scheduler: Scheduler | None = None
    if os.getenv("VERCEL") != "1":
        scheduler = Scheduler(container)
        scheduler.start()
    try:
        yield
    finally:
        if scheduler:
            await scheduler.stop()
        await container.close()


app = FastAPI(
    title="lobsterai2api",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(ChatAuthMiddleware)
register_exception_handlers(app)
for route_router in routers:
    app.include_router(route_router)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    """返回站点图标。"""
    return FileResponse(PUBLIC_DIR / "favicon.ico", media_type="image/x-icon")
