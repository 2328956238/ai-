"""底层 LLM 客户端（OpenAI 兼容 Chat Completions）。

只负责「发一次请求、拿回 assistant message」，不关心工具业务。
- 超时、重试、错误分类都在这里
- 传输层可替换（测试时注入假 transport，不联网）
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import settings

logger = logging.getLogger(__name__)


class AIConfigError(RuntimeError):
    """配置缺失（例如没有 API Key）。"""


class AICallError(RuntimeError):
    """调用失败（网络 / 5xx / 响应格式异常）。"""


class ToolsUnsupported(RuntimeError):
    """当前模型或网关不支持 tools 参数，调用方应降级到 prompt 模式。"""


Transport = Callable[..., Awaitable[dict[str, Any]]]

_TRANSPORT: Transport | None = None


def set_transport(fn: Transport | None) -> None:
    """测试注入点；生产不要调用。"""
    global _TRANSPORT
    _TRANSPORT = fn


def _require_key() -> str:
    if not settings.ai_api_key:
        raise AIConfigError(
            "未配置 AI_API_KEY。请在 .env 中设置 AI_API_KEY（禁止写进源码）。"
        )
    return settings.ai_api_key


async def _http_chat(payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {_require_key()}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.ai_timeout) as client:
        resp = await client.post(settings.chat_completions_url, json=payload, headers=headers)

    if resp.status_code in (400, 404, 422):
        text = resp.text.lower()
        # 网关不认识 tools 字段时的典型信号
        if "tool" in text and any(k in text for k in ("unknown", "unsupported", "invalid", "not support")):
            raise ToolsUnsupported(resp.text[:300])
        raise AICallError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    if resp.status_code >= 500:
        raise AICallError(f"上游 {resp.status_code}")

    if resp.status_code >= 400:
        raise AICallError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise AICallError(f"响应缺少 choices: {str(data)[:300]}")
    message = choices[0].get("message") or {}
    if "content" not in message and "tool_calls" not in message:
        raise AICallError(f"响应缺少 message 字段: {str(message)[:300]}")
    return message


async def chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = "auto",
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """调用一次模型，返回 assistant message dict。"""
    payload: dict[str, Any] = {
        "model": settings.ai_model,
        "messages": messages,
        "temperature": settings.ai_temperature if temperature is None else temperature,
        "max_tokens": settings.ai_max_tokens if max_tokens is None else max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"

    transport = _TRANSPORT or _http_chat

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(settings.ai_max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type((AICallError, httpx.TransportError)),
            reraise=True,
        ):
            with attempt:
                return await transport(payload)
    except ToolsUnsupported:
        raise
    except AIConfigError:
        raise
    except Exception as exc:
        logger.error("LLM 调用最终失败: %s", exc)
        raise AICallError(str(exc)) from exc

    raise AICallError("unreachable")  # pragma: no cover
