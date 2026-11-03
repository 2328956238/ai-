"""会话与消息持久化。"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ChatMessage, ChatSession, SessionStatus

logger = logging.getLogger(__name__)


async def get_session(db: AsyncSession, session_id: str) -> ChatSession | None:
    res = await db.execute(select(ChatSession).where(ChatSession.session_id == session_id))
    return res.scalars().first()


async def get_or_create_session(
    db: AsyncSession, customer_id: int, session_id: str | None = None
) -> ChatSession:
    if session_id:
        session = await get_session(db, session_id)
        if session is not None:
            if session.customer_id != customer_id:
                raise PermissionError("session 不属于当前客户")
            return session
        session = ChatSession(
            session_id=session_id, customer_id=customer_id, status=SessionStatus.ACTIVE
        )
        db.add(session)
        await db.commit()
        return session

    session = ChatSession(
        session_id=str(uuid.uuid4()), customer_id=customer_id, status=SessionStatus.ACTIVE
    )
    db.add(session)
    await db.commit()
    return session


async def append_message(
    db: AsyncSession,
    session_id: str,
    role: str,
    content: str | None = None,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_result: dict | None = None,
) -> None:
    db.add(
        ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
        )
    )
    await db.commit()


async def recent_messages(
    db: AsyncSession, session_id: str, limit: int | None = None
) -> list[dict[str, Any]]:
    """取最近 N 条，转成 OpenAI messages 格式（tool 消息不参与上下文回放）。"""
    limit = limit or settings.chat_history_limit
    res = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id.desc())
        .limit(limit)
    )
    rows = list(reversed(res.scalars().all()))
    messages = []
    for m in rows:
        if m.role in ("user", "assistant") and m.content:
            messages.append({"role": m.role, "content": m.content})
    return messages


async def set_status(db: AsyncSession, session: ChatSession, status: SessionStatus) -> None:
    session.status = status
    await db.commit()
