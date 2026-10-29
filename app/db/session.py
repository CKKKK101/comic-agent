"""异步引擎与会话工厂 + 启动初始化。

默认 SQLite 零配置可跑;engine 由 DATABASE_URL 驱动,换 Postgres 只需改配置
(postgresql+asyncpg://...)并加 asyncpg 依赖,业务代码零改动。
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel, select

from app.config import get_settings

# 确保模型全部注册进 SQLModel.metadata(否则 create_all 会漏表)
from app.models import agent_run, config_item, message, project  # noqa: F401
from app.models.agent_run import AgentRun
from app.models.project import ComicProject

logger = logging.getLogger("comic-agent.init_db")


def _build_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, echo=False, pool_pre_ping=True)


engine: AsyncEngine = _build_engine()
async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    """建表(缺失的表)+ 轻量列迁移 + 启动清扫 + 运营配置种子/回灌。"""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    await _migrate_columns()
    swept = await sweep_orphaned_runs()
    # 配置:首次启动把当前 Settings 种进 config_item;之后以 DB/面板值为准
    from app.services.config_service import ConfigService

    async with async_session_maker() as session:
        service = ConfigService(session)
        await service.ensure_initialized()
        await service.apply_stored()
    logger.info("init_db done (swept %s orphaned runs, config synced)", swept)


async def _migrate_columns() -> None:
    """轻量列迁移:老库文件缺新列时补齐(SQLite ALTER 幂等;已存在则跳过)。

    create_all 只补缺失的表,不补已有表的列 —— 消息幂等需要 message.superstep,
    老库文件(comic-agent.db/.test-comic.db)首次启动会缺列,查询直接报
    "no such column"。Postgres 下同语句也成立,失败仅告警不阻断启动。
    """
    statements = ("ALTER TABLE message ADD COLUMN superstep INTEGER",)
    async with engine.begin() as conn:
        for statement in statements:
            try:
                await conn.execute(text(statement))
                logger.info("migrated: %s", statement)
            except OperationalError:
                logger.info("column already present (skip): %s", statement.split("ADD")[-1])
            except Exception:
                logger.exception("column migration failed (non-fatal): %s", statement)


async def sweep_orphaned_runs() -> int:
    """启动清扫:上次进程崩溃遗留的 running run → interrupted(可 resume)。

    进程重启后,内存里没有任何任务在跑这些 run,但它们的状态还停在
    running(在审批门等待或执行中途)。把它们标记为 interrupted,
    由 /resume 端点从 LangGraph checkpoint 续跑。
    """
    async with async_session_maker() as session:
        rows = (
            (await session.execute(select(AgentRun).where(AgentRun.status == "running"))).scalars().all()
        )
        for run in rows:
            run.status = "interrupted"
            run.error = "Service restarted;run 已中断,可调用 resume 续跑"
            session.add(run)
            project = await session.get(ComicProject, run.project_id)
            if project is not None:
                project.status = "interrupted"  # 与 run 状态机对齐,列表不再误报"生成中"
                session.add(project)
        await session.commit()
        return len(rows)


async def checkpoint_exists(run_id: int) -> bool:
    """该 run 是否已有可续的 LangGraph checkpoint(表缺失/查询失败 → False)。

    供 generate/resume 的分流(generate 放行无断点的 interrupted run)与
    project_view 的动作推导(无断点 → 给"重新生成"不给"恢复续跑")共用。
    """
    try:
        async with async_session_maker() as session:
            count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM checkpoints WHERE thread_id = :tid"),
                    {"tid": f"agent-run-{run_id}"},
                )
            ).scalar_one()
            return int(count or 0) > 0
    except OperationalError:
        return False
