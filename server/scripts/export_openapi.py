"""导出 FastAPI REST OpenAPI 文档。

该脚本只构建应用对象并调用 ``app.openapi()``，不会进入 lifespan、执行迁移或启动服务。
生成的 JSON 是 REST 契约基线；SSE 流和动态 metadata 仍由手写事件协议维护。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

from app.main import app  # noqa: E402


def main() -> None:
    output = SERVER_DIR / "openapi.json"
    output.write_text(
        json.dumps(
            app.openapi(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"已导出 OpenAPI：{output}")


if __name__ == "__main__":
    main()
