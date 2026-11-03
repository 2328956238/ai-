"""工具注册表 —— 白名单 + 参数校验 + 统一执行入口。

安全设计：
1. 模型只能调用注册表里存在的工具，名字不在白名单里直接拒绝（不是 try/except 兜住，而是根本不派发）。
2. 参数用 pydantic 校验，extra="forbid"，多传字段会被打回。
3. 永远不 eval / exec 模型返回的任何内容，只做 dict 取值和函数调用。
4. 每次执行都记结构化日志，失败不抛出到外层，转成可回填给模型的结果。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChatSession

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """一次工具调用需要的运行时上下文，由后端注入，模型无法伪造。"""

    db: AsyncSession
    customer_id: int
    session_id: str
    session: ChatSession
    # 便于测试与审计
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]          # OpenAI tools 的 JSON Schema
    args_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], Awaitable[dict[str, Any]]]


@dataclass
class ToolExecution:
    name: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    latency_ms: int = 0
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "latency_ms": self.latency_ms,
        }


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    if spec.name in _REGISTRY:
        raise ValueError(f"工具重复注册: {spec.name}")
    _REGISTRY[spec.name] = spec
    logger.debug("注册工具 %s", spec.name)


def get_spec(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def tool_names() -> list[str]:
    return sorted(_REGISTRY)


def openai_tool_schemas() -> list[dict[str, Any]]:
    """转成 OpenAI / DeepSeek 兼容的 tools 参数。"""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }
        for s in _REGISTRY.values()
    ]


async def execute(name: str, raw_args: dict[str, Any] | str | None, ctx: ToolContext) -> ToolExecution:
    """执行一次工具调用。任何异常都转成 ok=False，不向上抛。"""
    started = time.perf_counter()

    spec = get_spec(name)
    if spec is None:
        # 白名单之外：直接拒绝，并且不尝试任何解释执行
        logger.warning("拒绝非白名单工具调用 name=%s session=%s", name, ctx.session_id)
        return ToolExecution(
            name=name, ok=False, error=f"unknown_tool: {name}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    args_dict = raw_args
    if isinstance(args_dict, str):
        import json

        try:
            args_dict = json.loads(args_dict or "{}")
        except json.JSONDecodeError as exc:
            logger.warning("工具参数不是合法 JSON name=%s err=%s", name, exc)
            return ToolExecution(
                name=name, ok=False, error="invalid_arguments: 参数不是合法 JSON",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
    if args_dict is None:
        args_dict = {}
    if not isinstance(args_dict, dict):
        return ToolExecution(
            name=name, ok=False, error="invalid_arguments: 参数必须是对象",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    try:
        parsed = spec.args_model(**args_dict)
    except ValidationError as exc:
        logger.warning("工具参数校验失败 name=%s err=%s", name, exc.errors())
        return ToolExecution(
            name=name, ok=False,
            error="invalid_arguments: " + "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
            ),
            arguments=args_dict,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    try:
        result = await spec.handler(ctx, parsed)
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info(
            "工具执行成功 name=%s session=%s customer=%s latency=%sms",
            name, ctx.session_id, ctx.customer_id, elapsed,
        )
        return ToolExecution(name=name, ok=True, result=result, latency_ms=elapsed,
                             arguments=args_dict)
    except Exception as exc:  # 工具内部异常不能把整个对话打挂
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.exception("工具执行失败 name=%s session=%s err=%s", name, ctx.session_id, exc)
        return ToolExecution(
            name=name, ok=False, error=f"tool_error: {type(exc).__name__}",
            arguments=args_dict, latency_ms=elapsed,
        )
