from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


class RoomHub:
    """房间级常驻广播通道（阶段 2 实时联机的唯一输出出口）。

    与 ``GenerationManager`` 不同：订阅者集合**不随单次生成结束而清空**，
    而是与成员的 ``/live`` SSE 连接同寿命。生成产物、玩家行动、检定、OOC、
    入座/在场等一切要让全房间看到的事件，都经 ``broadcast`` 下发。

    ``_inflight`` 缓存「当前生成」已广播的 chunk，供生成期间中途接入的订阅者
    立即重放，看到正在流式的叙述；生成结束即清空。离散持久事件的可靠补全
    由 ``/live`` 连接时从 ``event_logs`` 重放负责，不依赖本 buffer。
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._inflight: dict[str, list[str]] = {}

    def subscribe(self, room_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        # 生成中途接入：先把当前生成已广播的 chunk 重放给新订阅者
        for chunk in self._inflight.get(room_id, []):
            q.put_nowait(chunk)
        self._subs[room_id].append(q)
        return q

    def unsubscribe(self, room_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(room_id, [])
        if q in subs:
            subs.remove(q)
        if not subs:
            self._subs.pop(room_id, None)

    def broadcast(self, room_id: str, chunk: str) -> None:
        buf = self._inflight.get(room_id)
        if buf is not None:
            buf.append(chunk)
        for q in self._subs.get(room_id, []):
            q.put_nowait(chunk)

    def begin_generation(self, room_id: str) -> None:
        self._inflight[room_id] = []

    def end_generation(self, room_id: str) -> None:
        self._inflight.pop(room_id, None)

    def member_count(self, room_id: str) -> int:
        return len(self._subs.get(room_id, []))


room_hub = RoomHub()


async def stream_room(room_id: str, q: asyncio.Queue) -> AsyncIterator[str]:
    """把房间订阅队列转成 SSE 文本流；连接断开时自动退订。"""
    try:
        while True:
            chunk = await q.get()
            if chunk is None:
                break
            yield chunk
    finally:
        room_hub.unsubscribe(room_id, q)
