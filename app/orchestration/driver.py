"""LangGraph 执行驱动器:interrupt/resume 循环。

只管"怎么跑":ainvoke → 有 __interrupt__ 就把裁决交给 on_interrupt 回调 →
Command(resume=...) 续跑,直到图自然走到 END。
审批策略(等多久、要不要自动过)由调用方通过 on_interrupt 注入 —— HITL 策略
可注入、可测试(driver 对 Redis/DB/人工一无所知)。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.types import Command

logger = logging.getLogger(__name__)

InterruptHandler = Callable[[Any], Awaitable[Any]]


def interrupt_value(item: Any) -> Any:
    """LangGraph Interrupt 对象 → 我们在 interrupt() 里塞的 payload。"""
    return getattr(item, "value", None)


async def drive_graph_until_idle(
    compiled_graph: Any,
    *,
    initial_payload: Any,
    graph_config: dict[str, Any],
    on_interrupt: InterruptHandler,
    run_id: int | None = None,
) -> dict[str, Any]:
    payload: Any = initial_payload
    last_state: dict[str, Any] = {}

    while True:
        result = await compiled_graph.ainvoke(payload, graph_config)
        last_state = result if isinstance(result, dict) else {}
        interrupts = last_state.get("__interrupt__") or []
        if not interrupts:
            break

        value = interrupt_value(interrupts[0])
        logger.info("[graph-driver] run=%s interrupt value=%r", run_id, value)
        resume_payload = await on_interrupt(interrupts[0])
        payload = Command(resume=resume_payload)

    return last_state
