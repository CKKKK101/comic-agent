"""运营配置端点(薄壳):持久化/掩码/热更/预检都在 services/config_service.py。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.session import async_session_maker
from app.services.config_service import ConfigService

router = APIRouter()


class ConfigUpdate(BaseModel):
    values: dict[str, str] = Field(description="Settings 字段名 → 值;留空串表示清空")


@router.get("/config")
async def get_config() -> dict:
    async with async_session_maker() as session:
        return {"items": await ConfigService(session).list_public()}


@router.put("/config")
async def update_config(body: ConfigUpdate) -> dict:
    async with async_session_maker() as session:
        try:
            items = await ConfigService(session).save(body.values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"applied": True, "items": items}


