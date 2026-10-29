"""人工确认信号网关。

进程内 asyncio.Event 实现"run 在审批门挂起 → 用户 confirm → 唤醒":
- accept(run_id, feedback):裁决必须打在 run"正在某次门停留上等待"时,否则拒绝;
- release(run_id):裁决落库(消息行已 commit)后才唤醒 —— 唤醒先于反馈可读,
  回炉的定向修改会丢,故 release 由调用方在 DB 提交之后调;
- wait_confirm(run_id, timeout):run 的后台任务每次停在门时进入等待,消费裁决。

【裁决必须绑定"具体某一次停留"】run 的门与门之间(审批后进入 review/回炉/
下一生产阶段,DB current_stage 可能仍停留在旧门名)会持续数秒到数十秒。
若期间再收到一次 confirm(双击/双标签页),没有门归属的陈旧裁决会在 run
再次停在下一道门时被"先到先得"吞掉 —— 用户没见过的门被静默通过。
因此裁决走状态机,只有 run 正停在门等待(wait_confirm 已进入)才接受:
  waiting(在门等待) → accept → decided(已裁决,待落库) → release → waiting 被唤醒
  wait_confirm 消费裁决后回到 released;released 状态任何 confirm 一律拒绝(409)。

【单进程演示实现】多实例部署时 asyncio.Event 跨不了进程,需换 Redis pub/sub
(按 run 维度建 confirm channel,信号与 DB 解耦)或 DB 轮询;届时仅 gateway
内部实现变化,节点/驱动器代码零改动 —— 信号总线被刻意隔离。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ConfirmGateway:
    def __init__(self) -> None:
        self._events: dict[int, asyncio.Event] = {}
        self._feedback: dict[int, str] = {}
        # 每个 run 一台小状态机:waiting(在门等待)→ decided(已裁决待落库)→ released(已消费)
        # released 是默认态;accept 只认 waiting —— 门与门之间/停留之外的裁决无门可落。
        self._state: dict[int, str] = {}
        self._lock = asyncio.Lock()

    async def register(self, run_id: int) -> None:
        async with self._lock:
            self._events[run_id] = asyncio.Event()
            self._feedback[run_id] = ""
            self._state[run_id] = "released"

    async def remove(self, run_id: int) -> None:
        async with self._lock:
            self._events.pop(run_id, None)
            self._feedback.pop(run_id, None)
            self._state.pop(run_id, None)

    def accept(self, run_id: int, feedback: str) -> bool:
        """接受裁决并标记 decided。仅当 run 正停在门等待(waiting)时返回 True。"""
        if self._state.get(run_id) != "waiting":
            return False
        self._feedback[run_id] = (feedback or "").strip()
        self._state[run_id] = "decided"
        return True

    def is_waiting(self, run_id: int) -> bool:
        return self._state.get(run_id) == "waiting"

    def release(self, run_id: int) -> None:
        """裁决已落库,唤醒等在门上的 run。无 decided 裁决时为 no-op。"""
        if self._state.get(run_id) == "decided":
            event = self._events.get(run_id)
            if event is not None:
                event.set()

    def abort(self, run_id: int) -> None:
        """放弃本次裁决(裁决后落库失败等):回到 waiting,run 继续在门等待。"""
        if self._state.get(run_id) == "decided":
            self._feedback[run_id] = ""
            self._state[run_id] = "waiting"

    async def wait_confirm(self, run_id: int, timeout: float) -> str:
        """run 每次停在审批门时进入本等待;返回裁决文本(空串 = 用户点了"通过")。

        停留开始时先把状态置回 waiting 并清事件 —— 上一轮停留消费后事件仍为
        set 态,不清除会让下一道门立刻"被通过"(见 released 状态语义);
        本状态机保证清事件时不可能有未消费裁决(accept 只认 waiting,而
        waiting 只在进入本方法时置位),不会重蹈"先到先得"清掉信号的覆辙。
        """
        event = self._events.get(run_id)
        if event is None:
            raise RuntimeError(f"run {run_id} 未注册到确认网关")
        self._state[run_id] = "waiting"
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            raise TimeoutError(f"run {run_id} 等待人工确认超时({timeout}s)")
        feedback = self._feedback.pop(run_id, "")
        self._state[run_id] = "released"
        return feedback


gateway = ConfirmGateway()
