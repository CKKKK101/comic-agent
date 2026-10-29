"""HTTP 薄壳聚合:业务在 services/,路由按资源分文件。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.config import router as config_router
from app.api.projects import router as projects_router
from app.api.creative import router as creative_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(projects_router)
api_router.include_router(config_router)
api_router.include_router(creative_router)

__all__ = ["api_router"]
