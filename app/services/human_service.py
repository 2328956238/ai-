"""转人工业务逻辑。

不是返回一句"已转人工"就完事，而是：
1. 落 human_handoffs 记录（谁、为什么、什么类型、什么时候）
2. 改会话状态为 awaiting_human，AI 之后不再自动回复
3. 幂等：同一会话只会有一条记录（唯一约束 + 先查后插），
   模型重复调用也只返回同一条，不会产生重复工单。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChatSession, HandoffStatus, HumanHandoff, SessionStatus

logger = logging.getLogger(__name__)


async def request_handoff(
    db: AsyncSession,
    session: ChatSession,
    customer_id: int,
    reason: str,
    reason_code: str = "ai_limit",
) -> dict:
    """创建（或复用）转人工记录。返回给工具的固定结构。"""
    res = await db.execute(select(HumanHandoff).where(HumanHandoff.session_id == session.session_id))
    existing = res.scalars().first()
    if existing is not None:
        logger.info("转人工重复触发，复用既有记录 session=%s", session.session_id)
        return _payload(existing, duplicated=True)

    handoff = HumanHandoff(
        session_id=session.session_id,
        customer_id=customer_id,
        reason=reason,
        reason_code=reason_code,
        status=HandoffStatus.PENDING,
    )
    db.add(handoff)
    session.status = SessionStatus.AWAITING_HUMAN
    session.handoff_at = datetime.now(timezone.utc)

    try:
        await db.commit()
    except IntegrityError:
        # 并发下另一个请求先插入了，回滚后复用那一条
        await db.rollback()
        res = await db.execute(select(HumanHandoff).where(HumanHandoff.session_id == session.session_id))
        existing = res.scalars().first()
        if existing is not None:
            return _payload(existing, duplicated=True)
        raise

    logger.info("已创建转人工工单 session=%s code=%s", session.session_id, reason_code)
    return _payload(handoff, duplicated=False)


def _payload(handoff: HumanHandoff, duplicated: bool) -> dict:
    return {
        "handoff_created": True,
        "duplicated": duplicated,
        "handoff_id": handoff.id,
        "status": handoff.status.value if handoff.status else None,
        "reason_code": handoff.reason_code,
        "reason": handoff.reason,
        "created_at": handoff.created_at.isoformat() if handoff.created_at else None,
        "session_status": SessionStatus.AWAITING_HUMAN.value,
        "next_step": "人工客服将在工作时间内接入，期间 AI 不再自动回复。",
    }


async def accept_handoff(
    db: AsyncSession, session_id: str, agent: str, note: str | None = None
) -> dict:
    """人工客服接管的预留接口。"""
    res = await db.execute(select(HumanHandoff).where(HumanHandoff.session_id == session_id))
    handoff = res.scalars().first()
    if handoff is None:
        return {"ok": False, "reason": "handoff_not_found"}

    handoff.status = HandoffStatus.ACCEPTED
    handoff.accepted_at = datetime.now(timezone.utc)
    handoff.accepted_by = agent
    handoff.note = note

    session = (await db.execute(
        select(ChatSession).where(ChatSession.session_id == session_id)
    )).scalars().first()
    if session is not None:
        session.status = SessionStatus.HUMAN
    await db.commit()
    return {"ok": True, "session_status": SessionStatus.HUMAN.value, "agent": agent}


async def close_session(db: AsyncSession, session_id: str) -> dict:
    session = (await db.execute(
        select(ChatSession).where(ChatSession.session_id == session_id)
    )).scalars().first()
    if session is None:
        return {"ok": False, "reason": "session_not_found"}
    session.status = SessionStatus.CLOSED
    await db.commit()
    return {"ok": True, "session_status": SessionStatus.CLOSED.value}
