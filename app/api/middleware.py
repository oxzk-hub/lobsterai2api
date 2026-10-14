from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.api.dependencies import get_container, is_authorized, unauthorized


CHAT_PATHS = frozenset({"/v1/chat/completions", "/v1/responses"})


class ChatAuthMiddleware(BaseHTTPMiddleware):
    """每个请求从 Redis 刷新账号池; 仅聊天接口校验 Bearer。"""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """先同步 Redis 状态, 再按路径决定是否鉴权。"""
        container = get_container(request)
        await container.reload()
        if request.url.path in CHAT_PATHS:
            if not is_authorized(container, request.headers.get("authorization")):
                return unauthorized()
        return await call_next(request)
