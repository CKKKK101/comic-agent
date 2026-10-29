"""checkpoint 持久化:把 LangGraph 状态落盘 SQLite,支撑进程重启后的断点续跑。

thread_id(agent-run-{run_id})是把"业务 run"与"图执行线"绑定的钥匙:
- 生成与 resume 用同一个 thread_id,图才知道接着上次的 checkpoint 走;
- checkpoint 与业务表同库(SQLite),一个文件即全量备份;
- 换 Postgres 时替换为 langgraph-checkpoint-postgres(连接串逻辑不变)。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


def sqlite_checkpointer_path(database_url: str) -> str | None:
    """从 SQLAlchemy 连接串提取 SQLite 文件路径;非 SQLite 返回 None。"""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if database_url.startswith(prefix):
            return database_url[len(prefix) :]
    return None


@asynccontextmanager
async def open_checkpointer(database_url: str) -> AsyncIterator[Any]:
    path = sqlite_checkpointer_path(database_url)
    if path is None:
        raise RuntimeError(f"当前数据库暂不支持 checkpoint 持久化: {database_url}")
    # from_conn_string 进入上下文时会自动建 checkpoint 表(setup)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        yield saver
