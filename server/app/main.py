from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router

app = FastAPI(title="TRPG Player", version="0.1.0")

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
