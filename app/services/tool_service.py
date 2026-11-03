"""Agent 编排层：把「会话 / 上下文 / 工具执行 / 持久化」串起来。

路由层只调用 run_chat_turn，不直接碰模型或工具。

本轮新增：行为信号（signals.py）
- 命中「客户明确要人工」时，**不赌模型判断**，后端直接执行 transfer_to_human；
- 其它信号翻译成一句行为约束注入 system prompt（降追问密度、别重复长解释等）。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChatSession, SessionStatus
from ..schemas import ChatResponse, HandoffInfo, ToolCallRecord
from ..tools import ToolContext, ToolExecution, execute
from . import ai_service, session_service
from . import signals as signal_mod

logger = logging.getLogger(__name__)


SILENT_STATUSES = {SessionStatus.AWAITING_HUMAN, SessionStatus.HUMAN, SessionStatus.CLOSED}


def _handoff_info(payload: dict) -> HandoffInfo:
    return HandoffInfo(
        reason=payload.get("reason") or "",
        reason_code=payload.get("reason_code") or "other",
        status=payload.get("status") or "pending",
        created_at=payload.get("created_at"),
        duplicated=bool(payload.get("duplicated")),
    )


async def run_chat_turn(
    db: AsyncSession,
    customer_id: int,
    message: str,
    session_id: str | None = None,
) -> ChatResponse:
    session: ChatSession = await session_service.get_or_create_session(db, customer_id, session_id)

    # 已经转人工 / 已关闭的会话，AI 不再抢答
    if session.status in SILENT_STATUSES:
        await session_service.append_message(db, session.session_id, "user", message)
        return ChatResponse(
            session_id=session.session_id,
            reply="当前会话已转人工，客服稍后会回复您。",
            session_status=session.status.value,
            tool_calls=[],
        )

    history = await session_service.recent_messages(db, session.session_id)
    await session_service.append_message(db, session.session_id, "user", message)

    ctx = ToolContext(
        db=db,
        customer_id=customer_id,
        session_id=session.session_id,
        session=session,
    )

    # ---- 行为信号：先判定，再决定要不要交给模型 ----
    signal = signal_mod.detect(message, history)
    forced: list[ToolExecution] = []
    if signal.action.get("force_handoff"):
        logger.info(
            "行为信号 %s 触发强制转人工 session=%s reasons=%s",
            signal.code, session.session_id, signal.reasons,
        )
        forced.append(
            await execute(
                "transfer_to_human",
                {
                    "reason": f"客户在对话中明确要求人工（信号：{signal.code}）",
                    "reason_code": "customer_request",
                },
                ctx,
            )
        )

    guidance = signal_mod.guidance_note(signal)
    messages = history + [{"role": "user", "content": message}]
    result = await ai_service.agent_turn(ctx, messages, guidance=guidance)

    executions = forced + result.tool_executions

    # 持久化工具调用（便于审计与人工接手时查看）
    for ex in executions:
        await session_service.append_message(
            db,
            session.session_id,
            "tool",
            content=None,
            tool_name=ex.name,
            tool_args=ex.arguments,
            tool_result=ex.result if ex.ok else {"error": ex.error},
        )

    await session_service.append_message(db, session.session_id, "assistant", result.reply)

    # 工具里可能已经改过 session.status，刷新一次
    await db.refresh(session)

    handoff_payload = next(
        (ex.result for ex in executions if ex.name == "transfer_to_human" and ex.ok and ex.result),
        None,
    )

    return ChatResponse(
        session_id=session.session_id,
        reply=result.reply,
        session_status=session.status.value,
        tool_calls=[
            ToolCallRecord(
                name=ex.name,
                arguments=ex.arguments,
                ok=ex.ok,
                result=ex.result,
                error=ex.error,
                latency_ms=ex.latency_ms,
            )
            for ex in executions
        ],
        handoff=_handoff_info(handoff_payload) if handoff_payload else None,
        signal=signal.to_dict(),
    )
