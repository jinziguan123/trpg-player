import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 打包首次启动先从内置种子初始化 app-data（开箱即用），再把数据库升到最新；
    # 失败不阻断启动（多数功能不依赖最新迁移），但醒目记录。
    from app.database import run_migrations, seed_if_needed

    seed_if_needed()
    try:
        run_migrations()
    except Exception:
        logger.exception("启动自动迁移失败，请手动执行 alembic upgrade head")
    yield


app = FastAPI(title="TRPG Player", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # 打包客户端 / 跨机联机：客户端从任意 origin（tauri://、其它机器）连主机后端。
    # 鉴权走 X-Player-Token 头、不用 cookie，故允许任意来源是安全的。
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# 单服务 / 打包模式：若存在前端构建产物（apps/web/dist），由后端同源托管，Tauri 窗口直接指向
# 本机后端即可（同源 → /api、SSE 都不涉及跨域）。dev 下 dist 不存在则跳过，前端仍走 vite。
# 打包（PyInstaller frozen）时前端会被放到 sys._MEIPASS，下面按需覆盖。
def _frontend_dist() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "web_dist"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"


_DIST = _frontend_dist()
if _DIST.is_dir():
    _assets = _DIST / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/{full_path:path}")
    def _spa(full_path: str):
        # /api/* 未命中的一律 404（交给 API 层语义），其余非 API 路径回退到 SPA 入口，
        # 由前端路由接管（刷新 /game/:id 等深链也能正常返回 index.html）。
        if full_path.startswith("api"):
            raise HTTPException(status_code=404)
        candidate = (_DIST / full_path).resolve()
        if full_path and candidate.is_file() and _DIST in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
