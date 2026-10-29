"""编排层集成测试:真实驱动 15 阶段图。

- 自动模式:全流程跑通,content 各键齐全;
- 人工反馈:在角色审批门注入"角色太单薄" → review 分诊回 plan_characters
  重跑 → 后续照常,验证 interrupt/resume + 分诊回炉 + force_rerun 全链路。
"""
from __future__ import annotations

import logging

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlmodel import select

from app.db.session import async_session_maker
from app.models.message import Message
from app.orchestration.driver import drive_graph_until_idle, interrupt_value
from app.orchestration.graph import build_phase2_graph

logger = logging.getLogger(__name__)

_EXPECTED_CONTENT = {"outline", "characters", "shots", "character_images", "shot_images", "video"}


async def _drive(run: object, on_interrupt) -> dict:
    compiled = build_phase2_graph().compile(checkpointer=InMemorySaver())
    return await drive_graph_until_idle(
        compiled,
        initial_payload={
            "project_id": run.project_id,
            "run_id": run.id,
            "auto_mode": run.auto_mode,
        },
        graph_config={"configurable": {"thread_id": f"agent-run-{run.id}"}},
        on_interrupt=on_interrupt,
        run_id=run.id,
    )


async def _content_keys(project_id: int) -> set[str]:
    async with async_session_maker() as session:
        from app.models.project import ComicProject

        project = await session.get(ComicProject, project_id)
        return set((project.content or {}).keys())


async def _messages_for(project_id: int, agent: str) -> list[str]:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(Message).where(Message.project_id == project_id, Message.agent == agent)
            )
        ).scalars().all()
        return [r.content for r in rows]


@pytest.mark.asyncio
async def test_auto_mode_runs_full_pipeline(make_project, make_run) -> None:
    project = await make_project()
    run = await make_run(project, auto_mode=True)

    final = await _drive(run, on_interrupt=lambda _item: "")

    assert final.get("current_stage") == "compose_approval"
    assert await _content_keys(project.id) >= _EXPECTED_CONTENT


@pytest.mark.asyncio
async def test_feedback_triages_through_review_and_reruns(make_project, make_run) -> None:
    project = await make_project()
    run = await make_run(project, auto_mode=False)
    seen: set[str] = set()

    async def judge(interrupt_item) -> dict:
        gate = interrupt_value(interrupt_item).get("gate")
        # 只在第一次角色审批门给一条文本反馈,其余全通过
        if gate == "characters_approval" and gate not in seen:
            seen.add(gate)
            return {"feedback": "角色太单薄,性格要更鲜明"}
        return {"feedback": ""}

    final = await _drive(run, on_interrupt=judge)

    assert final.get("current_stage") == "compose_approval"
    assert await _content_keys(project.id) >= _EXPECTED_CONTENT

    # 角色阶段被重跑:plan_characters 的消息应出现两次;review 分诊消息存在
    plan_msgs = await _messages_for(project.id, "plan_characters")
    assert len(plan_msgs) == 2, f"plan_characters 应重跑一次,实际消息数 {len(plan_msgs)}"
    review_msgs = await _messages_for(project.id, "review")
    assert any("plan_characters" in m for m in review_msgs), "review 应记录回炉目标"


@pytest.mark.asyncio
async def test_feedback_after_compose_approval_routes_to_review(make_project, make_run) -> None:
    """终审门给反馈 → review;review 分诊回炉后再走一轮到 END。"""
    project = await make_project()
    run = await make_run(project, auto_mode=False)
    seen: set[str] = set()

    async def judge(interrupt_item) -> dict:
        gate = interrupt_value(interrupt_item).get("gate")
        if gate == "compose_approval" and gate not in seen:
            seen.add(gate)
            return {"feedback": "分镜图有几张画面崩了"}
        return {"feedback": ""}

    final = await _drive(run, on_interrupt=judge)
    assert final.get("current_stage") == "compose_approval"
    # 反馈提到"分镜图/画面" → 回 render_shots,渲染阶段必须重跑
    render_msgs = await _messages_for(project.id, "render_shots")
    assert len(render_msgs) == 2, f"render_shots 应重跑一次,实际 {len(render_msgs)}"
    # 下游被清空后,compose 也必须被强制重跑,video 产物不能缺失(回归用例)
    assert "video" in await _content_keys(project.id)
