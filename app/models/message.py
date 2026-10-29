"""Message:项目级对话流(给人看的)。

confirm(用户反馈)与各阶段进度都以消息行落库,前端聊天/时间线直接读这张表。
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.db.utils import china_now


class Message(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="comicproject.id", index=True)
    run_id: int | None = Field(default=None, foreign_key="agentrun.id", index=True)
    agent: str = ""  # 阶段/agent 名;user 表示用户反馈
    role: str = "assistant"  # assistant|user|system
    content: str = ""
    # 消息幂等的超步代次(插入时 = state.stage_history 长度;见 nodes._push_message_once)
    superstep: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=china_now)
