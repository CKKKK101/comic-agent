"""API 集成测试(TestClient + 真实 DB 文件):薄壳路由的行为契约。

注意:client 必须是上下文管理器(with TestClient(...)),保证后台生成任务
与请求共用同一个事件循环;否则每个请求各起一个 loop,后台任务会被取消。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.db.session import async_session_maker

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

_TOPIC = "咖啡师与时间旅行者"


def _wait_run_finished(client: TestClient, project_id: int, *, expect: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/v1/projects/{project_id}").json()["latest_run"]["status"]
        if status == expect:
            return status
        time.sleep(0.05)
    raise AssertionError(f"等待 run 状态 {expect} 超时,最后状态 {status}")


def _confirm_gate(client: TestClient, project_id: int, run_id: int, feedback: str) -> None:
    """确认当前审批门。容忍两个合法的 409:
    - run 恰在确认瞬间结束(最后一道门确认后 runner 收尾的毫秒级窗口);
    - 点击落在"门已可见、wait 尚未开始"的毫秒窗口 —— 裁决必须打在 run 正在
      门等待时(见 gateway 状态机),稍等重试即可。
    """
    deadline = time.time() + 5
    while True:
        resp = client.post(
            f"/api/v1/projects/{project_id}/confirm",
            json={"run_id": run_id, "feedback": feedback},
        )
        if resp.status_code == 200:
            return
        if resp.status_code == 409:
            run = client.get(f"/api/v1/projects/{project_id}").json()["latest_run"]
            if run["status"] != "running":
                return  # run 刚结束,无门可确认
            if time.time() >= deadline:
                raise AssertionError(f"confirm 持续 409: {resp.text}")
            time.sleep(0.05)  # 等 run 真正停到门上后重试
            continue
        raise AssertionError(f"confirm 失败 {resp.status_code}: {resp.text}")


def test_full_api_flow_create_generate_confirm(client) -> None:
    resp = client.post("/api/v1/projects", json={"topic": _TOPIC, "style": "日系动漫"})
    assert resp.status_code == 201
    project_id = resp.json()["id"]

    # 手动模式启动 → 卡在第一道门(outline)
    r = client.post(f"/api/v1/projects/{project_id}/generate", json={"auto_mode": False})
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    # 同项目再启动 → 409(单执行模型)
    assert client.post(f"/api/v1/projects/{project_id}/generate", json={}).status_code == 409

    # 逐步通过 6 道门(都点"通过"),直到 succeeded。
    # 注意:生产阶段(plan_outline 等)是合法瞬时状态,只在审批门等待时确认。
    deadline = time.time() + 30
    while time.time() < deadline:
        view = client.get(f"/api/v1/projects/{project_id}").json()
        run = view["latest_run"]
        if run["status"] != "running":
            break
        if run["current_stage"] and run["current_stage"].endswith("_approval"):
            _confirm_gate(client, project_id, run_id, feedback="")
            time.sleep(0.15)  # 等 run 跨过 runner 收尾窗口,避免重复确认终态 run
        else:
            time.sleep(0.02)

    view = client.get(f"/api/v1/projects/{project_id}").json()
    assert view["latest_run"]["status"] == "succeeded"
    assert view["status"] == "succeeded"
    expected = {"outline", "characters", "shots", "character_images", "shot_images", "video"}
    assert expected <= set(view["content"])

    # 每道门的"请确认"提示在消息流里只落一条(动态 interrupt 的 resume 重放
    # 会把 interrupt 前的落库副作用执行两遍 → 幂等写修复,防 UI 双行)
    gate_counts: dict[str, int] = {}
    for m in view["messages"]:
        if m["role"] == "assistant" and m["agent"].endswith("_approval"):
            gate_counts[m["agent"]] = gate_counts.get(m["agent"], 0) + 1
    assert set(gate_counts) == {
        "outline_approval",
        "characters_approval",
        "shots_approval",
        "character_images_approval",
        "shot_images_approval",
        "compose_approval",
    }, f"六道门都应有提示消息,实际 {sorted(gate_counts)}"
    assert all(v == 1 for v in gate_counts.values()), f"提示消息重复: {gate_counts}"


def test_feedback_confirmed_via_api_is_stored_and_applied(client) -> None:
    """给第一道门发文本反馈 → review 分诊 → 回炉 plan_outline(大纲)重跑。"""
    project_id = client.post("/api/v1/projects", json={"topic": _TOPIC}).json()["id"]
    run_id = client.post(
        f"/api/v1/projects/{project_id}/generate", json={"auto_mode": False}
    ).json()["run_id"]

    # 先等 run 真正停在大纲门(confirm 只认挂在确认门的时刻 —— 生产阶段
    # 内的提前确认会被 409 拒绝,这正是护栏语义)
    deadline = time.time() + 10
    while time.time() < deadline:
        view = client.get(f"/api/v1/projects/{project_id}").json()
        run = view["latest_run"]
        if run["status"] != "running" or run["current_stage"] == "outline_approval":
            break
        time.sleep(0.03)
    assert client.get(f"/api/v1/projects/{project_id}").json()["latest_run"]["current_stage"] == (
        "outline_approval"
    ), "run 应停在大纲门等待反馈"

    # 裁决必须打在 run 真正停在门的时刻:毫秒级"门可见、wait 未开始"窗口内
    # 会 409,helper 带重试(见 gateway 状态机语义)
    _confirm_gate(client, project_id, run_id, feedback="结局太仓促,要更圆满")

    # 大纲反馈 → 直接回 plan_outline 重跑 → 重新卡在大纲门;此时给它"通过"
    deadline = time.time() + 10
    while time.time() < deadline:
        view = client.get(f"/api/v1/projects/{project_id}").json()
        run = view["latest_run"]
        if run["status"] != "running":
            break
        if run["current_stage"] == "outline_approval":
            _confirm_gate(client, project_id, run_id, feedback="")
        time.sleep(0.05)

    # 用户反馈已落库成一条 user 消息
    view = client.get(f"/api/v1/projects/{project_id}").json()
    assert any(
        m["role"] == "user" and "结局太仓促" in m["content"] for m in view["messages"]
    ), "反馈应作为 user 消息出现在消息流"

    # 一路点到成功
    deadline = time.time() + 30
    while time.time() < deadline:
        view = client.get(f"/api/v1/projects/{project_id}").json()
        run = view["latest_run"]
        if run["status"] != "running":
            break
        if run["current_stage"] and run["current_stage"].endswith("_approval"):
            _confirm_gate(client, project_id, run_id, feedback="")
            time.sleep(0.15)
        else:
            time.sleep(0.02)
    assert view["latest_run"]["status"] == "succeeded"


def test_generate_conflict_and_cancel(client) -> None:
    resp = client.post("/api/v1/projects", json={"topic": _TOPIC})
    project_id = resp.json()["id"]

    assert client.post(
        f"/api/v1/projects/{project_id}/generate", json={"auto_mode": False}
    ).status_code == 200

    # 确认不存在的 run → 404
    assert client.post(
        f"/api/v1/projects/{project_id}/confirm", json={"run_id": 99999, "feedback": ""}
    ).status_code == 404

    # 取消:任务取消 → run 标记 cancelled,项目回到草稿(不再永久"生成中")
    assert client.post(f"/api/v1/projects/{project_id}/cancel").status_code == 200
    _wait_run_finished(client, project_id, expect="cancelled")
    view = client.get(f"/api/v1/projects/{project_id}").json()
    assert view["latest_run"]["status"] == "cancelled"
    assert view["status"] == "draft", "取消后项目状态必须退出 generating"


def test_project_404(client) -> None:
    assert client.get("/api/v1/projects/424242").status_code == 404
    assert client.post("/api/v1/projects/424242/generate", json={}).status_code == 404


def _complete_run_to_success(client, project_id: int, run_id: int) -> None:
    """逐步通过全部 6 道门直到 succeeded(fake provider 秒过)。"""
    deadline = time.time() + 30
    while time.time() < deadline:
        view = client.get(f"/api/v1/projects/{project_id}").json()
        run = view["latest_run"]
        if run["status"] != "running":
            break
        if run["current_stage"] and run["current_stage"].endswith("_approval"):
            _confirm_gate(client, project_id, run_id, feedback="")
            time.sleep(0.1)
        else:
            time.sleep(0.02)
    assert client.get(f"/api/v1/projects/{project_id}").json()["latest_run"]["status"] == "succeeded"


def test_full_regenerate_clears_old_assets(client) -> None:
    """整轮重新生成(不带 start_stage)= 推倒重来 → 旧产物(content)必须清空,
    否则新一轮文本批准前会展示旧分镜图/立绘/成片误导用户。"""
    project_id = client.post("/api/v1/projects", json={"topic": _TOPIC}).json()["id"]
    run_id = client.post(f"/api/v1/projects/{project_id}/generate", json={}).json()["run_id"]
    _complete_run_to_success(client, project_id, run_id)
    assert client.get(f"/api/v1/projects/{project_id}").json()["content"], "第一轮应有产物"

    # 再次整轮重新生成 → content 立即清空(run 从 plan_outline 起步)
    resp = client.post(f"/api/v1/projects/{project_id}/generate", json={})
    assert resp.status_code == 200
    view = client.get(f"/api/v1/projects/{project_id}").json()
    assert view["content"] == {}, "整轮重跑必须清空旧产物"
    assert view["latest_run"]["status"] == "running"
    client.post(f"/api/v1/projects/{project_id}/cancel")


def test_staged_rerun_keeps_existing_assets(client) -> None:
    """定点补跑(start_stage=render_shots/compose)复用已批准内容 → 不清空。"""
    project_id = client.post("/api/v1/projects", json={"topic": _TOPIC}).json()["id"]
    run_id = client.post(f"/api/v1/projects/{project_id}/generate", json={}).json()["run_id"]
    _complete_run_to_success(client, project_id, run_id)
    keys = set(client.get(f"/api/v1/projects/{project_id}").json()["content"])

    resp = client.post(
        f"/api/v1/projects/{project_id}/generate",
        json={"start_stage": "render_shots"},
    )
    assert resp.status_code == 200
    view = client.get(f"/api/v1/projects/{project_id}").json()
    assert set(view["content"]) == keys, "定点补跑不清空已有产物"
    client.post(f"/api/v1/projects/{project_id}/cancel")


# ---------------------------------------------------------------------------
# 删除项目(不可恢复)
# ---------------------------------------------------------------------------

def test_delete_project_not_found(client) -> None:
    assert client.delete("/api/v1/projects/424242").status_code == 404


def test_delete_project_stops_running_run(client) -> None:
    """生成中删除:自动取消后台 run 并等它收尾,项目与行彻底删除。"""
    project_id = client.post("/api/v1/projects", json={"topic": _TOPIC}).json()["id"]
    client.post(
        f"/api/v1/projects/{project_id}/generate", json={"auto_mode": False}
    ).json()["run_id"]

    # 等 run 真正停在审批门(取消才可被及时处理,不测竞态窗口)
    deadline = time.time() + 10
    while time.time() < deadline:
        view = client.get(f"/api/v1/projects/{project_id}").json()
        run = view["latest_run"]
        if run["current_stage"] and run["current_stage"].endswith("_approval"):
            break
        time.sleep(0.05)
    assert (
        client.get(f"/api/v1/projects/{project_id}").json()["latest_run"]["status"] == "running"
    ), "run 应仍挂在审批门"

    resp = client.delete(f"/api/v1/projects/{project_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404
    # 后台任务已收尾退场:再点取消必然失败 —— 项目已删则 404,残留项目无任务则 409
    assert client.post(f"/api/v1/projects/{project_id}/cancel").status_code in (404, 409)
    assert all(
        p["id"] != project_id
        for p in client.get("/api/v1/projects").json()["items"]
    ), "删除后项目不应再出现在列表"


async def test_delete_project_clears_rows_checkpoints_and_files(db) -> None:
    """直调删除端点(与 HTTP 测试同 loop 安全):业务行、checkpoint、静态文件全清。"""
    from sqlalchemy import func, text

    from app.api.projects import delete_project as delete_route
    from app.config import get_settings
    from app.db.session import async_session_maker
    from app.models.agent_run import AgentRun
    from app.models.message import Message
    from app.models.project import ComicProject
    from app.orchestration.persistence import open_checkpointer
    from app.services.media import disk_path, store_asset

    async with async_session_maker() as session:
        project = ComicProject(topic="待删除项目")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        run = AgentRun(project_id=project.id, status="succeeded")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        session.add(
            Message(project_id=project.id, run_id=run.id, agent="plan_outline", content="大纲完成")
        )
        asset_url = store_asset(b"fake-png-bytes", subdir="images", suffix=".png")
        assert disk_path(asset_url) is not None
        project.set_content("character_images", [{"character_name": "X", "url": asset_url}])
        # 成片 + 配音轨都是活引用,删除时必须一并清理(voiceover_url 曾漏删成孤儿)
        video_url = store_asset(b"fake-video-bytes", subdir="videos", suffix=".mp4")
        voice_url = store_asset(b"fake-voice-bytes", subdir="audio", suffix=".mp3")
        assert disk_path(video_url) is not None and disk_path(voice_url) is not None
        project.set_content("video", {"url": video_url, "voiceover_url": voice_url})
        session.add(project)
        await session.commit()

    # 造一条该 run 的 checkpoint(AsyncSqliteSaver 上下文会自动建表)
    async with open_checkpointer(get_settings().database_url), async_session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO checkpoints"
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,"
                " type, checkpoint, metadata) VALUES (:tid, :ns, :cid, NULL, 'channel', :ckpt, :meta)"
            ),
            {
                "tid": f"agent-run-{run.id}",
                "ns": "",
                "cid": "cid-test",
                "ckpt": b"{}",
                "meta": b"{}",
            },
        )
        await session.commit()

    result = await delete_route(project.id)  # 直调路由函数(同事件循环)
    assert result["deleted"] is True

    async with async_session_maker() as session:
        assert await session.get(ComicProject, project.id) is None, "项目行已删"
        n_runs = (await session.execute(
            select(func.count()).select_from(AgentRun).where(AgentRun.project_id == project.id)
        )).scalar_one()
        assert n_runs == 0, "该项目的 run 全删"
        n_msgs = (await session.execute(
            select(func.count()).select_from(Message).where(Message.project_id == project.id)
        )).scalar_one()
        assert n_msgs == 0, "该项目的消息全删"
        n_ckpt = (await session.execute(
            text("SELECT COUNT(*) FROM checkpoints WHERE thread_id = :tid"),
            {"tid": f"agent-run-{run.id}"},
        )).scalar_one()
        assert n_ckpt == 0, "该 run 的 checkpoint 行已清"
    assert disk_path(asset_url) is None, "引用的静态文件已从磁盘删除"
    assert disk_path(video_url) is None, "成片文件已从磁盘删除"
    assert disk_path(voice_url) is None, "配音音轨文件已从磁盘删除(曾漏删成孤儿)"


# ---------------------------------------------------------------------------
# 审查修复回归(P0/P1 批次)
# ---------------------------------------------------------------------------

def test_failed_run_marks_project_failed(client, monkeypatch) -> None:
    """run 失败 → project.status 必须如实落 failed(否则列表永久"生成中")。"""
    from app import runner as runner_module

    async def failing_drive(*args, **kwargs):
        raise RuntimeError("boom-test")

    # 打真实 runner 内部:让真正的状态机收尾逻辑执行,只让图执行失败
    monkeypatch.setattr(runner_module, "drive_graph_until_idle", failing_drive)
    project_id = client.post("/api/v1/projects", json={"topic": _TOPIC}).json()["id"]
    assert client.post(f"/api/v1/projects/{project_id}/generate", json={}).status_code == 200
    _wait_run_finished(client, project_id, expect="failed")
    view = client.get(f"/api/v1/projects/{project_id}").json()
    assert view["status"] == "failed", "run 失败后项目状态必须为 failed"
    assert "boom-test" in (view["latest_run"]["error"] or "")


async def test_review_target_index_passthrough(db, monkeypatch) -> None:
    """review 分诊点名的角色下标必须透传(曾被子阶段序号覆盖,定点重画失效)。"""
    from app.models.agent_run import AgentRun
    from app.models.project import ComicProject
    from app.orchestration.nodes import review_node

    async with async_session_maker() as session:
        project = ComicProject(topic="评审角色", content={"characters": [{"name": "甲"}, {"name": "乙"}]})
        session.add(project)
        await session.commit()
        await session.refresh(project)
        run = AgentRun(project_id=project.id, status="running", current_stage="character_images_approval")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        # 已有下游产物,review 清场应把它们删掉
        project.set_content("shot_images", [{"shot_index": 1, "url": "/static/images/x.png"}])
        session.add(project)
        await session.commit()
        run_id = run.id
        project_id = project.id

    from app import agents as agents_module

    async def fake_decide(project, feedback, settings, *, allowed):
        return "render_characters", 1  # 点名 1 号角色

    monkeypatch.setattr(agents_module, "decide_review_target", fake_decide)

    outcome = await review_node({
        "run_id": run_id,
        "approval_feedback": "乙角色的手表画错了",
        "current_stage": "character_images_approval",
    })
    assert outcome["review_target_index"] == 1, "角色下标必须原样透传"
    assert outcome["route_stage"] == "render_characters"
    assert "compose" in outcome["force_rerun"]
    async with async_session_maker() as session:
        project = await session.get(ComicProject, project_id)
        assert project.get_content("shot_images") is None, "下游产物应被清空"


async def test_confirm_rejected_while_production_stage(db) -> None:
    """run 在生产阶段(非确认门)时 confirm → 409 且不落消息(曾吞陈旧意见)。"""
    from fastapi import HTTPException
    from sqlalchemy import func

    from app.api.projects import ConfirmRequest, confirm
    from app.gateway import gateway
    from app.models.agent_run import AgentRun
    from app.models.message import Message
    from app.models.project import ComicProject

    async with async_session_maker() as session:
        project = ComicProject(topic="生产中确认")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        run = AgentRun(project_id=project.id, status="running", current_stage="plan_shots")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id, project_id = run.id, project.id
    await gateway.register(run_id)  # 模拟真实 run 注册着信号(否则早就 409)

    with pytest.raises(HTTPException) as ei:
        await confirm(project_id, ConfirmRequest(run_id=run_id, feedback="陈旧意见"))
    assert ei.value.status_code == 409
    assert "执行中" in ei.value.detail

    async with async_session_maker() as session:
        n = (await session.execute(
            select(func.count()).select_from(Message).where(Message.project_id == project_id)
        )).scalar_one()
        assert n == 0, "被拒的确认不得落消息"
    await gateway.remove(run_id)


async def test_stale_confirm_rejected_after_decision_consumed(db) -> None:
    """P1-1 回归:裁决被消费后(run 离开门进入回炉/下一生产阶段,DB 阶段名
    仍停留在 _approval 的滞后窗口),第二次 confirm 必须 409 ——
    陈旧裁决不得被下一道门"先到先得"吞掉,user 消息也不得重复落。"""
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy import func

    from app.api.projects import ConfirmRequest, confirm
    from app.db.session import async_session_maker
    from app.gateway import gateway
    from app.models.agent_run import AgentRun
    from app.models.message import Message
    from app.models.project import ComicProject

    async with async_session_maker() as session:
        project = ComicProject(topic="陈旧点击")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        run = AgentRun(project_id=project.id, status="running", current_stage="outline_approval")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id, project_id = run.id, project.id
    await gateway.register(run_id)

    async def parked_run() -> None:
        """模拟 run 停在门:消费一次裁决后"继续跑"(不更新 DB 阶段 → 滞后窗口)。"""
        await gateway.wait_confirm(run_id, timeout=5.0)

    # 第一次裁决正常走通(accept → 落消息 → release → 门消费)
    task = asyncio.create_task(parked_run())
    await asyncio.sleep(0.05)  # 等 wait_confirm 进入等待
    first = await confirm(project_id, ConfirmRequest(run_id=run_id, feedback="改标题"))
    assert first["ack"] is True
    await task

    # 滞后窗口内第二次点击:状态机拒绝(409),不得落第二条 user 消息
    with pytest.raises(HTTPException) as ei:
        await confirm(project_id, ConfirmRequest(run_id=run_id, feedback="再改一次"))
    assert ei.value.status_code == 409
    async with async_session_maker() as session:
        n = (await session.execute(
            select(func.count()).select_from(Message).where(
                Message.run_id == run_id, Message.role == "user"
            )
        )).scalar_one()
        assert n == 1, "被拒的陈旧裁决不得再落消息"
    await gateway.remove(run_id)


async def test_interrupted_actions_follow_checkpoint_existence(db) -> None:
    """P1-3 回归:interrupted run 的动作推导与 checkpoint 存在性同源 ——
    无断点(崩溃于起步阶段)→ 给"重新生成"而不是必 409 的"恢复续跑";
    有断点 → 只给 resume。"""
    from sqlalchemy import text

    from app.config import get_settings
    from app.db.session import async_session_maker
    from app.models.agent_run import AgentRun
    from app.models.project import ComicProject
    from app.orchestration.persistence import open_checkpointer
    from app.services.project_view import project_view

    async with async_session_maker() as session:
        project = ComicProject(topic="起步阶段崩溃")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        run = AgentRun(project_id=project.id, status="interrupted", current_stage="plan_outline")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        pid = project.id

    async with async_session_maker() as session:
        project = await session.get(ComicProject, pid)
        view = await project_view(session, project)
        a = view["actions"]
        assert a["can_generate"] is True, "无断点应给重新生成"
        assert a["can_resume"] is False, "无断点 resume 必 409,不能给"

    # 造一条该 run 的 checkpoint 后翻转
    async with open_checkpointer(get_settings().database_url), async_session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO checkpoints"
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,"
                " type, checkpoint, metadata) VALUES (:tid, :ns, :cid, NULL, 'channel', :ckpt, :meta)"
            ),
            {
                "tid": f"agent-run-{run.id}",
                "ns": "",
                "cid": "cid-iv",
                "ckpt": b"{}",
                "meta": b"{}",
            },
        )
        await session.commit()

    async with async_session_maker() as session:
        project = await session.get(ComicProject, pid)
        view = await project_view(session, project)
        a = view["actions"]
        assert a["can_generate"] is False, "有断点应引导续跑而非重开"
        assert a["can_resume"] is True
async def test_node_message_replay_is_idempotent(db) -> None:
    """消息幂等扩展:重放节点(run 崩溃于'消息已落、checkpoint 未落'的瞬间)
    重复插同一行 → 幂等键 (run, agent, role, content) 只落一次;
    文案变化 = 新一轮信号,正常新增。"""
    from app.db.session import async_session_maker
    from app.models.agent_run import AgentRun
    from app.models.message import Message
    from app.models.project import ComicProject
    from app.orchestration.nodes import _push_message_once

    async with async_session_maker() as session:
        project = ComicProject(topic="消息幂等")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        run = AgentRun(project_id=project.id, status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        project_id, run_id = project.id, run.id

    async with async_session_maker() as session:
        # 同一次执行 + 崩溃重放:同 run 同 agent 同文案同超步代次
        await _push_message_once(
            session, project_id=project_id, run_id=run_id, agent="compose",
            role="assistant", content="跳过成片合成:x", superstep=3,
        )
        await _push_message_once(
            session, project_id=project_id, run_id=run_id, agent="compose",
            role="assistant", content="跳过成片合成:x", superstep=3,
        )
        await session.commit()
    async with async_session_maker() as session:
        rows = (
            (await session.execute(select(Message).where(Message.run_id == run_id))).scalars().all()
        )
        assert len(rows) == 1, "同代次重放不得重复插行"
        # 新一轮合法执行(回炉):stage_history 已增长 → 同文案也正常新增
        await _push_message_once(
            session, project_id=project_id, run_id=run_id, agent="compose",
            role="assistant", content="跳过成片合成:x", superstep=7,
        )
        await session.commit()
        rows = (
            (await session.execute(select(Message).where(Message.run_id == run_id))).scalars().all()
        )
        assert len(rows) == 2



async def test_config_invalid_value_not_persisted(db) -> None:
    """非法配置值先校验后落库:拒绝保存且不毒化 config_item。"""
    from app.config import get_settings
    from app.models.config_item import ConfigItem
    from app.services.config_service import ConfigService

    async with async_session_maker() as session:
        service = ConfigService(session)
        await service.ensure_initialized()
        with pytest.raises(ValueError):
            await service.save({"video_max_shots": "abc"})
        row = await session.get(ConfigItem, "video_max_shots")
        stored = row.value if row else None
    assert stored in (None, "0", ""), f"非法值不得落库,实际 {stored!r}"
    assert get_settings().video_max_shots == 0

    # 历史毒行(旧版本可能已写入)→ apply_stored 跳过,绝不打死启动
    async with async_session_maker() as session:
        row = await session.get(ConfigItem, "video_max_shots")
        if row is None:
            session.add(ConfigItem(key="video_max_shots", value="abc"))
        else:
            row.value = "abc"
        await session.commit()
        service = ConfigService(session)
        await service.apply_stored()  # 不应抛
    assert get_settings().video_max_shots == 0


async def test_resume_without_checkpoint_guides_to_regenerate(db) -> None:
    """interrupted 但无 checkpoint(起步阶段崩溃)→ resume 拒绝并引导,generate 放行重开。"""
    from fastapi import HTTPException

    from app.api.projects import GenerateRequest, generate, resume
    from app.models.agent_run import AgentRun
    from app.models.project import ComicProject

    async with async_session_maker() as session:
        project = ComicProject(topic="无断点续跑")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        run = AgentRun(project_id=project.id, status="interrupted", current_stage="plan_outline")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        project_id, run_id = project.id, run.id

    with pytest.raises(HTTPException) as ei:
        await resume(project_id)
    assert ei.value.status_code == 409
    assert "断点" in ei.value.detail

    # 无断点 → 允许整轮重开(不再被 interrupted 卡死)
    result = await generate(project_id, GenerateRequest(auto_mode=True))
    assert result["run_id"] != run_id
    await _cancel_async(project_id)


async def _cancel_async(project_id: int) -> None:
    from fastapi import HTTPException

    from app.api.projects import cancel as cancel_route

    try:
        await cancel_route(project_id)
    except HTTPException:
        pass  # 任务可能已自然结束,无任务可取消(409 属正常)
