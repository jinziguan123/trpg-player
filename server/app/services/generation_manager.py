from __future__ import annotations

import asyncio
import logging

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

    def start(self, room_id: str, coro) -> asyncio.Task:
        if self.is_generating(room_id):
            raise ValueError("该房间正在生成中")
        room_hub.begin_generation(room_id)
        task = asyncio.create_task(coro)
        self._tasks[room_id] = task
        task.add_done_callback(lambda _: self._on_done(room_id))
        return task

    def _on_done(self, room_id: str) -> None:
        room_hub.end_generation(room_id)
        self._tasks.pop(room_id, None)


generation_manager = GenerationManager()
