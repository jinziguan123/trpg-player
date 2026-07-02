"""OpenAI 兼容协议的 Provider（DeepSeek / OpenAI / 任意 OpenAI 兼容端点）。"""

import json
from collections.abc import AsyncIterator

import httpx

from app.ai.provider import LLMProvider


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容协议的通用 Provider（DeepSeek / OpenAI / 任意 OpenAI 兼容端点）。"""

    def __init__(self, model: str = "deepseek-chat", base_url: str = "", api_key: str = "", vision: bool = False):
        self.model = model
        self._api_key = api_key
        self._vision = vision  # 配置里的显式「支持视觉」开关
        self._client = httpx.AsyncClient(timeout=120.0)
        base = base_url.rstrip("/") if base_url else "https://api.deepseek.com"
        self._api_url = f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> str:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        # 不主动施加输出上限：max_tokens 为 None 时不下发，交由服务端默认/上限。
        # 推理类模型的 reasoning 会占用输出预算，硬上限会让长局/复杂裁定后续无内容可生成。
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format

        resp = await self._client.post(
            self._api_url, headers=self._headers(), json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        # content 可能为 null（推理模型只填 reasoning_content、内容被过滤等）→ 归一为空串，
        # 免得下游把 None 当合法输出。
        return data["choices"][0]["message"].get("content") or ""

    # 视觉能力按模型名启发式判断（deepseek-chat 等纯文本模型返回 False）
    _VISION_HINTS = ("gpt-4o", "gpt-4.1", "gpt-4-vision", "o4", "vision", "claude",
                     "gemini", "qwen-vl", "qwen2-vl", "qwen2.5-vl", "glm-4v", "llava", "internvl", "yi-vision")

    def supports_vision(self) -> bool:
        if self._vision:   # 配置里显式开了视觉 → 直接认可（覆盖名字猜测）
            return True
        m = (self.model or "").lower()
        return any(h in m for h in self._VISION_HINTS)

    async def complete_vision(
        self, prompt: str, images: list[tuple[str, str]], max_tokens: int | None = None,
    ) -> str:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for b64, mime in images:
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        payload: dict = {"model": self.model, "messages": [{"role": "user", "content": content}], "temperature": 0.4}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        resp = await self._client.post(self._api_url, headers=self._headers(), json=payload)
        if resp.status_code >= 400:
            # 把服务端返回体带上，便于定位（如图片格式/数量/尺寸被拒），并给出可读错误
            body = (resp.text or "")[:500]
            raise RuntimeError(f"视觉接口返回 {resp.status_code}：{body}")
        return resp.json()["choices"][0]["message"]["content"]

    async def stream(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        async with self._client.stream(
            "POST", self._api_url, headers=self._headers(), json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue  # 忽略心跳/非 JSON 行
                # 有些 OpenAI 兼容服务会发 choices=[] 的块（usage 统计 / 内容过滤 /
                # keep-alive），不能用 choices[0] 硬取，否则 IndexError 整段断流。
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content
