"""漫剧项目:业务内容全部收敛在一个 content JSON 里(骨架期刻意简化)。

content 结构(由各生产阶段写入):
{
  "outline":      {"title": str, "logline": str, "acts": [{title, plot}], "style_note": str},
  "characters":   [{"name", "personality", "appearance", "quirks", "image_url"}],
  "shots":        [{"index", "scene", "camera", "action", "dialogue", "duration", "character_ids"}],
  "character_images": [{"character_name", "url", "prompt"}],
  "shot_images":  [{"shot_index", "url", "prompt"}],
  "video":        {"url", "voiceover_url", "notes"},
}
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from app.db.utils import china_now


class ComicProject(SQLModel, table=True):
    """一个漫剧创作项目(一次完整的"想法 → 成片"叙事线)。"""

    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(default="")
    topic: str  # 用户输入的故事想法/一句话
    style: str = Field(default="日系动漫", description="视觉风格")
    status: str = Field(
        default="draft",
        description="draft|generating|succeeded|failed|interrupted(启动清扫;取消回 draft)",
    )
    content: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=china_now)
    updated_at: datetime = Field(default_factory=china_now)

    def get_content(self, key: str) -> Any:
        return self.content.get(key)

    def set_content(self, key: str, value: Any) -> None:
        """写入 content 的一个键。

        注意:必须整体替换 dict 而不是原地修改 —— SQLAlchemy 不追踪
        JSON 列的原地变更,原地改会静默丢更新(commit 不发 UPDATE)。
        """
        updated = dict(self.content or {})
        updated[key] = value
        self.content = updated
