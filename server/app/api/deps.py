from __future__ import annotations

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

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
