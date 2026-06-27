from __future__ import annotations

from fastapi import Header


def player_token(x_player_token: str | None = Header(default=None)) -> str | None:
    """读取玩家身份 token（局域网 MVP：明文 bearer，无鉴权）。

    前端在 localStorage 生成 UUID，并以 ``X-Player-Token`` 头随请求带上。
    """
    return x_player_token
