"""测试共享 fixture。

约定:测试直接 Settings() 不读仓库 .env;这里在导入 app 前用
环境变量钉死测试配置(独立 DB 文件 + fake provider),保证测试与真实环境隔离。
"""
from __future__ import annotations

import os
from pathlib import Path

_TEST_DB = Path(__file__).resolve().parents[1] / ".test-comic.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB.as_posix()}"
os.environ["TEXT_PROVIDER"] = "fake"
os.environ["ENVIRONMENT"] = "dev"

# 仓库里可能存在真实 .env —— 只中和 .env 里真实出现的键:给它们盖上
# "等于 Settings 默认值/空值"的环境变量(pydantic-settings 优先级:进程 env > .env),
# 保证测试只见 fake 基线,真实密钥绝不进入测试进程(包括 config_item 种子)。
from app.config import Settings as _SettingsCfg  # noqa: E402(读类级字段,不实例化)

_FIELD_BY_ENV = {name.upper(): name for name in _SettingsCfg.model_fields}
_env_file = Path(__file__).resolve().parents[1] / ".env"
_env_lines = _env_file.read_text(encoding="utf-8").splitlines() if _env_file.exists() else []
for _line in _env_lines:
    _raw = _line.strip()
    if not _raw or _raw.startswith("#") or "=" not in _raw:
        continue
    _env_key = _raw.split("=", 1)[0].strip()
    _field = _FIELD_BY_ENV.get(_env_key)
    if _field is None:
        continue  # 非 Settings 键(Settings extra=ignore,本就无影响)
    _annotation = str(_SettingsCfg.model_fields[_field].annotation)
    if _env_key in ("DATABASE_URL", "TEXT_PROVIDER", "ENVIRONMENT"):
        continue  # 上面已显式钉死,优先
    _default = _SettingsCfg.model_fields[_field].default
    if "bool" in _annotation:
        _neutral = "1" if _default else "0"
    elif "str" in _annotation:
        # 有非空默认值的(provider/ratio 等)用默认值;可选/密钥类用空串
        _neutral = str(_default) if _default not in (None, "") else ""
    else:  # int/float:用默认值或 0(provider 已被钉成 fake,数值无行为影响)
        _neutral = str(_default) if _default is not None else "0"
    os.environ[_env_key] = _neutral

if _TEST_DB.exists():  # 清掉上一次运行的残留数据,保证计数类断言稳定
    _TEST_DB.unlink()

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from app.db.session import async_session_maker, init_db
from app.models import agent_run as ar
from app.models.config_item import ConfigItem
from app.models.message import Message
from app.models.project import ComicProject
from app.models.library_character import LibraryCharacter

_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


@pytest_asyncio.fixture
async def db():
    """干净库:每个测试用例开始时建表(幂等),结束后清空全部业务表。

    注意:必须连 LangGraph checkpoint 表一起清 —— SQLite 删除行后自增 ID
    会复用,不清 checkpoint 会导致后一个测试的 run 复用前一个测试残留的
    thread_id,resume 加载到别人的旧状态。
    """
    await init_db()
    yield
    async with async_session_maker() as session:
        for model in (Message, ar.AgentRun, ComicProject, ConfigItem, LibraryCharacter):
            for row in (await session.execute(select(model))).scalars().all():
                await session.delete(row)
        for table in _CHECKPOINT_TABLES:
            try:
                await session.execute(text(f"DELETE FROM {table}"))
            except OperationalError:
                # checkpoint 表尚未创建(该测试没用过持久化 saver),无需清理
                continue
        await session.commit()


@pytest.fixture
def client(db):
    """TestClient(上下文管理器):后台生成任务与请求共用同一事件循环。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest_asyncio.fixture
async def make_project(db):
    created: list[ComicProject] = []

    async def _make(topic: str = "咖啡师与时间旅行者", style: str = "日系动漫") -> ComicProject:
        async with async_session_maker() as session:
            project = ComicProject(topic=topic, style=style)
            session.add(project)
            await session.commit()
            await session.refresh(project)
            created.append(project)
            return project

    return _make


@pytest_asyncio.fixture
async def make_run(db):
    created: list[ar.AgentRun] = []

    async def _make(project: ComicProject, *, auto_mode: bool = True) -> ar.AgentRun:
        async with async_session_maker() as session:
            run = ar.AgentRun(
                project_id=project.id,
                status="running",
                current_stage="plan_outline",
                auto_mode=auto_mode,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            created.append(run)
            return run

    return _make
