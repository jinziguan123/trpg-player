"""AI 模型多配置管理 API"""

from __future__ import annotations

import json
import time
import uuid

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
    # KP 生成走 agent loop（标准工具调用）新路径的开关。**默认开启**（tool_use 为治本方向，
    # 台词走 say() 结构化出口）；仅当 Provider 支持工具（supports_tools）时才实际生效，否则安全
    # 回退旧正则指令路径，见 chat_service._tool_loop_active。
    use_tool_calls: bool = True
    # 模型上下文窗口（token）。0 = 未知，由 resolve_context_window 按模型名启发式回落。
    # 用于「上下文占用预估」判断模型是否还撑得住继续跑团。
    context_window: int = 0
    # 推理档位（reasoning_effort）：minimal/low/medium/high/xhigh 等。空=不下发该参数、用模型默认档。
    # 仅 OpenAI 兼容协议、且模型支持推理时生效；设了会一并省略 temperature（推理模型多拒绝/忽略它）。
    reasoning_effort: str = ""
    # 文生图模型名（如 dall-e-3 / gpt-image-1）。空=不生图。走 OpenAI Images 端点
    # （{base_url}/images/generations），复用本 profile 的 base_url + api_key。用于手书配图。
    image_model: str = ""


class AIProfileCreate(BaseModel):
    name: str
    protocol: str = "openai"
    base_url: str = ""
    model_name: str = ""
    api_key: str = ""
    vision: bool = False
    use_tool_calls: bool = True
    context_window: int = 0
    reasoning_effort: str = ""
    image_model: str = ""


class AIProfileUpdate(BaseModel):
    name: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    model_name: str | None = None
    api_key: str | None = None
    vision: bool | None = None
    use_tool_calls: bool | None = None
    context_window: int | None = None
    reasoning_effort: str | None = None
    image_model: str | None = None


# 常见模型的上下文窗口（token）——用于用户没显式配 context_window 时的启发式回落。
# 只做子串匹配，覆盖主流；未命中回落 _DEFAULT_CONTEXT_WINDOW（偏保守但 ≥ 现有上下文预算）。
_MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("claude", 200_000),
    ("gpt-4o", 128_000),
    ("gpt-4.1", 1_000_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("gemini", 1_000_000),
    ("deepseek", 65_536),
    ("qwen", 131_072),
    ("glm", 131_072),
    ("moonshot", 131_072),
    ("kimi", 131_072),
    ("doubao", 131_072),
    ("yi", 65_536),
]
_DEFAULT_CONTEXT_WINDOW = 65_536


def resolve_context_window(profile: "AIProfile | None") -> int:
    """解析模型的有效上下文窗口：显式配置优先，否则按模型名启发式，最后回落默认值。"""
    if profile and profile.context_window and profile.context_window > 0:
        return profile.context_window
    name = (profile.model_name if profile else "").lower()
    for key, window in _MODEL_CONTEXT_WINDOWS:
        if key in name:
            return window
    return _DEFAULT_CONTEXT_WINDOW


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


# ---------- API 端点 ----------

class AIStatus(BaseModel):
    configured: bool
    name: str | None = None


@router.get("/ai/status", response_model=AIStatus)
def ai_status():
    """开局前置校验：是否存在可用的激活 AI 配置（有 api_key + model_name）。

    前端在创建会话/开场前调用，未配置时引导用户去设置页，避免开场直接失败还无从下手。
    """
    p = load_active_profile()
    ok = bool(p and p.api_key and p.model_name)
    return AIStatus(configured=ok, name=p.name if p else None)


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
        context_window=body.context_window,
        reasoning_effort=body.reasoning_effort,
        image_model=body.image_model,
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
    if body.context_window is not None:
        target.context_window = body.context_window
    if body.reasoning_effort is not None:
        target.reasoning_effort = body.reasoning_effort
    if body.image_model is not None:
        target.image_model = body.image_model
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


async def _test_image(client: httpx.AsyncClient, profile: AIProfile) -> str:
    """真调一次 OpenAI Images 端点，判断该配置能否生图。失败抛错（由端点统一格式化）。"""
    base = profile.base_url.rstrip("/") if profile.base_url else "https://api.openai.com/v1"
    url = f"{base}/images/generations"
    headers = {"Authorization": f"Bearer {profile.api_key}", "Content-Type": "application/json"}
    payload = {"model": profile.image_model, "prompt": "A small grey test square on white background.",
               "n": 1, "size": "1024x1024"}
    resp = await client.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    item = ((resp.json().get("data") or [{}])[0]) or {}
    if item.get("b64_json") or item.get("url"):
        return "生图成功：该配置可用于手书配图。"
    return "端点有响应但未返回图像数据（检查模型名/返回格式）。"


@router.post("/ai/profiles/{profile_id}/test-image", response_model=TestResult)
async def test_profile_image(profile_id: str):
    """测试文生图能力：填了 image_model 后，真打一次 images 端点看能否生图。"""
    profiles = _load_profiles()
    target = next((p for p in profiles if p.id == profile_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="配置不存在")
    if not (getattr(target, "image_model", "") or "").strip():
        return TestResult(success=False, message="未填写「文生图模型」（image_model）", latency_ms=0)
    if target.protocol != "openai":
        return TestResult(success=False, message="文生图仅支持 OpenAI 兼容协议的配置", latency_ms=0)
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:  # 生图慢，给足超时
            result = await _test_image(client, target)
        return TestResult(success=True, message=result, latency_ms=int((time.time() - start) * 1000))
    except httpx.TimeoutException:
        return TestResult(success=False, message="生图超时（>60s）", latency_ms=int((time.time() - start) * 1000))
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "") or e.response.text[:200]
        except Exception:
            detail = e.response.text[:200]
        return TestResult(success=False, message=f"HTTP {e.response.status_code}: {detail}",
                          latency_ms=int((time.time() - start) * 1000))
    except Exception as e:
        return TestResult(success=False, message=str(e), latency_ms=int((time.time() - start) * 1000))


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
    # 设了推理档就按真实调用口径带上（并省略 temperature），让连接测试如实反映能否用
    if getattr(profile, "reasoning_effort", "").strip():
        payload["reasoning_effort"] = profile.reasoning_effort.strip()
        payload.pop("temperature", None)
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
