from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.session import GameSession
from app.services import session_service


_PLAYER_TOKEN_HEADER = APIKeyHeader(name="X-Player-Token", auto_error=False)


def player_token(
    x_player_token: str | None = Security(_PLAYER_TOKEN_HEADER),
) -> str | None:
    """读取玩家身份 token（局域网 MVP：明文 bearer，无鉴权）。

    前端在 localStorage 生成 UUID，并以 ``X-Player-Token`` 头随请求带上。
    """
    return x_player_token


def require_session_viewer(
    db: Session,
    session_id: str,
    token: str | None,
):
    """统一读取授权：返回可读会话，否则抛出统一的 404/403。"""
    session = session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    if not session_service.can_view_session(db, session_id, token):
        raise HTTPException(403, "无权读取该会话")
    return session


def _raise_actor_error(error: ValueError) -> NoReturn:
    detail = str(error)
    status_code = 404 if detail == "房间不存在" else 403
    raise HTTPException(status_code, detail) from error


def require_session_actor(
    db: Session,
    session_id: str,
    token: str | None,
    acting_character_id: str | None,
) -> Character:
    """统一行动授权：校验显式角色属于当前 token 的真人席位。"""
    try:
        return session_service.resolve_actor(
            db, session_id, token, acting_character_id,
        )
    except ValueError as error:
        _raise_actor_error(error)


def require_session_token_actor(
    db: Session,
    session_id: str,
    token: str | None,
) -> Character:
    """统一无角色参数写授权：按 token 解析真人席位，兼容纯本机旧会话。"""
    try:
        return session_service.resolve_token_actor(db, session_id, token)
    except ValueError as error:
        _raise_actor_error(error)


def require_session_manager(
    db: Session,
    session_id: str,
    token: str | None,
    *,
    detail: str = "只有房主可以管理该会话",
) -> GameSession:
    """统一房主管理授权，兼容没有席位归属的纯本机旧会话。"""
    session = session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    if not session_service.can_manage_session(db, session_id, token):
        raise HTTPException(403, detail)
    return session


def require_session_host(
    db: Session,
    session_id: str,
    token: str | None,
    *,
    detail: str = "只有房主可以执行该操作",
) -> GameSession:
    """统一严格房主授权；与 manager 不同，不放行无归属旧会话。"""
    session = session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    if not session_service.is_host(db, session_id, token):
        raise HTTPException(403, detail)
    return session


def require_session_kp(
    db: Session,
    session_id: str,
    token: str | None,
) -> GameSession:
    """真人 KP 专用授权；KP 席与普通玩家席位权限严格分离。"""
    try:
        return session_service.authorize_kp(db, session_id, token)
    except ValueError as error:
        _raise_actor_error(error)
