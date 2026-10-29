"""前端业务逻辑集中地:项目读模型组装 + 可执行动作推导。

API 层只做"接参/回状态","给前端看的业务"都在这里,便于单独单测:
- project_view:项目详情读模型(内容 + 最新 run + 消息流 + 可执行动作);
- 动作推导:can_generate / can_resume / can_cancel / waiting_gate ——
  前端按钮可用性与确认框是否弹出的唯一依据。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlmodel import Session

from app.config import get_settings
from app.db.session import checkpoint_exists
from app.models import agent_run as ar
from app.models.message import Message
from app.models.project import ComicProject
from app.services.config_service import compose_blocked_reason
from app.services.creative import edit_permissions

_TERMINAL = ("succeeded", "failed", "cancelled")


async def project_view(session: Session, project: ComicProject) -> dict[str, Any]:
    latest = (
        await session.execute(
            select(ar.AgentRun)
            .where(ar.AgentRun.project_id == project.id)
            .order_by(ar.AgentRun.id.desc())
            .limit(1)
        )
    ).scalars().first()
    messages = (
        await session.execute(
            select(Message)
            .where(Message.project_id == project.id)
            .order_by(Message.id.desc())
            .limit(50)
        )
    ).scalars().all()
    # interrupted 且无 checkpoint(崩溃于起步阶段)的 run 只能重开不能续跑,
    # 动作推导与 generate() 的放行逻辑同源(否则 UI 只有 resume 按钮、点了必 409)
    has_checkpoint = (
        await checkpoint_exists(latest.id) if latest is not None and latest.status == "interrupted" else False
    )
    actions = _actions(latest, has_checkpoint=has_checkpoint)
    actions.update(_video_actions(latest, project.content))
    actions.update(edit_permissions(latest))
    return {
        "id": project.id,
        "title": project.title,
        "topic": project.topic,
        "style": project.style,
        "status": project.status,
        "content": project.content,
        "created_at": project.created_at.isoformat(),
        "latest_run": run_view(latest) if latest else None,
        # 消息带元数据(id/agent/role):前端按角色渲染气泡与图标
        "messages": [
            {"id": m.id, "agent": m.agent, "role": m.role, "content": m.content}
            for m in reversed(messages)
        ],
        "actions": actions,
    }


def run_view(run: ar.AgentRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "current_stage": run.current_stage,
        "progress": round(run.progress, 3),
        "auto_mode": run.auto_mode,
        "video_pending": run.video_pending,
        "error": run.error,
    }


def _video_actions(
    latest: ar.AgentRun | None, content: dict[str, Any] | None
) -> dict[str, Any]:
    """『补生成成片』动作推导(与 compose 阶段跳过共用 compose_blocked_reason):

    成片缺失 + 上次跑已经"到过合成阶段"(video_pending 跳过 / 合成失败终态)
    + 有真实分镜画面 → can_complete_video;服务未就绪给出去『设置』页的文案。
    放宽到 failed:曾经"只认 video_pending"导致合成真失败后补片入口消失,
    用户只能整轮清空重来。
    """
    base = {"can_complete_video": False, "video_blocker": None}
    if latest is None:
        return base
    video = (content or {}).get("video") or {}
    if video.get("url"):
        return base  # 成片已存在(例如后来完整重跑过),无需补
    shots = (content or {}).get("shot_images") or []
    if not shots:
        return base  # 还没有分镜画面,谈何补成片
    reached_compose = (bool(getattr(latest, "video_pending", False)) or latest.status == "failed"
                       or bool((content or {}).get("video_dirty")))
    indexes = {im.get("shot_index") for im in shots if not im.get("stale")}
    if any(s.get("index") not in indexes for s in (content or {}).get("shots") or []):
        base["video_blocker"] = "有分镜画面需要重新生成，请先更新对应镜头"
        return base
    if not reached_compose:
        return base  # 没到合成阶段(如文本阶段失败)就不误导用户
    reason = compose_blocked_reason(get_settings(), shots)
    if reason is None:
        base["can_complete_video"] = True
    else:
        base["video_blocker"] = reason
    return base


def _actions(latest: ar.AgentRun | None, *, has_checkpoint: bool = False) -> dict[str, Any]:
    """按钮可用性与 UI 状态推导(产品语义):

    - 无 run / run 已终态(succeeded|failed|cancelled)→ 可"重新生成";
    - run 中断(interrupted):有断点 → 只能 resume 续跑;无断点(崩溃于起步阶段,
      无可续内容)→ 给"重新生成"而非必 409 的 resume(generate() 同源放行);
    - run 运行中 → 可取消;卡在审批门 → waiting_gate 给前端弹确认框。
    """
    if latest is None or latest.status in _TERMINAL:
        return {"can_generate": True, "can_resume": False, "can_cancel": False, "waiting_gate": None}
    if latest.status == "interrupted":
        return {
            "can_generate": not has_checkpoint,
            "can_resume": has_checkpoint,
            "can_cancel": False,
            "waiting_gate": None,
        }
    stage = latest.current_stage or ""
    waiting = stage if stage.endswith("_approval") else None
    return {"can_generate": False, "can_resume": False, "can_cancel": True, "waiting_gate": waiting}
