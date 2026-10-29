"""ConfigItem:运营配置的持久层(前端『设置』页读写;.env 仅作部署基线)。

键名与 Settings 字段名一致(如 text_model / doubao_api_key),
值一律存字符串,类型转换发生在 apply_settings_overrides(见 config.py)。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from app.db.utils import china_now


class ConfigItem(SQLModel, table=True):
    key: str = Field(primary_key=True, max_length=255)
    value: str = Field(default="", sa_column=Column(Text, nullable=False))
    updated_at: datetime = Field(default_factory=china_now)
