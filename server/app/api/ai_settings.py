"""AI 模型多配置管理 API"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings

# 配置文件与数据库同目录：dev 下是 server/ai_settings.json（行为不变）；打包运行时落到用户
# 可写的 app-data（跟随 settings.db_path），否则会写进 PyInstaller 临时目录（sys._MEIPASS，
# 退出即删）导致配置读不到 / 重启丢失。
SETTINGS_FILE = settings.db_path.parent / "ai_settings.json"

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ---------- 数据模型 ----------

class AIProfile(BaseModel):
    id: str = ""
    name: str = ""
    protocol: str = "openai"  # "openai" | "anthropic"
    base_url: str = ""
    model_name: str = ""
    api_key: str = ""
    is_active: bool = False
    vision: bool = False  # 是否支持多模态（看图）。显式开关，覆盖按模型名的启发式判断
    # KP 生成走 agent loop（标准工具调用）新路径的开关。默认关闭走旧正则指令路径；
    # 开启且 Provider 支持工具（supports_tools）时才生效，见 chat_service._tool_loop_active。
    use_tool_calls: bool = False


class AIProfileCreate(BaseModel):
    name: str
    protocol: str = "openai"
    base_url: str = ""
    model_name: str = ""
    api_key: str = ""
    vision: bool = False
    use_tool_calls: bool = False


class AIProfileUpdate(BaseModel):
    name: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    model_name: str | None = None
    api_key: str | None = None
    vision: bool | None = None
    use_tool_calls: bool | None = None


class TestResult(BaseModel):
    success: bool
    message: str
    latency_ms: int = 0


# ---------- 存储层 ----------

def _load_raw() -> dict:
    """读取原始 JSON，支持旧格式自动迁移"""
    if not SETTINGS_FILE.exists():
        return {"profiles": []}
    try:
        data = json.loads(SETTINGS_FILE.read_text("utf-8"))
    except Exception:
        return {"profiles": []}

    # 旧格式迁移：{base_url, model_name, api_key} -> {profiles: [...]}
    if "profiles" not in data and ("base_url" in data or "model_name" in data or "api_key" in data):
        old_profile = AIProfile(
            id=str(uuid.uuid4()),
            name="默认配置（迁移）",
            protocol="openai",
            base_url=data.get("base_url", ""),
            model_name=data.get("model_name", ""),
            api_key=data.get("api_key", ""),
            is_active=True,
        )
        new_data = {"profiles": [old_profile.model_dump()]}
        _save_raw(new_data)
        return new_data

    return data


def _save_raw(data: dict) -> None:
    SETTINGS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_profiles() -> list[AIProfile]:
    data = _load_raw()
    return [AIProfile(**p) for p in data.get("profiles", [])]


def _save_profiles(profiles: list[AIProfile]) -> None:
    _save_raw({"profiles": [p.model_dump() for p in profiles]})


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


# ---------- 公开函数（供 get_llm 调用） ----------

def load_active_profile() -> AIProfile | None:
    """返回当前激活的配置，没有则返回 None"""
    profiles = _load_profiles()
    for p in profiles:
        if p.is_active:
            return p
    return None


# 向后兼容：旧代码可能仍调用 load_ai_settings()
def load_ai_settings():
    """向后兼容接口"""
    profile = load_active_profile()
    if profile:
        return profile
    # 回退到 .env 配置
    from app.config import settings
    return AIProfile(
        id="env-fallback",
        name="环境变量默认配置",
        protocol="openai",
        base_url=settings.deepseek_base_url,
        model_name="deepseek-chat",
        api_key=settings.deepseek_api_key,
        is_active=True,
    )


# ---------- API 端点 ----------

@router.get("/ai/profiles", response_model=list[AIProfile])
def list_profiles():
    """列出所有配置（api_key 掩码处理）"""
    profiles = _load_profiles()
    for p in profiles:
        p.api_key = _mask_key(p.api_key)
    return profiles


@router.post("/ai/profiles", response_model=AIProfile)
def create_profile(body: AIProfileCreate):
    """新建配置"""
    profiles = _load_profiles()
    new_profile = AIProfile(
        id=str(uuid.uuid4()),
        name=body.name,
        protocol=body.protocol,
        base_url=body.base_url,
        model_name=body.model_name,
        api_key=body.api_key,
        vision=body.vision,
        use_tool_calls=body.use_tool_calls,
        is_active=len(profiles) == 0,  # 第一个配置自动激活
    )
    profiles.append(new_profile)
    _save_profiles(profiles)
    new_profile.api_key = _mask_key(new_profile.api_key)
    return new_profile


@router.put("/ai/profiles/{profile_id}", response_model=AIProfile)
def update_profile(profile_id: str, body: AIProfileUpdate):
    """更新配置。如果 api_key 包含掩码字符，保留旧 key"""
    profiles = _load_profiles()
    target = None
    for p in profiles:
        if p.id == profile_id:
            target = p
            break
    if not target:
        raise HTTPException(status_code=404, detail="配置不存在")

    if body.name is not None:
        target.name = body.name
    if body.protocol is not None:
        target.protocol = body.protocol
    if body.base_url is not None:
        target.base_url = body.base_url
    if body.model_name is not None:
        target.model_name = body.model_name
    if body.vision is not None:
        target.vision = body.vision
    if body.use_tool_calls is not None:
        target.use_tool_calls = body.use_tool_calls
    if body.api_key is not None:
        # 如果包含掩码字符，说明前端没有修改 key，保留旧值
        if "****" not in body.api_key:
            target.api_key = body.api_key

    _save_profiles(profiles)
    target.api_key = _mask_key(target.api_key)
    return target


@router.delete("/ai/profiles/{profile_id}")
def delete_profile(profile_id: str):
    """删除配置"""
    profiles = _load_profiles()
    new_profiles = [p for p in profiles if p.id != profile_id]
    if len(new_profiles) == len(profiles):
        raise HTTPException(status_code=404, detail="配置不存在")
    # 如果删除的是激活的配置，激活第一个剩余配置
    if not any(p.is_active for p in new_profiles) and new_profiles:
        new_profiles[0].is_active = True
    _save_profiles(new_profiles)
    return {"status": "ok"}


@router.post("/ai/profiles/{profile_id}/activate")
def activate_profile(profile_id: str):
    """设为激活配置"""
    profiles = _load_profiles()
    found = False
    for p in profiles:
        if p.id == profile_id:
            p.is_active = True
            found = True
        else:
            p.is_active = False
    if not found:
        raise HTTPException(status_code=404, detail="配置不存在")
    _save_profiles(profiles)
    return {"status": "ok"}


@router.post("/ai/profiles/{profile_id}/test", response_model=TestResult)
async def test_profile(profile_id: str):
    """测试配置连接"""
    profiles = _load_profiles()
    target = None
    for p in profiles:
        if p.id == profile_id:
            target = p
            break
    if not target:
        raise HTTPException(status_code=404, detail="配置不存在")

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if target.protocol == "anthropic":
                result = await _test_anthropic(client, target)
            else:
                result = await _test_openai(client, target)
        latency = int((time.time() - start) * 1000)
        return TestResult(success=True, message=result, latency_ms=latency)
    except httpx.TimeoutException:
        latency = int((time.time() - start) * 1000)
        return TestResult(success=False, message="连接超时", latency_ms=latency)
    except httpx.HTTPStatusError as e:
        latency = int((time.time() - start) * 1000)
        detail = ""
        try:
            err_body = e.response.json()
            detail = err_body.get("error", {}).get("message", "") or str(err_body)
        except Exception:
            detail = e.response.text[:200]
        return TestResult(
            success=False,
            message=f"HTTP {e.response.status_code}: {detail}",
            latency_ms=latency,
        )
    except Exception as e:
        latency = int((time.time() - start) * 1000)
        return TestResult(success=False, message=str(e), latency_ms=latency)


async def _test_openai(client: httpx.AsyncClient, profile: AIProfile) -> str:
    """使用 OpenAI 兼容协议测试连接"""
    base = profile.base_url.rstrip("/") if profile.base_url else "https://api.deepseek.com"
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {profile.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": profile.model_name,
        "messages": [{"role": "user", "content": "回复OK"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    resp = await client.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return f"连接成功: {content.strip()}"


async def _test_anthropic(client: httpx.AsyncClient, profile: AIProfile) -> str:
    """使用 Anthropic Messages API 测试连接"""
    base = profile.base_url.rstrip("/") if profile.base_url else "https://api.anthropic.com"
    url = f"{base}/v1/messages"
    headers = {
        "x-api-key": profile.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": profile.model_name,
        "messages": [{"role": "user", "content": "回复OK"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    resp = await client.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    content = data["content"][0]["text"]
    return f"连接成功: {content.strip()}"
