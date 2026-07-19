"""ComfyUI 文生图客户端：把内网 ComfyUI 实例接进 Provider 的 generate_image 抽象。

时序：占位符注入工作流 → POST /prompt 拿 prompt_id → 轮询 GET /history/{id} →
按 outputs 文件名 GET /view 取图 → 转 base64 返回（与 OpenAI Images 路径契约一致）。

约定：
- 工作流用 ComfyUI「导出 (API)」格式 JSON；正/负提示词处写 PLACEHOLDER_POSITIVE /
  PLACEHOLDER_NEGATIVE 占位（未配置工作流时用内置默认 SDXL 文生图模板）。
- 每次生成把所有 KSampler 的 seed 随机化（否则同提示词恒出同图）。
- ComfyUI 是单队列：进程内互斥，同时只提交一张，排队即等待。
- 失败/超时一律返回 None（配图是可选增强，绝不阻塞跑团主流程）。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import random
import uuid

import httpx

logger = logging.getLogger(__name__)

POSITIVE_PLACEHOLDER = "PLACEHOLDER_POSITIVE"
NEGATIVE_PLACEHOLDER = "PLACEHOLDER_NEGATIVE"

# 生成总超时（提交 + 排队 + 采样 + 取图）与轮询间隔
GENERATE_TIMEOUT_S = 180
POLL_INTERVAL_S = 1.5

# 默认负面提示词：占位符留空时的兜底（通用画质负面）
DEFAULT_NEGATIVE = "text, watermark, lowres, blurry, deformed, bad anatomy"

# 未配置工作流时的内置默认文生图模板（SDXL 通用；ckpt 名须与目标机上的模型一致，
# 建议用户直接粘贴自己导出的工作流，这个模板只是兜底）
DEFAULT_WORKFLOW = {
    "1": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
    "2": {"class_type": "CLIPTextEncode",
          "inputs": {"text": POSITIVE_PLACEHOLDER, "clip": ["1", 1]}},
    "3": {"class_type": "CLIPTextEncode",
          "inputs": {"text": NEGATIVE_PLACEHOLDER, "clip": ["1", 1]}},
    "4": {"class_type": "EmptyLatentImage",
          "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
    "5": {"class_type": "KSampler",
          "inputs": {"seed": 0, "steps": 25, "cfg": 6.5, "sampler_name": "dpmpp_2m_sde",
                     "scheduler": "karras", "denoise": 1.0, "model": ["1", 0],
                     "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
    "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
    "7": {"class_type": "SaveImage",
          "inputs": {"filename_prefix": "trpg_gen", "images": ["6", 0]}},
}

# 单队列互斥：ComfyUI 逐张出图，并发提交只会排队占资源
_gen_lock = asyncio.Lock()


def build_workflow(
    workflow_json: str, prompt: str, negative: str = "",
) -> dict | None:
    """把提示词注入工作流：占位符替换 + KSampler seed 随机化。

    坏 JSON 返回 None（调用方 fail-open）。占位符逐节点替换字符串字段的**精确值**，
    不做子串替换——防止用户工作流里恰含同名子串被误伤。
    """
    if (workflow_json or "").strip():
        try:
            wf = json.loads(workflow_json)
        except json.JSONDecodeError:
            logger.warning("ComfyUI 工作流不是合法 JSON，放弃生成")
            return None
        if not isinstance(wf, dict):
            return None
    else:
        wf = copy.deepcopy(DEFAULT_WORKFLOW)

    for node in wf.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key, val in inputs.items():
            if val == POSITIVE_PLACEHOLDER:
                inputs[key] = prompt
            elif val == NEGATIVE_PLACEHOLDER:
                inputs[key] = (negative or "").strip() or DEFAULT_NEGATIVE
        if node.get("class_type") in ("KSampler", "KSamplerAdvanced"):
            for seed_key in ("seed", "noise_seed"):
                if seed_key in inputs:
                    inputs[seed_key] = random.randint(0, 2**31 - 1)
    return wf


class ComfyUIClient:
    """极薄的 ComfyUI HTTP 客户端。base_url 形如 http://172.30.18.236:8188。"""

    def __init__(self, base_url: str, workflow_json: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self.workflow_json = workflow_json or ""

    async def generate(self, prompt: str, negative: str = "") -> str | None:
        """文生图，返回图片 base64（PNG 原始字节）；任何失败返回 None。"""
        wf = build_workflow(self.workflow_json, prompt, negative)
        if wf is None or not self.base_url:
            return None
        try:
            async with _gen_lock:
                return await asyncio.wait_for(
                    self._generate(wf), timeout=GENERATE_TIMEOUT_S,
                )
        except asyncio.TimeoutError:
            logger.warning("ComfyUI 生成超时（%ss），放弃本张", GENERATE_TIMEOUT_S)
            return None
        except Exception:  # noqa: BLE001 — 配图是可选增强，绝不上抛
            logger.exception("ComfyUI 生成失败（已降级为无图）")
            return None

    async def _generate(self, wf: dict) -> str | None:
        client_id = uuid.uuid4().hex
        # trust_env=False：绝不走系统代理（HTTP_PROXY 等环境变量）。ComfyUI 是内网直连，
        # 开发环境常为访问外网 API 配代理，代理转发内网 IP 会「Server disconnected」。
        async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
            resp = await http.post(
                f"{self.base_url}/prompt",
                json={"prompt": wf, "client_id": client_id},
            )
            resp.raise_for_status()
            prompt_id = resp.json().get("prompt_id")
            if not prompt_id:
                logger.warning("ComfyUI /prompt 未返回 prompt_id：%s", resp.text[:200])
                return None

            while True:
                await asyncio.sleep(POLL_INTERVAL_S)
                hist = await http.get(f"{self.base_url}/history/{prompt_id}")
                hist.raise_for_status()
                entry = hist.json().get(prompt_id)
                if not entry:
                    continue  # 仍在队列/执行中
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    logger.warning("ComfyUI 执行报错：%s", json.dumps(status)[:300])
                    return None
                images = [
                    img
                    for out in (entry.get("outputs") or {}).values()
                    for img in (out.get("images") or [])
                    if img.get("type") == "output"
                ]
                if not images:
                    # 已完成但无输出图（如工作流没有 SaveImage）
                    if status.get("completed"):
                        logger.warning("ComfyUI 完成但无输出图（工作流缺 SaveImage？）")
                        return None
                    continue
                img = images[0]
                view = await http.get(
                    f"{self.base_url}/view",
                    params={
                        "filename": img.get("filename", ""),
                        "subfolder": img.get("subfolder", ""),
                        "type": img.get("type", "output"),
                    },
                )
                view.raise_for_status()
                return base64.b64encode(view.content).decode()
