import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动即把数据库升到最新；失败不阻断启动（多数功能不依赖最新迁移），但醒目记录。
    from app.database import run_migrations

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
