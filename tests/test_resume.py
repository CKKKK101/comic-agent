"""断点续跑测试:进程崩溃 → 启动清扫 → resume 从 checkpoint 续跑。

核心断言:resume 后**不重跑已完成阶段**(plan_outline 等生产节点只产一次),
证明续跑读的是 checkpoint 而不是从头再生成。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlmodel import select

from app.config import get_settings
from app.db.session import async_session_maker, sweep_orphaned_runs
from app.models import agent_run as ar
from app.models.message import Message
from app.orchestration.driver import drive_graph_until_idle
from app.orchestration.graph import build_phase2_graph
from app.orchestration.persistence import open_checkpointer

_EXPECTED_CONTENT = {"outline", "characters", "shots", "character_images", "shot_images", "video"}


async def _messages_count(project_id: int, agent: str) -> int:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(Message).where(Message.project_id == project_id, Message.agent == agent)
            )
        ).scalars().all()
        return len(rows)


async def _content_keys(project_id: int) -> set[str]:
    from app.models.project import ComicProject

    async with async_session_maker() as session:
        project = await session.get(ComicProject, project_id)
        return set((project.content or {}).keys())


async def _drive(run: ar.AgentRun, *, resume: bool = False, on_interrupt) -> dict:
    settings = get_settings()
    async with open_checkpointer(settings.database_url) as saver:
        compiled = build_phase2_graph().compile(checkpointer=saver)
        return await drive_graph_until_idle(
            compiled,
            initial_payload=(
                None
                if resume
                else {"project_id": run.project_id, "run_id": run.id, "auto_mode": run.auto_mode}
            ),
            graph_config={"configurable": {"thread_id": f"agent-run-{run.id}"}},
            on_interrupt=on_interrupt,
            run_id=run.id,
        )


@pytest.mark.asyncio
async def test_sweep_marks_orphaned_running_as_interrupted(db, make_project, make_run) -> None:
    project = await make_project()
    run = await make_run(project, auto_mode=True)
    async with async_session_maker() as session:
        run.status = "running"  # 模拟进程崩溃时遗留的 running 行
        session.add(run)
        await session.commit()

    swept = await sweep_orphaned_runs()
    assert swept == 1
    async with async_session_maker() as session:
        fresh = await session.get(ar.AgentRun, run.id)
        assert fresh.status == "interrupted"
        assert "resume" in (fresh.error or "")


@pytest.mark.asyncio
async def test_crash_then_resume_continues_from_checkpoint_without_replay(
    make_project, make_run
) -> None:
    """第一次驱动在 outline 门"崩溃";resume 后从 checkpoint 续跑:
    - 重新停在 outline 门等人确认(interrupt 原样返回);
    - approve 后继续走完,plan_outline 只生产一次(没有全量重放)。
    """
    project = await make_project()
    run = await make_run(project, auto_mode=False)

    # 第一次执行:在第一个 interrupt 处模拟进程崩溃(judge 抛异常)
    async def crash(_item) -> None:
        raise RuntimeError("模拟进程崩溃")

    with pytest.raises(RuntimeError):
        await _drive(run, on_interrupt=crash)

    # 模拟重启:run 行被清扫为 interrupted,然后 resume
    async with async_session_maker() as session:
        fresh = await session.get(ar.AgentRun, run.id)
        fresh.status = "interrupted"
        fresh.error = "Service restarted"
        session.add(fresh)
        await session.commit()

    async def approve_all(_item) -> dict:
        return {"feedback": ""}

    final = await _drive(run, resume=True, on_interrupt=approve_all)

    assert final.get("current_stage") == "compose_approval"
    assert await _content_keys(project.id) >= _EXPECTED_CONTENT
    # 核心断言:生产阶段没有被重放(每阶段只产一次,读的是 checkpoint)
    assert await _messages_count(project.id, "plan_outline") == 1
    assert await _messages_count(project.id, "plan_characters") == 1
    assert await _messages_count(project.id, "compose") == 1


@pytest.mark.asyncio
async def test_api_resume_end_to_end(db, make_project, make_run) -> None:
    """端到端:API 生成 → 取消(crash 模拟)→ 标记 interrupted → /resume 续跑成功。

    取消发生在 outline 门等待时,checkpoint 已落盘;resume 后各阶段不重放。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_api import _confirm_gate

    project = await make_project()
    run = await make_run(project, auto_mode=False)

    with TestClient(create_app()) as client:
        # 1) 后台任务驱动到 outline 门后"崩溃"(任务直接取消,checkpoint 已在 interrupt 处落盘)
        async def _parked() -> None:
            from app.runner import run_pipeline

            task = asyncio.create_task(run_pipeline(run.id))
            # 等 run 真正停在 outline 门
            deadline = time.time() + 10
            while time.time() < deadline:
                async with async_session_maker() as session:
                    fresh = await session.get(ar.AgentRun, run.id)
                    if fresh is not None and fresh.current_stage == "outline_approval":
                        break
                await asyncio.sleep(0.02)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _parked()

        # 2) 模拟启动清扫:任务被直接取消(无"显式取消"意图)= 停机/崩溃语义,
        #    runner 收尾已落 interrupted(可 resume);与用户点「停止」的
        #    cancelled 区分 —— 后者由 cancel 端点先登记意图(见 runner.py)
        async with async_session_maker() as session:
            fresh = await session.get(ar.AgentRun, run.id)
            assert fresh.status == "interrupted", "停机中止应标 interrupted(可续跑)"
            fresh.error = "Service restarted"
            session.add(fresh)
            await session.commit()

        # 3) /resume → 重新卡在 outline 门 → 逐步确认到成功
        resp = client.post(f"/api/v1/projects/{project.id}/resume")
        assert resp.status_code == 200, resp.text
        assert resp.json()["resumed"] is True

        deadline = time.time() + 15
        while time.time() < deadline:
            view = client.get(f"/api/v1/projects/{project.id}").json()
            run_view = view["latest_run"]
            if run_view["status"] != "running":
                break
            if run_view["current_stage"] and run_view["current_stage"].endswith("_approval"):
                _confirm_gate(client, project.id, run.id, feedback="")
                await asyncio.sleep(0.15)
            else:
                await asyncio.sleep(0.02)

        assert view["latest_run"]["status"] == "succeeded"
        assert set(view["content"]) >= _EXPECTED_CONTENT
