"""桌面版后端入口（PyInstaller 打成 sidecar 二进制，由 Tauri 外壳启动）。

在本机 127.0.0.1 上挑一个可用端口起 uvicorn，并把端口以固定前缀打到 stdout，
Tauri 外壳据此轮询 /api/health 就绪后，把窗口指向 http://127.0.0.1:<port>。
"""
from __future__ import annotations

import socket

PORT_LINE_PREFIX = "TRPG_BACKEND_PORT "
PREFERRED_PORT = 8756


def _pick_port(preferred: int = PREFERRED_PORT) -> int:
    """优先用固定端口；被占用则让系统分配一个空闲端口。"""
    for candidate in (preferred, 0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", candidate))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    return preferred


def main() -> None:
    import uvicorn

    from app.main import app

    port = _pick_port()
    # 先告知外壳端口（外壳随后轮询 /api/health 确认就绪，不依赖此行的时序）。
    print(f"{PORT_LINE_PREFIX}{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
