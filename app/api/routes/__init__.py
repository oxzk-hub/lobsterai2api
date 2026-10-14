from fastapi import APIRouter

from .chat import router as chat_router
from .checkin import router as checkin_router
from .credits import router as credits_router
from .health import router as health_router
from .login import router as login_router
from .models import router as models_router
from .status import router as status_router

router = APIRouter()
routers = [health_router, status_router, models_router, chat_router, checkin_router, credits_router, login_router]

__all__ = ["router", "routers"]
