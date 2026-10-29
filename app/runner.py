"""执行器:把"一条 AgentRun"推成一次完整图执行。

生成入口(resume 同)在 api.py 里 create_task 调用 run_pipeline;本模块负责:
1. 编译图(AsyncSqliteSaver,checkpoint 落盘 → 断点续跑的地基);
2. 组装初始 state(全新生成)或 None(从 checkpoint 续跑);
3. 注入 on_interrupt:挂到 gateway 等人工 confirm;
4. 收尾:run 状态机(succeeded/failed/cancelled/interrupted)。

resume 语义(经探针验证):
- 进程崩溃时 checkpoint 已存在 interrupt 处;
- resume 后 ainvoke(None) 会原样返回挂起的 interrupt(重新停在门等人确认);
- Command(resume=...) 从 checkpoint 继续,已完成阶段的 lineage 完整保留,
  不会从头重跑。
"""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.db.session import async_session_maker
from app.gateway import gateway
from app.models import agent_run as ar
from app.models.project import ComicProject
from app.orchestration.driver import drive_graph_until_idle, interrupt_value
from app.orchestration.graph import build_phase2_graph
from app.orchestration.persistence import open_checkpointer

logger = logging.getLogger(__name__)


def _thread_id_for_run(run_id: int) -> str:
    return f"agent-run-{run_id}"


# 显式取消意图:端点先登记再 task.cancel()(见 api/projects.py 的 cancel/delete)。
# 对 run 而言 task.cancel() 与进程停机无差别,收尾靠意图区分语义:
# - 有意图 = 用户取消 → cancelled + 项目回 draft(放弃本次,可重新开始);
# - 无意图 = 服务停机/异常收尾 → interrupted + 项目 interrupted(可 resume 续跑,
#   与崩溃后启动清扫的语义对齐 —— 重启不该让用户丢整轮产物)。
_cancel_intents: set[int] = set()


def mark_cancel_intent(run_id: int) -> None:
    _cancel_intents.add(run_id)


def clear_cancel_intent(run_id: int) -> bool:
    """消费意图并返回是否存在。"""
    had = run_id in _cancel_intents
    _cancel_intents.discard(run_id)
    return had


async def run_pipeline(run_id: int, *, resume: bool = False) -> None:
    """执行一条 run 直到 END(或失败/取消);resume=True 从 checkpoint 续跑。"""
    settings = get_settings()
    project_id: int | None = None  # 起步阶段(首个 session.get 前)就失败时,收尾处理器不能引用未绑定变量
    try:
        await gateway.register(run_id)

        async with async_session_maker() as session:
            run = await session.get(ar.AgentRun, run_id)
            if run is None:
                raise RuntimeError(f"run {run_id} not found")
            project_id = run.project_id
            auto_mode = run.auto_mode

        # 全新生成带初始 state;resume 传 None → 图从 checkpoint 继续。
        # current_stage 决定从哪个生产阶段切入(补成片 = run 以 compose 起步)。
        initial: dict | None = (
            None
            if resume
            else {
                "project_id": project_id,
                "run_id": run_id,
                "auto_mode": auto_mode,
                "current_stage": run.current_stage or "plan_outline",
            }
        )
        config: dict[str, dict[str, str]] = {
            "configurable": {"thread_id": _thread_id_for_run(run_id)}
        }

        async def on_interrupt(interrupt_item: object) -> str:
            value = interrupt_value(interrupt_item)
            gate = value.get("gate") if isinstance(value, dict) else "?"
            logger.info("[run=%s] 停在审批门 %s,等待人工确认…", run_id, gate)
            return await gateway.wait_confirm(run_id, timeout=settings.confirm_timeout_s)

        async with open_checkpointer(settings.database_url) as saver:
            compiled = build_phase2_graph().compile(checkpointer=saver)
            final_state = await drive_graph_until_idle(
                compiled,
                initial_payload=initial,
                graph_config=config,
                on_interrupt=on_interrupt,
                run_id=run_id,
            )

        async with async_session_maker() as session:
            run = await session.get(ar.AgentRun, run_id)
            if run is not None:
                run.status = "succeeded"
                run.progress = 1.0
                run.current_stage = final_state.get("current_stage")
                session.add(run)
                if project_id is not None:
                    project = await session.get(ComicProject, project_id)
                    if project is not None:
                        project.status = "succeeded"
                        session.add(project)
                await session.commit()
    except asyncio.CancelledError:
        explicit_cancel = clear_cancel_intent(run_id)
        async with async_session_maker() as session:
            run = await session.get(ar.AgentRun, run_id)
            if run is not None:
                run.status = "cancelled" if explicit_cancel else "interrupted"
                run.error = (
                    "Cancelled" if explicit_cancel else "服务停止,run 已中断,可『恢复续跑』续跑"
                )
                session.add(run)
                if project_id is not None:
                    project = await session.get(ComicProject, project_id)
                    if project is not None:
                        # 用户取消 = 放弃本次,回草稿;停机中断 = 与崩溃清扫一致,可续跑
                        project.status = "draft" if explicit_cancel else "interrupted"
                        session.add(project)
                await session.commit()
        raise
    except Exception as exc:
        clear_cancel_intent(run_id)
        logger.exception("[run=%s] pipeline failed", run_id)
        async with async_session_maker() as session:
            run = await session.get(ar.AgentRun, run_id)
            if run is not None:
                run.status = "failed"
                # 用户可读文案给前端;开发细节留在日志(见 app/errors.py)
                from app.errors import user_facing

                run.error = user_facing(exc)[:500]
                session.add(run)
                if project_id is not None:
                    project = await session.get(ComicProject, project_id)
                    if project is not None:
                        project.status = "failed"  # 失败也要让列表/徽章如实呈现
                        session.add(project)
                await session.commit()
    finally:
        clear_cancel_intent(run_id)
        await gateway.remove(run_id)
