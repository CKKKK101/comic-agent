"""跨项目复用的角色；图片独立保存，不依赖来源项目的生命周期。"""
from datetime import datetime

from sqlmodel import Field, SQLModel

from app.db.utils import china_now


class LibraryCharacter(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    personality: str = ""
    appearance: str = ""
    quirks: str = ""
    style: str = ""
    image_url: str | None = None
    created_at: datetime = Field(default_factory=china_now)
