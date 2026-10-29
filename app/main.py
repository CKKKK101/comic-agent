"""FastAPI 装配入口(工厂模式,测试可独立 create_app)。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.config import get_settings
from app.db.session import init_db
from app.services.media import STATIC_ROOT, ensure_static_dirs

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_static_dirs()  # images/videos/audio
    await init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    ensure_static_dirs()  # mount 前目录必须存在
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    # 托管生成的图片/视频(/static/... 路径由业务落盘)
    app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s", request.url.path)
        details = {"error": str(exc)} if settings.environment == "dev" else {}
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "服务器内部错误", "details": details}},
        )

    # 前端(卡通宇宙风单页;HashRouter → 只服务根路径;未 build 时静默跳过)。
    # 必须最后 mount:根路径会截获所有未匹配请求,抢先注册会让 /health 等路由 404。
    frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


app = create_app()
