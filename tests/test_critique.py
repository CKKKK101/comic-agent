"""批评环(质量闭环)测试:默认高分放行;强制低分时回炉一次后放行,不无限循环。"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.config import get_settings
from app.db.session import async_session_maker
from app.models.message import Message
from tests.test_orchestration import _EXPECTED_CONTENT, _drive, _messages_for


async def _content_keys(project_id: int) -> set[str]:
    from app.models.project import ComicProject

    async with async_session_maker() as session:
        project = await session.get(ComicProject, project_id)
        return set((project.content or {}).keys())


def _count_quality_fail(msgs: list[str]) -> int:
    return sum(1 for m in msgs if "质量不达标" in m)


async def _all_messages(project_id: int) -> list[str]:
    async with async_session_maker() as session:
        rows = (
            await session.execute(select(Message).where(Message.project_id == project_id))
        ).scalars().all()
        return [r.content for r in rows]


@pytest.mark.asyncio
async def test_critique_passes_by_default(make_project, make_run) -> None:
    project = await make_project()
    run = await make_run(project, auto_mode=True)
    await _drive(run, on_interrupt=lambda _item: "")
    assert not _count_quality_fail(await _all_messages(project.id))


@pytest.mark.asyncio
async def test_critique_rerenders_once_then_forces_through(
    make_project, make_run, monkeypatch
) -> None:
    # 强制 critic 打低分。轮次按资产类型独立计数(max_rounds=2):
    # 角色图与分镜图各经历:round0 失败→回炉重渲;round1 失败→强制放行。
    # 因此每种资产 2 条"质量不达标"、渲染段各跑 2 次,互不连坐。
    settings = get_settings()
    monkeypatch.setattr(settings, "critique_force_fail", True)

    project = await make_project()
    run = await make_run(project, auto_mode=True)
    final = await _drive(run, on_interrupt=lambda _item: "")

    assert final.get("current_stage") == "compose_approval"
    msgs = await _all_messages(project.id)
    fail_count = _count_quality_fail(msgs)
    assert fail_count == 4, f"两类资产各失败 2 次(回炉+强制放行),实际 {fail_count} 次"
    assert len(await _messages_for(project.id, "render_characters")) == 2
    assert len(await _messages_for(project.id, "render_shots")) == 2
    assert await _content_keys(project.id) >= _EXPECTED_CONTENT


@pytest.mark.asyncio
async def test_critique_disabled_skips_check(make_project, make_run, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "critique_enabled", False)
    monkeypatch.setattr(settings, "critique_force_fail", True)  # 即使强制低分也不查

    project = await make_project()
    run = await make_run(project, auto_mode=True)
    await _drive(run, on_interrupt=lambda _item: "")
    assert not _count_quality_fail(await _all_messages(project.id))
