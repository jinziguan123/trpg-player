"""生成错误分类与后台收尾任务生命周期。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from app.ai import usage_tracker

logger = logging.getLogger(__name__)

HousekeepingFn = Callable[[object, str, object], Awaitable[None]]


def classify_llm_error(exc: BaseException) -> str:
    """把底层异常翻成对玩家可行动的一句话；无法归类时返回空串。"""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "鉴权失败，请到设置页检查 API Key 是否正确并已激活"
        if code == 429:
            return "被限流或额度不足，请稍后重试或检查账户额度"
        if code >= 500:
            return "AI 服务端错误，通常稍后重试即可"
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.ReadTimeout):
        return "连接 AI 服务失败，请检查网络或设置页的 base_url"
    return ""


class HousekeepingManager:
    """管理每个房间至多一个后台收尾任务，并在下一轮开始前排干。"""

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}

    async def drain(self, session_id: str) -> None:
        task = self.tasks.pop(session_id, None)
        if task is not None and not task.done():
            try:
                await task
            except BaseException:
                pass

    def spawn(
        self,
        session_id: str,
        llm,
        summarize: HousekeepingFn,
        backstage: HousekeepingFn,
    ) -> None:
        async def run() -> None:
            from app.database import SessionLocal

            db = SessionLocal()
            started_at = time.monotonic()
            try:
                await summarize(db, session_id, llm)
                await backstage(db, session_id, llm)
            finally:
                db.close()
                elapsed = time.monotonic() - started_at
                if elapsed > 0.1:
                    logger.info("耗时|收尾（后台）%.1fs session=%s", elapsed, session_id)

        self.tasks[session_id] = asyncio.create_task(
            usage_tracker.tracked(session_id, run()),
        )
