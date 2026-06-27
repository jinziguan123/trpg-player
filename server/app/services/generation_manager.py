from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


class GenerationManager:

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        # 缓存每个会话本次生成已 publish 的全部 chunk，
        # 供中途接入（如刷新页面后重连）的订阅者重放，保证内容完整。
        self._buffers: dict[str, list[str]] = {}

    def is_generating(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    def start(self, session_id: str, coro) -> asyncio.Queue:
        if self.is_generating(session_id):
            raise ValueError("该会话正在生成中")
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[session_id].append(q)
        self._buffers[session_id] = []
        task = asyncio.create_task(coro)
        self._tasks[session_id] = task
        task.add_done_callback(lambda _: self._on_done(session_id))
        return q

    def _on_done(self, session_id: str) -> None:
        for q in self._subscribers.pop(session_id, []):
            q.put_nowait(None)
        self._tasks.pop(session_id, None)
        self._buffers.pop(session_id, None)

    def publish(self, session_id: str, chunk: str) -> None:
        buf = self._buffers.get(session_id)
        if buf is not None:
            buf.append(chunk)
        for q in self._subscribers.get(session_id, []):
            q.put_nowait(chunk)

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        if not self.is_generating(session_id):
            q.put_nowait(None)
        else:
            # 先重放已生成的内容，让中途接入的订阅者看到完整输出，
            # 再加入订阅列表接收后续 chunk。
            for chunk in self._buffers.get(session_id, []):
                q.put_nowait(chunk)
            self._subscribers[session_id].append(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(session_id, [])
        if q in subs:
            subs.remove(q)


generation_manager = GenerationManager()


async def stream_from_queue(
    session_id: str, q: asyncio.Queue,
) -> AsyncIterator[str]:
    try:
        while True:
            chunk = await q.get()
            if chunk is None:
                break
            yield chunk
    finally:
        generation_manager.unsubscribe(session_id, q)
