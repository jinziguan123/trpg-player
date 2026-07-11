"""按异步任务累加「本次生成」里所有 LLM 调用的服务端 usage，用于「本局累计 token 消耗」。

用 contextvar 承载累加器：天然按 asyncio task 隔离（并发多局互不干扰），且自动传播到
task 内 await 的所有子调用——一个回合里 planner、主叙事、validator、AI 队友、NPC/幕后
子代理、战斗叙述等即便用不同的 Provider 实例，也都记进同一个累加器。

Provider 每拿到服务端 usage 就 ``add()``；生成入口协程由 ``generation_manager`` 用
``tracked()`` 包一层，结束（含取消/异常）时把本次合计累进 ``world_state.session_usage``。
无累加器（如脱离生成的零散调用）时 ``add()`` 静默忽略；全程 fail-open。
"""

from __future__ import annotations

import contextvars
import logging

logger = logging.getLogger(__name__)

_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "calls")
_acc: contextvars.ContextVar[dict | None] = contextvars.ContextVar("llm_usage_acc", default=None)


def _zero() -> dict:
    return {k: 0 for k in _FIELDS}


def add(usage: dict | None) -> None:
    """把一次调用的服务端 usage 累加进当前任务的累加器（无累加器/无效 usage 时忽略）。"""
    if not isinstance(usage, dict):
        return
    acc = _acc.get()
    if acc is None:
        return
    acc["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
    acc["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    acc["total_tokens"] += int(usage.get("total_tokens") or 0)
    acc["calls"] += 1


def snapshot() -> dict:
    """取当前任务累加器的合计（无则全 0）。"""
    acc = _acc.get()
    return dict(acc) if acc else _zero()


def accumulate(ws: dict | None, snap: dict) -> dict:
    """把一次生成的 usage 合计累进 world_state.session_usage（纯函数，返回新 ws，单调累增）。"""
    cur = dict((ws or {}).get("session_usage") or _zero())
    for k in _FIELDS:
        cur[k] = int(cur.get(k) or 0) + int(snap.get(k) or 0)
    new_ws = dict(ws or {})
    new_ws["session_usage"] = cur
    return new_ws


async def tracked(session_id: str, coro) -> None:
    """包住一个生成协程：起累加器 → 执行 → 把本次合计累进该局 session_usage（fail-open）。

    取消/异常时 finally 仍会把已产生的用量记账（半截生成也花了 token），随后原样上抛。
    """
    _acc.set(_zero())
    try:
        await coro
    finally:
        snap = snapshot()
        if snap.get("calls"):
            _persist(session_id, snap)


def _persist(session_id: str, snap: dict) -> None:
    from app.database import SessionLocal
    from app.models.session import GameSession

    db = SessionLocal()
    try:
        gs = db.get(GameSession, session_id)
        if gs is not None:
            gs.world_state = accumulate(dict(gs.world_state or {}), snap)
            db.commit()
    except Exception:
        logger.exception("累计本局 token 用量失败（忽略）: session=%s", session_id)
        db.rollback()
    finally:
        db.close()
