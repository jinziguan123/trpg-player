from __future__ import annotations

import asyncio
import logging

from app.ai import usage_tracker
from app.services.room_hub import room_hub

logger = logging.getLogger(__name__)


class GenerationManager:
    """单房间生成 task 的生命周期与并发锁。

    阶段 2 起，生成产物不再由本类维护订阅者，而是统一经 ``RoomHub`` 广播给
    房间内所有 ``/live`` 订阅者。本类只负责：保证同一房间同一时刻至多一次生成
    （自由式回合的并发锁），并在生成开始/结束时切换 RoomHub 的 in-flight buffer。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def is_generating(self, room_id: str) -> bool:
        task = self._tasks.get(room_id)
        return task is not None and not task.done()

    def start(self, room_id: str, coro, prelude: list[str] | None = None) -> asyncio.Task:
        if self.is_generating(room_id):
            raise ValueError("该房间正在生成中")
        room_hub.begin_generation(room_id)
        # prelude（如玩家本轮行动事件 + generating）在 begin_generation 之后广播，
        # 从而进入 in-flight buffer：断线重连时可被重放，避免玩家消息「被吞」。
        for chunk in (prelude or []):
            room_hub.broadcast(room_id, chunk)
        # 用 usage_tracker 包一层：本次生成里所有 LLM 子调用（planner/主叙事/validator/
        # 队友/子代理/战斗…）的服务端 usage 按 task 累加，结束时累进本局 session_usage。
        task = asyncio.create_task(usage_tracker.tracked(room_id, coro))
        self._tasks[room_id] = task
        task.add_done_callback(lambda _: self._on_done(room_id))
        return task

    async def cancel(self, room_id: str) -> None:
        """取消正在进行（或僵死）的生成 task 并等其真正结束——供「重新生成」打断卡住的生成。

        协作式取消：task 内部的 CancelledError 会被各 run_* 捕获（并把已生成的半截叙事落库，
        由调用方随后回滚清理）。等待 task 结束后让出一拍，确保 done_callback（end_generation +
        清理 _tasks）执行完毕，这样紧接着的 start 不会撞上「正在生成中」。
        """
        task = self._tasks.get(room_id)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        await asyncio.sleep(0)

    def _on_done(self, room_id: str) -> None:
        room_hub.end_generation(room_id)
        self._tasks.pop(room_id, None)


generation_manager = GenerationManager()
