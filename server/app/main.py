import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router

logger = logging.getLogger(__name__)

# 迁移失败进入的「维护模式」：非空即代表启动迁移失败，所有请求返回可读错误页而非
# 以「新代码 + 旧 schema」带病运行。开发与打包环境都启用，避免业务接口继续返回 500。
_MIGRATION_ERROR: str | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 打包首次启动先从内置种子初始化 app-data（开箱即用），再把数据库升到最新（升级前自动备份）。
    global _MIGRATION_ERROR
    from app.database import run_migrations, seed_if_needed

    seed_if_needed()
    try:
        run_migrations()
    except Exception as e:
        logger.exception("启动自动迁移失败，请手动执行 alembic upgrade head")
        # 不以半新半旧 schema 运行，进入维护模式，请求返回可读错误页。
        _MIGRATION_ERROR = str(e)
    yield


app = FastAPI(title="TRPG Player", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _maintenance_gate(request: Request, call_next):
    """迁移失败的维护模式：除健康检查外一律返回可读错误页，提示用户升级/从备份恢复。"""
    if _MIGRATION_ERROR is not None and request.url.path != "/api/health":
        return HTMLResponse(
            "<html><head><meta charset='utf-8'><title>需要维护</title></head>"
            "<body style='font-family:serif;background:#0c0e13;color:#e8dcc0;"
            "text-align:center;padding:12vh 8vw'>"
            "<h1>数据库升级未完成</h1>"
            "<p>本次启动的自动迁移失败，为保护你的存档，应用已暂停运行。</p>"
            f"<pre style='color:#98a2ad;white-space:pre-wrap'>{_MIGRATION_ERROR}</pre>"
            "<p>迁移前的自动备份位于数据目录下的 <code>trpg.db.bak-*</code>；"
            "可升级到匹配的程序版本后重试，或从备份恢复。</p>"
            "</body></html>",
            status_code=503,
        )
    return await call_next(request)

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
