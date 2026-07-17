"""OpenAI 兼容协议的 Provider（DeepSeek / OpenAI / 任意 OpenAI 兼容端点）。"""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.ai import usage_tracker
from app.ai.provider import LLMProvider, StreamDelta, ToolCall

logger = logging.getLogger(__name__)

# 可重试的瞬时传输错误：连接被对端中途掐断/网络抖动/超时——非流式补全（模组解析、校验、
# 转正等）遇到这类错误重试一次往往就成，避免整条请求裸 500。4xx（鉴权/参数）不重试。
_TRANSIENT_ERRORS = (
    httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.ConnectError,
    httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout,
)


class ToolCallAggregator:
    """把 OpenAI 流式 delta 里按 index 分片下发的 tool_calls 聚合成完整调用。

    协议形态：首个分片带 index/id/function.name，后续分片只带 index 与
    function.arguments 的字符串增量；聚合到流结束（finish_reason/[DONE]）才算完整。
    arguments 解析失败归一为空 dict——让执行器以缺参错误回给模型重试，而不是断流。
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict] = {}

    def add(self, delta_tool_calls: list[dict]) -> None:
        for part in delta_tool_calls or []:
            index = int(part.get("index") or 0)
            slot = self._calls.setdefault(
                index, {"id": "", "name": "", "arguments": ""},
            )
            if part.get("id"):
                slot["id"] = part["id"]
            fn = part.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def flush(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            if not slot["name"]:
                continue  # 无名分片是坏流，丢弃
            try:
                arguments = json.loads(slot["arguments"]) if slot["arguments"] else {}
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                logger.warning("工具调用参数 JSON 解析失败: %s", slot["arguments"][:200])
                arguments = {}
            calls.append(ToolCall(
                id=slot["id"] or f"call_{index}", name=slot["name"], arguments=arguments,
            ))
        self._calls.clear()
        return calls


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容协议的通用 Provider（DeepSeek / OpenAI / 任意 OpenAI 兼容端点）。"""

    def __init__(
        self, model: str = "deepseek-chat", base_url: str = "", api_key: str = "",
        vision: bool = False, reasoning_effort: str = "", image_model: str = "",
    ):
        self.model = model
        self._api_key = api_key
        self._vision = vision  # 配置里的显式「支持视觉」开关
        # 推理档位（reasoning_effort：minimal/low/medium/high/xhigh…）。空=不带该参数，用模型默认档。
        self._reasoning_effort = (reasoning_effort or "").strip()
        # 文生图模型名（dall-e-3 / gpt-image-1…）。空=不生图。
        self._image_model = (image_model or "").strip()
        self._client = httpx.AsyncClient(timeout=120.0)
        base = base_url.rstrip("/") if base_url else "https://api.deepseek.com"
        self._api_url = f"{base}/chat/completions"
        self._images_url = f"{base}/images/generations"
        # 最近一次调用的服务端真实 usage（prompt/completion/total_tokens）。每次 complete/stream
        # 结束后更新——调用方须在下一次调用前读取（生成串行化，主叙事后、validator 前读即拿到主叙事那次）。
        self.last_usage: dict | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _apply_reasoning(self, payload: dict) -> dict:
        """按配置带上 reasoning_effort；推理模型多拒绝/忽略 temperature，设了推理档就去掉它。"""
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
            payload.pop("temperature", None)
        return payload

    def supports_image_gen(self) -> bool:
        return bool(self._image_model)

    async def generate_image(self, prompt: str, size: str = "1024x1024") -> str | None:
        """文生图（OpenAI Images 端点 {base}/images/generations）。返回 base64（无 data: 前缀）。

        未配置 image_model 或任何失败一律返回 None——配图是可选增强，**绝不因它失败而中断游戏**。
        不下发 response_format 以兼容 dall-e-3（默认回 url）与 gpt-image-1（默认回 b64_json）：
        两种响应都能解析，回的是 url 时再抓一次转成 base64。
        """
        if not self._image_model:
            return None
        payload = {"model": self._image_model, "prompt": prompt, "size": size, "n": 1}
        try:
            resp = await self._client.post(self._images_url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            item = ((resp.json().get("data") or [{}])[0]) or {}
            if item.get("b64_json"):
                return item["b64_json"]
            if item.get("url"):
                img = await self._client.get(item["url"])
                img.raise_for_status()
                return base64.b64encode(img.content).decode()
        except Exception:
            logger.warning("文生图失败（忽略，不影响游戏）: model=%s", self._image_model, exc_info=True)
        return None

    async def complete(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> str:
        # **内部走流式**再拼回完整字符串：长输出（如模组解析）用非流式常被 DeepSeek 等
        # 中途掐断连接（RemoteProtocolError: incomplete chunked read）——流式增量下发能避免。
        # 对外仍是「给完整结果」的语义。stream_options.include_usage 让流式收尾块带真实 usage。
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format
        self._apply_reasoning(payload)

        # 瞬时传输错误（连接被中途掐断/抖动/超时）与 5xx 重试最多 3 次；4xx 立即抛。
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                parts: list[str] = []
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
                            continue   # 心跳/非 JSON 行
                        if chunk.get("usage"):
                            self.last_usage = chunk["usage"]
                            usage_tracker.add(chunk["usage"])
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        content = (choices[0].get("delta") or {}).get("content")
                        if content:
                            parts.append(content)
                return "".join(parts)
            except _TRANSIENT_ERRORS as e:
                last_exc = e
                logger.warning("补全传输中断，重试 %d/3：%s", attempt + 1, e)
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise            # 4xx（鉴权/参数）不重试
                last_exc = e
                logger.warning("补全 %s，重试 %d/3", e.response.status_code, attempt + 1)
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

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
        self._apply_reasoning(payload)
        resp = await self._client.post(self._api_url, headers=self._headers(), json=payload)
        if resp.status_code >= 400:
            # 把服务端返回体带上，便于定位（如图片格式/数量/尺寸被拒），并给出可读错误
            body = (resp.text or "")[:500]
            raise RuntimeError(f"视觉接口返回 {resp.status_code}：{body}")
        data = resp.json()
        if data.get("usage"):
            self.last_usage = data["usage"]
            usage_tracker.add(data["usage"])
        return data["choices"][0]["message"]["content"]

    def supports_tools(self) -> bool:
        return True  # OpenAI 兼容协议均有 tools 字段；个别端点不支持时由配置层关闭

    async def _iter_stream_chunks(
        self, payload: dict, produced: list[bool],
    ) -> AsyncIterator[dict]:
        """流式打开请求并逐行解析出 JSON chunk（[DONE] 收流、坏 JSON/心跳跳过）。

        **首个可见输出之前**遇瞬时传输错误/5xx 自动重试（最多 3 次）——对应上游「响应头都没发
        就断连」（httpx.RemoteProtocolError: Server disconnected）。一旦调用方已 yield 过可见
        token/工具调用（``produced[0]`` 置真），就绝不重试、原样抛：重试会把已下发的内容重复一遍。
        4xx（鉴权/参数）不重试。调用方在 yield 可见内容后须把 produced[0] 置真。
        """
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with self._client.stream(
                    "POST", self._api_url, headers=self._headers(), json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue  # 心跳/非 JSON 行
                        yield chunk
                return
            except _TRANSIENT_ERRORS as e:
                if produced[0]:
                    raise            # 已下发可见内容 → 不能重试（会重复）
                last_exc = e
                logger.warning("流式建连/传输中断（未产出可见内容），重试 %d/3：%s", attempt + 1, e)
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500 or produced[0]:
                    raise            # 4xx 或已产出 → 不重试
                last_exc = e
                logger.warning("流式 %s（未产出可见内容），重试 %d/3", e.response.status_code, attempt + 1)
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    async def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},   # 收尾块附真实 usage
        }
        if tools:
            payload["tools"] = tools
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        self._apply_reasoning(payload)

        aggregator = ToolCallAggregator()
        produced = [False]
        async for chunk in self._iter_stream_chunks(payload, produced):
            # usage 可能挂在收尾内容块上（DeepSeek）或单独的 choices=[] 块（标准 OpenAI）——
            # 见到 usage 就抓，别只认 choices 为空。
            if chunk.get("usage"):
                self.last_usage = chunk["usage"]
                usage_tracker.add(chunk["usage"])
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                produced[0] = True
                yield StreamDelta(kind="text", text=content)
            if delta.get("tool_calls"):
                aggregator.add(delta["tool_calls"])
            # finish_reason=tool_calls 时该轮调用分片已齐；等到此处才 flush，
            # 保证参数字符串完整（分片乱序/中途 flush 都会产出半截 JSON）。
            if choices[0].get("finish_reason"):
                for call in aggregator.flush():
                    produced[0] = True
                    yield StreamDelta(kind="tool_call", tool_call=call)
        # 兜底：个别端点不发 finish_reason 直接 [DONE]
        for call in aggregator.flush():
            produced[0] = True
            yield StreamDelta(kind="tool_call", tool_call=call)

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
            "stream_options": {"include_usage": True},   # 收尾块附真实 usage
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        self._apply_reasoning(payload)

        produced = [False]
        async for chunk in self._iter_stream_chunks(payload, produced):
            if chunk.get("usage"):
                self.last_usage = chunk["usage"]
                usage_tracker.add(chunk["usage"])
            # 有些 OpenAI 兼容服务会发 choices=[] 的块（usage 统计 / 内容过滤 /
            # keep-alive），不能用 choices[0] 硬取，否则 IndexError 整段断流。
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                produced[0] = True
                yield content
