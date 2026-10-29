"""AgentRun:一次"入口动作"的执行账本(生成/恢复各建一条)。

current_stage 沿图推进更新(一个 run 跨多个 agent/阶段);
thread_id 关联 LangGraph checkpoint,是断点续跑的关键(Day4-5)。
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.db.utils import china_now


class AgentRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="comicproject.id", index=True)
    # queued|running|succeeded|failed|cancelled|interrupted(进程崩溃遗留,可 resume)
    status: str = Field(default="queued")
    current_stage: str | None = None
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    auto_mode: bool = Field(default=False, description="自动模式:审批门免审直接通过")
    video_pending: bool = Field(
        default=False,
        description="成片被跳过(视频服务未就绪);配置完善后可『补生成成片』",
    )
    thread_id: str | None = Field(default=None, index=True)
    error: str | None = None
    created_at: datetime = Field(default_factory=china_now)
    updated_at: datetime = Field(default_factory=china_now)
