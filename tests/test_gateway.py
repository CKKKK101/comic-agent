"""gateway(人工确认信号)单测:两阶段裁决状态机。

契约:
- run 停在门等待(wait_confirm 已进入)后才接受裁决:accept → release → wait 消费;
- 裁决先落库(release 前的消息 commit)再唤醒 —— 本文件用 accept/release
  两步模拟 confirm 端点的"落库后 release"行为;
- 未在等待的 confirm 一律拒绝:门与门之间的陈旧点击不得被下一道门吞掉(P1-1 回归)。
"""
from __future__ import annotations

import asyncio

import pytest

from app.gateway import ConfirmGateway


async def _decide(gw: ConfirmGateway, run_id: int, feedback: str) -> bool:
    """模拟 confirm 端点:accept(裁决)→ 落库 → release(唤醒)。"""
    if not gw.accept(run_id, feedback):
        return False
    gw.release(run_id)
    return True


@pytest.mark.asyncio
async def test_confirm_wakes_waiting_run() -> None:
    gw = ConfirmGateway()
    await gw.register(1)

    async def user_confirms() -> None:
        await asyncio.sleep(0.01)
        assert await _decide(gw, 1, "角色再冷酷一点") is True

    task = asyncio.create_task(user_confirms())
    feedback = await gw.wait_confirm(1, timeout=5)
    await task
    assert feedback == "角色再冷酷一点"


@pytest.mark.asyncio
async def test_confirm_with_empty_feedback_means_approve() -> None:
    gw = ConfirmGateway()
    await gw.register(2)

    async def user_approves() -> None:
        await asyncio.sleep(0.01)
        assert await _decide(gw, 2, "") is True

    task = asyncio.create_task(user_approves())
    assert await gw.wait_confirm(2, timeout=5) == ""
    await task


@pytest.mark.asyncio
async def test_timeout_raises() -> None:
    gw = ConfirmGateway()
    await gw.register(3)
    with pytest.raises(TimeoutError):
        await gw.wait_confirm(3, timeout=0.05)


@pytest.mark.asyncio
async def test_confirm_unknown_run_returns_false() -> None:
    gw = ConfirmGateway()
    assert gw.accept(999, "x") is False


@pytest.mark.asyncio
async def test_remove_cleans_state() -> None:
    gw = ConfirmGateway()
    await gw.register(4)
    await gw.remove(4)
    assert gw.accept(4, "x") is False


@pytest.mark.asyncio
async def test_second_wait_blocks_until_new_confirm() -> None:
    """回归:run 连过两道门时,第二次 wait 不能被上一次的残留 set 直接放行。"""
    gw = ConfirmGateway()
    await gw.register(5)

    async def first_confirm() -> None:
        await asyncio.sleep(0.01)
        assert await _decide(gw, 5, "") is True

    t1 = asyncio.create_task(first_confirm())
    assert await gw.wait_confirm(5, timeout=5) == ""
    await t1

    # 第二道门:未确认前必须阻塞,而不是立刻返回
    with pytest.raises(TimeoutError):
        await gw.wait_confirm(5, timeout=0.05)


@pytest.mark.asyncio
async def test_accept_rejected_when_run_not_waiting() -> None:
    """P1-1 回归:run 没停在门时 confirm 一律拒绝 ——
    陈旧/早到裁决不能再"先到先得"被下一道门吞掉(用户没见过的门被静默通过)。"""
    gw = ConfirmGateway()
    await gw.register(6)

    # 还没开始等(wait_confirm 未进入):拒绝
    assert gw.accept(6, "早到点击") is False

    # 停到第一道门:正常消费一次
    async def first() -> None:
        await asyncio.sleep(0.01)
        assert await _decide(gw, 6, "ok") is True

    t = asyncio.create_task(first())
    assert await gw.wait_confirm(6, timeout=5) == "ok"
    await t

    # 裁决已消费、run 尚未停到下一道门(门间/回炉窗口):陈旧点击 → 拒绝
    assert gw.accept(6, "重复点击1") is False
    assert gw.accept(6, "重复点击2") is False
    # run 到下一道门重新等 → 新裁决正常
    async def second() -> None:
        await asyncio.sleep(0.01)
        assert await _decide(gw, 6, "第二道门裁决") is True

    t2 = asyncio.create_task(second())
    assert await gw.wait_confirm(6, timeout=5) == "第二道门裁决"
    await t2


@pytest.mark.asyncio
async def test_double_confirm_same_stay_rejected_and_abort_restores() -> None:
    """同一停留内第二次点击(双击)被拒;abort(落库失败)撤回裁决回到 waiting。"""
    gw = ConfirmGateway()
    await gw.register(7)

    async def confirm_twice() -> None:
        await asyncio.sleep(0.01)
        assert await _decide(gw, 7, "第一次") is True
        assert gw.accept(7, "双击第二次") is False  # 已 decided,拒绝

    t = asyncio.create_task(confirm_twice())
    assert await gw.wait_confirm(7, timeout=5) == "第一次"
    await t

    # abort 场景:accept 后落库失败 → 撤回裁决 → 同一停留可重新确认
    async def abort_and_retry() -> None:
        await asyncio.sleep(0.01)
        assert gw.accept(7, "将撤回的裁决") is True
        gw.abort(7)
        assert await _decide(gw, 7, "重试成功") is True

    t2 = asyncio.create_task(abort_and_retry())
    assert await gw.wait_confirm(7, timeout=5) == "重试成功"
    await t2
