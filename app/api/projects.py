"""项目资源端点(薄壳):CRUD + 生成/确认/取消/恢复。

- 执行真相在 DB(AgentRun 行),进度用轮询读行(实时推送见 Day10 前端 WS 计划);
- 读模型组装与"可执行动作"推导在 services/project_view.py;
- 并发模型:每项目同时只允许一条 running run(409 挡住)。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, delete, text
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from app.config import get_settings
from app.db.session import async_session_maker, checkpoint_exists
from app.gateway import gateway
from app.models import agent_run as ar
from app.models.message import Message
from app.models.project import ComicProject
from app.runner import clear_cancel_intent, mark_cancel_intent, run_pipeline
from app.services.config_service import blocking_issues
from app.services.media import delete_static_asset
from app.services.project_view import project_view

logger = logging.getLogger(__name__)
router = APIRouter()

_tasks: dict[int, asyncio.Task] = {}
# 每项目一把锁:generate/resume 的"查最新 run → 判态 → 落库"必须原子,
# 否则双击/双标签能同时通过检查,同项目跑出两条管线(互踩 content、双烧钱)
_locks: dict[int, asyncio.Lock] = {}


def _project_lock(project_id: int) -> asyncio.Lock:
    lock = _locks.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[project_id] = lock
    return lock


def _forget_task_done(project_id: int, task: asyncio.Task) -> None:
    """run 收尾回调:只弹自己那一条 —— 不校验归属会把后续 run 的条目弹掉,
    造成 cancel 假 409、delete 找不到任务直接与活 run 并发删行。"""
    if _tasks.get(project_id) is task:
        _tasks.pop(project_id, None)


class ProjectCreate(BaseModel):
    topic: str = Field(min_length=1, description="故事想法/一句话")
    style: str = "日系动漫"
    title: str = ""


class GenerateRequest(BaseModel):
    auto_mode: bool = Field(default=False, description="自动模式:审批门全部自动通过")
    start_stage: str | None = Field(
        default=None,
        description="从指定生产阶段起步(仅支持 compose:配置完善后补生成被跳过的成片)",
    )


class ConfirmRequest(BaseModel):
    run_id: int
    feedback: str = Field(default="", description="留空=通过;非空=修改意见")


# ---------------------------------------------------------------------------
# 项目
# ---------------------------------------------------------------------------

@router.post("/projects", status_code=201)
async def create_project(body: ProjectCreate) -> dict:
    project = ComicProject(topic=body.topic, style=body.style, title=body.title)
    async with async_session_maker() as session:
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return {"id": project.id, "topic": project.topic, "style": project.style}


@router.get("/projects")
async def list_projects() -> dict:
    async with async_session_maker() as session:
        projects = (
            (await session.execute(select(ComicProject).order_by(ComicProject.id.desc()))).scalars().all()
        )
        return {"items": [await project_view(session, p) for p in projects]}


@router.get("/projects/{project_id}")
async def get_project(project_id: int) -> dict:
    async with async_session_maker() as session:
        project = await session.get(ComicProject, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        return await project_view(session, project)


# ---------------------------------------------------------------------------
# 删除(不可恢复;进行中的 run 先自动停止再清)
# ---------------------------------------------------------------------------

@router.delete("/projects/{project_id}")
async def delete_project(project_id: int) -> dict:
    """删除项目(不可恢复):先停掉进行中的 run,再清 项目行 + run/消息 + checkpoint + 静态文件。

    全程持项目锁:generate/resume 的"commit → create_task 注册"在锁内完成,
    删除若与注册间隙交错会留下"在已删项目上空跑的管线";同一把锁保证
    删除开始后不再有新的 run 诞生。进行中的 run 走 task.cancel() 并等它收尾:
    取消是协作式的,runner 收到 CancelledError 会先把状态落成 cancelled 再
    退出 —— 等它退干净才删行,否则后台任务与我们并发写同一批行
    (SQLite 写锁 / 半删态竞态)。超时(异常卡住的 run)则强制继续删:
    runner 每步都判 run 行是否存在,行没了它会安全空转(见 runner 的状态机收尾)。
    """
    async with _project_lock(project_id):
        task = _tasks.get(project_id)
        if task is not None and not task.done():
            # 先登记"显式取消"意图:runner 收尾按 cancelled 处理而非 interrupted
            async with async_session_maker() as session:
                latest = (
                    await session.execute(
                        select(ar.AgentRun)
                        .where(ar.AgentRun.project_id == project_id)
                        .order_by(ar.AgentRun.id.desc())
                        .limit(1)
                    )
                ).scalars().first()
                if latest is not None:
                    mark_cancel_intent(latest.id)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except (asyncio.CancelledError, TimeoutError):
                logger.warning("delete project=%s: 等待后台 run 收尾超时,强制删除", project_id)

        async with async_session_maker() as session:
            project = await session.get(ComicProject, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="项目不存在")

            # 行删除前先收集引用的资产 URL(content 删了就取不到了)
            asset_urls: list[str] = []
            for key in ("character_images", "shot_images"):
                for item in project.content.get(key) or []:
                    url = (item or {}).get("url")
                    if isinstance(url, str):
                        asset_urls.append(url)
            for item in project.content.get("reference_images") or []:
                asset_urls.append(item["url"])
            for item in project.content.get("cast") or []:
                if item.get("reference_url"):
                    asset_urls.append(item["reference_url"])
            video = project.content.get("video") or {}
            for key in ("url", "voiceover_url"):  # 成片 + 配音轨都是活引用
                url = video.get(key)
                if isinstance(url, str):
                    asset_urls.append(url)

            run_ids = (
                await session.execute(select(ar.AgentRun.id).where(ar.AgentRun.project_id == project_id))
            ).scalars().all()
            await session.execute(delete(Message).where(Message.project_id == project_id))
            await session.execute(delete(ar.AgentRun).where(ar.AgentRun.project_id == project_id))
            await session.delete(project)
            if run_ids:  # 同库的 LangGraph checkpoint 一并清(表可能从未建过,防御)
                thread_ids = [f"agent-run-{rid}" for rid in run_ids]
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    try:
                        stmt = text(f"DELETE FROM {table} WHERE thread_id IN :ids").bindparams(
                            bindparam("ids", expanding=True)
                        )
                        await session.execute(stmt, {"ids": thread_ids})
                    except OperationalError:
                        logger.warning("checkpoint 表 %s 不存在或不可用,清理跳过", table)
            await session.commit()

    for url in asset_urls:  # commit 后删磁盘文件;helper 不抛(尽力而为)
        delete_static_asset(url)
    logger.info("project %s deleted(runs=%d)", project_id, len(run_ids))
    return {"deleted": True, "project_id": project_id}


# ---------------------------------------------------------------------------
# 生成 / 确认 / 恢复 / 取消
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/generate")
async def generate(project_id: int, body: GenerateRequest) -> dict:
    if body.start_stage not in (None, "compose", "render_shots"):
        raise HTTPException(
            status_code=400,
            detail='start_stage 仅支持 "compose"(补生成成片)或 "render_shots"(复用已批准内容重渲分镜画面)',
        )

    # 预检:真实 provider 选了但没配全 → 409 引导去『设置』页(失败发生在用户能行动的地方)。
    # 视频不阻塞:缺视频配置照样跑文本+图像,成片阶段跳过并打 video_pending。
    issues = blocking_issues(get_settings())
    if issues:
        raise HTTPException(
            status_code=409,
            detail="存在未完成的外部服务配置,请到『设置』页完善后重试",
            headers={"X-Config-Issues": ",".join(sorted(issues))},
        )

    async with _project_lock(project_id), async_session_maker() as session:
        project = await session.get(ComicProject, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        latest = (
            await session.execute(
                select(ar.AgentRun)
                .where(ar.AgentRun.project_id == project_id)
                .order_by(ar.AgentRun.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if latest is not None and latest.status in ("queued", "running"):
            raise HTTPException(
                status_code=409,
                detail=f"项目已有进行中的 run({latest.id}),请先等它结束或取消",
            )
        if latest is not None and latest.status == "interrupted":
            if await checkpoint_exists(latest.id):
                raise HTTPException(
                    status_code=409, detail="上一次 run 中断未完成,请先 /resume 续跑"
                )
            # 中断发生在首个 checkpoint 之前(如起步阶段崩溃)→ 没有可续的
            # 断点,允许整轮重开,不再把项目困在"只能 resume"的死胡同
            logger.info("run %s interrupted 但无 checkpoint,按新 run 重开", latest.id)
            clear_cancel_intent(latest.id)
        start_stage = body.start_stage or "plan_outline"
        if start_stage == "compose":
            valid_images = {im.get("shot_index") for im in project.get_content("shot_images") or []
                            if not im.get("stale")}
            shots = project.get_content("shots") or []
            if any(s["index"] not in valid_images for s in shots):
                raise HTTPException(409, "请先生成全部分镜画面，更新后再合成成片")
        if body.start_stage is None and project.content:
            # 整轮重新生成 = 推倒重来:清空旧产物(旧分镜图/立绘/成片若残留,
            # 会在新一轮文本批准前误导用户);定点补跑(render_shots/compose)
            # 复用已批准内容,不清空。
            project.content = {
                key: value for key, value in project.content.items()
                if key in ("reference_images", "cast")
            }
        run = ar.AgentRun(
            project_id=project_id,
            status="running",
            current_stage=start_stage,
            progress=0.0,
            auto_mode=body.auto_mode,
        )
        session.add(run)
        project.status = "generating"
        session.add(project)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

        # create_task 必须在锁内:commit 后、任务注册前的空窗若被并发
        # delete/cancel 撞上,会留下"已删项目上空跑"或"取消后复活"的管线
        task = asyncio.create_task(run_pipeline(run_id), name=f"run-{run_id}")
        _tasks[project_id] = task
        task.add_done_callback(lambda t: _forget_task_done(project_id, t))
    return {"run_id": run_id, "status": "running", "auto_mode": body.auto_mode,
            "start_stage": start_stage}


@router.post("/projects/{project_id}/confirm")
async def confirm(project_id: int, body: ConfirmRequest) -> dict:
    """裁决当前审批门。裁决必须绑定"run 正停在的这一次门停留":

    - run 处于生产阶段(门与门之间)时拒绝 —— 否则残留确认条上的点击会带着
      陈旧意见,被下一道门当"先到先得"信号吞掉,用户没见过的门被静默通过;
      该窗口比 current_stage 更宽:审批后进 review/回炉/下一生产阶段期间,
      DB 阶段名可能仍停留在旧门 —— gateway 内部状态机兜底(未在等待即拒);
    - 裁决落库(消息行 commit)后才 release 唤醒 —— 唤醒先于反馈可读,
      回炉的定向修改(生产节点查"最近一条 user 消息")会丢;
    - 消息行在裁决成功后落(先裁决后写,run 已结束的确认不再污染消息流)。
    """
    async with _project_lock(project_id), async_session_maker() as session:
        run = await session.get(ar.AgentRun, body.run_id)
        if run is None or run.project_id != project_id:
            raise HTTPException(status_code=404, detail="run 不存在")
        if run.status != "running":
            raise HTTPException(status_code=409, detail="该 run 当前不在等待确认")
        stage = run.current_stage or ""
        if not stage.endswith("_approval"):
            raise HTTPException(
                status_code=409,
                detail=f"run 正在执行中(当前阶段 {stage}),请等它停在确认门再确认",
            )
        if stage == "shot_images_approval" and not body.feedback.strip():
            project = await session.get(ComicProject, project_id)
            valid = {im.get("shot_index") for im in project.get_content("shot_images") or []
                     if not im.get("stale")}
            if any(s["index"] not in valid for s in project.get_content("shots") or []):
                raise HTTPException(409, "分镜已修改，请先重新生成对应画面再通过确认")
        if not gateway.accept(run.id, body.feedback):
            raise HTTPException(
                status_code=409,
                detail="run 未在等待确认(可能刚离开确认门或本次裁决已收到),请刷新后再试",
            )
        try:
            session.add(
                Message(
                    project_id=project_id,
                    run_id=run.id,
                    agent="user",
                    role="user",
                    content=body.feedback.strip() or "通过",
                )
            )
            await session.commit()
        except Exception:
            gateway.abort(run.id)  # 落库失败 → 撤回裁决,run 继续在门等待
            raise
        gateway.release(run.id)  # 反馈已可见,才唤醒 run 消费裁决

    return {"run_id": run.id, "ack": True}


@router.post("/projects/{project_id}/resume")
async def resume(project_id: int) -> dict:
    """续跑被中断(interrupted)的 run:从它的 LangGraph checkpoint 接着走。

    可续跑前提:进程崩溃后启动清扫把遗留 run 标成 interrupted
    (见 db/session.py sweep_orphaned_runs);且该 run 确实有 checkpoint ——
    崩溃于首个 checkpoint 之前时无断点可续,只能重新生成(generate 会放行)。
    cancel 掉的 run 不算可续跑 —— 取消=放弃,重新生成即可。
    """
    async with _project_lock(project_id), async_session_maker() as session:
        project = await session.get(ComicProject, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        latest = (
            await session.execute(
                select(ar.AgentRun)
                .where(ar.AgentRun.project_id == project_id)
                .order_by(ar.AgentRun.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if latest is None or latest.status != "interrupted":
            raise HTTPException(
                status_code=409,
                detail="没有可续跑的 run(仅进程中断遗留的 interrupted run 可 resume)",
            )
        if not await checkpoint_exists(latest.id):
            raise HTTPException(
                status_code=409,
                detail="该 run 没有执行断点(崩溃于起步阶段),无法续跑,请直接重新生成",
            )
        latest.status = "running"
        latest.error = None
        session.add(latest)
        project.status = "generating"
        session.add(project)
        await session.commit()
        run_id = latest.id

        # create_task 与 generate 同款:锁内注册,杜绝与 delete/cancel 的空窗竞态
        task = asyncio.create_task(run_pipeline(run_id, resume=True), name=f"run-{run_id}-resume")
        _tasks[project_id] = task
        task.add_done_callback(lambda t: _forget_task_done(project_id, t))
    return {"run_id": run_id, "resumed": True}


@router.post("/projects/{project_id}/cancel")
async def cancel(project_id: int) -> dict:
    """停止进行中的 run。持项目锁 + 先登记显式取消意图:

    - 持锁:与 generate/resume 的"commit→任务注册"互斥(见 delete_project);
    - 意图登记(runner.mark_cancel_intent):task.cancel() 对 run 而言与进程
      停机无差别,runner 靠意图区分"用户取消"(cancelled,回 draft)与
      "服务重启/异常收尾"(interrupted,可 resume 续跑)。
    """
    async with _project_lock(project_id):
        task = _tasks.get(project_id)
        if task is not None and not task.done():
            async with async_session_maker() as session:
                latest = (
                    await session.execute(
                        select(ar.AgentRun)
                        .where(ar.AgentRun.project_id == project_id)
                        .order_by(ar.AgentRun.id.desc())
                        .limit(1)
                    )
                ).scalars().first()
                if latest is not None:
                    mark_cancel_intent(latest.id)
            task.cancel()
            return {"cancelled": True}
        # 兜底:DB 里还挂着 running 但进程内没有任务(异常死锁态)→ 直接清理,
        # 否则 cancel/generate 会永久互相 409,只能等重启清扫
        async with async_session_maker() as session:
            project = await session.get(ComicProject, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="项目不存在")
            latest = (
                await session.execute(
                    select(ar.AgentRun)
                    .where(ar.AgentRun.project_id == project_id)
                    .order_by(ar.AgentRun.id.desc())
                    .limit(1)
                )
            ).scalars().first()
            if latest is None or latest.status not in ("running", "queued"):
                raise HTTPException(status_code=409, detail="没有运行中的任务")
            clear_cancel_intent(latest.id)
            latest.status = "cancelled"
            latest.error = "Stale running run cleaned up"
            session.add(latest)
            project.status = "draft"
            session.add(project)
            await session.commit()
            return {"cancelled": True, "stale": True}
